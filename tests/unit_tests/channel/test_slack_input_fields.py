"""Unit tests for the Slack field contracts a rendered input element must hold.

Well-formed Block Kit is not the same thing as a payload Slack accepts. A field
Slack makes required, or one it types differently from the declaration feeding
it, costs the message every control in it: the validator declines the element
and then reads the ``input`` block as carrying none at all, so it answers
``/<index>/element=missing_field`` and names neither the field nor the reason.
A live question asking for a date, a start time and a headcount was refused
whole because of one of the three.

``number_input`` is where both faults happened at once, and it is the only
entry in ``ELEMENT_SPECS`` that declares either rule. ``blocks.validate`` was
called on 2026-09-22 with the workspace's own bot token, one ``input`` block
per call:

    number_input, min_value=1 (int),   no is_decimal_allowed    -> refused
    number_input, min_value=1 (int),   is_decimal_allowed=False -> refused
    number_input, min_value="1" (str), no is_decimal_allowed    -> refused
    number_input, min_value="1" (str), is_decimal_allowed=False -> accepted
    number_input, no bounds at all,    is_decimal_allowed=False -> accepted
    email_text_input                                            -> accepted
    url_text_input                                              -> accepted
    rich_text_input                                             -> accepted

Both fields have to be right or the element is still refused, which is why one
test pins each and a third pins the payload that was refused in service.

That probe is the whole of the evidence, and it answers one question:
``blocks.validate`` accepts these elements in a message. Whether
``chat.postMessage`` then renders them has not been established either way, and
nothing here asserts that it does.

Nothing in this file touches Slack. The module under test is pure, so every
case is a declaration in and a list of blocks out.
"""

from __future__ import annotations

from typing import Any

import pytest

from jiuwenswarm.gateway.channel_manager.im_platforms.slack.slack_inputs import (
    ELEMENT_SPECS,
    INPUT_SPECS,
    parse_inputs,
    render_input_blocks,
)

PREFIX = "jiuwenswarm_answer:input:"

_OPTIONS = [
    {"label": "Red", "value": "red"},
    {"label": "Blue", "value": "blue"},
]


def _declare(type_name: str, **extra: Any) -> dict[str, Any]:
    declaration: dict[str, Any] = {"type": type_name, "label": f"Pick {type_name}"}
    spec = INPUT_SPECS[type_name]
    if any(ELEMENT_SPECS[name].needs_options for name in spec.element_types):
        declaration["options"] = list(_OPTIONS)
    declaration.update(extra)
    return declaration


def _render(type_name: str, **extra: Any) -> list[dict[str, Any]]:
    inputs = parse_inputs({"inputs": [_declare(type_name, **extra)]})
    return render_input_blocks(inputs, action_id_prefix=PREFIX)


def _element(type_name: str, **extra: Any) -> dict[str, Any]:
    return _render(type_name, **extra)[0]["element"]


# ---------------------------------------------------------------------------
# is_decimal_allowed: required, so never the declaration's to omit
# ---------------------------------------------------------------------------


def test_a_number_question_asks_for_a_number_input() -> None:
    """A pin on the element itself, because everything below is its fields.

    Slack's block element reference lists ``number_input`` for Modals and not
    for Messages, and an earlier reading of that table took the refusal for a
    surface rule and built a plain text field instead. ``blocks.validate``
    accepts the element in a message once its two fields are right, so the
    table is not what decides and the fields are.
    """
    assert _element("number")["type"] == "number_input"


@pytest.mark.parametrize(
    "declaration",
    [{}, {"min": 1}, {"max": 10}, {"min": 1, "max": 10}, {"initial": 4}],
    ids=["bare", "min", "max", "both-bounds", "initial"],
)
def test_a_number_element_always_says_whether_decimals_are_allowed(
    declaration: dict[str, Any],
) -> None:
    """Slack makes the field required and refuses the element without it.

    A question is free to say nothing about decimals, and the element is not
    free to leave the question unanswered, so a declaration that is silent
    still sends the field.
    """
    assert "is_decimal_allowed" in _element("number", **declaration)


def test_silence_about_decimals_allows_them() -> None:
    """The default cannot narrow what the question asked for.

    Asking for whole numbers is something a question does by declaring
    ``decimal: false``. Defaulting to that would refuse 3.5 for a question
    that never mentioned decimals.
    """
    assert _element("number")["is_decimal_allowed"] is True


@pytest.mark.parametrize("declared", [True, False])
def test_a_declared_decimal_flag_is_the_one_that_is_sent(declared: bool) -> None:
    assert _element("number", decimal=declared)["is_decimal_allowed"] is declared


# ---------------------------------------------------------------------------
# min_value / max_value: Strings, whatever the declaration carries
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("declared", "sent"),
    [(1, "1"), (0, "0"), (-3, "-3"), (2.5, "2.5"), (2.0, "2"), ("7", "7")],
    ids=["int", "zero", "negative", "decimal", "whole-float", "already-a-string"],
)
def test_a_declared_bound_reaches_slack_as_a_string(
    declared: Any, sent: str,
) -> None:
    """``min``/``max`` are JSON numbers; Slack types the fields they feed String.

    A whole number loses the fractional part JSON gives it: ``"2.0"`` beside
    ``is_decimal_allowed: false`` would be a bound written in a form the
    element accepts no answer in.
    """
    element = _element("number", min=declared, max=declared)

    assert element["min_value"] == sent
    assert element["max_value"] == sent


def test_an_undeclared_bound_is_not_invented() -> None:
    """The fields are optional, unlike the flag; only their type is fixed."""
    element = _element("number")

    assert "min_value" not in element
    assert "max_value" not in element


def test_a_number_starts_at_the_declared_value_as_a_string_too() -> None:
    assert _element("number", initial=4)["initial_value"] == "4"


# ---------------------------------------------------------------------------
# The declaration that was refused in service
# ---------------------------------------------------------------------------


def test_the_question_refused_in_service_now_carries_both_fields() -> None:
    """The live call, rebuilt declaration for declaration.

    The date and the time were never the problem. The headcount's ``min: 1``
    went as a JSON number with no ``is_decimal_allowed`` beside it, and one
    element Slack will not take costs the message all three.
    """
    inputs = parse_inputs(
        {
            "inputs": [
                {"label": "Date", "name": "date", "type": "date"},
                {"label": "Start time", "name": "start_time", "type": "time"},
                {
                    "label": "Number of people",
                    "name": "people",
                    "type": "number",
                    "min": 1,
                },
            ]
        }
    )

    blocks = render_input_blocks(inputs, action_id_prefix=PREFIX)
    elements = [block["element"] for block in blocks]

    assert [element["type"] for element in elements] == [
        "datepicker",
        "timepicker",
        "number_input",
    ]
    headcount = elements[2]
    assert headcount["min_value"] == "1"
    assert isinstance(headcount["min_value"], str)
    assert headcount["is_decimal_allowed"] is True


# ---------------------------------------------------------------------------
# The tables that carry the two rules
# ---------------------------------------------------------------------------


def test_a_string_typed_field_is_one_the_element_actually_sends() -> None:
    """A rule naming a field nothing builds would hold over nothing.

    ``string_typed`` names Slack fields by hand, and what feeds them is a
    second hand-written name in ``passthrough``. Renaming one and not the
    other leaves the rule pointing at a field that is never sent, and the
    bound goes quietly back to being a JSON number.
    """
    for name, spec in ELEMENT_SPECS.items():
        sent = {slack_name for _declared, slack_name in spec.passthrough}
        assert spec.string_typed <= sent, name
