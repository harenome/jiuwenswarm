# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.

"""The thread status must not contradict the reaction sitting beside it.

``thinking_status`` used to be the only value ``assistant.threads.setStatus``
ever wrote, so a message that was actually held behind a running turn --
marked ``queued_emoji`` on the message itself -- still had the thread claiming
"is thinking…" about it, and a message folded into a running turn under
``delivery.mid_turn: steer`` got the same lie. These tests pin the two
corrections, ``queued_status`` and ``steered_status``, alongside the existing
``thinking_status``: which of the three is showing at any moment is decided by
nothing but which one was set most recently, because
``assistant.threads.setStatus`` holds exactly one string per thread and every
call here replaces it outright. No counter, no queue-depth tracking, no
reconciliation between the three -- see ``_set_queued_status`` and
``_set_steered_status`` in ``slack_connect.py`` for the reasoning.

Ended threads are pinned once more here (already covered for the plain
thinking-status case in ``test_slack_thinking_status_clear.py``) to confirm the
same unconditional clear closes a thread that last showed ``queued_status`` or
``steered_status`` too -- there is no clear of its own for either, on purpose.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import pytest

from jiuwenswarm.common.schema.message import EventType, Message
from jiuwenswarm.gateway.channel_manager.base import RobotMessageRouter
from jiuwenswarm.gateway.channel_manager.im_platforms.slack import slack_connect
from jiuwenswarm.gateway.channel_manager.im_platforms.slack.slack_connect import (
    MID_TURN_QUEUE,
    MID_TURN_STEER,
    SlackChannel,
    SlackChannelConfig,
)

ASKER = "U0ASKER001"
SECOND = "U0OTHER002"
TEAM = "T0TESTTEAM"
DM = "D0DIRECT01"
CHANNEL = "C0CHANNEL1"

THINKING = "is thinking…"
QUEUED = "has queued a message"
STEERED = "has steered a message into the running turn"


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
def slack_logs(caplog):
    """Capture the connector's own log records.

    A jiuwenswarm logger does not propagate to the root logger caplog installs
    on, so the handler has to go on the emitting logger directly -- copied
    from ``test_slack_thinking_status_clear.py`` for the same reason it copied
    it from ``test_slack_channel.py``.
    """
    target = logging.getLogger(slack_connect.__name__)
    previous_level = target.level
    previous_propagate = target.propagate
    target.addHandler(caplog.handler)
    target.propagate = False
    target.setLevel(logging.DEBUG)
    caplog.set_level(logging.DEBUG, logger=target.name)
    try:
        yield caplog
    finally:
        target.removeHandler(caplog.handler)
        target.propagate = previous_propagate
        target.setLevel(previous_level)


def _lines_at(caplog, level: int) -> list[str]:
    return [
        record.getMessage()
        for record in caplog.records
        if record.levelno == level and record.name == slack_connect.__name__
    ]


class _RecordingSlackClient:
    """Just enough Slack to see what the status and the reactions did."""

    def __init__(self) -> None:
        self.posts: list[dict[str, Any]] = []
        self.added: list[tuple[str, str, str]] = []
        self.removed: list[tuple[str, str, str]] = []
        self.api_calls: list[dict[str, Any]] = []

    async def chat_postMessage(self, **kwargs: Any) -> dict[str, str]:
        self.posts.append(kwargs)
        return {"ts": f"1710000199.{len(self.posts):06d}"}

    async def chat_update(self, **kwargs: Any) -> dict[str, str]:
        return {"ts": kwargs.get("ts", "")}

    async def chat_postEphemeral(self, **kwargs: Any) -> dict[str, bool]:
        return {"ok": True}

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


def _status_calls(client: _RecordingSlackClient) -> list[dict[str, Any]]:
    return [
        call
        for call in client.api_calls
        if call["api_method"] == slack_connect._ASSISTANT_SET_STATUS_METHOD
    ]


def _channel(
    *, mid_turn: str = MID_TURN_QUEUE, client: Any = None, **overrides: Any
) -> tuple[SlackChannel, list[Message], Any]:
    overrides.setdefault("allow_from", [ASKER, SECOND])
    overrides.setdefault("reply_in_thread", True)
    overrides.setdefault("thinking_status", THINKING)
    overrides.setdefault("queued_status", QUEUED)
    overrides.setdefault("steered_status", STEERED)
    channel = SlackChannel(
        SlackChannelConfig(enabled=True, **overrides), RobotMessageRouter()
    )
    channel._running = True
    channel._client = client if client is not None else _RecordingSlackClient()
    channel._mid_turn_mode = lambda *_a, **_kw: mid_turn  # type: ignore[assignment]
    dispatched: list[Message] = []
    channel.on_message(dispatched.append)
    return channel, dispatched, channel._client


def _channel_event(
    *,
    user: str = ASKER,
    ts: str,
    text: str = "sweep the repos",
    thread_ts: str = "",
) -> dict:
    event: dict[str, Any] = {
        "type": "message",
        "channel_type": "channel",
        "channel": CHANNEL,
        "user": user,
        "text": text,
        "ts": ts,
    }
    if thread_ts:
        event["thread_ts"] = thread_ts
    return event


def _dm_event(*, user: str = ASKER, ts: str, text: str = "sweep the repos") -> dict:
    return {
        "type": "message",
        "channel_type": "im",
        "channel": DM,
        "user": user,
        "text": text,
        "ts": ts,
    }


async def _post_channel(channel: SlackChannel, event: dict, event_id: str) -> str:
    return await channel._handle_slack_event(
        event, {"event_id": event_id, "team_id": TEAM}, is_dm=False, trigger="mention"
    )


async def _post_dm(channel: SlackChannel, event: dict, event_id: str) -> str:
    return await channel._handle_slack_event(
        event, {"event_id": event_id, "team_id": TEAM}, is_dm=True, trigger="dm"
    )


def _ending(
    request: Message,
    *,
    channel_id: str,
    event_type: EventType = EventType.CHAT_FINAL,
    content: str = "done",
) -> Message:
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
        metadata={"slack_channel_id": channel_id},
    )


# --------------------------------------------------------------------------
# queued
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_queued_message_sets_the_queued_status() -> None:
    channel, dispatched, client = _channel(mid_turn=MID_TURN_QUEUE)
    root = "1710000070.000100"

    await _post_channel(channel, _channel_event(ts=root), "EvRoot")
    outcome = await _post_channel(
        channel,
        _channel_event(ts="1710000070.000200", thread_ts=root, text="also this"),
        "EvQueue",
    )

    assert outcome.startswith("queued:")
    statuses = [call["data"]["status"] for call in _status_calls(client)]
    # The running turn's own acknowledgement set thinking_status first; the
    # queue's own correction is what a reader sees now.
    assert statuses[-1] == QUEUED
    assert THINKING in statuses


@pytest.mark.asyncio
async def test_draining_it_sets_the_thinking_status() -> None:
    channel, dispatched, client = _channel(mid_turn=MID_TURN_QUEUE)
    root = "1710000080.000100"

    await _post_channel(channel, _channel_event(ts=root), "EvRoot")
    await _post_channel(
        channel,
        _channel_event(ts="1710000080.000200", thread_ts=root, text="also this"),
        "EvQueue",
    )
    assert [c["data"]["status"] for c in _status_calls(client)][-1] == QUEUED
    assert len(dispatched) == 1

    await channel.send(_ending(dispatched[0], channel_id=CHANNEL))

    # The drain dispatches the held message, which is the turn now running.
    assert len(dispatched) == 2
    statuses = [c["data"]["status"] for c in _status_calls(client)]
    assert statuses[-1] == THINKING


# --------------------------------------------------------------------------
# steered
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_steered_message_sets_the_steered_status() -> None:
    channel, dispatched, client = _channel(mid_turn=MID_TURN_STEER)
    root = "1710000060.000100"

    await _post_channel(channel, _channel_event(ts=root), "EvRoot")
    outcome = await _post_channel(
        channel,
        _channel_event(
            ts="1710000060.000200", thread_ts=root, text="also check the logs"
        ),
        "EvSteer",
    )

    assert outcome.startswith("steered:")
    statuses = [call["data"]["status"] for call in _status_calls(client)]
    assert statuses[-1] == STEERED
    # Two requests reach the callback -- the root turn and the steer that
    # joined it -- but only one turn-initiator entry exists: a steer records
    # no entry of its own, which is what keeps the stop floor with whoever
    # started the turn. See MID_TURN_STEER in slack_connect.py.
    assert len(dispatched) == 2


# --------------------------------------------------------------------------
# last event wins
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_last_event_wins_across_two_messages_in_one_thread() -> None:
    """No reconciliation: the thread shows the latest event, not a summary.

    The turn started by the root message is still genuinely running when the
    second message queues behind it -- and the thread stops saying so anyway,
    because the queue's correction is the more recent of the two calls. That
    is the design, not a bug the read-back would need to paper over.
    """
    channel, dispatched, client = _channel(mid_turn=MID_TURN_QUEUE)
    root = "1710000040.000100"

    await _post_channel(channel, _channel_event(ts=root), "EvRoot")
    await _post_channel(
        channel,
        _channel_event(ts="1710000040.000200", thread_ts=root, text="also do this"),
        "EvQueued",
    )

    statuses = [call["data"]["status"] for call in _status_calls(client)]
    assert statuses == [THINKING, THINKING, QUEUED]


# --------------------------------------------------------------------------
# thread coordinates
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_root_level_message_and_an_in_thread_message_both_get_a_status() -> (
    None
):
    """A root message opens its own thread; a reply uses the one it joined."""
    channel, dispatched, client = _channel(mid_turn=MID_TURN_QUEUE)
    root_ts = "1710000050.000100"

    await _post_channel(channel, _channel_event(ts=root_ts), "EvRoot")
    assert _status_calls(client)[-1]["data"]["thread_ts"] == root_ts

    existing_thread = "1710000049.000900"
    reply_ts = "1710000050.000200"
    await _post_channel(
        channel,
        _channel_event(ts=reply_ts, thread_ts=existing_thread, text="a fresh ask"),
        "EvReply",
    )

    # The status is set on the thread the reply joined, not on the reply's
    # own timestamp.
    assert _status_calls(client)[-1]["data"]["thread_ts"] == existing_thread


@pytest.mark.asyncio
async def test_a_dm_with_no_thread_makes_no_call() -> None:
    """A DM answered at the top level has nowhere to put any of the three."""
    channel, dispatched, client = _channel(mid_turn=MID_TURN_QUEUE)

    await _post_dm(channel, _dm_event(ts="1710000090.000100"), "EvDM1")
    outcome = await _post_dm(
        channel, _dm_event(ts="1710000090.000200", text="also this"), "EvDM2"
    )

    assert outcome.startswith("queued:")
    assert _status_calls(client) == []


# --------------------------------------------------------------------------
# refusal and failure
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_refused_message_leaves_a_running_turns_status_untouched() -> None:
    """A refusal is not a status event, on either surface it could be read as.

    ``_reject_request`` marks ``rejected_emoji`` on the message and
    ``_explain_queue_refusal`` tells its sender privately why -- both scoped to
    the one sender, which is also why the status is the wrong surface for it a
    second time: the status is thread-wide, and this concerns one message.
    Nothing in the refusal path calls ``assistant.threads.setStatus``, so the
    only status call the refused message produces is the ordinary
    acknowledgement every accepted message gets before the queue is even
    checked -- and that call is still true: a turn genuinely is running
    whenever the queue can be full enough to refuse one.
    """
    channel, dispatched, client = _channel(mid_turn=MID_TURN_QUEUE)
    root = "1710000100.000100"
    await _post_channel(channel, _channel_event(ts=root), "EvRoot")
    for index in range(slack_connect._MAX_QUEUED_PER_SESSION):
        await _post_channel(
            channel,
            _channel_event(
                ts=f"1710000101.{index:06d}", thread_ts=root, text=f"held {index}"
            ),
            f"EvQ{index}",
        )
    before = len(_status_calls(client))

    outcome = await _post_channel(
        channel,
        _channel_event(ts="1710000199.000100", thread_ts=root, text="one too many"),
        "EvOver",
    )

    assert outcome.startswith("refused:queue-full")
    after = _status_calls(client)
    # Exactly one more: the refused message's own unconditional
    # acknowledgement, fired before the queue-full check ever runs. The
    # refusal itself adds nothing.
    assert len(after) == before + 1
    assert after[-1]["data"]["status"] == THINKING
    assert (CHANNEL, "1710000199.000100", channel.config.rejected_emoji) in client.added


@pytest.mark.asyncio
async def test_a_refused_setstatus_does_not_fail_the_turn(slack_logs) -> None:
    """Best effort, like the plain thinking-status setter and its clear."""

    class _RefusingStatusClient(_RecordingSlackClient):
        async def api_call(self, api_method: str, **kwargs: Any) -> dict[str, bool]:
            self.api_calls.append({"api_method": api_method, **kwargs})
            raise RuntimeError("method_not_supported")

    channel, dispatched, client = _channel(
        mid_turn=MID_TURN_QUEUE, client=_RefusingStatusClient()
    )
    root = "1710000110.000100"

    await _post_channel(channel, _channel_event(ts=root), "EvRoot")
    outcome = await _post_channel(
        channel,
        _channel_event(ts="1710000110.000200", thread_ts=root, text="also this"),
        "EvQueue",
    )

    assert outcome.startswith("queued:")
    session_id = str(dispatched[0].session_id)
    assert len(channel._queued_messages[session_id]) == 1
    assert _lines_at(slack_logs, logging.WARNING) == []
    assert (
        "Slack assistant.threads.setStatus failed: method_not_supported"
        in _lines_at(slack_logs, logging.DEBUG)
    )


# --------------------------------------------------------------------------
# the ending still clears
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_ending_still_clears_a_queued_status() -> None:
    channel, dispatched, client = _channel(mid_turn=MID_TURN_QUEUE)
    root = "1710000120.000100"

    await _post_channel(channel, _channel_event(ts=root), "EvRoot")
    await _post_channel(
        channel,
        _channel_event(ts="1710000120.000200", thread_ts=root, text="also this"),
        "EvQueue",
    )
    assert [c["data"]["status"] for c in _status_calls(client)][-1] == QUEUED

    await channel.send(_ending(dispatched[0], channel_id=CHANNEL))
    assert len(dispatched) == 2
    await channel.send(_ending(dispatched[1], channel_id=CHANNEL))

    statuses = [c["data"]["status"] for c in _status_calls(client)]
    assert statuses[-1] == ""


@pytest.mark.asyncio
async def test_the_ending_still_clears_a_steered_status() -> None:
    channel, dispatched, client = _channel(mid_turn=MID_TURN_STEER)
    root = "1710000130.000100"

    await _post_channel(channel, _channel_event(ts=root), "EvRoot")
    await _post_channel(
        channel,
        _channel_event(ts="1710000130.000200", thread_ts=root, text="and this too"),
        "EvSteer",
    )
    assert [c["data"]["status"] for c in _status_calls(client)][-1] == STEERED

    await channel.send(_ending(dispatched[0], channel_id=CHANNEL))

    statuses = [c["data"]["status"] for c in _status_calls(client)]
    assert statuses[-1] == ""
