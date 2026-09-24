# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Which configured Slack install serves one tool call.

``select_slack_workspace`` is the whole of the reading, so this is where the
three cases are pinned: one install answered without a question asked, several
resolved by the team the request arrived from, and a team nobody answers to
refused rather than served from whichever block came first.

The first test in the file is the one that matters most to a deployment that
has never heard of workspaces: the flat mapping every Slack config written
before this key existed must come back out of the reader unchanged.
"""

from __future__ import annotations

from typing import Any

import pytest

from jiuwenswarm.common.slack_history_policy import (
    METADATA_TEAM_KEY,
    SlackWorkspaceUnresolved,
    select_slack_workspace,
    slack_config,
    slack_team_id_from_session,
    slack_workspace_blocks,
)

FLAT = {
    "bot_token": "xoxb-one",
    "app_token": "xapp-one",
    "search_enabled": True,
    "history": "members",
    "history_max_api_calls": 40,
}

TWO = {
    "search_enabled": True,
    "history": "members",
    "workspaces": [
        {"bot_token": "xoxb-a", "app_token": "xapp-a"},
        {"bot_token": "xoxb-b", "app_token": "xapp-b", "default_channel_id": "C0B"},
    ],
}

IDENTITIES = {"xoxb-a": "T0AAA", "xoxb-b": "T0BBB"}


def _identify(token: str) -> str | None:
    return IDENTITIES.get(token)


def _wrapped(slack: Any) -> dict[str, Any]:
    return {"channels": {"slack": slack}}


# --- the single mapping, which is what the live deployment writes ------------


def test_a_flat_mapping_is_returned_unchanged_whatever_is_asked_of_it() -> None:
    """The one case a multi-workspace change must not move at all.

    Not merely equal: the same object. A flat ``channels.slack`` holds one
    install's tokens beside every connector-wide key, so there is nothing to
    select between and nothing to overlay, and a caller that asked for a team
    gets exactly what a caller that asked for nothing gets.
    """
    assert select_slack_workspace(FLAT) is FLAT
    assert select_slack_workspace(FLAT, "") is FLAT
    assert select_slack_workspace(FLAT, "T0AAA") is FLAT
    assert select_slack_workspace(FLAT, "T0ZZZ", team_of_token=_identify) is FLAT


def test_slack_config_without_a_team_reads_exactly_what_it_always_read() -> None:
    """The default call is untouched: no selection, no refusal, no identity."""
    assert slack_config(_wrapped(FLAT)) is FLAT
    assert slack_config(_wrapped(TWO)) is TWO
    assert slack_config(_wrapped({})) == {}
    assert slack_config(_wrapped("nonsense")) == {}
    assert slack_config({}) == {}


def test_an_empty_workspaces_list_is_read_as_no_list_at_all() -> None:
    """Upgrading a working deployment must not take its token away.

    The shipped template carries ``workspaces: []`` because a key absent from
    the template is deleted from the operator's file, so every flat config
    gains the key the moment it is upgraded.
    """
    upgraded = dict(FLAT, workspaces=[])
    assert slack_workspace_blocks(upgraded) == (upgraded,)
    assert select_slack_workspace(upgraded, "T0AAA") is upgraded


def test_one_written_block_is_overlaid_onto_the_connector_wide_keys() -> None:
    """A one-entry list is one install, and its token is the one that serves.

    The connector-wide keys beside the list keep reading as they did; only the
    four that belong to an install are written over.
    """
    one = {"search_enabled": True, "workspaces": [{"bot_token": "xoxb-a"}]}
    resolved = select_slack_workspace(one, "T0ZZZ")
    assert resolved["bot_token"] == "xoxb-a"
    assert resolved["search_enabled"] is True
    # No identity was asked for and no team compared: one install means one
    # Socket Mode connection, so the only events that can arrive are its own.


# --- several installs -------------------------------------------------------


def test_a_request_is_served_by_the_block_installed_in_its_team() -> None:
    a = select_slack_workspace(TWO, "T0AAA", team_of_token=_identify)
    b = select_slack_workspace(TWO, "T0BBB", team_of_token=_identify)
    assert a["bot_token"] == "xoxb-a"
    assert b["bot_token"] == "xoxb-b"
    # The install's own keys travel with it; the connector-wide ones do not
    # change between the two.
    assert b["default_channel_id"] == "C0B"
    assert a.get("default_channel_id", "") == ""
    assert a["history"] == b["history"] == "members"


def test_a_team_no_block_answers_to_is_refused_rather_than_served() -> None:
    """The failure the whole selection exists to prevent.

    Falling back to the first block would read or write in a workspace nobody
    asked about, and would do it silently.
    """
    with pytest.raises(SlackWorkspaceUnresolved) as raised:
        select_slack_workspace(TWO, "T0CCC", team_of_token=_identify)
    assert raised.value.code == "slack_workspace_unknown"
    assert "T0CCC" in raised.value.detail


def test_a_request_naming_no_workspace_is_refused_once_there_are_several() -> None:
    """Empty is not a licence to guess, and it says which key was missing."""
    with pytest.raises(SlackWorkspaceUnresolved) as raised:
        select_slack_workspace(TWO, "", team_of_token=_identify)
    assert raised.value.code == "slack_request_names_no_workspace"
    assert METADATA_TEAM_KEY in raised.value.detail


def test_a_token_nobody_has_identified_yet_matches_nothing() -> None:
    """An unanswered identity is not a match, and not a fallback either."""
    with pytest.raises(SlackWorkspaceUnresolved) as raised:
        select_slack_workspace(TWO, "T0AAA", team_of_token=lambda _token: None)
    assert raised.value.code == "slack_workspace_unknown"
    with pytest.raises(SlackWorkspaceUnresolved):
        select_slack_workspace(TWO, "T0AAA")


def test_a_workspaces_key_that_is_not_a_list_yields_no_install() -> None:
    """The one shape that must not fall back to the top-level pair.

    An operator who wrote a list wrote it to stop one token serving
    everything, so a list that cannot be read leaves nothing to serve with.
    """
    broken = dict(FLAT, workspaces="nonsense")
    assert slack_workspace_blocks(broken) == ()
    with pytest.raises(SlackWorkspaceUnresolved) as raised:
        select_slack_workspace(broken, "T0AAA", team_of_token=_identify)
    assert raised.value.code == "slack_workspace_unconfigured"


def test_a_malformed_entry_is_dropped_and_the_others_still_serve() -> None:
    mixed = {
        "workspaces": [
            "not a mapping",
            {"bot_token": "xoxb-a"},
            {"bot_token": "xoxb-b"},
        ]
    }
    assert len(slack_workspace_blocks(mixed)) == 2
    resolved = select_slack_workspace(mixed, "T0BBB", team_of_token=_identify)
    assert resolved["bot_token"] == "xoxb-b"


def test_a_written_list_wins_over_a_top_level_pair_left_beside_it() -> None:
    """The connector says the list wins; the tools must agree with it.

    A config holding both is the shape where a tool reading the top-level
    token would reach a workspace the connector has declared ignored.
    """
    mixed = dict(FLAT, workspaces=[{"bot_token": "xoxb-b"}, {"bot_token": "xoxb-a"}])
    resolved = select_slack_workspace(mixed, "T0AAA", team_of_token=_identify)
    assert resolved["bot_token"] == "xoxb-a"
    assert resolved["app_token"] == "xapp-one"


# --- the reading agrees with the connector's own ----------------------------


@pytest.mark.parametrize(
    "raw",
    [
        FLAT,
        dict(FLAT, workspaces=[]),
        TWO,
        {"workspaces": [{"bot_token": "xoxb-a"}]},
        {},
    ],
)
def test_the_runtime_reads_the_same_installs_the_connector_builds(raw: Any) -> None:
    """One shape, two readers, pinned to each other.

    The runtime may not import the connector, so it has its own reading of
    ``channels.slack``. A test is the only thing that keeps the two from
    drifting, and a drift here is a tool serving a workspace the connector is
    not connected to.
    """
    from jiuwenswarm.gateway.channel_manager.im_platforms.slack.slack_connect import (
        SLACK_WORKSPACES_KEY,
        normalize_slack_conf,
    )

    connector = normalize_slack_conf(raw)[SLACK_WORKSPACES_KEY]
    runtime = slack_workspace_blocks(raw)
    assert [str(block.get("bot_token") or "").strip() for block in connector] == [
        str(block.get("bot_token") or "").strip() for block in runtime
    ]


# --- the team a cron run has ------------------------------------------------


@pytest.mark.parametrize(
    ("session_id", "expected"),
    [
        ("slack_T0AAA_C0AAA", "T0AAA"),
        ("slack_T0AAA_C0AAA_1700000000.000100", "T0AAA"),
        ("slack_T0AAA", ""),
        ("slack_", ""),
        ("wechat_T0AAA_C0AAA", ""),
        ("", ""),
        (None, ""),
    ],
)
def test_the_team_is_read_off_a_slack_session_id(
    session_id: Any, expected: str
) -> None:
    assert slack_team_id_from_session(session_id) == expected
