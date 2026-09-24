# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Pin a Slack message, or unpin one.

The one tool in this module writes. That is what separates it from everything
in ``slack_history``, and it is why it is a tool of its own rather than a mode
of the tool that lists pins. ``permissions.tools`` is keyed by name and matched
exactly, so one tool is one policy: folding the read and the write into a
single name would make *may list the pins, may not pin* permanently
inexpressible, and an operator asked to choose between all of it and none of it
picks none.

**The conversation is not an argument.** It comes from the trusted request
metadata the Slack gateway stamps, exactly as the reading tools' origin does,
and there is deliberately no ``chat_id`` here. Two reasons, and the second is
the load-bearing one:

* Pinning into a conversation the request did not come from is acting where
  nobody asked. A read answered in the wrong room is a disclosure; a write made
  in the wrong room is a change everybody in it sees, made on nobody's request.
* The membership gate the reading tools use is a *read* rule. ``members(S)
  subset-of members(T)`` says nobody in S learns anything they could not
  already learn, which is a statement about disclosure and authorises nothing
  about writing into T. Reusing it here would be reusing the words rather than
  the argument.

A ``chat_id`` argument is additive and can be added later, under whatever rule
is then written for it. It is not added now for want of that rule.

Two refusals are swallowed as success, and two are not. ``already_pinned`` and
``no_pin`` say the conversation is already in the state that was asked for, so
reporting them as failures would make a retry look like a fault and invite a
model to undo its own work. ``too_many_pins`` and ``not_pinnable`` say the
change did not happen and will not happen on a second call; the caller has to
see them. Slack documents that a conversation's pin list is bounded but not
what the bound is, so no number is stated anywhere here.

Every one of the four arrives as HTTP 200 with an ``{"ok": false, "error":
...}`` body, which the SDK raises as ``SlackApiError``. Both shapes are read,
because which one an operator sees depends on the installed SDK version.

Only cross-cutting primitives are borrowed from ``slack_history``: the reading
of a Slack response, the rule for when a refusal is worth retrying, the pass
that keeps a credential out of an error code, and the failure type that names
the refused method the way Slack's own documentation spells it. They are
security- or correctness-critical and a fix to one copy would not reach a
second. No gate and no data path is shared: this module reads no conversation
and returns no content.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections.abc import Mapping
from typing import Any

from openjiuwen.core.foundation.tool import LocalFunction, Tool, ToolCard

from jiuwenswarm.agents.harness.common.tools.slack_history import (
    _DEFAULT_RETRY_AFTER_SECONDS,
    SlackWorkspaceClients,
    _SlackCallFailure,
    _as_mapping,
    _retry_after_seconds,
    _safe_error_code,
    shared_slack_workspaces,
)
from jiuwenswarm.common.slack_history_policy import (
    METADATA_ORIGIN_KEY,
    METADATA_TEAM_KEY,
    ORIGIN_CRON_JOB,
    SlackWorkspaceUnresolved,
)


logger = logging.getLogger(__name__)

#: The channel id the cron scheduler delivers a scheduled job under. Spelled
#: here rather than imported from the gateway: a harness tool must not reach
#: into the gateway, which is why the policy module holds the other two names
#: this predicate reads.
_CRON_REQUEST_CHANNEL_ID = "__cron__"

#: The two SDK method names, in the SDK's spelling. ``_SlackCallFailure.where``
#: turns each back into the ``pins.add`` an operator looks up.
_PIN_ADD = "pins_add"
_PIN_REMOVE = "pins_remove"

#: The scope both of them need. One scope for the whole module, so a refusal
#: can name it without deriving anything from what the call asked for.
_PIN_WRITE_SCOPE = "pins:write"

#: Refused because the conversation is already in the state that was asked for.
#: Keyed by which call was made, because the two are not interchangeable:
#: ``already_pinned`` from a remove would be Slack answering a question nobody
#: asked, and swallowing it there would report an unpin that did not happen.
_ALREADY_IN_STATE = {_PIN_ADD: "already_pinned", _PIN_REMOVE: "no_pin"}

#: What a refusal means, for the two that are worth explaining and not worth
#: retrying. Everything else falls through to naming the method, which is what
#: an operator looks a code up by.
_PIN_FAILURE_DETAIL = {
    "too_many_pins": (
        "this conversation holds as many pinned messages as Slack allows, so"
        " nothing further can be pinned until somebody unpins one; calling"
        " again will not help"
    ),
    "not_pinnable": (
        "Slack will not pin this message; a second call will not change that"
    ),
    "message_not_found": (
        "this conversation holds no message with that id; a message id is not"
        " transferable between conversations, so one taken from elsewhere names"
        " nothing here"
    ),
}

#: A Slack message ts, which is what a message id is on this platform: seconds
#: and microseconds, separated by a dot. Checked before the value travels into
#: an API argument, because it reaches this tool from a model that read it out
#: of somewhere else.
_MESSAGE_ID_RE = re.compile(r"\A\d{1,12}\.\d{1,6}\Z")

#: Wall clock for one pin, retries included. A single call with no pagination,
#: so this is a ceiling on the tool rather than a scan budget, and a rate-limit
#: wait is spent from it rather than added to it.
_TIMEOUT_SECONDS = 30.0

#: How many times one call will wait out a rate limit before giving up. A pin
#: is a small write nobody is waiting on for long, and the deadline above
#: usually bites first.
_MAX_RATE_LIMIT_RETRIES = 2


def slack_pin_request_metadata(
    channel_id: str | None,
    metadata: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Return trusted metadata for a request that may pin, or fail closed.

    Two request shapes, the same two the reading tools accept, and for the same
    reason: in both the conversation is named by the gateway before the turn
    starts, and in neither is it part of the tool argument surface.

    * An inbound Slack turn arrives on the ``slack`` transport, and the
      connector stamps the conversation the message came from.
    * A scheduled run arrives on the cron transport and is honoured only where
      the scheduler marked it as its own. A job is created in a conversation
      and posts into it, so pinning there is a write into the conversation the
      job already writes to. Without the marker a stray ``slack_channel_id``
      left on some other request cannot reach this tool.

    Not a condition: the history policy word. That says how far this
    conversation may *read*, and a deployment that reads nothing but its own
    scrollback has said nothing about whether the bot may pin. Making a write
    depend on a read setting would take the tool away from ``origin``
    deployments, which are most of them, and would give an operator widening
    their reads a write they did not ask for.
    """
    channel = str(channel_id or "").strip().lower()
    if channel != "slack":
        if channel != _CRON_REQUEST_CHANNEL_ID:
            return {}
        if not isinstance(metadata, Mapping):
            return {}
        if metadata.get(METADATA_ORIGIN_KEY) != ORIGIN_CRON_JOB:
            return {}
    if not isinstance(metadata, Mapping):
        return {}
    if not str(metadata.get("slack_channel_id") or "").strip():
        return {}
    return dict(metadata)


def _pin_refusal_json(
    code: str,
    detail: str = "",
    *,
    chat_id: str = "",
    message_id: str = "",
) -> str:
    """One refusal, in a shape that never claims to know what it does not.

    ``pinned`` is absent. It is the state of one message in one conversation,
    and a call Slack refused establishes nothing about that state -- a message
    that could not be pinned because the list is full is not thereby unpinned.
    A ``false`` here would read as *it is not pinned*, which is a different
    claim from *this call did not pin it*, and the difference is what a caller
    deciding whether to try something else acts on.
    """
    payload: dict[str, Any] = {"ok": False, "error": code}
    if detail:
        payload["detail"] = detail
    if chat_id:
        payload["chat_id"] = chat_id
    if message_id:
        payload["message_id"] = message_id
    logger.warning(
        "slack pin refused: %s%s", code, f" -- {detail}" if detail else ""
    )
    return json.dumps(payload, ensure_ascii=False)


class SlackPinToolkit:
    """Toolkit scoped to the Slack conversation in the active request metadata."""

    def __init__(
        self,
        *,
        metadata: dict[str, Any] | None = None,
        metadata_provider: Any | None = None,
        client: Any | None = None,
        workspaces: "SlackWorkspaceClients | None" = None,
        timeout_seconds: float = _TIMEOUT_SECONDS,
        sleep: Any = asyncio.sleep,
        monotonic: Any = time.monotonic,
    ) -> None:
        self._request_metadata = dict(metadata) if metadata else {}
        self._metadata_provider = metadata_provider
        self._client = client
        self._workspaces = workspaces or shared_slack_workspaces()
        self._timeout_seconds = max(1.0, float(timeout_seconds))
        self._sleep = sleep
        self._monotonic = monotonic
        self._bot_token = ""

    def update_runtime_context(
        self,
        *,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Refresh the request-scoped metadata without recreating the tool."""
        self._request_metadata = dict(metadata) if metadata else {}

    def _runtime_metadata(self) -> dict[str, Any]:
        if self._metadata_provider is None:
            return dict(self._request_metadata)
        try:
            provided = self._metadata_provider()
        except Exception:  # noqa: BLE001 - providers must fail closed.
            return {}
        if not isinstance(provided, Mapping):
            return {}
        return dict(provided)

    async def _load_settings(self, metadata: Mapping[str, Any]) -> None:
        """Bind this request to the Slack install it arrived from.

        Takes the metadata the caller already read rather than reading it
        again. A provider is read once per request on purpose: it is live, and
        two reads of it are two requests as far as it is concerned.

        Read per call rather than captured at construction: this toolkit is
        built once and answers every request for the life of the process, a
        token rotated underneath it must take effect without a restart, and
        with several installs configured which token serves a call is a
        property of the request rather than of start-up.

        A pin is a write, and a write into the wrong workspace cannot be taken
        back by a later read, so an unresolvable install is refused here.
        """
        slack = await self._workspaces.settings_for(
            str(metadata.get(METADATA_TEAM_KEY) or "").strip()
        )
        self._bot_token = str(slack.get("bot_token") or "").strip()

    def _get_client(self) -> Any:
        if self._client is not None:
            return self._client
        return self._workspaces.client_for(self._bot_token)

    async def _call(self, method: str, **kwargs: Any) -> dict[str, Any]:
        """Make one Slack call, reducing any refusal to a credential-free code.

        Both failure shapes are read. Slack answers a refused pin with HTTP 200
        and ``{"ok": false, "error": ...}``; the SDK raises that as
        ``SlackApiError``, and an older one hands the body back instead, so a
        code arriving one way on one deployment and the other way on the next
        would otherwise be two behaviours.
        """
        try:
            client = self._get_client()
        except _SlackCallFailure as exc:
            exc.method = exc.method or method
            raise
        deadline = self._monotonic() + self._timeout_seconds
        retries = 0
        while True:
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                raise _SlackCallFailure("pin_timed_out", method)
            try:
                response = await asyncio.wait_for(
                    getattr(client, method)(**kwargs), timeout=remaining
                )
            except TimeoutError:
                raise _SlackCallFailure("pin_timed_out", method) from None
            except Exception as exc:  # noqa: BLE001 - SDK types vary by version.
                delay = _retry_after_seconds(exc)
                if delay is not None and retries < _MAX_RATE_LIMIT_RETRIES:
                    retries += 1
                    await self._sleep(delay)
                    continue
                data = _as_mapping(getattr(exc, "response", None))
                code = _safe_error_code(data.get("error"), self._bot_token)
                raise _SlackCallFailure(code, method) from None

            data = _as_mapping(response)
            if data.get("ok", True) is False:
                code = _safe_error_code(data.get("error"), self._bot_token)
                if code == "ratelimited" and retries < _MAX_RATE_LIMIT_RETRIES:
                    retries += 1
                    await self._sleep(_DEFAULT_RETRY_AFTER_SECONDS)
                    continue
                raise _SlackCallFailure(code, method)
            # ``pins.add`` answers ``{"ok": true}`` and nothing else, and so
            # does ``pins.remove``. There is nothing here to normalise into a
            # result, which is why the result below is built from what was
            # asked for rather than from what came back.
            return data

    async def pin_message(self, message_id: str, remove: bool = False) -> str:
        """Pin one message in this conversation, or unpin it.

        ``message_id`` is the only thing named. The conversation is the one the
        request arrived in, taken from trusted metadata, for the reason the
        module docstring gives.

        ``file`` and ``file_comment`` are not accepted, and their absence is not
        a limitation of this tool: Slack retired them. Its own documentation for
        ``pins.add`` says *"In the past, files and file comments could be pinned
        to a channel as well"*, and the arguments now do nothing. A file is
        pinned by pinning the message that shared it.
        """
        metadata = self._runtime_metadata()
        chat_id = str(metadata.get("slack_channel_id") or "").strip()
        raw_id = str(message_id or "").strip()

        # Everything that costs no call, first. A write refused for a bad
        # argument must not have reached Slack to find that out.
        if not chat_id:
            # The same code the reading tools refuse an untrusted request with.
            # It is the same fact -- no Slack path settled which conversation
            # this request is about -- and one word for it means an operator
            # searching for it finds every place it can happen.
            return _pin_refusal_json(
                "trusted_slack_channel_context_required",
                "this request carries no Slack conversation, so there is"
                " nowhere to pin a message and nothing to fall back to",
            )
        if not raw_id:
            return _pin_refusal_json(
                "message_id_required",
                "no message id was given; a message id identifies one message"
                " in this conversation and is taken unchanged from a result"
                " that reported it",
                chat_id=chat_id,
            )
        if not _MESSAGE_ID_RE.match(raw_id):
            return _pin_refusal_json(
                "message_id_malformed",
                "a Slack message id is seconds and microseconds separated by a"
                " dot, such as 1758123456.123456; this is not one",
                chat_id=chat_id,
                # Bounded, because it is about to be echoed back into a result
                # that is persisted.
                message_id=raw_id[:32],
            )

        try:
            await self._load_settings(metadata)
        except SlackWorkspaceUnresolved as unresolved:
            return _pin_refusal_json(
                unresolved.code, unresolved.detail, chat_id=chat_id
            )
        method = _PIN_REMOVE if remove else _PIN_ADD
        try:
            await self._call(method, channel=chat_id, timestamp=raw_id)
        except _SlackCallFailure as exc:
            if exc.code == _ALREADY_IN_STATE[method]:
                # The conversation is already in the state that was asked for.
                # Reported as the success it is: the caller wanted this message
                # pinned, and it is pinned, by whoever got there first.
                logger.info(
                    "slack pin: %s in %s was already in the requested state (%s)",
                    raw_id,
                    chat_id,
                    exc.code,
                )
                return self._pin_result(chat_id, raw_id, remove=remove)
            detail = _PIN_FAILURE_DETAIL.get(exc.code)
            if not detail:
                detail = f"Slack refused {exc.where}"
                if exc.code == "missing_scope":
                    detail += (
                        f"; pinning needs the {_PIN_WRITE_SCOPE} scope, which"
                        f" this installation does not hold"
                    )
            return _pin_refusal_json(
                exc.code, detail, chat_id=chat_id, message_id=raw_id
            )

        logger.info(
            "slack pin: %s %s in %s",
            "unpinned" if remove else "pinned",
            raw_id,
            chat_id,
        )
        return self._pin_result(chat_id, raw_id, remove=remove)

    @staticmethod
    def _pin_result(chat_id: str, message_id: str, *, remove: bool) -> str:
        """The one success shape, ours rather than Slack's.

        Built from what was asked for and not from what came back, because
        nothing came back: both methods answer ``{"ok": true}`` alone. Echoing
        Slack's response would hand a caller a bare ``ok`` with no way to tell
        which message in which conversation it was about.
        """
        return json.dumps(
            {
                "ok": True,
                "chat_id": chat_id,
                "message_id": message_id,
                # The state of that message now, which after a swallowed
                # ``already_pinned`` or ``no_pin`` is the state that was asked
                # for just as much as after a call that did the work.
                "pinned": not remove,
            },
            ensure_ascii=False,
        )

    def get_tools(self) -> list[Tool]:
        """Return the request-scoped Slack pin tool."""
        return [LocalFunction(card=self._pin_card(), func=self.pin_message)]

    @staticmethod
    def _pin_card() -> ToolCard:
        """The card for ``pin_message``.

        Unconditional: there is one argument shape, and it does not vary with
        any setting. It names no other tool, because this one is mounted on its
        own decision and a model holding this card may hold no other.
        """
        return ToolCard(
            name="pin_message",
            description=(
                "Pin a message in this Slack conversation so the "
                "conversation keeps it at hand, or unpin one that is pinned. "
                "A pinned message is shared: everybody in the conversation "
                "sees the same short list, and pinning is a change they all "
                "see rather than a private bookmark. Pin what the people here "
                "will want to find again -- a decision, a standing link, the "
                "message a thread keeps returning to -- and leave everything "
                "else unpinned."
                "\nWhich conversation. It acts in the conversation this "
                "request came from, and there is no argument for naming "
                "another one. A message that lives somewhere else cannot be "
                "pinned from here."
                "\nWhich message. Pass message_id to pin. Pass message_id "
                "with remove set to true to unpin."
                "\nMessage identifiers. A message_id is an opaque Slack "
                "identifier and not a date: take it unchanged from a result "
                "that reported one, never build one, and never derive one "
                "from a time somebody named."
                "\nRepeating a call. Pinning a message that is already "
                "pinned succeeds and changes nothing, and unpinning one that "
                "is not pinned does the same, so a call that is repeated or "
                "that races somebody else is safe."
                "\nWhen the list is full. Nothing is unpinned to make room: "
                "a conversation holds a limited number of pins, and a call "
                "made when it is full fails and says so. That is for a person "
                "to resolve by unpinning something, and calling again will "
                "not help."
                "\nWhat comes back. The result is the conversation, the "
                "message, and whether that message is pinned now. Nothing "
                "about the conversation and nothing about the message's "
                "content comes back, so read the message elsewhere if you "
                "need to quote it."
            ),
            input_params={
                "type": "object",
                "properties": {
                    "message_id": {
                        "type": "string",
                        "description": (
                            "The message to pin or unpin, named by its Slack "
                            "identifier, such as 1758123456.123456. Copy it "
                            "verbatim from a result that reported it and pass "
                            "it unchanged. It names a message in this "
                            "conversation; an id taken from another "
                            "conversation names nothing here."
                        ),
                    },
                    "remove": {
                        "type": "boolean",
                        "default": False,
                        "description": (
                            "Unpin the message instead of pinning it. Omit it "
                            "to pin."
                        ),
                    },
                },
                "required": ["message_id"],
            },
        )


__all__ = ["SlackPinToolkit", "slack_pin_request_metadata"]
