# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Per-request registration tests for the Slack history tool."""

from __future__ import annotations

import asyncio
import ast
import inspect
import textwrap
from contextvars import Context
from types import SimpleNamespace
from typing import Any, Callable

import pytest

from jiuwenswarm.server.runtime.agent_adapter import interface_deep as interface_module


# Both tools the history toolkit mounts. They share one availability
# predicate, one toolkit instance and one registration block, so "the Slack
# history tool is mounted" is a statement about the pair.
_TOOL_NAMES = ("read_slack_conversation", "download_slack_file")


class _FakeAbilityManager:
    def __init__(self) -> None:
        self._cards: dict[str, Any] = {}

    def list(self) -> list[Any]:
        return list(self._cards.values())

    def add(self, card: Any) -> None:
        self._cards[card.name] = card

    def remove(self, name: str) -> None:
        self._cards.pop(name, None)


class _FakeResourceManager:
    def __init__(self) -> None:
        self.tools: dict[str, Any] = {}

    def add_tool(self, tool: Any) -> None:
        if tool.card.id in self.tools:
            raise AssertionError(f"duplicate tool id: {tool.card.id}")
        self.tools[tool.card.id] = tool


class _FakeSlackHistoryToolkit:
    instances: list["_FakeSlackHistoryToolkit"] = []

    def __init__(
        self,
        *,
        metadata: dict[str, Any] | None = None,
        metadata_provider: Callable[[], dict[str, Any] | None] | None = None,
        session_id: str | None = None,
        session_id_provider: Callable[[], str | None] | None = None,
    ) -> None:
        self.metadata = dict(metadata or {})
        self.metadata_provider = metadata_provider
        self.session_id = session_id
        self.session_id_provider = session_id_provider
        self.updates: list[dict[str, Any]] = []
        self.tools = []
        for tool_name in _TOOL_NAMES:
            tool_id = f"{tool_name}-{len(self.instances)}"
            self.tools.append(
                SimpleNamespace(card=SimpleNamespace(id=tool_id, name=tool_name))
            )
        self.instances.append(self)

    def update_runtime_context(
        self,
        *,
        metadata: dict[str, Any] | None = None,
        session_id: str | None = None,
    ) -> None:
        self.metadata = dict(metadata or {})
        self.session_id = session_id
        self.updates.append(self.metadata)

    def get_tools(self) -> list[Any]:
        return list(self.tools)


class _FakeSlackNoopToolkit:
    """Stands in for the four toolkits this file has no opinion about.

    ``_update_session_tools`` refreshes every Slack toolkit on each call (see
    ``_refresh_slack_reaction_runtime_tool``'s docstring: history, search,
    reaction, pin, bookmarks, the name lookup and the Home tab pair are
    independent decisions), so the real ``SlackReactionToolkit``/
    ``SlackPinToolkit``/``SlackBookmarkToolkit``/``SlackDirectoryToolkit``/
    ``SlackHomeTabToolkit`` would otherwise register ``react_to_message``/
    ``pin_message``/``write_bookmark``/``read_bookmarks``/``find_by_name``/
    ``publish_slack_home_tab``/``clear_slack_home_tab`` alongside the history pair every
    time this file's metadata names a Slack conversation -- which is most of
    these tests, since that is exactly what the history gate also requires. This file's assertions are
    about the history pair only, so the other toolkits are faked out here the
    way ``SlackHistoryToolkit`` itself is, rather than left real: mounting zero
    tools keeps them out of the way without touching the gate that decides
    whether *they* run.
    """

    def __init__(self, *, metadata_provider: Any = None, **_: Any) -> None:
        self.metadata_provider = metadata_provider

    def get_tools(self) -> list[Any]:
        return []


def _make_adapter() -> tuple[Any, _FakeAbilityManager]:
    adapter = object.__new__(interface_module.JiuWenSwarmDeepAdapter)
    ability_manager = _FakeAbilityManager()
    adapter._instance = SimpleNamespace(ability_manager=ability_manager)
    adapter._send_file_toolkit = None
    # ``_update_session_tools`` reaches the session-messaging tools before any
    # Slack toolkit, and reads both of these outside a guard. Set here for the
    # same reason as the Slack pairs below: this fixture skips ``__init__``, so
    # every attribute the path under test reads has to be named.
    adapter._session_messaging_toolkit = None
    adapter._last_mode = "agent.work.normal"
    # _update_session_tools refreshes all five Slack toolkits unconditionally
    # before this file's own gate is even reached (history's early return is
    # last, so search/reaction/pin/bookmarks run regardless of what this file
    # is testing). Every one of their toolkit/tools pairs is set here, mirroring
    # __init__'s starting values, even though this file's metadata only ever
    # satisfies history's own predicate -- see
    # test_make_adapter_fixture_covers_every_slack_toolkit_attribute below,
    # which fails by name instead of by AttributeError the next time __init__
    # gains one this fixture does not know about.
    adapter._slack_history_toolkit = None
    adapter._slack_history_tools = []
    adapter._slack_search_toolkit = None
    adapter._slack_search_tools = []
    adapter._slack_reaction_toolkit = None
    adapter._slack_reaction_tools = []
    adapter._slack_pin_toolkit = None
    adapter._slack_pin_tools = []
    adapter._slack_bookmark_toolkit = None
    adapter._slack_bookmark_tools = []
    adapter._slack_directory_toolkit = None
    adapter._slack_directory_tools = []
    adapter._slack_post_toolkit = None
    adapter._slack_post_tools = []
    adapter._slack_home_tab_toolkit = None
    adapter._slack_home_tab_tools = []
    adapter._slack_canvas_toolkit = None
    adapter._slack_canvas_tools = []
    adapter._slack_list_toolkit = None
    adapter._slack_list_tools = []
    adapter._slack_write_rail = None
    adapter._runtime_cron_tool_context = interface_module._RuntimeCronToolContext(
        tool_scope=f"test_{id(adapter):x}",
    )
    # object.__new__ skips __init__, so every attribute the code under test reads has
    # to be set here. _update_session_tools -> _ensure_cron_tools_registered reads this
    # one outside its try block, so leaving it out fails the whole module rather than
    # the one code path. Same reason test_deep_adapter_cron_tool_registration sets it.
    adapter._cron_tools_registered_language = None
    adapter._build_cron_tools = lambda: []
    adapter._resolve_prompt_channel = lambda _session_id: "web"
    return adapter, ability_manager


def _slack_toolkit_attribute_names() -> set[str]:
    """Every ``self._slack_*`` attribute ``__init__`` gives a starting value.

    Hand-listing exactly what ``_make_adapter`` sets is deliberate, not an
    oversight: mirroring ``__init__`` wholesale would drag in dozens of
    attributes this file has no opinion about, and this fixture exists to
    stand in for one narrow code path, not the whole adapter. What can be
    automatic is the *complaint* when the fixture and ``__init__`` drift
    apart, so this reads ``__init__``'s own source for every
    ``self._slack_...`` assignment. A future toolkit added there is then
    caught by name in
    ``test_make_adapter_fixture_covers_every_slack_toolkit_attribute``,
    instead of surfacing three calls deep as an opaque ``AttributeError``
    the way ``_slack_reaction_toolkit`` and ``_slack_pin_toolkit`` did here.
    """
    source = textwrap.dedent(
        inspect.getsource(interface_module.JiuWenSwarmDeepAdapter.__init__)
    )
    names: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.AnnAssign):
            targets = [node.target]
        elif isinstance(node, ast.Assign):
            targets = node.targets
        else:
            continue
        for target in targets:
            if (
                isinstance(target, ast.Attribute)
                and isinstance(target.value, ast.Name)
                and target.value.id == "self"
                and target.attr.startswith("_slack_")
            ):
                names.add(target.attr)
    return names


def test_make_adapter_fixture_covers_every_slack_toolkit_attribute() -> None:
    adapter, _ability_manager = _make_adapter()

    missing = {
        name for name in _slack_toolkit_attribute_names() if not hasattr(adapter, name)
    }
    assert not missing, (
        "JiuWenSwarmDeepAdapter.__init__ now sets _slack_* attribute(s) "
        f"{sorted(missing)} that _make_adapter() does not -- add them there "
        "(None for a *_toolkit, [] for a *_tools), matching the existing "
        "ones, or a toolkit added after this test will fail with an "
        "AttributeError far from its actual cause instead of here."
    )


async def _update_tools(
    adapter: Any,
    *,
    channel_id: str,
    metadata: dict[str, Any] | None,
) -> None:
    channel_token = interface_module._CRON_TOOL_CHANNEL_ID.set(channel_id)
    metadata_token = interface_module._CRON_TOOL_METADATA.set(metadata)
    bound_token = interface_module._CRON_TOOL_BOUND.set(True)
    try:
        adapter._runtime_cron_tool_context.remember_current_binding()
        await adapter._update_session_tools(
            session_id="session-1",
            request_id="request-1",
            channel_id=channel_id,
        )
    finally:
        interface_module._CRON_TOOL_BOUND.reset(bound_token)
        interface_module._CRON_TOOL_METADATA.reset(metadata_token)
        interface_module._CRON_TOOL_CHANNEL_ID.reset(channel_token)


async def _read_provider(
    toolkit: _FakeSlackHistoryToolkit,
    *,
    channel_id: str,
    metadata: dict[str, Any] | None,
) -> dict[str, Any] | None:
    channel_token = interface_module._CRON_TOOL_CHANNEL_ID.set(channel_id)
    metadata_token = interface_module._CRON_TOOL_METADATA.set(metadata)
    bound_token = interface_module._CRON_TOOL_BOUND.set(True)
    try:
        await asyncio.sleep(0)
        assert toolkit.metadata_provider is not None
        return toolkit.metadata_provider()
    finally:
        interface_module._CRON_TOOL_BOUND.reset(bound_token)
        interface_module._CRON_TOOL_METADATA.reset(metadata_token)
        interface_module._CRON_TOOL_CHANNEL_ID.reset(channel_token)


@pytest.fixture(autouse=True)
def _patch_runtime(monkeypatch: pytest.MonkeyPatch) -> _FakeResourceManager:
    _FakeSlackHistoryToolkit.instances.clear()
    resource_manager = _FakeResourceManager()
    monkeypatch.setattr(
        interface_module, "SlackHistoryToolkit", _FakeSlackHistoryToolkit
    )
    # Reaction, pin, bookmarks and the name lookup are refreshed on every
    # call alongside history (see _FakeSlackNoopToolkit above); faked out so
    # they cannot add cards this file's set-equality assertions do not expect.
    monkeypatch.setattr(
        interface_module, "SlackReactionToolkit", _FakeSlackNoopToolkit
    )
    monkeypatch.setattr(interface_module, "SlackPinToolkit", _FakeSlackNoopToolkit)
    monkeypatch.setattr(
        interface_module, "SlackBookmarkToolkit", _FakeSlackNoopToolkit
    )
    monkeypatch.setattr(
        interface_module, "SlackDirectoryToolkit", _FakeSlackNoopToolkit
    )
    monkeypatch.setattr(
        interface_module, "SlackHomeTabToolkit", _FakeSlackNoopToolkit
    )
    monkeypatch.setattr(
        interface_module, "SlackCanvasToolkit", _FakeSlackNoopToolkit
    )
    monkeypatch.setattr(
        interface_module, "SlackListToolkit", _FakeSlackNoopToolkit
    )
    monkeypatch.setattr(
        interface_module,
        "Runner",
        SimpleNamespace(resource_mgr=resource_manager),
    )
    monkeypatch.setattr(
        interface_module,
        "get_config",
        lambda: {
            "channels": {
                "slack": {
                    "send_file_allowed": False,
                    "history": "origin",
                },
                "web": {"send_file_allowed": False},
            }
        },
    )
    return resource_manager


@pytest.mark.asyncio
async def test_slack_history_tool_uses_context_local_metadata_for_every_request() -> (
    None
):
    adapter, abilities = _make_adapter()

    await _update_tools(
        adapter,
        channel_id="slack",
        metadata={
            "slack_channel_id": "C-ONE",
            "slack_user_id": "U-ONE",
            "slack_history_policy": "origin",
        },
    )

    assert {card.name for card in abilities.list()} == set(_TOOL_NAMES)
    assert len(_FakeSlackHistoryToolkit.instances) == 1
    toolkit = _FakeSlackHistoryToolkit.instances[0]
    assert toolkit.metadata == {}
    assert toolkit.updates == []

    first, second, non_slack = await asyncio.gather(
        _read_provider(
            toolkit,
            channel_id="slack",
            metadata={
                "slack_channel_id": "C-ONE",
                "slack_user_id": "U-ONE",
                "slack_history_policy": "origin",
            },
        ),
        _read_provider(
            toolkit,
            channel_id="slack",
            metadata={
                "slack_channel_id": "C-TWO",
                "slack_user_id": "U-TWO",
                "slack_history_policy": "origin",
            },
        ),
        _read_provider(
            toolkit,
            channel_id="web",
            metadata={"slack_channel_id": "C-HIDDEN"},
        ),
    )

    assert first == {
        "slack_channel_id": "C-ONE",
        "slack_user_id": "U-ONE",
        "slack_history_policy": "origin",
    }
    assert second == {
        "slack_channel_id": "C-TWO",
        "slack_user_id": "U-TWO",
        "slack_history_policy": "origin",
    }
    assert non_slack == {}


@pytest.mark.asyncio
async def test_slack_history_tool_keeps_trusted_metadata_across_worker_boundary() -> (
    None
):
    adapter, _abilities = _make_adapter()
    metadata = {
        "slack_channel_id": "C-ONE",
        "slack_channel_type": "channel",
        "slack_user_id": "U-ONE",
        "slack_history_policy": "origin",
    }

    await _update_tools(adapter, channel_id="slack", metadata=metadata)

    toolkit = _FakeSlackHistoryToolkit.instances[0]
    assert toolkit.metadata_provider is not None

    async def _invoke_from_worker() -> dict[str, Any] | None:
        await asyncio.sleep(0)
        return toolkit.metadata_provider()

    provided = await Context().run(asyncio.create_task, _invoke_from_worker())

    assert provided == metadata


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("channel_id", "metadata"),
    [
        ("web", {"slack_channel_id": "C-ONE"}),
        ("slack", {"slack_channel_id": "C-NOT-ALLOWED"}),
        ("slack", {}),
        ("slack", None),
    ],
)
async def test_slack_history_tool_is_not_registered_outside_current_slack_channel(
    channel_id: str,
    metadata: dict[str, Any] | None,
) -> None:
    adapter, abilities = _make_adapter()

    await _update_tools(adapter, channel_id=channel_id, metadata=metadata)

    assert abilities.list() == []
    assert _FakeSlackHistoryToolkit.instances == []


@pytest.mark.asyncio
async def test_registered_slack_history_tool_stays_stable_across_transports() -> None:
    adapter, abilities = _make_adapter()
    abilities.add(SimpleNamespace(id="unrelated-tool", name="unrelated_tool"))

    await _update_tools(
        adapter,
        channel_id="slack",
        metadata={
            "slack_channel_id": "C-ONE",
            "slack_history_policy": "origin",
        },
    )
    await _update_tools(
        adapter,
        channel_id="web",
        metadata={"slack_channel_id": "C-ONE"},
    )
    assert {card.name for card in abilities.list()} == {"unrelated_tool"} | set(_TOOL_NAMES)
    toolkit = _FakeSlackHistoryToolkit.instances[0]
    assert toolkit.metadata_provider is not None
    assert toolkit.metadata_provider() == {}

    await _update_tools(
        adapter,
        channel_id="slack",
        metadata={
            "slack_channel_id": "C-THREE",
            "slack_history_policy": "origin",
        },
    )

    assert {card.name for card in abilities.list()} == {"unrelated_tool"} | set(_TOOL_NAMES)
    assert len(_FakeSlackHistoryToolkit.instances) == 1
    assert _FakeSlackHistoryToolkit.instances[0].updates == []
    assert toolkit.metadata_provider() == {
        "slack_channel_id": "C-THREE",
        "slack_history_policy": "origin",
    }


@pytest.mark.asyncio
async def test_slack_history_tool_is_isolated_between_session_adapters(
    _patch_runtime: _FakeResourceManager,
) -> None:
    first_adapter, first_abilities = _make_adapter()
    second_adapter, second_abilities = _make_adapter()

    await _update_tools(
        first_adapter,
        channel_id="slack",
        metadata={
            "slack_channel_id": "C-ONE",
            "slack_history_policy": "origin",
        },
    )
    await _update_tools(
        second_adapter,
        channel_id="slack",
        metadata={
            "slack_channel_id": "C-TWO",
            "slack_history_policy": "origin",
        },
    )

    # Two adapters, each mounting the toolkit's whole pair, and no sharing.
    assert len(_patch_runtime.tools) == 2 * len(_TOOL_NAMES)
    assert {card.name for card in first_abilities.list()} == set(_TOOL_NAMES)
    assert {card.name for card in second_abilities.list()} == set(_TOOL_NAMES)

    await _update_tools(
        first_adapter,
        channel_id="web",
        metadata={"slack_channel_id": "C-ONE"},
    )

    assert {card.name for card in first_abilities.list()} == set(_TOOL_NAMES)
    assert {card.name for card in second_abilities.list()} == set(_TOOL_NAMES)
    assert _FakeSlackHistoryToolkit.instances[0].updates == []
    assert _FakeSlackHistoryToolkit.instances[1].updates == []
    first_provider = _FakeSlackHistoryToolkit.instances[0].metadata_provider
    second_provider = _FakeSlackHistoryToolkit.instances[1].metadata_provider
    assert first_provider is not None
    assert second_provider is not None
    assert first_provider() == {}
    assert second_provider() == {
        "slack_channel_id": "C-TWO",
        "slack_history_policy": "origin",
    }


# ── Cron turns ───────────────────────────────────────────────────────────────
#
# A cron run arrives on "__cron__" and has no Slack channel of its own. The
# scheduler stamps the conversation the job was created in, and marks the stamp
# as its own doing; the runtime honours that mark and nothing else, so the model
# still cannot say which channel is read and a non-Slack cron job gains nothing.

_CRON_CHANNEL_ID = "__cron__"


def _cron_metadata(**overrides: Any) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "cron": {"job_id": "job-1", "run_id": "job-1:1"},
        "slack_channel_id": "C-ONE",
        "slack_channel_type": "channel",
        "slack_history_policy": "origin",
        "slack_history_origin": "cron_job",
    }
    metadata.update(overrides)
    return metadata


@pytest.mark.asyncio
async def test_slack_history_tool_mounts_for_a_slack_originated_cron_run() -> None:
    adapter, abilities = _make_adapter()
    metadata = _cron_metadata(slack_thread_ts="1712345678.000100")

    await _update_tools(adapter, channel_id=_CRON_CHANNEL_ID, metadata=metadata)

    assert {card.name for card in abilities.list()} == set(_TOOL_NAMES)
    toolkit = _FakeSlackHistoryToolkit.instances[0]
    assert toolkit.metadata_provider is not None
    provided = await _read_provider(
        toolkit, channel_id=_CRON_CHANNEL_ID, metadata=metadata
    )
    assert provided is not None
    assert provided["slack_channel_id"] == "C-ONE"
    assert provided["slack_thread_ts"] == "1712345678.000100"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("metadata", "reason"),
    [
        (
            _cron_metadata(slack_history_policy="disabled"),
            "the conversation's settled policy reads no history",
        ),
        (
            # The security case: a cron job whose Slack-looking session id was
            # never proven has no origin mark, because the scheduler never
            # stamped one. Slack-shaped metadata that the scheduler did not put
            # there is not a Slack context.
            {
                "cron": {"job_id": "job-1", "run_id": "job-1:1"},
                "slack_channel_id": "C-PRIVATE",
                "slack_channel_type": "channel",
                "slack_history_policy": "origin",
            },
            "unproven Slack session leaves no origin mark",
        ),
        (
            {"cron": {"job_id": "job-1", "run_id": "job-1:1"}},
            "non-Slack cron job",
        ),
        (None, "no metadata at all"),
    ],
    ids=["not-allowlisted", "unproven-session", "non-slack-cron", "no-metadata"],
)
async def test_slack_history_tool_does_not_mount_for_other_cron_runs(
    metadata: dict[str, Any] | None,
    reason: str,
) -> None:
    adapter, abilities = _make_adapter()

    await _update_tools(adapter, channel_id=_CRON_CHANNEL_ID, metadata=metadata)

    assert abilities.list() == [], reason
    assert _FakeSlackHistoryToolkit.instances == [], reason


@pytest.mark.asyncio
async def test_cron_origin_mark_does_not_travel_to_other_channels() -> None:
    """The mark answers "which cron run is this", not "trust this metadata"."""
    adapter, abilities = _make_adapter()

    await _update_tools(adapter, channel_id="web", metadata=_cron_metadata())

    assert abilities.list() == []
    assert _FakeSlackHistoryToolkit.instances == []
