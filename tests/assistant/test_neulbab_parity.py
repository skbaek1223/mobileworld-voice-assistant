"""Python port (neulbab.logic) vs the original web prototype (mock_api.js) on random cases.

Needs the real 늘밥 app folder and Node.js:
    NEULBAB_APP_DIR=.../agent-company/product/app pytest tests/assistant/test_neulbab_parity.py
"""

import json
import os
import random
import shutil
import subprocess
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from mobile_world.assistant.neulbab.data import load_data
from mobile_world.assistant.neulbab.logic import (
    GUARDIAN_TITLES,
    TAG_LIST,
    NeulbabLogic,
    dir_particle,
    obj_particle,
    subj_particle,
)

APP_DIR = os.getenv("NEULBAB_APP_DIR")
pytestmark = pytest.mark.skipif(
    not APP_DIR or not shutil.which("node"), reason="needs NEULBAB_APP_DIR and node"
)
N_CASES = 300
NOW = 1_762_311_600_000  # 2025-11-05 12:00 KST
# every ending type: no batchim, ㄹ batchim, other batchim, non-Hangul, empty, spaces
WORDS = ["점심", "저녁", "아침", "서울", "물", "국", "죽", "전복죽", "가 배달앱", "나 배달앱",
         "혈압이 높아요", "자녀분", "따님", "보호자", "abc", "12", "", "  ", "미역국 정식 "]  # fmt: skip


def make_cases(data, seed=7):
    rng = random.Random(seed)
    cats = sorted({m["category"] for m in data.menus})
    cond_ids = [c["id"] for c in data.conditions] + ["unknown-condition"]
    names = ["어머니", "아버지", "할머니", "김순자", "이영희님", "", "  "]
    titles = [*GUARDIAN_TITLES, None, "엄마"]

    def subset(items, p):
        return [x for x in items if rng.random() < p]

    cases = []
    for _ in range(N_CASES):
        settings = {
            "parentName": rng.choice(names),
            "guardianTitle": rng.choice(titles),
            "preferredCategories": subset(cats, 0.3),
            "allergies": subset(data.allergens, 0.2),
            "requiredTags": subset(TAG_LIST, 0.15),
            "preferredTags": subset(TAG_LIST, 0.3),
            "healthConditions": subset(cond_ids, 0.25),
            "payment": "가상 카드 A",
            "address": "x",
        }
        now = NOW + rng.randrange(0, 24) * 3_600_000 + rng.randrange(0, 60) * 60_000
        orders = []
        for i in range(rng.randrange(0, 4)):
            m = rng.choice(data.menus)
            created = now - rng.choice(
                [rng.randrange(0, 40_000), rng.randrange(0, 5 * 24 * 3_600_000)]
            )
            o = {
                "id": f"o{i}",
                "menuId": m["id"],
                "menuName": m["name"],
                "price": m["price"],
                "orderedBy": rng.choice(["parent", "guardian"]),
                "createdAt": created,
                "cancelled": rng.random() < 0.2,
                "cancelledAt": None,
                "acknowledgedAt": created + 31_000 if rng.random() < 0.3 else None,
            }
            if o["cancelled"]:
                o["cancelledAt"] = created + rng.randrange(0, 9_000)
            if rng.random() < 0.7:
                o["category"] = m["category"]
            orders.append(o)
        orders.sort(key=lambda o: o["createdAt"])
        cases.append({"settings": settings, "orders": orders, "now": now})
    return cases


def python_side(lg: NeulbabLogic, c):
    s, orders, now = c["settings"], c["orders"], c["now"]
    ids = lg.health_conditions_of(s)
    rec, more = lg.recommend(s, orders, now), lg.more_menus(s, orders, now)
    return {
        "recommend": [m["id"] for m in rec],
        "more": [m["id"] for m in more],
        "reasons": [lg.reason_for(m, s) for m in rec + more],
        "healthNote": lg.health_note_text(s),
        "parentHonorific": lg.parent_honorific(s),
        "guardianTitle": lg.guardian_title(s),
        "menus": [
            {
                "id": m["id"],
                "passes": lg.passes_filter(m, s),
                "health": [r["text"] for r in lg.health_result_lines(m, ids)],
                "nutrition": [r["text"] for r in lg.nutrition_rows(m["id"])],
            }
            for m in lg.data.menus
        ],
        "orders": [
            {
                "status": lg.get_status(o, now),
                "byGuardian": lg.ordered_by_text(o, s, "guardian"),
                "byParent": lg.ordered_by_text(o, s, "parent"),
                "proxy": lg.proxy_notice_text(o, s),
                "eta": lg.eta_text(o),
                "time": lg.format_time(o["createdAt"]),
                "meal": lg.meal_word(o["createdAt"]),
            }
            for o in orders
        ],
        "canGuardianOrder": lg.can_guardian_order(orders, now),
        "notices": [[n["orderId"], n["status"], n["at"], n["text"]] for n in lg.notice_list(orders, now, s)],
        "history": [[h["order"]["id"], h["statusLabel"], h["whenText"]] for h in lg.order_history(orders, now)],
    }  # fmt: skip


def test_python_port_matches_web_prototype(tmp_path):
    data = load_data(APP_DIR)
    cases = make_cases(data)
    case_file = tmp_path / "cases.json"
    case_file.write_text(json.dumps(cases, ensure_ascii=False), encoding="utf-8")
    script = Path(__file__).with_name("neulbab_parity.js")
    proc = subprocess.run(
        ["node", str(script), APP_DIR, str(case_file), json.dumps(WORDS, ensure_ascii=False)],
        capture_output=True,
        text=True,
        env={**os.environ, "TZ": "Asia/Seoul"},
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    js = out["cases"]
    py_particles = [[obj_particle(w), subj_particle(w), dir_particle(w)] for w in WORDS]
    assert py_particles == out["particles"]

    lg = NeulbabLogic(data, tz=ZoneInfo("Asia/Seoul"))
    mismatches = []
    for i, (case, expected) in enumerate(zip(cases, js, strict=True)):
        got = python_side(lg, case)
        for key in expected:
            if got[key] != expected[key]:
                mismatches.append((i, key, expected[key], got[key]))
    assert not mismatches, "\n".join(map(str, mismatches[:10]))

    # the random cases must actually exercise the interesting branches
    flat = [j for j in js]
    assert sum(bool(c["recommend"]) for c in flat) > N_CASES * 0.5
    assert any(not c["recommend"] for c in flat)
    assert any(c["healthNote"] for c in flat)
    statuses = {o["status"] for c in flat for o in c["orders"]}
    assert statuses == {"received", "cooking", "delivering", "arrived", "cancelled"}
