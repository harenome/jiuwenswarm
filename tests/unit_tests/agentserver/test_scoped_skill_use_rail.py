"""Scoped Skill views must follow each request, including reused sessions."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from openjiuwen.core.single_agent.rail.base import AgentCallbackEvent, RunContext
from openjiuwen.harness.deep_agent import DeepAgent
from openjiuwen.harness.prompts import SystemPromptBuilder
from openjiuwen.harness.prompts.sections import SectionName
from openjiuwen.harness.rails import SkillUseRail
from openjiuwen.harness.schema.interaction import RoundWorkItem
from openjiuwen.harness.tools import ListSkillTool, SkillTool

from jiuwenswarm.agents.harness.common.rails.scoped_skill_use_rail import (
    SCOPED_SKILLS_RUN_CONTEXT_KEY,
    ScopedSkillUseRail,
    bind_scoped_skills,
    required_skill_text,
    reset_scoped_skills,
    scoped_skill_names,
    unrestricted_skill_inventory,
    with_scoped_skill_run_context,
)
from jiuwenswarm.agents.harness.common.rails.skill_retrieval_prompt_rail import (
    SkillRetrievalPromptRail,
)
from jiuwenswarm.agents.harness.common.tools.skill_retrieval_toolkits import (
    SkillRetrievalToolkit,
)


def _skill(root, name, body):
    skill_dir = root / name
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(body, encoding="utf-8")


@pytest_asyncio.fixture(name="skill_rail")
async def skill_rail_fixture(tmp_path):
    _skill(tmp_path, "ambient", "---\nname: ambient\n---\nAmbient instructions")
    _skill(tmp_path, "research", "---\nname: research\n---\nResearch instructions")
    rail = ScopedSkillUseRail(str(tmp_path), skill_mode="all", include_tools=False)
    await rail.reload_skills()
    return rail


def test_scope_binding_preserves_empty_catalog_and_restores_outer_scope():
    outer = bind_scoped_skills({"agent_skills_available": ["ambient"]})
    try:
        assert scoped_skill_names() == frozenset({"ambient"})
        inner = bind_scoped_skills({"agent_skills_available": []})
        try:
            assert scoped_skill_names() == frozenset()
        finally:
            reset_scoped_skills(inner)
        with unrestricted_skill_inventory():
            assert scoped_skill_names() is None
        assert scoped_skill_names() == frozenset({"ambient"})
    finally:
        reset_scoped_skills(outer)
    assert scoped_skill_names() is None


@pytest.mark.asyncio
async def test_skill_tool_view_changes_between_turns(skill_rail):
    assert {skill.name for skill in skill_rail.get_skills_for_session()} == {
        "ambient", "research"
    }
    token = bind_scoped_skills({"agent_skills_available": ["ambient"]})
    try:
        assert [skill.name for skill in skill_rail.get_skills_for_session()] == [
            "ambient"
        ]
    finally:
        reset_scoped_skills(token)
    token = bind_scoped_skills({"agent_skills_available": []})
    try:
        assert skill_rail.get_skills_for_session() == []
    finally:
        reset_scoped_skills(token)
    assert len(skill_rail.get_skills_for_session()) == 2


@pytest.mark.asyncio
async def test_disallowed_skill_tool_call_fails_at_dispatch(skill_rail):
    skill_tool = SkillTool(operation=None, get_skills=skill_rail.get_skills_for_session)
    list_tool = ListSkillTool(get_skills=skill_rail.get_skills_for_session)
    token = bind_scoped_skills({"agent_skills_available": ["ambient"]})
    try:
        denied = await skill_tool.invoke({"skill_name": "research"})
        listed = await list_tool.invoke({})
        assert denied.success is False
        assert denied.error == "Skill not found: research"
        assert [skill["name"] for skill in listed.data["skills"]] == ["ambient"]
    finally:
        reset_scoped_skills(token)


@pytest.mark.asyncio
async def test_native_prompt_catalog_follows_available_list(skill_rail, monkeypatch):
    skill_rail.system_prompt_builder = SimpleNamespace(language="en")
    queries = []

    def apply_skill_budget(skills, query=None):
        queries.append(query)
        return skills

    monkeypatch.setattr(skill_rail, "_apply_skill_budget", apply_skill_budget)
    token = bind_scoped_skills({"agent_skills_available": ["ambient"]})
    try:
        content = skill_rail._build_skills_section(query="answer ambient").render("en")
        assert "ambient" in content
        assert "research" not in content
        assert queries == ["answer ambient"]
    finally:
        reset_scoped_skills(token)


@pytest.mark.asyncio
async def test_required_skill_instructions_are_loaded_before_model_call(
    skill_rail, monkeypatch
):
    sections = {}
    skill_rail.system_prompt_builder = SimpleNamespace(
        language="en",
        get_section=lambda name: sections.get(name),
        add_section=lambda section: sections.__setitem__(section.name, section),
    )

    async def native_prompt(rail, _ctx):
        sections[SectionName.SKILLS] = rail._build_skills_section()

    monkeypatch.setattr(SkillUseRail, "_sync_skill_prompt_and_attachment", native_prompt)
    token = bind_scoped_skills({
        "agent_skills_available": ["ambient"],
        "agent_skills_required": ["ambient"],
    })
    try:
        native_catalog = skill_rail._build_skills_section().render("en")
        await skill_rail._sync_skill_prompt_and_attachment(None)
        content = sections[SectionName.SKILLS].render("en")
        assert content.startswith(native_catalog + "\n\n# Required Skills\n")
        assert (
            "Follow each instruction on this turn according to its stated conditions."
            in content
        )
        assert "You do not need `skill_tool` to read this text." in content
        assert "Ambient instructions" in content
        assert "Research instructions" not in content
        await skill_rail._sync_skill_prompt_and_attachment(None)
        content = sections[SectionName.SKILLS].render("en")
        assert content.count("# Required Skills") == 1
        assert content.count("## Required Skill: ambient") == 1
        assert content.count("Ambient instructions") == 1
    finally:
        reset_scoped_skills(token)

    token = bind_scoped_skills({"agent_skills_available": ["research"]})
    try:
        native_catalog = skill_rail._build_skills_section().render("en")
        await skill_rail._sync_skill_prompt_and_attachment(None)
        content = sections[SectionName.SKILLS].render("en")
        assert content == native_catalog
        assert "## Required Skill: ambient" not in content
        assert "research" in content
    finally:
        reset_scoped_skills(token)


@pytest.mark.asyncio
async def test_inner_model_and_tool_callbacks_restore_round_scope(skill_rail):
    """The SDK model and tool workers predate the outer round's ContextVars."""
    assert {
        AgentCallbackEvent.BEFORE_MODEL_CALL,
        AgentCallbackEvent.AFTER_MODEL_CALL,
        AgentCallbackEvent.BEFORE_TOOL_CALL,
        AgentCallbackEvent.AFTER_TOOL_CALL,
    } <= skill_rail.get_callbacks().keys()
    builder = SystemPromptBuilder(language="en")
    skill_rail.system_prompt_builder = builder
    run_context = RunContext(extra={
        SCOPED_SKILLS_RUN_CONTEXT_KEY: {
            "agent_skills_available": ["ambient"],
            "agent_skills_required": ["ambient"],
        }
    })

    worker_ready = asyncio.Event()
    resume_worker = asyncio.Event()

    async def inner_worker():
        worker_ready.set()
        await resume_worker.wait()
        assert scoped_skill_names() is None
        model_ctx = SimpleNamespace(
            extra={"run_context": run_context},
            inputs=SimpleNamespace(messages=[]),
            session=None,
        )
        await skill_rail.before_model_call(model_ctx)
        assert scoped_skill_names() == frozenset({"ambient"})
        assert "Ambient instructions" in required_skill_text()
        assert "Ambient instructions" in builder.build()
        assert "research" not in builder.build()
        await skill_rail.after_model_call(model_ctx)
        assert scoped_skill_names() is None
        assert required_skill_text() == ""

        tool_ctx = SimpleNamespace(extra={"run_context": run_context})
        skill_tool = SkillTool(
            operation=None, get_skills=skill_rail.get_skills_for_session
        )
        list_tool = ListSkillTool(get_skills=skill_rail.get_skills_for_session)
        await skill_rail.before_tool_call(tool_ctx)
        assert (await skill_tool.invoke({"skill_name": "research"})).error == (
            "Skill not found: research"
        )
        listed = await list_tool.invoke({})
        assert [item["name"] for item in listed.data["skills"]] == ["ambient"]
        await skill_rail.after_tool_call(tool_ctx)
        assert scoped_skill_names() is None

        unscoped_ctx = SimpleNamespace(
            extra={}, inputs=SimpleNamespace(messages=[]), session=None
        )
        await skill_rail.before_model_call(unscoped_ctx)
        assert "## Required Skill: ambient" not in builder.build()
        assert "research" in builder.build()
        await skill_rail.after_model_call(unscoped_ctx)

    worker = asyncio.create_task(inner_worker())
    await worker_ready.wait()
    outer = bind_scoped_skills({"agent_skills_available": ["research"]})
    try:
        resume_worker.set()
        await worker
        assert scoped_skill_names() == frozenset({"research"})
    finally:
        reset_scoped_skills(outer)


@pytest.mark.asyncio
async def test_required_disabled_skill_fails_before_model_call(
    skill_rail, monkeypatch
):
    skill_rail.system_prompt_builder = SimpleNamespace(language="en")

    async def no_native_prompt(_self, _ctx):
        return None

    monkeypatch.setattr(SkillUseRail, "_sync_skill_prompt_and_attachment", no_native_prompt)
    skill_rail.disabled_skills = {"ambient"}
    await skill_rail.reload_skills()
    token = bind_scoped_skills({"agent_skills_required": ["ambient"]})
    try:
        with pytest.raises(ValueError, match="not installed or enabled: ambient"):
            await skill_rail._sync_skill_prompt_and_attachment(None)
    finally:
        reset_scoped_skills(token)


@pytest.mark.asyncio
async def test_required_skill_rejects_prompt_mode_none(skill_rail, monkeypatch):
    skill_rail.system_prompt_builder = SimpleNamespace(mode="none")

    async def no_native_prompt(_self, _ctx):
        return None

    monkeypatch.setattr(SkillUseRail, "_sync_skill_prompt_and_attachment", no_native_prompt)
    token = bind_scoped_skills({"agent_skills_required": ["ambient"]})
    try:
        with pytest.raises(ValueError, match="prompt_mode=none"):
            await skill_rail._sync_skill_prompt_and_attachment(None)
    finally:
        reset_scoped_skills(token)


@pytest.mark.asyncio
async def test_retrieval_keeps_required_text_after_hiding_catalog(
    skill_rail, monkeypatch
):
    sections = {}
    builder = SimpleNamespace(
        language="en",
        get_section=lambda name: sections.get(name),
        add_section=lambda section: sections.__setitem__(section.name, section),
        remove_section=lambda name: sections.pop(name, None),
    )
    skill_rail.system_prompt_builder = builder

    async def native_prompt(rail, _ctx):
        sections[SectionName.SKILLS] = rail._build_skills_section()

    monkeypatch.setattr(SkillUseRail, "_sync_skill_prompt_and_attachment", native_prompt)
    retrieval = SkillRetrievalPromptRail.__new__(SkillRetrievalPromptRail)
    retrieval._agent = None
    retrieval.system_prompt_builder = builder
    retrieval.attachment_manager = None
    retrieval._session_enabled = True
    retrieval._hidden_skills_section = None
    retrieval._has_skill_index = lambda _ctx: True
    retrieval._hide_legacy_list_skill = lambda: None
    retrieval._filter_legacy_list_skill_from_model_inputs = lambda _ctx: None
    retrieval._prompt_snapshot = lambda: object()
    retrieval._build_candidate_appendix = lambda _lang, _snapshot: "catalog"
    retrieval._add_prompt_builder_section = lambda _lang, _appendix: None

    async def no_op(_ctx):
        return None

    retrieval._clear_runtime_skill_attachment = no_op
    retrieval._clear_prompt_attachments = no_op
    token = bind_scoped_skills({"agent_skills_required": ["ambient"]})
    try:
        await skill_rail._sync_skill_prompt_and_attachment(None)
        await retrieval._sync_prompt_attachment(SimpleNamespace(agent=None))
        content = sections[SectionName.SKILLS].render("en")
        assert "Ambient instructions" in content
        assert "research" not in content
    finally:
        reset_scoped_skills(token)


def test_required_skill_must_be_available():
    with pytest.raises(ValueError, match="outside agent_skills_available"):
        bind_scoped_skills({
            "agent_skills_available": [],
            "agent_skills_required": ["ambient"],
        })


def test_explicit_null_catalog_does_not_inherit_all():
    with pytest.raises(ValueError, match="agent_skills_available must be a list"):
        bind_scoped_skills({"agent_skills_available": None})


@pytest.mark.asyncio
async def test_two_sdk_rounds_bind_each_turns_skill_scope(skill_rail, monkeypatch):
    """The SDK supervisor does not inherit the Host send_input ContextVars."""
    sections = {}
    skill_rail.system_prompt_builder = SimpleNamespace(
        language="en",
        get_section=lambda name: sections.get(name),
        add_section=lambda section: sections.__setitem__(section.name, section),
    )

    async def no_native_before(_self, _ctx):
        return None

    async def native_prompt(rail, _ctx):
        sections[SectionName.SKILLS] = rail._build_skills_section()

    monkeypatch.setattr(SkillUseRail, "before_invoke", no_native_before)
    monkeypatch.setattr(SkillUseRail, "_sync_skill_prompt_and_attachment", native_prompt)
    observations = []
    list_tool = ListSkillTool(get_skills=skill_rail.get_skills_for_session)

    class CallbackManager:
        async def execute(self, event, ctx):
            if event is AgentCallbackEvent.BEFORE_INVOKE:
                await skill_rail.before_invoke(ctx)
            elif event is AgentCallbackEvent.AFTER_INVOKE:
                await skill_rail.after_invoke(ctx)

    class Controller:
        async def submit_round(self, _session, _query, **_kwargs):
            ctx = SimpleNamespace(extra={"run_context": _kwargs["run_context"]})
            await skill_rail.before_model_call(ctx)
            try:
                listed = await list_tool.invoke({})
                observations.append((
                    [item["name"] for item in listed.data["skills"]],
                    sections[SectionName.SKILLS].render("en"),
                ))
            finally:
                await skill_rail.after_model_call(ctx)

        async def wait_round_completion(self, *, timeout):
            return {}

    controller = Controller()
    coordinator = SimpleNamespace(reset=lambda: None)
    agent = SimpleNamespace(
        agent_callback_manager=CallbackManager(),
        _invoke_active=False,
        _deep_config=None,
        _is_resume_input=lambda _inputs: False,
        _build_interaction_next_work=lambda **_kwargs: None,
        load_state=lambda _session: SimpleNamespace(stop_condition_state=None),
        save_state=lambda *_args: None,
        clear_state=lambda _session: None,
    )

    async def prepare(_session):
        return coordinator, controller

    async def no_op(*_args):
        return None

    agent.prepare_interaction_task_loop = prepare
    agent._normalize_inputs = lambda inputs: DeepAgent._normalize_inputs(agent, inputs)
    agent._sync_expert_role_attachment = no_op
    agent._write_round_result_to_stream = no_op

    for params in (
        {
            "agent_skills_available": ["ambient"],
            "agent_skills_required": ["ambient"],
        },
        {},
    ):
        work = RoundWorkItem.user(
            request_id="same-session",
            inputs=with_scoped_skill_run_context({"query": "ping"}, params),
        )
        outcome = await DeepAgent.run_one_round(agent, work, "task", object())
        assert outcome.error_code is None

    assert observations[0][0] == ["ambient"]
    assert "## Required Skill: ambient" in observations[0][1]
    assert set(observations[1][0]) == {"ambient", "research"}
    assert "## Required Skill: ambient" not in observations[1][1]


@pytest.mark.asyncio
async def test_duplicate_reload_callback_restores_outer_scope(skill_rail, monkeypatch):
    native_before = AsyncMock()
    monkeypatch.setattr(SkillUseRail, "before_invoke", native_before)
    ctx = SimpleNamespace(
        inputs=SimpleNamespace(run_context=SimpleNamespace(extra={
            SCOPED_SKILLS_RUN_CONTEXT_KEY: {"agent_skills_available": ["ambient"]}
        })),
        extra={},
    )
    outer = bind_scoped_skills({"agent_skills_available": ["research"]})
    try:
        await skill_rail.before_invoke(ctx)
        await skill_rail.before_invoke(ctx)
        assert scoped_skill_names() == frozenset({"ambient"})
        native_before.assert_awaited_once()
        await skill_rail.after_invoke(ctx)
        await skill_rail.after_invoke(ctx)
        assert scoped_skill_names() == frozenset({"research"})
    finally:
        reset_scoped_skills(outer)


def test_retrieval_prompt_restores_full_snapshot_after_restricted_turn():
    rail = SkillRetrievalPromptRail.__new__(SkillRetrievalPromptRail)
    full = object()
    restricted = object()
    rail._frozen_prompt_snapshot = full
    rail._prompt_skillfs = SimpleNamespace(prompt_snapshot=lambda: restricted)
    token = bind_scoped_skills({"agent_skills_available": ["ambient"]})
    try:
        assert rail._prompt_snapshot() is restricted
    finally:
        reset_scoped_skills(token)
    assert rail._prompt_snapshot() is full


def test_retrieval_index_filters_and_restores_catalog(tmp_path):
    root = tmp_path / "skills"
    root.mkdir()
    _skill(root, "ambient", "---\nname: ambient\n---\nAmbient")
    _skill(root, "research", "---\nname: research\n---\nResearch")
    toolkit = SkillRetrievalToolkit(
        skill_directories=[str(root)],
        visible_skill_names=scoped_skill_names,
        artifact_root=tmp_path / "index",
    )
    assert toolkit.environment.prompt_snapshot().total_count == 2
    token = bind_scoped_skills({"agent_skills_available": ["ambient"]})
    try:
        assert toolkit.environment.prompt_snapshot().total_count == 1
    finally:
        reset_scoped_skills(token)
    token = bind_scoped_skills({"agent_skills_available": []})
    try:
        assert toolkit.environment.prompt_snapshot().total_count == 0
    finally:
        reset_scoped_skills(token)
    assert toolkit.environment.prompt_snapshot().total_count == 2
