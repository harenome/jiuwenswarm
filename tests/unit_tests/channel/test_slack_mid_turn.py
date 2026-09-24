# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.

"""What happens to a Slack message that arrives while a turn is already running.

Slack has one gesture for two intents: posting a message is the only ambient
action there is, so "stop, this is wrong" and "here is one more detail" arrive
identically and, today, are resolved identically -- the running turn is
cancelled and the new message starts a fresh one. These tests hold the three
answers to that, and the default is pinned first: a connector nobody configured
behaves exactly as it did before any of this existed.

The mechanism is exercised by asking the connector for one of the other two
directly, because what *chooses* between them is a per-conversation setting that
is not this file's subject. ``_mid_turn_mode`` is the seam, and monkeypatching
it is the whole of the arrangement.

Two things this file is careful about, both of which have already bitten
elsewhere in this connector:

* **A steer must not touch the initiator record.** Steering someone's turn is
  contributing to their work, not starting your own, so the entry stays with
  whoever opened it -- which is what keeps a stop floor where it belongs.
* **A steer's own request ends immediately**, because the running turn holds the
  interaction's output lease and the adapter answers the second caller with
  ``runtime.accepted`` and an end-of-stream. Read as an ordinary ending, that
  would close a live turn's entry and let a held message out from behind a turn
  that is still working.

Warnings are captured by replacing the module logger's ``warning`` rather than
through ``caplog``: this project's loggers do not propagate, so ``caplog`` sees
nothing under the pytest CI runs on.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Iterator

import pytest

from jiuwenswarm.common.schema.message import EventType, Message
from jiuwenswarm.gateway.channel_manager.base import RobotMessageRouter
from jiuwenswarm.gateway.channel_manager.im_platforms.slack import slack_connect
from jiuwenswarm.common.scopes import MID_TURN_CANCEL, SESSION_CHANNEL
from jiuwenswarm.gateway.channel_manager.im_platforms.slack.slack_connect import (
    MID_TURN_DEFAULT,
    MID_TURN_QUEUE,
    MID_TURN_STEER,
    SlackChannel,
    SlackChannelConfig,
    _SlackQueuedMessage,
)

ASKER = "U0ASKER001"
SECOND = "U0OTHER002"
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


@pytest.fixture
def warnings(monkeypatch) -> list[str]:
    recorded: list[str] = []

    def record(message: str, *args: Any, **kwargs: Any) -> None:
        recorded.append(message % args if args else message)

    monkeypatch.setattr(slack_connect.logger, "warning", record)
    return recorded


@pytest.fixture
def records() -> Iterator[list[logging.LogRecord]]:
    """Everything the connector's own logger emits, taken off it directly.

    Not ``caplog``, which listens on the root logger: whether anything reaches
    it depends on whether this package's loggers propagate, which is a property
    of whatever configured logging first rather than of the code under test.
    Whole records rather than the ``warnings`` fixture's rendered strings,
    because the level is half of what these tests are about.
    """
    collected: list[logging.LogRecord] = []
    logger = logging.getLogger(slack_connect.__name__)
    handler = logging.Handler()
    handler.emit = collected.append  # type: ignore[method-assign]
    previous_level = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    try:
        yield collected
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous_level)


def _notice_records(records: list[logging.LogRecord]) -> list[logging.LogRecord]:
    return [r for r in records if "what became of their message" in r.getMessage()]


class _RecordingSlackClient:
    """Just enough Slack to see which reactions landed and which came off."""

    def __init__(self) -> None:
        self.posts: list[dict[str, Any]] = []
        self.ephemerals: list[dict[str, Any]] = []
        self.added: list[tuple[str, str, str]] = []
        self.removed: list[tuple[str, str, str]] = []

    async def chat_postMessage(self, **kwargs: Any) -> dict[str, str]:
        self.posts.append(kwargs)
        return {"ts": f"1710000099.{len(self.posts):06d}"}

    async def chat_update(self, **kwargs: Any) -> dict[str, str]:
        return {"ts": kwargs.get("ts", "")}

    async def chat_postEphemeral(self, **kwargs: Any) -> dict[str, bool]:
        self.ephemerals.append(kwargs)
        return {"ok": True}

    async def reactions_add(self, **kwargs: Any) -> dict[str, bool]:
        self.added.append(
            (kwargs.get("channel", ""), kwargs.get("timestamp", ""), kwargs.get("name", ""))
        )
        return {"ok": True}

    async def reactions_remove(self, **kwargs: Any) -> dict[str, bool]:
        self.removed.append(
            (kwargs.get("channel", ""), kwargs.get("timestamp", ""), kwargs.get("name", ""))
        )
        return {"ok": True}


def _channel(*, mid_turn: str = MID_TURN_CANCEL, **overrides: Any):
    overrides.setdefault("allow_from", [ASKER, SECOND])
    channel = SlackChannel(
        SlackChannelConfig(enabled=True, **overrides), RobotMessageRouter()
    )
    channel._running = True
    channel._client = _RecordingSlackClient()
    channel._mid_turn_mode = lambda *_a, **_kw: mid_turn  # type: ignore[assignment]
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


def _room_event(
    *, user: str, ts: str, text: str = "keep going", thread: str = THREAD
) -> dict:
    return {
        "type": "message",
        "channel_type": "channel",
        "channel": ROOM,
        "user": user,
        "text": text,
        "ts": ts,
        "thread_ts": thread,
    }


def _body(event_id: str) -> dict:
    return {"event_id": event_id, "team_id": TEAM}


async def _post(channel: SlackChannel, event: dict, event_id: str, *, is_dm: bool = True) -> str:
    return await channel._handle_slack_event(
        event, _body(event_id), is_dm=is_dm, trigger="dm" if is_dm else "mention"
    )


def _terminal(
    *,
    request_id: str,
    session_id: str = DM_SESSION,
    event_type: EventType = EventType.CHAT_FINAL,
    channel_id: str = DM,
    content: str = "done",
) -> Message:
    return Message(
        id=request_id,
        type="event",
        channel_id="slack",
        session_id=session_id,
        params={},
        timestamp=time.time(),
        ok=True,
        payload={"event_type": event_type.value, "content": content},
        event_type=event_type,
        metadata={"slack_channel_id": channel_id},
    )


def _accepted(
    *,
    request_id: str,
    session_id: str = DM_SESSION,
    typed: bool = True,
    channel_id: str = DM,
) -> Message:
    """The runtime saying it took the input, as a steer's own request gets back.

    ``typed`` is the difference between the two transports: a streamed request's
    chunks are typed by the gateway before publication, a non-streamed one comes
    back as a response whose payload holds the name and may not have been
    recognised as an event on the way.
    """
    return Message(
        id=request_id,
        type="event" if typed else "res",
        channel_id="slack",
        session_id=session_id,
        params={},
        timestamp=time.time(),
        ok=True,
        payload={"event_type": "runtime.accepted", "request_id": request_id},
        event_type=EventType.RUNTIME_ACCEPTED if typed else None,
        metadata={"slack_channel_id": channel_id},
    )


# --------------------------------------------------------------------------
# cancel -- asked for, no longer inherited
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_queue_is_what_an_unconfigured_connector_does() -> None:
    # Not the patched seam: the real method, so that "the default is queue" is
    # a fact about the connector rather than about this file's fixture. Read
    # through MID_TURN_DEFAULT rather than the literal, so that a change to the
    # default moves this test with it instead of failing it.
    channel = SlackChannel(
        SlackChannelConfig(enabled=True, allow_from=[ASKER]), RobotMessageRouter()
    )
    assert channel._mid_turn_mode(ROOM, ASKER) == MID_TURN_DEFAULT
    assert MID_TURN_DEFAULT == MID_TURN_QUEUE


@pytest.mark.asyncio
async def test_cancel_is_not_reached_without_being_asked_for() -> None:
    # The point of the default change: a deployment that configures nothing no
    # longer destroys a running turn, which it did for as long as cancel was
    # what "unset" meant.
    channel = SlackChannel(
        SlackChannelConfig(enabled=True, allow_from=[ASKER]), RobotMessageRouter()
    )
    assert channel._mid_turn_mode(ROOM, ASKER) != MID_TURN_CANCEL


@pytest.mark.asyncio
async def test_under_cancel_a_second_message_dispatches_and_takes_the_turn() -> None:
    channel, received = _channel(mid_turn=MID_TURN_CANCEL)

    assert (await _post(channel, _dm_event(ts="1710000001.000100"), "Ev01")).startswith(
        "dispatched:"
    )
    first = received[-1]
    assert (await _post(channel, _dm_event(ts="1710000002.000100"), "Ev02")).startswith(
        "dispatched:"
    )
    second = received[-1]

    # Two ordinary requests, neither holding a steer, and the initiator record
    # now names the turn that replaced the first.
    assert len(received) == 2
    assert "input_mode" not in (first.params or {})
    assert "input_mode" not in (second.params or {})
    initiator = channel.turn_initiator(DM_SESSION)
    assert initiator is not None
    assert initiator.request_id == second.id


# --------------------------------------------------------------------------
# steer
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_steer_only_happens_when_a_turn_is_actually_running() -> None:
    channel, received = _channel(mid_turn=MID_TURN_STEER)

    # Nothing is running, so this is an ordinary send. A steer here would fall
    # through the runtime as a follow-up round -- a fourth mode nobody chose.
    outcome = await _post(channel, _dm_event(ts="1710000001.000100"), "Ev01")

    assert outcome.startswith("dispatched:")
    assert "input_mode" not in (received[-1].params or {})
    assert channel.turn_initiator(DM_SESSION) is not None


@pytest.mark.asyncio
async def test_a_message_arriving_mid_turn_steers_the_running_round() -> None:
    channel, received = _channel(mid_turn=MID_TURN_STEER)
    await _post(channel, _dm_event(ts="1710000001.000100"), "Ev01")

    outcome = await _post(
        channel, _dm_event(ts="1710000002.000100", text="and skip the vendored ones"), "Ev02"
    )

    assert outcome.startswith("steered:")
    steer = received[-1]
    assert steer.params["input_mode"] == MID_TURN_STEER
    # The text is the whole point: it has to reach the round, not merely be
    # accounted for.
    assert "skip the vendored ones" in steer.params["query"]


@pytest.mark.asyncio
async def test_a_steer_leaves_the_initiator_with_whoever_started_the_turn() -> None:
    channel, received = _channel(mid_turn=MID_TURN_STEER)
    await _post(channel, _room_event(user=ASKER, ts="1710000001.000100"), "Ev01", is_dm=False)
    started = received[-1]

    await _post(channel, _room_event(user=SECOND, ts="1710000002.000100"), "Ev02", is_dm=False)

    initiator = channel.turn_initiator(ROOM_SESSION)
    assert initiator is not None
    # Still the person who asked for the work, under the request id their turn
    # is running as. This is the starter's floor: A may stop the turn B steered.
    assert initiator.user_id == ASKER
    assert initiator.request_id == started.id


@pytest.mark.asyncio
async def test_a_steers_acknowledgement_does_not_close_the_running_turn() -> None:
    channel, received = _channel(mid_turn=MID_TURN_STEER)
    await _post(channel, _dm_event(ts="1710000001.000100"), "Ev01")
    started = received[-1]
    await _post(channel, _dm_event(ts="1710000002.000100"), "Ev02")
    steer = received[-1]

    # What the runtime answers the steer's own request with, because the running
    # turn holds the output lease: an acceptance and then an immediate ending.
    await channel.send(_accepted(request_id=steer.id))
    await channel.send(_terminal(request_id=steer.id, content=""))

    initiator = channel.turn_initiator(DM_SESSION)
    assert initiator is not None
    assert initiator.request_id == started.id


@pytest.mark.asyncio
async def test_an_acknowledgement_is_recognised_on_either_transport() -> None:
    channel, _received = _channel()

    # Streamed: typed by the gateway before publication.
    await channel.send(_accepted(request_id="req-typed"))
    assert channel._is_ack_only_request("req-typed") is True

    # Non-streamed: the name is in the payload and nowhere else.
    await channel.send(_accepted(request_id="req-untyped", typed=False))
    assert channel._is_ack_only_request("req-untyped") is True


@pytest.mark.asyncio
async def test_the_acknowledgement_note_is_spent_when_it_is_read() -> None:
    channel, _received = _channel()
    await channel.send(_accepted(request_id="req-1"))

    assert channel._is_ack_only_request("req-1") is True
    # A replayed ending must not be excused twice: the note answered the one
    # question it existed for.
    assert channel._is_ack_only_request("req-1") is False


@pytest.mark.asyncio
async def test_the_real_turns_ending_still_closes_its_entry() -> None:
    # The other half of the fix: excusing an acknowledged request must not
    # excuse the turn itself, or nothing would ever close.
    channel, received = _channel(mid_turn=MID_TURN_STEER)
    await _post(channel, _dm_event(ts="1710000001.000100"), "Ev01")
    started = received[-1]
    await _post(channel, _dm_event(ts="1710000002.000100"), "Ev02")
    steer = received[-1]
    await channel.send(_accepted(request_id=steer.id))
    await channel.send(_terminal(request_id=steer.id, content=""))

    await channel.send(_terminal(request_id=started.id))

    assert channel.turn_initiator(DM_SESSION) is None


# --------------------------------------------------------------------------
# queue
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_message_arriving_mid_turn_is_held_not_dispatched() -> None:
    channel, received = _channel(mid_turn=MID_TURN_QUEUE)
    await _post(channel, _dm_event(ts="1710000001.000100"), "Ev01")
    assert len(received) == 1

    outcome = await _post(
        channel, _dm_event(ts="1710000002.000100", text="then open a PR"), "Ev02"
    )

    assert outcome.startswith("queued:")
    # Nothing left the connector, which is what makes queue need no output
    # routing of its own.
    assert len(received) == 1
    assert len(channel._queued_messages[DM_SESSION]) == 1


@pytest.mark.asyncio
async def test_the_turn_ending_dispatches_the_held_message_as_an_ordinary_turn() -> None:
    channel, received = _channel(mid_turn=MID_TURN_QUEUE)
    await _post(channel, _dm_event(ts="1710000001.000100"), "Ev01")
    started = received[-1]
    await _post(channel, _dm_event(ts="1710000002.000100", text="then open a PR"), "Ev02")

    await channel.send(_terminal(request_id=started.id))

    assert len(received) == 2
    drained = received[-1]
    # An ordinary send: no steer, its own request id, its own initiator.
    assert "input_mode" not in (drained.params or {})
    assert "then open a PR" in drained.params["query"]
    initiator = channel.turn_initiator(DM_SESSION)
    assert initiator is not None
    assert initiator.request_id == drained.id
    assert DM_SESSION not in channel._queued_messages


@pytest.mark.asyncio
async def test_only_one_held_message_leaves_per_ending() -> None:
    channel, received = _channel(mid_turn=MID_TURN_QUEUE)
    await _post(channel, _dm_event(ts="1710000001.000100"), "Ev01")
    started = received[-1]
    await _post(channel, _dm_event(ts="1710000002.000100", text="first held"), "Ev02")
    await _post(channel, _dm_event(ts="1710000003.000100", text="second held"), "Ev03")

    await channel.send(_terminal(request_id=started.id))

    # Each held message is its own turn, so the second cannot start until the
    # first has ended.
    assert len(received) == 2
    assert "first held" in received[-1].params["query"]
    assert len(channel._queued_messages[DM_SESSION]) == 1

    await channel.send(_terminal(request_id=received[-1].id))

    assert len(received) == 3
    assert "second held" in received[-1].params["query"]


@pytest.mark.asyncio
async def test_a_second_ending_for_one_turn_does_not_release_a_second_message() -> None:
    # The guardrail the web client's queue has and warns about porting without:
    # a late close arriving after the drain must not reopen the idle state and
    # let another message out beside the one already running.
    channel, received = _channel(mid_turn=MID_TURN_QUEUE)
    await _post(channel, _dm_event(ts="1710000001.000100"), "Ev01")
    started = received[-1]
    await _post(channel, _dm_event(ts="1710000002.000100", text="first held"), "Ev02")
    await _post(channel, _dm_event(ts="1710000003.000100", text="second held"), "Ev03")
    await channel.send(_terminal(request_id=started.id))
    assert len(received) == 2

    # An error after the final, a replayed close: same request id, again.
    await channel.send(
        _terminal(request_id=started.id, event_type=EventType.CHAT_ERROR, content="")
    )

    assert len(received) == 2
    assert len(channel._queued_messages[DM_SESSION]) == 1


@pytest.mark.asyncio
async def test_a_held_message_is_not_overtaken_by_one_sent_after_it() -> None:
    # The stale-queue case: the turn ended without an ending ever reaching this
    # connector, so nothing drained. The next message must still queue behind
    # what is already waiting rather than jumping it.
    channel, received = _channel(mid_turn=MID_TURN_QUEUE)
    await _post(channel, _dm_event(ts="1710000001.000100"), "Ev01")
    await _post(channel, _dm_event(ts="1710000002.000100", text="held first"), "Ev02")
    channel._turn_initiators.clear()

    outcome = await _post(
        channel, _dm_event(ts="1710000003.000100", text="sent later"), "Ev03"
    )

    # It joined the queue, and the drain that followed released the head -- the
    # message that was already waiting, not the one that just arrived.
    assert outcome.startswith("queued:")
    assert len(received) == 2
    assert "held first" in received[-1].params["query"]
    assert len(channel._queued_messages[DM_SESSION]) == 1


@pytest.mark.asyncio
async def test_a_queue_is_shared_in_a_thread_and_private_in_a_dm() -> None:
    channel, received = _channel(mid_turn=MID_TURN_QUEUE)
    await _post(channel, _room_event(user=ASKER, ts="1710000001.000100"), "Ev01", is_dm=False)

    await _post(channel, _room_event(user=SECOND, ts="1710000002.000100"), "Ev02", is_dm=False)
    # A DM is keyed on the person, so it is a different session and nothing
    # there is waiting on the thread's turn.
    outcome = await _post(channel, _dm_event(ts="1710000003.000100"), "Ev03")

    assert len(channel._queued_messages[ROOM_SESSION]) == 1
    assert outcome.startswith("dispatched:")
    assert DM_SESSION not in channel._queued_messages


@pytest.mark.asyncio
async def test_a_held_message_says_so_on_the_senders_own_message() -> None:
    channel, received = _channel(mid_turn=MID_TURN_QUEUE)
    await _post(channel, _dm_event(ts="1710000001.000100"), "Ev01")
    started = received[-1]
    channel._client.added.clear()

    await _post(channel, _dm_event(ts="1710000002.000100"), "Ev02")

    assert (DM, "1710000002.000100", channel.config.queued_emoji) in channel._client.added

    await channel.send(_terminal(request_id=started.id))

    # And the mark comes off when it stops being true.
    assert (DM, "1710000002.000100", channel.config.queued_emoji) in channel._client.removed


@pytest.mark.asyncio
async def test_the_queued_mark_is_not_added_when_acknowledgements_are_off() -> None:
    channel, received = _channel(mid_turn=MID_TURN_QUEUE, acknowledge_mode="off")
    await _post(channel, _dm_event(ts="1710000001.000100"), "Ev01")

    await _post(channel, _dm_event(ts="1710000002.000100"), "Ev02")

    # off is a deployment asking for no feedback at all, and this is feedback.
    assert channel._client.added == []
    assert len(channel._queued_messages[DM_SESSION]) == 1


@pytest.mark.asyncio
async def test_a_full_queue_refuses_the_newest_and_says_so(warnings) -> None:
    channel, received = _channel(mid_turn=MID_TURN_QUEUE)
    await _post(channel, _dm_event(ts="1710000001.000100"), "Ev01")
    for index in range(slack_connect._MAX_QUEUED_PER_SESSION):
        await _post(
            channel,
            _dm_event(ts=f"17100001{index:02d}.000100", text=f"held {index}"),
            f"EvQ{index}",
        )
    assert len(channel._queued_messages[DM_SESSION]) == slack_connect._MAX_QUEUED_PER_SESSION

    outcome = await _post(
        channel, _dm_event(ts="1710000999.000100", text="one too many"), "EvOver"
    )

    assert outcome.startswith("refused:queue-full")
    # The newest, never the oldest: every message already waiting holds a mark
    # telling its sender it will run.
    assert len(channel._queued_messages[DM_SESSION]) == slack_connect._MAX_QUEUED_PER_SESSION
    assert "held 0" in channel._queued_messages[DM_SESSION][0].request.params["query"]
    assert any("which is the cap" in line for line in warnings)
    assert (
        DM,
        "1710000999.000100",
        channel.config.rejected_emoji,
    ) in channel._client.added


@pytest.mark.asyncio
async def test_a_held_message_does_not_outlive_the_turn_it_waited_on(warnings) -> None:
    channel, received = _channel(mid_turn=MID_TURN_QUEUE)
    await _post(channel, _dm_event(ts="1710000001.000100"), "Ev01")
    await _post(channel, _dm_event(ts="1710000002.000100"), "Ev02")
    held = channel._queued_messages[DM_SESSION][0]

    # The turn whose ending never arrived: the age is what covers it.
    held.queued_at -= slack_connect._QUEUED_MESSAGE_TIMEOUT_SECONDS + 1.0
    channel._prune_queued_messages()

    assert DM_SESSION not in channel._queued_messages
    assert any("never reported that it ended" in line for line in warnings)


def test_too_many_waiting_sessions_drop_the_one_waiting_longest(warnings) -> None:
    channel, _received = _channel(mid_turn=MID_TURN_QUEUE)
    for index in range(slack_connect._MAX_QUEUED_SESSIONS):
        session_id = f"slack_{TEAM}_{DM}_U{index:05d}"
        entry = _SlackQueuedMessage(
            request=Message(
                id=f"req-{index}",
                type="req",
                channel_id="slack",
                session_id=session_id,
                params={"query": "hold"},
                timestamp=time.time(),
                ok=True,
            ),
            session_id=session_id,
            user_id=f"U{index:05d}",
            is_dm=True,
            chat_type="im",
        )
        # Oldest head first, so the eviction below has an unambiguous victim.
        entry.queued_at -= float(slack_connect._MAX_QUEUED_SESSIONS - index)
        assert channel._queue_mid_turn_message(entry) is True

    channel._prune_queued_messages()

    assert f"slack_{TEAM}_{DM}_U00000" not in channel._queued_messages
    assert any("too many" in line for line in warnings)


@pytest.mark.asyncio
async def test_stopping_the_channel_drops_what_it_promised_to_run(warnings) -> None:
    channel, received = _channel(mid_turn=MID_TURN_QUEUE)
    await _post(channel, _dm_event(ts="1710000001.000100"), "Ev01")
    await _post(channel, _dm_event(ts="1710000002.000100"), "Ev02")

    await channel.stop()

    assert channel._queued_messages == {}
    # Said out loud, because the sender was told it would run.
    assert any("queued message(s) across" in line for line in warnings)


# --------------------------------------------------------------------------
# what a refused message is told
#
# A refusal is the only outcome on this path that discards what somebody typed.
# A dispatched message and a held one both end up in the session's history; an
# ignored one is left untouched in Slack and nothing ever comes back about it.
# Seventeen of them landed in one DM with nothing but the rejected emoji to go
# on, which says "not accepted" and cannot say that the work is still running or
# that sending it again later would work.
# --------------------------------------------------------------------------


async def _fill_the_queue(channel: SlackChannel) -> None:
    await _post(channel, _dm_event(ts="1710000001.000100"), "Ev01")
    for index in range(slack_connect._MAX_QUEUED_PER_SESSION):
        await _post(
            channel,
            _dm_event(ts=f"17100001{index:02d}.000100", text=f"held {index}"),
            f"EvQ{index}",
        )


@pytest.mark.asyncio
async def test_a_refused_message_is_told_why_and_what_to_do() -> None:
    channel, _received = _channel(mid_turn=MID_TURN_QUEUE)
    await _fill_the_queue(channel)

    outcome = await _post(
        channel, _dm_event(ts="1710000999.000100", text="one too many"), "EvOver"
    )

    assert outcome.startswith("refused:queue-full")
    assert len(channel._client.ephemerals) == 1
    notice = channel._client.ephemerals[0]
    # To the sender and to nobody else, in the conversation it happened in.
    assert notice["channel"] == DM
    assert notice["user"] == ASKER
    text = notice["text"]
    # The cap, so the state is legible rather than mysterious.
    assert str(slack_connect._MAX_QUEUED_PER_SESSION) in text
    # That it was not taken, and that this is not permanent.
    assert "not accepted" in text
    assert "again" in text


@pytest.mark.asyncio
async def test_the_refusal_notice_never_reaches_the_conversation() -> None:
    """Ephemeral for the reason the stop refusal is: it concerns one person.

    Everyone else in a thread may not have seen the message at all, and telling
    them a queue they are not in is full is noise about somebody else's turn.
    """
    channel, _received = _channel(mid_turn=MID_TURN_QUEUE)
    await _fill_the_queue(channel)

    await _post(channel, _dm_event(ts="1710000999.000100"), "EvOver")

    assert channel._client.posts == []


@pytest.mark.asyncio
async def test_a_refused_message_is_told_nothing_when_feedback_is_off() -> None:
    """``off`` is a deployment asking for no feedback at all, and this is feedback."""
    channel, _received = _channel(mid_turn=MID_TURN_QUEUE, acknowledge_mode="off")
    await _fill_the_queue(channel)

    await _post(channel, _dm_event(ts="1710000999.000100"), "EvOver")

    assert channel._client.ephemerals == []
    assert channel._client.added == []


@pytest.mark.asyncio
async def test_a_refusal_under_text_mode_is_still_explained() -> None:
    """The notice is not gated on the modes the mark is gated on.

    ``text`` posts no reactions, so ``_reject_request`` leaves nothing at all on
    a refused message. Withholding the notice there too would make that
    deployment's refusals completely silent, which is the failure this fixes.
    """
    channel, _received = _channel(mid_turn=MID_TURN_QUEUE, acknowledge_mode="text")
    await _fill_the_queue(channel)

    await _post(channel, _dm_event(ts="1710000999.000100"), "EvOver")

    assert channel._client.added == []
    assert len(channel._client.ephemerals) == 1


@pytest.mark.asyncio
async def test_a_refusal_notice_that_cannot_be_posted_is_still_a_refusal(
    warnings,
) -> None:
    """Best effort, like every other courtesy this connector posts.

    The message was refused before this ran and stays refused whatever Slack
    says about the explanation.
    """
    channel, _received = _channel(mid_turn=MID_TURN_QUEUE)
    await _fill_the_queue(channel)

    async def _refuse(**_kwargs: Any) -> dict[str, bool]:
        raise RuntimeError("channel_not_found")

    channel._client.chat_postEphemeral = _refuse  # type: ignore[method-assign]

    outcome = await _post(channel, _dm_event(ts="1710000999.000100"), "EvOver")

    assert outcome.startswith("refused:queue-full")
    assert len(channel._queued_messages[DM_SESSION]) == (
        slack_connect._MAX_QUEUED_PER_SESSION
    )
    assert any("what became of their message" in line for line in warnings)


@pytest.mark.asyncio
async def test_the_notice_says_nothing_about_what_was_in_the_message() -> None:
    """The boundary rule, held here as it is on the log line.

    ``_event_identity`` names which message rather than what was in it, and that
    is what makes it safe at INFO forever. A notice quoting the refused text
    back would put the same content somewhere else -- and it would be pointless,
    because the sender is looking at their own message.
    """
    channel, _received = _channel(mid_turn=MID_TURN_QUEUE)
    await _fill_the_queue(channel)

    secret = "the passphrase is hunter2"
    await _post(channel, _dm_event(ts="1710000999.000100", text=secret), "EvOver")

    assert channel._client.ephemerals
    assert secret not in channel._client.ephemerals[0]["text"]


@pytest.mark.asyncio
async def test_a_notice_that_was_delivered_says_so_in_the_log(records) -> None:
    """The refusal is auditable from the log alone, which it was not.

    Eighteen refusals in one live queue-overflow run each wrote a line saying
    the message was dropped, and nothing anywhere said whether the eighteen
    senders had been told. A grep answering neither way is consistent with
    "the notices went out" and with "this code was never reached", and only
    somebody watching Slack at the time could tell them apart -- on a surface
    where an ephemeral is delivered to the clients connected at that moment and
    to nobody afterwards.
    """
    channel, _received = _channel(mid_turn=MID_TURN_QUEUE)
    await _fill_the_queue(channel)

    await _post(channel, _dm_event(ts="1710000999.000100"), "EvOver")

    told = _notice_records(records)
    assert len(told) == 1
    assert told[0].levelno == logging.INFO
    assert ASKER in told[0].getMessage()
    assert "queue-full" in told[0].getMessage()


@pytest.mark.asyncio
async def test_the_record_says_which_notice_and_not_what_it_said(records) -> None:
    """The boundary rule the notice itself keeps, kept again on the way out.

    These hold user-facing prose, and elsewhere in this connector they quote a
    person's input back at them. ``notice`` names which one was sent so that the
    log answers "who was told what about" without becoming a second copy of it.

    The recipient is written as a bare ``%s`` rather than ``user_id=%s``
    deliberately: ``_KV_SENSITIVE_PATTERN`` in ``jiuwenswarm.common.utils`` masks
    the value of any key matching ``user[_-]?id``, so the second form would
    redact the one field that makes the record worth keeping.
    """
    channel, _received = _channel(mid_turn=MID_TURN_QUEUE)
    await _fill_the_queue(channel)

    await _post(channel, _dm_event(ts="1710000999.000100"), "EvOver")

    message = _notice_records(records)[0].getMessage()
    assert slack_connect._QUEUE_FULL_NOTICE not in message
    assert "user_id=" not in message


@pytest.mark.asyncio
async def test_a_notice_nobody_attempted_is_not_recorded_as_delivered(
    records,
) -> None:
    """What makes the record worth reading: it means told, not tried.

    Under ``off`` the refusal is silent by configuration, and the paths into
    ``_post_ephemeral`` return early for a missing client or channel as well. A
    record written before those returns would say the sender was told in exactly
    the cases where nobody was.
    """
    channel, _received = _channel(mid_turn=MID_TURN_QUEUE, acknowledge_mode="off")
    await _fill_the_queue(channel)

    await _post(channel, _dm_event(ts="1710000999.000100"), "EvOver")

    assert channel._client.ephemerals == []
    assert _notice_records(records) == []


@pytest.mark.asyncio
async def test_a_notice_slack_refused_is_not_recorded_as_delivered(records) -> None:
    """The failure is warned about and is not also reported as a success."""
    channel, _received = _channel(mid_turn=MID_TURN_QUEUE)
    await _fill_the_queue(channel)

    async def _refuse(**_kwargs: Any) -> dict[str, bool]:
        raise RuntimeError("channel_not_found")

    channel._client.chat_postEphemeral = _refuse  # type: ignore[method-assign]

    await _post(channel, _dm_event(ts="1710000999.000100"), "EvOver")

    assert [r.levelno for r in _notice_records(records)] == [logging.WARNING]


# --------------------------------------------------------------------------
# who the preview is addressed to
# --------------------------------------------------------------------------
#
# ``chat.startStream`` refuses a channel stream without the user it is for, so
# the connector notes that person when a turn is dispatched and reads it back
# when the reply opens its stream. The note is keyed by session, and a session
# can hold a running turn and a queue behind it -- so what each of the three
# mid-turn answers leaves in it is the whole of what these tests are about. The
# entry is read for nothing else: it names a recipient and does not decide
# where the reply is posted.


def _recipient(channel: SlackChannel, session_id: str) -> tuple[str, str] | None:
    return channel._stream_recipients.get(session_id)


@pytest.mark.asyncio
async def test_an_ordinary_dispatch_addresses_the_preview_to_its_sender() -> None:
    channel, _received = _channel(mid_turn=MID_TURN_QUEUE)

    await _post(channel, _room_event(user=ASKER, ts="1710000001.000100"), "Ev01", is_dm=False)

    assert _recipient(channel, ROOM_SESSION) == (ASKER, TEAM)


@pytest.mark.asyncio
async def test_a_queued_message_leaves_the_running_turns_preview_alone() -> None:
    channel, _received = _channel(mid_turn=MID_TURN_QUEUE)
    await _post(channel, _room_event(user=ASKER, ts="1710000001.000100"), "Ev01", is_dm=False)

    outcome = await _post(
        channel, _room_event(user=SECOND, ts="1710000002.000100"), "Ev02", is_dm=False
    )

    # The held message has no turn, so it has nothing to be previewed as. The
    # preview on screen belongs to the turn that is still running.
    assert outcome.startswith("queued:")
    assert _recipient(channel, ROOM_SESSION) == (ASKER, TEAM)


@pytest.mark.asyncio
async def test_a_drained_message_addresses_its_own_preview_to_its_sender() -> None:
    channel, received = _channel(mid_turn=MID_TURN_QUEUE)
    await _post(channel, _room_event(user=ASKER, ts="1710000001.000100"), "Ev01", is_dm=False)
    started = received[-1]
    await _post(channel, _room_event(user=SECOND, ts="1710000002.000100"), "Ev02", is_dm=False)

    await channel.send(
        _terminal(request_id=started.id, session_id=ROOM_SESSION, channel_id=ROOM)
    )

    # Drained, and now a turn of its own: its reply is previewed to the person
    # who sent it, not to whoever it was waiting behind.
    assert len(received) == 2
    assert _recipient(channel, ROOM_SESSION) == (SECOND, TEAM)


@pytest.mark.asyncio
async def test_a_steer_leaves_the_preview_addressed_to_the_turns_starter() -> None:
    channel, _received = _channel(mid_turn=MID_TURN_STEER)
    await _post(channel, _room_event(user=ASKER, ts="1710000001.000100"), "Ev01", is_dm=False)

    outcome = await _post(
        channel, _room_event(user=SECOND, ts="1710000002.000100"), "Ev02", is_dm=False
    )

    # The same rule the initiator record keeps, for the same reason: steering
    # someone's turn is contributing to their work rather than starting your
    # own, and the preview is that turn's.
    assert outcome.startswith("steered:")
    assert _recipient(channel, ROOM_SESSION) == (ASKER, TEAM)
    initiator = channel.turn_initiator(ROOM_SESSION)
    assert initiator is not None
    assert initiator.user_id == ASKER


@pytest.mark.asyncio
async def test_two_threads_under_one_channel_session_each_address_their_own() -> None:
    """One session for a whole room, so the collision reaches across threads.

    Under a thread-keyed session the two messages below would be two sessions
    and could not touch each other's entries at all. Under a channel-wide one
    they share every session-keyed record this connector holds, which is what
    makes a message from a thread the running turn has nothing to do with able
    to reach it.
    """
    channel, received = _channel(mid_turn=MID_TURN_QUEUE)
    channel._session_scope = lambda *_a, **_kw: SESSION_CHANNEL  # type: ignore[assignment]
    session = f"slack_{TEAM}_{ROOM}"

    await _post(
        channel,
        _room_event(user=ASKER, ts="1710000001.000100", thread="1710000001.000100"),
        "Ev01",
        is_dm=False,
    )
    started = received[-1]
    outcome = await _post(
        channel,
        _room_event(user=SECOND, ts="1710000002.000100", thread="1710000002.000100"),
        "Ev02",
        is_dm=False,
    )

    # One session for both threads, so the second message queues behind a turn
    # started in a thread it was never posted in -- and leaves that turn's
    # preview where it was.
    assert started.session_id == session
    assert outcome.startswith("queued:")
    assert _recipient(channel, session) == (ASKER, TEAM)

    await channel.send(
        _terminal(request_id=started.id, session_id=session, channel_id=ROOM)
    )

    assert len(received) == 2
    assert _recipient(channel, session) == (SECOND, TEAM)
