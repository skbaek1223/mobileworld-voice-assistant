"""Latency features: rule fast paths, state snapshot, session-run confirmations, fillers,
sentence streaming — and that they never weaken safety."""

import json
import threading
import time
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from PIL import Image

from mobile_world.assistant.confirm import classify_yes_no, is_bare_no
from mobile_world.assistant.device import ToolsOnlyDevice
from mobile_world.assistant.neulbab import MockNeulbabApp, build_toolset, load_data
from mobile_world.assistant.neulbab.fast_paths import build_router
from mobile_world.assistant.neulbab.toolset import (
    build_context_provider,
    commit_tools,
    role_instructions,
)
from mobile_world.assistant.scope import AppScopeGuard
from mobile_world.assistant.session import VoiceAssistantSession, _unspoken_rest
from mobile_world.assistant.speech import SpeechToText, TextToSpeech
from mobile_world.assistant.streaming import SpeechStreamExtractor
from mobile_world.runtime.utils.models import JSONAction

KST = ZoneInfo("Asia/Seoul")
T0 = 1_762_311_600_000


# ------------------------------------------------------------------ yes / no
@pytest.mark.parametrize(
    ("text", "verdict"),
    [
        ("네", "yes"), ("응 주문해", "yes"), ("그래도 바꿔 줘", "yes"), ("예 해주세요", "yes"),
        ("아니요, 안 할래요", "no"), ("아니, 다음에 할게", "no"), ("주문하지 마", "no"),
        ("잠깐만", "no"), ("아니 전복죽으로 해 줘", "no"), ("", "unclear"), ("음...", "unclear"),
        ("글쎄", "unclear"),
    ],
)  # fmt: skip
def test_classify_yes_no(text, verdict):
    assert classify_yes_no(text) == verdict


def test_bare_no_vs_no_with_a_new_wish():
    assert (
        is_bare_no("아니요, 안 할래요") and is_bare_no("아니, 다음에 할게") and is_bare_no("아니")
    )
    assert not is_bare_no("아니 전복죽으로 해 줘")


# ------------------------------------------------------------------ streaming extractor
def feed_all(chunks):
    ex, out = SpeechStreamExtractor(), []
    for c in chunks:
        out += ex.feed(c)
    return ex, out


def test_stream_extractor_emits_sentences_as_they_finish():
    text = 'Thought: 추천.\nAction: {"action_type": "answer", "text": "미역국이 있어요. 전복죽도 있어요! 뭐 드실래요?"}'
    ex, out = feed_all([text[i : i + 7] for i in range(0, len(text), 7)])
    assert out == ["미역국이 있어요.", "전복죽도 있어요!", "뭐 드실래요?"]
    assert ex.done and ex.spoken.strip() == "미역국이 있어요. 전복죽도 있어요! 뭐 드실래요?"


def test_stream_extractor_first_sentence_before_the_end():
    ex = SpeechStreamExtractor()
    assert ex.feed('Action: {"action_type": "ask_user", "text": "전복죽이에요. 주문') == [
        "전복죽이에요."
    ]
    assert ex.feed('할까요?"}') == ["주문할까요?"]


def test_stream_extractor_ignores_tools_and_unknown_order():
    _, out = feed_all(['Action: {"action_type": "mcp", "action_name": "x", "text": "a. b."}'])
    assert out == []
    _, out = feed_all(['Action: {"text": "먼저 온 글. 이건 안 읽어요.", "action_type": "answer"}'])
    assert out == []
    _, out = feed_all(
        ['Action: {"action_type": "answer", "text": "따옴표 \\"안\\" 깨져요. 끝\\u0021"}']
    )
    assert out == ['따옴표 "안" 깨져요.', "끝!"]


def test_unspoken_rest():
    assert _unspoken_rest("미역국이 있어요. 뭐 드실래요?", "미역국이 있어요. ") == "뭐 드실래요?"
    assert _unspoken_rest("미역국이 있어요.", "미역국이 있어요. ") == ""
    assert _unspoken_rest("전혀 다른 말", "미역국") == "전혀 다른 말"


# ------------------------------------------------------------------ fixtures
class Clock:
    def __init__(self):
        self.t = T0

    def __call__(self):
        return self.t


class Script(SpeechToText):
    def __init__(self, lines):
        self.lines = list(lines)

    def listen(self, prompt=None):
        return self.lines.pop(0) if self.lines else ""


class Spoken(TextToSpeech):
    def __init__(self):
        self.said, self.at = [], []

    def speak(self, text):
        self.said.append(text)
        self.at.append(time.time())


class PlanAgent:
    """Fixed actions; optional delay; optional streamed sentences (via speech_sink)."""

    def __init__(self, actions, delay=0.0, stream=None):
        self.actions = [JSONAction(**a) for a in actions]
        self.delay, self.stream = delay, stream or []
        self.speech_sink = None
        self.instructions, self.calls = [], 0

    def initialize(self, instruction):
        self.instructions.append(instruction)

    def predict(self, obs):
        self.calls += 1
        time.sleep(self.delay)
        if self.speech_sink:
            for sentence in self.stream:
                self.speech_sink(sentence)
        return "Thought: plan", self.actions.pop(0)

    def done(self):
        pass


@pytest.fixture
def app(app_dir):
    return MockNeulbabApp(load_data(app_dir), clock=Clock(), tz=KST)


def session_for(app, role, agent, replies=(), **kw):
    tools = build_toolset(app, role)
    tts = Spoken()
    s = VoiceAssistantSession(
        agent,
        ToolsOnlyDevice("com.neulbab.app"),
        Script(replies),
        tts,
        AppScopeGuard("com.neulbab.app", tool_names=tools.names, allow_gui=False),
        tools=tools,
        tool_wait_time=0,
        **kw,
    )
    return s, tts


def mcp(name, **args):
    return {"action_type": "mcp", "action_name": name, "action_json": args}


# ------------------------------------------------------------------ session-run confirmation
def test_session_confirms_and_commits_without_the_agent(app):
    agent = PlanAgent([mcp("prepare_order", menu_id="f2")])
    s, tts = session_for(app, "parent", agent, ["네"], commit_tools=commit_tools(app, "parent"))
    r = s.handle_request("고등어구이 시켜 줘")
    assert r.status == "answered" and r.llm_calls == 1
    assert tts.said[0].startswith("고등어구이 정식, 9,500원이에요") and tts.said[1].startswith(
        "고등어구이 정식을 주문했어요"
    )
    assert app.orders[0]["menuId"] == "f2"
    by = [(st.action.get("by"), st.action["action_type"]) for st in r.steps]
    assert by == [(None, "mcp"), ("session", "ask_user"), ("session", "mcp")]


@pytest.mark.parametrize(
    ("reply", "status", "orders"),
    [
        ("아니요", "cancelled", 0),
        ("", "cancelled", 0),
        ("음...", "cancelled", 0),
        ("그래", "answered", 1),
    ],
)
def test_session_confirm_outcomes(app, reply, status, orders):
    agent = PlanAgent([mcp("prepare_order", menu_id="f1")])
    s, _ = session_for(app, "parent", agent, [reply, ""], commit_tools=commit_tools(app, "parent"))
    assert s.handle_request("미역국 시켜 줘").status == status
    assert len(app.orders) == orders


def test_no_with_a_new_wish_goes_back_to_the_agent(app):
    agent = PlanAgent(
        [mcp("prepare_order", menu_id="f1"), {"action_type": "answer", "text": "알겠어요"}]
    )
    s, _ = session_for(
        app, "parent", agent, ["아니 전복죽으로 해 줘"], commit_tools=commit_tools(app, "parent")
    )
    s.handle_request("미역국 시켜 줘")
    assert agent.calls == 2 and app.orders == []


def test_agent_never_sees_the_confirmation_id(app):
    seen = []

    class Spy(PlanAgent):
        def predict(self, obs):
            seen.append(obs.get("tool_call"))
            return super().predict(obs)

    agent = Spy([mcp("prepare_order", menu_id="f3"), {"action_type": "answer", "text": "못 해요"}])
    s, _ = session_for(app, "parent", agent, commit_tools=commit_tools(app, "parent"))
    s.handle_request("새우탕 시켜 줘")  # refused (allergy) -> the error goes to the agent
    assert "MENU_NOT_ALLOWED" in seen[1]
    agent = Spy([mcp("prepare_order", menu_id="f1")])
    s, _ = session_for(app, "parent", agent, ["네"], commit_tools=commit_tools(app, "parent"))
    s.handle_request("미역국")
    assert not any("confirmation_id" in (x or "") for x in seen)


# ------------------------------------------------------------------ fast paths
@pytest.mark.parametrize(
    ("role", "text", "routed"),
    [
        ("parent", "점심 뭐 먹을까?", True), ("parent", "배고파", True), ("parent", "밥 언제 와?", True),
        ("parent", "누가 밥 시켰어?", True), ("parent", "밥 잘 받았어", True),
        ("parent", "전복죽 시켜 줘", False), ("parent", "전복죽 말고 뭐 있어?", False),
        ("parent", "택시 불러 줘", False), ("parent", "고등어 먹고 싶어", False),
        ("guardian", "엄마 밥 어디쯤이야?", True), ("guardian", "알림 온 거 있어?", True),
        ("guardian", "최근 주문 내역 알려 줘", True), ("guardian", "엄마 당뇨 있다고 설정해 줘", False),
        ("guardian", "미역국 나트륨 얼마야?", False), ("guardian", "엄마 주문 취소해 줘", True),
    ],
)  # fmt: skip
def test_router_takes_only_clear_requests(app, role, text, routed):
    agent = PlanAgent([{"action_type": "answer", "text": "LLM"}])
    s, _ = session_for(app, role, agent, router=build_router(role, app.errors))
    r = s.handle_request(text)
    assert r.routed is routed and (agent.calls == 0) is routed


def test_router_cancel_asks_first_and_respects_no(app):
    app.call("parent", "prepare_order", {"menu_id": "f1"})
    prep = app.call("parent", "prepare_order", {"menu_id": "f1"})["data"]
    app.call("parent", "place_order", {"confirmation_id": prep["confirmation_id"]})
    router = build_router("parent", app.errors)
    s, tts = session_for(app, "parent", PlanAgent([]), ["아니요"], router=router)
    r = s.handle_request("주문 취소해 줘")
    assert r.status == "cancelled" and not app.orders[-1]["cancelled"]
    assert tts.said[0] == "미역국 정식 주문을 취소할까요?"
    s, tts = session_for(app, "parent", PlanAgent([]), ["네"], router=router)
    r = s.handle_request("주문 취소해 줘")
    assert app.orders[-1]["cancelled"] and "돈은 나가지 않았어요" in r.reply
    assert [st.action["action_type"] for st in r.steps] == ["mcp", "ask_user", "mcp"]


def test_router_too_late_and_guardian_cancel(app):
    prep = app.call("parent", "prepare_order", {"menu_id": "f1"})["data"]
    app.call("parent", "place_order", {"confirmation_id": prep["confirmation_id"]})
    app.clock.t += 15_000  # cooking
    s, _ = session_for(app, "parent", PlanAgent([]), router=build_router("parent", app.errors))
    assert s.handle_request("아까 주문한 거 취소해 줘").reply == app.errors["CANNOT_CANCEL"]
    s, _ = session_for(app, "guardian", PlanAgent([]), router=build_router("guardian", app.errors))
    assert "어머님 화면에서만" in s.handle_request("엄마 주문 취소해 줘").reply
    assert not app.orders[-1]["cancelled"]


# ------------------------------------------------------------------ state snapshot
def test_context_snapshot(app):
    snap = build_context_provider(app, "guardian")()
    assert snap.startswith("App state")
    body = json.loads(snap.split("\n", 1)[1])
    assert body["top_3"] == ["고등어구이 정식", "잔치국수", "미역국 정식"]
    assert [m["menu_id"] for m in body["menus_you_may_offer"]] == ["f2", "f5", "f1", "f4"]
    assert body["latest_order"] is None and body["settings"]["payment_label"] == "등록한 첫째 카드"
    assert "address" not in body["settings"] and "options" not in body["settings"]
    assert {"id": "pb", "label": "나 배달앱"} in body["settings"]["choices"]["platforms"]
    parent = json.loads(build_context_provider(app, "parent")().split("\n", 1)[1])
    assert "settings" not in parent

    agent = PlanAgent([{"action_type": "answer", "text": "네"}])
    s, _ = session_for(app, "parent", agent, context_provider=build_context_provider(app, "parent"))
    s.handle_request("안녕")
    assert "App state" in agent.instructions[0] and agent.instructions[0].endswith(
        "User request (spoken): 안녕"
    )


def test_role_instructions_follow_features():
    on, off = role_instructions("parent"), role_instructions("parent", False, False)
    assert "App state" in on and "do not ask or commit yourself" in on
    assert "get_context" in off and "word for word" in off


# ------------------------------------------------------------------ filler & streaming
def test_filler_when_the_agent_is_slow(app):
    agent = PlanAgent([{"action_type": "answer", "text": "다 됐어요."}], delay=0.3)
    s, tts = session_for(app, "parent", agent, filler_after_sec=0.1)
    r = s.handle_request("뭐 해?")
    assert tts.said == ["잠시만요.", "다 됐어요."] and r.first_speech_sec < 0.25
    assert ("Assistant", "잠시만요.") not in r.dialog  # fillers stay out of the memory

    fast = PlanAgent([{"action_type": "answer", "text": "바로요."}])
    s, tts = session_for(app, "parent", fast, filler_after_sec=0.5)
    s.handle_request("뭐 해?")
    assert tts.said == ["바로요."]


def test_streamed_sentences_are_not_repeated(app):
    agent = PlanAgent(
        [{"action_type": "answer", "text": "미역국이 있어요. 뭐 드실래요?"}],
        stream=["미역국이 있어요."],
    )
    s, tts = session_for(app, "parent", agent)
    r = s.handle_request("뭐 있어?")
    assert tts.said == ["미역국이 있어요.", "뭐 드실래요?"]
    assert r.reply == "미역국이 있어요. 뭐 드실래요?"


def test_streaming_agent_speaks_while_generating(monkeypatch):
    pytest.importorskip("openai")
    from mobile_world.assistant.agent import InAppAssistantAgent

    text = 'Thought: x\nAction: {"action_type": "answer", "text": "첫 문장이에요. 둘째예요."}'
    chunks = [text[i : i + 5] for i in range(0, len(text), 5)]
    spoken_at = []

    def fake_create(**kw):
        assert kw["stream"] and "max_completion_tokens" in kw and "temperature" not in kw
        for c in chunks:
            yield SimpleNamespace(
                usage=None, choices=[SimpleNamespace(delta=SimpleNamespace(content=c))]
            )
        yield SimpleNamespace(
            usage=SimpleNamespace(completion_tokens=3, prompt_tokens=5, prompt_tokens_details=None),
            choices=[],
        )

    agent = InAppAssistantAgent("gpt-5", "http://localhost:1", gui=False)
    monkeypatch.setattr(agent.openai_client.chat.completions, "create", fake_create)
    agent.speech_sink = lambda s: spoken_at.append(s)
    agent.initialize("hi")
    _, action = agent.predict(
        {"screenshot": Image.new("RGB", (4, 4)), "tool_call": None, "ask_user_response": None}
    )
    assert (
        spoken_at == ["첫 문장이에요.", "둘째예요."] and action.text == "첫 문장이에요. 둘째예요."
    )
    assert agent.get_total_token_usage()["completion_tokens"] == 3


def test_filler_thread_is_cleaned_up(app):
    before = threading.active_count()
    agent = PlanAgent([{"action_type": "answer", "text": "네."}])
    s, _ = session_for(app, "parent", agent, filler_after_sec=5)
    s.handle_request("x")
    time.sleep(0.05)
    assert threading.active_count() <= before


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (
            '{"action_type": "prepare_order", "menu_id": "m04"}',
            ("prepare_order", {"menu_id": "m04"}),
        ),
        (
            '{"action_type": "prepare_order", "args": {"menu_id": "m01"}}',
            ("prepare_order", {"menu_id": "m01"}),
        ),
        ('{"action_type": "mcp", "name": "get_context"}', ("get_context", {})),
        ('{"action_type": "launch_rocket"}', None),
    ],
)
def test_tool_name_as_action_type_is_accepted(monkeypatch, raw, expected):
    """Smaller open models often put the tool name in action_type."""
    pytest.importorskip("openai")
    from mobile_world.assistant.agent import InAppAssistantAgent

    tools = [
        {"name": "prepare_order", "inputSchema": {}},
        {"name": "get_context", "inputSchema": {}},
    ]
    agent = InAppAssistantAgent(
        "qwen2.5-14b-instruct", "http://localhost:1", gui=False, tools=tools
    )
    reply = SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content=f"Thought: x\nAction: {raw}"), finish_reason="stop"
            )
        ],
        usage=None,
    )
    monkeypatch.setattr(agent.openai_client.chat.completions, "create", lambda **kw: reply)
    agent.initialize("hi")
    _, action = agent.predict(
        {"screenshot": Image.new("RGB", (4, 4)), "tool_call": None, "ask_user_response": None}
    )
    if expected is None:
        assert action.action_type == "unknown"  # unusable, but no crash
    else:
        assert (action.action_type, action.action_name, action.action_json) == ("mcp", *expected)


# ------------------------------------------------------------------ language guard
def test_keep_language_drops_only_foreign_sentences():
    from mobile_world.assistant.session import keep_language

    assert keep_language("현재 음식이 오고 있어요. 도착后再下单。", "ko") == (
        "현재 음식이 오고 있어요.",
        ["도착后再下单。"],
    )
    assert keep_language("주문번호 PA-123 이에요.", "ko") == ("주문번호 PA-123 이에요.", [])
    assert keep_language("영양均衡且符合老年人口味", "ko")[0] == ""
    assert keep_language("ありがとう。네, 알겠어요.", "ko") == ("네, 알겠어요.", ["ありがとう。"])


def test_mixed_reply_is_cleaned_before_speaking(app):
    agent = PlanAgent(
        [{"action_type": "answer", "text": "현재 음식이 오고 있어요. 도착后再下单。"}]
    )
    s, tts = session_for(app, "parent", agent)
    r = s.handle_request("또 시켜 줘")
    assert tts.said == ["현재 음식이 오고 있어요."] and r.reply == "현재 음식이 오고 있어요."
    assert r.steps[0].action["language_dropped"] == ["도착后再下单。"]


def test_all_foreign_reply_is_regenerated_once(app):
    agent = PlanAgent(
        [
            {"action_type": "answer", "text": "请稍等。"},
            {"action_type": "answer", "text": "잠시만 기다려 주세요."},
        ]
    )
    s, tts = session_for(app, "parent", agent)
    r = s.handle_request("뭐 해?")
    assert agent.calls == 2 and tts.said == ["잠시만 기다려 주세요."]
    assert r.steps[0].action["language_dropped"] == ["请稍等。"] and r.status == "answered"

    agent = PlanAgent(
        [{"action_type": "answer", "text": "请稍等。"}, {"action_type": "answer", "text": "好的。"}]
    )
    s, tts = session_for(app, "parent", agent)
    r = s.handle_request("뭐 해?")
    assert r.status == "failed" and tts.said == ["죄송해요, 요청을 처리하지 못했어요."]


def test_foreign_sentence_is_never_streamed(app):
    agent = PlanAgent(
        [{"action_type": "answer", "text": "현재 음식이 오고 있어요. 도착后再下单。"}],
        stream=["현재 음식이 오고 있어요.", "도착后再下单。"],
    )
    s, tts = session_for(app, "parent", agent)
    s.handle_request("또 시켜 줘")
    assert tts.said == ["현재 음식이 오고 있어요."]  # spoken once, nothing in Chinese


def test_guard_can_be_turned_off(app):
    agent = PlanAgent([{"action_type": "answer", "text": "你好。"}])
    s, tts = session_for(app, "parent", agent, language_guard=False)
    s.handle_request("x")
    assert tts.said == ["你好。"]


@pytest.mark.parametrize("text", ["배달 얼마나 남았어?", "몇 분 남았어", "언제쯤 와?", "얼마나 더 기다려야 돼"])
def test_router_says_minutes_left(app, text):
    app.logic.status_times = (0, 600, 1200, 1800)  # a real delivery: 30 minutes
    prep = app.call("parent", "prepare_order", {"menu_id": "f1"})["data"]
    app.call("parent", "place_order", {"confirmation_id": prep["confirmation_id"]})
    app.clock.t += 21 * 60_000  # delivering, 9 minutes to go
    agent = PlanAgent([{"action_type": "answer", "text": "LLM"}])
    s, _ = session_for(app, "parent", agent, router=build_router("parent", app.errors))
    r = s.handle_request(text)
    assert r.routed and agent.calls == 0
    assert r.reply.startswith("지금은 배달중 단계예요. 9분쯤 남았어요.") and "쯤 도착해요" in r.reply
    app.clock.t += 8 * 60_000 + 30_000  # 30 s left
    assert "1분도 안 남았어요" in s.handle_request(text).reply
    app.clock.t += 60_000  # arrived: no minutes, just that it has come
    assert "도착했어요" in s.handle_request(text).reply
