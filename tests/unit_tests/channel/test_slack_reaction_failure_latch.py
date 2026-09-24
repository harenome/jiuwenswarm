# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.

"""How often a refused acknowledgement reaction is reported.

``acknowledge_mode`` ships as ``reaction``, so the connector attempts a mark on
every message it takes. An install that was not granted ``reactions:write``
refuses every one of them, and the refusal used to be written as a WARNING each
time -- one line per inbound message, for the life of the process. Nothing else
noticed either: the caller discards what ``_add_reaction`` returns.

The failure itself is real and is not silenced here. A sender whose message gets
no mark has no sign it was taken. What these tests pin is the **rate**: the
first of a kind is a warning, every repeat is a DEBUG, and the latch is held
where it cannot hide a second workspace's problem.

Warnings are captured by replacing the module logger's methods rather than
through ``caplog``: this project's loggers do not propagate, so ``caplog`` sees
nothing under the pytest CI runs on.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from jiuwenswarm.gateway.channel_manager.base import RobotMessageRouter
from jiuwenswarm.gateway.channel_manager.im_platforms.slack import slack_connect
from jiuwenswarm.gateway.channel_manager.im_platforms.slack.slack_connect import (
    SlackChannel,
    SlackChannelConfig,
)

_CHANNEL = "C0OPSROOM1"
_TS = "1777423717.666499"


class _SlackApiError(Exception):
    """The shape slack_sdk raises: an exception holding the failed response."""

    def __init__(self, error: str) -> None:
        super().__init__(f"The request to the Slack API failed. (error: {error})")
        self.response = SimpleNamespace(data={"ok": False, "error": error})


class _RefusingClient:
    """Refuses every ``reactions.add`` with one code, and counts the calls."""

    def __init__(self, error: str) -> None:
        self.error = error
        self.calls: list[dict[str, Any]] = []

    async def reactions_add(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        raise _SlackApiError(self.error)


@pytest.fixture
def logged(monkeypatch) -> dict[str, list[str]]:
    recorded: dict[str, list[str]] = {"warning": [], "debug": []}

    def record(level: str):
        def write(message: str, *args: Any, **kwargs: Any) -> None:
            recorded[level].append(message % args if args else message)

        return write

    monkeypatch.setattr(slack_connect.logger, "warning", record("warning"))
    monkeypatch.setattr(slack_connect.logger, "debug", record("debug"))
    return recorded


def _channel(client: Any) -> SlackChannel:
    channel = SlackChannel(SlackChannelConfig(enabled=True), RobotMessageRouter())
    channel._running = True
    channel._client = client
    return channel


@pytest.mark.asyncio
async def test_a_refused_reaction_is_warned_about_once(logged) -> None:
    """The defect, stated as the test that would have caught it.

    The second message must produce no further warning. It still produces a
    DEBUG line, because the reaction genuinely did not land and nothing else in
    the system records that.
    """
    client = _RefusingClient("missing_scope")
    channel = _channel(client)

    await channel._add_reaction(_CHANNEL, _TS, "eyes")
    assert len(logged["warning"]) == 1
    assert "missing_scope" in logged["warning"][0]
    assert logged["debug"] == []

    await channel._add_reaction(_CHANNEL, _TS, "eyes")

    assert len(logged["warning"]) == 1
    assert len(logged["debug"]) == 1
    assert "missing_scope" in logged["debug"][0]
    # Latched, not suppressed: the call was still attempted both times, so
    # nothing here decides on Slack's behalf that a mark cannot be posted.
    assert len(client.calls) == 2


@pytest.mark.asyncio
async def test_the_failure_is_still_returned_to_the_caller(logged) -> None:
    """The contract ``_add_reaction`` has always had is unchanged.

    The latch is about the log. A caller that wants to know is still told, and
    the method still never raises into the turn.
    """
    channel = _channel(_RefusingClient("missing_scope"))

    first = await channel._add_reaction(_CHANNEL, _TS, "eyes")
    second = await channel._add_reaction(_CHANNEL, _TS, "eyes")

    assert isinstance(first, _SlackApiError)
    assert isinstance(second, _SlackApiError)


@pytest.mark.asyncio
async def test_a_second_kind_of_refusal_is_reported_on_its_own(logged) -> None:
    """One latch entry per failure kind, so one does not hide another."""
    channel = _channel(_RefusingClient("missing_scope"))
    await channel._add_reaction(_CHANNEL, _TS, "eyes")
    await channel._add_reaction(_CHANNEL, _TS, "eyes")

    channel._client = _RefusingClient("not_in_channel")
    await channel._add_reaction(_CHANNEL, _TS, "eyes")

    assert len(logged["warning"]) == 2
    assert "missing_scope" in logged["warning"][0]
    assert "not_in_channel" in logged["warning"][1]


@pytest.mark.asyncio
async def test_a_second_bad_emoji_is_reported_on_its_own(logged) -> None:
    """``invalid_name`` is about the shortcode, so the emoji is part of the kind.

    Latching the second bad emoji behind the first would hide a second thing an
    operator has to fix, and the two are fixed separately.
    """
    channel = _channel(_RefusingClient("invalid_name"))

    await channel._add_reaction(_CHANNEL, _TS, "definitely_not_an_emoji")
    await channel._add_reaction(_CHANNEL, _TS, "definitely_not_an_emoji")
    await channel._add_reaction(_CHANNEL, _TS, "also_not_an_emoji")

    assert len(logged["warning"]) == 2
    assert "definitely_not_an_emoji" in logged["warning"][0]
    assert "also_not_an_emoji" in logged["warning"][1]


@pytest.mark.asyncio
async def test_a_failure_that_never_reached_slack_keeps_its_own_line(logged) -> None:
    """A timeout has no error code and is not bucketed with a refusal.

    Transient failures are a different kind of thing, and latching one behind a
    scope refusal would drop the only line saying the connection is unwell.
    """
    channel = _channel(_RefusingClient("missing_scope"))
    await channel._add_reaction(_CHANNEL, _TS, "eyes")

    class _Dropped:
        async def reactions_add(self, **kwargs: Any) -> Any:
            raise TimeoutError("read timed out")

    channel._client = _Dropped()
    await channel._add_reaction(_CHANNEL, _TS, "eyes")

    assert len(logged["warning"]) == 2
    assert "TimeoutError" in logged["warning"][1] or "timed out" in logged["warning"][1]


@pytest.mark.asyncio
async def test_two_workspaces_each_report_their_own_refusal(logged) -> None:
    """The latch is per channel instance, which is per workspace.

    The gateway builds one ``SlackChannel`` per credential block. A module-level
    flag would report the first installation's missing grant and then swallow
    the same grant missing in the second, which would read in the log as the
    second workspace being healthy.
    """
    first = _channel(_RefusingClient("missing_scope"))
    second = _channel(_RefusingClient("missing_scope"))

    await first._add_reaction(_CHANNEL, _TS, "eyes")
    await second._add_reaction(_CHANNEL, _TS, "eyes")

    assert len(logged["warning"]) == 2
