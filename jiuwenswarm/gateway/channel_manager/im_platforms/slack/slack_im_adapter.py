# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Slack implementation of ``IMPlatformAdapter``.

Feishu and WeCom satisfy the adapter protocol from a connector-fed local store,
because every inbound method the protocol declares is synchronous.  Slack has no
synchronous SDK, so the same split is used here for a different reason: the six
protocol methods only ever read process-local caches, and every Slack API call
happens on an ``await`` seam the connector owns (``observe_message``).  Calling
Slack from the synchronous methods would either block the gateway event loop on
an HTTP round trip or require a nested loop, and both are worse than serving a
slightly stale channel snapshot.

History and display names come from ``SlackHistoryToolkit``, which already wraps
``auth.test``, ``conversations.history``, ``conversations.replies`` and
``users.info`` with pagination, rate-limit retries, secret redaction and hard
collection caps.  Its public snapshot method is used rather than the private
wrappers so this module does not depend on the toolkit's internal call
bookkeeping.
"""

from __future__ import annotations

import json
import time
from typing import Any, Callable

from jiuwenswarm.common.slack_history_policy import (
    HISTORY_ORIGIN,
    METADATA_POLICY_KEY,
)
from jiuwenswarm.common.utils import logger
from jiuwenswarm.gateway.im_pipeline.im_inbound import IMHistoryMessage

PLATFORM_NAME = "Slack"

# metadata keys this adapter owns, mirroring the feishu/wecom pairs.
REPLY_CANDIDATE_USER_ID_KEY = "reply_candidate_slack_user_id"
REPLY_USER_ID_KEY = "reply_slack_user_id"

# A full history scan costs several Slack API calls, so it is amortised over a
# window rather than run per inbound message.  Messages seen in between are
# appended locally by ``observe_message``, so the cache stays current without
# the scan; the scan exists to pick up traffic the bot was not dispatched (and
# to resolve display names).
_DEFAULT_REFRESH_INTERVAL_SECONDS = 300.0
_DEFAULT_HISTORY_HOURS = 24.0
# Bounded so a busy channel cannot grow the cache without limit.  The inbound
# pipeline asks for 500 by default, so keeping that many is enough to answer it.
_MAX_CACHED_MESSAGES = 500

# ``conversations.history`` is only meaningful for the channel types the
# toolkit itself accepts.
_HISTORY_CHANNEL_TYPES = ("channel", "group")


def _timestamp_ms(slack_ts: Any) -> int:
    """Convert a Slack ``ts`` ("1710000000.000100") to epoch milliseconds."""
    try:
        return int(float(slack_ts) * 1000)
    except (TypeError, ValueError):
        return 0


class SlackIMPlatformAdapter:
    """Adapter letting Slack take part in the group digital-avatar pipeline."""

    channel_id = "slack"

    def __init__(
        self,
        *,
        my_user_id: str = "",
        principal_name: str = "",
        bot_name: str = "",
        bot_user_id: str = "",
        history_hours: float = _DEFAULT_HISTORY_HOURS,
        refresh_interval_seconds: float = _DEFAULT_REFRESH_INTERVAL_SECONDS,
        toolkit_factory: Callable[[dict[str, Any]], Any] | None = None,
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        self._my_user_id = (my_user_id or "").strip()
        self._principal_name = (principal_name or "").strip()
        self._bot_name = (bot_name or "").strip()
        self._bot_user_id = (bot_user_id or "").strip()
        self._history_hours = history_hours
        self._refresh_interval_seconds = max(0.0, float(refresh_interval_seconds))
        self._toolkit_factory = toolkit_factory or self._build_toolkit
        self._now = now
        # channel id -> slack ts -> history entry.  Keyed by ts so a backfill
        # cannot duplicate a message already appended live.
        self._history: dict[str, dict[str, IMHistoryMessage]] = {}
        self._refreshed_at: dict[str, float] = {}
        self._user_names: dict[str, str] = {}

    # ------------------------------------------------------------------
    # connector-side (async) surface
    # ------------------------------------------------------------------

    def set_bot_user_id(self, bot_user_id: str) -> None:
        """Record the bot's own user id once ``auth.test`` has resolved it."""
        self._bot_user_id = (bot_user_id or "").strip()

    async def observe_message(
        self,
        *,
        channel_id: str,
        channel_type: str,
        user_id: str,
        text: str,
        message_ts: str,
    ) -> None:
        """Fold one inbound Slack message into the caches the pipeline reads.

        Called from the connector's event handler, i.e. the only place in this
        flow where awaiting is allowed.  Failures are swallowed: a degraded
        history snapshot must never stop the message itself being handled.
        """
        channel_id = (channel_id or "").strip()
        if not channel_id:
            return
        if channel_type in _HISTORY_CHANNEL_TYPES and self._refresh_due(channel_id):
            try:
                await self._refresh_history(channel_id, channel_type)
            except Exception as exc:  # noqa: BLE001 - best effort by contract
                # Caught separately from the local append below: a Slack outage
                # must degrade the snapshot, not stop the adapter recording the
                # traffic it is being handed directly.
                logger.warning("[SlackIMAdapter] history refresh failed: %s", exc)
        self._remember_message(
            channel_id=channel_id,
            user_id=(user_id or "").strip(),
            text=text or "",
            message_ts=(message_ts or "").strip(),
        )

    def _refresh_due(self, channel_id: str) -> bool:
        last = self._refreshed_at.get(channel_id)
        if last is None:
            return True
        return (self._now() - last) >= self._refresh_interval_seconds

    def _build_toolkit(self, metadata: dict[str, Any]) -> Any:
        from jiuwenswarm.agents.harness.common.tools.slack_history import (
            SlackHistoryToolkit,
        )

        return SlackHistoryToolkit(metadata=metadata)

    async def _refresh_history(self, channel_id: str, channel_type: str) -> None:
        """Replace the cached snapshot for one channel from the Slack API."""
        # Stamped before the call, not after: a failing or slow scan must not
        # make every subsequent message retry it.
        self._refreshed_at[channel_id] = self._now()
        toolkit = self._toolkit_factory(
            {
                "slack_channel_id": channel_id,
                "slack_channel_type": channel_type,
                # This adapter reads the conversation it is already in and no
                # other, which is what ``origin`` names. Stamped rather than left
                # absent, because the toolkit reads an absent policy as *no side
                # that has the configuration settled this request* and refuses.
                #
                # Not ``channels.slack.history``: that key governs what the
                # *model* may ask for, while this is the connector filling its
                # own context cache for a conversation it is answering in.
                METADATA_POLICY_KEY: HISTORY_ORIGIN,
            }
        )
        raw = await toolkit.read_slack_conversation(
            hours=self._history_hours,
            include_threads=True,
        )
        try:
            snapshot = json.loads(raw)
        except (TypeError, ValueError) as exc:
            logger.warning("[SlackIMAdapter] history snapshot is not JSON: %s", exc)
            return
        if not isinstance(snapshot, dict) or not snapshot.get("ok"):
            logger.info(
                "[SlackIMAdapter] history unavailable for %s: %s",
                channel_id,
                (snapshot or {}).get("error") if isinstance(snapshot, dict) else "",
            )
            return

        messages = snapshot.get("messages")
        if not isinstance(messages, list):
            return

        bucket = self._history.setdefault(channel_id, {})
        for item in messages:
            if not isinstance(item, dict):
                continue
            ts = str(item.get("ts") or "").strip()
            if not ts:
                continue
            user_id = str(item.get("author_user_id") or "").strip()
            user_name = str(item.get("author_name") or "").strip()
            # The toolkit falls back to the id when users.info is unavailable;
            # caching that as a name would pin the id in place forever.
            if user_id and user_name and user_name != user_id:
                self._user_names[user_id] = user_name
            bucket[ts] = IMHistoryMessage(
                user_id=user_id,
                user_name=user_name or self.resolve_user_display_name(user_id),
                content=str(item.get("text") or "").strip(),
                timestamp_ms=_timestamp_ms(ts),
            )
        self._trim(channel_id)

    def _remember_message(
        self, *, channel_id: str, user_id: str, text: str, message_ts: str
    ) -> None:
        if not message_ts or not text.strip():
            return
        bucket = self._history.setdefault(channel_id, {})
        # The name is deliberately left blank rather than resolved now: the next
        # refresh may learn it, and load_recent_messages fills it in at read
        # time so an already-cached message is not stuck showing a raw user id.
        bucket[message_ts] = IMHistoryMessage(
            user_id=user_id,
            user_name="",
            content=text.strip(),
            timestamp_ms=_timestamp_ms(message_ts),
        )
        self._trim(channel_id)

    def _trim(self, channel_id: str) -> None:
        bucket = self._history.get(channel_id)
        if not bucket or len(bucket) <= _MAX_CACHED_MESSAGES:
            return
        keep = sorted(bucket.items(), key=lambda pair: pair[1].timestamp_ms)[
            -_MAX_CACHED_MESSAGES:
        ]
        self._history[channel_id] = dict(keep)

    # ------------------------------------------------------------------
    # IMPlatformAdapter -- inbound
    # ------------------------------------------------------------------

    def get_principal_user_id(self) -> str:
        return self._my_user_id

    def get_principal_display_name(self) -> str:
        if self._principal_name:
            return self._principal_name
        return self.resolve_user_display_name(self._my_user_id)

    def resolve_user_display_name(self, user_id: str) -> str:
        """Best-effort display name, falling back to the raw Slack user id.

        Names are populated by the periodic history refresh, which resolves them
        through ``users.info``.  A participant seen for the first time between
        two refreshes therefore shows as ``U…`` until the next one, the same
        fallback the WeCom adapter uses permanently.
        """
        user_id = (user_id or "").strip()
        if not user_id:
            return ""
        return self._user_names.get(user_id, user_id)

    def get_bot_mention_tokens(self) -> list[str]:
        """Tokens that mean "the bot was addressed" inside raw Slack text.

        Slack renders a mention as ``<@U123ABC>``; the plain ``@name`` form is
        also accepted because a human may type the bot's name without letting
        Slack turn it into a link.
        """
        tokens: list[str] = []
        if self._bot_user_id:
            tokens.append(f"<@{self._bot_user_id}>")
        if self._bot_name:
            tokens.append(f"@{self._bot_name}")
        return tokens

    def load_recent_messages(
        self, thread_id: str, limit: int = 500
    ) -> list[IMHistoryMessage]:
        bucket = self._history.get((thread_id or "").strip())
        if not bucket:
            return []
        ordered = sorted(bucket.values(), key=lambda item: item.timestamp_ms)
        selected = ordered[-limit:] if limit > 0 else []
        return [
            item
            if item.user_name
            else IMHistoryMessage(
                user_id=item.user_id,
                user_name=self.resolve_user_display_name(item.user_id),
                content=item.content,
                timestamp_ms=item.timestamp_ms,
            )
            for item in selected
        ]

    def build_relevance_metadata(
        self,
        metadata: dict[str, Any],
        *,
        sender_user_id: str,
        relevant: bool,
    ) -> dict[str, Any]:
        if not relevant:
            return {}
        # An explicit delivery decision already exists; do not overwrite it.
        if str(metadata.get("reply_scope") or "").strip():
            return {}
        if str(metadata.get("chat_type") or "").strip() != "group":
            return {}

        principal_user_id = self.get_principal_user_id()
        if not principal_user_id or sender_user_id == principal_user_id:
            return {}

        patch: dict[str, Any] = {
            REPLY_CANDIDATE_USER_ID_KEY: principal_user_id,
            "reply_candidate_reason": "processor_target_user",
            "reply_candidate_user_id": principal_user_id,
        }
        principal_name = self.get_principal_display_name()
        if principal_name:
            patch["reply_target_name"] = principal_name
        return patch

    # ------------------------------------------------------------------
    # IMPlatformAdapter -- outbound
    # ------------------------------------------------------------------

    @property
    def platform_name(self) -> str:
        return PLATFORM_NAME

    @property
    def reply_user_id_key(self) -> str:
        return REPLY_USER_ID_KEY

    @property
    def use_keyword_override(self) -> bool:
        return True

    def get_candidate_user_id(self, metadata: dict[str, Any]) -> str:
        candidate = str(metadata.get(REPLY_CANDIDATE_USER_ID_KEY) or "").strip()
        if candidate:
            return candidate
        # Same fallback as WeCom: a reply whose request never went through the
        # inbound pipeline still gets a candidate, so an avatar answer can be
        # routed rather than silently staying in the channel.
        patch = self.build_relevance_metadata(
            metadata,
            sender_user_id=str(metadata.get("im_sender_user_id") or ""),
            relevant=True,
        )
        if patch:
            candidate = str(patch.get(REPLY_CANDIDATE_USER_ID_KEY) or "").strip()
            if candidate:
                metadata.update(patch)
        return candidate
