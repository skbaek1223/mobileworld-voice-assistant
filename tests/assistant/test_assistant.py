"""Offline tests for the in-app voice assistant (no phone, no LLM, no audio)."""

import json

import numpy as np
import pytest
from PIL import Image

from mobile_world.assistant.app_tools import (
    ToolRegistry,
    broadcast_tool,
    deeplink_tool,
    load_tools_file,
)
from mobile_world.assistant.device import (
    DeviceBackend,
    parse_broadcast_result,
    parse_focused_package,
)
from mobile_world.assistant.scope import AppScopeGuard
from mobile_world.assistant.session import VoiceAssistantSession
from mobile_world.assistant.speech import EnergyVAD, SpeechToText, TextToSpeech, to_wav_bytes
from mobile_world.runtime.utils.models import JSONAction

PKG = "com.example.myapp"


# --------------------------------------------------------------------------- fakes
class FakeDevice(DeviceBackend):
    def __init__(self, packages=None):
        self.packages = list(packages or [])  # successive current_package() results
        self.executed: list[JSONAction] = []
        self.shell_calls: list[list[str]] = []
        self.launched: list[str] = []
        self.backs = 0
        self.shell_output = "Starting: Intent"

    def screenshot(self):
        return Image.new("RGB", (108, 240))

    def execute(self, action):
        self.executed.append(action)
        return self.screenshot()

    def current_package(self):
        return self.packages.pop(0) if self.packages else PKG

    def launch(self, package, activity=None):
        self.launched.append(package)
        return True

    def back(self):
        self.backs += 1

    def shell(self, args, timeout=20.0):
        self.shell_calls.append(args)
        return self.shell_output


class ScriptedAgent:
    """Returns pre-baked actions and records every observation it was shown."""

    def __init__(self, actions):
        self.actions = [JSONAction(**a) for a in actions]
        self.observations = []
        self.instructions = []

    def initialize(self, instruction):
        self.instructions.append(instruction)

    def predict(self, obs):
        self.observations.append(obs)
        return "Thought: scripted", self.actions.pop(0)

    def done(self):
        pass


class ScriptedSTT(SpeechToText):
    def __init__(self, utterances):
        self.utterances = list(utterances)

    def listen(self, prompt=None):
        return self.utterances.pop(0) if self.utterances else ""


class RecordingTTS(TextToSpeech):
    def __init__(self):
        self.said = []

    def speak(self, text):
        self.said.append(text)


def make_session(actions, utterances=(), device=None, tools=None, **kw):
    device = device or FakeDevice()
    tools = tools or ToolRegistry()
    agent = ScriptedAgent(actions)
    tts = RecordingTTS()
    guard = AppScopeGuard(PKG, app_name="MyApp", tool_names=tools.names)
    session = VoiceAssistantSession(
        agent,
        device,
        ScriptedSTT(utterances),
        tts,
        guard,
        tools=tools,
        app_name="MyApp",
        tool_wait_time=0,
        **kw,
    )
    return session, agent, device, tts


# --------------------------------------------------------------------------- parsing
def test_parse_focused_package():
    out = "  mCurrentFocus=Window{5f3a u0 com.example.myapp/com.example.myapp.MainActivity}\n"
    assert parse_focused_package(out) == PKG
    out = "  mCurrentFocus=null\n  mFocusedApp=ActivityRecord{1a2b u0 com.android.chrome/.Main t3}"
    assert parse_focused_package(out) == "com.android.chrome"
    assert parse_focused_package("mCurrentFocus=Window{9 u0 NotificationShade}") == (
        "com.android.systemui"
    )
    assert parse_focused_package("nothing here") is None


def test_parse_broadcast_result():
    out = 'Broadcasting: Intent { ... }\nBroadcast completed: result=-1, data="[{\\"id\\":1}]"'
    assert parse_broadcast_result(out) == '[{\\"id\\":1}]'
    assert parse_broadcast_result("Broadcast completed: result=0") == "result=0"


# --------------------------------------------------------------------------- tools
def test_registry_decorator_and_validation():
    reg = ToolRegistry()

    @reg.tool(
        "add",
        "add numbers",
        {"type": "object", "properties": {"a": {"type": "integer"}}, "required": ["a"]},
    )
    def add(a, b=1):
        return {"sum": a + b}

    assert reg.schemas()[0]["inputSchema"]["required"] == ["a"]
    assert json.loads(reg.call("add", {"a": 2}, None)) == {"sum": 3}
    assert reg.call("add", {}, None).startswith("Error:")
    assert reg.call("nope", {}, None).startswith("Error: unknown tool")


def test_deeplink_tool_encodes_args():
    dev = FakeDevice()
    tool = deeplink_tool("search", "search", "myapp://search?q={query}", PKG)
    ToolRegistry([tool]).call("search", {"query": "빨간 신발 & 양말"}, dev)
    args = dev.shell_calls[0]
    uri = args[args.index("-d") + 1]
    assert uri.startswith("myapp://search?q=") and " " not in uri and "&" not in uri[16:]
    assert args[-2:] == ["-p", PKG]


def test_broadcast_tool_returns_result_data():
    dev = FakeDevice()
    dev.shell_output = 'Broadcast completed: result=-1, data="2 orders"'
    tool = broadcast_tool("orders", "orders", "com.example.GET", PKG)
    assert ToolRegistry([tool]).call("orders", {"limit": 2}, dev) == "2 orders"
    assert dev.shell_calls[0][-3:] == ["--es", "limit", "2"]


def test_load_example_tools_file():
    reg = load_tools_file("docs/assistant_tools.example.json", PKG)
    assert {"open_order_detail", "get_recent_orders"} <= reg.names


# --------------------------------------------------------------------------- scope guard
def test_guard_blocks_leaving_app():
    guard = AppScopeGuard(PKG, app_name="MyApp", tool_names={"t"})
    assert guard.check_action(JSONAction(action_type="navigate_home"))
    assert guard.check_action(JSONAction(action_type="open_app", app_name="Chrome"))
    assert guard.check_action(JSONAction(action_type="open_app", app_name="myapp")) is None
    assert guard.check_action(JSONAction(action_type="mcp", action_name="x"))
    assert guard.check_action(JSONAction(action_type="mcp", action_name="t")) is None
    assert guard.check_action(JSONAction(action_type="click", x=1, y=1)) is None


def test_guard_recovers_with_back_then_relaunch():
    guard = AppScopeGuard(PKG)
    dev = FakeDevice(["com.android.chrome", PKG])
    assert "Pressed back" in guard.ensure_in_scope(dev)
    assert dev.backs == 1 and not dev.launched

    dev = FakeDevice(["com.android.chrome", "com.android.chrome"])
    assert "relaunched" in guard.ensure_in_scope(dev)
    assert dev.launched == [PKG]


# --------------------------------------------------------------------------- session
def test_answer_is_spoken():
    session, _, _, tts = make_session([{"action_type": "answer", "text": "주문은 2건이에요."}])
    result = session.handle_request("주문 몇 개야")
    assert result.status == "answered"
    assert tts.said == ["주문은 2건이에요."]


def test_tool_then_gui_then_answer_hybrid():
    reg = ToolRegistry()
    reg.tool("get_orders", "orders")(lambda: [{"id": "A-1"}])
    session, agent, device, tts = make_session(
        [
            {"action_type": "mcp", "action_name": "get_orders", "action_json": {}},
            {"action_type": "click", "x": 10, "y": 20},
            {"action_type": "answer", "text": "A-1 주문을 열었어요."},
        ],
        tools=reg,
    )
    result = session.handle_request("최근 주문 열어줘")
    assert result.status == "answered" and len(result.steps) == 3
    assert "A-1" in agent.observations[1]["tool_call"]
    assert agent.observations[1]["screenshot"] is not None
    assert [a.action_type for a in device.executed] == ["click"]


def test_blocked_action_is_fed_back_and_not_executed():
    session, agent, device, _ = make_session(
        [{"action_type": "navigate_home"}, {"action_type": "answer", "text": "ok"}]
    )
    session.handle_request("홈으로 가")
    assert device.executed == []
    assert agent.observations[1]["tool_call"].startswith("Blocked:")


def test_leaving_app_triggers_recovery_note():
    device = FakeDevice([PKG, "com.android.chrome", PKG])  # start, after click, after back
    session, agent, device, _ = make_session(
        [{"action_type": "click", "x": 1, "y": 1}, {"action_type": "answer", "text": "ok"}],
        device=device,
    )
    session.handle_request("링크 눌러줘")
    assert device.backs == 1
    assert agent.observations[1]["tool_call"].startswith("Note:")


def test_ask_user_round_trip_and_cancel():
    session, agent, _, tts = make_session(
        [
            {"action_type": "ask_user", "text": "몇 개 담을까요?"},
            {"action_type": "answer", "text": "3개 담았어요."},
        ],
        utterances=["세 개"],
    )
    assert session.handle_request("장바구니에 담아줘").status == "answered"
    assert tts.said == ["몇 개 담을까요?", "3개 담았어요."]
    assert agent.observations[1]["ask_user_response"] == "세 개"

    session, _, _, tts = make_session(
        [{"action_type": "ask_user", "text": "결제할까요?"}], utterances=["취소"]
    )
    assert session.handle_request("결제해줘").status == "cancelled"
    assert tts.said[-1] == "취소했어요."


def test_status_complete_and_max_steps():
    session, _, _, tts = make_session([{"action_type": "answer", "text": "task finished"}])
    session.handle_request("x")
    assert tts.said == ["완료했어요."]

    session, _, _, _ = make_session([{"action_type": "wait"}] * 3, max_steps=2)
    assert session.handle_request("x").status == "max_steps"


def test_history_is_included_in_followup_instruction():
    session, agent, _, _ = make_session(
        [
            {"action_type": "answer", "text": "A-1 주문이에요."},
            {"action_type": "answer", "text": "취소했어요."},
        ]
    )
    session.handle_request("최근 주문 뭐야")
    session.handle_request("그거 취소해줘")
    assert "A-1 주문이에요." in agent.instructions[1]


def test_run_forever_exits_on_phrase():
    session, _, _, tts = make_session(
        [{"action_type": "answer", "text": "네"}], utterances=["", "안녕", "종료"]
    )
    session.run_forever()
    assert tts.said == ["MyApp 비서예요. 무엇을 도와드릴까요?", "네", "비서를 종료할게요."]


# --------------------------------------------------------------------------- speech utils
def test_energy_vad_endpointing():
    vad = EnergyVAD(
        frame_ms=10, energy_threshold=0.1, silence_seconds=0.03, start_timeout_seconds=1.0
    )
    quiet, loud = np.zeros(160, np.float32), np.full(160, 0.5, np.float32)
    assert not vad.push(quiet)
    assert not vad.push(loud)
    assert not vad.push(quiet) and not vad.push(quiet)
    assert vad.push(quiet)
    assert vad.audio().size == 160 * 4

    vad = EnergyVAD(frame_ms=10, start_timeout_seconds=0.02)
    assert not vad.push(quiet) and vad.push(quiet)
    assert vad.audio().size == 0


def test_to_wav_bytes_header():
    data = to_wav_bytes(np.zeros(1600, np.float32))
    assert data[:4] == b"RIFF" and len(data) == 44 + 3200


# --------------------------------------------------------------------------- agent prompt
def test_agent_prompt_and_messages():
    pytest.importorskip("openai")
    from mobile_world.assistant.agent import InAppAssistantAgent

    reg = ToolRegistry()
    reg.tool("get_orders", "Return recent orders")(lambda: [])
    agent = InAppAssistantAgent(
        "some-model", "http://localhost:1", app_name="MyApp", tools=reg.schemas()
    )
    prompt = agent._render_system_prompt(1000)
    assert "MyApp" in prompt and "Korean" in prompt and "get_orders" in prompt
    assert "Tools first" in prompt

    msg = agent._get_user_message(Image.new("RGB", (4, 4)), "Blocked: no", None)
    assert [c["type"] for c in msg["content"]] == ["text", "image_url"]
    assert msg["content"][0]["text"] == "Blocked: no"
    msg = agent._get_user_message(Image.new("RGB", (4, 4)), None, "네")
    assert msg["content"][0]["text"] == "User replied: 네"
