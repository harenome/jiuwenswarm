# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""将审批问题渲染为纯文本，供不支持交互的渠道显示。"""

from typing import Any

_CANNOT_ANSWER_HERE = "本渠道无法直接作答，请到支持审批交互的客户端确认。"


def render_prompt_as_text(payload: dict[str, Any]) -> str:
    """渲染问题和选项；无可展示的问题时返回空串。"""
    if not isinstance(payload, dict):
        return ""
    questions = payload.get("questions")
    if not isinstance(questions, list):
        return ""

    lines: list[str] = []
    for question in questions:
        if not isinstance(question, dict):
            continue
        header = str(question.get("header") or "").strip()
        body = str(question.get("question") or "").strip()
        lines.extend(part for part in (header, body) if part)
        options = question.get("options")
        if not isinstance(options, list):
            continue
        option_lines: list[str] = []
        for option in options:
            if not isinstance(option, dict):
                continue
            label = str(option.get("label") or option.get("value") or "").strip()
            if not label:
                continue
            description = str(option.get("description") or "").strip()
            option_lines.append(f"- {label}: {description}" if description else f"- {label}")
        if option_lines:
            lines.append("")
            lines.extend(option_lines)

    if not lines:
        return ""
    lines.extend(("", _CANNOT_ANSWER_HERE))
    return "\n".join(lines)
