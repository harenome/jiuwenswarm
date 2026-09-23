# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""TypeSafe's Jev, reached through an OpenRouter-compatible decisions endpoint.

Three facts about this model are held here and nowhere else, because each one
is a property of this implementation rather than of the protocol.

**The path is ``/api/alpha/decisions``.** ``/chat/completions`` refuses the
model outright, saying so in the error body: a decisions model cannot be used
with that endpoint.

**The catalogue does not list it.** Only a direct lookup returns it, so
anything that enumerates models will not find it. Nothing here enumerates.

**It declares no supported parameters.** An OpenRouter chat entry in this
project sets ``require_parameters: true`` under ``extra_body.provider``, which
routes only to providers accepting every parameter sent. Against a model
declaring none, that flag routes nowhere, so it is not copied across. The
provider block is passed through as configured and this module adds nothing to
it.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx

from jiuwenswarm.common.typed_decision.adapters.base import TEXT_ONLY
from jiuwenswarm.common.typed_decision.schema import (
    Answer,
    Question,
    TypedDecisionError,
    parse_answers,
)

logger = logging.getLogger(__name__)

#: The path a decisions request is posted to, appended to whatever host the
#: configured ``api_base`` names. An ``api_base`` written for chat completions
#: ends in ``/api/v1``; the decisions path is a sibling of it rather than a
#: child, which is why the host is taken and the path replaced.
DECISIONS_PATH = "/api/alpha/decisions"

#: The model names this adapter claims. Selection is by prefix so a version
#: bump needs no change here.
MODEL_PREFIXES = ("typesafe/",)
REQUEST_FIELDS = frozenset({"model", "state", "questions"})


def decisions_url(api_base: str) -> str:
    """The decisions endpoint for a configured API base.

    ``api_base`` is whatever an operator wrote for this model entry, and the
    two forms seen in practice are a bare host and a host with a chat path on
    it. Both name the same deployment, so the host is what is kept.
    """
    if not str(api_base or "").strip():
        raise TypedDecisionError("a decision model needs an api_base")
    parts = urlsplit(str(api_base).strip())
    if not parts.scheme or not parts.netloc:
        raise TypedDecisionError(f"api_base is not an absolute URL: {api_base!r}")
    return urlunsplit((parts.scheme, parts.netloc, DECISIONS_PATH, "", ""))


class JevAdapter:
    """Answer typed-decision questions with a TypeSafe decisions model."""

    name = "jev"
    modalities = TEXT_ONLY

    def __init__(
        self,
        *,
        model_name: str,
        api_base: str,
        api_key: str,
        timeout: float,
        extra_body: Mapping[str, Any] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.model_name = model_name
        self.url = decisions_url(api_base)
        self._api_key = api_key
        self._timeout = timeout
        self._extra_body = dict(extra_body or {})
        reserved = REQUEST_FIELDS.intersection(self._extra_body)
        if reserved:
            raise TypedDecisionError(
                "decision extra_body cannot override request fields: "
                + ", ".join(sorted(reserved))
            )
        # Injected so a test drives this adapter against a recorded response
        # without reaching the network. Production passes nothing.
        self._transport = transport

    @classmethod
    def claims(cls, model_name: str) -> bool:
        """Whether this adapter answers for the named model."""
        lowered = str(model_name or "").strip().lower()
        return any(lowered.startswith(prefix) for prefix in MODEL_PREFIXES)

    def request_body(
        self,
        state: Mapping[str, Any] | str,
        questions: Mapping[str, Question],
    ) -> dict[str, Any]:
        """The JSON body for one decisions request.

        ``questions`` goes on the wire as a record keyed by id, not as an
        array, and every option of a choice is named and described inside it.
        """
        if not questions:
            raise TypedDecisionError("a decision request needs at least one question")
        body: dict[str, Any] = {
            "model": self.model_name,
            "state": state if isinstance(state, str) else dict(state),
            "questions": {
                str(question_id): question.as_request_value()
                for question_id, question in questions.items()
            },
        }
        body.update(self._extra_body)
        return body

    async def ask(
        self,
        state: Mapping[str, Any] | str,
        questions: Mapping[str, Question],
    ) -> dict[str, Answer]:
        body = self.request_body(state, questions)
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout, transport=self._transport
            ) as client:
                response = await client.post(self.url, json=body, headers=headers)
        except httpx.HTTPError as exc:
            raise TypedDecisionError(f"decisions request failed: {exc}") from exc
        if response.status_code >= 400:
            # The body states the reason and the reasons are specific -- the
            # wrong endpoint says which one to use. It is logged rather than
            # folded into the exception message because it is unbounded.
            logger.debug(
                "[typed_decision] decisions endpoint refused: status=%s body=%s",
                response.status_code,
                response.text[:1000],
            )
            raise TypedDecisionError(
                f"decisions endpoint returned {response.status_code}"
            )
        try:
            payload = response.json()
        except (json.JSONDecodeError, ValueError) as exc:
            raise TypedDecisionError("decisions response was not JSON") from exc
        if not isinstance(payload, Mapping):
            raise TypedDecisionError("decisions response was not a record")
        answers = parse_answers(payload)
        missing = set(questions) - set(answers)
        if missing:
            # Partial results are refused rather than returned. A rule reading
            # four answers out of five would apply a different rule from the
            # one it was tested as, and nothing downstream could see that.
            raise TypedDecisionError(
                f"decisions response answered none of: {sorted(missing)}"
            )
        return answers
