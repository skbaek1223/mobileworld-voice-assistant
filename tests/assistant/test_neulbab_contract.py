"""Consistency checks for the 늘밥 assistant tool contract (docs/neulbab/tools.json).

Set NEULBAB_APP_DIR to agent-company/product/app to also check that the enums match the
web prototype's data files.
"""

import json
import os
import re
from pathlib import Path

import pytest

from mobile_world.assistant.app_tools import load_tools_file

CONTRACT = Path("docs/neulbab/tools.json")
DATA = json.loads(CONTRACT.read_text(encoding="utf-8"))
TOOLS = {t["name"]: t for t in DATA["tools"]}
ROLES = ("parent", "guardian")


def test_names_unique_and_actions_follow_prefix():
    assert len(TOOLS) == len(DATA["tools"])
    for name, t in TOOLS.items():
        assert t["action"] == DATA["action_prefix"] + name.upper(), name
        assert t["type"] == "broadcast"
        assert set(t["roles"]) <= set(ROLES) and t["roles"], name


def test_role_filtering():
    parent = load_tools_file(CONTRACT, role="parent")
    guardian = load_tools_file(CONTRACT, role="guardian")
    assert len(parent) == 9 and len(guardian) == 12
    assert {"cancel_order", "acknowledge_arrival"} <= parent.names
    assert not {"cancel_order", "acknowledge_arrival"} & guardian.names
    assert {"get_settings", "apply_settings_change"} <= guardian.names
    assert not {"get_settings", "apply_settings_change"} & parent.names
    assert len(load_tools_file(CONTRACT)) == len(TOOLS)  # no role -> everything


def test_package_defaults_to_contract_and_returns_are_described():
    reg = load_tools_file(CONTRACT, role="parent")
    schema = {s["name"]: s for s in reg.schemas()}["place_order"]
    assert "Returns data fields: order_id" in schema["description"]

    class Dev:
        calls = []

        def shell(self, args, timeout=20.0):
            self.calls.append(args)
            return 'Broadcast completed: result=-1, data="{}"'

    dev = Dev()
    reg.call("prepare_order", {"menu_id": "m04"}, dev)
    assert dev.calls[0][:6] == [
        "am",
        "broadcast",
        "-a",
        "com.neulbab.app.assistant.PREPARE_ORDER",
        "-p",
        "com.neulbab.app",
    ]


def test_error_codes_are_defined():
    for name, t in TOOLS.items():
        unknown = set(t["errors"]) - set(DATA["errors"])
        assert not unknown, f"{name}: {unknown}"


def test_confirmation_protocol_is_complete():
    issuers = [t for t in TOOLS.values() if t["confirmation"] == "issues"]
    requirers = [t for t in TOOLS.values() if t["confirmation"] == "requires"]
    assert {t["name"] for t in issuers} == {"prepare_order", "prepare_settings_change"}
    for t in issuers:
        assert "confirmation_id" in t["returns"] and "confirm_question" in t["returns"]
        assert t["effect"] == "read", "issuing a confirmation must not change state"
    for t in requirers:
        assert t["parameters"]["required"] == ["confirmation_id"], t["name"]
        assert "CONFIRMATION_INVALID" in t["errors"]
        # every requirer has an issuer usable by the same roles
        assert any(set(t["roles"]) <= set(i["roles"]) for i in issuers), t["name"]


def test_every_state_change_is_guarded():
    """Writes either need a confirmation_id or act on an explicit order_id."""
    for t in TOOLS.values():
        if t["effect"] != "write":
            continue
        params = t["parameters"].get("required", [])
        assert "confirmation_id" in params or "order_id" in params, t["name"]


def test_schemas_are_valid_json_schema():
    jsonschema = pytest.importorskip("jsonschema")
    for t in TOOLS.values():
        jsonschema.Draft7Validator.check_schema(t["parameters"])
        assert t["parameters"].get("additionalProperties") is False, t["name"]


def test_markdown_matches_contract():
    md = Path("docs/neulbab/tool_contract.md").read_text(encoding="utf-8")
    assert set(re.findall(r"^\| `([a-z_]+)` \|", md, re.M)) == set(TOOLS)
    assert set(re.findall(r"^\| `([A-Z_]+)` \|", md, re.M)) == set(DATA["errors"])
    for code, message in DATA["errors"].items():
        assert f"| `{code}` |" in md and message in md, code


@pytest.mark.skipif(not os.getenv("NEULBAB_APP_DIR"), reason="NEULBAB_APP_DIR not set")
def test_enums_match_web_prototype():
    app = Path(os.environ["NEULBAB_APP_DIR"])
    rules = json.loads((app / "data/health_rules.json").read_text(encoding="utf-8"))
    platforms = json.loads((app / "data/platforms.json").read_text(encoding="utf-8"))
    menus = json.loads((app / "data/menus.json").read_text(encoding="utf-8"))
    mock_api = (app / "mock_api.js").read_text(encoding="utf-8")

    props = TOOLS["prepare_settings_change"]["parameters"]["properties"]
    assert props["health_conditions"]["items"]["enum"] == [c["id"] for c in rules["conditions"]]
    assert props["platform"]["enum"] == [p["id"] for p in platforms["platforms"]]
    assert set(props["preferred_categories"]["items"]["enum"]) == {m["category"] for m in menus}

    def js_list(const):
        return json.loads(re.search(rf"const {const} = (\[.*?\]);", mock_api).group(1))

    assert props["required_tags"]["items"]["enum"] == js_list("TAG_LIST")
    assert props["guardian_title"]["enum"] == js_list("GUARDIAN_TITLES")
    assert props["preferred_categories"]["items"]["enum"] == js_list("CATEGORY_ORDER")

    # error messages reused from the web app's TEXT table stay identical
    assert DATA["errors"]["NO_SETTINGS"] in mock_api
    assert DATA["errors"]["GUARDIAN_BLOCKED"] in mock_api


def test_issuers_name_their_commit_tool():
    for t in TOOLS.values():
        if t["confirmation"] == "issues":
            commit = TOOLS[t["commit_tool"]]
            assert commit["confirmation"] == "requires" and set(t["roles"]) == set(commit["roles"])
        else:
            assert "commit_tool" not in t, t["name"]
