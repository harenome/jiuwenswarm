# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""What this app asks Slack for, and which install tier each grant belongs to.

One table per kind of requirement -- the Slack methods this repository calls,
and the events its connector registers -- and a renderer that turns either into
an app manifest an operator pastes into Slack.

**Why a table and not a scan.** Almost nothing in this repository says out loud
which scope a call needs. Seven of the scopes -- ``app_mentions:read``, the four
``*:history`` scopes, ``reactions:read`` and ``pins:read`` -- appear nowhere in
the package at all, because the thing that needs them is Slack's willingness to
deliver an event rather than any line of code. Five more appear only inside a
comment or a sentence written for a person. A scanner reading the source would
find thirteen of twenty-five and report that as complete, which is worse than
not scanning. So the dependency is written down here, once, and
``tests/unit_tests/channel/test_slack_app_manifest.py`` makes the writing-down
compulsory: it finds every Slack call in the source and fails on any that this
table does not classify.

That is the property worth having. It does not make the manifest provably
complete -- see the module docstring of that test for what it cannot see -- but
it does mean a call added in six months stops CI until somebody says what it
needs, and saying so is what puts the scope into every manifest below.

**Why tiers.** A scope's absence is allowed. Every model-facing tool refuses
cleanly and by name when Slack answers ``missing_scope``, so an install that
grants less is a bot that can do less rather than a bot that breaks. Demanding
all twenty-five up front would make the smallest install larger than it needs to
be and would hand an operator a manifest their workspace may refuse over the
search family. So the grants are graded, and the operator picks a grade.

* ``TIER_CORE``  -- without it there is no bot: the events that reach it and the
  scope that lets it answer.
* ``TIER_CONNECTOR`` -- calls the connector makes on its own behalf, on a
  default config, with no model waiting on the result.
* ``TIER_TOOLS`` -- the model-facing tools, and connector features that ship off.

A scope's own tier is the lowest tier of anything that needs it.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping

TIER_CORE = 0
TIER_CONNECTOR = 1
TIER_TOOLS = 2

TIERS: tuple[int, ...] = (TIER_CORE, TIER_CONNECTOR, TIER_TOOLS)

#: The word each tier is named by in a filename and in the guides. Short,
#: because it is part of a path an operator types.
TIER_NAMES: Mapping[int, str] = MappingProxyType(
    {TIER_CORE: "core", TIER_CONNECTOR: "connector", TIER_TOOLS: "tools"}
)

TIER_SUMMARIES: Mapping[int, str] = MappingProxyType(
    {
        TIER_CORE: (
            "The smallest app that is a bot at all: it receives a mention or a"
            " direct message and answers it."
        ),
        TIER_CONNECTOR: (
            "Core, plus the three grants the connector spends on its own behalf"
            " on a default configuration: the acknowledgement reaction and"
            " files in both directions. This is the tier to install unless"
            " there is a reason not to."
        ),
        TIER_TOOLS: (
            "Everything, including the grants behind features that ship off."
            " Install this to decide feature by feature in config rather than"
            " by reinstalling the app."
        ),
    }
)

#: The four conversation kinds Slack scopes apart. ``event_chat_type`` in the
#: connector reads ``channel_type`` and passes Slack's own word for each --
#: channel, group, im, mpim -- straight to the scope matcher, so the connector
#: declares all four and a call that reads a conversation may be reading any of
#: them.
_HISTORY_SCOPES = ("channels:history", "groups:history", "im:history", "mpim:history")
_CONVERSATION_READ_SCOPES = ("channels:read", "groups:read", "im:read", "mpim:read")

#: One method, six grants. ``assistant.search.context`` serves four channel
#: kinds and two extra content types, and each is granted separately.
_SEARCH_SCOPES = (
    "search:read.public",
    "search:read.private",
    "search:read.im",
    "search:read.mpim",
    "search:read.files",
    "search:read.users",
)


@dataclass(frozen=True)
class Requirement:
    """What one call or one event needs, and which tier it belongs to."""

    #: The scopes, in the order the manifest should list them. Empty is a
    #: claim and not a gap: it says the call needs nothing beyond holding a bot
    #: token at all.
    scopes: tuple[str, ...]
    tier: int
    #: One clause, rendered into the manifest beside the scopes it justifies.
    why: str


#: Every Slack API method this repository calls.
#:
#: Keyed in the SDK's underscore spelling, which is also what a dotted name
#: normalises to: ``reactions.add`` and ``reactions_add`` are one entry, because
#: this repository writes a method both ways -- the SDK spelling where it calls
#: a wrapper, the dotted spelling where it goes through ``api_call`` because the
#: pinned SDK has no wrapper for the method.
METHOD_REQUIREMENTS: Mapping[str, Requirement] = MappingProxyType(
    {
        # ── core ────────────────────────────────────────────────────────────
        # Called once at startup to learn the bot's own user id and the
        # workspace's host. Needs no scope; a bot token alone answers it.
        "auth_test": Requirement((), TIER_CORE, "startup identity"),
        "chat_postMessage": Requirement(("chat:write",), TIER_CORE, "posting a reply"),
        "chat_update": Requirement(("chat:write",), TIER_CORE, "editing a reply"),
        "chat_delete": Requirement(("chat:write",), TIER_CORE, "withdrawing a message"),
        "chat_postEphemeral": Requirement(
            ("chat:write",), TIER_CORE, "a notice for one reader"
        ),
        "chat_startStream": Requirement(
            ("chat:write",), TIER_CORE, "opening a streamed reply"
        ),
        "chat_appendStream": Requirement(
            ("chat:write",), TIER_CORE, "extending a streamed reply"
        ),
        "chat_stopStream": Requirement(
            ("chat:write",), TIER_CORE, "closing a streamed reply"
        ),
        # ── connector, default on ───────────────────────────────────────────
        # The thread status line. Chosen over agents.sessions.setStatus
        # precisely because chat:write is enough for it, so it adds no grant.
        "assistant_threads_setStatus": Requirement(
            ("chat:write",), TIER_CONNECTOR, "the thread status line"
        ),
        # A linting endpoint for a payload the caller already holds. It reads
        # nothing belonging to the workspace and needs no scope.
        "blocks_validate": Requirement(
            (), TIER_CONNECTOR, "checking a Block Kit payload before sending it"
        ),
        "reactions_add": Requirement(
            ("reactions:write",), TIER_CONNECTOR, "the acknowledgement mark"
        ),
        "reactions_remove": Requirement(
            ("reactions:write",), TIER_CONNECTOR, "clearing the acknowledgement mark"
        ),
        "files_info": Requirement(
            ("files:read",), TIER_CONNECTOR, "resolving an inbound attachment"
        ),
        "files_upload_v2": Requirement(
            ("files:write",), TIER_CONNECTOR, "sending a file"
        ),
        # ── tools, and connector features that ship off ─────────────────────
        # Reached by the history toolkit, and by the group digital avatar,
        # which ships off. All four kinds, for the reason _HISTORY_SCOPES
        # gives.
        "conversations_history": Requirement(
            _HISTORY_SCOPES, TIER_TOOLS, "reading a conversation"
        ),
        "conversations_replies": Requirement(
            _HISTORY_SCOPES, TIER_TOOLS, "reading a thread"
        ),
        "conversations_info": Requirement(
            _CONVERSATION_READ_SCOPES, TIER_TOOLS, "telling one conversation kind from another"
        ),
        "conversations_members": Requirement(
            _CONVERSATION_READ_SCOPES, TIER_TOOLS, "listing who is in a conversation"
        ),
        # Nothing opened a direct message before the posting tools did. A
        # chat_id naming a person rather than a room has no conversation behind
        # it yet, so one is opened and then posted into; Slack answers with the
        # existing D… where there already is one, so a second call costs
        # nothing and creates nothing.
        #
        # Both scopes, because one method covers both shapes: im:write opens a
        # direct message with one person, mpim:write opens one with several, and
        # the call is the same call. Granting only the first leaves a group
        # direct message refused by Slack with the scope named, which is the
        # degrade every other tool here gets.
        "conversations_open": Requirement(
            ("im:write", "mpim:write"), TIER_TOOLS, "opening a direct message"
        ),
        "pins_add": Requirement(("pins:write",), TIER_TOOLS, "pinning a message"),
        "pins_remove": Requirement(("pins:write",), TIER_TOOLS, "unpinning a message"),
        "pins_list": Requirement(("pins:read",), TIER_TOOLS, "reading what is pinned"),
        # The listing half and the writing half are separate grants, and an
        # edit calls both: it reads the bookmark before writing it back, so the
        # fields the edit does not name keep the values they have.
        "bookmarks_add": Requirement(
            ("bookmarks:write",), TIER_TOOLS, "adding a bookmark"
        ),
        "bookmarks_edit": Requirement(
            ("bookmarks:write",), TIER_TOOLS, "editing a bookmark"
        ),
        "bookmarks_remove": Requirement(
            ("bookmarks:write",), TIER_TOOLS, "removing a bookmark"
        ),
        "bookmarks_list": Requirement(
            ("bookmarks:read",), TIER_TOOLS, "reading the bookmark bar"
        ),
        # Two uses and they fail differently. Resolving an author's name
        # degrades: the history toolkit falls back to raw user ids and says so
        # in its own warnings. Deciding whether somebody could read a public
        # channel they are not in does not, and cannot -- that is a gate, and a
        # gate whose question was declined refuses. Only a deployment on
        # channels.slack.history: visible reaches the second use.
        "users_info": Requirement(
            ("users:read",),
            TIER_TOOLS,
            "turning user ids into names, and under history: visible deciding"
            " whether a member could read a public target themselves",
        ),
        # The four listing methods behind find_by_name, which runs the other
        # way round from users.info: a name in, an id out. Four methods and
        # four grants rather than one, because a workspace may well grant the
        # people directory and not the emoji list; the tool reports a refused
        # kind in its coverage block and searches the rest.
        "users_list": Requirement(
            ("users:read",), TIER_TOOLS, "finding a person by name"
        ),
        # channels:read and groups:read only, and not the other two members of
        # _CONVERSATION_READ_SCOPES. A direct message has no name, so it can
        # never answer a name query, and asking for im:read and mpim:read here
        # would widen the install for a page of results that cannot hold an
        # answer.
        "conversations_list": Requirement(
            ("channels:read", "groups:read"),
            TIER_TOOLS,
            "finding a conversation by name",
        ),
        "usergroups_list": Requirement(
            ("usergroups:read",), TIER_TOOLS, "finding a user group by name"
        ),
        # Without it nothing tells a model which custom emoji this workspace
        # has, and react_to_message is left guessing a shortcode and failing.
        "emoji_list": Requirement(
            ("emoji:read",), TIER_TOOLS, "naming this workspace's custom emoji"
        ),
        "assistant_search_context": Requirement(
            _SEARCH_SCOPES, TIER_TOOLS, "searching the workspace"
        ),
        # The App Home tab, which is a page of Block Kit this app publishes into
        # a private space it shares with one person. Needs no scope, which is
        # unusual: what gates it is features.app_home.home_tab_enabled, the
        # app's own configuration rather than a grant a workspace makes, and
        # Slack refuses the call with ``not_enabled`` while that flag is off. So
        # the entry puts no scope into any manifest; it exists because every
        # Slack call this repository makes has to be classified, and "no scope"
        # is a classification.
        "views_publish": Requirement(
            (), TIER_TOOLS, "publishing somebody's App Home tab"
        ),
        # The canvas toolkit. Two scopes and seven calls, and the split between
        # them is not the usual read/write one: canvases:read unlocks exactly
        # one method, and that method answers with section identifiers and no
        # text at all. Everything else about a canvas -- creating it, changing
        # it, sharing it, deleting it -- is canvases:write.
        #
        # Reading a canvas's *content* needs neither of them. It is an
        # authenticated fetch of the file's url_private, so what it needs is
        # files:read, which files.info already carries at the connector tier.
        # An install can therefore hold canvases:read and still not read a word
        # of a canvas, which is why the read tool reports the two halves
        # separately rather than failing as one.
        "canvases_sections_lookup": Requirement(
            ("canvases:read",), TIER_TOOLS, "listing a canvas's sections"
        ),
        "canvases_create": Requirement(
            ("canvases:write",), TIER_TOOLS, "creating a canvas"
        ),
        "conversations_canvases_create": Requirement(
            ("canvases:write",),
            TIER_TOOLS,
            "creating a conversation's own canvas",
        ),
        "canvases_edit": Requirement(
            ("canvases:write",), TIER_TOOLS, "changing what a canvas says"
        ),
        "canvases_access_set": Requirement(
            ("canvases:write",),
            TIER_TOOLS,
            "setting who may read or write a canvas",
        ),
        "canvases_access_delete": Requirement(
            ("canvases:write",), TIER_TOOLS, "taking canvas access away"
        ),
        "canvases_delete": Requirement(
            ("canvases:write",), TIER_TOOLS, "deleting a canvas"
        ),
        # The List toolkit. Two scopes and twelve calls, split the ordinary
        # read/write way -- reading rows and starting an export are lists:read,
        # everything that changes a List is lists:write.
        #
        # Deleting one is neither, and that is the entry below this block.
        # Slack has no method that deletes a List, so the tool goes through
        # files.delete, and what it spends is the files grant rather than the
        # Lists grant. An install can therefore hold both List scopes and still
        # be unable to delete a List, and hold neither and still be able to --
        # which is why the delete tool's own guard reads the file first rather
        # than trusting that holding a List scope means the id is a List's.
        "slackLists_items_list": Requirement(
            ("lists:read",), TIER_TOOLS, "reading a List's rows"
        ),
        "slackLists_items_info": Requirement(
            ("lists:read",), TIER_TOOLS, "reading one row, and the List's schema"
        ),
        "slackLists_download_start": Requirement(
            ("lists:read",), TIER_TOOLS, "starting an export of a List"
        ),
        "slackLists_download_get": Requirement(
            ("lists:read",), TIER_TOOLS, "collecting an export of a List"
        ),
        "slackLists_create": Requirement(
            ("lists:write",), TIER_TOOLS, "creating a List"
        ),
        "slackLists_update": Requirement(
            ("lists:write",), TIER_TOOLS, "renaming a List"
        ),
        "slackLists_items_create": Requirement(
            ("lists:write",), TIER_TOOLS, "adding a row to a List"
        ),
        "slackLists_items_update": Requirement(
            ("lists:write",), TIER_TOOLS, "writing cells in a List"
        ),
        "slackLists_items_delete": Requirement(
            ("lists:write",), TIER_TOOLS, "deleting one row of a List"
        ),
        "slackLists_items_deleteMultiple": Requirement(
            ("lists:write",), TIER_TOOLS, "deleting several rows of a List"
        ),
        "slackLists_access_set": Requirement(
            ("lists:write",), TIER_TOOLS, "setting who may read or write a List"
        ),
        "slackLists_access_delete": Requirement(
            ("lists:write",), TIER_TOOLS, "taking List access away"
        ),
        # The only call in this repository that destroys a file, and the reason
        # delete_slack_list reads before it writes: this method deletes any
        # file it is handed. files:write is already granted at the connector
        # tier for uploads, so classifying it here widens no install; what it
        # does is make the call visible to the scan, which is the point.
        "files_delete": Requirement(
            ("files:write",), TIER_TOOLS, "deleting a List, which is a file"
        ),
    }
)

#: Every event subscription the connector has a listener for.
#:
#: These are the requirements nothing in the source records, because what needs
#: them is Slack's willingness to deliver rather than a call. An absent one is
#: the quietest failure in the system: no call fails, nothing is logged, and the
#: bot is simply mute in the conversations that event would have come from.
EVENT_REQUIREMENTS: Mapping[str, Requirement] = MappingProxyType(
    {
        "app_mention": Requirement(
            ("app_mentions:read",), TIER_CORE, "being addressed in a channel"
        ),
        # Bolt registers one ``message`` listener and the connector routes on
        # ``channel_type``, so which of the four arrive is decided entirely by
        # the subscription list below.
        "message.channels": Requirement(
            ("channels:history",), TIER_CORE, "a message in a public channel"
        ),
        "message.im": Requirement(("im:history",), TIER_CORE, "a direct message"),
        "message.groups": Requirement(
            ("groups:history",), TIER_CORE, "a message in a private channel"
        ),
        "message.mpim": Requirement(
            ("mpim:history",), TIER_CORE, "a message in a group direct message"
        ),
        # The seven non-message events, registered from EVENT_TYPE_FAMILIES in
        # slack_events_policy. Their disposition ships as ``off``, so they are
        # opt-in however the app was installed.
        "reaction_added": Requirement(
            ("reactions:read",), TIER_TOOLS, "somebody reacted"
        ),
        "reaction_removed": Requirement(
            ("reactions:read",), TIER_TOOLS, "somebody took a reaction off"
        ),
        "pin_added": Requirement(("pins:read",), TIER_TOOLS, "somebody pinned"),
        "pin_removed": Requirement(("pins:read",), TIER_TOOLS, "somebody unpinned"),
        # A membership change is delivered per conversation kind, and the two
        # kinds a bot can be a member of are a public and a private channel.
        "member_joined_channel": Requirement(
            ("channels:read", "groups:read"), TIER_TOOLS, "somebody joined"
        ),
        "member_left_channel": Requirement(
            ("channels:read", "groups:read"), TIER_TOOLS, "somebody left"
        ),
        # The one entry in this table that asks for nothing. Slack delivers
        # app_home_opened to any app that has a bot user, so the only thing
        # standing between this connector and the event is the subscription
        # line itself -- which makes this the purest case of what this table's
        # docstring says about events: nothing in the source can be scanned to
        # find it, and its absence produces no failure anywhere, only silence.
        #
        # **Tier tools, and the empty scope list is not what decides it.** A
        # scope-free subscription could sit in any tier without widening a
        # single install, so the tier has to be argued from what the tier means:
        # core is what a bot needs to be a bot, connector is what the connector
        # spends on a default configuration, and tools is the model-facing work
        # plus the features that ship off. This ships off -- delivery.events
        # defaults to off for every family, app_home among them -- so a core or
        # connector install would carry a subscription it could never act on.
        # It sits with the other six opt-in event families for the same reason
        # they sit there, which also keeps one rule true of the whole table
        # rather than one rule with an exception for the cheap entry.
        "app_home_opened": Requirement(
            (), TIER_TOOLS, "somebody opened this app's App Home"
        ),
    }
)

#: How a method is spelled when it is not the SDK's own key with its first
#: underscore turned into a dot. Only the methods the pinned SDK has no wrapper
#: for need an entry: they are called through ``api_call`` with the dotted name
#: written out, and that is the name an operator looks up.
DOTTED_NAMES: Mapping[str, str] = MappingProxyType(
    {
        "assistant_threads_setStatus": "assistant.threads.setStatus",
        "assistant_search_context": "assistant.search.context",
        # Three of the canvas methods are two dots deep and a fourth is a
        # method on a nested family, so the first-underscore rule would spell
        # them canvases.access_set, canvases.sections_lookup and
        # conversations.canvases_create -- none of which an operator can look
        # up. They are written out for the same reason the two above are.
        "canvases_access_set": "canvases.access.set",
        "canvases_access_delete": "canvases.access.delete",
        "canvases_sections_lookup": "canvases.sections.lookup",
        "conversations_canvases_create": "conversations.canvases.create",
        # Ten of the twelve List methods are two dots deep, so the
        # first-underscore rule would spell them slackLists.items_list and
        # slackLists.access_set. The two that are one dot deep are written out
        # as well, for a different reason: the family is spelled slackLists
        # with a capital L, the documentation's own navigation calls it
        # "lists", and leaving the two to a rule would put one spelling of the
        # family in this table and another in the manifest beside it.
        "slackLists_create": "slackLists.create",
        "slackLists_update": "slackLists.update",
        "slackLists_access_set": "slackLists.access.set",
        "slackLists_access_delete": "slackLists.access.delete",
        "slackLists_items_create": "slackLists.items.create",
        "slackLists_items_info": "slackLists.items.info",
        "slackLists_items_list": "slackLists.items.list",
        "slackLists_items_update": "slackLists.items.update",
        "slackLists_items_delete": "slackLists.items.delete",
        "slackLists_items_deleteMultiple": "slackLists.items.deleteMultiple",
        "slackLists_download_start": "slackLists.download.start",
        "slackLists_download_get": "slackLists.download.get",
    }
)


def dotted(method: str) -> str:
    """One method as Slack's own documentation spells it."""
    return DOTTED_NAMES.get(method) or method.replace("_", ".", 1)


#: The subscriptions one bolt ``message`` listener stands for.
MESSAGE_SUBSCRIPTIONS: tuple[str, ...] = (
    "message.channels",
    "message.groups",
    "message.im",
    "message.mpim",
)

#: Scopes an operator may delete from the tier that carries them, with the
#: single sentence saying what deleting one costs. Rendered into the manifest
#: beside the scope, so the narrowing is offered where it is acted on rather
#: than only in a guide.
NARROWINGS: Mapping[str, str] = MappingProxyType(
    {
        "groups:history": (
            "delete this and message.groups to keep the bot out of private"
            " channels"
        ),
        "mpim:history": (
            "delete this and message.mpim to keep the bot out of group DMs"
        ),
        "search:read.files": (
            "if Slack refuses this manifest over the six search scopes, delete"
            " all six and leave channels.slack.search_enabled false"
        ),
    }
)


def scope_tiers() -> dict[str, int]:
    """Every scope, mapped to the lowest tier that needs it."""
    tiers: dict[str, int] = {}
    for requirement in (*METHOD_REQUIREMENTS.values(), *EVENT_REQUIREMENTS.values()):
        for scope in requirement.scopes:
            if scope not in tiers or requirement.tier < tiers[scope]:
                tiers[scope] = requirement.tier
    return tiers


def scope_reasons(tier: int = TIER_TOOLS) -> dict[str, list[str]]:
    """Every scope, mapped to what needs it, in table order.

    Derived rather than written out a second time, so the comment beside a
    scope in the manifest cannot come to disagree with the table above it.

    Bounded by ``tier``, because a reason has to be true of the manifest it is
    written in: ``channels:history`` is in the core manifest for
    ``message.channels`` and for nothing else, and naming
    ``conversations.history`` beside it there would point an operator at a tool
    that install cannot reach.
    """
    reasons: dict[str, list[str]] = {}
    for name, requirement in METHOD_REQUIREMENTS.items():
        if requirement.tier > tier:
            continue
        for scope in requirement.scopes:
            reasons.setdefault(scope, []).append(
                f"{dotted(name)} ({requirement.why})"
            )
    for name, requirement in EVENT_REQUIREMENTS.items():
        if requirement.tier > tier:
            continue
        for scope in requirement.scopes:
            reasons.setdefault(scope, []).append(f"{name} ({requirement.why})")
    return reasons


def scopes_for_tier(tier: int) -> list[str]:
    """The bot scopes an install at ``tier`` asks for, ordered by tier then name."""
    tiers = scope_tiers()
    chosen = [scope for scope, own in tiers.items() if own <= tier]
    return sorted(chosen, key=lambda scope: (tiers[scope], scope))


def events_for_tier(tier: int) -> list[str]:
    """The event subscriptions an install at ``tier`` asks for."""
    chosen = [
        name
        for name, requirement in EVENT_REQUIREMENTS.items()
        if requirement.tier <= tier
    ]
    return sorted(chosen, key=lambda name: (EVENT_REQUIREMENTS[name].tier, name))


def manifest_filename(tier: int) -> str:
    """The name the manifest for ``tier`` is shipped under."""
    return f"slack-app-manifest-{tier}-{TIER_NAMES[tier]}.yaml"


def _wrap(text: str, width: int, indent: str, cont: "str | None" = None) -> list[str]:
    """Comment lines for one sentence, wrapped without a dependency.

    ``cont`` is what a continuation line starts with. It defaults to ``indent``,
    which is right for a plain comment and wrong for one that opens with a label
    -- repeating "optional:" on every line of a two-line note would read as two
    notes.
    """
    cont = indent if cont is None else cont
    words = text.split()
    lines: list[str] = []
    current = ""
    start = indent
    for word in words:
        candidate = f"{current} {word}" if current else f"{start}{word}"
        if len(candidate) > width and current:
            lines.append(current)
            start = cont
            current = f"{cont}{word}"
        else:
            current = candidate
    if current:
        lines.append(current)
    return lines


_APP_NAME = "JiuwenSwarm"
_DESCRIPTION = "JiuwenSwarm agent runtime, connected over Socket Mode."
_LONG_DESCRIPTION = (
    "JiuwenSwarm connects an agent runtime to this workspace over Socket"
    " Mode, so no public callback URL is needed. It answers mentions and"
    " direct messages, streams long replies, and posts questions and"
    " approvals as buttons.\n\nIt is installed per workspace from a manifest"
    " and configured with a bot token and an app-level token pasted into the"
    " operator's own configuration file. It is not distributed, and it is not"
    " listed in the Slack Marketplace."
)


def render_manifest(tier: int) -> str:
    """The manifest for ``tier``, as the text that is committed and pasted.

    Rendered by hand rather than dumped, because the comments are the point: a
    scope list with no reason beside each entry is exactly the artefact that
    drifted, and an operator deciding whether to narrow an install needs to
    read what a line is for at the moment they are looking at it.
    """
    if tier not in TIER_NAMES:
        raise ValueError(f"no such tier: {tier!r}")

    reasons = scope_reasons(tier)
    tiers = scope_tiers()
    out: list[str] = []

    out.append(f"# Slack app manifest for {_APP_NAME} -- tier {tier}, {TIER_NAMES[tier]}.")
    out.append("#")
    out.extend(_wrap(TIER_SUMMARIES[tier], 79, "# "))
    out.append("#")
    out.extend(
        _wrap(
            "GENERATED FILE. Edit jiuwenswarm/common/slack_scope_policy.py and run"
            " scripts/generate_slack_manifest.py. A test regenerates this file and"
            " fails on any difference, and a second test fails when the code calls"
            " a Slack method the policy module does not classify.",
            79,
            "# ",
        )
    )
    out.append("#")
    out.extend(
        _wrap(
            "TO INSTALL: https://api.slack.com/apps -> Create New App -> From a"
            " manifest, pick the workspace, paste this file. Two things no"
            " manifest field carries stay manual afterwards: an App-Level Token"
            " carrying connections:write, minted under Basic Information, and the"
            " install itself, which returns the bot token.",
            79,
            "# ",
        )
    )
    out.append("_metadata:")
    out.append("  major_version: 1")
    out.append("  minor_version: 1")
    out.append("")
    out.append("display_information:")
    out.append(f"  name: {_APP_NAME}")
    out.append(f"  description: {_DESCRIPTION}")
    out.append('  background_color: "#1f2937"')
    out.append("  long_description: |-")
    for paragraph in _LONG_DESCRIPTION.split("\n"):
        out.append(f"    {paragraph}" if paragraph else "")
    out.append("")
    out.append("features:")
    out.append("  bot_user:")
    out.append(f"    display_name: {_APP_NAME}")
    out.append("    always_online: true")
    out.append("  app_home:")
    # Enabled so the app has a Home tab to publish into. Nothing publishes one
    # yet, so a member who opens it sees Slack's empty state. The alternative,
    # leaving it off until there is a view, costs a second reinstall for
    # whoever turns it on later: this is a manifest feature rather than a
    # scope, so it takes effect on the app's own configuration without one.
    out.append("    home_tab_enabled: true")
    out.append("    messages_tab_enabled: true")
    out.append("    messages_tab_read_only_enabled: false")
    out.append("")
    out.append("oauth_config:")
    out.append("  scopes:")
    out.append("    bot:")
    current_tier = -1
    for scope in scopes_for_tier(tier):
        if tiers[scope] != current_tier:
            current_tier = tiers[scope]
            out.append(
                f"      # tier {current_tier}, {TIER_NAMES[current_tier]}"
            )
        out.append(f"      - {scope}")
        out.extend(_wrap("needed by " + "; ".join(reasons[scope]), 79, "        # "))
        narrowing = NARROWINGS.get(scope)
        if narrowing:
            out.extend(
                _wrap(narrowing, 79, "        # optional: ", "        #   ")
            )
    out.append("")
    out.append("settings:")
    out.append("  event_subscriptions:")
    out.extend(
        _wrap(
            "No request_url: Socket Mode delivers these over the WebSocket the"
            " connector opens with the app-level token.",
            79,
            "    # ",
        )
    )
    out.append("    bot_events:")
    for event in events_for_tier(tier):
        requirement = EVENT_REQUIREMENTS[event]
        out.append(f"      - {event}")
        # An empty scope list is a claim rather than a gap -- see Requirement --
        # so it is rendered as one. "needs " with nothing after it would read as
        # a line the generator failed to finish, and an operator deciding what
        # to narrow needs to be told that this one costs no grant at all.
        needs = (
            f"needs {', '.join(requirement.scopes)}"
            if requirement.scopes
            else "needs no scope: Slack delivers it to any app with a bot user"
        )
        out.extend(_wrap(f"{requirement.why}; {needs}", 79, "        # "))
    out.append("")
    out.append("  interactivity:")
    out.extend(
        _wrap(
            "The question, approval and stop buttons send block_actions payloads."
            " With Socket Mode on those arrive on the same WebSocket as the events"
            " above, so no request_url is needed. Turned off, the buttons render"
            " and every click is discarded.",
            79,
            "    # ",
        )
    )
    out.append("    is_enabled: true")
    out.append("")
    out.append("  socket_mode_enabled: true")
    out.append("")
    out.extend(
        _wrap(
            "Not distributed. This app is created per workspace from this manifest"
            " and installed by its own operator; it is not submitted to the Slack"
            " Marketplace and has no OAuth redirect flow.",
            79,
            "  # ",
        )
    )
    out.append("  org_deploy_enabled: false")
    out.append("  token_rotation_enabled: false")
    return "\n".join(out) + "\n"


__all__ = [
    "EVENT_REQUIREMENTS",
    "MESSAGE_SUBSCRIPTIONS",
    "METHOD_REQUIREMENTS",
    "NARROWINGS",
    "Requirement",
    "TIERS",
    "TIER_CONNECTOR",
    "TIER_CORE",
    "TIER_NAMES",
    "TIER_SUMMARIES",
    "TIER_TOOLS",
    "events_for_tier",
    "manifest_filename",
    "render_manifest",
    "scope_reasons",
    "scope_tiers",
    "scopes_for_tier",
]
