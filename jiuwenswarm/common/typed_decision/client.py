# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""One call: a state and some questions in, typed answers out.

The client owns the two things that are true of every implementation and of
none of them in particular -- a deadline, and how many times a failed attempt
is repeated. The adapter below it owns the wire.

**The deadline covers the whole call, not one attempt.** A caller that has a
budget has it once. Retrying inside a deadline the caller set is the client's
business; a retry that extends the deadline is a second call the caller never
asked for.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Mapping
from typing import Any

from jiuwenswarm.common.typed_decision.adapters.base import DecisionAdapter
from jiuwenswarm.common.typed_decision.schema import (
    Answer,
    Question,
    TypedDecisionError,
)

logger = logging.getLogger(__name__)

#: Seconds a whole call may take, retries included. Measured median latency of
#: one attempt is roughly a third of a second, so this leaves room for two
#: failures without being a budget a caller can feel.
DEFAULT_TIMEOUT_SECONDS = 4.0

#: Attempts, not retries: one is no retry at all.
DEFAULT_ATTEMPTS = 2

#: Seconds between attempts. Flat rather than exponential, because the
#: deadline above is short enough that a second backoff would consume it.
RETRY_DELAY_SECONDS = 0.25


class TypedDecisionClient:
    """Ask an implementation a set of independent questions about a state."""

    def __init__(
        self,
        adapter: DecisionAdapter,
        *,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        attempts: int = DEFAULT_ATTEMPTS,
        retry_delay: float = RETRY_DELAY_SECONDS,
    ) -> None:
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        if attempts < 1:
            raise ValueError("attempts must be at least one")
        self.adapter = adapter
        self.timeout = timeout
        self.attempts = attempts
        self.retry_delay = retry_delay

    @property
    def name(self) -> str:
        """The implementation answering, for a log line to name."""
        return getattr(self.adapter, "name", "unknown")

    async def decide(
        self,
        state: Mapping[str, Any] | str,
        questions: Mapping[str, Question],
    ) -> dict[str, Answer]:
        """Answer every question, or raise.

        Raises :class:`TypedDecisionError` when the deadline passes or when
        every attempt failed. A caller that must not fail catches that one
        class; nothing here returns a half answer instead of raising.
        """
        deadline = time.monotonic() + self.timeout
        last: BaseException | None = None
        for attempt in range(1, self.attempts + 1):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                return await asyncio.wait_for(
                    self.adapter.ask(state, questions), timeout=remaining
                )
            except asyncio.CancelledError:
                # The caller, or a shutdown, took the task away. Cancellation
                # is not a failure to retry past: swallowing it here would
                # leave a task nobody can stop.
                raise
            except (TypedDecisionError, asyncio.TimeoutError, OSError) as exc:
                last = exc
                logger.debug(
                    "[typed_decision] attempt %s of %s failed: adapter=%s error=%s",
                    attempt,
                    self.attempts,
                    self.name,
                    exc,
                )
            if attempt < self.attempts:
                # The deadline decides whether there is another attempt. A
                # zero backoff is a caller asking for no pause, not for no
                # retry, and reading it as the latter silently halves the
                # attempts every test that sets it was counting on.
                left = deadline - time.monotonic()
                if left <= 0:
                    break
                pause = min(self.retry_delay, left)
                if pause > 0:
                    await asyncio.sleep(pause)
        raise TypedDecisionError(
            f"no typed decision after {self.attempts} attempt(s)"
            f" within {self.timeout}s: {last}"
        )
