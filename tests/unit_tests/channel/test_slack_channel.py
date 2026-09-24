"""Unit tests for the Slack Socket Mode channel."""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from jiuwenswarm.common.schema.message import EventType, Message
from jiuwenswarm.gateway.channel_manager.base import RobotMessageRouter
from jiuwenswarm.gateway.channel_manager.im_platforms.slack import slack_connect
from jiuwenswarm.gateway.channel_manager.im_platforms.slack.slack_connect import (
    SlackChannel,
    SlackChannelConfig,
    SlackChannelOverride,
    SlackDeliveryError,
)
from jiuwenswarm.gateway.routing.keys import SlackDeliveryTarget, make_delivery_target
from jiuwenswarm.gateway.routing.session_sharing import RoutingTarget


@pytest.fixture(autouse=True)
def _isolated_dedup_store(tmp_path, monkeypatch):
    """Give every test its own dedup file.

    The store is durable by design, so without this the whole module shares one
    window: parametrised cases reusing a message ts would see each other's
    entries, and a stray run would write into the real workspace.
    """
    monkeypatch.setattr(
        slack_connect.SlackEventDedupStore,
        "__init__",
        lambda self, path=None, **kw: _real_store_init(
            self, path or tmp_path / "slack_seen_events.json", **kw
        ),
    )


_real_store_init = slack_connect.SlackEventDedupStore.__init__


def _message(
    *,
    event_type: EventType = EventType.CHAT_FINAL,
    content: str = "response",
    metadata: dict[str, Any] | None = None,
    session_id: str = "slack_T1_C1_1710000000.000100",
) -> Message:
    return Message(
        id="response-1",
        type="event",
        channel_id="slack",
        session_id=session_id,
        params={},
        timestamp=time.time(),
        ok=True,
        payload={"content": content},
        event_type=event_type,
        metadata=metadata,
    )


@pytest.mark.asyncio
async def test_app_mention_creates_thread_scoped_message_and_deduplicates() -> None:
    channel = SlackChannel(
        SlackChannelConfig(
            enabled=True,
            allow_from=["U1"],
            allowed_channel_ids=["C1"],
            history="origin",
            reply_in_thread=True,
        ),
        RobotMessageRouter(),
    )
    channel._running = True
    received: list[Message] = []
    channel.on_message(received.append)

    event = {
        "type": "app_mention",
        "user": "U1",
        "channel": "C1",
        "channel_type": "channel",
        "text": "<@U-BOT> summarize this",
        "ts": "1710000000.000100",
    }
    body = {
        "event_id": "Ev1",
        "team_id": "T1",
        "authorizations": [{"user_id": "U-BOT", "is_bot": True}],
    }

    await channel._handle_app_mention(event, body)
    await channel._handle_app_mention(event, body)

    assert len(received) == 1
    message = received[0]
    marked = "summarize this\n\n[ts: 1710000000.000100]"
    assert message.params == {"content": marked, "query": marked}
    assert message.session_id == "slack_T1_C1_1710000000.000100"
    assert message.chat_id == "C1"
    assert message.user_id == "U1"
    assert message.metadata == {
        "user_id": "U1",
        "slack_event_id": "Ev1",
        "slack_team_id": "T1",
        "slack_channel_id": "C1",
        "slack_channel_type": "channel",
        "slack_user_id": "U1",
        "slack_message_ts": "1710000000.000100",
        "message_ts": "1710000000.000100",
        "slack_thread_ts": "1710000000.000100",
        "slack_history_policy": "origin",
        "slack_history_never_read": [],
        "slack_history_exempt_members": [],
        # The posting ladder, stamped on every inbound request beside the
        # reading one and always present: an absent word means no Slack
        # connector settled this request, which the runtime reads as a refusal,
        # while a connector that settled it to "disabled" is a different fact.
        "slack_write_policy": "disabled",
        "slack_trigger": "mention",
        # This message opened with a mention of the bot. The key decides no
        # delivery -- it names which case a withheld reply was, for the log --
        # and is stamped only where it is true, so every other message in this
        # file arrives without it.
        "slack_addressed": True,
    }


@pytest.mark.asyncio
async def test_history_origin_stamps_the_policy_on_any_channel() -> None:
    channel = SlackChannel(
        SlackChannelConfig(
            enabled=True,
            allow_from=["U1"],
            allowed_channel_ids=[],
            history="origin",
        ),
        RobotMessageRouter(),
    )
    channel._running = True
    received: list[Message] = []
    channel.on_message(received.append)

    await channel._handle_app_mention(
        {
            "type": "app_mention",
            "user": "U1",
            "channel": "C-ANY",
            "channel_type": "channel",
            "text": "<@U-BOT> summarize this channel",
            "ts": "1710000000.000200",
        },
        {
            "event_id": "EvWildcard",
            "team_id": "T1",
            "authorizations": [{"user_id": "U-BOT", "is_bot": True}],
        },
    )

    assert len(received) == 1
    assert received[0].metadata["slack_channel_id"] == "C-ANY"
    assert received[0].metadata["slack_history_policy"] == "origin"


@pytest.mark.asyncio
async def test_direct_message_is_not_restricted_by_channel_allowlist() -> None:
    channel = SlackChannel(
        SlackChannelConfig(
            enabled=True,
            allow_from=["U1"],
            allowed_channel_ids=["C-ONLY"],
        ),
        RobotMessageRouter(),
    )
    channel._running = True
    received: list[Message] = []
    channel.on_message(received.append)

    await channel._handle_message_event(
        {
            "type": "message",
            "channel_type": "im",
            "channel": "D1",
            "user": "U1",
            "text": "hello",
            "ts": "1710000001.000200",
        },
        {"event_id": "Ev2", "team_id": "T1"},
    )

    assert len(received) == 1
    assert received[0].session_id == "slack_T1_D1_U1"
    assert received[0].metadata["slack_thread_ts"] == ""
    assert received[0].metadata["slack_history_policy"] == "disabled"


@pytest.mark.asyncio
async def test_event_filters_reject_bots_subtypes_users_and_channels() -> None:
    channel = SlackChannel(
        SlackChannelConfig(
            enabled=True,
            allow_from=["U1"],
            allowed_channel_ids=["C1"],
        ),
        RobotMessageRouter(),
    )
    channel._running = True
    received: list[Message] = []
    channel.on_message(received.append)
    base_event = {
        "type": "app_mention",
        "user": "U1",
        "channel": "C1",
        "text": "<@B1> hello",
        "ts": "1710000002.000300",
    }

    await channel._handle_app_mention(
        {**base_event, "bot_id": "B1"}, {"event_id": "Ev3", "team_id": "T1"}
    )
    await channel._handle_app_mention(
        {**base_event, "subtype": "message_changed"},
        {"event_id": "Ev4", "team_id": "T1"},
    )
    await channel._handle_app_mention(
        {**base_event, "user": "U2"},
        {"event_id": "Ev5", "team_id": "T1"},
    )
    await channel._handle_app_mention(
        {**base_event, "channel": "C2"},
        {"event_id": "Ev6", "team_id": "T1"},
    )

    assert received == []


def _file_share_dm(
    *,
    text: str = "have a look at this",
    ts: str = "1710000010.000100",
    user: str = "U1",
) -> dict[str, Any]:
    """A direct message whose attachment made Slack tag it ``file_share``."""
    return {
        "type": "message",
        "subtype": "file_share",
        "channel_type": "im",
        "channel": "D1",
        "user": user,
        "text": text,
        "ts": ts,
        "files": [
            {
                "id": "F1",
                "name": "notes.txt",
                "mimetype": "text/plain",
                "url_private_download": "https://files.slack.com/files-pri/T1-F1/notes.txt",
            }
        ],
    }


class _FakeStreamResponse:
    def __init__(
        self,
        status_code: int,
        content_type: str,
        chunks: tuple[bytes, ...],
        location: str = "",
    ) -> None:
        self.status_code = status_code
        self.headers = {"content-type": content_type}
        if location:
            self.headers["location"] = location
        self._chunks = chunks

    async def aiter_bytes(self):
        for chunk in self._chunks:
            yield chunk


class _FakeStreamContext:
    def __init__(self, response: _FakeStreamResponse) -> None:
        self._response = response

    async def __aenter__(self) -> _FakeStreamResponse:
        return self._response

    async def __aexit__(self, *_exc: Any) -> bool:
        return False


def _stub_downloads(
    monkeypatch,
    tmp_path,
    *,
    status: int = 200,
    content_type: str = "text/plain",
    chunks: tuple[bytes, ...] = (b"file body",),
    redirect_to: str = "",
) -> list[dict[str, Any]]:
    """Route attachment downloads to a fake transport and a temporary upload root.

    Returns the list the GET calls are recorded into, so a test can assert what
    URL was fetched and with which headers.

    ``redirect_to`` makes the first request answer 302 towards it, which is how
    a test reaches the per-hop half of the check without a second stub.
    """
    requests: list[dict[str, Any]] = []

    class _FakeAsyncClient:
        def __init__(self, **kwargs: Any) -> None:
            self.kwargs = kwargs

        async def __aenter__(self) -> "_FakeAsyncClient":
            return self

        async def __aexit__(self, *_exc: Any) -> bool:
            return False

        def stream(
            self, method: str, url: str, headers: dict[str, str] | None = None
        ) -> _FakeStreamContext:
            requests.append({"method": method, "url": url, "headers": headers or {}})
            if redirect_to and len(requests) == 1:
                return _FakeStreamContext(
                    _FakeStreamResponse(302, content_type, (), redirect_to)
                )
            return _FakeStreamContext(
                _FakeStreamResponse(status, content_type, chunks)
            )

    monkeypatch.setattr(
        slack_connect,
        "httpx",
        SimpleNamespace(AsyncClient=_FakeAsyncClient, Timeout=lambda *a, **kw: None),
    )
    monkeypatch.setattr(slack_connect, "get_agent_sessions_dir", lambda: tmp_path)
    return requests


@pytest.mark.asyncio
async def test_file_share_message_is_not_dropped_as_a_subtype(
    monkeypatch, tmp_path
) -> None:
    _stub_downloads(monkeypatch, tmp_path)
    channel = SlackChannel(
        SlackChannelConfig(enabled=True, allow_from=["U1"]),
        RobotMessageRouter(),
    )
    channel._running = True
    received: list[Message] = []
    channel.on_message(received.append)

    await channel._handle_message_event(
        _file_share_dm(), {"event_id": "Ev-file", "team_id": "T1"}
    )

    assert len(received) == 1
    assert received[0].params["content"].startswith("have a look at this")


@pytest.mark.asyncio
async def test_bot_echoes_are_still_dropped_after_file_shares_are_admitted() -> None:
    """The narrowed subtype guard must not reopen the self-reply loop."""
    channel = SlackChannel(
        SlackChannelConfig(enabled=True, allow_from=["U1", "U-BOT"]),
        RobotMessageRouter(),
    )
    channel._running = True
    channel._bot_user_id = "U-BOT"
    received: list[Message] = []
    channel.on_message(received.append)

    # An app-posted message, identified by bot_id / bot_profile.
    await channel._handle_message_event(
        {**_file_share_dm(ts="1710000011.000100"), "bot_id": "B1"},
        {"event_id": "Ev-bot-id", "team_id": "T1"},
    )
    await channel._handle_message_event(
        {**_file_share_dm(ts="1710000012.000100"), "bot_profile": {"id": "B1"}},
        {"event_id": "Ev-bot-profile", "team_id": "T1"},
    )
    # A file this bot uploaded: neither field is set, only the poster identity.
    await channel._handle_message_event(
        _file_share_dm(ts="1710000013.000100", user="U-BOT"),
        {"event_id": "Ev-own-upload", "team_id": "T1"},
    )
    # Subtypes that are not user content stay filtered out.
    await channel._handle_message_event(
        {
            "type": "message",
            "subtype": "message_changed",
            "channel_type": "im",
            "channel": "D1",
            "user": "U1",
            "text": "edited",
            "ts": "1710000014.000100",
        },
        {"event_id": "Ev-changed", "team_id": "T1"},
    )

    assert received == []


@pytest.mark.asyncio
async def test_a_reply_broadcast_to_the_channel_is_still_the_senders_content() -> None:
    """``thread_broadcast`` is an ordinary reply the sender also sent to root.

    The subtype guard is an allow-list, so every subtype not named in it is
    discarded unread. That is the safe direction for the loop guard it also
    serves and the unsafe one for content: a broadcast reply is text a person
    typed, and it was dropped for having a subtype at all.
    """
    channel = SlackChannel(
        SlackChannelConfig(enabled=True, group_chat_mode="all"),
        RobotMessageRouter(),
    )
    channel._running = True
    channel._bot_user_id = "U-BOT"
    received: list[Message] = []
    channel.on_message(received.append)

    await channel._handle_message_event(
        {
            "type": "message",
            "subtype": "thread_broadcast",
            "channel_type": "channel",
            "channel": "C-BROADCAST",
            "user": "U1",
            "text": "posting this at root so everyone sees it",
            "ts": "1710000400.000200",
            "thread_ts": "1710000400.000100",
            "root": {"ts": "1710000400.000100", "text": "the thread root"},
        },
        {"event_id": "Ev-broadcast", "team_id": "T1"},
    )

    assert len(received) == 1
    assert received[0].params["content"].startswith(
        "posting this at root so everyone sees it"
    )
    # The root message is repeated on the event and is not this sender's post.
    assert "the thread root" not in received[0].params["content"]


def _attachment_channel(**overrides: Any) -> tuple[SlackChannel, _FakeSlackClient, list[Message]]:
    channel = SlackChannel(
        SlackChannelConfig(
            enabled=True,
            allow_from=["U1"],
            acknowledge_mode="off",
            bot_token="xoxb-test",
            **overrides,
        ),
        RobotMessageRouter(),
    )
    channel._running = True
    client = _FakeSlackClient()
    channel._client = client
    received: list[Message] = []
    channel.on_message(received.append)
    return channel, client, received


def _end_turn(channel: SlackChannel, request: Message) -> None:
    """Close the turn ``request`` opened, so the next message starts its own.

    A message arriving while a turn is running is held rather than dispatched,
    because ``queue`` is what a conversation nobody configured does. A test
    wanting two dispatches wants two turns, and this is the first one ending --
    the connector's own bookkeeping, without the reply delivery a terminal
    event would also perform, which these tests are not about.
    """
    channel._forget_turn_initiator(request.session_id, request.id)


@pytest.mark.asyncio
async def test_inbound_attachment_is_downloaded_and_handed_to_the_agent(
    monkeypatch, tmp_path
) -> None:
    requests = _stub_downloads(monkeypatch, tmp_path, chunks=(b"line one\n", b"line two"))
    channel, client, received = _attachment_channel()

    await channel._handle_message_event(
        _file_share_dm(text=""), {"event_id": "Ev-download", "team_id": "T1"}
    )

    # url_private is fetched with the bot token; a plain GET gets a login page.
    assert requests[0]["url"].endswith("/notes.txt")
    assert requests[0]["headers"]["Authorization"] == "Bearer xoxb-test"

    assert client.calls == []  # nothing failed, so nothing to apologise for
    assert len(received) == 1
    documents = received[0].params["files"]["uploaded_documents"]
    assert len(documents) == 1
    stored = Path(documents[0]["path"])
    assert stored.read_bytes() == b"line one\nline two"
    assert stored.parent == tmp_path / "slack_T1_D1_U1" / "uploads"
    assert documents[0]["mime_type"] == "text/plain"
    assert documents[0]["size_bytes"] == 17
    # An attachment with no comment is still a request: the description is what
    # keeps it from being discarded as an empty message.
    assert str(stored) in received[0].params["content"]
    assert received[0].params["content"] == received[0].params["query"]


# The instruction a channel prompt puts under every message from a conversation
# it governs. Long enough that a test asserting on it cannot match by accident.
_STANDING_PROMPT = "Answer in one paragraph, and name the file you read."


def _prompted_channel(**overrides: Any):
    """An attachment channel whose every message gets a standing prompt appended."""
    return _attachment_channel(
        platform_override=SlackChannelOverride(prompt=_STANDING_PROMPT), **overrides
    )


def _empty_dm(ts: str = "1710000200.000100") -> dict[str, Any]:
    """A direct message holding no text and no file at all.

    Slack sends these: an unsupported block, an attachment removed between the
    post and the event, a message whose whole body was an emoji Slack strips.
    """
    return {
        "type": "message",
        "channel_type": "im",
        "channel": "D1",
        "user": "U1",
        "text": "",
        "ts": ts,
    }


@pytest.mark.asyncio
async def test_a_message_holding_nothing_the_sender_wrote_dispatches_no_turn() -> None:
    """The append must not be what makes an empty message look like a request.

    Everything appended is written by this connector. Appended before the
    emptiness check, a message holding no text and no readable file is made
    non-empty by the connector's own additions and dispatched as a turn whose
    entire content is text nobody asked for.
    """
    channel, _client, received = _prompted_channel()

    await channel._handle_message_event(
        _empty_dm(), {"event_id": "Ev-empty", "team_id": "T1"}
    )

    assert received == []


@pytest.mark.asyncio
async def test_a_file_posted_with_no_comment_is_dispatched_and_gets_the_prompt(
    monkeypatch, tmp_path
) -> None:
    """The attachment path fills the text in before the check, and still appends.

    ``_describe_attachments`` is what makes a file posted with no comment a
    request, so this message reaches the append and is dispatched like any
    other.
    """
    _stub_downloads(monkeypatch, tmp_path)
    channel, _client, received = _prompted_channel()

    await channel._handle_message_event(
        _file_share_dm(text=""), {"event_id": "Ev-file-only", "team_id": "T1"}
    )

    assert len(received) == 1
    content = received[0].params["content"]
    assert "notes.txt" in content
    assert content.endswith(f"\n\n{_STANDING_PROMPT}")


@pytest.mark.asyncio
async def test_a_message_with_text_gets_the_append_it_always_got() -> None:
    """A message that was never empty gets the marker and the standing prompt."""
    channel, _client, received = _prompted_channel()

    await channel._handle_message_event(
        {**_empty_dm(ts="1710000201.000100"), "text": "where did the build stop"},
        {"event_id": "Ev-text", "team_id": "T1"},
    )

    assert len(received) == 1
    assert received[0].params["content"] == (
        f"where did the build stop\n\n[ts: 1710000201.000100]\n\n{_STANDING_PROMPT}"
    )


def _attachment_only_dm(
    attachments: list[dict[str, Any]],
    *,
    ts: str = "1710000300.000100",
) -> dict[str, Any]:
    """A direct message whose whole content sits in Slack's ``attachments``.

    Shaped on a real payload: an empty ``text``, no ``files`` key at all, and
    one attachment entry. Slack sends this for a GIF, for a shared message and
    for some link unfurls, and a human in the channel sees content in every one
    of them.
    """
    return {
        "type": "message",
        "channel_type": "im",
        "channel": "D1",
        "user": "U1",
        "text": "",
        "ts": ts,
        "attachments": attachments,
    }


_SHARED_GIF = {
    "id": 1,
    "fallback": "shared a GIF",
    "blocks": [
        {
            "type": "image",
            "alt_text": "a cat sliding off a shelf",
            "image_url": "https://example.invalid/cat.gif",
        }
    ],
}

_LINK_UNFURL = {
    "id": 1,
    "fallback": "Postgres 18 release notes",
    "service_name": "example.invalid",
    "title": "Postgres 18 release notes",
    "title_link": "https://example.invalid/pg18",
    "text": "Asynchronous I/O is on by default.",
}


@pytest.mark.asyncio
async def test_an_attachment_only_message_is_dispatched_rather_than_dropped() -> None:
    """A message whose only content is an attachment is not an empty message.

    The emptiness check tested text and files. Slack puts a GIF, a shared
    message and some unfurls in ``attachments`` with neither of those set, so a
    message everyone in the channel saw as content produced no turn at all.
    """
    channel, _client, received = _attachment_channel()

    await channel._handle_message_event(
        _attachment_only_dm([_SHARED_GIF]), {"event_id": "Ev-gif", "team_id": "T1"}
    )

    assert len(received) == 1


@pytest.mark.asyncio
async def test_what_an_attachment_shows_reaches_what_the_model_receives() -> None:
    """Counting the attachment is not enough on its own.

    A message counted as non-empty and lifted from would otherwise reach the
    agent with nothing in it, leaving a turn to acknowledge a post it was never
    told anything about. The walk reaches the blocks nested inside the
    attachment, which is where a GIF states what it shows.
    """
    channel, _client, received = _attachment_channel()

    await channel._handle_message_event(
        _attachment_only_dm([_SHARED_GIF]), {"event_id": "Ev-gif-text", "team_id": "T1"}
    )

    content = received[0].params["content"]
    assert "shared a GIF" in content
    assert "a cat sliding off a shelf" in content
    # An image URL is not text and says nothing a reader can act on.
    assert "cat.gif" not in content


@pytest.mark.asyncio
async def test_an_unfurl_lifts_its_title_and_text_beside_its_fallback() -> None:
    """``fallback`` alone is a summary; the richer fields are the content.

    An unfurl states the page title, a description and the source separately,
    and a lift that took only ``fallback`` would report the title twice and the
    description not at all.
    """
    channel, _client, received = _attachment_channel()

    await channel._handle_message_event(
        _attachment_only_dm([_LINK_UNFURL]),
        {"event_id": "Ev-unfurl", "team_id": "T1"},
    )

    content = received[0].params["content"]
    assert "Postgres 18 release notes" in content
    assert "Asynchronous I/O is on by default." in content
    assert "https://example.invalid/pg18" in content
    assert "example.invalid" in content
    # Slack states the title in ``fallback`` and again in ``title``. Saying it
    # twice spends the window on nothing.
    assert content.count("Postgres 18 release notes") == 1


@pytest.mark.asyncio
async def test_an_attachment_with_nothing_readable_still_says_one_arrived() -> None:
    """An attachment holding no text at all is still something a human saw.

    Reporting that one arrived is what lets the turn ask about it. The
    alternative is the silence this whole path exists to end.
    """
    channel, _client, received = _attachment_channel()

    await channel._handle_message_event(
        _attachment_only_dm([{"id": 1, "image_url": "https://example.invalid/x.png"}]),
        {"event_id": "Ev-bare", "team_id": "T1"},
    )

    assert len(received) == 1
    assert "no readable text" in received[0].params["content"]


@pytest.mark.asyncio
async def test_lifted_attachment_text_is_kept_apart_from_what_the_sender_typed() -> None:
    """Nobody typed "shared a GIF"; Slack wrote it to say what was posted.

    A model quoting the message back has to be able to tell the two apart, so
    the lift sits under a header of its own rather than being folded into the
    sender's sentence.
    """
    channel, _client, received = _attachment_channel()

    event = _attachment_only_dm([_SHARED_GIF], ts="1710000301.000100")
    event["text"] = "look at this"
    await channel._handle_message_event(
        event, {"event_id": "Ev-gif-comment", "team_id": "T1"}
    )

    content = received[0].params["content"]
    assert content.startswith("look at this\n\nAttachment shown on this message:")


@pytest.mark.asyncio
async def test_an_empty_attachments_array_leaves_a_message_as_empty_as_it_was() -> None:
    """Only what the sender contributed may satisfy the emptiness check.

    An ``attachments`` key set to an empty list is the same message as one with
    no such key. Counting the key rather than its entries would make the
    connector's own appended prompt the entire content of a dispatched turn,
    which is the defect the append was moved below the check to fix.
    """
    channel, _client, received = _prompted_channel()

    await channel._handle_message_event(
        _attachment_only_dm([], ts="1710000302.000100"),
        {"event_id": "Ev-no-attachments", "team_id": "T1"},
    )

    assert received == []


@pytest.mark.asyncio
async def test_a_message_with_text_and_no_attachments_reads_as_it_always_did() -> None:
    """An ordinary message gains no header it did not have before."""
    channel, _client, received = _attachment_channel()

    await channel._handle_message_event(
        {**_empty_dm(ts="1710000303.000100"), "text": "where did the build stop"},
        {"event_id": "Ev-plain", "team_id": "T1"},
    )

    assert received[0].params["content"] == (
        "where did the build stop\n\n[ts: 1710000303.000100]"
    )


def test_collect_event_attachments_ignores_malformed_payloads() -> None:
    assert SlackChannel._collect_event_attachments({}) == []
    assert SlackChannel._collect_event_attachments({"attachments": "A1"}) == []
    assert SlackChannel._collect_event_attachments(
        {"attachments": [{"id": 1}, "A2"]}
    ) == [{"id": 1}]


@pytest.mark.asyncio
async def test_inbound_image_is_offered_on_the_multimodal_path(
    monkeypatch, tmp_path
) -> None:
    _stub_downloads(monkeypatch, tmp_path, content_type="image/png", chunks=(b"\x89PNG",))
    channel, _client, received = _attachment_channel()

    event = _file_share_dm(text="what is this")
    event["files"] = [
        {
            "id": "F9",
            "name": "chart.png",
            "mimetype": "image/png",
            "url_private": "https://files.slack.com/files-pri/T1-F9/chart.png",
        }
    ]
    await channel._handle_message_event(
        event, {"event_id": "Ev-image", "team_id": "T1"}
    )

    params = received[0].params
    assert "uploaded_documents" not in params["files"]
    assert params["files"]["uploaded_images"][0]["type"] == "image"
    assert params["media_items"] == params["files"]["uploaded_images"]


@pytest.mark.asyncio
async def test_attachment_without_an_inline_url_is_resolved_via_files_info(
    monkeypatch, tmp_path
) -> None:
    requests = _stub_downloads(monkeypatch, tmp_path)

    class _FilesInfoClient(_FakeSlackClient):
        def __init__(self) -> None:
            super().__init__()
            self.looked_up: list[str] = []

        async def files_info(self, **kwargs: Any) -> dict[str, Any]:
            self.looked_up.append(kwargs["file"])
            return {
                "file": {
                    "id": kwargs["file"],
                    "name": "late.txt",
                    "mimetype": "text/plain",
                    "url_private": "https://files.slack.com/files-pri/T1-F7/late.txt",
                }
            }

    channel, _client, received = _attachment_channel()
    channel._client = _FilesInfoClient()

    event = _file_share_dm(text="")
    event["files"] = [{"id": "F7"}]
    await channel._handle_message_event(
        event, {"event_id": "Ev-files-info", "team_id": "T1"}
    )

    assert channel._client.looked_up == ["F7"]
    assert requests[0]["url"].endswith("/late.txt")
    assert received[0].params["files"]["uploaded_documents"][0]["filename"] == "late.txt"


@pytest.mark.asyncio
async def test_sign_in_page_is_reported_as_a_missing_scope(
    monkeypatch, tmp_path
) -> None:
    """An unauthenticated GET is answered 200 with HTML, not an error status."""
    _stub_downloads(
        monkeypatch,
        tmp_path,
        content_type="text/html",
        chunks=(b"<html>sign in</html>",),
    )
    channel, client, received = _attachment_channel()

    await channel._handle_message_event(
        _file_share_dm(text=""), {"event_id": "Ev-html", "team_id": "T1"}
    )

    assert received == []
    assert "files:read" in client.calls[0]["text"]
    assert "notes.txt" in client.calls[0]["text"]
    assert list((tmp_path / "slack_T1_D1_U1" / "uploads").glob("*")) == []


def _external_file_share(url: str) -> dict[str, Any]:
    """A share of an *external* file, as ``files.remote.add`` registers one.

    The shape is the one Slack documents and returns: ``mode: "external"``,
    ``is_external: true``, ``mimetype: "application/vnd.slack-remote"``,
    ``size: 0``, no ``url_private_download`` at all -- and ``url_private`` set
    to the URL whoever registered the file supplied, which is the field this
    test is about. ``permalink`` is the Slack-hosted page, and stays Slack's.
    """
    event = _file_share_dm(text="have a look at this")
    event["files"] = [
        {
            "id": "F-EXT",
            "name": "Quarterly plan",
            "title": "Quarterly plan",
            "mimetype": "application/vnd.slack-remote",
            "filetype": "remote",
            "pretty_type": "Remote",
            "size": 0,
            "mode": "external",
            "is_external": True,
            "external_type": "app",
            "external_id": "1234",
            "external_url": url,
            "url_private": url,
            "permalink": "https://acme.slack.com/files/U1/F-EXT/quarterly_plan",
        }
    ]
    return event


@pytest.mark.asyncio
async def test_external_file_url_never_receives_the_bot_token(
    monkeypatch, tmp_path
) -> None:
    """The live bug: url_private on an external file is the poster's own URL.

    Anybody who can share an external file into a channel the bot is in chooses
    that URL, so a download that trusts it sends the workspace's bot token, as a
    bearer header, to a host of their choosing. The refusal has to land before
    the request is made and not after it comes back.
    """
    requests = _stub_downloads(monkeypatch, tmp_path)
    channel, client, received = _attachment_channel()

    await channel._handle_message_event(
        _external_file_share("https://attacker.example/collect"),
        {"event_id": "Ev-external", "team_id": "T1"},
    )

    # Nothing was sent at all -- not a request without the header, none.
    assert requests == []
    assert not (tmp_path / "slack_T1_D1_U1").exists()
    # The message text still arrives; one unreadable attachment does not cost
    # the user the question they asked beside it.
    assert len(received) == 1
    assert received[0].params["content"] == (
        "have a look at this\n\n[ts: 1710000010.000100]"
    )
    assert "files" not in received[0].params
    notice = client.calls[0]["text"]
    assert "stored outside Slack" in notice
    assert "permalink" in notice
    # The URL is attacker-supplied text and is never echoed back into the
    # channel it came from.
    assert "attacker.example" not in notice


@pytest.mark.asyncio
async def test_off_slack_download_url_is_refused_whatever_it_looks_like(
    monkeypatch, tmp_path
) -> None:
    """One comparison, so every near-miss spelling fails the same way."""
    channel, _client, _received = _attachment_channel()
    channel._workspace_host = "acme.slack.com"

    for url in (
        "https://attacker.example/files-pri/T1-F1/notes.txt",
        # A host that merely ends in the allowed one: no suffix matching.
        "https://files.slack.com.attacker.example/x",
        # Credentials in the authority, which some parsers read as the host.
        "https://files.slack.com@attacker.example/x",
        # http, which httpx would send the header across on an upgrade.
        "http://files.slack.com/files-pri/T1-F1/notes.txt",
        "",
        "not a url",
        "file:///etc/passwd",
    ):
        with pytest.raises(slack_connect.SlackAttachmentError) as excinfo:
            channel._assert_slack_file_url(url)
        assert excinfo.value.reason == slack_connect._FILE_REASON_OFF_SLACK
        # The refused URL never reaches the exception message either.
        assert "attacker.example" not in str(excinfo.value)

    # And the two hosts that are Slack's own are not refused.
    channel._assert_slack_file_url(
        "https://files.slack.com/files-pri/T1-F1/download/notes.txt"
    )
    channel._assert_slack_file_url("https://acme.slack.com/files/U1/F1/notes.txt")


@pytest.mark.asyncio
async def test_workspace_host_widens_the_allow_list_only_once_auth_test_names_it(
    monkeypatch, tmp_path
) -> None:
    """auth.test's url is where the second accepted host comes from."""
    requests = _stub_downloads(monkeypatch, tmp_path)
    channel, client, received = _attachment_channel()

    event = _file_share_dm(text="")
    event["files"] = [
        {
            "id": "F1",
            "name": "notes.txt",
            "mimetype": "text/plain",
            "url_private": "https://acme.slack.com/files/U1/F1/notes.txt",
        }
    ]

    # Before auth.test has answered, files.slack.com is the whole allow-list.
    await channel._handle_message_event(
        event, {"event_id": "Ev-ws-before", "team_id": "T1"}
    )
    assert requests == []
    assert "stored outside Slack" in client.calls[0]["text"]

    class _WorkspaceClient(_FakeSlackClient):
        async def auth_test(self) -> dict[str, str]:
            self.auth_test_calls += 1
            return {"user_id": "U-BOT", "url": "https://acme.slack.com/"}

    channel._client = _WorkspaceClient()
    await channel._load_bot_user_id()
    assert channel._workspace_host == "acme.slack.com"

    await channel._handle_message_event(
        _file_share_dm(text="", ts="1710000015.000100"),
        {"event_id": "Ev-ws-plain", "team_id": "T1"},
    )
    # Two uploads, so two turns: the second file is shared after the turn the
    # first one started has ended, rather than while it is still running.
    _end_turn(channel, received[-1])
    event["ts"] = "1710000020.000100"
    await channel._handle_message_event(
        event, {"event_id": "Ev-ws-after", "team_id": "T1"}
    )
    assert [request["url"] for request in requests] == [
        "https://files.slack.com/files-pri/T1-F1/notes.txt",
        "https://acme.slack.com/files/U1/F1/notes.txt",
    ]
    assert len(received) == 2


@pytest.mark.asyncio
async def test_auth_test_without_a_url_leaves_the_narrow_allow_list() -> None:
    channel, _client, _received = _attachment_channel()
    await channel._load_bot_user_id()  # _FakeSlackClient answers no url
    assert channel._workspace_host == ""
    assert channel._slack_file_hosts() == frozenset({"files.slack.com"})


@pytest.mark.asyncio
async def test_a_redirect_off_slack_is_refused_before_a_request_is_made(
    monkeypatch, tmp_path
) -> None:
    """The second hop faces the allow-list the first hop faced.

    A ``Location`` header is a URL nobody checked. Letting httpx follow it would
    make the request and keep the answer, on the strength of httpx happening to
    drop the ``Authorization`` header on the way out of the origin. Here the hop
    is refused, no second request exists, and the user is told the file was not
    read rather than being handed whatever answered.
    """
    requests = _stub_downloads(
        monkeypatch, tmp_path, redirect_to="https://attacker.example/steal"
    )
    channel, client, received = _attachment_channel()

    await channel._handle_message_event(
        _file_share_dm(text=""), {"event_id": "Ev-redirect", "team_id": "T1"}
    )

    assert [request["url"] for request in requests] == [
        "https://files.slack.com/files-pri/T1-F1/notes.txt"
    ]
    assert not list(tmp_path.rglob("notes.txt"))
    notice = client.calls[0]["text"]
    assert "notes.txt" in notice
    assert "attacker.example" not in notice
    assert received == []


@pytest.mark.asyncio
async def test_a_redirect_within_slack_is_followed_with_the_token(
    monkeypatch, tmp_path
) -> None:
    """Enterprise Grid's second hop still works, and still carries the header."""
    target = "https://acme.slack.com/files-pri/T1-F1/notes.txt"
    requests = _stub_downloads(monkeypatch, tmp_path, redirect_to=target)
    channel, _client, received = _attachment_channel()
    channel._workspace_host = "acme.slack.com"

    await channel._handle_message_event(
        _file_share_dm(text=""), {"event_id": "Ev-grid", "team_id": "T1"}
    )

    assert [request["url"] for request in requests] == [
        "https://files.slack.com/files-pri/T1-F1/notes.txt",
        target,
    ]
    assert all(
        request["headers"]["Authorization"].startswith("Bearer ")
        for request in requests
    )
    assert len(received) == 1


@pytest.mark.asyncio
async def test_httpx_drops_the_bearer_header_when_a_redirect_leaves_slack() -> None:
    """A backstop, no longer a load-bearing assumption.

    The connector used to follow redirects with httpx and check only the first
    hop, which was sound exactly as long as httpx kept stripping
    ``Authorization`` when a redirect left the origin. It no longer rests on
    that: ``stream_slack_file`` follows the hops itself and puts each target
    through the allow-list before issuing it, so a ``Location`` naming
    ``elsewhere.example`` is refused rather than fetched without a header.

    The test stays because the property is still worth knowing about. Something
    else in this codebase may one day follow a redirect on a request carrying a
    credential, and an httpx that quietly stopped dropping the header is better
    found out about here than there.
    """
    seen: list[tuple[str, str | None]] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append((str(request.url), request.headers.get("authorization")))
        if request.url.host == "files.slack.com":
            return httpx.Response(302, headers={"Location": "https://elsewhere.example/x"})
        return httpx.Response(200, content=b"body")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handle), follow_redirects=True
    ) as client:
        response = await client.get(
            "https://files.slack.com/files-pri/T1-F1/notes.txt",
            headers={"Authorization": "Bearer xoxb-test"},
        )

    assert response.status_code == 200
    assert seen[0][1] == "Bearer xoxb-test"
    assert seen[1][0] == "https://elsewhere.example/x"
    assert seen[1][1] is None


@pytest.mark.asyncio
async def test_oversized_attachment_is_reported_and_never_written(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setattr(slack_connect, "MAX_FILE_BYTES", 8)
    _stub_downloads(monkeypatch, tmp_path, chunks=(b"12345", b"67890"))
    channel, client, received = _attachment_channel()

    await channel._handle_message_event(
        _file_share_dm(text=""), {"event_id": "Ev-large", "team_id": "T1"}
    )

    assert received == []
    assert "size limit" in client.calls[0]["text"]
    assert not (tmp_path / "slack_T1_D1_U1").exists()


@pytest.mark.asyncio
async def test_failed_download_reports_but_keeps_the_message_text(
    monkeypatch, tmp_path
) -> None:
    _stub_downloads(monkeypatch, tmp_path, status=500)
    channel, client, received = _attachment_channel()

    await channel._handle_message_event(
        _file_share_dm(text="summarize this"),
        {"event_id": "Ev-http-500", "team_id": "T1"},
    )

    assert len(received) == 1
    assert received[0].params["content"] == (
        "summarize this\n\n[ts: 1710000010.000100]"
    )
    assert "files" not in received[0].params
    assert "download from Slack failed" in client.calls[0]["text"]


@pytest.mark.asyncio
async def test_download_failure_notice_failure_does_not_block_the_request(
    monkeypatch, tmp_path
) -> None:
    _stub_downloads(monkeypatch, tmp_path, status=500)

    class _RefusingClient(_FakeSlackClient):
        async def chat_postMessage(self, **kwargs: Any) -> dict[str, str]:
            raise RuntimeError("channel_not_found")

    channel, _client, received = _attachment_channel()
    channel._client = _RefusingClient()

    await channel._handle_message_event(
        _file_share_dm(text="summarize this"),
        {"event_id": "Ev-notice-fails", "team_id": "T1"},
    )

    assert len(received) == 1


@pytest.mark.asyncio
async def test_download_failure_notice_summarizes_a_long_attachment_list(
    monkeypatch, tmp_path
) -> None:
    _stub_downloads(monkeypatch, tmp_path, status=404)
    channel, client, _received = _attachment_channel()

    event = _file_share_dm(text="")
    event["files"] = [
        {
            "id": f"F{index}",
            "name": f"file-{index}.txt",
            "url_private": f"https://files.slack.com/files-pri/T1-F{index}/f.txt",
        }
        for index in range(7)
    ]
    await channel._handle_message_event(
        event, {"event_id": "Ev-many-files", "team_id": "T1"}
    )

    text = client.calls[0]["text"]
    assert "Received 7 attachments" in text
    assert "file-4.txt" in text
    assert "file-5.txt" not in text
    assert "(+2 more)" in text


@pytest.mark.asyncio
async def test_a_failed_attachment_does_not_cost_the_ones_that_worked(
    monkeypatch, tmp_path
) -> None:
    _stub_downloads(monkeypatch, tmp_path)

    class _LookupFailsClient(_FakeSlackClient):
        async def files_info(self, **_kwargs: Any) -> dict[str, Any]:
            raise RuntimeError("file_not_found")

    channel, client, received = _attachment_channel()
    channel._client = _LookupFailsClient()
    client = channel._client

    event = _file_share_dm(text="")
    event["files"] = [
        {
            "id": "F1",
            "name": "good.txt",
            "mimetype": "text/plain",
            "url_private": "https://files.slack.com/files-pri/T1-F1/good.txt",
        },
        {"id": "F2", "name": "orphan.txt"},  # no inline URL; the lookup fails too
    ]
    await channel._handle_message_event(
        event, {"event_id": "Ev-mixed", "team_id": "T1"}
    )

    assert len(received[0].params["files"]["uploaded_documents"]) == 1
    assert "orphan.txt" in client.calls[0]["text"]
    assert "good.txt" not in client.calls[0]["text"]


def _stub_stalling_downloads(
    monkeypatch,
    tmp_path,
    *,
    delay: float = 30.0,
    stalls: Any = None,
) -> list[str]:
    """Route downloads to a transport whose body stalls part way through.

    A stalled response hands over its first chunk and then waits, which is what
    a large upload trickling in looks like from the client side: bytes do keep
    arriving, so a per-chunk read timeout never fires however long the transfer
    runs. Returns the list of URLs a stream was actually opened for.
    """
    started: list[str] = []
    should_stall = stalls if stalls is not None else (lambda _url: True)

    class _StallingResponse:
        def __init__(self, url: str) -> None:
            self.status_code = 200
            self.headers = {"content-type": "text/plain"}
            self._url = url

        async def aiter_bytes(self):
            yield b"first "
            if should_stall(self._url):
                await asyncio.sleep(delay)
            yield b"chunk"

    class _StallingContext:
        def __init__(self, url: str) -> None:
            self._url = url

        async def __aenter__(self) -> _StallingResponse:
            return _StallingResponse(self._url)

        async def __aexit__(self, *_exc: Any) -> bool:
            return False

    class _StallingClient:
        def __init__(self, **_kwargs: Any) -> None:
            pass

        async def __aenter__(self) -> "_StallingClient":
            return self

        async def __aexit__(self, *_exc: Any) -> bool:
            return False

        def stream(
            self, method: str, url: str, headers: dict[str, str] | None = None
        ) -> _StallingContext:
            started.append(url)
            return _StallingContext(url)

    monkeypatch.setattr(
        slack_connect,
        "httpx",
        SimpleNamespace(AsyncClient=_StallingClient, Timeout=lambda *a, **kw: None),
    )
    monkeypatch.setattr(slack_connect, "get_agent_sessions_dir", lambda: tmp_path)
    return started


@pytest.mark.asyncio
async def test_a_stalled_download_is_cut_off_and_reported(monkeypatch, tmp_path) -> None:
    """A transfer that never finishes must not hold the request open forever.

    The per-operation httpx timeout cannot catch this: the body is still
    arriving, just far too slowly. Only a deadline over the whole phase does.
    """
    monkeypatch.setattr(slack_connect, "_ATTACHMENT_PHASE_TIMEOUT_SECONDS", 0.05)
    _stub_stalling_downloads(monkeypatch, tmp_path)
    channel, client, received = _attachment_channel()

    started_at = time.monotonic()
    await channel._handle_message_event(
        _file_share_dm(text="summarize this"),
        {"event_id": "Ev-stalled", "team_id": "T1"},
    )
    elapsed = time.monotonic() - started_at

    assert elapsed < 5.0  # given up on, not waited out
    # Reported through the ordinary unreadable-attachment notice, not dropped
    # silently and not raised out of the handler.
    assert "notes.txt" in client.calls[0]["text"]
    assert "took too long" in client.calls[0]["text"]
    # The message itself still reaches the agent, holding no file records.
    assert len(received) == 1
    assert received[0].params["content"] == (
        "summarize this\n\n[ts: 1710000010.000100]"
    )
    assert "files" not in received[0].params
    assert list((tmp_path / "slack_T1_D1_U1" / "uploads").glob("*")) == []


@pytest.mark.asyncio
async def test_the_download_budget_is_shared_across_every_attachment(
    monkeypatch, tmp_path
) -> None:
    """One deadline for the phase, not one per file.

    A per-file timeout lets a message multiply the worst case by however many
    attachments it holds. Once the budget is gone the remaining files are
    reported without a fetch being attempted at all.
    """
    monkeypatch.setattr(slack_connect, "_ATTACHMENT_PHASE_TIMEOUT_SECONDS", 0.05)
    started = _stub_stalling_downloads(monkeypatch, tmp_path)
    channel, client, received = _attachment_channel()

    event = _file_share_dm(text="summarize these")
    event["files"] = [
        {
            "id": f"F{index}",
            "name": f"file-{index}.txt",
            "mimetype": "text/plain",
            "url_private": f"https://files.slack.com/files-pri/T1-F{index}/f.txt",
        }
        for index in range(4)
    ]

    started_at = time.monotonic()
    await channel._handle_message_event(
        event, {"event_id": "Ev-budget", "team_id": "T1"}
    )
    elapsed = time.monotonic() - started_at

    assert elapsed < 5.0
    # The first file spent the budget; the rest were never dialled.
    assert len(started) == 1
    text = client.calls[0]["text"]
    assert "Received 4 attachments" in text
    assert "took too long" in text
    assert len(received) == 1


@pytest.mark.asyncio
async def test_a_timed_out_attachment_does_not_cost_the_ones_that_worked(
    monkeypatch, tmp_path
) -> None:
    """A stalled file is reported on its own, like any other per-file failure."""
    monkeypatch.setattr(slack_connect, "_ATTACHMENT_PHASE_TIMEOUT_SECONDS", 0.5)
    _stub_stalling_downloads(
        monkeypatch, tmp_path, stalls=lambda url: url.endswith("slow.txt")
    )
    channel, client, received = _attachment_channel()

    event = _file_share_dm(text="")
    event["files"] = [
        {
            "id": "F1",
            "name": "quick.txt",
            "mimetype": "text/plain",
            "url_private": "https://files.slack.com/files-pri/T1-F1/quick.txt",
        },
        {
            "id": "F2",
            "name": "stalled.txt",
            "mimetype": "text/plain",
            "url_private": "https://files.slack.com/files-pri/T1-F2/slow.txt",
        },
    ]
    await channel._handle_message_event(
        event, {"event_id": "Ev-partial-timeout", "team_id": "T1"}
    )

    documents = received[0].params["files"]["uploaded_documents"]
    assert [record["filename"] for record in documents] == ["quick.txt"]
    assert "stalled.txt" in client.calls[0]["text"]
    assert "quick.txt" not in client.calls[0]["text"]


@pytest.mark.asyncio
async def test_an_oversized_attachment_stops_being_pulled_at_the_ceiling(
    monkeypatch, tmp_path
) -> None:
    """The size ceiling abandons the transfer instead of measuring it afterwards.

    What is held in memory is bounded by the ceiling because the check runs
    before each chunk is kept and the rest of the body is never asked for.
    """
    monkeypatch.setattr(slack_connect, "MAX_FILE_BYTES", 8)
    pulled: list[bytes] = []

    class _CountingResponse:
        status_code = 200
        headers = {"content-type": "text/plain"}

        async def aiter_bytes(self):
            for chunk in (b"12345", b"67890", b"and there is more still"):
                pulled.append(chunk)
                yield chunk

    class _CountingContext:
        async def __aenter__(self) -> _CountingResponse:
            return _CountingResponse()

        async def __aexit__(self, *_exc: Any) -> bool:
            return False

    class _CountingClient:
        def __init__(self, **_kwargs: Any) -> None:
            pass

        async def __aenter__(self) -> "_CountingClient":
            return self

        async def __aexit__(self, *_exc: Any) -> bool:
            return False

        def stream(self, *_args: Any, **_kwargs: Any) -> _CountingContext:
            return _CountingContext()

    monkeypatch.setattr(
        slack_connect,
        "httpx",
        SimpleNamespace(AsyncClient=_CountingClient, Timeout=lambda *a, **kw: None),
    )
    monkeypatch.setattr(slack_connect, "get_agent_sessions_dir", lambda: tmp_path)
    channel, client, received = _attachment_channel()

    await channel._handle_message_event(
        _file_share_dm(text=""), {"event_id": "Ev-ceiling", "team_id": "T1"}
    )

    assert pulled == [b"12345", b"67890"]  # the rest of the body is never read
    assert received == []
    assert "size limit" in client.calls[0]["text"]


@pytest.mark.asyncio
async def test_attachment_names_cannot_escape_the_upload_directory(
    monkeypatch, tmp_path
) -> None:
    _stub_downloads(monkeypatch, tmp_path)
    channel, _client, received = _attachment_channel()

    event = _file_share_dm(text="")
    event["files"] = [
        {
            "id": "F1",
            "name": "../../../etc/passwd",
            "mimetype": "text/plain",
            "url_private": "https://files.slack.com/files-pri/T1-F1/x",
        }
    ]
    await channel._handle_message_event(
        event, {"event_id": "Ev-traversal", "team_id": "T1"}
    )

    stored = Path(received[0].params["files"]["uploaded_documents"][0]["path"])
    assert stored.parent == tmp_path / "slack_T1_D1_U1" / "uploads"
    assert stored.name == "passwd"


@pytest.mark.asyncio
async def test_a_repeated_filename_does_not_overwrite_the_earlier_upload(
    monkeypatch, tmp_path
) -> None:
    _stub_downloads(monkeypatch, tmp_path)
    channel, _client, received = _attachment_channel()

    for index, ts in enumerate(("1710000020.000100", "1710000021.000100")):
        if received:
            # Each share is its own turn. The collision this is about is between
            # two files landing in one session's upload directory, not between
            # two messages landing in one running turn.
            _end_turn(channel, received[-1])
        await channel._handle_message_event(
            _file_share_dm(text="", ts=ts),
            {"event_id": f"Ev-dup-{index}", "team_id": "T1"},
        )

    stored = sorted(
        path.name for path in (tmp_path / "slack_T1_D1_U1" / "uploads").iterdir()
    )
    assert stored == ["notes-1.txt", "notes.txt"]
    assert (
        received[1].params["files"]["uploaded_documents"][0]["filename"]
        == "notes-1.txt"
    )


def test_safe_path_component_rejects_separators_and_dot_names() -> None:
    assert SlackChannel._safe_path_component("a/b/c.txt", "x") == "c.txt"
    assert SlackChannel._safe_path_component("..", "fallback") == "fallback"
    assert SlackChannel._safe_path_component("", "fallback") == "fallback"
    assert SlackChannel._safe_path_component("rapport final.pdf", "x") == "rapport_final.pdf"


def test_private_url_prefers_the_download_variant() -> None:
    assert (
        SlackChannel._private_url({"url_private_download": "d", "url_private": "p"})
        == "d"
    )
    assert SlackChannel._private_url({"url_private": "p"}) == "p"
    assert SlackChannel._private_url({}) == ""


def test_slack_file_name_falls_back_to_title_then_id() -> None:
    assert SlackChannel._slack_file_name({"name": "a.txt", "title": "T"}) == "a.txt"
    assert SlackChannel._slack_file_name({"title": "Report"}) == "Report"
    assert SlackChannel._slack_file_name({"id": "F1"}) == "F1"
    assert SlackChannel._slack_file_name({}) == "unnamed file"


def test_collect_event_files_ignores_malformed_payloads() -> None:
    assert SlackChannel._collect_event_files({}) == []
    assert SlackChannel._collect_event_files({"files": "F1"}) == []
    assert SlackChannel._collect_event_files({"files": [{"id": "F1"}, "F2"]}) == [
        {"id": "F1"}
    ]


class _FakeSlackClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.reactions: list[dict[str, Any]] = []
        # Methods slack_sdk has no wrapper for and that the connector therefore
        # reaches through ``api_call``: assistant.threads.setStatus and
        # blocks.validate. Kept apart from ``calls`` so a test asserting on
        # posted messages is not also asserting on these.
        self.api_calls: list[dict[str, Any]] = []
        self.auth_test_calls = 0

    async def api_call(self, api_method: str, **kwargs: Any) -> dict[str, bool]:
        self.api_calls.append({"api_method": api_method, **kwargs})
        return {"ok": True}

    async def chat_postMessage(self, **kwargs: Any) -> dict[str, str]:
        self.calls.append(kwargs)
        return {"ts": f"1710000099.{len(self.calls):06d}"}

    async def reactions_add(self, **kwargs: Any) -> dict[str, bool]:
        self.reactions.append(kwargs)
        return {"ok": True}

    async def auth_test(self) -> dict[str, str]:
        self.auth_test_calls += 1
        return {"user_id": "U-BOT"}


@pytest.mark.asyncio
async def test_auth_test_failure_does_not_block_bot_identity_loading() -> None:
    class FailingSlackClient:
        async def auth_test(self) -> dict[str, str]:
            raise RuntimeError("Slack unavailable")

    channel = SlackChannel(SlackChannelConfig(enabled=True), RobotMessageRouter())
    channel._client = FailingSlackClient()

    await channel._load_bot_user_id()

    assert channel._bot_user_id == ""


@pytest.mark.asyncio
async def test_missing_bot_identity_still_deduplicates_message_and_mention() -> None:
    # The channel listens for links, so the message below wakes the bot even
    # though the bot's own id was never resolved and the mention cannot match.
    channel = SlackChannel(
        SlackChannelConfig(
            enabled=True,
            conversation_overrides={
                "C-RESEARCH": SlackChannelOverride(
                    mode=frozenset({"mention", "url"})
                )
            },
        ),
        RobotMessageRouter(),
    )
    channel._running = True
    received: list[Message] = []
    channel.on_message(received.append)

    shared_event = {
        "user": "U1",
        "channel": "C-RESEARCH",
        "channel_type": "channel",
        "text": "<@U-BOT> analyze https://example.com/paper",
        "ts": "1710000002.000475",
    }
    await channel._handle_message_event(
        {
            **shared_event,
            "type": "message",
            "client_msg_id": "MsgAutoMention",
        },
        {"event_id": "EvAutoMentionMessage", "team_id": "T1"},
    )
    await channel._handle_app_mention(
        {**shared_event, "type": "app_mention"},
        {"event_id": "EvAutoMentionApp", "team_id": "T1"},
    )

    assert channel._bot_user_id == ""
    assert len(received) == 1


@pytest.mark.asyncio
async def test_acknowledgement_is_sent_once_before_agent_processing() -> None:
    channel = SlackChannel(
        SlackChannelConfig(
            enabled=True,
            allow_from=["U1"],
            allowed_channel_ids=["C1"],
            reply_in_thread=True,
            acknowledge_mode="text",
            acknowledgement_text="Received. Analyzing…",
        ),
        RobotMessageRouter(),
    )
    client = _FakeSlackClient()
    channel._running = True
    channel._client = client
    calls_seen_by_agent: list[list[dict[str, Any]]] = []
    channel.on_message(lambda _: calls_seen_by_agent.append(list(client.calls)))

    event = {
        "type": "app_mention",
        "user": "U1",
        "channel": "C1",
        "channel_type": "channel",
        "text": "<@B1> analyze this",
        "ts": "1710000002.000500",
    }
    body = {"event_id": "EvAck", "team_id": "T1"}

    await channel._handle_app_mention(event, body)
    await channel._handle_app_mention(event, body)

    expected_call = {
        "channel": "C1",
        "text": "Received. Analyzing…",
        "thread_ts": "1710000002.000500",
    }
    assert client.calls == [expected_call]
    assert calls_seen_by_agent == [[expected_call]]


@pytest.mark.asyncio
async def test_acknowledgement_failure_does_not_block_agent_processing() -> None:
    class FailingSlackClient:
        async def chat_postMessage(self, **kwargs: Any) -> None:
            raise RuntimeError("Slack unavailable")

    channel = SlackChannel(
        SlackChannelConfig(
            enabled=True,
            allow_from=["U1"],
            acknowledge_mode="text",
        ),
        RobotMessageRouter(),
    )
    channel._running = True
    channel._client = FailingSlackClient()
    received: list[Message] = []
    channel.on_message(received.append)

    await channel._handle_message_event(
        {
            "type": "message",
            "channel_type": "im",
            "channel": "D1",
            "user": "U1",
            "text": "analyze this",
            "ts": "1710000002.000600",
        },
        {"event_id": "EvAckFailure", "team_id": "T1"},
    )

    assert len(received) == 1


def _mention_event(ts: str = "1710000004.000100") -> dict[str, Any]:
    return {
        "type": "app_mention",
        "user": "U1",
        "channel": "C1",
        "channel_type": "channel",
        "text": "<@U-BOT> analyze this",
        "ts": ts,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "expect_reaction", "expect_text"),
    [
        ("reaction", True, False),
        ("text", False, True),
        ("both", True, True),
        ("off", False, False),
        # Unknown values fall back to the reaction default rather than going silent.
        ("nonsense", True, False),
    ],
)
async def test_acknowledge_mode_selects_reaction_text_or_both(
    mode: str, expect_reaction: bool, expect_text: bool
) -> None:
    channel = SlackChannel(
        SlackChannelConfig(
            enabled=True,
            allow_from=["U1"],
            allowed_channel_ids=["C1"],
            acknowledge_mode=mode,
            acknowledgement_text="Received. Analyzing…",
        ),
        RobotMessageRouter(),
    )
    client = _FakeSlackClient()
    channel._running = True
    channel._client = client
    channel.on_message(lambda _: None)

    await channel._handle_app_mention(_mention_event(), {"event_id": "EvMode", "team_id": "T1"})

    assert bool(client.reactions) is expect_reaction
    assert bool(client.calls) is expect_text
    if expect_reaction:
        assert client.reactions == [
            {"channel": "C1", "timestamp": "1710000004.000100", "name": "eyes"}
        ]


@pytest.mark.asyncio
async def test_acknowledgement_reaction_targets_the_request_not_the_thread() -> None:
    """reactions.add must decorate the incoming message, not its thread parent."""
    channel = SlackChannel(
        SlackChannelConfig(enabled=True, allow_from=["U1"], reply_in_thread=True),
        RobotMessageRouter(),
    )
    client = _FakeSlackClient()
    channel._running = True
    channel._client = client
    channel.on_message(lambda _: None)

    event = _mention_event(ts="1710000004.000900")
    event["thread_ts"] = "1710000004.000100"

    await channel._handle_app_mention(event, {"event_id": "EvThread", "team_id": "T1"})

    assert client.reactions == [
        {"channel": "C1", "timestamp": "1710000004.000900", "name": "eyes"}
    ]


@pytest.mark.asyncio
async def test_rejected_user_gets_reaction_and_no_agent_dispatch() -> None:
    channel = SlackChannel(
        SlackChannelConfig(
            enabled=True,
            allow_from=["U-ALLOWED"],
            rejected_emoji="no_entry_sign",
        ),
        RobotMessageRouter(),
    )
    client = _FakeSlackClient()
    channel._running = True
    channel._client = client
    received: list[Message] = []
    channel.on_message(received.append)

    await channel._handle_app_mention(_mention_event(), {"event_id": "EvDeny", "team_id": "T1"})

    assert received == []
    assert client.calls == []
    assert client.reactions == [
        {"channel": "C1", "timestamp": "1710000004.000100", "name": "no_entry_sign"}
    ]


@pytest.mark.asyncio
async def test_excluded_channel_stays_silent() -> None:
    """A channel the operator excluded must not even receive a reaction."""
    channel = SlackChannel(
        SlackChannelConfig(
            enabled=True, allow_from=["U1"], allowed_channel_ids=["C-OTHER"]
        ),
        RobotMessageRouter(),
    )
    client = _FakeSlackClient()
    channel._running = True
    channel._client = client
    received: list[Message] = []
    channel.on_message(received.append)

    await channel._handle_app_mention(_mention_event(), {"event_id": "EvExcl", "team_id": "T1"})

    assert received == []
    assert client.calls == []
    assert client.reactions == []


@pytest.mark.asyncio
async def test_retried_event_is_not_acknowledged_twice() -> None:
    channel = SlackChannel(
        SlackChannelConfig(enabled=True, allow_from=["U1"]), RobotMessageRouter()
    )
    client = _FakeSlackClient()
    channel._running = True
    channel._client = client
    received: list[Message] = []
    channel.on_message(received.append)

    event = _mention_event()
    body = {"event_id": "EvRetry", "team_id": "T1"}
    await channel._handle_app_mention(event, body)
    await channel._handle_app_mention(event, body)

    assert len(received) == 1
    assert len(client.reactions) == 1


class _OrderedSlackClient(_FakeSlackClient):
    """A fake client that appends every outbound call to a shared log.

    The log is what makes the ordering assertable: the acknowledgement and the
    attachment download both land in one list, in the order they happened.
    """

    def __init__(self, log: list[str]) -> None:
        super().__init__()
        self.log = log

    async def chat_postMessage(self, **kwargs: Any) -> dict[str, str]:
        self.log.append("post")
        return await super().chat_postMessage(**kwargs)

    async def reactions_add(self, **kwargs: Any) -> dict[str, bool]:
        self.log.append("react")
        return await super().reactions_add(**kwargs)


def _log_downloads(channel: SlackChannel, log: list[str]) -> None:
    """Record when the download phase starts, without changing what it does."""
    original = channel._download_attachments

    async def _recording(
        files: list[dict[str, Any]], session_id: str
    ) -> tuple[list[dict[str, Any]], list[tuple[str, str]]]:
        log.append("download")
        return await original(files, session_id)

    channel._download_attachments = _recording  # type: ignore[method-assign]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "expected_ack"),
    [
        ("reaction", ["react"]),
        ("text", ["post"]),
        ("both", ["react", "post"]),
        ("off", []),
    ],
)
@pytest.mark.parametrize("text", ["have a look at this", ""])
async def test_acknowledgement_precedes_the_attachment_download(
    monkeypatch, tmp_path, mode: str, expected_ack: list[str], text: str
) -> None:
    """Someone uploading a large file must not wait out the transfer in silence.

    Asserted as an ordering rather than a duration: the acknowledgement calls
    have to appear before the download starts, in every acknowledge_mode, and
    whether or not the upload came with a comment.
    """
    _stub_downloads(monkeypatch, tmp_path)
    log: list[str] = []
    channel = SlackChannel(
        SlackChannelConfig(
            enabled=True,
            allow_from=["U1"],
            acknowledge_mode=mode,
            bot_token="xoxb-test",
        ),
        RobotMessageRouter(),
    )
    channel._running = True
    channel._client = _OrderedSlackClient(log)
    received: list[Message] = []
    channel.on_message(received.append)
    _log_downloads(channel, log)

    await channel._handle_message_event(
        _file_share_dm(text=text), {"event_id": "Ev-ack-order", "team_id": "T1"}
    )

    assert log == [*expected_ack, "download"]
    assert len(received) == 1


@pytest.mark.asyncio
async def test_acknowledgement_precedes_the_unreadable_file_notice(
    monkeypatch, tmp_path
) -> None:
    """The acknowledgement has to come before the "could not read them" notice.

    With the acknowledgement sequenced after the download, the apology for an
    unreadable attachment reached the user first, which reads as a reply to a
    message that was never acknowledged.
    """
    _stub_downloads(monkeypatch, tmp_path, status=500)
    channel = SlackChannel(
        SlackChannelConfig(
            enabled=True,
            allow_from=["U1"],
            acknowledge_mode="text",
            acknowledgement_text="Received. Analyzing…",
            bot_token="xoxb-test",
        ),
        RobotMessageRouter(),
    )
    channel._running = True
    client = _FakeSlackClient()
    channel._client = client
    received: list[Message] = []
    channel.on_message(received.append)

    await channel._handle_message_event(
        _file_share_dm(text="summarize this"),
        {"event_id": "Ev-ack-then-notice", "team_id": "T1"},
    )

    assert client.calls[0]["text"] == "Received. Analyzing…"
    assert "download from Slack failed" in client.calls[1]["text"]
    assert len(received) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["reaction", "text", "both", "off"])
async def test_bare_mention_with_no_text_and_no_files_stays_unacknowledged(
    mode: str,
) -> None:
    """A mention holding nothing at all is still ignored outright.

    The acknowledgement moved above the empty-text return, so it is guarded on
    there being something to acknowledge. A mention with no comment and no
    upload is not a request, and must not draw a reaction or a reply.
    """
    channel = SlackChannel(
        SlackChannelConfig(
            enabled=True, allow_from=["U1"], acknowledge_mode=mode
        ),
        RobotMessageRouter(),
    )
    channel._running = True
    channel._bot_user_id = "U-BOT"
    client = _FakeSlackClient()
    channel._client = client
    received: list[Message] = []
    channel.on_message(received.append)

    event = _mention_event(ts="1710000004.000700")
    event["text"] = "<@U-BOT>"
    await channel._handle_app_mention(event, {"event_id": "Ev-bare", "team_id": "T1"})

    assert received == []
    assert client.calls == []
    assert client.reactions == []


@pytest.mark.asyncio
async def test_retried_file_share_is_not_acknowledged_twice(
    monkeypatch, tmp_path
) -> None:
    """Hoisting the acknowledgement must not reopen the double-reaction hole.

    Deduplication runs above every acknowledgement path, so a retry of an event
    that holds attachments is dropped before either the reaction or the
    download is reached.
    """
    _stub_downloads(monkeypatch, tmp_path)
    channel = SlackChannel(
        SlackChannelConfig(
            enabled=True,
            allow_from=["U1"],
            acknowledge_mode="both",
            bot_token="xoxb-test",
        ),
        RobotMessageRouter(),
    )
    channel._running = True
    client = _FakeSlackClient()
    channel._client = client
    received: list[Message] = []
    channel.on_message(received.append)

    event = _file_share_dm(text="have a look at this")
    body = {"event_id": "Ev-file-retry", "team_id": "T1"}
    await channel._handle_message_event(event, body)
    await channel._handle_message_event(event, body)

    assert len(received) == 1
    assert len(client.reactions) == 1
    assert len(client.calls) == 1


@pytest.mark.asyncio
async def test_reaction_failure_does_not_block_agent_processing() -> None:
    class FailingReactionClient(_FakeSlackClient):
        async def reactions_add(self, **kwargs: Any) -> dict[str, bool]:
            raise RuntimeError("missing_scope")

    channel = SlackChannel(
        SlackChannelConfig(enabled=True, allow_from=["U1"]), RobotMessageRouter()
    )
    channel._running = True
    channel._client = FailingReactionClient()
    received: list[Message] = []
    channel.on_message(received.append)

    await channel._handle_app_mention(_mention_event(), {"event_id": "EvBoom", "team_id": "T1"})

    assert len(received) == 1


@pytest.mark.asyncio
async def test_emoji_names_tolerate_surrounding_colons() -> None:
    channel = SlackChannel(
        SlackChannelConfig(
            enabled=True, allow_from=["U1"], acknowledgement_emoji=":thumbsup:"
        ),
        RobotMessageRouter(),
    )
    client = _FakeSlackClient()
    channel._running = True
    channel._client = client
    channel.on_message(lambda _: None)

    await channel._handle_app_mention(_mention_event(), {"event_id": "EvColon", "team_id": "T1"})

    assert client.reactions[0]["name"] == "thumbsup"


@pytest.mark.parametrize(
    ("conf", "expected"),
    [
        ({}, "reaction"),
        ({"acknowledge_mode": "both"}, "both"),
        ({"acknowledge_mode": "  OFF  "}, "off"),
        ({"acknowledge_mode": "bogus"}, "reaction"),
        # Deprecated boolean maps onto the mode that does what it did. Before
        # the mode existed it switched a single text acknowledgement on and
        # off, so false is "acknowledge nothing" and not the reactions the
        # mode defaults to.
        ({"acknowledge_requests": True}, "text"),
        ({"acknowledge_requests": "yes"}, "text"),
        ({"acknowledge_requests": False}, "off"),
        ({"acknowledge_requests": "no"}, "off"),
        # A key written with nothing after the colon is null by the time this
        # sees it. It names neither state of a toggle, so it is read as unset
        # and the default stands -- which is what lets the templates ship the
        # key with no value.
        ({"acknowledge_requests": None}, "reaction"),
        # An explicit mode always wins over the deprecated boolean, whichever
        # way the boolean points.
        ({"acknowledge_mode": "off", "acknowledge_requests": True}, "off"),
        ({"acknowledge_mode": "text", "acknowledge_requests": False}, "text"),
        ({"acknowledge_mode": "both", "acknowledge_requests": False}, "both"),
        ({"acknowledge_mode": "reaction", "acknowledge_requests": False}, "reaction"),
    ],
)
def test_resolve_acknowledge_mode(conf: dict[str, Any], expected: str) -> None:
    assert slack_connect.resolve_acknowledge_mode(conf) == expected


def test_acknowledge_requests_false_is_off_not_reaction() -> None:
    """The deprecated false must not start emitting reactions.

    Before ``acknowledge_mode`` existed, ``acknowledge_requests`` guarded the
    only acknowledgement there was: false returned before posting anything. A
    deployment that turned acknowledgement off has to stay silent across the
    upgrade rather than begin reacting to every accepted request.
    """
    assert slack_connect.resolve_acknowledge_mode({"acknowledge_requests": False}) == (
        slack_connect.ACK_MODE_OFF
    )
    assert slack_connect.resolve_acknowledge_mode({"acknowledge_requests": True}) == (
        slack_connect.ACK_MODE_TEXT
    )


def test_resolve_acknowledge_mode_warns_once_on_the_legacy_key(slack_logs) -> None:
    """The legacy path still announces itself, and names the mode it chose."""
    assert slack_connect.resolve_acknowledge_mode({"acknowledge_requests": False}) == (
        "off"
    )

    warnings = _lines_at(slack_logs, logging.WARNING)
    assert len(warnings) == 1
    assert "acknowledge_requests is deprecated" in warnings[0]
    assert "acknowledge_mode: off" in warnings[0]


def test_resolve_acknowledge_mode_stays_quiet_without_the_legacy_key(
    slack_logs,
) -> None:
    """Neither an absent key nor a value-less one is a deprecated setting.

    The templates ship ``acknowledge_requests`` with no value, so warning on a
    null would warn every deployment that never set the key.
    """
    assert slack_connect.resolve_acknowledge_mode({}) == "reaction"
    assert slack_connect.resolve_acknowledge_mode({"acknowledge_requests": None}) == (
        "reaction"
    )
    assert slack_connect.resolve_acknowledge_mode(
        {"acknowledge_mode": "both", "acknowledge_requests": True}
    ) == "both"

    assert _lines_at(slack_logs, logging.WARNING) == []


def test_resolve_acknowledge_mode_warns_on_an_unknown_mode(slack_logs) -> None:
    """A misspelled mode is a misspelling, not a request for the legacy key.

    It falls back to the default without consulting ``acknowledge_requests``,
    so a config holding both does not quietly resolve through the deprecated
    one when the mode is mistyped.
    """
    assert slack_connect.resolve_acknowledge_mode(
        {"acknowledge_mode": "bogus", "acknowledge_requests": True}
    ) == "reaction"

    warnings = _lines_at(slack_logs, logging.WARNING)
    assert len(warnings) == 1
    assert "acknowledge_mode='bogus'" in warnings[0]
    assert "deprecated" not in warnings[0]


@pytest.mark.asyncio
async def test_send_uses_routing_target_chunks_text_and_ignores_delta() -> None:
    channel = SlackChannel(SlackChannelConfig(enabled=True), RobotMessageRouter())
    client = _FakeSlackClient()
    channel._client = client
    target = RoutingTarget(
        intent="godview",
        delivery=SlackDeliveryTarget(
            target_channel_id="C-TARGET",
            thread_ts="1710000003.000400",
        ),
    )

    oversized = "x" * (slack_connect._MAX_SLACK_TEXT_LENGTH + 100)
    await channel.send(_message(content=oversized), routing_target=target)
    await channel.send(
        _message(event_type=EventType.CHAT_DELTA, content="partial"),
        routing_target=target,
    )

    assert len(client.calls) == 2
    assert client.calls[0] == {
        "channel": "C-TARGET",
        "text": "x" * slack_connect._MAX_SLACK_TEXT_LENGTH,
        "thread_ts": "1710000003.000400",
    }
    assert client.calls[1] == {
        "channel": "C-TARGET",
        "text": "x" * 100,
        "thread_ts": "1710000003.000400",
    }


@pytest.mark.asyncio
async def test_send_splits_slack_report_into_root_summary_and_own_thread() -> None:
    channel = SlackChannel(SlackChannelConfig(enabled=True), RobotMessageRouter())
    client = _FakeSlackClient()
    channel._client = client
    content = (
        "*Daily Intelligence*\n\n"
        "Three concise findings."
        "\n<!-- jiuwenswarm:slack-thread-details -->\n"
        "*Trend Signals*\n"
        + ("Detailed evidence. " * (slack_connect._MAX_SLACK_TEXT_LENGTH // 10))
    )

    await channel.send(
        _message(content=content, metadata={"post_as_root": True}),
        routing_target=RoutingTarget(
            intent="godview",
            delivery=SlackDeliveryTarget(
                target_channel_id="C-REPORT",
                thread_ts="1710000003.000400",
            ),
        ),
    )

    assert client.calls[0] == {
        "channel": "C-REPORT",
        "text": "*Daily Intelligence*\n\nThree concise findings.",
    }
    assert len(client.calls) == 3
    assert all(call["thread_ts"] == "1710000099.000001" for call in client.calls[1:])
    assert all(
        "jiuwenswarm:slack-thread-details" not in call["text"] for call in client.calls
    )


@pytest.mark.asyncio
async def test_send_makes_every_marker_a_message_boundary() -> None:
    """Two markers are a root and two replies, in the order they were written."""
    channel = SlackChannel(SlackChannelConfig(enabled=True), RobotMessageRouter())
    client = _FakeSlackClient()
    channel._client = client
    marker = slack_connect._SLACK_THREAD_DETAILS_MARKER
    content = f"Brief.\n{marker}\nTables.\n{marker}\nProse."

    await channel.send(
        _message(content=content, metadata={"post_as_root": True}),
        routing_target=RoutingTarget(
            intent="godview",
            delivery=SlackDeliveryTarget(target_channel_id="C-REPORT"),
        ),
    )

    assert client.calls == [
        {"channel": "C-REPORT", "text": "Brief."},
        {"channel": "C-REPORT", "text": "Tables.", "thread_ts": "1710000099.000001"},
        {"channel": "C-REPORT", "text": "Prose.", "thread_ts": "1710000099.000001"},
    ]


@pytest.mark.asyncio
async def test_send_drops_a_section_that_rendered_to_nothing() -> None:
    """An empty section costs a boundary, not a blank message.

    A report whose tables are all empty on a quiet run writes both markers all
    the same; posting the gap between them would put an empty reply in the
    thread every quiet run.
    """
    channel = SlackChannel(SlackChannelConfig(enabled=True), RobotMessageRouter())
    client = _FakeSlackClient()
    channel._client = client
    marker = slack_connect._SLACK_THREAD_DETAILS_MARKER
    content = f"Brief.\n{marker}\n\n{marker}\nProse."

    await channel.send(
        _message(content=content, metadata={"post_as_root": True}),
        routing_target=RoutingTarget(
            intent="godview",
            delivery=SlackDeliveryTarget(target_channel_id="C-REPORT"),
        ),
    )

    assert client.calls == [
        {"channel": "C-REPORT", "text": "Brief."},
        {"channel": "C-REPORT", "text": "Prose.", "thread_ts": "1710000099.000001"},
    ]


def test_a_report_with_no_root_above_its_markers_is_one_flat_message() -> None:
    """No root means nothing to thread under, so the marker does nothing."""
    marker = slack_connect._SLACK_THREAD_DETAILS_MARKER
    assert SlackChannel._split_threaded_report(f"{marker}\nOnly details.") == [
        "Only details."
    ]
    assert SlackChannel._split_threaded_report(f"Only a brief.\n{marker}") == [
        "Only a brief."
    ]
    assert SlackChannel._split_threaded_report("No marker at all.") == [
        "No marker at all."
    ]


@pytest.mark.asyncio
async def test_send_keeps_structured_report_in_existing_thread() -> None:
    channel = SlackChannel(SlackChannelConfig(enabled=True), RobotMessageRouter())
    client = _FakeSlackClient()
    channel._client = client
    content = "Summary\n<!-- jiuwenswarm:slack-thread-details -->\nDetails"

    await channel.send(
        _message(content=content),
        routing_target=RoutingTarget(
            intent="godview",
            delivery=SlackDeliveryTarget(
                target_channel_id="C-REPORT",
                thread_ts="1710000003.000400",
            ),
        ),
    )

    assert client.calls == [
        {
            "channel": "C-REPORT",
            "text": "Summary\n\nDetails",
            "thread_ts": "1710000003.000400",
        }
    ]


def test_normalize_slack_mrkdwn_converts_common_markdown() -> None:
    markdown = (
        "# Slack Channel Digest\n"
        "## **Topic 1**\n"
        "**Summary**: concise overview\n"
        "- **Fact**: A result ([Alice](https://example.slack.com/archives/C1/"
        "p123?thread_ts=120&cid=C1)).\n"
        "+ **Recommendation**: Capture the design.\n"
        "* plain bullet\n"
        "__init__\n"
        "#hashtag\n"
        "---"
    )

    assert SlackChannel._normalize_slack_mrkdwn(markdown) == (
        "*Slack Channel Digest*\n"
        "*Topic 1*\n"
        "*Summary*: concise overview\n"
        "\u2022 *Fact:* A result "
        "(<https://example.slack.com/archives/C1/"
        "p123?thread_ts=120&cid=C1|Alice>).\n"
        "\u2022 *Recommendation:* Capture the design.\n"
        "\u2022 plain bullet\n"
        "__init__\n"
        "#hashtag\n"
        "---"
    )
    normalized = SlackChannel._normalize_slack_mrkdwn(markdown)
    assert SlackChannel._normalize_slack_mrkdwn(normalized) == normalized


def test_normalize_slack_mrkdwn_preserves_code_and_native_tokens() -> None:
    native_source = (
        "<https://example.slack.com/archives/C1/p123?thread_ts=120&cid=C1|source>"
    )
    markdown = (
        "Use `**raw** [x](https://example.com)` with <@U123>.\n"
        "```markdown\n"
        "# untouched\n"
        "**raw** [x](https://example.com)\n"
        "```\n"
        f"{native_source}"
    )

    normalized = SlackChannel._normalize_slack_mrkdwn(markdown)

    assert normalized == markdown
    assert SlackChannel._normalize_slack_mrkdwn(normalized) == normalized


def test_normalize_slack_mrkdwn_keeps_protected_tokens_in_line_context() -> None:
    source = "<https://example.slack.com/archives/C1/p123?thread_ts=120&cid=C1|source>"
    markdown = (
        "## Use `run()` safely\n"
        f"## Source {source}\n"
        "Prefix <@U1> - not a list\n"
        "  - nested"
    )

    assert SlackChannel._normalize_slack_mrkdwn(markdown) == (
        "*Use `run()` safely*\n"
        f"*Source {source}*\n"
        "Prefix <@U1> - not a list\n"
        "  \u2022 nested"
    )


def test_normalize_slack_mrkdwn_handles_headings_and_balanced_urls() -> None:
    markdown = (
        "## Technical **Findings**\n"
        "# C#\n"
        "# Title ###\n"
        "[wiki](https://en.wikipedia.org/wiki/Foo_(bar))"
    )

    assert SlackChannel._normalize_slack_mrkdwn(markdown) == (
        "*Technical Findings*\n"
        "*C#*\n"
        "*Title*\n"
        "<https://en.wikipedia.org/wiki/Foo_(bar)|wiki>"
    )


def test_normalize_slack_mrkdwn_does_not_cross_plain_brackets() -> None:
    markdown = (
        "Use [draft] then [source](https://example.com).\n"
        "[Fact] A claim ([source](https://example.com))."
    )
    expected = (
        "Use [draft] then <https://example.com|source>.\n"
        "[Fact] A claim (<https://example.com|source>)."
    )

    assert SlackChannel._normalize_slack_mrkdwn(markdown) == expected
    assert SlackChannel._normalize_slack_mrkdwn(expected) == expected


def test_normalize_slack_mrkdwn_closes_fence_only_on_delimiter_line() -> None:
    markdown = '```python\nprint("```")\n## untouched\n```\n## converted'

    assert SlackChannel._normalize_slack_mrkdwn(markdown) == (
        '```python\nprint("```")\n## untouched\n```\n*converted*'
    )


@pytest.mark.asyncio
async def test_send_normalizes_markdown_before_long_text_splitting() -> None:
    channel = SlackChannel(SlackChannelConfig(enabled=True), RobotMessageRouter())
    client = _FakeSlackClient()
    channel._client = client
    source = "https://example.com/wiki/Foo_(bar)"
    content = f"# Digest\n- **Fact**: [source]({source}) " + (
        "x" * (slack_connect._MAX_SLACK_TEXT_LENGTH + 100)
    )

    await channel.send(
        _message(content=content),
        routing_target=RoutingTarget(
            intent="godview",
            delivery=SlackDeliveryTarget(
                target_channel_id="C-REPORT",
                thread_ts="1710000003.000400",
            ),
        ),
    )

    rendered = "\n".join(call["text"] for call in client.calls)
    assert len(client.calls) == 2
    assert all(
        len(call["text"]) <= slack_connect._MAX_SLACK_TEXT_LENGTH
        for call in client.calls
    )
    assert rendered.startswith("*Digest*")
    assert f"\u2022 *Fact:* <{source}|source>" in rendered
    assert "# Digest" not in rendered
    assert "**Fact**" not in rendered
    assert "[source](" not in rendered


def test_normalize_slack_mrkdwn_preserves_unclosed_fenced_code() -> None:
    markdown = "```markdown\n# untouched\n**raw** [x](https://example.com)"

    assert SlackChannel._normalize_slack_mrkdwn(markdown) == markdown


@pytest.mark.asyncio
async def test_send_normalizes_markdown_before_root_and_thread_splitting() -> None:
    channel = SlackChannel(SlackChannelConfig(enabled=True), RobotMessageRouter())
    client = _FakeSlackClient()
    channel._client = client
    source = "https://example.slack.com/archives/C1/p123?thread_ts=120&cid=C1"
    content = (
        "# Channel digest\n"
        "**Summary**: One important finding."
        "\n<!-- jiuwenswarm:slack-thread-details -->\n"
        "## Topic 1\n"
        f"- **Fact**: Finding ([source]({source}))."
    )

    await channel.send(
        _message(content=content, metadata={"post_as_root": True}),
        routing_target=RoutingTarget(
            intent="godview",
            delivery=SlackDeliveryTarget(target_channel_id="C-REPORT"),
        ),
    )

    assert client.calls == [
        {
            "channel": "C-REPORT",
            "text": "*Channel digest*\n*Summary*: One important finding.",
        },
        {
            "channel": "C-REPORT",
            "text": (f"*Topic 1*\n\u2022 *Fact:* Finding (<{source}|source>)."),
            "thread_ts": "1710000099.000001",
        },
    ]


def test_split_text_prefers_paragraph_boundaries() -> None:
    half = slack_connect._MAX_SLACK_TEXT_LENGTH // 2
    # The first two paragraphs plus their separator must fit in one message,
    # and adding the third must not.
    first_paragraph = "a" * half
    second_paragraph = "b" * (half - 100)
    final_paragraph = "c" * 500

    chunks = SlackChannel._split_text(
        f"{first_paragraph}\n\n{second_paragraph}\n\n{final_paragraph}"
    )

    assert chunks == [
        f"{first_paragraph}\n\n{second_paragraph}",
        final_paragraph,
    ]


def test_split_text_does_not_break_slack_link_near_limit() -> None:
    prefix = "x" * (slack_connect._MAX_SLACK_TEXT_LENGTH - 50)
    slack_link = "<https://github.com/example/repo/issues/123|Issue #123>"

    chunks = SlackChannel._split_text(f"{prefix}\n{slack_link}\nMore detail")

    assert chunks == [prefix, f"{slack_link}\nMore detail"]


@pytest.mark.asyncio
async def test_send_falls_back_to_metadata_session_and_default_channel() -> None:
    channel = SlackChannel(
        SlackChannelConfig(enabled=True, default_channel_id="C-DEFAULT"),
        RobotMessageRouter(),
    )
    client = _FakeSlackClient()
    channel._client = client

    await channel.send(
        _message(
            metadata={
                "slack_channel_id": "C-META",
                "slack_thread_ts": "1710000004.000500",
            },
        ),
    )
    await channel.send(
        _message(metadata={}, session_id="slack_T1_C-SESSION_1710000005.000600")
    )
    await channel.send(_message(metadata={}, session_id="unknown"))

    assert [call["channel"] for call in client.calls] == [
        "C-META",
        "C-SESSION",
        "C-DEFAULT",
    ]
    assert client.calls[0]["thread_ts"] == "1710000004.000500"
    assert client.calls[1]["thread_ts"] == "1710000005.000600"
    assert "thread_ts" not in client.calls[2]


@pytest.mark.asyncio
async def test_send_post_as_root_ignores_inherited_thread() -> None:
    channel = SlackChannel(
        SlackChannelConfig(enabled=True),
        RobotMessageRouter(),
    )
    client = _FakeSlackClient()
    channel._client = client
    target = RoutingTarget(
        intent="godview",
        delivery=SlackDeliveryTarget(
            target_channel_id="C1",
            thread_ts="1710000005.000600",
        ),
    )

    await channel.send(
        _message(metadata={"post_as_root": True}),
        routing_target=target,
    )

    assert client.calls == [{"channel": "C1", "text": "response"}]


@pytest.mark.asyncio
async def test_start_and_stop_socket_mode_lifecycle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = asyncio.Event()
    closed = asyncio.Event()
    registered_events: list[str] = []
    registered_actions: list[Any] = []
    fake_client = _FakeSlackClient()

    class FakeAsyncApp:
        def __init__(self, token: str, logger: Any = None) -> None:
            assert token == "xoxb-test"
            # Not decoration: bolt copies a base logger's level, handlers and
            # filters onto every logger it builds, and hands the same one to
            # the socket-mode client. Without it both trees are unwired, which
            # is what made a dropped inbound message leave no trace at all.
            assert logger is not None
            assert logger.name == slack_connect.SDK_BASE_LOGGER_NAME
            self.client = fake_client

        def event(self, event_name: str):
            registered_events.append(event_name)

            def register(listener):
                return listener

            return register

        def action(self, constraint: Any):
            registered_actions.append(constraint)

            def register(listener):
                return listener

            return register

    class FakeSocketModeHandler:
        def __init__(self, app: Any, app_token: str) -> None:
            assert isinstance(app, FakeAsyncApp)
            assert app_token == "xapp-test"

        async def start_async(self) -> None:
            started.set()
            await closed.wait()

        async def close_async(self) -> None:
            closed.set()

    monkeypatch.setattr(slack_connect, "SLACK_AVAILABLE", True)
    monkeypatch.setattr(slack_connect, "AsyncApp", FakeAsyncApp)
    monkeypatch.setattr(slack_connect, "AsyncSocketModeHandler", FakeSocketModeHandler)

    channel = SlackChannel(
        SlackChannelConfig(
            enabled=True,
            bot_token="xoxb-test",
            app_token="xapp-test",
        ),
        RobotMessageRouter(),
    )
    task = asyncio.create_task(channel.start())
    await asyncio.wait_for(started.wait(), timeout=1)

    assert channel.is_running
    # The trailing entry is the catch-all, and its place in the list is the
    # point: bolt returns at the first matching listener, so an event type
    # named above it is claimed by its own handler and everything else is
    # acknowledged by this one rather than left on bolt's 404.
    assert registered_events[:2] == ["app_mention", "message"]
    # Both directions of every state change, and read off the same table the
    # router reads, so a family cannot come to cover one half and not the other.
    from jiuwenswarm.common.slack_events_policy import EVENT_TYPE_FAMILIES

    assert set(registered_events[2:-1]) == set(EVENT_TYPE_FAMILIES)
    assert registered_events[-1] is slack_connect._ANY_EVENT_TYPE
    # Interaction payloads share this connection, so the button that answers a
    # question and the one that stops a turn are both listened for here rather
    # than at a Request URL.
    assert len(registered_actions) == 2
    assert fake_client.auth_test_calls == 1
    assert channel._bot_user_id == "U-BOT"

    await channel.stop()
    await asyncio.wait_for(task, timeout=1)

    assert not channel.is_running
    assert closed.is_set()
    assert channel._bot_user_id == ""


def test_make_delivery_target_builds_slack_thread_target() -> None:
    target = make_delivery_target(
        "slack",
        chat_id="C1",
        physical_user_id="U1",
        thread_ts="1710000006.000700",
    )

    assert isinstance(target, SlackDeliveryTarget)
    assert target.target_channel_id == "C1"
    assert target.thread_ts == "1710000006.000700"
    assert target.physical_user_id == "U1"
    assert target.get_container_id() == "C1:1710000006.000700"


class _FailingSlackClient:
    """Fake client whose ``chat_postMessage`` fails from ``fail_from`` onwards."""

    def __init__(self, *, fail_from: int = 0, error: str = "channel_not_found") -> None:
        self.calls: list[dict[str, Any]] = []
        self._fail_from = fail_from
        self._error = error

    async def chat_postMessage(self, **kwargs: Any) -> dict[str, str]:
        self.calls.append(kwargs)
        if len(self.calls) > self._fail_from:
            raise RuntimeError(self._error)
        return {"ts": f"1710000099.{len(self.calls):06d}"}


@pytest.mark.asyncio
async def test_send_raises_when_the_only_chunk_fails() -> None:
    channel = SlackChannel(SlackChannelConfig(enabled=True), RobotMessageRouter())
    channel._client = _FailingSlackClient(fail_from=0)

    with pytest.raises(SlackDeliveryError) as excinfo:
        await channel.send(_message(metadata={"slack_channel_id": "C1"}))

    assert excinfo.value.channel_id == "C1"
    assert excinfo.value.chunks_sent == 0
    assert "channel_not_found" in str(excinfo.value)


@pytest.mark.asyncio
async def test_send_reports_how_much_of_a_split_reply_was_delivered() -> None:
    # A long reply is posted as several messages. Failing partway leaves truncated
    # output in the channel, so the error has to say how far it got.
    channel = SlackChannel(SlackChannelConfig(enabled=True), RobotMessageRouter())
    client = _FailingSlackClient(fail_from=2, error="ratelimited")
    channel._client = client
    oversized = "\n\n".join(
        "paragraph %d %s" % (index, "x" * 20000) for index in range(3)
    )

    with pytest.raises(SlackDeliveryError) as excinfo:
        await channel.send(
            _message(content=oversized, metadata={"slack_channel_id": "C1"})
        )

    assert len(client.calls) > 2
    assert excinfo.value.chunks_sent == 2
    assert excinfo.value.chunks_total > 2
    assert "2/%d chunks" % excinfo.value.chunks_total in str(excinfo.value)


@pytest.mark.asyncio
async def test_send_raises_when_no_target_channel_can_be_resolved() -> None:
    # No routing target, no metadata, an unparseable session id and no
    # default_channel_id: there is nowhere to deliver, and a warning with a
    # silent return would report a delivery that never happened.
    channel = SlackChannel(SlackChannelConfig(enabled=True), RobotMessageRouter())
    channel._client = _FakeSlackClient()

    with pytest.raises(SlackDeliveryError) as excinfo:
        await channel.send(_message(metadata={}, session_id="unknown"))

    assert "no target channel resolved" in str(excinfo.value)


@pytest.mark.asyncio
async def test_send_raises_when_the_channel_is_not_connected() -> None:
    channel = SlackChannel(SlackChannelConfig(enabled=True), RobotMessageRouter())

    with pytest.raises(SlackDeliveryError):
        await channel.send(_message(metadata={"slack_channel_id": "C1"}))


@pytest.mark.asyncio
async def test_send_stays_quiet_when_there_is_nothing_to_deliver() -> None:
    # Deltas and empty payloads are not failures, and must not raise even when the
    # channel is stopped -- otherwise shutdown would produce spurious cron errors.
    channel = SlackChannel(SlackChannelConfig(enabled=True), RobotMessageRouter())

    await channel.send(_message(event_type=EventType.CHAT_DELTA, content="partial"))
    await channel.send(_message(content=""))

    channel._client = _FakeSlackClient()
    await channel.send(_message(event_type=EventType.CHAT_DELTA, content="partial"))
    assert channel._client.calls == []


@pytest.mark.asyncio
async def test_send_raises_when_the_threaded_report_summary_fails() -> None:
    channel = SlackChannel(SlackChannelConfig(enabled=True), RobotMessageRouter())
    channel._client = _FailingSlackClient(fail_from=0)
    content = f"brief\n{slack_connect._SLACK_THREAD_DETAILS_MARKER}\ndetail"

    with pytest.raises(SlackDeliveryError) as excinfo:
        await channel.send(
            _message(content=content, metadata={"slack_channel_id": "C1"})
        )

    assert excinfo.value.chunks_sent == 0


@pytest.mark.asyncio
async def test_send_reports_a_threaded_report_whose_detail_fails() -> None:
    # The summary lands as a top-level message and the detail goes in its thread.
    # Losing only the detail is the nastiest partial: the channel shows a brief that
    # looks complete.
    channel = SlackChannel(SlackChannelConfig(enabled=True), RobotMessageRouter())
    client = _FailingSlackClient(fail_from=1)
    channel._client = client
    content = f"brief\n{slack_connect._SLACK_THREAD_DETAILS_MARKER}\ndetail"

    with pytest.raises(SlackDeliveryError) as excinfo:
        await channel.send(
            _message(content=content, metadata={"slack_channel_id": "C1"})
        )

    assert excinfo.value.chunks_sent == 1
    assert excinfo.value.chunks_total == 2
    assert len(client.calls) == 2
    assert "thread_ts" in client.calls[1]


class _RateLimitError(Exception):
    """Shaped like slack_sdk's SlackApiError: holds a .response."""

    def __init__(self, retry_after: Any = "1", *, status: int = 429) -> None:
        super().__init__("ratelimited")
        self.response = SimpleNamespace(
            status_code=status,
            data={"ok": False, "error": "ratelimited"},
            headers={"Retry-After": retry_after},
        )


class _RateLimitedThenOkClient:
    def __init__(self, *, failures: int) -> None:
        self.calls: list[dict[str, Any]] = []
        self.attempts = 0
        self._failures = failures

    async def chat_postMessage(self, **kwargs: Any) -> dict[str, str]:
        self.attempts += 1
        if self.attempts <= self._failures:
            raise _RateLimitError()
        self.calls.append(kwargs)
        return {"ts": "1710000099.000001"}


def test_retry_after_seconds_only_retries_rate_limits() -> None:
    assert slack_connect.retry_after_seconds(_RateLimitError("2")) == 2.0
    # Terminal errors must not be retried; retrying only delays the real error.
    assert slack_connect.retry_after_seconds(RuntimeError("channel_not_found")) is None
    not_found = _RateLimitError(status=404)
    not_found.response.data = {"ok": False, "error": "channel_not_found"}
    assert slack_connect.retry_after_seconds(not_found) is None


def test_retry_after_seconds_clamps_and_defaults() -> None:
    # A hostile or malformed header must not park the dispatch loop.
    assert slack_connect.retry_after_seconds(_RateLimitError("99999")) == 60.0
    assert slack_connect.retry_after_seconds(_RateLimitError("nonsense")) == 1.0
    assert slack_connect.retry_after_seconds(_RateLimitError(None)) == 1.0
    assert slack_connect.retry_after_seconds(_RateLimitError(["3"])) == 3.0
    assert slack_connect.retry_after_seconds(_RateLimitError("-5")) == 0.0


@pytest.mark.asyncio
async def test_send_retries_a_rate_limited_post_and_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    slept: list[float] = []

    async def _fake_sleep(delay: float) -> None:
        slept.append(delay)

    monkeypatch.setattr(slack_connect.asyncio, "sleep", _fake_sleep)
    channel = SlackChannel(SlackChannelConfig(enabled=True), RobotMessageRouter())
    client = _RateLimitedThenOkClient(failures=2)
    channel._client = client

    await channel.send(_message(metadata={"slack_channel_id": "C1"}))

    assert client.attempts == 3
    assert slept == [1.0, 1.0]
    assert [call["channel"] for call in client.calls] == ["C1"]


@pytest.mark.asyncio
async def test_send_gives_up_after_the_retry_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _fake_sleep(delay: float) -> None:
        return None

    monkeypatch.setattr(slack_connect.asyncio, "sleep", _fake_sleep)
    channel = SlackChannel(SlackChannelConfig(enabled=True), RobotMessageRouter())
    client = _RateLimitedThenOkClient(failures=99)
    channel._client = client

    with pytest.raises(SlackDeliveryError) as excinfo:
        await channel.send(_message(metadata={"slack_channel_id": "C1"}))

    assert client.attempts == slack_connect._MAX_RATE_LIMIT_RETRIES + 1
    assert "ratelimited" in str(excinfo.value)


@pytest.mark.asyncio
async def test_send_does_not_retry_a_terminal_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    slept: list[float] = []

    async def _fake_sleep(delay: float) -> None:
        slept.append(delay)

    monkeypatch.setattr(slack_connect.asyncio, "sleep", _fake_sleep)
    channel = SlackChannel(SlackChannelConfig(enabled=True), RobotMessageRouter())
    client = _FailingSlackClient(fail_from=0, error="invalid_auth")
    channel._client = client

    with pytest.raises(SlackDeliveryError):
        await channel.send(_message(metadata={"slack_channel_id": "C1"}))

    assert len(client.calls) == 1
    assert slept == []


class _UploadingSlackClient:
    """Fake client whose ``files_upload_v2`` fails from ``fail_from`` onwards."""

    def __init__(self, *, fail_from: int | None = None, error: Exception | None = None) -> None:
        self.uploads: list[dict[str, Any]] = []
        self.attempts = 0
        self._fail_from = fail_from
        self._error = error or RuntimeError("upload_failed")

    async def files_upload_v2(self, **kwargs: Any) -> dict[str, Any]:
        self.attempts += 1
        if self._fail_from is not None and self.attempts > self._fail_from:
            raise self._error
        self.uploads.append(kwargs)
        return {"ok": True}


def _file_message(
    *,
    files: Any,
    metadata: dict[str, Any] | None = None,
    session_id: str = "slack_T1_C1_1710000000.000100",
) -> Message:
    return Message(
        id="file-1",
        type="event",
        channel_id="slack",
        session_id=session_id,
        params={},
        timestamp=time.time(),
        ok=True,
        payload={"event_type": "chat.file", "files": files},
        event_type=EventType.CHAT_FILE,
        metadata=metadata,
    )


@pytest.mark.asyncio
async def test_send_uploads_files_into_the_resolved_conversation(tmp_path) -> None:
    report = tmp_path / "report.pdf"
    report.write_bytes(b"%PDF-")
    channel = SlackChannel(SlackChannelConfig(enabled=True), RobotMessageRouter())
    client = _UploadingSlackClient()
    channel._client = client

    await channel.send(
        _file_message(
            files=[{"path": str(report), "name": "quarterly.pdf"}],
            metadata={
                "slack_channel_id": "C1",
                "slack_thread_ts": "1710000004.000500",
            },
        )
    )

    assert client.uploads == [
        {
            "channel": "C1",
            "file": str(report),
            "filename": "quarterly.pdf",
            "thread_ts": "1710000004.000500",
        }
    ]


@pytest.mark.asyncio
async def test_send_raises_when_an_upload_fails(tmp_path) -> None:
    # Same contract as the text path: a scheduled job must not report success for
    # a file that never reached the conversation.
    report = tmp_path / "report.pdf"
    report.write_bytes(b"%PDF-")
    channel = SlackChannel(SlackChannelConfig(enabled=True), RobotMessageRouter())
    channel._client = _UploadingSlackClient(
        fail_from=0, error=RuntimeError("missing_scope")
    )

    with pytest.raises(SlackDeliveryError) as excinfo:
        await channel.send(
            _file_message(
                files=[str(report)], metadata={"slack_channel_id": "C1"}
            )
        )

    assert excinfo.value.channel_id == "C1"
    assert excinfo.value.chunks_sent == 0
    assert "missing_scope" in str(excinfo.value)


@pytest.mark.asyncio
async def test_send_reports_how_many_files_landed_before_the_failure(tmp_path) -> None:
    paths = []
    for index in range(3):
        path = tmp_path / f"part-{index}.txt"
        path.write_text("x")
        paths.append(str(path))
    channel = SlackChannel(SlackChannelConfig(enabled=True), RobotMessageRouter())
    client = _UploadingSlackClient(fail_from=2)
    channel._client = client

    with pytest.raises(SlackDeliveryError) as excinfo:
        await channel.send(
            _file_message(files=paths, metadata={"slack_channel_id": "C1"})
        )

    assert len(client.uploads) == 2
    assert excinfo.value.chunks_sent == 2
    assert excinfo.value.chunks_total == 3
    assert "2/3 files" in str(excinfo.value)


@pytest.mark.asyncio
async def test_send_raises_when_a_file_has_gone_missing(tmp_path) -> None:
    present = tmp_path / "present.txt"
    present.write_text("x")
    channel = SlackChannel(SlackChannelConfig(enabled=True), RobotMessageRouter())
    client = _UploadingSlackClient()
    channel._client = client

    with pytest.raises(SlackDeliveryError) as excinfo:
        await channel.send(
            _file_message(
                files=[str(present), str(tmp_path / "gone.txt")],
                metadata={"slack_channel_id": "C1"},
            )
        )

    assert len(client.uploads) == 1
    assert excinfo.value.chunks_sent == 1
    assert "gone.txt" in str(excinfo.value)


@pytest.mark.asyncio
async def test_send_raises_when_files_have_nowhere_to_go(tmp_path) -> None:
    present = tmp_path / "present.txt"
    present.write_text("x")
    channel = SlackChannel(SlackChannelConfig(enabled=True), RobotMessageRouter())
    channel._client = _UploadingSlackClient()

    with pytest.raises(SlackDeliveryError) as excinfo:
        await channel.send(
            _file_message(files=[str(present)], metadata={}, session_id="unknown")
        )

    assert "no target channel resolved" in str(excinfo.value)


@pytest.mark.asyncio
async def test_send_raises_when_files_arrive_on_a_disconnected_channel(tmp_path) -> None:
    present = tmp_path / "present.txt"
    present.write_text("x")
    channel = SlackChannel(SlackChannelConfig(enabled=True), RobotMessageRouter())

    with pytest.raises(SlackDeliveryError):
        await channel.send(
            _file_message(files=[str(present)], metadata={"slack_channel_id": "C1"})
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("files", [[], None, "report.pdf", [{"name": "no-path.pdf"}]])
async def test_send_stays_quiet_for_a_file_event_with_nothing_to_upload(files) -> None:
    # A payload with no usable entry is nothing to do, not a failed delivery, and
    # must not raise even on a stopped channel.
    channel = SlackChannel(SlackChannelConfig(enabled=True), RobotMessageRouter())

    await channel.send(_file_message(files=files, metadata={"slack_channel_id": "C1"}))


@pytest.mark.asyncio
async def test_send_retries_a_rate_limited_upload(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    slept: list[float] = []

    async def _fake_sleep(delay: float) -> None:
        slept.append(delay)

    monkeypatch.setattr(slack_connect.asyncio, "sleep", _fake_sleep)
    report = tmp_path / "report.pdf"
    report.write_bytes(b"%PDF-")

    class _RateLimitedUploads(_UploadingSlackClient):
        async def files_upload_v2(self, **kwargs: Any) -> dict[str, Any]:
            self.attempts += 1
            if self.attempts <= 2:
                raise _RateLimitError()
            self.uploads.append(kwargs)
            return {"ok": True}

    channel = SlackChannel(SlackChannelConfig(enabled=True), RobotMessageRouter())
    client = _RateLimitedUploads()
    channel._client = client

    await channel.send(
        _file_message(files=[str(report)], metadata={"slack_channel_id": "C1"})
    )

    assert client.attempts == 3
    assert slept == [1.0, 1.0]
    assert len(client.uploads) == 1


def test_extract_outgoing_files_accepts_mappings_and_bare_paths() -> None:
    msg = _file_message(
        files=[
            {"path": "/tmp/a.txt", "name": "renamed.txt"},
            {"path": "/tmp/b.txt"},
            "/tmp/c.txt",
            {"name": "no-path.txt"},
            "",
        ]
    )

    assert SlackChannel._extract_outgoing_files(msg) == [
        ("/tmp/a.txt", "renamed.txt"),
        ("/tmp/b.txt", "b.txt"),
        ("/tmp/c.txt", "c.txt"),
    ]


def test_shipped_config_offers_the_send_file_tool_on_slack() -> None:
    """The tool is gated on config, so the connector alone does not enable it.

    ``_is_send_file_enabled`` defaults every channel except ``web`` to disabled,
    which means an omitted flag is indistinguishable from a deliberate opt-out
    and the toolkit is never constructed. Assert the resolved switch rather than
    the YAML key, so the test fails if either half stops agreeing.
    """
    import yaml

    import jiuwenswarm
    from jiuwenswarm.agents.swarm.providers.runtime_tools import _is_send_file_enabled

    config_path = Path(jiuwenswarm.__file__).parent / "resources" / "config.yaml"
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))

    assert config["channels"]["slack"]["send_file_allowed"] is True
    assert _is_send_file_enabled(config, "slack") is True
    # A channel that does not declare the flag stays off.
    assert _is_send_file_enabled(config, "telegram") is False


def _group_channel(mode: str, **overrides: Any) -> tuple[SlackChannel, list[Message]]:
    config = SlackChannelConfig(
        enabled=True,
        group_chat_mode=mode,
        **overrides,
    )
    channel = SlackChannel(config, RobotMessageRouter())
    channel._running = True
    channel._bot_user_id = "U-BOT"
    received: list[Message] = []
    channel.on_message(received.append)
    return channel, received


def _channel_event(
    *, text: str = "team status update", ts: str = "1710000010.000100", **extra: Any
) -> dict[str, Any]:
    event = {
        "type": "message",
        "channel_type": "channel",
        "channel": "C1",
        "user": "U1",
        "text": text,
        "ts": ts,
    }
    event.update(extra)
    return event


@pytest.mark.parametrize(
    ("mode", "expected"),
    [("off", 0), ("mention", 0), ("reply", 0), ("all", 1)],
)
@pytest.mark.asyncio
async def test_group_chat_mode_controls_plain_channel_messages(
    mode: str, expected: int
) -> None:
    channel, received = _group_channel(mode)

    await channel._handle_message_event(
        _channel_event(), {"event_id": f"Ev-{mode}", "team_id": "T1"}
    )

    assert len(received) == expected
    if expected:
        assert received[0].metadata["slack_trigger"] == "all"


@pytest.mark.parametrize(
    ("mode", "expected"),
    [("off", 0), ("mention", 0), ("reply", 1), ("all", 1)],
)
@pytest.mark.asyncio
async def test_reply_mode_answers_threads_under_the_bot(
    mode: str, expected: int
) -> None:
    channel, received = _group_channel(mode)

    await channel._handle_message_event(
        _channel_event(
            text="what about the second point?",
            ts="1710000011.000200",
            thread_ts="1710000010.000100",
            parent_user_id="U-BOT",
        ),
        {"event_id": f"EvReply-{mode}", "team_id": "T1"},
    )

    assert len(received) == expected


@pytest.mark.asyncio
async def test_reply_mode_ignores_threads_under_someone_else() -> None:
    channel, received = _group_channel("reply")

    await channel._handle_message_event(
        _channel_event(
            text="replying to a colleague",
            ts="1710000012.000300",
            thread_ts="1710000011.000100",
            parent_user_id="U-OTHER",
        ),
        {"event_id": "EvReplyOther", "team_id": "T1"},
    )

    assert received == []


@pytest.mark.asyncio
async def test_reply_mode_ignores_a_thread_root() -> None:
    # A root message holds thread_ts == ts once it has replies. It is not itself
    # a reply, so answering it would make "reply" behave like "all".
    channel, received = _group_channel("reply")

    await channel._handle_message_event(
        _channel_event(
            ts="1710000013.000400",
            thread_ts="1710000013.000400",
            parent_user_id="U-BOT",
        ),
        {"event_id": "EvRoot", "team_id": "T1"},
    )

    assert received == []


@pytest.mark.asyncio
async def test_reply_mode_fails_closed_when_the_bot_id_is_unknown() -> None:
    # auth.test can fail at startup. Guessing here would silently promote
    # "reply" to "all".
    channel, received = _group_channel("reply")
    channel._bot_user_id = ""

    await channel._handle_message_event(
        _channel_event(
            ts="1710000014.000500",
            thread_ts="1710000013.000400",
            parent_user_id="U-BOT",
        ),
        {"event_id": "EvNoBotId", "team_id": "T1"},
    )

    assert received == []


@pytest.mark.parametrize("mode", ["mention", "reply", "all"])
@pytest.mark.asyncio
async def test_group_chat_mode_still_answers_mentions(mode: str) -> None:
    # Slack delivers mentions through app_mention, so unlike Telegram the
    # non-off modes are cumulative rather than exclusive.
    channel, received = _group_channel(mode)

    await channel._handle_app_mention(
        {
            "type": "app_mention",
            "user": "U1",
            "channel": "C1",
            "channel_type": "channel",
            "text": "<@U-BOT> status?",
            "ts": "1710000015.000600",
        },
        {"event_id": f"EvMention-{mode}", "team_id": "T1"},
    )

    assert len(received) == 1


@pytest.mark.asyncio
async def test_off_mode_silences_mentions_too() -> None:
    channel, received = _group_channel("off")

    await channel._handle_app_mention(
        {
            "type": "app_mention",
            "user": "U1",
            "channel": "C1",
            "channel_type": "channel",
            "text": "<@U-BOT> status?",
            "ts": "1710000016.000700",
        },
        {"event_id": "EvMentionOff", "team_id": "T1"},
    )

    assert received == []


@pytest.mark.parametrize("mode", ["mention", "reply", "all", "off"])
@pytest.mark.asyncio
async def test_direct_messages_ignore_the_group_mode(mode: str) -> None:
    channel, received = _group_channel(mode)

    await channel._handle_message_event(
        {
            "type": "message",
            "channel_type": "im",
            "channel": "D1",
            "user": "U1",
            "text": "hello",
            "ts": "1710000017.000800",
        },
        {"event_id": f"EvDm-{mode}", "team_id": "T1"},
    )

    assert len(received) == 1


@pytest.mark.asyncio
async def test_all_mode_does_not_double_dispatch_a_mention() -> None:
    # The same Slack message arrives as both app_mention and message. Only one
    # dispatch may result, or the agent answers twice.
    channel, received = _group_channel("all")
    event = {
        "type": "message",
        "channel_type": "channel",
        "channel": "C1",
        "user": "U1",
        "text": "<@U-BOT> summarize",
        "ts": "1710000019.001000",
    }
    body = {"event_id": "EvBoth", "team_id": "T1"}

    await channel._handle_app_mention({**event, "type": "app_mention"}, body)
    await channel._handle_message_event(event, body)

    assert len(received) == 1


@pytest.mark.asyncio
async def test_message_ts_reaches_metadata_verbatim_with_full_precision() -> None:
    # Six decimal places, none of them zero: int(float(ts) * 1000) would
    # truncate to 1777423717666 and the ".499" is unrecoverable from that.
    # message_ts must carry the string Slack sent, untouched.
    channel, received = _group_channel("all")

    await channel._handle_message_event(
        _channel_event(text="ping", ts="1777423717.666499"),
        {"event_id": "EvFullPrecisionTs", "team_id": "T1"},
    )

    assert received[0].metadata["message_ts"] == "1777423717.666499"


def test_unknown_group_chat_mode_falls_back_to_mention() -> None:
    channel, _ = _group_channel("nonsense")
    assert channel._group_chat_mode() == "mention"
    channel.config.group_chat_mode = ""
    assert channel._group_chat_mode() == "mention"
    channel.config.group_chat_mode = "ALL"
    assert channel._group_chat_mode() == "all"


class _RecordingAvatarAdapter:
    """Minimal stand-in for the connector-facing half of the avatar adapter."""

    def __init__(self) -> None:
        self.observed: list[dict[str, Any]] = []
        self.bot_user_id = ""

    def set_bot_user_id(self, bot_user_id: str) -> None:
        self.bot_user_id = bot_user_id

    async def observe_message(self, **kwargs: Any) -> None:
        self.observed.append(kwargs)


def _avatar_channel(
    **overrides: Any,
) -> tuple[SlackChannel, list[Message], _RecordingAvatarAdapter]:
    adapter = _RecordingAvatarAdapter()
    config = SlackChannelConfig(
        enabled=True,
        group_chat_mode="all",
        group_digital_avatar=True,
        my_user_id="U0PRINCIPAL",
        enable_memory=True,
        **overrides,
    )
    channel = SlackChannel(config, RobotMessageRouter(), im_platform_adapter=adapter)
    channel._running = True
    channel._bot_user_id = "U-BOT"
    received: list[Message] = []
    channel.on_message(received.append)
    return channel, received, adapter


@pytest.mark.asyncio
async def test_avatar_mode_is_off_by_default() -> None:
    channel, received = _group_channel("all")

    await channel._handle_message_event(
        _channel_event(), {"event_id": "EvAvatarOff", "team_id": "T1"}
    )

    assert len(received) == 1
    message = received[0]
    assert message.group_digital_avatar is False
    assert message.enable_memory is None
    # None of the platform-neutral pipeline keys appear, so the message is
    # exactly what it was before the avatar fields existed.
    assert not [key for key in message.metadata if key.startswith("im_")]
    assert "chat_type" not in message.metadata
    assert "avatar_mode" not in message.metadata


@pytest.mark.asyncio
async def test_avatar_mode_adds_the_pipeline_metadata_for_channel_messages() -> None:
    channel, received, adapter = _avatar_channel()

    await channel._handle_message_event(
        _channel_event(text="<@U0PRINCIPAL> can you confirm?"),
        {"event_id": "EvAvatarOn", "team_id": "T1"},
    )

    assert len(received) == 1
    message = received[0]
    assert message.group_digital_avatar is True
    assert message.enable_memory is True
    assert message.metadata["chat_type"] == "group"
    assert message.metadata["im_chat_type"] == "group"
    assert message.metadata["im_platform"] == "slack"
    assert message.metadata["im_sender_user_id"] == "U1"
    assert message.metadata["im_thread_id"] == "C1"
    assert message.metadata["timestamp_ms"] == 1710000010000
    assert message.metadata["im_mentioned_user_ids"] == ["U0PRINCIPAL"]
    assert message.metadata["avatar_mode"] is True
    assert message.metadata["principal_user_id"] == "U0PRINCIPAL"
    assert message.metadata["triggering_user_id"] == "U1"
    # The slack_* keys the rest of the connector reads are untouched.
    assert message.metadata["slack_channel_id"] == "C1"
    assert adapter.observed == [
        {
            "channel_id": "C1",
            "channel_type": "channel",
            "user_id": "U1",
            # Has the envelope marker, like every dispatched message: the
            # avatar pipeline is handed the same text the model is.
            "text": "<@U0PRINCIPAL> can you confirm?\n\n[ts: 1710000010.000100]",
            "message_ts": "1710000010.000100",
        }
    ]


@pytest.mark.asyncio
async def test_message_ts_and_timestamp_ms_coexist_one_raw_one_lossy() -> None:
    # Both keys come from the same Slack ts, on the one path that sets both.
    # timestamp_ms is int(float(ts) * 1000) and cannot keep the ".499"; only
    # message_ts, kept as a raw string, can be handed back to address the
    # message later.
    channel, received, _ = _avatar_channel()

    await channel._handle_message_event(
        _channel_event(text="ping", ts="1777423717.666499"),
        {"event_id": "EvCoexist", "team_id": "T1"},
    )

    metadata = received[0].metadata
    assert metadata["message_ts"] == "1777423717.666499"
    assert metadata["timestamp_ms"] == 1777423717666


@pytest.mark.asyncio
async def test_avatar_mode_passes_the_private_channel_type_through() -> None:
    channel, _, adapter = _avatar_channel()

    await channel._handle_message_event(
        _channel_event(channel_type="group"),
        {"event_id": "EvAvatarPrivate", "team_id": "T1"},
    )

    assert adapter.observed[0]["channel_type"] == "group"


@pytest.mark.asyncio
async def test_avatar_mode_leaves_direct_messages_alone() -> None:
    channel, received, adapter = _avatar_channel()

    await channel._handle_message_event(
        {
            "type": "message",
            "channel_type": "im",
            "channel": "D1",
            "user": "U1",
            "text": "ping",
            "ts": "1710000020.000100",
        },
        {"event_id": "EvAvatarDm", "team_id": "T1"},
    )

    assert len(received) == 1
    assert received[0].group_digital_avatar is False
    assert received[0].enable_memory is None
    assert "chat_type" not in received[0].metadata
    assert adapter.observed == []


@pytest.mark.asyncio
async def test_avatar_metadata_needs_a_registered_adapter() -> None:
    channel, received = _group_channel("all", group_digital_avatar=True)

    await channel._handle_message_event(
        _channel_event(), {"event_id": "EvAvatarNoAdapter", "team_id": "T1"}
    )

    assert len(received) == 1
    assert received[0].group_digital_avatar is False
    assert "chat_type" not in received[0].metadata


@pytest.mark.asyncio
async def test_avatar_mode_records_no_mentions_when_there_are_none() -> None:
    channel, received, _ = _avatar_channel()

    await channel._handle_message_event(
        _channel_event(text="just thinking out loud"),
        {"event_id": "EvAvatarNoMention", "team_id": "T1"},
    )

    assert "im_mentioned_user_ids" not in received[0].metadata


@pytest.mark.asyncio
async def test_bot_user_id_reaches_the_adapter_from_auth_test() -> None:
    class _AuthClient:
        async def auth_test(self) -> dict[str, str]:
            return {"user_id": "U-BOT"}

    channel, _, adapter = _avatar_channel()
    channel._bot_user_id = ""
    channel._client = _AuthClient()

    await channel._load_bot_user_id()

    assert adapter.bot_user_id == "U-BOT"


def test_bot_user_id_reaches_the_adapter_from_an_event_body() -> None:
    channel, _, adapter = _avatar_channel()
    channel._bot_user_id = ""

    channel._remember_bot_user_id(
        {"authorizations": [{"user_id": "U-BOT", "is_bot": True}]}
    )

    assert adapter.bot_user_id == "U-BOT"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("<@U01ABC> and <@W02DEF>", ["U01ABC", "W02DEF"]),
        ("<@U01ABC> then <@U01ABC> again", ["U01ABC"]),
        ("<@U01ABC|alice> legacy form", ["U01ABC"]),
        ("<#C01ABC|general> is a channel, not a user", []),
        ("<!here> is a broadcast", []),
        ("", []),
    ],
)
def test_mentioned_user_ids_extraction(text: str, expected: list[str]) -> None:
    assert slack_connect._mentioned_user_ids(text) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [("1710000000.000100", 1710000000000), ("1710000000", 1710000000000)],
)
def test_slack_timestamp_conversion(value: str, expected: int) -> None:
    assert slack_connect._slack_timestamp_ms(value) == expected


def test_slack_timestamp_falls_back_to_now_when_unparseable() -> None:
    before = int(time.time() * 1000)
    converted = slack_connect._slack_timestamp_ms("not-a-timestamp")
    assert before <= converted <= int(time.time() * 1000)


@pytest.fixture
def slack_logs(caplog):
    """Capture the Slack connector's own log records.

    ``caplog`` installs its handler on the root logger, and a jiuwenswarm logger
    reaches it only while it still propagates — which the runtime's logging
    setup turns off. Attaching the handler to the emitting logger makes the
    capture independent of that, and propagation is disabled for the duration so
    a logger that does still propagate is not recorded twice.
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
    """Return the connector's messages logged at exactly *level*."""
    return [
        record.getMessage()
        for record in caplog.records
        if record.levelno == level and record.name == slack_connect.__name__
    ]


@pytest.mark.asyncio
async def test_successful_text_delivery_is_logged_with_the_message_ts(
    slack_logs,
) -> None:
    # A delivered message and one that was never attempted used to look the
    # same in the log: nothing was written either way.
    channel = SlackChannel(SlackChannelConfig(enabled=True), RobotMessageRouter())
    channel._client = _FailingSlackClient(fail_from=10)

    await channel.send(_message(metadata={"slack_channel_id": "C1"}))

    assert _lines_at(slack_logs, logging.INFO) == [
        "[SlackChannel] delivered text: channel=C1 ts=1710000099.000001 "
        "chunk=1/1 mode=post"
    ]


@pytest.mark.asyncio
async def test_every_chunk_of_a_split_reply_is_logged_with_its_position(
    slack_logs,
) -> None:
    channel = SlackChannel(SlackChannelConfig(enabled=True), RobotMessageRouter())
    channel._client = _FailingSlackClient(fail_from=10)
    oversized = "\n\n".join(
        "paragraph %d %s" % (index, "x" * 20000) for index in range(3)
    )

    await channel.send(
        _message(content=oversized, metadata={"slack_channel_id": "C1"})
    )

    lines = _lines_at(slack_logs, logging.INFO)
    assert len(lines) > 1
    total = len(lines)
    for index, line in enumerate(lines):
        assert "chunk=%d/%d" % (index + 1, total) in line
        assert "mode=post" in line


@pytest.mark.asyncio
async def test_streaming_edits_stay_at_debug_so_deliveries_stand_out(
    slack_logs,
) -> None:
    # The streaming surface rewrites its message once per debounce window. At
    # INFO those edits would bury the one line that reports a delivery.
    channel = SlackChannel(SlackChannelConfig(enabled=True), RobotMessageRouter())
    channel._client = _FakeSlackClient()

    sent, message_ts, error = await channel._post_text(
        channel_id="C1", text="partial", thread_ts=""
    )

    assert (sent, error) == (True, "")
    assert _lines_at(slack_logs, logging.INFO) == []
    assert _lines_at(slack_logs, logging.DEBUG) == [
        "[SlackChannel] streaming edit applied: channel=C1 ts=%s mode=post"
        % message_ts
    ]


@pytest.mark.asyncio
async def test_a_chat_update_is_logged_as_an_edit_not_a_new_post(
    slack_logs,
) -> None:
    class _UpdatingSlackClient(_FakeSlackClient):
        async def chat_update(self, **kwargs: Any) -> dict[str, str]:
            self.calls.append(kwargs)
            return {"ts": kwargs["ts"]}

    channel = SlackChannel(SlackChannelConfig(enabled=True), RobotMessageRouter())
    channel._client = _UpdatingSlackClient()

    await channel._post_text(
        channel_id="C1",
        text="final",
        thread_ts="",
        update_ts="1710000042.000900",
        chunk_index=1,
        chunk_total=1,
    )

    assert _lines_at(slack_logs, logging.INFO) == [
        "[SlackChannel] delivered text: channel=C1 ts=1710000042.000900 "
        "chunk=1/1 mode=update"
    ]


@pytest.mark.asyncio
async def test_successful_upload_is_logged_with_size_and_file_id(
    slack_logs, tmp_path
) -> None:
    class _UploadResultClient:
        def __init__(self) -> None:
            self.uploads: list[dict[str, Any]] = []

        async def files_upload_v2(self, **kwargs: Any) -> dict[str, Any]:
            self.uploads.append(kwargs)
            return {"files": [{"id": "F0123"}]}

    report = tmp_path / "report.pdf"
    report.write_bytes(b"%PDF-")
    channel = SlackChannel(SlackChannelConfig(enabled=True), RobotMessageRouter())
    channel._client = _UploadResultClient()

    await channel.send(
        _file_message(
            files=[{"path": str(report), "name": "quarterly.pdf"}],
            metadata={"slack_channel_id": "C1"},
        )
    )

    assert _lines_at(slack_logs, logging.INFO) == [
        "[SlackChannel] delivered file: channel=C1 filename=quarterly.pdf "
        "bytes=5 file_id=F0123 file=1/1"
    ]


@pytest.mark.asyncio
async def test_upload_without_a_reported_file_id_still_logs_the_delivery(
    slack_logs, tmp_path
) -> None:
    # The id is a convenience, not a contract; losing it must not cost the line
    # that says the file landed.
    report = tmp_path / "report.pdf"
    report.write_bytes(b"%PDF-")
    channel = SlackChannel(SlackChannelConfig(enabled=True), RobotMessageRouter())
    channel._client = _UploadingSlackClient()

    await channel.send(
        _file_message(files=[str(report)], metadata={"slack_channel_id": "C1"})
    )

    assert _lines_at(slack_logs, logging.INFO) == [
        "[SlackChannel] delivered file: channel=C1 filename=report.pdf "
        "bytes=5 file_id=- file=1/1"
    ]


@pytest.mark.asyncio
async def test_a_failed_send_still_raises_and_logs_no_delivery(
    slack_logs,
) -> None:
    channel = SlackChannel(SlackChannelConfig(enabled=True), RobotMessageRouter())
    channel._client = _FailingSlackClient(fail_from=0)

    with pytest.raises(SlackDeliveryError):
        await channel.send(_message(metadata={"slack_channel_id": "C1"}))

    assert _lines_at(slack_logs, logging.INFO) == []


@pytest.mark.asyncio
async def test_a_failed_upload_still_raises_and_logs_no_delivery(
    slack_logs, tmp_path
) -> None:
    report = tmp_path / "report.pdf"
    report.write_bytes(b"%PDF-")
    channel = SlackChannel(SlackChannelConfig(enabled=True), RobotMessageRouter())
    channel._client = _UploadingSlackClient(
        fail_from=0, error=RuntimeError("missing_scope")
    )

    with pytest.raises(SlackDeliveryError):
        await channel.send(
            _file_message(files=[str(report)], metadata={"slack_channel_id": "C1"})
        )

    assert _lines_at(slack_logs, logging.INFO) == []


@pytest.mark.asyncio
async def test_reaction_success_is_logged_at_debug_and_stays_best_effort(
    slack_logs,
) -> None:
    channel = SlackChannel(SlackChannelConfig(enabled=True), RobotMessageRouter())
    client = _FakeSlackClient()
    channel._client = client

    failure = await channel._add_reaction("C1", "1710000000.000100", ":eyes:")

    assert failure is None
    assert len(client.reactions) == 1
    assert _lines_at(slack_logs, logging.INFO) == []
    assert _lines_at(slack_logs, logging.DEBUG) == [
        "[SlackChannel] reaction added: channel=C1 ts=1710000000.000100 emoji=eyes"
    ]




# --- the standing prompt a scope settled ------------------------------------
#
# _channel_prompt is two layers deep and both are silent when wrong: a prompt
# resolved from the layer below, or from no layer at all, is a message that
# looks entirely normal and simply never includes the instruction the operator
# wrote. So the chain is pinned per layer here, and the platform layer is
# followed all the way onto a dispatched message.


def _prompt_channel(**overrides: Any) -> tuple[SlackChannel, list[Message]]:
    channel = SlackChannel(
        SlackChannelConfig(
            enabled=True,
            group_chat_mode="all",
            **overrides,
        ),
        RobotMessageRouter(),
    )
    channel._running = True
    channel._bot_user_id = "U-BOT"
    received: list[Message] = []
    channel.on_message(received.append)
    return channel, received


def test_a_conversation_scope_prompt_is_what_that_conversation_gets() -> None:
    channel, _ = _prompt_channel(
        conversation_overrides={"C-A": SlackChannelOverride(prompt="Be terse.")},
        platform_override=SlackChannelOverride(prompt="Answer in English."),
    )

    assert channel._channel_prompt("C-A") == "Be terse."


def test_the_platform_scope_prompt_covers_a_conversation_nobody_named() -> None:
    channel, _ = _prompt_channel(
        platform_override=SlackChannelOverride(prompt="Answer in English."),
    )

    assert channel._channel_prompt("C-UNNAMED") == "Answer in English."


def test_the_platform_scope_prompt_covers_a_conversation_that_set_other_keys() -> None:
    # The layers settle per key. A conversation whose scope named only a mode
    # has said nothing about the prompt, so the platform layer still applies --
    # the mistake this guards is reading "the conversation has an entry" as
    # "the conversation has a prompt".
    channel, _ = _prompt_channel(
        conversation_overrides={"C-A": SlackChannelOverride(mode=frozenset({"all"}))},
        platform_override=SlackChannelOverride(prompt="Answer in English."),
    )

    assert channel._channel_prompt("C-A") == "Answer in English."


def test_a_conversation_that_asked_for_no_prompt_does_not_fall_back() -> None:
    # "" is a value, not an absence: a scope that wrote prompt: "" asked for
    # nothing to be appended there, and falling through to the platform layer
    # would answer with the opposite of what it asked for.
    channel, _ = _prompt_channel(
        conversation_overrides={"C-A": SlackChannelOverride(prompt="")},
        platform_override=SlackChannelOverride(prompt="Answer in English."),
    )

    assert channel._channel_prompt("C-A") == ""


def test_no_scope_at_all_appends_nothing() -> None:
    channel, _ = _prompt_channel()

    assert channel._channel_prompt("C-A") == ""


@pytest.mark.asyncio
async def test_the_platform_scope_prompt_reaches_a_dispatched_message() -> None:
    # The end of the chain, because every failure above it is invisible: the
    # message is dispatched either way and only the instruction goes missing.
    channel, received = _prompt_channel(
        platform_override=SlackChannelOverride(prompt="Answer in English."),
    )

    await channel._handle_message_event(
        {
            "type": "message",
            "channel_type": "channel",
            "channel": "C-UNNAMED",
            "user": "U1",
            "text": "team status update",
            "ts": "1710000040.000100",
        },
        {"event_id": "EvPlatformPrompt", "team_id": "T1"},
    )

    # The trigger label appears because a prompt does: a prompt may be written
    # to branch on which predicate woke the bot, so it is always told. The
    # location marker leads it on the same line, and the prompt keeps the blank
    # line under both.
    assert len(received) == 1
    assert received[0].params["content"] == (
        "team status update\n\n"
        "[ts: 1710000040.000100] [trigger: all]\n\n"
        "Answer in English."
    )


# --- allowed_channel_ids and the conversation a scope opts in ---------------
#
# The exemption runs in both directions and each is silent when wrong. A
# conversation a scope names is opted in by that alone and answers even though
# allowed_channel_ids does not list it -- lose that and the operator's rule
# quietly does nothing. A conversation nothing named is still held to the
# allowlist -- lose that and the bot answers across a workspace it was never
# invited into. Nothing else asserts either direction.


def _allowlist_channel(**overrides: Any) -> tuple[SlackChannel, list[Message]]:
    channel = SlackChannel(
        SlackChannelConfig(
            enabled=True,
            allowed_channel_ids=["C-ELSEWHERE"],
            **overrides,
        ),
        RobotMessageRouter(),
    )
    channel._running = True
    channel._bot_user_id = "U-BOT"
    received: list[Message] = []
    channel.on_message(received.append)
    return channel, received


def _channel_message(*, channel: str = "C-A", ts: str) -> dict[str, Any]:
    return {
        "type": "message",
        "channel_type": "channel",
        "channel": channel,
        "user": "U1",
        "text": "team status update",
        "ts": ts,
    }


@pytest.mark.asyncio
async def test_naming_a_channel_exempts_it_from_the_allowlist() -> None:
    channel, received = _allowlist_channel(
        conversation_overrides={
            "C-A": SlackChannelOverride(mode=frozenset({"mention", "all"}))
        },
    )

    await channel._handle_message_event(
        _channel_message(ts="1710000041.000100"),
        {"event_id": "EvExempt", "team_id": "T1"},
    )

    assert len(received) == 1


@pytest.mark.asyncio
async def test_an_unnamed_channel_is_still_held_to_the_allowlist() -> None:
    channel, received = _allowlist_channel(
        group_chat_mode="all",
        conversation_overrides={
            "C-A": SlackChannelOverride(mode=frozenset({"mention", "all"}))
        },
    )

    await channel._handle_message_event(
        _channel_message(channel="C-OTHER", ts="1710000042.000100"),
        {"event_id": "EvBlocked", "team_id": "T1"},
    )

    assert received == []


# --- the thinking status ----------------------------------------------------
#
# The middle rung of the progress ladder. The acknowledgement above is instant
# and the activity card only appears after activity_card_delay_seconds, so a
# turn that takes a handful of seconds had nothing between the two. These cover
# what the status must not cost: it is added to the reaction rather than
# replacing it, it is skipped rather than guessed at when there is no thread to
# draw it in, and no failure of it may reach the reaction or the reply.


def _dm_event(ts: str = "1710000020.000100") -> dict[str, Any]:
    """A direct message answered at the top of the conversation.

    No ``thread_ts``, so the reply is not threaded either -- which is the case
    the status cannot be set for, and the same case the streamed preview
    already falls back from.
    """
    return {
        "type": "message",
        "channel_type": "im",
        "channel": "D1",
        "user": "U1",
        "text": "what is the status of the build",
        "ts": ts,
    }


def _status_calls(client: _FakeSlackClient) -> list[dict[str, Any]]:
    return [
        call
        for call in client.api_calls
        if call["api_method"] == slack_connect._ASSISTANT_SET_STATUS_METHOD
    ]


def test_the_status_method_is_the_legacy_one() -> None:
    """agents.sessions.setStatus is gated and holds its status for an hour.

    Pinned as a test rather than left to the comment at the constant: the two
    methods differ in whether a missed clear is recoverable, and swapping one
    for the other is a one-word edit that nothing else here would notice.
    """
    assert (
        slack_connect._ASSISTANT_SET_STATUS_METHOD == "assistant.threads.setStatus"
    )


@pytest.mark.asyncio
async def test_thinking_status_is_set_alongside_the_reaction() -> None:
    channel = SlackChannel(
        SlackChannelConfig(
            enabled=True,
            allow_from=["U1"],
            allowed_channel_ids=["C1"],
            reply_in_thread=True,
            thinking_status="is thinking…",
        ),
        RobotMessageRouter(),
    )
    client = _FakeSlackClient()
    channel._running = True
    channel._client = client
    received: list[Message] = []
    channel.on_message(received.append)

    await channel._handle_app_mention(
        _mention_event(), {"event_id": "EvStatus", "team_id": "T1"}
    )

    assert _status_calls(client) == [
        {
            "api_method": "assistant.threads.setStatus",
            "data": {
                "channel_id": "C1",
                "thread_ts": "1710000004.000100",
                "status": "is thinking…",
            },
        }
    ]
    # The reaction is not replaced by it.
    assert client.reactions == [
        {"channel": "C1", "timestamp": "1710000004.000100", "name": "eyes"}
    ]
    assert len(received) == 1


@pytest.mark.asyncio
async def test_thinking_status_is_set_in_the_thread_the_reply_will_use() -> None:
    """Which is what makes the status self-clearing.

    Slack drops it when the app posts into that same thread, so setting it
    anywhere else would leave it standing for the full two-minute timeout after
    a reply the reader has already read.
    """
    channel = SlackChannel(
        SlackChannelConfig(
            enabled=True, allow_from=["U1"], reply_in_thread=True
        ),
        RobotMessageRouter(),
    )
    client = _FakeSlackClient()
    channel._running = True
    channel._client = client
    received: list[Message] = []
    channel.on_message(received.append)

    event = _mention_event(ts="1710000004.000900")
    event["thread_ts"] = "1710000004.000100"
    await channel._handle_app_mention(
        event, {"event_id": "EvStatusThread", "team_id": "T1"}
    )

    assert len(received) == 1
    assert _status_calls(client)[0]["data"]["thread_ts"] == (
        received[0].metadata["slack_thread_ts"]
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "expect_status"),
    [
        ("reaction", True),
        ("text", True),
        ("both", True),
        # off is a deployment asking for no feedback at all, and this is
        # feedback.
        ("off", False),
    ],
)
async def test_thinking_status_follows_acknowledge_mode(
    mode: str, expect_status: bool
) -> None:
    channel = SlackChannel(
        SlackChannelConfig(
            enabled=True,
            allow_from=["U1"],
            reply_in_thread=True,
            acknowledge_mode=mode,
        ),
        RobotMessageRouter(),
    )
    client = _FakeSlackClient()
    channel._running = True
    channel._client = client
    channel.on_message(lambda _: None)

    await channel._handle_app_mention(
        _mention_event(), {"event_id": f"EvStatusMode-{mode}", "team_id": "T1"}
    )

    assert bool(_status_calls(client)) is expect_status


@pytest.mark.asyncio
async def test_blank_thinking_status_turns_it_off() -> None:
    """Empty is the whole off switch; there is no separate mode for it."""
    channel = SlackChannel(
        SlackChannelConfig(
            enabled=True,
            allow_from=["U1"],
            reply_in_thread=True,
            thinking_status="   ",
        ),
        RobotMessageRouter(),
    )
    client = _FakeSlackClient()
    channel._running = True
    channel._client = client
    received: list[Message] = []
    channel.on_message(received.append)

    await channel._handle_app_mention(
        _mention_event(), {"event_id": "EvStatusBlank", "team_id": "T1"}
    )

    assert _status_calls(client) == []
    assert len(client.reactions) == 1
    assert len(received) == 1


@pytest.mark.asyncio
async def test_a_thread_less_reply_gets_no_status_and_no_error() -> None:
    """A DM answered at the top of the conversation has nowhere to put one.

    The method needs a thread_ts, so this degrades exactly where the streamed
    preview already does. Silently, and with the reaction still there.
    """
    channel = SlackChannel(
        SlackChannelConfig(enabled=True, allow_from=["U1"]), RobotMessageRouter()
    )
    client = _FakeSlackClient()
    channel._running = True
    channel._client = client
    received: list[Message] = []
    channel.on_message(received.append)

    await channel._handle_message_event(
        _dm_event(), {"event_id": "EvStatusDM", "team_id": "T1"}
    )

    assert _status_calls(client) == []
    assert client.reactions == [
        {"channel": "D1", "timestamp": "1710000020.000100", "name": "eyes"}
    ]
    assert len(received) == 1


@pytest.mark.asyncio
async def test_status_failure_disturbs_neither_the_reaction_nor_the_reply(
    slack_logs,
) -> None:
    """A status line is not worth a lost request, or a lost acknowledgement.

    The reaction is added before the status is attempted, so a workspace that
    refuses the method is left with exactly the acknowledgement it had before
    this rung existed.
    """

    class _RefusingStatusClient(_FakeSlackClient):
        async def api_call(self, api_method: str, **kwargs: Any) -> dict[str, bool]:
            self.api_calls.append({"api_method": api_method, **kwargs})
            raise RuntimeError("method_not_supported")

    channel = SlackChannel(
        SlackChannelConfig(
            enabled=True, allow_from=["U1"], reply_in_thread=True
        ),
        RobotMessageRouter(),
    )
    client = _RefusingStatusClient()
    channel._running = True
    channel._client = client
    received: list[Message] = []
    channel.on_message(received.append)

    await channel._handle_app_mention(
        _mention_event(), {"event_id": "EvStatusBoom", "team_id": "T1"}
    )

    assert len(_status_calls(client)) == 1
    assert len(client.reactions) == 1
    assert len(received) == 1
    # DEBUG, not WARNING: nothing has degraded that the reader can see, and a
    # workspace where the method is unavailable would warn per message forever.
    assert _lines_at(slack_logs, logging.WARNING) == []
    assert (
        "Slack assistant.threads.setStatus failed: method_not_supported"
        in _lines_at(slack_logs, logging.DEBUG)
    )


@pytest.mark.asyncio
async def test_a_client_without_api_call_still_acknowledges_and_dispatches() -> None:
    """slack_sdk grew api_call long ago, but nothing here depends on it.

    The status is the only caller in the acknowledgement path, and it must be
    the kind of thing a client that cannot do it at all simply does not do.
    """

    class _NoApiCallClient:
        def __init__(self) -> None:
            self.reactions: list[dict[str, Any]] = []

        async def reactions_add(self, **kwargs: Any) -> dict[str, bool]:
            self.reactions.append(kwargs)
            return {"ok": True}

    channel = SlackChannel(
        SlackChannelConfig(
            enabled=True, allow_from=["U1"], reply_in_thread=True
        ),
        RobotMessageRouter(),
    )
    client = _NoApiCallClient()
    channel._running = True
    channel._client = client
    received: list[Message] = []
    channel.on_message(received.append)

    await channel._handle_app_mention(
        _mention_event(), {"event_id": "EvStatusNoApi", "team_id": "T1"}
    )

    assert len(client.reactions) == 1
    assert len(received) == 1


def test_the_shipped_template_default_matches_the_dataclass() -> None:
    """A template that disagrees with the code hands operators a silent change.

    Read off the two documents rather than through the merge, the same way
    test_config_template_key_coverage measures the neighbouring property.
    """
    import yaml

    template = yaml.safe_load(
        (
            Path(slack_connect.__file__).resolve().parents[4]
            / "resources"
            / "config.yaml"
        ).read_text(encoding="utf-8")
    )
    shipped = template["channels"]["slack"]
    assert "thinking_status" in shipped
    assert shipped["thinking_status"] == SlackChannelConfig().thinking_status
    assert SlackChannelConfig().thinking_status == (
        slack_connect.DEFAULT_THINKING_STATUS
    )
    # app_messages is a word, and the narrow one has to survive YAML 1.1
    # unquoted the way history's "disabled" was chosen to. "none" is not one of
    # that dialect's null tokens -- those are null/Null/NULL/~/empty -- so it
    # arrives here as the string it was written as, and this is what would say
    # so if it ever stopped being true.
    assert shipped["app_messages"] == "none"
    assert shipped["app_messages"] == SlackChannelConfig().app_messages
    assert shipped["app_messages"] == slack_connect.APP_MESSAGES_DEFAULT
    # Shipped empty, and empty admits nothing: an upgrade into these two keys
    # cannot widen a deployment that has not written them.
    assert shipped["app_messages_from"] == []
    assert SlackChannelConfig().app_messages_from == ()


# --- the envelope marker and where an answer lands ---------------------------
#
# Two facts about a message that the message itself does not state. Its own
# location is appended to the text, because an agent that cannot name the
# position of the message it is answering cannot pass ts, after_ts or
# before_ts to the history tool for the conversation it is in. The answer's
# location is settled from the same field. Both are silent when wrong: the
# turn runs and the answer is posted either way, and the reader is the only
# one who loses the connection.


def _marker_channel(**overrides: Any) -> tuple[SlackChannel, list[Message]]:
    channel = SlackChannel(
        SlackChannelConfig(
            enabled=True,
            group_chat_mode="all",
            **overrides,
        ),
        RobotMessageRouter(),
    )
    channel._running = True
    channel._bot_user_id = "U-BOT"
    received: list[Message] = []
    channel.on_message(received.append)
    return channel, received


@pytest.mark.asyncio
async def test_a_threaded_message_is_answered_in_its_thread_with_the_option_off(
) -> None:
    """``reply_in_thread`` governs opening a thread on a root-level message.

    With the option off, a message posted inside an existing thread was
    answered at channel root, detached from the conversation it answered. The
    detachment is invisible from inside the turn: ``session_id`` is keyed on
    the thread root whichever way the reply went, so the context was kept and
    the reader was the only one who lost the connection.
    """
    channel, received = _marker_channel(reply_in_thread=False)

    await channel._handle_message_event(
        {
            "type": "message",
            "channel_type": "channel",
            "channel": "C-THREADED",
            "user": "U1",
            "text": "and the second half?",
            "ts": "1710000050.000200",
            "thread_ts": "1710000050.000100",
        },
        {"event_id": "EvThreadedAnswer", "team_id": "T1"},
    )

    assert len(received) == 1
    assert received[0].metadata["slack_thread_ts"] == "1710000050.000100"
    assert received[0].session_id == "slack_T1_C-THREADED_1710000050.000100"


@pytest.mark.asyncio
async def test_a_root_level_message_still_answers_at_root_with_the_option_off(
) -> None:
    """The half of the option the fix above must leave alone.

    Answering here in a thread would turn ``reply_in_thread: false`` into a
    setting that does nothing at all.
    """
    channel, received = _marker_channel(reply_in_thread=False)

    await channel._handle_message_event(
        {
            "type": "message",
            "channel_type": "channel",
            "channel": "C-ROOTED",
            "user": "U1",
            "text": "status?",
            "ts": "1710000060.000100",
        },
        {"event_id": "EvRootedAnswer", "team_id": "T1"},
    )

    assert len(received) == 1
    assert received[0].metadata["slack_thread_ts"] == ""


@pytest.mark.asyncio
async def test_the_envelope_marker_names_the_thread_a_message_sits_in() -> None:
    """The marker on its own, with no trigger label beside it.

    ``all`` explains itself, so no label is emitted without a prompt, which
    leaves the marker as the whole appended block. ``thread_ts`` is present
    because the event has one.
    """
    channel, received = _marker_channel()

    await channel._handle_message_event(
        {
            "type": "message",
            "channel_type": "channel",
            "channel": "C-MARKER",
            "user": "U1",
            "text": "and the second half?",
            "ts": "1710000050.000200",
            "thread_ts": "1710000050.000100",
        },
        {"event_id": "EvMarkerThread", "team_id": "T1"},
    )

    assert len(received) == 1
    assert received[0].params["content"] == (
        "and the second half?\n\n"
        "[ts: 1710000050.000200, thread_ts: 1710000050.000100]"
    )
    # Both fields reach the text the agent is handed, not the metadata alone.
    # A value the agent cannot read is a value it cannot pass to a tool.
    assert received[0].params["content"] == received[0].params["query"]


def test_the_envelope_marker_omits_thread_ts_at_channel_root() -> None:
    """An empty ``thread_ts`` would read as a thread whose id went missing.

    Pinned on the helper as well as through a dispatch. The caller passes the
    event's own ``thread_ts``, and a future caller reaching for the resolved
    thread root instead would put every root-level message in a thread of one.
    That output reads as correct at a glance.
    """
    assert slack_connect.location_label("1710000050.000200", "") == (
        "[ts: 1710000050.000200]"
    )
    assert slack_connect.location_label("1710000050.000200", "   ") == (
        "[ts: 1710000050.000200]"
    )


def test_a_message_with_no_timestamp_gets_no_marker_at_all() -> None:
    """A marker naming an empty position is worse than none.

    ``[ts: ]`` reads as a position the tools take, and the value it hands over
    selects nothing.
    """
    assert slack_connect.location_label("", "1710000050.000100") == ""
    assert slack_connect.location_label("   ", "") == ""


@pytest.mark.asyncio
async def test_the_envelope_marker_never_names_the_conversation() -> None:
    """``chat_id`` is left out even though the history tool declares it.

    Omitting the argument reads the conversation the request came from, and
    passing that conversation's own id means the same thing, so the field
    would be inert on every read an agent makes of the conversation it is in.
    The card restricts the argument to a conversation somebody named, and a
    marker supplying an id on every message argues against that. ``ts`` and
    ``thread_ts`` are named because nothing else in the turn states them.
    """
    channel, received = _marker_channel()

    await channel._handle_message_event(
        {
            "type": "message",
            "channel_type": "channel",
            "channel": "C-NAMEDNOWHERE",
            "user": "U1",
            "text": "what did I miss?",
            "ts": "1710000080.000100",
        },
        {"event_id": "EvMarkerNoChannel", "team_id": "T1"},
    )

    assert len(received) == 1
    content = received[0].params["content"]
    assert content == "what did I miss?\n\n[ts: 1710000080.000100]"
    assert "chat_id" not in content
    assert "C-NAMEDNOWHERE" not in content
    # The conversation still reaches the runtime, on the metadata the history
    # tool derives its target from. Naming it in the text adds nothing there.
    assert received[0].metadata["slack_channel_id"] == "C-NAMEDNOWHERE"
