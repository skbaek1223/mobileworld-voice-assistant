"""Voice assistant session: listen -> think/act inside the app -> speak.

One *request* = one spoken utterance:

    router (optional, no LLM) ──handled──> speak
        │ not handled
        v
    agent loop: agent.predict -> guard -> tool call | GUI action -> scope check -> ...
        -> ask_user  : TTS question, STT answer, continue
        -> answer    : TTS reply, request done

Latency features (each optional):
- ``router``: frequent requests ("언제 와?", "취소해 줘") are handled by rules, without the LLM.
- ``context_provider``: the app state is fetched up front and put in the request, so the
  agent does not spend LLM round trips on lookups.
- ``commit_tools``: when a tool returns an app-made ``confirm_question`` (e.g. prepare_order),
  the session asks it, understands yes/no itself and runs the commit tool. The agent never
  sees the confirmation id, so it cannot commit without the user's yes.
- ``filler_after_sec``: if the agent is still thinking after this long, say a short filler.
- streaming: an agent that supports ``speech_sink`` speaks its reply sentence by sentence
  while it is still being generated.

The session keeps a few previous turns so follow-ups ("그거 취소해줘") have context.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from loguru import logger

from mobile_world.agents.base import BaseAgent
from mobile_world.assistant.app_tools import ToolRegistry
from mobile_world.assistant.confirm import classify_yes_no, is_bare_no
from mobile_world.assistant.device import DeviceBackend
from mobile_world.assistant.scope import AppScopeGuard
from mobile_world.assistant.speech import SpeechToText, TextToSpeech
from mobile_world.runtime.utils.models import (
    ANSWER,
    ASK_USER,
    ENV_FAIL,
    FINISHED,
    MCP,
    OPEN_APP,
    STATUS,
    UNKNOWN,
    WAIT,
    JSONAction,
)

PHRASES = {
    "ko": {
        "greeting": "{app} 비서예요. 무엇을 도와드릴까요?",
        "listening": "말씀하세요",
        "done": "완료했어요.",
        "failed": "죄송해요, 요청을 처리하지 못했어요.",
        "max_steps": "시간이 너무 오래 걸려서 중단했어요. 다시 말씀해 주세요.",
        "cancelled": "취소했어요.",
        "bye": "비서를 종료할게요.",
        "no_answer": "답변을 듣지 못해서 요청을 중단했어요.",
        "declined": "알겠어요. 하지 않을게요.",
        "say_yes_no": "네, 또는 아니요로 말씀해 주세요.",
        "fillers": ("잠시만요.",),
        "exit": ("종료", "비서 종료", "그만", "끝"),
        "cancel": ("취소", "그만해", "됐어", "멈춰"),
    },
    "en": {
        "greeting": "Hi, I'm the {app} assistant. How can I help?",
        "listening": "Listening",
        "done": "Done.",
        "failed": "Sorry, I couldn't complete that.",
        "max_steps": "That was taking too long, so I stopped. Please try again.",
        "cancelled": "Cancelled.",
        "bye": "Goodbye.",
        "no_answer": "I didn't hear an answer, so I stopped.",
        "declined": "Okay, I won't do that.",
        "say_yes_no": "Please answer yes or no.",
        "fillers": ("One moment.",),
        "exit": ("exit", "quit", "goodbye", "stop assistant"),
        "cancel": ("cancel", "stop", "never mind"),
    },
}

# GeneralE2E maps `status` to these fixed answers; replace them with natural speech.
_STATUS_TEXTS = {"task finished": "done", "task failed": "failed"}


class State(StrEnum):
    IDLE = "idle"
    LISTENING = "listening"
    THINKING = "thinking"
    ACTING = "acting"
    SPEAKING = "speaking"


@dataclass
class StepRecord:
    step: int
    prediction: str | None
    action: dict[str, Any]
    feedback: str | None = None


@dataclass
class TurnResult:
    request: str
    reply: str
    status: str  # answered | failed | max_steps | cancelled
    steps: list[StepRecord] = field(default_factory=list)
    duration: float = 0.0
    # everything said during the request, in order: ("Assistant" | "User", text)
    dialog: list[tuple[str, str]] = field(default_factory=list)
    first_speech_sec: float | None = None  # request start -> first sound (filler included)
    llm_calls: int = 0
    routed: bool = False  # handled by the router, without the LLM


def _normalize(text: str) -> str:
    return "".join(ch for ch in text.lower().strip() if ch.isalnum() or ch.isspace()).strip()


def _unspoken_rest(full: str, spoken: str) -> str:
    """Part of ``full`` not yet spoken, comparing without whitespace ('' if all spoken)."""
    target = "".join(spoken.split())
    if not target:
        return full
    seen = 0
    for i, ch in enumerate(full):
        if ch.isspace():
            continue
        if seen >= len(target) or ch != target[seen]:
            return full if seen < len(target) else full[i:].strip()
        seen += 1
        if seen == len(target):
            return full[i + 1 :].strip()
    return ""


class RouteContext:
    """What a router may do: call tools and ask the user to confirm. Every call is recorded
    as a step (marked ``by: router``) so evaluations can audit it like agent actions."""

    def __init__(self, session: VoiceAssistantSession):
        self.session = session
        self.steps: list[StepRecord] = []

    def _record(self, action: dict, feedback: str | None = None) -> None:
        self.steps.append(StepRecord(len(self.steps) + 1, "router", action, feedback))

    def tool(self, name: str, **args: Any) -> dict:
        raw = self.session.tools.call(name, args, self.session.device)
        self._record(
            {"action_type": MCP, "action_name": name, "action_json": args, "by": "router"}, raw
        )
        try:
            return json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            return {"ok": False, "error": {"code": "TOOL_OUTPUT", "message": str(raw)}}

    def confirm(self, question: str) -> str:
        """'yes' | 'no' | 'silent'. Unclear answers count as no."""
        self._record({"action_type": ASK_USER, "text": question, "by": "router"})
        return self.session._confirm(question)


class VoiceAssistantSession:
    def __init__(
        self,
        agent: BaseAgent,
        device: DeviceBackend,
        stt: SpeechToText,
        tts: TextToSpeech,
        guard: AppScopeGuard,
        tools: ToolRegistry | None = None,
        app_name: str = "앱",
        language: str = "ko",
        max_steps: int = 20,
        history_turns: int = 3,
        max_empty_listens: int = 2,
        tool_wait_time: float = 1.0,
        on_event: Callable[[State, dict[str, Any]], None] | None = None,
        router: Callable[[str, RouteContext], str | None] | None = None,
        context_provider: Callable[[], str] | None = None,
        commit_tools: dict[str, str] | None = None,
        filler_after_sec: float | None = None,
        stream_speech: bool = True,
    ):
        self.agent = agent
        self.device = device
        self.stt = stt
        self.tts = tts
        self.guard = guard
        self.tools = tools or ToolRegistry()
        self.app_name = app_name
        self.language = language
        self.phrases = PHRASES.get(language, PHRASES["en"])
        self.max_steps = max_steps
        self.history_turns = history_turns
        self.max_empty_listens = max_empty_listens
        self.tool_wait_time = tool_wait_time
        self.on_event = on_event
        self.router = router
        self.context_provider = context_provider
        self.commit_tools = dict(commit_tools or {})
        self.filler_after_sec = filler_after_sec
        self.stream_speech = stream_speech
        self.history: list[TurnResult] = []
        self._dialog: list[tuple[str, str]] = []
        self._start = 0.0
        self._first_speech: float | None = None
        self._filler_used = False
        self._streamed = ""  # text the agent already spoke by streaming, this LLM step
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ io helpers
    def _emit(self, state: State, **payload: Any) -> None:
        if self.on_event:
            try:
                self.on_event(state, payload)
            except Exception:
                logger.exception("on_event callback failed")

    def _speak(self, text: str) -> None:
        with self._lock:
            if self._first_speech is None:
                self._first_speech = time.time() - self._start
        self.tts.speak(text)

    def say(self, text: str) -> None:
        self._emit(State.SPEAKING, text=text)
        self._dialog.append(("Assistant", text))
        self._speak(text)

    def hear(self, prompt: str | None = None) -> str:
        self._emit(State.LISTENING, prompt=prompt)
        text = self.stt.listen(prompt).strip()
        logger.info(f"[STT] {text!r}")
        return text

    def _matches(self, text: str, key: str) -> bool:
        norm = _normalize(text)
        return any(norm == _normalize(p) for p in self.phrases[key])

    def _stream_sentence(self, sentence: str) -> None:
        """Speech sink for streaming agents: speak a finished sentence right away."""
        self._cancel_filler()
        self._streamed += sentence + " "
        self.say(sentence)

    # ------------------------------------------------------------------ main loop
    def run_forever(self) -> None:
        self.say(self.phrases["greeting"].format(app=self.app_name))
        while True:
            self._emit(State.IDLE)
            request = self.hear(self.phrases["listening"])
            if not request:
                continue
            if self._matches(request, "exit"):
                self.say(self.phrases["bye"])
                return
            self.handle_request(request)

    def _build_instruction(self, request: str) -> str:
        recent = self.history[-self.history_turns :] if self.history_turns > 0 else []
        lines = []
        if recent:
            lines.append("Previous conversation (most recent last):")
            for t in recent:
                # keep what was actually said, so an unanswered question (e.g. a menu list
                # ending in "어느 걸 드실까요?") still gives context to the follow-up request
                lines.append(f"- User: {t.request}")
                lines += [
                    f"  {who}: {text}" for who, text in (t.dialog or [("Assistant", t.reply)])
                ]
            lines.append("")
        if self.context_provider:
            try:
                lines += [self.context_provider(), ""]
            except Exception:
                logger.exception("context_provider failed; continuing without it")
        lines.append(f"{'Current u' if recent else 'U'}ser request (spoken): {request}")
        return "\n".join(lines)

    def handle_request(self, request: str) -> TurnResult:
        self._start = time.time()
        self._first_speech = None
        self._filler_used = False
        self._streamed = ""
        self._dialog = []
        steps: list[StepRecord] = []

        if self.router:
            ctx = RouteContext(self)
            try:
                routed = self.router(request, ctx)
            except Exception:
                logger.exception("router failed; falling back to the agent")
                routed = None
            if routed is not None:
                reply, status = routed if isinstance(routed, tuple) else (routed, "answered")
                logger.info(f"[router] handled {request!r}")
                return self._finish(request, reply, status, ctx.steps, llm_calls=0, routed=True)
            steps += ctx.steps  # e.g. lookups made before the router gave up

        llm_calls = 0
        self.agent.initialize(self._build_instruction(request))
        feedback = self.guard.ensure_in_scope(self.device)
        obs: dict[str, Any] = {
            "screenshot": self.device.screenshot(),
            "tool_call": feedback,
            "ask_user_response": None,
        }
        reply, status = self.phrases["max_steps"], "max_steps"
        try:
            for step in range(1, self.max_steps + 1):
                self._emit(State.THINKING, step=step)
                prediction, action = self._predict(obs)
                llm_calls += 1
                record = StepRecord(step, prediction, action.model_dump(exclude_none=True))
                steps.append(record)
                logger.info(f"[step {step}] {record.action}")

                outcome = self._terminal_reply(prediction, action)
                if outcome is not None:
                    reply, status = outcome
                    break

                if action.action_type == ASK_USER:
                    question = action.text or ""
                    rest = self._take_streamed(question)
                    if rest and rest != question:
                        self.say(rest)  # finish a partly streamed question
                    answer = self._ask(question, already_spoken=rest != question)
                    if answer is None:
                        reply, status = self.phrases["no_answer"], "cancelled"
                        break
                    if self._matches(answer, "cancel") or self._matches(answer, "exit"):
                        reply, status = self.phrases["cancelled"], "cancelled"
                        break
                    obs = {
                        "screenshot": self.device.screenshot(),
                        "tool_call": None,
                        "ask_user_response": answer,
                    }
                    continue

                self._emit(State.ACTING, step=step, action=record.action)
                record.feedback, screenshot, final = self._act(action, steps)
                if final is not None:
                    reply, status = final
                    break
                obs = {
                    "screenshot": screenshot,
                    "tool_call": record.feedback,
                    "ask_user_response": None,
                }
        except Exception as e:
            logger.exception(f"Request failed: {e}")
            reply, status = self.phrases["failed"], "failed"
        finally:
            self._cancel_filler()
            self.agent.done()
        return self._finish(request, reply, status, steps, llm_calls=llm_calls)

    def _finish(self, request, reply, status, steps, llm_calls, routed=False) -> TurnResult:
        rest = self._take_streamed(reply)
        if rest:
            self.say(rest)
        result = TurnResult(
            request,
            reply,
            status,
            steps,
            time.time() - self._start,
            list(self._dialog),
            None if self._first_speech is None else round(self._first_speech, 3),
            llm_calls,
            routed,
        )
        self.history.append(result)
        return result

    # ------------------------------------------------------------------ agent call
    def _predict(self, obs: dict):
        """agent.predict with an optional filler and sentence streaming."""
        self._streamed = ""
        sink_set = False
        if self.stream_speech and hasattr(self.agent, "speech_sink"):
            self.agent.speech_sink = self._stream_sentence
            sink_set = True
        self._start_filler()
        try:
            return self.agent.predict(obs)
        finally:
            self._cancel_filler()
            if sink_set:
                self.agent.speech_sink = None

    def _take_streamed(self, full: str) -> str:
        """What is left to say of ``full`` after streaming; resets the stream buffer."""
        streamed, self._streamed = self._streamed, ""
        return _unspoken_rest(full, streamed) if streamed else full

    def _start_filler(self) -> None:
        self._filler_timer = None
        if self.filler_after_sec is None or self._filler_used or self._first_speech is not None:
            return

        def fire():
            if not self._filler_used and self._first_speech is None:
                self._filler_used = True
                self._speak(self.phrases["fillers"][0])

        self._filler_timer = threading.Timer(self.filler_after_sec, fire)
        self._filler_timer.daemon = True
        self._filler_timer.start()

    def _cancel_filler(self) -> None:
        timer = getattr(self, "_filler_timer", None)
        if timer is not None:
            timer.cancel()
            self._filler_timer = None

    # ------------------------------------------------------------------ pieces
    def _terminal_reply(self, prediction: str | None, action: JSONAction) -> tuple[str, str] | None:
        t = action.action_type
        if prediction is None or t in (UNKNOWN, ENV_FAIL):
            return self.phrases["failed"], "failed"
        if t == ANSWER:
            text = (action.text or "").strip()
            key = _STATUS_TEXTS.get(text.lower())
            if key:
                return self.phrases[key], "answered" if key == "done" else "failed"
            return text or self.phrases["done"], "answered"
        if t == FINISHED or (t == STATUS and action.goal_status == "complete"):
            return self.phrases["done"], "answered"
        if t == STATUS:
            return self.phrases["failed"], "failed"
        return None

    def _ask(self, question: str, already_spoken: bool = False) -> str | None:
        """Speak a question and return the answer (one retry on silence)."""
        for attempt in range(self.max_empty_listens):
            if not (already_spoken and attempt == 0):
                self.say(question)
            answer = self.hear()
            if answer:
                self._dialog.append(("User", answer))
                return answer
            logger.info(f"[ask_user] no answer (attempt {attempt + 1})")
        return None

    def _confirm(self, question: str) -> str:
        """Ask a yes/no question; 'yes' | 'no' | 'silent'. Unclear twice counts as no."""
        answer = self._ask(question)
        if answer is None:
            return "silent"
        verdict = classify_yes_no(answer, self.language)
        if verdict == "unclear":
            answer = self._ask(self.phrases["say_yes_no"])
            if answer is None:
                return "silent"
            verdict = classify_yes_no(answer, self.language)
        self._last_confirm_answer = answer
        return "yes" if verdict == "yes" else "no"

    def _session_confirm(self, issuer: str, raw: str, steps: list[StepRecord]):
        """Handle an app-issued confirmation. Returns (feedback, final) like ``_act``."""
        try:
            result = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            return raw, None
        data = result.get("data") or {}
        if not result.get("ok") or "confirmation_id" not in data:
            return raw, None
        question = data.get("confirm_question") or ""
        commit = self.commit_tools[issuer]
        steps.append(
            StepRecord(
                len(steps) + 1,
                "session",
                {"action_type": ASK_USER, "text": question, "by": "session"},
            )
        )
        verdict = self._confirm(question)
        if verdict == "silent":
            return None, (self.phrases["no_answer"], "cancelled")
        if verdict == "no":
            said = getattr(self, "_last_confirm_answer", "")
            if is_bare_no(said, self.language):
                return None, (self.phrases["declined"], "cancelled")
            # a refusal that carries a new wish ("아니, 전복죽으로") goes back to the agent
            return (
                f'Note: the user did NOT confirm (they said: "{said}"). Nothing was done.',
                None,
            )
        out = self.tools.call(commit, {"confirmation_id": data["confirmation_id"]}, self.device)
        steps.append(
            StepRecord(
                len(steps) + 1,
                "session",
                {"action_type": MCP, "action_name": commit, "action_json": {}, "by": "session"},
                out,
            )
        )
        try:
            done = json.loads(out)
        except (TypeError, json.JSONDecodeError):
            return f"Tool call result ({commit}): {out}", None
        if done.get("ok") and done.get("say"):
            return None, (done["say"], "answered")
        # failure (e.g. the state changed meanwhile): let the agent explain it
        return f"The user confirmed, then {commit} returned: {out}", None

    def _act(self, action: JSONAction, steps: list[StepRecord] | None = None):
        """Run a non-terminal action. Returns (feedback_for_agent, screenshot, final_reply).

        ``final_reply`` is (reply, status) when the request is finished without another LLM
        step (an app-confirmed commit), else None."""
        rejection = self.guard.check_action(action)
        if rejection:
            logger.info(f"[scope] {rejection}")
            return rejection, self.device.screenshot(), None

        feedback, screenshot, final = None, None, None
        if action.action_type == MCP:
            feedback = self.tools.call(action.action_name, action.action_json, self.device)
            if action.action_name in self.commit_tools:
                feedback, final = self._session_confirm(
                    action.action_name, feedback, steps if steps is not None else []
                )
            time.sleep(self.tool_wait_time)
        elif action.action_type == OPEN_APP:  # only reaches here for the host app itself
            self.device.launch(self.guard.package, self.guard.launch_activity)
        elif action.action_type == WAIT:
            time.sleep(max(self.tool_wait_time, 1.0))
        else:
            screenshot = self.device.execute(action)

        if final is not None:
            return feedback, None, final
        note = self.guard.ensure_in_scope(self.device)
        if note:
            feedback = f"{feedback}\n{note}" if feedback else note
            screenshot = None  # the screen changed during recovery
        return feedback, screenshot or self.device.screenshot(), None
