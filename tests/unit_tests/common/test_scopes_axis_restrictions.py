# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.

"""Per-key restrictions on *how a rule may be addressed*.

A ``match`` says which requests a rule is about; a section says what happens to
them. Most keys do not care how the rule was addressed, and those keys have no
entry anywhere -- absent is unrestricted, and ``delivery.prompt`` is the example
that stays absent on purpose.

A key earns an entry when its own meaning already accounts for an axis, so that
a rule addressing that axis as well is either inert or a second and *static*
answer to a question something else answers dynamically per request. ``clicks``
is that case for a whole section: a click has its own principal, which is
not the sender the match selects on.

A key earns one for a second reason too, and ``permissions`` is that case: its
consumer is handed a channel and a chat and no sender at all, so an identity
axis there is read by nobody. That row is provisional and comes out when the
runtime has an identity; until then the tests below pin it, because the
failure it prevents is silent in both directions -- a rule naming people that
fires for none of them, and a ``not:`` exemption that exempts none of them.

Three properties are pinned here and none of them is about any one connector:

* an allow-list, so an axis added later is barred until somebody permits it;
* two tables -- the schema's and each channel's -- intersected, so a connector
  narrows and can never widen;
* a refusal that names the rule, the axis, and the spelling the author wrote.

``warn`` is injected rather than read off the logger: jiuwenswarm's loggers do
not propagate, so ``caplog`` sees nothing and a test written against it would
assert nothing where it matters. The two warnings that are *not* an operator's
mistake go to the module logger instead, and those are read with a handler
attached to that logger for the same reason.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Any, Iterator

import pytest

from jiuwenswarm.common.scopes import (
    ANY_KEY,
    AXIS_CHANNEL,
    AXIS_CHAT,
    AXIS_CHAT_TYPE,
    AXIS_ROLE,
    AXIS_USER,
    AXIS_WORKSPACE,
    MATCH_AXIS_RESTRICTIONS,
    SECTION_AGENT,
    SECTION_CLICKS,
    SECTION_PERMISSIONS,
    SUPPORTED_AXES,
    AxisRestriction,
    ChannelCapabilities,
    compile_scopes,
    register_channel,
    restriction_in,
)
from jiuwenswarm.common.scopes import capabilities as caps

_CAPABILITIES_LOGGER = "jiuwenswarm.common.scopes.capabilities"
_SCHEMA_LOGGER = "jiuwenswarm.common.scopes.schema"

PEOPLE = {"alice": {"restricted": "U_ALICE"}}
ROLES = {"operator": ["alice"]}


@pytest.fixture
def warnings() -> list[str]:
    return []


@pytest.fixture
def warn(warnings: list[str]):
    def record(message: str, *args: Any) -> None:
        warnings.append(message % args if args else message)

    return record


@contextmanager
def _captured(logger_name: str) -> Iterator[list[logging.LogRecord]]:
    """Records taken off one logger directly.

    Not ``caplog``: the handler pytest installs sits on the root logger, so what
    it sees depends on whether this package's loggers propagate -- a property of
    whatever configured logging first rather than of the code under test.
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


@pytest.fixture(autouse=True)
def restricted():
    """A synthetic channel with one restricted key and one unrestricted one.

    Synthetic rather than Slack, because this file is the connector-agnostic
    half: the mechanism belongs to every channel that declares a restriction,
    and asserting it against the real declaration would couple it to one
    connector's key list.
    """
    snapshot = caps.snapshot_registry()
    register_channel(
        ChannelCapabilities(
            channel="restricted",
            # ``workspace`` is populated here on purpose. The refusals below are
            # then about what the *key* can be addressed on, and not about a
            # channel that cannot fill the axis -- which is a different
            # mechanism, with a different warning, one function over.
            axes=frozenset(
                {
                    AXIS_CHANNEL,
                    AXIS_WORKSPACE,
                    AXIS_CHAT,
                    AXIS_CHAT_TYPE,
                    AXIS_USER,
                }
            ),
            sections={
                "delivery": None,
                SECTION_AGENT: frozenset({"gated", "ungated"}),
                "permissions": None,
                SECTION_CLICKS: None,
            },
            identity_keys=("user",),
            axis_values={AXIS_CHAT_TYPE: frozenset({"room", "direct"})},
            axis_restrictions={
                f"{SECTION_AGENT}.gated": AxisRestriction.only_on(
                    AXIS_CHANNEL,
                    because="Who is asking is already settled per request",
                ),
            },
        )
    )
    yield snapshot
    caps.restore_registry(snapshot)


def _compile(entry: dict[str, Any], warn, **kw: Any):
    return compile_scopes([entry], people=PEOPLE, roles=ROLES, warn=warn, **kw)


# --------------------------------------------------------------------------
# Accepting a legal rule, refusing an illegal one
# --------------------------------------------------------------------------


def test_a_restricted_key_is_kept_on_the_axis_it_permits(warn, warnings):
    scopes = _compile(
        {"match": {"channel": "restricted"}, "agent": {"gated": "on"}}, warn
    )

    assert len(scopes) == 1
    assert scopes[0].section(SECTION_AGENT) == {"gated": "on"}
    assert not [line for line in warnings if "cannot appear in a rule" in line]


@pytest.mark.parametrize(
    ("match", "axis"),
    [
        ({"channel": "restricted", "chat": "C1"}, AXIS_CHAT),
        ({"channel": "restricted", "chat_type": "room"}, AXIS_CHAT_TYPE),
        ({"channel": "restricted", "user": ["U_ALICE"]}, AXIS_USER),
        ({"channel": "restricted", "not": {"user": ["U_ALICE"]}}, AXIS_USER),
    ],
)
def test_a_restricted_key_is_refused_on_an_axis_it_bars(match, axis, warn, warnings):
    """Including the negative half, which is the same axis written backwards.

    ``not`` names no dimension of its own, so barring ``user`` bars
    ``not: {user: ...}`` with it and needs no row of its own anywhere.

    ``chat_type`` is in the list for the property the file opens with: an
    allow-list refuses an axis added later until somebody permits it, and this
    declaration was written before the axis existed. Nothing in it was edited to
    make the refusal happen.
    """
    scopes = _compile({"match": match, "agent": {"gated": "on"}}, warn)

    assert scopes == ()
    assert [
        line
        for line in warnings
        if f"scopes[0].agent.gated cannot appear in a rule matching on {axis}" in line
    ]


def test_the_refusal_names_the_rule_the_key_and_the_permitted_axes(warn, warnings):
    scopes = _compile(
        {
            "match": {"channel": "restricted", "chat": "C1"},
            "agent": {"gated": "on", "ungated": "fine"},
        },
        warn,
    )

    (line,) = [entry for entry in warnings if "cannot appear in a rule" in entry]
    # The rule, by the position an operator can count to in their file.
    assert line.startswith("scopes[0].agent.gated ")
    # The axis, by name.
    assert "matching on chat" in line
    # What to write instead, so the warning is actionable without the design.
    assert "It may be addressed on channel" in line
    # And the reason the declaration gave, kept rather than composed here.
    assert "Who is asking is already settled per request" in line
    # The key alone is dropped, not the rule: the sibling key is legal and
    # survives, which is what stops one refused key taking settings with it.
    assert len(scopes) == 1
    assert scopes[0].section(SECTION_AGENT) == {"ungated": "fine"}


def test_the_refusal_names_the_spelling_the_author_wrote(warn, warnings):
    """A role-only rule is reported as ``role``, never as the ids it folds to.

    Resolution turns a ``role`` into the ``user`` ids it holds, so a check taken
    after it would name a ``user:`` line that is not in the operator's file.
    """
    scopes = _compile(
        {"match": {"channel": "restricted", "role": "operator"}, "agent": {"gated": "x"}},
        warn,
    )

    (line,) = [entry for entry in warnings if "cannot appear in a rule" in entry]
    assert "matching on role" in line
    assert "matching on user" not in line
    assert scopes == ()


def test_every_barred_axis_is_named_rather_than_the_first(warn, warnings):
    """Two things to fix is two things to say, or the operator comes back."""
    _compile(
        {
            "match": {"channel": "restricted", "chat": "C1", "user": ["U_ALICE"]},
            "agent": {"gated": "on"},
        },
        warn,
    )

    (line,) = [entry for entry in warnings if "cannot appear in a rule" in entry]
    assert "matching on chat, user" in line


def test_an_append_is_restricted_exactly_as_the_key_it_appends_to(warn, warnings):
    """An append is a way of writing a key, not a second key."""
    scopes = _compile(
        {"match": {"channel": "restricted", "chat": "C1"}, "agent": {"gated_append": "x"}},
        warn,
    )

    assert scopes == ()
    assert [
        line
        for line in warnings
        if "scopes[0].agent.gated_append cannot appear in a rule matching on chat" in line
    ]


def test_an_unrestricted_key_is_addressable_on_every_axis(warn, warnings):
    """Absent is unrestricted, and most keys are absent.

    ``agent.ungated`` stands for the whole class here: nothing about it accounts
    for an axis, and its consumer is given every axis the match names, so a
    per-person rule setting it is a legitimate thing to write and must stay
    writable.
    """
    scopes = _compile(
        {
            "match": {"channel": "restricted", "chat": "C1", "user": ["U_ALICE"]},
            "agent": {"ungated": "x"},
            "delivery": {"prompt": "Be terse."},
        },
        warn,
    )

    assert len(scopes) == 1
    assert scopes[0].section(SECTION_AGENT) == {"ungated": "x"}
    assert scopes[0].section("delivery") == {"prompt": "Be terse."}
    assert not [line for line in warnings if "cannot appear in a rule" in line]


# --------------------------------------------------------------------------
# permissions: barred because its consumer is handed no sender
# --------------------------------------------------------------------------


def test_permissions_is_kept_on_a_rule_addressing_the_conversation(warn, warnings):
    """The axes the permission hook is actually given, and the section survives."""
    scopes = _compile(
        {
            "match": {"channel": "restricted", "chat": "C1"},
            "permissions": {"tools": {"Bash": "deny"}},
        },
        warn,
    )

    assert len(scopes) == 1
    assert scopes[0].section(SECTION_PERMISSIONS) == {"tools": {"Bash": "deny"}}
    assert not [line for line in warnings if "cannot appear in a rule" in line]


@pytest.mark.parametrize(
    ("match", "axis"),
    [
        ({"channel": "restricted", "chat": "C1", "user": ["U_ALICE"]}, AXIS_USER),
        ({"channel": "restricted", "chat": "C1", "role": "operator"}, AXIS_ROLE),
        (
            {"channel": "restricted", "chat": "C1", "not": {"user": ["U_ALICE"]}},
            AXIS_USER,
        ),
    ],
)
def test_permissions_is_refused_on_an_identity_axis(match, axis, warn, warnings):
    """Both halves of the identity axis, and both are silent failures.

    The hook evaluates every one of these with ``user=None``. Positively
    addressed, the rule then fires for nobody and the narrowing an operator
    wrote is absent. Negatively addressed, it fires for everybody including the
    people the ``not:`` names -- inverted rather than ignored, which is the
    worse of the two and the reason the bar is a refusal rather than a note.
    """
    scopes = _compile({"match": match, "permissions": {"tools": {"Bash": "deny"}}}, warn)

    assert scopes == ()
    (line,) = [entry for entry in warnings if "cannot appear in a rule" in entry]
    assert line.startswith(f"scopes[0].permissions cannot appear in a rule matching on {axis}")
    assert "is given the channel and the chat alone" in line
    assert "It may be addressed on channel, chat" in line


# --------------------------------------------------------------------------
# The existing clicks bar, as one rule rather than two
# --------------------------------------------------------------------------


def test_the_clicks_bar_is_a_row_in_the_shared_table(warn, warnings):
    """One mechanism, not a hand-written branch beside a table that agrees.

    The bar on a rule naming a sender is a row here rather than a branch of
    its own, read from the same place as every other, which is what stops the
    two drifting into disagreeing about one section.
    """
    restriction = restriction_in(MATCH_AXIS_RESTRICTIONS, SECTION_CLICKS, "approve")

    assert restriction is not None
    assert restriction.allowed == frozenset({AXIS_CHANNEL, AXIS_CHAT})

    scopes = _compile(
        {
            "match": {"channel": "restricted", "chat": "C1", "user": ["U_ALICE"]},
            "clicks": {"approve": {"user": ["U_ALICE"]}, "stop": {"user": ["U_ALICE"]}},
        },
        warn,
    )

    assert scopes == ()
    refusals = [line for line in warnings if "cannot appear in a rule" in line]
    # One line for the section, not one per gesture: a section-wide entry drops
    # the section, which is what the hand-written branch did.
    assert len(refusals) == 1
    assert refusals[0].startswith("scopes[0].clicks cannot appear in a rule matching on user")
    # The row's own reason holds the sentence, so an operator reads the same
    # wording whichever mechanism refused it.
    assert "has no effect on a rule that also names a sender" in refusals[0]


# --------------------------------------------------------------------------
# Where the workspace falls
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("section", "body"),
    [
        (SECTION_PERMISSIONS, {"tools": {"Bash": "deny"}}),
        (SECTION_CLICKS, {"approve": {"user": ["U_ALICE"]}}),
    ],
)
def test_a_key_addressable_on_the_conversation_is_not_satisfied_by_the_workspace(
    section, body, warn, warnings
):
    """A workspace is coarser than a conversation, and does not stand in for one.

    Both entries permit ``channel`` and ``chat``, and both permit them for the
    same reason: that is the grain their consumer is handed. A click arrives
    with the conversation it was pressed in; the permission hook is given a
    channel and a chat. Neither is given a workspace, so a rule naming one is
    inert -- the narrowing an operator wrote is absent and nothing in the config
    shows it, which is the failure these entries exist to convert into a line.

    The channel here populates the axis, so this is a statement about the key
    rather than about the platform.
    """
    scopes = _compile(
        {
            "match": {"channel": "restricted", "workspace": "T_ACME"},
            section: body,
        },
        warn,
    )

    assert scopes == ()
    (line,) = [entry for entry in warnings if "cannot appear in a rule" in entry]
    assert line.startswith(
        f"scopes[0].{section} cannot appear in a rule matching on {AXIS_WORKSPACE}"
    )
    assert "It may be addressed on channel, chat" in line


def test_the_workspace_was_barred_by_the_allow_list_and_by_nobody_editing_it():
    """The fail-closed shape doing the thing it is there for.

    Neither entry was touched when the axis was added, and neither had to be:
    an allow-list permits what it names, so an axis nobody has permitted is
    refused. A deny-list would have admitted ``workspace`` into both sections
    silently, and the only evidence would have been a rule that never fires.
    """
    for section in (SECTION_CLICKS, SECTION_PERMISSIONS):
        restriction = restriction_in(MATCH_AXIS_RESTRICTIONS, section, ANY_KEY)
        assert restriction is not None
        assert restriction.allowed == frozenset({AXIS_CHANNEL, AXIS_CHAT})
        assert not restriction.permits(AXIS_WORKSPACE)
        assert restriction.refuses({AXIS_CHANNEL, AXIS_WORKSPACE}) == (
            AXIS_WORKSPACE,
        )


def test_an_unrestricted_key_may_still_be_addressed_on_the_workspace(warn, warnings):
    """Absent is unrestricted, and most keys are absent.

    The bar above is a statement about two sections whose consumers are handed
    two ids. It is not a statement about the axis: a key that says what to do
    with a request, and nothing about how the rule was addressed, takes a
    workspace as readily as it takes a channel.
    """
    scopes = _compile(
        {
            "match": {"channel": "restricted", "workspace": "T_ACME"},
            "delivery": {"prompt": "the ACME workspace"},
            SECTION_AGENT: {"ungated": "fine"},
        },
        warn,
    )

    assert len(scopes) == 1
    assert scopes[0].section(SECTION_AGENT) == {"ungated": "fine"}
    assert not [line for line in warnings if "cannot appear in a rule" in line]


def test_a_clicks_rule_on_the_conversation_alone_is_untouched(warn, warnings):
    scopes = _compile(
        {
            "match": {"channel": "restricted", "chat": "C1"},
            "clicks": {"approve": {"user": ["U_ALICE"]}},
        },
        warn,
    )

    assert len(scopes) == 1
    assert scopes[0].section(SECTION_CLICKS)["approve"].users == ("U_ALICE",)
    assert not [line for line in warnings if "cannot appear in a rule" in line]


# --------------------------------------------------------------------------
# Two tables, intersected
# --------------------------------------------------------------------------


def test_a_channel_cannot_widen_what_the_schema_bars(warn, warnings):
    """Intersection, so "narrow only" is true by construction.

    There is no precedence to get the wrong way round and no rule anybody has to
    remember: a connector permitting an axis the schema bars simply does not get
    it.
    """
    snapshot = caps.snapshot_registry()
    register_channel(
        ChannelCapabilities(
            channel="overreaching",
            axes=frozenset({AXIS_CHANNEL, AXIS_CHAT, AXIS_USER}),
            sections={SECTION_CLICKS: None},
            identity_keys=("user",),
            axis_restrictions={
                f"{SECTION_CLICKS}.{ANY_KEY}": AxisRestriction.only_on(
                    AXIS_CHANNEL, AXIS_CHAT, AXIS_USER
                )
            },
        )
    )
    try:
        scopes = _compile(
            {
                "match": {"channel": "overreaching", "user": ["U_ALICE"]},
                "clicks": {"approve": {"user": ["U_ALICE"]}},
            },
            warn,
        )
    finally:
        caps.restore_registry(snapshot)

    assert scopes == ()
    (line,) = [entry for entry in warnings if "cannot appear in a rule" in entry]
    assert line.startswith("scopes[0].clicks cannot appear in a rule matching on user")
    # And the schema's reason is the one given, because the schema is the side
    # that did the barring: the intersection is what it permits, not what the
    # channel asked for.
    assert "has no effect on a rule that also names a sender" in line


def test_a_channel_may_narrow_what_the_schema_leaves_open(warn, warnings):
    """The direction that is allowed, and the one the design asks for."""
    snapshot = caps.snapshot_registry()
    register_channel(
        ChannelCapabilities(
            channel="narrowing",
            axes=frozenset({AXIS_CHANNEL, AXIS_CHAT}),
            sections={SECTION_CLICKS: None},
            axis_restrictions={
                f"{SECTION_CLICKS}.{ANY_KEY}": AxisRestriction.only_on(AXIS_CHANNEL)
            },
        )
    )
    try:
        scopes = _compile(
            {
                "match": {"channel": "narrowing", "chat": "C1"},
                "clicks": {"approve": {"user": ["U_ALICE"]}},
            },
            warn,
        )
    finally:
        caps.restore_registry(snapshot)

    assert scopes == ()
    assert [line for line in warnings if "clicks cannot appear in a rule matching on chat" in line]


def test_a_section_wide_entry_and_a_per_key_one_both_apply():
    """Both are true statements, so they intersect rather than override.

    A per-key entry from the same file must not be a way to widen a section-wide
    one written above it.
    """
    table = {
        f"{SECTION_AGENT}.{ANY_KEY}": AxisRestriction.only_on(AXIS_CHANNEL, AXIS_CHAT),
        f"{SECTION_AGENT}.gated": AxisRestriction.only_on(AXIS_CHANNEL, AXIS_USER),
    }

    settled = restriction_in(table, SECTION_AGENT, "gated")

    assert settled is not None
    assert settled.allowed == frozenset({AXIS_CHANNEL})


# --------------------------------------------------------------------------
# Fail closed
# --------------------------------------------------------------------------


def test_an_allow_list_bars_an_axis_nobody_has_permitted():
    """The shape, and the reason there is no ``excluding()`` constructor.

    A deny-list would silently admit an axis added later; an allow-list silently
    refuses it. Only the second is found by reading a warning.
    """
    restriction = AxisRestriction.only_on(AXIS_CHANNEL)

    for axis in SUPPORTED_AXES:
        assert restriction.permits(axis) is (axis == AXIS_CHANNEL)
    assert restriction.permits("an_axis_invented_next_year") is False


def test_an_empty_allow_list_bars_every_addressed_rule(warn, warnings):
    """Empty is a statement; absent is the absence of one. They differ."""
    snapshot = caps.snapshot_registry()
    register_channel(
        ChannelCapabilities(
            channel="sealed",
            axes=frozenset({AXIS_CHANNEL}),
            sections={SECTION_AGENT: frozenset({"gated"})},
            axis_restrictions={f"{SECTION_AGENT}.gated": AxisRestriction()},
        )
    )
    try:
        scopes = _compile(
            {"match": {"channel": "sealed"}, "agent": {"gated": "x"}}, warn
        )
    finally:
        caps.restore_registry(snapshot)

    assert scopes == ()
    (line,) = [entry for entry in warnings if "cannot appear in a rule" in entry]
    assert "It may be addressed on nothing" in line


@pytest.mark.parametrize("axis", [5, "", "   ", None])
def test_a_malformed_declaration_refuses_to_be_built(axis):
    """Raising, which nothing an operator writes can reach.

    The failure it produces is a state this design already has a meaning for:
    the connector's declaration module does not import, so it is not opted in,
    and scopes naming it warn as inert. Warning instead would leave a
    restriction that reads as enforced and is not.
    """
    with pytest.raises(ValueError):
        AxisRestriction.only_on(axis)  # type: ignore[arg-type]


def test_an_axis_name_that_is_not_an_axis_is_reported_and_fails_closed(warn, warnings):
    """A typo in a declaration permits nothing, which is the safe direction.

    Reported on the module logger all the same: without it the only evidence is
    a refusal of a rule that looks correct, and the audience for the fix is
    whoever is editing the declaration rather than whoever wrote the config.
    """
    snapshot = caps.snapshot_registry()
    register_channel(
        ChannelCapabilities(
            channel="typoed",
            axes=frozenset({AXIS_CHANNEL}),
            sections={SECTION_AGENT: frozenset({"gated"})},
            axis_restrictions={
                f"{SECTION_AGENT}.gated": AxisRestriction.only_on("chanel")
            },
        )
    )
    try:
        with _captured(_SCHEMA_LOGGER) as records:
            scopes = _compile(
                {"match": {"channel": "typoed"}, "agent": {"gated": "x"}}, warn
            )
    finally:
        caps.restore_registry(snapshot)

    assert scopes == ()
    assert [line for line in warnings if "cannot appear in a rule matching on channel" in line]
    assert [
        record
        for record in records
        if "permits chanel, which is not an axis" in record.getMessage() % record.args
    ]


def test_a_restriction_on_a_key_the_channel_never_reads_is_reported():
    """It enforces nothing: the key is dropped earlier, by ``acts_on``.

    In the declaration it reads as a live protection, which is the worst way for
    one to be absent -- so the file is told, on the logger whose audience is
    whoever is editing it.
    """
    snapshot = caps.snapshot_registry()
    try:
        with _captured(_CAPABILITIES_LOGGER) as records:
            register_channel(
                ChannelCapabilities(
                    channel="dangling",
                    axes=frozenset({AXIS_CHANNEL}),
                    sections={SECTION_AGENT: frozenset({"declared"})},
                    axis_restrictions={
                        f"{SECTION_AGENT}.undeclared": AxisRestriction.only_on(
                            AXIS_CHANNEL
                        )
                    },
                )
            )
    finally:
        caps.restore_registry(snapshot)

    assert [
        record
        for record in records
        if "does not name undeclared among the agent keys it acts on"
        in record.getMessage() % record.args
    ]


# --------------------------------------------------------------------------
# The default is what shipped
# --------------------------------------------------------------------------


def test_the_shared_table_holds_exactly_the_rows_that_were_argued_for():
    """The whole of the behaviour change, stated as a set.

    Two rows. ``clicks`` is the bar it already had, written as a table row
    rather than a hand-written branch. ``permissions`` is a real change to what
    an existing config does, and it is a refusal of rules that were doing
    nothing -- or the opposite of what they said -- in either case silently.

    Anything else added here changes what an existing config does too, so this
    test is where that has to be argued for rather than noticed.
    """
    assert set(MATCH_AXIS_RESTRICTIONS) == {
        f"{SECTION_CLICKS}.{ANY_KEY}",
        f"{SECTION_PERMISSIONS}.{ANY_KEY}",
    }


def test_no_key_that_already_existed_has_gained_a_restriction(restricted):
    """No existing deployment's config compiles differently because of this.

    Stated as the set of keys writable on every axis rather than as "no channel
    declares anything", so that it goes on
    saying something once a *new* key declares one: a restriction may only ever
    arrive with the key it restricts.

    ``restricted`` is the fixture's pre-registration snapshot -- the real
    connectors as discovery found them, without this file's synthetic ones.

    About connector declarations only. ``permissions.tools`` is still on the
    list because no connector may declare a restriction on it; the schema's own
    table now bars the identity axes there, which is the one place that change
    is argued for and is asserted two tests above.
    """
    already_writable = {
        "delivery.mode",
        "delivery.prompt",
        "delivery.mid_turn",
        "agent.model_name",
        "permissions.tools",
    }

    for name, declared in restricted.items():
        for entry in declared.axis_restrictions:
            assert entry not in already_writable, f"{name}: {entry}"


def test_a_config_using_every_section_is_unchanged(warn, warnings):
    """The default path: nothing an existing config writes is newly refused."""
    scopes = compile_scopes(
        [
            {
                "match": {"channel": "restricted"},
                "delivery": {"mode": ["mention"]},
                "agent": {"ungated": "m"},
            },
            {
                "match": {"channel": "restricted", "chat": "C1", "user": ["U_ALICE"]},
                "agent": {"ungated": "theirs"},
            },
            {
                # On the conversation, because that is the whole of what the
                # permission hook is given to settle it with.
                "match": {"channel": "restricted", "chat": "C1"},
                "permissions": {"tools": {"Bash": "ask"}},
            },
            {
                "match": {"channel": "restricted", "chat": "C1"},
                "clicks": {"stop": {"role": "operator"}},
            },
        ],
        people=PEOPLE,
        roles=ROLES,
        warn=warn,
    )

    assert len(scopes) == 4
    assert not [line for line in warnings if "cannot appear in a rule" in line]
