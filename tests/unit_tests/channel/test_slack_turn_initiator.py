"""Who started the turn a Slack session is running.

The connector dispatches a turn and then loses the person: the reply comes back
holding the session and the channel and no user at all, and nothing downstream
brings one back either -- a ``chat.send`` reaching the gateway cancels whatever
stream that session already had without reference to who started it. So a stop
arriving from Slack has nothing to check itself against.

These tests hold the substrate that closes that: an entry opened at dispatch,
attributed to the person who asked, closed by that turn's terminal event, and
bounded so that a turn whose terminal event never arrives cannot pin one
forever. Nothing here authorizes anything -- the check belongs with the
permissions work -- but every one of these is a fact that check will rest on.
"""

from __future__ import annotations

import json
import time
from typing import Any

import pytest

from jiuwenswarm.common.schema.message import EventType, Message, ReqMethod
from jiuwenswarm.common.scopes import MID_TURN_CANCEL
from jiuwenswarm.gateway.channel_manager.base import RobotMessageRouter
from jiuwenswarm.gateway.channel_manager.im_platforms.slack import slack_connect
from jiuwenswarm.gateway.channel_manager.im_platforms.slack.slack_connect import (
    SlackChannel,
    SlackChannelConfig,
)

ASKER = "U0ASKER001"
BYSTANDER = "U0OTHER002"
TEAM = "T0TESTTEAM"
DM = "D0DIRECT01"
ROOM = "C0CHANNEL1"
DM_SESSION = f"slack_{TEAM}_{DM}_{ASKER}"
THREAD = "1710000001.000100"
ROOM_SESSION = f"slack_{TEAM}_{ROOM}_{THREAD}"


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


class _RecordingSlackClient:
    def __init__(self) -> None:
        self.posts: list[dict[str, Any]] = []
        self.updates: list[dict[str, Any]] = []

    async def chat_postMessage(self, **kwargs: Any) -> dict[str, str]:
        self.posts.append(kwargs)
        return {"ts": f"1710000099.{len(self.posts):06d}"}

    async def chat_update(self, **kwargs: Any) -> dict[str, str]:
        self.updates.append(kwargs)
        return {"ts": kwargs.get("ts", "")}

    # Not this file's subject, but reached by every path here: an ending writes
    # its outcome onto the message that started the turn. Answered rather than
    # left missing so the connector is not swallowing an AttributeError and
    # logging a warning on each of these tests. What the marks say is pinned in
    # test_slack_turn_marks.py.
    async def reactions_add(self, **kwargs: Any) -> dict[str, bool]:
        return {"ok": True}

    async def reactions_remove(self, **kwargs: Any) -> dict[str, bool]:
        return {"ok": True}


async def _noop(*_args: Any, **_kwargs: Any) -> None:
    return None


def _channel(
    *, mid_turn: str = MID_TURN_CANCEL, **overrides: Any
) -> tuple[SlackChannel, list[Message]]:
    overrides.setdefault("allow_from", [ASKER, BYSTANDER])
    channel = SlackChannel(
        SlackChannelConfig(enabled=True, **overrides), RobotMessageRouter()
    )
    # Asked for, not inherited. The entry this file is about is opened at
    # dispatch and overwritten by the next dispatch, so every test here where a
    # second message takes over a session needs that message to supersede the
    # running turn rather than wait behind it, and cancel stopped being what an
    # unconfigured conversation does when the default became queue. Patched at
    # the seam rather than written as a scope, so these tests keep testing the
    # initiator bookkeeping and not the settling cascade test_slack_scopes
    # covers.
    channel._mid_turn_mode = lambda *_a, **_kw: mid_turn  # type: ignore[assignment]
    channel._running = True
    channel._client = _RecordingSlackClient()
    channel._acknowledge_request = _noop  # type: ignore[method-assign]
    received: list[Message] = []
    channel.on_message(received.append)
    return channel, received


def _dm_event(*, user: str = ASKER, ts: str, text: str = "sweep the repos") -> dict:
    return {
        "type": "message",
        "channel_type": "im",
        "channel": DM,
        "user": user,
        "text": text,
        "ts": ts,
    }


def _room_event(*, user: str, ts: str, thread_ts: str) -> dict:
    return {
        "type": "message",
        "channel_type": "channel",
        "channel": ROOM,
        "user": user,
        "text": "keep going",
        "ts": ts,
        "thread_ts": thread_ts,
    }


def _body(event_id: str) -> dict:
    return {"event_id": event_id, "team_id": TEAM}


def _terminal(
    *,
    request_id: str,
    session_id: str = DM_SESSION,
    event_type: EventType = EventType.CHAT_FINAL,
    channel_id: str = DM,
) -> Message:
    return Message(
        id=request_id,
        type="event",
        channel_id="slack",
        session_id=session_id,
        params={},
        timestamp=time.time(),
        ok=True,
        payload={"event_type": event_type.value, "content": "done"},
        event_type=event_type,
        metadata={"slack_channel_id": channel_id},
    )


def _question(
    *,
    turn_request_id: str,
    session_id: str = DM_SESSION,
    request_id: str = "call_abc123",
    channel_id: str = DM,
) -> Message:
    """A ``permission_interrupt`` question, as the rail emits it."""
    return Message(
        id=turn_request_id,
        type="event",
        channel_id="slack",
        session_id=session_id,
        params={},
        timestamp=time.time(),
        ok=True,
        payload={
            "event_type": "chat.ask_user_question",
            "request_id": request_id,
            "source": "permission_interrupt",
            "questions": [
                {
                    "question": "Tool `bash` needs permission to run",
                    "header": "Permission",
                    "options": [
                        {"label": "Approve", "value": "approve"},
                        {"label": "Reject", "value": "reject"},
                    ],
                    "multi_select": False,
                }
            ],
        },
        event_type=EventType.CHAT_ASK_USER_QUESTION,
        metadata={"slack_channel_id": channel_id, "slack_thread_ts": ""},
    )


def _click(
    *,
    user_id: str,
    session_id: str = DM_SESSION,
    request_id: str = "call_abc123",
    channel_id: str = DM,
) -> tuple[dict[str, Any], dict[str, Any]]:
    encoded = json.dumps(
        {
            "request_id": request_id,
            "index": 0,
            "value": "approve",
            "source": "permission_interrupt",
            "session_id": session_id,
        },
        separators=(",", ":"),
    )
    action = {
        "type": "button",
        "action_id": "jiuwenswarm_answer:0",
        "value": encoded,
        "text": {"type": "plain_text", "text": "Approve"},
    }
    body = {
        "type": "block_actions",
        "user": {"id": user_id},
        "team": {"id": TEAM},
        "channel": {"id": channel_id},
        "container": {
            "type": "message",
            "channel_id": channel_id,
            "message_ts": "1710000099.000001",
        },
        "message": {"ts": "1710000099.000001", "blocks": []},
        "actions": [action],
    }
    return body, action


class _Ack:
    async def __call__(self, *_args: Any, **_kwargs: Any) -> None:
        return None


@pytest.mark.asyncio
async def test_a_dispatched_turn_records_who_asked_for_it() -> None:
    channel, received = _channel()

    await channel._handle_slack_event(
        _dm_event(ts="1710000001.000100"),
        _body("Ev01"),
        is_dm=True,
        trigger="dm",
    )

    (req,) = received
    initiator = channel.turn_initiator(req.session_id)
    assert initiator is not None
    assert initiator.user_id == ASKER
    # The turn's own id, so a stop naming a request can tell the turn it means
    # from the one that replaced it.
    assert initiator.request_id == req.id


@pytest.mark.asyncio
async def test_a_session_with_no_turn_running_has_no_initiator() -> None:
    channel, _received = _channel()
    assert channel.turn_initiator(DM_SESSION) is None
    # Not merely absent from the map: an empty session id is not a lookup.
    assert channel.turn_initiator("") is None


@pytest.mark.asyncio
async def test_the_turns_terminal_event_closes_the_entry() -> None:
    channel, received = _channel()
    await channel._handle_slack_event(
        _dm_event(ts="1710000001.000100"),
        _body("Ev01"),
        is_dm=True,
        trigger="dm",
    )
    (req,) = received

    await channel.send(_terminal(request_id=req.id, session_id=req.session_id))

    assert channel.turn_initiator(req.session_id) is None


@pytest.mark.asyncio
async def test_a_turn_that_died_closes_the_entry_too() -> None:
    channel, received = _channel()
    await channel._handle_slack_event(
        _dm_event(ts="1710000001.000100"),
        _body("Ev01"),
        is_dm=True,
        trigger="dm",
    )
    (req,) = received

    await channel.send(
        _terminal(
            request_id=req.id,
            session_id=req.session_id,
            event_type=EventType.CHAT_ERROR,
        )
    )

    assert channel.turn_initiator(req.session_id) is None


@pytest.mark.asyncio
async def test_the_entry_does_not_depend_on_the_activity_card_being_on() -> None:
    """The card is a display setting; whether a turn is running is not.

    The obvious home for this fact was ``_SlackActivityRecord``, which is the
    connector's only genuinely per-turn structure. It is the wrong one: every
    path into it is gated on ``activity_card``, so an operator who turned the
    card off would be turning off the record of who may stop a turn.
    """
    channel, received = _channel(activity_card=False)
    await channel._handle_slack_event(
        _dm_event(ts="1710000001.000100"),
        _body("Ev01"),
        is_dm=True,
        trigger="dm",
    )
    (req,) = received

    initiator = channel.turn_initiator(req.session_id)
    assert initiator is not None and initiator.user_id == ASKER
    assert channel._activity_records == {}

    await channel.send(_terminal(request_id=req.id, session_id=req.session_id))
    assert channel.turn_initiator(req.session_id) is None


@pytest.mark.asyncio
async def test_a_second_persons_message_takes_over_the_thread() -> None:
    """Because the gateway hands them the turn: a send cancels the live stream.

    Leaving the first person on record would name someone whose work has
    already been stopped, and the turn now running would have no initiator at
    all.
    """
    channel, received = _channel()
    thread = "1710000001.000100"
    for user, ts, event_id in (
        (ASKER, "1710000001.000100", "Ev01"),
        (BYSTANDER, "1710000002.000100", "Ev02"),
    ):
        await channel._handle_slack_event(
            _room_event(user=user, ts=ts, thread_ts=thread),
            _body(event_id),
            is_dm=False,
            trigger="mention",
        )

    first, second = received
    assert first.session_id == second.session_id
    initiator = channel.turn_initiator(second.session_id)
    assert initiator is not None
    assert initiator.user_id == BYSTANDER
    assert initiator.request_id == second.id


@pytest.mark.asyncio
async def test_the_displaced_turns_terminal_event_leaves_the_live_entry_alone() -> None:
    """The cancelled turn ends after the one that replaced it started.

    Dropping the entry by session alone would clear the live turn's initiator on
    the strength of a dead turn's final, which is exactly the window a stop
    would land in.
    """
    channel, received = _channel()
    thread = "1710000001.000100"
    for user, ts, event_id in (
        (ASKER, "1710000001.000100", "Ev01"),
        (BYSTANDER, "1710000002.000100", "Ev02"),
    ):
        await channel._handle_slack_event(
            _room_event(user=user, ts=ts, thread_ts=thread),
            _body(event_id),
            is_dm=False,
            trigger="mention",
        )
    first, second = received

    await channel.send(
        _terminal(
            request_id=first.id,
            session_id=first.session_id,
            event_type=EventType.CHAT_ERROR,
            channel_id=ROOM,
        )
    )

    initiator = channel.turn_initiator(second.session_id)
    assert initiator is not None
    assert initiator.user_id == BYSTANDER


@pytest.mark.asyncio
async def test_a_terminal_event_with_no_request_id_closes_nothing() -> None:
    """It cannot be attributed, so it is not evidence about any turn."""
    channel, received = _channel()
    await channel._handle_slack_event(
        _dm_event(ts="1710000001.000100"),
        _body("Ev01"),
        is_dm=True,
        trigger="dm",
    )
    (req,) = received

    await channel.send(_terminal(request_id="", session_id=req.session_id))

    assert channel.turn_initiator(req.session_id) is not None


@pytest.mark.asyncio
async def test_answering_an_interrupt_keeps_the_turn_with_the_person_who_asked() -> None:
    """A permission prompt is routinely cleared by someone other than the asker.

    The pause ends the turn on the wire, so the session's entry is already
    closed by the time the button is pressed; the resumed turn is attributed
    through the question, the way its status card already is.
    """
    channel, received = _channel()
    await channel._handle_slack_event(
        _dm_event(ts="1710000001.000100"),
        _body("Ev01"),
        is_dm=True,
        trigger="dm",
    )
    (opening,) = received
    received.clear()

    await channel.send(_question(turn_request_id=opening.id))
    await channel.send(_terminal(request_id=opening.id))
    assert channel.turn_initiator(DM_SESSION) is None

    body, action = _click(user_id=BYSTANDER)
    await channel._handle_question_action(_Ack(), body, action)

    (resume,) = received
    assert resume.req_method == ReqMethod.CHAT_SEND
    initiator = channel.turn_initiator(DM_SESSION)
    assert initiator is not None
    assert initiator.user_id == ASKER
    assert initiator.request_id == resume.id


@pytest.mark.asyncio
async def test_an_unattributable_resume_falls_back_to_whoever_answered() -> None:
    """Better a running turn someone can stop than one nobody can.

    Reached when the question outlived the entry that named its asker -- a
    gateway restart, an aged entry, a question posted by a path that never
    opened one.
    """
    channel, received = _channel()

    await channel.send(_question(turn_request_id="req-orphan"))
    body, action = _click(user_id=BYSTANDER)
    await channel._handle_question_action(_Ack(), body, action)

    (resume,) = received
    initiator = channel.turn_initiator(DM_SESSION)
    assert initiator is not None
    assert initiator.user_id == BYSTANDER
    assert initiator.request_id == resume.id


@pytest.mark.asyncio
async def test_an_entry_older_than_any_turn_is_not_reported_as_live() -> None:
    channel, _received = _channel()
    channel._remember_turn_initiator(DM_SESSION, ASKER, "req-1", is_dm=True, chat_type="im")
    entry = channel._turn_initiators[DM_SESSION]
    entry.started_at -= slack_connect._TURN_INITIATOR_TIMEOUT_SECONDS + 1.0

    assert channel.turn_initiator(DM_SESSION) is None
    # Read once and gone: the stale entry is not left to be reported again.
    assert DM_SESSION not in channel._turn_initiators


@pytest.mark.asyncio
async def test_the_map_is_capped_however_many_sessions_are_open() -> None:
    channel, _received = _channel()
    for index in range(slack_connect._MAX_TURN_INITIATORS + 40):
        channel._remember_turn_initiator(
            f"session-{index}", ASKER, f"req-{index}", is_dm=True, chat_type="im"
        )

    assert len(channel._turn_initiators) <= slack_connect._MAX_TURN_INITIATORS
    # The oldest go first, so the most recently started turns are the ones kept.
    assert f"session-{slack_connect._MAX_TURN_INITIATORS + 39}" in (
        channel._turn_initiators
    )
    assert "session-0" not in channel._turn_initiators


@pytest.mark.asyncio
async def test_a_stopped_channel_holds_no_turns(monkeypatch) -> None:
    """It can deliver nothing, so it is tracking nothing."""
    channel, _received = _channel()
    channel._remember_turn_initiator(DM_SESSION, ASKER, "req-1", is_dm=True, chat_type="im")

    await channel.stop()

    assert channel._turn_initiators == {}


@pytest.mark.asyncio
async def test_a_dm_turn_records_that_it_came_from_a_dm() -> None:
    """The regime the entry is nearly redundant in, and it says so.

    A DM session is keyed on the user, so the person on the entry cannot change
    for as long as the session exists.
    """
    channel, received = _channel()

    await channel._handle_slack_event(
        _dm_event(ts="1710000001.000100"),
        _body("Ev01"),
        is_dm=True,
        trigger="dm",
    )

    (req,) = received
    initiator = channel.turn_initiator(req.session_id)
    assert initiator is not None
    assert initiator.is_dm is True
    # The surface the field names is the one that picked the session-id shape.
    assert req.session_id.endswith(ASKER)


@pytest.mark.asyncio
async def test_a_thread_turn_records_that_it_came_from_a_channel() -> None:
    """The regime the entry holds real information in.

    The session is keyed on the root thread and shared, so a second person's
    ordinary message takes the turn over -- which is what a consumer has to
    know before it writes a rule about who may stop one.
    """
    channel, received = _channel()

    await channel._handle_slack_event(
        _room_event(user=ASKER, ts=THREAD, thread_ts=THREAD),
        _body("Ev01"),
        is_dm=False,
        trigger="mention",
    )

    (req,) = received
    assert req.session_id == ROOM_SESSION
    initiator = channel.turn_initiator(req.session_id)
    assert initiator is not None
    assert initiator.is_dm is False


@pytest.mark.asyncio
async def test_a_group_dm_is_routed_as_a_channel_and_not_as_a_dm() -> None:
    """An mpim is the shared regime, which is why the surface is a bool.

    Slack sends a group DM as channel_type "mpim". Only ``im`` takes the DM
    route, so an mpim is routed as channel traffic and keyed on the root thread
    like any other -- two people in one group DM share a session rather than
    getting one each. Were it the other way round it would be a third regime
    and a bool would flatten it; it is not, so two values say everything there
    is to say. The outcome below is the channel route's own answer -- it looked
    for a trigger in the text, which is a thing the DM route never does -- and
    it is reached only after the ``im`` branch declined the event.
    """
    channel, received = _channel()

    outcome = await channel._route_message_event(
        {
            "type": "message",
            "channel_type": "mpim",
            "channel": ROOM,
            "user": ASKER,
            "text": "keep going",
            "ts": THREAD,
        },
        _body("Ev01"),
    )

    assert outcome == "ignored:no-trigger-matched"
    assert received == []


@pytest.mark.asyncio
async def test_a_resumed_dm_turn_keeps_the_surface_it_started_on() -> None:
    """Kept through the question, not re-decided at the click.

    The pause has already closed the session's entry by the time the button is
    pressed, so the resumed entry is rebuilt from the pending question -- and a
    resume that assumed "channel" would relabel every DM turn an interrupt ever
    paused.
    """
    channel, received = _channel()
    await channel._handle_slack_event(
        _dm_event(ts="1710000001.000100"),
        _body("Ev01"),
        is_dm=True,
        trigger="dm",
    )
    (opening,) = received
    received.clear()

    await channel.send(_question(turn_request_id=opening.id))
    await channel.send(_terminal(request_id=opening.id))

    body, action = _click(user_id=BYSTANDER)
    await channel._handle_question_action(_Ack(), body, action)

    initiator = channel.turn_initiator(DM_SESSION)
    assert initiator is not None
    assert initiator.user_id == ASKER
    assert initiator.is_dm is True


@pytest.mark.asyncio
async def test_a_resumed_thread_turn_keeps_the_channel_surface() -> None:
    """The other half of the same fact, so neither direction is a default."""
    channel, received = _channel()
    await channel._handle_slack_event(
        _room_event(user=ASKER, ts=THREAD, thread_ts=THREAD),
        _body("Ev01"),
        is_dm=False,
        trigger="mention",
    )
    (opening,) = received
    received.clear()

    await channel.send(
        _question(turn_request_id=opening.id, session_id=ROOM_SESSION, channel_id=ROOM)
    )
    await channel.send(
        _terminal(request_id=opening.id, session_id=ROOM_SESSION, channel_id=ROOM)
    )

    body, action = _click(user_id=BYSTANDER, session_id=ROOM_SESSION, channel_id=ROOM)
    await channel._handle_question_action(_Ack(), body, action)

    initiator = channel.turn_initiator(ROOM_SESSION)
    assert initiator is not None
    assert initiator.user_id == ASKER
    assert initiator.is_dm is False


@pytest.mark.asyncio
async def test_an_unattributable_resume_reads_the_surface_off_the_click() -> None:
    """No entry survived to say, so the conversation the click came from does.

    Slack's own id prefixes are the evidence -- D is a direct message -- which
    is what this path already classifies the request metadata by. It is a
    fallback and not the rule: a question that remembers its surface is never
    asked to guess.
    """
    channel, received = _channel()

    await channel.send(_question(turn_request_id="req-orphan"))
    body, action = _click(user_id=BYSTANDER)
    await channel._handle_question_action(_Ack(), body, action)

    (resume,) = received
    initiator = channel.turn_initiator(DM_SESSION)
    assert initiator is not None
    assert initiator.user_id == BYSTANDER
    assert initiator.is_dm is True
    assert initiator.request_id == resume.id


@pytest.mark.asyncio
async def test_an_unattributable_resume_in_a_channel_says_channel() -> None:
    channel, received = _channel()

    await channel.send(
        _question(
            turn_request_id="req-orphan", session_id=ROOM_SESSION, channel_id=ROOM
        )
    )
    body, action = _click(user_id=BYSTANDER, session_id=ROOM_SESSION, channel_id=ROOM)
    await channel._handle_question_action(_Ack(), body, action)

    initiator = channel.turn_initiator(ROOM_SESSION)
    assert initiator is not None
    assert initiator.user_id == BYSTANDER
    assert initiator.is_dm is False
