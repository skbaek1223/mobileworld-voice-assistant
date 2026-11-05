"""App tools: your app's own features exposed to the agent as callable functions.

The agent sees them in the same format as MobileWorld MCP tools
(``{"name", "description", "inputSchema"}``) and calls them with
``{"action_type": "mcp", "action_name": ..., "action_json": {...}}``.
Anything without a tool falls back to GUI actions on the screenshot.

Tool kinds available in the ADB prototype:

- ``deeplink``  -> ``am start -a VIEW -d <uri> -p <package>`` (navigate / open a screen)
- ``broadcast`` -> ``am broadcast -a <action> -p <package> --es k v``; the app's
  ``BroadcastReceiver`` can return data with ``setResultData(...)``, which becomes the
  tool result the agent reads.
- ``python``    -> any Python callable (mock backends, server APIs, tests).

On-device, the same names/schemas map 1:1 onto Kotlin functions in the app.
"""

from __future__ import annotations

import json
import string
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

from loguru import logger

from mobile_world.assistant.device import parse_broadcast_result

if TYPE_CHECKING:
    from mobile_world.assistant.device import DeviceBackend

ToolHandler = Callable[[dict[str, Any], "DeviceBackend | None"], Any]


@dataclass
class AppTool:
    name: str
    description: str
    handler: ToolHandler
    parameters: dict[str, Any] = field(default_factory=lambda: {"type": "object", "properties": {}})

    def schema(self) -> dict[str, Any]:
        return {"name": self.name, "description": self.description, "inputSchema": self.parameters}


class ToolError(Exception):
    pass


def _validate_args(tool: AppTool, args: dict[str, Any]) -> None:
    schema = tool.parameters or {}
    try:
        import jsonschema

        jsonschema.validate(args, schema)
        return
    except ImportError:
        pass
    except Exception as e:  # jsonschema.ValidationError
        raise ToolError(f"invalid arguments for '{tool.name}': {getattr(e, 'message', e)}")
    missing = [k for k in schema.get("required", []) if k not in args]
    if missing:
        raise ToolError(f"missing required arguments for '{tool.name}': {missing}")


class ToolRegistry:
    def __init__(self, tools: list[AppTool] | None = None):
        self._tools: dict[str, AppTool] = {}
        for t in tools or []:
            self.register(t)

    def register(self, tool: AppTool) -> AppTool:
        if tool.name in self._tools:
            raise ValueError(f"Duplicate tool name: {tool.name}")
        self._tools[tool.name] = tool
        return tool

    def tool(self, name: str, description: str, parameters: dict | None = None):
        """Decorator: ``@registry.tool("add_event", "Add a calendar event", {...})``."""

        def deco(fn: Callable[..., Any]):
            self.register(
                AppTool(
                    name=name,
                    description=description,
                    handler=lambda args, _device: fn(**args),
                    parameters=parameters or {"type": "object", "properties": {}},
                )
            )
            return fn

        return deco

    @property
    def names(self) -> set[str]:
        return set(self._tools)

    def __len__(self) -> int:
        return len(self._tools)

    def schemas(self) -> list[dict[str, Any]]:
        return [t.schema() for t in self._tools.values()]

    def call(self, name: str, args: dict[str, Any] | None, device: DeviceBackend | None) -> str:
        """Run a tool and always return a string result for the agent (errors included)."""
        tool = self._tools.get(name)
        if tool is None:
            return f"Error: unknown tool '{name}'. Available: {sorted(self._tools)}"
        args = args or {}
        try:
            _validate_args(tool, args)
            result = tool.handler(args, device)
        except ToolError as e:
            return f"Error: {e}"
        except Exception as e:
            logger.exception(f"Tool {name} failed")
            return f"Error: tool '{name}' failed: {e}"
        if result is None:
            return "ok"
        if isinstance(result, str):
            return result
        return json.dumps(result, ensure_ascii=False)


# --------------------------------------------------------------------------- ADB-backed tool kinds
def _template_fields(template: str) -> set[str]:
    return {f for _, f, _, _ in string.Formatter().parse(template) if f}


def deeplink_tool(
    name: str, description: str, uri: str, package: str, parameters: dict | None = None
) -> AppTool:
    """``uri`` may contain ``{placeholders}`` filled (URL-encoded) from the arguments."""

    def handler(args: dict[str, Any], device: DeviceBackend | None) -> str:
        if device is None:
            raise ToolError("deeplink tools need a device")
        fields = _template_fields(uri)
        filled = uri.format(**{k: quote(str(args.get(k, "")), safe="") for k in fields})
        out = device.shell(
            ["am", "start", "-W", "-a", "android.intent.action.VIEW", "-d", filled, "-p", package]
        )
        if "Error" in out:
            raise ToolError(out.strip())
        return f"opened {filled}"

    return AppTool(name, description, handler, parameters or {"type": "object", "properties": {}})


def broadcast_tool(
    name: str, description: str, action: str, package: str, parameters: dict | None = None
) -> AppTool:
    """Arguments are sent as string extras; non-strings are JSON-encoded."""

    def handler(args: dict[str, Any], device: DeviceBackend | None) -> str:
        if device is None:
            raise ToolError("broadcast tools need a device")
        cmd = ["am", "broadcast", "-a", action, "-p", package]
        for k, v in args.items():
            cmd += ["--es", k, v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)]
        return parse_broadcast_result(device.shell(cmd))

    return AppTool(name, description, handler, parameters or {"type": "object", "properties": {}})


def load_tools_file(path: str | Path, package: str) -> ToolRegistry:
    """Load tool definitions from JSON (see docs/assistant_tools.example.json)."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    entries = data["tools"] if isinstance(data, dict) else data
    registry = ToolRegistry()
    for e in entries:
        kind = e.get("type")
        pkg = e.get("package", package)
        params = e.get("parameters")
        if kind == "deeplink":
            registry.register(deeplink_tool(e["name"], e["description"], e["uri"], pkg, params))
        elif kind == "broadcast":
            registry.register(broadcast_tool(e["name"], e["description"], e["action"], pkg, params))
        else:
            raise ValueError(f"Tool '{e.get('name')}': unsupported type '{kind}'")
    return registry
