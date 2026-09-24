# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.

"""Each of the six state marks must be removable on its own.

The connector marks a turn's progress with six reactions -- seen, refused,
waiting, finished, broken, halted -- and until ``resolve_reaction_emoji`` there
was no way to drop one of them. Blanking a key hands back the default, so the
only switch was ``acknowledge_mode: off``, which takes all six away together
with the thread statuses. An operator who wanted the completion tick gone had
to give up the queue hourglass and the failure cross with it.

These tests pin the word that drops one mark, that blank still means "use the
default" rather than joining it, and that a silenced mark leaves the call sites
around it behaving as they document -- in particular that a silenced ending
still clears the acknowledgement, so a finished message is left bare rather
than wearing "still running" for good.
"""

from __future__ import annotations

from typing import Any

import pytest

from jiuwenswarm.gateway.channel_manager.base import RobotMessageRouter
from jiuwenswarm.gateway.channel_manager.im_platforms.slack import slack_connect
from jiuwenswarm.gateway.channel_manager.im_platforms.slack.slack_connect import (
    REACTION_DISABLED,
    SlackChannel,
    SlackChannelConfig,
    resolve_reaction_emoji,
)

ASKER = "U0ASKER001"
CHANNEL = "C0CHANNEL1"
MESSAGE_TS = "1710000001.000100"
THREAD_TS = "1710000001.000100"


class _RecordingSlackClient:
    """Just enough Slack to see which reactions were attempted."""

    def __init__(self) -> None:
        self.added: list[tuple[str, str, str]] = []
        self.removed: list[tuple[str, str, str]] = []
        self.api_calls: list[dict[str, Any]] = []
        self.posts: list[dict[str, Any]] = []

    async def chat_postMessage(self, **kwargs: Any) -> dict[str, str]:
        self.posts.append(kwargs)
        return {"ts": "1710000009.000100"}

    async def api_call(self, api_method: str, **kwargs: Any) -> dict[str, bool]:
        self.api_calls.append({"api_method": api_method, **kwargs})
        return {"ok": True}

    async def reactions_add(self, **kwargs: Any) -> dict[str, bool]:
        self.added.append(_coordinates(kwargs))
        return {"ok": True}

    async def reactions_remove(self, **kwargs: Any) -> dict[str, bool]:
        self.removed.append(_coordinates(kwargs))
        return {"ok": True}


def _coordinates(kwargs: dict[str, Any]) -> tuple[str, str, str]:
    return (
        str(kwargs.get("channel", "")),
        str(kwargs.get("timestamp", "")),
        str(kwargs.get("name", "")),
    )


def _channel(**overrides: Any) -> tuple[SlackChannel, _RecordingSlackClient]:
    channel = SlackChannel(
        SlackChannelConfig(enabled=True, allow_from=[ASKER], **overrides),
        RobotMessageRouter(),
    )
    channel._running = True
    client = _RecordingSlackClient()
    channel._client = client
    return channel, client


def _initiator() -> Any:
    return slack_connect._SlackTurnInitiator(
        user_id=ASKER,
        request_id="req-1",
        is_dm=False,
        chat_type="channel",
        channel_id=CHANNEL,
        message_ts=MESSAGE_TS,
        thread_ts=THREAD_TS,
    )


def _queued() -> Any:
    return slack_connect._SlackQueuedMessage(
        request=None,
        session_id="session-1",
        user_id=ASKER,
        is_dm=False,
        chat_type="channel",
        channel_id=CHANNEL,
        message_ts=MESSAGE_TS,
        thread_ts=THREAD_TS,
    )


@pytest.mark.parametrize(
    ("configured", "expected"),
    [
        # Absent and value-less both mean "nothing was decided here". The
        # template ships every key with a value, but a config upgrade can leave
        # one bare, and that must not silence a mark by accident.
        (None, "eyes"),
        ("", "eyes"),
        ("   ", "eyes"),
        # Blank is deliberately still the default and not the off switch. It is
        # what queued_status and steered_status beside these keys already mean
        # by blank, and changing it would move an existing config's behaviour
        # under an operator who had blanked a key expecting the default back.
        ("heavy_check_mark", "heavy_check_mark"),
        ("  white_check_mark  ", "white_check_mark"),
        # The one word that drops the mark.
        (REACTION_DISABLED, ""),
        ("  disabled  ", ""),
        # Cased differently it is an emoji name and not the word: Slack emoji
        # names are lower-case, so nothing here is being helpfully corrected.
        ("Disabled", "Disabled"),
        # The escape for a workspace whose own custom emoji is named disabled.
        # The word is matched before _add_reaction strips the colons, so the
        # colon form still asks for the emoji.
        (":disabled:", ":disabled:"),
    ],
)
def test_resolve_reaction_emoji(configured: Any, expected: str) -> None:
    assert resolve_reaction_emoji(configured, "eyes") == expected


def test_resolve_reaction_emoji_reads_every_key_the_same_way() -> None:
    """One word for all six, not a special case for the tick.

    The complaint that produced this key was about ``completed_emoji``, but a
    mechanism that only answered that one would have to be argued again the
    next time a workspace finds a different mark wrong.
    """
    for default in (
        "eyes",
        "no_entry_sign",
        "hourglass_flowing_sand",
        "heavy_check_mark",
        "x",
        "black_square_for_stop",
    ):
        assert resolve_reaction_emoji(REACTION_DISABLED, default) == ""
        assert resolve_reaction_emoji(None, default) == default


def test_reaction_disabled_is_not_a_yaml_boolean() -> None:
    """The word has to survive the config file unquoted.

    ``channels.slack`` is read as YAML 1.1, which resolves a bare ``off`` to
    the boolean false. A word an operator could only write in quotes would be
    the one spelling of "turn this off" the file cannot hold plainly, which is
    why ``history`` and ``write`` already spell their narrowest word this way.
    """
    yaml = pytest.importorskip("yaml")
    loaded = yaml.safe_load(f"completed_emoji: {REACTION_DISABLED}\n")
    assert loaded == {"completed_emoji": REACTION_DISABLED}


@pytest.mark.asyncio
async def test_silenced_ending_still_clears_the_acknowledgement() -> None:
    """A finished message is left bare, not left claiming to be running.

    ``_mark_turn_ended`` removes the acknowledgement and adds the ending. With
    the ending silenced the remove still has to happen: leaving the "seen" mark
    standing would say an answer is still coming, for good, on every turn the
    bot ever completes -- which is worse than the tick the operator asked to be
    rid of.
    """
    channel, client = _channel(completed_emoji=REACTION_DISABLED)

    await channel._mark_turn_ended(_initiator(), channel.config.completed_emoji)

    assert client.removed == [(CHANNEL, MESSAGE_TS, "eyes")]
    assert client.added == []


@pytest.mark.asyncio
async def test_silencing_one_ending_leaves_the_others_alone() -> None:
    """The tick goes and the cross stays -- that is the whole point."""
    channel, client = _channel(completed_emoji=REACTION_DISABLED)

    await channel._mark_turn_ended(_initiator(), channel.config.failed_emoji)

    assert (CHANNEL, MESSAGE_TS, "x") in client.added


@pytest.mark.asyncio
async def test_silenced_acknowledgement_still_sets_the_thread_status() -> None:
    """Dropping the mark drops the mark and nothing else.

    The thread status rides on ``acknowledge_mode``, not on whether a reaction
    was drawn, so an operator who silences the "seen" emoji keeps the line
    Slack draws in the thread while the turn runs.
    """
    channel, client = _channel(acknowledgement_emoji=REACTION_DISABLED)

    await channel._acknowledge_request(CHANNEL, THREAD_TS, MESSAGE_TS)

    assert client.added == []
    assert [
        call
        for call in client.api_calls
        if call["api_method"] == slack_connect._ASSISTANT_SET_STATUS_METHOD
    ]


@pytest.mark.asyncio
async def test_silenced_queue_mark_is_neither_added_nor_removed() -> None:
    """The waiting mark is a pair of calls and both have to go.

    A silenced add with a live remove would call ``reactions.remove`` on a
    reaction that was never there, which Slack answers ``no_reaction`` to --
    harmless, and still a call per drained message that says nothing.
    """
    channel, client = _channel(queued_emoji=REACTION_DISABLED)
    entry = _queued()

    await channel._mark_queued(entry)
    await channel._unmark_queued(entry)

    assert client.added == []
    assert client.removed == []
