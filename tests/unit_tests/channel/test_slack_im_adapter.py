# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Unit tests for the Slack IMPlatformAdapter implementation."""

from __future__ import annotations

import json
from typing import Any

import pytest

from jiuwenswarm.gateway.channel_manager.im_platforms.slack.slack_im_adapter import (
    REPLY_CANDIDATE_USER_ID_KEY,
    REPLY_USER_ID_KEY,
    SlackIMPlatformAdapter,
)


class _FakeToolkit:
    """Stand-in for SlackHistoryToolkit returning a canned JSON snapshot."""

    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload
        self.metadata: dict[str, Any] = {}
        self.calls: list[dict[str, Any]] = []

    async def read_slack_conversation(self, **kwargs: Any) -> str:
        self.calls.append(kwargs)
        return json.dumps(self.payload)


def _snapshot(*messages: dict[str, Any]) -> dict[str, Any]:
    return {"ok": True, "messages": list(messages)}


def _adapter(
    payload: dict[str, Any] | None = None,
    **kwargs: Any,
) -> tuple[SlackIMPlatformAdapter, list[_FakeToolkit]]:
    built: list[_FakeToolkit] = []

    def factory(metadata: dict[str, Any]) -> _FakeToolkit:
        toolkit = _FakeToolkit(payload if payload is not None else _snapshot())
        toolkit.metadata = metadata
        built.append(toolkit)
        return toolkit

    kwargs.setdefault("my_user_id", "UPRINCIPAL")
    kwargs.setdefault("bot_name", "Swarm")
    kwargs.setdefault("bot_user_id", "UBOT")
    return SlackIMPlatformAdapter(toolkit_factory=factory, **kwargs), built


def test_adapter_satisfies_the_protocol():
    adapter, _ = _adapter()
    # IMPlatformAdapter is a plain Protocol, so structural conformance has to be
    # asserted by name rather than with isinstance.
    for name in (
        "get_principal_user_id",
        "get_principal_display_name",
        "resolve_user_display_name",
        "get_bot_mention_tokens",
        "load_recent_messages",
        "build_relevance_metadata",
        "get_candidate_user_id",
    ):
        assert callable(getattr(adapter, name)), name
    for name in ("platform_name", "reply_user_id_key", "use_keyword_override"):
        assert isinstance(getattr(type(adapter), name), property), name
    assert adapter.channel_id == "slack"
    assert adapter.platform_name == "Slack"
    assert adapter.reply_user_id_key == REPLY_USER_ID_KEY
    assert adapter.use_keyword_override is True


def test_principal_identity_prefers_the_configured_name():
    adapter, _ = _adapter(principal_name="Ada")
    assert adapter.get_principal_user_id() == "UPRINCIPAL"
    assert adapter.get_principal_display_name() == "Ada"


def test_principal_display_name_falls_back_to_the_user_id():
    adapter, _ = _adapter()
    assert adapter.get_principal_display_name() == "UPRINCIPAL"


def test_bot_mention_tokens_cover_both_slack_and_plain_forms():
    adapter, _ = _adapter()
    assert adapter.get_bot_mention_tokens() == ["<@UBOT>", "@Swarm"]


def test_bot_mention_tokens_omit_unknown_parts():
    adapter, _ = _adapter(bot_user_id="", bot_name="")
    assert adapter.get_bot_mention_tokens() == []


async def test_observe_message_backfills_history_and_names():
    payload = _snapshot(
        {
            "ts": "1710000000.000100",
            "author_user_id": "UALICE",
            "author_name": "Alice",
            "text": "old message",
        }
    )
    adapter, built = _adapter(payload)

    await adapter.observe_message(
        channel_id="C1",
        channel_type="channel",
        user_id="UBOB",
        text="live message",
        message_ts="1710000100.000200",
    )

    assert len(built) == 1
    assert built[0].metadata == {
        "slack_channel_id": "C1",
        "slack_channel_type": "channel",
        # This adapter reads the conversation it is already in and no other,
        # which is what origin names. Stamped rather than left absent: the
        # toolkit reads an absent policy as "no side that has the configuration
        # settled this request" and refuses, and the failure here would be
        # silent -- an empty context cache reads as a quiet channel.
        "slack_history_policy": "origin",
    }
    history = adapter.load_recent_messages("C1")
    assert [(item.user_name, item.content) for item in history] == [
        ("Alice", "old message"),
        ("UBOB", "live message"),
    ]
    assert history[0].timestamp_ms == 1710000000000
    assert adapter.resolve_user_display_name("UALICE") == "Alice"


async def test_live_message_picks_up_a_name_learned_later():
    payload = _snapshot()
    adapter, built = _adapter(payload, refresh_interval_seconds=0.0)

    await adapter.observe_message(
        channel_id="C1",
        channel_type="channel",
        user_id="UBOB",
        text="live message",
        message_ts="1710000100.000200",
    )
    assert adapter.load_recent_messages("C1")[0].user_name == "UBOB"

    payload["messages"] = [
        {
            "ts": "1710000050.000000",
            "author_user_id": "UBOB",
            "author_name": "Bob",
            "text": "earlier",
        }
    ]
    await adapter.observe_message(
        channel_id="C1",
        channel_type="channel",
        user_id="UBOB",
        text="second live message",
        message_ts="1710000200.000300",
    )

    names = [item.user_name for item in adapter.load_recent_messages("C1")]
    assert names == ["Bob", "Bob", "Bob"]


async def test_refresh_is_rate_limited_between_messages():
    clock = [0.0]
    adapter, built = _adapter(
        _snapshot(),
        refresh_interval_seconds=300.0,
        now=lambda: clock[0],
    )

    for index in range(3):
        await adapter.observe_message(
            channel_id="C1",
            channel_type="channel",
            user_id="UBOB",
            text=f"message {index}",
            message_ts=f"171000000{index}.000000",
        )
    assert len(built) == 1

    clock[0] = 301.0
    await adapter.observe_message(
        channel_id="C1",
        channel_type="channel",
        user_id="UBOB",
        text="later",
        message_ts="1710000400.000000",
    )
    assert len(built) == 2
    # Every message is cached regardless of whether it triggered a refresh.
    assert len(adapter.load_recent_messages("C1")) == 4


async def test_direct_messages_never_trigger_a_history_scan():
    adapter, built = _adapter(_snapshot())
    await adapter.observe_message(
        channel_id="D1",
        channel_type="im",
        user_id="UBOB",
        text="hello",
        message_ts="1710000000.000100",
    )
    assert built == []


async def test_a_failing_snapshot_does_not_break_message_capture():
    class _Boom:
        async def read_slack_conversation(self, **_: Any) -> str:
            raise RuntimeError("slack is down")

    adapter = SlackIMPlatformAdapter(
        my_user_id="UPRINCIPAL",
        toolkit_factory=lambda metadata: _Boom(),
    )
    await adapter.observe_message(
        channel_id="C1",
        channel_type="channel",
        user_id="UBOB",
        text="still handled",
        message_ts="1710000000.000100",
    )
    # The scan blew up, but the message it was handed directly is still cached
    # and the failure never reaches the connector.
    assert [item.content for item in adapter.load_recent_messages("C1")] == [
        "still handled"
    ]


async def test_a_not_ok_snapshot_is_ignored():
    adapter, _ = _adapter({"ok": False, "error": "channel_not_found", "messages": []})
    await adapter.observe_message(
        channel_id="C1",
        channel_type="channel",
        user_id="UBOB",
        text="live",
        message_ts="1710000000.000100",
    )
    assert [item.content for item in adapter.load_recent_messages("C1")] == ["live"]


async def test_backfill_does_not_duplicate_an_already_seen_message():
    payload = _snapshot()
    adapter, _ = _adapter(payload, refresh_interval_seconds=0.0)
    await adapter.observe_message(
        channel_id="C1",
        channel_type="channel",
        user_id="UBOB",
        text="live",
        message_ts="1710000000.000100",
    )
    payload["messages"] = [
        {
            "ts": "1710000000.000100",
            "author_user_id": "UBOB",
            "author_name": "Bob",
            "text": "live",
        }
    ]
    await adapter.observe_message(
        channel_id="C1",
        channel_type="channel",
        user_id="UBOB",
        text="next",
        message_ts="1710000001.000100",
    )
    assert [item.content for item in adapter.load_recent_messages("C1")] == [
        "live",
        "next",
    ]


async def test_history_cache_is_bounded():
    adapter, _ = _adapter(_snapshot())
    for index in range(520):
        await adapter.observe_message(
            channel_id="C1",
            channel_type="channel",
            user_id="UBOB",
            text=f"message {index}",
            message_ts=f"{1710000000 + index}.000000",
        )
    history = adapter.load_recent_messages("C1", limit=1000)
    assert len(history) == 500
    assert history[-1].content == "message 519"


def test_load_recent_messages_for_an_unknown_channel_is_empty():
    adapter, _ = _adapter()
    assert adapter.load_recent_messages("C-nope") == []


def test_relevance_metadata_names_the_principal_as_the_reply_candidate():
    adapter, _ = _adapter(principal_name="Ada")
    patch = adapter.build_relevance_metadata(
        {"chat_type": "group"},
        sender_user_id="UBOB",
        relevant=True,
    )
    assert patch == {
        REPLY_CANDIDATE_USER_ID_KEY: "UPRINCIPAL",
        "reply_candidate_reason": "processor_target_user",
        "reply_candidate_user_id": "UPRINCIPAL",
        "reply_target_name": "Ada",
    }


@pytest.mark.parametrize(
    ("metadata", "sender_user_id", "relevant"),
    [
        ({"chat_type": "group"}, "UBOB", False),
        ({"chat_type": "im"}, "UBOB", True),
        ({"chat_type": "group", "reply_scope": "dm"}, "UBOB", True),
        ({"chat_type": "group"}, "UPRINCIPAL", True),
    ],
)
def test_relevance_metadata_stays_out_of_the_way(metadata, sender_user_id, relevant):
    adapter, _ = _adapter()
    assert (
        adapter.build_relevance_metadata(
            metadata, sender_user_id=sender_user_id, relevant=relevant
        )
        == {}
    )


def test_relevance_metadata_needs_a_configured_principal():
    adapter, _ = _adapter(my_user_id="")
    assert (
        adapter.build_relevance_metadata(
            {"chat_type": "group"}, sender_user_id="UBOB", relevant=True
        )
        == {}
    )


def test_candidate_user_id_reads_the_patch_key():
    adapter, _ = _adapter()
    assert (
        adapter.get_candidate_user_id({REPLY_CANDIDATE_USER_ID_KEY: "UPRINCIPAL"})
        == "UPRINCIPAL"
    )


def test_candidate_user_id_backfills_the_metadata_when_absent():
    adapter, _ = _adapter()
    metadata = {"chat_type": "group", "im_sender_user_id": "UBOB"}
    assert adapter.get_candidate_user_id(metadata) == "UPRINCIPAL"
    assert metadata[REPLY_CANDIDATE_USER_ID_KEY] == "UPRINCIPAL"


def test_candidate_user_id_is_empty_outside_a_group_chat():
    adapter, _ = _adapter()
    assert adapter.get_candidate_user_id({"chat_type": "im"}) == ""
