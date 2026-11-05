"""Pull speakable sentences out of a streaming agent response.

The agent answers as ``Thought: ...\\nAction: {"action_type": "answer", "text": "..."}``.
While tokens arrive, this extractor watches for an ``answer`` / ``ask_user`` action whose
``text`` value has started and hands out each finished sentence immediately, so text-to-
speech can begin before the model is done. If the action type is not known before the text
starts (unusual key order), nothing is streamed and the full text is spoken at the end.
"""

from __future__ import annotations

import re

_TYPE = re.compile(r'"action_type"\s*:\s*"(\w+)"')
_TEXT = re.compile(r'"text"\s*:\s*"')
_SENTENCE_END = re.compile(r"[.?!。](?=\s|$)")
_SPEAKABLE = ("answer", "ask_user")


class SpeechStreamExtractor:
    def __init__(self) -> None:
        self.buffer = ""
        self.decoded = ""  # the (partial) text value, JSON-unescaped
        self.done = False  # the closing quote of the text value was seen
        self.speakable: bool | None = None
        self._emitted = 0  # chars of `decoded` already handed out
        self._pos: int | None = None  # where the text value starts in buffer
        self._scan = 0  # next buffer index to decode

    @property
    def spoken(self) -> str:
        return self.decoded[: self._emitted]

    def feed(self, chunk: str) -> list[str]:
        self.buffer += chunk
        if self.speakable is False or self.done:
            return []
        if self._pos is None and not self._locate():
            return []
        self._decode()
        return self._sentences(final=self.done)

    def _locate(self) -> bool:
        action_at = self.buffer.rfind("Action:")
        if action_at < 0:
            return False
        tail = self.buffer[action_at:]
        text_m = _TEXT.search(tail)
        if not text_m:
            return False
        type_m = _TYPE.search(tail)
        if not type_m or type_m.start() > text_m.start():
            self.speakable = False  # type unknown before the text starts: don't guess
            return False
        self.speakable = type_m.group(1) in _SPEAKABLE
        if not self.speakable:
            return False
        self._pos = self._scan = action_at + text_m.end()
        return True

    def _decode(self) -> None:
        b, i = self.buffer, self._scan
        escapes = {'"': '"', "\\": "\\", "/": "/", "n": " ", "t": " ", "r": " "}
        while i < len(b):
            ch = b[i]
            if ch == "\\":
                if i + 1 >= len(b):
                    break  # wait for the rest of the escape
                nxt = b[i + 1]
                if nxt == "u":
                    if i + 6 > len(b):
                        break
                    try:
                        self.decoded += chr(int(b[i + 2 : i + 6], 16))
                    except ValueError:
                        pass
                    i += 6
                    continue
                self.decoded += escapes.get(nxt, nxt)
                i += 2
                continue
            if ch == '"':
                self.done = True
                i += 1
                break
            self.decoded += ch
            i += 1
        self._scan = i

    def _sentences(self, final: bool) -> list[str]:
        out = []
        rest = self.decoded[self._emitted :]
        while True:
            m = _SENTENCE_END.search(rest)
            if not m:
                break
            piece = rest[: m.end()]
            self._emitted += len(piece)
            rest = rest[m.end() :]
            if piece.strip():
                out.append(piece.strip())
        if final and rest.strip():
            self._emitted += len(rest)
            out.append(rest.strip())
        return out
