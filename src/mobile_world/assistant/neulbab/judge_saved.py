"""Judge saved runs (results.jsonl) with an LLM: one verdict per conversation, pass/border/fail.

    python -m mobile_world.assistant.neulbab.judge_saved RESULTS_DIR TASKS_JSON \
        --model gpt-5-mini --effort medium
Writes RESULTS_DIR/verdicts.jsonl and prints a table by category.
"""

from __future__ import annotations

import argparse
import json
import os
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from mobile_world.assistant.neulbab import load_data
from mobile_world.assistant.neulbab.benchmark import LLMConfig, judge_verdict
from mobile_world.assistant.neulbab.evaluation import load_tasks
from mobile_world.assistant.neulbab.rescore import rebuild_orders


def outcome_text(r: dict, names: dict, task: dict | None = None) -> str:
    parts = []
    for o in (task or {}).get("setup", {}).get("orders", []):  # what existed before they spoke
        step = ["접수", "조리중", "배달중", "도착"][min(3, int(o.get("age_sec", 0)) // 10)]
        parts.append(f"an earlier order already existed: {names.get(o['menu_id'], o['menu_id'])} ({step})")
    new = [f"ordered {names.get(o['menuId'], o['menuId'])}" for o in rebuild_orders(r["calls"])]
    if any(c["name"] == "cancel_order" and c["ok"] for c in r["calls"]):
        new.append("cancelled the order")
    return "; ".join(parts + (new or ["no new order, nothing changed in this conversation"]))


def table(rows: list[dict]) -> str:
    def line(name, rs):
        c = Counter(r["verdict"] for r in rs)
        n = len(rs)
        pct = lambda k: f"{c[k]} ({100 * c[k] / n:.0f}%)"  # noqa: E731
        auto = sum(r["auto_pass"] for r in rs)
        return f"| {name} | {n} | {pct('pass')} | {pct('border')} | {pct('fail')} | {auto} ({100 * auto / n:.0f}%) |"

    out = ["| 묶음 | 수 | 통과 | 경계 | 실패 | 자동 채점 통과 |", "|---|---|---|---|---|---|"]
    out.append(line("전체", rows))
    by = defaultdict(list)
    for r in rows:
        by[r["category"]].append(r)
    out += [line(k, v) for k, v in sorted(by.items())]
    return "\n".join(out)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("results_dir")
    p.add_argument("tasks")
    p.add_argument("--model", default="gpt-5-mini")
    p.add_argument("--effort", default="medium")
    p.add_argument("--base-url", default="https://api.openai.com/v1")
    p.add_argument("--workers", type=int, default=8)
    args = p.parse_args(argv)
    llm = LLMConfig(args.model, args.base_url, os.getenv("OPENAI_API_KEY", ""), reasoning_effort=args.effort)
    data = load_data(None)
    names = {m["id"]: m["name"] for m in data.menus}
    menus = json.dumps(
        [{"name": m["name"], "price": f"{m['price']:,}원", "category": m["category"],
          "allergens": m.get("allergens", []), "description": m.get("description", "")}
         for m in data.menus],
        ensure_ascii=False,
    )
    tasks = {t["id"]: t for t in load_tasks(Path(args.tasks))}
    results = [json.loads(line) for line in open(Path(args.results_dir) / "results.jsonl", encoding="utf-8")]

    def one(r):
        v = judge_verdict(tasks[r["id"]], r["dialog"], outcome_text(r, names, tasks[r["id"]]), llm, menus)
        return {"id": r["id"], "category": r["category"], "auto_pass": r["success"], **v}

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        rows = list(pool.map(one, results))
    with open(Path(args.results_dir) / "verdicts.jsonl", "w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    rows = [r for r in rows if r.get("verdict")]
    print(table(rows))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
