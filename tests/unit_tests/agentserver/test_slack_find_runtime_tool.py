# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Per-request registration tests for ``find_by_name``.

The sixth Slack decision, and the widest. Turning a name into an id reads no
conversation, writes nothing and holds no per-event token, so the only thing it
needs is a request that came from Slack at all. These tests pin the three
consequences that follow: a turn refused the history tool still gets this one,
a scheduled run gets it where the search tool is refused one, and a request on
any other transport gets nothing whatever its metadata claims.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Callable

import pytest

from jiuwenswarm.agents.harness.common.tools.slack_directory import (
    SlackDirectoryToolkit,
)
from jiuwenswarm.agents.harness.common.tools.slack_history import SlackHistoryToolkit
from jiuwenswarm.common.slack_history_policy import (
    HISTORY_DISABLED,
    HISTORY_ORIGIN,
    METADATA_POLICY_KEY,
)
from jiuwenswarm.server.runtime.agent_adapter import interface_deep as interface_module


def _bound_func(tool: Any) -> Any:
    """The callable a tool was built around; ``LocalFunction`` keeps it private."""
    return getattr(tool, "func", None) or tool._func


def _card_name(toolkit: Any, func: Callable[..., Any]) -> str:
    """The name a toolkit gives one of its tools, read off the card that holds it.

    Never written as a literal here, for the reason the search tool's tests
    give: a renamed card must arrive through the card rather than leaving a
    stale string asserting nothing.
    """
    (tool,) = (
        candidate
        for candidate in toolkit.get_tools()
        if _bound_func(candidate) == func
    )
    return str(tool.card.name)


_FIND_TOOLKIT = SlackDirectoryToolkit()
_FIND_TOOL = _card_name(_FIND_TOOLKIT, _FIND_TOOLKIT.find_by_name)
_HISTORY_TOOLKIT = SlackHistoryToolkit(metadata={"slack_channel_id": "C1"})
_HISTORY_TOOL = _card_name(_HISTORY_TOOLKIT, _HISTORY_TOOLKIT.read_slack_conversation)


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


class _FakeToolkit:
    """Stands in for a real toolkit; both of the two here are built alike."""

    tool_name = ""
    instances: list["_FakeToolkit"] = []

    def __init__(
        self,
        *,
        metadata: dict[str, Any] | None = None,
        metadata_provider: Callable[[], dict[str, Any] | None] | None = None,
        session_id_provider: Callable[[], str | None] | None = None,
    ) -> None:
        self.metadata = dict(metadata or {})
        self.metadata_provider = metadata_provider
        self.session_id_provider = session_id_provider
        tool_id = f"{self.tool_name}-{len(type(self).instances)}"
        self.tools = [
            SimpleNamespace(card=SimpleNamespace(id=tool_id, name=self.tool_name))
        ]
        type(self).instances.append(self)

    def update_runtime_context(self, *, metadata: dict[str, Any] | None = None) -> None:
        self.metadata = dict(metadata or {})

    def get_tools(self) -> list[Any]:
        return list(self.tools)


class _FakeDirectoryToolkit(_FakeToolkit):
    tool_name = _FIND_TOOL
    instances: list["_FakeToolkit"] = []


class _FakeHistoryToolkit(_FakeToolkit):
    tool_name = _HISTORY_TOOL
    instances: list["_FakeToolkit"] = []


class _FakeNoopToolkit:
    """Stands in for the canvas toolkit, which this file has no opinion about.

    It is refreshed on every call, on a predicate of its own -- a request that
    came from Slack, with no conversation and no requester required -- so on a
    default it would register its six cards beside the one this file's
    set-equality assertions expect. Mounting zero tools keeps it out of the way
    without touching the gate that decides whether it runs.
    """

    def __init__(self, **_: Any) -> None:
        pass

    def get_tools(self) -> list[Any]:
        return []



@pytest.fixture(autouse=True)
def _toolkits(monkeypatch: pytest.MonkeyPatch) -> None:
    _FakeDirectoryToolkit.instances = []
    _FakeHistoryToolkit.instances = []
    # send_file is on for Slack in the shipped config and would build a second
    # toolkit on the way past, pulling in half the adapter for a decision this
    # file is not about.
    monkeypatch.setattr(
        interface_module,
        "get_config",
        lambda: {
            "channels": {
                "slack": {"send_file_allowed": False},
                "web": {"send_file_allowed": False},
            }
        },
    )
    monkeypatch.setattr(
        interface_module, "SlackDirectoryToolkit", _FakeDirectoryToolkit
    )
    monkeypatch.setattr(interface_module, "SlackHistoryToolkit", _FakeHistoryToolkit)
    monkeypatch.setattr(interface_module, "SlackCanvasToolkit", _FakeNoopToolkit)
    monkeypatch.setattr(interface_module, "SlackListToolkit", _FakeNoopToolkit)
    monkeypatch.setattr(
        interface_module,
        "Runner",
        SimpleNamespace(resource_mgr=_FakeResourceManager()),
    )


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
    adapter._slack_canvas_toolkit = None
    adapter._slack_canvas_tools = []
    adapter._slack_list_toolkit = None
    adapter._slack_list_tools = []
    adapter._slack_write_rail = None
    adapter._runtime_cron_tool_context = interface_module._RuntimeCronToolContext(
        tool_scope=f"test_{id(adapter):x}",
    )
    # object.__new__ skips __init__, so every attribute the code under test
    # reads has to be set here.
    adapter._cron_tools_registered_language = None
    adapter._build_cron_tools = lambda: []
    adapter._resolve_prompt_channel = lambda _session_id: "web"
    return adapter, ability_manager


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


def _registered(ability_manager: _FakeAbilityManager) -> set[str]:
    return {getattr(card, "name", "") for card in ability_manager.list()}


def _slack_metadata(**extra: Any) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "slack_channel_id": "C-ROOM",
        "slack_channel_type": "channel",
        "slack_team_id": "T-ACME",
        METADATA_POLICY_KEY: HISTORY_ORIGIN,
    }
    metadata.update(extra)
    return metadata


@pytest.mark.asyncio
async def test_an_inbound_slack_turn_gets_the_lookup() -> None:
    adapter, ability_manager = _make_adapter()

    await _update_tools(adapter, channel_id="slack", metadata=_slack_metadata())

    assert _FIND_TOOL in _registered(ability_manager)


@pytest.mark.asyncio
async def test_a_turn_refused_history_still_gets_the_lookup() -> None:
    """The decisions are separate, and this is the one that proves it.

    ``disabled`` is the operator saying no conversation's record may be read
    out. A name and an id are not a record, so the lookup stands while the
    history tool does not.
    """
    adapter, ability_manager = _make_adapter()

    await _update_tools(
        adapter,
        channel_id="slack",
        metadata=_slack_metadata(**{METADATA_POLICY_KEY: HISTORY_DISABLED}),
    )

    registered = _registered(ability_manager)
    assert _FIND_TOOL in registered
    assert _HISTORY_TOOL not in registered


@pytest.mark.asyncio
async def test_a_slack_originated_cron_run_gets_the_lookup() -> None:
    """Unlike search, which needs a token only an inbound event carries."""
    adapter, ability_manager = _make_adapter()

    await _update_tools(
        adapter,
        channel_id=interface_module.CRON_REQUEST_CHANNEL_ID,
        metadata=_slack_metadata(
            **{
                interface_module.SLACK_HISTORY_ORIGIN_KEY: (
                    interface_module.SLACK_HISTORY_ORIGIN_CRON_JOB
                )
            }
        ),
    )

    assert _FIND_TOOL in _registered(ability_manager)


@pytest.mark.asyncio
async def test_a_cron_run_the_scheduler_did_not_mark_gets_nothing() -> None:
    """A stray Slack-looking key on a cron request grants nothing."""
    adapter, ability_manager = _make_adapter()

    await _update_tools(
        adapter,
        channel_id=interface_module.CRON_REQUEST_CHANNEL_ID,
        metadata=_slack_metadata(),
    )

    assert _FIND_TOOL not in _registered(ability_manager)


@pytest.mark.asyncio
async def test_a_web_turn_gets_nothing_however_its_metadata_reads() -> None:
    """The transport decides. A web request carrying Slack keys is still web."""
    adapter, ability_manager = _make_adapter()

    await _update_tools(adapter, channel_id="web", metadata=_slack_metadata())

    assert _FIND_TOOL not in _registered(ability_manager)


@pytest.mark.asyncio
async def test_the_toolkit_is_built_once_and_reads_the_live_request() -> None:
    """One toolkit for the life of the process, answering per request.

    The card is shared across concurrent transports, so it is registered once
    and never withdrawn; what keeps a later non-Slack request from using it is
    the provider, which is read at invocation time rather than captured.
    """
    adapter, _ability_manager = _make_adapter()

    await _update_tools(adapter, channel_id="slack", metadata=_slack_metadata())
    await _update_tools(
        adapter,
        channel_id="slack",
        metadata=_slack_metadata(slack_channel_id="C-OTHER"),
    )

    assert len(_FakeDirectoryToolkit.instances) == 1
    provider = _FakeDirectoryToolkit.instances[0].metadata_provider
    assert callable(provider)
    assert provider()["slack_channel_id"] == "C-OTHER"


@pytest.mark.asyncio
async def test_the_provider_fails_closed_once_the_request_is_not_a_slack_one() -> None:
    adapter, _ability_manager = _make_adapter()

    await _update_tools(adapter, channel_id="slack", metadata=_slack_metadata())
    provider = _FakeDirectoryToolkit.instances[0].metadata_provider
    await _update_tools(adapter, channel_id="web", metadata=_slack_metadata())

    assert provider() == {}


@pytest.mark.asyncio
async def test_the_card_is_restored_if_something_removed_it() -> None:
    adapter, ability_manager = _make_adapter()

    await _update_tools(adapter, channel_id="slack", metadata=_slack_metadata())
    ability_manager.remove(_FIND_TOOL)
    await _update_tools(adapter, channel_id="slack", metadata=_slack_metadata())

    assert _FIND_TOOL in _registered(ability_manager)
    assert len(_FakeDirectoryToolkit.instances) == 1
