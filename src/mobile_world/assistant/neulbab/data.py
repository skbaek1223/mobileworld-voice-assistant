"""Loads the 늘밥 web prototype's data files at runtime (they are not copied into this repo).

Point ``NEULBAB_APP_DIR`` (or ``app_dir``) at ``agent-company/product/app``.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ENV_APP_DIR = "NEULBAB_APP_DIR"


@dataclass
class NeulbabData:
    menus: list[dict[str, Any]]
    nutrition: dict[str, Any]
    health_rules: dict[str, Any]
    platforms: list[dict[str, Any]]
    demo_accounts: dict[str, Any] = field(default_factory=dict)
    sample_settings: dict[str, Any] = field(default_factory=dict)

    @property
    def conditions(self) -> list[dict[str, Any]]:
        return self.health_rules.get("conditions", [])

    def condition(self, cid: str) -> dict[str, Any] | None:
        return next((c for c in self.conditions if c["id"] == cid), None)

    def menu(self, menu_id: str) -> dict[str, Any] | None:
        return next((m for m in self.menus if m["id"] == menu_id), None)

    def platform(self, pid: str | None) -> dict[str, Any] | None:
        return next((p for p in self.platforms if p["id"] == pid), None)

    @property
    def allergens(self) -> list[str]:
        """Allergen options shown on the guardian settings screen (child.html)."""
        return ["새우", "게", "달걀", "우유", "땅콩", "밀", "대두", "고등어"]


def resolve_app_dir(app_dir: str | Path | None = None) -> Path:
    path = app_dir or os.getenv(ENV_APP_DIR)
    if not path:
        raise FileNotFoundError(
            f"늘밥 web app folder not given. Set {ENV_APP_DIR} to agent-company/product/app"
        )
    path = Path(path)
    if not (path / "data" / "menus.json").exists():
        raise FileNotFoundError(f"{path} does not look like the 늘밥 app (no data/menus.json)")
    return path


def load_data(app_dir: str | Path | None = None) -> NeulbabData:
    d = resolve_app_dir(app_dir) / "data"

    def read(name: str, default: Any = None) -> Any:
        p = d / name
        if not p.exists():
            if default is not None:
                return default
            raise FileNotFoundError(p)
        return json.loads(p.read_text(encoding="utf-8"))

    return NeulbabData(
        menus=read("menus.json"),
        nutrition=read("nutrition.json"),
        health_rules=read("health_rules.json"),
        platforms=read("platforms.json")["platforms"],
        demo_accounts=read("demo_accounts.json", {}),
        sample_settings=read("sample_settings.json", {}),
    )
