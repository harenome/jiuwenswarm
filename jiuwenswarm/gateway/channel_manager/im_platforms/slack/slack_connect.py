"""Slack channel implementation based on Slack Bolt Socket Mode."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import urlparse

import httpx

from jiuwenswarm.common.schema.message import EventType, Message, ReqMethod
from jiuwenswarm.common.slack_file_transfer import (
    FILE_TRANSFER_TIMEOUT_SECONDS,
    MAX_FILE_BYTES,
    SLACK_FILE_HOST,
    UNSAFE_PATH_CHARS_RE,
    SlackFileTransferRefused,
    slack_file_transport,
    stream_slack_file,
)
from jiuwenswarm.common.slack_events_policy import (
    EVENT_CONTEXT,
    EVENT_OFF,
    EVENT_TURN,
    EVENT_TYPE_FAMILIES,
    KEY_EVENTS,
    InboundEventBuffer,
    event_actor_id,
    event_anchor,
    event_chat_id,
    event_disposition,
    event_family,
    family_anchors_on_a_message,
    normalize_event_policy,
    render_event,
)
from jiuwenswarm.common.slack_history_policy import (
    HISTORY_POLICY_DEFAULT,
    KEY_HISTORY,
    history_policy_metadata,
    id_list,
    normalize_history_policy,
)
from jiuwenswarm.common.slack_write_policy import (
    WRITE_POLICY_DEFAULT,
    KEY_WRITE,
    resolve_write_policy,
    write_policy_metadata,
)
from jiuwenswarm.common.slack_rich_text import (
    RICH_TEXT_KEYS,
    walk_strings,
)
from jiuwenswarm.common.slack_scope_policy import (
    NARROWINGS,
    TIER_CORE,
    TIER_TOOLS,
    scope_reasons,
    scopes_for_tier,
)
from jiuwenswarm.common.scopes import (
    AXIS_CHAT_TYPE,
    CLICK_APPROVE,
    CLICK_STOP,
    KEY_MID_TURN,
    KEY_REPLY,
    KEY_SESSION,
    MID_TURN_DEFAULT,
    MID_TURN_QUEUE,
    MID_TURN_STEER,
    MID_TURN_VALUES,
    PEOPLE_KEY,
    REPLY_OPTIONAL,
    ROLES_KEY,
    SECTION_AGENT,
    SECTION_DELIVERY,
    SESSION_CHANNEL,
    SESSION_DEFAULT,
    SESSION_THREAD,
    SESSION_VALUES,
    Scope,
    click_rule,
    compile_scopes,
    compose_section,
    scoped_chats,
)
from jiuwenswarm.common.utils import get_agent_sessions_dir
from jiuwenswarm.gateway.channel_manager.base import (
    BaseChannel,
    ChannelMetadata,
    RobotMessageRouter,
)
from jiuwenswarm.gateway.channel_manager.im_platforms.platform_adapter.streaming_session import (
    StreamingSession,
)
from jiuwenswarm.common import slack_blocks
from jiuwenswarm.common.slack_text import (
    MARKDOWN_FENCE_CLOSE_RE,
    MARKDOWN_FENCE_OPEN_RE,
    MAX_SLACK_TEXT_LENGTH,
    MAX_SLACK_UPDATE_TEXT_LENGTH,
    normalize_slack_mrkdwn,
    preferred_split_index,
    split_text,
)
from jiuwenswarm.gateway.channel_manager.im_platforms.slack import (
    slack_inputs,
)
from jiuwenswarm.gateway.channel_manager.im_platforms.slack.scope_capabilities import (
    SLACK_CAPABILITIES,
)
from jiuwenswarm.gateway.channel_manager.im_platforms.slack.slack_dedup import (
    SlackEventDedupStore,
)
from jiuwenswarm.common.slack_routing import (
    SLACK_CHANNEL_ID,
    parse_slack_cron_session,
)
from jiuwenswarm.gateway.routing.keys import SlackDeliveryTarget
from jiuwenswarm.gateway.routing.session_sharing import RoutingTarget

logger = logging.getLogger(__name__)

try:
    from slack_bolt.adapter.socket_mode.aiohttp import AsyncSocketModeHandler
    from slack_bolt.async_app import AsyncApp

    SLACK_AVAILABLE = True
except ImportError:
    SLACK_AVAILABLE = False
    AsyncApp = None  # type: ignore[assignment,misc]
    AsyncSocketModeHandler = None  # type: ignore[assignment,misc]


_HTTP_URL_RE = re.compile(r"https?://[^\s<>()|]+", re.IGNORECASE)
# Slack renders a user mention as "<@U01ABCDEF>", optionally with a "|label"
# suffix on older payloads.
_USER_MENTION_RE = re.compile(r"<@([UW][A-Z0-9]+)(?:\|[^>]*)?>")
# The Markdown-to-mrkdwn rules now live in ``common/slack_text``, which the
# agent runtime can import and this module cannot be imported by. Only the two
# fence patterns are still named here, because a second scan in this file reads
# them directly.
_MARKDOWN_FENCE_OPEN_RE = MARKDOWN_FENCE_OPEN_RE
_MARKDOWN_FENCE_CLOSE_RE = MARKDOWN_FENCE_CLOSE_RE
_DEFAULT_ACKNOWLEDGEMENT_TEXT = "Received. Analyzing…"
# Written in place of an emoji name to turn one of the six marks off on its own,
# leaving the other five working. Before this there was no such lever: an empty
# value falls back to the default, and the only switch was ``acknowledge_mode:
# off``, which removes every mark at once including the ones a workspace finds
# useful.
#
# ``disabled`` and not ``off`` because the config is read as YAML 1.1, which
# resolves a bare off to the boolean false -- it is the one spelling an operator
# could not write without quoting it. The narrowest word of
# ``channels.slack.history`` and ``channels.slack.write`` is spelled this way
# for the same reason, so the file already reads one word for "not at all".
#
# Matched against the configured value before the surrounding colons are
# stripped, so a workspace that really does have a custom emoji named
# ``disabled`` asks for it as ``:disabled:`` and still gets it.
REACTION_DISABLED = "disabled"
_DEFAULT_ACKNOWLEDGEMENT_EMOJI = "eyes"
_DEFAULT_REJECTED_EMOJI = "no_entry_sign"
_DEFAULT_QUEUED_EMOJI = "hourglass_flowing_sand"
# The three endings, completing a six-state vocabulary with the three above.
#
# Picked apart by outline rather than by hue, because a reaction is drawn small
# and a reader scanning a conversation sees shape before colour -- and some
# readers cannot use colour at all. The set is a ring, an hourglass, a tick, a
# cross, a square and a barred circle; an addition should keep that property.
#
# ``heavy_check_mark`` rather than the boxed variant, because every processed
# message gets one and a wall of green boxes down a conversation the agent
# reads all day is louder than the fact deserves.
#
# ``x`` rather than a fire or an explosion, both of which already read as
# approval or as a joke in chat. A failure mark that looks like enthusiasm is
# worse than no mark.
#
# ``black_square_for_stop`` rather than a stop sign, because the octagon reads
# as "blocked", which is what the refusal mark already means. The media square
# reads as "halted", which is what happened.
_DEFAULT_COMPLETED_EMOJI = "heavy_check_mark"
_DEFAULT_FAILED_EMOJI = "x"
_DEFAULT_STOPPED_EMOJI = "black_square_for_stop"
DEFAULT_THINKING_STATUS = "is thinking…"
# Two siblings of DEFAULT_THINKING_STATUS, one per event that used to leave
# "is thinking…" standing after it stopped being true: a message held behind a
# running turn, and a message folded into one. Worded as predicates, like
# thinking_status, and never in the second person -- the status is broadcast to
# the whole thread, not addressed to whoever sent the message that caused it,
# so "your message" would lie for everyone else reading it.
#
# thinking_status stays continuous ("is thinking…") because it names an action
# genuinely in progress for as long as it is showing. These two name a
# discrete event instead -- the queueing or the steering already happened by
# the time either is drawn -- so both read in the present perfect, and neither
# carries thinking_status's ellipsis: nothing is still unfolding for a reader
# to wait out.
_DEFAULT_QUEUED_STATUS = "has queued a message"
_DEFAULT_STEERED_STATUS = "has steered a message into the running turn"

# The middle rung of the progress ladder: a status line Slack draws in the
# thread, between the instant acknowledgement and the activity card that only
# appears once a turn has been working for activity_card_delay_seconds.
#
# The legacy method, and not the agents.sessions.setStatus that replaced it:
#
# * it clears itself. Slack drops the status when the app posts into the thread
#   and, failing that, two minutes after it was set, so nothing here owns a
#   timer, a task or a piece of state and no failure path can leave a thread
#   claiming forever that it is thinking. The replacement holds "processing" for
#   an hour when a clear is missed.
# * it takes free text, so the wording is the operator's; the replacement has
#   four fixed lifecycle values.
# * chat:write is enough, which this app already holds. agents.sessions.setStatus
#   answers not_authorized without a feature toggle this app does not have.
#
# slack_sdk has no wrapper for it in the version pinned here, so it goes through
# the same generic escape hatch blocks.validate uses.
_ASSISTANT_SET_STATUS_METHOD = "assistant.threads.setStatus"

ACK_MODE_REACTION = "reaction"
ACK_MODE_TEXT = "text"
ACK_MODE_BOTH = "both"
ACK_MODE_OFF = "off"
ACKNOWLEDGE_MODES = (ACK_MODE_REACTION, ACK_MODE_TEXT, ACK_MODE_BOTH, ACK_MODE_OFF)


# Message subtypes that hold a user's own content rather than a system notice.
#
# Slack tags an upload as ``file_share``: the poster stays in ``user``, the
# comment stays in ``text``, and the files arrive in ``files``. It tags a thread
# reply the sender also sent to the channel as ``thread_broadcast``, which is an
# ordinary reply the sender chose to make visible at root: same ``user``, same
# ``text``, same ``thread_ts``, and the root message repeated under ``root``
# where nothing here reads it.
#
# Every other subtype -- ``message_changed``, ``message_deleted``,
# ``channel_join``, ``bot_message`` and the rest -- is an edit, a join notice or
# an echo, and is still discarded unread. An edit stays out deliberately rather
# than for want of handling: re-running a turn on content already answered would
# reply twice to one message, and Slack emits ``message_changed`` for its own
# unfurl of a link the sender posted, which is a message the connector has
# already seen.
#
# An allow-list, so a subtype nobody anticipated is dropped rather than
# dispatched. That is the safe direction for a loop guard and the unsafe one for
# sender content: anything genuinely written by a person has to be named here or
# it is silently discarded. ``me_message`` and ``slackbot_response`` are the
# remaining candidates and are absent because the inbound shape of neither has
# been seen.
_USER_CONTENT_SUBTYPES = frozenset({"file_share", "thread_broadcast"})

# The keys an ``attachments`` entry puts reader-facing text under: the Block Kit
# set plus the four that only an attachment has. An unfurl names its source in
# ``service_name`` and its target in ``title_link``, a shared message names who
# wrote it in ``author_name``, and an attachment field states its content under
# ``value`` beside the ``title`` already in the set. ``image_url`` and
# ``thumb_url`` stay out for the reason ``image_url`` stays out of the Block Kit
# set: a picture is not text, and its URL describes nothing.
_ATTACHMENT_TEXT_KEYS = RICH_TEXT_KEYS | {
    "author_name",
    "service_name",
    "title_link",
    "value",
}

# How much of one message's attachments reaches the agent's prompt. The blocks
# nested inside an attachment can run to a screenful of an app's rendering, and
# what is lifted is a description of what was posted rather than the posted
# thing: past a few paragraphs it stops describing the message and starts
# crowding out the conversation around it.
_MAX_ATTACHMENT_TEXT_CHARS = 2000

# How many attachment names to name individually before summarizing the rest.
_FILE_NOTICE_MAX_NAMES = 5
_FILE_REASON_DOWNLOAD_FAILED = "the download from Slack failed"
# The one reason an absent ``files:read`` earns, whichever half of the
# attachment path meets it: the ``files.info`` lookup that resolves a partial
# ``file_share`` event, or the bearer GET on ``url_private``. Both need that one
# grant -- ``slack_scope_policy`` records it against ``files_info`` -- so an
# operator reading this does not have to know which of the two ran.
_FILE_REASON_UNAUTHORIZED = (
    "Slack refused the download; the bot token is missing the files:read scope"
)
# Slack's word for "this token was not granted what this call needs". It is the
# only refusal code that names something an operator can act on by granting it,
# which is why it is the only one mapped to the reason above.
_MISSING_SCOPE_ERROR = "missing_scope"
_FILE_REASON_TOO_LARGE = "the file is over the size limit this bot will download"
_FILE_REASON_TIMED_OUT = "the download took too long and was given up on"
_FILE_REASON_OFF_SLACK = (
    "the file is stored outside Slack, so the bot did not fetch it; open it"
    " from its permalink in Slack instead"
)
# Separate from the one above, because the two are different findings for
# whoever has to act on them. That one says the file record named somewhere
# other than Slack. This one says the record named Slack and the network did
# not: the host resolved to an address that is not on the public internet, or
# the connection could not be pinned to a validated address at all. The user
# can do nothing about either, so both end in the permalink; the log line is
# where they are told apart, and it carries the refusal's own code.
_FILE_REASON_UNSAFE_ROUTE = (
    "the download would not have reached Slack, so the bot did not fetch it;"
    " open the file from its permalink in Slack instead"
)

# ``SLACK_FILE_HOST``, ``MAX_FILE_BYTES``, ``FILE_TRANSFER_TIMEOUT_SECONDS`` and
# ``UNSAFE_PATH_CHARS_RE`` are imported from
# ``jiuwenswarm.common.slack_file_transfer``, which is where the reasoning for
# each of them lives. They are shared with the runtime's Slack file tool, which
# makes the same four decisions about the same transfer and must not import this
# module. The host in particular decides which host may be sent the bot token.

# Wall-clock budget for fetching every attachment on one message, measured
# across the whole phase rather than per file. A per-operation timeout leaves
# two ways to wait indefinitely -- a slow trickle that resets the read timeout
# forever, and a batch of files whose individual timeouts add up -- and the
# handler is holding the user's request open throughout. Whatever has not
# arrived by the deadline is reported as unread rather than waited on.
_ATTACHMENT_PHASE_TIMEOUT_SECONDS = 120.0

GROUP_MODE_MENTION = "mention"
GROUP_MODE_REPLY = "reply"
GROUP_MODE_ALL = "all"
GROUP_MODE_OFF = "off"
GROUP_CHAT_MODES = (
    GROUP_MODE_MENTION,
    GROUP_MODE_REPLY,
    GROUP_MODE_ALL,
    GROUP_MODE_OFF,
)

# What a channel's ``mode`` list may name. Each is a pre-model predicate: it
# reads the event Slack already delivered, does no I/O, makes no LLM call and
# creates no session. That property is load-bearing rather than incidental --
# the acknowledgement reaction is posted, and the session created, before the
# model is ever consulted, so "let the model decide whether to answer" cannot
# be a trigger. Anything added here must be answerable from the payload alone.
TRIGGER_MENTION = "mention"
TRIGGER_REPLY = "reply"
TRIGGER_ALL = "all"
TRIGGER_URL = "url"
TRIGGER_HAS_FILE = "has_file"
CHANNEL_MODE_TRIGGERS = (
    TRIGGER_MENTION,
    TRIGGER_REPLY,
    TRIGGER_ALL,
    TRIGGER_URL,
    TRIGGER_HAS_FILE,
)

# What woke a turn that no message woke: a reaction or a pin under
# ``delivery.events: turn``.
#
# **Deliberately not in CHANNEL_MODE_TRIGGERS.** ``mode`` is a list of
# predicates over an inbound *message*, and this is not one -- there is no
# message for it to be true or false of. Writing it in a ``mode`` list is a
# typo, and the mode validator refuses it as one. What this name is for is the
# label: the turn it starts is labelled with it, so the model can tell that
# nothing addressed it, and the outcome log names it the way it names the
# others.
TRIGGER_EVENT = "event"

# Which entries in a ``mode`` claim a message that addressed the bot. Two rather
# than one because ``all`` is a superset of every other trigger, and a mention
# is the only one that does not reach _match_channel_trigger to be told so:
# Slack delivers it on its own app_mention subscription, which is routed before
# any trigger is matched. Spelled out here so the two routes cannot disagree
# again -- they did, and a conversation set to answer everything ignored the one
# message addressed to it.
MENTION_ROUTE_TRIGGERS: frozenset[str] = frozenset({TRIGGER_MENTION, TRIGGER_ALL})


def _slack_action_token(
    event: Mapping[str, Any], body: Mapping[str, Any]
) -> str:
    """The per-event permission Slack issues, or the empty string.

    Slack documents it on the event object, and that is where both of this
    connector's subscriptions put it: ``app_mention``, and the ``message``
    family in a direct message. The envelope is read as a fallback rather than
    as an alternative -- an event delivered through Socket Mode is wrapped in a
    ``payload`` that has held fields the inner object did not, and reading
    both costs one dictionary lookup on a path that already does several.

    Absent far more often than present: Slack issues one only for a message
    that addressed the app, so an ordinary channel message the bot answers
    under the ``all`` trigger has none. That is not an error, and the empty
    string it returns is how the caller declines to publish the key at all.
    """
    for source in (event, body):
        if not isinstance(source, Mapping):
            continue
        value = str(source.get("action_token") or "").strip()
        if value:
            return value
    return ""


# How the legacy single-string group_chat_mode reads as a trigger set. "reply"
# and "all" include "mention" because Slack delivers mentions on a separate
# app_mention subscription that neither of those modes switches off, and "off"
# is the empty set. The table is Slack's alone: Telegram's modes are exclusive,
# so the same strings there would map to {reply} and {all}, and unifying the two
# would silently change what live Telegram bots answer.
_LEGACY_MODE_TRIGGERS: dict[str, frozenset[str]] = {
    GROUP_MODE_MENTION: frozenset({TRIGGER_MENTION}),
    GROUP_MODE_REPLY: frozenset({TRIGGER_MENTION, TRIGGER_REPLY}),
    GROUP_MODE_ALL: frozenset({TRIGGER_MENTION, TRIGGER_ALL}),
    GROUP_MODE_OFF: frozenset(),
}

# Which dispatches name, to the model, the predicate that woke the bot. One word
# per predicate the whole way through: the name ``_handle_slack_event`` is
# dispatched with is the name an operator writes in ``mode``, the vocabulary a
# channel prompt is written against, and the value ``metadata["slack_trigger"]``
# holds. A set rather than a table, because there is nothing to translate.
#
# "dm" is absent: a direct message is governed by no channel trigger, so there
# is no predicate to name.
#
# ``event`` is here and is in no ``mode`` list. It names the one predicate a
# message did not satisfy, because there was no message: the turn was started
# by a reaction or a pin. The label is the whole of why the name exists, and
# reusing this mechanism rather than writing a second one is what keeps one
# vocabulary in front of the model.
_LABELLED_TRIGGERS: frozenset[str] = frozenset(
    {
        TRIGGER_MENTION,
        TRIGGER_REPLY,
        TRIGGER_ALL,
        TRIGGER_URL,
        TRIGGER_HAS_FILE,
        TRIGGER_EVENT,
    }
)

# The triggers whose reason for waking the bot the message itself does not
# state. A mention is in the text, a reply is in the thread and "all" is the
# whole channel -- a reader, human or model, can see why the bot answered. A
# link posted in a watched channel that addresses nobody cannot be told apart
# from any other message, so for these two the label is the only account of why
# there is a reply at all, and it is emitted whether or not a prompt follows it.
_SELF_EXPLAINING_TRIGGERS: frozenset[str] = frozenset(
    {TRIGGER_MENTION, TRIGGER_REPLY, TRIGGER_ALL}
)

# The rendered form. A bracketed label rather than a sentence: appended prose
# sits next to the user's message as more message, and has been observed being
# copied into a file as though it were content the user asked for. A bracket
# reads as metadata to a model and to a human skimming the thread. Not
# configurable -- it states a fact the connector knows rather than
# operator-authored behaviour.
_TRIGGER_LABEL_FORMAT = "[trigger: {name}]"


def trigger_label(trigger: str, *, has_prompt: bool) -> str:
    """The label naming ``trigger``, or ``""`` when none is emitted.

    A channel prompt is appended -> always label, on every trigger, ``mention``
    included. The label is context *for* that prompt, and a prompt author can
    only branch on something guaranteed to be there.

    No prompt is appended -> label only the triggers in ``mode`` that the message
    does not explain by itself, which is ``url`` and ``has_file``. Everything
    else is inferable from what was posted, so a label would be noise on the
    highest-volume path there is. ``event`` falls on that side too and is
    labelled always: there is no message for a reader to infer anything from.

    ``has_prompt`` is whether a prompt was resolved for this dispatch, not
    whether it was ultimately concatenated. A prompt the caller drops because
    the user already pasted the same text is still in the message and still
    governing, so the label it is context for stays.
    """
    if trigger not in _LABELLED_TRIGGERS:
        return ""
    if not has_prompt and trigger in _SELF_EXPLAINING_TRIGGERS:
        return ""
    return _TRIGGER_LABEL_FORMAT.format(name=trigger)


# The rendered form of the location marker. Bracketed like the trigger label
# above and for the reason that one is. It states a fact about the envelope
# that the sender never wrote, and a bracket reads as metadata to a model and
# to a human skimming the thread.
#
# ``ts`` and ``thread_ts`` are the words the common message schema, the history
# tool and the search tool already use, so a value read off this marker goes
# into either tool with no translation. The ``key: value`` shape is there for
# the same reason: the marker has to read as something a tool takes. "in the
# thread started at 14:31" would not.
#
# The conversation is left out. ``read_slack_conversation`` does declare
# ``chat_id`` wherever the settled history policy lets a request name a target,
# so the field could be filled in. The value would be inert: omitting the
# argument reads the conversation the request came from, and passing that
# conversation's own id means the same thing, since no membership is read for a
# conversation to read itself. The card also restricts the argument to a
# conversation somebody named, and a marker supplying an id on every inbound
# message pushes against that.
#
# What decides it is that ``ts`` and ``thread_ts`` have no such default.
# Nothing else in a turn states them, so an agent cannot otherwise learn the
# position of the message it is answering, which leaves ``ts``, ``after_ts``
# and ``before_ts`` reachable only when a person pastes a value into the
# prompt. The marker names what is unreachable without it and stops there. A
# conversation other than this one is named by a search result or by the person
# asking, and neither of those is the envelope.
#
# Nothing here tells the model to call anything. The tool cards already say
# what the tools take.
_LOCATION_LABEL_FORMAT = "[ts: {ts}]"
_LOCATION_LABEL_THREAD_FORMAT = "[ts: {ts}, thread_ts: {thread_ts}]"


def location_label(ts: str, thread_ts: str) -> str:
    """The marker naming where a message sits, or ``""`` with no ``ts`` to name.

    ``thread_ts`` is left out when the message sits at channel root. An empty
    value would read as a thread whose id went missing on the way here.

    Pass the value from the event and never a fallback that stands in this
    message's own ``ts``. Slack sets the field only on a message posted inside
    a thread. Its presence is the distinction this marker records.
    """
    message_ts = str(ts or "").strip()
    if not message_ts:
        return ""
    root_ts = str(thread_ts or "").strip()
    if root_ts:
        return _LOCATION_LABEL_THREAD_FORMAT.format(ts=message_ts, thread_ts=root_ts)
    return _LOCATION_LABEL_FORMAT.format(ts=message_ts)


# Names the block of events a turn carries, and says nothing else.
#
# A bracket, like the location marker above it, because it states a fact about
# the envelope that the sender never wrote and a bracket delimits itself. What
# follows it is the buffer's payload as JSON, verbatim: the fields are the ones
# read_slack_conversation already publishes, so a value read off this block goes
# into a Slack tool with no translation, exactly as the location marker's do.
#
# **Nothing here tells the model to do anything.** What to do about a reaction
# is a policy: it belongs in a scope's prompt_append, where an operator wrote it
# and can read it back, not in a string compiled into this connector. The block
# states what happened in the room and stops.
_INBOUND_EVENTS_LABEL = "[slack_events]"


def inbound_events_block(payload: "Mapping[str, Any] | None") -> str:
    """One folded buffer as the text a turn carries, or ``""`` for nothing.

    Structured rather than prose, and not summarised. Twenty reactions are
    twenty records: "five people reacted" is a derived representation, and the
    derivation is this connector deciding what mattered.

    ``ensure_ascii`` is off so an emoji name or a display name holding
    non-ASCII reads as itself rather than as escapes. The payload is a plain
    mapping of strings, numbers and lists by construction, so there is nothing
    in it json cannot render.
    """
    if not payload:
        return ""
    return f"{_INBOUND_EVENTS_LABEL}\n{json.dumps(payload, ensure_ascii=False)}"


# Names the one event that started a turn, as against the block above, which
# names the events a turn merely carries. Two labels rather than one, because
# the two say different things about the same JSON: this one is the reason the
# turn exists, and a model reading a single label could not tell which it had.
_EVENT_TURN_LABEL = "[slack_event]"


def event_turn_block(record: "Mapping[str, Any] | None") -> str:
    """The event that woke a turn, as the text that turn opens with.

    The same rendering :func:`inbound_events_block` gives a buffered event, from
    the same ``render_event`` record and in the same field names, so one fact
    reaches the model in one shape whether it woke the turn or was folded into
    it. What kind of event, who did it and which message it was about are all in
    there already, which is why nothing here writes prose about them: a sentence
    naming the actor would be a second spelling of ``author_user_id``, and the
    two would drift.
    """
    if not record:
        return ""
    return f"{_EVENT_TURN_LABEL}\n{json.dumps(record, ensure_ascii=False)}"


# What a turn woken by an event is told about its own situation, and the whole
# of it. Two layers, and they answer different questions.
#
# **Layer one: what happened.** Handled by ``event_turn_block`` above rather
# than by this string, because the facts are the event's and belong in the
# event's own field names. What is left for prose is the one fact the record
# cannot state: that nothing addressed the bot, and that there is therefore no
# question outstanding.
#
# **Layer two: what to do about it, ranked.** A licence that only says "prefer
# silence" is a veto -- the model then stays silent on everything, which is
# indistinguishable from ``off`` and makes the word pointless. So the floor is
# stated *with what beats it*: a convention the conversation itself has written
# down. That ranking is code-owned because it has to be written once and
# written well, and because an operator writing it per room would be writing
# the half that is not theirs.
#
# **A pointer, and deliberately empty of content.** It says that the
# conversation's own instructions decide which events are requests; it does not
# say what any of them are, and nothing an operator writes can change a word of
# this text. That gives the degenerate case the right shape: a deployment that
# switches ``turn`` on and writes no conventions gets a reliably silent model,
# because nothing beat the floor. That configuration is the one somebody tries
# first, so it has to be quiet rather than noisy.
#
# **It promises no destination but the one.** A turn can post its own reply and
# nothing else, so the destination clause says exactly that. Read it as the
# bound it is: there is no facility here for acting somewhere else and no text
# may imply one.
#
# **The destination clause is the only part that varies**, because the
# destination is the only thing that does. A reaction and a pin are about a
# message, so the reply goes in that message's thread; a membership change is
# about the room, so it goes at the channel's top level. The ranking above is
# one text for both, since what beats silence does not depend on where the
# answer would land.
#
# Spelled here, next to ``NO_REPLY_INSTRUCTION``, which is the other half of
# the same contract: this text says silence is usually right, and that one says
# how to express it. Neither is reachable from config, so nothing in a scope
# can misspell either.
EVENT_TURN_RANKING = (
    "That event started this turn. Nobody addressed you and nothing above is a"
    " question. Staying silent is the right outcome for an event unless this"
    " conversation's own instructions say that an event of this kind is a"
    " request; nothing else overrides that."
)

# Where the answer lands, stated because the model cannot see it and because a
# turn deciding whether to speak should know who would hear it.
EVENT_TURN_DESTINATION_THREAD = (
    " If you do answer, your reply is posted in the thread named above, which"
    " is the only place this turn can post anything."
)

# The channel's top level, which is a louder room than a thread: everybody in
# the channel sees it, and nobody asked. That is a fact about the destination
# rather than a second ranking, and it is stated as one -- the sentence above
# is what decides whether to speak at all.
EVENT_TURN_DESTINATION_ROOT = (
    " If you do answer, your reply is posted at the top level of this channel,"
    " where everyone in it sees it rather than only the people following a"
    " thread, and that is the only place this turn can post anything."
)


def event_turn_instruction(*, anchored_on_a_message: bool) -> str:
    """What a turn woken by an event is told, for the destination it has.

    One ranking, two destination clauses, joined here so that the choice is
    made once from the same predicate the routing uses. Nothing in config
    reaches either half.
    """
    return EVENT_TURN_RANKING + (
        EVENT_TURN_DESTINATION_THREAD
        if anchored_on_a_message
        else EVENT_TURN_DESTINATION_ROOT
    )


# The token a conversation may write instead of a reply, and the whole of the
# contract this connector enforces for one. Defined once because three things
# have to agree on the same string: the instruction asking a model to write it,
# the matcher that withholds a reply consisting of it, and the near-miss warning
# that counts the times a model wrote it somewhere the matcher does not look.
#
# Not invented here. Two public projects use this exact token, so a model has
# met the contract before and follows it more readily than one spelled for this
# deployment alone.
NO_REPLY_SENTINEL = "NO_REPLY"

# Zero-width space, zero-width no-break space and word joiner: the three
# invisibles ``\s`` does not cover, Unicode classing them as format characters
# rather than as whitespace. A reply padded with one of them is the token and
# nothing else, and looks that way to whoever is reading the thread.
#
# Spelled as escapes rather than as themselves: written literally they are a
# run of characters no editor, diff or review shows, and one of them going
# missing would narrow the matcher with nothing on screen to say so.
_SENTINEL_INVISIBLE = "\u200b\ufeff\u2060"
_SENTINEL_PADDING = rf"[\s{_SENTINEL_INVISIBLE}]*"

# One anchored pass over the whole reply.
#
# **The anchors are the safety property.** A longer message holding the token
# anywhere is not this, and is delivered untouched.
#
# **Strict on purpose, because the two failures are not symmetric.** A match
# deletes a reply. A matcher that is too relaxed destroys a real answer and
# nobody finds out; one that is too strict posts a visible token that a reader
# notices within a message or two. The recoverable failure is the one to choose,
# so anything doubtful stays delivered.
#
# What is accepted: the token, optionally wrapped in backticks, brackets,
# parentheses or quotes, optionally followed by one ``.`` or ``!``, padded with
# whitespace visible or otherwise. The wrappers are read once and symmetrically,
# around the whole string, in this single pass. Never by stripping and testing
# again -- repeated stripping can reduce a real sentence to the token.
#
# There is deliberately no allow-list of phrases beside it. "I have nothing to
# add", and every other wording a model invents, stays delivered. Such a list has
# no end, and every entry widens the set of real answers that can be deleted.
#
# ``re.DOTALL`` is not set, so nothing here spans a reply whose next line says
# something. Two tokens in a row do not match either: the second one is left over
# where the pattern wants the end of the string.
_NO_REPLY_RE = re.compile(
    rf"^{_SENTINEL_PADDING}[`\[\(\"']*\s*{re.escape(NO_REPLY_SENTINEL)}\s*"
    rf"[`\]\)\"']*[.!]?{_SENTINEL_PADDING}$",
    re.IGNORECASE,
)

# What puts the token in reach of a model, built from the constant above so the
# two cannot drift.
#
# **Code-owned, and that is the point.** An operator who writes this instruction
# writes the token, and a typo there leaks: the model writes what it was told,
# the matcher does not recognise it, and the misspelled token is posted into the
# channel as an ordinary reply. Nothing in the config can spell it, so nothing in
# the config can misspell it.
#
# **Three sentences, and it is appended to every message in a conversation that
# reads them all, so its length is paid on every model call.** It states the
# token, states that the reply must be the token alone, and states what happens
# to anything else. That third sentence is the one the observed failure needs:
# a model with a correct judgement and no way to express it writes a sentence
# describing its silence, and a description of silence is text.
#
# **It says nothing about when to stay silent.** That is the operator's prompt to
# write, and keeping it out of here is what makes the two texts composable rather
# than rival: this one governs the *form* of a decision, the operator's governs
# *when* the decision is taken. An operator prompt demanding an answer to
# everything does not contradict this one -- the model simply never writes the
# token, and the matcher never fires.
NO_REPLY_INSTRUCTION = (
    f"If a message needs no reply, answer with exactly {NO_REPLY_SENTINEL} and"
    " nothing else; nothing is posted. Any other text is posted in full,"
    " including a sentence saying there is nothing to add."
)


# Where the inbound path records, for this one request, whether a token written
# by the turn it started is read as a decision to say nothing rather than posted
# as text. Read on the way out, because the fact that settles it is only in
# reach on the way in: the conversation's own ``delivery.reply``, settled for
# the kind of room this message arrived in.
#
# **This is the matcher, not the offer.** Whether the turn was *told* it may
# answer ``NO_REPLY`` is a second decision, taken beside this one and answered
# differently for a message that addressed the bot: such a message is never
# handed the fragment, because somebody who names the bot should not meet an
# unexplained silence. One variable used to decide both, so withholding the
# offer withheld this key too. That is not the same judgement. A model in a
# conversation the fragment has been appended in before knows the token whether
# or not this particular message carried it, and the one time a turn wrote it on
# an addressed message -- after a person said the answer had been enough -- the
# matcher was disarmed and the bare word went into the channel as ordinary text.
# The offer keeps the address test. This key does not.
#
# Request metadata rather than a map keyed on the request id, because a turn can
# run for many minutes and every bounded map in this connector ages out well
# before that. The gateway merges the whole request metadata into every response
# it builds, which is the same round trip ``_extract_delivery`` already depends
# on for the channel and the thread.
#
# Never trusted on its own. The reader below requires the settled config to agree,
# so this can only narrow the contract for one message and never grant it: a
# response arriving with the key set for a conversation nobody licensed is
# answered the way that conversation already answers.
SLACK_REPLY_OPTIONAL_KEY = "slack_reply_optional"


# Whether the message behind this turn opened with a mention of the bot.
#
# **It decides one log line and nothing else.** No delivery reads it: what a
# withheld reply turns on is the key above and the live config, and both are
# settled without this. What it buys is that the line recording a withheld reply
# says which of the two cases it was. A turn that addressed the bot and got
# nothing back is the newly possible one, and the one somebody goes looking for
# when a person says they were ignored.
#
# Written only when true, the way the key above and the action token are. Absent
# reads as not addressed, which is the ordinary case and the reading every path
# that never stamps it already gets.
SLACK_ADDRESSED_KEY = "slack_addressed"


# The request metadata key that puts a reply at the top level of its channel
# instead of in a thread. Read by ``_extract_delivery``, which applies it once
# over the whole delivery ladder rather than at any one rung -- so it outranks
# every rung, including a routing target carrying a thread of its own.
#
# **Not invented here, and not Slack's.** The scheduler already writes it, for a
# cron result that belongs at channel top level rather than buried in whichever
# thread the job was created from; channels that do not understand it ignore it.
# Spelled once in this module so the writer below and the reader further down
# cannot come to disagree about it. The scheduler's own spelling is a literal in
# a different package and is left alone.
POST_AS_ROOT_KEY = "post_as_root"


# The token as a model might spell it when it is not copying: the underscore
# written as a space or a hyphen, any case, anywhere in the reply. Bounded by
# word boundaries so "no_reply_handler" in a code block is not read as the model
# addressing the connector.
_SENTINEL_SPELLINGS_RE = re.compile(r"\bno[ _-]reply\b", re.IGNORECASE)

# The wordings a turn reaches for when it has no token to write. Anchored over
# the whole reply, and length-bounded: what is being detected is a reply that is
# nothing but a refusal to answer, not an answer that uses these words. The
# bracket forms are first because both observed failures were parentheticals.
_DECLINED_REPLY_PHRASES = "no reply|nothing to add|nothing to say|no comment"
_DECLINED_REPLY_SHAPE_RE = re.compile(
    rf"^[\s\[\(]*(?:{_DECLINED_REPLY_PHRASES})[^)\]]{{0,80}}[\s\)\]\.!]*$",
    re.IGNORECASE,
)


def is_no_reply_sentinel(content: str) -> bool:
    """Whether ``content`` is the sentinel token and nothing else.

    True here means a reply is deleted, so the pattern behind it is deliberately
    narrow. See :data:`_NO_REPLY_RE` for what it accepts and why it accepts no
    more than that.
    """
    return bool(_NO_REPLY_RE.match(content or ""))


def looks_like_a_declined_reply(content: str) -> bool:
    """Whether ``content`` looks like a turn trying to say it has nothing to say.

    **This decides nothing but a log line, and is loose on purpose.** The matcher
    above decides delivery and is therefore strict, because a wrong answer there
    deletes a reply. Nothing here changes what is posted: a false positive costs
    one warning, so the sensible setting is as wide as is useful. The two are
    kept apart in the code for that reason, and neither may be defined in terms
    of the other -- widening the matcher to match this would start deleting real
    answers, and narrowing this to match the matcher is what made it blind.

    Blind is not hypothetical. The first version tested for the token, and both
    of the replies that motivated the feature spell the words with a space where
    the token has an underscore, so the instrumentation would have counted zero
    on the two failures it exists to count.

    So it catches three shapes, and none of them is a delivery rule:

    * the token, however it is spelled -- underscore, space or hyphen, any case,
      anywhere in the reply. That is the model writing the contract somewhere the
      matcher does not look: appended to a real answer, in a wrapper nobody
      anticipated, or discussed in prose.
    * a reply that is wholly a parenthetical and says "no reply", "nothing to
      add" or "nothing to say". Wholly, because a parenthetical inside a real
      answer is an aside rather than a refusal to answer.
    * a short reply that is nothing but one of those phrases, bracketed or not.
      Short, because a long reply that happens to use the words is prose about
      the subject rather than a turn declining to speak.

    This is not the phrase allow-list the matcher refuses to have. That refusal
    is about deletion: a list of phrases that deletes replies has no end and
    every entry widens the set of real answers destroyed. A list that writes a
    log line has a cost of one log line.
    """
    text = (content or "").strip()
    if not text:
        return False
    if _SENTINEL_SPELLINGS_RE.search(text):
        return True
    return bool(_DECLINED_REPLY_SHAPE_RE.match(text))


# Both ceilings are Slack's rather than this connector's, and the posting tools
# in the agent runtime are held to the same two, so they are stated once in
# ``common/slack_text`` and read here under the names this file already used.
_MAX_SLACK_TEXT_LENGTH = MAX_SLACK_TEXT_LENGTH
_MAX_SLACK_UPDATE_TEXT_LENGTH = MAX_SLACK_UPDATE_TEXT_LENGTH
# What one chat.appendStream call accepts in markdown_text -- "up to 12,000
# characters" (docs.slack.dev/reference/methods/chat.appendStream). Three times
# what an edit takes, and it bounds one append rather than the whole answer:
# a reply longer than this is appended in several pieces to the same message
# instead of being split across several messages. No total for a finished
# streamed message is documented anywhere, so none is assumed here.
_MAX_SLACK_APPEND_TEXT_LENGTH = 12000
# Marks a preview as still being written, so a cut prefix does not read as a
# finished but oddly abrupt answer.
_STREAM_TRUNCATION_SUFFIX = "…"
# What goes at the top of the message holding the part Slack dropped. The cut
# cannot be marked where it happened: the message that was cut is longer than
# chat.update will rewrite, that ceiling being what made it worth cutting. So
# the marker leads the continuation instead, immediately below.
_SLACK_TRUNCATION_NOTICE = (
    "…Slack cut the message above short. The rest of it follows."
)
_SLACK_THREAD_DETAILS_MARKER = "<!-- jiuwenswarm:slack-thread-details -->"

# How a reply asks for Block Kit rendering, and how it declines. An inline
# marker rather than a field on the message, as the thread-details marker above
# is: the content is the only thing every producer of a Slack reply has in
# common. A cron push, a skill's report and an ordinary answer all arrive as
# text through the same path, and a new Message field would be unavailable to a
# skill that only writes prose.
_SLACK_BLOCKS_MARKER = "<!-- jiuwenswarm:slack-blocks -->"
_SLACK_BLOCKS_OFF_MARKER = "<!-- jiuwenswarm:slack-blocks:off -->"
# Collapses the gap a stripped marker leaves behind on a line of its own.
_BLANK_RUN_RE = re.compile(r"\n{3,}")

# What channels.slack.blockkit_tables accepts.
#   "off"    -- never render blocks; markers are still stripped so one cannot
#               leak into a channel as literal text.
#   "marker" -- render only when the reply asked for it. Inert unless the
#               operator also puts the marker in front of the model, which
#               nothing in this repository does.
#   "auto"   -- render any Markdown table, unless the reply opted out with the
#               off marker. The default.
BLOCKKIT_TABLES_OFF = "off"
BLOCKKIT_TABLES_MARKER = "marker"
BLOCKKIT_TABLES_AUTO = "auto"
BLOCKKIT_TABLES_MODES = (
    BLOCKKIT_TABLES_OFF,
    BLOCKKIT_TABLES_MARKER,
    BLOCKKIT_TABLES_AUTO,
)
# Named once so the dataclass default, the raw-config reader and the runtime
# fallback cannot drift apart into three different answers.
BLOCKKIT_TABLES_DEFAULT = BLOCKKIT_TABLES_AUTO

# How a reply is shown while it is still being written. **Internal**: the three
# are chosen between per reply, by this connector, and none of them is a value an
# operator writes anywhere. channels.slack.enable_streaming is a boolean and asks
# only whether to preview at all.
#   "off"    -- no preview. The reply is posted once, whole, when the turn ends.
#   "edit"   -- post a message and rewrite it with chat.update roughly once a
#               second. Works anywhere a message can be posted.
#   "stream" -- chat.startStream / chat.appendStream / chat.stopStream. Sends
#               only what is new, takes 12,000 characters per call instead of
#               4,000, and puts the reply's tables and charts in the position
#               they were written in rather than all of them at the end. Needs a
#               thread to reply into and, in a channel, both recipient ids.
#
# The choice is not an operator's to make, because it depends on something only
# this connector can see: whether the reply has a thread. ``_new_stream`` walks
# down from "stream" to "edit" per reply, so a preview is shown wherever one can
# be, and "off" is reached only by being asked for.
STREAMING_OFF = "off"
STREAMING_EDIT = "edit"
STREAMING_STREAM = "stream"
STREAMING_MODES = (STREAMING_OFF, STREAMING_EDIT, STREAMING_STREAM)
# Today's behaviour, for a config that says nothing: a deployment that has not
# asked for a preview must not acquire one.
STREAMING_MODE_DEFAULT = STREAMING_OFF
# What ``enable_streaming: true`` asks for -- the top of the ladder, which every
# reply that cannot take it falls off gracefully. One line, so that preferring
# "edit" instead is a release rather than a config migration.
STREAMING_MODE_ENABLED = STREAMING_STREAM

# The errors that mean "these blocks are unacceptable" rather than "this message
# cannot be delivered". render_blocks validates against the limits it knows
# locally and declines to render when it cannot fit, but Slack is the authority
# on its own payload and can refuse a rendering that passed every local check.
_BLOCK_REJECTION_ERRORS = frozenset(
    {"invalid_blocks", "invalid_blocks_format", "msg_blocks_too_long"}
)

# What ``channels.slack.blockkit_validate`` accepts, and what each mode spends.
#
# ``blocks.validate`` renders nothing, posts nothing and needs no scope, and on
# a refusal it answers with a JSON *pointer* to the offending element. That
# pointer is the whole reason the call exists. A refused send reports
# ``invalid_blocks`` and nothing else, which tells an operator that something in
# a fifty-block payload was wrong and never which thing.
#
# It answers "is this payload well formed", which is weaker than "will this
# send", and the gap is not theoretical. Measured against the live API:
#
#   {"type": "team", "team_id": "T..."} inside a rich_text_section
#       validate approves it; chat.postMessage answers ``internal_error``
#   {"type": "section"} holding neither text nor fields
#       validate approves it, though the block has nothing to render
#
# So a pass is a hint and never a guarantee: it rules out the malformed payloads
# it recognises, and promises nothing about the rest. The post-hoc fallback on a
# refused send is therefore not redundant with this check and must stay -- it is
# the only thing that catches what the validator waves through.
#
# How expensive it is is unknowable: Slack rates the method "Special" and
# publishes no number. The limit is per method, per workspace, per app, so
# validation draws on a budget of its own and cannot starve chat.postMessage.
# ``risky`` is the default: it spends a call where a refusal is both likely and
# expensive to diagnose, and spends nothing on the payloads this connector's own
# typed builders emit by the thousand.
BLOCKKIT_VALIDATE_OFF = "off"
BLOCKKIT_VALIDATE_RISKY = "risky"
BLOCKKIT_VALIDATE_ALL = "all"
BLOCKKIT_VALIDATE_MODES = (
    BLOCKKIT_VALIDATE_OFF,
    BLOCKKIT_VALIDATE_RISKY,
    BLOCKKIT_VALIDATE_ALL,
)
BLOCKKIT_VALIDATE_DEFAULT = BLOCKKIT_VALIDATE_RISKY
# The method itself. slack_sdk 3.43.0 has no wrapper for it, so it goes through
# the generic ``api_call`` escape hatch; naming it here keeps the string out of
# the call site and gives the tests one thing to patch.
_BLOCKS_VALIDATE_METHOD = "blocks.validate"

# Which kinds ``risky`` pays for, and why each one is on the list.
#
# ``fence`` is JSON a model wrote. It is the only source here that was never
# built by a typed function, it is where ``invalid_blocks`` actually comes from
# in practice, and it is the only one whose author can act on a pointer.
#
# ``interactive`` is on the list for the opposite reason: the payload is
# trustworthy and the *failure* is not survivable. Every other kind degrades to
# text stating the same information, while a control that does not draw is a
# question nobody can answer and a turn that stays paused.
#
# ``unknown`` is here so that a call site added later and not classified is
# validated rather than silently skipped; the cost of being wrong in that
# direction is one API call.
_RISKY_BLOCK_KINDS = frozenset(
    {
        slack_blocks.BLOCK_KIND_FENCE,
        slack_blocks.BLOCK_KIND_INTERACTIVE,
        slack_blocks.BLOCK_KIND_UNKNOWN,
    }
)
# What makes a payload risky whatever built it. Both numbers are well under
# Slack's own ceilings and are not limits: they are the point past which a local
# check missing something is plausible, and past which a refusal costs the
# reader enough to be worth a call.
_RISKY_BLOCK_COUNT = 12
_RISKY_BLOCK_CHARACTERS = 8000

# How much of a refused payload is quoted back at its author, and how many of
# Slack's complaints are listed. A validator can report one error per field and
# a wall of them is not more legible than the first few; the log line keeps the
# full count either way.
_MAX_REFUSED_PAYLOAD_LENGTH = 2500
_MAX_VALIDATION_ERRORS_SHOWN = 5

# How much of one validator complaint reaches the log, and how many refused
# blocks are identified there.
#
# The length matters for a reason beyond legibility. These logs also record
# message content, and a validator's prose is written about the payload rather
# than about the reader -- but it is Slack's prose, not this connector's, and a
# complaint that quotes an offending value back is a complaint that carries a
# fragment of the message into a line nothing else would have put it in. The
# clamp bounds how much of anything can travel that way; what is deliberately
# *not* logged bounds the rest, which is why the block summary below is built
# from structure alone and never from a label, a placeholder, a hint, an option
# or a button's value.
_MAX_LOGGED_VALIDATION_MESSAGE = 200
_MAX_LOGGED_REFUSED_BLOCKS = 5

# Where Slack puts the same complaints when the refusal comes back from
# ``chat.postMessage`` rather than from ``blocks.validate``: prose lines under
# ``response_metadata.messages``, each tagging the element it is about in the
# body of the sentence rather than in a field of its own.
_SEND_REFUSAL_POINTER_RE = re.compile(r"json-pointer:\s*([^\]\s]+)")

# What the reader is told, per kind. Three notices rather than one because the
# three failures are not the same event.
#
# A refused table cost them formatting and nothing else -- the rows follow -- so
# the line is an aside. A refused fence cost the author a rendering they asked
# for and can fix, so the payload comes back with the validator's own words. A
# refused control cost a function: there is nothing to click, and the notice
# says only that. The payload is this connector's internals rather than
# anything the reader wrote, so none of it is quoted back.
_BLOCK_FAILURE_DATA_NOTICE = (
    "_Slack refused the rich rendering of this message, so the data above is"
    " shown as text._"
)
_BLOCK_FAILURE_FENCE_NOTICE = (
    "_Slack refused this Block Kit payload, so it is left here as source."
    " Slack's validator reported:_"
)
_BLOCK_FAILURE_INTERACTIVE_NOTICE = (
    "_Slack refused this message's controls, so there is nothing here to click._"
)

# What ``text`` becomes when a reply is nothing but a fence and
# ``slack_blocks.blockkit_fallback_summary`` still found nothing to lift out
# of the rendered blocks -- an interactive-only payload, or one built from
# types the summary does not read. Short and honest rather than an empty
# string, which chat.postMessage refuses outright, and rather than the fence
# source, which is the defect this stands in for.
_BLOCKKIT_FALLBACK_GENERIC_TEXT = "Sent a Block Kit message."

# How long a derived fallback may run. ``text`` here is read for the desktop
# and mobile notification and by a screen reader, all three wanting a line
# rather than a paragraph, and a ``section`` block's own text has no such
# ceiling -- it can be the longest thing in the reply.
_BLOCKKIT_FALLBACK_TEXT_LIMIT = 150


def _question_text_route(source: str) -> str:
    """How the same answer can be given without the buttons, if it can be.

    Only half the questions this connector posts have an answer. A question from
    an interrupt source has paused a live turn, and an ordinary message in that
    session is already routed into the turn -- see ``_withdraw_session_questions``,
    which retires the pending entry precisely because upstream is about to feed
    the message to the waiting task. So "type it instead" is the mechanism, and
    it works whether or not any buttons ever drew.

    Every other question is a standalone approval answered against a turn that
    has already finished, and it is answered through ``chat.user_answer`` and
    nowhere else. There is no text route, so none is named: telling a reader to
    reply to a message nothing is reading is worse than telling them nothing.
    """
    if source in _INTERRUPT_RESUME_SOURCES:
        return "Reply in this thread and the waiting task will read your answer."
    return ""


def validation_errors(response: Any) -> list[Mapping[str, Any]]:
    """The complaints in a ``blocks.validate`` reply, or ``[]`` if it approved.

    ``{"ok": true}`` is the whole of a success. A failure holds ``error`` --
    ``invalid_blocks``, the same opaque code a refused send reports -- and an
    ``errors`` array where the value is: each entry names a ``pointer`` into the
    payload alongside a ``code``, a ``message`` and a ``constraint``.

    A reply that says it failed but lists nothing still returns a list with one
    entry in it, synthesised from ``error``, because "Slack refused this" is
    true and actionable even when the detail is missing. Returning ``[]`` there
    would read as approval.

    Narrow in exactly the way ``rejected_blocks_error`` is narrow, and for the
    same reason: only a failure that is *about the blocks* may cost a message
    its rendering. ``ratelimited``, ``invalid_auth`` and a workspace where the
    method is not enabled all arrive here in the same shape and say nothing
    about the payload, so they read as "not validated" -- which sends the
    message exactly as it would have been sent without this call. A pointered
    ``errors`` array is admitted alongside the known codes because that shape
    belongs to schema validation and to nothing else, so a code this file has
    not heard of still degrades usefully rather than silently.
    """
    data = response if isinstance(response, Mapping) else getattr(response, "data", None)
    if not isinstance(data, Mapping):
        return []
    if data.get("ok"):
        return []
    entries = [
        entry
        for entry in (data.get("errors") or [])
        if isinstance(entry, Mapping)
    ]
    code = str(data.get("error") or "").strip()
    pointered = any(entry.get("pointer") for entry in entries)
    if code not in _BLOCK_REJECTION_ERRORS and not pointered:
        return []
    if entries:
        return entries
    return [{"code": code}] if code else []


def validation_refusal_code(response: Any) -> str:
    """The top-level error code on a refusal, or ``""``.

    ``validation_errors`` deliberately answers with the ``errors`` array alone,
    because that array is what the reader and the pointer logic are built on.
    The code beside it -- ``invalid_blocks`` -- is the part an operator greps
    for, and it is lost the moment the array is non-empty. This reads it back
    off the same reply so the log line can carry both.
    """
    data = response if isinstance(response, Mapping) else getattr(response, "data", None)
    if not isinstance(data, Mapping) or data.get("ok"):
        return ""
    return str(data.get("error") or "").strip()


def send_refusal_errors(exc: Exception) -> list[Mapping[str, Any]]:
    """Slack's validator lines from a *refused send*, in the shape of errors.

    The post-hoc path has no ``errors`` array. ``chat.postMessage`` answers
    ``invalid_blocks`` and puts its reasoning in ``response_metadata.messages``
    as prose -- "invalid_blocks [json-pointer:/blocks/4/element] ..." -- which
    is the same information wearing a different shape. Lifting the pointer back
    out of the sentence is what lets one description serve both paths.

    A line with no recognisable pointer is kept without one: the sentence is
    still the only thing Slack said about the payload.
    """
    response = getattr(exc, "response", None)
    data = response if isinstance(response, Mapping) else getattr(response, "data", None)
    if not isinstance(data, Mapping):
        return []
    metadata = data.get("response_metadata")
    raw = metadata.get("messages") if isinstance(metadata, Mapping) else None
    if not isinstance(raw, (list, tuple)):
        return []
    errors: list[Mapping[str, Any]] = []
    for line in raw:
        message = str(line or "").strip()
        if not message:
            continue
        found = _SEND_REFUSAL_POINTER_RE.search(message)
        errors.append(
            {"pointer": found.group(1) if found else "", "message": message}
        )
    return errors


def _block_identity(block: Any) -> str:
    """One block described by its structure and by nothing it displays.

    Type, element type, and the ids this connector chose. Every field that
    could hold a word of the message -- a label, a hint, a placeholder, an
    option, a button's encoded value -- is left out by construction: this
    string goes into a log, and what identifies the offending element is its
    type, not its text.
    """
    if not isinstance(block, Mapping):
        return "?"
    parts = [str(block.get("type") or "?")]
    block_id = str(block.get("block_id") or "").strip()
    if block_id:
        parts.append(f"block_id={block_id}")
    element = block.get("element")
    if isinstance(element, Mapping):
        parts.append(f"element={element.get('type') or '?'}")
        action_id = str(element.get("action_id") or "").strip()
        if action_id:
            parts.append(f"action_id={action_id}")
    elements = block.get("elements")
    if isinstance(elements, list):
        types = [
            str(item.get("type") or "?")
            for item in elements
            if isinstance(item, Mapping)
        ]
        if types:
            parts.append("elements=" + ",".join(types))
    return " ".join(parts)


def describe_refused_blocks(
    blocks: list[dict[str, Any]], errors: list[Mapping[str, Any]]
) -> str:
    """Which blocks Slack's pointers name, for the operator's log.

    The pointer alone says ``/4``, and nobody reading a log at two in the
    morning knows what the fifth block of a question was. Naming the element
    type beside the index is the difference between "something in that payload"
    and "the number field".

    Falls back to the whole payload's block types when no pointer parses, which
    is what a bare ``invalid_blocks`` leaves to work with. Less precise, and
    still the one thing that narrows the search.
    """
    indices = _pointed_indices(blocks, errors)
    if indices:
        listed = indices[:_MAX_LOGGED_REFUSED_BLOCKS]
        described = "; ".join(f"/{index} {_block_identity(blocks[index])}" for index in listed)
        if len(indices) > len(listed):
            described += f"; (+{len(indices) - len(listed)} more)"
        return described
    types = [
        str(block.get("type") or "?")
        for block in blocks
        if isinstance(block, Mapping)
    ]
    return f"no pointer; {len(blocks)} blocks: " + ",".join(types) if types else "no payload"


def validation_error_lines(errors: list[Mapping[str, Any]]) -> list[str]:
    """Slack's complaints as bullet lines, pointer first.

    The pointer leads because it is the only part that is not guessable. A model
    reading back its own fence can find ``/blocks/2/elements/0`` in it; it cannot
    find "must be one of the allowed values" without being told where.
    """
    lines: list[str] = []
    for entry in errors[:_MAX_VALIDATION_ERRORS_SHOWN]:
        pointer = str(entry.get("pointer") or "").strip()
        message = str(entry.get("message") or "").strip()
        code = str(entry.get("code") or "").strip()
        constraint = str(entry.get("constraint") or "").strip()
        detail = message or constraint or code or "refused"
        if code and detail != code:
            detail = f"{detail} ({code})"
        lines.append(f"• `{pointer}` — {detail}" if pointer else f"• {detail}")
    remaining = len(errors) - len(lines)
    if remaining > 0:
        lines.append(f"• …and {remaining} more")
    return lines


def describe_validation_errors(errors: list[Mapping[str, Any]]) -> str:
    """The same complaints on one line, for the operator's log.

    Pointer, code **and** Slack's own message. The message is prose written for
    whoever wrote the payload -- which is why the reader is never shown it on
    the interactive path, where the payload is this connector's internals and
    not theirs -- but it is the only part that says *what was wrong*, and that
    is precisely the operator's question. A pointer alone turns a five-minute
    diagnosis into an hour: it says which element and never which rule.

    Clamped per complaint, because Slack writes it and this line lands in a log
    that also records message content.
    """
    parts: list[str] = []
    for entry in errors[:_MAX_VALIDATION_ERRORS_SHOWN]:
        pointer = str(entry.get("pointer") or "").strip() or "?"
        code = str(entry.get("code") or "").strip()
        message = str(entry.get("message") or "").strip()
        constraint = str(entry.get("constraint") or "").strip()
        detail = code or "refused"
        said = message or constraint
        if said:
            detail = f"{detail} ({_clamp(said, _MAX_LOGGED_VALIDATION_MESSAGE)})"
        parts.append(f"{pointer}={detail}")
    if len(errors) > len(parts):
        parts.append(f"(+{len(errors) - len(parts)} more)")
    return " ".join(parts)


def should_validate_blocks(
    blocks: list[dict[str, Any]] | None, kind: str, mode: str
) -> bool:
    """Whether this payload is worth a ``blocks.validate`` call.

    A cost question and never a correctness one: skipping validation leaves the
    payload exactly as it would have been sent without this feature, and the
    post-hoc fallback still catches a refusal.

    Empty is never validated. An empty list is not a rendering -- it is how
    chat.update takes a message's blocks away -- and there is nothing in it that
    could be wrong.
    """
    if not blocks or mode == BLOCKKIT_VALIDATE_OFF:
        return False
    if mode == BLOCKKIT_VALIDATE_ALL:
        return True
    if kind in _RISKY_BLOCK_KINDS:
        return True
    if len(blocks) >= _RISKY_BLOCK_COUNT:
        return True
    return len(json.dumps(blocks, ensure_ascii=False)) >= _RISKY_BLOCK_CHARACTERS


def pointed_blocks(
    blocks: list[dict[str, Any]], errors: list[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    """The blocks Slack's pointers actually name, or all of them if none do.

    Quoting the whole payload back at an author buries the one element that was
    wrong among the paragraphs that were not. A pointer is
    ``/blocks/2/elements/0``, so the leading index is the block, and showing that
    block alone is the difference between "your message was refused" and "this is
    the part to fix".

    Everything is shown when no pointer parses -- a refusal reported as a bare
    ``invalid_blocks`` with no detail, which is what the post-hoc fallback has
    to work with. Less precise, and still the payload the author asked about.
    """
    wanted = _pointed_indices(blocks, errors)
    return [blocks[index] for index in wanted] if wanted else blocks


def _pointed_indices(
    blocks: list[dict[str, Any]], errors: list[Mapping[str, Any]]
) -> list[int]:
    """The block indices Slack's pointers resolve to, in the order given.

    Both pointer dialects are read. ``blocks.validate`` answers ``/4/element``
    because it was handed the array; a refused send says
    ``/blocks/4/element`` because it was handed the message. The leading
    ``blocks`` segment is the only difference between them.
    """
    wanted: list[int] = []
    for entry in errors:
        parts = str(entry.get("pointer") or "").strip("/").split("/")
        if parts and parts[0] == "blocks":
            parts = parts[1:]
        if parts and parts[0].isdigit():
            index = int(parts[0])
            if 0 <= index < len(blocks) and index not in wanted:
                wanted.append(index)
    return wanted


def block_failure_text(
    *,
    text: str,
    blocks: list[dict[str, Any]],
    kind: str,
    errors: list[Mapping[str, Any]],
    text_route: str = "",
) -> str:
    """What the reader is shown instead of a rendering Slack will not accept.

    Kind-aware, and the kind is the argument rather than something inferred from
    *blocks*, because the payload cannot tell the three apart: a ``section``
    holding an approval prompt and a ``section`` holding a paragraph of an answer
    are the same object.

    The message's own ``text`` always leads. It is a complete rendering of the
    same content on every path that has one, so the notice is an addition to a
    message that still says what it said, never a replacement for it.
    """
    parts = [text.strip()] if text.strip() else []
    if kind == slack_blocks.BLOCK_KIND_INTERACTIVE:
        # No payload, and no pointer either. Both are this connector's
        # internals -- a button's value encodes a request id -- and neither
        # helps a reader whose problem is that the thing they were asked to
        # click is not there. The pointer is in the log, where it is the
        # operator's to act on.
        notice = _BLOCK_FAILURE_INTERACTIVE_NOTICE
        if text_route.strip():
            notice = f"{notice.rstrip('_')} {text_route.strip()}_"
        parts.append(notice)
        return "\n\n".join(parts)

    if kind == slack_blocks.BLOCK_KIND_FENCE:
        parts.append(_BLOCK_FAILURE_FENCE_NOTICE)
        parts.extend(validation_error_lines(errors))
        payload = json.dumps(
            pointed_blocks(blocks, errors), ensure_ascii=False, indent=2
        )
        parts.append(f"```\n{_clamp(payload, _MAX_REFUSED_PAYLOAD_LENGTH)}\n```")
        return "\n\n".join(parts)

    # Data, chrome, and anything unclassified. The reader asked for numbers, so
    # the numbers are rebuilt from the blocks themselves and the notice comes
    # last, under them: it explains what they are looking at rather than
    # standing between them and it.
    rebuilt = slack_blocks.data_fallback_text(blocks, already_in=text)
    if rebuilt:
        parts.append(rebuilt)
    parts.append(_BLOCK_FAILURE_DATA_NOTICE)
    return "\n\n".join(parts)

# Block Kit's own limits, which are much tighter than the message text limit
# above and are enforced by Slack rather than negotiated: a message holding one
# oversized field is rejected whole, so every field built from agent-supplied
# content is clamped before it is sent rather than after it is refused.
#
# A button's value is a string here, unlike Feishu's card actions, which take an
# arbitrary object. Whatever the click needs to hold has to be encoded into it
# and has to fit.
_MAX_BUTTON_VALUE_LENGTH = 2000
_MAX_BUTTON_TEXT_LENGTH = 75
_MAX_SECTION_TEXT_LENGTH = 3000
_MAX_ACTIONS_ELEMENTS = 25

# An option's description is meant to be the one line that says what choosing it
# does, so it is clamped well below the section limit it shares with the other
# options' descriptions. Without a per-description bound, one long description
# would push the rest of them out of the block.
_MAX_OPTION_DESCRIPTION_LENGTH = 200

# Identifies this connector's answer buttons. Bolt matches an action listener on
# action_id, which must also be unique within a message, so the option's
# position is appended and the listener is registered with the prefix pattern.
_QUESTION_ACTION_ID_PREFIX = "jiuwenswarm_answer:"

# A question may instead be answered with values a person entered rather than an
# option they pressed (see slack_inputs). Those elements share the prefix, and
# therefore the one listener: Slack shows the person an error when an
# interaction is not acknowledged within three seconds, and a picker fires an
# interaction on every change.
#
# ``submit`` is the commit point -- a picker on its own has none -- and is the
# only one of these that answers anything. ``input:<n>`` and ``input:<n>.<part>``
# are the elements themselves, acknowledged and otherwise ignored; the values
# they hold are read from ``state.values`` when submit arrives, never
# accumulated from the events.
_QUESTION_SUBMIT_ACTION_ID = f"{_QUESTION_ACTION_ID_PREFIX}submit"
_QUESTION_INPUT_ACTION_ID_PREFIX = f"{_QUESTION_ACTION_ID_PREFIX}input:"
_QUESTION_ACTION_ID_RE = re.compile(
    rf"^{re.escape(_QUESTION_ACTION_ID_PREFIX)}"
    rf"(?:\d+|submit|input:\d+(?:\.[a-z_]+)?)$"
)

# Matches every Events API envelope, and exists so that no subscribed event can
# reach Slack unacknowledged.
#
# Bolt leaves an unmatched request on the 404 its dispatcher builds when no
# listener claimed it, and the Socket Mode adapter acknowledges the envelope
# only on a 200 (``slack_bolt/adapter/socket_mode/async_internals.py``:
# ``if bolt_resp.status == 200``). Slack counts an unacknowledged envelope as a
# failure and, past a threshold, disables Event Subscriptions for the whole app
# -- which takes message delivery down with it, not merely the event nobody was
# reading.
#
# Today that never happens, but only by accident: ``async_dispatch`` seeds its
# response with ``BoltResponse(status=200)`` before the middleware chain runs,
# and ``_handle_unmatched_requests`` hands that same 200 back rather than the
# 404 it built. That is an implementation detail of a library this file pins
# nothing about. A listener that matches everything makes the acknowledgement
# something this connector does rather than something it is currently given.
#
# A ``Pattern`` rather than the string ``"*"``: bolt's event matcher takes a
# ``str`` as an exact type name and only a compiled pattern as a pattern.
_ANY_EVENT_TYPE = re.compile(r".*")

# Identifies the stop button, and deliberately not under the answer prefix. A
# stop is not an answer to anything: it arrives against a turn that is still
# running rather than one paused on a question, it resolves to a cancel rather
# than to an option, and the two listeners refuse different things. Sharing the
# prefix would put both through ``_handle_question_action``, which reads every
# payload it is given as an answer to a pending question.
#
# One id and no index, because a card holds at most one of these: the button
# stands for the turn, not for a choice within it.
_STOP_ACTION_ID = "jiuwenswarm_stop"

# What the stop button says. English, like every other fixed string this
# connector posts.
_STOP_BUTTON_LABEL = "Stop"

# Shown to the person who pressed stop and to nobody else. Three outcomes, and
# they are kept apart because they are three different things to do next: the
# work is being stopped, the turn had already ended so there is nothing to stop,
# or this person may not stop it.
_STOP_ACCEPTED_NOTICE = "Stopping this turn."
_STOP_STALE_NOTICE = (
    "That turn has already finished, so there is nothing left to stop."
)
_STOP_REFUSED_NOTICE = "You are not permitted to stop this turn."

# The same courtesy on the answer path, where a clicks rule is the only thing
# that can produce it. The allow-list refusal beside it stays silent, as it
# always has; this notice belongs to the new gate and does not reach back to
# change the old one.
_ANSWER_REFUSED_NOTICE = "You are not permitted to answer this request."

# The blocks a question's answering machinery lives in, and therefore the ones a
# rewrite takes away when the question stops being answerable. Option buttons and
# the submit button are ``actions``; every entered value is an ``input`` block,
# which is the only container Slack gives a label to. Both are listed here so
# that withdrawal stays what it already was -- a chat.update that rewrites the
# blocks -- rather than growing a per-element-type branch.
_QUESTION_INTERACTIVE_BLOCK_TYPES = frozenset({"actions", "input"})

# Slack's ceiling on how many blocks one message may hold.
_MAX_QUESTION_BLOCKS = 50

# What the submit button says when the question does not name it. English, like
# the other fixed strings this connector posts; a question that wants its own
# word supplies ``submit_label``.
_DEFAULT_SUBMIT_LABEL = "Submit"

# Shown, to the person who pressed submit and to nobody else, when what they
# submitted cannot be answered with. The question stays posted and its elements
# stay on screen, so the fix is to pick and press again.
_INCOMPLETE_SUBMIT_NOTICE = "This cannot be submitted yet —"

# The label the question builder appends to every option list to offer a typed
# answer instead of a choice (see _build_multi_questions). Slack has no way to
# collect one from a button, and answering with the bare label resolves to an
# empty answer, so the button is not rendered at all.
_FREE_FORM_OPTION_LABEL = "Other"

# The two values Slack accepts in a button's ``style``. Every other word --
# "warning", "secondary", "success" -- is refused with ``must be a valid enum
# value``, and omitting the field gives the neutral default. Slack's own
# guidance is that "primary" marks at most one button in a set and that "danger"
# is for destructive actions and should be used more sparingly still, which is
# what the resolver below spends them on.
_BUTTON_STYLE_PRIMARY = "primary"
_BUTTON_STYLE_DANGER = "danger"

# Sources whose answer resumes a paused turn rather than replying to a finished
# one. Named to match the gateway's own set, which decides whether an inbound
# chat.send is a resume and therefore must not cancel the stream it is resuming.
_INTERRUPT_RESUME_SOURCES = frozenset(
    {
        "ask_user_interrupt",
        "confirm_interrupt",
        "permission_interrupt",
        "evolution_interrupt",
    }
)

# Questions posted and still waiting for a click. Bounded and aged for the same
# reason the stream map is: a turn that is cancelled, or answered from another
# client, never comes back to clear its entry.
_MAX_PENDING_QUESTIONS = 32
_PENDING_QUESTION_TIMEOUT_SECONDS = 3600.0

# Cron runs whose placeholder is on screen and may still be superseded by a
# result. Bounded and aged like the questions above: a run whose terminal push
# never arrives -- the gateway restarted between the placeholder and the result
# -- would otherwise leave an entry behind for every run, forever. An hour
# rather than the streaming path's fifteen minutes, because a cron run
# legitimately outlives a stream. Losing an entry costs the edit and not the
# delivery: an unknown run posts a new message.
_MAX_CRON_RECORDS = 32
_CRON_RECORD_TIMEOUT_SECONDS = 3600.0

# Slack accepts exactly three statuses on a task_card. Established by posting to
# a real workspace, not from the SDK, which builds and validates blocks Slack
# then refuses: "pending", "queued", "not_started", "todo", "cancelled",
# "failed", "running", "success" and "waiting" were each rejected. There is no
# "not yet started" value, which is unproblematic here because the gateway never
# posts before a run has started -- the push fires after the wake, always.
_CARD_STATUS_IN_PROGRESS = "in_progress"
_CARD_STATUS_COMPLETE = "complete"
_CARD_STATUS_ERROR = "error"
# The scheduler's own vocabulary is pending|running|succeeded|failed
# (CronRunState.status). Only the two terminal values map; everything else --
# including a status this connector has never heard of -- is treated as
# non-terminal, which is what the supersession contract requires.
_CRON_TERMINAL_CARD_STATUS = {
    "succeeded": _CARD_STATUS_COMPLETE,
    "failed": _CARD_STATUS_ERROR,
}
# The card's own fields. A title is a job name and a detail is a status line;
# neither is a place to put a report, and an oversized field is rejected with
# the whole message rather than trimmed.
_MAX_CARD_TITLE_LENGTH = 150
_MAX_CARD_DETAILS_LENGTH = 2000

# Slack accepts a fourth status on a task_card nested inside a plan: "pending",
# which a standalone card refuses outright ("must be a valid enum value").
# Verified against a live workspace, not from the SDK. It is the only value that
# says "written down, not started", which is exactly what an untouched todo is,
# and it is why the card is always wrapped in a plan even when only one section
# has anything in it.
_CARD_STATUS_PENDING = "pending"

# TaskTool is synchronous and its call/result pair describes one delegation.
# Persistent spawn/wait tools only coordinate a separate subagent lifecycle;
# their results do not say whether the subagent finished.
_SUBAGENT_TOOL_NAME = "task_tool"
_PERSISTENT_SUBAGENT_TOOL_NAMES = frozenset({"subagent_spawn", "subagent_wait"})
# One tracked run's state -- a dispatched subagent, or a tool the parent called.
# "unreported" is not a fourth outcome the runtime can report: it is what a run
# is called when the turn ended without its result arriving, which a cancelled
# turn does.
#
# These four say what the harness observed and nothing else. "done" means the
# runtime got a result back, not that the work in it was any good; "failed"
# means the runtime classified the result as an error, which for a subagent it
# never does -- a subagent's own tool results stay inside the subagent, and its
# exit codes reach this connector only as prose in a result nobody parses here.
_RUN_RUNNING = "running"
_RUN_DONE = "done"
_RUN_FAILED = "failed"
_RUN_UNREPORTED = "unreported"
# The Block Kit emoji standing for each state, in the order one line lists
# them. The element form -- ``{"type": "emoji", "name": ...}`` -- is the only
# one that works here: ``rich_text`` does not expand a ``:shortcode:`` inside a
# ``text`` element the way ``mrkdwn`` does, so a shortcode arrives as literal
# text. A literal unicode glyph does render, but it would tie the card's
# meaning to this file's encoding rather than naming the emoji, so it is not
# used and a test asserts none appears.
_RUN_STATE_EMOJI: tuple[tuple[str, str], ...] = (
    (_RUN_DONE, "white_check_mark"),
    (_RUN_RUNNING, "zap"),
    (_RUN_FAILED, "x"),
    (_RUN_UNREPORTED, "grey_question"),
)
_RUN_STATE_EMOJI_BY_STATE = dict(_RUN_STATE_EMOJI)
# The todo list's own vocabulary, as JiuSwarmStreamEventRail formats it for the
# frontend; a cancelled item is dropped before it ever reaches the wire. Its
# "not started" state has no counterpart among the run states -- a todo nobody
# has begun is neither a failure nor a result that went missing -- so it is
# named with an emoji of its own.
_TODO_COMPLETED = "completed"
_TODO_IN_PROGRESS = "in_progress"
_TODO_PENDING = "pending"
_TODO_STATE_EMOJI: tuple[tuple[str, str], ...] = (
    (_TODO_COMPLETED, "white_check_mark"),
    (_TODO_IN_PROGRESS, "zap"),
    (_TODO_PENDING, "hourglass_flowing_sand"),
)
_TODO_STATES = frozenset(state for state, _ in _TODO_STATE_EMOJI)
# The section labels. Titles rather than aggregates: a row inside a plan is a
# section of one card, and the counts go on the detail line beneath it.
_THINKING_CARD_TITLE = "Thinking"
# The thinking section's ``task_id`` namespace, named because one decision reads
# it: whether the card as it stands holds this section and no other. Every
# section namespaces its ids the same way -- the id space is shared with
# whatever else posts cards -- and this is the only prefix anything matches on.
_THINKING_TASK_ID_PREFIX = "thinking-"
_TODO_CARD_TITLE = "Todos"
_SUBAGENT_CARD_TITLE = "Subagents"
_TOOL_CARD_TITLE = "Tools"
# The runtime compacting its own context, which is work no other section on this
# card can see. A turn whose window is over threshold spends the whole of it
# inside ``get_context_window`` before the model is called even once: no tool
# call, no todo, no dispatch and not one chunk of reasoning, so every other
# section is empty and the card has nothing to appear for. The case this was
# built from ran six minutes and nineteen seconds that way and then answered in
# twenty-one seconds, and the channel showed an empty thread for the whole of
# the six.
#
# Matched on the payload's own ``event_type`` rather than on ``msg.event_type``.
# ``EventType`` has no member for this event, so the gateway cannot parse the
# string, logs it at debug and leaves the field unset; the web channel matches
# the same string the same way.
_COMPRESSION_EVENT_TYPE = "context.compression_state"
_COMPRESSION_CARD_TITLE = "Context compression"
_COMPRESSION_TASK_ID_PREFIX = "compression-"
# The runtime's own vocabulary for how one processor's pass ended
# (``ContextCompressionState.status``), kept verbatim rather than mapped onto
# the run states beside it. Three of the five have no counterpart there: a pass
# that ran and saved nothing is not a failure, a pass that never ran is not the
# same as one that saved nothing, and neither is a result that went missing.
_COMPRESSION_STARTED = "started"
_COMPRESSION_COMPLETED = "completed"
_COMPRESSION_NOOP = "noop"
_COMPRESSION_SKIPPED = "skipped"
_COMPRESSION_FAILED = "failed"
_COMPRESSION_STATES = frozenset(
    {
        _COMPRESSION_STARTED,
        _COMPRESSION_COMPLETED,
        _COMPRESSION_NOOP,
        _COMPRESSION_SKIPPED,
        _COMPRESSION_FAILED,
    }
)
# The glyph per state. "zap" for a pass in flight and a tick for one that saved
# something, both borrowed from the run states so a reader learns them once.
# "heavy_minus_sign" is the one this card had no word for: a pass that ran to
# the end and had nothing to save is neither a success worth a tick nor a
# failure worth a cross, and the wording beside it says which nothing it was.
_COMPRESSION_STATE_EMOJI = {
    _COMPRESSION_STARTED: "zap",
    _COMPRESSION_COMPLETED: "white_check_mark",
    _COMPRESSION_NOOP: "heavy_minus_sign",
    _COMPRESSION_SKIPPED: "heavy_minus_sign",
    _COMPRESSION_FAILED: "x",
    _RUN_UNREPORTED: _RUN_STATE_EMOJI_BY_STATE[_RUN_UNREPORTED],
}
# What each terminal state says about itself when it has no figure to print. A
# pass that saved nothing is the one an operator actually wants to read: the
# turn this section was built from ran two processors and only the second did
# any work, which is a fact about how that deployment is configured and is
# invisible on every other surface Slack has.
_COMPRESSION_STATE_WORDING = {
    _COMPRESSION_NOOP: "saved nothing",
    _COMPRESSION_SKIPPED: "did not run",
    _COMPRESSION_FAILED: "failed",
    _RUN_UNREPORTED: "reported no result",
}
# The one failure this card names rather than lumping in with the rest. A
# compaction the runtime abandoned because somebody pressed stop is reported as
# ``failed`` with this exact string, and it does not kill the turn -- the
# runtime logs a warning and carries on -- so without the distinction a card
# would say "failed" about a turn that went on to answer perfectly well, having
# done exactly what it was told. The sentinel is matched rather than the error
# text being printed: the card carries no runtime error string of its own, and
# the one already on it has a preformatted block and a section of its own.
_COMPRESSION_CANCELLED_ERROR = "cancelled"
# Below this, a terminal pass prints no duration at all. A processor that found
# nothing to do returns in microseconds, and "saved nothing  0s" reads as a
# rendering fault rather than as a measurement. The figure is on the line to
# explain a wait, so a line with no wait to explain does without it.
_MIN_COMPRESSION_ELAPSED_SECONDS = 1.0
# How many passes one record holds. A processor is triggered once per phase it
# finds the window over threshold in, and two processors are the whole of an
# ordinary deployment, so this is generous by an order of magnitude and exists
# only so a runtime emitting in a loop cannot grow the record without bound.
_MAX_COMPRESSION_RUNS = 16
# The thinking section's two glyphs. "zap" is what every running thing on this
# card already holds, so a reader learns it once. The terminal one is an
# hourglass that has run out rather than the tick the run states use: a stretch
# of reasoning ending says the model stopped thinking, and nothing whatever
# about whether the turn it belonged to went well. Slack's task_card vocabulary
# has no neutral terminal status to say that with, so the glyph and the wording
# say it instead.
_THINKING_RUNNING_EMOJI = "zap"
_THINKING_ENDED_EMOJI = "hourglass"
# The plan's own line. ASCII, like every other string in these blocks: a single
# non-ASCII character in a text element has been seen to take the whole message
# down with invalid_blocks, and a title is not worth the risk of finding out
# which fields share that rule.
_ACTIVITY_TITLE_WORKING = "Working..."
_ACTIVITY_TITLE_DONE = "Turn finished"
# The turn itself died: a stream that stopped arriving, a tool loop the runtime
# gave up on, a run cancelled underneath it. Deliberately not folded into the
# "n failed" the title already counts, which means something narrower -- a tool
# told the runtime it had failed -- and which a turn that never got that far
# cannot produce. Both are harness-side statements and neither says anything
# about whether the work was any good.
_ACTIVITY_TITLE_FAILED = "Turn failed"
# Somebody pressed the button. Distinct from both of its neighbours: "Turn
# finished" claims the turn ran to its answer, which is the one thing a stop
# guarantees it did not, and "Turn failed" is reserved above for the turn dying
# underneath itself.
#
# It names no one. Every other string here is a harness-side statement, and a
# name in the title would be the first thing on this card that is a statement
# about a person, addressed to everyone who can read the channel. The person who
# pressed it is told by the ephemeral, which reaches exactly them; a
# ``clicks.stop`` rule can also let somebody other than the starter stop a turn,
# and publishing who exercised that turns a permitted gesture into an
# attribution. ASCII like the rest.
_ACTIVITY_TITLE_STOPPED = "Turn stopped"
# And the same words on the reply itself. A streamed message holds whatever the
# turn had written by the moment the cancel landed -- usually a sentence that
# stops mid-clause -- and nothing else marks it, so read on its own it is a
# complete answer that happens to end badly. The tail is appended by the close
# rather than replacing anything: an append cannot take back what the reader
# already watched arrive.
#
# Derived from the title rather than spelled again, so the two cannot drift into
# saying different things about one ending. Markdown, not mrkdwn -- ``_x_`` is
# emphasis in what ``markdown_text`` reads -- and the blank line ahead of it
# puts it in a paragraph of its own however the streamed prefix ended.
_STREAM_STOPPED_TAIL = f"\n\n_{_ACTIVITY_TITLE_STOPPED}_"
# The section that shows such a failure. It is appended to the plan rather
# than chipped onto a section that is already there, because a harness error is
# usually attributable to nothing on the card: a model call that timed out
# belongs to the turn, not to any tool or subagent, and putting an error chip on
# a tool that came back fine is a lie someone will act on.
_ERROR_CARD_TITLE = "Harness error"
_HARNESS_ERROR_LEAD = "The turn ended before it answered."
# What a trimmed error says about itself. The tail goes rather than the head:
# the first line of these is the one that names the failure, and a message that
# was cut must say so rather than end mid-sentence and read like the whole of
# it.
_HARNESS_ERROR_TRIM_MARKER = "\n[trimmed]"
# How much of the error reaches the notification string. That line is the
# fallback Slack shows when it refuses the blocks, so it says enough of the
# failure to be worth reading on its own -- but it is a notification, not a
# report, and the whole error is in the card above it.
_MAX_SUMMARY_ERROR_LENGTH = 200
# A subagent type and a tool name are both model-supplied, so both are reduced
# to an identifier before they are shown: a card is not a place to put text
# nobody validated.
_RUN_LABEL_ALLOWED = re.compile(r"[^0-9A-Za-z_.\- ]+")
_MAX_RUN_LABEL_LENGTH = 48
_UNKNOWN_SUBAGENT_LABEL = "subagent"
_UNKNOWN_TOOL_LABEL = "tool"
# A processor's name is the runtime's rather than the model's, but it is reduced
# on the same rule and falls back the same way: one kind of text on this card
# must not become two because of where it came from.
_UNKNOWN_COMPRESSION_LABEL = "processor"
# One line per distinct label, so a detail block is bounded by how many kinds of
# thing ran rather than by how many times they ran. Eight lines of a clamped
# name and four counts stays an order of magnitude under
# _MAX_CARD_DETAILS_LENGTH, which is why a detail block is built as elements and
# never has to be trimmed as text.
_MAX_CARD_DETAIL_LINES = 8
# How many in-flight tools are named individually before the rest are summed.
# The parent runs its tools mostly one at a time, so this is generous already.
_MAX_RUNNING_TOOL_LINES = 4
# How many individual tool runs one record holds. A long turn calls hundreds,
# and everything older than this survives as counts: the run objects exist to
# pair a result with its call and to say how long a call has been out, and
# neither is needed once it has returned.
_MAX_TOOL_RUNS = 200
# A record is one turn's activity. Bounded and aged the same way the cron
# records are, for the same reason: a turn that never reaches a terminal event
# leaves one behind.
_MAX_ACTIVITY_RECORDS = 32
_ACTIVITY_RECORD_TIMEOUT_SECONDS = 3600.0
# An interrupt answered from Slack resumes the paused turn as a new request with
# an id of its own, so the turn that carries on doing the work the reader asked
# for is, on the wire, a different request from the one they asked. These map
# the resumed id back to the id the request started under, which is what keeps
# one question-and-answer round from splitting the card in two. Bounded for the
# same reason the records are: an entry is only ever waited on by the turn that
# immediately follows it.
_MAX_ACTIVITY_REQUEST_ALIASES = 64

# Replaces the question's text once the turn it was asked for has moved on. The
# buttons go with it, so this is what the message says from then on.
_WITHDRAWN_QUESTION_NOTICE = (
    "_This request is no longer waiting for an answer: an ordinary message sent "
    "while it was pending withdrew it, and the task carried on without it. Ask "
    "again if it still needs doing._"
)
# Shown on a message that was clicked anyway, in place of the answered note.
_WITHDRAWN_QUESTION_NOTE = "This request is no longer waiting for an answer."

# The Slack SDK installs AsyncConnectionErrorRetryHandler by default and nothing
# else, so a 429 arrives here as a raised error rather than a retried call
# (slack_sdk.http_retry.builtin_async_handlers.async_default_handlers). Retrying
# rate limits is therefore ours to do.
_MAX_RATE_LIMIT_RETRIES = 3
# Slack's Retry-After is in seconds and is normally single digits. Cap it so a
# hostile or malformed header cannot park the dispatch loop for an hour.
_MAX_RETRY_AFTER_SECONDS = 60.0
_DEFAULT_RETRY_AFTER_SECONDS = 1.0

# Slack meters message operations at roughly one per second per channel, and
# chat.update draws on the same budget as the posts that deliver the reply itself.
# Edits are therefore both debounced (how long a snapshot waits for more text)
# and spaced (how close two edits in one channel may be), the second being the
# one that matters when several conversations stream into the same channel.
_STREAM_DEBOUNCE_MS = 1000
_STREAM_MIN_UPDATE_INTERVAL_SECONDS = 1.0
# An append is metered more generously and costs less: chat.appendStream is
# Tier 4 ("100+ per minute") where chat.update is Tier 3 ("50+"), and none of
# the three streaming methods appears on the rate-limit page's special
# per-channel tier at all. One stream at 600 ms spends about 100 appends a
# minute, which is Tier 4's floor rather than its ceiling; the spacing is kept
# beside the debounce, and still keyed by channel, because the tier is per app
# per workspace and several concurrent streams share it.
_STREAM_APPEND_DEBOUNCE_MS = 600
_STREAM_APPEND_MIN_INTERVAL_SECONDS = 0.6
# What chat.appendStream and chat.stopStream answer when the message can take
# no more. Only the first is a reader's doing -- "The streaming message was
# stopped by the user and no further appends are accepted" -- and it has no
# analogue on the edit path, where a reader cannot refuse an edit. The rest
# mean the message is no longer this app's to write into. Slack documents no
# code for a stream that aged out server-side; if one exists it is not named
# here, and whichever of these it arrives as is handled the same way.
_STREAM_STOPPED_BY_USER = "stopped_by_user"
_STREAM_UNUSABLE_ERRORS = frozenset(
    {
        _STREAM_STOPPED_BY_USER,
        "message_not_in_streaming_state",
        "message_not_owned_by_app",
    }
)
# Refused for want of the recipient a channel stream requires. Never expected:
# both ids are read off the inbound event and remembered against the session
# before a reply can be streamed into it, so one of these means the plumbing
# broke rather than that the reader did anything.
_STREAM_RECIPIENT_ERRORS = frozenset(
    {"missing_recipient_user_id", "missing_recipient_team_id"}
)
# What chat.update answers for a message that is still streaming. Measured
# against the live API; it appears in neither the SDK nor any documentation
# page. It is the opposite of the codes above: they mean the message will take
# no more, this one means it is still the streaming API's and an append is what
# it wants. Editing a streamed message is refused cleanly -- an append made
# straight after a refused edit succeeds -- so learning this costs an error and
# nothing else.
_STREAM_STATE_CONFLICT = "streaming_state_conflict"
# What every chunk is answered with when the stream was opened in text mode.
# A stream is locked to the shape of its opening call for the whole of its life:
# opened with ``markdown_text`` it takes only ``markdown_text``, and opened with
# ``chunks`` it takes chunks of every kind, the markdown ones included. Measured;
# documented nowhere. This connector always opens with chunks, so a mismatch
# means the opening call has been changed back.
_STREAM_MODE_MISMATCH = "streaming_mode_mismatch"
# The failures that are about the payload rather than about the stream. Retrying
# one of these unchanged cannot succeed however healthy the stream turns out to
# be, which is the distinction ``resume`` is built on.
_STREAM_PAYLOAD_ERRORS = frozenset({_STREAM_MODE_MISMATCH})
# What a refused rendering looks like on a stream, which is the posting path's
# set plus one. A "file" block faults the streaming methods with ``internal_error``
# rather than being cleanly rejected -- measured, three times out of three, on
# both append and stop, while the same block posts fine through chat.postMessage.
# Unreachable today: nothing here builds a file block. It is admitted anyway
# because the two outcomes are "one message with the text and no rendering"
# against "two messages", and because a generic internal error that only ever
# arrives with blocks attached is worth one attempt without them. Kept apart from
# _BLOCK_REJECTION_ERRORS so the posting path does not start dropping renderings
# on every transient fault.
_STREAM_BLOCK_REJECTION_ERRORS = _BLOCK_REJECTION_ERRORS | {"internal_error"}
# How much of the finished answer has to be found at the end of what streamed
# before that point is taken for the seam between the two. Any seam that holds
# is textually safe -- what follows it is appended and the message reads as the
# whole answer either way -- so this is not a correctness floor. It is there so
# that a chance agreement of a character or two does not quietly turn a reply
# the runtime rewrote into an append, which is the one case the search must
# still refuse.
_STREAM_ANCHOR_MIN_CHARS = 8
# A stream is closed by the terminal event of its turn. An event holding no
# text never reaches that point, so the map is also bounded and aged.
_MAX_ACTIVE_STREAMS = 32
_STREAM_IDLE_TIMEOUT_SECONDS = 900.0
# One remembered recipient per session, and a session is cheaper than a stream:
# an entry is a pair of ids, it is written once per inbound message, and it has
# to outlive the whole turn rather than one reply.
_MAX_STREAM_RECIPIENTS = 256

# One remembered initiator per session, sized like the stream recipients above
# and for the same reason: an entry is opened once per dispatch, holds two ids,
# and lives for one turn.
#
# The timeout is the backstop rather than the mechanism. An entry is dropped by
# the terminal event of the turn it belongs to, and the age is what covers the
# turn whose terminal event never arrived at all -- the gateway restarted under
# it, the event was lost, the connector was reconfigured mid-turn. An hour is
# far longer than any turn and far shorter than the process, which is what
# separates "still running" from "leaked".
_MAX_TURN_INITIATORS = 256
_TURN_INITIATOR_TIMEOUT_SECONDS = 3600.0

# The hold under ``mid_turn: queue``, bounded on three axes because it can fill
# three ways.
#
# The per-session cap is the one an operator meets: a thread where the agent is
# working and four people keep typing. It is small: a queue is a promise to run
# each message *later*, and by the time a fortieth message ran the conversation
# it belonged to would be an hour gone. Overflow refuses the **newest** message
# and says so on it, rather than dropping the oldest, which already holds a
# queued reaction that told its sender it would run.
#
# It is spent faster under ``delivery.session: channel``, where one queue serves
# every thread in the room rather than one per thread. That is the cost of the
# setting rather than a reason to size this for it: a room busy enough to fill
# eight has more waiting than a queue can usefully hold, and raising the cap
# would only lengthen the wait before the same refusal.
#
# The session cap covers a busy workspace queueing in many conversations at
# once, and is spent on the session whose head is oldest -- the queue least
# likely to still be wanted.
#
# The age is the backstop, and it is the one that matters most here. A queue is
# drained by the terminal event of the turn it is waiting behind, so a turn
# whose terminal never arrives would otherwise strand its queue forever. It is
# the same hour ``_TURN_INITIATOR_TIMEOUT_SECONDS`` uses and for the same
# reason: far longer than a turn, far shorter than the process.
_MAX_QUEUED_SESSIONS = 64
_MAX_QUEUED_PER_SESSION = 8
_QUEUED_MESSAGE_TIMEOUT_SECONDS = 3600.0

# What the sender of a refused message is told. The reaction alone says "not
# accepted" and stops there, leaving the sender unable to learn what was wrong,
# whether the work was still running, or whether trying again would help. A
# refusal is the only outcome on this path that discards what somebody typed:
# dispatched and queued messages both end up in the session's history, and an
# ignored one is left untouched in Slack.
#
# The cap is named because it is what makes the state legible: this
# conversation is holding its maximum, which is a different thing from being
# refused entry to it.
_QUEUE_FULL_NOTICE = (
    f"This conversation is already holding {_MAX_QUEUED_PER_SESSION} messages"
    " waiting on the turn in progress, which is as many as it holds. Your"
    " message was not accepted and will not be answered. Send it again once"
    " the current work finishes."
)

# Requests whose "terminal" event says only that the runtime took the input, not
# that a turn ended. A steer is the case: the running turn holds the
# interaction's output lease, so ``attach_output`` hands the steer's own request
# nothing to read and the adapter answers it with ``runtime.accepted`` followed
# immediately by an end-of-stream. Read as an ordinary ending, that pair would
# close a live turn's initiator entry and drain a queue behind a turn that is
# still working.
#
# Bounded and aged like the rest. An entry is written when the acceptance
# arrives and read once, by the ending that follows it within milliseconds; ten
# minutes is generous for that gap and short enough that nothing accumulates.
_MAX_ACK_ONLY_REQUESTS = 256
_ACK_ONLY_REQUEST_TIMEOUT_SECONDS = 600.0

# A streamed turn reports its progress as well as its answer: the model thinking
# aloud, every tool it calls and what came back, and the periodic status, usage
# and todo events that a rich client renders as chrome around the reply. Slack
# has nowhere to put chrome -- ``send()`` renders whatever text an event holds
# as a message of its own -- so relaying these would bury the answer under a
# running commentary of its own construction.
#
# Only events that exist to describe progress are listed. chat.delta is folded
# into the streamed message, chat.final and chat.error are the reply and its
# failure, chat.file is an upload, and heartbeat.relay is explicitly rendered;
# all of them are deliveries and none of them are here. chat.ask_user_question
# is also absent: it asks the user something and is meant to reach them, which
# it does through _send_question rather than through the text path.
#
# Tool calls, results, subagent updates, todo snapshots and reasoning chunks
# are read before this gate to build the turn card. They stay listed so a
# disabled card still drops them rather than narrating them.
#
# chat.reasoning is the one whose payload is never opened. The card
# reads that it arrived and when, and nothing else: the content of a reasoning
# chunk is high-volume, unvetted model output.
#
# context.compression_state is another progress event and is deliberately absent
# rather than missing. ``EventType`` has no member for it, so a frozenset of
# enum members cannot hold it and this gate never saw it -- which is why it was
# the one progress event that reached the text path. It is recognised on its
# payload instead and consumed whether or not the card is on; see
# ``_track_compression_event``.
_INTERMEDIATE_STREAM_EVENTS = frozenset(
    {
        EventType.CHAT_REASONING,
        EventType.CHAT_TOOL_CALL,
        EventType.CHAT_TOOL_UPDATE,
        EventType.CHAT_TOOL_RESULT,
        EventType.CHAT_SUBTASK_UPDATE,
        EventType.CHAT_PROCESSING_STATUS,
        EventType.CHAT_USAGE_METADATA,
        EventType.CHAT_USAGE_SUMMARY,
        EventType.CHAT_SYMPHONY_STATUS,
        EventType.CHAT_EVOLUTION_STATUS,
        EventType.CHAT_RETRACT,
        EventType.CONTEXT_USAGE,
        EventType.TODO_UPDATED,
    }
)


def _prune_bounded_map(
    entries: dict[Any, Any],
    *,
    cap: int,
    timeout: float,
    timestamp: Callable[[Any], float],
    what: str,
) -> None:
    """Age out and then cap one of this connector's tracking maps.

    Each of these maps is opened by one event and cleared by a later one that a
    cancelled, superseded or never-answered turn does not send, so every one of
    them needs a bound or it grows for the life of the process. The two rules
    are the same everywhere: an entry older than ``timeout`` cannot still be
    wanted, and at ``cap`` the oldest gives way so the newest can be stored.

    ``timestamp`` reads the age off a value, because the maps hold different
    things -- dataclasses whose field is named for whatever last touched them,
    and one bare float.

    An eviction is always warned about. It means this connector has lost
    something a later event was going to look for, and the loss is silent on
    every surface except the log; ``what`` names the map so the line says which
    one filled up. The age-out above it is not warned about, because an entry
    past its timeout is one nothing was ever going to come back for.
    """
    cutoff = time.monotonic() - timeout
    for key in [key for key, value in entries.items() if timestamp(value) < cutoff]:
        del entries[key]
    while len(entries) >= cap:
        oldest = min(entries, key=lambda key: timestamp(entries[key]))
        logger.warning("Slack dropped the oldest of too many %s: %s", what, oldest)
        del entries[oldest]


def retry_after_seconds(exc: Exception) -> float | None:
    """Return how long to wait before retrying, or ``None`` if not rate limited.

    Only 429 / ``ratelimited`` is retryable. ``channel_not_found``,
    ``invalid_auth`` and friends are terminal: retrying them just delays the
    error the caller needs to see.

    This mirrors ``SlackHistoryToolkit._retry_after`` on the read path. The two
    are deliberately separate -- importing the agent-side toolkit into the
    gateway connector would drag in a 1,500-line module and invert the layering
    -- but they should stay behaviourally identical.
    """
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    data = response if isinstance(response, Mapping) else getattr(response, "data", None)
    if not isinstance(data, Mapping):
        data = {}
    if status != 429 and data.get("error") != "ratelimited":
        return None
    headers = getattr(response, "headers", None)
    if not isinstance(headers, Mapping):
        headers = data.get("headers")
    value: Any = None
    if isinstance(headers, Mapping):
        value = headers.get("Retry-After") or headers.get("retry-after")
    if isinstance(value, (list, tuple)):
        value = value[0] if value else None
    try:
        return max(0.0, min(float(value), _MAX_RETRY_AFTER_SECONDS))
    except (TypeError, ValueError):
        # Rate limited, but the header was absent or unparseable. Back off a
        # little rather than hammering or giving up.
        return _DEFAULT_RETRY_AFTER_SECONDS


def rejected_blocks_error(exc: Exception) -> str | None:
    """Return the error code if *exc* is Slack refusing the blocks, else ``None``.

    Narrow on purpose. These three say the structured rendering was unacceptable
    and say nothing about the message being undeliverable, so the same payload's
    ``text`` is still worth sending. Everything else -- ``channel_not_found``,
    ``invalid_auth``, ``ratelimited`` -- is about the message or the caller, and
    retrying it without blocks would only delay the error the caller needs.
    """
    response = getattr(exc, "response", None)
    data = response if isinstance(response, Mapping) else getattr(response, "data", None)
    if not isinstance(data, Mapping):
        return None
    error = data.get("error")
    return error if error in _BLOCK_REJECTION_ERRORS else None


#: The response header Slack answers **every** Web API call with, holding the
#: bot scopes the token actually carries as a comma-separated list. It is why
#: the startup check below costs no extra call: ``auth.test`` already runs, and
#: the answer to "what was this app granted" is in its headers.
#:
#: Read case-insensitively because the two things that can hold it disagree.
#: The async SDK hands over aiohttp's own ``CIMultiDictProxy``, which is
#: case-insensitive; a retry path, a recorded fixture or a test double hands
#: over a plain ``dict``, which is not, and HTTP/1.1 does not fix the casing.
_GRANTED_SCOPES_HEADER = "x-oauth-scopes"


def slack_granted_scopes(headers: Any) -> "frozenset[str] | None":
    """The bot scopes Slack says this token holds, or ``None`` for "it did not say".

    **The two answers are not the same and nothing may conflate them.** An empty
    frozenset is Slack stating that this token was granted nothing; ``None`` is
    this connector being unable to find out. The caller refuses to start on the
    first and starts anyway on the second, so reading one as the other would
    either kill a healthy channel or wave a broken install through.

    A header that is present but blank is read as ``None`` rather than as "no
    scopes", and the asymmetry of the two mistakes is the whole argument. A
    proxy or a recorded fixture that drops the *value* while keeping the key is
    indistinguishable here from Slack saying "none", and being wrong that way
    takes a working channel down over something that has nothing to do with the
    app's grants. Being wrong the other way costs one warning that is not
    printed. An app with no bot scopes at all cannot be installed anyway, so the
    reading this gives up on is one that does not occur.
    """
    if headers is None:
        return None
    getter = getattr(headers, "items", None)
    if not callable(getter):
        return None
    raw: Any = None
    for key, value in getter():
        if str(key).strip().lower() == _GRANTED_SCOPES_HEADER:
            raw = value
            break
    if raw is None:
        return None
    granted = {scope.strip() for scope in str(raw).split(",")}
    granted.discard("")
    return frozenset(granted) if granted else None


def slack_required_scopes() -> frozenset[str]:
    """The grants without which this connector will not start a channel.

    Tier 0 from :mod:`jiuwenswarm.common.slack_scope_policy`, less the entries
    that table's own ``NARROWINGS`` invites an operator to delete.

    **The subtraction is not a second opinion about the tier.** ``NARROWINGS``
    is rendered into the shipped manifests beside the scope it describes, so an
    operator reading ``groups:history`` is told there, in the file they are
    pasting into Slack, that deleting it and ``message.groups`` is how they keep
    the bot out of private channels. Refusing to start over a scope the manifest
    offers to remove would refuse a configuration this repository documents and
    supports. What is left is the set with no supported way to be absent: no
    ``chat:write`` and the bot cannot answer anything, no ``app_mentions:read``
    and it is never addressed in a channel, no ``channels:history`` or
    ``im:history`` and the messages never arrive.

    Derived rather than written out, so a scope added to the core tier is
    covered here without an edit, and a narrowing added there stops being a
    refusal without one either.
    """
    return frozenset(scopes_for_tier(TIER_CORE)) - frozenset(NARROWINGS)


def slack_error_code(exc: Exception) -> str:
    """Slack's own word for what went wrong, or ``""`` if it did not say one.

    The ``error`` field of the body, which is where the fact lives: Slack
    answers a refusal with HTTP 200 and ``ok: false``, so the status says
    nothing and only this field does. ``""`` covers every failure that never
    reached Slack at all -- a timeout, a socket error -- which is a different
    kind of thing from a refusal and must not be read as one.

    Lifted out of :func:`slack_failure_reason` rather than re-derived, because a
    caller that groups failures needs the code alone and the two readings of one
    response must not drift.
    """
    response = getattr(exc, "response", None)
    data = response if isinstance(response, Mapping) else getattr(response, "data", None)
    if not isinstance(data, Mapping):
        return ""
    return str(data.get("error") or "").strip()


def slack_failure_reason(exc: Exception) -> str:
    """What Slack refused with, on one line, with the code first.

    ``str`` of a ``SlackApiError`` runs to two lines. The first names the method
    and an HTTP status, and that status is ``200`` even for a refusal, because
    Slack answers ``ok: false`` in the body of a successful HTTP request. The
    code that says what actually happened is on the second line, inside the body
    it prints.

    A logger writes that verbatim. The line holding the warning's own prefix,
    its level and its module therefore ends at ``status: 200`` and says nothing,
    and the code lands on a following line attributed to nothing at all, so a
    grep for the warning finds only the half that cannot be acted on.
    ``chat.stopStream`` failures were read that way from 23 August to 7
    September. Every one of them held ``msg_too_long``.

    Lifting the code out and putting it first leaves one greppable line.
    Everything the exception said is still there behind it, with its newlines
    folded to spaces, because the URL and the body are what tell two failures
    with the same code apart.
    """
    code = slack_error_code(exc)
    detail = " ".join(str(exc).split()) or exc.__class__.__name__
    return f"{code}: {detail}" if code else detail


def drop_refused_blocks(kwargs: dict[str, Any], *, update_ts: str) -> None:
    """Take a refused rendering off a payload, in the one way that works.

    **Emptied on an edit, removed on a post.** ``chat.update`` only takes
    blocks away when the field is present and empty: omitting it leaves the
    message's existing blocks exactly where they were, so a refused rendering's
    predecessor would stay on screen beside the notice in ``text`` saying it had
    gone. A fresh post has nothing on screen yet, so there the field simply
    comes off.

    One function because there are two ways to reach this -- validation refusing
    a payload before it is sent, and Slack refusing one it was sent -- and the
    rule is a property of ``chat.update`` rather than of either path.

    The caller sets its own ``blocks`` local to ``None`` afterwards. That local
    is what the delivery log reads, and it says "no rendering was delivered",
    which is true in both spellings here.
    """
    if update_ts:
        kwargs["blocks"] = []
    else:
        kwargs.pop("blocks", None)


def _fenced_regions_stripped(text: str) -> str:
    """*text* with every fenced code block removed, opening and closing lines too.

    Used only to ask whether anything is left once every fence is taken out --
    never to build what is actually sent, which is *text* exactly as written
    on every path but the one this makes possible. A fence is never prose
    however wide it renders, recognised language or not: code shown back is
    still source, not a sentence a notification can hold, so an unrecognised
    fence counts against emptiness the same as a ```blockkit one does.

    The scan is the same one ``_normalize_slack_mrkdwn`` runs, kept separate
    because that one has to keep every line it does not touch and this one
    only has to keep the lines that are not fenced at all.
    """
    kept: list[str] = []
    fence_char = ""
    fence_length = 0
    for line in text.splitlines():
        if fence_char:
            closing_fence = _MARKDOWN_FENCE_CLOSE_RE.match(line)
            if closing_fence:
                marker = closing_fence.group("fence")
                if marker[0] == fence_char and len(marker) >= fence_length:
                    fence_char = ""
                    fence_length = 0
            continue
        opening_fence = _MARKDOWN_FENCE_OPEN_RE.match(line)
        if opening_fence:
            marker = opening_fence.group("fence")
            fence_char = marker[0]
            fence_length = len(marker)
            continue
        kept.append(line)
    return "\n".join(kept)


def _clamp(text: str, limit: int) -> str:
    """Shorten ``text`` to ``limit`` characters, marking where it was cut.

    Block Kit rejects a message holding one oversized field rather than
    truncating it, so every field built from agent-supplied text goes through
    here. The ellipsis is inside the budget: the limit is what Slack enforces.
    """
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    if limit == 1:
        return "…"
    return text[: limit - 1].rstrip() + "…"


def _clamp_harness_error(text: str) -> str:
    """Fit an error into the card's details field, saying so when it did not.

    ``_clamp`` is not used here. It cuts a field to a length and marks the cut
    with an ellipsis, which is right for a title and wrong for an error: an
    ellipsis at the end of a stack trace is easily read as the trace ending
    there. This says the word instead, and leaves the head rather than the tail
    -- the first lines are what name the failure, and an error long enough to
    need this does not belong in a chat message anyway.

    The budget is the whole field's, the sentence above the error included, so
    that the two together cannot exceed what Slack accepts.
    """
    room = _MAX_CARD_DETAILS_LENGTH - len(_HARNESS_ERROR_LEAD)
    if len(text) <= room:
        return text
    keep = max(0, room - len(_HARNESS_ERROR_TRIM_MARKER))
    return text[:keep].rstrip() + _HARNESS_ERROR_TRIM_MARKER


def _mentioned_user_ids(text: str) -> list[str]:
    """Slack user ids mentioned in ``text``, in order and without duplicates."""
    return list(dict.fromkeys(_USER_MENTION_RE.findall(text or "")))


def _https_host(url: str) -> str:
    """The lowercase host of *url*, or ``""`` unless it is a plain ``https`` URL.

    Everything that is not an ``https`` URL with a host answers the empty
    string, which no allow-list contains, so a malformed value, a ``file://``
    path and an unparseable one are all refused by the same comparison rather
    than by three special cases.

    ``https`` in particular, and not merely "has a host": httpx keeps an
    ``Authorization`` header across an ``http`` to ``https`` redirect to the
    same host, so an ``http://files.slack.com/…`` URL would put the bot token on
    the wire in the clear before the upgrade. Slack never spells one that way.
    """
    try:
        parsed = urlparse(str(url or ""))
    except ValueError:
        return ""
    if parsed.scheme != "https":
        return ""
    return (parsed.hostname or "").strip().lower()


def _slack_timestamp_ms(message_ts: str) -> int:
    """Convert a Slack ``ts`` ("1710000000.000100") to epoch milliseconds."""
    try:
        return int(float(message_ts) * 1000)
    except (TypeError, ValueError):
        return int(time.time() * 1000)


def resolve_blockkit_tables_mode(slack_conf: Mapping[str, Any]) -> str:
    """Pick the Block Kit table mode from a raw ``channels.slack`` config block.

    The mode names collide with YAML 1.1 booleans: an unquoted ``off`` parses as
    ``False`` long before this sees it, and ``on`` as ``True``. Reading the key
    as a plain string would turn the value that means "render nothing" into the
    default that renders -- the one misreading a config author cannot detect,
    because the word they wrote is the word they meant. Booleans are therefore
    mapped back to the mode they were written as, and ``on`` resolves to the
    default rather than being guessed at, since it names no single mode.
    """
    raw = slack_conf.get("blockkit_tables")
    if raw is None:
        return BLOCKKIT_TABLES_DEFAULT
    if isinstance(raw, bool):
        return BLOCKKIT_TABLES_OFF if raw is False else BLOCKKIT_TABLES_DEFAULT

    mode = str(raw).strip().lower()
    if not mode:
        return BLOCKKIT_TABLES_DEFAULT
    if mode not in BLOCKKIT_TABLES_MODES:
        logger.warning(
            "channels.slack.blockkit_tables=%r is not one of %s; using %r",
            raw, "/".join(BLOCKKIT_TABLES_MODES), BLOCKKIT_TABLES_DEFAULT,
        )
        return BLOCKKIT_TABLES_DEFAULT
    return mode


def resolve_streaming_mode(value: Any) -> str:
    """Read the boolean ``channels.slack.enable_streaming`` as an internal mode.

    The key asks one question -- preview the reply, or do not -- and this answers
    it with the best path available, which is what ``STREAMING_MODE_ENABLED``
    names. Which of the three modes a given reply actually gets is decided per
    reply in ``_new_stream``, because it turns on whether the reply has a thread,
    which a config file cannot know.

    The YAML 1.1 trap ``resolve_blockkit_tables_mode`` documents applies here and
    is harmless: an unquoted ``off`` parses as ``False`` and ``on`` as ``True``
    long before this sees them, which for a boolean key is the right answer.

    A value that is neither a boolean nor a spelling of one is read as
    **enabled**, not as off. The key has meant "true is on" for its whole life,
    so the failure that costs something is quietly withdrawing a preview a
    deployment already had, not granting one it typed badly.

    ``None`` is not that case. A key written with nothing after the colon names
    neither state, so it is read as unset and leaves the default alone.
    """
    if value is None:
        return STREAMING_MODE_DEFAULT
    if isinstance(value, bool):
        return STREAMING_MODE_ENABLED if value else STREAMING_OFF

    text = str(value).strip().lower()
    if not text:
        return STREAMING_MODE_DEFAULT
    # Spelled out for a config assembled in Python or read from an environment
    # variable, neither of which YAML has coerced on the way in.
    if text in ("false", "0", "no", "off"):
        return STREAMING_OFF
    if text in ("true", "1", "yes", "on"):
        return STREAMING_MODE_ENABLED
    logger.warning(
        "channels.slack.enable_streaming=%r is not a boolean; reading it as "
        "enabled, since that is what every value of this key other than false "
        "has ever meant",
        value,
    )
    return STREAMING_MODE_ENABLED


def resolve_streaming_enabled(value: Any) -> bool:
    """Read the boolean ``channels.slack.enable_streaming`` as a boolean.

    What the gateway stores, because the config field is a boolean and how a
    preview is written is not settled until there is a reply to write. Routed
    through ``resolve_streaming_mode`` rather than reimplementing ``bool()`` so
    that a value neither of them can read is answered the same way once --
    including the asymmetry that an unreadable value enables rather than
    disables.
    """
    return resolve_streaming_mode(value) != STREAMING_OFF


def render_tables_for_row_threshold(raw: Any) -> str | None:
    """Read a deprecated ``data_table_row_threshold`` as a ``render_tables`` mode.

    ``None`` means the value says nothing and the default should stand: the key
    is absent, or holds something that was never a row count -- a word, a
    boolean, a negative number.

    A number that *is* a row count maps to one of two modes, and neither
    reproduces it exactly, because no single mode can: the threshold split one
    table from another by length, and ``render_tables`` does not split at all.
    What survives is which side of the split the operator was on for the tables
    they actually send.

    ``0`` is exact: the comparison it fed was *more rows than the threshold* and
    nothing has fewer than zero, so it meant "every non-empty table becomes a
    ``data_table``".

    Every other threshold becomes ``basic``. A positive number said "keep a table
    plain until it grows past this", and in a chat reply almost every table is
    under the threshold. The cost is a table over ``MAX_PLAIN_TABLE_ROWS``, which
    a threshold would have made a ``data_table`` on size and which now declines
    to text; that is what the warning beside every call of this is for, and
    ``render_tables: data_table`` is the word that answers it.

    The config upgrade does the same reading and rewrites the old key into the
    new one in the operator's own file -- see
    ``_migrate_legacy_slack_render_tables`` in ``jiuwenswarm.common.config``. The
    two are separate because that one runs before the template merge, in a module
    that must not import a connector; a test pins them to the same answer.
    """
    # bool is an int subclass; True/False as a row count is never what was
    # meant, so it says nothing here rather than being read as 1 or 0.
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
        return None
    if raw == 0:
        return slack_blocks.RENDER_TABLES_DATA
    return slack_blocks.RENDER_TABLES_BASIC


KEY_APP_MESSAGES = "app_messages"
KEY_APP_MESSAGES_FROM = "app_messages_from"
APP_MESSAGES_NONE = "none"
APP_MESSAGES_LISTED = "listed"
APP_MESSAGES_ALL = "all"
# Ordered as the template lists them, narrowest first, so a warning that quotes
# the words quotes them in the order an operator read them in.
APP_MESSAGES_WORDS = (APP_MESSAGES_NONE, APP_MESSAGES_LISTED, APP_MESSAGES_ALL)
APP_MESSAGES_DEFAULT = APP_MESSAGES_NONE


def normalize_app_messages(raw: Any) -> str:
    """One of the three words, or the default for anything else.

    Total rather than partial, and applied where the word is *read* rather than
    only where config is parsed, because a ``SlackChannelConfig`` can be built
    by hand -- the gateway is one caller of many -- and a word nobody recognises
    must fail to the narrow end of the ladder wherever it came from.

    ``bool`` is rejected before anything else: YAML 1.1 reads a bare ``no`` as
    ``False``, and ``str(False).lower()`` is a word that is not on the list but
    is also not obviously a mistake when it appears in a warning.
    """
    if isinstance(raw, bool) or raw is None:
        return APP_MESSAGES_DEFAULT
    word = str(raw).strip().lower()
    return word if word in APP_MESSAGES_WORDS else APP_MESSAGES_DEFAULT


def resolve_app_messages(slack_conf: Mapping[str, Any] | None) -> str:
    """Whose app-posted messages this connector reads, from a raw config block.

    Three words, widening: ``none`` reads none of them, ``listed`` reads the
    apps named in ``app_messages_from``, ``all`` reads every app. ``none`` is
    the default, so a config that has not written the key keeps the behaviour it
    had, where anything carrying a ``bot_id`` was discarded before a trigger was
    looked for.

    A misspelling warns and falls to ``none`` rather than raising. The narrow
    end is the safe end of this particular ladder: the cost of the fallback is a
    deployment reading fewer messages than intended and saying so once per
    start, against a mistyped word silently making every app in the workspace
    audible.

    Unlike ``history``'s ``disabled``, ``none`` survives YAML 1.1 unquoted --
    the null tokens are ``null``/``Null``/``NULL``/``~``/empty, and ``none`` is
    none of them -- so the word can be written the way it reads.
    """
    if not isinstance(slack_conf, Mapping):
        return APP_MESSAGES_DEFAULT
    raw = slack_conf.get(KEY_APP_MESSAGES)
    if raw is None:
        return APP_MESSAGES_DEFAULT
    word = normalize_app_messages(raw)
    if isinstance(raw, bool) or word != str(raw).strip().lower():
        logger.warning(
            "channels.slack.%s=%r is not one of %s; reading no app messages"
            " (%s)",
            KEY_APP_MESSAGES,
            raw,
            "/".join(APP_MESSAGES_WORDS),
            APP_MESSAGES_DEFAULT,
        )
    return word


def resolve_app_messages_from(
    slack_conf: Mapping[str, Any] | None,
) -> tuple[str, ...]:
    """The app ids ``app_messages: listed`` admits, from a raw config block.

    Keyed on ``app_id`` and not on ``bot_id``. An ``app_id`` is global to the
    app and is the same string in every workspace it is installed in, while a
    ``bot_id`` and the bot user id are minted per installation -- so reinstalling
    an app changes them, and a list written against one stops matching without
    saying so. Nothing here is checked against Slack: an id naming no app simply
    never matches, which is indistinguishable from not having written it.

    Carried whatever the word is, and read only under ``listed``. Keeping the
    list rather than emptying it under the other two words is what lets an
    operator move down to ``none`` and back without retyping what they had
    decided, and the template says so where they will read it.
    """
    if not isinstance(slack_conf, Mapping):
        return ()
    return id_list(slack_conf.get(KEY_APP_MESSAGES_FROM))


def resolve_render_tables(slack_conf: Mapping[str, Any]) -> str:
    """Pick the table rendering from a raw ``channels.slack`` config block.

    Which of Slack's two table blocks a Markdown table becomes, or ``off`` for
    neither. The choice is the operator's and is made once for the channel,
    because the two blocks differ in what a reader can *do* with a table --
    ``data_table`` sorts, filters and downloads, ``table`` does none of those --
    and that is a property of who is reading, not of how many rows arrived.

    The YAML 1.1 trap ``resolve_blockkit_tables_mode`` documents applies here
    with more force, because this key's ``off`` is the value with teeth: an
    unquoted ``off`` reaches this as ``False`` long before it is read as a word,
    and reading it as a plain string would turn "render no tables" into the
    default that renders every one of them. ``on`` names no single mode, so it
    resolves to the default rather than being guessed at.

    ``data_table_row_threshold`` is the retired predecessor and is read only by
    a config that has not set this key, so an operator who has not upgraded
    their file keeps the behaviour they had rather than being moved to the
    default under them -- see ``render_tables_for_row_threshold`` for how a
    threshold is read as a mode and what it costs. An unrecognised value of
    *this* key does not reach it: a misspelling is a misspelling, not a request
    to fall back to a key that was removed.
    """
    raw = slack_conf.get("render_tables")
    if isinstance(raw, bool):
        return (
            slack_blocks.RENDER_TABLES_DEFAULT
            if raw
            else slack_blocks.RENDER_TABLES_OFF
        )
    mode = str(raw if raw is not None else "").strip().lower()
    if mode in slack_blocks.RENDER_TABLES_MODES:
        if "data_table_row_threshold" in slack_conf:
            logger.warning(
                "channels.slack.data_table_row_threshold is no longer read; "
                "channels.slack.render_tables=%r decides the table rendering. "
                "Delete the old key -- a config upgrade removes it for you",
                mode,
            )
        return mode
    if mode:
        logger.warning(
            "channels.slack.render_tables=%r is not one of %s; using %r",
            raw, "/".join(slack_blocks.RENDER_TABLES_MODES),
            slack_blocks.RENDER_TABLES_DEFAULT,
        )
        return slack_blocks.RENDER_TABLES_DEFAULT

    legacy = render_tables_for_row_threshold(
        slack_conf.get("data_table_row_threshold")
    )
    if legacy is None:
        return slack_blocks.RENDER_TABLES_DEFAULT
    logger.warning(
        "channels.slack.data_table_row_threshold=%r is deprecated and is being "
        "read as channels.slack.render_tables=%r; a table's size no longer "
        "decides which block it becomes. Write render_tables to choose: %s",
        slack_conf.get("data_table_row_threshold"), legacy,
        "/".join(slack_blocks.RENDER_TABLES_MODES),
    )
    return legacy


def resolve_blockkit_allowed_block_types(
    slack_conf: Mapping[str, Any],
) -> tuple[str, ...]:
    """Which Block Kit types a hand-written fence may hold, from a raw config block.

    An empty result means "no restriction", and that is what an unset, empty or
    unusable value all come to. It is the default because which block types Slack
    draws is Slack's to say and changes without this repository hearing about it:
    a name missing from a hard-coded list would cost an author the whole
    message's rendering for a block Slack would have drawn.

    This is a portability preference, not the safety control. Widening it does
    not admit anything a reader can click -- see
    ``resolve_blockkit_allow_interactive``, which is separate.

    A single name written without a list is read as a list of one, which is the
    mistake the YAML invites. Anything else is logged and dropped, and a value
    that leaves nothing behind is read as "no restriction" rather than "refuse
    everything", so a mistyped config cannot silently cost every hand-written
    block in the channel.
    """
    raw = slack_conf.get("blockkit_allowed_block_types")
    if raw is None or raw == "":
        return ()
    if isinstance(raw, str):
        entries: list[Any] = [raw]
    elif isinstance(raw, (list, tuple, set, frozenset)):
        entries = list(raw)
    else:
        logger.warning(
            "channels.slack.blockkit_allowed_block_types=%r is not a list of block"
            " type names; no block type is restricted",
            raw,
        )
        return ()

    names: list[str] = []
    for entry in entries:
        if isinstance(entry, str) and entry.strip():
            names.append(entry.strip().lower())
        else:
            logger.warning(
                "channels.slack.blockkit_allowed_block_types entry %r is not a"
                " block type name; ignoring it",
                entry,
            )
    if entries and not names:
        logger.warning(
            "channels.slack.blockkit_allowed_block_types=%r named no usable block"
            " type; no block type is restricted",
            raw,
        )
    # Deduplicated, order kept, so the metadata reads back as it was written.
    return tuple(dict.fromkeys(names))


def resolve_blockkit_allow_interactive(slack_conf: Mapping[str, Any]) -> bool:
    """Whether a hand-written fence may hold buttons, selects and inputs.

    Off, and it fails closed on anything it cannot read. This connector posts
    real permission-approval buttons into the same channels it posts replies
    into, and a reader cannot tell one an author wrote from one the approval flow
    wrote -- they are the same pixels, and clicking either sends an interaction to
    this app. Every other key here recovers to its default on an unreadable
    value, which costs a formatting choice; here it would cost the distinction
    between a real approval prompt and a decorative one.

    Not folded into ``blockkit_allowed_block_types``. Naming ``actions`` or
    ``section`` there says which blocks this workspace draws well; it does not
    say that a model's reply may ask a reader to click something.
    """
    raw = slack_conf.get("blockkit_allow_interactive")
    if raw is None:
        return slack_blocks.DEFAULT_ALLOW_INTERACTIVE_BLOCKS
    if isinstance(raw, bool):
        return raw
    text = str(raw).strip().lower()
    if text in ("true", "1", "yes", "on"):
        return True
    if text in ("", "false", "0", "no", "off"):
        return False
    logger.warning(
        "channels.slack.blockkit_allow_interactive=%r is not a boolean; using %r",
        raw, slack_blocks.DEFAULT_ALLOW_INTERACTIVE_BLOCKS,
    )
    return slack_blocks.DEFAULT_ALLOW_INTERACTIVE_BLOCKS


def resolve_blockkit_validate(slack_conf: Mapping[str, Any]) -> str:
    """Pick how much is spent on ``blocks.validate`` from a raw config block.

    Falls back to the default on anything it cannot read, rather than refusing
    as ``blockkit_allow_interactive`` does. That key decides whether a reader can
    be asked to click something; this one decides only how good the diagnosis is
    when a payload turns out to be wrong.
    """
    raw = slack_conf.get("blockkit_validate")
    if raw is None:
        return BLOCKKIT_VALIDATE_DEFAULT
    # A YAML ``off`` is the boolean False, not the word: the mode an operator
    # typed has to survive the loader turning it into one. ``true`` is read as
    # the widest setting for the same reason -- it is what someone writing a
    # boolean into a key that takes words meant.
    if isinstance(raw, bool):
        return BLOCKKIT_VALIDATE_ALL if raw else BLOCKKIT_VALIDATE_OFF
    mode = str(raw).strip().lower()
    if mode in BLOCKKIT_VALIDATE_MODES:
        return mode
    if mode:
        logger.warning(
            "channels.slack.blockkit_validate=%r is not one of %s; using %r",
            raw, BLOCKKIT_VALIDATE_MODES, BLOCKKIT_VALIDATE_DEFAULT,
        )
    return BLOCKKIT_VALIDATE_DEFAULT


def resolve_acknowledge_mode(slack_conf: Mapping[str, Any]) -> str:
    """Pick the acknowledgement mode from a raw ``channels.slack`` config block.

    ``acknowledge_mode`` decides on its own whenever it names a known mode, so a
    config holding both keys is answered by the mode and the deprecated boolean
    is not read at all. The boolean is consulted only when the mode is absent or
    blank; an unrecognised mode is a misspelling rather than a request to fall
    back, and resolves to the default without reaching the boolean either.

    ``acknowledge_requests`` predates the mode, where it was a plain on/off
    toggle over a single text acknowledgement: ``true`` posted
    ``acknowledgement_text``, ``false`` acknowledged nothing. It is mapped onto
    the mode that does the same thing -- ``true`` to ``text``, ``false`` to
    ``off`` -- so a deployment that set it keeps the behaviour it had rather
    than gaining the reactions the mode defaults to.

    A key written with nothing after the colon is ``null`` by the time this sees
    it, which names neither state of a toggle. It is read as unset, leaving the
    default in place: that is what the key already resolved to, and it lets the
    templates ship the key with no value so a config upgrade stops deleting an
    operator's ``true`` or ``false``.

    The YAML 1.1 trap ``resolve_blockkit_tables_mode`` documents reaches this key
    too, and costs more here than anywhere else it applies: an unquoted ``off``
    is the boolean ``False`` long before this reads it, and a mode read as a
    plain string would fall through to the default and acknowledge every request
    with the reaction the operator wrote the line to stop. The boolean is
    therefore mapped back to the mode it was written as. It is read before
    ``acknowledge_requests`` rather than as one, because the two keys are
    different questions and a config setting both must still be answered by the
    mode.
    """
    raw_mode = slack_conf.get("acknowledge_mode")
    if isinstance(raw_mode, bool):
        # ``true`` names no single mode -- reaction, text and both are all
        # acknowledgements -- so it resolves to the default rather than being
        # guessed at, which is what ``on`` gets from the three sibling keys.
        return ACK_MODE_OFF if raw_mode is False else ACK_MODE_REACTION
    mode = str(raw_mode or "").strip().lower()
    if mode in ACKNOWLEDGE_MODES:
        return mode
    if mode:
        logger.warning(
            "channels.slack.acknowledge_mode=%r is not one of %s; using %r",
            mode, "/".join(ACKNOWLEDGE_MODES), ACK_MODE_REACTION,
        )
        return ACK_MODE_REACTION

    legacy_raw = slack_conf.get("acknowledge_requests")
    if legacy_raw is not None:
        legacy_on = (
            str(legacy_raw).strip().lower() in ("true", "1", "yes", "on")
            if isinstance(legacy_raw, str)
            else bool(legacy_raw)
        )
        resolved = ACK_MODE_TEXT if legacy_on else ACK_MODE_OFF
        logger.warning(
            "channels.slack.acknowledge_requests is deprecated;"
            " set acknowledge_mode: %s instead",
            resolved,
        )
        return resolved
    return ACK_MODE_REACTION


def resolve_reaction_emoji(configured: Any, default: str) -> str:
    """One ``*_emoji`` key as the name to react with, or ``""`` for no mark.

    The six marks are a vocabulary, not one feature, and an operator who finds
    one of them wrong in their workspace was previously offered a choice
    between every mark and none: blanking a key hands back the default, and
    ``acknowledge_mode: off`` removes the five they wanted to keep along with
    the one they did not. ``REACTION_DISABLED`` is the per-key lever, and it is
    read the same way for all six so that silencing the completion tick and
    silencing the queue hourglass are the same operation.

    An empty value still falls back to *default*, deliberately unchanged: that
    is what a key written with nothing after the colon resolves to, which is
    how the template ships a key without overwriting an operator's value on
    upgrade, and it is what ``queued_status`` and ``steered_status`` next to
    these keys already mean by blank.

    The word is matched before the colons are stripped, so ``:disabled:`` is
    the escape for a workspace whose custom emoji is named ``disabled``.

    Returning ``""`` rather than a flag of its own is what keeps the call sites
    out of this: ``_add_reaction`` and ``_remove_reaction`` both already treat
    an empty name as nothing to do, so a silenced mark is neither added nor
    removed and every ordering they document still holds. A turn whose ending
    is silenced still has its acknowledgement removed, which is the point --
    the message is left bare rather than wearing a "still running" mark for
    good.
    """
    value = str(configured or "").strip()
    if not value:
        return default
    if value == REACTION_DISABLED:
        return ""
    return value


# The two logger trees the Slack libraries write to. Both sit outside the
# ``jiuwenswarm`` namespace, which is the only tree ``setup_logger`` attaches
# handlers to, so until something does what ``configure_sdk_logging`` does below
# they are not quiet -- they are unwired. Nothing they say has anywhere to go.
#
# That is not a cosmetic gap. Every decision slack_bolt makes about an envelope
# is reported there, including the exceptions it catches on a listener's behalf
# rather than letting them propagate, and slack_sdk's socket-mode client reports
# each frame it receives and each message it enqueues there too. A message that
# never reaches this connector's own logging is, today, indistinguishable from
# one Slack never sent.
SDK_LOGGER_NAMES: tuple[str, ...] = ("slack_bolt", "slack_sdk")
# The logger handed to ``AsyncApp(logger=...)``. A child of the bolt tree, and
# deliberately one with no handlers of its own: bolt copies a base logger's
# handlers onto every logger it builds and leaves those loggers propagating, so
# handing it a logger that *has* handlers writes every bolt line twice -- once
# on the child and once again on the tree above it. Passing only the level down
# and letting the records propagate up to the handlers on ``slack_bolt`` is what
# keeps it to one line.
SDK_BASE_LOGGER_NAME = "slack_bolt.jiuwenswarm"
# Deliberately not tied to ``logging.level``. These are far chattier than the
# connector: at DEBUG slack_sdk writes a line per websocket frame, per enqueue
# and per dispatch, which is exactly what is wanted for an afternoon spent
# chasing a lost message and not what anyone wants running all week. WARNING is
# the libraries' own default and keeps their errors -- which is the part that
# was going nowhere -- without the volume.
DEFAULT_SDK_LOG_LEVEL = "WARNING"
SDK_LOG_LEVELS: tuple[str, ...] = (
    "CRITICAL",
    "ERROR",
    "WARNING",
    "INFO",
    "DEBUG",
)


def resolve_sdk_log_level(slack_conf: Mapping[str, Any]) -> str:
    """Pick the Slack SDK log level from a raw ``channels.slack`` config block.

    An unrecognised value is a misspelling rather than a request for silence, so
    it is logged and resolves to the default. A key written with nothing after
    the colon is ``null`` by the time this sees it and reads as unset, which is
    what lets the template ship the key without overwriting an operator's value
    on upgrade.
    """
    raw = str(slack_conf.get("sdk_log_level") or "").strip().upper()
    if not raw:
        return DEFAULT_SDK_LOG_LEVEL
    if raw in SDK_LOG_LEVELS:
        return raw
    logger.warning(
        "channels.slack.sdk_log_level=%r is not one of %s; using %s",
        raw,
        "/".join(SDK_LOG_LEVELS),
        DEFAULT_SDK_LOG_LEVEL,
    )
    return DEFAULT_SDK_LOG_LEVEL


def configure_sdk_logging(level_name: str) -> logging.Logger:
    """Point ``slack_bolt`` and ``slack_sdk`` at the handlers everything else uses.

    Returns the logger to hand to ``AsyncApp(logger=...)``, which is not
    optional decoration. Bolt builds a logger per internal class and, when no
    base logger is given, pins each one's level to ``logging.root``'s -- so
    setting the level on the ``slack_bolt`` tree here does nothing for any of
    them, because an explicit level on a child wins over its parent's. Passing
    a base logger in makes bolt copy that level onto every logger it creates.
    The same object then reaches ``slack_sdk``'s socket-mode client, which the
    adapter builds with ``app.logger``: one argument covers both trees.

    That returned logger is ``SDK_BASE_LOGGER_NAME`` and holds no handlers of
    its own; see the note there for why handing bolt one that does would double
    every line it writes.

    The handlers are the ones already on the ``jiuwenswarm`` logger rather than
    new ones, which is what makes this safe to do at all: every one of them
    holds ``SensitiveDataFilter``, and these two trees are the ones that
    genuinely do hold tokens and whole message bodies. Building a handler here
    would mean re-deriving that filtering, and getting it subtly wrong is how a
    bot token ends up in a log file.

    ``propagate`` is turned off for the same reason it is off on the
    ``jiuwenswarm`` logger: with handlers attached here, propagation would only
    add an unfiltered second copy by way of the root logger.

    The effective floor is reported because it is a real trap. Each of those
    handlers has its own level, so asking for DEBUG here does not by itself put
    DEBUG anywhere -- ``logging.console`` and ``logging.full`` have to allow it
    too. Saying so once at startup is cheaper than an operator concluding the
    SDK is silent when it is the handler in front of it that is.
    """
    level = logging.getLevelName(level_name)
    if not isinstance(level, int):
        level = logging.WARNING
    shared = list(logging.getLogger("jiuwenswarm").handlers)
    for name in SDK_LOGGER_NAMES:
        sdk_logger = logging.getLogger(name)
        sdk_logger.setLevel(level)
        sdk_logger.propagate = False
        for handler in shared:
            if handler not in sdk_logger.handlers:
                sdk_logger.addHandler(handler)
    floor = min((handler.level for handler in shared), default=level)
    logger.info(
        "Slack SDK logging wired: loggers=%s level=%s handlers=%d"
        " effective_floor=%s",
        "/".join(SDK_LOGGER_NAMES),
        logging.getLevelName(level),
        len(shared),
        logging.getLevelName(max(level, floor)),
    )
    base = logging.getLogger(SDK_BASE_LOGGER_NAME)
    base.setLevel(level)
    return base


@dataclass(frozen=True)
class SlackChannelOverride:
    """What one conversation's scopes settled, in the shape this connector reads.

    Named for the map it fills. It is the carrier every composed scope arrives
    in, and the three per-conversation call sites read it and nothing else.

    ``None`` means no layer spoke about the key and the connector's own default
    applies; every other value is something a scope set, including the empty
    ones. The distinction is the whole contract: ``mode: []`` is a request for
    silence and ``prompt: ""`` a request to append nothing, and reading either
    as "absent" would answer the operator with the opposite of what they asked
    for.

    ``model_name``, ``mid_turn``, ``session``, ``reply`` and ``history`` have no
    empty state for a value to mean, so each is one value or ``None``: a name
    found in ``models.defaults``, one of ``MID_TURN_VALUES``, one of
    ``SESSION_VALUES``, one of ``REPLY_VALUES``, one of
    ``HISTORY_POLICY_VALUES``. The loader has already refused anything else.

    ``events`` is the one mapping on this carrier, and the type is what gives it
    its composition rule: a mapping merges per key, so a conversation naming one
    family keeps whatever the platform layer said about the others. ``None``
    means no layer spoke; ``{}`` means a layer spoke and named nothing, which is
    every family ``off`` and is a different statement from silence even though
    the two behave alike today.

    ``history`` is carried and never consumed here -- the connector settles the
    word and stamps it onto the request, and the runtime's history toolkit is
    what acts on it, because the gate the word names is a membership comparison
    taken against Slack at read time rather than a value anything can settle at
    load.
    """

    mode: frozenset[str] | None = None
    prompt: str | None = None
    model_name: str | None = None
    mid_turn: str | None = None
    session: str | None = None
    reply: str | None = None
    events: "dict[str, str] | None" = None
    history: str | None = None
    subagents: frozenset[str] | None = None
    skills: "dict[str, list[str]] | None" = None


@dataclass(frozen=True)
class ConfiguredModels:
    """The names a request may put in ``model_name`` and expect to be honoured.

    Built from ``get_default_models`` -- the same call
    ``JiuWenSwarmDeepAdapter._build_model_cache_from_defaults`` iterates to fill
    the cache a chat turn is resolved against. Sharing the call is the point: a
    name this accepts is a name that cache can resolve, and a name it rejects is
    one the adapter would have silently swapped for the default. Reading
    ``models.defaults`` directly instead would be a second source of truth that
    drifts the first time either side changes, and the drift would surface as a
    channel quietly running on a model nobody chose.

    ``aliases`` are accepted alongside ``names`` because the adapter registers
    both: ``_register_model_cache_entry`` keys the cache by ``model_name`` and,
    when the entry holds one, by its ``alias`` too. Both live on the entries
    this reads, so accepting them adds no second source.

    The ``{model_name}#{index}`` cache key is deliberately not accepted. It is
    the adapter's internal disambiguator for two entries sharing a name, not a
    name anything writes into a config file, and a plain ``model_name`` already
    resolves to the same entry through ``_model_name_to_keys``. Rejecting it
    costs an operator who pasted one a warning naming what to write instead;
    accepting it would mean this module tracking a key format it does not own.
    """

    names: tuple[str, ...] = ()
    aliases: tuple[str, ...] = ()

    @property
    def known(self) -> bool:
        """Whether the model list could be read at all."""
        return bool(self.names)

    @property
    def default(self) -> str:
        """The model a request without a usable ``model_name`` lands on.

        The adapter points its default at the first entry marked
        ``is_default``, and ``_infer_is_default`` marks the first entry of each
        distinct ``model_name``, so in configured order that is the first name
        here.
        """
        return self.names[0] if self.names else ""

    def accepts(self, name: str) -> bool:
        return name in self.names or name in self.aliases

    def describe(self) -> str:
        """The valid choices, for a warning that has to be actionable."""
        if not self.names:
            return "none"
        listed = ", ".join(self.names)
        if self.aliases:
            listed += f" (or the aliases {', '.join(self.aliases)})"
        return listed

    def describe_fallback(self) -> str:
        """Name the model a dropped override leaves the channel on."""
        return (
            f"the default model {self.default}"
            if self.default
            else "whatever model the server defaults to"
        )


def configured_models() -> ConfiguredModels:
    """Read ``models.defaults`` as the set of names a request may ask for.

    Returns an empty ``ConfiguredModels`` rather than raising if the config
    cannot be read. Every caller reads "no names known" as "do not validate",
    which leaves the channel exactly where it already was instead of dropping a
    correct override -- or taking the connector down -- over a transient read.
    """
    try:
        from jiuwenswarm.common.config import get_default_models

        names: list[str] = []
        aliases: list[str] = []
        for entry in get_default_models():
            if not isinstance(entry, Mapping):
                continue
            mcc = entry.get("model_client_config")
            mcc = mcc if isinstance(mcc, Mapping) else {}
            name = str(mcc.get("model_name") or "").strip()
            if not name:
                # The adapter skips a nameless entry outright, so there is
                # nothing here a request could ask for.
                continue
            if name not in names:
                names.append(name)
            alias = str(entry.get("alias") or "").strip()
            if alias and alias != name and alias not in aliases:
                aliases.append(alias)
        return ConfiguredModels(tuple(names), tuple(aliases))
    except Exception:
        logger.warning(
            "channels.slack could not read models.defaults to check the model"
            " names written in scopes; leaving them unchecked",
            exc_info=True,
        )
        return ConfiguredModels()


def resolve_group_chat_mode(raw: Any) -> str:
    """One raw ``channels.slack.group_chat_mode`` value as one of the four words.

    The words collide with YAML 1.1 booleans exactly the way the Block Kit modes
    do, and ``off`` is again the value with teeth: an unquoted ``off`` is
    ``False`` by the time any reader here sees it, and reading it as a plain
    string would leave the bot answering the mentions that line was written to
    silence -- ``mention`` is the default, and it is the one word ``off`` exists
    to rule out. The boolean is therefore mapped back to the word it was written
    as, and ``true`` resolves to the default rather than being guessed at: it
    names no single mode, since reply and all are both "more than mention".

    Every raw read of the key goes through here so the dataclass field, the
    layer-0 trigger seed and the startup line cannot come to disagree about what
    an operator wrote. Case and surrounding space are settled here for the same
    reason. A word outside the four is left as it was written: the callers
    already fall back to the default on a word they do not recognise, and each
    says so in its own terms.
    """
    if isinstance(raw, bool):
        return GROUP_MODE_OFF if raw is False else GROUP_MODE_MENTION
    return str(raw or "").strip().lower()


def _slack_layer0_triggers(group_chat_mode: Any) -> frozenset[str]:
    """The triggers a conversation would have with no scope at all.

    Layer 0 of the cascade, and the only thing a signed ``mode`` list has to
    mutate: ``mode: [+has_file]`` on a conversation means "whatever it already
    answered, plus files", and the answer to "already" is ``group_chat_mode``.

    The same step ``channel_triggers`` takes once an override has declined to
    answer, so it is also the last fallback. It is per platform rather than per
    conversation -- ``group_chat_mode`` is one global string -- so nothing here
    needs to know which conversation is being composed.

    Both composition paths call it: the settled platform layer built once per
    config apply, and the per-sender fold ``settled_override`` does when a rule
    names people. They have to agree, because each is the seed the same signed
    ``mode`` list mutates, and two seeds would compose one channel's ``+has_file``
    into two different trigger sets depending on whether a message's sender
    happened to be named by a rule.

    Takes the raw value rather than a settled one, because one of its two
    callers reads ``channels.slack`` directly. ``resolve_group_chat_mode`` is
    what makes the two agree about an unquoted ``off``.
    """
    mode = resolve_group_chat_mode(group_chat_mode)
    return _LEGACY_MODE_TRIGGERS.get(mode, _LEGACY_MODE_TRIGGERS[GROUP_MODE_MENTION])


def _as_trigger_set(value: Any) -> frozenset[str]:
    """A settled ``mode`` value as a set of trigger names.

    Defensive about a bare string for one reason: ``frozenset("all")`` is
    ``{"a", "l"}``, which would be a silent, unreadable corruption of a
    channel's triggers rather than an error anybody could see.
    """
    if isinstance(value, str):
        return _LEGACY_MODE_TRIGGERS.get(value.strip().lower(), frozenset())
    if isinstance(value, (frozenset, set, list, tuple)):
        return frozenset(str(item) for item in value)
    return frozenset()


def sections_as_override(
    sections: Mapping[str, Mapping[str, Any]]
) -> SlackChannelOverride:
    """Composed sections as the entry the connector already reads.

    ``SlackChannelOverride`` is the carrier: every place that consumes a
    per-channel setting reads that dataclass, and ``None`` on it means "no layer
    spoke, so the global applies". Composition produces exactly that -- a key
    nothing set is missing from the mapping rather than present and empty.

    That the carrier spans two sections is a property of this connector rather
    than a hole in the split. It is the one object every per-conversation reader
    already takes its answer from, and a second per-channel map for the two
    ``agent`` keys would double the plumbing for something no caller asks
    separately.
    """
    delivery = sections.get(SECTION_DELIVERY) or {}
    agent = sections.get(SECTION_AGENT) or {}
    mode = delivery.get("mode")
    prompt = delivery.get("prompt")
    mid_turn = delivery.get(KEY_MID_TURN)
    session = delivery.get(KEY_SESSION)
    reply = delivery.get(KEY_REPLY)
    events = delivery.get(KEY_EVENTS)
    model_name = agent.get("model_name")
    history = agent.get(KEY_HISTORY)
    subagents = agent.get("subagents")
    skills = agent.get("skills")
    return SlackChannelOverride(
        mode=_as_trigger_set(mode) if mode is not None else None,
        prompt=str(prompt) if prompt is not None else None,
        # Taken as settled. The loader refuses anything that is not one of the
        # three and drops the key, so a value arriving here is canonical and
        # re-checking it would be a second copy of a list this module does not
        # own -- the copy that goes stale when a fourth value is added.
        mid_turn=str(mid_turn) if mid_turn is not None else None,
        # Taken as settled for the same reason mid_turn is, and by the same
        # loader branch: the two words are a closed vocabulary the shared schema
        # owns, so a value arriving here has already been recognised.
        session=str(session) if session is not None else None,
        # Taken as settled for the reason the two above are, and by the same
        # loader branch: the shared schema owns the two words and has already
        # recognised the value.
        reply=str(reply) if reply is not None else None,
        # Re-checked, unlike the three words above, for the reason ``history``
        # is: the families and the two dispositions are a vocabulary a mapping
        # can hold a typo in per entry, and a mapping reaching here from
        # anything but the loader must land on the narrow side. Normalising also
        # settles case and space, which the connector ``validator`` may not do
        # -- it can only say whether a value is usable, so it leaves the value
        # exactly as written.
        events=normalize_event_policy(events),
        model_name=(str(model_name).strip() or None) if model_name is not None else None,
        # Re-checked, unlike mid_turn, and the difference is deliberate. This is
        # a *read* gate: a word the loader has not vetted -- because the Slack
        # capability declaration that vets it is a separate change, or because
        # something other than the loader built this mapping -- must land on the
        # narrow side rather than be passed through as canonical. An
        # unrecognised word is dropped to None, which leaves layer 0 answering.
        history=normalize_history_policy(history) or None,
        subagents=(
            frozenset(subagents)
            if isinstance(subagents, (list, tuple, set, frozenset))
            else None
        ),
        skills={key: list(names) for key, names in skills.items()} if isinstance(skills, Mapping) else None,
    )


def load_slack_scopes() -> tuple[Scope, ...]:
    """Compile the top-level ``scopes:`` list, or nothing if it cannot be read.

    Read here rather than handed down from ``_apply_channel_config``, which is
    given the ``channels`` block alone and has no top-level config to pass. The
    idiom is the one ``configured_models`` already uses for ``models.defaults``:
    the connector asks the config module for the section it needs, once per
    config apply rather than once per message.

    ``people:`` and ``roles:`` are read alongside, and must be: a ``role:`` on a
    scope resolves against them, and compiling without them would leave every
    role undeclared and drop every scope naming one -- loudly, but for a config
    that was perfectly good.
    """
    try:
        from jiuwenswarm.common.config import get_config

        data = get_config()
    except Exception:
        logger.warning(
            "channels.slack could not read the top-level scopes list; no scope"
            " applies and every channel follows channels.slack alone",
            exc_info=True,
        )
        return ()
    if not isinstance(data, Mapping):
        return ()
    channels = data.get("channels")
    return compile_scopes(
        data.get("scopes"),
        channels_config=channels if isinstance(channels, Mapping) else None,
        people=data.get(PEOPLE_KEY),
        roles=data.get(ROLES_KEY),
    )


def apply_scopes_to_slack_overrides(
    slack_conf: Mapping[str, Any],
    *,
    scopes: "Sequence[Scope] | None" = None,
) -> tuple[SlackChannelOverride, dict[str, SlackChannelOverride]]:
    """Fold ``scopes`` into the two things this connector reads per conversation.

    Returns the platform-wide layer and the per-conversation map. The first is
    what a scope matching ``{channel: slack}`` settled -- layer 1, above
    ``group_chat_mode`` and below anything naming a conversation. The second
    holds every conversation a scope names.

    **With nothing written, nothing is returned.** No scopes means an empty map
    and an empty platform layer, which is the connector following
    ``channels.slack`` alone.

    ``prompt_append`` composes over scope layers only: there is no connector-wide
    prompt for it to inherit, so ``delivery.prompt`` has no layer 0. ``mode``
    does, because ``group_chat_mode`` is exactly that and ``+has_file`` means
    nothing without a set to add to.

    Two sections are folded, separately, and only ``delivery`` is given a layer
    0. ``agent`` needs none. ``model_name`` has no ``channels.slack`` key
    beneath it at all; ``history`` does -- ``channels.slack.history``, registered
    as its layer-0 twin -- but it is a word from a closed vocabulary with no
    append form, so there is no base for a scope to mutate. Its fallback to the
    connector key is taken where it is read, in
    :func:`slack_history_request_metadata`.
    """
    if not scopes:
        return SlackChannelOverride(), {}

    def _compose(chat: "str | None") -> dict[str, Mapping[str, Any]]:
        return {
            SECTION_DELIVERY: compose_section(
                scopes,
                channel="slack",
                chat=chat,
                section=SECTION_DELIVERY,
                layer0={
                    "mode": _slack_layer0_triggers(slack_conf.get("group_chat_mode"))
                },
            ),
            SECTION_AGENT: compose_section(
                scopes, channel="slack", chat=chat, section=SECTION_AGENT
            ),
        }

    platform = sections_as_override(_compose(None))

    resolved: dict[str, SlackChannelOverride] = {}
    for channel_id in sorted(scoped_chats(scopes, channel="slack")):
        override = sections_as_override(_compose(channel_id))
        # A conversation whose scopes settle to nothing is kept all the same:
        # the id is what the startup summary lists. Availability-only settings
        # do not exempt it from allowed_channel_ids.
        resolved[channel_id] = override
        if override.mode is not None:
            _warn_if_mode_is_unusual(f"scopes for {channel_id}", override.mode)

    if platform.mode is not None:
        _warn_if_mode_is_unusual("the scope for channel slack", platform.mode)

    return platform, resolved


def _warn_if_mode_is_unusual(label: str, triggers: frozenset[str]) -> None:
    """Say when a composed trigger set is legal but probably not what was meant.

    Deliberately not refusals. A channel that watches for links or files without
    being conversational is a legitimate pure-ingest configuration, and silence
    is a legitimate thing to ask for; refusing either would refuse a use case
    this exists to support. They are unusual enough to be worth one line,
    because the same result is more often a slip -- particularly here, where it
    can be the *composition* of two correct-looking layers rather than anything
    an operator wrote in one place.
    """
    if not triggers:
        logger.warning(
            "%s settle on no triggers at all; that conversation is now silent"
            " and answers nothing, not even @mentions of the bot",
            label,
        )
        return
    if TRIGGER_ALL in triggers:
        # ``mention`` is strictly redundant beside ``all`` too, and is left out
        # of this list on purpose. ``+all`` written on a conversation that
        # already answers mentions composes to exactly {all, mention}, so
        # naming it here would warn at the composition operators reach for
        # first. The set arriving here says what a conversation settled on and
        # not which layer wrote each entry, so the warning cannot tell that
        # composition apart from someone typing both by hand.
        redundant = sorted(triggers & {TRIGGER_REPLY, TRIGGER_URL, TRIGGER_HAS_FILE})
        if redundant:
            logger.warning(
                "%s settle on all alongside %s; all already matches every"
                " channel message, so those entries add nothing",
                label,
                ", ".join(redundant),
            )
    # ``all`` counts as listing it: the mention route honours ``all`` the same
    # way the message route does, so a conversation set to answer everything
    # answers mentions and must not be told otherwise.
    if not (triggers & MENTION_ROUTE_TRIGGERS):
        logger.warning(
            "%s settle on [%s] and name neither mention nor all; that"
            " conversation ignores @mentions of the bot. Add mention, or"
            " +mention, to keep answering them",
            label,
            ", ".join(sorted(triggers)),
        )


@dataclass(frozen=True)
class _QuestionOption:
    """One choice a question offers, holding everything its button needs.

    Named fields rather than a positional tuple, so that widening it is additive:
    a field with a default touches only the code that reads it.

    Built in one pass from the question and only read afterwards, so it is
    frozen.
    """

    # What the button says.
    label: str
    # What the agent is answered with, which is the option's own ``value`` when
    # it has one and its label otherwise -- the same precedence every other
    # client applies, and the reason an approval answers "approve" rather than
    # "Approve".
    value: str
    # What choosing this option does. Empty for the several builders that supply
    # none. Kept on the option rather than looked up separately so that the
    # descriptions shown are exactly the options offered.
    description: str = ""
    # The Slack button style this option earns, "" for the neutral default.
    style: str = ""


def _option_button_style(intent: str, value: str) -> str:
    """Return the Slack button style an option earns, or "" for the default.

    Resolved in three steps, so that an option can one day state its own intent
    without this having to be rewritten:

    1. an intent stated on the option itself, when it has one;
    2. otherwise the answer value, which is where the asking side already
       writes the action;
    3. otherwise the neutral default.

    Nothing populates step 1 yet -- the shared question builder rebuilds every
    option from a fixed set of keys -- but a field added there needs no change
    here. Both steps go through the same vocabulary, because an intent stated on
    an option is an action name and not a Slack enum: a payload shared by every
    connector must not use one platform's styling words.

    The action is read with ``resolve_permission_action`` rather than compared
    against literals. The vocabulary is the asking side's own, it is stable and
    language independent, and it holds the aliases every builder actually
    emits -- "approve", "reject", and the Chinese labels a localized deployment
    sends when an option has no separate value. Comparing English strings here
    would leave every such deployment unstyled while looking correct in tests.

    Only ``allow_once`` is affirmed. An approval offers three allowing options,
    and marking all three "primary" would both breach Slack's one-per-set
    guidance and erase the distinction the styling exists to draw, so the
    narrowest of them takes it and the two that persist a rule stay neutral.

    An unresolved value yields no style at all. Unrecognized is not rejected:
    the answering side already falls back to a rejection for a value it cannot
    read, but inferring the same here would paint a red button whenever the
    vocabulary drifted, making a rendering gap look like a deliberately
    destructive choice. Model-authored questions hold arbitrary option values
    and land here on every option, which is exactly right -- their choices are
    not permissions and nothing about them is safe or destructive to say.
    """
    # Imported where it is used: the vocabulary module has no dependencies of
    # its own, but its package initializes the rail registry, which this
    # connector otherwise never loads.
    from jiuwenswarm.agents.harness.common.rails.interrupt.permission_options import (
        ALLOW_ONCE,
        REJECT,
        resolve_permission_action,
    )

    action = resolve_permission_action(intent) if intent else None
    if action is None:
        action = resolve_permission_action(value)
    if action == REJECT:
        return _BUTTON_STYLE_DANGER
    if action == ALLOW_ONCE:
        return _BUTTON_STYLE_PRIMARY
    return ""


class SlackDeliveryError(RuntimeError):
    """Raised when a message that had content and a resolved target failed to post.

    ChannelManager wraps every ``send()`` call in ``try/except`` and turns a raised
    exception on a ``cron-push-`` message into a user-visible ``chat.error`` via
    ``_notify_cron_delivery_error``. Returning quietly instead would let a
    scheduled job report success for a delivery that never happened: a revoked
    token, a `channel_not_found` or a 429 producing one log line and nothing
    else.

    Holds the chunk counters so a *partial* send is distinguishable from a total
    one. A long reply is posted as several messages, and failing halfway leaves
    truncated output in the channel; "delivery failed" on its own would wrongly
    suggest nothing was posted. A batch of files is counted the same way; ``unit``
    only renames what the counters are counting in the message shown to the user.
    """

    def __init__(
        self,
        reason: str,
        *,
        channel_id: str = "",
        chunks_sent: int = 0,
        chunks_total: int = 0,
        unit: str = "chunks",
    ) -> None:
        self.reason = reason
        self.channel_id = channel_id
        self.chunks_sent = chunks_sent
        self.chunks_total = chunks_total
        target = f" to {channel_id}" if channel_id else ""
        progress = (
            f" after {chunks_sent}/{chunks_total} {unit}"
            if chunks_sent and chunks_total > 1
            else ""
        )
        super().__init__(f"Slack delivery{target} failed{progress}: {reason}")


class SlackAttachmentError(RuntimeError):
    """Raised when one inbound attachment could not be made available locally.

    Holds a ``reason`` phrased for the user, separate from the technical
    detail that goes to the log. Never escapes the event handler: one unreadable
    attachment must not cost the user the message text posted beside it.
    """

    def __init__(self, detail: str, reason: str) -> None:
        self.reason = reason
        super().__init__(detail)


class SlackStreamError(RuntimeError):
    """Raised when the message a reply would stream into cannot be created.

    Never escapes the connector: streaming is an optimisation, and the reply is
    still delivered in full by the ordinary posting path.
    """


class _SlackMessageSurface:
    """Post one Slack message, then edit it in place as the reply arrives.

    ``failed`` latches on the first terminal error. A placeholder the user
    deleted mid-stream (``message_not_found``) or a revoked ``chat:write`` would
    otherwise be retried once per debounce window for the rest of the turn, and
    the reply must fall back to a fresh message rather than editing something
    that is no longer there.

    ``error`` retains what that terminal error was. Without it ``_close_stream``
    has a silent exit: a stream that became unusable mid-turn returns no handle
    and says nothing about why, so the reply arrives as a fresh message below an
    abandoned preview with nothing in the log tying the two together.

    ``opened_ts``, ``edits`` and ``edit_failures`` are the turn's delivery tally.
    Individual edits stay at DEBUG -- one per debounce window would flood a busy
    channel -- but the counts are reported once when the stream closes, which is
    what makes "the preview never caught up" visible without them.
    """

    def __init__(
        self, channel: "SlackChannel", channel_id: str, thread_ts: str
    ) -> None:
        self._channel = channel
        self._channel_id = channel_id
        self._thread_ts = thread_ts
        self.failed = False
        self.error = ""
        self.opened_ts = ""
        self.edits = 0
        self.edit_failures = 0

    def ready_to_open(self, text: str) -> bool:
        """Whether an opening write can be made from the deltas so far.

        Always, for this surface: a post holds whatever the first delta rendered
        to, and a rendering that came out empty fails the post and abandons the
        stream.
        """
        del text
        return True

    def render(self, text: str) -> str:
        """Render accumulated raw deltas into what this surface writes.

        The session holds raw text and each surface renders it, because the two
        surfaces need different renderings of the same deltas and only the
        surface knows which. This one rewrites the whole message every time, so
        it renders the whole snapshot every time.
        """
        return self._channel._stream_snapshot(text)

    async def open(self, text: str) -> str:
        sent, message_ts, error = await self._channel._post_text(
            channel_id=self._channel_id,
            text=self.render(text),
            thread_ts=self._thread_ts,
        )
        if not sent or not message_ts:
            raise SlackStreamError(error or "chat.postMessage returned no message ts")
        self.opened_ts = message_ts
        # At INFO, and deliberately: this happens once per streamed turn, so it
        # cannot flood, and it is the ts every later edit and the closing rewrite
        # refer to. Without it the streamed message has no identity in the log at
        # all, and a reply that lands on screen truncated cannot be told apart
        # from one that was never rewritten.
        logger.info(
            "[SlackChannel] streaming opened: channel=%s ts=%s preview_chars=%d",
            self._channel_id,
            message_ts,
            len(text),
        )
        return message_ts

    async def write(self, handle: str, text: str, sequence: int) -> None:
        del sequence  # Slack orders edits by arrival; there is no sequence field.
        await self._channel._wait_for_stream_slot(
            self._channel_id, _STREAM_MIN_UPDATE_INTERVAL_SECONDS
        )
        self.edits += 1
        sent, _, error = await self._channel._post_text(
            channel_id=self._channel_id,
            text=self.render(text),
            thread_ts="",
            update_ts=handle,
        )
        if not sent:
            self.failed = True
            self.edit_failures += 1
            self.error = error or "chat.update failed without an error"
            logger.warning("Slack streaming update abandoned: %s", error)

    async def close(self, handle: str, text: str, sequence: int) -> None:
        # The closing edit is the delivery, so it obeys send()'s raise contract
        # and is made there. Nothing to do here.
        del handle, text, sequence


class _SlackStreamingSurface:
    """Stream one Slack message through chat.startStream / append / stopStream.

    Same three lifecycle methods as ``_SlackMessageSurface`` and the same
    latching, so the session driving it cannot tell them apart -- but the
    protocol underneath is the opposite shape. An edit replaces the message with
    the whole answer so far; an append adds only what is new and can never be
    taken back. Three consequences run through everything below.

    **Only a stable prefix may be sent.** ``write`` is handed the same
    accumulated snapshot the edit surface gets, and has to find the part of it
    that no later delta can change. The normaliser works a line at a time, so a
    complete line renders the same however much follows it -- but an incomplete
    one does not, because ``**bold**`` becomes ``*bold*`` only once the closing
    marker arrives. Structure is worse: a fence is source text until it closes,
    and a row of pipes is a paragraph until the delimiter row under it arrives.
    ``_sendable`` is where that judgement lives, and what it holds back the
    close delivers.

    **The close is a delivery.** ``chat.stopStream`` is what takes the message
    out of streaming state and delivers the reply's tail, so unlike the edit
    surface this one has real work to do at the end. ``send()`` still owns the
    raise: it calls ``stop_stream`` and decides what a failure means, because it
    is the only caller that knows whether the answer is on screen.

    **A reader can stop it.** ``stopped_by_user`` has no analogue on the edit
    path, where a reader cannot refuse an edit. It is latched like any other
    terminal error, and what has not been appended yet is posted as a fresh
    message rather than being lost.

    **It speaks a different dialect.** ``markdown_text`` is real Markdown and
    Slack converts it server-side, measured: ``**bold**`` arrives and ``*bold*``
    is stored. Everything else this connector writes -- ``chat.postMessage``,
    ``chat.update``, a ``section`` block's text -- takes mrkdwn, which
    ``_normalize_slack_mrkdwn`` produces. The two are not compatible and the
    same string means different things in each: ``*bold*`` is bold in mrkdwn and
    *emphasis* in Markdown, and a ``\u2022`` that the normaliser makes out of a
    dash opens a ``rich_text_list`` that swallows every line after it into one
    item. Both were measured and both are defects; neither is visible until
    someone reads the message.

    So this surface sends the deltas **as they were written**, and the
    conversion happens at the boundary where the ordinary API begins:
    ``_recover_streamed_reply`` and ``_rewrite_streamed_message`` normalise
    before they post or edit, and a blocks chunk is built from normalised text
    because a block is Block Kit rather than Markdown. What Slack's own
    ``<url|label>`` token does inside ``markdown_text`` is the one crossover
    that is safe -- it is honoured, measured -- which is why a mention or a
    channel link in the agent's own text survives untouched.

    **It expires, and long turns outlive it.** A stream lasts about five minutes
    from ``chat.startStream``, measured, and appending does not extend it: two
    probes six times apart in cadence died within seven seconds of the same
    elapsed time. Turns here run far longer than that, so a stream ending
    part-way through one is the ordinary case rather than an edge, and it ends
    with ``message_not_in_streaming_state``.

    What happens then is that the preview stops and the message keeps what it
    already has, until the turn finishes and ``_recover_streamed_reply`` writes
    the rest. It does *not* carry on previewing with ``chat.update``, which is
    the obvious alternative and is wrong for one measurable reason: an edit
    takes 4,000 characters and a stream that ran for five minutes has very
    likely put more than that on screen already. Continuing with edits would
    mean cutting the answer down to what an edit accepts -- taking away text the
    reader has already read -- to keep a preview moving.
    """

    def __init__(
        self,
        channel: "SlackChannel",
        channel_id: str,
        thread_ts: str,
        *,
        recipient_user_id: str = "",
        recipient_team_id: str = "",
    ) -> None:
        self._channel = channel
        self._channel_id = channel_id
        self._thread_ts = thread_ts
        self._recipient_user_id = recipient_user_id
        self._recipient_team_id = recipient_team_id
        self.failed = False
        self.error = ""
        self.opened_ts = ""
        self.edits = 0
        self.edit_failures = 0
        # Set when the reader pressed stop. Separate from ``failed`` because it
        # is the one terminal error that is somebody's decision rather than a
        # fault, and it is worth saying so in the log.
        self.stopped_by_user = False
        # Exactly what has been appended, in the form it was appended in. The
        # close works out the reply's tail by matching the finished answer
        # against this, so it is advanced only by a call that landed.
        self.sent_text = ""

    def ready_to_open(self, text: str) -> bool:
        """Whether the deltas so far contain anything a stream may open with.

        A stream is opened with content rather than empty and filled in later:
        what an empty ``chat.startStream`` does is not documented, and finding
        out costs a turn's reply. So the first delta that renders to a stable
        prefix opens the stream and the ones before it only accumulate. A reply
        short enough to arrive as a single unterminated line therefore never
        opens one at all and is posted whole, which is the right outcome for a
        reply that would have finished before the first debounce window elapsed.
        """
        return bool(self._sendable(text))

    def _sendable(self, text: str) -> str:
        """The rendered prefix of *text* that no later delta can change.

        Four cuts, in order, each of which only ever moves forward as *text*
        grows -- which is what makes the result usable as an append offset:

        1. leading whitespace, once, so the streamed message agrees with the
           stripped answer ``send()`` delivers at the close;
        2. the last complete line. Slack parses each append on its own, so a
           construct cut in half arrives as two halves and neither is what was
           written -- ``**bo`` is literal text and ``ld**`` after it does not
           rescue it. The line is the smallest unit no continuation reaches
           inside;
        3. the thread-details marker, because only the reply's first piece is
           written into this message and the rest are posted under it;
        4. the first fence or table, because a block cannot be un-sent and text
           that turns out to be one would be on screen twice.

        No conversion. What comes out is the Markdown the model wrote, which is
        what ``markdown_text`` reads -- see the class docstring for why running
        it through the mrkdwn normaliser first was a defect rather than a
        precaution.

        The Block Kit markers are removed as they pass rather than being
        stripped with the surrounding blank space the way
        ``_extract_block_request`` does it: collapsing whitespace would rewrite
        text already sent. A marker on a line of its own takes the line with it.
        """
        stable = text.lstrip()
        cut = stable.rfind("\n")
        stable = stable[: cut + 1] if cut >= 0 else ""
        if not stable:
            return ""
        marker_at = stable.find(_SLACK_THREAD_DETAILS_MARKER)
        if marker_at >= 0:
            stable = stable[:marker_at]
        stable = self._strip_block_markers(stable)
        return stable[
            : slack_blocks.streamable_prose_prefix(
                stable,
                allowed_block_types=self._channel._blockkit_allowed_block_types(),
                allow_interactive=self._channel._blockkit_allow_interactive(),
            )
        ]

    @staticmethod
    def _strip_block_markers(text: str) -> str:
        if _SLACK_BLOCKS_MARKER not in text and _SLACK_BLOCKS_OFF_MARKER not in text:
            return text
        kept: list[str] = []
        for line in text.splitlines(keepends=True):
            body = line.rstrip("\r\n")
            if (
                _SLACK_BLOCKS_MARKER not in body
                and _SLACK_BLOCKS_OFF_MARKER not in body
            ):
                kept.append(line)
                continue
            cleaned = body.replace(_SLACK_BLOCKS_MARKER, "").replace(
                _SLACK_BLOCKS_OFF_MARKER, ""
            )
            if cleaned.strip():
                kept.append(cleaned + line[len(body) :])
        return "".join(kept)

    async def open(self, text: str) -> str:
        """Create the message, in the mode everything afterwards depends on.

        Opened with ``chunks`` and never with ``markdown_text``, even though the
        opening is nothing but markdown text and the two calls are otherwise
        identical. A stream is locked to the shape of its opening call for the
        whole of its life: one opened in text mode refuses every chunk it is
        later sent -- markdown, plan, task or blocks alike -- with
        ``streaming_mode_mismatch``. Since the close sends chunks whenever the
        reply ends in anything renderable, opening the cheaper-looking way
        breaks the close for exactly the replies that have the most to show.
        Measured, and documented nowhere.
        """
        opening = self._sendable(text)
        response = await self._channel._call_stream_method(
            "chat_startStream",
            channel=self._channel_id,
            thread_ts=self._thread_ts,
            chunks=[{"type": "markdown_text", "text": opening}],
            recipient_user_id=self._recipient_user_id or None,
            recipient_team_id=self._recipient_team_id or None,
        )
        message_ts = ""
        if response is not None:
            try:
                message_ts = str(response.get("ts") or "").strip()
            except (AttributeError, TypeError):
                message_ts = ""
        if not message_ts:
            raise SlackStreamError("chat.startStream returned no message ts")
        self.opened_ts = message_ts
        self.sent_text = opening
        # At INFO for the same reason the edit surface says it once: this is the
        # ts every append and the close refer to, and without it a streamed
        # reply has no identity in the log at all. The recipient is named
        # because a channel stream is refused without one.
        logger.info(
            "[SlackChannel] streaming opened: channel=%s ts=%s mode=stream "
            "recipient=%s opening_chars=%d",
            self._channel_id,
            message_ts,
            self._recipient_user_id or "-",
            len(opening),
        )
        return message_ts

    async def write(self, handle: str, text: str, sequence: int) -> None:
        del sequence  # Appends land in the order they are made.
        await self.append(handle, self._sendable(text))

    async def append(self, handle: str, sendable: str) -> None:
        """Append whatever of *sendable* has not been appended yet.

        Best effort, like every intermediate write: a refused append costs a
        moment of staleness, and the close appends the same text again because
        ``sent_text`` was not advanced past a call that did not land.
        """
        if self.failed:
            return
        if not sendable.startswith(self.sent_text):
            # Not reachable through ``_sendable``, which only ever extends.
            # Latched rather than papered over: appending text computed against
            # a prefix that has changed underneath it is how a reply gets
            # corrupted, and posting the answer fresh is a visible, correct
            # outcome where that is not.
            self._latch("the streamed prefix stopped matching what was sent")
            return
        pending = sendable[len(self.sent_text) :]
        if not pending:
            return
        for piece in self._split_appends(pending):
            await self._channel._wait_for_stream_slot(
                self._channel_id, _STREAM_APPEND_MIN_INTERVAL_SECONDS
            )
            self.edits += 1
            try:
                await self._channel._call_stream_method(
                    "chat_appendStream",
                    channel=self._channel_id,
                    ts=handle,
                    chunks=[{"type": "markdown_text", "text": piece}],
                )
            except Exception as exc:  # noqa: BLE001
                self.edit_failures += 1
                self._latch(slack_failure_reason(exc))
                return
            self.sent_text += piece

    async def close(self, handle: str, text: str, sequence: int) -> None:
        """Take the message out of streaming state.

        The mirror image of the edit surface's ``close``, which is a no-op
        because ``send()`` makes the closing rewrite itself. ``chat.stopStream``
        is not optional -- a message left in streaming state stays that way --
        so the closing call belongs here. What has not moved is who is
        accountable for it: ``send()`` calls ``stop_stream`` directly and raises
        on what it returns, and this method exists so that a session finalising
        the surface without ``send()`` still leaves nothing open.
        """
        del sequence
        chunks = self._markdown_chunks(text) if text else []
        await self.stop_stream(handle, chunks=chunks)

    async def stop_stream(
        self,
        handle: str,
        *,
        chunks: list[dict[str, Any]] | None = None,
        plain_chunks: list[dict[str, Any]] | None = None,
    ) -> bool:
        """Finish the streamed message. ``True`` when it left streaming state.

        Never raises: the caller has to tell a stop that failed with the whole
        answer already on screen from one that failed with the reply's tail
        still undelivered, and only it knows which. Latches like an append, so a
        second attempt cannot be made against a message that has said it will
        take no more.

        ``plain_chunks`` is the same content with no rendering, used once if
        Slack refuses the one it was given. It is the caller's to supply because
        a blocks chunk holds no text of its own to fall back to -- unlike a
        ``chat.postMessage`` payload, where the text and the blocks are two
        renderings of one thing sitting side by side.
        """
        payload: dict[str, Any] = {"channel": self._channel_id, "ts": handle}
        if chunks:
            payload["chunks"] = chunks
        try:
            await self._channel._call_stream_method("chat_stopStream", **payload)
        except Exception as exc:  # noqa: BLE001
            reason = slack_failure_reason(exc)
            rendered = any(chunk.get("type") == "blocks" for chunk in chunks or [])
            if rendered and any(
                code in reason for code in _STREAM_BLOCK_REJECTION_ERRORS
            ):
                # The same bargain ``_post_text`` strikes for a refused
                # rendering: the content would have delivered on its own, so a
                # rendering Slack will not draw costs the formatting rather than
                # the reply. Loud, because it means the local validation has a
                # blind spot.
                logger.warning(
                    "Slack rejected the Block Kit rendering at the close (%s); "
                    "finishing this stream as plain text",
                    reason,
                )
                return await self.stop_stream(handle, chunks=plain_chunks)
            self._latch(reason)
            return False
        for chunk in chunks or []:
            if chunk.get("type") == "markdown_text":
                self.sent_text += str(chunk.get("text") or "")
        return True

    def _latch(self, error: str) -> None:
        self.failed = True
        self.error = error
        if _STREAM_STOPPED_BY_USER in error:
            self.stopped_by_user = True
            logger.info(
                "[SlackChannel] streaming stopped by the reader: channel=%s ts=%s",
                self._channel_id,
                self.opened_ts or "-",
            )
            return
        if any(code in error for code in _STREAM_RECIPIENT_ERRORS):
            # Not a condition the reader or the network can produce: the ids are
            # read off the inbound event and remembered before a reply can be
            # streamed, so this is the plumbing failing rather than Slack.
            logger.error(
                "[SlackChannel] streaming refused for want of a recipient: "
                "channel=%s ts=%s user=%s team=%s error=%s",
                self._channel_id,
                self.opened_ts or "-",
                self._recipient_user_id or "-",
                self._recipient_team_id or "-",
                error,
            )
            return
        if any(code in error for code in _STREAM_UNUSABLE_ERRORS):
            logger.warning(
                "Slack streaming will take no more (%s); what is left of the "
                "reply is posted separately",
                error,
            )
            return
        logger.warning("Slack streaming append abandoned: %s", error)

    def resume(self) -> str:
        """Clear the latch if the failure it holds was worth retrying.

        Returns the error it cleared, or ``""`` if it declined to clear one.

        The one way back out of ``failed``, and it takes evidence: chat.update
        refusing with ``streaming_state_conflict`` says the message is still the
        streaming API's, which means the stream is alive whatever the close
        thought. Without this the latch would keep a recoverable stream shut on
        the strength of one bad call.

        But "the stream is alive" is only half of what a retry needs, and the
        two halves can be answered by Slack in the same breath and contradict
        each other. A close refused with ``streaming_mode_mismatch`` and an edit
        refused with ``streaming_state_conflict`` are both true at once: the
        stream is open, *and* the payload will never be accepted by it. Believing
        the second and retrying the first is how one failed close became two.
        So a payload rejection is not resumed from -- nothing about the state of
        the stream makes the same chunks acceptable to it.
        """
        if not self.failed:
            return ""
        if any(code in self.error for code in _STREAM_PAYLOAD_ERRORS):
            return ""
        cleared, self.error, self.failed = self.error, "", False
        return cleared

    @staticmethod
    def _split_appends(text: str) -> list[str]:
        """Cut one append into calls Slack will accept.

        Reuses the reply splitter's own boundary preference so a piece is not
        cut through a link or a mention, and only ever fires for a single
        stretch of new text longer than a whole append -- which in practice
        means the reply's tail at the close, not a debounce window.
        """
        if len(text) <= _MAX_SLACK_APPEND_TEXT_LENGTH:
            return [text] if text else []
        pieces: list[str] = []
        remaining = text
        while len(remaining) > _MAX_SLACK_APPEND_TEXT_LENGTH:
            split_at = SlackChannel._preferred_split_index(
                remaining, _MAX_SLACK_APPEND_TEXT_LENGTH
            )
            pieces.append(remaining[:split_at])
            remaining = remaining[split_at:]
        if remaining:
            pieces.append(remaining)
        return pieces

    @classmethod
    def _markdown_chunks(cls, text: str) -> list[dict[str, Any]]:
        return [
            {"type": "markdown_text", "text": piece}
            for piece in cls._split_appends(text)
        ]


@dataclass
class _SlackStream:
    """One reply being rendered into one Slack message."""

    surface: _SlackMessageSurface | _SlackStreamingSurface
    # Where the reply is going, kept beside the surface rather than only inside
    # it: a stream that cannot be opened is answered by building a different
    # surface for the same destination, and the destination outlives the choice.
    channel_id: str = ""
    thread_ts: str = ""
    session: StreamingSession | None = None
    text: str = ""
    # Set when the message could not be created at all, as opposed to
    # surface.failed, which means it was created and later became unusable.
    failed: bool = False
    touched_at: float = field(default_factory=time.monotonic)


@dataclass
class _SlackTurnInitiator:
    """Who asked for the turn a session is currently running.

    Recorded at dispatch because that is the only point at which the person and
    the turn are both known here. The reply comes back holding the session and
    the channel and no user at all -- the same gap ``_stream_recipients`` exists
    to cover -- and nothing downstream brings it back either: a ``chat.send``
    reaching the gateway cancels whatever stream that session already had, with
    no reference to who started it.

    Deliberately not folded into ``_stream_recipients``, which is the nearest
    existing map and the wrong one. That is written wherever a turn is
    dispatched, as this one is, but it is never cleared, so it answers the
    question for a session whose turn ended ten minutes ago as readily as for
    one still running. Nor onto ``_SlackActivityRecord``, which is genuinely per
    turn but exists only while ``activity_card`` is on and only for a turn that
    did tracked work: a fact about who may stop a turn must not be contingent on
    a display setting.

    ``request_id`` is the turn's own id on the wire, held so that the terminal
    event of an *older* turn cannot clear the entry of the one that replaced it
    -- an ordinary message from a second person in the same thread cancels the
    first person's turn and starts their own, and the two terminal events then
    arrive in an order nothing guarantees.

    ``is_dm`` says which surface the turn came from, because the initiator is
    worth different amounts in the two. A DM session is keyed on the user, so
    the initiator is the same person for the whole life of the session and the
    entry is close to redundant; a channel session is shared -- keyed on the
    root thread, or on the channel where ``delivery.session`` says so -- so the
    initiator changes per turn and a second person's ordinary message cancels
    the first person's work. A consumer that cannot tell the two apart writes
    one rule for both.

    Recorded rather than recovered from the session id, which would mean a third
    hand-maintained copy of the positional parse ``parse_slack_cron_session``
    already warns about being a second one -- and a copy that could not answer
    this anyway: that parse returns ``""`` for a DM's user id and for a cron
    discriminator alike, so the string does not distinguish the two surfaces it
    was built from. One recorded bool is cheaper and cannot disagree with
    itself.
    """

    user_id: str
    request_id: str
    # True only for a Slack ``im``. An mpim -- a group DM -- is not one: it
    # arrives with channel_type "mpim", takes the channel path, and gets the
    # shared thread-keyed session, so it is the second regime and not a third.
    is_dm: bool
    # Slack's own word for the kind of conversation the turn was started in, as
    # it arrived on the event. Held for the same reason ``is_dm`` is and with
    # one more of its own: a scope may be addressed on the kind, and a turn is
    # settled again when it resumes after a click, by which point the event is
    # gone and the click carries the conversation it was pressed in and nothing
    # about what kind of conversation that is.
    #
    # Not the same fact as ``is_dm``, and neither derives from the other. That
    # bool is true for an ``im`` alone and answers a question about session
    # keying; this is one of four words and answers a question about matching,
    # in which a private channel and a group DM are two different rooms that
    # ``is_dm`` calls the same thing.
    #
    # ``""`` is a real state and is the fail-closed one: a dispatch whose event
    # named no kind records none, and a rule naming a kind then does not reach
    # the resumed turn either. Required rather than defaulted, for the reason
    # ``is_dm`` is.
    chat_type: str
    # Where the message that started this turn was posted, held so that the
    # turn's ending can be written back onto it. The terminal event knows the
    # session and the request and nothing else, and by the time it arrives the
    # event that these were read off is long gone.
    #
    # Held here rather than on ``_SlackActivityRecord``, which is the other
    # per-turn record and the wrong one for the same reason the initiator is
    # not kept there: that record exists only while ``activity_card`` is on and
    # only for a turn that did tracked work, and whether a turn ended is not
    # contingent on a display setting.
    #
    # Empty is a real state, not a missing one. A turn dispatched from a path
    # with no message behind it -- a scheduled push, a resume whose question
    # predates the fields -- has nothing to mark, and the mark is skipped
    # rather than guessed at.
    channel_id: str = ""
    message_ts: str = ""
    # Where the thinking status this turn's acknowledgement opened is to be
    # cleared -- the thread the reply will land in, which is ``reply_thread_ts``
    # at dispatch time and not always ``message_ts`` above. The two coincide
    # for a message that opens a fresh thread, where the root is the message's
    # own timestamp, but not for one posted as a reply into a thread that
    # already existed: there ``message_ts`` is the reply's own stamp, kept for
    # the reaction, while the status was opened on the thread's root instead.
    # Empty for the same reason ``channel_id``/``message_ts`` can be: a DM
    # answered at top level has no thread to open a status on, so it gets no
    # coordinates to clear one from either.
    thread_ts: str = ""
    # What the request this turn was dispatched under already said about its
    # own reply, held because a turn resumed after an interrupt has no way back
    # to it. The resume is a fresh request built from a button click, and a
    # click names the conversation it was pressed in and nothing about the
    # message that started the work: not whether that message addressed the
    # bot, not whether the turn was woken by an event with nothing to anchor
    # to, not which trigger produced it.
    #
    # Recorded rather than settled a second time at resume. The rule behind it
    # is the room's ``delivery.reply``, read for the kind of conversation the
    # triggering message arrived in -- and a click carries neither the room's
    # kind nor a guarantee that the config still says what it said when the
    # turn was dispatched, since the WebUI rewrites config at runtime and a
    # paused turn can sit for minutes. The answer the turn was actually run
    # under is the dispatch's, so the dispatch's answer is what is kept.
    #
    # The matcher fact, never the offer. Whether the dispatch appended the
    # fragment is a different question, withheld on a message that addressed
    # the bot; carrying that answer here would disarm the matcher on exactly
    # the resumes this field exists for.
    silence_is_honoured: bool = False
    # Whether the message that started this turn opened with a mention of the
    # bot. Diagnostic, and held for the same reason the field above is: it is
    # a fact about a message the click cannot see. A resumed turn that stays
    # silent is one of the cases the withheld-reply line is read for, and
    # without this it would be logged as unaddressed whatever the message said.
    addressed: bool = False
    # Where this turn's reply belongs when an event woke it with nothing to
    # anchor to: the room's top level, not a thread. Held for the same reason
    # and against a worse failure than a lost silence -- the resume routes by
    # the clicked card's container, so a question that happened to be posted
    # inside a thread would land a turn about the room in that thread.
    post_as_root: bool = False
    # Which trigger woke this turn. Diagnostic on the way in and diagnostic
    # after a resume, and without it every resumed turn reads as having no
    # trigger at all -- a value no dispatch path ever stamps, so a reader
    # cannot tell it from a field that went missing.
    trigger: str = ""
    started_at: float = field(default_factory=time.monotonic)


@dataclass
class _SlackQueuedMessage:
    """One message held under ``mid_turn: queue`` until the session goes idle.

    The whole built request is held rather than the text it came from, because
    everything between the two costs an API call or cannot be redone at all: the
    attachments have been downloaded, the standing prompt has been appended, the
    trigger label is on it and the session id is settled. Re-deriving any of it
    at drain time would be a second copy of the dispatch path, and a second copy
    that ran against a config the operator may have edited in between.

    The ids beside it are what the *drain* needs and the request does not
    bring back. ``user_id`` and ``is_dm`` open the initiator entry for the turn
    the drain starts -- the drained message is genuinely its author's own turn,
    with its own id and its own initiator, which is what ``queue`` buys over a
    follow-up round. ``user_id`` and ``team_id`` together are the pair a stream
    into that turn's reply is addressed to, recorded at the drain for the same
    reason the initiator is: until then the session's entries belong to the turn
    this message is waiting behind. ``channel_id`` and ``message_ts`` are where
    the reaction that says "queued" was put, so the drain can take it off again.

    ``thread_ts`` is a fourth coordinate for the same reason it is on
    ``_SlackTurnInitiator``: where the thinking status is set again once the
    drained message is dispatched -- replacing ``queued_status``, which is
    what was showing while this message waited -- kept here because a message
    posted into a thread that already existed opens one at its root rather
    than at ``message_ts``, and holding it is what lets the turn opened at
    drain time clear the right one when it ends.

    ``queued_at`` is monotonic and is what the age bound is measured from, so a
    queue behind a turn whose terminal event never arrives cannot outlive the
    hour.
    """

    request: Message
    session_id: str
    user_id: str
    is_dm: bool
    # The kind of conversation the queued message arrived in, carried for the
    # reason the built request is carried: the drain opens this message's own
    # turn, and re-deriving the kind then would mean re-reading an event that
    # is long gone. Required, as ``is_dm`` beside it is.
    chat_type: str
    # Empty for a workspace the event did not name, which is what the stream
    # recipient already treats as "unknown" rather than as a team.
    team_id: str = ""
    channel_id: str = ""
    message_ts: str = ""
    thread_ts: str = ""
    queued_at: float = field(default_factory=time.monotonic)


@dataclass
class _SlackPendingQuestion:
    """One posted question, kept until a button on it is clicked.

    The button that holds the click has room for an identifier and little
    else, so the parts of the answer that are unbounded in length -- the
    question text, which keys the answer the agent receives -- are held here and
    looked up by ``request_id`` instead of being encoded into every button.

    Holding it also makes a second click detectable. The posted message is
    rewritten without its buttons as soon as one is pressed, which is what stops
    a second press in practice; this is what stops two presses that raced, or a
    press on a copy of the message that was never rewritten, from answering the
    same question twice.

    An entry outlives the answer it was waiting for when the question is
    withdrawn instead: ``withdrawn_at`` marks it, and the entry is kept rather
    than dropped so a click that arrives afterwards can be recognised as stale
    and reported instead of being read as an answer.
    """

    request_id: str
    session_id: str
    source: str
    question: str
    # Indexed as the buttons are: the click reports which button it was, and
    # these are the label shown on it and the value the agent is answered with.
    labels: list[str]
    values: list[str]
    # The inputs the question was posted with, empty for a question answered by
    # pressing one of its options. Kept here for the same reason the values are:
    # a submit holds the elements' contents but not what they were declared to
    # mean, and reading them needs the declaration that rendered them.
    inputs: list[slack_inputs.QuestionInput] = field(default_factory=list)
    # Where the question was posted, which is what lets the message be rewritten
    # without a click to take the coordinates from.
    channel_id: str = ""
    message_ts: str = ""
    # The text the message was posted with, kept so a rewrite that drops the
    # blocks still shows what was asked.
    notification_text: str = ""
    # The id of the request whose turn asked this, as distinct from
    # ``request_id``, which identifies the question. Answering an interrupt
    # resumes that turn under a new request id, and this is the only place the
    # link between the two is known: it is what lets the resumed turn find the
    # status card the request already has instead of posting a second one.
    turn_request_id: str = ""
    # Who started the turn this question interrupted, kept here for the same
    # reason ``turn_request_id`` is: answering resumes that turn as a fresh
    # request, and the pause has already dropped the session's live entry by the
    # time the click arrives. Without this the resumed turn would be attributed
    # to whoever pressed the button, which for a permission prompt is routinely
    # not the person who asked for the work.
    initiator_user_id: str = ""
    # Which surface that initiator's turn came from, kept for the same
    # reason and taken from the same entry. ``None`` means no entry was on
    # record to read it off, and is kept distinct from ``False``: resuming a DM
    # turn labelled as a channel one is the confusion recording the surface
    # exists to remove, so the resume falls back to the conversation the click
    # came from rather than to a bare default.
    initiator_is_dm: bool | None = None
    # The kind of conversation that initiator's turn was started in, off the
    # same entry, so a scope addressed on the kind settles a resumed turn the
    # way it settled the first dispatch.
    #
    # ``""`` rather than ``None`` for the absent case, and no three-state here.
    # ``initiator_is_dm`` needs one because both of its values are claims and
    # neither may be guessed; this axis already has a word for "no kind stated"
    # and the matcher already fails closed on it, so there is nothing a third
    # state would say. With no entry on record the resumed turn is matched by no
    # rule naming a kind, which is what the axis promises everywhere else.
    initiator_chat_type: str = ""
    # Where that initiator's message was posted, kept for the same reason and
    # off the same entry. The resumed turn is the interrupted one continuing,
    # so its ending belongs on the message that started the work rather than on
    # the question, and this is the last point at which the two are linked: the
    # pause closes the entry these were read from.
    initiator_channel_id: str = ""
    initiator_message_ts: str = ""
    # Where that initiator's thinking status was opened, kept for the same
    # reason and off the same entry: the resumed turn's ending clears it the
    # same way it marks ``initiator_message_ts`` above, and by the time a
    # click arrives the record it would otherwise be read from is gone.
    initiator_thread_ts: str = ""
    # What that initiator's request said about its own reply, off the same
    # entry and kept for the sharpest version of the reason the fields above
    # are kept: the resume is built from a click, and a click is silent about
    # every one of them.
    #
    # ``initiator_silence_is_honoured`` is the matcher half of the silence
    # contract: it says that a turn answering with the token alone has that
    # answer read as a decision to say nothing. The outbound half consults the
    # matcher only where the request says so. Lose it across a pause and a turn
    # that writes the token has the bare word posted into the room as ordinary
    # text -- which is what happened, twice, before these were held.
    #
    # The matcher fact, never the offer, and the two are decided apart on the
    # way in. The offer is withheld from a message that addressed the bot;
    # carrying that answer here instead would disarm the matcher on exactly the
    # resumes this field exists for. Taken from the dispatch rather than settled
    # again, for the reason the initiator record states.
    initiator_silence_is_honoured: bool = False
    # Whether that initiator's message opened with a mention of the bot, off
    # the same entry. It decides nothing about the reply and one thing about
    # the log: a resumed turn that withholds its reply is recorded as the
    # addressed case or the unaddressed one, and only this says which.
    initiator_addressed: bool = False
    # Where that initiator's reply belongs. An event turn with nothing to
    # anchor to is routed to the room's top level, and the resume would
    # otherwise route by the clicked card's container instead -- putting a turn
    # about the room inside whichever thread the question happened to be
    # posted in.
    initiator_post_as_root: bool = False
    # Which trigger woke that initiator's turn, diagnostic on both sides of the
    # pause. Empty means no entry was on record, and is left unstamped rather
    # than guessed: no dispatch stamps an empty trigger, so inventing one here
    # would make a resumed turn unreadable in exactly the logs it is traced in.
    initiator_trigger: str = ""
    # Set when the question stopped being answerable, and never cleared: it is
    # both the flag a stale click is recognised by and the instant its staleness
    # is measured from.
    withdrawn_at: float | None = None
    touched_at: float = field(default_factory=time.monotonic)


@dataclass
class _SlackCronRecord:
    """One scheduled run's message, kept so its result can rewrite it.

    The scheduler cannot hold this. Its push ends at an ``asyncio.Queue``, so
    it has returned before a channel is even chosen, and the channel is
    resolved here -- which is also why the key holds one: the scheduler does
    not know where the message landed.
    """

    run_id: str
    channel_id: str
    message_ts: str
    touched_at: float = field(default_factory=time.monotonic)


@dataclass
class _SlackCronPush:
    """What one cron push means for the record it belongs to."""

    run_id: str
    channel_id: str
    is_placeholder: bool
    # None when the outcome cannot be stated: a terminal push holding a status
    # this connector does not recognise. The message is still delivered, and
    # still supersedes the placeholder; only the chip is withheld, because an
    # invented one would be a claim about a run nobody checked.
    card: dict[str, Any] | None
    # The placeholder's ts, or "" when there is nothing to rewrite -- no
    # placeholder was sent, the gateway restarted, or the entry aged out. All
    # three post a new message, which is the path a short job takes every day.
    anchor_ts: str = ""
    # Whether the message body still has to be rendered beneath the card.
    # False when the card already says everything the body said.
    body_below_card: bool = False


@dataclass
class _SlackActivityRun:
    """One dispatched subagent or tool call, identified by call or roster id.

    ``started_at`` is the whole of the duration machinery. No payload on the
    wire holds one -- neither the call nor the result says how long anything
    took -- so a call is clocked when it arrives, which is what every other
    record in this connector already does.
    """

    call_id: str
    # ``subagent_type`` for a dispatch, the tool's name for a tool call, both
    # reduced to an identifier. Never a task description or a result.
    label: str
    state: str = _RUN_RUNNING
    started_at: float = field(default_factory=time.monotonic)


@dataclass
class _SlackCompressionRun:
    """One context processor's pass over the window, keyed by its operation id.

    The operation id is the runtime's own, minted per processor per phase, so
    two processors working the same window are two runs here and the second
    never overwrites the first. That separation is the point: the turn this was
    built from ran an offloader that saved nothing and a compressor that saved
    ninety-six per cent, and a section that summed them would have reported one
    average that describes neither.

    ``before_*`` is the size of the thing being compacted and is read off the
    ``started`` event; ``saved_*`` is what the pass actually recovered and
    arrives with the terminal one. Both are the runtime's own figures. Nothing
    here holds the summary the compressor wrote -- ``compact_summary`` is the
    model's prose about a conversation and is no more publishable than a line of
    reasoning is.

    ``duration_ms`` is the one duration in this connector that is read off the
    wire rather than clocked here, because for a finished pass the runtime
    measured it around the work itself and this process only saw the event
    arrive. ``started_at`` still exists for the pass that has not finished: the
    terminal event is what carries the figure, and until it lands the elapsed
    time is the whole of what the card has to say.
    """

    operation_id: str
    # The processor's class name, reduced to an identifier like every other
    # label on this card. It is runtime-supplied rather than model-supplied, but
    # it is reduced on the same rule so that one kind of text on a card cannot
    # become two.
    label: str
    state: str = _COMPRESSION_STARTED
    started_at: float = field(default_factory=time.monotonic)
    before_messages: int = 0
    before_tokens: int = 0
    before_percent: int | None = None
    saved_messages: int = 0
    saved_tokens: int = 0
    saved_percent: float = 0.0
    duration_ms: int | None = None
    # Whether this pass ended because a stop reached it, which the runtime says
    # by failing it with the error ``cancelled``. Kept as a flag rather than as
    # the error text, because the text is the only thing the card would be
    # quoting and the flag is the only thing it needs.
    cancelled: bool = False

    def elapsed_seconds(self) -> float:
        """How long this pass has taken, the runtime's figure where there is one."""
        if self.duration_ms is not None:
            return max(0.0, self.duration_ms / 1000.0)
        return max(0.0, time.monotonic() - self.started_at)

    def saved_anything(self) -> bool:
        """Whether the pass recovered anything at all."""
        return bool(self.saved_messages or self.saved_tokens)


@dataclass
class _SlackActivityRecord:
    """One user request's activity, and the message standing for it.

    Keyed on the request id rather than on any one piece of work: a turn that
    spawns twenty subagents and calls fifty tools is one thing happening, and a
    message apiece would be a wall where one card is a status line.

    The record outlives the work. A model that
    works through its subagents one at a time -- the ordinary case, not the
    parallel burst -- has nothing running between every pair of them, and a
    record that closed on that produced one card per subagent for what was a
    single turn. So the record stays until the turn does, and stays on past that
    too whenever a card was posted for it, so that a turn resumed after an
    interrupt folds into the card its own request already has instead of posting
    a second one beside it.

    ``message_ts`` is empty until the card is actually posted, which is the
    whole point of the delay: a turn that finishes before it elapses leaves
    nothing behind at all.
    """

    request_id: str
    channel_id: str
    thread_ts: str
    # The session the turn is running on, which is what a stop has to name: the
    # gateway cancels on ``(channel_id, session_id)`` and has no notion of a
    # request to cancel. Kept here rather than looked up, because the card is
    # the only per-turn surface Slack has and the button on it must be able to
    # say which session it means without consulting anything that may since have
    # aged out. Empty when the first event of the turn held none, in which
    # case no button is rendered at all -- a control that cannot name its target
    # is worse than no control.
    session_id: str = ""
    # Set when a stop has been dispatched for this turn, so that the rewrite
    # which follows takes the button away. Not the same as ``closed``: the turn
    # is still running here, and may go on running for as long as the cancel
    # takes to reach it. What this prevents is the second click -- the cancel is
    # addressed to a session rather than to a turn, so a click landing after the
    # turn it meant has ended would reach whatever the session is running now.
    #
    # Sticky, unlike ``closed``, which a fresh tool call clears. A request that
    # resumes after its stop comes back without a button rather than with one
    # whose claim has been quietly returned: the alternative is a control that
    # reappears in the window between a cancel being sent and the turn noticing
    # it.
    stop_requested: bool = False
    subagent_runs: dict[str, _SlackActivityRun] = field(default_factory=dict)
    # Only the recent tool calls are held as runs; older ones survive as counts
    # in ``retired_tools``, keyed by label and then by state. A long turn calls
    # hundreds of tools and the card only ever shows how many of each there
    # were.
    tool_runs: dict[str, _SlackActivityRun] = field(default_factory=dict)
    retired_tools: dict[str, dict[str, int]] = field(default_factory=dict)
    # Counts per state from the last ``todo.updated`` snapshot. Replaced whole,
    # never accumulated: the event holds the entire list every time.
    todo_counts: dict[str, int] = field(default_factory=dict)
    # The turn's context compaction, one entry per processor pass, keyed by the
    # runtime's own operation id. Ordered by arrival and rendered that way: the
    # runtime runs its processors in a fixed order and which one ran first is
    # the fact an operator reads off this section, so it is not sorted into
    # something else.
    compression_runs: dict[str, _SlackCompressionRun] = field(default_factory=dict)
    # The turn's reasoning, held as three numbers and nothing else. No chunk of
    # reasoning text is stored here, and none is stored anywhere else either:
    # what the card reports is that the model was thinking and for how long.
    #
    # ``reasoning_since`` is the start of the stretch currently open, or
    # ``None``, which is also how the section knows whether to render as
    # running. ``reasoning_elapsed`` sums every closed stretch, so the number on
    # the card only ever grows: a model that thinks, calls a tool and thinks
    # again would otherwise appear to have its timer reset.
    reasoning_seen: bool = False
    reasoning_since: float | None = None
    reasoning_elapsed: float = 0.0
    message_ts: str = ""
    # The task that posts the card once the turn has outlived the delay.
    post_timer: asyncio.Task | None = None
    # Earliest monotonic time at which a coalescable edit may be spent. A
    # subagent reaching a terminal state ignores it -- that is the news the card
    # exists to report -- and so does the turn ending: a card left claiming that
    # a finished turn is live is the one failure this whole record prevents.
    next_edit_at: float = 0.0
    # The ``status`` of the subagents section as it currently stands on screen,
    # so that a dispatch arriving after that section went ``complete`` can tell
    # that the message now contradicts the record and rewrite it without
    # waiting.
    subagent_card_status: str = ""
    # Set when the turn that owned this record ended. The record is kept so a
    # later turn of the same request rebinds to its card; nothing reopens it
    # except fresh activity, and a closed record is the first thing evicted when
    # too many are held.
    closed: bool = False
    # Set when the turn ended in a harness-side failure rather than an answer,
    # with the text of the terminal chat.error event beside it. Sticky: once a
    # request has failed the card keeps saying so, because the separate message
    # that used to report that error is suppressed on the strength of the card
    # showing it, and a later event unsaying it would take the only copy with
    # it. Nothing on the wire resumes a request after an error anyway.
    failed: bool = False
    harness_error: str = ""
    # Set when this record was settled *by* a stop rather than by the turn
    # ending on its own, which is what lets the title say so. Not the same as
    # ``stop_requested``: that one records that a cancel was sent and is sticky
    # because the button must not come back, whereas this one records why the
    # card reads the way it does.
    #
    # Not sticky, unlike ``failed``, which is kept because the card holds the
    # only copy of the error. A stop leaves no such copy to lose, and a request
    # that resumes and then finishes really did finish, so fresh activity clears
    # this along with ``closed`` -- see ``reopen``.
    stopped: bool = False
    touched_at: float = field(default_factory=time.monotonic)

    def reopen(self) -> None:
        """The turn this record belongs to is working again.

        One place rather than three, because the two flags have to move
        together: a card that is live again must not still be captioned with
        how it last ended.
        """
        self.closed = False
        self.stopped = False

    def settle(self, *, stopped: bool = False) -> None:
        """Mark the turn over, whichever way it ended.

        Runs still open are marked ``unreported`` rather than counted as
        finished or as failures -- nothing established either. A tool that was
        mid-call when the turn ended did not fail: it was never heard from, and
        the card has a word for that which the title's ``n failed`` count
        deliberately does not touch.

        The model is not thinking any more either, whichever way it ended: a
        thinking section still chipped ``in_progress`` above a settled card is
        the same lie as a fan-out still called live.

        Idempotent in the only sense that matters -- both callers check
        ``closed`` before reaching here, so a second terminal event neither
        re-marks a run nor spends an edit saying nothing new.
        """
        if self.post_timer is not None:
            self.post_timer.cancel()
            self.post_timer = None
        for run in (*self.subagent_runs.values(), *self.tool_runs.values()):
            if run.state == _RUN_RUNNING:
                run.state = _RUN_UNREPORTED
        # A compaction pass the turn ended underneath is the same fact in the
        # same word: it was never heard from. The runtime does emit a terminal
        # state of its own when a cancel reaches it -- ``failed`` with the error
        # ``cancelled`` -- so this is for the endings that arrive before that
        # one does, or instead of it.
        for compression in self.compression_runs.values():
            if compression.state == _COMPRESSION_STARTED:
                compression.state = _RUN_UNREPORTED
        self.end_reasoning()
        self.stopped = stopped
        self.closed = True
        self.touched_at = time.monotonic()

    def subagent_totals(self) -> dict[str, int]:
        """How many subagents are in each state, across every type."""
        counts: dict[str, int] = {}
        for run in self.subagent_runs.values():
            counts[run.state] = counts.get(run.state, 0) + 1
        return counts

    def subagents_by_type(self) -> dict[str, dict[str, int]]:
        """The same counts, broken down by ``subagent_type``."""
        by_type: dict[str, dict[str, int]] = {}
        for run in self.subagent_runs.values():
            counts = by_type.setdefault(run.label, {})
            counts[run.state] = counts.get(run.state, 0) + 1
        return by_type

    def tools_by_name(self) -> dict[str, dict[str, int]]:
        """Counts per tool name, the retired runs folded back in."""
        merged = {
            label: dict(counts) for label, counts in self.retired_tools.items()
        }
        for run in self.tool_runs.values():
            counts = merged.setdefault(run.label, {})
            counts[run.state] = counts.get(run.state, 0) + 1
        return merged

    def tool_totals(self) -> dict[str, int]:
        """How many tool calls are in each state, across every tool."""
        totals: dict[str, int] = {}
        for counts in self.tools_by_name().values():
            for state, count in counts.items():
                totals[state] = totals.get(state, 0) + count
        return totals

    def note_reasoning(self) -> None:
        """Open a stretch of reasoning, or let the open one keep running.

        Consecutive chunks continue one stretch rather than starting one apiece:
        the gap between two chunks is the model still producing the same block
        of reasoning, and clocking each chunk separately would report the last
        few milliseconds of a turn that had been thinking for minutes.
        """
        self.reasoning_seen = True
        if self.reasoning_since is None:
            self.reasoning_since = time.monotonic()

    def end_reasoning(self) -> None:
        """Close the open stretch, folding its duration into the total.

        Called when the model does something that is not thinking -- a tool
        call -- and when the turn ends. Idempotent: with no stretch open there
        is nothing to close, which is the ordinary case for the second and third
        caller in a row.
        """
        if self.reasoning_since is None:
            return
        self.reasoning_elapsed += max(0.0, time.monotonic() - self.reasoning_since)
        self.reasoning_since = None

    def compression_running(self) -> bool:
        """Whether any compaction pass is still in flight."""
        return any(
            run.state == _COMPRESSION_STARTED
            for run in self.compression_runs.values()
        )

    def compression_saved_tokens(self) -> int:
        """How many tokens every finished pass recovered between them."""
        return sum(run.saved_tokens for run in self.compression_runs.values())

    def reasoning_seconds(self) -> float:
        """How long this turn has spent reasoning, the open stretch included."""
        total = self.reasoning_elapsed
        if self.reasoning_since is not None:
            total += max(0.0, time.monotonic() - self.reasoning_since)
        return total

    def remember_written(self, blocks: list[dict[str, Any]]) -> None:
        """Note what the message now says, for the one decision that reads it."""
        self.subagent_card_status = ""
        for block in blocks:
            for card in block.get("tasks") or []:
                if str(card.get("task_id") or "").startswith("subagents-"):
                    self.subagent_card_status = str(card.get("status") or "")


@dataclass
class SlackChannelConfig:
    """Runtime configuration for the Slack channel."""

    enabled: bool = False
    bot_token: str = ""
    app_token: str = ""
    # Which workspace this instance serves, as the gateway named it. One
    # ``SlackChannel`` is built per credential block and every one of them
    # reports ``channel_id: slack``, so this is the only thing that tells two
    # apart -- in ``ChannelKey``, in a session subscription, and on the request
    # metadata that has to find its way back to the right connection.
    #
    # Empty is one workspace, and it keys as ``("slack", "default")``: exactly
    # the key the connector has always registered under, so a deployment that
    # never asked for a second workspace is byte-for-byte unchanged by this
    # field existing.
    #
    # Not the Slack team id, which would be the obvious value and is not
    # available: the team id comes from ``auth.test``, which cannot run until
    # the channel starts, while the key is fixed when it registers. The team id
    # is learned anyway and is what an outbound message is matched against --
    # see ``_workspace_team_id`` and ``claims_message`` -- but it is an
    # observation about a live connection rather than an identity the config can
    # hand out.
    app_id: str = ""
    allow_from: list[str] = field(default_factory=list)
    allowed_channel_ids: list[str] = field(default_factory=list)
    # Whose app-posted messages are read as the messages of whoever the ``user``
    # field names: "none" | "listed" | "all", as channels.slack.app_messages
    # settles it. "none" is the default and is the behaviour this connector had
    # before the key existed, so no deployment changes by upgrading into it.
    #
    # A word rather than a boolean because the middle rung is the useful one and
    # a boolean cannot express it, and because the axis has room to widen
    # further without the key changing shape -- the same reason ``history`` is a
    # word. Connector-wide rather than a scope key: this decides whether a
    # message is seen at all, which is nearer a security boundary than a
    # preference, and the security-adjacent sections are deliberately not
    # freely addressable. A per-conversation version can be added later as a
    # scope key without disturbing the word list.
    app_messages: str = APP_MESSAGES_DEFAULT
    # The app ids ``app_messages: listed`` admits, and read under that word
    # alone: entries left here under "none" admit nothing and entries left here
    # under "all" narrow nothing.
    #
    # ``app_id`` values (``A…``) and never ``bot_id`` (``B…``): the first is
    # global to the app, the second is minted per installation, so a reinstall
    # would silently empty a list written against it. See
    # ``resolve_app_messages_from``.
    app_messages_from: tuple[str, ...] = ()
    # How far the Slack history tool may read here, as the one word
    # channels.slack.history settles: disabled | origin | members | visible | open.
    # Layer 0; a scope's agent.history sits above it, per conversation.
    #
    # It replaces history_digest_channel_ids, which is still read and still
    # shipped -- resolve_history_policy translates it -- because a key removed
    # from the template is deleted from an operator's file on upgrade. The list
    # form is gone rather than kept alongside: membership decides per request,
    # against Slack, what that per-deployment allow-list decided by hand.
    history: str = HISTORY_POLICY_DEFAULT
    # Conversations that may never be a target, and members whose presence does
    # not block the subset test. Properties of the workspace rather than of one
    # conversation, which is why neither is a scope key: if a channel must never
    # travel that is true whichever room asks, and an integration is installed
    # workspace-wide. Written per scope they would have to be repeated in every
    # rule and would be forgotten in one.
    history_never_read: tuple[str, ...] = ()
    history_exempt_members: tuple[str, ...] = ()
    # How far the Slack posting tools may write from here, as the one word
    # channels.slack.write settles: disabled | origin | members | open. Layer 0,
    # and layer 0 alone -- there is deliberately no scope key above it, unlike
    # history.
    #
    # A key of its own rather than a widening of history because the widest word
    # inverts between the two. A public target is the safest case for a read,
    # its membership being self-serve, and the widest possible audience for a
    # write. slack_write_policy carries the whole argument, and the inverted
    # membership rule that follows from it.
    write: str = WRITE_POLICY_DEFAULT
    # Per-conversation behaviour, keyed by channel id and already settled by the
    # gateway from the top-level scopes list. A channel with an entry here
    # overrides group_chat_mode, or the prompt appended to its messages, or the
    # model its turns run on, or any combination, and inherits whichever of them
    # it did not name. A delivery or model/history scope opts the room in beyond
    # allowed_channel_ids. Availability-only scopes do not.
    #
    # Every key here is a conversation some scope named, which is what lets the
    # startup summary report one source for all of them.
    conversation_overrides: dict[str, SlackChannelOverride] = field(
        default_factory=dict
    )
    # What a scope matching {channel: slack} and naming no conversation
    # settled, across both sections it can speak in: layer 1, above
    # group_chat_mode and below anything that names a conversation. Every field
    # is None when no such scope exists, which is the default and is
    # indistinguishable from the connector with no scopes written at all.
    #
    # Named for the carrier rather than for a section, because it is not one:
    # mode and prompt come from delivery and model_name from agent.
    platform_override: SlackChannelOverride = field(
        default_factory=SlackChannelOverride
    )
    # The compiled scopes themselves, kept so that a rule naming a sender can be
    # settled when there is a sender to settle it for.
    #
    # The two fields above are settled once, when the config is applied, and
    # keyed on the conversation alone. That is the whole answer only while a
    # rule names no person, the identity axis having no value until a message
    # arrives. The settled maps stay -- they remain the answer for every
    # conversation no rule names a sender in, and for every caller with no
    # sender to offer -- and this is what the per-message path folds again for
    # the rest. See ``settled_override``.
    scopes: tuple[Scope, ...] = ()
    default_channel_id: str = ""
    reply_in_thread: bool = True
    # "mention" | "reply" | "all" | "off", controlling what the bot reads in
    # channels. Named after the Telegram field so operators meet one vocabulary,
    # but the semantics are cumulative rather than exclusive: Slack delivers
    # mentions through a separate app_mention subscription, so "reply" and "all"
    # keep answering mentions instead of ignoring them the way Telegram's
    # equivalent does. "off" is the only mode that silences mentions too.
    group_chat_mode: str = GROUP_MODE_MENTION
    # "reaction" | "text" | "both" | "off". Reactions are the default because
    # they are far quieter in a shared channel than an extra message, and match
    # what the Discord and Telegram channels already do.
    acknowledge_mode: str = ACK_MODE_REACTION
    acknowledgement_text: str = _DEFAULT_ACKNOWLEDGEMENT_TEXT
    # The "is thinking…" line Slack draws in the thread while the turn runs.
    # Free text, and empty turns it off -- which is the whole switch, because
    # there is nothing else to configure: no interval, no timeout, and clearing
    # it when the turn ends is unconditional connector behaviour rather than a
    # setting of its own -- see ``_clear_thinking_status``.
    #
    # No mode of its own either. It rides on acknowledge_mode, so off means off
    # here too, and it is added to the reaction rather than replacing it: a
    # workspace that will not draw the status still gets the emoji, and the
    # request is acknowledged at the same instant either way.
    thinking_status: str = DEFAULT_THINKING_STATUS
    # Two more thread-status values, drawn in place of thinking_status rather
    # than beside it: assistant.threads.setStatus holds one string per thread,
    # so the newer of the two calls is what a reader sees, whichever it is.
    # That is deliberate rather than a limitation to route around -- see
    # ``_set_queued_status`` and ``_set_steered_status``.
    #
    # Unlike thinking_status, blank does not turn either off on its own: these
    # follow the ``or <literal>`` pattern queued_emoji and its siblings use, so
    # an operator wanting no status at all reaches for acknowledge_mode: off.
    # They have no per-key word either, where the six reactions below now take
    # ``disabled`` to drop one mark and keep the rest; the statuses are three
    # spellings of one line and silencing one of them leaves the other two
    # saying the same thing about the same slot, so there is nothing a word
    # here would let an operator express.
    queued_status: str = _DEFAULT_QUEUED_STATUS
    steered_status: str = _DEFAULT_STEERED_STATUS
    # Six emoji for the six states a message can be in: seen, waiting, refused,
    # finished, broken, halted. Each of the six is silenced on its own by writing ``REACTION_DISABLED``
    # in place of a name, which resolves to ``""`` here and makes every add and
    # remove of that one mark a no-op. Blank is not that switch: it falls back
    # to the default, as it always has. See ``resolve_reaction_emoji``.
    acknowledgement_emoji: str = _DEFAULT_ACKNOWLEDGEMENT_EMOJI
    rejected_emoji: str = _DEFAULT_REJECTED_EMOJI
    # Added when a message is held rather than run, and taken off again when it
    # is dispatched. A third outcome beside acknowledged and rejected, and it
    # gets its own emoji for the same reason those two do: a reaction lands on
    # the message it is about, so in a busy thread each person sees the state of
    # their own message and nobody is broadcast at.
    #
    # Reaction only, whatever the acknowledge mode says about text. A queue that
    # posted a message per held request would be noisier than the messages it is
    # holding.
    queued_emoji: str = _DEFAULT_QUEUED_EMOJI
    # How a turn ended, written onto the message that started it once the turn
    # stops: the acknowledgement comes off and one of these goes on. Without
    # them the acknowledgement is the only mark a message ever gets and it
    # stays there for good, so a reader scrolling a busy conversation cannot
    # tell a turn still working from one that finished hours ago -- every
    # message reads "seen at some point".
    #
    # ``completed_emoji`` is written whether or not the turn replied. A turn
    # that did its work and said nothing is a success, and the mark is the only
    # evidence in the channel that it happened at all.
    #
    # ``failed_emoji`` is its own key rather than a second use of
    # ``rejected_emoji``, because the two states differ in what the reader
    # should do about them. Refused means nothing ran: no tool was called, no
    # file was written, and the same message may well go through later.
    # Failed means the turn started, may have called tools and made outbound
    # requests, and then broke -- side effects may exist and re-sending is not
    # obviously safe. One emoji for both would say "something went wrong" and
    # withhold the half that decides what to do next.
    #
    # ``stopped_emoji`` is a third ending and not a shade of either. A halted
    # turn broke nothing and was not refused: it ran and was deliberately
    # destroyed. Two paths reach it -- somebody pressed the stop button, or a
    # later message superseded the turn under ``mid_turn: cancel`` -- and they
    # share a mark because the reader's next step is the same in both: it did
    # not finish, nothing further is coming, ask again if it is still wanted.
    #
    # Reaction only, like the queue mark, and gated on the same acknowledge
    # modes: a deployment on ``text`` or ``off`` gains no reactions from this.
    completed_emoji: str = _DEFAULT_COMPLETED_EMOJI
    failed_emoji: str = _DEFAULT_FAILED_EMOJI
    stopped_emoji: str = _DEFAULT_STOPPED_EMOJI
    # Level for the slack_bolt and slack_sdk logger trees, which are wired into
    # the shared handlers when the channel starts. Its own key rather than
    # logging.level because these two are an order of magnitude chattier than
    # anything else in the process; see DEFAULT_SDK_LOG_LEVEL.
    sdk_log_level: str = DEFAULT_SDK_LOG_LEVEL
    # Show the reply as it is written, or post it once when the turn ends. Off
    # by default: a preview costs API calls from the channel's own budget and
    # produces churn everyone in a shared channel can see, and scheduled pushes
    # have no reader waiting on them.
    #
    # A boolean, like every other connector's copy of this key. How a preview is
    # written -- streamed, or edited in place -- is decided per reply against
    # what Slack will accept for it, which is a thing this connector can see and
    # a config file cannot. See STREAMING_MODES.
    enable_streaming: bool = False
    # "off" | "marker" | "auto", controlling when a Markdown table in a reply is
    # posted as a Block Kit table instead of as text. "auto" is the default: it
    # triggers on the reply containing a pipe table, which models write
    # unprompted, and a reply that judges the structure unwanted can still
    # decline per message with the off marker. "marker" inverts that and waits to
    # be asked, which only does something if the operator has separately put the
    # marker in front of the model.
    blockkit_tables: str = BLOCKKIT_TABLES_DEFAULT
    # "off" | "basic" | "data_table": which block a Markdown table becomes once
    # blockkit_tables has allowed blocks at all. "data_table" is the default and
    # is the block that sorts, filters and downloads; "basic" is the plain one,
    # every row at once and half the budget; "off" leaves the pipes as text
    # without stopping a chart or a hand-written fence in the same reply.
    render_tables: str = slack_blocks.RENDER_TABLES_DEFAULT
    # Which Block Kit block types a hand-written ```blockkit fence may hold.
    # Empty is the default and means no restriction: which types Slack draws is
    # Slack's to say and changes without this repository hearing about it. An
    # operator who wants a narrower surface names the types they want.
    blockkit_allowed_block_types: tuple[str, ...] = ()
    # Whether such a fence may hold buttons, selects and other elements that
    # post back to this app. Off, and separate from the list above on purpose:
    # this connector posts real permission-approval buttons into these channels
    # and a reader cannot tell a forged one from a real one. Widening which
    # blocks may be drawn must not be a way of also widening what a reader can
    # be asked to click.
    blockkit_allow_interactive: bool = slack_blocks.DEFAULT_ALLOW_INTERACTIVE_BLOCKS
    # "off" | "risky" | "all", deciding which payloads are put through
    # blocks.validate before they are sent. "risky" is the default and spends a
    # call on the sources a refusal actually comes from -- a model's own
    # ```blockkit fence, a control nobody can click if it fails, anything
    # unusually large -- while leaving the connector's own typed builders alone.
    # See BLOCKKIT_VALIDATE_MODES for what each mode buys and what it costs.
    blockkit_validate: str = BLOCKKIT_VALIDATE_DEFAULT
    # Group digital avatar, off by default. When enabled, channel traffic is
    # additionally routed through IMInboundPipeline/IMOutboundPipeline, which
    # judge each message's relevance to the principal and decide whether the
    # answer goes back to the channel or as a DM. Left off, every field below is
    # unread.
    #
    # Only useful together with group_chat_mode: all -- the pipeline exists to
    # decide which of the messages the bot sees are worth answering, and the
    # other modes have already filtered that down to mentions or thread replies.
    group_digital_avatar: bool = False
    # Slack user id of the person the avatar speaks for, e.g. "U01ABCDEF".
    my_user_id: str = ""
    # Display name for that person. Resolved from users.info when left empty.
    principal_name: str = ""
    # Bot display name, used to recognise a plain-text "@name" mention that
    # Slack did not turn into a link.
    bot_name: str = ""
    enable_memory: bool = False
    # Show what a turn is doing as one in-progress card: its todo list, the
    # subagents it dispatched, the tools it ran. On by default: a turn that
    # delegates or grinds through a long tool sequence is silent while it does
    # so -- the parent is blocked, so not a single delta is produced -- and a
    # working agent and a hung one look identical from the channel.
    activity_card: bool = True
    # How long a turn has to still be working before it is worth a message. A
    # turn that finishes inside this window posts nothing, which is what keeps
    # the common quick lookup from leaving a card behind it.
    activity_card_delay_seconds: float = 5.0
    # Floor between two intermediate edits of the same card. Every edit spends
    # from the same per-channel budget as the posts that deliver the answer, so
    # progress within a long turn is coalesced rather than reported at every
    # tool call. A subagent returning, and the final edit, are never withheld.
    activity_card_min_edit_seconds: float = 10.0


SLACK_WORKSPACES_KEY = "workspaces"

# Everything a workspace block may hold. Credentials, the switch that turns one
# install off, and the conversation a producer with no request metadata falls
# back to -- which is a per-workspace fact because a channel id names a room in
# one workspace and nothing at all in another.
#
# Nothing else belongs here, and the list is what says so. Every other
# channels.slack key is one connector-wide value: the emoji, the statuses, the
# streaming switch, the history policy, the block-kit settings, group_chat_mode.
# Per-workspace *behaviour* is written as a scope matching that workspace, in
# the top-level scopes list, rather than by giving each block a private copy of
# the connector's settings -- two ways to set one thing is how a deployment ends
# up running the value it did not write.
SLACK_WORKSPACE_KEYS = (
    "bot_token",
    "app_token",
    "enabled",
    "default_channel_id",
)


def normalize_slack_conf(raw: Any) -> dict[str, Any]:
    """Read ``channels.slack`` in either form and return it in the list form.

    The connector serves one workspace per Socket Mode connection, and
    ``SlackChannel.start`` blocks on that connection for the life of the
    channel, so a second workspace is a second channel rather than a second
    handler on the first. What the config has to express is therefore a list of
    installs, and ``workspaces`` is it: one entry per Slack app, holding the
    token pair that app was issued.

    ``bot_token`` and ``app_token`` are only meaningful together -- one
    authenticates the app to the Web API and the other opens its socket, and
    both are minted per install -- so the unit is a block holding the pair.
    Never two parallel lists: two lists can differ in length, can be reordered
    independently, and leave nowhere to put the third fact that belongs to the
    same install.

    **The single-mapping form keeps working and is not deprecated.** A config
    writing ``bot_token`` and ``app_token`` at the top of ``channels.slack`` --
    which is every Slack config written before this key existed -- is read as
    one block holding them, and produces the one channel it always produced,
    under the key it always registered under. Returning the list form from both
    shapes is what lets the gateway hold one loop rather than a loop with a
    special case beside it. An empty ``workspaces`` list is read as no list at
    all, for the reason given at the branch below.

    ``enabled`` is passed through rather than defaulted, in both shapes, so that
    ``_is_channel_enabled`` applies its own rule to a block: written, it
    decides; absent, the block is on exactly when it carries both tokens. That
    is the rule the single mapping has always been read by.

    ``default_channel_id`` falls back to the connector-wide value when a block
    does not name one, so an operator with one fallback conversation need not
    repeat it and one with several can say so per workspace.

    Anything unreadable is reported and dropped rather than raised on. A
    malformed block must not stop the workspaces beside it from coming up, and a
    connector that refuses to start because one entry is a string is a worse
    failure than the one being reported.
    """
    if not isinstance(raw, Mapping):
        if raw is not None:
            logger.warning(
                "channels.slack is %s rather than a mapping; no Slack"
                " workspace is configured",
                type(raw).__name__,
            )
        return {SLACK_WORKSPACES_KEY: []}

    conf = dict(raw)
    fallback_channel = str(conf.get("default_channel_id") or "").strip()
    raw_blocks = conf.get(SLACK_WORKSPACES_KEY)

    # An empty list is read as no list at all, and it has to be: the shipped
    # template carries ``workspaces: []`` -- a key absent from the template is
    # deleted from an operator's file on the next upgrade, so it cannot be
    # left out -- and every existing config gains it the moment it is upgraded.
    # Read literally, that key would empty out a working single-workspace
    # deployment on upgrade, which is the one thing this must not do.
    if not raw_blocks:
        conf[SLACK_WORKSPACES_KEY] = [_slack_workspace_block(conf, fallback_channel)]
        return conf

    if not isinstance(raw_blocks, (list, tuple)):
        logger.warning(
            "channels.slack.workspaces is %s rather than a list; no Slack"
            " workspace is configured",
            type(raw_blocks).__name__,
        )
        conf[SLACK_WORKSPACES_KEY] = []
        return conf

    blocks: list[dict[str, Any]] = []
    for index, entry in enumerate(raw_blocks):
        if not isinstance(entry, Mapping):
            logger.warning(
                "channels.slack.workspaces[%d] is %s rather than a mapping;"
                " skipping it",
                index,
                type(entry).__name__,
            )
            continue
        blocks.append(_slack_workspace_block(entry, fallback_channel, index=index))
    conf[SLACK_WORKSPACES_KEY] = blocks

    # Both shapes written at once. The list wins -- it is the shape that can
    # express what the operator asked for -- and the top-level pair is named so
    # it can be deleted rather than left looking effective.
    #
    # Said only when the pair is nowhere in the list, which is what makes this
    # function idempotent. Normalising writes the list back beside the keys it
    # was built from, and the gateway normalises the same mapping again on every
    # config apply, so a pair that matches a block is this function's own output
    # coming back rather than an operator writing two things.
    top_pair = (
        str(conf.get("bot_token") or "").strip(),
        str(conf.get("app_token") or "").strip(),
    )
    if any(top_pair) and not any(
        (block["bot_token"], block["app_token"]) == top_pair for block in blocks
    ):
        logger.warning(
            "channels.slack holds both a channels.slack.workspaces list and a"
            " top-level bot_token/app_token pair; the list is used and the"
            " top-level pair is ignored"
        )
    return conf


def _slack_workspace_block(
    raw: "Mapping[str, Any]",
    fallback_channel: str,
    *,
    index: int | None = None,
) -> dict[str, Any]:
    """One workspace block, read down to the four keys a block may hold.

    ``index`` is the position in the list, and is ``None`` for the block the
    single-mapping form yields. It is used only to name a key in a warning: the
    single mapping legitimately holds every connector-wide key beside its
    tokens, so there is nothing there to warn about, while a key written inside
    a list entry was written in the belief that it would do something.
    """
    block: dict[str, Any] = {
        "bot_token": str(raw.get("bot_token") or "").strip(),
        "app_token": str(raw.get("app_token") or "").strip(),
        "default_channel_id": (
            str(raw.get("default_channel_id") or "").strip() or fallback_channel
        ),
    }
    # Carried only when written, so _is_channel_enabled keeps applying its own
    # rule to an unset one: a block holding both tokens and no enabled key is
    # on, which is how the single mapping has always been read.
    if "enabled" in raw:
        block["enabled"] = raw["enabled"]
    if index is not None:
        stray = sorted(set(raw) - set(SLACK_WORKSPACE_KEYS))
        if stray:
            logger.warning(
                "channels.slack.workspaces[%d] holds %s, which a workspace"
                " block cannot set: a block holds credentials only, every other"
                " channels.slack key is one connector-wide value, and"
                " per-workspace behaviour is written as a scope. The listed"
                " keys are ignored",
                index,
                ", ".join(stray),
            )
    return block


def _scopes_name_a_sender(scopes: "Sequence[Scope] | None") -> bool:
    """Whether any compiled scope constrains the identity axis.

    One half of the switch between the two paths below. With nothing naming a
    sender there is nothing a sender could change, so the settled maps are the
    whole answer and the per-message fold never runs -- which keeps this free
    for every deployment that has not written such a rule, and byte-for-byte
    identical for them.
    """
    return any(scope.match.constrains_identity for scope in (scopes or ()))


def _scopes_name_a_chat_type(scopes: "Sequence[Scope] | None") -> bool:
    """Whether any compiled scope names a kind of conversation.

    The other half of the same switch, and it is needed for the same reason the
    sender half is. The settled maps are keyed on the conversation and were
    composed with no kind, because at config-apply time there is nothing to ask:
    the ids in them come out of the file, and what kind of conversation each is
    is a fact Slack puts on a message.
    """
    return any(scope.match.chat_type is not None for scope in (scopes or ()))


def _scopes_name_a_workspace(scopes: "Sequence[Scope] | None") -> bool:
    """Whether any compiled scope names a workspace.

    The third half of the same switch, and it is needed for the reason the other
    two are. The settled maps are composed once per config apply, and the
    workspace is not known then: it is the team id ``auth.test`` answers with,
    and ``auth.test`` cannot run until the channel starts. So a deployment that
    writes a workspace rule refolds per message and every other deployment does
    not pay for the axis existing.
    """
    return any(scope.match.workspace is not None for scope in (scopes or ()))


def event_chat_type(event: "Mapping[str, Any] | None") -> str:
    """What kind of conversation one event came from, in Slack's own word.

    ``event["channel_type"]`` and nothing derived. Slack is the only writer of
    it, and the four words it writes are the four this connector declares, so
    the value reaches the matcher exactly as it arrived.

    **Nothing is invented for an event that does not carry it.** The empty
    string is what an absent field yields, and it fails closed at the matcher:
    a rule naming a kind does not fire for a message whose kind Slack did not
    state, rather than firing as whichever kind seemed likeliest. Reading a
    missing field as "channel" would be the one failure worth avoiding here --
    a rule written for public rooms silently applied to a group direct message
    -- and the id prefix cannot settle it either, D marking a direct message
    while C and G cover a public channel, a private one and a group DM between
    them.

    A separate function rather than an inline read, so the one convention lives
    in one place and a second caller cannot invent a second.

    **What it reports is the payload, not the conversation.** A payload that
    says nothing yields nothing here, and a payload that says something in a
    vocabulary this connector does not use yields that. Neither is a kind a
    decision may be taken on. :meth:`SlackChannel._conversation_chat_type` is
    the reader that turns this into one, and every decision goes through that.
    """
    if not isinstance(event, Mapping):
        return ""
    return str(event.get("channel_type") or "").strip()


#: Slack's four words for a kind of conversation, and the only four anything
#: here acts on: ``channel`` for a public channel, ``group`` for a private one,
#: ``im`` for a one-to-one direct message, ``mpim`` for a group direct message.
#:
#: Taken from the scope declaration rather than spelled again, so the words an
#: operator may write in a ``match: {chat_type: [...]}`` rule and the words this
#: connector will accept off the wire cannot drift apart.
#:
#: The set is needed because Slack does not keep to one vocabulary for the
#: field. A ``message`` event carries one of these four in ``channel_type``;
#: ``member_joined_channel`` and ``member_left_channel`` carry ``"C"`` or
#: ``"G"`` in that same field, which is the older one-letter code for a public
#: and a private channel. A one-letter code is not one of the four, so it is
#: not a kind, and passing it on would be worse than passing nothing: it
#: matches no rule an operator can write, and it reaches request metadata where
#: ``read_slack_conversation`` refuses any word it does not recognise.
SLACK_CHAT_TYPES: "frozenset[str]" = frozenset(
    SLACK_CAPABILITIES.axis_values.get(AXIS_CHAT_TYPE) or ()
)

#: Slack's word for a one-to-one direct message, which is the one kind a
#: conversation id states on its own. See :func:`chat_type_from_chat_id`.
CHAT_TYPE_IM = "im"


def chat_type_from_chat_id(chat_id: str) -> str:
    """The kind a Slack conversation id states by itself, or ``""``.

    One prefix answers and the rest do not, and the asymmetry is the whole of
    why this is a narrow function rather than a lookup table.

    ``D`` names a one-to-one direct message and names nothing else, so reading
    ``D`` as ``im`` is reading the id rather than guessing at it. This connector
    already draws that line in several places -- a stream needs recipient ids
    outside a DM, a reply may never be optional inside one -- and each of those
    asks only "is this the one-to-one conversation", which is exactly what the
    prefix states.

    ``C`` and ``G`` do not separate the three kinds that are left. Slack issued
    ``G`` for private channels and group DMs and now issues ``C`` for public
    and private channels alike, so either letter can be a ``channel``, a
    ``group`` or an ``mpim``. Reading a ``C`` as ``channel`` would apply a rule
    written for public rooms to a private one, which is the one failure worth
    avoiding here: a wrong kind matches a rule the operator wrote about
    somewhere else, while an absent kind matches no rule at all and leaves the
    conversation on the layer below. So nothing is returned for them.
    """
    return CHAT_TYPE_IM if str(chat_id or "").startswith("D") else ""


def settled_override(
    config: "SlackChannelConfig",
    channel_id: str,
    *,
    user_id: str = "",
    chat_type: str = "",
    workspace: str = "",
) -> SlackChannelOverride:
    """What the scopes settle for one message: one conversation, one sender.

    Every per-conversation setting this connector reads comes through here --
    mode, prompt, mid_turn, session, reply, model_name and history -- so there
    is one place where "which layer won" is decided rather than a copy of the
    same two-line cascade at each reader.

    **Two paths, and the fast one needs neither the sender nor the kind.** With
    no rule naming either -- every deployment that has not written one -- this
    is the settled per-conversation entry laid over the settled platform layer,
    per key, with no refolding at all. With such a rule the scopes are folded
    again for this ``(conversation, sender, kind)`` triple, because neither of
    those two axes has a value until a message arrives and a map keyed on the
    conversation cannot hold the answer.

    ``chat_type`` is the second reason to refold and it is the same reason. The
    conversation map is built at config apply, from ids read out of the file,
    and what kind of conversation each id names is not in the file: Slack puts
    it on the message.

    ``workspace`` is the third and its reason is the same one again, one step
    further out. The map is composed at config apply and the team id is not
    known then -- it is what ``auth.test`` answers with, and that call cannot
    run until the channel starts -- so a rule naming a workspace can only be
    folded in once a live connection has said which workspace this is.

    **It is the connection's team, not the event's.** Every caller passes
    ``self._workspace_team_id``, which is what this connection *is*, rather than
    the ``team`` an inbound body says a message came from. The two differ in a
    Slack Connect channel, where the event names the sender's home team; and the
    event-derived value does not exist at all on the paths that hold no
    event -- a cron push, a resumed turn, a heartbeat follow-up -- where an
    absent value would fail the axis closed and quietly drop a workspace rule
    the operator wrote.

    **An unidentified sender, an event with no kind on it, and a connection that
    has not yet learned its team take the fast path deliberately.** Folding
    again for an empty value would give the same answer the matcher already
    gives for one -- a rule naming people, or a kind, or a workspace does not
    fire -- and the settled maps were composed without any of the three, so they
    already are that answer.

    The composed result is not re-warned about here. ``_warn_if_mode_is_unusual``
    speaks once per config apply, naming the conversation; saying it again per
    message would be the same line at message rate, which is how a warning worth
    reading gets filtered out.
    """
    if (
        (user_id and _scopes_name_a_sender(config.scopes))
        or (chat_type and _scopes_name_a_chat_type(config.scopes))
        or (workspace and _scopes_name_a_workspace(config.scopes))
    ):
        # All three values are handed to both folds whichever of them triggered
        # the refold. A deployment with one rule of each kind gets one answer
        # rather than three partial ones, and the fold is where they are
        # supposed to meet.
        return sections_as_override(
            {
                SECTION_DELIVERY: compose_section(
                    config.scopes,
                    channel="slack",
                    chat=channel_id or None,
                    chat_type=chat_type or None,
                    workspace=workspace or None,
                    user=user_id,
                    section=SECTION_DELIVERY,
                    layer0={"mode": _slack_layer0_triggers(config.group_chat_mode)},
                ),
                SECTION_AGENT: compose_section(
                    config.scopes,
                    channel="slack",
                    chat=channel_id or None,
                    chat_type=chat_type or None,
                    workspace=workspace or None,
                    user=user_id,
                    section=SECTION_AGENT,
                ),
            }
        )

    conversation = (config.conversation_overrides or {}).get(channel_id)
    platform = config.platform_override
    if conversation is None:
        return platform
    # Per key, not per entry. A conversation whose scope set only a prompt keeps
    # the platform layer's model and triggers rather than being read as having
    # declined every other key.
    return SlackChannelOverride(
        mode=conversation.mode if conversation.mode is not None else platform.mode,
        prompt=(
            conversation.prompt if conversation.prompt is not None else platform.prompt
        ),
        mid_turn=(
            conversation.mid_turn
            if conversation.mid_turn is not None
            else platform.mid_turn
        ),
        session=(
            conversation.session if conversation.session is not None else platform.session
        ),
        reply=(
            conversation.reply if conversation.reply is not None else platform.reply
        ),
        # Per key like every other line here, and not a second merge of the two
        # mappings. The conversation entry was composed from every scope that
        # matches the conversation, which includes the platform-wide ones, so
        # the per-family merge has already happened inside ``compose_values``.
        # Merging again here would fold the platform layer in twice and let a
        # conversation that deliberately set a family back to ``off`` be
        # overridden by the platform value it was written to escape.
        events=(
            conversation.events if conversation.events is not None else platform.events
        ),
        history=(
            conversation.history
            if conversation.history is not None
            else platform.history
        ),
        model_name=(
            conversation.model_name
            if conversation.model_name is not None
            else platform.model_name
        ),
        subagents=(
            conversation.subagents
            if conversation.subagents is not None
            else platform.subagents
        ),
        skills=(
            conversation.skills if conversation.skills is not None else platform.skills
        ),
    )


def slack_agent_request_params(
    config: SlackChannelConfig,
    channel_id: str,
    *,
    user_id: str = "",
    chat_type: str = "",
    workspace: str = "",
) -> dict[str, list[str]]:
    """Put configured agent availability and required skills on this request."""
    override = settled_override(
        config, channel_id, user_id=user_id, chat_type=chat_type, workspace=workspace
    )
    params: dict[str, list[str]] = {}
    if override.subagents is not None:
        params["agent_subagents_available"] = sorted(override.subagents)
    if override.skills is not None:
        for key, param in (
            ("available", "agent_skills_available"),
            ("required", "agent_skills_required"),
        ):
            if key in override.skills:
                params[param] = list(override.skills[key])
    return params


def slack_scope_enables_channel(config: SlackChannelConfig, channel_id: str) -> bool:
    """Whether a room's scope opts it in beyond ``allowed_channel_ids``."""
    if not config.scopes:
        # Legacy callers can still supply settled overrides directly.
        return channel_id in (config.conversation_overrides or {})
    return any(
        scope.match.channel in (None, "slack")
        and scope.match.chat == channel_id
        and (
            bool(scope.section(SECTION_DELIVERY))
            or bool({"model_name", KEY_HISTORY} & scope.section(SECTION_AGENT).keys())
        )
        for scope in config.scopes
    )


def slack_history_request_metadata(
    config: SlackChannelConfig,
    channel_id: str,
    *,
    user_id: str = "",
    workspace: str = "",
) -> dict[str, Any]:
    """The three history keys one request holds, settled for its sender.

    Settled per request rather than per conversation because a scope may name a
    sender: in a shared thread the person asking and the person the session
    belongs to are routinely different people, and it is the asker's request
    that is being answered into this room.

    The word is the settled ``agent.history`` where a scope wrote one and
    ``channels.slack.history`` otherwise. The two lists have no scope form and
    come from layer 0 alone.

    Always all three keys, never a subset. An absent word means *no Slack
    connector settled this request*, which the runtime reads as a refusal; a
    connector that settled it to ``disabled`` is a different thing, and only
    one of those two should ever be reachable from a path holding a Slack
    conversation.

    ``workspace`` is the team id of the connection this request arrived on, and
    it travels for the reason ``user_id`` does: a scope may name a workspace,
    and with two installations configured a rule written for one of them must
    not settle the other one's requests.
    """
    settled = settled_override(
        config, channel_id, user_id=user_id, workspace=workspace
    )
    return history_policy_metadata(
        settled.history or config.history,
        never_read=config.history_never_read,
        exempt_members=config.history_exempt_members,
    )


def slack_write_request_metadata(config: SlackChannelConfig) -> dict[str, Any]:
    """The one write key a request holds, settled for the connector.

    Settled per connector rather than per request, which is the whole of the
    difference from :func:`slack_history_request_metadata` next door. The
    history word has a scope form -- ``agent.history`` can narrow or widen one
    conversation, and a rule may name a sender -- so it has to be settled again
    for every message. ``channels.slack.write`` has no scope form: there is one
    answer for the platform, and it does not vary with who is speaking or where.

    A scope key can be added later without this shape changing, and is left out
    now rather than written speculatively. The three axes a ``write`` scope would
    be interesting on -- conversation, sender, workspace -- all bear on *who may
    be posted to*, which the membership rule already decides per call against
    Slack rather than against a list somebody maintained by hand.

    Always stamped, never omitted. An absent word means *no Slack connector
    settled this request*, which the runtime reads as a refusal; a connector
    that settled it to ``disabled`` is a different thing.
    """
    return write_policy_metadata(config.write)


def channel_triggers(
    config: SlackChannelConfig,
    channel_id: str,
    *,
    group_chat_mode: str,
    user_id: str = "",
    chat_type: str = "",
    workspace: str = "",
) -> frozenset[str]:
    """Which triggers wake the bot in ``channel_id``, for ``user_id``.

    The cascade, read from the most specific layer down:

    1. What the scopes settled for this conversation and this sender -> use it,
       stop. It is total for them; nothing below is consulted for it.
    2. Otherwise what a scope naming the platform settled, which is the same
       rule one layer up and total for every conversation that did not name
       itself.
    3. Otherwise the global ``group_chat_mode``, coerced through
       ``_LEGACY_MODE_TRIGGERS``.

    The first two are :func:`settled_override`'s job, and the layering between
    them is stated there rather than repeated here.

    ``mode`` is settled per key, so a conversation whose scope set only a prompt
    or a model falls through to the layer below for its triggers rather than
    being taken as silent.

    ``user_id`` is optional because two callers genuinely have none: the startup
    summary settles a conversation before anyone has spoken in it, and a caller
    that omits it gets the answer for an unidentified sender rather than an
    answer for everybody. ``chat_type`` is optional for the same reason and with
    the same consequence: the startup summary has no message to read a kind off.
    ``workspace`` is optional for a third variant of it -- the startup summary
    runs before ``auth.test`` has named the team -- with the same answer: a rule
    naming a workspace does not contribute to what it reports.
    """
    override = settled_override(
        config,
        channel_id,
        user_id=user_id,
        chat_type=chat_type,
        workspace=workspace,
    )
    if override.mode is not None:
        return override.mode

    return _LEGACY_MODE_TRIGGERS.get(
        group_chat_mode, _LEGACY_MODE_TRIGGERS[GROUP_MODE_MENTION]
    )


def describe_configured_channels(config: SlackChannelConfig) -> str:
    """One line naming every channel the config speaks about, and how.

    Not a check that the bot is a member of each: that would add a startup
    failure mode for the times Slack is unreachable, go stale the moment someone
    invites the bot somewhere, and still miss the likelier error of a mistyped
    but well-formed id. Listing what was configured costs no API call and
    catches that one, because a mistyped id looks wrong beside the working ones.
    """
    mode = str(config.group_chat_mode or "").strip().lower()
    if mode not in _LEGACY_MODE_TRIGGERS:
        mode = GROUP_MODE_MENTION

    # What a channel nobody named actually follows. A platform scope is layer 1
    # and beats group_chat_mode, so naming the global here when a scope has
    # replaced it would print the one value no longer in force. Only mode is
    # reported: prompt, model, mid_turn and session have no connector-wide key
    # beneath them to be misread, and this line does not name history.
    fallback = f"group_chat_mode={mode}"
    platform = config.platform_override
    if platform.mode is not None:
        fallback = (
            "the scope for channel slack:"
            f" mode=[{','.join(sorted(platform.mode)) or 'silent'}]"
        )

    channel_ids = sorted(config.conversation_overrides or {})
    if not channel_ids:
        message = (
            "channels.slack has no per-channel configuration; every channel"
            f" follows {fallback}"
        )
        logger.info(message)
        return message

    entries: list[str] = []
    for channel_id in channel_ids:
        # The settled override, which lays the conversation layer over the
        # platform one. Reading the conversation entry alone would print "no
        # prompt" and "default model" for a channel that has both from a
        # platform scope. One read serves the whole line.
        override = settled_override(config, channel_id)
        triggers = sorted(channel_triggers(config, channel_id, group_chat_mode=mode))
        # Per entry rather than per key, and deliberately coarse: it names where
        # this conversation was spoken about, not which layer settled each of
        # mode, prompt and model. Splitting it per key would be a provenance
        # table rather than a summary line.
        via = "scopes"
        prompt = via if override.prompt is not None else "none"
        # Named rather than reduced to a yes/no, for the reason the channel ids
        # are listed: a subtly wrong model name reads as wrong beside the right
        # ones, and "default" beside a channel the operator believes they pinned
        # is what tells them the setting was dropped.
        model = override.model_name or "default"
        # Named only when a scope settled it, unlike mode, prompt and model.
        # Those three answer "what is in force here", where the default is worth
        # reading; this one answers "does this conversation depart from the
        # norm", and an unset key is already absent rather than filled in with
        # the default. A settled value is printed whichever value it is: when
        # the default was cancel this line also suppressed a written cancel, on
        # the grounds that it said nothing, but a conversation that asks to have
        # its turns cancelled is now departing from the norm and is exactly the
        # line worth reading.
        mid_turn = f" mid_turn={override.mid_turn}" if override.mid_turn else ""
        # Named on the same terms as mid_turn, and for the same reason: a
        # conversation whose threads share one session departs from what every
        # other conversation does, and that is the line an operator wants to
        # find when a thread answers as though it had read another one.
        session = f" session={override.session}" if override.session else ""
        # Named on the same terms as the two above, and for the same reason: a
        # conversation whose turns may answer nothing at all departs from what
        # every other conversation does, and that is the line an operator wants
        # when a room has gone quiet. Printed whichever word a scope settled,
        # ``required`` included, because writing today's default explicitly is
        # itself a statement about a conversation somebody thought about.
        reply = f" reply={override.reply}" if override.reply else ""
        entries.append(
            f"{channel_id} mode=[{','.join(triggers) or 'silent'}]"
            f" prompt={prompt} model={model}{mid_turn}{session}{reply}"
            f" via={via}"
        )

    message = (
        f"channels.slack is configured for {len(entries)} channel(s):"
        f" {'; '.join(entries)}; every other channel follows {fallback}"
    )
    logger.info(message)
    return message


def slack_default_channel_id_from_config(
    config: Mapping[str, Any] | None = None,
) -> str:
    """``channels.slack.default_channel_id`` as the last rung of the ladder sees it.

    The producers that have to ask "is there a fallback?" all run before a
    ``SlackChannelConfig`` exists -- the cron RPC on every job write, the
    heartbeat check while the gateway is still assembling its own config -- so
    the value is read from the config mapping instead. Pass the mapping when the
    caller already has it; ``None`` reads live config.

    Never raises. A config that cannot be read is reported as no fallback, which
    is the same answer the ladder gives for an unset one and errs towards
    warning rather than towards silence.

    **With ``channels.slack.workspaces`` written this answers for the
    deployment, not for one workspace.** The connector-wide key is routinely
    empty there -- a channel id names a room in one workspace and nothing at all
    in another, so the fallback belongs in the block -- and the first block that
    names one is taken as the answer. That is exact for the single-workspace
    deployment, which is every caller's shape today, and for several workspaces
    it is what the question deserves: both callers ask "is there a fallback at
    all, or is this producer aimed at nowhere?", and one configured fallback is
    enough for the answer to be no-warning. Which workspace a given message
    actually lands in is decided by the channel instance that delivers it, off
    the request it is answering, not here.
    """
    mapping: Any = config
    if mapping is None:
        try:
            from jiuwenswarm.common.config import get_config

            mapping = get_config() or {}
        except Exception as exc:  # noqa: BLE001 - a warning may not break a start.
            logger.warning(
                "channels.slack.default_channel_id could not be read, treating"
                " it as unset: %s",
                exc,
            )
            return ""
    channels = mapping.get("channels") if isinstance(mapping, Mapping) else None
    slack = channels.get("slack") if isinstance(channels, Mapping) else None
    raw = slack.get("default_channel_id") if isinstance(slack, Mapping) else None
    fallback = str(raw or "").strip()
    if fallback:
        return fallback
    blocks = slack.get(SLACK_WORKSPACES_KEY) if isinstance(slack, Mapping) else None
    for block in blocks if isinstance(blocks, (list, tuple)) else ():
        if not isinstance(block, Mapping):
            continue
        candidate = str(block.get("default_channel_id") or "").strip()
        if candidate:
            return candidate
    return ""


HEARTBEAT_SLACK_UNREACHABLE_MESSAGE = (
    "heartbeat.target=slack but channels.slack.default_channel_id is unset:"
    " a heartbeat relay carries no conversation of its own, so no heartbeat"
    " will ever be delivered to Slack. Set channels.slack.default_channel_id to"
    " the channel heartbeats should land in, or point heartbeat.target at"
    " another channel."
)


def warn_if_heartbeat_relay_unreachable(
    *,
    heartbeat_target: Any,
    default_channel_id: Any,
) -> str:
    """Say so when the heartbeat is aimed at Slack and can never get there.

    Stated flatly rather than as a possibility, which is where this differs from
    the cron warning: a cron job at least holds a session id that might name a
    conversation, so its warning is about the one it happens to hold. A
    heartbeat relay has no session at all, so rung 4 is the only rung it can
    ever reach and an unset ``default_channel_id`` is not a risk but a decided
    outcome.

    Returns the message it logged, or ``""`` when there was nothing to say.
    Warns and returns either way -- a misconfigured heartbeat is not a reason to
    refuse to start, and the relay it cannot deliver is a health ping, not work.
    """
    if str(heartbeat_target or "").strip().lower() != SLACK_CHANNEL_ID:
        return ""
    if SlackChannel.delivery_is_reachable(
        session_id=None, default_channel_id=default_channel_id
    ):
        return ""
    logger.warning(HEARTBEAT_SLACK_UNREACHABLE_MESSAGE)
    return HEARTBEAT_SLACK_UNREACHABLE_MESSAGE


async def describe_slack_delivery_reachability(
    config: SlackChannelConfig,
    *,
    cron_store: Any,
    heartbeat_target: Any = "",
) -> str:
    """One line saying which Slack producers have somewhere to deliver to.

    Logged beside ``describe_configured_channels``: that line answers "where
    will the bot listen", and this one answers "where will it speak". Every
    producer otherwise fails at delivery time, one scheduled run at a time, in a
    log written hours after the change that broke it. This is written at
    startup, where an operator is reading.

    ``cron_store`` is duck-typed on ``list_jobs()`` so this stays free of the
    store module, and an unreadable store is reported in the line rather than
    raised: a Slack connector that refuses to come up because cron_jobs.json is
    malformed would be a much worse failure than the one being warned about.

    Returns the message, logged at WARNING when anything is unreachable or
    unknown and at INFO when everything resolves.
    """
    fallback = str(config.default_channel_id or "").strip()
    parts: list[str] = []
    unreachable: list[str] = []
    store_readable = True

    try:
        jobs = list(await cron_store.list_jobs())
    except Exception as exc:  # noqa: BLE001 - reported, never raised.
        store_readable = False
        jobs = []
        parts.append(
            f"the cron store could not be read ({exc}), so no cron job was checked"
        )

    if store_readable:
        enabled_slack = [
            job
            for job in jobs
            if str(getattr(job, "targets", "") or "").strip().lower()
            == SLACK_CHANNEL_ID
            and bool(getattr(job, "enabled", False))
        ]
        for job in enabled_slack:
            if not SlackChannel.delivery_is_reachable(
                session_id=getattr(job, "session_id", None),
                default_channel_id=fallback,
            ):
                unreachable.append(str(getattr(job, "id", "") or "").strip() or "<no id>")
        if not enabled_slack:
            parts.append("no enabled Slack cron job")
        elif not unreachable:
            parts.append(
                f"all {len(enabled_slack)} enabled Slack cron job(s) resolve a channel"
            )
        else:
            parts.append(
                f"{len(unreachable)} of {len(enabled_slack)} enabled Slack cron"
                f" job(s) cannot resolve a channel and will fail at delivery"
                f" ({', '.join(sorted(unreachable))})"
            )

    target = str(heartbeat_target or "").strip().lower()
    if target != SLACK_CHANNEL_ID:
        parts.append(f"heartbeat.target={target or '<unset>'} is not Slack")
    elif SlackChannel.delivery_is_reachable(
        session_id=None, default_channel_id=fallback
    ):
        parts.append("heartbeat.target=slack falls back to default_channel_id")
    else:
        parts.append(
            "heartbeat.target=slack has no fallback and can never be delivered"
        )

    parts.append(
        f"channels.slack.default_channel_id={fallback}"
        if fallback
        else "channels.slack.default_channel_id is unset"
    )

    message = f"channels.slack delivery: {'; '.join(parts)}"
    if unreachable or not store_readable or (target == SLACK_CHANNEL_ID and not fallback):
        logger.warning(message)
    else:
        logger.info(message)
    return message


def slack_session_team_id(session_id: Any) -> str:
    """The workspace a Slack session id was built for, or the empty string.

    Every Slack session id opens ``slack_{team}_…`` -- see ``_session_id`` for
    the three forms -- so the team survives on anything that carries a session,
    including a scheduled run rebuilt hours later with no inbound event behind
    it. A Slack team id is ``T`` followed by alphanumerics and never holds an
    underscore, so the second field is the whole of it.

    Stdlib string work and no import of the cron package, which has its own
    parser for the *conversation* half of the same string and deliberately does
    not reach back into this module.
    """
    raw = str(session_id or "").strip()
    if not raw.startswith("slack_"):
        return ""
    parts = raw.split("_", 2)
    if len(parts) < 3:
        # ``slack_T…`` with no conversation after it is not one of the three
        # forms; it was assembled out of something missing.
        return ""
    return parts[1].strip()


def slack_message_team_id(msg: Any) -> str:
    """The workspace one message belongs to, from whichever of the two sources has it.

    ``slack_team_id`` on the metadata first. It is stamped on every inbound
    request by the connector that received it, from the event body, and it
    survives the round trip through the runtime -- so a reply carries the
    workspace of the message it answers.

    The session id second, which is what a producer with no inbound message
    behind it has: a scheduled push, a heartbeat relay, anything the gateway
    rebuilds after the request that started it is gone.

    Empty when neither says. That is not a workspace to guess at -- see
    ``ChannelManager.resolve_outbound_channel`` for what is done with it.
    """
    metadata = getattr(msg, "metadata", None)
    if isinstance(metadata, Mapping):
        team = str(metadata.get("slack_team_id") or "").strip()
        if team:
            return team
    return slack_session_team_id(getattr(msg, "session_id", ""))


class SlackChannel(BaseChannel):
    """Slack Bot channel using Bolt's asynchronous Socket Mode adapter."""

    name = "slack"

    # ``send`` renders a ``chat.ask_user_question`` as something answerable:
    # option buttons, or the input elements a question's ``inputs`` declare, with
    # the answer routed back to the waiting turn. The default is False because
    # most IM channels read only ``payload["content"]`` and would drop the event
    # silently; ``outgoing_for_channel`` degrades those to plain text so the wait
    # is at least visible. That degradation is a loss here -- it discards the
    # request_id, the options and the inputs, so a question this connector can
    # fully render would arrive as text saying it cannot be answered, and the
    # renderer would be unreachable from the dispatch loop. A question this
    # channel genuinely cannot answer is still handled -- ``_send_question``
    # posts a notice naming it -- so nothing depends on the degradation
    # happening upstream of us.
    renders_interactive_prompts = True

    def __init__(
        self,
        config: SlackChannelConfig,
        router: RobotMessageRouter,
        *,
        dedup_store: SlackEventDedupStore | None = None,
        im_platform_adapter: Any = None,
    ):
        super().__init__(config, router)
        self.config: SlackChannelConfig = config
        self._app: Any = None
        self._handler: Any = None
        self._client: Any = None
        self._bot_user_id = ""
        # The ``B…`` id of the app's bot, as distinct from the ``U…`` id above
        # that it posts as. Never a valid mention token; kept only so a message
        # addressed to it can be named rather than filed as chatter.
        self._bot_id = ""
        # ``auth.test``'s ``url`` reduced to a host: "acme.slack.com". Empty
        # until the call has answered, and empty again after ``stop``. Nothing
        # is granted by it -- it only widens the file-download allow-list from
        # files.slack.com to include the workspace's own host, so an unset value
        # is the narrow reading and not a failure.
        self._workspace_host = ""
        # The ``T…`` id of the workspace these credentials are installed in,
        # read off the same ``auth.test`` payload. Empty until that call has
        # answered, and empty again after ``stop``.
        #
        # It is what an outbound message is matched against when more than one
        # workspace is configured: every inbound request carries the team on its
        # metadata and every Slack session id opens with it, so a reply can be
        # traced back to the connection that has to deliver it. See
        # ``claims_message``. With one workspace configured nothing reads it --
        # there is no other instance a message could belong to.
        #
        # Named for the workspace rather than called ``_team_id``, which is the
        # method that reads a team off an inbound body: one is what this
        # connection is, the other is what an event says it came from, and the
        # whole point of the first is to be compared against the second.
        self._workspace_team_id = ""
        # What Slack said this token was granted, from the ``x-oauth-scopes``
        # header on the startup ``auth.test``. ``None`` until that call answers,
        # and ``None`` again if it answered without the header -- which is not
        # the same as an empty set, and the startup check turns on the
        # difference. See ``slack_granted_scopes``.
        self._granted_scopes: "frozenset[str] | None" = None
        self._on_message_cb: Callable[[Message], Any] | None = None
        # Only set when channels.slack.group_digital_avatar is on; ``None``
        # keeps every avatar branch below inert.
        self._im_platform_adapter: Any = im_platform_adapter
        # Persisted, so a restart cannot re-admit the retries Slack still has in
        # flight. Injectable for tests.
        self._dedup = dedup_store or SlackEventDedupStore()
        # Replies currently being streamed, keyed by (message id, channel,
        # thread) -- one turn fanned out to two channels is two streams.
        self._streams: dict[tuple[str, str, str], _SlackStream] = {}
        # Earliest monotonic time at which each channel may be edited again.
        self._stream_next_update_at: dict[str, float] = {}
        # Who the session's most recently dispatched turn is for, keyed by
        # session id: chat.startStream refuses a channel stream without a
        # recipient, and the reply that will be streamed holds the session but
        # not the person. The ids are read off the inbound event, which is the
        # only place both are available, and written where the turn starts --
        # never where a message is merely held or handed to a turn already
        # running. Kept no longer than the streams that read it.
        self._stream_recipients: dict[str, tuple[str, str]] = {}
        # Who started the turn each session is currently running, keyed by
        # session id. Opened at dispatch and closed by that turn's terminal
        # event; a session with no turn in flight has no entry, which is the
        # distinction ``_stream_recipients`` above cannot make.
        self._turn_initiators: dict[str, _SlackTurnInitiator] = {}
        # Messages held under ``mid_turn: queue``, keyed by session id and in
        # arrival order within each session. One list per session rather than
        # one queue overall, because idleness is per session: a thread that is
        # working must not hold up a DM that is not.
        self._queued_messages: dict[str, list[_SlackQueuedMessage]] = {}
        # Requests whose end-of-stream says only "input taken", keyed by request
        # id. Written when ``runtime.accepted`` arrives and read by the ending
        # that follows it.
        self._ack_only_requests: dict[str, float] = {}
        self._stream_lock = asyncio.Lock()
        # Questions posted as buttons and not yet answered, keyed by request id.
        self._pending_questions: dict[str, _SlackPendingQuestion] = {}
        # Scheduled runs whose placeholder is on screen, keyed by (run id,
        # channel) -- the pair rather than the run id alone because the channel
        # is resolved here and a run that ever fans out must not edit another
        # channel's card.
        self._cron_records: dict[tuple[str, str], _SlackCronRecord] = {}
        # Turns in flight, keyed by (request id, channel) for the same reason
        # the cron records are: the channel is resolved here.
        self._activity_records: dict[tuple[str, str], _SlackActivityRecord] = {}
        # Resumed request id -> the id the request started under, so that the
        # turns an interrupt splits a request into share one card.
        self._activity_request_aliases: dict[str, str] = {}
        # The delayed post runs in a task of its own, so the dispatch loop and
        # that task can both be holding a record. Every mutation is under this.
        self._activity_lock = asyncio.Lock()
        # (channel id, model name) pairs already reported as no longer
        # configured, so the send-time check in ``_channel_model_name`` says it
        # once instead of once per message. Deliberately per instance: a config
        # reload builds a new channel, which re-arms the warning, and a warning
        # that reappears after a reload is exactly the one worth seeing again.
        self._reported_missing_models: set[tuple[str, str]] = set()
        # Channels already told that their delivery.session and delivery.mid_turn
        # disagree, so the refusal is reported once instead of once per message.
        # Per instance, like the set above, so a config reload re-arms it.
        self._reported_steer_under_channel_session: set[str] = set()
        # Event types the catch-all listener has already reported as having no
        # reader here. Per instance, like the two sets above, so a config reload
        # re-arms the line.
        self._unclaimed_event_types: set[str] = set()
        # Refusals ``reactions.add`` has already been reported for, keyed by
        # (Slack's own error code, the emoji it was asked for).
        #
        # **Per instance, which is per workspace.** The gateway builds one
        # ``SlackChannel`` per credential block, so a latch held here cannot
        # report one installation's missing grant and then swallow the same
        # grant missing in another. A module-level flag would, and a workspace
        # whose app was installed from a narrower manifest would then look
        # healthy in the log. A config reload builds a new channel and re-arms
        # the line, which is the behaviour ``_reported_missing_models`` already
        # has and for the same reason.
        #
        # **The emoji is part of the key, not a detail of the line.** Most codes
        # here are about the app -- ``missing_scope`` is one fact however many
        # marks it refuses -- but ``invalid_name`` is about the shortcode that
        # was asked for, and latching the second bad emoji behind the first
        # would hide a second thing an operator has to fix. The pair is bounded
        # by the four emoji this connector can be configured with, so the
        # repetition this exists to stop -- one line per inbound message,
        # forever -- is stopped either way.
        self._reported_reaction_failures: set[tuple[str, str]] = set()
        # Reactions, pins and membership changes a conversation asked to keep,
        # waiting for a turn that starts for its own reasons to fold them in.
        # In memory and lost on restart, deliberately -- see the class.
        self._inbound_events = InboundEventBuffer()
        # What kind of conversation each chat id is, in Slack's own word, as
        # learned from the payloads that state one. Keyed by conversation id.
        #
        # **Slack's word, never this connector's.** Only one of the four words
        # in ``SLACK_CHAT_TYPES`` is ever written here, off a payload that
        # carried it; nothing is derived and nothing is inferred, so a read of
        # this map is a read of something Slack said about that conversation.
        #
        # Written on the message path, read on the event path, and that is the
        # whole point of it: a ``message`` carries ``channel_type`` and a
        # ``reaction_added`` does not, so without somewhere to keep it the kind
        # is simply lost between the two. See ``_conversation_chat_type``.
        #
        # In memory and lost on restart, like the event buffer above, and with
        # the same consequence stated rather than hidden: the first event in a
        # conversation after a restart may find no entry, and an unknown kind
        # stays unknown. That costs a scope rule naming a kind one event, and
        # the alternative -- guessing, or paying ``conversations.info`` per
        # event -- costs either correctness or the property that recording an
        # event is free.
        #
        # Bounded by the conversations this workspace's bot is a member of and
        # actually receives traffic in. One short string per conversation, so
        # there is nothing here to evict.
        self._chat_types: dict[str, str] = {}

    @property
    def channel_id(self) -> str:
        return self.name

    @property
    def app_id(self) -> str:
        """What ``ChannelManager`` indexes this instance under.

        Read by ``ChannelManager._resolve_app_id`` to build the ``ChannelKey``,
        and the same string is stamped onto every request this instance sends
        upstream, so a reply carries the workspace it belongs to back down.

        ``channel_id`` stays ``slack`` for every workspace. It is what the
        delivery targets, the avatar adapter registration, the cron target word
        and the runtime's own tools all name the platform by, and one connection
        per workspace is not a different platform. The Feishu ``apps`` loop
        draws the line in the same place, for the same reason -- distinct
        ``app_id``, shared ``channel_id: feishu`` -- while ``feishu_enterprise``
        takes a channel id of its own because it is a different platform
        variant rather than a second account on one.

        Falls back to ``"default"`` as Feishu's does, so a single-workspace
        deployment that named nothing keys where it always keyed.
        """
        return self.config.app_id or "default"

    def claims_message(self, msg: Any) -> bool:
        """Whether this workspace is the one an outbound message belongs to.

        Read by ``ChannelManager`` when more than one Slack instance is
        registered and the message did not name an ``app_id`` precisely enough
        to be routed by key -- a scheduled push, a heartbeat relay, anything
        rebuilt after the request behind it is gone.

        The comparison is between what Slack said this connection is, from
        ``auth.test``, and what the message says it came from, from its metadata
        or its session id. Nothing else would do: ``app_id`` is a name the
        gateway hands out and appears on neither of those, and the token that
        would identify the install is not on the message either.

        **Fails closed, on both halves.** An instance that has not learned its
        own team yet -- before ``auth.test`` answers, or after ``stop`` -- claims
        nothing, and a message naming no team is claimed by nobody. Both refuse
        to deliver rather than deliver into the wrong workspace, which is the
        one outcome worth avoiding here: a reply posted in a workspace it was
        not asked in is a disclosure, while an undelivered one is a message
        missing from a log somebody is reading.
        """
        mine = self._workspace_team_id
        if not mine:
            return False
        return slack_message_team_id(msg) == mine

    @property
    def clients(self) -> set[Any]:
        return set()

    def on_message(self, callback: Callable[[Message], None]) -> None:
        self._on_message_cb = callback

    async def start(self) -> None:
        if not SLACK_AVAILABLE:
            logger.error("Slack SDK not installed. Run: pip install slack-bolt")
            return
        if not self.config.enabled:
            logger.warning("SlackChannel is disabled (enabled=false)")
            return
        if not self.config.bot_token.strip():
            logger.error("SlackChannel missing bot_token")
            return
        if not self.config.app_token.strip():
            logger.error("SlackChannel missing app_token")
            return
        if self._running:
            logger.warning("SlackChannel is already running")
            return

        # Before the app is built, because the logger it returns is what the app
        # is built with: bolt copies a base logger's level, handlers and filters
        # onto every logger it creates, and hands the same one to the socket-mode
        # client. Constructed without one, bolt pins each of its loggers to the
        # root logger's level and to no handler at all, and everything it has to
        # say about an inbound envelope is discarded.
        sdk_logger = configure_sdk_logging(self.config.sdk_log_level)

        app = AsyncApp(token=self.config.bot_token.strip(), logger=sdk_logger)
        app.event("app_mention")(self._handle_app_mention)
        app.event("message")(self._handle_message_event)
        # The non-message events, one listener per type and all seven routed to
        # one handler. They are registered from the family table rather than
        # spelled out, so a family cannot come to cover one direction of its
        # state change and not the other: the table is what says both belong to
        # it, and the listeners are read off the same table.
        #
        # None of these runs the inbound message path. No acknowledgement
        # reaction is posted, nothing is queued, and no turn starts: an event is
        # a state change rather than an utterance, and the two paths share
        # nothing beyond the outcome line they both write.
        for event_type in sorted(EVENT_TYPE_FAMILIES):
            app.event(event_type)(self._handle_inbound_event)
        # Registered after every named event listener, and the order is the
        # whole mechanism. Bolt walks its listener list and returns the first
        # response a matching listener produces, so a catch-all registered
        # ahead of a named handler would answer for it and the named handler
        # would never run. Anything added below this line is dead.
        app.event(_ANY_EVENT_TYPE)(self._handle_unclaimed_event)
        # Interaction payloads arrive on the same Socket Mode connection as the
        # events above and need no subscription and no Request URL of their own:
        # the handler forwards every envelope it receives to the app, which
        # routes a block_actions payload by the action_id its buttons were given.
        app.action(_QUESTION_ACTION_ID_RE)(self._handle_question_action)
        # A second listener, because a stop is a second thing. The answer
        # listener resolves a click against a question the session is paused on;
        # this one resolves a click against a turn that is still running, and
        # the two share no state and refuse for different reasons.
        app.action(_STOP_ACTION_ID)(self._handle_stop_action)

        handler = AsyncSocketModeHandler(app, self.config.app_token.strip())
        self._app = app
        self._handler = handler
        self._client = app.client
        await self._load_bot_user_id()
        # After auth.test, because that is the call whose headers carry the
        # granted scopes, and before the handler is started, because a channel
        # that cannot answer must not first accept a message it will then drop.
        # A refusal here leaves ``_running`` false and every other configured
        # channel untouched -- see ``_scopes_permit_start`` for why that and not
        # an exception.
        if not self._scopes_permit_start():
            return
        self._running = True
        self._log_approval_button_availability()
        self._log_stop_button_availability()

        try:
            await handler.start_async()
        except Exception as exc:  # noqa: BLE001
            logger.error("SlackChannel start failed: %s", exc, exc_info=True)
            raise
        finally:
            self._running = False

    async def stop(self) -> None:
        self._running = False
        # Whatever each stream last wrote stays on screen; there is no client
        # left to finish the edit with, and holding the state would only make a
        # restarted channel edit messages from a previous life.
        self._streams.clear()
        self._stream_next_update_at.clear()
        self._stream_recipients.clear()
        # A question whose buttons are still on screen is not answerable once
        # there is no client to route the answer with, and a restarted channel
        # must not answer a turn that no longer exists.
        self._pending_questions.clear()
        # A record is a promise to rewrite a message, and there is no client
        # left to keep it with. Every entry degrades to "no placeholder known",
        # which posts a new message -- and after a restart the scheduler will
        # not send one anyway: a run whose wake time has passed is skipped on
        # reload, so an orphaned placeholder is never superseded by anything.
        self._cron_records.clear()
        # A pending card is a message that has not been posted yet, and there is
        # no client left to post it with. Cancelling the timers rather than
        # letting them fire is what stops a stopped channel from writing into a
        # channel it no longer serves.
        for record in self._activity_records.values():
            if record.post_timer is not None:
                record.post_timer.cancel()
        self._activity_records.clear()
        self._activity_request_aliases.clear()
        # A stopped channel delivers nothing, so no turn it was tracking can
        # still reach anyone through it. Keeping the entries would make a claim
        # about which turns are live across a reconfiguration that ended them.
        self._turn_initiators.clear()
        # A held message is a promise to run it once the turn ahead of it ends.
        # That turn has ended by fiat here and there is no client left to answer
        # with, and running the backlog against a restarted channel would answer
        # messages hours after they were sent. Said out loud, because the sender
        # was told it would run.
        held = sum(len(queue) for queue in self._queued_messages.values())
        if held:
            logger.warning(
                "Slack dropped %s queued message(s) across %s session(s):"
                " the channel stopped before the turns they were waiting on"
                " ended",
                held,
                len(self._queued_messages),
            )
        self._queued_messages.clear()
        # A buffered event is context for the next turn, and a stopped channel
        # has no next turn. Keeping them would have a restarted channel open by
        # describing a room as it stood before the reconfiguration.
        self._inbound_events.forget()
        self._ack_only_requests.clear()
        handler = self._handler
        self._handler = None
        self._app = None
        self._client = None
        self._bot_user_id = ""
        self._bot_id = ""
        self._workspace_host = ""
        self._workspace_team_id = ""
        # Learned from a connection that no longer exists. A restart re-reads it
        # from its own auth.test, which is also how a reinstall with a different
        # manifest is noticed rather than assumed away.
        self._granted_scopes = None
        if handler is not None:
            try:
                await handler.close_async()
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "SlackChannel stop failed: %s", slack_failure_reason(exc)
                )
        logger.info("SlackChannel stopped")

    async def send(
        self, msg: Message, *, routing_target: RoutingTarget | None = None
    ) -> None:
        """Post a message to Slack, raising ``SlackDeliveryError`` if it does not land.

        Nothing-to-do cases return quietly: a token-level delta, one of the
        progress events a streamed turn reports alongside its answer, an event
        whose payload renders to no text, or -- in a conversation woken by every
        message -- a reply that is the silence sentinel and nothing else.
        Anything past that point is a delivery the caller expects to happen, so a
        failure is raised rather than logged and dropped.

        A delta is still not a delivery when streaming is on: it is folded into a
        message that this reply's terminal event goes on to rewrite in full, and
        that rewrite is the delivery the raise contract covers.

        A turn that died is one more nothing-to-do case, and only once the
        activity card has been rewritten to say so. The failure was delivered
        by that write; posting it again below the card that already holds it
        is a second copy, not a second delivery.

        A turn that says nothing takes its activity card with it, which is the
        opposite disposal for the opposite reason: nothing was delivered, so
        there is nothing for a card to front, and one left standing announces a
        turn that ran and declined to speak. The card is deleted rather than
        settled, in the same call that would have settled it. A card left
        holding nothing but the thinking section goes the same way and for the
        same cost, whatever the reply said -- see ``_close_activity_card``.
        """
        if msg.event_type == EventType.CHAT_DELTA:
            await self._stream_delta(msg, routing_target)
            return

        if msg.event_type == EventType.CHAT_FILE:
            await self._send_files(msg, routing_target)
            return

        if msg.event_type == EventType.CHAT_ASK_USER_QUESTION:
            await self._send_question(msg, routing_target)
            return

        # Not an answer and not progress: the runtime saying it took the input
        # and that this request will produce nothing further. Noted before the
        # ending that follows it, which is the whole point -- see
        # ``_note_ack_only_request``.
        if self._is_runtime_accepted(msg):
            self._note_ack_only_request(str(msg.id or "").strip())
            return

        # A turn that delegates, works through a long tool sequence or thinks
        # for minutes produces no delta at all, and the channel cannot tell it
        # from a hung one. The ordinary tool call, tool result, todo snapshot
        # and reasoning chunk every turn emits are read here rather than
        # relayed, Slack having nowhere to put a running commentary. None is
        # part of the answer, so consuming them takes nothing off the reply
        # path, and the reasoning chunk's payload is not opened even here.
        if await self._track_activity_event(msg, routing_target):
            return

        # The turn ending is what settles a card the last tool result did not:
        # a cancelled turn leaves runs with no result, and a card must never be
        # left claiming that a finished turn is still live. Not a return -- a
        # final still has its own delivery to make, and the card is written
        # first so it sits above the answer it fronted. An error that reached
        # the card is a different matter: it has been delivered, and posting it
        # again underneath would say the same thing twice.
        card_carried_error = False
        if msg.event_type in (EventType.CHAT_FINAL, EventType.CHAT_ERROR):
            session_id = str(msg.session_id or "")
            request_id = str(msg.id or "").strip()
            # Whose ending this is decides everything below it. A request that
            # was only acknowledged ends immediately and while the turn it was
            # injected into is still working, so reading it as "the turn is
            # over" would close a live turn's initiator entry and let a held
            # message out from behind a turn that has not finished.
            #
            # Only the turn bookkeeping is skipped. Whatever text the event
            # holds still takes the ordinary path below: an acknowledgement
            # holds none, and suppressing the delivery outright would be this
            # branch deciding what may reach a channel on a guess about who the
            # request was.
            if not self._is_ack_only_request(request_id):
                # Read before the entry is retired, because retiring it is what
                # drops the coordinates the mark is written at. Matched on the
                # request for the same reason ``_forget_turn_initiator``
                # matches: a second person's message cancels the first
                # person's turn and opens a new entry, and the cancelled turn's
                # ending must not mark the message that replaced it.
                ended = self._turn_initiators.get(session_id)
                if ended is not None and ended.request_id != request_id:
                    ended = None
                # The turn is over, so the session no longer has one to
                # attribute. Done here rather than beside the card because the
                # card is a display feature an operator can switch off, and
                # whether a turn is still running is not.
                self._forget_turn_initiator(session_id, request_id)
                # Read here, before the card is settled, because it is what
                # decides whether the card is settled at all. Everything it
                # consults is on the event or in the config -- see
                # ``_turn_says_nothing`` -- so asking early costs nothing and
                # asking late would mean rewriting a card in order to delete
                # it a moment afterwards.
                withheld = self._turn_says_nothing(msg, routing_target)
                card_carried_error = await self._close_activity_card(
                    msg, routing_target, withheld=withheld
                )
                # How the turn ended is decided here and nowhere else: this is
                # the terminal event, and a ``chat.error`` is the turn dying
                # after it started rather than a request that was never taken.
                # The refusals are settled on the inbound path, before anything
                # is dispatched, so no refused message ever reaches this branch
                # and the two states cannot be confused for one another.
                #
                # Ahead of the drain below, so that two reaction calls are not
                # queued behind dispatching somebody else's held message.
                if ended is not None:
                    await self._mark_turn_ended(
                        ended,
                        self.config.failed_emoji
                        if msg.event_type == EventType.CHAT_ERROR
                        else self.config.completed_emoji,
                    )
                    # Cleared unconditionally, on a turn that replied and one
                    # that did not alike: this is the terminal event, so the
                    # turn is over either way, and re-reading whether a reply
                    # was actually posted would duplicate the delivery
                    # decision the rest of ``send()`` has already made further
                    # down. Not gated on the acknowledge mode the mark above
                    # is -- see ``_clear_thinking_status``.
                    await self._clear_thinking_status(
                        ended.channel_id, ended.thread_ts
                    )
                # Read after the entry is closed, never before: the drain's
                # whole test is that the session has no turn left, and asking
                # while this turn's own entry still stood would answer "busy"
                # every time.
                await self._drain_queued_messages(session_id)

        # Progress reporting, which only a streamed turn produces. Gated on the
        # same setting that asks for the stream so that this cannot discard an
        # event that reached a non-streaming channel by some other route: with
        # streaming off the set is never consulted and every event is delivered
        # exactly as it was before streaming existed.
        if (
            self._streaming_enabled()
            and msg.event_type in _INTERMEDIATE_STREAM_EVENTS
        ):
            return

        content = self._extract_outgoing_text(msg)
        if not content:
            return

        # Read before anything is split or normalised, because the marker is
        # about the whole reply rather than any one chunk of it. A reply that was
        # nothing but a marker has now rendered to no text, which is one of the
        # nothing-to-do cases above and not a failed delivery.
        content, block_request = self._extract_block_request(content)
        if not content:
            return

        # One more nothing-to-do case, and the only one the model chose: a
        # conversation woken by every message whose turn wrote the sentinel
        # rather than an answer. Read here, beside the two emptiness checks,
        # because it means the same thing they do -- there is no message to post
        # -- and because the reply must be judged whole, before anything splits
        # or normalises it.
        if self._withhold_on_no_reply(msg, content, routing_target):
            await self._discard_streamed_preview(msg, routing_target)
            return

        # Checked after the no-op cases so a stopped channel does not raise for
        # deltas it would have discarded anyway.
        if self._client is None:
            raise SlackDeliveryError("Slack client is not connected")

        channel_id, thread_ts = self._require_delivery(msg, routing_target)

        # Empty unless this reply was streamed. In "edit" mode the first chunk
        # rewrites the message the deltas were rendered into instead of posting
        # a second copy of the same answer underneath it; in "stream" mode the
        # message cannot be rewritten at all, and the surface that owns it comes
        # back so the reply's tail can be appended and the stream stopped.
        stream_ts, streaming = await self._close_stream(msg, channel_id, thread_ts)

        # A scheduled run's messages are one record rather than two: the
        # placeholder is posted as a status card and the result rewrites it in
        # place. Resolved here because the channel it landed in is what the
        # record is keyed on, and only this connector knows it. ``None`` for
        # every message that is not a cron push, which is all of them.
        cron = self._cron_status_record(msg, channel_id, content)

        # The failure is on the card, so this message would be a second copy of
        # it. Withheld here rather than at the top of the function, past the two
        # things that are not a second copy: a streamed reply still open has to
        # be closed with something, and a scheduled run's placeholder has to be
        # superseded or it sits there claiming the run is live. Both write into a
        # message that already exists.
        if card_carried_error and not stream_ts and cron is None:
            logger.info(
                "[SlackChannel] turn failure delivered on the activity card: "
                "channel=%s",
                channel_id,
            )
            return

        # Not gated on enable_streaming: that flag is off by default because
        # "scheduled pushes have no reader waiting on them", which is exactly
        # why the record is worth keeping -- it is what the reader finds when
        # they come back.
        # A streamed message is not an editable one: chat.update against a
        # message still in streaming state is not documented to work, and the
        # answer is already on screen in the message rather than waiting to
        # replace a placeholder in it. It is finished below instead.
        root_update_ts = (
            ""
            if streaming is not None
            else stream_ts or (cron.anchor_ts if cron is not None else "")
        )
        # The first chunk is written with chat.update exactly when there is a
        # message to edit -- a streamed reply, or a placeholder this run posted
        # -- and an edit takes a tenth of what a post does. Sizing that chunk for
        # a post would fail the whole delivery for any answer over the edit
        # ceiling; the remaining chunks are fresh posts and keep the posting
        # limit, which stops a long answer becoming ten messages.
        first_limit = _MAX_SLACK_UPDATE_TEXT_LENGTH if root_update_ts else 0

        # Normalised before the empty ones are dropped, because a piece that was
        # only a marker or only whitespace renders to nothing here and would
        # otherwise be posted as a blank message.
        parts = [
            self._normalize_slack_mrkdwn(part)
            for part in self._split_threaded_report(content)
        ]
        parts = [part for part in parts if part]

        # The fork the surface abstraction does not hide: an edit takes 4,000
        # characters and a post 38,000, so both split the answer across messages,
        # while an append takes 12,000 of one message however long the answer
        # runs. The streamed reply is therefore finished here rather than chunked
        # below, falling through only when its message is unusable -- in which
        # case the whole answer is posted fresh, as for a stream that never
        # opened.
        if streaming is not None and await self._finish_streamed_reply(
            streaming,
            handle=stream_ts,
            channel_id=channel_id,
            thread_ts=thread_ts,
            content=content,
            block_request=block_request,
        ):
            return

        if len(parts) > 1 and not thread_ts:
            summary_chunks = self._split_text(parts[0], first_limit)
            if not summary_chunks:
                return
            # Each part is split on its own, so a part that is over the text
            # ceiling adds chunks after itself and never merges with the next
            # one. Blocks are decided per chunk downstream, which is what gives
            # each part its own Block Kit budget.
            detail_chunks = list(summary_chunks[1:])
            for part in parts[1:]:
                detail_chunks += self._split_text(part)
            total = 1 + len(detail_chunks)
            root_blocks, root_kind = self._root_blocks_and_kind_for(
                summary_chunks[0], block_request, cron
            )
            sent, report_thread_ts, error = await self._post_root(
                channel_id=channel_id,
                text=summary_chunks[0],
                thread_ts="",
                update_ts=root_update_ts,
                chunk_index=1,
                chunk_total=total,
                blocks=root_blocks,
                block_kind=root_kind,
                cron=cron,
            )
            if not sent:
                raise SlackDeliveryError(
                    error, channel_id=channel_id, chunks_sent=0, chunks_total=total
                )
            for index, chunk in enumerate(detail_chunks):
                chunk_blocks, chunk_kind = self._blocks_and_kind_for(
                    chunk, block_request
                )
                sent, _, error = await self._post_text(
                    channel_id=channel_id,
                    text=chunk,
                    thread_ts=report_thread_ts,
                    chunk_index=2 + index,
                    chunk_total=total,
                    blocks=chunk_blocks,
                    block_kind=chunk_kind,
                )
                if not sent:
                    raise SlackDeliveryError(
                        error,
                        channel_id=channel_id,
                        chunks_sent=1 + index,
                        chunks_total=total,
                    )
            return

        # Already in a thread, or nothing to thread: the parts are rejoined and
        # chunked as one reply, which is the same text the marker-free path has
        # always produced.
        flattened = "\n\n".join(parts)
        chunks = self._split_text(flattened, first_limit)
        if not chunks:
            return
        root_blocks, root_kind = self._root_blocks_and_kind_for(
            chunks[0], block_request, cron
        )
        sent, root_ts, error = await self._post_root(
            channel_id=channel_id,
            text=chunks[0],
            thread_ts=thread_ts,
            update_ts=root_update_ts,
            chunk_index=1,
            chunk_total=len(chunks),
            blocks=root_blocks,
            block_kind=root_kind,
            cron=cron,
        )
        if not sent:
            raise SlackDeliveryError(
                error,
                channel_id=channel_id,
                chunks_sent=0,
                chunks_total=len(chunks),
            )
        # A record is the run's anchor, so what does not fit in it hangs under
        # it rather than beside it: one entry in the channel per run, and the
        # card keeps the position the placeholder had. Every other reply keeps
        # posting its continuation exactly where it always did.
        continuation_ts = thread_ts
        if cron is not None and not thread_ts and root_ts:
            continuation_ts = root_ts
        for index, chunk in enumerate(chunks[1:], start=1):
            chunk_blocks, chunk_kind = self._blocks_and_kind_for(chunk, block_request)
            sent, _, error = await self._post_text(
                channel_id=channel_id,
                text=chunk,
                thread_ts=continuation_ts,
                chunk_index=index + 1,
                chunk_total=len(chunks),
                blocks=chunk_blocks,
                block_kind=chunk_kind,
            )
            if not sent:
                raise SlackDeliveryError(
                    error,
                    channel_id=channel_id,
                    chunks_sent=index,
                    chunks_total=len(chunks),
                )

    async def _send_files(
        self, msg: Message, routing_target: RoutingTarget | None
    ) -> None:
        """Upload the files held by a ``chat.file`` event.

        Holds to the same contract as the text path, because the same caller is
        watching: a payload with nothing in it is a no-op, and everything past
        that point is a delivery the agent was told had happened. A file that
        cannot be uploaded therefore raises rather than logging, so a scheduled
        job cannot report a report it never sent.
        """
        files = self._extract_outgoing_files(msg)
        if not files:
            logger.warning("Slack chat.file event carried no usable file entries")
            return

        if self._client is None:
            raise SlackDeliveryError("Slack client is not connected")

        channel_id, thread_ts = self._require_delivery(msg, routing_target)

        total = len(files)
        for index, (path, filename) in enumerate(files):
            # Checked here rather than in the extractor so a missing file is
            # reported with the same counters as a failed upload: both leave the
            # user with part of what the agent said it delivered.
            if not Path(path).is_file():
                raise SlackDeliveryError(
                    f"file no longer exists: {path}",
                    channel_id=channel_id,
                    chunks_sent=index,
                    chunks_total=total,
                    unit="files",
                )
            sent, error = await self._upload_file(
                channel_id=channel_id,
                thread_ts=thread_ts,
                path=path,
                filename=filename,
                file_index=index + 1,
                file_total=total,
            )
            if not sent:
                raise SlackDeliveryError(
                    error,
                    channel_id=channel_id,
                    chunks_sent=index,
                    chunks_total=total,
                    unit="files",
                )

    async def _send_question(
        self, msg: Message, routing_target: RoutingTarget | None
    ) -> None:
        """Post a ``chat.ask_user_question`` as a message with answer buttons.

        The turn that emitted this event is paused until it is answered, so the
        post is a delivery in the strongest sense the raise contract has: a
        failure here is not a message the user missed, it is a turn that can
        never continue. It therefore raises exactly as the text and file paths
        do.

        An ask_user question with no options or inputs uses one Slack text
        input. Other questions with nothing to render receive a notice: their
        missing controls do not imply that a typed answer is valid.

        That notice is best effort and does not raise. The turn is not waiting
        on it, and the failure it reports is a capability this channel does not
        have rather than a delivery that went wrong -- reporting it through the
        delivery-error path would name the wrong cause every time it happened.
        """
        parsed = self._extract_question(msg)
        if parsed is None:
            logger.warning(
                "Slack ask_user_question carried no answerable question: id=%s",
                msg.id,
            )
            return
        request_id, source, question, dropped = parsed
        if dropped:
            # The log names the questions that were dropped. The line this
            # replaced gave only their number, and a number cannot be matched
            # against what the model reported it had asked, so the first report
            # of this fault took an afternoon to place.
            logger.warning(
                "Slack posts question 1 of %d and drops the others: "
                "request_id=%s source=%s dropped=%s",
                len(dropped) + 1,
                request_id,
                source or "-",
                " | ".join(entry or "(no prompt)" for entry in dropped),
            )

        # An input question is answered with values a person entered rather than
        # with an option they pressed, and the two are alternatives: the inputs
        # are the answer, so a row of option buttons beside them would offer a
        # second, contradictory way to finish. A question declaring both is a
        # question built wrong, and the inputs win because they are the more
        # specific statement of what is wanted.
        try:
            inputs = slack_inputs.parse_inputs(question)
        except slack_inputs.InputRenderError as exc:
            logger.warning(
                "Slack cannot render the inputs a question declared: "
                "request_id=%s source=%s error=%s",
                request_id,
                source,
                exc,
            )
            await self._post_unanswerable_notice(
                msg, routing_target, request_id, source, question
            )
            return

        # A missing choice list means free text only for ask_user. An "Other"
        # choice is not a request for a standalone text answer.
        if (
            source == "ask_user_interrupt"
            and not inputs
            and question.get("options", []) == []
            and question.get("inputs") in (None, [])
        ):
            inputs = slack_inputs.parse_inputs(
                {"inputs": [{"type": "text", "label": "Answer", "multiline": True}]}
            )

        options = [] if inputs else self._question_options(question)
        if inputs and isinstance(question.get("options"), list) and question["options"]:
            logger.warning(
                "Slack question declares both inputs and options; the options "
                "are not rendered: request_id=%s",
                request_id,
            )
        if not inputs and not options:
            logger.warning(
                "Slack cannot render a free-form question, which needs a typed "
                "answer this channel cannot route: request_id=%s source=%s",
                request_id,
                source,
            )
            await self._post_unanswerable_notice(
                msg, routing_target, request_id, source, question
            )
            return

        # Checked after the nothing-to-render cases, matching send(): a stopped
        # channel must not raise for a question it would not have posted anyway.
        if self._client is None:
            raise SlackDeliveryError("Slack client is not connected")

        channel_id, thread_ts = self._require_delivery(msg, routing_target)

        session_id = str(msg.session_id or "")
        if inputs:
            blocks = self._question_input_blocks(
                request_id=request_id,
                source=source,
                session_id=session_id,
                question=question,
                inputs=inputs,
            )
        else:
            blocks = self._question_blocks(
                request_id=request_id,
                source=source,
                session_id=session_id,
                question=question,
                options=options,
            )
        if blocks is None:
            raise SlackDeliveryError(
                f"question {request_id} could not be rendered as Block Kit",
                channel_id=channel_id,
            )

        if dropped:
            # The note goes on the message, where the person answering it will
            # read it. That person is about to be asked the same thing a second
            # time, once the model learns what did not arrive. One line here
            # explains the repeat before it happens.
            #
            # The block budget is checked a second time. The builders above
            # check it against what they built, and this block is added after
            # they return. A question already at the ceiling keeps its fields
            # and loses the note, and the log then says that the note was lost.
            # A note dropped in silence is the fault this change is about.
            if len(blocks) < _MAX_QUESTION_BLOCKS:
                blocks.append(self._dropped_questions_block(dropped))
            else:
                logger.warning(
                    "Slack has no room to name the dropped questions on the "
                    "message, which already holds %d blocks: request_id=%s",
                    len(blocks),
                    request_id,
                )

        notification_text = self._question_fallback_text(question)
        sent, message_ts, error = await self._post_text(
            channel_id=channel_id,
            text=notification_text,
            thread_ts=thread_ts,
            blocks=blocks,
            block_kind=slack_blocks.BLOCK_KIND_INTERACTIVE,
            block_text_route=_question_text_route(source),
            chunk_index=1,
            chunk_total=1,
        )
        if not sent:
            raise SlackDeliveryError(error, channel_id=channel_id)

        # Read while the paused turn's entry is still open. The question is
        # emitted before its turn's terminal event, so this is the last point at
        # which the person who asked for the work is knowable from the session
        # alone; by the time a button is pressed the pause has closed it.
        initiator = self.turn_initiator(session_id)
        self._remember_question(
            _SlackPendingQuestion(
                request_id=request_id,
                session_id=session_id,
                source=source,
                question=str(question.get("question") or "").strip(),
                labels=[option.label for option in options],
                values=[option.value for option in options],
                inputs=inputs,
                channel_id=channel_id,
                message_ts=message_ts,
                notification_text=notification_text,
                turn_request_id=str(msg.id or "").strip(),
                initiator_user_id=initiator.user_id if initiator else "",
                initiator_is_dm=initiator.is_dm if initiator else None,
                initiator_chat_type=initiator.chat_type if initiator else "",
                initiator_channel_id=initiator.channel_id if initiator else "",
                initiator_message_ts=initiator.message_ts if initiator else "",
                initiator_thread_ts=initiator.thread_ts if initiator else "",
                initiator_silence_is_honoured=(
                    initiator.silence_is_honoured if initiator else False
                ),
                initiator_addressed=(initiator.addressed if initiator else False),
                initiator_post_as_root=(
                    initiator.post_as_root if initiator else False
                ),
                initiator_trigger=initiator.trigger if initiator else "",
            )
        )
        if inputs:
            logger.info(
                "[SlackChannel] delivered question: channel=%s ts=%s request_id=%s "
                "source=%s inputs=%s",
                channel_id,
                message_ts or "-",
                request_id,
                source or "-",
                ",".join(f"{entry.name}:{entry.spec.name}" for entry in inputs),
            )
            return
        logger.info(
            "[SlackChannel] delivered question: channel=%s ts=%s request_id=%s "
            "source=%s options=%d",
            channel_id,
            message_ts or "-",
            request_id,
            source or "-",
            len(options),
        )

    @staticmethod
    def _extract_question(
        msg: Message,
    ) -> tuple[str, str, dict[str, Any], list[str]] | None:
        """Return ``(request_id, source, question, dropped)`` from an ask-user payload.

        ``None`` when the payload is not one this connector can answer: without
        a ``request_id`` there is nothing to answer, and without a question
        there is nothing to ask.

        Only the first question is taken, as the Feishu card does. A payload
        holding several would need every one of them answered in the same
        response, and a message per question cannot produce that. An option
        question is answered by the click on the button that renders it, so a
        second row of buttons in the same message answers the request a second
        time and the first answer wins.

        ``dropped`` names the questions that are not posted, one prompt per
        entry. It is empty for the ordinary single-question payload. It is
        returned rather than logged here because three readers need it. The log
        line below serves the operator. The note on the posted message serves
        the person answering. The ask_user rail compares the answer against the
        call and serves the model, which is the only reader that can send the
        missing questions a second time.
        """
        payload = getattr(msg, "payload", None)
        if not isinstance(payload, dict):
            return None
        request_id = str(payload.get("request_id") or "").strip()
        questions = payload.get("questions")
        if not request_id or not isinstance(questions, list) or not questions:
            return None
        question = questions[0]
        if not isinstance(question, Mapping):
            return None
        # One entry per question beyond the first, including any that is not an
        # object and any that has no prompt. Both are still questions the
        # model sent and this message does not hold, and leaving them out would
        # make the count on the message and in the log disagree with the call.
        dropped = [
            SlackChannel._question_prompt_text(entry)
            if isinstance(entry, Mapping)
            else ""
            for entry in questions[1:]
        ]
        return (
            request_id,
            str(payload.get("source") or "").strip(),
            dict(question),
            dropped,
        )

    @staticmethod
    def _question_prompt_text(question: Mapping[str, Any]) -> str:
        """The prompt of one question, in the order every channel resolves it.

        ``question`` first, then ``header``. The call's top-level ``query`` is
        the third source elsewhere and is deliberately not read here: this names
        a question in a list of several, and a query shared by all of them names
        none of them apart.
        """
        for candidate in (question.get("question"), question.get("header")):
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()
        return ""

    @staticmethod
    def _question_options(
        question: Mapping[str, Any],
    ) -> list[_QuestionOption]:
        """Return a ``_QuestionOption`` per option worth a button.

        The fields are documented on the structure itself. What is decided here
        rather than there is which entries earn one at all, and that ``style``
        is resolved at this point: this is the last place the option is still
        whole, because the intent it may state is read off the entry and only
        the answer value survives into the button.

        An option kept alongside its description rather than looked up
        separately keeps the descriptions shown exactly the options offered: an
        entry dropped below keeps neither a button nor a line describing one
        that is not there.

        The free-form option is dropped. It exists so a rich client can offer a
        text box beside the choices, and pressing it as a button would answer
        with a label the agent resolves to nothing at all.
        """
        raw = question.get("options")
        if not isinstance(raw, list):
            return []

        options: list[_QuestionOption] = []
        for entry in raw:
            if not isinstance(entry, Mapping):
                continue
            value = str(entry.get("value") or "").strip()
            label = str(entry.get("label") or "").strip() or value
            if not label:
                continue
            if not value:
                if label == _FREE_FORM_OPTION_LABEL:
                    continue
                value = label
            description = str(entry.get("description") or "").strip()
            intent = str(entry.get("intent") or "").strip()
            options.append(
                _QuestionOption(
                    label=label,
                    value=value,
                    description=description,
                    style=_option_button_style(intent, value),
                )
            )
        return options

    def _question_blocks(
        self,
        *,
        request_id: str,
        source: str,
        session_id: str,
        question: Mapping[str, Any],
        options: list[_QuestionOption],
    ) -> list[dict[str, Any]] | None:
        """Render the question and its options as Block Kit.

        ``None`` when a button could not be given a usable value, which is the
        one failure that cannot be clamped away: a button the click cannot be
        traced back to would answer nothing, and posting the rest of the
        question without it would silently drop one of the user's choices.
        """
        blocks: list[dict[str, Any]] = self._question_prompt_blocks(question)

        legend = self._question_options_legend(options)
        if legend:
            blocks.append(
                {
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": legend},
                }
            )

        elements: list[dict[str, Any]] = []
        for index, option in enumerate(options):
            encoded = self._encode_button_value(
                request_id=request_id,
                index=index,
                value=option.value,
                source=source,
                session_id=session_id,
            )
            if not encoded:
                logger.warning(
                    "Slack question option %d has no value that fits in %d "
                    "characters: request_id=%s",
                    index,
                    _MAX_BUTTON_VALUE_LENGTH,
                    request_id,
                )
                return None
            element: dict[str, Any] = {
                "type": "button",
                "action_id": f"{_QUESTION_ACTION_ID_PREFIX}{index}",
                "text": {
                    "type": "plain_text",
                    "text": _clamp(option.label, _MAX_BUTTON_TEXT_LENGTH),
                    "emoji": True,
                },
                "value": encoded,
            }
            # Omitted rather than sent empty: Slack validates the field against
            # its enum and refuses the whole message for a value outside it.
            if option.style:
                element["style"] = option.style
            elements.append(element)

        for start in range(0, len(elements), _MAX_ACTIONS_ELEMENTS):
            blocks.append(
                {
                    "type": "actions",
                    "elements": elements[start : start + _MAX_ACTIONS_ELEMENTS],
                }
            )
        return blocks

    def _question_input_blocks(
        self,
        *,
        request_id: str,
        source: str,
        session_id: str,
        question: Mapping[str, Any],
        inputs: list[slack_inputs.QuestionInput],
    ) -> list[dict[str, Any]] | None:
        """Render a question answered with entered values, plus its submit.

        The prompt is rendered exactly as it is for an option question, so the
        two read alike; a ``divider`` separates it from the fields, which is the
        one thing a message of stacked input blocks needs to stop looking like
        one continuous form.

        The submit button is what makes any of this answerable. A picker, a
        select and a text field all report every change as it happens and none
        of them has a commit point, so without a button there is no moment at
        which the person has finished and no event that means "read this now".
        It holds the same encoded identity an option button does, for the same
        reason: the click has to say which question it belongs to.

        ``None`` when that identity does not fit, matching the option path --
        the only failure that cannot be clamped away, because a submit the click
        cannot be traced back to answers nothing.
        """
        blocks: list[dict[str, Any]] = self._question_prompt_blocks(question)
        blocks.append(slack_inputs.divider_block())
        try:
            blocks.extend(
                slack_inputs.render_input_blocks(
                    inputs, action_id_prefix=_QUESTION_INPUT_ACTION_ID_PREFIX
                )
            )
        except slack_inputs.InputRenderError as exc:
            logger.warning(
                "Slack could not render a question's inputs: request_id=%s "
                "error=%s",
                request_id,
                exc,
            )
            return None

        note = str(question.get("note") or "").strip()
        if note:
            blocks.append(
                slack_inputs.context_block(
                    _clamp(self._normalize_slack_mrkdwn(note), _MAX_SECTION_TEXT_LENGTH)
                )
            )

        encoded = self._encode_button_value(
            request_id=request_id,
            # Not an option's position: an input question has no options, and a
            # negative index is what tells the shared resolver that the record's
            # option list is not where this answer comes from.
            index=-1,
            value="",
            source=source,
            session_id=session_id,
        )
        if not encoded:
            logger.warning(
                "Slack question submit has no value that fits in %d characters: "
                "request_id=%s",
                _MAX_BUTTON_VALUE_LENGTH,
                request_id,
            )
            return None
        label = (
            str(question.get("submit_label") or "").strip() or _DEFAULT_SUBMIT_LABEL
        )
        blocks.append(
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "action_id": _QUESTION_SUBMIT_ACTION_ID,
                        "style": "primary",
                        "text": {
                            "type": "plain_text",
                            "text": _clamp(label, _MAX_BUTTON_TEXT_LENGTH),
                            "emoji": True,
                        },
                        "value": encoded,
                    }
                ],
            }
        )
        if len(blocks) > _MAX_QUESTION_BLOCKS:
            logger.warning(
                "Slack question renders %d blocks, over the %d a message holds: "
                "request_id=%s",
                len(blocks),
                _MAX_QUESTION_BLOCKS,
                request_id,
            )
            return None
        return blocks

    @classmethod
    def _question_prompt_blocks(
        cls, question: Mapping[str, Any]
    ) -> list[dict[str, Any]]:
        """Render the question itself, as one section or as a heading and one.

        One ``section`` by default, which is what every question posted before
        this existed rendered as and what they must keep rendering as.

        ``header_block`` opts into Slack's ``header`` block instead: larger and
        bolder, and worth having on a question that asks for several things.
        Opt-in rather than inferred, because a ``header`` block is
        ``plain_text`` only -- markup reaches it as literal asterisks -- and is
        clamped to 150 characters against the section's 3,000.
        """
        if not question.get("header_block"):
            return [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": cls._question_section_text(question),
                    },
                }
            ]
        header = str(question.get("header") or "").strip()
        body = cls._normalize_slack_mrkdwn(
            str(question.get("question") or "").strip()
        )
        blocks: list[dict[str, Any]] = []
        if header:
            blocks.append(slack_inputs.header_block(header))
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": _clamp(body or "…", _MAX_SECTION_TEXT_LENGTH),
                },
            }
        )
        return blocks

    @classmethod
    def _dropped_questions_block(cls, dropped: list[str]) -> dict[str, Any]:
        """Name, on the message, the questions this message does not hold.

        A ``context`` block. It is the smallest thing Slack renders, and it
        reads as an aside instead of as a second question. The wording states
        only what the person can see: this message holds one question, and the
        request asked for more. It makes no claim about what the model does
        next, because nothing here can promise that.

        A question with no prompt of its own is counted and not named. There is
        no text to quote, and an empty pair of quotation marks would read as a
        question that asked nothing.
        """
        named = [prompt for prompt in dropped if prompt]
        quoted = ", ".join(f'"{cls._normalize_slack_mrkdwn(p)}"' for p in named)
        count = len(dropped)
        tail = (
            f"{count} more question that is not on this message"
            if count == 1
            else f"{count} more questions that are not on this message"
        )
        body = f"Slack shows one question at a time. This request also asked {tail}"
        body = f"{body}: {quoted}." if quoted else f"{body}."
        return {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": _clamp(body, _MAX_SECTION_TEXT_LENGTH),
                }
            ],
        }

    @classmethod
    def _question_options_legend(
        cls, options: list[_QuestionOption]
    ) -> str:
        """Render the options' descriptions as mrkdwn, or "" when there are none.

        A button shows only its label, and the four an approval offers read
        almost alike: what separates allowing a tool once from writing a rule to
        disk for every session afterwards is stated in the description alone.
        Block Kit has no way to hang that off a button, so it is rendered beside
        them.

        Its own block rather than an addition to the question's, so that the two
        do not compete for one section's budget. Appending here would spend the
        question's remaining characters on the legend and clamp a long question
        sooner than it is clamped today.

        Options without a description are left out rather than emitted as a bare
        label, which would read as a choice whose effect was left blank. An empty
        result means no option had one, and the caller drops the block. No
        heading or labelling is added: the descriptions arrive in the language
        the question was built in, and nothing here should be in another.
        """
        lines: list[str] = []
        for option in options:
            if not option.description:
                continue
            shown = _clamp(option.description, _MAX_OPTION_DESCRIPTION_LENGTH)
            lines.append(
                f"*{cls._normalize_slack_mrkdwn(option.label)}* — "
                f"{cls._normalize_slack_mrkdwn(shown)}"
            )
        if not lines:
            return ""
        return _clamp("\n".join(lines), _MAX_SECTION_TEXT_LENGTH)

    @classmethod
    def _question_section_text(cls, question: Mapping[str, Any]) -> str:
        """Render the question's header and body as one mrkdwn section.

        The body goes through the same Markdown conversion an ordinary reply
        does: a permission prompt names the tool in backticks and bolds the
        arguments, and left unconverted that reaches Slack as literal asterisks.

        A body identical to the header is shown once. A question that declares
        `inputs` need not hold a sentence of its own -- its fields are labelled
        and the header names the group -- so its prompt is derived from the
        header, and printing the derivation beneath its source says the same
        thing twice.
        """
        header = str(question.get("header") or "").strip()
        raw_body = str(question.get("question") or "").strip()
        if header and raw_body == header:
            raw_body = ""
        body = cls._normalize_slack_mrkdwn(raw_body)
        if header:
            body = f"*{cls._normalize_slack_mrkdwn(header)}*\n{body}".strip()
        return _clamp(body or "…", _MAX_SECTION_TEXT_LENGTH)

    def _log_approval_button_availability(self) -> None:
        """Say at startup whether an approval could reach this channel at all.

        The buttons have no setting of their own. A question is only emitted on
        a streamed turn, so ``enable_streaming`` decides whether an approval can
        ever be shown -- a coupling that is invisible in the config file, where
        the two features do not appear related. Without this line the first sign
        is a task that paused for an approval nobody was asked for.
        """
        if self._streaming_enabled():
            logger.info("[SlackChannel] approval buttons active")
            return
        logger.warning(
            "[SlackChannel] approval buttons inactive: an approval is only "
            "delivered on a streamed turn and channels.slack.enable_streaming "
            "is false, so a task that asks for one pauses with nothing shown "
            "in Slack"
        )

    def _log_stop_button_availability(self) -> None:
        """Say at startup whether a turn in this channel can be stopped at all.

        The same invisible coupling the line above exists for, on a different
        setting. The stop button has no switch of its own and rides on the turn
        activity card, so ``activity_card`` decides whether Slack has any
        gesture that means only "stop". With it off the channel keeps a way to
        halt a turn -- posting a message still cancels it -- but has none that
        can be refused.
        """
        if self.config.activity_card:
            logger.info("[SlackChannel] stop button active")
            return
        logger.warning(
            "[SlackChannel] stop button inactive: it is rendered on the turn "
            "activity card and channels.slack.activity_card is false, so the "
            "only way to stop a running turn is to post a message, which "
            "cancels it without checking who sent it"
        )

    async def _post_unanswerable_notice(
        self,
        msg: Message,
        routing_target: RoutingTarget | None,
        request_id: str,
        source: str,
        question: Mapping[str, Any],
    ) -> None:
        """Tell the channel a question arrived that it cannot answer.

        Best effort throughout: every failure here is logged and swallowed,
        because this is already the path where something could not be done, and
        a notice that fails to post must not turn into a second, less accurate
        error about the question itself.
        """
        if self._client is None:
            return
        channel_id, thread_ts = self._extract_delivery(msg, routing_target)
        if not channel_id:
            logger.warning(
                "Slack could not report an unanswerable question, no target "
                "channel resolved: request_id=%s source=%s",
                request_id,
                source,
            )
            return

        text = (
            f"{self._question_fallback_text(question)}\n\n"
            "_This question needs a typed answer, which this channel cannot "
            "route back to the waiting task. The task stays paused until it is "
            "answered elsewhere or times out._"
        )
        try:
            sent, _, error = await self._post_text(
                channel_id=channel_id,
                text=text,
                thread_ts=thread_ts,
                chunk_index=1,
                chunk_total=1,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Slack failed to report an unanswerable question: "
                "request_id=%s source=%s error=%s",
                request_id,
                source,
                exc,
            )
            return
        if not sent:
            logger.warning(
                "Slack failed to report an unanswerable question: "
                "request_id=%s source=%s error=%s",
                request_id,
                source,
                error,
            )

    @staticmethod
    def _question_fallback_text(question: Mapping[str, Any]) -> str:
        """Plain text for the notification and for screen readers.

        Slack shows this and nothing else on mobile push, so it states the
        question rather than a generic label.

        A body identical to the header is shown once, for the same reason the
        section is: a question that declares `inputs` derives its prompt from
        the header, and "Follow-up: Follow-up" is not a notification.
        """
        header = str(question.get("header") or "").strip()
        body = str(question.get("question") or "").strip()
        if body == header:
            body = ""
        joined = f"{header}: {body}" if header and body else (body or header)
        return _clamp(joined or "A question is waiting for an answer", 500)

    @staticmethod
    def _encode_button_value(
        *,
        request_id: str,
        index: int,
        value: str,
        source: str,
        session_id: str,
    ) -> str:
        """JSON-encode one button's value, or "" when it cannot be made to fit.

        Slack caps this field at 2000 characters and rejects the whole message
        when one button exceeds it, so the encoding sheds fields until it fits.
        ``request_id`` and ``index`` are never shed: together they identify the
        pending-question record every other field is recoverable from. The rest
        ride along so a click arriving after that record is gone still holds
        enough to be reported.
        """
        fields: dict[str, Any] = {
            "request_id": request_id,
            "index": index,
            "value": value,
            "source": source,
            "session_id": session_id,
        }
        for shed in ((), ("value",), ("value", "session_id"), ("value", "session_id", "source")):
            candidate = {key: item for key, item in fields.items() if key not in shed}
            encoded = json.dumps(candidate, separators=(",", ":"), ensure_ascii=False)
            if len(encoded) <= _MAX_BUTTON_VALUE_LENGTH:
                return encoded
        return ""

    def _remember_question(self, pending: _SlackPendingQuestion) -> None:
        """Record a posted question, pruning the map first.

        Bounded and aged like the stream map, and for the same reason: a turn
        that is cancelled, or answered from another client, never comes back to
        clear its entry. Dropping one costs a click its full-fidelity answer,
        not the ability to answer.
        """
        _prune_bounded_map(
            self._pending_questions,
            cap=_MAX_PENDING_QUESTIONS,
            timeout=_PENDING_QUESTION_TIMEOUT_SECONDS,
            timestamp=lambda entry: entry.touched_at,
            what="unanswered questions",
        )
        self._pending_questions[pending.request_id] = pending

    async def _withdraw_questions_for_session(self, session_id: str) -> None:
        """Retire the questions an ordinary message into this session voids.

        An interrupt is withdrawn upstream as soon as a resume input arrives
        that is not an answer to it: a plain message is consumed by the same
        suspend the interrupt raised, so the round continues as conversation with
        the interrupted call closed off as declined. The buttons are then
        attached to nothing, and a click on them is worse than inert -- the
        answer travels as a chat.send into a session with nothing paused, where
        the option's label reads as something the user typed.

        Nothing says so on the wire: the withdrawal writes a tool result into
        the model's context and a warning into the log, and no event type holds
        a question's request id back once it has been asked. What the connector
        has instead is the input that causes the withdrawal, because it is the
        connector that forwards it -- a message about to be routed into the
        session that asked is exactly the input the interrupt cannot survive.

        Only the interrupt sources are retired. The rest are standalone
        approvals, answered through chat.user_answer against a turn that has
        already finished, and an ordinary message does not disturb them.

        The entry is marked rather than dropped so that a click arriving later
        -- against a rewrite that failed, or a copy of the message a client
        still has -- is recognised as stale instead of looking like a question
        this process never posted.

        Observed in production as an approval clicked several minutes after the
        message that withdrew it, and read by the model as something the user
        had just typed.
        """
        if not session_id:
            return
        withdrawn = [
            entry
            for entry in self._pending_questions.values()
            if entry.session_id == session_id
            and entry.withdrawn_at is None
            and entry.source in _INTERRUPT_RESUME_SOURCES
        ]
        for entry in withdrawn:
            entry.withdrawn_at = time.monotonic()
            logger.info(
                "[SlackChannel] question withdrawn by an ordinary message in the "
                "same session: request_id=%s source=%s",
                entry.request_id,
                entry.source or "-",
            )
            await self._strip_question_buttons(entry)

    async def _strip_question_buttons(self, pending: _SlackPendingQuestion) -> None:
        """Rewrite a withdrawn question so it stops inviting an answer.

        The blocks are replaced with an empty list, which is how chat.update
        removes the ones a message already has; the question itself survives as
        the message text, followed by a line saying it is no longer waiting, so
        the channel still records what was asked and why nothing came of it.

        Best effort, like every other rewrite here. Nothing is waiting on this
        edit -- the turn it belonged to has already carried on -- and a failure
        costs the channel a set of buttons that no longer answer anything, which
        the click path refuses on its own.
        """
        if self._client is None or not pending.channel_id or not pending.message_ts:
            return
        text = _clamp(
            f"{pending.notification_text}\n\n{_WITHDRAWN_QUESTION_NOTICE}".strip(),
            _MAX_SLACK_TEXT_LENGTH,
        )
        try:
            sent, _, error = await self._post_text(
                channel_id=pending.channel_id,
                text=text,
                thread_ts="",
                update_ts=pending.message_ts,
                blocks=[],
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Slack could not withdraw the buttons of a question the session "
                "moved past: request_id=%s error=%s",
                pending.request_id,
                exc,
            )
            return
        if not sent:
            logger.warning(
                "Slack could not withdraw the buttons of a question the session "
                "moved past: request_id=%s error=%s",
                pending.request_id,
                error,
            )

    @staticmethod
    def _extract_outgoing_files(msg: Message) -> list[tuple[str, str]]:
        """Return ``(path, filename)`` for each file on a ``chat.file`` payload.

        Entries arrive either as a mapping with ``path``/``name`` or as a bare
        path string, which is what the other connectors accept. Anything without
        a path is skipped: it cannot be uploaded and cannot be reported usefully.
        """
        payload = getattr(msg, "payload", None)
        raw = payload.get("files") if isinstance(payload, dict) else None
        if not isinstance(raw, list):
            return []

        files: list[tuple[str, str]] = []
        for entry in raw:
            if isinstance(entry, Mapping):
                path = str(entry.get("path") or "").strip()
                filename = str(entry.get("name") or "").strip()
            else:
                path = str(entry or "").strip()
                filename = ""
            if not path:
                continue
            files.append((path, filename or Path(path).name))
        return files

    async def _upload_file(
        self,
        *,
        channel_id: str,
        thread_ts: str,
        path: str,
        filename: str,
        file_index: int = 0,
        file_total: int = 0,
    ) -> tuple[bool, str]:
        """Upload one file. Returns ``(sent, error)``.

        ``files_upload_v2`` is the external-upload flow (getUploadURLExternal
        then completeUploadExternal) behind one call, and needs ``files:write``.
        Errors come back rather than being raised so the caller can attach the
        progress counters, matching how ``_post_text`` reports a failed chunk.

        ``file_index`` / ``file_total`` mirror the chunk counters ``_post_text``
        takes, so one upload out of a batch is identifiable in the log by the
        same counters the failure path reports.
        """
        kwargs: dict[str, Any] = {
            "channel": channel_id,
            "file": path,
            "filename": filename,
        }
        if thread_ts:
            kwargs["thread_ts"] = thread_ts

        try:
            size_bytes = Path(path).stat().st_size
        except OSError:
            # Only the log line loses a field; a file that cannot be stat'd is
            # still worth attempting, and the upload reports its own failure.
            size_bytes = -1

        for attempt in range(_MAX_RATE_LIMIT_RETRIES + 1):
            try:
                response = await self._client.files_upload_v2(**kwargs)
            except Exception as exc:  # noqa: BLE001
                delay = retry_after_seconds(exc)
                if delay is not None and attempt < _MAX_RATE_LIMIT_RETRIES:
                    logger.warning(
                        "Slack rate limited on upload; retrying in %.1fs (%d/%d)",
                        delay,
                        attempt + 1,
                        _MAX_RATE_LIMIT_RETRIES,
                    )
                    await asyncio.sleep(delay)
                    continue
                reason = slack_failure_reason(exc)
                logger.warning("SlackChannel file upload failed: %s", reason)
                return False, reason
            logger.info(
                "[SlackChannel] delivered file: channel=%s filename=%s bytes=%d "
                "file_id=%s file=%d/%d",
                channel_id,
                filename,
                size_bytes,
                self._uploaded_file_id(response) or "-",
                file_index,
                file_total,
            )
            return True, ""

        # Unreachable, as in _post_text: the loop either returns or falls into
        # the terminal branch. Present so there is no implicit None path.
        return False, "rate limited"

    @staticmethod
    def _uploaded_file_id(response: Any) -> str:
        """Return the id Slack assigned an upload, or "" when it is not there.

        ``files_upload_v2`` reports the completed upload under ``files``; older
        payloads hold a single ``file``. Neither is contractual enough to index
        blindly from a log line, so every miss degrades to an empty id rather
        than costing the caller a successful upload.
        """
        if response is None:
            return ""
        try:
            entries = response.get("files")
            entry = entries[0] if isinstance(entries, list) and entries else None
            if entry is None:
                entry = response.get("file")
            if not isinstance(entry, Mapping):
                return ""
            return str(entry.get("id") or "").strip()
        except (AttributeError, TypeError, IndexError, KeyError):
            return ""

    async def _refused_blocks(
        self, blocks: list[dict[str, Any]] | None, block_kind: str
    ) -> tuple[str, list[Mapping[str, Any]]]:
        """Slack's verdict on *blocks*: ``(code, complaints)``.

        An empty complaint list is "send it". The code beside it is the
        top-level ``error`` Slack answered with -- ``invalid_blocks`` and
        nothing else, in practice -- and it is returned rather than dropped
        because it is what an operator greps a log for. It is empty whenever
        the complaint list is.

        Best effort from end to end, and that must not drift. Every other Slack
        call in this file that can fail transiently retries, because a dropped
        upload or chunk is content the caller was told had been delivered.
        Nothing is delivered here: a validation that times out, is rate limited,
        hits a workspace without the method, or fails unexpectedly costs a
        better error message and nothing else, so it is swallowed and "could not
        validate" and "valid" are the same answer to the caller.

        ``retry_after_seconds`` is not consulted for the same reason: sleeping
        to improve a diagnostic delays a message the reader is waiting for, and
        a 429 on this method's own budget says the connector is already
        validating more than the workspace will bear.
        """
        if self._client is None:
            return "", []
        if not should_validate_blocks(blocks, block_kind, self._blockkit_validate_mode()):
            return "", []
        try:
            # blocks.validate is absent from slack_sdk 3.43.0, so it goes
            # through the generic escape hatch. ``blocks`` is JSON-encoded
            # because exactly one of blocks/message/view must be supplied and
            # each is a JSON string rather than a structure.
            response = await self._client.api_call(
                _BLOCKS_VALIDATE_METHOD,
                data={"blocks": json.dumps(blocks, ensure_ascii=False)},
            )
        except Exception as exc:  # noqa: BLE001
            # A refusal arrives here rather than as a return value: slack_sdk
            # raises on any reply that is not ``ok``, and ``ok: false`` is
            # precisely what a payload failing validation produces. So the
            # exception is asked whether it is holding a verdict before it is
            # treated as a transport failure. ``validation_errors`` is what
            # keeps the two apart -- a rate limit reaches this branch in the
            # very same shape and must not cost the message its rendering.
            response = getattr(exc, "response", None)
            errors = validation_errors(response)
            if errors:
                return validation_refusal_code(response), errors
            # INFO rather than WARNING: nothing has degraded. The message is
            # about to be sent exactly as it would have been sent if this
            # feature did not exist, and a workspace where the method is
            # unavailable would otherwise warn on every payload forever.
            logger.info(
                "[SlackChannel] blocks.validate did not answer for a %s payload "
                "(%s); sending it unvalidated",
                block_kind,
                exc,
            )
            return "", []
        return validation_refusal_code(response), validation_errors(response)

    async def _post_text(
        self,
        *,
        channel_id: str,
        text: str,
        thread_ts: str,
        update_ts: str = "",
        chunk_index: int = 0,
        chunk_total: int = 0,
        blocks: list[dict[str, Any]] | None = None,
        block_kind: str = slack_blocks.BLOCK_KIND_UNKNOWN,
        block_text_route: str = "",
        repair_truncation: bool = True,
    ) -> tuple[bool, str, str]:
        """Post one chunk. Returns ``(sent, message_ts, error)``.

        ``repair_truncation`` is cleared for the continuation this call may post
        for itself, and for nothing else. One pass is the whole guard: a
        continuation short enough for Slack to store whole needs none, and one
        that is cut again would otherwise open a chain of them.

        With ``update_ts`` the chunk rewrites that existing message through
        chat.update instead of posting a new one. Both calls are metered the same
        way and fail the same way, so they share the rate-limit handling here
        rather than growing a second copy of it.

        The error string is returned rather than raised so ``send()`` can attach the
        chunk counters before raising, which is what tells a partial send apart from
        a total failure.

        ``chunk_index`` / ``chunk_total`` are supplied only by ``send()``, whose
        writes are the deliveries the caller was told had happened; those are
        logged at INFO. The streaming surface leaves them at zero because its
        edits are intermediate -- one per debounce window, superseded by the
        closing rewrite -- so they log at DEBUG and cannot flood a busy channel.

        ``blocks`` is additive and never replaces ``text``. Slack renders the
        blocks when they are present and falls back to ``text`` for the desktop
        notification, the mobile notification -- which uses nothing else -- and
        for screen readers, so the chunk is always supplied in full whether or
        not it also has a structured rendering. Both calls accept the field, so a
        streamed reply's closing rewrite can hold one as readily as a fresh
        post, and an answered question can be rewritten without the buttons it
        was posted with.

        That additivity is what makes the rendering droppable: if Slack refuses
        the blocks, the call is retried once without them and the chunk is
        delivered as text. Only the block-specific errors qualify; every other
        failure is returned unchanged.

        ``text`` is replaced rather than kept additive in exactly one case: a
        chunk that is nothing but a fence, so that stripping every fenced
        region out of it leaves nothing behind. There the chunk *is* the
        source ``blocks`` was rendered from, and sending it as ``text`` too
        would put the raw payload in the notification and in front of a
        screen reader -- the additivity guarantee protects a fallback, not a
        second copy of the machine-readable form. ``blocks`` still has to be
        present for this to fire: a fence that declined to render leaves
        nothing to derive a fallback from, and the source is what a reader
        gets instead, same as it always was. The replacement carries into the
        refusal branch below as well, becoming the lead line
        ``block_failure_text`` builds on rather than the fence source it would
        otherwise have quoted back verbatim -- a shorter first line, not a
        loss, since a fence's own kind already quotes the payload again
        further down.

        ``block_kind`` is one of ``slack_blocks.BLOCK_KIND_*`` and says what
        built the payload -- a table, a model's fence, a control. It is passed
        by the caller because the caller is the only one who still knows: by the
        time the blocks arrive here they are a flat list that no longer says
        where any of them came from. It decides two things and nothing else:
        whether the payload is worth validating, and what the reader is shown if
        validation refuses it.

        ``block_text_route`` is the sentence naming how the same action can be
        taken without the control, for the callers that have one. Only read on
        an interactive failure, where the whole message is that there is nothing
        to click and the useful half is what to do instead.
        """
        if blocks and not _fenced_regions_stripped(text).strip():
            # A reply that is only a fence has no prose for ``text`` to be, and
            # the chunk itself is the payload ``blocks`` was rendered from --
            # sending it unmodified is how the raw JSON reached a notification
            # in the first place. The derived summary reads the rendering
            # instead of the source it came from, which is the one thing here
            # that still says something about what is on screen.
            text = _clamp(
                slack_blocks.blockkit_fallback_summary(blocks)
                or _BLOCKKIT_FALLBACK_GENERIC_TEXT,
                _BLOCKKIT_FALLBACK_TEXT_LIMIT,
            )
        kwargs: dict[str, Any] = {"channel": channel_id, "text": text}
        if blocks is not None:
            # Passed even when empty, because an empty list is how chat.update
            # removes the blocks a message already has. Omitting the field
            # instead would leave the buttons exactly where they were. The Block
            # Kit path cannot reach here with one: render_blocks returns None on
            # every path that declines, so the wider test admits nothing beyond
            # the button-withdrawal caller that needs it.
            kwargs["blocks"] = blocks
        if update_ts:
            # An edit lands wherever the message already is; thread_ts is not
            # accepted here and would be meaningless if it were.
            kwargs["ts"] = update_ts
        elif thread_ts:
            kwargs["thread_ts"] = thread_ts

        # Kept because the post-hoc fallback needs the payload after the field
        # has been taken off ``kwargs``, and because ``blocks`` is set to None
        # there to stop the loop retrying with them.
        refused_blocks = list(blocks) if blocks else []
        refusal_code, errors = await self._refused_blocks(blocks, block_kind)
        if errors:
            # Not sent, and not retried without them either: the payload is
            # known bad before a single write is spent on it, so the degraded
            # message is what goes out first time. The post-hoc path below still
            # stands behind this -- validation approving a payload is not a
            # promise chat.postMessage will take it.
            # Everything Slack said, and enough of the payload to find the
            # element it said it about. The reader is told far less on the
            # interactive path -- there is nothing for them to fix -- and that
            # asymmetry is the point: a refusal nobody can act on for the
            # reader is still the operator's to act on, and a line naming only
            # the pointer leaves them guessing which element it reached.
            logger.warning(
                "Slack refused a Block Kit payload of kind %s before it was "
                "sent (%s); Slack's validator reported: %s; the payload it "
                "named: %s; delivering the message without it",
                block_kind,
                refusal_code or "refused",
                describe_validation_errors(errors),
                describe_refused_blocks(refused_blocks, errors),
            )
            text = block_failure_text(
                text=text,
                blocks=blocks or [],
                kind=block_kind,
                errors=errors,
                text_route=block_text_route,
            )
            # Rebound rather than only written into ``kwargs``: the delivery
            # checks further down compare what Slack echoed against ``text``,
            # and comparing against a longer string than was actually sent
            # would report a truncation that never happened.
            text = _clamp(
                text,
                _MAX_SLACK_UPDATE_TEXT_LENGTH if update_ts else _MAX_SLACK_TEXT_LENGTH,
            )
            kwargs["text"] = text
            drop_refused_blocks(kwargs, update_ts=update_ts)
            blocks = None

        rate_limit_retries = 0
        while True:
            try:
                if update_ts:
                    response = await self._client.chat_update(**kwargs)
                else:
                    response = await self._client.chat_postMessage(**kwargs)
            except Exception as exc:  # noqa: BLE001
                delay = retry_after_seconds(exc)
                if delay is not None and rate_limit_retries < _MAX_RATE_LIMIT_RETRIES:
                    rate_limit_retries += 1
                    logger.warning(
                        "Slack rate limited; retrying in %.1fs (%d/%d)",
                        delay,
                        rate_limit_retries,
                        _MAX_RATE_LIMIT_RETRIES,
                    )
                    await asyncio.sleep(delay)
                    continue
                rejected = rejected_blocks_error(exc)
                # Present *and* holding something. On a post the field is gone
                # after one pass through here and the test is the same either
                # way; on an edit it is left empty rather than removed, so a
                # membership test would send this branch round again on a
                # payload that no longer has a rendering to drop.
                if rejected is not None and kwargs.get("blocks"):
                    # The text in this very payload is the whole chunk and would
                    # have delivered on its own, so a refused rendering must cost
                    # the formatting rather than the message. Logged loudly
                    # because it means the local validation has a blind spot.
                    # ``response_metadata.messages`` is the only place a
                    # refused *send* says which element it meant, and it is
                    # read here for the reason the pre-send line reads the
                    # errors array: an operator who is told ``invalid_blocks``
                    # and nothing else has a fifty-block payload and no way in.
                    sent_refusal = send_refusal_errors(exc)
                    logger.warning(
                        "Slack rejected the Block Kit rendering (%s); Slack "
                        "reported: %s; the payload it named: %s; delivering "
                        "this chunk as plain text",
                        rejected,
                        describe_validation_errors(sent_refusal)
                        if sent_refusal
                        else "no detail",
                        describe_refused_blocks(
                            list(refused_blocks or kwargs.get("blocks") or []),
                            sent_refusal,
                        ),
                    )
                    # The backstop, and it stays the backstop: pre-send
                    # validation is skipped by policy for most payloads, can be
                    # unavailable for all of them, and a payload it approves can
                    # still be refused here. The reader is told in the same words
                    # the pre-send path uses, and the retry is the same -- the
                    # blocks come off and the chunk is delivered.
                    refused = list(refused_blocks or kwargs["blocks"] or [])
                    drop_refused_blocks(kwargs, update_ts=update_ts)
                    blocks = None
                    if refused:
                        # Rebound for the same reason the pre-send path rebinds
                        # it: the echo check below compares what Slack returned
                        # against ``text``, and this is the one path that has
                        # just made ``text`` longer. Written only into
                        # ``kwargs``, the check would compare the echo of the
                        # notice against the chunk without it and see a
                        # shortfall it never reports -- truncation detection
                        # switched off exactly where the payload grew.
                        text = _clamp(
                            block_failure_text(
                                text=text,
                                blocks=refused,
                                kind=block_kind,
                                errors=[{"code": rejected}],
                                text_route=block_text_route,
                            ),
                            _MAX_SLACK_UPDATE_TEXT_LENGTH
                            if update_ts
                            else _MAX_SLACK_TEXT_LENGTH,
                        )
                        kwargs["text"] = text
                    continue
                reason = slack_failure_reason(exc)
                logger.warning("SlackChannel send failed: %s", reason)
                return False, "", reason

            response_ts = ""
            if response is not None:
                try:
                    response_ts = str(response.get("ts") or "").strip()
                except (AttributeError, TypeError):
                    response_ts = ""
            dropped = self._short_echo_tail(
                response=response,
                channel_id=channel_id,
                response_ts=response_ts,
                sent_text=text,
                update_ts=update_ts,
                had_blocks=bool(blocks),
            )
            if dropped and repair_truncation and not update_ts:
                await self._post_truncated_tail(
                    channel_id=channel_id,
                    thread_ts=thread_ts,
                    dropped=dropped,
                )
            # Suffixed rather than a field of its own, so a delivery that used
            # no blocks -- every delivery but an answerable question or a
            # rendered table -- logs exactly the line it logged before they
            # existed. Tested the same way as the guard above, so the log
            # reports whether the payload held a blocks field rather than
            # inferring it from that field's contents; the two cannot disagree.
            mode = "update" if update_ts else "post"
            if blocks is not None:
                mode = f"{mode}+blocks"
            if chunk_total:
                logger.info(
                    "[SlackChannel] delivered text: channel=%s ts=%s chunk=%d/%d mode=%s",
                    channel_id,
                    response_ts or "-",
                    chunk_index,
                    chunk_total,
                    mode,
                )
            else:
                logger.debug(
                    "[SlackChannel] streaming edit applied: channel=%s ts=%s mode=%s",
                    channel_id,
                    response_ts or "-",
                    mode,
                )
            return True, response_ts, ""

    @staticmethod
    def _short_echo_tail(
        *,
        response: Any,
        channel_id: str,
        response_ts: str,
        sent_text: str,
        update_ts: str,
        had_blocks: bool = False,
    ) -> str:
        """The text Slack cut off the end, or ``""`` when nothing is missing.

        Both chat.postMessage and chat.update answer with the stored message, so
        the reply is the one place a write can be checked against its own intent
        without reading the conversation back. An ``ok: true`` that did not
        apply -- the failure mode a returned ts cannot distinguish from a
        successful one -- shows up here and nowhere else, which is why a reply
        that arrived complete at the API and truncated on screen would otherwise
        leave no trace on the sending side at all.

        Slack shortens what it echoes for two reasons. One of them loses
        content and the other does not, so a shorter echo on its own says
        nothing.

        * It cuts the end off one message. The echo is then the *start* of what
          was sent, and everything past it is owed back to the reader.
        * It breaks one oversized post into several messages of its own. The
          reply holds the ts and the text of the *last* of them, so the echo is
          the *end* of what was sent, and every character is already on screen
          across messages whose ids this connector never learns.

        Where the echo sits inside what was sent separates the two, and that
        is what this checks. An echo the sent text ends with is a post Slack
        split, and nothing is missing. An echo the sent text starts with is a
        message Slack cut, and what follows it is exactly what was lost. An echo
        that is neither has been rewritten past recognition -- Slack normalises
        what it stores, turning mrkdwn links into ``<url|label>`` and escaping
        bare entities -- so no offset into the sent text means anything and
        nothing is posted rather than guessed at.

        Length alone cannot separate them. The last part of a three-message
        split is a small fraction of the payload, so a shortfall computed from
        its length spans two messages that were delivered whole. Both the
        duplication and the wrong resume point follow from that one reading.

        A payload holding blocks is never repaired. Blocks are what such a
        message renders and ``text`` is the notification fallback beside them,
        holding the same content; Slack stores a long fallback cut to its own
        ceiling while the blocks arrive whole, so the reader is missing nothing
        and a continuation would put the end of the message on screen twice.

        **The tail is returned as well as counted.** Reporting the shortfall and
        stopping there leaves a reply that ends mid-sentence on screen with
        nothing to say it was cut: the reader sees a finished answer that
        happens to stop, and the only trace goes to a log nobody in the
        conversation opens.

        The cut point is where the stored text ends, which is a character count
        rather than a place in the sender's string, so the tail is the remainder
        by length. The prefix test makes that count exact, because the two
        strings agree character for character up to it.
        """
        if response is None:
            return ""
        try:
            echoed = response.get("message") or {}
            echoed_text = str(echoed.get("text") or "")
        except (AttributeError, TypeError):
            return ""
        if not echoed_text or len(echoed_text) >= len(sent_text):
            return ""

        mode = "update" if update_ts else "post"
        stored = echoed_text.rstrip()
        sent = sent_text.rstrip()

        if sent.endswith(stored):
            # Slack split the post and answered for the last piece. Logged
            # because a payload larger than one Slack message is worth knowing
            # about, and at INFO because the reader has all of it.
            logger.info(
                "[SlackChannel] Slack split what was sent into messages of its "
                "own: channel=%s ts=%s mode=%s sent_chars=%d last_chars=%d",
                channel_id,
                response_ts or "-",
                mode,
                len(sent_text),
                len(echoed_text),
            )
            return ""

        if not sent.startswith(stored):
            logger.warning(
                "[SlackChannel] Slack echoed text that is neither the start nor "
                "the end of what was sent, so what reached the reader cannot be "
                "placed: channel=%s ts=%s mode=%s sent_chars=%d stored_chars=%d",
                channel_id,
                response_ts or "-",
                mode,
                len(sent_text),
                len(echoed_text),
            )
            return ""

        logger.warning(
            "[SlackChannel] Slack stored less text than was sent: channel=%s "
            "ts=%s mode=%s sent_chars=%d stored_chars=%d",
            channel_id,
            response_ts or "-",
            mode,
            len(sent_text),
            len(echoed_text),
        )
        if had_blocks:
            logger.info(
                "[SlackChannel] the text Slack cut is the fallback beside a "
                "Block Kit rendering that arrived whole, so nothing is posted "
                "back: channel=%s ts=%s",
                channel_id,
                response_ts or "-",
            )
            return ""
        return sent_text[len(stored) :]

    async def _post_truncated_tail(
        self, *, channel_id: str, thread_ts: str, dropped: str
    ) -> None:
        """Put the part Slack dropped back on screen, under a marker.

        The same bargain the streaming ladder strikes when a close is refused
        with the reply's tail still undelivered: the content is owed to the
        reader whatever the API did with it, and a message of its own is the
        only surface left once the first one is too long to rewrite.

        Never raises and never reports failure upward. The chunk this repairs
        was delivered -- Slack answered ``ok`` and stored most of it -- so the
        caller's own result is already settled and a failure here must not turn
        a delivered reply into a reported one. A repair that could not be made
        is logged instead.
        """
        tail = dropped.strip()
        if not tail:
            return
        chunks = self._split_text(f"{_SLACK_TRUNCATION_NOTICE}\n\n{tail}")
        for index, chunk in enumerate(chunks):
            sent, _, error = await self._post_text(
                channel_id=channel_id,
                text=chunk,
                thread_ts=thread_ts,
                chunk_index=index + 1,
                chunk_total=len(chunks),
                repair_truncation=False,
            )
            if not sent:
                logger.warning(
                    "[SlackChannel] the part Slack cut could not be posted "
                    "beside it: channel=%s chunk=%d/%d error=%s",
                    channel_id,
                    index + 1,
                    len(chunks),
                    error or "-",
                )
                return

    # ── The cron status record ───────────────────────────────────────────────
    #
    # Every push a scheduled run makes holds payload["cron"] with a run id, a
    # placeholder flag and a status. That triple is a supersession contract: the
    # terminal message is the placeholder's own event reaching its conclusion,
    # not a second event. Honouring it is entirely local -- the scheduler's push
    # ends at a queue, so it has returned before the channel is picked and it
    # knows nothing of cards, edits or message ids.
    #
    # Nothing below is reachable unless a message holds that dict, so a
    # connector-wide behaviour change is impossible by construction.

    @staticmethod
    def _cron_payload(msg: Message) -> dict[str, Any] | None:
        """Return the cron metadata on a push, or ``None`` for anything else."""
        payload = getattr(msg, "payload", None)
        if not isinstance(payload, dict):
            return None
        cron = payload.get("cron")
        if not isinstance(cron, dict):
            return None
        if not str(cron.get("run_id") or "").strip():
            # Without a correlation key there is no record to keep and nothing
            # a later message could supersede.
            return None
        return cron

    @staticmethod
    def _rich_text(text: str) -> dict[str, Any]:
        """Wrap plain text as the rich_text block a card field must be.

        ``details`` and ``output`` are typed blocks rather than strings, which
        the SDK does not enforce and Slack does.
        """
        return {
            "type": "rich_text",
            "elements": [
                {
                    "type": "rich_text_section",
                    "elements": [{"type": "text", "text": text}],
                }
            ],
        }

    @classmethod
    def _cron_card_details(
        cls, cron: Mapping[str, Any], *, card_status: str, body: str
    ) -> str:
        """The one or two lines under the chip.

        For a failure this is the run's own words, which is what states the
        distinction Slack's three statuses cannot: a run that failed and a run
        that finished and had its result thrown away are both ``error``, and
        only the text says which. The scheduler writes that sentence; nothing
        here parses it.
        """
        lines: list[str] = []
        if card_status == _CARD_STATUS_ERROR:
            reason = (body or "").strip().splitlines()
            if reason:
                lines.append(reason[0].strip())
        scheduled = str(cron.get("push_at") or "").strip()
        if scheduled:
            lines.append(f"Scheduled for {scheduled}")
        return _clamp("\n".join(lines), _MAX_CARD_DETAILS_LENGTH)

    @classmethod
    def _cron_task_card(
        cls, cron: Mapping[str, Any], *, card_status: str, body: str
    ) -> dict[str, Any]:
        """Build the card from the payload's structured fields.

        Never from ``job.description``: that is the prompt, up to four thousand
        characters of data directory paths, state files and operator
        instructions. The brief is the job's name.
        """
        run_id = str(cron.get("run_id") or "").strip()
        title = str(cron.get("job_name") or "").strip() or "Scheduled job"
        card: dict[str, Any] = {
            "type": "task_card",
            # Namespaced, because the id space is shared with whatever else
            # posts cards, and it matches the run id already held in the
            # Message id.
            "task_id": f"cron-{run_id}",
            "title": _clamp(title, _MAX_CARD_TITLE_LENGTH),
            "status": card_status,
        }
        details = cls._cron_card_details(cron, card_status=card_status, body=body)
        if details:
            card["details"] = cls._rich_text(details)
        return card

    def _cron_status_record(
        self, msg: Message, channel_id: str, content: str
    ) -> _SlackCronPush | None:
        """Decide what this push does to the run's record, if it is one."""
        cron = self._cron_payload(msg)
        if cron is None:
            return None
        run_id = str(cron.get("run_id") or "").strip()
        is_placeholder = bool(cron.get("is_placeholder"))
        status = str(cron.get("status") or "").strip().lower()

        if is_placeholder:
            # Provisional by definition, whatever the status says.
            card_status = _CARD_STATUS_IN_PROGRESS
            anchor_ts = ""
        else:
            card_status = _CRON_TERMINAL_CARD_STATUS.get(status, "")
            record = self._cron_records.pop((run_id, channel_id), None)
            anchor_ts = record.message_ts if record is not None else ""
            if not card_status:
                # A terminal message with an outcome this connector cannot
                # name. It still supersedes -- the placeholder must not be left
                # asserting that a finished run is live -- but it does so as
                # text, with no chip claiming an outcome nobody established.
                logger.info(
                    "[SlackChannel] cron run %s reported status %r; delivering "
                    "the result without a status card",
                    run_id,
                    status,
                )
                return _SlackCronPush(
                    run_id=run_id,
                    channel_id=channel_id,
                    is_placeholder=False,
                    card=None,
                    anchor_ts=anchor_ts,
                    body_below_card=True,
                )

        card = self._cron_task_card(cron, card_status=card_status, body=content)
        # The placeholder's prose restates every field the card renders, and a
        # failure's first line is already the card's details, so neither is
        # repeated underneath it. A completed run's report is not: the card
        # fronts it rather than replacing it.
        body_below_card = card_status == _CARD_STATUS_COMPLETE or (
            card_status == _CARD_STATUS_ERROR
            and len((content or "").strip().splitlines()) > 1
        )
        return _SlackCronPush(
            run_id=run_id,
            channel_id=channel_id,
            is_placeholder=is_placeholder,
            card=card,
            anchor_ts=anchor_ts,
            body_below_card=body_below_card,
        )

    @classmethod
    def _section_blocks_for(cls, text: str) -> list[dict[str, Any]] | None:
        """Render already-normalised mrkdwn as section blocks, or ``None``.

        Needed only because blocks replace a message's text rather than
        accompany it: a card attached to a body that has no block rendering of
        its own would hide that body. ``None`` means "this will not fit", and
        the caller drops the card rather than the content.
        """
        remaining = (text or "").strip()
        blocks: list[dict[str, Any]] = []
        while remaining:
            if len(remaining) <= _MAX_SECTION_TEXT_LENGTH:
                piece, remaining = remaining, ""
            else:
                split_at = cls._preferred_split_index(
                    remaining, _MAX_SECTION_TEXT_LENGTH
                )
                piece = remaining[:split_at].rstrip()
                remaining = remaining[split_at:].lstrip()
                if not piece:
                    piece = remaining[:_MAX_SECTION_TEXT_LENGTH]
                    remaining = remaining[_MAX_SECTION_TEXT_LENGTH:]
            blocks.append(
                {"type": "section", "text": {"type": "mrkdwn", "text": piece}}
            )
            if len(blocks) >= slack_blocks.MAX_BLOCKS_PER_MESSAGE:
                return None
        return blocks or None

    def _root_blocks_and_kind_for(
        self, text: str, requested: bool | None, cron: _SlackCronPush | None
    ) -> tuple[list[dict[str, Any]] | None, str]:
        """``_blocks_and_kind_for`` for a root message, which may hold a card.

        A cron card is this connector's own chrome, so a root that is only a
        card is exactly that. A root that is a card *above* a rendered body
        takes the body's kind, because the body is the part a refusal would cost
        the reader and the card is a status chip they can lose without noticing.
        """
        if cron is None or cron.card is None:
            return self._blocks_and_kind_for(text, requested)
        if not cron.body_below_card:
            return [cron.card], slack_blocks.BLOCK_KIND_CHROME
        body, kind = self._blocks_and_kind_for(text, requested)
        if body is None:
            # Blocks replace a message's text rather than accompany it, so a
            # body with no rendering of its own has to be given one before a
            # card can sit above it -- otherwise attaching the card would hide
            # the result.
            body = self._section_blocks_for(text)
            kind = slack_blocks.BLOCK_KIND_CHROME
        if body is None:
            return None, slack_blocks.BLOCK_KIND_UNKNOWN
        if len(body) + 1 > slack_blocks.MAX_BLOCKS_PER_MESSAGE:
            # No room for both. The card is what gives way: the result is the
            # thing the reader was promised, and supersession is unaffected --
            # the message is still rewritten, just without a chip.
            logger.info(
                "[SlackChannel] cron run %s renders to the full block budget; "
                "delivering the result without a status card",
                cron.run_id,
            )
            return body, kind
        return [cron.card, *body], slack_blocks.combine_block_kinds(
            (kind, slack_blocks.BLOCK_KIND_CHROME)
        )

    def _remember_cron_record(self, cron: _SlackCronPush, message_ts: str) -> None:
        """Record a posted placeholder so its result can rewrite it."""
        _prune_bounded_map(
            self._cron_records,
            cap=_MAX_CRON_RECORDS,
            timeout=_CRON_RECORD_TIMEOUT_SECONDS,
            timestamp=lambda entry: entry.touched_at,
            what="tracked cron runs",
        )
        self._cron_records[(cron.run_id, cron.channel_id)] = _SlackCronRecord(
            run_id=cron.run_id,
            channel_id=cron.channel_id,
            message_ts=message_ts,
        )

    # ------------------------------------------------------------------
    # Turn activity card
    # ------------------------------------------------------------------

    @staticmethod
    def _run_label(raw: Any, fallback: str) -> str:
        """Reduce a model-supplied name to something showable.

        Applied to a ``subagent_type`` and to a tool name alike: both are
        strings the model chose, and a card is not a place to put text nobody
        validated. Anything outside letters, digits and the three punctuation
        marks an identifier uses is dropped, and what is left is clamped. A name
        this connector has never heard of therefore renders as itself, and a
        name that is secretly a paragraph renders as a fragment of one with no
        newlines in it.
        """
        if not isinstance(raw, str):
            return fallback
        label = _RUN_LABEL_ALLOWED.sub(" ", raw)
        label = " ".join(label.split())
        return _clamp(label, _MAX_RUN_LABEL_LENGTH) or fallback

    @classmethod
    def _subagent_type_label(cls, arguments: Any) -> str:
        """The ``subagent_type`` argument, reduced to something showable.

        The only field of a dispatch or roster update this connector reads.
        The brief beside it stays off the card, as it does in the runtime log.
        """
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except (TypeError, ValueError):
                return _UNKNOWN_SUBAGENT_LABEL
        if not isinstance(arguments, Mapping):
            return _UNKNOWN_SUBAGENT_LABEL
        return cls._run_label(arguments.get("subagent_type"), _UNKNOWN_SUBAGENT_LABEL)

    @staticmethod
    def _tool_event_body(msg: Message, nested_key: str) -> Mapping[str, Any] | None:
        """The tool's own dict, whether it is nested or flattened.

        The two wire builders disagree: a ``chat.tool_call`` keeps its fields
        under ``tool_call``, while a ``chat.tool_result`` has them spread across
        the payload itself. Both shapes are accepted rather than one of them
        being declared correct, because a card that silently stops appearing is
        exactly the kind of failure nobody reports.
        """
        payload = msg.payload if isinstance(msg.payload, dict) else None
        if payload is None:
            return None
        nested = payload.get(nested_key)
        return nested if isinstance(nested, Mapping) else payload

    @classmethod
    def _tool_call_event(cls, msg: Message) -> tuple[str, str, Any] | None:
        """``(tool name, call id, arguments)`` for a tool call, else ``None``."""
        body = cls._tool_event_body(msg, "tool_call")
        if body is None:
            return None
        name = str(body.get("name") or body.get("tool_name") or "").strip()
        if not name:
            return None
        call_id = str(body.get("tool_call_id") or body.get("id") or "").strip()
        return name, call_id, body.get("arguments")

    @classmethod
    def _tool_result_event(cls, msg: Message) -> tuple[str, str, str] | None:
        """``(tool name, call id, state)`` for a finished tool, else ``None``.

        The state is the rail's own classification and nothing more.
        ``_infer_tool_result_error`` has already read the structured result and
        said whether it was an error; this reads the flags it set. The result
        text itself is never parsed here -- a card that guessed at an exit code
        buried in prose would be right often and confidently wrong sometimes,
        and being confidently wrong about whether the work succeeded is the one
        thing this card must not do.
        """
        body = cls._tool_event_body(msg, "tool_result")
        if body is None:
            return None
        call_id = str(body.get("tool_call_id") or body.get("id") or "").strip()
        if not call_id:
            # Nothing to pair this with, so there is nothing here for the card
            # to read and no reason to take the event off the ordinary path.
            return None
        name = str(body.get("tool_name") or body.get("name") or "").strip()
        failed = (
            body.get("success") is False
            or bool(body.get("is_error"))
            or str(body.get("status") or "").strip().lower() == "error"
        )
        return name, call_id, _RUN_FAILED if failed else _RUN_DONE

    @staticmethod
    def _todo_snapshot(msg: Message) -> dict[str, int] | None:
        """Counts per state for a ``todo.updated`` payload, else ``None``.

        The event holds the whole list every time it is emitted, so this is a
        snapshot to render rather than a change to accumulate: the counts it
        produces replace whatever the record held. Only the status of each entry
        is read. The content beside it is the plan in the model's own words, and
        a card is not where a channel reads that.
        """
        payload = msg.payload if isinstance(msg.payload, dict) else None
        if payload is None:
            return None
        todos = payload.get("todos")
        if not isinstance(todos, list):
            return None
        counts: dict[str, int] = {}
        for item in todos:
            if not isinstance(item, Mapping):
                continue
            state = str(item.get("status") or "").strip().lower()
            if state not in _TODO_STATES:
                # Anything the runtime has not named is counted as not done.
                # Overstating what is left is the safe direction; the list is
                # the parent's own and its vocabulary is three words wide.
                state = _TODO_PENDING
            counts[state] = counts.get(state, 0) + 1
        return counts

    @staticmethod
    def _compression_event(msg: Message) -> Mapping[str, Any] | None:
        """The compaction payload, or ``None`` when this event is not one.

        Read off the payload's own ``event_type`` because ``msg.event_type`` is
        unset here: ``EventType`` has no member for ``context.compression_state``,
        so the gateway's parse raises, it logs the string at debug and leaves the
        field ``None``. The web channel matches the same string the same way,
        which is the precedent rather than a coincidence -- adding an enum member
        would change what every other channel sees of an event none of them asked
        about, for a card that only needs to recognise it.
        """
        payload = msg.payload if isinstance(msg.payload, dict) else None
        if payload is None:
            return None
        if str(payload.get("event_type") or "") != _COMPRESSION_EVENT_TYPE:
            return None
        return payload

    @staticmethod
    def _compression_int(value: Any) -> int:
        """One count off the payload, or zero for anything that is not one.

        The figures on this section are the runtime's, not the model's, but they
        cross the same wire as everything else and arrive as whatever JSON made
        of them. A field that is missing, null or a string reads as zero and its
        line simply says less, which is the direction to fail in on a card whose
        whole job is to be legible while something slow is happening.
        """
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return 0
        return max(0, int(value))

    @classmethod
    def _compression_metric(
        cls, body: Mapping[str, Any], key: str
    ) -> Mapping[str, Any]:
        """The ``before`` / ``after`` / ``saved`` sub-object, or an empty one.

        ``after`` and ``saved`` are absent on a ``started`` event and null on a
        pass that never got far enough to fill them, so the empty mapping is the
        ordinary case rather than the defensive one.
        """
        value = body.get(key)
        return value if isinstance(value, Mapping) else {}

    # -- Rendering ------------------------------------------------------

    @staticmethod
    def _format_elapsed(seconds: float) -> str:
        """How long something has been running, in ASCII.

        Kept to digits and ``h``/``m``/``s``: a text element holding anything
        outside ASCII has been seen to take the whole message down with
        ``invalid_blocks``.
        """
        whole = max(0, int(seconds))
        if whole < 60:
            return f"{whole}s"
        minutes, secs = divmod(whole, 60)
        if minutes < 60:
            return f"{minutes}m {secs:02d}s"
        hours, minutes = divmod(minutes, 60)
        return f"{hours}h {minutes:02d}m"

    @staticmethod
    def _counts_line(
        counts: Mapping[str, int],
        states: tuple[tuple[str, str], ...],
        *,
        label: str = "",
    ) -> list[dict[str, Any]]:
        """One line: an optional label, then a count per state anything is in.

        Each state is an ``emoji`` element rather than a word, which is what
        lets four counts share a line. A state nobody is in is left off
        entirely: a zero is not information, and four states spelt out every
        time would be a table.
        """
        line: list[dict[str, Any]] = []
        if label:
            line.append({"type": "text", "text": label, "style": {"code": True}})
        for state, emoji in states:
            count = counts.get(state, 0)
            if not count:
                continue
            if line:
                line.append({"type": "text", "text": "  "})
            line.append({"type": "emoji", "name": emoji})
            line.append({"type": "text", "text": f" {count}"})
        return line

    @staticmethod
    def _rich_text_lines(lines: list[list[dict[str, Any]]]) -> dict[str, Any] | None:
        """Wrap ``lines`` as the single rich_text a card field takes.

        ``details`` and ``output`` both hold exactly one rich_text entity, so
        every line goes into one section separated by newlines in text elements
        of their own. Returns ``None`` for nothing to say, which is how a field
        that would have been empty is left off the card entirely.
        """
        elements: list[dict[str, Any]] = []
        for line in lines:
            if not line:
                continue
            if elements:
                elements.append({"type": "text", "text": "\n"})
            elements.extend(line)
        if not elements:
            return None
        return {
            "type": "rich_text",
            "elements": [{"type": "rich_text_section", "elements": elements}],
        }

    @classmethod
    def _grouped_lines(
        cls,
        by_label: Mapping[str, Mapping[str, int]],
        states: tuple[tuple[str, str], ...],
    ) -> list[list[dict[str, Any]]]:
        """One line per label, alphabetically, each with its own counts.

        Grouped rather than listed because the fan-out that needs this most is
        the wide one, and twenty rows is the wall a single card exists to avoid.
        Alphabetical is the only order available that does not encode dispatch
        order -- meaningless once things run concurrently -- and it keeps a
        label on the same line across the edits that rewrite the card in place.
        """
        lines = [
            cls._counts_line(by_label[label], states, label=label)
            for label in sorted(by_label)
        ]
        if len(lines) > _MAX_CARD_DETAIL_LINES:
            hidden = len(lines) - _MAX_CARD_DETAIL_LINES
            lines = lines[:_MAX_CARD_DETAIL_LINES]
            lines.append([{"type": "text", "text": f"and {hidden} more"}])
        return lines

    @staticmethod
    def _runs_card_status(counts: Mapping[str, int]) -> str:
        """The chip for a section built from runs.

        A run nobody heard from reaches the same chip as one that failed --
        Slack has three statuses nested and neither of the other two is honest
        about it -- but only the counts distinguish them, which is why both
        emoji stay on the line.
        """
        if counts.get(_RUN_RUNNING):
            return _CARD_STATUS_IN_PROGRESS
        if counts.get(_RUN_FAILED) or counts.get(_RUN_UNREPORTED):
            return _CARD_STATUS_ERROR
        return _CARD_STATUS_COMPLETE

    @classmethod
    def _thinking_summary(cls, record: "_SlackActivityRecord") -> str:
        """``thinking for 1m 12s`` / ``thought for 2m 45s``, or ``""`` for neither.

        One wording, used both as the section's detail line and as its share of
        the notification text, so the two can never drift apart.

        The settled form is past tense rather than a word like "done" or
        "complete". All that ended is the thinking. Whether the turn it belonged
        to answered or died is a fact about the turn -- stated on the plan's own
        line and in the error section, both of which say it plainly -- and not
        one this duration is entitled to restate. A section that said "complete"
        in words would be asserting something about the turn that nothing here
        established.
        """
        if not record.reasoning_seen:
            return ""
        elapsed = cls._format_elapsed(record.reasoning_seconds())
        if record.reasoning_since is not None:
            return f"thinking for {elapsed}"
        return f"thought for {elapsed}"

    @staticmethod
    def _format_count(value: int) -> str:
        """A count with thousands separators, in ASCII.

        Grouped, because the size of the thing being compacted is the whole
        reason this number is on the card: six hundred thousand tokens is a wall
        of digits ungrouped, and a reader who has to count them has not been
        told anything a spinner would not have told them.
        """
        return f"{int(value):,}"

    @classmethod
    def _compression_notes(cls, run: "_SlackCompressionRun") -> list[str]:
        """What one compaction pass's line says after its label.

        A pass in flight says how big the window it is working on is. That is
        the answer to "why is this taking so long", it is available nowhere else
        in the channel, and it is the one thing a generic "working" line cannot
        say: two hundred messages at sixty-five per cent of the window explains
        a six-minute wait, and a spinner explains nothing.

        A pass that finished says what it recovered and how long it took
        instead. The size it started from stops being news the moment it is no
        longer what the turn is waiting on, and the duration becomes the news:
        it is the difference between a turn that was slow and a turn that was
        stuck, which is the question the reader had all along.

        A pass that saved nothing says so in words. There is no figure to print
        and the absence is itself the report -- a processor that ran to the end
        and recovered nothing is a fact about how the deployment is configured,
        and a blank line would read as a rendering fault instead.

        A pass a stop reached says "cancelled" rather than "failed". The two are
        the same state to the runtime and different facts to a reader: a
        cancelled compaction is the turn doing what it was told, and it does not
        end the turn -- the runtime logs a warning and carries on -- so a card
        calling it a failure would be reporting a fault in a turn that went on
        to answer.
        """
        elapsed = run.elapsed_seconds()
        if run.state == _COMPRESSION_STARTED:
            notes: list[str] = []
            if run.before_messages:
                notes.append(f"{cls._format_count(run.before_messages)} messages")
            if run.before_tokens:
                notes.append(f"{cls._format_count(run.before_tokens)} tokens")
            if run.before_percent is not None:
                notes.append(f"{int(run.before_percent)}% of window")
            notes.append(cls._format_elapsed(elapsed))
            return notes
        # A terminal line prints its duration only when there was one worth
        # explaining; see ``_MIN_COMPRESSION_ELAPSED_SECONDS``.
        tail = (
            [cls._format_elapsed(elapsed)]
            if elapsed >= _MIN_COMPRESSION_ELAPSED_SECONDS
            else []
        )
        if run.saved_anything():
            saved: list[str] = []
            if run.saved_messages:
                saved.append(f"{cls._format_count(run.saved_messages)} messages")
            if run.saved_tokens:
                saved.append(f"{cls._format_count(run.saved_tokens)} tokens")
            phrase = "saved " + ", ".join(saved)
            if run.saved_percent:
                phrase += f" ({round(run.saved_percent)}%)"
            return [phrase, *tail]
        if run.cancelled:
            return [_COMPRESSION_CANCELLED_ERROR, *tail]
        return [_COMPRESSION_STATE_WORDING.get(run.state, run.state), *tail]

    @classmethod
    def _compression_lines(
        cls, record: "_SlackActivityRecord"
    ) -> list[list[dict[str, Any]]]:
        """One line per pass, in the order the runtime ran them.

        Arrival order rather than alphabetical, unlike every other grouped list
        on this card. Which processor ran first is a fact about the pipeline and
        the reader is meant to see it: the turn this was built from ran an
        offloader that saved nothing and then a compressor that did all the
        work, and sorting the two by name would have hidden that the cheap one
        goes first and that it is the expensive one costing the minutes.
        """
        runs = list(record.compression_runs.values())
        lines: list[list[dict[str, Any]]] = []
        for run in runs[:_MAX_CARD_DETAIL_LINES]:
            line: list[dict[str, Any]] = [
                {
                    "type": "emoji",
                    "name": _COMPRESSION_STATE_EMOJI.get(
                        run.state, _COMPRESSION_STATE_EMOJI[_RUN_UNREPORTED]
                    ),
                },
                {"type": "text", "text": " "},
                {"type": "text", "text": run.label, "style": {"code": True}},
            ]
            line.extend(
                {"type": "text", "text": f"  {note}"}
                for note in cls._compression_notes(run)
            )
            lines.append(line)
        hidden = len(runs) - _MAX_CARD_DETAIL_LINES
        if hidden > 0:
            lines.append([{"type": "text", "text": f"and {hidden} more"}])
        return lines

    @classmethod
    def _compression_card(cls, record: "_SlackActivityRecord") -> dict[str, Any] | None:
        """The runtime compacting its own context. ``None`` when it did not.

        The section for the turn that has not started yet. Compaction runs in
        ``get_context_window``, before the model is called at all, so a turn
        over its threshold emits no tool call, no todo, no dispatch and not one
        chunk of reasoning for however long it takes -- six minutes and nineteen
        seconds on the turn this was written for -- and every other section on
        this card is built from work that has not happened. Without this the
        card does not appear, because there is nothing yet to put on it.

        One line per pass rather than one aggregate, and nothing here reads the
        summary the compressor wrote. ``compact_summary`` is the model's prose
        about the conversation it just folded up; it is high-volume unvetted
        output like a line of reasoning, and this section shows counts and
        durations for the same reason the thinking section shows a number.

        The chip is ``error`` for a pass that failed or was never heard from,
        which includes the one a cancel reached: the runtime reports that as
        ``failed`` with the error ``cancelled``, and a section chipped complete
        over it would say the compaction finished when it was abandoned.
        """
        runs = list(record.compression_runs.values())
        if not runs:
            return None
        if record.compression_running():
            status = _CARD_STATUS_IN_PROGRESS
        elif any(
            run.state in (_COMPRESSION_FAILED, _RUN_UNREPORTED) for run in runs
        ):
            status = _CARD_STATUS_ERROR
        else:
            status = _CARD_STATUS_COMPLETE
        card: dict[str, Any] = {
            "type": "task_card",
            # Namespaced like every other section: the id space is shared with
            # whatever else posts cards.
            "task_id": f"{_COMPRESSION_TASK_ID_PREFIX}{record.request_id}",
            "title": _COMPRESSION_CARD_TITLE,
            "status": status,
        }
        details = cls._rich_text_lines(cls._compression_lines(record))
        if details is not None:
            card["details"] = details
        return card

    @classmethod
    def _compression_summary(cls, record: "_SlackActivityRecord") -> str:
        """The compaction's share of the notification string, or ``""``.

        The size leads while a pass is running, for the reason the detail line
        gives: a reader whose phone buzzed after four minutes of nothing is
        asking whether the wait has a cause, and the number is the answer. Once
        every pass is over the line reports what was recovered instead, which is
        what a search result or a scrollback is worth reading.
        """
        runs = list(record.compression_runs.values())
        if not runs:
            return ""
        running = [run for run in runs if run.state == _COMPRESSION_STARTED]
        if running:
            largest = max(running, key=lambda run: run.before_messages)
            if largest.before_messages:
                return (
                    f"compacting {cls._format_count(largest.before_messages)} "
                    "messages of context"
                )
            return "compacting context"
        if any(run.state == _COMPRESSION_FAILED for run in runs):
            if all(
                run.cancelled
                for run in runs
                if run.state == _COMPRESSION_FAILED
            ):
                return "context compaction cancelled"
            return "context compaction failed"
        if any(run.state == _RUN_UNREPORTED for run in runs):
            return "context compaction reported no result"
        saved = record.compression_saved_tokens()
        if saved:
            return f"context compacted, {cls._format_count(saved)} tokens saved"
        return "context compaction saved nothing"

    @classmethod
    def _thinking_card(cls, record: "_SlackActivityRecord") -> dict[str, Any] | None:
        """How long the turn has reasoned. ``None`` when it has not reasoned at all.

        This is the section for the turn that does nothing else: no tool, no
        subagent, no todo, just a model thinking for a minute or seventeen while
        the channel shows an empty thread. Without it that turn produces no card
        at all, because every other section is built from work it never did.

        A duration and a chip, and nothing else. Not a snippet of the reasoning,
        not its first line, not a summary of it, not a count of chunks -- the
        payload of a ``chat.reasoning`` event is never opened anywhere in this
        connector. The signal is *that* the model is thinking and *for how
        long*; what it is thinking is high-volume unvetted output, and putting
        any of it here would cost this card the one property it has to keep.

        The chip is ``in_progress`` exactly while a stretch is open. Terminal is
        ``complete`` because Slack accepts no neutral fourth value -- see
        ``_CARD_STATUS_*`` -- so the word that would have stated the meaning
        lives in the detail line instead, where past tense can say the thinking
        ended without saying the turn succeeded.
        """
        if not record.reasoning_seen:
            return None
        thinking = record.reasoning_since is not None
        card: dict[str, Any] = {
            "type": "task_card",
            "task_id": f"{_THINKING_TASK_ID_PREFIX}{record.request_id}",
            "title": _THINKING_CARD_TITLE,
            "status": (
                _CARD_STATUS_IN_PROGRESS if thinking else _CARD_STATUS_COMPLETE
            ),
        }
        details = cls._rich_text_lines(
            [
                [
                    {
                        "type": "emoji",
                        "name": (
                            _THINKING_RUNNING_EMOJI
                            if thinking
                            else _THINKING_ENDED_EMOJI
                        ),
                    },
                    {"type": "text", "text": f" {cls._thinking_summary(record)}"},
                ]
            ]
        )
        if details is not None:
            card["details"] = details
        return card

    @classmethod
    def _todo_card(cls, record: "_SlackActivityRecord") -> dict[str, Any] | None:
        """The turn's todo list, as counts per state. ``None`` when there is none.

        ``pending`` is the reason this card is never posted on its own. Nested
        in a plan Slack accepts it; standalone it is refused outright, and it is
        the only value that says "written down, not started", which is what an
        untouched todo is.
        """
        counts = record.todo_counts
        total = sum(counts.values())
        if not total:
            return None
        done = counts.get(_TODO_COMPLETED, 0)
        if done >= total:
            status = _CARD_STATUS_COMPLETE
        elif done or counts.get(_TODO_IN_PROGRESS, 0):
            status = _CARD_STATUS_IN_PROGRESS
        else:
            status = _CARD_STATUS_PENDING
        card: dict[str, Any] = {
            "type": "task_card",
            # Namespaced, because the id space is shared with whatever else
            # posts cards -- ``cron-`` is the other occupant today.
            "task_id": f"todos-{record.request_id}",
            "title": _TODO_CARD_TITLE,
            "status": status,
        }
        details = cls._rich_text_lines([cls._counts_line(counts, _TODO_STATE_EMOJI)])
        if details is not None:
            card["details"] = details
        return card

    @classmethod
    def _subagent_card(cls, record: "_SlackActivityRecord") -> dict[str, Any] | None:
        """The turn's fan-out. ``None`` when the turn delegated nothing.

        ``details`` counts the whole fan-out. ``output`` always names its
        types, including when there is only one; the aggregate cannot tell a
        reader whether the turn used code_agent or another subagent.

        There is deliberately no row per subagent and no numbering. A row
        without a label says nothing a count does not already say: the
        number would be an artifact of dispatch order, meaningless once
        subagents run concurrently, and impossible to tie back to any piece of
        work. A label is not available either -- ``task_tool`` declares only
        ``subagent_type`` and ``task_description``, and the brief is redacted
        upstream on purpose and is never read here.
        """
        runs = list(record.subagent_runs.values())
        if not runs:
            return None
        card: dict[str, Any] = {
            "type": "task_card",
            "task_id": f"subagents-{record.request_id}",
            "title": _SUBAGENT_CARD_TITLE,
            "status": cls._runs_card_status(record.subagent_totals()),
        }
        details = cls._rich_text_lines(
            [cls._counts_line(record.subagent_totals(), _RUN_STATE_EMOJI)]
        )
        if details is not None:
            card["details"] = details
        output = cls._rich_text_lines(
            cls._grouped_lines(record.subagents_by_type(), _RUN_STATE_EMOJI)
        )
        if output is not None:
            card["output"] = output
        return card

    @classmethod
    def _tool_card(cls, record: "_SlackActivityRecord") -> dict[str, Any] | None:
        """The tools the turn ran itself. ``None`` when it ran none.

        Delegation and wait tools are not among them: the subagent card owns
        those runs, and counting their coordination calls would repeat them.

        A subagent's own tools never reach this connector either. What a
        subagent did inside itself is not on the wire at all, so the count is
        the parent's own work and nothing else.

        ``output`` names what is in flight right now, with how long it has been,
        and is left off entirely when nothing is -- which is most of the time,
        the parent being blocked on a subagent or thinking rather than calling.
        """
        by_tool = record.tools_by_name()
        if not by_tool:
            return None
        card: dict[str, Any] = {
            "type": "task_card",
            "task_id": f"tools-{record.request_id}",
            "title": _TOOL_CARD_TITLE,
            "status": cls._runs_card_status(record.tool_totals()),
        }
        details = cls._rich_text_lines(cls._grouped_lines(by_tool, _RUN_STATE_EMOJI))
        if details is not None:
            card["details"] = details
        output = cls._rich_text_lines(cls._running_tool_lines(record))
        if output is not None:
            card["output"] = output
        return card

    @classmethod
    def _running_tool_lines(
        cls, record: "_SlackActivityRecord"
    ) -> list[list[dict[str, Any]]]:
        """One line per tool still in flight, oldest first.

        Oldest first because the one that has been out longest is the one the
        turn is actually waiting on. The duration is measured here: no payload
        on the wire holds one, so the call is clocked when it arrives, the
        same way every other record in this connector is.
        """
        running = sorted(
            (
                run
                for run in record.tool_runs.values()
                if run.state == _RUN_RUNNING
            ),
            key=lambda run: run.started_at,
        )
        if not running:
            return []
        now = time.monotonic()
        lines: list[list[dict[str, Any]]] = []
        for run in running[:_MAX_RUNNING_TOOL_LINES]:
            lines.append(
                [
                    {"type": "emoji", "name": _RUN_STATE_EMOJI_BY_STATE[_RUN_RUNNING]},
                    {"type": "text", "text": " "},
                    {"type": "text", "text": run.label, "style": {"code": True}},
                    {
                        "type": "text",
                        "text": f"  {cls._format_elapsed(now - run.started_at)}",
                    },
                ]
            )
        hidden = len(running) - _MAX_RUNNING_TOOL_LINES
        if hidden > 0:
            lines.append([{"type": "text", "text": f"and {hidden} more"}])
        return lines

    @classmethod
    def _error_card(cls, record: "_SlackActivityRecord") -> dict[str, Any] | None:
        """The failure that ended the turn. ``None`` when none did.

        A section of its own, appended rather than folded into any of the
        others. What ends a turn this way -- a stream that stopped arriving, a
        tool loop the runtime abandoned, a cancelled run -- usually belongs to
        the turn itself and to nothing on the card: the model call that timed
        out is not a tool call and not a subagent, and there is no section it
        could be attributed to without inventing the attribution. So nothing
        already on the card is marked failed; a task that says what actually
        happened is added beside them.

        The error text is the one piece of unvetted text this card shows, and
        it is here because a failure nobody can read is the thing this whole
        section exists to prevent. It goes in a preformatted block, where a
        stack trace stays a stack trace, and it is trimmed to fit the field
        rather than taking the message down with it.
        """
        if not record.failed:
            return None
        card: dict[str, Any] = {
            "type": "task_card",
            # Namespaced like every other section: the id space is shared with
            # whatever else posts cards, and "cron-" is the other occupant.
            "task_id": f"error-{record.request_id}",
            "title": _ERROR_CARD_TITLE,
            # Set directly rather than derived from run counts: no run failed,
            # the turn did.
            "status": _CARD_STATUS_ERROR,
        }
        card["details"] = cls._error_details(record.harness_error)
        return card

    @staticmethod
    def _error_details(error: str) -> dict[str, Any]:
        """A human line, then the raw error, as the one rich_text a field takes.

        Two elements rather than one: a section for the sentence and a
        preformatted block for the error itself, so that a message which is
        already hard to read is not also reflowed into prose. Both forms were
        confirmed accepted by posting them to a live workspace.

        Never ``None``, unlike the other fields built here: the sentence stands
        on its own, and an error event that held no text at all is still a
        turn that died and still has that much to say.
        """
        text = (error or "").strip()
        elements: list[dict[str, Any]] = [
            {
                "type": "rich_text_section",
                "elements": [{"type": "text", "text": _HARNESS_ERROR_LEAD}],
            }
        ]
        if text:
            elements.append(
                {
                    "type": "rich_text_preformatted",
                    "elements": [
                        {"type": "text", "text": _clamp_harness_error(text)}
                    ],
                }
            )
        return {"type": "rich_text", "elements": elements}

    @classmethod
    def _activity_title(cls, record: "_SlackActivityRecord") -> str:
        """The plan's own line: that the turn is working, or how it ended.

        A bare string, whatever Slack's own table says the field takes. The
        plan's ``status`` is not set: it is undocumented, accepted, and inert --
        the client derives the chip from the children -- so the terminal wording
        lives here instead.

        What it reports is harness-side and nothing more. "Turn finished" says
        the turn ended, not that the work was any good; "n failed" says a tool
        told the runtime it had failed, not that a subagent's task went badly;
        and "Turn failed" says the turn itself died -- a stream that stopped, a
        loop the runtime gave up on -- which is a wider thing than any count of
        tools and is why it replaces the stem instead of joining the notes.
        "Turn stopped" is the same width and a different fact: somebody ended
        it on purpose, and it is checked first because a stop is the one ending
        this connector watched happen rather than inferred from an event.

        The notes are unchanged by any of that. A tool still running when the
        stop landed is counted as having reported no result, never as having
        failed; see ``_RUN_UNREPORTED`` for why those are different counts.
        """
        if not record.closed:
            return _ACTIVITY_TITLE_WORKING
        totals: dict[str, int] = {}
        for counts in (record.subagent_totals(), record.tool_totals()):
            for state, count in counts.items():
                totals[state] = totals.get(state, 0) + count
        notes: list[str] = []
        failed = totals.get(_RUN_FAILED, 0)
        if failed:
            notes.append(f"{failed} failed")
        unreported = totals.get(_RUN_UNREPORTED, 0)
        if unreported:
            notes.append(f"{unreported} reported no result")
        if record.stopped:
            stem = _ACTIVITY_TITLE_STOPPED
        elif record.failed:
            stem = _ACTIVITY_TITLE_FAILED
        else:
            stem = _ACTIVITY_TITLE_DONE
        if not notes:
            return stem
        return f"{stem}, " + ", ".join(notes)

    @classmethod
    def _activity_summary(cls, record: "_SlackActivityRecord") -> str:
        """The notification string, which blocks alone would leave blank.

        The card's titles are section labels now, so the aggregate that used to
        be in them is spelt out here instead -- this is what a mobile client
        shows and what a search result reads as.

        It is also the only part of the message Slack cannot refuse. A turn that
        died posts nothing else now, so a clamped line of the error goes in here
        too: if the blocks are rejected and this message arrives as bare text,
        the reader still learns that the turn failed and roughly why.
        """
        parts = [cls._activity_title(record)]
        compression = cls._compression_summary(record)
        if compression:
            parts.append(compression)
        thinking = cls._thinking_summary(record)
        if thinking:
            parts.append(thinking)
        todo_total = sum(record.todo_counts.values())
        if todo_total:
            done = record.todo_counts.get(_TODO_COMPLETED, 0)
            noun = "todo" if todo_total == 1 else "todos"
            parts.append(f"{done} of {todo_total} {noun} done")
        parts.extend(
            summary
            for summary in (
                cls._runs_summary(record.subagent_totals(), "subagent"),
                cls._runs_summary(record.tool_totals(), "tool"),
            )
            if summary
        )
        if record.harness_error:
            # Last, and on one line: the counts are short and fixed and the
            # error is neither, so anything after it in a truncated preview
            # would be lost rather than shortened.
            parts.append(
                _clamp(
                    " ".join(record.harness_error.split()),
                    _MAX_SUMMARY_ERROR_LENGTH,
                )
            )
        return " - ".join(parts)

    @staticmethod
    def _runs_summary(counts: Mapping[str, int], noun: str) -> str:
        """``2 of 3 subagents running`` and friends, or ``""`` for nothing to say.

        A failure and a run nobody heard from are named separately rather than
        both called "failed": they reach the same chip, as ``_runs_card_status``
        says, so only the words can state the distinction.
        """
        total = sum(counts.values())
        if not total:
            return ""
        plural = noun if total == 1 else f"{noun}s"
        running = counts.get(_RUN_RUNNING, 0)
        if running:
            return f"{running} of {total} {plural} running"
        failed = counts.get(_RUN_FAILED, 0)
        if failed:
            return f"{failed} of {total} {plural} failed"
        unreported = counts.get(_RUN_UNREPORTED, 0)
        if unreported:
            return f"{unreported} of {total} {plural} reported no result"
        return f"{total} {plural} finished"

    @classmethod
    def _activity_blocks(
        cls, record: "_SlackActivityRecord"
    ) -> list[dict[str, Any]] | None:
        """The whole card: a plan wrapping whichever sections have anything in them.

        Always a plan, even around a single card. Nested in one, a task_card may
        be ``pending``; standalone that value is refused outright, and dropping
        the wrapper for the one-section case would mean the same turn rendered
        two different ways and the queued state disappearing in one of them.

        Nothing here reads a result, an argument, a brief or a line of
        reasoning: every section is assembled from how many runs are in which
        state, from the labels collected when they started, and from durations
        this connector clocked itself. That is what keeps the card short and
        safe whatever the turn was asked to do, and it is why the thinking
        section shows a number and no text.
        """
        cards = [
            card
            for card in (
                # First, because it happens before the turn starts: compaction
                # runs inside get_context_window, ahead of the model call that
                # the thinking section is the first sign of. It is also the only
                # section that can be on the card before anything else exists to
                # put there, which is the case it was written for.
                cls._compression_card(record),
                # Then the first sign of the model itself, and the section that
                # exists for the case where none of the ones below it do: a
                # reader scanning a card that appeared out of a silent thread is
                # looking for whether anything is happening at all, and that
                # answer should not be below three sections that are empty.
                cls._thinking_card(record),
                cls._todo_card(record),
                cls._subagent_card(record),
                cls._tool_card(record),
                # Last, because it is the last thing that happened, and because
                # a turn that died having done nothing else is a plan of one
                # section -- which is the whole reason this one can stand alone.
                cls._error_card(record),
            )
            if card is not None
        ]
        if not cards:
            return None
        blocks: list[dict[str, Any]] = [
            {
                "type": "plan",
                "title": _clamp(cls._activity_title(record), _MAX_CARD_TITLE_LENGTH),
                "tasks": cards,
            }
        ]
        blocks.extend(cls._stop_blocks(record))
        return blocks

    @classmethod
    def _card_shows_only_thinking(cls, record: "_SlackActivityRecord") -> bool:
        """Whether the card as it now stands holds the thinking section alone.

        Asked of the rendered card rather than of the record's history, and it
        is the same render the message would have been rewritten with, so the
        answer is exactly what a reader would be left looking at. Nothing here
        edits the card: this decides whether the message stays or goes, and the
        one caller either deletes the whole of it or leaves every section it has
        untouched.

        Read off ``_activity_blocks`` instead of re-testing the fields each
        section is built from, so the two cannot drift. A sixth section added
        later appears in ``tasks`` without anybody remembering this, and the
        card it appears on keeps its place -- which is the safe direction to
        fail in: the cost of wrongly keeping a card is a line of chrome, and
        the cost of wrongly deleting one is a turn that vanishes.

        ``False`` for a card with nothing on it at all. A record that renders to
        no blocks has no card to take down, and ``None`` here means the delayed
        post would have found nothing worth posting.

        **A failed turn can never answer ``True``**, without a test for it
        anywhere in this method. ``_error_card`` is one of the sections, and the
        caller sets ``failed`` on the record before it asks, so the plan holds
        two cards and the length check refuses it. That is the first of the
        three guards standing between a failure and a deleted card -- see
        ``_close_activity_card``.
        """
        blocks = cls._activity_blocks(record)
        if blocks is None:
            return False
        # The plan is always the first block and the sections are always its
        # tasks; a stop button, when there is one, is a block beside it and not
        # a section. Anything else in that slot has no ``tasks``, reads as zero
        # sections and keeps the card, which is the direction to be wrong in.
        tasks = blocks[0].get("tasks") or []
        if len(tasks) != 1:
            return False
        return str(tasks[0].get("task_id") or "").startswith(
            _THINKING_TASK_ID_PREFIX
        )

    @classmethod
    def _stop_blocks(cls, record: "_SlackActivityRecord") -> list[dict[str, Any]]:
        """The stop button, or nothing, for the card as it currently stands.

        Rendered on the card because the card is the per-turn surface: it is
        posted while the turn is working, rewritten as it goes, and settled when
        it ends, which is exactly the button's lifetime. A turn that finishes
        inside ``activity_card_delay_seconds`` never gets one, which is the
        intended outcome rather than a gap -- nobody needs to stop a turn that
        is already over -- and a deployment with ``activity_card`` off has no
        stop button at all, which is a real limitation and is why the record of
        who started a turn is deliberately kept somewhere else.

        Withheld in three states, all of them "there is nothing here to stop":
        a turn that has ended, one whose stop has already been dispatched, and
        one whose session this connector cannot name.

        **Not withheld during a context compaction**, which is the one stretch
        of a turn where the card can now appear before the model has been called
        at all. A button that draws and does nothing is worse than no button, so
        the path was read before it was allowed: ``chat.interrupt`` reaches
        ``_cancel_scheduler_running_tasks``, which cancels the scheduler's exec
        task; ``get_context_window`` -- and the compressor under it -- is
        awaited directly inside that task; and the compressor's own model call
        is a plain ``await``, with no thread, no executor and no shield between
        the two. The runtime is written for it: it catches the
        ``CancelledError``, reports the pass as ``failed`` with the error
        ``cancelled``, and re-raises. So the compaction section shows the cancel
        landing rather than the card going quiet, and a stop pressed at minute
        four of six does what the reader asked for.
        """
        if record.closed or record.stop_requested or not record.session_id:
            return []
        return [
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "action_id": _STOP_ACTION_ID,
                        "text": {
                            "type": "plain_text",
                            "text": _clamp(
                                _STOP_BUTTON_LABEL, _MAX_BUTTON_TEXT_LENGTH
                            ),
                        },
                        # Danger, because it destroys work. Slack renders it red
                        # and asks nothing further; the confirmation dialog was
                        # considered and left out, because the cost of a stop is
                        # a turn someone starts again and the cost of a dialog
                        # is two clicks on every stop that was meant.
                        "style": "danger",
                        "value": cls._stop_button_value(record),
                    }
                ],
            }
        ]

    @staticmethod
    def _stop_button_value(record: "_SlackActivityRecord") -> str:
        """What the stop button sends back when it is pressed.

        The session is what the cancel is addressed to; the request and the
        channel are what the click is matched back to its record with, and the
        record is what says whether the turn is still running. All three are
        this connector's own ids and none of them is read from model output.
        """
        return json.dumps(
            {
                "session_id": record.session_id,
                "request_id": record.request_id,
                "channel_id": record.channel_id,
            },
            separators=(",", ":"),
        )

    @classmethod
    def _activity_block_kind(cls, record: "_SlackActivityRecord") -> str:
        """Which kind the card's payload is, which its stop button decides.

        A card is chrome around an answer -- a refused rendering costs a status
        line and the reader still gets the reply -- but a card holding a
        control is not: a button's ``value`` is this connector's internals and
        must never be quoted back at a reader, and a control that did not draw
        is a broken function rather than a formatting loss. So the same message
        is one kind or the other depending on whether the button is on it.
        """
        return (
            slack_blocks.BLOCK_KIND_INTERACTIVE
            if cls._stop_blocks(record)
            else slack_blocks.BLOCK_KIND_CHROME
        )

    # -- Tracking -------------------------------------------------------

    def _prune_activity_records(self) -> None:
        """Age out records, and cap how many are held.

        A closed record is history: its card has been written to whatever
        conclusion the turn reached, and it is kept only so a turn resumed
        moments later rebinds to it. So closed records are given up first and
        quietly, and the warning is spent only on evicting one that is still
        live -- which is the case that actually loses a card.
        """
        cutoff = time.monotonic() - _ACTIVITY_RECORD_TIMEOUT_SECONDS
        for key, entry in list(self._activity_records.items()):
            if entry.touched_at < cutoff:
                if entry.post_timer is not None:
                    entry.post_timer.cancel()
                del self._activity_records[key]
        while len(self._activity_records) >= _MAX_ACTIVITY_RECORDS:
            closed = [
                key
                for key, entry in self._activity_records.items()
                if entry.closed
            ]
            candidates = closed or list(self._activity_records)
            oldest = min(
                candidates,
                key=lambda key: self._activity_records[key].touched_at,
            )
            if not closed:
                logger.warning(
                    "Slack dropped the oldest of too many tracked turns: %s",
                    oldest,
                )
            entry = self._activity_records.pop(oldest)
            if entry.post_timer is not None:
                entry.post_timer.cancel()

    def _remember_activity_alias(self, resumed_id: str, original_id: str) -> None:
        """Record that ``resumed_id`` carries on the request ``original_id``.

        Resolved transitively at registration rather than at lookup, so a
        request interrupted three times collapses to the id it started under
        rather than to a chain that has to be walked.
        """
        if not resumed_id or not original_id:
            return
        original_id = self._activity_request_aliases.get(original_id, original_id)
        if resumed_id == original_id:
            return
        while len(self._activity_request_aliases) >= _MAX_ACTIVITY_REQUEST_ALIASES:
            self._activity_request_aliases.pop(
                next(iter(self._activity_request_aliases))
            )
        self._activity_request_aliases[resumed_id] = original_id

    def _activity_key(
        self, msg: Message, routing_target: RoutingTarget | None
    ) -> tuple[str, str] | None:
        """``(request id, channel)`` for the request this event belongs to.

        The request, not the turn. A turn is what the wire has ids for, and for
        an uninterrupted request the two are the same thing; but a permission
        prompt answered from Slack resumes the work as a fresh request, and
        counting per turn told a reader who asked for two subagents that one
        had finished, twice, and separately. So a resumed id is mapped back to
        the id its request started under before the key is built.
        """
        request_id = str(msg.id or "").strip() or str(msg.session_id or "").strip()
        if not request_id:
            return None
        request_id = self._activity_request_aliases.get(request_id, request_id)
        channel_id, _ = self._extract_delivery(msg, routing_target)
        if not channel_id:
            return None
        return request_id, channel_id

    def _activity_record(
        self, key: tuple[str, str], msg: Message, routing_target: RoutingTarget | None
    ) -> "_SlackActivityRecord":
        """The record for ``key``, created if this is the turn's first activity."""
        session_id = str(getattr(msg, "session_id", "") or "").strip()
        record = self._activity_records.get(key)
        if record is None:
            self._prune_activity_records()
            record = _SlackActivityRecord(
                request_id=key[0],
                channel_id=key[1],
                thread_ts=self._extract_delivery(msg, routing_target)[1],
                session_id=session_id,
            )
            self._activity_records[key] = record
        elif not record.session_id and session_id:
            # Filled in rather than only set at creation, because the first
            # event of a turn is not guaranteed to hold the session and a later
            # one may. A record that never learns it simply renders no stop
            # button; one that learns it late gets the button on the next write.
            record.session_id = session_id
        return record

    def _retire_tool_run(self, record: "_SlackActivityRecord", call_id: str) -> None:
        """Fold one tool run into the counts and stop holding the run itself."""
        run = record.tool_runs.pop(call_id, None)
        if run is None:
            return
        counts = record.retired_tools.setdefault(run.label, {})
        counts[run.state] = counts.get(run.state, 0) + 1

    def _note_tool_call(
        self, record: "_SlackActivityRecord", call_id: str, label: str
    ) -> None:
        """Start clocking one tool call.

        A run that is already held is left as it is: a resumed turn replays the
        calls it was interrupted in the middle of, and counting those again
        would inflate the totals of the request they belong to. The one
        exception is a run the turn's end marked ``unreported`` -- a permission
        prompt ends the turn between a call and its result, and the resumed turn
        replaying that call is what re-establishes that it is live.
        """
        existing = record.tool_runs.get(call_id)
        if existing is not None:
            if existing.state == _RUN_UNREPORTED:
                existing.state = _RUN_RUNNING
                existing.started_at = time.monotonic()
            return
        while record.tool_runs and len(record.tool_runs) >= _MAX_TOOL_RUNS:
            # Long turns run hundreds of tools, so the held runs are the recent
            # ones and everything older survives only as counts. Finished runs
            # are given up first: a running one is still needed to pair with its
            # result and to say how long it has been out.
            finished = [
                held
                for held, run in record.tool_runs.items()
                if run.state != _RUN_RUNNING
            ]
            oldest = min(
                finished or list(record.tool_runs),
                key=lambda held: record.tool_runs[held].started_at,
            )
            self._retire_tool_run(record, oldest)
        record.tool_runs[call_id] = _SlackActivityRun(call_id=call_id, label=label)

    async def _track_activity_event(
        self, msg: Message, routing_target: RoutingTarget | None
    ) -> bool:
        """Fold one progress event into the turn's card.

        Returns whether the event was consumed. Tool calls, results, subagent
        updates, todos, reasoning and compaction all describe progress. None
        is an answer to post into the channel. Reasoning contains model-only
        text, while a failed compaction can contain an error string. The card
        reads their state and consumes them without relaying their content.

        The compaction state is matched on its payload rather than on
        ``msg.event_type``, which is the one the gateway leaves
        unset: ``EventType`` has no member for it. That is also why it is the
        one that is not in ``_INTERMEDIATE_STREAM_EVENTS`` -- a
        frozenset of enum members cannot hold it -- and therefore the one that
        was never dropped. See ``_track_compression_event``.

        Deliberately not gated on ``enable_streaming``. That flag asks for the
        answer to be typed out as it is written; this is the opposite case, a
        turn that produces no text at all for minutes, and gating the card on it
        would withhold the card precisely from the channels that cannot see
        anything else either.
        """
        if msg.event_type == EventType.CHAT_SUBTASK_UPDATE:
            return await self._track_persistent_subagent_event(msg, routing_target)
        if msg.event_type == EventType.TODO_UPDATED:
            return await self._track_todo_event(msg, routing_target)
        if msg.event_type == EventType.CHAT_REASONING:
            return await self._track_reasoning_event(msg, routing_target)
        if self._compression_event(msg) is not None:
            return await self._track_compression_event(msg, routing_target)
        if msg.event_type not in (
            EventType.CHAT_TOOL_CALL,
            EventType.CHAT_TOOL_RESULT,
        ):
            return False

        call: tuple[str, str, Any] | None = None
        outcome: tuple[str, str, str] | None = None
        if msg.event_type == EventType.CHAT_TOOL_CALL:
            call = self._tool_call_event(msg)
            if call is None:
                return False
            name = call[0]
        else:
            outcome = self._tool_result_event(msg)
            if outcome is None:
                return False
            name = outcome[0]
        is_subagent = name == _SUBAGENT_TOOL_NAME
        if name in _PERSISTENT_SUBAGENT_TOOL_NAMES:
            # The roster update carries the real subagent state. A spawn
            # result only confirms enqueueing; wait is a coordination call.
            return True

        # A subagent event is recognised as ours before the feature switch is
        # consulted, so a disabled card still swallows it rather than letting it
        # reach the text path. Every other tool event is left exactly where it
        # was when the card is off.
        if not self.config.activity_card or self._client is None:
            return is_subagent

        key = self._activity_key(msg, routing_target)
        if key is None:
            return is_subagent

        async with self._activity_lock:
            if call is not None:
                _, call_id, arguments = call
                if not call_id:
                    # Without the id there is nothing for the result to close,
                    # and a run that can never leave "running" would hold the
                    # card open until the turn ended. Better not to count it.
                    return True
                record = self._activity_record(key, msg, routing_target)
                # A call reopens whatever the record had reached. The turn this
                # one belongs to is working again, so a card that says the
                # request is done is now wrong whatever it said a moment ago.
                record.reopen()
                # ...and it has stopped thinking, because it is doing this
                # instead. Closing the stretch here rather than only at the end
                # of the turn is what keeps the thinking section from claiming a
                # model is deliberating while the tool section counts up beside
                # it. A later chunk opens a fresh stretch; the total is kept
                # across, so the number never goes backwards.
                record.end_reasoning()
                force = False
                if is_subagent:
                    existing = record.subagent_runs.get(call_id)
                    if existing is None:
                        record.subagent_runs[call_id] = _SlackActivityRun(
                            call_id=call_id,
                            label=self._subagent_type_label(arguments),
                        )
                    elif existing.state == _RUN_UNREPORTED:
                        # The same call, dispatched again after a turn ended
                        # underneath it. It is live once more, and saying so is
                        # what the resumed turn actually established. A run that
                        # did report is left alone: a replayed dispatch does not
                        # unmake a result that already arrived.
                        existing.state = _RUN_RUNNING
                    # A dispatch joining a fan-out already reported as running
                    # says only that the denominator grew, and a wave arriving
                    # over one second would spend an edit per subagent to say
                    # it -- so those coalesce. A dispatch arriving after the
                    # section reached a conclusion does not: the card would
                    # otherwise sit there calling a request finished while its
                    # next subagent runs, which is the same lie in reverse.
                    force = record.subagent_card_status not in (
                        "",
                        _CARD_STATUS_IN_PROGRESS,
                    )
                else:
                    self._note_tool_call(
                        record, call_id, self._run_label(name, _UNKNOWN_TOOL_LABEL)
                    )
                record.touched_at = time.monotonic()
                await self._arm_or_refresh(key, record, force=force)
                return True

            assert outcome is not None
            _, call_id, state = outcome
            record = self._activity_records.get(key)
            if record is None or not call_id:
                # A result for a turn this connector never saw start: the
                # channel was restarted, or the record aged out. There is
                # nothing on screen to correct.
                return True
            run = record.subagent_runs.get(call_id)
            if run is not None:
                run.state = state
                record.touched_at = time.monotonic()
                # A subagent returning is the news the card exists to report, so
                # it is never coalesced away. Withheld, a fan-out of short
                # subagents showed its first frame and its last and nothing in
                # between.
                await self._refresh_activity_card(record, force=True)
                return True
            tool_run = record.tool_runs.get(call_id)
            if tool_run is not None:
                tool_run.state = state
                record.touched_at = time.monotonic()
                # A tool returning is not news in the same way: a turn calls
                # dozens of them, and one edit apiece would spend the channel's
                # whole budget narrating its own construction. These wait out
                # the floor.
                await self._refresh_activity_card(record, force=False)
        return True

    async def _track_persistent_subagent_event(
        self, msg: Message, routing_target: RoutingTarget | None
    ) -> bool:
        """Count persistent instances from their roster, not their tool calls."""
        payload = msg.payload if isinstance(msg.payload, Mapping) else None
        if payload is None or payload.get("dispatch_source") == "task_tool":
            # TaskTool's call/result pair already owns its count.
            return True
        subagent_id = str(payload.get("subagent_id") or "").strip()
        if not subagent_id or not self.config.activity_card or self._client is None:
            return True
        outcome = str(payload.get("turn_outcome") or "").strip().lower()
        status = str(payload.get("status") or "").strip().lower()
        if outcome == "completed":
            state = _RUN_DONE
        elif outcome == "failed":
            state = _RUN_FAILED
        elif outcome == "cancelled":
            state = _RUN_UNREPORTED
        elif status == "running":
            state = _RUN_RUNNING
        elif status == "closed":
            state = _RUN_UNREPORTED
        else:
            # Idle without a reported outcome does not establish a result.
            return True
        key = self._activity_key(msg, routing_target)
        if key is None:
            return True
        async with self._activity_lock:
            record = self._activity_record(key, msg, routing_target)
            run_id = f"persistent:{subagent_id}"
            run = record.subagent_runs.get(run_id)
            previous = run.state if run is not None else None
            if run is None:
                record.subagent_runs[run_id] = _SlackActivityRun(
                    call_id=run_id,
                    label=self._subagent_type_label(payload),
                    state=state,
                )
            elif previous != state:
                # A closed instance must not erase an outcome already shown.
                if status == "closed" and previous in (_RUN_DONE, _RUN_FAILED):
                    return True
                run.state = state
            else:
                return True
            if state == _RUN_RUNNING:
                record.reopen()
                record.end_reasoning()
            record.touched_at = time.monotonic()
            force = state != _RUN_RUNNING or record.subagent_card_status not in (
                "",
                _CARD_STATUS_IN_PROGRESS,
            )
            await self._arm_or_refresh(key, record, force=force)
        return True

    async def _track_reasoning_event(
        self, msg: Message, routing_target: RoutingTarget | None
    ) -> bool:
        """Note that the model is thinking. Never what it is thinking.

        ``msg.payload`` is not read at all -- not for content, not for a length,
        not to decide whether the chunk was worth counting. The event arriving
        is the whole signal, and the clock is this connector's own, exactly as
        it is for a tool call: nothing on the wire says how long anything has
        been going on.

        Consumed only when the card is on. With it off this returns ``False``
        and the event falls through to the gate that has always dropped it, so
        switching the card off restores the previous behaviour rather than
        turning reasoning into a running commentary.

        The refresh is never forced. A chunk is churn by definition -- a long
        turn emits hundreds -- so the duration on screen ticks at whatever
        ``activity_card_min_edit_seconds`` allows, and a turn that finishes
        inside ``activity_card_delay_seconds`` still posts nothing at all.
        """
        if not self.config.activity_card or self._client is None:
            return False
        key = self._activity_key(msg, routing_target)
        if key is None:
            return True
        async with self._activity_lock:
            record = self._activity_record(key, msg, routing_target)
            # Thinking is working: a request whose card had settled is live
            # again, the same as it would be for a replayed tool call.
            record.reopen()
            record.note_reasoning()
            record.touched_at = time.monotonic()
            await self._arm_or_refresh(key, record, force=False)
        return True

    async def _track_compression_event(
        self, msg: Message, routing_target: RoutingTarget | None
    ) -> bool:
        """Fold one ``context.compression_state`` event into the turn's card.

        **Consumed whether or not the card is on**, unlike the reasoning chunk
        beside it, and for the reason a ``task_tool`` event is: with the card
        off this event does not fall through to a gate that drops it, it falls
        through to ``_extract_outgoing_text``, which reads ``error`` off any
        payload that has no ``content``. A compaction that fails or is cancelled
        carries one, so the previous behaviour was to post the bare word
        ``cancelled`` into the channel as a message of its own, with nothing
        beside it to say what had been cancelled. That is not behaviour worth
        restoring when the card is switched off.

        Every state is recorded, terminal and otherwise. A ``noop`` is a
        processor that ran and saved nothing, which is exactly as worth showing
        as one that saved ninety-six per cent -- an operator reading a card with
        two processors on it, one of which recovered nothing, has learnt
        something about the deployment that no other surface tells them.

        The refresh is forced. These arrive a handful of times in a turn, one
        per processor per phase, and each one is a boundary rather than churn:
        the ``started`` is the whole reason the card exists on a turn like this,
        and the terminal state is the moment the wait the reader has been
        watching finally ends. Coalescing either behind the edit floor would
        withhold the card from precisely the minutes it was written for.
        """
        body = self._compression_event(msg)
        if body is None:
            return False
        status = str(body.get("status") or "").strip().lower()
        if status not in _COMPRESSION_STATES:
            # A state this connector has no wording for. Swallowed rather than
            # rendered as an unknown word or let through to the text path, and
            # the pass it belonged to keeps whatever it last said -- which
            # ``settle`` will mark unreported if nothing else arrives.
            return True
        operation_id = str(body.get("operation_id") or "").strip()
        if not operation_id:
            # Without it there is nothing to pair a terminal state with its
            # start, and a second event would open a second line for one pass.
            return True
        if not self.config.activity_card or self._client is None:
            return True
        key = self._activity_key(msg, routing_target)
        if key is None:
            return True
        async with self._activity_lock:
            record = self._activity_record(key, msg, routing_target)
            # Compaction is work, so a request whose card had settled is live
            # again, the same as it would be for a replayed tool call.
            record.reopen()
            # ...and the model is not thinking while the runtime folds up its
            # context. Closing the stretch here keeps the thinking section from
            # claiming a model is deliberating through six minutes of
            # compaction; the total carries across, so the number never goes
            # backwards when it starts again.
            record.end_reasoning()
            self._note_compression(record, operation_id, status, body)
            record.touched_at = time.monotonic()
            await self._arm_or_refresh(key, record, force=True)
        return True

    def _note_compression(
        self,
        record: "_SlackActivityRecord",
        operation_id: str,
        status: str,
        body: Mapping[str, Any],
    ) -> None:
        """Record one pass's state, opening the run if this is its first event.

        A pass is not guaranteed to announce itself: a processor that finds
        nothing to do reports ``noop`` and nothing before it, which is how the
        offloader on the turn this was written for showed up. So the terminal
        event opens the run as readily as the ``started`` one does, and the
        ``before`` figures are taken from whichever arrived first.
        """
        run = record.compression_runs.get(operation_id)
        if run is None:
            while len(record.compression_runs) >= _MAX_COMPRESSION_RUNS:
                record.compression_runs.pop(
                    next(iter(record.compression_runs))
                )
            run = _SlackCompressionRun(
                operation_id=operation_id,
                label=self._run_label(
                    body.get("processor"), _UNKNOWN_COMPRESSION_LABEL
                ),
            )
            record.compression_runs[operation_id] = run
            before = self._compression_metric(body, "before")
            run.before_messages = self._compression_int(before.get("messages"))
            run.before_tokens = self._compression_int(before.get("tokens"))
            percent = before.get("context_percent")
            if isinstance(percent, (int, float)) and not isinstance(percent, bool):
                run.before_percent = int(percent)
        if status == _COMPRESSION_STARTED and run.state != _COMPRESSION_STARTED:
            # A start arriving after the end of the pass it belongs to, which is
            # a reordering rather than a second pass: the operation id is the
            # same. Reopening the line would have the card announce that
            # something already finished is now running.
            return
        run.state = status
        if status == _COMPRESSION_STARTED:
            # Nothing has been saved yet and no duration has been measured, and
            # the event carries neither. Left as they are rather than read, so
            # that a start cannot blank figures a terminal event already wrote.
            return
        saved = self._compression_metric(body, "saved")
        run.saved_messages = self._compression_int(saved.get("messages"))
        run.saved_tokens = self._compression_int(saved.get("tokens"))
        percent_saved = saved.get("percent")
        run.saved_percent = (
            float(percent_saved)
            if isinstance(percent_saved, (int, float))
            and not isinstance(percent_saved, bool)
            else 0.0
        )
        duration = body.get("duration_ms")
        if isinstance(duration, (int, float)) and not isinstance(duration, bool):
            run.duration_ms = max(0, int(duration))
        run.cancelled = (
            str(body.get("error") or "").strip().lower()
            == _COMPRESSION_CANCELLED_ERROR
        )

    async def _track_todo_event(
        self, msg: Message, routing_target: RoutingTarget | None
    ) -> bool:
        """Replace the turn's todo counts from one ``todo.updated`` snapshot."""
        counts = self._todo_snapshot(msg)
        if counts is None:
            return False
        if not self.config.activity_card or self._client is None:
            return False
        key = self._activity_key(msg, routing_target)
        if key is None:
            return True
        async with self._activity_lock:
            record = self._activity_records.get(key)
            if record is None:
                if not counts:
                    # An empty list is what a turn that keeps no todos reports.
                    # There is no section to show and nothing to open a record
                    # for.
                    return True
                record = self._activity_record(key, msg, routing_target)
            record.reopen()
            # Replaced wholesale: the event holds the list, not a change to
            # it, and accumulating snapshots would count one todo once per tool
            # call that touched the list.
            record.todo_counts = counts
            record.touched_at = time.monotonic()
            await self._arm_or_refresh(key, record, force=False)
        return True

    async def _arm_or_refresh(
        self, key: tuple[str, str], record: "_SlackActivityRecord", *, force: bool
    ) -> None:
        """Start the delayed post, or rewrite the card that is already up.

        Called with the lock held.
        """
        if not record.message_ts:
            # Nothing on screen yet, so the delay decides whether there ever
            # will be. Re-armed rather than assumed live: a record kept past the
            # end of a turn has had its timer cancelled.
            if record.post_timer is None:
                record.post_timer = asyncio.create_task(
                    self._post_activity_card_later(key),
                    name="slack-activity-card",
                )
            return
        await self._refresh_activity_card(record, force=force)

    async def _post_activity_card_later(self, key: tuple[str, str]) -> None:
        """Post the card once the turn has outlived the configured delay.

        The delay is the whole answer to "when to emit nothing": a turn that
        finishes inside it is done and forgotten before this wakes, finds no
        record, and leaves the channel exactly as it was. Only a turn that is
        genuinely still working is worth a message, which is the same judgement
        the scheduler makes when it posts a cron placeholder only for a run that
        has not finished by its push time.
        """
        delay = max(0.0, float(self.config.activity_card_delay_seconds or 0.0))
        try:
            await asyncio.sleep(delay)
            # Held across the write, so the dispatch loop cannot fold a result
            # into a record while this is deciding what the card says. The
            # window is one Slack call wide, which is what ``send()`` already
            # spends on every message it posts.
            async with self._activity_lock:
                record = self._activity_records.get(key)
                if record is None:
                    return
                # Cleared before any of the early returns, so that a record kept
                # past a turn that finished inside the delay can arm a fresh
                # timer if the same request starts working again.
                record.post_timer = None
                if record.message_ts or record.closed:
                    return
                if self._client is None:
                    self._activity_records.pop(key, None)
                    return
                await self._post_activity_card(key, record)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.warning(
                "Slack turn activity card failed for request %s",
                key[0],
                exc_info=True,
            )

    async def _post_activity_card(
        self, key: tuple[str, str], record: "_SlackActivityRecord"
    ) -> bool:
        """Write the card for the first time. Returns whether it landed.

        Called with the lock held, by the delayed post and by the one other
        caller that has to put a card on screen without waiting for it: a turn
        that died before the delay elapsed, whose failure has nowhere else to
        go now that it is not posted as a message of its own.

        A failure to post is best effort from end to end when the card is
        chrome around an answer that is still on its way -- failing the turn
        over it would cost the reader the reply as well as the notice -- so it
        is logged and the record given up. The caller is told, because for the
        failure case that answer is never coming and the message has to be
        posted some other way.
        """
        blocks = self._activity_blocks(record)
        if blocks is None:
            return False
        sent, message_ts, error = await self._post_text(
            block_kind=self._activity_block_kind(record),
            channel_id=record.channel_id,
            text=self._activity_summary(record),
            thread_ts=record.thread_ts,
            blocks=blocks,
        )
        if not sent:
            logger.warning(
                "Slack could not post the turn activity card for request %s (%s)",
                record.request_id,
                error,
            )
            self._activity_records.pop(key, None)
            return False
        record.message_ts = message_ts
        record.remember_written(blocks)
        record.next_edit_at = time.monotonic() + max(
            0.0, float(self.config.activity_card_min_edit_seconds or 0.0)
        )
        return True

    async def _refresh_activity_card(
        self, record: "_SlackActivityRecord", *, force: bool
    ) -> bool:
        """Rewrite the card, or decline to spend the edit.

        ``force`` is what separates news from churn. A subagent reaching a
        terminal state, and the turn ending, are news: withholding them shows
        only a card's first frame and its last where every subagent took a few
        seconds, and at turn end can leave it claiming a finished fan-out is
        still live. A tool call, a todo moving, a dispatch joining a fan-out
        already reported as running are churn, produced by the dozen on a long
        turn, and they wait out ``activity_card_min_edit_seconds``.

        Nothing here ends the record. The fan-out emptying is not the turn
        ending -- a model that works through its subagents one at a time empties
        it between every pair -- and a record retired on that produced one card
        per subagent for a single turn.

        Returns whether the message on screen now says what the record says.
        ``False`` for a write that was declined as well as for one that failed:
        the one caller that reads this is deciding whether a failure has been
        delivered, and a card that was not rewritten has not delivered it
        however good the reason.
        """
        if not record.message_ts:
            # Still inside the delay, so there is nothing on screen to edit.
            # Whether anything is ever posted is the delayed task's decision,
            # taken against the record as it stands when the delay is up.
            return False
        now = time.monotonic()
        if not force and now < record.next_edit_at:
            return False
        blocks = self._activity_blocks(record)
        if blocks is None:
            return False
        sent, _, error = await self._post_text(
            block_kind=self._activity_block_kind(record),
            channel_id=record.channel_id,
            text=self._activity_summary(record),
            thread_ts=record.thread_ts,
            update_ts=record.message_ts,
            blocks=blocks,
        )
        if not sent:
            logger.warning(
                "Slack could not update the turn activity card for request "
                "%s (%s); leaving the card as it was",
                record.request_id,
                error,
            )
        else:
            # Only a write that landed changes what the message says, and the
            # next dispatch reads this to decide whether the card on screen
            # still contradicts the record.
            record.remember_written(blocks)
        record.next_edit_at = now + max(
            0.0, float(self.config.activity_card_min_edit_seconds or 0.0)
        )
        return sent

    async def _close_activity_card(
        self,
        msg: Message,
        routing_target: RoutingTarget | None,
        *,
        withheld: bool = False,
    ) -> bool:
        """Settle the request's card when the turn ends, or take it down.

        This is what ends a card, rather than the work emptying out: a turn that
        dispatches its subagents one after another is at zero running between
        every pair of them, and closing on that posted a card apiece.

        Runs still open here are marked ``unreported`` rather than counted as
        finished -- nothing established that they were -- and the rewrite that
        says so is never withheld, because a card claiming a turn is running
        after it has gone lies for as long as anyone scrolls past it.

        How the turn ended is read here rather than discarded. A ``chat.error``
        is the turn dying -- the stream stopped arriving, the runtime abandoned
        a tool loop, the run was cancelled -- and a card chipping "complete" over
        that is worse than no card: whoever is waiting reads "finished" and stops
        watching. The failure is written into the card and the return value says
        whether it landed. Only a card that shows it lets the caller stop posting
        the error as a message of its own.

        A failure with no card yet forces a second write. A turn that timed out
        on its first model call called no tool, kept no todo and delegated
        nothing, so there is no record and nothing on screen to amend; and a
        message may hold a plan or bare task cards but never both, so the failure
        cannot be appended to the reply. It gets a card of its own.

        The record is kept afterwards, marked closed. A turn resumed after an
        interrupt is a new request on the wire but the same request to the person
        who asked, and it rebinds to this card instead of posting a second one.
        Kept only when a card was actually posted: with nothing on screen there
        is nothing to rebind to, and the point of the delay is that a turn nobody
        had time to notice leaves no trace.

        ``withheld`` says the turn ended cleanly and is posting no message at
        all, and turns the settling rewrite into a deletion. A card reading
        "complete" with no answer beside it is a turn announcing that it ran and
        chose to say nothing, which is the noise ``delivery.reply: optional``
        exists to spare the room; the reaction on the message the turn answered
        stays, and is the whole trace a silent turn leaves. The record goes with
        the card, for the reason the delay case gives one line above: with
        nothing on screen there is nothing for a resumed request to rebind to,
        so it opens a fresh card rather than editing a message that is gone.

        **A card whose only section is the thinking one goes the same way**,
        whatever the reply said. That section exists for the turn that does
        nothing else -- it is the liveness signal for a model thinking for a
        minute over an empty thread, which is worth a card *while* it is
        happening. Once the turn has ended the card says "thought for 12s" and
        nothing a reader cannot already infer from the answer sitting beside it,
        and the duration is the gap between the question and the reply. So the
        card earns its place during the turn and loses it at the end, and in a
        room the bot reads all day what is left is residue.

        The question is asked of the card rather than of the turn's history --
        ``_card_shows_only_thinking``, against the render, after ``settle`` --
        and it is the whole message or nothing. A card that also holds tools,
        todos, subagents or an error keeps every section it has, unedited: this
        decides whether a message stays on screen, never what is on it.

        **A failed turn can never be taken down**, and not as a matter of
        care. Both reasons refuse one on their own account before ``discard`` is
        assembled: ``_turn_says_nothing`` answers ``False`` for anything that is
        not a ``chat.final``, and ``_card_shows_only_thinking`` answers ``False``
        for a failed record because ``_error_card`` is one of the sections it
        counts and ``failed`` is set above. The pair is then folded into
        ``discard`` beside ``failed`` in a single ``and``, so the reasons cannot
        drift apart or grow a path around it; and ``_discard_activity_card``
        refuses a record carrying a failure a third time. The stakes are why it
        is guarded at every step: this method's return value is what lets the
        caller stop posting the error as a message of its own, so a deleted card
        on a failed turn would suppress the failure outright and the reader
        would watch the turn vanish. A discard therefore returns ``False`` --
        nothing was delivered -- and the one path that returns ``True`` still
        requires a rewrite that landed.

        A stopped turn keeps its card too, and needs no guard of its own: the
        stop settles the record itself, so this returns at the ``closed`` check
        above before ``discard`` is ever reached -- which holds for both reasons,
        neither being consulted until after that check.
        """
        failed = msg.event_type == EventType.CHAT_ERROR
        # The very text ``send`` would have posted, so that suppressing that
        # message can lose nothing: whatever renders to no message here is
        # rendering to no message there either.
        error_text = self._extract_outgoing_text(msg) if failed else ""
        if not self.config.activity_card or self._client is None:
            return False
        key = self._activity_key(msg, routing_target)
        if key is None:
            return False
        async with self._activity_lock:
            record = self._activity_records.get(key)
            if record is not None and record.closed:
                # Already settled. A second terminal event for the same request
                # -- an error after a final, a replayed close -- has nothing
                # left to say and must not spend an edit saying it. The error
                # still has to reach the channel, so it goes out as a message.
                return False
            if record is None:
                if not failed:
                    # A turn that finished having done nothing this card
                    # reports. There was never anything to settle.
                    return False
                # A turn that died before it did any tracked work at all: no
                # tool call, no todo, no dispatch, so no record was ever opened.
                # One is opened now, for the card that shows the failure.
                record = self._activity_record(key, msg, routing_target)
            if failed:
                record.failed = True
                record.harness_error = error_text
            # Only the runs still open are touched, and a harness error is not
            # attributed to any of them: the model call that timed out is not a
            # tool call, and chipping one that came back fine would be a lie
            # someone will act on. ``settle`` is what says that, and the stop
            # path says it the same way.
            record.settle()
            if not record.message_ts:
                if not failed:
                    # Nothing was ever posted: the turn finished inside the
                    # delay. Leaving the channel untouched is the intended
                    # outcome, not a missed one -- and it is also the whole of
                    # what a withheld turn wants, reached without a call.
                    self._activity_records.pop(key, None)
                    return False
                # A turn that died inside the delay is the opposite case. The
                # silence was going to be the whole of it, and the failure is
                # exactly the thing worth a message.
                return await self._post_activity_card(key, record)
            # The two reasons a card comes down, in one expression and gated
            # once. Taken here rather than at the top because the second of them
            # is a question about the card, which needs the record, and needs it
            # settled: ``settle`` closes the open stretch of reasoning and marks
            # the runs nobody heard from, so this reads the card in the state it
            # would have been rewritten into. Folded beside ``failed`` in a
            # single ``and`` so that no later edit can give either reason a path
            # around it.
            discard = (
                withheld or self._card_shows_only_thinking(record)
            ) and not failed
            if discard and await self._discard_activity_card(
                key, record, reason="silence" if withheld else "thinking only"
            ):
                return False
            # Reached by a turn that spoke, and by one whose delete the
            # workspace refused. The rewrite is owed in both cases -- a card
            # left claiming a finished turn is live is the one failure this
            # whole record prevents, and it is worse than the card a discard
            # wanted gone -- but only a failure that reached the card is a
            # failure this reports as delivered.
            written = await self._refresh_activity_card(record, force=True)
            return failed and written

    def _turn_says_nothing(
        self, msg: Message, routing_target: RoutingTarget | None
    ) -> bool:
        """Whether this terminal event will post no message at all.

        The question ``send`` answers three screens further down, asked early so
        that ``_close_activity_card`` can act on it. Everything it reads is on
        the event or in the config -- no API call, no state -- so asking twice
        costs nothing and asking here cannot change what the delivery path goes
        on to decide.

        **Only a ``chat.final``.** A ``chat.error`` is the turn dying, not the
        turn declining, and the difference is the whole of what stops a failure
        being deleted off the screen. It is tested first and by event type
        rather than by anything the payload happens to say, so no reply text can
        reach past it. A stop produces no terminal event at all and never
        arrives here.

        **Only under the contract.** ``_reply_was_optional`` is the same pair of
        answers the matcher consults, so a room that never licensed a silence
        cannot have a card deleted on one. Outside the contract a reply that
        renders to nothing is not a judgement anybody made -- it is an event
        that came back wrong -- and the card is then the only record that the
        turn ran at all, which is worth keeping for exactly that reason.

        **Every way the reply renders to nothing, not just the token.** The
        sentinel is the licensed form, and a turn that answered with whitespace
        or with a Block Kit marker and no text reaches the same two returns in
        ``send`` and posts the same nothing. A rule covering one and not the
        others would leave a card standing on the cases that look identical
        from the channel, so the emptiness checks are mirrored here in the
        order ``send`` applies them.
        """
        if msg.event_type != EventType.CHAT_FINAL:
            return False
        channel_id, _ = self._extract_delivery(msg, routing_target)
        if not self._reply_was_optional(msg, channel_id):
            return False
        content, _ = self._extract_block_request(self._extract_outgoing_text(msg))
        return not content or is_no_reply_sentinel(content)

    async def _discard_activity_card(
        self,
        key: tuple[str, str],
        record: "_SlackActivityRecord",
        *,
        reason: str,
    ) -> bool:
        """Take a card off the screen. Returns whether it went.

        ``reason`` is written to the log and read nowhere, and it is required
        rather than defaulted: two different judgements delete a card -- a turn
        that withheld its reply and a card whose only section is the thinking
        one -- so a default would name one of them and be silently wrong for the
        other. A line saying only that a card was deleted leaves the question a
        reader would actually ask unanswerable.

        The whole message goes or none of it does. Nothing here rewrites a card
        or drops a section from one: a card that holds anything besides the
        thinking section keeps every part of it, unedited.

        Called with the lock held, which is what makes the delete safe without
        the wait ``_discard_streamed_preview`` puts in front of its own. A
        preview is written by a debounced task outside any lock, so that path
        closes the stream first to keep a write from landing on a message that
        has gone; every write to a card is already made under
        ``_activity_lock``, and the delayed post cancels itself on ``settle``,
        so holding the lock across the delete is the same guarantee reached for
        nothing.

        **A record carrying a failure is refused here**, independently of the
        two gates the caller has already passed. Nothing on the wire resumes a
        request after an error and ``failed`` is sticky, so this should be
        unreachable -- which is the point of writing it down: the cost of it
        becoming reachable is a harness error with no card and no message, and
        a reader watching their turn disappear.

        Best effort, like the preview delete and like every other write around
        the card that is not the reply itself. A workspace that refuses the
        delete gets the ordinary settling rewrite instead, so the worst outcome
        is the card that stands there today.
        """
        if record.failed or record.harness_error:
            logger.error(
                "[SlackChannel] refusing to delete the activity card of a"
                " failed turn: request=%s channel=%s",
                record.request_id,
                record.channel_id,
            )
            return False
        if self._client is None or not record.message_ts:
            return False
        channel_id, message_ts = record.channel_id, record.message_ts
        try:
            await self._client.chat_delete(channel=channel_id, ts=message_ts)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[SlackChannel] the turn's activity card could not be deleted"
                " and is still on screen: channel=%s ts=%s discard=%s reason=%s",
                channel_id,
                message_ts,
                reason,
                slack_failure_reason(exc),
            )
            return False
        record.message_ts = ""
        self._activity_records.pop(key, None)
        logger.info(
            "[SlackChannel] activity card deleted (%s): channel=%s ts=%s",
            reason,
            channel_id,
            message_ts,
        )
        return True

    async def _post_root(
        self,
        *,
        channel_id: str,
        text: str,
        thread_ts: str,
        update_ts: str,
        blocks: list[dict[str, Any]] | None,
        chunk_index: int,
        chunk_total: int,
        cron: _SlackCronPush | None,
        block_kind: str = slack_blocks.BLOCK_KIND_UNKNOWN,
    ) -> tuple[bool, str, str]:
        """Write the reply's first message, rewriting a placeholder if there is one.

        The fallback for a refused rewrite lives here, and it is the same code
        path as "there was no placeholder": a message that cannot be edited --
        deleted by a user, aged past what Slack will amend, or refused after the
        rate-limit retries ran out -- is posted fresh.
        """
        sent, message_ts, error = await self._post_text(
            channel_id=channel_id,
            text=text,
            thread_ts=thread_ts,
            update_ts=update_ts,
            chunk_index=chunk_index,
            chunk_total=chunk_total,
            blocks=blocks,
            block_kind=block_kind,
        )
        if not sent and update_ts and cron is not None:
            logger.warning(
                "Slack refused to rewrite the record for cron run %s (%s); "
                "posting the result as a new message",
                cron.run_id,
                error,
            )
            sent, message_ts, error = await self._post_text(
                channel_id=channel_id,
                text=text,
                thread_ts=thread_ts,
                update_ts="",
                chunk_index=chunk_index,
                chunk_total=chunk_total,
                blocks=blocks,
                block_kind=block_kind,
            )
        if not sent:
            return False, "", error
        # An edit answers with the ts it edited; keep it either way, since it
        # is what the rest of the reply threads under.
        message_ts = message_ts or update_ts
        if cron is not None and cron.is_placeholder and message_ts:
            self._remember_cron_record(cron, message_ts)
        return True, message_ts, ""

    async def _finish_streamed_reply(
        self,
        surface: _SlackStreamingSurface,
        *,
        handle: str,
        channel_id: str,
        thread_ts: str,
        content: str,
        block_request: bool | None,
    ) -> bool:
        """Append the reply's tail, stop the stream, and report the delivery.

        ``True`` when the reply is on screen in full and ``send()`` has nothing
        left to do; ``False`` when the streamed message turned out to be
        unusable for this answer and the whole of it should be posted fresh.
        Raises ``SlackDeliveryError`` when neither happened -- which is the whole
        point of the method, and the reason the closing call is made from here
        rather than left to the session that drives the surface. ``send()``'s
        contract is that a reply either lands or raises, and moving the close
        into the surface must not quietly turn a failed answer into a log line.

        An append is additive, so a failure here is not the edit path's failure.
        Everything appended is on screen and correct, and only the part not yet
        appended is at risk. A refused close therefore posts that remainder as an
        ordinary message rather than giving up on the answer, and the raise is
        kept for the case where that fails too. A reader who pressed stop gets
        the same treatment: the stream will take nothing more, and the rest of
        the answer is owed to them anyway.

        ``content`` is the reply as the model wrote it, deliberately: it is
        matched against what the surface appended, which is also unconverted,
        and it is what the remaining ``markdown_text`` will hold. ``send()``'s
        normalised parts would agree with neither.
        """
        # One message, always. The thread-details marker splits a reply into a
        # root and its replies only when there is no thread to put them in, and
        # a reply with no thread is one ``_cannot_stream`` refused to stream --
        # so a streamed reply is by construction one that the posting path would
        # have flattened too. ``thread_ts`` is taken as an argument all the same,
        # because it is where the tail goes if the close cannot take it.
        parts = [
            piece.strip()
            for piece in self._split_threaded_report(content)
            if piece.strip()
        ]
        root = "\n\n".join(parts)
        remainder = self._streamed_remainder(surface.sent_text, root)
        if remainder is None:
            # The finished answer is not a continuation of what was streamed --
            # a runtime that rewrote its reply rather than extending it. Nothing
            # can be appended that would make the message right, so it is taken
            # out of streaming state and the answer is posted whole below it.
            logger.warning(
                "[SlackChannel] the finished reply does not continue what was "
                "streamed: channel=%s ts=%s streamed_chars=%d final_chars=%d",
                channel_id,
                handle or "-",
                len(surface.sent_text),
                len(root),
            )
            await surface.stop_stream(handle)
            return False

        chunks = self._streamed_chunks(surface, remainder, block_request)
        if surface.failed:
            # Already unusable before the close -- a reader's stop, or an append
            # that was refused. Stopping is still attempted so the message does
            # not sit in streaming state for good, but nothing is entrusted to
            # it.
            await surface.stop_stream(handle)
            stopped = False
        else:
            stopped = await surface.stop_stream(
                handle,
                chunks=chunks,
                plain_chunks=surface._markdown_chunks(remainder),
            )

        if not stopped:
            stopped = await self._recover_streamed_reply(
                surface,
                handle=handle,
                channel_id=channel_id,
                thread_ts=thread_ts,
                remainder=remainder,
                block_request=block_request,
            )

        # ``one_message`` rather than ``stopped``: by this point the reply has
        # been delivered either way, and what is worth knowing in the log is
        # whether the reader is looking at one message or two.
        logger.info(
            "[SlackChannel] streaming finished: channel=%s ts=%s appends=%d "
            "failed=%d tail_chars=%d one_message=%s",
            channel_id,
            handle or "-",
            surface.edits,
            surface.edit_failures,
            len(remainder),
            "yes" if stopped else "no",
        )
        return True

    async def _recover_streamed_reply(
        self,
        surface: _SlackStreamingSurface,
        *,
        handle: str,
        channel_id: str,
        thread_ts: str,
        remainder: str,
        block_request: bool | None,
    ) -> bool:
        """Deliver what the close could not. ``True`` if the reply is one message.

        Three rungs, best first, because they differ in what the reader ends up
        looking at rather than in how likely they are to work.

        **Rewrite the message.** A stream that has expired releases its message
        back to the ordinary API -- chat.update succeeds on an expired stream
        and is refused on a live one -- so the whole answer can replace what was
        streamed and the reply stays a single message. Only when the whole of it
        still fits an edit, which is a tenth of what a stream would have taken.

        **Retry the close.** A refused edit that names
        ``streaming_state_conflict`` has told us something the failed close did
        not: the message is still in streaming state, so whatever went wrong was
        transient and the close is worth making again -- with the chunks, so the
        rendering survives.

        **Post it beside.** Two messages, which is what the reader gets when the
        answer is too long to fit an edit. Raises if even that fails, and only
        then: the appended part is on screen whatever happens here, so a close
        that failed with nothing left to say has already delivered the reply and
        must not report otherwise. A cron push reporting a failure for an answer
        the reader can see is the false alarm the raise contract exists to avoid,
        from the other side.

        ``remainder`` arrives as the Markdown the stream was being fed and is
        converted on the way past the first rung, because that rung can still
        end up back on the streaming API while everything below it writes
        through ``chat.update`` or ``chat.postMessage``, which take mrkdwn.
        """
        if not remainder.strip():
            logger.warning(
                "[SlackChannel] the stream could not be stopped but the reply "
                "is complete on screen: channel=%s reason=%s",
                channel_id,
                surface.error or "-",
            )
            return False

        # Not offered to a stream a reader ended. Rewriting the message whole
        # would put the answer they stopped back on screen in place of the part
        # they let through, erasing the evidence that they stopped it at all.
        # The rest of the answer is still owed to them -- a person pressing stop
        # is not asking for the work to be thrown away -- but it goes in a
        # message of its own, underneath, where it reads as a continuation
        # rather than as the stop having been ignored.
        if not surface.stopped_by_user and await self._rewrite_streamed_message(
            surface,
            handle=handle,
            channel_id=channel_id,
            remainder=remainder,
            block_request=block_request,
        ):
            return True

        chunks = self._split_text(self._normalize_slack_mrkdwn(remainder))
        if not chunks:
            return False
        logger.warning(
            "[SlackChannel] the stream would take no more (%s); posting the "
            "rest of the reply as a message of its own: channel=%s chunks=%d",
            surface.error or "-",
            channel_id,
            len(chunks),
        )
        for index, chunk in enumerate(chunks):
            chunk_blocks, chunk_kind = self._blocks_and_kind_for(chunk, block_request)
            sent, _, error = await self._post_text(
                channel_id=channel_id,
                text=chunk,
                thread_ts=thread_ts,
                chunk_index=index + 1,
                chunk_total=len(chunks),
                blocks=chunk_blocks,
                block_kind=chunk_kind,
            )
            if not sent:
                raise SlackDeliveryError(
                    error,
                    channel_id=channel_id,
                    chunks_sent=index,
                    chunks_total=len(chunks),
                )
        return False

    async def _rewrite_streamed_message(
        self,
        surface: _SlackStreamingSurface,
        *,
        handle: str,
        channel_id: str,
        remainder: str,
        block_request: bool | None,
    ) -> bool:
        """Put the whole answer into the streamed message with one edit.

        Worth trying because the alternative is two messages for one answer.
        A stream releases its message when it expires -- chat.update succeeds on
        an expired stream and is refused on a live one with
        ``streaming_state_conflict`` -- so this is the ordinary closing rewrite
        the edit path makes, arriving late.

        Bounded by what an edit accepts, which is 4,000 characters where an
        append takes 12,000. A longer answer cannot be rewritten at all: the
        call would be refused whole, and refusing it here costs nothing where
        finding out costs a round trip.

        The blocks are always sent, empty if there are none. A streamed message
        stores the blocks its chunks built, so an edit that held text alone
        would leave the old rendering on screen above the new text -- and an
        empty list is how chat.update takes a message's blocks away.

        Both dialects are in play and each is used where it belongs. The edit is
        an ordinary write, so it holds mrkdwn -- including for the part that
        was already streamed, which Slack converted for itself on the way in and
        which has to be converted again here because this call replaces it. The
        retry underneath is back on the streaming API and keeps the Markdown.
        """
        whole = self._normalize_slack_mrkdwn(
            f"{surface.sent_text}{remainder}"
        ).strip()
        if len(whole) > _MAX_SLACK_UPDATE_TEXT_LENGTH:
            return False

        whole_blocks, whole_kind = self._blocks_and_kind_for(whole, block_request)
        sent, _, error = await self._post_text(
            channel_id=channel_id,
            text=whole,
            thread_ts="",
            update_ts=handle,
            blocks=whole_blocks or [],
            block_kind=whole_kind,
        )
        if sent:
            logger.info(
                "[SlackChannel] the expired stream was rewritten whole: "
                "channel=%s ts=%s chars=%d",
                channel_id,
                handle,
                len(whole),
            )
            return True

        if _STREAM_STATE_CONFLICT not in error:
            return False

        # The edit was refused because the message is still streaming, which is
        # the one thing the failed close did not establish -- but it says nothing
        # about whether the close's own failure was transient, and a payload the
        # stream has already refused stays refused. ``resume`` is what knows the
        # difference, and declining to resume leaves the ladder to post instead.
        cleared = surface.resume()
        if not cleared:
            logger.warning(
                "[SlackChannel] the stream is still open but will not take this "
                "close (%s); not asking it twice: channel=%s ts=%s",
                surface.error or "-",
                channel_id,
                handle,
            )
            return False
        logger.info(
            "[SlackChannel] the stream is still open after all (%s); closing it "
            "again: channel=%s ts=%s",
            cleared,
            channel_id,
            handle,
        )
        return await surface.stop_stream(
            handle,
            chunks=self._streamed_chunks(surface, remainder, block_request),
            plain_chunks=surface._markdown_chunks(remainder),
        )

    def _streamed_chunks(
        self,
        surface: _SlackStreamingSurface,
        remainder: str,
        block_request: bool | None,
    ) -> list[dict[str, Any]]:
        """Compose the reply's tail for chat.stopStream.

        Into ``chunks``, never into the method's own ``blocks`` argument: that
        one is documented to render "at the bottom of the finalized message",
        after everything else, and this is body content that belongs where it
        was written. The argument is left for things that genuinely belong last.

        A rendering covers the whole tail or none of it, which is why the
        surface holds back everything from the reply's first table or fence: the
        prose above it has already been appended as text and cannot be taken
        back into a section block beside the table.

        ``remainder`` arrives as the model wrote it and leaves in two dialects.
        A markdown_text chunk keeps it, because that field is Markdown. A blocks
        chunk is Block Kit -- a ``section`` holds mrkdwn like every other block
        this connector builds -- so the renderer is given the normalised form it
        documents wanting.
        """
        if not remainder.strip():
            return []
        blocks = self._blocks_for(
            self._normalize_slack_mrkdwn(remainder), block_request
        )
        if blocks is not None:
            return [{"type": "blocks", "blocks": blocks}]
        return surface._markdown_chunks(remainder)

    @staticmethod
    def _streamed_remainder(sent: str, root: str) -> str | None:
        """What of *root* has not been appended yet, or ``None`` if they diverge.

        The two are computed from different text -- one from the deltas as they
        arrived, one from the terminal event -- and they are not the same reply.
        The runtime emits every assistant segment of the turn as a delta, the
        narration written before a tool call included, but the terminal event
        holds only the last one. So what streamed routinely reads as a
        preamble followed by the beginning of the answer, while the finished
        reply is the answer alone: it does not begin what is on screen, it
        begins part way into it.

        They disagree at the ends too. The streamed side stops at the last
        complete line and keeps its newline, while the finished side is
        stripped, and the finished side can say less than what streamed.

        So the answer is looked for inside what was streamed rather than assumed
        to open it, and what comes back is whatever lies past the point the two
        last agreed. Any such point is safe: if the tail of what is on screen is
        character for character the head of the answer, writing the rest after it
        leaves the message ending in the answer entire. The earliest is taken
        because it appends least, and ``_STREAM_ANCHOR_MIN_CHARS`` keeps a chance
        agreement of a character or two from passing as a seam.

        No seam at all means the reply was rewritten rather than extended, and
        there is nothing an append can do about it.
        """
        if not sent:
            return root
        if root.startswith(sent):
            return root[len(sent) :]
        trimmed = sent.rstrip()
        if root.startswith(trimmed):
            return root[len(trimmed) :]
        if trimmed.startswith(root):
            # The finished reply says less than what streamed. Everything it
            # holds is already on screen.
            return ""
        # Both spellings of the streamed side, for the same reason the two
        # prefix tests above take both: the seam can fall either side of a
        # newline the surface kept and the finished reply does not hold.
        for text in (sent, trimmed):
            tail = SlackChannel._streamed_anchor_tail(text, root)
            if tail is not None:
                return tail
        return None

    @staticmethod
    def _streamed_anchor_tail(sent: str, root: str) -> str | None:
        """*root* past the point where its head last ends *sent*, or ``None``.

        The seam is a position in *sent* from which everything remaining is the
        opening of *root*. Candidates are found by looking for *root*'s opening
        run, which also fixes the shortest overlap that will be believed: the
        run has to fit inside *sent* for the position to be a candidate at all.
        """
        needle = root[:_STREAM_ANCHOR_MIN_CHARS]
        if not needle:
            return None
        start = sent.find(needle)
        while start != -1:
            if root.startswith(sent[start:]):
                return root[len(sent) - start :]
            start = sent.find(needle, start + 1)
        return None

    async def _stream_delta(
        self, msg: Message, routing_target: RoutingTarget | None
    ) -> None:
        """Fold one delta into the message this reply is being written into.

        Best effort from end to end. An edit that never lands costs a moment of
        staleness in a message the terminal event rewrites in full anyway, so
        raising here would report a failure for text still on its way while
        aborting a delivery that would otherwise have succeeded -- and it would
        raise on an event the caller is documented to treat as nothing to do,
        which is how a cron push reports an error it cannot act on. The message
        that has to land still raises: it is posted, or edited, by ``send()``.
        """
        if not self._streaming_enabled() or self._client is None:
            return
        delta = self._extract_delta_text(msg)
        if not delta:
            return
        channel_id, thread_ts = self._extract_delivery(msg, routing_target)
        if not channel_id:
            return

        key = self._stream_key(msg, channel_id, thread_ts)
        async with self._stream_lock:
            stream = self._streams.get(key)
            if stream is None:
                # Slack rejects an empty message, so the leading whitespace of a
                # reply cannot open one. It is restored by the closing write.
                if not delta.strip():
                    return
                stream = self._new_stream(key, msg, channel_id, thread_ts)
                stream.text = delta
                await self._start_stream(stream)
                return
            if stream.failed or stream.surface.failed:
                return
            stream.text += delta
            stream.touched_at = time.monotonic()
            if stream.session is None:
                # A surface that had nothing it could safely write yet. The
                # streaming one holds back an unterminated line, so a reply
                # whose first delta holds no line break opens its message on
                # the delta that does rather than on an empty call.
                await self._start_stream(stream)
                return
            stream.session.replace(stream.text)

    def _new_stream(
        self,
        key: tuple[str, str, str],
        msg: Message,
        channel_id: str,
        thread_ts: str,
    ) -> _SlackStream:
        """Record a reply being previewed, with the best surface available to it.

        A ladder rather than a cliff. ``stream`` is what the operator asked for
        and is used wherever the API will take it; where it will not -- a reply
        outside a thread, a channel whose recipient ids are unknown -- the reply
        falls to the ``edit`` preview rather than to no preview at all. Only
        ``off`` means no preview, and only the operator can ask for that.

        The distinction matters because the two failures look alike from here
        and are not: ``stream`` being unavailable for this particular reply is a
        fact about Slack, while showing nothing at all is a change to what the
        reader sees. Without it, turning ``stream`` on would make every DM and
        every non-threaded channel worse than they are under ``edit``.
        """
        self._prune_streams()
        surface: _SlackMessageSurface | _SlackStreamingSurface | None = None
        if self._streaming_mode() == STREAMING_STREAM:
            recipient_user_id, recipient_team_id = self._stream_recipient(msg)
            refusal = self._cannot_stream(
                channel_id, thread_ts, recipient_user_id, recipient_team_id
            )
            if refusal:
                # Known before any call is made, so falling back costs nothing
                # and nothing has been put on screen to be inconsistent with.
                # Said once, here, rather than once per delta: the surface this
                # builds is the one the rest of the turn uses.
                logger.info(
                    "[SlackChannel] streaming unavailable for this reply (%s); "
                    "showing it with edits instead: channel=%s thread=%s",
                    refusal,
                    channel_id,
                    thread_ts or "-",
                )
            else:
                surface = _SlackStreamingSurface(
                    self,
                    channel_id,
                    thread_ts,
                    recipient_user_id=recipient_user_id,
                    recipient_team_id=recipient_team_id,
                )
        if surface is None:
            surface = _SlackMessageSurface(self, channel_id, thread_ts)
        stream = _SlackStream(
            surface=surface, channel_id=channel_id, thread_ts=thread_ts
        )
        self._streams[key] = stream
        return stream

    async def _start_stream(self, stream: _SlackStream) -> None:
        """Create the message a reply will be written into, if one can be yet.

        Returns without a session, and without marking the stream failed, when
        the surface has nothing it can safely write: the next delta tries again.

        A stream that cannot be opened at all falls to the edit preview and
        tries once more, which is the same ladder ``_new_stream`` walks for a
        reply it can see in advance cannot be streamed. It is safe here for the
        same reason it is safe there: the opening call is the first thing either
        surface does, so nothing is on screen yet and the swap is invisible.

        Only when that second attempt fails too is the stream marked failed, and
        then the reply is posted whole when the turn ends -- which is what a
        channel with no preview at all has always done.

        Deliberately not a fallback for a stream that has already appended. A
        message in streaming state refuses ``chat.update`` outright, so an edit
        surface could not write to it, and the answer is not at risk in any case:
        the close and its recovery ladder deliver the whole of it.
        """
        if stream.failed or not stream.surface.ready_to_open(stream.text):
            return
        if await self._open_session(stream):
            return
        if isinstance(stream.surface, _SlackStreamingSurface):
            stream.surface = _SlackMessageSurface(
                self, stream.channel_id, stream.thread_ts
            )
            if await self._open_session(stream):
                logger.info(
                    "[SlackChannel] the stream could not be opened; showing "
                    "this reply with edits instead: channel=%s",
                    stream.channel_id,
                )
                return
        # Remembered as failed rather than forgotten: forgetting would make the
        # next delta try to open again, once per fragment.
        stream.failed = True

    async def _open_session(self, stream: _SlackStream) -> bool:
        """Open the surface and hand it a session. ``False`` if it would not open."""
        debounce = (
            _STREAM_APPEND_DEBOUNCE_MS
            if isinstance(stream.surface, _SlackStreamingSurface)
            else _STREAM_DEBOUNCE_MS
        )
        session = StreamingSession(stream.surface, debounce_ms=debounce)
        try:
            await session.start(stream.text)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Slack streaming could not start: %s", exc)
            return False
        stream.session = session
        return True

    async def _close_stream(
        self, msg: Message, channel_id: str, thread_ts: str
    ) -> tuple[str, _SlackStreamingSurface | None]:
        """Hand ``send()`` this reply's streamed message, if it has one.

        Two answers because the two modes end differently. An edited message is
        a ts and nothing more: ``send()`` rewrites it with chat.update and owns
        the delivery from there, which is what it has always done. A streamed
        one cannot be rewritten, so the surface comes back with it -- the close
        is ``chat.stopStream`` and only the surface knows what has already been
        appended.

        ``("", None)`` when there is nothing to finish -- streaming off, no
        delta seen, a message that became unusable mid-stream, or one that never
        opened -- which leaves ``send()`` posting exactly as it did before
        streaming existed.
        """
        stream = self._streams.pop(self._stream_key(msg, channel_id, thread_ts), None)
        if stream is None or stream.session is None or stream.failed:
            return "", None
        try:
            # Waits out any write still in flight: one landing after the close
            # would leave a half-finished answer on screen for good.
            handle = await stream.session.stop()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Slack streaming could not be closed: %s", exc)
            return "", None
        surface = stream.surface
        if surface.failed:
            # The one exit path of the three that would otherwise say nothing,
            # and the one that changes where the reply lands: send() posts a
            # fresh message, leaving the abandoned preview above it. A streamed
            # message loses less than an edited one -- what was appended is on
            # screen and correct -- so the line says which of the two happened.
            logger.warning(
                "[SlackChannel] streaming abandoned, %s: "
                "channel=%s ts=%s edits=%d failed=%d reason=%s",
                "the rest of the reply is posted separately"
                if isinstance(surface, _SlackStreamingSurface)
                else "reply posts fresh",
                channel_id,
                surface.opened_ts or "-",
                surface.edits,
                surface.edit_failures,
                surface.error or "-",
            )
            if isinstance(surface, _SlackStreamingSurface):
                # Still handed back. What was appended is on screen and correct,
                # so the reply owes the reader only what came after it -- and
                # the message has to be taken out of streaming state whatever
                # else happens.
                return handle, surface
            return "", None
        logger.info(
            "[SlackChannel] streaming closed: channel=%s ts=%s edits=%d failed=%d",
            channel_id,
            surface.opened_ts or handle or "-",
            surface.edits,
            surface.edit_failures,
        )
        if isinstance(surface, _SlackStreamingSurface):
            return handle, surface
        return handle, None

    def _prune_streams(self) -> None:
        """Bound the stream map.

        A turn whose terminal event holds no text never reaches
        ``_close_stream``, so without this the map grows for the lifetime of the
        process. Dropping a stream only costs the message its final edit: the
        reply is still posted, below what was streamed so far.
        """
        _prune_bounded_map(
            self._streams,
            cap=_MAX_ACTIVE_STREAMS,
            timeout=_STREAM_IDLE_TIMEOUT_SECONDS,
            timestamp=lambda stream: stream.touched_at,
            what="open streams",
        )
        if len(self._stream_next_update_at) > _MAX_ACTIVE_STREAMS:
            self._stream_next_update_at.clear()

    async def _wait_for_stream_slot(
        self, channel_id: str, interval: float | None = None
    ) -> None:
        """Wait until this channel may be written to again.

        Keyed by channel rather than by stream because that is how Slack meters
        it: two conversations streaming into one channel share one budget, and a
        per-stream throttle would let each of them spend all of it.

        ``interval`` is the caller's, because the two surfaces spend from
        different tiers -- an append is metered twice as generously as an edit
        -- and both still queue behind the same channel. ``None`` is the edit
        spacing, so a caller that says nothing is throttled as it always was,
        and it is read here rather than as a default argument so that the
        constant can still be lowered under a running process.
        """
        if interval is None:
            interval = _STREAM_MIN_UPDATE_INTERVAL_SECONDS
        while True:
            now = time.monotonic()
            ready_at = self._stream_next_update_at.get(channel_id, 0.0)
            if now >= ready_at:
                self._stream_next_update_at[channel_id] = now + interval
                return
            await asyncio.sleep(ready_at - now)

    async def _call_stream_method(self, method: str, **kwargs: Any) -> Any:
        """Call one of the three chat streaming methods, retrying a rate limit.

        The same retry ``_post_text`` makes, and made separately rather than by
        widening that function: the streaming methods take chunks where it takes
        text and blocks, they are metered on a different tier, and a refused
        rendering is recovered from at a different level -- ``_post_text``
        re-sends the same chunk without its blocks, while a stream can only drop
        the chunk that held them.

        Raises rather than returning an error string. Both callers latch, and a
        surface that latches has to keep the reason; an exception holds the
        Slack error code, which is what the caller reads to tell a reader
        pressing stop apart from an expired stream.

        ``None`` arguments are dropped and empty strings are not, which matters
        for exactly one field. ``chat.startStream`` answers ``invalid_thread_ts``
        both when ``thread_ts`` is the empty string and when it is left out
        altogether -- measured against the live API, and contradicting a
        reference page that marks the argument optional and does not list that
        error at all. There is therefore no way to stream a message that is not
        a reply, and ``_cannot_stream`` refuses the attempt before it is made
        rather than spending a call to be told so.

        The recipient ids are passed as ``None`` when they are unknown, which is
        a DM: the docs mark them required "when streaming to channels", and a
        channel stream with neither is refused by ``_cannot_stream`` for the
        same reason.
        """
        payload = {key: value for key, value in kwargs.items() if value is not None}
        call = getattr(self._client, method)
        rate_limit_retries = 0
        while True:
            try:
                return await call(**payload)
            except Exception as exc:  # noqa: BLE001
                delay = retry_after_seconds(exc)
                if delay is None or rate_limit_retries >= _MAX_RATE_LIMIT_RETRIES:
                    raise
                rate_limit_retries += 1
                logger.warning(
                    "Slack rate limited on %s; retrying in %.1fs (%d/%d)",
                    method,
                    delay,
                    rate_limit_retries,
                    _MAX_RATE_LIMIT_RETRIES,
                )
                await asyncio.sleep(delay)

    def _remember_stream_recipient(
        self, session_id: str, user_id: str, team_id: str
    ) -> None:
        """Note who the turn now starting on ``session_id`` is for.

        The ids come off the inbound event because that is the only point at
        which both are on the wire: the response the reply streams into holds
        the session and the channel but not the person, and the runtime drops
        the user id entirely. A channel session id does not hold it either -- it
        ends in the root thread ts, where a DM's ends in the user -- and
        channels are exactly where ``chat.startStream`` requires it.

        Written where a turn is dispatched, which is the same three points
        ``_remember_turn_initiator`` is written at and for the same reason. A
        message that is only held under ``mid_turn: queue`` records nothing: it
        has no turn yet, and the entry it would write is the one the turn
        already running is about to be previewed under. The drain writes it
        instead, when that message becomes a turn of its own. Nor does a steer,
        which joins someone else's turn rather than starting one.

        Overwriting on dispatch is right for the same reason it is right for the
        initiator: an ordinary send cancels whatever the session was running, so
        after this the turn in flight is this person's.

        Bounded the way the stream map is, and for the same reason: a session
        that never produces a streamed reply would otherwise sit here for the
        lifetime of the process.
        """
        if not session_id or not user_id:
            return
        if len(self._stream_recipients) >= _MAX_STREAM_RECIPIENTS:
            for stale in list(self._stream_recipients)[
                : len(self._stream_recipients) - _MAX_STREAM_RECIPIENTS + 1
            ]:
                del self._stream_recipients[stale]
        self._stream_recipients[session_id] = (user_id, team_id)

    def _remember_turn_initiator(
        self,
        session_id: str,
        user_id: str,
        request_id: str,
        *,
        is_dm: bool,
        chat_type: str,
        channel_id: str = "",
        message_ts: str = "",
        thread_ts: str = "",
        request_metadata: Mapping[str, Any] | None = None,
    ) -> None:
        """Note who asked for the turn now starting on ``session_id``.

        Overwrites whatever the session held, because that is what the gateway
        does with the turn itself: an ordinary ``chat.send`` cancels the stream
        the session already had and runs the new request in its place, so after
        this dispatch the turn in flight is this person's and no longer the
        previous one's. An entry that outlived its turn would name someone whose
        work has already been stopped.

        Bounded and aged the way every other map here is. The cap is the case
        the timeout cannot reach -- a busy workspace opening entries faster than
        an hour retires them -- and it is spent on the oldest first, which for
        this map is the turn least likely to still be running.

        ``is_dm`` has no default on purpose. Every caller already knows which
        surface it is dispatching on, and a default would be a claim about the
        surface made by whoever forgot to pass one -- silently right for most
        traffic and silently wrong for the case the field exists to name.

        ``chat_type`` has none for the same reason, though its empty value is a
        legitimate answer. Every caller knows whether it holds a kind, and
        letting the absent case arrive by omission would make "this dispatch
        had no kind" and "nobody thought about it" the same call -- which is
        exactly the distinction a rule addressed on the kind turns on.

        ``channel_id`` and ``message_ts`` do have one, and the difference is
        deliberate: they say where the turn's ending is to be marked, and an
        empty pair is the truthful answer for a dispatch with no message behind
        it. A wrong default here would write a mark onto somebody else's
        message, so the absent case is spelled rather than assumed.

        ``thread_ts`` has the same empty default and a different value from
        ``message_ts`` when the two diverge: it is where the acknowledgement's
        thinking status was opened, which the turn's ending clears the same way
        its mark is written back onto ``message_ts``.

        ``request_metadata`` is the dispatched request's own metadata bag, and
        the three facts kept off it are read here rather than handed over one
        at a time on purpose: what is recorded is then literally what the
        request held. Each of the three is settled at dispatch by a rule of
        its own, and a caller that worked one of them out a second time to pass
        it here could disagree with the request it had just sent.

        ``None`` is the fail-closed reading and is what a caller with no
        request in hand gets: no contract, no root routing, no trigger. That is
        the same reading the outbound half already gives an absent key, so a
        path this parameter has not reached behaves exactly as it did before
        the field existed.
        """
        if not session_id or not user_id:
            return
        self._prune_turn_initiators()
        metadata = request_metadata or {}
        self._turn_initiators[session_id] = _SlackTurnInitiator(
            user_id=user_id,
            request_id=request_id,
            is_dm=is_dm,
            chat_type=chat_type,
            channel_id=channel_id,
            message_ts=message_ts,
            thread_ts=thread_ts,
            silence_is_honoured=bool(metadata.get(SLACK_REPLY_OPTIONAL_KEY)),
            addressed=bool(metadata.get(SLACK_ADDRESSED_KEY)),
            post_as_root=bool(metadata.get(POST_AS_ROOT_KEY)),
            trigger=str(metadata.get("slack_trigger") or ""),
        )

    def _prune_turn_initiators(self) -> None:
        """Retire entries whose turn cannot still be running, and cap the rest."""
        _prune_bounded_map(
            self._turn_initiators,
            cap=_MAX_TURN_INITIATORS,
            timeout=_TURN_INITIATOR_TIMEOUT_SECONDS,
            timestamp=lambda entry: entry.started_at,
            what="tracked turn initiators",
        )

    def _forget_turn_initiator(self, session_id: str, request_id: str) -> None:
        """Close the entry the turn ``request_id`` opened, if it still owns one.

        Matched on the request rather than dropped by session, because the two
        differ exactly when it matters. A second person posting into the same
        thread cancels the running turn and starts their own; the cancelled
        turn's terminal event then arrives *after* the new entry was opened, so
        dropping by session alone would leave the live turn with no initiator. An
        event holding no id is left alone for the same reason, and the age and
        the cap retire the entry.
        """
        if not session_id or not request_id:
            return
        entry = self._turn_initiators.get(session_id)
        if entry is not None and entry.request_id == request_id:
            del self._turn_initiators[session_id]

    def turn_initiator(self, session_id: str) -> _SlackTurnInitiator | None:
        """Who started the turn ``session_id`` is running, or ``None``.

        The seam an authorization check reads: a stop arriving from Slack
        compares its clicker or sender against this and decides. Recording the
        initiator is what makes such a check possible at all; every other
        question about who may do what needs policy this does not hold.

        ``None`` is not "nobody may stop it". It means this connector cannot
        say, which is the ordinary state for a session with nothing running and
        for a turn whose entry aged out, and a caller must not read it as a
        refusal.
        """
        if not session_id:
            return None
        entry = self._turn_initiators.get(session_id)
        if entry is None:
            return None
        if entry.started_at < time.monotonic() - _TURN_INITIATOR_TIMEOUT_SECONDS:
            del self._turn_initiators[session_id]
            return None
        return entry

    def _session_scope(
        self, channel_id: str, user_id: str = "", chat_type: str = ""
    ) -> str:
        """How much of this conversation shares one session.

        ``thread``, the default, keys a channel session on the thread root, so
        every thread is its own conversation with its own history and its own
        turn. ``channel`` keys it on the channel, so the threads of one room
        share a history and the agent has continuity across them.

        The same three-rung cascade every other per-conversation setting reads,
        settled by :func:`settled_override`: this conversation and this sender,
        then a scope naming the platform, then :data:`SESSION_DEFAULT`.

        Answers for a DM as readily as for a channel, and its caller ignores the
        answer there: a DM has no threads to group, so the key has nothing to do
        in one. See :meth:`_session_id` for why that is an ignore rather than a
        refusal.
        """
        settled = settled_override(
            self.config,
            channel_id,
            user_id=user_id,
            chat_type=chat_type,
            workspace=self._workspace_team_id,
        ).session
        return settled if settled in SESSION_VALUES else SESSION_DEFAULT

    @staticmethod
    def _session_id(
        *,
        team_id: str,
        channel_id: str,
        user_id: str,
        root_thread_ts: str,
        is_dm: bool,
        channel_wide: bool = False,
    ) -> str:
        """The session this message belongs to, and the session key for its turn.

        Three forms, and the trailing field is what separates them:

        * ``slack_{team}_{channel}_{user}`` for a DM;
        * ``slack_{team}_{channel}_{thread_ts}`` for a channel under
          ``session: thread``, one session per thread;
        * ``slack_{team}_{channel}`` for a channel under ``session: channel``,
          one session for the room.

        **The empty string when the event named no workspace, and the caller
        drops the message.** The team is the first field of all three forms, and
        it used to be spelled ``default`` when Slack sent none -- harmless while
        one workspace could ever be configured, and not harmless now: two
        installs delivering an event with no team would share one bucket, which
        means one workspace's conversation would be answered out of another's
        history and its turns would collide with the other's in every map keyed
        on this string. There is no id that is safely shared here, so an
        unattributable message produces no session rather than a shared one.

        ``channel_wide`` is the settled ``session`` as a bool, read once by the
        caller so that this stays a function of its arguments alone. It is never
        true for a DM.

        **A DM ignores the key rather than refusing it.** A DM is already one
        session for the whole conversation -- keyed on the person, so its threads
        already share a history -- which is what ``channel`` asks for, so the
        setting has nothing left to do there. Refusing it would mean the natural
        way to ask for a channel-wide session everywhere, a scope matching
        ``{channel: slack}``, warning on every DM message about a setting that
        changes nothing in one. Keeping the id byte-identical also keeps a DM's
        existing history where it is: rekeying it would silently start every
        DM over.

        **Nothing here decides where an answer goes.** The reply target is
        ``reply_thread_ts``, settled per message from the thread that message
        was posted in and stamped onto the request as ``slack_thread_ts``, which
        the delivery ladder reads before it ever looks at a session id. Under
        ``session: channel`` a question asked in a thread is still answered in
        that thread; what changes is which conversation the answer was composed
        from.
        """
        team = str(team_id or "").strip()
        if not team:
            return ""
        if is_dm:
            return f"slack_{team}_{channel_id}_{user_id}"
        if channel_wide:
            return f"slack_{team}_{channel_id}"
        return f"slack_{team}_{channel_id}_{root_thread_ts}"

    def _mid_turn_mode(
        self,
        channel_id: str,
        user_id: str = "",
        *,
        chat_type: str = "",
        session_is_channel_wide: bool = False,
    ) -> str:
        """What a message arriving mid-turn does in this conversation.

        One place, so that the three branches on the dispatch path read a single
        answer rather than three copies of a cascade.

        The cascade, read from the most specific layer down:

        1. What the scopes settled for this conversation and this sender -> use
           it, stop.
        2. Otherwise what a scope naming the platform settled.
        3. Otherwise :data:`MID_TURN_DEFAULT`, which is ``queue``: the message
           waits, and runs when the turn it arrived during ends.

        The first two are :func:`settled_override`'s job, and the layering
        between them is stated there rather than repeated here.

        The third was ``cancel`` until the reason for it was examined and found
        to be no reason at all. An ordinary ``chat.send`` reaching the gateway
        already finishes the stream that session had, so cancel was what the
        connector did before this key existed and "unset" simply inherited it.
        A default that destroys a running turn is a poor one to hand a
        deployment that configured nothing: it is met most often by someone
        sending a follow-up, which kills the turn the follow-up was meant to
        add to, and in a channel or thread the turn it kills may be work
        somebody else started. ``queue`` refuses nothing silently -- an
        overflowing queue says so on the message -- and it is the only value of
        the three that keeps a second sender out of a turn already running.

        There is no ``channels.slack`` key under this, unlike ``mode``, which
        has ``group_chat_mode`` beneath it, and ``layer0_keys`` names none:
        inventing a connector-wide key would give one value two homes when the
        scopes already name every conversation this could be set for.

        ``session_is_channel_wide`` is the one thing that can override the
        settled answer, and it refuses exactly one value. See the branch below
        for why a steer and a channel-wide session cannot both hold. The caller
        passes it because the session id is built before this is asked, and
        deriving it here a second time would be a second read of the same
        cascade.
        """
        settled = settled_override(
            self.config,
            channel_id,
            user_id=user_id,
            chat_type=chat_type,
            workspace=self._workspace_team_id,
        ).mid_turn
        mode = settled if settled in MID_TURN_VALUES else MID_TURN_DEFAULT
        if mode == MID_TURN_STEER and session_is_channel_wide:
            # A steer folds this message into a turn that is already running,
            # and that turn's output stream was fixed when it started. Under a
            # channel-wide session the running turn may have been started from a
            # different thread, so the steered message's answer would be posted
            # into a thread its sender never wrote in -- and there is no second
            # place for it to go, a steer producing one answer in one stream by
            # construction. Settled to the default instead, which gives this
            # message a turn of its own and therefore its own reply target.
            self._report_steer_under_a_channel_session(channel_id)
            return MID_TURN_DEFAULT
        return mode

    def _report_steer_under_a_channel_session(self, channel_id: str) -> None:
        """Say once that a conversation asked for two settings that disagree.

        Once per channel rather than once per message, the way a model that has
        left ``models.defaults`` is reported: a conversation configured this way
        would otherwise warn on every message it receives, which is the shape of
        log noise that gets a warning filtered out and then missed. The set is
        per connector instance, so a config reload re-arms it.
        """
        if channel_id in self._reported_steer_under_channel_session:
            return
        self._reported_steer_under_channel_session.add(channel_id)
        logger.warning(
            "channels.slack: %s is set to delivery.session=%s and"
            " delivery.mid_turn=%s, which cannot both hold. A steer joins the"
            " turn already running, and that turn's answer goes wherever it was"
            " already going -- under one session for the whole channel that is"
            " a thread the steering message was never posted in. Those messages"
            " take delivery.mid_turn=%s instead, so each gets a turn of its own"
            " and is answered where it was sent. Write session=%s to steer, or"
            " mid_turn=%s to say so explicitly",
            channel_id,
            SESSION_CHANNEL,
            MID_TURN_STEER,
            MID_TURN_DEFAULT,
            SESSION_THREAD,
            MID_TURN_DEFAULT,
        )

    @staticmethod
    def _is_runtime_accepted(msg: Message) -> bool:
        """Whether this event is the runtime saying "input taken", nothing more.

        Read off both the typed field and the payload, because the two paths a
        response can take fill different ones. A streamed request's chunks are
        typed by the gateway before they are published, so ``event_type`` is set;
        a non-streamed one comes back as a single response whose payload holds
        the name and may not have been recognised as an event on the way.
        Checking one alone would work only for whichever transport the
        deployment happens to have on.
        """
        if msg.event_type == EventType.RUNTIME_ACCEPTED:
            return True
        payload = getattr(msg, "payload", None)
        return (
            isinstance(payload, Mapping)
            and str(payload.get("event_type") or "")
            == EventType.RUNTIME_ACCEPTED.value
        )

    def _note_ack_only_request(self, request_id: str) -> None:
        """Remember that ``request_id``'s ending will not mean a turn ended.

        Written when ``runtime.accepted`` arrives, which is always *before* the
        ending it qualifies: the adapter yields the acceptance and then closes
        the stream, in that order and on the same generator, so there is no
        interleaving that could deliver the ending first.

        Bounded and aged like every other map here. An entry is read once,
        milliseconds after it is written, so the cap is only ever reached by a
        run of acknowledgements whose endings never arrived at all.
        """
        if not request_id:
            return
        _prune_bounded_map(
            self._ack_only_requests,
            cap=_MAX_ACK_ONLY_REQUESTS,
            timeout=_ACK_ONLY_REQUEST_TIMEOUT_SECONDS,
            timestamp=lambda noted_at: noted_at,
            what="acknowledged requests",
        )
        self._ack_only_requests[request_id] = time.monotonic()

    def _is_ack_only_request(self, request_id: str) -> bool:
        """Whether ``request_id`` was acknowledged rather than run.

        Consumed on read. The entry answers exactly one question, asked once by
        the ending that follows the acceptance, and holding it afterwards would
        only give a replayed ending a way to be ignored twice.
        """
        if not request_id:
            return False
        noted_at = self._ack_only_requests.pop(request_id, None)
        if noted_at is None:
            return False
        return noted_at >= time.monotonic() - _ACK_ONLY_REQUEST_TIMEOUT_SECONDS

    def _queue_mid_turn_message(self, entry: _SlackQueuedMessage) -> bool:
        """Hold ``entry`` behind the turn its session is running.

        ``False`` means the queue for that session is full and this message was
        not taken -- the caller says so on the message rather than letting the
        sender believe it is waiting its turn.

        The newest is what overflow refuses, never the oldest. Every message
        already in the queue holds a reaction telling its sender it will run,
        and quietly dropping one of those to make room would break the only
        promise this feature makes.
        """
        self._prune_queued_messages()
        queue = self._queued_messages.setdefault(entry.session_id, [])
        if len(queue) >= _MAX_QUEUED_PER_SESSION:
            logger.warning(
                "Slack refused a queued message: session %s already holds %s,"
                " which is the cap",
                entry.session_id,
                len(queue),
            )
            if not queue:
                # Only reachable with the cap set to zero, and then the empty
                # list this just created would sit in the map forever.
                self._queued_messages.pop(entry.session_id, None)
            return False
        queue.append(entry)
        return True

    def _prune_queued_messages(self) -> None:
        """Drop held messages that cannot still be wanted, and cap the rest."""
        cutoff = time.monotonic() - _QUEUED_MESSAGE_TIMEOUT_SECONDS
        for session_id, queue in list(self._queued_messages.items()):
            fresh = [entry for entry in queue if entry.queued_at >= cutoff]
            if len(fresh) != len(queue):
                logger.warning(
                    "Slack dropped %s queued message(s) for session %s: the turn"
                    " they were waiting on never reported that it ended",
                    len(queue) - len(fresh),
                    session_id,
                )
            if fresh:
                self._queued_messages[session_id] = fresh
            else:
                del self._queued_messages[session_id]
        while len(self._queued_messages) >= _MAX_QUEUED_SESSIONS:
            # By the head's age, not the tail's: the session whose oldest held
            # message is oldest is the queue least likely to still be wanted,
            # and a session someone is still typing into keeps a young head only
            # if its queue drained in between.
            oldest = min(
                self._queued_messages,
                key=lambda key: self._queued_messages[key][0].queued_at,
            )
            logger.warning(
                "Slack dropped the queued messages of the oldest of too many"
                " waiting sessions: %s",
                oldest,
            )
            del self._queued_messages[oldest]

    async def _drain_queued_messages(self, session_id: str) -> None:
        """Dispatch one held message if ``session_id`` has no turn running.

        One, not the whole queue. Each held message is its own turn -- its own
        request id, its own initiator, its own reply -- so the second cannot
        start until the first has ended, and the event that ends it comes back
        here and drains the next.

        The liveness test is ``turn_initiator``, the same signal the inbound
        path decides on, and it is re-read here rather than assumed: an ending
        that arrives twice for one turn, or an ending for a turn that was
        already replaced, must not let a second message out alongside the one
        this already started. Dispatching is what makes the test say "busy"
        again, and the entry is opened inside ``_dispatch_queued_message``
        before it awaits anything, so there is no window between the two.
        """
        if not session_id:
            return
        self._prune_queued_messages()
        queue = self._queued_messages.get(session_id)
        if not queue:
            return
        if self.turn_initiator(session_id) is not None:
            return
        entry = queue.pop(0)
        if not queue:
            del self._queued_messages[session_id]
        await self._dispatch_queued_message(entry)

    async def _dispatch_queued_message(self, entry: _SlackQueuedMessage) -> None:
        """Run a held message as the ordinary turn it always was.

        An ordinary send, deliberately: the message left the connector unchanged
        by having waited, so it needs no output routing, no second stream and no
        demultiplexing of one reply into two. That is what ``queue`` buys over a
        follow-up round, and it is why this reuses the dispatch tail rather than
        a path of its own.

        The initiator entry is opened here and not when the message was held,
        because until now there was no turn to attribute -- and because the
        entry opened at hold time would have named this person as the owner of
        somebody else's running turn.

        The stream recipient is recorded here for exactly the same reason. Held
        messages reach this method one at a time and only once the session has
        no turn left, so writing it now names the turn that is starting rather
        than the one it was waiting behind.

        The thinking status is set again here too, for a reason that has
        nothing to do with dispatch itself: while this message waited,
        ``_mark_queued`` had the thread showing ``queued_status`` instead. That
        was true then and would be a lie now -- this call is what starts the
        turn ``queued_status`` was describing as not yet started -- so setting
        ``thinking_status`` again is the correction, not a repeat. No branch is
        needed to tell a drained message apart from a fresh one: this is simply
        the latest event for the thread, and ``assistant.threads.setStatus``
        already replaces on every call.
        """
        self._remember_turn_initiator(
            entry.session_id,
            entry.user_id,
            entry.request.id,
            is_dm=entry.is_dm,
            chat_type=entry.chat_type,
            # The same coordinates the queue mark was put and taken off at,
            # which is where this message's own ending belongs too.
            channel_id=entry.channel_id,
            message_ts=entry.message_ts,
            thread_ts=entry.thread_ts,
            # The held request's own bag, which is the point of holding the
            # request rather than the text: these were settled when the message
            # arrived, against the conversation it arrived in, and the drain has
            # no event left to settle them from.
            request_metadata=entry.request.metadata,
        )
        self._remember_stream_recipient(
            entry.session_id, entry.user_id, entry.team_id
        )
        await self._unmark_queued(entry)
        if self._acknowledge_mode() != ACK_MODE_OFF:
            await self._set_thinking_status(entry.channel_id, entry.thread_ts)
        # The same reason it is done on the inbound path: this message is what
        # ends any interrupt the session is paused on, and a click arriving
        # after it would be routed as an answer to a turn that stopped waiting.
        await self._withdraw_questions_for_session(entry.session_id)
        logger.info(
            "[SlackChannel] queued message dispatched: session=%s request_id=%s"
            " user=%s still_queued=%s",
            entry.session_id,
            entry.request.id,
            entry.user_id,
            len(self._queued_messages.get(entry.session_id, ())),
        )
        await self._route_request(entry.request)

    async def _route_request(self, req: Message) -> None:
        """Hand a built request to whatever is consuming this channel's traffic.

        The callback when one is registered -- which is how the tests read what
        was dispatched -- and the bus otherwise. Extracted so that no two of the
        four paths that produce a request -- the inbound message, the queue
        drain, the stop button's cancel and a question's answer -- can drift
        into two answers to the same question.
        """
        if self._on_message_cb is not None:
            result = self._on_message_cb(req)
            if asyncio.iscoroutine(result):
                await result
            return
        await self.bus.route_user_message(req)

    def _cannot_stream(
        self, channel_id: str, thread_ts: str, user_id: str, team_id: str
    ) -> str:
        """Why this reply cannot be streamed, or ``""`` if it can.

        Both conditions are measured rather than inferred, and both are refused
        here rather than at the API, because the answer either way is the same
        -- post the reply the way a non-streamed turn posts it -- and finding
        out from Slack costs a call and a turn of latency to learn something
        that was knowable in advance.

        **A stream is a threaded reply or it is nothing.** ``chat.startStream``
        answers ``invalid_thread_ts`` for an absent ``thread_ts`` as readily as
        for an empty one, and the guidance beside it says "Streamed messages
        should always be replies to a user request". This connector already
        decides where a reply goes, and two of its answers are "not in a
        thread": a DM answered at the top of the conversation, and a channel
        whose operator set ``reply_in_thread: false``. Streaming therefore
        follows threading -- it is available exactly where the connector already
        replies in a thread, and ``reply_in_thread`` is the lever that turns it
        on for a channel. Putting the reply in a thread the operator did not ask
        for, in order to enable a preview they did not ask for either, inverts
        which of the two is in charge.

        **A channel stream needs both recipient ids.** The reference marks
        ``recipient_user_id`` and ``recipient_team_id`` "Required when streaming
        to channels", and a call holding only the first is refused with
        ``missing_recipient_team_id``. A DM needs neither. The id prefix is what
        tells them apart, and it is asked only the question it can answer: ``D``
        is the one-to-one direct message and nothing else is. Which of the three
        room kinds a ``C`` or a ``G`` names is not asked here and the id could
        not say; see :func:`chat_type_from_chat_id`.
        """
        if not thread_ts:
            return "the reply is not in a thread and a stream has to be one"
        if not channel_id.startswith("D") and not (user_id and team_id):
            return "a channel stream needs both recipient ids and one is unknown"
        return ""

    def _stream_recipient(self, msg: Message) -> tuple[str, str]:
        """The user and team a stream into this reply's message is for.

        Three sources, most reliable first: what was remembered at inbound, the
        reply's own metadata if the runtime happened to include it, and the
        session id, which spells out the team always and the user for a DM.
        """
        session_id = str(msg.session_id or "")
        remembered = self._stream_recipients.get(session_id)
        if remembered is not None:
            return remembered

        metadata = msg.metadata or {}
        user_id = str(metadata.get("slack_user_id") or "").strip()
        team_id = str(metadata.get("slack_team_id") or "").strip()
        if user_id and team_id:
            return user_id, team_id

        if session_id.startswith("slack_"):
            parts = session_id.split("_", 4)
            if len(parts) >= 3:
                # A session id may spell a workspace-less request "default",
                # which is a placeholder and not an encoded team. Reading it
                # through would send Slack a team that does not exist, where
                # sending nothing is refused with a name for what is missing.
                #
                # No longer minted -- ``_session_id`` now produces no session at
                # all for an event naming no workspace -- but still read, because
                # a session id written before that change outlives it: a cron job
                # stores the string it was created with, and fires against it
                # months later.
                candidate = parts[1].strip()
                if not team_id and candidate != "default":
                    team_id = candidate
            if len(parts) >= 4:
                target = parts[3].strip()
                # A DM session ends in the user; a channel session ends in the
                # root thread ts, which has a dot in it and is not one. A
                # session keyed on the whole channel has no trailing field at
                # all, and so names nobody -- the team above is all it holds.
                if not user_id and target and "." not in target:
                    user_id = target
        return user_id, team_id

    def _stream_snapshot(self, text: str) -> str:
        """Render accumulated deltas the way the finished reply will be rendered.

        Only the first chunk, and only as much of it as an edit accepts: an
        answer past the per-message limit is split by ``send()`` too, the
        message being streamed into is the one that ends up holding that same
        first chunk, and every write into it after the opening post is a
        chat.update.

        The opening post is held to the same ceiling even though chat.postMessage
        would take more. It is not exposed to msg_too_long itself -- a post
        truncates where an edit rejects -- but a preview that opened above the
        edit ceiling would visibly shrink on the very first edit, which reads as
        the answer losing text while it is being written.

        Text only. A stream is a message rewritten roughly once a second, and a
        table built a row at a time is worse to watch than the same rows arriving
        as plain text: every edit reflows the grid, and a table whose last row is
        still a fragment renders as broken. Nothing is lost by waiting, the
        terminal event rewriting this message in full through ``send()``, which
        is where the blocks land. The marker is stripped here all the same, so it
        does not flicker while the reply is being written.
        """
        text, _ = self._extract_block_request(text)
        # The first piece only: the preview is the message the deltas are being
        # written into, and that message is the root. Every further piece becomes
        # a reply the closing rewrite posts, however many there are.
        summary = self._split_threaded_report(text)[0]
        return self._clamp_stream_preview(self._normalize_slack_mrkdwn(summary))

    @classmethod
    def _clamp_stream_preview(cls, text: str) -> str:
        """Trim one in-progress preview to what chat.update accepts.

        Truncating a preview costs nothing the user keeps: it is superseded by
        the closing rewrite, which holds the whole answer and is chunked. What
        the cut must not do is leave debris on screen, so it prefers a paragraph,
        line or word boundary over a hard cut mid-word, and inherits the
        splitter's rule about not cutting inside a ``<...>`` token -- half of a
        link or a mention renders as literal punctuation.
        """
        text = text.strip()
        if len(text) <= _MAX_SLACK_UPDATE_TEXT_LENGTH:
            return text
        budget = _MAX_SLACK_UPDATE_TEXT_LENGTH - len(_STREAM_TRUNCATION_SUFFIX)
        split_at = cls._preferred_split_index(text, budget)
        return text[:split_at].rstrip() + _STREAM_TRUNCATION_SUFFIX

    @staticmethod
    def _stream_key(
        msg: Message, channel_id: str, thread_ts: str
    ) -> tuple[str, str, str]:
        return str(msg.id or ""), channel_id, thread_ts

    @staticmethod
    def _extract_delta_text(msg: Message) -> str:
        """Return one delta's text with its whitespace intact.

        ``_extract_outgoing_text`` strips, which is right for a whole message and
        wrong for a fragment: the space between two words often arrives as a
        fragment of its own, and stripping every one of them runs the reply
        together. Reasoning fragments are skipped -- they are the model thinking
        aloud, not the answer being written.
        """
        payload = getattr(msg, "payload", None)
        if not isinstance(payload, dict):
            return ""
        chunk_type = str(payload.get("source_chunk_type") or "").strip().lower()
        if chunk_type == "llm_reasoning":
            return ""
        content = payload.get("content")
        if isinstance(content, dict):
            content = content.get("output", "")
        return str(content or "")

    def _group_chat_mode(self) -> str:
        mode = str(self.config.group_chat_mode or "").strip().lower()
        if mode in GROUP_CHAT_MODES:
            return mode
        if mode:
            logger.warning(
                "channels.slack.group_chat_mode=%r is not one of %s; using %r",
                self.config.group_chat_mode,
                "/".join(GROUP_CHAT_MODES),
                GROUP_MODE_MENTION,
            )
        return GROUP_MODE_MENTION

    def _channel_triggers(
        self, channel_id: str, user_id: str = "", chat_type: str = ""
    ) -> frozenset[str]:
        return channel_triggers(
            self.config,
            channel_id,
            group_chat_mode=self._group_chat_mode(),
            user_id=user_id,
            chat_type=chat_type,
            workspace=self._workspace_team_id,
        )

    def _channel_prompt(
        self, channel_id: str, user_id: str = "", chat_type: str = ""
    ) -> str:
        """The standing instruction appended to a message from ``channel_id``.

        The layers, most specific first, and all of them cover every trigger in
        the conversations they match: a scope's prompt was written knowing no
        trigger in particular, so scoping it to one would be this code choosing
        a narrower meaning than the operator wrote.

        1. what the scopes settled for this conversation and this sender
        2. what a scope naming the platform settled
        3. nothing

        The first two are :func:`settled_override`'s job. ``user_id`` is the
        sender, and it is optional: a caller with none gets the answer for an
        unidentified sender, which is the answer a rule naming people does not
        contribute to.

        Exactly one prompt is appended, never two: a conversation's prompt
        replaces the platform's outright rather than stacking with it, because
        stacked instructions contradict each other with no precedence rule an
        operator could predict. ``prompt_append`` is how a scope adds to the
        layer above it, and it is settled into this one value long before here.

        An empty value is a value. A scope that set ``prompt: ""`` asked for
        nothing to be appended, and returning "" for it is that answer, not a
        fall-through to the layer below.

        Nothing here reorders anything: the resolved prompt is appended after
        the user's text. Putting the instructions last is both the more recent
        text, which helps compliance, and the ordering operators have already
        written against.
        """
        override = settled_override(
            self.config,
            channel_id,
            user_id=user_id,
            chat_type=chat_type,
            workspace=self._workspace_team_id,
        )
        return override.prompt if override.prompt is not None else ""

    def _fold_inbound_events(self, channel_id: str) -> str:
        """Everything ``channel_id`` buffered, as text, and forget it.

        Called once per dispatched message, from the one place a turn's input is
        assembled. ``""`` when nothing accumulated, which is every conversation
        that has not asked for events and most turns in the ones that have.

        **Folding is not gated on the policy.** A room whose licence was
        withdrawn between the last turn and this one still hands over what it
        collected while the licence stood: those events are exactly the ones the
        operator asked for, and dropping them at the fold would lose them with
        nothing to say so. What the policy gates is the recording, one gate
        earlier, so nothing new accumulates from the moment it changes.
        """
        return inbound_events_block(self._inbound_events.fold(channel_id))

    def _channel_event_disposition(
        self, channel_id: str, family: str, chat_type: str = ""
    ) -> str:
        """What ``channel_id`` does with one family of non-message events.

        ``delivery.events``, and nothing else. ``off`` is the default and is
        what every conversation has today; ``context`` and ``turn`` are each a
        licence somebody wrote for one room, the second wider than the first.

        **No sender, and the declaration bars one.** An event carries an actor
        -- who reacted, who pinned, who joined -- and that actor is not the
        sender of anything: under ``context`` the record is folded into
        whichever turn runs next, which need not be a turn that person started,
        and under ``turn`` the event starts a turn nobody sent a message for.
        Settling this per sender would buffer a reaction or drop it depending on
        who had last spoken in the room, and nothing in Slack would show either
        half of that.

        Read on the arriving event rather than at the fold, so a room whose
        licence was withdrawn stops collecting immediately instead of on its
        next turn. What is already buffered still folds: it was collected under
        a licence that existed at the time, and dropping it would lose events
        the operator had asked for with no sign that anything had been lost.
        """
        return event_disposition(
            settled_override(
                self.config,
                channel_id,
                chat_type=chat_type,
                workspace=self._workspace_team_id,
            ).events,
            family,
        )

    def _channel_reply_is_optional(self, channel_id: str, chat_type: str = "") -> bool:
        """Whether a turn in ``channel_id`` may decline to answer.

        ``delivery.reply``, and nothing else. ``required`` is the default and is
        what every conversation has today; ``optional`` is a licence somebody
        wrote for one room.

        **Not a trigger, and deliberately not derived from one.** Which trigger
        woke a turn and whether that turn owes an answer are separate questions,
        and one word cannot answer both: two conversations reading every link
        posted in them can want opposite things, one saving bookmarks in silence
        and one writing an analysis of each. A trigger is also a pre-model
        predicate answered from the payload before any session exists -- the
        trigger list says so where it is written -- while this is settled after
        the model has run.

        **One place, for both halves of the feature.** The instruction appended
        to an inbound message and the matcher applied to the outbound reply both
        come here. Neither broken state is reachable: a conversation matched
        against a token it was never told to write would lose real replies, and
        one told to write a token nothing acts on would post it.

        **No sender.** Nothing on an outbound reply says which sender it answers,
        so the matcher has no sender to settle for, and taking none here is what
        keeps the two halves reading one value. The declaration bars the key from
        the identity axes for that reason, so no rule may name one and the answer
        is the same for everybody in the room.

        **Never a direct message, whatever the key says.** In a one-to-one
        conversation an unanswered message reads as the bot being broken rather
        than as a judgement nobody needed to hear, and there is no third party
        for the silence to spare. The id prefix is the test, asked only what it
        can answer -- ``D`` is the one-to-one direct message, and which of the
        three room kinds a ``C`` or a ``G`` names is neither asked nor knowable
        from the id. It is a refusal rather than a default, so no configuration
        reaches past it. Written here rather than as a validator because a
        scope naming a DM is not wrong about anything else it might set.
        """
        # Before the settled word, so the refusal cannot be read as one more
        # thing the key decides.
        if channel_id.startswith("D"):
            return False
        return (
            settled_override(
                self.config,
                channel_id,
                chat_type=chat_type,
                workspace=self._workspace_team_id,
            ).reply
            == REPLY_OPTIONAL
        )

    def _reply_was_optional(self, msg: Message, channel_id: str) -> bool:
        """Whether a token written by the turn behind ``msg`` is honoured.

        Two answers have to agree, and each covers what the other cannot.

        The conversation's settled ``delivery.reply`` is read live, from config
        the model cannot reach. It is what stops a response that arrived with the
        metadata key set from licensing a silence in a room nobody licensed.

        The request metadata is what the inbound path recorded for this request
        when it was dispatched. It is the only account of what the turn behind
        this reply was run under: a reply says nothing on its own about the
        room its request was settled for.

        **Not "was it offered".** The fragment inviting a turn to answer with
        the token is withheld from a message that addressed the bot, and the
        token is honoured there all the same: a model that has read the
        fragment elsewhere in the conversation can reach a correct decision to
        stay silent on a turn that never carried it, and did.
        See :data:`SLACK_REPLY_OPTIONAL_KEY`.

        Absent means no. A scheduled push, a health check, a reply whose metadata
        did not survive the round trip -- none of them carries that record, and
        none of them should have a reply deleted on a token nothing says this
        turn was licensed to write. Every way this can be wrong therefore ends in text being
        posted, which is the failure a reader can see and correct.
        """
        metadata = msg.metadata or {}
        # The kind of conversation this answer came from, off the reply's own
        # metadata, because ``delivery.reply`` may now be addressed on it and
        # the outbound path has no event to read. The inbound half stamped it
        # under the key the history toolkit already reads, so this is the same
        # fact rather than a second record of it, and a reply that arrives
        # without the key reads as no kind -- which withholds the licence and
        # posts the text, the direction the paragraph above keeps every failure
        # pointing in.
        if not self._channel_reply_is_optional(
            channel_id, str(metadata.get("slack_channel_type") or "")
        ):
            return False
        return bool(metadata.get(SLACK_REPLY_OPTIONAL_KEY))

    async def _discard_streamed_preview(
        self, msg: Message, routing_target: RoutingTarget | None
    ) -> None:
        """Take a withheld reply's preview off the screen.

        Streaming is on in the conversations this runs in, so a turn that
        declines to answer has usually already drawn its answer into a message:
        the deltas rendered the token live, and returning without posting would
        leave that message standing. A silence that leaves the token on screen is
        not a silence.

        **The preview is deleted, rather than never opened or closed to nothing.**
        Never opening one under the contract would cost every turn in the room its
        live preview to handle the minority that decline, which is a permanent
        price for an occasional case. Closing it to nothing cannot be done at all
        -- Slack refuses an empty message, as ``_stream_delta`` notes where it
        declines to open one on whitespace -- and were it possible it would leave
        a blank bubble in the thread, which is a message about having nothing to
        say. Deleting spends one call, only on the turns that decline, and leaves
        the thread as it would have been had the turn never spoken.

        The stream is closed first rather than deleted out from under itself.
        ``_close_stream`` waits out any write still in flight, so nothing can land
        on a message after it is gone, and it takes the entry out of the map --
        which is also what stops a withheld reply leaking one until the map is
        reaped.

        Best effort, like every other write here that is not the reply itself. A
        workspace that refuses the delete leaves the preview where the reader can
        see it, which is the same visible failure every other path in this feature
        degrades to.
        """
        channel_id, thread_ts = self._extract_delivery(msg, routing_target)
        if not channel_id or self._client is None:
            return
        preview_ts, _ = await self._close_stream(msg, channel_id, thread_ts)
        if not preview_ts:
            # No preview, or one whose surface had already failed -- in which
            # case ``_close_stream`` has warned about an abandoned message and
            # this has no handle to act on.
            return
        try:
            await self._client.chat_delete(channel=channel_id, ts=preview_ts)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[SlackChannel] the withheld reply's preview could not be"
                " deleted and is still on screen: channel=%s ts=%s reason=%s",
                channel_id,
                preview_ts,
                slack_failure_reason(exc),
            )
            return
        logger.info(
            "[SlackChannel] withheld reply's preview deleted: channel=%s ts=%s",
            channel_id,
            preview_ts,
        )

    def _withhold_on_no_reply(
        self,
        msg: Message,
        content: str,
        routing_target: RoutingTarget | None,
    ) -> bool:
        """Whether this reply is the model declining to speak, so nothing is posted.

        Sits at the delivery boundary beside the empty-reply check, and returns
        the same way it does. Everything the connector had already done for this
        turn stands, and the reaction the message wears is settled by the
        terminal event, not here.

        **A chosen silence is marked as an ordinary ending.** The turn reaches
        ``send``'s terminal branch the same way a replying turn does, so the
        acknowledgement comes off and ``completed_emoji`` goes on. That is the
        right outcome twice over. It does not claim the message was never
        processed, which is what leaving it bare would say and is the state a
        person re-sends into. And it does not broadcast the model's judgement:
        the mark is the one every finished turn gets, so a reader cannot tell a
        turn that chose silence from one that replied, which is the whole point.
        A distinct emoji for silence was considered and rejected for that
        reason -- announcing the decision back into the room is a reply by other
        means.

        **The activity card needs nothing here.** ``_close_activity_card`` has
        already run for this event, above and before any text was read, and it
        was told there what this is about to decide: ``_turn_says_nothing``
        reads the same reply off the same event, so a turn that did tracked work
        has already had its card deleted rather than settled, and a turn that
        did none never posted one. The decision is taken once and consulted
        twice; what is left here is the reply itself and the line that records
        the judgement.

        **The line is written here rather than folded into the inbound outcome.**
        That outcome is settled and logged when the request is dispatched, which
        is before the turn runs; this decision arrives when the turn ends,
        minutes later and on a different event. There is no honest way to put it
        in a string that was already written, so it gets a line of its own, at
        the moment of the decision, naming the conversation and the session. It
        cannot be confused with a delivery failure: a failure raises
        ``SlackDeliveryError`` and is reported as one, while this is the connector
        saying the turn chose to say nothing.

        **The line says whether the message addressed the bot.** Both are
        licensed silences and neither is an error, but only one of them can end
        with a person who wrote the bot's name seeing nothing come back, and
        that is the line somebody greps for after being told the bot ignored
        them. The near-miss warning below carries no such field: it names a
        reply that *was* posted, so nobody goes looking for it under that
        complaint.

        A near miss is a reply that looks like a turn trying to decline and is
        not the token. It changes nothing -- the reply is delivered exactly as it
        would have been -- and is warned about with the whole text, because it is
        the only evidence there is about how often the contract is being reached
        for and missed. The detector is much looser than the matcher above, and
        may be: it decides a log line rather than a delivery.
        """
        channel_id, _ = self._extract_delivery(msg, routing_target)
        if not self._reply_was_optional(msg, channel_id):
            return False
        session_id = str(msg.session_id or "") or "-"
        if is_no_reply_sentinel(content):
            logger.info(
                "[SlackChannel] nothing posted, the turn wrote %s: channel=%s"
                " session=%s addressed=%s",
                NO_REPLY_SENTINEL,
                channel_id or "-",
                session_id,
                "yes" if (msg.metadata or {}).get(SLACK_ADDRESSED_KEY) else "no",
            )
            return True
        if looks_like_a_declined_reply(content):
            logger.warning(
                "[SlackChannel] %s near miss, reply delivered in full:"
                " channel=%s session=%s reply=%r",
                NO_REPLY_SENTINEL,
                channel_id or "-",
                session_id,
                content,
            )
        return False

    def _channel_model_name(
        self, channel_id: str, user_id: str = "", chat_type: str = ""
    ) -> str:
        """The model a message from ``channel_id`` should run on, or ``""``.

        **This check is connector-side**, whichever section declares the key.
        ``model_name`` is an ``agent`` setting because the runtime acts on it;
        this is the connector deciding whether to put it on the wire at all, and
        the fact it depends on lives here -- see the next paragraph.

        Checked again here against the config as it stands, rather than trusting
        what the scope loader settled at load. ``models.defaults`` is rewritten
        at runtime by the WebUI (``models.replace_all``), so a name valid when
        the connector last read its config can be gone by the time a message
        arrives, and nothing reloads the Slack channel when the model list
        changes. Sending the stale name would land on
        ``JiuWenSwarmDeepAdapter._resolve_model_by_name``, which returns the
        default for an unrecognised name and logs nothing.

        Dropping the name rather than sending it changes nothing about which
        model runs: the server would have used its default either way. What it
        buys is the warning.

        Said once per (channel, name). A channel whose model was deleted would
        otherwise warn on every message for as long as the config stays that
        way, which is the shape of log noise that gets a warning filtered out
        and then missed. The set is per connector instance, so a config reload
        re-arms it.

        Costs one config read per message, and only for the channels that
        actually name a model -- every other channel returns before the check.

        ``user_id`` is whoever the model is being chosen *for*, which is not
        always whoever caused this call: a turn resumed by a click is still the
        turn its initiator started, and the click names a different person.
        See the resume path, which passes the initiator rather than the clicker.

        ``chat_type`` travels the same way and for the same reason: a resume has
        no event to read the kind off, so it comes from the initiator entry the
        first dispatch wrote. Both are the turn's own facts rather than the
        click's, and a turn must not change model halfway through because it was
        resumed from somewhere the rule reads differently.
        """
        name = (
            settled_override(
                self.config,
                channel_id,
                user_id=user_id,
                chat_type=chat_type,
                workspace=self._workspace_team_id,
            ).model_name
            or ""
        )
        if not name:
            return ""

        known = configured_models()
        if not known.known or known.accepts(name):
            return name

        key = (channel_id, name)
        if key not in self._reported_missing_models:
            self._reported_missing_models.add(key)
            logger.warning(
                "channels.slack: the model pinned for %s (%r) is no longer in"
                " models.defaults (%s); it was there when the channel was"
                " configured, so a model has been renamed or removed since."
                " That channel runs on %s until the setting -- a scope's"
                " agent.model_name -- or the model list is corrected",
                channel_id,
                name,
                known.describe(),
                known.describe_fallback(),
            )
        return ""

    def _match_channel_trigger(
        self, triggers: frozenset[str], event: dict[str, Any], text: str
    ) -> str | None:
        """Name the first trigger in ``triggers`` that claims this message.

        Union, not intersection: any one entry matching is enough. Evaluation
        short-circuits on the first match, which is a micro-optimisation for the
        two predicates that exist and becomes load-bearing the moment a costlier
        one is added.

        The order is the order the connector already dispatched in, so the
        trigger names it reports downstream do not shift under a config that
        resolves to the same behaviour it had before. ``text`` is the raw event
        text; a leading bot mention has already returned by this point, so there
        is none to strip.
        """
        if TRIGGER_ALL in triggers:
            return "all"
        if TRIGGER_REPLY in triggers and self._is_reply_to_bot(event):
            return "reply"
        if TRIGGER_URL in triggers and _HTTP_URL_RE.search(text):
            return "url"
        if TRIGGER_HAS_FILE in triggers and self._collect_event_files(event):
            return "has_file"
        return None

    def _is_reply_to_bot(self, event: dict[str, Any]) -> bool:
        """True when the message is a thread reply under one of the bot's messages.

        ``parent_user_id`` is only present on thread replies, and names whoever
        posted the thread root. A message whose ``thread_ts`` equals its own ``ts``
        is the root itself, not a reply.

        Fails closed when the bot's own id is unknown -- ``auth.test`` can fail at
        startup, and guessing there would turn ``reply`` mode into ``all``.
        """
        thread_ts = str(event.get("thread_ts") or "").strip()
        if not thread_ts or thread_ts == str(event.get("ts") or "").strip():
            return False
        if not self._bot_user_id:
            return False
        return str(event.get("parent_user_id") or "").strip() == self._bot_user_id

    def _non_user_content_outcome(self, event: Mapping[str, Any]) -> str | None:
        """Name why ``event`` holds no user content, or ``None`` if it does.

        Loop protection, in three parts. ``bot_id``/``bot_profile`` mark anything
        an app posted, this bot's own replies included. The identity check
        catches the same echo when neither field is set: an upload made with a
        bot token comes back as a ``file_share`` attributed to the bot user, and
        ``file_share`` is no longer filtered out by subtype. The subtype check
        then drops every remaining non-content subtype.

        The first part is a ladder rather than a wall, and ``app_messages`` is
        the rung. A message a *person* posted through an app holding a user
        token carries both of them -- ``user`` is the person and is correct,
        ``bot_id``/``app_id`` are only the app that issued the token -- and under
        ``none`` every such message is discarded for the second fact while the
        first one is sitting right there. ``listed`` reads the apps an operator
        named, ``all`` reads every app; either way the message goes down the
        ordinary path as the ``user`` field's message, with nothing reattributed
        because nothing needs to be.

        Two things no rung of that ladder can readmit, and they are checked in
        this order:

        * **This bot.** A floor rather than a policy. Under ``all`` it is the
          only thing between the connector and answering itself, so it is
          matched on ``bot_id`` as well as on the user id -- either identity
          alone is enough, since every message this connector posts carries
          both -- and an app message is refused outright while *neither* is
          known. ``auth.test`` can fail at startup, and a loop that opens up
          only on the deployments where it did is the worst shape this could
          take.
        * **An app posting as itself.** The rungs admit messages with a person
          behind them, and the person is the ``user`` field. With nothing there
          the rest of the connector would be handed a message with no sender,
          which ``allow_from``, the scopes' user axis and the session key each
          assume there is.

        One predicate with two callers rather than a copy in each. The two would
        drift, and the drift would show up as the same Slack event reported under
        two different names -- which is the defect this was factored out to fix.
        """
        if event.get("bot_id") or event.get("bot_profile"):
            mode = normalize_app_messages(self.config.app_messages)
            if mode == APP_MESSAGES_NONE:
                # The default, and byte-for-byte what this returned before the
                # key existed -- the bot's own echoes included, which this word
                # refuses along with everything else rather than by name.
                return "ignored:posted-by-an-app"
            if self._is_own_app_post(event):
                return "ignored:posted-by-this-bot"
            if not (self._bot_id or self._bot_user_id):
                return "ignored:app-post-with-this-bot-unidentified"
            if mode == APP_MESSAGES_LISTED:
                app_id = str(event.get("app_id") or "").strip()
                if not app_id:
                    # A legacy custom integration or an incoming webhook: a
                    # bot_id with no app to key on. Named apart from the one
                    # below because an operator who has written the list and is
                    # still seeing drops needs the difference between "your id
                    # is not this one" and "there is no id here to compare
                    # yours against".
                    return "ignored:posted-by-an-app-with-no-app_id"
                if app_id not in self.config.app_messages_from:
                    return (
                        f"ignored:app-not-in-{KEY_APP_MESSAGES_FROM} ({app_id})"
                    )
            if not str(event.get("user") or "").strip():
                return "ignored:app-posted-with-no-user"
        user_id = str(event.get("user") or "").strip()
        if self._bot_user_id and user_id == self._bot_user_id:
            return "ignored:posted-by-this-bot"
        subtype = str(event.get("subtype") or "").strip()
        if subtype and subtype not in _USER_CONTENT_SUBTYPES:
            return f"ignored:subtype-not-user-content ({subtype})"
        return None

    @staticmethod
    def _event_identity(event: Mapping[str, Any], body: Mapping[str, Any]) -> str:
        """The fields needed to line one inbound event up against Slack itself.

        No text: the boundary line is written for every event including the ones
        that are ignored, and what makes it safe to leave on at INFO forever is
        that it says which message rather than what was in it. ``ts`` is the
        identity Slack itself uses -- it is what ``conversations.history``
        returns and what a permalink ends with -- so a line here can be checked
        against the conversation without anything else being logged.

        **Two fields have a second spelling, because this line is written for
        six event types that are not messages.** A message names its
        conversation as ``channel`` and a reaction names it inside ``item``,
        which is why every such line for a reaction used to read ``channel=-``
        -- the one line written for every inbound event, unable to say which
        room the event was in. The named helpers are used rather than a second
        set of lookups, so this line and the routing agree by construction.

        ``channel_type`` and ``subtype`` stay as they are and go on reading
        ``-`` for these six. Neither is on such a payload, and this is a line
        about what arrived: reporting a kind resolved from somewhere else would
        make the log say Slack sent a field it did not. What the routing settles
        the kind to is a separate question, answered by
        :meth:`_conversation_chat_type` and visible in the outcome.
        """
        family = event_family(event)
        anchor_ts = event_anchor(event, family=family)[0] if family else ""
        return (
            "channel=%s channel_type=%s ts=%s subtype=%s user=%s event_id=%s"
            % (
                str(event.get("channel") or event_chat_id(event) or "-"),
                str(event.get("channel_type") or "-"),
                str(event.get("ts") or anchor_ts or "-"),
                str(event.get("subtype") or "-"),
                str(event.get("user") or "-"),
                str(body.get("event_id") or event.get("client_msg_id") or "-"),
            )
        )

    @staticmethod
    def _click_identity(body: Any, action: Any) -> str:
        """The fields needed to line one click up against Slack itself.

        ``_event_identity``'s rule, applied to an interaction: what was pressed,
        by whom, on which message -- and nothing that was said in any of them.
        The ``action_id`` is the useful half, since it is what the listener was
        registered against and what says which of the two buttons this was.

        Defensive about the payload's shape because its one caller is an
        exception handler: a body that is not the mapping the handler expected
        is one of the things that can have raised, and a line that cannot be
        written is the diagnosis lost.
        """
        body = body if isinstance(body, Mapping) else {}
        action = action if isinstance(action, Mapping) else {}
        user = body.get("user")
        channel = body.get("channel")
        container = body.get("container")
        return "action_id=%s channel=%s user=%s message_ts=%s trigger_id=%s" % (
            str(action.get("action_id") or "-"),
            str((channel.get("id") if isinstance(channel, Mapping) else "") or "-"),
            str((user.get("id") if isinstance(user, Mapping) else "") or "-"),
            str(
                (container.get("message_ts") if isinstance(container, Mapping) else "")
                or "-"
            ),
            str(body.get("trigger_id") or "-"),
        )

    def _log_event_outcome(
        self,
        event: Mapping[str, Any],
        body: Mapping[str, Any],
        source: str,
        outcome: str,
    ) -> None:
        """Report, once per inbound event, what this connector did with it.

        One line, INFO, whichever way it went. A message that is deliberately
        ignored says so and says why: the point is not to record the dispatches
        -- those already leave a trail through the gateway -- but to make the
        silent paths audible, because a Slack message that produces no turn and
        no reply is otherwise indistinguishable from one that never arrived.
        """
        logger.info(
            "[SlackChannel] inbound %s: %s%s outcome=%s",
            source,
            self._event_identity(event, body),
            self._app_messages_note(event),
            outcome,
        )

    def _is_own_app_post(self, event: Mapping[str, Any]) -> bool:
        """True when this connector is what posted ``event``.

        The floor under ``app_messages``, and the reason it is matched two ways.
        ``bot_id`` is on every message this app posts and is learned from
        ``auth.test`` beside the user id, so either field alone identifies the
        echo; requiring both would make the floor depend on whichever of them a
        given Slack payload happened to carry.

        Its caller refuses the message outright when neither id is known, so
        this answering ``False`` there is never read as "somebody else posted
        it".
        """
        bot_id = str(event.get("bot_id") or "").strip()
        if self._bot_id and bot_id == self._bot_id:
            return True
        user_id = str(event.get("user") or "").strip()
        return bool(self._bot_user_id and user_id == self._bot_user_id)

    def _app_messages_note(self, event: Mapping[str, Any]) -> str:
        """`` app_messages=<word> app_id=A…`` when a rung is why this got in.

        The one thing the outcome string cannot say for itself. Every refusal of
        an app-posted message names itself; a message a rung admitted goes on to
        be dispatched, and its outcome is then whatever the dispatch did, with
        nothing left to say that an app was involved at all.

        Written here rather than where the decision is made, because the
        decision is made twice for a channel message -- once by the router and
        again by the dispatch -- and this runs exactly once per inbound event.
        Written only where a rung admits, so a deployment left at ``none`` sees
        the lines it saw before, unchanged. It is attached to the two refusals
        that survive an admitting rung as well: ``app_messages=all app_id=A…
        outcome=ignored:posted-by-this-bot`` is precisely the pair an operator
        who has just widened the word wants to be able to read.
        """
        mode = normalize_app_messages(self.config.app_messages)
        if mode == APP_MESSAGES_NONE:
            return ""
        if not (event.get("bot_id") or event.get("bot_profile")):
            return ""
        app_id = str(event.get("app_id") or "").strip()
        if mode == APP_MESSAGES_LISTED and app_id not in self.config.app_messages_from:
            return ""
        return f" app_messages={mode} app_id={app_id or '-'}"

    async def _handle_app_mention(
        self, event: dict[str, Any], body: dict[str, Any]
    ) -> None:
        outcome = "handler-raised"
        try:
            outcome = await self._route_app_mention(event, body)
        except Exception:  # noqa: BLE001
            # slack_bolt catches whatever a listener raises and reports it on
            # its own logger, which has no handler until configure_sdk_logging()
            # runs -- so a raise here would be silent, and would look from the
            # channel exactly like a message Slack never sent. Logged at this
            # edge as well, so it stays visible whatever the SDK level is.
            logger.exception(
                "[SlackChannel] inbound app_mention raised: %s",
                self._event_identity(event, body),
            )
        finally:
            self._log_event_outcome(event, body, "app_mention", outcome)

    async def _route_app_mention(
        self, event: dict[str, Any], body: dict[str, Any]
    ) -> str:
        self._remember_bot_user_id(body)
        channel_id = str(event.get("channel") or "").strip()
        # The sender, because a scope may name one. Read straight off the event
        # rather than from anything the turn later holds: this is the earliest
        # point at which it exists and the only writer of it is Slack.
        sender_id = str(event.get("user") or "").strip()
        # The kind of conversation, resolved from the conversation rather than
        # from this payload. **Slack sends no ``channel_type`` on an
        # ``app_mention``**, so read off the event it was always the empty
        # string and a rule naming a kind never fired for a mention -- the same
        # defect a reaction had, on the busiest trigger this connector has.
        # See ``_conversation_chat_type``.
        chat_type = self._conversation_chat_type(channel_id, event)
        if not (
            self._channel_triggers(channel_id, sender_id, chat_type)
            & MENTION_ROUTE_TRIGGERS
        ):
            return "ignored:mention-not-a-trigger-here"
        return await self._handle_slack_event(
            event, body, is_dm=False, trigger="mention"
        )

    async def _handle_message_event(
        self, event: dict[str, Any], body: dict[str, Any]
    ) -> None:
        outcome = "handler-raised"
        try:
            outcome = await self._route_message_event(event, body)
        except Exception:  # noqa: BLE001
            # See _handle_app_mention: bolt swallows this, and its logger is not
            # where anyone looks first.
            logger.exception(
                "[SlackChannel] inbound message raised: %s",
                self._event_identity(event, body),
            )
        finally:
            self._log_event_outcome(event, body, "message", outcome)

    async def _route_message_event(
        self, event: dict[str, Any], body: dict[str, Any]
    ) -> str:
        self._remember_bot_user_id(body)
        # The kind of conversation this message came from, kept for the payloads
        # that do not carry one. Ahead of every gate below, including the ones
        # that ignore the message: what a ``channel_join`` notice or a bot's own
        # post says about the *room* is true whether or not the message itself
        # is answered, and a room whose traffic is mostly ignored is exactly
        # where a cold map would bite. See ``_learn_chat_type``.
        self._learn_chat_type(event)
        # Ahead of everything surface-specific, so an edit, a join notice or an
        # echo is named for what it is whether it arrived in a DM or a channel.
        # Not inside _handle_slack_event, which a channel message reaches only
        # once a trigger has claimed it: no trigger claims a channel_join, so the
        # identical event would be reported as ``subtype-not-user-content`` in a
        # DM and as ``no-trigger-matched`` in a channel, where it is
        # indistinguishable from the human conversation around it.
        #
        # Nothing is ignored here that either path would have delivered.
        outcome = self._non_user_content_outcome(event)
        if outcome is not None:
            return outcome

        if str(event.get("channel_type") or "") == "im":
            return await self._handle_slack_event(
                event, body, is_dm=True, trigger="dm"
            )

        # Direct messages are never governed by the group mode; everything below
        # this point is channel traffic, and what wakes the bot in it is decided
        # per channel rather than once for the whole connector.
        channel_id = str(event.get("channel") or "").strip()
        triggers = self._channel_triggers(
            channel_id,
            str(event.get("user") or "").strip(),
            # The one reader, here too. A ``message`` always states its kind, so
            # this is the same value the raw read gave; going through the reader
            # anyway is what leaves ``event_chat_type`` with no caller that
            # decides anything, which is the property that keeps a second
            # convention from growing beside it.
            self._conversation_chat_type(channel_id, event),
        )
        if not triggers:
            return "ignored:no-trigger-configured-for-channel"

        text = str(event.get("text") or "").strip()
        # A leading bot mention is handled by app_mention. Ignoring it here
        # avoids dispatching the same Slack message through two event types.
        if self._has_leading_bot_mention(text):
            return "ignored:leading-bot-mention-handled-as-app_mention"

        trigger = self._match_channel_trigger(triggers, event, text)
        if trigger is None:
            if self._mentions_bot_by_bot_id(text):
                return "ignored:bot-id-mention-not-a-trigger"
            return "ignored:no-trigger-matched"
        return await self._handle_slack_event(
            event, body, is_dm=False, trigger=trigger
        )

    async def _handle_inbound_event(
        self, event: dict[str, Any], body: dict[str, Any]
    ) -> None:
        """Record one non-message event, or start the turn it was licensed to.

        The same shape ``_handle_app_mention`` has, for the same reason: bolt
        catches whatever a listener raises and reports it on its own logger,
        which has no handler until ``configure_sdk_logging`` runs, so a raise
        here would be silent and would look exactly like an event Slack never
        sent.
        """
        outcome = "handler-raised"
        try:
            outcome = await self._route_inbound_event(event, body)
        except Exception:  # noqa: BLE001
            logger.exception(
                "[SlackChannel] inbound event raised: %s",
                self._event_identity(event, body),
            )
        finally:
            self._log_event_outcome(
                event,
                body,
                str((event or {}).get("type") or "-"),
                outcome,
            )

    async def _route_inbound_event(
        self, event: dict[str, Any], body: dict[str, Any]
    ) -> str:
        """Where one non-message event ends up, named.

        A reaction, a pin, a membership change, or somebody opening this app's
        App Home.

        Returns the outcome its caller logs, the way the two message routers do.
        Most exits are an event that produces no turn and no reply: ``off``
        drops and ``context`` buffers. The one exit that dispatches is ``turn``,
        and every way it can decline to dispatch buffers the event instead of
        losing it -- see :meth:`_wake_turn_for_event`.

        **The dispatching exit is unreachable for ``app_home``.** That family is
        outside ``EVENT_TURN_FAMILIES``, so the loader refuses ``turn`` where an
        operator writes it and the read gate below answers ``off`` for a mapping
        that reached here some other way. Nothing in this method special-cases
        it: the vocabulary is what closes the path, which is why the vocabulary
        is where the reason is written.
        """
        if not self._running:
            return "dropped:channel-not-running"
        if not isinstance(event, dict) or not isinstance(body, dict):
            return "dropped:malformed-payload"
        family = event_family(event)
        if family is None:
            # Reachable only if a listener is registered for a type the family
            # table does not name, which the registration loop makes impossible
            # -- it reads the same table. Kept because "impossible" is a claim
            # about today's registration and this is the function that would
            # otherwise buffer a record under a family nobody declared.
            return "ignored:event-type-has-no-family"

        # ------------------------------------------------------------------
        # Self-filtering, and it comes before everything else on purpose.
        # ------------------------------------------------------------------
        # This connector reacts to every inbound message with a lifecycle mark
        # -- eyes on arrival, a check when the turn ends -- so without this
        # filter the first thing a room with reaction: context would feed the
        # model is the bot's own acknowledgement of the very message that
        # started the turn, inside that same turn. It would then mark its own
        # answer and feed that back too.
        #
        # Filtered on the **actor**: who reacted, who pinned, who joined, who
        # opened App Home. Never on the author of the item acted upon, which a
        # reaction payload also carries as item_user. A filter written
        # against that one is exactly
        # backwards -- it drops everyone else's reactions to the bot's answers
        # and keeps the bot's own reactions to everyone else's messages, which
        # is the loop that exists.
        #
        # Not every family can loop. App Home is opened by a person in a Slack
        # client and this app has no client, so nothing this connector does can
        # produce an app_home_opened at all -- the filter is a floor there
        # rather than a fix. It is applied to every family regardless, because
        # a filter with an exception list is a filter somebody has to keep
        # correct.
        #
        # First, rather than after the policy read, so that a room which has
        # not asked for events cannot make the order of these checks matter, and
        # so that adding a gate below can never accidentally move the filter
        # down. The bot's own id is learned from the envelope on the way past,
        # which is part of the filter and not a step before it: without the id
        # there is nothing to compare against, and every event would pass.
        self._remember_bot_user_id(body)
        actor_id = event_actor_id(event)
        if not actor_id:
            return "dropped:no-actor-on-event"
        if self._bot_user_id and actor_id == self._bot_user_id:
            return "ignored:done-by-this-bot"
        if event.get("bot_id") or event.get("bot_profile"):
            # The same second half ``_non_user_content_outcome`` gives a
            # message: an app-attributed event whose actor id is not ours is
            # still an app's, and the id is not always set on one.
            return "ignored:done-by-an-app"

        chat_id = event_chat_id(event)
        if not chat_id:
            return "dropped:no-conversation-on-event"

        # The opt-out the operator wrote for messages holds for events too. A
        # platform-wide events rule would otherwise reach into every room the
        # workspace has, including the ones allowed_channel_ids exists to keep
        # this connector out of -- and reading a room is exactly what that list
        # refuses. The scoped-conversation exemption is the message path's, for
        # the message path's reason: a scope is hand-written by the operator and
        # already says the conversation is on, except availability-only scopes.
        if (
            not slack_scope_enables_channel(self.config, chat_id)
            and self.config.allowed_channel_ids
            and chat_id not in self.config.allowed_channel_ids
        ):
            return "ignored:channel-not-in-allowed_channel_ids"
        # allow_from, applied to the actor and silently. A message from someone
        # outside the list is refused with a reaction saying so; an event is not
        # a request, so there is nothing to refuse and nobody to tell. What the
        # list settles here is whose actions reach the model at all, which is
        # the narrow reading of a list an operator wrote to draw exactly that
        # line. An empty list -- the default -- admits everybody.
        if not self.is_allowed(actor_id):
            return "refused:actor-not-in-allow_from"

        # Resolved once here and handed down, rather than read again inside the
        # turn path. Both the disposition and everything ``_wake_turn_for_event``
        # settles are answers about one conversation, and asking twice invites
        # the two to differ.
        chat_type = self._conversation_chat_type(chat_id, event)
        disposition = self._channel_event_disposition(chat_id, family, chat_type)
        if disposition == EVENT_OFF:
            return f"ignored:events-off-for-{family}-here"

        record = render_event(event, family=family)

        if disposition == EVENT_TURN:
            # Every refusal inside this buffers the event and says which
            # refusal it was, so a room set to ``turn`` is never worse off for
            # events than the same room set to ``context``.
            return await self._wake_turn_for_event(
                event,
                body,
                family=family,
                chat_id=chat_id,
                chat_type=chat_type,
                actor_id=actor_id,
                record=record,
            )

        if disposition != EVENT_CONTEXT:
            # Unreachable: the read gate answers with one of the three words
            # and the two above are taken. Kept because this is the line a
            # fourth word would fall through, and falling through here means
            # buffering a room's events under a licence nobody granted.
            return f"dropped:unknown-events-disposition-for-{family}"

        # Not deduplicated, unlike a message, and the asymmetry is deliberate.
        # The dedup store is a locked read-modify-write of a file on disk; a
        # message pays for that once, while a reaction storm would pay for it
        # per emoji. What it would buy is one duplicate record in a fold on the
        # rare envelope Slack replays -- noise in a payload, where the same
        # replay on the message path would have been a second answer posted into
        # the room. The costs are not comparable and neither are the harms.
        #
        # The ``turn`` branch above does deduplicate, and for the reason this
        # one does not: what a replayed envelope buys there is a second turn.
        self._inbound_events.record(chat_id, family, record)
        return f"buffered:{family}"

    async def _wake_turn_for_event(
        self,
        event: dict[str, Any],
        body: dict[str, Any],
        *,
        family: str,
        chat_id: str,
        chat_type: str,
        actor_id: str,
        record: dict[str, Any],
    ) -> str:
        """Start a turn over one event, or say why it was buffered instead.

        Reached only for a family that took ``turn`` and only after every gate
        the buffering path passes -- the self-filter first of all, which runs
        above this in :meth:`_route_inbound_event` and never below it.

        **Every refusal here buffers rather than drops.** A room that asked for
        ``turn`` asked for more attention to its events than ``context`` gives,
        not less, so a turn that cannot be started leaves the event where a room
        set to ``context`` would have left it: in the buffer, for whichever turn
        runs next. Each refusal returns its own outcome and logs its own line,
        because "the event was buffered" is the same sentence whichever refusal
        produced it, and a reader needs to know which one did.

        **The destination is the anchor, and there are two kinds.** A reaction
        and a pin are about a message, so the request is stamped with the same
        ``slack_channel_id`` / ``slack_thread_ts`` an inbound message in that
        thread would stamp. A membership change is about the room, so it is
        stamped with the channel and with ``post_as_root``, which is the key
        the connector already reads to put a reply at channel top level. Either
        way the reply travels the existing delivery ladder: no rung was added
        for this and none is bypassed.
        """
        anchored = family_anchors_on_a_message(family)
        # Asked of the event as well as of the family, because the family says
        # where an answer *would* go and a single event can still fail to name
        # the message the family promised. A reaction on a *file* is the case:
        # its family takes ``turn`` and anchors on a message, and that
        # particular event names none. Buffered rather than answered at the
        # channel root -- root is the member family's destination, settled by
        # what a membership change is about, and not a fallback for an event
        # that lost its message.
        anchor_ts, anchor_thread_ts = event_anchor(event, family=family)
        if anchored and not anchor_ts:
            return self._buffer_instead_of_waking(
                chat_id,
                family,
                record,
                "no-anchor",
                "the event names no message to answer",
            )

        team_id = self._team_id(body, event)
        if not team_id:
            # The same refusal the message path makes, for the same reason: the
            # team is the first field of every session id, and an event with no
            # workspace on it would be answered out of a bucket shared with
            # whatever other install is in the same state.
            return self._buffer_instead_of_waking(
                chat_id,
                family,
                record,
                "no-team",
                "the event named no workspace, so it has no session of its own",
            )

        # ``chat_type`` is settled by the caller, from the conversation rather
        # than from this payload -- see ``_conversation_chat_type``, and see
        # ``event_chat_type`` for why the payload alone cannot answer it. It is
        # what every rule naming a kind is matched against below: the events
        # disposition that licensed this call, the ``delivery.reply`` contract
        # tested next, the session scope, the standing prompt and the model.
        #
        # The id prefix, which is what tells the two apart everywhere else in
        # this connector. Read here as "is this the one-to-one conversation",
        # which the prefix states outright.
        is_dm = chat_id.startswith("D")

        # ------------------------------------------------------------------
        # The one conservative choice in this path, and the one place to
        # change it.
        # ------------------------------------------------------------------
        # ``delivery.reply: required`` obliges every turn in this conversation
        # to produce a reply. A turn woken by an event under that contract
        # would be *obliged* to say something about a reaction nobody asked
        # about, in the thread of somebody else's message. So ``turn`` is
        # treated as ``context`` there, and the room gets the reaction in its
        # next turn instead of a turn of its own.
        #
        # **This is conservative rather than settled.** The other reading is
        # that ``reply`` was written about messages and has nothing to say
        # about an event, which would leave an event turn optional whatever the
        # key holds. That reading is defensible and is not the one implemented,
        # because the failure it risks is the bot talking unbidden in a room
        # that never licensed silence, and the failure this one risks is a
        # feature quietly doing nothing until the operator writes one more key.
        # The second is the one an operator can see and fix.
        #
        # One consequence worth stating: ``reply`` is never optional in a direct
        # message -- ``_channel_reply_is_optional`` refuses it there outright --
        # so an event in a DM always takes this exit. A DM that wants its
        # reactions read gets them folded into its next turn.
        if not self._channel_reply_is_optional(chat_id, chat_type):
            return self._buffer_instead_of_waking(
                chat_id,
                family,
                record,
                "reply-required",
                "a required reply would oblige this turn to speak about the"
                " event; set delivery.reply to optional for this conversation"
                " to let an event turn stay silent",
            )

        # ------------------------------------------------------------------
        # Which session, and why an unanchored event is always the room's.
        # ------------------------------------------------------------------
        # ``session: thread`` keys a channel session on the thread root, which
        # is the right answer for a reaction or a pin: the anchor names a
        # thread, and the turn belongs to the conversation that thread is.
        #
        # A membership change names no thread, so that setting has nothing to
        # say about it -- there is no thread for it to key on. Left to fall
        # through, ``_session_id`` would build ``slack_{team}_{channel}_`` from
        # an empty root: an id nobody chose, ending in a separator, shared by
        # every membership change in the room by accident rather than by
        # decision. So the channel-wide form is taken **explicitly** here. It is
        # the existing form, chosen for its meaning: it is the id whose subject
        # is the room, and the room is what a membership change is about.
        #
        # This is the same move ``_session_id`` already makes for a direct
        # message, which ignores the setting because a DM is one conversation
        # whatever the setting says. No new session form is invented.
        #
        # One consequence, stated rather than left to be found: under
        # ``session: thread`` a member turn runs on ``slack_{team}_{channel}``
        # while message turns in that room run on per-thread ids, so a member
        # event does not see a message turn running in a thread and will start
        # its own beside it. Under ``session: channel`` the two share one id and
        # the in-flight fold below covers both. What the channel-wide form does
        # guarantee under either setting is that membership changes share one id
        # with each other, which is what folds a bulk invite into one turn.
        session_is_channel_wide = not is_dm and (
            not anchored
            or self._session_scope(chat_id, actor_id, chat_type) == SESSION_CHANNEL
        )
        session_id = self._session_id(
            team_id=team_id,
            channel_id=chat_id,
            user_id=actor_id,
            # The anchor's thread, so the turn lands in the conversation the
            # message it answers belongs to. Empty for a membership change, and
            # unread there: the channel-wide branch above ignores it, the way it
            # is ignored for a message under ``session: channel``.
            root_thread_ts=anchor_thread_ts,
            is_dm=is_dm,
            channel_wide=session_is_channel_wide,
        )
        if not session_id:
            # Unreachable while the team check above stands, since that is the
            # only thing that empties the id. Kept because the two are separate
            # claims and this is the one that would dispatch into a shared
            # bucket if they ever came apart.
            return self._buffer_instead_of_waking(
                chat_id,
                family,
                record,
                "no-session",
                "no session id could be built for the anchor",
            )

        # ------------------------------------------------------------------
        # No stampede.
        # ------------------------------------------------------------------
        # An emoji storm on one message is the ordinary shape of reaction
        # traffic, and every reaction in it would otherwise be a turn. Folded
        # into the buffer instead, so the turn already running carries them:
        # that is the same outcome ``context`` would have given, reached for
        # the reason ``context`` exists.
        #
        # **Not a mid-turn word.** ``queue``, ``cancel`` and ``steer`` are
        # defined for messages somebody sent, and each answers "what happens to
        # what this person said". Nobody said anything here, so none of the
        # three has an answer and this path does not consult them.
        if self.turn_initiator(session_id) is not None:
            return self._buffer_instead_of_waking(
                chat_id,
                family,
                record,
                "turn-in-flight",
                f"a turn is already running on session={session_id}",
            )
        # A session paused on a question is not running by the record above --
        # the pause closes the initiator entry -- and is still the worst moment
        # to start something else. An ordinary message withdraws the buttons on
        # its way past, which is right for a person typing a follow-up and
        # wrong for a reaction: somebody looking at an approval prompt would
        # find it dead because a third party added an emoji.
        if self._session_awaits_an_answer(session_id):
            return self._buffer_instead_of_waking(
                chat_id,
                family,
                record,
                "question-on-screen",
                f"session={session_id} is waiting on an answer from somebody",
            )

        # Deduplicated, unlike a buffered event. What a replayed envelope buys
        # here is a second turn in the room, which is the harm the message path
        # pays a locked file write to avoid; the buffering path declines that
        # cost because what it would buy there is one duplicate record.
        event_id = str(
            body.get("event_id") or event.get("event_ts") or ""
        ).strip()
        dedupe_key = ":".join(
            part for part in (team_id, chat_id, family, event_id) if part
        )
        if dedupe_key and not await self._remember_event(dedupe_key):
            return f"ignored:already-handled (dedupe) family={family}"

        # ------------------------------------------------------------------
        # The text, in the order the message path assembles its own.
        # ------------------------------------------------------------------
        # Facts first, then the markers that state where this is, then the
        # instructions. The event itself stands where a sender's message would:
        # it is what the turn is about, and it is rendered in the field names
        # the Slack tools already use rather than described in prose.
        #
        # Anything buffered for this room is folded in behind it, exactly as a
        # message turn folds it, so a room set to ``turn`` does not accumulate
        # a buffer nothing ever empties.
        #
        # ``NO_REPLY_INSTRUCTION`` is appended unconditionally, which is sound
        # only because the ``required`` exit above has already run: reaching
        # this line means the conversation licensed silence, so the contract is
        # one the operator wrote rather than one this path granted itself.
        # ``location_label`` is empty for an unanchored event and the join
        # below drops it, so a membership change carries the trigger label
        # alone. Nothing stands in a ts it does not have: an identifier that
        # addresses no message is worse than an absent one, because a model
        # would take it to a tool.
        text = "\n\n".join(
            part
            for part in (
                event_turn_block(record),
                f"{location_label(anchor_ts, anchor_thread_ts)}"
                f" {trigger_label(TRIGGER_EVENT, has_prompt=True)}".strip(),
                self._fold_inbound_events(chat_id),
                event_turn_instruction(anchored_on_a_message=anchored),
                self._channel_prompt(chat_id, actor_id, chat_type),
                NO_REPLY_INSTRUCTION,
            )
            if part
        )

        metadata: dict[str, Any] = {
            "user_id": actor_id,
            "slack_event_id": event_id,
            "slack_team_id": team_id,
            # The two the delivery ladder reads, on its second rung, and the
            # whole of how an event turn's reply finds its way back. The anchor
            # is the destination: its conversation, and the thread it sits in.
            # The conversation is always known; the thread is empty for an
            # unanchored event, which is what ``post_as_root`` below is for.
            "slack_channel_id": chat_id,
            "slack_thread_ts": anchor_thread_ts,
            # The resolved kind -- see the identical line on the message
            # path for why the last resort is "channel" and not an absent
            # key. One thing this fixes outright: before the kind was
            # resolved, a turn woken by a ``member_joined_channel`` stamped
            # Slack's one-letter ``"C"`` here, and ``read_slack_conversation``
            # refuses an origin type it does not recognise -- so the history
            # tool was unusable inside exactly the turns that most needed to
            # look at the room.
            "slack_channel_type": "im" if is_dm else (chat_type or "channel"),
            "slack_user_id": actor_id,
            # Settled for the actor, who is the nearest thing to a principal an
            # event has: the reply is posted into this conversation, and the
            # person whose action produced the turn is the one whose reach the
            # rule is about.
            **slack_history_request_metadata(
                self.config,
                chat_id,
                user_id=actor_id,
                workspace=self._workspace_team_id,
            ),
            **slack_write_request_metadata(self.config),
        }
        if anchored:
            # The anchor, not the event. What a tool would take back to address
            # this is the message that was reacted to; the reaction has no ts a
            # tool accepts. Absent for a membership change, because there is no
            # message and an absent key is how a reader already tells that from
            # one that went missing.
            metadata["slack_message_ts"] = anchor_ts
            metadata["message_ts"] = anchor_ts
        else:
            # The channel's top level, through the key the connector already
            # reads for a cron result that belongs there. Written even though
            # ``slack_thread_ts`` is empty above, because the two are not the
            # same statement: an empty thread lets a rung further up the ladder
            # supply one -- a routing target carrying a ``SlackDeliveryTarget``
            # is tried before metadata -- while this is applied once over the
            # whole ladder and outranks every rung. A turn about the room has to
            # land in the room, not in whichever thread something upstream knew
            # about.
            metadata[POST_AS_ROOT_KEY] = True
        metadata["slack_trigger"] = TRIGGER_EVENT
        # Always true on this path, by the exit above. Written the same way the
        # message path writes it -- only when true -- so the outbound half
        # cannot tell an event turn from a message turn whose token is honoured
        # the same way, which is correct: the contract is the same one. Nothing
        # addresses the bot here, so the addressed key is never stamped and an
        # event turn's silence is logged as the unaddressed case it is.
        metadata[SLACK_REPLY_OPTIONAL_KEY] = True
        action_token = _slack_action_token(event, body)
        if action_token:
            # Imported where it is used: the harness tool package initialises
            # the whole toolset, and this module is imported by
            # ``channel_manager``'s package, which
            # ``test_gateway_lazy_exports`` keeps free of that cost. By the
            # time an event arrives the toolset is loaded anyway.
            from jiuwenswarm.agents.harness.common.tools.slack_search import (
                SLACK_ACTION_TOKEN_KEY,
            )

            metadata[SLACK_ACTION_TOKEN_KEY] = action_token

        params: dict[str, Any] = {"content": text, "query": text}
        params.update(slack_agent_request_params(
            self.config, chat_id, user_id=actor_id, chat_type=chat_type,
            workspace=self._workspace_team_id,
        ))
        model_name = self._channel_model_name(chat_id, actor_id, chat_type)
        if model_name:
            params["model_name"] = model_name

        req = Message(
            id=event_id or f"slack-event-{int(time.time() * 1000)}",
            type="req",
            channel_id=self.channel_id,
            session_id=session_id,
            params=params,
            timestamp=time.time(),
            ok=True,
            provider="slack",
            chat_id=chat_id,
            user_id=actor_id,
            req_method=ReqMethod.CHAT_SEND,
            is_stream=self._streaming_enabled(),
            app_id=self.app_id,
            bot_id=self.app_id,
            metadata=metadata,
        )

        self._remember_stream_recipient(session_id, actor_id, team_id)
        # **No mark coordinates, and that is the truthful entry rather than a
        # missing one.** Those two fields say where this turn's ending is to be
        # written back, and an event turn acknowledges nothing: no reaction is
        # added to the anchor and no thinking status is opened. There is
        # therefore no mark to replace when it ends, and a pair naming the
        # anchor would have the bot marking somebody's message as though that
        # message had been picked up -- when what was picked up was a third
        # party's reaction to it, possibly to be answered with silence.
        self._remember_turn_initiator(
            session_id,
            actor_id,
            req.id,
            is_dm=is_dm,
            chat_type=chat_type,
            # Both of the facts an interrupt would otherwise cost this turn are
            # on this bag and nowhere else: an event turn always holds the
            # silence contract, and an unanchored one is routed to the room.
            request_metadata=req.metadata,
        )
        await self._route_request(req)
        logger.info(
            "[SlackChannel] event woke a turn: family=%s channel=%s anchor=%s"
            " actor=%s session=%s request_id=%s",
            family,
            chat_id,
            # "channel-root" rather than "-", because the two say different
            # things: this turn has a destination and it is the room, where a
            # dash would read as one the connector failed to work out.
            anchor_ts if anchored else "channel-root",
            actor_id,
            session_id,
            req.id,
        )
        return f"dispatched:{TRIGGER_EVENT}:{family} session={session_id}"

    def _buffer_instead_of_waking(
        self,
        chat_id: str,
        family: str,
        record: "Mapping[str, Any]",
        reason: str,
        explanation: str,
    ) -> str:
        """Keep an event that was licensed to wake a turn but could not, and say so.

        One place, so that every refusal in :meth:`_wake_turn_for_event` lands
        the event in the same buffer and reports itself in the same shape. The
        reason is a fixed word for a log reader to grep and the explanation is
        the sentence for a person reading one line; both are here because a
        refusal that only said "buffered" would be indistinguishable from a
        room that had merely asked for ``context``.
        """
        self._inbound_events.record(chat_id, family, record)
        logger.info(
            "[SlackChannel] event buffered instead of waking a turn:"
            " family=%s channel=%s reason=%s -- %s",
            family,
            chat_id,
            reason,
            explanation,
        )
        return f"buffered:{family} (turn-downgraded:{reason})"

    def _session_awaits_an_answer(self, session_id: str) -> bool:
        """Whether an interrupt in ``session_id`` still has buttons on screen.

        The same set :meth:`_withdraw_questions_for_session` retires, asked
        rather than retired: this is a read, and the caller's whole point is
        that an event must not retire anything.
        """
        if not session_id:
            return False
        return any(
            entry.session_id == session_id
            and entry.withdrawn_at is None
            and entry.source in _INTERRUPT_RESUME_SOURCES
            for entry in self._pending_questions.values()
        )

    async def _handle_unclaimed_event(
        self, event: dict[str, Any], body: dict[str, Any]
    ) -> None:
        """Acknowledge an event no named listener took, and name it once.

        The acknowledgement is the point and it is bolt's to give: a listener
        that matched at all leaves the dispatcher on its 200, which is what the
        Socket Mode adapter sends back. This body does nothing to the event.

        **Never acts on the payload.** A type reaching here is one nothing in
        this connector was written to read, so reading it would be guessing at
        a shape. Acknowledged and dropped is the correct treatment of an event
        whose subscription outlived, or arrived ahead of, its handler.

        Reported once per event type rather than once per event. Bolt's own
        ``warning_unhandled_request`` fired per envelope and is now silenced by
        this listener matching, so something has to say that a subscription has
        no reader -- but a widened manifest can deliver those at message rate,
        and a warning at message rate is a warning nobody reads. The set is per
        instance, so a reconfiguration re-arms it.
        """
        event_type = str((event or {}).get("type") or "").strip() or "-"
        if event_type not in self._unclaimed_event_types:
            self._unclaimed_event_types.add(event_type)
            logger.info(
                "[SlackChannel] inbound %s: acknowledged and dropped, no"
                " listener reads it. Either the app manifest subscribes to it"
                " and nothing here was written for it, or the subscription is"
                " newer than this build",
                event_type,
            )

    async def _handle_question_action(
        self, ack: Any, body: dict[str, Any], action: dict[str, Any]
    ) -> None:
        """Answer a waiting turn with the option whose button was pressed.

        Acknowledged first and unconditionally. Slack gives an interaction three
        seconds before it shows the person who clicked an error, and everything
        below -- two API calls and a dispatch into the gateway -- can take
        longer than that. The acknowledgement says the click arrived, not that
        the answer was accepted.

        The posted message is then rewritten without its buttons, so the same
        question cannot be answered twice, and the answer is dispatched. In that
        order: the rewrite is what a second clicker sees, and leaving the buttons
        up while the answer travels invites the second click this exists to
        prevent. The record of the question is claimed before either, which is
        what makes two clicks that raced resolve to one answer.

        Who may press the button is settled before any of that, in two clauses.
        The allow list is the first and is unchanged. The second is a
        ``clicks.approve`` rule for this conversation, which narrows the allow
        list and can never widen it -- and which refuses a click it cannot place
        a clicker for, rather than waving it through. There is no starter's
        allowance here of the kind the stop button has: a
        wrongly permitted stop wastes work, whereas a wrongly permitted approval
        is the tool call the question existed to stop, and starting a turn is no
        claim to answer what it goes on to ask.

        A question the session has already moved past is the one case that
        answers nothing at all. It is checked before the record is claimed and
        left in place afterwards, so that every click on it -- not only the
        first -- is reported as the stale click it is rather than mistaken for a
        question this process never posted.

        An input question reaches here twice over: once per change to any of its
        elements, which is acknowledged and otherwise ignored, and once when
        submit is pressed, which is the only one that answers anything. The
        change events are not read because they do not need to be -- every
        ``block_actions`` payload holds ``state.values``, the whole current
        contents of the message, so the submit alone has everything. They are
        still acknowledged, because Slack shows the person an error otherwise.

        Guarded the way the two event listeners are, and for a reason a click
        makes worse than a message does. slack_bolt catches whatever a listener
        raises and reports it on its own logger, so a raise here is silent at
        this edge; and everything below runs *after* the acknowledgement, so
        what the person sees is a button that reported success and did nothing
        -- a question still on screen with its answer nowhere.
        """
        try:
            await self._route_question_action(ack, body, action)
        except Exception:  # noqa: BLE001
            logger.exception(
                "[SlackChannel] a question click raised: %s",
                self._click_identity(body, action),
            )

    async def _route_question_action(
        self, ack: Any, body: dict[str, Any], action: dict[str, Any]
    ) -> None:
        await ack()
        if not self._running:
            return
        if not isinstance(body, dict) or not isinstance(action, dict):
            return

        action_id = str(action.get("action_id") or "")
        if action_id.startswith(_QUESTION_INPUT_ACTION_ID_PREFIX):
            # A picker moved, a menu opened, a box was typed in. The
            # acknowledgement above is the entire response: nothing is
            # accumulated here, so nothing is lost by not reading it.
            logger.debug(
                "Slack question input changed, waiting for submit: action_id=%s",
                action_id,
            )
            return

        decoded = self._decode_button_value(action)
        if decoded is None:
            return
        request_id = str(decoded.get("request_id") or "").strip()

        user_id = str((body.get("user") or {}).get("id") or "").strip()
        if user_id and not self.is_allowed(user_id):
            # An approval is exactly the kind of thing the allow list exists to
            # gate: anyone who can see the message can press the button, and
            # membership of the channel is not permission to drive the agent.
            logger.warning(
                "Slack answer refused from a user outside allow_from: "
                "user=%s request_id=%s",
                user_id,
                request_id,
            )
            return

        chat_id, _clicked_ts = self._click_target(body)
        verdict = self._clicks_verdict(CLICK_APPROVE, chat_id, user_id)
        if verdict is not None and not verdict[0]:
            # The approval is the security-relevant click -- it is the gate
            # on a tool call the agent was told to stop at -- so a rule that
            # matches and a clicker it cannot place is refused, and the reason
            # names the conversation the rule was written for. The question is
            # left standing and its buttons are left up: this click answered
            # nothing, and somebody the rule does name still can.
            logger.warning(
                "Slack answer refused by a clicks rule: user=%s chat=%s "
                "request_id=%s reason=%s",
                user_id or "-",
                chat_id or "-",
                request_id,
                verdict[1],
            )
            await self._notify_clicker(
                body, user_id, _ANSWER_REFUSED_NOTICE, notice="answer-refused"
            )
            return

        withdrawn = self._pending_questions.get(request_id)
        if withdrawn is not None and withdrawn.withdrawn_at is not None:
            # The question was retired when the session moved past it, and the
            # rewrite that should have taken the buttons away either failed or
            # has not reached this client. Routing the answer now would deliver
            # the option's label into a session with nothing paused, where it
            # reads as a message the user typed, so it goes no further than this
            # log and a second attempt at the rewrite.
            logger.warning(
                "Slack answer ignored for a question withdrawn %.0fs earlier: "
                "request_id=%s source=%s user=%s",
                max(time.monotonic() - withdrawn.withdrawn_at, 0.0),
                request_id,
                withdrawn.source or "-",
                user_id,
            )
            await self._close_question_message(body, note=_WITHDRAWN_QUESTION_NOTE)
            return

        pending = self._pending_questions.get(request_id)
        if pending is None:
            # Already answered, or posted by a process that has since restarted.
            # Either way the turn that asked is no longer waiting on this click,
            # and answering again risks approving something twice.
            logger.info(
                "Slack answer ignored for a question that is no longer waiting: "
                "request_id=%s user=%s",
                request_id,
                user_id,
            )
            await self._close_question_message(body, note=_WITHDRAWN_QUESTION_NOTE)
            return

        if pending.inputs:
            values, label, problem = self._read_submitted_inputs(pending, body)
            if problem:
                # Not an answer and not a stale click: the question is still
                # waiting and its fields are still on screen, so the submit is
                # refused without claiming it and the person is told what is
                # missing. Claiming it here would leave the turn paused forever
                # behind a message nobody can complete.
                logger.info(
                    "Slack submit refused as incomplete: request_id=%s user=%s "
                    "reason=%s",
                    request_id,
                    user_id,
                    problem,
                )
                await self._report_incomplete_submit(
                    body, user_id=user_id, problem=problem
                )
                return
        elif action_id == _QUESTION_SUBMIT_ACTION_ID:
            # A submit against a record holding no inputs. Nothing renders that
            # pairing, so the payload is a replay or a hand-built copy. Refused
            # rather than resolved: the shared resolver falls back to the label
            # Slack echoes, which here would answer the waiting turn with the
            # word on the button.
            logger.warning(
                "Slack submit ignored for a question with nothing to read: "
                "request_id=%s user=%s",
                request_id,
                user_id,
            )
            return
        else:
            label, value = self._resolve_answer(pending, decoded, action)
            values = [value]

        # Claimed only once the click is known to answer something, and with no
        # await between the lookup above and here, so two clicks in flight at
        # once still cannot both find the question unanswered.
        self._pending_questions.pop(request_id, None)
        await self._close_question_message(
            body, note=self._answered_note(label, user_id)
        )
        await self._dispatch_answer(pending, values=values, body=body, user_id=user_id)

    @staticmethod
    def _read_submitted_inputs(
        pending: _SlackPendingQuestion, body: Mapping[str, Any]
    ) -> tuple[list[str], str, str]:
        """Read a submit's ``state.values``, returning ``(values, label, problem)``.

        ``problem`` is non-empty when the submit cannot be answered with, and is
        phrased for the person who pressed it: an input left empty, or one
        holding something that is not the kind of value it asked for. All three
        of the failures are explicit -- nothing picked, a malformed value, and a
        required field left blank -- because none of them can be assumed away by
        a client that Slack lets anyone build a copy of.

        ``state.values`` is read whole and once. It is the complete contents of
        every element in the message at the instant submit was pressed, which is
        why the change events that preceded it were ignored.
        """
        state = body.get("state")
        state_values = state.get("values") if isinstance(state, Mapping) else None
        if logger.isEnabledFor(logging.DEBUG):
            # Keys and value types, never the values themselves. What this
            # answers is whether Slack's payload is shaped the way the element
            # table says it is -- the one thing about this that cannot be
            # checked without a real click -- and it answers it without writing
            # what somebody typed into a log.
            logger.debug(
                "Slack submit state: request_id=%s %s",
                pending.request_id,
                "; ".join(
                    "{}={{{}}}".format(
                        action_id,
                        ",".join(
                            f"{key}:{type(value).__name__}"
                            for key, value in sorted(entry.items())
                        ),
                    )
                    for action_id, entry in sorted(
                        slack_inputs.flatten_state(state_values).items()
                    )
                )
                or "(empty)",
            )
        results = slack_inputs.read_state(
            pending.inputs,
            state_values,
            action_id_prefix=_QUESTION_INPUT_ACTION_ID_PREFIX,
        )
        problem = slack_inputs.submit_problem(pending.inputs, results)
        if problem:
            return [], "", problem
        values = slack_inputs.compose_answer(pending.inputs, results)
        if not values:
            return [], "", "nothing was selected"
        return values, ", ".join(values), ""

    async def _handle_stop_action(
        self, ack: Any, body: dict[str, Any], action: dict[str, Any]
    ) -> None:
        """Stop the turn whose card's stop button was pressed.

        Acknowledged first and unconditionally, for the reason every other
        interaction is: Slack shows the person an error if nothing answers
        within three seconds, and the dispatch below can take longer. The
        acknowledgement says the click arrived, not that the turn was stopped.

        Then three questions in this order, and the order is the point.

        **May this person stop it.** The floor first: whoever started a turn may
        stop it, which is an invariant and not a rule anyone wrote. Then the
        allow list, which is what layer 0 already gates. Then a ``clicks.stop``
        rule if the conversation has one, which narrows who *else* may and
        cannot reach the floor above it. A refusal is told to the clicker and to
        nobody else, because for everyone else nothing happened.

        **Is there still a turn to stop.** The record for the request, claimed
        under the lock, is this connector's own statement that the turn is
        running: opened by the turn's first tracked work and closed by its
        terminal event. A click that finds no live record is refused rather than
        forwarded, which is stricter than it has to be. The cancel is addressed
        to a *session*, and a session in a channel thread is shared by everyone
        posting in it, so a stop arriving after the turn it meant has ended would
        reach whatever the session is running now. Refusing costs a click that
        has to be made again; forwarding costs a turn nobody asked to stop.

        **Dispatch.** A ``chat.interrupt`` with ``intent: cancel``, which is the
        same request Web, CLI, TUI and ACP send and the same one the gateway has
        always handled -- only the connector half was missing. It is sent after
        the record is claimed and the button is on its way off the card, so a
        second click cannot spend a second cancel on the same turn.

        **Write the ending.** A stop sends no terminal event -- see
        ``_settle_stopped_card`` for why the cancel produces none -- so
        everything that ending would have done is done here. Three things, in
        the order a terminal does them: the card is settled
        (``_settle_stopped_card``), the message being streamed into is taken out
        of streaming state (``_stop_open_stream``), and the session is released
        so the conversation can move on (``_release_stopped_turn``). Each was
        found by somebody hitting it.

        Note what this does *not* change: an ordinary message into a running
        session still cancels it, with no check at all, and in a channel thread
        that is a second person's message cancelling the first person's turn.
        That path is untouched here. What the button adds is a gesture that
        means only "stop" -- Slack's ambient one means both "stop" and "here is
        more" -- and a place where a stop can be refused, which a message is not.

        Guarded like the question button and for the same reason: bolt reports a
        listener's exception on its own logger and nowhere this connector
        writes, and the acknowledgement has already gone out by then, so a raise
        leaves somebody watching a card whose stop button says the click landed
        while the turn runs on.
        """
        try:
            await self._route_stop_action(ack, body, action)
        except Exception:  # noqa: BLE001
            logger.exception(
                "[SlackChannel] a stop click raised: %s",
                self._click_identity(body, action),
            )

    async def _route_stop_action(
        self, ack: Any, body: dict[str, Any], action: dict[str, Any]
    ) -> None:
        await ack()
        if not self._running:
            return
        if not isinstance(body, dict) or not isinstance(action, dict):
            return

        decoded = self._decode_stop_value(action)
        if decoded is None:
            return
        session_id, request_id, channel_id = decoded

        user = body.get("user")
        user_id = str(
            (user.get("id") if isinstance(user, Mapping) else "") or ""
        ).strip()

        permitted, ground = self._may_stop(session_id, user_id, channel_id)
        if not permitted:
            logger.warning(
                "Slack stop refused: user=%s session_id=%s request_id=%s "
                "ground=%s",
                user_id or "-",
                session_id,
                request_id,
                ground,
            )
            await self._notify_clicker(
                body, user_id, _STOP_REFUSED_NOTICE, notice="stop-refused"
            )
            return

        record = await self._claim_stop(request_id, channel_id)
        if record is None:
            logger.info(
                "Slack stop ignored for a turn that is no longer running: "
                "user=%s session_id=%s request_id=%s",
                user_id or "-",
                session_id,
                request_id,
            )
            await self._notify_clicker(
                body, user_id, _STOP_STALE_NOTICE, notice="stop-stale"
            )
            return

        logger.info(
            "[SlackChannel] stopping a turn: session_id=%s request_id=%s "
            "user=%s ground=%s",
            session_id,
            request_id,
            user_id or "-",
            ground,
        )
        await self._dispatch_stop(
            body, session_id=session_id, channel_id=channel_id, user_id=user_id
        )
        await self._notify_clicker(
            body, user_id, _STOP_ACCEPTED_NOTICE, notice="stop-accepted"
        )
        await self._settle_stopped_card(record)
        await self._stop_open_stream(record)
        await self._release_stopped_turn(session_id, request_id)

    def _clicks_verdict(
        self, kind: str, channel_id: str, user_id: str
    ) -> "tuple[bool, str] | None":
        """What a ``clicks`` rule says about this click, or ``None`` if none does.

        ``None`` is the whole of the no-rule case: with no rule matching this
        conversation, whatever gated the click before still gates it and nothing
        here has an opinion. Every deployment that has written no ``clicks:``
        block takes that branch, so this method cannot change what any of them
        do.

        A rule that does match answers on the identity the payload held, and
        **an unidentified clicker is refused rather than waved through**.
        That is the opposite direction from an unidentified *sender*, which falls
        out of positive lists and stays inside restrictions, and the asymmetry is
        deliberate: identity goes missing by degradation -- a payload shape that
        changed, a field that was never populated, an id blanked because it was
        serving a second purpose -- and under the permissive reading each of
        those quietly converts a written restriction into no restriction while
        the config still says otherwise. Both cases arrive here as the same
        fact: an id that is in no list, either because none came or because no
        ``people:`` entry maps it into the role the rule names.

        The reason is returned rather than logged, because the two callers name
        different things -- an answer names its request, a stop names its
        session -- and one log line that named neither would be the one worth
        reading and the one nobody could act on.
        """
        rule = click_rule(
            self.config.scopes,
            channel="slack",
            chat=channel_id or None,
            kind=kind,
        )
        if rule is None:
            return None
        if rule.permits(user_id):
            return True, f"clicks.{kind}={rule.describe()}"
        if not user_id:
            return False, (
                f"clicks.{kind}={rule.describe()} and the payload named no"
                " clicker"
            )
        return False, f"clicks.{kind}={rule.describe()}"

    def _may_stop(
        self, session_id: str, user_id: str, channel_id: str = ""
    ) -> tuple[bool, str]:
        """Whether ``user_id`` may stop the turn on ``session_id``, and on what ground.

        Two clauses, and only the second is policy.

        **The starter's allowance is a floor.** Whoever asked for a turn may
        stop their own work. It is not conferred by any rule and no rule can
        take it away; a per-conversation rule, when there is one, will only ever
        narrow who *else* may. It is affordable here and would not be on an
        approval: the worst a too-generous stop buys is a turn someone has to
        start again, whereas a too-generous approval is the tool call the
        question existed to stop. Starting a turn is no claim to answer what it
        goes on to ask, so there is no equivalent on the answer path.

        **The allowance needs a recorded starter, and an identified clicker.**
        ``turn_initiator`` returning ``None`` means this connector cannot say
        who started the turn -- an entry that aged out, a session with nothing
        running, a turn nobody recorded -- and it is never read as a refusal:
        the floor simply does not arise and the allow list decides alone. The
        clicker's own id is required for the same reason from the other side. On
        a payload holding no user, ``initiator.user_id == user_id`` would be
        two empty strings comparing equal, and "whoever started it may stop it"
        would have quietly become "anyone may stop it" -- which is the one
        reading this allowance must never have.

        **What decides when the floor does not.** The allow list, which is what
        gates every other inbound gesture on this channel and is empty by
        default. It is applied to an empty clicker id rather than skipped for
        one, which is the opposite of what the sibling gate on the answer path
        does; that inconsistency is known and is not this method's to resolve,
        but a new control that destroys work does not inherit it.

        **And then a ``clicks.stop`` rule, if the conversation has one.** It is
        asked last of the three and it can only refuse, which is what keeps the
        floor above it un-removable: a rule that named nobody, or that named
        somebody other than the starter, is never reached on the starter's own
        click. It is ANDed with the allow list rather than replacing it,
        so a rule can never admit someone layer 0 keeps out.
        """
        initiator = self.turn_initiator(session_id)
        if initiator is not None and user_id and initiator.user_id == user_id:
            return True, "starter"
        if not self.is_allowed(user_id):
            return False, "allow_from"
        verdict = self._clicks_verdict(CLICK_STOP, channel_id, user_id)
        if verdict is None:
            return True, "allow_from"
        return verdict

    @staticmethod
    def _decode_stop_value(action: Mapping[str, Any]) -> tuple[str, str, str] | None:
        """``(session, request, channel)`` from a stop button, or ``None``.

        Slack echoes a button's value back exactly as it was sent, so anything
        that does not decode into all three is a button this connector did not
        post -- or posted in a version that encoded something else -- and is not
        acted on. All three are required: without the session there is nothing
        to cancel, and without the request and the channel there is no record to
        check the turn is still running against.
        """
        raw = action.get("value")
        if not isinstance(raw, str) or not raw:
            logger.warning("Slack stop button carried no value")
            return None
        try:
            decoded = json.loads(raw)
        except (TypeError, ValueError):
            logger.warning("Slack stop button value was not valid JSON")
            return None
        if not isinstance(decoded, Mapping):
            logger.warning("Slack stop button value was not an object")
            return None
        session_id = str(decoded.get("session_id") or "").strip()
        request_id = str(decoded.get("request_id") or "").strip()
        channel_id = str(decoded.get("channel_id") or "").strip()
        if not session_id or not request_id or not channel_id:
            logger.warning("Slack stop button value identified no turn")
            return None
        return session_id, request_id, channel_id

    async def _claim_stop(
        self, request_id: str, channel_id: str
    ) -> "_SlackActivityRecord | None":
        """Take the stop for this turn, or ``None`` if there is none to take.

        One claim per turn. Two people pressing the same button, or one person
        pressing it twice while the first cancel is still travelling, resolve to
        a single dispatch: the second finds the record already claimed and is
        reported as the stale click it is.

        ``None`` for a record that has closed, and for one that is not there at
        all. The second covers a card left on screen by a process that has since
        restarted, and one whose record aged out from under it -- in both cases
        this connector has nothing left saying the turn is live, and a cancel
        addressed to a shared session on that basis could stop work nobody asked
        to stop.
        """
        key = (request_id, channel_id)
        async with self._activity_lock:
            record = self._activity_records.get(key)
            if record is None or record.closed or record.stop_requested:
                return None
            record.stop_requested = True
            record.touched_at = time.monotonic()
            return record

    async def _settle_stopped_card(
        self, record: "_SlackActivityRecord"
    ) -> None:
        """Write the ending a stopped turn will never send an event for.

        Every other ending on this card arrives as one: a ``chat.final`` or a
        ``chat.error`` reaches ``send``, and ``_close_activity_card`` settles
        the record on the strength of it. **A stop produces neither.** The
        cancel reaches ``_cancel_agent_work_for_session``, which cancels the
        gateway's own stream-consumer task -- ``process_stream`` -- and that
        coroutine catches its ``CancelledError``, logs that the stream was
        cancelled and returns. Its ``finally`` emits a
        ``chat.processing_status``, which is not a terminal event and closes
        nothing; the ``chat.interrupt_result`` the gateway publishes afterwards
        renders to no text and settles nothing either. So the last thing the
        connector ever hears about a stopped turn is the click it handled
        itself, and a card that waited for a terminal waited forever, captioned
        "Working..." for as long as anyone scrolled past it. That is what was
        seen in production.

        So the ending is written here, by the one participant that knows a stop
        happened. Deliberately after the dispatch rather than before it: the
        button is already claimed, so a write that fails leaves a control that
        refuses its own second click rather than one that spends a second
        cancel.

        Settling twice is harmless in both directions. This method does nothing
        to a record that has already closed, and ``_close_activity_card``
        returns early on one -- so a terminal that does arrive later, on some
        path this has not seen, neither relabels the card nor spends an edit
        re-saying what it says. A late ``chat.error`` on such a path is reported
        as undelivered, which is the existing behaviour and the right one: it
        goes out as its own message rather than being swallowed by a card that
        is not holding it.

        What does move the card again is fresh activity, exactly as before. A
        request resumed after its stop reopens the record, the caption goes back
        to "Working...", and whatever ends it next names that ending -- which is
        why ``stopped`` is cleared by ``reopen`` and ``stop_requested`` is not.
        """
        async with self._activity_lock:
            if record.closed:
                return
            record.settle(stopped=True)
            if not record.message_ts:
                # Nothing on screen to settle. Only reachable if the card was
                # never posted, which the button being pressed argues against --
                # it is rendered on the card and nowhere else -- so this is
                # belt-and-braces rather than a case with a story.
                return
            await self._refresh_activity_card(record, force=True)

    def _stream_keys_for(
        self, record: "_SlackActivityRecord"
    ) -> list[tuple[str, str, str]]:
        """Every open stream belonging to the turn ``record`` describes.

        The two maps are keyed on the same identity written two ways. A stream
        is keyed on the outgoing message's own id, and a card on the request
        that id belongs to -- which for an uninterrupted request is the same
        string, and for a resumed one is not: ``_activity_key`` folds a resumed
        id back onto the id its request started under, and nothing folds the
        stream map. Comparing the two raw would miss exactly the stream of a
        turn that had already been interrupted once, which is the turn most
        likely to be stopped again.

        So the fold is applied to the stream's id before the two are compared,
        and the thread is deliberately not part of the test. Both sides derive
        it from ``_extract_delivery`` and should agree, but the record's copy is
        taken from the turn's first tracked event and the stream's from a delta,
        and a mismatch there would silently leave the message open rather than
        loudly close the wrong one -- while the channel and the request together
        already name one turn.

        A list because nothing guarantees a turn has at most one, and closing
        the one that happened to be found first would leave the others exactly
        as broken as before. In practice it holds nought or one.
        """
        return [
            key
            for key in self._streams
            if key[1] == record.channel_id
            and self._activity_request_aliases.get(key[0], key[0])
            == record.request_id
        ]

    async def _stop_open_stream(self, record: "_SlackActivityRecord") -> None:
        """Take this turn's streamed message out of streaming state.

        The other half of what a stop leaves unfinished, and broken for the same
        reason the card was: ``_close_stream`` is reached from ``send()`` and
        from nowhere else, so a turn whose terminal event never arrives never
        reaches it. The card at least kept saying something; the message keeps
        an open stream, and this was watched happen -- a stream opened, a stop
        ten seconds later, and no ``chat.stopStream`` at all before the process
        was restarted a minute after that.

        Gated on ``record.stopped``, which ``_settle_stopped_card`` has just set
        unless it found the record already closed. Already closed means a
        terminal did arrive -- the turn finished in the window between the click
        and the cancel -- and that terminal took ``send()`` through
        ``_close_stream`` with the whole answer, which is a better ending than
        this one and must not be undone. So this runs when the stop is what
        ended the turn, and stands aside when something else did.

        The entry is taken under the lock and finished outside it. Popping under
        the lock is what makes this exactly-once: a terminal racing in behind it
        finds nothing to close and posts its answer as a fresh message, which is
        what a dropped stream has always done, and a second click cannot reach
        here at all because the record is claimed before the dispatch. Finishing
        outside it keeps a channel-wide lock off an HTTP call that the turn this
        lock protects is no longer making.
        """
        if not record.stopped:
            return
        async with self._stream_lock:
            keys = self._stream_keys_for(record)
            streams = [(key, self._streams.pop(key)) for key in keys]
        for key, stream in streams:
            await self._finish_stopped_stream(stream, key[1])

    async def _finish_stopped_stream(
        self, stream: "_SlackStream", channel_id: str
    ) -> None:
        """Close one abandoned stream, marking what the reader was left with.

        ``session.stop()`` first, exactly as ``_close_stream`` makes it: it
        waits out a write already in flight, so the last debounced append cannot
        land after the close and leave a half-sentence below the mark, and it
        latches the session shut so nothing can write again. A write neither
        awaited nor cancelled outlives the turn by a debounce window and then
        writes into a message nobody is going to finish.

        Then the close itself, which is ``stop_stream`` and not a second path to
        the same call: the same method ``_finish_streamed_reply`` ends an
        ordinary reply with, given the stop's tail instead of the answer's.
        Nothing is entrusted to a surface that has already latched -- a reader
        who pressed Slack's own stop, or a stream that outlived its five minutes
        -- so that one is stopped bare and the mark is dropped rather than
        chased onto a message that will refuse it.

        Best effort throughout, and never raising. There is no reply owed here
        to raise about: the answer this would have held is the answer the
        cancel destroyed, and ``_recover_streamed_reply``'s ladder exists to
        deliver a finished reply the close could not, which is the one thing a
        stopped turn does not have. A close that fails costs the message its
        mark and leaves it to Slack's own expiry, which is where an unclosed
        stream ends up anyway.

        Only the streaming surface has anything to close. The edit surface's
        ``close`` is a documented no-op -- its message is an ordinary one that
        ``send()`` would have rewritten -- so it is left showing its last
        preview, unmarked. Writing to it would mean re-rendering the whole
        snapshot through the preview renderer, which clamps to the edit ceiling
        and keeps only the reply's first piece, so a mark appended to the text
        could be truncated away or land in a piece that is never posted, and the
        rewrite could shorten text the reader has already read. An unmarked
        message is a smaller wrong than a shortened one.
        """
        if stream.session is None:
            # Never opened: nothing on screen, and nothing to take back.
            return
        try:
            handle = await stream.session.stop()
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Slack streaming could not be closed after a stop: %s", exc
            )
            return
        surface = stream.surface
        if not isinstance(surface, _SlackStreamingSurface) or not handle:
            return
        chunks = (
            []
            if surface.failed
            else surface._markdown_chunks(_STREAM_STOPPED_TAIL)
        )
        stopped = await surface.stop_stream(handle, chunks=chunks)
        logger.info(
            "[SlackChannel] streaming stopped with the turn: channel=%s ts=%s "
            "appends=%d streamed_chars=%d closed=%s marked=%s",
            channel_id,
            surface.opened_ts or handle or "-",
            surface.edits,
            len(surface.sent_text),
            "yes" if stopped else "no",
            "yes" if stopped and chunks else "no",
        )

    async def _release_stopped_turn(
        self, session_id: str, request_id: str
    ) -> None:
        """Let the conversation move on from a turn that was stopped.

        The third thing ``send()``'s terminal branch does that a stop never
        sends a terminal for, and the one nobody sees until several minutes
        later. That branch retires the session's initiator entry and then
        drains whatever was held behind it. A stopped turn reaches neither, so
        the entry stands and the drain never runs.

        What the surviving entry costs is the whole conversation.
        ``turn_initiator`` is the liveness signal the inbound path decides on:
        an entry that outlived its turn answers "a turn is running" to every
        message that arrives afterwards, so under ``mid_turn: queue`` each one
        is held rather than dispatched -- including the first, arriving minutes
        after the stop into a session running nothing at all. A held message
        drains only on a terminal event, which is the event the cancel
        guarantees will not come, so the queue fills and nothing empties it.
        ``_TURN_INITIATOR_TIMEOUT_SECONDS`` retires the entry in the end, which
        makes this an hour-long outage per stopped conversation rather than a
        permanent one. That is what was seen in production: a stop, four
        messages eight minutes later, four ``outcome=queued:dm`` and not one
        dispatch.

        **The entry is matched, not dropped.** ``_forget_turn_initiator``
        refuses an entry that belongs to a different request, and that guard is
        the reason a second person's turn is not closed by the first person's
        ending. It is kept here for the same reason: by the time a cancel is
        dispatched the session may already be running something else, and this
        must retire the stopped turn's entry or nothing.

        The two ids are not always spelled the same, which is why the match is
        made here rather than by handing ``_forget_turn_initiator`` the id off
        the button. A request resumed after an interrupt runs under a fresh id
        and opens its initiator entry under that one, while its card -- and so
        the button on it -- keeps the id the request started under, because
        ``_activity_key`` folds a resumed id back onto it. Comparing the two raw
        would miss exactly the turn that had already been interrupted once,
        which is the turn most likely to be stopped. So the entry's own id is
        folded through the alias map before the two are compared, the same fold
        ``_stream_keys_for`` applies for the same reason, and the id actually
        deleted is the entry's.

        **Idempotent, and harmless if a terminal does arrive.** A stop that
        raced a real ending finds the entry already gone and does nothing; a
        terminal arriving after this one ran finds the same. Neither can retire
        an entry that a newer turn has since opened, because that entry's id --
        folded or raw -- is not this request's.

        **The queue is drained rather than dropped.** The button names one
        turn, and the messages behind it are their sender's own, typed while
        somebody's turn was running and marked with a reaction that promises
        they will run when it ends. A stop *is* that turn ending. Dropping them
        would break the only promise the queue makes, silently, and in a thread
        the person who pressed the button need not be the person whose message
        is waiting. In the incident they were simply lost.

        One message, not the queue, because that is what the drain does
        everywhere: each held message is its own turn, and the next one leaves
        when this one ends.

        Ordered after the retire and never before it. The drain's whole test is
        that the session has no turn left, and asking while the stopped turn's
        entry still stood would answer "busy" and hold the queue exactly as the
        production failure did.
        """
        entry = self._turn_initiators.get(session_id)
        if entry is not None:
            folded = self._activity_request_aliases.get(
                entry.request_id, entry.request_id
            )
            if folded == request_id:
                # Written from the entry before it is retired, which is the
                # only reason this can be marked at all: a stopped turn sends
                # no terminal event, so the coordinates the terminal branch
                # would have used are about to be dropped here instead. The
                # same is true of the thinking status: a stopped turn reaches
                # ``send()``'s terminal branch no more than it reaches the
                # mark, so this is the only point left to clear the one it
                # opened.
                await self._mark_turn_ended(entry, self.config.stopped_emoji)
                await self._clear_thinking_status(entry.channel_id, entry.thread_ts)
                self._forget_turn_initiator(session_id, entry.request_id)
                logger.info(
                    "[SlackChannel] turn initiator retired by a stop: "
                    "session_id=%s request_id=%s",
                    session_id,
                    request_id,
                )
        # Attempted whatever the entry said, and a no-op while the session is
        # busy: with a newer turn now running, the drain reads it and stands
        # aside, which is the same answer it gives on every other path.
        await self._drain_queued_messages(session_id)

    async def _end_superseded_turn(
        self,
        superseded: "_SlackTurnInitiator | None",
        channel_id: str,
        request_id: str,
    ) -> None:
        """End the turn this message is about to cancel.

        **The second trigger of the missing terminal, and by far the commoner
        one.** An ordinary ``chat.send`` into a busy session finishes the stream
        that session had before starting the new request, and that turn is
        cancelled the way a stop cancels one -- no terminal event, so everything
        ``send()``'s terminal branch would have done for it is skipped. See
        ``_settle_stopped_card`` for why the cancel produces none.

        The stop path already writes that ending, and it is reached only from
        ``_handle_stop_action``. This path reached nothing, so every cancelled
        turn on a busy channel left a card pinned at "Working..." and, if it was
        streaming, a Slack message stuck in streaming state. Watched happen
        with four mentions in quick succession: three streams opened, one
        closed, and the reader was left looking at several cards claiming that
        turns finished a minute ago were still running. The stop button is a
        deliberate gesture somebody has to find; this is what happens whenever
        anyone types twice, and it predates the button entirely.

        **Nothing here decides that a cancel is coming.** Reaching the dispatch
        tail with a turn already running *is* that decision, taken above by the
        three mid-turn branches: ``queue`` holds the message instead of sending
        it, ``steer`` folds it into the running round and asks the gateway to
        skip its cancel, and only ``cancel`` falls through to here. So the
        caller's ``running`` is the whole condition and this method adds none of
        its own. A conversation reaches this path by asking for it: ``cancel``
        stopped being what an unconfigured deployment gets when the default
        became ``queue``.

        **The initiator entry is not this method's problem**, and that is worth
        saying because it is the one piece of turn state this path never leaked.
        ``_remember_turn_initiator`` overwrites, so the session's entry already
        names the new turn by the time this runs; the stale entry a stop leaves
        behind has no counterpart here. Nor is a queue drained: under ``cancel``
        nothing is ever held.

        **The record is found the way everything else on this path finds one.**
        The card's key is the request the turn started under, and a turn resumed
        after an interrupt runs under a fresh id -- which is the id the
        initiator entry holds -- so the entry's id is folded through the alias
        map before the lookup, exactly as ``_release_stopped_turn`` and
        ``_stream_keys_for`` fold it. A record that is missing or already closed
        means there is nothing to end: no card was ever posted, the deployment
        has ``activity_card`` off, or a terminal did arrive after all.

        **Before the dispatch rather than after it**, which is the opposite of
        where the stop path settles and for a reason that does not apply there.
        A stop settles after its cancel because the button is claimed first and
        a failed write must not cost a second click. There is no claim here and
        no second gesture; what there is instead is a request already committed
        to -- the initiator entry was overwritten a few lines above, which is
        this connector saying the old turn is over -- and an ending written
        after the routing would race the new turn's first events for the same
        lock.
        """
        if superseded is None or superseded.request_id == request_id:
            return
        # Marked before the record lookup below and never behind it. The card
        # is a display feature an operator can switch off and one a short turn
        # never reaches, and the halted turn's message needs its mark either
        # way -- the same reason the terminal branch settles the initiator
        # apart from the card. The entry itself is not retired here: the
        # dispatch overwrote it a few lines above this method's caller, so it
        # already names the turn that replaced this one.
        await self._mark_turn_ended(superseded, self.config.stopped_emoji)
        # A superseded turn reaches ``send()``'s terminal branch no more than
        # a stopped one does: the message that cancelled it is what is being
        # dispatched here, not a terminal event for the turn it replaced. This
        # is the only remaining point that can close the status the cancelled
        # turn's acknowledgement opened.
        await self._clear_thinking_status(
            superseded.channel_id, superseded.thread_ts
        )
        folded = self._activity_request_aliases.get(
            superseded.request_id, superseded.request_id
        )
        record = self._activity_records.get((folded, channel_id))
        if record is None or record.closed:
            return
        logger.info(
            "[SlackChannel] ending a turn the next message cancels: "
            "request_id=%s channel=%s",
            folded,
            channel_id,
        )
        await self._settle_stopped_card(record)
        await self._stop_open_stream(record)

    async def _dispatch_stop(
        self,
        body: Mapping[str, Any],
        *,
        session_id: str,
        channel_id: str,
        user_id: str,
    ) -> None:
        """Send the cancel the button stands for.

        ``chat.interrupt`` with ``intent: cancel`` -- the request every other
        surface already sends and the gateway already handles, reaching
        ``_cancel_agent_work_for_session``, which cancels the session's
        in-flight stream tasks *and* tells AgentServer to stop the work it has
        already dispatched. That second half is what a posted message does not
        do, and it is what stops a turn that is sitting in a tool rather than
        streaming.

        Not a stream: a cancel is answered once. The gateway's own reply is a
        ``chat.interrupt_result`` event, which renders to no text and is
        therefore posted nowhere -- which is why the person who pressed the
        button is told separately.
        """
        team_id = self._team_id(body)
        req = Message(
            id=f"slack-stop-{int(time.time() * 1000)}",
            type="req",
            channel_id=self.channel_id,
            session_id=session_id,
            params={"intent": "cancel", "session_id": session_id},
            timestamp=time.time(),
            ok=True,
            provider="slack",
            chat_id=channel_id,
            user_id=user_id,
            req_method=ReqMethod.CHAT_CANCEL,
            is_stream=False,
            # Which workspace this came from, on the request rather than only
            # in the metadata: it is what the gateway indexes the connection by
            # and what a reply is routed back through. bot_id carries the same
            # string because it is what resolve_app_id falls back to, and it is
            # the only one of the two that channels which have not migrated set.
            app_id=self.app_id,
            bot_id=self.app_id,
            metadata={
                "user_id": user_id,
                "slack_team_id": team_id,
                "slack_channel_id": channel_id,
                # Slack's own word for this conversation, through the one
                # reader every decision that turns on a kind goes through. The
                # id prefix used to answer here and it cannot: ``C`` and ``G``
                # separate none of the three room kinds, so a private channel
                # and a group DM were both labelled ``channel``. See
                # ``chat_type_from_chat_id``.
                #
                # No payload is passed, because a ``block_actions`` body states
                # no ``channel_type``: the map and the ``D`` prefix are the
                # whole of what is knowable here.
                #
                # An unknown kind stays unknown, and unlike the dispatch paths
                # this one spends no last resort on it. Those stamp "channel"
                # so ``read_slack_conversation`` still has an origin type to
                # accept; a cancel carries no history metadata, starts no turn
                # and reads no history, so there is nothing here for a made-up
                # word to buy.
                "slack_channel_type": self._conversation_chat_type(channel_id),
                "slack_user_id": user_id,
            },
        )
        await self._route_request(req)

    async def _notify_clicker(
        self, body: Mapping[str, Any], user_id: str, text: str, *, notice: str
    ) -> None:
        """Tell the person who clicked what happened, and nobody else.

        Ephemeral, because the outcome concerns one person: everyone else in the
        channel reads the card, which says what it says whatever any one click
        was told. Best effort -- a failure here costs an explanation and never
        the action itself, which has already been taken or already been refused.

        ``notice`` names which of these was sent, and is what reaches the log in
        place of ``text``; see ``_post_ephemeral`` for why the prose does not.
        """
        channel_id, _message_ts = self._click_target(body)
        await self._post_ephemeral(channel_id, user_id, text, notice=notice)

    async def _post_ephemeral(
        self, channel_id: str, user_id: str, text: str, *, notice: str
    ) -> None:
        """Say something to one person in a conversation, and to nobody else.

        The ephemeral post the click paths and the inbound refusal share, so
        that "tell the person and only the person" has one implementation rather
        than one per caller. Not the only one in the connector:
        ``_report_incomplete_submit`` posts its own, because it composes its
        text out of a problem string and warns about a failure in its own words.

        Both outcomes are recorded, because the sender's copy proves nothing
        after the fact: a Slack ephemeral reaches only the clients connected
        when it is posted, is gone on reload, and is never backfilled. An
        explanation that left no trace here and none on screen cannot be told
        apart from one that was never attempted -- and the paths that lead here
        return early often enough (no client, no channel, feedback off) that a
        missing warning is not evidence of a delivery. So a success says who was
        told and which notice they got. ``notice`` names it; ``text`` never
        reaches the log, being user-facing prose that in places quotes a
        person's own words back at them.

        The success sits at ``INFO`` for the same reason the refusals it
        accounts for do: it is what an operator reading an ordinary log needs in
        order to answer "was anybody told?", and a level nobody enables in
        production would answer that only for deployments which already
        suspected the problem. The volume is one line per refusal or per click
        -- the same order as the lines the click paths already write, and
        bounded by the queue cap rather than by traffic.

        Best effort throughout. Every caller has already taken -- or already
        refused -- the action this explains, so a failure here costs the
        explanation and never the outcome. It is warned about rather than
        swallowed, because a workspace where these never arrive is one where
        every refusal is silent.
        """
        if self._client is None or not user_id or not channel_id:
            return
        try:
            await self._client.chat_postEphemeral(
                channel=channel_id,
                user=user_id,
                text=_clamp(text, _MAX_SLACK_TEXT_LENGTH),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Slack could not tell %s what became of their message: %s",
                user_id or "-",
                exc,
            )
        else:
            logger.info(
                "Slack told %s what became of their message: %s",
                user_id,
                notice,
            )

    async def _report_incomplete_submit(
        self, body: Mapping[str, Any], *, user_id: str, problem: str
    ) -> None:
        """Tell the person who pressed submit why it did not answer anything.

        Ephemeral: everyone else in the channel sees the question exactly as it
        was, because for them nothing happened. Best effort, like every other
        courtesy this connector posts -- the question is still pending and its
        fields are still on screen either way, so a failure here costs an
        explanation rather than the ability to answer.
        """
        if self._client is None or not user_id:
            return
        channel_id, _message_ts = self._click_target(body)
        if not channel_id:
            return
        try:
            await self._client.chat_postEphemeral(
                channel=channel_id,
                user=user_id,
                text=_clamp(
                    f"{_INCOMPLETE_SUBMIT_NOTICE} {problem}", _MAX_SLACK_TEXT_LENGTH
                ),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Slack could not tell %s why their submit was refused: %s",
                user_id or "-",
                exc,
            )

    @staticmethod
    def _decode_button_value(action: Mapping[str, Any]) -> dict[str, Any] | None:
        """Return the object a button's ``value`` was encoded with, or ``None``.

        Slack echoes the value back exactly as it was sent, so anything that
        does not decode is a button this connector did not post -- or one it
        posted in a version that encoded something else -- and is not acted on.
        """
        raw = action.get("value")
        if not isinstance(raw, str) or not raw:
            logger.warning("Slack answer button carried no value")
            return None
        try:
            decoded = json.loads(raw)
        except (TypeError, ValueError):
            logger.warning("Slack answer button value was not valid JSON")
            return None
        if not isinstance(decoded, dict) or not str(decoded.get("request_id") or ""):
            logger.warning("Slack answer button value identified no question")
            return None
        return decoded

    @staticmethod
    def _resolve_answer(
        pending: _SlackPendingQuestion,
        decoded: Mapping[str, Any],
        action: Mapping[str, Any],
    ) -> tuple[str, str]:
        """Return the ``(label, answer value)`` the pressed button stands for.

        The record is preferred over the button: it holds the value in full,
        while the button's copy of it is dropped when the whole encoding would
        not fit. The button is the fallback, and the label Slack echoes back is
        the last resort -- it is what the person who clicked actually read.
        """
        index = decoded.get("index")
        if isinstance(index, int) and 0 <= index < len(pending.values):
            return pending.labels[index], pending.values[index]
        text = action.get("text")
        label = str((text or {}).get("text") or "").strip() if isinstance(text, Mapping) else ""
        value = str(decoded.get("value") or "").strip() or label
        return label or value, value

    @staticmethod
    def _answered_note(label: str, user_id: str) -> str:
        who = f" by <@{user_id}>" if user_id else ""
        return _clamp(f"Answered{who}: {label}", _MAX_BUTTON_TEXT_LENGTH)

    @staticmethod
    def _team_id(
        body: Mapping[str, Any], event: Mapping[str, Any] | None = None
    ) -> str:
        """The workspace a payload came from, wherever Slack put it.

        Three fields, because three payload shapes reach this connector and no
        one field is in all of them: a ``block_actions`` payload holds a
        ``team`` object, an Events API envelope holds a top-level ``team_id``,
        and a message event holds its own ``team`` when the envelope's is a
        shared channel's other side.

        The order is only a preference. Slack puts one of the first two in a
        payload, never both, so nothing observed here has ever had to choose
        between them; the event field is consulted last because it describes the
        message rather than the delivery.

        ``event`` is optional so that the click paths, which have no event, ask
        the same question the message path asks.
        """
        team = body.get("team")
        return str(
            (team.get("id") if isinstance(team, Mapping) else "")
            or body.get("team_id")
            or (event.get("team") if isinstance(event, Mapping) else "")
            or ""
        ).strip()

    @staticmethod
    def _click_channel_id(body: Mapping[str, Any]) -> str:
        """The conversation a ``block_actions`` payload was clicked in.

        Slack states it twice and not always in the same place: ``channel`` is
        absent from some payload shapes, ``container`` from none. Both are
        consulted so the answer does not depend on which shape arrived.

        Separate from ``_click_target`` because the answer path needs the
        channel without the message: it takes its own ``thread_ts`` and
        ``message_ts`` out of the same payload, for the request it is building
        rather than for a rewrite.
        """
        channel = body.get("channel")
        container = body.get("container")
        container = container if isinstance(container, Mapping) else {}
        return str(
            (channel.get("id") if isinstance(channel, Mapping) else "")
            or container.get("channel_id")
            or ""
        ).strip()

    @staticmethod
    def _click_target(body: Mapping[str, Any]) -> tuple[str, str]:
        """Return ``(channel_id, message_ts)`` for the message that was clicked.

        ``message`` is absent from some payload shapes and ``container`` from
        none, so the timestamp is read from both for the same reason the channel
        is.
        """
        message = body.get("message")
        message = message if isinstance(message, Mapping) else {}
        container = body.get("container")
        container = container if isinstance(container, Mapping) else {}
        message_ts = str(message.get("ts") or container.get("message_ts") or "").strip()
        return SlackChannel._click_channel_id(body), message_ts

    async def _close_question_message(
        self, body: Mapping[str, Any], *, note: str
    ) -> None:
        """Rewrite the question without its buttons, leaving the question itself.

        Best effort. The answer has been claimed by the time this runs, so a
        failed edit costs the channel a stale set of buttons that resolve to
        nothing rather than an answer the waiting turn never receives -- which
        is why it logs instead of raising, unlike the post that put them there.
        """
        if self._client is None:
            return
        message = body.get("message")
        message = message if isinstance(message, Mapping) else {}
        channel_id, message_ts = self._click_target(body)
        if not channel_id or not message_ts:
            logger.warning("Slack answer arrived without a message to update")
            return

        raw_blocks = message.get("blocks")
        blocks = [
            block
            for block in (raw_blocks if isinstance(raw_blocks, list) else [])
            if isinstance(block, dict)
            and block.get("type") not in _QUESTION_INTERACTIVE_BLOCK_TYPES
        ]
        blocks.append(
            {"type": "context", "elements": [{"type": "mrkdwn", "text": note}]}
        )
        text = str(message.get("text") or "").strip()
        sent, _, error = await self._post_text(
            channel_id=channel_id,
            text=_clamp(f"{text}\n\n{note}".strip(), _MAX_SLACK_TEXT_LENGTH),
            thread_ts="",
            update_ts=message_ts,
            blocks=blocks,
            # Chrome rather than interactive: the interactive blocks were
            # filtered out just above, so what is left is the question's own
            # text with a note under it and there is nothing to click by
            # design. Calling it interactive would tell a reader their controls
            # had failed at the moment they successfully used them.
            block_kind=slack_blocks.BLOCK_KIND_CHROME,
        )
        if not sent:
            logger.warning(
                "Slack could not withdraw the buttons of an answered question: %s",
                error,
            )

    async def _dispatch_answer(
        self,
        pending: _SlackPendingQuestion,
        *,
        values: list[str],
        body: Mapping[str, Any],
        user_id: str,
    ) -> None:
        """Send the answer back to the session whose turn is waiting on it.

        Which request method holds it is decided by the question's ``source``,
        and the two are not interchangeable. An interrupt -- a permission
        prompt, a confirmation, an ask_user question, an evolution approval --
        paused a running turn, and that turn resumes only through chat.send: the
        gateway recognises a send holding a request id, an answers list and one
        of those sources as a resume, and lets it through without cancelling the
        stream it is resuming. chat.user_answer reaches the adapter's answer
        handler instead, which resolves the standalone approvals and leaves an
        interrupted turn exactly as paused as it found it.

        A question only reaches this connector on a streamed turn, because that
        is the only shape in which it is emitted at all, so the resume is
        streamed too.
        """
        # One list, whatever produced it. A pressed button contributes the one
        # option it stands for; a submit contributes what its inputs held,
        # already rendered as strings. Deliberately the same field either way:
        # the receiving side reads a single selected option as a value and
        # several as a list, and it should not have to learn a second convention
        # to hear about a date.
        answers = [
            {
                "question": pending.question,
                "selected_options": list(values),
                "custom_input": "",
            }
        ]
        params: dict[str, Any] = {
            "request_id": pending.request_id,
            "answers": answers,
            "source": pending.source,
        }
        resumes_a_paused_turn = pending.source in _INTERRUPT_RESUME_SOURCES
        if resumes_a_paused_turn:
            # No new prompt: the answer is the whole of the input, and a query
            # beside it would be read as a second thing to do.
            params["query"] = ""
            params["supports_user_interaction"] = True

        container = body.get("container")
        container = container if isinstance(container, Mapping) else {}
        message = body.get("message")
        message = message if isinstance(message, Mapping) else {}
        channel_id = self._click_channel_id(body)
        thread_ts = str(
            message.get("thread_ts") or container.get("thread_ts") or ""
        ).strip()
        team_id = self._team_id(body)
        action_ts = str(container.get("message_ts") or time.time()).strip()

        if resumes_a_paused_turn:
            # Re-asserted on the resume, and it has to be. The adapter applies
            # the request's model by mutating the live ReAct agent, and a
            # request with no model_name resolves to the configured default
            # rather than to whatever the previous request set -- so a resume
            # that stayed silent about the model would not merely decline to
            # override, it would switch the turn back to the default model
            # halfway through, at the first permission prompt the turn hits.
            # The override would then hold for exactly as long as a turn went
            # uninterrupted, which is the least predictable behaviour available.
            #
            # Keyed on the channel the buttons were clicked in, which is the
            # channel the paused turn was started from: a question is posted
            # where its turn lives.
            #
            # Not done on the chat.user_answer branch. That method reaches the
            # adapter's answer handler, which resolves standalone approvals and
            # never selects a model at all, so a model_name there would be
            # written to the session's metadata and applied to nothing.
            # The initiator, never the clicker. A click is not a turn: it
            # arrives against one that already exists, from someone who may not
            # have started it and routinely did not -- a permission prompt is
            # answered by whoever is watching. Resolving the model against the
            # person who pressed the button would switch the model of somebody
            # else's turn halfway through, which is the failure the paragraph
            # above spends its length preventing in the other direction.
            #
            # Falls back to no sender rather than to the clicker when no
            # initiator was on record: the unidentified answer is the layer
            # below, and the layer below is the right place to land.
            model_name = self._channel_model_name(
                channel_id, pending.initiator_user_id, pending.initiator_chat_type
            )
            if model_name:
                params["model_name"] = model_name
            params.update(slack_agent_request_params(
                self.config, channel_id,
                user_id=pending.initiator_user_id,
                chat_type=pending.initiator_chat_type or self._conversation_chat_type(channel_id),
                workspace=self._workspace_team_id,
            ))

        # The resumed turn streams its answer like any other, and the stream
        # needs a recipient. Noted against the session that asked rather than
        # the click, which is the session the reply will come back on.
        self._remember_stream_recipient(pending.session_id, user_id, team_id)

        # What kind of conversation this answer belongs to, settled once and
        # read twice below: once for the request's bag, once for the initiator
        # record a second interrupt would resume from. Two readings would be
        # two chances to disagree about one conversation.
        #
        # **Never off the id.** ``SLACK_CHAT_TYPES`` holds four words and the
        # prefix separates exactly one of them. Slack issued ``G`` for private
        # channels and for group DMs alike and now issues ``C`` for public and
        # private channels alike, so reading anything but ``D`` labelled a
        # private channel, a group DM and a public channel all as ``channel``.
        # That is the failure worth avoiding: a wrong kind does not fail to
        # match, it matches the rule the operator wrote about somewhere else.
        # See ``chat_type_from_chat_id``.
        #
        # **The record first.** ``initiator_chat_type`` is the kind the
        # interrupted turn was dispatched with, settled against the message
        # that started it. A resume is owed the answer that turn already gave
        # rather than a fresh one -- the rule the three keys at the foot of
        # this method follow, for the reason the model is re-asserted above: a
        # turn must not change kind halfway through because somebody pressed a
        # button.
        #
        # **The map second**, where the record holds none -- a question clicked
        # after a restart. It is Slack's own word for this conversation, kept
        # from a payload that stated one, with the ``D`` prefix behind it.
        # Neither is derived. A conversation nothing has stated a kind for
        # leaves this empty, which is a real state, and the two readers below
        # each answer it in their own way.
        conversation_chat_type = pending.initiator_chat_type or (
            self._conversation_chat_type(channel_id)
        )

        metadata: dict[str, Any] = {
            "user_id": user_id,
            "slack_team_id": team_id,
            "slack_channel_id": channel_id,
            # The settled kind, and a last resort of "channel" held for the
            # reason the message and event dispatch paths hold it rather than
            # the one the prefix gave: this bag carries the history metadata
            # below, ``read_slack_conversation`` refuses an origin type it does
            # not recognise, and an empty word would take the history tool away
            # from every resumed turn in a conversation nothing has stated a
            # kind for. It cannot licence a silence on its own --
            # ``delivery.reply`` needs this *and* the key written further down,
            # which is copied off the record and never settled here.
            "slack_channel_type": conversation_chat_type or "channel",
            "slack_user_id": user_id,
            "slack_message_ts": str(container.get("message_ts") or ""),
            "slack_thread_ts": thread_ts,
            # Same key and same reasoning as the message-dispatch path:
            # the raw, unprefixed identifier half of this interactive
            # message's ts, for a tool to address it by.
            "message_ts": str(container.get("message_ts") or ""),
            # Settled for the person who clicked, not for whoever started
            # the turn being answered. The click is its own request with its
            # own principal, and a history read this request makes is
            # answered into this conversation in front of this clicker.
            **slack_history_request_metadata(
                self.config,
                channel_id,
                user_id=user_id,
                workspace=self._workspace_team_id,
            ),
            **slack_write_request_metadata(self.config),
        }
        if resumes_a_paused_turn:
            # **Everything above this line is built from the click. These three
            # cannot be.** A resume is a new request with a new bag, and a
            # click says where it was pressed and by whom and nothing at all
            # about the message that started the work being resumed. Rebuilt
            # from the click alone, the bag lost the whole of what the
            # interrupted turn had been told about its own reply.
            #
            # What the room saw: a turn woken by an event under
            # ``delivery.reply: optional`` was asked, in its own prompt, to
            # answer ``NO_REPLY`` when nothing needed saying. It hit a
            # permission prompt, somebody approved, and the resumed turn
            # answered exactly as instructed -- into a bag that no longer said
            # the contract had been offered. The outbound half consults the
            # matcher only where the request says so, so it never looked, and
            # the bare token was posted as ordinary text.
            #
            # **Taken from the record, never settled again.** The obvious
            # repair is to ask the config what this conversation's
            # ``delivery.reply`` says, and it is the wrong one twice over. It
            # needs the kind of room the original message arrived in, which the
            # click does not carry and which a scope may be addressed on. And
            # the config it would read is the config as it stands now: the
            # WebUI rewrites it at runtime, a paused turn can wait for as long
            # as the person who has to approve it takes, and the answer that
            # governs this reply is the one the turn was dispatched under. The
            # interrupted turn answered this question once, with its own
            # message in front of it, and that answer is the one kept.
            #
            # **The matcher fact, not the offer.** These carry whether a token
            # this turn writes is honoured, which is what the outbound half
            # asks. Whether the initiator's prompt carried the fragment is a
            # separate decision, withheld on a message that addressed the bot,
            # and it is not what the resume needs: the interrupted turn read
            # the fragment before it paused.
            #
            # Each written only when it held, which is how the readers already
            # tell a turn that holds the fact from one that does not. A resume
            # of a turn that never held the contract therefore arrives with
            # exactly what it arrived with before.
            if pending.initiator_silence_is_honoured:
                metadata[SLACK_REPLY_OPTIONAL_KEY] = True
            # Diagnostic alone, and stamped for the same reason the trigger
            # below is: a resumed turn that withholds its reply is one of the
            # cases the log is read for, and an unstamped bag would record
            # every one of them as unaddressed.
            if pending.initiator_addressed:
                metadata[SLACK_ADDRESSED_KEY] = True
            # Routing, not silence, and the louder of the two failures. An
            # unanchored event turn is addressed to the room; the resume would
            # otherwise route by the clicked card's container, so a question
            # that happened to be posted inside a thread would put a turn about
            # the room into that thread. The one incident on record landed at
            # root only because the card did.
            if pending.initiator_post_as_root:
                metadata[POST_AS_ROOT_KEY] = True
            # Diagnostic, and the reason it is stamped at all: no dispatch path
            # writes an empty trigger, so a resumed turn arriving without the
            # key reads as a turn nothing woke rather than as the continuation
            # of one that something did.
            if pending.initiator_trigger:
                metadata["slack_trigger"] = pending.initiator_trigger

        req = Message(
            id=f"slack-answer-{pending.request_id}-{action_ts}",
            type="req",
            channel_id=self.channel_id,
            # The session that asked, not one derived from where the click
            # happened: the answer is only meaningful to the turn that is
            # waiting for it.
            session_id=pending.session_id,
            params=params,
            timestamp=time.time(),
            ok=True,
            provider="slack",
            chat_id=channel_id,
            user_id=user_id,
            req_method=(
                ReqMethod.CHAT_SEND if resumes_a_paused_turn else ReqMethod.CHAT_ANSWER
            ),
            is_stream=resumes_a_paused_turn,
            app_id=self.app_id,
            bot_id=self.app_id,
            metadata=metadata,
        )

        if resumes_a_paused_turn:
            # The resumed turn carries on the request that was interrupted, but
            # arrives under an id of its own, and every event it produces will
            # be keyed by that id. Recorded here because this is the only place
            # both ids are known -- the runtime has no notion that the two
            # requests are one -- so that the status card the interrupted turn
            # already put up is the one the resumed turn goes on writing to,
            # rather than a second card counting from zero beside it.
            self._remember_activity_alias(req.id, pending.turn_request_id)
            # The resumed turn is in flight under a new id, so the session gets
            # a live entry again -- but attributed to whoever started the work,
            # not to whoever cleared the prompt in front of it. Answering a
            # permission question is not asking for the turn. The clicker is the
            # fallback and not the rule: with nothing remembered they are the
            # only person this connector can name, and naming them is better
            # than leaving a running turn with no initiator at all.
            #
            # The surface is passed the same way and falls back the same way:
            # with nothing remembered, the conversation the click arrived in is
            # the only evidence there is, and its id states outright whether it
            # is the one-to-one one. That is the narrow question the prefix can
            # answer -- "is this a DM", not "which of the four kinds is this"
            # -- and it is why this boolean may fall back to the id where the
            # ``chat_type`` below may not. Defaulting to False instead would
            # mislabel every resumed DM turn, which is the confusion the field
            # exists to end.
            self._remember_turn_initiator(
                pending.session_id,
                pending.initiator_user_id or user_id,
                req.id,
                is_dm=(
                    pending.initiator_is_dm
                    if pending.initiator_is_dm is not None
                    else channel_id.startswith("D")
                ),
                # The kind settled at the top of this method, raw. Not the
                # bag's ``or "channel"``: that word is a floor under one tool's
                # origin check rather than something Slack said, and writing it
                # here would hand a second interrupt a "channel" to resume a
                # private channel on. Nothing known still means no kind, and a
                # rule naming one does not reach this turn -- which is the
                # outcome a guess replaces not with none but with the wrong one.
                chat_type=conversation_chat_type,
                # No fallback to the click's own message, unlike the two fields
                # above. The question is not what started the work and marking
                # it would put the turn's ending on the wrong message; with
                # nothing recorded the resumed turn simply goes unmarked.
                channel_id=pending.initiator_channel_id,
                message_ts=pending.initiator_message_ts,
                thread_ts=pending.initiator_thread_ts,
                # The resumed request's bag, which by this line states
                # whatever the interrupted turn was dispatched with. That is what keeps a turn
                # interrupted twice intact: the second question reads this
                # entry the way the first one read the dispatch's.
                request_metadata=req.metadata,
            )

        logger.info(
            "[SlackChannel] answered question: request_id=%s source=%s method=%s "
            "session_id=%s user=%s",
            pending.request_id,
            pending.source or "-",
            req.req_method.value,
            pending.session_id,
            user_id,
        )

        await self._route_request(req)

    async def _handle_slack_event(
        self,
        event: dict[str, Any],
        body: dict[str, Any],
        *,
        is_dm: bool,
        trigger: str,
    ) -> str:
        """Dispatch one Slack message, or name the reason it goes no further.

        The return value is the outcome its caller logs. Every exit below is a
        message that produces no turn and no reply, and every one of them used
        to be a bare ``return`` -- which is how a dropped message came to look
        identical whichever of them it took, and identical to never arriving at
        all. Returning the reason rather than logging it here keeps it to one
        line per event, written in one place, whichever path the event took.
        """
        if not self._running:
            return "dropped:channel-not-running"
        if not isinstance(event, dict) or not isinstance(body, dict):
            return "dropped:malformed-payload"
        # Loop protection and the non-content subtypes, judged by the same
        # predicate the message router applies before it looks for a trigger.
        # Kept here rather than trusted from the caller: this is the last gate
        # before dispatch, and _route_app_mention arrives at it without passing
        # through that one. The check is pure and cheap, so the message path
        # paying for it twice buys a dispatch path that cannot be reached
        # unguarded by a future third caller.
        outcome = self._non_user_content_outcome(event)
        if outcome is not None:
            return outcome
        user_id = str(event.get("user") or "").strip()
        channel_id = str(event.get("channel") or "").strip()
        # Read once here and passed down, beside the sender and for the same
        # reason: both are settled at this point and neither is recoverable from
        # anything the dispatch later holds.
        #
        # Resolved from the conversation, because this method serves two
        # routers and only one of them brings a payload that states a kind. A
        # ``message`` carries ``channel_type``, where this is byte-for-byte the
        # read it replaces; an ``app_mention`` carries none at all.
        chat_type = self._conversation_chat_type(channel_id, event)

        message_ts = str(event.get("ts") or "").strip()
        if not user_id or not channel_id:
            return "dropped:no-user-or-channel-on-event"

        team_id = self._team_id(body, event)
        event_id = str(
            body.get("event_id") or event.get("client_msg_id") or event.get("ts") or ""
        ).strip()
        message_identity = str(
            event.get("ts") or event.get("client_msg_id") or event_id
        ).strip()
        dedupe_key = ":".join(
            part for part in (team_id, channel_id, message_identity) if part
        )
        # Deduplicate before acknowledging, so Slack's event retries cannot put a
        # second reaction on a message that is already being handled.
        if dedupe_key and not await self._remember_event(dedupe_key):
            return "ignored:already-handled (dedupe)"

        # An excluded channel is excluded outright: stay silent rather than
        # reacting inside a conversation the operator opted out of.
        #
        # A conversation with delivery or model/history scope settings is
        # opted in. Availability-only scopes do not enable the room.
        #
        # The exemption is keyed on the conversation, never on which trigger
        # fired. Keying it on a trigger name would exempt every channel that
        # matched that trigger, which is the whole workspace rather than the
        # conversations the operator opted in.
        if (
            not is_dm
            and not slack_scope_enables_channel(self.config, channel_id)
            and self.config.allowed_channel_ids
        ):
            if channel_id not in self.config.allowed_channel_ids:
                return "ignored:channel-not-in-allowed_channel_ids"
        if not self.is_allowed(user_id):
            await self._reject_request(channel_id, message_ts)
            return "refused:user-not-in-allow_from"

        text = str(event.get("text") or "").strip()
        # Read from the raw event text, before the strip below removes the very
        # thing being looked for. Reading the local ``text`` after that point
        # would answer "not addressed" for every message that was.
        #
        # A message opening with a mention of the bot is addressed to it, so an
        # answer is owed however the conversation is configured. A mention
        # further in is a reference to the bot rather than an address to it, and
        # that message stays optional. ``_has_leading_bot_mention`` is anchored
        # and already draws exactly that line.
        #
        # The text is what is tested, never the trigger this dispatch was given.
        # Both routes accept a mention, so which label a dispatch gets depends on
        # which event Slack delivered first; where the mention sits in the
        # message does not race.
        addressed = self._has_leading_bot_mention(text)
        if not is_dm and trigger == "mention":
            text = self._strip_leading_bot_mention(text).strip()
        # Set only on a message posted inside a thread, since a root-level
        # message has no ``thread_ts`` at all. ``root_thread_ts`` below falls
        # back to this message's own timestamp and is therefore set on every
        # event, so it cannot make that distinction and is not asked to.
        thread_ts = str(event.get("thread_ts") or "").strip()
        root_thread_ts = thread_ts or message_ts
        reply_thread_ts = ""
        if is_dm:
            reply_thread_ts = thread_ts
        elif thread_ts or self.config.reply_in_thread:
            # A message posted inside a thread is answered in that thread
            # whatever ``reply_in_thread`` says. The option governs whether the
            # bot opens a thread on a root-level message. Whether the bot ever
            # uses one is a separate question the option never answered.
            # Answering a threaded question at channel root detaches the answer
            # from what it answers for everyone reading the channel. The
            # detachment is invisible from inside the turn: the session below
            # is the same one whichever branch ran, so the conversation's
            # context is kept either way.
            reply_thread_ts = root_thread_ts

        # Whether everything keyed on this session now spans the whole room:
        # the queue, the turn already running, the questions still on screen.
        # Never true in a DM, which is keyed on the person whatever the setting
        # says -- see ``_session_id``.
        session_is_channel_wide = (
            not is_dm
            and self._session_scope(channel_id, user_id, chat_type) == SESSION_CHANNEL
        )
        session_id = self._session_id(
            team_id=team_id,
            channel_id=channel_id,
            user_id=user_id,
            root_thread_ts=root_thread_ts,
            is_dm=is_dm,
            channel_wide=session_is_channel_wide,
        )
        if not session_id:
            # No workspace on the event, so there is no session this message
            # can belong to that is not shared with whatever other install
            # sends an event in the same state. Dropped rather than answered
            # out of a bucket that is not its own -- see ``_session_id``.
            #
            # Before the acknowledgement, which is the next thing this path
            # does, so nothing is reacted to and nothing is posted for a
            # message that will not be answered. The event has been
            # deduplicated by this point, so a retry of it takes the same exit.
            logger.warning(
                "Slack event carried no team_id; dropping it rather than"
                " sharing a session with another workspace: channel=%s ts=%s",
                channel_id, message_ts,
            )
            return "dropped:no-team-on-event"

        # Read off the event payload, with no I/O of its own, purely so the
        # acknowledgement below can tell an attachment-only message apart from an
        # empty one before anything is fetched.
        files = self._collect_event_files(event)
        # Slack's other attachment array, read beside ``files`` because a GIF, a
        # shared message and some unfurls arrive with an empty ``text`` and no
        # ``files`` at all. A message holding only one of these used to read as
        # empty, which cost it its turn, its acknowledgement and any record that
        # something had been posted.
        attachments = self._collect_event_attachments(event)

        # Acknowledged before the attachments are fetched. A download takes as
        # long as the upload is large, and an acknowledgement sequenced after it
        # leaves whoever posted the file with no sign the message was seen for
        # the entire transfer. Nothing here depends on the download: the
        # acknowledgement needs only the channel, the thread and the message
        # timestamp, all of which are known by this point. Deduplication still
        # runs further up, so an event Slack retries is dropped before reaching
        # this line and cannot produce a second acknowledgement.
        if text or files or attachments:
            await self._acknowledge_request(channel_id, reply_thread_ts, message_ts)

        # Downloaded before the empty-text return below, because an attachment
        # posted with no comment is a request in itself: the description added to
        # the text is what keeps it from being dropped as an empty message.
        downloaded: list[dict[str, Any]] = []
        if files:
            downloaded, failures = await self._download_attachments(
                files, session_id
            )
            if failures:
                await self._report_download_failures(
                    channel_id, reply_thread_ts, failures
                )
            if downloaded:
                text = self._describe_attachments(text, downloaded)

        # Written into the text above the emptiness check, for the reason the
        # file descriptions are: what an attachment states about itself is the
        # sender's own content, and describing it is what keeps a message made
        # entirely of one from being dropped as empty.
        if attachments:
            text = self._describe_event_attachments(text, attachments)

        if not text:
            return "dropped:no-text-and-no-readable-attachment"

        # Appended below the emptiness check above. Everything appended here
        # is written by this connector, so a message holding nothing the
        # sender wrote and no file worth reading would otherwise be made
        # non-empty by the connector's own additions and dispatched as a
        # request consisting of them alone.
        prompt = self._channel_prompt(channel_id, user_id, chat_type)
        # Two decisions, and they are settled apart because they are not the
        # same question. One variable answered both, and that made the second
        # answer wrong.
        #
        # ``silence_is_honoured`` is the room's ``delivery.reply`` and nothing
        # else. It is recorded for the outbound half, which reads it to decide
        # whether a reply consisting of the token alone is a decision to say
        # nothing or a word to post.
        #
        # ``silence_is_offered`` is whether this message carries the fragment
        # that tells the turn the token exists, and it keeps the address test.
        # Somebody who opens with a mention of the bot is owed an answer, and
        # the connector will not be the thing that suggests otherwise in the
        # prompt of the message they wrote.
        #
        # **The address test belongs to the offer alone.** Withholding the
        # fragment is about what a person is invited to expect. Withholding the
        # matcher decides what happens to a judgement the model has already
        # reached -- and a model reads the fragment in every other message of a
        # conversation licensed for silence, so it knows the token with or
        # without this one. One wrote it on an addressed turn, after a person
        # said the answer had been enough and nothing more was needed. The
        # judgement was right and the matcher was disarmed, so the bare word
        # ``NO_REPLY`` was posted into the channel. The cost of honouring it
        # instead is that an addressed message can now get nothing back, and
        # only where the room licensed silence and the turn chose it; a person
        # who wanted an answer can ask again, which is the cheaper of the two.
        silence_is_honoured = self._channel_reply_is_optional(channel_id, chat_type)
        silence_is_offered = silence_is_honoured and not addressed
        # Two brackets share a line. Each states a fact of the envelope that the
        # sender never wrote, and a bracket delimits itself, so neither can run
        # on into the other. The blank line below keeps the prompt beneath them
        # from running on into prose.
        envelope = location_label(message_ts, thread_ts)
        label = trigger_label(trigger, has_prompt=bool(prompt))
        if label:
            envelope = f"{envelope} {label}".strip()
        # The membership test avoids appending an instruction the user happened
        # to paste in themselves. It does not gate the label: the label states
        # which predicate fired, which no user paste can make true or false, and
        # a prompt already present in the text is still a prompt in play.
        #
        # The sentinel instruction is last, and it is not a second standing
        # prompt.
        #
        # Exactly one standing prompt is appended: a conversation's replaces the
        # platform's outright, because two operator-authored instructions
        # contradict each other with no precedence rule anyone could predict.
        # That rule is about two texts of equal standing, neither privileged and
        # neither visible to the other's author. This is not such a text. It is
        # connector-authored, fixed, and readable in this file, the same kind of
        # thing the envelope marker and the trigger label above it are -- so it
        # composes by sitting beside whichever prompt won rather than replacing
        # it, and the operator can see exactly what it will sit beside.
        #
        # There is no contradiction for a precedence rule to settle, because the
        # two texts answer different questions. The fragment says how to express
        # a decision to stay silent; the operator's prompt says when to take one,
        # and says everything else besides. A prompt demanding an answer to every
        # message leaves the model writing answers, so the token never appears
        # and the matcher never fires -- which is that prompt getting exactly
        # what it asked for.
        #
        # Last, on the ordering this function already follows: the appended text
        # is the more recent, and recency helps compliance. Separated by a blank
        # line like everything else here, for the same reason -- a single newline
        # reads as one paragraph to a markdown renderer and to a model, which
        # would run the fragment into the prompt above it.
        #
        # It gets no membership test, unlike the prompt: a user who pasted
        # something resembling it has not made the contract true, and the real
        # text has to be present for the matcher downstream to be honest about
        # what the model was asked for.
        # Between the envelope marker and the standing prompt, and the position
        # is argued rather than convenient. The marker and this block are both
        # statements of fact about the room that the sender did not write, so
        # they sit together; the prompt and the sentinel instruction are
        # instructions, and an instruction reads better with the facts it
        # applies to already above it. A prompt written about reactions --
        # "when somebody reacts with :bookmark:, save the link" -- therefore
        # arrives after the reactions rather than before them.
        #
        # Folded here rather than earlier, because this is past every exit that
        # produces no turn: an event handed to a message that is then dropped
        # for having no text would be consumed and never delivered.
        buffered_events = self._fold_inbound_events(channel_id)
        appended = [
            part
            for part in (
                envelope,
                buffered_events,
                prompt if prompt and prompt not in text else "",
                NO_REPLY_INSTRUCTION if silence_is_offered else "",
            )
            if part
        ]
        if appended:
            # Blank lines throughout, including between the brackets and the
            # prompt under them. A single newline lets both a markdown renderer
            # and a model read the two as one paragraph, which is exactly the
            # run-on the bracket exists to prevent.
            text = "\n\n".join([text, *appended])

        metadata = {
            "user_id": user_id,
            "slack_event_id": event_id,
            "slack_team_id": team_id,
            "slack_channel_id": channel_id,
            # The resolved kind rather than the raw field, so a mention -- which
            # carries no ``channel_type`` -- is stamped with the conversation's
            # actual kind wherever one is known, instead of always as "channel".
            #
            # The last resort stays "channel" rather than becoming an absent
            # key, and that is a departure from the rule this connector follows
            # elsewhere. ``read_slack_conversation`` refuses any origin type it
            # does not recognise, so an empty value here would take the history
            # tool away from every turn in a conversation nothing has stated a
            # kind for. It cannot license a silence on its own: ``delivery.reply``
            # needs this *and* the key the inbound half stamps only when the
            # resolved kind matched, and the resolved kind is what is stamped
            # whenever anything is known.
            "slack_channel_type": "im" if is_dm else (chat_type or "channel"),
            "slack_user_id": user_id,
            "slack_message_ts": message_ts,
            "slack_thread_ts": reply_thread_ts,
            # Kept raw, unprefixed and unconverted -- the identifier half of
            # Slack's ts, for a tool to address this message by. timestamp_ms
            # takes only the time half of the same value, lossily, and cannot
            # give the identifier back.
            "message_ts": message_ts,
            # Settled for the sender of this message, which is the principal
            # the rule needs: the answer is posted back into this conversation,
            # and who asked is the sender of the triggering message rather than
            # whoever the session happens to belong to.
            **slack_history_request_metadata(
                self.config,
                channel_id,
                user_id=user_id,
                workspace=self._workspace_team_id,
            ),
            **slack_write_request_metadata(self.config),
        }
        # Published unconditionally rather than for a hand-kept subset. The field
        # is diagnostic, and a filter list would have to be edited every time a
        # trigger name was added -- a maintenance step whose only symptom when
        # missed is a new trigger silently reading as a mention downstream.
        metadata["slack_trigger"] = trigger
        # The delivery path can recompute neither. It sees a reply, which says
        # nothing about the kind of room its request was settled for and
        # nothing about whether the message behind it opened with a mention.
        #
        # The first decides whether a token is honoured. The second decides one
        # log line: an addressed turn that stays silent is the case an operator
        # goes looking for, and it is indistinguishable from any other silence
        # by the time the reply comes back.
        #
        # Each set only when true, the way the action token is. Absent is how
        # both readers already tell a turn that holds the fact from one that
        # does not, so writing ``False`` would add a key to the metadata of
        # every message in every conversation to state the default, and this
        # bag crosses a process boundary on each one.
        if silence_is_honoured:
            metadata[SLACK_REPLY_OPTIONAL_KEY] = True
        if addressed:
            metadata[SLACK_ADDRESSED_KEY] = True

        # Slack mints a short-lived permission per inbound event and hands it to
        # the app on the event itself. It is what a bot-token search call is
        # refused without, and it exists nowhere else: this is the only moment
        # it is in reach, and it is gone by the time the turn asks for it.
        #
        # Put on request metadata rather than offered as a tool argument,
        # for the reason the conversation id is: metadata is written by the
        # gateway before the turn starts and is not part of the surface a model
        # can write to.
        #
        # Set only when there is one. A turn with no token is the ordinary case
        # -- Slack sends one with a direct message either way, but only with a
        # channel message that mentions the app -- and an absent key is how the
        # runtime tells that turn apart from one that can search. An empty
        # string would read as present.
        action_token = _slack_action_token(event, body)
        if action_token:
            # Imported where it is used: the harness tool package initialises
            # the whole toolset, and this module is imported by
            # ``channel_manager``'s package, which
            # ``test_gateway_lazy_exports`` keeps free of that cost. By the
            # time an event arrives the toolset is loaded anyway.
            from jiuwenswarm.agents.harness.common.tools.slack_search import (
                SLACK_ACTION_TOKEN_KEY,
            )

            metadata[SLACK_ACTION_TOKEN_KEY] = action_token

        avatar_mode = await self._apply_avatar_context(
            metadata,
            is_dm=is_dm,
            channel_id=channel_id,
            user_id=user_id,
            text=text,
            message_ts=message_ts,
        )

        params: dict[str, Any] = {"content": text, "query": text}
        params.update(slack_agent_request_params(
            self.config, channel_id, user_id=user_id, chat_type=chat_type,
            workspace=self._workspace_team_id,
        ))
        if downloaded:
            params["files"] = self._attachment_params(downloaded)
            params["media_items"] = downloaded
        # Put on every request rather than set once when the session opens,
        # because that is the only shape the server offers. The adapter picks a
        # turn's model from this param alone and applies it by mutating the live
        # agent; a request that omits it resolves to the configured default
        # rather than to whatever the session last ran on. So there is no
        # create-time model to set, and no stored model to inherit -- a turn that
        # does not say which model it wants gets the default, and one session id
        # is routinely served by more than one model over its life.
        #
        # Sent only when the name is non-empty, because an empty value would be
        # a claim this connector should not make on behalf of the channels that
        # never asked for a model.
        model_name = self._channel_model_name(channel_id, user_id, chat_type)
        if model_name:
            params["model_name"] = model_name

        req = Message(
            id=event_id or f"slack-{int(time.time() * 1000)}",
            type="req",
            channel_id=self.channel_id,
            session_id=session_id,
            params=params,
            timestamp=time.time(),
            ok=True,
            provider="slack",
            chat_id=channel_id,
            user_id=user_id,
            req_method=ReqMethod.CHAT_SEND,
            # A request that is not marked as streaming is answered by one
            # terminal event, so the chat.update path below never sees a delta to
            # fold in. Asking for a stream is what produces them. Left false while
            # streaming is off, which is the default: an unread scheduled push has
            # nothing to gain from the intermediate events a stream also sends.
            is_stream=self._streaming_enabled(),
            # The workspace this instance serves, carried upstream so the reply
            # comes back to it. Set on every request the connector makes; see
            # the ``app_id`` property for why it is not the Slack team id.
            app_id=self.app_id,
            bot_id=self.app_id,
            metadata=metadata,
            group_digital_avatar=avatar_mode,
            enable_memory=self.config.enable_memory if avatar_mode else None,
        )

        # What becomes of this message if that session is already working. The
        # decision is made here, in the connector, because for all three values
        # the connector is what acts: it looks at whether a turn is live and
        # then sends normally, sends a steer, or sends nothing at all. Under
        # ``queue`` the message never leaves this process, so there is nothing
        # downstream for anyone else to decide.
        mid_turn = self._mid_turn_mode(
            channel_id,
            user_id,
            chat_type=chat_type,
            session_is_channel_wide=session_is_channel_wide,
        )
        # The liveness signal, read once. ``None`` is a session with no turn in
        # flight, which is the ordinary case and takes the ordinary path
        # whichever value is set: a steer sent with no round to join does not
        # fail, it silently becomes a follow-up, which is a fourth mode nobody
        # chose.
        # The entry itself rather than a boolean, because it is also what names
        # the turn a cancel is about to destroy. Read once, here, and used
        # twice: nothing between this line and the dispatch can change it.
        superseded = self.turn_initiator(session_id)
        running = superseded is not None

        # A message joins the queue when the session is working *or* when it
        # already holds one. The second half is what keeps the order: a queue
        # left behind by a turn whose ending never arrived would otherwise be
        # overtaken by every message sent afterwards, so the messages already
        # waiting would run last, in a conversation that had moved on.
        if mid_turn == MID_TURN_QUEUE and (
            running or self._queued_messages.get(session_id)
        ):
            entry = _SlackQueuedMessage(
                request=req,
                session_id=session_id,
                user_id=user_id,
                team_id=team_id,
                is_dm=is_dm,
                chat_type=chat_type,
                channel_id=channel_id,
                message_ts=message_ts,
                thread_ts=reply_thread_ts,
            )
            if not self._queue_mid_turn_message(entry):
                await self._reject_request(channel_id, message_ts)
                # The mark says "not accepted"; this says why, and that trying
                # again later will work. Both, because the mark is on the
                # message and the notice is not, and only the mark survives a
                # reader scrolling back.
                await self._explain_queue_refusal(channel_id, user_id)
                return f"refused:queue-full session={session_id}"
            await self._mark_queued(entry)
            # Attempted immediately, and it is a no-op while the turn this is
            # waiting behind is still running. It is here for the case the
            # drain on the terminal event cannot cover: a queue whose turn ended
            # without an ending ever reaching this connector would otherwise sit
            # untouched until the age bound retired it, and every later message
            # would join a queue that nothing was left to empty. Popping the
            # head rather than the message just added is what keeps the order
            # the sender sees the same as the order the agent runs.
            await self._drain_queued_messages(session_id)
            return f"queued:{trigger} session={session_id}"

        if running and mid_turn == MID_TURN_STEER:
            # The one parameter, and it does two things: the gateway reads it
            # and skips the cancel it performs before every other chat.send,
            # and the adapter maps it onto the runtime's STEER dispatch so the
            # text joins the running round before its next model call.
            params["input_mode"] = MID_TURN_STEER
            # No initiator is recorded, and that is the point rather than an
            # omission. Steering someone's turn is contributing to their work,
            # not starting your own, so the entry stays with whoever started it
            # -- which is also what keeps a stop floor where it belongs: A may
            # still stop the turn B steered.
            #
            # Nor are the session's questions withdrawn. The ordinary path
            # withdraws them because the message it is dispatching ends the turn
            # that asked them; a steer ends nothing, so buttons still on screen
            # still answer the turn that is still waiting on them.
            logger.info(
                "[SlackChannel] steering the running turn: session=%s"
                " request_id=%s user=%s",
                session_id,
                req.id,
                user_id,
            )
            # The same correction _mark_queued makes for the queue branch:
            # _acknowledge_request already wrote thinking_status for this
            # message before the mid-turn decision was made, and a steer is
            # not what that status describes -- nothing new started, the
            # running turn absorbed this one. reply_thread_ts is the running
            # turn's own thread: _mid_turn_mode above never returns STEER
            # under a channel-wide session, which is the only case a steer's
            # thread could differ from the turn it joins.
            if self._acknowledge_mode() != ACK_MODE_OFF:
                await self._set_steered_status(channel_id, reply_thread_ts)
            await self._route_request(req)
            return f"steered:{trigger} session={session_id}"

        # Both recorded here, below the branch, because both describe the turn
        # this message is about to start and neither of the paths above starts
        # one. Before the dispatch, so that the turn cannot produce an event --
        # or reach a stop -- with nothing on record.
        #
        # chat.startStream refuses a channel stream without the user it is for,
        # and this is the last point at which the user is on the wire: the reply
        # comes back holding the session and the channel, and the runtime drops
        # the user id at the boundary.
        self._remember_stream_recipient(session_id, user_id, team_id)
        # The request id is the request's own and is settled here, and the
        # coordinates travel with it. They are three separate values on purpose:
        # a reaction mark lands on the triggering message, while the thinking
        # status is opened and cleared at the thread root, and those are not the
        # same ts whenever the trigger was itself a reply inside a thread.
        self._remember_turn_initiator(
            session_id,
            user_id,
            req.id,
            is_dm=is_dm,
            chat_type=chat_type,
            channel_id=channel_id,
            message_ts=message_ts,
            thread_ts=reply_thread_ts,
            # The request's own bag rather than the locals it was built from.
            # ``silence_is_honoured`` above is a decision about this message's
            # room, taken with the message in hand, and a turn resumed after an
            # interrupt has to be given that decision back rather than asked to
            # make it again from a click that names neither the room's kind nor
            # the moment the config was read.
            request_metadata=req.metadata,
        )

        # Ahead of the routing, because this message is what ends any interrupt
        # the session is paused on: once it is in flight the buttons answer
        # nothing, and the gap between the two is a click that would be routed
        # as an answer to a turn that has stopped waiting for one.
        await self._withdraw_questions_for_session(session_id)

        # Reaching this line with a turn already running means this message is
        # about to cancel it, and the turn it cancels reports no ending of its
        # own. See ``_end_superseded_turn``.
        if running:
            await self._end_superseded_turn(superseded, channel_id, req.id)

        await self._route_request(req)
        return f"dispatched:{trigger} session={session_id}"

    async def _apply_avatar_context(
        self,
        metadata: dict[str, Any],
        *,
        is_dm: bool,
        channel_id: str,
        user_id: str,
        text: str,
        message_ts: str,
    ) -> bool:
        """Add the digital-avatar metadata and report whether the mode applies.

        Returns ``False`` -- leaving ``metadata`` untouched -- unless
        ``channels.slack.group_digital_avatar`` is on, an adapter is registered
        and the message came from a channel rather than a DM. That keeps the
        connector's behaviour bit-identical when the feature is off, and leaves
        direct messages alone even when it is on: the pipeline exists to decide
        which of a channel's messages concern the principal, a question a DM to
        the bot has already answered.

        The keys written here are the platform-neutral ones IMInboundPipeline
        and IMOutboundPipeline read; the ``slack_*`` keys the rest of the
        connector uses stay as they are.
        """
        adapter = self._im_platform_adapter
        if not self.config.group_digital_avatar or adapter is None or is_dm:
            return False

        await adapter.observe_message(
            channel_id=channel_id,
            # The event's own type, not a constant: Slack reports a private
            # channel as "group", and the history toolkit validates the value
            # against the conversation it is asked to read.
            channel_type=str(metadata.get("slack_channel_type") or "channel"),
            user_id=user_id,
            text=text,
            message_ts=message_ts,
        )

        metadata["im_platform"] = "slack"
        # Both spellings: the inbound pipeline reads im_chat_type, while the
        # outbound pipeline and the relevance patch read chat_type.
        metadata["chat_type"] = "group"
        metadata["im_chat_type"] = "group"
        metadata["im_sender_user_id"] = user_id
        # A Slack channel is the conversation the avatar reasons over, so the
        # thread id is the channel rather than any one thread_ts: relevance is
        # judged against everything said in the room.
        metadata["im_thread_id"] = channel_id
        metadata["timestamp_ms"] = _slack_timestamp_ms(message_ts)
        mentioned_user_ids = _mentioned_user_ids(text)
        if mentioned_user_ids:
            metadata["im_mentioned_user_ids"] = mentioned_user_ids
        metadata["avatar_mode"] = True
        metadata["principal_user_id"] = self.config.my_user_id.strip()
        metadata["triggering_user_id"] = user_id
        return True

    async def _load_bot_user_id(self) -> None:
        if self._client is None:
            return
        try:
            response = await self._client.auth_test()
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Slack auth.test failed; bot mentions may be duplicated: %s", exc
            )
            return

        # From the headers of the call that has just answered, before anything
        # is read out of its body. Slack puts the granted bot scopes on every
        # Web API response, so the whole of what this install was given is in
        # reach here for no second call -- and it has to be read here, because
        # this is the only response the connector holds before the handler
        # starts and messages begin arriving.
        self._granted_scopes = slack_granted_scopes(getattr(response, "headers", None))

        self._bot_user_id = str(response.get("user_id") or "").strip()
        if not self._bot_user_id:
            logger.warning("Slack auth.test returned no bot user_id")
        # The same payload holds the bot id, which was being discarded. It is
        # not an identity anything dispatches on -- nothing here starts treating
        # it as a mention -- and is retained for one purpose: recognising a
        # message that addressed the bot by it, so that mistake can be reported
        # instead of disappearing into the channel's ordinary traffic.
        self._bot_id = str(response.get("bot_id") or "").strip()
        # The workspace itself, from the same payload. It is what decides which
        # of several configured connections an outbound message belongs to, so
        # a deployment running more than one workspace cannot route replies
        # without it -- hence the WARNING rather than the silence the host
        # below settles for. One workspace never reads it.
        self._workspace_team_id = str(response.get("team_id") or "").strip()
        if not self._workspace_team_id:
            logger.warning(
                "Slack auth.test returned no team_id; with more than one"
                " workspace configured this connection cannot be matched to"
                " the replies meant for it"
            )
        # And the workspace's own host, from the same payload's ``url``
        # ("https://acme.slack.com/"). It is read here rather than by a second
        # auth.test because this is the call that already has it, and it is
        # needed before the first attachment arrives: the handler is started
        # after this returns.
        self._workspace_host = _https_host(response.get("url"))
        if not self._workspace_host:
            # Not fatal and not a scope problem: files.slack.com alone is what
            # every ordinary url_private is on. Logged because the narrower
            # allow-list is a fact worth having in the log if a download is
            # later refused for its host.
            logger.info(
                "Slack auth.test returned no workspace url; inbound file"
                " downloads will accept %s only",
                SLACK_FILE_HOST,
            )
        self._publish_bot_user_id()

    def _workspace_label(self) -> str:
        """How one line names which of several connections it is about.

        The team id once ``auth.test`` has said it, and the gateway's own name
        for the credential block otherwise. Both, when they differ and both are
        known, because an operator holds the config under one of them and reads
        Slack's admin pages under the other.

        ``""`` for a deployment that configured one workspace and named
        nothing -- there is no second connection for a line to be confused
        with, and a bare ``default`` in every message would be noise.
        """
        block = self.config.app_id.strip()
        team = self._workspace_team_id
        if block and team:
            return f" [{block}, {team}]"
        return f" [{block or team}]" if (block or team) else ""

    def _scopes_permit_start(self) -> bool:
        """Say what this install was and was not granted. ``False`` = do not start.

        Runs once, at startup, on the header ``auth.test`` has just answered
        with. It reads :mod:`jiuwenswarm.common.slack_scope_policy` -- the same
        table the shipped app manifests are rendered from -- so what is demanded
        here and what an operator was handed to paste into Slack cannot come to
        disagree.

        **A missing Tier 0 grant refuses to start this channel, and refuses
        nothing else.** ``logger.error`` and ``return``, following the token
        checks three lines above it in :meth:`start`: no exception and no exit,
        because the gateway serves other connectors and other workspaces and
        none of them has anything to do with this app's manifest. With several
        workspaces configured the degrade is therefore per workspace -- the one
        whose install is short stops, the rest run.

        **A missing Tier 1 or Tier 2 grant is said once and the channel
        starts.** That is the tiering working as designed: every model-facing
        tool refuses cleanly and by name when Slack answers ``missing_scope``,
        so a narrower install is a bot that can do less rather than a bot that
        is broken. The line names what each absent scope was for, from
        ``scope_reasons``, because that is the actionable half -- "reactions:write
        is not granted" tells an operator nothing they can weigh, while "the
        acknowledgement mark" tells them what they are choosing to go without.

        **A header this connector could not read starts the channel.** Refusing
        over a header rather than over a grant would take a working install down
        for a reason that is not about the install, and the whole check is a
        convenience riding on a call made for something else. Said at INFO, so
        the absence of the usual line is not read as a clean bill of health.
        """
        granted = self._granted_scopes
        label = self._workspace_label()
        if granted is None:
            logger.info(
                "Slack auth.test answered without %s, so the granted scopes"
                " could not be checked%s; a scope this app is missing will be"
                " reported by the call that needs it instead",
                _GRANTED_SCOPES_HEADER,
                label,
            )
            return True

        required = slack_required_scopes()
        absent_core = sorted(required - granted)
        if absent_core:
            logger.error(
                "SlackChannel%s was not granted %s, without which it cannot"
                " receive or answer a message; not starting this workspace."
                " Reinstall the app from a manifest holding these scopes; the"
                " other configured channels are unaffected",
                label,
                ", ".join(absent_core),
            )
            return False

        reasons = scope_reasons(TIER_TOOLS)
        absent = sorted(
            scope for scope in scopes_for_tier(TIER_TOOLS) if scope not in granted
        )
        if absent:
            logger.warning(
                "SlackChannel%s was not granted %s. The features each one"
                " carries are unavailable and the calls that need it will be"
                " refused by Slack: %s. This is said once, at startup",
                label,
                ", ".join(absent),
                "; ".join(
                    f"{scope} -> {', '.join(reasons.get(scope) or ['no call here needs it'])}"
                    for scope in absent
                ),
            )
        else:
            logger.info(
                "SlackChannel%s holds every scope this connector asks for", label
            )
        return True

    def _remember_bot_user_id(self, body: dict[str, Any]) -> None:
        if self._bot_user_id:
            return
        authorizations = body.get("authorizations")
        if not isinstance(authorizations, list):
            return
        for authorization in authorizations:
            if not isinstance(authorization, dict) or not authorization.get("is_bot"):
                continue
            user_id = str(authorization.get("user_id") or "").strip()
            if user_id:
                self._bot_user_id = user_id
                self._publish_bot_user_id()
                return

    def _learn_chat_type(self, event: "Mapping[str, Any] | None") -> None:
        """Keep the kind of conversation a payload states, for the ones that do not.

        Called at the top of the message routers, ahead of every gate they
        apply. A message that is ignored -- a ``channel_join`` notice, an edit,
        a bot's own post, a room with no trigger configured -- states the kind
        of its conversation just as truthfully as one that is answered, and
        dropping it before learning from it would leave the map cold in exactly
        the busy rooms it is wanted for.

        Nothing is written unless Slack named both the conversation and one of
        the four words. In particular a ``member_joined_channel`` naming its
        conversation as ``"C"`` teaches nothing, because ``"C"`` is not one of
        the four; see :data:`SLACK_CHAT_TYPES`.

        A later statement replaces an earlier one rather than being discarded.
        A conversation's kind does change -- a public channel can be made
        private -- and Slack's most recent word on it is the better of the two.
        """
        if not isinstance(event, Mapping):
            return
        chat_type = event_chat_type(event)
        if chat_type not in SLACK_CHAT_TYPES:
            return
        chat_id = event_chat_id(event)
        if chat_id:
            self._chat_types[chat_id] = chat_type

    def _conversation_chat_type(
        self, chat_id: str, event: "Mapping[str, Any] | None" = None
    ) -> str:
        """What kind of conversation ``chat_id`` is, or ``""`` if it is not known.

        The single reader every decision that turns on a kind goes through --
        the events disposition, ``delivery.reply``, ``delivery.session``,
        ``delivery.prompt``, ``agent.model_name`` -- so that one conversation
        gets one answer whichever payload is being handled at the time.

        **The kind belongs to the conversation, not to the payload.** Slack puts
        ``channel_type`` on a message and leaves it off a reaction, a pin and an
        ``app_mention``, but a reaction in a public channel is no less in a
        public channel for the payload's silence. Reading it off the payload
        alone therefore made every rule naming a kind inert for those -- and
        inert in the direction that is hardest to see, since a rule that matches
        nothing leaves the conversation on the layer below and looks like a room
        that was simply never configured.

        Three sources, best evidence first, and each is something Slack stated
        rather than something worked out:

        1. This payload, when it carries one of the four words. Slack's live
           statement about the conversation the payload came from, and the only
           source that can catch a channel converted to private since the last
           one.
        2. What a payload in this conversation stated earlier -- see
           :meth:`_learn_chat_type`. The same fact, kept rather than re-asked.
        3. The conversation id, which states one kind and only one: ``D`` is a
           direct message. See :func:`chat_type_from_chat_id` for why no other
           prefix answers.

        **An unknown kind stays unknown.** There is no fourth source and no
        default. A rule naming a kind then matches nothing, which leaves the
        conversation on the layer below -- the same outcome as before this
        method existed, and the safe one: a kind guessed wrong does not fail to
        match, it matches the *wrong* rule, and applies a setting written for
        public rooms inside a private channel or a group DM.

        **No API call, ever.** ``conversations.info`` would answer this exactly,
        and it is refused on the same ground ``render_event`` refuses a name
        lookup and ``event_anchor`` refuses a thread lookup: this is asked on
        the path that decides whether an event is even recorded, which every
        reaction in every room reaches. An event must not cost more to route
        than a message does, and a message costs no call at all.
        """
        stated = event_chat_type(event)
        if stated in SLACK_CHAT_TYPES:
            return stated
        return self._chat_types.get(chat_id, "") or chat_type_from_chat_id(chat_id)

    def _publish_bot_user_id(self) -> None:
        """Hand the bot's own id to the avatar adapter, if one is registered.

        The adapter needs it to build the ``<@U…>`` mention token that makes a
        message unconditionally relevant, and it is only known after auth.test
        or the first event body that holds an authorization block.
        """
        adapter = self._im_platform_adapter
        if adapter is None or not self._bot_user_id:
            return
        setter = getattr(adapter, "set_bot_user_id", None)
        if callable(setter):
            setter(self._bot_user_id)

    def _has_leading_bot_mention(self, text: str) -> bool:
        pattern = self._leading_bot_mention_pattern()
        return pattern is not None and pattern.match(text) is not None

    def _strip_leading_bot_mention(self, text: str) -> str:
        pattern = self._leading_bot_mention_pattern()
        if pattern is None:
            return text
        return pattern.sub("", text, count=1)

    def _mentions_bot_by_bot_id(self, text: str) -> bool:
        """True when ``text`` addresses this bot by its bot id.

        Slack only turns ``<@U…>`` -- the bot's user id -- into a mention. The
        bot id is a ``B…`` string from the same auth.test payload, it renders as
        literal text, it matches no trigger and it raises no app_mention, so a
        message written with it is ignored in complete silence. That happened:
        a question asked in a watched channel got no reply and no error, and its
        log line was the same ``no-trigger-matched`` the standup notes beside it
        recorded. Ninety-two seconds later the sender retyped it with the right
        id and it worked.

        This names that case and nothing else. It is not a guess at who a message
        was meant for: only this bot's own id counts, so ``<@U…>`` naming a
        colleague stays ordinary chatter, and a typed ``@name`` -- which is a
        valid token elsewhere in this connector -- is not touched. It changes no
        dispatch: a bot id is still not a way to address this bot, and making it
        one is a decision for the operator, not a side effect of a log fix.

        Fails closed when auth.test never resolved the id, like _is_reply_to_bot.
        """
        if not self._bot_id:
            return False
        return f"<@{self._bot_id}>".casefold() in text.casefold()

    def _leading_bot_mention_pattern(self) -> re.Pattern[str] | None:
        if not self._bot_user_id:
            return None
        return re.compile(
            rf"^\s*<@{re.escape(self._bot_user_id)}>\s*",
            re.IGNORECASE,
        )

    def _acknowledge_mode(self) -> str:
        mode = str(self.config.acknowledge_mode or "").strip().lower()
        if mode not in ACKNOWLEDGE_MODES:
            logger.warning(
                "Unknown Slack acknowledge_mode %r; falling back to %r",
                self.config.acknowledge_mode,
                ACK_MODE_REACTION,
            )
            return ACK_MODE_REACTION
        return mode

    @staticmethod
    def _reaction_name(emoji: str) -> str:
        """One configured ``*_emoji`` value as Slack's ``name``, or ``""``.

        Both reaction helpers below already treat an empty name as nothing to
        do, so this is where a silenced mark becomes that: ``REACTION_DISABLED``
        returns ``""`` and neither the add nor the remove for that one mark is
        ever issued.

        ``resolve_reaction_emoji`` has already read the word by the time a
        config built from the template's ``channels.slack`` block reaches here.
        This is the second reading, for the same reason ``_acknowledge_mode``
        re-validates a mode the gateway also resolved: a ``SlackChannelConfig``
        can be constructed directly, and one carrying the word must not react
        with an emoji literally named ``disabled``.

        Read before the colons come off, which is what leaves ``:disabled:``
        available to a workspace whose custom emoji really is named that.
        """
        name = str(emoji or "").strip()
        if not name or name == REACTION_DISABLED:
            return ""
        return name.strip(":")

    async def _add_reaction(
        self, channel_id: str, timestamp: str, emoji: str
    ) -> Exception | None:
        """Add a reaction, returning the failure instead of raising.

        Acknowledgement is best-effort: a missing ``reactions:write`` scope or an
        ``already_reacted`` race must never stop the request from being handled.

        Reported through ``_report_reaction_failure`` rather than logged here,
        because every caller discards what this returns and the default
        ``acknowledge_mode`` attempts a mark on every message the channel takes.
        A refusal that is a property of the install rather than of the message
        is therefore the same refusal several times a minute for as long as the
        bot runs. See that method for what is said, and how often.
        """
        name = self._reaction_name(emoji)
        if not name or not channel_id or not timestamp or self._client is None:
            return None
        try:
            await self._client.reactions_add(
                channel=channel_id, timestamp=timestamp, name=name
            )
        except Exception as exc:  # noqa: BLE001
            self._report_reaction_failure(exc, name)
            return exc
        # DEBUG, not INFO: an acknowledgement lands on every accepted request
        # and says nothing about whether the reply itself was delivered.
        logger.debug(
            "[SlackChannel] reaction added: channel=%s ts=%s emoji=%s",
            channel_id,
            timestamp,
            name,
        )
        return None

    def _report_reaction_failure(self, exc: Exception, name: str) -> None:
        """Say what ``reactions.add`` refused with -- once, then at DEBUG.

        **The failure is not silenced, and it is not the failure that is the
        defect.** A reaction that cannot be posted is a real loss: the sender
        gets no sign the message was taken. What was wrong was the rate. An
        install lacking ``reactions:write`` refuses every acknowledgement this
        connector ever attempts, so the warning was written once per inbound
        message for the life of the process -- which is how a warning worth
        reading stops being read, and takes the unrelated warnings around it
        with it.

        So the first of each kind is a WARNING and every repeat is a DEBUG,
        which keeps the whole record available to anyone who turns the level
        down without putting it in front of an operator several times a minute.

        The kind is Slack's own error code paired with the emoji; see the latch
        in ``__init__`` for why the pair and not the code alone, and for why it
        is held per instance and therefore per workspace.

        A failure that never reached Slack -- a timeout, a dropped
        connection -- has no code, and takes the exception's class name as its
        kind rather than sharing one bucket with every refusal. Those are
        transient by nature, and latching one behind a scope refusal would drop
        the only line saying the connection is unwell.

        This says nothing about which grant is missing. That belongs to the
        startup scope check, which reads the granted scopes once and names what
        each absent one costs; a per-message line cannot be the place an
        operator learns it, whatever it says.
        """
        reason = slack_failure_reason(exc)
        kind = (slack_error_code(exc) or exc.__class__.__name__, name)
        if kind in self._reported_reaction_failures:
            logger.debug("Slack reactions.add %r failed again: %s", name, reason)
            return
        self._reported_reaction_failures.add(kind)
        logger.warning(
            "Slack reactions.add %r failed: %s. An acknowledgement is attempted"
            " on every message this channel takes, so this is said once and"
            " logged at DEBUG from here on",
            name,
            reason,
        )

    async def _set_thread_status(
        self, channel_id: str, thread_ts: str, status: str, *, label: str = ""
    ) -> None:
        """The one call every thread-status setter and the clear route through.

        ``status`` travels through unexamined, empty string included -- that is
        how ``assistant.threads.setStatus`` clears whatever it is currently
        showing, and it is also how every setter above this one replaces
        whatever was there before: the method holds one string per thread, so
        the thinking, queued and steered status all draw here, and the most
        recent call is the only one a reader ever sees. Nothing here decides
        which of them that should be -- each caller already knows, from where
        it is placed in the connector's own flow, that it is the latest event
        for the thread it names -- so there is no ordering or bookkeeping to
        get wrong here.

        Guarded once, for every caller: no client, no ``channel_id``, no
        ``thread_ts`` means no call. That covers a conversation with no thread
        to draw one in -- a DM answered at the top level -- for the setters and
        the clear alike, since none of them ever opened a status there to
        begin with.

        Best effort throughout, and never fails the turn: the request this
        status is about is acknowledged and the reply is unaffected whatever
        this call does, so a refusal is logged at DEBUG and swallowed rather
        than raised -- a workspace where the method is unavailable would
        otherwise warn once per call forever. ``label`` only changes what that
        DEBUG line says failed, distinguishing a refused clear from a refused
        set without every caller keeping its own except-block to say so.
        """
        if not channel_id or not thread_ts or self._client is None:
            return
        try:
            await self._client.api_call(
                _ASSISTANT_SET_STATUS_METHOD,
                data={
                    "channel_id": channel_id,
                    "thread_ts": thread_ts,
                    "status": status,
                },
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "Slack %s%s failed: %s",
                _ASSISTANT_SET_STATUS_METHOD,
                f" ({label})" if label else "",
                exc,
            )

    async def _set_thinking_status(self, channel_id: str, thread_ts: str) -> None:
        """Say in the thread that the turn is running.

        Best effort. Not stateless any more: the status this opens is closed by
        ``_clear_thinking_status``, called from every point a turn can end --
        the terminal event, a stop and a supersede -- rather than left to Slack
        alone. Two things used to be trusted to clear it on their own: this
        app's own reply landing in the same thread, and Slack's own two-minute
        timeout for a turn that never answers. The first is false whenever the
        turn's reply is the ``NO_REPLY`` sentinel under ``delivery.reply:
        optional`` -- a turn that posts nothing leaves nothing to drop the
        status -- and the second was never a defect this fix disputes, only a
        delay too long to stand next to a turn that has already finished: the
        status was observed live to expire on its own, so the timeout is real,
        it is just slower than a reader waiting on it. A turn still working
        past either has the activity card by then, which is the rung above
        this one.

        ``thread_ts`` is the thread the reply will be posted into -- the same
        value the caller hands ``chat.startStream`` -- so the status is always
        set where the ending clears it from. A conversation with no thread to
        reply into, which is a DM answered at the top level, gets no status:
        the API has nowhere to put one, and the reaction the caller has already
        added is the acknowledgement there.

        Not the only setter that can leave this exact string standing: a
        message drained from the queue calls this again once it starts, and
        that is deliberate -- see ``_dispatch_queued_message``. Nothing here
        needs to know whether this call is the first for the thread or a
        replacement for ``queued_status``; ``_set_thread_status`` replaces
        whatever was there either way.
        """
        status = str(self.config.thinking_status or "").strip()
        if not status:
            return
        await self._set_thread_status(channel_id, thread_ts, status)

    async def _clear_thinking_status(self, channel_id: str, thread_ts: str) -> None:
        """Say the turn that opened the thinking status is no longer running.

        A sibling of ``_set_thinking_status`` rather than a parameter on it,
        because that method's own guard -- ``if not status ... return`` -- exists
        to skip the call entirely on a deployment with the status turned off,
        and a clear has no status of its own for that guard to test: the
        payload this sends is always the empty string, which is how
        ``assistant.threads.setStatus`` is cleared, whatever
        ``config.thinking_status`` currently reads. Folding the two into one
        method behind a flag would mean re-deriving that difference at every
        call site instead of choosing it once, here.

        Called from every place a turn can end -- the terminal branch in
        ``send()``, a stop and a supersede -- unconditionally, on a reply
        posted and a turn left silent alike: branching on whether a reply went
        out would re-derive the delivery decision ``SlackChannel.send()`` has
        already settled further down, and clearing after a reply that already
        dropped the status costs one refused-or-harmless call, not a wrong
        one. It is not gated on the acknowledge mode the way ``_mark_turn_ended``
        is, because the status itself is not: ``_acknowledge_request`` opens it
        for every mode that acknowledges at all, ``text`` included, not only
        the two that touch a reaction, so a clear gated on the reaction modes
        would leave exactly the ``text`` deployments with the defect this
        exists to close.

        Same coordinates as the setter and the same absent case: no client, no
        ``channel_id``, no ``thread_ts`` means no call, and a DM answered at
        top level -- which never got a status to begin with -- gets none of
        this either.

        Never fails the turn. Swallowed and logged at DEBUG for the same
        reason as the setter: nothing has degraded, and a workspace without
        this method would otherwise warn once per turn forever.

        Also the ending for ``queued_status`` and ``steered_status``: neither
        has a clear of its own, because none is needed. Both are opened on the
        same thread this closes, so whichever of the three is standing when a
        turn ends is closed by the one call already wired to every ending.
        """
        await self._set_thread_status(channel_id, thread_ts, "", label="clear")

    async def _set_queued_status(self, channel_id: str, thread_ts: str) -> None:
        """Say in the thread that the latest message is waiting, not running.

        The correction ``_mark_queued`` makes beside the reaction it adds.
        Without this, a message that joins the queue leaves standing whatever
        ``_acknowledge_request`` already wrote for it moments earlier --
        ordinarily ``thinking_status``, since the acknowledgement runs before
        the queue decision is made -- and the thread goes on claiming the turn
        is thinking about a message that is, in fact, sitting behind another
        one. This call is what makes that claim current again.

        No clear of its own. ``queued_status`` is closed the same way
        ``thinking_status`` is: either the running turn ends and
        ``_clear_thinking_status`` drops whatever is showing, or the held
        message is itself drawn and ``_set_thinking_status`` overwrites this
        with the running status again -- see ``_dispatch_queued_message``.
        Either is the next real event for this thread, and
        ``assistant.threads.setStatus`` already replaces on every call, so
        there is nothing to reconcile between them.
        """
        status = str(self.config.queued_status or "").strip()
        if not status:
            return
        await self._set_thread_status(channel_id, thread_ts, status)

    async def _set_steered_status(self, channel_id: str, thread_ts: str) -> None:
        """Say in the thread that the latest message joined the running turn.

        The correction the steer branch makes for the same reason
        ``_set_queued_status`` corrects the queue branch: the acknowledgement
        already wrote ``thinking_status`` for this message before the mid-turn
        decision was made, and a steered message is not what that status
        describes -- nothing new started, an already-running turn absorbed it.

        No initiator is recorded for a steer (see the caller), and no ending
        follows from this message either: the turn it joined keeps running
        under whoever started it, and that turn's own ending is what clears
        whatever this leaves standing.
        """
        status = str(self.config.steered_status or "").strip()
        if not status:
            return
        await self._set_thread_status(channel_id, thread_ts, status)

    async def _acknowledge_request(
        self, channel_id: str, thread_ts: str, message_ts: str
    ) -> None:
        mode = self._acknowledge_mode()
        if mode == ACK_MODE_OFF or self._client is None:
            return

        if mode in (ACK_MODE_REACTION, ACK_MODE_BOTH):
            # Reacts to the request itself, so message_ts is required here:
            # thread_ts would decorate the thread parent instead.
            await self._add_reaction(
                channel_id, message_ts, self.config.acknowledgement_emoji
            )

        # After the reaction rather than instead of it, and before the text
        # branch returns: the status belongs to every mode that acknowledges at
        # all, not only to the two that post a message.
        await self._set_thinking_status(channel_id, thread_ts)

        if mode not in (ACK_MODE_TEXT, ACK_MODE_BOTH):
            return

        text = self.config.acknowledgement_text.strip()
        if not text:
            return

        kwargs: dict[str, Any] = {"channel": channel_id, "text": text}
        if thread_ts:
            kwargs["thread_ts"] = thread_ts
        try:
            await self._client.chat_postMessage(**kwargs)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Slack acknowledgement failed: %s", exc)

    async def _reject_request(self, channel_id: str, message_ts: str) -> None:
        """Signal that a request will not be handled.

        Two callers, and one mark for both. The allow-list refusal is the
        original; a message arriving when that conversation is already holding
        as many as it may is the second, and it is the same fact from the
        sender's side -- this one is not going to be answered -- so giving it a
        third emoji would ask a reader to learn a distinction that changes
        nothing they can act on.

        Reaction only: posting "you are not allowed" into a shared channel is
        noise, and the modes that disable reactions opt out of feedback entirely.

        Not the mark a turn that broke gets. This one is written on the inbound
        path, before anything is dispatched, and it means no turn ever started:
        no tool ran, nothing was written, and the same message may go through
        later. A turn that started and then died is marked by
        ``_mark_turn_ended`` with ``failed_emoji``, and the two are kept apart
        because they decide different things for whoever reads them.

        Which is also why the two callers no longer end here alike. The mark is
        all an allow-list refusal has to say, because there is nothing the
        sender can do about it. A full queue is a passing state of one
        conversation and the same message works a few minutes later, so that
        caller follows this with ``_explain_queue_refusal`` -- ephemeral, to the
        sender alone, and therefore not the noise this paragraph rules out.
        """
        if self._acknowledge_mode() in (ACK_MODE_REACTION, ACK_MODE_BOTH):
            await self._add_reaction(
                channel_id, message_ts, self.config.rejected_emoji
            )

    async def _explain_queue_refusal(self, channel_id: str, user_id: str) -> None:
        """Tell the sender why their message was not taken, and what to do.

        The reaction ``_reject_request`` adds is the same one an allow-list
        refusal gets, and deliberately so: from the sender's side both mean
        "this will not be answered". But the two are not the same to act on. An
        allow-list refusal is permanent and there is nothing to do about it; a
        full queue is a transient state of one conversation, and the same
        message sent a few minutes later goes through. An emoji cannot state
        that difference, and seventeen refusals in a row in one DM is what not
        stating it looks like.

        Ephemeral, and the same mechanism the stop button's refusal uses: the
        refusal concerns one sender, and posting it into a shared thread would
        write at everyone else about a message they may not have seen.

        Silent only under ``off``, which is a deployment asking for no feedback
        at all. Notably not gated on the reaction modes the mark is gated on:
        under ``text`` there is no mark, so this is the only thing the sender
        would get, and withholding it there would leave that deployment's
        refusals entirely silent.
        """
        if self._acknowledge_mode() == ACK_MODE_OFF:
            return
        await self._post_ephemeral(
            channel_id, user_id, _QUEUE_FULL_NOTICE, notice="queue-full"
        )

    async def _remove_reaction(
        self, channel_id: str, timestamp: str, emoji: str
    ) -> None:
        """Take a reaction off, treating every failure as already gone.

        The mirror of ``_add_reaction`` and best-effort for the same reasons,
        with one of its own: ``no_reaction`` is the ordinary answer whenever the
        add it undoes never landed -- a missing ``reactions:write`` scope, a
        message deleted in the meantime -- and that is not a fault worth a line
        at the level the add's failure already got one.
        """
        name = self._reaction_name(emoji)
        if not name or not channel_id or not timestamp or self._client is None:
            return
        try:
            await self._client.reactions_remove(
                channel=channel_id, timestamp=timestamp, name=name
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("Slack reactions.remove %r failed: %s", name, exc)

    async def _mark_queued(self, entry: _SlackQueuedMessage) -> None:
        """Say the message is waiting for the turn to end -- on it, and in it.

        Two marks, at two granularities, for the same fact. The reaction lands
        on the message itself and is reaction-mode only, for the reason it
        always was: a queue posting a reply per held message would be louder
        than the conversation it is holding, and four people typing at once
        each need to see the state of their own message without the other
        three being written at. The status lands on the thread instead, and
        follows ``acknowledge_mode`` the way ``thinking_status`` does rather
        than the reaction modes alone -- ``_acknowledge_request`` already set
        ``thinking_status`` there for every mode that acknowledges at all,
        ``text`` included, before the queue decision was even made, and this is
        the correction for the modes that call ends up wrong for too.

        Both are skipped under ``off``, which is a deployment asking for no
        feedback at all -- both are feedback.
        """
        mode = self._acknowledge_mode()
        if mode == ACK_MODE_OFF:
            return
        if mode in (ACK_MODE_REACTION, ACK_MODE_BOTH):
            await self._add_reaction(
                entry.channel_id, entry.message_ts, self.config.queued_emoji
            )
        await self._set_queued_status(entry.channel_id, entry.thread_ts)

    async def _unmark_queued(self, entry: _SlackQueuedMessage) -> None:
        """Take the waiting mark off a message that is now being run."""
        if self._acknowledge_mode() not in (ACK_MODE_REACTION, ACK_MODE_BOTH):
            return
        await self._remove_reaction(
            entry.channel_id, entry.message_ts, self.config.queued_emoji
        )

    async def _mark_turn_ended(
        self, initiator: _SlackTurnInitiator, emoji: str
    ) -> None:
        """Say on the starting message how the turn it started ended.

        The acknowledgement comes off and one of the three endings goes on, so
        that the mark on a message states an outcome instead of a moment. Left
        alone, the acknowledgement says only "seen at some point" and says it
        forever, which in a conversation the agent works through all day is a
        column of identical emoji that separates nothing.

        **A turn that replied and a turn that did not are marked alike.** A turn
        that read a thread, decided nothing needed saying and stopped is a
        success, and this reaction is the only trace of it there will ever be:
        no reply was posted, and the activity card is a display feature that a
        deployment may have switched off and that a short turn never reaches.

        **Every ending replaces the acknowledgement, not only the good one.** A
        turn that broke or was halted leaves the running mark behind otherwise,
        and a stale "in progress" on work that stopped is worse than nothing:
        it says an answer is still coming.

        The ending is passed in rather than derived here. Three callers reach
        this and each already knows which of the three it is -- the terminal
        event, the stop button and the message that supersedes a turn -- and
        deriving it a second time from a flag would put the vocabulary in two
        places.

        **Removed first, added second.** One of the two calls can fail on its
        own -- Slack is best-effort here throughout -- and the two failures are
        not equally bad. Adding first and failing to remove leaves a message
        wearing both an "in progress" and an "ended" mark, which is a
        contradiction a reader has to resolve and may resolve the wrong way.
        Removing first and failing to add leaves it bare, which is the state of
        every message the bot never took, and reads as "no outcome recorded"
        rather than as a claim. The second call is issued whatever the first
        returned: neither helper raises, and the add is not conditioned on the
        remove, so a workspace where ``reactions.remove`` is refused still gets
        its ending mark.

        **Nothing here can fail the turn.** ``_add_reaction`` and
        ``_remove_reaction`` both swallow every exception by contract, and
        neither result is read. A missing ``reactions:write`` scope, a deleted
        message and an ``already_reacted`` race are all no-ops with a log line.

        Gated on the reaction modes, like the queue mark: under ``text`` and
        ``off`` there is no acknowledgement on the message to replace, and
        adding an ending to a deployment that asked for no reactions would be
        this method introducing the very thing the setting turns off.
        """
        if self._acknowledge_mode() not in (ACK_MODE_REACTION, ACK_MODE_BOTH):
            return
        if not initiator.channel_id or not initiator.message_ts:
            return
        await self._remove_reaction(
            initiator.channel_id,
            initiator.message_ts,
            self.config.acknowledgement_emoji,
        )
        await self._add_reaction(
            initiator.channel_id, initiator.message_ts, emoji
        )

    @staticmethod
    def _collect_event_files(event: Mapping[str, Any]) -> list[dict[str, Any]]:
        """Return the attachment records held by a Slack message event.

        Slack puts them in ``files`` as a list of file objects. Anything else
        found there is skipped rather than trusted, so a malformed payload
        degrades to "no attachments" instead of raising inside the handler.
        """
        raw = event.get("files")
        if not isinstance(raw, list):
            return []
        return [item for item in raw if isinstance(item, dict)]

    @staticmethod
    def _collect_event_attachments(event: Mapping[str, Any]) -> list[dict[str, Any]]:
        """Return the ``attachments`` entries a Slack message event holds.

        A different thing from the uploads the rest of this file calls
        attachments, which arrive in ``files``. This is Slack's secondary
        attachment array: a link unfurl, a shared message, a GIF, anything the
        platform or an app attached to what was posted. A message can hold all
        of its content here and nothing in ``text`` or ``files`` at all, which
        is how one that every human in the channel saw as content came to read
        as empty.

        Malformed entries are skipped rather than trusted, for the reason
        ``_collect_event_files`` skips them.
        """
        raw = event.get("attachments")
        if not isinstance(raw, list):
            return []
        return [item for item in raw if isinstance(item, dict)]

    @staticmethod
    def _slack_file_name(file_info: Mapping[str, Any]) -> str:
        """Name an attachment for a user-facing message.

        ``name`` is what the uploader sees. ``title`` and ``id`` are fallbacks
        for a payload that omits it -- naming the file by id is unhelpful but
        still better than an empty pair of parentheses.
        """
        for key in ("name", "title", "id"):
            value = str(file_info.get(key) or "").strip()
            if value:
                return value
        return "unnamed file"

    @staticmethod
    def _private_url(file_info: Mapping[str, Any]) -> str:
        """Return the authenticated download URL of a Slack file object.

        ``url_private_download`` forces a download response; ``url_private``
        serves the same bytes and is the fallback when the first is absent.
        """
        for key in ("url_private_download", "url_private"):
            url = str(file_info.get(key) or "").strip()
            if url:
                return url
        return ""

    @staticmethod
    def _safe_path_component(value: str, fallback: str) -> str:
        """Reduce a Slack-supplied name to a single safe path component.

        Slack filenames are user input and arrive with directory separators,
        spaces and non-ASCII intact, so they are never joined to a path as-is.
        Deliberately a local copy of what the browser-upload path does for the
        same reason: importing a private helper out of that module to share ten
        lines would couple the connector to it.
        """
        name = Path(str(value or "")).name.strip()
        if not name or name in {".", ".."}:
            name = fallback
        return UNSAFE_PATH_CHARS_RE.sub("_", name)[:180] or fallback

    @staticmethod
    def _unique_path(path: Path) -> Path:
        """Return *path*, or the first free ``name-N`` beside it.

        Two uploads named ``report.pdf`` in one session are ordinary, and the
        second must not silently overwrite what the agent was told to read.
        """
        if not path.exists():
            return path
        for index in range(1, 1000):
            candidate = path.with_name(f"{path.stem}-{index}{path.suffix}")
            if not candidate.exists():
                return candidate
        return path.with_name(f"{path.stem}-overflow{path.suffix}")

    async def _resolve_attachment(
        self, file_info: Mapping[str, Any]
    ) -> tuple[str, Mapping[str, Any]]:
        """Return ``(download url, file object)`` for one attachment.

        A ``file_share`` message normally embeds the whole file object, but a
        partial one still names the id, and ``files.info`` returns the rest.

        **One missing grant, one explanation.** ``files:read`` is what both
        halves of this path need -- ``files.info`` to look a file up, and the
        bearer GET on ``url_private`` to fetch it -- and without the branch
        below the two halves said different things about the same absent
        scope. The download half raised ``_FILE_REASON_UNAUTHORIZED``, which
        names the scope; the lookup half raised nothing at all, so Slack's
        ``missing_scope`` fell through to the caller's generic handler and the
        message was reported to the user as "the download from Slack failed"
        and to the log as an unexpected exception with a traceback. Which
        sentence an operator got depended on whether the ``file_share`` event
        happened to embed ``url_private``, which is not a distinction they can
        see, act on, or even find out about.
        """
        url = self._private_url(file_info)
        if url:
            return url, file_info

        file_id = str(file_info.get("id") or "").strip()
        if not file_id or self._client is None:
            raise SlackAttachmentError(
                "no private URL and no file id to look one up with",
                _FILE_REASON_DOWNLOAD_FAILED,
            )

        try:
            response = await self._client.files_info(file=file_id)
        except Exception as exc:  # noqa: BLE001
            # Caught here rather than left to the caller's generic handler,
            # which has only the exception and cannot tell a refused grant from
            # a network failure. Slack's own code decides: ``missing_scope`` is
            # the one answer that names something an operator can grant, and
            # every other refusal keeps the generic reason it already had rather
            # than being given an explanation this cannot stand behind.
            raise SlackAttachmentError(
                f"files.info for {file_id} failed: {slack_failure_reason(exc)}",
                _FILE_REASON_UNAUTHORIZED
                if slack_error_code(exc) == _MISSING_SCOPE_ERROR
                else _FILE_REASON_DOWNLOAD_FAILED,
            ) from exc
        refreshed = response.get("file") if response is not None else None
        if not isinstance(refreshed, Mapping):
            raise SlackAttachmentError(
                f"files.info returned no file object for {file_id}",
                _FILE_REASON_DOWNLOAD_FAILED,
            )
        url = self._private_url(refreshed)
        if not url:
            raise SlackAttachmentError(
                f"files.info returned no private URL for {file_id}",
                _FILE_REASON_DOWNLOAD_FAILED,
            )
        return url, refreshed

    def _slack_file_hosts(self) -> frozenset[str]:
        """The hosts a bot-token download may be sent to.

        ``files.slack.com`` always, and the workspace's own host once
        ``auth.test`` has named it. Two entries and no wildcard: a suffix match
        on ``.slack.com`` would be a rule about a string rather than about a
        host Slack told us it serves, and this list is short precisely because
        every entry is one Slack named.
        """
        if self._workspace_host:
            return frozenset({SLACK_FILE_HOST, self._workspace_host})
        return frozenset({SLACK_FILE_HOST})

    def _assert_slack_file_url(self, url: str) -> None:
        """Refuse a download URL Slack does not itself serve, before it is fetched.

        The URL comes out of a file record, and a file record is not the same as
        a fact about Slack. For an external file -- ``is_external: true``,
        ``mode: "external"`` -- Slack fills ``url_private`` with the URL
        whoever registered the file supplied, so the value is under the control
        of anybody who can share such a file into a channel this bot is in.
        Sending the bot token to it as a bearer header would hand them a live
        workspace credential.

        Checked here rather than at the two places a URL is resolved, because
        this is the one function that attaches the header: a later caller that
        finds a URL by some other route cannot route around the check without
        also writing its own request.

        Checks the first URL and nothing else, because it is not the only check
        a hop faces. ``stream_slack_file`` re-applies this same rule to every
        redirect target, and the transport applies it again to every request it
        is handed, so a ``Location`` header naming somewhere else is refused
        rather than followed.
        """
        if _https_host(url) in self._slack_file_hosts():
            return
        # The URL itself is deliberately absent from both the log line and the
        # user-facing reason. It is attacker-supplied text, it is what the
        # bearer header was nearly sent to, and naming it adds nothing an
        # operator can act on that the file's name and permalink do not.
        raise SlackAttachmentError(
            "the file's download URL is not on a Slack file host",
            _FILE_REASON_OFF_SLACK,
        )

    async def _fetch_attachment_bytes(self, url: str, mimetype: str) -> bytearray:
        """Download one attachment with the bot token.

        ``url_private`` is not a public link: an unauthenticated GET is answered
        with **200 and Slack's sign-in page**, so a missing or unscoped token
        looks exactly like a successful download of an HTML document. The
        content-type check below is what turns that into a reportable failure
        instead of an HTML file handed to the agent as if it were the upload.

        Returns a ``bytearray`` rather than ``bytes`` deliberately: the only
        caller writes it straight to disk, and converting would copy the whole
        attachment a second time for no benefit.
        """
        # Before the token is even read, let alone attached: a URL this bot will
        # not send a credential to is refused without a request being made.
        self._assert_slack_file_url(url)

        token = self.config.bot_token.strip()
        if not token:
            raise SlackAttachmentError(
                "no bot token configured", _FILE_REASON_UNAUTHORIZED
            )

        headers = {"Authorization": f"Bearer {token}"}
        timeout = httpx.Timeout(FILE_TRANSFER_TIMEOUT_SECONDS)
        # ``transport=`` is what makes the allow-list above mean anything at the
        # moment of connection. Without it httpx resolves the hostname itself,
        # separately from the lookup the check implied, and whoever answers the
        # second lookup decides where the bearer header goes. The transport
        # resolves once, refuses the answer unless every address in it is on the
        # public internet, and dials the address it validated.
        #
        # ``follow_redirects=False`` because a redirect target is a URL nobody
        # checked: httpx would take a host out of a ``Location`` header and dial
        # it without the allow-list ever seeing it. ``stream_slack_file``
        # follows them instead, one at a time, each through the same checks the
        # first URL faced.
        hosts = self._slack_file_hosts()
        async with httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=False,
            transport=slack_file_transport(hosts),
        ) as client:
            async with stream_slack_file(
                client, url, headers=headers, allowed_hosts=hosts
            ) as response:
                if response.status_code in (401, 403):
                    raise SlackAttachmentError(
                        f"HTTP {response.status_code}", _FILE_REASON_UNAUTHORIZED
                    )
                if response.status_code != 200:
                    raise SlackAttachmentError(
                        f"HTTP {response.status_code}", _FILE_REASON_DOWNLOAD_FAILED
                    )
                content_type = (
                    str(response.headers.get("content-type") or "")
                    .split(";")[0]
                    .strip()
                    .lower()
                )
                # Decided on the headers alone, before pulling a body that is
                # going to be discarded anyway.
                if content_type == "text/html" and mimetype != "text/html":
                    raise SlackAttachmentError(
                        "Slack served its sign-in page instead of the file",
                        _FILE_REASON_UNAUTHORIZED,
                    )

                # Accumulated into one growing buffer rather than a list of
                # chunks joined at the end, which held the whole attachment
                # twice at the moment of the join. The ceiling is checked
                # before each chunk is kept, so the buffer never exceeds it and
                # a file that is over the limit is abandoned mid-transfer
                # instead of being pulled down in full first.
                body = bytearray()
                size = 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > MAX_FILE_BYTES:
                        raise SlackAttachmentError(
                            f"over {MAX_FILE_BYTES} bytes",
                            _FILE_REASON_TOO_LARGE,
                        )
                    body.extend(chunk)

        return body

    async def _download_attachment(
        self, file_info: Mapping[str, Any], session_id: str
    ) -> dict[str, Any]:
        """Persist one attachment beside the session's other uploads."""
        url, resolved = await self._resolve_attachment(file_info)
        mimetype = str(resolved.get("mimetype") or "").strip().lower()
        name = self._slack_file_name(resolved)
        data = await self._fetch_attachment_bytes(url, mimetype)

        upload_dir = (
            get_agent_sessions_dir()
            / self._safe_path_component(session_id, "default")
            / "uploads"
        )
        upload_dir.mkdir(parents=True, exist_ok=True)
        file_id = str(resolved.get("id") or "").strip()
        path = self._unique_path(
            upload_dir / self._safe_path_component(name, file_id or "slack-attachment")
        )
        path.write_bytes(data)

        return {
            "type": "image" if mimetype.startswith("image/") else "document",
            "filename": path.name,
            "path": str(path),
            "mime_type": mimetype,
            "size_bytes": len(data),
            "slack_file_id": file_id,
        }

    async def _download_attachments(
        self, files: list[dict[str, Any]], session_id: str
    ) -> tuple[list[dict[str, Any]], list[tuple[str, str]]]:
        """Fetch every attachment, reporting per file rather than all-or-nothing.

        Returns the records that landed and ``(name, reason)`` for those that did
        not, so a mixed batch tells the user exactly which files the agent will
        not see instead of failing the whole message.

        The whole phase runs against one wall-clock deadline. Files are fetched
        one at a time, so without a shared budget each additional attachment
        would extend the worst case by another full timeout; sharing it means
        the message is held up for a bounded time no matter how many files it
        holds. Whatever the deadline cuts short is reported through the same
        per-file notice as any other failure, so a download given up on reads as
        an attachment the agent will not see rather than as silence.

        Kept sequential on purpose. Fetching in parallel would need its own
        concurrency bound to stop several attachments being held in memory at
        once, and the shared deadline already removes the reason to want it:
        the phase costs the same in the worst case either way. One file at a
        time also keeps peak memory at a single attachment.
        """
        downloaded: list[dict[str, Any]] = []
        failures: list[tuple[str, str]] = []
        deadline = time.monotonic() + _ATTACHMENT_PHASE_TIMEOUT_SECONDS
        for file_info in files:
            name = self._slack_file_name(file_info)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                # The budget went on the files before this one. Reported, not
                # attempted: starting a fetch that is already out of time only
                # delays the reply further.
                logger.warning(
                    "Slack attachment %r not read: no time left in the "
                    "download budget",
                    name,
                )
                failures.append((name, _FILE_REASON_TIMED_OUT))
                continue
            try:
                record = await asyncio.wait_for(
                    self._download_attachment(file_info, session_id), remaining
                )
            except TimeoutError:
                logger.warning(
                    "Slack attachment %r not read: download exceeded the %.0fs "
                    "budget",
                    name,
                    _ATTACHMENT_PHASE_TIMEOUT_SECONDS,
                )
                failures.append((name, _FILE_REASON_TIMED_OUT))
            except SlackAttachmentError as exc:
                logger.warning("Slack attachment %r not read: %s", name, exc)
                failures.append((name, exc.reason))
            except SlackFileTransferRefused as refusal:
                # Logged at its own code rather than folded into the blanket
                # clause below, which reports every unexpected exception as a
                # failed download with a traceback. This is not a download that
                # failed: it is one that was stopped, and the difference between
                # "a Slack host answered with a private address" and "the
                # connection dropped" is the whole point of having the code.
                logger.warning(
                    "Slack attachment %r not read: %s -- %s",
                    name,
                    refusal.code,
                    refusal.detail,
                )
                failures.append((name, _FILE_REASON_UNSAFE_ROUTE))
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "Slack attachment %r failed to download: %s",
                    name,
                    exc,
                    exc_info=True,
                )
                failures.append((name, _FILE_REASON_DOWNLOAD_FAILED))
            else:
                downloaded.append(record)
        return downloaded, failures

    @staticmethod
    def _describe_attachments(text: str, records: list[dict[str, Any]]) -> str:
        """Name the downloaded files, and where they are, in the agent's prompt.

        The structured records travel in ``params`` for the multimodal rail, but
        that rail only picks up images. Everything else reaches the agent solely
        as a path it can open, so the path has to be in the text.
        """
        lines = [
            f"- {record['filename']}"
            + (f" ({record['mime_type']})" if record["mime_type"] else "")
            + f": {record['path']}"
            for record in records
        ]
        header = (
            "Attached file, saved locally:"
            if len(records) == 1
            else f"{len(records)} attached files, saved locally:"
        )
        block = "\n".join([header, *lines])
        return f"{text}\n\n{block}" if text else block

    @staticmethod
    def _describe_event_attachments(
        text: str, attachments: list[dict[str, Any]]
    ) -> str:
        """State what a message's attachments show, under a header of their own.

        Read rather than dropped because a message can hold all its content
        here. Slack sends an empty ``text`` and no ``files`` for a GIF, for a
        shared message and for some unfurls, and the only readable thing in the
        payload is what the attachment states about itself.

        Under a header rather than folded into the sender's text, because the
        two are different claims. Nobody typed "shared a GIF"; Slack wrote it to
        say what was posted, and a model quoting the message back has to be able
        to tell that apart from a sentence the sender wrote.

        A header is written even when the walk finds nothing readable. That an
        attachment arrived at all is the fact worth stating -- it is what a
        human in the channel saw -- and the alternative is the silence this
        exists to end.

        Bounded, because an attachment may nest a whole Block Kit rendering and
        this is a description of the message rather than the message.
        """
        lifted = walk_strings(attachments, _ATTACHMENT_TEXT_KEYS)
        count = len(attachments)
        noun = "Attachment" if count == 1 else f"{count} attachments"
        header = (
            f"{noun} shown on this message:"
            if lifted
            else f"{noun} shown on this message, with no readable text."
        )
        block = _clamp(
            "\n".join([header, *lifted]), _MAX_ATTACHMENT_TEXT_CHARS
        )
        return f"{text}\n\n{block}" if text else block

    @staticmethod
    def _attachment_params(records: list[dict[str, Any]]) -> dict[str, Any]:
        """Group downloaded attachments the way the request params expect.

        ``files.uploaded_images`` is what the multimodal prompt builder reads;
        ``files.uploaded_documents`` mirrors the browser upload path so both
        arrive in one recognizable shape.
        """
        grouped: dict[str, Any] = {}
        images = [record for record in records if record["type"] == "image"]
        documents = [record for record in records if record["type"] != "image"]
        if images:
            grouped["uploaded_images"] = images
        if documents:
            grouped["uploaded_documents"] = documents
        return grouped

    async def _report_download_failures(
        self, channel_id: str, thread_ts: str, failures: list[tuple[str, str]]
    ) -> None:
        """Post one notice per distinct reason, not one per file."""
        grouped: dict[str, list[str]] = {}
        for name, reason in failures:
            grouped.setdefault(reason, []).append(name)
        for reason, names in grouped.items():
            await self._report_unreadable_files(channel_id, thread_ts, names, reason)

    async def _report_unreadable_files(
        self,
        channel_id: str,
        thread_ts: str,
        names: list[str],
        reason: str,
    ) -> None:
        """Say that attachments arrived and were not read.

        Deliberately unconditional on ``acknowledge_mode``: this is the reply to
        a request the bot accepted and could only partly serve, not a courtesy
        acknowledgement, and ``off`` should not restore the silence.

        Best-effort like the acknowledgement itself. A channel this bot cannot
        post into must not turn a notice about an attachment into a failure that
        also loses the message text next to it.
        """
        if self._client is None or not channel_id or not names:
            return

        shown = names[:_FILE_NOTICE_MAX_NAMES]
        listed = ", ".join(shown)
        hidden = len(names) - len(shown)
        if hidden > 0:
            listed = f"{listed} (+{hidden} more)"
        singular = len(names) == 1
        text = (
            f"Received {len(names)} "
            f"{'attachment' if singular else 'attachments'} ({listed}) but could"
            f" not read {'it' if singular else 'them'}: {reason}."
        )

        kwargs: dict[str, Any] = {"channel": channel_id, "text": text}
        if thread_ts:
            kwargs["thread_ts"] = thread_ts
        try:
            await self._client.chat_postMessage(**kwargs)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Slack attachment notice failed: %s", exc)

    async def _remember_event(self, event_id: str) -> bool:
        """True if this message is new, False if it has already been handled.

        Backed by a file, so the answer survives a restart -- Slack keeps
        retrying an event it thinks went unacked, and a process-local window
        forgets everything exactly when a deploy makes those retries arrive.
        """
        return await self._dedup.remember(event_id)

    @staticmethod
    def resolve_delivery(
        *,
        delivery: Any = None,
        metadata: Mapping[str, Any] | None = None,
        session_id: Any = None,
        default_channel_id: Any = "",
    ) -> tuple[str, str]:
        """The delivery ladder: which channel an outbound message goes to.

        Four rungs, first non-empty answer wins:

        1. a ``SlackDeliveryTarget`` on the routing target -- the connector's own
           answer, passed alongside the message;
        2. ``slack_channel_id`` in the message metadata, which is what an inbound
           turn stamps and what a cron run's Slack context holds;
        3. the session id, when it is a Slack session naming a conversation;
        4. ``channels.slack.default_channel_id``, the operator's blanket
           fallback, empty by default.

        Written as arguments rather than read off a ``Message`` so that a
        *producer* -- a cron job being written, a heartbeat being configured --
        can ask the same question before there is a message to ask it about. A
        producer holds only rungs 3 and 4, and that is precisely the case
        ``delivery_is_reachable`` exists to warn about. Keeping both the send
        path and the warnings on this one function is the point: a second
        implementation of the order would be a second answer to "where does this
        go", and the warning would go quiet exactly when the ladder changed.
        """
        meta = metadata or {}
        if isinstance(delivery, SlackDeliveryTarget):
            channel_id = str(delivery.target_channel_id or "").strip()
            if channel_id:
                return channel_id, str(delivery.thread_ts or "").strip()

        channel_id = str(meta.get("slack_channel_id") or "").strip()
        if channel_id:
            return channel_id, str(meta.get("slack_thread_ts") or "").strip()

        # Parsed by the same helper the cron history gate uses, so a job cannot
        # deliver into one conversation while reading another: two parsers that
        # could drift apart would be two answers to "which channel is this".
        channel_id, parsed_thread = parse_slack_cron_session(session_id)
        if channel_id:
            return channel_id, parsed_thread

        return str(default_channel_id or "").strip(), ""

    @staticmethod
    def delivery_is_reachable(
        *,
        session_id: Any = None,
        default_channel_id: Any = "",
        metadata: Mapping[str, Any] | None = None,
        delivery: Any = None,
    ) -> bool:
        """Whether these fields resolve to a channel, i.e. can be delivered at all.

        The predicate behind every "this will fail at delivery" warning. It runs
        the real ladder rather than restating its conclusions, so a producer's
        verdict and the send path's behaviour cannot disagree; the arguments
        default to "absent" because the producers asking have neither a routing
        target nor metadata, and a heartbeat has no session either.

        False here does not mean the send path raises today -- it means the only
        thing that could still save it is a rung a producer cannot supply.
        """
        channel_id, _thread_ts = SlackChannel.resolve_delivery(
            delivery=delivery,
            metadata=metadata,
            session_id=session_id,
            default_channel_id=default_channel_id,
        )
        return bool(channel_id)

    def _extract_delivery(
        self,
        msg: Message,
        routing_target: RoutingTarget | None,
    ) -> tuple[str, str]:
        metadata = msg.metadata or {}
        channel_id, thread_ts = SlackChannel.resolve_delivery(
            delivery=routing_target.delivery if routing_target is not None else None,
            metadata=metadata,
            session_id=msg.session_id,
            default_channel_id=self.config.default_channel_id,
        )
        # Applied once here rather than at each rung: every rung blanked the
        # thread under ``post_as_root`` and the last one never had one, so this
        # is the same answer expressed in the one place that depends on a
        # ``Message``. The ladder itself stays free of it, which is what lets a
        # producer with no message call it.
        if bool(metadata.get(POST_AS_ROOT_KEY)):
            return channel_id, ""
        return channel_id, thread_ts

    def _require_delivery(
        self,
        msg: Message,
        routing_target: RoutingTarget | None,
    ) -> tuple[str, str]:
        """``_extract_delivery``, refusing the delivery when no channel resolved.

        The three outbound paths -- text, files and questions -- all raise the
        same refusal, and its wording is not a summary: it names the four rungs
        ``resolve_delivery`` tries, in the order it tries them, because that
        list is what an operator needs in order to know where to put the answer.
        Written out three times, adding or renaming a rung meant editing the
        message in three places, and a rung reachable in one path and unnamed in
        the other two would leave two of them describing a ladder that no longer
        exists.
        """
        channel_id, thread_ts = self._extract_delivery(msg, routing_target)
        if not channel_id:
            raise SlackDeliveryError(
                "no target channel resolved from the routing target, message "
                "metadata, session id, or channels.slack.default_channel_id"
            )
        return channel_id, thread_ts

    @staticmethod
    def _extract_outgoing_text(msg: Message) -> str:
        payload = getattr(msg, "payload", None) or {}
        if msg.event_type == EventType.HEALTH_CHECK_RELAY and isinstance(payload, dict):
            health_check = payload.get("health_check")
            if health_check:
                return str(health_check).strip()

        if isinstance(payload, dict):
            if "content" in payload:
                content = payload.get("content")
                if isinstance(content, dict):
                    return str(content.get("output", content)).strip()
                return str(content or "").strip()
            if payload.get("error"):
                return str(payload.get("error")).strip()
        if msg.params and "content" in msg.params:
            return str(msg.params.get("content") or "").strip()
        if isinstance(msg.payload, str):
            return msg.payload.strip()
        return ""

    @staticmethod
    def _split_text(content: str, first_limit: int = 0) -> list[str]:
        """Split *content* into chunks Slack will accept.

        The implementation is shared with the agent runtime's posting tools and
        lives in ``common/slack_text``; this name is kept because every caller
        in this file reads as a method of the channel.
        """
        return split_text(content, first_limit)

    @staticmethod
    def _preferred_split_index(content: str, limit: int) -> int:
        """Kept as a name; the implementation is shared with ``common/slack_text``."""
        return preferred_split_index(content, limit)

    def _blocks_for(
        self, text: str, requested: bool | None
    ) -> list[dict[str, Any]] | None:
        """Return the Block Kit rendering of one chunk, or ``None`` for plain text.

        Decided per chunk rather than per reply because a chunk is what a Slack
        message holds, and the limits blocks have to fit are per message. A reply
        long enough to be split can therefore have a table rendered in the chunk
        that contains it and be posted as ordinary text everywhere else.

        The three gates are cheap and ordered so the untouched path costs
        nothing: with the default mode and a reply that said nothing, this
        returns before the renderer is even called, and ``_post_text`` receives
        the same arguments it received before blocks existed.

        ``text`` is already-normalised mrkdwn by the time it arrives here, which
        is what lets the renderer reuse the normaliser rather than reimplement
        it: headings, bold, bullets and links have been converted once, and only
        the table structure is built from scratch.
        """
        return self._blocks_and_kind_for(text, requested)[0]

    def _blocks_and_kind_for(
        self, text: str, requested: bool | None
    ) -> tuple[list[dict[str, Any]] | None, str]:
        """``_blocks_for``, plus what the renderer built the blocks out of.

        The kind is passed from here to ``_post_text`` rather than worked out
        there, because this is the last point at which anyone knows. One chunk
        can hold a Markdown table and a hand-written ```blockkit fence at once,
        and the finished list of blocks does not say which of them came from
        which -- see ``slack_blocks.combine_block_kinds`` for how one message
        holding both is treated.
        """
        mode = self._blockkit_tables_mode()
        if mode == BLOCKKIT_TABLES_OFF or requested is False:
            return None, slack_blocks.BLOCK_KIND_UNKNOWN
        if requested is not True and mode != BLOCKKIT_TABLES_AUTO:
            return None, slack_blocks.BLOCK_KIND_UNKNOWN
        rendered = slack_blocks.render_blocks_with_kind(
            text,
            requested=requested is True,
            render_tables=self._render_tables_mode(),
            allowed_block_types=self._blockkit_allowed_block_types(),
            allow_interactive=self._blockkit_allow_interactive(),
        )
        if rendered is None:
            return None, slack_blocks.BLOCK_KIND_UNKNOWN
        return rendered

    def _streaming_mode(self) -> str:
        """The effective streaming mode, re-validated the way the others are.

        ``self.config.enable_streaming`` normally arrives already settled by
        ``resolve_streaming_mode`` on the gateway side, but a config built
        directly -- a test, or a caller that skips the gateway -- can hand this
        a raw boolean or an unknown word, so it is resolved again here rather
        than trusted. Both readings agree by construction: there is one
        resolver.
        """
        return resolve_streaming_mode(self.config.enable_streaming)

    def _streaming_enabled(self) -> bool:
        """Whether a reply is previewed at all, in either mode.

        The question most callers are asking: a streamed turn is what produces
        the deltas, the progress events and the interrupts, and all three are
        about there being a preview rather than about how it is written.
        """
        return self._streaming_mode() != STREAMING_OFF

    def _blockkit_tables_mode(self) -> str:
        mode = str(self.config.blockkit_tables or "").strip().lower()
        if mode in BLOCKKIT_TABLES_MODES:
            return mode
        if mode:
            logger.warning(
                "channels.slack.blockkit_tables=%r is not one of %s; using %r",
                self.config.blockkit_tables,
                "/".join(BLOCKKIT_TABLES_MODES),
                BLOCKKIT_TABLES_DEFAULT,
            )
        return BLOCKKIT_TABLES_DEFAULT

    def _render_tables_mode(self) -> str:
        """The effective table rendering, re-validated the way the others are.

        Routed through the same resolver the gateway uses rather than
        reimplementing its reading, so a config built directly -- a test, or a
        caller that skips the gateway -- cannot be checked more loosely than one
        read from disk, and so the deprecated threshold is honoured on exactly
        one code path.
        """
        return resolve_render_tables({"render_tables": self.config.render_tables})

    def _blockkit_allowed_block_types(self) -> tuple[str, ...]:
        """The effective block-type allow-list, re-validated the way the others are.

        Routed through the same resolver the gateway uses rather than
        reimplementing its cleanup, so a config built directly -- a test, or a
        caller that skips the gateway -- cannot be checked more loosely than one
        read from disk.
        """
        return resolve_blockkit_allowed_block_types(
            {"blockkit_allowed_block_types": self.config.blockkit_allowed_block_types}
        )

    def _blockkit_allow_interactive(self) -> bool:
        """Whether hand-written interactive elements are allowed, re-validated."""
        return resolve_blockkit_allow_interactive(
            {"blockkit_allow_interactive": self.config.blockkit_allow_interactive}
        )

    def _blockkit_validate_mode(self) -> str:
        """The effective validation policy, re-validated the way the others are."""
        return resolve_blockkit_validate(
            {"blockkit_validate": self.config.blockkit_validate}
        )

    @staticmethod
    def _extract_block_request(content: str) -> tuple[str, bool | None]:
        """Read the reply's own decision about Block Kit, and remove it from the text.

        Returns the content with both markers gone and one of three answers:
        ``True`` asked for blocks, ``False`` refused them, ``None`` said nothing.
        Only ``None`` leaves the mode in ``channels.slack.blockkit_tables`` to
        decide, which is what makes this a per-message choice sitting inside an
        operator-level policy rather than either one on its own.

        A refusal wins over a request when a reply somehow holds both: the
        marker that suppresses structure is the safe reading of a contradiction.

        The content is returned untouched, byte for byte, when neither marker is
        present. Stripping is the exception rather than the rule here precisely so
        that an ordinary answer cannot be reshaped by a feature it never used.
        """
        requested: bool | None = None
        cleaned = content
        if _SLACK_BLOCKS_OFF_MARKER in cleaned:
            requested = False
            cleaned = cleaned.replace(_SLACK_BLOCKS_OFF_MARKER, "")
        if _SLACK_BLOCKS_MARKER in cleaned:
            if requested is None:
                requested = True
            cleaned = cleaned.replace(_SLACK_BLOCKS_MARKER, "")
        if requested is None:
            return content, None
        # A marker on a line of its own leaves an empty line behind it, which
        # would otherwise show up as a gap the author did not write.
        return _BLANK_RUN_RE.sub("\n\n", cleaned).strip(), requested

    @staticmethod
    def _split_threaded_report(content: str) -> list[str]:
        """Cut a reply into the root message and its thread replies.

        Every marker is a boundary, so a reply holding two of them is a root
        and two replies rather than a root and one. The marker used to split on
        its first occurrence with the rest stripped, which capped a report at one
        reply; the author had no way to say where a second one should begin, and
        the sections that would have been separate messages shared one message's
        Block Kit budget instead. Slack counts table rows, blocks and table
        characters per message, so one over-budget section degraded every table
        beside it to raw pipe text. Separate messages are separate budgets.

        Empty pieces are dropped rather than posted, which is what makes a
        section that renders to nothing on a given run cost a boundary and not a
        blank message: a report whose tables are all empty is a root and the
        prose, still in order.

        Returns a single element -- the whole content with every marker removed
        -- whenever fewer than two pieces survive. That covers a marker with
        nothing above it as well as one with nothing below: there is no root to
        thread under, so the reply is posted the way it would have been without
        the marker at all.
        """
        if _SLACK_THREAD_DETAILS_MARKER not in content:
            return [content]
        pieces = [
            piece.strip() for piece in content.split(_SLACK_THREAD_DETAILS_MARKER)
        ]
        pieces = [piece for piece in pieces if piece]
        if len(pieces) < 2:
            return [content.replace(_SLACK_THREAD_DETAILS_MARKER, "").strip()]
        return pieces

    @staticmethod
    def _normalize_slack_mrkdwn(content: str) -> str:
        """Convert a narrow Markdown subset to Slack mrkdwn safely.

        The implementation is shared with the agent runtime's posting tools and
        lives in ``common/slack_text``. It is what makes the promise those tools
        make true: a reply the connector posts and a message a model posts
        through ``post_message`` are put through one converter, so the same
        text cannot render two ways depending on which of them sent it.
        """
        return normalize_slack_mrkdwn(content)

    def get_metadata(self) -> ChannelMetadata:
        return ChannelMetadata(
            channel_id=self.channel_id,
            source="slack",
            extra={
                "default_channel_id": self.config.default_channel_id,
                "allowed_channel_ids": list(self.config.allowed_channel_ids),
                "history": self.config.history,
                "history_never_read": list(self.config.history_never_read),
                "history_exempt_members": list(self.config.history_exempt_members),
                KEY_WRITE: self.config.write,
                "reply_in_thread": self.config.reply_in_thread,
                # group_chat_mode stays the raw global string it has always
                # been. The resolved value is now per conversation and this key
                # is conversation-agnostic, so publishing a set here would
                # answer a question nobody asked it; conversation_overrides
                # holds the per-conversation picture alongside it instead.
                "group_chat_mode": self._group_chat_mode(),
                "conversation_overrides": {
                    channel_id: {
                        key: value
                        for key, value in (
                            ("mode", sorted(override.mode) if override.mode is not None else None),
                            ("prompt", override.prompt),
                            ("model_name", override.model_name),
                            ("mid_turn", override.mid_turn),
                            ("session", override.session),
                            ("history", override.history),
                        )
                        if value is not None
                    }
                    for channel_id, override in (self.config.conversation_overrides or {}).items()
                },
                "acknowledge_mode": self._acknowledge_mode(),
                "acknowledgement_emoji": self.config.acknowledgement_emoji,
                "rejected_emoji": self.config.rejected_emoji,
                "queued_emoji": self.config.queued_emoji,
                "completed_emoji": self.config.completed_emoji,
                "failed_emoji": self.config.failed_emoji,
                "stopped_emoji": self.config.stopped_emoji,
                # Empty is a real setting here, not a missing one: it is how
                # the thread status is turned off.
                "thinking_status": self.config.thinking_status,
                # Siblings of thinking_status, drawn in the same thread slot
                # rather than a second one -- see _set_queued_status and
                # _set_steered_status. Neither has an off switch of its own;
                # blank is not a value either reaches, since both are loaded
                # with the ``or <literal>`` pattern the emoji keys use.
                "queued_status": self.config.queued_status,
                "steered_status": self.config.steered_status,
                # How many messages are being held right now, per session. A
                # count rather than the messages: what is queued is user text,
                # and a diagnostic snapshot is not the place for it. Absent
                # entirely when nothing is held, which is every deployment on
                # the default.
                "queued_messages": {
                    session_id: len(queue)
                    for session_id, queue in self._queued_messages.items()
                    if queue
                },
                # The key as the operator set it. Which path a given reply
                # takes is per reply and appears in the log line that opens it,
                # not here, where it would read as a setting.
                "enable_streaming": bool(self.config.enable_streaming),
                "blockkit_tables": self._blockkit_tables_mode(),
                "render_tables": self._render_tables_mode(),
                "blockkit_allowed_block_types": list(
                    self._blockkit_allowed_block_types()
                ),
                "blockkit_allow_interactive": self._blockkit_allow_interactive(),
                "blockkit_validate": self._blockkit_validate_mode(),
            },
        )
