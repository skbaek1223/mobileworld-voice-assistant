"""늘밥 voice assistant as a mobile web app (works on Android and iPhone browsers).

Phone ──WebSocket──> this server ──> STT (OpenAI) -> VoiceAssistantSession (rules / LLM /
mock 늘밥 app) -> each sentence -> TTS (OpenAI) -> mp3 back to the phone, played in order.

All phones share one family (one MockNeulbabApp), so an order placed on the parent's phone
shows up on the guardian's phone. The OpenAI key never leaves the server; an access code
keeps strangers from spending it.

Protocol (JSON messages):
  phone -> server  {"type": "start"} | {"type": "text", "text"} |
                   {"type": "audio", "mime", "data": <base64>} | {"type": "silence"} |
                   {"type": "state"}
  server -> phone  {"type": "say", "id", "text"} | {"type": "audio", "id", "data", "mime"} |
                   {"type": "heard", "text"} | {"type": "thinking"} | {"type": "listen"} |
                   {"type": "done", "status", "reply"} | {"type": "busy"} |
                   {"type": "state", ...} | {"type": "error", "message"}
"""

from __future__ import annotations

import asyncio
import base64
import json
import queue
import re
import secrets
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
)
from fastapi.staticfiles import StaticFiles
from loguru import logger

from mobile_world.assistant.device import ToolsOnlyDevice
from mobile_world.assistant.neulbab import PROMPT_EXAMPLES, MockNeulbabApp, build_toolset
from mobile_world.assistant.neulbab.fast_paths import build_router
from mobile_world.assistant.neulbab.toolset import (
    build_context_provider,
    commit_tools,
    role_instructions,
)
from mobile_world.assistant.scope import AppScopeGuard
from mobile_world.assistant.session import VoiceAssistantSession
from mobile_world.assistant.speech import SpeechToText, TextToSpeech

STATIC_DIR = Path(__file__).with_name("static")
ROLES = ("parent", "guardian")
ANSWER_TIMEOUT_SEC = 20.0  # how long to wait for a reply to the assistant's question
VOICE_PAGES = {"parent.html": "parent", "child.html": "guardian"}  # 늘밥 pages that get a mic
CODE_SNIPPET = (
    "<script>try{var c=new URLSearchParams(location.search).get('code');"
    "if(c)localStorage.setItem('nb_code',c)}catch(e){}</script>"
)
WIDGET_TAGS = '<script src="/voice/widget.js"></script>\n'  # its CSS loads inside a shadow root


@dataclass
class WebConfig:
    model_name: str = "gpt-5"
    llm_base_url: str = "https://api.openai.com/v1"
    api_key: str = ""
    reasoning_effort: str | None = "minimal"
    access_code: str | None = field(default_factory=lambda: f"{secrets.randbelow(10**6):06d}")
    filler_after_sec: float | None = 1.0


class Family:
    """The shared mock app. Tool calls from several phones are serialized with a lock."""

    def __init__(self, app: MockNeulbabApp):
        self.app = app
        self.lock = threading.RLock()
        self.version = 0
        self.connections: set[Connection] = set()
        original = app.call

        def locked(role, name, args=None):
            with self.lock:
                return original(role, name, args)

        app.call = locked  # type: ignore[method-assign]

    # ---- sync with the 늘밥 web screens (their localStorage uses the same shapes)
    def snapshot(self) -> dict:
        with self.lock:
            return {
                "type": "sync",
                "version": self.version,
                "settings": self.app.settings,
                "orders": self.app.orders,
            }

    def apply_from_screen(self, settings: Any, orders: Any) -> bool:
        """A phone changed settings/orders by tapping the 늘밥 screen: adopt them."""
        if not (settings is None or isinstance(settings, dict)) or not isinstance(orders, list):
            return False
        if not all(isinstance(o, dict) and "id" in o and "createdAt" in o for o in orders):
            return False
        with self.lock:
            self.app.settings = settings
            self.app.orders = orders
            self.app.settings_rev += 1  # pending voice confirmations are now stale
            self.app.confirmations.clear()
            self.app._save()
        return True

    def broadcast(self) -> None:
        with self.lock:
            self.version += 1
        snap = self.snapshot()
        for conn in list(self.connections):
            conn.send(snap)

    def state(self, role: str) -> dict:
        call = self.app.call
        ctx = call(role, "get_context", {}).get("data", {})
        order = call(role, "get_order_status", {})
        rec = call(role, "get_recommendations", {})
        o = order.get("data") if order.get("ok") else None
        return {
            "type": "state",
            "role": role,
            "honorific": ctx.get("parent_honorific"),
            "order": None
            if not o
            else {
                k: o.get(k)
                for k in (
                    "menu_name",
                    "status",
                    "status_label",
                    "status_desc",
                    "eta_text",
                    "ordered_by_text",
                )
            },
            "menus": [m["name"] for m in rec.get("data", {}).get("menus", [])]
            if rec.get("ok")
            else [],
            "health_note": rec.get("data", {}).get("health_note", "") if rec.get("ok") else "",
        }


# --------------------------------------------------------------------------- per-phone speech
class WebTTS(TextToSpeech):
    """Sends each sentence as text right away and its mp3 as soon as it is synthesized.
    Synthesis runs in parallel; the phone plays clips in ``id`` order."""

    def __init__(self, send: Callable[[dict], None], synthesize: Callable[[str], bytes], pool):
        self.send, self.synthesize, self.pool = send, synthesize, pool
        self._next = 0
        self._lock = threading.Lock()

    def speak(self, text: str) -> None:
        with self._lock:
            self._next += 1
            sid = self._next
        self.send({"type": "say", "id": sid, "text": text})

        def work():
            audio = self.synthesize(text) if self.synthesize else b""
            self.send(
                {
                    "type": "audio",
                    "id": sid,
                    "mime": "audio/mpeg",
                    "data": base64.b64encode(audio).decode() if audio else "",
                }
            )

        self.pool.submit(work)


class WebSTT(SpeechToText):
    """The session asks a question -> the phone is told to listen -> its next utterance
    (or a silence/timeout) becomes the answer."""

    def __init__(self, send: Callable[[dict], None]):
        self.send = send
        self.answers: queue.Queue[str] = queue.Queue()
        self.waiting = threading.Event()

    def listen(self, prompt: str | None = None) -> str:
        self.waiting.set()
        self.send({"type": "listen"})
        try:
            return self.answers.get(timeout=ANSWER_TIMEOUT_SEC)
        except queue.Empty:
            return ""
        finally:
            self.waiting.clear()


class Connection:
    def __init__(
        self, ws: WebSocket, role: str, family: Family, cfg: WebConfig, voice, agent_factory
    ):
        self.ws, self.role, self.family, self.voice = ws, role, family, voice
        self.loop = asyncio.get_running_loop()
        self.pool = ThreadPoolExecutor(max_workers=4)
        self.busy = threading.Event()
        self.closed = False
        app = family.app
        tools = build_toolset(app, role)
        self.screen = ""  # what the phone's 늘밥 screen shows right now (sent by the widget)
        self._app_state = build_context_provider(app, role)
        self.tts = WebTTS(self.send, voice.synthesize if voice else None, self.pool)
        self.stt = WebSTT(self.send)
        self.session = VoiceAssistantSession(
            agent_factory(tools, role),
            ToolsOnlyDevice(app.contract["package"]),
            self.stt,
            self.tts,
            AppScopeGuard(
                app.contract["package"], app_name="늘밥", tool_names=tools.names, allow_gui=False
            ),
            tools=tools,
            app_name="늘밥",
            tool_wait_time=0.0,
            router=build_router(role, app.errors),
            context_provider=self._context,
            commit_tools=commit_tools(app, role),
            filler_after_sec=cfg.filler_after_sec,
        )

    def send(self, msg: dict) -> None:
        """Thread-safe send (sessions and TTS run in worker threads)."""
        if self.closed:
            return
        fut = asyncio.run_coroutine_threadsafe(self.ws.send_json(msg), self.loop)
        fut.add_done_callback(lambda f: f.exception())  # don't leak "never retrieved" errors

    def _turn(self, text: str) -> None:
        try:
            self.send({"type": "thinking"})
            result = self.session.handle_request(text)
            self.send({"type": "done", "status": result.status, "reply": result.reply})
            self.family.broadcast()  # every phone's 늘밥 screen shows the change
            self.send(self.family.state(self.role))
        except Exception as e:  # never let a turn kill the connection
            logger.exception("turn failed")
            self.send({"type": "error", "message": str(e)})
        finally:
            self.busy.clear()

    def utterance(self, text: str) -> None:
        text = (text or "").strip()
        if self.stt.waiting.is_set():  # it is the answer to the assistant's question
            self.stt.answers.put(text)
            return
        if not text:
            return
        if self.busy.is_set():
            self.send({"type": "busy"})
            return
        self.busy.set()
        threading.Thread(target=self._turn, args=(text,), daemon=True).start()

    def _context(self) -> str:
        state = self._app_state()
        if self.screen:
            state += f"\nScreen the user is looking at now: {self.screen}"
        return state

    def set_screen(self, screen: Any) -> None:
        if isinstance(screen, str):
            self.screen = screen[:400]

    def audio(self, data_b64: str, mime: str, transcribe_only: bool = False) -> None:
        """``transcribe_only``: the widget handles the words itself (e.g. 네/아니요 on a
        screen's own confirmation), so no assistant turn is started."""

        def work():
            text = self.voice.transcribe(base64.b64decode(data_b64), mime) if self.voice else ""
            self.send({"type": "heard", "text": text, "transcribe_only": transcribe_only})
            if not transcribe_only:
                self.utterance(text)

        self.pool.submit(work)

    def speak_only(self, text: Any) -> None:
        """Read on-screen guidance aloud (TTS only, no LLM)."""
        if not isinstance(text, str) or not text.strip():
            return
        # one clip per sentence: the short first one ("오늘 뭐 드실래요?") is ready
        # quickly and plays while the rest is still being synthesized
        for sentence in re.split(r"(?<=[.?!])\s+", text.strip()[:400]):
            if sentence.strip():
                self.tts.speak(sentence.strip())

    def close(self) -> None:
        self.closed = True
        self.stt.answers.put("")  # release a session waiting for an answer
        self.pool.shutdown(wait=False, cancel_futures=True)


# --------------------------------------------------------------------------- app
def create_app(
    family: Family,
    cfg: WebConfig,
    voice=None,
    agent_factory: Callable[[Any, str], Any] | None = None,
    neulbab_app_dir: str | Path | None = None,
) -> FastAPI:
    """``voice``: OpenAIVoice (or a fake in tests); ``agent_factory(tools, role)`` -> agent.

    With ``neulbab_app_dir`` (agent-company/product/app) the phones get the original 늘밥
    screens, unchanged, with the voice widget added to the parent and guardian pages.
    Without it, ``/`` is the standalone voice page. That page is always at ``/voice/``.
    """
    if agent_factory is None:
        from mobile_world.assistant.agent import InAppAssistantAgent

        def agent_factory(tools, role):
            return InAppAssistantAgent(
                model_name=cfg.model_name,
                llm_base_url=cfg.llm_base_url,
                api_key=cfg.api_key,
                app_name="늘밥",
                tools=tools.schemas(),
                gui=False,
                extra_instructions=role_instructions(role),
                reasoning_effort=cfg.reasoning_effort,
                **PROMPT_EXAMPLES,
            )

    app = FastAPI(title="늘밥 음성 비서")

    def code_ok(code: str | None) -> bool:
        return cfg.access_code is None or secrets.compare_digest(code or "", cfg.access_code)

    @app.get("/api/check")
    def check(code: str = ""):
        return JSONResponse({"ok": code_ok(code), "code_required": cfg.access_code is not None})

    @app.websocket("/ws")
    async def ws_endpoint(ws: WebSocket):
        code = ws.query_params.get("code")
        role = ws.query_params.get("role", "parent")
        if not code_ok(code) or role not in ROLES:
            await ws.close(code=4401)
            return
        await ws.accept()
        conn = Connection(ws, role, family, cfg, voice, agent_factory)
        family.connections.add(conn)
        conn.send(family.snapshot())
        logger.info(f"[web] {role} connected")
        try:
            while True:
                msg = json.loads(await ws.receive_text())
                kind = msg.get("type")
                if kind == "start":
                    conn.send(family.state(role))
                    greeting = (
                        "늘밥 비서예요. 드시고 싶은 걸 말씀해 주세요."
                        if role == "parent"
                        else "늘밥 보호자 비서예요. 무엇을 도와드릴까요?"
                    )
                    conn.tts.speak(greeting)
                elif kind == "text":
                    conn.set_screen(msg.get("screen"))
                    conn.send({"type": "heard", "text": msg.get("text", "")})
                    conn.utterance(msg.get("text", ""))
                elif kind == "audio":
                    conn.set_screen(msg.get("screen"))
                    conn.audio(
                        msg.get("data", ""),
                        msg.get("mime", "audio/webm"),
                        transcribe_only=bool(msg.get("transcribe_only")),
                    )
                elif kind == "speak":
                    conn.speak_only(msg.get("text"))
                elif kind == "silence":
                    conn.utterance("")
                elif kind == "state":
                    conn.send(family.state(role))
                elif kind == "sync_up":  # changed by tapping the 늘밥 screen
                    if family.apply_from_screen(msg.get("settings"), msg.get("orders")):
                        family.broadcast()
        except WebSocketDisconnect:
            pass
        finally:
            family.connections.discard(conn)
            conn.close()
            logger.info(f"[web] {role} disconnected")

    app.mount("/voice", StaticFiles(directory=STATIC_DIR, html=True), name="voice")

    if neulbab_app_dir is None:

        @app.get("/")
        def root():
            return RedirectResponse("/voice/")

        return app

    app_root = Path(neulbab_app_dir).resolve()

    @app.get("/")
    def home(request: Request):
        query = request.url.query  # keep ?code=... for the page to remember
        return RedirectResponse("/index.html" + (f"?{query}" if query else ""))

    @app.get("/{path:path}")
    def neulbab_file(path: str):
        target = (app_root / path).resolve()
        if not target.is_relative_to(app_root) or not target.is_file():
            return PlainTextResponse("Not found", status_code=404)
        if target.suffix == ".html" and target.parent == app_root:
            html = target.read_text(encoding="utf-8")
            # remember ?code=... before the page's own login redirect drops the query
            html = html.replace("<head>", "<head>" + CODE_SNIPPET, 1)
            if target.name in VOICE_PAGES:
                html = html.replace("</body>", WIDGET_TAGS + "</body>", 1)
            return HTMLResponse(html)
        return FileResponse(target)

    return app
