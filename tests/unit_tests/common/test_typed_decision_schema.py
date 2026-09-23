# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Question shapes and answer parsing.

The three question types take three different ``criteria`` shapes and the
endpoint enforces the difference. Refusing the wrong shape here names the
fault at the call site rather than inside a transport error, and the answer
half is tested against bodies recorded from a live decisions endpoint.
"""

from __future__ import annotations

import pytest

from jiuwenswarm.common.typed_decision import (
    Answer,
    Question,
    TypedDecisionError,
    parse_answer,
    parse_answers,
)


def test_a_noul_takes_no_criteria() -> None:
    Question(type="noul", instructions="the last line asserts something as fact")
    with pytest.raises(TypedDecisionError):
        Question(
            type="noul",
            instructions="the last line asserts something as fact",
            criteria={"yes": "it does", "no": "it does not"},
        )


def test_a_choice_needs_every_option_described() -> None:
    Question(
        type="choice",
        instructions="who is the request in this message directed at",
        criteria={
            "nobody": "it asks nothing of anybody",
            "the room": "it puts a question to everyone present",
        },
    )
    with pytest.raises(TypedDecisionError):
        Question(type="choice", instructions="who is it for", criteria={})
    with pytest.raises(TypedDecisionError):
        Question(
            type="choice",
            instructions="who is it for",
            criteria=["nobody", "the room"],
        )


def test_a_score_needs_an_ordered_rubric() -> None:
    Question(
        type="score",
        instructions="judge the last line on its own",
        criteria=["nobody benefits", "mildly useful", "a false claim is adopted"],
    )
    with pytest.raises(TypedDecisionError):
        Question(
            type="score",
            instructions="judge the last line",
            criteria={"0": "low", "1": "high"},
        )
    with pytest.raises(TypedDecisionError):
        Question(type="score", instructions="judge it", criteria=["only one level"])


def test_an_unknown_type_and_an_empty_instruction_are_refused() -> None:
    with pytest.raises(TypedDecisionError):
        Question(type="probability", instructions="anything")
    with pytest.raises(TypedDecisionError):
        Question(type="noul", instructions="   ")


def test_the_request_value_omits_the_question_id() -> None:
    """The id is for our code. All the meaning has to be in the wording."""
    question = Question(type="noul", instructions="the bot already answered this")
    assert question.as_request_value() == {
        "type": "noul",
        "instructions": "the bot already answered this",
    }


def test_a_recorded_choice_answer_parses_whole() -> None:
    answer = parse_answer(
        {
            "type": "choice",
            "choice": "here",
            "confidence": 0.51,
            "probabilities": {
                "here": 0.63,
                "none": 0.36,
                "elsewhere": 0.01,
                "later": 0,
            },
        }
    )
    assert answer.value == "here"
    assert answer.confidence == pytest.approx(0.51)
    assert answer.probabilities is not None
    assert answer.probabilities["none"] == pytest.approx(0.36)
    assert answer.number is None


def test_a_recorded_noul_answer_is_one_number_with_no_confidence() -> None:
    """Two outcomes, so the single probability describes the distribution."""
    answer = parse_answer({"type": "noul", "noul": 0.03})
    assert answer.number == pytest.approx(0.03)
    assert answer.confidence is None
    assert answer.probabilities is None


def test_a_recorded_score_answer_keeps_its_legend() -> None:
    """The legend is how a caller maps the number back onto the rubric sent."""
    answer = parse_answer(
        {
            "type": "score",
            "score": 1.11,
            "legend": {"0": "irrelevant", "1": "mildly useful", "2": "important"},
            "probabilities": {"0": 0.19, "1": 0.52, "2": 0.29},
            "confidence": 0.28,
        }
    )
    assert answer.number == pytest.approx(1.11)
    assert answer.legend is not None
    assert answer.legend["1"] == "mildly useful"


def test_probabilities_confidence_and_legend_are_all_optional() -> None:
    """A chat model behind a JSON schema returns none of the three."""
    answer = parse_answer({"type": "choice", "choice": "the room"})
    assert answer == Answer(type="choice", value="the room")


def test_a_malformed_answer_is_refused_rather_than_guessed() -> None:
    with pytest.raises(TypedDecisionError):
        parse_answer({"type": "noul"})
    with pytest.raises(TypedDecisionError):
        parse_answer({"type": "noul", "noul": "quite likely"})
    with pytest.raises(TypedDecisionError):
        parse_answer({"choice": "here"})
    with pytest.raises(TypedDecisionError):
        parse_answers({"usage": {"cost": 0.0}})
