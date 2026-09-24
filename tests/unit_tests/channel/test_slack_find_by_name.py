# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Unit tests for ``find_by_name``, the Slack name-to-id lookup.

Four properties are pinned here, and they are the four the tool exists for.

*Every match comes back and none is chosen.* Two people called Alice both
appear, and each says which of Slack's three name fields the query hit, because
that is what lets a model put the ambiguity to a person instead of guessing.

*Nothing usable is dropped for being unusable.* A deactivated account and an
archived channel are returned marked, because "it exists and is archived" and
"there is no such channel" are opposite answers.

*A shortened answer says so in numbers.* When more things match than fit, the
count is the true one and the flag is set.

*An absence is only ever an absence.* A kind refused by Slack is named in
``coverage`` rather than skipped, and a lookup in which nothing could be read
refuses rather than answering with an empty list.

Alongside those: the operator's ``history_never_read`` list removing a
conversation, the per-workspace cache serving a second call without a second
dump, and the metadata provider failing closed.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

import jiuwenswarm.common.config as config_module
from jiuwenswarm.agents.harness.common.tools.slack_directory import (
    SLACK_KINDS,
    SlackDirectoryToolkit,
    scopes_for_kind,
    slack_find_request_metadata,
)

_BOT_TOKEN = "xoxb-config-secret"
_TEAM = "T-ACME"

# Two people whose display names are the same string and whose accounts are
# not. This pair is the whole reason the tool returns a list.
_ALICE_ONE = {
    "id": "U-ALICE-1",
    "name": "alice.tan",
    "deleted": False,
    "is_bot": False,
    "profile": {"real_name": "Alice Tan", "display_name": "alice"},
}
_ALICE_TWO = {
    "id": "U-ALICE-2",
    "name": "a.mwangi",
    "deleted": False,
    "is_bot": False,
    "profile": {"real_name": "Alice Mwangi", "display_name": "alice"},
}
_RETIRED = {
    "id": "U-ALICE-3",
    "name": "alice.former",
    "deleted": True,
    "is_bot": False,
    "profile": {"real_name": "Alice Former", "display_name": "alice.former"},
}
_BOT = {
    "id": "U-BOT",
    "name": "alicebot",
    "deleted": False,
    "is_bot": True,
    "profile": {"real_name": "Alice Bot", "display_name": "alicebot"},
}

_ANNOUNCEMENTS = {
    "id": "C-ANN",
    "name": "announcements",
    "is_private": False,
    "is_archived": False,
}
_ANNOUNCEMENTS_OLD = {
    "id": "C-ANN-OLD",
    "name": "announcements-2019",
    "is_private": False,
    "is_archived": True,
}
_ANNOUNCEMENTS_HR = {
    "id": "C-ANN-HR",
    "name": "announcements-hr",
    "is_private": True,
    "is_archived": False,
}

_USERGROUPS = [
    {
        "id": "S-ALICE",
        "name": "Alice's reviewers",
        "handle": "alice-reviewers",
        "description": "reviews Alice's work",
        "date_delete": 0,
    },
    {
        "id": "S-GONE",
        "name": "Old alice group",
        "handle": "alice-old",
        "description": "",
        "date_delete": 1700000000,
    },
]

_EMOJI = {
    "alice-wave": "https://emoji.example/alice-wave.png",
    "ship-it": "alias:rocket",
    "party": "https://emoji.example/party.png",
}


class _SlackApiError(Exception):
    """The shape slack_sdk raises: an exception holding the failed response."""

    def __init__(self, error: str) -> None:
        super().__init__("sanitized fake failure")
        self.response = {"ok": False, "error": error}


class _FakeClient:
    """Answers the four listing methods, and records every call made.

    ``refuse`` maps a method to the error code it should refuse with, which is
    how a workspace holding some grants and not others is reproduced.
    """

    def __init__(
        self,
        *,
        users: list[dict[str, Any]] | None = None,
        channels: list[dict[str, Any]] | None = None,
        usergroups: list[dict[str, Any]] | None = None,
        emoji: dict[str, str] | None = None,
        refuse: dict[str, str] | None = None,
        user_pages: list[list[dict[str, Any]]] | None = None,
    ) -> None:
        self.users = [] if users is None else users
        self.channels = [] if channels is None else channels
        self.usergroups = [] if usergroups is None else usergroups
        self.emoji = {} if emoji is None else emoji
        self.refuse = refuse or {}
        self.user_pages = user_pages
        self.calls: list[dict[str, Any]] = []

    def _record(self, method: str, kwargs: dict[str, Any]) -> None:
        self.calls.append({"method": method, **kwargs})
        error = self.refuse.get(method)
        if error:
            raise _SlackApiError(error)

    def methods_called(self) -> list[str]:
        return [call["method"] for call in self.calls]

    async def users_list(self, **kwargs: Any) -> Any:
        self._record("users_list", kwargs)
        if self.user_pages is None:
            return {"ok": True, "members": self.users}
        cursor = str(kwargs.get("cursor") or "")
        index = int(cursor) if cursor else 0
        page = self.user_pages[index]
        more = index + 1 < len(self.user_pages)
        return {
            "ok": True,
            "members": page,
            "response_metadata": {"next_cursor": str(index + 1) if more else ""},
        }

    async def conversations_list(self, **kwargs: Any) -> Any:
        self._record("conversations_list", kwargs)
        return {"ok": True, "channels": self.channels}

    async def usergroups_list(self, **kwargs: Any) -> Any:
        self._record("usergroups_list", kwargs)
        return {"ok": True, "usergroups": self.usergroups}

    async def emoji_list(self, **kwargs: Any) -> Any:
        self._record("emoji_list", kwargs)
        return {"ok": True, "emoji": self.emoji}


@pytest.fixture(autouse=True)
def _config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        config_module,
        "get_config",
        lambda: {"channels": {"slack": {"bot_token": _BOT_TOKEN}}},
    )


def _metadata(**overrides: Any) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "slack_team_id": _TEAM,
        "slack_channel_id": "C-OPS",
        "slack_history_never_read": [],
    }
    metadata.update(overrides)
    return metadata


def _toolkit(
    client: Any,
    *,
    metadata: dict[str, Any] | None = None,
    provider: Any | None = None,
    **kwargs: Any,
) -> SlackDirectoryToolkit:
    if provider is not None:
        return SlackDirectoryToolkit(
            metadata_provider=provider, client=client, **kwargs
        )
    chosen = _metadata() if metadata is None else metadata
    return SlackDirectoryToolkit(
        metadata_provider=lambda: chosen, client=client, **kwargs
    )


def _find(toolkit: SlackDirectoryToolkit, **kwargs: Any) -> dict[str, Any]:
    return json.loads(asyncio.run(toolkit.find_by_name(**kwargs)))


def _everything(**kwargs: Any) -> _FakeClient:
    """A workspace holding all four directories, with something in each."""
    return _FakeClient(
        users=[_ALICE_ONE, _ALICE_TWO, _RETIRED, _BOT],
        channels=[_ANNOUNCEMENTS, _ANNOUNCEMENTS_OLD, _ANNOUNCEMENTS_HR],
        usergroups=_USERGROUPS,
        emoji=_EMOJI,
        **kwargs,
    )


def _ids(result: dict[str, Any], kind: str | None = None) -> list[str]:
    return [
        match["id"]
        for match in result["matches"]
        if kind is None or match["kind"] == kind
    ]


# -- every match comes back, and none is chosen ------------------------------


def test_two_people_with_the_same_name_are_both_returned() -> None:
    """The property the tool exists for: an ambiguity reaches the model whole."""
    result = _find(_toolkit(_everything()), query="alice", kind="person")

    assert result["ok"] is True
    assert "U-ALICE-1" in _ids(result)
    assert "U-ALICE-2" in _ids(result)
    assert result["match_count"] >= 2


def test_neither_alice_is_preferred_over_the_other() -> None:
    """Both are exact display-name matches, so nothing may rank one above."""
    result = _find(_toolkit(_everything()), query="alice", kind="person")
    exact = [
        match["id"]
        for match in result["matches"]
        if match["match_type"] == "exact"
    ]

    assert sorted(exact) == ["U-ALICE-1", "U-ALICE-2"]


def test_matched_names_the_field_the_query_hit() -> None:
    """A model cannot explain an ambiguity without knowing which name matched."""
    result = _find(_toolkit(_everything()), query="alice.tan", kind="person")
    by_id = {match["id"]: match for match in result["matches"]}

    assert by_id["U-ALICE-1"]["matched"] == "name"
    assert by_id["U-ALICE-1"]["match_type"] == "exact"


def test_matched_distinguishes_the_three_name_fields() -> None:
    client = _everything()
    display = _find(_toolkit(client), query="alice", kind="person")
    real = _find(_toolkit(client), query="Alice Mwangi", kind="person")

    assert {
        match["matched"] for match in display["matches"] if match["id"] == "U-ALICE-1"
    } == {"display_name"}
    assert {
        match["matched"] for match in real["matches"] if match["id"] == "U-ALICE-2"
    } == {"real_name"}


def test_a_person_carries_the_common_core_and_slack_s_own_fields() -> None:
    result = _find(_toolkit(_everything()), query="alice.tan", kind="person")
    match = result["matches"][0]

    for key in ("kind", "id", "name", "is_active"):
        assert key in match, key
    assert match["real_name"] == "Alice Tan"
    assert match["display_name"] == "alice"
    assert match["is_bot"] is False


def test_a_bot_account_is_returned_marked_rather_than_dropped() -> None:
    result = _find(_toolkit(_everything()), query="alicebot", kind="person")

    assert [match["is_bot"] for match in result["matches"]] == [True]


# -- kind, and the open vocabulary -------------------------------------------


def test_omitting_kind_searches_every_kind_this_platform_names() -> None:
    client = _everything()
    result = _find(_toolkit(client), query="alice")

    assert result["kinds"] == list(SLACK_KINDS)
    assert sorted(client.methods_called()) == [
        "conversations_list",
        "emoji_list",
        "usergroups_list",
        "users_list",
    ]
    assert {match["kind"] for match in result["matches"]} == {
        "person",
        "usergroup",
        "emoji",
    }


def test_naming_one_kind_calls_only_that_kind_s_method() -> None:
    client = _everything()
    _find(_toolkit(client), query="announcements", kind="chat")

    assert client.methods_called() == ["conversations_list"]


def test_a_kind_this_platform_does_not_name_is_refused_by_name() -> None:
    client = _everything()
    result = _find(_toolkit(client), query="alice", kind="department")

    assert result["ok"] is False
    assert result["error"] == "unknown_kind"
    assert "department" in result["detail"]
    assert client.calls == []


def test_an_empty_query_is_refused_before_slack_is_asked() -> None:
    client = _everything()
    result = _find(_toolkit(client), query="   ")

    assert result["ok"] is False
    assert result["error"] == "query_required"
    assert client.calls == []


def test_decoration_a_person_writes_round_a_name_is_ignored() -> None:
    client = _everything()
    hashed = _find(_toolkit(client), query="#announcements", kind="chat")
    bare = _find(_toolkit(client), query="announcements", kind="chat")

    assert _ids(hashed) == _ids(bare)
    assert "C-ANN" in _ids(hashed)


def test_an_emoji_is_found_by_its_shortcode_and_carries_it_as_the_id() -> None:
    """``react_to_message`` takes the shortcode, so the shortcode is the id."""
    result = _find(_toolkit(_everything()), query=":ship-it:", kind="emoji")

    assert [match["id"] for match in result["matches"]] == ["ship-it"]
    assert result["matches"][0]["name"] == "ship-it"
    assert result["matches"][0]["alias_for"] == "rocket"


def test_a_usergroup_carries_the_handle_a_person_types() -> None:
    result = _find(_toolkit(_everything()), query="alice-reviewers", kind="usergroup")
    match = result["matches"][0]

    assert match["id"] == "S-ALICE"
    assert match["handle"] == "alice-reviewers"
    assert match["matched"] == "handle"


# -- nothing usable is dropped for being unusable ----------------------------


def test_a_deactivated_person_comes_back_marked() -> None:
    result = _find(_toolkit(_everything()), query="alice.former", kind="person")
    match = result["matches"][0]

    assert match["id"] == "U-ALICE-3"
    assert match["is_active"] is False


def test_an_archived_channel_comes_back_marked() -> None:
    result = _find(_toolkit(_everything()), query="announcements", kind="chat")
    by_id = {match["id"]: match for match in result["matches"]}

    assert by_id["C-ANN-OLD"]["is_active"] is False
    assert by_id["C-ANN-OLD"]["is_archived"] is True
    assert by_id["C-ANN"]["is_active"] is True
    assert by_id["C-ANN-HR"]["is_private"] is True


def test_a_disabled_usergroup_comes_back_marked() -> None:
    result = _find(_toolkit(_everything()), query="alice-old", kind="usergroup")

    assert [match["is_active"] for match in result["matches"]] == [False]


def test_a_live_match_outranks_a_dead_one_of_the_same_strength() -> None:
    """Ordering decides only what survives the cap, and it keeps the usable."""
    result = _find(_toolkit(_everything()), query="announcements", kind="chat")

    assert result["matches"][0]["id"] == "C-ANN"


# -- truncation --------------------------------------------------------------


def _many_people(count: int) -> list[dict[str, Any]]:
    return [
        {
            "id": f"U-{index:03d}",
            "name": f"alice{index:03d}",
            "deleted": False,
            "is_bot": False,
            "profile": {"real_name": f"Alice {index}", "display_name": ""},
        }
        for index in range(count)
    ]


def test_truncation_reports_the_true_count_rather_than_hiding_it() -> None:
    client = _FakeClient(users=_many_people(47))
    result = _find(_toolkit(client), query="alice", kind="person")

    assert result["truncated"] is True
    assert result["match_count"] == 47
    assert result["returned"] == 20
    assert len(result["matches"]) == 20
    assert "47" in result["coverage"]["truncation_note"]


def test_an_untruncated_answer_says_so() -> None:
    client = _FakeClient(users=_many_people(3))
    result = _find(_toolkit(client), query="alice", kind="person")

    assert result["truncated"] is False
    assert result["match_count"] == 3
    assert result["returned"] == 3
    assert result["coverage"]["status"] == "complete"


def test_truncation_makes_the_coverage_status_partial() -> None:
    client = _FakeClient(users=_many_people(47))
    result = _find(_toolkit(client), query="alice", kind="person")

    assert result["coverage"]["status"] == "partial"
    assert "more_matches_than_returned" in result["coverage"]["partial_reasons"]


# -- the operator's exclusion ------------------------------------------------


def test_a_conversation_the_operator_forbade_is_not_returned() -> None:
    client = _everything()
    toolkit = _toolkit(
        client,
        metadata=_metadata(slack_history_never_read=["C-ANN-HR"]),
    )
    result = _find(toolkit, query="announcements", kind="chat")

    assert "C-ANN-HR" not in _ids(result)
    assert "C-ANN" in _ids(result)
    assert result["coverage"]["excluded_by_operator"] == 1
    assert "operator" in result["coverage"]["exclusion_note"]


def test_the_exclusion_does_not_reach_people_or_emoji() -> None:
    """The list names conversations, so it removes conversations and nothing else."""
    client = _everything()
    toolkit = _toolkit(
        client,
        metadata=_metadata(slack_history_never_read=["U-ALICE-1", "S-ALICE"]),
    )
    result = _find(toolkit, query="alice")

    assert "U-ALICE-1" in _ids(result)
    assert "S-ALICE" in _ids(result)
    assert result["coverage"]["excluded_by_operator"] == 0


def test_an_empty_never_read_list_excludes_nothing() -> None:
    result = _find(_toolkit(_everything()), query="announcements", kind="chat")

    assert result["coverage"]["excluded_by_operator"] == 0
    assert len(_ids(result)) == 3


# -- the cache ---------------------------------------------------------------


def test_a_second_lookup_is_served_from_the_cache() -> None:
    client = _everything()
    toolkit = _toolkit(client)

    first = _find(toolkit, query="alice", kind="person")
    second = _find(toolkit, query="alice.tan", kind="person")

    assert client.methods_called() == ["users_list"]
    assert first["coverage"]["scanned"]["person"]["served_from_cache"] is False
    assert second["coverage"]["scanned"]["person"]["served_from_cache"] is True
    assert _ids(second) == ["U-ALICE-1"]


def test_the_cache_expires_and_the_directory_is_read_again() -> None:
    clock = {"now": 1000.0}
    client = _everything()
    toolkit = _toolkit(
        client,
        monotonic=lambda: clock["now"],
        cache_ttl_seconds=300.0,
    )

    _find(toolkit, query="alice", kind="person")
    clock["now"] += 301.0
    again = _find(toolkit, query="alice", kind="person")

    assert client.methods_called() == ["users_list", "users_list"]
    assert again["coverage"]["scanned"]["person"]["served_from_cache"] is False


def test_the_cache_is_kept_per_workspace() -> None:
    """A token is a directory. Two installs must never share one entry."""
    client = _everything()
    toolkit = _toolkit(client)

    _find(toolkit, query="alice", kind="person")
    toolkit._bot_token = "xoxb-a-different-install"
    toolkit._cache[("xoxb-a-different-install", "person")] = toolkit._cache[
        (_BOT_TOKEN, "person")
    ]

    assert ("xoxb-a-different-install", "person") in toolkit._cache
    assert (_BOT_TOKEN, "person") in toolkit._cache
    assert len(toolkit._cache) == 2


def test_pagination_walks_the_whole_directory() -> None:
    client = _FakeClient(
        user_pages=[_many_people(3), [_ALICE_ONE], [_ALICE_TWO]],
    )
    result = _find(_toolkit(client), query="alice", kind="person")

    assert client.methods_called() == ["users_list"] * 3
    assert result["match_count"] == 5
    assert result["coverage"]["scanned"]["person"]["directory_complete"] is True


# -- a missing grant narrows the answer rather than ending it ----------------


def test_one_refused_kind_does_not_take_the_others_down() -> None:
    client = _everything(refuse={"emoji_list": "missing_scope"})
    result = _find(_toolkit(client), query="alice")

    assert result["ok"] is True
    assert "U-ALICE-1" in _ids(result)
    assert result["coverage"]["kinds_unavailable"] == ["emoji"]
    assert "emoji" not in result["coverage"]["kinds_searched"]


def test_a_refused_kind_names_the_permission_it_wanted() -> None:
    client = _everything(refuse={"emoji_list": "missing_scope"})
    result = _find(_toolkit(client), query="alice")
    entry = result["coverage"]["unavailable"]["emoji"]

    assert entry["error"] == "missing_scope"
    assert entry["needs"] == ["emoji:read"]
    assert "emoji:read" in entry["detail"]
    assert "reinstall" in entry["detail"]


def test_the_named_permission_is_the_one_the_manifest_asks_for() -> None:
    """Derived from the scope policy, so a refusal cannot name a stale string."""
    assert scopes_for_kind("emoji") == ("emoji:read",)
    assert scopes_for_kind("usergroup") == ("usergroups:read",)
    assert scopes_for_kind("person") == ("users:read",)
    assert scopes_for_kind("chat") == ("channels:read", "groups:read")


def test_a_refused_kind_makes_the_answer_partial_and_says_why() -> None:
    client = _everything(refuse={"usergroups_list": "missing_scope"})
    result = _find(_toolkit(client), query="alice")

    assert result["coverage"]["status"] == "partial"
    assert "kinds_not_granted_to_this_app" in result["coverage"]["partial_reasons"]
    assert "some_kinds_unavailable" in result["coverage"]["warnings"]
    assert "usergroup" in result["coverage"]["unavailable_note"]


def test_an_explicitly_named_unavailable_kind_refuses() -> None:
    """They asked for precisely the thing that could not be read."""
    client = _everything(refuse={"emoji_list": "missing_scope"})
    result = _find(_toolkit(client), query="ship-it", kind="emoji")

    assert result["ok"] is False
    assert result["error"] == "missing_scope"
    assert result["matches"] == []
    assert "emoji:read" in result["detail"]


def test_every_kind_unavailable_refuses_rather_than_answering_empty() -> None:
    client = _everything(
        refuse={
            "users_list": "missing_scope",
            "conversations_list": "missing_scope",
            "usergroups_list": "missing_scope",
            "emoji_list": "missing_scope",
        }
    )
    result = _find(_toolkit(client), query="alice")

    assert result["ok"] is False
    assert result["error"] == "missing_scope"
    assert result["matches"] == []
    for scope in ("users:read", "channels:read", "usergroups:read", "emoji:read"):
        assert scope in result["detail"]


def test_disagreeing_refusals_do_not_invent_a_winner() -> None:
    client = _everything(
        refuse={
            "users_list": "missing_scope",
            "conversations_list": "ratelimited",
            "usergroups_list": "missing_scope",
            "emoji_list": "missing_scope",
        }
    )
    result = _find(_toolkit(client), query="alice")

    assert result["ok"] is False
    assert result["error"] == "slack_lookup_failed"
    assert "ratelimited" in result["detail"]


def test_a_searched_kind_that_found_nothing_is_not_reported_as_unavailable() -> None:
    """An empty matches list must mean the search ran; this is the other half."""
    result = _find(_toolkit(_everything()), query="nobody-by-that-name")

    assert result["ok"] is True
    assert result["matches"] == []
    assert result["match_count"] == 0
    assert result["coverage"]["kinds_unavailable"] == []
    assert result["coverage"]["kinds_searched"] == list(SLACK_KINDS)
    assert result["coverage"]["status"] == "complete"


# -- the metadata provider fails closed --------------------------------------


def test_a_request_that_is_not_a_mapping_is_refused() -> None:
    assert slack_find_request_metadata(None) == {}
    assert slack_find_request_metadata("slack") == {}
    assert slack_find_request_metadata(["slack_team_id"]) == {}


def test_a_slack_request_passes_its_metadata_through() -> None:
    metadata = _metadata()

    assert slack_find_request_metadata(metadata) == metadata


def test_a_provider_that_raises_serves_nothing() -> None:
    client = _everything()

    def _explode() -> dict[str, Any]:
        raise RuntimeError("the request context is gone")

    result = _find(_toolkit(client, provider=_explode), query="alice")

    assert result["ok"] is False
    assert result["error"] == "slack_lookup_unavailable_for_this_turn"
    assert client.calls == []


def test_a_provider_answering_nothing_serves_nothing() -> None:
    client = _everything()
    result = _find(_toolkit(client, provider=dict), query="alice")

    assert result["ok"] is False
    assert result["error"] == "slack_lookup_unavailable_for_this_turn"
    assert client.calls == []


# -- the card ----------------------------------------------------------------


def test_the_card_is_one_tool_named_for_what_it_does() -> None:
    tools = SlackDirectoryToolkit(metadata=_metadata()).get_tools()

    assert len(tools) == 1
    assert tools[0].card.name == "find_by_name"


def test_the_card_declares_this_platform_s_kinds_and_nothing_else() -> None:
    card = SlackDirectoryToolkit(metadata=_metadata()).get_tools()[0].card
    properties = card.input_params["properties"]

    assert card.input_params["required"] == ["query"]
    assert properties["kind"]["enum"] == list(SLACK_KINDS)
    assert "kind" not in card.input_params["required"]


def test_the_card_tells_the_model_what_it_must_not_assume() -> None:
    """The description is the contract; these four clauses are the contract."""
    card = SlackDirectoryToolkit(metadata=_metadata()).get_tools()[0].card
    description = card.description

    assert "chooses between none of them" in description
    assert "coverage" in description
    assert "not cheap" in description
    assert "never to be read" in description
