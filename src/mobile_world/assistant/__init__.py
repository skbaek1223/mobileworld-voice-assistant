"""In-app voice assistant built on MobileWorld.

Pieces (each swappable):
- speech.py    : SpeechToText / TextToSpeech (console, faster-whisper, OpenAI-compatible)
- device.py    : DeviceBackend (MobileWorld server + adb today, on-device later)
- app_tools.py : the app's own features as tools (deeplink / broadcast / python)
- scope.py     : AppScopeGuard keeps the agent inside the host app
- agent.py     : InAppAssistantAgent (hybrid tools + GUI, spoken replies)
- session.py   : VoiceAssistantSession (listen -> act -> speak loop)
"""

from mobile_world.assistant.app_tools import (
    AppTool,
    ToolRegistry,
    broadcast_tool,
    deeplink_tool,
    load_tools_file,
)
from mobile_world.assistant.scope import AppScopeGuard
from mobile_world.assistant.session import State, TurnResult, VoiceAssistantSession
from mobile_world.assistant.speech import SpeechToText, TextToSpeech, build_stt, build_tts

__all__ = [
    "AppScopeGuard",
    "AppTool",
    "SpeechToText",
    "State",
    "TextToSpeech",
    "ToolRegistry",
    "TurnResult",
    "VoiceAssistantSession",
    "broadcast_tool",
    "build_stt",
    "build_tts",
    "deeplink_tool",
    "load_tools_file",
]
