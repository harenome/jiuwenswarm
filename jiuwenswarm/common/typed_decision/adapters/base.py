# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""What every typed-decision implementation has to offer, and nothing else.

Two members. One call that answers questions, and a declaration of which
modalities the state may hold. The second exists so that a state builder can
emit an attachment in the richest form the implementation accepts and degrade
to a description otherwise, in the manner ``supports_vision`` already serves a
model entry. Today every implementation is text only; when one is not, one
declaration changes and the callers stay as they are.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol, runtime_checkable

from jiuwenswarm.common.typed_decision.schema import Answer, Question

#: The modality every implementation accepts. A state is text, a record of
#: text, or a sequence of text values.
TEXT = "text"

#: The declaration a text-only implementation makes.
TEXT_ONLY = frozenset({TEXT})


@runtime_checkable
class DecisionAdapter(Protocol):
    """One way of answering a typed-decision request."""

    #: The name this implementation is selected by, and the word written into
    #: a log line so a recorded decision names what produced it.
    name: str

    #: What the state may hold. A caller compares against this rather than
    #: assuming text.
    modalities: frozenset[str]

    async def ask(
        self,
        state: Mapping[str, Any] | str,
        questions: Mapping[str, Question],
    ) -> dict[str, Answer]:
        """Answer every question against the state.

        Raises :class:`~jiuwenswarm.common.typed_decision.schema.TypedDecisionError`
        for a transport failure or an unreadable response. It never returns a
        partial result silently: a caller reading three answers out of four
        would have no way of knowing which one it lost.
        """
        raise NotImplementedError
