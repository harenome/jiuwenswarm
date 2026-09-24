# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Read, write, share and delete Slack canvases.

A canvas is a document that lives in Slack rather than beside it. Slack keeps
two kinds and nothing converts between them, which is why creating one is two
tools here rather than one tool with a flag:

* A **standalone canvas**, made by ``canvases.create``. It belongs to whoever
  made it, it is shared by being sent to somebody, and passing a channel to the
  call additionally tabs it into that channel. A workspace may hold any number
  of them.
* A **channel canvas**, made by ``conversations.canvases.create``. It is the
  channel's own canvas, a channel holds one at a time, and who may read it is
  decided by channel membership rather than by any access call.
  ``canvases.delete`` does delete one -- observed, and documented nowhere --
  and whether a channel accepts a new one afterwards is untried.

``permissions.tools`` is keyed by tool name and matched exactly, so one tool is
one policy. Folding the two creations into one name would make *may write a
canvas of its own, may not create the channel's* permanently inexpressible, and
an operator asked to choose between all of it and none of it picks none. The
same argument keeps ``delete_slack_canvas`` apart from everything else.

**The name ``write_slack_channel_canvas`` keeps Slack's word.** This package
writes ``chat_id`` and never ``channel_id``, because a conversation is a
conversation on every platform the runtime speaks to. A *channel canvas* is not
that: it is an object type Slack names, with rules -- one per channel at a
time, access derived from membership, no conversion to or from the other kind
-- that are true of nothing else and are true of no direct message. Spelling it ``write_slack_chat_canvas`` would invent
a name for something Slack already named, would send a reader looking for
documentation that does not exist under that word, and would claim a breadth the
object does not have. The argument that names the conversation stays ``chat_id``,
because that is an identifier this package passes rather than an object Slack
names.

**A canvas's content cannot be read through the canvas API.** ``canvases:read``
unlocks exactly one method, ``canvases.sections.lookup``, and it answers with
section identifiers and no text at all. ``files.info`` carries a dozen
canvas-specific properties and not one of them holds content. So content comes
from an authenticated ``GET`` on the file's ``url_private``, and what comes back
is **HTML** rather than the markdown that was written. The round trip is lossy
by construction: there is no read-modify-write, and an edit has to be written as
new markdown rather than as a patch of what was read.

The one thing that survives the conversion is the identifier. Each element in
the HTML carries its section id in its ``id`` attribute, and those are the same
ids ``canvases.sections.lookup`` answers with, so a reader can map content to
section without a second call.

That download is not written here. ``jiuwenswarm.common.slack_file_transfer``
already holds the hardened path -- DNS-pinned resolution with every address
validated, per-hop redirect revalidation, a fail-closed transport and a byte
ceiling -- and the history toolkit already fetches through it. A second fetch
written beside it would be a second security boundary to keep in step.

**Two tools would be needed to share a canvas, and only one of them exists
here.** ``canvases.access.set`` does not share a canvas; it sets a level on one
that is already shared, and Slack is explicit that "if you are passing the
``user_ids`` argument to update a specific user's access, you must have sent the
user the canvas directly *first*." No canvas method performs that first step --
it is a ``chat.postMessage`` carrying the permalink -- and nothing here posts
it. Posting would make a sharing tool write into a conversation without passing
the ``channels.slack.write`` ladder that governs posting, so the tool names the
missing step in its card and in the refusal, and stops there.

**A multi-target grant can half succeed.** ``failed_to_update_user_ids`` is a
documented refusal of a call that named several users, and Slack's response
carries no field saying which of them took. Reporting one boolean for five
targets would be a claim nobody checked, so a failure that could be partial is
followed by one call per target, which is idempotent, and the result says which
targets took, which did not and with what, and -- if the deadline bit first --
which were never determined.

Only cross-cutting primitives are borrowed, and from the module that owns them.
``slack_history`` holds the reading of a Slack response, the rule for when a
refusal is worth retrying, the pass that keeps a credential out of an error code
or out of a message Slack wrote, and the per-request choice of workspace.
``slack_scope_policy`` holds the one spelling of a method name: three of the six
methods here are two dots deep, so the first-underscore rule the other Slack
tools use would turn ``canvases_access_set`` into ``canvases.access_set``, which
is not a method an operator can look up.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import httpx
from openjiuwen.core.foundation.tool import LocalFunction, Tool, ToolCard

from jiuwenswarm.agents.harness.common.tools.slack_history import (
    _DEFAULT_RETRY_AFTER_SECONDS,
    SlackWorkspaceClients,
    _SlackCallFailure,
    _as_mapping,
    _retry_after_seconds,
    _safe_error_code,
    redact_credentials,
    shared_slack_workspaces,
)
from jiuwenswarm.common.slack_file_transfer import (
    FILE_TRANSFER_TIMEOUT_SECONDS,
    SLACK_FILE_HOST,
    SlackFileTransferRefused,
    https_file_host,
    slack_file_transport,
    stream_slack_file,
)
from jiuwenswarm.common.slack_history_policy import (
    METADATA_ORIGIN_KEY,
    METADATA_TEAM_KEY,
    ORIGIN_CRON_JOB,
    SlackWorkspaceUnresolved,
)
from jiuwenswarm.common.slack_scope_policy import dotted


logger = logging.getLogger(__name__)

#: The six tool names, in the order the cards are built. Nothing reads it: the
#: registration mounts the built tools and takes each one's own ``name``, and
#: each card spells its name itself. It stays exported as this module's
#: statement of the set -- the six names ``permissions.tools`` is written with
#: -- readable without building the toolkit to get them.
CANVAS_TOOL_NAMES: tuple[str, ...] = (
    "read_slack_canvas",
    "write_slack_canvas",
    "write_slack_channel_canvas",
    "edit_slack_canvas",
    "share_slack_canvas",
    "delete_slack_canvas",
)

#: The channel id the cron scheduler delivers a scheduled job under. Spelled
#: here rather than imported from the gateway: a harness tool must not reach
#: into the gateway, which is why the policy module holds the other two names
#: the request predicate reads.
_CRON_REQUEST_CHANNEL_ID = "__cron__"

# ── the methods, in Slack's own spelling ─────────────────────────────────────
#
# Written dotted and sent through ``api_call`` rather than through an SDK
# wrapper. Four of the six take a nested object or an array -- ``changes``,
# ``document_content``, ``criteria``, ``user_ids`` -- and a JSON body is the one
# encoding in which an array is unambiguously an array. The same reasoning the
# search tool records for its own call.

_CANVAS_CREATE = "canvases.create"
_CHANNEL_CANVAS_CREATE = "conversations.canvases.create"
_CANVAS_EDIT = "canvases.edit"
_CANVAS_DELETE = "canvases.delete"
#: Slack's own name for the conversation array on the two access methods. The
#: public argument is ``chat_ids``, because a conversation is a conversation on
#: every platform the runtime speaks to, and the translation happens here.
#: Sending the public name instead is not an error Slack reports as one: an
#: argument it does not define is ignored, the call then names no target at
#: all, and the answer is ``invalid_parameters`` -- which reads as a complaint
#: about the ids rather than about their label.
_SLACK_TARGET_ARGUMENT: Mapping[str, str] = {
    "chat_ids": "channel_ids",
    "user_ids": "user_ids",
}

_CANVAS_ACCESS_SET = "canvases.access.set"
_CANVAS_ACCESS_DELETE = "canvases.access.delete"
_CANVAS_SECTIONS_LOOKUP = "canvases.sections.lookup"

#: The one method here that has an SDK wrapper and no nested argument.
_FILES_INFO = "files_info"

#: The two scopes this module spends, named in a refusal because the code alone
#: tells nobody what to go and grant.
_CANVAS_READ_SCOPE = "canvases:read"
_CANVAS_WRITE_SCOPE = "canvases:write"

#: The 19 section types Slack documents as filterable, in its own order.
#:
#: Rendered into the card as an enum so a model does not guess one. Omitting the
#: argument is a different query rather than a lazier one: Slack says "other
#: types of sections exist that are not currently able to be used as a type to
#: filter on. You may discover these when using a query with no section type
#: provided", so dropping section_types is how those are reached. It is not a
#: licence to send nothing: ``criteria`` is a required argument of the lookup
#: and Slack answers an empty one with ``invalid_arguments``, so the query that
#: reaches those types drops section_types and keeps contains_text.
SECTION_TYPES: tuple[str, ...] = (
    "any_header",
    "blockquote",
    "callout",
    "canvas_unfurl",
    "chart",
    "citation",
    "file_unfurl",
    "flexbox",
    "h1",
    "h2",
    "h3",
    "horizontal_line",
    "list",
    "message_unfurl",
    "sfdc_record_mention",
    "sfdc_record_unfurl",
    "table",
    "user_mention",
    "user_unfurl",
)

#: The three levels ``canvases.access.set`` accepts, in Slack's own words.
ACCESS_LEVELS: tuple[str, ...] = ("read", "write", "owner")

#: What each edit operation needs. The key is Slack's own operation name; the
#: value says whether a section id is required, forbidden or optional, and which
#: argument the operation reads its content from.
#:
#: ``replace`` is the one operation whose section id is optional, and the
#: difference is the whole document rather than a detail: with a section id it
#: replaces that section, without one it replaces the entire canvas. The card
#: says so, because a model that omitted the id by accident would overwrite
#: everything and get a success back.
#:
#: ``rename`` reads ``title``, which is the argument the create tools take for
#: the same thing. Slack wraps a title and a body differently on the wire, and
#: that difference is handled in :meth:`_one_change` rather than being spelled
#: out as a second argument name.
#:
#: The one argument the two paths read differently is a blank title, and they
#: are both right. Slack documents ``canvases.create``'s ``title`` as optional
#: -- "The canvas will be created untitled and empty if none of the optional
#: parameters are specified" -- so the create tools drop a blank and ask for
#: exactly that. ``rename`` carries nothing but the title, and Slack documents
#: no operation that removes one, so a rename to nothing is a request that
#: cannot be honoured and is refused here rather than sent to come back as a
#: bare ``canvas_editing_failed``. The cards state the difference, because it
#: is the caller who sees it.
#:
#: The length check divides on the same documented line. Slack attaches the
#: 1 MiB ceiling to ``document_content`` on create and to "the markdown content
#: of each change" on edit, and a rename's ``title_content`` is the markdown
#: content of a change -- so the check on a rename title is Slack's own limit,
#: while ``title`` on create has no documented limit and is not given an
#: invented one.
_EDIT_OPERATIONS: Mapping[str, tuple[str, str]] = {
    "insert_after": ("required", "markdown"),
    "insert_before": ("required", "markdown"),
    "insert_at_start": ("forbidden", "markdown"),
    "insert_at_end": ("forbidden", "markdown"),
    "replace": ("optional", "markdown"),
    "delete": ("required", ""),
    "rename": ("forbidden", "title"),
}

#: Slack's documented ceiling on the content of one create or one change:
#: "The markdown content of each change is limited to 1 MiB (1,048,576
#: characters)." Counted in characters, which is the unit Slack gave.
MAX_MARKDOWN_CHARS = 1_048_576

#: How many bytes of canvas HTML are worth pulling down. Ours rather than
#: Slack's: the markdown behind a canvas is capped at 1 MiB, the HTML it renders
#: to is larger than that, and nothing is served by streaming an unbounded
#: document into memory to throw most of it away.
MAX_CANVAS_BYTES = 4 * 1024 * 1024

#: How much of that HTML is returned. Also ours, and also not a platform limit:
#: a tool result is read by a model with a finite context, and a canvas can be
#: longer than one. When the cap bites the result says so by name and reports
#: both counts, because silent truncation would hand a reader a document that
#: looks complete and is not.
MAX_CONTENT_CHARS = 60_000

#: How many section records one lookup returns. Slack paginates nothing here,
#: so this bounds what is echoed into a result rather than what is fetched.
MAX_SECTIONS = 500

#: A Slack canvas is a file, and a file id is ``F`` followed by upper-case
#: alphanumerics. Checked before the value travels into an API argument, because
#: it reaches this tool from a model that read it out of somewhere else. Kept
#: deliberately loose on length: Slack has lengthened an id family before, and a
#: bound invented here would refuse a real canvas.
_CANVAS_ID_RE = re.compile(r"\AF[A-Z0-9]{2,}\Z")

#: A Slack conversation id. ``D`` is a direct message and is refused by name;
#: ``C`` and ``G`` are not told apart here, because a private channel and a
#: group direct message share the ``G`` family and Slack is the only thing that
#: can say which of them an id is.
_CHAT_ID_RE = re.compile(r"\A[CDG][A-Z0-9]{2,}\Z")

#: A Slack user id.
_USER_ID_RE = re.compile(r"\A[UW][A-Z0-9]{2,}\Z")

#: How much of a Slack-written ``detail`` string is echoed back. Slack writes
#: the line number and the offending construct into it, which is the part a
#: caller acts on, and it is not a value this module composed.
_MAX_DETAIL_CHARS = 1000

#: Wall clock for one tool call, retries included. Generous relative to the pin
#: and Home tab tools because two of these make more than one Slack call: a read
#: makes three, and a share that has to find out which target failed makes one
#: per target.
_TIMEOUT_SECONDS = 60.0

#: How many times one call will wait out a rate limit before giving up.
_MAX_RATE_LIMIT_RETRIES = 2

#: What a refusal means, for the codes where the code alone is not actionable.
#:
#: One table for all six tools. A code means the same thing whichever method
#: raised it, and two tables would be two chances to explain one code two ways.
_FAILURE_DETAIL: Mapping[str, str] = {
    "canvas_not_found": (
        "Slack answers this for three different situations and does not say"
        " which: the canvas does not exist, it has been deleted, or it exists"
        " and has never been shared with this app. Only the third is fixable,"
        " and only by a person: a canvas this app did not create is invisible"
        " to it until somebody shares it. Retrying changes none of the three"
    ),
    "canvas_deleted": (
        "this canvas has been deleted. Slack keeps no undelete, so it cannot be"
        " read or edited again and a retry will not help"
    ),
    "canvas_editing_locked": (
        "another edit to this canvas is in progress. Slack rejects a concurrent"
        " edit rather than queueing it, so this one did not happen and nothing"
        " changed. This is the one refusal here where a retry after a short"
        " delay is the remedy"
    ),
    "canvas_editing_failed": (
        "Slack accepted the request and then could not apply the change. The"
        " canvas is unchanged. Check the markdown, or retry"
    ),
    "canvas_too_large": (
        "the canvas is too large for the requested change. Either the canvas or"
        " the edit has to get smaller; a retry of the same change will fail the"
        " same way"
    ),
    "canvas_creation_failed": (
        "Slack accepted the request and then could not create the canvas."
        " Nothing was created"
    ),
    "canvas_tab_creation_failed": (
        "the canvas was created but Slack could not tab it into the"
        " conversation, so it exists and is not visible there"
    ),
    "channel_canvas_creation_failed": (
        "Slack accepted the request and then could not create the channel"
        " canvas. Nothing was created"
    ),
    "channel_canvas_already_exists": (
        "this conversation already has its canvas. A conversation holds one at"
        " a time and Slack refuses a second, so edit the existing canvas"
        " instead. delete_slack_canvas does delete a channel canvas, but the"
        " deletion cannot be undone and whether a conversation accepts a new"
        " one afterwards has not been tried, so deleting to start again risks"
        " leaving the conversation with none"
    ),
    "free_teams_cannot_create_standalone_canvases": (
        "this workspace is on a free plan, where a canvas cannot exist on its"
        " own and has to be tabbed into a conversation. Pass chat_id, which is"
        " optional on a paid plan and required here"
    ),
    "free_teams_cannot_edit_standalone_canvases": (
        "this workspace is on a free plan, which cannot edit a canvas that is"
        " not attached to a conversation. Nothing was changed, and no argument"
        " to this tool alters that"
    ),
    "free_team_canvas_tab_already_exists": (
        "this workspace is on a free plan, which allows one canvas tab per"
        " conversation, and that conversation already has one"
    ),
    "team_tier_cannot_create_channel_canvases": (
        "this workspace's plan does not allow a conversation to have its own"
        " canvas at all. That is a plan to change rather than a call to retry"
    ),
    "canvas_disabled_user_team": (
        "canvases are switched off for this workspace. Nothing this tool can be"
        " passed turns them back on; an administrator has to"
    ),
    "canvas_globally_disabled": (
        "canvases are switched off across this Slack organisation. An"
        " administrator has to turn them back on"
    ),
    "canvas_deleting_disabled": (
        "deleting a canvas is switched off in this workspace. The canvas is"
        " untouched and a retry will not help"
    ),
    "canvas_disabled_file_team": (
        "canvases are switched off for the workspace this canvas belongs to,"
        " which is not necessarily this one"
    ),
    "restricted_action": (
        "this workspace's own rules refuse this action. It is an administrator"
        " setting rather than a missing scope, so reinstalling the app will not"
        " change it"
    ),
    "invalid_parameters": (
        "Slack says of the call this tool sent: \"One of user_ids or"
        " channel_ids must be defined, but not both\". Those are Slack's own"
        " argument names, and its complaint is about what reached it rather"
        " than about what was passed here -- so a call that named exactly one"
        " set of targets and got this back is a fault in this tool, not in the"
        " arguments, and is worth reporting as one"
    ),
    "user_not_found": (
        "one of the user ids names nobody in this workspace. A user id is not"
        " transferable between workspaces, so one taken from elsewhere names"
        " nothing here"
    ),
    "channel_not_found": (
        "one of the chat ids names no conversation this app can see. A"
        " conversation id is not transferable between workspaces, and a private"
        " conversation this app is not in is invisible to it"
    ),
    "failed_to_update_user_ids": (
        "Slack refused the grant for at least one of the named users. The"
        " commonest cause is the one thing no canvas method does: a user's"
        " access can only be set on a canvas that has already been sent to that"
        " user directly, and being a member of a conversation the canvas was"
        " shared into does not count"
    ),
    "not_allowed_token_type": (
        "this method declines the kind of token this app holds"
    ),
}

#: Codes that mean the call named several targets and some subset of them
#: failed. Reaching one is what starts the per-target pass.
_PARTIAL_FAILURE_CODES = frozenset(
    {"failed_to_update_user_ids", "user_not_found", "channel_not_found"}
)


def slack_canvas_request_metadata(
    channel_id: str | None,
    metadata: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Return trusted metadata for a request that may touch a canvas, or nothing.

    Two request shapes, and one condition behind both: a Slack install can be
    resolved for this request, so there is a workspace for a canvas to exist in
    and a token to reach it with.

    * An inbound Slack turn, which arrives on the ``slack`` transport.
    * A scheduled run, honoured only where the scheduler marked it as its own.
      A canvas is the surface a recurring report belongs on -- it is edited in
      place rather than reposted -- so excluding cron would take the capability
      away from the case it fits best. Without the marker a stray Slack field
      left on some other request cannot reach these tools.

    Nothing further is required, and each absence is a decision. No conversation
    is demanded, because five of the six tools name no conversation at all and
    the sixth takes one as an argument. No requester is demanded, because a
    canvas belongs to the app that made it rather than to the person who asked.

    Not a condition either: the history policy word. That says how far this
    conversation may *read* scrollback, and a canvas holds none of it.
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
    return dict(metadata)


@dataclass(frozen=True)
class _AccessRequest:
    """One access change, as the five facts every step of it needs.

    The three methods below -- the call, the per-target pass that follows a
    partial failure, and the report it produces -- take the same five values
    and must agree about every one of them. Passed as one object rather than as
    five parameters threaded through three signatures, so that the per-target
    retry cannot end up sending a different level from the call it is finding
    out about.
    """

    #: ``canvases.access.set`` or ``canvases.access.delete``.
    method: str
    canvas_id: str
    #: ``"chat_ids"`` or ``"user_ids"`` -- which target array this call fills
    #: in. One or the other, never both. These are the *public* names and a
    #: result reports them; ``_SLACK_TARGET_ARGUMENT`` gives the name the wire
    #: wants, which for a conversation is ``channel_ids`` rather than this.
    kind: str
    ids: tuple[str, ...]
    #: Empty for a revoke, which carries no level.
    level: str

    @property
    def removes(self) -> bool:
        return self.method == _CANVAS_ACCESS_DELETE

    @property
    def reported_level(self) -> str:
        """The level a result reports, which for a revoke is ``"none"``."""
        return "none" if self.removes else self.level


class _CanvasCallFailure(_SlackCallFailure):
    """A Slack refusal that keeps the sentence Slack wrote beside the code.

    ``canvases.edit`` is the reason this type exists. Slack refuses unparseable
    markdown with a generic code and puts the whole of the useful answer in a
    ``detail`` string -- ``"'content' error: line 28: Unsupported block type
    (List) within block quote"`` -- which names the line and the construct. The
    shared failure type carries a code alone, so a caller would be told that
    something was wrong with a thousand lines of markdown and nothing more.

    ``where`` is overridden because three of these methods are two dots deep.
    The shared rule turns the first underscore into a dot, which is exact for
    ``chat_postMessage`` and wrong for ``canvases_access_set``.
    """

    def __init__(
        self,
        code: str,
        method: str = "",
        *,
        subject: str = "",
        detail: str = "",
    ) -> None:
        super().__init__(code, method, subject=subject)
        self.detail = detail

    @property
    def where(self) -> str:
        if not self.method:
            return "a Slack call"
        name = dotted(self.method.replace(".", "_"))
        return f"{name} on {self.subject}" if self.subject else name


def _canvas_refusal_json(
    code: str,
    detail: str = "",
    **fields: Any,
) -> str:
    """One refusal, in a shape that never claims to know what it does not.

    Nothing about the canvas's state is asserted. A call Slack refused
    establishes nothing about what the canvas holds now -- an edit refused for
    its size did not thereby empty the document -- so no ``created``,
    ``edited`` or ``deleted`` key appears on a failure, where a ``false`` would
    read as a claim about the canvas rather than about this call.
    """
    payload: dict[str, Any] = {"ok": False, "error": code}
    if detail:
        payload["detail"] = detail
    for key, value in fields.items():
        if value:
            payload[key] = value
    logger.warning(
        "slack canvas refused: %s%s", code, f" -- {detail}" if detail else ""
    )
    return json.dumps(payload, ensure_ascii=False)


def _string_list(value: Any) -> "list[str] | None":
    """A declared array of ids as a list of non-empty strings, or ``None``.

    ``None`` means *this was not an array*, which is a refusal the caller
    reports; an empty list means *an array with nothing in it*, which is a
    different refusal. A bare string is not silently wrapped: a model that
    passed one meant an array with one element, and accepting the shorthand here
    would make the arity check below unreachable for exactly the argument it
    exists to guard.
    """
    if value is None:
        return []
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return None
    out: list[str] = []
    for item in value:
        if not isinstance(item, str):
            return None
        text = item.strip()
        if text:
            out.append(text)
    return out


class SlackCanvasToolkit:
    """The six canvas tools, scoped to the Slack install the request came from."""

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
        http_client_factory: Any = None,
    ) -> None:
        self._request_metadata = dict(metadata) if metadata else {}
        self._metadata_provider = metadata_provider
        self._client = client
        self._workspaces = workspaces or shared_slack_workspaces()
        self._timeout_seconds = max(1.0, float(timeout_seconds))
        self._sleep = sleep
        self._monotonic = monotonic
        self._http_client_factory = http_client_factory
        self._bot_token = ""
        self._deadline = 0.0

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
        """Bind this request to the install it arrived from, and start the clock.

        Read per call rather than captured at construction: this toolkit is
        built once and answers every request for the life of the process, a
        token rotated underneath it must take effect without a restart, and with
        several installs configured which token serves a call is a property of
        the request rather than of start-up.

        A canvas written into the wrong workspace is a document in a stranger's
        Slack, so an unresolvable install is refused rather than served from
        whichever block happens to hold a token.
        """
        slack = await self._workspaces.settings_for(
            str(metadata.get(METADATA_TEAM_KEY) or "").strip()
        )
        self._bot_token = str(slack.get("bot_token") or "").strip()
        self._deadline = self._monotonic() + self._timeout_seconds

    def _get_client(self) -> Any:
        if self._client is not None:
            return self._client
        return self._workspaces.client_for(self._bot_token)

    def _remaining(self) -> float:
        """What is left of this call's wall clock."""
        return self._deadline - self._monotonic()

    # ── the Slack calls ──────────────────────────────────────────────────

    async def _invoke(self, method: str, make_call: Any) -> dict[str, Any]:
        """Run one Slack call, reducing any refusal to a credential-free code.

        Both failure shapes are read. Slack answers a refused canvas call with
        HTTP 200 and ``{"ok": false, "error": ..., "detail": ...}``; the SDK
        raises that as ``SlackApiError``, and an older one hands the body back
        instead, so a code arriving one way on one deployment and the other way
        on the next would otherwise be two behaviours.

        The ``detail`` string is read from both shapes for the same reason, and
        is put through the same credential pass the code is. It is text Slack
        wrote rather than a value this module composed, and it is about to be
        echoed into a result that is persisted.
        """
        try:
            client = self._get_client()
        except _SlackCallFailure as exc:
            raise _CanvasCallFailure(exc.code, exc.method or method) from None
        retries = 0
        while True:
            remaining = self._remaining()
            if remaining <= 0:
                raise _CanvasCallFailure("slack_call_timed_out", method)
            try:
                response = await asyncio.wait_for(
                    make_call(client), timeout=remaining
                )
            except TimeoutError:
                raise _CanvasCallFailure("slack_call_timed_out", method) from None
            except Exception as exc:  # noqa: BLE001 - SDK types vary by version.
                delay = _retry_after_seconds(exc)
                if delay is not None and retries < _MAX_RATE_LIMIT_RETRIES:
                    retries += 1
                    await self._sleep(delay)
                    continue
                data = _as_mapping(getattr(exc, "response", None))
                raise self._failure(data, method) from None

            data = _as_mapping(response)
            if data.get("ok", True) is False:
                failure = self._failure(data, method)
                if (
                    failure.code == "ratelimited"
                    and retries < _MAX_RATE_LIMIT_RETRIES
                ):
                    retries += 1
                    await self._sleep(_DEFAULT_RETRY_AFTER_SECONDS)
                    continue
                raise failure
            return data

    def _failure(self, data: Mapping[str, Any], method: str) -> _CanvasCallFailure:
        """One Slack body as the failure this module raises."""
        code = _safe_error_code(data.get("error"), self._bot_token)
        detail = ""
        raw = data.get("detail")
        if raw:
            detail, _, _ = redact_credentials(
                raw, bot_token=self._bot_token, cap=_MAX_DETAIL_CHARS
            )
        return _CanvasCallFailure(code, method, detail=detail)

    async def _api(self, method: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        """One canvas method, sent as a JSON body.

        Sent as JSON rather than as a form because four of these methods take an
        array or a nested object, and a JSON body is the one encoding in which
        an array is unambiguously an array.
        """
        body = dict(payload)
        return await self._invoke(
            method, lambda client: client.api_call(method, json=body)
        )

    async def _call(self, method: str, **kwargs: Any) -> dict[str, Any]:
        """One method the SDK has a wrapper for, called through it."""
        return await self._invoke(
            method, lambda client: getattr(client, method)(**kwargs)
        )

    # ── argument checks shared by more than one tool ─────────────────────

    @staticmethod
    def _canvas_id_refusal(canvas_id: str) -> "str | None":
        """Why *canvas_id* cannot be sent, or ``None`` when it can."""
        if not canvas_id:
            return _canvas_refusal_json(
                "canvas_id_required",
                "no canvas id was given. A canvas id is taken unchanged from a"
                " result that reported one",
            )
        if not _CANVAS_ID_RE.match(canvas_id):
            return _canvas_refusal_json(
                "canvas_id_malformed",
                "a Slack canvas id is an F followed by upper-case letters and"
                " digits, such as F1234ABCD; this is not one",
                canvas_id=canvas_id[:32],
            )
        return None

    @staticmethod
    def _markdown_refusal(markdown: str, *, where: str) -> "str | None":
        """Why *markdown* cannot be sent, or ``None`` when it can."""
        if len(markdown) <= MAX_MARKDOWN_CHARS:
            return None
        return _canvas_refusal_json(
            "content_too_large",
            f"Slack limits the markdown content of {where} to"
            f" {MAX_MARKDOWN_CHARS} characters and this came to"
            f" {len(markdown)}. Nothing was sent. Split the content across"
            f" several calls, or write less",
        )

    def _slack_refusal(self, exc: _CanvasCallFailure, **fields: Any) -> str:
        """One Slack refusal as the shape every failure here returns.

        Slack's own ``detail`` wins over anything written here and is passed
        through rather than summarised. It is the only thing that names the line
        and the construct behind a markdown refusal, and a caller with a
        thousand lines of markdown and no line number has nothing to act on.
        """
        detail = _FAILURE_DETAIL.get(exc.code, "")
        if not detail:
            detail = f"Slack refused {exc.where}"
            if exc.code == "missing_scope":
                scope = (
                    _CANVAS_READ_SCOPE
                    if exc.method == _CANVAS_SECTIONS_LOOKUP
                    else _CANVAS_WRITE_SCOPE
                )
                detail += (
                    f"; this needs the {scope} scope, which this installation"
                    f" does not hold"
                )
        if exc.detail:
            detail = f"{detail}. Slack said: {exc.detail}"
        return _canvas_refusal_json(exc.code, detail, **fields)

    # ── reading ──────────────────────────────────────────────────────────

    async def _canvas_file(self, canvas_id: str) -> dict[str, Any]:
        """The ``files.info`` record for a canvas, or the refusal it earns."""
        response = await self._call(_FILES_INFO, file=canvas_id)
        record = response.get("file")
        if not isinstance(record, Mapping):
            raise _CanvasCallFailure("canvas_not_found", _FILES_INFO)
        return dict(record)

    async def _workspace_hosts(self) -> frozenset[str]:
        """The hosts a bot-token download may be sent to.

        ``files.slack.com`` always, and the workspace's own host once
        ``auth.test`` has named it -- a canvas is served from the second rather
        than the first, its ``url_private`` being a ``/docs/`` path on the
        workspace. Two entries and no wildcard: a suffix match on
        ``.slack.com`` would be a rule about a string rather than about a host
        Slack told us it serves.

        A failed ``auth.test`` narrows the allow-list rather than widening it,
        so a download is refused where it cannot be placed.
        """
        try:
            identity = await self._call("auth_test")
        except _CanvasCallFailure:
            return frozenset({SLACK_FILE_HOST})
        host = https_file_host(identity.get("url"))
        if host:
            return frozenset({SLACK_FILE_HOST, host})
        return frozenset({SLACK_FILE_HOST})

    async def _canvas_html(self, url: str) -> tuple[str, int, bool]:
        """Fetch one canvas as HTML, returning the text, its length and a flag.

        Not a new fetch. ``slack_file_transfer`` holds the hardened path the
        history toolkit already downloads through -- one resolution whose every
        address is validated, the address dialled rather than the name, each
        redirect hop put through the same checks the first URL faced, and a
        transport that refuses an unpinned connection -- and a second copy of
        that reasoning is a second security boundary to keep in step.

        The URL stays a local. It is never returned, never logged and never
        allowed into an exception that reaches a result: an httpx exception
        message embeds the URL verbatim, so a failure is mapped from the status
        code and the exception class alone.
        """
        hosts = await self._workspace_hosts()
        if https_file_host(url) not in hosts:
            raise _CanvasCallFailure("canvas_host_not_allowed", _FILES_INFO)
        headers = {"Authorization": f"Bearer {self._bot_token}"}
        timeout = httpx.Timeout(FILE_TRANSFER_TIMEOUT_SECONDS)
        factory = self._http_client_factory or (
            lambda: httpx.AsyncClient(
                timeout=timeout,
                follow_redirects=False,
                transport=slack_file_transport(hosts),
            )
        )
        chunks: list[bytes] = []
        size = 0
        truncated = False
        async with (
            factory() as client,
            stream_slack_file(
                client, url, headers=headers, allowed_hosts=hosts
            ) as response,
        ):
                if response.status_code in (401, 403):
                    raise _CanvasCallFailure("canvas_read_denied", _FILES_INFO)
                if response.status_code != 200:
                    raise _CanvasCallFailure("canvas_download_failed", _FILES_INFO)
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > MAX_CANVAS_BYTES:
                        truncated = True
                        break
                    chunks.append(chunk)
        text = b"".join(chunks).decode("utf-8", "replace")
        total = len(text)
        if total > MAX_CONTENT_CHARS:
            return text[:MAX_CONTENT_CHARS], total, True
        return text, total, truncated

    async def _sections(
        self, canvas_id: str, criteria: Mapping[str, Any]
    ) -> list[dict[str, Any]]:
        """The section records one lookup answers with.

        Identifiers and nothing else: this method returns no text, which is the
        whole reason the content has to be fetched separately.
        """
        response = await self._api(
            _CANVAS_SECTIONS_LOOKUP,
            {"canvas_id": canvas_id, "criteria": dict(criteria)},
        )
        raw = response.get("sections")
        out: list[dict[str, Any]] = []
        for item in raw if isinstance(raw, list) else []:
            if isinstance(item, Mapping):
                section_id = str(item.get("id") or "").strip()
                if section_id:
                    out.append({"section_id": section_id})
            if len(out) >= MAX_SECTIONS:
                break
        return out

    async def read_slack_canvas(
        self,
        canvas_id: str,
        section_types: Any = None,
        contains_text: str | None = None,
    ) -> str:
        """Read one canvas: its content as HTML, and its section identifiers."""
        metadata = self._runtime_metadata()
        if not metadata:
            return self._no_slack_request_refusal()
        raw_id = str(canvas_id or "").strip()
        refusal = self._canvas_id_refusal(raw_id)
        if refusal:
            return refusal

        types = _string_list(section_types)
        if types is None:
            return _canvas_refusal_json(
                "section_types_malformed",
                "section_types is a list of section type names. It was not a"
                " list of strings",
                canvas_id=raw_id,
            )
        unknown = [name for name in types if name not in SECTION_TYPES]
        if unknown:
            return _canvas_refusal_json(
                "section_type_unknown",
                f"Slack filters on {len(SECTION_TYPES)} section types and"
                f" {', '.join(sorted(unknown))} is not among them. Dropping"
                f" section_types and filtering on contains_text alone is the"
                f" only way to reach the types Slack does not let a query"
                f" filter on",
                canvas_id=raw_id,
            )
        text = str(contains_text or "").strip()

        try:
            await self._load_settings(metadata)
        except SlackWorkspaceUnresolved as unresolved:
            return _canvas_refusal_json(
                unresolved.code, unresolved.detail, canvas_id=raw_id
            )

        try:
            record = await self._canvas_file(raw_id)
        except _CanvasCallFailure as exc:
            return self._slack_refusal(exc, canvas_id=raw_id)

        filetype = str(record.get("filetype") or "").strip().lower()
        if filetype and filetype not in {"canvas", "quip"}:
            return _canvas_refusal_json(
                "not_a_canvas",
                f"this id names a file of type {filetype!r} rather than a"
                f" canvas. Nothing was read",
                canvas_id=raw_id,
            )

        result: dict[str, Any] = {
            "ok": True,
            "canvas_id": raw_id,
            "title": str(record.get("title") or ""),
            "is_channel_canvas": bool(record.get("is_channel_space")),
            "linked_chat_id": str(record.get("linked_channel_id") or ""),
            "permalink": str(record.get("permalink") or ""),
            # Unconditional, and the sentence is the fact rather than a caveat
            # about this call. A canvas is written as markdown and served as
            # HTML, so what comes back here is never what was written and can
            # never be edited back in.
            "content_format": "html",
            "content_note": (
                "Content comes back as HTML. Edits are written as markdown, so"
                " this cannot be modified and written back -- compose the new"
                " markdown instead. Each element carries its section id in its"
                " id attribute, and those are the same ids the sections list"
                " below reports."
            ),
        }

        coverage: list[str] = []
        url = str(record.get("url_private") or "").strip()
        if not url:
            coverage.append(
                "content_unavailable: Slack's record for this canvas carries no"
                " private URL to fetch the content from"
            )
        else:
            try:
                html, total, truncated = await self._canvas_html(url)
            except SlackFileTransferRefused as refused:
                coverage.append(f"content_unavailable: {refused.detail}")
            except _CanvasCallFailure as exc:
                coverage.append(
                    f"content_unavailable: {exc.code} from the canvas download"
                )
            except Exception:  # noqa: BLE001 - httpx types vary by version.
                # Deliberately not re-raised and deliberately not detailed: an
                # httpx exception message embeds the URL, which is the one
                # thing that must not reach a persisted result.
                coverage.append(
                    "content_unavailable: the canvas download failed"
                )
            else:
                result["content_html"] = html
                result["content_chars"] = len(html)
                if truncated:
                    result["content_truncated"] = True
                    result["content_chars_total"] = total
                    coverage.append(
                        f"content_truncated: this canvas is {total} characters"
                        f" of HTML and the first {len(html)} are returned. What"
                        f" is missing is the end of the document"
                    )

        criteria: dict[str, Any] = {}
        if types:
            criteria["section_types"] = types
        if text:
            criteria["contains_text"] = text
        if not criteria:
            # Not a failure, and kept lexically apart from one. Slack takes
            # ``criteria`` as a required argument and answers an empty one with
            # ``invalid_arguments``, so there is no call to make here -- and a
            # caller who named no filter asked for the canvas rather than for
            # its sections, so refusing the read would cost them the content
            # over an argument the content does not need. Reporting it as
            # ``sections_unavailable`` would claim a lookup was made and lost.
            # Nobody asked, nothing was sent, and the two are worth telling
            # apart: one is this caller's choice and the other is a failure
            # they may be able to fix.
            result["sections_filter"] = "none"
            coverage.append(
                "sections_not_requested: no section filter was named, so"
                " Slack's section lookup was not called and no section was"
                " looked for. Pass section_types or contains_text to list"
                " them. Short of that, every element of the content above"
                " carries its section id in its id attribute"
            )
        else:
            try:
                sections = await self._sections(raw_id, criteria)
            except _CanvasCallFailure as exc:
                detail = _FAILURE_DETAIL.get(exc.code, "")
                if exc.code == "missing_scope":
                    detail = (
                        f"this installation does not hold the"
                        f" {_CANVAS_READ_SCOPE} scope"
                    )
                coverage.append(
                    f"sections_unavailable: {exc.code}"
                    + (f" -- {detail}" if detail else "")
                )
            else:
                result["sections"] = sections
                result["sections_count"] = len(sections)
                if len(sections) >= MAX_SECTIONS:
                    coverage.append(
                        f"sections_truncated: this listing stops at"
                        f" {MAX_SECTIONS} sections"
                    )
                result["sections_filter"] = criteria

        if coverage:
            result["coverage"] = coverage
        return json.dumps(result, ensure_ascii=False)

    # ── creating ─────────────────────────────────────────────────────────

    @staticmethod
    def _document_content(markdown: str) -> dict[str, Any]:
        """Slack's content wrapper around one piece of markdown.

        ``type`` is not an argument of any tool here. Slack documents exactly
        one value for it, so an argument would be a question with one answer,
        and the wrapper itself is plumbing rather than a choice: a caller writes
        markdown and this is the envelope Slack wants it in.
        """
        return {"type": "markdown", "markdown": markdown}

    def _no_slack_request_refusal(self) -> str:
        """The refusal for a request no Slack path settled.

        The registration gate already declines to mount these tools for such a
        request, so this is defence in depth: the provider fails closed per
        request, and a toolkit mounted for one turn answers the next.
        """
        return _canvas_refusal_json(
            "trusted_slack_request_required",
            "this request did not arrive from Slack, so there is no workspace"
            " for a canvas to exist in and no token to reach one with",
        )

    async def _create(
        self,
        method: str,
        payload: dict[str, Any],
        *,
        chat_id: str = "",
    ) -> str:
        """Make one canvas and report it, or say why it was not made."""
        try:
            response = await self._api(method, payload)
        except _CanvasCallFailure as exc:
            return self._slack_refusal(exc, chat_id=chat_id)
        canvas_id = str(response.get("canvas_id") or "").strip()
        logger.info("slack canvas: created %s via %s", canvas_id, method)
        result: dict[str, Any] = {"ok": True, "canvas_id": canvas_id}
        if chat_id:
            result["chat_id"] = chat_id
        result["is_channel_canvas"] = method == _CHANNEL_CANVAS_CREATE
        return json.dumps(result, ensure_ascii=False)

    async def write_slack_canvas(
        self,
        title: str | None = None,
        markdown: str | None = None,
        chat_id: str | None = None,
    ) -> str:
        """Create a standalone canvas, optionally tabbed into a conversation."""
        metadata = self._runtime_metadata()
        if not metadata:
            return self._no_slack_request_refusal()

        body = str(markdown or "")
        refusal = self._markdown_refusal(body, where="a canvas")
        if refusal:
            return refusal
        room = str(chat_id or "").strip()
        if room and not _CHAT_ID_RE.match(room):
            return _canvas_refusal_json(
                "chat_id_malformed",
                "a Slack conversation id is a C, D or G followed by upper-case"
                " letters and digits; this is not one",
                chat_id=room[:32],
            )
        if room.startswith("D"):
            return _canvas_refusal_json(
                "chat_id_is_a_direct_message",
                "a canvas cannot be tabbed into a direct message. Create the"
                " canvas without a chat_id and share it instead",
                chat_id=room,
            )

        try:
            await self._load_settings(metadata)
        except SlackWorkspaceUnresolved as unresolved:
            return _canvas_refusal_json(unresolved.code, unresolved.detail)

        payload: dict[str, Any] = {}
        heading = str(title or "").strip()
        if heading:
            payload["title"] = heading
        if body:
            payload["document_content"] = self._document_content(body)
        if room:
            payload["channel_id"] = room
        return await self._create(_CANVAS_CREATE, payload, chat_id=room)

    async def write_slack_channel_canvas(
        self,
        chat_id: str,
        title: str | None = None,
        markdown: str | None = None,
    ) -> str:
        """Create the conversation's own canvas, which it may have only once."""
        metadata = self._runtime_metadata()
        if not metadata:
            return self._no_slack_request_refusal()

        room = str(chat_id or "").strip()
        if not room:
            return _canvas_refusal_json(
                "chat_id_required",
                "no conversation was named. A channel canvas belongs to one"
                " conversation and there is no default to fall back to",
            )
        if not _CHAT_ID_RE.match(room):
            return _canvas_refusal_json(
                "chat_id_malformed",
                "a Slack conversation id is a C, D or G followed by upper-case"
                " letters and digits; this is not one",
                chat_id=room[:32],
            )
        if room.startswith("D"):
            return _canvas_refusal_json(
                "chat_id_is_a_direct_message",
                "a direct message has no canvas of its own. Only a channel"
                " does",
                chat_id=room,
            )
        body = str(markdown or "")
        refusal = self._markdown_refusal(body, where="a canvas")
        if refusal:
            return refusal

        try:
            await self._load_settings(metadata)
        except SlackWorkspaceUnresolved as unresolved:
            return _canvas_refusal_json(unresolved.code, unresolved.detail)

        payload: dict[str, Any] = {"channel_id": room}
        heading = str(title or "").strip()
        if heading:
            payload["title"] = heading
        if body:
            payload["document_content"] = self._document_content(body)
        return await self._create(_CHANNEL_CANVAS_CREATE, payload, chat_id=room)

    # ── editing ──────────────────────────────────────────────────────────

    def _one_change(self, change: Any) -> "tuple[dict[str, Any], str]":
        """One declared change as Slack's own change object, or a refusal.

        Returns ``({}, refusal)`` for anything Slack would decline, so that a
        malformed change never reaches the API to be refused there: an edit is
        a write, and a write refused for a bad argument must not have happened
        before we find out.
        """
        if not isinstance(change, Mapping):
            return {}, _canvas_refusal_json(
                "change_malformed",
                "each entry in changes is an object naming an operation and"
                " what it acts on. This one is not an object",
            )
        operation = str(change.get("operation") or "").strip()
        if operation not in _EDIT_OPERATIONS:
            return {}, _canvas_refusal_json(
                "operation_unknown",
                f"{operation!r} is not an edit operation. The operations are"
                f" {', '.join(sorted(_EDIT_OPERATIONS))}",
            )
        section_rule, content_key = _EDIT_OPERATIONS[operation]
        section_id = str(change.get("section_id") or "").strip()
        if section_rule == "required" and not section_id:
            return {}, _canvas_refusal_json(
                "section_id_required",
                f"the {operation} operation acts on one section and none was"
                f" named. A section id is taken unchanged from a read of this"
                f" canvas",
            )
        if section_rule == "forbidden" and section_id:
            return {}, _canvas_refusal_json(
                "section_id_not_accepted",
                f"the {operation} operation does not act on a single section,"
                f" so a section id would be ignored. Nothing was sent",
            )

        body: dict[str, Any] = {"operation": operation}
        if section_id:
            body["section_id"] = section_id
        if not content_key:
            return body, ""

        markdown = str(change.get(content_key) or "")
        if operation == "rename":
            # Stripped, and only here. A title made of spaces is the same
            # request as a title left out -- neither names the canvas -- so it
            # meets the same refusal below rather than being sent as a name
            # made of whitespace. Document content is not stripped with it:
            # leading spaces open a code block and a trailing newline closes a
            # list, so trimming a body would edit it.
            #
            # This is the only input on which the rename path and the create
            # tools disagreed. Blank is a deliberate difference between them --
            # see the two cards -- and whitespace was not; it was create
            # stripping and rename not.
            markdown = markdown.strip()
        if not markdown:
            return {}, _canvas_refusal_json(
                "change_content_required",
                f"the {operation} operation writes content and none was given."
                f" Pass it as {content_key}",
            )
        refusal = self._markdown_refusal(markdown, where="one change")
        if refusal:
            return {}, refusal
        # Slack carries a rename under ``title_content`` and every other
        # operation under ``document_content``. Both hold the same wrapper, so
        # the only thing that differs is the key, and a caller who passed a
        # title has already said which one they meant.
        key = "title_content" if operation == "rename" else "document_content"
        body[key] = self._document_content(markdown)
        return body, ""

    async def edit_slack_canvas(self, canvas_id: str, changes: Any) -> str:
        """Apply one change to a canvas."""
        metadata = self._runtime_metadata()
        if not metadata:
            return self._no_slack_request_refusal()
        raw_id = str(canvas_id or "").strip()
        refusal = self._canvas_id_refusal(raw_id)
        if refusal:
            return refusal

        if changes is None or isinstance(changes, (str, bytes)):
            return _canvas_refusal_json(
                "changes_malformed",
                "changes is a list of change objects. It was not a list",
                canvas_id=raw_id,
            )
        if not isinstance(changes, Sequence):
            return _canvas_refusal_json(
                "changes_malformed",
                "changes is a list of change objects. It was not a list",
                canvas_id=raw_id,
            )
        if not changes:
            return _canvas_refusal_json(
                "changes_required",
                "changes held nothing, so there is nothing to apply and the"
                " canvas is untouched",
                canvas_id=raw_id,
            )
        if len(changes) > 1:
            # Slack's own sentence, quoted rather than paraphrased, and the
            # argument stays an array for the same reason: "Only one operation
            # per API call is currently supported." When that stops being true
            # this check is the only thing to delete, rather than an argument
            # shape to redesign.
            return _canvas_refusal_json(
                "too_many_changes",
                f"Slack says of this method that \"Only one operation per API"
                f" call is currently supported\", and {len(changes)} were"
                f" given. Nothing was sent. Apply them one call at a time, in"
                f" the order they should take effect",
                canvas_id=raw_id,
            )

        body, change_refusal = self._one_change(changes[0])
        if change_refusal:
            return change_refusal

        try:
            await self._load_settings(metadata)
        except SlackWorkspaceUnresolved as unresolved:
            return _canvas_refusal_json(
                unresolved.code, unresolved.detail, canvas_id=raw_id
            )

        try:
            await self._api(
                _CANVAS_EDIT, {"canvas_id": raw_id, "changes": [body]}
            )
        except _CanvasCallFailure as exc:
            return self._slack_refusal(exc, canvas_id=raw_id)

        logger.info(
            "slack canvas: applied %s to %s", body["operation"], raw_id
        )
        return json.dumps(
            {
                "ok": True,
                "canvas_id": raw_id,
                "operation": body["operation"],
                "section_id": body.get("section_id", ""),
            },
            ensure_ascii=False,
        )

    # ── sharing ──────────────────────────────────────────────────────────

    def _access_targets(
        self, chat_ids: Any, user_ids: Any
    ) -> "tuple[str, list[str], str]":
        """Which targets a share names, or the refusal the pair earns.

        Returns ``(kind, ids, refusal)`` where *kind* is ``"chat_ids"`` or
        ``"user_ids"``. Both arrays are Slack's own shape, and neither has a
        singular twin here: one argument per target type, with the cardinality
        that matters checked below rather than expressed by a second name.
        """
        rooms = _string_list(chat_ids)
        people = _string_list(user_ids)
        if rooms is None or people is None:
            return "", [], _canvas_refusal_json(
                "access_targets_malformed",
                "chat_ids and user_ids are each a list of identifiers. One of"
                " them was not a list of strings",
            )
        if rooms and people:
            return "", [], _canvas_refusal_json(
                "access_targets_ambiguous",
                "chat_ids and user_ids cannot both be given: Slack sets access"
                " for conversations or for people, never for both in one call."
                " Make one call for each",
            )
        if not rooms and not people:
            return "", [], _canvas_refusal_json(
                "access_targets_required",
                "no target was named. Pass chat_ids to act on conversations or"
                " user_ids to act on people; exactly one of the two",
            )
        if rooms:
            bad = [room for room in rooms if not _CHAT_ID_RE.match(room)]
            if bad:
                return "", [], _canvas_refusal_json(
                    "chat_id_malformed",
                    f"a Slack conversation id is a C, D or G followed by"
                    f" upper-case letters and digits."
                    f" {', '.join(sorted(bad))[:200]} is not one",
                )
            dms = [room for room in rooms if room.startswith("D")]
            if dms:
                return "", [], _canvas_refusal_json(
                    "chat_id_is_a_direct_message",
                    f"a canvas is not shared with a direct message."
                    f" {', '.join(sorted(dms))} names one. Share it with the"
                    f" people in it by passing user_ids instead",
                )
            return "chat_ids", rooms, ""
        bad_people = [who for who in people if not _USER_ID_RE.match(who)]
        if bad_people:
            return "", [], _canvas_refusal_json(
                "user_id_malformed",
                f"a Slack user id is a U or W followed by upper-case letters"
                f" and digits. {', '.join(sorted(bad_people))[:200]} is not"
                f" one",
            )
        return "user_ids", people, ""

    async def _access_call(
        self, request: _AccessRequest, ids: Sequence[str]
    ) -> dict[str, Any]:
        """One access call for one set of targets."""
        payload: dict[str, Any] = {
            "canvas_id": request.canvas_id,
            _SLACK_TARGET_ARGUMENT[request.kind]: list(ids),
        }
        if request.level:
            payload["access_level"] = request.level
        return await self._api(request.method, payload)

    async def _per_target(
        self, request: _AccessRequest
    ) -> tuple[list[str], list[dict[str, str]], list[str]]:
        """Find out which targets took, one call each.

        Reached only after a call naming several targets failed with a code
        that means *some of them*. Slack's response carries no field saying
        which, so the only way to answer the question is to ask it once per
        target, and setting the same level twice on a target that already took
        is idempotent.

        Returns the three lists a caller needs kept apart: the ones that took,
        the ones that did not and with what, and the ones the deadline reached
        before they were tried. The third exists because reporting an untried
        target as failed would be as wrong as reporting it as granted.
        """
        granted: list[str] = []
        failed: list[dict[str, str]] = []
        undetermined: list[str] = []
        for index, target in enumerate(request.ids):
            if self._remaining() <= 0:
                undetermined.extend(request.ids[index:])
                break
            try:
                await self._access_call(request, [target])
            except _CanvasCallFailure as exc:
                failed.append({"id": target, "error": exc.code})
            else:
                granted.append(target)
        return granted, failed, undetermined

    async def share_slack_canvas(
        self,
        canvas_id: str,
        access_level: str | None = None,
        chat_ids: Any = None,
        user_ids: Any = None,
        remove: bool = False,
    ) -> str:
        """Set or revoke access to a canvas for conversations or for people."""
        metadata = self._runtime_metadata()
        if not metadata:
            return self._no_slack_request_refusal()
        raw_id = str(canvas_id or "").strip()
        refusal = self._canvas_id_refusal(raw_id)
        if refusal:
            return refusal

        level = str(access_level or "").strip().lower()
        if remove and level:
            return _canvas_refusal_json(
                "access_level_not_accepted",
                "remove takes access away entirely, so there is no level to"
                " set alongside it. Pass one or the other",
                canvas_id=raw_id,
            )
        if not remove:
            if not level:
                return _canvas_refusal_json(
                    "access_level_required",
                    f"no access level was given. It is one of"
                    f" {', '.join(ACCESS_LEVELS)}, or pass remove to take"
                    f" access away instead",
                    canvas_id=raw_id,
                )
            if level not in ACCESS_LEVELS:
                return _canvas_refusal_json(
                    "access_level_unknown",
                    f"{level!r} is not an access level. Slack's three are"
                    f" {', '.join(ACCESS_LEVELS)}",
                    canvas_id=raw_id,
                )

        kind, ids, target_refusal = self._access_targets(chat_ids, user_ids)
        if target_refusal:
            return target_refusal

        if level == "owner":
            if kind == "chat_ids":
                return _canvas_refusal_json(
                    "owner_must_be_a_person",
                    "only a person can own a canvas, and chat_ids names"
                    " conversations. Pass the owner as a single entry in"
                    " user_ids",
                    canvas_id=raw_id,
                )
            if len(ids) != 1:
                # Slack does not document refusing this, and it is refused here
                # anyway: a canvas has one owner, so a call naming several is
                # incoherent whatever Slack would do with it, and the outcomes
                # it could have -- the last one wins, the first one wins,
                # several owners -- are not ones a caller could tell apart from
                # the response.
                return _canvas_refusal_json(
                    "owner_is_one_person",
                    f"a canvas has one owner and {len(ids)} were named."
                    f" Ownership transfers to exactly one person; name that"
                    f" person alone and set the others to read or write",
                    canvas_id=raw_id,
                )

        try:
            await self._load_settings(metadata)
        except SlackWorkspaceUnresolved as unresolved:
            return _canvas_refusal_json(
                unresolved.code, unresolved.detail, canvas_id=raw_id
            )

        request = _AccessRequest(
            method=_CANVAS_ACCESS_DELETE if remove else _CANVAS_ACCESS_SET,
            canvas_id=raw_id,
            kind=kind,
            ids=tuple(ids),
            level=level,
        )
        try:
            await self._access_call(request, ids)
        except _CanvasCallFailure as exc:
            if exc.code in _PARTIAL_FAILURE_CODES and len(ids) > 1:
                return await self._partial_result(exc, request)
            return self._slack_refusal(
                exc,
                canvas_id=raw_id,
                **{kind: list(ids)},
                **self._sharing_hint(exc, kind, remove=remove),
            )

        logger.info(
            "slack canvas: %s access for %d %s on %s",
            "revoked" if remove else level,
            len(ids),
            kind,
            raw_id,
        )
        result: dict[str, Any] = {
            "ok": True,
            "canvas_id": raw_id,
            "access_level": request.reported_level,
            "granted": list(ids),
        }
        return json.dumps(result, ensure_ascii=False)

    @staticmethod
    def _sharing_hint(
        exc: _CanvasCallFailure, kind: str, *, remove: bool
    ) -> dict[str, Any]:
        """The missing first step, named where it is the likely cause.

        ``canvases.access.set`` sets a level on a canvas somebody already has;
        it does not hand the canvas over. Slack says that to set a user's
        access "you must have sent the user the canvas directly first", and no
        canvas method does that -- it is a message carrying the permalink, and
        nothing here posts one, because a sharing tool that wrote into a
        conversation would be posting without passing the rail that governs
        posting.

        Attached to ``canvas_not_found`` as well as to the user codes, and for
        a second reason: a *channel* canvas has no access of its own -- who may
        read it follows from who is in the channel -- and Slack answers an
        attempt to set access on one with exactly that code.
        """
        if remove or kind != "user_ids":
            if exc.code == "canvas_not_found":
                return {
                    "next_step": (
                        "If this canvas belongs to a conversation, its access"
                        " follows from who is in that conversation and cannot"
                        " be set here at all."
                    )
                }
            return {}
        if exc.code not in {
            "canvas_not_found",
            "failed_to_update_user_ids",
            "user_not_found",
        }:
            return {}
        return {
            "next_step": (
                "A person's access can only be set on a canvas that has already"
                " been sent to them directly; being in a conversation the"
                " canvas was shared into does not count. Send them the canvas"
                " permalink in a message first, then set the level. If this is"
                " a conversation's own canvas, access follows membership and"
                " cannot be set here at all."
            )
        }

    async def _partial_result(
        self, exc: _CanvasCallFailure, request: _AccessRequest
    ) -> str:
        """Report a multi-target call that may have half succeeded.

        One boolean would be a lie here. Slack refuses the whole call with one
        code and says nothing about which of five targets it applied, so the
        outcome is established per target and reported per target.
        """
        granted, failed, undetermined = await self._per_target(request)
        detail = _FAILURE_DETAIL.get(exc.code, f"Slack refused {exc.where}")
        payload: dict[str, Any] = {
            "ok": False,
            "error": "access_partially_applied" if granted else exc.code,
            "detail": (
                f"{detail}. Slack refuses such a call as a whole and does not"
                f" say which targets it applied, so each was tried on its own"
                f" and the outcome below is per target"
            ),
            "canvas_id": request.canvas_id,
            "access_level": request.reported_level,
            "granted": granted,
            "failed": failed,
        }
        if undetermined:
            payload["undetermined"] = undetermined
            payload["detail"] += (
                "; the call ran out of time before the undetermined targets"
                " were tried, and nothing is claimed about them"
            )
        payload.update(
            self._sharing_hint(exc, request.kind, remove=request.removes)
        )
        logger.warning(
            "slack canvas: access partially applied on %s -- %d granted, %d"
            " failed, %d undetermined",
            request.canvas_id,
            len(granted),
            len(failed),
            len(undetermined),
        )
        return json.dumps(payload, ensure_ascii=False)

    # ── deleting ─────────────────────────────────────────────────────────

    async def delete_slack_canvas(self, canvas_id: str) -> str:
        """Delete one canvas, permanently."""
        metadata = self._runtime_metadata()
        if not metadata:
            return self._no_slack_request_refusal()
        raw_id = str(canvas_id or "").strip()
        refusal = self._canvas_id_refusal(raw_id)
        if refusal:
            return refusal

        try:
            await self._load_settings(metadata)
        except SlackWorkspaceUnresolved as unresolved:
            return _canvas_refusal_json(
                unresolved.code, unresolved.detail, canvas_id=raw_id
            )

        try:
            await self._api(_CANVAS_DELETE, {"canvas_id": raw_id})
        except _CanvasCallFailure as exc:
            return self._slack_refusal(exc, canvas_id=raw_id)

        logger.info("slack canvas: deleted %s", raw_id)
        return json.dumps(
            {"ok": True, "canvas_id": raw_id, "deleted": True},
            ensure_ascii=False,
        )

    # ── cards ────────────────────────────────────────────────────────────

    def get_tools(self) -> list[Tool]:
        """Return the six request-scoped canvas tools."""
        return [
            LocalFunction(card=_read_card(), func=self.read_slack_canvas),
            LocalFunction(card=_write_card(), func=self.write_slack_canvas),
            LocalFunction(
                card=_channel_write_card(), func=self.write_slack_channel_canvas
            ),
            LocalFunction(card=_edit_card(), func=self.edit_slack_canvas),
            LocalFunction(card=_share_card(), func=self.share_slack_canvas),
            LocalFunction(card=_delete_card(), func=self.delete_slack_canvas),
        ]


def _read_card() -> ToolCard:
    """The card for ``read_slack_canvas``.

    Two sentences here are load-bearing and neither is obvious from the
    arguments. Content comes back as HTML while an edit is written as markdown,
    so a read-modify-write is not available and a model that assumed it was
    would send back a mangled document. And the section list arrives only when
    a filter asks for it, because Slack's lookup requires its ``criteria`` and
    has no unfiltered form: a model that expected the sections unasked-for
    would read the result as a canvas that has none.
    """
    return ToolCard(
        name="read_slack_canvas",
        description=(
            "Read a canvas in Slack: what it says, and the identifiers of the "
            "pieces it is made of. A canvas is a document that lives in Slack "
            "-- either on its own or as a tab on a conversation."
            "\nThe content comes back as HTML. It is written as markdown and "
            "served as HTML, and the two are not the same document: you cannot "
            "read a canvas, change the text you got back, and write that in "
            "again. To change a canvas, compose the new markdown yourself."
            "\nEvery element in that HTML carries its section identifier in "
            "its id attribute, and those are the same identifiers listed "
            "separately in the result. So a section you want to edit can be "
            "found in the content without asking again."
            "\nAsking for the section list. It comes back only when a filter "
            "asks for it: section_types to list only sections of named kinds, "
            "contains_text to list only sections holding some text, or both. "
            "Name neither and the content comes back with no section list and "
            "a note saying none was asked for, because Slack has no "
            "unfiltered section lookup. Leaving section_types out while "
            "passing contains_text is how to reach sections of the kinds "
            "Slack does not offer as a filter, which is the only way to find "
            "out those kinds exist."
            "\nThe section list holds identifiers and no text. Slack's section "
            "lookup returns no content of any kind, which is why the document "
            "itself is fetched separately."
            "\nLong canvases. A very long canvas comes back with its beginning "
            "and a note saying how much was left out. Nothing is cut silently."
            "\nWhen a canvas cannot be found. Slack gives one answer for three "
            "situations and does not say which: it does not exist, it was "
            "deleted, or it has never been shared with this app. A canvas this "
            "app did not create stays invisible to it until a person shares "
            "it, and that is the only one of the three anybody can fix."
        ),
        input_params={
            "type": "object",
            "properties": {
                "canvas_id": {
                    "type": "string",
                    "description": (
                        "The canvas to read, such as F1234ABCD. Copy it "
                        "verbatim from a result that reported one."
                    ),
                },
                "section_types": {
                    "type": "array",
                    "items": {"type": "string", "enum": list(SECTION_TYPES)},
                    "description": (
                        "List only sections of these kinds. Leave it out and "
                        "pass contains_text instead to reach sections of the "
                        "kinds that cannot be filtered on."
                    ),
                },
                "contains_text": {
                    "type": "string",
                    "description": (
                        "List only sections holding this text. Combines with "
                        "section_types when both are given."
                    ),
                },
            },
            "required": ["canvas_id"],
        },
    )


def _write_card() -> ToolCard:
    """The card for ``write_slack_canvas``.

    It names ``write_slack_channel_canvas``, and the cross-reference is earned:
    the two make different objects out of the same-looking request, nothing
    converts one into the other afterwards, and a model that picked the wrong
    one has to delete what it made and start again. The six tools are mounted on one decision
    and always arrive together, so naming a sibling describes something the
    model can reach.
    """
    return ToolCard(
        name="write_slack_canvas",
        description=(
            "Create a canvas in Slack -- a document that belongs to this app "
            "rather than to any conversation. Use it for something written "
            "once and read later: a summary, a reference page, a report that "
            "will be updated in place rather than reposted."
            "\nContent is markdown. Headings, lists, tables, quotes, code "
            "blocks and links all work. Block Kit does not."
            "\nWho can see it. Nobody, at first. A canvas made here is visible "
            "to this app alone until it is shared, and creating it notifies "
            "nobody."
            "\nTabbing it into a conversation. Pass chat_id to put the canvas "
            "on a conversation as a tab. That is different from the "
            "conversation's own canvas: use write_slack_channel_canvas for "
            "that one. A canvas tabbed in this way is still a canvas of its "
            "own, and a conversation can hold several."
            "\nOn a free Slack plan chat_id is required rather than optional: "
            "a canvas cannot exist on its own there, and a call without it "
            "fails and says so."
            "\nTitle and content are both optional. Creating an empty canvas "
            "and filling it afterwards is allowed."
            "\nWhat comes back. The new canvas's identifier, which is what "
            "every other canvas call takes."
        ),
        input_params={
            "type": "object",
            "properties": {
                "title": {
                    "type": "string",
                    "description": (
                        "The canvas's title. Optional: leave it out and "
                        "Slack creates the canvas untitled, which is a "
                        "supported thing to ask for. A title can be added "
                        "later with edit_slack_canvas, where it is required "
                        "instead -- there is no way to take a title off a "
                        "canvas once it has one."
                    ),
                },
                "markdown": {
                    "type": "string",
                    "description": (
                        "What the canvas says, in markdown. At most "
                        f"{MAX_MARKDOWN_CHARS} characters."
                    ),
                },
                "chat_id": {
                    "type": "string",
                    "description": (
                        "Put the canvas on this conversation as a tab. "
                        "Optional on a paid plan and required on a free one. "
                        "A direct message cannot hold a canvas tab."
                    ),
                },
            },
            "required": [],
        },
    )


def _channel_write_card() -> ToolCard:
    """The card for ``write_slack_channel_canvas``.

    The one slot is what this card has to get across. A channel holds one of
    these at a time and Slack refuses a second, so a model that made one on a
    guess has filled the slot with something nobody asked for.

    This card used to say the slot could never be freed. That was inferred
    from ``channel_canvas_already_exists`` and from the absence of a
    conversion method, and stated as though Slack documented it; a live
    ``canvases.delete`` on a channel canvas disproved it. What stands here now
    says what is known and marks what is untried, because a card that says
    *cannot* makes a model refuse work it could have done.

    It names its sibling for the reason that sibling names it.
    """
    return ToolCard(
        name="write_slack_channel_canvas",
        description=(
            "Create the canvas that belongs to a Slack channel -- the one "
            "everybody in the channel sees on its Canvas tab, and the one the "
            "channel uses for its standing notes: what this channel is for, "
            "who to ask, the links people keep re-posting."
            "\nA channel has one of these at a time. If the channel already "
            "has one this call fails, and the way to change what it says is "
            "to edit it. delete_slack_canvas does delete a channel canvas, "
            "but that cannot be undone and nobody has tried whether a channel "
            "accepts a new one afterwards, so deleting to start again may "
            "leave the channel with none. Create one only when somebody asked "
            "for the channel's canvas, never on a guess."
            "\nIt is not the same thing as a canvas tabbed into a channel. "
            "write_slack_canvas makes a canvas of its own that can be put on a "
            "channel as a tab, of which a channel may have several. Nothing "
            "converts one kind into the other."
            "\nWho can see it. Everybody in the channel, and only them. Access "
            "follows channel membership and cannot be granted or revoked "
            "separately."
            "\nContent is markdown. Headings, lists, tables, quotes, code "
            "blocks and links all work. Block Kit does not. Title and content "
            "are both optional."
            "\nOnly a channel. A direct message has no canvas of its own."
        ),
        input_params={
            "type": "object",
            "properties": {
                "chat_id": {
                    "type": "string",
                    "description": (
                        "The channel whose canvas this is. It must be a "
                        "channel; a direct message has no canvas of its own."
                    ),
                },
                "title": {
                    "type": "string",
                    "description": (
                        "The canvas's title. Optional: leave it out and "
                        "Slack creates the canvas untitled, which is a "
                        "supported thing to ask for. A title can be added "
                        "later with edit_slack_canvas, where it is required "
                        "instead -- there is no way to take a title off a "
                        "canvas once it has one."
                    ),
                },
                "markdown": {
                    "type": "string",
                    "description": (
                        "What the canvas says, in markdown. At most "
                        f"{MAX_MARKDOWN_CHARS} characters."
                    ),
                },
            },
            "required": ["chat_id"],
        },
    )


def _edit_card() -> ToolCard:
    """The card for ``edit_slack_canvas``.

    Three things have to be said. ``replace`` without a section id replaces the
    whole canvas, which is the one way to destroy a document by omitting an
    argument. One change per call is Slack's rule rather than this tool's, and
    saying so stops a model retrying the same batch. And a concurrent edit is
    rejected rather than queued, so a retry is the remedy for exactly one of
    these failures and for none of the others.
    """
    return ToolCard(
        name="edit_slack_canvas",
        description=(
            "Change what a canvas says: add a piece, replace a piece, remove a "
            "piece, or rename the whole thing."
            "\nOne change per call. Slack applies a single operation per call, "
            "so a rewrite of several sections is several calls, made in the "
            "order the changes should take effect. Passing more than one is "
            "refused rather than partly applied."
            "\nWhere a change goes. insert_after and insert_before need the "
            "identifier of a section to sit beside; insert_at_start and "
            "insert_at_end need none; delete needs the section to remove. "
            "Section identifiers come from reading the canvas."
            "\nreplace behaves differently depending on one argument. With a "
            "section_id it replaces that section. Without one it replaces the "
            "entire canvas, and what was there is gone. Pass the section_id "
            "unless replacing everything is what was asked for."
            "\nrename changes the title and touches no content. The new "
            "title goes in title, inside the change -- the same argument name "
            "the canvas was created with, read one way: a rename needs a "
            "title and a blank one is refused, where a create tool takes a "
            "blank title as a request for an untitled canvas."
            "\nContent is markdown, and it is written rather than patched: "
            "pass the new text of the piece, not a description of what to "
            "change. Slack refuses markdown it cannot parse and names the line "
            "and the construct, which comes back unchanged."
            "\nWhen somebody else is editing. Slack rejects a change made "
            "while another edit is in progress rather than waiting for it. "
            "Nothing was applied, and trying again after a moment is the right "
            "response -- it is the one failure here that a retry fixes."
        ),
        input_params={
            "type": "object",
            "properties": {
                "canvas_id": {
                    "type": "string",
                    "description": (
                        "The canvas to change, such as F1234ABCD."
                    ),
                },
                "changes": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "operation": {
                                "type": "string",
                                "enum": sorted(_EDIT_OPERATIONS),
                                "description": "What to do.",
                            },
                            "section_id": {
                                "type": "string",
                                "description": (
                                    "The section to act on. Required for "
                                    "insert_after, insert_before and delete. "
                                    "Optional for replace, where leaving it "
                                    "out replaces the whole canvas. Not "
                                    "accepted by the others."
                                ),
                            },
                            "markdown": {
                                "type": "string",
                                "description": (
                                    "The new content, for every operation "
                                    "except delete and rename."
                                ),
                            },
                            "title": {
                                "type": "string",
                                "description": (
                                    "The new title, for rename. Required "
                                    "there and refused blank: renaming a "
                                    "canvas to nothing is not something "
                                    "Slack can be asked to do, so it is "
                                    "declined here rather than attempted. "
                                    "The create tools take the same argument "
                                    "and do accept a blank one, because "
                                    "leaving it out there asks for an "
                                    "untitled canvas. At most "
                                    f"{MAX_MARKDOWN_CHARS} characters, the "
                                    "limit Slack sets on the content of one "
                                    "change."
                                ),
                            },
                        },
                        "required": ["operation"],
                    },
                    "description": (
                        "The change to apply, as a list holding exactly one "
                        "change. Slack accepts one operation per call."
                    ),
                },
            },
            "required": ["canvas_id", "changes"],
        },
    )


def _share_card() -> ToolCard:
    """The card for ``share_slack_canvas``.

    The precondition is the reason this card is long. ``canvases.access.set``
    reads like a sharing call and is not one: it adjusts a level on a canvas
    somebody already has, and Slack requires the canvas to have been sent to a
    person directly before their level can be set at all. Nothing here performs
    that step, so a model has to know what the missing move is; without the
    sentence it would call this again with different arguments.
    """
    return ToolCard(
        name="share_slack_canvas",
        description=(
            "Set who may read or write a canvas, or take that access away."
            "\nThis does not hand anybody the canvas. It adjusts the level of "
            "access on a canvas somebody can already reach. For a person in "
            "particular, Slack requires that the canvas has already been sent "
            "to them directly -- in a message, by its link -- before their "
            "level can be set at all; being in a conversation where the canvas "
            "was shared does not count. If that has not happened, send them "
            "the canvas link first. This tool will not send it."
            "\nConversations or people, never both in one call. Pass chat_ids "
            "to act on conversations, or user_ids to act on people. Both are "
            "lists. Make two calls when both are needed."
            "\nThe levels are read, write and owner. owner transfers ownership "
            "and applies to exactly one person: it cannot be given to a "
            "conversation, and it cannot be given to several people at once."
            "\nTaking access away. Pass remove instead of a level, with the "
            "same targets."
            "\nA conversation's own canvas has no access to set. Who may read "
            "it follows from who is in the conversation, and a call naming one "
            "fails."
            "\nWhen some targets take and others do not. Slack refuses such a "
            "call as a whole and does not say which of them applied, so each "
            "target is then tried on its own and the result lists them "
            "separately: which took, which did not and why, and which were "
            "never determined. Read the lists rather than the overall result."
        ),
        input_params={
            "type": "object",
            "properties": {
                "canvas_id": {
                    "type": "string",
                    "description": (
                        "The canvas whose access is being set, such as "
                        "F1234ABCD."
                    ),
                },
                "access_level": {
                    "type": "string",
                    "enum": list(ACCESS_LEVELS),
                    "description": (
                        "The level to set. owner applies to one person only. "
                        "Leave it out and pass remove to take access away."
                    ),
                },
                "chat_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "The conversations to set access for. Cannot be given "
                        "together with user_ids."
                    ),
                },
                "user_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "The people to set access for. Cannot be given "
                        "together with chat_ids. Each of them must already "
                        "have been sent the canvas directly."
                    ),
                },
                "remove": {
                    "type": "boolean",
                    "default": False,
                    "description": (
                        "Take access away from the named targets instead of "
                        "setting a level. Pass no access_level with it."
                    ),
                },
            },
            "required": ["canvas_id"],
        },
    )


def _delete_card() -> ToolCard:
    """The card for ``delete_slack_canvas``.

    A tool of its own rather than a flag on another, because
    ``permissions.tools`` is keyed by name: one tool is one policy, and folding
    deletion into a writing tool would make *may write a canvas, may not delete
    one* inexpressible.
    """
    return ToolCard(
        name="delete_slack_canvas",
        description=(
            "Delete a canvas in Slack."
            "\nIt cannot be undone. Slack says of this method: \"Once a canvas "
            "is deleted, there is no way to get it back\". Whatever the canvas "
            "held is gone, along with every link anybody has to it, and "
            "nobody is told. Delete a canvas when somebody asked for it to be "
            "deleted."
            "\nThis deletes a conversation's own canvas as well. Slack "
            "documents no exception for one and a channel canvas has been "
            "deleted this way. A channel holds one at a time, and nobody has "
            "tried whether it accepts a new one afterwards, so a channel left "
            "without its canvas may stay that way. Editing it empty is the "
            "way to clear one that can be undone."
            "\nDeleting can be switched off for a whole workspace, in which "
            "case this fails and says so, and nothing but an administrator "
            "change will alter that."
        ),
        input_params={
            "type": "object",
            "properties": {
                "canvas_id": {
                    "type": "string",
                    "description": (
                        "The canvas to delete, such as F1234ABCD."
                    ),
                },
            },
            "required": ["canvas_id"],
        },
    )


__all__ = [
    "ACCESS_LEVELS",
    "CANVAS_TOOL_NAMES",
    "MAX_CANVAS_BYTES",
    "MAX_CONTENT_CHARS",
    "MAX_MARKDOWN_CHARS",
    "MAX_SECTIONS",
    "SECTION_TYPES",
    "SlackCanvasToolkit",
    "slack_canvas_request_metadata",
]
