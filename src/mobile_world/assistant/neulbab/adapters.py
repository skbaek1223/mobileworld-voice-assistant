"""Mock delivery-platform adapters (ported from the web prototype's adapters/pa|pb|pc.js).

Each mock platform answers in its own "raw" format; the adapter converts to 늘밥's shape.
That is the point of the design: a real integration only replaces the adapter.
Common interface: create_order(req) -> {"platformOrderId"}; cancel_order(order, now) -> bool.
"""

from __future__ import annotations

import numpy as np

from mobile_world.assistant.neulbab.data import NeulbabData


class PlatformError(Exception):
    pass


def _step_index(created_at: int, now: int, times: tuple[int, ...]) -> int:
    elapsed = (now - created_at) / 1000
    idx = 0
    for i, t in enumerate(times):
        if elapsed >= t:
            idx = i
    return idx


def _base36(n: int) -> str:
    return np.base_repr(n, 36).lower()


class Adapter:
    id = ""

    def __init__(self, data: NeulbabData, times: tuple[int, ...]):
        self.data = data
        self.times = times

    def _sellable(self, menu_id: str) -> bool:
        m = self.data.menu(menu_id)
        return bool(m and m.get("available") is not False)

    # raw platform calls, overridden per platform
    def _raw_place(self, menu_id: str, created_at: int) -> dict: ...
    def _raw_cancel(self, ref: str, created_at: int, now: int) -> dict: ...
    def _order_id(self, raw: dict) -> str: ...
    def _cancel_ok(self, raw: dict) -> bool: ...

    def create_order(self, menu_id: str, created_at: int) -> dict:
        return {"platformOrderId": self._order_id(self._raw_place(menu_id, created_at))}

    def cancel_order(self, order: dict, now: int) -> bool:
        if order.get("cancelled") or order.get("cancelledAt"):
            return False
        return self._cancel_ok(self._raw_cancel(order["platformOrderId"], order["createdAt"], now))


class PaAdapter(Adapter):
    """가 배달앱: exceptions on error, order numbers like 'PA-<ms>', results 'OK'/'REJECTED'."""

    id = "pa"

    def _raw_place(self, menu_id, created_at):
        if not self._sellable(menu_id):
            raise PlatformError(f"가 배달앱: 팔지 않는 메뉴예요 {menu_id}")
        return {"orderNo": f"PA-{created_at}"}

    def _order_id(self, raw):
        return raw["orderNo"]

    def _raw_cancel(self, ref, created_at, now):
        ok = _step_index(created_at, now, self.times) == 0
        return {"result": "OK" if ok else "REJECTED"}

    def _cancel_ok(self, raw):
        return raw["result"] == "OK"


class PbAdapter(Adapter):
    """나 배달앱: HTTP-like codes, numeric order ids."""

    id = "pb"

    def _raw_place(self, menu_id, created_at):
        if not self._sellable(menu_id):
            return {"code": 409, "msg": "sold out"}
        return {"code": 200, "order_id": created_at % 1_000_000_000}

    def _order_id(self, raw):
        if raw.get("code") != 200:
            raise PlatformError(f"나 배달앱: 주문 실패 ({raw.get('code')})")
        return str(raw["order_id"])

    def _raw_cancel(self, ref, created_at, now):
        ok = _step_index(created_at, now, self.times) == 0
        return {"code": 200 if ok else 409}

    def _cancel_ok(self, raw):
        return raw["code"] == 200


class PcAdapter(Adapter):
    """다 배달앱: {ok, result|error} envelopes, base-36 references."""

    id = "pc"

    def _raw_place(self, menu_id, created_at):
        if not self._sellable(menu_id):
            return {"ok": False, "error": {"reason": "unavailable_item"}}
        return {"ok": True, "result": {"reference": "nc_" + _base36(created_at)}}

    def _order_id(self, raw):
        if not raw.get("ok"):
            raise PlatformError(f"다 배달앱: {raw.get('error', {}).get('reason')}")
        return raw["result"]["reference"]

    def _raw_cancel(self, ref, created_at, now):
        ok = _step_index(created_at, now, self.times) == 0
        return {"ok": ok, "result": {"voided": True} if ok else None}

    def _cancel_ok(self, raw):
        return raw["ok"] is True


ADAPTER_CLASSES = {cls.id: cls for cls in (PaAdapter, PbAdapter, PcAdapter)}


def build_adapters(data: NeulbabData, times: tuple[int, ...]) -> dict[str, Adapter]:
    return {
        p["id"]: ADAPTER_CLASSES[p["id"]](data, times)
        for p in data.platforms
        if p["id"] in ADAPTER_CLASSES
    }
