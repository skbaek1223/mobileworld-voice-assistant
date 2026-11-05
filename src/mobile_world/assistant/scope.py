"""Keeps the assistant inside its host app.

Two layers:
1. ``check_action`` rejects actions that would leave the app *before* they run
   (home button, launching other apps, unknown tools).
2. ``is_in_scope`` / ``recover`` catch the app being left anyway (a link opening a
   browser, a share sheet, ...) and bring it back: press back once, then relaunch.

The rejection/recovery message is fed back to the agent so it can choose another path.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING

from loguru import logger

from mobile_world.runtime.utils.models import MCP, NAVIGATE_HOME, OPEN_APP, WAIT, JSONAction

if TYPE_CHECKING:
    from mobile_world.assistant.device import DeviceBackend

# Short-lived system UI that may legitimately appear on top of the app.
DEFAULT_TRANSIENT_PACKAGES = (
    "com.android.permissioncontroller",
    "com.google.android.permissioncontroller",
)


class AppScopeGuard:
    def __init__(
        self,
        package: str,
        app_name: str | None = None,
        extra_packages: Iterable[str] = (),
        transient_packages: Iterable[str] = DEFAULT_TRANSIENT_PACKAGES,
        tool_names: Iterable[str] = (),
        launch_activity: str | None = None,
        allow_gui: bool = True,
    ):
        self.package = package
        self.allow_gui = allow_gui
        self.app_name = app_name or package
        self.allowed = {package, *extra_packages}
        self.transient = set(transient_packages)
        self.tool_names = set(tool_names)
        self.launch_activity = launch_activity

    def _is_self(self, app_name: str | None) -> bool:
        if not app_name:
            return False
        return app_name.strip().lower() in {self.package.lower(), self.app_name.lower()}

    def check_action(self, action: JSONAction) -> str | None:
        """Return a rejection message, or None if the action may run."""
        t = action.action_type
        if not self.allow_gui and t not in (MCP, WAIT):
            return (
                "Blocked: there is no screen in this session. Use the app tools, then reply "
                "with `answer` or `ask_user`."
            )
        if t == NAVIGATE_HOME:
            return (
                f"Blocked: navigate_home would leave {self.app_name}. You can only operate "
                "inside this app. Use navigate_back or in-app navigation instead."
            )
        if t == OPEN_APP and not self._is_self(action.app_name):
            return (
                f"Blocked: opening '{action.app_name}' is not allowed. You can only operate "
                f"inside {self.app_name}. If the request needs another app, tell the user "
                "with an `answer` action."
            )
        if t == MCP and action.action_name not in self.tool_names:
            return (
                f"Blocked: '{action.action_name}' is not an available tool. "
                f"Available tools: {sorted(self.tool_names) or 'none'}."
            )
        return None

    def is_in_scope(self, package: str | None) -> bool:
        # Unknown focus (e.g. adb hiccup) is not treated as a violation.
        return package is None or package in self.allowed or package in self.transient

    def ensure_in_scope(self, device: DeviceBackend) -> str | None:
        """Bring the app back to the foreground if needed. Returns a note for the agent."""
        pkg = device.current_package()
        if self.is_in_scope(pkg):
            return None
        logger.info(f"[scope] left app: focused={pkg}, recovering")
        device.back()
        if self.is_in_scope(device.current_package()):
            return (
                f"Note: the last action left {self.app_name} (opened {pkg}). "
                "Pressed back to return. Do not repeat that action."
            )
        device.launch(self.package, self.launch_activity)
        return (
            f"Note: the last action left {self.app_name} (opened {pkg}). The app was "
            "relaunched, so the screen may have changed. Do not repeat that action."
        )
