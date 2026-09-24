# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The gate on reading a Slack conversation other than the one asked in.

The rule under test, for a request made in conversation ``S`` about conversation
``T``, is

    (members(S) - exempt) subset-of members(T)

read as *nobody in S learns anything they could not already learn*, with the
asker added back after the exemption, a relaxation for public *targets* only,
and a refusal on anything that cannot be established.

Every test past the first section is reachable only by a deployment that widened
``channels.slack.history`` past ``origin``. The first section asserts the other
half of that: at ``disabled`` and at ``origin`` the tool has no target
parameter, takes no extra API call, and returns what it returned before any of
this existed.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections import defaultdict
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

import pytest

import jiuwenswarm.common.config as config_module
from jiuwenswarm.agents.harness.common.tools import slack_history
from jiuwenswarm.agents.harness.common.tools.slack_history import SlackHistoryToolkit
from jiuwenswarm.common.slack_history_policy import (
    HISTORY_DISABLED,
    HISTORY_MEMBERS,
    HISTORY_OPEN,
    HISTORY_ORIGIN,
    HISTORY_VISIBLE,
    METADATA_EXEMPT_MEMBERS_KEY,
    METADATA_NEVER_READ_KEY,
    METADATA_ORIGIN_KEY,
    METADATA_POLICY_KEY,
    ORIGIN_CRON_JOB,
)

# Attached to the module the class actually lives in rather than to a literal.
# A module relocated behind a ``sys.modules`` alias goes on importing under the
# old name while emitting under the new one, and a test listening to the dead
# name would watch a logger nothing writes to and pass on an empty list.
_LOGGER_NAME = SlackHistoryToolkit.__module__

_ORIGIN = "C-ORIGIN"
_TARGET = "C-TARGET"
_PUBLIC = "C-PUBLIC"
_DM = "D-ALICE"
_ALICE = "U-ALICE"
_BOB = "U-BOB"
_BOT = "U-BOT"
_INTEGRATION = "B-PAGERDUTY"
# The real incident's shape: a roomful of ordinary people, one guest among
# them, and one application. Named apart because ``visible`` asks Slack what
# each of them is and gets three different answers.
_CROWD = tuple(f"U-CROWD{index:02d}" for index in range(11))
_GUEST = "U-GUEST"
_SINGLE_CHANNEL_GUEST = "U-SCGUEST"
_APP = "U-APPBOT"
_MPIM = "C-MPIM"


@contextmanager
def _captured(logger_name: str) -> Iterator[list[logging.LogRecord]]:
    """Records emitted by one logger, taken off that logger directly.

    Not ``caplog``: the handler pytest installs sits on the root logger, so what
    it sees depends on whether this package's loggers propagate -- which is a
    property of whatever configured logging first, not of the code under test.
    """
    records: list[logging.LogRecord] = []
    logger = logging.getLogger(logger_name)
    handler = logging.Handler()
    handler.emit = records.append  # type: ignore[method-assign]
    previous_level = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    try:
        yield records
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous_level)


def _warnings(records: list[logging.LogRecord]) -> list[str]:
    return [r.getMessage() for r in records if r.levelno >= logging.WARNING]


class _Workspace:
    """A fake Slack with a membership list and one message per conversation.

    Deliberately not a scripted response queue. The gate makes a different
    number of calls depending on which branch refuses, and a queue would make
    every test assert the call count by accident.
    """

    def __init__(
        self,
        *,
        members: "dict[str, set[str]] | None" = None,
        public: "set[str] | None" = None,
        mpim: "set[str] | None" = None,
        guests: "set[str] | None" = None,
        single_channel_guests: "set[str] | None" = None,
        applications: "set[str] | None" = None,
        users: "dict[str, dict[str, Any]] | None" = None,
        fail: "dict[str, str] | None" = None,
        fail_members: "dict[str, str] | None" = None,
        members_pages: "dict[str, list[dict[str, Any]]] | None" = None,
    ) -> None:
        self.members = members or {}
        self.public = public or set()
        self.mpim = mpim or set()
        # What ``users.info`` says about a person. The three named sets are the
        # three answers that matter to the gate; ``users`` is the escape hatch
        # for a record that answers something else, or nothing.
        self.guests = guests or set()
        self.single_channel_guests = single_channel_guests or set()
        self.applications = applications or set()
        self.users = users or {}
        self.fail = fail or {}
        # Keyed by conversation rather than by method, so a test can decline the
        # membership of one room and not the other. The gate reads the source's
        # first, and a whole-method failure would never reach the target's.
        self.fail_members = fail_members or {}
        self.members_pages = members_pages or {}
        self.calls: dict[str, list[dict[str, Any]]] = defaultdict(list)

    def _record(self, method: str, kwargs: dict[str, Any]) -> None:
        self.calls[method].append(kwargs)
        if method in self.fail:
            raise _FakeSlackError(self.fail[method])
        if method == "conversations_members":
            error = self.fail_members.get(str(kwargs.get("channel") or ""))
            if error:
                raise _FakeSlackError(error)

    async def auth_test(self, **kwargs: Any) -> dict[str, Any]:
        self._record("auth_test", kwargs)
        return {"user_id": _BOT, "bot_id": "B-SELF", "url": "https://x.slack.com/"}

    async def conversations_info(self, **kwargs: Any) -> dict[str, Any]:
        self._record("conversations_info", kwargs)
        channel = str(kwargs["channel"])
        if channel.startswith("D"):
            return {"channel": {"id": channel, "is_im": True, "is_private": True}}
        if channel in self.mpim:
            return {"channel": {"id": channel, "is_mpim": True, "is_private": True}}
        return {
            "channel": {
                "id": channel,
                "is_channel": True,
                "is_private": channel not in self.public,
            }
        }

    async def users_info(self, **kwargs: Any) -> dict[str, Any]:
        self._record("users_info", kwargs)
        user_id = str(kwargs["user"])
        if user_id in self.users:
            return {"ok": True, "user": self.users[user_id]}
        if user_id in self.applications:
            # What Slack sends for an app: it says the account is a bot and
            # says nothing at all about guest status, which is the shape the
            # gate has to be right about.
            return {"ok": True, "user": {"id": user_id, "is_bot": True}}
        return {
            "ok": True,
            "user": {
                "id": user_id,
                "is_bot": False,
                "is_restricted": (
                    user_id in self.guests or user_id in self.single_channel_guests
                ),
                "is_ultra_restricted": user_id in self.single_channel_guests,
            },
        }

    async def conversations_members(self, **kwargs: Any) -> dict[str, Any]:
        self._record("conversations_members", kwargs)
        channel = str(kwargs["channel"])
        scripted = self.members_pages.get(channel)
        if scripted is not None:
            cursor = str(kwargs.get("cursor") or "")
            return scripted[0 if not cursor else int(cursor)]
        return {"members": sorted(self.members.get(channel, set()))}

    async def conversations_history(self, **kwargs: Any) -> dict[str, Any]:
        self._record("conversations_history", kwargs)
        channel = str(kwargs["channel"])
        return {
            "messages": [
                {"ts": "199999.000100", "user": _ALICE, "text": f"hello from {channel}"}
            ]
        }

    async def conversations_replies(self, **kwargs: Any) -> dict[str, Any]:
        self._record("conversations_replies", kwargs)
        return {"messages": []}


class _FakeResponse:
    def __init__(self, error: str) -> None:
        self.status_code = 500
        self.data = {"ok": False, "error": error}
        self.headers: dict[str, Any] = {}


class _FakeSlackError(Exception):
    def __init__(self, error: str) -> None:
        super().__init__("sanitized fake failure")
        self.response = _FakeResponse(error)


@pytest.fixture(autouse=True)
def _config(monkeypatch: pytest.MonkeyPatch) -> None:
    """The bounds this toolkit reads. Never the policy: that arrives stamped."""
    monkeypatch.setattr(
        config_module,
        "get_config",
        lambda: {"channels": {"slack": {"bot_token": "xoxb-config-secret"}}},
    )


def _metadata(policy: str = HISTORY_MEMBERS, **overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "slack_channel_id": _ORIGIN,
        "slack_channel_type": "channel",
        "slack_user_id": _ALICE,
        METADATA_POLICY_KEY: policy,
        METADATA_NEVER_READ_KEY: [],
        METADATA_EXEMPT_MEMBERS_KEY: [],
    }
    base.update(overrides)
    return base


def _toolkit(
    workspace: _Workspace,
    policy: str = HISTORY_MEMBERS,
    *,
    lookups: int = 0,
    monotonic: "Any | None" = None,
    **metadata: Any,
) -> SlackHistoryToolkit:
    """One toolkit, with the directory-lookup budget an argument.

    Zero by default, which is what every test written before ``visible``
    existed wants: no display name is resolved, so a ``users.info`` call in the
    record is the gate's and nobody else's. ``visible`` needs a real budget,
    because that same key is what bounds the gate's own lookups.
    """
    clock = {"monotonic": monotonic} if monotonic is not None else {}
    return SlackHistoryToolkit(
        metadata=_metadata(policy, **metadata),
        client=workspace,
        now=lambda: 200_000.0,
        max_user_lookups=lookups,
        **clock,
    )


def _read(toolkit: SlackHistoryToolkit, **kwargs: Any) -> dict[str, Any]:
    return json.loads(asyncio.run(toolkit.read_slack_conversation(**kwargs)))


def _card(toolkit: SlackHistoryToolkit) -> Any:
    tool = toolkit.get_tools()[0]
    return getattr(tool, "card", None) or tool._card


# ── 1. disabled and origin are what shipped ──────────────────────────────────


@pytest.mark.parametrize("policy", [HISTORY_DISABLED, HISTORY_ORIGIN])
def test_the_narrow_words_declare_no_target_parameter(policy: str) -> None:
    """The card is byte-identical, so the model has nothing to reach for.

    The parameter is not the permission -- the gate is -- but a card that
    declares it in a deployment that cannot use it is a model repeatedly offered
    a refusal, and a description that mentions a capability nobody has.
    """
    card = _card(_toolkit(_Workspace(), policy))
    assert card.name == "read_slack_conversation"
    assert set(card.input_params["properties"]) == {
        "hours",
        "all_history",
        "include_threads",
        "max_messages",
        # The two per-message bounds. They name what one record keeps of a
        # list and reach no further than the conversation the policy settled,
        # so they are declared under every word for the reason ts is.
        "max_files_per_message",
        "max_reactors_per_reaction",
        "before_ts",
        "after_ts",
        "after_iso_utc",
        # Declared under every word, unlike chat_id. It names a position
        # inside the conversation the policy has already settled rather than
        # naming a conversation, and a thread is a subset of its channel, so
        # there is no audience for it to widen.
        "ts",
    }
    assert "The conversation cannot be selected by the model." in card.description


def test_each_widening_word_states_its_own_rule_on_the_card() -> None:
    """The card has to say which of the three reads this deployment allows.

    The rule is not one rule. ``members`` asks who is in the target,
    ``visible`` asks who could reach it, and ``open`` asks nothing at all
    about a public channel. One sentence for all three is wrong for two of
    them, and it is wrong in the direction that costs the deployment the word
    it configured: a card stating the membership rule tells a ``visible``
    model that a read it is allowed will be refused, so the model does not
    make the call.

    Asserted as *three different texts* and as *the texts the module holds*,
    rather than as phrases. A reword of any of them keeps this test green; a
    card that goes back to describing one behaviour does not.
    """
    words = (HISTORY_MEMBERS, HISTORY_VISIBLE, HISTORY_OPEN)
    cards = {word: _card(_toolkit(_Workspace(), word)) for word in words}
    bodies = {word: card.description for word, card in cards.items()}
    arguments = {
        word: card.input_params["properties"]["chat_id"]["description"]
        for word, card in cards.items()
    }

    assert len(set(bodies.values())) == len(words)
    assert len(set(arguments.values())) == len(words)

    for word in words:
        for other in words:
            present = other == word
            assert (slack_history._TARGET_RULE_PROSE[other] in bodies[word]) is present
            assert (
                slack_history._TARGET_ARGUMENT_PROSE[other] in arguments[word]
            ) is present


def test_one_call_spends_the_lookup_budget_once_between_its_two_spenders() -> None:
    """``history_max_user_lookups`` is one budget, not one budget each.

    Two places call ``users.info`` on a single read: the ``visible`` gate,
    asking whether a person missing from a public target could have reached it
    anyway, and the pass that turns author ids into display names. Each used to
    take the whole key, so a deployment that wrote 20 could spend 40.

    The gate draws first and names take the remainder, because the two run out
    differently. Out of budget at the gate raises and the read is refused; out
    of budget in the name pass warns and leaves an author as an id. The
    starvable one is the one that only costs presentation.
    """
    workspace = _Workspace(
        members={
            _ORIGIN: {_ALICE, _BOT, _CROWD[0], _CROWD[1]},
            _PUBLIC: {_ALICE, _BOT},
        },
        public={_PUBLIC},
        # A real display name, so a resolved author and an unresolved one are
        # told apart. Without it the fallback is the id and both look alike.
        users={
            _ALICE: {
                "id": _ALICE,
                "is_bot": False,
                "profile": {"display_name": "Alice A"},
            }
        },
    )
    budget = 2
    result = _read(
        _toolkit(workspace, HISTORY_VISIBLE, lookups=budget), chat_id=_PUBLIC
    )

    assert result["ok"] is True
    # Two people were missing from the public target and both were asked about.
    # That is the whole budget, so nothing is left to spend on names.
    assert len(workspace.calls["users_info"]) == budget
    assert {call["user"] for call in workspace.calls["users_info"]} == {
        _CROWD[0],
        _CROWD[1],
    }
    assert result["messages"][0]["author_name"] == _ALICE
    assert "author_name_lookup_limit" in result["coverage"]["warnings"]


def test_origin_reads_its_own_conversation_with_no_gate_call_at_all() -> None:
    """Not merely satisfied: not consulted.

    ``origin`` is the setting whose correctness does not depend on our own gate
    being right, and it is the cheap one. Both of those are false if it quietly
    fetches a member list, so no membership is read here and none is read for
    the conversation either.

    The conversation's own record is fetched, and after the scan rather than
    before it: it supplies the name the answer refers to the room by, which is
    presentation and no part of deciding what may be read. A membership list is
    the thing the gate would need and the thing that is absent.
    """
    workspace = _Workspace(members={_ORIGIN: {_ALICE, _BOT}})
    result = _read(_toolkit(workspace, HISTORY_ORIGIN))

    assert result["ok"] is True
    assert result["chat_id"] == _ORIGIN
    assert result["chat_type"] == "channel"
    assert "conversations_members" not in workspace.calls
    assert [call["channel"] for call in workspace.calls["conversations_info"]] == [
        _ORIGIN
    ]


def test_disabled_refuses_before_anything_is_read() -> None:
    workspace = _Workspace(members={_ORIGIN: {_ALICE, _BOT}})
    result = _read(_toolkit(workspace, HISTORY_DISABLED))

    assert result["ok"] is False
    assert result["error"] == "history_policy_forbids_history"
    assert result["messages"] == []
    assert workspace.calls == {}


def test_origin_refuses_a_named_target_without_reading_any_membership() -> None:
    """The cheapest refusal, and it costs no call to reach.

    A target under ``origin`` is settled from the stamped word alone, so the
    branch that says no is the one that has read nothing.
    """
    workspace = _Workspace(members={_ORIGIN: {_ALICE, _BOT}, _TARGET: {_ALICE, _BOT}})
    result = _read(_toolkit(workspace, HISTORY_ORIGIN), chat_id=_TARGET)

    assert result["ok"] is False
    assert result["error"] == "history_policy_forbids_other_conversations"
    assert result["chat_id"] == _ORIGIN
    assert workspace.calls == {}


def test_naming_the_originating_conversation_is_the_same_as_naming_nothing() -> None:
    """A conversation reading itself is not a gated read.

    It matters because a model told it may name a conversation will sometimes
    name the one it is already in. Showing a room its own scrollback discloses
    to exactly the people already there, so there is nothing for the subset rule
    to establish.
    """
    workspace = _Workspace(members={_ORIGIN: {_ALICE, _BOT}})
    result = _read(_toolkit(workspace, HISTORY_MEMBERS), chat_id=_ORIGIN)

    assert result["ok"] is True
    assert result["chat_id"] == _ORIGIN
    assert "conversations_members" not in workspace.calls


# ── 2. The subset rule ───────────────────────────────────────────────────────


def test_the_wider_words_declare_a_target_that_names_no_api_and_no_scope() -> None:
    """The card describes a capability, not a mechanism.

    A model cannot act on "the scope allows it" or on the name of a Slack
    endpoint; it can act on what the read is for and when it is refused.
    """
    card = _card(_toolkit(_Workspace(), HISTORY_MEMBERS))
    assert "chat_id" in card.input_params["properties"]
    text = card.description + card.input_params["properties"]["chat_id"]["description"]
    for forbidden in ("scope", "conversations.members", "conversations.info", "API"):
        assert forbidden not in text
    assert "everyone in this conversation is also in that one" in text


def test_a_read_is_allowed_when_everybody_here_is_also_there() -> None:
    workspace = _Workspace(
        members={_ORIGIN: {_ALICE, _BOT}, _TARGET: {_ALICE, _BOB, _BOT}}
    )
    result = _read(_toolkit(workspace, HISTORY_MEMBERS), chat_id=_TARGET)

    assert result["ok"] is True
    assert result["chat_id"] == _TARGET
    # The scan itself went to the target, not to the room that asked.
    assert [call["channel"] for call in workspace.calls["conversations_history"]] == [
        _TARGET
    ]


def test_a_read_is_refused_when_somebody_here_is_not_there_and_says_who() -> None:
    """A refusal an operator can act on names the people blocking it.

    "Not permitted" is the one answer nobody can do anything about; a name is
    either somebody to invite or somebody who should not be in the room.

    The target is public, which is where naming costs nothing: anybody in the
    workspace can read a public channel's membership for themselves, so the
    refusal discloses nothing the room could not have fetched. At ``members``
    publicness relaxes nothing about the *rule* -- the read is still refused --
    only about what the refusal is allowed to say.
    """
    workspace = _Workspace(
        members={_ORIGIN: {_ALICE, _BOB, _BOT}, _PUBLIC: {_ALICE, _BOT}},
        public={_PUBLIC},
    )
    with _captured(_LOGGER_NAME) as records:
        result = _read(_toolkit(workspace, HISTORY_MEMBERS), chat_id=_PUBLIC)

    assert result["ok"] is False
    assert result["error"] == "source_members_not_in_target"
    assert _BOB in result["detail"]
    # And the refusal names the room the request came from, never the room it
    # was refused access to dressed up as the room it read.
    assert result["chat_id"] == _ORIGIN
    assert result["messages"] == []
    assert any("source_members_not_in_target" in said for said in _warnings(records))


def _names_nobody(detail: str, ids: "set[str]") -> bool:
    """Whether a refusal detail mentions none of a set of member ids.

    The property, rather than the sentence: a rewording of the refusal must not
    quietly stop this being tested, and an id appearing anywhere in the string
    is the leak whatever the surrounding words are.
    """
    return not any(member in detail for member in ids)


def test_a_private_target_is_refused_by_count_and_names_nobody() -> None:
    """Who is missing from a private room is that room's business.

    The refusal is posted back into ``S``, so naming the person would tell
    everybody in ``S`` that this named colleague is not in ``T`` -- which is
    not something they could have looked up, and not something the blocked
    person agreed to publish. The count survives because it is both harmless
    and the part worth acting on: one is an invitation to make, thirty is a
    pair of rooms that were never going to line up.
    """
    workspace = _Workspace(
        members={_ORIGIN: {_ALICE, _BOB, _BOT}, _TARGET: {_ALICE, _BOT}}
    )
    result = _read(_toolkit(workspace, HISTORY_MEMBERS), chat_id=_TARGET)

    assert result["error"] == "source_members_not_in_target"
    assert _names_nobody(result["detail"], {_ALICE, _BOB, _BOT, _INTEGRATION})
    # Still says how many, and still says which rooms -- the asker named one of
    # those and was already in the other.
    assert "1" in result["detail"]
    assert _ORIGIN in result["detail"] and _TARGET in result["detail"]


def test_the_count_is_the_whole_of_it_however_many_are_blocking() -> None:
    """Not the cap with a suffix: no ids at all, and no "and N more" either.

    Above ``_MAX_REPORTED_BLOCKING_MEMBERS`` the public branch shows five names
    and says how many it held back. The non-public branch is that cap taken to
    zero, so what is left is the number and nothing else.
    """
    crowd = {f"U-{index:02d}" for index in range(9)}
    workspace = _Workspace(
        members={_ORIGIN: crowd | {_ALICE, _BOT}, _TARGET: {_ALICE, _BOT}}
    )
    result = _read(_toolkit(workspace, HISTORY_MEMBERS), chat_id=_TARGET)

    assert result["error"] == "source_members_not_in_target"
    assert _names_nobody(result["detail"], crowd)
    assert "9" in result["detail"]
    assert "more" not in result["detail"]


@pytest.mark.parametrize(
    ("record", "why"),
    [
        (
            {"is_channel": True, "is_private": True},
            "a private channel",
        ),
        (
            {"is_im": True, "is_private": False},
            "a direct message whose private flag reads false",
        ),
        (
            {"is_mpim": True, "is_private": False},
            "a group direct message whose private flag reads false",
        ),
        (
            {"is_channel": True},
            "a channel record carrying no private flag at all",
        ),
    ],
)
def test_only_a_public_channel_earns_a_name(
    record: "dict[str, Any]", why: str
) -> None:
    """The predicate is "public channel", not ``is_private``.

    A DM and a group DM answer ``is_im``/``is_mpim`` and can hold a private
    flag that reads false, so a bare ``not is_private`` check would name people
    into exactly the two conversations that hide their membership hardest. An
    absent flag is not read as public either: for a decision about what to
    disclose, unestablished has to fall on the quiet side.
    """
    workspace = _Workspace(members={_ORIGIN: {_ALICE, _BOB, _BOT}, "X-ROOM": {_ALICE}})

    async def _info(**kwargs: Any) -> dict[str, Any]:
        workspace.calls["conversations_info"].append(kwargs)
        return {"channel": dict(record, id="X-ROOM")}

    workspace.conversations_info = _info  # type: ignore[method-assign]
    result = _read(_toolkit(workspace, HISTORY_MEMBERS), chat_id="X-ROOM")

    assert result["error"] == "source_members_not_in_target", why
    assert _names_nobody(result["detail"], {_ALICE, _BOB, _BOT}), why


def test_the_asker_being_entitled_is_not_enough_on_its_own() -> None:
    """The answer is posted into the room, in front of everybody in it.

    A rule keyed on the asker alone would summarise T into a room full of people
    who are not in T -- which is the whole reason the rule is about the room.
    """
    workspace = _Workspace(
        members={_ORIGIN: {_ALICE, _BOB, _BOT}, _TARGET: {_ALICE, _BOT}}
    )
    result = _read(
        _toolkit(workspace, HISTORY_MEMBERS, slack_user_id=_ALICE), chat_id=_TARGET
    )
    assert result["error"] == "source_members_not_in_target"


def test_a_direct_message_reduces_to_the_asker_and_the_bot() -> None:
    """The DM case is subsumed, not special-cased.

    ``members(S)`` is two ids, so the rule reads *the asking user and the bot
    are both in T* -- which is the answer a special case would have had to
    write, arrived at by the general rule.
    """
    workspace = _Workspace(
        members={_DM: {_ALICE, _BOT}, _TARGET: {_ALICE, _BOB, _BOT}}
    )
    result = _read(
        _toolkit(
            workspace,
            HISTORY_MEMBERS,
            slack_channel_id=_DM,
            slack_channel_type="im",
        ),
        chat_id=_TARGET,
    )
    assert result["ok"] is True
    assert result["chat_id"] == _TARGET


def test_a_direct_message_is_still_refused_when_the_bot_is_not_in_the_target() -> None:
    """Bots are counted, not excluded.

    An integration in ``S`` is a reader that may archive or forward, and the bot
    is the one integration guaranteed to be there. The target is public so the
    refusal is allowed to say which member it was; that it *is* the bot is the
    whole of what this asserts.
    """
    workspace = _Workspace(
        members={_DM: {_ALICE, _BOT}, _PUBLIC: {_ALICE, _BOB}}, public={_PUBLIC}
    )
    result = _read(
        _toolkit(
            workspace,
            HISTORY_MEMBERS,
            slack_channel_id=_DM,
            slack_channel_type="im",
        ),
        chat_id=_PUBLIC,
    )
    assert result["error"] == "source_members_not_in_target"
    assert _BOT in result["detail"]


# ── 3. Public is asymmetric ──────────────────────────────────────────────────


def test_open_lets_a_public_target_skip_the_subset_rule() -> None:
    """Membership of a public channel is self-serve, so nothing new is shown."""
    workspace = _Workspace(
        members={_ORIGIN: {_ALICE, _BOB, _BOT}, _PUBLIC: {_ALICE}},
        public={_PUBLIC},
    )
    result = _read(_toolkit(workspace, HISTORY_OPEN), chat_id=_PUBLIC)

    assert result["ok"] is True
    assert result["chat_id"] == _PUBLIC
    # Relaxed before any member list was read, which is the point of relaxing.
    assert "conversations_members" not in workspace.calls


def test_members_does_not_relax_a_public_target() -> None:
    """The relaxation is the whole of what ``open`` adds over ``members``."""
    workspace = _Workspace(
        members={_ORIGIN: {_ALICE, _BOB, _BOT}, _PUBLIC: {_ALICE}},
        public={_PUBLIC},
    )
    result = _read(_toolkit(workspace, HISTORY_MEMBERS), chat_id=_PUBLIC)
    assert result["error"] == "source_members_not_in_target"


def test_a_public_source_earns_no_relaxation_at_all() -> None:
    """The dangerous side, and the one no value reads.

    The subset check holds at the instant it is evaluated; somebody may join a
    public ``S`` afterwards and read ``T`` out of the scrollback. That the
    relaxation is targets-only is enforced by there being no value that reads
    the source's privacy, not by reading it and deciding -- so a public source
    and a private one are refused identically.
    """
    workspace = _Workspace(
        members={_ORIGIN: {_ALICE, _BOB, _BOT}, _TARGET: {_ALICE, _BOT}},
        public={_ORIGIN},
    )
    result = _read(_toolkit(workspace, HISTORY_OPEN), chat_id=_TARGET)

    assert result["error"] == "source_members_not_in_target"
    # The source's own record was never even fetched, so its publicness could
    # not have been read whatever the branch had wanted to do with it.
    assert [call["channel"] for call in workspace.calls["conversations_info"]] == [
        _TARGET
    ]


def test_a_group_dm_is_never_public_however_its_private_flag_reads() -> None:
    workspace = _Workspace(members={_ORIGIN: {_ALICE, _BOB, _BOT}, "G-ROOM": {_ALICE}})

    async def _mpim_info(**kwargs: Any) -> dict[str, Any]:
        workspace.calls["conversations_info"].append(kwargs)
        return {
            "channel": {"id": "G-ROOM", "is_mpim": True, "is_private": False}
        }

    workspace.conversations_info = _mpim_info  # type: ignore[method-assign]
    result = _read(_toolkit(workspace, HISTORY_OPEN), chat_id="G-ROOM")
    assert result["error"] == "source_members_not_in_target"


# ── 3b. visible: the permission, not the membership ──────────────────────────
#
# The word this section is about was added because ``members`` refused a real
# read it should have allowed: a public channel, eleven people in the asking
# room who were simply not in it, and a refusal whose own last clause -- "content
# they cannot read themselves" -- was false about every one of them. Joining a
# public channel is self-serve for a full member of the workspace.


def _incident(**kwargs: Any) -> _Workspace:
    """The refusal that produced this word, as a workspace.

    Eleven members of ``S`` who are not in ``T``, ``T`` public, and the bot in
    both -- it has to be in ``T`` or Slack would decline the history read
    whatever this gate said.
    """
    return _Workspace(
        members={
            _ORIGIN: {_ALICE, _BOT, *_CROWD},
            _PUBLIC: {_ALICE, _BOT},
        },
        public={_PUBLIC},
        **kwargs,
    )


def test_members_refuses_the_incident_and_says_something_untrue() -> None:
    """The defect, pinned as it was observed.

    Kept as the control for the test below it. ``members`` is unchanged and
    must stay unchanged -- a deployment that wrote it down gets the membership
    rule it wrote -- so this is not a bug report, it is the baseline the new
    word is measured against.
    """
    result = _read(_toolkit(_incident(), HISTORY_MEMBERS), chat_id=_PUBLIC)

    assert result["error"] == "source_members_not_in_target"
    assert f"{len(_CROWD)} member(s)" in result["detail"]
    assert "cannot read themselves" in result["detail"]


def test_visible_lets_the_incident_through_because_they_could_all_join() -> None:
    """The fix, on the shape that produced it.

    Every one of the eleven is a full member of the workspace, so every one of
    them could have opened the channel without asking anybody. Under ``visible``
    the read is answered.
    """
    workspace = _incident()
    result = _read(_toolkit(workspace, HISTORY_VISIBLE, lookups=50), chat_id=_PUBLIC)

    assert result["ok"] is True
    assert result["chat_id"] == _PUBLIC
    # One lookup per person the subset test rejected, and none for anybody it
    # did not: the reduction is applied to the blocking set, not to the room.
    # Alice appears because she wrote the message the answer carries and her
    # display name is resolved after the gate, on the same budget; the bot is
    # in the target and is asked about by neither.
    asked = {call["user"] for call in workspace.calls["users_info"]}
    assert asked == set(_CROWD) | {_ALICE}
    assert _BOT not in asked


def test_visible_still_refuses_when_one_of_them_is_a_guest() -> None:
    """A guest cannot join a public channel, so one of them still blocks.

    The same eleven with a twelfth person who is a multi-channel guest. Ten
    full members do not rescue the read: the answer lands in front of all of
    them at once, and the guest is in the room.
    """
    workspace = _Workspace(
        members={_ORIGIN: {_ALICE, _BOT, _GUEST, *_CROWD}, _PUBLIC: {_ALICE, _BOT}},
        public={_PUBLIC},
        guests={_GUEST},
    )
    result = _read(_toolkit(workspace, HISTORY_VISIBLE, lookups=50), chat_id=_PUBLIC)

    assert result["error"] == "source_members_cannot_see_target"
    assert "1 member(s)" in result["detail"]


def test_a_single_channel_guest_blocks_like_a_multi_channel_one() -> None:
    """``is_ultra_restricted`` is the narrower guest and is read as one.

    Slack sets both flags for a single-channel guest and only the first for a
    multi-channel one. Either is a guest; reading only ``is_restricted`` would
    happen to work and reading only the other would not, so both are tested.
    """
    workspace = _Workspace(
        members={
            _ORIGIN: {_ALICE, _BOT, _SINGLE_CHANNEL_GUEST},
            _PUBLIC: {_ALICE, _BOT},
        },
        public={_PUBLIC},
        single_channel_guests={_SINGLE_CHANNEL_GUEST},
    )
    result = _read(_toolkit(workspace, HISTORY_VISIBLE, lookups=50), chat_id=_PUBLIC)
    assert result["error"] == "source_members_cannot_see_target"


def test_the_refusal_under_visible_names_nobody() -> None:
    """Naming the blocking members would publish which of them is a guest.

    Under ``members`` a public target's blocking set is *who is not in T*, which
    ``conversations.members`` will tell anybody who asks, so the refusal names
    them. Under ``visible`` that set has been reduced by what each person is,
    and Slack publishes no such list. The count is what survives.
    """
    workspace = _Workspace(
        members={_ORIGIN: {_ALICE, _BOT, _GUEST, *_CROWD}, _PUBLIC: {_ALICE, _BOT}},
        public={_PUBLIC},
        guests={_GUEST},
    )
    result = _read(_toolkit(workspace, HISTORY_VISIBLE, lookups=50), chat_id=_PUBLIC)

    assert result["error"] == "source_members_cannot_see_target"
    assert _GUEST not in result["detail"]
    for member in _CROWD:
        assert member not in result["detail"]


def test_an_application_in_the_source_still_blocks_a_public_target() -> None:
    """Bots are counted, not excluded, and ``visible`` does not change that.

    An integration in ``S`` is a reader that may archive or forward, and under
    this word there is a second reason that is the operative one: what an app's
    token may read is decided by the scopes it was installed with, and no call
    this toolkit can make reports them. "It could have read T anyway" is
    therefore not establishable, and a gate that cannot establish something
    refuses. ``history_exempt_members`` is where an operator says otherwise, by
    name and on the record.
    """
    workspace = _Workspace(
        members={_ORIGIN: {_ALICE, _BOT, _APP}, _PUBLIC: {_ALICE, _BOT}},
        public={_PUBLIC},
        applications={_APP},
    )
    refused = _read(_toolkit(workspace, HISTORY_VISIBLE, lookups=50), chat_id=_PUBLIC)
    assert refused["error"] == "source_members_cannot_see_target"

    allowed = _read(
        _toolkit(
            _Workspace(
                members={_ORIGIN: {_ALICE, _BOT, _APP}, _PUBLIC: {_ALICE, _BOT}},
                public={_PUBLIC},
                applications={_APP},
            ),
            HISTORY_VISIBLE,
            lookups=50,
            **{METADATA_EXEMPT_MEMBERS_KEY: [_APP]},
        ),
        chat_id=_PUBLIC,
    )
    assert allowed["ok"] is True


def test_an_application_is_kept_out_even_when_slack_sends_no_guest_flags() -> None:
    """The app markers are tested before the guest ones, and this is why.

    Slack is not documented to send ``is_restricted`` for a bot user, and an
    implementation that asked only "is this person a guest" would read the
    absence as *not a guest* and let every application through silently. This
    record is a bot and says nothing else at all.
    """
    workspace = _Workspace(
        members={_ORIGIN: {_ALICE, _BOT, _APP}, _PUBLIC: {_ALICE, _BOT}},
        public={_PUBLIC},
        users={_APP: {"id": _APP, "is_bot": True}},
    )
    result = _read(_toolkit(workspace, HISTORY_VISIBLE, lookups=50), chat_id=_PUBLIC)
    assert result["error"] == "source_members_cannot_see_target"


def test_an_app_user_is_kept_out_by_the_other_marker() -> None:
    """``is_app_user`` is the second way Slack says an account is an app."""
    workspace = _Workspace(
        members={_ORIGIN: {_ALICE, _BOT, _APP}, _PUBLIC: {_ALICE, _BOT}},
        public={_PUBLIC},
        users={
            _APP: {
                "id": _APP,
                "is_app_user": True,
                "is_restricted": False,
                "is_ultra_restricted": False,
            }
        },
    )
    result = _read(_toolkit(workspace, HISTORY_VISIBLE, lookups=50), chat_id=_PUBLIC)
    assert result["error"] == "source_members_cannot_see_target"


def test_somebody_from_another_workspace_blocks() -> None:
    """A Slack Connect member is nobody's guest here and nobody's member either.

    The two guest flags are about this workspace's own roles, so a person
    reaching a shared channel from another organisation can come back with both
    of them false while being unable to browse a single one of this workspace's
    public channels. ``is_stranger`` is what Slack says about them, and it is
    read beside the application markers rather than beside the guest ones.
    """
    workspace = _Workspace(
        members={_ORIGIN: {_ALICE, _BOT, _BOB}, _PUBLIC: {_ALICE, _BOT}},
        public={_PUBLIC},
        users={
            _BOB: {
                "id": _BOB,
                "is_bot": False,
                "is_stranger": True,
                "is_restricted": False,
                "is_ultra_restricted": False,
            }
        },
    )
    result = _read(_toolkit(workspace, HISTORY_VISIBLE, lookups=50), chat_id=_PUBLIC)
    assert result["error"] == "source_members_cannot_see_target"


def test_a_record_that_establishes_nothing_blocks() -> None:
    """Being a full member has to be established, not merely not contradicted.

    The two ways of being wrong are not symmetrical. Reading a guest as a full
    member shows them a channel nobody invited them to; reading a full member
    as a guest refuses a read that was fine and says so where somebody can act
    on it. So both guest flags must be present and false.
    """
    workspace = _Workspace(
        members={_ORIGIN: {_ALICE, _BOT, _BOB}, _PUBLIC: {_ALICE, _BOT}},
        public={_PUBLIC},
        users={_BOB: {"id": _BOB}},
    )
    result = _read(_toolkit(workspace, HISTORY_VISIBLE, lookups=50), chat_id=_PUBLIC)
    assert result["error"] == "source_members_cannot_see_target"


def test_visible_does_not_relax_a_private_target() -> None:
    """Only a public channel is joinable at will, so only one is relaxed.

    A private channel needs an invitation whatever anybody's account kind is,
    so the reduction has no question to ask and does not ask one.
    """
    workspace = _incident()
    workspace.public = set()
    result = _read(_toolkit(workspace, HISTORY_VISIBLE, lookups=50), chat_id=_PUBLIC)

    assert result["error"] == "source_members_not_in_target"
    assert "users_info" not in workspace.calls


def test_visible_does_not_relax_a_direct_message_target() -> None:
    """A direct message is joinable by nobody, however its flags read."""
    workspace = _Workspace(
        members={_ORIGIN: {_ALICE, _BOT, _BOB}, _DM: {_ALICE, _BOT}}
    )
    result = _read(_toolkit(workspace, HISTORY_VISIBLE, lookups=50), chat_id=_DM)

    assert result["error"] == "source_members_not_in_target"
    assert "users_info" not in workspace.calls


def test_visible_does_not_relax_a_group_direct_message_target() -> None:
    """Nor is a group direct message, which is the third kind and not a room."""
    workspace = _Workspace(
        members={_ORIGIN: {_ALICE, _BOT, _BOB}, _MPIM: {_ALICE, _BOT}},
        mpim={_MPIM},
    )
    result = _read(_toolkit(workspace, HISTORY_VISIBLE, lookups=50), chat_id=_MPIM)

    assert result["error"] == "source_members_not_in_target"
    assert "users_info" not in workspace.calls


def test_visible_reads_a_public_target_from_a_direct_message() -> None:
    """The source's kind changes who is in ``members(S)`` and nothing else.

    In a direct message that is the one person and the bot, so the rule reduces
    to *the asking person could read T, and the bot is in it*. Both hold here.
    """
    workspace = _Workspace(
        members={_DM: {_ALICE, _BOT}, _PUBLIC: {_BOB, _BOT}}, public={_PUBLIC}
    )
    result = _read(
        _toolkit(
            workspace,
            HISTORY_VISIBLE,
            lookups=50,
            slack_channel_id=_DM,
            slack_channel_type="im",
        ),
        chat_id=_PUBLIC,
    )
    assert result["ok"] is True


def test_a_public_source_earns_no_relaxation_under_visible_either() -> None:
    """The source cannot opt in, and no word reads its privacy.

    ``visible`` reduces the blocking set by what each person *is*, never by
    what the room they are in is. A public source and a private one are refused
    identically against a private target.
    """
    workspace = _Workspace(
        members={_ORIGIN: {_ALICE, _BOT, _BOB}, _TARGET: {_ALICE, _BOT}},
        public={_ORIGIN},
    )
    result = _read(_toolkit(workspace, HISTORY_VISIBLE, lookups=50), chat_id=_TARGET)

    assert result["error"] == "source_members_not_in_target"
    assert [call["channel"] for call in workspace.calls["conversations_info"]] == [
        _TARGET
    ]


def test_open_is_the_cheap_word_and_visible_is_not() -> None:
    """What ``open`` adds over ``visible``, stated as the cost it saves.

    ``open`` decides on the target's record alone: no member list, no directory
    lookup, and guests go through with everybody else. That is a word an
    operator can want, and it is what keeps ``visible`` from collapsing into it.
    """
    wide = _Workspace(
        members={_ORIGIN: {_ALICE, _BOT, _GUEST}, _PUBLIC: {_BOT}},
        public={_PUBLIC},
        guests={_GUEST},
    )
    assert _read(_toolkit(wide, HISTORY_OPEN), chat_id=_PUBLIC)["ok"] is True
    assert "conversations_members" not in wide.calls
    assert "users_info" not in wide.calls

    narrow = _Workspace(
        members={_ORIGIN: {_ALICE, _BOT, _GUEST}, _PUBLIC: {_BOT}},
        public={_PUBLIC},
        guests={_GUEST},
    )
    refused = _read(_toolkit(narrow, HISTORY_VISIBLE, lookups=50), chat_id=_PUBLIC)
    assert refused["error"] == "source_members_cannot_see_target"


def test_an_exempt_member_is_never_asked_about() -> None:
    """The exemption is subtracted before the subset test, so it costs nothing.

    A workspace-wide integration named in ``history_exempt_members`` never
    reaches the reduction, which is what keeps the directory-lookup budget from
    being spent on the people an operator already decided about.
    """
    workspace = _Workspace(
        members={_ORIGIN: {_ALICE, _BOT, _APP}, _PUBLIC: {_ALICE, _BOT}},
        public={_PUBLIC},
        applications={_APP},
    )
    result = _read(
        _toolkit(
            workspace,
            HISTORY_VISIBLE,
            lookups=50,
            **{METADATA_EXEMPT_MEMBERS_KEY: [_APP]},
        ),
        chat_id=_PUBLIC,
    )
    assert result["ok"] is True
    asked = {call["user"] for call in workspace.calls["users_info"]}
    assert _APP not in asked


def test_an_exemption_naming_the_asker_does_not_carry_them_under_visible() -> None:
    """The asker is added back after the exemption, under this word too.

    Theirs is the one entitlement that is definitionally required, and here the
    exempted asker is a guest: an exemption written about other people must not
    become a way to read a public channel you were never invited to.
    """
    workspace = _Workspace(
        members={_ORIGIN: {_GUEST, _BOT}, _PUBLIC: {_BOT}},
        public={_PUBLIC},
        guests={_GUEST},
    )
    result = _read(
        _toolkit(
            workspace,
            HISTORY_VISIBLE,
            lookups=50,
            slack_user_id=_GUEST,
            **{METADATA_EXEMPT_MEMBERS_KEY: [_GUEST]},
        ),
        chat_id=_PUBLIC,
    )
    assert result["error"] == "source_members_cannot_see_target"


# -- visible fails closed, cause by cause --


def test_a_declined_directory_lookup_refuses_and_names_the_call() -> None:
    """The scope that cannot be asked is the scope an operator must grant.

    ``users:read`` degrades where it resolves a display name and does not here:
    this is a gate, and a gate whose question was declined refuses. The refusal
    names ``users.info`` so that "add a scope" becomes "add this scope".
    """
    workspace = _incident(fail={"users_info": "missing_scope"})
    result = _read(_toolkit(workspace, HISTORY_VISIBLE, lookups=50), chat_id=_PUBLIC)

    assert result["error"] == "history_gate_slack_refused"
    assert "users.info" in result["detail"]
    assert "missing_scope" in result["detail"]


def test_a_directory_failure_that_is_not_a_scope_refuses_too() -> None:
    """Any answer that is not an answer is a refusal, not a default."""
    workspace = _incident(fail={"users_info": "fatal_error"})
    result = _read(_toolkit(workspace, HISTORY_VISIBLE, lookups=50), chat_id=_PUBLIC)
    assert result["error"] == "history_gate_slack_refused"


def test_a_blocking_set_larger_than_the_lookup_budget_refuses() -> None:
    """Its own code, because its own fix is a number rather than a scope.

    One label over four causes would leave an operator unable to tell whether
    to grant something, wait, or raise a number. This one says which number.
    """
    workspace = _incident()
    result = _read(_toolkit(workspace, HISTORY_VISIBLE, lookups=3), chat_id=_PUBLIC)

    assert result["error"] == "history_gate_user_lookups_exhausted"
    assert "history_max_user_lookups" in result["detail"]
    # Counted before anything is spent: a set that was never going to fit is
    # refused without paying for most of it first.
    assert "users_info" not in workspace.calls


def test_a_lookup_budget_of_zero_refuses_rather_than_relaxing() -> None:
    """Zero is a bound like any other and is not a licence.

    An operator who has said this tool makes no directory lookups has said
    ``visible`` cannot be established. The refusal says so by name rather than
    quietly falling back to either the wider reading or the narrower one.
    """
    workspace = _incident()
    result = _read(_toolkit(workspace, HISTORY_VISIBLE, lookups=0), chat_id=_PUBLIC)

    assert result["error"] == "history_gate_user_lookups_exhausted"
    assert "users_info" not in workspace.calls


def test_a_member_kind_is_believed_for_the_member_cache_window() -> None:
    """One clock for both, because both say how stale a refusal's input may be.

    The same toolkit reading twice asks Slack about each person once. A second
    toolkit is a second request and shares nothing, which is what keeps the
    cache from being a channel between requests.
    """
    def gate_lookups(workspace: _Workspace) -> int:
        # Only the people the gate asked about. Display names are resolved on
        # the same budget and are not cached, so counting every users.info
        # would be counting the answer's authors as well as the gate's
        # question.
        return len(
            [
                call
                for call in workspace.calls["users_info"]
                if call["user"] in set(_CROWD)
            ]
        )

    workspace = _incident()
    toolkit = _toolkit(workspace, HISTORY_VISIBLE, lookups=50)
    assert _read(toolkit, chat_id=_PUBLIC)["ok"] is True
    assert gate_lookups(workspace) == len(_CROWD)
    assert _read(toolkit, chat_id=_PUBLIC)["ok"] is True
    assert gate_lookups(workspace) == len(_CROWD)

    clock = {"now": 1_000.0}
    fresh = _incident()
    stale = _toolkit(
        fresh, HISTORY_VISIBLE, lookups=50, monotonic=lambda: clock["now"]
    )
    assert _read(stale, chat_id=_PUBLIC)["ok"] is True
    assert gate_lookups(fresh) == len(_CROWD)
    clock["now"] += 3_600.0
    assert _read(stale, chat_id=_PUBLIC)["ok"] is True
    assert gate_lookups(fresh) == 2 * len(_CROWD)


# ── 4. The two lists ─────────────────────────────────────────────────────────


def test_a_carved_out_conversation_is_refused_before_anything_is_fetched() -> None:
    """A conversation an operator excluded is never enumerated to be refused.

    Ordering is the substance of this one, not tidiness: the cheapest refusal
    first is what stops the refusal itself from reading the member list of a
    conversation the operator said must never travel.
    """
    workspace = _Workspace(
        members={_ORIGIN: {_ALICE, _BOT}, _TARGET: {_ALICE, _BOT}}
    )
    result = _read(
        _toolkit(
            workspace,
            HISTORY_MEMBERS,
            **{METADATA_NEVER_READ_KEY: [_TARGET]},
        ),
        chat_id=_TARGET,
    )

    assert result["ok"] is False
    assert result["error"] == "history_target_never_read"
    assert workspace.calls == {}


def test_a_carve_out_holds_even_where_the_subset_rule_would_have_passed() -> None:
    """Membership is not sensitivity.

    A channel everyone belongs to passes the subset rule trivially and may still
    be the last thing that should be summarised elsewhere. That is the job the
    deny-list does and the subset rule cannot express.
    """
    workspace = _Workspace(members={_ORIGIN: {_ALICE}, _TARGET: {_ALICE, _BOB, _BOT}})
    result = _read(
        _toolkit(
            workspace,
            HISTORY_OPEN,
            **{METADATA_NEVER_READ_KEY: [_TARGET]},
        ),
        chat_id=_TARGET,
    )
    assert result["error"] == "history_target_never_read"


def test_a_carve_out_says_it_is_configuration_and_names_no_one() -> None:
    """The other refusal abstracts; this one does not, and that is the same rule.

    What a carve-out discloses is that somebody holding the configuration wrote
    this conversation down -- about the id the asker put in the call, and about
    no person at all. It is not learnable elsewhere, but neither is it a fact
    about anybody, and saying it is the whole of what makes the answer usable:
    permanent, workspace-wide, and lifted by an operator rather than by getting
    somebody invited. Abstract it and the reader spends the afternoon fixing a
    membership that was never the problem.
    """
    workspace = _Workspace(
        members={_ORIGIN: {_ALICE, _BOT}, _TARGET: {_ALICE, _BOT}}
    )
    result = _read(
        _toolkit(
            workspace,
            HISTORY_MEMBERS,
            **{METADATA_NEVER_READ_KEY: [_TARGET, "C-OTHER-SECRET"]},
        ),
        chat_id=_TARGET,
    )
    detail = result["detail"]

    assert result["error"] == "history_target_never_read"
    # Named, so the reader knows which conversation and that it is a decision
    # rather than a state of the world they can change.
    assert _TARGET in detail
    assert "operator" in detail and "configuration" in detail
    # And nothing beyond what they handed in: not the rest of the deny-list,
    # not the source, and nobody's id. The list is metadata the asker never saw
    # and echoing a second entry would leak a conversation they did not name.
    assert "C-OTHER-SECRET" not in detail
    assert _names_nobody(detail, {_ALICE, _BOB, _BOT})


def test_a_carve_out_does_not_stop_a_room_reading_its_own_history() -> None:
    """It names conversations that may never be a *target*.

    Reading a room to the room itself discloses to exactly the people already
    in it, so blocking that would remove a capability without protecting
    anything -- and would break the promise that ``origin`` is unchanged.
    """
    workspace = _Workspace(members={_ORIGIN: {_ALICE, _BOT}})
    result = _read(
        _toolkit(
            workspace,
            HISTORY_MEMBERS,
            **{METADATA_NEVER_READ_KEY: [_ORIGIN]},
        )
    )
    assert result["ok"] is True
    assert result["chat_id"] == _ORIGIN


def test_an_exempt_member_stops_blocking_the_read() -> None:
    """For the workspace-wide integration that sits in every channel.

    Without it a single such app refuses every cross-conversation read in the
    deployment, which is the failure that makes an operator turn the whole
    feature off.
    """
    workspace = _Workspace(
        members={_ORIGIN: {_ALICE, _BOT, _INTEGRATION}, _TARGET: {_ALICE, _BOT}}
    )
    refused = _read(_toolkit(workspace, HISTORY_MEMBERS), chat_id=_TARGET)
    assert refused["error"] == "source_members_not_in_target"

    allowed = _read(
        _toolkit(
            _Workspace(
                members={
                    _ORIGIN: {_ALICE, _BOT, _INTEGRATION},
                    _TARGET: {_ALICE, _BOT},
                }
            ),
            HISTORY_MEMBERS,
            **{METADATA_EXEMPT_MEMBERS_KEY: [_INTEGRATION]},
        ),
        chat_id=_TARGET,
    )
    assert allowed["ok"] is True


def test_an_exemption_naming_the_asker_does_not_carry_them() -> None:
    """The asker is added back after the exemption is applied.

    Theirs is the one entitlement that is definitionally necessary, and an
    exemption written about other people must not become a way to read a
    conversation you are not in by being named on it. The target is public so
    the refusal may say who; that it is the exempted asker is the point.
    """
    workspace = _Workspace(
        members={_ORIGIN: {_ALICE, _BOT}, _PUBLIC: {_BOT}}, public={_PUBLIC}
    )
    result = _read(
        _toolkit(
            workspace,
            HISTORY_MEMBERS,
            **{METADATA_EXEMPT_MEMBERS_KEY: [_ALICE]},
        ),
        chat_id=_PUBLIC,
    )
    assert result["error"] == "source_members_not_in_target"
    assert _ALICE in result["detail"]


# ── 5. Cron ──────────────────────────────────────────────────────────────────


def _cron_metadata(policy: str = HISTORY_MEMBERS, **overrides: Any) -> dict[str, Any]:
    """What the scheduler stamps: a conversation, a marker, and no sender."""
    base = _metadata(policy)
    base.pop("slack_user_id")
    base[METADATA_ORIGIN_KEY] = ORIGIN_CRON_JOB
    base.update(overrides)
    return base


def test_a_cron_run_is_gated_on_membership_like_anything_else() -> None:
    """Its source is the conversation it delivers into.

    The guarantee the rule makes is about the audience -- nobody in the room the
    answer lands in learns anything they could not already learn -- and that is
    fully checkable without knowing who scheduled the job.
    """
    workspace = _Workspace(
        members={_ORIGIN: {_ALICE, _BOT}, _TARGET: {_ALICE, _BOB, _BOT}}
    )
    toolkit = SlackHistoryToolkit(
        metadata=_cron_metadata(HISTORY_MEMBERS),
        client=workspace,
        now=lambda: 200_000.0,
        max_user_lookups=0,
    )
    result = _read(toolkit, chat_id=_TARGET)
    assert result["ok"] is True
    assert result["chat_id"] == _TARGET


def test_a_cron_run_is_refused_on_the_same_rule() -> None:
    """And reports it the same way: a public target, so it still names who."""
    workspace = _Workspace(
        members={_ORIGIN: {_ALICE, _BOB, _BOT}, _PUBLIC: {_ALICE, _BOT}},
        public={_PUBLIC},
    )
    toolkit = SlackHistoryToolkit(
        metadata=_cron_metadata(HISTORY_MEMBERS),
        client=workspace,
        now=lambda: 200_000.0,
        max_user_lookups=0,
    )
    result = _read(toolkit, chat_id=_PUBLIC)
    assert result["error"] == "source_members_not_in_target"
    assert _BOB in result["detail"]


def test_a_cron_run_needs_no_asker_and_an_inbound_request_does() -> None:
    """The marker is read, never inferred from the missing sender.

    "No asker, and that is expected" and "no asker, and something is wrong" are
    opposite answers. Inferring the first from the second would let any request
    lose its sender and gain the cron reading; nothing on the inbound path
    stamps the marker.
    """
    members = {_ORIGIN: {_ALICE, _BOT}, _TARGET: {_ALICE, _BOT}}

    cron = SlackHistoryToolkit(
        metadata=_cron_metadata(HISTORY_MEMBERS),
        client=_Workspace(members=members),
        now=lambda: 200_000.0,
        max_user_lookups=0,
    )
    assert _read(cron, chat_id=_TARGET)["ok"] is True

    inbound = _metadata(HISTORY_MEMBERS)
    inbound.pop("slack_user_id")
    unsigned = SlackHistoryToolkit(
        metadata=inbound,
        client=_Workspace(members=members),
        now=lambda: 200_000.0,
        max_user_lookups=0,
    )
    assert _read(unsigned, chat_id=_TARGET)["error"] == "history_asker_unresolved"


# ── 6. Fail closed, branch by branch ─────────────────────────────────────────


def test_an_unsettled_policy_is_refused_rather_than_defaulted() -> None:
    """No stamped word means no side that has the configuration spoke.

    The registration gate already declines to mount the tool for such a request,
    so this is defence in depth -- and defaulting here would be the silence that
    hides a metadata path nobody meant to exist.
    """
    workspace = _Workspace(members={_ORIGIN: {_ALICE, _BOT}})
    toolkit = SlackHistoryToolkit(
        metadata={
            "slack_channel_id": _ORIGIN,
            "slack_channel_type": "channel",
            "slack_user_id": _ALICE,
        },
        client=workspace,
        now=lambda: 200_000.0,
    )
    result = _read(toolkit)
    assert result["error"] == "history_policy_unsettled"
    assert workspace.calls == {}


@pytest.mark.parametrize("word", ["shared", "current", "any", "ALL", 7, None])
def test_a_word_that_is_not_one_of_the_four_is_refused(word: Any) -> None:
    workspace = _Workspace(members={_ORIGIN: {_ALICE, _BOT}})
    result = _read(_toolkit(workspace, HISTORY_MEMBERS, **{METADATA_POLICY_KEY: word}))
    assert result["error"] == "history_policy_value_unknown"
    assert workspace.calls == {}


def test_no_trusted_conversation_returns_exactly_what_it_always_did() -> None:
    """The oldest refusal, reachable on a deployment configured for none of this.

    It comes back byte for byte: no detail, no conversation ids, because none of
    them are known.
    """
    toolkit = SlackHistoryToolkit(metadata={}, client=_Workspace())
    assert _read(toolkit) == {
        "ok": False,
        "error": "trusted_slack_channel_context_required",
        "messages": [],
    }


def test_a_membership_lookup_that_fails_refuses_rather_than_answering() -> None:
    """Falling back to the originating conversation is the one thing worse.

    An answer about the wrong conversation, presented as an answer about the
    right one, is worse than no answer: the model has no way to tell, and the
    room reading it has none either.
    """
    workspace = _Workspace(
        members={_ORIGIN: {_ALICE, _BOT}, _TARGET: {_ALICE, _BOT}},
        fail={"conversations_members": "channel_not_found"},
    )
    result = _read(_toolkit(workspace, HISTORY_MEMBERS), chat_id=_TARGET)

    assert result["ok"] is False
    assert result["error"] == "history_gate_slack_refused"
    assert result["chat_id"] == _ORIGIN
    assert result["messages"] == []
    assert "conversations_history" not in workspace.calls


def test_a_conversation_record_that_cannot_be_read_refuses() -> None:
    workspace = _Workspace(
        members={_ORIGIN: {_ALICE, _BOT}, _TARGET: {_ALICE, _BOT}},
        fail={"conversations_info": "channel_not_found"},
    )
    result = _read(_toolkit(workspace, HISTORY_MEMBERS), chat_id=_TARGET)
    assert result["error"] == "history_gate_slack_refused"
    assert "conversations_history" not in workspace.calls


def test_a_refused_call_says_which_call_slack_refused() -> None:
    """``missing_scope`` alone is not something an operator can act on.

    The gate makes two different calls, and Slack answers both with the same
    word when the token is short of a scope. Naming the method is the whole
    difference between "add a scope" and "add which scope": ``conversations.info``
    and ``conversations.members`` are not fixed by the same grant.

    Spelled with a dot, which is how Slack's own documentation, scope pages and
    error messages spell it -- the SDK's underscore is ours, not theirs.
    """
    workspace = _Workspace(
        members={_ORIGIN: {_ALICE, _BOT}, _TARGET: {_ALICE, _BOT}},
        fail={"conversations_info": "missing_scope"},
    )
    detail = _read(_toolkit(workspace, HISTORY_MEMBERS), chat_id=_TARGET)["detail"]

    assert "conversations.info" in detail
    assert "conversations.members" not in detail
    assert "missing_scope" in detail


@pytest.mark.parametrize(
    ("target", "public", "kind", "scope"),
    [
        (_TARGET, set(), "private channel", "groups:read"),
        (_PUBLIC, {_PUBLIC}, "public channel", "channels:read"),
        (_DM, set(), "direct message", "im:read"),
    ],
    ids=["private-channel", "public-channel", "direct-message"],
)
def test_a_refused_membership_names_the_conversation_kind_and_its_scope(
    target: str, public: "set[str]", kind: str, scope: str
) -> None:
    """The kind is already known, and it is what selects the scope.

    ``conversations.members`` is one method with four scopes behind it, one per
    kind of conversation. The record that says which kind has already been
    fetched by the ``conversations.info`` immediately before -- so an operator
    reading the refusal is told which grant is missing rather than which four it
    might be.

    Never guessed from the id: a prefix separates a DM from everything else and
    nothing further, and a guessed scope sends somebody to grant the wrong one.
    """
    workspace = _Workspace(
        members={_ORIGIN: {_ALICE, _BOT}, target: {_ALICE, _BOT}},
        public=public,
        fail_members={target: "missing_scope"},
    )
    detail = _read(_toolkit(workspace, HISTORY_MEMBERS), chat_id=target)["detail"]

    assert "conversations.members" in detail
    assert kind in detail
    assert scope in detail


def test_a_refused_source_membership_names_no_scope_it_has_not_established() -> None:
    """The source's record is never fetched, so its kind is never claimed.

    Fetching one would cost a call on every gated read to improve a message that
    only a failure ever prints. The refusal names the method and the
    conversation, and stops there rather than guessing a kind from the id.
    """
    workspace = _Workspace(
        members={_ORIGIN: {_ALICE, _BOT}, _TARGET: {_ALICE, _BOT}},
        fail_members={_ORIGIN: "missing_scope"},
    )
    detail = _read(_toolkit(workspace, HISTORY_MEMBERS), chat_id=_TARGET)["detail"]

    assert "conversations.members" in detail
    assert _ORIGIN in detail
    assert not [word for word in ("channels:read", "groups:read", "im:read") if word in detail]


def test_the_four_ways_the_gate_can_fail_are_four_different_codes() -> None:
    """One label over four causes tells an operator nothing to do.

    Slack declining a call, the check running out of time, the check running
    out of this deployment's API call budget, and the check having more people
    to ask about than its directory-lookup budget allows are fixed by granting
    a scope, by waiting or raising a timeout, by raising a call limit, and by
    raising a different limit. They shared the code
    ``history_gate_unavailable``, so the one string an operator could act on
    was the free-text detail -- which nothing pins and no caller can branch on.
    """
    refused = _Workspace(
        members={_ORIGIN: {_ALICE, _BOT}, _TARGET: {_ALICE, _BOT}},
        fail={"conversations_info": "missing_scope"},
    )
    assert (
        _read(_toolkit(refused, HISTORY_MEMBERS), chat_id=_TARGET)["error"]
        == "history_gate_slack_refused"
    )

    # A clock that jumps past the deadline the moment it is armed. The first
    # reading arms it; every reading after it is the timeout.
    readings = iter([0.0])
    timed_out = SlackHistoryToolkit(
        metadata=_metadata(HISTORY_MEMBERS),
        client=_Workspace(members={_ORIGIN: {_ALICE, _BOT}, _TARGET: {_ALICE}}),
        now=lambda: 200_000.0,
        monotonic=lambda: next(readings, 10_000.0),
        max_user_lookups=0,
    )
    result = _read(timed_out, chat_id=_TARGET)
    assert result["error"] == "history_gate_timed_out"
    assert "history_scan_timeout_seconds" in result["detail"]

    # One call is enough for the conversations.info and nothing after it.
    exhausted = SlackHistoryToolkit(
        metadata=_metadata(HISTORY_MEMBERS),
        client=_Workspace(members={_ORIGIN: {_ALICE, _BOT}, _TARGET: {_ALICE}}),
        now=lambda: 200_000.0,
        max_api_calls=1,
        max_user_lookups=0,
    )
    result = _read(exhausted, chat_id=_TARGET)
    assert result["error"] == "history_gate_call_budget_exhausted"
    assert "history_max_api_calls" in result["detail"]

    # The fourth, which only ``visible`` can reach: it is the one cause whose
    # fix is a different number from the one above.
    starved = _read(
        _toolkit(_incident(), HISTORY_VISIBLE, lookups=2), chat_id=_PUBLIC
    )
    assert starved["error"] == "history_gate_user_lookups_exhausted"
    assert "history_max_user_lookups" in starved["detail"]

    # And a lookup the scan clock overtakes is the timeout rather than either
    # budget: the reduction runs inside the scan deadline like every other
    # call, so a clock that jumps the moment the first directory lookup lands
    # stops it there.
    ticking = _incident()
    slow = SlackHistoryToolkit(
        metadata=_metadata(HISTORY_VISIBLE),
        client=ticking,
        now=lambda: 200_000.0,
        monotonic=lambda: 10_000.0 if ticking.calls["users_info"] else 0.0,
        max_user_lookups=50,
    )
    assert _read(slow, chat_id=_PUBLIC)["error"] == "history_gate_timed_out"
    assert len(ticking.calls["users_info"]) == 1


def test_a_truncated_member_list_refuses_rather_than_being_believed() -> None:
    """Half a member list is the one shape that turns a refusal into a grant.

    ``members(S)`` short by one person is a subset check that passes because the
    blocking member was on the page that did not arrive, so a page Slack says
    exists and gives no cursor for takes the whole read with it.
    """
    workspace = _Workspace(
        members={_TARGET: {_ALICE, _BOT}},
        members_pages={_ORIGIN: [{"members": [_ALICE], "has_more": True}]},
    )
    result = _read(_toolkit(workspace, HISTORY_MEMBERS), chat_id=_TARGET)
    # Named as itself rather than folded into the generic gate failure: Slack
    # answered, so nothing was unavailable -- what it returned was short, which
    # is a different thing for whoever reads the log.
    assert result["error"] == "conversation_members_incomplete"
    assert _ORIGIN in result["detail"]
    assert result["messages"] == []
    assert "conversations_history" not in workspace.calls


def _answering_info(
    workspace: _Workspace, answer: dict[str, Any]
) -> None:
    """Make every conversations.info on this workspace answer ``answer``."""

    async def _info(**kwargs: Any) -> dict[str, Any]:
        workspace.calls["conversations_info"].append(kwargs)
        return answer

    workspace.conversations_info = _info  # type: ignore[method-assign]


def test_a_target_this_tool_does_not_read_is_named_rather_than_attempted() -> None:
    workspace = _Workspace(members={_ORIGIN: {_ALICE, _BOT}})
    # A record that says what it is, and says something this tool does not read.
    _answering_info(workspace, {"channel": {"id": "X-ODD", "is_workflow": True}})

    result = _read(_toolkit(workspace, HISTORY_MEMBERS), chat_id="X-ODD")

    assert result["error"] == "target_conversation_record_unavailable"
    assert "conversations_history" not in workspace.calls


@pytest.mark.parametrize(
    "answer",
    [
        # Slack answered ok and sent no channel object at all.
        {},
        # A channel object that is not one.
        {"channel": "C-TARGET"},
        # A record that names the conversation and says nothing about its kind.
        {"channel": {"id": _TARGET}},
    ],
)
def test_a_target_whose_kind_slack_never_stated_is_refused(
    answer: dict[str, Any],
) -> None:
    """Fail closed. The gate reads the record; a record that says nothing denies.

    _conversation_info answers {} for a call that came back without a usable
    channel, and _channel_type_of then falls back to reading the id: anything
    that does not begin with D is a channel. That fallback is for the paths
    that fetch no record, where the kind is only a label. Here it decided
    whether the read was allowed: every target whose record was unreadable
    became a supported kind, never public so never relaxed, and went on to the
    membership comparison as though Slack had said it was a channel. A
    conversation of a kind this tool does not read was reachable that way, and
    the scope named on a declined membership read was the wrong scope.
    """
    workspace = _Workspace(members={_ORIGIN: {_ALICE, _BOT}, _TARGET: {_ALICE, _BOT}})
    _answering_info(workspace, answer)

    result = _read(_toolkit(workspace, HISTORY_MEMBERS), chat_id=_TARGET)

    assert result["ok"] is False
    assert result["error"] == "target_conversation_record_unavailable"
    assert _TARGET in result["detail"]
    # Refused before any membership is enumerated, and nothing is read.
    assert "conversations_members" not in workspace.calls
    assert "conversations_history" not in workspace.calls
    assert result["messages"] == []


def test_an_unreadable_record_denies_the_open_relaxation_too() -> None:
    """The widest word is still a word about a kind Slack has stated."""
    workspace = _Workspace(
        members={_ORIGIN: {_ALICE, _BOT}}, public={_PUBLIC}
    )
    _answering_info(workspace, {"channel": {"id": _PUBLIC, "is_private": False}})

    result = _read(_toolkit(workspace, HISTORY_OPEN), chat_id=_PUBLIC)

    assert result["error"] == "target_conversation_record_unavailable"
    assert "conversations_history" not in workspace.calls


def test_no_refusal_ever_returns_the_originating_conversations_messages() -> None:
    """The invariant behind every branch above, asserted once as itself."""
    cases = [
        (HISTORY_DISABLED, {}, None),
        (HISTORY_ORIGIN, {}, _TARGET),
        (HISTORY_MEMBERS, {METADATA_NEVER_READ_KEY: [_TARGET]}, _TARGET),
        (HISTORY_MEMBERS, {METADATA_POLICY_KEY: "shared"}, _TARGET),
    ]
    for policy, extra, target in cases:
        workspace = _Workspace(
            members={_ORIGIN: {_ALICE, _BOT}, _TARGET: {_ALICE, _BOT}}
        )
        result = _read(
            _toolkit(workspace, policy, **extra),
            **({"chat_id": target} if target else {}),
        )
        assert result["ok"] is False, (policy, extra)
        assert result["messages"] == []
        assert "conversations_history" not in workspace.calls


# ── 7. The membership cache ──────────────────────────────────────────────────


def test_one_member_list_is_read_once_for_several_reads_in_a_turn() -> None:
    """A cache keyed on the conversation, because that is what the answer is about.

    "Who is in C-TARGET" has one answer, whoever wants it, so two reads in one
    turn are one question. Without this every tool call in a turn re-paginates
    both member lists, which costs more than the history scan it guards.
    """
    workspace = _Workspace(
        members={_ORIGIN: {_ALICE, _BOT}, _TARGET: {_ALICE, _BOT}}
    )
    toolkit = _toolkit(workspace, HISTORY_MEMBERS)
    assert _read(toolkit, chat_id=_TARGET)["ok"] is True
    assert _read(toolkit, chat_id=_TARGET)["ok"] is True

    read = [call["channel"] for call in workspace.calls["conversations_members"]]
    assert sorted(read) == [_ORIGIN, _TARGET]


def test_the_cache_expires_so_a_removal_takes_effect() -> None:
    """It is the input to a refusal, so a stale copy is wrong for exactly that long.

    Sixty seconds is short enough that removing somebody from a channel takes
    effect while the person doing it is still watching.
    """
    clock = {"now": 1_000.0}
    workspace = _Workspace(
        members={_ORIGIN: {_ALICE, _BOT}, _PUBLIC: {_ALICE, _BOT}},
        public={_PUBLIC},
    )
    toolkit = SlackHistoryToolkit(
        metadata=_metadata(HISTORY_MEMBERS),
        client=workspace,
        now=lambda: 200_000.0,
        monotonic=lambda: clock["now"],
        max_user_lookups=0,
    )
    assert _read(toolkit, chat_id=_PUBLIC)["ok"] is True

    # Public so the second refusal still names the removed member, which is
    # what says the fresh list was read rather than the cached one.
    workspace.members[_PUBLIC] = {_ALICE}
    clock["now"] += 3_600.0
    result = _read(toolkit, chat_id=_PUBLIC)
    assert result["error"] == "source_members_not_in_target"
    assert _BOT in result["detail"]


def test_the_bound_this_cache_reads_ships_in_the_template() -> None:
    """A key honoured but not shipped is deleted from the operator's file.

    It is the one ``channels.slack`` key this gate reads, and it is a bound
    rather than the policy: how stale an input to a refusal may be, never who
    may read what.
    """
    import yaml

    root = Path(__file__).resolve().parents[3]
    data = yaml.safe_load(
        (root / "jiuwenswarm" / "resources" / "config.yaml").read_text()
    )
    assert "history_members_cache_seconds" in data["channels"]["slack"]


def test_the_toolkit_reads_the_names_the_connector_writes() -> None:
    """One declaration, imported here rather than restated.

    A rename would otherwise be a wire break with one symptom: the connector
    stamping one name, the gate reading another, and history quietly switching
    itself off.
    """
    from jiuwenswarm.common import slack_history_policy

    assert slack_history.METADATA_POLICY_KEY is slack_history_policy.METADATA_POLICY_KEY
    assert (
        slack_history.METADATA_NEVER_READ_KEY
        is slack_history_policy.METADATA_NEVER_READ_KEY
    )
    assert (
        slack_history.METADATA_EXEMPT_MEMBERS_KEY
        is slack_history_policy.METADATA_EXEMPT_MEMBERS_KEY
    )
    assert slack_history.METADATA_ASKER_KEY is slack_history_policy.METADATA_ASKER_KEY



@pytest.mark.parametrize(
    "key", [METADATA_NEVER_READ_KEY, METADATA_EXEMPT_MEMBERS_KEY]
)
def test_a_stamped_list_that_cannot_be_read_is_not_read_as_empty(key: str) -> None:
    """For a deny-list, empty means *nothing is denied* -- so it must be earned.

    A malformed ``history_never_read`` read as empty would silently stop carving
    anything out, which is the one direction a list of forbidden things must not
    fail in. Absent is still empty, because a deployment that wrote neither list
    is not an error.
    """
    workspace = _Workspace(
        members={_ORIGIN: {_ALICE, _BOT}, _TARGET: {_ALICE, _BOT}}
    )
    result = _read(
        _toolkit(workspace, HISTORY_MEMBERS, **{key: {"C0SECRET": True}}),
        chat_id=_TARGET,
    )
    assert result["error"] == "history_policy_list_malformed"
    assert workspace.calls == {} or "conversations_history" not in workspace.calls
