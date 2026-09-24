"""Unit tests for the Block Kit input framework behind ask-user questions.

The module under test is pure, so everything here is a dictionary in and a
dictionary out: no Slack client, no connector, no event loop.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from jiuwenswarm.gateway.channel_manager.im_platforms.slack import slack_inputs
from jiuwenswarm.gateway.channel_manager.im_platforms.slack.slack_inputs import (
    ELEMENT_SPECS,
    INPUT_SPECS,
    InputRenderError,
    compose_answer,
    compose_datetime,
    flatten_state,
    parse_inputs,
    read_state,
    render_input_blocks,
    submit_problem,
)

PREFIX = "jiuwenswarm_answer:input:"

_OPTIONS = [
    {"label": "Red", "value": "red", "description": "warm"},
    {"label": "Blue", "value": "blue"},
]


def _declare(type_name: str, **extra: Any) -> dict[str, Any]:
    """A minimal declaration for one input of ``type_name``."""
    spec = INPUT_SPECS[type_name]
    declaration: dict[str, Any] = {"type": type_name, "label": f"Pick {type_name}"}
    if any(ELEMENT_SPECS[name].needs_options for name in spec.element_types):
        declaration["options"] = list(_OPTIONS)
    declaration.update(extra)
    return declaration


def _render(type_name: str, **extra: Any) -> list[dict[str, Any]]:
    inputs = parse_inputs({"inputs": [_declare(type_name, **extra)]})
    return render_input_blocks(inputs, action_id_prefix=PREFIX)


def _state(*entries: tuple[str, dict[str, Any]]) -> dict[str, Any]:
    """Build a ``state.values`` keyed by block id, as Slack sends it."""
    return {f"block{index}": {key: value} for index, (key, value) in enumerate(entries)}


def _read_one(type_name: str, entry: dict[str, Any], **extra: Any):
    inputs = parse_inputs({"inputs": [_declare(type_name, **extra)]})
    action_id = inputs[0].action_id(PREFIX, inputs[0].spec.parts[0])
    return read_state(inputs, _state((action_id, entry)), action_id_prefix=PREFIX)[0]


# ---------------------------------------------------------------------------
# Declaring
# ---------------------------------------------------------------------------


def test_a_question_without_inputs_declares_none() -> None:
    """The whole of the backward-compatibility contract: no inputs, no change."""
    assert parse_inputs({"question": "Proceed?", "options": [{"label": "Yes"}]}) == []
    assert parse_inputs({"inputs": []}) == []
    assert parse_inputs({}) == []


def test_a_bare_type_name_is_a_complete_declaration() -> None:
    inputs = parse_inputs({"inputs": ["date", "time"]})

    assert [entry.spec.name for entry in inputs] == ["date", "time"]
    # The name keys the value and the label is what is shown; both fall back to
    # the type rather than to prose this module invented.
    assert [entry.name for entry in inputs] == ["date", "time"]
    assert [entry.label for entry in inputs] == ["date", "time"]


def test_an_unknown_input_type_is_refused_rather_than_skipped() -> None:
    with pytest.raises(InputRenderError) as raised:
        parse_inputs({"inputs": [{"type": "colour_wheel"}]})

    assert "colour_wheel" in str(raised.value)
    # The message names what is available, because the failure is a declaration
    # written against a type this connector does not have.
    assert "datetime" in str(raised.value)


def test_two_inputs_cannot_share_a_name() -> None:
    """A shared name would silently overwrite one value with the other."""
    with pytest.raises(InputRenderError):
        parse_inputs(
            {"inputs": [{"type": "date", "name": "when"}, {"type": "time", "name": "when"}]}
        )


def test_more_inputs_than_a_message_carries_are_refused() -> None:
    too_many = ["text"] * (slack_inputs.MAX_INPUTS_PER_QUESTION + 1)

    with pytest.raises(InputRenderError):
        parse_inputs({"inputs": too_many})


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("type_name", sorted(INPUT_SPECS))
def test_every_declared_type_renders_labelled_input_blocks(type_name: str) -> None:
    blocks = _render(type_name)

    spec = INPUT_SPECS[type_name]
    assert len(blocks) == len(spec.element_types)
    assert [block["element"]["type"] for block in blocks] == list(spec.element_types)
    for block in blocks:
        assert block["type"] == "input"
        # Every element is labelled, which is the reason all of them go in an
        # input block rather than some of them in an actions row.
        assert block["label"]["type"] == "plain_text"
        assert block["label"]["text"]
        assert block["element"]["action_id"].startswith(PREFIX)


@pytest.mark.parametrize("type_name", sorted(INPUT_SPECS))
def test_every_declared_type_is_accepted_by_the_slack_sdk_models(type_name: str) -> None:
    """The rendering is validated against the SDK's own block models.

    Slack rejects a message holding one malformed field rather than repairing
    it, and the rejection arrives at runtime in a channel. Parsing each block
    back through slack_sdk's models is the nearest thing to Slack's own
    validation that runs without a network.
    """
    Block = pytest.importorskip("slack_sdk.models.blocks").Block

    for block in _render(type_name, placeholder="pick one", hint="a hint"):
        Block.parse(block).validate_json()


def test_an_input_carries_its_declared_initial_value() -> None:
    blocks = _render("date", initial="2026-08-20")

    assert blocks[0]["element"]["initial_date"] == "2026-08-20"


def test_an_initial_option_is_the_rendered_option_and_not_a_copy() -> None:
    """Slack rejects an initial_option that is not one of the options."""
    blocks = _render("select", initial="blue")

    element = blocks[0]["element"]
    assert element["initial_option"] in element["options"]
    assert element["initial_option"]["value"] == "blue"


def test_an_initial_naming_an_option_that_was_not_offered_is_dropped() -> None:
    blocks = _render("select", initial="green")

    assert "initial_option" not in blocks[0]["element"]


def test_a_composite_datetime_renders_two_labelled_elements() -> None:
    blocks = _render(
        "datetime",
        label="Start",
        time_label="At",
        initial_date="2026-08-20",
        initial_time="09:30",
    )

    assert [block["element"]["type"] for block in blocks] == ["datepicker", "timepicker"]
    assert [block["label"]["text"] for block in blocks] == ["Start", "At"]
    assert blocks[0]["element"]["initial_date"] == "2026-08-20"
    assert blocks[1]["element"]["initial_time"] == "09:30"
    # Distinct action ids within one message, which Slack requires and which is
    # what lets the two halves be told apart in state.values.
    assert blocks[0]["element"]["action_id"] != blocks[1]["element"]["action_id"]


def test_a_pinned_timezone_reaches_the_timepicker() -> None:
    """A question may say which zone its picker means, for a client that will not."""
    blocks = _render("time", timezone="Europe/Paris")

    assert blocks[0]["element"]["timezone"] == "Europe/Paris"


def test_a_declared_option_value_that_slack_cannot_hold_is_refused() -> None:
    """Truncating it would answer with a value the question cannot receive."""
    with pytest.raises(InputRenderError):
        _render("select", options=[{"label": "Long", "value": "v" * 76}])


def test_a_select_with_no_options_is_refused_rather_than_posted_empty() -> None:
    with pytest.raises(InputRenderError):
        _render("select", options=[])


def test_an_option_label_is_clamped_and_its_value_is_not() -> None:
    blocks = _render("checkboxes", options=[{"label": "L" * 200, "value": "keep"}])

    option = blocks[0]["element"]["options"][0]
    assert len(option["text"]["text"]) == slack_inputs.MAX_OPTION_TEXT_LENGTH
    assert option["value"] == "keep"


def test_declared_knobs_reach_their_slack_field_names() -> None:
    assert _render("text", multiline=True)[0]["element"]["multiline"] is True
    assert _render("number", decimal=True)[0]["element"]["is_decimal_allowed"] is True
    assert (
        _render("multi_select", max_selected=2)[0]["element"]["max_selected_items"] == 2
    )


def test_required_and_optional_reach_slack_as_declared() -> None:
    assert _render("date")[0]["optional"] is False
    assert _render("date", optional=True)[0]["optional"] is True


# ---------------------------------------------------------------------------
# Reading state.values
# ---------------------------------------------------------------------------


def test_state_is_flattened_by_action_id_not_by_block_id() -> None:
    """Slack generates block ids when none were given; action ids are ours."""
    flattened = flatten_state(
        {"generated-1": {"a": {"selected_date": "2026-08-20"}}, "junk": "not a block"}
    )

    assert flattened == {"a": {"selected_date": "2026-08-20"}}
    assert flatten_state(None) == {}


@pytest.mark.parametrize(
    "type_name,entry,expected",
    [
        ("date", {"selected_date": "2026-08-20"}, ["2026-08-20"]),
        ("datetime_unix", {"selected_date_time": 1755678000}, ["2025-08-20T08:20:00+00:00"]),
        ("select", {"selected_option": {"value": "red"}}, ["red"]),
        (
            "multi_select",
            {"selected_options": [{"value": "red"}, {"value": "blue"}]},
            ["red", "blue"],
        ),
        ("user", {"selected_user": "U123"}, ["U123"]),
        ("users", {"selected_users": ["U1", "U2"]}, ["U1", "U2"]),
        ("conversation", {"selected_conversation": "C123"}, ["C123"]),
        ("conversations", {"selected_conversations": ["C1", "D2"]}, ["C1", "D2"]),
        ("channel", {"selected_channel": "C9"}, ["C9"]),
        ("channels", {"selected_channels": ["C9"]}, ["C9"]),
        (
            "checkboxes",
            {"selected_options": [{"value": "blue"}]},
            ["blue"],
        ),
        ("radio", {"selected_option": {"value": "red"}}, ["red"]),
        ("text", {"value": "some words"}, ["some words"]),
        ("email", {"value": "a@example.com"}, ["a@example.com"]),
        ("url", {"value": "https://example.com"}, ["https://example.com"]),
        ("number", {"value": "42"}, ["42"]),
    ],
)
def test_each_element_type_is_read_from_the_key_slack_uses(
    type_name: str, entry: dict[str, Any], expected: list[str]
) -> None:
    assert _read_one(type_name, entry).values == expected


@pytest.mark.parametrize("type_name", sorted(INPUT_SPECS))
def test_an_element_nobody_touched_reads_as_empty(type_name: str) -> None:
    """Slack sends the key with a null value, or does not send it at all."""
    assert _read_one(type_name, {}).empty
    assert _read_one(
        type_name,
        {
            "selected_date": None,
            "selected_time": None,
            "selected_option": None,
            "selected_options": [],
            "selected_user": None,
            "value": "",
        },
    ).empty


def test_an_option_without_a_value_falls_back_to_the_label_it_showed() -> None:
    result = _read_one(
        "select", {"selected_option": {"text": {"type": "plain_text", "text": "Red"}}}
    )

    assert result.values == ["Red"]


def test_a_rich_text_answer_is_flattened_to_the_text_it_displays() -> None:
    result = _read_one(
        "rich_text",
        {
            "rich_text_value": {
                "type": "rich_text",
                "elements": [
                    {
                        "type": "rich_text_section",
                        "elements": [
                            {"type": "text", "text": "see "},
                            {"type": "link", "url": "https://x", "text": "here"},
                            {"type": "text", "text": " and "},
                            {"type": "user", "user_id": "U7"},
                        ],
                    }
                ],
            }
        },
    )

    assert result.values == ["see here and U7"]


# ---------------------------------------------------------------------------
# Formatting and validation
# ---------------------------------------------------------------------------


def test_a_time_carries_its_zone_by_name_and_no_offset() -> None:
    """An offset is a property of an instant, and a bare time is not one."""
    result = _read_one(
        "time", {"selected_time": "09:30", "timezone": "Europe/Paris"}
    )

    assert result.values == ["09:30:00[Europe/Paris]"]


def test_a_time_with_no_zone_anywhere_says_only_what_it_knows() -> None:
    assert _read_one("time", {"selected_time": "09:30"}).values == ["09:30:00"]


def test_a_pinned_zone_is_used_when_the_client_reported_none() -> None:
    result = _read_one(
        "time", {"selected_time": "09:30"}, timezone="Europe/Paris"
    )

    assert result.values == ["09:30:00[Europe/Paris]"]


def test_the_zone_slack_reported_beats_the_one_the_question_pinned() -> None:
    """It is what the person was actually looking at."""
    result = _read_one(
        "time",
        {"selected_time": "09:30", "timezone": "Asia/Tokyo"},
        timezone="Europe/Paris",
    )

    assert result.values == ["09:30:00[Asia/Tokyo]"]


def test_a_malformed_date_is_reported_rather_than_answered_with() -> None:
    result = _read_one("date", {"selected_date": "20/08/2026"})

    assert result.empty
    assert "YYYY-MM-DD" in result.error


def test_a_malformed_number_is_reported() -> None:
    result = _read_one("number", {"value": "twelve"})

    assert result.empty
    assert "number" in result.error


@pytest.mark.parametrize("typed", ["inf", "-inf", "nan", "Infinity"])
def test_a_value_float_reads_as_a_number_but_nobody_means_is_refused(
    typed: str,
) -> None:
    """``float`` accepts all four and none of them is an answer.

    The field is Slack's and the string is the client's, so what a numeric
    field ought to make impossible is checked rather than assumed.
    """
    result = _read_one("number", {"value": typed})

    assert result.empty
    assert "number" in result.error


def test_a_malformed_timestamp_is_reported() -> None:
    result = _read_one("datetime_unix", {"selected_date_time": "soon"})

    assert result.empty
    assert result.error


# ---------------------------------------------------------------------------
# Composing a datetime
# ---------------------------------------------------------------------------


def _datetime_state(date: str, time: str, zone: str | None) -> Any:
    entry: dict[str, Any] = {"selected_time": time}
    if zone is not None:
        entry["timezone"] = zone
    return _state(
        (f"{PREFIX}0.date", {"selected_date": date}), (f"{PREFIX}0.time", entry)
    )


def _read_datetime(date: str, time: str, zone: str | None, **extra: Any):
    inputs = parse_inputs({"inputs": [_declare("datetime", **extra)]})
    return read_state(
        inputs, _datetime_state(date, time, zone), action_id_prefix=PREFIX
    )[0]


def test_a_datetime_is_one_iso_instant_tagged_with_its_zone() -> None:
    result = _read_datetime("2026-08-20", "09:30", "Europe/Paris")

    assert result.values == ["2026-08-20T09:30:00+02:00[Europe/Paris]"]


def test_the_offset_is_resolved_against_the_date_that_was_picked() -> None:
    """The whole reason the two halves are composed rather than delivered apart."""
    summer = _read_datetime("2026-08-20", "09:30", "Europe/Paris").values[0]
    winter = _read_datetime("2026-01-20", "09:30", "Europe/Paris").values[0]

    assert summer.startswith("2026-08-20T09:30:00+02:00")
    assert winter.startswith("2026-01-20T09:30:00+01:00")
    # And the zone name survives both, which an offset alone would not express:
    # "09:30 in Paris, every day" is not "09:30+02:00, every day".
    assert summer.endswith("[Europe/Paris]")
    assert winter.endswith("[Europe/Paris]")


def test_everything_before_the_bracket_is_plain_iso_8601() -> None:
    from datetime import datetime

    value = _read_datetime("2026-08-20", "09:30", "Europe/Paris").values[0]

    parsed = datetime.fromisoformat(value.split("[", 1)[0])
    assert parsed.utcoffset().total_seconds() == 2 * 3600


def test_a_datetime_with_no_zone_carries_no_offset_either() -> None:
    assert _read_datetime("2026-08-20", "09:30", None).values == [
        "2026-08-20T09:30:00"
    ]


def test_a_zone_this_host_cannot_resolve_keeps_its_name_and_loses_its_offset() -> None:
    assert compose_datetime("2026-08-20", "09:30", "Mars/Olympus") == (
        "2026-08-20T09:30:00[Mars/Olympus]"
    )


def test_half_a_datetime_says_which_half_is_missing() -> None:
    inputs = parse_inputs({"inputs": [_declare("datetime")]})
    only_date = read_state(
        inputs,
        _state((f"{PREFIX}0.date", {"selected_date": "2026-08-20"})),
        action_id_prefix=PREFIX,
    )[0]
    only_time = read_state(
        inputs,
        _state((f"{PREFIX}0.time", {"selected_time": "09:30"})),
        action_id_prefix=PREFIX,
    )[0]

    assert only_date.error == "no time was picked"
    assert only_time.error == "no date was picked"


def test_an_untouched_datetime_is_empty_rather_than_half_missing() -> None:
    inputs = parse_inputs({"inputs": [_declare("datetime")]})

    result = read_state(inputs, {}, action_id_prefix=PREFIX)[0]

    assert result.empty
    assert result.error == ""


# ---------------------------------------------------------------------------
# The answer
# ---------------------------------------------------------------------------


def test_one_input_answers_with_its_values_and_nothing_else() -> None:
    """Byte-identical in shape to the answer a pressed button produces."""
    inputs = parse_inputs({"inputs": ["date"]})
    results = read_state(
        inputs,
        _state((f"{PREFIX}0", {"selected_date": "2026-08-20"})),
        action_id_prefix=PREFIX,
    )

    assert compose_answer(inputs, results) == ["2026-08-20"]


def test_one_multi_valued_input_answers_with_every_value_it_held() -> None:
    inputs = parse_inputs({"inputs": [_declare("multi_select")]})
    results = read_state(
        inputs,
        _state(
            (
                f"{PREFIX}0",
                {"selected_options": [{"value": "red"}, {"value": "blue"}]},
            )
        ),
        action_id_prefix=PREFIX,
    )

    assert compose_answer(inputs, results) == ["red", "blue"]


def test_several_inputs_answer_with_one_json_object_keyed_by_name() -> None:
    inputs = parse_inputs(
        {
            "inputs": [
                {"type": "date", "name": "starts"},
                {"type": "multi_select", "name": "colours", "options": _OPTIONS},
                {"type": "text", "name": "note"},
            ]
        }
    )
    results = read_state(
        inputs,
        _state(
            (f"{PREFIX}0", {"selected_date": "2026-08-20"}),
            (f"{PREFIX}1", {"selected_options": [{"value": "red"}]}),
            (f"{PREFIX}2", {"value": "ship it"}),
        ),
        action_id_prefix=PREFIX,
    )

    answer = compose_answer(inputs, results)
    assert len(answer) == 1
    assert json.loads(answer[0]) == {
        "starts": "2026-08-20",
        # A multi-valued input stays a list even when it holds one value, so a
        # reader does not have to guess whether one item means one or many.
        "colours": ["red"],
        "note": "ship it",
    }


def test_an_optional_input_nobody_filled_is_absent_rather_than_null() -> None:
    inputs = parse_inputs(
        {
            "inputs": [
                {"type": "date", "name": "starts"},
                {"type": "text", "name": "note", "optional": True},
            ]
        }
    )
    results = read_state(
        inputs,
        _state((f"{PREFIX}0", {"selected_date": "2026-08-20"})),
        action_id_prefix=PREFIX,
    )

    assert json.loads(compose_answer(inputs, results)[0]) == {"starts": "2026-08-20"}
    assert submit_problem(inputs, results) == ""


# ---------------------------------------------------------------------------
# Refusing a submit
# ---------------------------------------------------------------------------


def test_a_submit_with_nothing_entered_is_refused() -> None:
    inputs = parse_inputs({"inputs": ["date"]})
    results = read_state(inputs, {}, action_id_prefix=PREFIX)

    assert submit_problem(inputs, results)
    assert compose_answer(inputs, results) == []


def test_a_required_input_left_blank_names_itself() -> None:
    inputs = parse_inputs(
        {"inputs": [{"type": "date", "label": "Start date"}, "text"]}
    )
    results = read_state(
        inputs,
        _state((f"{PREFIX}1", {"value": "hello"})),
        action_id_prefix=PREFIX,
    )

    assert "Start date" in submit_problem(inputs, results)


def test_a_malformed_value_is_reported_before_a_blank_one() -> None:
    """The person can only fix what they are told about; the wrong one is worse."""
    inputs = parse_inputs(
        {"inputs": [{"type": "date", "label": "Start date"}, {"type": "text", "label": "Note"}]}
    )
    results = read_state(
        inputs,
        _state((f"{PREFIX}0", {"selected_date": "nonsense"})),
        action_id_prefix=PREFIX,
    )

    problem = submit_problem(inputs, results)
    assert problem.startswith("Start date")
    assert "YYYY-MM-DD" in problem


def test_a_question_whose_every_input_is_optional_still_needs_one_of_them() -> None:
    """There would otherwise be nothing to answer the waiting turn with."""
    inputs = parse_inputs(
        {"inputs": [{"type": "text", "optional": True}, {"type": "date", "optional": True}]}
    )
    results = read_state(inputs, {}, action_id_prefix=PREFIX)

    assert submit_problem(inputs, results) == "nothing was selected"


# ---------------------------------------------------------------------------
# Formatting blocks
# ---------------------------------------------------------------------------


def test_the_formatting_blocks_are_what_slack_calls_them() -> None:
    assert slack_inputs.divider_block() == {"type": "divider"}
    header = slack_inputs.header_block("A" * 400)
    assert header["type"] == "header"
    assert header["text"]["type"] == "plain_text"
    assert len(header["text"]["text"]) == slack_inputs.MAX_HEADER_LENGTH
    context = slack_inputs.context_block("one", "", "two")
    assert context["type"] == "context"
    assert [element["text"] for element in context["elements"]] == ["one", "two"]
