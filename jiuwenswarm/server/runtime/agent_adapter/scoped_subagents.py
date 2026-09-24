# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Apply a request's subagent ceiling to its session-owned delegation tools."""

from __future__ import annotations

from typing import Any

from openjiuwen.harness.prompts.tools import get_tool_description


_DELEGATION_TOOLS = {"task_tool", "subagent_spawn"}


def _requested_names(params: dict[str, Any]) -> frozenset[str] | None:
    if "agent_subagents_available" not in params:
        return None
    names = params["agent_subagents_available"]
    if not isinstance(names, list) or any(
        not isinstance(name, str) or not name.strip() for name in names
    ):
        raise ValueError("agent_subagents_available must be a list of non-empty names")
    return frozenset(name.strip() for name in names)


def apply_scoped_subagent_availability(
    agent: Any,
    params: dict[str, Any],
    *,
    active_round: bool = False,
) -> None:
    """Restrict the advertised roster and both SDK delegation tools for one session."""
    requested = _requested_names(params)
    rails = (
        *(getattr(agent, "_pending_rails", ()) or ()),
        *(getattr(agent, "_registered_rails", ()) or ()),
    )
    for rail in rails:
        refresh = getattr(rail, "refresh_available_agents", None)
        tools = getattr(rail, "tools", None)
        if not callable(refresh) or not tools:
            continue
        previous = {
            tool.card.name: getattr(tool, "_allowed_subagent_types", None)
            for tool in tools
            if getattr(tool, "card", None) is not None
        }
        # Refresh the SDK tool cards after runtime configuration changes.
        refresh(agent)
        specs = list(getattr(agent.deep_config, "subagents", None) or ())
        partition = getattr(rail, "_partition_runtime_subagents", None)
        runtime_mode = getattr(rail, "_runtime_mode", None)
        if callable(partition) and callable(runtime_mode) and runtime_mode():
            runtime_specs, sync_specs = partition(specs)
        else:
            runtime_specs, sync_specs = specs, specs
        language = getattr(
            getattr(rail, "system_prompt_builder", None), "language", "cn"
        )
        for tool in tools:
            card = getattr(tool, "card", None)
            name = getattr(card, "name", None)
            if name not in _DELEGATION_TOOLS:
                continue
            set_allowed = getattr(tool, "set_allowed_subagent_types", None)
            if not callable(set_allowed):
                raise RuntimeError(f"{name} cannot enforce scoped subagent availability")
            tool_specs = runtime_specs if name == "subagent_spawn" else sync_specs
            globally_enabled = frozenset(
                rail._extract_agent_meta(spec)[0] for spec in tool_specs
            )
            allowed = (
                globally_enabled if requested is None
                else globally_enabled.intersection(requested)
            )
            prior_allowed = previous.get(name)
            # Supplemental input can arrive while an earlier turn runs.
            # Keep the narrower ceiling until that SDK round ends.
            if active_round and prior_allowed is not None:
                allowed = allowed.intersection(prior_allowed)
            visible = [
                spec for spec in tool_specs
                if rail._extract_agent_meta(spec)[0] in allowed
            ]
            card.description = get_tool_description(name, language).format(
                available_agents=rail._build_available_agents_description(visible),
            )
            set_allowed(allowed)


__all__ = ["apply_scoped_subagent_availability"]
