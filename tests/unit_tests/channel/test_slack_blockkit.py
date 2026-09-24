"""Unit tests for the Slack connector's Block Kit affordance.

Kept out of ``test_slack_channel.py`` so the Block Kit surface can be read, and
reverted, on its own.
"""

from __future__ import annotations

import json
import logging
import pathlib
import time
from typing import Any

import pytest

from jiuwenswarm.common.schema.message import EventType, Message
from jiuwenswarm.gateway.channel_manager.base import RobotMessageRouter
from jiuwenswarm.common import slack_blocks
from jiuwenswarm.gateway.channel_manager.im_platforms.slack import (
    slack_connect,
)
from jiuwenswarm.gateway.channel_manager.im_platforms.slack.slack_connect import (
    SlackChannel,
    SlackChannelConfig,
    SlackDeliveryError,
    resolve_blockkit_allow_interactive,
    resolve_blockkit_allowed_block_types,
    render_tables_for_row_threshold,
    resolve_blockkit_tables_mode,
    resolve_render_tables,
)

MARKER = slack_connect._SLACK_BLOCKS_MARKER
OFF_MARKER = slack_connect._SLACK_BLOCKS_OFF_MARKER

TABLE = "\n".join(
    [
        "| Name | Count |",
        "| --- | --- |",
        "| alpha | 1 |",
        "| beta | 2 |",
    ]
)


def _channel(**settings) -> SlackChannel:
    return SlackChannel(
        SlackChannelConfig(enabled=True, **settings), RobotMessageRouter()
    )


def _message(content: str) -> Message:
    return Message(
        id="response-1",
        type="event",
        channel_id="slack",
        session_id="slack_T1_C1_1710000000.000100",
        params={},
        timestamp=time.time(),
        ok=True,
        payload={"content": content},
        event_type=EventType.CHAT_FINAL,
        metadata={"slack_channel_id": "C1"},
    )


class _RecordingClient:
    """Fake Slack client capturing every posted or edited payload."""

    def __init__(self, *, fail_from: int | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self._fail_from = fail_from

    async def chat_postMessage(self, **kwargs: Any) -> dict[str, str]:
        self.calls.append(kwargs)
        if self._fail_from is not None and len(self.calls) > self._fail_from:
            raise RuntimeError("channel_not_found")
        return {"ts": f"1710000099.{len(self.calls):06d}"}

    chat_update = chat_postMessage


async def _send(channel: SlackChannel, content: str) -> _RecordingClient:
    client = _RecordingClient()
    channel._client = client
    await channel.send(_message(content))
    return client


@pytest.fixture
def slack_logs(caplog):
    """Capture the connector's own log records.

    A local copy of the fixture in ``test_slack_channel.py`` and for the same
    reason: ``caplog`` handles the root logger, and a jiuwenswarm logger reaches
    it only while it still propagates, which the runtime's logging setup turns
    off. Sharing it would mean a conftest that exports one module's fixture to
    another, which is a larger commitment than fifteen lines.
    """
    target = logging.getLogger(slack_connect.__name__)
    previous_level = target.level
    previous_propagate = target.propagate
    target.addHandler(caplog.handler)
    target.propagate = False
    target.setLevel(logging.DEBUG)
    caplog.set_level(logging.DEBUG, logger=target.name)
    try:
        yield caplog
    finally:
        target.removeHandler(caplog.handler)
        target.propagate = previous_propagate
        target.setLevel(previous_level)


# --- the operator-level policy -------------------------------------------


def test_the_default_mode_renders_a_table_without_being_asked() -> None:
    assert SlackChannelConfig().blockkit_tables == slack_connect.BLOCKKIT_TABLES_AUTO
    assert _channel()._blockkit_tables_mode() == slack_connect.BLOCKKIT_TABLES_AUTO


def test_the_three_defaults_agree() -> None:
    """The dataclass, the raw-config reader and the runtime fallback are one value."""
    assert SlackChannelConfig().blockkit_tables == slack_connect.BLOCKKIT_TABLES_DEFAULT
    assert resolve_blockkit_tables_mode({}) == slack_connect.BLOCKKIT_TABLES_DEFAULT
    assert (
        _channel(blockkit_tables="nonsense")._blockkit_tables_mode()
        == slack_connect.BLOCKKIT_TABLES_DEFAULT
    )


def test_the_request_marker_is_named_nowhere_the_model_can_see_it() -> None:
    """Why the default cannot be ``marker``.

    ``marker`` waits for the reply to include a token that nothing in this
    repository ever puts in front of a model: no prompt builder, no rail, no
    skill. Shipped as the default it renders no tables at all. If a rail is ever
    added that teaches the marker, this test is the thing that should fail.
    """
    import pathlib

    root = pathlib.Path(slack_connect.__file__).resolve().parents[5]
    prompts = root / "jiuwenswarm" / "agents"
    assert prompts.is_dir()
    naming_it = [
        path
        for path in prompts.rglob("*")
        if path.is_file()
        and path.suffix in {".py", ".md", ".yaml", ".yml", ".json", ".txt"}
        and slack_connect._SLACK_BLOCKS_MARKER
        in path.read_text(encoding="utf-8", errors="ignore")
    ]
    assert naming_it == []


def test_every_documented_mode_resolves_to_itself() -> None:
    for mode in slack_connect.BLOCKKIT_TABLES_MODES:
        assert _channel(blockkit_tables=mode)._blockkit_tables_mode() == mode


def test_mode_is_case_and_whitespace_insensitive() -> None:
    assert (
        _channel(blockkit_tables="  AUTO ")._blockkit_tables_mode()
        == slack_connect.BLOCKKIT_TABLES_AUTO
    )


def test_an_unknown_mode_warns_and_falls_back_to_the_default(slack_logs) -> None:
    channel = _channel(blockkit_tables="sometimes")
    assert channel._blockkit_tables_mode() == slack_connect.BLOCKKIT_TABLES_DEFAULT
    assert "blockkit_tables" in slack_logs.text


def test_an_empty_mode_falls_back_without_warning(slack_logs) -> None:
    channel = _channel(blockkit_tables="")
    assert channel._blockkit_tables_mode() == slack_connect.BLOCKKIT_TABLES_DEFAULT
    assert "blockkit_tables" not in slack_logs.text


def test_the_mode_is_reported_in_the_channel_metadata() -> None:
    extra = _channel(blockkit_tables="auto").get_metadata().extra
    assert extra["blockkit_tables"] == slack_connect.BLOCKKIT_TABLES_AUTO


# --- which table block a table becomes ------------------------------------


def test_the_three_defaults_for_the_table_rendering_agree() -> None:
    """The dataclass, the raw-config reader and the runtime fallback are one value."""
    assert SlackChannelConfig().render_tables == slack_blocks.RENDER_TABLES_DEFAULT
    assert resolve_render_tables({}) == slack_blocks.RENDER_TABLES_DEFAULT
    assert _channel()._render_tables_mode() == slack_blocks.RENDER_TABLES_DEFAULT


def test_the_default_is_the_block_that_cannot_cost_a_reply_its_table() -> None:
    """Pinned as a value, not only as a constant, because it is a policy choice.

    ``data_table`` holds twice the rows and twice the characters of the plain
    block and is therefore the only value that renders every table a connector
    can build. Changing it is an operator-visible decision and should have to
    change this line.
    """
    assert slack_blocks.RENDER_TABLES_DEFAULT == "data_table"


def test_every_documented_rendering_resolves_to_itself() -> None:
    for mode in slack_blocks.RENDER_TABLES_MODES:
        assert resolve_render_tables({"render_tables": mode}) == mode
        assert _channel(render_tables=mode)._render_tables_mode() == mode


def test_the_rendering_is_case_and_whitespace_insensitive() -> None:
    assert (
        resolve_render_tables({"render_tables": "  DATA_TABLE "})
        == slack_blocks.RENDER_TABLES_DATA
    )


def test_an_unquoted_off_survives_yaml_reading_it_as_false() -> None:
    """The trap the mode keys are all taught: YAML 1.1 turns ``off`` into ``False``.

    Read as a plain string, the value that means "render no tables" would come
    out as the default that renders every one of them -- the misreading a config
    author cannot detect, because the word they wrote is the word they meant.
    """
    assert (
        resolve_render_tables({"render_tables": False})
        == slack_blocks.RENDER_TABLES_OFF
    )
    assert (
        resolve_render_tables({"render_tables": True})
        == slack_blocks.RENDER_TABLES_DEFAULT
    )


def test_an_unknown_rendering_warns_and_falls_back(slack_logs) -> None:
    channel = _channel(render_tables="fancy")
    assert channel._render_tables_mode() == slack_blocks.RENDER_TABLES_DEFAULT
    assert "render_tables" in slack_logs.text


def test_an_empty_rendering_falls_back_without_warning(slack_logs) -> None:
    assert _channel(render_tables="")._render_tables_mode() == (
        slack_blocks.RENDER_TABLES_DEFAULT
    )
    assert "render_tables" not in slack_logs.text


def test_the_rendering_is_reported_in_the_channel_metadata() -> None:
    extra = _channel(render_tables="basic").get_metadata().extra
    assert extra["render_tables"] == slack_blocks.RENDER_TABLES_BASIC


class TestTheRetiredRowThreshold:
    """What an operator who has not upgraded their config file gets.

    The threshold decided between the two blocks by counting rows and no longer
    exists. A config that still holds it is read rather than ignored, so the
    upgrade does not move a deployment onto a different rendering without
    anybody being told.
    """

    def test_a_zero_threshold_is_carried_over_exactly(self, caplog):
        """0 meant "every non-empty table is a data_table", which is a mode now."""
        with caplog.at_level(logging.WARNING, logger=slack_connect.logger.name):
            resolved = resolve_render_tables({"data_table_row_threshold": 0})
        assert resolved == slack_blocks.RENDER_TABLES_DATA
        assert "data_table_row_threshold" in caplog.text
        assert "render_tables" in caplog.text

    @pytest.mark.parametrize("threshold", [1, 12, 20, 5000])
    def test_any_other_row_count_is_read_as_the_plain_block(
        self, threshold: int, caplog
    ):
        """A positive threshold kept tables plain until they grew past it.

        Almost every table in a chat reply is under the threshold, so ``basic``
        is the reading that leaves the most replies looking as they did.
        """
        with caplog.at_level(logging.WARNING, logger=slack_connect.logger.name):
            resolved = resolve_render_tables(
                {"data_table_row_threshold": threshold}
            )
        assert resolved == slack_blocks.RENDER_TABLES_BASIC
        assert "data_table_row_threshold" in caplog.text
        assert "render_tables" in caplog.text

    @pytest.mark.parametrize("threshold", ["many", True, False, -5, None])
    def test_a_value_that_was_never_a_row_count_expresses_nothing(
        self, threshold, caplog
    ):
        """These all fell back to the module default before, and still do."""
        with caplog.at_level(logging.WARNING, logger=slack_connect.logger.name):
            resolved = resolve_render_tables(
                {"data_table_row_threshold": threshold}
            )
        assert resolved == slack_blocks.RENDER_TABLES_DEFAULT
        assert "is deprecated" not in caplog.text

    def test_the_new_key_wins_and_the_old_one_is_warned_about(self, caplog):
        """A file holding both has said what it wants with the key that exists."""
        with caplog.at_level(logging.WARNING, logger=slack_connect.logger.name):
            resolved = resolve_render_tables(
                {"render_tables": "off", "data_table_row_threshold": 12}
            )
        assert resolved == slack_blocks.RENDER_TABLES_OFF
        assert "data_table_row_threshold" in caplog.text

    def test_a_misspelt_new_key_does_not_fall_back_to_the_old_one(self, caplog):
        """A misspelling is a misspelling, not a request for the retired key."""
        with caplog.at_level(logging.WARNING, logger=slack_connect.logger.name):
            resolved = resolve_render_tables(
                {"render_tables": "datatable", "data_table_row_threshold": 0}
            )
        assert resolved == slack_blocks.RENDER_TABLES_DEFAULT

    def test_the_translation_is_a_function_the_config_upgrade_can_be_held_to(self):
        """Pins the mapping itself; ``test_config`` pins the upgrade against it."""
        assert render_tables_for_row_threshold(0) == "data_table"
        assert render_tables_for_row_threshold(20) == "basic"
        assert render_tables_for_row_threshold(-1) is None
        assert render_tables_for_row_threshold(True) is None
        assert render_tables_for_row_threshold("20") is None
        assert render_tables_for_row_threshold(None) is None


@pytest.mark.asyncio
async def test_basic_keeps_a_long_table_a_plain_table() -> None:
    """Size no longer decides: a table that used to page stays whole under basic."""
    rows = "\n".join(f"| r{index} | {index} |" for index in range(25))
    content = f"| Name | Count |\n| --- | --- |\n{rows}"
    client = await _send(_channel(render_tables="basic"), content)

    assert [block["type"] for block in client.calls[0]["blocks"]] == ["table"]


@pytest.mark.asyncio
async def test_data_table_makes_even_a_two_row_table_interactive() -> None:
    client = await _send(_channel(render_tables="data_table"), f"Totals:\n\n{TABLE}")

    assert [block["type"] for block in client.calls[0]["blocks"]] == [
        "section",
        "data_table",
    ]


@pytest.mark.asyncio
async def test_off_posts_the_table_as_the_text_it_was_written_as() -> None:
    """No table block, and no other rendering either: the reply is plain text."""
    client = await _send(_channel(render_tables="off"), f"Totals:\n\n{TABLE}")

    assert client.calls[0].get("blocks") in (None, [])
    assert "| alpha | 1 |" in client.calls[0]["text"]


@pytest.mark.asyncio
async def test_off_is_about_tables_and_leaves_a_chart_alone() -> None:
    """``blockkit_tables`` turns blocks off wholesale; this key never does.

    A reply that draws a chart beside a table still draws the chart, and the
    table travels in the same message as the prose it was written as.
    """
    content = (
        "Totals:\n\n"
        f"{TABLE}\n\n"
        "```mermaid\npie title Split\n  \"a\" : 1\n  \"b\" : 2\n```\n"
    )
    client = await _send(_channel(render_tables="off"), content)

    types = [block["type"] for block in client.calls[0]["blocks"]]
    assert "data_visualization" in types
    assert "table" not in types
    assert "data_table" not in types
    assert "| alpha | 1 |" in json.dumps(client.calls[0]["blocks"])


@pytest.mark.asyncio
async def test_a_table_too_large_for_basic_is_posted_as_text() -> None:
    """The plain block holds half of what the other one does, and is not trimmed.

    Over its row ceiling there is no plain rendering to send, so the whole
    message declines to text -- the module's all-or-nothing fallback, which
    costs the reader the formatting and never the content. It is the one cost of
    choosing ``basic``, and ``data_table`` is the one word that answers it.
    """
    rows = "\n".join(
        f"| r{index} | {index} |"
        for index in range(slack_blocks.MAX_PLAIN_TABLE_ROWS + 5)
    )
    content = f"| Name | Count |\n| --- | --- |\n{rows}"

    plain = await _send(_channel(render_tables="basic"), content)
    assert plain.calls[0].get("blocks") in (None, [])
    assert "| r99 | 99 |" in plain.calls[0]["text"]

    interactive = await _send(_channel(render_tables="data_table"), content)
    assert [block["type"] for block in interactive.calls[0]["blocks"]] == [
        "data_table"
    ]


# --- what a hand-written fence may hold ----------------------------------


BUTTON_BLOCK = {
    "type": "section",
    "text": {"type": "mrkdwn", "text": "Approve this?"},
    "accessory": {
        "type": "button",
        "text": {"type": "plain_text", "text": "Approve"},
        "action_id": "forged_approve",
    },
}


def _blockkit_fence(payload: Any) -> str:
    return "```blockkit\n" + json.dumps(payload) + "\n```"


def test_neither_control_restricts_anything_it_was_not_asked_to() -> None:
    """The dataclass default, the raw-config reader and the runtime accessor agree."""
    assert SlackChannelConfig().blockkit_allowed_block_types == ()
    assert resolve_blockkit_allowed_block_types({}) == ()
    assert _channel()._blockkit_allowed_block_types() == ()

    assert SlackChannelConfig().blockkit_allow_interactive is False
    assert resolve_blockkit_allow_interactive({}) is False
    assert _channel()._blockkit_allow_interactive() is False


def test_both_controls_are_reported_in_the_channel_metadata() -> None:
    extra = _channel(
        blockkit_allowed_block_types=("data_table",), blockkit_allow_interactive=True
    ).get_metadata().extra
    assert extra["blockkit_allowed_block_types"] == ["data_table"]
    assert extra["blockkit_allow_interactive"] is True


@pytest.mark.asyncio
async def test_an_unrestricted_channel_forwards_a_type_the_old_list_refused() -> None:
    """``section`` was refused outright before; nothing refuses it now."""
    fence = _blockkit_fence({"type": "section", "text": {"type": "mrkdwn", "text": "hi"}})
    client = await _send(_channel(), f"Notes follow.\n\n{fence}")

    assert [block["type"] for block in client.calls[0]["blocks"]] == [
        "section",
        "section",
    ]


@pytest.mark.asyncio
async def test_a_configured_list_restricts_the_channel_to_what_it_names() -> None:
    fence = _blockkit_fence({"type": "section", "text": {"type": "mrkdwn", "text": "hi"}})
    client = await _send(
        _channel(blockkit_allowed_block_types=["data_visualization"]),
        f"Notes follow.\n\n{fence}",
    )

    assert "blocks" not in client.calls[0]
    assert "```blockkit" in client.calls[0]["text"]


@pytest.mark.asyncio
async def test_a_forged_approval_prompt_is_refused_by_default() -> None:
    """The reason the two controls are separate, exercised end to end.

    Nothing restricts ``section`` any more, so this reaches the channel only if
    opening up block types also opened up what a reader can be asked to click.
    """
    fence = _blockkit_fence(BUTTON_BLOCK)
    client = await _send(_channel(), f"Please confirm.\n\n{fence}")

    assert "blocks" not in client.calls[0]
    assert "forged_approve" in client.calls[0]["text"]


@pytest.mark.asyncio
async def test_naming_a_type_does_not_admit_its_interactive_elements() -> None:
    """An operator allowing ``section`` has not thereby allowed buttons."""
    fence = _blockkit_fence(BUTTON_BLOCK)
    client = await _send(
        _channel(blockkit_allowed_block_types=["section"]), f"Please confirm.\n\n{fence}"
    )

    assert "blocks" not in client.calls[0]


@pytest.mark.asyncio
async def test_interactive_elements_arrive_only_when_asked_for_by_name() -> None:
    fence = _blockkit_fence(BUTTON_BLOCK)
    client = await _send(
        _channel(blockkit_allow_interactive=True), f"Please confirm.\n\n{fence}"
    )

    assert [block["type"] for block in client.calls[0]["blocks"]] == [
        "section",
        "section",
    ]
    assert client.calls[0]["blocks"][-1]["accessory"]["type"] == "button"


class TestResolveBlockkitAllowedBlockTypes:
    """Warn and fall back to "no restriction", never raise."""

    def test_absent_and_empty_are_the_same_answer(self):
        assert resolve_blockkit_allowed_block_types({}) == ()
        assert resolve_blockkit_allowed_block_types(
            {"blockkit_allowed_block_types": []}
        ) == ()
        assert resolve_blockkit_allowed_block_types(
            {"blockkit_allowed_block_types": None}
        ) == ()

    def test_names_are_trimmed_lower_cased_and_deduplicated(self):
        assert resolve_blockkit_allowed_block_types(
            {"blockkit_allowed_block_types": [" Data_Table ", "data_table", "SECTION"]}
        ) == ("data_table", "section")

    def test_one_name_written_without_a_list_is_read_as_a_list_of_one(self):
        """The mistake the YAML invites, and a harmless one to accept."""
        assert resolve_blockkit_allowed_block_types(
            {"blockkit_allowed_block_types": "data_table"}
        ) == ("data_table",)

    def test_an_entry_that_is_not_a_name_is_dropped_with_a_warning(self, caplog):
        with caplog.at_level(logging.WARNING, logger=slack_connect.logger.name):
            resolved = resolve_blockkit_allowed_block_types(
                {"blockkit_allowed_block_types": ["data_table", 7, "", None]}
            )
        assert resolved == ("data_table",)
        assert "blockkit_allowed_block_types" in caplog.text

    def test_a_value_that_is_not_a_list_warns_and_restricts_nothing(self, caplog):
        with caplog.at_level(logging.WARNING, logger=slack_connect.logger.name):
            resolved = resolve_blockkit_allowed_block_types(
                {"blockkit_allowed_block_types": {"data_table": True}}
            )
        assert resolved == ()
        assert "blockkit_allowed_block_types" in caplog.text

    def test_a_list_naming_nothing_usable_warns_and_restricts_nothing(self, caplog):
        """Fails open on purpose: a typo must not cost every block in the channel."""
        with caplog.at_level(logging.WARNING, logger=slack_connect.logger.name):
            resolved = resolve_blockkit_allowed_block_types(
                {"blockkit_allowed_block_types": [None, 7]}
            )
        assert resolved == ()
        assert "blockkit_allowed_block_types" in caplog.text


class TestResolveBlockkitAllowInteractive:
    """Fails closed, which is the opposite of every other key here."""

    def test_absent_is_off(self):
        assert resolve_blockkit_allow_interactive({}) is False

    def test_a_boolean_passes_through(self):
        assert resolve_blockkit_allow_interactive(
            {"blockkit_allow_interactive": True}
        ) is True
        assert resolve_blockkit_allow_interactive(
            {"blockkit_allow_interactive": False}
        ) is False

    @pytest.mark.parametrize("raw", ["true", " YES ", "on", "1"])
    def test_the_spellings_of_yes_are_read(self, raw: str):
        assert resolve_blockkit_allow_interactive(
            {"blockkit_allow_interactive": raw}
        ) is True

    @pytest.mark.parametrize("raw", ["false", "no", "off", "0", ""])
    def test_the_spellings_of_no_are_read(self, raw: str):
        assert resolve_blockkit_allow_interactive(
            {"blockkit_allow_interactive": raw}
        ) is False

    def test_anything_unreadable_warns_and_stays_off(self, caplog):
        """A value nobody can interpret must not decide what a reader can click."""
        with caplog.at_level(logging.WARNING, logger=slack_connect.logger.name):
            resolved = resolve_blockkit_allow_interactive(
                {"blockkit_allow_interactive": "sometimes"}
            )
        assert resolved is False
        assert "blockkit_allow_interactive" in caplog.text


def test_the_shipped_templates_all_carry_both_keys() -> None:
    """A key absent from a template is deleted from an operator's config on upgrade."""
    import jiuwenswarm
    import yaml

    directory = pathlib.Path(jiuwenswarm.__file__).parent / "resources"
    templates = [
        "config.yaml",
        "config.team.distributed.leader.yaml",
        "config.team.distributed.teammate.yaml",
    ]
    for name in templates:
        slack = yaml.safe_load((directory / name).read_text())["channels"]["slack"]
        assert slack["blockkit_allowed_block_types"] == []
        assert slack["blockkit_allow_interactive"] is False


# --- the per-message affordance ------------------------------------------


def test_content_without_a_marker_is_returned_byte_for_byte() -> None:
    """The unused path must not be reshaped by a feature it did not use."""
    content = "  Leading and trailing space, and\n\n\n three blank lines.  "
    assert SlackChannel._extract_block_request(content) == (content, None)


def test_the_marker_requests_blocks_and_is_removed() -> None:
    cleaned, requested = SlackChannel._extract_block_request(
        f"Here is the table.\n\n{MARKER}\n\n| a |\n| --- |\n| 1 |"
    )
    assert requested is True
    assert MARKER not in cleaned
    assert cleaned == "Here is the table.\n\n| a |\n| --- |\n| 1 |"


def test_the_off_marker_declines_blocks_and_is_removed() -> None:
    cleaned, requested = SlackChannel._extract_block_request(f"{OFF_MARKER}\nplain")
    assert requested is False
    assert cleaned == "plain"


def test_a_refusal_wins_when_a_reply_carries_both_markers() -> None:
    cleaned, requested = SlackChannel._extract_block_request(
        f"{MARKER}\ntext\n{OFF_MARKER}"
    )
    assert requested is False
    assert MARKER not in cleaned and OFF_MARKER not in cleaned


def test_an_inline_marker_is_removed_without_eating_the_sentence() -> None:
    cleaned, requested = SlackChannel._extract_block_request(f"before {MARKER} after")
    assert requested is True
    assert cleaned == "before  after"


def test_the_off_marker_is_not_mistaken_for_the_request_marker() -> None:
    """The two spellings share a prefix; only a full match may count."""
    assert MARKER not in OFF_MARKER
    _, requested = SlackChannel._extract_block_request(OFF_MARKER)
    assert requested is False


def test_the_thread_details_marker_is_left_for_its_own_reader() -> None:
    content = f"brief\n{slack_connect._SLACK_THREAD_DETAILS_MARKER}\ndetail\n{MARKER}"
    cleaned, requested = SlackChannel._extract_block_request(content)
    assert requested is True
    assert slack_connect._SLACK_THREAD_DETAILS_MARKER in cleaned


# --- the send path -------------------------------------------------------


@pytest.mark.asyncio
async def test_a_prose_answer_sends_no_blocks_at_all() -> None:
    """The byte-identical requirement: an unrelated answer must not gain a key."""
    client = await _send(_channel(), "A plain answer with *bold* and a bullet.")

    assert len(client.calls) == 1
    assert client.calls[0] == {
        "channel": "C1",
        "text": "A plain answer with *bold* and a bullet.",
    }


@pytest.mark.asyncio
async def test_a_table_needs_no_marker_by_default() -> None:
    """Detection is on by default; writing a table is the request."""
    client = await _send(_channel(), f"Totals:\n\n{TABLE}")

    assert [block["type"] for block in client.calls[0]["blocks"]] == [
        "section",
        slack_blocks.RENDER_TABLES_DEFAULT,
    ]
    # The text field still holds the whole answer, for notifications.
    assert "| alpha | 1 |" in client.calls[0]["text"]


@pytest.mark.asyncio
async def test_marker_mode_still_waits_to_be_asked() -> None:
    """The mode is kept, and still does nothing until something emits the marker."""
    client = await _send(_channel(blockkit_tables="marker"), f"Totals:\n\n{TABLE}")

    assert "blocks" not in client.calls[0]
    assert "| alpha | 1 |" in client.calls[0]["text"]


@pytest.mark.asyncio
async def test_the_marker_turns_a_table_into_blocks_with_a_text_fallback() -> None:
    """``data_table``, not ``table``: the marker is also the escalation.

    Two rows would render as a plain ``table`` if nobody asked. Asking is what
    buys paging, sorting and filtering on a table too short to earn them by
    size, which is the only way an author can reach them at all.
    """
    client = await _send(_channel(), f"{MARKER}\n\n## Totals\n\n{TABLE}")

    call = client.calls[0]
    assert [block["type"] for block in call["blocks"]] == ["section", "data_table"]
    # The heading went through the existing normaliser, not a second one.
    assert call["blocks"][0]["text"] == {"type": "mrkdwn", "text": "*Totals*"}
    assert [[cell["text"] for cell in row] for row in call["blocks"][1]["rows"]] == [
        ["Name", "Count"],
        ["alpha", "1"],
        ["beta", "2"],
    ]
    # Short enough that the pager it just earned never appears, which is what
    # keeps the escalation from making a two-row table worse to read.
    assert call["blocks"][1]["page_size"] == 2

    # text is always supplied: it is the notification and the accessible copy.
    assert call["text"].startswith("*Totals*")
    assert "| alpha | 1 |" in call["text"]
    assert MARKER not in call["text"]


@pytest.mark.asyncio
async def test_the_marker_on_an_answer_with_no_table_changes_nothing() -> None:
    client = await _send(_channel(), f"{MARKER}\n\nNo table here.")

    assert "blocks" not in client.calls[0]
    assert client.calls[0]["text"] == "No table here."


@pytest.mark.asyncio
async def test_auto_mode_renders_a_table_nobody_asked_about() -> None:
    client = await _send(_channel(blockkit_tables="auto"), f"Totals:\n\n{TABLE}")

    assert [block["type"] for block in client.calls[0]["blocks"]] == [
        "section",
        slack_blocks.RENDER_TABLES_DEFAULT,
    ]


@pytest.mark.asyncio
async def test_auto_mode_honours_a_per_message_opt_out() -> None:
    client = await _send(_channel(blockkit_tables="auto"), f"{OFF_MARKER}\n\n{TABLE}")

    assert "blocks" not in client.calls[0]
    assert OFF_MARKER not in client.calls[0]["text"]


@pytest.mark.asyncio
async def test_off_mode_ignores_the_marker_but_still_strips_it() -> None:
    client = await _send(_channel(blockkit_tables="off"), f"{MARKER}\n\n{TABLE}")

    assert "blocks" not in client.calls[0]
    assert MARKER not in client.calls[0]["text"]


@pytest.mark.asyncio
async def test_a_reply_that_is_only_a_marker_is_a_no_op() -> None:
    channel = _channel()
    client = _RecordingClient()
    channel._client = client

    await channel.send(_message(MARKER))

    assert client.calls == []


@pytest.mark.asyncio
async def test_a_delivery_that_used_blocks_says_so_in_the_log(slack_logs) -> None:
    await _send(_channel(), f"{MARKER}\n\n{TABLE}")

    lines = [
        record.getMessage()
        for record in slack_logs.records
        if record.levelno == logging.INFO
    ]
    assert lines == [
        "[SlackChannel] delivered text: channel=C1 ts=1710000099.000001 "
        "chunk=1/1 mode=post+blocks"
    ]


# --- limits on the send path ---------------------------------------------


@pytest.mark.asyncio
async def test_a_table_over_slacks_limits_is_delivered_as_text() -> None:
    """Blocks cannot be chunked, so the text path keeps the content intact."""
    rows = "\n".join(f"| r{index} | {index} |" for index in range(200))
    client = await _send(
        _channel(), f"{MARKER}\n| Name | Count |\n| --- | --- |\n{rows}"
    )

    assert len(client.calls) == 1
    assert "blocks" not in client.calls[0]
    assert "r199" in client.calls[0]["text"]


@pytest.mark.asyncio
async def test_every_chunk_of_a_split_reply_is_within_the_block_limits() -> None:
    filler = "\n\n".join(["prose " * 200] * 40)
    client = await _send(_channel(), f"{MARKER}\n\n{TABLE}\n\n{filler}\n\n{TABLE}")

    for call in client.calls:
        blocks = call.get("blocks")
        if blocks is None:
            continue
        assert len(blocks) <= slack_blocks.MAX_BLOCKS_PER_MESSAGE
        for block in blocks:
            if block["type"] == "section":
                assert (
                    len(block["text"]["text"])
                    <= slack_blocks.MAX_SECTION_TEXT_LENGTH
                )


# --- one budget per message ----------------------------------------------


def _wide_table(tag: str, rows: int = 100, width: int = 110) -> str:
    """A table whose cells total well under Slack's per-message character cap."""
    body = "\n".join(f"| {tag}{index:03d}{'.' * width} | {index} |" for index in range(rows))
    return f"| Name | Count |\n| --- | --- |\n{body}"


def test_the_two_tables_are_each_under_the_cap_and_over_it_together() -> None:
    """The premise of the two tests below, asserted rather than assumed."""
    one = slack_blocks.render_blocks(_wide_table("a"))
    assert one is not None, "a single table must render, or the pair proves nothing"
    characters = sum(
        len(cell["text"])
        for block in one
        if block["type"] in {"table", "data_table"}
        for row in block["rows"]
        for cell in row
    )
    assert characters < slack_blocks.MAX_TABLE_CHARACTERS_PER_MESSAGE
    assert 2 * characters > slack_blocks.MAX_TABLE_CHARACTERS_PER_MESSAGE


@pytest.mark.asyncio
async def test_two_tables_in_one_message_lose_the_rendering_together() -> None:
    """The cliff: the budget is shared, so a breach degrades both tables at once."""
    client = await _send(_channel(), f"{_wide_table('a')}\n\n{_wide_table('b')}")

    assert len(client.calls) == 1
    assert "blocks" not in client.calls[0]
    assert "| a000" in client.calls[0]["text"] and "| b000" in client.calls[0]["text"]


@pytest.mark.asyncio
async def test_the_same_two_tables_render_once_a_boundary_separates_them() -> None:
    """The whole point of a second message: a second budget.

    Same two tables, same connector, one marker added. Nothing about the tables
    changed, so a rendering here and not above is evidence that the limit is
    counted per message and that a boundary buys a fresh one.
    """
    content = (
        f"brief\n{slack_connect._SLACK_THREAD_DETAILS_MARKER}\n"
        f"{_wide_table('a')}\n{slack_connect._SLACK_THREAD_DETAILS_MARKER}\n"
        f"{_wide_table('b')}"
    )
    client = await _send(_channel(), content)

    assert len(client.calls) == 3
    assert "blocks" not in client.calls[0]
    for call in client.calls[1:]:
        assert [block["type"] for block in call["blocks"]] == ["data_table"]


@pytest.mark.asyncio
async def test_a_boundary_does_not_rescue_a_single_table_over_the_row_limit() -> None:
    """The row limit is per table, so splitting the message cannot help it."""
    rows = "\n".join(f"| r{index} | {index} |" for index in range(200))
    table = f"| Name | Count |\n| --- | --- |\n{rows}"
    client = await _send(
        _channel(), f"brief\n{slack_connect._SLACK_THREAD_DETAILS_MARKER}\n{table}"
    )

    assert len(client.calls) == 2
    assert "blocks" not in client.calls[1]
    assert "r199" in client.calls[1]["text"]


@pytest.mark.asyncio
async def test_a_ragged_table_is_delivered_rather_than_raising() -> None:
    ragged = "| A | B |\n| --- | --- |\n| only-one |\n| x | y | z |"
    client = await _send(_channel(), f"{MARKER}\n\n{ragged}")

    rows = [
        [cell["text"] for cell in row]
        for row in client.calls[0]["blocks"][0]["rows"]
    ]
    assert rows == [["A", "B"], ["only-one", ""], ["x", "y"]]


# --- the delivery contract -----------------------------------------------


@pytest.mark.asyncio
async def test_a_failed_blocks_post_raises_like_any_other_delivery() -> None:
    channel = _channel()
    channel._client = _RecordingClient(fail_from=0)

    with pytest.raises(SlackDeliveryError) as excinfo:
        await channel.send(_message(f"{MARKER}\n\n{TABLE}"))

    assert excinfo.value.channel_id == "C1"
    assert excinfo.value.chunks_sent == 0
    assert "channel_not_found" in str(excinfo.value)


@pytest.mark.asyncio
async def test_a_partial_blocks_delivery_still_reports_its_progress() -> None:
    channel = _channel()
    channel._client = _RecordingClient(fail_from=1)
    content = f"{MARKER}\n\n{TABLE}\n\n{'filler ' * 6000}"

    with pytest.raises(SlackDeliveryError) as excinfo:
        await channel.send(_message(content))

    assert excinfo.value.chunks_sent == 1
    assert excinfo.value.chunks_total > 1


# --- a rendering Slack refuses -------------------------------------------


class _SlackApiError(RuntimeError):
    """Shaped like ``slack_sdk.errors.SlackApiError``: a payload on ``response``."""

    def __init__(self, code: str) -> None:
        super().__init__(
            "The request to the Slack API failed. "
            "(url: https://slack.com/api/chat.postMessage, status: 200) "
            f"The server responded with: {{'ok': False, 'error': '{code}'}}"
        )
        self.response = {"ok": False, "error": code}


class _BlockRefusingClient:
    """Accepts a payload only once it holds no ``blocks``."""

    def __init__(self, code: str = "invalid_blocks", *, always: bool = False) -> None:
        self.calls: list[dict[str, Any]] = []
        self._code = code
        self._always = always

    async def chat_postMessage(self, **kwargs: Any) -> dict[str, str]:
        self.calls.append(kwargs)
        if self._always or "blocks" in kwargs:
            raise _SlackApiError(self._code)
        return {"ts": f"1710000099.{len(self.calls):06d}"}

    chat_update = chat_postMessage


@pytest.mark.parametrize(
    "code", ["invalid_blocks", "invalid_blocks_format", "msg_blocks_too_long"]
)
@pytest.mark.asyncio
async def test_a_refused_rendering_is_delivered_as_plain_text(code: str) -> None:
    """The text in the refused payload was always enough to deliver the message."""
    channel = _channel(blockkit_tables="auto")
    client = _BlockRefusingClient(code)
    channel._client = client

    await channel.send(_message(f"Totals:\n\n{TABLE}"))

    assert len(client.calls) == 2
    assert "blocks" in client.calls[0]
    assert "blocks" not in client.calls[1]
    # The retry still holds the whole chunk, so nothing of the answer is
    # lost; what is new is the line under it saying the formatting was refused.
    assert client.calls[1]["text"].startswith(client.calls[0]["text"])
    assert "alpha" in client.calls[1]["text"]
    assert slack_connect._BLOCK_FAILURE_DATA_NOTICE in client.calls[1]["text"]


@pytest.mark.asyncio
async def test_a_refused_rendering_is_logged_at_warning(slack_logs) -> None:
    channel = _channel(blockkit_tables="auto")
    channel._client = _BlockRefusingClient("msg_blocks_too_long")

    await channel.send(_message(f"Totals:\n\n{TABLE}"))

    warnings = [
        record.getMessage()
        for record in slack_logs.records
        if record.levelno == logging.WARNING
    ]
    assert len(warnings) == 1
    line = warnings[0]
    assert line.startswith(
        "Slack rejected the Block Kit rendering (msg_blocks_too_long)"
    )
    # Slack said nothing beyond the code here, and the line says so rather than
    # leaving the operator to wonder whether it was dropped.
    assert "no detail" in line
    # And the payload is still identified, by the block types it was made of.
    assert "data_table" in line
    assert line.endswith("delivering this chunk as plain text")


@pytest.mark.asyncio
async def test_the_fallback_reports_the_delivery_as_text_not_blocks(
    slack_logs,
) -> None:
    channel = _channel(blockkit_tables="auto")
    channel._client = _BlockRefusingClient()

    await channel.send(_message(f"Totals:\n\n{TABLE}"))

    lines = [
        record.getMessage()
        for record in slack_logs.records
        if record.levelno == logging.INFO
    ]
    assert lines == [
        "[SlackChannel] delivered text: channel=C1 ts=1710000099.000002 "
        "chunk=1/1 mode=post"
    ]


@pytest.mark.parametrize("code", ["channel_not_found", "invalid_auth"])
@pytest.mark.asyncio
async def test_an_error_that_is_not_about_the_blocks_still_fails(code: str) -> None:
    """Retrying these without blocks would only delay the error the caller needs."""
    channel = _channel(blockkit_tables="auto")
    client = _BlockRefusingClient(code, always=True)
    channel._client = client

    with pytest.raises(SlackDeliveryError) as excinfo:
        await channel.send(_message(f"Totals:\n\n{TABLE}"))

    assert code in str(excinfo.value)
    assert len(client.calls) == 1


@pytest.mark.asyncio
async def test_the_rendering_is_dropped_once_and_not_retried_forever() -> None:
    channel = _channel(blockkit_tables="auto")
    client = _BlockRefusingClient("invalid_blocks", always=True)
    channel._client = client

    with pytest.raises(SlackDeliveryError):
        await channel.send(_message(f"Totals:\n\n{TABLE}"))

    # One attempt with the rendering, one without, and then it gives up.
    assert len(client.calls) == 2
    assert "blocks" in client.calls[0]
    assert "blocks" not in client.calls[1]


@pytest.mark.asyncio
async def test_a_reply_that_never_had_blocks_is_not_retried() -> None:
    channel = _channel(blockkit_tables="auto")
    client = _BlockRefusingClient("invalid_blocks", always=True)
    channel._client = client

    with pytest.raises(SlackDeliveryError):
        await channel.send(_message("Just prose, no table."))

    assert len(client.calls) == 1


def test_only_the_block_specific_errors_are_recognised() -> None:
    for code in ("invalid_blocks", "invalid_blocks_format", "msg_blocks_too_long"):
        assert slack_connect.rejected_blocks_error(_SlackApiError(code)) == code
    for code in ("channel_not_found", "invalid_auth", "ratelimited", "msg_too_long"):
        assert slack_connect.rejected_blocks_error(_SlackApiError(code)) is None
    # An exception with no Slack payload at all must not be mistaken for one.
    assert slack_connect.rejected_blocks_error(RuntimeError("boom")) is None


# --- streaming -----------------------------------------------------------


def test_a_streamed_snapshot_stays_text_only() -> None:
    """A table rewritten once a second is worse to watch than the same text."""
    snapshot = _channel()._stream_snapshot(f"{MARKER}\n\n{TABLE}")

    assert isinstance(snapshot, str)
    assert MARKER not in snapshot
    assert "| alpha | 1 |" in snapshot


@pytest.mark.asyncio
async def test_the_closing_rewrite_of_a_streamed_reply_carries_the_blocks() -> None:
    channel = _channel(enable_streaming=True)
    client = _RecordingClient()
    channel._client = client
    final = _message(f"{MARKER}\n\n{TABLE}")

    for fragment in (f"{MARKER}\n\n", TABLE[:20], TABLE[20:]):
        await channel.send(
            Message(
                id=final.id,
                type="event",
                channel_id="slack",
                session_id=final.session_id,
                params={},
                timestamp=time.time(),
                ok=True,
                payload={"content": fragment},
                event_type=EventType.CHAT_DELTA,
                metadata={"slack_channel_id": "C1"},
            )
        )
    await channel.send(final)

    # The message is opened and edited as text; only the closing rewrite,
    # which is the delivery send() is contractually responsible for, has blocks.
    assert "blocks" not in client.calls[0]
    assert [block["type"] for block in client.calls[-1]["blocks"]] == ["data_table"]
    # An edit of the streamed message, not a second copy posted underneath it.
    assert client.calls[-1]["ts"] == "1710000099.000001"


class TestResolveBlockkitTablesMode:
    """The config value survives YAML's boolean coercion of the mode names."""

    def test_absent_key_uses_the_default_mode(self):
        assert resolve_blockkit_tables_mode({}) == "auto"

    def test_explicit_mode_names_pass_through(self):
        for mode in ("off", "marker", "auto"):
            assert resolve_blockkit_tables_mode({"blockkit_tables": mode}) == mode

    def test_unquoted_off_still_means_off(self):
        # YAML 1.1 parses off/no/false as False before this is reached. Reading
        # the value as a string would resolve it to the default, which renders --
        # enabling the feature the author wrote the word to disable.
        assert resolve_blockkit_tables_mode({"blockkit_tables": False}) == "off"

    def test_unquoted_on_resolves_to_the_default_rather_than_a_guess(self):
        # True names no single mode: it says "enabled" without saying which.
        assert resolve_blockkit_tables_mode({"blockkit_tables": True}) == "auto"

    def test_unknown_value_warns_and_falls_back(self, caplog):
        with caplog.at_level(logging.WARNING, logger=slack_connect.logger.name):
            assert resolve_blockkit_tables_mode({"blockkit_tables": "tables"}) == "auto"
        assert "blockkit_tables" in caplog.text

    def test_case_and_padding_are_ignored(self):
        assert resolve_blockkit_tables_mode({"blockkit_tables": "  OFF "}) == "off"


@pytest.mark.asyncio
async def test_a_table_cut_by_the_streamed_chunk_ceiling_posts_as_pipes() -> None:
    """The limit the raised table budget does not remove, pinned rather than claimed.

    ``_split_text`` runs before ``_blocks_for``, and the first chunk of a
    streamed reply is cut at the 4,000 characters ``chat.update`` accepts, not
    at the 38,000 a fresh post does. A table crossing that cut loses its
    delimiter row, so the half that follows is no longer a table to the parser
    and arrives as raw pipe characters.

    Asserted here so that "the table budget is 20,000 now" is never read as
    true on every path. Fixing it means splitting text after rendering rather
    than before it, which is the connector's chunker and not the renderer.
    """
    channel = _channel(enable_streaming=True)
    client = _RecordingClient()
    channel._client = client
    rows = "\n".join(
        f"| row{index:03d} | {'note ' * 12}{index} |" for index in range(100)
    )
    content = f"*Roster*\n| Name | Count |\n| --- | --- |\n{rows}"

    final = _message(content)
    await channel.send(
        Message(
            id=final.id,
            type="event",
            channel_id="slack",
            session_id=final.session_id,
            params={},
            timestamp=time.time(),
            ok=True,
            payload={"content": "prose "},
            event_type=EventType.CHAT_DELTA,
            metadata={"slack_channel_id": "C1"},
        )
    )
    await channel.send(final)

    # The head of the table is cut off at the ceiling and renders; the tail
    # begins mid-table, has no delimiter row above it, and is posted as pipes.
    # The opening of the stream, the rewrite that closes it, and one more post.
    assert len(client.calls) == 3
    head, tail = client.calls[-2:]
    assert [block["type"] for block in head["blocks"]] == ["section", "data_table"]
    assert "blocks" not in tail
    assert tail["text"].startswith("| row")
    assert "| row099 |" in tail["text"]


# --- the advanced tier through the connector -----------------------------


CHART_FENCE = "\n".join(
    [
        "```blockkit",
        '{"type": "data_visualization", "title": "Reviews",',
        ' "chart": {"type": "pie",',
        '  "segments": [{"label": "merged", "value": 7}]}}',
        "```",
    ]
)


@pytest.mark.asyncio
async def test_a_blockkit_fence_reaches_the_channel_as_a_chart() -> None:
    client = await _send(_channel(), f"Seven merged this week.\n\n{CHART_FENCE}")

    assert len(client.calls) == 1
    assert [block["type"] for block in client.calls[0]["blocks"]] == [
        "section",
        "data_visualization",
    ]


@pytest.mark.asyncio
async def test_the_operator_kill_switch_covers_the_advanced_tier_too() -> None:
    """``off`` has to keep meaning "this channel gets plain text"."""
    client = await _send(
        _channel(blockkit_tables="off"), f"Seven merged.\n\n{CHART_FENCE}"
    )

    assert "blocks" not in client.calls[0]
    assert "blockkit" in client.calls[0]["text"]


@pytest.mark.asyncio
async def test_the_notification_text_is_whatever_prose_the_author_wrote() -> None:
    """Why the tier requires one.

    ``text`` is what Slack uses for the desktop and mobile notifications and for
    screen readers. Prose beside the fence is what it is built from here; a
    fence with nothing beside it is the derived-fallback case covered below.
    """
    client = await _send(_channel(), f"Seven merged this week.\n\n{CHART_FENCE}")

    assert "Seven merged this week." in client.calls[0]["text"]


@pytest.mark.asyncio
async def test_a_fence_only_reply_never_sends_the_raw_payload_as_text() -> None:
    """The defect: nothing survives fence-stripping, so ``text`` was the JSON.

    Slack shows ``text`` in the desktop notification, the mobile notification
    and to a screen reader. A model whose whole reply is a fence used to leave
    the raw ``{"blocks": [...]`` there instead of anything a person could read
    without opening the message. Its own rendering has no title, ``alt_text``,
    section or header for the summary to lift, so the assertion is only that
    the source never reaches ``text`` -- the generic string is pinned in
    ``test_a_payload_yielding_nothing_usable_gets_the_generic_string`` below.
    """
    client = await _send(_channel(), CHART_FENCE)

    assert client.calls[0]["blocks"]
    assert not client.calls[0]["text"].startswith("```blockkit")
    assert "{" not in client.calls[0]["text"]


@pytest.mark.asyncio
async def test_prose_plus_a_fence_keeps_the_prose_byte_for_byte() -> None:
    """The ordinary path, unmodified: this is the case that must not regress."""
    content = f"Seven merged this week.\n\n{CHART_FENCE}"
    client = await _send(_channel(), content)

    assert client.calls[0]["text"] == content


IMAGE_BLOCK_WITH_TITLE = {
    "type": "image",
    "image_url": "https://example.com/cat.gif",
    "alt_text": "a cat batting at a box",
    "title": {"type": "plain_text", "text": "The office cat, mid-heist"},
}
IMAGE_BLOCK_ALT_TEXT_ONLY = {
    "type": "image",
    "image_url": "https://example.com/cat.gif",
    "alt_text": "a cat batting at a box",
}


@pytest.mark.asyncio
async def test_an_image_blocks_title_becomes_the_fallback() -> None:
    fence = _blockkit_fence(IMAGE_BLOCK_WITH_TITLE)
    client = await _send(_channel(), fence)

    assert client.calls[0]["text"] == "The office cat, mid-heist"


@pytest.mark.asyncio
async def test_an_image_blocks_alt_text_is_used_when_there_is_no_title() -> None:
    fence = _blockkit_fence(IMAGE_BLOCK_ALT_TEXT_ONLY)
    client = await _send(_channel(), fence)

    assert client.calls[0]["text"] == "a cat batting at a box"


@pytest.mark.asyncio
async def test_a_section_blocks_text_becomes_the_fallback() -> None:
    section = {"type": "section", "text": {"type": "mrkdwn", "text": "Approve this?"}}
    fence = _blockkit_fence(section)
    client = await _send(_channel(), fence)

    assert client.calls[0]["text"] == "Approve this?"


@pytest.mark.asyncio
async def test_a_header_blocks_text_becomes_the_fallback() -> None:
    header = {"type": "header", "text": {"type": "plain_text", "text": "Weekly digest"}}
    fence = _blockkit_fence(header)
    client = await _send(_channel(), fence)

    assert client.calls[0]["text"] == "Weekly digest"


ACTIONS_ONLY_BLOCK = {
    "type": "actions",
    "elements": [
        {
            "type": "button",
            "text": {"type": "plain_text", "text": "Approve"},
            "action_id": "approve",
        }
    ],
}


@pytest.mark.asyncio
async def test_a_payload_yielding_nothing_usable_gets_the_generic_string() -> None:
    """A fence-only control with no title, ``alt_text``, section or header text."""
    fence = _blockkit_fence(ACTIONS_ONLY_BLOCK)
    client = await _send(_channel(blockkit_allow_interactive=True), fence)

    assert client.calls[0]["blocks"]
    assert client.calls[0]["text"] == slack_connect._BLOCKKIT_FALLBACK_GENERIC_TEXT


# --- the basic tier through the connector --------------------------------


MERMAID_FENCE = "\n".join(
    [
        "```mermaid",
        "pie showData",
        "    title Reviews this week",
        '    "merged" : 7',
        '    "open" : 3',
        "```",
    ]
)


@pytest.mark.asyncio
async def test_a_mermaid_pie_reaches_the_channel_as_a_chart() -> None:
    """Nothing beside the fence asked for it: the language is the whole request."""
    client = await _send(_channel(), f"Seven merged.\n\n{MERMAID_FENCE}")

    chart = client.calls[0]["blocks"][-1]
    assert chart["type"] == "data_visualization"
    assert chart["chart"]["type"] == "pie"


@pytest.mark.asyncio
async def test_a_slack_raw_fence_is_delivered_as_the_source() -> None:
    """The escape hatch, for an author showing what the diagram is written as."""
    shown = MERMAID_FENCE.replace("```mermaid", "```slack-raw")
    client = await _send(_channel(), f"Here is the diagram.\n\n{shown}")

    assert "blocks" not in client.calls[0]
    assert "pie showData" in client.calls[0]["text"]


@pytest.mark.asyncio
async def test_a_bare_fence_is_delivered_as_the_source_too() -> None:
    shown = MERMAID_FENCE.replace("```mermaid", "```")
    client = await _send(_channel(), f"Here is the diagram.\n\n{shown}")

    assert "blocks" not in client.calls[0]
    assert "pie showData" in client.calls[0]["text"]


# --- what the reply's marker escalates ------------------------------------
#
# The table shape, and nothing else. The marker used to reach the fence path as
# well, where it turned an unmarked mermaid fence into a chart; the fence now
# decides that for itself and the marker is not consulted there at all.


@pytest.mark.asyncio
async def test_the_marker_forces_a_data_table_a_channel_configured_basic(
) -> None:
    """``render_tables`` is the operator's answer; the marker is the author's.

    An operator who chose ``basic`` chose it for the channel, not for the one
    reply that has something worth sorting in it. The marker is how that reply
    asks, and it is the only way to ask, so this is where the two settings meet.
    """
    plain = await _send(_channel(render_tables="basic"), f"Totals:\n\n{TABLE}")
    asked = await _send(
        _channel(render_tables="basic"), f"{MARKER}\n\nTotals:\n\n{TABLE}"
    )

    assert plain.calls[0]["blocks"][-1]["type"] == "table"
    assert asked.calls[0]["blocks"][-1]["type"] == "data_table"


@pytest.mark.asyncio
async def test_off_outranks_the_marker() -> None:
    """The operator's "no table block" is not a reply's to overturn.

    ``off`` is expressed by never looking for a table, so there is nothing for
    the marker to escalate: the pipes stay in the prose they were written in.
    """
    client = await _send(
        _channel(render_tables="off"), f"{MARKER}\n\nTotals:\n\n{TABLE}"
    )

    assert client.calls[0].get("blocks") in (None, [])
    assert "| alpha | 1 |" in client.calls[0]["text"]


@pytest.mark.asyncio
async def test_a_forced_data_table_is_still_the_same_table() -> None:
    """The escalation changes the block type, not the content.

    Worth stating: ``data_table`` builds its cells separately from ``table``, so
    a column read as numbers is the one place the two could disagree about what
    the author wrote.
    """
    client = await _send(_channel(), f"{MARKER}\n\n## Totals\n\n{TABLE}")

    block = client.calls[0]["blocks"][-1]
    assert [[cell["text"] for cell in row] for row in block["rows"]] == [
        ["Name", "Count"],
        ["alpha", "1"],
        ["beta", "2"],
    ]
    # ``data_table`` requires a caption where ``table`` has none, and it comes
    # from the prose above rather than from a word invented here.
    assert block["caption"] == "Totals"
    # The numeric column is handed over as numbers so the client sorts it as
    # numbers rather than as strings.
    assert [row[1]["type"] for row in block["rows"][1:]] == [
        "raw_number",
        "raw_number",
    ]


@pytest.mark.asyncio
async def test_the_marker_does_not_reach_the_fence_in_either_direction() -> None:
    """A fence answers for itself, so the reply's marker cannot overrule it.

    Both halves matter. The marker no longer turns a ``slack-raw`` fence into a
    chart, and a ``mermaid`` fence draws without it -- so a reply that holds
    the marker for its tables cannot silently draw a diagram the author fenced
    as source, and a reply that holds no marker still gets its diagram.
    """
    shown = MERMAID_FENCE.replace("```mermaid", "```slack-raw")
    asked = await _send(_channel(), f"{MARKER}\n\nSeven merged.\n\n{shown}")
    unasked = await _send(_channel(), f"Seven merged.\n\n{MERMAID_FENCE}")

    assert "blocks" not in asked.calls[0]
    assert "pie showData" in asked.calls[0]["text"]
    assert unasked.calls[0]["blocks"][-1]["type"] == "data_visualization"


@pytest.mark.asyncio
async def test_the_off_marker_withdraws_the_whole_reply_including_a_fence() -> None:
    """Declining is still per reply, and a fence is not exempt from it."""
    client = await _send(
        _channel(blockkit_tables="auto"),
        f"{OFF_MARKER}\n\n{TABLE}\n\n{MERMAID_FENCE}",
    )

    assert "blocks" not in client.calls[0]


@pytest.mark.asyncio
async def test_the_operator_kill_switch_outranks_the_reply_and_the_fence() -> None:
    """``off`` means this channel gets plain text, whatever a reply asked for."""
    client = await _send(
        _channel(blockkit_tables="off"), f"{MARKER}\n\n{TABLE}\n\n{MERMAID_FENCE}"
    )

    assert "blocks" not in client.calls[0]
    assert MARKER not in client.calls[0]["text"]
