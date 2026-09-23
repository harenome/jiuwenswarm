# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.

"""The ``chat_type`` axis: its vocabulary, and the pair it may not be written in.

No connector is involved anywhere here. The axis, the vocabulary mechanism and
the refusal are the shared loader's, and they are exercised against a fake
platform registered through ``register_channel`` -- which is what says the
generic half stands on its own and is not Slack's behaviour read back.

Warnings are collected through the injected ``warn`` rather than through
``caplog``: this project's loggers do not propagate, so ``caplog`` sees nothing
under the pytest CI runs on, and a test written against it would pass locally
and assert nothing where it matters.
"""

from __future__ import annotations

from typing import Any

import pytest

from jiuwenswarm.common.scopes import (
    AXIS_CHAT_TYPE,
    ChannelCapabilities,
    Scope,
    ScopeMatch,
    compile_scopes,
    compose_section,
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
    """A platform with two kinds of conversation and nothing else to it."""
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


@pytest.fixture
def typeless_channel():
    """A platform that has conversations but no kinds of them."""
    snapshot = caps.snapshot_registry()
    register_channel(
        ChannelCapabilities(
            channel="flat",
            axes=frozenset({"channel", "chat"}),
            sections={"delivery": None},
        )
    )
    yield
    caps.restore_registry(snapshot)


def _compile(entries: Any, warn, **kwargs: Any) -> tuple[Scope, ...]:
    return compile_scopes(entries, warn=warn, **kwargs)


# --------------------------------------------------------------------------
# Matching
# --------------------------------------------------------------------------


def test_a_kind_selects_every_conversation_of_that_kind(demo_channel, warn, warnings):
    scopes = _compile(
        [
            {
                "match": {"channel": "demo", "chat_type": "direct"},
                "delivery": {"mode": ["all"]},
            }
        ],
        warn,
    )

    assert scopes[0].match.chat_type == ("direct",)
    assert scopes[0].selects(channel="demo", chat="D1", chat_type="direct")
    assert scopes[0].selects(channel="demo", chat="D2", chat_type="direct")
    assert not scopes[0].selects(channel="demo", chat="C1", chat_type="room")
    assert warnings == []


def test_a_caller_with_no_kind_gets_the_unnamed_answer(demo_channel, warn):
    scopes = _compile(
        [
            {
                "match": {"channel": "demo", "chat_type": "direct"},
                "delivery": {"mode": ["all"]},
            }
        ],
        warn,
    )

    # The same fail-closed direction an unidentified sender takes. Reading an
    # absent kind as every kind would apply a rule written for one surface to
    # every other, which is the widening nothing here may perform.
    assert not scopes[0].selects(channel="demo", chat="C1")
    assert compose_section(scopes, channel="demo", chat="C1") == {}


def test_the_kind_is_matched_exactly(demo_channel, warn):
    scopes = _compile(
        [
            {
                "match": {"channel": "demo", "chat_type": "direct"},
                "delivery": {"mode": ["all"]},
            }
        ],
        warn,
    )

    assert not scopes[0].selects(channel="demo", chat="D1", chat_type="Direct")
    assert not scopes[0].selects(channel="demo", chat="D1", chat_type="direct ")


def test_a_kind_rule_sits_under_a_rule_naming_the_conversation(
    demo_channel, warn, warnings
):
    scopes = _compile(
        [
            {
                "match": {"channel": "demo", "chat": "D1"},
                "delivery": {"prompt": "this one"},
            },
            {
                "match": {"channel": "demo", "chat_type": "direct"},
                "delivery": {"prompt": "every direct message"},
            },
        ],
        warn,
    )

    # Written after and still folded first: the kind is coarser than the room
    # whichever order the two appear in.
    ordered = matching_scopes(scopes, channel="demo", chat="D1", chat_type="direct")
    assert [scope.section("delivery")["prompt"] for scope in ordered] == [
        "every direct message",
        "this one",
    ]
    assert warnings == []


def test_a_kind_rule_outranks_a_rule_naming_the_platform_alone(
    demo_channel, warn, warnings
):
    scopes = _compile(
        [
            {
                "match": {"channel": "demo", "chat_type": "direct"},
                "delivery": {"prompt": "direct"},
            },
            {"match": {"channel": "demo"}, "delivery": {"prompt": "anywhere"}},
        ],
        warn,
    )

    settled = compose_section(
        scopes, channel="demo", chat="D1", chat_type="direct"
    )
    assert settled == {"prompt": "direct"}
    assert warnings == []


def test_a_role_in_the_same_rule_does_not_drop_the_kind(demo_channel, warn, warnings):
    """Role resolution rebuilds the match, and it has to rebuild all of it.

    Asserted on the two things a dropped axis costs, because they are the two
    ways it shows: the rule reaches conversations it was not written for, and it
    sits on the wrong layer while doing so.
    """
    scopes = _compile(
        [
            {
                "match": {
                    "channel": "demo",
                    "chat_type": "direct",
                    "role": "admin",
                },
                "delivery": {"prompt": "admins, in direct messages"},
            }
        ],
        warn,
        people={"boss": {"demo": "U_BOSS"}},
        roles={"admin": ["boss"]},
    )

    assert warnings == []
    assert scopes[0].match.chat_type == ("direct",)
    assert scopes[0].selects(channel="demo", chat="D1", chat_type="direct", user="U_BOSS")
    # The half the drop used to give away: an admin in a room is not an admin in
    # a direct message, and the rule says direct message.
    assert not scopes[0].selects(
        channel="demo", chat="C1", chat_type="room", user="U_BOSS"
    )
    assert scopes[0].layer == (1, 0, 1, 1)


def test_the_axis_is_described(demo_channel, warn):
    scopes = _compile(
        [
            {
                "match": {"channel": "demo", "chat_type": "room"},
                "delivery": {"mode": ["all"]},
            }
        ],
        warn,
    )

    assert scopes[0].match.describe() == "{channel: demo, chat_type: room}"


# --------------------------------------------------------------------------
# Several kinds in one rule
# --------------------------------------------------------------------------


def test_a_list_of_kinds_selects_a_conversation_of_any_of_them(
    demo_channel, warn, warnings
):
    """The whole point of the list: one rule where two said the same thing.

    On Slack this is `[channel, group]` -- a private channel arrives as `group`
    rather than as `channel`, so "every channel-like conversation" is two kinds
    and used to need two rules with identical bodies.
    """
    scopes = _compile(
        [
            {
                "match": {"channel": "demo", "chat_type": ["room", "direct"]},
                "delivery": {"prompt": "both kinds"},
            }
        ],
        warn,
    )

    assert warnings == []
    assert scopes[0].selects(channel="demo", chat="C1", chat_type="room")
    assert scopes[0].selects(channel="demo", chat="D1", chat_type="direct")


def test_a_list_does_not_reach_a_kind_outside_it(warn, warnings):
    snapshot = caps.snapshot_registry()
    register_channel(
        ChannelCapabilities(
            channel="three",
            axes=frozenset({"channel", "chat", "chat_type"}),
            sections={"delivery": None},
            axis_values={AXIS_CHAT_TYPE: frozenset({"room", "direct", "huddle"})},
        )
    )
    try:
        scopes = _compile(
            [
                {
                    "match": {"channel": "three", "chat_type": ["room", "direct"]},
                    "delivery": {"prompt": "two of the three"},
                }
            ],
            warn,
        )
    finally:
        caps.restore_registry(snapshot)

    # A list ORs the kinds it names and adds none. The third kind is outside it
    # and stays on the layer below, which is what makes the list a selection
    # rather than a way of saying "any kind".
    assert warnings == []
    assert not scopes[0].selects(channel="three", chat="H1", chat_type="huddle")
    assert scopes[0].selects(channel="three", chat="C1", chat_type="room")


def test_one_kind_in_a_list_is_the_scalar_rule_exactly(demo_channel, warn, warnings):
    scalar, listed = _compile(
        [
            {
                "match": {"channel": "demo", "chat_type": "direct"},
                "delivery": {"prompt": "one"},
            },
            {
                "match": {"channel": "demo", "chat_type": ["direct"]},
                "delivery": {"prompt": "two"},
            },
        ],
        warn,
    )

    # Not merely equivalent in what they select: the same compiled match, so
    # there is one behaviour rather than two spellings that could drift.
    assert warnings == []
    assert scalar.match == listed.match


def test_naming_several_kinds_does_not_change_the_layer(demo_channel, warn, warnings):
    """Grain is how finely the axis is written, not how much it ends up reaching.

    The list form has to layer exactly where each of the rules it replaces
    layered, or collapsing two rules into one would silently reorder them
    against everything around them.
    """
    scalar, listed = _compile(
        [
            {
                "match": {"channel": "demo", "chat_type": "direct"},
                "delivery": {"prompt": "one"},
            },
            {
                "match": {"channel": "demo", "chat_type": ["room", "direct"]},
                "delivery": {"prompt": "both"},
            },
        ],
        warn,
    )

    assert warnings == []
    assert scalar.match.specificity == listed.match.specificity == (1, 0, 1, 0)
    # And still between the platform-only rule and the one naming a room.
    assert (
        ScopeMatch(channel="demo").specificity
        < listed.match.specificity
        < ScopeMatch(channel="demo", chat="D1").specificity
    )


def test_duplicates_are_a_set_and_are_deduped_rather_than_refused(
    demo_channel, warn, warnings
):
    scopes = _compile(
        [
            {
                "match": {"channel": "demo", "chat_type": ["room", "room"]},
                "delivery": {"mode": ["all"]},
            }
        ],
        warn,
    )

    # It names the set {room}, which says nothing wrong and selects exactly what
    # it reads as selecting. Refusing it would drop a rule for a spelling.
    assert warnings == []
    assert scopes[0].match.chat_type == ("room",)
    assert scopes[0].match.describe() == "{channel: demo, chat_type: room}"


def test_a_list_of_kinds_is_described_as_a_list(demo_channel, warn):
    scopes = _compile(
        [
            {
                "match": {"channel": "demo", "chat_type": ["room", "direct"]},
                "delivery": {"mode": ["all"]},
            }
        ],
        warn,
    )

    # Sorted, because the order of a set means nothing and two spellings of one
    # rule must not describe themselves differently.
    assert scopes[0].match.describe() == "{channel: demo, chat_type: [direct, room]}"


def test_an_empty_list_of_kinds_is_refused_rather_than_read_as_every_kind(
    demo_channel, warn, warnings
):
    scopes = _compile(
        [
            {
                "match": {"channel": "demo", "chat_type": []},
                "delivery": {"mode": ["all"]},
            }
        ],
        warn,
    )

    # The silent-widening direction, and the one reading this module never
    # takes: an author who wrote the key meant to name a kind.
    assert scopes == ()
    assert any(
        "names no kind of conversation" in line and "not read as every kind" in line
        for line in warnings
    )


def test_a_bad_entry_in_a_list_is_named(demo_channel, warn, warnings):
    scopes = _compile(
        [
            {
                "match": {"channel": "demo", "chat_type": ["room", "thread"]},
                "delivery": {"mode": ["all"]},
            }
        ],
        warn,
    )

    # Named, with the vocabulary beside it, exactly as a bad scalar is.
    assert any("thread" in line and "direct, room" in line for line in warnings)
    # And the rule is not called inert, because it is not: the half that reads
    # is still in force, and saying otherwise sends an operator hunting for a
    # rule that is applying.
    assert not any("will never match" in line for line in warnings)
    assert scopes[0].selects(channel="demo", chat="C1", chat_type="room")


def test_a_list_of_only_bad_entries_is_reported_the_way_a_bad_scalar_is(
    demo_channel, warn, warnings
):
    scopes = _compile(
        [
            {
                "match": {"channel": "demo", "chat_type": ["thread", "huddle"]},
                "delivery": {"mode": ["all"]},
            }
        ],
        warn,
    )

    assert any("will never match" in line for line in warnings)
    assert any("[huddle, thread]" in line for line in warnings)
    assert scopes and not scopes[0].selects(
        channel="demo", chat="C1", chat_type="room"
    )


def test_a_list_holding_something_that_is_not_a_word_drops_the_scope(
    demo_channel, warn, warnings
):
    scopes = _compile(
        [
            {
                "match": {"channel": "demo", "chat_type": ["room", 7]},
                "delivery": {"mode": ["all"]},
            }
        ],
        warn,
    )

    assert scopes == ()
    assert any("is not a list of kinds of conversation" in line for line in warnings)


def test_an_empty_word_in_a_list_drops_the_scope(demo_channel, warn, warnings):
    scopes = _compile(
        [
            {
                "match": {"channel": "demo", "chat_type": ["room", "  "]},
                "delivery": {"mode": ["all"]},
            }
        ],
        warn,
    )

    # An empty kind would match a request the connector gave no kind for, which
    # is the case `selects` fails closed on. Reading it as one kind of the two
    # would quietly widen the rule to those requests.
    assert scopes == ()
    assert any("has an empty kind in it" in line for line in warnings)


def test_a_mapping_where_kinds_are_wanted_drops_the_scope(
    demo_channel, warn, warnings
):
    scopes = _compile(
        [
            {
                "match": {"channel": "demo", "chat_type": {"room": True}},
                "delivery": {"mode": ["all"]},
            }
        ],
        warn,
    )

    assert scopes == ()
    assert any(
        "is not a kind of conversation or a list of them" in line
        for line in warnings
    )


# --------------------------------------------------------------------------
# The vocabulary
# --------------------------------------------------------------------------


def test_a_kind_the_platform_does_not_have_is_named_in_the_warning(
    demo_channel, warn, warnings
):
    scopes = _compile(
        [
            {
                "match": {"channel": "demo", "chat_type": "thread"},
                "delivery": {"mode": ["all"]},
            }
        ],
        warn,
    )

    # Named, and the alternatives with it: the operator's next edit has to be
    # one of the words that works, and only the declaration knows them.
    assert any("chat_type=thread" in line for line in warnings)
    assert any("direct, room" in line for line in warnings)
    # Warned rather than dropped. A kind no conversation carries matches
    # nothing, which already leaves those conversations on the layer below.
    assert scopes and not scopes[0].selects(
        channel="demo", chat="D1", chat_type="direct"
    )


def test_a_platform_with_no_kinds_is_told_so_rather_than_told_it_typed_one(
    typeless_channel, warn, warnings
):
    _compile(
        [
            {
                "match": {"channel": "flat", "chat_type": "direct"},
                "delivery": {"mode": ["all"]},
            }
        ],
        warn,
    )

    # Two different fixes, so two different sentences. Here there is no word
    # that would have worked, and listing an empty vocabulary would read as one.
    assert any(
        "does not say what kind of conversation" in line for line in warnings
    )
    assert not any("The kinds there are" in line for line in warnings)


def test_an_axis_without_a_declared_vocabulary_is_taken_as_written(warn, warnings):
    snapshot = caps.snapshot_registry()
    register_channel(
        ChannelCapabilities(
            channel="open",
            axes=frozenset({"channel", "chat_type"}),
            sections={"delivery": None},
        )
    )
    try:
        scopes = _compile(
            [
                {
                    "match": {"channel": "open", "chat_type": "whatever"},
                    "delivery": {"mode": ["all"]},
                }
            ],
            warn,
        )
    finally:
        caps.restore_registry(snapshot)

    # No entry is not an empty entry. A platform that populates the axis and
    # declares no vocabulary has not said the word is wrong, and inventing a
    # refusal would bar an axis whose values are ids on some other platform.
    assert warnings == []
    assert scopes[0].selects(channel="open", chat=None, chat_type="whatever")


# --------------------------------------------------------------------------
# chat and chat_type in one rule
# --------------------------------------------------------------------------


def test_chat_and_chat_type_together_are_refused_by_name(
    demo_channel, warn, warnings
):
    scopes = _compile(
        [
            {
                "match": {"channel": "demo", "chat": "D1", "chat_type": "direct"},
                "delivery": {"mode": ["all"]},
            }
        ],
        warn,
    )

    assert scopes == ()
    assert any(
        "chat=D1" in line and "chat_type=direct" in line for line in warnings
    )


def test_the_refusal_does_not_depend_on_the_two_disagreeing(
    demo_channel, warn, warnings
):
    """Structural, and asserted as such.

    The pair is refused because a named conversation has one type already, not
    because this particular id is of some other type. Settling that would mean
    asking the platform, at config-apply time, about a conversation that may not
    exist yet -- so both the agreeing and the disagreeing rule are refused, by
    the same line.
    """
    scopes = _compile(
        [
            {
                "match": {"channel": "demo", "chat": "D1", "chat_type": "room"},
                "delivery": {"mode": ["all"]},
            },
            {
                "match": {"channel": "demo", "chat": "D1", "chat_type": "direct"},
                "delivery": {"mode": ["all"]},
            },
        ],
        warn,
    )

    assert scopes == ()
    assert sum("is redundant where they agree" in line for line in warnings) == 2


def test_a_list_of_kinds_beside_a_chat_is_refused_the_same_way(
    demo_channel, warn, warnings
):
    """A conversation still has one type, however many the rule names.

    So the pair is the same redundancy where the type is in the set and the same
    empty match where it is not, and the refusal is the scalar one with the
    kinds printed as the list they were written as.
    """
    scopes = _compile(
        [
            {
                "match": {
                    "channel": "demo",
                    "chat": "D1",
                    "chat_type": ["room", "direct"],
                },
                "delivery": {"mode": ["all"]},
            }
        ],
        warn,
    )

    assert scopes == ()
    assert any(
        "chat=D1" in line and "chat_type=[direct, room]" in line
        for line in warnings
    )


def test_a_kind_still_needs_the_channel_it_belongs_to(warn, warnings):
    scopes = _compile(
        [{"match": {"chat_type": "direct"}, "delivery": {"mode": ["all"]}}], warn
    )

    # The kinds are each platform's own words, so with no platform there is
    # nothing to check "direct" against and the rule would select on every
    # connector that happens to spell a kind the same way.
    assert scopes == ()
    assert any("chat_type=direct" in line for line in warnings)
    assert any("Add channel: alongside chat_type:" in line for line in warnings)


def test_a_match_built_in_code_with_both_takes_the_finer_grain():
    """The loader refuses the pair, so only a match built in code can hold it.

    Asserted because the grain is read from whatever a ``ScopeMatch`` holds, and
    the axes AND: an intersection is no wider than either half, so the finer of
    the two is the honest answer if one is ever constructed.
    """
    both = ScopeMatch(channel="demo", chat="D1", chat_type=("direct",))

    assert both.specificity == (1, 0, 2, 0)
