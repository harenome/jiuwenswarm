"""``all`` means all, on the mention route as well as the message route.

Written against a live defect: a conversation configured with ``mode: [all]``
answered every message in it except the ones addressed to the bot. Slack
delivers a message opening with a bot mention twice, once as ``message`` and
once as ``app_mention``. The message route bowed out because ``app_mention``
was going to take it, and the mention route refused it because ``mention`` was
not listed -- a scope's ``mode`` replaces rather than extends, so ``[all]``
holds no ``mention``. The two routes disagreed about whether ``all`` is a
superset and the message fell between them.

Both routes now accept it, which makes the durable dedup the only thing
standing between one Slack message and two turns. That is pinned here by
counting dispatches.
"""

from __future__ import annotations

import pytest

from jiuwenswarm.common.schema.message import Message
from jiuwenswarm.gateway.channel_manager.base import RobotMessageRouter
from jiuwenswarm.gateway.channel_manager.im_platforms.slack import slack_connect
from jiuwenswarm.gateway.channel_manager.im_platforms.slack.slack_connect import (
    SlackChannel,
    SlackChannelConfig,
    SlackChannelOverride,
)

_BOT_USER_ID = "U0BOTUSER1"
_SENDER_ID = "U0SENDER01"
_WATCHED_CHANNEL = "C0WATCHED1"


@pytest.fixture(autouse=True)
def _isolated_dedup_store(tmp_path, monkeypatch):
    """Give every test its own dedup file, never the real workspace's."""
    real_init = slack_connect.SlackEventDedupStore.__init__
    monkeypatch.setattr(
        slack_connect.SlackEventDedupStore,
        "__init__",
        lambda self, path=None, **kw: real_init(
            self, path or tmp_path / "slack_seen_events.json", **kw
        ),
    )


async def _noop(*_args, **_kwargs) -> None:
    return None


def _channel(*modes: str) -> tuple[SlackChannel, list[Message]]:
    """A connector watching one channel whose ``mode`` lists ``modes``."""
    channel = SlackChannel(
        SlackChannelConfig(
            enabled=True,
            allow_from=[_SENDER_ID],
            conversation_overrides={
                _WATCHED_CHANNEL: SlackChannelOverride(mode=frozenset(modes))
            },
        ),
        RobotMessageRouter(),
    )
    channel._running = True
    channel._bot_user_id = _BOT_USER_ID
    channel._acknowledge_request = _noop  # type: ignore[method-assign]
    received: list[Message] = []
    channel.on_message(received.append)
    return channel, received


def _message_event(ts: str, text: str) -> dict:
    return {
        "type": "message",
        "channel_type": "channel",
        "channel": _WATCHED_CHANNEL,
        "user": _SENDER_ID,
        "text": text,
        "ts": ts,
    }


def _app_mention_event(ts: str, text: str) -> dict:
    return {
        "type": "app_mention",
        "channel": _WATCHED_CHANNEL,
        "user": _SENDER_ID,
        "text": text,
        "ts": ts,
    }


def _body(event_id: str) -> dict:
    return {"event_id": event_id, "team_id": "T0TESTTEAM"}


@pytest.mark.asyncio
async def test_a_channel_that_answers_everything_answers_a_message_addressed_to_it() -> (
    None
):
    """The defect. ``[all]`` ignored the one message that named the bot.

    Slack sends this as ``app_mention``, so the mention route is where it is
    accepted or lost.
    """
    channel, received = _channel("all")

    outcome = await channel._route_app_mention(
        _app_mention_event("1710000101.000100", f"<@{_BOT_USER_ID}> what is the status?"),
        _body("EvMentionUnderAll"),
    )

    assert outcome.startswith("dispatched:")
    assert len(received) == 1


@pytest.mark.asyncio
async def test_a_channel_that_answers_everything_still_answers_an_ordinary_message() -> (
    None
):
    channel, received = _channel("all")

    outcome = await channel._route_message_event(
        _message_event("1710000102.000100", "the build finished"),
        _body("EvPlainUnderAll"),
    )

    assert outcome.startswith("dispatched:all")
    assert len(received) == 1


@pytest.mark.asyncio
async def test_a_channel_configured_for_mentions_alone_answers_only_mentions() -> None:
    """``[mention]`` is untouched: it takes the mention and drops the rest."""
    channel, received = _channel("mention")

    mentioned = await channel._route_app_mention(
        _app_mention_event("1710000103.000100", f"<@{_BOT_USER_ID}> ping"),
        _body("EvMentionUnderMention"),
    )
    ordinary = await channel._route_message_event(
        _message_event("1710000104.000100", "the build finished"),
        _body("EvPlainUnderMention"),
    )

    assert mentioned.startswith("dispatched:mention")
    assert ordinary == "ignored:no-trigger-matched"
    assert len(received) == 1


@pytest.mark.asyncio
async def test_a_channel_that_answers_nothing_still_ignores_a_mention() -> None:
    """The refusal that route exists for stays reachable.

    A conversation whose ``mode`` settles on neither ``mention`` nor ``all``
    has opted out of mentions, and widening the gate must not have widened it
    to everyone.
    """
    channel, received = _channel("url")

    outcome = await channel._route_app_mention(
        _app_mention_event("1710000105.000100", f"<@{_BOT_USER_ID}> ping"),
        _body("EvMentionUnderUrl"),
    )

    assert outcome == "ignored:mention-not-a-trigger-here"
    assert received == []


@pytest.mark.asyncio
async def test_one_message_delivered_as_both_event_types_dispatches_once() -> None:
    """The regression this change could introduce, counted rather than inspected.

    A leading mention reaches this connector twice. Before, exactly one of the
    two routes accepted it because ``[all]`` refused the mention; now both
    would, and the dedup keyed on team, channel and timestamp is what keeps the
    result at one turn.
    """
    channel, received = _channel("all")
    ts = "1710000106.000100"
    text = f"<@{_BOT_USER_ID}> summarise today"

    from_mention = await channel._route_app_mention(
        _app_mention_event(ts, text), _body("EvBothMention")
    )
    from_message = await channel._route_message_event(
        _message_event(ts, text), _body("EvBothMessage")
    )

    assert from_mention.startswith("dispatched:")
    assert from_message == "ignored:leading-bot-mention-handled-as-app_mention"
    assert len(received) == 1


@pytest.mark.asyncio
async def test_a_mention_mid_sentence_under_all_dispatches_once() -> None:
    """The pair the dedup alone separates.

    A mention that does not lead raises ``app_mention`` like any other, and the
    message route does not bow out for it -- the early return reads the opening
    of the text. Under ``all`` both routes now claim it, so the two events reach
    the dedup and the second is turned away there.
    """
    channel, received = _channel("all")
    ts = "1710000107.000100"
    text = f"could <@{_BOT_USER_ID}> summarise today"

    from_message = await channel._route_message_event(
        _message_event(ts, text), _body("EvMidMessage")
    )
    from_mention = await channel._route_app_mention(
        _app_mention_event(ts, text), _body("EvMidMention")
    )

    assert from_message.startswith("dispatched:all")
    assert from_mention == "ignored:already-handled (dedupe)"
    assert len(received) == 1


@pytest.mark.asyncio
async def test_a_mid_sentence_mention_dispatches_once_whichever_event_lands_first() -> (
    None
):
    """Slack orders the two deliveries however it likes, and neither is a retry.

    Same message as above with the routes called the other way round, so the
    count does not depend on which event won the race.
    """
    channel, received = _channel("all")
    ts = "1710000108.000100"
    text = f"could <@{_BOT_USER_ID}> summarise today"

    from_mention = await channel._route_app_mention(
        _app_mention_event(ts, text), _body("EvMidMentionFirst")
    )
    from_message = await channel._route_message_event(
        _message_event(ts, text), _body("EvMidMessageSecond")
    )

    assert from_mention.startswith("dispatched:mention")
    assert from_message == "ignored:already-handled (dedupe)"
    assert len(received) == 1
