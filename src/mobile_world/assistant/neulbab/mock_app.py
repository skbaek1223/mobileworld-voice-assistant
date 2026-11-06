"""A headless 늘밥 app that implements the assistant tool contract (docs/neulbab/tools.json).

It stands in for the future Android app so the voice assistant can be developed and
evaluated now. One ``MockNeulbabApp`` holds the family's shared state (settings, orders);
each tool call names the role (``parent`` | ``guardian``) it is made as, so a parent's
order is immediately visible to the guardian, as on the web prototype's split screen.

Every tool returns the contract envelope: ``{"ok": true, "data": ..., "say": ...}`` or
``{"ok": false, "error": {"code", "message"}}``.
"""

from __future__ import annotations

import copy
import json
import secrets
import time
from collections.abc import Callable
from datetime import datetime, tzinfo
from pathlib import Path
from typing import Any

from loguru import logger

from mobile_world.assistant.neulbab.adapters import PlatformError, build_adapters
from mobile_world.assistant.neulbab.data import NeulbabData
from mobile_world.assistant.neulbab.logic import (
    CATEGORY_ORDER,
    GUARDIAN_TITLES,
    HEALTH_DISCLAIMER,
    STATUS_DESC,
    STATUS_LABELS,
    STATUS_TIMES_SEC,
    TAG_LIST,
    TEXT,
    NeulbabLogic,
    copula,
    dir_particle,
    format_price,
    obj_particle,
    subj_particle,
    topic_particle,
)

DEFAULT_CONTRACT = Path(__file__).resolve().parents[4] / "docs" / "neulbab" / "tools.json"
CONTRACT_VERSION_KEY = "version"
CONFIRMATION_TTL_SEC = 120
ROLES = ("parent", "guardian")
PARENT_SCREENS = ("home", "more_menus", "order_status")
GUARDIAN_SCREENS = ("home", "orders", "notifications", "my", "settings", "nutrition")
SETTING_FIELDS = {  # contract arg -> (settings key, spoken label)
    "health_conditions": ("healthConditions", "건강 상태"),
    "allergies": ("allergies", "드시면 안 되는 재료"),
    "preferred_categories": ("preferredCategories", "좋아하시는 음식 종류"),
    "required_tags": ("requiredTags", "꼭 지켜야 하는 식단"),
    "preferred_tags": ("preferredTags", "있으면 좋은 식단"),
    "platform": ("platform", "주문할 배달앱"),
    "guardian_title": ("guardianTitle", "부모님 화면에 보일 내 호칭"),
}


class ToolFailure(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _now_ms() -> int:
    return int(time.time() * 1000)


class MockNeulbabApp:
    def __init__(
        self,
        data: NeulbabData,
        contract_path: str | Path = DEFAULT_CONTRACT,
        clock: Callable[[], int] = _now_ms,
        tz: tzinfo | None = None,
        status_times: tuple[int, ...] = STATUS_TIMES_SEC,
        state_file: str | Path | None = None,
        with_sample_settings: bool = True,
    ):
        self.data = data
        self.logic = NeulbabLogic(data, status_times, tz)
        self.adapters = build_adapters(data, self.logic.status_times)
        self.contract = json.loads(Path(contract_path).read_text(encoding="utf-8"))
        self.errors: dict[str, str] = self.contract["errors"]
        self.tool_roles = {t["name"]: tuple(t["roles"]) for t in self.contract["tools"]}
        self.clock = clock
        self.tz = tz
        self.state_file = Path(state_file) if state_file else None
        self.confirmations: dict[str, dict[str, Any]] = {}
        self.screens: dict[str, str] = {"parent": "home", "guardian": "home"}

        self.settings: dict | None = None
        self.settings_rev = 0  # bumps on every settings save; stale confirmations are refused
        self.orders: list[dict] = []
        if self.state_file and self.state_file.exists():
            saved = json.loads(self.state_file.read_text(encoding="utf-8"))
            self.settings, self.orders = saved.get("settings"), saved.get("orders", [])
        elif with_sample_settings and data.sample_settings:
            self.settings = self._normalize_settings(data.sample_settings, self.clock())

        missing = set(self.tool_roles) - set(self._handlers())
        if missing:
            raise RuntimeError(f"Mock app does not implement contract tools: {sorted(missing)}")

    # ------------------------------------------------------------------ plumbing
    def _handlers(self) -> dict[str, Callable[..., dict]]:
        return {
            name[5:]: getattr(self, name)
            for name in dir(self)
            if name.startswith("tool_") and callable(getattr(self, name))
        }

    def call(self, role: str, name: str, args: dict[str, Any] | None = None) -> dict:
        """Run one contract tool as ``role``. Always returns an envelope, never raises."""
        args = dict(args or {})
        try:
            if role not in ROLES:
                raise ToolFailure("NOT_LOGGED_IN")
            if name not in self.tool_roles:
                raise ToolFailure("INVALID_ARGUMENT")
            if role not in self.tool_roles[name]:
                raise ToolFailure("WRONG_ROLE")
            data, say = self._handlers()[name](role, **args)
        except ToolFailure as f:
            return {"ok": False, "error": {"code": f.code, "message": self.errors[f.code]}}
        except TypeError as e:  # unexpected/missing argument names
            logger.warning(f"[neulbab] bad arguments for {name}: {e}")
            return self._fail("INVALID_ARGUMENT")
        out: dict[str, Any] = {"ok": True, "data": data}
        if say:
            out["say"] = say
        return out

    def _fail(self, code: str) -> dict:
        return {"ok": False, "error": {"code": code, "message": self.errors[code]}}

    def _save(self) -> None:
        if not self.state_file:
            return
        try:
            self.state_file.write_text(
                json.dumps({"settings": self.settings, "orders": self.orders}, ensure_ascii=False),
                encoding="utf-8",
            )
        except OSError as e:
            raise ToolFailure("STORAGE_ERROR") from e

    def _need_settings(self) -> dict:
        if not self.settings:
            raise ToolFailure("NO_SETTINGS")
        return self.settings

    def _menu(self, menu_id: str) -> dict:
        m = self.data.menu(menu_id)
        if not m:
            raise ToolFailure("MENU_NOT_FOUND")
        return m

    def _latest(self) -> dict:
        if not self.orders:
            raise ToolFailure("NO_ORDER")
        return self.orders[-1]

    def _active(self) -> dict | None:
        last = self.orders[-1] if self.orders else None
        return last if self.logic.is_active(last) else None

    def _platform_label(self, pid: str | None) -> str:
        p = self.data.platform(pid)
        return p["label"] if p else ""

    def _normalize_settings(self, s: dict, now: int) -> dict:
        """Same normalization as the web's saveSettings()."""
        platform = s.get("platform")
        if not self.data.platform(platform):
            platform = self.data.platforms[0]["id"]
        title = s.get("guardianTitle")
        return {
            "parentName": str(s.get("parentName") or "").strip() or "어머니",
            "guardianTitle": title if title in GUARDIAN_TITLES else "자녀분",
            "address": str(s.get("address") or "").strip(),
            "payment": s.get("payment"),
            "preferredCategories": list(s.get("preferredCategories") or []),
            "allergies": list(s.get("allergies") or []),
            "requiredTags": list(s.get("requiredTags") or []),
            "preferredTags": list(s.get("preferredTags") or []),
            "healthConditions": [
                c for c in s.get("healthConditions") or [] if self.data.condition(c)
            ],
            "platform": platform,
            "familyCode": s.get("familyCode")
            or str(self.data.demo_accounts.get("parent", {}).get("familyCode", "")),
            "updatedAt": now,
        }

    def _issue(self, role: str, kind: str, payload: dict) -> tuple[str, int]:
        cid = "c-" + secrets.token_hex(4)
        self.confirmations[cid] = {
            "role": role,
            "kind": kind,
            "payload": payload,
            "expires": self.clock() + CONFIRMATION_TTL_SEC * 1000,
        }
        return cid, CONFIRMATION_TTL_SEC

    def _redeem(self, role: str, kind: str, confirmation_id: str) -> dict:
        c = self.confirmations.pop(str(confirmation_id), None)  # single use, even on failure
        if not c or c["role"] != role or c["kind"] != kind or self.clock() > c["expires"]:
            raise ToolFailure("CONFIRMATION_INVALID")
        return c["payload"]

    # ------------------------------------------------------------------ views
    def _menu_card(self, m: dict) -> dict:
        return {
            "menu_id": m["id"],
            "name": m["name"],
            "price": m["price"],
            "price_text": format_price(m["price"]),
            "category_label": self.logic.category_label(m["category"]),
            # facts the assistant may use to describe a menu (contract v0.3)
            "tags": [self.logic.tag_label(t) for t in self.logic.tags_of(m)],
            "description": m.get("description", ""),
            "reason": self.logic.reason_for(m, self.settings),
        }

    def _check_orderable(self, role: str, menu: dict, settings: dict) -> None:
        if not self.logic.passes_filter(menu, settings):
            raise ToolFailure("MENU_NOT_ALLOWED")
        now = self.clock()
        if role == "parent" and self._active():
            raise ToolFailure("ACTIVE_ORDER_EXISTS")
        if role == "guardian" and not self.logic.can_guardian_order(self.orders, now):
            raise ToolFailure("GUARDIAN_BLOCKED")

    # ------------------------------------------------------------------ tools
    def tool_get_context(self, role: str) -> tuple[dict, str | None]:
        s = self.settings
        active = self._active()
        conds = [self.data.condition(c)["label"] for c in self.logic.health_conditions_of(s)]
        now = datetime.fromtimestamp(self.clock() / 1000, tz=self.tz)
        return {
            "role": role,
            "parent_name": self.data.demo_accounts.get("parent", {}).get("name", ""),
            "parent_honorific": self.logic.parent_honorific(s),
            "guardian_title": self.logic.guardian_title(s),
            "has_settings": bool(s),
            "active_order_id": active["id"] if active else None,
            "health_conditions": conds,
            "now": now.isoformat(timespec="seconds"),
            "contract_version": self.contract.get(CONTRACT_VERSION_KEY),
        }, None

    def tool_get_recommendations(
        self, role: str, include_more: bool = False
    ) -> tuple[dict, str | None]:
        s = self._need_settings()
        now = self.clock()
        menus = [self._menu_card(m) for m in self.logic.recommend(s, self.orders, now)]
        data: dict[str, Any] = {
            "menus": menus,
            "health_note": self.logic.health_note_text(s),
            "disclaimer": HEALTH_DISCLAIMER if self.logic.health_conditions_of(s) else "",
        }
        if include_more:
            data["more_menus"] = [
                self._menu_card(m) for m in self.logic.more_menus(s, self.orders, now)
            ]
        if not menus:
            return data, TEXT["noMenu"] if role == "parent" else TEXT["noGuardianMenu"]
        names = [m["name"] for m in menus]
        say = f"{', '.join(names)}{subj_particle(names[-1])} 있어요"
        if data["health_note"]:
            say = f"{data['health_note']}. {say}"
        return data, say

    def tool_get_menu_detail(self, role: str, menu_id: str) -> tuple[dict, str | None]:
        m = self._menu(menu_id)
        s = self.settings
        reason = self.logic.filter_failure(m, s) if s else "아직 자녀분이 설정을 하지 않았어요"
        data: dict[str, Any] = {
            "menu_id": m["id"],
            "name": m["name"],
            "description": m.get("description", ""),
            "price_text": format_price(m["price"]),
            "orderable": reason is None,
            "not_orderable_reason": reason,
        }
        if role == "guardian":
            ids = self.logic.health_conditions_of(s)
            entry = self.data.nutrition.get("menus", {}).get(m["id"], {})
            data["nutrition"] = [
                {"label": r["label"], "value_text": r["text"]}
                for r in self.logic.nutrition_rows(m["id"])
            ]
            data["health_check"] = [
                {"condition_label": r["label"], "result": r["text"].split(": ", 1)[1]}
                for r in self.logic.health_result_lines(m, ids)
            ]
            data["sources"] = [
                {"name": src["name"], "url": src["url"]} for src in entry.get("sources", [])
            ]
            data["disclaimer"] = HEALTH_DISCLAIMER
        price = data["price_text"]
        say = f"{m['name']}{copula(m['name'])}. {m.get('description', '')}. {price}{copula(price)}"
        if reason:
            say += f". {reason}"
        return data, say

    def tool_prepare_order(self, role: str, menu_id: str) -> tuple[dict, str | None]:
        s = self._need_settings()
        m = self._menu(menu_id)
        self._check_orderable(role, m, s)
        price = format_price(m["price"])
        if role == "parent":
            question = f"{m['name']}, {price}{copula(price)}. {TEXT['payParent']}. 주문할까요?"
        else:
            p = self.logic.parent_honorific(s)
            pay = self.logic.payment_label(s.get("payment", ""))
            question = (
                f"{p}께 {m['name']}{obj_particle(m['name'])} 대신 주문할게요. "
                f"{price}{copula(price)}. {pay}로 결제돼요. 주문할까요?"
            )
        cid, ttl = self._issue(role, "order", {"menu_id": m["id"]})
        return {
            "confirmation_id": cid,
            "expires_in_sec": ttl,
            "menu_name": m["name"],
            "price_text": price,
            "confirm_question": question,
        }, None

    def tool_place_order(self, role: str, confirmation_id: str) -> tuple[dict, str | None]:
        payload = self._redeem(role, "order", confirmation_id)
        s = self._need_settings()
        m = self._menu(payload["menu_id"])
        self._check_orderable(role, m, s)  # state may have changed since prepare
        now = self.clock()
        platform = self.data.platform(s.get("platform")) or self.data.platforms[0]
        try:
            placed = self.adapters[platform["id"]].create_order(m["id"], now)
        except (PlatformError, KeyError) as e:
            logger.warning(f"[neulbab] platform error: {e}")
            raise ToolFailure("PLATFORM_ERROR") from e
        order = {
            "id": f"o{now}",
            "menuId": m["id"],
            "menuName": m["name"],
            "category": m["category"],
            "price": m["price"],
            "address": s.get("address", ""),
            "payment": s.get("payment", ""),
            "orderedBy": role,
            "platform": platform["id"],
            "platformOrderId": str(placed["platformOrderId"]),
            "createdAt": now,
            "cancelled": False,
            "cancelledAt": None,
            "acknowledgedAt": None,
        }
        self.orders.append(order)
        self._save()
        eta = self.logic.eta_text(order)
        name = m["name"]
        if role == "parent":
            g = self.logic.guardian_title(s)
            say = f"{name}{obj_particle(name)} 주문했어요. {eta}쯤 도착해요. {g}께 알림이 가요"
        else:
            p = self.logic.parent_honorific(s)
            say = (
                f"{p}께 {name}{obj_particle(name)} 대신 주문했어요. {eta}쯤 도착해요. "
                f"{p} 화면에도 알림이 가요"
            )
        return {
            "order_id": order["id"],
            "menu_name": name,
            "status": "received",
            "eta_text": eta,
            "platform_label": platform["label"],
        }, say

    def tool_get_order_status(self, role: str) -> tuple[dict, str | None]:
        o = self._latest()
        now = self.clock()
        st = self.logic.get_status(o, now)
        by_text = self.logic.ordered_by_text(o, self.settings, role)
        data = {
            "order_id": o["id"],
            "menu_name": o["menuName"],
            "status": st,
            "status_label": STATUS_LABELS[st],
            "status_desc": STATUS_DESC[st],
            "ordered_by": self.logic.ordered_by_of(o),
            "ordered_by_text": by_text,
            "eta_text": self.logic.eta_text(o),
            "minutes_left": self.logic.minutes_left(o, now),
            "remaining_text": self.logic.remaining_text(o, now),
            "can_cancel": role == "parent" and self.logic.can_cancel(o, now),
            "acknowledged": bool(o.get("acknowledgedAt")),
            "platform_label": self._platform_label(o.get("platform")),
            # when it was ordered, and whether it is already over (v0.4): an old, received
            # order must not be read out as if it had just arrived
            "ordered_at_text": self.logic.when_text(o["createdAt"]),
            "ordered_day": self.logic.day_text(o["createdAt"], now),
            "finished": bool(o.get("acknowledgedAt")) or st == "cancelled",
        }
        name = o["menuName"]
        day = data["ordered_day"]
        if o.get("acknowledgedAt"):
            say = f"{day} 주문하신 {name}{topic_particle(name)} 이미 받으셨어요"
        elif st == "cancelled":
            say = f"{name} 주문은 취소됐어요"
        elif st == "arrived":
            say = f"{name}{subj_particle(name)} 도착했어요. {STATUS_DESC[st]}"
        else:
            say = f"{name}{copula(name)}. 지금은 {STATUS_LABELS[st]} 단계예요. {STATUS_DESC[st]}. {data['remaining_text']}"
        return data, say

    def tool_cancel_order(self, role: str, order_id: str) -> tuple[dict, str | None]:
        o = self._latest()
        if o["id"] != order_id:
            raise ToolFailure("ORDER_MISMATCH")
        now = self.clock()
        if not self.logic.can_cancel(o, now):
            raise ToolFailure("CANNOT_CANCEL")
        adapter = self.adapters.get(o.get("platform"))
        if adapter is None:
            raise ToolFailure("PLATFORM_ERROR")
        if not adapter.cancel_order(o, now):
            raise ToolFailure("CANNOT_CANCEL")
        o["cancelled"], o["cancelledAt"] = True, now
        self._save()
        return {"order_id": o["id"], "status": "cancelled"}, (
            f"주문을 취소했어요. {TEXT['cancelledParent']}"
        )

    def tool_acknowledge_arrival(self, role: str, order_id: str) -> tuple[dict, str | None]:
        o = self._latest()
        if o["id"] != order_id:
            raise ToolFailure("ORDER_MISMATCH")
        if self.logic.get_status(o, self.clock()) != "arrived":
            raise ToolFailure("NOT_ARRIVED")
        o["acknowledgedAt"] = self.clock()
        self._save()
        return {
            "order_id": o["id"],
            "acknowledged": True,
        }, "잘 받으셨다고 표시했어요. 맛있게 드세요"

    def tool_list_orders(self, role: str, limit: int = 5) -> tuple[dict, str | None]:
        rows = self.logic.order_history(self.orders, self.clock())[: max(1, min(int(limit), 20))]
        orders = [
            {
                "order_id": r["order"]["id"],
                "menu_name": r["order"]["menuName"],
                "price_text": format_price(r["order"]["price"]),
                "status_label": r["statusLabel"],
                "ordered_by_text": self.logic.ordered_by_text(
                    r["order"], self.settings, "guardian"
                ),
                "date_text": r["whenText"],
                "platform_label": self._platform_label(r["order"].get("platform")),
            }
            for r in rows
        ]
        say = "아직 주문이 없어요" if not orders else f"주문이 {len(orders)}건 있어요"
        return {"orders": orders}, say

    def tool_list_notifications(self, role: str, limit: int = 5) -> tuple[dict, str | None]:
        items = self.logic.notice_list(self.orders, self.clock(), self.settings)
        items = items[: max(1, min(int(limit), 20))]
        notes = [{"text": n["text"], "time_text": self.logic.clock_text(n["at"])} for n in items]
        return {"notifications": notes}, (notes[0]["text"] if notes else "아직 알림이 없어요")

    def _settings_view(self, s: dict) -> dict:
        p = self.data.platform(s.get("platform")) or self.data.platforms[0]
        return {
            "guardian_title": self.logic.guardian_title(s),
            "health_conditions": [
                {"id": c, "label": self.data.condition(c)["label"]}
                for c in self.logic.health_conditions_of(s)
            ],
            "allergies": list(s.get("allergies") or []),
            "preferred_categories": list(s.get("preferredCategories") or []),
            "required_tags": list(s.get("requiredTags") or []),
            "preferred_tags": list(s.get("preferredTags") or []),
            "platform": {"id": p["id"], "label": p["label"]},
            "payment_label": self.logic.payment_label(s.get("payment", "")),
            "has_address": bool(str(s.get("address") or "").strip()),
        }

    def tool_get_settings(self, role: str) -> tuple[dict, str | None]:
        s = self._need_settings()
        data = self._settings_view(s)
        data["options"] = {
            "health_conditions": [
                {"id": c["id"], "label": c["label"]} for c in self.data.conditions
            ],
            "allergies": self.data.allergens,
            "categories": list(CATEGORY_ORDER),
            "tags": list(TAG_LIST),
            "platforms": [{"id": p["id"], "label": p["label"]} for p in self.data.platforms],
            "guardian_titles": list(GUARDIAN_TITLES),
        }
        return data, None

    def _field_text(self, arg: str, value: Any) -> str:
        if arg == "health_conditions":
            labels = [self.data.condition(c)["label"] for c in value if self.data.condition(c)]
            return ", ".join(labels) or "없음"
        if arg == "platform":
            return self._platform_label(value) or str(value)
        if isinstance(value, list):
            return ", ".join(self.logic.tag_label(v) for v in value) or "없음"
        return str(value)

    def tool_prepare_settings_change(self, role: str, **changes: Any) -> tuple[dict, str | None]:
        s = self._need_settings()
        unknown = set(changes) - set(SETTING_FIELDS)
        if unknown or not changes:
            raise ToolFailure("INVALID_ARGUMENT")
        if any(a not in self.data.allergens for a in changes.get("allergies", [])):
            raise ToolFailure("INVALID_ARGUMENT")
        new = copy.deepcopy(s)
        diffs = []
        for arg, value in changes.items():
            key, label = SETTING_FIELDS[arg]
            before = s.get(key) if key != "guardianTitle" else self.logic.guardian_title(s)
            if isinstance(value, list):
                value = list(dict.fromkeys(value))  # drop duplicates, keep order
            if value == before:
                continue
            new[key] = value
            diffs.append(
                {
                    "field_label": label,
                    "before_text": self._field_text(arg, before or []),
                    "after_text": self._field_text(arg, value),
                }
            )
        if not diffs:
            raise ToolFailure("INVALID_ARGUMENT")
        menus_after = len(self.logic.rank_menus(new, self.orders, self.clock()))
        parts = [
            f"{d['field_label']}{obj_particle(d['field_label'])} {d['after_text']}{dir_particle(d['after_text'])} 바꿀게요"
            for d in diffs
        ]
        question = ". ".join(parts) + "."
        if menus_after == 0:
            question += " 바꾸면 고를 수 있는 메뉴가 없어요."
        question += " 바꿀까요?"
        cid, ttl = self._issue(role, "settings", {"settings": new, "base_rev": self.settings_rev})
        return {
            "confirmation_id": cid,
            "expires_in_sec": ttl,
            "changes": diffs,
            "confirm_question": question,
            "menus_after": menus_after,
        }, None

    def tool_apply_settings_change(
        self, role: str, confirmation_id: str
    ) -> tuple[dict, str | None]:
        payload = self._redeem(role, "settings", confirmation_id)
        s = self._need_settings()
        if self.settings_rev != payload["base_rev"]:
            raise ToolFailure("CONFIRMATION_INVALID")  # settings changed in between
        before = self._settings_view(s)
        self.settings = self._normalize_settings(payload["settings"], self.clock())
        self.settings_rev += 1
        self._save()
        after = self._settings_view(self.settings)
        changed = [
            label for arg, (_, label) in SETTING_FIELDS.items() if before.get(arg) != after.get(arg)
        ]
        say = "설정을 바꿨어요"
        note = self.logic.health_note_text(self.settings)
        if "건강 상태" in changed and note:
            say += f". 이제 {note}"
        return {"updated_fields": changed}, say

    def tool_open_screen(
        self, role: str, screen: str, menu_id: str | None = None
    ) -> tuple[dict, str | None]:
        allowed = PARENT_SCREENS if role == "parent" else GUARDIAN_SCREENS
        if screen not in PARENT_SCREENS + GUARDIAN_SCREENS:
            raise ToolFailure("INVALID_ARGUMENT")
        if screen not in allowed:
            raise ToolFailure("WRONG_ROLE")
        if menu_id is not None:
            self._menu(menu_id)
        self.screens[role] = screen
        return {"screen": screen}, None
