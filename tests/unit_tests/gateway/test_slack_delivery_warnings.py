# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Early warnings for Slack producers that have nowhere to deliver.

``SlackChannel`` resolves an outbound message's channel through four rungs, and
the last of them -- ``channels.slack.default_channel_id`` -- is empty by
default. Everything that works today works because it lands on rung 3: a cron
job created inside a Slack turn holds that turn's session id, and the session
id names the channel. Two producers cannot: a Slack-targeted cron job made from
the web panel or the TUI, which has no Slack session to store, and the
heartbeat, whose relay target names a connector rather than a conversation.

Both fail only at delivery, inside the send path of a run nobody is watching.
These tests pin the warnings that say so at the moment somebody is: when a job
is written, and when the gateway starts. They also pin that nothing here
refuses -- the job is still created, the gateway still starts.
"""

from __future__ import annotations

import logging
import time
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any, Iterator

import pytest

from jiuwenswarm.gateway.channel_manager.im_platforms.slack.slack_connect import (
    SlackChannel,
    SlackChannelConfig,
    describe_slack_delivery_reachability,
    slack_default_channel_id_from_config,
    warn_if_heartbeat_relay_unreachable,
)
from jiuwenswarm.gateway.cron.controller import CronController
from jiuwenswarm.common.slack_routing import (
    warn_if_slack_cron_delivery_unreachable,
)
from jiuwenswarm.gateway.cron.store import CronJobStore

_SLACK_CHANNEL = "C-ONE"
# What the connector derives from a real Slack turn: slack_{team}_{channel}_{x}.
_SLACK_SESSION = f"slack_T-TEAM_{_SLACK_CHANNEL}_U-ONE"
# What a job created from the web panel or the TUI holds instead.
_WEB_SESSION = "web_42"

_SLACK_ROUTING_LOGGER = "jiuwenswarm.common.slack_routing"
_SLACK_CONNECT_LOGGER = (
    "jiuwenswarm.gateway.channel_manager.im_platforms.slack.slack_connect"
)


@contextmanager
def _captured(logger_name: str) -> Iterator[list[logging.LogRecord]]:
    """Records emitted by one logger, taken off that logger directly.

    Not ``caplog``: the handler pytest installs sits on the root logger, so what
    it sees depends on whether this package's loggers propagate -- which is a
    property of whatever configured logging first, not of the code under test. A
    warning test that passes or fails on that is worse than no test.
    """
    records: list[logging.LogRecord] = []
    logger = logging.getLogger(logger_name)
    handler = logging.Handler()
    handler.emit = records.append  # type: ignore[method-assign]
    previous_level = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    try:
        yield records
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous_level)


def _messages(records: list[logging.LogRecord], level: int) -> list[str]:
    return [r.getMessage() for r in records if r.levelno == level]


@pytest.fixture
def slack_default(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Set ``channels.slack.default_channel_id`` for the duration of a test.

    Returns a setter; not calling it leaves the key absent, which is the live
    deployment's state and the one every warning here is about.
    """
    import jiuwenswarm.common.config as config_module

    def _set(value: str = "") -> None:
        monkeypatch.setattr(
            config_module,
            "get_config",
            lambda: {"channels": {"slack": {"default_channel_id": value}}},
        )

    _set("")
    return _set


class _StubScheduler:
    async def reload(self) -> None:
        return None

    async def project_execution_allowed(self, project_id: str, user_id: str) -> bool:
        return True


@pytest.fixture
def controller(tmp_path: Any) -> CronController:
    return CronController(
        store=CronJobStore(path=tmp_path / "cron_jobs.json"),
        scheduler=_StubScheduler(),
    )


def _create_params(**overrides: Any) -> dict[str, Any]:
    params: dict[str, Any] = {
        "name": "digest",
        "cron_expr": "0 0 9 * * ? *",
        "timezone": "Asia/Shanghai",
        "description": "summarise the channel",
        "targets": "slack",
    }
    params.update(overrides)
    return params


# ── The predicate, on the ladder itself ──────────────────────────────────────


def test_the_predicate_runs_the_real_ladder() -> None:
    """Each rung answers, in order, and rung 4 is the producers' only fallback."""
    assert SlackChannel.resolve_delivery(
        metadata={"slack_channel_id": "C-META", "slack_thread_ts": "1.2"},
        session_id=_SLACK_SESSION,
        default_channel_id="C-FALLBACK",
    ) == ("C-META", "1.2")
    assert SlackChannel.resolve_delivery(
        session_id="slack_T_C-SESS_1712345678.000100",
        default_channel_id="C-FALLBACK",
    ) == ("C-SESS", "1712345678.000100")
    assert SlackChannel.resolve_delivery(
        session_id=_WEB_SESSION, default_channel_id=" C-FALLBACK "
    ) == ("C-FALLBACK", "")

    assert SlackChannel.delivery_is_reachable(session_id=_SLACK_SESSION)
    assert not SlackChannel.delivery_is_reachable(session_id=_WEB_SESSION)
    assert SlackChannel.delivery_is_reachable(
        session_id=_WEB_SESSION, default_channel_id="C-FALLBACK"
    )
    # A heartbeat has no session at all, so rung 4 is all it can ever reach.
    assert not SlackChannel.delivery_is_reachable(session_id=None)
    assert SlackChannel.delivery_is_reachable(
        session_id=None, default_channel_id="C-FALLBACK"
    )


def test_a_session_named_by_channel_alone_is_deliverable() -> None:
    """Rung 3 reads every form of Slack session id, target or no target.

    The ladder parses the session id with the cron package's parser rather than
    a second copy, so a session belonging to a channel itself resolves to that
    channel's root instead of falling through to the operator's blanket
    fallback.
    """
    assert SlackChannel.resolve_delivery(
        session_id=f"slack_T-TEAM_{_SLACK_CHANNEL}", default_channel_id="C-FALLBACK"
    ) == (_SLACK_CHANNEL, "")
    assert SlackChannel.delivery_is_reachable(
        session_id=f"slack_T-TEAM_{_SLACK_CHANNEL}"
    )


def test_default_channel_id_is_read_off_the_config_mapping(
    slack_default: Any,
) -> None:
    assert slack_default_channel_id_from_config({}) == ""
    assert slack_default_channel_id_from_config({"channels": {"slack": {}}}) == ""
    assert (
        slack_default_channel_id_from_config(
            {"channels": {"slack": {"default_channel_id": " C-X "}}}
        )
        == "C-X"
    )
    # No mapping given: live config, which the fixture pinned to unset.
    assert slack_default_channel_id_from_config() == ""


def test_default_channel_id_reads_the_workspaces_list(slack_default: Any) -> None:
    """With workspaces written, the connector-wide key is routinely empty.

    Both callers ask "is this producer aimed at nowhere?", so the first block
    naming a fallback is the answer for the deployment.
    """
    assert (
        slack_default_channel_id_from_config(
            {
                "channels": {
                    "slack": {
                        "workspaces": [
                            {"bot_token": "b1", "app_token": "a1"},
                            {
                                "bot_token": "b2",
                                "app_token": "a2",
                                "default_channel_id": " C-BLOCK ",
                            },
                        ]
                    }
                }
            }
        )
        == "C-BLOCK"
    )
    # The connector-wide key still wins when it is set.
    assert (
        slack_default_channel_id_from_config(
            {
                "channels": {
                    "slack": {
                        "default_channel_id": "C-WIDE",
                        "workspaces": [{"default_channel_id": "C-BLOCK"}],
                    }
                }
            }
        )
        == "C-WIDE"
    )
    # A malformed list is no fallback rather than an exception.
    assert (
        slack_default_channel_id_from_config(
            {"channels": {"slack": {"workspaces": ["nonsense"]}}}
        )
        == ""
    )


def test_an_unreadable_config_reports_no_fallback_rather_than_raising(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import jiuwenswarm.common.config as config_module

    def _boom() -> dict:
        raise RuntimeError("config.yaml is a directory")

    monkeypatch.setattr(config_module, "get_config", _boom)
    with _captured(_SLACK_CONNECT_LOGGER) as records:
        assert slack_default_channel_id_from_config() == ""
    assert any("treating it as unset" in m for m in _messages(records, logging.WARNING))


# ── 1. At cron creation ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_slack_job_with_a_slack_session_does_not_warn(
    controller: CronController, slack_default: Any
) -> None:
    with _captured(_SLACK_ROUTING_LOGGER) as records:
        created = await controller.create_job(
            _create_params(session_id=_SLACK_SESSION),
            request_channel_id="slack",
            request_session_id=_SLACK_SESSION,
        )

    assert created["session_id"] == _SLACK_SESSION
    assert not [m for m in _messages(records, logging.WARNING) if "delivery" in m]


@pytest.mark.asyncio
async def test_a_slack_job_with_no_slack_session_and_no_fallback_warns(
    controller: CronController, slack_default: Any
) -> None:
    """The web/TUI case: targets says Slack, nothing says which conversation."""
    with _captured(_SLACK_ROUTING_LOGGER) as records:
        created = await controller.create_job(
            _create_params(session_id=_WEB_SESSION),
            request_channel_id="web",
            request_session_id=_WEB_SESSION,
        )

    # Warned, not refused: the job exists and keeps everything it was given.
    assert created["targets"] == "slack"
    assert created["session_id"] == _WEB_SESSION
    warnings = [m for m in _messages(records, logging.WARNING) if "delivery" in m]
    assert len(warnings) == 1
    assert "targets Slack but no delivery channel can be resolved" in warnings[0]
    assert _WEB_SESSION in warnings[0]
    assert "channels.slack.default_channel_id is unset" in warnings[0]


@pytest.mark.asyncio
async def test_the_same_job_does_not_warn_once_a_fallback_exists(
    controller: CronController, slack_default: Any
) -> None:
    slack_default("C-FALLBACK")
    with _captured(_SLACK_ROUTING_LOGGER) as records:
        await controller.create_job(
            _create_params(session_id=_WEB_SESSION),
            request_channel_id="web",
            request_session_id=_WEB_SESSION,
        )

    assert not [m for m in _messages(records, logging.WARNING) if "delivery" in m]


@pytest.mark.asyncio
async def test_a_job_targeting_another_channel_is_not_this_predicates_business(
    controller: CronController, slack_default: Any
) -> None:
    with _captured(_SLACK_ROUTING_LOGGER) as records:
        await controller.create_job(
            _create_params(targets="web", session_id=_WEB_SESSION),
            request_channel_id="web",
            request_session_id=_WEB_SESSION,
        )

    assert not [m for m in _messages(records, logging.WARNING) if "delivery" in m]


@pytest.mark.asyncio
async def test_repointing_an_existing_job_at_slack_warns(
    controller: CronController, slack_default: Any
) -> None:
    """The edit that breaks a working job without touching its session.

    ``targets`` moves to Slack, the session stays whatever the web panel had.
    Nothing in the patch mentions a channel, so nothing but this warning marks
    the moment the job stopped being deliverable.
    """
    created = await controller.create_job(
        _create_params(targets="web", session_id=_WEB_SESSION),
        request_channel_id="web",
        request_session_id=_WEB_SESSION,
    )

    with _captured(_SLACK_ROUTING_LOGGER) as records:
        patched = await controller.update_job(
            created["id"],
            {"targets": "slack"},
            request_channel_id="web",
            request_session_id=_WEB_SESSION,
        )

    assert patched["targets"] == "slack"
    warnings = [m for m in _messages(records, logging.WARNING) if "delivery" in m]
    assert len(warnings) == 1
    assert created["id"] in warnings[0]


@pytest.mark.asyncio
async def test_the_agent_tool_path_warns_the_same_way(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch, slack_default: Any
) -> None:
    """``CronTools`` is the other creation path and asks the same helper.

    A model on a web turn can name ``slack`` as a job's target; the session the
    tool stores is the route's own, which is not a Slack one, so the job has no
    conversation for the same reason the RPC's does not.
    """
    from jiuwenswarm.agents.harness.common.tools.cron.cron_tools import (
        CronTools,
        CronToolRoute,
    )

    root = tmp_path / "agent"
    root.mkdir()
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.project_store.get_agent_root_dir",
        lambda: root,
    )
    from jiuwenswarm.server.runtime.session import project_store

    project_store.invalidate_cache()

    tools = CronTools(agent_client=object(), message_handler=object())
    tools._local_store = CronJobStore(path=tmp_path / "cron_jobs.json")

    async def _noop() -> None:
        return None

    monkeypatch.setattr(tools, "_reload_scheduler", _noop)

    async def _no_sync(action: str, payload: dict) -> None:
        return None

    monkeypatch.setattr(tools, "_send", _no_sync)

    token = tools.push_cron_route(
        CronToolRoute(channel_id="web", session_id=_WEB_SESSION)
    )
    try:
        with _captured(_SLACK_ROUTING_LOGGER) as records:
            job = await tools.create_job(
                {
                    "id": "job-web-to-slack",
                    "name": "daily",
                    "cron_expr": "0 8 * * *",
                    "timezone": "Europe/Paris",
                    "description": "hello",
                    "targets": "slack",
                }
            )
    finally:
        tools.reset_cron_route(token)

    assert job["targets"] == "slack"
    warnings = [m for m in _messages(records, logging.WARNING) if "delivery" in m]
    assert len(warnings) == 1
    assert "job-web-to-slack" in warnings[0]


def test_a_reachability_check_that_itself_fails_stays_a_warning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A warning may never be the thing that breaks a job write."""
    import jiuwenswarm.common.config as config_module

    class _Hostile(dict):
        def get(self, *args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("config went away mid-read")

    monkeypatch.setattr(
        config_module, "get_config", lambda: _Hostile({"channels": {}})
    )
    with _captured(_SLACK_ROUTING_LOGGER) as records:
        assert (
            warn_if_slack_cron_delivery_unreachable(
                targets="slack", session_id=_WEB_SESSION, job_id="job-x"
            )
            is False
        )
    assert any(
        "could not check Slack delivery reachability" in m
        for m in _messages(records, logging.WARNING)
    )


# ── 2. At startup, folded into the per-channel summary ───────────────────────


def _job(job_id: str, *, targets: str = "slack", session_id: str = "", enabled: bool = True) -> Any:
    return SimpleNamespace(
        id=job_id,
        targets=targets,
        session_id=session_id,
        enabled=enabled,
        created_at=time.time(),
    )


class _StubStore:
    def __init__(self, jobs: list[Any] | None = None, error: Exception | None = None):
        self._jobs = jobs or []
        self._error = error

    async def list_jobs(self) -> list[Any]:
        if self._error is not None:
            raise self._error
        return list(self._jobs)


@pytest.mark.asyncio
async def test_the_startup_summary_counts_a_mix_of_jobs() -> None:
    jobs = [
        _job("ok-1", session_id=_SLACK_SESSION),
        _job("ok-2", session_id="slack_T_C-TWO_1712345678.000100"),
        _job("broken-1", session_id=_WEB_SESSION),
        _job("broken-2", session_id=""),
        # Disabled, and jobs for other connectors: neither is counted.
        _job("disabled", session_id=_WEB_SESSION, enabled=False),
        _job("elsewhere", targets="web", session_id=_WEB_SESSION),
    ]

    with _captured(_SLACK_CONNECT_LOGGER) as records:
        message = await describe_slack_delivery_reachability(
            SlackChannelConfig(),
            cron_store=_StubStore(jobs),
            heartbeat_target="web",
        )

    assert "2 of 4 enabled Slack cron job(s) cannot resolve a channel" in message
    assert "(broken-1, broken-2)" in message
    assert "channels.slack.default_channel_id is unset" in message
    assert "heartbeat.target=web is not Slack" in message
    assert message in _messages(records, logging.WARNING)


@pytest.mark.asyncio
async def test_the_startup_summary_is_information_when_everything_resolves() -> None:
    with _captured(_SLACK_CONNECT_LOGGER) as records:
        message = await describe_slack_delivery_reachability(
            SlackChannelConfig(),
            cron_store=_StubStore([_job("ok-1", session_id=_SLACK_SESSION)]),
            heartbeat_target="web",
        )

    assert "all 1 enabled Slack cron job(s) resolve a channel" in message
    assert message in _messages(records, logging.INFO)
    assert not _messages(records, logging.WARNING)


@pytest.mark.asyncio
async def test_a_fallback_makes_every_slack_job_reachable() -> None:
    with _captured(_SLACK_CONNECT_LOGGER) as records:
        message = await describe_slack_delivery_reachability(
            SlackChannelConfig(default_channel_id="C-FALLBACK"),
            cron_store=_StubStore([_job("broken-1", session_id=_WEB_SESSION)]),
            heartbeat_target="slack",
        )

    assert "all 1 enabled Slack cron job(s) resolve a channel" in message
    assert "heartbeat.target=slack falls back to default_channel_id" in message
    assert "channels.slack.default_channel_id=C-FALLBACK" in message
    assert message in _messages(records, logging.INFO)


@pytest.mark.asyncio
async def test_the_startup_summary_notes_a_heartbeat_it_cannot_deliver() -> None:
    with _captured(_SLACK_CONNECT_LOGGER) as records:
        message = await describe_slack_delivery_reachability(
            SlackChannelConfig(),
            cron_store=_StubStore([]),
            heartbeat_target="slack",
        )

    assert "no enabled Slack cron job" in message
    assert "heartbeat.target=slack has no fallback and can never be delivered" in message
    assert message in _messages(records, logging.WARNING)


@pytest.mark.asyncio
async def test_an_unreadable_cron_store_warns_rather_than_raising() -> None:
    """A malformed cron_jobs.json must not stop the Slack connector coming up."""
    with _captured(_SLACK_CONNECT_LOGGER) as records:
        message = await describe_slack_delivery_reachability(
            SlackChannelConfig(),
            cron_store=_StubStore(error=OSError("cron_jobs.json is unreadable")),
            heartbeat_target="web",
        )

    assert "the cron store could not be read" in message
    assert "cron_jobs.json is unreadable" in message
    assert message in _messages(records, logging.WARNING)


# ── 3. The heartbeat ─────────────────────────────────────────────────────────


def test_a_heartbeat_aimed_at_slack_with_no_fallback_warns() -> None:
    with _captured(_SLACK_CONNECT_LOGGER) as records:
        message = warn_if_heartbeat_relay_unreachable(
            heartbeat_target="slack", default_channel_id=""
        )

    assert "no heartbeat" in message and "will ever be delivered to Slack" in message
    assert "channels.slack.default_channel_id is unset" in message
    assert message in _messages(records, logging.WARNING)


@pytest.mark.parametrize(
    ("heartbeat_target", "default_channel_id"),
    [
        ("web", ""),
        ("", ""),
        (None, ""),
        ("tui", ""),
        # Aimed at Slack, but rung 4 answers.
        ("slack", "C-FALLBACK"),
        ("SLACK", "C-FALLBACK"),
    ],
)
def test_a_deliverable_heartbeat_says_nothing(
    heartbeat_target: Any, default_channel_id: str
) -> None:
    with _captured(_SLACK_CONNECT_LOGGER) as records:
        assert (
            warn_if_heartbeat_relay_unreachable(
                heartbeat_target=heartbeat_target,
                default_channel_id=default_channel_id,
            )
            == ""
        )
    assert not _messages(records, logging.WARNING)
