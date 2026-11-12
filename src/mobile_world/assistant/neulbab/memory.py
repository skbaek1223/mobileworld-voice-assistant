"""What the assistant remembers about one 어르신: a core profile and dated notes.

The usual agent-memory split (as in ChatGPT memory, Mem0, MemGPT/Letta):

* ``profile`` — semantic memory: facts that rarely change (allergies, illnesses, teeth, likes,
  family, how to talk to them). Small, always put in the context, editable by the guardian.
* ``notes`` — episodic memory: short dated sentences about what happened or was said
  ("11월 3일: 가자미조림이 맛있었다고 하심"). The latest ``NOTES_IN_CONTEXT`` go in the context.

After a conversation, ``update_memory`` asks an LLM what is worth remembering and applies its
answer as operations (add / remove / set a profile field, add or drop a note), never as a
rewritten memory, so nothing is lost by accident. What the user says now wins over older memory.

Safety-critical facts (allergies, illnesses) are also guardian settings that the app enforces;
the memory only helps the assistant talk about them.
"""

from __future__ import annotations

import copy
import json
import re
import threading
from collections.abc import Callable
from pathlib import Path

from loguru import logger

# profile fields: (key, Korean label, kind) — "list" fields hold short phrases
PROFILE_FIELDS: list[tuple[str, str, str]] = [
    ("name", "이름", "text"),
    ("age", "나이", "text"),
    ("lives_with", "함께 사는 사람", "text"),
    ("family", "가족", "list"),
    ("conditions", "앓는 병", "list"),
    ("allergies", "알레르기·탈 나는 음식", "list"),
    ("teeth", "치아·씹기", "text"),
    ("doctor_advice", "의사 말씀", "list"),
    ("likes", "좋아하는 음식", "list"),
    ("dislikes", "싫어하거나 피하는 음식", "list"),
    ("habits", "생활 습관", "list"),
    ("speech", "말하는 방식", "text"),
    ("plans", "다가오는 일", "list"),
]
LIST_FIELDS = {k for k, _, kind in PROFILE_FIELDS if kind == "list"}
TEXT_FIELDS = {k for k, _, kind in PROFILE_FIELDS if kind == "text"}
LABELS = {k: label for k, label, _ in PROFILE_FIELDS}
NOTES_IN_CONTEXT = 15
MAX_NOTES = 200


def empty_memory() -> dict:
    return {"profile": {k: ([] if k in LIST_FIELDS else "") for k in LABELS}, "notes": []}


def normalize(memory: dict | None) -> dict:
    """A memory with every profile field present and only known fields kept."""
    out = empty_memory()
    if not isinstance(memory, dict):
        return out
    prof = memory.get("profile") or {}
    for k in LIST_FIELDS:
        vals = prof.get(k) or []
        if isinstance(vals, str):
            vals = [vals]
        out["profile"][k] = [str(v).strip() for v in vals if str(v).strip()]
    for k in TEXT_FIELDS:
        out["profile"][k] = str(prof.get(k) or "").strip()
    for n in memory.get("notes") or []:
        if isinstance(n, dict) and str(n.get("text", "")).strip():
            out["notes"].append(
                {
                    "date": str(n.get("date", "")),
                    "text": str(n["text"]).strip(),
                    "source": str(n.get("source", "")),
                }
            )
    out["notes"] = out["notes"][-MAX_NOTES:]
    return out


def is_empty(memory: dict | None) -> bool:
    m = normalize(memory)
    return not m["notes"] and not any(m["profile"].values())


# --------------------------------------------------------------------------- reading
MEMORY_RULE = """
- `What you remember` is this user's profile and dated notes from earlier conversations.
  Use it the way a good helper who remembers would:
  - When suggesting or choosing ("아무거나", "맛있는 거", "지난번 그거"), choose a menu they
    liked; never one they disliked, found too sweet/salty/hard, or that upset their body.
    Say why in a few words ("지난번에 맛있게 드셨던 가자미조림 어떠세요?"). Mention only what
    helps the request; do not recite their profile.
  - Before ordering, check the menu against the memory. Allergies cover the whole food family
    (조개류 includes 전복, 갑각류 includes 새우).
    - Allergy or a food that made them sick: do not prepare it. Say why in one sentence and
      offer one safe menu instead.
    - A dislike or a doctor's diet advice (less salt -> 된장찌개 and other salty soups, less
      sugar -> 단호박죽): if they named that menu themselves, say the concern in one sentence
      and still prepare THE MENU THEY ASKED FOR — what they say now wins, and the app's
      question lets them say no. Only when they leave the choice to you, pick a better menu.
  - If the memory has a request the app cannot send with an order (싱겁게, 국물 적게, 밥 반
    공기), mention once, briefly, that the app cannot pass it on, so they are not surprised.
  - Never give advice about medicines or illnesses; for that, suggest asking a doctor or
    pharmacist.
  - If they ask about something you do not remember, say so honestly and offer to remember it
    if they tell you ("알려 주시면 기억해 둘게요").
  - When they share a feeling (lonely, no appetite, missing family), first answer the feeling
    warmly in one sentence, using what you remember about them (a family member's name, a
    plan), then gently offer one menu they liked.
  - Resolve references to the past ("어제 그거", "손주 오면 먹기로 한 거") from the notes.
  - If what they say now differs from the memory, follow what they say now.
  - Never invent facts that are not in the memory or the app state (names, illnesses,
    medicines, past meals). If you do not know, say you do not remember.
"""


def memory_section(memory: dict | None) -> str:
    """The context block for ``memory`` ('' when there is nothing to remember)."""
    if is_empty(memory):
        return ""
    m = normalize(memory)
    lines = ["", "What you remember about this user:", "Profile:"]
    for key, label, kind in PROFILE_FIELDS:
        val = m["profile"][key]
        if val:
            lines.append(f"- {label}: {', '.join(val) if kind == 'list' else val}")
    notes = m["notes"][-NOTES_IN_CONTEXT:]
    if notes:
        lines.append("Notes from earlier conversations (oldest first):")
        lines += [f"- {n['date']}: {n['text']}" for n in notes]
    return "\n".join(lines)


def with_memory(provider: Callable[[], str], memory) -> Callable[[], str]:
    """Wrap a context provider so every snapshot also carries the memory.

    ``memory`` is a dict, or a function returning the current one (a live store)."""
    get = memory if callable(memory) else (lambda: memory)
    if not callable(memory) and is_empty(memory):
        return provider
    return lambda: provider() + memory_section(get())


# --------------------------------------------------------------------------- writing
UPDATE_PROMPT = """You maintain the memory of a Korean voice assistant for an older adult in
the meal-ordering app "밥마중". Today is {today}. Below are the current memory and a new
conversation ({source} spoke as "어르신"/"보호자"; "비서" is the assistant).

Decide what is worth remembering for future conversations about meals and daily care:
lasting facts (likes, dislikes, foods that upset them, allergies, illnesses, teeth, doctor's
advice, family names, routines, how they like to be spoken to, upcoming plans) go in the
profile; notable events of this conversation go in one or two short notes (what they ate or
liked, a complaint, a plan, a feeling). Skip small talk, the assistant's own words, order
numbers and anything already remembered. If they now say something that contradicts the
memory, remove or replace the old item. Do not guess; only use what was said.

Profile fields: {fields}
Current memory: {memory}
New conversation:
{dialog}

Reply with JSON only: {{"ops": [
  {{"op": "add", "field": "likes", "value": "가자미조림 정식"}},
  {{"op": "remove", "field": "likes", "value": "고등어구이 정식"}},
  {{"op": "set", "field": "teeth", "value": "틀니라 딱딱한 건 못 씹음"}},
  {{"op": "note", "text": "가자미조림이 맛있었다고 하심"}}
]}}  (an empty list if nothing new)"""


def apply_ops(memory: dict | None, ops: list, date: str = "", source: str = "") -> dict:
    """Apply update operations; unknown fields or shapes are ignored."""
    m = normalize(memory)
    for op in ops or []:
        if not isinstance(op, dict):
            continue
        kind, field, value = op.get("op"), op.get("field"), str(op.get("value") or "").strip()
        if kind == "note":
            text = str(op.get("text") or value).strip()
            if text and all(n["text"] != text for n in m["notes"]):
                m["notes"].append({"date": date, "text": text, "source": source})
        elif kind == "add" and field in LIST_FIELDS and value:
            if value not in m["profile"][field]:
                m["profile"][field].append(value)
        elif kind == "remove" and field in LIST_FIELDS and value:
            m["profile"][field] = [v for v in m["profile"][field] if v != value]
        elif kind == "set" and field in TEXT_FIELDS:
            m["profile"][field] = value
        elif kind == "set" and field in LIST_FIELDS:
            vals = op.get("value") if isinstance(op.get("value"), list) else [value]
            m["profile"][field] = [str(v).strip() for v in vals if str(v).strip()]
    m["notes"] = m["notes"][-MAX_NOTES:]
    return m


def extract_ops(memory: dict | None, dialog: list, chat: Callable[[str], str], today: str,
                source: str = "어르신") -> list:
    """Ask the LLM (``chat(prompt) -> text``) what to remember from ``dialog``."""
    talk = "\n".join(f"{who}: {text}" for who, text in dialog if str(text).strip())
    if not talk.strip():
        return []
    prompt = UPDATE_PROMPT.format(
        today=today,
        source=source,
        fields=", ".join(f"{k} ({label}, {kind})" for k, label, kind in PROFILE_FIELDS),
        memory=json.dumps(normalize(memory), ensure_ascii=False),
        dialog=talk,
    )
    text = chat(prompt)
    m = re.search(r"\{.*\}", text or "", re.S)
    try:
        ops = json.loads(m.group(0) if m else text).get("ops", [])
    except Exception:
        logger.warning(f"memory update: not JSON: {(text or '')[:200]}")
        return []
    return ops if isinstance(ops, list) else []


def update_memory(memory: dict | None, dialog: list, chat: Callable[[str], str], today: str,
                  source: str = "어르신") -> dict:
    """The memory after ``dialog`` (one conversation)."""
    ops = extract_ops(memory, dialog, chat, today, source)
    return apply_ops(memory, ops, date=today, source=source)


class MemoryStore:
    """One family's memory in a JSON file, safe to use from several threads."""

    def __init__(self, path: str | Path | None):
        self.path = Path(path) if path else None
        self.lock = threading.RLock()
        self.memory = empty_memory()
        if self.path and self.path.exists():
            try:
                self.memory = normalize(json.loads(self.path.read_text(encoding="utf-8")))
            except Exception as e:
                logger.warning(f"memory file unreadable, starting empty: {e}")

    def get(self) -> dict:
        with self.lock:
            return copy.deepcopy(self.memory)

    def replace(self, memory: dict) -> dict:
        with self.lock:
            self.memory = normalize(memory)
            self._save()
            return self.get()

    def learn(self, dialog: list, chat: Callable[[str], str], today: str, source: str) -> None:
        """Update from one conversation (call it in a background thread)."""
        try:
            ops = extract_ops(self.get(), dialog, chat, today, source)
        except Exception as e:
            logger.warning(f"memory update failed: {e}")
            return
        if ops:
            with self.lock:
                self.memory = apply_ops(self.memory, ops, date=today, source=source)
                self._save()
            logger.info(f"[memory] {len(ops)} update(s) from {source}")

    def _save(self) -> None:
        if self.path:
            self.path.write_text(json.dumps(self.memory, ensure_ascii=False, indent=1), encoding="utf-8")


# --------------------------------------------------------------------------- order check
# Done in code, not left to the prompt: before the app asks "…주문할까요?", the menu is checked
# against the memory and one or two short sentences are put in front of the question.
ALLERGY_FAMILIES = {
    "조개": ["조개", "전복", "굴", "홍합", "바지락"],
    "갑각": ["새우", "꽃게", "대게", "랍스터"],
    "생선": ["고등어", "가자미", "생선"],
    "우유": ["우유", "유제품"],
    "대두": ["대두", "콩", "두부", "된장"],
    "밀": ["밀", "국수", "면"],
    "달걀": ["달걀", "계란"],
}
SALT_WORDS = re.compile(r"짠|짜게|저염|싱겁|나트륨|염분|혈압|신장|콩팥")
SUGAR_WORDS = re.compile(r"당뇨|혈당|단 ?거|단것|당분|달게")
REQUEST_WORDS = re.compile(
    r"싱겁게|간 ?약하게|덜 ?짜게|짜지 ?않게|안 ?(맵|짜|달)게|국물 ?(적게|조금|반)|밥 ?반|맵지 ?않게|덜 ?맵게|"
    r"빼고|빼 ?줘|잘게|덜 ?익|푹 ?익|많이 ?줘|곱빼기"
)
NUMBER_WORDS = re.compile(r"숫자|가격|금액|시간|두 ?번|한 ?번 ?더|다시 ?말")
SALTY_SODIUM_MG = 667  # one meal's limit in the app's hypertension rule (health_rules.json)


def _subject(word: str) -> str:
    last = word.strip()[-1:] if word.strip() else ""
    has = bool(last) and "가" <= last <= "힣" and (ord(last) - 0xAC00) % 28 != 0
    return word + ("은" if has else "는")


def _allergy_hit(allergy: str, menu: dict) -> bool:
    core = re.sub(r"\s*(알레르기|알러지|류)\s*", "", allergy).strip()
    if not core:
        return False
    keys = {core}
    for family, words in ALLERGY_FAMILIES.items():
        if family in core or core in words:
            keys |= set(words)
    # the menu's name and its listed allergens only (descriptions are prose: "고소하게" ≠ 게)
    hay = " ".join([menu.get("name", ""), *menu.get("allergens", [])])
    return any(len(k) >= 2 and k in hay for k in keys) or any(a in keys for a in menu.get("allergens", []))


def order_notes(memory: dict | None, menu: dict, nutrition: dict | None, request: str = "") -> list[str]:
    """Short sentences to say before confirming ``menu`` (most important first, at most two)."""
    m = normalize(memory)
    p = m["profile"]
    name = menu.get("name", "")
    notes: list[str] = []
    for a in p["allergies"]:
        if _allergy_hit(a, menu):
            notes.append(f"{a} 알레르기가 있으셔서 걱정돼요.")
            break
    for d in p["dislikes"]:
        core = d.replace(" 정식", "").strip()
        if core and (core in name or name.replace(" 정식", "") in d):
            notes.append(f"전에 {_subject(name)} 별로라고 하셨어요.")
            break
    health = " ".join(p["conditions"] + p["doctor_advice"] + p["dislikes"])
    sodium = (nutrition or {}).get("sodium_mg")
    if SALT_WORDS.search(health) and sodium and sodium > SALTY_SODIUM_MG:
        notes.append(f"짠 음식을 줄이셔야 하는데, {_subject(name)} 조금 짤 수 있어요.")
    elif SUGAR_WORDS.search(health) and ("단호박" in name or ((nutrition or {}).get("sugars_g") or 0) > 15):
        notes.append(f"단 음식을 줄이셔야 하는데, {_subject(name)} 달 수 있어요.")
    remembered = " ".join(p["likes"] + p["habits"] + p["doctor_advice"] + [p["speech"]] + [n["text"] for n in m["notes"]])
    if REQUEST_WORDS.search(request or "") or REQUEST_WORDS.search(remembered):
        notes.append("간이나 양 조절 같은 요청은 앱으로 전달이 안 돼요.")
    return notes[:2]


def make_confirm_note(data, memory) -> Callable[[str, dict, str], object]:
    """The session's ``confirm_note`` hook for prepare_order: the question to ask, or
    ``{"block": reason}`` for a menu the memory says they are allergic to. ``memory``: a dict or
    a function returning the current one; ``data``: NeulbabData (menus and nutrition)."""
    get = memory if callable(memory) else (lambda: memory)
    by_name = {mm["name"]: mm for mm in data.menus}
    nutrition = (data.nutrition or {}).get("menus", data.nutrition or {})

    def note(issuer: str, result: dict, request: str):
        question = result.get("confirm_question") or ""
        menu = by_name.get(result.get("menu_name", ""))
        if issuer != "prepare_order" or not menu:
            return question
        allergy = next((a for a in normalize(get())["profile"]["allergies"] if _allergy_hit(a, menu)), None)
        if allergy:  # never ask to order a remembered allergen: the agent offers another menu
            return {"block": f"{menu['name']} conflicts with the remembered allergy ({allergy})."}
        notes = order_notes(get(), menu, nutrition.get(menu["id"]), request)
        speech = normalize(get())["profile"]["speech"]
        if NUMBER_WORDS.search(speech) and result.get("price_text") and question.endswith("주문할까요?"):
            question = question[: -len("주문할까요?")] + f"다시 말씀드리면 {result['price_text']}이에요. 주문할까요?"
        return " ".join(notes + [question])

    return note
