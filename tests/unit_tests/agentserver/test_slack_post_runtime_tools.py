# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Per-request registration tests for the three Slack posting tools.

The seventh Slack decision, and the only one with a policy word of its own. The
six before it are mounted for any turn whose conversation the gateway named,
because each of them acts in that conversation and can reach nowhere else. These
can name a conversation, so ``channels.slack.write`` decides how far first.

Three things are pinned here and nowhere else:

* ``disabled`` -- the default, and what every existing deployment has -- mounts
  nothing at all, so upgrading past the release that added the key changes
  nothing;
* the rail that asks before a post widens is registered beside the tools, and
  under ``members`` the tools are not mounted without it;
* the decision is separate from the other six in both directions.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Callable

import pytest

from jiuwenswarm.common.slack_history_policy import (
    HISTORY_DISABLED,
    HISTORY_ORIGIN,
    METADATA_POLICY_KEY,
)
from jiuwenswarm.common.slack_write_policy import (
    METADATA_WRITE_POLICY_KEY,
    WRITE_DISABLED,
    WRITE_MEMBERS,
    WRITE_OPEN,
    WRITE_ORIGIN,
)
from jiuwenswarm.server.runtime.agent_adapter import interface_deep as interface_module

_TOOL_NAMES = ("post_message", "edit_message", "delete_message")


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


class _FakePostToolkit:
    """Stands in for ``SlackPostToolkit``: it mounts three cards and no gate.

    The real toolkit's own decisions -- which word lets a target be named, which
    word confirms -- are read off the request metadata and are tested against
    the real class in ``test_slack_post_message.py``. Here the metadata is real
    and only the Slack calls are absent, so ``confirms_widening`` reads the
    stamped word exactly as the real one does.
    """

    instances: list["_FakePostToolkit"] = []

    def __init__(self, *, metadata_provider: Any = None, **_: Any) -> None:
        self.metadata_provider = metadata_provider
        type(self).instances.append(self)

    def _word(self) -> str:
        provided = self.metadata_provider() if self.metadata_provider else {}
        return str((provided or {}).get(METADATA_WRITE_POLICY_KEY) or "")

    def confirms_widening(self) -> bool:
        return self._word() == WRITE_MEMBERS

    def names_a_target(self) -> bool:
        return self._word() in {WRITE_MEMBERS, WRITE_OPEN}

    def get_tools(self) -> list[Any]:
        return [
            SimpleNamespace(card=SimpleNamespace(id=f"{name}-1", name=name))
            for name in _TOOL_NAMES
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
    def __init__(self, *, rail_fails: bool = False) -> None:
        self.ability_manager = _FakeAbilityManager()
        self.rails: list[Any] = []
        self._rail_fails = rail_fails

    async def register_rail(self, rail: Any) -> None:
        if self._rail_fails:
            raise RuntimeError("this agent takes no rails")
        self.rails.append(rail)


@pytest.fixture(autouse=True)
def _toolkits(monkeypatch: pytest.MonkeyPatch) -> None:
    _FakePostToolkit.instances = []
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
    monkeypatch.setattr(interface_module, "SlackPostToolkit", _FakePostToolkit)
    for name in (
        "SlackHistoryToolkit",
        "SlackSearchToolkit",
        "SlackReactionToolkit",
        "SlackPinToolkit",
        "SlackBookmarkToolkit",
        "SlackDirectoryToolkit",
        "SlackHomeTabToolkit",
        "SlackCanvasToolkit",
        "SlackListToolkit",
    ):
        monkeypatch.setattr(interface_module, name, _FakeEmptyToolkit)
    monkeypatch.setattr(
        interface_module,
        "Runner",
        SimpleNamespace(resource_mgr=_FakeResourceManager()),
    )


def _make_adapter(*, rail_fails: bool = False) -> tuple[Any, _FakeInstance]:
    adapter = object.__new__(interface_module.JiuWenSwarmDeepAdapter)
    instance = _FakeInstance(rail_fails=rail_fails)
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


def _metadata(word: str, **extra: Any) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "slack_channel_id": "C0ROOM",
        "slack_channel_type": "channel",
        "slack_team_id": "T0ACME",
        "slack_user_id": "U0ASKER",
        METADATA_POLICY_KEY: HISTORY_ORIGIN,
        METADATA_WRITE_POLICY_KEY: word,
    }
    metadata.update(extra)
    return metadata


@pytest.mark.asyncio
async def test_the_default_word_mounts_nothing() -> None:
    """An upgrade must not hand a bot the ability to post on a model's say-so.

    Every Slack deployment written before this key existed resolves to
    ``disabled``, so this is the state of every one of them on the day they
    upgrade.
    """
    adapter, instance = _make_adapter()
    await _update_tools(adapter, metadata=_metadata(WRITE_DISABLED))

    assert _registered(instance) == set()
    assert instance.rails == []


@pytest.mark.asyncio
async def test_an_unstamped_request_mounts_nothing() -> None:
    """No word means nobody with the configuration settled this request.

    Different from settling it to ``disabled``, and mounting nothing is the only
    safe reading: a path that has not been taught to stamp the word loses the
    tools rather than gaining ungoverned ones.
    """
    adapter, instance = _make_adapter()
    metadata = _metadata(WRITE_DISABLED)
    metadata.pop(METADATA_WRITE_POLICY_KEY)
    await _update_tools(adapter, metadata=metadata)

    assert _registered(instance) == set()


@pytest.mark.asyncio
async def test_a_request_from_another_transport_mounts_nothing() -> None:
    adapter, instance = _make_adapter()
    await _update_tools(
        adapter, channel_id="web", metadata=_metadata(WRITE_OPEN)
    )

    assert _registered(instance) == set()


@pytest.mark.asyncio
async def test_origin_mounts_all_three_and_needs_no_rail() -> None:
    """The narrowest word that posts, and nothing it does can widen an audience.

    So no confirmation can arise, and the rail that asks is not registered. That
    is not an optimisation: a rail mounted where nothing can reach it is a rail
    nobody maintains.
    """
    adapter, instance = _make_adapter()
    await _update_tools(adapter, metadata=_metadata(WRITE_ORIGIN))

    assert _registered(instance) == set(_TOOL_NAMES)
    assert instance.rails == []


@pytest.mark.asyncio
async def test_open_mounts_all_three_and_needs_no_rail() -> None:
    """Naming any conversation is what the word buys, and it asks nothing."""
    adapter, instance = _make_adapter()
    await _update_tools(adapter, metadata=_metadata(WRITE_OPEN))

    assert _registered(instance) == set(_TOOL_NAMES)
    assert instance.rails == []


@pytest.mark.asyncio
async def test_members_mounts_the_rail_beside_the_tools() -> None:
    adapter, instance = _make_adapter()
    await _update_tools(adapter, metadata=_metadata(WRITE_MEMBERS))

    assert _registered(instance) == set(_TOOL_NAMES)
    assert len(instance.rails) == 1
    assert type(instance.rails[0]).__name__ == "SlackWriteConfirmationRail"


@pytest.mark.asyncio
async def test_members_mounts_nothing_when_the_rail_cannot_be_registered() -> None:
    """Keeping the word and dropping the promise is the one silent failure here.

    ``members`` promises that a post reaching people outside this conversation is
    put to whoever asked before it is sent, and the rail is the only thing that
    can put it -- a tool returns a string and has no way to stop and ask.
    Mounting the tools without it would leave the operator reading ``members`` in
    their config while posts go out unasked, with nothing on either side saying
    so.
    """
    adapter, instance = _make_adapter(rail_fails=True)
    await _update_tools(adapter, metadata=_metadata(WRITE_MEMBERS))

    assert _registered(instance) == set()
    assert instance.rails == []


@pytest.mark.asyncio
async def test_a_turn_refused_history_may_still_post() -> None:
    """Two words, two decisions, and neither reads the other.

    A deployment that lets its bot post where it is asked to has said nothing
    about whether the bot may read a conversation's scrollback, and the reverse
    is equally true.
    """
    adapter, instance = _make_adapter()
    await _update_tools(
        adapter,
        metadata=_metadata(WRITE_ORIGIN, **{METADATA_POLICY_KEY: HISTORY_DISABLED}),
    )

    assert _registered(instance) == set(_TOOL_NAMES)


@pytest.mark.asyncio
async def test_registration_is_one_way_within_a_process() -> None:
    """The cards are shared across concurrent transports.

    Withdrawing them because *this* request may not post would take them from a
    simultaneous request that may, so the mount is one-way and every tool
    refuses per request instead. The rail is registered once for the same
    reason.
    """
    adapter, instance = _make_adapter()
    await _update_tools(adapter, metadata=_metadata(WRITE_MEMBERS))
    await _update_tools(adapter, metadata=_metadata(WRITE_MEMBERS))
    assert len(instance.rails) == 1
    assert len(_FakePostToolkit.instances) == 1

    await _update_tools(adapter, channel_id="web", metadata=None)
    assert _registered(instance) == set(_TOOL_NAMES)
