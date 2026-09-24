# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for how a turn moves the cwd of an already-running session.

The controller's TaskScheduler is started once, right after the session's cwd
seed, and every round, tool call and subagent below it holds a *reference* to
that CwdState through its copied Context. Only an in-place mutation reaches
them; rebinding the ContextVar with ``init_cwd`` is visible to the calling task
alone -- which is exactly the inter-agent isolation Core's ``cwd`` module
documents, and exactly why it is the wrong write for a per-turn move.
"""

from __future__ import annotations

import asyncio
import contextvars

import pytest
from openjiuwen.core.sys_operation.cwd import get_cwd, get_project_root, get_workspace

from jiuwenswarm.server.runtime.agent_adapter.interface_deep import JiuWenSwarmDeepAdapter


def _make_adapter(session_id: str, workspace_dir: str) -> JiuWenSwarmDeepAdapter:
    """Build a bare adapter holding only the attributes the cwd seed reads."""
    adapter = object.__new__(JiuWenSwarmDeepAdapter)
    adapter._parent_session_id = session_id
    adapter._project_dir = None
    adapter._workspace_dir = workspace_dir
    return adapter


@pytest.mark.asyncio
async def test_turn_cwd_reaches_a_task_created_before_it(tmp_path):
    """A task started at session setup must observe a later turn's cwd.

    This mirrors the real ordering: the TaskScheduler is created inside
    ``start_interaction``, immediately after the construction seed, and every
    round and tool call runs beneath it. Asserting only that the reseed sets the
    right value would also pass for a reseed that rebinds the ContextVar, which
    no task created earlier can ever see.
    """
    session_dir = tmp_path / "session"
    session_dir.mkdir()
    moved_dir = tmp_path / "moved"
    moved_dir.mkdir()
    adapter = _make_adapter("sess", str(session_dir))

    adapter._seed_runtime_cwd(str(session_dir), workspace=str(session_dir))

    reseeded = asyncio.Event()
    observed: dict[str, str | None] = {}

    async def scheduler() -> None:
        observed["at_start"] = get_cwd()
        await reseeded.wait()
        observed["after_reseed"] = get_cwd()
        observed["project_root"] = get_project_root()
        observed["workspace"] = get_workspace()

    scheduler_task = asyncio.create_task(scheduler())
    await asyncio.sleep(0)

    adapter._reseed_runtime_cwd(str(moved_dir), workspace=str(session_dir))
    reseeded.set()
    await scheduler_task

    assert observed["at_start"] == str(session_dir)
    assert observed["after_reseed"] == str(moved_dir), (
        "a turn's cwd must mutate the CwdState the scheduler task already holds"
    )
    # The other two layers stay where the session put them: Core documents the
    # project root as never changing mid-session, and the workspace anchors
    # fs_operation's sandbox for the whole session.
    assert observed["project_root"] == str(session_dir)
    assert observed["workspace"] == str(session_dir)


@pytest.mark.asyncio
async def test_reseed_falls_back_to_a_full_seed_without_inherited_state(tmp_path):
    """A turn whose context inherited no CwdState has nothing to mutate.

    Every turn arrives on a freshly created Task, so this is the common case for
    turns after the one that built the agent. Such a turn must still end up with
    all three layers bound, as the replace seed always gave it.
    """
    session_dir = tmp_path / "session"
    session_dir.mkdir()
    adapter = _make_adapter("sess", str(session_dir))

    async def turn() -> tuple[str, str | None]:
        adapter._reseed_runtime_cwd(str(session_dir), workspace=str(session_dir))
        return get_cwd(), get_workspace()

    # An empty Context is the honest model of a turn that arrives on a task the
    # agent's own context never reached, and it keeps the assertion independent
    # of any CwdState an earlier test left bound in this one.
    cwd, workspace = await asyncio.create_task(turn(), context=contextvars.Context())

    assert cwd == str(session_dir)
    assert workspace == str(session_dir), "the fallback must bind the workspace layer too"


@pytest.mark.asyncio
async def test_subagent_startup_keeps_replace_semantics(tmp_path):
    """``_seed_runtime_cwd`` must not leak a child agent's cwd into its parent.

    Replacing the binding *is* the inter-agent isolation in Core's cwd model, so
    the subagent and team-member startup paths must keep using the seed, never
    the in-place reseed.
    """
    parent_dir = tmp_path / "parent"
    parent_dir.mkdir()
    child_dir = tmp_path / "child"
    child_dir.mkdir()
    parent = _make_adapter("sess", str(parent_dir))
    child = _make_adapter("sess_child", str(child_dir))

    parent._seed_runtime_cwd(str(parent_dir), workspace=str(parent_dir))

    async def child_agent() -> str:
        child._seed_runtime_cwd(str(child_dir), workspace=str(child_dir))
        return get_cwd()

    child_cwd = await asyncio.create_task(child_agent())

    assert child_cwd == str(child_dir)
    assert get_cwd() == str(parent_dir), "a child agent's seed must not move the parent"
