"""Expose a ``MockNeulbabApp`` to the voice assistant as contract tools for one role."""

from __future__ import annotations

from mobile_world.assistant.app_tools import AppTool, ToolRegistry, _agent_description
from mobile_world.assistant.neulbab.mock_app import MockNeulbabApp

ROLE_INSTRUCTIONS = {
    "parent": """\
# 늘밥 — talking with the parent (어르신)
- The user is an elderly parent who may see and hear poorly and is not used to apps. Use warm, \
polite Korean 해요체 with honorifics, short sentences, one idea per sentence, no English or \
technical words (never say id, confirmation, tool, platform order number).
- Call `get_context` once at the start of each request.
- "뭐 먹지 / 배고파 / 점심" means: call `get_recommendations` and offer at most 3 menu names.
- Ordering: `prepare_order` -> ask its `confirm_question` with `ask_user` word for word -> \
`place_order` only after a clear yes (네, 응, 그래, 주문해). On no or hesitation, do not order.
- If a health note or disclaimer is returned, mention the note briefly; say the disclaimer the \
first time health is discussed.
- Errors: read `error.message` gently; never retry a refused order with another menu unless the \
parent asks.""",
    "guardian": """\
# 늘밥 — talking with the guardian (보호자, the adult child)
- The user manages their parent's meals remotely. Be concise and clear (해요체).
- Call `get_context` once at the start of each request.
- Proxy orders and settings changes always go through `prepare_*` -> `ask_user` with the \
returned `confirm_question` -> the matching commit tool only after a clear yes.
- Settings lists replace the whole list: read `get_settings` first and send the full new list. \
If `menus_after` is 0, make sure the guardian hears that before agreeing.
- Nutrition answers come only from `get_menu_detail`; add that it does not replace a doctor or \
dietitian (의사·영양사 상담을 대신하지 않아요).""",
}


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
