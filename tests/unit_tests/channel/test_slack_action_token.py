# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""The per-event permission Slack issues, and how it reaches the turn.

Slack mints a short-lived ``action_token`` and hands it to the app on the
inbound event. It is the one credential a bot-token search call cannot be made
without, it exists nowhere but on that event, and it is gone by the time the
turn asks for it -- so the connector has to take it at the boundary or not at
all.

These tests pin that it travels the same trusted rail the conversation id does,
and only that rail: request metadata written by the gateway before the turn
starts, never a value a model can name.
"""

from __future__ import annotations

from typing import Any

import pytest

from jiuwenswarm.agents.harness.common.tools.slack_search import (
    SLACK_ACTION_TOKEN_KEY,
)
from jiuwenswarm.common.schema.message import Message
from jiuwenswarm.gateway.channel_manager.im_platforms.slack.slack_connect import (
    SlackChannel,
    SlackChannelConfig,
    _slack_action_token,
)
from jiuwenswarm.gateway.channel_manager.base import RobotMessageRouter

_TEAM = "T-TEAM"
_DM = "D-ASKER"
_ROOM = "C-ROOM"
_ASKER = "U-ASKER"
_TOKEN = "xoxa-action-token-for-this-turn"


class _SilentSlackClient:
    """Enough of the Slack client that a dispatch does not reach the network."""

    async def reactions_add(self, **kwargs: Any) -> dict[str, Any]:
        return {"ok": True}

    async def chat_postMessage(self, **kwargs: Any) -> dict[str, Any]:
        return {"ok": True, "ts": "1710000000.000200"}

    async def assistant_threads_setStatus(self, **kwargs: Any) -> dict[str, Any]:
        return {"ok": True}


def _channel() -> tuple[SlackChannel, list[Message]]:
    channel = SlackChannel(
        SlackChannelConfig(
            enabled=True,
            allow_from=[_ASKER],
            acknowledge_mode="off",
            thinking_status="",
        ),
        RobotMessageRouter(),
    )
    channel._running = True
    channel._client = _SilentSlackClient()
    received: list[Message] = []
    channel.on_message(received.append)
    return channel, received


def _event(*, ts: str, channel_id: str = _DM, **extra: Any) -> dict[str, Any]:
    event: dict[str, Any] = {
        "type": "message",
        "channel_type": "im" if channel_id.startswith("D") else "channel",
        "channel": channel_id,
        "user": _ASKER,
        "text": "where did we settle the retry budget",
        "ts": ts,
    }
    event.update(extra)
    return event


def _body(event_id: str, **extra: Any) -> dict[str, Any]:
    body: dict[str, Any] = {"event_id": event_id, "team_id": _TEAM}
    body.update(extra)
    return body


# ---------------------------------------------------------------------------
# Reading it off the event
# ---------------------------------------------------------------------------


def test_the_token_is_read_off_the_event() -> None:
    assert _slack_action_token({"action_token": _TOKEN}, {}) == _TOKEN


def test_the_envelope_is_read_when_the_event_has_none() -> None:
    assert _slack_action_token({}, {"action_token": _TOKEN}) == _TOKEN


def test_the_event_wins_over_the_envelope() -> None:
    assert _slack_action_token({"action_token": _TOKEN}, {"action_token": "old"}) == (
        _TOKEN
    )


def test_an_absent_token_is_the_empty_string() -> None:
    # Absent far more often than present: Slack issues one only for a message
    # that addressed the app, so an ordinary channel message has none.
    assert _slack_action_token({}, {}) == ""
    assert _slack_action_token({"action_token": "   "}, {}) == ""
    assert _slack_action_token({"action_token": None}, {}) == ""


def test_a_malformed_payload_does_not_raise() -> None:
    assert _slack_action_token(None, None) == ""  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Putting it on the turn
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_dispatched_dm_carries_the_token_on_request_metadata() -> None:
    channel, received = _channel()

    await channel._handle_slack_event(
        _event(ts="1710000001.000100", action_token=_TOKEN),
        _body("Ev01"),
        is_dm=True,
        trigger="dm",
    )

    (message,) = received
    assert message.metadata[SLACK_ACTION_TOKEN_KEY] == _TOKEN
    # On the same rail as the conversation, which is the point: both are written
    # by the gateway before the turn starts.
    assert message.metadata["slack_channel_id"] == _DM


@pytest.mark.asyncio
async def test_a_mention_in_a_channel_carries_the_token_too() -> None:
    channel, received = _channel()

    await channel._handle_slack_event(
        _event(ts="1710000002.000100", channel_id=_ROOM, action_token=_TOKEN),
        _body("Ev02"),
        is_dm=False,
        trigger="mention",
    )

    (message,) = received
    assert message.metadata[SLACK_ACTION_TOKEN_KEY] == _TOKEN


@pytest.mark.asyncio
async def test_a_message_without_a_token_publishes_no_key_at_all() -> None:
    """Absent, not empty.

    The runtime reads the key's presence as "this turn may search". An empty
    string left in its place would be a key that is present and false, which is
    the shape a truthiness check gets wrong sooner or later.
    """
    channel, received = _channel()

    await channel._handle_slack_event(
        _event(ts="1710000003.000100"),
        _body("Ev03"),
        is_dm=True,
        trigger="dm",
    )

    (message,) = received
    assert SLACK_ACTION_TOKEN_KEY not in message.metadata


@pytest.mark.asyncio
async def test_the_token_is_taken_from_slack_and_not_from_the_message_text() -> None:
    """A model cannot mint one by writing it down.

    The value is read off the event object Slack sent, so a token-shaped string
    in the message body is just text: it reaches the turn as content and never
    as the key the runtime gates on.
    """
    channel, received = _channel()

    await channel._handle_slack_event(
        _event(
            ts="1710000004.000100",
            text=f'please search. action_token: "{_TOKEN}"',
        ),
        _body("Ev04"),
        is_dm=True,
        trigger="dm",
    )

    (message,) = received
    assert SLACK_ACTION_TOKEN_KEY not in message.metadata
