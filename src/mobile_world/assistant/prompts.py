"""System prompt for the in-app voice assistant.

The prompt is assembled from parts so that nothing irrelevant or contradictory is shown:
GUI actions and coordinates only when there is a screen, the tool action only when there
are tools, and principles numbered after the ones that apply are chosen.
"""

from __future__ import annotations

import json

LANGUAGE_NAMES = {"ko": "Korean", "en": "English", "ja": "Japanese", "zh": "Chinese"}

# Neutral examples (not tied to any app domain); apps can pass their own.
DEFAULT_ASK_EXAMPLE = "어떤 걸로 할까요?"
DEFAULT_ANSWER_EXAMPLE = "요청하신 대로 마쳤어요."

_GUI_ACTIONS = [
    ("click", "Tap visible element", '{"action_type": "click", "coordinate": [x, y]}'),
    ("double_tap", "Double-tap visible element", '{"action_type": "double_tap", "coordinate": [x, y]}'),
    ("long_press", "Long-press visible element", '{"action_type": "long_press", "coordinate": [x, y]}'),
    ("drag", "Drag between two visible elements", '{"action_type": "drag", "start_coordinate": [x1, y1], "end_coordinate": [x2, y2]}'),
    ("input_text", "Type into the focused field", '{"action_type": "input_text", "text": "Hello"}'),
    ("keyboard_enter", "Press enter", '{"action_type": "keyboard_enter"}'),
    ("scroll", "Scroll up/down/left/right", '{"action_type": "scroll", "direction": "down"}'),
    ("navigate_back", "In-app back", '{"action_type": "navigate_back"}'),
    ("wait", "Wait for the screen to update", '{"action_type": "wait"}'),
]  # fmt: skip


def _coordinates_note(scale_factor) -> str:
    if isinstance(scale_factor, (list, tuple)):
        return (
            "- x, y are pixel coordinates on the screenshot "
            f"(width={scale_factor[0]}, height={scale_factor[1]})."
        )
    return f"- x, y are normalized to [0, {scale_factor}]."


def build_system_prompt(
    app_name: str,
    language: str,
    tools: list[dict],
    scale_factor=1000,
    gui: bool = True,
    extra_instructions: str = "",
    ask_example: str = DEFAULT_ASK_EXAMPLE,
    answer_example: str = DEFAULT_ANSWER_EXAMPLE,
) -> str:
    def js(obj: dict) -> str:
        return json.dumps(obj, ensure_ascii=False)

    # ---- actions
    rows = list(_GUI_ACTIONS) if gui else []
    rows.append(
        ("ask_user", "Ask the user (spoken)", js({"action_type": "ask_user", "text": ask_example}))
    )
    rows.append(
        (
            "answer",
            "Final spoken reply; ENDS the request",
            js({"action_type": "answer", "text": answer_example}),
        )
    )
    if tools:
        rows.append(
            (
                "mcp",
                "Call an app tool",
                '{"action_type": "mcp", "action_name": "tool_name", "action_json": {...}}',
            )
        )
    table = "\n".join(f"| `{name}` | {desc} | `{example}` |" for name, desc, example in rows)

    parts = [
        f'# Role: Voice Assistant inside "{app_name}"\n'
        f'You are a voice assistant embedded in the Android app "{app_name}". The user speaks to you; '
        "their words reach you through speech recognition, and your `answer` / `ask_user` text is read "
        "aloud with text-to-speech. You may ONLY operate inside this app.",
        "# Action Framework\nRespond with EXACT JSON for one of these actions:\n"
        "| Action | Description | JSON Format Example |\n|---|---|---|\n" + table,
    ]
    if gui:
        parts.append(
            f"`navigate_home` and `open_app` are NOT available: never leave {app_name}.\n\n"
            "Coordinates: the center of the target element, origin at the top-left of the screen.\n"
            + _coordinates_note(scale_factor)
        )
    else:
        parts.append(
            "There is NO screen in this session. Only the actions above exist: use app tools to do "
            "things and `ask_user` / `answer` to talk."
        )

    # ---- principles (only those that apply, numbered in order)
    rules = []
    if tools and gui:
        rules.append(
            "Tools first: if an app tool can do the job (or fetch the data), call it instead of "
            "navigating the GUI. Use GUI actions only for what no tool covers."
        )
    elif tools:
        rules.append("Use the app tools for everything; never guess data a tool can fetch.")
    else:
        rules.append("No app tools are configured, so use GUI actions.")
    rules += [
        "Speech input: the request may contain recognition errors (homophones, missing spaces, wrong "
        "numbers). Interpret it sensibly from context. If it is genuinely ambiguous, use `ask_user`.",
        "Confirm before anything irreversible (ordering, paying, sending, deleting, cancelling, changing "
        "settings), and never act without the user's clear yes in this request. "
        + (
            "If a tool returns `confirmation_id` and `confirm_question`, ask exactly that question with "
            "`ask_user`, and call the matching commit tool with that `confirmation_id` only after a clear "
            "yes. For irreversible tools without a confirmation step (e.g. cancelling), first ask a short "
            "yes/no question with `ask_user`. "
            if tools
            else "Ask a short yes/no question with `ask_user` first. "
        )
        + "On no, silence or hesitation, do not act.",
        f"Spoken replies (`answer`, `ask_user`): reply in {language}, 1-2 short natural sentences, no "
        "markdown, lists, emojis, URLs or coordinates. Always finish with `answer`: report the result "
        "for an action task, or the requested information for a question.",
        f"Out of scope: if the request cannot be done inside {app_name}, say so briefly with `answer`.",
        'Messages starting with "Blocked:" or "Note:" come from the system guarding the app; pick '
        'another way. "Tool call result:" messages are outputs of your tool calls.',
    ]
    if tools:
        rules.append(
            'Tool results are JSON like {"ok": true, "data": {...}, "say": "..."} or '
            '{"ok": false, "error": {"code": "...", "message": "..."}}. Base your spoken reply on `say` / '
            "`error.message` when present (you may shorten it); never invent data the tools did not return."
        )
    rules.append(
        "Efficiency: choose the shortest path; if an action fails twice, try an alternative."
        + (
            " Scroll down to see content further down. Click a text field before typing."
            if gui
            else ""
        )
    )
    parts.append("# Principles\n" + "\n".join(f"{i}. {r}" for i, r in enumerate(rules, 1)))

    if extra_instructions:
        parts.append(extra_instructions.strip())
    parts.append(
        "# Output Format (both lines required)\nThought: [brief reasoning]\nAction: [single JSON action]"
    )
    if tools:
        parts.append("# Available App Tools\n" + "\n".join(js(t) for t in tools))
    return "\n\n".join(parts)
