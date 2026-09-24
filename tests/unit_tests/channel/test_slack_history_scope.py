# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.

"""``agent.history`` -- one key, five words, and one axis it may be written on.

The Slack history toolkit reads exactly one conversation: the one the request
arrived in, named by trusted request metadata rather than by a model argument.
Reading any other conversation needs a policy to say how far, and this is where
that policy is declared.

    agent:
      history: members        # disabled | origin | members | visible | open

Each value is strictly wider than the one above it, and every reach past
``origin`` is still gated at read time on ``members(S) subset-of members(T)`` --
*nobody in S learns anything they could not already learn*. The key states the
policy; the toolkit enforces it, which is the same split ``permissions`` makes
between a declared level and the engine's decision.

The axis is the part worth pinning here. ``history`` may be written only in a
rule matching on ``channel``, so the feature is enabled per platform and the
membership rule is the whole of the protection on the source side. ``user`` and
``role`` are barred because the asker is *already* handled, and dynamically; a
second static treatment on the same axis can grant exactly what membership was
withholding. ``chat`` is barred because the membership rule already decides per
target, so a per-conversation list would be a weaker second statement of it.

None of that is a statement about what Slack can populate. Slack populates a
sender, ``agent.model_name`` is matched on one, and this key still may not be --
which is the distinction between ``axes`` and ``axis_restrictions``, and is the
reason both tests below are in this file rather than in the connector-agnostic
one.

``warn`` is injected rather than read off the logger: this project's loggers do
not propagate, so ``caplog`` sees nothing under the pytest CI runs on.
"""

from __future__ import annotations

from typing import Any

import pytest

from jiuwenswarm.common.scopes import (
    AXIS_CHANNEL,
    SECTION_AGENT,
    channel_capabilities,
    compile_scopes,
)
from jiuwenswarm.common.slack_history_policy import HISTORY_POLICY_VALUES


@pytest.fixture
def warnings() -> list[str]:
    return []


@pytest.fixture
def warn(warnings: list[str]):
    def record(message: str, *args: Any) -> None:
        warnings.append(message % args if args else message)

    return record


def _compile(entry: dict[str, Any], warn, **kw: Any):
    return compile_scopes([entry], warn=warn, **kw)


def _history(entry: dict[str, Any], warn, **kw: Any):
    scopes = _compile(entry, warn, **kw)
    return [scope.section(SECTION_AGENT).get("history") for scope in scopes]


# --------------------------------------------------------------------------
# The declaration
# --------------------------------------------------------------------------


def test_slack_declares_history_beside_the_model_it_carries():
    # Both are carried rather than consumed: the connector stamps them onto the
    # request and the runtime acts on them.
    assert channel_capabilities("slack").keys_for(SECTION_AGENT) == frozenset(
        {"model_name", "history"}
    )


def test_channels_slack_history_is_registered_as_the_layer_zero_twin():
    # The same question in the same five words, one layer down, and where a
    # deployment that wants the feature on everywhere says so without writing a
    # scope at all.
    assert channel_capabilities("slack").layer0_key(SECTION_AGENT, "history") == "history"


def test_history_is_declared_channel_only():
    restriction = channel_capabilities("slack").axis_restriction(SECTION_AGENT, "history")

    assert restriction is not None
    assert restriction.allowed == frozenset({AXIS_CHANNEL})


# --------------------------------------------------------------------------
# The five words, and nothing else
# --------------------------------------------------------------------------


@pytest.mark.parametrize("word", HISTORY_POLICY_VALUES)
def test_each_of_the_four_words_is_accepted(word, warn, warnings):
    assert _history({"match": {"channel": "slack"}, "agent": {"history": word}}, warn) == [
        word
    ]
    assert warnings == []


@pytest.mark.parametrize("written", ["Members", " open ", "DISABLED"])
def test_a_spelling_the_loader_cannot_normalise_is_refused(written, warn, warnings):
    """Where ``mid_turn`` settles case, and the difference is the mechanism.

    ``mid_turn`` is settled by a branch in the shared loader, which can replace
    the value with the word it recognised. This is a connector ``validator``,
    which may only say whether a value is usable -- so accepting ``Members``
    would store ``Members``, and the contract with the connector is that the
    resolved value is one of the four words. Refusing is the readable failure:
    the warning names all four and the next edit is correct.
    """
    scopes = _compile(
        {"match": {"channel": "slack"}, "agent": {"history": written}}, warn
    )

    assert scopes == ()
    assert [line for line in warnings if "written exactly and in lower case" in line]


@pytest.mark.parametrize("written", ["shared", "any", "", None, True, ["members"], 3])
def test_anything_that_is_not_one_of_the_four_is_refused(written, warn, warnings):
    """Fail closed: the key is dropped and the layer below decides.

    That floor is ``channels.slack.history``, whose default is ``disabled``, so a
    typo costs a reach somebody wrote rather than buying one nobody did. The
    other direction would hand a conversation the right to read another one on
    the strength of a misspelling.
    """
    scopes = _compile(
        {"match": {"channel": "slack"}, "agent": {"history": written}}, warn
    )

    assert scopes == ()
    # The refusal begins at the ``=``, so it joins the key with no space in
    # front of it: the key and the value it refuses read as one string.
    assert [line for line in warnings if line.startswith("scopes[0].agent.history=")]
    assert [
        line
        for line in warnings
        if "is not one of disabled, origin, members, visible, open" in line
    ]


def test_an_append_is_refused_because_there_is_nothing_to_append_to(warn, warnings):
    """Appending to one of five words produces a sixth that is not one of them."""
    scopes = _compile(
        {"match": {"channel": "slack"}, "agent": {"history_append": " and more"}}, warn
    )

    assert scopes == ()
    assert [
        line
        for line in warnings
        if "nothing to append to a word from a closed vocabulary" in line
    ]


# --------------------------------------------------------------------------
# One axis
# --------------------------------------------------------------------------


def test_a_platform_rule_is_the_shape_the_key_is_written_in(warn, warnings):
    assert _history(
        {"match": {"channel": "slack"}, "agent": {"history": "members"}}, warn
    ) == ["members"]
    assert not [line for line in warnings if "cannot appear in a rule" in line]


@pytest.mark.parametrize(
    ("match", "axis"),
    [
        ({"channel": "slack", "chat": "C1"}, "chat"),
        ({"channel": "slack", "user": ["U1"]}, "user"),
        ({"channel": "slack", "not": {"user": ["U1"]}}, "user"),
    ],
)
def test_history_is_refused_on_every_other_axis(match, axis, warn, warnings):
    scopes = _compile({"match": match, "agent": {"history": "members"}}, warn)

    assert scopes == ()
    (line,) = [entry for entry in warnings if "cannot appear in a rule" in entry]
    assert line.startswith(
        f"scopes[0].agent.history cannot appear in a rule matching on {axis}"
    )
    assert "It may be addressed on channel" in line


def test_the_refusal_says_why_and_where_to_write_it_instead(warn, warnings):
    scopes = _compile(
        {"match": {"channel": "slack", "user": ["U1"]}, "agent": {"history": "open"}},
        warn,
    )

    (line,) = [entry for entry in warnings if "cannot appear in a rule" in entry]
    assert scopes == ()
    # The reason: the asker is already handled, dynamically.
    assert "settled per request, by the membership rule" in line
    # And the two places it may be written instead.
    assert "{channel: slack} alone" in line
    assert "channels.slack.history" in line


def test_a_role_rule_is_refused_as_a_role_rather_than_as_the_ids_it_holds(warn, warnings):
    scopes = _compile(
        {"match": {"channel": "slack", "role": "admin"}, "agent": {"history": "members"}},
        warn,
        people={"boss": {"slack": "U_BOSS"}},
        roles={"admin": ["boss"]},
    )

    (line,) = [entry for entry in warnings if "cannot appear in a rule" in entry]
    assert scopes == ()
    assert "matching on role" in line
    assert "matching on user" not in line


def test_the_restriction_is_on_history_and_not_on_the_agent_section(warn, warnings):
    """``model_name`` under a rule naming a sender is a live, shipped feature.

    This is the property that makes the change safe to land: the restriction is
    per key, so declaring one on ``history`` takes nothing away from the key
    beside it.
    """
    import jiuwenswarm.gateway.channel_manager.im_platforms.slack.slack_connect as sc

    scopes = compile_scopes(
        [
            {
                "match": {"channel": "slack", "chat": "C1", "user": ["U1"]},
                "agent": {"model_name": next(iter(sc.configured_models().names), "m")},
            }
        ],
        warn=warn,
    )

    assert not [line for line in warnings if "cannot appear in a rule" in line]
    assert len(scopes) == 1


# --------------------------------------------------------------------------
# Two homes for one value
# --------------------------------------------------------------------------


def test_a_platform_scope_above_the_connector_key_warns_about_two_homes(warn, warnings):
    scopes = _compile(
        {"match": {"channel": "slack"}, "agent": {"history": "members"}},
        warn,
        channels_config={"slack": {"history": "origin"}},
    )

    assert len(scopes) == 1
    assert [
        line
        for line in warnings
        if "channels.slack.history both set this" in line
    ]


def test_nothing_written_settles_nothing(warn, warnings):
    """The default, and it is what shipped: no key, no warning, no rule."""
    assert compile_scopes([], warn=warn) == ()
    assert warnings == []
