"""Re-score saved results (results.jsonl) against the current task expectations.

The conversation and tool calls are saved per task, so changed checks can be applied without
running the models again. New orders are rebuilt from prepare_order -> place_order calls.

    python -m mobile_world.assistant.neulbab.rescore RESULTS_DIR TASKS_JSON
"""

from __future__ import annotations

import json
import sys
from dataclasses import fields
from pathlib import Path
from types import SimpleNamespace

from mobile_world.assistant.neulbab.evaluation import TaskResult, check_task, load_tasks, summarize


def rebuild_orders(calls: list[dict]) -> list[dict]:
    orders, pending = [], None
    for c in calls:
        if c["name"] == "prepare_order" and c["ok"]:
            pending = c["args"].get("menu_id")
        elif c["name"] == "place_order" and c["ok"] and pending:
            orders.append({"menuId": pending, "orderedBy": "parent"})
            pending = None
    return orders


def rescore(results_dir: Path, tasks_path: Path) -> list[TaskResult]:
    tasks = {t["id"]: t for t in load_tasks(tasks_path)}
    keep = {f.name for f in fields(TaskResult)}
    out = []
    for line in open(results_dir / "results.jsonl", encoding="utf-8"):
        r = json.loads(line)
        task = tasks[r["id"]]
        app = SimpleNamespace(orders=rebuild_orders(r["calls"]), settings=None)
        run = {
            "final_reply": r["final_reply"],
            "spoken": r["spoken"],
            "tool_calls": r["calls"],
            "statuses": r["statuses"],
            "turn_actions": [],
        }
        fails = [f for f in check_task(task, run, app, {"orders": [], "settings": None})]
        r.update(success=not fails and not r.get("error"), failures=fails)
        out.append(TaskResult(**{k: v for k, v in r.items() if k in keep}))
    return out


if __name__ == "__main__":
    res = rescore(Path(sys.argv[1]), Path(sys.argv[2]))
    print(summarize(res))
