# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""``typed_decision`` -- questions with typed answers, from any model that can.

The name is the request and response shape rather than the vendor. A caller
supplies a state and a set of independent questions and receives one typed
answer per question. Which model answers is configuration.

Naming the shape rather than the vendor is what keeps a caller honest. A
decisions model answers this; so does a chat model behind a strict prompt and
a JSON schema, which returns no probabilities and no confidence, and is
therefore the implementation that proves the protocol is not vendor-shaped.

**Nothing in this package may import ``jiuwenswarm`` outside it.** The rule is
what makes the package movable: a move would change the import prefix and
nothing else. It is enforced rather than trusted -- see
``tests/unit_tests/common/test_typed_decision_boundary.py``, which walks every
module here with ``ast`` and allows the standard library, intra-package
imports, third-party transports and agent-core's ``ModelClientConfig``.

The vocabulary of the questions is the caller's. This package defines the
three question types and nothing about what any of them mean.
"""

from __future__ import annotations

from jiuwenswarm.common.typed_decision.adapters import DecisionAdapter, JevAdapter
from jiuwenswarm.common.typed_decision.client import (
    DEFAULT_ATTEMPTS,
    DEFAULT_TIMEOUT_SECONDS,
    TypedDecisionClient,
)
from jiuwenswarm.common.typed_decision.compose import (
    ADAPTERS,
    DecisionModel,
    build_client,
    decision_model,
    select_adapter_factory,
)
from jiuwenswarm.common.typed_decision.schema import (
    CHOICE,
    NOUL,
    QUESTION_TYPES,
    SCORE,
    Answer,
    Question,
    TypedDecisionError,
    parse_answer,
    parse_answers,
)

__all__ = [
    "ADAPTERS",
    "CHOICE",
    "DEFAULT_ATTEMPTS",
    "DEFAULT_TIMEOUT_SECONDS",
    "NOUL",
    "QUESTION_TYPES",
    "SCORE",
    "Answer",
    "DecisionAdapter",
    "DecisionModel",
    "JevAdapter",
    "Question",
    "TypedDecisionClient",
    "TypedDecisionError",
    "build_client",
    "decision_model",
    "parse_answer",
    "parse_answers",
    "select_adapter_factory",
]
