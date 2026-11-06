"""Rule-based handling of frequent 늘밥 requests, without the LLM (≈ instant replies).

Deliberately conservative: a request is handled here only when it clearly matches one
intent and carries nothing else. Anything that names a menu to order, touches settings or
nutrition, or contains "말고" goes to the LLM. Cancelling still asks for confirmation.
Returning ``None`` means "not mine, let the agent handle it".
"""

from __future__ import annotations

import re
from collections.abc import Callable

from mobile_world.assistant.neulbab.logic import copula

# Requests with these go to the LLM (ordering a menu, settings, nutrition, alternatives).
_LLM_ONLY = re.compile(
    r"시켜|주문해|주문 해|주문할래|주문하고|보내 ?줘|바꿔|바꾸|설정|알레르기|나트륨|영양|칼로리|당뇨|혈압|말고|대신"
)
_RECOMMEND = re.compile(r"뭐\s*(먹|드|있)|배고파|메뉴\s*(뭐|알려|추천|보여)|추천해|먹을\s*거")
_STATUS = re.compile(
    r"언제\s*(와|오|도착)|어디쯤|도착했|주문\s*어떻게|밥\s*어떻게|어떻게\s*됐|오고\s*있|왔(어|나|니)\s*\??$"
)
# "how long until it comes?" -> answer with the minutes left first
_REMAINING = re.compile(r"얼마나\s*(더\s*)?(남|걸|기다|있어야|있으면)|몇\s*분|언제쯤|아직\s*멀|금방\s*와")
_WHO = re.compile(r"누가.{0,6}(시켰|주문)")
_CANCEL = re.compile(r"취소\s*(해|할|좀|하고)")
_ACK = re.compile(r"잘\s*받았|받았어|받았습니다")
_NOTICE = re.compile(r"알림")
_HISTORY = re.compile(r"주문\s*내역|최근\s*주문|뭐\s*시켰|주문한\s*거\s*(알려|보여)")

Route = Callable[[str, object], "str | tuple[str, str] | None"]


def _say(res: dict) -> str:
    return res.get("say") or res.get("error", {}).get("message", "")


def build_router(role: str, errors: dict[str, str]) -> Route:
    """``errors``: the contract's error messages (code -> sentence)."""

    def status(ctx) -> dict:
        return ctx.tool("get_order_status")

    def route(request: str, ctx) -> str | tuple[str, str] | None:
        text = request.strip()
        if not text:
            return None

        if _CANCEL.search(text) and "하지 마" not in text:
            if role == "guardian":
                p = (ctx.tool("get_context").get("data") or {}).get("parent_honorific", "부모님")
                return f"주문 취소는 {p} 화면에서만 할 수 있어요. {p}께 말씀드려 주세요."
            st = status(ctx)
            if not st.get("ok"):
                return _say(st)
            order = st["data"]
            if order["status"] == "cancelled":
                return "그 주문은 이미 취소됐어요."
            if not order["can_cancel"]:
                return errors["CANNOT_CANCEL"]
            name = order["menu_name"]
            verdict = ctx.confirm(f"{name} 주문을 취소할까요?")
            if verdict == "silent":
                return ctx.session.phrases["no_answer"], "cancelled"
            if verdict == "no":
                return "알겠어요. 주문은 그대로 둘게요.", "cancelled"
            return _say(ctx.tool("cancel_order", order_id=order["order_id"]))

        if _LLM_ONLY.search(text):
            return None

        if role == "parent" and _ACK.search(text):
            st = status(ctx)
            if not st.get("ok"):
                return _say(st)
            order = st["data"]
            if order["status"] != "arrived":
                return f"{errors['NOT_ARRIVED']}. {order['eta_text']}쯤 도착해요."
            if order["acknowledged"]:
                return "이미 잘 받으셨다고 표시돼 있어요. 맛있게 드세요."
            return _say(ctx.tool("acknowledge_arrival", order_id=order["order_id"]))

        if _WHO.search(text):
            st = status(ctx)
            return st["data"]["ordered_by_text"] if st.get("ok") else _say(st)

        if _REMAINING.search(text):
            st = status(ctx)
            d = st.get("data") or {}
            if st.get("ok") and not d.get("finished") and d.get("remaining_text"):
                return f"지금은 {d['status_label']} 단계예요. {d['remaining_text']}."
            return _say(st)

        if _STATUS.search(text):
            return _say(status(ctx))

        if role == "guardian" and _NOTICE.search(text):
            return _say(ctx.tool("list_notifications", limit=3))

        if role == "guardian" and _HISTORY.search(text):
            res = ctx.tool("list_orders", limit=3)
            orders = (res.get("data") or {}).get("orders", [])
            if not orders:
                return _say(res)
            names = [o["menu_name"] for o in orders]
            last = names[-1]
            return f"최근 주문은 {', '.join(names)}{copula(last)}."

        if _RECOMMEND.search(text):
            res = ctx.tool("get_recommendations")
            if not res.get("ok") or not res["data"]["menus"]:
                return _say(res)
            tail = "드시고 싶은 걸 말씀해 주세요." if role == "parent" else "어떤 걸로 주문할까요?"
            return f"{_say(res)}. {tail}"

        return None

    return route
