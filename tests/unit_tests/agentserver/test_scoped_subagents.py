# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Scoped subagent names restrict delegation in the owning session."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from openjiuwen.harness.tools.subagent.subagent_tools import build_subagent_tools
from openjiuwen.harness.tools.subagent.task_tool import create_task_tool

from jiuwenswarm.server.runtime.agent_adapter.scoped_subagents import (
    apply_scoped_subagent_availability,
)
from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
    JiuWenSwarmDeepAdapter,
)


class _Rail:
    def __init__(self, tools, names):
        self.tools = tools
        self.names = frozenset(names)
        self.system_prompt_builder = SimpleNamespace(language="en")

    def refresh_available_agents(self, agent):
        for tool in self.tools:
            if hasattr(tool, "set_allowed_subagent_types"):
                tool.set_allowed_subagent_types(self.names)

    @staticmethod
    def _extract_agent_meta(spec):
        return spec.agent_card.name, spec.agent_card.description

    @staticmethod
    def _build_available_agents_description(specs):
        return "\n".join(
            f"- {spec.agent_card.name}: {spec.agent_card.description}"
            for spec in specs
        )


def _agent(tool_kind="task_tool", names=("research_agent", "browser_agent")):
    specs = [
        SimpleNamespace(agent_card=SimpleNamespace(name=name, description=f"{name} work"))
        for name in names
    ]
    if tool_kind == "task_tool":
        tools = create_task_tool(
            parent_agent=None,
            available_agents="all",
            language="en",
            allowed_subagent_types=names,
        )
    else:
        tools = [
            tool for tool in build_subagent_tools(
                parent_agent=None,
                language="en",
                available_agents="all",
                allowed_subagent_types=names,
            )
            if tool.card.name == "subagent_spawn"
        ]
    rail = _Rail(tools, names)
    agent = SimpleNamespace(
        deep_config=SimpleNamespace(subagents=specs),
        _pending_rails=(),
        _registered_rails=(rail,),
    )
    return agent, tools[0]


def test_task_tool_denies_hidden_name_and_restores_global_roster():
    agent, tool = _agent()
    apply_scoped_subagent_availability(agent, {"agent_subagents_available": ["research_agent"]})

    assert "research_agent" in tool.card.description
    assert "- browser_agent:" not in tool.card.description
    with pytest.raises(Exception, match="not available through task_tool"):
        tool._parse_invocation_inputs({"subagent_type": "browser_agent", "task_description": "browse"})
    assert tool._parse_invocation_inputs(
        {"subagent_type": "research_agent", "task_description": "research"}
    )[0] == "research_agent"

    apply_scoped_subagent_availability(agent, {})
    assert tool._parse_invocation_inputs(
        {"subagent_type": "browser_agent", "task_description": "browse"}
    )[0] == "browser_agent"


def test_empty_and_unknown_names_do_not_enable_an_agent():
    agent, tool = _agent(names=("research_agent",))
    apply_scoped_subagent_availability(agent, {"agent_subagents_available": []})
    assert tool._allowed_subagent_types == frozenset()

    apply_scoped_subagent_availability(agent, {"agent_subagents_available": ["browser_agent"]})
    assert tool._allowed_subagent_types == frozenset()
    assert "- browser_agent:" not in tool.card.description


def test_code_agent_requires_global_registration_and_scope_permission():
    agent, tool = _agent(names=("research_agent", "code_agent"))
    apply_scoped_subagent_availability(
        agent, {"agent_subagents_available": ["research_agent"]}
    )
    with pytest.raises(Exception, match="not available through task_tool"):
        tool._parse_invocation_inputs(
            {"subagent_type": "code_agent", "task_description": "edit a file"}
        )

    apply_scoped_subagent_availability(
        agent, {"agent_subagents_available": ["code_agent"]}
    )
    assert tool._parse_invocation_inputs(
        {"subagent_type": "code_agent", "task_description": "edit a file"}
    )[0] == "code_agent"

    disabled_agent, disabled_tool = _agent(names=("research_agent",))
    apply_scoped_subagent_availability(
        disabled_agent, {"agent_subagents_available": ["code_agent"]}
    )
    with pytest.raises(Exception, match="not available through task_tool"):
        disabled_tool._parse_invocation_inputs(
            {"subagent_type": "code_agent", "task_description": "edit a file"}
        )


def test_overlapping_request_cannot_widen_active_round():
    agent, tool = _agent()
    apply_scoped_subagent_availability(agent, {"agent_subagents_available": ["research_agent"]})
    apply_scoped_subagent_availability(
        agent,
        {"agent_subagents_available": ["browser_agent"]},
        active_round=True,
    )
    assert tool._allowed_subagent_types == frozenset()

    apply_scoped_subagent_availability(agent, {}, active_round=True)
    assert tool._allowed_subagent_types == frozenset()
    apply_scoped_subagent_availability(agent, {})
    assert tool._allowed_subagent_types == frozenset({"research_agent", "browser_agent"})


@pytest.mark.asyncio
async def test_persistent_spawn_rejects_disallowed_agent_before_dispatch():
    agent, tool = _agent("subagent_spawn")
    apply_scoped_subagent_availability(agent, {"agent_subagents_available": ["research_agent"]})
    with pytest.raises(Exception, match="not available through subagent_spawn"):
        await tool.invoke({
            "subagent_type": "browser_agent",
            "task_description": "browse",
            "display_name": "browser",
            "role": "research",
        })


@pytest.mark.asyncio
async def test_concurrent_sessions_keep_separate_rosters():
    first, first_tool = _agent()
    second, second_tool = _agent()
    first_adapter = JiuWenSwarmDeepAdapter()
    second_adapter = JiuWenSwarmDeepAdapter()
    first_adapter._instance = first
    second_adapter._instance = second
    first_adapter._enable_auto_permission = False
    second_adapter._enable_auto_permission = False
    await asyncio.gather(
        first_adapter._prepare_root_input_dispatch(
            SimpleNamespace(params={"agent_subagents_available": ["research_agent"]}),
            {},
        ),
        second_adapter._prepare_root_input_dispatch(
            SimpleNamespace(params={"agent_subagents_available": ["browser_agent"]}),
            {},
        ),
    )

    assert first_tool._allowed_subagent_types == frozenset({"research_agent"})
    assert second_tool._allowed_subagent_types == frozenset({"browser_agent"})
    with pytest.raises(Exception, match="not available through task_tool"):
        first_tool._parse_invocation_inputs(
            {"subagent_type": "browser_agent", "task_description": "browse"}
        )
    assert second_tool._parse_invocation_inputs(
        {"subagent_type": "browser_agent", "task_description": "browse"}
    )[0] == "browser_agent"

    apply_scoped_subagent_availability(first, {"agent_subagents_available": ["browser_agent"]})
    assert first_tool._allowed_subagent_types == frozenset({"browser_agent"})
    apply_scoped_subagent_availability(first, {})
    assert first_tool._allowed_subagent_types == frozenset({"research_agent", "browser_agent"})


@pytest.mark.parametrize("value", [None, "research_agent", [""], [1], {}])
def test_invalid_trusted_request_value_fails_closed(value):
    agent, _ = _agent()
    with pytest.raises(ValueError, match="agent_subagents_available"):
        apply_scoped_subagent_availability(agent, {"agent_subagents_available": value})
