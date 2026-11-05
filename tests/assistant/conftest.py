"""Shared fixtures: a tiny self-contained 늘밥 dataset (no real data needed)."""

import json

import pytest


def menu(mid, name, cat, price, tags, allergens=(), available=True):
    return {
        "id": mid, "name": name, "category": cat, "price": price, "nutritionTags": list(tags),
        "allergens": list(allergens), "available": available, "description": f"{name} 설명",
    }  # fmt: skip


FIXTURE = {
    "menus.json": [
        menu("f1", "미역국 정식", "국·찌개", 8500, ["저염", "부드러운음식"]),
        menu("f2", "고등어구이 정식", "생선", 9500, ["저염", "고단백"], ["고등어"]),
        menu("f3", "새우탕 정식", "국·찌개", 9000, ["고단백"], ["새우"]),
        menu("f4", "전복죽", "죽", 10000, ["저염", "부드러운음식"]),
        menu("f5", "잔치국수", "밥·면", 7000, ["부드러운음식"], ["밀", "달걀"]),
        menu("f6", "버섯전골", "국·찌개", 9000, ["저염"], available=False),
    ],
    "nutrition.json": {
        "menus": {
            "f1": {
                "kcal": 265,
                "sodium_mg": 600,
                "sugars_g": 1.04,
                "sources": [{"name": "s1", "url": "https://example.org/1"}],
            },
            "f2": {"kcal": 522, "sodium_mg": 900, "sugars_g": 0},
            "f3": {"kcal": 359, "sodium_mg": 700, "sugars_g": 0},
            "f4": {"kcal": 250, "sodium_mg": None, "sugars_g": None},
            "f5": {"kcal": 599, "sodium_mg": 1600, "sugars_g": 0.7},
            "f6": {},
        }
    },  # fmt: skip
    "health_rules.json": {
        "conditions": [
            {
                "id": "hypertension",
                "label": "혈압이 높아요",
                "note": "싱겁게 드시도록 골랐어요",
                "limits": {"sodium_mg": {"max": 667}},
                "requireTags": [],
            },
            {
                "id": "chewing",
                "label": "씹기가 힘드세요",
                "note": "부드럽게 드실 수 있는 음식으로 골랐어요",
                "limits": {},
                "requireTags": ["부드러운음식"],
            },
        ]
    },  # fmt: skip
    "platforms.json": {
        "platforms": [
            {"id": "pa", "label": "가 배달앱"},
            {"id": "pb", "label": "나 배달앱"},
            {"id": "pc", "label": "다 배달앱"},
        ]
    },
    "demo_accounts.json": {
        "guardian": {"id": "demo"},
        "parent": {"familyCode": "246810", "name": "김순자"},
    },
    "sample_settings.json": {
        "parentName": "어머니",
        "address": "대전시 늘밥구 행복로 12",
        "payment": "가상 카드 A",
        "preferredCategories": ["생선"],
        "allergies": ["새우"],
        "requiredTags": [],
        "preferredTags": ["부드러운음식"],
    },  # fmt: skip
}


@pytest.fixture
def app_dir(tmp_path):
    d = tmp_path / "app" / "data"
    d.mkdir(parents=True)
    for name, content in FIXTURE.items():
        (d / name).write_text(json.dumps(content, ensure_ascii=False), encoding="utf-8")
    return tmp_path / "app"
