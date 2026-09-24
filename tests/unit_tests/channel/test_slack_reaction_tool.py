# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Unit tests for the request-scoped Slack reaction tool.

Two things are pinned here and they are worth telling apart. The first is
everything on our side of the Slack boundary: which turns reach the tool, which
message a call lands on, what the emoji argument is turned into, and what the
result says. The second is the handling of two Slack refusals that were
confirmed against the live API -- ``already_reacted`` and ``no_reaction`` --
which arrive as ``ok: false`` bodies and are reported here as success. The
fixtures below reproduce both shapes the SDK delivers them in.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

import pytest

import jiuwenswarm.common.config as config_module
from jiuwenswarm.agents.harness.common.tools.slack_reactions import (
    SlackReactionToolkit,
    slack_reaction_request_metadata,
)

_BOT_TOKEN = "xoxb-config-secret"
_CHANNEL = "C-OPS"
_INBOUND_TS = "1777423717.666499"


class _FakeClient:
    """Records the calls made, and answers every one of them ``ok``."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def reactions_add(self, **kwargs: Any) -> Any:
        self.calls.append({"method": "reactions.add", **kwargs})
        return {"ok": True}

    async def reactions_remove(self, **kwargs: Any) -> Any:
        self.calls.append({"method": "reactions.remove", **kwargs})
        return {"ok": True}


class _SlackApiError(Exception):
    """The shape slack_sdk raises: an exception holding the failed response."""

    def __init__(self, error: str) -> None:
        super().__init__("sanitized fake failure")
        self.response = SimpleNamespace(data={"ok": False, "error": error})


class _RefusingClient:
    """Refuses with one code, by whichever of the two routes is asked for."""

    def __init__(self, error: str, *, raised: bool = True) -> None:
        self.error = error
        self.raised = raised
        self.calls: list[dict[str, Any]] = []

    async def _answer(self, method: str, **kwargs: Any) -> Any:
        self.calls.append({"method": method, **kwargs})
        if self.raised:
            raise _SlackApiError(self.error)
        return {"ok": False, "error": self.error}

    async def reactions_add(self, **kwargs: Any) -> Any:
        return await self._answer("reactions.add", **kwargs)

    async def reactions_remove(self, **kwargs: Any) -> Any:
        return await self._answer("reactions.remove", **kwargs)


class _NeverCalledClient:
    async def reactions_add(self, **kwargs: Any) -> Any:
        raise AssertionError("the tool called Slack when it should not have")

    async def reactions_remove(self, **kwargs: Any) -> Any:
        raise AssertionError("the tool called Slack when it should not have")


@pytest.fixture(autouse=True)
def _config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        config_module,
        "get_config",
        lambda: {"channels": {"slack": {"bot_token": _BOT_TOKEN}}},
    )


def _metadata(**overrides: Any) -> dict[str, Any]:
    metadata = {
        "slack_channel_id": _CHANNEL,
        "message_ts": _INBOUND_TS,
        "timestamp_ms": 1777423717666,
    }
    metadata.update(overrides)
    return metadata


def _toolkit(
    client: Any,
    *,
    metadata: dict[str, Any] | None = None,
    provider: Any | None = None,
) -> SlackReactionToolkit:
    if provider is not None:
        return SlackReactionToolkit(metadata_provider=provider, client=client)
    return SlackReactionToolkit(
        metadata_provider=lambda: _metadata() if metadata is None else metadata,
        client=client,
    )


def _react(toolkit: SlackReactionToolkit, **kwargs: Any) -> dict[str, Any]:
    return json.loads(asyncio.run(toolkit.react_to_message(**kwargs)))


def _sent(client: Any) -> dict[str, Any]:
    assert len(client.calls) == 1, client.calls
    return client.calls[0]


# -- the emoji argument ------------------------------------------------------


def test_a_shortcode_is_sent_to_slack_unchanged() -> None:
    client = _FakeClient()
    result = _react(_toolkit(client), emoji="white_check_mark")

    assert result["ok"] is True
    assert result["emoji"] == "white_check_mark"
    assert _sent(client)["name"] == "white_check_mark"


def test_surrounding_colons_are_stripped() -> None:
    """``:tada:`` and ``tada`` are the same request; a model writes both."""
    client = _FakeClient()
    assert _react(_toolkit(client), emoji=":tada:")["emoji"] == "tada"
    assert _sent(client)["name"] == "tada"


def test_whitespace_and_case_are_settled_before_the_call() -> None:
    client = _FakeClient()
    assert _react(_toolkit(client), emoji="  :White_Check_Mark: ")["emoji"] == (
        "white_check_mark"
    )


def test_a_known_character_is_mapped_to_the_name_slack_knows() -> None:
    """Slack refuses a character outright, so the tool has to name it instead."""
    client = _FakeClient()
    assert _react(_toolkit(client), emoji="\N{PARTY POPPER}")["emoji"] == "tada"
    assert _sent(client)["name"] == "tada"


def test_the_connector_s_own_lifecycle_emoji_all_map() -> None:
    """The table is seeded from these, so a model can mark what the bot marks."""
    expected = {
        "\N{EYES}": "eyes",
        "\N{NO ENTRY SIGN}": "no_entry_sign",
        "\N{HOURGLASS WITH FLOWING SAND}": "hourglass_flowing_sand",
        "\N{HEAVY CHECK MARK}": "heavy_check_mark",
        "\N{CROSS MARK}": "x",
        "\N{BLACK SQUARE FOR STOP}": "black_square_for_stop",
    }
    for glyph, name in expected.items():
        client = _FakeClient()
        assert _react(_toolkit(client), emoji=glyph)["emoji"] == name


def test_slack_s_own_names_win_over_the_unicode_ones() -> None:
    """``+1`` and ``100`` are not what Unicode calls these, which is the point.

    A table generated from Unicode's names would be wrong here rather than
    merely incomplete, and wrong in the two entries a model reaches for most.
    """
    for glyph, name in (
        ("\N{THUMBS UP SIGN}", "+1"),
        ("\N{THUMBS DOWN SIGN}", "-1"),
        ("\N{HUNDRED POINTS SYMBOL}", "100"),
    ):
        client = _FakeClient()
        assert _react(_toolkit(client), emoji=glyph)["emoji"] == name


def test_a_presentation_selector_is_invisible_to_the_lookup() -> None:
    """U+FE0F is in what a person pastes and means nothing to Slack."""
    client = _FakeClient()
    assert _react(_toolkit(client), emoji="\N{HEAVY CHECK MARK}️")["emoji"] == (
        "heavy_check_mark"
    )


def test_a_skin_tone_becomes_slack_s_suffix() -> None:
    """Slack spells a tone on the name, not in the character."""
    client = _FakeClient()
    result = _react(_toolkit(client), emoji="\N{THUMBS UP SIGN}\U0001f3fd")

    assert result["emoji"] == "+1::skin-tone-4"
    assert _sent(client)["name"] == "+1::skin-tone-4"


def test_every_tone_modifier_has_its_number() -> None:
    tones = {
        "\U0001f3fb": 2,
        "\U0001f3fc": 3,
        "\U0001f3fd": 4,
        "\U0001f3fe": 5,
        "\U0001f3ff": 6,
    }
    for modifier, number in tones.items():
        client = _FakeClient()
        assert _react(_toolkit(client), emoji="\N{WAVING HAND SIGN}" + modifier)[
            "emoji"
        ] == f"wave::skin-tone-{number}"


def test_a_toned_shortcode_already_in_slack_s_form_survives_intact() -> None:
    """``:wave::skin-tone-2:`` loses its outer colons and nothing else."""
    client = _FakeClient()
    assert _react(_toolkit(client), emoji=":wave::skin-tone-2:")["emoji"] == (
        "wave::skin-tone-2"
    )


def test_an_unrecognised_character_is_refused_and_told_what_to_pass() -> None:
    """Better than Slack's ``invalid_name``: the caller can act on it at once."""
    client = _NeverCalledClient()
    result = _react(_toolkit(client), emoji="\N{MELTING FACE}")

    assert result["ok"] is False
    assert result["error"] == "emoji_character_not_recognised"
    assert "shortcode" in result["detail"]


def test_an_unrecognised_shortcode_is_not_refused() -> None:
    """It may be one of this workspace's own, which has no character at all."""
    client = _FakeClient()
    result = _react(_toolkit(client), emoji="party-parrot-deluxe")

    assert result["ok"] is True
    assert _sent(client)["name"] == "party-parrot-deluxe"


def test_an_empty_emoji_is_refused_without_calling_slack() -> None:
    for value in ("", "   ", "::"):
        result = _react(_toolkit(_NeverCalledClient()), emoji=value)
        assert result["error"] == "emoji_required"


# -- which message, and which conversation -----------------------------------


def test_the_conversation_comes_from_metadata_and_is_not_an_argument() -> None:
    client = _FakeClient()
    result = _react(_toolkit(client), emoji="eyes")

    assert result["chat_id"] == _CHANNEL
    assert _sent(client)["channel"] == _CHANNEL


def test_the_card_offers_no_way_to_name_another_conversation() -> None:
    (tool,) = SlackReactionToolkit().get_tools()
    properties = tool.card.input_params["properties"]

    assert set(properties) == {"emoji", "message_id", "remove"}
    assert tool.card.input_params["required"] == ["emoji"]
    assert "chat_id" not in json.dumps(tool.card.input_params)


def test_an_omitted_message_id_means_the_message_that_started_the_turn() -> None:
    """The default that removes the likeliest way to mark the wrong message."""
    client = _FakeClient()
    result = _react(_toolkit(client), emoji="eyes")

    assert result["message_id"] == _INBOUND_TS
    assert _sent(client)["timestamp"] == _INBOUND_TS


def test_the_inbound_ts_keeps_every_digit_slack_sent() -> None:
    """``timestamp_ms`` beside it is lossy and is never what gets used."""
    client = _FakeClient()
    _react(_toolkit(client), emoji="eyes")

    assert _sent(client)["timestamp"] == "1777423717.666499"


def test_a_given_message_id_is_used_instead_of_the_inbound_one() -> None:
    client = _FakeClient()
    result = _react(
        _toolkit(client), emoji="eyes", message_id="1710000000.000100"
    )

    assert result["message_id"] == "1710000000.000100"
    assert _sent(client)["timestamp"] == "1710000000.000100"


def test_a_turn_no_message_started_has_no_default_and_says_so() -> None:
    """A cron run's metadata carries a conversation and no message."""
    metadata = _metadata()
    metadata.pop("message_ts")
    result = _react(_toolkit(_NeverCalledClient(), metadata=metadata), emoji="eyes")

    assert result["ok"] is False
    assert result["error"] == "message_id_required"


def test_such_a_turn_reacts_perfectly_well_once_it_names_a_message() -> None:
    metadata = _metadata()
    metadata.pop("message_ts")
    client = _FakeClient()
    result = _react(
        _toolkit(client, metadata=metadata),
        emoji="white_check_mark",
        message_id="1710000000.000100",
    )

    assert result["ok"] is True
    assert _sent(client)["timestamp"] == "1710000000.000100"


# -- failing closed ----------------------------------------------------------


def test_metadata_without_a_conversation_is_refused_not_defaulted() -> None:
    result = _react(
        _toolkit(_NeverCalledClient(), metadata={"message_ts": _INBOUND_TS}),
        emoji="eyes",
    )

    assert result["ok"] is False
    assert result["error"] == "trusted_slack_channel_context_required"


def test_a_provider_that_raises_is_a_refusal_and_never_a_fallback() -> None:
    def _boom() -> dict[str, Any]:
        raise RuntimeError("the request context is gone")

    result = _react(
        _toolkit(_NeverCalledClient(), provider=_boom), emoji="eyes"
    )

    assert result["error"] == "trusted_slack_channel_context_required"


def test_a_provider_answering_something_that_is_not_metadata_is_refused() -> None:
    result = _react(
        _toolkit(_NeverCalledClient(), provider=lambda: "C-SOMEWHERE"),
        emoji="eyes",
    )

    assert result["error"] == "trusted_slack_channel_context_required"


def test_the_gate_needs_a_conversation_and_nothing_else() -> None:
    assert slack_reaction_request_metadata(None) == {}
    assert slack_reaction_request_metadata({}) == {}
    assert slack_reaction_request_metadata({"slack_channel_id": "  "}) == {}
    assert slack_reaction_request_metadata({"slack_channel_id": _CHANNEL}) == {
        "slack_channel_id": _CHANNEL
    }


def test_the_history_policy_word_does_not_decide_a_reaction() -> None:
    """Marking a message discloses nothing, so the read policy is not its gate."""
    assert slack_reaction_request_metadata(
        {"slack_channel_id": _CHANNEL, "slack_history_policy": "disabled"}
    )


def test_the_provider_is_read_per_call_and_not_captured() -> None:
    """One toolkit answers every request for the life of the process."""
    conversations = iter(["C-FIRST", "C-SECOND"])
    client = _FakeClient()
    toolkit = _toolkit(
        client,
        provider=lambda: {
            "slack_channel_id": next(conversations),
            "message_ts": _INBOUND_TS,
        },
    )

    assert _react(toolkit, emoji="eyes")["chat_id"] == "C-FIRST"
    assert _react(toolkit, emoji="eyes")["chat_id"] == "C-SECOND"


# -- removing, and the two refusals that are not failures --------------------


def test_remove_calls_the_other_method() -> None:
    client = _FakeClient()
    result = _react(_toolkit(client), emoji="eyes", remove=True)

    assert result["removed"] is True
    assert _sent(client)["method"] == "reactions.remove"


def test_adding_is_the_default_direction() -> None:
    client = _FakeClient()
    result = _react(_toolkit(client), emoji="eyes")

    assert result["removed"] is False
    assert _sent(client)["method"] == "reactions.add"


@pytest.mark.parametrize("raised", [True, False])
def test_already_reacted_is_success_so_marking_can_be_repeated(raised: bool) -> None:
    """A pass that marks what it handled must survive being run twice."""
    client = _RefusingClient("already_reacted", raised=raised)
    result = _react(_toolkit(client), emoji="white_check_mark")

    assert result["ok"] is True
    assert result["removed"] is False
    assert result["emoji"] == "white_check_mark"


@pytest.mark.parametrize("raised", [True, False])
def test_no_reaction_on_remove_is_success_for_the_same_reason(raised: bool) -> None:
    client = _RefusingClient("no_reaction", raised=raised)
    result = _react(_toolkit(client), emoji="eyes", remove=True)

    assert result["ok"] is True
    assert result["removed"] is True


def test_each_settled_refusal_only_settles_its_own_direction() -> None:
    """``no_reaction`` from an *add* is a real refusal, not an idempotent no-op."""
    add = _react(_toolkit(_RefusingClient("no_reaction")), emoji="eyes")
    assert add["ok"] is False
    assert add["error"] == "no_reaction"

    remove = _react(
        _toolkit(_RefusingClient("already_reacted")), emoji="eyes", remove=True
    )
    assert remove["ok"] is False
    assert remove["error"] == "already_reacted"


# -- what a real refusal says ------------------------------------------------


def test_a_refusal_a_caller_can_act_on_is_explained() -> None:
    for code, phrase in (
        ("invalid_name", "spelling"),
        ("missing_scope", "reactions:write"),
        ("message_not_found", "conversation"),
        ("not_in_channel", "invite"),
        ("is_archived", "archived"),
        ("too_many_reactions", "come off"),
    ):
        result = _react(_toolkit(_RefusingClient(code)), emoji="eyes")
        assert result["ok"] is False
        assert result["error"] == code
        assert phrase in result["detail"], code


def test_a_refusal_nothing_can_be_said_about_travels_as_the_code_alone() -> None:
    result = _react(_toolkit(_RefusingClient("fatal_error")), emoji="eyes")

    assert result == {"ok": False, "error": "fatal_error"}


def test_a_bot_token_in_a_refusal_never_reaches_the_result() -> None:
    result = _react(
        _toolkit(_RefusingClient(f"boom {_BOT_TOKEN}")), emoji="eyes"
    )

    assert _BOT_TOKEN not in json.dumps(result)


# -- the result shape --------------------------------------------------------


def test_the_result_is_this_deployment_s_shape_and_not_slack_s() -> None:
    result = _react(_toolkit(_FakeClient()), emoji=":eyes:")

    assert result == {
        "ok": True,
        "chat_id": _CHANNEL,
        "message_id": _INBOUND_TS,
        "emoji": "eyes",
        "removed": False,
    }


def test_the_reaction_list_is_not_fetched_afterwards() -> None:
    """``reactions.add`` answers ``ok`` and nothing else; no second call is made."""
    client = _FakeClient()
    _react(_toolkit(client), emoji="eyes")

    assert len(client.calls) == 1
    assert "reactions" not in json.dumps(_react(_toolkit(client), emoji="eyes"))


def test_the_wire_keeps_slack_s_argument_names() -> None:
    """The vocabulary is renamed on the card, never on the API."""
    client = _FakeClient()
    _react(_toolkit(client), emoji="eyes")

    assert set(_sent(client)) == {"method", "channel", "timestamp", "name"}


# -- the card ----------------------------------------------------------------


def test_the_card_says_what_the_default_message_is() -> None:
    (tool,) = SlackReactionToolkit().get_tools()
    description = tool.card.description

    assert "Omit message_id" in description
    assert "message_id has to be given" in description


def test_the_card_names_no_other_tool() -> None:
    """It is mounted on its own predicate, so a sibling may not be assumed."""
    (tool,) = SlackReactionToolkit().get_tools()
    description = tool.card.description

    for name in (
        "read_slack_conversation",
        "search_slack_workspace",
        "download_slack_file",
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
