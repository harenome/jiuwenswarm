"""Durable de-duplication of inbound Slack events.

Slack redelivers an event when it does not see a timely ack, and Socket Mode
replays across reconnects. A process-local window loses its identities on
restart, and a deploy or a crash-restart lands inside Slack's retry window, so
the bot answers a message it has already answered.

The storage shape follows ``CronJobStore``: an ``asyncio.Lock`` for coroutines
in this process, a ``portalocker`` sidecar for other processes, the whole
read-modify-write inside both, and an atomic ``tmp.replace()`` so a torn write
cannot corrupt the file.

Keys are message identities (``team:channel:ts``), which Slack never reuses, so
entries need no expiry -- the LRU cap is the only bound.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any, Callable, TypeVar

import portalocker

from jiuwenswarm.common.utils import get_user_workspace_dir

logger = logging.getLogger(__name__)

_T = TypeVar("_T")

# Slack retries an unacked event a handful of times over roughly a minute, and a
# reconnect replays a similar span. 1024 identities covers far more than that
# while keeping the file small enough to rewrite per message without noticing.
DEFAULT_MAX_ENTRIES = 1024
_FILE_LOCK_TIMEOUT_SEC = 10.0
_SCHEMA_VERSION = 1


def get_slack_dedup_path() -> Path:
    """Canonical path for the Slack dedup window."""
    return get_user_workspace_dir() / "gateway" / "slack_seen_events.json"


class SlackEventDedupStore:
    """Remembers which Slack messages have already been handled, across restarts.

    Every call re-reads under the lock rather than trusting an in-process cache.
    The re-read is what makes a shared workspace safe: a cached "not seen" would
    be stale the moment a sibling process admitted the same message.
    """

    def __init__(
        self,
        path: Path | None = None,
        *,
        max_entries: int = DEFAULT_MAX_ENTRIES,
        file_lock_timeout: float = _FILE_LOCK_TIMEOUT_SEC,
    ) -> None:
        self._path = path or get_slack_dedup_path()
        self._max_entries = max(1, int(max_entries))
        self._file_lock_timeout = float(file_lock_timeout)
        self._lock = asyncio.Lock()
        # Survives a broken file so the connector keeps de-duplicating in memory
        # instead of regressing to no de-duplication at all.
        self._fallback: list[str] = []
        self._degraded = False

    @property
    def path(self) -> Path:
        return self._path

    async def remember(self, key: str) -> bool:
        """Record ``key``. Returns True if it is new, False if already seen.

        A persistence failure never rejects a message: the caller gets the
        in-memory answer and the event is handled.
        """
        if not key:
            return True
        async with self._lock:
            try:
                return await asyncio.to_thread(self._remember_under_file_lock, key)
            except Exception as exc:  # noqa: BLE001 - portalocker/OS errors vary
                if not self._degraded:
                    logger.warning(
                        "Slack dedup store unavailable (%s); de-duplicating in "
                        "memory only, so a restart may re-answer a retried event",
                        exc,
                    )
                    self._degraded = True
                return self._remember_in_memory(key)

    def _remember_in_memory(self, key: str) -> bool:
        if key in self._fallback:
            return False
        self._fallback.append(key)
        del self._fallback[: max(0, len(self._fallback) - self._max_entries)]
        return True

    def _remember_under_file_lock(self, key: str) -> bool:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self._path.with_suffix(self._path.suffix + ".lock")
        with portalocker.Lock(str(lock_path), timeout=self._file_lock_timeout):
            seen = self._read_unlocked()
            if key in seen:
                return False
            seen.append(key)
            # Oldest-first trim: the list doubles as the LRU order.
            del seen[: max(0, len(seen) - self._max_entries)]
            self._write_unlocked(seen)
            self._fallback = list(seen)
            self._degraded = False
            return True

    def _read_unlocked(self) -> list[str]:
        """Return the stored window, or an empty one for anything unreadable.

        A corrupt or truncated file must not break startup or message handling;
        starting empty costs at most one duplicated answer.
        """
        try:
            if not self._path.exists():
                return []
            raw = self._path.read_text(encoding="utf-8")
            if not raw.strip():
                return []
            data: Any = json.loads(raw)
            if not isinstance(data, dict):
                return []
            events = data.get("events")
            if not isinstance(events, list):
                return []
            return [str(item) for item in events if isinstance(item, (str, int))]
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Slack dedup file %s is unreadable (%s); starting a fresh window",
                self._path,
                exc,
            )
            return []

    def _write_unlocked(self, events: list[str]) -> None:
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        payload = json.dumps(
            {"version": _SCHEMA_VERSION, "events": events},
            ensure_ascii=False,
        )
        tmp.write_text(payload, encoding="utf-8")
        tmp.replace(self._path)
