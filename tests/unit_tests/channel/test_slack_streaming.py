"""Unit tests for Slack replies shown while they are still being written.

Two modes, and they are not two implementations of one behaviour. ``edit``
posts a message and rewrites it with chat.update; ``stream`` opens a Slack
stream and appends to it. They clamp differently, they put a table in a
different place, and only one of them can be stopped by the reader. Every test
below says which one it is exercising, and ``off`` -- the default -- is the
third mode and is covered too, because a deployment that never set the key must
be unaffected by either.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import pytest

from jiuwenswarm.common.schema.message import EventType, Message
from jiuwenswarm.gateway.channel_manager.base import RobotMessageRouter
from jiuwenswarm.gateway.channel_manager.im_platforms.slack import slack_connect
from jiuwenswarm.gateway.channel_manager.im_platforms.slack.slack_connect import (
    SlackChannel,
    SlackChannelConfig,
    SlackDeliveryError,
)
from jiuwenswarm.gateway.channel_manager.im_platforms.slack.slack_dedup import (
    SlackEventDedupStore,
)


@pytest.fixture(autouse=True)
def _fast_streaming(monkeypatch):
    """Collapse the debounce and the per-channel spacing.

    Both are wall-clock waits sized for Slack's rate limit; the tests assert what
    is written, not how long it took to write it.
    """
    monkeypatch.setattr(slack_connect, "_STREAM_DEBOUNCE_MS", 0)
    monkeypatch.setattr(slack_connect, "_STREAM_MIN_UPDATE_INTERVAL_SECONDS", 0.0)
    monkeypatch.setattr(slack_connect, "_STREAM_APPEND_DEBOUNCE_MS", 0)
    monkeypatch.setattr(slack_connect, "_STREAM_APPEND_MIN_INTERVAL_SECONDS", 0.0)


class _FakeSlackClient:
    """Records posts and edits in the order they were made."""

    def __init__(self, *, update_error: Exception | None = None) -> None:
        self.posts: list[dict[str, Any]] = []
        self.updates: list[dict[str, Any]] = []
        self._update_error = update_error

    async def chat_postMessage(self, **kwargs: Any) -> dict[str, str]:
        self.posts.append(kwargs)
        return {"ts": f"1710000099.{len(self.posts):06d}"}

    async def chat_update(self, **kwargs: Any) -> dict[str, str]:
        if self._update_error is not None:
            raise self._update_error
        self.updates.append(kwargs)
        return {"ts": str(kwargs.get("ts") or "")}


def _channel(tmp_path, **overrides: Any) -> SlackChannel:
    settings: dict[str, Any] = {"enabled": True, "enable_streaming": True}
    settings.update(overrides)
    config = SlackChannelConfig(**settings)
    return SlackChannel(
        config,
        RobotMessageRouter(),
        dedup_store=SlackEventDedupStore(tmp_path / "slack_seen_events.json"),
    )


def _message(
    *,
    event_type: EventType = EventType.CHAT_DELTA,
    content: str = "",
    payload: dict[str, Any] | None = None,
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
        payload=payload if payload is not None else {"content": content},
        event_type=event_type,
        metadata={"slack_channel_id": "C1", "slack_thread_ts": "1710000000.000100"},
    )


async def _settle() -> None:
    """Let the debounced flush tasks run."""
    for _ in range(10):
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_deltas_post_once_then_edit_that_message(tmp_path) -> None:
    channel = _channel(tmp_path)
    client = _FakeSlackClient()
    channel._client = client

    await channel.send(_message(content="Hello"))
    await channel.send(_message(content=" world"))
    await _settle()

    # One message, in the request's thread, containing the first fragment.
    assert client.posts == [
        {
            "channel": "C1",
            "text": "Hello",
            "thread_ts": "1710000000.000100",
        }
    ]
    # The edit holds the whole snapshot, not the increment, and joins the two
    # fragments without eating the space between them.
    assert [update["text"] for update in client.updates] == ["Hello world"]
    assert client.updates[0]["ts"] == "1710000099.000001"
    assert "thread_ts" not in client.updates[0]


@pytest.mark.asyncio
async def test_the_final_reply_edits_the_streamed_message_instead_of_posting(
    tmp_path,
) -> None:
    channel = _channel(tmp_path)
    client = _FakeSlackClient()
    channel._client = client

    await channel.send(_message(content="Hello"))
    await _settle()
    await channel.send(
        _message(event_type=EventType.CHAT_FINAL, content="Hello world, in full.")
    )

    assert len(client.posts) == 1
    assert client.updates[-1] == {
        "channel": "C1",
        "text": "Hello world, in full.",
        "ts": "1710000099.000001",
    }
    # The stream is forgotten, so a second reply cannot edit the first one.
    assert channel._streams == {}


@pytest.mark.asyncio
async def test_a_failed_closing_edit_still_raises(tmp_path) -> None:
    """Streaming is best effort; the reply landing is not."""
    channel = _channel(tmp_path)
    channel._client = _FakeSlackClient(update_error=RuntimeError("edit rejected"))

    await channel.send(_message(content="Hello"))
    await _settle()

    with pytest.raises(SlackDeliveryError) as excinfo:
        await channel.send(
            _message(event_type=EventType.CHAT_FINAL, content="the whole answer")
        )

    assert "edit rejected" in str(excinfo.value)


@pytest.mark.asyncio
async def test_a_failed_intermediate_edit_is_swallowed_and_stops_the_stream(
    tmp_path,
) -> None:
    # A user deleting the streamed message must not fail the delivery, and must
    # not be re-attempted once per debounce window either.
    channel = _channel(tmp_path)
    client = _FakeSlackClient(update_error=RuntimeError("message_not_found"))
    channel._client = client

    await channel.send(_message(content="Hello"))
    await channel.send(_message(content=" world"))
    await _settle()
    await channel.send(_message(content=" again"))
    await _settle()

    key = ("response-1", "C1", "1710000000.000100")
    assert channel._streams[key].surface.failed is True

    # The reply falls back to a new message rather than editing what is gone.
    await channel.send(
        _message(event_type=EventType.CHAT_FINAL, content="the whole answer")
    )
    assert [post["text"] for post in client.posts] == ["Hello", "the whole answer"]


@pytest.mark.asyncio
async def test_the_streamed_message_is_identified_in_the_log(
    tmp_path, caplog: pytest.LogCaptureFixture
) -> None:
    """The opening post and the closing tally are the streamed message's record.

    Every write between them is DEBUG and stays that way, so a production log
    that has neither of these has no way to tell a reply that was rewritten in
    full from one whose preview was never caught up.
    """
    channel = _channel(tmp_path)
    channel._client = _FakeSlackClient()

    with caplog.at_level(logging.INFO, logger=slack_connect.logger.name):
        await channel.send(_message(content="Hello"))
        await channel.send(_message(content=" world"))
        await _settle()
        await channel.send(
            _message(event_type=EventType.CHAT_FINAL, content="Hello world, in full.")
        )

    assert "streaming opened" in caplog.text
    assert "ts=1710000099.000001" in caplog.text
    assert "preview_chars=5" in caplog.text
    # The tally names the message and how many edits it took, which is what
    # makes a preview that never advanced visible without DEBUG.
    assert "streaming closed" in caplog.text
    assert "edits=1 failed=0" in caplog.text


@pytest.mark.asyncio
async def test_an_abandoned_stream_reports_why_at_the_close(
    tmp_path, caplog: pytest.LogCaptureFixture
) -> None:
    """The one exit path that could go silent is the one that moves the reply.

    Were ``write`` to latch a bare bool and drop the error, ``_close_stream``
    would return no handle and say nothing; the reply would then post fresh,
    below an abandoned preview, with nothing in the log connecting the two.
    """
    channel = _channel(tmp_path)
    client = _FakeSlackClient(update_error=RuntimeError("message_not_found"))
    channel._client = client

    await channel.send(_message(content="Hello"))
    await channel.send(_message(content=" world"))
    await _settle()

    key = ("response-1", "C1", "1710000000.000100")
    surface = channel._streams[key].surface
    assert surface.failed is True
    # The reason is retained rather than discarded with the warning.
    assert "message_not_found" in surface.error

    with caplog.at_level(logging.WARNING, logger=slack_connect.logger.name):
        await channel.send(
            _message(event_type=EventType.CHAT_FINAL, content="the whole answer")
        )

    assert "streaming abandoned, reply posts fresh" in caplog.text
    assert "message_not_found" in caplog.text
    assert "ts=1710000099.000001" in caplog.text


@pytest.mark.asyncio
async def test_slack_storing_less_than_was_sent_is_reported(
    tmp_path, caplog: pytest.LogCaptureFixture
) -> None:
    """An ``ok: true`` that did not apply is invisible in the returned ts alone.

    Slack echoes the stored message, so the write can be checked against its own
    intent at the point of sending rather than by reading the conversation back
    afterwards.
    """

    class _TruncatingClient(_FakeSlackClient):
        async def chat_update(self, **kwargs: Any) -> dict[str, Any]:
            self.updates.append(kwargs)
            return {
                "ts": str(kwargs.get("ts") or ""),
                "message": {"text": str(kwargs.get("text") or "")[:19]},
            }

    channel = _channel(tmp_path)
    channel._client = _TruncatingClient()

    await channel.send(_message(content="Hello"))
    await _settle()
    with caplog.at_level(logging.WARNING, logger=slack_connect.logger.name):
        await channel.send(
            _message(
                event_type=EventType.CHAT_FINAL,
                content="Hello world, and a good deal more of it besides.",
            )
        )

    assert "Slack stored less text than was sent" in caplog.text
    assert "sent_chars=48" in caplog.text
    assert "stored_chars=19" in caplog.text


@pytest.mark.asyncio
async def test_a_faithful_echo_is_not_reported(
    tmp_path, caplog: pytest.LogCaptureFixture
) -> None:
    """Slack normalises what it stores, and every rewrite it makes lengthens it.

    Comparing for equality would warn on every message holding a link, so only
    a shorter echo counts.
    """

    class _EchoingClient(_FakeSlackClient):
        async def chat_update(self, **kwargs: Any) -> dict[str, Any]:
            self.updates.append(kwargs)
            text = str(kwargs.get("text") or "")
            # Stands in for mrkdwn autolinking, which only ever adds characters.
            return {"ts": str(kwargs.get("ts") or ""), "message": {"text": text + "!"}}

    channel = _channel(tmp_path)
    channel._client = _EchoingClient()

    await channel.send(_message(content="Hello"))
    await _settle()
    with caplog.at_level(logging.WARNING, logger=slack_connect.logger.name):
        await channel.send(
            _message(event_type=EventType.CHAT_FINAL, content="Hello world.")
        )

    assert "stored less text" not in caplog.text


@pytest.mark.asyncio
async def test_a_stream_that_cannot_start_does_not_retry_per_fragment(
    tmp_path,
) -> None:
    class _RefusingClient(_FakeSlackClient):
        async def chat_postMessage(self, **kwargs: Any) -> dict[str, str]:
            self.posts.append(kwargs)
            raise RuntimeError("channel_not_found")

    channel = _channel(tmp_path)
    client = _RefusingClient()
    channel._client = client

    await channel.send(_message(content="Hello"))
    await channel.send(_message(content=" world"))
    await channel.send(_message(content=" again"))

    assert len(client.posts) == 1


@pytest.mark.asyncio
async def test_streaming_is_off_unless_configured(tmp_path) -> None:
    channel = _channel(tmp_path, enable_streaming=False)
    client = _FakeSlackClient()
    channel._client = client

    await channel.send(_message(content="Hello"))
    await _settle()
    await channel.send(_message(event_type=EventType.CHAT_FINAL, content="Hello"))

    assert client.updates == []
    assert [post["text"] for post in client.posts] == ["Hello"]


@pytest.mark.asyncio
async def test_reasoning_fragments_are_not_streamed(tmp_path) -> None:
    channel = _channel(tmp_path)
    client = _FakeSlackClient()
    channel._client = client

    await channel.send(
        _message(payload={"content": "hmm", "source_chunk_type": "llm_reasoning"})
    )

    assert client.posts == []


@pytest.mark.asyncio
async def test_leading_whitespace_does_not_open_a_stream(tmp_path) -> None:
    # Slack rejects an empty message, so there is nothing to post yet.
    channel = _channel(tmp_path)
    client = _FakeSlackClient()
    channel._client = client

    await channel.send(_message(content="\n "))
    await channel.send(_message(content="Hello"))
    await _settle()

    assert [post["text"] for post in client.posts] == ["Hello"]


@pytest.mark.asyncio
async def test_streamed_text_is_rendered_as_slack_mrkdwn(tmp_path) -> None:
    channel = _channel(tmp_path)
    client = _FakeSlackClient()
    channel._client = client

    await channel.send(_message(content="**bold**"))
    await channel.send(_message(content=" and more"))
    await _settle()

    assert client.posts[0]["text"] == "*bold*"
    assert client.updates[-1]["text"] == "*bold* and more"


@pytest.mark.asyncio
async def test_an_overlong_reply_keeps_its_first_chunk_in_the_streamed_message(
    tmp_path,
) -> None:
    channel = _channel(tmp_path)
    client = _FakeSlackClient()
    channel._client = client
    oversized = "x" * (slack_connect._MAX_SLACK_TEXT_LENGTH + 100)

    await channel.send(_message(content="x"))
    await _settle()
    await channel.send(_message(event_type=EventType.CHAT_FINAL, content=oversized))

    # The first chunk goes into the streamed message, so it is cut where an edit
    # will take it rather than where a post would.
    edit_limit = slack_connect._MAX_SLACK_UPDATE_TEXT_LENGTH
    assert client.updates[-1]["text"] == "x" * edit_limit
    # Everything past that is posted underneath, still at the posting limit.
    assert [post["text"] for post in client.posts[1:]] == [
        "x" * (slack_connect._MAX_SLACK_TEXT_LENGTH + 100 - edit_limit)
    ]


@pytest.mark.asyncio
async def test_a_reply_that_outgrows_an_edit_still_lands(tmp_path) -> None:
    """The observed production failure.

    An answer of a few thousand characters is one ordinary chat.postMessage, so
    nothing in the non-streaming path ever noticed it. Streamed, the same answer
    is written with chat.update, which rejects anything past 4,000 outright --
    the reply was abandoned mid-sentence and then posted again in full
    underneath, so the user saw a truncated copy followed by a whole one.
    """
    channel = _channel(tmp_path)
    client = _FakeSlackClient()
    channel._client = client
    answer = "sentence. " * 600  # 6,000 characters: one post, but two edits

    await channel.send(_message(content="Working"))
    await _settle()
    await channel.send(_message(event_type=EventType.CHAT_FINAL, content=answer))

    edit = client.updates[-1]["text"]
    assert len(edit) <= slack_connect._MAX_SLACK_UPDATE_TEXT_LENGTH
    # The remainder lands as a follow-up post instead of being lost, and the two
    # together are the whole answer rather than a truncation of it.
    remainder = [post["text"] for post in client.posts[1:]]
    assert remainder
    rejoined = " ".join([edit, *remainder])
    assert rejoined.split() == answer.split()


@pytest.mark.asyncio
async def test_a_growing_preview_stays_within_what_an_edit_accepts(tmp_path) -> None:
    channel = _channel(tmp_path)
    client = _FakeSlackClient()
    channel._client = client
    fragment = "word " * 1000  # 5,000 characters per delta

    await channel.send(_message(content=fragment))
    await _settle()
    await channel.send(_message(content=fragment))
    await _settle()

    written = [post["text"] for post in client.posts] + [
        update["text"] for update in client.updates
    ]
    assert written
    for text in written:
        assert len(text) <= slack_connect._MAX_SLACK_UPDATE_TEXT_LENGTH
    # Every one of them was over the ceiling, so every one is marked unfinished
    # and cut at a word boundary rather than through the middle of one.
    for text in written:
        assert text.endswith(slack_connect._STREAM_TRUNCATION_SUFFIX)
        assert text.removesuffix(slack_connect._STREAM_TRUNCATION_SUFFIX).endswith(
            "word"
        )


@pytest.mark.asyncio
async def test_a_preview_is_never_cut_through_a_link(tmp_path) -> None:
    """Half of a ``<url|label>`` renders as literal punctuation, not a link."""
    channel = _channel(tmp_path)
    client = _FakeSlackClient()
    channel._client = client
    link = "<https://example.invalid/very/long/path|a linked label>"
    # Padded so the ceiling falls inside the trailing link.
    padding = "filler " * 570
    await channel.send(_message(content=f"{padding}{link} and more text after it"))
    await _settle()

    preview = client.posts[0]["text"]
    assert len(preview) <= slack_connect._MAX_SLACK_UPDATE_TEXT_LENGTH
    assert preview.count("<") == preview.count(">")


@pytest.mark.asyncio
async def test_a_reply_that_was_not_streamed_keeps_the_posting_limit(tmp_path) -> None:
    """Nothing edits, so nothing is bound by the edit ceiling."""
    channel = _channel(tmp_path, enable_streaming=False)
    client = _FakeSlackClient()
    channel._client = client
    oversized = "x" * (slack_connect._MAX_SLACK_TEXT_LENGTH + 100)

    await channel.send(_message(event_type=EventType.CHAT_FINAL, content=oversized))

    assert [post["text"] for post in client.posts] == [
        "x" * slack_connect._MAX_SLACK_TEXT_LENGTH,
        "x" * 100,
    ]
    assert client.updates == []


@pytest.mark.asyncio
async def test_two_replies_in_one_channel_stream_into_their_own_messages(
    tmp_path,
) -> None:
    channel = _channel(tmp_path)
    client = _FakeSlackClient()
    channel._client = client

    await channel.send(_message(content="first", message_id="response-1"))
    await channel.send(_message(content="second", message_id="response-2"))
    await channel.send(_message(content=" more", message_id="response-1"))
    await _settle()

    assert [post["text"] for post in client.posts] == ["first", "second"]
    assert client.updates[-1] == {
        "channel": "C1",
        "text": "first more",
        "ts": "1710000099.000001",
    }


@pytest.mark.asyncio
async def test_edits_in_one_channel_are_spaced_out(tmp_path, monkeypatch) -> None:
    """The throttle is per channel: concurrent streams share Slack's budget."""
    monkeypatch.setattr(slack_connect, "_STREAM_MIN_UPDATE_INTERVAL_SECONDS", 30.0)
    channel = _channel(tmp_path)
    channel._client = _FakeSlackClient()

    await channel._wait_for_stream_slot("C1")
    assert channel._stream_next_update_at["C1"] >= time.monotonic() + 29

    # A second stream editing the same channel waits for that slot ...
    waiting = asyncio.create_task(channel._wait_for_stream_slot("C1"))
    await _settle()
    assert not waiting.done()
    waiting.cancel()

    # ... while another channel has a budget of its own.
    await asyncio.wait_for(channel._wait_for_stream_slot("C2"), timeout=1)


@pytest.mark.asyncio
async def test_streams_are_bounded_and_aged_out(tmp_path) -> None:
    # A turn whose terminal event holds no text never closes its stream.
    channel = _channel(tmp_path)
    channel._client = _FakeSlackClient()

    for index in range(slack_connect._MAX_ACTIVE_STREAMS + 5):
        await channel.send(_message(content="hi", message_id=f"response-{index}"))
    await _settle()

    assert len(channel._streams) <= slack_connect._MAX_ACTIVE_STREAMS

    stale_key = next(iter(channel._streams))
    channel._streams[stale_key].touched_at -= (
        slack_connect._STREAM_IDLE_TIMEOUT_SECONDS + 1
    )
    channel._prune_streams()

    assert stale_key not in channel._streams


@pytest.mark.asyncio
async def test_stopping_the_channel_forgets_its_streams(tmp_path) -> None:
    channel = _channel(tmp_path)
    channel._client = _FakeSlackClient()

    await channel.send(_message(content="Hello"))
    await _settle()
    await channel.stop()

    assert channel._streams == {}


async def _inbound(channel: SlackChannel) -> Message:
    """Return the request one app mention produces."""
    channel._running = True
    received: list[Message] = []
    channel.on_message(received.append)
    await channel._handle_app_mention(
        {
            "type": "app_mention",
            "user": "U1",
            "channel": "C1",
            "channel_type": "channel",
            "text": "<@U-BOT> summarize this",
            "ts": "1710000000.000100",
        },
        {
            "event_id": "Ev1",
            "team_id": "T1",
            "authorizations": [{"user_id": "U-BOT", "is_bot": True}],
        },
    )
    assert len(received) == 1
    return received[0]


@pytest.mark.asyncio
async def test_a_streaming_channel_asks_for_a_streamed_answer(tmp_path) -> None:
    # Without this the reply arrives as one terminal event and chat.update never
    # has a delta to fold in, which is what left the streaming path inert.
    message = await _inbound(_channel(tmp_path, enable_streaming=True))

    assert message.is_stream is True


@pytest.mark.asyncio
async def test_streaming_off_still_asks_for_one_whole_answer(tmp_path) -> None:
    message = await _inbound(_channel(tmp_path, enable_streaming=False))

    assert message.is_stream is False


# ---------------------------------------------------------------------------
# "stream" mode: chat.startStream / chat.appendStream / chat.stopStream.
#
# Everything above this line is the "edit" mode the boolean flag means, and a
# deployment rolled back onto it has to keep behaving that way.
# ---------------------------------------------------------------------------


class _FakeStreamingClient:
    """Records the three streaming calls, and any ordinary post beside them."""

    def __init__(
        self,
        *,
        start_error: Exception | None = None,
        append_error: Exception | None = None,
        append_error_after: int = 0,
        stop_error: Exception | None = None,
        post_error: Exception | None = None,
        update_error: Exception | None = None,
    ) -> None:
        self.starts: list[dict[str, Any]] = []
        self.appends: list[dict[str, Any]] = []
        self.stops: list[dict[str, Any]] = []
        self.posts: list[dict[str, Any]] = []
        self.updates: list[dict[str, Any]] = []
        self._start_error = start_error
        self._append_error = append_error
        self._append_error_after = append_error_after
        self._stop_error = stop_error
        self._post_error = post_error
        self._update_error = update_error

    async def chat_startStream(self, **kwargs: Any) -> dict[str, str]:
        if self._start_error is not None:
            raise self._start_error
        self.starts.append(kwargs)
        return {"ts": "1710000099.000001"}

    async def chat_appendStream(self, **kwargs: Any) -> dict[str, str]:
        if (
            self._append_error is not None
            and len(self.appends) >= self._append_error_after
        ):
            raise self._append_error
        self.appends.append(kwargs)
        return {"ok": True}

    async def chat_stopStream(self, **kwargs: Any) -> dict[str, str]:
        if self._stop_error is not None:
            raise self._stop_error
        self.stops.append(kwargs)
        return {"ts": str(kwargs.get("ts") or "")}

    async def chat_postMessage(self, **kwargs: Any) -> dict[str, str]:
        if self._post_error is not None:
            raise self._post_error
        self.posts.append(kwargs)
        return {"ts": f"1710000098.{len(self.posts):06d}"}

    async def chat_update(self, **kwargs: Any) -> dict[str, str]:
        if self._update_error is not None:
            raise self._update_error
        self.updates.append(kwargs)
        return {"ts": str(kwargs.get("ts") or "")}


def _text_of(call: dict[str, Any]) -> str:
    """The markdown a start, append or stop call holds.

    Every one of them is made in chunk mode, because a stream is locked to the
    shape of its opening call and one opened with ``markdown_text`` refuses
    every chunk afterwards.
    """
    return "".join(
        str(chunk.get("text") or "")
        for chunk in call.get("chunks") or []
        if chunk.get("type") == "markdown_text"
    )


def _streaming_channel(tmp_path, **overrides: Any) -> SlackChannel:
    overrides.setdefault("enable_streaming", True)
    channel = _channel(tmp_path, **overrides)
    channel._remember_stream_recipient("slack_T1_C1_1710000000.000100", "U9", "T1")
    return channel


async def _stream(channel: SlackChannel, *deltas: str) -> None:
    for delta in deltas:
        await channel.send(_message(content=delta))
        await _settle()


# --- the flag ---------------------------------------------------------------


ENABLED = slack_connect.STREAMING_MODE_ENABLED


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # A key written with nothing after the colon names neither state.
        (None, "off"),
        ("", "off"),
        (False, "off"),
        (True, ENABLED),
        # The spellings a boolean takes in a config YAML never parsed -- an
        # environment variable, or a dict built in code. Unquoted "off" and "on"
        # never reach here from YAML, which coerces them first, and coerces them
        # correctly now that the key is a boolean again.
        ("false", "off"),
        ("no", "off"),
        ("off", "off"),
        ("0", "off"),
        ("true", ENABLED),
        ("yes", ENABLED),
        ("on", ENABLED),
        ("1", ENABLED),
        ("  TRUE  ", ENABLED),
        # Anything else is read as enabled rather than as off. The failure that
        # costs something is withdrawing a preview a deployment already had, not
        # granting one it typed badly -- and this covers an operator who wrote a
        # mode name here after reading a draft that offered them.
        ("stream", ENABLED),
        ("edit", ENABLED),
        ("streamed", ENABLED),
        (1, ENABLED),
    ],
)
def test_the_streaming_flag_reads_as_a_boolean(raw, expected) -> None:
    assert slack_connect.resolve_streaming_mode(raw) == expected


def test_an_unreadable_flag_says_so_before_it_guesses(tmp_path, caplog) -> None:
    with caplog.at_level(logging.WARNING, logger=slack_connect.logger.name):
        assert slack_connect.resolve_streaming_mode("stream") == ENABLED
    assert "not a boolean" in caplog.text


def test_the_three_modes_are_not_a_config_surface() -> None:
    """They are chosen per reply; nothing an operator writes names one."""
    assert slack_connect.STREAMING_MODES == ("off", "edit", "stream")
    assert ENABLED in slack_connect.STREAMING_MODES
    assert slack_connect.STREAMING_MODE_DEFAULT == "off"
    for name in ("edit", "stream"):
        assert slack_connect.resolve_streaming_mode(name) == ENABLED


def test_an_unset_flag_leaves_the_channel_exactly_as_it_was(tmp_path) -> None:
    """The default is today's behaviour, and nothing about it is streamed."""
    channel = _channel(tmp_path, enable_streaming=False)
    assert channel._streaming_mode() == "off"
    assert channel._streaming_enabled() is False

    default = SlackChannelConfig()
    assert default.enable_streaming is False
    assert slack_connect.resolve_streaming_mode(default.enable_streaming) == "off"


def test_the_flag_asks_for_the_best_path_available(tmp_path) -> None:
    channel = _channel(tmp_path, enable_streaming=True)
    assert channel._streaming_mode() == ENABLED
    assert channel._streaming_enabled() is True


@pytest.mark.parametrize(
    "template",
    [
        "config.yaml",
        "config.team.distributed.leader.yaml",
        "config.team.distributed.teammate.yaml",
    ],
)
def test_the_shipped_templates_all_carry_the_mode(template) -> None:
    """A key missing from a shipped template is deleted on upgrade.

    All three, because an operator running the distributed layout upgrades from
    a different file and would otherwise lose the key the others keep.
    """
    from pathlib import Path

    resources = Path(slack_connect.__file__).parents[4] / "resources"
    text = (resources / template).read_text(encoding="utf-8")
    lines = [line.strip() for line in text.splitlines()]
    assert "enable_streaming: false" in lines
    # A boolean, like every other connector's copy of this key. The internal
    # mode names must not appear as though they were values.
    assert 'enable_streaming: "' not in text
    assert "enable_streaming: off" not in lines
    assert "enable_streaming: stream" not in lines


@pytest.mark.parametrize(("raw", "reported"), [(False, False), (True, True)])
def test_the_channel_metadata_reports_the_key_not_the_choice(
    tmp_path, raw, reported
) -> None:
    """Which path a reply took is per reply and belongs in the log, not here."""
    channel = _channel(tmp_path, enable_streaming=raw)
    assert channel.get_metadata().extra["enable_streaming"] is reported


# --- the happy path ---------------------------------------------------------


@pytest.mark.asyncio
async def test_deltas_open_one_stream_and_append_only_what_is_new(tmp_path) -> None:
    channel = _streaming_channel(tmp_path)
    client = _FakeStreamingClient()
    channel._client = client

    await _stream(channel, "Hello\n", "world\n")

    assert client.starts == [
        {
            "channel": "C1",
            "thread_ts": "1710000000.000100",
            # Chunks, not markdown_text: the opening call decides the mode for
            # the whole life of the stream, and a text-mode stream refuses every
            # chunk the close would send.
            "chunks": [{"type": "markdown_text", "text": "Hello\n"}],
            "recipient_user_id": "U9",
            "recipient_team_id": "T1",
        }
    ]
    # The append is the increment, not the snapshot: that is the whole point of
    # the method, and the edit path re-sends the entire answer every window.
    assert [_text_of(append) for append in client.appends] == ["world\n"]
    assert client.appends[0]["ts"] == "1710000099.000001"
    assert client.updates == []
    assert client.posts == []


@pytest.mark.asyncio
async def test_the_terminal_event_appends_the_tail_and_stops_the_stream(
    tmp_path,
) -> None:
    channel = _streaming_channel(tmp_path)
    client = _FakeStreamingClient()
    channel._client = client

    await _stream(channel, "Hello\n", "world")
    await channel.send(_message(event_type=EventType.CHAT_FINAL, content="Hello\nworld"))
    await _settle()

    # "world" never had a line break after it, so it was held back until the
    # close, which is what delivers it.
    assert [_text_of(append) for append in client.appends] == []
    assert client.stops == [
        {
            "channel": "C1",
            "ts": "1710000099.000001",
            "chunks": [{"type": "markdown_text", "text": "world"}],
        }
    ]
    # And no second copy of the answer underneath it.
    assert client.posts == []
    assert client.updates == []


@pytest.mark.asyncio
async def test_a_reply_with_no_line_break_never_opens_a_stream(tmp_path) -> None:
    """Nothing can be appended safely, so the answer is posted whole instead."""
    channel = _streaming_channel(tmp_path)
    client = _FakeStreamingClient()
    channel._client = client

    await _stream(channel, "Yes", ", quite.")
    await channel.send(
        _message(event_type=EventType.CHAT_FINAL, content="Yes, quite.")
    )
    await _settle()

    assert client.starts == []
    assert client.stops == []
    assert [post["text"] for post in client.posts] == ["Yes, quite."]


@pytest.mark.asyncio
async def test_markup_split_across_two_deltas_is_sent_once_and_whole(
    tmp_path,
) -> None:
    """Prefix stability, which is what the whole holding-back exists for.

    Slack parses each append on its own, so a construct cut in half arrives as
    two halves and neither is what was written: ``**bo`` is literal text and
    ``ld**`` after it does not rescue it. Cutting at the last complete line is
    the smallest unit no continuation reaches inside.
    """
    channel = _streaming_channel(tmp_path)
    client = _FakeStreamingClient()
    channel._client = client

    await _stream(channel, "**bo", "ld** and on\n", "after\n")

    assert _text_of(client.starts[0]) == "**bold** and on\n"
    assert [_text_of(append) for append in client.appends] == ["after\n"]


@pytest.mark.asyncio
async def test_a_table_is_held_back_and_lands_in_position_at_the_close(
    tmp_path,
) -> None:
    """The gain streaming buys: structure where it was written, not at the end."""
    channel = _streaming_channel(tmp_path)
    client = _FakeStreamingClient()
    channel._client = client

    body = "Here it is:\n\n| a | b |\n| --- | --- |\n| 1 | 2 |"
    await _stream(channel, "Here it is:\n\n", "| a | b |\n", "| --- | --- |\n", "| 1 | 2 |")
    await channel.send(_message(event_type=EventType.CHAT_FINAL, content=body))
    await _settle()

    # The prose streamed; the table did not, because a row of pipes is a
    # paragraph until the delimiter row under it arrives.
    assert _text_of(client.starts[0]) == "Here it is:\n\n"
    assert client.appends == []
    chunks = client.stops[0]["chunks"]
    assert [chunk["type"] for chunk in chunks] == ["blocks"]
    assert [block["type"] for block in chunks[0]["blocks"]] == ["data_table"]
    # Into chunks, never into stopStream's own blocks= argument, which renders
    # at the bottom of the finished message rather than in position.
    assert "blocks" not in client.stops[0]


@pytest.mark.asyncio
async def test_a_code_fence_that_renders_to_nothing_still_streams(tmp_path) -> None:
    channel = _streaming_channel(tmp_path)
    client = _FakeStreamingClient()
    channel._client = client

    await _stream(channel, "```\ncode\n```\nafter\n")

    assert _text_of(client.starts[0]) == "```\ncode\n```\nafter\n"


@pytest.mark.asyncio
async def test_an_unclosed_fence_waits_for_its_closing_marker(tmp_path) -> None:
    channel = _streaming_channel(tmp_path)
    client = _FakeStreamingClient()
    channel._client = client

    await _stream(channel, "before\n", "```\n", "code\n")

    # The fence could still turn out to be a chart, which cannot be un-sent.
    assert _text_of(client.starts[0]) == "before\n"
    assert client.appends == []


@pytest.mark.asyncio
async def test_the_block_marker_never_reaches_the_channel(tmp_path) -> None:
    channel = _streaming_channel(tmp_path)
    client = _FakeStreamingClient()
    channel._client = client

    await _stream(channel, "one\n", "<!-- jiuwenswarm:slack-blocks -->\n", "two\n")

    written = _text_of(client.starts[0]) + "".join(
        _text_of(append) for append in client.appends
    )
    assert "jiuwenswarm:slack-blocks" not in written
    assert written == "one\ntwo\n"


@pytest.mark.asyncio
async def test_a_threaded_report_streams_as_one_message(tmp_path) -> None:
    """The marker splits a reply only where there is somewhere to put the pieces.

    That is a reply outside a thread, and a reply outside a thread is one that
    cannot be streamed at all -- so a streamed reply is always the flattened
    form the posting path would have produced in the same place.
    """
    channel = _streaming_channel(tmp_path)
    client = _FakeStreamingClient()
    channel._client = client
    marker = slack_connect._SLACK_THREAD_DETAILS_MARKER

    body = f"summary\n{marker}\ndetail\n"
    await _stream(channel, "summary\n", f"{marker}\n", "detail\n")
    await channel.send(_message(event_type=EventType.CHAT_FINAL, content=body))
    await _settle()

    # The marker stops the stream where it stands; the close delivers the rest.
    assert _text_of(client.starts[0]) == "summary\n"
    assert client.stops[0]["chunks"] == [
        {"type": "markdown_text", "text": "\ndetail"}
    ]
    # And never as a second message beside it.
    assert client.posts == []


@pytest.mark.asyncio
async def test_a_long_tail_is_appended_in_pieces_rather_than_split_into_messages(
    tmp_path,
) -> None:
    """The mode-aware fork: an append takes 12,000, an edit takes 4,000."""
    channel = _streaming_channel(tmp_path)
    client = _FakeStreamingClient()
    channel._client = client

    body = "opening\n" + ("word " * 6000)
    await _stream(channel, "opening\n")
    await channel.send(
        _message(event_type=EventType.CHAT_FINAL, content=body.strip())
    )
    await _settle()

    chunks = client.stops[0]["chunks"]
    assert len(chunks) == 3
    assert {chunk["type"] for chunk in chunks} == {"markdown_text"}
    assert "".join(chunk["text"] for chunk in chunks).strip() == ("word " * 6000).strip()
    assert all(
        len(chunk["text"]) <= slack_connect._MAX_SLACK_APPEND_TEXT_LENGTH
        for chunk in chunks
    )
    # One message, however long the answer runs. The edit path would have made
    # this eight, the first of them clamped to 4,000.
    assert client.posts == []


# --- the recipient ----------------------------------------------------------


@pytest.mark.asyncio
async def test_the_recipient_is_remembered_at_inbound_and_read_at_the_open(
    tmp_path,
) -> None:
    channel = _channel(tmp_path, enable_streaming=True)
    client = _FakeStreamingClient()
    channel._client = client
    assert channel._stream_recipients == {}

    channel._remember_stream_recipient("slack_T1_C1_1710000000.000100", "U9", "T1")
    await _stream(channel, "hi\n")

    assert client.starts[0]["recipient_user_id"] == "U9"
    assert client.starts[0]["recipient_team_id"] == "T1"


@pytest.mark.asyncio
async def test_a_dm_recipient_is_recoverable_from_the_session_id(tmp_path) -> None:
    """A DM session spells the user out; a channel session ends in a thread ts."""
    channel = _channel(tmp_path, enable_streaming=True)
    dm = _message(content="hi\n")
    dm.session_id = "slack_T7_D5_U3"
    dm.metadata = {"slack_channel_id": "D5", "slack_thread_ts": ""}
    assert channel._stream_recipient(dm) == ("U3", "T7")

    room = _message(content="hi\n")
    room.metadata = {"slack_channel_id": "C1", "slack_thread_ts": "1.1"}
    # The team is in the session id; the user is not, which is the whole reason
    # for the inbound record.
    assert channel._stream_recipient(room) == ("", "T1")


def test_the_recipient_map_is_bounded(tmp_path) -> None:
    channel = _channel(tmp_path, enable_streaming=True)
    for index in range(slack_connect._MAX_STREAM_RECIPIENTS + 40):
        channel._remember_stream_recipient(f"session-{index}", f"U{index}", "T1")
    assert len(channel._stream_recipients) <= slack_connect._MAX_STREAM_RECIPIENTS


# --- the raise contract, which is the highest-risk part of the change -------


@pytest.mark.asyncio
async def test_a_failed_close_that_cannot_be_recovered_from_still_raises(
    tmp_path,
) -> None:
    """The regression this whole inversion is at risk of.

    ``close()`` is a no-op on the edit surface, where ``send()`` makes the
    closing rewrite and raises on it. On the streaming surface the closing call
    lives in the surface, and the obligation to raise when the reply does not
    land has to survive that move.
    """
    channel = _streaming_channel(tmp_path)
    client = _FakeStreamingClient(
        stop_error=RuntimeError("message_not_in_streaming_state"),
        update_error=RuntimeError("message_not_found"),
        post_error=RuntimeError("channel_not_found"),
    )
    channel._client = client

    await _stream(channel, "opening\n")
    with pytest.raises(SlackDeliveryError) as excinfo:
        await channel.send(
            _message(
                event_type=EventType.CHAT_FINAL,
                content="opening\nthe part that never landed",
            )
        )
    assert "channel_not_found" in str(excinfo.value)


@pytest.mark.asyncio
async def test_an_expired_stream_is_rewritten_whole_and_stays_one_message(
    tmp_path,
) -> None:
    """chat.update succeeds on an expired stream, so the reply need not split.

    Measured: an edit is refused on a live stream with
    ``streaming_state_conflict`` and accepted once the stream has expired. The
    message comes back to the ordinary API, and the closing rewrite the edit
    path would have made can be made after all.
    """
    channel = _streaming_channel(tmp_path)
    client = _FakeStreamingClient(
        stop_error=RuntimeError("message_not_in_streaming_state")
    )
    channel._client = client

    await _stream(channel, "opening\n")
    await channel.send(
        _message(
            event_type=EventType.CHAT_FINAL, content="opening\nthe rest of it"
        )
    )
    await _settle()

    assert client.posts == []
    assert [update["text"] for update in client.updates] == [
        "opening\nthe rest of it"
    ]
    assert client.updates[0]["ts"] == "1710000099.000001"
    # Always sent, empty when there are none: a streamed message stores the
    # blocks its chunks built, and an edit holding text alone would leave the
    # old rendering above the new text.
    assert client.updates[0]["blocks"] == []


@pytest.mark.asyncio
async def test_a_reply_too_long_to_rewrite_is_posted_beside_the_stream(
    tmp_path,
) -> None:
    """An edit takes 4,000 characters; a stream took more than that."""
    channel = _streaming_channel(tmp_path)
    client = _FakeStreamingClient(
        stop_error=RuntimeError("message_not_in_streaming_state")
    )
    channel._client = client

    opening = ("word " * 1200) + "\n"
    await _stream(channel, opening)
    await channel.send(
        _message(
            event_type=EventType.CHAT_FINAL,
            content=f"{opening}the rest of it",
        )
    )
    await _settle()

    # The point is not that the edit would have been refused. It is that the
    # message already holds more than an edit can send, so a rewrite could
    # only have put back less than the reader can already see.
    assert len(opening) > slack_connect._MAX_SLACK_UPDATE_TEXT_LENGTH
    assert client.updates == []
    assert [post["text"] for post in client.posts] == ["the rest of it"]
    assert client.posts[0]["thread_ts"] == "1710000000.000100"


@pytest.mark.asyncio
async def test_an_edit_refused_because_the_stream_is_live_closes_it_again(
    tmp_path, caplog
) -> None:
    """``streaming_state_conflict`` is the one code that means "try again".

    It says the message is still the streaming API's, which is exactly what the
    failed close did not establish -- so whatever went wrong there was
    transient.
    """
    channel = _streaming_channel(tmp_path)

    class _ConflictingClient(_FakeStreamingClient):
        def __init__(self) -> None:
            super().__init__()
            self.stop_attempts = 0

        async def chat_stopStream(self, **kwargs: Any) -> dict[str, str]:
            # The first close fails transiently; the second, made only because
            # the refused edit said the message was still streaming, succeeds.
            self.stop_attempts += 1
            if self.stop_attempts == 1:
                raise RuntimeError("service_unavailable")
            return await super().chat_stopStream(**kwargs)

        async def chat_update(self, **kwargs: Any) -> dict[str, str]:
            raise RuntimeError("streaming_state_conflict")

    client = _ConflictingClient()
    channel._client = client

    await _stream(channel, "opening\n")
    with caplog.at_level(logging.INFO, logger=slack_connect.logger.name):
        await channel.send(
            _message(
                event_type=EventType.CHAT_FINAL, content="opening\nthe rest of it"
            )
        )
        await _settle()

    assert "still open after all" in caplog.text
    assert client.stop_attempts == 2
    assert [
        chunk["text"]
        for stop in client.stops
        for chunk in stop.get("chunks") or []
    ] == ["the rest of it"]
    assert client.posts == []


@pytest.mark.asyncio
async def test_a_close_that_fails_with_the_answer_already_on_screen_does_not_raise(
    tmp_path, caplog
) -> None:
    """Raising here would report a failure for a reply the reader can see."""
    channel = _streaming_channel(tmp_path)
    client = _FakeStreamingClient(
        stop_error=RuntimeError("message_not_in_streaming_state")
    )
    channel._client = client

    await _stream(channel, "the whole answer\n")
    with caplog.at_level(logging.WARNING, logger=slack_connect.logger.name):
        await channel.send(
            _message(event_type=EventType.CHAT_FINAL, content="the whole answer")
        )
    await _settle()

    assert client.posts == []
    assert "complete on screen" in caplog.text


@pytest.mark.asyncio
async def test_a_reader_stopping_the_stream_gets_the_rest_as_a_new_message(
    tmp_path, caplog
) -> None:
    """New behaviour with no analogue on the edit path: a reader can say stop."""
    channel = _streaming_channel(tmp_path)
    client = _FakeStreamingClient(
        append_error=RuntimeError("stopped_by_user"), append_error_after=0
    )
    channel._client = client

    with caplog.at_level(logging.INFO, logger=slack_connect.logger.name):
        await _stream(channel, "first\n", "second\n")
        await channel.send(
            _message(event_type=EventType.CHAT_FINAL, content="first\nsecond\nthird")
        )
        await _settle()

    assert "stopped by the reader" in caplog.text
    # Everything after what the reader let through is posted rather than lost.
    assert [post["text"] for post in client.posts] == ["second\nthird"]
    # And never by rewriting the message whole, which would put the answer they
    # stopped back on screen in place of the part they let through.
    assert client.updates == []


@pytest.mark.asyncio
async def test_a_message_this_app_does_not_own_is_reported_and_abandoned(
    tmp_path, caplog
) -> None:
    channel = _streaming_channel(tmp_path)
    client = _FakeStreamingClient(
        append_error=RuntimeError("message_not_owned_by_app"), append_error_after=0
    )
    channel._client = client

    with caplog.at_level(logging.WARNING, logger=slack_connect.logger.name):
        await _stream(channel, "first\n", "second\n")
    assert "message_not_owned_by_app" in caplog.text


@pytest.mark.asyncio
async def test_a_missing_recipient_is_logged_as_the_plumbing_failing(
    tmp_path, caplog
) -> None:
    """It cannot happen from the outside, so it is an error rather than a warning."""
    channel = _streaming_channel(tmp_path)
    client = _FakeStreamingClient(
        append_error=RuntimeError("missing_recipient_user_id"), append_error_after=0
    )
    channel._client = client

    with caplog.at_level(logging.ERROR, logger=slack_connect.logger.name):
        await _stream(channel, "first\n", "second\n")
    assert "for want of a recipient" in caplog.text


@pytest.mark.asyncio
async def test_a_stream_that_cannot_start_falls_back_to_the_edit_preview(
    tmp_path, caplog
) -> None:
    """Safe because the opening call is the first thing either surface makes.

    Nothing is on screen when it fails, so building the other surface and
    trying again is invisible to the reader.
    """
    channel = _streaming_channel(tmp_path)
    client = _FakeStreamingClient(start_error=RuntimeError("invalid_arguments"))
    channel._client = client

    with caplog.at_level(logging.INFO, logger=slack_connect.logger.name):
        await _stream(channel, "hello\n", "again\n")
        await channel.send(
            _message(event_type=EventType.CHAT_FINAL, content="hello\nagain")
        )
        await _settle()

    assert client.appends == []
    assert client.stops == []
    assert "showing this reply with edits instead" in caplog.text
    # One message posted and then rewritten, which is what "edit" does -- not a
    # single post at the end of the turn, which is what "off" does.
    assert [post["text"] for post in client.posts] == ["hello"]
    assert [update["text"] for update in client.updates][-1] == "hello\nagain"


@pytest.mark.asyncio
async def test_a_reply_is_posted_whole_only_when_neither_surface_opens(
    tmp_path,
) -> None:
    channel = _streaming_channel(tmp_path)

    class _ClosedClient(_FakeStreamingClient):
        async def chat_postMessage(self, **kwargs: Any) -> dict[str, str]:
            # The preview cannot be posted either; only the final delivery is
            # allowed through, which is send()'s own write.
            if kwargs.get("text") == "hello":
                raise RuntimeError("channel_not_found")
            return await super().chat_postMessage(**kwargs)

    client = _ClosedClient(start_error=RuntimeError("invalid_arguments"))
    channel._client = client

    await _stream(channel, "hello\n", "again\n")
    await channel.send(
        _message(event_type=EventType.CHAT_FINAL, content="hello\nagain")
    )
    await _settle()

    assert client.starts == []
    assert client.updates == []
    assert [post["text"] for post in client.posts] == ["hello\nagain"]


@pytest.mark.asyncio
async def test_a_reply_that_does_not_continue_what_streamed_is_posted_whole(
    tmp_path, caplog
) -> None:
    """An append cannot retract, so a rewritten answer starts a new message."""
    channel = _streaming_channel(tmp_path)
    client = _FakeStreamingClient()
    channel._client = client

    await _stream(channel, "the draft\n")
    with caplog.at_level(logging.WARNING, logger=slack_connect.logger.name):
        await channel.send(
            _message(
                event_type=EventType.CHAT_FINAL, content="something else entirely"
            )
        )
    await _settle()

    assert "does not continue what was streamed" in caplog.text
    # Taken out of streaming state all the same, then posted in full.
    assert client.stops[0] == {"channel": "C1", "ts": "1710000099.000001"}
    assert [post["text"] for post in client.posts] == ["something else entirely"]


# --- a preamble in front of the answer --------------------------------------
#
# The runtime emits every assistant segment of the turn as a delta, so text the
# model writes before it calls a tool is streamed; the terminal event holds
# only the final segment. What is on screen is therefore that narration plus as
# much of the answer as arrived, while the finished reply is the answer alone.
# It is the ordinary shape of a turn that used a tool, not an edge case.


_PREAMBLE = "Let me look that up first.\n\n"
_STREAMED_ANSWER = "The answer is 42, and has been\n"
_ANSWER_TAIL = "since 1979."
_FINAL_ANSWER = _STREAMED_ANSWER + _ANSWER_TAIL


def test_the_answer_is_found_past_the_narration_that_streamed_before_it() -> None:
    """The finished reply starts inside what streamed, not at its beginning."""
    sent = _PREAMBLE + _STREAMED_ANSWER

    remainder = SlackChannel._streamed_remainder(sent, _FINAL_ANSWER)

    # Only the part of the answer that never streamed, so the message ends in
    # the answer entire rather than in a second copy of its opening.
    assert remainder == _ANSWER_TAIL


def test_a_preamble_that_ends_in_the_answer_leaves_nothing_to_append() -> None:
    """A tool ran after the whole answer had already streamed."""
    remainder = SlackChannel._streamed_remainder(
        _PREAMBLE + _FINAL_ANSWER, _FINAL_ANSWER
    )

    assert remainder == ""


def test_the_seam_is_taken_at_its_earliest_reading() -> None:
    """A repeated opening must not make the append repeat what is on screen."""
    answer = "Checking the log.\nChecking the log.\nIt says nothing.\n"
    sent = "Working on it.\n\n" + "Checking the log.\nChecking the log.\n"

    remainder = SlackChannel._streamed_remainder(sent, answer)

    # The later reading of the seam would re-append a line already written.
    assert remainder == "It says nothing.\n"


def test_a_chance_agreement_at_the_seam_is_not_a_seam() -> None:
    """Too little in common to be the answer beginning inside the narration."""
    assert SlackChannel._streamed_remainder("the draft ends\n", "endless, really") is None


@pytest.mark.asyncio
async def test_a_narrated_turn_stays_one_message(tmp_path) -> None:
    channel = _streaming_channel(tmp_path)
    client = _FakeStreamingClient()
    channel._client = client

    await _stream(channel, _PREAMBLE, _STREAMED_ANSWER)
    await channel.send(
        _message(event_type=EventType.CHAT_FINAL, content=_FINAL_ANSWER)
    )
    await _settle()

    # The close delivers the tail alone. Everything before it is on screen.
    assert client.stops == [
        {
            "channel": "C1",
            "ts": "1710000099.000001",
            "chunks": [{"type": "markdown_text", "text": _ANSWER_TAIL}],
        }
    ]
    # The whole point: no second copy of the answer underneath the stream.
    assert client.posts == []
    assert client.updates == []


@pytest.mark.asyncio
async def test_a_rendering_slack_refuses_costs_the_formatting_not_the_reply(
    tmp_path,
) -> None:
    channel = _streaming_channel(tmp_path)

    class _PickyClient(_FakeStreamingClient):
        async def chat_stopStream(self, **kwargs: Any) -> dict[str, str]:
            if any(
                chunk.get("type") == "blocks" for chunk in kwargs.get("chunks") or []
            ):
                raise RuntimeError("invalid_blocks")
            return await super().chat_stopStream(**kwargs)

    client = _PickyClient()
    channel._client = client

    body = "Here it is:\n\n| a | b |\n| --- | --- |\n| 1 | 2 |"
    await _stream(channel, "Here it is:\n\n")
    await channel.send(_message(event_type=EventType.CHAT_FINAL, content=body))
    await _settle()

    # The stream still finished, and the table arrived as text rather than
    # being dropped with the rendering that held it.
    assert len(client.stops) == 1
    assert client.stops[0]["chunks"] == [
        {"type": "markdown_text", "text": "| a | b |\n| --- | --- |\n| 1 | 2 |"}
    ]
    assert client.posts == []


@pytest.mark.asyncio
async def test_a_rate_limited_append_is_retried(tmp_path, monkeypatch) -> None:
    channel = _streaming_channel(tmp_path)

    class _RateLimited(Exception):
        pass

    monkeypatch.setattr(
        slack_connect,
        "retry_after_seconds",
        lambda exc: 0.0 if isinstance(exc, _RateLimited) else None,
    )

    class _FlakyClient(_FakeStreamingClient):
        def __init__(self) -> None:
            super().__init__()
            self.attempts = 0

        async def chat_appendStream(self, **kwargs: Any) -> dict[str, str]:
            self.attempts += 1
            if self.attempts == 1:
                raise _RateLimited()
            return await super().chat_appendStream(**kwargs)

    client = _FlakyClient()
    channel._client = client

    await _stream(channel, "one\n", "two\n")
    assert [_text_of(append) for append in client.appends] == ["two\n"]
    assert client.attempts == 2


# --- the other two modes are untouched by any of this -----------------------


@pytest.mark.asyncio
async def test_streaming_off_makes_none_of_the_three_calls(tmp_path) -> None:
    channel = _channel(tmp_path, enable_streaming=False)
    client = _FakeStreamingClient()
    channel._client = client

    await _stream(channel, "hello\n")
    await channel.send(_message(event_type=EventType.CHAT_FINAL, content="hello"))
    await _settle()

    assert client.starts == []
    assert client.appends == []
    assert client.stops == []
    assert [post["text"] for post in client.posts] == ["hello"]


@pytest.mark.asyncio
async def test_edit_mode_makes_none_of_the_three_calls(tmp_path) -> None:
    channel = _channel(tmp_path, enable_streaming="edit")
    client = _FakeStreamingClient()
    channel._client = client

    await _stream(channel, "hello\n", "again\n")
    await channel.send(
        _message(event_type=EventType.CHAT_FINAL, content="hello\nagain")
    )
    await _settle()

    assert client.starts == []
    assert client.appends == []
    assert client.stops == []
    assert len(client.posts) == 1
    assert [update["text"] for update in client.updates][-1] == "hello\nagain"


# --- the prefix helper on its own -------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("plain prose\nand more\n", "plain prose\nand more\n"),
        ("prose\n| a | b |\n| --- | --- |\n", "prose\n"),
        # A pipe in a sentence is not a table, and a following line proves it.
        ("a | b\nordinary\n", "a | b\nordinary\n"),
        # ... but the last line received cannot be judged yet.
        ("prose\na | b\n", "prose\n"),
        ("```mermaid\npie title T\n    \"a\" : 10\n```\n", ""),
        ("before\n```\nplain\n```\n", "before\n```\nplain\n```\n"),
        ("before\n```\nunclosed\n", "before\n"),
        ("", ""),
    ],
)
def test_only_text_that_cannot_change_its_mind_is_streamable(text, expected) -> None:
    from jiuwenswarm.common import slack_blocks

    assert text[: slack_blocks.streamable_prose_prefix(text)] == expected


def test_the_streamable_prefix_only_ever_grows() -> None:
    """What makes it usable as an append offset at all."""
    from jiuwenswarm.common import slack_blocks

    body = (
        "An answer with *emphasis*\n\nand a table:\n\n"
        "| a | b |\n| --- | --- |\n| 1 | 2 |\n\nand a tail.\n"
    )
    seen = 0
    for size in range(1, len(body) + 1):
        prefix = body[:size]
        stable = prefix[: prefix.rfind("\n") + 1]
        length = slack_blocks.streamable_prose_prefix(stable)
        assert length >= seen, (size, length, seen)
        assert stable[:length] == body[:length]
        seen = length


# --- a stream is a threaded reply or it is nothing ---------------------------
#
# Measured against the live API rather than read off the reference page, which
# marks thread_ts optional and does not list the error it actually returns:
#
#   thread_ts: ""                                  -> invalid_thread_ts
#   thread_ts omitted                              -> invalid_thread_ts
#   real thread_ts, recipient_user_id only         -> missing_recipient_team_id
#   real thread_ts + both recipient ids            -> ok


def _at_top_level(event_type: EventType, content: str, channel_id: str = "C1"):
    """One event for a reply the connector would post outside any thread."""
    msg = _message(event_type=event_type, content=content)
    msg.metadata = {"slack_channel_id": channel_id, "slack_thread_ts": ""}
    return msg


@pytest.mark.asyncio
async def test_a_reply_that_is_not_in_a_thread_falls_to_the_edit_preview(
    tmp_path, caplog
) -> None:
    """A DM answered at the top of the conversation, which is the common DM.

    A ladder, not a cliff: ``stream`` cannot reach this reply, so it gets the
    preview ``edit`` would have given it. Turning ``stream`` on must not make a
    DM worse than it was, which is what removing the preview entirely did.
    """
    channel = _streaming_channel(tmp_path)
    client = _FakeStreamingClient()
    channel._client = client

    with caplog.at_level(logging.INFO, logger=slack_connect.logger.name):
        for delta in ("hello\n", "again\n"):
            await channel.send(_at_top_level(EventType.CHAT_DELTA, delta, "D5"))
            await _settle()
        await channel.send(
            _at_top_level(EventType.CHAT_FINAL, "hello\nagain", "D5")
        )
        await _settle()

    # Refused before the call rather than by it: no attempt is made at all.
    assert client.starts == []
    assert "streaming unavailable for this reply" in caplog.text
    # The preview is there, one message posted then rewritten.
    assert [post["text"] for post in client.posts] == ["hello"]
    assert [update["text"] for update in client.updates][-1] == "hello\nagain"


@pytest.mark.asyncio
async def test_the_refusal_is_logged_once_per_turn_not_once_per_delta(
    tmp_path, caplog
) -> None:
    channel = _streaming_channel(tmp_path)
    channel._client = _FakeStreamingClient()

    with caplog.at_level(logging.INFO, logger=slack_connect.logger.name):
        for delta in ("one\n", "two\n", "three\n", "four\n"):
            await channel.send(_at_top_level(EventType.CHAT_DELTA, delta, "D5"))
            await _settle()

    assert caplog.text.count("streaming unavailable for this reply") == 1


@pytest.mark.asyncio
async def test_a_channel_that_replies_flat_is_not_streamed_either(tmp_path) -> None:
    """reply_in_thread: false is an instruction, not an obstacle to route around.

    Threading the reply so that a preview becomes possible would put an optional
    feature in charge of a setting the operator wrote down.
    """
    channel = _streaming_channel(tmp_path, reply_in_thread=False)
    client = _FakeStreamingClient()
    channel._client = client

    await channel.send(_at_top_level(EventType.CHAT_DELTA, "hello\n"))
    await _settle()
    await channel.send(_at_top_level(EventType.CHAT_FINAL, "hello there"))
    await _settle()

    # No stream, and the reply is still posted flat -- but it is previewed.
    assert client.starts == []
    assert [post["text"] for post in client.posts] == ["hello"]
    assert "thread_ts" not in client.posts[0]
    assert [update["text"] for update in client.updates][-1] == "hello there"


@pytest.mark.asyncio
async def test_a_thread_ts_that_is_sent_is_never_empty(tmp_path) -> None:
    channel = _streaming_channel(tmp_path)
    client = _FakeStreamingClient()
    channel._client = client

    await _stream(channel, "hello\n")

    assert client.starts[0]["thread_ts"] == "1710000000.000100"


@pytest.mark.asyncio
async def test_a_channel_stream_carries_both_recipient_ids(tmp_path) -> None:
    channel = _streaming_channel(tmp_path)
    client = _FakeStreamingClient()
    channel._client = client

    await _stream(channel, "hello\n")

    assert client.starts[0]["recipient_user_id"] == "U9"
    assert client.starts[0]["recipient_team_id"] == "T1"


@pytest.mark.asyncio
async def test_a_channel_with_only_half_a_recipient_is_not_streamed(
    tmp_path, caplog
) -> None:
    """A call holding one of the two is refused with missing_recipient_team_id."""
    channel = _channel(tmp_path, enable_streaming=True)
    client = _FakeStreamingClient()
    channel._client = client
    # A user with no team: the session id would have supplied one, so this is
    # the case where it says "default" and is not an encoded team at all.
    channel._remember_stream_recipient("slack_default_C1_1.1", "U9", "")

    msg = _message(content="hello\n")
    msg.session_id = "slack_default_C1_1.1"
    with caplog.at_level(logging.INFO, logger=slack_connect.logger.name):
        await channel.send(msg)
        await _settle()

    assert client.starts == []
    assert "both recipient ids" in caplog.text
    # And it is previewed with edits rather than not previewed at all.
    assert [post["text"] for post in client.posts] == ["hello"]


def test_the_session_ids_placeholder_team_is_not_read_as_one(tmp_path) -> None:
    channel = _channel(tmp_path, enable_streaming=True)
    msg = _message(content="hi\n")
    msg.session_id = "slack_default_C1_1710000000.000100"
    msg.metadata = {"slack_channel_id": "C1", "slack_thread_ts": "1.1"}
    assert channel._stream_recipient(msg) == ("", "")


@pytest.mark.asyncio
async def test_a_dm_answered_inside_a_thread_streams_with_no_recipient(
    tmp_path,
) -> None:
    """The ids are documented required for channels only, and a DM has a thread."""
    channel = _channel(tmp_path, enable_streaming=True)
    client = _FakeStreamingClient()
    channel._client = client

    msg = _message(content="hello\n")
    msg.session_id = "slack_T7_D5_U3"
    msg.metadata = {"slack_channel_id": "D5", "slack_thread_ts": "1710000000.000100"}
    await channel.send(msg)
    await _settle()

    assert client.starts[0]["channel"] == "D5"
    assert client.starts[0]["thread_ts"] == "1710000000.000100"
    # Recovered from the session id, which spells a DM's user out.
    assert client.starts[0]["recipient_user_id"] == "U3"


@pytest.mark.parametrize(
    ("channel_id", "thread_ts", "user_id", "team_id", "refused"),
    [
        ("C1", "1.1", "U9", "T1", False),
        ("D5", "1.1", "", "", False),
        ("C1", "", "U9", "T1", True),
        ("D5", "", "U3", "T7", True),
        ("C1", "1.1", "U9", "", True),
        ("C1", "1.1", "", "T1", True),
        ("G2", "1.1", "", "", True),
    ],
)
def test_what_can_and_cannot_be_streamed(
    tmp_path, channel_id, thread_ts, user_id, team_id, refused
) -> None:
    channel = _channel(tmp_path, enable_streaming=True)
    assert bool(channel._cannot_stream(channel_id, thread_ts, user_id, team_id)) is (
        refused
    )


# --- expiry, which long turns meet as a matter of course ---------------------
#
# Measured: a stream lives about five minutes from chat.startStream and
# appending does not extend it. Two probes, one appending every 60s and one
# every 10s, died within seven seconds of the same elapsed time -- 5.30 and
# 5.18 minutes -- both with message_not_in_streaming_state. Turns here run far
# longer than that, so what follows is the ordinary path and not an edge.


@pytest.mark.asyncio
async def test_a_stream_that_expires_mid_turn_is_finished_at_the_close(
    tmp_path, caplog
) -> None:
    channel = _streaming_channel(tmp_path)
    client = _FakeStreamingClient(
        append_error=RuntimeError("message_not_in_streaming_state"),
        append_error_after=1,
    )
    channel._client = client

    with caplog.at_level(logging.WARNING, logger=slack_connect.logger.name):
        await _stream(channel, "one\n", "two\n", "three\n", "four\n")
        await channel.send(
            _message(
                event_type=EventType.CHAT_FINAL, content="one\ntwo\nthree\nfour"
            )
        )
        await _settle()

    # One append landed before the stream expired; the rest did not, and the
    # latch stopped it retrying once per delta for the rest of the turn.
    assert [_text_of(append) for append in client.appends] == ["two\n"]
    assert "will take no more" in caplog.text
    # The message keeps what it had until the turn ends, and then gets the whole
    # answer in one edit rather than a second message beside it.
    assert client.posts == []
    assert [update["text"] for update in client.updates] == [
        "one\ntwo\nthree\nfour"
    ]


@pytest.mark.asyncio
async def test_the_rewrite_after_expiry_never_shortens_the_message(
    tmp_path,
) -> None:
    """The rewrite may only ever add. It is bounded so that it cannot do less."""
    channel = _streaming_channel(tmp_path)
    client = _FakeStreamingClient(
        stop_error=RuntimeError("message_not_in_streaming_state")
    )
    channel._client = client

    await _stream(channel, "one\n", "two\n")
    await channel.send(
        _message(event_type=EventType.CHAT_FINAL, content="one\ntwo\nthree")
    )
    await _settle()

    on_screen = _text_of(client.starts[0]) + "".join(
        _text_of(append) for append in client.appends
    )
    assert client.updates[0]["text"].startswith(on_screen.strip())
    assert len(client.updates[0]["text"]) >= len(on_screen.strip())


# --- the dialect, which is not the one the rest of the connector writes ------
#
# Measured, by sending exactly what _normalize_slack_mrkdwn produces as
# markdown_text and reading back what Slack stored:
#
#   *bold-in-mrkdwn*   -> stored _bold-in-mrkdwn_, structure text[italic]
#   "• bullet item" followed by three lines
#                      -> rich_text_list, and all three lines absorbed into the
#                         same list item
#
# Both are defects and neither is visible until someone reads the message. What
# survives the crossing unharmed is Slack's own <url|label> token, which is
# honoured inside markdown_text, and _italic_ and `code`, which mean the same in
# both dialects.


@pytest.mark.asyncio
async def test_the_stream_is_fed_markdown_not_mrkdwn(tmp_path) -> None:
    channel = _streaming_channel(tmp_path)
    client = _FakeStreamingClient()
    channel._client = client

    body = "**bold** and [label](https://example.com)\n- item\n## Heading\n"
    await _stream(channel, body)

    # Sent exactly as the model wrote it. Slack does the converting.
    assert _text_of(client.starts[0]) == body


@pytest.mark.parametrize(
    "written",
    [
        "**bold**\n",
        "- item\n",
        "- **Label:** body\n",
        "## Heading\n",
        "[label](https://example.com)\n",
        "> quoted\n",
        "1. first\n",
    ],
)
@pytest.mark.asyncio
async def test_every_construct_reaches_the_stream_as_it_was_written(
    tmp_path, written
) -> None:
    channel = _streaming_channel(tmp_path)
    client = _FakeStreamingClient()
    channel._client = client

    await _stream(channel, written)

    assert _text_of(client.starts[0]) == written
    # The dot the normaliser substitutes for a dash is the one that opened a
    # rich_text_list and swallowed the three lines after it into one item.
    assert "\u2022" not in _text_of(client.starts[0])


@pytest.mark.parametrize(
    "written",
    [
        "**bold**\n",
        "- item\n",
        "- **Label:** body\n",
        "## Heading\n",
        "[label](https://example.com)\n",
    ],
)
@pytest.mark.asyncio
async def test_the_constructs_the_normaliser_rewrites_are_the_ones_at_risk(
    tmp_path, written
) -> None:
    """Every construct where the two dialects disagree, and none is sent converted.

    Listed separately from the pass-through cases because these are the ones
    that would have shipped wrong: two were measured -- bold arriving as
    emphasis, a bullet building a list that swallows what follows -- and the
    rest are the same crossing, unmeasured. A blockquote and an ordered list are
    not here because the normaliser does not touch them.
    """
    channel = _streaming_channel(tmp_path)
    client = _FakeStreamingClient()
    channel._client = client

    converted = channel._normalize_slack_mrkdwn(written)
    assert converted != written, "not a construct the normaliser rewrites"

    await _stream(channel, written)

    assert _text_of(client.starts[0]) == written
    assert _text_of(client.starts[0]) != converted


@pytest.mark.asyncio
async def test_a_slack_token_the_model_wrote_crosses_untouched(tmp_path) -> None:
    """<url|label> is Slack's own and is honoured inside markdown_text."""
    channel = _streaming_channel(tmp_path)
    client = _FakeStreamingClient()
    channel._client = client

    # Two lines, because a line holding a pipe cannot be judged a table header
    # or not until the line under it has arrived.
    body = "ask <@U123> or see <https://example.com|the notes>\nthen carry on\n"
    await _stream(channel, body)

    assert _text_of(client.starts[0]) == body


@pytest.mark.asyncio
async def test_the_close_carries_markdown_and_the_blocks_carry_mrkdwn(
    tmp_path,
) -> None:
    """One remainder, two dialects, each where it belongs.

    A markdown_text chunk is Markdown because that is what the field reads. A
    blocks chunk is Block Kit, and a section holds mrkdwn like every other block
    this connector builds.
    """
    channel = _streaming_channel(tmp_path)
    client = _FakeStreamingClient()
    channel._client = client

    body = "**opening**\n\nthen **more** words\n\n| a | b |\n| --- | --- |\n| 1 | 2 |"
    await _stream(channel, "**opening**\n\n")
    await channel.send(_message(event_type=EventType.CHAT_FINAL, content=body))
    await _settle()

    assert _text_of(client.starts[0]) == "**opening**\n\n"
    blocks = client.stops[0]["chunks"][0]["blocks"]
    section = next(block for block in blocks if block["type"] == "section")
    # Converted for the block, which is the one place mrkdwn is right.
    assert "*more*" in section["text"]["text"]
    assert "**more**" not in section["text"]["text"]


@pytest.mark.asyncio
async def test_the_tail_reaches_the_close_as_markdown(tmp_path) -> None:
    channel = _streaming_channel(tmp_path)
    client = _FakeStreamingClient()
    channel._client = client

    await _stream(channel, "opening\n")
    await channel.send(
        _message(event_type=EventType.CHAT_FINAL, content="opening\n**the tail**")
    )
    await _settle()

    assert client.stops[0]["chunks"] == [
        {"type": "markdown_text", "text": "**the tail**"}
    ]


@pytest.mark.asyncio
async def test_the_rewrite_after_expiry_converts_back_to_mrkdwn(tmp_path) -> None:
    """chat.update is an ordinary write and takes the ordinary dialect.

    Including for the part that was already streamed: Slack converted that for
    itself on the way in, and this call replaces it, so it has to be converted
    again here.
    """
    channel = _streaming_channel(tmp_path)
    client = _FakeStreamingClient(
        stop_error=RuntimeError("message_not_in_streaming_state")
    )
    channel._client = client

    await _stream(channel, "**opening**\n")
    await channel.send(
        _message(
            event_type=EventType.CHAT_FINAL, content="**opening**\n**the tail**"
        )
    )
    await _settle()

    assert [update["text"] for update in client.updates] == [
        "*opening*\n*the tail*"
    ]


@pytest.mark.asyncio
async def test_the_message_posted_beside_a_dead_stream_is_mrkdwn(tmp_path) -> None:
    channel = _streaming_channel(tmp_path)
    client = _FakeStreamingClient(
        stop_error=RuntimeError("message_not_in_streaming_state")
    )
    channel._client = client

    opening = ("word " * 1200) + "\n"
    await _stream(channel, opening)
    await channel.send(
        _message(
            event_type=EventType.CHAT_FINAL, content=f"{opening}**the tail**"
        )
    )
    await _settle()

    assert [post["text"] for post in client.posts] == ["*the tail*"]


@pytest.mark.asyncio
async def test_a_reader_stopped_stream_is_continued_in_mrkdwn(tmp_path) -> None:
    channel = _streaming_channel(tmp_path)
    client = _FakeStreamingClient(
        append_error=RuntimeError("stopped_by_user"), append_error_after=0
    )
    channel._client = client

    await _stream(channel, "first\n", "**second**\n")
    await channel.send(
        _message(event_type=EventType.CHAT_FINAL, content="first\n**second**")
    )
    await _settle()

    assert [post["text"] for post in client.posts] == ["*second*"]


# --- the mode a stream is locked into by its opening call --------------------
#
# Measured: a stream opened with markdown_text refuses every chunk it is later
# sent -- markdown_text, plan_update, task_update and blocks alike -- with
# streaming_mode_mismatch. One opened with chunks accepts all of them. The lock
# is for the life of the stream and is documented nowhere.


@pytest.mark.asyncio
async def test_every_streaming_call_is_made_in_chunk_mode(tmp_path) -> None:
    """The guard against opening the cheaper-looking way again.

    A text-mode open costs nothing visible until the close, which is the one
    call that has to send chunks -- so the defect hides until a reply ends in
    something renderable, and then splits it into two messages.
    """
    channel = _streaming_channel(tmp_path)
    client = _FakeStreamingClient()
    channel._client = client

    body = "Here it is:\n\n| a | b |\n| --- | --- |\n| 1 | 2 |"
    await _stream(channel, "Here it is:\n\n", "| a | b |\n")
    await channel.send(_message(event_type=EventType.CHAT_FINAL, content=body))
    await _settle()

    for call in client.starts + client.appends + client.stops:
        assert "markdown_text" not in call, call
        assert "chunks" in call, call
        for chunk in call["chunks"]:
            assert chunk["type"] in ("markdown_text", "blocks"), chunk


@pytest.mark.asyncio
async def test_an_empty_opening_is_still_a_chunk(tmp_path) -> None:
    """The opening is a chunk even when there is nothing much in it yet."""
    channel = _streaming_channel(tmp_path)
    client = _FakeStreamingClient()
    channel._client = client

    await _stream(channel, "x\n")

    assert client.starts[0]["chunks"] == [{"type": "markdown_text", "text": "x\n"}]


@pytest.mark.asyncio
async def test_a_close_the_stream_will_never_accept_is_not_asked_twice(
    tmp_path, caplog
) -> None:
    """The live sequence, where two Slack errors contradicted each other.

    ``stopStream`` said ``streaming_mode_mismatch`` -- this payload is not
    acceptable -- and ``chat.update`` said ``streaming_state_conflict`` -- the
    message is still streaming. Both were true at once. Believing the second and
    retrying the first turned one failed close into two.
    """
    channel = _streaming_channel(tmp_path)

    class _MismatchingClient(_FakeStreamingClient):
        def __init__(self) -> None:
            super().__init__()
            self.stop_attempts = 0

        async def chat_stopStream(self, **kwargs: Any) -> dict[str, str]:
            self.stop_attempts += 1
            raise RuntimeError("streaming_mode_mismatch")

        async def chat_update(self, **kwargs: Any) -> dict[str, str]:
            raise RuntimeError("streaming_state_conflict")

    client = _MismatchingClient()
    channel._client = client

    await _stream(channel, "opening\n")
    with caplog.at_level(logging.WARNING, logger=slack_connect.logger.name):
        await channel.send(
            _message(
                event_type=EventType.CHAT_FINAL, content="opening\nthe rest of it"
            )
        )
        await _settle()

    assert client.stop_attempts == 1
    assert "not asking it twice" in caplog.text
    # The answer still lands, by the rung underneath.
    assert [post["text"] for post in client.posts] == ["the rest of it"]


def test_a_payload_rejection_is_not_resumed_from(tmp_path) -> None:
    surface = slack_connect._SlackStreamingSurface(
        _channel(tmp_path, enable_streaming=True), "C1", "1.1"
    )

    surface._latch("streaming_mode_mismatch")
    assert surface.resume() == ""
    assert surface.failed is True

    surface.failed = False
    surface._latch("service_unavailable")
    assert surface.resume() == "service_unavailable"
    assert surface.failed is False


@pytest.mark.asyncio
async def test_a_rendering_that_faults_the_stream_costs_the_rendering(
    tmp_path,
) -> None:
    """A "file" block faults the streaming methods rather than being refused.

    Measured three times out of three, on both append and stop, while the same
    block posts fine through chat.postMessage. Unreachable today -- nothing here
    builds a file block -- but a generic internal error that only ever arrives
    with blocks attached is worth one attempt without them, and the alternative
    is latching and splitting the reply in two.
    """
    channel = _streaming_channel(tmp_path)

    class _FaultingClient(_FakeStreamingClient):
        async def chat_stopStream(self, **kwargs: Any) -> dict[str, str]:
            if any(
                chunk.get("type") == "blocks" for chunk in kwargs.get("chunks") or []
            ):
                raise RuntimeError("internal_error")
            return await super().chat_stopStream(**kwargs)

    client = _FaultingClient()
    channel._client = client

    body = "Here it is:\n\n| a | b |\n| --- | --- |\n| 1 | 2 |"
    await _stream(channel, "Here it is:\n\n")
    await channel.send(_message(event_type=EventType.CHAT_FINAL, content=body))
    await _settle()

    assert len(client.stops) == 1
    assert client.stops[0]["chunks"] == [
        {"type": "markdown_text", "text": "| a | b |\n| --- | --- |\n| 1 | 2 |"}
    ]
    assert client.posts == []


# --- the ladder: stream, then edit, and off only when asked for --------------


async def _preview_shape(channel: SlackChannel, *, dm: bool) -> tuple[int, int]:
    """Post and update counts for one turn: the shape of the preview."""
    client = _FakeStreamingClient()
    channel._client = client
    build = (
        (lambda kind, text: _at_top_level(kind, text, "D5"))
        if dm
        else (lambda kind, text: _message(event_type=kind, content=text))
    )
    for delta in ("one\n", "two\n"):
        await channel.send(build(EventType.CHAT_DELTA, delta))
        await _settle()
    await channel.send(build(EventType.CHAT_FINAL, "one\ntwo"))
    await _settle()
    return len(client.posts), len(client.updates)


@pytest.mark.asyncio
async def test_stream_mode_is_never_worse_than_edit_mode(tmp_path) -> None:
    """The regression this ladder exists to prevent.

    Turning ``stream`` on removed the preview from every DM and every
    non-threaded channel: they went from the ``edit`` preview they had to a
    single post at the end of the turn. A mode meant to show more must not show
    less anywhere.
    """
    edit = await _preview_shape(_channel(tmp_path, enable_streaming="edit"), dm=True)
    stream = await _preview_shape(_streaming_channel(tmp_path), dm=True)

    assert stream == edit
    # And what "off" actually looks like, for contrast: one post, no preview.
    off = await _preview_shape(_channel(tmp_path, enable_streaming=False), dm=True)
    assert off == (1, 0)
    assert stream != off


@pytest.mark.asyncio
async def test_a_threaded_reply_still_gets_the_stream_itself(tmp_path) -> None:
    """The ladder's top rung is unchanged: where a stream fits, a stream is used."""
    channel = _streaming_channel(tmp_path)
    client = _FakeStreamingClient()
    channel._client = client

    await _stream(channel, "one\n", "two\n")

    assert len(client.starts) == 1
    assert client.posts == []
    assert client.updates == []


@pytest.mark.asyncio
async def test_a_stream_that_has_already_appended_is_never_swapped(
    tmp_path,
) -> None:
    """The fallback is for a stream that never opened, and only that.

    A message in streaming state refuses ``chat.update`` outright, so an edit
    surface could not write to it -- and the answer is not at risk either way,
    because the close and its recovery ladder deliver the whole of it.
    """
    channel = _streaming_channel(tmp_path)
    client = _FakeStreamingClient(
        append_error=RuntimeError("message_not_in_streaming_state"),
        append_error_after=1,
    )
    channel._client = client

    await _stream(channel, "one\n", "two\n", "three\n", "four\n")
    key = next(iter(channel._streams))
    assert isinstance(
        channel._streams[key].surface, slack_connect._SlackStreamingSurface
    )

    await channel.send(
        _message(event_type=EventType.CHAT_FINAL, content="one\ntwo\nthree\nfour")
    )
    await _settle()

    # No preview message was posted beside the stream, and the answer landed by
    # the recovery ladder rather than by a second surface.
    assert client.posts == []
    assert [update["text"] for update in client.updates] == [
        "one\ntwo\nthree\nfour"
    ]


# --- what Slack refused, in a form a log can be read for --------------------


class _FakeApiResponse:
    """The shape ``slack_sdk`` hangs on a ``SlackApiError``."""

    def __init__(self, data: dict[str, Any]) -> None:
        self.status_code = 200
        self.data = data
        self.headers: dict[str, Any] = {}


def _api_error(code: str, method: str = "chat.stopStream") -> Exception:
    """A refusal rendered the way ``slack_sdk`` renders one.

    Two lines, and the HTTP status on the first is 200 because Slack answers
    ``ok: false`` in the body of a successful request. Reproduced verbatim
    because the split across lines is the whole defect.
    """
    error = Exception(
        f"The request to the Slack API failed. "
        f"(url: https://slack.com/api/{method}, status: 200)\n"
        f"The server responded with: {{'ok': False, 'error': '{code}'}}"
    )
    error.response = _FakeApiResponse({"ok": False, "error": code})  # type: ignore[attr-defined]
    return error


def test_a_refusal_is_rendered_on_one_line_with_the_code_first() -> None:
    """A logger writes the two lines verbatim, and only the first is attributed.

    The line carrying the level and the module ends at ``status: 200`` and says
    nothing that can be acted on; the code lands on a following line belonging
    to no record at all.
    """
    reason = slack_connect.slack_failure_reason(_api_error("msg_too_long"))

    assert "\n" not in reason
    assert reason.startswith("msg_too_long: ")
    # The rest is kept: two refusals with one code are told apart by the URL.
    assert "chat.stopStream" in reason


def test_a_failure_with_no_slack_body_still_renders() -> None:
    """A transport error has no ``response``, and must not become empty."""
    assert slack_connect.slack_failure_reason(RuntimeError("connection reset")) == (
        "connection reset"
    )
    assert slack_connect.slack_failure_reason(RuntimeError()) == "RuntimeError"


@pytest.mark.asyncio
async def test_a_close_slack_refuses_names_the_code_in_the_warning(
    tmp_path, caplog: pytest.LogCaptureFixture
) -> None:
    """One grep over the log finds the code as well as the warning."""
    channel = _streaming_channel(tmp_path)
    channel._client = _FakeStreamingClient(stop_error=_api_error("msg_too_long"))

    await _stream(channel, "opening\n")
    with caplog.at_level(logging.WARNING, logger=slack_connect.logger.name):
        await channel.send(
            _message(event_type=EventType.CHAT_FINAL, content="opening\nthe rest")
        )
        await _settle()

    refusals = [
        record.getMessage()
        for record in caplog.records
        if "msg_too_long" in record.getMessage()
    ]
    assert refusals, caplog.text
    assert all("\n" not in message for message in refusals), refusals


# --- the part Slack cut off -------------------------------------------------


class _CuttingClient(_FakeSlackClient):
    """Stores only the first ``keep`` characters of anything posted.

    Slack does this silently: the call answers ``ok`` and hands back a ts, and
    the reply is short on screen with nothing to say where it stopped.
    """

    def __init__(self, *, keep: int) -> None:
        super().__init__()
        self._keep = keep

    async def chat_postMessage(self, **kwargs: Any) -> dict[str, Any]:
        self.posts.append(kwargs)
        text = str(kwargs.get("text") or "")
        return {
            "ts": f"1710000099.{len(self.posts):06d}",
            "message": {"text": text[: self._keep]},
        }


@pytest.mark.asyncio
async def test_what_slack_cut_is_posted_back_under_a_marker(
    tmp_path, caplog: pytest.LogCaptureFixture
) -> None:
    """The warning goes to a log; the reply stays incomplete on screen.

    The reader sees an answer that stops mid-sentence and reads it as finished.
    The echo says how much survived and the sender still holds what it sent, so
    what was lost is known exactly and is owed back.
    """
    channel = _channel(tmp_path)
    client = _CuttingClient(keep=20)
    channel._client = client

    with caplog.at_level(logging.WARNING, logger=slack_connect.logger.name):
        sent, _ts, error = await channel._post_text(
            channel_id="C1",
            text="A" * 20 + "the part that was dropped",
            thread_ts="1710000000.000100",
        )

    assert (sent, error) == (True, "")
    assert "Slack stored less text than was sent" in caplog.text
    assert len(client.posts) == 2
    continuation = client.posts[1]["text"]
    assert continuation.startswith(slack_connect._SLACK_TRUNCATION_NOTICE)
    assert continuation.endswith("the part that was dropped")
    # Beside the message it continues, not adrift in the channel.
    assert client.posts[1]["thread_ts"] == "1710000000.000100"


@pytest.mark.asyncio
async def test_the_continuation_is_not_itself_continued(tmp_path) -> None:
    """One pass, so a channel that truncates everything cannot open a chain."""
    channel = _channel(tmp_path)
    client = _CuttingClient(keep=5)
    channel._client = client

    await channel._post_text(
        channel_id="C1",
        text="a much longer message than five characters",
        thread_ts="",
    )

    assert len(client.posts) == 2


@pytest.mark.asyncio
async def test_a_truncated_edit_is_not_repaired_by_a_second_message(
    tmp_path, caplog: pytest.LogCaptureFixture
) -> None:
    """An edit is one of many rewrites of a message still being written.

    Posting its shortfall beside it would put a fragment of an unfinished reply
    into the conversation, and the next rewrite would carry the same text again.
    """

    class _CuttingUpdateClient(_FakeSlackClient):
        async def chat_update(self, **kwargs: Any) -> dict[str, Any]:
            self.updates.append(kwargs)
            return {
                "ts": str(kwargs.get("ts") or ""),
                "message": {"text": str(kwargs.get("text") or "")[:4]},
            }

    channel = _channel(tmp_path)
    client = _CuttingUpdateClient()
    channel._client = client

    await channel.send(_message(content="Hello"))
    await _settle()
    with caplog.at_level(logging.WARNING, logger=slack_connect.logger.name):
        await channel.send(
            _message(event_type=EventType.CHAT_FINAL, content="Hello and then some")
        )
        await _settle()

    assert "Slack stored less text than was sent" in caplog.text
    assert len(client.posts) == 1


def _split_like_slack(text: str, limit: int) -> list[str]:
    """Break *text* the way Slack breaks an oversized post.

    Measured against a live workspace: chat.postMessage does not refuse a
    ``text`` longer than one message holds and does not truncate it either. It
    stores the payload as several messages cut at line boundaries, in order,
    and answers with the ts and the text of the last one alone.
    """
    parts: list[str] = []
    current = ""
    for line in text.splitlines(keepends=True):
        if current and len(current) + len(line) > limit:
            parts.append(current.rstrip("\n"))
            current = ""
        current += line
    parts.append(current.rstrip("\n"))
    return parts


class _SplittingClient(_FakeStreamingClient):
    """A Slack that splits long posts and names only the last message.

    ``stored`` is every message that reached the channel, in order. The API
    reply names only the last of them.
    """

    def __init__(self, *, limit: int, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._limit = limit
        self.stored: list[str] = []

    async def chat_postMessage(self, **kwargs: Any) -> dict[str, Any]:
        self.posts.append(kwargs)
        parts = _split_like_slack(str(kwargs.get("text") or ""), self._limit)
        self.stored.extend(parts)
        return {
            "ts": f"1710000098.{len(self.stored):06d}",
            "message": {"text": parts[-1]},
        }


def _numbered(first: int, last: int) -> str:
    return "".join(
        f"{number}. the quick brown fox jumps over the lazy dog\n"
        for number in range(first, last + 1)
    )


@pytest.mark.asyncio
async def test_a_reply_slack_split_for_itself_gets_no_continuation(
    tmp_path, caplog: pytest.LogCaptureFixture
) -> None:
    """Every character reached the reader, across messages the API never named.

    The abandoned stream hands Slack the whole remainder in one post, and Slack
    stores it as several messages of its own. The echo is then the end of the
    payload rather than the start of it, so nothing is owed back. Reading its
    length alone reports the earlier messages as lost and posts them again
    underneath.
    """
    channel = _streaming_channel(tmp_path)
    client = _SplittingClient(
        limit=3900, stop_error=RuntimeError("message_not_in_streaming_state")
    )
    channel._client = client

    opening = _numbered(1, 60)
    whole = _numbered(1, 250)
    await _stream(channel, opening)
    with caplog.at_level(logging.INFO, logger=slack_connect.logger.name):
        await channel.send(_message(event_type=EventType.CHAT_FINAL, content=whole))
        await _settle()

    assert len(client.posts) == 1
    assert slack_connect._SLACK_TRUNCATION_NOTICE not in "".join(client.stored)
    # The remainder is on screen once, in order, with the split points the only
    # difference between it and what was sent.
    assert "\n".join(client.stored) == _numbered(61, 250).strip()
    assert "Slack split what was sent into messages of its own" in caplog.text
    assert "Slack stored less text than was sent" not in caplog.text


@pytest.mark.asyncio
async def test_the_continuation_starts_where_the_stored_text_ends(
    tmp_path,
) -> None:
    """A cut message is repaired from the point its stored text ends at.

    The echo is the start of what was sent, so the count of characters that
    survived is also the offset the rest begins at.
    """
    channel = _channel(tmp_path)
    kept = _numbered(1, 10)
    channel._client = _CuttingClient(keep=len(kept))
    client = channel._client

    await channel._post_text(
        channel_id="C1",
        text=_numbered(1, 20),
        thread_ts="1710000000.000100",
    )

    assert len(client.posts) == 2
    continuation = client.posts[1]["text"]
    assert continuation == (
        f"{slack_connect._SLACK_TRUNCATION_NOTICE}\n\n{_numbered(11, 20).strip()}"
    )


@pytest.mark.asyncio
async def test_an_echo_rewritten_in_the_middle_gets_no_continuation(
    tmp_path, caplog: pytest.LogCaptureFixture
) -> None:
    """Slack normalises what it stores, which shortens an echo it did not cut.

    An echo that is neither the start nor the end of what was sent cannot be
    placed in it, so no offset into the sent text names a remainder. Posting
    one anyway would put a slice of a delivered message back on screen.
    """

    class _RewritingClient(_FakeSlackClient):
        async def chat_postMessage(self, **kwargs: Any) -> dict[str, Any]:
            self.posts.append(kwargs)
            text = str(kwargs.get("text") or "")
            return {
                "ts": f"1710000099.{len(self.posts):06d}",
                "message": {"text": text.replace("<https://example.test|here>", "x")},
            }

    channel = _channel(tmp_path)
    client = _RewritingClient()
    channel._client = client

    with caplog.at_level(logging.WARNING, logger=slack_connect.logger.name):
        await channel._post_text(
            channel_id="C1",
            text="The details are <https://example.test|here>, and that is all.",
            thread_ts="1710000000.000100",
        )

    assert len(client.posts) == 1
    assert "neither the start nor the end of what was sent" in caplog.text


@pytest.mark.asyncio
async def test_a_cut_notification_fallback_gets_no_continuation(
    tmp_path, caplog: pytest.LogCaptureFixture
) -> None:
    """Blocks render such a message; ``text`` is the fallback beside them.

    Slack stores a long fallback cut to its own ceiling while the blocks arrive
    whole, so the reader is missing nothing and a continuation would show the
    end of the message twice.
    """

    class _FallbackCuttingClient(_FakeSlackClient):
        async def chat_postMessage(self, **kwargs: Any) -> dict[str, Any]:
            self.posts.append(kwargs)
            return {
                "ts": f"1710000099.{len(self.posts):06d}",
                "message": {"text": str(kwargs.get("text") or "")[:40]},
            }

    channel = _channel(tmp_path, blockkit_validate="off")
    client = _FallbackCuttingClient()
    channel._client = client

    with caplog.at_level(logging.INFO, logger=slack_connect.logger.name):
        await channel._post_text(
            channel_id="C1",
            text=_numbered(1, 10),
            thread_ts="1710000000.000100",
            blocks=[{"type": "section", "text": {"type": "mrkdwn", "text": "table"}}],
        )

    assert len(client.posts) == 1
    assert "the text Slack cut is the fallback beside a Block Kit rendering" in (
        caplog.text
    )
