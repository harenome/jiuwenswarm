# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.

"""Composition by value type, and what layer 0 is and is not for."""

from __future__ import annotations

from typing import Any

import pytest

from jiuwenswarm.common.scopes import (
    ChannelCapabilities,
    compile_scopes,
    compose_section,
    compose_values,
    register_channel,
    scoped_chats,
    signed_entries,
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
            axes=frozenset({"channel", "chat"}),
            sections={"delivery": None, "agent": None},
        )
    )
    yield
    caps.restore_registry(snapshot)


# --------------------------------------------------------------------------
# Scalars
# --------------------------------------------------------------------------


def test_a_scalar_is_replaced_by_the_later_layer():
    assert compose_values([{"model_name": "a"}, {"model_name": "b"}]) == {"model_name": "b"}


def test_a_scalar_no_layer_set_is_absent_rather_than_defaulted():
    assert compose_values([{"prompt": "x"}], base={"model_name": "a"}) == {"prompt": "x"}


def test_a_falsy_scalar_is_a_value_and_not_an_absence():
    # An operator who writes an empty prompt is asking for nothing to be
    # appended. Reading that as "absent" would append the layer below instead:
    # the setting switched on by the value written to switch it off.
    assert compose_values([{"prompt": "x"}, {"prompt": ""}]) == {"prompt": ""}


# --------------------------------------------------------------------------
# Sets
# --------------------------------------------------------------------------


def test_a_plain_list_replaces_outright():
    composed = compose_values(
        [{"mode": ["mention", "reply"]}], base={"mode": frozenset({"all"})}
    )
    assert composed == {"mode": frozenset({"mention", "reply"})}


def test_an_empty_list_replaces_with_nothing():
    composed = compose_values([{"mode": []}], base={"mode": frozenset({"mention"})})
    assert composed == {"mode": frozenset()}


def test_a_signed_addition_inherits_layer_zero():
    composed = compose_values(
        [{"mode": ["+has_file"]}], base={"mode": frozenset({"mention"})}
    )
    assert composed == {"mode": frozenset({"mention", "has_file"})}


def test_a_signed_removal_inherits_layer_zero():
    composed = compose_values(
        [{"mode": ["-mention"]}], base={"mode": frozenset({"mention", "all"})}
    )
    assert composed == {"mode": frozenset({"all"})}


def test_a_signed_layer_inherits_the_layer_above_it_rather_than_layer_zero():
    composed = compose_values(
        [{"mode": ["url"]}, {"mode": ["+has_file"]}],
        base={"mode": frozenset({"mention"})},
    )
    assert composed == {"mode": frozenset({"url", "has_file"})}


def test_signs_may_be_mixed_with_each_other_within_one_list():
    composed = compose_values(
        [{"mode": ["+has_file", "-mention"]}], base={"mode": frozenset({"mention"})}
    )
    assert composed == {"mode": frozenset({"has_file"})}


def test_removing_something_absent_is_not_an_error():
    composed = compose_values([{"mode": ["-url"]}], base={"mode": frozenset({"mention"})})
    assert composed == {"mode": frozenset({"mention"})}


def test_a_signed_list_with_no_base_at_all_starts_from_nothing():
    assert compose_values([{"mode": ["+url"]}]) == {"mode": frozenset({"url"})}


def test_a_mixed_list_is_rejected_at_load_and_never_reaches_composition(
    demo_channel, warn, warnings
):
    scopes = compile_scopes(
        [{"match": {"channel": "demo"}, "delivery": {"mode": ["mention", "+has_file"]}}],
        warn=warn,
    )

    # The key is dropped, so the scope holds nothing and is not kept at all.
    assert scopes == ()
    assert any(
        "mixes plain entries with +/- ones" in line for line in warnings
    ), warnings


def test_a_mixed_list_does_not_take_the_rest_of_the_scope_with_it(
    demo_channel, warn, warnings
):
    scopes = compile_scopes(
        [
            {
                "match": {"channel": "demo"},
                "delivery": {"mode": ["mention", "+has_file"], "prompt": "keep me"},
            }
        ],
        warn=warn,
    )

    composed = compose_section(scopes, channel="demo")
    assert composed == {"prompt": "keep me"}
    assert any("mixes plain entries" in line for line in warnings)


@pytest.mark.parametrize(
    "value,expected",
    [
        (["a", "b"], None),
        ([], None),
        (["+a", "-b"], ("+a", "-b")),
        (["a", "+b"], ()),
        (["+a", "b"], ()),
        ("mention", None),
    ],
)
def test_signed_entries_classifies_a_list(value, expected):
    assert signed_entries(value) == expected


def test_a_sign_with_nothing_after_it_is_ignored(demo_channel, warn, warnings):
    scopes = compile_scopes(
        [{"match": {"channel": "demo"}, "delivery": {"mode": ["+"]}}], warn=warn
    )

    assert scopes == ()
    assert any("has a sign with nothing after it" in line for line in warnings)


def test_a_list_of_non_names_is_ignored(demo_channel, warn, warnings):
    scopes = compile_scopes(
        [{"match": {"channel": "demo"}, "delivery": {"mode": [1, 2]}}], warn=warn
    )

    assert scopes == ()
    assert any("is not a list of names" in line for line in warnings)


# --------------------------------------------------------------------------
# Prose
# --------------------------------------------------------------------------


def test_prose_replaces():
    assert compose_values([{"prompt": "a"}, {"prompt": "b"}]) == {"prompt": "b"}


def test_append_adds_to_the_layer_above():
    composed = compose_values([{"prompt": "a"}, {"prompt_append": "b"}])
    assert composed == {"prompt": "a\n\nb"}


def test_append_adds_to_layer_zero_when_no_scope_replaced_it():
    composed = compose_values([{"prompt_append": "b"}], base={"prompt": "a"})
    assert composed == {"prompt": "a\n\nb"}


def test_append_alone_with_nothing_to_append_to_is_just_the_text():
    assert compose_values([{"prompt_append": "b"}]) == {"prompt": "b"}


def test_replace_and_append_in_one_layer_append_to_the_replacement():
    composed = compose_values(
        [{"prompt": "new", "prompt_append": "more"}], base={"prompt": "old"}
    )
    assert composed == {"prompt": "new\n\nmore"}


def test_the_append_key_itself_never_survives_into_the_result():
    composed = compose_values([{"prompt_append": "b"}])
    assert "prompt_append" not in composed


def test_a_non_text_append_is_ignored_at_load(demo_channel, warn, warnings):
    scopes = compile_scopes(
        [{"match": {"channel": "demo"}, "delivery": {"prompt_append": ["a"]}}], warn=warn
    )

    assert scopes == ()
    assert any("prompt_append" in line and "is not text" in line for line in warnings)


# --------------------------------------------------------------------------
# Maps
# --------------------------------------------------------------------------


def test_a_map_merges_per_key_rather_than_replacing():
    composed = compose_values([{"tools": {"bash": "ask"}}, {"tools": {"write": "deny"}}])
    assert composed == {"tools": {"bash": "ask", "write": "deny"}}


def test_a_map_merge_seeds_from_layer_zero():
    composed = compose_values([{"tools": {"write": "deny"}}], base={"tools": {"bash": "ask"}})
    assert composed == {"tools": {"bash": "ask", "write": "deny"}}


# --------------------------------------------------------------------------
# Composition through the matcher
# --------------------------------------------------------------------------


def test_layers_compose_per_key_so_a_scope_need_not_restate_the_rest(demo_channel, warn):
    scopes = compile_scopes(
        [
            {
                "match": {"channel": "demo"},
                "delivery": {"mode": ["mention"], "prompt": "base"},
            },
            {"match": {"channel": "demo", "chat": "C1"}, "delivery": {"prompt": "hi"}},
        ],
        warn=warn,
    )

    composed = compose_section(scopes, channel="demo", chat="C1")
    assert composed == {"mode": frozenset({"mention"}), "prompt": "hi"}


def test_the_agent_section_composes_across_layers_like_any_other(demo_channel, warn):
    scopes = compile_scopes(
        [
            {"match": {"channel": "demo"}, "agent": {"model_name": "base-model"}},
            {
                "match": {"channel": "demo", "chat": "C1"},
                "agent": {"model_name": "pinned-model"},
            },
        ],
        warn=warn,
    )

    # Nothing about agent needed writing: composition follows the value's type,
    # so a scalar in a second section replaces the way a scalar in the first one
    # does. That the section cost no new rule is the property worth pinning.
    assert compose_section(scopes, channel="demo", chat="C1", section="agent") == {
        "model_name": "pinned-model"
    }
    assert compose_section(scopes, channel="demo", chat="C2", section="agent") == {
        "model_name": "base-model"
    }


def test_a_conversation_named_only_by_the_agent_section_still_inherits_delivery(
    demo_channel, warn
):
    scopes = compile_scopes(
        [
            {"match": {"channel": "demo"}, "delivery": {"mode": ["mention"]}},
            {
                "match": {"channel": "demo", "chat": "C1"},
                "agent": {"model_name": "pinned-model"},
            },
        ],
        warn=warn,
    )

    assert compose_section(scopes, channel="demo", chat="C1") == {
        "mode": frozenset({"mention"})
    }
    assert compose_section(scopes, channel="demo", chat="C1", section="agent") == {
        "model_name": "pinned-model"
    }


def test_the_two_sections_never_see_each_others_keys(demo_channel, warn):
    # One name, two sections, two meanings -- the shape "three sections, three
    # readers" has to survive. Folding them together would let whichever layer
    # came last decide both, and neither reader would notice.
    scopes = compile_scopes(
        [
            {
                "match": {"channel": "demo"},
                "delivery": {"label": "for the connector"},
                "agent": {"label": "for the runtime"},
            }
        ],
        warn=warn,
    )

    assert compose_section(scopes, channel="demo") == {"label": "for the connector"}
    assert compose_section(scopes, channel="demo", section="agent") == {
        "label": "for the runtime"
    }


def test_a_conversation_no_scope_names_gets_the_platform_layer_only(demo_channel, warn):
    scopes = compile_scopes(
        [
            {"match": {"channel": "demo"}, "delivery": {"mode": ["mention"]}},
            {"match": {"channel": "demo", "chat": "C1"}, "delivery": {"mode": ["all"]}},
        ],
        warn=warn,
    )

    assert compose_section(scopes, channel="demo", chat="C2") == {
        "mode": frozenset({"mention"})
    }


def test_nothing_matching_settles_nothing(demo_channel, warn):
    scopes = compile_scopes(
        [{"match": {"channel": "demo", "chat": "C1"}, "delivery": {"mode": ["all"]}}],
        warn=warn,
    )

    # An empty result must read as "carry on as before", never as a set of
    # empty values: the caller's own defaults are still in charge.
    assert compose_section(scopes, channel="demo", chat="C9") == {}


def test_scoped_chats_names_the_conversations_a_connector_must_settle(demo_channel, warn):
    scopes = compile_scopes(
        [
            {"match": {"channel": "demo"}, "delivery": {"mode": ["mention"]}},
            {"match": {"channel": "demo", "chat": "C2"}, "delivery": {"mode": ["all"]}},
            {"match": {"channel": "demo", "chat": "C1"}, "delivery": {"mode": ["all"]}},
        ],
        warn=warn,
    )

    assert scoped_chats(scopes, channel="demo") == ("C1", "C2")


def test_scoped_chats_counts_a_conversation_named_only_by_the_agent_section(
    demo_channel, warn
):
    # The list a connector iterates. A per-section default would have left this
    # conversation out of it, and the setting would have been lost rather than
    # reported -- silently, because every other check had passed.
    scopes = compile_scopes(
        [
            {"match": {"channel": "demo", "chat": "C1"}, "delivery": {"mode": ["all"]}},
            {"match": {"channel": "demo", "chat": "C2"}, "agent": {"model_name": "m"}},
        ],
        warn=warn,
    )

    assert scoped_chats(scopes, channel="demo") == ("C1", "C2")
    assert scoped_chats(scopes, channel="demo", sections=("agent",)) == ("C2",)
