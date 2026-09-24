"""Unit tests for ``channels.slack`` in its single-mapping and list forms.

``normalize_slack_conf`` is the one place that reads the two shapes, so this is
where backward compatibility is pinned: the single mapping every existing Slack
config is written in must keep producing exactly one workspace with exactly the
credentials it names, and must keep doing so after a config upgrade has added
the shipped ``workspaces: []`` to it.
"""

from __future__ import annotations

import logging

from jiuwenswarm.gateway.channel_manager.im_platforms.slack.slack_connect import (
    SLACK_WORKSPACES_KEY,
    normalize_slack_conf,
)


def test_single_mapping_yields_one_workspace() -> None:
    """The shape every Slack config had before workspaces existed."""
    conf = normalize_slack_conf(
        {
            "bot_token": "xoxb-one",
            "app_token": "xapp-one",
            "default_channel_id": "C0FALLBACK",
            "group_chat_mode": "all",
        }
    )

    assert conf[SLACK_WORKSPACES_KEY] == [
        {
            "bot_token": "xoxb-one",
            "app_token": "xapp-one",
            "default_channel_id": "C0FALLBACK",
        }
    ]
    # The connector-wide keys are untouched: normalising adds a list, it does
    # not move anything into it.
    assert conf["group_chat_mode"] == "all"
    assert conf["bot_token"] == "xoxb-one"


def test_single_mapping_survives_the_shipped_empty_list() -> None:
    """A config upgraded against the template must not lose its workspace.

    The template ships ``workspaces: []`` because a key absent from it is
    deleted from an operator's file on upgrade. Read literally, that key would
    empty out a working deployment.
    """
    conf = normalize_slack_conf(
        {
            "bot_token": "xoxb-one",
            "app_token": "xapp-one",
            SLACK_WORKSPACES_KEY: [],
        }
    )

    assert conf[SLACK_WORKSPACES_KEY] == [
        {
            "bot_token": "xoxb-one",
            "app_token": "xapp-one",
            "default_channel_id": "",
        }
    ]


def test_single_mapping_carries_enabled_only_when_written() -> None:
    """Unset means "on when both tokens are present", which is _is_channel_enabled's rule."""
    absent = normalize_slack_conf({"bot_token": "b", "app_token": "a"})
    assert "enabled" not in absent[SLACK_WORKSPACES_KEY][0]

    written = normalize_slack_conf(
        {"bot_token": "b", "app_token": "a", "enabled": False}
    )
    assert written[SLACK_WORKSPACES_KEY][0]["enabled"] is False


def test_two_blocks_stay_two_blocks() -> None:
    conf = normalize_slack_conf(
        {
            "group_chat_mode": "mention",
            SLACK_WORKSPACES_KEY: [
                {
                    "bot_token": "xoxb-acme",
                    "app_token": "xapp-acme",
                    "default_channel_id": "C0ACME",
                },
                {
                    "bot_token": "xoxb-other",
                    "app_token": "xapp-other",
                    "enabled": False,
                },
            ],
        }
    )

    blocks = conf[SLACK_WORKSPACES_KEY]
    assert [b["bot_token"] for b in blocks] == ["xoxb-acme", "xoxb-other"]
    assert [b["app_token"] for b in blocks] == ["xapp-acme", "xapp-other"]
    assert blocks[1]["enabled"] is False


def test_a_block_inherits_the_connector_wide_default_channel() -> None:
    conf = normalize_slack_conf(
        {
            "default_channel_id": "C0SHARED",
            SLACK_WORKSPACES_KEY: [
                {"bot_token": "b1", "app_token": "a1"},
                {"bot_token": "b2", "app_token": "a2", "default_channel_id": "C0OWN"},
            ],
        }
    )

    blocks = conf[SLACK_WORKSPACES_KEY]
    assert blocks[0]["default_channel_id"] == "C0SHARED"
    assert blocks[1]["default_channel_id"] == "C0OWN"


def test_a_policy_key_inside_a_block_is_dropped_and_named(caplog) -> None:
    """A block holds credentials only; anything else is ignored, loudly."""
    with caplog.at_level(
        logging.WARNING,
        logger="jiuwenswarm.gateway.channel_manager.im_platforms.slack.slack_connect",
    ):
        conf = normalize_slack_conf(
            {
                SLACK_WORKSPACES_KEY: [
                    {
                        "bot_token": "b",
                        "app_token": "a",
                        "group_chat_mode": "all",
                        "history": "open",
                    }
                ]
            }
        )

    assert set(conf[SLACK_WORKSPACES_KEY][0]) == {
        "bot_token",
        "app_token",
        "default_channel_id",
    }
    assert "group_chat_mode" in caplog.text
    assert "history" in caplog.text


def test_the_list_wins_over_a_top_level_pair_and_says_so(caplog) -> None:
    with caplog.at_level(
        logging.WARNING,
        logger="jiuwenswarm.gateway.channel_manager.im_platforms.slack.slack_connect",
    ):
        conf = normalize_slack_conf(
            {
                "bot_token": "xoxb-stale",
                "app_token": "xapp-stale",
                SLACK_WORKSPACES_KEY: [{"bot_token": "b", "app_token": "a"}],
            }
        )

    assert [b["bot_token"] for b in conf[SLACK_WORKSPACES_KEY]] == ["b"]
    assert "top-level" in caplog.text


def test_unreadable_input_is_reported_rather_than_raised(caplog) -> None:
    with caplog.at_level(
        logging.WARNING,
        logger="jiuwenswarm.gateway.channel_manager.im_platforms.slack.slack_connect",
    ):
        assert normalize_slack_conf(None) == {SLACK_WORKSPACES_KEY: []}
        assert normalize_slack_conf("nonsense") == {SLACK_WORKSPACES_KEY: []}
        assert normalize_slack_conf({SLACK_WORKSPACES_KEY: "nonsense"})[
            SLACK_WORKSPACES_KEY
        ] == []
        # One malformed entry must not take the workspaces beside it down.
        kept = normalize_slack_conf(
            {SLACK_WORKSPACES_KEY: ["nonsense", {"bot_token": "b", "app_token": "a"}]}
        )
        assert [b["bot_token"] for b in kept[SLACK_WORKSPACES_KEY]] == ["b"]


def test_normalising_twice_changes_nothing_and_says_nothing(caplog) -> None:
    """The gateway normalises the same mapping on every config apply.

    It writes the list back beside the keys it was built from and hands the
    result on, so the second pass sees its own output. A second pass that
    warned, or that read the list differently, would put a line in the log on
    every config reload and could restart every live connection.
    """
    once = normalize_slack_conf(
        {
            "bot_token": "xoxb-one",
            "app_token": "xapp-one",
            "default_channel_id": "C0FALLBACK",
            "group_chat_mode": "all",
        }
    )
    with caplog.at_level(
        logging.WARNING,
        logger="jiuwenswarm.gateway.channel_manager.im_platforms.slack.slack_connect",
    ):
        twice = normalize_slack_conf(once)

    assert twice == once
    assert caplog.text == ""


def test_normalising_a_list_twice_changes_nothing_and_says_nothing(caplog) -> None:
    once = normalize_slack_conf(
        {
            SLACK_WORKSPACES_KEY: [
                {"bot_token": "b1", "app_token": "a1", "default_channel_id": "C0A"},
                {"bot_token": "b2", "app_token": "a2", "enabled": False},
            ]
        }
    )
    with caplog.at_level(
        logging.WARNING,
        logger="jiuwenswarm.gateway.channel_manager.im_platforms.slack.slack_connect",
    ):
        twice = normalize_slack_conf(once)

    assert twice == once
    assert caplog.text == ""
