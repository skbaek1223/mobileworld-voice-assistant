"""Yes/no understanding for spoken confirmations (no LLM).

Used when the session itself asks an app-provided confirmation question. Negation wins over
affirmation ("아니, 주문하지 마" is a no), and anything unclear is *not* a yes — silence or
doubt must never place an order.
"""

from __future__ import annotations

import re

_NO = {
    "ko": ["아니", "안 ", "안해", "안 해", "안할", "안 할", "하지 마", "하지마", "싫", "됐어", "됐습니다",
           "그만", "취소", "다음에", "나중에", "잠깐", "잠시만", "말고", "필요 없", "괜찮아요", "괜찮아"],
    "en": ["no", "nope", "don't", "do not", "cancel", "stop", "wait", "later", "not now"],
}  # fmt: skip
_YES = {
    "ko": ["네", "예", "응", "어 ", "어,", "그래", "좋아", "좋습니다", "해 줘", "해줘", "해 주세요", "해주세요",
           "주문해", "바꿔", "맞아", "맞아요", "오케이", "알았어", "알겠어", "그렇게 해", "부탁"],
    "en": ["yes", "yeah", "yep", "sure", "ok", "okay", "please do", "go ahead", "do it"],
}  # fmt: skip


def _norm(text: str) -> str:
    return " " + re.sub(r"\s+", " ", (text or "").lower()).strip() + " "


def classify_yes_no(text: str, language: str = "ko") -> str:
    """'yes' | 'no' | 'unclear'."""
    t = _norm(text)
    if not t.strip():
        return "unclear"
    lang = language if language in _NO else "en"
    words = _NO[lang] + (_NO["en"] if lang != "en" else [])
    if any(w in t for w in words):
        return "no"
    yes = _YES[lang] + (_YES["en"] if lang != "en" else [])
    if any(w in t for w in yes) or t.strip() in ("어", "예", "네", "응"):
        return "yes"
    return "unclear"


def is_bare_no(text: str, language: str = "ko") -> bool:
    """A plain refusal with nothing else in it ("아니요, 안 할래요"), as opposed to a
    refusal that carries a new request ("아니, 전복죽으로 해 줘")."""
    t = _norm(text)
    lang = language if language in _NO else "en"
    for w in sorted(_NO[lang] + _YES[lang], key=len, reverse=True):
        t = t.replace(w, " ")
    rest = re.sub(r"[\s.,!?~요게래할]+", "", t)  # endings like 할게/할래요 carry no request
    return len(rest) <= 2
