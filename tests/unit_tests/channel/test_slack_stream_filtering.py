"""Unit tests for the events a streamed Slack turn declines to post.

A stream brings the answer and a running report of how it is being built. Only
the answer belongs in the channel; these cover which events are dropped, which
are still delivered, and that the whole filter is unreachable with streaming off.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest

from jiuwenswarm.common.schema.message import EventType, Message
from jiuwenswarm.gateway.channel_manager.base import RobotMessageRouter
from jiuwenswarm.gateway.channel_manager.im_platforms.slack import slack_connect
from jiuwenswarm.gateway.channel_manager.im_platforms.slack.slack_connect import (
    SlackChannel,
    SlackChannelConfig,
)
from jiuwenswarm.gateway.channel_manager.im_platforms.slack.slack_dedup import (
    SlackEventDedupStore,
)

_PROGRESS_EVENTS = sorted(slack_connect._INTERMEDIATE_STREAM_EVENTS, key=str)


@pytest.fixture(autouse=True)
def _fast_streaming(monkeypatch):
    """Collapse the debounce and the per-channel spacing."""
    monkeypatch.setattr(slack_connect, "_STREAM_DEBOUNCE_MS", 0)
    monkeypatch.setattr(slack_connect, "_STREAM_MIN_UPDATE_INTERVAL_SECONDS", 0.0)


class _FakeSlackClient:
    """Records posts and edits in the order they were made."""

    def __init__(self) -> None:
        self.posts: list[dict[str, Any]] = []
        self.updates: list[dict[str, Any]] = []

    async def chat_postMessage(self, **kwargs: Any) -> dict[str, str]:
        self.posts.append(kwargs)
        return {"ts": f"1710000099.{len(self.posts):06d}"}

    async def chat_update(self, **kwargs: Any) -> dict[str, str]:
        self.updates.append(kwargs)
        return {"ts": str(kwargs.get("ts") or "")}


def _channel(tmp_path, **overrides: Any) -> SlackChannel:
    settings: dict[str, Any] = {"enabled": True, "enable_streaming": True}
    settings.update(overrides)
    return SlackChannel(
        SlackChannelConfig(**settings),
        RobotMessageRouter(),
        dedup_store=SlackEventDedupStore(tmp_path / "slack_seen_events.json"),
    )


def _message(
    *,
    event_type: EventType = EventType.CHAT_DELTA,
    content: str = "",
    message_id: str = "response-1",
) -> Message:
    return Message(
        id=message_id,
        type="event",
        channel_id="slack",
        session_id="slack_T1_C1_1710000000.000100",
        params={},
        timestamp=time.time(),
        ok=True,
        payload={"content": content},
        event_type=event_type,
        metadata={"slack_channel_id": "C1"},
    )


async def _settle() -> None:
    """Let the debounced edit that a delta scheduled actually run."""
    for _ in range(5):
        await asyncio.sleep(0)


@pytest.mark.parametrize("event_type", _PROGRESS_EVENTS)
@pytest.mark.asyncio
async def test_progress_events_are_not_posted_as_messages(
    tmp_path, event_type
) -> None:
    channel = _channel(tmp_path)
    client = _FakeSlackClient()
    channel._client = client

    await channel.send(_message(event_type=event_type, content="thinking..."))

    assert client.posts == []
    assert client.updates == []


@pytest.mark.asyncio
async def test_a_streamed_turn_posts_only_its_answer(tmp_path) -> None:
    channel = _channel(tmp_path)
    client = _FakeSlackClient()
    channel._client = client

    await channel.send(_message(content="The "))
    await channel.send(
        _message(event_type=EventType.CHAT_TOOL_CALL, content="read_file(...)")
    )
    await channel.send(
        _message(event_type=EventType.CHAT_TOOL_RESULT, content="120 lines")
    )
    await channel.send(_message(content="answer."))
    await _settle()
    await channel.send(
        _message(event_type=EventType.CHAT_FINAL, content="The answer.")
    )

    # One post, opening the message the deltas stream into, and no second one
    # for either tool event. The snapshot is stripped, hence "The" not "The ".
    assert [post["text"] for post in client.posts] == ["The"]
    assert [update["text"] for update in client.updates][-1] == "The answer."


@pytest.mark.asyncio
async def test_the_answer_and_its_failures_are_still_delivered(tmp_path) -> None:
    channel = _channel(tmp_path)
    client = _FakeSlackClient()
    channel._client = client

    await channel.send(_message(event_type=EventType.CHAT_FINAL, content="done"))
    await channel.send(_message(event_type=EventType.CHAT_ERROR, content="it broke"))

    # Two messages: the answer, and the failure. The failure arrives as the
    # turn activity card rather than as bare text -- there was no card up for
    # this turn, and a message may hold a plan or the reply but not both -- so
    # what is asserted is that it arrived and that it says what went wrong.
    assert len(client.posts) == 2
    assert client.posts[0]["text"] == "done"
    assert client.posts[1]["text"] == "Turn failed - it broke"
    assert client.posts[1]["blocks"][0]["type"] == "plan"


@pytest.mark.asyncio
async def test_a_question_for_the_user_still_reaches_the_channel(tmp_path) -> None:
    # Not progress reporting: it is addressed to the user and is deliberately
    # left out of the filtered set. It reaches the channel as a message with
    # answer buttons rather than as text, because a question holds its
    # options instead of the content the text path renders.
    channel = _channel(tmp_path)
    client = _FakeSlackClient()
    channel._client = client

    question = _message(event_type=EventType.CHAT_ASK_USER_QUESTION, content="")
    question.payload = {
        "request_id": "call_1",
        "source": "permission_interrupt",
        "questions": [
            {
                "question": "Proceed?",
                "header": "",
                "options": [{"label": "Approve", "value": "approve"}],
            }
        ],
    }

    await channel.send(question)

    assert [post["text"] for post in client.posts] == ["Proceed?"]
    assert client.posts[0]["blocks"][-1]["type"] == "actions"


@pytest.mark.asyncio
async def test_reasoning_is_never_narrated_when_the_card_is_on(tmp_path) -> None:
    """The card consumes reasoning before the gate, so the gate is not needed.

    With streaming off the gate is unreachable, and reasoning would otherwise
    reach the text path and be posted as a message of its own -- the model's
    own deliberation, addressed to nobody, in the channel. The card takes it
    first, and takes it for its duration alone: none of that text reaches
    Slack by any route.
    """
    channel = _channel(tmp_path, enable_streaming=False, activity_card=True)
    client = _FakeSlackClient()
    channel._client = client

    await channel.send(
        _message(
            event_type=EventType.CHAT_REASONING,
            content="The user probably means the staging cluster, so I should",
        )
    )

    assert client.posts == []
    assert client.updates == []
    # The delayed post has not been awaited, so nothing is on screen yet either;
    # cancelling it is what a stopped channel does with a card in flight.
    await channel.stop()


@pytest.mark.asyncio
async def test_streaming_off_delivers_every_event_as_before(tmp_path) -> None:
    """The filter must be unreachable with streaming off.

    Also establishes that the filter does real work: every one of these renders
    to text and is posted once the gate is not in the way.

    The activity card is switched off so that the gate is the only thing in
    play. The card reads four of these events before the gate is reached, and
    ``chat.reasoning`` is the one it reads whatever its payload looks like --
    the others need a tool call or a todo list in theirs, which these fixtures
    deliberately do not have.
    """
    channel = _channel(tmp_path, enable_streaming=False, activity_card=False)
    client = _FakeSlackClient()
    channel._client = client

    for event_type in _PROGRESS_EVENTS:
        await channel.send(_message(event_type=event_type, content="progress"))

    assert [post["text"] for post in client.posts] == ["progress"] * len(
        _PROGRESS_EVENTS
    )
