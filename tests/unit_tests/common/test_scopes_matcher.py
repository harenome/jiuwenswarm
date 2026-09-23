# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.

"""The matcher and the layer ordering.

Warnings are collected through the injected ``warn`` rather than through
``caplog``: this project's loggers do not propagate, so ``caplog`` sees nothing
under the pytest CI runs on, and a test written against it would pass locally
and assert nothing where it matters.
"""

from __future__ import annotations

from typing import Any

import pytest

from jiuwenswarm.common.scopes import (
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
    """A channel that reads everything, so a test can isolate one rule."""
    snapshot = caps.snapshot_registry()
    register_channel(
        ChannelCapabilities(
            channel="demo",
            axes=frozenset({"channel", "chat", "user"}),
            sections={"delivery": None, "agent": None},
            # Named because the axis is: identity_keys says which of a
            # platform's ids for one person is the one matched on, and a channel
            # declaring the user axis with none of them is a declaration that
            # disagrees with itself.
            identity_keys=("user",),
        )
    )
    yield
    caps.restore_registry(snapshot)


def _compile(entries: Any, warn, **kwargs: Any) -> tuple[Scope, ...]:
    return compile_scopes(entries, warn=warn, **kwargs)


# --------------------------------------------------------------------------
# Shape of a match
# --------------------------------------------------------------------------


def test_an_absent_axis_matches_anything(demo_channel, warn, warnings):
    scopes = _compile([{"match": {"channel": "demo"}, "delivery": {"mode": ["all"]}}], warn)

    assert scopes[0].selects(channel="demo", chat="C1")
    assert scopes[0].selects(channel="demo", chat=None)
    assert not scopes[0].selects(channel="web", chat="C1")
    assert warnings == []


def test_a_match_with_no_criteria_applies_everywhere(warn, warnings):
    scopes = _compile([{"match": {}, "delivery": {"mode": ["all"]}}], warn)

    assert scopes[0].selects(channel="demo", chat="C1")
    assert scopes[0].selects(channel="web", chat=None)
    assert scopes[0].layer == (0, 0, 0, 0)
    assert warnings == []


def test_axes_and_rather_than_or(demo_channel, warn):
    scopes = _compile(
        [{"match": {"channel": "demo", "chat": "C1"}, "delivery": {"mode": ["all"]}}],
        warn,
    )

    assert scopes[0].selects(channel="demo", chat="C1")
    assert not scopes[0].selects(channel="demo", chat="C2")
    assert not scopes[0].selects(channel="web", chat="C1")


def test_ids_are_matched_exactly(demo_channel, warn):
    scopes = _compile(
        [{"match": {"channel": "demo", "chat": "C1"}, "delivery": {"mode": ["all"]}}],
        warn,
    )

    assert not scopes[0].selects(channel="demo", chat="c1")
    assert not scopes[0].selects(channel="demo", chat="C1 ")


def test_surrounding_whitespace_in_an_id_is_settled_at_load(demo_channel, warn):
    scopes = _compile(
        [{"match": {"channel": " demo ", "chat": " C1 "}, "delivery": {"mode": ["all"]}}],
        warn,
    )

    assert scopes[0].match == ScopeMatch(channel="demo", chat="C1")


# --------------------------------------------------------------------------
# Layering
# --------------------------------------------------------------------------


def test_specificity_decides_the_layer(demo_channel, warn):
    scopes = _compile(
        [
            {"match": {"channel": "demo"}, "delivery": {"mode": ["mention"]}},
            {"match": {"channel": "demo", "chat": "C1"}, "delivery": {"mode": ["all"]}},
        ],
        warn,
    )

    assert [scope.layer for scope in scopes] == [(1, 0, 0, 0), (1, 0, 2, 0)]


def test_a_more_specific_scope_wins_even_when_written_first(demo_channel, warn):
    scopes = _compile(
        [
            {"match": {"channel": "demo", "chat": "C1"}, "delivery": {"mode": ["all"]}},
            {"match": {"channel": "demo"}, "delivery": {"mode": ["mention"]}},
        ],
        warn,
    )

    ordered = matching_scopes(scopes, channel="demo", chat="C1")
    assert [scope.layer for scope in ordered] == [(1, 0, 0, 0), (1, 0, 2, 0)]


def test_within_one_layer_the_later_entry_wins(demo_channel, warn):
    scopes = _compile(
        [
            {"match": {"channel": "demo"}, "delivery": {"mode": ["mention"]}},
            {"match": {"channel": "demo"}, "delivery": {"mode": ["all"]}},
        ],
        warn,
    )

    ordered = matching_scopes(scopes, channel="demo", chat="C1")
    assert [scope.index for scope in ordered] == [0, 1]


def test_a_scope_that_does_not_select_is_not_returned(demo_channel, warn):
    scopes = _compile(
        [
            {"match": {"channel": "demo", "chat": "C1"}, "delivery": {"mode": ["all"]}},
            {"match": {"channel": "demo", "chat": "C2"}, "delivery": {"mode": ["reply"]}},
        ],
        warn,
    )

    ordered = matching_scopes(scopes, channel="demo", chat="C1")
    assert [scope.match.chat for scope in ordered] == ["C1"]


def test_a_scope_with_nothing_in_the_section_asked_for_is_skipped(demo_channel, warn):
    scopes = _compile(
        [{"match": {"channel": "demo"}, "delivery": {"mode": ["all"]}}], warn
    )

    # Both sections are read now, so this asks a real question rather than a
    # hypothetical one: a scope that speaks only about delivery must not turn up
    # in the fold the runtime's half performs.
    assert matching_scopes(scopes, channel="demo", section="agent") == ()


def test_the_two_sections_of_one_rule_are_kept_apart(demo_channel, warn, warnings):
    scopes = _compile(
        [
            {
                "match": {"channel": "demo"},
                "delivery": {"mode": ["all"]},
                "agent": {"model_name": "m"},
            }
        ],
        warn,
    )

    assert warnings == []
    assert scopes[0].section("delivery") == {"mode": ["all"]}
    assert scopes[0].section("agent") == {"model_name": "m"}


# --------------------------------------------------------------------------
# Shapes that are not rules
# --------------------------------------------------------------------------


def test_scopes_absent_is_no_scopes(warn, warnings):
    assert compile_scopes(None, warn=warn) == ()
    assert warnings == []


def test_scopes_that_is_not_a_list_is_ignored_with_a_warning(warn, warnings):
    assert compile_scopes({"match": {}}, warn=warn) == ()
    assert any("is not a list of rules" in line for line in warnings)


def test_an_entry_that_is_not_a_mapping_is_dropped(demo_channel, warn, warnings):
    scopes = _compile(
        ["nonsense", {"match": {"channel": "demo"}, "delivery": {"mode": ["all"]}}],
        warn,
    )

    assert len(scopes) == 1
    assert any("scopes[0]" in line and "is not a rule" in line for line in warnings)


def test_an_unknown_top_level_key_in_a_rule_is_reported_but_keeps_the_rule(
    demo_channel, warn, warnings
):
    scopes = _compile(
        [{"match": {"channel": "demo"}, "delivry": {}, "delivery": {"mode": ["all"]}}],
        warn,
    )

    assert len(scopes) == 1
    assert any("scopes[0].delivry" in line for line in warnings)


def test_a_match_that_is_not_a_mapping_drops_the_scope(warn, warnings):
    assert _compile([{"match": "slack", "delivery": {"mode": ["all"]}}], warn) == ()
    assert any("is not a mapping of criteria" in line for line in warnings)


def test_an_unquoted_numeric_id_drops_the_scope_rather_than_matching_nothing(
    demo_channel, warn, warnings
):
    assert _compile([{"match": {"chat": 12345}, "delivery": {"mode": ["all"]}}], warn) == ()
    assert any("quote ids" in line for line in warnings)


def test_an_empty_axis_value_drops_the_scope(demo_channel, warn, warnings):
    assert _compile([{"match": {"chat": "  "}, "delivery": {"mode": ["all"]}}], warn) == ()
    assert any("is empty" in line for line in warnings)


def test_a_rule_whose_sections_all_dropped_out_is_not_kept(demo_channel, warn):
    assert _compile([{"match": {"channel": "demo"}}], warn) == ()


# --------------------------------------------------------------------------
# Axes this version does not read
# --------------------------------------------------------------------------
#
# There are none left. ``role`` was the last, and it and the ``people:`` /
# ``roles:`` blocks it resolves against are pinned in
# ``test_scopes_people.py`` -- including the equality on ``DEFERRED_AXES``
# that used to live here, so that an axis added ahead of its reader still has
# to be thought about in one place.


# --------------------------------------------------------------------------
# The identity axis
# --------------------------------------------------------------------------


def test_a_user_axis_selects_only_the_senders_it_names(demo_channel, warn, warnings):
    scopes = _compile(
        [
            {
                "match": {"channel": "demo", "user": ["U1", "U2"]},
                "delivery": {"mode": ["all"]},
            }
        ],
        warn,
    )

    assert warnings == []
    assert scopes[0].selects(channel="demo", chat="C1", user="U1")
    assert scopes[0].selects(channel="demo", chat="C9", user="U2")
    assert not scopes[0].selects(channel="demo", chat="C1", user="U3")


def test_user_and_role_are_one_axis_so_a_not_does_not_add_a_layer(
    demo_channel, warn, warnings
):
    scopes = _compile(
        [
            {
                "match": {"channel": "demo", "chat": "C1", "user": ["U1"]},
                "delivery": {"prompt": "positive"},
            },
            {
                "match": {
                    "channel": "demo",
                    "chat": "C1",
                    "user": ["U1"],
                    "not": {"user": ["U2"]},
                },
                "delivery": {"prompt": "positive and negative"},
            },
        ],
        warn,
    )

    # user, role and not are one axis between them, and they settle one
    # component of the grain tuple. Counting the keys would have put the second
    # scope a whole layer up and let adding an exemption to a rule silently
    # promote it above a rule that had been winning.
    assert [scope.layer for scope in scopes] == [(1, 0, 2, 2), (1, 0, 2, 2)]


def test_a_not_on_its_own_still_constrains_the_identity_axis(
    demo_channel, warn, warnings
):
    scopes = _compile(
        [
            {
                "match": {"channel": "demo", "chat": "C1"},
                "delivery": {"prompt": "everyone"},
            },
            {
                "match": {
                    "channel": "demo",
                    "chat": "C1",
                    "not": {"user": ["U_admin"]},
                },
                "delivery": {"prompt": "everyone but the admin"},
            },
        ],
        warn,
    )

    # "Everyone except one person" is narrower than "everyone", so it is a
    # further layer -- which is what makes a read-only-except-admins rule work
    # without an override: directive. It is the coarse identity grain, not the
    # fine one: the exemption's size is a fact about the list it names, and a
    # bare "not:" cannot be ordered against a role.
    assert [scope.layer for scope in scopes] == [(1, 0, 2, 0), (1, 0, 2, 1)]
    assert scopes[1].selects(channel="demo", chat="C1", user="U_other")
    assert not scopes[1].selects(channel="demo", chat="C1", user="U_admin")


def test_a_positive_and_a_negative_together_are_the_team_minus_one(
    demo_channel, warn, warnings
):
    scopes = _compile(
        [
            {
                "match": {
                    "channel": "demo",
                    "user": ["U1", "U2", "U3"],
                    "not": {"user": ["U2"]},
                },
                "delivery": {"mode": ["all"]},
            }
        ],
        warn,
    )

    assert warnings == []
    assert scopes[0].selects(channel="demo", chat="C1", user="U1")
    assert not scopes[0].selects(channel="demo", chat="C1", user="U2")
    assert not scopes[0].selects(channel="demo", chat="C1", user="U9")


def test_an_unidentified_sender_matches_no_positive_and_escapes_no_negative(
    demo_channel, warn, warnings
):
    scopes = _compile(
        [
            {
                "match": {"channel": "demo", "user": ["U1"]},
                "delivery": {"prompt": "named"},
            },
            {
                "match": {"channel": "demo", "not": {"user": ["U1"]}},
                "delivery": {"prompt": "restricted"},
            },
        ],
        warn,
    )

    # Both halves fail closed, in opposite directions and for one reason: ids
    # are non-empty, so an unidentified sender is in no list. A rule written for
    # named people does not fire; a restriction written to exempt named people
    # still applies. Section 8.3's rule for a person with no id on the platform.
    for absent in (None, "", "   "):
        assert not scopes[0].selects(channel="demo", chat="C1", user=absent)
        assert scopes[1].selects(channel="demo", chat="C1", user=absent)


def test_ids_in_an_identity_list_are_settled_at_load(demo_channel, warn, warnings):
    scopes = _compile(
        [
            {
                "match": {"channel": "demo", "user": [" U2 ", "U1", "U1"]},
                "delivery": {"mode": ["all"]},
            }
        ],
        warn,
    )

    # Stripped, deduplicated and sorted. A list is a set: its order means no
    # meaning, so settling one keeps two spellings of the same rule equal.
    assert scopes[0].match.users == ("U1", "U2")
    assert warnings == []


def test_a_chat_bearing_scope_outranks_a_user_bearing_one_either_way_round(
    demo_channel, warn, warnings
):
    """The case the grain tuple has to be pinned on.

    ``{channel, user}`` -- this person anywhere -- and ``{channel, chat}`` --
    everyone in this conversation -- are genuinely incomparable: neither match
    set contains the other. Counting axes made them both layer 2 and left the
    answer to file position. The tuple reads the conversation before the sender,
    so the chat rule wins, and this is a decision rather than a derivation --
    which is why it is asserted both ways round. Position no longer decides it.
    """
    user_first = _compile(
        [
            {"match": {"channel": "demo", "user": ["U1"]}, "delivery": {"prompt": "u"}},
            {"match": {"channel": "demo", "chat": "C1"}, "delivery": {"prompt": "c"}},
        ],
        warn,
    )
    chat_first = _compile(
        [
            {"match": {"channel": "demo", "chat": "C1"}, "delivery": {"prompt": "c"}},
            {"match": {"channel": "demo", "user": ["U1"]}, "delivery": {"prompt": "u"}},
        ],
        warn,
    )

    assert [scope.layer for scope in user_first] == [(1, 0, 0, 2), (1, 0, 2, 0)]
    assert [scope.layer for scope in chat_first] == [(1, 0, 2, 0), (1, 0, 0, 2)]

    def _last(scopes):
        return matching_scopes(scopes, channel="demo", chat="C1", user="U1")[-1]

    assert _last(user_first).section("delivery") == {"prompt": "c"}
    assert _last(chat_first).section("delivery") == {"prompt": "c"}


def test_naming_both_a_conversation_and_a_sender_outranks_naming_either(
    demo_channel, warn, warnings
):
    scopes = _compile(
        [
            {
                "match": {"channel": "demo", "chat": "C1", "user": ["U1"]},
                "delivery": {"prompt": "both"},
            },
            {"match": {"channel": "demo", "chat": "C1"}, "delivery": {"prompt": "chat"}},
            {"match": {"channel": "demo", "user": ["U1"]}, "delivery": {"prompt": "user"}},
        ],
        warn,
    )

    # Written first and still last in the fold: naming both beats either alone
    # whichever order they appear in. The two that name one each are now ordered
    # between themselves as well -- the conversation is read before the sender --
    # so the user-only rule folds first and the chat-only rule over it.
    ordered = matching_scopes(scopes, channel="demo", chat="C1", user="U1")
    assert [scope.layer for scope in ordered] == [(1, 0, 0, 2), (1, 0, 2, 0), (1, 0, 2, 2)]
    assert ordered[-1].section("delivery") == {"prompt": "both"}


def test_a_caller_with_no_sender_gets_the_unidentified_answer(
    demo_channel, warn, warnings
):
    scopes = _compile(
        [
            {"match": {"channel": "demo", "chat": "C1"}, "delivery": {"prompt": "chat"}},
            {
                "match": {"channel": "demo", "chat": "C1", "user": ["U1"]},
                "delivery": {"prompt": "user"},
            },
        ],
        warn,
    )

    # The default is not "match everything": a caller that has no sender to
    # offer -- a startup summary, a pseudo-channel -- must not be handed the
    # rule written for a person.
    assert matching_scopes(scopes, channel="demo", chat="C1") == (scopes[0],)
    assert len(matching_scopes(scopes, channel="demo", chat="C1", user="U1")) == 2


# --------------------------------------------------------------------------
# Identity shapes that are refused
# --------------------------------------------------------------------------


def test_a_bare_id_where_a_list_is_wanted_drops_the_scope(demo_channel, warn, warnings):
    assert (
        _compile(
            [{"match": {"channel": "demo", "user": "U1"}, "delivery": {"mode": ["all"]}}],
            warn,
        )
        == ()
    )
    assert any("is a single id where a list is wanted" in line for line in warnings)


def test_an_empty_identity_list_drops_the_scope_rather_than_meaning_everyone(
    demo_channel, warn, warnings
):
    assert (
        _compile(
            [{"match": {"channel": "demo", "user": []}, "delivery": {"mode": ["all"]}}],
            warn,
        )
        == ()
    )
    assert any("names nobody" in line for line in warnings)


def test_an_unquoted_numeric_id_in_an_identity_list_drops_the_scope(
    demo_channel, warn, warnings
):
    assert (
        _compile(
            [{"match": {"channel": "demo", "user": [12345]}, "delivery": {"mode": ["all"]}}],
            warn,
        )
        == ()
    )
    assert any("quote ids" in line for line in warnings)


def test_a_not_that_is_not_a_mapping_drops_the_scope(demo_channel, warn, warnings):
    assert (
        _compile(
            [{"match": {"channel": "demo", "not": ["U1"]}, "delivery": {"mode": ["all"]}}],
            warn,
        )
        == ()
    )
    assert any("is not a mapping of criteria to exclude" in line for line in warnings)


def test_an_empty_not_drops_the_scope(demo_channel, warn, warnings):
    assert (
        _compile(
            [{"match": {"channel": "demo", "not": {}}, "delivery": {"mode": ["all"]}}],
            warn,
        )
        == ()
    )
    assert any("excludes nobody" in line for line in warnings)


@pytest.mark.parametrize("axis", ["chat", "channel"])
def test_a_not_on_a_non_identity_axis_drops_the_scope(demo_channel, warn, warnings, axis):
    # Section 4.4. Refused rather than ignored: "everywhere except C2" with its
    # not ignored would apply the rule inside C2 as well -- reaching into the
    # conversation it was written to leave alone.
    assert (
        _compile(
            [
                {
                    "match": {"channel": "demo", "not": {axis: ["C2"]}},
                    "delivery": {"mode": ["all"]},
                }
            ],
            warn,
        )
        == ()
    )
    assert any("applies to the identity axis only" in line for line in warnings)


def test_a_not_on_a_role_no_config_declares_drops_the_scope(demo_channel, warn, warnings):
    assert (
        _compile(
            [
                {
                    "match": {"channel": "demo", "not": {"role": "admin"}},
                    "delivery": {"mode": ["all"]},
                }
            ],
            warn,
        )
        == ()
    )
    # Section 4.3's motivating example, compiled with no ``roles:`` block behind
    # it. Dropping is the fail-closed direction: a role nothing declares, read
    # as "nobody", would exclude nobody and land the restriction on exactly the
    # people it was written to exempt. The resolved case is in
    # ``test_scopes_people.py``.
    assert any("does not declare" in line for line in warnings)


def test_a_sender_named_without_a_platform_drops_the_scope(demo_channel, warn, warnings):
    assert _compile([{"match": {"user": ["U1"]}, "delivery": {"mode": ["all"]}}], warn) == ()
    assert any("without saying which platform" in line for line in warnings)


def test_an_axis_that_is_not_an_axis_at_all_drops_the_scope(demo_channel, warn, warnings):
    assert _compile([{"match": {"team": "T1"}, "delivery": {"mode": ["all"]}}], warn) == ()
    assert any("is not a match axis" in line for line in warnings)


def test_a_section_the_channel_does_not_read_is_reported_as_ineffective(
    demo_channel, warn, warnings
):
    # demo declares delivery and agent only, so a permissions block on it is a
    # section that channel ignores. The delivery half still applies: an operator
    # who wrote both must be told which half is live, and told that the other
    # restricts nothing.
    scopes = _compile(
        [
            {
                "match": {"channel": "demo"},
                "permissions": {"tools": {"bash": "deny"}},
                "delivery": {"mode": ["all"]},
            }
        ],
        warn,
    )

    assert len(scopes) == 1
    assert scopes[0].section("permissions") == {}
    assert any("does not read the permissions section" in line for line in warnings)


def test_no_section_is_deferred_any_more():
    # agent left the list when model_name moved into it; permissions left it
    # when the permission rail started resolving it; clicks joined it already
    # read, by the connector's click handler. Pinned as an equality rather than
    # as "x not in it", so that a section added later has to be thought about
    # here rather than joining silently.
    from jiuwenswarm.common.scopes import DEFERRED_SECTIONS, SUPPORTED_SECTIONS

    assert DEFERRED_SECTIONS == ()
    assert SUPPORTED_SECTIONS == ("delivery", "agent", "permissions", "clicks")


def test_the_agent_section_is_stored_rather_than_warned_about(
    demo_channel, warn, warnings
):
    scopes = _compile(
        [{"match": {"channel": "demo"}, "agent": {"model_name": "m"}}], warn
    )

    # It used to warn as deferred and be dropped, which made it a heading over
    # nothing. A rule with agent alone is now a whole rule.
    assert len(scopes) == 1
    assert scopes[0].section("agent") == {"model_name": "m"}
    assert warnings == []
