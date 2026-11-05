"""Hybrid (app tools + GUI) agent tuned for an in-app voice assistant.

Reuses MobileWorld's ``GeneralE2EAgentMCP`` (history handling, coordinate conventions for
Claude/Gemini/Qwen/Kimi, output parsing) and only changes:
- the system prompt (in-app role, spoken replies, confirmation rule, tools-first);
- feedback turns keep the screenshot next to the text (tool result / user answer /
  guard message), so the agent never acts blind after a non-GUI step.
"""

from __future__ import annotations

import json
from typing import Any

from mobile_world.agents.implementations.general_e2e_agent import GeneralE2EAgentMCP
from mobile_world.assistant.prompts import IN_APP_ASSISTANT_PROMPT, LANGUAGE_NAMES


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
        **kwargs: Any,
    ):
        super().__init__(
            model_name=model_name,
            llm_base_url=llm_base_url,
            api_key=api_key,
            tools=tools or [],
            runtime_conf=runtime_conf
            or {"history_n_images": 2, "temperature": 0.0, "max_tokens": 2048},
            **kwargs,
        )
        self.app_name = app_name
        self.language = LANGUAGE_NAMES.get(language, language)

    def _render_system_prompt(self, scale_factor) -> str:
        return IN_APP_ASSISTANT_PROMPT.render(
            app_name=self.app_name,
            language=self.language,
            tools="\n".join(json.dumps(t, ensure_ascii=False) for t in self.tools),
            scale_factor=scale_factor,
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
        content.append({"type": "image_url", "image_url": img_data})
        return {"role": "user", "content": content}
