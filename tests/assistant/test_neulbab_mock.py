"""Behaviour of the headless 늘밥 mock app against the tool contract (self-contained data)."""

import json
from zoneinfo import ZoneInfo

import pytest

from mobile_world.assistant.device import ToolsOnlyDevice
from mobile_world.assistant.neulbab import MockNeulbabApp, build_toolset, load_data
from mobile_world.assistant.scope import AppScopeGuard
from mobile_world.assistant.session import VoiceAssistantSession
from mobile_world.assistant.speech import SpeechToText, TextToSpeech
from mobile_world.runtime.utils.models import JSONAction

KST = ZoneInfo("Asia/Seoul")
T0 = 1_762_311_600_000  # 2025-11-05 12:00:00 KST


class Clock:
    def __init__(self, t=T0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, sec):
        self.t += int(sec * 1000)


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def app(app_dir, clock):
    return MockNeulbabApp(load_data(app_dir), clock=clock, tz=KST)


def ok(res):
    assert res["ok"], res
    return res["data"]


def err(res):
    assert not res["ok"], res
    return res["error"]["code"]


def order_as(app, role, menu_id):
    """prepare + place; returns the first failing envelope or the place_order result."""
    prep = app.call(role, "prepare_order", {"menu_id": menu_id})
    if not prep["ok"]:
        return prep
    return app.call(role, "place_order", {"confirmation_id": prep["data"]["confirmation_id"]})


# ------------------------------------------------------------------ reading
def test_recommendations_follow_web_ranking(app):
    res = app.call("parent", "get_recommendations", {"include_more": True})
    data = ok(res)
    # 새우 allergy removes f3, f6 is not sold; 생선 +3; preferred 부드러운음식 +1; ties by price
    assert [m["menu_id"] for m in data["menus"]] == ["f2", "f5", "f1"]
    assert [m["menu_id"] for m in data["more_menus"]] == ["f4"]
    assert data["menus"][0]["reason"] == "좋아하시는 생선 음식이에요"
    assert data["menus"][0]["price_text"] == "9,500원"
    assert res["say"] == "고등어구이 정식, 잔치국수, 미역국 정식이 있어요"
    assert data["disclaimer"] == ""


def test_context_and_wrong_role(app):
    ctx = ok(app.call("parent", "get_context"))
    assert ctx["role"] == "parent" and ctx["parent_honorific"] == "어머님"
    assert ctx["guardian_title"] == "자녀분" and ctx["active_order_id"] is None
    assert ctx["now"].startswith("2025-11-05T12:00")
    assert err(app.call("parent", "get_settings")) == "WRONG_ROLE"
    assert err(app.call("parent", "open_screen", {"screen": "settings"})) == "WRONG_ROLE"
    assert err(app.call("nobody", "get_context")) == "NOT_LOGGED_IN"
    assert err(app.call("parent", "get_menu_detail", {"menu_id": "f1", "x": 1})) == (
        "INVALID_ARGUMENT"
    )


def test_menu_detail_by_role(app):
    parent = ok(app.call("parent", "get_menu_detail", {"menu_id": "f3"}))
    assert not parent["orderable"] and "새우" in parent["not_orderable_reason"]
    assert "nutrition" not in parent
    guardian = ok(app.call("guardian", "get_menu_detail", {"menu_id": "f1"}))
    rows = {r["label"]: r["value_text"] for r in guardian["nutrition"]}
    assert rows["나트륨"] == "600mg" and rows["당류"] == "1.0g" and rows["단백질"] == "모름"
    assert guardian["sources"][0]["url"].startswith("https://")
    assert err(app.call("guardian", "get_menu_detail", {"menu_id": "zz"})) == "MENU_NOT_FOUND"


# ------------------------------------------------------------------ ordering
def test_parent_order_flow_and_guardian_view(app, clock):
    prep = ok(app.call("parent", "prepare_order", {"menu_id": "f2"}))
    assert prep["confirm_question"] == (
        "고등어구이 정식, 9,500원이에요. 자녀분 카드로 결제돼요. 주문할까요?"
    )
    assert app.orders == []  # nothing ordered yet
    res = app.call("parent", "place_order", {"confirmation_id": prep["confirmation_id"]})
    placed = ok(res)
    assert placed["eta_text"] == "오후 12시"  # created 12:00:00 + 30 s
    assert res["say"].startswith("고등어구이 정식을 주문했어요.")
    assert app.orders[0]["orderedBy"] == "parent" and app.orders[0]["platformOrderId"] == f"PA-{T0}"

    g = ok(app.call("guardian", "get_order_status"))
    assert g["ordered_by_text"] == "어머님이 고등어구이 정식을 주문하셨어요"
    assert g["status"] == "received" and g["can_cancel"] is False  # guardian cannot cancel
    clock.advance(25)
    notes = ok(app.call("guardian", "list_notifications"))["notifications"]
    assert [n["text"] for n in notes] == [
        "배달 기사님이 가고 있어요",
        "고등어구이 정식을 만들고 있어요",
        "어머님이 고등어구이 정식을 주문하셨어요",
    ]
    assert notes[0]["time_text"] == "낮 12시"


def test_confirmation_is_single_use_role_bound_and_expires(app, clock):
    prep = ok(app.call("parent", "prepare_order", {"menu_id": "f1"}))
    cid = prep["confirmation_id"]
    assert err(app.call("guardian", "place_order", {"confirmation_id": cid})) == (
        "CONFIRMATION_INVALID"
    )
    # the wrong-role attempt consumed it
    assert err(app.call("parent", "place_order", {"confirmation_id": cid})) == (
        "CONFIRMATION_INVALID"
    )
    prep = ok(app.call("parent", "prepare_order", {"menu_id": "f1"}))
    clock.advance(121)
    assert err(app.call("parent", "place_order", {"confirmation_id": prep["confirmation_id"]})) == (
        "CONFIRMATION_INVALID"
    )
    assert app.orders == []


def test_filters_cannot_be_bypassed(app):
    assert err(app.call("parent", "prepare_order", {"menu_id": "f3"})) == "MENU_NOT_ALLOWED"
    assert err(app.call("parent", "prepare_order", {"menu_id": "f6"})) == "MENU_NOT_ALLOWED"
    # prepared before the guardian adds 혈압 -> rechecked at place time
    prep = ok(app.call("parent", "prepare_order", {"menu_id": "f5"}))
    change = ok(
        app.call("guardian", "prepare_settings_change", {"health_conditions": ["hypertension"]})
    )
    ok(
        app.call(
            "guardian", "apply_settings_change", {"confirmation_id": change["confirmation_id"]}
        )
    )
    assert err(app.call("parent", "place_order", {"confirmation_id": prep["confirmation_id"]})) == (
        "MENU_NOT_ALLOWED"
    )


def test_cancel_window_and_arrival(app, clock):
    first = ok(order_as(app, "parent", "f1"))
    assert err(order_as(app, "parent", "f2")) == "ACTIVE_ORDER_EXISTS"
    assert err(app.call("parent", "cancel_order", {"order_id": "nope"})) == "ORDER_MISMATCH"
    clock.advance(10)  # cooking
    assert err(app.call("parent", "cancel_order", {"order_id": first["order_id"]})) == (
        "CANNOT_CANCEL"
    )
    assert err(app.call("parent", "acknowledge_arrival", {"order_id": first["order_id"]})) == (
        "NOT_ARRIVED"
    )
    clock.advance(20)  # arrived
    ok(app.call("parent", "acknowledge_arrival", {"order_id": first["order_id"]}))
    ok(order_as(app, "parent", "f2"))  # a new order is possible again

    second = app.orders[-1]
    res = app.call("parent", "cancel_order", {"order_id": second["id"]})
    assert ok(res)["status"] == "cancelled" and "돈은 나가지 않았어요" in res["say"]
    assert ok(app.call("guardian", "list_orders"))["orders"][0]["status_label"] == "취소했어요"


def test_guardian_proxy_order(app, clock):
    ok(order_as(app, "parent", "f1"))
    assert err(order_as(app, "guardian", "f2")) == "GUARDIAN_BLOCKED"
    clock.advance(30)
    prep = ok(app.call("guardian", "prepare_order", {"menu_id": "f2"}))
    assert prep["confirm_question"].startswith("어머님께 고등어구이 정식을 대신 주문할게요.")
    assert "등록한 첫째 카드로 결제돼요" in prep["confirm_question"]
    ok(app.call("guardian", "place_order", {"confirmation_id": prep["confirmation_id"]}))
    p = ok(app.call("parent", "get_order_status"))
    assert p["ordered_by"] == "guardian" and p["ordered_by_text"] == "자녀분이 주문했어요"


@pytest.mark.parametrize(
    ("platform", "prefix"), [("pa", f"PA-{T0}"), ("pb", str(T0 % 1_000_000_000)), ("pc", "nc_")]
)
def test_platform_adapters(app, platform, prefix):
    if platform != "pa":  # pa is the default platform already
        change = ok(app.call("guardian", "prepare_settings_change", {"platform": platform}))
        cid = change["confirmation_id"]
        ok(app.call("guardian", "apply_settings_change", {"confirmation_id": cid}))
    placed = ok(order_as(app, "parent", "f1"))
    assert app.orders[-1]["platformOrderId"].startswith(prefix)
    assert placed["platform_label"].endswith("배달앱")
    ok(app.call("parent", "cancel_order", {"order_id": placed["order_id"]}))  # adapter accepts


# ------------------------------------------------------------------ settings
def test_settings_change_flow(app):
    s = ok(app.call("guardian", "get_settings"))
    assert s["payment_label"] == "등록한 첫째 카드" and s["has_address"]
    assert "address" not in s and {"id": "pb", "label": "나 배달앱"} in s["options"]["platforms"]

    assert err(app.call("guardian", "prepare_settings_change", {"allergies": ["돌"]})) == (
        "INVALID_ARGUMENT"
    )
    assert err(app.call("guardian", "prepare_settings_change", {"allergies": ["새우"]})) == (
        "INVALID_ARGUMENT"  # no actual change
    )
    prep = ok(
        app.call(
            "guardian",
            "prepare_settings_change",
            {"health_conditions": ["hypertension", "chewing"], "required_tags": ["고단백"]},
        )
    )
    assert prep["menus_after"] == 0
    assert "바꾸면 고를 수 있는 메뉴가 없어요" in prep["confirm_question"]

    prep = ok(
        app.call("guardian", "prepare_settings_change", {"health_conditions": ["hypertension"]})
    )
    assert prep["changes"] == [
        {"field_label": "건강 상태", "before_text": "없음", "after_text": "혈압이 높아요"}
    ]
    assert prep["confirm_question"] == "건강 상태를 혈압이 높아요로 바꿀게요. 바꿀까요?"
    res = app.call(
        "guardian", "apply_settings_change", {"confirmation_id": prep["confirmation_id"]}
    )
    assert ok(res)["updated_fields"] == ["건강 상태"]
    assert res["say"] == "설정을 바꿨어요. 이제 싱겁게 드시도록 골랐어요"

    rec = ok(app.call("parent", "get_recommendations", {"include_more": True}))
    assert [m["menu_id"] for m in rec["menus"] + rec["more_menus"]] == ["f1"]
    assert rec["disclaimer"] == "의사·영양사 상담을 대신하지 않아요"


def test_stale_settings_confirmation(app):
    a = ok(app.call("guardian", "prepare_settings_change", {"platform": "pb"}))
    b = ok(app.call("guardian", "prepare_settings_change", {"platform": "pc"}))
    ok(app.call("guardian", "apply_settings_change", {"confirmation_id": b["confirmation_id"]}))
    assert err(
        app.call("guardian", "apply_settings_change", {"confirmation_id": a["confirmation_id"]})
    ) == ("CONFIRMATION_INVALID")


def test_no_settings_and_state_file(app_dir, clock, tmp_path):
    bare = MockNeulbabApp(load_data(app_dir), clock=clock, with_sample_settings=False)
    assert err(bare.call("parent", "get_recommendations")) == "NO_SETTINGS"
    assert err(bare.call("parent", "get_order_status")) == "NO_ORDER"

    state = tmp_path / "state.json"
    one = MockNeulbabApp(load_data(app_dir), clock=clock, tz=KST, state_file=state)
    ok(order_as(one, "parent", "f1"))
    two = MockNeulbabApp(load_data(app_dir), clock=clock, tz=KST, state_file=state)
    assert ok(two.call("guardian", "get_order_status"))["menu_name"] == "미역국 정식"


def test_toolset_matches_contract_roles(app):
    parent, guardian = build_toolset(app, "parent"), build_toolset(app, "guardian")
    assert len(parent) == 9 and len(guardian) == 12
    out = json.loads(parent.call("get_context", {}, None))
    assert out["ok"] and out["data"]["role"] == "parent"
    # schema validation happens before the app is called
    assert parent.call("prepare_order", {}, None).startswith("Error: invalid arguments")


# ------------------------------------------------------------------ full voice loop
class ReactiveAgent:
    """Plays a fixed plan; plan steps may be callables of the last observation."""

    def __init__(self, plan):
        self.plan = list(plan)
        self.observations = []

    def initialize(self, instruction):
        self.instruction = instruction

    def predict(self, obs):
        self.observations.append(obs)
        step = self.plan.pop(0)
        return "Thought: plan", JSONAction(**(step(obs) if callable(step) else step))

    def done(self):
        pass


class Scripted(SpeechToText):
    def __init__(self, lines):
        self.lines = list(lines)

    def listen(self, prompt=None):
        return self.lines.pop(0) if self.lines else ""


class Spoken(TextToSpeech):
    def __init__(self):
        self.said = []

    def speak(self, text):
        self.said.append(text)


def _data(obs):
    return json.loads(obs["tool_call"])["data"]


def test_voice_session_orders_through_mock(app):
    def mcp(name, **args):
        return {"action_type": "mcp", "action_name": name, "action_json": args}

    agent = ReactiveAgent(
        [
            mcp("get_recommendations"),
            {"action_type": "click", "x": 5, "y": 5},  # refused: no screen
            lambda obs: mcp(
                "prepare_order", menu_id=_data(agent.observations[1])["menus"][0]["menu_id"]
            ),
            lambda obs: {"action_type": "ask_user", "text": _data(obs)["confirm_question"]},
            lambda obs: mcp(
                "place_order", confirmation_id=_data(agent.observations[3])["confirmation_id"]
            ),
            lambda obs: {"action_type": "answer", "text": json.loads(obs["tool_call"])["say"]},
        ]
    )
    tools = build_toolset(app, "parent")
    device = ToolsOnlyDevice("com.neulbab.app")
    guard = AppScopeGuard("com.neulbab.app", tool_names=tools.names, allow_gui=False)
    tts = Spoken()
    session = VoiceAssistantSession(
        agent, device, Scripted(["네"]), tts, guard, tools=tools, app_name="늘밥", tool_wait_time=0
    )
    result = session.handle_request("점심 뭐 먹지")

    assert result.status == "answered"
    assert agent.observations[2]["tool_call"].startswith("Blocked: there is no screen")
    assert device.executed == []
    assert tts.said[0] == "고등어구이 정식, 9,500원이에요. 자녀분 카드로 결제돼요. 주문할까요?"
    assert tts.said[1].startswith("고등어구이 정식을 주문했어요.")
    assert app.orders[0]["menuId"] == "f2" and app.orders[0]["orderedBy"] == "parent"


def test_agent_prompt_in_tools_only_mode(app):
    pytest.importorskip("openai")
    from mobile_world.assistant.agent import InAppAssistantAgent
    from mobile_world.assistant.neulbab import ROLE_INSTRUCTIONS

    tools = build_toolset(app, "parent")
    agent = InAppAssistantAgent(
        "m", "http://localhost:1", app_name="늘밥", tools=tools.schemas(),
        gui=False, extra_instructions=ROLE_INSTRUCTIONS["parent"],
    )  # fmt: skip
    prompt = agent._render_system_prompt(1000)
    assert "There is NO screen" in prompt and "talking with the parent" in prompt
    assert '"name": "prepare_order"' in prompt and "Returns data fields: confirmation_id" in prompt
    assert '"name": "get_settings"' not in prompt  # guardian-only tool not offered

    with_gui = InAppAssistantAgent("m", "http://localhost:1", app_name="X")._render_system_prompt(
        1000
    )
    assert "There is NO screen" not in with_gui and "talking with" not in with_gui
