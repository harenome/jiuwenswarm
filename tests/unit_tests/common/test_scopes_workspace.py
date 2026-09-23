# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.

"""The ``workspace`` axis: which installation of a platform a request came from.

No connector is involved anywhere here. The axis is the shared loader's, and it
is exercised against a fake platform registered through ``register_channel`` --
which is what says the generic half stands on its own and is not Slack's
behaviour read back. On Slack the value is a ``team_id``; nothing below knows
that, and nothing below should.

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
    AXIS_WORKSPACE,
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
    """A platform that says which installation a request arrived from."""
    snapshot = caps.snapshot_registry()
    register_channel(
        ChannelCapabilities(
            channel="demo",
            axes=frozenset({"channel", "workspace", "chat", "chat_type", "user"}),
            sections={"delivery": None, "agent": None},
            identity_keys=("user",),
            axis_values={AXIS_CHAT_TYPE: frozenset({"room", "direct"})},
        )
    )
    yield
    caps.restore_registry(snapshot)


@pytest.fixture
def single_workspace_channel():
    """A platform that is in one installation, or cannot say which."""
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


def test_a_workspace_selects_every_conversation_in_it(demo_channel, warn, warnings):
    scopes = _compile(
        [
            {
                "match": {"channel": "demo", "workspace": "T_ACME"},
                "delivery": {"mode": ["all"]},
            }
        ],
        warn,
    )

    assert warnings == []
    assert scopes[0].match.workspace == "T_ACME"
    assert scopes[0].selects(channel="demo", chat="C1", workspace="T_ACME")
    assert scopes[0].selects(channel="demo", chat="D9", workspace="T_ACME")
    assert not scopes[0].selects(channel="demo", chat="C1", workspace="T_OTHER")


def test_a_caller_with_no_workspace_gets_the_unnamed_answer(demo_channel, warn):
    scopes = _compile(
        [
            {
                "match": {"channel": "demo", "workspace": "T_ACME"},
                "delivery": {"mode": ["all"]},
            }
        ],
        warn,
    )

    # The same fail-closed direction an unidentified sender and an unnamed kind
    # of conversation take. Reading an absent workspace as every workspace would
    # apply a rule written for one installation to all of them, which is the
    # widening nothing in this module may perform.
    assert not scopes[0].selects(channel="demo", chat="C1")
    assert not scopes[0].selects(channel="demo", chat="C1", workspace="")


def test_the_workspace_is_matched_exactly(demo_channel, warn):
    scopes = _compile(
        [
            {
                "match": {"channel": "demo", "workspace": "T_ACME"},
                "delivery": {"mode": ["all"]},
            }
        ],
        warn,
    )

    assert not scopes[0].selects(channel="demo", chat="C1", workspace="t_acme")
    assert not scopes[0].selects(channel="demo", chat="C1", workspace="T_ACME ")


def test_a_rule_naming_no_workspace_applies_to_every_workspace(
    demo_channel, warn, warnings
):
    """The absent axis, which needs no rule of its own and gets none.

    An absent axis matches anything everywhere in this module, and that reading
    is what a deployment in one workspace relies on: every rule written before
    the axis existed keeps selecting exactly what it selected. Writing an
    exception here -- refusing a rule that names no workspace, or reading it as
    the workspace the connector happens to be in -- would change the meaning of
    every such rule the day a second installation was added.
    """
    scopes = _compile(
        [{"match": {"channel": "demo"}, "delivery": {"prompt": "anywhere"}}], warn
    )

    assert warnings == []
    assert scopes[0].match.workspace is None
    assert scopes[0].selects(channel="demo", chat="C1", workspace="T_ACME")
    assert scopes[0].selects(channel="demo", chat="C1", workspace="T_OTHER")
    # Including the request that carries no workspace at all: the axis is not
    # written, so there is nothing for it to fail closed on.
    assert scopes[0].selects(channel="demo", chat="C1")


def test_a_workspace_and_a_conversation_in_one_rule_both_apply(
    demo_channel, warn, warnings
):
    """Not the redundant pair ``chat`` and ``chat_type`` are, and not refused.

    A named conversation has one *type*, which is what makes that pair
    redundant. It does not have one workspace: a conversation shared between two
    of them carries the one the request came from, so this rule is the shared
    channel restricted to one side of it -- narrower than either half, and not
    expressible any other way.
    """
    scopes = _compile(
        [
            {
                "match": {
                    "channel": "demo",
                    "workspace": "T_ACME",
                    "chat": "C_SHARED",
                },
                "delivery": {"prompt": "our half of the shared channel"},
            }
        ],
        warn,
    )

    assert warnings == []
    assert scopes[0].selects(channel="demo", chat="C_SHARED", workspace="T_ACME")
    assert not scopes[0].selects(channel="demo", chat="C_SHARED", workspace="T_GUEST")
    assert not scopes[0].selects(channel="demo", chat="C_OTHER", workspace="T_ACME")


def test_a_role_in_the_same_rule_does_not_drop_the_workspace(
    demo_channel, warn, warnings
):
    """Role resolution rebuilds the match, and it has to rebuild all of it.

    The failure this pins was shipped once already, on ``chat_type``: an axis
    left out of the rebuild stops constraining the rule, so it reaches
    conversations it was not written for and sits on the wrong layer while doing
    so. Asserted on both, because they are the two ways it shows.
    """
    scopes = _compile(
        [
            {
                "match": {
                    "channel": "demo",
                    "workspace": "T_ACME",
                    "role": "admin",
                },
                "delivery": {"prompt": "admins, in the ACME workspace"},
            }
        ],
        warn,
        people={"boss": {"demo": "U_BOSS"}},
        roles={"admin": ["boss"]},
    )

    assert warnings == []
    assert scopes[0].match.workspace == "T_ACME"
    assert scopes[0].selects(channel="demo", chat="C1", workspace="T_ACME", user="U_BOSS")
    # The half the drop would give away: an admin in the other workspace is not
    # an admin in this one, and the rule names this one.
    assert not scopes[0].selects(
        channel="demo", chat="C1", workspace="T_OTHER", user="U_BOSS"
    )
    assert scopes[0].layer == (1, 1, 0, 1)


def test_the_axis_is_described(demo_channel, warn):
    scopes = _compile(
        [
            {
                "match": {"channel": "demo", "workspace": "T_ACME", "chat": "C1"},
                "delivery": {"mode": ["all"]},
            }
        ],
        warn,
    )

    # Coarsest first, which is the order the axes are declared and compared in.
    assert (
        scopes[0].match.describe() == "{channel: demo, workspace: T_ACME, chat: C1}"
    )


# --------------------------------------------------------------------------
# Layers
# --------------------------------------------------------------------------


def test_a_workspace_rule_outranks_a_rule_naming_the_platform_alone(
    demo_channel, warn, warnings
):
    scopes = _compile(
        [
            {
                "match": {"channel": "demo", "workspace": "T_ACME"},
                "delivery": {"prompt": "acme"},
            },
            {"match": {"channel": "demo"}, "delivery": {"prompt": "anywhere"}},
        ],
        warn,
    )

    # Written second and still folded first: the platform-wide rule is the layer
    # below whichever order the two appear in.
    ordered = matching_scopes(scopes, channel="demo", chat="C1", workspace="T_ACME")
    assert warnings == []
    assert [scope.section("delivery")["prompt"] for scope in ordered] == [
        "anywhere",
        "acme",
    ]


def test_naming_the_conversation_as_well_outranks_the_workspace_alone(
    demo_channel, warn, warnings
):
    scopes = _compile(
        [
            {
                "match": {"channel": "demo", "workspace": "T_ACME", "chat": "C1"},
                "delivery": {"prompt": "this room, our side"},
            },
            {
                "match": {"channel": "demo", "workspace": "T_ACME"},
                "delivery": {"prompt": "our workspace"},
            },
        ],
        warn,
    )

    ordered = matching_scopes(scopes, channel="demo", chat="C1", workspace="T_ACME")
    assert warnings == []
    assert [scope.section("delivery")["prompt"] for scope in ordered] == [
        "our workspace",
        "this room, our side",
    ]


def test_the_workspace_outranks_a_rule_naming_the_conversation_alone(
    demo_channel, warn, warnings
):
    """The decided ordering, pinned because it is a decision rather than a
    derivation.

    The two match sets do not nest -- a conversation can carry requests from
    more than one workspace, and a workspace holds more than one conversation --
    so neither rule contains the other and the layering had to choose. It
    chooses coarsest-axis-first, the order the axes are declared, described and
    compared in. An operator who wants the room to win writes the workspace on
    the room's rule, which is the rule above and which nests inside both.
    """
    scopes = _compile(
        [
            {
                "match": {"channel": "demo", "workspace": "T_ACME"},
                "delivery": {"prompt": "our workspace"},
            },
            {
                "match": {"channel": "demo", "chat": "C1"},
                "delivery": {"prompt": "this room, whoever it reaches"},
            },
        ],
        warn,
    )

    ordered = matching_scopes(scopes, channel="demo", chat="C1", workspace="T_ACME")
    assert warnings == []
    assert [scope.layer for scope in ordered] == [(1, 0, 2, 0), (1, 1, 0, 0)]
    assert ordered[-1].section("delivery")["prompt"] == "our workspace"


# --------------------------------------------------------------------------
# Shapes the loader refuses
# --------------------------------------------------------------------------


def test_a_list_of_workspaces_is_refused_as_an_id_would_be(
    demo_channel, warn, warnings
):
    """One string, ``chat``'s answer rather than ``chat_type``'s.

    An axis naming a *class* can name several at once, because the entries are
    one instruction about a set of kinds. An axis naming a *thing* cannot: two
    workspace ids are two organisations, each of which can be given a body of
    its own, so a list there is two rules collapsed rather than one rule stated.
    It goes through the id-shaped path for exactly that reason, and is refused
    by the sentence that path already had.
    """
    scopes = _compile(
        [
            {
                "match": {"channel": "demo", "workspace": ["T_ACME", "T_OTHER"]},
                "delivery": {"mode": ["all"]},
            }
        ],
        warn,
    )

    assert scopes == ()
    assert any(
        "match.workspace" in line and "is not an id" in line for line in warnings
    )


def test_an_empty_workspace_drops_the_scope(demo_channel, warn, warnings):
    scopes = _compile(
        [
            {
                "match": {"channel": "demo", "workspace": "  "},
                "delivery": {"mode": ["all"]},
            }
        ],
        warn,
    )

    # An empty id would match a request the connector gave no workspace for,
    # which is the case ``selects`` fails closed on.
    assert scopes == ()
    assert any("match.workspace is empty" in line for line in warnings)


def test_a_workspace_with_no_value_drops_the_scope(demo_channel, warn, warnings):
    scopes = _compile(
        [
            {
                "match": {"channel": "demo", "workspace": None},
                "delivery": {"mode": ["all"]},
            }
        ],
        warn,
    )

    assert scopes == ()
    assert any("match.workspace has no value" in line for line in warnings)


def test_a_workspace_still_needs_the_platform_that_issued_it(warn, warnings):
    scopes = _compile(
        [{"match": {"workspace": "T_ACME"}, "delivery": {"mode": ["all"]}}], warn
    )

    # A workspace id is a platform's own, so with no channel there is nothing to
    # validate it against and the rule would select on every connector that
    # declares scopes -- including the ones with no such concept.
    assert scopes == ()
    assert any("workspace=T_ACME" in line for line in warnings)
    assert any("Add channel: alongside workspace:" in line for line in warnings)


# --------------------------------------------------------------------------
# The capability
# --------------------------------------------------------------------------


def test_a_platform_that_cannot_say_which_workspace_is_told_so(
    single_workspace_channel, warn, warnings
):
    """Warned rather than dropped, which is what every capability mismatch gets.

    The rule is perfectly readable and it is the channel that cannot fill the
    axis. A value no request carries matches nothing, which already leaves those
    conversations on the layer below -- the same place a dropped scope would
    leave them.
    """
    scopes = _compile(
        [
            {
                "match": {"channel": "flat", "workspace": "T_ACME"},
                "delivery": {"mode": ["all"]},
            }
        ],
        warn,
    )

    assert any(
        "does not say which workspace a request came from" in line
        for line in warnings
    )
    assert scopes and not scopes[0].selects(channel="flat", chat="C1")


def test_a_workspace_rule_is_not_called_redundant_with_the_connector_block(
    warn, warnings
):
    """It does not reach every conversation, so layer 0 has not gone dead.

    The warning it is being kept away from fires for a rule that sits directly
    on layer 0 for *everything*, where setting the same key in both leaves the
    connector value governing nothing. A workspace rule leaves every
    conversation in every other workspace on layer 0, exactly as a rule naming a
    kind of conversation leaves every conversation of another kind there.
    """
    snapshot = caps.snapshot_registry()
    register_channel(
        ChannelCapabilities(
            channel="layered",
            axes=frozenset({"channel", "workspace", "chat"}),
            sections={"agent": frozenset({"model_name"})},
            layer0_keys={"agent": {"model_name": "model_name"}},
        )
    )
    try:
        scopes = _compile(
            [
                {
                    "match": {"channel": "layered", "workspace": "T_ACME"},
                    "agent": {"model_name": "a-model"},
                }
            ],
            warn,
            channels_config={"layered": {"model_name": "the-connector-default"}},
        )
    finally:
        caps.restore_registry(snapshot)

    assert warnings == []
    assert scopes[0].section("agent") == {"model_name": "a-model"}


def test_the_workspace_reaches_the_fold_a_caller_actually_performs(
    demo_channel, warn, warnings
):
    """``compose_section`` is the entry point every reader settles a key through.

    Without the argument there the axis would compile, layer and describe
    correctly and be unreachable: every caller composes, and a caller that
    cannot say which workspace a request came from gets the answer for a request
    in none -- which silently drops the rules that name one.
    """
    scopes = _compile(
        [
            {
                "match": {"channel": "demo", "workspace": "T_ACME"},
                "delivery": {"prompt": "acme"},
            },
            {"match": {"channel": "demo"}, "delivery": {"prompt": "anywhere"}},
        ],
        warn,
    )

    assert warnings == []
    assert compose_section(
        scopes, channel="demo", chat="C1", workspace="T_ACME"
    ) == {"prompt": "acme"}
    assert compose_section(
        scopes, channel="demo", chat="C1", workspace="T_OTHER"
    ) == {"prompt": "anywhere"}
    assert compose_section(scopes, channel="demo", chat="C1") == {
        "prompt": "anywhere"
    }


def test_the_axis_is_one_of_the_supported_ones(demo_channel, warn, warnings):
    """Named as an axis rather than reported as a typo, and spelled once.

    The constant is what every other reader of the schema matches on, so a rule
    that compiles must have gone through it.
    """
    from jiuwenswarm.common.scopes import SUPPORTED_AXES, SUPPORTED_MATCH_KEYS

    assert AXIS_WORKSPACE == "workspace"
    assert AXIS_WORKSPACE in SUPPORTED_AXES
    assert AXIS_WORKSPACE in SUPPORTED_MATCH_KEYS
    assert ScopeMatch(channel="demo", workspace="T1").describe() == (
        "{channel: demo, workspace: T1}"
    )
