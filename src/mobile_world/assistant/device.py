"""Device backends the assistant drives.

``DeviceBackend`` is the seam between the assistant logic and "the phone":

- ``MobileWorldDevice`` (now): GUI actions go through the MobileWorld server
  (``mw server``), package checks / intents go straight through ``adb``.
- On-device (later): the same methods implemented inside the app — screenshots via
  ``PixelCopy``/view drawing, taps via injected ``MotionEvent``s, tools as direct calls.
"""

from __future__ import annotations

import re
import shlex
import subprocess
from abc import ABC, abstractmethod

from loguru import logger
from PIL import Image

from mobile_world.runtime.utils.models import JSONAction

# e.g. "mCurrentFocus=Window{5f3a u0 com.example.app/com.example.app.MainActivity}"
_FOCUS_RE = re.compile(r"mCurrentFocus=Window\{[^}]*?\s([A-Za-z0-9_.]+)/[^\s}]+\}")
# Fallback, e.g. "mFocusedApp=ActivityRecord{1a2b u0 com.example.app/.MainActivity t12}"
_FOCUSED_APP_RE = re.compile(r"mFocusedApp=\S*\{[^}]*?\s([A-Za-z0-9_.]+)/")
# Focused non-activity windows such as "Window{... u0 NotificationShade}" / "StatusBar"
_FOCUS_WINDOW_ONLY_RE = re.compile(r"mCurrentFocus=Window\{[^}]*?\s([A-Za-z0-9_.]+)\}")


def parse_focused_package(dumpsys_window: str) -> str | None:
    """Extract the package that owns the focused window from ``dumpsys window`` output."""
    for regex in (_FOCUS_RE, _FOCUSED_APP_RE):
        m = regex.search(dumpsys_window)
        if m:
            return m.group(1)
    m = _FOCUS_WINDOW_ONLY_RE.search(dumpsys_window)
    if m:
        # System windows (notification shade, status bar) belong to SystemUI.
        return "com.android.systemui"
    return None


def parse_broadcast_result(output: str) -> str:
    """Pull ``data="..."`` out of ``am broadcast`` output (set via ``setResultData``)."""
    m = re.search(r'data="(.*)"', output, re.DOTALL)
    if m:
        return m.group(1)
    m = re.search(r"result=(-?\d+)", output)
    return f"result={m.group(1)}" if m else output.strip()


class DeviceBackend(ABC):
    @abstractmethod
    def screenshot(self) -> Image.Image: ...

    @abstractmethod
    def execute(self, action: JSONAction) -> Image.Image:
        """Run one GUI action and return the screenshot after it settled."""

    @abstractmethod
    def current_package(self) -> str | None: ...

    @abstractmethod
    def launch(self, package: str, activity: str | None = None) -> bool: ...

    @abstractmethod
    def back(self) -> None: ...

    @abstractmethod
    def shell(self, args: list[str], timeout: float = 20.0) -> str:
        """Run a command on the device shell; each arg is quoted for the device shell."""


class MobileWorldDevice(DeviceBackend):
    """Real phone or emulator driven by the MobileWorld server + adb."""

    def __init__(
        self,
        aw_url: str = "http://127.0.0.1:6800",
        device: str = "emulator-5554",
        step_wait_time: float = 1.0,
        adb_path: str = "adb",
    ):
        # Imported lazily: the client pulls in MCP dependencies we don't need for tests.
        from mobile_world.runtime.client import AndroidEnvClient

        self.env = AndroidEnvClient(aw_url, device, step_wait_time=step_wait_time)
        self.device = device
        self.adb_path = adb_path

    # -- adb helpers
    def _adb(self, *args: str, timeout: float = 20.0) -> str:
        cmd = [self.adb_path, "-s", self.device, *args]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if res.returncode != 0:
            logger.warning(f"adb failed ({res.returncode}): {' '.join(cmd)}\n{res.stderr}")
        return res.stdout

    def shell(self, args: list[str], timeout: float = 20.0) -> str:
        return self._adb("shell", " ".join(shlex.quote(a) for a in args), timeout=timeout)

    # -- DeviceBackend
    def screenshot(self) -> Image.Image:
        return self.env.get_screenshot(wait_to_stabilize=False)

    def execute(self, action: JSONAction) -> Image.Image:
        return self.env.execute_action(action).screenshot

    def current_package(self) -> str | None:
        try:
            return parse_focused_package(self._adb("shell", "dumpsys", "window", timeout=10))
        except Exception as e:
            logger.warning(f"Could not read focused package: {e}")
            return None

    def launch(self, package: str, activity: str | None = None) -> bool:
        if activity:
            component = activity if "/" in activity else f"{package}/{activity}"
            out = self.shell(["am", "start", "-n", component])
        else:
            out = self.shell(
                ["monkey", "-p", package, "-c", "android.intent.category.LAUNCHER", "1"]
            )
        return "Error" not in out and "No activities found" not in out

    def back(self) -> None:
        self.shell(["input", "keyevent", "KEYCODE_BACK"])
