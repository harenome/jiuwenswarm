# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The bookmark bar of a Slack conversation: read it, and change it.

Two tools, because there are two scopes and two policies. ``read_bookmarks``
lists what a conversation has bookmarked and needs ``bookmarks:read``;
``write_bookmark`` adds, edits and removes and needs ``bookmarks:write``.
``permissions.tools`` is keyed by name and matched exactly, so one tool is one
policy: folding the two into a single name would make *may see the bookmarks,
may not change them* permanently inexpressible, and an operator asked to
choose between all of it and none of it picks none. It is the split
``slack_pins`` makes between listing pins and pinning, for the same reason.

Three actions and one write tool, though, rather than three tools. Add, edit
and remove are the same authority over the same bar: an operator who may let
the bot put a link there has already decided the bar is the bot's to change,
and a policy that allowed adding while refusing removing would be a policy
nobody wrote. One name, one decision.

**The conversation is not an argument.** It comes from the trusted request
metadata the Slack gateway stamps, and there is deliberately no ``chat_id`` on
either tool. The argument is ``slack_pins``':

* Writing into a conversation the request did not come from is acting where
  nobody asked. A bookmark is shared furniture, visible at the top of the room
  to everybody in it, so a bookmark written in the wrong room is a change
  everybody there sees, made on nobody's request.
* The membership gate the reading tools use is a *read* rule. ``members(S)
  subset-of members(T)`` says nobody in S learns anything they could not
  already learn, which is a statement about disclosure and authorises nothing
  about writing into T. Reusing it here would be reusing the words rather than
  the argument.

``read_bookmarks`` takes no ``chat_id`` either, and there the reason is only
half the same. Listing another room's bookmarks *is* a disclosure and the read
rule would be the right rule to write for it. It is not written, and a target
argument that cannot be gated is worse than no target argument, so the tool
reads the conversation the request came from and nothing else. Both arguments
are additive and can be added later, under whatever rule is then written.

Not a condition on either: the history policy word. That says how far this
conversation may read *scrollback*, and a bookmark is not a message -- it is a
title and a link somebody put on the wall, closer to a channel topic than to
anything the history gate governs. Coupling the two would take both tools away
from ``origin`` deployments, which are most of them, for a reason that has
nothing to do with bookmarks.

Observations from a probe of a real workspace on 2026-09-14. They are recorded
as observations because that is what they are, and none of them is encoded
here as a constant or a bound:

* ``link`` is an arbitrary URL. ``https://example.com/probe?a=1#f`` came back
  with its query and its fragment intact. The field is spelled ``url`` rather
  than ``permalink`` because ``permalink`` already means *link to a message*
  in this connector's vocabulary, and this is not that.
* ``link`` was the only ``type`` ``bookmarks.add`` accepted. ``message`` and
  ``file`` both came back ``invalid_bookmark_type``; the ``file_not_found``
  and ``invalid_entity_id`` errors Slack documents belong to bookmarks made
  by other means. ``bookmark_type``, ``entity_id`` and ``access_level`` are
  exposed all the same: one optional argument each, nothing hidden today, and
  still correct on the day Slack widens the endpoint. ``invalid_bookmark_type``
  is surfaced unchanged rather than pre-empted here, because the set of types
  that work is Slack's to change and a local list of them would be wrong
  silently.
* A folder cannot be created through the API. A bookmark with no link and a
  ``type=folder`` both came back ``invalid_arguments``.
  ``parent_bookmark_id`` is exposed anyway, because nesting a bookmark under a
  folder somebody made in the Slack UI is real capability.
* ``edit`` appeared to preserve the fields the call omitted. That is
  undocumented, so it is not relied on: :meth:`SlackBookmarkToolkit.edit` does
  read-modify-write. It reads the bookmark from ``bookmarks.list``, merges the
  caller's changes over it and sends the editable fields explicitly, which is
  one extra unpaginated call and is correct whichever way Slack behaves now or
  later. The cost is that ``edit`` needs ``bookmarks:read`` beside
  ``bookmarks:write``, which is stated on the refusal and in the channel docs.
* ``add`` is not idempotent. Two identical calls produced two bookmarks with
  two ids. The new id comes back in the result and nothing here deduplicates,
  because deduplicating is not what the tool says it does.
* ``rank`` is a string, ``'g'`` and ``'p'`` among the values seen. It is a
  lexicographic sort key, and the card says so, because a reader who assumes a
  number sorts the bar wrongly and silently.

**Everything is returned, and nothing is truncated.** ``bookmarks.list`` takes
a conversation and nothing else: no cursor, no page size, no window. There is
nothing to page, so there is no bound to invent -- and inventing one would put
this tool where ``read_pinned_messages`` already is, reporting ``status:
complete`` over a list it cut. Slack documents a ceiling on how many bookmarks
a conversation may hold; that ceiling is Slack's, the number is not quoted
anywhere here, and a conversation that reaches it gets ``too_many_bookmarks``
surfaced unchanged.

A model-supplied value is echoed back into a refusal through
``redact_credentials`` and never otherwise. What Slack returns is passed
through as it came: a bookmark is a title and a URL that people in the room
curated in the Slack UI, and a URL mangled on the way out is a URL the caller
cannot use, which is the one thing this tool exists to hand over.

Only cross-cutting primitives are borrowed from the sibling modules: the
reading of a Slack response, the rule for when a refusal is worth retrying,
the pass that keeps a credential out of an error code, the failure type that
names the refused method the way Slack's documentation spells it, and the
emoji normaliser that decides what ``emoji_name`` means across this connector.
They are security- or correctness-critical and a fix to one copy would not
reach a second.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Mapping
from typing import Any

from openjiuwen.core.foundation.tool import LocalFunction, Tool, ToolCard

from jiuwenswarm.agents.harness.common.tools.slack_history import (
    _DEFAULT_RETRY_AFTER_SECONDS,
    SlackWorkspaceClients,
    _SlackCallFailure,
    _as_mapping,
    _iso_utc,
    _positive_timestamp,
    _retry_after_seconds,
    _safe_error_code,
    redact_credentials,
    shared_slack_workspaces,
)
from jiuwenswarm.agents.harness.common.tools.slack_reactions import (
    _ReactionRefused,
    _emoji_name,
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

#: Where the connector leaves the conversation this request arrived in.
SLACK_CHANNEL_ID_KEY = "slack_channel_id"

#: The four SDK method names, in the SDK's spelling. ``_SlackCallFailure.where``
#: turns each back into the ``bookmarks.add`` an operator looks up.
_BOOKMARK_ADD = "bookmarks_add"
_BOOKMARK_EDIT = "bookmarks_edit"
_BOOKMARK_REMOVE = "bookmarks_remove"
_BOOKMARK_LIST = "bookmarks_list"

#: The two scopes. Which one a refusal names is decided by the method Slack
#: declined, not by which tool was called: ``edit`` calls both, and telling an
#: operator to grant the write scope when the listing half was refused would
#: send them to the wrong switch.
_BOOKMARK_WRITE_SCOPE = "bookmarks:write"
_BOOKMARK_READ_SCOPE = "bookmarks:read"

#: The three things ``write_bookmark`` can do.
_ACTION_ADD = "add"
_ACTION_EDIT = "edit"
_ACTION_REMOVE = "remove"
_ACTIONS = (_ACTION_ADD, _ACTION_EDIT, _ACTION_REMOVE)

#: Which optional arguments each action can carry into a Slack call. An
#: argument outside its action's set is refused by name rather than dropped:
#: ``bookmarks.edit`` takes the title, the link and the emoji and nothing else,
#: so an ``access_level`` passed to an edit is a change the caller asked for
#: and would not get, and swallowing it would report that change as made.
#:
#: The refusal is only fair while every argument here is one the caller chose,
#: so no argument that belongs to a subset of the actions may declare a
#: ``default`` on the tool card. A default is something the card promises will
#: be sent, and an argument sent on the card's word rather than the caller's is
#: one this table would refuse the caller for.
_ACTION_ARGUMENTS: dict[str, frozenset[str]] = {
    _ACTION_ADD: frozenset(
        {
            "title",
            "url",
            "emoji_name",
            "bookmark_type",
            "entity_id",
            "access_level",
            "parent_bookmark_id",
        }
    ),
    _ACTION_EDIT: frozenset({"bookmark_id", "title", "url", "emoji_name"}),
    _ACTION_REMOVE: frozenset({"bookmark_id", "quip_section_id"}),
}

#: The indefinite article each action name takes. The refusal below names the
#: action in running prose twice, and no single word fits all three: it is "an
#: add", "an edit" and "a remove". Spelled out per action rather than worked
#: out from the first letter, because the article follows the sound and not the
#: spelling; a letter test is right for these three by luck and would be wrong
#: for a word such as "hour". A name absent from the table falls back to that
#: letter test, because a wrong article reads oddly and a KeyError would lose
#: the caller the refusal it was owed.
_ACTION_ARTICLE = {_ACTION_ADD: "an", _ACTION_EDIT: "an", _ACTION_REMOVE: "a"}

#: Our name for each editable field Slack spells its own way. Read where a
#: merged edit decides which of the three values to send.
_OUR_NAME_FOR = {"title": "title", "link": "url", "emoji": "emoji_name"}

#: What a refusal means, for the ones worth explaining. Everything else falls
#: through to naming the method, which is what an operator looks a code up by.
_BOOKMARK_FAILURE_DETAIL = {
    "too_many_bookmarks": (
        "this conversation holds as many bookmarks as Slack allows, so nothing"
        " further can be added until somebody removes one; calling again will"
        " not help"
    ),
    "bookmark_not_found": (
        "this conversation holds no bookmark with that id; a bookmark id is not"
        " transferable between conversations, so one taken from elsewhere names"
        " nothing here"
    ),
    "invalid_bookmark_type": (
        "Slack will not create a bookmark of that type through the API. A link"
        " bookmark, which is the default, is what it accepts; a second call"
        " with the same type will not change that"
    ),
    "invalid_arguments": (
        "Slack refused the arguments as a set. A bookmark with no url and a"
        " folder are both refused this way: a folder is made by a person in"
        " Slack and cannot be created through the API, although a bookmark can"
        " be placed inside one that already exists"
    ),
    "channel_not_found": (
        "Slack does not recognise this conversation, or the bot is not in it"
    ),
}

#: How much of a model-supplied value is echoed back inside a refusal. Long
#: enough to show what was passed, short enough that a runaway argument does
#: not become the whole tool result.
_MAX_ECHOED_CHARS = 60

#: Wall clock for one tool call, retries and every Slack call it makes
#: included. ``edit`` makes two, so the budget is set once per invocation
#: rather than once per call: two calls each free to spend the whole of it
#: would be a tool that takes twice as long as it says it does.
_TIMEOUT_SECONDS = 30.0

#: How many times one Slack call will wait out a rate limit before giving up.
#: A bookmark is a small write nobody is waiting on for long, and the deadline
#: above usually bites first.
_MAX_RATE_LIMIT_RETRIES = 2


class _BookmarkRefused(RuntimeError):
    """A refusal decided here, carrying the code and the text to report.

    Separate from :class:`_SlackCallFailure` because the two mean opposite
    things to a caller. A call failure is Slack saying no; this is us saying no
    before Slack was reached, and retrying it is the one thing that cannot
    help.
    """

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


def slack_bookmark_request_metadata(
    channel_id: str | None,
    metadata: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Return trusted metadata for a request that may touch bookmarks.

    Two request shapes, the same two the pin tool accepts, and for the same
    reason: in both the conversation is named by the gateway before the turn
    starts, and in neither is it part of the tool argument surface.

    * An inbound Slack turn arrives on the ``slack`` transport, and the
      connector stamps the conversation the message came from.
    * A scheduled run arrives on the cron transport and is honoured only where
      the scheduler marked it as its own. A job is created in a conversation
      and posts into it, so a bookmark written there is a change to the
      conversation the job already writes to. Without the marker a stray
      ``slack_channel_id`` left on some other request cannot reach these tools.

    Not a condition: the history policy word, for the reason the module
    docstring gives.
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
    if not str(metadata.get(SLACK_CHANNEL_ID_KEY) or "").strip():
        return {}
    return dict(metadata)


def _echo(value: Any) -> str:
    """One model-supplied value, safe and short enough to quote in a refusal."""
    text, _redacted, _truncated = redact_credentials(value, cap=_MAX_ECHOED_CHARS)
    return text.strip()


def _article(action: str) -> str:
    """``"a"`` or ``"an"`` for one action name, for a refusal that reads.

    The refusal these words go into is read by a model deciding what to do
    next, so it is prose and has to hold together as prose. One hardcoded
    article was wrong for two of the three actions and would be wrong for a
    fourth at random.
    """
    known = _ACTION_ARTICLE.get(action)
    if known:
        return known
    return "an" if action[:1].lower() in "aeiou" else "a"


def _clean(value: Any) -> str | None:
    """The caller's argument, or ``None`` where the caller passed nothing.

    ``None`` and an omitted argument are the same thing and mean *leave this
    alone*. An empty string is not the same thing: it is a caller saying
    *clear this*, and it is passed to Slack as the empty value it is rather
    than being folded into the omitted case here. Whether Slack accepts it is
    Slack's answer to give.
    """
    if value is None:
        return None
    return str(value).strip()


def _write_refusal_json(
    code: str,
    detail: str = "",
    *,
    chat_id: str = "",
    action: str = "",
    bookmark_id: str = "",
) -> str:
    """One write refusal, in a shape that never claims to know what it does not.

    There is no field here for the state of the bar. A call Slack refused
    establishes nothing about what the conversation now holds -- a bookmark
    that could not be added because the bar is full did not thereby remove
    anything -- and a caller deciding whether to try something else acts on the
    difference between *this call did not do it* and *it is not there*.
    """
    payload: dict[str, Any] = {"ok": False, "error": code}
    if detail:
        payload["detail"] = detail
    if chat_id:
        payload["chat_id"] = chat_id
    if action:
        payload["action"] = action
    if bookmark_id:
        payload["bookmark_id"] = bookmark_id
    logger.warning(
        "slack bookmark write refused: %s%s", code, f" -- {detail}" if detail else ""
    )
    return json.dumps(payload, ensure_ascii=False)


def _read_refusal_json(code: str, detail: str = "", *, chat_id: str = "") -> str:
    """One listing refusal, in the shape a successful listing already has.

    The empty ``bookmarks`` list is there on purpose, on the rule
    ``slack_history`` states for its own pin listing: a refusal and a result
    that differ in shape are two things for a caller to read, and the empty
    list is there so they do not.
    """
    payload: dict[str, Any] = {"ok": False, "error": code}
    if detail:
        payload["detail"] = detail
    if chat_id:
        payload["chat_id"] = chat_id
    payload["bookmarks"] = []
    logger.warning(
        "slack bookmarks read refused: %s%s", code, f" -- {detail}" if detail else ""
    )
    return json.dumps(payload, ensure_ascii=False)


def _scope_for(method: str) -> str:
    """The scope the refused call needs.

    Keyed on the method rather than on the tool: ``edit`` lists before it
    writes, and an operator told to grant the write scope when the listing half
    was declined is being sent to the wrong switch.
    """
    return _BOOKMARK_READ_SCOPE if method == _BOOKMARK_LIST else _BOOKMARK_WRITE_SCOPE


def _normalize_bookmark(item: Mapping[str, Any]) -> dict[str, Any]:
    """One Slack bookmark in this connector's vocabulary.

    Our shape rather than Slack's. The names are the ones the sibling tools
    already use -- ``bookmark_id`` rather than ``id``, ``url`` rather than
    ``link``, ``emoji_name`` rather than ``emoji``, ``created_iso_utc`` and
    ``updated_iso_utc`` for the two instants -- so that a value read here can
    be passed to ``write_bookmark`` and quoted in a reply without a translation
    step nobody wrote down.

    A field Slack left empty is absent rather than present and null. Most
    bookmarks carry no ``entity_id``, no ``shortcut_id``, no ``app_id``, no
    parent and no access level, and a row of nulls on every record is a row a
    reader learns to skip. Nothing is dropped that Slack filled in.
    """
    record: dict[str, Any] = {
        "bookmark_id": str(item.get("id") or "").strip(),
        "title": str(item.get("title") or ""),
        "url": str(item.get("link") or ""),
        # A shortcode without its colons, which is what ``emoji_name`` means
        # everywhere else in this connector. Slack stores it with colons.
        "emoji_name": str(item.get("emoji") or "").strip().strip(":"),
        "bookmark_type": str(item.get("type") or "").strip(),
        # A lexicographic sort key rather than a number. Passed through as the
        # string it is; sorting it is the caller's to do and the card says how.
        "rank": str(item.get("rank") or ""),
    }
    optional = {
        "icon_url": str(item.get("icon_url") or "").strip(),
        "entity_id": str(item.get("entity_id") or "").strip(),
        "access_level": str(item.get("access_level") or "").strip(),
        "parent_bookmark_id": str(item.get("parent_id") or "").strip(),
        "shortcut_id": str(item.get("shortcut_id") or "").strip(),
        "app_id": str(item.get("app_id") or "").strip(),
        "updated_by_user_id": str(item.get("last_updated_by_user_id") or "").strip(),
    }
    record.update({key: value for key, value in optional.items() if value})
    for key, source in (
        ("created_iso_utc", "date_created"),
        ("updated_iso_utc", "date_updated"),
    ):
        stamped = _iso_utc(_positive_timestamp(item.get(source)))
        if stamped:
            record[key] = stamped
    # Empty strings go the same way the absent optionals did, so that a reader
    # never has to tell "" and absent apart.
    return {key: value for key, value in record.items() if value != ""}


class SlackBookmarkToolkit:
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
        """Refresh the request-scoped metadata without recreating the tools."""
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

    @staticmethod
    def _chat_id(metadata: Mapping[str, Any]) -> str:
        return str(metadata.get(SLACK_CHANNEL_ID_KEY) or "").strip()

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

        A bookmark is a write, and a bookmark bar is a conversation's own, so
        an install that cannot be settled is refused rather than guessed at.
        """
        slack = await self._workspaces.settings_for(
            str(metadata.get(METADATA_TEAM_KEY) or "").strip()
        )
        self._bot_token = str(slack.get("bot_token") or "").strip()

    def _get_client(self) -> Any:
        if self._client is not None:
            return self._client
        return self._workspaces.client_for(self._bot_token)

    async def _call(
        self, method: str, deadline: float, **kwargs: Any
    ) -> dict[str, Any]:
        """Make one Slack call, reducing any refusal to a credential-free code.

        The deadline is passed in rather than computed here because ``edit``
        makes two calls and they share one budget, and a rate-limit wait is
        spent from it rather than added to it.

        Both failure shapes are read. Slack answers a refused bookmark call
        with HTTP 200 and ``{"ok": false, "error": ...}``; the SDK raises that
        as ``SlackApiError``, and an older one hands the body back instead, so
        a code arriving one way on one deployment and the other way on the next
        would otherwise be two behaviours.
        """
        try:
            client = self._get_client()
        except _SlackCallFailure as exc:
            exc.method = exc.method or method
            raise
        retries = 0
        while True:
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                raise _SlackCallFailure(self._timeout_code(method), method)
            try:
                response = await asyncio.wait_for(
                    getattr(client, method)(**kwargs), timeout=remaining
                )
            except TimeoutError:
                raise _SlackCallFailure(self._timeout_code(method), method) from None
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
            return data

    @staticmethod
    def _timeout_code(method: str) -> str:
        """Named for what ran out, and on which half of the work."""
        if method == _BOOKMARK_LIST:
            return "bookmarks_read_timed_out"
        return "bookmark_write_timed_out"

    def _failure_detail(self, exc: _SlackCallFailure) -> str:
        """The sentence a caller acts on, for one refusal from Slack."""
        detail = _BOOKMARK_FAILURE_DETAIL.get(exc.code)
        if detail:
            return detail
        detail = f"Slack refused {exc.where}"
        if exc.code == "missing_scope":
            detail += (
                f"; that call needs the {_scope_for(exc.method)} scope, which"
                f" this installation does not hold"
            )
        return detail

    # ── write ────────────────────────────────────────────────────────────────

    async def write_bookmark(
        self,
        action: str,
        bookmark_id: str | None = None,
        quip_section_id: str | None = None,
        title: str | None = None,
        url: str | None = None,
        emoji_name: str | None = None,
        bookmark_type: str | None = None,
        entity_id: str | None = None,
        access_level: str | None = None,
        parent_bookmark_id: str | None = None,
    ) -> str:
        """Add a bookmark to this conversation, change one, or take one away.

        The conversation is the one the request arrived in, taken from trusted
        metadata, for the reason the module docstring gives.

        Everything that costs no Slack call is settled first. A write refused
        for a bad argument must not have reached Slack to find that out, and an
        argument the chosen action cannot carry is refused by name rather than
        dropped on the floor.
        """
        metadata = self._runtime_metadata()
        chat_id = self._chat_id(metadata)
        chosen = str(action or "").strip().lower()
        supplied = {
            "bookmark_id": _clean(bookmark_id),
            "quip_section_id": _clean(quip_section_id),
            "title": _clean(title),
            "url": _clean(url),
            "emoji_name": _clean(emoji_name),
            "bookmark_type": _clean(bookmark_type),
            "entity_id": _clean(entity_id),
            "access_level": _clean(access_level),
            "parent_bookmark_id": _clean(parent_bookmark_id),
        }

        if not chat_id:
            # The same code the reading tools refuse an untrusted request with.
            # It is the same fact -- no Slack path settled which conversation
            # this request is about -- and one word for it means an operator
            # searching for it finds every place it can happen.
            return _write_refusal_json(
                "trusted_slack_channel_context_required",
                "this request carries no Slack conversation, so there is no"
                " bookmark bar to write to and nothing to fall back to",
            )
        try:
            self._check_action(chosen, supplied)
            arguments = await self._write_arguments(
                chat_id, chosen, supplied, metadata
            )
        except _BookmarkRefused as refusal:
            return _write_refusal_json(
                refusal.code,
                refusal.detail,
                chat_id=chat_id,
                action=chosen if chosen in _ACTIONS else "",
                bookmark_id=supplied["bookmark_id"] or "",
            )
        except _SlackCallFailure as exc:
            # The read half of an edit's read-modify-write. Reported under the
            # code a caller keys on, with the call named beside it, rather than
            # being turned into a failure of the write that never happened.
            return _write_refusal_json(
                exc.code,
                self._failure_detail(exc),
                chat_id=chat_id,
                action=chosen,
                bookmark_id=supplied["bookmark_id"] or "",
            )

        method, kwargs, deadline = arguments
        try:
            response = await self._call(method, deadline, **kwargs)
        except _SlackCallFailure as exc:
            return _write_refusal_json(
                exc.code,
                self._failure_detail(exc),
                chat_id=chat_id,
                action=chosen,
                bookmark_id=supplied["bookmark_id"] or "",
            )

        return self._write_result(chat_id, chosen, supplied, response)

    @staticmethod
    def _check_action(action: str, supplied: Mapping[str, str | None]) -> None:
        """Settle the action and the arguments it can carry, before any call."""
        if not action:
            raise _BookmarkRefused(
                "action_required",
                "action is required. Pass add to put a bookmark on this"
                " conversation, edit to change one, or remove to take one off.",
            )
        if action not in _ACTIONS:
            raise _BookmarkRefused(
                "action_unknown",
                f"action was {_echo(action)!r}, which is not one of"
                f" {', '.join(_ACTIONS)}.",
            )

        allowed = _ACTION_ARGUMENTS[action]
        unusable = sorted(
            name
            for name, value in supplied.items()
            if value is not None and name not in allowed
        )
        if unusable:
            raise _BookmarkRefused(
                "argument_not_valid_for_action",
                f"{', '.join(unusable)} cannot be applied by"
                f" {_article(action)} {action}, so passing it would ask for a"
                f" change that would not be made."
                f" {_article(action).capitalize()} {action} takes"
                f" {', '.join(sorted(allowed))}.",
            )

        if action == _ACTION_ADD and not supplied["title"]:
            raise _BookmarkRefused(
                "title_required",
                "title is required when adding a bookmark; it is the text"
                " everybody in the conversation sees on the bar.",
            )
        if action == _ACTION_EDIT and not supplied["bookmark_id"]:
            raise _BookmarkRefused(
                "bookmark_id_required",
                "bookmark_id is required when editing; take it unchanged from a"
                " result that reported one.",
            )
        if action == _ACTION_REMOVE and not (
            supplied["bookmark_id"] or supplied["quip_section_id"]
        ):
            raise _BookmarkRefused(
                "bookmark_id_required",
                "removing needs the bookmark named: pass bookmark_id, or"
                " quip_section_id for a Quip section bookmark.",
            )
        if action == _ACTION_EDIT and not any(
            supplied[name] is not None for name in ("title", "url", "emoji_name")
        ):
            raise _BookmarkRefused(
                "nothing_to_edit",
                "an edit changes the title, the url or the emoji_name, and none"
                " of the three was given, so there is no change to make.",
            )

    @staticmethod
    def _slack_emoji(emoji_name: str) -> str:
        """Slack's ``emoji`` argument for the ``emoji_name`` a caller passed.

        ``emoji_name`` means a shortcode without colons everywhere else in this
        connector, and models pass the character as often as the name because
        the field reads like it wants one. Both are accepted, through the same
        normaliser the reaction tool uses, so the word means one thing across
        the connector. Slack stores and takes the colon form, so the colons go
        back on here.

        An empty string survives as an empty string: that is a caller clearing
        the emoji, and what Slack does with it is Slack's to say.
        """
        if not emoji_name:
            return ""
        return f":{_emoji_name(emoji_name)}:"

    async def _write_arguments(
        self,
        chat_id: str,
        action: str,
        supplied: Mapping[str, str | None],
        metadata: Mapping[str, Any],
    ) -> tuple[str, dict[str, Any], float]:
        """The Slack call this write makes, its arguments, and its deadline.

        One deadline for the whole invocation, armed here, because ``edit``
        reads before it writes and the two share the budget.
        """
        try:
            await self._load_settings(metadata)
        except SlackWorkspaceUnresolved as unresolved:
            raise _BookmarkRefused(
                unresolved.code, unresolved.detail
            ) from None
        deadline = float(self._monotonic()) + self._timeout_seconds

        try:
            emoji = self._slack_emoji(supplied["emoji_name"] or "")
        except _ReactionRefused as refusal:
            raise _BookmarkRefused(
                # The reaction tool's own code for the same fact, so that a
                # caller who has met one of these has met both.
                refusal.code,
                refusal.detail,
            ) from None

        if action == _ACTION_REMOVE:
            kwargs: dict[str, Any] = {"channel_id": chat_id}
            if supplied["bookmark_id"]:
                kwargs["bookmark_id"] = supplied["bookmark_id"]
            if supplied["quip_section_id"]:
                kwargs["quip_section_id"] = supplied["quip_section_id"]
            return _BOOKMARK_REMOVE, kwargs, deadline

        if action == _ACTION_ADD:
            kwargs = {
                "channel_id": chat_id,
                "title": supplied["title"],
                # Slack's ``type``, which it requires. ``link`` is the default
                # because on 2026-09-14 it was the only value the endpoint
                # accepted; the argument exists so that a caller can name
                # another one the day Slack takes it, and an unaccepted value
                # comes back as Slack's own invalid_bookmark_type.
                "type": supplied["bookmark_type"] or "link",
            }
            for name, slack_name in (
                ("url", "link"),
                ("entity_id", "entity_id"),
                ("access_level", "access_level"),
                ("parent_bookmark_id", "parent_id"),
            ):
                if supplied[name] is not None:
                    kwargs[slack_name] = supplied[name]
            if supplied["emoji_name"] is not None:
                kwargs["emoji"] = emoji
            return _BOOKMARK_ADD, kwargs, deadline

        # ``edit``: read, modify, write. Slack appeared to keep the fields an
        # edit omitted, but that is undocumented and is not relied on. The
        # current record is read from the one unpaginated listing, the caller's
        # changes are merged over it, and the editable fields are sent
        # explicitly, so the call is correct whether Slack preserves omitted
        # fields or clears them.
        current = await self._read_bookmark(chat_id, supplied["bookmark_id"], deadline)
        merged = {
            "title": supplied["title"]
            if supplied["title"] is not None
            else str(current.get("title") or ""),
            "link": supplied["url"]
            if supplied["url"] is not None
            else str(current.get("link") or ""),
            "emoji": emoji
            if supplied["emoji_name"] is not None
            else str(current.get("emoji") or "").strip(),
        }
        kwargs = {"channel_id": chat_id, "bookmark_id": supplied["bookmark_id"]}
        for slack_name, value in merged.items():
            # A merged value that is empty because it was already empty and the
            # caller said nothing about it is left out. Sending it would be
            # sending an empty argument Slack may refuse, and omitting it
            # cannot change the outcome under either behaviour: the field was
            # empty before the call and is empty after it either way. A caller
            # who passed an empty string to clear a field that has something in
            # it is a different case, and that empty value is sent.
            if value or supplied[_OUR_NAME_FOR[slack_name]] is not None:
                kwargs[slack_name] = value
        return _BOOKMARK_EDIT, kwargs, deadline

    async def _read_bookmark(
        self, chat_id: str, bookmark_id: str, deadline: float
    ) -> dict[str, Any]:
        """The bookmark an edit is about, as Slack currently holds it.

        One unpaginated call. ``bookmarks.list`` takes a conversation and
        nothing else, so the whole bar comes back and the record is picked out
        of it here rather than fetched by id -- Slack offers no call that
        fetches one bookmark.

        An id that names nothing is refused here, before the edit is attempted,
        and under Slack's own word for it: the listing is already in hand, so
        the answer is certain and a round trip to be told the same thing is a
        round trip spent on nothing.
        """
        listing = await self._call(_BOOKMARK_LIST, deadline, channel_id=chat_id)
        for item in listing.get("bookmarks") or []:
            if not isinstance(item, Mapping):
                continue
            if str(item.get("id") or "").strip() == bookmark_id:
                return dict(item)
        raise _BookmarkRefused(
            "bookmark_not_found",
            f"this conversation holds no bookmark with the id"
            f" {_echo(bookmark_id)!r}. A bookmark id is not transferable"
            f" between conversations, so one taken from elsewhere names nothing"
            f" here.",
        )

    @staticmethod
    def _write_result(
        chat_id: str,
        action: str,
        supplied: Mapping[str, str | None],
        response: Mapping[str, Any],
    ) -> str:
        """The one success shape, ours rather than Slack's.

        ``add`` and ``edit`` come back with the bookmark Slack now holds, which
        is the one place the new id exists: adding is not idempotent, two
        identical adds make two bookmarks, and the id is how the caller names
        the one it just made. ``remove`` answers ``{"ok": true}`` and nothing
        else, so its result is built from what was asked for.
        """
        payload: dict[str, Any] = {"ok": True, "chat_id": chat_id, "action": action}
        bookmark = response.get("bookmark")
        if isinstance(bookmark, Mapping):
            record = _normalize_bookmark(bookmark)
            if record.get("bookmark_id"):
                payload["bookmark_id"] = record["bookmark_id"]
            payload["bookmark"] = record
        else:
            if supplied["bookmark_id"]:
                payload["bookmark_id"] = supplied["bookmark_id"]
            if supplied["quip_section_id"]:
                payload["quip_section_id"] = supplied["quip_section_id"]
        logger.info("slack bookmark: %s in %s", action, chat_id)
        return json.dumps(payload, ensure_ascii=False)

    # ── read ─────────────────────────────────────────────────────────────────

    async def read_bookmarks(self) -> str:
        """Return every bookmark on this conversation's bar.

        No arguments at all. ``bookmarks.list`` takes a conversation and
        nothing else -- no cursor, no page size, no window -- and the
        conversation is the one the request arrived in, so a window or a limit
        here would be an argument that could not be honoured.

        Nothing is truncated and nothing is filtered. There is no paging to
        resume, so a bound imposed here would leave a caller with a short list
        and no way to ask for the rest.
        """
        metadata = self._runtime_metadata()
        chat_id = self._chat_id(metadata)
        if not chat_id:
            return _read_refusal_json(
                "trusted_slack_channel_context_required",
                "this request carries no Slack conversation, so there is no"
                " bookmark bar to read and nothing to fall back to",
            )

        try:
            await self._load_settings(metadata)
        except SlackWorkspaceUnresolved as unresolved:
            return _read_refusal_json(
                unresolved.code, unresolved.detail, chat_id=chat_id
            )
        deadline = float(self._monotonic()) + self._timeout_seconds
        try:
            listing = await self._call(_BOOKMARK_LIST, deadline, channel_id=chat_id)
        except _SlackCallFailure as exc:
            return _read_refusal_json(
                exc.code, self._failure_detail(exc), chat_id=chat_id
            )

        records = [
            _normalize_bookmark(item)
            for item in (listing.get("bookmarks") or [])
            if isinstance(item, Mapping)
        ]
        logger.info(
            "slack bookmarks: listed %d in %s",
            len(records),
            chat_id,
        )
        return json.dumps(
            {
                "ok": True,
                "chat_id": chat_id,
                # The count is the length of the list beside it and not a
                # coverage claim. There is nothing to page and nothing was cut,
                # so there is no partial state to report.
                "bookmarks_returned": len(records),
                "bookmarks": records,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )

    # ── cards ────────────────────────────────────────────────────────────────

    def get_tools(self) -> list[Tool]:
        """Return the request-scoped Slack bookmark tools."""
        return [
            LocalFunction(card=self._write_card(), func=self.write_bookmark),
            LocalFunction(card=self._read_card(), func=self.read_bookmarks),
        ]

    @staticmethod
    def _write_card() -> ToolCard:
        """The card for ``write_bookmark``.

        Unconditional: there is one argument shape, and it does not vary with
        any setting. It names no other tool, because this one is mounted on its
        own decision and a model holding this card may hold no other.
        """
        return ToolCard(
            name="write_bookmark",
            description=(
                "Change the bookmark bar of this Slack conversation: add a "
                "bookmark, edit one, or remove one. A bookmark is shared "
                "furniture. It sits at the top of the conversation where "
                "everybody in it sees the same short row, so putting one there "
                "is a change they all see rather than a private note. Bookmark "
                "what the people here will keep going back to -- a dashboard, "
                "a runbook, the document a decision lives in -- and leave "
                "everything else alone."
                "\nWhich conversation. It acts on the conversation this "
                "request came from, and there is no argument for naming "
                "another one."
                "\nAdding. Pass action set to add with a title, which is the "
                "text people see, and a url. Adding is not idempotent: two "
                "identical calls make two bookmarks, so check what is already "
                "there before adding. The result carries the bookmark_id of "
                "the one just made, which is how it is named later."
                "\nEditing. Pass action set to edit with a bookmark_id and at "
                "least one of title, url and emoji_name. What is not named is "
                "left as it is. Nothing else about a bookmark can be changed "
                "by an edit."
                "\nRemoving. Pass action set to remove with a bookmark_id, or "
                "with quip_section_id for a Quip section bookmark. Removing "
                "takes the bookmark off the bar for everybody."
                "\nBookmark identifiers. A bookmark_id is an opaque Slack "
                "identifier: take it unchanged from a result that reported "
                "one, and never build one. It names a bookmark in this "
                "conversation; an id taken from another conversation names "
                "nothing here."
                "\nFolders. A folder cannot be created here. A folder made by "
                "a person in Slack can be used as a parent_bookmark_id, which "
                "puts the new bookmark inside it."
                "\nWhen the bar is full. Nothing is removed to make room: a "
                "conversation holds a limited number of bookmarks, and a call "
                "made when it is full fails and says so. That is for a person "
                "to resolve, and calling again will not help."
                "\nWhat comes back. Adding and editing return the bookmark as "
                "Slack now holds it. Removing returns the conversation and the "
                "bookmark that was named. A call that fails says so and claims "
                "nothing about what the bar now holds."
            ),
            input_params={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": list(_ACTIONS),
                        "description": (
                            "What to do: add a bookmark, edit one, or remove "
                            "one."
                        ),
                    },
                    "bookmark_id": {
                        "type": "string",
                        "description": (
                            "The bookmark to edit or remove, named by its "
                            "Slack identifier. Copy it verbatim from a result "
                            "that reported it. Required to edit, and required "
                            "to remove unless quip_section_id is given. Not "
                            "used when adding, because the id is what adding "
                            "produces."
                        ),
                    },
                    "quip_section_id": {
                        "type": "string",
                        "description": (
                            "The other way to name a bookmark when removing "
                            "one, for a bookmark that points at a section of a "
                            "Quip document. Use bookmark_id unless a result "
                            "reported one of these."
                        ),
                    },
                    "title": {
                        "type": "string",
                        "description": (
                            "The text everybody in the conversation sees on "
                            "the bar. Required when adding. When editing, pass "
                            "it only to change it."
                        ),
                    },
                    "url": {
                        "type": "string",
                        "description": (
                            "Where the bookmark points. Any URL, with its "
                            "query string and fragment kept as written. Pass "
                            "it when adding, and when editing only to change "
                            "it."
                        ),
                    },
                    "emoji_name": {
                        "type": "string",
                        "description": (
                            "The emoji shown beside the title, named as a "
                            "Slack shortcode without colons, such as chart "
                            "or books. The emoji character itself is accepted "
                            "too. Optional."
                        ),
                    },
                    "bookmark_type": {
                        "type": "string",
                        # No default is declared, and its absence is the fix
                        # for a refusal nobody earned. A schema default is a
                        # value the caller is told will be sent, so a model
                        # that honours it writes bookmark_type on every call,
                        # including the edits and the removes that cannot
                        # carry one; _ACTION_ARGUMENTS then refuses those
                        # calls by name for an argument the caller never
                        # chose. The default belongs where it is applied,
                        # which is the add path, and not on a surface all
                        # three actions share.
                        "description": (
                            "What kind of bookmark to add. Only an add takes "
                            "it, and pass it only to ask for something other "
                            "than a link: an add that leaves it out makes a "
                            "link bookmark, which is what Slack accepts here. "
                            "Another value is passed through and Slack "
                            "answers for it."
                        ),
                    },
                    "entity_id": {
                        "type": "string",
                        "description": (
                            "The thing a non-link bookmark points at, whose "
                            "meaning depends on bookmark_type. Leave it unset "
                            "for an ordinary link bookmark."
                        ),
                    },
                    "access_level": {
                        "type": "string",
                        "enum": ["read", "write"],
                        "description": (
                            "How much access the bookmark grants, where the "
                            "kind of bookmark has such a thing. Leave it unset "
                            "for an ordinary link bookmark."
                        ),
                    },
                    "parent_bookmark_id": {
                        "type": "string",
                        "description": (
                            "The folder to put the new bookmark inside, named "
                            "by its bookmark_id. Only a folder somebody made "
                            "in Slack can be a parent; one cannot be created "
                            "from here."
                        ),
                    },
                },
                "required": ["action"],
            },
        )

    @staticmethod
    def _read_card() -> ToolCard:
        """The card for ``read_bookmarks``.

        Takes nothing, promises everything, and says both. The sentence about
        ``rank`` is on the card rather than in the result because it is about
        how to use a value rather than about this particular listing.
        """
        return ToolCard(
            name="read_bookmarks",
            description=(
                "List the bookmarks on this Slack conversation's bar: the "
                "short row of links pinned across the top of the conversation "
                "that everybody in it sees. Read them to find out what this "
                "room already treats as its standing references before "
                "searching further afield, and to find the bookmark_id of one "
                "that has to be changed."
                "\nWhich conversation. The conversation this request came "
                "from, and no other. There is no argument for naming one."
                "\nWhat comes back. Every bookmark the conversation holds, in "
                "one listing. There is no paging and nothing is cut, so the "
                "list is the whole bar. Each entry carries its bookmark_id, "
                "its title, its url, and where Slack has them the emoji_name, "
                "the rank, the times it was created and last changed, and who "
                "last changed it. A field Slack left empty is absent rather "
                "than present and empty."
                "\nOrder. rank is a sort key and it is text, not a number: "
                "compare two of them as strings, and never read one as a "
                "position or a count. The listing comes back in the order "
                "Slack returned it."
                "\nTakes no arguments."
            ),
            input_params={"type": "object", "properties": {}, "required": []},
        )



__all__ = ["SlackBookmarkToolkit", "slack_bookmark_request_metadata"]
