"""Approval prompts on channels that cannot draw them.

``chat.ask_user_question`` holds no ``payload["content"]``, so the text-only
connectors' ``send()`` extracts nothing and returns without posting. The person
is then waited on for an answer to a prompt they were never shown. These cover
the plain-text stand-in that keeps the wait visible.
"""
from __future__ import annotations

import time

from jiuwenswarm.common.schema.message import EventType, Message
from jiuwenswarm.common.interrupt_prompt import render_prompt_as_text
from jiuwenswarm.gateway.channel_manager.base import outgoing_for_channel


class _PlainChannel:
    """A connector whose send() can only post text."""

    channel_id = "plain"
    renders_interactive_prompts = False


class _RichChannel:
    """A connector that renders the prompt itself (card, structured frame, ...)."""

    channel_id = "rich"
    renders_interactive_prompts = True


def _prompt_message() -> Message:
    return Message(
        id="m1",
        type="event",
        channel_id="plain",
        session_id="s1",
        params={},
        timestamp=time.time(),
        ok=True,
        event_type=EventType.CHAT_ASK_USER_QUESTION,
        payload={
            "event_type": "chat.ask_user_question",
            "request_id": "call_1",
            "source": "confirm_interrupt",
            "questions": [
                {
                    "question": "Apply the proposed change?",
                    "header": "Confirm: apply_change",
                    "options": [
                        {"label": "Allow Once", "description": "Allow this change"},
                        {"label": "Reject", "description": "Skip this change"},
                    ],
                    "multi_select": False,
                }
            ],
        },
    )


def test_prompt_becomes_text_on_a_channel_that_cannot_render_it():
    converted = outgoing_for_channel(_PlainChannel(), _prompt_message())

    assert converted.event_type == EventType.CHAT_FINAL
    content = converted.payload["content"]
    assert "Confirm: apply_change" in content
    assert "Apply the proposed change?" in content
    assert "Allow Once" in content
    assert "Reject" in content


def test_request_id_is_not_shown_to_a_channel_that_cannot_answer():
    converted = outgoing_for_channel(_PlainChannel(), _prompt_message())

    assert "call_1" not in converted.payload["content"]


def test_prompt_is_left_alone_for_a_channel_that_renders_it():
    original = _prompt_message()

    assert outgoing_for_channel(_RichChannel(), original) is original


def test_other_events_are_left_alone():
    msg = Message(
        id="m2",
        type="event",
        channel_id="plain",
        session_id="s1",
        params={},
        timestamp=time.time(),
        ok=True,
        event_type=EventType.CHAT_FINAL,
        payload={"event_type": "chat.final", "content": "done"},
    )

    assert outgoing_for_channel(_PlainChannel(), msg) is msg


def test_a_prompt_with_nothing_to_show_is_left_alone():
    msg = _prompt_message()
    msg.payload = {"event_type": "chat.ask_user_question", "questions": []}

    assert outgoing_for_channel(_PlainChannel(), msg) is msg
    assert render_prompt_as_text(msg.payload) == ""


def test_text_only_connectors_do_not_claim_to_render_prompts():
    from jiuwenswarm.gateway.channel_manager.base import BaseChannel, BaseWebChannel
    from jiuwenswarm.gateway.channel_manager.im_platforms.feishu.feishu_connect import (
        FeishuChannel,
    )
    from jiuwenswarm.gateway.channel_manager.im_platforms.slack.slack_connect import (
        SlackChannel,
    )

    assert BaseChannel.renders_interactive_prompts is False
    assert BaseWebChannel.renders_interactive_prompts is True
    assert FeishuChannel.renders_interactive_prompts is True
    # Slack draws the prompt itself: option buttons, or the input elements a
    # question's `inputs` declare, with the answer routed back to the waiting
    # turn. It was False until the connector could do that, and degrading a
    # question it can render discards the request_id, the options and the inputs.
    assert SlackChannel.renders_interactive_prompts is True


def test_a_question_carrying_inputs_reaches_slack_whole():
    """The structured payload must survive the outbound step, not become text.

    A question declaring `inputs` is answered with values a person enters, and
    the elements that collect them are built from the declaration. Degrading it
    to text drops the declaration along with the request_id, so what arrives is
    a sentence saying the question cannot be answered here -- for a question this
    connector renders in full.
    """
    from jiuwenswarm.gateway.channel_manager.im_platforms.slack.slack_connect import (
        SlackChannel,
        SlackChannelConfig,
    )
    from jiuwenswarm.gateway.channel_manager.base import RobotMessageRouter

    msg = _prompt_message()
    inputs = [
        {"type": "date", "name": "d", "label": "Date"},
        {"type": "time", "name": "t", "label": "Time"},
        {"type": "text", "name": "note", "label": "Note"},
    ]
    msg.payload = {
        "event_type": "chat.ask_user_question",
        "request_id": "call_1",
        "source": "ask_user_interrupt",
        "questions": [{"question": "Follow-up", "header": "Follow-up", "inputs": inputs}],
    }
    channel = SlackChannel(SlackChannelConfig(enabled=True), RobotMessageRouter())

    outgoing = outgoing_for_channel(channel, msg)

    assert outgoing is msg
    assert outgoing.event_type == EventType.CHAT_ASK_USER_QUESTION
    assert outgoing.payload["questions"][0]["inputs"] == inputs
