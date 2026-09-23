# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.

"""The ``permissions`` section: what it narrows, and what it cannot.

Warnings are collected through the injected ``warn`` rather than through
``caplog``: this project's loggers do not propagate, so ``caplog`` sees nothing
under the pytest CI runs on, and a test written against it would pass locally
and assert nothing where it matters.

Several tests below call agent-core's ``evaluate_tiered_policy`` directly rather
than asserting on the config this module produces. That is deliberate. The claim
being made is about *the engine's* evaluation order -- which short-circuits run
ahead of a tool's own level -- and a test that only inspected our own output
would keep passing on the day that order changed, which is the day it matters.
"""

from __future__ import annotations

from typing import Any

import pytest

from jiuwenswarm.common.scopes import (
    ChannelCapabilities,
    compile_scopes,
    denied_tool_level,
    narrow_config_for_scopes,
    register_channel,
    scoped_chats,
    settled_tool_levels,
)
from jiuwenswarm.common.scopes import capabilities as caps
from jiuwenswarm.common.scopes import permissions as scope_permissions


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
    """An attended channel that reads every section."""
    snapshot = caps.snapshot_registry()
    register_channel(
        ChannelCapabilities(
            channel="demo",
            axes=frozenset({"channel", "chat"}),
            sections={"delivery": None, "agent": None, "permissions": None},
            attended=True,
        )
    )
    register_channel(
        ChannelCapabilities(
            channel="unwatched",
            axes=frozenset({"channel"}),
            sections={"permissions": None},
            attended=False,
        )
    )
    yield
    caps.restore_registry(snapshot)


@pytest.fixture(autouse=True)
def _forget_reported_conflicts():
    """The rule-conflict warning is deduplicated for the life of the process."""
    scope_permissions._reported_rule_conflicts.clear()
    yield
    scope_permissions._reported_rule_conflicts.clear()


def _compile(entries: Any, warn, **kwargs: Any):
    return compile_scopes(entries, warn=warn, **kwargs)


def _scope(level: str, *, chat: str | None = None, tool: str = "bash") -> dict[str, Any]:
    match: dict[str, str] = {"channel": "demo"}
    if chat is not None:
        match["chat"] = chat
    return {"match": match, "permissions": {"tools": {tool: level}}}


# --------------------------------------------------------------------------
# It narrows
# --------------------------------------------------------------------------


def test_a_permissions_scope_is_kept_rather_than_reported_as_deferred(
    demo_channel, warn, warnings
):
    scopes = _compile([_scope("ask")], warn)

    assert len(scopes) == 1
    assert scopes[0].section("permissions") == {"tools": {"bash": "ask"}}
    assert warnings == []


def test_a_scope_narrows_an_allowed_tool_to_ask(demo_channel, warn):
    scopes = _compile([_scope("ask")], warn)
    base = {"enabled": True, "tools": {"bash": "allow", "write_file": "allow"}}

    narrowed = narrow_config_for_scopes(base, scopes, channel="demo", chat="C1")

    assert narrowed["tools"]["bash"] == "ask"
    # Untouched tools keep what they had; the narrowing is per key, not a
    # replacement of the map.
    assert narrowed["tools"]["write_file"] == "allow"
    # The base config is not edited in place: two conversations share it.
    assert base["tools"]["bash"] == "allow"


def test_a_tool_the_base_never_named_is_narrowed_through_the_defaults(
    demo_channel, warn
):
    scopes = _compile([_scope("deny")], warn)
    base = {"tools": {}, "defaults": {"*": "allow"}}

    narrowed = narrow_config_for_scopes(base, scopes, channel="demo", chat="C1")

    assert narrowed["tools"]["bash"] == "deny"


def test_no_matching_scope_leaves_the_config_exactly_as_it_was(demo_channel, warn):
    scopes = _compile([_scope("deny", chat="C1")], warn)
    base = {"tools": {"bash": "allow"}}

    assert narrow_config_for_scopes(base, scopes, channel="demo", chat="C2") is base
    assert narrow_config_for_scopes(base, scopes, channel="other") is base


# --------------------------------------------------------------------------
# It can only tighten
# --------------------------------------------------------------------------


def test_a_scope_cannot_widen_a_denied_tool(demo_channel, warn, warnings):
    scopes = _compile([_scope("allow")], warn)
    base = {"tools": {"bash": "deny"}}

    narrowed = narrow_config_for_scopes(base, scopes, channel="demo", chat="C1")

    assert narrowed["tools"]["bash"] == "deny"
    assert any("grants nothing" in line for line in warnings)


def test_a_scope_cannot_widen_a_tool_that_asks(demo_channel, warn):
    scopes = _compile([_scope("allow")], warn)

    assert narrow_config_for_scopes(
        {"tools": {"bash": "ask"}}, scopes, channel="demo", chat="C1"
    )["tools"]["bash"] == "ask"


def test_ask_cannot_widen_a_deny(demo_channel, warn):
    scopes = _compile([_scope("ask")], warn)

    assert narrow_config_for_scopes(
        {"tools": {"bash": "deny"}}, scopes, channel="demo", chat="C1"
    )["tools"]["bash"] == "deny"


# --------------------------------------------------------------------------
# Order-independence
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "first,second",
    [("ask", "deny"), ("deny", "ask"), ("allow", "deny"), ("deny", "allow")],
)
def test_two_scopes_settle_the_same_level_in_either_order(
    demo_channel, warn, first, second
):
    forwards = _compile([_scope(first), _scope(second, chat="C1")], warn)
    backwards = _compile([_scope(second, chat="C1"), _scope(first)], warn)

    settled_forwards = settled_tool_levels(forwards, channel="demo", chat="C1")
    settled_backwards = settled_tool_levels(backwards, channel="demo", chat="C1")

    assert settled_forwards == settled_backwards == {"bash": "deny"}


def test_order_independence_holds_between_two_scopes_of_the_same_layer(
    demo_channel, warn
):
    # Same specificity, so the tie-break is file position -- which is exactly
    # the ordering a security property must not depend on.
    forwards = _compile([_scope("ask"), _scope("deny")], warn)
    backwards = _compile([_scope("deny"), _scope("ask")], warn)

    assert (
        settled_tool_levels(forwards, channel="demo")
        == settled_tool_levels(backwards, channel="demo")
        == {"bash": "deny"}
    )


def test_scopes_naming_different_tools_both_apply(demo_channel, warn):
    scopes = _compile(
        [_scope("ask"), _scope("deny", chat="C1", tool="write_file")], warn
    )

    assert settled_tool_levels(scopes, channel="demo", chat="C1") == {
        "bash": "ask",
        "write_file": "deny",
    }


# --------------------------------------------------------------------------
# An unattended channel cannot ask
# --------------------------------------------------------------------------


def test_ask_becomes_deny_where_nobody_can_answer(demo_channel, warn, warnings):
    scopes = _compile(
        [{"match": {"channel": "unwatched"}, "permissions": {"tools": {"bash": "ask"}}}],
        warn,
    )

    assert settled_tool_levels(scopes, channel="unwatched") == {"bash": "deny"}
    assert any("nobody can answer" in line for line in warnings)


def test_ask_survives_on_an_attended_channel(demo_channel, warn, warnings):
    scopes = _compile([_scope("ask")], warn)

    assert settled_tool_levels(scopes, channel="demo") == {"bash": "ask"}
    assert warnings == []


def test_an_undeclared_channel_is_not_assumed_unattended(warn):
    # A scope with no channel at all matches everywhere, including surfaces that
    # never declared. Degrading there would turn "check with me" into "never" on
    # a surface that renders approvals perfectly well, which is a failure the
    # operator cannot see. The declaration is the opt-in, both ways.
    scopes = _compile([{"match": {}, "permissions": {"tools": {"bash": "ask"}}}], warn)

    assert settled_tool_levels(scopes, channel="tui") == {"bash": "ask"}


def test_cron_is_declared_unattended_and_reads_permissions():
    cron = caps.channel_capabilities("__cron__")

    assert cron is not None
    assert cron.attended is False
    assert cron.reads("permissions")
    # Still no delivery: a scheduled run has no trigger to configure.
    assert not cron.reads("delivery")


# --------------------------------------------------------------------------
# The approval_overrides interaction -- the caveat, pinned against the engine
# --------------------------------------------------------------------------


def _evaluate(config: dict[str, Any], command: str = "git status") -> str:
    from openjiuwen.harness.security.permission_engine.toolguard.tool_policy import (
        evaluate_tiered_policy,
    )

    level, _rule = evaluate_tiered_policy(config, "bash", {"command": command})
    return level.value


def _base_with_override() -> dict[str, Any]:
    return {
        "enabled": True,
        "tools": {"bash": "allow"},
        "defaults": {"*": "ask"},
        "approval_overrides": [
            {
                "id": "always-git",
                "tools": ["bash"],
                "match_type": "command",
                "pattern": "git status",
                "action": "allow",
            }
        ],
    }


def test_the_engine_lets_an_approval_override_beat_a_tools_ask():
    """The caveat itself, stated as a fact about agent-core rather than about us.

    ``narrow_permissions`` is monotone over ``tools`` only. This is what that
    costs: a config narrowed to ``ask`` and nothing else still allows the call.
    If this test starts failing because the engine changed its order, the
    pruning below has become unnecessary rather than wrong.
    """
    from openjiuwen.agent_teams.security.narrowing import narrow_permissions

    hand_narrowed = narrow_permissions(_base_with_override(), {"bash": "ask"})

    assert hand_narrowed["tools"]["bash"] == "ask"
    assert _evaluate(hand_narrowed) == "allow"


def test_a_scope_narrowing_to_ask_actually_asks(demo_channel, warn):
    scopes = _compile([_scope("ask")], warn)

    narrowed = narrow_config_for_scopes(
        _base_with_override(), scopes, channel="demo", chat="C1"
    )

    assert narrowed["tools"]["bash"] == "ask"
    assert narrowed["approval_overrides"] == []
    assert _evaluate(narrowed) == "ask"


def test_a_scope_narrowing_to_deny_beats_an_override_even_unpruned():
    """Why ``deny`` is the level to reach for when it has to be certain.

    A ``tools`` baseline of ``deny`` short-circuits ahead of the overrides, so
    this one holds with no help from us at all.
    """
    from openjiuwen.agent_teams.security.narrowing import narrow_permissions

    hand_narrowed = narrow_permissions(_base_with_override(), {"bash": "deny"})

    assert _evaluate(hand_narrowed) == "deny"


def test_an_override_naming_another_tool_survives(demo_channel, warn):
    base = _base_with_override()
    base["approval_overrides"][0]["tools"] = ["bash", "mcp_exec_command"]
    scopes = _compile([_scope("ask")], warn)

    narrowed = narrow_config_for_scopes(base, scopes, channel="demo", chat="C1")

    # Pruned down to the tools no scope narrowed, not deleted: the operator's
    # decision about the other tool was never the thing being tightened.
    assert narrowed["approval_overrides"][0]["tools"] == ["mcp_exec_command"]


def test_pruning_does_not_touch_a_config_no_scope_narrows(demo_channel, warn):
    scopes = _compile([_scope("ask", chat="C-other")], warn)
    base = _base_with_override()

    narrowed = narrow_config_for_scopes(base, scopes, channel="demo", chat="C1")

    assert narrowed["approval_overrides"][0]["id"] == "always-git"


def test_a_rules_entry_that_allows_still_out_ranks_an_ask(demo_channel, warn):
    """The half of the caveat that is left open, pinned so it stays visible.

    A ``rules`` entry is consulted before a tool's own level in the same way an
    override is, so ``action: allow`` there defeats a narrowing to ``ask``. It is
    *not* pruned, and that is the conservative choice rather than an oversight:
    a rule can also resolve to ``ask``, so dropping one could leave the call on a
    baseline the rule was tightening -- a widening performed in the name of
    narrowing. It is logged instead, and ``deny`` is unaffected.
    """
    base = {
        "enabled": True,
        "tools": {"bash": "allow"},
        "defaults": {"*": "ask"},
        "rules": [
            {
                "id": "git-is-fine",
                "tools": ["bash"],
                "action": "allow",
                "pattern": "git status",
            }
        ],
    }
    scopes = _compile([_scope("ask")], warn)

    narrowed = narrow_config_for_scopes(base, scopes, channel="demo", chat="C1")
    assert narrowed["tools"]["bash"] == "ask"
    assert _evaluate(narrowed) == "allow"

    # Deny is the level that holds, and this is what the log tells the operator
    # to reach for.
    denied = narrow_config_for_scopes(
        base, _compile([_scope("deny")], warn), channel="demo", chat="C1"
    )
    assert _evaluate(denied) == "deny"


# --------------------------------------------------------------------------
# The floor the permission hook enforces
# --------------------------------------------------------------------------


def test_denied_tool_level_reports_only_a_deny(demo_channel, warn):
    denies = _compile([_scope("deny")], warn)
    asks = _compile([_scope("ask")], warn)

    assert denied_tool_level(denies, "bash", channel="demo", chat="C1") == "deny"
    # ask is left to the narrowed config, where the engine can still raise the
    # interrupt an ask means. The hook's vocabulary has no third word.
    assert denied_tool_level(asks, "bash", channel="demo", chat="C1") is None
    assert denied_tool_level(denies, "write_file", channel="demo", chat="C1") is None


def test_the_floor_and_the_narrowing_read_the_same_levels(demo_channel, warn):
    scopes = _compile([_scope("ask"), _scope("deny", chat="C1")], warn)

    settled = settled_tool_levels(scopes, channel="demo", chat="C1")
    floor = denied_tool_level(scopes, "bash", channel="demo", chat="C1")

    assert settled["bash"] == "deny"
    assert floor == "deny"


def test_an_unattended_ask_reaches_the_floor_as_a_deny(demo_channel, warn):
    scopes = _compile(
        [{"match": {"channel": "unwatched"}, "permissions": {"tools": {"bash": "ask"}}}],
        warn,
    )

    assert denied_tool_level(scopes, "bash", channel="unwatched") == "deny"


# --------------------------------------------------------------------------
# A restriction must not open anything
# --------------------------------------------------------------------------


def test_a_permissions_only_scope_does_not_opt_its_conversation_in(
    demo_channel, warn
):
    scopes = _compile(
        [
            _scope("deny", chat="C-LOCKED"),
            {"match": {"channel": "demo", "chat": "C-ANSWERED"}, "delivery": {"mode": ["all"]}},
        ],
        warn,
    )

    # On Slack this list is what exempts a conversation from
    # allowed_channel_ids. A rule written to take bash away must not also hand
    # out an answer in a channel the operator never listed.
    assert scoped_chats(scopes, channel="demo") == ("C-ANSWERED",)
    # Asked for explicitly, it is still there.
    assert scoped_chats(scopes, channel="demo", sections=("permissions",)) == (
        "C-LOCKED",
    )


# --------------------------------------------------------------------------
# What the loader refuses
# --------------------------------------------------------------------------


def test_an_unknown_level_drops_that_entry_and_keeps_the_rest(
    demo_channel, warn, warnings
):
    scopes = _compile(
        [
            {
                "match": {"channel": "demo"},
                "permissions": {"tools": {"bash": "maybe", "write_file": "deny"}},
            }
        ],
        warn,
    )

    # Per entry, unlike everywhere else in this loader: the entries that survive
    # are restrictions, so keeping them is the conservative reading. Dropping
    # the whole key over one typo would hand back a permission the operator
    # believed they had taken away.
    assert scopes[0].section("permissions") == {"tools": {"write_file": "deny"}}
    assert any("is not one of allow/ask/deny" in line for line in warnings)


def test_a_tools_value_that_is_not_a_mapping_is_refused(demo_channel, warn, warnings):
    scopes = _compile(
        [{"match": {"channel": "demo"}, "permissions": {"tools": ["bash"]}}], warn
    )

    assert scopes == ()
    assert any("is not a mapping of tool to allow/ask/deny" in line for line in warnings)


def test_a_key_that_is_not_tools_is_refused(demo_channel, warn, warnings):
    scopes = _compile(
        [
            {
                "match": {"channel": "demo"},
                "permissions": {"rules": [], "tools": {"bash": "deny"}},
            }
        ],
        warn,
    )

    # rules, defaults and approval_overrides have no narrowing operation that
    # cannot also widen, so the section names exactly one key.
    assert scopes[0].section("permissions") == {"tools": {"bash": "deny"}}
    assert any("is not a permissions setting" in line for line in warnings)


def test_a_channel_that_does_not_read_permissions_says_so(warn, warnings):
    snapshot = caps.snapshot_registry()
    register_channel(
        ChannelCapabilities(
            channel="delivery-only",
            axes=frozenset({"channel"}),
            sections={"delivery": None},
        )
    )
    try:
        scopes = _compile(
            [
                {
                    "match": {"channel": "delivery-only"},
                    "permissions": {"tools": {"bash": "deny"}},
                }
            ],
            warn,
        )
    finally:
        caps.restore_registry(snapshot)

    assert scopes == ()
    assert any(
        "does not read the permissions section" in line for line in warnings
    )


def test_permissions_is_no_longer_deferred():
    from jiuwenswarm.common.scopes import (
        CONNECTOR_SECTIONS,
        DEFERRED_SECTIONS,
        SUPPORTED_SECTIONS,
    )

    assert DEFERRED_SECTIONS == ()
    assert SUPPORTED_SECTIONS == ("delivery", "agent", "permissions", "clicks")
    # Pinned as an equality so that a section added later has to be classified
    # here rather than joining the connector's list by accident -- which is what
    # decides whether it can opt a conversation in. clicks is the second section
    # kept out of it, for the same reason permissions is: it takes something
    # away, so letting it name a conversation to settle would let a restriction
    # open one.
    assert CONNECTOR_SECTIONS == ("delivery", "agent")


# --------------------------------------------------------------------------
# When the merge cannot run at all
# --------------------------------------------------------------------------
#
# The one operation this module does not implement is imported from agent-core
# at the point of use, and that import has raised in service before now: the
# compatibility shim it goes through drops a name the module it re-exports asks
# for. Both public functions run inside the permission hook on every tool check,
# and nothing wraps that hook, so an exception here ended the turn rather than
# the rule.


@pytest.fixture
def _forget_narrowing_failure():
    """The unavailability warning is written once for the life of the process."""
    scope_permissions._reported_narrowing_failure = False
    yield
    scope_permissions._reported_narrowing_failure = False


@pytest.fixture
def unimportable(monkeypatch, _forget_narrowing_failure):
    """agent-core's narrowing, as unavailable as it can be."""

    def _raise() -> Any:
        raise ImportError("no module named openjiuwen.agent_teams.security.narrowing")

    monkeypatch.setattr(scope_permissions, "_narrow_permissions", _raise)


def test_a_merge_that_cannot_run_settles_nothing_instead_of_raising(
    demo_channel, warn, unimportable
):
    """The turn survives. The scope does not, and the caller is told which."""
    scopes = _compile([_scope("deny")], warn)

    assert settled_tool_levels(scopes, channel="demo") == {}


def test_a_merge_that_cannot_run_leaves_the_permission_block_as_written(
    demo_channel, warn, unimportable
):
    """Not an allow, which is the distinction the whole guard turns on.

    The operator's own permissions block is returned untouched, so every level,
    rule and override in it still applies and the engine still asks and still
    refuses. Answering "allow" here would turn an unimportable module into a
    granted tool call.
    """
    scopes = _compile([_scope("deny")], warn)
    base = {"enabled": True, "tools": {"bash": "ask", "write_file": "deny"}}

    settled = narrow_config_for_scopes(base, scopes, channel="demo")

    assert settled == base
    assert settled["tools"]["bash"] == "ask"
    assert settled["tools"]["write_file"] == "deny"


def test_a_merge_that_cannot_run_refuses_nothing_at_the_hook(
    demo_channel, warn, unimportable
):
    """``deny`` is the hook's whole vocabulary, and it has nothing to say now.

    Which is the floor being lowered, not raised: the level the scope would have
    settled is gone, and the engine below decides on the config as written.
    """
    scopes = _compile([_scope("deny")], warn)

    assert denied_tool_level(scopes, "bash", channel="demo") is None


def test_the_unavailability_is_reported_once_and_not_once_per_check(
    demo_channel, warn, unimportable
):
    """It is called on every tool check; the failure that repeats is one failure."""
    scopes = _compile([_scope("deny")], warn)

    assert scope_permissions._reported_narrowing_failure is False
    for _ in range(5):
        settled_tool_levels(scopes, channel="demo")
    assert scope_permissions._reported_narrowing_failure is True


def test_nothing_is_reported_when_no_scope_asks_for_a_merge(
    demo_channel, warn, unimportable
):
    """An unimportable module nobody needed is not an operator's problem.

    A deployment with no permissions scope never reaches the merge, so it must
    not be warned about one -- the failure is only real for a config that was
    relying on it.
    """
    scopes = _compile(
        [{"match": {"channel": "demo"}, "delivery": {"mode": ["mention"]}}], warn
    )

    assert settled_tool_levels(scopes, channel="demo") == {}
    assert scope_permissions._reported_narrowing_failure is False
