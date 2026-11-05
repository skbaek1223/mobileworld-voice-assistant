"""OpenAI speech for the web app: speech-to-text for the phone's recordings and
text-to-speech for every sentence the assistant says.

Phones record in different formats (Android Chrome: webm/opus, iPhone Safari: mp4/aac);
both are accepted by the transcription API as-is. Speech is returned as mp3, which every
mobile browser can decode.
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from dataclasses import dataclass

from loguru import logger

# How the assistant should sound (gpt-4o-mini-tts follows free-text instructions).
ELDER_VOICE = (
    "Speak Korean warmly and politely, a little slower than usual, clearly pronouncing menu "
    "names, prices and times, like a kind helper talking to an elderly parent."
)
MIME_EXT = {
    "audio/webm": "webm",
    "audio/ogg": "ogg",
    "audio/mp4": "mp4",
    "audio/aac": "m4a",
    "audio/x-m4a": "m4a",
    "audio/mpeg": "mp3",
    "audio/wav": "wav",
}


@dataclass
class VoiceConfig:
    stt_model: str = "gpt-4o-mini-transcribe"
    tts_model: str = "gpt-4o-mini-tts"
    tts_voice: str = "coral"
    tts_instructions: str = ELDER_VOICE
    language: str = "ko"
    fallback_tts_model: str = "tts-1"
    fallback_stt_model: str = "whisper-1"


def extension_for(mime: str) -> str:
    base = (mime or "").split(";")[0].strip().lower()
    return MIME_EXT.get(base, "webm")


class OpenAIVoice:
    """Thread-safe STT/TTS with a small cache for repeated phrases."""

    def __init__(
        self, api_key: str, base_url: str | None = None, config: VoiceConfig | None = None
    ):
        from openai import OpenAI

        self.client = OpenAI(api_key=api_key, base_url=base_url, timeout=60.0)
        self.config = config or VoiceConfig()
        self._cache: OrderedDict[str, bytes] = OrderedDict()
        self._lock = threading.Lock()

    def transcribe(self, audio: bytes, mime: str) -> str:
        if not audio:
            return ""
        name = f"speech.{extension_for(mime)}"
        for model in (self.config.stt_model, self.config.fallback_stt_model):
            try:
                result = self.client.audio.transcriptions.create(
                    model=model, file=(name, audio), language=self.config.language
                )
                return (result.text or "").strip()
            except Exception as e:  # unknown model, bad audio, network
                logger.warning(f"STT with {model} failed: {e}")
        return ""

    def synthesize(self, text: str) -> bytes:
        """mp3 bytes for ``text`` (cached)."""
        with self._lock:
            if text in self._cache:
                self._cache.move_to_end(text)
                return self._cache[text]
        audio = b""
        try:
            audio = self.client.audio.speech.create(
                model=self.config.tts_model,
                voice=self.config.tts_voice,
                input=text,
                instructions=self.config.tts_instructions,
                response_format="mp3",
            ).content
        except Exception as e:
            logger.warning(f"TTS with {self.config.tts_model} failed ({e}); trying fallback")
            try:
                audio = self.client.audio.speech.create(
                    model=self.config.fallback_tts_model,
                    voice=self.config.tts_voice,
                    input=text,
                    response_format="mp3",
                ).content
            except Exception as e2:
                logger.error(f"TTS failed: {e2}")
                return b""
        with self._lock:
            self._cache[text] = audio
            while len(self._cache) > 200:
                self._cache.popitem(last=False)
        return audio
