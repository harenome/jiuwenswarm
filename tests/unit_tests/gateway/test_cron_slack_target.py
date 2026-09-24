"""Slack cron target validation."""

import logging

import pytest

from jiuwenswarm.gateway.cron.models import (
    CronTargetChannel,
    _normalize_targets_str,
    is_valid_target_channel_id,
    normalize_target_channel_id,
)


def test_slack_is_a_supported_cron_target() -> None:
    assert CronTargetChannel.SLACK.value == "slack"
    assert is_valid_target_channel_id("slack")
    assert is_valid_target_channel_id("SLACK")
    assert normalize_target_channel_id("SLACK") == "slack"


def test_unknown_persisted_target_is_coerced_but_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING, logger="jiuwenswarm.gateway.cron.models"):
        assert _normalize_targets_str("telegram") == "web"
    assert "telegram" in caplog.text
    assert "web" in caplog.text


@pytest.mark.parametrize(
    "raw",
    [
        "slack",
        "SLACK",
        "web",
        # A valid enterprise key normalizes to a shorter form; that is not an
        # unknown target and must not warn.
        "feishu_enterprise:cli_abc:chat:oc_1",
        # Empty simply means "unset" and falls back to the default silently.
        "",
    ],
)
def test_known_targets_do_not_warn(
    raw: str, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING, logger="jiuwenswarm.gateway.cron.models"):
        _normalize_targets_str(raw)
    assert "unknown targets" not in caplog.text
