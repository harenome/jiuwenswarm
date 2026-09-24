# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Post a Slack message, change one, and take one down.

Three tools, and the first thing to say about them is what they are not. The
connector decides where every *reply* goes, through four rungs that settle a
destination when nobody named one. These do not go near that. Here somebody did
name one, and the whole of what this module adds is the right to say where --
``chat_id`` -- under a word an operator wrote down.

**The conversation is an argument, and how far it may reach is
``channels.slack.write``.** Four words, widening: ``disabled`` mounts nothing,
``origin`` declares no ``chat_id`` at all, ``members`` lets one be named under
an inverted membership rule, ``open`` lets any be named. The argument for the
inversion, and for why this is a key of its own rather than a wider reading of
``history``, is in :mod:`jiuwenswarm.common.slack_write_policy`, which is where
the word this gate acts on is defined.

The rule is ``members(T) subset-of members(S)`` for a turn in ``S`` writing into
``T``, read as *post only where everyone who will see it could already have seen
the conversation it came from*. It is the reading rule evaluated in the
direction the content is travelling, and the two are not interchangeable: the
read rule authorises nothing about writing, and this one authorises nothing
about reading.

A target that reaches further is not refused. It is put to the person who
started the turn, with the widening stated -- how many people outside this
conversation will see it, and where it is going -- and written only if they
agree. That question is asked by
:mod:`jiuwenswarm.agents.harness.common.rails.slack_write_confirmation_rail`,
before the tool runs, because a tool body has no way to ask anything. The
judgement it asks about is computed here, by
:meth:`SlackPostToolkit.widening_question`, so the rail and the tool cannot come
to disagree about what counts as widening. A turn with nobody to ask -- a
scheduled run -- refuses instead of proceeding.

**Where a ``chat_id`` comes from.** ``find_by_name`` turns a name somebody wrote
into an identifier: a person, a conversation, a user group or an emoji. It is
the only supported way to get one, and the cards say so. A ``chat_id`` naming a
*person* is honoured: the direct message is opened with ``conversations.open``
and posted into, which is the one thing this repository could not do before.

**Text and blocks are both offered, deliberately.** ``text`` takes anything a
model would write in an ordinary reply and goes through the same three passes --
the same Markdown-to-mrkdwn converter, the same splitter, the same Block Kit
renderer -- so a fenced ``mermaid``, ``vega-lite`` or ``blockkit`` behaves
identically inside a tool and outside it. ``blocks`` is the explicit form of the
same thing and goes through the *same* allow-list, the same interactive gate and
the same ``blocks.validate`` behaviour: it is a second spelling and never a
second route with weaker checks.

What ``blocks`` buys over the fence is the fallback. With ``blocks`` set,
``text`` becomes the notification preview -- the line in the popup, in the
sidebar, and to a screen reader -- and what is posted if Slack refuses the
blocks. Through a fence the fallback is whatever prose surrounded it, which is
frequently nothing worth reading.

**Four arguments are withheld and will stay withheld.** ``username``,
``icon_emoji``, ``icon_url`` and ``as_user`` set the displayed identity of a
message. Offered, they would let a model post as a named colleague, and a reader
has nothing on screen to tell that from the colleague writing. That is the one
hard no here. ``metadata`` is withheld because the connector stamps its own;
``parse``, ``link_names`` and ``mrkdwn`` because the renderer owns text
handling and a second opinion about it would contradict the first; and
``attachments`` because ``blocks`` supersedes it.

**Ephemeral is restricted to the requester in code rather than in a prompt.** An
ephemeral message cannot be edited, cannot be deleted, never enters the
conversation's history, and is invisible to everyone but its one reader --
including the person who asked for the turn. So an ephemeral sent to the wrong
person is not merely a mistake; it is a mistake nobody who would care can
observe, and no instruction can restore observability after the fact.
``visible_only_to_user_id`` must therefore equal the sender the request arrived
with. A turn with no sender -- a scheduled run -- has nothing to compare, so
ephemeral is simply unavailable there and needs no rule of its own.

**Editing and deleting carry no guard beyond the reach rule.** Slack refuses
either on a message this app did not post, which is the whole of the ownership
question and it is answered on Slack's side. Whether a *particular* message this
app posted should be changed or taken down is judgement, and judgement belongs
to the model. So nothing here tracks what the tool posted and there is no
exception branch: the turn card, the streaming bubble and a question's buttons
are all reachable, because the app posted them. That is a fact about the surface
rather than a hazard to guard, and the cards state it.

**Not offered to a subagent.** These act outward, and a subagent's output is
read by the agent that started it rather than by a person, so a subagent posting
into a conversation is a message nobody asked for arriving from a turn nobody in
that conversation can see. Nothing special is done to arrange this: the cards
are added to the main agent's ability manager, exactly as the six other Slack
tools' are, and a subagent is built with its own.

**Authorization is ``permissions.tools``.** Three names, three policies, and no
operator flag of this module's own -- a second switch beside the permission
engine would only be a way for the two to disagree. What ``channels.slack.write``
decides is reach, which is a different question from whether this turn may call
the tool at all.

Only cross-cutting primitives are borrowed from ``slack_history``: the reading of
a Slack response, the rule for when a refusal is worth retrying, the pass that
keeps a credential out of an error code, the failure type that names the refused
method as Slack's documentation spells it, and the per-request choice of
workspace. They are security- or correctness-critical and a fix to one copy
would not reach a second. No gate is shared, and no data path: this module reads
no conversation and returns no message content.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections.abc import Mapping
from typing import Any
from urllib.parse import quote

from openjiuwen.core.foundation.tool import LocalFunction, Tool, ToolCard

from jiuwenswarm.agents.harness.common.tools.slack_history import (
    _DEFAULT_RETRY_AFTER_SECONDS,
    SlackWorkspaceClients,
    _SlackCallFailure,
    _as_mapping,
    _response_cursor,
    _retry_after_seconds,
    _safe_error_code,
    shared_slack_workspaces,
)
from jiuwenswarm.common import slack_blocks
from jiuwenswarm.common.slack_history_policy import (
    METADATA_ASKER_KEY,
    METADATA_ORIGIN_KEY,
    METADATA_TEAM_KEY,
    ORIGIN_CRON_JOB,
    SlackWorkspaceUnresolved,
    slack_workspace_blocks,
)
from jiuwenswarm.common.slack_text import (
    MAX_SLACK_TEXT_LENGTH,
    MAX_SLACK_UPDATE_TEXT_LENGTH,
    normalize_slack_mrkdwn,
    split_text,
)
from jiuwenswarm.common.slack_write_policy import (
    METADATA_WRITE_POLICY_KEY,
    WRITE_DISABLED,
    WRITE_OPEN,
    WRITE_POLICY_CONFIRMS_WIDENING,
    WRITE_POLICY_NAMES_A_TARGET,
    WRITE_POLICY_VALUES,
    normalize_write_policy,
)


logger = logging.getLogger(__name__)

#: The three tool names, in the order the cards are built. Held as a tuple
#: because two other places read them: the registration that mounts them, and
#: the rail that asks about the two that can widen.
POST_MESSAGE = "post_message"
EDIT_MESSAGE = "edit_message"
DELETE_MESSAGE = "delete_message"
WRITE_TOOL_NAMES: tuple[str, ...] = (POST_MESSAGE, EDIT_MESSAGE, DELETE_MESSAGE)

#: The two that can widen an audience, and therefore the two the confirmation
#: rail watches. ``delete_message`` is deliberately absent: it removes a message
#: and widens nothing, so there is no audience question to put.
WIDENING_TOOL_NAMES: frozenset[str] = frozenset({POST_MESSAGE, EDIT_MESSAGE})

#: The channel id the cron scheduler delivers a scheduled job under. Spelled
#: here rather than imported from the gateway: a harness tool must not reach
#: into the gateway, which is why the policy module holds the other names this
#: predicate reads.
_CRON_REQUEST_CHANNEL_ID = "__cron__"

#: A Slack message ts, which is what a message id is on this platform: seconds
#: and microseconds, separated by a dot. Checked before the value travels into
#: an API argument, because it reaches these tools from a model that read it out
#: of somewhere else.
_MESSAGE_ID_RE = re.compile(r"\A\d{1,12}\.\d{1,6}\Z")

#: What a Slack id looks like at all. Deliberately loose about which letter
#: starts it -- Slack has minted C, D, G, U and W prefixes and is not obliged to
#: stop -- and strict about the alphabet, because the value is about to be sent
#: as an API argument and echoed into a result that is persisted.
_SLACK_ID_RE = re.compile(r"\A[A-Z][A-Z0-9]{1,32}\Z")

#: The prefixes that name a person rather than a conversation. ``W`` is the
#: Enterprise Grid form of ``U`` and names the same kind of thing.
_USER_ID_PREFIXES = ("U", "W")

#: Wall clock for one call of any of the three, retries and the membership check
#: included. A write is a small call somebody is waiting on, and the membership
#: read that may precede it is bounded by the same budget rather than added to
#: it.
_TIMEOUT_SECONDS = 60.0

#: How many times one Slack call will wait out a rate limit before giving up.
_MAX_RATE_LIMIT_RETRIES = 2

#: One page of ``conversations.members``, and the most pages one gate will read.
#: The product bounds what a membership check can spend on a single enormous
#: channel; past it the check has not failed, it has declined to finish, and a
#: check that did not finish is a refusal rather than a permission.
_MEMBERS_PAGE_LIMIT = 200
_MAX_MEMBER_PAGES = 25

#: How long one conversation's member list is believed, in seconds. The input to
#: a refusal, so a stale copy is wrong for at most this long.
_MEMBERS_CACHE_SECONDS = 60.0
_MAX_MEMBERS_CACHE_ENTRIES = 64

#: How many people a widening confirmation names before it stops naming them and
#: counts instead. The question has to be readable in a Slack message.
_MAX_NAMED_OUTSIDERS = 5

#: What a refusal says when Slack declined the call and the code alone is not
#: actionable.
_WRITE_FAILURE_DETAIL: Mapping[str, str] = {
    "channel_not_found": (
        "this workspace has no conversation with that id, or this app is not in"
        " it. A conversation id names a room in one Slack workspace and nothing"
        " at all in another, so an id from somewhere else names nothing here;"
        " find the conversation with find_by_name rather than reusing an id"
    ),
    "not_in_channel": (
        "this app is not a member of that conversation and Slack will not let it"
        " post there. Somebody in the conversation has to invite it; there is no"
        " wording that gets past this"
    ),
    "is_archived": (
        "that conversation is archived, so nothing can be posted into it until"
        " somebody unarchives it"
    ),
    "message_not_found": (
        "that conversation holds no message with that id. A message id is not"
        " transferable between conversations, so one taken from elsewhere names"
        " nothing here"
    ),
    "cant_update_message": (
        "Slack will not let this app change that message. Only a message this"
        " app posted can be edited, and an ephemeral message cannot be edited at"
        " all"
    ),
    "cant_delete_message": (
        "Slack will not let this app delete that message. Only a message this"
        " app posted can be deleted, and an ephemeral message cannot be deleted"
        " at all"
    ),
    "user_not_found": (
        "this workspace has no user with that id, so no direct message can be"
        " opened with them"
    ),
    "user_not_visible": (
        "this app cannot see that user, so no direct message can be opened with"
        " them"
    ),
    "msg_too_long": (
        "Slack refused the text as too long for the call that was made. A fresh"
        " post is split automatically; an edit is not, because Slack accepts"
        " only a tenth as much text in an edit and the rest has nowhere to go"
    ),
}

#: The scope a refused call names, by method. Slack answers ``missing_scope``
#: with the same word whichever call was declined, and the calls here are fixed
#: by different grants.
_SCOPE_FOR_METHOD: Mapping[str, str] = {
    "chat_postMessage": "chat:write",
    "chat_postEphemeral": "chat:write",
    "chat_update": "chat:write",
    "chat_delete": "chat:write",
    "conversations_open": "im:write, and mpim:write for a group direct message",
    "conversations_members": (
        "channels:read, groups:read, im:read or mpim:read, depending on what"
        " kind of conversation it is"
    ),
    "conversations_info": (
        "channels:read, groups:read, im:read or mpim:read, depending on what"
        " kind of conversation it is"
    ),
}


class _WriteRefused(RuntimeError):
    """This turn may not write where it asked to, and why.

    Separate from :class:`_SlackCallFailure` because the two mean opposite
    things to a caller. A call failure is Slack saying no to us and may be worth
    retrying; this is us saying no on the operator's behalf, and retrying it is
    the one thing that cannot help. ``detail`` holds the part somebody can act
    on and is kept out of ``code``, so the code stays a stable string a test and
    an operator's log filter can both key on.
    """

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


def slack_post_request_metadata(
    channel_id: str | None,
    metadata: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Return trusted metadata for a request that may post, or fail closed.

    Four conditions, and every one of them is about the request rather than
    about the arguments a model wrote.

    * The request is one of the two shapes a Slack conversation can be named
      by: an inbound Slack turn, where the connector stamps the conversation
      the message came from, or a scheduled run that the cron scheduler marked
      as its own. Without the marker a stray ``slack_channel_id`` left on some
      other request cannot reach these tools.
    * It names a conversation. There is nowhere to post otherwise, and there is
      deliberately nothing to fall back to.
    * It carries a settled ``channels.slack.write`` word. An absent word means
      no Slack connector settled this request, which mounts nothing rather than
      mounting something ungoverned.
    * That word is not ``disabled``.

    Unlike the pin toolkit next door, a policy word *is* a condition here, and
    the difference is not inconsistency. Pinning acts inside the conversation
    the request came from and can reach nowhere else, so the only question is
    whether the turn may act at all, which ``permissions.tools`` answers. These
    tools can name a conversation, so there is a second question -- how far --
    and ``channels.slack.write`` is the answer to it. ``disabled`` is the whole
    of what is read here; every wider distinction is taken at call time against
    Slack, where the memberships are.
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
    policy = normalize_write_policy(metadata.get(METADATA_WRITE_POLICY_KEY))
    if not policy or policy == WRITE_DISABLED:
        return {}
    return dict(metadata)


def _refusal_json(
    code: str,
    detail: str = "",
    *,
    chat_id: str = "",
    message_id: str = "",
) -> str:
    """One refusal, in a shape that never claims to know what it does not.

    No ``posted`` field, and no ``deleted`` one. A call Slack refused
    establishes nothing about the state of the conversation -- a message that
    could not be edited is not thereby unchanged in some knowable way -- and a
    ``false`` there would read as a claim about the conversation rather than
    about this call.
    """
    payload: dict[str, Any] = {"ok": False, "error": code}
    if detail:
        payload["detail"] = detail
    if chat_id:
        payload["chat_id"] = chat_id
    if message_id:
        payload["message_id"] = message_id
    logger.warning("slack write refused: %s%s", code, f" -- {detail}" if detail else "")
    return json.dumps(payload, ensure_ascii=False)


def _blockkit_settings(slack: Mapping[str, Any]) -> tuple[tuple[str, ...], bool, str]:
    """``(allowed block types, interactive allowed, table mode)`` from config.

    The connector's ``resolve_blockkit_allowed_block_types``,
    ``resolve_blockkit_allow_interactive`` and ``resolve_render_tables`` are the
    reference reading of these three keys, and a test pins this reading to
    theirs over a table of written values. They are not imported because
    importing the connector pulls in ``slack_bolt``; they are not lifted because
    each of them logs its own warning for the operator, and an operator's config
    is warned about once, by the process that loads it, rather than again on
    every tool call in another process.

    The interactive flag fails closed on anything it cannot read, which is the
    one of the three that is a safety control rather than a rendering choice.
    """
    raw_types = slack.get("blockkit_allowed_block_types")
    if isinstance(raw_types, str):
        entries: list[Any] = [raw_types]
    elif isinstance(raw_types, (list, tuple, set, frozenset)):
        entries = list(raw_types)
    else:
        entries = []
    allowed = tuple(
        name.strip() for name in entries if isinstance(name, str) and name.strip()
    )

    raw_interactive = slack.get("blockkit_allow_interactive")
    if isinstance(raw_interactive, bool):
        interactive = raw_interactive
    elif raw_interactive is None:
        interactive = slack_blocks.DEFAULT_ALLOW_INTERACTIVE_BLOCKS
    else:
        # Fails closed, unlike the two rendering choices beside it: an
        # unreadable value here would otherwise decide whether a model's message
        # may ask a reader to click something.
        interactive = str(raw_interactive).strip().lower() == "true"

    raw_tables = slack.get("render_tables")
    if isinstance(raw_tables, bool):
        # YAML 1.1 resolves a bare ``off`` to False long before anybody reads it
        # as a word, and reading that as a string would turn "render no tables"
        # into the default that renders every one of them.
        tables = (
            slack_blocks.RENDER_TABLES_OFF
            if raw_tables is False
            else slack_blocks.RENDER_TABLES_DEFAULT
        )
    else:
        word = str(raw_tables or "").strip().lower()
        tables = (
            word if word in slack_blocks.RENDER_TABLES_MODES
            else slack_blocks.RENDER_TABLES_DEFAULT
        )
    return allowed, interactive, tables


class SlackPostToolkit:
    """The three posting tools, scoped to the Slack request in flight."""

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
        self._workspace_team = ""
        self._workspace_count = 0
        self._allowed_block_types: tuple[str, ...] = ()
        self._allow_interactive = slack_blocks.DEFAULT_ALLOW_INTERACTIVE_BLOCKS
        self._render_tables = slack_blocks.RENDER_TABLES_DEFAULT
        # Keyed by workspace and conversation, for the reason the reading
        # toolkit's member cache is: a conversation id names a room in the
        # install whose token read it and nothing at all in another, so an entry
        # read under one token must never answer a request served by a
        # different one.
        self._members_cache: dict[tuple[str, str], tuple[float, frozenset[str]]] = {}
        # ``auth.test``'s answer, keyed by the token it was asked about. The
        # workspace url is the whole of what is wanted, and it is a fact about
        # the token rather than about any request holding it.
        self._workspace_url: dict[str, str] = {}
        # Direct messages already opened, keyed by workspace and user, so that a
        # turn writing to one person twice opens the conversation once.
        self._direct_messages: dict[tuple[str, str], str] = {}

    # ── request context ──────────────────────────────────────────────────

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

    async def _load_settings(self, metadata: Mapping[str, Any]) -> None:
        """Bind this request to the install it arrived from, and read the config.

        Takes the metadata the caller already read rather than reading it again.
        A provider is read once per request on purpose: it is live, and two
        reads of it are two requests as far as it is concerned.

        Read per call rather than captured at construction: this toolkit is
        built once and answers every request for the life of the process, a
        token rotated underneath it must take effect without a restart, and with
        several installs configured which token serves a call is a property of
        the request rather than of start-up.

        A write into the wrong workspace cannot be taken back by a later read,
        so an unresolvable install is refused rather than served from whichever
        block happens to hold a token.
        """
        slack = await self._workspaces.settings_for(
            str(metadata.get(METADATA_TEAM_KEY) or "").strip()
        )
        self._bot_token = str(slack.get("bot_token") or "").strip()
        self._workspace_team = (
            "" if self._client is not None
            else self._workspaces.team_of_token(self._bot_token)
        )
        # Read off the answer rather than by a second config read: the
        # selected mapping still carries the ``workspaces`` key it was selected
        # from, and ``slack_workspace_blocks`` is the one reading of it.
        self._workspace_count = len(slack_workspace_blocks(slack))
        (
            self._allowed_block_types,
            self._allow_interactive,
            self._render_tables,
        ) = _blockkit_settings(slack)

    def _get_client(self) -> Any:
        if self._client is not None:
            return self._client
        return self._workspaces.client_for(self._bot_token)

    def _workspace_key(self) -> str:
        """What the per-workspace caches are keyed by for the request in flight."""
        return self._workspace_team or self._bot_token

    async def _call(self, method: str, **kwargs: Any) -> dict[str, Any]:
        """Make one Slack call, reducing any refusal to a credential-free code.

        Both failure shapes are read. Slack answers a refused write with HTTP 200
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
                raise _SlackCallFailure("slack_call_timed_out", method)
            try:
                response = await asyncio.wait_for(
                    getattr(client, method)(**kwargs), timeout=remaining
                )
            except TimeoutError:
                raise _SlackCallFailure("slack_call_timed_out", method) from None
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

    # ── the policy word and the request's own facts ──────────────────────

    @staticmethod
    def _policy_word(metadata: Mapping[str, Any]) -> str:
        """The stamped word, or a refusal if it is not one of the four.

        The connector and the cron path both stamp a word the shared resolver
        already settled, so anything else here is a request whose metadata
        something other than those two built. Refused rather than defaulted:
        defaulting to the narrow word would hide a metadata path nobody meant to
        exist.

        An absent key is refused too, as a different thing: it means no Slack
        path settled this request at all. The registration gate already declines
        to mount the tools for such a request, so this is defence in depth.
        """
        if METADATA_WRITE_POLICY_KEY not in metadata:
            raise _WriteRefused(
                "write_policy_unsettled",
                "this request carries no Slack write policy, so no side that"
                " has the configuration has said whether anything may be"
                " posted",
            )
        raw = metadata.get(METADATA_WRITE_POLICY_KEY)
        word = str(raw or "").strip().lower()
        if word not in WRITE_POLICY_VALUES:
            raise _WriteRefused(
                "write_policy_value_unknown",
                f"{METADATA_WRITE_POLICY_KEY}={raw!r} is not one of"
                f" {', '.join(WRITE_POLICY_VALUES)}",
            )
        if word == WRITE_DISABLED:
            raise _WriteRefused(
                "write_policy_forbids_posting",
                "this deployment is configured to post nothing on a model's"
                f" instruction ({METADATA_WRITE_POLICY_KEY}: {WRITE_DISABLED})",
            )
        return word

    @staticmethod
    def _origin(metadata: Mapping[str, Any]) -> str:
        """The conversation this request arrived in, or a refusal.

        Settled from trusted request metadata and costs no API call, which is
        why it runs before anything is sent. It is also the whole of the default
        path: a call that names no target writes here and asks nothing.
        """
        origin = str(metadata.get("slack_channel_id") or "").strip()
        if not origin:
            # The same code the reading tools refuse an untrusted request with.
            # It is the same fact -- no Slack path settled which conversation
            # this request is about -- and one word for it means an operator
            # searching for it finds every place it can happen.
            raise _WriteRefused(
                "trusted_slack_channel_context_required",
                "this request carries no Slack conversation, so there is"
                " nowhere to post and nothing to fall back to",
            )
        return origin

    @staticmethod
    def _is_cron_run(metadata: Mapping[str, Any]) -> bool:
        """Whether this request was built by the cron scheduler.

        Read off the marker the scheduler stamps rather than inferred from a
        missing sender. "No sender, and that is expected" and "no sender, and
        something is wrong" are opposite answers, and inferring the first from
        the second would let any request lose its sender and gain the cron
        reading.
        """
        return str(metadata.get(METADATA_ORIGIN_KEY) or "") == ORIGIN_CRON_JOB

    def _requester(self, metadata: Mapping[str, Any]) -> str:
        """Who this turn is being run for, or ``""`` when there is nobody.

        The sender the connector stamped, which for an event-woken turn is the
        person whose action produced it. That reading is the connector's rather
        than this module's, and reusing it is deliberate: the read gate treats
        the same field as *who is asking*, and two notions of that in one
        codebase would eventually disagree about a turn that a reaction started.

        Empty for a scheduled run, and empty is a real answer here rather than a
        failure -- two things are unavailable without a requester, and each says
        so in its own words rather than through one shared refusal.
        """
        if self._is_cron_run(metadata):
            return ""
        return str(metadata.get(METADATA_ASKER_KEY) or "").strip()

    def _require_one_installation(self, metadata: Mapping[str, Any]) -> None:
        """Refuse a membership comparison whose two sides may be in different installs.

        The rule is a comparison of Slack **user ids**, and a user id belongs to
        one installation: the same person holds unrelated ids in two workspaces,
        and two unrelated people can hold ids that happen to look alike. A subset
        test whose sides were read in different installs answers a question
        nobody asked, and answers it in either direction.

        What kept this from arising was the single token. A list of tokens
        removes that guarantee, so the comparison is licensed only where the
        install the request was stamped in is the install the bound client is in
        -- the second read from Slack's own ``auth.test`` answer about the bound
        token, never from the request being checked. Silent where there is
        nothing to cross.
        """
        if self._workspace_count <= 1:
            return
        request_team = str(metadata.get(METADATA_TEAM_KEY) or "").strip()
        bound_team = self._workspace_team
        if request_team and bound_team and request_team == bound_team:
            return
        raise _WriteRefused(
            "write_cross_workspace_comparison",
            "this deployment serves more than one Slack workspace, and whether"
            " the members about to be compared are all in one of them was not"
            f" established (request: {request_team or 'unstamped'}, client:"
            f" {bound_team or 'unbound'}); a user id names one person in one"
            " workspace and somebody else or nobody in another, so the"
            " membership rule is refused rather than computed across the two",
        )

    # ── membership, and what widens ──────────────────────────────────────

    async def _conversation_members(self, chat_id: str) -> frozenset[str]:
        """Everybody in one conversation, paginated, cached for a short while.

        Keyed by conversation rather than by request, session or asker: the
        answer is a fact about Slack rather than about whoever wants it, so two
        requests asking about the same room are asking one question.

        A failure is never cached, and never partially cached. Half a member
        list is the one shape that could turn a question into silence -- a
        ``members(T)`` short by one person is a subset check that passes because
        the outsider was on the page that did not arrive -- so a page that fails
        takes the whole read with it and the caller refuses.

        Not shared with the reading toolkit's copy of this, which is wired to a
        scan budget and an API-call counter that belong to a scan of a
        conversation. A write makes two calls and has neither, and threading a
        scan's bookkeeping through it to save a page loop would be the larger
        coupling.
        """
        now = float(self._monotonic())
        key = (self._workspace_key(), chat_id)
        cached = self._members_cache.get(key)
        if cached is not None and now - cached[0] < _MEMBERS_CACHE_SECONDS:
            return cached[1]

        members: set[str] = set()
        cursor = ""
        for _page in range(_MAX_MEMBER_PAGES):
            kwargs: dict[str, Any] = {
                "channel": chat_id,
                "limit": _MEMBERS_PAGE_LIMIT,
            }
            if cursor:
                kwargs["cursor"] = cursor
            page = await self._call("conversations_members", **kwargs)
            items = page.get("members")
            for member in items if isinstance(items, list) else []:
                identifier = str(member or "").strip()
                if identifier:
                    members.add(identifier)
            cursor = _response_cursor(page)
            if page.get("has_more") and not cursor:
                # Slack says there is more and gives nothing to ask with. The
                # list is short by an unknown amount, which is the direction
                # that silently skips the question, so it is a refusal rather
                # than a partial answer.
                raise _WriteRefused(
                    "conversation_members_incomplete",
                    f"Slack reported more members of {chat_id} than it returned"
                    f" and supplied no cursor to fetch them, so who would see"
                    f" this message could not be established",
                )
            if not cursor:
                break
        else:
            raise _WriteRefused(
                "conversation_members_incomplete",
                f"{chat_id} has more members than one audience check reads"
                f" ({_MAX_MEMBER_PAGES * _MEMBERS_PAGE_LIMIT}), so who would see"
                f" this message could not be established",
            )

        settled = frozenset(members)
        if len(self._members_cache) >= _MAX_MEMBERS_CACHE_ENTRIES:
            oldest = min(
                self._members_cache, key=lambda entry: self._members_cache[entry][0]
            )
            self._members_cache.pop(oldest, None)
        self._members_cache[key] = (now, settled)
        return settled

    async def _outsiders(self, origin_id: str, target_id: str) -> "tuple[str, ...]":
        """Who is in the target and not in the conversation this turn came from.

        The inverted rule, computed. Empty means ``members(T) subset-of
        members(S)`` holds and the write reaches nobody new.

        ``history_exempt_members`` is deliberately not subtracted here. It is a
        *history* key, and its argument is about who in the source could already
        have read the target -- a statement about disclosure into the source,
        which is the opposite direction from this one. Reusing the list because
        it is the nearest one to hand would be reusing the words rather than the
        argument. The cost is that a workspace-wide integration sitting in the
        target and not in the source makes a write ask; the cost of the other
        choice is a write that reaches people an operator never exempted.
        """
        origin_members = await self._conversation_members(origin_id)
        target_members = await self._conversation_members(target_id)
        return tuple(sorted(target_members - origin_members))

    @staticmethod
    def _direct_message_sentence(user_id: str) -> str:
        """The question a direct message to somebody else is put as.

        Worded apart from the channel case because the widening is a different
        shape. A direct message reaches exactly one person and nobody can see it
        afterwards, not even the people in the conversation the request came
        from, so the count that makes the channel question answerable says
        nothing here and who it is says everything.
        """
        return (
            f"This message would be sent privately to <@{user_id}>, who is not"
            f" the person this turn is being run for. Nobody in this"
            f" conversation will see it. Send it?"
        )

    @staticmethod
    def _widening_sentence(
        target_id: str, outsiders: "tuple[str, ...]"
    ) -> str:
        """The question a person is asked, saying what is widening.

        "Post to #general?" is not answerable by somebody who does not already
        know what is at stake; "this will also be seen by four people who are
        not in this conversation" is. So the count leads, the destination
        follows, and the people are named while there are few enough of them to
        read -- a short list is what turns "four people" into a decision
        somebody can take in one glance.
        """
        count = len(outsiders)
        named = ", ".join(f"<@{who}>" for who in outsiders[:_MAX_NAMED_OUTSIDERS])
        if count > _MAX_NAMED_OUTSIDERS:
            named += f" and {count - _MAX_NAMED_OUTSIDERS} more"
        person = "person who is" if count == 1 else "people who are"
        return (
            f"This message would go to {target_id}, where it will also be seen"
            f" by {count} {person} not in this conversation: {named}."
            f" Post it there?"
        )

    async def widening_question(
        self,
        tool_name: str,
        arguments: Mapping[str, Any] | None,
    ) -> "str | None":
        """The confirmation this call needs, or ``None`` if it needs none.

        Called by the rail, before the tool runs, because a tool body cannot ask
        anything. The judgement lives here rather than in the rail so that the
        two cannot come to disagree about what counts as widening: the rail asks
        the question this returns and nothing else.

        ``None`` for everything that is not a question -- the word does not
        confirm, the target is this conversation, the target reaches nobody new,
        the tool is ``delete_message``, or something is wrong with the call. A
        call that is wrong is not confirmed and not refused here: it goes on to
        the tool, which refuses it in its own shape with its own code. Asking a
        person to approve a call that was never going to happen is worse than
        useless.
        """
        if tool_name not in WIDENING_TOOL_NAMES:
            return None
        if not isinstance(arguments, Mapping):
            return None
        requested = str(arguments.get("chat_id") or "").strip()
        if not requested:
            return None

        metadata = self._runtime_metadata()
        try:
            policy = self._policy_word(metadata)
            origin_id = self._origin(metadata)
        except _WriteRefused:
            return None
        if policy not in WRITE_POLICY_CONFIRMS_WIDENING:
            return None
        if requested == origin_id or not _SLACK_ID_RE.match(requested):
            return None
        if requested.startswith(_USER_ID_PREFIXES):
            # A direct message with one person. Opening it is what settles the
            # conversation id, and the audience is that person, so the question
            # is asked about them rather than about a conversation that may not
            # exist yet.
            if requested == self._requester(metadata):
                return None
            if not self._requester(metadata):
                # Nobody to ask. The tool refuses this; refusing is not a
                # question, so nothing is returned here.
                return None
            return self._direct_message_sentence(requested)
        if not self._requester(metadata):
            # Nobody to ask. The tool refuses this, and refusing is not a
            # question, so nothing is returned here.
            return None

        try:
            await self._load_settings(metadata)
            self._require_one_installation(metadata)
            outsiders = await self._outsiders(origin_id, requested)
        except (_WriteRefused, SlackWorkspaceUnresolved, _SlackCallFailure):
            # The tool re-runs every one of these and reports the refusal in its
            # own shape. Answering "no question" here lets it do that, rather
            # than putting a question to somebody about a call that is going to
            # fail whatever they say.
            return None
        if not outsiders:
            return None
        return self._widening_sentence(requested, outsiders)

    async def _resolve_target(
        self,
        metadata: Mapping[str, Any],
        chat_id: Any,
        policy: str,
        origin_id: str,
        *,
        may_widen: bool,
    ) -> str:
        """The conversation to write into, or a refusal.

        ``may_widen`` is false for ``delete_message``. Under ``members`` a
        delete into a wider target proceeds without a question, because a delete
        removes a message and widens nothing; the reach words ``disabled`` and
        ``origin`` still bound it, because they say where this bot acts at all
        rather than who ends up reading something.

        A ``U…`` or ``W…`` id names a person rather than a conversation, and the
        direct message with them is opened here. Slack answers with the existing
        conversation where there is one, so this creates nothing that was not
        going to exist the moment the message was posted.
        """
        requested = str(chat_id or "").strip()
        if not requested:
            return origin_id
        if not _SLACK_ID_RE.match(requested):
            raise _WriteRefused(
                "chat_id_malformed",
                "a Slack chat_id is an identifier such as C0123456789 or"
                " U0123456789, taken unchanged from find_by_name or from an"
                " earlier result; this is not one",
            )
        if requested == origin_id:
            return origin_id
        if policy not in WRITE_POLICY_NAMES_A_TARGET:
            raise _WriteRefused(
                "write_policy_forbids_other_conversations",
                f"this turn may write into the conversation it came from and"
                f" nowhere else; naming {requested} needs a wider setting than"
                f" {policy}",
            )

        if not may_widen:
            # ``delete_message``. Under a word that lets a target be named, a
            # delete needs no audience check at all: it shows the message to
            # nobody, so there is nothing to widen and nothing to ask about.
            # The reach words above still bound which conversations it reaches.
            if requested.startswith(_USER_ID_PREFIXES):
                return await self._open_direct_message(requested)
            return requested

        if requested.startswith(_USER_ID_PREFIXES):
            opened = await self._open_direct_message(requested)
            if policy == WRITE_OPEN or opened == origin_id:
                return opened
            if requested == self._requester(metadata):
                # A direct message with the person this turn is being run for
                # reaches nobody the turn did not already reach.
                return opened
            if not self._requester(metadata):
                raise _WriteRefused(
                    "write_target_widens_audience",
                    f"a direct message with {requested} would be read by"
                    f" somebody this turn is not being run for, and this turn"
                    f" has nobody to put that to -- a scheduled run has no"
                    f" requester. Post into the conversation the run delivers"
                    f" into, or set channels.slack.write: {WRITE_OPEN} for"
                    f" this deployment",
                )
            # Widening, with somebody to ask. The rail asked before this ran.
            return opened

        if policy == WRITE_OPEN:
            return requested

        self._require_one_installation(metadata)
        outsiders = await self._outsiders(origin_id, requested)
        if not outsiders:
            return requested
        if not self._requester(metadata):
            raise _WriteRefused(
                "write_target_widens_audience",
                f"{len(outsiders)} member(s) of {requested} are not in"
                f" {origin_id}, so posting there would show this message to"
                f" people who cannot read the conversation it came from. That"
                f" is a question for the person who asked, and this turn has"
                f" nobody to ask -- a scheduled run has no requester",
            )
        return requested

    async def _open_direct_message(self, user_id: str) -> str:
        """The direct message with one person, opened if it does not exist.

        ``conversations.open`` answers with the existing ``D…`` where there is
        one and creates it where there is not, so this is safe to call every
        time and is cached for the life of the toolkit per workspace: which
        conversation two accounts share does not change.
        """
        key = (self._workspace_key(), user_id)
        known = self._direct_messages.get(key)
        if known:
            return known
        answer = await self._call("conversations_open", users=user_id)
        channel = answer.get("channel")
        opened = ""
        if isinstance(channel, Mapping):
            opened = str(channel.get("id") or "").strip()
        if not opened:
            raise _WriteRefused(
                "direct_message_unavailable",
                f"Slack opened no conversation with {user_id} and returned no"
                f" id, so there is nowhere to post",
            )
        self._direct_messages[key] = opened
        return opened

    # ── rendering ────────────────────────────────────────────────────────

    def _render(self, text: str) -> "list[tuple[str, list[dict[str, Any]] | None]]":
        """One piece of text as the chunks and blocks an ordinary reply becomes.

        The same three passes, in the same order: convert the narrow Markdown
        subset to mrkdwn, split into pieces Slack will store, then offer each
        piece to the Block Kit renderer. Rendering per chunk rather than per
        message is not a detail -- a chunk is what a Slack message holds, and
        the limits blocks must fit are per message.
        """
        normalised = normalize_slack_mrkdwn(text)
        rendered: list[tuple[str, list[dict[str, Any]] | None]] = []
        for chunk in split_text(normalised):
            blocks = slack_blocks.render_blocks(
                chunk,
                render_tables=self._render_tables,
                allowed_block_types=self._allowed_block_types,
                allow_interactive=self._allow_interactive,
            )
            rendered.append((chunk, blocks))
        return rendered

    def _declared_blocks(self, blocks: Any) -> "list[dict[str, Any]] | None":
        """A declared Block Kit payload, through the checks a fence goes through.

        ``None`` for a payload the checks refuse, which the caller reports and
        falls back from. The checks are ``slack_blocks.check_blocks`` and
        nothing else: the fence and this argument are two spellings of one
        thing, and a second reading of either check would eventually admit
        through one what the other refuses.
        """
        if blocks is None:
            return None
        return slack_blocks.check_blocks(
            blocks,
            allowed_block_types=self._allowed_block_types,
            allow_interactive=self._allow_interactive,
        )

    async def _permalink(self, chat_id: str, message_id: str) -> str:
        """A link to one message, built rather than asked for.

        ``auth.test`` already answers the workspace url and needs no scope, and
        it is asked once per token. ``chat.getPermalink`` would be a second
        method for a string that can be assembled, and every method this
        repository calls has to be classified in the scope policy -- a grant an
        operator is asked for is worth more than a saved line of string work.
        """
        known = self._workspace_url.get(self._bot_token)
        if known is None:
            try:
                answer = await self._call("auth_test")
            except _SlackCallFailure:
                # A permalink is a convenience on a message that has already
                # been posted. Losing it must not turn a delivered message into
                # a reported failure.
                return ""
            known = str(answer.get("url") or "").strip()
            self._workspace_url[self._bot_token] = known
        if not known or not message_id:
            return ""
        return (
            f"{known.rstrip('/')}/archives/{quote(chat_id)}"
            f"/p{quote(message_id.replace('.', ''))}"
        )

    def _slack_refusal(
        self, exc: _SlackCallFailure, *, chat_id: str, message_id: str = ""
    ) -> str:
        """One Slack refusal as the shape every failure here returns.

        A missing scope names the scope, because the code alone is not
        actionable: ``missing_scope`` reads the same whether ``chat.postMessage``
        or ``conversations.open`` was declined, and those are different grants to
        go and ask for.
        """
        detail = _WRITE_FAILURE_DETAIL.get(exc.code, "")
        if not detail:
            detail = f"Slack refused {exc.where}"
        if exc.code == "missing_scope":
            scope = _SCOPE_FOR_METHOD.get(exc.method, "")
            detail = f"Slack refused {exc.where}"
            if scope:
                detail += (
                    f"; it needs {scope}, which this installation does not"
                    f" hold. Adding a scope means reinstalling the Slack app"
                )
        return _refusal_json(
            exc.code, detail, chat_id=chat_id, message_id=message_id
        )

    # ── the three tools ──────────────────────────────────────────────────

    async def post_message(
        self,
        chat_id: str | None = None,
        text: str | None = None,
        blocks: Any = None,
        reply_to_message_id: str | None = None,
        also_post_to_chat: bool = False,
        visible_only_to_user_id: str | None = None,
        unfurl_links: bool | None = None,
        unfurl_media: bool | None = None,
    ) -> str:
        """Post one message, and say where it landed."""
        metadata = self._runtime_metadata()
        try:
            policy = self._policy_word(metadata)
            origin_id = self._origin(metadata)
        except _WriteRefused as refused:
            return _refusal_json(refused.code, refused.detail)

        body = str(text or "").strip()
        declared = self._declared_blocks(blocks) if blocks is not None else None
        if blocks is not None and declared is None:
            # Not silently dropped. A model that wrote blocks asked for a
            # rendering, and posting the fallback while saying nothing would
            # leave it believing the rendering happened.
            if not body:
                return _refusal_json(
                    "blocks_refused",
                    "the blocks were refused by this deployment's Block Kit"
                    " rules -- a block type that is not on"
                    " channels.slack.blockkit_allowed_block_types, or an"
                    " element a reader could click where"
                    " channels.slack.blockkit_allow_interactive is off -- and"
                    " no text was given to post in their place",
                    chat_id=origin_id,
                )
            logger.warning(
                "slack post: the declared blocks were refused by this"
                " deployment's Block Kit rules; posting the text instead"
            )
        if not body and declared is None:
            return _refusal_json(
                "nothing_to_post",
                "neither text nor blocks was given, and an empty message is not"
                " something Slack will accept",
                chat_id=origin_id,
            )

        thread_ts = str(reply_to_message_id or "").strip()
        if thread_ts and not _MESSAGE_ID_RE.match(thread_ts):
            return _refusal_json(
                "message_id_malformed",
                "a Slack message id is seconds and microseconds separated by a"
                " dot, such as 1758123456.123456; reply_to_message_id is not"
                " one",
                chat_id=origin_id,
            )

        ephemeral_to = str(visible_only_to_user_id or "").strip()
        if ephemeral_to:
            requester = self._requester(metadata)
            if not requester:
                return _refusal_json(
                    "ephemeral_requires_a_requester",
                    "an ephemeral message can only be sent to the person whose"
                    " message started this turn, and this turn was not started"
                    " by anybody -- a scheduled run has no requester. Post an"
                    " ordinary message instead",
                    chat_id=origin_id,
                )
            if ephemeral_to != requester:
                return _refusal_json(
                    "ephemeral_requires_the_requester",
                    "an ephemeral message can only be sent to the person whose"
                    " message started this turn. It cannot be edited, cannot be"
                    " deleted, never enters the conversation's history, and is"
                    " invisible to everybody else -- including whoever asked --"
                    " so one sent to the wrong person cannot be noticed or"
                    " undone. Post an ordinary message, or address this person"
                    " in it",
                    chat_id=origin_id,
                )

        try:
            await self._load_settings(metadata)
        except SlackWorkspaceUnresolved as unresolved:
            return _refusal_json(
                unresolved.code, unresolved.detail, chat_id=origin_id
            )

        try:
            target_id = await self._resolve_target(
                metadata, chat_id, policy, origin_id, may_widen=True
            )
        except _WriteRefused as refused:
            return _refusal_json(refused.code, refused.detail, chat_id=origin_id)
        except _SlackCallFailure as exc:
            return self._slack_refusal(exc, chat_id=origin_id)

        if ephemeral_to and target_id != origin_id:
            return _refusal_json(
                "ephemeral_leaves_this_conversation",
                "an ephemeral message is shown to one person inside one"
                " conversation and is the only kind of message nobody can go"
                " back and check, so it is confined to the conversation this"
                " turn came from",
                chat_id=origin_id,
            )

        # With blocks declared, ``text`` is the notification preview and the
        # fallback rather than a second copy on screen, so it is sent whole and
        # never split: a fallback cut in half is a fallback that fails at
        # exactly the moment it is needed.
        pieces: list[tuple[str, list[dict[str, Any]] | None]]
        if declared is not None:
            pieces = [(body[:MAX_SLACK_TEXT_LENGTH], declared)]
        else:
            pieces = self._render(body)

        posted: list[str] = []
        for index, (chunk, chunk_blocks) in enumerate(pieces):
            kwargs: dict[str, Any] = {"channel": target_id, "text": chunk}
            if chunk_blocks:
                kwargs["blocks"] = chunk_blocks
            if ephemeral_to:
                kwargs["user"] = ephemeral_to
            if thread_ts:
                kwargs["thread_ts"] = thread_ts
                if also_post_to_chat and index == 0:
                    # Slack shows a broadcast reply in the conversation as well
                    # as in the thread. Only the first piece is broadcast: a
                    # long answer broadcast piece by piece would fill the
                    # conversation with the thread it was meant to stay in.
                    kwargs["reply_broadcast"] = True
            if unfurl_links is not None:
                kwargs["unfurl_links"] = bool(unfurl_links)
            if unfurl_media is not None:
                kwargs["unfurl_media"] = bool(unfurl_media)

            method = "chat_postEphemeral" if ephemeral_to else "chat_postMessage"
            try:
                answer = await self._call(method, **kwargs)
            except _SlackCallFailure as exc:
                if posted:
                    # Part of the message is already in the conversation and
                    # cannot be recalled. Reporting a bare failure would leave
                    # the caller believing nothing was sent and posting it
                    # again; the refusal therefore says what did land.
                    return _refusal_json(
                        exc.code,
                        f"{len(posted)} of {len(pieces)} part(s) were posted"
                        f" before Slack refused {exc.where}; the message is in"
                        f" {target_id} and is incomplete. Do not post the whole"
                        f" message again",
                        chat_id=target_id,
                        message_id=posted[0],
                    )
                return self._slack_refusal(exc, chat_id=target_id)
            # ``chat.postEphemeral`` answers ``message_ts`` where the other two
            # answer ``ts``. Both are read, because an ephemeral has a position
            # in the conversation even though nothing can address it.
            posted.append(
                str(answer.get("ts") or answer.get("message_ts") or "").strip()
            )
            if not thread_ts and index == 0 and len(pieces) > 1:
                # Later parts hang under the first, so a long answer is one
                # message with a thread rather than several loose ones.
                thread_ts = posted[0]

        result: dict[str, Any] = {
            "ok": True,
            "chat_id": target_id,
            "message_id": posted[0],
        }
        if ephemeral_to:
            result["visible_only_to_user_id"] = ephemeral_to
            # No permalink, and the absence is the fact: an ephemeral message
            # has no archive url because it is in nobody's archive.
            result["ephemeral"] = True
        else:
            permalink = await self._permalink(target_id, posted[0])
            if permalink:
                result["permalink"] = permalink
        if len(posted) > 1:
            result["parts"] = len(posted)
            result["part_message_ids"] = posted
        logger.info(
            "slack post: %d part(s) into %s%s",
            len(posted),
            target_id,
            " (ephemeral)" if ephemeral_to else "",
        )
        return json.dumps(result, ensure_ascii=False)

    async def edit_message(
        self,
        message_id: str,
        chat_id: str | None = None,
        text: str | None = None,
        blocks: Any = None,
        also_post_to_chat: bool = False,
    ) -> str:
        """Rewrite one message this app posted."""
        metadata = self._runtime_metadata()
        try:
            policy = self._policy_word(metadata)
            origin_id = self._origin(metadata)
        except _WriteRefused as refused:
            return _refusal_json(refused.code, refused.detail)

        raw_id = str(message_id or "").strip()
        if not raw_id:
            return _refusal_json(
                "message_id_required",
                "no message id was given; a message id identifies one message"
                " and is taken unchanged from a result that reported it",
                chat_id=origin_id,
            )
        if not _MESSAGE_ID_RE.match(raw_id):
            return _refusal_json(
                "message_id_malformed",
                "a Slack message id is seconds and microseconds separated by a"
                " dot, such as 1758123456.123456; this is not one",
                chat_id=origin_id,
                message_id=raw_id[:32],
            )

        body = str(text or "").strip()
        declared = self._declared_blocks(blocks) if blocks is not None else None
        if blocks is not None and declared is None and not body:
            return _refusal_json(
                "blocks_refused",
                "the blocks were refused by this deployment's Block Kit rules"
                " and no text was given to put in their place, so there is"
                " nothing to rewrite the message to",
                chat_id=origin_id,
                message_id=raw_id,
            )
        if not body and declared is None:
            return _refusal_json(
                "nothing_to_post",
                "neither text nor blocks was given, so there is nothing to"
                " rewrite the message to. To take a message down, use"
                " delete_message",
                chat_id=origin_id,
                message_id=raw_id,
            )

        try:
            await self._load_settings(metadata)
        except SlackWorkspaceUnresolved as unresolved:
            return _refusal_json(
                unresolved.code, unresolved.detail, chat_id=origin_id
            )
        try:
            target_id = await self._resolve_target(
                metadata, chat_id, policy, origin_id, may_widen=True
            )
        except _WriteRefused as refused:
            return _refusal_json(
                refused.code, refused.detail, chat_id=origin_id, message_id=raw_id
            )
        except _SlackCallFailure as exc:
            return self._slack_refusal(exc, chat_id=origin_id, message_id=raw_id)

        if declared is not None:
            chunk, chunk_blocks = body[:MAX_SLACK_UPDATE_TEXT_LENGTH], declared
        else:
            # An edit is not split. Slack accepts a tenth as much text in
            # chat.update as in a fresh post and refuses the whole call past it,
            # and the overflow has nowhere to go: the second half of an edit is
            # not a second message, it is a message that was never asked for.
            pieces = self._render(body)
            chunk, chunk_blocks = pieces[0]
            if len(pieces) > 1 or len(chunk) > MAX_SLACK_UPDATE_TEXT_LENGTH:
                return _refusal_json(
                    "edit_text_too_long",
                    f"Slack accepts at most {MAX_SLACK_UPDATE_TEXT_LENGTH}"
                    f" characters when a message is edited, a tenth of what it"
                    f" accepts when one is posted, and this is longer. Shorten"
                    f" the replacement, or post the rest as a new message",
                    chat_id=target_id,
                    message_id=raw_id,
                )

        kwargs: dict[str, Any] = {
            "channel": target_id,
            "ts": raw_id,
            "text": chunk,
        }
        if chunk_blocks:
            kwargs["blocks"] = chunk_blocks
        elif blocks is not None:
            # An empty list is how chat.update takes the blocks off a message
            # that has them. Omitting the field would leave whatever is there,
            # including the buttons of an answered question.
            kwargs["blocks"] = []
        if also_post_to_chat:
            kwargs["reply_broadcast"] = True
        try:
            answer = await self._call("chat_update", **kwargs)
        except _SlackCallFailure as exc:
            return self._slack_refusal(exc, chat_id=target_id, message_id=raw_id)

        settled = str(answer.get("ts") or raw_id).strip()
        result: dict[str, Any] = {
            "ok": True,
            "chat_id": target_id,
            "message_id": settled,
        }
        permalink = await self._permalink(target_id, settled)
        if permalink:
            result["permalink"] = permalink
        logger.info("slack edit: %s in %s", settled, target_id)
        return json.dumps(result, ensure_ascii=False)

    async def delete_message(
        self,
        message_id: str,
        chat_id: str | None = None,
    ) -> str:
        """Take one message this app posted out of the conversation."""
        metadata = self._runtime_metadata()
        try:
            policy = self._policy_word(metadata)
            origin_id = self._origin(metadata)
        except _WriteRefused as refused:
            return _refusal_json(refused.code, refused.detail)

        raw_id = str(message_id or "").strip()
        if not raw_id:
            return _refusal_json(
                "message_id_required",
                "no message id was given; a message id identifies one message"
                " and is taken unchanged from a result that reported it",
                chat_id=origin_id,
            )
        if not _MESSAGE_ID_RE.match(raw_id):
            return _refusal_json(
                "message_id_malformed",
                "a Slack message id is seconds and microseconds separated by a"
                " dot, such as 1758123456.123456; this is not one",
                chat_id=origin_id,
                message_id=raw_id[:32],
            )

        try:
            await self._load_settings(metadata)
        except SlackWorkspaceUnresolved as unresolved:
            return _refusal_json(
                unresolved.code, unresolved.detail, chat_id=origin_id
            )
        try:
            # ``may_widen`` is false, and that is the whole of what makes delete
            # different: a delete into a conversation whose members are not all
            # here proceeds without a question, because taking a message down
            # shows it to nobody. The reach words still bound it.
            target_id = await self._resolve_target(
                metadata, chat_id, policy, origin_id, may_widen=False
            )
        except _WriteRefused as refused:
            return _refusal_json(
                refused.code, refused.detail, chat_id=origin_id, message_id=raw_id
            )
        except _SlackCallFailure as exc:
            return self._slack_refusal(exc, chat_id=origin_id, message_id=raw_id)

        try:
            await self._call("chat_delete", channel=target_id, ts=raw_id)
        except _SlackCallFailure as exc:
            return self._slack_refusal(exc, chat_id=target_id, message_id=raw_id)

        logger.info("slack delete: %s in %s", raw_id, target_id)
        return json.dumps(
            {"ok": True, "chat_id": target_id, "message_id": raw_id, "deleted": True},
            ensure_ascii=False,
        )

    # ── registration ─────────────────────────────────────────────────────

    def names_a_target(self) -> bool:
        """Whether this deployment's settled word lets a target be named.

        Read off the policy already stamped on the request rather than out of
        config: this file must not read the connector's settings, and the word
        is on the request precisely so that it does not have to.

        It decides the shape of the three cards, which is settled once at
        registration rather than per request, so a deployment that widens the
        word needs a restart before ``chat_id`` appears. The per-request half --
        which conversation, under which rule -- is read at call time. The
        argument grants nothing, the gate being what decides, so a card offering
        one to a request that will be refused costs a line of schema and no
        reach.
        """
        try:
            metadata = self._runtime_metadata()
        except Exception:  # noqa: BLE001 - a write gate must fail closed.
            return False
        word = str(metadata.get(METADATA_WRITE_POLICY_KEY) or "").strip().lower()
        return word in WRITE_POLICY_NAMES_A_TARGET

    def confirms_widening(self) -> bool:
        """Whether this deployment's settled word asks before a write widens."""
        try:
            metadata = self._runtime_metadata()
        except Exception:  # noqa: BLE001 - a write gate must fail closed.
            return False
        word = str(metadata.get(METADATA_WRITE_POLICY_KEY) or "").strip().lower()
        return word in WRITE_POLICY_CONFIRMS_WIDENING

    def get_tools(self) -> list[Tool]:
        """The three posting tools, shaped by the settled policy word."""
        names_a_target = self.names_a_target()
        return [
            LocalFunction(
                card=_post_card(names_a_target), func=self.post_message
            ),
            LocalFunction(
                card=_edit_card(names_a_target), func=self.edit_message
            ),
            LocalFunction(
                card=_delete_card(names_a_target), func=self.delete_message
            ),
        ]


# ── the cards ────────────────────────────────────────────────────────────


_CHAT_ID_PARAM = {
    "type": "string",
    "description": (
        "The conversation to act in, as a Slack identifier such as "
        "C0123456789 for a channel or U0123456789 for a person. Omit it to "
        "act in the conversation this request came from, which is what is "
        "wanted almost every time. Get an identifier from find_by_name, "
        "which turns a name somebody wrote into the thing it names, or take "
        "one unchanged from an earlier result. Never build one and never "
        "guess one: an identifier that names nothing here fails, and one "
        "that names the wrong conversation does not."
    ),
}

_ELSEWHERE = (
    "\nWhere it goes. Pass chat_id to act in another conversation, or "
    "leave it out to act in this one. A conversation everybody in can "
    "already read this one is written to without further ado. A "
    "conversation that reaches anybody else is put to the person who "
    "asked, saying how many people outside this conversation would see "
    "it, and goes ahead only if they agree -- so naming a wider "
    "conversation is a thing to do on purpose and to expect a question "
    "about. A turn nobody started, such as a scheduled run, has nobody "
    "to ask and is refused instead."
    "\nA chat_id that names a person opens the direct message with them "
    "and posts there. That is the way to write to somebody privately; "
    "there is no other."
)

_NOT_ELSEWHERE = (
    "\nWhere it goes. It acts in the conversation this request came from, "
    "and there is no way to name another one."
)


def _post_card(names_a_target: bool) -> ToolCard:
    """The card for ``post_message``."""
    properties: dict[str, Any] = {
        "text": {
            "type": "string",
            "description": (
                "What to say, written exactly as it would be written in an "
                "ordinary reply. Markdown headings, bold, bullets and links "
                "all work, and so do fenced mermaid, vega-lite and blockkit "
                "blocks: the same renderer handles this text as handles a "
                "reply, so nothing behaves differently for being sent through "
                "a tool. Text longer than one Slack message is split "
                "automatically, and the parts after the first are threaded "
                "under it."
                "\nWith blocks set, this is not shown in the conversation. It "
                "becomes the notification popup, the line in the sidebar, "
                "what a screen reader announces, and what is posted if Slack "
                "refuses the blocks. Write one sentence somebody could act on "
                "having read nothing else -- not a copy of the whole message, "
                "and not a label such as \"a chart\"."
            ),
        },
        "blocks": {
            "type": "array",
            "items": {"type": "object"},
            "description": (
                "Slack Block Kit, written out, for a message whose structure "
                "matters. The same thing a ```blockkit fence in text produces "
                "and checked the same way, so nothing is admitted here that a "
                "fence would be refused. Use it when the fallback line matters "
                "-- see text -- and use a fence otherwise. This deployment may "
                "narrow which block types are allowed and usually forbids "
                "anything a reader can click; a refused payload is reported "
                "and the text is posted instead."
            ),
        },
        "reply_to_message_id": {
            "type": "string",
            "description": (
                "Post as a reply in that message's thread rather than into the "
                "conversation. Pass the message identifier of the message being "
                "replied to, taken unchanged from a result that reported one."
            ),
        },
        "also_post_to_chat": {
            "type": "boolean",
            "default": False,
            "description": (
                "With reply_to_message_id, show the reply in the conversation "
                "as well as in the thread. Read only with it. Use it for a "
                "reply the people who are not following the thread still need "
                "to see -- a conclusion, a decision -- and leave it off for "
                "everything else, because it puts the reply in front of the "
                "whole conversation."
            ),
        },
        "visible_only_to_user_id": {
            "type": "string",
            "description": (
                "Show this message to one person only. It can only be the "
                "person whose message started this turn; sending one to "
                "anybody else is refused. That is not a formality: such a "
                "message cannot be edited, cannot be deleted, never enters the "
                "conversation's history, and is invisible to everybody else, "
                "so one sent to the wrong person can be neither noticed nor "
                "undone. Use it for something the asker alone needs -- a "
                "confirmation, a note about how something was done -- and post "
                "an ordinary message otherwise."
            ),
        },
        "unfurl_links": {
            "type": "boolean",
            "description": (
                "Whether Slack expands links in the message into previews. "
                "Leave it out to let Slack decide as it does for any other "
                "message. Set it false for a message full of links, where the "
                "previews would bury the text."
            ),
        },
        "unfurl_media": {
            "type": "boolean",
            "description": (
                "Whether Slack expands image and video links into previews. "
                "Leave it out unless the previews would get in the way."
            ),
        },
    }
    if names_a_target:
        properties = {"chat_id": _CHAT_ID_PARAM, **properties}

    return ToolCard(
        name=POST_MESSAGE,
        description=(
            "Post a message into Slack. Use it to say something somewhere "
            "other than as the answer to the message being handled -- to "
            "another conversation, to one person, in a thread, or as a second "
            "message here. The ordinary answer to a request does not need this "
            "tool: replying is what happens by default, and posting the answer "
            "through here as well delivers it twice."
            + (_ELSEWHERE if names_a_target else _NOT_ELSEWHERE)
            + "\nWhat to send. Give text for anything that reads as writing. "
            "Give blocks as well when a one-line notification summary is worth "
            "writing by hand, because with blocks set the text becomes that "
            "summary instead of appearing in the conversation."
            "\nThe message is posted as this app, under its own name and "
            "picture, and there is no argument that changes either. A message "
            "that should read as coming from somebody is a message that says "
            "so in its own words."
            "\nWhat comes back. The conversation, the identifier of the "
            "message, and a link to it. Keep the identifier: it is what "
            "edit_message and delete_message take, and what "
            "reply_to_message_id takes to start a thread under it. A message "
            "long enough to be split also reports how many parts it became."
        ),
        input_params={"type": "object", "properties": properties, "required": []},
    )


def _edit_card(names_a_target: bool) -> ToolCard:
    """The card for ``edit_message``."""
    properties: dict[str, Any] = {
        "message_id": {
            "type": "string",
            "description": (
                "The message to rewrite, named by its Slack identifier, such "
                "as 1758123456.123456. Copy it verbatim from a result that "
                "reported it. It names a message in one conversation; an "
                "identifier taken from another conversation names nothing."
            ),
        },
        "text": {
            "type": "string",
            "description": (
                "What the message should say now. It replaces the whole "
                "message rather than being added to it. Slack accepts far less "
                "text in an edit than in a new message -- about four thousand "
                "characters -- and a longer replacement is refused rather than "
                "cut, because the rest of it has nowhere to go."
                "\nWith blocks set, this is the notification preview and the "
                "fallback, exactly as it is for post_message."
            ),
        },
        "blocks": {
            "type": "array",
            "items": {"type": "object"},
            "description": (
                "Slack Block Kit for the rewritten message, checked the same "
                "way post_message checks it. Passing an empty array removes "
                "the structure a message already has and leaves the text."
            ),
        },
        "also_post_to_chat": {
            "type": "boolean",
            "default": False,
            "description": (
                "If the message is a thread reply, also show it in the "
                "conversation. Read only for a message that is in a thread."
            ),
        },
    }
    if names_a_target:
        properties = {"chat_id": _CHAT_ID_PARAM, **properties}

    return ToolCard(
        name=EDIT_MESSAGE,
        description=(
            "Rewrite a message this app posted. Use it to correct something "
            "already said, to fill in a message posted as a placeholder, or to "
            "remove the buttons from a question that has been answered."
            "\nWhat can be reached. Any message this app posted, which "
            "includes the messages it posts on its own -- the card that tracks "
            "this turn, the bubble a streamed answer is written into, a "
            "question's buttons. That is a fact about this surface rather than "
            "a licence: rewriting the turn's own card mid-turn will confuse "
            "whoever is reading it, and the card is rewritten again by the "
            "thing that owns it. Edit what this tool posted, and leave the "
            "rest alone unless there is a reason to do otherwise."
            "\nSlack refuses an edit to any message this app did not post, and "
            "to an ephemeral message, which cannot be changed at all."
            + (_ELSEWHERE if names_a_target else _NOT_ELSEWHERE)
            + "\nAn edit replaces the message. Whatever is not passed is gone, "
            "so send the whole new text rather than the part that changed."
            "\nWhat comes back. The conversation, the message identifier, and "
            "a link to it."
        ),
        input_params={
            "type": "object",
            "properties": properties,
            "required": ["message_id"],
        },
    )


def _delete_card(names_a_target: bool) -> ToolCard:
    """The card for ``delete_message``."""
    properties: dict[str, Any] = {
        "message_id": {
            "type": "string",
            "description": (
                "The message to take down, named by its Slack identifier, such "
                "as 1758123456.123456. Copy it verbatim from a result that "
                "reported it."
            ),
        },
    }
    if names_a_target:
        properties = {"chat_id": _CHAT_ID_PARAM, **properties}

    return ToolCard(
        name=DELETE_MESSAGE,
        description=(
            "Delete a message this app posted. Use it for something posted by "
            "mistake, or for a message whose purpose has passed -- a progress "
            "note nobody needs once the work is done."
            "\nIt cannot be undone and there is no confirmation. A deleted "
            "message is gone for everybody, including from the scrollback of "
            "people who had already read it, so be sure before calling it and "
            "prefer editing where the message could say something more useful "
            "instead."
            "\nWhat can be reached. Any message this app posted, which "
            "includes the card that tracks this turn and the bubble a streamed "
            "answer is written into. Deleting either of those mid-turn removes "
            "what somebody is reading. Slack refuses a delete on any message "
            "this app did not post."
            + (
                "\nWhere it acts. Pass chat_id for another conversation, or "
                "leave it out for this one. Nothing is confirmed, whichever "
                "conversation is named: deleting a message shows it to nobody."
                if names_a_target
                else _NOT_ELSEWHERE
            )
            + "\nWhat comes back. The conversation and the message that was "
            "deleted."
        ),
        input_params={
            "type": "object",
            "properties": properties,
            "required": ["message_id"],
        },
    )


__all__ = [
    "DELETE_MESSAGE",
    "EDIT_MESSAGE",
    "POST_MESSAGE",
    "WIDENING_TOOL_NAMES",
    "WRITE_TOOL_NAMES",
    "SlackPostToolkit",
    "slack_post_request_metadata",
]
