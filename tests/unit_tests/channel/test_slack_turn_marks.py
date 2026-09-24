# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.

"""What the reaction on a Slack message says once the turn it started is over.

The acknowledgement is added at dispatch and, before this, was never taken off
again. A message therefore wore the same mark whether its turn was still
working, had answered an hour ago, had broken, or had been stopped -- so the
mark said "seen at some point" and nothing else, and a conversation the agent
works through all day became a column of identical emoji.

These tests pin the six-state vocabulary the mark now speaks. Three of the
states already existed and are asserted here as unchanged: running, waiting
behind another turn, and never taken. Three are the endings: finished, broken,
halted.

The case worth naming on its own is **a turn that finishes without replying**.
It posts no message and may never put a card up, so the reaction is the only
evidence in the conversation that the work happened. Marking it exactly as a
turn that answered is what makes a deliberate silence legible instead of
indistinguishable from a turn that died.

The endings arrive by three different routes, which is why there are three
groups below. A turn that ends normally sends a terminal event. A turn that is
stopped or superseded sends nothing at all -- the same gap that once left
activity cards pinned at "Working..." -- and is marked from the initiator record
the two cancel paths already read.
"""

from __future__ import annotations

import time
from typing import Any

import pytest

from jiuwenswarm.common.schema.message import EventType, Message
from jiuwenswarm.gateway.channel_manager.base import RobotMessageRouter
from jiuwenswarm.gateway.channel_manager.im_platforms.slack import slack_connect
from jiuwenswarm.common.scopes import MID_TURN_CANCEL
from jiuwenswarm.gateway.channel_manager.im_platforms.slack.slack_connect import (
    MID_TURN_QUEUE,
    SlackChannel,
    SlackChannelConfig,
)

ASKER = "U0ASKER001"
SECOND = "U0OTHER002"
TEAM = "T0TESTTEAM"
DM = "D0DIRECT01"

ACKNOWLEDGED = "eyes"
QUEUED = "hourglass_flowing_sand"
COMPLETED = "heavy_check_mark"
FAILED = "x"
STOPPED = "black_square_for_stop"
REJECTED = "no_entry_sign"


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
    """Just enough Slack to see which reactions landed and which came off."""

    def __init__(self) -> None:
        self.posts: list[dict[str, Any]] = []
        self.added: list[tuple[str, str, str]] = []
        self.removed: list[tuple[str, str, str]] = []

    async def chat_postMessage(self, **kwargs: Any) -> dict[str, str]:
        self.posts.append(kwargs)
        return {"ts": f"1710000099.{len(self.posts):06d}"}

    async def chat_update(self, **kwargs: Any) -> dict[str, str]:
        return {"ts": kwargs.get("ts", "")}

    async def chat_postEphemeral(self, **kwargs: Any) -> dict[str, bool]:
        return {"ok": True}

    async def api_call(self, api_method: str, **kwargs: Any) -> dict[str, bool]:
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


def _channel(
    *, mid_turn: str = MID_TURN_CANCEL, client: Any = None, **overrides: Any
) -> tuple[SlackChannel, list[Message], Any]:
    overrides.setdefault("allow_from", [ASKER, SECOND])
    channel = SlackChannel(
        SlackChannelConfig(enabled=True, **overrides), RobotMessageRouter()
    )
    channel._running = True
    channel._client = client if client is not None else _RecordingSlackClient()
    channel._mid_turn_mode = lambda *_a, **_kw: mid_turn  # type: ignore[assignment]
    dispatched: list[Message] = []
    channel.on_message(dispatched.append)
    return channel, dispatched, channel._client


def _dm_event(*, user: str = ASKER, ts: str, text: str = "sweep the repos") -> dict:
    return {
        "type": "message",
        "channel_type": "im",
        "channel": DM,
        "user": user,
        "text": text,
        "ts": ts,
    }


async def _post(channel: SlackChannel, event: dict, event_id: str) -> str:
    return await channel._handle_slack_event(
        event, {"event_id": event_id, "team_id": TEAM}, is_dm=True, trigger="dm"
    )


def _ending(
    request: Message,
    *,
    event_type: EventType = EventType.CHAT_FINAL,
    content: str = "done",
) -> Message:
    """The turn's terminal event, keyed to the request the connector dispatched.

    Built from the dispatched request rather than from literals so that the id
    and the session are the ones the initiator entry was actually opened under.
    Matching those is the whole of what the terminal branch checks, and a test
    that guessed them would pass while checking nothing.
    """
    return Message(
        id=str(request.id),
        type="event",
        channel_id="slack",
        session_id=str(request.session_id),
        params={},
        timestamp=time.time(),
        ok=True,
        payload={"event_type": event_type.value, "content": content},
        event_type=event_type,
        metadata={"slack_channel_id": DM},
    )


# --------------------------------------------------------------------------
# A turn that reached its terminal event
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_turn_that_replies_ends_marked_completed_and_not_acknowledged() -> None:
    channel, dispatched, client = _channel()
    ts = "1710000010.000100"

    await _post(channel, _dm_event(ts=ts), "EvReplied")
    assert client.added == [(DM, ts, ACKNOWLEDGED)]

    await channel.send(_ending(dispatched[0]))

    assert client.removed == [(DM, ts, ACKNOWLEDGED)]
    assert client.added == [(DM, ts, ACKNOWLEDGED), (DM, ts, COMPLETED)]


@pytest.mark.asyncio
async def test_a_turn_that_produces_no_reply_is_marked_completed_like_any_other() -> None:
    """The case the mark exists for: silence that did its work is still success.

    Nothing is posted, so the reaction is the only thing in the conversation
    that says the message was processed at all. Asserted alongside an empty
    ``posts`` so that "marked completed" cannot pass by way of a reply nobody
    asked for.
    """
    channel, dispatched, client = _channel()
    ts = "1710000011.000100"

    await _post(channel, _dm_event(ts=ts), "EvSilent")
    await channel.send(_ending(dispatched[0], content=""))

    assert client.posts == []
    assert client.removed == [(DM, ts, ACKNOWLEDGED)]
    assert client.added == [(DM, ts, ACKNOWLEDGED), (DM, ts, COMPLETED)]


@pytest.mark.asyncio
async def test_a_turn_that_ran_and_broke_is_marked_failed_rather_than_refused() -> None:
    """Failed and refused are different marks because they mean different things.

    Refused means no turn started and the same message may go through later;
    failed means one started, may have had side effects, and then died. The
    rejected mark is asserted absent explicitly, because reusing it here is the
    plausible shortcut this test exists to rule out.
    """
    channel, dispatched, client = _channel()
    ts = "1710000012.000100"

    await _post(channel, _dm_event(ts=ts), "EvBroke")
    await channel.send(
        _ending(dispatched[0], event_type=EventType.CHAT_ERROR, content="the run died")
    )

    assert client.removed == [(DM, ts, ACKNOWLEDGED)]
    assert client.added == [(DM, ts, ACKNOWLEDGED), (DM, ts, FAILED)]
    assert REJECTED not in [name for _c, _t, name in client.added]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["text", "off"])
async def test_a_deployment_that_does_not_react_gains_no_ending_mark(mode: str) -> None:
    channel, dispatched, client = _channel(acknowledge_mode=mode)
    ts = "1710000013.000100"

    await _post(channel, _dm_event(ts=ts), f"EvNoReact-{mode}")
    await channel.send(_ending(dispatched[0]))

    assert client.added == []
    assert client.removed == []


@pytest.mark.asyncio
async def test_a_reaction_call_that_fails_does_not_fail_the_turn() -> None:
    """Both calls are best effort, and each is issued whatever the other did.

    The remove is what fails here, because that is the ordering the helper
    commits to: the ending mark must still land on a workspace where
    ``reactions.remove`` is refused, or a missing scope would cost the very
    thing this feature adds.
    """

    class _RefusingRemoveClient(_RecordingSlackClient):
        async def reactions_remove(self, **kwargs: Any) -> dict[str, bool]:
            self.removed.append(_coordinates(kwargs))
            raise RuntimeError("missing_scope")

    channel, dispatched, client = _channel(client=_RefusingRemoveClient())
    ts = "1710000014.000100"

    await _post(channel, _dm_event(ts=ts), "EvRemoveBoom")
    await channel.send(_ending(dispatched[0]))

    assert client.added == [(DM, ts, ACKNOWLEDGED), (DM, ts, COMPLETED)]


@pytest.mark.asyncio
async def test_an_ending_marks_nothing_when_a_later_turn_owns_the_session() -> None:
    """A cancelled turn's late ending must not mark the message that replaced it.

    Two messages in a cancelling conversation: the second supersedes the first
    and takes over the session's initiator entry. The first turn's terminal
    event then arrives, and the only marks it may leave are the halted one
    supersession already wrote.
    """
    channel, dispatched, client = _channel(mid_turn=MID_TURN_CANCEL)
    first_ts = "1710000015.000100"
    second_ts = "1710000015.000200"

    await _post(channel, _dm_event(ts=first_ts), "EvFirst")
    await _post(channel, _dm_event(ts=second_ts, text="no, the other one"), "EvSecond")
    before = list(client.added)

    await channel.send(_ending(dispatched[0]))

    assert client.added == before
    assert (DM, second_ts, COMPLETED) not in client.added


@pytest.mark.asyncio
async def test_an_acknowledgement_only_ending_leaves_the_running_turn_marked() -> None:
    """A request the runtime merely took is not the turn ending.

    ``runtime.accepted`` is followed immediately by an end-of-stream while the
    turn it was folded into is still working. Marking on that would tell a
    reader the work is over while it is still running.
    """
    channel, dispatched, client = _channel()
    ts = "1710000016.000100"

    await _post(channel, _dm_event(ts=ts), "EvAckOnly")
    request_id = str(dispatched[0].id)
    accepted = _ending(dispatched[0])
    accepted.event_type = EventType.RUNTIME_ACCEPTED
    accepted.payload = {"event_type": "runtime.accepted", "request_id": request_id}

    await channel.send(accepted)
    await channel.send(_ending(dispatched[0], content=""))

    assert client.removed == []
    assert client.added == [(DM, ts, ACKNOWLEDGED)]


# --------------------------------------------------------------------------
# A turn that was destroyed and sends no ending of its own
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_turn_the_stop_button_halted_is_marked_stopped() -> None:
    """Halted is its own mark: nothing broke, and it did run.

    Driven through the release path rather than through a button click, because
    that method is the seam the stop handler reaches for exactly this: it is
    where a stopped turn's initiator entry is retired, and the entry is what
    holds the coordinates the mark is written at.
    """
    channel, dispatched, client = _channel()
    ts = "1710000017.000100"

    await _post(channel, _dm_event(ts=ts), "EvStopped")
    request = dispatched[0]

    await channel._release_stopped_turn(
        str(request.session_id), str(request.id)
    )

    assert client.removed == [(DM, ts, ACKNOWLEDGED)]
    assert client.added == [(DM, ts, ACKNOWLEDGED), (DM, ts, STOPPED)]


@pytest.mark.asyncio
async def test_a_turn_a_later_message_superseded_is_marked_stopped() -> None:
    """The commoner halt: somebody typed twice and the first turn was destroyed.

    The second message keeps its own acknowledgement -- its turn is running --
    so the assertion covers both messages rather than only the halted one.
    """
    channel, dispatched, client = _channel(mid_turn=MID_TURN_CANCEL)
    first_ts = "1710000018.000100"
    second_ts = "1710000018.000200"

    await _post(channel, _dm_event(ts=first_ts), "EvSupersededFirst")
    await _post(
        channel, _dm_event(ts=second_ts, text="no, the other one"), "EvSupersededSecond"
    )

    assert client.removed == [(DM, first_ts, ACKNOWLEDGED)]
    assert client.added == [
        (DM, first_ts, ACKNOWLEDGED),
        (DM, second_ts, ACKNOWLEDGED),
        (DM, first_ts, STOPPED),
    ]


@pytest.mark.asyncio
async def test_a_stopped_turn_gains_no_mark_where_reactions_are_off() -> None:
    channel, dispatched, client = _channel(acknowledge_mode="off")
    ts = "1710000019.000100"

    await _post(channel, _dm_event(ts=ts), "EvStoppedQuiet")
    request = dispatched[0]

    await channel._release_stopped_turn(
        str(request.session_id), str(request.id)
    )

    assert client.added == []
    assert client.removed == []


# --------------------------------------------------------------------------
# The three states that existed already, asserted unchanged
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_refused_request_still_gets_the_rejected_mark_and_nothing_else() -> None:
    channel, dispatched, client = _channel(allow_from=["U-SOMEBODY-ELSE"])
    ts = "1710000020.000100"

    await _post(channel, _dm_event(ts=ts), "EvRefused")

    assert dispatched == []
    assert client.added == [(DM, ts, REJECTED)]
    assert client.removed == []


@pytest.mark.asyncio
async def test_a_held_message_still_gains_and_loses_only_the_waiting_mark() -> None:
    """The queue's own two marks, unchanged by the endings around them.

    A held message is acknowledged like any other on arrival and then gains the
    waiting mark on top; when the turn ahead of it ends the waiting mark comes
    off and the acknowledgement stays, which is correct because the held
    message's own turn is now the one running. Its ending is a separate event
    and is not sent here.

    The first message's completion mark is asserted only because it is what
    lets the queue drain at all. What this test is about is the two marks
    either side of it.
    """
    channel, dispatched, client = _channel(mid_turn=MID_TURN_QUEUE)
    first_ts = "1710000021.000100"
    held_ts = "1710000021.000200"

    await _post(channel, _dm_event(ts=first_ts), "EvQueueFirst")
    await _post(channel, _dm_event(ts=held_ts, text="and the docs"), "EvQueueHeld")

    assert client.added == [
        (DM, first_ts, ACKNOWLEDGED),
        (DM, held_ts, ACKNOWLEDGED),
        (DM, held_ts, QUEUED),
    ]
    assert client.removed == []

    await channel.send(_ending(dispatched[0], content=""))

    assert client.removed == [
        (DM, first_ts, ACKNOWLEDGED),
        (DM, held_ts, QUEUED),
    ]
    assert client.added == [
        (DM, first_ts, ACKNOWLEDGED),
        (DM, held_ts, ACKNOWLEDGED),
        (DM, held_ts, QUEUED),
        (DM, first_ts, COMPLETED),
    ]
