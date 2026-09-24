# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.

"""How much of a Slack channel shares one session, and what that changes.

``delivery.session`` picks between two shapes of session id. ``thread``, the
default, keys a channel session on the thread root, so every thread is its own
conversation. ``channel`` keys it on the channel, so the threads of one room
share a history and the agent has continuity across them.

The property this file exists to pin is the one that would be silently wrong:
**widening a session must not move a reply.** Where an answer goes is settled
per message from the thread it was posted in, and the delivery ladder reads
that before it ever looks at a session id -- so a question asked in a thread is
still answered in that thread when the room is one session.

Warnings are captured by replacing the module logger's ``warning`` rather than
through ``caplog``: this project's loggers do not propagate, so ``caplog`` sees
nothing under the pytest CI runs on.
"""

from __future__ import annotations

import time
from typing import Any

import pytest

from jiuwenswarm.common.schema.message import EventType, Message
from jiuwenswarm.common.scopes import (
    MID_TURN_QUEUE,
    MID_TURN_STEER,
    SESSION_CHANNEL,
    SESSION_DEFAULT,
    SESSION_THREAD,
    compile_scopes,
)
from jiuwenswarm.gateway.channel_manager.base import RobotMessageRouter
from jiuwenswarm.gateway.channel_manager.im_platforms.slack import slack_connect
from jiuwenswarm.gateway.channel_manager.im_platforms.slack.slack_connect import (
    SlackChannel,
    SlackChannelConfig,
    apply_scopes_to_slack_overrides,
)

ASKER = "U0ASKER001"
SECOND = "U0OTHER002"
TEAM = "T0TESTTEAM"
DM = "D0DIRECT01"
ROOM = "C0CHANNEL1"
OTHER_ROOM = "C0CHANNEL2"
THREAD_A = "1710000001.000100"
THREAD_B = "1710000002.000200"


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


class _RecordingSlackClient:
    """Just enough Slack to see where a post landed and which reactions stuck."""

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


def _config(entries: Any = ()) -> SlackChannelConfig:
    """A config built the way ``app_gateway`` builds one, from written scopes."""
    scopes = compile_scopes(list(entries), warn=lambda *a: None)
    platform, per_chat = apply_scopes_to_slack_overrides(
        {"group_chat_mode": "mention"}, scopes=scopes
    )
    return SlackChannelConfig(
        enabled=True,
        allow_from=[ASKER, SECOND],
        group_chat_mode="mention",
        conversation_overrides=per_chat,
        platform_override=platform,
        scopes=scopes,
    )


def _channel(entries: Any = ()) -> tuple[SlackChannel, list[Message]]:
    channel = SlackChannel(_config(entries), RobotMessageRouter())
    channel._running = True
    channel._client = _RecordingSlackClient()
    received: list[Message] = []
    channel.on_message(received.append)
    return channel, received


def _room_event(*, user: str = ASKER, ts: str, thread: str, text: str = "keep going") -> dict:
    return {
        "type": "message",
        "channel_type": "channel",
        "channel": ROOM,
        "user": user,
        "text": text,
        "ts": ts,
        "thread_ts": thread,
    }


def _dm_event(*, user: str = ASKER, ts: str, text: str = "sweep the repos") -> dict:
    return {
        "type": "message",
        "channel_type": "im",
        "channel": DM,
        "user": user,
        "text": text,
        "ts": ts,
    }


async def _post(channel: SlackChannel, event: dict, event_id: str, *, is_dm: bool = False) -> str:
    return await channel._handle_slack_event(
        event,
        {"event_id": event_id, "team_id": TEAM},
        is_dm=is_dm,
        trigger="dm" if is_dm else "mention",
    )


def _reply_to(request: Message, *, content: str = "done") -> Message:
    """The answer to ``request``, addressed the way the request addressed itself.

    Holds the request's own id, which is what closes that turn, and the
    request's own metadata, which is where the connector stamped the channel
    and the thread this message is to be answered in.
    """
    return Message(
        id=request.id,
        type="event",
        channel_id="slack",
        session_id=request.session_id,
        params={},
        timestamp=time.time(),
        ok=True,
        payload={"event_type": EventType.CHAT_FINAL.value, "content": content},
        event_type=EventType.CHAT_FINAL,
        metadata=dict(request.metadata or {}),
    )


_CHANNEL_WIDE = (
    {
        "match": {"channel": "slack", "chat": ROOM},
        "delivery": {"session": SESSION_CHANNEL},
    },
)


# --------------------------------------------------------------------------
# The default, and the ids it produces
# --------------------------------------------------------------------------


def test_a_conversation_nobody_wrote_a_rule_for_is_keyed_on_its_thread():
    channel, _ = _channel()

    assert channel._session_scope(ROOM, ASKER) == SESSION_DEFAULT
    assert SESSION_DEFAULT == SESSION_THREAD


@pytest.mark.asyncio
async def test_by_default_two_threads_of_one_room_are_two_sessions():
    channel, received = _channel()

    await _post(channel, _room_event(ts="1710000010.000100", thread=THREAD_A), "Ev01")
    await _post(channel, _room_event(ts="1710000011.000100", thread=THREAD_B), "Ev02")

    # The ids are spelled out rather than compared, because "unchanged" is the
    # whole claim: a deployment that writes nothing gets the string it already
    # had, character for character.
    assert received[0].session_id == f"slack_{TEAM}_{ROOM}_{THREAD_A}"
    assert received[1].session_id == f"slack_{TEAM}_{ROOM}_{THREAD_B}"


@pytest.mark.asyncio
async def test_a_dm_is_keyed_on_the_person_whatever_the_setting_says():
    # The rule names the platform, which is how a deployment asks for one
    # session per channel everywhere -- and a DM is not a channel with threads
    # to group. It is already one session for the whole conversation, keyed on
    # the person, so the setting has nothing left to do and is ignored rather
    # than refused. Rekeying it would silently start every DM over.
    channel, received = _channel(
        [{"match": {"channel": "slack"}, "delivery": {"session": SESSION_CHANNEL}}]
    )

    await _post(channel, _dm_event(ts="1710000010.000100"), "Ev01", is_dm=True)

    assert received[0].session_id == f"slack_{TEAM}_{DM}_{ASKER}"


@pytest.mark.asyncio
async def test_a_dm_under_the_widened_setting_warns_about_nothing(warnings):
    channel, _ = _channel(
        [{"match": {"channel": "slack"}, "delivery": {"session": SESSION_CHANNEL}}]
    )

    await _post(channel, _dm_event(ts="1710000010.000100"), "Ev01", is_dm=True)

    assert warnings == []


# --------------------------------------------------------------------------
# A session is never shared between workspaces
# --------------------------------------------------------------------------


def test_a_session_id_names_the_workspace_it_belongs_to():
    assert (
        SlackChannel._session_id(
            team_id=TEAM,
            channel_id=ROOM,
            user_id=ASKER,
            root_thread_ts=THREAD_A,
            is_dm=False,
        )
        == f"slack_{TEAM}_{ROOM}_{THREAD_A}"
    )


def test_no_workspace_yields_no_session_rather_than_a_shared_name():
    """It used to spell an absent team "default", which two installs would share.

    Sharing that bucket means one workspace's conversation is answered out of
    another's history, and their turns collide in every map keyed on the string.
    There is no id that is safe to share here, so there is no id.
    """
    for is_dm, channel_wide in ((False, False), (False, True), (True, False)):
        assert (
            SlackChannel._session_id(
                team_id="",
                channel_id=ROOM,
                user_id=ASKER,
                root_thread_ts=THREAD_A,
                is_dm=is_dm,
                channel_wide=channel_wide,
            )
            == ""
        )
    assert (
        SlackChannel._session_id(
            team_id="   ",
            channel_id=ROOM,
            user_id=ASKER,
            root_thread_ts=THREAD_A,
            is_dm=False,
        )
        == ""
    )


@pytest.mark.asyncio
async def test_an_event_with_no_workspace_is_dropped_before_it_is_acknowledged():
    channel, received = _channel()

    outcome = await channel._handle_slack_event(
        _room_event(ts="1710000010.000100", thread=THREAD_A),
        {"event_id": "Ev-NO-TEAM"},
        is_dm=False,
        trigger="mention",
    )

    assert outcome == "dropped:no-team-on-event"
    assert received == []
    # Nothing was said about it either: no reaction, no reply.
    assert channel._client.added == []
    assert channel._client.posts == []


# --------------------------------------------------------------------------
# One session for the room
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_channel_scoped_room_gives_two_threads_one_session():
    channel, received = _channel(_CHANNEL_WIDE)

    await _post(channel, _room_event(ts="1710000010.000100", thread=THREAD_A), "Ev01")
    await channel.send(_reply_to(received[0]))
    await _post(
        channel,
        _room_event(user=SECOND, ts="1710000011.000100", thread=THREAD_B),
        "Ev02",
    )

    assert received[0].session_id == f"slack_{TEAM}_{ROOM}"
    assert received[1].session_id == received[0].session_id


@pytest.mark.asyncio
async def test_a_thread_waits_for_a_turn_another_thread_started():
    """What one session for the room costs, said out loud.

    Everything keyed on a session is now the room's: the turn in flight, and
    the queue behind it. A message in a thread where nothing is happening waits
    for work started somewhere the sender may never have read. Under the
    default it would have run at once.
    """
    channel, received = _channel(_CHANNEL_WIDE)

    await _post(channel, _room_event(ts="1710000010.000100", thread=THREAD_A), "Ev01")
    outcome = await _post(
        channel,
        _room_event(user=SECOND, ts="1710000011.000100", thread=THREAD_B),
        "Ev02",
    )

    assert outcome.startswith("queued:")
    assert len(received) == 1

    # And it runs, in its own thread, once the first turn ends.
    await channel.send(_reply_to(received[0]))
    assert len(received) == 2
    assert received[1].metadata["slack_thread_ts"] == THREAD_B


@pytest.mark.asyncio
async def test_a_room_nobody_named_keeps_its_own_threads():
    # Per conversation, which is what makes this a scope rather than a
    # connector setting: the rule names one room and every other one is
    # untouched.
    channel, received = _channel(_CHANNEL_WIDE)

    assert channel._session_scope(ROOM, ASKER) == SESSION_CHANNEL
    assert channel._session_scope(OTHER_ROOM, ASKER) == SESSION_THREAD


def test_a_conversation_rule_beats_a_platform_rule():
    channel, _ = _channel(
        [
            {"match": {"channel": "slack"}, "delivery": {"session": SESSION_CHANNEL}},
            {
                "match": {"channel": "slack", "chat": ROOM},
                "delivery": {"session": SESSION_THREAD},
            },
        ]
    )

    assert channel._session_scope(ROOM, ASKER) == SESSION_THREAD
    assert channel._session_scope(OTHER_ROOM, ASKER) == SESSION_CHANNEL


# --------------------------------------------------------------------------
# Where the answer goes, which the setting must not move
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_message_in_a_thread_is_answered_in_that_thread():
    """The one thing a widened session must not change.

    Two threads, one session, and each answer goes back where its question was
    asked. The reply target is settled per message from the thread it arrived
    in and stamped on the request; the session id is not asked.
    """
    channel, received = _channel(_CHANNEL_WIDE)

    await _post(channel, _room_event(ts="1710000010.000100", thread=THREAD_A), "Ev01")
    await channel.send(_reply_to(received[0], content="about A"))
    await _post(channel, _room_event(ts="1710000011.000100", thread=THREAD_B), "Ev02")
    await channel.send(_reply_to(received[1], content="about B"))

    assert received[0].session_id == received[1].session_id
    assert received[0].metadata["slack_thread_ts"] == THREAD_A
    assert received[1].metadata["slack_thread_ts"] == THREAD_B

    posts = [
        post for post in channel._client.posts if post.get("text") in ("about A", "about B")
    ]
    assert [(post["channel"], post.get("thread_ts")) for post in posts] == [
        (ROOM, THREAD_A),
        (ROOM, THREAD_B),
    ]


@pytest.mark.asyncio
async def test_the_session_id_alone_would_have_said_the_channel_root():
    """Why the previous test is not a tautology.

    A channel-wide session id names no thread, so the rung of the delivery
    ladder that reads one would answer "the channel root" for both threads. It
    is never reached: the request's own metadata is read first and holds the
    thread the message arrived in.
    """
    channel, received = _channel(_CHANNEL_WIDE)

    await _post(channel, _room_event(ts="1710000010.000100", thread=THREAD_A), "Ev01")
    session_id = received[0].session_id

    assert SlackChannel.resolve_delivery(session_id=session_id) == (ROOM, "")
    assert channel._extract_delivery(_reply_to(received[0]), None) == (ROOM, THREAD_A)


# --------------------------------------------------------------------------
# steer, which a channel-wide session cannot honour
# --------------------------------------------------------------------------


def test_steer_is_permitted_while_each_thread_is_its_own_session():
    channel, _ = _channel(
        [
            {
                "match": {"channel": "slack", "chat": ROOM},
                "delivery": {"mid_turn": MID_TURN_STEER},
            }
        ]
    )

    assert channel._mid_turn_mode(ROOM, ASKER) == MID_TURN_STEER


def test_steer_is_refused_when_the_whole_room_is_one_session(warnings):
    channel, _ = _channel(
        [
            {
                "match": {"channel": "slack", "chat": ROOM},
                "delivery": {"mid_turn": MID_TURN_STEER, "session": SESSION_CHANNEL},
            }
        ]
    )

    # Settled to the default rather than to some third behaviour, which is what
    # every other refusal in this design does.
    assert channel._mid_turn_mode(ROOM, ASKER, session_is_channel_wide=True) == MID_TURN_QUEUE
    assert any("cannot both hold" in line for line in warnings), warnings
    assert any(ROOM in line for line in warnings), warnings


def test_the_refusal_is_said_once_rather_than_once_per_message(warnings):
    channel, _ = _channel(
        [
            {
                "match": {"channel": "slack", "chat": ROOM},
                "delivery": {"mid_turn": MID_TURN_STEER, "session": SESSION_CHANNEL},
            }
        ]
    )

    for _ in range(5):
        channel._mid_turn_mode(ROOM, ASKER, session_is_channel_wide=True)

    assert len([line for line in warnings if "cannot both hold" in line]) == 1


@pytest.mark.asyncio
async def test_a_second_thread_queues_instead_of_steering_the_first():
    """The refusal reaching the dispatch path.

    Under one session for the room the running turn was started from another
    thread, and a steer's answer would appear there rather than where it was
    asked. The message takes a turn of its own instead, which is answered in
    its own thread.
    """
    channel, received = _channel(
        [
            {
                "match": {"channel": "slack", "chat": ROOM},
                "delivery": {"mid_turn": MID_TURN_STEER, "session": SESSION_CHANNEL},
            }
        ]
    )

    first = await _post(
        channel, _room_event(ts="1710000010.000100", thread=THREAD_A), "Ev01"
    )
    second = await _post(
        channel,
        _room_event(user=SECOND, ts="1710000011.000100", thread=THREAD_B),
        "Ev02",
    )

    assert first.startswith("dispatched:")
    assert second.startswith("queued:")
    assert len(received) == 1
    assert "input_mode" not in (received[0].params or {})


@pytest.mark.asyncio
async def test_a_second_message_in_the_same_thread_still_steers():
    """The refusal is about the widened session and nothing else.

    The same configuration without the widening steers, which is what says the
    branch above turns on the session shape rather than on ``mid_turn``.
    """
    channel, received = _channel(
        [
            {
                "match": {"channel": "slack", "chat": ROOM},
                "delivery": {"mid_turn": MID_TURN_STEER},
            }
        ]
    )

    await _post(channel, _room_event(ts="1710000010.000100", thread=THREAD_A), "Ev01")
    outcome = await _post(
        channel,
        _room_event(ts="1710000011.000100", thread=THREAD_A, text="and skip the vendored ones"),
        "Ev02",
    )

    assert outcome.startswith("steered:")
    assert received[-1].params["input_mode"] == MID_TURN_STEER


# --------------------------------------------------------------------------
# What the operator is shown
# --------------------------------------------------------------------------


def test_the_startup_summary_names_a_room_that_departs_from_the_norm():
    config = _config(_CHANNEL_WIDE)

    summary = slack_connect.describe_configured_channels(config)

    assert f"session={SESSION_CHANNEL}" in summary
    assert ROOM in summary


def test_the_summary_says_nothing_where_no_layer_settled_it():
    config = _config(
        [{"match": {"channel": "slack", "chat": ROOM}, "delivery": {"prompt": "be terse"}}]
    )

    summary = slack_connect.describe_configured_channels(config)

    # Unlike mode, prompt and model, this one answers "does this conversation
    # depart from the norm", so an unset key is absent rather than filled in.
    assert "session=" not in summary
