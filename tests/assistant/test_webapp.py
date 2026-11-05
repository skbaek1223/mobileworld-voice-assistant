"""Web app server: protocol, access code, voice round trips and family sync (no network)."""

import base64
import time
from zoneinfo import ZoneInfo

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")
from fastapi.testclient import TestClient

from mobile_world.assistant.neulbab import MockNeulbabApp, load_data
from mobile_world.assistant.webapp.server import Family, WebConfig, create_app
from mobile_world.assistant.webapp.voice import extension_for
from mobile_world.runtime.utils.models import JSONAction


class FakeVoice:
    """STT: the 'audio' is utf-8 text. TTS: mp3 bytes are the text, tagged."""

    def transcribe(self, audio: bytes, mime: str) -> str:
        return audio.decode()

    def synthesize(self, text: str) -> bytes:
        return b"MP3:" + text.encode()


class OrderAgent:
    """Tries to order the menu named in the request; otherwise answers."""

    def __init__(self, tools, role):
        self.tools = tools

    def initialize(self, instruction):
        self.instruction = instruction
        self.step = 0

    def predict(self, obs):
        self.step += 1
        if self.step == 1 and "미역국" in self.instruction:
            return "x", JSONAction(
                action_type="mcp", action_name="prepare_order", action_json={"menu_id": "f1"}
            )
        return "x", JSONAction(action_type="answer", text="다른 건 아직 못 해요.")

    def done(self):
        pass


@pytest.fixture
def client(app_dir):
    app = MockNeulbabApp(load_data(app_dir), tz=ZoneInfo("Asia/Seoul"))
    family = Family(app)
    cfg = WebConfig(access_code="123456", filler_after_sec=None)
    web = create_app(
        family, cfg, FakeVoice(), agent_factory=lambda tools, role: OrderAgent(tools, role)
    )
    with TestClient(web) as c:
        c.family = family
        yield c


def recv_until(ws, kind, limit=40):
    seen = []
    for _ in range(limit):
        m = ws.receive_json()
        seen.append(m)
        if m["type"] == kind:
            return m, seen
    raise AssertionError(f"no {kind} in {seen}")


def test_static_pages_and_code_check(client):
    page = client.get("/")
    assert (
        page.status_code == 200 and "어르신으로 시작" in page.text and "보호자로 시작" in page.text
    )
    assert client.get("/manifest.webmanifest").json()["display"] == "standalone"
    assert client.get("/apple-touch-icon.png").status_code == 200
    assert client.get("/api/check?code=000000").json() == {"ok": False, "code_required": True}
    assert client.get("/api/check?code=123456").json()["ok"]


def test_wrong_code_or_role_is_refused(client):
    from starlette.websockets import WebSocketDisconnect

    for query in ("code=000000&role=parent", "code=123456&role=admin"):
        with pytest.raises(WebSocketDisconnect) as e:
            with client.websocket_connect(f"/ws?{query}") as ws:
                ws.receive_json()
        assert e.value.code == 4401


def test_greeting_and_state(client):
    with client.websocket_connect("/ws?code=123456&role=parent") as ws:
        ws.send_json({"type": "start"})
        state, _ = recv_until(ws, "state")
        assert state["order"] is None and state["menus"] == [
            "고등어구이 정식",
            "잔치국수",
            "미역국 정식",
        ]
        say, _ = recv_until(ws, "say")
        assert say["text"].startswith("늘밥 비서예요")
        audio, _ = recv_until(ws, "audio")
        assert audio["id"] == say["id"] and base64.b64decode(audio["data"]).startswith(
            "MP3:늘밥".encode()
        )


def test_spoken_request_routed_without_llm(client):
    with client.websocket_connect("/ws?code=123456&role=parent") as ws:
        ws.send_json(
            {
                "type": "audio",
                "mime": "audio/mp4",
                "data": base64.b64encode("점심 뭐 먹을까?".encode()).decode(),
            }
        )
        heard, _ = recv_until(ws, "heard")
        assert heard["text"] == "점심 뭐 먹을까?"
        done, seen = recv_until(ws, "done")
        said = " ".join(m["text"] for m in seen if m["type"] == "say")
        assert (
            "고등어구이 정식, 잔치국수, 미역국 정식이 있어요" in said
            and done["status"] == "answered"
        )


def test_voice_order_with_spoken_yes_and_family_sync(client):
    with (
        client.websocket_connect("/ws?code=123456&role=parent") as parent,
        client.websocket_connect("/ws?code=123456&role=guardian") as guardian,
    ):
        parent.send_json({"type": "text", "text": "미역국 시켜 줘"})
        _, seen = recv_until(parent, "listen")
        question = [m["text"] for m in seen if m["type"] == "say"][-1]
        assert question == "미역국 정식, 8,500원이에요. 자녀분 카드로 결제돼요. 주문할까요?"
        parent.send_json(
            {
                "type": "audio",
                "mime": "audio/webm",
                "data": base64.b64encode("네".encode()).decode(),
            }
        )
        done, seen = recv_until(parent, "done")
        assert done["reply"].startswith("미역국 정식을 주문했어요")
        state, _ = recv_until(parent, "state")
        assert state["order"]["menu_name"] == "미역국 정식"

        guardian.send_json({"type": "state"})
        gstate, _ = recv_until(guardian, "state")
        assert gstate["order"]["ordered_by_text"] == "어머님이 미역국 정식을 주문하셨어요"
    assert len(client.family.app.orders) == 1


def test_yes_no_buttons_and_silence(client):
    with client.websocket_connect("/ws?code=123456&role=parent") as ws:
        ws.send_json({"type": "text", "text": "미역국 시켜 줘"})
        recv_until(ws, "listen")
        ws.send_json({"type": "text", "text": "아니요"})  # the [아니요] button
        done, _ = recv_until(ws, "done")
        assert done["status"] == "cancelled"

        ws.send_json({"type": "text", "text": "미역국 시켜 줘"})
        recv_until(ws, "listen")
        ws.send_json({"type": "silence"})  # nothing heard -> asked again
        recv_until(ws, "listen")
        ws.send_json({"type": "silence"})
        done, _ = recv_until(ws, "done")
        assert done["status"] == "cancelled"
    assert client.family.app.orders == []


def test_busy_while_thinking(client, monkeypatch):
    with client.websocket_connect("/ws?code=123456&role=parent") as ws:
        ws.send_json({"type": "text", "text": "미역국 시켜 줘"})
        recv_until(ws, "listen")
        # a new request is impossible while the question waits; it is taken as the answer
        ws.send_json({"type": "text", "text": "네"})
        recv_until(ws, "done")
        time.sleep(0.05)
    assert len(client.family.app.orders) == 1


@pytest.mark.parametrize(
    ("mime", "ext"),
    [("audio/webm;codecs=opus", "webm"), ("audio/mp4", "mp4"), ("audio/mpeg", "mp3"), ("", "webm")],
)
def test_upload_extension(mime, ext):
    assert extension_for(mime) == ext
