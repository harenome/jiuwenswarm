# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Which Slack install a model-facing tool call is served by.

A bot token is issued per installation and so is every id it can see: a user
id, a conversation id and a message ts name something in one workspace and
nothing in another. Until ``channels.slack`` could hold a list, one token was
the whole of the answer and there was nothing to get wrong. These tests pin
what replaces it.

Four things are asserted, in this order:

* a deployment writing the flat mapping reads exactly as it did, spending no
  extra call and being asked no extra question;
* a request is served by the install it arrived from;
* an install that cannot be settled is refused rather than served from
  whichever block happens to hold a token;
* a membership comparison whose two sides may be in different installs is
  refused rather than computed.
"""

from __future__ import annotations

import asyncio
import json
from collections import defaultdict
from typing import Any

import pytest

import jiuwenswarm.common.config as config_module
from jiuwenswarm.agents.harness.common.tools.slack_bookmarks import (
    SlackBookmarkToolkit,
)
from jiuwenswarm.agents.harness.common.tools.slack_history import (
    SlackHistoryToolkit,
    SlackWorkspaceClients,
)
from jiuwenswarm.agents.harness.common.tools.slack_pins import SlackPinToolkit
from jiuwenswarm.agents.harness.common.tools.slack_reactions import (
    SlackReactionToolkit,
)
from jiuwenswarm.common.slack_history_policy import (
    HISTORY_MEMBERS,
    METADATA_EXEMPT_MEMBERS_KEY,
    METADATA_NEVER_READ_KEY,
    METADATA_POLICY_KEY,
    METADATA_TEAM_KEY,
)

_TEAM_A = "T0AAA"
_TEAM_B = "T0BBB"
_TOKEN_A = "xoxb-alpha"
_TOKEN_B = "xoxb-bravo"
_ORIGIN = "C-ORIGIN"
_TARGET = "C-TARGET"
_ALICE = "U-ALICE"
_TS = "1758123456.123456"

FLAT = {"bot_token": _TOKEN_A}
TWO = {"workspaces": [{"bot_token": _TOKEN_A}, {"bot_token": _TOKEN_B}]}


class _Install:
    """One fake Slack installation, answering as the token it was built for.

    Every call is recorded, so a test can say *which install this call reached*
    rather than inferring it from a result that looks the same either way.
    """

    def __init__(self, token: str, team: str, *, members: set[str] | None = None):
        self.token = token
        self.team = team
        self.members = members if members is not None else {_ALICE}
        self.calls: dict[str, list[dict[str, Any]]] = defaultdict(list)

    def _record(self, method: str, kwargs: dict[str, Any]) -> None:
        self.calls[method].append(kwargs)

    @property
    def made(self) -> list[str]:
        return sorted(self.calls)

    async def auth_test(self, **kwargs: Any) -> dict[str, Any]:
        self._record("auth_test", kwargs)
        return {
            "ok": True,
            "team_id": self.team,
            "user_id": f"U-BOT-{self.team}",
            "bot_id": f"B-{self.team}",
            "url": f"https://{self.team.lower()}.slack.com/",
        }

    async def conversations_info(self, **kwargs: Any) -> dict[str, Any]:
        self._record("conversations_info", kwargs)
        return {
            "ok": True,
            "channel": {
                "id": str(kwargs["channel"]),
                "is_channel": True,
                "is_private": True,
                "name": f"room-in-{self.team}",
            },
        }

    async def conversations_members(self, **kwargs: Any) -> dict[str, Any]:
        self._record("conversations_members", kwargs)
        return {"ok": True, "members": sorted(self.members)}

    async def conversations_history(self, **kwargs: Any) -> dict[str, Any]:
        self._record("conversations_history", kwargs)
        return {
            "ok": True,
            "messages": [
                {"ts": "199999.000100", "user": _ALICE, "text": f"hello {self.team}"}
            ],
        }

    async def conversations_replies(self, **kwargs: Any) -> dict[str, Any]:
        self._record("conversations_replies", kwargs)
        return {"ok": True, "messages": []}

    async def pins_add(self, **kwargs: Any) -> dict[str, Any]:
        self._record("pins_add", kwargs)
        return {"ok": True}

    async def reactions_add(self, **kwargs: Any) -> dict[str, Any]:
        self._record("reactions_add", kwargs)
        return {"ok": True}

    async def bookmarks_add(self, **kwargs: Any) -> dict[str, Any]:
        self._record("bookmarks_add", kwargs)
        return {"ok": True, "bookmark": {"id": "Bk1", "title": "t", "link": "l"}}


class _Slack:
    """Every configured installation, and the factory that hands one out."""

    def __init__(self, **by_token: str) -> None:
        self.installs = {
            token: _Install(token, team) for token, team in by_token.items()
        }
        self.built: list[str] = []

    def __call__(self, token: str) -> _Install:
        self.built.append(token)
        install = self.installs.get(token)
        if install is None:
            raise AssertionError(f"a client was built for an unknown token: {token!r}")
        return install

    def resolver(self) -> SlackWorkspaceClients:
        return SlackWorkspaceClients(client_factory=self)

    def __getitem__(self, team: str) -> _Install:
        for install in self.installs.values():
            if install.team == team:
                return install
        raise KeyError(team)


@pytest.fixture
def two_installs() -> _Slack:
    return _Slack(**{_TOKEN_A: _TEAM_A, _TOKEN_B: _TEAM_B})


def _configured(monkeypatch: pytest.MonkeyPatch, slack: Any) -> None:
    monkeypatch.setattr(
        config_module, "get_config", lambda: {"channels": {"slack": slack}}
    )


def _metadata(team: str | None = None, **overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "slack_channel_id": _ORIGIN,
        "slack_channel_type": "channel",
        "slack_user_id": _ALICE,
        "message_ts": _TS,
        METADATA_POLICY_KEY: HISTORY_MEMBERS,
        METADATA_NEVER_READ_KEY: [],
        METADATA_EXEMPT_MEMBERS_KEY: [],
    }
    if team is not None:
        base[METADATA_TEAM_KEY] = team
    base.update(overrides)
    return base


def _history(resolver: SlackWorkspaceClients, **meta: Any) -> SlackHistoryToolkit:
    return SlackHistoryToolkit(
        metadata=_metadata(**meta),
        workspaces=resolver,
        now=lambda: 200_000.0,
        max_user_lookups=0,
    )


def _read(toolkit: SlackHistoryToolkit, **kwargs: Any) -> dict[str, Any]:
    return json.loads(asyncio.run(toolkit.read_slack_conversation(**kwargs)))


# ── 1. the flat mapping, which is what the live deployment writes ────────────


def test_one_configured_install_is_served_without_a_question_being_asked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The case a multi-workspace change must not move.

    No ``auth.test`` is spent identifying the token and no team is compared:
    one install is one Socket Mode connection, so the only events that can
    arrive are its own. The read costs exactly what it cost before any of this
    existed.
    """
    slack = _Slack(**{_TOKEN_A: _TEAM_A})
    _configured(monkeypatch, FLAT)

    resolver = slack.resolver()
    result = _read(_history(resolver))

    assert result["ok"] is True
    assert slack.built == [_TOKEN_A]
    # Nothing was learned about the token, because nothing needed to be. The
    # one ``auth.test`` the read does spend is the toolkit's own -- which bot
    # user it is, so it can recognise its own messages -- and predates this.
    assert resolver.team_of_token(_TOKEN_A) == ""
    assert len(slack[_TEAM_A].calls["auth_test"]) == 1


def test_a_flat_mapping_serves_a_request_from_any_team_it_is_stamped_with(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Including one that names no team at all, which is every request today.

    The connector has stamped ``slack_team_id`` for some time, and a cron run
    stamps none. Neither may start being refused by a deployment that has one
    install.
    """
    slack = _Slack(**{_TOKEN_A: _TEAM_A})
    _configured(monkeypatch, FLAT)

    for team in (None, "", _TEAM_A, "T-SOMETHING-ELSE"):
        assert _read(_history(slack.resolver(), team=team))["ok"] is True


# ── 2. the install a request arrived from ────────────────────────────────────


def test_a_request_reaches_the_install_it_arrived_from(
    monkeypatch: pytest.MonkeyPatch, two_installs: _Slack
) -> None:
    """The read lands in B's workspace, and A's is never called for history."""
    _configured(monkeypatch, TWO)
    resolver = two_installs.resolver()

    assert _read(_history(resolver, team=_TEAM_B))["ok"] is True

    assert "conversations_history" in two_installs[_TEAM_B].calls
    assert "conversations_history" not in two_installs[_TEAM_A].calls

    # And the other way round, through the same resolver, so the second
    # request is served by a different client than the first.
    assert _read(_history(resolver, team=_TEAM_A))["ok"] is True
    assert "conversations_history" in two_installs[_TEAM_A].calls


def test_a_token_is_identified_once_and_the_answer_is_reused(
    monkeypatch: pytest.MonkeyPatch, two_installs: _Slack
) -> None:
    """A token belongs to the install that minted it, so the answer keeps.

    What is *not* cached is the config: it is read again on every call, which
    is the no-restart contract ``slack_config`` already had.
    """
    _configured(monkeypatch, TWO)
    resolver = two_installs.resolver()

    for _ in range(4):
        assert _read(_history(resolver, team=_TEAM_B))["ok"] is True

    # A is never served and never read from, so every call it saw is an
    # identification: one, for four requests.
    assert two_installs[_TEAM_A].made == ["auth_test"]
    assert len(two_installs[_TEAM_A].calls["auth_test"]) == 1
    # B is identified once too. Its remaining four are the toolkit's own
    # bot-identity read, one per request, which is not this resolution.
    assert len(two_installs[_TEAM_B].calls["auth_test"]) == 5


def test_a_token_swapped_in_the_config_is_obeyed_without_a_restart(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The identity cache is keyed by the token, never by the team.

    Keyed by team, an operator repointing a workspace at a freshly issued
    token would keep being served by the old one until the process was
    restarted. Keyed by token, the new string is simply a key nobody has
    answered for yet.
    """
    replacement = "xoxb-alpha-reissued"
    slack = _Slack(
        **{_TOKEN_A: _TEAM_A, _TOKEN_B: _TEAM_B, replacement: _TEAM_A}
    )
    _configured(monkeypatch, TWO)
    resolver = slack.resolver()

    assert _read(_history(resolver, team=_TEAM_A))["ok"] is True
    assert "conversations_history" in slack.installs[_TOKEN_A].calls

    _configured(
        monkeypatch,
        {"workspaces": [{"bot_token": replacement}, {"bot_token": _TOKEN_B}]},
    )
    assert _read(_history(resolver, team=_TEAM_A))["ok"] is True
    assert "conversations_history" in slack.installs[replacement].calls


# ── 3. an install that cannot be settled ─────────────────────────────────────


def test_a_team_no_configured_install_answers_to_is_refused(
    monkeypatch: pytest.MonkeyPatch, two_installs: _Slack
) -> None:
    """Serving it from the first block is the failure this all exists to stop."""
    _configured(monkeypatch, TWO)

    result = _read(_history(two_installs.resolver(), team="T0CCC"))

    assert result["ok"] is False
    assert result["error"] == "slack_workspace_unknown"
    assert "T0CCC" in result["detail"]
    for install in two_installs.installs.values():
        assert "conversations_history" not in install.calls


@pytest.mark.parametrize("team", [None, ""])
def test_a_request_naming_no_install_is_refused_once_there_are_several(
    monkeypatch: pytest.MonkeyPatch, two_installs: _Slack, team: str | None
) -> None:
    """Which install it belongs to is unknowable, and every fallback is a guess."""
    _configured(monkeypatch, TWO)

    result = _read(_history(two_installs.resolver(), team=team))

    assert result["ok"] is False
    assert result["error"] == "slack_request_names_no_workspace"
    assert METADATA_TEAM_KEY in result["detail"]


def test_a_cron_run_is_served_by_the_install_its_session_names(
    monkeypatch: pytest.MonkeyPatch, two_installs: _Slack
) -> None:
    """A scheduled run stamps no team, and its session id carries one.

    Without this, every scheduled Slack read in a multi-workspace deployment
    would be refused for naming no install.
    """
    _configured(monkeypatch, TWO)
    toolkit = SlackHistoryToolkit(
        metadata=_metadata(),
        session_id=f"slack_{_TEAM_B}_{_ORIGIN}",
        workspaces=two_installs.resolver(),
        now=lambda: 200_000.0,
        max_user_lookups=0,
    )

    assert _read(toolkit)["ok"] is True
    assert "conversations_history" in two_installs[_TEAM_B].calls


def test_a_write_into_an_unresolvable_install_never_reaches_slack(
    monkeypatch: pytest.MonkeyPatch, two_installs: _Slack
) -> None:
    """The three writing tools fail closed, which for a write is the whole point.

    A pin, a reaction or a bookmark left in the wrong workspace is a mark in a
    room nobody asked about, and no later read takes it back.
    """
    _configured(monkeypatch, TWO)
    resolver = two_installs.resolver()
    metadata = _metadata(team="T0CCC")

    pinned = json.loads(
        asyncio.run(
            SlackPinToolkit(
                metadata=metadata, workspaces=resolver
            ).pin_message(_TS)
        )
    )
    reacted = json.loads(
        asyncio.run(
            SlackReactionToolkit(
                metadata=metadata, workspaces=resolver
            ).react_to_message("eyes")
        )
    )
    bookmarked = json.loads(
        asyncio.run(
            SlackBookmarkToolkit(
                metadata=metadata, workspaces=resolver
            ).write_bookmark(
                action="add", title="t", url="https://example.invalid/"
            )
        )
    )

    for result in (pinned, reacted, bookmarked):
        assert result["ok"] is False
        assert result["error"] == "slack_workspace_unknown"
    for install in two_installs.installs.values():
        assert install.made in ([], ["auth_test"])


# ── 4. the membership rule across an installation boundary ───────────────────


def test_a_membership_comparison_is_refused_where_the_install_is_unestablished(
    monkeypatch: pytest.MonkeyPatch, two_installs: _Slack
) -> None:
    """The rule compares user ids, and a user id belongs to one installation.

    Here the toolkit was handed a client directly, so which install it talks to
    is not something this toolkit can learn, while the asker id on the request
    was stamped by a connector in a named one. Comparing the two answers a
    question nobody asked, and can refuse a read that was fine as easily as
    allow one that was not.
    """
    _configured(monkeypatch, TWO)

    toolkit = SlackHistoryToolkit(
        metadata=_metadata(team=_TEAM_A),
        client=two_installs[_TEAM_A],
        workspaces=two_installs.resolver(),
        now=lambda: 200_000.0,
        max_user_lookups=0,
    )
    result = _read(toolkit, chat_id=_TARGET)

    assert result["ok"] is False
    assert result["error"] == "history_cross_workspace_comparison"
    assert "user id" in result["detail"]
    # Refused before a single membership was fetched, so the ids of one
    # workspace were never enumerated in order to be compared with another's.
    assert "conversations_members" not in two_installs[_TEAM_A].calls


def test_the_same_toolkit_still_compares_freely_where_one_install_is_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The guard is silent where there is no boundary to cross.

    Same injected client, same request: the only difference is that one
    install is configured rather than two, and the membership rule runs to its
    own answer.
    """
    slack = _Slack(**{_TOKEN_A: _TEAM_A})
    _configured(monkeypatch, FLAT)

    toolkit = SlackHistoryToolkit(
        metadata=_metadata(team=_TEAM_A),
        client=slack[_TEAM_A],
        workspaces=slack.resolver(),
        now=lambda: 200_000.0,
        max_user_lookups=0,
    )
    result = _read(toolkit, chat_id=_TARGET)

    assert result["ok"] is True
    assert "conversations_members" in slack[_TEAM_A].calls


def test_a_member_list_read_in_one_install_never_answers_for_another(
    monkeypatch: pytest.MonkeyPatch, two_installs: _Slack
) -> None:
    """The member cache is keyed by the install as well as the conversation.

    Keyed by the conversation alone it was a fact about Slack, which is true
    only inside one installation: a conversation id names a room in the install
    whose token read it and nothing at all in another. An entry read under one
    token deciding a membership gate for a request served by another is a
    member list from workspace A authorising a read in workspace B.
    """
    _configured(monkeypatch, TWO)
    resolver = two_installs.resolver()

    assert _read(_history(resolver, team=_TEAM_A), chat_id=_TARGET)["ok"] is True
    assert _read(_history(resolver, team=_TEAM_B), chat_id=_TARGET)["ok"] is True

    # Both installs were asked, rather than the second being answered out of
    # the first one's entry.
    for team in (_TEAM_A, _TEAM_B):
        read = [
            call["channel"]
            for call in two_installs[team].calls["conversations_members"]
        ]
        assert sorted(set(read)) == [_ORIGIN, _TARGET]
