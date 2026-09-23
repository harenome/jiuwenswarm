# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The client and the Jev adapter, against recorded responses.

Every body below was recorded from a live decisions endpoint or quoted from
the error it returns. **No test here reaches the network**: the adapter takes
a transport, and every case drives it with one that answers from memory.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from jiuwenswarm.common.typed_decision import (
    JevAdapter,
    Question,
    TypedDecisionClient,
    TypedDecisionError,
)
from jiuwenswarm.common.typed_decision.adapters.jev import DECISIONS_PATH, decisions_url

#: The response to the four-question form, recorded against the labelled
#: transcript's planted false claim.
RECORDED_BODY = {
    "model": "typesafe/jev-1.13-20260917",
    "answers": {
        "audience": {
            "type": "choice",
            "choice": "the room",
            "confidence": 0.51,
            "probabilities": {"the room": 0.63, "nobody": 0.36, "a person": 0.01},
        },
        "claim": {"type": "noul", "noul": 0.74},
        "value": {
            "type": "score",
            "score": 1.88,
            "legend": {"0": "nobody benefits", "1": "mildly useful", "2": "important"},
            "probabilities": {"0": 0.01, "1": 0.10, "2": 0.89},
            "confidence": 0.82,
        },
    },
    "usage": {"input_tokens": 631, "output_tokens": 65, "cost": 2.65e-05},
}

#: What ``/chat/completions`` answers for this model, verbatim.
WRONG_ENDPOINT_BODY = {
    "error": {
        "message": (
            "typesafe/jev-1.13 is a decisions model and cannot be used with the"
            " chat/completions endpoint. Use the /api/alpha/decisions endpoint"
            " instead."
        )
    }
}

QUESTIONS = {
    "audience": Question(
        type="choice",
        instructions="who is the request in this message directed at",
        criteria={
            "nobody": "it asks nothing of anybody",
            "the room": "it puts something to everyone present",
            "a person": "it is aimed at one named person",
        },
    ),
    "claim": Question(
        type="noul",
        instructions="the last line asserts something as fact",
    ),
    "value": Question(
        type="score",
        instructions="judge the last line on its own",
        criteria=["nobody benefits", "mildly useful", "important"],
    ),
}


def adapter(handler, **kwargs) -> JevAdapter:
    return JevAdapter(
        model_name=kwargs.pop("model_name", "typesafe/jev-1.13"),
        api_base=kwargs.pop("api_base", "https://openrouter.example/api/v1"),
        api_key=kwargs.pop("api_key", "sk-test"),
        timeout=kwargs.pop("timeout", 2.0),
        transport=httpx.MockTransport(handler),
        **kwargs,
    )


def test_the_endpoint_is_the_decisions_path_not_chat_completions() -> None:
    """The host is kept and the path replaced: the two are siblings."""
    assert decisions_url("https://openrouter.ai/api/v1") == (
        f"https://openrouter.ai{DECISIONS_PATH}"
    )
    assert decisions_url("https://openrouter.ai") == (
        f"https://openrouter.ai{DECISIONS_PATH}"
    )
    with pytest.raises(TypedDecisionError):
        decisions_url("")
    with pytest.raises(TypedDecisionError):
        decisions_url("openrouter.ai/api/v1")


def test_questions_go_on_the_wire_as_a_record_keyed_by_id() -> None:
    sent: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        sent.update(json.loads(request.content))
        return httpx.Response(200, json=RECORDED_BODY)

    built = adapter(handler).request_body("the state", QUESTIONS)
    assert set(built["questions"]) == {"audience", "claim", "value"}
    assert built["questions"]["claim"] == {
        "type": "noul",
        "instructions": "the last line asserts something as fact",
    }
    assert built["model"] == "typesafe/jev-1.13"


def test_extra_body_reaches_the_top_level_of_the_request() -> None:
    """``provider`` is a top-level field, and ``require_parameters`` is absent.

    A model declaring no supported parameters routes nowhere under that flag,
    so nothing here adds it.
    """
    body = adapter(
        lambda request: httpx.Response(200, json=RECORDED_BODY),
        extra_body={"provider": {"order": ["TypeSafe"]}},
    ).request_body({"channel": "C1"}, QUESTIONS)
    assert body["provider"] == {"order": ["TypeSafe"]}
    assert "require_parameters" not in json.dumps(body)


@pytest.mark.parametrize("field", ["model", "state", "questions"])
def test_extra_body_cannot_replace_decision_request_fields(field: str) -> None:
    with pytest.raises(TypedDecisionError, match=rf"request fields: {field}$"):
        adapter(
            lambda request: httpx.Response(200, json=RECORDED_BODY),
            extra_body={"provider": {"order": ["TypeSafe"]}, field: "replacement"},
        )


async def test_a_recorded_response_yields_one_typed_answer_per_question() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=RECORDED_BODY)

    client = TypedDecisionClient(adapter(handler))
    answers = await client.decide({"channel": "C1"}, QUESTIONS)

    assert set(answers) == {"audience", "claim", "value"}
    assert answers["audience"].value == "the room"
    assert answers["claim"].number == pytest.approx(0.74)
    assert answers["value"].number == pytest.approx(1.88)
    assert answers["value"].confidence == pytest.approx(0.82)
    assert seen[0].url.path == DECISIONS_PATH
    assert seen[0].headers["Authorization"] == "Bearer sk-test"


async def test_the_wrong_endpoint_refusal_becomes_one_error_class() -> None:
    client = TypedDecisionClient(
        adapter(lambda request: httpx.Response(404, json=WRONG_ENDPOINT_BODY)),
        attempts=1,
    )
    with pytest.raises(TypedDecisionError):
        await client.decide("state", QUESTIONS)


async def test_a_partial_response_is_refused_rather_than_returned() -> None:
    """Three answers where four were asked is a different rule, silently."""
    partial = {"model": "x", "answers": {"claim": {"type": "noul", "noul": 0.2}}}
    client = TypedDecisionClient(
        adapter(lambda request: httpx.Response(200, json=partial)), attempts=1
    )
    with pytest.raises(TypedDecisionError):
        await client.decide("state", QUESTIONS)


async def test_a_transient_failure_is_retried_and_the_second_answer_stands() -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(503, text="upstream busy")
        return httpx.Response(200, json=RECORDED_BODY)

    client = TypedDecisionClient(adapter(handler), attempts=2, retry_delay=0.0)
    answers = await client.decide("state", QUESTIONS)
    assert len(calls) == 2
    assert answers["claim"].number == pytest.approx(0.74)


async def test_the_deadline_covers_the_whole_call_not_one_attempt() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(5)
        return httpx.Response(200, json=RECORDED_BODY)

    client = TypedDecisionClient(
        adapter(handler, timeout=5.0), timeout=0.2, attempts=3, retry_delay=0.0
    )
    loop = asyncio.get_running_loop()
    started = loop.time()
    with pytest.raises(TypedDecisionError):
        await client.decide("state", QUESTIONS)
    assert loop.time() - started < 2.0


async def test_an_unreadable_body_is_an_error_and_not_an_empty_answer() -> None:
    client = TypedDecisionClient(
        adapter(lambda request: httpx.Response(200, text="not json at all")),
        attempts=1,
    )
    with pytest.raises(TypedDecisionError):
        await client.decide("state", QUESTIONS)


async def test_asking_nothing_is_refused() -> None:
    client = TypedDecisionClient(
        adapter(lambda request: httpx.Response(200, json=RECORDED_BODY)), attempts=1
    )
    with pytest.raises(TypedDecisionError):
        await client.decide("state", {})
