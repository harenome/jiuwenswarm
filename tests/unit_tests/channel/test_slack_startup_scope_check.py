# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.

"""What a channel does at startup about the scopes its install was granted.

``start`` used to verify only that both tokens were non-empty, so an app
installed from a narrower manifest than it needs started cleanly and then failed
one call at a time, for as long as it ran, with nothing at the top of the log
saying why. Slack answers **every** Web API call with ``x-oauth-scopes``, and
``auth.test`` already runs at startup, so the whole of what the install holds is
in reach for no extra call.

Three behaviours are pinned here.

* A missing Tier 0 grant refuses to start that channel -- ``logger.error`` and
  ``return``, following the token checks above it. Not an exception and not an
  exit: the gateway serves other connectors and other workspaces, and with
  several Slack workspaces configured the degrade has to be per workspace.
* A missing Tier 1 or Tier 2 grant is reported once and the channel starts. The
  line names what each absent scope was for, which is the half an operator can
  weigh.
* A header this connector could not read starts the channel. Refusing over a
  header rather than over a grant would take a working install down for a reason
  that is not about the install.

The tier table is ``jiuwenswarm.common.slack_scope_policy``, the same one the
shipped manifests are rendered from, and these tests read it rather than
restating it -- a scope moved between tiers there must not need an edit here.

Log lines are captured by replacing the module logger's methods rather than
through ``caplog``: this project's loggers do not propagate.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from jiuwenswarm.common.slack_scope_policy import (
    TIER_TOOLS,
    scopes_for_tier,
)
from jiuwenswarm.gateway.channel_manager.base import RobotMessageRouter
from jiuwenswarm.gateway.channel_manager.im_platforms.slack import slack_connect
from jiuwenswarm.gateway.channel_manager.im_platforms.slack.slack_connect import (
    SlackChannel,
    SlackChannelConfig,
    slack_granted_scopes,
    slack_required_scopes,
)

_EVERYTHING = frozenset(scopes_for_tier(TIER_TOOLS))


class _AuthResponse(dict):
    """What the SDK hands back: a mapping for the body, headers beside it."""

    def __init__(self, headers: Any, **body: Any) -> None:
        super().__init__(**body)
        self.headers = headers


class _CIHeaders(dict):
    """aiohttp's headers are case-insensitive; a plain dict is not.

    Both reach this code -- the async SDK passes aiohttp's own mapping straight
    through, while a retry path or a recorded fixture passes a plain dict -- so
    the reader has to cope with either. This stands in for the first.
    """

    def items(self):  # noqa: D102
        return [(key.upper(), value) for key, value in super().items()]


def _client(granted: "frozenset[str] | None", *, header: bool = True) -> Any:
    class _Client:
        auth_test_calls = 0

        async def auth_test(self) -> Any:
            type(self).auth_test_calls += 1
            if not header:
                return _AuthResponse(None, user_id="U0BOT00001", team_id="T0TEAM0001")
            value = ",".join(sorted(granted or ()))
            return _AuthResponse(
                {"content-type": "application/json", "x-oauth-scopes": value},
                user_id="U0BOT00001",
                team_id="T0TEAM0001",
            )

    return _Client()


@pytest.fixture
def logged(monkeypatch) -> dict[str, list[str]]:
    recorded: dict[str, list[str]] = {"error": [], "warning": [], "info": []}

    def record(level: str):
        def write(message: str, *args: Any, **kwargs: Any) -> None:
            recorded[level].append(message % args if args else message)

        return write

    for level in recorded:
        monkeypatch.setattr(slack_connect.logger, level, record(level))
    return recorded


async def _started(channel: SlackChannel, client: Any) -> None:
    channel._client = client
    await channel._load_bot_user_id()


def _channel(**config: Any) -> SlackChannel:
    return SlackChannel(
        SlackChannelConfig(
            enabled=True, bot_token="xoxb-test", app_token="xapp-test", **config
        ),
        RobotMessageRouter(),
    )


# --------------------------------------------------------------------------
# Reading the header
# --------------------------------------------------------------------------


def test_the_granted_scopes_come_off_the_header():
    headers = {"x-oauth-scopes": "chat:write,app_mentions:read, im:history"}
    assert slack_granted_scopes(headers) == frozenset(
        {"chat:write", "app_mentions:read", "im:history"}
    )


def test_the_header_is_read_whatever_its_casing():
    assert slack_granted_scopes(_CIHeaders({"x-oauth-scopes": "chat:write"})) == (
        frozenset({"chat:write"})
    )


def test_a_header_that_is_not_there_is_not_an_empty_grant():
    """``None`` and ``frozenset()`` mean different things and decide differently.

    One is Slack saying this token holds nothing; the other is this connector
    being unable to find out. The check starts on the second and refuses on the
    first, so conflating them would either kill a healthy channel or wave a
    broken install through.
    """
    assert slack_granted_scopes(None) is None
    assert slack_granted_scopes({}) is None
    assert slack_granted_scopes({"content-type": "application/json"}) is None
    # Present but blank is read as "could not find out" too: a proxy that drops
    # the value while keeping the key is indistinguishable here from Slack
    # saying "none", and only one of those two mistakes costs a live channel.
    assert slack_granted_scopes({"x-oauth-scopes": ""}) is None
    assert slack_granted_scopes({"x-oauth-scopes": " , "}) is None


def test_the_refusal_set_is_tier_zero_less_what_the_manifest_offers_to_delete():
    """Derived from the table, so a scope added there is covered with no edit.

    ``NARROWINGS`` is rendered into the shipped manifests beside the scope it
    describes, so refusing to start over one would refuse a configuration this
    repository hands an operator in the file they paste into Slack.
    """
    from jiuwenswarm.common.slack_scope_policy import (
        NARROWINGS,
        TIER_CORE,
        scopes_for_tier as for_tier,
    )

    required = slack_required_scopes()
    assert required == frozenset(for_tier(TIER_CORE)) - frozenset(NARROWINGS)
    # The two the manifest invites an operator to delete are not refusals.
    assert "groups:history" not in required
    assert "mpim:history" not in required
    # And what is left has no supported way to be absent.
    assert "chat:write" in required
    assert "app_mentions:read" in required


# --------------------------------------------------------------------------
# What the check decides
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_complete_install_starts_and_says_so(logged) -> None:
    channel = _channel()
    await _started(channel, _client(_EVERYTHING))

    assert channel._scopes_permit_start() is True
    assert logged["error"] == []
    assert logged["warning"] == []
    assert any("every scope" in line for line in logged["info"])


@pytest.mark.asyncio
async def test_a_missing_core_scope_refuses_to_start_and_names_it(logged) -> None:
    channel = _channel()
    await _started(channel, _client(_EVERYTHING - {"chat:write"}))

    assert channel._scopes_permit_start() is False
    assert len(logged["error"]) == 1
    assert "chat:write" in logged["error"][0]
    # And the refusal says the rest of the gateway is unaffected, because the
    # operator reading it has to know whether Slack alone is down or everything.
    assert "unaffected" in logged["error"][0]


@pytest.mark.asyncio
async def test_a_missing_tool_scope_is_reported_and_the_channel_starts(
    logged,
) -> None:
    channel = _channel()
    await _started(channel, _client(_EVERYTHING - {"reactions:write"}))

    assert channel._scopes_permit_start() is True
    assert logged["error"] == []
    assert len(logged["warning"]) == 1
    line = logged["warning"][0]
    assert "reactions:write" in line
    # The actionable half: what the grant was for, not merely that it is absent.
    assert "acknowledgement mark" in line


@pytest.mark.asyncio
async def test_a_narrowed_install_starts_though_the_scope_is_tier_zero(
    logged,
) -> None:
    """``groups:history`` is core, and the manifest offers to delete it.

    An operator who narrowed the install to keep the bot out of private
    channels did what the file they pasted told them they could, and must not
    find the channel refusing to start over it.
    """
    channel = _channel()
    await _started(channel, _client(_EVERYTHING - {"groups:history"}))

    assert channel._scopes_permit_start() is True
    assert logged["error"] == []
    assert any("groups:history" in line for line in logged["warning"])


@pytest.mark.asyncio
async def test_an_unreadable_header_starts_the_channel(logged) -> None:
    channel = _channel()
    await _started(channel, _client(None, header=False))

    assert channel._scopes_permit_start() is True
    assert logged["error"] == []
    assert logged["warning"] == []
    # Said, so the absence of the usual line is not read as a clean bill of
    # health by whoever goes looking for it.
    assert any("could not be checked" in line for line in logged["info"])


@pytest.mark.asyncio
async def test_an_auth_test_that_failed_leaves_the_scopes_unknown(logged) -> None:
    """No answer is not an empty grant either, and must not stop the channel."""

    class _Failing:
        async def auth_test(self) -> Any:
            raise RuntimeError("Slack unavailable")

    channel = _channel()
    await _started(channel, _Failing())

    assert channel._granted_scopes is None
    assert channel._scopes_permit_start() is True


# --------------------------------------------------------------------------
# Several workspaces
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_one_short_install_does_not_stop_the_others(logged) -> None:
    """The degrade is per workspace, which is what one channel per block means."""
    short = _channel(app_id="acme")
    await _started(short, _client(_EVERYTHING - {"chat:write"}))
    whole = _channel(app_id="globex")
    await _started(whole, _client(_EVERYTHING))

    assert short._scopes_permit_start() is False
    assert whole._scopes_permit_start() is True
    # The refusal names which workspace it is about, both by the gateway's own
    # name for the credential block and by the team Slack answered with.
    assert "acme" in logged["error"][0]
    assert "T0TEAM0001" in logged["error"][0]


@pytest.mark.asyncio
async def test_start_returns_before_the_handler_when_a_core_scope_is_missing(
    monkeypatch, logged
) -> None:
    """The refusal is a ``return``, not an exception, and nothing starts listening.

    A channel that cannot answer must not first accept a message it will then
    drop, so the check sits after ``auth.test`` and before the socket-mode
    handler is started.
    """
    handler_started = False
    fake_client = _client(_EVERYTHING - {"chat:write"})

    class FakeAsyncApp:
        def __init__(self, token: str, logger: Any = None) -> None:
            self.client = fake_client

        def event(self, event_name: str):
            return lambda listener: listener

        def action(self, constraint: Any):
            return lambda listener: listener

    class FakeSocketModeHandler:
        def __init__(self, app: Any, app_token: str) -> None:
            pass

        async def start_async(self) -> None:
            nonlocal handler_started
            handler_started = True
            await asyncio.sleep(3600)

        async def close_async(self) -> None:
            pass

    monkeypatch.setattr(slack_connect, "SLACK_AVAILABLE", True)
    monkeypatch.setattr(slack_connect, "AsyncApp", FakeAsyncApp)
    monkeypatch.setattr(slack_connect, "AsyncSocketModeHandler", FakeSocketModeHandler)

    channel = _channel()
    await asyncio.wait_for(channel.start(), timeout=5)

    assert handler_started is False
    assert channel.is_running is False
    assert len(logged["error"]) == 1
    assert "chat:write" in logged["error"][0]


@pytest.mark.asyncio
async def test_the_direct_message_scopes_are_reported_when_they_are_absent(
    logged,
) -> None:
    """Nothing opened a DM before the posting tools did, so these are new grants.

    The startup check needs no edit to cover them, which is the point of driving
    it from the scope table: adding ``conversations.open`` to
    ``METHOD_REQUIREMENTS`` is what puts its two scopes into the manifests, into
    the tier-2 warning, and into the sentence naming what an absent one costs.
    This is the line that fails if the entry is dropped or retiered.

    An install that predates them is short of both until it is reinstalled, and
    that is a bot which posts into channels and refuses a direct message by
    name, rather than a bot that does not start.
    """
    channel = _channel()
    await _started(channel, _client(_EVERYTHING - {"im:write", "mpim:write"}))

    assert channel._scopes_permit_start() is True
    assert logged["error"] == []
    assert len(logged["warning"]) == 1
    line = logged["warning"][0]
    assert "im:write" in line
    assert "mpim:write" in line
    # The actionable half: what the grant was for, not merely that it is absent.
    assert "opening a direct message" in line
    # Neither is a refusal to start: a direct message is one destination out of
    # several, and an install short of it can still answer where it was spoken
    # to.
    assert "im:write" not in slack_required_scopes()
    assert "mpim:write" not in slack_required_scopes()
