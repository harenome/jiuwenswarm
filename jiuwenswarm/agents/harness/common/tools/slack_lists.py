# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Read, write, share and delete Slack Lists.

A Slack List is a table that lives in Slack: named columns, rows of typed
cells, and a row that may itself hold rows. It is a file -- its identifier is
an ``F`` identifier, the same family a canvas and an upload carry -- which is
why deleting one is the one operation in this module that is not a List method
at all.

**The family is ``slackLists.*``, not ``lists.*``.** Twelve methods, every one
of them spelled with the capital L: ``slackLists.create``,
``slackLists.update``, ``slackLists.access.set``, ``slackLists.access.delete``,
``slackLists.items.create``, ``slackLists.items.info``,
``slackLists.items.list``, ``slackLists.items.update``,
``slackLists.items.delete``, ``slackLists.items.deleteMultiple``,
``slackLists.download.start`` and ``slackLists.download.get``. The
documentation's own navigation says "lists", and no method is spelled that way.

**There is no ``slackLists.info``, no ``slackLists.list`` and no
``slackLists.delete``.** Each consequence is a shape in this module rather than
a note about one:

* *No info method.* ``slackLists.items.info`` is the one that answers with the
  List itself. Fetching any single item returns the whole parent object -- the
  column schema, every view with its per-column visibility and ordering, the
  subtask schema, the limits, and the export URL -- so the way to learn what a
  List *is* runs through one of its rows. ``slackLists.items.list`` reaches the
  same object by a different door, ``include_list``, which is off by default.
* *No list-of-lists method.* Nothing here discovers a List. A ``list_id``
  arrives from somebody who already had it.
* *No delete method.* Deletion goes through ``files.delete``, which deletes any
  file at all and which this repository calls nowhere else. That is the reason
  for the guard below.

**``delete_slack_list`` reads before it deletes, and the read is the tool.**
``files.delete`` takes a file id and asks no questions: handed a canvas, an
upload or somebody's screenshot it destroys that instead, permanently and
silently, and the refusal a caller would get for a wrong id is no refusal at
all but a successful deletion of the wrong thing. So the file is fetched first
and its ``filetype`` must be ``list``. Without that check this tool would be a
delete-any-file capability wearing a List's name, and an operator who allowed
``delete_slack_list`` in ``permissions.tools`` would have allowed something
much wider than the name says.

**A bot cannot change a List's schema after it is created.**
``slackLists.update`` covers the name, the description and ``todo_mode``, and
nothing else. No method adds a column, removes one, renames one or changes its
type. The columns a List will ever have are decided by the ``schema`` passed to
``slackLists.create``, which makes creation the one irreversible decision in
this module and is why the write card says so rather than leaving a model to
find out. One hint points the other way and is deliberately not built on:
``column_id_to_create`` is named in ``slackLists.items.update``'s error table --
*"The ``column_id`` or ``column_id_to_create`` field must be provided"* -- and
appears in no arguments table and in no example. It is recorded here as
unprobed. Nothing in this module sends it, and nothing in this module claims it
does not work.

**There is no server-side query.** ``slackLists.items.list`` takes ``limit``,
``cursor``, ``archived`` and ``include_list``, and that is the whole of it: no
filter, no sort, no column predicate. Finding the rows where one column has one
value means paging the entire List and filtering afterwards. The card says so,
because a model that expected a ``where`` would otherwise conclude the argument
was missing rather than absent.

**A text cell cannot be written as text.** The reference this repository
transcribed for :mod:`jiuwenswarm.common.slack_rich_text_render` is explicit:
*"While a bit counterintuitive, you must use rich_text blocks in a request that
includes plain text… You may see the text property appear in a response as a
fallback, but it is not accepted in the request payload."* The method pages
carry no such sentence, and they do not need to: every worked example on
``slackLists.items.create`` and ``slackLists.items.update`` writes a text cell
under a key named ``rich_text``, and no example anywhere writes one under
``text``. The value is an array holding a ``rich_text`` *block* --
``[{"type": "rich_text", "elements": [...]}]`` -- rather than a bare elements
array. The renderer builds the elements and this module wraps them in that
block, which is what ``rich_text_block`` already produces.

**Every cell value travels unread, and the card says what to write anyway.**
``slackLists.items.create`` carries a *Field types* section with a worked cell
for fifteen value keys, so those shapes are transcribed rather than guessed at
and :data:`CELL_VALUE_SHAPES` holds them. Nine column types appear in a schema
example and in no cell example -- ``multi_select``, ``vote``, ``canvas``,
``assignee``, ``due_date``, ``completed`` and the three ``todo_`` types
``todo_mode`` adds -- and those are named as undocumented rather than filled in
from a neighbour that looks similar. Documented or not, a cell's keys are
forwarded exactly as they were written: nothing here wraps a scalar in an array
or renames a key, because a tool that accepts what Slack refuses teaches the
wrong shape for next time, and Slack's per-type refusal is passed back by name.

**A ``column_id`` is Slack's id, not the caller's key.** The ``schema`` handed
to ``slackLists.create`` names each column with a ``key`` the caller chose, and
Slack answers with the same column carrying a generated ``id`` -- ``Col``
followed by letters and digits. Every write takes the ``id``. Passing the key
back is refused with ``invalid_arguments``, which names no argument at all, so
the three cards that mention a column id say which of the two it means and
where to read it: ``read_slack_list`` with ``include_list`` folds the schema in
beside the rows.

Both spellings are offered and neither is required. ``text`` is Markdown and is
rendered; ``rich_text`` is the payload itself and is forwarded untouched. The
renderer does not cover everything Markdown can say, and what it drops is
listed in its module docstring and repeated in the edit card, because a caller
who cannot predict what survives has been handed a silent reduction.

**Two partial failures, reported as what is known rather than as a boolean.**

* ``slackLists.access.set`` naming several targets can fail for some of them.
  ``failed_to_update_user_ids`` is a documented *error code* and names no ids,
  and no response field carries them either, so a call that refused five
  targets says only that it refused. The remedy is the canvas toolkit's: one
  call per target, and three lists in the result -- which took, which did not
  and with what, and which the deadline reached before they were tried.
  Reporting an untried target as failed would be as wrong as reporting it
  granted.
* ``slackLists.items.deleteMultiple`` documents no per-record error of any
  kind. Its error table carries ``list_not_found`` and the standard auth and
  rate-limit set and nothing about a record, so whether a refusal means *none
  of them* or *some of them* is not established anywhere. The card says that
  in those words rather than implying all-or-nothing, and no per-target pass is
  invented for it: a delete is not idempotent, and retrying one to find out
  whether the first attempt took would be a second deletion.

**An export is fetched rather than handed over as a URL.**
``slackLists.download.get`` answers with a ``download_url`` on
``files.slack.com`` that is unreadable without this app's bot token. Returning
it alone was an instruction to go and get it some other way, and a model given
one did exactly that -- it shelled out with ``curl``, which puts a live
workspace credential into a command line and fetches a URL past every check
this repository wrote for the purpose. So the bytes are pulled here, through
``jiuwenswarm.common.slack_file_transfer``: DNS-pinned resolution with every
address validated, each redirect hop facing the checks the first URL faced, a
transport that refuses an unpinned connection, and a byte ceiling. It is the
path ``read_slack_canvas`` already downloads through, and a second copy of that
reasoning would be a second security boundary to keep in step.

The URL is returned as well, because a caller may want the link rather than the
content and because an export can outgrow what a tool result may carry. Two
caps bound it -- :data:`MAX_EXPORT_BYTES` on what is pulled down and
:data:`MAX_EXPORT_CHARS` on what is returned -- and when either bites the
result says so by name and reports both counts, because a CSV that looks whole
and is not would be read as the whole List.

**Almost nothing here has been exercised against a real workspace.** Every
argument name, enum value, error code and limit is taken from
``https://docs.slack.dev/reference/methods/``. Two consequences are worth the
sentence: the export polling below reports Slack's ``status`` string verbatim
rather than matching against values nobody wrote down, and the arity check on
``owner`` is this module's own rule rather than a documented refusal. The
exception is one session against a live workspace, which is where the column id
rule, the value shapes and the export host above come from; each of those says
so where it is written, and nothing else here rests on it.

Only cross-cutting primitives are borrowed, and from the modules that own them.
``slack_history`` holds the reading of a Slack response, the rule for when a
refusal is worth retrying, the pass that keeps a credential out of an error
code, and the per-request choice of workspace. ``slack_scope_policy`` holds the
one spelling of a method name: ten of the twelve are two dots deep, so the
first-underscore rule would turn ``slackLists_items_list`` into
``slackLists.items_list``, which is not a method an operator can look up.
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
from jiuwenswarm.common.slack_rich_text_render import rich_text_block
from jiuwenswarm.common.slack_scope_policy import dotted


logger = logging.getLogger(__name__)

#: The five tool names, in the order the cards are built. Nothing reads it: the
#: registration mounts the built tools and takes each one's own ``name``, and
#: each card spells its name itself. It stays exported as this module's
#: statement of the set -- the five names ``permissions.tools`` is written with
#: -- readable without building the toolkit to get them.
LIST_TOOL_NAMES: tuple[str, ...] = (
    "read_slack_list",
    "write_slack_list",
    "edit_slack_list",
    "share_slack_list",
    "delete_slack_list",
)

#: The channel id the cron scheduler delivers a scheduled job under. Spelled
#: here rather than imported from the gateway: a harness tool must not reach
#: into the gateway, which is why the policy module holds the other two names
#: the request predicate reads.
_CRON_REQUEST_CHANNEL_ID = "__cron__"

# ── the methods, in Slack's own spelling ─────────────────────────────────────
#
# Written dotted and sent through ``api_call`` rather than through an SDK
# wrapper. The pinned SDK has no wrapper for any of them, and six take an array
# -- ``cells``, ``initial_fields``, ``schema``, ``ids``, ``user_ids``,
# ``channel_ids`` -- for which a JSON body is the one encoding where an array is
# unambiguously an array.

_LIST_CREATE = "slackLists.create"
_LIST_UPDATE = "slackLists.update"
_LIST_ACCESS_SET = "slackLists.access.set"
_LIST_ACCESS_DELETE = "slackLists.access.delete"
_ITEMS_CREATE = "slackLists.items.create"
_ITEMS_INFO = "slackLists.items.info"
_ITEMS_LIST = "slackLists.items.list"
_ITEMS_UPDATE = "slackLists.items.update"
_ITEMS_DELETE = "slackLists.items.delete"
_ITEMS_DELETE_MULTIPLE = "slackLists.items.deleteMultiple"
_DOWNLOAD_START = "slackLists.download.start"
_DOWNLOAD_GET = "slackLists.download.get"

#: The two methods here that are not List methods. ``files.info`` is the guard
#: in front of ``files.delete``, and both have an SDK wrapper.
_FILES_INFO = "files_info"
_FILES_DELETE = "files_delete"

#: The two scopes this module spends, named in a refusal because the code alone
#: tells nobody what to go and grant.
_LISTS_READ_SCOPE = "lists:read"
_LISTS_WRITE_SCOPE = "lists:write"

#: The methods that read. Everything else here writes, which is what decides
#: which scope a ``missing_scope`` refusal names.
_READ_METHODS = frozenset(
    {_ITEMS_INFO, _ITEMS_LIST, _DOWNLOAD_START, _DOWNLOAD_GET}
)

#: The three levels ``slackLists.access.set`` accepts, in Slack's own words.
ACCESS_LEVELS: tuple[str, ...] = ("read", "write", "owner")

#: The two export formats. ``csv`` is Slack's default "for backward
#: compatibility"; ``json`` is documented as "a complete, hierarchical export"
#: and is the only one that honours ``include_threads`` and
#: ``include_attachments``.
EXPORT_FORMATS: tuple[str, ...] = ("csv", "json")

#: The column types Slack's own ``slackLists.create`` example writes out, in its
#: order, with the three ``todo_mode`` adds after them.
#:
#: Named in the create card and enforced nowhere. It is a reading of one worked
#: example rather than a published enumeration, so a type Slack adds tomorrow
#: would be absent from it; refusing a schema on the strength of that would
#: refuse a column this module has no business having an opinion about. The
#: card lists them because a model composing a schema from nothing would
#: otherwise invent type names, and a list that helps is worth having even
#: where a gate would not be.
COLUMN_TYPES: tuple[str, ...] = (
    "text",
    "rich_text",
    "message",
    "number",
    "select",
    "multi_select",
    "date",
    "user",
    "attachment",
    "checkbox",
    "email",
    "phone",
    "channel",
    "rating",
    "vote",
    "assignee",
    "due_date",
    "completed",
    "canvas",
    "link",
    "todo_completed",
    "todo_assignee",
    "todo_due_date",
)

#: The value each documented cell key takes, transcribed from the *Field
#: types* section of ``slackLists.items.create``. Keyed by the value key, which
#: is the column type's own name everywhere but one: a ``text`` column takes
#: ``rich_text``, which Slack says in those words.
#:
#: Every one of them but ``checkbox`` puts its value in an array, including the
#: ones whose column holds a single thing. That is the shape a caller guesses
#: wrong, and it is worth saying in a card rather than leaving to a refusal
#: that names no argument.
CELL_VALUE_SHAPES: Mapping[str, str] = {
    "rich_text": (
        "an array holding one rich_text block, never a plain string --"
        " write the value as Markdown in text instead and this builds it"
    ),
    "select": (
        "an array of option ids. An option id is a choice's value in the"
        " column's schema, not the label printed beside it"
    ),
    "user": "an array of user ids, each a U or a W",
    "date": (
        "an array of one YYYY-MM-DD string. Not a Unix time, and not a bare"
        " string outside an array"
    ),
    "checkbox": (
        "a bare true or false. The one documented value that is not in an"
        " array"
    ),
    "number": "an array of numbers",
    "rating": "an array of one number",
    "email": "an array of email addresses",
    "phone": "an array of phone numbers, written as text",
    "channel": "an array of conversation ids, each a C",
    "message": "an array of message permalinks",
    "attachment": "an array of file ids, each an F",
    "link": (
        "an array of objects, each with original_url and optionally"
        " display_name and display_as_url"
    ),
    "timestamp": "an array of one Unix time in seconds",
    "reference": (
        'an array of objects shaped {"file": {"file_id": "F1234ABCD"}}'
    ),
}

#: Column types Slack lists in a schema example and shows no cell for.
#:
#: Named rather than guessed at. ``assignee`` probably takes ``user``,
#: ``completed`` probably takes ``checkbox`` and ``multi_select`` probably
#: takes ``select`` -- and *probably* is not what a card a model writes from
#: should contain. Slack says ``initial_fields`` supports every column type, so
#: these are writable; only their key is unpublished. The card says so, the
#: value travels unchanged, and Slack answers.
UNDOCUMENTED_CELL_TYPES: tuple[str, ...] = (
    "multi_select",
    "vote",
    "canvas",
    "assignee",
    "due_date",
    "completed",
    "todo_completed",
    "todo_assignee",
    "todo_due_date",
)

#: The five things ``edit_slack_list`` does, each naming the method it sends.
#: One tool rather than five, because splitting at item level would give an
#: operator five names to allow where the distinction that matters -- writing a
#: List at all -- is one.
EDIT_OPERATIONS: Mapping[str, str] = {
    "create_item": _ITEMS_CREATE,
    "update_cells": _ITEMS_UPDATE,
    "delete_item": _ITEMS_DELETE,
    "delete_items": _ITEMS_DELETE_MULTIPLE,
    "update_list": _LIST_UPDATE,
}

#: Which of ``edit_slack_list``'s arguments belong to which operation. An
#: argument given for the wrong operation is refused by name rather than
#: dropped: a model that passed ``todo_mode`` to a row deletion meant something,
#: and silently ignoring it would report success for a call that did not do what
#: was asked.
_EDIT_ARGUMENTS: Mapping[str, tuple[str, ...]] = {
    "create_item": ("initial_fields", "duplicated_item_id", "parent_item_id"),
    "update_cells": ("cells",),
    "delete_item": ("item_id",),
    "delete_items": ("item_ids",),
    "update_list": ("name", "description", "description_blocks", "todo_mode"),
}

#: A Slack List is a file, and a file id is ``F`` followed by upper-case
#: alphanumerics. Checked before the value travels into an API argument,
#: because it reaches this tool from a model that read it out of somewhere else.
#: Kept deliberately loose on length: Slack has lengthened an id family before,
#: and a bound invented here would refuse a real List.
_LIST_ID_RE = re.compile(r"\AF[A-Z0-9]{2,}\Z")

#: A Slack conversation id. ``D`` is a direct message and is refused by name;
#: ``C`` and ``G`` are not told apart here, because a private channel and a
#: group direct message share the ``G`` family and Slack is the only thing that
#: can say which of them an id is.
_CHAT_ID_RE = re.compile(r"\A[CDG][A-Z0-9]{2,}\Z")

#: A Slack user id.
_USER_ID_RE = re.compile(r"\A[UW][A-Z0-9]{2,}\Z")

#: How much of a Slack-written ``detail`` string is echoed back. It is text
#: Slack wrote rather than a value this module composed, and it is about to be
#: echoed into a result that is persisted.
_MAX_DETAIL_CHARS = 1000

#: How many rows one listing returns. Ours rather than Slack's, which publishes
#: no ceiling on ``limit``: a tool result is read by a model with a finite
#: context. A page that hits it says so and carries the cursor to continue with,
#: because a silent stop would look like the end of the List.
MAX_ITEMS_PER_PAGE = 200

#: Wall clock for one tool call, retries included. Generous because two paths
#: make more than one Slack call: a share that has to find out which target
#: failed makes one per target, and an export starts a job and then asks
#: whether it is ready.
_TIMEOUT_SECONDS = 60.0

#: How many times one call will wait out a rate limit before giving up.
_MAX_RATE_LIMIT_RETRIES = 2

#: How many times an export is asked whether it is finished before the job id is
#: handed back instead. Small on purpose: Slack documents no status vocabulary
#: and no expected duration, so a longer wait would be this module guessing at
#: both, and the job id is a complete answer -- the caller asks again with it.
_MAX_EXPORT_POLLS = 3

#: Seconds between those asks.
_EXPORT_POLL_SECONDS = 1.0

#: How many bytes of an export are worth pulling down. Ours rather than
#: Slack's, which publishes no ceiling on an export at all: a List may hold
#: thousands of rows with a thread and a set of attachments named on each, and
#: streaming an unbounded document into memory to throw most of it away serves
#: nobody. Matched to the canvas toolkit's figure, which answers the same
#: question about the same kind of fetch.
MAX_EXPORT_BYTES = 4 * 1024 * 1024

#: How many characters of it are returned. Also ours, and also not a platform
#: limit: a tool result is read by a model with a finite context and an export
#: is the one call here that can exceed it in a single answer. When the cap
#: bites the result says so by name and reports both counts, and the
#: download_url is still there for whatever wants the whole file.
MAX_EXPORT_CHARS = 60_000

#: The one host an export is fetched from. Narrower than the canvas toolkit's
#: pair on purpose: a canvas is served from the workspace's own host, so that
#: toolkit pays for an ``auth.test`` to learn it, while an export observed in
#: testing arrives on ``files.slack.com`` -- the host this repository already
#: documents as the only one Slack serves its own uploads from. A URL naming
#: anywhere else is refused by name rather than followed, which is a widening
#: somebody decides on rather than one that happens.
_EXPORT_HOSTS = frozenset({SLACK_FILE_HOST})

#: What a refusal means, for the codes where the code alone is not actionable.
#:
#: One table for all five tools. A code means the same thing whichever method
#: raised it, and two tables would be two chances to explain one code two ways.
_FAILURE_DETAIL: Mapping[str, str] = {
    "list_not_found": (
        "no List with this id is visible to this app. Slack gives the same"
        " answer whether it never existed, has been deleted, or exists and has"
        " never been shared with this app, and only the last is fixable -- by a"
        " person, not by a retry"
    ),
    "record_not_found": (
        "this List has no item with that id. An item id is taken unchanged from"
        " a listing of this List, and an id from a different List names nothing"
        " here"
    ),
    "record_deleted": (
        "this item has been deleted. Slack keeps no undelete, so it cannot be"
        " read or changed again"
    ),
    "row_not_found": (
        "this List has no row with that id. A row id is taken unchanged from a"
        " listing of this List"
    ),
    "invalid_row_id": (
        "one of the cells names a row id Slack will not accept. A row id is"
        " taken unchanged from a listing of this List"
    ),
    "row_id_not_provided": (
        "one of the cells names neither a row to change nor a row to create."
        " Give the cell a row_id, or row_id_to_create set to true"
    ),
    "invalid_column_id": (
        "one of the cells names a column id this List does not have. Column ids"
        " come from the List's schema, which a read of any one item returns"
    ),
    "column_not_found": (
        "one of the cells names a column that is not in this List. Column ids"
        " come from the List's schema, which a read of any one item returns"
    ),
    "column_id_not_provided": (
        "one of the cells names no column at all. Every cell needs a column_id"
    ),
    "uneditable_column": (
        "this column cannot be written by this app. Some columns are computed"
        " or managed by Slack, and no argument to this tool makes one writable"
    ),
    "invalid_input_type": (
        "a cell's value is the wrong shape for its column's type. Each column"
        " type takes its own key -- a text column takes rich_text, a select"
        " column takes select, a person column takes user, and so on -- and the"
        " List's schema says which type each column is"
    ),
    "invalid_option_id": (
        "a select cell names an option this column does not offer. The options"
        " are in the column's schema, and this app cannot add one"
    ),
    "invalid_vote_value": "a vote cell's value is not one this column accepts",
    "invalid_date": (
        "a date cell's value is not a date Slack accepts. Its documented"
        " example is an array holding one YYYY-MM-DD string"
    ),
    "invalid_email": "an email cell's value is not an email address",
    "invalid_phone_number": "a phone cell's value is not a phone number",
    "invalid_link": "a link cell's value is not a URL Slack will accept",
    "invalid_message": (
        "a message cell's value does not name a Slack message this app can see"
    ),
    "invalid_blocks": (
        "a cell's rich_text payload is not Block Kit Slack will accept. A cell"
        " carries an array holding a rich_text block, not a bare elements array"
        " and not a plain string"
    ),
    "invalid_text_block": (
        "a text cell's rich_text payload is not a shape Slack will accept. A"
        " text cell carries an array holding a rich_text block; plain text is"
        " not accepted in a request whatever a response shows"
    ),
    "invalid_attachment": "an attachment cell's value is not one Slack accepts",
    "invalid_schema": (
        "the schema is not a column definition Slack will accept. Nothing was"
        " created"
    ),
    "invalid_column_type": (
        "the schema names a column type Slack does not have. Nothing was"
        " created"
    ),
    "invalid_primary_column": (
        "the schema's primary column is not one Slack will accept as the"
        " primary. Nothing was created"
    ),
    "invalid_copy_and_schema_args": (
        "a copy and a schema cannot both be given: copying a List takes the"
        " copied List's columns, so there is nothing for a schema to decide."
        " Pass one or the other"
    ),
    "missing_arg_copy_from_list_id": (
        "include_copied_list_records says what to do with the records of a List"
        " being copied, and no List was named to copy. Pass copy_from_list_id"
    ),
    "unexpected_description_blocks_arg": (
        "Slack refused the description payload on this call"
    ),
    "duplicated_item_not_found": (
        "the item to copy is not one this app can see. An item id is taken"
        " unchanged from a listing, and one from a different List names nothing"
        " here"
    ),
    "over_row_maximum": (
        "this List already holds as many rows as Slack allows. Nothing was"
        " added, and a retry will fail the same way -- delete rows, or use"
        " another List"
    ),
    "over_column_maximum": (
        "the schema asks for more columns than Slack allows in one List."
        " Nothing was created"
    ),
    "over_cell_fields_limit": (
        "this call carries more cell values than Slack accepts at once."
        " Nothing was written. Split the cells across several calls"
    ),
    "over_list_file_maximum": (
        "this workspace already holds as many Lists as Slack allows. Nothing"
        " was created, and only deleting a List makes room"
    ),
    "over_title_length_maximum": (
        "the name is longer than Slack allows. Nothing was written"
    ),
    "archive_not_supported": (
        "this List does not keep archived items, so there is no archived page"
        " to return"
    ),
    "invalid_cursor": (
        "this cursor is not one Slack will accept any more. Start the listing"
        " again from the first page"
    ),
    "job_not_found": (
        "no export job with this id. A job id is returned by the call that"
        " starts an export, and Slack does not keep one indefinitely -- start"
        " the export again"
    ),
    "permission_denied": (
        "this app is not allowed to do this to this List. Being able to see a"
        " List is not being able to change it, and the level a List grants this"
        " app is set by a person rather than by any argument here"
    ),
    "lists_disabled_user_team": (
        "Lists are switched off for this workspace. Nothing this tool can be"
        " passed turns them back on; an administrator has to"
    ),
    "unknown_method": (
        "Slack answers this with \"Feature not enabled for this team\". Lists"
        " are a paid-plan feature, and on a workspace without them the method"
        " itself does not exist. No argument changes that"
    ),
    "restricted_action": (
        "this workspace's own rules refuse this action. It is an administrator"
        " setting rather than a missing scope, so reinstalling the app will not"
        " change it"
    ),
    "invalid_parameters": (
        "Slack read the targets as neither one thing nor the other. Exactly one"
        " of chat_ids and user_ids may be given, and one of them must be"
    ),
    "failed_to_update_user_ids": (
        "Slack refused the change for at least one of the named users and does"
        " not say which"
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
    "file_not_found": (
        "no file with this id is visible to this app. A List this app did not"
        " create stays invisible to it until somebody shares it"
    ),
    "file_deleted": "this file has already been deleted",
    "file_deleting_disabled": (
        "deleting a file is switched off in this workspace. The List is"
        " untouched and a retry will not help"
    ),
    "cant_delete_file": (
        "this app is not allowed to delete this file. That is a permission a"
        " person grants rather than an argument this tool takes"
    ),
    "delete_not_allowed": (
        "Slack refuses to delete this file at all. Its documented example is a"
        " canvas template; whatever the reason, nothing was deleted"
    ),
    "not_allowed_token_type": (
        "this method declines the kind of token this app holds"
    ),
    "invalid_arguments": (
        "Slack rejected the call's arguments and names none of them. Every"
        " value it saw is one this tool was given and forwarded unchanged"
    ),
    "export_host_not_allowed": (
        "Slack named a download host this app will not send its token to."
        " Exports are fetched from files.slack.com and nowhere else, and a"
        " host outside that is a widening for a person to decide on"
    ),
    "export_read_denied": (
        "Slack refused the download of a finished export. The job completed,"
        " so this is about the token rather than about the export"
    ),
    "export_download_failed": (
        "the export finished and downloading it did not. The job id stays"
        " valid, so asking again with it is worth one attempt"
    ),
}

#: The two methods that write cells. ``invalid_arguments`` reaching one of them
#: is about a cell, which is what makes a sentence about cells safe to attach
#: to a code Slack answers any malformed call at all with.
_CELL_WRITE_METHODS = frozenset({_ITEMS_CREATE, _ITEMS_UPDATE})

#: What to check when Slack refuses a cell write and says only that the
#: arguments were invalid.
#:
#: Slack names no argument, so neither can this: it names the two mistakes that
#: are known to produce this code and declines to say which of them happened.
#: Both were made by the same model in one sitting, and each cost calls a
#: sentence here would have saved. The last clause is the canvas toolkit's, for
#: the same reason it is there -- a caller whose column ids and value shapes
#: are both right has found something this tool is doing, and nothing in the
#: refusal would otherwise tell it that its own arguments are not the suspect.
_INVALID_CELL_ARGUMENTS_DETAIL = (
    "Slack rejected the arguments for this cell write and names none of them,"
    " so this names what to check rather than what was wrong. Two causes"
    " produce it. A column_id that is the key a schema was written with rather"
    " than the id Slack generated for that column -- read the List with"
    " include_list true and take the id from the schema. Or a value in the"
    " wrong shape for its column type: almost every type takes its value in an"
    " array even where the column holds one thing, a text cell takes an array"
    " holding a rich_text block rather than a string, and checkbox is the one"
    " Slack documents as a bare boolean. A call whose column ids and value"
    " shapes are both right and that still gets this back is a fault in this"
    " tool rather than in the arguments, and is worth reporting as one"
)

#: Codes that mean the call named several targets and some subset of them
#: failed. Reaching one is what starts the per-target pass.
_PARTIAL_FAILURE_CODES = frozenset(
    {"failed_to_update_user_ids", "user_not_found", "channel_not_found"}
)


def slack_list_request_metadata(
    channel_id: str | None,
    metadata: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Return trusted metadata for a request that may touch a List, or nothing.

    Two request shapes, and one condition behind both: a Slack install can be
    resolved for this request, so there is a workspace for a List to exist in
    and a token to reach it with.

    * An inbound Slack turn, which arrives on the ``slack`` transport.
    * A scheduled run, honoured only where the scheduler marked it as its own. A
      List is the surface a recurring tally belongs on -- rows are appended and
      cells are updated in place rather than reposted -- so excluding cron would
      take the capability away from the case it fits best. Without the marker a
      stray Slack field left on some other request cannot reach these tools.

    Nothing further is required, and each absence is a decision. No conversation
    is demanded, because a List belongs to the workspace rather than to a
    conversation and no tool here names one except as a sharing target. No
    requester is demanded, because a List belongs to the app that made it rather
    than to the person who asked.

    Not a condition either: the history policy word. That says how far this
    conversation may *read* scrollback, and a List holds none of it.
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
    partial failure, and the report it produces -- take the same five values and
    must agree about every one of them. Passed as one object rather than as five
    parameters threaded through three signatures, so that the per-target retry
    cannot end up sending a different level from the call it is finding out
    about.
    """

    #: ``slackLists.access.set`` or ``slackLists.access.delete``.
    method: str
    list_id: str
    #: ``"channel_ids"`` or ``"user_ids"`` -- which of Slack's two target arrays
    #: this call fills in. One or the other, never both. Slack's spelling,
    #: because this value is an API key; the argument a model writes is
    #: ``chat_ids``.
    kind: str
    ids: tuple[str, ...]
    #: Empty for a revoke, which carries no level.
    level: str

    @property
    def removes(self) -> bool:
        return self.method == _LIST_ACCESS_DELETE

    @property
    def reported_level(self) -> str:
        """The level a result reports, which for a revoke is ``"none"``."""
        return "none" if self.removes else self.level


class _ListCallFailure(_SlackCallFailure):
    """A Slack refusal that keeps the sentence Slack wrote beside the code.

    ``where`` is overridden because ten of these twelve methods are two dots
    deep. The shared rule turns the first underscore into a dot, which is exact
    for ``chat_postMessage`` and wrong for ``slackLists_items_list``.
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


def _list_refusal_json(code: str, detail: str = "", **fields: Any) -> str:
    """One refusal, in a shape that never claims to know what it does not.

    Nothing about the List's state is asserted. A call Slack refused establishes
    nothing about what the List holds now -- an update refused for its size did
    not thereby empty a row -- so no ``created``, ``updated`` or ``deleted`` key
    appears on a failure, where a ``false`` would read as a claim about the List
    rather than about this call.
    """
    payload: dict[str, Any] = {"ok": False, "error": code}
    if detail:
        payload["detail"] = detail
    for key, value in fields.items():
        if value:
            payload[key] = value
    logger.warning(
        "slack list refused: %s%s", code, f" -- {detail}" if detail else ""
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


def _object_list(value: Any) -> "list[dict[str, Any]] | None":
    """A declared array of objects as a list of dicts, or ``None``.

    ``None`` means *this was not an array of objects*. An empty array is
    returned as an empty list, which the caller refuses separately: a cells
    array holding nothing is a call that would change nothing.
    """
    if value is None:
        return []
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return None
    out: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, Mapping):
            return None
        out.append(dict(item))
    return out


def _cell_payload(cell: Mapping[str, Any]) -> "tuple[dict[str, Any], str]":
    """One declared cell as the object Slack wants, or ``({}, reason)``.

    Everything but ``text`` travels unchanged. A cell's value key is named
    after its column's type -- ``rich_text``, ``select``, ``user``, ``date``
    and the rest -- and :data:`CELL_VALUE_SHAPES` documents the shapes without
    enforcing them. Nothing is checked here and nothing is reshaped: the schema
    a workspace wrote is the authority, Slack validates against it, and an
    allow-list would refuse a column type added after it was written, while a
    scalar quietly wrapped in an array would teach a caller a shape Slack does
    not take.

    ``text`` is the one key this module adds. It is Markdown, it is rendered to
    the array a text cell takes, and it exists because the alternative is asking
    a model to hand-build a ``rich_text`` tree for the commonest cell in any
    List. Supplying ``rich_text`` directly stays available and is not second
    best; supplying both is refused rather than resolved, because there is no
    right answer to which of two contradicting values was meant.
    """
    payload = {key: value for key, value in cell.items() if key != "text"}
    raw = cell.get("text")
    if raw is None:
        return payload, ""
    if not isinstance(raw, str):
        return {}, "a cell's text must be a string of Markdown"
    if "rich_text" in payload:
        return {}, (
            "a cell carries text or rich_text, not both: they are two spellings"
            " of one value and nothing here can say which was meant"
        )
    block = rich_text_block(raw)
    if block is None:
        return {}, (
            "a cell's text held nothing a reader would see. Write the text, or"
            " leave the cell out"
        )
    payload["rich_text"] = [block]
    return payload, ""


class SlackListToolkit:
    """The five List tools, scoped to the Slack install the request came from."""

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

        A List written into the wrong workspace is a table of somebody else's
        data in a stranger's Slack, so an unresolvable install is refused rather
        than served from whichever block happens to hold a token.
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

        Both failure shapes are read. Slack answers a refused call with HTTP 200
        and ``{"ok": false, "error": ...}``; the SDK raises that as
        ``SlackApiError``, and an older one hands the body back instead, so a
        code arriving one way on one deployment and the other way on the next
        would otherwise be two behaviours.
        """
        try:
            client = self._get_client()
        except _SlackCallFailure as exc:
            raise _ListCallFailure(exc.code, exc.method or method) from None
        retries = 0
        while True:
            remaining = self._remaining()
            if remaining <= 0:
                raise _ListCallFailure("slack_call_timed_out", method)
            try:
                response = await asyncio.wait_for(
                    make_call(client), timeout=remaining
                )
            except TimeoutError:
                raise _ListCallFailure("slack_call_timed_out", method) from None
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

    def _failure(self, data: Mapping[str, Any], method: str) -> _ListCallFailure:
        """One Slack body as the failure this module raises."""
        code = _safe_error_code(data.get("error"), self._bot_token)
        detail = ""
        raw = data.get("detail")
        if raw:
            detail, _, _ = redact_credentials(
                raw, bot_token=self._bot_token, cap=_MAX_DETAIL_CHARS
            )
        return _ListCallFailure(code, method, detail=detail)

    async def _api(self, method: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        """One List method, sent as a JSON body.

        Sent as JSON rather than as a form because six of these methods take an
        array, and a JSON body is the one encoding in which an array is
        unambiguously an array.
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
    def _list_id_refusal(list_id: str) -> "str | None":
        """Why *list_id* cannot be sent, or ``None`` when it can."""
        if not list_id:
            return _list_refusal_json(
                "list_id_required",
                "no List id was given. A List id is taken unchanged from a"
                " result that reported one",
            )
        if not _LIST_ID_RE.match(list_id):
            return _list_refusal_json(
                "list_id_malformed",
                "a Slack List id is an F followed by upper-case letters and"
                " digits, such as F1234ABCD; this is not one",
                list_id=list_id[:32],
            )
        return None

    def _slack_refusal(self, exc: _ListCallFailure, **fields: Any) -> str:
        """One Slack refusal as the shape every failure here returns.

        Two codes are answered by where they came from rather than by the code
        alone. ``missing_scope`` names the scope the method needs, which
        depends on whether the method reads or writes. ``invalid_arguments``
        names no argument at all in Slack's answer, so on the two cell-writing
        methods it is answered with the two mistakes that are known to produce
        it -- and on every other method with the plain sentence, because
        nothing here knows which argument a create or a share got wrong.
        """
        detail = _FAILURE_DETAIL.get(exc.code, "")
        if (
            exc.code == "invalid_arguments"
            and exc.method in _CELL_WRITE_METHODS
        ):
            detail = _INVALID_CELL_ARGUMENTS_DETAIL
        if not detail:
            detail = f"Slack refused {exc.where}"
            if exc.code == "missing_scope":
                scope = (
                    _LISTS_READ_SCOPE
                    if exc.method in _READ_METHODS
                    else _LISTS_WRITE_SCOPE
                )
                detail += (
                    f"; this needs the {scope} scope, which this installation"
                    f" does not hold"
                )
        if exc.detail:
            detail = f"{detail}. Slack said: {exc.detail}"
        return _list_refusal_json(exc.code, detail, **fields)

    @staticmethod
    def _no_slack_request_refusal() -> str:
        """The refusal for a request no Slack path settled.

        The registration gate already declines to mount these tools for such a
        request, so this is defence in depth: the provider fails closed per
        request, and a toolkit mounted for one turn answers the next.
        """
        return _list_refusal_json(
            "trusted_slack_request_required",
            "this request did not arrive from Slack, so there is no workspace"
            " for a List to exist in and no token to reach one with",
        )

    def _description_refusal(
        self, description: Any, description_blocks: Any
    ) -> "tuple[list[dict[str, Any]] | None, str]":
        """The description payload for a create or an update, or a refusal.

        ``None`` with no refusal means *neither was given*, which leaves the
        argument off the call rather than sending an empty one: on an update
        that is the difference between leaving the description alone and
        clearing it.
        """
        if description is not None and description_blocks is not None:
            return None, _list_refusal_json(
                "description_ambiguous",
                "description and description_blocks are two spellings of one"
                " value and nothing here can say which was meant. Pass one",
            )
        if description_blocks is not None:
            blocks = _object_list(description_blocks)
            if blocks is None:
                return None, _list_refusal_json(
                    "description_blocks_malformed",
                    "description_blocks is a list of Block Kit blocks. It was"
                    " not a list of objects",
                )
            return blocks, ""
        if description is None:
            return None, ""
        if not isinstance(description, str):
            return None, _list_refusal_json(
                "description_malformed",
                "description is a string of Markdown. It was not a string",
            )
        block = rich_text_block(description)
        return ([] if block is None else [block]), ""

    # ── reading ──────────────────────────────────────────────────────────

    async def read_slack_list(
        self,
        list_id: str,
        item_id: str | None = None,
        include_is_subscribed: bool = False,
        archived: bool = False,
        include_list: bool = False,
        limit: int | None = None,
        cursor: str | None = None,
        export: bool = False,
        export_format: str | None = None,
        export_job_id: str | None = None,
        include_archived: bool = False,
        include_threads: bool = False,
        include_attachments: bool = False,
    ) -> str:
        """Read a List: one item, a page of items, or an export of the whole."""
        metadata = self._runtime_metadata()
        if not metadata:
            return self._no_slack_request_refusal()
        raw_id = str(list_id or "").strip()
        refusal = self._list_id_refusal(raw_id)
        if refusal:
            return refusal

        item = str(item_id or "").strip()
        job = str(export_job_id or "").strip()
        wants_export = bool(export) or bool(job)
        if item and wants_export:
            return _list_refusal_json(
                "read_mode_ambiguous",
                "item_id reads one item and export exports the whole List."
                " They are two different calls; make one of them",
                list_id=raw_id,
            )

        if wants_export:
            return await self._export(
                metadata,
                raw_id,
                job=job,
                export_format=export_format,
                include_archived=bool(include_archived),
                include_threads=bool(include_threads),
                include_attachments=bool(include_attachments),
            )

        misplaced = [
            name
            for name, given in (
                ("export_format", export_format is not None),
                ("include_archived", bool(include_archived)),
                ("include_threads", bool(include_threads)),
                ("include_attachments", bool(include_attachments)),
            )
            if given
        ]
        if misplaced:
            return _list_refusal_json(
                "export_arguments_without_export",
                f"{', '.join(misplaced)} only affect an export and no export"
                f" was asked for. Pass export to start one",
                list_id=raw_id,
            )

        if item:
            stray = [
                name
                for name, given in (
                    ("archived", bool(archived)),
                    ("include_list", bool(include_list)),
                    ("limit", limit is not None),
                    ("cursor", cursor is not None),
                )
                if given
            ]
            if stray:
                return _list_refusal_json(
                    "listing_arguments_with_item_id",
                    f"{', '.join(stray)} page through a List and item_id reads"
                    f" one item. Reading one item returns the parent List"
                    f" anyway -- its schema, its views and its limits -- so"
                    f" neither is needed alongside the other",
                    list_id=raw_id,
                )
            return await self._read_item(
                metadata,
                raw_id,
                item,
                include_is_subscribed=bool(include_is_subscribed),
            )

        if include_is_subscribed:
            return _list_refusal_json(
                "include_is_subscribed_without_item_id",
                "include_is_subscribed says whether one item is subscribed to,"
                " so it needs the item. Pass item_id",
                list_id=raw_id,
            )
        return await self._read_page(
            metadata,
            raw_id,
            archived=bool(archived),
            include_list=bool(include_list),
            limit=limit,
            cursor=cursor,
        )

    async def _read_item(
        self,
        metadata: Mapping[str, Any],
        list_id: str,
        item_id: str,
        *,
        include_is_subscribed: bool,
    ) -> str:
        """One item, and the List object Slack folds in beside it."""
        try:
            await self._load_settings(metadata)
        except SlackWorkspaceUnresolved as unresolved:
            return _list_refusal_json(
                unresolved.code, unresolved.detail, list_id=list_id
            )

        payload: dict[str, Any] = {"list_id": list_id, "id": item_id}
        if include_is_subscribed:
            payload["include_is_subscribed"] = True
        try:
            response = await self._api(_ITEMS_INFO, payload)
        except _ListCallFailure as exc:
            return self._slack_refusal(exc, list_id=list_id, item_id=item_id)

        result: dict[str, Any] = {
            "ok": True,
            "list_id": list_id,
            "item_id": item_id,
            "item": response.get("record") or response.get("item") or {},
            # Unconditional, and the sentence is the fact rather than a caveat
            # about this call. Slack has no method that returns a List on its
            # own, so this is where a schema comes from.
            "list_note": (
                "Reading one item returns the List it belongs to as well: its"
                " column schema, its views and its limits. Slack has no method"
                " that returns a List on its own."
            ),
        }
        for key in ("list", "subtask_schema", "views", "limits"):
            if key in response:
                result[key] = response[key]
        if include_is_subscribed and "is_subscribed" in response:
            result["is_subscribed"] = response["is_subscribed"]
        return json.dumps(result, ensure_ascii=False)

    async def _read_page(
        self,
        metadata: Mapping[str, Any],
        list_id: str,
        *,
        archived: bool,
        include_list: bool,
        limit: Any,
        cursor: Any,
    ) -> str:
        """One page of items, and the cursor that continues it."""
        if limit is not None and not isinstance(limit, int):
            return _list_refusal_json(
                "limit_malformed",
                "limit is a whole number of rows. It was not a number",
                list_id=list_id,
            )
        if isinstance(limit, int) and limit < 1:
            return _list_refusal_json(
                "limit_malformed",
                f"limit is a whole number of rows and {limit} is not one",
                list_id=list_id,
            )
        if cursor is not None and not isinstance(cursor, str):
            return _list_refusal_json(
                "cursor_malformed",
                "cursor is the string a previous page reported. It was not a"
                " string",
                list_id=list_id,
            )

        try:
            await self._load_settings(metadata)
        except SlackWorkspaceUnresolved as unresolved:
            return _list_refusal_json(
                unresolved.code, unresolved.detail, list_id=list_id
            )

        asked = min(int(limit), MAX_ITEMS_PER_PAGE) if limit else MAX_ITEMS_PER_PAGE
        payload: dict[str, Any] = {"list_id": list_id, "limit": asked}
        if archived:
            payload["archived"] = True
        if include_list:
            payload["include_list"] = True
        page = str(cursor or "").strip()
        if page:
            payload["cursor"] = page

        try:
            response = await self._api(_ITEMS_LIST, payload)
        except _ListCallFailure as exc:
            return self._slack_refusal(exc, list_id=list_id)

        raw = response.get("items")
        if not isinstance(raw, list):
            raw = response.get("records")
        items = [dict(row) for row in raw if isinstance(row, Mapping)] if isinstance(
            raw, list
        ) else []
        result: dict[str, Any] = {
            "ok": True,
            "list_id": list_id,
            "items": items,
            "items_count": len(items),
            "archived": bool(archived),
        }
        if include_list and "list" in response:
            result["list"] = response["list"]
        next_cursor = ""
        meta = response.get("response_metadata")
        if isinstance(meta, Mapping):
            next_cursor = str(meta.get("next_cursor") or "").strip()
        if not next_cursor:
            next_cursor = str(response.get("next_cursor") or "").strip()
        coverage: list[str] = []
        if next_cursor:
            result["next_cursor"] = next_cursor
            coverage.append(
                "more_items: this is one page and there are more. Pass"
                " next_cursor back as cursor to continue"
            )
        if limit and int(limit) > MAX_ITEMS_PER_PAGE:
            coverage.append(
                f"limit_reduced: {int(limit)} rows were asked for and this tool"
                f" returns at most {MAX_ITEMS_PER_PAGE} in one page"
            )
        # Unconditional, because it is a property of Slack's method rather than
        # of this page. A model that believed a filter existed would keep
        # looking for the argument.
        coverage.append(
            "no_server_side_query: Slack's listing takes only a page size, a"
            " cursor and the archived flag. There is no way to ask it for the"
            " rows where a column has a value, so filtering or sorting by a"
            " column means paging the whole List and doing it here"
        )
        result["coverage"] = coverage
        return json.dumps(result, ensure_ascii=False)

    async def _export_content(self, url: str) -> "tuple[str, int, bool]":
        """One export's bytes as text, its full length, and whether it was cut.

        Not a new fetch. ``slack_file_transfer`` holds the hardened path the
        history toolkit and the canvas toolkit already download through -- one
        resolution whose every address is validated, the address dialled
        rather than the name, each redirect hop put through the checks the
        first URL faced, and a transport that refuses an unpinned connection.

        The URL stays a local. It is never logged and never allowed into an
        exception that reaches a result: an httpx exception message embeds the
        URL verbatim, so a failure is mapped from the status code and the
        exception class alone.

        Two caps, and the flag says only *something was dropped* -- not which
        one bit. A byte-capped read reports the characters it decoded rather
        than the export's true length, which is a count nothing here can know
        without pulling the rest down, and the caller is told the figure is a
        floor by the same flag that tells it the content is partial.
        """
        if https_file_host(url) not in _EXPORT_HOSTS:
            raise _ListCallFailure("export_host_not_allowed", _DOWNLOAD_GET)
        headers = {"Authorization": f"Bearer {self._bot_token}"}
        factory = self._http_client_factory or (
            lambda: httpx.AsyncClient(
                timeout=httpx.Timeout(FILE_TRANSFER_TIMEOUT_SECONDS),
                follow_redirects=False,
                transport=slack_file_transport(_EXPORT_HOSTS),
            )
        )
        chunks: list[bytes] = []
        size = 0
        truncated = False
        try:
            async with (
                factory() as client,
                stream_slack_file(
                    client, url, headers=headers, allowed_hosts=_EXPORT_HOSTS
                ) as response,
            ):
                if response.status_code in (401, 403):
                    raise _ListCallFailure("export_read_denied", _DOWNLOAD_GET)
                if response.status_code != 200:
                    raise _ListCallFailure(
                        "export_download_failed", _DOWNLOAD_GET
                    )
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > MAX_EXPORT_BYTES:
                        truncated = True
                        break
                    chunks.append(chunk)
        except SlackFileTransferRefused as refused:
            raise _ListCallFailure(refused.code, _DOWNLOAD_GET) from None
        except httpx.HTTPError:
            raise _ListCallFailure(
                "export_download_failed", _DOWNLOAD_GET
            ) from None
        text = b"".join(chunks).decode("utf-8", "replace")
        total = len(text)
        if total > MAX_EXPORT_CHARS:
            return text[:MAX_EXPORT_CHARS], total, True
        return text, total, truncated

    async def _export(
        self,
        metadata: Mapping[str, Any],
        list_id: str,
        *,
        job: str,
        export_format: Any,
        include_archived: bool,
        include_threads: bool,
        include_attachments: bool,
    ) -> str:
        """Start an export, or ask an already-started one whether it is ready.

        Slack splits this in two: one call starts a job and another asks after
        it. The second call has to be handed the same ``format``,
        ``include_threads`` and ``include_attachments`` the first was -- Slack
        says so in those words -- so they are arguments of both halves here and
        a caller resuming with a job id passes them again.
        """
        fmt = str(export_format or "csv").strip().lower()
        if fmt not in EXPORT_FORMATS:
            return _list_refusal_json(
                "export_format_unknown",
                f"{fmt!r} is not an export format. Slack's two are"
                f" {', '.join(EXPORT_FORMATS)}",
                list_id=list_id,
            )
        if fmt != "json":
            extras = [
                name
                for name, given in (
                    ("include_threads", include_threads),
                    ("include_attachments", include_attachments),
                )
                if given
            ]
            if extras:
                return _list_refusal_json(
                    "export_option_needs_json",
                    f"Slack applies {' and '.join(extras)} only when the format"
                    f" is json, so a csv export would silently leave that out."
                    f" Pass export_format json, or drop the option",
                    list_id=list_id,
                )

        try:
            await self._load_settings(metadata)
        except SlackWorkspaceUnresolved as unresolved:
            return _list_refusal_json(
                unresolved.code, unresolved.detail, list_id=list_id
            )

        job_id = job
        if not job_id:
            start: dict[str, Any] = {"list_id": list_id, "format": fmt}
            if include_archived:
                start["include_archived"] = True
            if include_threads:
                start["include_threads"] = True
            if include_attachments:
                start["include_attachments"] = True
            try:
                response = await self._api(_DOWNLOAD_START, start)
            except _ListCallFailure as exc:
                return self._slack_refusal(exc, list_id=list_id)
            job_id = str(response.get("job_id") or "").strip()
            if not job_id:
                return _list_refusal_json(
                    "export_job_missing",
                    "Slack accepted the export and named no job to ask after,"
                    " so there is nothing to collect it with",
                    list_id=list_id,
                )

        ask: dict[str, Any] = {
            "list_id": list_id,
            "job_id": job_id,
            "format": fmt,
        }
        if include_threads:
            ask["include_threads"] = True
        if include_attachments:
            ask["include_attachments"] = True

        status = ""
        for attempt in range(_MAX_EXPORT_POLLS):
            if self._remaining() <= 0:
                break
            try:
                response = await self._api(_DOWNLOAD_GET, ask)
            except _ListCallFailure as exc:
                return self._slack_refusal(
                    exc, list_id=list_id, export_job_id=job_id
                )
            status = str(response.get("status") or "").strip()
            url = str(response.get("download_url") or "").strip()
            if url:
                logger.info("slack list: exported %s as %s", list_id, fmt)
                return await self._exported(list_id, job_id, fmt, status, url)
            if attempt + 1 < _MAX_EXPORT_POLLS:
                await self._sleep(_EXPORT_POLL_SECONDS)

        return json.dumps(
            {
                "ok": True,
                "list_id": list_id,
                "export_job_id": job_id,
                "export_format": fmt,
                "status": status,
                "ready": False,
                # Not a failure. The export was accepted and is running; this
                # call simply stopped asking. Slack documents no status
                # vocabulary and no expected duration, so its own word is
                # passed through rather than matched against values nobody
                # wrote down.
                "detail": (
                    "The export has been started and was not ready yet. Ask"
                    " again with export_job_id set to this job, and the same"
                    " export_format, include_threads and include_attachments --"
                    " Slack requires them to match the call that started it."
                ),
            },
            ensure_ascii=False,
        )

    async def _exported(
        self,
        list_id: str,
        job_id: str,
        fmt: str,
        status: str,
        url: str,
    ) -> str:
        """A finished export, with its content where that could be fetched.

        The URL is reported either way. A caller may want the link rather than
        the bytes -- to hand to a person, or to fetch the whole of a file this
        result only carries the head of -- and a download this tool could not
        make is a fact about the download rather than about the export, which
        ran and finished.
        """
        result: dict[str, Any] = {
            "ok": True,
            "list_id": list_id,
            "export_job_id": job_id,
            "export_format": fmt,
            "status": status,
            "download_url": url,
        }
        try:
            content, total, truncated = await self._export_content(url)
        except _ListCallFailure as exc:
            result["content_error"] = exc.code
            result["download_note"] = (
                "The export finished and its content could not be fetched"
                f" here: {exc.code}. The URL is Slack's own, needs this app's"
                " token, and is not public."
            )
            return json.dumps(result, ensure_ascii=False)
        result["content"] = content
        result["content_chars"] = len(content)
        result["content_truncated"] = truncated
        if truncated:
            result["content_chars_total"] = total
            result["download_note"] = (
                f"Only the first {len(content)} characters are here. This"
                f" tool returns at most {MAX_EXPORT_CHARS} characters of an"
                f" export and pulls down at most {MAX_EXPORT_BYTES} bytes of"
                " one, so content_chars_total is a floor rather than the"
                " file's length when the byte cap is what stopped it. The"
                " whole file is at download_url, which is Slack's own and"
                " needs this app's token."
            )
        else:
            result["download_note"] = (
                "The content above is the whole export. The URL is Slack's"
                " own, needs this app's token, and is not public."
            )
        return json.dumps(result, ensure_ascii=False)

    # ── creating ─────────────────────────────────────────────────────────

    async def write_slack_list(
        self,
        name: str,
        description: str | None = None,
        description_blocks: Any = None,
        schema: Any = None,
        copy_from_list_id: str | None = None,
        include_copied_list_records: bool = False,
        todo_mode: bool = False,
    ) -> str:
        """Create a List, from a schema or as a copy of an existing one."""
        metadata = self._runtime_metadata()
        if not metadata:
            return self._no_slack_request_refusal()

        title = str(name or "").strip()
        if not title:
            return _list_refusal_json(
                "name_required",
                "a List is created with a name and none was given",
            )

        blocks, refusal = self._description_refusal(description, description_blocks)
        if refusal:
            return refusal

        columns: "list[dict[str, Any]] | None" = None
        if schema is not None:
            columns = _object_list(schema)
            if columns is None:
                return _list_refusal_json(
                    "schema_malformed",
                    "schema is a list of column definitions. It was not a list"
                    " of objects",
                )
            if not columns:
                return _list_refusal_json(
                    "schema_empty",
                    "schema held no columns. Leave it out to take Slack's"
                    " default of one text column, or name the columns",
                )

        source = str(copy_from_list_id or "").strip()
        if source:
            source_refusal = self._list_id_refusal(source)
            if source_refusal:
                return source_refusal
            if columns is not None:
                # Slack refuses this with invalid_copy_and_schema_args. Refused
                # here as well because a create is a write, and a write refused
                # for a bad argument must not have to happen to find out.
                return _list_refusal_json(
                    "invalid_copy_and_schema_args",
                    _FAILURE_DETAIL["invalid_copy_and_schema_args"],
                )
        elif include_copied_list_records:
            return _list_refusal_json(
                "missing_arg_copy_from_list_id",
                _FAILURE_DETAIL["missing_arg_copy_from_list_id"],
            )

        try:
            await self._load_settings(metadata)
        except SlackWorkspaceUnresolved as unresolved:
            return _list_refusal_json(unresolved.code, unresolved.detail)

        payload: dict[str, Any] = {"name": title}
        if blocks is not None:
            payload["description_blocks"] = blocks
        if columns is not None:
            payload["schema"] = columns
        if source:
            payload["copy_from_list_id"] = source
            if include_copied_list_records:
                payload["include_copied_list_records"] = True
        if todo_mode:
            payload["todo_mode"] = True

        try:
            response = await self._api(_LIST_CREATE, payload)
        except _ListCallFailure as exc:
            return self._slack_refusal(exc)

        created = response.get("list")
        new_id = ""
        if isinstance(created, Mapping):
            new_id = str(created.get("id") or created.get("list_id") or "").strip()
        if not new_id:
            new_id = str(response.get("list_id") or response.get("id") or "").strip()
        logger.info("slack list: created %s", new_id)
        result: dict[str, Any] = {
            "ok": True,
            "list_id": new_id,
            "name": title,
            "todo_mode": bool(todo_mode),
            # Unconditional, and the one sentence about this call that a caller
            # cannot get back later. The columns are settled here for good.
            "schema_note": (
                "The columns this List has are settled now. No Slack method"
                " adds, removes, renames or retypes a column afterwards, and"
                " editing a List covers its name, its description and todo mode"
                " only. Changing the columns means creating another List."
            ),
        }
        if isinstance(created, Mapping):
            result["list"] = dict(created)
        return json.dumps(result, ensure_ascii=False)

    # ── editing ──────────────────────────────────────────────────────────

    async def edit_slack_list(
        self,
        list_id: str,
        operation: str,
        cells: Any = None,
        initial_fields: Any = None,
        duplicated_item_id: str | None = None,
        parent_item_id: str | None = None,
        item_id: str | None = None,
        item_ids: Any = None,
        name: str | None = None,
        description: str | None = None,
        description_blocks: Any = None,
        todo_mode: Any = None,
    ) -> str:
        """Add an item, write cells, delete items, or rename the List."""
        metadata = self._runtime_metadata()
        if not metadata:
            return self._no_slack_request_refusal()
        raw_id = str(list_id or "").strip()
        refusal = self._list_id_refusal(raw_id)
        if refusal:
            return refusal

        action = str(operation or "").strip()
        if action not in EDIT_OPERATIONS:
            return _list_refusal_json(
                "operation_unknown",
                f"{action!r} is not an edit operation. The operations are"
                f" {', '.join(sorted(EDIT_OPERATIONS))}",
                list_id=raw_id,
            )

        given = {
            "cells": cells is not None,
            "initial_fields": initial_fields is not None,
            "duplicated_item_id": bool(str(duplicated_item_id or "").strip()),
            "parent_item_id": bool(str(parent_item_id or "").strip()),
            "item_id": bool(str(item_id or "").strip()),
            "item_ids": item_ids is not None,
            "name": name is not None,
            "description": description is not None,
            "description_blocks": description_blocks is not None,
            "todo_mode": todo_mode is not None,
        }
        allowed = set(_EDIT_ARGUMENTS[action])
        stray = sorted(key for key, present in given.items() if present and key not in allowed)
        if stray:
            return _list_refusal_json(
                "argument_not_for_this_operation",
                f"{', '.join(stray)} {'do' if len(stray) > 1 else 'does'} not"
                f" belong to {action}, which takes"
                f" {', '.join(sorted(allowed))}. Nothing was sent, because an"
                f" argument this call would have ignored means the call was not"
                f" the one that was meant",
                list_id=raw_id,
            )

        builder = {
            "create_item": lambda: self._create_item_payload(
                raw_id, initial_fields, duplicated_item_id, parent_item_id
            ),
            "update_cells": lambda: self._update_cells_payload(raw_id, cells),
            "delete_item": lambda: self._delete_item_payload(raw_id, item_id),
            "delete_items": lambda: self._delete_items_payload(raw_id, item_ids),
            "update_list": lambda: self._update_list_payload(
                raw_id, name, description, description_blocks, todo_mode
            ),
        }[action]
        payload, build_refusal = builder()
        if build_refusal:
            return build_refusal

        try:
            await self._load_settings(metadata)
        except SlackWorkspaceUnresolved as unresolved:
            return _list_refusal_json(
                unresolved.code, unresolved.detail, list_id=raw_id
            )

        method = EDIT_OPERATIONS[action]
        try:
            response = await self._api(method, payload)
        except _ListCallFailure as exc:
            return self._slack_refusal(exc, list_id=raw_id, operation=action)

        logger.info("slack list: %s on %s", action, raw_id)
        result: dict[str, Any] = {
            "ok": True,
            "list_id": raw_id,
            "operation": action,
        }
        if action == "create_item":
            record = response.get("record") or response.get("item")
            if isinstance(record, Mapping):
                result["item"] = dict(record)
                result["item_id"] = str(record.get("id") or "").strip()
            if str(parent_item_id or "").strip():
                result["parent_item_id"] = str(parent_item_id).strip()
        elif action == "update_cells":
            result["cells"] = len(payload["cells"])
        elif action == "delete_item":
            result["item_id"] = payload["id"]
            result["deleted"] = True
        elif action == "delete_items":
            result["item_ids"] = list(payload["ids"])
            result["requested"] = len(payload["ids"])
            # Honest rather than tidy. Slack documents no per-record error for
            # this method, so nothing in the response distinguishes "all of
            # them" from "some of them", and claiming either would be a claim
            # nobody checked.
            result["detail"] = (
                "Slack answered this call without refusing it. It documents no"
                " per-item result for a multiple delete, so which of the named"
                " items were removed is not established by this response. Read"
                " the List back to find out."
            )
        return json.dumps(result, ensure_ascii=False)

    def _create_item_payload(
        self,
        list_id: str,
        initial_fields: Any,
        duplicated_item_id: Any,
        parent_item_id: Any,
    ) -> "tuple[dict[str, Any], str]":
        """The body of one ``slackLists.items.create``, or the refusal it earns."""
        payload: dict[str, Any] = {"list_id": list_id}
        if initial_fields is not None:
            fields = _object_list(initial_fields)
            if fields is None:
                return {}, _list_refusal_json(
                    "initial_fields_malformed",
                    "initial_fields is a list of cell objects. It was not a"
                    " list of objects",
                    list_id=list_id,
                )
            rendered: list[dict[str, Any]] = []
            for field in fields:
                cell, reason = _cell_payload(field)
                if reason:
                    return {}, _list_refusal_json(
                        "cell_malformed", reason, list_id=list_id
                    )
                rendered.append(cell)
            payload["initial_fields"] = rendered
        source = str(duplicated_item_id or "").strip()
        if source:
            payload["duplicated_item_id"] = source
        parent = str(parent_item_id or "").strip()
        if parent:
            payload["parent_item_id"] = parent
        return payload, ""

    def _update_cells_payload(
        self, list_id: str, cells: Any
    ) -> "tuple[dict[str, Any], str]":
        """The body of one ``slackLists.items.update``, or the refusal it earns."""
        rows = _object_list(cells)
        if rows is None:
            return {}, _list_refusal_json(
                "cells_malformed",
                "cells is a list of cell objects, each naming a column and"
                " carrying a value. It was not a list of objects",
                list_id=list_id,
            )
        if not rows:
            return {}, _list_refusal_json(
                "cells_required",
                "cells held nothing, so there is nothing to write and the List"
                " is untouched",
                list_id=list_id,
            )
        rendered: list[dict[str, Any]] = []
        for row in rows:
            cell, reason = _cell_payload(row)
            if reason:
                return {}, _list_refusal_json(
                    "cell_malformed", reason, list_id=list_id
                )
            if not str(cell.get("column_id") or "").strip() and not cell.get(
                "column_id_to_create"
            ):
                return {}, _list_refusal_json(
                    "column_id_required",
                    "every cell names the column it writes. One of them named"
                    " none",
                    list_id=list_id,
                )
            if not str(cell.get("row_id") or "").strip() and not cell.get(
                "row_id_to_create"
            ):
                return {}, _list_refusal_json(
                    "row_id_required",
                    "every cell names the row it writes, or asks for a new one."
                    " One of them did neither: give it a row_id, or"
                    " row_id_to_create set to true",
                    list_id=list_id,
                )
            rendered.append(cell)
        return {"list_id": list_id, "cells": rendered}, ""

    def _delete_item_payload(
        self, list_id: str, item_id: Any
    ) -> "tuple[dict[str, Any], str]":
        """The body of one ``slackLists.items.delete``, or the refusal it earns."""
        target = str(item_id or "").strip()
        if not target:
            return {}, _list_refusal_json(
                "item_id_required",
                "no item was named. An item id is taken unchanged from a"
                " listing of this List",
                list_id=list_id,
            )
        return {"list_id": list_id, "id": target}, ""

    def _delete_items_payload(
        self, list_id: str, item_ids: Any
    ) -> "tuple[dict[str, Any], str]":
        """The body of one ``slackLists.items.deleteMultiple``, or a refusal."""
        targets = _string_list(item_ids)
        if targets is None:
            return {}, _list_refusal_json(
                "item_ids_malformed",
                "item_ids is a list of item identifiers. It was not a list of"
                " strings",
                list_id=list_id,
            )
        if not targets:
            return {}, _list_refusal_json(
                "item_ids_required",
                "item_ids held nothing, so there is nothing to delete and the"
                " List is untouched",
                list_id=list_id,
            )
        return {"list_id": list_id, "ids": targets}, ""

    def _update_list_payload(
        self,
        list_id: str,
        name: Any,
        description: Any,
        description_blocks: Any,
        todo_mode: Any,
    ) -> "tuple[dict[str, Any], str]":
        """The body of one ``slackLists.update``, or the refusal it earns.

        ``id`` rather than ``list_id``: this is the one method in the family
        that names the List that way, and the difference is Slack's rather than
        a choice here.
        """
        blocks, refusal = self._description_refusal(description, description_blocks)
        if refusal:
            return {}, refusal
        payload: dict[str, Any] = {"id": list_id}
        title = str(name or "").strip() if name is not None else ""
        if title:
            payload["name"] = title
        if blocks is not None:
            payload["description_blocks"] = blocks
        if todo_mode is not None:
            if not isinstance(todo_mode, bool):
                return {}, _list_refusal_json(
                    "todo_mode_malformed",
                    "todo_mode is true or false. It was neither",
                    list_id=list_id,
                )
            payload["todo_mode"] = todo_mode
        if len(payload) == 1:
            return {}, _list_refusal_json(
                "nothing_to_update",
                "update_list changes the name, the description or todo mode,"
                " and none was given. It cannot change a column: no Slack"
                " method adds, removes, renames or retypes one",
                list_id=list_id,
            )
        return payload, ""

    # ── sharing ──────────────────────────────────────────────────────────

    def _access_targets(
        self, chat_ids: Any, user_ids: Any
    ) -> "tuple[str, list[str], str]":
        """Which targets a share names, or the refusal the pair earns.

        Returns ``(kind, ids, refusal)`` where *kind* is Slack's own
        ``"channel_ids"`` or ``"user_ids"``. Both are arrays and neither has a
        singular twin here: one argument per target type, with the cardinality
        that matters checked below rather than expressed by a second name.
        """
        rooms = _string_list(chat_ids)
        people = _string_list(user_ids)
        if rooms is None or people is None:
            return "", [], _list_refusal_json(
                "access_targets_malformed",
                "chat_ids and user_ids are each a list of identifiers. One of"
                " them was not a list of strings",
            )
        if rooms and people:
            return "", [], _list_refusal_json(
                "access_targets_ambiguous",
                "chat_ids and user_ids cannot both be given: Slack sets access"
                " for conversations or for people, never for both in one call."
                " Make one call for each",
            )
        if not rooms and not people:
            return "", [], _list_refusal_json(
                "access_targets_required",
                "no target was named. Pass chat_ids to act on conversations or"
                " user_ids to act on people; exactly one of the two",
            )
        if rooms:
            bad = [room for room in rooms if not _CHAT_ID_RE.match(room)]
            if bad:
                return "", [], _list_refusal_json(
                    "chat_id_malformed",
                    f"a Slack conversation id is a C, D or G followed by"
                    f" upper-case letters and digits."
                    f" {', '.join(sorted(bad))[:200]} is not one",
                )
            return "channel_ids", rooms, ""
        bad_people = [who for who in people if not _USER_ID_RE.match(who)]
        if bad_people:
            return "", [], _list_refusal_json(
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
            "list_id": request.list_id,
            request.kind: list(ids),
        }
        if request.level:
            payload["access_level"] = request.level
        return await self._api(request.method, payload)

    async def _per_target(
        self, request: _AccessRequest
    ) -> tuple[list[str], list[dict[str, str]], list[str]]:
        """Find out which targets took, one call each.

        Reached only after a call naming several targets failed with a code that
        means *some of them*. Slack's response carries no field saying which, so
        the only way to answer the question is to ask it once per target, and
        setting the same level twice on a target that already took is
        idempotent.

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
            except _ListCallFailure as exc:
                failed.append({"id": target, "error": exc.code})
            else:
                granted.append(target)
        return granted, failed, undetermined

    async def share_slack_list(
        self,
        list_id: str,
        access_level: str | None = None,
        chat_ids: Any = None,
        user_ids: Any = None,
        remove: bool = False,
    ) -> str:
        """Set or revoke access to a List for conversations or for people."""
        metadata = self._runtime_metadata()
        if not metadata:
            return self._no_slack_request_refusal()
        raw_id = str(list_id or "").strip()
        refusal = self._list_id_refusal(raw_id)
        if refusal:
            return refusal

        level = str(access_level or "").strip().lower()
        if remove and level:
            return _list_refusal_json(
                "access_level_not_accepted",
                "remove takes access away entirely, so there is no level to set"
                " alongside it. Pass one or the other",
                list_id=raw_id,
            )
        if not remove:
            if not level:
                return _list_refusal_json(
                    "access_level_required",
                    f"no access level was given. It is one of"
                    f" {', '.join(ACCESS_LEVELS)}, or pass remove to take"
                    f" access away instead",
                    list_id=raw_id,
                )
            if level not in ACCESS_LEVELS:
                return _list_refusal_json(
                    "access_level_unknown",
                    f"{level!r} is not an access level. Slack's three are"
                    f" {', '.join(ACCESS_LEVELS)}",
                    list_id=raw_id,
                )

        kind, ids, target_refusal = self._access_targets(chat_ids, user_ids)
        if target_refusal:
            return target_refusal

        if level == "owner":
            if kind == "channel_ids":
                return _list_refusal_json(
                    "owner_must_be_a_person",
                    "only a person can own a List, and chat_ids names"
                    " conversations. Pass the owner as a single entry in"
                    " user_ids",
                    list_id=raw_id,
                )
            if len(ids) != 1:
                # Slack does not document refusing this, and it is refused here
                # anyway: a List has one owner, so a call naming several is
                # incoherent whatever Slack would do with it, and the outcomes
                # it could have -- the last one wins, the first one wins,
                # several owners -- are not ones a caller could tell apart from
                # the response.
                return _list_refusal_json(
                    "owner_is_one_person",
                    f"a List has one owner and {len(ids)} were named."
                    f" Ownership transfers to exactly one person; name that"
                    f" person alone and set the others to read or write",
                    list_id=raw_id,
                )

        try:
            await self._load_settings(metadata)
        except SlackWorkspaceUnresolved as unresolved:
            return _list_refusal_json(
                unresolved.code, unresolved.detail, list_id=raw_id
            )

        request = _AccessRequest(
            method=_LIST_ACCESS_DELETE if remove else _LIST_ACCESS_SET,
            list_id=raw_id,
            kind=kind,
            ids=tuple(ids),
            level=level,
        )
        try:
            await self._access_call(request, ids)
        except _ListCallFailure as exc:
            if exc.code in _PARTIAL_FAILURE_CODES and len(ids) > 1:
                return await self._partial_result(exc, request)
            return self._slack_refusal(
                exc, list_id=raw_id, **{self._reported_kind(kind): list(ids)}
            )

        logger.info(
            "slack list: %s access for %d %s on %s",
            "revoked" if remove else level,
            len(ids),
            kind,
            raw_id,
        )
        return json.dumps(
            {
                "ok": True,
                "list_id": raw_id,
                "access_level": request.reported_level,
                "granted": list(ids),
            },
            ensure_ascii=False,
        )

    @staticmethod
    def _reported_kind(kind: str) -> str:
        """Slack's target key as this package spells it back to a caller."""
        return "chat_ids" if kind == "channel_ids" else kind

    async def _partial_result(
        self, exc: _ListCallFailure, request: _AccessRequest
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
            "list_id": request.list_id,
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
        logger.warning(
            "slack list: access partially applied on %s -- %d granted, %d"
            " failed, %d undetermined",
            request.list_id,
            len(granted),
            len(failed),
            len(undetermined),
        )
        return json.dumps(payload, ensure_ascii=False)

    # ── deleting ─────────────────────────────────────────────────────────

    async def delete_slack_list(self, list_id: str) -> str:
        """Delete one List, permanently, after checking that it is one."""
        metadata = self._runtime_metadata()
        if not metadata:
            return self._no_slack_request_refusal()
        raw_id = str(list_id or "").strip()
        refusal = self._list_id_refusal(raw_id)
        if refusal:
            return refusal

        try:
            await self._load_settings(metadata)
        except SlackWorkspaceUnresolved as unresolved:
            return _list_refusal_json(
                unresolved.code, unresolved.detail, list_id=raw_id
            )

        # The guard, and the reason this tool reads before it writes. Slack has
        # no method that deletes a List, so deletion goes through files.delete,
        # which deletes any file it is handed -- a canvas, an upload, somebody's
        # screenshot -- permanently and without asking. An id that is not a
        # List's would otherwise be answered with a successful deletion of the
        # wrong thing, which is the one failure mode nothing downstream could
        # detect or undo.
        try:
            response = await self._call(_FILES_INFO, file=raw_id)
        except _ListCallFailure as exc:
            return self._slack_refusal(exc, list_id=raw_id)
        record = response.get("file")
        if not isinstance(record, Mapping):
            return _list_refusal_json(
                "file_not_found",
                "Slack answered with no record for this id, so there is nothing"
                " this tool can confirm is a List. Nothing was deleted",
                list_id=raw_id,
            )
        filetype = str(record.get("filetype") or "").strip().lower()
        if filetype != "list":
            return _list_refusal_json(
                "not_a_list",
                f"this id names a file of type {filetype or 'unknown'!r} rather"
                f" than a List. Nothing was deleted: the call behind this tool"
                f" deletes any file at all, so it is only sent for a file Slack"
                f" confirms is a List",
                list_id=raw_id,
            )

        try:
            await self._call(_FILES_DELETE, file=raw_id)
        except _ListCallFailure as exc:
            return self._slack_refusal(exc, list_id=raw_id)

        logger.info("slack list: deleted %s", raw_id)
        return json.dumps(
            {
                "ok": True,
                "list_id": raw_id,
                "deleted": True,
                "title": str(record.get("title") or ""),
            },
            ensure_ascii=False,
        )

    # ── cards ────────────────────────────────────────────────────────────

    def get_tools(self) -> list[Tool]:
        """Return the five request-scoped List tools."""
        return [
            LocalFunction(card=_read_card(), func=self.read_slack_list),
            LocalFunction(card=_write_card(), func=self.write_slack_list),
            LocalFunction(card=_edit_card(), func=self.edit_slack_list),
            LocalFunction(card=_share_card(), func=self.share_slack_list),
            LocalFunction(card=_delete_card(), func=self.delete_slack_list),
        ]


#: Where a column id comes from, in the words a caller can act on. Repeated in
#: three cards and in the refusal, because the schema a caller wrote is the
#: obvious place to look for one and is the wrong place.
_COLUMN_ID_RULE = (
    "\nColumn ids are Slack's, not yours. A schema passed to "
    "write_slack_list names each column with a key you chose; Slack keeps "
    "that key and assigns the column an id of its own -- Col followed by "
    "letters and digits -- and every write takes the id. Passing the key "
    "back is refused, and Slack's refusal names no argument. Read the List "
    "with read_slack_list and include_list true, which folds the schema in "
    "beside the rows, and take each column's id from there."
)

#: The shapes Slack's own worked cells write, as one card paragraph.
_CELL_SHAPES = (
    "\nWhat a cell value looks like. A cell is a column_id and one value "
    "whose key is the column type's own name -- a select column takes "
    "select, a date column takes date -- with one exception: a text column "
    "takes rich_text. Almost every value sits in an array even where the "
    "column holds one thing, and that is the shape most often got wrong. "
    "Slack's documented ones:"
    + "".join(
        f"\n  {key} -- {shape}."
        for key, shape in CELL_VALUE_SHAPES.items()
    )
    + "\nTypes Slack shows no cell for: "
    + ", ".join(UNDOCUMENTED_CELL_TYPES)
    + ". Slack says every column type can be written and publishes a key "
    "for none of these, so none is guessed at here. Write what the type "
    "name suggests and read the refusal if it is wrong."
    "\nNothing is reshaped on the way out. A scalar is not wrapped in an "
    "array and no key is renamed, because a tool that accepted what Slack "
    "refuses would teach the wrong shape for next time."
)

#: What ``text`` does not carry, in the words a caller can act on. Taken from
#: the renderer's own statement of its exclusions, so that the card promises
#: exactly what arrives rather than something close to it.
_TEXT_COVERAGE = (
    "\nWhat text carries. Bold, italic, strikethrough, inline code, links, "
    "bullet and numbered lists, block quotes, fenced code blocks, emoji "
    "shortcodes, and Slack's own <@U…> and <#C…> tokens."
    "\nWhat text does not carry, so that nothing is lost without warning. A "
    "table arrives as its pipe characters -- rich text has no table of any "
    "kind. A heading arrives bold with its level gone, so # and ###### look "
    "alike. An image arrives as a link to itself. Horizontal rules arrive as "
    "characters, as does a second level of block quote and an indented (rather "
    "than fenced) code block. A list item is the one line it was written on. A "
    "checkbox arrives as the characters [ ]. A mention written as a name -- "
    "@alice, #general -- stays text, because turning it into a real mention "
    "would need a directory this does not consult; write <@U01ABCDEF> for a "
    "real one. A link needs a scheme, so [here](/docs) stays characters. "
    "Styling inside part of a link label is dropped unless it covers the whole "
    "label. If any of that matters, build the payload yourself and pass it as "
    "rich_text instead."
)


def _read_card() -> ToolCard:
    """The card for ``read_slack_list``.

    Five things have to be said that no argument shows. There is no
    server-side query, so a model expecting a ``where`` would look for a
    missing argument rather than conclude there is none. Reading one item
    returns the whole List, which is the only way to obtain a schema, and the
    column ids in it are Slack's rather than the keys the schema was written
    with. An export is a second call after the first, so a result saying *not
    ready* is the normal case rather than a failure. And a finished export
    arrives as content with a cap declared on it, because the alternative --
    a URL and no further word -- is what sent a model to a shell with the bot
    token to fetch one.
    """
    return ToolCard(
        name="read_slack_list",
        description=(
            "Read a Slack List -- a table in Slack with named columns and rows "
            "of typed cells, where a row may itself hold rows."
            "\nThree ways to read one. Pass list_id alone for a page of rows. "
            "Pass item_id as well for a single row. Pass export to get the "
            "whole List as a file."
            "\nReading one row also returns the List itself: its column "
            "schema, its views and its limits. That is the only way to obtain "
            "a schema, because Slack has no method that returns a List on its "
            "own. Column ids come from there, and every write needs them."
            "\nA column id is Slack's, not yours. Each column in the schema "
            "carries both the key it was created with and an id Slack "
            "generated -- Col followed by letters and digits -- and "
            "edit_slack_list takes the id. Pass include_list true to get the "
            "schema folded in beside a page of rows, which is the short way "
            "to collect them."
            "\nThere is no way to search or sort. Slack's row listing takes a "
            "page size, a cursor and nothing else about the contents. To find "
            "the rows where a column has some value, read every page and "
            "filter them here. Do not look for a query argument; there is "
            "none."
            "\nPaging. A page that has more after it reports next_cursor; pass "
            "it back as cursor for the next one. Pass include_list to get the "
            "List's own details folded in beside the rows, which is off by "
            "default."
            "\nArchived rows are a separate page. Pass archived to read those "
            "instead of the live ones; a single call never mixes them."
            "\nExporting. Pass export to ask Slack for the whole List as a "
            "file. csv is the default and json is the complete, hierarchical "
            "form -- and json is the only format in which include_threads and "
            "include_attachments do anything, so asking for either with csv is "
            "refused rather than quietly ignored. An export runs in the "
            "background: if it is not finished the result says so and reports "
            "an export_job_id. Ask again with that id and the same format and "
            "options, which Slack requires to match."
            "\nA finished export comes back as content, not just a link. The "
            "file is fetched here and returned in content, up to "
            f"{MAX_EXPORT_CHARS} characters of it; download_url is reported "
            "beside it for whatever wants the link or the rest of the file. "
            "The URL is Slack's own and needs this app's token, so it is not "
            "something to open elsewhere. If the content was cut, "
            "content_truncated says so and content_chars_total gives the "
            "length -- a floor rather than the length when the byte ceiling "
            "on the download is what stopped it. If the download itself "
            "failed, content_error names why and the URL is still there."
            "\nLists are a paid Slack feature. On a workspace without them "
            "every call here fails with the method itself being unknown."
        ),
        input_params={
            "type": "object",
            "properties": {
                "list_id": {
                    "type": "string",
                    "description": (
                        "The List to read, such as F1234ABCD. Copy it verbatim "
                        "from a result that reported one."
                    ),
                },
                "item_id": {
                    "type": "string",
                    "description": (
                        "Read this one row instead of a page. The List's own "
                        "schema, views and limits come back with it."
                    ),
                },
                "include_is_subscribed": {
                    "type": "boolean",
                    "default": False,
                    "description": (
                        "Also report whether this app is subscribed to the "
                        "row. Needs item_id."
                    ),
                },
                "archived": {
                    "type": "boolean",
                    "default": False,
                    "description": (
                        "Read the archived rows rather than the live ones."
                    ),
                },
                "include_list": {
                    "type": "boolean",
                    "default": False,
                    "description": (
                        "Fold the List's own details -- its title, column "
                        "schema and total row count -- in beside the rows. "
                        "The schema is where each column's generated id "
                        "lives, which is what a write takes."
                    ),
                },
                "limit": {
                    "type": "integer",
                    "description": (
                        "How many rows to return in this page. At most "
                        f"{MAX_ITEMS_PER_PAGE}, which is also the default."
                    ),
                },
                "cursor": {
                    "type": "string",
                    "description": (
                        "Continue a listing from where it stopped. Use the "
                        "next_cursor a previous page reported."
                    ),
                },
                "export": {
                    "type": "boolean",
                    "default": False,
                    "description": (
                        "Export the whole List as a file instead of reading "
                        "rows. Cannot be combined with item_id."
                    ),
                },
                "export_format": {
                    "type": "string",
                    "enum": list(EXPORT_FORMATS),
                    "description": (
                        "The export's format. csv is the default; json is the "
                        "complete, hierarchical export and the only format "
                        "that honours include_threads and include_attachments."
                    ),
                },
                "export_job_id": {
                    "type": "string",
                    "description": (
                        "Ask after an export already started rather than "
                        "starting another. Pass the same export_format, "
                        "include_threads and include_attachments it was "
                        "started with; Slack requires them to match."
                    ),
                },
                "include_archived": {
                    "type": "boolean",
                    "default": False,
                    "description": (
                        "Include archived rows in the export. Export only."
                    ),
                },
                "include_threads": {
                    "type": "boolean",
                    "default": False,
                    "description": (
                        "Include each row's conversation thread in the export. "
                        "json format only."
                    ),
                },
                "include_attachments": {
                    "type": "boolean",
                    "default": False,
                    "description": (
                        "Include file attachment details and access paths in "
                        "the export. json format only."
                    ),
                },
            },
            "required": ["list_id"],
        },
    )


def _write_card() -> ToolCard:
    """The card for ``write_slack_list``.

    The permanence of the schema is the larger half of what this card has to
    get across. A bot can never change a List's columns afterwards, so a List
    created with the wrong ones is a List to abandon rather than repair, and a
    model that did not know would create first and look for the column tool
    second.

    The smaller half is the ``key``. It is the caller's own name for a column
    and it is not what writes one, which is a mistake this card is the first
    place to catch: a model that creates a List here has the keys in hand and
    no reason to think they are not enough.
    """
    return ToolCard(
        name="write_slack_list",
        description=(
            "Create a Slack List -- a table in Slack with named columns and "
            "rows of typed cells. Use it for something tracked rather than "
            "said: a set of tasks, a register of items, a tally that rows get "
            "added to over time."
            "\nThe columns are decided now and cannot be changed later. No "
            "Slack method adds a column, removes one, renames one or changes "
            "its type, and editing a List afterwards covers its name, its "
            "description and todo mode only. Getting different columns means "
            "creating a different List. Decide the schema before calling this."
            "\nWith no schema Slack creates a List with a single text column."
            "\nThe key you give a column is not what writes it. Slack keeps "
            "the key and assigns the column an id of its own -- Col followed "
            "by letters and digits -- and edit_slack_list takes that id. Read "
            "the new List back with read_slack_list and include_list true to "
            "collect them before writing a row."
            "\nCopying an existing List. Pass copy_from_list_id to take another "
            "List's columns, and include_copied_list_records to take its rows "
            "as well. A copy and a schema cannot both be given: the copy "
            "already decides the columns."
            "\ntodo_mode adds Slack's own task-tracking columns -- Completed, "
            "Assignee and Due Date -- on top of whatever the schema asks for. "
            "Use it for a list of things to do rather than building those "
            "three by hand."
            "\nThe description is optional. Write it as Markdown in "
            "description, or pass the Block Kit yourself as "
            "description_blocks."
            "\nWho can see it. Nobody, at first. A List made here is visible "
            "to this app alone until it is shared, and creating it notifies "
            "nobody."
            "\nWhat comes back. The new List's identifier, which every other "
            "List call takes."
            "\nLists are a paid Slack feature, and a workspace has a ceiling "
            "on how many it may hold."
        ),
        input_params={
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "The List's name.",
                },
                "description": {
                    "type": "string",
                    "description": (
                        "What the List is for, in Markdown. Cannot be given "
                        "with description_blocks."
                    ),
                },
                "description_blocks": {
                    "type": "array",
                    "items": {"type": "object"},
                    "description": (
                        "The description as Block Kit, supplied verbatim "
                        "instead of description. Use it when the Markdown "
                        "rendering is not what is wanted."
                    ),
                },
                "schema": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "key": {
                                "type": "string",
                                "description": (
                                    "The column's own key, chosen here. It "
                                    "names the column in this schema and "
                                    "nowhere else: writing a cell later takes "
                                    "the id Slack generates beside it, not "
                                    "this."
                                ),
                            },
                            "name": {
                                "type": "string",
                                "description": (
                                    "What the column is called on screen."
                                ),
                            },
                            "type": {
                                "type": "string",
                                "description": (
                                    "The column's type. Slack's own example "
                                    "writes out: "
                                    + ", ".join(COLUMN_TYPES[:20])
                                    + ". todo_mode adds "
                                    + ", ".join(COLUMN_TYPES[20:])
                                    + ". Those are what Slack documents rather "
                                    "than a closed set this tool enforces."
                                ),
                            },
                            "is_primary_column": {
                                "type": "boolean",
                                "description": (
                                    "Set on the one column that titles each "
                                    "row."
                                ),
                            },
                            "options": {
                                "type": "object",
                                "description": (
                                    "The column's own settings, whose shape "
                                    "depends on its type -- a select's "
                                    "choices, a number's precision, a rating's "
                                    "emoji and maximum."
                                ),
                            },
                        },
                    },
                    "description": (
                        "The columns, in Slack's own column-definition shape. "
                        "Omit it for a single text column. These columns are "
                        "permanent: nothing can change them afterwards."
                    ),
                },
                "copy_from_list_id": {
                    "type": "string",
                    "description": (
                        "Create this List as a copy of another one, taking its "
                        "columns. Cannot be given with schema."
                    ),
                },
                "include_copied_list_records": {
                    "type": "boolean",
                    "default": False,
                    "description": (
                        "Copy the other List's rows as well as its columns. "
                        "Needs copy_from_list_id."
                    ),
                },
                "todo_mode": {
                    "type": "boolean",
                    "default": False,
                    "description": (
                        "Add Slack's task-tracking columns -- completed, "
                        "assignee, due date -- which do not count against the "
                        "column limit."
                    ),
                },
            },
            "required": ["name"],
        },
    )


def _edit_card() -> ToolCard:
    """The card for ``edit_slack_list``.

    Six things have to be said, and two of them were learned from a model that
    spent six refused calls on them. A column id is Slack's generated id rather
    than the key a schema was written with, and Slack refuses the mistake with
    ``invalid_arguments``, which names no argument. A cell value is almost
    always an array, ``checkbox`` alone excepted. The other four: a cell cannot
    be written as plain text, which is counterintuitive enough that Slack says
    so itself; writing cells is a batch across rows and columns despite the
    singular method name; a multiple delete reports nothing per item, so a
    caller cannot read success as *all of them*; and no operation here changes
    a column, which is where a model would otherwise look after finding the
    create tool's schema argument.
    """
    return ToolCard(
        name="edit_slack_list",
        description=(
            "Change a Slack List: add a row, write cells, delete rows, or "
            "rename the List."
            "\nPick one with operation. create_item adds a row. update_cells "
            "writes values into cells. delete_item removes one row. "
            "delete_items removes several. update_list changes the List's own "
            "name, description or todo mode."
            "\nIt cannot change a column. No Slack method adds, removes, "
            "renames or retypes a column, so a List whose columns are wrong "
            "has to be recreated. update_list covers the name, the description "
            "and todo mode and nothing else."
            "\nWriting cells is a batch. One update_cells call carries a cells "
            "list that may span many rows and many columns at once, each entry "
            "naming its row_id and its column_id. An entry with "
            "row_id_to_create set to true creates a row as part of the same "
            "call."
            + _COLUMN_ID_RULE
            + _CELL_SHAPES
            + "\nText cells are the one Slack spells out itself: a text "
            "cell is rich text, and plain text is not accepted in a request "
            "however it appears in a response. Write the value as Markdown "
            "in text and it is converted, or build the payload yourself and "
            "pass it as rich_text. Passing both is refused."
            + _TEXT_COVERAGE
            + "\nAdding a row. create_item makes an empty row, or one filled "
            "in by initial_fields, which takes the same cell entries "
            "update_cells does. duplicated_item_id copies an existing row "
            "instead. parent_item_id makes the new row a subtask of another "
            "row, which is how a List becomes a tree rather than a table."
            "\nDeleting several rows at once. Slack reports no per-row result "
            "for delete_items, so a call that comes back without an error does "
            "not establish that every named row was removed, and one that "
            "fails does not establish that none were. Read the List back to "
            "find out. delete_item, one row at a time, is the one that answers "
            "for itself."
            "\nRow and item are the same thing. Slack's methods say item and "
            "its responses say record; both mean a row."
        ),
        input_params={
            "type": "object",
            "properties": {
                "list_id": {
                    "type": "string",
                    "description": (
                        "The List to change, such as F1234ABCD."
                    ),
                },
                "operation": {
                    "type": "string",
                    "enum": sorted(EDIT_OPERATIONS),
                    "description": "What to do.",
                },
                "cells": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "row_id": {
                                "type": "string",
                                "description": "The row to write into.",
                            },
                            "row_id_to_create": {
                                "type": "boolean",
                                "description": (
                                    "Create a new row for this cell instead of "
                                    "writing into an existing one."
                                ),
                            },
                            "column_id": {
                                "type": "string",
                                "description": (
                                    "The column to write into, named by the "
                                    "id Slack generated for it -- Col "
                                    "followed by letters and digits. Not the "
                                    "key a schema was written with: Slack "
                                    "keeps that key and answers with an id "
                                    "beside it, and the id is what a write "
                                    "takes. Read the List with include_list "
                                    "true and take it from the schema there."
                                ),
                            },
                            "text": {
                                "type": "string",
                                "description": (
                                    "The value of a text cell, as Markdown. "
                                    "Converted to the rich text Slack "
                                    "requires. Cannot be given with rich_text."
                                ),
                            },
                            "rich_text": {
                                "type": "array",
                                "items": {"type": "object"},
                                "description": (
                                    "The value of a text cell as Block Kit, "
                                    "supplied verbatim instead of text: an "
                                    "array holding a rich_text block."
                                ),
                            },
                        },
                        "description": (
                            "One cell. Besides the keys above it carries one "
                            "value whose key is its column's type -- select, "
                            "user, date, number, checkbox, email, phone and "
                            "so on. Every documented value but checkbox is "
                            "an array, including the ones whose column holds "
                            "a single thing; the tool's own description "
                            "lists the shape each key takes."
                        ),
                    },
                    "description": (
                        "For update_cells: the cells to write, across any "
                        "number of rows and columns."
                    ),
                },
                "initial_fields": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "column_id": {
                                "type": "string",
                                "description": (
                                    "The column this value goes in, named by "
                                    "the id Slack generated for it -- Col "
                                    "followed by letters and digits, not the "
                                    "key a schema was written with. Read the "
                                    "List with include_list true to get them."
                                ),
                            },
                            "text": {
                                "type": "string",
                                "description": (
                                    "The value of a text cell, as Markdown. "
                                    "Converted to the rich text Slack "
                                    "requires. Cannot be given with "
                                    "rich_text."
                                ),
                            },
                            "rich_text": {
                                "type": "array",
                                "items": {"type": "object"},
                                "description": (
                                    "The value of a text cell as Block Kit, "
                                    "supplied verbatim instead of text: an "
                                    "array holding a rich_text block."
                                ),
                            },
                        },
                        "description": (
                            "One cell of the new row. Besides column_id it "
                            "carries one value whose key is its column's "
                            "type, and every documented value but checkbox "
                            "is an array; the tool's own description lists "
                            "the shape each key takes. A row id is neither "
                            "given nor needed -- the row is being made."
                        ),
                    },
                    "description": (
                        "For create_item: the new row's cells, in the same "
                        "shape the cells argument takes minus the row keys."
                    ),
                },
                "duplicated_item_id": {
                    "type": "string",
                    "description": (
                        "For create_item: copy this existing row rather than "
                        "making an empty one."
                    ),
                },
                "parent_item_id": {
                    "type": "string",
                    "description": (
                        "For create_item: make the new row a subtask of this "
                        "row."
                    ),
                },
                "item_id": {
                    "type": "string",
                    "description": "For delete_item: the row to remove.",
                },
                "item_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "For delete_items: the rows to remove. Slack reports "
                        "no per-row result for this."
                    ),
                },
                "name": {
                    "type": "string",
                    "description": "For update_list: the List's new name.",
                },
                "description": {
                    "type": "string",
                    "description": (
                        "For update_list: the List's new description, in "
                        "Markdown. Cannot be given with description_blocks."
                    ),
                },
                "description_blocks": {
                    "type": "array",
                    "items": {"type": "object"},
                    "description": (
                        "For update_list: the new description as Block Kit, "
                        "supplied verbatim instead of description."
                    ),
                },
                "todo_mode": {
                    "type": "boolean",
                    "description": (
                        "For update_list: turn Slack's task-tracking columns "
                        "on or off."
                    ),
                },
            },
            "required": ["list_id", "operation"],
        },
    )


def _share_card() -> ToolCard:
    """The card for ``share_slack_list``.

    The partial failure is the reason this card is long. Slack refuses a
    multi-target call as a whole and names none of the targets, so a caller
    reading the overall result would learn nothing about four of five people,
    and the three lists are the answer to a question the API does not answer.
    """
    return ToolCard(
        name="share_slack_list",
        description=(
            "Set who may read or write a Slack List, or take that access away."
            "\nConversations or people, never both in one call. Pass chat_ids "
            "to act on conversations, or user_ids to act on people. Both are "
            "lists. Make two calls when both are needed."
            "\nThe levels are read, write and owner. owner transfers ownership "
            "and applies to exactly one person: it cannot be given to a "
            "conversation, and it cannot be given to several people at once."
            "\nTaking access away. Pass remove instead of a level, with the "
            "same targets."
            "\nWhen some targets take and others do not. Slack refuses such a "
            "call as a whole and does not say which of them applied, so each "
            "target is then tried on its own and the result lists them "
            "separately: which took, which did not and why, and which were "
            "never determined because the call ran out of time. Read the lists "
            "rather than the overall result."
            "\nSharing a List with a conversation makes it reachable by "
            "everybody in that conversation. It notifies nobody; to tell "
            "people it exists, say so."
        ),
        input_params={
            "type": "object",
            "properties": {
                "list_id": {
                    "type": "string",
                    "description": (
                        "The List whose access is being set, such as F1234ABCD."
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
                        "together with chat_ids."
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
            "required": ["list_id"],
        },
    )


def _delete_card() -> ToolCard:
    """The card for ``delete_slack_list``.

    A tool of its own rather than a flag on another, because
    ``permissions.tools`` is keyed by name: one tool is one policy, and folding
    deletion into the editing tool would make *may write a List, may not delete
    one* inexpressible.
    """
    return ToolCard(
        name="delete_slack_list",
        description=(
            "Delete a Slack List."
            "\nIt cannot be undone. Slack keeps no copy and has no undelete: "
            "every row the List held is gone, along with every link anybody "
            "has to it, and nobody is told. Delete a List when somebody asked "
            "for it to be deleted."
            "\nOnly a List. The id is checked against Slack before anything is "
            "deleted, and an id naming anything else -- a canvas, an uploaded "
            "file, an image -- is refused and nothing happens to it."
            "\nDeleting files can be switched off for a whole workspace, in "
            "which case this fails and says so, and nothing but an "
            "administrator change will alter that."
        ),
        input_params={
            "type": "object",
            "properties": {
                "list_id": {
                    "type": "string",
                    "description": (
                        "The List to delete, such as F1234ABCD."
                    ),
                },
            },
            "required": ["list_id"],
        },
    )


__all__ = [
    "ACCESS_LEVELS",
    "CELL_VALUE_SHAPES",
    "COLUMN_TYPES",
    "EDIT_OPERATIONS",
    "EXPORT_FORMATS",
    "LIST_TOOL_NAMES",
    "MAX_EXPORT_BYTES",
    "MAX_EXPORT_CHARS",
    "MAX_ITEMS_PER_PAGE",
    "SlackListToolkit",
    "UNDOCUMENTED_CELL_TYPES",
    "slack_list_request_metadata",
]
