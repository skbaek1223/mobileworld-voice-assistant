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
    Response,
)
from fastapi.staticfiles import StaticFiles
from loguru import logger

from mobile_world.assistant.device import ToolsOnlyDevice
from mobile_world.assistant.neulbab import BRAND, PROMPT_EXAMPLES, MockNeulbabApp, build_toolset
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
    "if(c)localStorage.setItem('nb_code',c);"
    # one design only: the A/B chooser is removed and the app uses design B
    # (one big photo card at a time), which the user picked for 밥마중
    "localStorage.setItem('neulbab_variant','b')}catch(e){}</script>"
)
# The service is now called 밥마중: the prototype's own name is swapped when its files are
# served (pages, scripts, app manifest, icon title). "늘밥구" is a made-up district in the
# sample address, not the service name, so it is left alone.
OLD_NAME = re.compile(r"늘밥(?!구)")
RENAMED_TYPES = {".html": "text/html", ".js": "text/javascript", ".webmanifest": "application/manifest+json", ".svg": "image/svg+xml"}  # fmt: skip

# The 늘밥 home page offers an A/B design chooser for its research study; the voice app
# uses one design, so the chooser ("화면 디자인을 바꿔 볼 수 있어요" + A안/B안 buttons) is removed.
DESIGN_CHOOSER = re.compile(
    r'<p class="note variant-help">.*?</p>\s*<nav class="links" aria-label="디자인 고르기">.*?</nav>',
    re.DOTALL,
)
WIDGET_TAGS = '<script src="/voice/widget.js"></script>\n'  # its CSS loads inside a shadow root

# 밥마중 app look (design B, persimmon colours). Applied when the prototype's files are served;
# the prototype's own files are never edited.
THEME_COLOR = "#C2410C"
OLD_THEME_COLOR = "#8E3210"
THEME_TAGS = '<link rel="stylesheet" href="style.css">\n  <link rel="stylesheet" href="/voice/theme.css">'
FULLSCREEN_TAGS = (  # open as a full-screen app from the home screen (iOS reads these metas)
    '<meta name="apple-mobile-web-app-capable" content="yes">'
    '<meta name="mobile-web-app-capable" content="yes">'
    '<meta name="apple-mobile-web-app-status-bar-style" content="default">'
    f'<meta name="apple-mobile-web-app-title" content="{BRAND}">'
)
BACK_BUTTON = re.compile(r'(<button[^>]*id="btn-nav-back"[^>]*>뒤로</button>)')
BRAND_TAG = (  # app header logo; hidden by theme.css whenever the back button shows
    f'<span class="bm-brand"><img src="/voice/brand.svg" alt="" width="44" height="44">{BRAND}</span>'
)
# "practice only" lines inside the guardian settings form (the rest are hidden by theme.css)
PRACTICE_LINES = {
    "child.html": [
        ('<p class="field-help">연습용이라 지어낸 주소를 써도 돼요. 예를 들면 ', '<p class="field-help">예를 들면 '),
        ('<p class="field-help">연습용 카드라 진짜 돈은 나가지 않아요</p>', ""),
        ('<p class="field-help">연습용 배달앱이에요. 진짜 배달앱과 연결되지 않아요</p>', ""),
    ],
}
# Both roles sign in with one tap: no family-code keypad and no guardian id/password.
# 어르신 joins this phone's family (the one a guardian set up here, else the sample family);
# 보호자 uses the account of that family, else the sample guardian.
ONE_TAP_LOGIN = (
    "<script>document.addEventListener('click',function(e){"
    "var b=e.target.closest&&e.target.closest('#login-role-parent,#login-role-guardian');if(!b)return;"
    "e.preventDefault();e.stopImmediatePropagation();"
    "var A=window.NeulbapAPI,code=String(A.currentFamily());"
    "if(b.id==='login-role-parent'){A.loginParent(code,Date.now());location.href='parent.html';return}"
    "var acc=A.loadAccounts().filter(function(a){return String(a.familyCode)===code})[0]"
    "||A.findAccount(A.DEMO_ACCOUNTS.guardian.id);"
    "A.loginGuardian(acc,Date.now());location.href='child.html'},true)</script>\n"
)
NO_CACHE = {"Cache-Control": "no-cache"}  # phones must pick up restyled files after an update
# Phones (iOS home-screen apps especially) keep old scripts that were served without cache
# headers, so every page asks for its scripts and styles with a per-start version.
ASSET_VERSION = secrets.token_hex(4)
LOCAL_ASSET = re.compile(r'((?:src|href)=")(?!/|https?:)([^"?#]+\.(?:js|css))"')
# 어르신 order screen goes back to the start once the meal is done: right away when the
# order was confirmed elsewhere (voice, guardian), and by itself 30 s after it arrived.
PA3_OLD = """    if (!order) { openPa1(); return; }
    const v = A.pa3View(order, now(), confirming, A.loadSettings());"""
PA3_NEW = """    if (!order || order.acknowledgedAt) { openPa1(); return; }
    const v = A.pa3View(order, now(), confirming, A.loadSettings());
    const arrivedAt = order.createdAt + STATUS_TIMES_SEC[STATUS_TIMES_SEC.length - 1] * 1000;
    if (v.status === "arrived" && now() - arrivedAt >= AUTO_RESET_SEC * 1000) { actions.ack(); return; }"""
AUTO_RESET_SEC = 30
# "다른 메뉴" screen: "뒤로" sits right next to "다른 거 볼래요" (the top-bar copy is hidden
# there by theme.css), so both hands-on choices are in one row at thumb height.
PAM_ACTIONS = '<div class="actions" id="pam-actions" hidden>'
PAM_ROW = (
    '<div class="bm-pam-row"><button type="button" class="btn btn-big btn-secondary" id="bm-pam-back"'
    " onclick=\"document.getElementById('btn-nav-back').click()\">뒤로</button>" + PAM_ACTIONS
)
# 밥마중 app icon in place of the prototype's (home screen, tab icon, splash, start screen)
ICON_FILES = {
    "assets/app_icon.svg": "app-icon.svg",
    "assets/app_icon_192.png": "app-icon-192.png",
    "assets/app_icon_512.png": "app-icon-512.png",
}
# The one-card menu screens address 어르신 first (the voice guide reads these titles aloud too)
MENU_TITLES = [
    ('"오늘 이 메뉴 어떠세요?"', '"어르신, 오늘 이 메뉴 어떠세요?"'),
    ('"이 메뉴는 어떠세요?"', '"어르신, 이 메뉴는 어떠세요?"'),
]
# 어르신 first screen: three menus at a time as big photo rows (no scrolling). "다른 메뉴 보기"
# shows the next three and "앞 메뉴" the previous ones. Back from the confirm screen returns
# to the same three.
MENUS_PER_PAGE = 3
OPEN_B_OLD = "  function openB(i, back) {\n"
OPEN_B_NEW = """  let bmPage = 0;
  function openPage(page, back) {
    const settings = A.loadSettings(), orders = A.loadOrders(), t = now();
    const v = A.bCardView(settings, orders, t, 0);
    const all = settings ? A.candidateList(A.MENUS, settings, orders, t) : [];
    const pages = Math.max(1, Math.ceil(all.length / PER));
    bmPage = Math.max(0, Math.min(page, pages - 1));
    const list = all.slice(bmPage * PER, bmPage * PER + PER);
    $("pa1-title").textContent = bmPage === 0 ? "어르신, 오늘 뭐 드실래요?" : "어르신, 이런 메뉴도 있어요";
    const box = $("pa1-cards");
    box.className = "card-list bm-all";
    U.fillCards(box, list.map(function (m) {
      return { id: m.id, name: m.name, priceText: A.formatPrice(m.price), pic: A.menuPic(m.id) };
    }), function (id) { choose(id, "pa-1", bmPage); });
    setNote(bmPage === 0 ? v.healthNote : "", bmPage === 0 ? v.healthDisclaimer : "");
    setMoreNote("");
    $("more-menus").textContent = "다른 메뉴 보기";
    $("more-menus").hidden = bmPage >= pages - 1;
    $("bm-pa1-back").hidden = bmPage === 0;
    $("pa1-position").textContent = all.length > PER ? "메뉴 " + all.length + "개 중 " + (bmPage + 1) + "쪽" : "";
    $("pa1-position").hidden = all.length <= PER;
    $("pa1-last").hidden = true;
    $("pa1-empty").textContent = v.empty || "";
    $("pa1-empty").hidden = !v.empty;
    show("pa-1", back);
  }
  window.bmMenuPage = function (step) { openPage(bmPage + step); };
  function openB(i, back) {
    if (i === 0) { openPage(back ? bmPage : 0, back); return; }
    if (current === "pa-1") { openPage(bmPage + 1); return; }
"""
MENU_PHOTO_DIR = STATIC_DIR / "menus"  # real food photos (credits.json); others keep their drawing
MENU_PIC_FN = 'function menuPic(id) { return "assets/menus/" + id + ".svg"; }'


def _menu_pic_fn() -> str:
    ids = sorted(p.stem for p in MENU_PHOTO_DIR.glob("*.jpg"))
    return (
        f"var BM_PHOTOS = {json.dumps(ids)};\n"
        "function menuPic(id) { return BM_PHOTOS.indexOf(id) >= 0 ? "
        '"/voice/menus/" + id + ".jpg" : "assets/menus/" + id + ".svg"; }'
    )


def restyle_text(name: str, text: str) -> str:
    """Brand, colours and photos for the prototype's scripts and manifest."""
    text = OLD_NAME.sub(BRAND, text)
    if name == "mock_api.js":
        text = text.replace(MENU_PIC_FN, _menu_pic_fn())
        for old, new in MENU_TITLES:
            text = text.replace(old, new)
    elif name == "parent.js":
        text = text.replace(OPEN_B_OLD, OPEN_B_NEW.replace("PER", str(MENUS_PER_PAGE)), 1)
        if PA3_OLD not in text:
            logger.warning("parent.js changed: the order screen will not reset by itself")
        text = text.replace(PA3_OLD, PA3_NEW.replace("AUTO_RESET_SEC", str(AUTO_RESET_SEC)))
    elif name == "manifest.webmanifest":
        text = text.replace(" (연습용 화면)", "").replace(OLD_THEME_COLOR, THEME_COLOR)
    return text


def restyle_page(name: str, html: str) -> str:
    """Brand, app header, theme and full-screen metas for one of the prototype's pages."""
    html = OLD_NAME.sub(BRAND, html)
    html = html.replace(OLD_THEME_COLOR, THEME_COLOR)
    # remember ?code=... before the page's own login redirect drops the query
    html = html.replace("<head>", "<head>" + CODE_SNIPPET + FULLSCREEN_TAGS, 1)
    html = html.replace('<link rel="stylesheet" href="style.css">', THEME_TAGS, 1)
    html = LOCAL_ASSET.sub(lambda m: f'{m.group(1)}{m.group(2)}?v={ASSET_VERSION}"', html)
    html = html.replace('href="/voice/theme.css"', f'href="/voice/theme.css?v={ASSET_VERSION}"')
    html = BACK_BUTTON.sub(lambda m: m.group(1) + BRAND_TAG, html, count=1)
    if name == "parent.html":
        html = html.replace(
            '<button type="button" class="btn btn-big btn-secondary btn-more" id="more-menus"',
            '<button type="button" class="btn btn-big btn-secondary" id="bm-pa1-back" hidden'
            ' onclick="window.bmMenuPage(-1)">앞 메뉴</button>'
            '<button type="button" class="btn btn-big btn-secondary btn-more" id="more-menus"',
            1,
        )
    if name == "parent.html" and PAM_ACTIONS in html:
        start = html.index(PAM_ACTIONS)
        end = html.index("</div>", start) + len("</div>")
        html = html[:start] + PAM_ROW + html[start + len(PAM_ACTIONS) : end] + "</div>" + html[end:]
    for old, new in PRACTICE_LINES.get(name, []):
        html = html.replace(old, new)
    if name == "index.html":
        html = DESIGN_CHOOSER.sub("", html)
    if name == "login.html":
        html = re.sub(r'<span class="role-sub">[^<]*</span>', "", html)  # role buttons: name only
        html = html.replace(f"{BRAND}에 오신 걸 환영해요", "따뜻한 밥 한 끼,<br>가족이 챙겨 드려요")
        # 어르신 first: they are the main users
        g = re.search(r'<button[^>]*id="login-role-guardian".*?</button>', html, re.S)
        pa = re.search(r'<button[^>]*id="login-role-parent".*?</button>', html, re.S)
        if g and pa and g.start() < pa.start():
            html = html[: g.start()] + pa.group(0) + html[g.end() : pa.start()] + g.group(0) + html[pa.end() :]
        # friendlier role pictures: an older person and a family member with a heart
        html = html.replace('src="assets/tab_my.svg" width="56" height="56" alt="사람 그림"',
                            'src="/voice/role-guardian.svg" width="72" height="72" alt="보호자 그림"')
        html = html.replace('src="assets/family_code.svg" width="56" height="56" alt="열쇠 그림"',
                            'src="/voice/role-parent.svg" width="72" height="72" alt="어르신 그림"')
        html = html.replace("</body>", ONE_TAP_LOGIN + "</body>", 1)
    if name in VOICE_PAGES:
        widget = WIDGET_TAGS.replace("widget.js", f"widget.js?v={ASSET_VERSION}")
        html = html.replace("</body>", widget + "</body>", 1)
    return html


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
                app.contract["package"], app_name=BRAND, tool_names=tools.names, allow_gui=False
            ),
            tools=tools,
            app_name=BRAND,
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
                app_name=BRAND,
                tools=tools.schemas(),
                gui=False,
                extra_instructions=role_instructions(role),
                reasoning_effort=cfg.reasoning_effort,
                **PROMPT_EXAMPLES,
            )

    app = FastAPI(title=f"{BRAND} 음성 비서")

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
                        f"{BRAND} 비서예요. 드시고 싶은 걸 말씀해 주세요."
                        if role == "parent"
                        else f"{BRAND} 보호자 비서예요. 무엇을 도와드릴까요?"
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

    # The app always starts on the role screen (login.html). The prototype's index.html is a
    # demo hub ("누가 쓰실 화면인가요?") shown when both roles are signed in on one phone, which
    # one-tap sign-in makes common, so it is never shown.
    @app.get("/")
    @app.get("/index.html")
    def home(request: Request):
        query = request.url.query  # keep ?code=... for the page to remember
        return RedirectResponse("/login.html" + (f"?{query}" if query else ""))

    @app.get("/{path:path}")
    def neulbab_file(path: str):
        if path in ICON_FILES:
            return FileResponse(STATIC_DIR / ICON_FILES[path], headers=NO_CACHE)
        target = (app_root / path).resolve()
        if not target.is_relative_to(app_root) or not target.is_file():
            return PlainTextResponse("Not found", status_code=404)
        if target.suffix in RENAMED_TYPES and not (
            target.suffix == ".html" and target.parent == app_root
        ):
            text = restyle_text(target.name, target.read_text(encoding="utf-8"))
            media = RENAMED_TYPES[target.suffix] + "; charset=utf-8"
            return Response(text, media_type=media, headers=NO_CACHE)
        if target.suffix == ".html" and target.parent == app_root:
            html = restyle_page(target.name, target.read_text(encoding="utf-8"))
            return HTMLResponse(html, headers=NO_CACHE)
        return FileResponse(target)

    return app
