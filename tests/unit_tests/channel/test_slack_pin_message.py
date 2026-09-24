# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Pinning a Slack message, and the four refusals that decide what a caller sees.

Two of Slack's refusals mean *the conversation is already as you asked*, and are
reported as the success they are. Two mean *the change did not happen*, and are
reported as failures, because a caller that cannot tell them apart either
retries something that will never work or believes a pin that does not exist.

The other half of this file is about what the tool will not do: it pins in the
conversation the gateway named and nowhere else, it reaches Slack only after
its own arguments are established, and a request that carries no conversation
is refused rather than answered against some other one.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

import jiuwenswarm.common.config as config_module
from jiuwenswarm.agents.harness.common.tools.slack_pins import (
    SlackPinToolkit,
    slack_pin_request_metadata,
)
from jiuwenswarm.common.slack_history_policy import (
    METADATA_ORIGIN_KEY,
    ORIGIN_CRON_JOB,
)

_CHAT = "C-ROOM"
_OTHER = "C-ELSEWHERE"
_TS = "1758123456.123456"


class _FakeResponse:
    """What the SDK hangs off a refusal: a body naming the error and nothing else."""

    def __init__(self, error: str) -> None:
        self.status_code = 200
        self.data = {"ok": False, "error": error}
        self.headers: dict[str, Any] = {}


class _FakeSlackError(Exception):
    def __init__(self, error: str) -> None:
        super().__init__("sanitized fake failure")
        self.response = _FakeResponse(error)


class _Workspace:
    """A fake Slack that records what was asked of it and can refuse.

    Both pin methods answer ``{"ok": true}`` and nothing else, which is what
    the live API does; a result holding more than that would be this tool
    inventing it.
    """

    def __init__(self, fail: "dict[str, str] | None" = None) -> None:
        self.fail = fail or {}
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def _record(self, method: str, kwargs: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((method, kwargs))
        error = self.fail.get(method)
        if error:
            raise _FakeSlackError(error)
        return {"ok": True}

    async def pins_add(self, **kwargs: Any) -> dict[str, Any]:
        return self._record("pins_add", kwargs)

    async def pins_remove(self, **kwargs: Any) -> dict[str, Any]:
        return self._record("pins_remove", kwargs)


@pytest.fixture(autouse=True)
def _config(monkeypatch: pytest.MonkeyPatch) -> None:
    """The token this toolkit reads. Never the conversation: that arrives stamped."""
    monkeypatch.setattr(
        config_module,
        "get_config",
        lambda: {"channels": {"slack": {"bot_token": "xoxb-config-secret"}}},
    )


def _toolkit(workspace: _Workspace, **metadata: Any) -> SlackPinToolkit:
    base: dict[str, Any] = {"slack_channel_id": _CHAT}
    base.update(metadata)
    return SlackPinToolkit(metadata=base, client=workspace)


def _pin(toolkit: SlackPinToolkit, **kwargs: Any) -> dict[str, Any]:
    return json.loads(asyncio.run(toolkit.pin_message(**kwargs)))


def _card(toolkit: SlackPinToolkit) -> Any:
    tool = toolkit.get_tools()[0]
    return getattr(tool, "card", None) or tool._card


# ── 1. the two calls that do the work ────────────────────────────────────────


def test_a_pin_is_made_in_the_conversation_the_request_came_from() -> None:
    workspace = _Workspace()
    result = _pin(_toolkit(workspace), message_id=_TS)

    assert result == {
        "ok": True,
        "chat_id": _CHAT,
        "message_id": _TS,
        "pinned": True,
    }
    assert workspace.calls == [("pins_add", {"channel": _CHAT, "timestamp": _TS})]


def test_remove_unpins_and_says_the_message_is_no_longer_pinned() -> None:
    workspace = _Workspace()
    result = _pin(_toolkit(workspace), message_id=_TS, remove=True)

    assert result["ok"] is True
    assert result["pinned"] is False
    assert workspace.calls == [("pins_remove", {"channel": _CHAT, "timestamp": _TS})]


# ── 2. the two refusals that are already the requested state ─────────────────


def test_already_pinned_is_the_success_it_is() -> None:
    """The caller wanted this message pinned, and it is pinned.

    Reported as a failure it would read as *the pin did not happen*, and a
    model acting on that either retries forever or tells somebody the message
    is not pinned when everybody in the conversation can see that it is.
    """
    workspace = _Workspace(fail={"pins_add": "already_pinned"})
    result = _pin(_toolkit(workspace), message_id=_TS)

    assert result == {
        "ok": True,
        "chat_id": _CHAT,
        "message_id": _TS,
        "pinned": True,
    }


def test_no_pin_on_a_remove_is_the_success_it_is() -> None:
    workspace = _Workspace(fail={"pins_remove": "no_pin"})
    result = _pin(_toolkit(workspace), message_id=_TS, remove=True)

    assert result == {
        "ok": True,
        "chat_id": _CHAT,
        "message_id": _TS,
        "pinned": False,
    }


def test_the_two_swallowed_refusals_are_not_interchangeable() -> None:
    """``no_pin`` from a pin is not *already as you asked*, and is not swallowed.

    Each of the two is swallowed on the call it can mean success for, and on no
    other. A single set of codes checked whichever call was made would report an
    unpin that never happened.
    """
    workspace = _Workspace(fail={"pins_add": "no_pin"})
    result = _pin(_toolkit(workspace), message_id=_TS)

    assert result["ok"] is False
    assert result["error"] == "no_pin"


# ── 3. the two refusals a caller has to see ──────────────────────────────────


def test_a_full_pin_list_is_a_failure_and_says_what_resolves_it() -> None:
    """Not swallowed, and not retryable.

    The conversation's pin limit is reached, which is a fact about the
    conversation that only a person unpinning something changes. No number is
    quoted: Slack documents the limit's existence and not its size.
    """
    workspace = _Workspace(fail={"pins_add": "too_many_pins"})
    result = _pin(_toolkit(workspace), message_id=_TS)

    assert result["ok"] is False
    assert result["error"] == "too_many_pins"
    assert result["chat_id"] == _CHAT
    assert result["message_id"] == _TS
    assert "unpins" in result["detail"]
    assert "calling again will not help" in result["detail"]


def test_a_message_slack_will_not_pin_is_a_failure() -> None:
    workspace = _Workspace(fail={"pins_add": "not_pinnable"})
    result = _pin(_toolkit(workspace), message_id=_TS)

    assert result["ok"] is False
    assert result["error"] == "not_pinnable"
    assert "second call will not change that" in result["detail"]


def test_a_refusal_claims_nothing_about_whether_the_message_is_pinned() -> None:
    """``pinned`` is absent from every failure, on both calls.

    A refused call establishes nothing about the state of the message: a pin
    that failed for a full list left whatever was there untouched. ``false``
    would read as *it is not pinned*, which is a different claim from *this
    call did not pin it*.
    """
    for method, error in (
        ("pins_add", "too_many_pins"),
        ("pins_add", "not_pinnable"),
        ("pins_remove", "message_not_found"),
    ):
        workspace = _Workspace(fail={method: error})
        result = _pin(
            _toolkit(workspace), message_id=_TS, remove=method == "pins_remove"
        )
        assert result["ok"] is False
        assert "pinned" not in result


def test_a_missing_scope_names_the_scope_to_grant() -> None:
    workspace = _Workspace(fail={"pins_add": "missing_scope"})
    result = _pin(_toolkit(workspace), message_id=_TS)

    assert result["ok"] is False
    assert result["error"] == "missing_scope"
    assert "pins.add" in result["detail"]
    assert "pins:write" in result["detail"]


# ── 4. what is refused before Slack is reached at all ────────────────────────


def test_a_request_with_no_conversation_is_refused_and_calls_nothing() -> None:
    """Fails closed. There is no conversation to fall back to and none is invented."""
    workspace = _Workspace()
    toolkit = SlackPinToolkit(metadata={}, client=workspace)
    result = json.loads(asyncio.run(toolkit.pin_message(message_id=_TS)))

    assert result["ok"] is False
    assert result["error"] == "trusted_slack_channel_context_required"
    assert workspace.calls == []


def test_a_provider_that_raises_is_a_refusal_rather_than_a_default() -> None:
    def _explode() -> dict[str, Any]:
        raise RuntimeError("no context here")

    workspace = _Workspace()
    toolkit = SlackPinToolkit(metadata_provider=_explode, client=workspace)
    result = json.loads(asyncio.run(toolkit.pin_message(message_id=_TS)))

    assert result["ok"] is False
    assert result["error"] == "trusted_slack_channel_context_required"
    assert workspace.calls == []


@pytest.mark.parametrize(
    "message_id",
    ["", "   ", "yesterday", "1758123456", "1758123456.123456 ; rm -rf /", "C-ROOM"],
)
def test_a_message_id_that_is_not_one_never_reaches_slack(message_id: str) -> None:
    """A write refused for a bad argument must not have called Slack to find out."""
    workspace = _Workspace()
    result = _pin(_toolkit(workspace), message_id=message_id)

    assert result["ok"] is False
    assert result["error"] in {"message_id_required", "message_id_malformed"}
    assert workspace.calls == []


def test_the_conversation_is_never_taken_from_an_argument() -> None:
    """There is no ``chat_id`` on the card, and the schema is the whole of it."""
    card = _card(_toolkit(_Workspace()))

    assert card.name == "pin_message"
    assert set(card.input_params["properties"]) == {"message_id", "remove"}
    assert card.input_params["required"] == ["message_id"]
    assert "chat_id" not in card.description


def test_the_card_names_no_other_tool() -> None:
    """A model holding this card may hold no other.

    The tool is mounted on its own decision, so a sentence explaining it by
    reference to a sibling would, on a turn that has only this one, describe
    something the model cannot reach.
    """
    description = _card(_toolkit(_Workspace())).description

    for name in (
        "read_pinned_messages",
        "read_slack_conversation",
        "download_slack_file",
        "search_slack_workspace",
        "read_slack_canvas",
        "write_slack_canvas",
        "write_slack_channel_canvas",
        "edit_slack_canvas",
        "share_slack_canvas",
        "delete_slack_canvas",
        "read_slack_list",
        "write_slack_list",
        "edit_slack_list",
        "share_slack_list",
        "delete_slack_list",
    ):
        assert name not in description


# ── 5. which requests may pin at all ─────────────────────────────────────────


def test_an_inbound_slack_turn_may_pin() -> None:
    assert slack_pin_request_metadata("slack", {"slack_channel_id": _CHAT}) == {
        "slack_channel_id": _CHAT
    }


def test_a_scheduled_run_may_pin_where_the_scheduler_marked_it() -> None:
    """A job posts into the conversation it was created in; pinning is a write there."""
    metadata = {
        "slack_channel_id": _CHAT,
        METADATA_ORIGIN_KEY: ORIGIN_CRON_JOB,
    }
    assert slack_pin_request_metadata("__cron__", metadata) == metadata


@pytest.mark.parametrize(
    ("channel_id", "metadata"),
    [
        # Slack-looking metadata on another transport is not a Slack request.
        ("web", {"slack_channel_id": _OTHER}),
        # A cron request without the scheduler's own marker.
        ("__cron__", {"slack_channel_id": _OTHER}),
        # The marker without a conversation to write into.
        ("__cron__", {METADATA_ORIGIN_KEY: ORIGIN_CRON_JOB}),
        # An inbound Slack turn the connector stamped no conversation on.
        ("slack", {}),
        ("slack", None),
    ],
)
def test_everything_else_mounts_nothing(
    channel_id: str, metadata: "dict[str, Any] | None"
) -> None:
    assert slack_pin_request_metadata(channel_id, metadata) == {}


def test_the_history_policy_does_not_decide_whether_a_turn_may_pin() -> None:
    """A write is not governed by a setting about reads.

    ``permissions.tools`` is where pinning is allowed or refused. Reading the
    history word here would take the tool from every ``origin`` deployment,
    which is most of them, and hand it to an operator who widened their reads
    and asked for nothing else.
    """
    metadata = {"slack_channel_id": _CHAT, "slack_history_policy": "disabled"}
    assert slack_pin_request_metadata("slack", metadata) == metadata
