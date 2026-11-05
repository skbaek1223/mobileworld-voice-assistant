"""Voice assistant session: listen -> think/act inside the app -> speak.

One *request* = one spoken utterance handled by an agent loop:

    STT -> agent.predict(screen) -> guard -> tool call | GUI action -> scope check -> ...
        -> ask_user  : TTS question, STT answer, continue
        -> answer    : TTS reply, request done

The session keeps a few previous turns so follow-ups ("그거 취소해줘") have context.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from loguru import logger

from mobile_world.agents.base import BaseAgent
from mobile_world.assistant.app_tools import ToolRegistry
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


def _normalize(text: str) -> str:
    return "".join(ch for ch in text.lower().strip() if ch.isalnum() or ch.isspace()).strip()


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
    ):
        self.agent = agent
        self.device = device
        self.stt = stt
        self.tts = tts
        self.guard = guard
        self.tools = tools or ToolRegistry()
        self.app_name = app_name
        self.phrases = PHRASES.get(language, PHRASES["en"])
        self.max_steps = max_steps
        self.history_turns = history_turns
        self.max_empty_listens = max_empty_listens
        self.tool_wait_time = tool_wait_time
        self.on_event = on_event
        self.history: list[TurnResult] = []

    # ------------------------------------------------------------------ io helpers
    def _emit(self, state: State, **payload: Any) -> None:
        if self.on_event:
            try:
                self.on_event(state, payload)
            except Exception:
                logger.exception("on_event callback failed")

    def say(self, text: str) -> None:
        self._emit(State.SPEAKING, text=text)
        self.tts.speak(text)

    def hear(self, prompt: str | None = None) -> str:
        self._emit(State.LISTENING, prompt=prompt)
        text = self.stt.listen(prompt).strip()
        logger.info(f"[STT] {text!r}")
        return text

    def _matches(self, text: str, key: str) -> bool:
        norm = _normalize(text)
        return any(norm == _normalize(p) for p in self.phrases[key])

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
        if not recent:
            return f"User request (spoken): {request}"
        lines = ["Previous conversation (most recent last):"]
        for t in recent:
            lines.append(f"- User: {t.request}\n  Assistant: {t.reply}")
        lines.append(f"\nCurrent user request (spoken): {request}")
        return "\n".join(lines)

    def handle_request(self, request: str) -> TurnResult:
        start = time.time()
        steps: list[StepRecord] = []
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
                prediction, action = self.agent.predict(obs)
                record = StepRecord(step, prediction, action.model_dump(exclude_none=True))
                steps.append(record)
                logger.info(f"[step {step}] {record.action}")

                outcome = self._terminal_reply(prediction, action)
                if outcome is not None:
                    reply, status = outcome
                    break

                if action.action_type == ASK_USER:
                    answer = self._ask(action.text or "")
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
                record.feedback, screenshot = self._act(action)
                obs = {
                    "screenshot": screenshot,
                    "tool_call": record.feedback,
                    "ask_user_response": None,
                }
        except Exception as e:
            logger.exception(f"Request failed: {e}")
            reply, status = self.phrases["failed"], "failed"
        finally:
            self.agent.done()

        self.say(reply)
        result = TurnResult(request, reply, status, steps, time.time() - start)
        self.history.append(result)
        return result

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

    def _ask(self, question: str) -> str | None:
        """Speak the agent's question and return the answer (one retry on silence)."""
        for attempt in range(self.max_empty_listens):
            self.say(question)
            answer = self.hear()
            if answer:
                return answer
            logger.info(f"[ask_user] no answer (attempt {attempt + 1})")
        return None

    def _act(self, action: JSONAction):
        """Run a non-terminal action. Returns (feedback_for_agent, screenshot)."""
        rejection = self.guard.check_action(action)
        if rejection:
            logger.info(f"[scope] {rejection}")
            return rejection, self.device.screenshot()

        feedback, screenshot = None, None
        if action.action_type == MCP:
            feedback = self.tools.call(action.action_name, action.action_json, self.device)
            time.sleep(self.tool_wait_time)
        elif action.action_type == OPEN_APP:  # only reaches here for the host app itself
            self.device.launch(self.guard.package, self.guard.launch_activity)
        elif action.action_type == WAIT:
            time.sleep(max(self.tool_wait_time, 1.0))
        else:
            screenshot = self.device.execute(action)

        note = self.guard.ensure_in_scope(self.device)
        if note:
            feedback = f"{feedback}\n{note}" if feedback else note
            screenshot = None  # the screen changed during recovery
        return feedback, screenshot or self.device.screenshot()
