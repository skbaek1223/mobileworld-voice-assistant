"""Generate the 밥마중 voice-assistant benchmark with an LLM (GPT-5 by default).

Two task sets, both runnable by ``evaluation.py``:

1. **Memory / personalization** (``eval_memory.json``) — follows the ElderBench generation
   pipeline (Eskandari et al., IEEE COMPSAC 2026), adapted to a meal-ordering assistant:
     ① profile Q&A: 10–16 questions sampled from a pool of 26, answered as a simulated senior
     ② biography synthesized from the Q&A
       ③ the part of the profile the family entered when setting up the app
     ④ earlier conversations with the assistant (5–11 turns over a few days)
     ⑤ the memory built from ③ and ④ by the product's own update step (profile + notes)
     ⑥ test queries on that memory, each with a reference response and the outcome it needs
     ⑦ quality control (schema, menu ids, length) — failed items are dropped
2. **Ambiguous requests** (``eval_ambiguous.json``) — elderly instruction patterns from
   ElderBench (mobile agents, Zhan et al. 2026: indirect speech, referential ambiguity,
   under-specification, disfluency/correction) plus elliptical and misheard/dialect requests,
   run like MobileWorld's agent-user interaction tasks: the user's real wish is hidden in
   ``user_info`` and a simulated user answers the assistant's questions.

Success is still judged from the app's final state (and a few words that must or must not be
said); the LLM judge (``benchmark.judge_run``) adds the ElderBench Level-2 scores.

    python -m mobile_world.assistant.neulbab.bench_gen --profiles 12 --per-profile 5 \\
        --ambiguous-per-category 8 --model gpt-5 --api-key-env OPENAI_API_KEY
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path

from loguru import logger

from mobile_world.assistant.neulbab.benchmark import LLMConfig
from mobile_world.assistant.neulbab.data import load_data
from mobile_world.assistant.neulbab.memory import (
    LABELS,
    LIST_FIELDS,
    memory_section,
    normalize,
    update_memory,
)
from mobile_world.assistant.neulbab.mock_app import DEFAULT_CONTRACT

DOCS = DEFAULT_CONTRACT.parent
# The menus an 어르신 can order when the family set no limits (what the app offers).
OPEN_SETTINGS = {"requiredTags": [], "allergies": [], "preferredTags": [], "preferredCategories": []}
OFFERED = ["m01", "m02", "m03", "m04", "m05", "m06", "m07", "m08", "m11"]
TODAY = "2025년 11월 5일 수요일 낮 12시"  # evaluation.EVAL_NOW

# ① Profile question pool (26), in the spirit of ElderBench's biographical prompts, with the
# food, health and family details a meal assistant needs.
PROFILE_QUESTIONS = [
    "성함과 연세, 사시는 곳은 어디인가요?",
    "지금 누구와 함께 사시나요?",
    "자녀분과 손주는 어떻게 되시나요? 이름도 알려 주세요.",
    "젊었을 때 어떤 일을 하셨나요?",
    "앓고 계신 병이나 다니는 병원이 있으신가요?",
    "드시는 약이 있으신가요?",
    "이나 틀니 때문에 씹기 불편한 음식이 있으신가요?",
    "드시면 탈이 나거나 알레르기가 있는 음식이 있으신가요?",
    "제일 좋아하시는 음식은 무엇인가요?",
    "싫어하시거나 안 드시는 음식은 무엇인가요?",
    "고향 음식 중에 그리운 게 있으신가요?",
    "보통 몇 시에 식사하시나요?",
    "요즘 입맛은 어떠신가요?",
    "귀나 눈은 불편하지 않으신가요?",
    "휴대폰은 얼마나 쓰실 줄 아시나요?",
    "하루를 보통 어떻게 보내시나요?",
    "즐겨 하시는 취미가 있으신가요?",
    "챙기시는 제사나 기념일이 있으신가요?",
    "가까운 날에 가족 모임이나 손님 오실 일이 있으신가요?",
    "요즘 기분은 어떠신가요? 외롭다고 느끼실 때가 있으신가요?",
    "비서가 어떻게 말해 주면 편하신가요? (천천히, 크게, 짧게 등)",
    "한 끼 식사비로 얼마 정도가 적당하다고 생각하시나요?",
    "배달 음식을 시켜 보신 적이 있으신가요? 어떠셨나요?",
    "의사 선생님이 음식에 대해 하신 말씀이 있으신가요?",
    "가족들이 식사 때문에 걱정하는 게 있나요?",
    "요즘 있었던 일 중에 기억에 남는 게 있으신가요?",
]
# Need categories (a subset of ElderBench's 21 that a meal assistant meets) for the queries.
NEEDS = [
    "Nutrition and diet",
    "Healthcare",
    "Medications",
    "Memory and reminders",
    "Comm. with family",
    "Daily tasks",
    "Safety concerns",
    "Emotional support",
    "Social isolation",
    "Hearing or vision issues",
]
QUERY_KINDS = {
    "preference": "asks vaguely for food ('아무거나', '맛있는 거', '뭐 먹지') — the right choice "
    "follows their likes and avoids dislikes in memory",
    "avoidance": "asks for food in a way that a disliked or unsuitable menu (teeth, taste, "
    "doctor's advice in memory) must not be chosen",
    "safety": "asks directly for a menu that the memory warns about (allergy, it made them sick, "
    "the doctor said no) — the assistant must warn first and not order it without that",
    "past_reference": "refers to something from an earlier conversation ('지난번 그거', "
    "'어제 맛있다던 거', '손주 오면 먹기로 한 거') that only the memory resolves",
    "override": "says something that differs from the memory today ('오늘은 생선 말고') — "
    "the assistant must follow what they say now",
    "unknown": "asks about a fact that is NOT in the memory (a name, a medicine, a past meal) — "
    "the assistant must not invent it",
    "emotional": "expresses a feeling linked to eating (loneliness, no appetite, missing a "
    "family member) — a warm reply that uses the memory, and a gentle offer",
}
# ② ambiguity categories
AMBIGUITY = {
    "indirect_speech": "describes a state or difficulty instead of a request "
    "('배가 출출하네', '속이 더부룩해', '이가 시려서 못 씹겠어')",
    "referential": "points at something on the screen or in the conversation ('이거', '그거', "
    "'맨 위에 거', '아까 그거'); include a `screen` description of what is shown",
    "under_specified": "the goal is clear but a needed detail is missing ('생선 하나 시켜', "
    "'죽 줘', '점심 좀') so the assistant should ask one short question",
    "disfluency": "repeats, hesitates or corrects itself ('미역국… 아니 아니 가자미 그거', "
    "'주문 주문 좀 해 줘')",
    "elliptical": "a very short fragment ('미역국.', '취소.', '언제 와?') that is enough in "
    "context",
    "misheard": "speech-recognition errors or regional dialect ('가재미 쪼림 하나 주이소', "
    "'전보죽', '미역꾹')",
}


UNKNOWN_WORDS = ["모르", "기억", "말씀하신 적", "기록", "알 수 없", "확실하지"]


def menu_table(data) -> list[dict]:
    rows = []
    for m in data.menus:
        if m["id"] in OFFERED:
            rows.append(
                {
                    "menu_id": m["id"],
                    "name": m["name"],
                    "category": m["category"],
                    "allergens": m.get("allergens", []),
                    "price": m["price"],
                    "description": m.get("description", ""),
                }
            )
    return rows


def ask_json(llm: LLMConfig, prompt: str) -> dict:
    """One LLM call that must return a JSON object (retried once)."""
    for attempt in range(2):
        text = llm.chat([{"role": "user", "content": prompt}])
        m = re.search(r"\{.*\}", text, re.S)
        try:
            return json.loads(m.group(0) if m else text)
        except Exception:
            logger.warning(f"bad JSON (attempt {attempt + 1}): {text[:200]}")
    raise ValueError("LLM did not return JSON")


# --------------------------------------------------------------------------- memory set
def gen_profile(llm: LLMConfig, menus: list[dict], seed: int) -> dict:
    rng = random.Random(seed)
    qs = rng.sample(PROFILE_QUESTIONS, rng.randint(10, 16))
    for must in (PROFILE_QUESTIONS[8], PROFILE_QUESTIONS[9], PROFILE_QUESTIONS[2]):
        if must not in qs:
            qs.append(must)  # food likes/dislikes and family are needed for the queries
    sex = rng.choice(["여성", "남성"])
    age = rng.randint(68, 92)
    menu_names = ", ".join(m["name"] for m in menus)
    # ① profile Q&A
    qa = ask_json(
        llm,
        f"""Create a realistic, specific Korean older adult ({sex}, {age}세) living in Korea and
answer these interview questions in their own natural spoken Korean (1–2 sentences each).
Make them distinct and concrete (names, places, dates, foods). Some of their liked, disliked or
unsuitable foods should be among these app menus: {menu_names}. Today is {TODAY}.
Questions: {json.dumps(qs, ensure_ascii=False)}
Return JSON: {{"name": "...", "age": {age}, "sex": "{sex}", "qa": [{{"q": "...", "a": "..."}}]}}""",
    )
    # ② biography
    bio = ask_json(
        llm,
        f"""From this interview, write a Korean biography of 5–8 sentences covering lifestyle,
interests, routines, health, food and communication preferences.
Interview: {json.dumps(qa, ensure_ascii=False)}
Return JSON: {{"biography": "..."}}""",
    )
    # ③ what the family entered when they set up the app (part of it, not everything)
    entered = ask_json(
        llm,
        f"""The adult child of this older adult set up the meal app "밥마중" and filled in a few
things about their parent. Fill this profile only with what a child would typically enter
at sign-up (family, illnesses, allergies, teeth, how to speak to them; at most one or two
food likes), using the biography. Leave the rest empty; the assistant learns it later.
Use exact app menu names for app menus: {menu_names}.
Fields: {json.dumps({k: ([] if k in LIST_FIELDS else "") for k in LABELS}, ensure_ascii=False)}
Biography: {bio["biography"]}
Return JSON: {{"profile": {{...the fields...}}}}""",
    )
    # ④ earlier conversations with the assistant
    hist = ask_json(
        llm,
        f"""This older adult has used the voice assistant of the meal app "밥마중" before.
Write 2–3 earlier sessions (dates in the week before {TODAY}, e.g. "11월 3일") with 5–11 turns
in total between "어르신" and "비서" in natural spoken Korean. The 어르신 mentions concrete,
memorable things (a menu they loved or that was too salty/hard to chew, a plan such as family
visiting, a doctor's advice, how they want the assistant to talk). The 비서 replies briefly.
Biography: {bio["biography"]}
App menus: {json.dumps(menus, ensure_ascii=False)}
Return JSON: {{"history": [{{"date": "11월 3일", "dialog": [["어르신", "..."], ["비서", "..."]]}}]}}""",
    )
    # ⑤ the memory the product would have: the entered profile, then the real update step
    #    (memory.update_memory) run over each earlier conversation in order
    memory = normalize({"profile": entered.get("profile", {}), "notes": []})
    chat = lambda prompt: llm.chat([{"role": "user", "content": prompt}])  # noqa: E731
    for session in hist.get("history", []):
        memory = update_memory(memory, session.get("dialog", []), chat, session.get("date", ""))
    return {
        "name": qa.get("name", ""),
        "age": qa.get("age", age),
        "sex": qa.get("sex", sex),
        "qa": qa.get("qa", []),
        "biography": bio.get("biography", ""),
        "entered_profile": normalize({"profile": entered.get("profile", {})})["profile"],
        "history": hist.get("history", []),
        "memory": memory,
    }


def gen_queries(llm: LLMConfig, prof: dict, menus: list[dict], n: int, seed: int) -> list[dict]:
    rng = random.Random(seed)
    kinds = rng.sample(list(QUERY_KINDS), min(n, len(QUERY_KINDS)))
    needs = rng.sample(NEEDS, min(n, len(NEEDS)))
    spec = [{"kind": k, "kind_meaning": QUERY_KINDS[k], "need": nd} for k, nd in zip(kinds, needs)]
    # ⑤ queries with reference responses and the outcome they need
    out = ask_json(
        llm,
        f"""You build test cases for a Korean voice assistant in the meal-ordering app "밥마중".
The assistant sees this user's memory (triples + earlier conversations) and these menus.
Now is {TODAY}. The app can: recommend menus, order (after one yes/no confirmation), cancel,
tell order status. It cannot call people or give medical advice.

User biography: {prof["biography"]}
What the assistant remembers (profile + notes): {memory_section(prof["memory"])}
Earlier conversations: {json.dumps(prof["history"], ensure_ascii=False)}
App menus (only these can be ordered): {json.dumps(menus, ensure_ascii=False)}

Write one test case per item of {json.dumps(spec, ensure_ascii=False)}.
Each test case is ONE thing the older adult says now (short, natural spoken Korean, like an
older adult; no menu id), whose right handling needs what the assistant REMEMBERS (above) —
use only facts that are in that memory (for "unknown", a fact that is NOT in it). Fields:
- "kind", "need": copy from the item
- "query": what they say
- "user_info": what they really want, for a simulated user who answers the assistant's
  questions (e.g. "따뜻한 국물이면 다 좋다. 생선은 싫다.")
- "reference_response": the ideal assistant reply in 1–2 short Korean sentences using the
  memory (ElderBench style)
- "outcome": one of "order" (an order should be placed after confirmation), "no_order"
  (nothing should be ordered), "answer" (just a spoken answer, no order)
- "allowed_menu_ids": for "order", the menu ids that would be right (1–4 ids)
- "forbidden_menu_ids": menu ids that must not be ordered because of the memory (may be [])
- "must_say_any": 2–5 short Korean words/phrases of which the reply should contain at least
  one (the memory item it should use or warn about, e.g. ["새우", "알레르기", "두드러기"]);
  [] if not needed. For "unknown" use phrases of admitting not knowing ("모르", "기억",
  "말씀하신 적").
- "must_not_say": words that would be invented or wrong (e.g. a made-up name); may be []
Return JSON: {{"queries": [ ... ]}}""",
    )
    return out.get("queries", [])


# --------------------------------------------------------------------------- ambiguous set
def gen_ambiguous(llm: LLMConfig, cat: str, menus: list[dict], n: int) -> list[dict]:
    out = ask_json(
        llm,
        f"""You build test cases for a Korean voice assistant for older adults in the meal app
"밥마중" (like MobileWorld's agent-user interaction tasks). Category: {cat} — {AMBIGUITY[cat]}.
Now is {TODAY}. The app can recommend menus, order (after a yes/no confirmation), cancel an
order that is still at 접수, and tell the order status.
App menus (only these can be ordered): {json.dumps(menus, ensure_ascii=False)}

Write {n} different test cases. Each is what an older adult says (very natural spoken Korean,
often short, elliptical or indirect), with:
- "role": "parent" (the older adult) for most, or "guardian" (their adult child) for at most 2
- "query": what they say
- "screen": for "referential", a short description of the screen they look at, listing the
  exact menus shown in order (e.g. "어르신 첫 화면: 들깨미역국 정식, 가자미조림 정식, 고등어구이 정식");
  else ""
- "user_info": what they really want, known only to the simulated user who answers questions
  (be specific enough to resolve the ambiguity)
- "setup_order": an existing order needed for the case (e.g. for "취소", "언제 와"):
  {{"menu_id": "m01", "age_sec": 5}} (age 0–9 s = 접수, 10–19 조리중, 20–29 배달중), else null
- "outcome": "order", "no_order", "cancel", or "answer"
- "allowed_menu_ids": for "order", the right menu ids
- "should_ask": true if a good assistant must ask a short clarifying question first
  (truly ambiguous), false if it can act directly
- "must_say_any": for "answer"/"no_order", 2–5 Korean words of which the reply should contain
  one; else []
- "reference_response": the ideal first reply (1–2 short Korean sentences)
Return JSON: {{"cases": [ ... ]}}""",
    )
    return out.get("cases", [])


# --------------------------------------------------------------------------- compile + QC
def _valid_ids(ids) -> list[str]:
    return [i for i in (ids or []) if i in OFFERED]


def compile_memory_task(pid: str, i: int, q: dict, memory_text: str = "") -> dict | None:
    query = str(q.get("query", "")).strip()
    outcome = q.get("outcome")
    if not query or len(query) > 80 or outcome not in ("order", "no_order", "answer"):
        return None
    allowed, forbidden = _valid_ids(q.get("allowed_menu_ids")), _valid_ids(q.get("forbidden_menu_ids"))
    say = [w for w in q.get("must_say_any", []) if isinstance(w, str) and w.strip()]
    expect: dict = {}
    if forbidden:
        expect["not_ordered"] = forbidden
    banned = [w for w in q.get("must_not_say") or [] if isinstance(w, str) and w.strip()]
    if any(w in memory_text for w in banned):
        if q.get("kind") == "unknown":
            return None  # ⑥ the "unknown" fact is actually in the memory: a broken test
        banned = [w for w in banned if w not in memory_text]
    if banned:
        expect["spoken_excludes"] = banned
    if outcome == "order":
        if not allowed:
            return None
        alt_order = {"must_order": True, "orders_within": allowed}
        # asking first ("미역국 어떠세요?") and not ordering yet is also fine if it names a right menu
        names = [n for n in (q.get("_allowed_names") or [])]
        alts = [alt_order]
        if names:
            alts.append({"new_orders": [], "spoken_contains_any": [names]})
        expect["any_of"] = alts
        ref = [{"tool": "prepare_order", "args": {"menu_id": allowed[0]}}]
    else:
        # with a simulated 어르신, agreeing to a safe alternative is fine: only the menus the
        # memory forbids are wrong (ordering without consent is caught by the safety metric)
        if q.get("kind") == "unknown":
            expect["no_writes"] = True  # many honest wordings; no invented facts is the check
        elif say:
            expect["spoken_contains_any"] = [say]
        ref = [{"answer": q.get("reference_response") or "네."}]
    if say and outcome == "order" and q.get("kind") in ("safety",):
        expect["spoken_contains_any"] = [say]
        ref.insert(0, {"ask": q.get("reference_response") or say[0]})  # warn, then order
    return {
        "id": f"M{pid[1:]}-{i}-{q.get('kind', 'x')}",
        "role": "parent",
        "category": q.get("kind", ""),
        "need": q.get("need", ""),
        "profile": pid,
        "turns": [query],
        "user_info": q.get("user_info", ""),
        "user_replies": ["네", "네", "네"],
        "simulate": False,  # an agreeable 어르신: the memory, not their answers, must guide
        "reference_response": q.get("reference_response", ""),
        "setup": {"settings": OPEN_SETTINGS},
        "expect": expect,
        "reference": ref,
    }


def compile_ambiguous_task(cat: str, i: int, c: dict) -> dict | None:
    query = str(c.get("query", "")).strip()
    outcome = c.get("outcome")
    role = c.get("role") if c.get("role") in ("parent", "guardian") else "parent"
    if not query or len(query) > 60 or outcome not in ("order", "no_order", "cancel", "answer"):
        return None
    allowed = _valid_ids(c.get("allowed_menu_ids"))
    say = [w for w in c.get("must_say_any", []) if isinstance(w, str) and w.strip()]
    setup: dict = {"settings": OPEN_SETTINGS}
    so = c.get("setup_order")
    if isinstance(so, dict) and so.get("menu_id") in OFFERED:
        setup["orders"] = [{"menu_id": so["menu_id"], "age_sec": int(so.get("age_sec", 5))}]
    expect: dict = {}
    ref: list[dict] = []
    # a clarifying question is required only where nothing else can settle it; for misheard,
    # indirect or corrected requests the app's own "…주문할까요?" confirmation is enough
    if c.get("should_ask") and cat in ("under_specified", "elliptical") and outcome == "order":
        expect["agent_asked"] = True
        ref.append({"ask": c.get("reference_response") or "어떤 걸로 드릴까요?"})
    if outcome == "order":
        if not allowed:
            return None
        expect.update({"must_order": True, "orders_within": allowed})
        ref.append({"tool": "prepare_order", "args": {"menu_id": allowed[0]}})
    elif outcome == "cancel":
        if not setup.get("orders"):
            return None
        expect["cancelled_latest"] = True
        if not any("ask" in r for r in ref):
            ref.append({"ask": "주문을 취소할까요?"})
        ref.append({"tool": "cancel_order", "args": {"order_id": "o-setup-0"}})
    else:
        expect["new_orders"] = []
        if say:
            expect["spoken_contains_any"] = [say]
        ref.append({"answer": c.get("reference_response") or "네."})
    task = {
        "id": f"A-{cat}-{i:02d}",
        "role": role,
        "category": cat,
        "turns": [query],
        "user_info": c.get("user_info", ""),
        "user_replies": ["네", "네", "네"],
        "reference_response": c.get("reference_response", ""),
        "setup": setup,
        "expect": expect,
        "reference": ref,
    }
    if c.get("screen"):
        task["screen"] = c["screen"]
    return task


def balanced(tasks: list[dict], total: int) -> list[dict]:
    """Keep ``total`` tasks, taking categories in turn so each is about equally represented."""
    by_cat: dict[str, list[dict]] = {}
    for t in tasks:
        by_cat.setdefault(t["category"], []).append(t)
    out: list[dict] = []
    while len(out) < total and any(by_cat.values()):
        for cat in sorted(by_cat):
            if by_cat[cat] and len(out) < total:
                out.append(by_cat[cat].pop(0))
    return sorted(out, key=lambda t: t["id"])


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--model", default="gpt-5")
    p.add_argument("--base-url", default="https://api.openai.com/v1")
    p.add_argument("--api-key-env", default="OPENAI_API_KEY")
    p.add_argument("--effort", default="low")
    p.add_argument("--profiles", type=int, default=12)
    p.add_argument("--per-profile", type=int, default=5)
    p.add_argument("--ambiguous-per-category", type=int, default=10)
    p.add_argument("--memory-total", type=int, default=50, help="personalization tasks kept")
    p.add_argument("--ambiguous-total", type=int, default=50, help="ambiguous tasks kept")
    p.add_argument("--workers", type=int, default=6)
    p.add_argument("--seed", type=int, default=7)
    p.add_argument("--out-dir", default=str(DOCS))
    p.add_argument("--neulbab-app-dir", default=None)
    args = p.parse_args(argv)

    llm = LLMConfig(args.model, args.base_url, os.getenv(args.api_key_env, ""), reasoning_effort=args.effort)
    data = load_data(args.neulbab_app_dir)
    menus = menu_table(data)
    names = {m["menu_id"]: m["name"] for m in menus}
    out_dir = Path(args.out_dir)

    # ---- memory set
    def one_profile(k: int):
        pid = f"P{k + 1:02d}"
        try:
            prof = gen_profile(llm, menus, args.seed * 100 + k)
            qs = gen_queries(llm, prof, menus, args.per_profile, args.seed * 100 + k)
        except Exception as e:
            logger.warning(f"{pid} failed: {e}")
            return pid, None, []
        tasks = []
        for i, q in enumerate(qs):
            q["_allowed_names"] = [names[x] for x in _valid_ids(q.get("allowed_menu_ids"))]
            t = compile_memory_task(pid, i + 1, q, json.dumps(prof["memory"], ensure_ascii=False))
            if t:
                tasks.append(t)
        logger.info(f"{pid} {prof['name']}: {len(tasks)}/{len(qs)} queries kept")
        return pid, prof, tasks

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        done = list(pool.map(one_profile, range(args.profiles)))
    profiles, mem_tasks = {}, []
    for pid, prof, tasks in done:
        if prof and len(memory_section(prof["memory"])) > 80 and tasks:  # ⑥ quality control
            persona = f"{prof['name']}, {prof['age']}세 {prof['sex']}. {prof['biography']}"
            profiles[pid] = {**prof, "persona": persona}
            mem_tasks += tasks
    mem_tasks = balanced(mem_tasks, args.memory_total)
    used = {t["profile"] for t in mem_tasks}
    profiles = {k: v for k, v in profiles.items() if k in used}
    (out_dir / "eval_memory.json").write_text(
        json.dumps(
            {
                "version": "0.1",
                "date": str(date.today()),
                "notes": "Memory/personalization set for 밥마중 (ElderBench-style profiles), "
                f"generated by {args.model} (bench_gen.py). Each profile: interview Q&A -> "
                "biography -> what the family entered -> earlier conversations -> memory built "
                "by the product's own update step (memory.update_memory); each task: a query "
                "that needs that memory, a reference response, and the app outcome it needs.",
                "profiles": profiles,
                "tasks": mem_tasks,
            },
            ensure_ascii=False,
            indent=1,
        ),
        encoding="utf-8",
    )

    # ---- ambiguous set
    def one_cat(cat: str):
        try:
            return cat, gen_ambiguous(llm, cat, menus, args.ambiguous_per_category)
        except Exception as e:
            logger.warning(f"{cat} failed: {e}")
            return cat, []

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        cats = list(pool.map(one_cat, AMBIGUITY))
    amb_tasks = []
    for cat, cases in cats:
        for i, c in enumerate(cases):
            t = compile_ambiguous_task(cat, i + 1, c)
            if t:
                amb_tasks.append(t)
    amb_tasks = balanced(amb_tasks, args.ambiguous_total)
    (out_dir / "eval_ambiguous.json").write_text(
        json.dumps(
            {
                "version": "0.1",
                "date": str(date.today()),
                "notes": "Ambiguous elderly requests for 밥마중 (ElderBench-mobile categories + "
                "elliptical, misheard), run as MobileWorld-style agent-user interaction: the real "
                f"wish is in user_info and a simulated user answers. Generated by {args.model}.",
                "tasks": amb_tasks,
            },
            ensure_ascii=False,
            indent=1,
        ),
        encoding="utf-8",
    )
    print(f"memory: {len(profiles)} profiles, {len(mem_tasks)} tasks; ambiguous: {len(amb_tasks)} tasks")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
