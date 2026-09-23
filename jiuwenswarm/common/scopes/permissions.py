# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.

"""Folding the ``permissions`` section into the config the engine evaluates.

A mistake in ``permissions`` does not crash: it grants a tool call that should
have been refused. So this module does as little arithmetic of its own as it can
get away with, and borrows the rest.

**There is exactly one merge, and agent-core owns it.**
``narrow_permissions`` already exists in agent-core
(``openjiuwen/agent_teams/security/narrowing.py``): it deep-copies a permission
config and replaces each named tool's level with ``strictest(base, override)``.
jiuwenswarm already reaches for it from ``agents/swarm/providers/member_rails.py``
when a teammate is created with narrower rails than its leader. A scope is the
same operation with a different subject, so it uses the same function, twice:
folding several matching scopes into one settled level is also just
``strictest``, done by folding them through ``narrow_permissions`` over an
all-``allow`` base rather than by a second merge written by hand.

That makes *order-independent composition* structural rather than asserted.
``strictest`` is a meet: commutative and associative. Fold the layers in any
order and the settled level is the same, because nothing here ever takes "the
last one wins".

**And it is why ``allow`` in a scope can never widen anything.** A scope
saying ``allow`` folds to ``strictest(base, allow) == base``: it is a no-op by
construction, with no check to forget. The loader warns about it
so an operator is not left believing they granted something.

Two things this module has to do that ``narrow_permissions`` does not.

**An unattended channel degrades ``ask`` to ``deny``.** An ``ask``
reaching a channel where nobody can click, cron among them, is a turn that waits
forever. The degrade is applied to the *scope's* levels before they are folded,
never to the base config's own: degrading the base would change what every cron
job already does today, which is a far larger decision than this section.

**The ``approval_overrides`` caveat.** See :func:`narrow_config_for_scopes`; it
is the reason this module exists rather than a two-line call at the hook.

**Nothing here raises either.** The package contract already claims that for
the whole of ``common/scopes``, and until this module was changed it could not
honour it: the one thing it borrows is imported from agent-core at the point of
use, and both of that import's paths -- and its deliberate re-``raise`` -- ran
on every tool check, inside a permission hook nothing wraps. Both public
functions now answer "no
scope says anything" if the merge cannot run, which leaves the operator's
permission block applying exactly as written. It is never answered as an allow;
see :func:`_scopes_unavailable`.
"""

from __future__ import annotations

import logging
from typing import Any, Mapping, Sequence

from jiuwenswarm.common.scopes.capabilities import channel_capabilities
from jiuwenswarm.common.scopes.schema import (
    LEVEL_ALLOW,
    LEVEL_ASK,
    LEVEL_DENY,
    SECTION_PERMISSIONS,
    Scope,
    matching_scopes,
)

logger = logging.getLogger(__name__)

#: Fed to ``narrow_permissions`` as the base when the question is "what do the
#: scopes settle", with no real config underneath. It has to be ``allow`` rather
#: than empty: ``narrow_permissions`` resolves a tool missing from ``tools``
#: through ``defaults``, and falls back to ``ASK`` when ``defaults`` is silent --
#: which would turn a scope's ``allow`` into a settled ``ask`` and invent a
#: restriction nobody wrote.
_NEUTRAL_BASE: dict[str, Any] = {"tools": {}, "defaults": {"*": LEVEL_ALLOW}}

#: One line per (tool, rule) pair, not one per tool call. See
#: :func:`narrow_config_for_scopes`.
_reported_rule_conflicts: set[tuple[str, str]] = set()

#: Once, not once per tool call. See :func:`_scopes_unavailable`.
_reported_narrowing_failure = False


def _scopes_unavailable(what: str) -> None:
    """Report that the merge could not run, once, and let the caller carry on.

    **The package contract is that nothing here raises** (see
    :mod:`jiuwenswarm.common.scopes.schema`), and everything below it is called
    from the permission hook on every tool check. Three exits could break that:
    either of :func:`_narrow_permissions`' imports, and its deliberate ``raise``
    when the shim is not the thing that is wrong. An exception out of here would
    take down the turn -- not the rule, the turn -- over an agent-core module
    that could not be loaded, which is the same reasoning ``runtime_scopes``
    already applies to a config it cannot read.

    **What the caller returns instead is "no scope says anything", never
    "allow".** The two are not the same: the first leaves the permission block
    exactly as the operator wrote it, so every level, rule and override still
    applies and the engine still asks and still refuses. Swallowing into an
    allow would turn an unimportable module into a granted tool call, which is
    the one outcome a permission path may not have.

    A scope that *was* narrowing something is therefore not narrowing it any
    more, and that is worth a warning rather than a debug line -- but exactly
    one: the caller runs on every check, and every repeat reports the same
    failure.
    """
    global _reported_narrowing_failure
    if _reported_narrowing_failure:
        return
    _reported_narrowing_failure = True
    logger.warning(
        "[scopes] %s could not be evaluated; no scope narrows permissions and"
        " the permissions block applies as written",
        what,
        exc_info=True,
    )


def _narrow_permissions() -> Any:
    """agent-core's ``narrow_permissions``, imported where it is used.

    Lazily, because ``common/`` is imported by the gateway process too and the
    gateway has no permission engine in it: a module-level import would pull
    ``agent_teams`` into a process that will never evaluate a tool call.

    **The fallback is repairing agent-core's own re-export, not replacing it.**
    ``openjiuwen/harness/security/tiered_policy.py`` is a compatibility shim
    whose whole body is ``from …toolguard.tool_policy import *``, and a star
    import skips names beginning with an underscore. ``narrowing.py`` imports
    ``_parse_level`` from that shim *by name*, so the module raises ImportError
    on every version of agent-core that has the shim -- including the one in
    service. (The same import sits in ``agents/swarm/providers/member_rails.py``,
    where it takes the team permission rail down with it; that is the same
    upstream defect and is not fixed here.) Putting the one missing name back on
    the shim and retrying gets the real function rather than a local
    reimplementation of it: two narrowing implementations that could disagree is
    the failure this module exists to avoid, and a broken import is not a good
    enough reason to create one. Delete this branch when agent-core exports the
    name.
    """
    try:
        from openjiuwen.agent_teams.security.narrowing import narrow_permissions
    except ImportError:
        from openjiuwen.harness.security import tiered_policy as _shim
        from openjiuwen.harness.security.permission_engine.toolguard import (
            tool_policy as _canonical,
        )

        missing = [
            name
            for name in ("_parse_level",)
            if not hasattr(_shim, name) and hasattr(_canonical, name)
        ]
        if not missing:
            raise
        for name in missing:
            setattr(_shim, name, getattr(_canonical, name))
        logger.debug(
            "[scopes] restored %s on the tiered_policy shim so"
            " agent_teams.security.narrowing can be imported",
            ", ".join(missing),
        )
        from openjiuwen.agent_teams.security.narrowing import narrow_permissions

    return narrow_permissions


def _narrow(base: Mapping[str, Any], override: Mapping[str, str]) -> dict[str, Any]:
    return _narrow_permissions()(dict(base), dict(override))


def _scope_layers(
    scopes: Sequence[Scope], *, channel: "str | None", chat: "str | None"
) -> tuple[dict[str, str], ...]:
    """Each matching scope's ``permissions.tools`` map, in cascade order.

    The order is ``matching_scopes``' and is kept only so that a log line reads
    the way the file does. Nothing downstream depends on it: the fold is
    order-independent.
    """
    layers: list[dict[str, str]] = []
    for scope in matching_scopes(
        scopes, channel=channel, chat=chat, section=SECTION_PERMISSIONS
    ):
        tools = scope.section(SECTION_PERMISSIONS).get("tools")
        if isinstance(tools, Mapping) and tools:
            layers.append({str(k): str(v) for k, v in tools.items()})
    return tuple(layers)


def _degrade_unattended(
    levels: Mapping[str, str], *, channel: "str | None"
) -> dict[str, str]:
    """Where no human can answer an approval, ``ask`` means ``deny``.

    Only on a channel that has *declared* ``attended=False``. A channel with no
    declaration is left alone rather than assumed unattended: the declaration is
    the opt-in, and guessing here would silently deny on a surface that
    renders approvals perfectly well -- turning a rule the operator wrote as
    "check with me" into "never", which is a failure they cannot see.
    """
    if channel is None:
        return dict(levels)
    capabilities = channel_capabilities(channel)
    if capabilities is None or capabilities.attended:
        return dict(levels)
    return {
        tool: (LEVEL_DENY if level == LEVEL_ASK else level)
        for tool, level in levels.items()
    }


def settled_tool_levels(
    scopes: Sequence[Scope], *, channel: "str | None", chat: "str | None" = None
) -> dict[str, str]:
    """What the matching scopes settle for each tool they name.

    Folded through ``narrow_permissions`` over an all-``allow`` base, so the
    fold is the same ``strictest`` the config narrowing uses and the two can
    never drift apart. Returns ``{}`` when no scope says anything, which every
    caller must read as "leave the configuration exactly as it was".

    Also ``{}`` when the fold cannot run at all -- see :func:`_scopes_unavailable`
    for why that is the answer and why it is not an allow.
    """
    layers = _scope_layers(scopes, channel=channel, chat=chat)
    if not layers:
        return {}
    settled: dict[str, Any] = dict(_NEUTRAL_BASE)
    try:
        for layer in layers:
            settled = _narrow(settled, _degrade_unattended(layer, channel=channel))
    except Exception:  # noqa: BLE001
        _scopes_unavailable("the permission levels a scope settles")
        return {}
    tools = settled.get("tools")
    return {str(k): str(v) for k, v in tools.items()} if isinstance(tools, Mapping) else {}


def _prune_approval_overrides(
    config: dict[str, Any], narrowed_tools: Mapping[str, str]
) -> None:
    """Take the narrowed tools out of ``approval_overrides``, in place.

    ``narrow_permissions`` is monotone over the ``tools`` field *only*, and
    ``tools`` is not the first thing the engine looks at. Reading
    ``evaluate_tiered_policy`` in ``permission_engine/toolguard/tool_policy.py``
    top to bottom:

    1. a ``tools`` baseline of ``deny`` short-circuits to DENY, ahead of
       everything below -- so narrowing a tool to ``deny`` *is* enforceable;
    2. a ``builtin`` or user ``rules`` entry resolving to DENY wins next;
    3. **an ``approval_overrides`` hit short-circuits to ALLOW** -- ahead of the
       ``tools`` baseline;
    4. only then do the rules' non-deny hits and the ``tools`` baseline apply.

    So narrowing a tool from ``allow`` to ``ask`` does **not** by itself produce
    an ask: a persisted "always allow" for that tool matches at step 3 and the
    ask never happens. The two mechanisms would disagree silently, which is
    precisely the failure order-independent composition exists to avoid.

    Removing the narrowed tool from the overrides closes it, and closes it
    *safely*: an override hit can only ever return ALLOW, so dropping one can
    only make the outcome stricter or leave it unchanged. That one-directional
    property is what makes this pruning sound; it is **not** true of ``rules``,
    which can resolve to ASK, so those are reported rather than edited -- see
    :func:`narrow_config_for_scopes`.

    An entry naming several tools keeps the ones no scope narrowed; an entry
    left naming nothing is dropped.
    """
    overrides = config.get("approval_overrides")
    if not isinstance(overrides, list) or not overrides:
        return

    kept: list[Any] = []
    for entry in overrides:
        if not isinstance(entry, dict):
            kept.append(entry)
            continue
        tools = entry.get("tools")
        if isinstance(tools, str):
            tools = [tools]
        if not isinstance(tools, list):
            kept.append(entry)
            continue
        remaining = [
            name
            for name in tools
            if narrowed_tools.get(str(name).strip(), LEVEL_ALLOW) == LEVEL_ALLOW
        ]
        if not remaining:
            logger.info(
                "[scopes] approval_overrides[%s] dropped for this conversation:"
                " a scope narrows %s, and an override would otherwise allow it"
                " before the narrowed level is ever consulted",
                entry.get("id", "?"),
                ", ".join(sorted(str(name) for name in tools)),
            )
            continue
        if len(remaining) != len(tools):
            entry = {**entry, "tools": remaining}
        kept.append(entry)

    config["approval_overrides"] = kept


def _report_rule_conflicts(
    config: Mapping[str, Any], narrowed_tools: Mapping[str, str]
) -> None:
    """Say when a ``rules`` entry will out-rank a narrowing, since we cannot fix it.

    Same short-circuit as the overrides, one step lower: a matching ``rules`` or
    ``builtin`` entry is consulted before the ``tools`` baseline, so one whose
    action is ``allow`` defeats a scope that narrowed the tool to ``ask``.
    ``deny`` is unaffected -- step 1 above returns before any rule is read.

    Unlike an override, a rule is *not* safe to prune: a rule can resolve to ASK
    as well as to ALLOW, and removing that one would leave the call on a
    baseline the rule was tightening -- a widening, done in the name of
    narrowing. So this reports and leaves the config alone. It is deduplicated
    per (tool, rule) because the snapshot is rebuilt on every first check.
    """
    rules = config.get("rules")
    if not isinstance(rules, list):
        return
    for entry in rules:
        if not isinstance(entry, dict):
            continue
        if str(entry.get("action") or "").strip().lower() != LEVEL_ALLOW:
            continue
        tools = entry.get("tools")
        if isinstance(tools, str):
            tools = [tools]
        if not isinstance(tools, list):
            continue
        rule_id = str(entry.get("id") or entry.get("pattern") or "?")
        for name in tools:
            tool = str(name).strip()
            if narrowed_tools.get(tool) != LEVEL_ASK:
                continue
            if (tool, rule_id) in _reported_rule_conflicts:
                continue
            _reported_rule_conflicts.add((tool, rule_id))
            logger.warning(
                "[scopes] permissions.tools.%s is narrowed to ask, but"
                " permissions.rules[%s] allows %s and is evaluated before the"
                " tool baseline, so that call may still run without asking."
                " Narrow it to deny, or change that rule",
                tool,
                rule_id,
                tool,
            )


def narrow_config_for_scopes(
    permission_config: Mapping[str, Any],
    scopes: Sequence[Scope],
    *,
    channel: "str | None",
    chat: "str | None" = None,
) -> dict[str, Any]:
    """The permission config this conversation should be evaluated against.

    Returns the config unchanged when no scope narrows anything here, so a
    deployment with an empty ``scopes:`` pays one dict lookup and gets back the
    object it passed in. Unchanged too when the merge cannot run: the operator's
    own permission block is what a failure here falls back to, never a wider one.
    """
    if not isinstance(permission_config, Mapping):
        return permission_config  # type: ignore[return-value]
    # The answer to both "nothing to narrow" and "the narrowing could not run",
    # named once so the two cannot come to differ. A dict is handed straight
    # back, so an unchanged config is the object the caller passed in.
    as_written = (
        permission_config
        if isinstance(permission_config, dict)
        else dict(permission_config)
    )
    levels = settled_tool_levels(scopes, channel=channel, chat=chat)
    tightened = {
        tool: level for tool, level in levels.items() if level != LEVEL_ALLOW
    }
    if not tightened:
        return as_written

    try:
        narrowed = _narrow(permission_config, tightened)
        _prune_approval_overrides(narrowed, tightened)
        _report_rule_conflicts(narrowed, tightened)
    except Exception:  # noqa: BLE001
        _scopes_unavailable("the permission config a scope narrows")
        return as_written
    logger.info(
        "[scopes] permissions narrowed for channel=%s chat=%s: %s",
        channel,
        chat,
        ", ".join(f"{tool}={level}" for tool, level in sorted(tightened.items())),
    )
    return narrowed


def denied_tool_level(
    scopes: Sequence[Scope],
    tool_name: str,
    *,
    channel: "str | None",
    chat: "str | None" = None,
) -> "str | None":
    """``"deny"`` if the scopes refuse ``tool_name`` here, else ``None``.

    The floor the permission scene hook enforces ahead of the engine. It reads
    the *same* :func:`settled_tool_levels` the config narrowing reads, so the
    two cannot answer differently; it is a strict subset of what the narrowing
    already expresses, and exists because the hook runs on every check while the
    config snapshot is only rebuilt on the first one -- and because a refusal
    should never be rendered as an approval card somebody could click.

    ``ask`` is deliberately not returned. The hook's vocabulary is approve or
    reject and has no third word, so an ``ask`` answered here could only become
    one of those two; it is left to the narrowed config, where the engine can
    still raise the interrupt an ``ask`` means.
    """
    levels = settled_tool_levels(scopes, channel=channel, chat=chat)
    return LEVEL_DENY if levels.get(tool_name) == LEVEL_DENY else None


__all__ = [
    "denied_tool_level",
    "narrow_config_for_scopes",
    "settled_tool_levels",
]
