"""Expose a ``MockNeulbabApp`` to the voice assistant as contract tools for one role.

Also: role-specific speaking instructions, the up-front app-state snapshot (so the agent
does not spend LLM round trips on lookups), and the prepare -> commit tool pairs that the
session confirms by itself.
"""

from __future__ import annotations

import json
from collections.abc import Callable

from mobile_world.assistant.app_tools import AppTool, ToolRegistry, _agent_description
from mobile_world.assistant.neulbab.mock_app import MockNeulbabApp

# Spoken examples for the action table (instead of the generic ones).
PROMPT_EXAMPLES = {
    "ask_example": "전복죽, 10,000원이에요. 자녀분 카드로 결제돼요. 주문할까요?",
    "answer_example": "전복죽을 주문했어요. 오후 12시 30분쯤 도착해요.",
}


def role_instructions(role: str, prefetch: bool = True, session_confirm: bool = True) -> str:
    """Speaking/behaviour guidance for ``role``, matching how the session is set up."""
    state = (
        "- Each request comes with an `App state` snapshot (context, menus you may offer"
        + (", current settings" if role == "guardian" else "")
        + ", latest order). Use it directly; call a get_* tool only for something not in it."
        if prefetch
        else "- Call `get_context` once at the start of each request."
    )
    commit = (
        "the app then reads its confirmation question to the user and places it on a clear yes, "
        "so you do not ask or commit yourself. Never ask your own confirmation first, and do not "
        'ask "did you mean …?" when one menu clearly matches (also for misheard names such as '
        "전보죽 → 전복죽): call the prepare tool right away — the app's question names the menu "
        "and price, so the user can still say no. Ask only if several menus could match"
        if session_confirm
        else "ask its `confirm_question` with `ask_user` word for word, then call the commit tool "
        "only after a clear yes. On no or hesitation, do not act"
    )
    if role == "parent":
        return f"""\
# 늘밥 — talking with the parent (어르신)
- The user is an elderly parent who may see and hear poorly and is not used to apps. Use warm, \
polite Korean 해요체 with honorifics, short sentences, one idea per sentence, no English or \
technical words (never say id, confirmation, tool, platform order number).
{state}
- "뭐 먹지 / 배고파 / 점심" means: offer at most 3 menu names from the menus you may offer.
- Describe menus ONLY with their facts (category_label, tags, description, price). Never invent taste, side dishes or ingredients. A menu is soft only if its tags include "부드러운 음식"; soup or broth means category 국과 찌개 (죽 is porridge); fish means category 생선. Offer only menus that match what was asked.
- When the parent leaves it to you ("아무거나", "알아서 해 줘", "몰라", "밥 줘"), do not list choices: pick the first of `top_3` that fits and call `prepare_order` right away — the app's question names the menu and price, so the parent can still say no.
- Ask at most ONE clarifying question per request, with at most two options ("가자미조림으로 할까요, 고등어구이로 할까요?"). If the answer is just "네/응/그래", take the first option. If the answer is still unclear, choose the best fitting menu and call `prepare_order`.
- Ordering: call `prepare_order` with the menu_id; {commit}. Only menus in the snapshot / \
`get_recommendations` can be ordered; for any other menu, say briefly that it cannot be chosen now.
- Cancelling: confirm once with `ask_user` ("…주문을 취소할까요?") before `cancel_order`.
- If a health note or disclaimer is present, mention the note briefly; say the disclaimer the \
first time health is discussed.
- Errors: read `error.message` gently, naming the menu; never retry a refused order with another \
menu unless the parent asks."""
    return f"""\
# 늘밥 — talking with the guardian (보호자, the adult child)
- The user manages their parent's meals remotely. Be concise and clear (해요체).
{state}
- Proxy orders: call `prepare_order`; settings: call `prepare_settings_change`. For both, {commit}.
- Settings lists replace the whole list: send the full new list (current values + the change). \
If `menus_after` is 0, the guardian must hear that before agreeing.
- You cannot cancel orders as the guardian; say that the parent can cancel on their screen.
- When the guardian mentions a health or eating difficulty of the parent (이가 안 좋아 / 씹기 힘들어 → chewing, 혈압 → hypertension, 당뇨·혈당 → diabetes, 콩팥·신장 → kidney), propose the matching health setting with `prepare_settings_change` (send the full new list); the app asks the guardian to confirm.
- When the guardian leaves the menu to you ("엄마 밥 좀 챙겨 줘"), pick the first of `top_3` and call `prepare_order` (the app asks the guardian to confirm). Describe menus only with their facts.
- Address, payment, family code and accounts cannot be changed by voice: point to the app screen.
- Nutrition answers come only from `get_menu_detail`; add that it does not replace a doctor or \
dietitian (의사·영양사 상담을 대신하지 않아요)."""


# Backwards compatible defaults (all latency features on).
ROLE_INSTRUCTIONS = {r: role_instructions(r) for r in ("parent", "guardian")}


def build_toolset(app: MockNeulbabApp, role: str) -> ToolRegistry:
    """Contract tools available to ``role``, executed against ``app``."""
    registry = ToolRegistry()
    for entry in app.contract["tools"]:
        if role not in entry["roles"]:
            continue
        name = entry["name"]
        registry.register(
            AppTool(
                name=name,
                description=_agent_description(entry),
                handler=lambda args, _device, _n=name: app.call(role, _n, args),
                parameters=entry["parameters"],
            )
        )
    return registry


def commit_tools(app: MockNeulbabApp, role: str) -> dict[str, str]:
    """prepare -> commit pairs (from the contract's ``commit_tool``) usable by ``role``."""
    return {
        t["name"]: t["commit_tool"]
        for t in app.contract["tools"]
        if "commit_tool" in t and role in t["roles"]
    }


def build_context_provider(
    app: MockNeulbabApp,
    role: str,
    call: Callable[[str, str, dict], dict] | None = None,
) -> Callable[[], str]:
    """Returns a function giving a compact snapshot of the app state for ``role``.

    ``call`` defaults to ``app.call``; evaluations pass an untraced call so snapshot reads are
    not counted as the agent's tool calls.
    """
    call = call or app.call

    def data(name: str, **args) -> dict | None:
        res = call(role, name, args)
        return res.get("data") if res.get("ok") else {"error": res.get("error", {}).get("code")}

    def provider() -> str:
        ctx = data("get_context") or {}
        snap: dict = {
            "who": {
                k: ctx.get(k)
                for k in ("role", "parent_honorific", "guardian_title", "health_conditions", "now")
            },
            "has_settings": ctx.get("has_settings"),
        }
        if ctx.get("has_settings"):
            rec = data("get_recommendations", include_more=True) or {}
            pick = ("menu_id", "name", "price_text", "category_label", "tags", "description")
            snap["menus_you_may_offer"] = [
                {k: m[k] for k in pick} for m in rec.get("menus", []) + rec.get("more_menus", [])
            ]
            snap["top_3"] = [m["name"] for m in rec.get("menus", [])]
            for k in ("health_note", "disclaimer"):
                if rec.get(k):
                    snap[k] = rec[k]
        order = data("get_order_status")
        snap["latest_order"] = None if (order or {}).get("error") == "NO_ORDER" else order
        if role == "guardian" and ctx.get("has_settings"):
            settings = data("get_settings") or {}
            settings.pop("options", None)
            snap["settings"] = settings
        return "App state (fetched just now):\n" + json.dumps(
            snap, ensure_ascii=False, separators=(",", ":")
        )

    return provider
