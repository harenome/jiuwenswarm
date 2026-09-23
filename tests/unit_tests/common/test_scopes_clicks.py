# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.

"""The ``clicks`` section: who may press a button, and what happens when nobody knows.

A click is not a turn. It arrives against a turn that already exists, from
someone who may not have started it, and it has its own principal. So it gets
a section of its own rather than a key in ``delivery``, and the section
is matched on the conversation rather than on the sender -- the question is who
may press this button *here*.

Three clauses decide what a click may do, and the tests below are grouped by
them:

* **No rule matches**, so layer 0 decides. Absence of a rule is spelled by
  there being no rule, never by an empty one, and nothing in this section may
  change what a config with no ``clicks:`` block does.
* **A rule matches and the clicker cannot be identified**, so the click is
  refused. Two ways to be unidentifiable: no id arrived at all, or one arrived
  and the rule names a role no ``people:`` entry maps it into.
* **The starter's allowance** lives on the connector, not here: it needs a
  recorded starter.

And one distinction that is not a clause and is easy to lose: **no surface** is
not **no identity**. A rule on a channel that renders no buttons is a rule
written against the wrong channel and warns as having no effect; a rule on a
channel that renders them and names nobody is a rule that would have applied had
the fact it needed arrived, and refuses every click.

``warn`` is injected rather than read off the logger: jiuwenswarm's loggers do
not propagate, so ``caplog`` sees nothing and a test written against it would
assert nothing where it matters.
"""

from __future__ import annotations

from typing import Any

import pytest

from jiuwenswarm.common.scopes import (
    CLICK_APPROVE,
    CLICK_STOP,
    SECTION_CLICKS,
    SUPPORTED_SECTIONS,
    ChannelCapabilities,
    ClickRule,
    click_rule,
    compile_scopes,
    register_channel,
    scoped_chats,
)
from jiuwenswarm.common.scopes import capabilities as caps

PEOPLE = {
    "alice": {"clicky": "U_ALICE"},
    "bob": {"clicky": "U_BOB"},
    "carol": {"elsewhere": "X_CAROL"},
}
ROLES = {"operator": ["alice"], "member": ["alice", "bob"], "offsite": ["carol"]}


@pytest.fixture
def warnings() -> list[str]:
    return []


@pytest.fixture
def warn(warnings: list[str]):
    def record(message: str, *args: Any) -> None:
        warnings.append(message % args if args else message)

    return record


@pytest.fixture(autouse=True)
def clicky():
    """A channel that renders buttons and names who pressed them.

    Synthetic rather than Slack, because this file is the connector-agnostic
    half: the section, its composition and its refusals belong to every channel
    that renders a button, and asserting them against the real declaration would
    couple them to one connector's key list.
    """
    snapshot = caps.snapshot_registry()
    register_channel(
        ChannelCapabilities(
            channel="clicky",
            axes=frozenset({"channel", "chat", "user"}),
            sections={"delivery": None, SECTION_CLICKS: None},
            identity_keys=("user",),
        )
    )
    register_channel(
        ChannelCapabilities(
            # Renders buttons, names nobody. Not a hypothetical: some
            # connectors leave the sender in metadata, and the pseudo-channels
            # ship ``identity_keys=()``.
            channel="anonymous",
            axes=frozenset({"channel", "chat"}),
            sections={SECTION_CLICKS: None},
        )
    )
    register_channel(
        # Renders none. The other half of the distinction.
        ChannelCapabilities(
            channel="silent",
            axes=frozenset({"channel"}),
            sections={"permissions": None},
        )
    )
    yield
    caps.restore_registry(snapshot)


def _compile(entry: dict[str, Any], warn, **kw: Any):
    return compile_scopes([entry], people=PEOPLE, roles=ROLES, warn=warn, **kw)


# --------------------------------------------------------------------------
# The section
# --------------------------------------------------------------------------


def test_clicks_is_a_section_the_loader_knows(warn, warnings):
    # It used to warn as an unknown key, which said the block was a typo rather
    # than a feature: section 13's Q6, dissolved by the section existing.
    assert SECTION_CLICKS in SUPPORTED_SECTIONS

    scopes = _compile(
        {"match": {"channel": "clicky", "chat": "C1"}, "clicks": {"approve": {"role": "operator"}}},
        warn,
    )

    assert scopes
    assert not [line for line in warnings if "is not part of a rule" in line]


def test_a_role_settles_to_the_ids_it_holds_on_this_platform(warn):
    scopes = _compile(
        {"match": {"channel": "clicky"}, "clicks": {"approve": {"role": "member"}}},
        warn,
    )

    rule = click_rule(scopes, channel="clicky")
    assert rule == ClickRule(users=("U_ALICE", "U_BOB"), roles=("member",))
    assert rule.permits("U_ALICE")
    assert not rule.permits("U_CHARLIE")


def test_user_and_role_are_one_axis_and_they_or(warn):
    # The same union the match axis performs. AND would mean "alice, but
    # only if also an operator", which nobody would write on purpose.
    scopes = _compile(
        {
            "match": {"channel": "clicky"},
            "clicks": {"approve": {"user": ["U_BOB"], "role": "operator"}},
        },
        warn,
    )

    rule = click_rule(scopes, channel="clicky")
    assert rule.users == ("U_ALICE", "U_BOB")


def test_both_gestures_are_settled_independently(warn):
    scopes = _compile(
        {
            "match": {"channel": "clicky"},
            "clicks": {"approve": {"role": "operator"}, "stop": {"role": "member"}},
        },
        warn,
    )

    assert click_rule(scopes, channel="clicky", kind=CLICK_APPROVE).users == ("U_ALICE",)
    assert click_rule(scopes, channel="clicky", kind=CLICK_STOP).users == (
        "U_ALICE",
        "U_BOB",
    )


def test_a_gesture_nobody_renders_is_reported(warn, warnings):
    scopes = _compile(
        {"match": {"channel": "clicky"}, "clicks": {"undo": {"role": "operator"}}},
        warn,
    )

    assert scopes == ()
    assert any("is not a click this version can gate" in line for line in warnings)


# --------------------------------------------------------------------------
# Composition: the map merges per gesture, a clause replaces
# --------------------------------------------------------------------------


def test_a_narrower_rule_naming_one_gesture_leaves_the_other_alone(warn):
    # The ``clicks`` map merges per key, so a conversation that narrows stop
    # keeps the platform layer's approve rather than being read as having
    # declined it.
    scopes = compile_scopes(
        [
            {
                "match": {"channel": "clicky"},
                "clicks": {"approve": {"role": "operator"}, "stop": {"role": "member"}},
            },
            {
                "match": {"channel": "clicky", "chat": "C1"},
                "clicks": {"stop": {"role": "operator"}},
            },
        ],
        people=PEOPLE,
        roles=ROLES,
        warn=warn,
    )

    assert click_rule(scopes, channel="clicky", chat="C1").users == ("U_ALICE",)
    assert click_rule(scopes, channel="clicky", chat="C1", kind=CLICK_STOP).users == (
        "U_ALICE",
    )
    assert click_rule(scopes, channel="clicky", chat="C2", kind=CLICK_STOP).users == (
        "U_ALICE",
        "U_BOB",
    )


def test_a_later_clause_replaces_rather_than_widening_the_one_above(warn):
    # A clause is one settled value, so it composes as a scalar does: replace.
    # Merging its halves would make a per-conversation rule able only to add
    # people, which is the wrong direction for a section that exists to
    # tighten.
    scopes = compile_scopes(
        [
            {"match": {"channel": "clicky"}, "clicks": {"approve": {"role": "member"}}},
            {
                "match": {"channel": "clicky", "chat": "C1"},
                "clicks": {"approve": {"user": ["U_BOB"]}},
            },
        ],
        people=PEOPLE,
        roles=ROLES,
        warn=warn,
    )

    assert click_rule(scopes, channel="clicky", chat="C1").users == ("U_BOB",)
    assert click_rule(scopes, channel="clicky", chat="C9").users == (
        "U_ALICE",
        "U_BOB",
    )


def test_a_clicks_rule_never_opts_a_conversation_into_being_answered(warn):
    # The same security property ``permissions`` has: on Slack these ids are
    # what exempts a channel from allowed_channel_ids, so a rule written to
    # take something away must not hand something else out.
    scopes = _compile(
        {
            "match": {"channel": "clicky", "chat": "C_LOCKED"},
            "clicks": {"approve": {"role": "operator"}},
        },
        warn,
    )

    assert scopes
    assert scoped_chats(scopes, channel="clicky") == ()


# --------------------------------------------------------------------------
# No rule, no change
# --------------------------------------------------------------------------


def test_no_clicks_rule_settles_to_none_rather_than_to_nobody(warn):
    """``None`` means no rule; an empty rule means nobody. Not the same thing."""
    scopes = _compile(
        {"match": {"channel": "clicky"}, "delivery": {"mode": ["mention"]}}, warn
    )

    assert click_rule(scopes, channel="clicky") is None


def test_a_rule_on_one_conversation_leaves_the_others_on_layer_zero(warn):
    scopes = _compile(
        {
            "match": {"channel": "clicky", "chat": "C1"},
            "clicks": {"approve": {"role": "operator"}},
        },
        warn,
    )

    assert click_rule(scopes, channel="clicky", chat="C1") is not None
    assert click_rule(scopes, channel="clicky", chat="C2") is None


# --------------------------------------------------------------------------
# A rule matches and the clicker cannot be identified
# --------------------------------------------------------------------------


def test_a_click_carrying_no_id_is_refused(warn):
    scopes = _compile(
        {"match": {"channel": "clicky"}, "clicks": {"approve": {"role": "member"}}},
        warn,
    )

    rule = click_rule(scopes, channel="clicky")
    assert not rule.permits("")
    assert not rule.permits(None)
    assert not rule.permits("   ")


def test_a_role_nobody_here_is_in_admits_nobody_here(warn, warnings):
    """The rule for an unnamed person, read at click time.

    The role is declared and its member is a real person -- they simply have no
    id on this platform -- so the rule is honourable and its answer is that
    nobody satisfies it. Kept rather than dropped: reading "a restriction naming
    nobody restricts nobody" is the permissive direction this settles against.
    """
    scopes = _compile(
        {"match": {"channel": "clicky"}, "clicks": {"approve": {"role": "offsite"}}},
        warn,
    )

    rule = click_rule(scopes, channel="clicky")
    assert rule == ClickRule(users=(), roles=("offsite",))
    assert not rule.permits("U_ALICE")
    assert any(
        "admits nobody on clicky" in line and "is refused" in line for line in warnings
    )


def test_an_undeclared_role_drops_the_clause_and_says_the_click_is_ungated(
    warn, warnings
):
    """A typo is a different failure, and the warning must not blur the two.

    A name in no ``roles:`` block is a config this file cannot honour at all, so
    the clause goes and the click falls back to layer 0. That is a widening, and
    it is said out loud -- refusing every click over a misspelling would take a
    deployment's approvals away for a mistake the same warning tells it how to
    fix.
    """
    scopes = _compile(
        {"match": {"channel": "clicky"}, "clicks": {"approve": {"role": "opeartor"}}},
        warn,
    )

    assert scopes == ()
    assert any(
        "does not declare" in line and "nothing gates that click here" in line
        for line in warnings
    )


def test_an_empty_clause_names_nobody_and_is_not_read_as_everyone(warn, warnings):
    scopes = _compile({"match": {"channel": "clicky"}, "clicks": {"approve": {}}}, warn)

    assert scopes == ()
    assert any("names nobody" in line for line in warnings)


def test_a_clause_that_is_not_a_mapping_is_dropped(warn, warnings):
    scopes = _compile(
        {"match": {"channel": "clicky"}, "clicks": {"approve": ["U_ALICE"]}}, warn
    )

    assert scopes == ()
    assert any("is not a mapping of who may click" in line for line in warnings)


def test_not_is_not_a_way_of_naming_who_may_click(warn, warnings):
    # "anyone but these people may approve" is the permissive default clicks
    # exists to close, written the long way round.
    scopes = _compile(
        {
            "match": {"channel": "clicky"},
            "clicks": {"approve": {"not": {"user": ["U_BOB"]}}},
        },
        warn,
    )

    assert scopes == ()
    assert any("is not a way of naming who may click" in line for line in warnings)


# --------------------------------------------------------------------------
# No surface is not no identity
# --------------------------------------------------------------------------


def test_a_channel_that_renders_no_buttons_reports_no_effect(warn, warnings):
    scopes = _compile(
        {"match": {"channel": "silent"}, "clicks": {"approve": {"role": "operator"}}},
        warn,
    )

    assert scopes == ()
    assert any("clicks has no effect" in line for line in warnings)
    # Not the other warning: there is no click to refuse here.
    assert not any("is refused rather than gated" in line for line in warnings)


def test_a_channel_that_renders_buttons_and_names_nobody_refuses_every_click(
    warn, warnings
):
    scopes = _compile(
        {"match": {"channel": "anonymous"}, "clicks": {"approve": {"role": "operator"}}},
        warn,
    )

    # Kept, and refusing: this is the rule that would have applied had the fact
    # it needed arrived, which is not the same as a rule written against the
    # wrong channel.
    rule = click_rule(scopes, channel="anonymous")
    assert rule is not None
    assert not rule.permits("U_ALICE")
    assert any("is refused rather than gated" in line for line in warnings)
    assert not any("clicks has no effect" in line for line in warnings)


def test_the_pseudo_channels_render_none_and_say_so():
    for pseudo in ("__cron__", "__heartbeat__"):
        declared = caps.channel_capabilities(pseudo)
        assert not declared.reads(SECTION_CLICKS)


# --------------------------------------------------------------------------
# The section is about the conversation, not about the sender
# --------------------------------------------------------------------------


def test_a_rule_that_also_names_a_sender_cannot_gate_a_click(warn, warnings):
    """Two principals, and a click has no turn behind it to read the first from.

    Left standing, such a rule would be settled for nobody -- inert, silently,
    in the one section written to restrict.
    """
    scopes = _compile(
        {
            "match": {"channel": "clicky", "chat": "C1", "user": ["U_ALICE"]},
            "clicks": {"approve": {"role": "operator"}},
        },
        warn,
    )

    assert scopes == ()
    assert any(
        "has no effect on a rule that also names a sender" in line
        for line in warnings
    )


def test_the_rule_does_not_move_with_the_clicker(warn):
    """Composed per conversation, so the same answer whoever is asking."""
    scopes = _compile(
        {"match": {"channel": "clicky"}, "clicks": {"approve": {"role": "operator"}}},
        warn,
    )

    assert click_rule(scopes, channel="clicky").users == ("U_ALICE",)
