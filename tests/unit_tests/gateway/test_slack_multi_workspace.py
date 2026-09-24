"""Several Slack workspaces in one process: indexing and outbound routing.

Two things are pinned here. That two configured workspaces register as two
distinct channels rather than one silently overwriting the other -- the failure
this whole change exists to fix, and the only one with no error message behind
it. And that an outbound message reaches the workspace it belongs to rather than
whichever instance the index happened to scan first.
"""

from __future__ import annotations

import logging
import time

import pytest

from jiuwenswarm.common.schema.message import EventType, Message
from jiuwenswarm.gateway.channel_manager.base import RobotMessageRouter
from jiuwenswarm.gateway.channel_manager.channel_manager import ChannelManager
from jiuwenswarm.gateway.channel_manager.im_platforms.slack.slack_connect import (
    SlackChannel,
    SlackChannelConfig,
)
from jiuwenswarm.gateway.routing.keys import ChannelKey


class _FakeMessageHandler:
    """The two accessors ChannelManager reaches for on the paths under test."""

    def __init__(self) -> None:
        self.agent_client = None

    @staticmethod
    def resolve_app_id(msg: Message) -> str:
        return getattr(msg, "app_id", None) or getattr(msg, "bot_id", None) or "default"


def _channel(app_id: str, *, team_id: str = "") -> SlackChannel:
    channel = SlackChannel(
        SlackChannelConfig(
            enabled=True,
            app_id=app_id,
            bot_token=f"xoxb-{app_id or 'default'}",
            app_token=f"xapp-{app_id or 'default'}",
        ),
        RobotMessageRouter(),
    )
    # What auth.test would have written. Set here because nothing in this test
    # opens a connection.
    channel._workspace_team_id = team_id
    return channel


def _manager() -> ChannelManager:
    return ChannelManager(_FakeMessageHandler())


def _reply(session_id: str = "", metadata: dict | None = None) -> Message:
    return Message(
        id="reply-1",
        type="event",
        channel_id="slack",
        session_id=session_id,
        params={},
        timestamp=time.time(),
        ok=True,
        payload={"content": "hello"},
        event_type=EventType.CHAT_FINAL,
        metadata=metadata or {},
    )


def test_one_workspace_keys_where_it_always_keyed() -> None:
    """A single-workspace deployment must be untouched by app_id existing."""
    manager = _manager()
    channel = _channel("")

    manager.register_channel(channel)

    assert list(manager._channels) == [ChannelKey("slack", "default")]
    assert manager.get_by_key(ChannelKey("slack", "default")) is channel


def test_two_workspaces_register_as_two_channels() -> None:
    """The collision this change exists to fix.

    Before app_id existed both instances keyed as ("slack", "default") and the
    second replaced the first in the index, with no log line and no error: one
    configured workspace simply vanished from the deployment.
    """
    manager = _manager()
    first = _channel("workspace-1")
    second = _channel("workspace-2")

    manager.register_channel(first)
    manager.register_channel(second)

    assert set(manager._channels) == {
        ChannelKey("slack", "workspace-1"),
        ChannelKey("slack", "workspace-2"),
    }
    assert manager.get_by_key(ChannelKey("slack", "workspace-1")) is first
    assert manager.get_by_key(ChannelKey("slack", "workspace-2")) is second
    # Both are reachable by channel_id as well, which is what the stop path
    # walks on a hot reload.
    assert set(manager.get_channels_by_id("slack")) == {first, second}


def test_a_shared_key_is_reported_rather_than_swallowed(caplog) -> None:
    manager = _manager()
    first = _channel("")
    second = _channel("")

    with caplog.at_level(
        logging.ERROR,
        logger="jiuwenswarm.gateway.channel_manager.channel_manager",
    ):
        manager.register_channel(first)
        manager.register_channel(second)

    assert "ChannelKey" in caplog.text
    # The newcomer still wins, so a stop path that failed to unregister cannot
    # strand a dead channel in the index for good.
    assert manager.get_by_key(ChannelKey("slack", "default")) is second


@pytest.mark.parametrize(
    ("metadata", "session_id", "expected"),
    [
        ({"slack_team_id": "T-ACME"}, "", "acme"),
        ({"slack_team_id": "T-OTHER"}, "", "other"),
        # A scheduled push carries no inbound metadata; its session id opens
        # with the workspace it was created in.
        ({}, "slack_T-ACME_C0_1710000005.000600", "acme"),
        ({}, "slack_T-OTHER_C0", "other"),
        # Metadata wins over the session id when both are present.
        ({"slack_team_id": "T-OTHER"}, "slack_T-ACME_C0", "other"),
    ],
)
def test_an_outbound_message_resolves_to_its_own_workspace(
    metadata: dict, session_id: str, expected: str
) -> None:
    manager = _manager()
    acme = _channel("workspace-1", team_id="T-ACME")
    other = _channel("workspace-2", team_id="T-OTHER")
    manager.register_channel(acme)
    manager.register_channel(other)
    by_name = {"acme": acme, "other": other}

    resolved = manager.resolve_outbound_channel(
        _reply(session_id=session_id, metadata=metadata)
    )

    assert resolved is by_name[expected]


def test_an_unattributable_message_is_refused_rather_than_guessed(caplog) -> None:
    """Returning the first instance would post one workspace's reply in another."""
    manager = _manager()
    manager.register_channel(_channel("workspace-1", team_id="T-ACME"))
    manager.register_channel(_channel("workspace-2", team_id="T-OTHER"))

    with caplog.at_level(
        logging.WARNING,
        logger="jiuwenswarm.gateway.channel_manager.channel_manager",
    ):
        assert manager.resolve_outbound_channel(_reply()) is None
        # A team nobody serves is equally unattributable.
        assert (
            manager.resolve_outbound_channel(
                _reply(metadata={"slack_team_id": "T-THIRD"})
            )
            is None
        )
    assert "slack" in caplog.text


_DROP_LINE = "无法判定出站消息归属于"


def _drop_records(caplog) -> list:
    """Only the rung-3 drop lines; registration chatter shares the logger."""
    return [r for r in caplog.records if _DROP_LINE in r.getMessage()]


def _drop_warnings(caplog) -> list[str]:
    return [
        r.getMessage() for r in _drop_records(caplog) if r.levelno == logging.WARNING
    ]


def test_the_unattributable_drop_is_warned_once_not_once_per_interval(caplog) -> None:
    """The health-check relay and a web-made Slack cron push arrive on a timer.

    Both reach rung 3 with nothing to resolve on, so an unlatched warning is
    reported for as long as the process runs: every ``interval_seconds`` for
    the relay, every firing for the cron job. The condition is a standing
    property of the configuration and is worth one line.
    """
    manager = _manager()
    manager.register_channel(_channel("workspace-1", team_id="T-ACME"))
    manager.register_channel(_channel("workspace-2", team_id="T-OTHER"))

    with caplog.at_level(
        logging.DEBUG,
        logger="jiuwenswarm.gateway.channel_manager.channel_manager",
    ):
        for _ in range(5):
            assert manager.resolve_outbound_channel(_reply()) is None

    dropped = _drop_records(caplog)
    assert len(dropped) == 5, "every drop stays traceable"
    warnings = [r for r in dropped if r.levelno == logging.WARNING]
    assert len(warnings) == 1, "the condition is reported once, not once per interval"
    assert dropped[0].levelno == logging.WARNING
    assert all(r.levelno == logging.DEBUG for r in dropped[1:])


def test_the_latch_is_held_per_channel_id_not_process_wide(caplog) -> None:
    """One connector's standing condition must not silence another's first report."""
    manager = _manager()
    manager.register_channel(_channel("workspace-1", team_id="T-ACME"))
    manager.register_channel(_channel("workspace-2", team_id="T-OTHER"))
    # Two instances under a second connector, registered by key so the fixture
    # above can stand in for one this test does not need to build.
    for app_id in ("app-1", "app-2"):
        manager.register_external_channel(
            ChannelKey("other", app_id), _channel(app_id, team_id=f"T-{app_id}")
        )
    manager.resolve_outbound_channel(_reply())

    with caplog.at_level(
        logging.WARNING,
        logger="jiuwenswarm.gateway.channel_manager.channel_manager",
    ):
        caplog.clear()
        assert manager.resolve_outbound_channel(_reply()) is None
        assert _drop_warnings(caplog) == [], "slack has already been reported"
        elsewhere = _reply()
        elsewhere.channel_id = "other"
        assert manager.resolve_outbound_channel(elsewhere) is None

    reported = _drop_warnings(caplog)
    assert len(reported) == 1
    assert "other" in reported[0]


def test_an_instance_that_has_not_identified_itself_claims_nothing() -> None:
    """Before auth.test answers, a connection cannot say which workspace it is."""
    manager = _manager()
    manager.register_channel(_channel("workspace-1", team_id=""))
    manager.register_channel(_channel("workspace-2", team_id="T-OTHER"))

    assert (
        manager.resolve_outbound_channel(_reply(metadata={"slack_team_id": "T-ACME"}))
        is None
    )


def test_one_workspace_never_asks_who_owns_a_message() -> None:
    """With one instance there is nothing to disambiguate, and nothing is.

    A single-workspace deployment must keep delivering a message that names no
    team -- a heartbeat relay, a push rebuilt without metadata -- exactly as it
    always did, whether or not auth.test has answered yet.
    """
    manager = _manager()
    only = _channel("", team_id="")
    manager.register_channel(only)

    assert manager.resolve_outbound_channel(_reply()) is only
    assert (
        manager.resolve_outbound_channel(_reply(metadata={"slack_team_id": "T-ANY"}))
        is only
    )


def test_the_exact_key_still_wins_before_anyone_is_asked() -> None:
    """app_id on the message is the precise route; the team match is the fallback."""
    manager = _manager()
    acme = _channel("workspace-1", team_id="T-ACME")
    other = _channel("workspace-2", team_id="T-OTHER")
    manager.register_channel(acme)
    manager.register_channel(other)

    msg = _reply(metadata={"slack_team_id": "T-ACME"})
    msg.app_id = "workspace-2"

    assert manager.resolve_outbound_channel(msg) is other


def test_a_platform_that_cannot_say_keeps_its_first_match() -> None:
    """Refusing would break multi-app Feishu, which implements no claims_message."""

    class _Plain:
        def __init__(self, app_id: str) -> None:
            self.channel_id = "feishu"
            self.app_id = app_id

        def on_message(self, callback) -> None:  # noqa: ANN001 - test double
            pass

    manager = _manager()
    first = _Plain("cli_one")
    manager.register_channel(first)
    manager.register_channel(_Plain("cli_two"))

    msg = _reply()
    msg.channel_id = "feishu"

    assert manager.resolve_outbound_channel(msg) is first
