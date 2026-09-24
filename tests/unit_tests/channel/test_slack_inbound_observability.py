"""The inbound Slack path must not be able to lose a message silently.

Written against a live incident in which direct messages arrived in Slack,
were visible in ``conversations.history``, and produced no turn, no reply, no
acknowledgement reaction and not one log line anywhere. The ingestion path had
thirteen ways to stop before dispatching, twelve of them a bare ``return``, so
all twelve looked identical from outside -- and identical to Slack never having
sent the message at all. These tests hold that closed.
"""

from __future__ import annotations

import logging

import pytest

from jiuwenswarm.common.schema.message import Message
from jiuwenswarm.gateway.channel_manager.base import RobotMessageRouter
from jiuwenswarm.gateway.channel_manager.im_platforms.slack import slack_connect
from jiuwenswarm.gateway.channel_manager.im_platforms.slack.slack_connect import (
    SlackChannel,
    SlackChannelConfig,
    SlackChannelOverride,
)

_LOGGER_NAME = slack_connect.logger.name


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


def _dm_channel(**config_kwargs) -> tuple[SlackChannel, list[Message]]:
    config_kwargs.setdefault("enabled", True)
    config_kwargs.setdefault("allow_from", ["U0ALLOWED1"])
    channel = SlackChannel(
        SlackChannelConfig(**config_kwargs),
        RobotMessageRouter(),
    )
    channel._running = True
    channel._acknowledge_request = _noop  # type: ignore[method-assign]
    received: list[Message] = []
    channel.on_message(received.append)
    return channel, received


async def _noop(*_args, **_kwargs) -> None:
    return None


def _end_turn(channel: SlackChannel, request: Message) -> None:
    """Close the turn ``request`` opened, so the next message starts its own.

    A message arriving while a turn is running is held rather than dispatched,
    because ``queue`` is what a conversation nobody configured does. A test
    wanting two dispatched messages wants two turns, and this is the first one
    ending, without the reply delivery a terminal event would also perform.
    """
    channel._forget_turn_initiator(request.session_id, request.id)


def _dm_event(ts: str, text: str, **extra) -> dict:
    event = {
        "type": "message",
        "channel_type": "im",
        "channel": "D0DIRECT01",
        "user": "U0ALLOWED1",
        "text": text,
        "ts": ts,
    }
    event.update(extra)
    return event


def _body(event_id: str) -> dict:
    return {"event_id": event_id, "team_id": "T0TESTTEAM"}


def _outcome_lines(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if " outcome=" in r.getMessage()]


@pytest.mark.asyncio
async def test_a_dispatched_direct_message_is_reported_at_the_boundary(caplog) -> None:
    channel, received = _dm_channel()
    with caplog.at_level(logging.INFO, logger=_LOGGER_NAME):
        await channel._handle_message_event(
            _dm_event("1710000001.000100", "the parcel arrived"),
            _body("EvDirect"),
        )

    assert len(received) == 1
    (line,) = _outcome_lines(caplog)
    assert "channel=D0DIRECT01" in line
    assert "ts=1710000001.000100" in line
    assert "subtype=-" in line
    assert "outcome=dispatched:dm" in line
    # The identity is enough to find the message in Slack, and stops short of
    # what it said: this line is written for every event including the ignored
    # ones, so it has to be safe to leave on at INFO.
    assert "the parcel arrived" not in line


@pytest.mark.asyncio
async def test_a_second_delivery_of_one_message_says_it_was_deduplicated(caplog) -> None:
    channel, received = _dm_channel()
    event = _dm_event("1710000002.000100", "Four cats sit on a fence.")
    with caplog.at_level(logging.INFO, logger=_LOGGER_NAME):
        await channel._handle_message_event(event, _body("Ev1"))
        # Slack redelivers an envelope it did not see acked in time. The second
        # arrival is correct behaviour and must not read as a lost message.
        await channel._handle_message_event(event, _body("Ev1-retry"))

    assert len(received) == 1
    first, second = _outcome_lines(caplog)
    assert "outcome=dispatched:dm" in first
    assert "outcome=ignored:already-handled (dedupe)" in second


@pytest.mark.asyncio
async def test_the_same_text_sent_twice_is_two_distinguishable_lines(caplog) -> None:
    """A prompt sent twice, as in the incident: same words, two Slack messages.

    Both were lost and the pair was the reason a content-keyed dedup was
    suspected. The boundary line is keyed on ``ts``, so the two are separable
    even though nothing about their text is.
    """
    channel, received = _dm_channel()
    text = "Four cats - one white, three black - sit on a fence."
    with caplog.at_level(logging.INFO, logger=_LOGGER_NAME):
        await channel._handle_message_event(_dm_event("1710000002.000100", text), _body("EvA"))
        # Two prompts, two turns. What is under test is that the line naming the
        # second is separable from the line naming the first, which is a
        # question about the line rather than about a message arriving mid-turn.
        _end_turn(channel, received[-1])
        await channel._handle_message_event(_dm_event("1710000003.000100", text), _body("EvB"))

    assert len(received) == 2
    first, second = _outcome_lines(caplog)
    assert "ts=1710000002.000100" in first
    assert "ts=1710000003.000100" in second
    assert first != second


@pytest.mark.asyncio
async def test_an_edit_is_reported_as_ignored_rather_than_passed_over(caplog) -> None:
    channel, received = _dm_channel()
    with caplog.at_level(logging.INFO, logger=_LOGGER_NAME):
        await channel._handle_message_event(
            _dm_event("1710000004.000100", "", subtype="message_changed"),
            _body("EvEdit"),
        )

    assert received == []
    (line,) = _outcome_lines(caplog)
    assert "subtype=message_changed" in line
    assert "outcome=ignored:subtype-not-user-content (message_changed)" in line


@pytest.mark.asyncio
async def test_a_stopped_channel_says_so_instead_of_returning_in_silence(caplog) -> None:
    channel, received = _dm_channel()
    channel._running = False
    with caplog.at_level(logging.INFO, logger=_LOGGER_NAME):
        await channel._handle_message_event(
            _dm_event("1710000005.000100", "is anyone there"), _body("EvStopped")
        )

    assert received == []
    (line,) = _outcome_lines(caplog)
    assert "outcome=dropped:channel-not-running" in line


@pytest.mark.asyncio
async def test_a_handler_exception_is_logged_at_the_connector_edge(caplog) -> None:
    """slack_bolt swallows this. It must not also be invisible here.

    Bolt catches whatever a listener raises and reports it on its own logger,
    which holds no handler unless ``configure_sdk_logging`` has run. A raise
    inside the handler was therefore perfectly silent, and from the channel it
    looked exactly like a message Slack never sent.
    """
    channel, _received = _dm_channel()

    async def _boom(*_args, **_kwargs):
        raise RuntimeError("wire fell off")

    channel._handle_slack_event = _boom  # type: ignore[method-assign]

    with caplog.at_level(logging.INFO, logger=_LOGGER_NAME):
        # Does not propagate: bolt would swallow it anyway, and a listener that
        # raises out of this handler takes no other message with it.
        await channel._handle_message_event(
            _dm_event("1710000001.000100", "the parcel arrived"), _body("EvBoom")
        )

    raised = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert raised, "an exception inside the handler must reach the log"
    assert "ts=1710000001.000100" in raised[0].getMessage()
    # The traceback comes with it: knowing a message was lost to a raise is
    # only half of what the next occurrence needs.
    assert "RuntimeError: wire fell off" in caplog.text
    (line,) = _outcome_lines(caplog)
    assert "outcome=handler-raised" in line


# ---------------------------------------------------------------------------
# One name per kind of event, whichever surface it arrived on.
#
# The outcome vocabulary is only worth logging if each name means one thing.
# ``ignored:no-trigger-matched`` had come to mean three: human conversation in a
# watched channel, a join notice or an edit that is not user content at all, and
# a question addressed to the bot in a way that could never wake it. Nineteen
# events in one night, and the third was one of them.
# ---------------------------------------------------------------------------

_BOT_USER_ID = "U0BOTUSER1"
_BOT_ID = "B0BOTAPP01"
_WATCHED_CHANNEL = "C0WATCHED1"


def _channel_channel(**config_kwargs) -> tuple[SlackChannel, list[Message]]:
    """A connector watching one channel in the default ``mention`` mode.

    Which is the deployed shape: the channel has triggers configured, but the
    only one is ``mention``, and ``mention`` is delivered as ``app_mention`` --
    so every ordinary message in it reaches the end of the trigger match.
    """
    config_kwargs.setdefault("enabled", True)
    config_kwargs.setdefault("allow_from", ["U0ALLOWED1", "U0SENDER01"])
    channel = SlackChannel(SlackChannelConfig(**config_kwargs), RobotMessageRouter())
    channel._running = True
    channel._bot_user_id = _BOT_USER_ID
    channel._bot_id = _BOT_ID
    channel._acknowledge_request = _noop  # type: ignore[method-assign]
    received: list[Message] = []
    channel.on_message(received.append)
    return channel, received


def _channel_event(ts: str, text: str, **extra) -> dict:
    event = {
        "type": "message",
        "channel_type": "channel",
        "channel": _WATCHED_CHANNEL,
        "user": "U0SENDER01",
        "text": text,
        "ts": ts,
    }
    event.update(extra)
    return event


def _outcome_of(line: str) -> str:
    return line.split(" outcome=", 1)[1]


@pytest.mark.asyncio
@pytest.mark.parametrize("subtype", ["channel_join", "message_changed"])
async def test_a_non_content_subtype_is_named_the_same_in_a_channel_as_in_a_dm(
    subtype, caplog
) -> None:
    """The asymmetry the vocabulary was hiding, held closed in both directions.

    Observed live within the same hour: an edit in a DM reported
    ``ignored:subtype-not-user-content (message_changed)``, and the identical
    edit in a channel reported ``ignored:no-trigger-matched``. Four
    ``channel_join`` notices went the same way -- the bot being invited to four
    channels at once, filed as though four people had been chatting.
    """
    dm_channel, dm_received = _dm_channel()
    dm_channel._bot_user_id = _BOT_USER_ID
    ch_channel, ch_received = _channel_channel()

    with caplog.at_level(logging.INFO, logger=_LOGGER_NAME):
        await dm_channel._handle_message_event(
            _dm_event("1710000006.000100", "", subtype=subtype), _body("EvDM")
        )
        await ch_channel._handle_message_event(
            _channel_event("1710000007.000100", "", subtype=subtype), _body("EvCh")
        )

    assert dm_received == []
    assert ch_received == []
    in_dm, in_channel = (_outcome_of(line) for line in _outcome_lines(caplog))
    assert in_dm == f"ignored:subtype-not-user-content ({subtype})"
    assert in_channel == in_dm


@pytest.mark.asyncio
async def test_a_join_notice_is_not_user_content_before_it_is_an_unwatched_channel(
    caplog,
) -> None:
    """Being a join notice is the more fundamental fact, so it is reported first.

    Otherwise the same ``channel_join`` would be named three ways depending on
    where it landed, and the one name would still not mean one thing.
    """
    channel, received = _channel_channel(group_chat_mode="off")
    with caplog.at_level(logging.INFO, logger=_LOGGER_NAME):
        await channel._handle_message_event(
            _channel_event("1710000008.000100", "", subtype="channel_join"),
            _body("EvJoin"),
        )

    assert received == []
    (line,) = _outcome_lines(caplog)
    assert _outcome_of(line) == "ignored:subtype-not-user-content (channel_join)"


@pytest.mark.asyncio
async def test_an_ordinary_channel_message_still_reports_no_trigger_matched(
    caplog,
) -> None:
    """The bucket keeps its original meaning; it just stops holding the rest."""
    channel, received = _channel_channel()
    with caplog.at_level(logging.INFO, logger=_LOGGER_NAME):
        await channel._handle_message_event(
            _channel_event("1710000009.000100", "the kettle is on"), _body("EvChat")
        )

    assert received == []
    (line,) = _outcome_lines(caplog)
    assert _outcome_of(line) == "ignored:no-trigger-matched"


@pytest.mark.asyncio
async def test_a_question_addressed_to_the_bot_id_is_not_filed_as_chatter(
    caplog,
) -> None:
    """The lost message: a real request, ignored in silence, logged as noise.

    ``B0BOTAPP01`` is the app's bot id. Slack only makes a mention out of the
    bot's user id, so this raised no app_mention, matched no trigger, and got no
    reply and no error. It is still ignored -- a bot id is not a way to address
    this bot -- but it no longer looks like the chatter around it.
    """
    channel, received = _channel_channel()
    with caplog.at_level(logging.INFO, logger=_LOGGER_NAME):
        await channel._handle_message_event(
            _channel_event(
                "1710000010.000100",
                f"<@{_BOT_ID}> put every open ticket into a spreadsheet"
                " for me, sorted by age.",
            ),
            _body("EvBotIdMention"),
        )

    assert received == [], "naming the case must not start dispatching it"
    (line,) = _outcome_lines(caplog)
    assert _outcome_of(line) == "ignored:bot-id-mention-not-a-trigger"
    # Still the identity only: this line reports that the bot was named the
    # wrong way, not what was asked.
    assert "spreadsheet" not in line


@pytest.mark.asyncio
async def test_mentioning_a_colleague_stays_ordinary_chatter(caplog) -> None:
    """Someone addressing a human in a watched channel is not a near-miss.

    This is the case that keeps the new name worth reading: it fires on one id
    that this connector knows to be its own, never on a guess about who a
    message was for.
    """
    channel, _received = _channel_channel()
    with caplog.at_level(logging.INFO, logger=_LOGGER_NAME):
        await channel._handle_message_event(
            _channel_event("1710000011.000100", "<@U0COLLEAG1> does this look right to you?"),
            _body("EvColleague"),
        )

    (line,) = _outcome_lines(caplog)
    assert _outcome_of(line) == "ignored:no-trigger-matched"


@pytest.mark.asyncio
async def test_a_typed_bot_name_is_not_reported_as_a_bot_id_mention(caplog) -> None:
    """``@name`` is a valid token elsewhere in this connector, so it is left alone."""
    channel, _received = _channel_channel()
    with caplog.at_level(logging.INFO, logger=_LOGGER_NAME):
        await channel._handle_message_event(
            _channel_event("1710000012.000100", "@jiuwenswarm anybody home?"),
            _body("EvTypedName"),
        )

    (line,) = _outcome_lines(caplog)
    assert _outcome_of(line) == "ignored:no-trigger-matched"


@pytest.mark.asyncio
async def test_a_bot_id_mention_that_matches_a_trigger_still_dispatches(caplog) -> None:
    """Naming the miss changes nothing about the messages that do wake the bot."""
    channel, received = _channel_channel(
        conversation_overrides={
            _WATCHED_CHANNEL: SlackChannelOverride(mode=frozenset({"mention", "url"}))
        },
        allow_from=["U0SENDER01"],
    )
    with caplog.at_level(logging.INFO, logger=_LOGGER_NAME):
        await channel._handle_message_event(
            _channel_event(
                "1710000013.000100",
                f"<@{_BOT_ID}> look at https://example.invalid/pr/1",
            ),
            _body("EvUrlTrigger"),
        )

    assert len(received) == 1
    (line,) = _outcome_lines(caplog)
    assert _outcome_of(line).startswith("dispatched:url")


@pytest.mark.asyncio
async def test_the_correct_mention_wins_even_carrying_a_bot_id_beside_it(
    caplog,
) -> None:
    """What a retype looks like when the first attempt woke nothing: both ids.

    The valid mention leads, so app_mention takes the message and the ``message``
    copy of it says so. The new name must not step in front of that.
    """
    channel, _received = _channel_channel()
    with caplog.at_level(logging.INFO, logger=_LOGGER_NAME):
        await channel._handle_message_event(
            _channel_event(
                "1710000014.000100",
                f"<@{_BOT_USER_ID}> <@{_BOT_ID}> list the open tickets"
                " for me, sorted by age.",
            ),
            _body("EvLeadingBotId"),
        )

    (line,) = _outcome_lines(caplog)
    assert _outcome_of(line) == "ignored:leading-bot-mention-handled-as-app_mention"


@pytest.mark.asyncio
async def test_an_unresolved_bot_id_falls_back_to_the_old_name(caplog) -> None:
    """auth.test can fail at startup. Nothing may be claimed about the id then."""
    channel, _received = _channel_channel()
    channel._bot_id = ""
    with caplog.at_level(logging.INFO, logger=_LOGGER_NAME):
        await channel._handle_message_event(
            _channel_event("1710000010.000100", f"<@{_BOT_ID}> where are the tickets?"),
            _body("EvNoBotId"),
        )

    (line,) = _outcome_lines(caplog)
    assert _outcome_of(line) == "ignored:no-trigger-matched"


@pytest.mark.asyncio
async def test_auth_test_keeps_the_bot_id_beside_the_bot_user_id() -> None:
    """Both ids arrive in one payload, and the bot id must not be dropped."""

    class _Client:
        async def auth_test(self):
            return {"ok": True, "user_id": _BOT_USER_ID, "bot_id": _BOT_ID}

    channel = SlackChannel(SlackChannelConfig(enabled=True), RobotMessageRouter())
    channel._client = _Client()
    await channel._load_bot_user_id()

    assert channel._bot_user_id == _BOT_USER_ID
    assert channel._bot_id == _BOT_ID


@pytest.mark.asyncio
async def test_an_app_post_is_named_the_same_in_a_channel_as_in_a_dm(caplog) -> None:
    """The loop protection moved with the subtype check, and moved together.

    It is one predicate with two callers now; this is what keeps it one.
    """
    dm_channel, _dm_received = _dm_channel()
    ch_channel, _ch_received = _channel_channel()
    with caplog.at_level(logging.INFO, logger=_LOGGER_NAME):
        await dm_channel._handle_message_event(
            _dm_event("1710000015.000100", "posted by an app", bot_id="B0BOTHER"),
            _body("EvAppDM"),
        )
        await ch_channel._handle_message_event(
            _channel_event("1710000015.000100", "posted by an app", bot_id="B0BOTHER"),
            _body("EvAppCh"),
        )

    in_dm, in_channel = (_outcome_of(line) for line in _outcome_lines(caplog))
    assert in_dm == "ignored:posted-by-an-app"
    assert in_channel == in_dm


# ---------------------------------------------------------------------------
# channels.slack.app_messages
#
# The first check above drops anything an app touched, and a message posted with
# a *user* token is touched by one: it carries the person in ``user`` and the app
# in ``bot_id``/``app_id`` at the same time. Measured on a live workspace, a
# message posted as a real person read back as user=U0C17A65C5D, bot_id=B…,
# app_id=A…, and was discarded for the second half of that while the first half
# was correct. ``app_messages`` is the ladder an operator can raise against that
# check -- none, listed, all -- and these hold open exactly how far each rung
# goes and what no rung reaches.
# ---------------------------------------------------------------------------

_PERSONAS_APP = "A0PERSONAS1"
_PERSONAS_BOT = "B0PERSONAS1"
_OWN_APP = "A0OWNAPP001"


def _app_dm_event(ts: str, text: str, **extra) -> dict:
    """A direct message a person posted through an app holding a user token."""
    extra.setdefault("bot_id", _PERSONAS_BOT)
    extra.setdefault("app_id", _PERSONAS_APP)
    return _dm_event(ts, text, **extra)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "config_kwargs",
    [
        {"app_messages": "listed", "app_messages_from": (_PERSONAS_APP,)},
        {"app_messages": "all"},
    ],
    ids=["listed", "all"],
)
async def test_an_admitted_app_posts_as_the_person_its_user_field_names(
    config_kwargs, caplog
) -> None:
    """The point of the key: the person posted, through an app, and is dispatched.

    Nothing is reattributed. ``user`` was already the right person, so the
    message goes down the ordinary path as that person's, and the outcome is the
    ordinary dispatch outcome rather than one of this key's own. Both admitting
    words reach the same place, which is the property that makes ``listed`` the
    middle of a ladder rather than a separate feature.
    """
    channel, received = _dm_channel(**config_kwargs)
    channel._bot_user_id = _BOT_USER_ID
    with caplog.at_level(logging.INFO, logger=_LOGGER_NAME):
        await channel._handle_message_event(
            _app_dm_event("1710000020.000100", "posted through an app, by a person"),
            _body("EvAdmittedApp"),
        )

    assert len(received) == 1
    assert received[0].user_id == "U0ALLOWED1"
    (line,) = _outcome_lines(caplog)
    # The dispatch names the session it opened, and that session is keyed on the
    # person -- the attribution under test, stated by the connector itself
    # rather than only by the message object above.
    assert _outcome_of(line).startswith("dispatched:dm ")
    assert "_U0ALLOWED1" in _outcome_of(line)
    # A dispatch names the dispatch, so without this the line would say nothing
    # about an app having been involved at all -- the one disposition the
    # outcome string cannot report for itself.
    assert f"app_messages={config_kwargs['app_messages']}" in line
    assert f"app_id={_PERSONAS_APP}" in line


@pytest.mark.asyncio
async def test_an_app_that_is_not_listed_is_told_the_list_was_consulted(
    caplog,
) -> None:
    """``posted-by-an-app`` would not say whether anything had been checked.

    The id is quoted for the same reason the subtype is quoted elsewhere: an
    operator who has written the list and is still seeing drops needs the id to
    add, and the log line is where they are already looking.
    """
    channel, received = _dm_channel(
        app_messages="listed", app_messages_from=(_PERSONAS_APP,)
    )
    channel._bot_user_id = _BOT_USER_ID
    with caplog.at_level(logging.INFO, logger=_LOGGER_NAME):
        await channel._handle_message_event(
            _app_dm_event(
                "1710000021.000100",
                "posted by some other app",
                bot_id="B0OTHERAPP",
                app_id="A0OTHERAPP",
            ),
            _body("EvOtherApp"),
        )

    assert received == []
    (line,) = _outcome_lines(caplog)
    assert _outcome_of(line) == "ignored:app-not-in-app_messages_from (A0OTHERAPP)"
    assert "app_messages=" not in line


@pytest.mark.asyncio
async def test_an_app_with_no_app_id_is_named_apart_from_one_that_did_not_match(
    caplog,
) -> None:
    """A legacy integration or an incoming webhook: a bot_id and no app to key on.

    Reported as its own thing, because "there is no id here" and "your id is not
    this one" send an operator to two different places.
    """
    channel, received = _dm_channel(
        app_messages="listed", app_messages_from=(_PERSONAS_APP,)
    )
    channel._bot_user_id = _BOT_USER_ID
    event = _app_dm_event("1710000022.000100", "from a webhook", bot_id="B0WEBHOOK1")
    event.pop("app_id")
    with caplog.at_level(logging.INFO, logger=_LOGGER_NAME):
        await channel._handle_message_event(event, _body("EvWebhook"))

    assert received == []
    (line,) = _outcome_lines(caplog)
    assert _outcome_of(line) == "ignored:posted-by-an-app-with-no-app_id"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "config_kwargs",
    [
        {"app_messages": "listed", "app_messages_from": (_PERSONAS_APP,)},
        {"app_messages": "all"},
    ],
    ids=["listed", "all"],
)
async def test_an_admitted_app_posting_as_itself_is_still_dropped(
    config_kwargs, caplog
) -> None:
    """Every rung admits messages with a person behind them, and only those.

    An app posting as itself names nobody, and letting it through would hand
    every identity rule downstream -- allow_from, the scopes' user axis, the
    session key -- a message with no sender to apply itself to. ``all`` is
    included deliberately: the word widens which apps are read, not what a read
    message is allowed to be missing.
    """
    channel, received = _dm_channel(**config_kwargs)
    channel._bot_user_id = _BOT_USER_ID
    event = _app_dm_event("1710000023.000100", "the nightly report")
    event.pop("user")
    with caplog.at_level(logging.INFO, logger=_LOGGER_NAME):
        await channel._handle_message_event(event, _body("EvAppAsItself"))

    assert received == []
    (line,) = _outcome_lines(caplog)
    assert _outcome_of(line) == "ignored:app-posted-with-no-user"


@pytest.mark.asyncio
async def test_a_classic_bot_message_is_refused_under_the_widest_word(caplog) -> None:
    """What an incoming webhook actually looks like, and it is refused twice over.

    ``subtype: bot_message`` with no ``user``: the shape an app posting as itself
    through a webhook sends, as against an app posting through its own bot user,
    which carries a user id and no subtype and is the shape a rung admits. It is
    named for the missing sender rather than for the subtype because the sender
    floor is checked first, and that ordering is the thing worth pinning -- the
    subtype allow-list below would refuse it too.
    """
    channel, received = _dm_channel(app_messages="all")
    channel._bot_user_id = _BOT_USER_ID
    event = _app_dm_event(
        "1710000030.000100", "deploy finished", subtype="bot_message"
    )
    event.pop("user")
    with caplog.at_level(logging.INFO, logger=_LOGGER_NAME):
        await channel._handle_message_event(event, _body("EvWebhookPost"))

    assert received == []
    (line,) = _outcome_lines(caplog)
    assert _outcome_of(line) == "ignored:app-posted-with-no-user"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "config_kwargs, expected",
    [
        ({"app_messages": "none"}, "ignored:posted-by-an-app"),
        (
            {"app_messages": "listed", "app_messages_from": (_OWN_APP,)},
            "ignored:posted-by-this-bot",
        ),
        ({"app_messages": "all"}, "ignored:posted-by-this-bot"),
    ],
    ids=["none", "listed", "all"],
)
async def test_this_bot_is_dropped_under_every_word(
    config_kwargs, expected, caplog
) -> None:
    """The floor. No word and no list readmits this connector's own messages.

    Under ``none`` the blanket refusal covers it and names itself that way;
    under the two admitting words the identity check is what stops it, and under
    ``all`` that check is the only thing between the connector and answering
    itself. Listing the bot's own app id is the hostile case and is included:
    the list widens which apps are read and cannot reach the floor beneath it.
    """
    channel, received = _dm_channel(**config_kwargs)
    channel._bot_user_id = _BOT_USER_ID
    channel._bot_id = _BOT_ID
    with caplog.at_level(logging.INFO, logger=_LOGGER_NAME):
        await channel._handle_message_event(
            _app_dm_event(
                "1710000024.000100",
                "an answer this bot posted",
                user=_BOT_USER_ID,
                bot_id=_BOT_ID,
                app_id=_OWN_APP,
            ),
            _body("EvOwnEcho"),
        )

    assert received == []
    (line,) = _outcome_lines(caplog)
    assert _outcome_of(line) == expected


@pytest.mark.asyncio
async def test_the_bot_id_alone_is_enough_to_recognise_this_bots_own_post(
    caplog,
) -> None:
    """Either identity stops the echo, because a payload may carry only one.

    ``auth.test`` returns both and the connector keeps both. Requiring them
    together would make the floor depend on which fields a given Slack payload
    happened to set.
    """
    channel, received = _dm_channel(app_messages="all")
    channel._bot_user_id = ""
    channel._bot_id = _BOT_ID
    event = _app_dm_event(
        "1710000027.000100", "an answer this bot posted", bot_id=_BOT_ID
    )
    event.pop("user")
    with caplog.at_level(logging.INFO, logger=_LOGGER_NAME):
        await channel._handle_message_event(event, _body("EvOwnEchoByBotId"))

    assert received == []
    (line,) = _outcome_lines(caplog)
    assert _outcome_of(line) == "ignored:posted-by-this-bot"


@pytest.mark.asyncio
async def test_an_admitting_word_refuses_every_app_while_this_bot_is_unidentified(
    caplog,
) -> None:
    """auth.test can fail at startup, and ``all`` must not open a loop when it does.

    With neither the bot id nor the bot user id known there is nothing to
    recognise this connector's own messages by, so the floor cannot be applied
    and the ladder is refused wholesale rather than applied without it. Fails
    closed for the same reason ``_is_reply_to_bot`` does.
    """
    channel, received = _dm_channel(app_messages="all")
    channel._bot_user_id = ""
    channel._bot_id = ""
    with caplog.at_level(logging.INFO, logger=_LOGGER_NAME):
        await channel._handle_message_event(
            _app_dm_event("1710000028.000100", "posted through an app"),
            _body("EvNoBotIdentity"),
        )

    assert received == []
    (line,) = _outcome_lines(caplog)
    assert _outcome_of(line) == "ignored:app-post-with-this-bot-unidentified"


@pytest.mark.asyncio
async def test_the_default_word_leaves_an_app_post_exactly_as_it_was(caplog) -> None:
    """``none`` is what every deployment upgrades into, and it changes nothing.

    An app-posted message with an app_id on it is still reported under the name
    it has always had, and the line carries nothing new -- so a config that has
    not written this key reads the same before and after. A list left behind by
    an operator who moved back down to ``none`` admits nothing either.
    """
    channel, received = _dm_channel(app_messages_from=(_PERSONAS_APP,))
    channel._bot_user_id = _BOT_USER_ID
    assert channel.config.app_messages == "none"
    with caplog.at_level(logging.INFO, logger=_LOGGER_NAME):
        await channel._handle_message_event(
            _app_dm_event("1710000025.000100", "posted by an app"),
            _body("EvDefaultApp"),
        )

    assert received == []
    (line,) = _outcome_lines(caplog)
    assert _outcome_of(line) == "ignored:posted-by-an-app"
    assert "app_messages=" not in line


@pytest.mark.asyncio
async def test_an_unreadable_word_falls_to_the_narrow_end_of_the_ladder(
    caplog,
) -> None:
    """A config built by hand is one caller of many, and a typo must not widen.

    The gateway warns about a word it cannot read; what is pinned here is that
    the connector reading the field does not depend on having been warned.
    """
    channel, received = _dm_channel(app_messages="everything")
    channel._bot_user_id = _BOT_USER_ID
    with caplog.at_level(logging.INFO, logger=_LOGGER_NAME):
        await channel._handle_message_event(
            _app_dm_event("1710000029.000100", "posted by an app"),
            _body("EvBadWord"),
        )

    assert received == []
    (line,) = _outcome_lines(caplog)
    assert _outcome_of(line) == "ignored:posted-by-an-app"


@pytest.mark.asyncio
async def test_an_admitted_app_in_a_channel_is_noted_once_not_twice(caplog) -> None:
    """The predicate runs twice for a channel message; the note must not.

    Once in the router, before a trigger is looked for, and again in the dispatch
    that no third caller may reach unguarded. The note is written where the line
    is -- one per inbound event -- rather than where the decision is made.
    """
    channel, received = _channel_channel(app_messages="all")
    with caplog.at_level(logging.INFO, logger=_LOGGER_NAME):
        await channel._handle_message_event(
            _channel_event(
                "1710000026.000100",
                f"<@{_BOT_USER_ID}> posted through an app",
                bot_id=_PERSONAS_BOT,
                app_id=_PERSONAS_APP,
            ),
            _body("EvAdmittedAppChannel"),
        )

    assert received == []
    (line,) = _outcome_lines(caplog)
    assert line.count(f"app_id={_PERSONAS_APP}") == 1
    # A leading mention is app_mention's to handle, which is the ordinary
    # disposition here and not this key's business. What matters is that the
    # message reached a trigger decision at all.
    assert _outcome_of(line) == "ignored:leading-bot-mention-handled-as-app_mention"


def test_the_word_is_read_off_a_raw_config_block() -> None:
    """Three words, and everything else is the narrow end.

    Including the YAML 1.1 hazard the neighbouring ``history`` key documents: a
    bare ``no`` arrives here as ``False`` rather than as a word.
    """
    assert slack_connect.resolve_app_messages({}) == "none"
    assert slack_connect.resolve_app_messages(None) == "none"
    assert slack_connect.resolve_app_messages({"app_messages": None}) == "none"
    assert slack_connect.resolve_app_messages({"app_messages": " LISTED "}) == "listed"
    assert slack_connect.resolve_app_messages({"app_messages": "all"}) == "all"
    assert slack_connect.resolve_app_messages({"app_messages": False}) == "none"
    assert slack_connect.resolve_app_messages({"app_messages": "everything"}) == "none"


def test_the_list_is_read_off_a_raw_config_block() -> None:
    """Ids are cleaned, de-duplicated and kept in the order they were written.

    Absent and empty are the same answer, and under ``listed`` that answer
    admits nothing -- the only thing an allow-list's empty state can mean, and
    what stops a config upgrade from widening a deployment that never asked.
    """
    assert slack_connect.resolve_app_messages_from({}) == ()
    assert slack_connect.resolve_app_messages_from(None) == ()
    assert slack_connect.resolve_app_messages_from({"app_messages_from": []}) == ()
    assert slack_connect.resolve_app_messages_from(
        {"app_messages_from": [" A0ONE ", "A0TWO", "A0ONE", ""]}
    ) == ("A0ONE", "A0TWO")


@pytest.mark.asyncio
async def test_an_app_mention_event_is_still_guarded_before_dispatch(caplog) -> None:
    """app_mention does not pass through the message router's check.

    So _handle_slack_event keeps its own call to the shared predicate. Slack
    should never send this, which is the point: the gate before dispatch cannot
    depend on that.
    """
    channel, received = _channel_channel()
    with caplog.at_level(logging.INFO, logger=_LOGGER_NAME):
        await channel._handle_app_mention(
            {
                "type": "app_mention",
                "channel": _WATCHED_CHANNEL,
                "user": "U0SENDER01",
                "text": f"<@{_BOT_USER_ID}> hello",
                "ts": "1710000014.000100",
                "subtype": "message_changed",
            },
            _body("EvMentionEdit"),
        )

    assert received == []
    (line,) = _outcome_lines(caplog)
    assert _outcome_of(line) == "ignored:subtype-not-user-content (message_changed)"


# ---------------------------------------------------------------------------
# The two click listeners, which lose a *gesture* rather than a message.
#
# Registered the same way as the event listeners and swallowed the same way by
# bolt, but with one difference that makes the silence worse: both acknowledge
# the interaction before they can fail. The person is shown a control that
# reported success, and nothing happened -- a question still waiting with its
# answer nowhere, or a card whose stop button took the click and left the turn
# running.
# ---------------------------------------------------------------------------


class _Ack:
    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self, *_args, **_kwargs) -> None:
        self.calls += 1


def _click_body(action_id: str) -> tuple[dict, dict]:
    action = {"action_id": action_id, "value": "{}"}
    body = {
        "user": {"id": "U0CLICKER1"},
        "channel": {"id": "C0WATCHED1"},
        "container": {"type": "message", "message_ts": "1710000020.000100"},
        "actions": [action],
    }
    return body, action


@pytest.mark.asyncio
async def test_a_question_click_that_raises_is_logged_at_the_connector_edge(
    caplog,
) -> None:
    channel, _received = _dm_channel()

    def _boom(*_args, **_kwargs):
        raise RuntimeError("button fell off")

    channel._decode_button_value = _boom  # type: ignore[method-assign]
    ack = _Ack()
    body, action = _click_body("jiuwenswarm_answer:req-1:0")

    with caplog.at_level(logging.INFO, logger=_LOGGER_NAME):
        # Does not propagate, for the reason the event handlers do not: bolt
        # would swallow it, and one click that raised must not take the
        # listener down for the next one.
        await channel._handle_question_action(ack, body, action)

    assert ack.calls == 1
    raised = [record for record in caplog.records if record.levelno >= logging.ERROR]
    assert raised, "an exception inside the click handler must reach the log"
    # Enough to find the click in Slack, and nothing that was said in it.
    assert "action_id=jiuwenswarm_answer:req-1:0" in raised[0].getMessage()
    assert "user=U0CLICKER1" in raised[0].getMessage()
    assert "RuntimeError: button fell off" in caplog.text


@pytest.mark.asyncio
async def test_a_stop_click_that_raises_is_logged_at_the_connector_edge(
    caplog,
) -> None:
    channel, _received = _dm_channel()

    def _boom(*_args, **_kwargs):
        raise RuntimeError("card fell off")

    channel._decode_stop_value = _boom  # type: ignore[method-assign]
    ack = _Ack()
    body, action = _click_body(slack_connect._STOP_ACTION_ID)

    with caplog.at_level(logging.INFO, logger=_LOGGER_NAME):
        await channel._handle_stop_action(ack, body, action)

    assert ack.calls == 1
    raised = [record for record in caplog.records if record.levelno >= logging.ERROR]
    assert raised, "an exception inside the click handler must reach the log"
    assert "message_ts=1710000020.000100" in raised[0].getMessage()
    assert "RuntimeError: card fell off" in caplog.text


@pytest.mark.asyncio
async def test_a_click_that_does_not_raise_logs_nothing_extra(caplog) -> None:
    """The guard is a guard. A click that works reads exactly as it did."""
    channel, _received = _dm_channel()
    ack = _Ack()
    # No pending question for this id, which is a path that returns quietly.
    body, action = _click_body("jiuwenswarm_answer:nothing-waiting:0")

    with caplog.at_level(logging.INFO, logger=_LOGGER_NAME):
        await channel._handle_question_action(ack, body, action)

    assert ack.calls == 1
    assert [record for record in caplog.records if record.levelno >= logging.ERROR] == []
