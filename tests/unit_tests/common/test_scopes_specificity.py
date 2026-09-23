# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.

"""The grain tuple: the table, the invariant behind it, and the ordering it changes.

Three kinds of test, and the second is the one that makes the scheme reviewable.

* The table, case by case, for the readings each grain level was written for.
* A property over generated rule pairs: wherever one rule's match set is
  provably contained in another's, the contained rule's grain is at least as
  fine. That is the whole of the design -- grain must never be finer for a wider
  match set -- and it is asserted over every pair the generator produces rather
  than over a list of examples somebody has to trust.
* The winner fixture: for a realistic list, which rule wins per conversation and
  key, computed under the count this replaces and under the tuple, asserted
  equal. A realistic config must not be reordered by a correction aimed at the
  cases it does not contain.

No connector is involved: the fake ``demo`` platform is registered here through
``register_channel``.
"""

from __future__ import annotations

import itertools
from typing import Any

import pytest

from jiuwenswarm.common.scopes import (
    AXIS_CHAT_TYPE,
    ChannelCapabilities,
    Scope,
    ScopeMatch,
    compile_scopes,
    matching_scopes,
    register_channel,
)
from jiuwenswarm.common.scopes import capabilities as caps


@pytest.fixture
def warnings() -> list[str]:
    return []


@pytest.fixture
def warn(warnings: list[str]):
    def record(message: str, *args: Any) -> None:
        warnings.append(message % args if args else message)

    return record


@pytest.fixture
def demo_channel():
    snapshot = caps.snapshot_registry()
    register_channel(
        ChannelCapabilities(
            channel="demo",
            axes=frozenset({"channel", "chat", "chat_type", "user"}),
            sections={"delivery": None, "agent": None},
            identity_keys=("user",),
            axis_values={AXIS_CHAT_TYPE: frozenset({"room", "direct"})},
        )
    )
    yield
    caps.restore_registry(snapshot)


def _compile(entries: Any, warn, **kwargs: Any) -> tuple[Scope, ...]:
    return compile_scopes(entries, warn=warn, **kwargs)


# --------------------------------------------------------------------------
# The table
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "match, expected",
    [
        (ScopeMatch(), (0, 0, 0, 0)),
        (ScopeMatch(channel="demo"), (1, 0, 0, 0)),
        (ScopeMatch(channel="demo", chat_type=("direct",)), (1, 0, 1, 0)),
        (ScopeMatch(channel="demo", chat="C1"), (1, 0, 2, 0)),
        (ScopeMatch(channel="demo", users=("U1",)), (1, 0, 0, 2)),
        (ScopeMatch(channel="demo", users=("U1",), roles=("admin",)), (1, 0, 0, 1)),
        (ScopeMatch(channel="demo", users=("U1", "U2"), roles=("admin",)), (1, 0, 0, 1)),
        (ScopeMatch(channel="demo", not_users=("U2",)), (1, 0, 0, 1)),
        (ScopeMatch(channel="demo", not_users=("U2",), not_roles=("admin",)), (1, 0, 0, 1)),
        (ScopeMatch(channel="demo", users=("U1",), not_users=("U2",)), (1, 0, 0, 2)),
        (ScopeMatch(channel="demo", users=("U1",), not_roles=("admin",)), (1, 0, 0, 2)),
        (ScopeMatch(channel="demo", chat="C1", users=("U1",)), (1, 0, 2, 2)),
        (ScopeMatch(chat="C1"), (0, 0, 2, 0)),
        (ScopeMatch(channel="demo", workspace="T1"), (1, 1, 0, 0)),
        (ScopeMatch(channel="demo", workspace="T1", chat="C1"), (1, 1, 2, 0)),
        (
            ScopeMatch(channel="demo", workspace="T1", users=("U1",)),
            (1, 1, 0, 2),
        ),
    ],
)
def test_the_grain_of_one_match(
    match: ScopeMatch, expected: tuple[int, int, int, int]
):
    assert match.specificity == expected


def test_a_role_that_resolves_to_nobody_keeps_the_grain_its_spelling_gives_it():
    """Grain is about what the rule is written to select, not about today's roles.

    A role holding nobody on this platform resolves to an empty ``users`` and
    matches nothing. Reading that extensionally -- the empty set is contained in
    everything, so it should be the finest grain there is -- would make a rule
    jump to the top of the order for having matched nobody, and drop back down
    the moment somebody joined the role.
    """
    empty_role = ScopeMatch(channel="demo", chat="C1", users=(), roles=("nobody",))

    assert empty_role.specificity == (1, 0, 2, 1)


def test_an_exemption_does_not_promote_the_rule_it_is_written_into():
    plain = ScopeMatch(channel="demo", chat="C1", users=("U1", "U2"))
    with_exemption = ScopeMatch(
        channel="demo", chat="C1", users=("U1", "U2"), not_users=("U2",)
    )

    assert plain.specificity == with_exemption.specificity


# --------------------------------------------------------------------------
# The invariant
# --------------------------------------------------------------------------
#
# A rule is generated as a spelling plus an interpretation of the one role it
# may name. The role's membership is not in the rule, so the subset test is
# taken over *every* interpretation: A is treated as contained in B only where
# it is contained for each possible membership of the role. That is what keeps
# the honest ties honest -- a role against a list of ids cannot be ordered, and
# the quantifier is where that fact enters the test rather than being asserted
# by hand.

#: Everyone the generated rules are matched against, plus the unidentified
#: sender, which is a request the matcher has a defined answer for.
_SENDERS = ("U1", "U2", "U3", "")

#: Two conversations of *each* kind, so that naming a kind is strictly wider
#: than naming a conversation of it, plus one whose kind the caller did not
#: supply, which is the fail-closed case. The second of each kind is what makes
#: the test able to tell the two grains apart: with one conversation per kind
#: the two spellings would have identical match sets and the coarser grain would
#: read as an inversion.
_CONVERSATIONS = (
    ("C1", "room"),
    ("C2", "room"),
    ("D1", "direct"),
    ("D2", "direct"),
    ("C3", None),
)

_CHANNELS = ("demo", "other")

#: Two workspaces, and every conversation below is reachable from both. That is
#: the matcher's own reading rather than a convenience: the axes AND
#: independently, and a conversation shared between two workspaces is a real
#: Slack Connect channel rather than a hypothetical. It is also what makes
#: ``{channel, workspace}`` and ``{channel, chat}`` incomparable here, which is
#: the fact the ordering between them rests on.
_WORKSPACES = ("T1", "T2")

#: Every membership the role could have, the empty one excluded. A role holding
#: nobody matches nobody, and the empty match set is contained in every other --
#: which would demand a grain finer than anything and is the degenerate reading
#: the scheme deliberately does not take. It has its own test above.
_ROLE_MEMBERSHIPS = tuple(
    frozenset(members)
    for size in (1, 2, 3)
    for members in itertools.combinations(("U1", "U2", "U3"), size)
)

#: (channel, workspace, chat, chat_type) -- the addressing half of a spelling.
#: ``chat`` and ``chat_type`` never appear together: the loader refuses the
#: pair, so a rule holding both is not a rule this scheme has to order.
#: ``workspace`` is free beside either, because the loader refuses no such pair
#: and a shared conversation makes the combination meaningful.
_CONVERSATION_SPELLINGS = tuple(
    (channel, workspace, chat, chat_type)
    for channel in (None, "demo")
    for workspace in (None, "T1")
    for chat, chat_type in (
        (None, None),
        (None, "room"),
        (None, "direct"),
        ("C1", None),
        ("D1", None),
    )
)

#: (written ids, role named) for the positive half and for the negative half.
#: ``None`` for the ids means the clause is absent; the role is named or not.
_IDENTITY_SPELLINGS = (
    (None, False),
    (("U1",), False),
    (("U1", "U2"), False),
    (None, True),
    (("U1",), True),
)


def _materialise(
    spelling: tuple[Any, ...], members: "frozenset[str]"
) -> ScopeMatch:
    """One spelling under one reading of the role, as the loader would leave it.

    ``_resolve_roles`` folds a role's ids into ``users`` at load and keeps the
    name for the description, so a faithful match holds both.
    """
    channel, workspace, chat, chat_type, (ids, role), (not_ids, not_role) = spelling

    def _fold(written, named):
        if written is None and not named:
            return None, None
        resolved = set(written or ())
        if named:
            resolved |= members
        return tuple(sorted(resolved)), (("admin",) if named else None)

    users, roles = _fold(ids, role)
    not_users, not_roles = _fold(not_ids, not_role)
    return ScopeMatch(
        channel=channel,
        workspace=workspace,
        chat=chat,
        chat_type=chat_type,
        users=users,
        not_users=not_users,
        roles=roles,
        not_roles=not_roles,
    )


def _match_set(match: ScopeMatch) -> frozenset[tuple[str, str, str, str]]:
    return frozenset(
        (channel, workspace, chat, sender)
        for channel in _CHANNELS
        for workspace in _WORKSPACES
        for chat, chat_type in _CONVERSATIONS
        for sender in _SENDERS
        if match.selects(
            channel=channel,
            chat=chat,
            chat_type=chat_type,
            workspace=workspace,
            user=sender,
        )
    )


def test_grain_is_never_finer_for_a_wider_match_set():
    spellings = [
        (channel, workspace, chat, chat_type, positive, negative)
        for channel, workspace, chat, chat_type in _CONVERSATION_SPELLINGS
        for positive in _IDENTITY_SPELLINGS
        for negative in _IDENTITY_SPELLINGS
    ]
    # Materialised once per (spelling, role membership) and reused across every
    # pair: the pairwise loop is quadratic and the matcher is not the thing
    # under test here.
    sets = {
        (index, members): _match_set(_materialise(spelling, members))
        for index, spelling in enumerate(spellings)
        for members in _ROLE_MEMBERSHIPS
    }
    grains = {
        index: _materialise(spelling, _ROLE_MEMBERSHIPS[0]).specificity
        for index, spelling in enumerate(spellings)
    }

    checked = 0
    for a, b in itertools.permutations(range(len(spellings)), 2):
        contained = True
        for members in _ROLE_MEMBERSHIPS:
            left = sets[(a, members)]
            # An empty match set is contained in everything, and a rule that
            # selects nobody is still a rule about the people it names. Skipped
            # rather than asserted on, for the reason the empty role has.
            if not left or not left <= sets[(b, members)]:
                contained = False
                break
        if not contained:
            continue
        checked += 1
        assert grains[a] >= grains[b], (
            f"{spellings[a]} is contained in {spellings[b]} but is graded"
            f" {grains[a]} against {grains[b]}"
        )

    # The generator has to actually produce nested pairs, or the loop above
    # passes by finding nothing to check.
    assert checked > 200


def test_the_generator_covers_the_orderings_the_table_claims():
    """The pairs the invariant is interesting for, named so the loop is legible.

    Each is a spelling whose match set is contained in the other's under every
    reading of the role, which is what the loop above searches for in bulk.
    """
    room = ScopeMatch(channel="demo", chat="C1")
    kind = ScopeMatch(channel="demo", chat_type=("room",))
    ids = ScopeMatch(channel="demo", users=("U1",))
    mixed = ScopeMatch(channel="demo", users=("U1",), roles=("admin",))

    assert _match_set(room) < _match_set(kind)
    assert room.specificity > kind.specificity

    assert _match_set(ids) < _match_set(ScopeMatch(channel="demo"))
    assert ids.specificity > ScopeMatch(channel="demo").specificity

    # The union contains the half that is written out, so the half must not be
    # graded coarser than the union it sits inside.
    assert _match_set(ids) <= _match_set(mixed)
    assert ids.specificity >= mixed.specificity

    # The workspace nests under the platform and is nested into by a rule that
    # names both it and a conversation, which is the ladder the table draws.
    workspace = ScopeMatch(channel="demo", workspace="T1")
    inside = ScopeMatch(channel="demo", workspace="T1", chat="C1")
    assert _match_set(workspace) < _match_set(ScopeMatch(channel="demo"))
    assert workspace.specificity > ScopeMatch(channel="demo").specificity
    assert _match_set(inside) < _match_set(workspace)
    assert inside.specificity > workspace.specificity

    # And the pair the ordering had to be *decided* for: neither contains the
    # other, so the invariant above says nothing about them and the tuple puts
    # the coarser-sounding axis first on purpose.
    assert not _match_set(workspace) <= _match_set(room)
    assert not _match_set(room) <= _match_set(workspace)
    assert workspace.specificity > room.specificity


# --------------------------------------------------------------------------
# The winner fixture
# --------------------------------------------------------------------------


def _counted_specificity(match: ScopeMatch) -> int:
    """The scheme this replaces: how many axes the match names, identity once.

    Kept here rather than in the module because nothing should compute it any
    more. It exists so the change can be asserted to be a no-op on a config that
    does not contain the cases it corrects.
    """
    return sum(
        1
        for present in (
            match.channel is not None,
            match.chat is not None or match.chat_type is not None,
            match.constrains_identity,
        )
        if present
    )


#: A realistic list, in the shape the config template documents: a platform
#: rule, a conversation with a model and a prompt, a second conversation with a
#: standing instruction, one sender inside it, and everybody else inside it.
_REALISTIC = [
    {
        "match": {"channel": "demo"},
        "delivery": {"mode": ["mention", "url"]},
    },
    {
        "match": {"channel": "demo", "chat": "C1"},
        "delivery": {"mode": ["+has_file"], "prompt": "Answer in English."},
        "agent": {"model_name": "fast"},
    },
    {
        "match": {"channel": "demo", "chat": "C2"},
        "delivery": {"prompt_append": "Be terse.", "mid_turn": "queue"},
    },
    {
        "match": {"channel": "demo", "chat": "C2", "user": ["U1"]},
        "delivery": {"prompt_append": "Answer this person in French."},
    },
    {
        "match": {"channel": "demo", "chat": "C2", "not": {"user": ["U1"]}},
        "delivery": {"mode": ["mention"]},
    },
]


def _winners(
    scopes: tuple[Scope, ...], key: Any
) -> dict[tuple[str, str, str], dict[str, int]]:
    """Which rule settled each key, per conversation and sender, under ``key``."""
    settled: dict[tuple[str, str, str], dict[str, int]] = {}
    for chat, chat_type in (("C1", "room"), ("C2", "room"), ("D1", "direct")):
        for sender in ("U1", "U2", ""):
            applicable = [
                scope
                for scope in scopes
                if scope.section("delivery")
                and scope.selects(
                    channel="demo", chat=chat, chat_type=chat_type, user=sender
                )
            ]
            applicable.sort(key=key)
            per_key: dict[str, int] = {}
            for scope in applicable:
                for name in scope.section("delivery"):
                    per_key[name] = scope.index
            settled[(chat, chat_type, sender)] = per_key
    return settled


def test_the_tuple_reorders_nothing_in_a_realistic_list(demo_channel, warn, warnings):
    scopes = _compile(_REALISTIC, warn)

    assert warnings == []
    counted = _winners(scopes, lambda scope: (_counted_specificity(scope.match), scope.index))
    grained = _winners(scopes, lambda scope: (scope.layer, scope.index))

    assert counted == grained


def test_the_tuple_does_reorder_the_pair_the_count_got_wrong(
    demo_channel, warn, warnings
):
    """The correction, stated as the case it corrects.

    Under the count these two tied and the later line won. They are not a tie: a
    role is a set whose size lives in ``roles:``, so nothing in the second rule
    says it is narrower than the first, and letting file position decide made
    the answer depend on which was typed last.
    """
    scopes = _compile(
        [
            {
                "match": {"channel": "demo", "chat": "C1", "user": ["U1"]},
                "delivery": {"prompt": "by id"},
            },
            {
                "match": {"channel": "demo", "chat": "C1", "role": "admin"},
                "delivery": {"prompt": "by role"},
            },
        ],
        warn,
        people={"alice": {"demo": "U1"}},
        roles={"admin": ["alice"]},
    )

    assert [_counted_specificity(scope.match) for scope in scopes] == [3, 3]
    assert [scope.layer for scope in scopes] == [(1, 0, 2, 2), (1, 0, 2, 1)]

    ordered = matching_scopes(scopes, channel="demo", chat="C1", user="U1")
    assert ordered[-1].section("delivery") == {"prompt": "by id"}
