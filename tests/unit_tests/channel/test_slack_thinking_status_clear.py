# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.

"""The thinking status is opened at acknowledgement and closed at the ending.

Before this, nothing ever cleared it: the docstring on ``_set_thinking_status``
assumed the caller's own reply would drop it, or failing that Slack's own
two-minute timeout would. ``delivery.reply: optional`` falsified the first half
-- a turn that writes the ``NO_REPLY`` sentinel posts nothing into the thread,
so nothing clears the status -- and the second half, while true, is a delay
nobody wants standing beside a turn that has already finished.

These tests pin ``_clear_thinking_status``, called from every point a turn can
end: the terminal event in ``send()``, a stop, and a supersede. All three routes
are covered because none of them reaches ``send()``'s terminal branch for a
stopped or superseded turn -- see ``test_slack_turn_marks.py``, which pins the
same three routes for the reaction mark this status clear is a sibling to.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import pytest

from jiuwenswarm.common.schema.message import EventType, Message
from jiuwenswarm.common.scopes import MID_TURN_CANCEL
from jiuwenswarm.gateway.channel_manager.base import RobotMessageRouter
from jiuwenswarm.gateway.channel_manager.im_platforms.slack import slack_connect
from jiuwenswarm.gateway.channel_manager.im_platforms.slack.slack_connect import (
    SlackChannel,
    SlackChannelConfig,
)

ASKER = "U0ASKER001"
SECOND = "U0OTHER002"
TEAM = "T0TESTTEAM"
DM = "D0DIRECT01"
CHANNEL = "C0CHANNEL1"

THINKING = "is thinking…"


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

    Copied from ``test_slack_channel.py`` rather than shared, because there is
    no ``conftest.py`` in this directory to hold it: a jiuwenswarm logger does
    not propagate to the root logger caplog installs on, so the handler has to
    go on the emitting logger directly.
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
        return {"ts": f"1710000099.{len(self.posts):06d}"}

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
    *, mid_turn: str = MID_TURN_CANCEL, client: Any = None, **overrides: Any
) -> tuple[SlackChannel, list[Message], Any]:
    overrides.setdefault("allow_from", [ASKER, SECOND])
    overrides.setdefault("reply_in_thread", True)
    overrides.setdefault("thinking_status", THINKING)
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
    """A channel message, optionally a reply within an existing thread.

    ``root_thread_ts`` falls back to the message's own ``ts`` when the event
    carries no ``thread_ts`` of its own, so with ``reply_in_thread`` on, this is
    also the thread the thinking status opens in -- ``message_ts`` and the
    status's ``thread_ts`` coincide for a root message, which is the common
    case. A second message wanting to supersede the first has to name the
    first's ``ts`` as its own ``thread_ts``: two root messages open two
    threads and so two sessions, and neither supersedes the other.
    """
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
    """A direct message answered at the top of the conversation.

    No ``thread_ts``, so there is nowhere for the status to open -- the same
    case ``test_slack_channel.py`` covers for the setter, pinned here for the
    clear.
    """
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
    """The turn's terminal event, keyed to the request the connector dispatched.

    Built from the dispatched request rather than from literals, the same
    reasoning ``test_slack_turn_marks.py``'s ``_ending`` documents: matching the
    id and the session is the whole of what the terminal branch checks.
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
        metadata={"slack_channel_id": channel_id},
    )


# --------------------------------------------------------------------------
# A turn that reached its terminal event
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_silent_turn_clears_the_thinking_status() -> None:
    """The case the fix exists for: a turn that posts nothing still clears it.

    Asserted alongside an empty ``posts``, so "cleared" cannot pass by way of a
    reply nobody asked for landing in the thread and dropping the status by
    Slack's own side effect instead of by this connector's own call.
    """
    channel, dispatched, client = _channel()
    ts = "1710000030.000100"

    await _post_channel(channel, _channel_event(ts=ts), "EvSilent")
    assert _status_calls(client) == [
        {
            "api_method": "assistant.threads.setStatus",
            "data": {"channel_id": CHANNEL, "thread_ts": ts, "status": THINKING},
        }
    ]

    await channel.send(_ending(dispatched[0], channel_id=CHANNEL, content=""))

    assert client.posts == []
    assert _status_calls(client) == [
        {
            "api_method": "assistant.threads.setStatus",
            "data": {"channel_id": CHANNEL, "thread_ts": ts, "status": THINKING},
        },
        {
            "api_method": "assistant.threads.setStatus",
            "data": {"channel_id": CHANNEL, "thread_ts": ts, "status": ""},
        },
    ]


@pytest.mark.asyncio
async def test_a_replying_turn_also_clears_the_thinking_status() -> None:
    """Clearing is unconditional: a turn that answered clears it too.

    Slack is expected to have already dropped the status once the reply landed
    in the thread, so this call is a refused-or-harmless no-op against Slack --
    but it is issued all the same, because branching on whether a reply went
    out would re-derive the delivery decision ``send()`` has already made.
    """
    channel, dispatched, client = _channel()
    ts = "1710000030.000200"

    await _post_channel(channel, _channel_event(ts=ts), "EvReplied")
    await channel.send(_ending(dispatched[0], channel_id=CHANNEL, content="done"))

    assert len(client.posts) == 1
    statuses = [call["data"]["status"] for call in _status_calls(client)]
    assert statuses == [THINKING, ""]


@pytest.mark.asyncio
async def test_a_failed_turn_clears_the_thinking_status() -> None:
    channel, dispatched, client = _channel()
    ts = "1710000030.000300"

    await _post_channel(channel, _channel_event(ts=ts), "EvBroke")
    await channel.send(
        _ending(
            dispatched[0],
            channel_id=CHANNEL,
            event_type=EventType.CHAT_ERROR,
            content="the run died",
        )
    )

    statuses = [call["data"]["status"] for call in _status_calls(client)]
    assert statuses == [THINKING, ""]


@pytest.mark.asyncio
async def test_a_dm_with_no_thread_makes_no_status_call_at_all() -> None:
    """A DM answered at top level never had a status to clear.

    No fallback to the channel or the message's own ``ts``: with nothing
    recorded, ``_clear_thinking_status`` is guarded the same way the setter is
    and makes no call.
    """
    channel, dispatched, client = _channel()
    ts = "1710000030.000400"

    await _post_dm(channel, _dm_event(ts=ts), "EvDM")
    assert _status_calls(client) == []

    await channel.send(_ending(dispatched[0], channel_id=DM, content="done"))

    assert _status_calls(client) == []


@pytest.mark.asyncio
async def test_runtime_accepted_only_end_of_stream_leaves_the_running_status() -> None:
    """``runtime.accepted`` is not the turn ending, so its status is untouched.

    Mirrors ``test_an_acknowledgement_only_ending_leaves_the_running_turn_marked``
    in ``test_slack_turn_marks.py`` for the reaction: the turn this status
    belongs to is still running, and clearing on this event would tell the
    reader work that has not started is already done.
    """
    channel, dispatched, client = _channel()
    ts = "1710000030.000500"

    await _post_channel(channel, _channel_event(ts=ts), "EvAckOnly")
    request_id = str(dispatched[0].id)
    accepted = _ending(dispatched[0], channel_id=CHANNEL)
    accepted.event_type = EventType.RUNTIME_ACCEPTED
    accepted.payload = {"event_type": "runtime.accepted", "request_id": request_id}

    await channel.send(accepted)

    statuses = [call["data"]["status"] for call in _status_calls(client)]
    assert statuses == [THINKING]


@pytest.mark.asyncio
async def test_a_refused_clear_does_not_fail_the_turn(slack_logs) -> None:
    """Best effort, like the setter: a missing method must not break delivery."""

    class _RefusingStatusClient(_RecordingSlackClient):
        async def api_call(self, api_method: str, **kwargs: Any) -> dict[str, bool]:
            self.api_calls.append({"api_method": api_method, **kwargs})
            raise RuntimeError("method_not_supported")

    channel, dispatched, client = _channel(client=_RefusingStatusClient())
    ts = "1710000030.000600"

    await _post_channel(channel, _channel_event(ts=ts), "EvBoom")
    await channel.send(_ending(dispatched[0], channel_id=CHANNEL, content="done"))

    assert len(client.posts) == 1
    assert len(_status_calls(client)) == 2
    assert _lines_at(slack_logs, logging.WARNING) == []
    assert (
        "Slack assistant.threads.setStatus (clear) failed: method_not_supported"
        in _lines_at(slack_logs, logging.DEBUG)
    )


# --------------------------------------------------------------------------
# A turn that was destroyed and sends no ending of its own
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_stopped_turn_clears_the_thinking_status() -> None:
    """A stopped turn reaches no terminal event, and reaches no clear either --

    unless the stop path closes the status itself, the same way it already
    writes the reaction ending. Driven through ``_release_stopped_turn`` for the
    same reason ``test_slack_turn_marks.py`` does: it is the seam the stop
    handler reaches for, and the seam that holds the coordinates to clear.
    """
    channel, dispatched, client = _channel()
    ts = "1710000030.000700"

    await _post_channel(channel, _channel_event(ts=ts), "EvStopped")
    request = dispatched[0]

    await channel._release_stopped_turn(str(request.session_id), str(request.id))

    statuses = [call["data"]["status"] for call in _status_calls(client)]
    assert statuses == [THINKING, ""]


@pytest.mark.asyncio
async def test_a_superseded_turn_clears_the_thinking_status() -> None:
    """The commoner halt: a second message cancels the first turn outright.

    The second message replies inside the thread the first opened, so the two
    share a session -- a second root message would open a thread, and so a
    session, of its own, and never supersede anything. That also makes this the
    test for the coordinate ``_mark_turn_ended`` does not need and this clear
    does: the superseded turn's own message ts (``first_ts``, where its
    reaction sits) is not the thread ts its status was opened in (also
    ``first_ts`` here because it was itself a root message), and a clear
    written to the wrong one -- the second message's own ``second_ts`` -- would
    leave the first status standing while appearing to have closed it.
    """
    channel, dispatched, client = _channel(mid_turn=MID_TURN_CANCEL)
    first_ts = "1710000030.000800"
    second_ts = "1710000030.000900"

    await _post_channel(channel, _channel_event(ts=first_ts), "EvSupersededFirst")
    await _post_channel(
        channel,
        _channel_event(
            ts=second_ts, text="no, the other one", thread_ts=first_ts
        ),
        "EvSupersededSecond",
    )

    calls = _status_calls(client)
    assert calls == [
        {
            "api_method": "assistant.threads.setStatus",
            "data": {
                "channel_id": CHANNEL,
                "thread_ts": first_ts,
                "status": THINKING,
            },
        },
        {
            "api_method": "assistant.threads.setStatus",
            "data": {
                "channel_id": CHANNEL,
                "thread_ts": first_ts,
                "status": THINKING,
            },
        },
        {
            "api_method": "assistant.threads.setStatus",
            "data": {"channel_id": CHANNEL, "thread_ts": first_ts, "status": ""},
        },
    ]
    # The second message's own reaction sits on its own ts, not the thread's --
    # confirming the clear above used the initiator's ``thread_ts`` and not a
    # coordinate borrowed from wherever the reaction landed.
    assert (CHANNEL, second_ts, "eyes") in client.added
