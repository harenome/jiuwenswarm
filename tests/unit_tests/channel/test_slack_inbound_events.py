"""Non-message Slack events: acknowledged, and folded into the next turn."""

from __future__ import annotations

import pytest

from jiuwenswarm.common.scopes import compile_scopes
from jiuwenswarm.gateway.channel_manager.im_platforms.slack import slack_connect
from jiuwenswarm.gateway.channel_manager.im_platforms.slack.slack_connect import (
    SlackChannel,
    SlackChannelConfig,
    apply_scopes_to_slack_overrides,
)
from jiuwenswarm.gateway.channel_manager.base import RobotMessageRouter

_real_store_init = slack_connect.SlackEventDedupStore.__init__


@pytest.fixture(autouse=True)
def _isolated_dedup_store(tmp_path, monkeypatch):
    """Give every test its own dedup file.

    The store is durable by design, so without this the whole module shares one
    window -- two tests dispatching a message with the same ts would see each
    other's entries -- and a stray run would write into the real workspace.
    """
    monkeypatch.setattr(
        slack_connect.SlackEventDedupStore,
        "__init__",
        lambda self, path=None, **kw: _real_store_init(
            self, path or tmp_path / "slack_seen_events.json", **kw
        ),
    )


def _bolt_app(*, process_before_response: bool = False):
    """A real ``AsyncApp``, with every network-touching check switched off.

    The dispatcher is what these tests are about -- which listener claims an
    envelope, and what status the adapter is therefore handed -- so a fake app
    would test the fake. Nothing here talks to Slack: the listeners registered
    below never call the client.
    """
    pytest.importorskip("slack_bolt")
    from slack_bolt.async_app import AsyncApp
    from slack_bolt.authorization import AuthorizeResult

    async def _authorize(**kwargs):
        # Supplied so bolt does not reach for auth.test to learn who it is.
        # Passing a token instead installs SingleTeamAuthorization, which calls
        # Slack on the first dispatch.
        return AuthorizeResult(
            enterprise_id=None,
            team_id="T1",
            bot_id="B1",
            bot_user_id="U-BOT",
            bot_token="xoxb-test",
        )

    return AsyncApp(
        signing_secret="secret",
        process_before_response=process_before_response,
        authorize=_authorize,
        request_verification_enabled=False,
        ignoring_self_events_enabled=False,
        url_verification_enabled=False,
        ssl_check_enabled=False,
    )


def _envelope(event: dict) -> dict:
    return {
        "type": "event_callback",
        "team_id": "T1",
        "event_id": "Ev1",
        "event": event,
    }


async def _dispatch(app, body: dict):
    from slack_bolt.request.async_request import AsyncBoltRequest

    return await app.async_dispatch(AsyncBoltRequest(mode="socket_mode", body=body))


@pytest.mark.asyncio
async def test_catch_all_acknowledges_an_event_no_named_listener_takes() -> None:
    # The Socket Mode adapter acknowledges the envelope only on a 200. An event
    # nothing here reads must still be acknowledged: Slack counts an unacked
    # envelope as a failure and, past a threshold, disables Event Subscriptions
    # for the whole app, which takes message delivery with it.
    app = _bolt_app()
    channel = SlackChannel(SlackChannelConfig(enabled=True), RobotMessageRouter())
    app.event("message")(channel._handle_message_event)
    app.event(slack_connect._ANY_EVENT_TYPE)(channel._handle_unclaimed_event)

    resp = await _dispatch(app, _envelope({"type": "star_added", "user": "U1"}))

    assert resp.status == 200


@pytest.mark.asyncio
async def test_catch_all_registered_last_does_not_shadow_a_named_listener() -> None:
    # Bolt returns at the first listener that matches, so a catch-all in front
    # of a named handler would answer for it and the named one would never run.
    # ``process_before_response`` runs a listener inline instead of in a task,
    # so which one ran is readable the moment dispatch returns. It changes
    # nothing about the ordering under test: the walk that picks the listener is
    # in ``async_dispatch`` and is the same code either way.
    app = _bolt_app(process_before_response=True)
    claimed: list[str] = []

    async def named(event, body) -> None:
        claimed.append("named")

    async def catch_all(event, body) -> None:
        claimed.append("catch-all")

    app.event("reaction_added")(named)
    app.event(slack_connect._ANY_EVENT_TYPE)(catch_all)

    await _dispatch(app, _envelope({"type": "reaction_added", "user": "U1"}))

    assert claimed == ["named"]


# ----------------------------------------------------------------------
# Recording: what reaches the buffer, and what never does
# ----------------------------------------------------------------------


def _events_channel(
    policy: dict | None = None,
    *,
    chat_id: str = "C1",
    allowed: list[str] | None = None,
    allow_from: list[str] | None = None,
) -> SlackChannel:
    """A running channel whose conversation ``chat_id`` keeps ``policy``."""
    config = SlackChannelConfig(
        enabled=True,
        allow_from=allow_from or [],
        allowed_channel_ids=allowed if allowed is not None else [],
        conversation_overrides=(
            {chat_id: slack_connect.SlackChannelOverride(events=policy)}
            if policy is not None
            else {}
        ),
    )
    channel = SlackChannel(config, RobotMessageRouter())
    channel._running = True
    channel._bot_user_id = "U-BOT"
    return channel


def _reaction_event(user: str = "U1", emoji: str = "eyes", channel: str = "C1") -> dict:
    return {
        "type": "reaction_added",
        "user": user,
        "reaction": emoji,
        "item": {"type": "message", "channel": channel, "ts": "1710000000.000100"},
        "item_user": "U-BOT",
        "event_ts": "1710000100.000200",
    }


def _pin_event(user: str = "U1", channel: str = "C1") -> dict:
    return {
        "type": "pin_added",
        "user": user,
        "channel_id": channel,
        "item": {
            "type": "message",
            "channel": channel,
            "message": {"ts": "1710000000.000100"},
        },
        "event_ts": "1710000200.000300",
    }


def _member_event(user: str = "U1", channel: str = "C1") -> dict:
    return {
        "type": "member_joined_channel",
        "user": user,
        "channel": channel,
        "channel_type": "C",
        "team": "T1",
        "event_ts": "1710000300.000400",
    }


def _app_home_event(
    user: str = "U1", channel: str = "D1", tab: str = "messages"
) -> dict:
    """Somebody entering this app's App Home, as Slack sends it.

    ``channel`` is a ``D`` and cannot be anything else: an App Home belongs to
    one person, and what Slack names here is the direct message between that
    person and this app -- the conversation the Messages tab of that same App
    Home already is.

    ``view`` is carried, because the live payload carries it whenever a Home
    tab has been published and it is the field the record deliberately drops.
    """
    return {
        "type": "app_home_opened",
        "user": user,
        "channel": channel,
        "event_ts": "1710000400.000500",
        "tab": tab,
        "view": {"id": "V1", "type": "home", "blocks": [{"type": "divider"}]},
    }


_BODY = {"event_id": "Ev1", "team_id": "T1"}

#: Every family, the payload each arrives as, and the conversation it can
#: arrive from. The fourth carries its own chat id rather than borrowing the
#: channel the other three use, because Slack never sends an App Home opening
#: from a channel and a test that pretended otherwise would be testing a
#: payload that does not exist.
_FAMILY_CASES = (
    ("reaction", _reaction_event, "C1"),
    ("pin", _pin_event, "C1"),
    ("member", _member_event, "C1"),
    ("app_home", _app_home_event, "D1"),
)

_FAMILY_IDS = [family for family, _, _ in _FAMILY_CASES]


@pytest.mark.parametrize(("family", "build", "chat"), _FAMILY_CASES, ids=_FAMILY_IDS)
@pytest.mark.asyncio
async def test_each_family_buffers_under_context(family, build, chat) -> None:
    channel = _events_channel({family: "context"}, chat_id=chat)

    outcome = await channel._route_inbound_event(build(), _BODY)

    assert outcome == f"buffered:{family}"
    folded = channel._inbound_events.fold(chat)
    assert [record["family"] for record in folded["events"]] == [family]


@pytest.mark.parametrize(("family", "build", "chat"), _FAMILY_CASES, ids=_FAMILY_IDS)
@pytest.mark.asyncio
async def test_each_family_is_dropped_under_off(family, build, chat) -> None:
    channel = _events_channel({family: "off"}, chat_id=chat)

    outcome = await channel._route_inbound_event(build(), _BODY)

    assert outcome == f"ignored:events-off-for-{family}-here"
    assert channel._inbound_events.fold(chat) is None


@pytest.mark.parametrize(("family", "build", "chat"), _FAMILY_CASES, ids=_FAMILY_IDS)
@pytest.mark.asyncio
async def test_unset_behaves_exactly_as_off(family, build, chat) -> None:
    # The property that makes this free for every deployment that has not
    # written the key: no scope, no buffering, no change.
    channel = _events_channel(None, chat_id=chat)

    outcome = await channel._route_inbound_event(build(), _BODY)

    assert outcome == f"ignored:events-off-for-{family}-here"
    assert channel._inbound_events.fold(chat) is None


@pytest.mark.parametrize(
    ("build", "chat"),
    [(build, chat) for _, build, chat in _FAMILY_CASES],
    ids=_FAMILY_IDS,
)
@pytest.mark.asyncio
async def test_the_bots_own_action_is_filtered_out(build, chat) -> None:
    # This connector reacts to every inbound message with a lifecycle mark, so
    # without the self-filter the first thing a room with reaction: context
    # feeds the model is the bot's own eyes on the message that started the
    # turn -- inside that same turn.
    #
    # app_home is here for completeness rather than for a loop it has: nothing
    # this connector does opens an App Home, because App Home is opened by a
    # person in a Slack client and this app has no client. The filter is
    # applied to every family regardless, because a filter with an exception
    # list is a filter somebody has to keep correct.
    channel = _events_channel(
        {
            "reaction": "context",
            "pin": "context",
            "member": "context",
            "app_home": "context",
        },
        chat_id=chat,
    )

    outcome = await channel._route_inbound_event(build(user="U-BOT"), _BODY)

    assert outcome == "ignored:done-by-this-bot"
    assert channel._inbound_events.fold(chat) is None


@pytest.mark.asyncio
async def test_the_filter_reads_the_actor_and_not_the_author_of_the_item() -> None:
    # item_user is the bot: the reaction is somebody reacting to the bot's own
    # answer, which is exactly the event worth keeping. A filter written on that
    # field instead would drop this and keep the bot's own reactions.
    channel = _events_channel({"reaction": "context"})

    outcome = await channel._route_inbound_event(
        {**_reaction_event(user="U-HUMAN"), "item_user": "U-BOT"}, _BODY
    )

    assert outcome == "buffered:reaction"


@pytest.mark.asyncio
async def test_the_self_filter_runs_before_the_policy_is_read() -> None:
    # Ordering, pinned: the bot's own action is refused by name even in a room
    # that keeps nothing, so no gate added below the filter can move it down.
    channel = _events_channel(None)

    assert (
        await channel._route_inbound_event(_reaction_event(user="U-BOT"), _BODY)
        == "ignored:done-by-this-bot"
    )


@pytest.mark.asyncio
async def test_the_bot_user_id_is_learned_from_the_envelope() -> None:
    # A restarted channel that has not yet run auth.test still has to recognise
    # itself, or the very first events it sees are its own.
    channel = _events_channel({"reaction": "context"})
    channel._bot_user_id = ""

    outcome = await channel._route_inbound_event(
        _reaction_event(user="U-BOT"),
        {**_BODY, "authorizations": [{"user_id": "U-BOT", "is_bot": True}]},
    )

    assert outcome == "ignored:done-by-this-bot"


@pytest.mark.asyncio
async def test_an_app_attributed_event_is_filtered_even_under_another_id() -> None:
    channel = _events_channel({"reaction": "context"})

    outcome = await channel._route_inbound_event(
        {**_reaction_event(user="U-OTHER-APP"), "bot_id": "B9"}, _BODY
    )

    assert outcome == "ignored:done-by-an-app"


@pytest.mark.asyncio
async def test_a_conversation_the_operator_excluded_is_not_read() -> None:
    # allowed_channel_ids exists to keep this connector out of a room, and
    # reading one is exactly what it refuses. A platform-wide events rule must
    # not reach past it.
    config = SlackChannelConfig(
        enabled=True,
        allowed_channel_ids=["C-OPEN"],
        platform_override=slack_connect.SlackChannelOverride(
            events={"reaction": "context"}
        ),
    )
    channel = SlackChannel(config, RobotMessageRouter())
    channel._running = True
    channel._bot_user_id = "U-BOT"

    assert (
        await channel._route_inbound_event(_reaction_event(channel="C-CLOSED"), _BODY)
        == "ignored:channel-not-in-allowed_channel_ids"
    )
    assert (
        await channel._route_inbound_event(_reaction_event(channel="C-OPEN"), _BODY)
        == "buffered:reaction"
    )


@pytest.mark.asyncio
async def test_an_actor_outside_allow_from_is_refused_without_a_reply() -> None:
    channel = _events_channel({"reaction": "context"}, allow_from=["U-ALLOWED"])

    assert (
        await channel._route_inbound_event(_reaction_event(user="U-OTHER"), _BODY)
        == "refused:actor-not-in-allow_from"
    )
    assert (
        await channel._route_inbound_event(_reaction_event(user="U-ALLOWED"), _BODY)
        == "buffered:reaction"
    )


@pytest.mark.asyncio
async def test_a_stopped_channel_records_nothing() -> None:
    channel = _events_channel({"reaction": "context"})
    channel._running = False

    assert (
        await channel._route_inbound_event(_reaction_event(), _BODY)
        == "dropped:channel-not-running"
    )


@pytest.mark.asyncio
async def test_the_handler_writes_an_outcome_line_and_swallows_a_raise(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    channel = _events_channel({"reaction": "context"})
    lines: list[tuple] = []
    monkeypatch.setattr(
        channel, "_log_event_outcome", lambda *args: lines.append(args)
    )

    async def _boom(event, body):
        raise RuntimeError("no")

    monkeypatch.setattr(channel, "_route_inbound_event", _boom)
    await channel._handle_inbound_event(_reaction_event(), _BODY)

    assert lines and lines[0][-1] == "handler-raised"


# ----------------------------------------------------------------------
# Folding: what a turn started for its own reasons carries
# ----------------------------------------------------------------------


def _message_event(text: str = "ping", ts: str = "1710000500.000100") -> dict:
    return {
        "type": "message",
        "user": "U1",
        "channel": "C1",
        "channel_type": "channel",
        "text": text,
        "ts": ts,
    }


def _dispatching_channel(policy: dict | None = None) -> tuple[SlackChannel, list]:
    config = SlackChannelConfig(
        enabled=True,
        allowed_channel_ids=["C1"],
        group_chat_mode="all",
        conversation_overrides=(
            {"C1": slack_connect.SlackChannelOverride(events=policy)}
            if policy is not None
            else {}
        ),
    )
    channel = SlackChannel(config, RobotMessageRouter())
    channel._running = True
    channel._bot_user_id = "U-BOT"
    received: list = []
    channel.on_message(received.append)
    return channel, received


def _folded(content: str) -> dict:
    """The JSON object under the events label in a dispatched message."""
    import json

    marker = slack_connect._INBOUND_EVENTS_LABEL + "\n"
    assert marker in content, content
    body = content[content.index(marker) + len(marker) :]
    return json.loads(body.splitlines()[0])


@pytest.mark.asyncio
async def test_a_turn_carries_what_the_room_buffered_with_the_settled_names() -> None:
    channel, received = _dispatching_channel({"reaction": "context"})
    await channel._route_inbound_event(_reaction_event(user="U2"), _BODY)

    await channel._handle_message_event(_message_event(), _BODY)

    payload = _folded(received[0].params["content"])
    assert payload == {
        "chat_id": "C1",
        "events": [
            {
                "family": "reaction",
                "event_type": "reaction_added",
                "event_ts_iso_utc": "2024-03-09T16:01:40.000200Z",
                "author_user_id": "U2",
                "author_name": "U2",
                "emoji_name": "eyes",
                "ts": "1710000000.000100",
                "ts_iso_utc": "2024-03-09T16:00:00.000100Z",
            }
        ],
    }


@pytest.mark.asyncio
async def test_a_room_that_buffered_nothing_carries_nothing() -> None:
    # The text of a message in a conversation nobody wrote about must be
    # byte-for-byte what it was before this feature existed.
    channel, received = _dispatching_channel(None)

    await channel._handle_message_event(_message_event(), _BODY)

    assert received[0].params["content"] == "ping\n\n[ts: 1710000500.000100]"


@pytest.mark.asyncio
async def test_the_drop_count_reaches_the_turn() -> None:
    # A fold that quietly holds part of a burst is a lie about the room.
    channel, received = _dispatching_channel({"reaction": "context"})
    channel._inbound_events = slack_connect.InboundEventBuffer(
        limits={"reaction": 2, "pin": 2, "member": 2}
    )
    for index in range(5):
        await channel._route_inbound_event(
            _reaction_event(user=f"U{index}"), {**_BODY, "event_id": f"Ev{index}"}
        )

    await channel._handle_message_event(_message_event(), _BODY)

    payload = _folded(received[0].params["content"])
    assert len(payload["events"]) == 2
    assert payload["dropped"] == {"reaction": {"overflow": 3}}


@pytest.mark.asyncio
async def test_the_fold_is_taken_once_and_the_next_turn_starts_empty() -> None:
    channel, received = _dispatching_channel({"reaction": "context"})
    await channel._route_inbound_event(_reaction_event(user="U2"), _BODY)

    await channel._handle_message_event(_message_event(ts="1710000500.000100"), _BODY)
    await channel._handle_message_event(
        _message_event(ts="1710000600.000100"), {**_BODY, "event_id": "Ev2"}
    )

    assert slack_connect._INBOUND_EVENTS_LABEL in received[0].params["content"]
    assert slack_connect._INBOUND_EVENTS_LABEL not in received[1].params["content"]


@pytest.mark.asyncio
async def test_the_events_sit_above_the_standing_prompt() -> None:
    # Facts first, instructions after: a prompt written about reactions arrives
    # under the reactions it is about.
    config = SlackChannelConfig(
        enabled=True,
        allowed_channel_ids=["C1"],
        group_chat_mode="all",
        conversation_overrides={
            "C1": slack_connect.SlackChannelOverride(
                events={"reaction": "context"},
                prompt="Save anything bookmarked.",
            )
        },
    )
    channel = SlackChannel(config, RobotMessageRouter())
    channel._running = True
    channel._bot_user_id = "U-BOT"
    received: list = []
    channel.on_message(received.append)
    await channel._route_inbound_event(_reaction_event(user="U2"), _BODY)

    await channel._handle_message_event(_message_event(), _BODY)

    content = received[0].params["content"]
    assert content.index(slack_connect._INBOUND_EVENTS_LABEL) < content.index(
        "Save anything bookmarked."
    )


@pytest.mark.asyncio
async def test_a_turn_that_never_starts_leaves_the_buffer_alone() -> None:
    # An empty message produces no turn, so consuming the buffer for it would
    # lose the events with nothing anywhere to say so.
    channel, received = _dispatching_channel({"reaction": "context"})
    await channel._route_inbound_event(_reaction_event(user="U2"), _BODY)

    await channel._handle_message_event(_message_event(text=""), _BODY)

    assert received == []
    assert channel._inbound_events.fold("C1") is not None


@pytest.mark.asyncio
async def test_a_room_whose_licence_was_withdrawn_still_hands_over_what_it_has() -> None:
    channel, received = _dispatching_channel({"reaction": "context"})
    await channel._route_inbound_event(_reaction_event(user="U2"), _BODY)
    channel.config.conversation_overrides["C1"] = slack_connect.SlackChannelOverride(
        events={"reaction": "off"}
    )

    # Nothing new is collected from the moment the licence changes...
    assert (
        await channel._route_inbound_event(
            _reaction_event(user="U3"), {**_BODY, "event_id": "Ev2"}
        )
        == "ignored:events-off-for-reaction-here"
    )
    # ...and what was collected under the licence that stood is still delivered.
    await channel._handle_message_event(_message_event(), _BODY)
    assert len(_folded(received[0].params["content"])["events"]) == 1


@pytest.mark.asyncio
async def test_the_fold_reaches_the_model_through_the_user_turn_envelope() -> None:
    """The boundary, checked rather than argued.

    The fold rides on the request's ``content``, which is the user message
    itself -- the one field of a chat request nothing on the way to the runtime
    may drop. ``UserTurn.render`` is where that value becomes the JSON envelope
    the model actually reads, so rendering the dispatched content through it is
    the end of the path, and the only assertion that says the model sees this at
    all rather than that a connector wrote it down.

    Metadata is the route this deliberately does not take. The gateway-to-agent
    -server hop strips only four underscore-prefixed wire keys, so a metadata
    key would survive it -- but it would survive into the *tool* side: the
    envelope bridges exactly two metadata fields to the model (``chat_type`` and
    ``sender_name``, through ``_sender_fields``), so a fold written there would
    be invisible to the model and the feature would do nothing.
    """
    import json

    from jiuwenswarm.server.runtime.agent_adapter.user_turn import UserTurn

    channel, received = _dispatching_channel({"member": "context"})
    await channel._route_inbound_event(_member_event(user="U-NEW"), _BODY)
    await channel._handle_message_event(_message_event(), _BODY)

    rendered = UserTurn(
        text=received[0].params["content"],
        channel="slack",
        language="en",
        files={},
        metadata=dict(received[0].metadata or {}),
    ).render()

    envelope = json.loads(rendered[rendered.index("{") :])
    assert slack_connect._INBOUND_EVENTS_LABEL in envelope["content"]
    assert '"author_user_id": "U-NEW"' in envelope["content"]


# ----------------------------------------------------------------------
# turn: the event that starts a turn of its own
# ----------------------------------------------------------------------


def _turn_channel(
    policy: dict | None = None,
    *,
    reply: str = "optional",
    prompt: str | None = None,
    chat_id: str = "C1",
) -> tuple[SlackChannel, list]:
    """A conversation licensed to wake turns, and the requests it dispatches.

    ``reply: optional`` by default, because ``required`` -- which is what every
    conversation has until somebody writes otherwise -- downgrades ``turn`` to
    ``context``. A test wanting that downgrade asks for it by name.
    """
    config = SlackChannelConfig(
        enabled=True,
        allowed_channel_ids=[chat_id],
        conversation_overrides={
            chat_id: slack_connect.SlackChannelOverride(
                events=policy,
                reply=reply,
                prompt=prompt,
            )
        },
    )
    channel = SlackChannel(config, RobotMessageRouter())
    channel._running = True
    channel._bot_user_id = "U-BOT"
    received: list = []
    channel.on_message(received.append)
    return channel, received


def _woken(content: str) -> dict:
    """The JSON object under the waking-event label in a dispatched turn."""
    import json

    marker = slack_connect._EVENT_TURN_LABEL + "\n"
    assert marker in content, content
    body = content[content.index(marker) + len(marker) :]
    return json.loads(body.splitlines()[0])


@pytest.mark.asyncio
async def test_a_reaction_under_turn_dispatches_a_request() -> None:
    channel, received = _turn_channel({"reaction": "turn"})

    outcome = await channel._route_inbound_event(_reaction_event(user="U2"), _BODY)

    assert outcome.startswith("dispatched:event:reaction session=")
    assert len(received) == 1
    # And nothing is left in the buffer: the event went into the turn.
    assert channel._inbound_events.fold("C1") is None


@pytest.mark.asyncio
async def test_a_pin_under_turn_dispatches_a_request() -> None:
    channel, received = _turn_channel({"pin": "turn"})

    outcome = await channel._route_inbound_event(_pin_event(user="U2"), _BODY)

    assert outcome.startswith("dispatched:event:pin session=")
    assert len(received) == 1


@pytest.mark.asyncio
async def test_the_anchor_is_the_reply_destination() -> None:
    # The whole of how an event turn's answer finds its way back: the same two
    # metadata keys an inbound message stamps, read by the same second rung of
    # the delivery ladder. No rung was added for this and none is bypassed.
    channel, received = _turn_channel({"reaction": "turn"})

    await channel._route_inbound_event(_reaction_event(user="U2"), _BODY)

    metadata = received[0].metadata
    assert metadata["slack_channel_id"] == "C1"
    assert metadata["slack_thread_ts"] == "1710000000.000100"
    assert metadata["slack_message_ts"] == "1710000000.000100"
    assert SlackChannel.resolve_delivery(metadata=metadata) == (
        "C1",
        "1710000000.000100",
    )


@pytest.mark.asyncio
async def test_an_anchor_inside_a_thread_is_answered_in_that_thread() -> None:
    channel, received = _turn_channel({"pin": "turn"})
    event = _pin_event(user="U2")
    event["item"]["message"]["thread_ts"] = "1709000000.000001"

    await channel._route_inbound_event(event, _BODY)

    assert SlackChannel.resolve_delivery(metadata=received[0].metadata) == (
        "C1",
        "1709000000.000001",
    )


@pytest.mark.asyncio
async def test_the_session_is_the_ordinary_one_keyed_on_the_anchors_thread() -> None:
    # No new session form: the id is exactly what a message posted in that
    # thread would have produced, so an event turn and a reply in the same
    # thread share one conversation.
    channel, received = _turn_channel({"reaction": "turn"})

    await channel._route_inbound_event(_reaction_event(user="U2"), _BODY)

    assert received[0].session_id == SlackChannel._session_id(
        team_id="T1",
        channel_id="C1",
        user_id="U2",
        root_thread_ts="1710000000.000100",
        is_dm=False,
    )


@pytest.mark.asyncio
async def test_the_bots_own_reaction_never_wakes_a_turn() -> None:
    # This connector marks every inbound message with a reaction of its own, so
    # this is the loop that matters: without the self-filter the first thing a
    # room set to turn would do is answer its own acknowledgement, then answer
    # its answer. The filter runs above the policy read and cannot be reached
    # past.
    channel, received = _turn_channel(
        {"reaction": "turn", "pin": "turn", "member": "context"}
    )

    for build in (_reaction_event, _pin_event):
        assert (
            await channel._route_inbound_event(build(user="U-BOT"), _BODY)
            == "ignored:done-by-this-bot"
        )
    assert received == []
    assert channel._inbound_events.fold("C1") is None


@pytest.mark.asyncio
async def test_an_app_attributed_reaction_never_wakes_a_turn() -> None:
    channel, received = _turn_channel({"reaction": "turn"})

    assert (
        await channel._route_inbound_event(
            {**_reaction_event(user="U-OTHER-APP"), "bot_id": "B9"}, _BODY
        )
        == "ignored:done-by-an-app"
    )
    assert received == []


@pytest.mark.asyncio
async def test_an_event_arriving_mid_turn_is_folded_into_the_running_turn() -> None:
    # No stampede: an emoji storm produces one turn at most. The rest become
    # exactly what context would have made of them, which is what the turn
    # already running carries.
    channel, received = _turn_channel({"reaction": "turn"})

    first = await channel._route_inbound_event(_reaction_event(user="U2"), _BODY)
    second = await channel._route_inbound_event(
        _reaction_event(user="U3"), {**_BODY, "event_id": "Ev2"}
    )

    assert first.startswith("dispatched:event:reaction")
    assert second == "buffered:reaction (turn-downgraded:turn-in-flight)"
    assert len(received) == 1
    folded = channel._inbound_events.fold("C1")
    assert [record["author_user_id"] for record in folded["events"]] == ["U3"]


@pytest.mark.asyncio
async def test_a_required_reply_downgrades_the_turn_to_context() -> None:
    # The conservative reading of two keys written separately: a required reply
    # would oblige the turn to say something about a reaction nobody asked
    # about. Buffered instead, and the downgrade is named in the outcome.
    channel, received = _turn_channel({"reaction": "turn"}, reply="required")

    outcome = await channel._route_inbound_event(_reaction_event(user="U2"), _BODY)

    assert outcome == "buffered:reaction (turn-downgraded:reply-required)"
    assert received == []
    assert len(channel._inbound_events.fold("C1")["events"]) == 1


@pytest.mark.asyncio
async def test_a_conversation_that_wrote_no_reply_key_gets_the_downgrade() -> None:
    # required is the default, so this is what a room that switched turn on and
    # nothing else actually does. Stated as its own test because it is the
    # configuration somebody writes first.
    config = SlackChannelConfig(
        enabled=True,
        allowed_channel_ids=["C1"],
        conversation_overrides={
            "C1": slack_connect.SlackChannelOverride(events={"reaction": "turn"})
        },
    )
    channel = SlackChannel(config, RobotMessageRouter())
    channel._running = True
    channel._bot_user_id = "U-BOT"
    received: list = []
    channel.on_message(received.append)

    outcome = await channel._route_inbound_event(_reaction_event(user="U2"), _BODY)

    assert outcome == "buffered:reaction (turn-downgraded:reply-required)"
    assert received == []


@pytest.mark.asyncio
async def test_an_event_naming_no_message_is_buffered_rather_than_answered() -> None:
    # A reaction on a file: its family takes turn and this event still has no
    # thread to answer in. The channel root is not a destination this connector
    # invents, so the event lands where context would have left it.
    channel, received = _turn_channel({"reaction": "turn"})
    on_a_file = {
        "type": "reaction_added",
        "user": "U2",
        "reaction": "eyes",
        "item": {"type": "file", "channel": "C1", "file": "F1"},
        "event_ts": "1710000100.000200",
    }

    outcome = await channel._route_inbound_event(on_a_file, _BODY)

    assert outcome == "buffered:reaction (turn-downgraded:no-anchor)"
    assert received == []


@pytest.mark.asyncio
async def test_an_event_with_no_workspace_is_buffered_rather_than_answered() -> None:
    channel, received = _turn_channel({"reaction": "turn"})

    outcome = await channel._route_inbound_event(
        _reaction_event(user="U2"), {"event_id": "Ev1"}
    )

    assert outcome == "buffered:reaction (turn-downgraded:no-team)"
    assert received == []


@pytest.mark.asyncio
async def test_a_replayed_envelope_does_not_wake_a_second_turn() -> None:
    # A turn is what a replay would buy here, which is why this path pays for
    # the dedup store the buffering path declines.
    channel, received = _turn_channel({"reaction": "turn"})

    first = await channel._route_inbound_event(_reaction_event(user="U2"), _BODY)
    channel._turn_initiators.clear()
    second = await channel._route_inbound_event(_reaction_event(user="U2"), _BODY)

    assert first.startswith("dispatched:event:reaction")
    assert second == "ignored:already-handled (dedupe) family=reaction"
    assert len(received) == 1


@pytest.mark.asyncio
async def test_unset_is_byte_identical_to_today_for_every_family() -> None:
    # The promise to a deployment that never writes the key: no turn, no
    # buffer, no change, whichever of the seven event types arrives. A family
    # added later keeps that promise or breaks it here.
    for family, build, chat in _FAMILY_CASES:
        channel, received = _turn_channel(None, chat_id=chat)

        assert (
            await channel._route_inbound_event(build(user="U2"), _BODY)
            == f"ignored:events-off-for-{family}-here"
        )
        assert received == []
        assert channel._inbound_events.fold(chat) is None


# ----------------------------------------------------------------------
# What a turn woken by an event is told
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_turn_carries_the_events_own_facts_in_the_settled_names() -> None:
    channel, received = _turn_channel({"reaction": "turn"})

    await channel._route_inbound_event(_reaction_event(user="U2"), _BODY)

    assert _woken(received[0].params["content"]) == {
        "family": "reaction",
        "event_type": "reaction_added",
        "event_ts_iso_utc": "2024-03-09T16:01:40.000200Z",
        "author_user_id": "U2",
        "author_name": "U2",
        "emoji_name": "eyes",
        "ts": "1710000000.000100",
        "ts_iso_utc": "2024-03-09T16:00:00.000100Z",
    }


@pytest.mark.asyncio
async def test_an_event_turn_uses_the_actors_agent_scope() -> None:
    scopes = compile_scopes([
        {"match": {"channel": "slack", "chat": "C1"},
         "delivery": {"events": {"reaction": "turn"}, "reply": "optional"}},
        {"match": {"channel": "slack", "chat": "C1", "user": ["U2"]},
         "agent": {"subagents": ["research_agent"],
                   "skills": {"available": ["ambient"], "required": ["ambient"]}}},
    ], warn=lambda *args: None)
    platform, per_chat = apply_scopes_to_slack_overrides({}, scopes=scopes)
    channel = SlackChannel(SlackChannelConfig(
        enabled=True, allowed_channel_ids=["C1"], scopes=scopes,
        platform_override=platform, conversation_overrides=per_chat,
    ), RobotMessageRouter())
    channel._running = True
    channel._bot_user_id = "U-BOT"
    received: list = []
    channel.on_message(received.append)

    outcome = await channel._route_inbound_event(_reaction_event(user="U2"), _BODY)

    assert outcome.startswith("dispatched:event:reaction")
    assert received[0].params["agent_subagents_available"] == ["research_agent"]
    assert received[0].params["agent_skills_available"] == ["ambient"]
    assert received[0].params["agent_skills_required"] == ["ambient"]


@pytest.mark.asyncio
async def test_the_turn_is_labelled_with_the_trigger_that_woke_it() -> None:
    # The existing label mechanism, reused rather than duplicated: one bracket
    # vocabulary in front of the model.
    channel, received = _turn_channel({"reaction": "turn"})

    await channel._route_inbound_event(_reaction_event(user="U2"), _BODY)

    content = received[0].params["content"]
    assert "[trigger: event]" in content
    assert received[0].metadata["slack_trigger"] == "event"
    # And where the anchor is, in the same marker a message turn carries.
    assert "[ts: 1710000000.000100, thread_ts: 1710000000.000100]" in content


@pytest.mark.asyncio
async def test_the_turn_is_told_it_may_stay_silent_and_how() -> None:
    channel, received = _turn_channel({"reaction": "turn"})

    await channel._route_inbound_event(_reaction_event(user="U2"), _BODY)

    content = received[0].params["content"]
    assert slack_connect.EVENT_TURN_RANKING in content
    assert slack_connect.EVENT_TURN_DESTINATION_THREAD in content
    # The other half of the same contract: this one says silence is usually
    # right, that one says how to express it. Both, or a model with the correct
    # judgement writes a sentence describing its silence and the sentence is
    # posted.
    assert slack_connect.NO_REPLY_INSTRUCTION in content
    assert received[0].metadata[slack_connect.SLACK_REPLY_OPTIONAL_KEY] is True


@pytest.mark.asyncio
async def test_the_instruction_is_a_pointer_and_holds_no_operator_content() -> None:
    """The ranking is code's and the conventions are the operator's.

    Layer two states that silence is the floor and names what beats it: a
    convention this conversation has written down. It must name that without
    containing any of it, or the sentence stops being one text written once and
    becomes as many texts as there are rooms.
    """
    spelled = "Save anything bookmarked with a note."
    with_prompt, first = _turn_channel({"reaction": "turn"}, prompt=spelled)
    without_prompt, second = _turn_channel(
        {"reaction": "turn"}, prompt=None, chat_id="C2"
    )

    await with_prompt._route_inbound_event(_reaction_event(user="U2"), _BODY)
    await without_prompt._route_inbound_event(
        _reaction_event(user="U2", channel="C2"), {**_BODY, "event_id": "Ev2"}
    )

    # Identical text in both rooms, byte for byte.
    ranking = slack_connect.EVENT_TURN_RANKING
    assert ranking in first[0].params["content"]
    assert ranking in second[0].params["content"]
    assert spelled not in ranking
    # It points at the conversation's own instructions without quoting them.
    assert "conversation's own instructions" in ranking
    # The operator's text is still in the turn -- above the floor it beats.
    assert spelled in first[0].params["content"]
    assert spelled not in second[0].params["content"]


@pytest.mark.asyncio
async def test_the_instruction_promises_no_destination_but_the_reply() -> None:
    # There is no way for a model to post anywhere except this turn's own
    # reply, so no text handed to it may imply one -- in either destination.
    for anchored in (True, False):
        text = slack_connect.event_turn_instruction(anchored_on_a_message=anchored)
        assert slack_connect.EVENT_TURN_RANKING in text
        assert "the only place this turn can post anything" in text
        for forbidden in (
            "react",
            "pin",
            "post to",
            "another channel",
            "direct message",
        ):
            assert forbidden not in text.lower(), (anchored, forbidden)


@pytest.mark.asyncio
async def test_the_standing_prompt_sits_under_the_facts_and_the_ranking() -> None:
    # Facts, then the ranking they are ranked under, then the operator's
    # conventions: an instruction reads better with the facts it applies to
    # already above it.
    channel, received = _turn_channel(
        {"reaction": "turn"}, prompt="Save anything bookmarked."
    )

    await channel._route_inbound_event(_reaction_event(user="U2"), _BODY)

    content = received[0].params["content"]
    assert (
        content.index(slack_connect._EVENT_TURN_LABEL)
        < content.index(slack_connect.EVENT_TURN_RANKING)
        < content.index("Save anything bookmarked.")
        < content.index(slack_connect.NO_REPLY_INSTRUCTION)
    )


@pytest.mark.asyncio
async def test_a_woken_turn_also_carries_whatever_the_room_had_buffered() -> None:
    # Otherwise a room set to turn on one family and context on another would
    # accumulate a buffer nothing ever emptied.
    channel, received = _turn_channel({"reaction": "turn", "member": "context"})
    await channel._route_inbound_event(_member_event(user="U-NEW"), _BODY)

    await channel._route_inbound_event(
        _reaction_event(user="U2"), {**_BODY, "event_id": "Ev2"}
    )

    content = received[0].params["content"]
    assert slack_connect._INBOUND_EVENTS_LABEL in content
    assert '"author_user_id": "U-NEW"' in content
    assert channel._inbound_events.fold("C1") is None


@pytest.mark.asyncio
async def test_a_woken_turn_marks_nothing_on_anybodys_message() -> None:
    # An event turn acknowledges nothing, so its initiator entry carries no
    # coordinates for an ending to be written back to. A pair naming the anchor
    # would mark somebody's message as picked up when what was picked up was a
    # third party's reaction to it.
    channel, received = _turn_channel({"reaction": "turn"})

    await channel._route_inbound_event(_reaction_event(user="U2"), _BODY)

    initiator = channel.turn_initiator(received[0].session_id)
    assert initiator is not None
    assert initiator.user_id == "U2"
    assert (initiator.channel_id, initiator.message_ts, initiator.thread_ts) == (
        "",
        "",
        "",
    )


# ----------------------------------------------------------------------
# member: the family anchored on the room rather than on a message
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_membership_change_under_turn_dispatches_a_request() -> None:
    channel, received = _turn_channel({"member": "turn"})

    outcome = await channel._route_inbound_event(_member_event(user="U-NEW"), _BODY)

    assert outcome.startswith("dispatched:event:member session=")
    assert len(received) == 1
    assert channel._inbound_events.fold("C1") is None


@pytest.mark.asyncio
async def test_a_member_turn_answers_at_the_channels_top_level() -> None:
    # Not a fifth rung and not a new mechanism: post_as_root is the key this
    # connector already reads to put a cron result at channel top level, and
    # _extract_delivery applies it once over the whole ladder.
    channel, received = _turn_channel({"member": "turn"})

    await channel._route_inbound_event(_member_event(user="U-NEW"), _BODY)

    metadata = received[0].metadata
    assert metadata["slack_channel_id"] == "C1"
    assert metadata["slack_thread_ts"] == ""
    assert metadata[slack_connect.POST_AS_ROOT_KEY] is True
    assert SlackChannel.resolve_delivery(metadata=metadata) == ("C1", "")


@pytest.mark.asyncio
async def test_post_as_root_outranks_a_routing_target_carrying_a_thread() -> None:
    # The reason the key is written even though slack_thread_ts is already
    # empty: rung one is tried before metadata, so an empty thread on rung two
    # does not by itself keep a turn about the room out of somebody's thread.
    from jiuwenswarm.gateway.routing.keys import SlackDeliveryTarget
    from jiuwenswarm.gateway.routing.session_sharing import RoutingTarget

    channel, received = _turn_channel({"member": "turn"})
    await channel._route_inbound_event(_member_event(user="U-NEW"), _BODY)
    target = SlackDeliveryTarget(
        target_channel_id="C1", thread_ts="1710000000.000100"
    )

    # The ladder on its own puts the reply in that thread...
    assert SlackChannel.resolve_delivery(
        delivery=target, metadata=received[0].metadata
    ) == ("C1", "1710000000.000100")
    # ...and the ladder's one consumer of a Message applies the key over it.
    assert channel._extract_delivery(
        received[0], RoutingTarget(intent="mention", delivery=target)
    ) == ("C1", "")


@pytest.mark.asyncio
async def test_a_member_turn_fabricates_no_message_identifier() -> None:
    # A membership change is about a room. A ts standing in for the message it
    # does not have would be an identifier a model takes to a tool, and no tool
    # would accept it.
    channel, received = _turn_channel({"member": "turn"})

    await channel._route_inbound_event(_member_event(user="U-NEW"), _BODY)

    metadata = received[0].metadata
    assert "slack_message_ts" not in metadata
    assert "message_ts" not in metadata
    content = received[0].params["content"]
    assert "[ts:" not in content
    # The trigger label is still there: it is the one thing that says why a
    # turn exists at all, and it needs no ts.
    assert "[trigger: event]" in content


@pytest.mark.asyncio
async def test_a_member_turn_is_told_its_reply_goes_to_the_whole_channel() -> None:
    # Same two layers, and only the destination clause differs: the ranking is
    # one text for both because what beats silence does not depend on where the
    # answer lands.
    channel, received = _turn_channel({"member": "turn"})

    await channel._route_inbound_event(_member_event(user="U-NEW"), _BODY)

    content = received[0].params["content"]
    assert slack_connect.EVENT_TURN_RANKING in content
    assert slack_connect.EVENT_TURN_DESTINATION_ROOT in content
    assert slack_connect.EVENT_TURN_DESTINATION_THREAD not in content
    # And it says who would hear it, which a thread reply does not need to.
    assert "everyone in it sees it" in content


@pytest.mark.asyncio
async def test_a_member_turn_keeps_the_same_two_layers_in_the_same_order() -> None:
    # Facts from the event record, then the one ranking sentence pointing at
    # the conversation's own written instructions, then those instructions,
    # then the silence contract. Only the destination clause differs from a
    # reaction turn, and it rides on the end of the ranking rather than
    # becoming a third layer.
    channel, received = _turn_channel(
        {"member": "turn"}, prompt="Greet anyone new by name."
    )

    await channel._route_inbound_event(_member_event(user="U-NEW"), _BODY)

    content = received[0].params["content"]
    assert (
        content.index(slack_connect._EVENT_TURN_LABEL)
        < content.index(slack_connect.EVENT_TURN_RANKING)
        < content.index(slack_connect.EVENT_TURN_DESTINATION_ROOT)
        < content.index("Greet anyone new by name.")
        < content.index(slack_connect.NO_REPLY_INSTRUCTION)
    )


@pytest.mark.asyncio
async def test_a_member_turn_carries_the_events_own_facts() -> None:
    channel, received = _turn_channel({"member": "turn"})

    await channel._route_inbound_event(_member_event(user="U-NEW"), _BODY)

    assert _woken(received[0].params["content"]) == {
        "family": "member",
        "event_type": "member_joined_channel",
        "event_ts_iso_utc": "2024-03-09T16:05:00.000400Z",
        "author_user_id": "U-NEW",
        "author_name": "U-NEW",
    }


@pytest.mark.asyncio
async def test_a_member_turn_keys_on_the_room_rather_than_on_an_empty_thread() -> None:
    # session: thread has no thread to key on here, so the channel-wide form is
    # taken explicitly. Left to fall through, _session_id would have built an
    # id ending in a separator from an empty root -- shared by every membership
    # change in the room by accident rather than by decision.
    channel, received = _turn_channel({"member": "turn"})

    await channel._route_inbound_event(_member_event(user="U-NEW"), _BODY)

    assert received[0].session_id == "slack_T1_C1"
    assert received[0].session_id == SlackChannel._session_id(
        team_id="T1",
        channel_id="C1",
        user_id="U-NEW",
        root_thread_ts="",
        is_dm=False,
        channel_wide=True,
    )
    # Which is emphatically not what the fall-through would have produced.
    assert received[0].session_id != SlackChannel._session_id(
        team_id="T1",
        channel_id="C1",
        user_id="U-NEW",
        root_thread_ts="",
        is_dm=False,
        channel_wide=False,
    )


@pytest.mark.asyncio
async def test_a_bulk_invite_produces_one_turn_and_folds_the_rest() -> None:
    # Twenty people invited at once is the shape membership traffic actually
    # has, and it is the case the room-wide session id buys: every one of them
    # keys to the same session, so the in-flight fold catches the other
    # nineteen instead of starting nineteen more turns.
    channel, received = _turn_channel({"member": "turn"})
    outcomes = [
        await channel._route_inbound_event(
            _member_event(user=f"U-NEW{index}"), {**_BODY, "event_id": f"Ev{index}"}
        )
        for index in range(20)
    ]

    assert outcomes[0].startswith("dispatched:event:member")
    assert outcomes[1:] == [
        "buffered:member (turn-downgraded:turn-in-flight)"
    ] * 19
    assert len(received) == 1
    folded = channel._inbound_events.fold("C1")
    assert [record["author_user_id"] for record in folded["events"]] == [
        f"U-NEW{index}" for index in range(1, 20)
    ]


@pytest.mark.asyncio
async def test_the_bot_joining_a_channel_never_wakes_a_turn_about_itself() -> None:
    # The self-filter, on the family that would otherwise make it loudest: the
    # bot being added to a room would have it announcing its own arrival at the
    # top of that room.
    channel, received = _turn_channel({"member": "turn"})

    outcome = await channel._route_inbound_event(_member_event(user="U-BOT"), _BODY)

    assert outcome == "ignored:done-by-this-bot"
    assert received == []
    assert channel._inbound_events.fold("C1") is None


@pytest.mark.asyncio
async def test_a_departure_wakes_a_turn_just_as_an_arrival_does() -> None:
    # The family covers both directions and is not split, so switching member:
    # turn on switches it on for people leaving as well as joining. Pinned by a
    # test because it is the half an operator is least likely to have pictured.
    channel, received = _turn_channel({"member": "turn"})
    left = {**_member_event(user="U-GONE"), "type": "member_left_channel"}

    outcome = await channel._route_inbound_event(left, _BODY)

    assert outcome.startswith("dispatched:event:member")
    assert _woken(received[0].params["content"])["event_type"] == "member_left_channel"


@pytest.mark.asyncio
async def test_a_required_reply_downgrades_a_member_turn_too() -> None:
    channel, received = _turn_channel({"member": "turn"}, reply="required")

    outcome = await channel._route_inbound_event(_member_event(user="U-NEW"), _BODY)

    assert outcome == "buffered:member (turn-downgraded:reply-required)"
    assert received == []
    assert len(channel._inbound_events.fold("C1")["events"]) == 1


# ----------------------------------------------------------------------
# The kind of conversation an event came from
# ----------------------------------------------------------------------
#
# A reaction, a pin and an app_mention carry no ``channel_type``, so the kind
# cannot come off the payload. It comes off the conversation instead, and these
# pin where each of the three sources answers and where none does.


def _stated_message(
    *, channel: str = "C1", channel_type: str = "channel", user: str = "U2"
) -> dict:
    """An ordinary inbound message, which is the payload that states a kind."""
    return {
        "type": "message",
        "channel": channel,
        "channel_type": channel_type,
        "user": user,
        "text": "hello",
        "ts": "1710000000.000100",
    }


def test_a_conversation_id_answers_for_a_dm_and_for_nothing_else() -> None:
    # D names a one-to-one direct message and names nothing else. C and G are
    # shared between a public channel, a private one and a group DM, so neither
    # letter settles which -- and a wrong kind is worse than an absent one,
    # because it matches a rule written about somewhere else.
    assert slack_connect.chat_type_from_chat_id("D0BPP3XDX2A") == "im"
    assert slack_connect.chat_type_from_chat_id("C0BPP3XDX2A") == ""
    assert slack_connect.chat_type_from_chat_id("G0BPP3XDX2A") == ""
    assert slack_connect.chat_type_from_chat_id("") == ""


@pytest.mark.parametrize("kind", ["channel", "group", "mpim"])
def test_a_message_teaches_the_kind_and_a_reaction_reads_it(kind) -> None:
    channel = _events_channel({"reaction": "context"})

    channel._learn_chat_type(_stated_message(channel_type=kind))

    assert channel._conversation_chat_type("C1", _reaction_event()) == kind


def test_a_reaction_in_a_dm_is_an_im_with_nothing_learned() -> None:
    # No message needed: the id states this one outright, which is the reading
    # this connector already makes of a D prefix everywhere else.
    channel = _events_channel({"reaction": "context"}, chat_id="D1")

    resolved = channel._conversation_chat_type("D1", _reaction_event(channel="D1"))

    assert resolved == "im"


def test_an_unlearned_channel_stays_unknown_rather_than_being_guessed() -> None:
    # The whole of the safety argument: a rule naming a kind then matches
    # nothing and the conversation stays on the layer below, rather than a rule
    # written for public rooms firing inside a private one.
    channel = _events_channel({"reaction": "context"})

    assert channel._conversation_chat_type("C1", _reaction_event()) == ""
    assert channel._conversation_chat_type("G1") == ""


def test_slacks_one_letter_code_is_not_a_kind_and_teaches_nothing() -> None:
    # member_joined_channel puts "C" or "G" in channel_type, which is the older
    # one-letter code rather than one of the four words. Passing it on would
    # match no rule an operator can write and would reach request metadata,
    # where read_slack_conversation refuses any word it does not recognise.
    channel = _events_channel({"member": "context"})

    channel._learn_chat_type(_member_event())

    assert channel._chat_types == {}
    assert channel._conversation_chat_type("C1", _member_event()) == ""


def test_a_later_statement_replaces_an_earlier_one() -> None:
    # A public channel can be made private, and Slack's most recent word on it
    # is the better of the two.
    channel = _events_channel({"reaction": "context"})

    channel._learn_chat_type(_stated_message(channel_type="channel"))
    channel._learn_chat_type(_stated_message(channel_type="group"))

    assert channel._conversation_chat_type("C1") == "group"


@pytest.mark.asyncio
async def test_an_ignored_message_still_teaches_the_kind() -> None:
    # Learned at the top of the router, ahead of every gate below it. What a
    # channel_join notice says about the room is true whether or not the notice
    # itself is answered, and a room whose traffic is mostly ignored is exactly
    # where a cold map would bite.
    channel = _events_channel({"reaction": "turn"})
    joined = {
        **_stated_message(channel_type="group"),
        "subtype": "channel_join",
    }

    await channel._route_message_event(joined, _BODY)

    assert channel._conversation_chat_type("C1") == "group"


# ----------------------------------------------------------------------
# A scope keyed on the kind, and the event it has to reach
# ----------------------------------------------------------------------


def _scoped_turn_channel(entries: list, *, chat_id: str = "C1"):
    """A channel whose settings come from written scopes, as a deployment's do."""
    from jiuwenswarm.common.scopes import compile_scopes

    scopes = compile_scopes(entries, warn=lambda *a: None)
    platform, per_chat = slack_connect.apply_scopes_to_slack_overrides(
        {"group_chat_mode": "mention"}, scopes=scopes
    )
    config = SlackChannelConfig(
        enabled=True,
        group_chat_mode="mention",
        conversation_overrides=per_chat,
        platform_override=platform,
        scopes=scopes,
    )
    channel = SlackChannel(config, RobotMessageRouter())
    channel._running = True
    channel._bot_user_id = "U-BOT"
    received: list = []
    channel.on_message(received.append)
    return channel, received


#: The shape the live deployment writes, and the shape this whole section is
#: about: the licence to stay silent is stated once for every room of a kind,
#: and each conversation says only which of its events it wants turns for.
_LIVE_SHAPE = [
    {
        "match": {"channel": "slack", "chat_type": ["channel", "group"]},
        "delivery": {"reply": "optional"},
    },
    {
        "match": {"channel": "slack", "chat": "C1"},
        "delivery": {"events": {"reaction": "turn"}},
    },
]


@pytest.mark.asyncio
async def test_a_reaction_wakes_a_turn_under_a_reply_rule_keyed_on_the_kind() -> None:
    """The live case, end to end.

    A reaction in a public channel whose ``delivery.reply`` is licensed by a
    rule naming the *kind* of conversation rather than the conversation itself.

    Read off the payload, the kind was the empty string -- ``reaction_added``
    carries no ``channel_type`` -- so the rule did not fire, ``reply`` fell back
    to its default ``required``, and ``turn`` was downgraded to ``context``: the
    room's reactions were buffered and no turn ever started. Resolved from the
    conversation, the rule fires and the turn does.
    """
    channel, received = _scoped_turn_channel(_LIVE_SHAPE)
    # The room has ordinary traffic, which is where the kind comes from.
    await channel._route_message_event(_stated_message(), _BODY)

    outcome = await channel._route_inbound_event(_reaction_event(user="U2"), _BODY)

    assert outcome.startswith("dispatched:event:reaction session=")
    assert len(received) == 1
    assert channel._inbound_events.fold("C1") is None


@pytest.mark.asyncio
async def test_the_conversation_need_not_restate_the_reply_key() -> None:
    # What the fix makes redundant: before it, a conversation wanting event
    # turns had to carry a reply: optional of its own, because the rule naming
    # the kind could never reach an event. Inheriting it from the kind is the
    # natural way to write this, and is what the live deployment wrote.
    channel, _received = _scoped_turn_channel(_LIVE_SHAPE)
    await channel._route_message_event(_stated_message(), _BODY)

    assert channel.config.conversation_overrides["C1"].reply is None
    assert channel._channel_reply_is_optional("C1", "channel")


@pytest.mark.asyncio
async def test_a_kind_nobody_stated_leaves_the_rule_unfired() -> None:
    # The other half of the same property, and the one that keeps a guess out:
    # with no traffic to learn from, the kind is unknown, the rule matches
    # nothing and the event is buffered rather than answered under a licence
    # that was never established.
    channel, received = _scoped_turn_channel(_LIVE_SHAPE)

    outcome = await channel._route_inbound_event(_reaction_event(user="U2"), _BODY)

    assert outcome == "buffered:reaction (turn-downgraded:reply-required)"
    assert received == []
    assert len(channel._inbound_events.fold("C1")["events"]) == 1


@pytest.mark.asyncio
async def test_the_events_disposition_itself_can_be_keyed_on_the_kind() -> None:
    # delivery.events admits the chat_type axis, and this is the rule that
    # could not fire for want of a kind on the event. Nothing in it names C1.
    channel, received = _scoped_turn_channel(
        [
            {
                "match": {"channel": "slack", "chat_type": ["channel", "group"]},
                "delivery": {"reply": "optional", "events": {"reaction": "turn"}},
            }
        ]
    )
    await channel._route_message_event(_stated_message(channel_type="group"), _BODY)

    outcome = await channel._route_inbound_event(_reaction_event(user="U2"), _BODY)

    assert outcome.startswith("dispatched:event:reaction session=")


@pytest.mark.asyncio
async def test_a_rule_naming_another_kind_still_does_not_fire() -> None:
    # The kind is resolved, not assumed: a private channel is a group, and a
    # rule written for one-to-one direct messages has nothing to say about it.
    channel, received = _scoped_turn_channel(
        [
            {
                "match": {"channel": "slack", "chat_type": "im"},
                "delivery": {"reply": "optional"},
            },
            {
                "match": {"channel": "slack", "chat": "C1"},
                "delivery": {"events": {"reaction": "turn"}},
            },
        ]
    )
    await channel._route_message_event(_stated_message(channel_type="group"), _BODY)

    outcome = await channel._route_inbound_event(_reaction_event(user="U2"), _BODY)

    assert outcome == "buffered:reaction (turn-downgraded:reply-required)"
    assert received == []


@pytest.mark.asyncio
async def test_context_still_buffers_whatever_the_kind_resolves_to() -> None:
    # The disposition that shipped first, unchanged in both directions: a room
    # asking for context buffers its reactions whether or not the kind is known,
    # and buffering was never gated on delivery.reply.
    known, _ = _scoped_turn_channel(
        [
            {
                "match": {"channel": "slack", "chat_type": ["channel", "group"]},
                "delivery": {"reply": "optional"},
            },
            {
                "match": {"channel": "slack", "chat": "C1"},
                "delivery": {"events": {"reaction": "context"}},
            },
        ]
    )
    await known._route_message_event(_stated_message(), _BODY)
    unknown, _ = _scoped_turn_channel(
        [
            {
                "match": {"channel": "slack", "chat": "C1"},
                "delivery": {"events": {"reaction": "context"}},
            }
        ]
    )

    assert await known._route_inbound_event(_reaction_event(), _BODY) == (
        "buffered:reaction"
    )
    assert await unknown._route_inbound_event(_reaction_event(), _BODY) == (
        "buffered:reaction"
    )


# ----------------------------------------------------------------------
# What the payloads do not carry, and what read them as though they did
# ----------------------------------------------------------------------


def test_the_boundary_line_names_the_conversation_of_every_family() -> None:
    # One line is written for every inbound event, and it is what makes the
    # silent paths audible. A reaction names its conversation inside item and a
    # pin names it as channel_id, so reading `channel` alone reported `-` for
    # both -- the log could not say which room the event was in.
    body = {"event_id": "Ev1", "team_id": "T1"}

    assert "channel=C1" in SlackChannel._event_identity(_reaction_event(), body)
    assert "channel=C1" in SlackChannel._event_identity(_pin_event(), body)
    assert "channel=C1" in SlackChannel._event_identity(_member_event(), body)


def test_the_boundary_line_names_the_message_a_reaction_is_about() -> None:
    # A reaction has no top-level ts. The identifier worth logging is the one
    # that addresses a message, which is the anchor the routing already reads.
    body = {"event_id": "Ev1", "team_id": "T1"}

    assert "ts=1710000000.000100" in SlackChannel._event_identity(
        _reaction_event(), body
    )
    assert "ts=1710000000.000100" in SlackChannel._event_identity(_pin_event(), body)
    # A membership change is about a room rather than about a message, so there
    # is no ts and none is invented.
    assert "ts=-" in SlackChannel._event_identity(_member_event(), body)


def test_the_boundary_line_reports_the_kind_as_the_payload_stated_it() -> None:
    # A line about what arrived. Slack puts no channel_type on a reaction, and
    # reporting a kind resolved from somewhere else would have the log say Slack
    # sent a field it did not.
    body = {"event_id": "Ev1", "team_id": "T1"}

    assert "channel_type=-" in SlackChannel._event_identity(_reaction_event(), body)
    assert "subtype=-" in SlackChannel._event_identity(_reaction_event(), body)


@pytest.mark.asyncio
async def test_a_member_turn_is_not_stamped_with_slacks_one_letter_code() -> None:
    # member_joined_channel puts "C" in channel_type. Stamped onto the request
    # as slack_channel_type it reached read_slack_conversation, which refuses an
    # origin type it does not recognise -- so the history tool was unusable
    # inside exactly the turns that most needed to look at the room.
    from jiuwenswarm.agents.harness.common.tools import slack_history

    channel, received = _turn_channel({"member": "turn"})
    await channel._route_message_event(_stated_message(), _BODY)

    await channel._route_inbound_event(_member_event(user="U-NEW"), _BODY)

    stamped = received[0].metadata["slack_channel_type"]
    assert stamped == "channel"
    assert stamped in slack_history._ALLOWED_CHANNEL_TYPES


@pytest.mark.asyncio
async def test_a_mention_is_matched_on_the_kind_of_its_conversation() -> None:
    # Slack sends no channel_type on an app_mention either, so a rule naming a
    # kind never fired for the busiest trigger this connector has.
    channel, _received = _scoped_turn_channel(
        [
            {
                "match": {"channel": "slack", "chat_type": ["channel", "group"]},
                "delivery": {"prompt_append": "be terse"},
            }
        ]
    )
    mention = {
        "type": "app_mention",
        "channel": "C1",
        "user": "U2",
        "text": "<@U-BOT> ping",
        "ts": "1710000700.000100",
    }
    assert "channel_type" not in mention

    assert channel._conversation_chat_type("C1", mention) == ""
    await channel._route_message_event(_stated_message(), _BODY)
    assert channel._conversation_chat_type("C1", mention) == "channel"
    assert channel._channel_prompt("C1", "U2", "channel") == "be terse"
