# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.

"""Where the ``scopes`` permissions section meets the permission rail.

Two sites, and the tests are about the division of labour between them: the
scene hook refuses a ``deny`` before anything can render an approval card, and
the permissions snapshot puts everything a level can say into the engine.
Both read the same settled levels, so what is pinned here is that the *placement*
is right -- which branches sit above the new one, which sit below, and that it
fires at all for an ordinary conversation.
"""

from __future__ import annotations

import asyncio
import pathlib
from types import SimpleNamespace

import pytest

from jiuwenswarm.agents.harness.common.rails.interrupt.interrupt_helpers import (
    build_permission_rail,
)
from jiuwenswarm.agents.harness.common.rails.permissions import scope_permissions
from jiuwenswarm.agents.harness.common.rails.permissions.tool_permission_context import (
    SKILLS_REBUILD_SILENT,
    TOOL_PERMISSION_CHANNEL_ID,
    TOOL_PERMISSION_CHAT_ID,
)


def _scene_hook_input(tool_name: str, user_input=None):
    from openjiuwen.harness.security.host import PermissionSceneHookInput

    return PermissionSceneHookInput(
        ctx=SimpleNamespace(session=None),
        tool_call=SimpleNamespace(id="call_1", name=tool_name, arguments={}),
        user_input=user_input,
        normalized_tool_name=tool_name,
        tool_args={},
        engine=None,
    )


def _config(scopes, tools=None, people=None, roles=None):
    return {
        "scopes": scopes,
        "people": people,
        "roles": roles,
        "channels": {},
        "permissions": {
            "enabled": True,
            "tools": tools if tools is not None else {"bash": "allow"},
            "defaults": {"*": "allow"},
        },
    }


@pytest.fixture
def deployment(monkeypatch):
    """Install a config, bind a conversation, and hand back the two call sites."""

    bound: list = []

    def install(scopes, *, channel="slack", chat="C0", tools=None, people=None, roles=None):
        import jiuwenswarm.common.config as config_module

        data = _config(scopes, tools, people, roles)
        monkeypatch.setattr(config_module, "get_config", lambda: data)
        scope_permissions.reset_cache()
        # Bound the way the request handler binds them, rather than patched:
        # the ContextVar *is* the mechanism under test.
        bound.append((TOOL_PERMISSION_CHANNEL_ID, TOOL_PERMISSION_CHANNEL_ID.set(channel)))
        bound.append((TOOL_PERMISSION_CHAT_ID, TOOL_PERMISSION_CHAT_ID.set(chat)))

        rail = build_permission_rail({"permissions": data["permissions"]})
        assert rail is not None
        return SimpleNamespace(
            hook=rail._host.permission_scene_hook,
            snapshot=rail._host.get_permissions_snapshot,
        )

    yield install
    for var, token in reversed(bound):
        var.reset(token)
    scope_permissions.reset_cache()


def _deny_bash(chat="C0"):
    return [
        {
            "match": {"channel": "slack", "chat": chat},
            "permissions": {"tools": {"bash": "deny"}},
        }
    ]


def _ask_bash(chat="C0"):
    return [
        {
            "match": {"channel": "slack", "chat": chat},
            "permissions": {"tools": {"bash": "ask"}},
        }
    ]


# --------------------------------------------------------------------------
# The scene hook branch
# --------------------------------------------------------------------------


def test_a_scope_deny_is_refused_before_anything_can_ask(deployment):
    sites = deployment(_deny_bash())

    outcome = asyncio.run(sites.hook(_scene_hook_input("bash")))

    assert outcome[0] == "reject"
    assert "scopes" in outcome[1]


def test_it_fires_without_any_permission_context(deployment):
    """The reason the branch sits above ``if perm_ctx is None: return None``.

    ``setup_permission_context`` builds a PermissionContext only for the
    digital-avatar scene or when memory is off, so an ordinary Slack turn has
    none. A branch below that line would never run for the conversations scopes
    exist to govern, and this test is what says so -- nothing here sets a
    context.
    """
    from jiuwenswarm.agents.harness.common.rails.permissions.owner_scopes import (
        TOOL_PERMISSION_CONTEXT,
    )

    assert TOOL_PERMISSION_CONTEXT.get() is None
    sites = deployment(_deny_bash())

    assert asyncio.run(sites.hook(_scene_hook_input("bash")))[0] == "reject"


def test_an_ask_is_left_to_the_engine(deployment):
    """The hook has no word for "ask", so it says nothing and the snapshot does.

    Returning ``approve`` here would widen; returning ``reject`` would turn a
    question into a refusal. ``None`` falls through to the narrowed config,
    where the engine can raise the interrupt an ask actually means.
    """
    sites = deployment(_ask_bash())

    assert asyncio.run(sites.hook(_scene_hook_input("bash"))) is None
    assert sites.snapshot()["tools"]["bash"] == "ask"


def test_a_scope_for_another_conversation_does_not_reach_this_one(deployment):
    sites = deployment(_deny_bash(chat="C-OTHER"), chat="C0")

    assert asyncio.run(sites.hook(_scene_hook_input("bash"))) is None
    assert sites.snapshot()["tools"]["bash"] == "allow"


def test_a_tool_no_scope_names_still_falls_through(deployment):
    sites = deployment(_deny_bash())

    assert asyncio.run(sites.hook(_scene_hook_input("write_file"))) is None


def test_no_scopes_at_all_changes_nothing(deployment):
    sites = deployment([])

    assert asyncio.run(sites.hook(_scene_hook_input("bash"))) is None
    assert sites.snapshot()["tools"]["bash"] == "allow"


# --------------------------------------------------------------------------
# What sits above the new branch, and why
# --------------------------------------------------------------------------


def test_ask_user_stays_out_of_scopes_reach(deployment):
    """Ordering, stated as behaviour.

    The ask_user bypass exists because the permission rail otherwise swallows
    the ask_user answer and re-pops its card forever (issue #1976). A scope
    denying ask_user would reopen that, so the bypass stays above and ask_user
    is not something a scope can refuse.
    """
    sites = deployment(
        [
            {
                "match": {"channel": "slack", "chat": "C0"},
                "permissions": {"tools": {"ask_user": "deny"}},
            }
        ]
    )

    assert asyncio.run(sites.hook(_scene_hook_input("ask_user"))) == ("approve",)


def test_the_silent_skills_rebuild_session_stays_above_scopes(deployment):
    """It has no UI to render an approval in, so it approves everything.

    A scope cannot be honoured there in any form -- there is nobody to ask and
    a refusal would leave a rebuild reported as successful while stopped on a
    write. The bypass keeps its place at the top.
    """
    sites = deployment(_deny_bash())
    token = SKILLS_REBUILD_SILENT.set(True)
    try:
        assert asyncio.run(sites.hook(_scene_hook_input("bash"))) == ("approve",)
        snapshot = sites.snapshot()
        assert snapshot["defaults"] == {"*": "allow"}
        assert "bash" not in (snapshot.get("tools") or {})
    finally:
        SKILLS_REBUILD_SILENT.reset(token)


# --------------------------------------------------------------------------
# The snapshot
# --------------------------------------------------------------------------


def test_the_snapshot_narrows_the_config_the_engine_evaluates(deployment):
    sites = deployment(_ask_bash(), tools={"bash": "allow", "write_file": "allow"})

    narrowed = sites.snapshot()

    assert narrowed["tools"]["bash"] == "ask"
    assert narrowed["tools"]["write_file"] == "allow"


def test_the_snapshot_cannot_widen(deployment):
    sites = deployment(
        [
            {
                "match": {"channel": "slack", "chat": "C0"},
                "permissions": {"tools": {"bash": "allow"}},
            }
        ],
        tools={"bash": "deny"},
    )

    assert sites.snapshot()["tools"]["bash"] == "deny"


def test_the_two_sites_agree_on_a_deny(deployment):
    sites = deployment(_deny_bash())

    assert asyncio.run(sites.hook(_scene_hook_input("bash")))[0] == "reject"
    assert sites.snapshot()["tools"]["bash"] == "deny"


def test_a_scope_narrowing_to_ask_defeats_a_persisted_always_allow(deployment):
    """The approval_overrides caveat, at the site that has to survive it.

    Without the pruning in common/scopes/permissions.py this returns "allow":
    an approval_overrides hit short-circuits ahead of the tool baseline, so the
    ask the operator wrote would never happen.
    """
    from openjiuwen.harness.security.permission_engine.toolguard.tool_policy import (
        evaluate_tiered_policy,
    )

    import jiuwenswarm.common.config as config_module

    sites = deployment(_ask_bash())
    data = config_module.get_config()
    data["permissions"]["approval_overrides"] = [
        {
            "id": "always-git",
            "tools": ["bash"],
            "match_type": "command",
            "pattern": "git status",
            "action": "allow",
        }
    ]

    level, _rule = evaluate_tiered_policy(
        sites.snapshot(), "bash", {"command": "git status"}
    )

    assert level.value == "ask"


# --------------------------------------------------------------------------
# The conversation the two sites read
# --------------------------------------------------------------------------


def test_every_entry_point_binds_the_chat_beside_the_channel():
    """A structural guard, because a missed binding fails silently.

    The chat id is bound and released beside the channel id, wherever that is
    bound. Today that is one contextmanager the three entry points share; it was
    four separate call sites before, and this counts rather than naming them so
    that it keeps holding either way. An entry point that set one and not the
    other would leave scopes matching on ``{channel}`` alone there -- a scope
    written for one conversation quietly applying to every conversation on that
    platform, which is the one direction a mistake here must not go.
    """
    source = (
        pathlib.Path(__file__).resolve().parents[3]
        / "jiuwenswarm/server/runtime/agent_adapter/interface_deep.py"
    ).read_text(encoding="utf-8")

    assert source.count("TOOL_PERMISSION_CHANNEL_ID.set(") == source.count(
        "TOOL_PERMISSION_CHAT_ID.set("
    )
    assert source.count("TOOL_PERMISSION_CHANNEL_ID.reset(") == source.count(
        "TOOL_PERMISSION_CHAT_ID.reset("
    )


# --------------------------------------------------------------------------
# Roles, runtime side
# --------------------------------------------------------------------------


def test_a_role_bearing_permissions_rule_is_refused_rather_than_inverted(deployment):
    """Closed at the config instead of biting at the tool call.

    The permission rail composes with no sender, so a ``not: {role: admin}``
    exemption would reach nobody and the restriction would land on exactly the
    people it was written to exempt -- a rule that reads as "everyone but the
    admins" and means "everyone, admins included", with nothing said about it
    anywhere. ``permissions`` is barred from the identity axes for that reason,
    so the rule is refused when it is compiled and the conversation falls to the
    layer below rather than to an inverted rule.

    Refusing is the direction the axis table always takes: it declines to
    tighten and grants nothing that was not already granted, and the operator
    gets a warning naming the rule instead of a silent inversion. Lift the bar
    and this rule starts meaning what it says -- the test then wants the admin
    exempted and everybody else rejected.
    """
    sites = deployment(
        [
            {
                "match": {"channel": "slack", "chat": "C0", "not": {"role": "admin"}},
                "permissions": {"tools": {"bash": "deny"}},
            }
        ],
        people={"harenome": {"slack": "U000000AAAA"}},
        roles={"admin": ["harenome"]},
    )

    outcome = asyncio.run(sites.hook(_scene_hook_input("bash")))

    assert outcome is None
    # The section was the whole of the rule, so nothing of it survives to settle
    # a level for anybody -- not the admin, and not the people it did restrict.
    assert scope_permissions.runtime_scopes() == ()


def test_a_role_no_config_declares_leaves_the_conversation_alone(deployment):
    sites = deployment(
        [
            {
                "match": {"channel": "slack", "chat": "C0", "not": {"role": "admin"}},
                "permissions": {"tools": {"bash": "deny"}},
            }
        ]
    )

    # No roles: block at all, so the scope is dropped rather than read as
    # excluding nobody. The warning says the restriction is not applied, and
    # this is that: bash falls through to the permissions block as written.
    outcome = asyncio.run(sites.hook(_scene_hook_input("bash")))

    assert outcome is None


def test_editing_only_the_roles_block_recompiles(deployment, monkeypatch):
    """The runtime compiles its own copy, so it needs its own people:/roles:.

    Both processes read the same file and neither serialises a scope to the
    other, so a block one of them forgot to pass would leave every role
    undeclared on that side alone -- a rule that resolves in the connector and
    not at the tool call, which is the one asymmetry a security section cannot
    have.

    The rule names ``delivery`` rather than ``permissions`` because
    ``permissions`` is barred from the identity axes until the runtime has a
    sender, and a barred rule is dropped before its role is ever resolved. What
    is pinned is the resolution and the cache key, which are the same for every
    section; put ``permissions`` back here when the bar lifts.
    """
    import jiuwenswarm.common.config as config_module

    scopes = [
        {
            "match": {"channel": "slack", "chat": "C0", "not": {"role": "admin"}},
            "delivery": {"prompt": "Be terse."},
        }
    ]
    deployment(scopes, people={"harenome": {"slack": "U1"}}, roles={"admin": ["harenome"]})
    assert scope_permissions.runtime_scopes()[0].match.not_users == ("U1",)

    # The scopes: list is byte for byte what it was. A cache keyed on it alone
    # would serve the old ids until something unrelated changed.
    monkeypatch.setattr(
        config_module,
        "get_config",
        lambda: _config(
            scopes, people={"harenome": {"slack": "U2"}}, roles={"admin": ["harenome"]}
        ),
    )

    assert scope_permissions.runtime_scopes()[0].match.not_users == ("U2",)


# --------------------------------------------------------------------------
# Neither entry point may raise into the callback that called it
# --------------------------------------------------------------------------


def test_a_compile_that_raises_leaves_the_permissions_block_as_written(
    deployment, monkeypatch
):
    """Both callbacks are agent-core's, and neither catches anything.

    The scene hook and the permissions snapshot are handed to
    ``ToolPermissionHost``, which catches nothing: an exception raised while
    settling a scope stops the tool call rather than the rule it was written
    for. The answer to a scope that cannot be settled is the permission block
    the deployment was already running on.
    """
    sites = deployment(_deny_bash())

    def explode(*_args, **_kwargs):
        raise RuntimeError("a scope this compiler cannot read")

    monkeypatch.setattr(scope_permissions, "compile_scopes", explode)
    scope_permissions.reset_cache()

    assert scope_permissions.runtime_scopes() == ()
    assert asyncio.run(sites.hook(_scene_hook_input("bash"))) is None
    assert sites.snapshot()["tools"] == {"bash": "allow"}


def test_a_fold_that_raises_leaves_the_permissions_block_as_written(
    deployment, monkeypatch
):
    """The same, one layer in: the scopes compiled and the fold is what failed.

    ``scope_refuses`` answers ``False`` rather than ``True``. A refusal here
    that the narrowed snapshot cannot also express would be a denial with
    nothing behind it, and the two have to agree.
    """
    sites = deployment(_deny_bash())

    def explode(*_args, **_kwargs):
        raise RuntimeError("levels that cannot be settled")

    monkeypatch.setattr(scope_permissions, "denied_tool_level", explode)
    monkeypatch.setattr(scope_permissions, "narrow_config_for_scopes", explode)

    assert scope_permissions.scope_refuses("bash") is False
    assert asyncio.run(sites.hook(_scene_hook_input("bash"))) is None
    assert sites.snapshot()["tools"] == {"bash": "allow"}
