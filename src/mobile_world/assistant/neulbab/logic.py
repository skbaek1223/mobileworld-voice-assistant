"""Pure 늘밥 rules ported from the web prototype's ``mock_api.js`` (v4).

Function names follow the JS ones (snake_case) so behaviour can be compared side by side;
``tests/assistant/test_neulbab_parity.py`` runs both and checks they agree.
Times are epoch milliseconds, like ``Date.now()``.
"""

from __future__ import annotations

import math
import re
from datetime import datetime, tzinfo
from typing import Any

from mobile_world.assistant.neulbab.data import NeulbabData

# ---- constants (mock_api.js) -------------------------------------------------------------
STATUS_TIMES_SEC = (0, 10, 20, 30)
STATUS_KEYS = ("received", "cooking", "delivering", "arrived")
STATUS_LABELS = {
    "received": "접수",
    "cooking": "조리중",
    "delivering": "배달중",
    "arrived": "도착",
    "cancelled": "취소됨",
}
STATUS_DESC = {
    "received": "가게에서 주문을 받았어요",
    "cooking": "가게에서 음식을 만들고 있어요",
    "delivering": "배달하는 분이 음식을 가지고 가고 있어요",
    "arrived": "음식이 문 앞에 도착했어요",
    "cancelled": "주문을 취소했어요. 돈은 나가지 않아요",
}
CATEGORY_ORDER = ("국·찌개", "생선", "죽", "고기반찬", "밥·면")
CATEGORY_LABEL = {"국·찌개": "국과 찌개", "밥·면": "밥과 국수"}
TAG_LABEL = {"부드러운음식": "부드러운 음식"}
PAYMENT_LABEL = {"가상 카드 A": "등록한 첫째 카드", "가상 카드 B": "등록한 둘째 카드"}
TAG_LIST = ("저염", "당뇨식", "부드러운음식", "고단백")
GUARDIAN_TITLES = ("자녀분", "따님", "아드님", "보호자")
DEFAULT_GUARDIAN_TITLE = "자녀분"
MAX_CARDS = 3
MAX_MORE = 6
RECENT_MS = 3 * 24 * 3600 * 1000
NUTRI_KEYS = ("kcal", "sodium_mg", "sugars_g", "protein_g", "potassium_mg")
NUTRI_LABEL = {
    "kcal": "열량",
    "sodium_mg": "나트륨",
    "sugars_g": "당류",
    "protein_g": "단백질",
    "potassium_mg": "칼륨",
}
HEALTH_RESULT_TEXT = {
    "ok": "기준에 맞아요",
    "over": "기준보다 많아요",
    "unknown": "정보가 없어서 빼 두었어요",
}
HEALTH_DISCLAIMER = "의사·영양사 상담을 대신하지 않아요"
TEXT = {
    "noMenu": "지금 드실 수 있는 메뉴가 없어요. 자녀분께 말씀해 주세요",
    "noGuardianMenu": "조건에 맞는 메뉴가 없어요. 설정 바꾸기 버튼을 눌러 조건을 확인해 주세요",
    "cancelledParent": "돈은 나가지 않았어요",
    "payParent": "자녀분 카드로 결제돼요",
}


def js_round(x: float) -> int:
    """``Math.round``: halves go up (Python's round() uses banker's rounding)."""
    return math.floor(x + 0.5)


# ---- Korean particles & phrasing --------------------------------------------------------
def last_hangul(word: str) -> int:
    s = str(word or "").strip()
    if not s:
        return -1
    c = ord(s[-1])
    return c - 0xAC00 if 0xAC00 <= c <= 0xD7A3 else -1


def has_batchim(word: str) -> bool | None:
    h = last_hangul(word)
    return None if h < 0 else h % 28 != 0


def obj_particle(word: str) -> str:
    b = has_batchim(word)
    return "을(를)" if b is None else ("을" if b else "를")


def subj_particle(word: str) -> str:
    b = has_batchim(word)
    return "이(가)" if b is None else ("이" if b else "가")


def dir_particle(word: str) -> str:
    h = last_hangul(word)
    if h < 0:
        return "(으)로"
    jong = h % 28
    return "로" if jong in (0, 8) else "으로"


def copula(word: str) -> str:
    """'이에요' / '예요' (not in the web app; used for spoken confirmations)."""
    b = has_batchim(word)
    return "이에요" if b in (True, None) else "예요"


def format_price(n: int) -> str:
    return f"{n:,}원"


class NeulbabLogic:
    def __init__(
        self,
        data: NeulbabData,
        status_times: tuple[int, ...] = STATUS_TIMES_SEC,
        tz: tzinfo | None = None,
    ):
        self.data = data
        self.status_times = tuple(status_times)
        self.tz = tz

    # ---- time
    def _dt(self, ts_ms: int) -> datetime:
        return datetime.fromtimestamp(ts_ms / 1000, tz=self.tz)

    def format_time(self, ts: int) -> str:
        d = self._dt(ts)
        h, m = d.hour, d.minute
        return f"{'오전' if h < 12 else '오후'} {h % 12 or 12}시" + (f" {m}분" if m else "")

    @staticmethod
    def _part(h: int) -> str:
        return (
            "새벽"
            if h < 6
            else "오전"
            if h < 12
            else "낮"
            if h < 14
            else "오후"
            if h < 18
            else "밤"
        )

    def clock_text(self, ts: int) -> str:
        d = self._dt(ts)
        return f"{self._part(d.hour)} {d.hour % 12 or 12}시" + (
            f" {d.minute}분" if d.minute else ""
        )

    def when_text(self, ts: int) -> str:
        d = self._dt(ts)
        return f"{d.month}월 {d.day}일 " + self.clock_text(ts)

    def meal_word(self, ts: int) -> str:
        h = self._dt(ts).hour
        if 5 <= h < 10:
            return "아침"
        if 10 <= h < 15:
            return "점심"
        if 15 <= h < 21:
            return "저녁"
        return "식사"

    def eta_text(self, order: dict) -> str:
        return self.format_time(order["createdAt"] + self.status_times[-1] * 1000)

    # ---- people
    @staticmethod
    def parent_honorific(settings: dict | None) -> str:
        if not settings:
            return "부모님"
        name = str(settings.get("parentName") or "").strip()
        mapping = {
            "어머니": "어머님",
            "아버지": "아버님",
            "할머니": "할머님",
            "할아버지": "할아버님",
        }
        if not name:
            return "부모님"
        if name in mapping:
            return mapping[name]
        return name if name.endswith("님") else name + "님"

    @staticmethod
    def guardian_title(settings: dict | None) -> str:
        t = settings.get("guardianTitle") if settings else None
        return t if t in GUARDIAN_TITLES else DEFAULT_GUARDIAN_TITLE

    # ---- filtering & health
    @staticmethod
    def tags_of(menu: dict) -> list[str]:
        return menu.get("nutritionTags") or menu.get("nutrition_tags") or []

    @staticmethod
    def pref_categories(settings: dict | None) -> list[str]:
        if not settings:
            return []
        return settings.get("preferredCategories") or settings.get("categories") or []

    @staticmethod
    def health_conditions_of(settings: dict | None) -> list[str]:
        c = settings.get("healthConditions") if settings else None
        return c if isinstance(c, list) else []

    def health_check(self, menu: dict, condition_ids: list[str]) -> dict:
        entry = self.data.nutrition.get("menus", {}).get(menu["id"], {})
        tags = self.tags_of(menu)
        items = []
        for cid in condition_ids or []:
            c = self.data.condition(cid)
            if not c:
                continue
            for key, lim in (c.get("limits") or {}).items():
                v = entry.get(key)
                if not isinstance(v, (int, float)) or isinstance(v, bool):
                    result = "unknown"
                elif (isinstance(lim.get("max"), (int, float)) and v > lim["max"]) or (
                    isinstance(lim.get("min"), (int, float)) and v < lim["min"]
                ):
                    result = "over"
                else:
                    result = "ok"
                items.append({"conditionId": cid, "key": key, "value": v, "result": result})
            for t in c.get("requireTags") or []:
                items.append(
                    {
                        "conditionId": cid,
                        "key": t,
                        "value": t if t in tags else None,
                        "result": "ok" if t in tags else "missingTag",
                    }
                )
        return {"ok": all(it["result"] == "ok" for it in items), "items": items}

    def meets_health(self, menu: dict, condition_ids: list[str]) -> bool:
        return not condition_ids or self.health_check(menu, condition_ids)["ok"]

    def passes_filter(self, menu: dict, settings: dict | None) -> bool:
        return self.filter_failure(menu, settings) is None

    def filter_failure(self, menu: dict, settings: dict | None) -> str | None:
        """Why ``passes_filter`` is False, as an easy sentence (None if it passes)."""
        if menu.get("available") is False:
            return "지금은 팔지 않는 메뉴예요"
        allergies = (settings or {}).get("allergies") or []
        hit = [a for a in menu.get("allergens") or [] if a in allergies]
        if hit:
            return f"드시면 안 되는 재료({', '.join(hit)})가 들어 있어요"
        tags = self.tags_of(menu)
        missing = [t for t in (settings or {}).get("requiredTags") or [] if t not in tags]
        if missing:
            label = self.tag_label(missing[0])
            return f"꼭 지켜야 하는 식단({label})에 맞지 않아요"
        ids = self.health_conditions_of(settings)
        if not self.meets_health(menu, ids):
            bad = [r for r in self.health_result_lines(menu, ids) if r["result"] != "ok"]
            return bad[0]["text"] if bad else "건강 조건에 맞지 않아요"
        return None

    def health_note_text(self, settings: dict | None) -> str:
        ids = [i for i in self.health_conditions_of(settings) if self.data.condition(i)]
        if not ids:
            return ""
        if len(ids) == 1:
            return self.data.condition(ids[0])["note"]
        return "건강을 생각해서 골랐어요"

    @staticmethod
    def nutri_text(key: str, v: Any) -> str:
        if not isinstance(v, (int, float)) or isinstance(v, bool):
            return "모름"
        if key == "kcal":
            return f"{js_round(v)}kcal"
        if re.search(r"_mg$", key):
            return f"{js_round(v)}mg"
        return f"{js_round(v * 10) / 10:.1f}g"

    def nutrition_rows(self, menu_id: str) -> list[dict]:
        e = self.data.nutrition.get("menus", {}).get(menu_id, {})
        return [
            {"key": k, "label": NUTRI_LABEL[k], "text": self.nutri_text(k, e.get(k))}
            for k in NUTRI_KEYS
        ]

    def health_result_lines(self, menu: dict, condition_ids: list[str]) -> list[dict]:
        chk = self.health_check(menu, condition_ids)
        out = []
        for cid in condition_ids or []:
            c = self.data.condition(cid)
            if not c:
                continue
            mine = [it for it in chk["items"] if it["conditionId"] == cid]
            bad = next(
                (
                    it
                    for kind in ("over", "unknown", "missingTag")
                    for it in mine
                    if it["result"] == kind
                ),
                None,
            )
            result = bad["result"] if bad else "ok"
            if result == "missingTag":
                lbl = self.tag_label(bad["key"])
                text = f"{lbl}{subj_particle(lbl)} 아니라서 빼 두었어요"
            else:
                text = HEALTH_RESULT_TEXT[result]
            out.append(
                {
                    "conditionId": cid,
                    "label": c["label"],
                    "result": result,
                    "text": f"{c['label']}: {text}",
                }
            )
        return out

    # ---- labels
    @staticmethod
    def category_label(category: str) -> str:
        return CATEGORY_LABEL.get(category, category)

    @staticmethod
    def tag_label(tag: str) -> str:
        return TAG_LABEL.get(tag, tag)

    @staticmethod
    def payment_label(value: str) -> str:
        return PAYMENT_LABEL.get(value, value)

    # ---- recommendation
    @staticmethod
    def is_cancelled(order: dict | None) -> bool:
        return bool(order and (order.get("cancelled") or order.get("cancelledAt")))

    def recent_orders(self, orders: list[dict], now: int) -> list[dict]:
        return [
            o
            for o in orders or []
            if not self.is_cancelled(o) and now - o["createdAt"] <= RECENT_MS
        ]

    def order_category(self, order: dict) -> str | None:
        if order.get("category"):
            return order["category"]
        m = self.data.menu(order.get("menuId"))
        return m["category"] if m else None

    def score_menu(self, menu: dict, settings: dict, orders: list[dict], now: int) -> int:
        score = 0
        if menu["category"] in self.pref_categories(settings):
            score += 3
        required = settings.get("requiredTags") or []
        preferred = settings.get("preferredTags") or []
        for t in self.tags_of(menu):
            if t in preferred and t not in required:
                score += 1
        recent = self.recent_orders(orders, now)
        if any(o.get("menuId") == menu["id"] for o in recent):
            score -= 5
        if any(self.order_category(o) == menu["category"] for o in recent):
            score -= 1
        return score

    def rank_menus(self, settings: dict, orders: list[dict], now: int) -> list[dict]:
        ranked = [
            (m, self.score_menu(m, settings, orders, now))
            for m in self.data.menus
            if self.passes_filter(m, settings)
        ]
        ranked.sort(key=lambda x: (-x[1], x[0]["price"], x[0]["id"]))
        return [m for m, _ in ranked]

    def recommend(self, settings: dict | None, orders: list[dict], now: int) -> list[dict]:
        if not settings:
            return []
        return self.rank_menus(settings, orders, now)[:MAX_CARDS]

    def more_menus(self, settings: dict | None, orders: list[dict], now: int) -> list[dict]:
        if not settings:
            return []
        ranked = self.rank_menus(settings, orders, now)
        shown = {m["id"] for m in ranked[:MAX_CARDS]}
        return [m for m in ranked if m["id"] not in shown][:MAX_MORE]

    def reason_for(self, menu: dict, settings: dict | None) -> str:
        tags = self.tags_of(menu)
        if menu["category"] in self.pref_categories(settings):
            return f"좋아하시는 {self.category_label(menu['category'])} 음식이에요"
        if "저염" in tags:
            return "싱겁게 만든 음식이에요"
        if "부드러운음식" in tags:
            return "부드러워서 드시기 편해요"
        return "자녀분이 고른 조건에 맞아요"

    # ---- order status
    def get_status(self, order: dict | None, now: int) -> str | None:
        if not order:
            return None
        if self.is_cancelled(order):
            return "cancelled"
        elapsed = (now - order["createdAt"]) / 1000
        status = STATUS_KEYS[0]
        for key, t in zip(STATUS_KEYS, self.status_times, strict=True):
            if elapsed >= t:
                status = key
        return status

    def can_cancel(self, order: dict | None, now: int) -> bool:
        return self.get_status(order, now) == "received"

    def is_active(self, order: dict | None) -> bool:
        return bool(order) and not self.is_cancelled(order) and not order.get("acknowledgedAt")

    @staticmethod
    def ordered_by_of(order: dict | None) -> str:
        return "guardian" if order and order.get("orderedBy") == "guardian" else "parent"

    def can_guardian_order(self, orders: list[dict], now: int) -> bool:
        last = orders[-1] if orders else None
        if not last or self.is_cancelled(last) or last.get("acknowledgedAt"):
            return True
        return self.get_status(last, now) == "arrived"

    def ordered_by_text(self, order: dict, settings: dict | None, side: str) -> str:
        who = self.ordered_by_of(order)
        name = order["menuName"]
        if side == "guardian":
            if who == "guardian":
                return "내가 대신 주문했어요"
            p = self.parent_honorific(settings)
            return f"{p}{subj_particle(p)} {name}{obj_particle(name)} 주문하셨어요"
        if who == "parent":
            return "직접 주문하셨어요"
        g = self.guardian_title(settings)
        return f"{g}{subj_particle(g)} 주문했어요"

    def proxy_notice_text(self, order: dict, settings: dict | None) -> str:
        g = self.guardian_title(settings)
        meal = self.meal_word(order["createdAt"])
        name = order["menuName"]
        return (
            f"{g}{subj_particle(g)} {meal}{dir_particle(meal)} {name}{obj_particle(name)} "
            f"주문했어요. {self.eta_text(order)}쯤 도착해요"
        )

    def notice_list(self, orders: list[dict], now: int, settings: dict | None) -> list[dict]:
        out = []
        honor = self.parent_honorific(settings)
        for o in orders or []:
            name = o["menuName"]
            first = (
                f"내가 {name}{obj_particle(name)} 대신 주문했어요"
                if self.ordered_by_of(o) == "guardian"
                else f"{honor}{subj_particle(honor)} {name}{obj_particle(name)} 주문하셨어요"
            )
            if o["createdAt"] <= now:
                out.append(
                    {"orderId": o["id"], "status": "received", "at": o["createdAt"], "text": first}
                )
            if self.is_cancelled(o):
                at = o.get("cancelledAt") or o["createdAt"]
                if at <= now:
                    out.append(
                        {
                            "orderId": o["id"],
                            "status": "cancelled",
                            "at": at,
                            "text": f"{name}{obj_particle(name)} 취소했어요",
                        }
                    )
                continue
            texts = {
                "cooking": f"{name}{obj_particle(name)} 만들고 있어요",
                "delivering": "배달 기사님이 가고 있어요",
                "arrived": f"{honor} 댁에 도착했어요",
            }
            for i in range(1, len(STATUS_KEYS)):
                at = o["createdAt"] + self.status_times[i] * 1000
                if at <= now:
                    key = STATUS_KEYS[i]
                    out.append({"orderId": o["id"], "status": key, "at": at, "text": texts[key]})
        out.sort(key=lambda n: -n["at"])  # stable, like Array.prototype.sort in modern JS
        return out

    def order_history(self, orders: list[dict], now: int) -> list[dict]:
        rows = []
        for o in sorted(orders or [], key=lambda o: -o["createdAt"]):
            st = self.get_status(o, now)
            rows.append(
                {
                    "order": o,
                    "status": st,
                    "statusLabel": "취소했어요" if st == "cancelled" else STATUS_LABELS[st],
                    "whenText": self.when_text(o["createdAt"]),
                }
            )
        return rows
