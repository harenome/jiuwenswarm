"""Approval prompts remain visible on text-only channels."""

import asyncio
import time
from types import SimpleNamespace

from jiuwenswarm.common.interrupt_prompt import render_prompt_as_text
from jiuwenswarm.common.schema.message import EventType, Message
from jiuwenswarm.gateway.channel_manager.base import outgoing_for_channel
from jiuwenswarm.gateway.channel_manager.channel_manager import ChannelManager


class _PlainChannel:
    channel_id = "plain"
    renders_interactive_prompts = False


class _RichChannel:
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
    assert "call_1" not in content


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


def test_malformed_options_do_not_hide_the_question():
    msg = _prompt_message()
    msg.payload["questions"][0]["options"] = 42

    converted = outgoing_for_channel(_PlainChannel(), msg)
    assert converted.event_type == EventType.CHAT_FINAL
    assert "Apply the proposed change?" in converted.payload["content"]


def test_channel_prompt_capabilities_match_implementations():
    from jiuwenswarm.gateway.channel_manager.base import BaseChannel, BaseWebChannel
    from jiuwenswarm.gateway.channel_manager.im_platforms.feishu.feishu_connect import (
        FeishuChannel,
    )

    assert BaseChannel.renders_interactive_prompts is False
    assert BaseWebChannel.renders_interactive_prompts is True
    assert FeishuChannel.renders_interactive_prompts is True


def test_dispatch_converts_prompt_for_plain_channel():
    sent = []

    class Handler:
        async def consume_robot_messages(self, timeout):
            return _prompt_message()

        def resolve_app_id(self, msg):
            return ""

    class Channel(_PlainChannel):
        async def send(self, msg):
            sent.append(msg)
            manager._running = False

    async def skip_fanout(msg, event_type):
        pass

    manager = SimpleNamespace(
        _running=True,
        _message_handler=Handler(),
        _inject_file_delivery_fanout=skip_fanout,
        _resolve_outbound_channel=lambda msg: Channel(),
    )
    asyncio.run(ChannelManager._dispatch_robot_messages(manager))

    assert sent[0].event_type == EventType.CHAT_FINAL
