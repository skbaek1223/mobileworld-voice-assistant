"""Hybrid (app tools + GUI) agent tuned for an in-app voice assistant.

Reuses MobileWorld's ``GeneralE2EAgentMCP`` (history handling, coordinate conventions for
Claude/Gemini/Qwen/Kimi, output parsing) and only changes:
- the system prompt (in-app role, spoken replies, confirmation rule, tools-first);
- feedback turns keep the screenshot next to the text (tool result / user answer /
  guard message), so the agent never acts blind after a non-GUI step.
"""

from __future__ import annotations

import re
from typing import Any

from loguru import logger

from mobile_world.agents.implementations.general_e2e_agent import GeneralE2EAgentMCP
from mobile_world.assistant.prompts import (
    DEFAULT_ANSWER_EXAMPLE,
    DEFAULT_ASK_EXAMPLE,
    LANGUAGE_NAMES,
    build_system_prompt,
)
from mobile_world.runtime.utils.models import JSONAction

# Reasoning models reject a non-default temperature and spend completion tokens on hidden
# reasoning, so they need no temperature and a larger budget.
_REASONING_MODEL = re.compile(r"(^|/)(gpt-5|o1|o3|o4)", re.IGNORECASE)


def default_runtime_conf(
    model_name: str, gui: bool = True, reasoning_effort: str | None = None
) -> dict:
    """``reasoning_effort`` (minimal | low | medium | high) applies to reasoning models only."""
    images = 2 if gui else 0
    if "qwen3" in model_name.lower():  # open model served by vLLM: skip the <think> pass
        conf = {"history_n_images": images, "temperature": 0.0, "max_tokens": 2048}
        if reasoning_effort in (None, "minimal", "low"):
            conf["extra_body"] = {"chat_template_kwargs": {"enable_thinking": False}}
        return conf
    if _REASONING_MODEL.search(model_name):
        conf = {"history_n_images": images, "max_tokens": 16000}
        if reasoning_effort:
            conf["reasoning_effort"] = reasoning_effort
        return conf
    return {"history_n_images": images, "temperature": 0.0, "max_tokens": 2048}


class InAppAssistantAgent(GeneralE2EAgentMCP):
    def __init__(
        self,
        model_name: str,
        llm_base_url: str,
        api_key: str = "empty",
        app_name: str = "the app",
        language: str = "ko",
        tools: list[dict] | None = None,
        runtime_conf: dict | None = None,
        gui: bool = True,
        extra_instructions: str = "",
        ask_example: str = DEFAULT_ASK_EXAMPLE,
        answer_example: str = DEFAULT_ANSWER_EXAMPLE,
        reasoning_effort: str | None = None,
        **kwargs: Any,
    ):
        super().__init__(
            model_name=model_name,
            llm_base_url=llm_base_url,
            api_key=api_key,
            tools=tools or [],
            runtime_conf=runtime_conf or default_runtime_conf(model_name, gui, reasoning_effort),
            **kwargs,
        )
        self.app_name = app_name
        self.language = LANGUAGE_NAMES.get(language, language)
        self.gui = gui
        self.extra_instructions = extra_instructions
        self.ask_example = ask_example
        self.answer_example = answer_example

    def _render_system_prompt(self, scale_factor) -> str:
        return build_system_prompt(
            app_name=self.app_name,
            language=self.language,
            tools=self.tools,
            scale_factor=scale_factor,
            gui=self.gui,
            extra_instructions=self.extra_instructions,
            ask_example=self.ask_example,
            answer_example=self.answer_example,
        )

    def _get_user_message(
        self, img_data, tool_call_res, ask_user_response_res, instruction: str | None = None
    ) -> dict:
        content: list[dict] = []
        if instruction is not None:
            content.append({"type": "text", "text": instruction})
        if tool_call_res is not None:
            text = str(tool_call_res)
            if not text.startswith(("Blocked:", "Note:")):
                text = f"Tool call result: {text}"
            content.append({"type": "text", "text": text})
        if ask_user_response_res is not None:
            content.append({"type": "text", "text": f"User replied: {ask_user_response_res}"})
        if self.gui:  # no screen in tools-only sessions: don't pay for blank images
            content.append({"type": "image_url", "image_url": img_data})
        elif not content:
            content.append({"type": "text", "text": "(no new information)"})
        return {"role": "user", "content": content}

    # ------------------------------------------------------------------ streaming
    speech_sink = None  # set by the session: callable(sentence) to speak while generating

    def openai_chat_completions_create(
        self, model: str, messages: list[dict], retry_times: int = 3, stream: bool = False, **kwargs
    ):
        """Stream when a speech sink is set; otherwise MobileWorld's normal (blocking) call."""
        sink = self.speech_sink
        if sink is None or stream:
            return super().openai_chat_completions_create(
                model, messages, retry_times=retry_times, stream=stream, **kwargs
            )
        from mobile_world.assistant.streaming import SpeechStreamExtractor

        original_kwargs, kwargs = kwargs, dict(kwargs)
        name = model.lower()
        if "claude" in name:
            kwargs["max_tokens"] = 64000
            kwargs.pop("temperature", None)
        if ("gpt" in name or _REASONING_MODEL.search(model)) and "max_tokens" in kwargs:
            kwargs["max_completion_tokens"] = kwargs.pop("max_tokens")
        extractor = SpeechStreamExtractor()
        text, usage = "", None
        try:
            response = self.openai_client.chat.completions.create(
                model=model,
                messages=messages,
                stream=True,
                stream_options={"include_usage": True},
                **kwargs,
            )
            for chunk in response:
                if getattr(chunk, "usage", None) is not None:
                    usage = chunk
                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta.content or ""
                if delta:
                    text += delta
                    for sentence in extractor.feed(delta):
                        sink(sentence)
        except Exception as e:
            if extractor.spoken:  # we already said part of it: don't start over
                logger.warning(f"Streaming broke after speaking part of the reply: {e}")
                return text.strip() or None
            logger.warning(f"Streaming failed ({e}); retrying without streaming")
            self.speech_sink = None
            try:
                return super().openai_chat_completions_create(
                    model, messages, retry_times=retry_times, **original_kwargs
                )
            finally:
                self.speech_sink = sink
        if usage is not None:
            self._log_openai_usage(usage)
        return text.strip()

    # ------------------------------------------------------------------ lenient actions
    def predict(self, observation):
        """Like GeneralE2E, but accept a tool name used as the action type.

        Smaller open models often answer {"action_type": "prepare_order", "menu_id": "m04"}
        instead of {"action_type": "mcp", "action_name": "prepare_order", "action_json": {...}}.
        The intent is clear, so it is normalized instead of failing the whole request."""
        from pydantic import ValidationError

        try:
            raw, action = super().predict(observation)
            if action.action_type != "mcp" or action.action_name:
                return raw, action
            # "mcp" but the tool name sits under another key ({"name": ...})
            fixed = self._normalize_tool_action(dict(self.actions[-1]))
            if fixed is None:
                return raw, JSONAction(action_type="unknown", text="invalid action")
            self.actions[-1] = fixed
            return raw, JSONAction(**fixed)
        except ValidationError:
            raw = self.history_responses[-1]["content"] if self.history_responses else ""
            data = dict(self.actions[-1]) if self.actions else {}
            fixed = self._normalize_tool_action(data)
            if fixed is not None:
                self.actions[-1] = fixed
                logger.info(f"Normalized a tool call: {data} -> {fixed}")
                return raw, JSONAction(**fixed)
            logger.warning(f"Unusable action from the model: {data}")
            return raw or "invalid action", JSONAction(action_type="unknown", text="invalid action")

    def _normalize_tool_action(self, data: dict) -> dict | None:
        names = {t.get("name") for t in self.tools}
        kind = data.get("action_type")
        name = kind if kind in names else None
        if kind == "mcp":
            name = data.get("action_name") or data.get("name") or data.get("tool")
        if name not in names:
            return None
        args = (
            data.get("action_json")
            or data.get("args")
            or data.get("arguments")
            or data.get("parameters")
        )
        if not isinstance(args, dict):
            skip = {
                "action_type",
                "action_name",
                "name",
                "tool",
                "action_json",
                "args",
                "arguments",
                "parameters",
            }
            args = {k: v for k, v in data.items() if k not in skip}
        return {"action_type": "mcp", "action_name": name, "action_json": args}
