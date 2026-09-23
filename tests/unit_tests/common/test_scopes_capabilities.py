# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.

"""The capability registry, and the four things it warns about.

The declaration is the opt-in, so every warning here comes from comparing what a
scope asked for against what a channel said it supports. The four classes are:

1. the scope names a channel that has not declared     -- inert
2. it uses an axis that channel cannot fill            -- will never match
3. it sets a section or key that channel ignores       -- has no effect
4. it sets a key the connector block already sets      -- two homes for one value

All four are warnings and none of them refuses a load. A config mistake must
not take a service down, which is the rule the mechanism this generalises
already followed.
"""

from __future__ import annotations

from typing import Any

import pytest

from jiuwenswarm.common.scopes import (
    AXIS_CHAT_TYPE,
    MID_TURN_CANCEL,
    MID_TURN_DEFAULT,
    MID_TURN_QUEUE,
    MID_TURN_STEER,
    MID_TURN_VALUES,
    SESSION_CHANNEL,
    SESSION_DEFAULT,
    SESSION_THREAD,
    SESSION_VALUES,
    ChannelCapabilities,
    channel_capabilities,
    compile_scopes,
    compose_section,
    known_channels,
    register_channel,
    scoped_chats,
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
def registry():
    snapshot = caps.snapshot_registry()
    yield register_channel
    caps.restore_registry(snapshot)


# --------------------------------------------------------------------------
# The declaration itself
# --------------------------------------------------------------------------


def test_a_declaration_cannot_be_edited_through_the_registry(registry):
    registry(
        ChannelCapabilities(
            channel="demo", axes=frozenset({"channel"}), sections={"delivery": None}
        )
    )
    declared = channel_capabilities("demo")

    with pytest.raises(TypeError):
        declared.sections["delivery"] = frozenset()  # type: ignore[index]


def test_no_declaration_means_not_opted_in():
    assert channel_capabilities("a-channel-nobody-wrote") is None


def test_the_pseudo_channels_ship_declared():
    # Cron and the heartbeat drive real turns and have no connector to hang a
    # declaration on, so theirs live in the registry module itself.
    assert "__cron__" in known_channels()
    assert "__heartbeat__" in known_channels()


def test_cron_is_declared_unattended():
    # Nothing reads this yet. It is recorded now because the answer is known
    # now, and a half-filled table is how a capability question gets asked twice.
    assert channel_capabilities("__cron__").attended is False


def test_web_is_one_conversation_and_says_so():
    assert channel_capabilities("web").axes == frozenset({"channel"})


# --------------------------------------------------------------------------
# 1 -- unknown channel
# --------------------------------------------------------------------------


def test_a_scope_naming_a_channel_that_has_not_declared_warns_as_inert(warn, warnings):
    compile_scopes(
        [{"match": {"channel": "carrier-pigeon"}, "delivery": {"mode": ["all"]}}],
        warn=warn,
    )

    assert any(
        "carrier-pigeon is not a channel that supports scopes" in line
        for line in warnings
    ), warnings


def test_the_unknown_channel_warning_names_the_channels_that_do_support_it(warn, warnings):
    compile_scopes([{"match": {"channel": "nope"}, "delivery": {"mode": ["all"]}}], warn=warn)

    assert any("__cron__" in line and "web" in line for line in warnings)


def test_an_inert_scope_is_still_compiled_because_it_harms_nothing(warn):
    # It can never be selected -- no request arrives on a channel that does not
    # exist -- so dropping it would buy nothing and lose the warning's subject.
    scopes = compile_scopes(
        [{"match": {"channel": "carrier-pigeon"}, "delivery": {"mode": ["all"]}}],
        warn=warn,
    )
    assert len(scopes) == 1


# --------------------------------------------------------------------------
# 2 -- an axis the channel cannot populate
# --------------------------------------------------------------------------


def test_a_chat_axis_on_a_channel_with_one_conversation_warns_it_will_never_match(
    warn, warnings
):
    compile_scopes(
        [{"match": {"channel": "web", "chat": "C1"}, "delivery": {"mode": ["all"]}}],
        warn=warn,
    )

    assert any(
        "web does not identify a conversation" in line and "never match" in line
        for line in warnings
    ), warnings


def test_matching_on_channel_alone_where_that_is_all_there_is_does_not_warn(warn, warnings):
    compile_scopes([{"match": {"channel": "web"}, "delivery": {"mode": ["all"]}}], warn=warn)

    assert warnings == []


def test_a_channel_that_declares_the_axis_is_not_warned_about(registry, warn, warnings):
    registry(
        ChannelCapabilities(
            channel="demo",
            axes=frozenset({"channel", "chat"}),
            sections={"delivery": None},
        )
    )
    compile_scopes(
        [{"match": {"channel": "demo", "chat": "C1"}, "delivery": {"mode": ["all"]}}],
        warn=warn,
    )

    assert warnings == []


# --------------------------------------------------------------------------
# 3 -- a section or key the channel ignores
# --------------------------------------------------------------------------


def test_delivery_on_cron_warns_that_it_has_no_effect(warn, warnings):
    # Cron has no trigger, no mention and no thread, so there is nothing for
    # delivery to decide. It declares no delivery section rather than an empty
    # one, and absence is what produces this line.
    compile_scopes(
        [{"match": {"channel": "__cron__"}, "delivery": {"mode": ["all"]}}], warn=warn
    )

    assert any(
        "has no effect" in line and "__cron__ does not read the delivery section" in line
        for line in warnings
    ), warnings


def test_a_section_the_channel_ignores_is_dropped_rather_than_carried(warn):
    scopes = compile_scopes(
        [{"match": {"channel": "__cron__"}, "delivery": {"mode": ["all"]}}], warn=warn
    )
    assert scopes == ()


def test_a_section_a_channel_declares_and_a_key_it_does_not_are_different_lines(
    registry, warn, warnings
):
    # The shape a key that moved sections produces. The channel reads agent, so
    # the section is not the complaint; model_name is simply not one of the
    # delivery settings any more, and the line has to say which of the two it
    # is or an operator upgrading a config cannot tell what to move.
    registry(
        ChannelCapabilities(
            channel="demo",
            axes=frozenset({"channel"}),
            sections={"delivery": frozenset({"mode"}), "agent": frozenset({"model_name"})},
        )
    )
    scopes = compile_scopes(
        [
            {
                "match": {"channel": "demo"},
                "delivery": {"mode": ["all"], "model_name": "m"},
            }
        ],
        warn=warn,
    )

    assert any(
        "delivery.model_name has no effect" in line and "demo ignores it" in line
        for line in warnings
    ), warnings
    assert not any("does not read the delivery section" in line for line in warnings)
    # The rest of the rule stands. A key in the wrong section costs that key and
    # nothing else, which is what makes the warning actionable rather than a
    # report of a config that stopped working.
    assert compose_section(scopes, channel="demo") == {"mode": frozenset({"all"})}


def test_a_key_the_channel_ignores_warns_and_is_dropped(registry, warn, warnings):
    registry(
        ChannelCapabilities(
            channel="demo",
            axes=frozenset({"channel"}),
            sections={"delivery": frozenset({"mode"})},
        )
    )
    scopes = compile_scopes(
        [{"match": {"channel": "demo"}, "delivery": {"mode": ["all"], "prompt": "hi"}}],
        warn=warn,
    )

    assert any(
        "delivery.prompt has no effect" in line and "demo ignores it" in line
        for line in warnings
    ), warnings
    assert compose_section(scopes, channel="demo") == {"mode": frozenset({"all"})}


def test_the_ignored_key_warning_lists_what_the_channel_does_read(registry, warn, warnings):
    registry(
        ChannelCapabilities(
            channel="demo",
            axes=frozenset({"channel"}),
            sections={"delivery": frozenset({"mode"})},
        )
    )
    compile_scopes(
        [{"match": {"channel": "demo"}, "delivery": {"prompt": "hi"}}], warn=warn
    )

    assert any("it reads are mode" in line for line in warnings), warnings


def test_an_append_key_is_checked_against_the_key_it_appends_to(registry, warn, warnings):
    registry(
        ChannelCapabilities(
            channel="demo",
            axes=frozenset({"channel"}),
            sections={"delivery": frozenset({"prompt"})},
        )
    )
    compile_scopes(
        [{"match": {"channel": "demo"}, "delivery": {"prompt_append": "hi"}}], warn=warn
    )

    # prompt_append is not itself a declared key and must not have to be:
    # declaring prompt is declaring the thing being appended to.
    assert warnings == []


def test_a_channel_declaring_all_keys_never_warns_about_one(registry, warn, warnings):
    registry(
        ChannelCapabilities(
            channel="demo", axes=frozenset({"channel"}), sections={"delivery": None}
        )
    )
    compile_scopes(
        [{"match": {"channel": "demo"}, "delivery": {"anything_at_all": "x"}}], warn=warn
    )

    assert warnings == []


def test_a_scope_with_no_channel_is_not_checked_against_any_declaration(warn, warnings):
    # There is no channel to ask. Guessing one would be the wrong answer for
    # every other channel the scope also applies to.
    compile_scopes([{"match": {}, "delivery": {"whatever": "x"}}], warn=warn)

    assert warnings == []


# --------------------------------------------------------------------------
# 4 -- a key the connector block already sets
# --------------------------------------------------------------------------


def test_a_key_the_connector_block_already_sets_warns_about_two_homes(
    registry, warn, warnings
):
    registry(
        ChannelCapabilities(
            channel="demo",
            axes=frozenset({"channel", "chat"}),
            sections={"delivery": None},
            layer0_keys={"delivery": {"mode": "group_chat_mode"}},
        )
    )
    compile_scopes(
        [{"match": {"channel": "demo"}, "delivery": {"mode": ["all"]}}],
        channels_config={"demo": {"group_chat_mode": "mention"}},
        warn=warn,
    )

    assert any(
        "channels.demo.group_chat_mode both set this" in line for line in warnings
    ), warnings


def test_that_warning_says_the_connector_setting_now_governs_nothing(
    registry, warn, warnings
):
    registry(
        ChannelCapabilities(
            channel="demo",
            axes=frozenset({"channel", "chat"}),
            sections={"delivery": None},
            layer0_keys={"delivery": {"mode": "group_chat_mode"}},
        )
    )
    compile_scopes(
        [{"match": {"channel": "demo"}, "delivery": {"mode": ["all"]}}],
        channels_config={"demo": {"group_chat_mode": "mention"}},
        warn=warn,
    )

    assert any("now governs nothing" in line for line in warnings), warnings


def test_a_scope_naming_a_kind_is_not_redundant_with_the_connector_block(
    registry, warn, warnings
):
    """A kind rule leaves every other kind on layer 0, so the connector setting
    still governs them.

    The same argument the conversation case makes. ``{chat_type: channel}``
    settles nothing for a DM, and on Slack the DMs are most of the conversations
    a deployment has, so reporting ``group_chat_mode`` as dead would be false.
    """
    registry(
        ChannelCapabilities(
            channel="demo",
            axes=frozenset({"channel", "chat", "chat_type"}),
            sections={"delivery": None},
            layer0_keys={"delivery": {"mode": "group_chat_mode"}},
        )
    )
    compile_scopes(
        [
            {
                "match": {"channel": "demo", "chat_type": "channel"},
                "delivery": {"mode": ["all"]},
            }
        ],
        channels_config={"demo": {"group_chat_mode": "mention"}},
        warn=warn,
    )

    assert not any("now governs nothing" in line for line in warnings), warnings


def test_a_scope_naming_a_conversation_is_not_redundant_with_the_connector_block(
    registry, warn, warnings
):
    # Layer 0 has no per-conversation form, so there is nothing to be redundant
    # with, and the connector setting still governs every other conversation --
    # calling it dead would be false as well as noisy. It is also the shape
    # every real config has: group_chat_mode ships with a value, so warning here
    # would fire on every per-conversation mode anyone ever writes.
    registry(
        ChannelCapabilities(
            channel="demo",
            axes=frozenset({"channel", "chat"}),
            sections={"delivery": None},
            layer0_keys={"delivery": {"mode": "group_chat_mode"}},
        )
    )
    compile_scopes(
        [{"match": {"channel": "demo", "chat": "C1"}, "delivery": {"mode": ["all"]}}],
        channels_config={"demo": {"group_chat_mode": "mention"}},
        warn=warn,
    )

    assert warnings == []


def test_the_key_still_applies_because_layering_is_the_feature(registry, warn):
    registry(
        ChannelCapabilities(
            channel="demo",
            axes=frozenset({"channel"}),
            sections={"delivery": None},
            layer0_keys={"delivery": {"mode": "group_chat_mode"}},
        )
    )
    scopes = compile_scopes(
        [{"match": {"channel": "demo"}, "delivery": {"mode": ["all"]}}],
        channels_config={"demo": {"group_chat_mode": "mention"}},
        warn=warn,
    )

    assert compose_section(scopes, channel="demo") == {"mode": frozenset({"all"})}


@pytest.mark.parametrize("empty", [None, "", [], {}])
def test_a_connector_key_left_empty_is_not_a_second_home(registry, warn, warnings, empty):
    registry(
        ChannelCapabilities(
            channel="demo",
            axes=frozenset({"channel"}),
            sections={"delivery": None},
            layer0_keys={"delivery": {"prompt": "carrier_pigeon_prompt"}},
        )
    )
    compile_scopes(
        [{"match": {"channel": "demo"}, "delivery": {"prompt": "hi"}}],
        channels_config={"demo": {"carrier_pigeon_prompt": empty}},
        warn=warn,
    )

    assert warnings == []


def test_a_key_with_no_connector_counterpart_is_not_warned_about(registry, warn, warnings):
    registry(
        ChannelCapabilities(
            channel="demo", axes=frozenset({"channel"}), sections={"delivery": None}
        )
    )
    compile_scopes(
        [{"match": {"channel": "demo"}, "delivery": {"mode": ["all"]}}],
        channels_config={"demo": {"group_chat_mode": "mention"}},
        warn=warn,
    )

    assert warnings == []


# --------------------------------------------------------------------------
# A connector's own value checks, kept in the connector
# --------------------------------------------------------------------------


def test_a_declared_validator_can_reject_a_value_without_the_loader_knowing_why(
    registry, warn, warnings
):
    def only_known_models(value: Any) -> "str | None":
        if value in ("good-model",):
            return None
        return f"={value!r} is not a configured model; ignoring it"

    registry(
        ChannelCapabilities(
            channel="demo",
            axes=frozenset({"channel", "chat"}),
            sections={"delivery": None, "agent": None},
            validators={"agent.model_name": only_known_models},
        )
    )
    scopes = compile_scopes(
        [
            {"match": {"channel": "demo"}, "agent": {"model_name": "good-model"}},
            {
                "match": {"channel": "demo", "chat": "C1"},
                "agent": {"model_name": "typo"},
                "delivery": {"prompt": "kept"},
            },
        ],
        warn=warn,
    )

    assert compose_section(scopes, channel="demo", chat="C1", section="agent") == {
        "model_name": "good-model"
    }
    assert compose_section(scopes, channel="demo", chat="C1") == {"prompt": "kept"}
    assert any("is not a configured model" in line for line in warnings), warnings


def test_a_validator_is_keyed_by_section_so_the_same_key_can_differ(
    registry, warn, warnings
):
    # The key is "<section>.<key>", so declaring agent.model_name says nothing
    # about a delivery.model_name -- which is exactly the discipline that lets a
    # key move between sections without its checks following it by accident.
    registry(
        ChannelCapabilities(
            channel="demo",
            axes=frozenset({"channel"}),
            sections={"delivery": None, "agent": None},
            validators={"agent.model_name": lambda value: "is refused here"},
        )
    )
    scopes = compile_scopes(
        [{"match": {"channel": "demo"}, "delivery": {"model_name": "anything"}}],
        warn=warn,
    )

    assert compose_section(scopes, channel="demo") == {"model_name": "anything"}
    assert warnings == []


# --------------------------------------------------------------------------
# A connector's second hook: what a value no request can reach
# --------------------------------------------------------------------------


@pytest.fixture
def caution_calls() -> list[tuple[Any, Any]]:
    """Everything a caution was handed, in the order it was handed it."""
    return []


@pytest.fixture
def caution_channel(registry, caution_calls):
    """A platform with two kinds of conversation and one key worth a caution.

    The caution stands in for the shape the hook exists for: a value that is
    spelled correctly and is a thing the channel implements, addressed at a
    rule no request will arrive through. What that is on a real connector is
    the connector's business; what is generic is that a reason warns and the
    value stays.
    """

    def read_only_in_a_direct_message(value: Any, match: Any) -> "str | None":
        caution_calls.append((value, match))
        kinds = match.chat_type
        if not kinds or "direct" in kinds:
            return None
        return f" is read in a direct message, and this rule names {', '.join(kinds)}"

    registry(
        ChannelCapabilities(
            channel="demo",
            axes=frozenset({"channel", "chat", "chat_type"}),
            sections={"delivery": None, "agent": None},
            axis_values={AXIS_CHAT_TYPE: frozenset({"room", "direct"})},
            cautions={"delivery.prompt": read_only_in_a_direct_message},
        )
    )
    return None


def test_a_caution_warns_and_keeps_the_value(caution_channel, warn, warnings):
    """The whole of what separates a caution from a validator.

    A refused value is dropped so the conversation falls to the layer below. A
    cautioned value is correct and merely unreachable, so it is kept and said
    out loud: dropping it would take the rest of the rule down with it, over an
    entry that costs nothing at runtime.
    """
    scopes = compile_scopes(
        [
            {
                "match": {"channel": "demo", "chat_type": "room"},
                "delivery": {"prompt": "hello tab", "mode": ["all"]},
            }
        ],
        warn=warn,
    )

    assert compose_section(scopes, channel="demo", chat="C1", chat_type="room") == {
        "prompt": "hello tab",
        "mode": frozenset({"all"}),
    }
    assert any("is read in a direct message" in line for line in warnings), warnings
    assert any("delivery.prompt" in line for line in warnings), warnings


def test_a_caution_is_handed_the_match_as_well_as_the_value(
    caution_channel, caution_calls, warn
):
    """Which is the reason it is a second hook rather than a validator.

    Reachability is a property of the pairing rather than of the value: the
    same setting is right on one rule and addressed at nothing on another, and
    a validator never sees the rule.
    """
    scopes = compile_scopes(
        [
            {
                "match": {"channel": "demo", "chat_type": "room"},
                "delivery": {"prompt": "hello tab"},
            }
        ],
        warn=warn,
    )

    assert len(caution_calls) == 1
    value, match = caution_calls[0]
    assert value == "hello tab"
    assert match is scopes[0].match
    assert match.chat_type == ("room",)


def test_a_caution_that_returns_nothing_says_nothing(
    caution_channel, warn, warnings
):
    scopes = compile_scopes(
        [
            {
                "match": {"channel": "demo", "chat_type": "direct"},
                "delivery": {"prompt": "hello tab"},
            }
        ],
        warn=warn,
    )

    assert compose_section(scopes, channel="demo", chat="D1", chat_type="direct") == {
        "prompt": "hello tab"
    }
    assert warnings == []


def test_a_key_with_no_caution_registered_is_untouched(
    registry, caution_calls, warn, warnings
):
    """A channel that registers none is a channel nothing changed for.

    The mapping defaults to empty and the accessor answers ``None``, so the
    hook costs a lookup and nothing else on every key nobody declared.
    """
    registry(
        ChannelCapabilities(
            channel="quiet",
            axes=frozenset({"channel", "chat_type"}),
            sections={"delivery": None},
            axis_values={AXIS_CHAT_TYPE: frozenset({"room", "direct"})},
        )
    )
    declared = channel_capabilities("quiet")

    scopes = compile_scopes(
        [
            {
                "match": {"channel": "quiet", "chat_type": "room"},
                "delivery": {"prompt": "hello tab"},
            }
        ],
        warn=warn,
    )

    assert declared.caution("delivery", "prompt") is None
    assert compose_section(scopes, channel="quiet", chat="C1", chat_type="room") == {
        "prompt": "hello tab"
    }
    assert caution_calls == []
    assert warnings == []


def test_a_caution_is_keyed_by_section_like_a_validator(
    caution_channel, caution_calls, warn, warnings
):
    # Declaring delivery.prompt says nothing about an agent.prompt, which is
    # the discipline that lets a key move between sections without the things
    # said about it following it by accident.
    scopes = compile_scopes(
        [
            {
                "match": {"channel": "demo", "chat_type": "room"},
                "agent": {"prompt": "hello tab"},
            }
        ],
        warn=warn,
    )

    assert compose_section(scopes, channel="demo", chat_type="room", section="agent") == {
        "prompt": "hello tab"
    }
    assert caution_calls == []
    assert warnings == []


def test_a_caution_is_not_consulted_for_a_value_the_validator_refused(
    registry, caution_calls, warn, warnings
):
    """The order is the contract, not an ordering that happened.

    There is nothing to say about the reach of a value that is not going to be
    kept, and a second line about a key already reported would read as a second
    fault.
    """

    def refuse_everything(value: Any) -> "str | None":
        return "=is refused here"

    def never_reached(value: Any, match: Any) -> "str | None":
        caution_calls.append((value, match))
        return " should not have been asked"

    registry(
        ChannelCapabilities(
            channel="demo",
            axes=frozenset({"channel", "chat_type"}),
            sections={"delivery": None},
            axis_values={AXIS_CHAT_TYPE: frozenset({"room", "direct"})},
            validators={"delivery.prompt": refuse_everything},
            cautions={"delivery.prompt": never_reached},
        )
    )
    scopes = compile_scopes(
        [
            {
                "match": {"channel": "demo", "chat_type": "room"},
                "delivery": {"prompt": "hello tab", "mode": ["all"]},
            }
        ],
        warn=warn,
    )

    # The refusal drops the key and leaves the rest of the rule standing.
    assert compose_section(scopes, channel="demo", chat_type="room") == {
        "mode": frozenset({"all"})
    }
    assert caution_calls == []
    assert any("is refused here" in line for line in warnings), warnings
    assert not any("should not have been asked" in line for line in warnings), warnings


# --------------------------------------------------------------------------
# delivery.mid_turn -- a vocabulary the schema owns rather than a connector
# --------------------------------------------------------------------------


@pytest.fixture
def mid_turn_channel(registry):
    """A channel that reads the whole delivery section, and nothing else."""
    registry(
        ChannelCapabilities(
            channel="demo",
            axes=frozenset({"channel", "chat"}),
            sections={"delivery": frozenset({"mode", "mid_turn"})},
        )
    )
    return None


@pytest.mark.parametrize("value", MID_TURN_VALUES)
def test_each_of_the_three_values_settles(mid_turn_channel, warn, warnings, value):
    scopes = compile_scopes(
        [{"match": {"channel": "demo"}, "delivery": {"mid_turn": value}}], warn=warn
    )

    assert compose_section(scopes, channel="demo") == {"mid_turn": value}
    assert warnings == []


def test_the_first_value_is_the_default(mid_turn_channel):
    # Not a naming preference. The list is read out in refusals, so it leads
    # with what an operator who writes nothing already has. Pinning the two
    # together is what stops the list and the default drifting apart: cancel
    # led it while cancel was the default, and moving one without the other
    # would leave a refusal recommending something else first.
    assert MID_TURN_VALUES[0] == MID_TURN_DEFAULT
    assert MID_TURN_DEFAULT == MID_TURN_QUEUE


def test_nothing_written_settles_nothing(mid_turn_channel, warn, warnings):
    scopes = compile_scopes(
        [{"match": {"channel": "demo"}, "delivery": {"mode": ["all"]}}], warn=warn
    )

    # Absent, not filled in with the default: the connector reads an absent key
    # as "no layer spoke", and a key present-and-equal-to-the-default would say
    # a scope had settled it.
    assert "mid_turn" not in compose_section(scopes, channel="demo")


def test_an_unrecognised_value_is_refused_and_leaves_the_layer_below(
    mid_turn_channel, warn, warnings
):
    scopes = compile_scopes(
        [
            {"match": {"channel": "demo"}, "delivery": {"mid_turn": "queue"}},
            {"match": {"channel": "demo", "chat": "C1"}, "delivery": {"mid_turn": "wait"}},
        ],
        warn=warn,
    )

    # The conversation keeps what the layer above it settled rather than a
    # behaviour nobody wrote.
    assert compose_section(scopes, channel="demo", chat="C1") == {
        "mid_turn": MID_TURN_QUEUE
    }
    listed = ", ".join(MID_TURN_VALUES)
    assert any(f"is not one of {listed}" in line for line in warnings), warnings
    assert any("leaves that conversation on the layer below" in line for line in warnings)


def test_a_value_that_is_not_a_word_at_all_is_refused(mid_turn_channel, warn, warnings):
    scopes = compile_scopes(
        [{"match": {"channel": "demo"}, "delivery": {"mid_turn": ["steer"]}}], warn=warn
    )

    assert compose_section(scopes, channel="demo") == {}
    listed = ", ".join(MID_TURN_VALUES)
    assert any(f"is not one of {listed}" in line for line in warnings), warnings


def test_case_and_space_are_settled_rather_than_refused(mid_turn_channel, warn, warnings):
    scopes = compile_scopes(
        [{"match": {"channel": "demo"}, "delivery": {"mid_turn": "  Queue "}}], warn=warn
    )

    # A closed vocabulary has nothing for a capital letter to collide with, and
    # refusing one would leave a conversation cancelling when it was written to
    # wait. The settled value is canonical, so a reader never has to fold it.
    assert compose_section(scopes, channel="demo") == {"mid_turn": MID_TURN_QUEUE}
    assert warnings == []


def test_appending_to_it_is_refused_because_it_is_not_prose(
    mid_turn_channel, warn, warnings
):
    scopes = compile_scopes(
        [
            {"match": {"channel": "demo"}, "delivery": {"mid_turn": "steer"}},
            {
                "match": {"channel": "demo", "chat": "C1"},
                "delivery": {"mid_turn_append": "queue"},
            },
        ],
        warn=warn,
    )

    # Appending to one of three words produces a fourth that is not one of them.
    assert compose_section(scopes, channel="demo", chat="C1") == {
        "mid_turn": MID_TURN_STEER
    }
    assert any("nothing to append to" in line for line in warnings), warnings


def test_a_later_layer_replaces_it_rather_than_combining(mid_turn_channel, warn):
    scopes = compile_scopes(
        [
            {"match": {"channel": "demo"}, "delivery": {"mid_turn": "steer"}},
            {
                "match": {"channel": "demo", "chat": "C1"},
                "delivery": {"mid_turn": "cancel"},
            },
        ],
        warn=warn,
    )

    # A scalar, so it composes the way every other scalar does.
    assert compose_section(scopes, channel="demo") == {"mid_turn": MID_TURN_STEER}
    assert compose_section(scopes, channel="demo", chat="C1") == {
        "mid_turn": MID_TURN_CANCEL
    }


def test_a_channel_that_does_not_declare_it_drops_it(registry, warn, warnings):
    registry(
        ChannelCapabilities(
            channel="demo",
            axes=frozenset({"channel"}),
            sections={"delivery": frozenset({"mode"})},
        )
    )
    scopes = compile_scopes(
        [{"match": {"channel": "demo"}, "delivery": {"mid_turn": "steer"}}], warn=warn
    )

    # The vocabulary being shared does not make the key universal: a connector
    # that has not implemented it says so by not declaring it.
    assert compose_section(scopes, channel="demo") == {}
    assert any("delivery.mid_turn has no effect" in line for line in warnings), warnings


def test_follow_up_is_not_one_of_the_values(mid_turn_channel, warn, warnings):
    # It exists in the runtime and is deliberately not offered here: its answer
    # comes back on the first request's stream under the first request's id.
    scopes = compile_scopes(
        [{"match": {"channel": "demo"}, "delivery": {"mid_turn": "follow_up"}}], warn=warn
    )

    assert compose_section(scopes, channel="demo") == {}
    assert warnings


# --------------------------------------------------------------------------
# delivery.session -- the second vocabulary the schema owns
# --------------------------------------------------------------------------


@pytest.fixture
def session_channel(registry):
    """A channel that reads the session key, and nothing else in delivery."""
    registry(
        ChannelCapabilities(
            channel="demo",
            axes=frozenset({"channel", "chat"}),
            sections={"delivery": frozenset({"mode", "session"})},
        )
    )
    return None


@pytest.mark.parametrize("value", SESSION_VALUES)
def test_each_session_value_settles(session_channel, warn, warnings, value):
    scopes = compile_scopes(
        [{"match": {"channel": "demo"}, "delivery": {"session": value}}], warn=warn
    )

    assert compose_section(scopes, channel="demo") == {"session": value}
    assert warnings == []


def test_the_first_session_value_is_the_default(session_channel):
    # The same pinning mid_turn's list gets, and for the same reason: refusals
    # read the list out, so it has to lead with what an operator who wrote
    # nothing already has.
    assert SESSION_VALUES[0] == SESSION_DEFAULT
    assert SESSION_DEFAULT == SESSION_THREAD


def test_nothing_written_settles_no_session(session_channel, warn, warnings):
    scopes = compile_scopes(
        [{"match": {"channel": "demo"}, "delivery": {"mode": ["all"]}}], warn=warn
    )

    # Absent rather than filled in with the default, so that the connector can
    # tell "no layer spoke" from "a layer asked for thread".
    assert "session" not in compose_section(scopes, channel="demo")


def test_an_unrecognised_session_is_refused_and_leaves_the_layer_below(
    session_channel, warn, warnings
):
    scopes = compile_scopes(
        [
            {"match": {"channel": "demo"}, "delivery": {"session": "channel"}},
            {
                "match": {"channel": "demo", "chat": "C1"},
                "delivery": {"session": "workspace"},
            },
        ],
        warn=warn,
    )

    assert compose_section(scopes, channel="demo", chat="C1") == {
        "session": SESSION_CHANNEL
    }
    listed = ", ".join(SESSION_VALUES)
    assert any(f"is not one of {listed}" in line for line in warnings), warnings


def test_session_case_and_space_are_settled_rather_than_refused(
    session_channel, warn, warnings
):
    scopes = compile_scopes(
        [{"match": {"channel": "demo"}, "delivery": {"session": " Channel "}}], warn=warn
    )

    assert compose_section(scopes, channel="demo") == {"session": SESSION_CHANNEL}
    assert warnings == []


def test_appending_to_a_session_is_refused_because_it_is_not_prose(
    session_channel, warn, warnings
):
    scopes = compile_scopes(
        [
            {"match": {"channel": "demo"}, "delivery": {"session": "channel"}},
            {
                "match": {"channel": "demo", "chat": "C1"},
                "delivery": {"session_append": "thread"},
            },
        ],
        warn=warn,
    )

    assert compose_section(scopes, channel="demo", chat="C1") == {
        "session": SESSION_CHANNEL
    }
    assert any("nothing to append to" in line for line in warnings), warnings


def test_a_later_layer_replaces_the_session_rather_than_combining(session_channel, warn):
    scopes = compile_scopes(
        [
            {"match": {"channel": "demo"}, "delivery": {"session": "channel"}},
            {
                "match": {"channel": "demo", "chat": "C1"},
                "delivery": {"session": "thread"},
            },
        ],
        warn=warn,
    )

    assert compose_section(scopes, channel="demo") == {"session": SESSION_CHANNEL}
    assert compose_section(scopes, channel="demo", chat="C1") == {
        "session": SESSION_THREAD
    }


def test_a_channel_that_does_not_declare_a_session_drops_it(registry, warn, warnings):
    registry(
        ChannelCapabilities(
            channel="demo",
            axes=frozenset({"channel"}),
            sections={"delivery": frozenset({"mode"})},
        )
    )
    scopes = compile_scopes(
        [{"match": {"channel": "demo"}, "delivery": {"session": "channel"}}], warn=warn
    )

    # Sharing the vocabulary does not make the key universal: a platform with no
    # threads to group says so by not declaring it.
    assert compose_section(scopes, channel="demo") == {}
    assert any("delivery.session has no effect" in line for line in warnings), warnings


def test_user_is_not_one_of_the_session_values(session_channel, warn, warnings):
    # One session per person per room is a different feature with its own
    # questions about what the agent may then see, and nobody has asked for it.
    scopes = compile_scopes(
        [{"match": {"channel": "demo"}, "delivery": {"session": "user"}}], warn=warn
    )

    assert compose_section(scopes, channel="demo") == {}
    assert warnings


# --------------------------------------------------------------------------
# A chat named without its channel
# --------------------------------------------------------------------------


def test_a_chat_without_a_channel_is_dropped_rather_than_left_unvalidated(warn, warnings):
    """A conversation id is only meaningful inside a platform.

    Validation is keyed on ``match.channel``: with none, ``_compile_section``
    checks the rule against nothing -- not the key allow-list, not the layer-0
    map, not one connector validator -- and the rule still matches. Here the
    trigger name is not a trigger name, and without this refusal it would
    survive compilation in silence.
    """
    scopes = compile_scopes(
        [{"match": {"chat": "C-X"}, "delivery": {"mode": ["not_a_trigger"]}}],
        warn=warn,
    )

    assert scopes == ()
    assert any("without saying which channel" in line for line in warnings), warnings
    assert any("C-X" in line for line in warnings), warnings


def test_the_dropped_chat_never_reaches_a_connector(warn, warnings):
    """Why it is dropped rather than merely warned about.

    On Slack a scope naming a chat also exempts that conversation from
    ``allowed_channel_ids``, so an unvalidated one widens who can be answered.
    """
    scopes = compile_scopes(
        [{"match": {"chat": "C-X"}, "delivery": {"mode": ["mention"]}}],
        warn=warn,
    )

    assert scoped_chats(scopes, channel="slack") == ()


def test_naming_the_channel_alongside_the_chat_is_accepted(warn, warnings, registry):
    """The fix refuses one shape, not the axis."""
    registry(
        ChannelCapabilities(
            channel="demo",
            axes=frozenset({"channel", "chat"}),
            sections={"delivery": frozenset({"mode"})},
        )
    )
    scopes = compile_scopes(
        [{"match": {"channel": "demo", "chat": "C-OK"}, "delivery": {"mode": ["mention"]}}],
        warn=warn,
    )

    assert len(scopes) == 1
    assert scoped_chats(scopes, channel="demo") == ("C-OK",)


def test_a_scope_with_neither_axis_is_untouched(warn, warnings):
    """Deliberately unchanged.

    There is still no channel to ask, but it names no conversation, so it cannot
    exempt one. Only the chat-without-channel shape is refused.
    """
    scopes = compile_scopes([{"match": {}, "delivery": {"mode": ["mention"]}}], warn=warn)

    assert len(scopes) == 1
    assert not any("without saying which channel" in line for line in warnings), warnings


# --------------------------------------------------------------------------
# 2 -- the identity axis, which fails in both directions
# --------------------------------------------------------------------------


def test_a_user_axis_on_a_channel_with_no_sender_warns_that_it_will_never_match(
    warn, warnings
):
    # web is one conversation with the bot and names nobody in it, which is why
    # it declares neither the chat axis nor any identity_keys.
    compile_scopes(
        [{"match": {"channel": "web", "user": ["U1"]}, "delivery": {"mode": ["all"]}}],
        warn=warn,
    )

    assert any(
        "web does not identify a sender" in line and "never match" in line
        for line in warnings
    ), warnings


def test_a_not_on_a_channel_with_no_sender_warns_that_it_exempts_nobody(warn, warnings):
    compile_scopes(
        [
            {
                "match": {"channel": "web", "not": {"user": ["U1"]}},
                "delivery": {"mode": ["all"]},
            }
        ],
        warn=warn,
    )

    # The opposite failure from the positive case, and the one worth a distinct
    # line: an exemption that cannot be honoured leaves the restriction applying
    # to the person it was written to let through.
    assert any(
        "nobody is excluded" in line and "written to exempt" in line
        for line in warnings
    ), warnings


def test_a_channel_that_declares_the_user_axis_is_not_warned_about(
    registry, warn, warnings
):
    registry(
        ChannelCapabilities(
            channel="demo",
            axes=frozenset({"channel", "user"}),
            sections={"delivery": None},
            identity_keys=("user",),
        )
    )
    compile_scopes(
        [
            {
                "match": {"channel": "demo", "user": ["U1"], "not": {"user": ["U2"]}},
                "delivery": {"mode": ["all"]},
            }
        ],
        warn=warn,
    )

    assert warnings == []


def test_a_scope_naming_a_sender_still_applies_where_the_axis_is_unpopulated(
    warn, warnings
):
    """The warning does not drop the rule, and the two halves diverge here.

    Section 7.2: all of these are warnings, never failures. What differs is what
    each half then does -- the positive scope matches nobody, so it is inert;
    the negative one excludes nobody, so it applies to everyone. Both are the
    fail-closed reading, and the warnings above are what make them findable.
    """
    scopes = compile_scopes(
        [
            {"match": {"channel": "web", "user": ["U1"]}, "delivery": {"prompt": "named"}},
            {
                "match": {"channel": "web", "not": {"user": ["U1"]}},
                "delivery": {"prompt": "restricted"},
            },
        ],
        warn=warn,
    )

    assert len(scopes) == 2
    assert compose_section(scopes, channel="web") == {"prompt": "restricted"}


# --------------------------------------------------------------------------
# axes and identity_keys are two statements about one fact
# --------------------------------------------------------------------------


def test_declaring_the_user_axis_without_identity_keys_is_reported(registry, caplog):
    """Cross-checked, and reported to the connector author.

    On the module logger rather than through the injected ``warn``: no operator
    can cause this and none can fix it. It is a connector's declaration
    disagreeing with itself, and jiuwenswarm's loggers not propagating is why
    this test reaches for the logger by name rather than for ``caplog`` alone.
    """
    with caplog.at_level("WARNING", logger=caps.logger.name):
        caps.logger.propagate = True
        try:
            registry(
                ChannelCapabilities(
                    channel="demo",
                    axes=frozenset({"channel", "user"}),
                    sections={"delivery": None},
                )
            )
        finally:
            caps.logger.propagate = False

    assert any("names no identity_keys" in record.message for record in caplog.records)


def test_naming_identity_keys_without_the_axis_is_reported(registry, caplog):
    with caplog.at_level("WARNING", logger=caps.logger.name):
        caps.logger.propagate = True
        try:
            registry(
                ChannelCapabilities(
                    channel="demo",
                    axes=frozenset({"channel"}),
                    sections={"delivery": None},
                    identity_keys=("open_id", "union_id"),
                )
            )
        finally:
            caps.logger.propagate = False

    assert any("does not declare the user axis" in record.message for record in caplog.records)


def test_the_shipped_pseudo_channels_agree_with_themselves(registry, caplog):
    """No warning fires for anything this package registers on import.

    A cross-check that warned about the table it ships with would be a check
    nobody could act on, and would train the reader to ignore the line.
    """
    with caplog.at_level("WARNING", logger=caps.logger.name):
        caps.logger.propagate = True
        try:
            for name in ("web", "__cron__", "__heartbeat__"):
                declared = channel_capabilities(name)
                assert declared is not None
                registry(declared)
        finally:
            caps.logger.propagate = False

    assert caplog.records == []
