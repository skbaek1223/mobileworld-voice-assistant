"""Speech input/output for the in-app voice assistant.

Every engine implements one of two tiny interfaces so the session never cares which
STT/TTS is behind it:

- ``SpeechToText.listen() -> str``
- ``TextToSpeech.speak(text) -> None``

Heavy/optional dependencies (``sounddevice``, ``faster_whisper``) are imported lazily,
so the console engines work with no audio stack at all. When porting to Android, the
same two interfaces map onto ``SpeechRecognizer`` and ``TextToSpeech``.
"""

from __future__ import annotations

import io
import wave
from abc import ABC, abstractmethod
from typing import Any

import numpy as np
from loguru import logger

DEFAULT_SAMPLE_RATE = 16000


# --------------------------------------------------------------------------- interfaces
class SpeechToText(ABC):
    @abstractmethod
    def listen(self, prompt: str | None = None) -> str:
        """Capture one utterance and return its transcript ('' if nothing was heard)."""


class TextToSpeech(ABC):
    @abstractmethod
    def speak(self, text: str) -> None:
        """Say ``text`` out loud (blocking until finished)."""


# --------------------------------------------------------------------------- console
class ConsoleSTT(SpeechToText):
    """Keyboard input stand-in for a microphone. Useful for development and tests."""

    def listen(self, prompt: str | None = None) -> str:
        try:
            return input(f"🎤 {prompt or '말씀하세요'} > ").strip()
        except EOFError:
            return ""


class ConsoleTTS(TextToSpeech):
    def speak(self, text: str) -> None:
        print(f"🔊 {text}", flush=True)


# --------------------------------------------------------------------------- recording
class EnergyVAD:
    """Minimal energy-based endpointing: start on speech, stop after trailing silence.

    Kept free of any audio I/O so it can be unit-tested and reused for streamed audio.
    """

    def __init__(
        self,
        frame_ms: int = 30,
        energy_threshold: float = 0.01,
        silence_seconds: float = 0.8,
        start_timeout_seconds: float = 6.0,
        max_seconds: float = 15.0,
    ):
        self.frame_ms = frame_ms
        self.energy_threshold = energy_threshold
        self.silence_frames = max(1, int(silence_seconds * 1000 / frame_ms))
        self.start_timeout_frames = max(1, int(start_timeout_seconds * 1000 / frame_ms))
        self.max_frames = max(1, int(max_seconds * 1000 / frame_ms))
        self.reset()

    def reset(self) -> None:
        self.started = False
        self._frames_seen = 0
        self._trailing_silence = 0
        self.frames: list[np.ndarray] = []

    def push(self, chunk: np.ndarray) -> bool:
        """Feed one frame of float32 audio. Returns True when the utterance is complete."""
        self._frames_seen += 1
        rms = float(np.sqrt(np.mean(np.square(chunk)))) if chunk.size else 0.0
        if rms >= self.energy_threshold:
            self.started = True
            self._trailing_silence = 0
        elif self.started:
            self._trailing_silence += 1

        if self.started:
            self.frames.append(chunk)

        if self.started and self._trailing_silence >= self.silence_frames:
            return True
        if not self.started and self._frames_seen >= self.start_timeout_frames:
            return True
        return self._frames_seen >= self.max_frames

    def audio(self) -> np.ndarray:
        if not self.frames:
            return np.zeros(0, dtype=np.float32)
        return np.concatenate(self.frames).astype(np.float32)


class MicrophoneRecorder:
    """Records a single utterance from the default microphone (needs ``sounddevice``)."""

    def __init__(self, sample_rate: int = DEFAULT_SAMPLE_RATE, vad: EnergyVAD | None = None):
        self.sample_rate = sample_rate
        self.vad = vad or EnergyVAD()

    def record(self) -> np.ndarray:
        try:
            import sounddevice as sd
        except ImportError as e:  # pragma: no cover - depends on host audio stack
            raise RuntimeError("Microphone input needs `pip install sounddevice`") from e

        block = int(self.sample_rate * self.vad.frame_ms / 1000)
        self.vad.reset()
        with sd.InputStream(
            samplerate=self.sample_rate, channels=1, dtype="float32", blocksize=block
        ) as stream:
            while True:
                data, _ = stream.read(block)
                if self.vad.push(data[:, 0].copy()):
                    break
        audio = self.vad.audio()
        logger.debug(f"Recorded {audio.size / self.sample_rate:.2f}s of audio")
        return audio


def to_wav_bytes(audio: np.ndarray, sample_rate: int = DEFAULT_SAMPLE_RATE) -> bytes:
    """Encode float32 [-1, 1] mono audio as 16-bit PCM WAV."""
    pcm = (np.clip(audio, -1.0, 1.0) * 32767).astype("<i2")
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(pcm.tobytes())
    return buf.getvalue()


def play_wav_bytes(data: bytes) -> None:
    """Play WAV bytes on the default output device (needs ``sounddevice``)."""
    try:
        import sounddevice as sd
    except ImportError as e:  # pragma: no cover
        raise RuntimeError("Audio playback needs `pip install sounddevice`") from e

    with wave.open(io.BytesIO(data), "rb") as w:
        frames = w.readframes(w.getnframes())
        dtype = {1: "u1", 2: "<i2", 4: "<i4"}[w.getsampwidth()]
        audio = np.frombuffer(frames, dtype=dtype).reshape(-1, w.getnchannels())
        sd.play(audio, w.getframerate())
        sd.wait()


# --------------------------------------------------------------------------- local whisper
class WhisperSTT(SpeechToText):
    """Offline STT with faster-whisper (``pip install faster-whisper sounddevice``)."""

    def __init__(
        self,
        model_size: str = "small",
        language: str = "ko",
        device: str = "auto",
        compute_type: str = "default",
        recorder: MicrophoneRecorder | None = None,
    ):
        try:
            from faster_whisper import WhisperModel
        except ImportError as e:  # pragma: no cover
            raise RuntimeError("WhisperSTT needs `pip install faster-whisper`") from e
        self.model = WhisperModel(model_size, device=device, compute_type=compute_type)
        self.language = language
        self.recorder = recorder or MicrophoneRecorder()

    def listen(self, prompt: str | None = None) -> str:
        if prompt:
            print(f"🎤 {prompt}", flush=True)
        audio = self.recorder.record()
        if audio.size == 0:
            return ""
        segments, _ = self.model.transcribe(audio, language=self.language, vad_filter=True)
        return "".join(seg.text for seg in segments).strip()


# --------------------------------------------------------------------------- cloud (OpenAI-compatible)
def _openai_client(base_url: str | None, api_key: str | None):
    from openai import OpenAI

    return OpenAI(base_url=base_url, api_key=api_key or "empty", timeout=60.0)


class OpenAISTT(SpeechToText):
    """STT through any OpenAI-compatible ``/audio/transcriptions`` endpoint."""

    def __init__(
        self,
        model: str = "whisper-1",
        language: str = "ko",
        base_url: str | None = None,
        api_key: str | None = None,
        recorder: MicrophoneRecorder | None = None,
    ):
        self.client = _openai_client(base_url, api_key)
        self.model = model
        self.language = language
        self.recorder = recorder or MicrophoneRecorder()

    def listen(self, prompt: str | None = None) -> str:
        if prompt:
            print(f"🎤 {prompt}", flush=True)
        audio = self.recorder.record()
        if audio.size == 0:
            return ""
        wav = to_wav_bytes(audio, self.recorder.sample_rate)
        result = self.client.audio.transcriptions.create(
            model=self.model, file=("speech.wav", wav), language=self.language
        )
        return (result.text or "").strip()


class OpenAITTS(TextToSpeech):
    """TTS through any OpenAI-compatible ``/audio/speech`` endpoint."""

    def __init__(
        self,
        model: str = "tts-1",
        voice: str = "alloy",
        base_url: str | None = None,
        api_key: str | None = None,
    ):
        self.client = _openai_client(base_url, api_key)
        self.model = model
        self.voice = voice

    def speak(self, text: str) -> None:
        if not text:
            return
        print(f"🔊 {text}", flush=True)
        response = self.client.audio.speech.create(
            model=self.model, voice=self.voice, input=text, response_format="wav"
        )
        play_wav_bytes(response.content)


# --------------------------------------------------------------------------- factories
STT_ENGINES = ("console", "whisper", "openai")
TTS_ENGINES = ("console", "openai")


def build_stt(kind: str, **opts: Any) -> SpeechToText:
    kind = kind.lower()
    if kind == "console":
        return ConsoleSTT()
    if kind == "whisper":
        return WhisperSTT(
            model_size=opts.get("model") or "small", language=opts.get("language", "ko")
        )
    if kind == "openai":
        return OpenAISTT(
            model=opts.get("model") or "whisper-1",
            language=opts.get("language", "ko"),
            base_url=opts.get("base_url"),
            api_key=opts.get("api_key"),
        )
    raise ValueError(f"Unknown STT engine '{kind}'. Choose from {STT_ENGINES}")


def build_tts(kind: str, **opts: Any) -> TextToSpeech:
    kind = kind.lower()
    if kind == "console":
        return ConsoleTTS()
    if kind == "openai":
        return OpenAITTS(
            model=opts.get("model") or "tts-1",
            voice=opts.get("voice") or "alloy",
            base_url=opts.get("base_url"),
            api_key=opts.get("api_key"),
        )
    raise ValueError(f"Unknown TTS engine '{kind}'. Choose from {TTS_ENGINES}")
