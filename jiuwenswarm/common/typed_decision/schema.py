# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The request and response shapes of a typed decision.

A caller supplies a state and a set of independent questions. It receives one
typed answer per question. Nothing here knows which model answers, which
platform asked, or what the caller does with the result.

Three question types, and the request fields differ between them:

``noul``
    One proposition. The answer is the probability that it holds. It takes no
    criteria and returns no confidence, because a distribution over two
    outcomes is described completely by one number. A value near 0.5 is
    ambiguity rather than a middling intensity.
``choice``
    One option out of a named set. ``criteria`` is a record mapping each option
    to a description of when it applies, so the vocabulary is stated in the
    request rather than assumed.
``score``
    A position along an ordered rubric. ``criteria`` is a sequence of levels,
    lowest first, and the answer interpolates between them. A ``legend`` comes
    back so the caller can map the number onto the rubric it sent.

The ordering is the whole distinction between the last two. A ``score`` rubric
must be ordered because the answer moves along it. A ``choice`` list must not
be, because the answer names one member.

**``probabilities``, ``confidence`` and ``legend`` are optional on every
answer, and no caller may require one.** They are what one implementation
returns. A chat model behind a strict prompt and a JSON schema implements this
same protocol and returns none of them.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

#: The three question types the protocol defines. An implementation that
#: cannot answer one of them declines that question rather than inventing a
#: fourth.
NOUL = "noul"
CHOICE = "choice"
SCORE = "score"
QUESTION_TYPES = (NOUL, CHOICE, SCORE)


class TypedDecisionError(RuntimeError):
    """A typed decision could not be obtained.

    Raised for a malformed question, a transport failure and an unreadable
    response alike. Callers that must not fail catch this one class.
    """


@dataclass(frozen=True)
class Question:
    """One question, independent of every other question in the same request.

    ``instructions`` holds all of the meaning. **The question id never
    reaches the model**, so a question may not lean on its own name: an id of
    ``addressed`` tells the model nothing.
    """

    type: str
    instructions: str
    criteria: Mapping[str, str] | Sequence[Any] | None = None

    def __post_init__(self) -> None:
        if self.type not in QUESTION_TYPES:
            raise TypedDecisionError(
                f"question type must be one of {QUESTION_TYPES}: {self.type!r}"
            )
        if not str(self.instructions).strip():
            raise TypedDecisionError("question instructions must not be empty")
        # Each type constrains ``criteria`` differently, and the endpoint
        # rejects the wrong shape rather than ignoring it. Refusing here names
        # the fault at the call site instead of inside a transport error.
        if self.type == NOUL:
            if self.criteria is not None:
                raise TypedDecisionError("a noul question takes no criteria")
        elif self.type == CHOICE:
            if not isinstance(self.criteria, Mapping) or not self.criteria:
                raise TypedDecisionError(
                    "a choice question needs criteria as a non-empty"
                    " {option: description} record"
                )
        elif self.type == SCORE:
            if isinstance(self.criteria, (str, bytes, Mapping)) or not isinstance(
                self.criteria, Sequence
            ):
                raise TypedDecisionError(
                    "a score question needs criteria as an ordered sequence of"
                    " levels, lowest first"
                )
            if len(self.criteria) < 2:
                raise TypedDecisionError("a score rubric needs at least two levels")

    def as_request_value(self) -> dict[str, Any]:
        """The wire form of this question, without its id."""
        payload: dict[str, Any] = {
            "type": self.type,
            "instructions": self.instructions,
        }
        if self.criteria is not None:
            payload["criteria"] = (
                dict(self.criteria)
                if isinstance(self.criteria, Mapping)
                else list(self.criteria)
            )
        return payload


@dataclass(frozen=True)
class Answer:
    """One typed answer.

    ``value`` is a probability for a ``noul``, the chosen option for a
    ``choice``, and a position along the rubric for a ``score``. The three
    optional fields below are present only where the implementation returns
    them.
    """

    type: str
    value: float | str
    confidence: float | None = None
    probabilities: Mapping[str, float] | None = None
    legend: Mapping[str, Any] | None = None

    @property
    def number(self) -> float | None:
        """``value`` where it is numeric, and ``None`` for a ``choice``.

        A rule over several answers reads probabilities and scores as numbers
        and a choice as a word. This keeps the ``isinstance`` out of the rule.
        """
        return self.value if isinstance(self.value, (int, float)) else None


def parse_answer(payload: Mapping[str, Any]) -> Answer:
    """Read one answer out of a decision response.

    The type is taken from the payload rather than from the question that was
    asked, so a response that answers a different type is a readable error
    here instead of a wrong number downstream.
    """
    answer_type = str(payload.get("type") or "").strip()
    if answer_type not in QUESTION_TYPES:
        raise TypedDecisionError(f"answer stated no known type: {payload!r}")
    if answer_type not in payload:
        raise TypedDecisionError(
            f"a {answer_type} answer had no {answer_type!r} field: {payload!r}"
        )
    raw = payload[answer_type]
    value: float | str
    if answer_type == CHOICE:
        value = str(raw)
    else:
        try:
            value = float(raw)
        except (TypeError, ValueError) as exc:
            raise TypedDecisionError(
                f"a {answer_type} answer was not numeric: {raw!r}"
            ) from exc
    probabilities = payload.get("probabilities")
    legend = payload.get("legend")
    confidence = payload.get("confidence")
    return Answer(
        type=answer_type,
        value=value,
        confidence=float(confidence) if isinstance(confidence, (int, float)) else None,
        probabilities=(
            MappingProxyType(
                {str(key): float(item) for key, item in probabilities.items()}
            )
            if isinstance(probabilities, Mapping)
            else None
        ),
        legend=(
            MappingProxyType(dict(legend)) if isinstance(legend, Mapping) else None
        ),
    )


def parse_answers(payload: Mapping[str, Any]) -> dict[str, Answer]:
    """Read every answer out of a decision response body."""
    answers = payload.get("answers")
    if not isinstance(answers, Mapping):
        raise TypedDecisionError("decision response had no answers record")
    parsed: dict[str, Answer] = {}
    for question_id, entry in answers.items():
        if not isinstance(entry, Mapping):
            raise TypedDecisionError(
                f"answer for {question_id!r} was not a record: {entry!r}"
            )
        parsed[str(question_id)] = parse_answer(entry)
    return parsed
