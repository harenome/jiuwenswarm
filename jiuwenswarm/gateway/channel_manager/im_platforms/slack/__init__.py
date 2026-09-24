"""Slack channel integration."""

from jiuwenswarm.gateway.channel_manager.im_platforms.slack.slack_dedup import (
    SlackEventDedupStore,
    get_slack_dedup_path,
)
from jiuwenswarm.gateway.channel_manager.im_platforms.slack.slack_connect import (
    SlackChannel,
    SlackChannelConfig,
    SlackDeliveryError,
)

__all__ = [
    "SlackChannel",
    "SlackChannelConfig",
    "SlackDeliveryError",
    "SlackEventDedupStore",
    "get_slack_dedup_path",
]
