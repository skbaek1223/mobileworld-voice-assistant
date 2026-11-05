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
    assert page.url.path == "/voice/"  # no 늘밥 app folder given: standalone voice page
    assert client.get("/voice/manifest.webmanifest").json()["display"] == "standalone"
    assert client.get("/voice/apple-touch-icon.png").status_code == 200
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


# ------------------------------------------------------------------ original 늘밥 screens + widget
@pytest.fixture
def neulbab_client(app_dir):
    pages = {
        "index.html": "<html><head><title>늘밥</title></head><body>허브</body></html>",
        "login.html": "<html><head></head><body>보호자로 시작 어르신으로 시작</body></html>",
        "parent.html": "<html><head></head><body><main>어르신 화면</main></body></html>",
        "child.html": "<html><head></head><body><nav id='tabbar'></nav></body></html>",
        "style.css": "body{}",
    }
    for name, text in pages.items():
        (app_dir / name).write_text(text, encoding="utf-8")
    app = MockNeulbabApp(load_data(app_dir), tz=ZoneInfo("Asia/Seoul"))
    family = Family(app)
    web = create_app(
        family,
        WebConfig(access_code="123456", filler_after_sec=None),
        FakeVoice(),
        agent_factory=lambda tools, role: OrderAgent(tools, role),
        neulbab_app_dir=app_dir,
    )
    with TestClient(web) as c:
        c.family = family
        yield c


def test_original_screens_with_widget_only_on_role_pages(neulbab_client):
    home = neulbab_client.get("/?code=123456")
    assert (
        home.url.path == "/index.html" and home.url.query == b"code=123456" and "허브" in home.text
    )
    for page in ("parent.html", "child.html"):
        html = neulbab_client.get(f"/{page}").text
        assert html.count("/voice/widget.js") == 1 and "nb_code" in html
    for page in ("index.html", "login.html"):
        html = neulbab_client.get(f"/{page}").text
        assert "widget.js" not in html and "nb_code" in html  # code saved before redirect
    assert neulbab_client.get("/style.css").text == "body{}"
    assert neulbab_client.get("/data/menus.json").json()[0]["id"] == "f1"
    assert neulbab_client.get("/voice/widget.js").status_code == 200
    assert neulbab_client.get("/../../etc/passwd").status_code == 404
    assert neulbab_client.get("/%2e%2e/conftest.py").status_code == 404


def test_screen_and_voice_stay_in_sync(neulbab_client):
    fam = neulbab_client.family
    with (
        neulbab_client.websocket_connect("/ws?code=123456&role=parent") as parent,
        neulbab_client.websocket_connect("/ws?code=123456&role=guardian") as guardian,
    ):
        first, _ = recv_until(parent, "sync")
        assert first["orders"] == [] and first["settings"]["allergies"] == ["새우"]
        recv_until(guardian, "sync")

        # the guardian taps "대신 주문" on the 늘밥 screen -> their localStorage -> sync_up
        tapped = {
            "id": "o1", "menuId": "f4", "menuName": "전복죽", "category": "죽", "price": 10000,
            "orderedBy": "guardian", "platform": "pa", "platformOrderId": "PA-1",
            "createdAt": int(time.time() * 1000), "cancelled": False, "cancelledAt": None,
            "acknowledgedAt": None,
        }  # fmt: skip
        guardian.send_json({"type": "sync_up", "settings": first["settings"], "orders": [tapped]})
        got, _ = recv_until(parent, "sync")
        assert got["orders"][0]["menuName"] == "전복죽" and fam.app.orders[0]["id"] == "o1"

        # ...and the parent's voice now knows about it
        parent.send_json({"type": "text", "text": "누가 밥 시켰어?"})
        done, _ = recv_until(parent, "done")
        assert done["reply"] == "자녀분이 주문했어요"

        # a voice change is pushed to the other phone's screen
        parent.send_json({"type": "text", "text": "주문 취소해 줘"})
        recv_until(parent, "listen")
        parent.send_json({"type": "text", "text": "네"})
        recv_until(parent, "done")
        pushed = None
        for _ in range(10):
            m, _ = recv_until(guardian, "sync")
            if m["orders"] and m["orders"][0]["cancelled"]:
                pushed = m
                break
        assert pushed is not None


def test_bad_sync_up_is_ignored(neulbab_client):
    with neulbab_client.websocket_connect("/ws?code=123456&role=parent") as ws:
        recv_until(ws, "sync")
        ws.send_json({"type": "sync_up", "settings": "nope", "orders": [{"x": 1}]})
        ws.send_json({"type": "state"})
        recv_until(ws, "state")
    assert neulbab_client.family.app.orders == [] and neulbab_client.family.app.settings
