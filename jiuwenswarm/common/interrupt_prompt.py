# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""chat.ask_user_question 的纯文本渲染。

审批提示的正常形态是结构化 payload：问题、选项、以及回传答案用的 request_id。
能渲染它的只有飞书卡片和 Web / TUI / CLI 前端。其余传输要么按 payload["content"]
取文本、什么也取不到，要么根本收不到这个事件——提示消失，agent 却还在等答复。

退化成文本救不了作答，但至少让等待是可见的。渲染由 gateway 出站与非流式响应两
条路共用，两边措辞一致。
"""

from __future__ import annotations

from typing import Any

# 结尾提示：说清楚为什么这里只有文本。不描述这个请求接下来的命运——那由 agent
# 侧决定，本模块保证不了。
_CANNOT_ANSWER_HERE = "本渠道无法直接作答，请到支持审批交互的客户端确认。"


def render_prompt_as_text(payload: dict[str, Any]) -> str:
    """把 chat.ask_user_question payload 渲染成纯文本。

    只保留人需要看到的部分：等待的是什么、有哪些选项。request_id 不带出去——
    渲染不了审批的传输同样回传不了结构化答复，露出一个用不上的 id 只会误导。

    Args:
        payload: chat.ask_user_question 事件的 payload。

    Returns:
        可直接发送的文本；payload 里没有可展示的问题时返回空串。
    """
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
        if header:
            lines.append(header)
        if body:
            lines.append(body)
        # 提示正文本身常带 markdown 列表（比如逐条列出待批准的变更），选项紧跟其后
        # 会和正文的条目连成一片。空行把两者分开。
        option_lines: list[str] = []
        for option in question.get("options") or []:
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
    lines.append("")
    lines.append(_CANNOT_ANSWER_HERE)
    return "\n".join(lines)
