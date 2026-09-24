# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Per-request registration tests for the two Slack Home tab tools.

The eighth Slack decision, and the only one whose surface is not a conversation.
The seven before it act in the conversation the gateway named and are mounted on
that; a Home tab belongs to one person, so what these need is that the request
names the person whose turn it is.

Two things are pinned here and nowhere else:

* a request that names nobody -- a scheduled run, a turn from another transport
  -- mounts neither tool;
* the decision is separate from the seven in both directions, so a deployment
  that withheld the posting tools still gets these, and a request carrying no
  conversation at all still gets these.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from jiuwenswarm.common.slack_history_policy import (
    METADATA_ASKER_KEY,
    METADATA_ORIGIN_KEY,
    ORIGIN_CRON_JOB,
)
from jiuwenswarm.common.slack_write_policy import (
    METADATA_WRITE_POLICY_KEY,
    WRITE_DISABLED,
)
from jiuwenswarm.server.runtime.agent_adapter import interface_deep as interface_module

_TOOL_NAMES = {"publish_slack_home_tab", "clear_slack_home_tab"}


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
        self.tools[tool.card.id] = tool


class _FakeHomeTabToolkit:
    """Stands in for ``SlackHomeTabToolkit``: two cards and no Slack call.

    The toolkit's own refusals are tested against the real class in
    ``test_slack_home_tab.py``. What is under test here is the gate above it,
    so nothing but the card names matters.
    """

    instances: list["_FakeHomeTabToolkit"] = []

    def __init__(self, *, metadata_provider: Any = None, **_: Any) -> None:
        self.metadata_provider = metadata_provider
        type(self).instances.append(self)

    def get_tools(self) -> list[Any]:
        return [
            SimpleNamespace(card=SimpleNamespace(id=f"{name}-1", name=name))
            for name in sorted(_TOOL_NAMES)
        ]


class _FakeEmptyToolkit:
    """Every other Slack toolkit, mounting nothing so it stays out of the way."""

    def __init__(self, **_: Any) -> None:
        pass

    def update_runtime_context(self, **_: Any) -> None:
        pass

    def get_tools(self) -> list[Any]:
        return []


class _FakeInstance:
    def __init__(self) -> None:
        self.ability_manager = _FakeAbilityManager()
        self.rails: list[Any] = []

    async def register_rail(self, rail: Any) -> None:
        self.rails.append(rail)


@pytest.fixture(autouse=True)
def _toolkits(monkeypatch: pytest.MonkeyPatch) -> None:
    _FakeHomeTabToolkit.instances = []
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
        interface_module, "SlackHomeTabToolkit", _FakeHomeTabToolkit
    )
    for name in (
        "SlackHistoryToolkit",
        "SlackSearchToolkit",
        "SlackReactionToolkit",
        "SlackPinToolkit",
        "SlackBookmarkToolkit",
        "SlackDirectoryToolkit",
        "SlackPostToolkit",
        "SlackCanvasToolkit",
        "SlackListToolkit",
    ):
        monkeypatch.setattr(interface_module, name, _FakeEmptyToolkit)
    monkeypatch.setattr(
        interface_module,
        "Runner",
        SimpleNamespace(resource_mgr=_FakeResourceManager()),
    )


def _make_adapter() -> tuple[Any, _FakeInstance]:
    adapter = object.__new__(interface_module.JiuWenSwarmDeepAdapter)
    instance = _FakeInstance()
    adapter._instance = instance
    adapter._send_file_toolkit = None
    # ``_update_session_tools`` reaches the session-messaging tools before any
    # Slack toolkit, and reads both of these outside a guard. Set here for the
    # same reason as the Slack pairs below: this fixture skips ``__init__``, so
    # every attribute the path under test reads has to be named.
    adapter._session_messaging_toolkit = None
    adapter._last_mode = "agent.work.normal"
    for attribute in (
        "history",
        "search",
        "reaction",
        "pin",
        "bookmark",
        "directory",
        "post",
        "home_tab",
        "canvas",
        "list",
    ):
        setattr(adapter, f"_slack_{attribute}_toolkit", None)
        setattr(adapter, f"_slack_{attribute}_tools", [])
    adapter._slack_write_rail = None
    adapter._runtime_cron_tool_context = interface_module._RuntimeCronToolContext(
        tool_scope=f"test_{id(adapter):x}",
    )
    # object.__new__ skips __init__, so every attribute the code under test
    # reads has to be set here.
    adapter._cron_tools_registered_language = None
    adapter._build_cron_tools = lambda: []
    adapter._resolve_prompt_channel = lambda _session_id: "web"
    return adapter, instance


async def _update_tools(
    adapter: Any,
    *,
    channel_id: str = "slack",
    metadata: "dict[str, Any] | None",
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


def _registered(instance: _FakeInstance) -> set[str]:
    return {getattr(card, "name", "") for card in instance.ability_manager.list()}


def _metadata(**extra: Any) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "slack_channel_id": "C0ROOM",
        "slack_channel_type": "channel",
        "slack_team_id": "T0ACME",
        METADATA_ASKER_KEY: "U0ASKER",
    }
    metadata.update(extra)
    return metadata


@pytest.mark.asyncio
async def test_a_turn_that_names_who_asked_gets_both_tools() -> None:
    adapter, instance = _make_adapter()

    await _update_tools(adapter, metadata=_metadata())

    assert _registered(instance) == _TOOL_NAMES


@pytest.mark.asyncio
async def test_a_turn_naming_nobody_gets_neither() -> None:
    """The requester is the destination, so a request without one has no tab."""
    adapter, instance = _make_adapter()
    metadata = _metadata()
    metadata.pop(METADATA_ASKER_KEY)

    await _update_tools(adapter, metadata=metadata)

    assert _registered(instance) == set()


@pytest.mark.asyncio
async def test_a_scheduled_run_gets_neither() -> None:
    """A job is started by a clock, so there is nobody whose tab it could be."""
    adapter, instance = _make_adapter()

    await _update_tools(
        adapter,
        channel_id="__cron__",
        metadata=_metadata(**{METADATA_ORIGIN_KEY: ORIGIN_CRON_JOB}),
    )

    assert _registered(instance) == set()


@pytest.mark.asyncio
async def test_a_turn_from_another_transport_gets_neither() -> None:
    adapter, instance = _make_adapter()

    await _update_tools(adapter, channel_id="web", metadata=_metadata())

    assert _registered(instance) == set()


@pytest.mark.asyncio
async def test_the_posting_word_decides_nothing_here() -> None:
    """``channels.slack.write`` governs reach, and these tools name no target.

    A deployment that has never written the key -- which resolves to
    ``disabled`` and is every deployment that predates it -- still gets both of
    these, because withholding them would be answering a question about
    somebody's own private page with a rule written about other people's
    conversations.
    """
    adapter, instance = _make_adapter()

    await _update_tools(
        adapter,
        metadata=_metadata(**{METADATA_WRITE_POLICY_KEY: WRITE_DISABLED}),
    )

    assert _registered(instance) == _TOOL_NAMES
    assert instance.rails == [], "there is no audience to ask about"


@pytest.mark.asyncio
async def test_a_turn_with_no_conversation_still_gets_both() -> None:
    """The seven tools beside these need a conversation; a Home tab is not one.

    Separate in both directions, and this is the direction that is easy to lose:
    copying the conversation condition from the tool next door would withhold a
    surface that has nothing to do with a conversation.
    """
    adapter, instance = _make_adapter()
    metadata = _metadata()
    metadata.pop("slack_channel_id")

    await _update_tools(adapter, metadata=metadata)

    assert _registered(instance) == _TOOL_NAMES


@pytest.mark.asyncio
async def test_the_toolkit_is_built_once_and_holds_a_provider_not_a_copy() -> None:
    """One toolkit for the process, reading the request through a provider.

    The cards are shared across concurrent transports, so the registration is
    one-way: withdrawing them because *this* request names nobody would take
    them from a simultaneous request that does. What keeps that safe is that
    the toolkit is handed a provider rather than one request's metadata, so
    the second turn is answered against the second turn's facts.
    """
    adapter, _instance = _make_adapter()

    await _update_tools(adapter, metadata=_metadata())
    await _update_tools(adapter, metadata=_metadata(**{METADATA_ASKER_KEY: "U0OTHER"}))

    assert len(_FakeHomeTabToolkit.instances) == 1
    provider = _FakeHomeTabToolkit.instances[0].metadata_provider
    assert provider is not None
    assert provider()[METADATA_ASKER_KEY] == "U0OTHER"
