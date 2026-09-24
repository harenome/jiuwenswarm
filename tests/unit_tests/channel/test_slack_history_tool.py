# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Unit tests for the request-scoped Slack history toolkit."""

from __future__ import annotations

import asyncio
import json
from collections import defaultdict
from contextvars import ContextVar
from pathlib import Path
from typing import Any

import pytest

import jiuwenswarm.common.config as config_module
import jiuwenswarm.common.slack_rich_text as slack_rich_text
from jiuwenswarm.agents.harness.common.tools import slack_history
from jiuwenswarm.agents.harness.common.tools.slack_history import SlackHistoryToolkit

from tests.unit_tests.channel.slack_card_rules import CANONICAL_CARD_SENTENCES


class _FakeClient:
    def __init__(self, responses: dict[str, list[Any]]) -> None:
        self.responses = {name: list(items) for name, items in responses.items()}
        self.calls: dict[str, list[dict[str, Any]]] = defaultdict(list)

    async def _respond(self, method: str, kwargs: dict[str, Any]) -> Any:
        self.calls[method].append(kwargs)
        item = self.responses[method].pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    async def auth_test(self, **kwargs: Any) -> Any:
        return await self._respond("auth_test", kwargs)

    async def conversations_history(self, **kwargs: Any) -> Any:
        return await self._respond("conversations_history", kwargs)

    async def conversations_replies(self, **kwargs: Any) -> Any:
        return await self._respond("conversations_replies", kwargs)

    async def conversations_info(self, **kwargs: Any) -> Any:
        # Declined by a test that scripts no answer, which is what a workspace
        # without the scope does. A read must survive that with its messages.
        return await self._respond("conversations_info", kwargs)

    async def users_info(self, **kwargs: Any) -> Any:
        return await self._respond("users_info", kwargs)


class _ConcurrentClient:
    def __init__(self) -> None:
        self.history_channels: list[str] = []

    async def auth_test(self, **kwargs: Any) -> dict[str, Any]:
        await asyncio.sleep(0)
        return {"user_id": "U-BOT"}

    async def conversations_history(self, **kwargs: Any) -> dict[str, Any]:
        channel = str(kwargs["channel"])
        self.history_channels.append(channel)
        await asyncio.sleep(0)
        return {
            "messages": [{"ts": "199999.0", "user": "U1", "text": f"from {channel}"}]
        }


class _FakeResponse:
    def __init__(
        self,
        *,
        status_code: int,
        data: dict[str, Any],
        headers: dict[str, Any] | None = None,
    ) -> None:
        self.status_code = status_code
        self.data = data
        self.headers = headers or {}


class _FakeSlackError(Exception):
    def __init__(self, response: _FakeResponse) -> None:
        super().__init__("sanitized fake failure")
        self.response = response


@pytest.fixture(autouse=True)
def _config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        config_module,
        "get_config",
        lambda: {"channels": {"slack": {"bot_token": "xoxb-config-secret"}}},
    )


def _toolkit(client: _FakeClient, **kwargs: Any) -> SlackHistoryToolkit:
    return SlackHistoryToolkit(
        metadata={
            "slack_channel_id": "C-RESEARCH",
            "slack_channel_type": "channel",
            "slack_history_policy": "origin",
        },
        client=client,
        now=lambda: 200_000.0,
        max_user_lookups=0,
        **kwargs,
    )


@pytest.mark.asyncio
async def test_requires_trusted_request_channel_and_card_has_no_channel_argument() -> (
    None
):
    client = _FakeClient({})
    toolkit = SlackHistoryToolkit(metadata={"slack_channel_id": "C1"}, client=client)

    result = json.loads(await toolkit.read_slack_conversation())

    assert result == {
        "ok": False,
        "error": "trusted_slack_channel_context_required",
        "messages": [],
    }
    assert not client.calls
    card = toolkit.get_tools()[0]._card
    assert card.name == "read_slack_conversation"
    assert "chat_id" not in card.input_params["properties"]


def test_the_shared_rules_are_worded_the_way_the_other_card_words_them() -> None:
    """One rule, one sentence, across both Slack cards.

    Three rules apply to both tools and were stated twice in different words:
    the untrusted-data warning, the ts-is-not-a-date rule, and the
    never-build-a-link rule. Two wordings of one rule is worse than saying it
    twice, because a model reading both has to decide whether the difference is
    meaningful.

    All three concern this tool's own results and refer to nothing outside it,
    so a model given this card and not the other one loses nothing by the
    sharing.
    """
    toolkit = SlackHistoryToolkit(
        metadata={"slack_channel_id": "C1"}, client=_FakeClient({})
    )
    description = toolkit.get_tools()[0]._card.description
    for sentence in CANONICAL_CARD_SENTENCES:
        assert sentence in description, sentence


def test_the_card_states_its_own_paging_without_naming_the_other_tool() -> None:
    """Paging is stated for this tool alone, not as a contrast with search.

    The rule a model needs here is the direction: a further slice is older,
    which is what makes before_ts and next_before_ts mean what they say. That
    is sayable without mentioning the search tool, and has to be -- this tool's
    own default is disabled and search is switched on separately, so on most
    deployments the contrast would describe a tool that is not mounted.

    The card no longer says paging never goes forward, because after_ts does.
    The two are one rule and not two: paging walks back, and going forward is
    a separate argument rather than a slice, so the card names both and keeps
    the direction attached to the argument that has it.
    """
    toolkit = SlackHistoryToolkit(
        metadata={"slack_channel_id": "C1"}, client=_FakeClient({})
    )
    description = toolkit.get_tools()[0]._card.description
    assert "Paging walks backwards in time" in description
    assert "older than the one before it" in description
    assert "To go forward instead" in description
    assert "never newer" not in description
    for sibling_claim in ("The two Slack tools", "search_slack_workspace"):
        assert sibling_claim not in description, sibling_claim


def test_the_card_describes_the_tool_and_leaves_the_task_to_the_skill() -> None:
    """A card says what a tool is; a skill says what to do with it.

    The card used to give digest instructions -- write it yourself, in
    Markdown, link each claim to its evidence -- all of which the digest skill
    already said, more fully and where the task actually lives. A card is
    permanent context on every turn that mounts the tool, including every turn
    that is not writing a digest, so task policy there is paid for constantly
    and read by the wrong callers.

    What stays is the half that was capability wearing task clothing: a
    permalink returned here is authoritative and a link assembled from parts is
    a guess, which is true of every use of this tool and of no particular task.
    """
    toolkit = SlackHistoryToolkit(
        metadata={"slack_channel_id": "C1"}, client=_FakeClient({})
    )
    description = toolkit.get_tools()[0]._card.description

    for policy in (
        "For a channel digest",
        "write the digest yourself",
        "in Markdown",
        "Link a claim to its evidence",
    ):
        assert policy not in description

    # And the capability half is still said, because nothing else says it.
    assert "Copy a permalink verbatim from this result" in description
    assert "building a Slack link from parts" in description


@pytest.mark.asyncio
async def test_rejects_multipart_direct_message_context() -> None:
    client = _FakeClient({})
    toolkit = SlackHistoryToolkit(
        metadata={
            "slack_channel_id": "G1",
            "slack_channel_type": "mpim",
            "slack_history_policy": "origin",
        },
        client=client,
    )

    result = json.loads(await toolkit.read_slack_conversation())

    assert result["ok"] is False
    assert result["error"] == "trusted_slack_channel_context_required"
    assert not client.calls


@pytest.mark.asyncio
@pytest.mark.parametrize("hours", [float("nan"), float("inf"), float("-inf")])
async def test_rejects_non_finite_hours(hours: float) -> None:
    client = _FakeClient({})

    result = json.loads(
        await _toolkit(client).read_slack_conversation(hours=hours)
    )

    assert result == {
        "ok": False,
        "error": "hours_must_be_finite",
        "messages": [],
    }
    assert not client.calls


@pytest.mark.asyncio
async def test_client_receives_token_only_from_resolved_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _FakeClient(
        {
            "auth_test": [{"user_id": "U-BOT"}],
            "conversations_history": [{"messages": []}],
        }
    )
    captured: dict[str, str] = {}

    def client_factory(*, token: str) -> _FakeClient:
        captured["token"] = token
        return client

    monkeypatch.setattr(slack_history, "AsyncWebClient", client_factory)
    toolkit = SlackHistoryToolkit(
        metadata={
            "slack_channel_id": "C1",
            "slack_channel_type": "channel",
            "slack_history_policy": "origin",
        },
        now=lambda: 200_000.0,
        max_user_lookups=0,
    )

    result = json.loads(await toolkit.read_slack_conversation())

    assert result["ok"] is True
    assert captured == {"token": "xoxb-config-secret"}
    assert "xoxb-config-secret" not in json.dumps(result)


@pytest.mark.asyncio
async def test_undocumented_history_limit_config_is_ignored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        config_module,
        "get_config",
        lambda: {
            "channels": {
                "slack": {
                    "bot_token": "xoxb-config-secret",
                    "history_digest_max_messages": 1,
                    "history_digest_max_api_calls": 1,
                }
            }
        },
    )
    client = _FakeClient(
        {
            "auth_test": [{"user_id": "U-BOT"}],
            "conversations_history": [
                {
                    "messages": [
                        {"ts": "199999.0", "user": "U1", "text": "first"},
                        {"ts": "199998.0", "user": "U2", "text": "second"},
                    ]
                }
            ],
        }
    )

    result = json.loads(await _toolkit(client).read_slack_conversation())

    assert result["ok"] is True
    assert result["coverage"]["status"] == "complete"
    assert result["coverage"]["messages_returned"] == 2
    # auth.test, the history page, and the conversations.info the snapshot
    # takes its chat_name from.
    assert result["coverage"]["api_calls"] == 3


@pytest.mark.asyncio
async def test_metadata_provider_isolates_concurrent_channel_requests() -> None:
    current_metadata: ContextVar[dict[str, Any] | None] = ContextVar(
        "slack_history_test_metadata", default=None
    )
    client = _ConcurrentClient()
    toolkit = SlackHistoryToolkit(
        metadata={
            "slack_channel_id": "C-FALLBACK",
            "slack_channel_type": "channel",
            "slack_history_policy": "origin",
        },
        metadata_provider=current_metadata.get,
        client=client,
        now=lambda: 200_000.0,
        max_user_lookups=0,
    )

    async def read_channel(channel_id: str) -> dict[str, Any]:
        token = current_metadata.set(
            {
                "slack_channel_id": channel_id,
                "slack_channel_type": "channel",
                "slack_history_policy": "origin",
            }
        )
        try:
            return json.loads(await toolkit.read_slack_conversation())
        finally:
            current_metadata.reset(token)

    first, second = await asyncio.gather(read_channel("C1"), read_channel("C2"))

    assert first["chat_id"] == "C1"
    assert first["messages"][0]["text"] == "from C1"
    assert second["chat_id"] == "C2"
    assert second["messages"][0]["text"] == "from C2"
    assert sorted(client.history_channels) == ["C1", "C2"]


@pytest.mark.asyncio
async def test_scan_deadline_returns_partial_and_keeps_messages_on_name_lookup() -> (
    None
):
    ticks = iter([0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 91.0])
    client = _FakeClient(
        {
            "auth_test": [{"user_id": "U-BOT"}],
            "conversations_history": [
                {"messages": [{"ts": "199999.0", "user": "U1", "text": "hi"}]}
            ],
            "users_info": [
                {
                    "user": {
                        "id": "U1",
                        "profile": {"display_name": "Alice"},
                    }
                }
            ],
        }
    )
    toolkit = SlackHistoryToolkit(
        metadata={
            "slack_channel_id": "C1",
            "slack_channel_type": "channel",
            "slack_history_policy": "origin",
        },
        client=client,
        now=lambda: 200_000.0,
        monotonic=lambda: next(ticks, 91.0),
        scan_timeout_seconds=90,
        max_user_lookups=10,
    )

    result = json.loads(await toolkit.read_slack_conversation())

    assert result["ok"] is True
    assert result["coverage"]["status"] == "partial"
    assert "scan_time_limit" in result["coverage"]["partial_reasons"]
    assert result["messages"][0]["author_user_id"] == "U1"
    assert result["messages"][0]["author_name"] == "U1"


@pytest.mark.asyncio
async def test_paginates_roots_and_includes_recent_reply_to_old_root() -> None:
    client = _FakeClient(
        {
            "auth_test": [
                {
                    "ok": True,
                    "user_id": "U-BOT",
                    "bot_id": "B-BOT",
                    "url": "https://example.slack.com/",
                }
            ],
            "conversations_history": [
                {
                    "ok": True,
                    "messages": [
                        {
                            "ts": "190000.000000",
                            "user": "U1",
                            "text": "old design discussion",
                            "reply_count": 1,
                            "latest_reply": "199000.000000",
                        },
                        {
                            "ts": "198500.000000",
                            "user": "U-BOT",
                            "bot_id": "B-BOT",
                            "text": "Received. Analyzing…",
                        },
                    ],
                    "response_metadata": {"next_cursor": "page-2"},
                },
                {
                    "ok": True,
                    "messages": [
                        {
                            "ts": "198000.000000",
                            "user": "U2",
                            "text": "new root",
                        }
                    ],
                    "response_metadata": {"next_cursor": ""},
                },
            ],
            "conversations_replies": [
                {
                    "ok": True,
                    "messages": [
                        {
                            "ts": "190000.000000",
                            "user": "U1",
                            "text": "old design discussion",
                        },
                        {
                            "ts": "195000.000000",
                            "thread_ts": "190000.000000",
                            "user": "U3",
                            "text": "old reply outside the window",
                        },
                        {
                            "ts": "199000.000000",
                            "thread_ts": "190000.000000",
                            "user": "U2",
                            "text": "decision made",
                        },
                    ],
                    "response_metadata": {"next_cursor": ""},
                }
            ],
        }
    )

    result = json.loads(
        await _toolkit(client).read_slack_conversation(hours=1)
    )

    assert result["ok"] is True
    assert result["coverage"]["status"] == "complete"
    assert result["coverage"]["scope_note"].startswith(
        "Coverage describes Slack-accessible history"
    )
    assert result["window"]["cutoff_iso_utc"] == "1970-01-03T06:33:20Z"
    assert result["window"]["snapshot_iso_utc"] == "1970-01-03T07:33:20Z"
    assert result["coverage"]["history_pages"] == 2
    assert result["coverage"]["thread_pages"] == 1
    assert [message["ts"] for message in result["messages"]] == [
        "190000.000000",
        "198000.000000",
        "198500.000000",
        "199000.000000",
    ]
    assert result["messages"][0]["outside_window_context"] is True
    assert result["messages"][2]["is_own_bot_message"] is True
    assert result["messages"][3]["is_thread_reply"] is True
    assert result["messages"][3]["is_own_bot_message"] is False
    assert result["coverage"]["context_root_messages_returned"] == 1
    assert result["coverage"]["threads_returned"] == 1
    assert "thread_ts=190000.000000" in result["messages"][3]["permalink"]
    assert result["messages"][0]["source_mrkdwn"] == (
        f"<{result['messages'][0]['permalink']}|source>"
    )
    assert result["messages"][3]["source_mrkdwn"] == (
        "<https://example.slack.com/archives/C-RESEARCH/p199000000000"
        "?thread_ts=190000.000000&cid=C-RESEARCH|source>"
    )
    assert client.calls["conversations_history"][1]["cursor"] == "page-2"
    # Windowed thread scans intentionally omit `oldest` from root history so
    # older roots with recent replies remain discoverable.
    assert "oldest" not in client.calls["conversations_history"][0]
    # Slack can omit valid replies when conversations.replies receives a
    # non-zero `oldest`; replies are fetched without it and filtered locally.
    assert "oldest" not in client.calls["conversations_replies"][0]


@pytest.mark.asyncio
async def test_reports_partial_when_expected_thread_replies_are_not_returned() -> None:
    client = _FakeClient(
        {
            "auth_test": [{"user_id": "U-BOT"}],
            "conversations_history": [
                {
                    "messages": [
                        {
                            "ts": "198000.0",
                            "user": "U1",
                            "text": "design proposal",
                            "reply_count": 4,
                            "latest_reply": "199500.0",
                        }
                    ]
                }
            ],
            # Some Slack responses contain only the root even though history
            # metadata declares replies. Coverage must not be called complete.
            "conversations_replies": [
                {
                    "messages": [
                        {
                            "ts": "198000.0",
                            "user": "U1",
                            "text": "design proposal",
                        }
                    ]
                }
            ],
        }
    )

    result = json.loads(await _toolkit(client).read_slack_conversation())

    assert result["coverage"]["status"] == "partial"
    assert "thread_replies_not_returned" in result["coverage"]["partial_reasons"]
    assert result["coverage"]["thread_replies_returned"] == 0
    assert result["coverage"]["threads_returned"] == 0


@pytest.mark.asyncio
async def test_preserves_bot_root_and_replies_and_deduplicates_pages() -> None:
    client = _FakeClient(
        {
            "auth_test": [
                {
                    "user_id": "U-BOT",
                    "bot_id": "B-BOT",
                    "url": "https://x.slack.com",
                }
            ],
            "conversations_history": [
                {
                    "messages": [
                        {
                            "ts": "198000.0",
                            "user": "U-BOT",
                            "bot_id": "B-BOT",
                            "text": "substantive bot analysis",
                            "reply_count": 3,
                            "latest_reply": "199500.0",
                        }
                    ]
                }
            ],
            "conversations_replies": [
                {
                    "messages": [
                        {
                            "ts": "198000.0",
                            "user": "U-BOT",
                            "bot_id": "B-BOT",
                            "text": "substantive bot analysis",
                        },
                        {
                            "ts": "199000.0",
                            "user": "U2",
                            "text": "useful",
                        },
                        {
                            "ts": "199100.0",
                            "user": "U-BOT",
                            "text": "generated output",
                        },
                    ],
                    "response_metadata": {"next_cursor": "more"},
                },
                {
                    "messages": [
                        {
                            "ts": "199000.0",
                            "user": "U2",
                            "text": "useful duplicate",
                        },
                        {"ts": "199500.0", "user": "U3", "text": "follow-up"},
                    ]
                },
            ],
        }
    )

    result = json.loads(await _toolkit(client).read_slack_conversation())

    assert [message["ts"] for message in result["messages"]] == [
        "198000.0",
        "199000.0",
        "199100.0",
        "199500.0",
    ]
    assert result["messages"][0]["is_own_bot_message"] is True
    assert result["messages"][1]["is_own_bot_message"] is False
    assert result["messages"][2]["is_own_bot_message"] is True
    assert result["messages"][3]["is_own_bot_message"] is False
    assert client.calls["conversations_replies"][1]["cursor"] == "more"


@pytest.mark.asyncio
async def test_retries_429_using_retry_after() -> None:
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    rate_limited = _FakeSlackError(
        _FakeResponse(
            status_code=429,
            data={"ok": False, "error": "ratelimited"},
            headers={"Retry-After": "2"},
        )
    )
    client = _FakeClient(
        {
            "auth_test": [rate_limited, {"user_id": "U-BOT"}],
            "conversations_history": [{"messages": []}],
        }
    )
    toolkit = SlackHistoryToolkit(
        metadata={
            "slack_channel_id": "C1",
            "slack_channel_type": "channel",
            "slack_history_policy": "origin",
        },
        client=client,
        sleep=fake_sleep,
        now=lambda: 200_000.0,
        max_user_lookups=0,
    )

    result = json.loads(await toolkit.read_slack_conversation())

    assert result["ok"] is True
    assert sleeps == [2.0]
    assert len(client.calls["auth_test"]) == 2


@pytest.mark.asyncio
async def test_redacts_secrets_and_reports_partial_size_limit() -> None:
    client = _FakeClient(
        {
            "auth_test": [{"user_id": "U-BOT"}],
            "conversations_history": [
                {
                    "messages": [
                        {
                            "ts": "199999.0",
                            "user": "U1",
                            "text": (
                                "token=xoxb-config-secret "
                                "Authorization: Bearer abcdefghijklmnop "
                                "OPENAI_API_KEY=top-secret "
                                "DATABASE_PASSWORD=hunter2 "
                                "OAUTH_CLIENT_SECRET=client-value "
                                "SIGNING_PRIVATE_KEY=private-value "
                                "ALT_SLACK_TOKEN=xoxc-alternate-secret"
                            ),
                        },
                        {"ts": "199998.0", "user": "U2", "text": "second"},
                    ]
                }
            ],
        }
    )

    result = json.loads(
        await _toolkit(client, max_messages=1).read_slack_conversation()
    )
    serialized = json.dumps(result)

    assert result["coverage"]["status"] == "partial"
    assert "message_limit" in result["coverage"]["partial_reasons"]
    assert result["coverage"]["redacted_count"] >= 7
    assert "xoxb-config-secret" not in serialized
    assert "abcdefghijklmnop" not in serialized
    assert "top-secret" not in serialized
    assert "hunter2" not in serialized
    assert "client-value" not in serialized
    assert "private-value" not in serialized
    assert "xoxc-alternate-secret" not in serialized


@pytest.mark.asyncio
async def test_users_read_failure_falls_back_to_user_ids() -> None:
    missing_scope = _FakeSlackError(
        _FakeResponse(
            status_code=200,
            data={"ok": False, "error": "missing_scope"},
        )
    )
    client = _FakeClient(
        {
            "auth_test": [{"user_id": "U-BOT"}],
            "conversations_history": [
                {"messages": [{"ts": "199999.0", "user": "U1", "text": "hi"}]}
            ],
            "users_info": [missing_scope],
        }
    )
    toolkit = SlackHistoryToolkit(
        metadata={
            "slack_channel_id": "C1",
            "slack_channel_type": "channel",
            "slack_history_policy": "origin",
        },
        client=client,
        now=lambda: 200_000.0,
        max_user_lookups=10,
    )

    result = json.loads(await toolkit.read_slack_conversation())

    assert result["messages"][0]["author_name"] == "U1"
    assert "users_read_scope_unavailable_using_ids" in result["coverage"]["warnings"]


@pytest.mark.asyncio
async def test_every_name_lookup_warning_is_keyed_on_the_field_it_is_about() -> None:
    """One concept, one word, including in the warnings.

    The three codes below said ``user_name`` while the field they describe is
    ``author_name``. Left alone they would have rebuilt the collision the rename
    removed, one remove away: a reader told ``user_name_lookup_limit`` about a
    snapshot with no ``user_name`` in it has to work out that the two are the
    same thing.

    ``users_read_scope_unavailable_using_ids`` deliberately keeps Slack's word.
    ``users:read`` is Slack's scope, and an operator sent looking for
    ``authors:read`` would find nothing.
    """
    client = _FakeClient(
        {
            "auth_test": [{"user_id": "U-BOT"}],
            "conversations_history": [
                {
                    "messages": [
                        {"ts": "199999.0", "user": "U1", "text": "one"},
                        {"ts": "199998.0", "user": "U2", "text": "two"},
                    ]
                }
            ],
            "users_info": [{"user": {"name": "alice"}}],
        }
    )
    toolkit = SlackHistoryToolkit(
        metadata={
            "slack_channel_id": "C1",
            "slack_channel_type": "channel",
            "slack_history_policy": "origin",
        },
        client=client,
        now=lambda: 200_000.0,
        # Two distinct authors, one lookup allowed: the cap is reported rather
        # than silently applied.
        max_user_lookups=1,
    )

    warnings = json.loads(await toolkit.read_slack_conversation())["coverage"]["warnings"]

    assert "author_name_lookup_limit" in warnings
    assert not [word for word in warnings if word.startswith("user_name")]


@pytest.mark.asyncio
async def test_a_name_lookup_stopped_by_the_call_budget_says_so_as_author_name() -> None:
    """The other half of the same vocabulary, on the other reason it stops."""
    client = _FakeClient(
        {
            "auth_test": [{"user_id": "U-BOT"}],
            "conversations_history": [
                {"messages": [{"ts": "199999.0", "user": "U1", "text": "one"}]}
            ],
            "users_info": [],
        }
    )
    toolkit = SlackHistoryToolkit(
        metadata={
            "slack_channel_id": "C1",
            "slack_channel_type": "channel",
            "slack_history_policy": "origin",
        },
        client=client,
        now=lambda: 200_000.0,
        # auth.test and conversations.history spend the budget, so the name
        # lookup never gets a call of its own.
        max_api_calls=2,
        max_user_lookups=10,
    )

    warnings = json.loads(await toolkit.read_slack_conversation())["coverage"]["warnings"]

    assert "author_names_not_resolved_api_limit" in warnings
    assert not [word for word in warnings if word.startswith("user_name")]


def _app_post(ts: str, display_name: str, text: str) -> dict[str, Any]:
    """Build a message shaped like the one Slack sends for an app-posted message.

    An app post holds no ``user``: the account fields are replaced by
    ``bot_id``, the name the app posted under in ``username``, and the
    installed app's own profile.
    """
    return {
        "ts": ts,
        "subtype": "bot_message",
        "bot_id": "B0BUILDBOT",
        "username": display_name,
        "bot_profile": {
            "id": "B0BUILDBOT",
            "name": display_name,
            "app_id": "A0BUILDBOT",
        },
        "text": text,
    }


@pytest.mark.asyncio
async def test_app_display_name_is_never_looked_up_as_a_user_id() -> None:
    """A display name is a label, so users.info must never be asked for one.

    ``users.info`` answers an unknown identifier with ``user_not_found``, which
    costs a lookup out of the configured budget, can crowd real accounts out of
    it, and reports a failure that never had a cause.
    """
    user_not_found = _FakeSlackError(
        _FakeResponse(
            status_code=200,
            data={"ok": False, "error": "user_not_found"},
        )
    )
    client = _FakeClient(
        {
            "auth_test": [{"user_id": "U-BOT", "bot_id": "B-BOT"}],
            "conversations_history": [
                {
                    "messages": [
                        _app_post("199999.0", "Uptime Robot", "deploy finished"),
                        {"ts": "199998.0", "user": "U1", "text": "thanks"},
                    ]
                }
            ],
            "users_info": [
                {"user": {"id": "U1", "profile": {"display_name": "Alice"}}},
                user_not_found,
            ],
        }
    )
    toolkit = SlackHistoryToolkit(
        metadata={
            "slack_channel_id": "C1",
            "slack_channel_type": "channel",
            "slack_history_policy": "origin",
        },
        client=client,
        now=lambda: 200_000.0,
        max_user_lookups=10,
    )

    result = json.loads(await toolkit.read_slack_conversation())

    app_post = result["messages"][1]
    assert app_post["ts"] == "199999.0"
    assert app_post["author_user_id"] == ""
    assert app_post["author_name"] == "Uptime Robot"
    assert result["messages"][0]["author_name"] == "Alice"
    assert [call["user"] for call in client.calls["users_info"]] == ["U1"]
    assert "author_name_lookup_failed" not in result["coverage"]["warnings"]


@pytest.mark.asyncio
async def test_app_display_name_is_redacted_like_message_text() -> None:
    """The name an app posts under is author-supplied text on the same footing."""
    client = _FakeClient(
        {
            "auth_test": [{"user_id": "U-BOT", "bot_id": "B-BOT"}],
            "conversations_history": [
                {
                    "messages": [
                        _app_post("199999.0", "deploy xoxb-leaked-secret", "done")
                    ]
                }
            ],
        }
    )

    result = json.loads(
        await _toolkit(client).read_slack_conversation(include_threads=False)
    )

    assert "xoxb-leaked-secret" not in json.dumps(result)
    assert result["messages"][0]["author_name"] == "deploy [REDACTED_SLACK_TOKEN]"
    assert result["coverage"]["redacted_count"] >= 1
    assert "sensitive_values_redacted" in result["coverage"]["warnings"]


@pytest.mark.asyncio
async def test_without_threads_uses_server_side_time_filter() -> None:
    client = _FakeClient(
        {
            "auth_test": [{"user_id": "U-BOT"}],
            "conversations_history": [{"messages": []}],
        }
    )

    result = json.loads(
        await _toolkit(client).read_slack_conversation(
            hours=2, include_threads=False
        )
    )

    assert result["coverage"]["status"] == "complete"
    assert client.calls["conversations_history"][0]["oldest"] == "192800.000000"
    assert not client.calls["conversations_replies"]


@pytest.mark.asyncio
async def test_messages_and_coverage_carry_human_readable_utc_dates() -> None:
    """Regression: a model reported a message four days after it was sent.

    ts is a Slack identifier that merely looks like a Unix epoch. The payload used
    to expose it raw with no readable equivalent, leaving the model to do the
    conversion itself, which it got wrong by four days. The pair below is five
    days apart, which is what the conversion has to keep.
    """
    client = _FakeClient(
        {
            "auth_test": [{"user_id": "U-BOT"}],
            "conversations_history": [
                {
                    "messages": [
                        {"ts": "1712777678.062809", "user": "U1", "text": "latest"},
                        {"ts": "1712345678.188269", "user": "U1", "text": "earliest"},
                    ]
                }
            ],
        }
    )

    result = json.loads(
        await SlackHistoryToolkit(
            metadata={
                "slack_channel_id": "D0DIRECT01",
                "slack_channel_type": "im",
                "slack_history_policy": "origin",
            },
            client=client,
            now=lambda: 1_712_777_800.0,
            max_user_lookups=0,
        ).read_slack_conversation(all_history=True, include_threads=False)
    )

    by_ts = {message["ts"]: message for message in result["messages"]}
    assert by_ts["1712345678.188269"]["ts_iso_utc"] == "2024-04-05T19:34:38.188269Z"
    assert by_ts["1712777678.062809"]["ts_iso_utc"] == "2024-04-10T19:34:38.062809Z"

    coverage = result["coverage"]
    # The raw identifiers stay untouched: source_ids and the renderer depend on them.
    assert coverage["earliest_message_ts"] == "1712345678.188269"
    assert coverage["earliest_message_iso_utc"].startswith("2024-04-05T19:34:38")
    assert coverage["latest_message_iso_utc"].startswith("2024-04-10T19:34:38")


def _slack_file(
    file_id: str,
    name: str,
    mimetype: str,
    size: int,
    **extra: Any,
) -> dict[str, Any]:
    """Build a file object shaped like the ones Slack embeds in a message.

    Slack repeats the name in the title and serves the bytes from url_private;
    both are reproduced here so the tests exercise what a real payload holds
    rather than a convenient subset of it.
    """
    return {
        "id": file_id,
        "name": name,
        "title": name,
        "mimetype": mimetype,
        "filetype": mimetype.split("/")[-1],
        "size": size,
        "url_private": f"https://files.slack.com/files-pri/T1-{file_id}/{name}",
        "url_private_download": (
            f"https://files.slack.com/files-pri/T1-{file_id}/download/{name}"
        ),
        "permalink": f"https://example.slack.com/files/U1/{file_id}/{name}",
        **extra,
    }


async def _history_with(
    messages: list[dict[str, Any]],
    read_kwargs: dict[str, Any] | None = None,
    **kwargs: Any,
) -> Any:
    """One canned page of history.

    ``kwargs`` build the toolkit; ``read_kwargs`` are the call's own arguments.
    """
    client = _FakeClient(
        {
            "auth_test": [{"user_id": "U-BOT", "url": "https://example.slack.com/"}],
            "conversations_history": [{"messages": messages}],
        }
    )
    return json.loads(
        await _toolkit(client, **kwargs).read_slack_conversation(
            include_threads=False, **(read_kwargs or {})
        )
    )


@pytest.mark.asyncio
async def test_message_with_one_file_carries_the_attachment() -> None:
    result = await _history_with(
        [
            {
                "ts": "199999.0",
                "user": "U1",
                "subtype": "file_share",
                "text": "What is this file? Can you summarize it?",
                "files": [
                    _slack_file(
                        "F0FILE0001", "paper.pdf", "application/pdf", 2_215_244
                    )
                ],
            }
        ]
    )

    message = result["messages"][0]
    assert message["text"] == "What is this file? Can you summarize it?"
    assert message["files"] == [
        {
            "name": "paper.pdf",
            "id": "F0FILE0001",
            "mimetype": "application/pdf",
            "permalink": "https://example.slack.com/files/U1/F0FILE0001/paper.pdf",
            "size_bytes": 2_215_244,
        }
    ]
    # The title repeats the name, so it earns no field of its own.
    assert "title" not in message["files"][0]
    assert "files_truncated" not in message


@pytest.mark.asyncio
async def test_file_only_message_is_not_an_empty_event() -> None:
    """A bare upload holds no comment, and used to normalize to nothing."""
    result = await _history_with(
        [
            {
                "ts": "199998.0",
                "user": "U1",
                "subtype": "file_share",
                "text": "",
                "files": [
                    _slack_file(
                        "F0FILE0002", "screenshot.jpg", "image/jpeg", 1_380_602
                    )
                ],
            }
        ]
    )

    assert result["coverage"]["messages_returned"] == 1
    message = result["messages"][0]
    assert message["text"] == ""
    assert [entry["name"] for entry in message["files"]] == ["screenshot.jpg"]
    assert message["files"][0]["mimetype"] == "image/jpeg"


@pytest.mark.asyncio
async def test_message_with_several_files_keeps_every_attachment() -> None:
    result = await _history_with(
        [
            {
                "ts": "199997.0",
                "user": "U1",
                "subtype": "file_share",
                "text": "the batch",
                "files": [
                    _slack_file("F0FILE0004", "one.jpg", "image/jpeg", 1_147_660),
                    _slack_file("F0FILE0005", "two.jpg", "image/jpeg", 1_637_190),
                    _slack_file("F0FILE0003", "notes.txt", "text/plain", 2_629),
                ],
            }
        ]
    )

    files = result["messages"][0]["files"]
    assert [entry["name"] for entry in files] == ["one.jpg", "two.jpg", "notes.txt"]
    assert [entry["size_bytes"] for entry in files] == [1_147_660, 1_637_190, 2_629]


def _file_dump(count: int) -> list[dict[str, Any]]:
    return [
        {
            "ts": "199996.0",
            "user": "U1",
            "subtype": "file_share",
            "text": "",
            "files": [
                _slack_file(f"F{index:010d}", f"file-{index}.txt", "text/plain", 10)
                for index in range(count)
            ],
        }
    ]


@pytest.mark.asyncio
async def test_attachments_beyond_the_cap_are_reported_not_dropped_silently() -> None:
    default = slack_history._DEFAULT_MAX_FILES_PER_MESSAGE
    result = await _history_with(_file_dump(default + 3))

    message = result["messages"][0]
    assert len(message["files"]) == default
    assert message["files_truncated"] is True
    # The number the flag was measured against, so that the caller can raise it.
    assert result["coverage"]["max_files_per_message"] == default


@pytest.mark.asyncio
async def test_the_attachment_bound_is_the_callers_to_move() -> None:
    """A flag with no argument behind it tells a caller what it cannot act on.

    The bound keeps one file dump from spending the whole answer, which is a
    real thing to defend. It is not a reason to make the rest unreachable, so
    the number has a default, an argument, a mark on the record it applied to
    and a statement of what applied.
    """
    default = slack_history._DEFAULT_MAX_FILES_PER_MESSAGE
    raised = await _history_with(
        _file_dump(default + 3), read_kwargs={"max_files_per_message": default + 3}
    )

    message = raised["messages"][0]
    assert len(message["files"]) == default + 3
    assert "files_truncated" not in message
    assert raised["coverage"]["max_files_per_message"] == default + 3

    lowered = await _history_with(
        _file_dump(default + 3), read_kwargs={"max_files_per_message": 2}
    )

    assert len(lowered["messages"][0]["files"]) == 2
    assert lowered["messages"][0]["files_truncated"] is True
    assert lowered["coverage"]["max_files_per_message"] == 2


@pytest.mark.asyncio
async def test_message_without_files_is_unchanged() -> None:
    result = await _history_with(
        [{"ts": "199995.0", "user": "U1", "text": "just words"}]
    )

    message = result["messages"][0]
    assert message["text"] == "just words"
    assert "files" not in message
    assert "files_truncated" not in message


@pytest.mark.asyncio
async def test_attachment_never_exposes_the_authenticated_download_url() -> None:
    """url_private needs a bearer token, so it would read as a link to nothing."""
    result = await _history_with(
        [
            {
                "ts": "199994.0",
                "user": "U1",
                "subtype": "file_share",
                "text": "",
                "files": [_slack_file("F0FILE0003", "notes.txt", "text/plain", 2_629)],
            }
        ]
    )

    entry = result["messages"][0]["files"][0]
    assert entry["permalink"].startswith("https://example.slack.com/files/")
    assert "url_private" not in entry
    assert "files.slack.com" not in json.dumps(result)


@pytest.mark.asyncio
async def test_bot_uploaded_file_is_still_attributed_to_the_bot() -> None:
    """Slack announces a bot token upload as a file_share by the bot user."""
    result = await _history_with(
        [
            {
                "ts": "199993.0",
                "user": "U-BOT",
                "subtype": "file_share",
                "text": "",
                "files": [_slack_file("F0FILE0004", "report.csv", "text/csv", 4_096)],
            }
        ]
    )

    message = result["messages"][0]
    assert message["is_own_bot_message"] is True
    assert message["files"][0]["name"] == "report.csv"


@pytest.mark.asyncio
async def test_attachment_labels_are_redacted_and_fall_back_when_unnamed() -> None:
    result = await _history_with(
        [
            {
                "ts": "199992.0",
                "user": "U1",
                "subtype": "file_share",
                "text": "",
                "files": [
                    _slack_file(
                        "F0FILE0005",
                        "xoxb-1234567890-abcdefghij.env",
                        "text/plain",
                        128,
                    ),
                    {"id": "F0FILE0002", "name": None, "title": "Untitled dump"},
                    "not a file object",
                ],
            }
        ]
    )

    files = result["messages"][0]["files"]
    assert files[0]["name"] == "[REDACTED_SLACK_TOKEN].env"
    assert "xoxb-" not in json.dumps(result)
    assert result["coverage"]["redacted_count"] >= 1
    # A missing name falls back to the title rather than leaving the entry blank.
    assert files[1]["name"] == "Untitled dump"
    assert "title" not in files[1]
    assert "size_bytes" not in files[1]
    # A malformed entry is skipped instead of failing the scan.
    assert len(files) == 2


@pytest.mark.asyncio
async def test_attachments_are_charged_to_the_total_character_budget() -> None:
    """A window of file shares must not report a size the budget does not bound."""
    result = await _history_with(
        [
            {
                "ts": f"1999{index:02d}.0",
                "user": "U1",
                "subtype": "file_share",
                "text": "",
                "files": [
                    _slack_file(
                        f"F{index:010d}",
                        f"attachment-{index}.pdf",
                        "application/pdf",
                        1,
                    )
                ],
            }
            for index in range(40, 90)
        ],
        max_total_chars=1_000,
    )

    coverage = result["coverage"]
    assert coverage["status"] == "partial"
    assert "total_character_limit" in coverage["partial_reasons"]
    # Text alone is empty everywhere, so only the attachments can have stopped it.
    assert all(message["text"] == "" for message in result["messages"])
    assert coverage["messages_returned"] < 50


@pytest.mark.asyncio
async def test_reactions_are_charged_to_the_total_character_budget() -> None:
    """Reaction names are unbounded workspace text and must be charged too."""
    result = await _history_with(
        [
            {
                "ts": f"1999{index:02d}.0",
                "user": "U1",
                "text": "",
                "reactions": [
                    {
                        "name": f"custom-workspace-emoji-{index}-{slot}",
                        "users": ["U1", "U2"],
                        "count": 2,
                    }
                    for slot in range(20)
                ],
            }
            for index in range(40, 90)
        ],
        max_total_chars=1_000,
    )

    coverage = result["coverage"]
    assert coverage["status"] == "partial"
    assert "total_character_limit" in coverage["partial_reasons"]
    # Text alone is empty everywhere, so only the reactions can have stopped it.
    assert all(message["text"] == "" for message in result["messages"])
    assert coverage["messages_returned"] < 50
    # The records still hold the reactions they were charged for.
    assert len(result["messages"][0]["reactions"]) == 20


@pytest.mark.asyncio
async def test_empty_history_reports_null_iso_dates() -> None:
    client = _FakeClient(
        {
            "auth_test": [{"user_id": "U-BOT"}],
            "conversations_history": [{"messages": []}],
        }
    )

    result = json.loads(
        await _toolkit(client).read_slack_conversation(include_threads=False)
    )

    assert result["coverage"]["earliest_message_iso_utc"] is None
    assert result["coverage"]["latest_message_iso_utc"] is None


class _ChannelClient:
    """A Slack stand-in that honours ``latest``, ``inclusive`` and paging.

    The continuation tests assert that two calls tile the channel rather than
    nesting, which is only meaningful against a server that actually applies the
    bounds the tool sends. A fake that replays canned pages would pass whatever
    the tool did.
    """

    def __init__(
        self,
        roots: list[dict[str, Any]],
        replies: dict[str, list[dict[str, Any]]] | None = None,
        page_size: int = 200,
    ) -> None:
        self.roots = sorted(roots, key=lambda item: float(item["ts"]), reverse=True)
        self.replies = replies or {}
        self.page_size = page_size
        self.history_calls: list[dict[str, Any]] = []

    async def auth_test(self, **kwargs: Any) -> dict[str, Any]:
        return {"user_id": "U-BOT", "bot_id": "B-BOT", "url": "https://x.slack.com"}

    async def conversations_history(self, **kwargs: Any) -> dict[str, Any]:
        self.history_calls.append(dict(kwargs))
        latest = float(kwargs["latest"])
        inclusive = bool(kwargs.get("inclusive"))
        oldest = float(kwargs["oldest"]) if kwargs.get("oldest") else None
        selected = [
            root
            for root in self.roots
            if (
                float(root["ts"]) <= latest if inclusive else float(root["ts"]) < latest
            )
            and (oldest is None or float(root["ts"]) >= oldest)
        ]
        start = int(kwargs.get("cursor") or 0)
        page = selected[start : start + self.page_size]
        next_start = start + self.page_size
        has_more = next_start < len(selected)
        response: dict[str, Any] = {"messages": page, "has_more": has_more}
        if has_more:
            response["response_metadata"] = {"next_cursor": str(next_start)}
        return response

    async def conversations_replies(self, **kwargs: Any) -> dict[str, Any]:
        root_ts = str(kwargs["ts"])
        thread = [{"ts": root_ts}] + list(self.replies.get(root_ts, []))
        return {"messages": thread, "has_more": False}


def _sample_channel() -> _ChannelClient:
    """Ten roots, one of which holds replies newer than every later root.

    That thread is the shape a message-timestamp cursor cannot express: its
    root is among the oldest in the channel while its replies are the newest
    messages in it, so a slice that stops in the middle of the channel by
    message timestamp would strand them.
    """
    roots = [
        {"ts": f"19990{index}.000000", "user": "U1", "text": f"root {index}"}
        for index in range(10)
    ]
    roots[1] = {
        "ts": "199901.000000",
        "user": "U1",
        "text": "root 1",
        "reply_count": 2,
        "latest_reply": "199951.000000",
    }
    replies = {
        "199901.000000": [
            {"ts": "199950.000000", "user": "U2", "text": "late reply a"},
            {"ts": "199951.000000", "user": "U2", "text": "late reply b"},
        ]
    }
    return _ChannelClient(roots, replies)


async def _slice(
    client: _ChannelClient, **kwargs: Any
) -> tuple[dict[str, Any], list[str]]:
    result = json.loads(
        await _toolkit(client).read_slack_conversation(
            all_history=True, **kwargs
        )
    )
    assert result["ok"] is True
    return result, [str(item["ts"]) for item in result["messages"]]


@pytest.mark.asyncio
async def test_call_without_arguments_keeps_todays_behaviour() -> None:
    client = _sample_channel()

    result, returned = await _slice(client)

    assert len(returned) == 12
    assert result["coverage"]["status"] == "complete"
    assert result["coverage"]["max_messages"] == 2_000
    assert result["coverage"]["next_before_ts"] is None
    assert "resume_note" not in result["coverage"]
    assert result["window"]["before_ts"] is None


@pytest.mark.asyncio
async def test_max_messages_returns_fewer_messages_than_an_unbounded_call() -> None:
    unbounded_result, unbounded = await _slice(_sample_channel())
    bounded_result, bounded = await _slice(_sample_channel(), max_messages=4)

    assert unbounded_result["coverage"]["status"] == "complete"
    assert len(unbounded) == 12
    assert len(bounded) == 4
    assert bounded_result["coverage"]["status"] == "partial"
    assert "message_limit" in bounded_result["coverage"]["partial_reasons"]
    assert bounded_result["coverage"]["max_messages"] == 4
    assert set(bounded).issubset(set(unbounded))


@pytest.mark.asyncio
async def test_partial_result_names_an_exclusive_resume_position() -> None:
    result, returned = await _slice(_sample_channel(), max_messages=4)

    coverage = result["coverage"]
    oldest_root = min(str(item["thread_ts"]) for item in result["messages"])
    assert coverage["next_before_ts"] == oldest_root
    assert coverage["next_before_iso_utc"] is not None
    assert "exclusive" in coverage["resume_note"]
    assert coverage["next_before_ts"] in returned


@pytest.mark.asyncio
async def test_resumed_slices_tile_the_channel_without_overlap_or_gap() -> None:
    _, whole = await _slice(_sample_channel())

    walked: list[str] = []
    before_ts: str | None = None
    for _ in range(10):
        result, returned = await _slice(
            _sample_channel(), max_messages=4, before_ts=before_ts
        )
        walked.extend(returned)
        before_ts = result["coverage"]["next_before_ts"]
        if before_ts is None:
            break

    assert before_ts is None
    assert len(walked) == len(set(walked)), "a slice returned a message twice"
    assert sorted(walked) == sorted(whole), "the walk lost or invented a message"
    # The thread whose replies are the newest messages in the channel arrives
    # with its root, in the last slice, rather than with the newest slice.
    assert {"199950.000000", "199951.000000", "199901.000000"}.issubset(set(walked))


@pytest.mark.asyncio
async def test_resuming_from_the_oldest_message_ends_the_walk() -> None:
    result, returned = await _slice(_sample_channel(), before_ts="199900.000000")

    assert returned == []
    assert result["coverage"]["status"] == "complete"
    assert result["coverage"]["next_before_ts"] is None
    assert result["window"]["before_ts"] == "199900.000000"


@pytest.mark.asyncio
async def test_a_thread_is_returned_whole_even_when_it_busts_the_bound() -> None:
    result, returned = await _slice(
        _sample_channel(), max_messages=2, before_ts="199902.000000"
    )

    # The thread is three messages against a bound of two. Splitting it would
    # strand the replies above any later resume position, so it comes back whole.
    assert sorted(returned) == ["199901.000000", "199950.000000", "199951.000000"]
    assert "thread_exceeded_requested_bounds" in result["coverage"]["warnings"]
    assert result["coverage"]["next_before_ts"] == "199901.000000"


@pytest.mark.asyncio
async def test_a_position_reaches_slack_with_the_characters_it_arrived_with(
) -> None:
    """A ts is an identifier, and str(float(ts)) is not the identifier.

    Python prints the shortest string that round-trips as a number, so a ts
    parsed and printed again loses its trailing zeros: 199900.000000 became
    199900.0 and 199901.000100 became 199901.0001. Those name the same instants
    and name no message Slack has ever minted, and they were what the bounds
    on conversations.history and conversations.replies were built from.
    """
    channel = _ChannelClient(
        [{"ts": "199905.000000", "user": "U1", "text": "one"}],
        page_size=200,
    )
    await _read(channel, before_ts="199902.000000", include_threads=False)

    assert channel.history_calls[0]["latest"] == "199902.000000"

    thread = _thread_client()
    await _read(thread, ts="199900.000000", before_ts="199903.000000")

    assert thread.calls["conversations_replies"][0]["latest"] == "199903.000000"


@pytest.mark.asyncio
async def test_an_older_edge_taken_from_a_result_reaches_slack_unchanged() -> None:
    """The same rule for after_ts, which Slack is given as ``oldest``."""
    client = _ChannelClient(
        [
            {"ts": "199905.000000", "user": "U1", "text": "new"},
            {"ts": "199900.000000", "user": "U1", "text": "old"},
        ],
        page_size=200,
    )
    result = await _read(client, after_ts="199901.000000", include_threads=False)

    assert client.history_calls[0]["oldest"] == "199901.000000"
    assert result["window"]["cutoff_ts"] == "199901.000000"


@pytest.mark.asyncio
async def test_every_position_the_result_states_is_one_it_accepts_back() -> None:
    """The round trip the card tells the caller to make has to close.

    window.cutoff_ts and window.snapshot_ts were printed from floats, so they
    came out in a shape the tool's own argument check refuses -- 199905.0 where
    a ts has six decimal places. A field named ts, in a result whose card says
    to hand positions back, has to be a position this tool takes.
    """
    result = await _read(_sample_channel(), hours=2)

    stated = [
        result["window"]["cutoff_ts"],
        result["window"]["snapshot_ts"],
        result["coverage"]["earliest_message_ts"],
        result["coverage"]["latest_message_ts"],
        result["coverage"]["next_before_ts"],
    ]
    assert any(value is not None for value in stated)
    for value in stated:
        if value is None:
            continue
        assert slack_history._SLACK_TS_SHAPE.match(value), value
        assert slack_history._slack_message_ts(value) is not None, value


@pytest.mark.asyncio
async def test_before_ts_that_is_not_a_slack_timestamp_is_refused() -> None:
    toolkit = _toolkit(_sample_channel())

    result = json.loads(
        await toolkit.read_slack_conversation(before_ts="yesterday")
    )

    assert result["ok"] is False
    assert result["error"] == "before_ts_must_be_a_slack_timestamp"
    assert result["messages"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("argument", "code"),
    [
        ("before_ts", "before_ts_must_be_a_slack_timestamp"),
        ("after_ts", "after_ts_must_be_a_slack_timestamp"),
        ("ts", "ts_must_be_a_slack_timestamp"),
    ],
)
async def test_a_round_number_is_not_a_slack_timestamp(
    argument: str, code: str
) -> None:
    """The value a model invented, refused where it is written.

    ``1788515000.0`` parses as a float and names a plausible instant, which was
    the whole of the old check, and it matches no message Slack has ever
    minted. Each of the three arguments already said the right thing when it
    refused; none of them ever reached the refusal for this.
    """
    toolkit = _toolkit(_sample_channel())

    result = json.loads(
        await toolkit.read_slack_conversation(**{argument: "1788515000.0"})
    )

    assert result["ok"] is False
    assert result["error"] == code
    assert result["messages"] == []
    assert "six decimal places" in result["detail"]


@pytest.mark.asyncio
async def test_a_ts_in_the_shape_slack_mints_is_still_accepted() -> None:
    """The shape check refuses inventions and nothing else."""
    result = await _read(_sample_channel(), before_ts="199905.000000")

    assert result["ok"] is True
    assert [item["ts"] for item in result["messages"]][:1] == ["199900.000000"]


@pytest.mark.asyncio
async def test_coverage_hands_back_positions_the_tool_will_take_again() -> None:
    """Every ts a result states has to survive being passed back.

    ``latest_message_ts`` came off a parsed float, so a message at
    199001.000100 was reported as 199001.0001 -- the same instant, and an
    identifier of nothing. It measured the right position, which is why the
    loss stayed invisible until the shape of a ts became a thing the tool
    checks.
    """
    client = _ChannelClient(
        [
            {"ts": "199001.000100", "user": "U1", "text": "first"},
            {"ts": "199002.000000", "user": "U1", "text": "second"},
        ]
    )

    whole = await _read(client, all_history=True)
    coverage = whole["coverage"]

    assert coverage["earliest_message_ts"] == "199001.000100"
    assert coverage["latest_message_ts"] == "199002.000000"
    since = await _read(
        _ChannelClient(list(client.roots)),
        all_history=True,
        after_ts=str(coverage["latest_message_ts"]),
    )
    assert since["ok"] is True
    assert since["messages"] == []


@pytest.mark.asyncio
async def test_the_documented_walk_only_passes_back_what_it_was_given() -> None:
    """The walk end to end, against the tightened check.

    Every position the loop feeds back is one the previous result stated, so a
    check on the shape of a ts must not be able to stop it.
    """
    walked: list[str] = []
    before_ts: str | None = None
    for _ in range(10):
        result = await _read(
            _sample_channel(), all_history=True, max_messages=4, before_ts=before_ts
        )
        assert result["ok"] is True
        walked.extend(str(item["ts"]) for item in result["messages"])
        before_ts = result["coverage"]["next_before_ts"]
        if before_ts is None:
            break

    assert before_ts is None
    assert len(walked) == 12
    assert len(walked) == len(set(walked))


@pytest.mark.asyncio
async def test_a_thread_slice_resumes_from_a_position_of_the_right_shape() -> None:
    """The thread walk hands back a reply ts, which is checked the same way."""
    client = _LongThreadClient(20)
    first = await _read(client, ts=_LongThreadClient.ROOT_TS, max_messages=2)
    resume = first["coverage"]["next_before_ts"]

    assert resume is not None
    second = await _read(
        _LongThreadClient(20), ts=_LongThreadClient.ROOT_TS, before_ts=resume
    )

    assert second["ok"] is True


@pytest.mark.asyncio
async def test_a_before_ts_older_than_the_default_window_is_not_called_complete(
) -> None:
    """The call a model actually made, and the answer it was given.

    ``before_ts`` months in the past with no ``hours`` beside it intersects the
    default day and leaves nothing: no message is both older than the position
    and newer than yesterday. The read came back with zero messages, status
    complete, and an empty list of reasons, which reads exactly like a
    conversation nobody has posted in.
    """
    result = await _read(_sample_channel(), before_ts="100000.000000")

    assert result["messages"] == []
    assert result["coverage"]["status"] == "partial"
    assert "window_bounds_exclude_each_other" in result["coverage"]["partial_reasons"]
    assert (
        "default_window_applied_beside_before_ts" in result["coverage"]["warnings"]
    )
    # The note names the default, because the default is the half of the
    # contradiction the caller never wrote and cannot see in its own arguments.
    note = result["coverage"]["window_note"]
    assert "default 24 hours" in note
    assert "all_history" in note


@pytest.mark.asyncio
async def test_an_empty_conversation_reads_differently_from_a_narrowed_window(
) -> None:
    """The distinction the fix exists to make.

    Both calls return nothing. One of them was asked a question with no range
    in it and one of them was asked about a channel with nothing in it, and a
    caller that cannot tell them apart will report the second when it met the
    first.
    """
    empty_channel = await _read(_ChannelClient([]))
    narrowed = await _read(_sample_channel(), before_ts="100000.000000")

    assert empty_channel["messages"] == []
    assert empty_channel["coverage"]["status"] == "complete"
    assert empty_channel["coverage"]["partial_reasons"] == []
    assert "window_note" not in empty_channel["coverage"]
    assert narrowed["coverage"]["status"] == "partial"
    assert "window_note" in narrowed["coverage"]


@pytest.mark.asyncio
async def test_a_before_ts_inside_the_default_window_reports_no_contradiction(
) -> None:
    """An empty answer from a coherent window stays an ordinary empty answer.

    The channel is empty and the position is inside the day, so there is
    nothing to report beyond the default having applied. Reporting a
    contradiction here would make the reason mean nothing.
    """
    result = await _read(_ChannelClient([]), before_ts="199900.000000")

    assert result["messages"] == []
    assert result["coverage"]["status"] == "complete"
    assert "window_bounds_exclude_each_other" not in (
        result["coverage"]["partial_reasons"]
    )
    assert "window_note" not in result["coverage"]


@pytest.mark.asyncio
async def test_the_documented_walk_still_tiles_a_default_window_read() -> None:
    """The loop the card tells a caller to run, with no ``hours`` anywhere.

    Every slice after the first carries ``before_ts`` and nothing else, which
    is the same argument shape as the broken call above. The walk has to keep
    working: refusing that shape, or dropping the default window when it
    arrives, would either error the loop at its tail or silently widen it to
    all of history.
    """
    whole = await _read(_sample_channel())

    walked: list[str] = []
    statuses: list[str] = []
    before_ts: str | None = None
    for _ in range(10):
        result = await _read(_sample_channel(), max_messages=4, before_ts=before_ts)
        walked.extend(str(item["ts"]) for item in result["messages"])
        statuses.append(result["coverage"]["status"])
        assert "window_bounds_exclude_each_other" not in (
            result["coverage"]["partial_reasons"]
        )
        before_ts = result["coverage"]["next_before_ts"]
        if before_ts is None:
            break

    assert before_ts is None
    assert len(walked) == len(set(walked)), "a slice returned a message twice"
    assert sorted(walked) == sorted(str(item["ts"]) for item in whole["messages"])
    assert statuses[-1] == "complete"


@pytest.mark.asyncio
async def test_a_stated_hours_beside_before_ts_is_honoured_and_not_reported(
) -> None:
    """A caller that wrote the window gets the window it wrote.

    Nothing about the pair is narrowed behind the caller's back, so neither the
    warning nor the reason belongs on it.
    """
    result = await _read(_sample_channel(), hours=48, before_ts="199905.000000")

    assert {item["ts"] for item in result["messages"]} == {
        "199900.000000",
        "199901.000000",
        "199902.000000",
        "199903.000000",
        "199904.000000",
        "199950.000000",
        "199951.000000",
    }
    assert result["window"]["mode"] == "hours"
    assert result["window"]["requested_hours"] == 48
    assert result["coverage"]["warnings"] == []
    assert result["coverage"]["status"] == "complete"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "older_edge",
    [
        {"after_ts": "199905.000000"},
        {"after_iso_utc": "1970-01-03T07:31:45Z"},
    ],
    ids=["after_ts", "after_iso_utc"],
)
async def test_a_stated_older_edge_above_before_ts_is_reported_the_same_way(
    older_edge: dict[str, str],
) -> None:
    """The other bound, treated as the default one is.

    ``after_ts`` and ``after_iso_utc`` supply the older edge themselves, so no
    default can contradict them, but either of them above ``before_ts`` empties
    the window exactly as the default does. A caller reading the result cannot
    see the arithmetic any more easily for having written both numbers.
    """
    result = await _read(
        _sample_channel(), before_ts="199901.000000", **older_edge
    )

    assert result["messages"] == []
    assert result["coverage"]["status"] == "partial"
    assert "window_bounds_exclude_each_other" in result["coverage"]["partial_reasons"]
    # No default applied, so nothing is named as one; the note says to move a
    # bound rather than to state a window that is already stated.
    assert result["coverage"]["warnings"] == []
    assert "default 24 hours" not in result["coverage"]["window_note"]
    assert "Move whichever" in result["coverage"]["window_note"]


@pytest.mark.asyncio
async def test_a_thread_read_carries_no_window_for_before_ts_to_contradict(
) -> None:
    """``ts`` does not share the flaw, and is left as it is.

    A thread named with no ``hours`` has no older edge at all, so a resume
    position of any age is coherent. The check has nothing to fire on here, and
    a check that fired anyway would break resuming a thread older than a day.
    """
    result = await _read(
        _old_thread(), ts="100000.000000", before_ts="100001.000000"
    )

    assert [item["ts"] for item in result["messages"]] == ["100000.000000"]
    assert result["window"]["mode"] == "thread"
    assert "window_bounds_exclude_each_other" not in (
        result["coverage"]["partial_reasons"]
    )


@pytest.mark.asyncio
async def test_bounds_that_exclude_each_other_are_not_reported_when_something_came_back(
) -> None:
    """Why the reason is charged to the empty answer and not to the bounds.

    A root older than both bounds still comes back when replies of its own are
    in window, threads being taken whole, so bounds that leave no range are not
    by themselves a promise that nothing will be returned. The caller here has
    messages in front of it and nothing happened behind its back.
    """
    result = await _read(
        _sample_channel(), before_ts="199902.000000", after_ts="199905.000000"
    )

    assert {item["ts"] for item in result["messages"]} == {
        "199901.000000",
        "199950.000000",
        "199951.000000",
    }
    assert "window_bounds_exclude_each_other" not in (
        result["coverage"]["partial_reasons"]
    )
    assert "window_note" not in result["coverage"]


@pytest.mark.asyncio
async def test_the_card_says_what_an_excluded_window_means() -> None:
    """A reason a caller meets for the first time in a result is a reason it
    has to be able to read there and then."""
    card = _toolkit(_sample_channel()).get_tools()[0]._card

    assert "window_bounds_exclude_each_other" in card.description
    assert "coverage.window_note" in card.description
    before_ts = card.input_params["properties"]["before_ts"]
    assert "narrows the window and does not replace it" in before_ts["description"]


@pytest.mark.asyncio
async def test_the_card_names_the_bound_behind_each_truncation_mark() -> None:
    """A mark is only actionable where the caller can read what to do about it.

    files_truncated and reactors_truncated were both set silently against
    numbers the card never mentioned, so a caller could learn that a list had
    been cut and had nowhere to go with that.
    """
    card = _toolkit(_sample_channel()).get_tools()[0]._card
    properties = card.input_params["properties"]

    assert "files_truncated" in properties["max_files_per_message"]["description"]
    assert (
        "reactors_truncated"
        in properties["max_reactors_per_reaction"]["description"]
    )
    assert "coverage.max_files_per_message" in card.description
    assert "coverage.max_reactors_per_reaction" in card.description
    # The cap that was removed is claimed nowhere.
    assert "reactions_truncated" not in card.description


@pytest.mark.asyncio
async def test_configured_bound_caps_a_call_that_asks_for_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        config_module,
        "get_config",
        lambda: {
            "channels": {
                "slack": {
                    "bot_token": "xoxb-config-secret",
                    "history_max_messages": 3,
                }
            }
        },
    )

    result, returned = await _slice(_sample_channel())

    assert len(returned) == 3
    assert result["coverage"]["max_messages"] == 3
    assert result["coverage"]["status"] == "partial"
    assert result["coverage"]["next_before_ts"] is not None


@pytest.mark.asyncio
async def test_a_caller_cannot_ask_for_more_than_the_deployment_allows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        config_module,
        "get_config",
        lambda: {
            "channels": {
                "slack": {
                    "bot_token": "xoxb-config-secret",
                    "history_max_messages": 3,
                }
            }
        },
    )

    result, returned = await _slice(_sample_channel(), max_messages=500)

    assert len(returned) == 3
    assert result["coverage"]["max_messages"] == 3


# ---------------------------------------------------------------------------
# The redaction pass, which two tools run and only one defines.
#
# ``slack_search`` already imported the regexes by name because they are
# security-critical and a fix to one copy would not reach a second. The pass
# driving them was copied rather than imported, which left exactly the gap the
# import was reasoned about to close. These tests are on the shared function and
# on the one property the two callers do not share.
# ---------------------------------------------------------------------------


def test_both_slack_tools_run_the_same_redaction_pass() -> None:
    """One function, named from both, rather than two that happen to agree."""
    from jiuwenswarm.agents.harness.common.tools import slack_search

    assert slack_search.redact_credentials is slack_history.redact_credentials


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("run xoxb-11-22-abcdef with it", "run [REDACTED_SLACK_TOKEN] with it"),
        ("Bearer abcdefghijkl", "Bearer [REDACTED]"),
        ("api_key=hunter2hunter2", "api_key=[REDACTED]"),
    ],
)
def test_every_credential_shape_is_masked(value: str, expected: str) -> None:
    text, redacted, truncated = slack_history.redact_credentials(value)

    assert text == expected
    assert redacted == 1
    assert truncated is False


def test_the_known_token_is_masked_by_literal_match_before_the_patterns() -> None:
    """The one credential known exactly rather than by shape.

    Masked first, so a token no pattern happens to match is gone anyway.
    """
    text, redacted, _ = slack_history.redact_credentials(
        "token is s3cr3t-not-a-slack-shape", bot_token="s3cr3t-not-a-slack-shape"
    )

    assert text == "token is [REDACTED]"
    assert redacted == 1


def test_a_cap_cuts_and_says_it_cut() -> None:
    text, _redacted, truncated = slack_history.redact_credentials("y" * 40, cap=10)

    assert truncated is True
    assert len(text) == 10
    assert text.endswith("…")


def test_no_cap_means_uncapped_rather_than_a_cap_of_zero() -> None:
    """``None`` is what the search tool passes for a permalink.

    A locator that has been shortened is not a locator, so this is the one
    difference between the two callers and the whole reason the cap is a
    parameter rather than a constant.
    """
    link = "https://example.slack.com/archives/C1/p" + "9" * 400

    text, _redacted, truncated = slack_history.redact_credentials(link)

    assert text == link
    assert truncated is False


# --------------------------------------------------------------------------
# What an app posted, when the app did not post it in ``text``
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_message_written_in_blocks_is_not_read_as_nearly_empty() -> None:
    """``text`` is the notification fallback; the message is in ``blocks``.

    An app posting through Block Kit puts the body in ``blocks`` and leaves
    ``text`` as one line for the mobile notification. A record built from
    ``text`` alone reports the whole message as that one line, which reads as
    though nothing was said rather than as though the reader cannot see it.
    """
    result = await _history_with(
        [
            {
                "ts": "199999.0",
                "user": "U1",
                "text": "New alert",
                "blocks": [
                    {
                        "type": "section",
                        "block_id": "b1",
                        "text": {"type": "mrkdwn", "text": "Build 4821 failed"},
                    },
                    {
                        "type": "section",
                        "fields": [
                            {"type": "mrkdwn", "text": "Branch: develop"},
                            {"type": "mrkdwn", "text": "Owner: release"},
                        ],
                    },
                ],
            }
        ]
    )

    message = result["messages"][0]
    assert message["text"] == "New alert"
    assert message["block_text"] == (
        "Build 4821 failed\nBranch: develop\nOwner: release"
    )


@pytest.mark.asyncio
async def test_block_kit_vocabulary_is_not_filed_as_something_a_person_wrote() -> None:
    """The payload holds Slack's rendering words beside the author's.

    ``mrkdwn``, ``section`` and a block id are in the same mapping as the text,
    and a walk that took every string would hand a reader ids to quote back.
    """
    result = await _history_with(
        [
            {
                "ts": "199999.0",
                "user": "U1",
                "text": "",
                "blocks": [
                    {
                        "type": "actions",
                        "block_id": "B0DEADBEEF",
                        "elements": [
                            {
                                "type": "button",
                                "action_id": "approve",
                                "value": "run-77",
                                "text": {"type": "plain_text", "text": "Approve"},
                            }
                        ],
                    }
                ],
            }
        ]
    )

    block_text = result["messages"][0]["block_text"]
    assert block_text == "Approve"
    for noise in ("actions", "B0DEADBEEF", "approve", "run-77", "plain_text"):
        assert noise not in block_text


@pytest.mark.asyncio
async def test_an_attachment_body_is_kept_and_said_only_once() -> None:
    """A legacy attachment repeats itself, and its ``fallback`` is its ``text``."""
    result = await _history_with(
        [
            {
                "ts": "199999.0",
                "user": "U1",
                "text": "",
                "attachments": [
                    {
                        "color": "#36a64f",
                        "pretext": "Deploy report",
                        "title": "v30 is live",
                        "text": "Rolled out to every region.",
                        "fallback": "Rolled out to every region.",
                    }
                ],
            }
        ]
    )

    message = result["messages"][0]
    assert message["attachment_text"] == (
        "Deploy report\nv30 is live\nRolled out to every region."
    )
    assert "block_text" not in message


@pytest.mark.asyncio
async def test_message_metadata_is_asked_for_and_kept() -> None:
    """Slack withholds message metadata unless the call asks for it.

    Absent rather than empty, so without the argument nothing downstream can
    tell a message that carried none from one whose metadata was never sent.
    The payload's keys belong to the posting app rather than to Slack, so
    every string in it is read.
    """
    client = _FakeClient(
        {
            "auth_test": [{"user_id": "U-BOT", "url": "https://example.slack.com/"}],
            "conversations_history": [
                {
                    "messages": [
                        {
                            "ts": "199999.0",
                            "user": "U1",
                            "text": "",
                            "reply_count": 1,
                            "latest_reply": "199999.5",
                            "metadata": {
                                "event_type": "incident_opened",
                                "event_payload": {
                                    "severity": "major",
                                    "summary": "Gateway is refusing writes",
                                },
                            },
                        }
                    ]
                }
            ],
            "conversations_replies": [
                {
                    "messages": [
                        {"ts": "199999.0"},
                        {"ts": "199999.5", "user": "U2", "text": "on it"},
                    ]
                }
            ],
        }
    )

    result = json.loads(await _toolkit(client).read_slack_conversation())

    message = result["messages"][0]
    assert message["metadata_text"] == (
        "incident_opened\nmajor\nGateway is refusing writes"
    )
    assert client.calls["conversations_history"][0]["include_all_metadata"] is True
    assert client.calls["conversations_replies"][0]["include_all_metadata"] is True


# ── reading one thread ───────────────────────────────────────────────────────


class _ThreadClient:
    """``conversations.replies`` answering the way the live endpoint answers.

    A root's ts returns the whole thread. A reply's ts returns that reply on
    its own, whatever the endpoint documents, which is why a thread has to be
    read again from the ``thread_ts`` the answer states. A ts belonging to
    neither is declined as ``thread_not_found``.

    ``expands_a_reply`` models the documented answer instead, where any ts in
    the thread returns all of it. Both are worth covering: the second read
    exists for the first case and must not happen in the second.
    """

    def __init__(
        self,
        root: dict[str, Any],
        replies: list[dict[str, Any]],
        *,
        info: dict[str, Any] | None = None,
        page_size: int = 200,
        expands_a_reply: bool = False,
    ) -> None:
        self.root = root
        self.replies = replies
        self.info = info
        self.page_size = page_size
        self.expands_a_reply = expands_a_reply
        self.calls: dict[str, list[dict[str, Any]]] = defaultdict(list)

    def _thread(self) -> list[dict[str, Any]]:
        root_ts = str(self.root["ts"])
        parent = dict(self.root)
        if self.replies:
            # Slack states thread_ts on a message that is in a thread and on no
            # other, so a message nothing hangs from is its own root and says
            # nothing about a thread.
            parent.setdefault("thread_ts", root_ts)
        return [parent] + [
            dict(reply, thread_ts=root_ts) for reply in self.replies
        ]

    async def auth_test(self, **kwargs: Any) -> dict[str, Any]:
        self.calls["auth_test"].append(kwargs)
        return {"user_id": "U-BOT", "bot_id": "B-BOT", "url": "https://x.slack.com"}

    async def conversations_history(self, **kwargs: Any) -> dict[str, Any]:
        self.calls["conversations_history"].append(kwargs)
        return {"messages": [], "has_more": False}

    async def conversations_replies(self, **kwargs: Any) -> dict[str, Any]:
        self.calls["conversations_replies"].append(dict(kwargs))
        thread = self._thread()
        asked = str(kwargs.get("ts") or "")
        if asked != str(self.root["ts"]) and not self.expands_a_reply:
            named = [item for item in thread if str(item["ts"]) == asked]
            if not named:
                raise _FakeSlackError(
                    _FakeResponse(
                        status_code=200,
                        data={"ok": False, "error": "thread_not_found"},
                    )
                )
            return {"messages": named, "has_more": False}
        start = int(kwargs.get("cursor") or 0)
        page = thread[start : start + self.page_size]
        has_more = start + self.page_size < len(thread)
        response: dict[str, Any] = {"messages": page, "has_more": has_more}
        if has_more:
            response["response_metadata"] = {
                "next_cursor": str(start + self.page_size)
            }
        return response

    async def conversations_info(self, **kwargs: Any) -> dict[str, Any]:
        self.calls["conversations_info"].append(kwargs)
        if self.info is None:
            raise _FakeSlackError(
                _FakeResponse(status_code=200, data={"ok": False, "error": "x"})
            )
        return {"channel": self.info}


def _thread_client(**kwargs: Any) -> _ThreadClient:
    """One root at 199900 with three replies, the newest at 199903."""
    return _ThreadClient(
        {"ts": "199900.000000", "user": "U1", "text": "root", "reply_count": 3},
        [
            {"ts": "199901.000000", "user": "U2", "text": "reply one"},
            {"ts": "199902.000000", "user": "U2", "text": "reply two"},
            {"ts": "199903.000000", "user": "U3", "text": "reply three"},
        ],
        **kwargs,
    )


async def _read(client: Any, **kwargs: Any) -> dict[str, Any]:
    return json.loads(await _toolkit(client).read_slack_conversation(**kwargs))


@pytest.mark.asyncio
async def test_a_named_thread_is_entered_directly_and_no_channel_is_walked() -> None:
    """``ts`` names a position inside an audience the gate has settled.

    Reaching a thread by walking the channel to its root costs the whole scan
    to answer a question about four messages. A root old enough to have fallen
    out of the window would never be reached at all.
    """
    client = _thread_client()

    result = await _read(client, ts="199900.000000")

    assert result["ok"] is True
    assert [item["ts"] for item in result["messages"]] == [
        "199900.000000",
        "199901.000000",
        "199902.000000",
        "199903.000000",
    ]
    assert "conversations_history" not in client.calls
    assert result["window"]["mode"] == "thread"
    assert result["window"]["ts"] == "199900.000000"
    assert result["window"]["requested_hours"] is None


@pytest.mark.asyncio
async def test_a_reply_ts_names_the_thread_it_is_in_rather_than_a_root_of_its_own(
) -> None:
    """The trap the argument is named ``ts`` for.

    A ts copied from a message link is the message somebody cares about and is
    a reply far more often than it is a thread's first message.
    ``conversations.replies`` answers a reply's ts with that reply alone, so a
    tool that stops there returns one message under a request for a thread, and
    files it as a root: ``is_thread_reply`` false, ``thread_ts`` pointing at
    itself, and a permalink with no thread fragment, which opens the channel
    rather than the conversation the caller asked about.
    """
    client = _thread_client()

    result = await _read(client, ts="199902.000000")

    by_ts = {item["ts"]: item for item in result["messages"]}
    assert sorted(by_ts) == [
        "199900.000000",
        "199901.000000",
        "199902.000000",
        "199903.000000",
    ]
    assert by_ts["199900.000000"]["is_thread_reply"] is False
    assert result["coverage"]["root_messages_returned"] == 1
    reply = by_ts["199902.000000"]
    assert reply["is_thread_reply"] is True
    assert reply["thread_ts"] == "199900.000000"
    assert "thread_ts=199900.000000" in reply["permalink"]
    # The argument itself is what Slack was asked for first, unaltered, and the
    # root only once the answer named it.
    assert [call["ts"] for call in client.calls["conversations_replies"]] == [
        "199902.000000",
        "199900.000000",
    ]
    assert (
        "ts_named_a_reply_read_from_its_root" in result["coverage"]["warnings"]
    )
    assert result["window"]["ts"] == "199902.000000"


@pytest.mark.asyncio
async def test_a_root_ts_costs_the_one_call_it_has_always_cost() -> None:
    """The second call is spent only where the first answer was incomplete.

    Reading every thread twice to cover the reply case would double what a
    thread read spends of a budget the deployment sets.
    """
    client = _thread_client()

    result = await _read(client, ts="199900.000000")

    assert len(result["messages"]) == 4
    assert len(client.calls["conversations_replies"]) == 1
    assert result["coverage"]["thread_pages"] == 1
    assert (
        "ts_named_a_reply_read_from_its_root"
        not in result["coverage"]["warnings"]
    )


@pytest.mark.asyncio
async def test_a_reply_ts_slack_answers_in_full_is_not_read_a_second_time() -> None:
    """The documented answer costs nothing extra either.

    The second read is decided on what came back rather than on what was
    passed, so a workspace whose replies endpoint expands a reply's ts pays for
    one call and the whole thread still comes back exactly once.
    """
    client = _thread_client(expands_a_reply=True)

    result = await _read(client, ts="199902.000000")

    assert [item["ts"] for item in result["messages"]] == [
        "199900.000000",
        "199901.000000",
        "199902.000000",
        "199903.000000",
    ]
    assert len(client.calls["conversations_replies"]) == 1
    assert (
        "ts_named_a_reply_read_from_its_root"
        not in result["coverage"]["warnings"]
    )


@pytest.mark.asyncio
async def test_a_ts_naming_a_message_in_no_thread_returns_that_message() -> None:
    """One message is the whole answer, not a failed thread read.

    A message nothing hangs from states no ``thread_ts``, so it is its own
    root, there is no second ts to follow, and no second call is made.
    """
    client = _ThreadClient({"ts": "199900.000000", "user": "U1", "text": "alone"}, [])

    result = await _read(client, ts="199900.000000")

    assert result["ok"] is True
    assert [item["ts"] for item in result["messages"]] == ["199900.000000"]
    assert result["messages"][0]["is_thread_reply"] is False
    assert result["coverage"]["root_messages_returned"] == 1
    assert result["coverage"]["thread_replies_returned"] == 0
    assert len(client.calls["conversations_replies"]) == 1
    assert result["window"]["mode"] == "thread"


@pytest.mark.asyncio
async def test_a_ts_slack_will_not_resolve_says_which_call_it_declined() -> None:
    """``thread_not_found`` is the gate holding, not the read breaking.

    A ts belonging to another conversation selects nothing in the one the gate
    settled, and that is the answer a caller acts on: the code is reported as
    Slack sent it, beside the call that earned it.
    """
    client = _thread_client()

    result = await _read(client, ts="100000.000000")

    assert result["ok"] is False
    assert result["error"] == "thread_not_found"
    assert result.get("detail") == "Slack refused conversations.replies"
    assert result["messages"] == []


def _old_thread() -> _ThreadClient:
    """A thread whose every message is older than the default day.

    The toolkit's clock reads 200000, so a 24-hour window starts at 113600 and
    a thread at 100000 falls wholly outside it.
    """
    return _ThreadClient(
        {"ts": "100000.000000", "user": "U1", "text": "old root", "reply_count": 1},
        [{"ts": "100001.000000", "user": "U2", "text": "old reply"}],
    )


@pytest.mark.asyncio
async def test_naming_a_thread_does_not_impose_a_day_long_window() -> None:
    """A thread of any age is readable by its ts and nothing else.

    ``hours`` had to stop defaulting to 24 for this to be expressible: with a
    number filled in, a call that named a thread exactly said it wanted the
    last day of it, and a thread older than that came back empty with nothing
    but ``mode: hours`` to say why.

    The call is written the way a model writes it, with ``hours`` absent rather
    than passed as ``None``. A probe that spells the omission out is testing an
    argument no model sends.
    """
    result = await _read(_old_thread(), ts="100000.000000")

    assert [item["ts"] for item in result["messages"]] == [
        "100000.000000",
        "100001.000000",
    ]
    assert result["window"]["mode"] == "thread"
    assert result["window"]["cutoff_ts"] is None
    assert result["window"]["cutoff_iso_utc"] is None
    assert result["window"]["requested_hours"] is None


@pytest.mark.asyncio
async def test_a_stated_twenty_four_hours_is_not_what_omitting_hours_means(
) -> None:
    """Two requests, and the difference is the whole of the thread mode.

    A number written beside a ts is a caller asking for the last day of a
    thread, which is honoured. Writing nothing asks for the thread, which is
    why the card advertises no default a model could helpfully fill in.
    """
    omitted = await _read(_old_thread(), ts="100000.000000")
    stated = await _read(_old_thread(), ts="100000.000000", hours=24)

    assert omitted["window"]["mode"] == "thread"
    assert len(omitted["messages"]) == 2
    assert stated["window"]["mode"] == "hours"
    assert stated["window"]["requested_hours"] == 24
    # The root still comes back, marked as the context it is, so the answer
    # says what it read rather than reading as an empty thread.
    assert [item["ts"] for item in stated["messages"]] == ["100000.000000"]
    assert stated["messages"][0]["outside_window_context"] is True


@pytest.mark.asyncio
async def test_a_stated_number_windows_a_thread_read_as_it_windows_any_other(
) -> None:
    """A stated number is honoured, thread or no thread."""
    narrowed = await _read(_old_thread(), ts="100000.000000", hours=1)

    assert [item["ts"] for item in narrowed["messages"]] == ["100000.000000"]
    assert narrowed["messages"][0]["outside_window_context"] is True
    assert narrowed["window"]["mode"] == "hours"


@pytest.mark.asyncio
async def test_the_card_advertises_no_default_a_thread_read_would_ignore(
) -> None:
    """What a model is told about ``hours`` decides what it sends.

    Declaring 24 as the default says that writing the number changes nothing,
    and writing it costs the caller the thread mode omission would have given
    them. The number a conversation read applies is still stated in words,
    where it belongs.
    """
    card = _toolkit(_thread_client()).get_tools()[0]._card
    hours = card.input_params["properties"]["hours"]

    assert "default" not in hours
    assert "the last 24 hours" in hours["description"]
    assert "Leave this out beside ts" in hours["description"]


@pytest.mark.asyncio
async def test_include_threads_is_reported_as_inert_beside_a_thread_read() -> None:
    """Reported, because nobody sets ``include_threads`` on purpose.

    It defaults to true, so refusing the pair would break every unmodified
    caller's first thread read on an argument they never chose.
    """
    for include_threads in (True, False):
        result = await _read(
            _thread_client(), ts="199900.000000", include_threads=include_threads
        )

        assert result["ok"] is True
        assert len(result["messages"]) == 4
        assert (
            "include_threads_not_used_on_a_thread_read"
            in result["coverage"]["warnings"]
        )


@pytest.mark.asyncio
async def test_before_ts_pages_a_thread_by_its_replies_and_keeps_the_root() -> None:
    """Inside one thread the resume position is a reply.

    Every message hangs from the same root, so a root offered as
    ``next_before_ts`` would ask for the same slice forever. The root still
    comes back each time, marked as context when the window excludes it, which
    is how the channel walk already treats an old root with in-window replies.
    """
    walked: list[str] = []
    before_ts: str | None = None
    for _ in range(5):
        result = await _read(
            _thread_client(), ts="199900.000000", max_messages=2, before_ts=before_ts
        )
        walked.extend(
            item["ts"] for item in result["messages"] if item["is_thread_reply"]
        )
        before_ts = result["coverage"]["next_before_ts"]
        assert result["messages"][0]["ts"] == "199900.000000"
        if before_ts is None:
            break

    assert before_ts is None
    assert sorted(walked) == [
        "199901.000000",
        "199902.000000",
        "199903.000000",
    ], "the walk lost, repeated or invented a reply"


@pytest.mark.asyncio
async def test_a_bound_reached_inside_a_thread_read_stops_at_a_reply() -> None:
    """Per-reply granularity, where the channel walk takes a thread whole.

    Atomicity exists to stop a reply being stranded above a resume position
    expressed as a root. A resume position that is itself a reply cannot
    strand one, so the rule has nothing to do here.
    """
    result = await _read(_thread_client(), ts="199900.000000", max_messages=2)

    assert [item["ts"] for item in result["messages"]] == [
        "199900.000000",
        "199903.000000",
    ]
    assert result["coverage"]["status"] == "partial"
    assert "message_limit" in result["coverage"]["partial_reasons"]
    assert result["coverage"]["next_before_ts"] == "199903.000000"


@pytest.mark.asyncio
async def test_a_ts_that_is_not_a_slack_timestamp_is_refused() -> None:
    result = await _read(_thread_client(), ts="yesterday")

    assert result["ok"] is False
    assert result["error"] == "ts_must_be_a_slack_timestamp"
    assert result["messages"] == []


# ── walking forward ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_after_ts_returns_only_what_arrived_since_the_position_it_names(
) -> None:
    """The direction the tool could not go in.

    ``hours`` measures from now, so a session polling every ten minutes with
    ``hours=1`` re-reads fifty minutes it has already seen. ``after_ts`` takes
    the position the previous result reported and is exclusive, so the message
    that position names does not come back as though it were new.
    """
    whole, _ = await _slice(_sample_channel())
    latest = whole["coverage"]["latest_message_ts"]

    caught_up = await _read(_sample_channel(), after_ts=latest)

    assert caught_up["ok"] is True
    assert [
        item["ts"]
        for item in caught_up["messages"]
        if not item["outside_window_context"]
    ] == []
    assert caught_up["window"]["mode"] == "after_ts"
    assert caught_up["window"]["after_ts"] == latest
    assert caught_up["window"]["requested_hours"] is None

    since_the_middle = await _read(_sample_channel(), after_ts="199905.000000")
    fresh = {
        item["ts"]
        for item in since_the_middle["messages"]
        if not item["outside_window_context"]
    }
    assert fresh == {
        "199906.000000",
        "199907.000000",
        "199908.000000",
        "199909.000000",
        "199950.000000",
        "199951.000000",
    }


@pytest.mark.asyncio
async def test_after_iso_utc_takes_a_date_where_after_ts_takes_a_position() -> None:
    """Two arguments because they take two kinds of value.

    The house rule is that an identifier stays raw and a date takes
    ``_iso_utc``; ``before_ts`` refuses the word "yesterday" on the strength of
    it. One spelling for both would mean guessing which had been passed.
    """
    named_date = slack_history._iso_utc(199_905.0)
    assert named_date is not None

    result = await _read(_sample_channel(), after_iso_utc=named_date)

    fresh = {
        item["ts"]
        for item in result["messages"]
        if not item["outside_window_context"]
    }
    assert fresh == {
        "199906.000000",
        "199907.000000",
        "199908.000000",
        "199909.000000",
        "199950.000000",
        "199951.000000",
    }
    assert result["window"]["mode"] == "after_iso_utc"
    assert result["window"]["after_iso_utc"] == named_date

    refused = await _read(_sample_channel(), after_iso_utc="yesterday")
    assert refused["ok"] is False
    assert refused["error"] == "after_iso_utc_must_be_an_iso_instant"


@pytest.mark.asyncio
async def test_a_position_beats_a_date_beats_a_duration_and_the_result_says_so(
) -> None:
    """The rule the file already follows, applied to three more arguments.

    A value that cannot mean anything is refused; between well-formed values
    the more specific is preferred silently and ``window.mode`` names the one
    applied, exactly as ``all_history`` has always beaten ``hours``.
    """
    both = await _read(
        _sample_channel(),
        hours=1,
        all_history=True,
        after_iso_utc="1976-05-01",
        after_ts="199905.000000",
    )
    assert both["window"]["mode"] == "after_ts"
    assert both["window"]["cutoff_ts"] == "199905.000000"
    assert both["window"]["requested_hours"] is None

    without_position = await _read(
        _sample_channel(), hours=1, all_history=True, after_iso_utc="1976-05-01"
    )
    assert without_position["window"]["mode"] == "after_iso_utc"


@pytest.mark.parametrize(
    ("window_arguments", "channel_mode", "thread_mode"),
    [
        ({}, "hours", "thread"),
        ({"hours": 1}, "hours", "hours"),
        ({"all_history": True}, "all_history", "all_history"),
        ({"all_history": True, "hours": 1}, "all_history", "all_history"),
        ({"after_iso_utc": "1976-05-01"}, "after_iso_utc", "after_iso_utc"),
        (
            {"after_iso_utc": "1976-05-01", "all_history": True, "hours": 1},
            "after_iso_utc",
            "after_iso_utc",
        ),
        ({"after_ts": "199905.000000"}, "after_ts", "after_ts"),
        (
            {
                "after_ts": "199905.000000",
                "after_iso_utc": "1976-05-01",
                "all_history": True,
                "hours": 1,
            },
            "after_ts",
            "after_ts",
        ),
    ],
)
@pytest.mark.asyncio
async def test_the_older_edge_settles_in_one_order_whether_or_not_ts_is_named(
    window_arguments: dict[str, Any], channel_mode: str, thread_mode: str
) -> None:
    """The whole table: after_ts, then after_iso_utc, then all_history, then
    thread, then hours.

    ``thread`` sits between the two because a ts names what to read rather than
    where the window starts: it beats a duration nobody asked for and loses to
    every edge somebody did ask for. Only the no-argument row differs between
    the two columns, which is the whole of what naming a thread adds.

    Every row is written the way a model writes it, arguments omitted rather
    than passed as ``None``.
    """
    channel = _sample_channel()
    read = await _read(channel, **window_arguments)
    assert read["window"]["mode"] == channel_mode

    thread = _thread_client()
    named = await _read(thread, ts="199900.000000", **window_arguments)
    assert named["window"]["mode"] == thread_mode
    # ``window.mode`` orders the window's older edge and does not name the call
    # that was made. A ts reads the thread through conversations.replies under
    # every one of these, and walks no channel to do it.
    assert thread.calls["conversations_replies"]
    assert "conversations_history" not in thread.calls


# ── what one message now says about itself ───────────────────────────────────


def _one_message(message: dict[str, Any]) -> _FakeClient:
    return _FakeClient(
        {
            "auth_test": [
                {"user_id": "U-BOT", "bot_id": "B-BOT", "url": "https://x.slack.com"}
            ],
            "conversations_history": [{"messages": [message]}],
            "conversations_info": [{"channel": {"name": "research"}}],
        }
    )


@pytest.mark.asyncio
async def test_a_message_that_is_not_a_plain_message_says_which_kind_it_is() -> None:
    """The word is reported, and the filtering is the caller's.

    Which subtypes are noise depends on the question, so dropping them by
    default would be answering it here. A caller that wants only what people
    said can skip them once the word is in the record.
    """
    joined = await _read(
        _one_message(
            {
                "ts": "199999.0",
                "user": "U1",
                "subtype": "channel_join",
                "text": "has joined the channel",
            }
        )
    )
    assert joined["messages"][0].get("subtype") == "channel_join"

    plain = await _read(
        _one_message({"ts": "199999.0", "user": "U1", "text": "hello"})
    )
    assert "subtype" not in plain["messages"][0]


@pytest.mark.asyncio
async def test_a_message_posted_by_any_app_is_marked_as_one() -> None:
    """The shared word, beside the narrower one this tool already had.

    ``is_own_bot_message`` answers "did we say this"; ``is_author_bot`` answers
    "did a person say this", which is the one the search tool reports and the
    one a reader summarising a channel acts on.
    """
    from_an_app = await _read(
        _one_message(
            {
                "ts": "199999.0",
                "bot_id": "B-DEPLOYER",
                "username": "Deployer",
                "text": "build 41 is out",
            }
        )
    )
    record = from_an_app["messages"][0]
    assert record.get("is_author_bot") is True
    assert record["is_own_bot_message"] is False

    from_a_person = await _read(
        _one_message({"ts": "199999.0", "user": "U1", "text": "thanks"})
    )
    assert from_a_person["messages"][0].get("is_author_bot") is False


@pytest.mark.asyncio
async def test_a_message_changed_after_it_was_posted_says_when() -> None:
    """Otherwise it reads exactly like one posted as it stands."""
    edited = await _read(
        _one_message(
            {
                "ts": "199990.000000",
                "user": "U1",
                "text": "the meeting is at four",
                "edited": {"user": "U1", "ts": "199995.000000"},
            }
        )
    )
    record = edited["messages"][0]
    assert record.get("edited") == {
        "ts": "199995.000000",
        "ts_iso_utc": slack_history._iso_utc(199_995.0),
    }

    untouched = await _read(
        _one_message({"ts": "199990.000000", "user": "U1", "text": "as posted"})
    )
    assert "edited" not in untouched["messages"][0]


# ── naming the conversation, and naming a refused call ───────────────────────


@pytest.mark.asyncio
async def test_the_snapshot_names_the_conversation_and_not_only_its_id() -> None:
    """A summary that can say only C-RESEARCH reads as though it were about one.

    The topic and the purpose arrive in the same answer, so they cost nothing
    beyond the call the name already needs.
    """
    client = _FakeClient(
        {
            "auth_test": [{"user_id": "U-BOT"}],
            "conversations_history": [
                {"messages": [{"ts": "199999.0", "user": "U1", "text": "hi"}]}
            ],
            "conversations_info": [
                {
                    "channel": {
                        "name": "research",
                        "topic": {"value": "what we are reading"},
                        "purpose": {"value": "the reading group"},
                    }
                }
            ],
        }
    )

    result = await _read(client)

    assert result.get("chat_name") == "research"
    assert result.get("topic") == "what we are reading"
    assert result.get("purpose") == "the reading group"


@pytest.mark.asyncio
async def test_a_conversation_whose_record_is_refused_still_returns_its_messages(
) -> None:
    """The name is how the answer refers to the read. Allowing it is settled
    elsewhere.

    A refusal here must not cost the messages, and must say which call was
    declined: ``missing_scope`` on ``conversations.info`` and on
    ``conversations.history`` are fixed by different grants.
    """
    client = _FakeClient(
        {
            "auth_test": [{"user_id": "U-BOT"}],
            "conversations_history": [
                {"messages": [{"ts": "199999.0", "user": "U1", "text": "hi"}]}
            ],
            "conversations_info": [
                _FakeSlackError(
                    _FakeResponse(
                        status_code=200,
                        data={"ok": False, "error": "missing_scope"},
                    )
                )
            ],
        }
    )

    result = await _read(client)

    assert result["ok"] is True
    assert len(result["messages"]) == 1
    assert result.get("chat_name") == ""
    assert result["coverage"].get("slack_calls_refused") == ["conversations.info"]


@pytest.mark.asyncio
async def test_a_scan_that_slack_refuses_says_which_call_it_refused() -> None:
    """``missing_scope`` alone is not something an operator can act on.

    The gate path has always named the call. The scan path returned the bare
    code, so the same word arrived whether the history read or a thread read
    had been declined, and those are two different grants to go and make.
    """
    client = _FakeClient(
        {
            "auth_test": [{"user_id": "U-BOT"}],
            "conversations_history": [
                _FakeSlackError(
                    _FakeResponse(
                        status_code=200,
                        data={"ok": False, "error": "missing_scope"},
                    )
                )
            ],
        }
    )

    result = await _read(client)

    assert result["ok"] is False
    assert result["error"] == "missing_scope"
    assert result.get("detail") == "Slack refused conversations.history"


# ── one fact about the workspace, asked once ─────────────────────────────────


@pytest.mark.asyncio
async def test_auth_test_is_asked_once_for_the_life_of_the_toolkit() -> None:
    """Three facts about the token, which the request holding it does not change.

    The toolkit is built once and answers every request for the life of the
    process, so a per-read call spends the API budget being told the same
    thing. ``_workspace_file_hosts`` next door was already cached for exactly
    this reason and now reads the same answer.
    """
    client = _FakeClient(
        {
            # Two scripted answers, so that a toolkit asking twice still works
            # and the count below is what fails rather than the read.
            "auth_test": [
                {"user_id": "U-BOT", "url": "https://acme.slack.com/"},
                {"user_id": "U-BOT", "url": "https://acme.slack.com/"},
            ],
            "conversations_history": [
                {"messages": [{"ts": "199999.0", "user": "U1", "text": "one"}]},
                {"messages": [{"ts": "199998.0", "user": "U1", "text": "two"}]},
            ],
            "conversations_info": [
                {"channel": {"name": "research"}},
                {"channel": {"name": "research"}},
            ],
        }
    )
    toolkit = _toolkit(client)

    first = json.loads(await toolkit.read_slack_conversation())
    second = json.loads(await toolkit.read_slack_conversation())

    assert first["ok"] is True and second["ok"] is True
    assert len(client.calls["auth_test"]) == 1
    assert await toolkit._workspace_file_hosts() == frozenset(
        {"files.slack.com", "acme.slack.com"}
    )
    assert len(client.calls["auth_test"]) == 1


# ── a window that is not a window ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_an_hours_value_longer_than_any_history_is_refused_not_widened(
) -> None:
    """Refused, like the two refusals on ``hours`` beside it.

    A million hours quietly became an all-history scan reported as
    ``window.mode: hours`` with a cutoff before Slack existed, so the result
    described itself as something it was not. Clamping would do the same thing
    under a different number.
    """
    refused = await _read(_sample_channel(), hours=1_000_000)

    assert refused["ok"] is False
    assert refused["error"] == "hours_must_name_a_window"
    assert "all_history" in refused["detail"]
    assert refused["messages"] == []

    allowed = await _read(_sample_channel(), hours=24 * 365 * 10)
    assert allowed["ok"] is True
    assert allowed["window"]["mode"] == "hours"


# ── a reply the sender also sent to the channel ──────────────────────────────


class _BroadcastClient(_ChannelClient):
    """A channel walk that also records which ts each thread read asked for."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.replies_ts: list[str] = []

    async def conversations_replies(self, **kwargs: Any) -> dict[str, Any]:
        self.replies_ts.append(str(kwargs["ts"]))
        return await super().conversations_replies(**kwargs)


def _broadcast_channel(**broadcast_extra: Any) -> _BroadcastClient:
    """A broadcast at 199905 beside a genuine thread root at 199906.

    Slack sends a broadcast as a channel item of its own, stating the parent's
    ts in ``thread_ts`` and its own in ``ts``. The parent here sits at 199000,
    far older than anything this page holds, which is the case where nothing
    else in the walk corrects the record. A genuine root states a ``thread_ts``
    equal to its own ts, which is the shape 199906 carries.
    """
    roots = [
        {"ts": "199902.000000", "user": "U1", "text": "plain"},
        {
            "ts": "199906.000000",
            "user": "U1",
            "text": "a root of its own",
            "thread_ts": "199906.000000",
            "reply_count": 1,
            "latest_reply": "199908.000000",
        },
        dict(
            {
                "ts": "199905.000000",
                "thread_ts": "199000.000000",
                "subtype": "thread_broadcast",
                "user": "U2",
                "text": "sent to the channel as well",
            },
            **broadcast_extra,
        ),
    ]
    replies = {
        "199906.000000": [
            {"ts": "199908.000000", "user": "U3", "text": "a reply in the thread"}
        ]
    }
    return _BroadcastClient(roots, replies)


@pytest.mark.asyncio
async def test_a_broadcast_reply_states_the_thread_it_was_written_in() -> None:
    """The record says which thread the message belongs to, not that it is one.

    A broadcast carries its parent's ``thread_ts``. Taking every channel item
    as its own root filed it under a thread_ts naming itself, which states a
    thread that does not exist and hides the one that does. The genuine root
    beside it states a thread_ts equal to its own ts, so it reads exactly as
    it did before.
    """
    result, _ = await _slice(_broadcast_channel())

    broadcast = next(
        item for item in result["messages"] if item["ts"] == "199905.000000"
    )
    assert broadcast["thread_ts"] == "199000.000000"
    assert broadcast["is_thread_reply"] is True
    # A reply count belongs to a root. The broadcast is not one, so it states
    # none rather than stating zero.
    assert "reply_count" not in broadcast

    root = next(item for item in result["messages"] if item["ts"] == "199906.000000")
    assert root["thread_ts"] == "199906.000000"
    assert root["is_thread_reply"] is False
    assert root["reply_count"] == 1


@pytest.mark.asyncio
async def test_a_broadcast_is_not_expanded_as_a_thread_of_its_own() -> None:
    """No thread read is made against a reply, and none is reported missing.

    Slack answers a reply's ts with that one message, so a walk that expanded a
    broadcast spent a call to learn nothing and then reported the thread as
    unreturned. The parent is expanded when the walk reaches the root it names,
    which is older and therefore still ahead of a backwards scan.
    """
    client = _broadcast_channel(reply_count=4, latest_reply="199905.000000")

    result, _ = await _slice(client)

    assert client.replies_ts == ["199906.000000"]
    assert "thread_replies_not_returned" not in result["coverage"]["partial_reasons"]
    assert result["coverage"]["status"] == "complete"


@pytest.mark.asyncio
async def test_a_broadcast_does_not_move_the_resume_position_to_its_parent() -> None:
    """The next slice starts where the scan stopped, not where the thread began.

    The resume position is a channel position. A broadcast's parent sits
    further back in the channel than the scan has reached, so reading the
    position off the message's thread_ts would send the next slice past roots
    this one never scanned.
    """
    client = _BroadcastClient(
        [
            {
                "ts": "199907.000000",
                "thread_ts": "199000.000000",
                "subtype": "thread_broadcast",
                "user": "U2",
                "text": "sent to the channel as well",
            },
            {"ts": "199906.000000", "user": "U1", "text": "plain"},
            {"ts": "199905.000000", "user": "U1", "text": "plain"},
        ],
        {},
    )

    result = json.loads(
        await _toolkit(client).read_slack_conversation(
            all_history=True, max_messages=2
        )
    )

    assert result["ok"] is True
    assert result["coverage"]["status"] == "partial"
    broadcast = next(
        item for item in result["messages"] if item["ts"] == "199907.000000"
    )
    assert broadcast["thread_ts"] == "199000.000000"
    # 199907 and 199906 are in hand and 199905 is not, so the next slice starts
    # above 199906. The parent at 199000 is what a thread_ts read would have
    # offered, and everything between it and 199906 would have been skipped.
    assert result["coverage"]["next_before_ts"] == "199906.000000"


# ── who reacted, and how much of that is known ───────────────────────────────


def _reacted(reactions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{"ts": "199999.0", "user": "U1", "text": "a message", "reactions": reactions}]


@pytest.mark.asyncio
async def test_a_reaction_names_the_emoji_and_the_people_who_left_it() -> None:
    """The shortcode is spelled the way a write tool will have to take it.

    Slack calls the shortcode ``name``. A tool that adds a reaction cannot take
    a top-level argument by that word, so a record spelling it ``name`` would
    have a model read one word here and pass a different one back.
    """
    result = await _history_with(
        _reacted([{"name": "eyes", "count": 2, "users": ["U1", "U2"]}])
    )

    reaction = result["messages"][0]["reactions"][0]
    assert reaction["emoji_name"] == "eyes"
    assert "name" not in reaction
    assert reaction["count"] == 2
    assert reaction["reactor_user_ids"] == ["U1", "U2"]
    # Nothing was short and nothing was cut, so neither is claimed.
    assert "reactors_partial" not in reaction
    assert "reactors_truncated" not in reaction


@pytest.mark.asyncio
async def test_a_reactor_list_shorter_than_its_count_says_that_it_is_short() -> None:
    """Slack documents ``users`` as possibly short of ``count``.

    A reader deciding whether somebody acknowledged a message has to tell
    "absent from the list I was given" from "did not react", and only the
    record can say which of the two it is holding.
    """
    result = await _history_with(
        _reacted([{"name": "eyes", "count": 4, "users": ["U1"]}])
    )

    reaction = result["messages"][0]["reactions"][0]
    assert reaction["count"] == 4
    assert reaction["reactor_user_ids"] == ["U1"]
    assert reaction["reactors_partial"] is True
    assert "reactors_truncated" not in reaction


@pytest.mark.asyncio
async def test_a_reactor_list_this_record_cut_says_so_as_a_different_thing() -> None:
    """The two ways a list can be short are named separately.

    Slack sending fewer than it counted and this record bounding what Slack
    sent are different claims about the same list, and folding them into one
    word would leave a reader unable to tell which happened.
    """
    reactors = [f"U{index:03d}" for index in range(30)]

    result = await _history_with(
        _reacted([{"name": "eyes", "count": 30, "users": reactors}])
    )

    reaction = result["messages"][0]["reactions"][0]
    assert reaction["count"] == 30
    assert len(reaction["reactor_user_ids"]) == 20
    assert reaction["reactors_truncated"] is True
    # Slack sent everybody it counted, so nothing about its answer was partial.
    assert "reactors_partial" not in reaction
    assert result["coverage"]["max_reactors_per_reaction"] == 20


@pytest.mark.asyncio
async def test_the_reactor_bound_is_the_callers_to_move() -> None:
    """The one cap here that defends something Slack charges for.

    Every reactor named costs a users.info lookup from a budget the whole
    answer shares, so the default stays low. A caller whose question is who
    reacted can pay for the rest, and is told what bound it is paying to lift.
    """
    reactors = [f"U{index:03d}" for index in range(30)]
    raised = await _history_with(
        _reacted([{"name": "eyes", "count": 30, "users": reactors}]),
        read_kwargs={"max_reactors_per_reaction": 30},
    )

    reaction = raised["messages"][0]["reactions"][0]
    assert reaction["reactor_user_ids"] == reactors
    assert "reactors_truncated" not in reaction
    assert raised["coverage"]["max_reactors_per_reaction"] == 30

    lowered = await _history_with(
        _reacted([{"name": "eyes", "count": 30, "users": reactors}]),
        read_kwargs={"max_reactors_per_reaction": 3},
    )

    cut = lowered["messages"][0]["reactions"][0]
    assert cut["reactor_user_ids"] == reactors[:3]
    assert cut["reactors_truncated"] is True
    assert lowered["coverage"]["max_reactors_per_reaction"] == 3


@pytest.mark.asyncio
async def test_every_reaction_a_message_holds_is_kept() -> None:
    """There was a cap of twenty here, and it defended nothing.

    It was set by analogy with the attachment bound. A reaction entry is an
    emoji name and a count, the people named under it are bounded one level
    down by the lookup budget, and how many distinct emoji a message carries is
    bounded by how many a room thought to use. What the cap actually did was
    report, silently, that nobody reacted with the twenty-first emoji.
    """
    result = await _history_with(
        _reacted(
            [
                {"name": f"emoji-{index}", "count": 1, "users": ["U1"]}
                for index in range(40)
            ]
        )
    )

    message = result["messages"][0]
    assert len(message["reactions"]) == 40
    assert [entry["emoji_name"] for entry in message["reactions"]] == [
        f"emoji-{index}" for index in range(40)
    ]
    # No flag, because there is nothing left for one to be about.
    assert "reactions_truncated" not in message


def _reaction_lookup_client() -> _FakeClient:
    return _FakeClient(
        {
            "auth_test": [{"user_id": "U-BOT"}],
            "conversations_history": [
                {
                    "messages": [
                        {
                            "ts": "199999.0",
                            "user": "U1",
                            "text": "shipped",
                            "reactions": [
                                {"name": "tada", "count": 2, "users": ["U2", "U3"]}
                            ],
                        },
                        {"ts": "199998.0", "user": "U2", "text": "nice"},
                    ]
                }
            ],
            "users_info": [
                {"user": {"id": "U1", "profile": {"display_name": "Alice"}}},
                {"user": {"id": "U2", "profile": {"display_name": "Bob"}}},
                {"user": {"id": "U3", "profile": {"display_name": "Cleo"}}},
            ],
        }
    )


def _lookup_toolkit(client: _FakeClient, lookups: int) -> SlackHistoryToolkit:
    return SlackHistoryToolkit(
        metadata={
            "slack_channel_id": "C1",
            "slack_channel_type": "channel",
            "slack_history_policy": "origin",
        },
        client=client,
        now=lambda: 200_000.0,
        max_user_lookups=lookups,
    )


@pytest.mark.asyncio
async def test_reactor_ids_are_resolved_in_the_same_pass_the_authors_use() -> None:
    """A list of account identifiers answers the question no better than nothing.

    One pass and one budget rather than a second resolution path, which would
    mean a second cap nobody configured and a second set of warnings saying
    what the first set already says.
    """
    client = _reaction_lookup_client()

    result = json.loads(
        await _lookup_toolkit(client, 10).read_slack_conversation(
            include_threads=False
        )
    )

    reaction = result["messages"][1]["reactions"][0]
    assert reaction["reactor_user_ids"] == ["U2", "U3"]
    assert reaction["reactor_names"] == ["Bob", "Cleo"]
    assert sorted(call["user"] for call in client.calls["users_info"]) == [
        "U1",
        "U2",
        "U3",
    ]


@pytest.mark.asyncio
async def test_the_lookup_budget_is_spent_on_authors_before_reactors() -> None:
    """An author is never displaced by a reactor.

    Every message has an author and the record's attribution is built on it,
    while a reactor annotates somebody else's message. A reactor left
    unresolved keeps its id, which is what an unresolved author keeps and is
    visibly not a name.
    """
    client = _reaction_lookup_client()

    result = json.loads(
        await _lookup_toolkit(client, 2).read_slack_conversation(
            include_threads=False
        )
    )

    assert [call["user"] for call in client.calls["users_info"]] == ["U1", "U2"]
    assert [item["author_name"] for item in result["messages"]] == ["Bob", "Alice"]
    reaction = result["messages"][1]["reactions"][0]
    assert reaction["reactor_names"] == ["Bob", "U3"]
    assert "author_name_lookup_limit" in result["coverage"]["warnings"]


# ── a rich-text walk that stopped short says so ──────────────────────────────


@pytest.mark.asyncio
async def test_block_text_cut_by_the_string_bound_says_it_was_cut() -> None:
    """The bound cuts the list of strings, so no string can carry an ellipsis.

    A Block Kit payload built to be enormous costs a truncated read rather
    than the scan, which is the bound working. Without a marker the record
    presents the part it read as the whole of what was posted.
    """
    blocks = [
        {"type": "section", "text": {"type": "mrkdwn", "text": f"line {index}"}}
        for index in range(slack_history.MAX_RICH_TEXT_STRINGS + 5)
    ]

    result = await _history_with(
        [{"ts": "199999.0", "user": "U1", "text": "", "blocks": blocks}]
    )

    message = result["messages"][0]
    assert message["block_text_truncated"] is True
    assert len(message["block_text"].splitlines()) == (
        slack_history.MAX_RICH_TEXT_STRINGS
    )


@pytest.mark.asyncio
async def test_attachment_text_cut_by_the_depth_bound_says_it_was_cut() -> None:
    """The other bound, on the other payload, reported the same way."""
    buried: dict[str, Any] = {"text": "the readable line"}
    for _ in range(slack_history.MAX_RICH_TEXT_DEPTH + 2):
        buried = {"nested": buried}

    result = await _history_with(
        [{"ts": "199999.0", "user": "U1", "text": "hello", "attachments": [buried]}]
    )

    message = result["messages"][0]
    # Nothing readable came back at all, which is exactly the case a bare list
    # of strings cannot tell from an attachment that held no text. The flag is
    # what tells them apart, so it is stated with no text beside it.
    assert "attachment_text" not in message
    assert message["attachment_text_truncated"] is True

    shallow = await _history_with(
        [
            {
                "ts": "199999.0",
                "user": "U1",
                "text": "hello",
                "attachments": [{"text": "the readable line"}],
            }
        ]
    )
    assert shallow["messages"][0]["attachment_text"] == "the readable line"
    assert "attachment_text_truncated" not in shallow["messages"][0]


@pytest.mark.asyncio
async def test_a_payload_within_both_bounds_claims_no_truncation() -> None:
    result = await _history_with(
        [
            {
                "ts": "199999.0",
                "user": "U1",
                "text": "",
                "blocks": [
                    {"type": "section", "text": {"type": "mrkdwn", "text": "one"}}
                ],
                "attachments": [{"text": "two"}],
                "metadata": {"event_type": "deploy", "event_payload": {"env": "prod"}},
            }
        ]
    )

    message = result["messages"][0]
    assert message["block_text"] == "one"
    assert message["attachment_text"] == "two"
    assert message["metadata_text"] == "deploy\nprod"
    assert not [key for key in message if key.endswith("_text_truncated")]


def test_the_connector_still_reads_a_bare_list_of_strings() -> None:
    """The shared walk keeps the signature the inbound path calls it by.

    The connector writes the strings into a description it clamps to a
    character count of its own, so a bound reached in the walk reaches its
    reader as that clamp. Widening the one signature both readers share would
    have made a change to this tool a change to the path that dispatches turns.
    """
    payload = [{"text": "one"}, {"text": "two"}]

    assert slack_rich_text.walk_strings(payload, slack_rich_text.RICH_TEXT_KEYS) == [
        "one",
        "two",
    ]
    assert slack_rich_text.walk_strings_bounded(
        payload, slack_rich_text.RICH_TEXT_KEYS
    ) == (["one", "two"], False)


# ── paging a long thread ─────────────────────────────────────────────────────


class _LongThreadClient:
    """A thread read that honours ``latest`` and pages the range it leaves.

    Slack answers ``conversations.replies`` with the root and then the replies
    inside the bounds, a page at a time. A fake that ignored ``latest`` would
    report the same messages whatever the tool asked for, and the cost this
    tests is exactly what the tool asks for.
    """

    ROOT_TS = "199000.000000"

    def __init__(self, reply_count: int, page_size: int = 200) -> None:
        self.page_size = page_size
        self.root = {
            "ts": self.ROOT_TS,
            "user": "U1",
            "text": "root",
            "thread_ts": self.ROOT_TS,
            "reply_count": reply_count,
        }
        self.replies = [
            {
                "ts": f"199{index:03d}.000000",
                "user": "U2",
                "text": f"reply {index}",
                "thread_ts": self.ROOT_TS,
            }
            for index in range(1, reply_count + 1)
        ]
        self.reply_calls: list[dict[str, Any]] = []

    def _in_range(self, kwargs: dict[str, Any]) -> list[dict[str, Any]]:
        latest = float(kwargs["latest"])
        inclusive = bool(kwargs.get("inclusive"))
        return [
            reply
            for reply in self.replies
            if (float(reply["ts"]) <= latest if inclusive else float(reply["ts"]) < latest)
        ]

    async def auth_test(self, **kwargs: Any) -> dict[str, Any]:
        return {"user_id": "U-BOT", "bot_id": "B-BOT", "url": "https://x.slack.com"}

    async def conversations_history(self, **kwargs: Any) -> dict[str, Any]:
        return {"messages": [], "has_more": False}

    async def conversations_info(self, **kwargs: Any) -> dict[str, Any]:
        return {"channel": {"name": "research"}}

    async def conversations_replies(self, **kwargs: Any) -> dict[str, Any]:
        self.reply_calls.append(dict(kwargs))
        # The root is answered whatever the bounds say, which is what the
        # neighbouring comment about ``oldest`` records having observed.
        selected = [self.root] + self._in_range(kwargs)
        start = int(kwargs.get("cursor") or 0)
        page = selected[start : start + self.page_size]
        has_more = start + self.page_size < len(selected)
        response: dict[str, Any] = {"messages": page, "has_more": has_more}
        if has_more:
            response["response_metadata"] = {
                "next_cursor": str(start + self.page_size)
            }
        return response


class _LatestIgnoringThreadClient(_LongThreadClient):
    """The same thread, from a server that pays no attention to ``latest``."""

    def _in_range(self, kwargs: dict[str, Any]) -> list[dict[str, Any]]:
        return list(self.replies)


@pytest.mark.asyncio
async def test_a_resumed_thread_slice_is_not_paged_from_the_newest_reply() -> None:
    """The read starts at the resume position rather than at the thread's end.

    Every reply above the resume position is one the previous slice already
    returned, and downloading them again on every slice is what puts a long
    thread out of reach of the call budget entirely.
    """
    bounded = _LongThreadClient(400)
    unbounded = _LongThreadClient(400)

    resumed = await _read(bounded, ts=_LongThreadClient.ROOT_TS, before_ts="199201.000000")
    whole = await _read(unbounded, ts=_LongThreadClient.ROOT_TS)

    assert resumed["ok"] is True
    # 201 messages in range against a page of 200, where the whole thread is
    # 401 and costs three.
    assert len(bounded.reply_calls) == 2
    assert len(unbounded.reply_calls) == 3
    assert {call["latest"] for call in bounded.reply_calls} == {"199201.000000"}
    assert all(call.get("inclusive") is True for call in bounded.reply_calls)


@pytest.mark.asyncio
async def test_the_resume_bound_changes_the_price_and_not_the_messages() -> None:
    """The local filter is the guarantee; the bound sent is an optimisation.

    A server that applied ``latest`` differently, or ignored it, would hand
    back the same messages at the old price rather than a different set.
    """
    honoured = await _read(
        _LongThreadClient(400),
        ts=_LongThreadClient.ROOT_TS,
        before_ts="199201.000000",
    )
    ignored = await _read(
        _LatestIgnoringThreadClient(400),
        ts=_LongThreadClient.ROOT_TS,
        before_ts="199201.000000",
    )

    expected = [_LongThreadClient.ROOT_TS] + [
        f"199{index:03d}.000000" for index in range(1, 201)
    ]
    assert [item["ts"] for item in honoured["messages"]] == expected
    assert [item["ts"] for item in ignored["messages"]] == expected


class _EmptyUnderTheBoundClient(_LongThreadClient):
    """A server that answers nothing at all once ``latest`` is not the snapshot."""

    async def conversations_replies(self, **kwargs: Any) -> dict[str, Any]:
        if float(kwargs["latest"]) < 200_000.0:
            self.reply_calls.append(dict(kwargs))
            return {"messages": [], "has_more": False}
        return await super().conversations_replies(**kwargs)


@pytest.mark.asyncio
async def test_an_empty_answer_under_the_resume_bound_is_read_again_without_it(
) -> None:
    """A thread is never lost to the bound this call added.

    Slack answers a read with the message the ts names whatever the bounds say,
    so this cannot happen against the endpoint as documented. It is here
    because the resume position is the only thing that could empty an answer
    the unbounded read fills, and losing a whole thread to it would be silent.
    """
    client = _EmptyUnderTheBoundClient(4)

    result = await _read(
        client, ts=_LongThreadClient.ROOT_TS, before_ts="199003.000000"
    )

    assert [item["ts"] for item in result["messages"]] == [
        _LongThreadClient.ROOT_TS,
        "199001.000000",
        "199002.000000",
    ]
    assert (
        "thread_read_retried_without_the_resume_bound"
        in result["coverage"]["warnings"]
    )
    # One bounded read, one unbounded read, and no third.
    assert [call["latest"] for call in client.reply_calls] == [
        "199003.000000",
        "200000.000000",
    ]


# ── where a windowed threaded scan stops ─────────────────────────────────────


async def _windowed(client: _ChannelClient, **kwargs: Any) -> dict[str, Any]:
    """One hour of a channel, which puts the cutoff at 196400."""
    result = json.loads(
        await _toolkit(client).read_slack_conversation(hours=1, **kwargs)
    )
    assert result["ok"] is True
    return result


@pytest.mark.asyncio
async def test_a_windowed_threaded_scan_stops_at_the_first_page_beyond_it(
) -> None:
    """The scan ends rather than walking the channel to its beginning.

    Slack is sent no oldest bound on a threaded scan, because a thread with
    recent replies can have a root older than the cutoff. Nothing stopped the
    scan locally either, so a day of a busy channel read years of it and then
    reported a resume position the card told the caller to keep following.
    """
    client = _ChannelClient(
        [
            {"ts": "199901.000000", "user": "U1", "text": "today"},
            {"ts": "199900.000000", "user": "U1", "text": "today"},
            {"ts": "190002.000000", "user": "U1", "text": "last week"},
            {"ts": "190001.000000", "user": "U1", "text": "last week"},
            {"ts": "190000.000000", "user": "U1", "text": "last week"},
        ],
        page_size=2,
    )

    result = await _windowed(client)

    # The second page is entirely older than the cutoff and names no reply
    # inside it, so the third is never asked for.
    assert len(client.history_calls) == 2
    assert result["coverage"]["history_pages"] == 2
    assert [item["ts"] for item in result["messages"]] == [
        "199900.000000",
        "199901.000000",
    ]
    assert result["coverage"]["status"] == "complete"
    assert result["coverage"]["next_before_ts"] is None


@pytest.mark.asyncio
async def test_an_old_root_with_a_recent_reply_keeps_the_scan_going() -> None:
    """The case the missing oldest bound exists for still works.

    A root older than the cutoff whose latest reply is inside it is why Slack
    is not given the bound, so a stop that ignored latest_reply would lose
    exactly the threads the scan is paging back for.
    """
    client = _ChannelClient(
        [
            {"ts": "190001.000000", "user": "U1", "text": "old and quiet"},
            {
                "ts": "190000.000000",
                "user": "U1",
                "text": "old but still going",
                "reply_count": 1,
                "latest_reply": "199950.000000",
            },
            {"ts": "189001.000000", "user": "U1", "text": "older"},
            {"ts": "189000.000000", "user": "U1", "text": "older"},
            {"ts": "188001.000000", "user": "U1", "text": "older still"},
            {"ts": "188000.000000", "user": "U1", "text": "older still"},
        ],
        {
            "190000.000000": [
                {"ts": "199950.000000", "user": "U2", "text": "a reply today"}
            ]
        },
        page_size=2,
    )

    result = await _windowed(client)

    assert [item["ts"] for item in result["messages"]] == [
        "190000.000000",
        "199950.000000",
    ]
    root = result["messages"][0]
    assert root["outside_window_context"] is True
    assert result["messages"][1]["is_thread_reply"] is True
    # The first page held the thread, so the second was still worth asking for
    # and the third was not.
    assert len(client.history_calls) == 2
    assert result["coverage"]["next_before_ts"] is None


@pytest.mark.asyncio
async def test_a_scan_without_threads_still_lets_slack_apply_the_cutoff() -> None:
    """The bound Slack can apply is still applied where it is sound to."""
    client = _ChannelClient(
        [
            {"ts": "199901.000000", "user": "U1", "text": "today"},
            {"ts": "190002.000000", "user": "U1", "text": "last week"},
        ],
        page_size=2,
    )

    result = await _windowed(client, include_threads=False)

    assert client.history_calls[0]["oldest"] == "196400.000000"
    assert [item["ts"] for item in result["messages"]] == ["199901.000000"]
    assert result["coverage"]["next_before_ts"] is None


@pytest.mark.asyncio
async def test_an_all_history_scan_still_walks_the_channel_to_its_beginning(
) -> None:
    """There is no cutoff to be older than, so nothing stops the scan early."""
    client = _ChannelClient(
        [
            {"ts": f"19{index:04d}.000000", "user": "U1", "text": "a message"}
            for index in range(9990, 9996)
        ],
        page_size=2,
    )

    result, returned = await _slice(client)

    assert len(client.history_calls) == 3
    assert len(returned) == 6
    assert result["coverage"]["next_before_ts"] is None


# ── the fields Slack already sent ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_root_says_when_its_thread_was_last_answered() -> None:
    """A reply count with no age against it cannot choose a thread.

    Slack states latest_reply on every root and the scan already reads it to
    decide what to expand. Discarding it left a caller with reply_count: 7 and
    no way to tell a thread that stopped in March from one that moved an hour
    ago, short of opening every one of them.
    """
    result = await _history_with(
        [
            {
                "ts": "199900.000000",
                "user": "U1",
                "text": "a thread root",
                "reply_count": 7,
                "reply_users_count": 3,
                "latest_reply": "199950.000000",
            },
            {"ts": "199901.000000", "user": "U1", "text": "a message on its own"},
        ]
    )

    root = next(item for item in result["messages"] if item["ts"] == "199900.000000")
    assert root["reply_count"] == 7
    assert root["latest_reply_ts"] == "199950.000000"
    assert root["latest_reply_iso_utc"] is not None
    assert root["reply_users_count"] == 3

    alone = next(item for item in result["messages"] if item["ts"] == "199901.000000")
    assert alone["reply_count"] == 0
    assert "latest_reply_ts" not in alone
    assert "reply_users_count" not in alone


@pytest.mark.asyncio
async def test_an_app_post_names_the_app_and_not_only_its_label() -> None:
    """The display name is a label; the ids are what stay put.

    Two messages from one app posted under two configured names read as two
    apps, and one name moved between apps reads as one, so a record holding
    only the label had no stable answer to which app posted a message.
    """
    result = await _history_with(
        [
            {
                "ts": "199900.000000",
                "username": "Uptime Robot",
                "bot_id": "B0123",
                "app_id": "A0456",
                "subtype": "bot_message",
                "text": "deploy finished",
            },
            {
                "ts": "199901.000000",
                "username": "Buildbot",
                "bot_id": "B0789",
                "bot_profile": {"name": "Buildbot", "app_id": "A0999"},
                "text": "build green",
            },
            {"ts": "199902.000000", "user": "U1", "text": "a person"},
        ]
    )

    first, second, person = result["messages"]
    assert first["author_bot_id"] == "B0123"
    assert first["author_app_id"] == "A0456"
    assert first["author_name"] == "Uptime Robot"
    assert first["author_user_id"] == ""
    # Slack states app_id on the bot profile rather than beside the message on
    # some payloads, and it is the same identifier either way.
    assert second["author_app_id"] == "A0999"
    assert "author_bot_id" not in person
    assert "author_app_id" not in person


@pytest.mark.asyncio
async def test_a_partial_thread_read_can_hand_back_messages_with_no_resume(
) -> None:
    """The card claimed a null resume position meant nothing came back.

    Inside a thread the resume position is a reply, so a slice holding the root
    and no reply has nowhere to point and still has a message in it. A caller
    following the old wording would have thrown the root away.
    """
    result = await _read(_thread_client(), ts="199900.000000", max_messages=1)

    assert result["coverage"]["status"] == "partial"
    assert result["coverage"]["next_before_ts"] is None
    assert [item["ts"] for item in result["messages"]] == ["199900.000000"]


def test_the_card_states_the_paging_rules_that_hold() -> None:
    """Two claims the card made were false of a thread read.

    A thread slice repeats its root, because the replies are read against it,
    so "nothing is fetched twice" described the conversation walk alone. And a
    partial result with a null next_before_ts can hold the root it repeated,
    so "the scan returned nothing" was not a reading of it either.
    """
    description = _toolkit(_thread_client()).get_tools()[0]._card.description

    assert "Slices of one thread repeat that thread's root message" in description
    assert "nothing is fetched twice" not in description
    assert "means the scan returned nothing" not in description
    assert "nothing further to page to" in description
