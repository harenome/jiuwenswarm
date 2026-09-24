"""A failed cron push must surface as a visible error, not a silent success.

``ChannelManager`` already turns an exception from ``Channel.send()`` into a
``chat.error`` on the web channel, but only for messages whose id starts with
``cron-push-``. That mechanism was dead for Slack: ``SlackChannel.send()`` returned
``None`` on every failure path, so a scheduled job whose delivery failed still
reported success. These tests pin the contract from both ends -- the channel raises,
and the manager converts that into something the user actually sees.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest

from jiuwenswarm.common.schema.message import EventType, Message
from jiuwenswarm.gateway.channel_manager.channel_manager import ChannelManager
from jiuwenswarm.gateway.channel_manager.im_platforms.slack.slack_connect import (
    SlackDeliveryError,
)


class _FakeHandler:
    """MessageHandler stub exposing only what the dispatch loop touches."""

    def __init__(self, messages: list[Message]) -> None:
        self._messages = list(messages)

    async def consume_robot_messages(self, timeout: float = 1.0) -> Message | None:
        if self._messages:
            return self._messages.pop(0)
        await asyncio.sleep(0.01)
        return None

    def resolve_app_id(self, msg: Message) -> str:
        return ""


class _RecordingChannel:
    """Captures what it was asked to send; optionally fails."""

    def __init__(self, channel_id: str, error: Exception | None = None) -> None:
        self._channel_id = channel_id
        self._error = error
        self.sent: list[Message] = []

    @property
    def channel_id(self) -> str:
        return self._channel_id

    def on_message(self, callback: Any) -> None:
        return None

    async def send(self, msg: Message, *, routing_target: Any = None) -> None:
        if self._error is not None:
            raise self._error
        self.sent.append(msg)


def _cron_push(msg_id: str = "cron-push-1") -> Message:
    return Message(
        id=msg_id,
        type="event",
        channel_id="slack",
        session_id="slack_T1_C1_1710000000.000100",
        params={},
        timestamp=time.time(),
        ok=True,
        payload={"content": "digest", "cron": {"job_name": "Daily Digest"}},
        event_type=EventType.CHAT_FINAL,
    )


async def _run_one_dispatch(manager: ChannelManager, web: _RecordingChannel) -> None:
    """Drive the dispatch loop until the web channel sees something, then stop."""
    manager._running = True
    task = asyncio.create_task(manager._dispatch_robot_messages())
    try:
        for _ in range(200):
            if web.sent:
                break
            await asyncio.sleep(0.01)
    finally:
        manager._running = False
        await asyncio.wait_for(task, timeout=5.0)


@pytest.mark.asyncio
async def test_failed_slack_cron_push_reaches_the_user_as_chat_error() -> None:
    slack = _RecordingChannel(
        "slack",
        error=SlackDeliveryError(
            "ratelimited", channel_id="C1", chunks_sent=1, chunks_total=3
        ),
    )
    web = _RecordingChannel("web")
    manager = ChannelManager(_FakeHandler([_cron_push()]))
    manager.register_channel(slack)
    manager.register_channel(web)

    await _run_one_dispatch(manager, web)

    assert len(web.sent) == 1
    error_msg = web.sent[0]
    assert error_msg.event_type == EventType.CHAT_ERROR
    assert error_msg.ok is False
    assert error_msg.session_id == "slack_T1_C1_1710000000.000100"
    # The job name and the reason both have to be in the text, or the user is told
    # only that "something" failed.
    error_text = (error_msg.payload or {}).get("error", "")
    assert "Daily Digest" in error_text
    assert "ratelimited" in error_text
    # Partial delivery is the detail that changes what the user should do next.
    assert "1/3 chunks" in error_text


@pytest.mark.asyncio
async def test_non_cron_delivery_failure_is_logged_but_not_pushed_to_web() -> None:
    # An interactive reply already failed in front of the user; a duplicate
    # chat.error on the web channel would be noise, and the id prefix is what
    # distinguishes the two cases.
    slack = _RecordingChannel("slack", error=SlackDeliveryError("channel_not_found"))
    web = _RecordingChannel("web")
    manager = ChannelManager(_FakeHandler([_cron_push(msg_id="reply-1")]))
    manager.register_channel(slack)
    manager.register_channel(web)

    manager._running = True
    task = asyncio.create_task(manager._dispatch_robot_messages())
    await asyncio.sleep(0.2)
    manager._running = False
    await asyncio.wait_for(task, timeout=5.0)

    assert web.sent == []
