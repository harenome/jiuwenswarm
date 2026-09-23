# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.

"""Reading the ``scopes:`` list, and saying what is wrong with it.

``scopes`` is a top-level list of rules about *requests* -- which conversation,
on which platform -- as against ``channels.<platform>``, which holds properties
of the connector. Each entry pairs a ``match`` with one or more sections:

.. code-block:: yaml

    scopes:
      - match:    {channel: slack, chat: C000000AAAA}
        delivery: {mode: [+has_file], prompt_append: "Be terse."}
        agent:    {model_name: "deepseek-v3"}

Loose YAML in, frozen dataclasses out, compiled once at load. That shape is
lifted from ``file_guard.paths``, which is the better-engineered of the two
list-of-rules-with-a-matcher mechanisms already in the tree.

**One name collides and must not be confused with the other.**
``file_guard.paths[].match`` is the match *kind* -- the string ``"prefix"`` or
``"glob"``, saying how ``path`` should be read. A scope's ``match`` is the
criteria themselves. Nothing here is a "match kind" and nothing there is a
criteria block, though both are spelled ``match``.

**Nothing in this module raises, and nothing it is given can fail a load.**
Config is re-read on reload rather than only at boot, so an exception on a
malformed entry would take down every correctly configured scope, and the
connector with them, over one typo. Every failure below warns, keeps going, and
leaves the affected scope or key on a defensible default.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Mapping, Sequence

from jiuwenswarm.common.scopes.capabilities import (
    ANY_KEY,
    AxisRestriction,
    Warn,
    channel_capabilities,
    known_channels,
    restriction_in,
)
from jiuwenswarm.common.scopes.people import (
    EMPTY_DIRECTORY,
    PEOPLE_KEY,
    ROLES_KEY,
    PeopleDirectory,
    compile_people,
)

logger = logging.getLogger(__name__)

AXIS_CHANNEL = "channel"
AXIS_WORKSPACE = "workspace"
AXIS_CHAT = "chat"
AXIS_CHAT_TYPE = "chat_type"
AXIS_USER = "user"
AXIS_ROLE = "role"

#: The key that inverts the identity axis. Not an axis itself: ``not`` names no
#: dimension of its own, it removes people from the one ``user`` and ``role``
#: name together, which is why it is listed apart from :data:`SUPPORTED_AXES`
#: and why it never adds a layer of its own (see :class:`ScopeMatch`).
MATCH_NOT = "not"

#: The axes the matcher understands. ``user`` reads the sender; ``role`` is the
#: same axis spelled as a named set of people, resolved against ``people:`` and
#: ``roles:`` at load into the very ids ``user`` would have enumerated.
#: ``chat_type`` reads the *kind* of conversation -- a direct message, a
#: channel -- from a vocabulary each platform declares for itself, and takes one
#: kind or a list of them, which OR. ``workspace`` reads which *installation* of
#: a platform the request arrived from -- a Slack ``team_id`` -- for a
#: deployment whose connector is in more than one.
#:
#: Listed coarsest first, which is the order :attr:`ScopeMatch.specificity`
#: compares them in and the order a warning lists them in.
SUPPORTED_AXES: tuple[str, ...] = (
    AXIS_CHANNEL,
    AXIS_WORKSPACE,
    AXIS_CHAT,
    AXIS_CHAT_TYPE,
    AXIS_USER,
    AXIS_ROLE,
)

#: Everything a ``match:`` block may legally hold -- the axes, plus ``not``.
SUPPORTED_MATCH_KEYS: tuple[str, ...] = (*SUPPORTED_AXES, MATCH_NOT)

#: The axes that name *who is asking*. They are one axis and they OR: a role
#: resolves to a set of the same ``(platform, id)`` pairs ``user`` enumerates,
#: so ``role`` is sugar over ``user`` rather than a second dimension, and
#: ``{user: [U1], role: admin}`` means U1 *or* an admin. AND would mean "U1, but
#: only if also an admin", which nobody wants and which a config author would
#: write by accident.
IDENTITY_AXES: tuple[str, ...] = (AXIS_USER, AXIS_ROLE)

#: The axes that name *which conversation*, and the pair a rule may not write
#: together. ``chat`` names one conversation, whose type is already settled by
#: the platform; adding ``chat_type`` to it is redundant where the two agree and
#: contradictory where they do not, and the contradictory rule is silently inert
#: rather than wrong in a way anything shows. It is refused at load, by name.
#:
#: **Refused rather than reconciled.** Checking that a given id really is of a
#: given type would mean asking the platform, at config-apply time, about a
#: conversation that may not exist, may not be one the bot is in, and may be
#: created later. The pair is structurally redundant, which is a fact about the
#: two keys and needs no lookup to establish.
#:
#: ``chat_type`` may name several kinds and ``chat`` may not name several
#: conversations, and that is the difference between an axis naming a class and
#: an axis naming a thing: "every channel-like conversation" is one instruction
#: about a set of kinds, while two conversation ids are two rooms that can each
#: be told something different. Naming a set of kinds does not make the pair any
#: less redundant, so the refusal above is unchanged by it.
CONVERSATION_AXES: tuple[str, ...] = (AXIS_CHAT, AXIS_CHAT_TYPE)

#: What ``not:`` may name in this version. Identity only: ``not: {chat: [...]}``
#: raises a question that need not be answered yet -- whether
#: ``not: {chat: A, user: B}`` means *not (A and B)* or *(not A) and (not B)* --
#: and channel lists are short enough to write out. The syntax below stays
#: forward-compatible with either reading.
#:
#: Both identity spellings are in it, and they union rather than replace one
#: another: ``not: {user: [U1], role: admin}`` excludes U1 *and* the admins,
#: which is the same OR the positive half performs.
NOT_AXES: tuple[str, ...] = (AXIS_USER, AXIS_ROLE)

#: Declared but not read by the matcher. Named separately so an author who
#: writes one is told it is deferred rather than told it is a typo, and kept
#: as a name now that it is empty so an axis added ahead of its reader still has
#: somewhere to be declared inert.
#:
#: Empty since ``role`` left it: every axis the schema names is now read.
DEFERRED_AXES: tuple[str, ...] = ()

SECTION_DELIVERY = "delivery"
SECTION_AGENT = "agent"
SECTION_PERMISSIONS = "permissions"
SECTION_CLICKS = "clicks"

CLICK_APPROVE = "approve"
CLICK_STOP = "stop"

LEVEL_ALLOW = "allow"
LEVEL_ASK = "ask"
LEVEL_DENY = "deny"

#: The three words a ``permissions.tools`` entry may use, in widening order.
#: The same vocabulary the permission engine parses, spelled here so a typo is
#: caught at load rather than at the tool call it was written to stop.
PERMISSION_LEVELS: tuple[str, ...] = (LEVEL_ALLOW, LEVEL_ASK, LEVEL_DENY)

KEY_MID_TURN = "mid_turn"

MID_TURN_CANCEL = "cancel"
MID_TURN_STEER = "steer"
MID_TURN_QUEUE = "queue"

#: What ``delivery.mid_turn`` may say a mid-turn message does, and the one
#: vocabulary in this module that is *not* a connector's to define. ``mode``
#: names Slack triggers and ``model_name`` names configured models, so both are
#: checked through the channel's own declaration; these three name mechanisms
#: any connector implementing the key implements the same way -- cancel the
#: running turn, join it, or wait for it -- so spelling them per connector would
#: be three copies of one word list and three chances for one of them to drift.
#:
#: ``queue`` is first because it is the default, which every deployment that
#: writes nothing gets: a message arriving mid-turn waits, and runs when the turn
#: it arrived during ends.
#:
#: ``cancel`` held that place until it was read as what it is -- a description of
#: what the connector did before this key existed, rather than a choice anyone
#: made. An ordinary send has always finished the running stream, so "unset"
#: inherited that and called it a default. What it does is destroy work nobody
#: asked it to destroy: the commonest way to meet it is a follow-up killing the
#: turn it was meant to add to, and in a conversation more than one person can
#: type into, the turn it kills belongs to somebody else. ``queue`` is the only
#: one of the three where a message from a second sender cannot reach into a
#: turn that is already running.
#:
#: The three are not grammatically parallel, deliberately. ``cancel`` and
#: ``steer`` act on the turn already running; ``queue`` acts on the message that
#: just arrived. Each names a mechanism a reader can grep for, which is worth
#: more than the symmetry -- ``join``/``wait`` were considered and rejected
#: because in concurrency vocabulary ``join`` *means* wait, so ``join`` would
#: read as a synonym for ``wait`` while doing the opposite.
#:
#: ``follow_up`` is deliberately absent. It exists in the runtime, but its answer
#: is emitted on the *first* request's stream under the first request's id, so a
#: connector whose stream bookkeeping is request-id-shaped would have to
#: demultiplex one stream holding two answers -- and a follow-up round is
#: genuinely its author's work, which makes a single-valued record of who may
#: stop a turn wrong. ``queue`` reaches the same place by giving each message its
#: own turn, its own id and its own owner.
MID_TURN_VALUES: tuple[str, ...] = (MID_TURN_QUEUE, MID_TURN_CANCEL, MID_TURN_STEER)

#: What a conversation does when no layer settled ``mid_turn``. Named here, beside
#: the values, so that the connector reading it and the list declaring it cannot
#: disagree about which one it is.
MID_TURN_DEFAULT = MID_TURN_QUEUE

KEY_SESSION = "session"

SESSION_THREAD = "thread"
SESSION_CHANNEL = "channel"

#: What ``delivery.session`` may say one session spans, and the second closed
#: vocabulary this module owns rather than a connector.
#:
#: A session is the unit of conversation state: one history, one turn at a time,
#: one queue. These two words say how much of a room shares one.
#:
#: ``thread`` gives every thread its own session, so two threads in a room are
#: two conversations that know nothing of each other and can run at once. It is
#: the default, and it is the shape a deployment that writes nothing has.
#:
#: ``channel`` gives the whole room one session, so threads share a history and
#: the agent has continuity across them. What it buys is memory; what it costs
#: is that everything keyed on a session is now shared room-wide -- one turn at
#: a time for the whole room, one queue for the whole room, and a message in any
#: thread reaching the turn a message in some other thread started.
#:
#: The two are spelled here rather than per connector for the reason the
#: ``mid_turn`` words are: they name how wide a session is, which is the same
#: question wherever a room can hold sub-conversations, and a connector with no
#: threads simply does not declare the key. What they must not be read as is a
#: statement about where an answer goes. Delivery is settled per message, from
#: the thread that message was posted in; ``channel`` widens the session and
#: leaves every reply where it was.
#:
#: ``user`` is deliberately absent. It would name a third partition -- one
#: session per person per room -- which is a different feature with its own
#: questions about what the agent may then see, and no deployment has asked for
#: it. A DM is already keyed that way and gets there without a word.
SESSION_VALUES: tuple[str, ...] = (SESSION_THREAD, SESSION_CHANNEL)

#: What a conversation gets when no layer settled ``session``. ``thread``, which
#: is the shape every deployment already has: widening a session changes what
#: the agent remembers and who can reach whose turn, so it happens only where
#: somebody asked for it.
SESSION_DEFAULT = SESSION_THREAD

KEY_REPLY = "reply"

REPLY_REQUIRED = "required"
REPLY_OPTIONAL = "optional"

#: Whether a turn in this conversation owes an answer, and the third closed
#: vocabulary this module owns rather than a connector.
#:
#: ``required`` is what every conversation has today: whatever a turn produces is
#: delivered, and a turn with nothing to say has no way to say so. It is the
#: default, so a deployment that writes nothing keeps exactly the behaviour it
#: has.
#:
#: ``optional`` says a turn may decline to answer. It does not say how a turn
#: declines: the connector supplies that contract, because a token a model has to
#: write is a string the code has to match, and an operator who spelled it would
#: be spelling half of a matcher. What the key buys is the licence, in the
#: conversations that want it.
#:
#: **Named for the obligation rather than for the traffic.** The question is
#: whether an answer is owed, which is a different question from what woke the
#: turn: two conversations reading every link posted in them can want opposite
#: things, one saving bookmarks silently and one writing an analysis of each. It
#: is also the reading that travels: a scheduled run producing nothing is
#: meaningful, another platform's group conversations behave the same way, and a
#: surface that implements only ``required`` still reads the word correctly.
#:
#: Deliberately not a trigger. A trigger is a pre-model predicate answered from
#: the payload alone, before any session exists -- see the trigger list in the
#: Slack connector, which says so -- and whether an answer is owed is settled
#: after the model has run.
REPLY_VALUES: tuple[str, ...] = (REPLY_REQUIRED, REPLY_OPTIONAL)

#: What a conversation gets when no layer settled ``reply``. ``required``, which
#: is every conversation that exists today: licensing a turn to say nothing
#: changes what a room sees, so it happens only where somebody asked for it.
REPLY_DEFAULT = REPLY_REQUIRED

#: The ``delivery`` keys this module settles itself, and the words each takes.
#: Every other key in the section is a connector's, checked through its own
#: ``validator``; these three are checked here because the words are the same
#: wherever the key is implemented. Named as a table rather than a run of
#: branches so that a fourth such key is one row.
_DELIVERY_VOCABULARIES: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {
        KEY_MID_TURN: MID_TURN_VALUES,
        KEY_SESSION: SESSION_VALUES,
        KEY_REPLY: REPLY_VALUES,
    }
)

#: What a ``permissions`` section may set. One key, because ``tools`` is the one
#: field ``narrow_permissions`` is monotone over: ``rules``, ``defaults`` and
#: ``approval_overrides`` have no narrowing operation that cannot also widen,
#: and a scope that could widen them would undo the point of the section.
PERMISSION_KEYS: frozenset[str] = frozenset({"tools"})

#: The gestures a ``clicks`` section may gate. Two, because
#: two exist: an approval button, which answers a question the agent stopped at,
#: and a stop button, which cancels a running turn. A third would need a surface
#: before it needed a key here.
#:
#: Spelled in the shared schema for the same reason ``PERMISSION_LEVELS`` is: a
#: connector narrows this further in its own declaration, but the vocabulary
#: itself is one list, and a misspelled gesture is caught at load rather than at
#: the click it was written to refuse.
CLICK_KEYS: frozenset[str] = frozenset({CLICK_APPROVE, CLICK_STOP})

#: Which match axes a key may be addressed on, for the keys whose meaning this
#: module defines. Keyed ``"<section>.<key>"``, with ``"<section>.*"`` for a
#: statement about a whole section, and intersected with whatever the channel's
#: own declaration says about the same key.
#:
#: **Absent means unrestricted, and most keys are absent.** ``delivery.prompt``
#: is the example worth naming: it says what to splice into a message and
#: nothing whatever about how the rule was addressed, so every axis is
#: legitimate there and it has no entry. A key earns an entry only when its own
#: meaning already accounts for an axis, or when the consumer cannot be given
#: the axis at all -- both a small and stateable class rather than a default.
#:
#: **``permissions`` is the second kind, and the restriction is provisional.**
#: A ``permissions`` section is settled in the runtime, at the tool call, from
#: the two ContextVars the request handler sets: a channel and a chat, and no
#: sender. Every such rule is therefore evaluated with ``user=None``, and
#: :meth:`Match.selects` answers that the fail-closed way in both directions --
#: which is the right answer for a sender nobody could name and the wrong one
#: for a sender nobody asked about. ``match: {chat: C1, user: [U_ONCALL]}``
#: never fires at all, so the narrowing an operator wrote is simply absent;
#: ``match: {chat: C1, not: {user: [U_ADMIN]}}`` fires for everybody including
#: U_ADMIN, so the exemption is inverted. Neither is visible from the config.
#: The second is the more dangerous: a rule that reads as "restrict everyone but
#: the admins" restricts the admins.
#: Lift this row when the runtime learns who is asking: the restriction is a
#: statement about today's consumer, not about what ``permissions`` means: the
#: section wants the identity axis back once there is a consumer for it.
#:
#: **The division of labour is by who defines the key.** The ``agent`` and
#: ``delivery`` sections have no shared key list at all -- a key exists in them
#: because a connector named it in ``sections`` -- so a restriction on one of
#: those belongs beside that declaration, in the connector's own
#: ``scope_capabilities``. ``clicks`` is the other case: the gestures are
#: :data:`CLICK_KEYS`, spelled here, and the reason the section cannot be
#: addressed on a sender is a fact about what a click *is* rather than about any
#: platform. It is therefore stated here, once, and it holds for a channel that
#: has not declared at all; a connector-side table could not reach that case.
#:
#: ``not`` needs no entry of its own. It names no dimension: it
#: removes people from the axis ``user`` and ``role`` name together (see
#: :data:`MATCH_NOT`), so a ``not:`` clause constrains whichever axis it is
#: written over, and barring ``user`` and ``role`` bars ``not`` with them. When
#: ``not`` grows a non-identity form it will constrain that axis instead, and a
#: table permitting the axis will permit it without an edit.
#:
#: **``workspace`` is barred from both entries, and it is barred by being left
#: out of them.** That is the allow-list working as designed -- an axis added
#: later is refused until somebody permits it -- and the classification was
#: made rather than inherited: both entries were re-read against the new axis
#: and neither should have it.
#:
#: The ground is the one both sentences already give. Each consumer is handed
#: the ids it is handed: a click arrives with the conversation it was pressed
#: in, and the permission hook is given a channel and a chat. Neither is given
#: a workspace, so a rule naming one is inert -- the setting an operator wrote
#: is simply absent, and nothing in the config shows it.
#:
#: It stays barred even once a consumer carries the fact, and for a second
#: reason. A workspace is *coarser* than a conversation: "channel or chat" is
#: the grain these keys are settled at, and a rule addressed one level above it
#: does not satisfy that requirement -- it states something about many
#: conversations while the key is read per conversation. An operator wanting a
#: workspace-wide gate writes it on the conversations, or the section gains an
#: entry saying otherwise. What may not happen is the axis arriving into a
#: permitted list by nobody having thought about it.
MATCH_AXIS_RESTRICTIONS: Mapping[str, AxisRestriction] = MappingProxyType(
    {
        f"{SECTION_CLICKS}.{ANY_KEY}": AxisRestriction.only_on(
            AXIS_CHANNEL,
            AXIS_CHAT,
            because=(
                "It has no effect on a rule that also names a sender: clicks"
                " says who may press a button, which is not the person the"
                " match selects on, and nothing resolves the two together. A"
                " click also arrives with the conversation it was pressed in"
                " and nothing else about where it came from -- not what kind of"
                " conversation it is and not which workspace it is in -- so a"
                " rule naming either of those never fires. So those clicks stay"
                " gated by layer 0 alone, which is wider than the rule that was"
                " written -- move it to a rule matching the conversation alone"
            ),
        ),
        f"{SECTION_PERMISSIONS}.{ANY_KEY}": AxisRestriction.only_on(
            AXIS_CHANNEL,
            AXIS_CHAT,
            because=(
                "Its consumer is given the channel and the chat alone: the"
                " permission hook settles this section from those two ids, so a"
                " rule naming a person never fires and a not: clause exempting"
                " one applies to them as well -- inert one way round and"
                " inverted the other, and invisible either way -- and a rule"
                " naming a kind of conversation, or the workspace it is in, is"
                " inert for want of anything saying which. Address it on the"
                " conversation instead. The bar is on today's consumer, not on"
                " the section, and lifts when the runtime carries the fact"
            ),
        ),
    }
)

#: Each section has exactly one reader: ``delivery`` the connector, ``agent``
#: the runtime, ``permissions`` the permission engine, ``clicks`` the
#: connector's interaction handler. A key belongs to the section whose reader
#: *acts* on it, which is not always the process that reads the config.
#:
#: The distinction that decides it is carry versus consume. ``prompt`` is read by
#: the connector and consumed there: it is spliced into the message text, and
#: nothing named ``prompt`` ever leaves the connector, so it is ``delivery``.
#: ``model_name`` is read by the connector and merely carried: it goes onto the
#: request as ``params["model_name"]`` and the runtime is what acts on it, so it
#: is ``agent``. Resolving a key in the process that only forwards it is an
#: implementation detail of where the config is read, and must not decide which
#: section names it -- otherwise every key a connector touches would drift into
#: ``delivery`` and the sections would stop meaning anything.
#: ``clicks`` is read by the connector and *is* consumed there -- the refusal
#: happens in the click handler, not one process later -- which is why it is not
#: folded into ``delivery`` all the same: ``delivery`` settles what happens to a
#: message, and a click is not a message. It arrives against a turn that already
#: exists, from someone who may not have started it, and it has its own
#: principal.
SUPPORTED_SECTIONS: tuple[str, ...] = (
    SECTION_DELIVERY,
    SECTION_AGENT,
    SECTION_PERMISSIONS,
    SECTION_CLICKS,
)

#: The sections a *connector* settles per conversation. ``permissions`` is not
#: one of them: it is resolved in the runtime, where the tool call happens.
#: Neither is ``clicks``, which the connector does settle but not per
#: conversation-to-answer: it is read when a button is pressed, against a
#: conversation the connector is already talking in. The distinction is
#: load-bearing rather than tidy -- see ``scoped_chats``, where treating a
#: restricting section as a connector one would let it open a conversation it
#: was written to lock down.
CONNECTOR_SECTIONS: tuple[str, ...] = (SECTION_DELIVERY, SECTION_AGENT)

#: Declared but read by nothing. Empty, every section named in the schema
#: being read by something. Kept as
#: a name rather than deleted, so that a section added ahead of its reader has
#: somewhere to be declared inert instead of being silently obeyed in part.
DEFERRED_SECTIONS: tuple[str, ...] = ()

ENTRY_KEYS: frozenset[str] = frozenset(
    {"match", *SUPPORTED_SECTIONS, *DEFERRED_SECTIONS}
)

#: A key ending in this appends to the key it names instead of replacing it.
#: Two keys rather than a sigil inside the text, because a marker embedded in a
#: multi-line prose block is hard to read and hard to escape.
APPEND_SUFFIX = "_append"



def _describe_identity(
    users: "tuple[str, ...] | None", roles: "tuple[str, ...] | None"
) -> "list[str]":
    """The ``user:`` and ``role:`` clauses of a description, in that order.

    ``None`` omits a clause; an empty tuple prints as an empty list, which is
    the distinction a ``clicks`` rule needs -- there, naming nobody is a rule
    that refuses everybody and not an absent one.

    Role-derived ids appear under ``user:`` as well as the role name they came
    from, because the ids are what a sender or a clicker is checked against.
    Showing the name alone would describe the config rather than the rule, and
    the two differ exactly when a role resolves to fewer people than it reads as
    -- which is the case worth being able to see in a log.
    """
    parts: "list[str]" = []
    if users is not None:
        parts.append(f"{AXIS_USER}: [{', '.join(users)}]")
    if roles is not None:
        parts.append(f"{AXIS_ROLE}: [{', '.join(roles)}]")
    return parts


def _describe_kinds(kinds: "tuple[str, ...]") -> str:
    """The kinds under ``chat_type``, in the shape a rule would be written in.

    One kind bare and several as a list, which is what keeps ``chat_type: im``
    readable back out of a startup summary or a warning now that the axis can
    hold more than one. The identity axis always prints its brackets because it
    is always a list in the file; this axis takes both spellings, so printing
    the one the rule used is the reading an operator can find in their config.

    Not a faithful echo of what was typed, and it cannot be: the entries are
    deduped and sorted on the way in, so ``[channel, channel]`` prints as the
    single kind it selects. That is the honest line, because the set is what the
    rule does.
    """
    if len(kinds) == 1:
        return kinds[0]
    return f"[{', '.join(kinds)}]"


@dataclass(frozen=True)
class ScopeMatch:
    """The criteria one scope is selected by.

    ::

        match     = channel? and workspace? and (chat? or chat_type?)
                    and identity?
        identity  = sender in (user or role) minus not.(user or role)
        chat_type = the conversation's kind is one of the kinds named
        workspace = the request came from the installation named

    Different axes AND; an absent axis matches anything. An absent positive
    identity means everyone; an absent ``not`` excludes nobody. A rule naming no
    workspace is therefore a rule about every workspace, which is the reading
    every other absent axis already has and needs no exception of its own.

    ``users`` and ``not_users`` are the two halves of **one** axis, held as
    sorted tuples because the config spells them as lists and the order of a set
    means nothing.

    **``chat_type`` names a set of kinds while ``chat`` names one conversation,
    and the asymmetry is the design rather than an omission.** An axis naming a
    *class* can sensibly name several classes at once: "every channel-like
    conversation" is two kinds on Slack, where a private channel arrives as
    ``group`` and not as ``channel``, and writing it as two rules with identical
    bodies says the same thing twice and leaves the second to rot. The entries
    OR, which is the OR the identity axis already performs between its two
    spellings (:data:`IDENTITY_AXES`). An axis naming one conversation cannot do
    the same: two ids are two conversations, each of which can be given a
    different body, so a list there would be two rules collapsed rather than one
    rule stated -- and ``chat`` stays a string.

    The field keeps the axis's one name though it holds a set, and does not
    follow ``users`` into the plural. ``users`` is plural because it is a union
    of two config keys -- ``user`` written out, and ``role`` resolved into the
    same ids -- so it could keep neither one's name. ``chat_type`` is one key
    with one spelling, and keeping it means the axis is called the same thing in
    the file, in the code, and in every warning printed about it.

    **A ``role`` is resolved into those same two tuples at load, and that is the
    whole of the feature.** A role names a set of people and a person is a set of
    ``(platform, id)`` pairs, so on the one platform a scope names, a role *is* a
    list of ids -- which is exactly what ``user`` enumerates. Resolving at load
    rather than at match time keeps ``role`` sugar over ``user``: the matcher,
    the composition and both readers are
    untouched by it, the OR between the two spellings is a set union performed
    once instead of a branch evaluated per message, and there is no way for the
    two to drift into meaning different things. ``roles`` and ``not_roles`` keep
    the names the ids came from, for the description only.

    **``workspace`` is a string and not a list, and it takes ``chat``'s side of
    that question rather than ``chat_type``'s.** The asymmetry above is between
    an axis naming a *class* and an axis naming a *thing*: a class can sensibly
    be named several times at once, because the entries are one instruction
    about a set of kinds, while two ids are two things that can each be told
    something different. A workspace id is a thing. It is an opaque id the
    platform issued, out of no vocabulary anything here can hold -- which is why
    it has no ``axis_values`` entry and never will, exactly as ``chat`` and
    ``user`` have none -- and two of them are two organisations, each of which
    can be given a body of its own. A list there would be two rules collapsed
    rather than one rule stated, so it is refused for ``chat``'s reason and by
    ``chat``'s code path.

    **It is not refused beside ``chat``, though ``chat_type`` is.** A named
    conversation has exactly one *type*, which is what makes that pair
    redundant. It does not have exactly one workspace: a Slack Connect channel
    is shared between workspaces and a request in it carries the workspace it
    came from, so ``{chat: C1, workspace: T_OURS}`` is the shared channel
    restricted to the side of it that is ours -- a rule selecting strictly less
    than either half, which is the opposite of a redundancy.
    """

    channel: "str | None" = None
    workspace: "str | None" = None
    chat: "str | None" = None
    chat_type: "tuple[str, ...] | None" = None
    users: "tuple[str, ...] | None" = None
    not_users: "tuple[str, ...] | None" = None
    roles: "tuple[str, ...] | None" = None
    not_roles: "tuple[str, ...] | None" = None

    @property
    def constrains_identity(self) -> bool:
        """Whether this match says anything at all about who is asking.

        Every half counts, and they count as **one**. ``not: {user: [U_intern]}``
        with no positive is "everyone except one person", which is narrower than
        "everyone" and is therefore a constraint on the same axis a positive
        ``user`` constrains; ``role`` is that axis spelled as a name. One
        boolean rather than a count is what keeps ``{channel, chat, role}`` and
        ``{channel, chat, user}`` at the same layer, so that rewriting a list of
        ids as a role cannot silently change which scope wins.

        The role halves are still tested here even though resolution has already
        folded their ids into ``users``, because a role that resolves to nobody
        on this platform leaves an empty tuple -- and a match that says "the
        admins, of whom there are none here" constrains identity every bit as
        much as one naming an id. Reading it as unconstrained would promote it
        to a broader layer for having matched nobody.
        """
        return (
            self.users is not None
            or self.not_users is not None
            or self.roles is not None
            or self.not_roles is not None
        )

    @property
    def _chat_grain(self) -> int:
        """How finely this match names the conversation: 0 absent, 1 kind, 2 one.

        ``chat`` is exact -- one conversation -- and ``chat_type`` is a kind, so
        a rule naming the kind sits under a rule naming the room. The two are
        refused together at load (:data:`CONVERSATION_AXES`), so the ``max``
        settles only a :class:`ScopeMatch` built in code rather than read from a
        file; it takes the finer of the two because the axes AND, and an
        intersection is no wider than either half.

        **Naming several kinds changes nothing here.** ``chat_type`` is grain 1
        whether it names one kind or every kind the platform has. Grain is how
        finely the axis is *written*, not how many conversations end up on the
        far side of it -- the same reading that leaves ``user: [U1, U2]`` and
        ``user: [U1]`` both at grain 2 -- and a list selects a set of kinds at
        exactly the level of precision one kind does, rather than a finer or a
        coarser one. So a rule for ``[channel, group]`` layers where each of the
        two single-kind rules it replaces layered, and collapsing them cannot
        reorder anything against the rules around them.
        """
        return max(
            2 if self.chat is not None else 0,
            1 if self.chat_type is not None else 0,
        )

    @property
    def _user_grain(self) -> int:
        """How finely this match names who is asking: 0 anyone, 1 coarse, 2 ids.

        **The governing rule is that grain must never be finer for a wider match
        set**, and every case below follows from it.

        *Explicit ids* are grain 2: the set is written out, and it is exactly as
        large as the line that names it.

        *A role* is grain 1, because its size is a property of ``roles:`` rather
        than of the rule. ``role: admin`` may hold one person or forty, and the
        rule cannot be ordered against a list of ids without knowing which.
        Reading it as grain 2 would rank it above ``user: [U1]`` on a config
        where the admins are the whole workspace, which is the inversion.

        *Mixed identity takes the coarsest component present*, so ``{user: [U1],
        role: admin}`` is grain 1. The two spellings OR
        (:data:`IDENTITY_AXES`), so the mixed rule is a union that strictly
        contains the ``user`` half: giving it grain 2 would put a rule above the
        narrower one it contains. It then ties with ``{role: admin}``, and a tie
        is the safe answer where an order cannot be established.

        *A pure negative shares the role's grain* rather than taking a level of
        its own. ``not: {user: [U2]}`` is everyone-but-one, and it cannot be
        ordered against ``{role: admin}`` without knowing how big the role is --
        so they tie, which is the honest answer, and it is the same tie a role
        already has with anything else of unknown size.

        *A negative alongside a positive changes nothing.* ``{user: [U1, U2],
        not: {user: [U2]}}`` is narrower than the positive half alone, and
        narrowing never demands a coarser grain, so it keeps the grain the
        positive half gives it.
        """
        if not self.constrains_identity:
            return 0
        if self.users is None and self.roles is None:
            # A pure negative: everyone, minus a set of unknown size.
            return 1
        if self.roles is not None:
            # A role is involved, so the coarsest component present is the role.
            return 1
        return 2

    @property
    def specificity(self) -> tuple[int, int, int, int]:
        """How narrowly this is written, as ``[platform, workspace, chat, user]``.

        Compared lexicographically, and that ordering is the layering (section
        6). Read left to right: a rule naming the platform outranks one naming
        none; among those, the one naming the workspace outranks one naming
        none; then the one naming the conversation more finely wins; and only
        where those are equal does the identity grain decide.

        ::

            (0, 0, 0, 0)   {}                  -- and channels.<platform>.* under it
            (1, 0, 0, 0)   {channel: X}
            (1, 0, 1, 0)   {channel: X, chat_type: im}
            (1, 0, 2, 0)   {channel: X, chat: Y}
            (1, 0, 2, 2)   {channel: X, chat: Y, user: [Z]}
            (1, 1, 0, 0)   {channel: X, workspace: T}
            (1, 1, 2, 0)   {channel: X, workspace: T, chat: Y}

        =====  ==========  =========================  =========================
        grain  workspace   chat dimension             user dimension
        =====  ==========  =========================  =========================
        0      absent      absent                     unconstrained
        1      named       by kind (``chat_type``)    coarse: a role, or a
                                                      pure negative
        2      --          exact (``chat``)           explicit ids only
        =====  ==========  =========================  =========================

        The two graded dimensions are :attr:`_chat_grain` and
        :attr:`_user_grain`, and the reasoning for each level is with them. The
        platform and workspace components are 0 for absent and 1 for named:
        there is one way to name either, and a list is refused on both.

        **The workspace sits above the conversation, and the consequence is
        worth stating.** ``{channel: X, workspace: T}`` outranks ``{channel: X,
        chat: Y}``, so a workspace-wide rule beats a rule about one room. That
        is the same decision ``chat`` beating ``user`` already is, and it rests
        on the same fact: the two match sets do not nest. A conversation is not
        inside a workspace as far as this matcher is concerned -- a Slack
        Connect channel carries requests from more than one -- so neither rule
        contains the other, section 6 orders only the nested case, and an
        ordering had to be chosen. It is chosen so that the axes read
        coarsest-first, the order they are declared and described in. An
        operator who wants the room to win writes the workspace on the room's
        rule, which is a rule that genuinely nests inside both.

        **A tuple rather than a count of axes, because the count is wrong and not
        merely coarse.** Counting made ``{channel, chat, role: admin}`` and
        ``{channel, chat, user: [U1]}`` both 3, and a role resolves at load into
        the very ids ``user`` would have enumerated -- the matcher reads
        ``users`` and nothing else -- so the two were being tied on file position
        while one of them was strictly narrower. Which won then depended on the
        order the rules happened to be written in, with no crash and no log line
        to show it: it appears as the wrong model or the wrong trigger set on a
        real conversation.

        **Identity still counts once, however it is spelled.** ``user``, ``role``
        and ``not`` are one axis between them and they settle one component of
        the tuple, so adding a ``not:`` exemption to a rule cannot promote it
        above a rule it had been sitting under. Counting keys would have made
        ``{channel, chat, user, not}`` outrank ``{channel, chat, user}``, and the
        reordering is exactly as invisible as the one above.

        **``chat`` beats ``user``, and that is a decision rather than a
        derivation.** ``{channel, user}`` -- this person anywhere on the platform
        -- and ``{channel, chat}`` -- everyone in this conversation -- are
        genuinely incomparable: neither match set contains the other, and section
        6 orders the nested case and says nothing about this one. Counting axes
        left them tied and settled them by file position. They are now ordered,
        with the conversation deciding first, which makes explicit what the code
        used to leave to line number. A rule about a room is the one an operator
        reads as being about that room.

        **Ties still break by file position, later wins**, exactly as before, and
        every tie above is deliberate: where two rules cannot be ordered without
        knowing something the rules do not say, a tie is safe and an inversion is
        not.
        """
        return (
            1 if self.channel is not None else 0,
            1 if self.workspace is not None else 0,
            self._chat_grain,
            self._user_grain,
        )

    def selects(
        self,
        *,
        channel: "str | None",
        chat: "str | None",
        chat_type: "str | None" = None,
        workspace: "str | None" = None,
        user: "str | None" = None,
    ) -> bool:
        """Whether this match claims a request from ``user`` in ``chat``.

        ``workspace`` fails closed exactly as ``chat`` does, and by the same
        line: a caller with no workspace to offer is in none, so a rule written
        for one installation does not fire for a request that cannot be placed
        in it. A rule that names no workspace is untouched by this and applies
        to every one of them, which is what an absent axis means everywhere in
        this module rather than a case of its own.

        ``chat_type`` fails closed the way the sender does and for the same
        reason. A caller with no kind to offer -- a startup summary, a
        pseudo-channel, a connector that populates the axis on messages and not
        on the path being settled here -- is in no named kind, so a rule written
        for direct messages does not fire for it. The alternative would be to
        read an absent kind as every kind, which would apply a rule written for
        one surface to every other.

        **A request carries one kind and a rule may name several**, so this is a
        membership test rather than an equality. A rule fires for a request in
        any of the kinds it names, and the fail-closed answer above is the same
        one a list gives: the empty string is in no list of kinds, however long.

        **An unidentified sender fails both halves of the identity axis in the
        same direction, and it falls out of one rule rather than two.** Ids are
        non-empty, so an empty sender is in no positive list -- a scope written
        for named people does not fire for someone the connector could not name
        -- and is in no ``not`` list either, so a restriction written to exempt
        named people still applies to them. Both are the fail-closed answer, and
        they are the rule for a person with no id on this platform: the
        restriction applies where they have not been identified.
        """
        if self.channel is not None and self.channel != channel:
            return False
        if self.workspace is not None and self.workspace != workspace:
            return False
        if self.chat is not None and self.chat != chat:
            return False
        if self.chat_type is not None and (chat_type or "") not in self.chat_type:
            return False
        sender = (user or "").strip()
        if self.users is not None and sender not in self.users:
            return False
        if self.not_users is not None and sender in self.not_users:
            return False
        return True

    def describe(self) -> str:
        """This match as one line, for a log or a startup summary.

        The identity halves are ``_describe_identity``'s, twice: once for what
        the match selects and once inside ``not``, which is the same two clauses
        about the other direction. ``chat_type`` is ``_describe_kinds``', which
        reads a one-kind rule back exactly as it was written.
        """
        parts = [
            f"{name}: {value}"
            for name, value in (
                (AXIS_CHANNEL, self.channel),
                (AXIS_WORKSPACE, self.workspace),
                (AXIS_CHAT, self.chat),
            )
            if value is not None
        ]
        if self.chat_type is not None:
            parts.append(f"{AXIS_CHAT_TYPE}: {_describe_kinds(self.chat_type)}")
        parts.extend(_describe_identity(self.users, self.roles))
        written = _describe_identity(self.not_users, self.not_roles)
        if written:
            parts.append(f"{MATCH_NOT}: {{{', '.join(written)}}}")
        return "{" + ", ".join(parts) + "}" if parts else "{}"


#: What a warning says has happened to a value it could not read. Everything in
#: ``match`` drops the whole scope, which is the safe direction there; a
#: ``clicks`` clause drops the clause alone, and saying "dropping that scope" of
#: it would send an operator looking for settings that are still in force.
DROP_SCOPE = "Dropping that scope"


@dataclass(frozen=True)
class ClickRule:
    """Who may make one kind of click in the conversations a scope matches.

    ``users`` is the settled list of ids, with every ``role`` already folded into
    it at load exactly as :func:`_resolve_roles` folds one into a ``match``: the
    two spellings are one axis and they OR. ``roles`` keeps the names they
    came from, for the description only.

    **An empty ``users`` is a rule and not an absence.** A role that is declared
    but holds nobody with an id on this platform settles here as an empty tuple:
    a person with no id for the current platform is not in the role there, so
    nobody satisfies it and every click is refused.

    A rule that should not apply is left out -- leaving the layer below to decide
    -- rather than
    written empty.
    """

    users: tuple[str, ...] = ()
    roles: tuple[str, ...] = ()

    def permits(self, user_id: "str | None") -> bool:
        """Whether ``user_id`` may make this click.

        Both halves of "cannot be identified" fail here in the same direction and
        by the same line. A payload that held no value for any of the
        channel's ``identity_keys`` arrives as an empty string, and ids are
        non-empty, so it is in no list; an id the rule's ``role`` maps nobody
        onto is likewise in no list. Neither is read as a permission, so the
        refusal is the answer whenever the id is missing, by whichever of the two
        routes it went missing.
        """
        clicker = (user_id or "").strip()
        return bool(clicker) and clicker in self.users

    def describe(self) -> str:
        """This rule as one line, for a log.

        ``user:`` is always written, empty tuple included, because an empty one
        here is a rule that refuses everybody rather than an absent clause.
        """
        parts = _describe_identity(self.users, self.roles or None)
        return "{" + ", ".join(parts) + "}"


@dataclass(frozen=True)
class Scope:
    """One compiled entry, with everything unusable already dropped.

    ``index`` is the entry's position in the file. It breaks ties between two
    scopes of equal specificity, so that within one layer the later line wins --
    the only ordering rule an author can apply without reading the grain table.
    """

    match: ScopeMatch
    sections: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    index: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "sections",
            MappingProxyType(
                {k: MappingProxyType(dict(v)) for k, v in self.sections.items()}
            ),
        )

    @property
    def layer(self) -> tuple[int, int, int, int]:
        return self.match.specificity

    def selects(
        self,
        *,
        channel: "str | None",
        chat: "str | None",
        chat_type: "str | None" = None,
        workspace: "str | None" = None,
        user: "str | None" = None,
    ) -> bool:
        return self.match.selects(
            channel=channel,
            chat=chat,
            chat_type=chat_type,
            workspace=workspace,
            user=user,
        )

    def section(self, name: str) -> Mapping[str, Any]:
        return self.sections.get(name, {})


def signed_entries(value: Any) -> "tuple[str, ...] | None":
    """The ``+x`` / ``-x`` entries of ``value``, or ``None`` if it has none.

    A list is either all-plain or all-signed. ``[mention, +has_file]`` is
    rejected because it reads as either "replace with these two" or "replace
    with mention, then add has_file", and would mean whichever the
    implementation happened to do.
    """
    if not isinstance(value, (list, tuple)):
        return None
    entries = [str(item).strip() for item in value if isinstance(item, str)]
    if len(entries) != len(list(value)):
        return None
    signed = [e for e in entries if e[:1] in ("+", "-")]
    if not signed:
        return None
    return tuple(signed) if len(signed) == len(entries) else ()


def _describe_list_mix(value: Sequence[Any]) -> str:
    return "[" + ", ".join(repr(item) for item in value) + "]"


def _compile_ids(
    raw_value: Any,
    *,
    position: str,
    label: str,
    warn: Warn,
    consequence: str = DROP_SCOPE,
) -> "tuple[str, ...] | None":
    """The ids under one identity key, or ``None`` if it cannot be honoured.

    In ``match``, every failure here drops the whole scope rather than the key,
    and the direction is the reason. An identity axis that could not be read and
    were simply ignored would leave a rule written for named people applying to
    everyone in the conversation, and a ``not`` that were ignored would apply a
    restriction inside the exemption it was written to carve out. Both reach
    further than what was written. Dropping reaches less far, and leaves the
    conversation on the layer below rather than on a rule nobody wrote.

    ``consequence`` is what the caller does about it, said out loud in the
    warning. A ``clicks`` clause drops the clause and not the scope -- it is one
    key of one section, and the sections beside it are still perfectly readable
    -- so it says so rather than sending an operator looking for delivery
    settings that are still in force.
    """
    if isinstance(raw_value, str):
        # A bare id where a list is wanted. Refused rather than read as a list
        # of one: ``user`` is a list precisely so that promoting
        # an inline list to a role later is a pure refactor, and accepting both
        # spellings would give the axis two shapes with one meaning.
        warn(
            "scopes%s.%s=%r is a single id where a list is wanted; write"
            " [%s]. %s",
            position,
            label,
            raw_value,
            raw_value,
            consequence,
        )
        return None
    if not isinstance(raw_value, (list, tuple)):
        warn(
            "scopes%s.%s=%r is not a list of ids. %s",
            position,
            label,
            raw_value,
            consequence,
        )
        return None

    ids: list[str] = []
    for item in raw_value:
        if not isinstance(item, str):
            warn(
                "scopes%s.%s=%s is not a list of ids; quote ids so YAML leaves"
                " them as written. %s",
                position,
                label,
                _describe_list_mix(list(raw_value)),
                consequence,
            )
            return None
        text = item.strip()
        if not text:
            warn(
                "scopes%s.%s has an empty id in it. %s",
                position,
                label,
                consequence,
            )
            return None
        ids.append(text)

    if not ids:
        # Not read as "no constraint". An author who wrote the key meant to name
        # somebody, and reading an empty positive list as everyone would turn
        # a key written to name specific people into a match on all of them.
        warn(
            "scopes%s.%s is an empty list, which names nobody; it is not read as"
            " everyone. %s",
            position,
            label,
            consequence,
        )
        return None
    return tuple(sorted(set(ids)))


def _compile_role_names(
    raw_value: Any,
    *,
    position: str,
    label: str,
    warn: Warn,
    consequence: str = DROP_SCOPE,
) -> "tuple[str, ...] | None":
    """The role names under one key, or ``None`` if it cannot be honoured.

    A scalar and a list are both accepted, and this is not the inconsistency it
    looks like beside :func:`_compile_ids`, which refuses a bare id for the
    reason given at that refusal. A role name has no such refactor to protect,
    ``role: admin`` is the ordinary spelling, and naming two roles is the same
    OR the axis already performs, so refusing either spelling would be a rule
    nobody asked for.
    """
    values = [raw_value] if isinstance(raw_value, str) else raw_value
    if not isinstance(values, (list, tuple)):
        warn(
            "scopes%s.%s=%r is not a role name or a list of them. %s",
            position,
            label,
            raw_value,
            consequence,
        )
        return None

    names: list[str] = []
    for item in values:
        if not isinstance(item, str):
            warn(
                "scopes%s.%s=%s is not a list of role names. %s",
                position,
                label,
                _describe_list_mix(list(values)),
                consequence,
            )
            return None
        text = item.strip()
        if not text:
            warn(
                "scopes%s.%s has an empty role name in it. %s",
                position,
                label,
                consequence,
            )
            return None
        names.append(text)

    if not names:
        # The same reading as an empty ``user`` list, for the same reason: an
        # author who wrote the key meant to name somebody, and an empty positive
        # matching everyone is the widest possible reading of the narrowest
        # possible instruction.
        warn(
            "scopes%s.%s is an empty list, which names no role; it is not read"
            " as everyone. %s",
            position,
            label,
            consequence,
        )
        return None
    return tuple(sorted(set(names)))


def _compile_chat_types(
    raw_value: Any,
    *,
    position: str,
    label: str,
    warn: Warn,
    consequence: str = DROP_SCOPE,
) -> "tuple[str, ...] | None":
    """The kinds under ``chat_type``, or ``None`` if the scope must be dropped.

    A bare word and a list are both accepted, as for :func:`_compile_role_names`
    and for a reason of the same shape. ``chat_type: im`` is the ordinary
    spelling of the ordinary rule; naming two kinds is the OR the axis performs
    between the conversations it selects, and refusing either spelling would be
    a rule nobody asked for. The bare-value refusal :func:`_compile_ids` makes
    has no counterpart here: a kind is a word out of a vocabulary the platform
    declares rather than an id, and there is no later promotion into a named set
    for the list spelling to be protecting.

    **Duplicates are a set, so they are deduped rather than refused.**
    ``[channel, channel]`` names ``{channel}``, which is a set an operator
    reaches by generating the list or by editing one, and it says nothing wrong:
    it selects exactly what it reads as selecting. Sorting is the choice the
    identity axis already makes and is made here for the same reason -- the
    order of a set means nothing, and fixing one keeps two spellings of one rule
    from describing themselves differently.

    **An empty list is refused and is not read as every kind.** That is the
    silent-widening direction, and it is the same reading an empty ``user`` list
    gets: an author who wrote the key meant to name something, and turning the
    narrowest possible instruction into the widest possible match is the failure
    no refusal in this module may perform.
    """
    values = [raw_value] if isinstance(raw_value, str) else raw_value
    if not isinstance(values, (list, tuple)):
        warn(
            "scopes%s.%s=%r is not a kind of conversation or a list of them. %s",
            position,
            label,
            raw_value,
            consequence,
        )
        return None

    kinds: list[str] = []
    for item in values:
        if not isinstance(item, str):
            warn(
                "scopes%s.%s=%s is not a list of kinds of conversation. %s",
                position,
                label,
                _describe_list_mix(list(values)),
                consequence,
            )
            return None
        text = item.strip()
        if not text:
            warn(
                "scopes%s.%s has an empty kind in it. %s",
                position,
                label,
                consequence,
            )
            return None
        kinds.append(text)

    if not kinds:
        warn(
            "scopes%s.%s is an empty list, which names no kind of conversation;"
            " it is not read as every kind. %s",
            position,
            label,
            consequence,
        )
        return None
    return tuple(sorted(set(kinds)))


def _compile_not(
    raw: Any, *, position: str, warn: Warn
) -> "tuple[bool, tuple[str, ...] | None, tuple[str, ...] | None]":
    """Settle a ``not:`` block into the people it excludes.

    Returns ``(kept, not_users, not_roles)``. ``kept`` is false when the scope
    must go. The two halves are collected separately and both survive: a block
    naming ``user`` and ``role`` together excludes the union of them, which is
    the same OR the positive half performs.
    """
    if not isinstance(raw, Mapping):
        warn(
            "scopes%s.match.%s=%r is not a mapping of criteria to exclude;"
            " dropping that scope",
            position,
            MATCH_NOT,
            raw,
        )
        return False, None, None
    if not raw:
        warn(
            "scopes%s.match.%s is empty, so it excludes nobody; dropping that"
            " scope rather than reading it as a rule that names no exemption",
            position,
            MATCH_NOT,
        )
        return False, None, None

    not_users: "tuple[str, ...] | None" = None
    not_roles: "tuple[str, ...] | None" = None
    for raw_key, raw_value in raw.items():
        key = str(raw_key).strip()
        if key in DEFERRED_AXES:
            warn(
                "scopes%s.match.%s.%s is not supported in this version;"
                " dropping that scope rather than applying a restriction to the"
                " people its exemption named",
                position,
                MATCH_NOT,
                key,
            )
            return False, None, None
        if key == AXIS_ROLE:
            names = _compile_role_names(
                raw_value,
                position=position,
                label=f"match.{MATCH_NOT}.{key}",
                warn=warn,
            )
            if names is None:
                return False, None, None
            not_roles = names
            continue
        if key not in NOT_AXES:
            # Section 4.4. Not a typo and not a deferral of the whole axis: the
            # axis is readable, ``not`` on it is what is undecided, because
            # ``not: {chat: A, user: B}`` has two defensible meanings and
            # picking one here would settle an open question by accident.
            warn(
                "scopes%s.match.%s.%s is not something this version can"
                " exclude: %s applies to the identity axis only, and %s is not"
                " it. Dropping that scope; write the conversations out instead",
                position,
                MATCH_NOT,
                key,
                MATCH_NOT,
                key,
            )
            return False, None, None
        ids = _compile_ids(
            raw_value, position=position, label=f"match.{MATCH_NOT}.{key}", warn=warn
        )
        if ids is None:
            return False, None, None
        not_users = ids

    return True, not_users, not_roles


def _compile_match(
    raw: Any, *, position: str, warn: Warn
) -> "ScopeMatch | None":
    """Settle one ``match`` block, or ``None`` if the scope must be dropped."""
    if raw is None:
        # A scope with no criteria applies everywhere. That is legal -- an
        # absent axis matches anything -- and is the layer below any scope that
        # names a platform.
        return ScopeMatch()
    if not isinstance(raw, Mapping):
        warn(
            "scopes%s.match=%r is not a mapping of criteria; dropping that"
            " scope, so nothing it configured applies",
            position,
            raw,
        )
        return None

    values: dict[str, str] = {}
    chat_types: "tuple[str, ...] | None" = None
    users: "tuple[str, ...] | None" = None
    not_users: "tuple[str, ...] | None" = None
    roles: "tuple[str, ...] | None" = None
    not_roles: "tuple[str, ...] | None" = None
    for raw_key, raw_value in raw.items():
        key = str(raw_key).strip()
        if key in DEFERRED_AXES:
            # Dropped rather than ignored, and the direction matters. A scope
            # written for one person would, with its identity axis ignored,
            # apply to everyone in the conversation instead -- a rule that
            # reaches further than it was written to reach. Dropping it reaches
            # less far, which is the failure an operator can see and fix.
            warn(
                "scopes%s.match.%s is not supported in this version and would"
                " otherwise widen the scope to everyone it did not name;"
                " dropping that scope",
                position,
                key,
            )
            return None
        if key == MATCH_NOT:
            kept, not_users, not_roles = _compile_not(
                raw_value, position=position, warn=warn
            )
            if not kept:
                return None
            continue
        if key == AXIS_USER:
            users = _compile_ids(
                raw_value, position=position, label=f"match.{key}", warn=warn
            )
            if users is None:
                return None
            continue
        if key == AXIS_ROLE:
            roles = _compile_role_names(
                raw_value, position=position, label=f"match.{key}", warn=warn
            )
            if roles is None:
                return None
            continue
        if key == AXIS_CHAT_TYPE:
            # Read apart from the id-shaped axes below, which take one string
            # and nothing else. This one takes a word or a list of words, so it
            # cannot go through the single-string path that refuses a list as
            # "not an id".
            chat_types = _compile_chat_types(
                raw_value, position=position, label=f"match.{key}", warn=warn
            )
            if chat_types is None:
                return None
            continue
        if key not in SUPPORTED_AXES:
            warn(
                "scopes%s.match.%s is not a match axis; a match carries %s."
                " Dropping that scope rather than matching more broadly than it"
                " was written to",
                position,
                key,
                ", ".join(SUPPORTED_MATCH_KEYS),
            )
            return None
        if raw_value is None:
            warn(
                "scopes%s.match.%s has no value; dropping that scope",
                position,
                key,
            )
            return None
        if not isinstance(raw_value, str):
            warn(
                "scopes%s.match.%s=%r is not an id; quote ids so YAML leaves"
                " them as written. Dropping that scope",
                position,
                key,
                raw_value,
            )
            return None
        text = raw_value.strip()
        if not text:
            warn(
                "scopes%s.match.%s is empty; dropping that scope",
                position,
                key,
            )
            return None
        values[key] = text

    return ScopeMatch(
        channel=values.get(AXIS_CHANNEL),
        # Through the id-shaped path above, which is where it belongs: a
        # workspace is an opaque id the platform issued, so it takes one string,
        # refuses a list, and needs no compile step of its own.
        workspace=values.get(AXIS_WORKSPACE),
        chat=values.get(AXIS_CHAT),
        chat_type=chat_types,
        users=users,
        not_users=not_users,
        roles=roles,
        not_roles=not_roles,
    )


def _refuse_chat_with_chat_type(
    match: "ScopeMatch", *, position: str, warn: Warn
) -> bool:
    """Whether a scope kept the two conversation axes apart, as it must.

    ``chat`` names one conversation, and that conversation's type is already
    settled by the platform. Writing ``chat_type`` beside it therefore adds
    nothing where the two agree and names an empty set where they disagree --
    and the disagreeing rule is not an error anywhere: it compiles, it is listed
    in the startup summary, and it silently never fires. Refusing the pair by
    name is what turns that into a line an operator can read.

    **A structural refusal and not a consistency check.** Nothing here asks
    whether the id really is of the named type, and nothing may: the answer
    lives on the platform, the question would be asked at config-apply time
    about a conversation that may not exist yet, may be one the bot is not in,
    and may be renamed or archived after the check passed. The redundancy is a
    fact about the two keys and is established by reading the rule.

    Dropped rather than narrowed to ``chat``, which is the direction every other
    refusal in this module takes: keeping one half would apply a rule the
    operator did not write, and leaving the conversation on the layer below is
    the failure they can see and fix.

    **A list of kinds changes nothing about this.** One conversation still has
    one type, so naming it beside a set of kinds is the same redundancy where
    the type is in the set and the same empty match where it is not.
    """
    if match.chat is None or match.chat_type is None:
        return True
    warn(
        "scopes%s.match names both %s=%s and %s=%s, and a conversation has one"
        " type already; the pair is redundant where they agree and matches"
        " nothing where they do not. Dropping that scope -- write %s alone for"
        " that conversation, or %s alone for every conversation of that kind",
        position,
        AXIS_CHAT,
        match.chat,
        AXIS_CHAT_TYPE,
        _describe_kinds(match.chat_type),
        AXIS_CHAT,
        AXIS_CHAT_TYPE,
    )
    return False


def _require_channel_for_chat(
    match: "ScopeMatch", *, position: str, warn: Warn
) -> bool:
    """Whether a scope naming a conversation also names the channel it is on.

    A conversation id is only meaningful inside a platform: ``C000000AAAA`` is a
    Slack channel and nothing else, but the matcher cannot know that, and a chat
    named without a channel selects for **every** declaring connector.

    ``chat_type`` is held to the same rule for the same two reasons. Its
    vocabulary is declared per platform -- a word one connector defines for a
    kind of conversation another need not have at all -- so with no channel
    there is nothing to check the word against, and the rule would select on
    every declaring connector that happens to spell a kind the same way.

    The reason to refuse it is not tidiness. Validation is keyed on the channel:
    ``_compile_section`` looks the capabilities up from ``match.channel``, so a
    scope with no channel is checked against nothing at all -- not the key
    allow-list, not the layer-0 map, not a single connector validator. It then
    still matches, and on Slack a scope naming a chat also exempts that
    conversation from ``allowed_channel_ids``. An unvalidated rule that widens
    who can be answered is exactly the shape this design refuses elsewhere.

    Dropped rather than validated-against-everything: which connector's
    validators should apply is section 13's Q2, still open, and guessing at it
    here would settle an open question by accident. A scope with neither axis is
    untouched -- there is no channel to ask, it names no conversation, and it
    cannot exempt one.
    """
    if match.chat is not None:
        named, value = AXIS_CHAT, match.chat
    elif match.chat_type is not None:
        named, value = AXIS_CHAT_TYPE, _describe_kinds(match.chat_type)
    else:
        return True
    if match.channel is not None:
        return True
    warn(
        "scopes%s.match.%s=%s names a conversation without saying which"
        " channel it belongs to, so nothing can validate it and it would match"
        " every connector that declares scopes; dropping that scope. Add"
        " channel: alongside %s:",
        position,
        named,
        value,
        named,
    )
    return False


def _require_channel_for_workspace(
    match: "ScopeMatch", *, position: str, warn: Warn
) -> bool:
    """Whether a scope naming a workspace also names the platform it is on.

    The rule :func:`_require_channel_for_chat` applies, one level coarser. A
    workspace id is issued by a platform and means nothing without it --
    ``T000000AAAA`` is a Slack team and nothing else -- so a workspace named
    with no channel would select on every connector that declares scopes, and
    would select on the ones that have no such concept at all.

    The validation argument is the same one and is the stronger half. Everything
    ``_compile_section`` checks is looked up from ``match.channel``: with no
    channel there is no capability declaration, no key allow-list, no layer-0
    map and no connector validator, so the rule is checked against nothing
    whatever and still matches.

    Dropped rather than validated against every channel, for the reason given
    there: which connector's validators should apply is still open, and
    answering it here would settle it by accident.
    """
    if match.workspace is None or match.channel is not None:
        return True
    warn(
        "scopes%s.match.%s=%s names a workspace without saying which platform"
        " issued it, so nothing can validate it and it would match every"
        " connector that declares scopes; dropping that scope. Add channel:"
        " alongside %s:",
        position,
        AXIS_WORKSPACE,
        match.workspace,
        AXIS_WORKSPACE,
    )
    return False


def _require_channel_for_identity(
    match: "ScopeMatch", *, position: str, warn: Warn
) -> bool:
    """Whether a scope naming senders also names the platform they are on.

    The same rule as :func:`_require_channel_for_chat`, for the same reason and
    with one more of its own. A raw id is meaningless without the platform that
    issued it: identity is an opaque per-channel bag, ``U000000AAAA`` is a
    Slack id and nothing else, and the matcher cannot know that. Without a
    channel the scope is also checked against no capability declaration at all,
    so nothing says whether the axis is populated there.

    "This person, anywhere" is a real requirement and it is not this. It is what
    ``people:`` is for: a person is named once and mapped to an id
    per platform, which is the only shape in which one entry can mean the same
    human on two of them. That is now built -- and it makes the rule *stricter*
    rather than looser, because a role is resolved per platform. ``role: admin``
    on no channel is not "the admins everywhere": it is a set of ids this
    function cannot even look up, since the same role holds different ids on
    each platform its members are on. A scope that names a sender, by id or by
    role, names the platform.
    """
    if not match.constrains_identity or match.channel is not None:
        return True
    named = [
        *(match.users or ()),
        *(match.not_users or ()),
        *(match.roles or ()),
        *(match.not_roles or ()),
    ]
    warn(
        "scopes%s.match names senders (%s) without saying which platform they"
        " are on, so nothing can resolve or validate them and they would be"
        " matched against every connector that declares scopes; dropping that"
        " scope. Add channel: alongside",
        position,
        ", ".join(named),
    )
    return False


def _identity_spelling(
    users: "tuple[str, ...] | None", roles: "tuple[str, ...] | None"
) -> str:
    """Which spelling of the identity axis the author actually wrote.

    One axis, two spellings, and a warning that named the wrong one would send
    an operator looking for a ``user:`` line that is not in their file.
    """
    written = [
        name
        for name, value in ((AXIS_USER, users), (AXIS_ROLE, roles))
        if value is not None
    ]
    return "/".join(written) or AXIS_USER


def _match_axes(match: ScopeMatch) -> frozenset[str]:
    """The axes a ``match`` constrains, spelled the way the author wrote them.

    **Must be taken before roles are resolved**, and the caller does. After
    :func:`_resolve_roles` a role-only match holds a ``users`` tuple of the
    ids the role folded into, so this would report ``user`` on a rule whose file
    contains no ``user:`` line -- the same mistake :func:`_identity_spelling`
    exists to avoid, made one function over.

    ``not`` contributes no axis of its own. It removes people from the axis
    ``user`` and ``role`` name together (:data:`MATCH_NOT`), so ``not: {user:
    [...]}`` constrains ``user`` and is reported as ``user``. That is what makes
    a restriction barring the identity axes bar the negative half with the
    positive one, without ``not`` needing a row anywhere.
    """
    axes: set[str] = set()
    if match.channel is not None:
        axes.add(AXIS_CHANNEL)
    if match.workspace is not None:
        axes.add(AXIS_WORKSPACE)
    if match.chat is not None:
        axes.add(AXIS_CHAT)
    if match.chat_type is not None:
        axes.add(AXIS_CHAT_TYPE)
    if match.users is not None or match.not_users is not None:
        axes.add(AXIS_USER)
    if match.roles is not None or match.not_roles is not None:
        axes.add(AXIS_ROLE)
    return frozenset(axes)


#: What dropping a scope costs, in the words the warning uses. Paired with
#: ``_DROP_CLAUSE`` below: both say the drop is a *widening*, and they say
#: different things because the two clauses leave different things behind. A
#: dropped ``match`` leaves the conversation on the layer below; a dropped
#: ``clicks`` clause leaves the click gated by layer 0 alone, there being no
#: scope under it to fall to.
_DROP_SCOPE = (
    "Dropping that scope, which leaves that conversation on the layer below --"
    " a restriction written this way is not applied either"
)


def _refuse_unknown_roles(
    names: "tuple[str, ...]",
    *,
    directory: PeopleDirectory,
    position: str,
    label: str,
    consequence: str,
    warn: Warn,
) -> bool:
    """Warn and answer ``True`` if any of ``names`` is in no ``roles:`` block.

    Both places a config may name a role -- a ``match`` and a ``clicks`` clause
    -- refuse an undeclared name, and refuse it for one reason: a name in no
    ``roles:`` block is a typo or a role deleted from under a rule still using
    it, and reading it as "nobody" would make ``not: {role: admin}`` exclude
    nobody and land the restriction on exactly the people it was written to
    exempt.

    ``consequence`` is the caller's and is not shared, because the two drops
    leave different things standing and saying which is the whole point. The
    declared roles are listed either way, so the warning that reports a typo
    also shows what could have been meant.
    """
    unknown = sorted({name for name in names if not directory.knows_role(name)})
    if not unknown:
        return False
    warn(
        "scopes%s.%s names %s, which %s: does not declare, so nothing can say"
        " who is in %s. %s. The declared roles are %s",
        position,
        label,
        ", ".join(unknown),
        ROLES_KEY,
        "them" if len(unknown) > 1 else "it",
        consequence,
        ", ".join(sorted(directory.roles)) or "none",
    )
    return True


def _resolve_roles(
    match: ScopeMatch,
    *,
    directory: PeopleDirectory,
    position: str,
    warn: Warn,
) -> "ScopeMatch | None":
    """Fold every named role into the ids it holds on this scope's platform.

    Returns the match with ``users`` and ``not_users`` widened by the roles, or
    ``None`` if the scope must be dropped. Runs after the channel checks above,
    because a role has no meaning without the platform whose ids it resolves to.

    **An undeclared role name is refused, and a declared one that resolves to
    nobody here is not.** They look alike -- both contribute no ids -- and they
    are different configs. A name that is in no ``roles:`` block is one this
    file cannot honour at all: it is a typo, or a role deleted from under a rule
    still using it, and reading it as "nobody" would make ``not: {role: admin}``
    exclude nobody and land the restriction on exactly the people it was written
    to exempt. The scope is dropped, which leaves the conversation on the layer
    below rather than under a rule nobody wrote -- the same direction every
    other refusal in this module takes, and the same one this axis already took
    while it was deferred.

    A *declared* role whose members simply have no id on this platform is a
    config that can be honoured, and its answer is the rule for an unnamed
    person: someone with no id for the current platform is not in the role
    there. So
    the positive half matches nobody and the negative half excludes nobody --
    which is bit for bit the answer the ``user`` axis already gives for a sender
    the connector could not name, and it is why this is a warning and not a
    refusal. Both directions are said out loud, because a restriction that
    applies to everyone including its intended exemptions is the failure section
    8.3 asks to be warned about by name.
    """
    if match.roles is None and match.not_roles is None:
        return match

    channel = match.channel
    named = (*(match.roles or ()), *(match.not_roles or ()))
    if _refuse_unknown_roles(
        named,
        directory=directory,
        position=position,
        label="match",
        consequence=_DROP_SCOPE,
        warn=warn,
    ):
        return None

    def _ids(names: "tuple[str, ...] | None", *, excluding: bool) -> "tuple[str, ...] | None":
        if names is None:
            return None
        found: set[str] = set()
        for name in names:
            ids = directory.ids_for_role(name, channel=channel)
            if ids:
                found.update(ids)
                continue
            elsewhere = ", ".join(directory.channels_for_role(name)) or "no platform"
            if excluding:
                warn(
                    "scopes%s.match.%s.%s=%s excludes nobody on %s: nobody in"
                    " that role has an id there, so the restriction applies to"
                    " them too. Add their %s id under %s: -- they are identified"
                    " on %s",
                    position,
                    MATCH_NOT,
                    AXIS_ROLE,
                    name,
                    channel,
                    channel,
                    PEOPLE_KEY,
                    elsewhere,
                )
            else:
                warn(
                    "scopes%s.match.%s=%s adds nobody on %s: nobody in that role"
                    " has an id there. Add their %s id under %s: -- they are"
                    " identified on %s",
                    position,
                    AXIS_ROLE,
                    name,
                    channel,
                    channel,
                    PEOPLE_KEY,
                    elsewhere,
                )
        return tuple(sorted(found))

    def _union(
        written: "tuple[str, ...] | None", from_roles: "tuple[str, ...] | None"
    ) -> "tuple[str, ...] | None":
        """The OR of the two spellings, as one set of ids (``IDENTITY_AXES``)."""
        if from_roles is None:
            return written
        return tuple(sorted(set(written or ()) | set(from_roles)))

    # Every axis carried through, and ``chat_type`` is the one worth naming:
    # resolution rebuilds the match rather than mutating it, so an axis left out
    # here is an axis the rule stops constraining. Dropping the kind turned
    # ``{channel, chat_type: im, role: admin}`` -- the admins, in direct messages
    # -- into the admins everywhere on the platform, which is a rule reaching
    # further than it was written to reach, decided by whether it happened to
    # mention a role. It also moved the rule down a layer, since the grain is
    # read off the match this returns.
    #
    # ``workspace`` is here for that reason and not for a reason of its own. The
    # failure is a property of the rebuild rather than of any one axis, so every
    # axis added below has to be added here in the same commit; the test over
    # this function's fields is what says so when one is not.
    return ScopeMatch(
        channel=match.channel,
        workspace=match.workspace,
        chat=match.chat,
        chat_type=match.chat_type,
        users=_union(match.users, _ids(match.roles, excluding=False)),
        not_users=_union(match.not_users, _ids(match.not_roles, excluding=True)),
        roles=match.roles,
        not_roles=match.not_roles,
    )


def _check_chat_type_value(
    match: ScopeMatch,
    capabilities: Any,
    *,
    position: str,
    warn: Warn,
) -> None:
    """Warn when ``chat_type`` names a kind of conversation this channel has not.

    Two failures, and they are told apart because the fixes differ. A channel
    that does not populate the axis at all has no kinds: every rule written on
    it is inert, whatever word it uses, and the fix is to stop writing the axis
    there. A channel that does populate it and does not know this word has a
    typo in the rule, and the fix is one of the words it does know -- which is
    why the declared vocabulary is printed. Nothing but a declaration can tell
    the two apart, which is why the vocabulary is declared rather than checked
    by a callback that could only answer yes or no.

    Warned rather than dropped, the treatment every other capability mismatch
    gets here: the rule is perfectly readable and it is the channel that cannot
    fill the axis, and a value no conversation carries matches nothing -- which
    is already the safe direction. A rule that matches nothing leaves its
    conversations on the layer below, exactly as a dropped one would.

    **Every entry is checked and the bad ones are named**, which is what the
    scalar case has always done, said once per rule rather than once per word.
    A third case appears with the list: some entries good and some not. That
    rule is not inert -- it still selects the kinds it got right -- so saying it
    "will never match" of it would be false and would send an operator looking
    for a rule that is in force. It gets its own sentence, naming the half that
    selects nothing and the half that still applies.
    """
    if not capabilities.populates(AXIS_CHAT_TYPE):
        warn(
            "scopes%s.match.%s is set but %s does not say what kind of"
            " conversation a message came from, so that scope will never match."
            " Match on the channel or the conversation instead",
            position,
            AXIS_CHAT_TYPE,
            match.channel,
        )
        return
    declared = capabilities.values_for(AXIS_CHAT_TYPE)
    if declared is None:
        return
    unknown = tuple(kind for kind in match.chat_type if kind not in declared)
    if not unknown:
        return
    known = tuple(kind for kind in match.chat_type if kind in declared)
    if known:
        warn(
            "scopes%s.match.%s names %s, which is not a kind of conversation %s"
            " has, so that part of the rule selects nothing; it still applies to"
            " %s. The kinds there are %s",
            position,
            AXIS_CHAT_TYPE,
            _describe_kinds(unknown),
            match.channel,
            _describe_kinds(known),
            ", ".join(sorted(declared)) or "none",
        )
        return
    warn(
        "scopes%s.match.%s=%s is not a kind of conversation %s has, so that"
        " scope will never match. The kinds there are %s",
        position,
        AXIS_CHAT_TYPE,
        _describe_kinds(unknown),
        match.channel,
        ", ".join(sorted(declared)) or "none",
    )


def _check_axes_against_capabilities(
    match: ScopeMatch, *, position: str, warn: Warn
) -> None:
    """Warn about a channel that has not opted in, or an axis it cannot fill."""
    if match.channel is None:
        return
    capabilities = channel_capabilities(match.channel)
    if capabilities is None:
        warn(
            "scopes%s.match.channel=%s is not a channel that supports scopes,"
            " so that scope is inert and will never apply. The channels that"
            " declare support are %s",
            position,
            match.channel,
            ", ".join(known_channels()) or "none",
        )
        return
    if match.chat is not None and not capabilities.populates(AXIS_CHAT):
        warn(
            "scopes%s.match.chat is set but %s does not identify a conversation,"
            " so that scope will never match. Match on channel alone",
            position,
            match.channel,
        )
    if match.workspace is not None and not capabilities.populates(AXIS_WORKSPACE):
        # The answer for a connector in exactly one workspace, as much as for one
        # that cannot say. Either way nothing tells the matcher which
        # installation a request came from, the rule fails closed, and naming the
        # channel alone is the rule that was meant.
        warn(
            "scopes%s.match.%s is set but %s does not say which workspace a"
            " request came from, so that scope will never match. Match on"
            " channel alone",
            position,
            AXIS_WORKSPACE,
            match.channel,
        )
    if match.chat_type is not None:
        _check_chat_type_value(
            match, capabilities, position=position, warn=warn
        )
    if match.constrains_identity and not capabilities.populates(AXIS_USER):
        # Two warnings, because the two halves fail in opposite directions and
        # an operator needs to be told which one happened. A positive identity
        # on a channel that names no sender matches nobody, so the rule is
        # inert. A negative one excludes nobody, so the rule applies to
        # everyone -- including the people it was written to exempt, which is
        # the failure worth naming.
        #
        # Warned rather than dropped, and ``role`` gets the same treatment as
        # ``user`` because it is the same axis. The config here is
        # perfectly readable -- it is the *channel* that cannot fill the axis --
        # so this is the class of mistake this function reports and the
        # per-scope refusals above are for the class it cannot read at all.
        # Giving one spelling of one axis a refusal where the other gets a
        # warning would be a second answer to one question.
        if match.users is not None or match.roles is not None:
            warn(
                "scopes%s.match.%s is set but %s does not identify a sender, so"
                " that scope will never match. Match on the conversation"
                " instead",
                position,
                _identity_spelling(match.users, match.roles),
                match.channel,
            )
        if match.not_users is not None or match.not_roles is not None:
            warn(
                "scopes%s.match.%s excludes senders but %s does not identify"
                " one, so nobody is excluded and that scope applies to everyone"
                " there -- including whoever it was written to exempt",
                position,
                MATCH_NOT,
                match.channel,
            )


def _effective_axis_restriction(
    section: str,
    key: str,
    capabilities: "Any | None",
) -> "AxisRestriction | None":
    """What both tables together say about how ``section.key`` may be addressed.

    :data:`MATCH_AXIS_RESTRICTIONS` speaks for the keys this module defines and
    for every channel, declared or not; the channel's own declaration speaks for
    the keys it declares. They are intersected rather than ordered, which is
    what makes "a connector may narrow, never widen" true by construction: there
    is no precedence to get the wrong way round, and a connector permitting an
    axis the schema bars simply does not get it.

    An axis name in a declaration that is not one of :data:`SUPPORTED_AXES` is a
    typo in that file, and it is already fail-closed -- an allow-list permits
    only what it names, so an unrecognised name permits nothing and the key ends
    up barred from a rule the author meant to allow. It is reported all the same,
    on the module logger, because otherwise the only evidence is a warning about
    a rule that looks correct. The audience is whoever is editing the
    declaration, not whoever wrote the config.
    """
    shared = restriction_in(MATCH_AXIS_RESTRICTIONS, section, key)
    declared = (
        capabilities.axis_restriction(section, key) if capabilities is not None else None
    )
    if declared is None:
        restriction = shared
    elif shared is None:
        restriction = declared
    else:
        restriction = declared.narrowed_by(shared)
    if restriction is None:
        return None

    unknown = sorted(restriction.allowed - set(SUPPORTED_AXES))
    if unknown:
        logger.warning(
            "scopes: the axis restriction on %s.%s permits %s, which %s not"
            " %s; the axes are %s. Nothing matches on it, so the restriction is"
            " narrower than it reads",
            section,
            key,
            ", ".join(unknown),
            "are" if len(unknown) > 1 else "is",
            "axes" if len(unknown) > 1 else "an axis",
            ", ".join(SUPPORTED_AXES),
        )
    return restriction


def _refuse_for_axis(
    restriction: AxisRestriction,
    matched_axes: "frozenset[str]",
    *,
    position: str,
    section: str,
    key: "str | None",
    warn: Warn,
) -> bool:
    """Warn and refuse if the rule is addressed on an axis ``section.key`` bars.

    Returns whether it refused, so the caller can drop the key -- or the whole
    section, when ``key`` is ``None``.

    **Whether dropping widens or narrows depends on the key, and is not knowable
    here.** For a restriction-shaped key the drop is a widening, and that is the
    direction this module otherwise refuses: a ``clicks`` clause dropped leaves
    the click gated by layer 0 alone, which is exactly what
    :func:`_settle_click_rule` says out loud about its own drops. For a
    value-shaped key -- ``model_name``, or anything a connector settles per
    conversation -- the drop leaves the conversation on the layer below, and
    whether that layer is wider or narrower than the refused rule depends on
    what the operator wrote there. So the sentence naming the direction is the
    declaration's ``because``, written by whoever knows which kind of key it is,
    and this function states only what happened.

    **Refusing is nonetheless the fail-closed answer, and for a reason that does
    not depend on the direction.** The rule cannot be honoured as written: its
    match addresses an axis the key has no meaning on. Keeping the value and
    ignoring the axis would apply a setting written for some requests to all of
    them -- the widening that actually matters, because it is one nobody wrote.
    Dropping grants nothing; it only declines to tighten, and a tightening that
    was wanted can be rewritten at a layer the key does accept.
    """
    refused = restriction.refuses(matched_axes)
    if not refused:
        return False
    warn(
        "scopes%s.%s%s cannot appear in a rule matching on %s. %s. %s. It may"
        " be addressed on %s",
        position,
        section,
        f".{key}" if key is not None else "",
        ", ".join(refused),
        (
            "Ignoring that key, which leaves that conversation on the layer"
            " below"
            if key is not None
            else "Ignoring the whole section, which leaves everything it set on"
            " the layer below"
        ),
        restriction.because or "That axis is not one this key has a meaning on",
        restriction.describe(),
    )
    return True


def _settle_permission_tools(
    raw: Any,
    *,
    position: str,
    channel: "str | None",
    attended: bool,
    warn: Warn,
) -> "dict[str, str] | None":
    """Settle ``permissions.tools``, or ``None`` if nothing in it survives.

    Per entry rather than per key, and this is the one place in this module
    where a partial result is the safe one. Everywhere else a bad value drops
    the whole key, because a half-read ``mode`` would answer messages nobody
    asked it to. Here the direction is reversed: the surviving entries are
    *restrictions*, so keeping them is the conservative reading and dropping
    the lot because one tool name was misspelled would quietly hand back
    permissions the operator believed they had taken away.
    """
    if not isinstance(raw, Mapping):
        warn(
            "scopes%s.%s.tools=%r is not a mapping of tool to allow/ask/deny;"
            " ignoring it, so nothing it named is restricted",
            position,
            SECTION_PERMISSIONS,
            raw,
        )
        return None

    settled: dict[str, str] = {}
    for raw_tool, raw_level in raw.items():
        tool = str(raw_tool).strip()
        if not tool:
            warn(
                "scopes%s.%s.tools has an entry with no tool name; ignoring it",
                position,
                SECTION_PERMISSIONS,
            )
            continue
        level = str(raw_level).strip().lower() if isinstance(raw_level, str) else ""
        if level not in PERMISSION_LEVELS:
            warn(
                "scopes%s.%s.tools.%s=%r is not one of %s; ignoring that entry,"
                " which leaves %s on whatever the permission config already"
                " said rather than on a level nobody wrote",
                position,
                SECTION_PERMISSIONS,
                tool,
                raw_level,
                "/".join(PERMISSION_LEVELS),
                tool,
            )
            continue
        if level == LEVEL_ALLOW:
            # Kept, not dropped: strictest(base, allow) is base, so dropping
            # it would only make the warning and the behaviour describe
            # different configs.
            warn(
                "scopes%s.%s.tools.%s=allow grants nothing: a scope can only"
                " tighten, so this leaves %s exactly as the permission config"
                " already had it. Remove it, or write ask/deny",
                position,
                SECTION_PERMISSIONS,
                tool,
                tool,
            )
        elif level == LEVEL_ASK and not attended:
            warn(
                "scopes%s.%s.tools.%s=ask asks a question nobody can answer on"
                " %s, so it is enforced as deny. Write deny if that is what was"
                " meant",
                position,
                SECTION_PERMISSIONS,
                tool,
                channel,
            )
        settled[tool] = level

    return settled or None


def _attached_reason(reason: str) -> str:
    """One refusal, joined to the key it is about the way it was written to be.

    A refusal reads as a continuation of the key -- ``=[a, b] is not a list of
    triggers`` -- or as a sentence about it -- ``names '++url', which is not a
    trigger``. The first wants no space in front of it and the second wants one,
    and which it is is settled by its first character.

    Deciding here rather than in each writer is what makes both spellings safe.
    A fixed separator in the caller cannot serve both: it produced
    ``delivery.mode  names '++url'`` for a writer that had supplied its own
    space, and ``delivery.mode ='all'`` for one that had not. Neither writer was
    wrong. A validator is written by whoever owns the key, which is a connector
    rather than this module, so a convention enforced by comment goes on being
    broken by people who never read it.
    """
    text = reason.lstrip()
    return text if text.startswith("=") else f" {text}"


def _settle_closed_word(
    key: str, value: Any, *, values: "tuple[str, ...]", setting: str
) -> "tuple[str | None, str]":
    """``(settled value, reason it was refused)`` for one word from ``values``.

    Shared by the ``delivery`` keys whose vocabulary this module owns rather
    than a connector: ``mid_turn``, ``session`` and ``reply``. Each is one word
    from a short closed list, and each wants the same three answers, so they ask
    once.

    Refuses rather than narrows, which is the idiom every other key here
    follows: an unrecognised value drops the key and leaves that conversation on
    the layer below, so a typo costs the setting rather than buying a behaviour
    nobody wrote. What the layer below is depends on where the typo was -- a
    conversation whose own scope is refused falls back to the platform scope,
    and a platform scope refused falls back to the key's default -- and that
    direction is the safe one: each default is what a connector implementing
    the key did before the key existed.

    Case and surrounding space are settled rather than refused. Each list is a
    closed vocabulary rather than an id belonging to somebody else, so there is
    nothing for ``Queue`` to collide with and nothing gained by making an
    operator find the capital letter -- while refusing it would silently leave a
    conversation on the layer below when the word it was written with was one
    of the right ones.

    An ``_append`` on either is refused outright, whatever it says. Appending is
    for prose, and appending to one of a handful of words produces one more that
    is not among them.
    """
    listed = ", ".join(values)
    if key.endswith(APPEND_SUFFIX):
        return None, (
            f" appends to a setting that is one of {listed}"
            f" and has nothing to append to. Ignoring it; write {setting}"
            f" with the value you want"
        )
    if not isinstance(value, str):
        return None, (
            f"={value!r} is not one of {listed}; ignoring it,"
            f" which leaves that conversation on the layer below"
        )
    settled = value.strip().lower()
    if settled not in values:
        return None, (
            f"={value!r} is not one of {listed}; ignoring it,"
            f" which leaves that conversation on the layer below rather than on"
            f" a behaviour nobody wrote"
        )
    return settled, ""


_DROP_CLAUSE = "Ignoring that clause, so nothing gates that click here"


def _settle_click_rule(
    raw: Any,
    *,
    position: str,
    kind: str,
    channel: "str | None",
    directory: PeopleDirectory,
    identifies_clicker: bool,
    warn: Warn,
) -> "ClickRule | None":
    """Settle one ``clicks.<gesture>`` clause, or ``None`` if it cannot be read.

    A clause names people the way a ``match`` does -- ``user``, ``role``, or both
    -- because a click is authorized like a sender: the same identity bag and the
    same roles, rather than a second principal model beside them.

    **A clause that cannot be read is dropped, and the drop is a widening.** That
    is said out loud in every warning below, because it is the one direction this
    module otherwise refuses: a ``clicks`` clause is a restriction, so ignoring
    it leaves the click on whatever layer 0 already gates rather than on the
    rule that was written. It must not be confused with a readable config that
    is missing a fact at click time: that one warns nothing at load and refuses
    the click, while this one warns at load and refuses no click. Refusing every
    click over a typo would take a deployment's approvals away for a mistake it
    can see and fix from the same warning.

    **``not`` is not offered.** Section 4.4 leaves ``not`` on non-identity axes
    open, and a click has no second axis to exclude on: "anyone but these people
    may approve" is the same permissive default `clicks` exists to close, written
    the long way round. Accepting it would answer a question this design has not
    asked.
    """
    if not isinstance(raw, Mapping):
        warn(
            "scopes%s.%s.%s=%r is not a mapping of who may click; write user: or"
            " role:. %s",
            position,
            SECTION_CLICKS,
            kind,
            raw,
            _DROP_CLAUSE,
        )
        return None
    if not raw:
        warn(
            "scopes%s.%s.%s is empty, which names nobody; it is not read as"
            " everyone. %s",
            position,
            SECTION_CLICKS,
            kind,
            _DROP_CLAUSE,
        )
        return None

    users: "tuple[str, ...] | None" = None
    roles: "tuple[str, ...] | None" = None
    for raw_key, raw_value in raw.items():
        key = str(raw_key).strip()
        label = f"{SECTION_CLICKS}.{kind}.{key}"
        if key == AXIS_USER:
            ids = _compile_ids(
                raw_value,
                position=position,
                label=label,
                warn=warn,
                consequence=_DROP_CLAUSE,
            )
            if ids is None:
                return None
            users = ids
            continue
        if key == AXIS_ROLE:
            names = _compile_role_names(
                raw_value,
                position=position,
                label=label,
                warn=warn,
                consequence=_DROP_CLAUSE,
            )
            if names is None:
                return None
            roles = names
            continue
        warn(
            "scopes%s.%s.%s.%s is not a way of naming who may click; a click"
            " names %s. %s",
            position,
            SECTION_CLICKS,
            kind,
            key,
            " or ".join(IDENTITY_AXES),
            _DROP_CLAUSE,
        )
        return None

    settled = set(users or ())
    if roles is not None:
        # The same refusal ``_resolve_roles`` makes on a ``match``, and for the
        # same reason. The consequence differs because the clause differs --
        # there is no scope to leave on the layer below, only a click that goes
        # back to being gated by layer 0 alone -- so it is passed in.
        if _refuse_unknown_roles(
            roles,
            directory=directory,
            position=position,
            label=f"{SECTION_CLICKS}.{kind}",
            consequence=_DROP_CLAUSE,
            warn=warn,
        ):
            return None
        for name in roles:
            ids = directory.ids_for_role(name, channel=channel)
            if ids:
                settled.update(ids)
                continue
            # Kept rather than dropped: a declared role whose members have no
            # id here holds nobody here, so it admits nobody here. Said out loud
            # because it is a live restriction that reads like a no-op.
            elsewhere = ", ".join(directory.channels_for_role(name)) or "no platform"
            warn(
                "scopes%s.%s.%s.%s=%s admits nobody on %s: nobody in that role"
                " has an id there, so every %s click there is refused. Add their"
                " %s id under %s: -- they are identified on %s",
                position,
                SECTION_CLICKS,
                kind,
                AXIS_ROLE,
                name,
                channel,
                kind,
                channel,
                PEOPLE_KEY,
                elsewhere,
            )

    if not identifies_clicker:
        # The half that is not "has no effect". A channel that renders no
        # buttons gets the ordinary ineffective-section warning from
        # ``_compile_section``, because there is no click to refuse. A channel
        # that renders them and names nobody has a click to refuse and refuses
        # it -- every one of them -- which is worth a different sentence.
        warn(
            "scopes%s.%s.%s says who may click but %s does not identify who"
            " clicked, so every %s click there is refused rather than gated."
            " That is not the same as %s rendering no such button, which would"
            " report the section as having no effect",
            position,
            SECTION_CLICKS,
            kind,
            channel,
            kind,
            channel,
        )

    return ClickRule(users=tuple(sorted(settled)), roles=roles or ())


def _compile_section(
    name: str,
    raw: Any,
    match: ScopeMatch,
    *,
    position: str,
    matched_axes: "frozenset[str]",
    channels_config: Mapping[str, Any],
    directory: PeopleDirectory = EMPTY_DIRECTORY,
    warn: Warn,
) -> dict[str, Any]:
    """Settle one section's keys, dropping the ones that cannot be honoured.

    ``directory`` is needed by ``clicks`` alone, which resolves a ``role`` of its
    own -- one that ``_resolve_roles`` cannot fold, because it names who may
    press a button rather than who the rule is about.

    ``matched_axes`` is passed in rather than read off ``match`` because by the
    time this runs the roles have been folded into ``users``, and a warning
    derived from that would name an axis the author's file does not contain.
    :func:`_match_axes` is taken before the fold, in :func:`compile_scopes`.
    """
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        warn(
            "scopes%s.%s=%r is not a mapping of settings; ignoring that section",
            position,
            name,
            raw,
        )
        return {}

    capabilities = (
        channel_capabilities(match.channel) if match.channel is not None else None
    )
    if capabilities is not None and not capabilities.reads(name):
        warn(
            "scopes%s.%s has no effect: %s does not read the %s section."
            " The sections it reads are %s",
            position,
            name,
            match.channel,
            name,
            ", ".join(sorted(capabilities.sections)) or "none",
        )
        return {}

    # Section-wide first, and dropping the section is the whole answer when it
    # fires. ``clicks`` is the entry that makes this concrete: a ``match``'s
    # identity axis names the sender of a *turn*, a ``clicks`` clause names the
    # person who pressed a button, and a rule naming both would be settled for
    # nobody -- inert, silently, in the one section written to restrict. That
    # was a hand-written branch here until the restriction table existed; it is
    # one row in that table now, so there is one mechanism and one place to read
    # what a section will accept.
    section_restriction = _effective_axis_restriction(name, ANY_KEY, capabilities)
    if section_restriction is not None and _refuse_for_axis(
        section_restriction,
        matched_axes,
        position=position,
        section=name,
        key=None,
        warn=warn,
    ):
        return {}

    connector_block = channels_config.get(match.channel) if match.channel else None
    connector_block = connector_block if isinstance(connector_block, Mapping) else {}

    settled: dict[str, Any] = {}
    for raw_key, value in raw.items():
        key = str(raw_key).strip()
        base_key = key[: -len(APPEND_SUFFIX)] if key.endswith(APPEND_SUFFIX) else key

        if capabilities is not None and not capabilities.acts_on(name, base_key):
            allowed = capabilities.keys_for(name)
            warn(
                "scopes%s.%s.%s has no effect: %s ignores it. The %s settings it"
                " reads are %s",
                position,
                name,
                key,
                match.channel,
                name,
                ", ".join(sorted(allowed)) if allowed else "none",
            )
            continue

        # Before the value is looked at, because how the rule was addressed is
        # settled whatever the value says: a key barred from ``user`` is barred
        # there whether the value is well formed or not, and checking the value
        # first would warn twice about one rule that has one thing wrong with
        # it. On ``base_key``, so that ``x_append`` is restricted exactly as
        # ``x`` is -- an append is a way of writing the key, not a second key.
        key_restriction = _effective_axis_restriction(name, base_key, capabilities)
        if key_restriction is not None and _refuse_for_axis(
            key_restriction,
            matched_axes,
            position=position,
            section=name,
            key=key,
            warn=warn,
        ):
            continue

        if key.endswith(APPEND_SUFFIX):
            if not isinstance(value, str):
                warn(
                    "scopes%s.%s.%s=%r is not text; %s appends to %s and has"
                    " nothing else to mean. Ignoring it",
                    position,
                    name,
                    key,
                    value,
                    key,
                    base_key,
                )
                continue
        elif isinstance(value, (list, tuple)):
            signed = signed_entries(value)
            if signed == ():
                warn(
                    "scopes%s.%s.%s=%s mixes plain entries with +/- ones, which"
                    " could mean either replacing the set or changing it."
                    " Ignoring it; write every entry signed to change the"
                    " inherited set, or none of them to replace it",
                    position,
                    name,
                    key,
                    _describe_list_mix(value),
                )
                continue
            if signed is not None and any(len(entry) < 2 for entry in signed):
                warn(
                    "scopes%s.%s.%s=%s has a sign with nothing after it;"
                    " ignoring it",
                    position,
                    name,
                    key,
                    _describe_list_mix(value),
                )
                continue
            if signed is None and any(not isinstance(item, str) for item in value):
                warn(
                    "scopes%s.%s.%s=%s is not a list of names; ignoring it",
                    position,
                    name,
                    key,
                    _describe_list_mix(value),
                )
                continue

        if name == SECTION_CLICKS:
            if base_key not in CLICK_KEYS:
                warn(
                    "scopes%s.%s.%s is not a click this version can gate; the"
                    " clicks are %s. Ignoring it",
                    position,
                    name,
                    key,
                    ", ".join(sorted(CLICK_KEYS)),
                )
                continue
            rule = _settle_click_rule(
                value,
                position=position,
                kind=key,
                channel=match.channel,
                directory=directory,
                identifies_clicker=(
                    capabilities.populates(AXIS_USER)
                    if capabilities is not None
                    else True
                ),
                warn=warn,
            )
            if rule is None:
                continue
            settled[key] = rule
            continue

        if name == SECTION_PERMISSIONS:
            if base_key not in PERMISSION_KEYS:
                warn(
                    "scopes%s.%s.%s is not a permissions setting; the settings"
                    " are %s. Ignoring it",
                    position,
                    name,
                    key,
                    ", ".join(sorted(PERMISSION_KEYS)),
                )
                continue
            settled_tools = _settle_permission_tools(
                value,
                position=position,
                channel=match.channel,
                attended=capabilities.attended if capabilities is not None else True,
                warn=warn,
            )
            if settled_tools is None:
                continue
            settled[key] = settled_tools
            continue

        # Checked here rather than through a channel's declaration, because
        # unlike every other value in this loop the vocabulary is not the
        # channel's. ``mid_turn``'s three words name mechanisms, ``session``'s
        # two name how wide a session is, and ``reply``'s two name whether an
        # answer is owed -- and a connector implementing any of them implements
        # the same words. A channel that does not implement one at all has
        # already dropped the key above, on ``acts_on``.
        closed = _DELIVERY_VOCABULARIES.get(base_key) if name == SECTION_DELIVERY else None
        if closed is not None:
            settled_word, refusal = _settle_closed_word(
                key, value, values=closed, setting=base_key
            )
            if settled_word is None:
                warn(
                    "scopes%s.%s.%s%s",
                    position,
                    name,
                    key,
                    _attached_reason(refusal),
                )
                continue
            value = settled_word

        validator = capabilities.validator(name, base_key) if capabilities else None
        if validator is not None:
            reason = validator(value)
            if reason:
                warn(
                    "scopes%s.%s.%s%s",
                    position,
                    name,
                    key,
                    _attached_reason(reason),
                )
                continue

        # After the validator, and with no ``continue`` after it. Both are the
        # contract rather than an ordering that happened: there is nothing to
        # say about the reach of a value that is not going to be kept, and what
        # a caution reports is that no request will arrive at a value that is
        # otherwise correct -- which costs nothing at runtime and is not worth
        # taking the rest of the rule down for. It is the only thing a channel
        # reads off the match, and it reads it to warn rather than to decide.
        caution = capabilities.caution(name, base_key) if capabilities else None
        if caution is not None:
            note = caution(value, match)
            if note:
                warn(
                    "scopes%s.%s.%s%s",
                    position,
                    name,
                    key,
                    _attached_reason(note),
                )

        # Only a scope that reaches every conversation. A platform-only scope is
        # layer 1 sitting directly on layer 0, and setting the same key in both
        # leaves the connector value governing nothing -- that is the redundancy
        # worth a line. A scope that names a conversation is not redundant with
        # anything: layer 0 has no per-conversation form, and the connector
        # setting still governs every other conversation, so saying it had gone
        # dead would be false as well as noisy.
        #
        # Naming a kind is the same case as naming a conversation, for the same
        # reason: a rule matching {chat_type: channel} leaves every conversation
        # of another kind on layer 0, so the connector setting still governs
        # them. On Slack that is every DM, which is the majority of the
        # conversations a deployment has. The test is therefore whether the rule
        # selects on the conversation axis at all, not whether it names one.
        #
        # Naming a workspace is the same case again. A rule matching
        # {workspace: T} leaves every conversation in every other workspace on
        # layer 0, so the connector setting still governs those and has not gone
        # dead. That a connector may be in only one workspace does not change
        # the reading, any more than a platform with one kind of conversation
        # changes the reading above: the test is structural, and the alternative
        # is a warning whose truth depends on a fact no config file holds.
        if (
            capabilities is not None
            and match.chat is None
            and match.chat_type is None
            and match.workspace is None
        ):
            layer0 = capabilities.layer0_key(name, base_key)
            if layer0 is not None and connector_block.get(layer0) not in (None, "", [], {}):
                warn(
                    "scopes%s.%s.%s and channels.%s.%s both set this, and the"
                    " scope is above it for every conversation, so the connector"
                    " setting now governs nothing. Two places to look for one"
                    " value; keep whichever one is meant",
                    position,
                    name,
                    key,
                    match.channel,
                    layer0,
                )

        settled[key] = value

    return settled


def compile_scopes(
    raw: Any,
    *,
    channels_config: "Mapping[str, Any] | None" = None,
    people: Any = None,
    roles: Any = None,
    directory: "PeopleDirectory | None" = None,
    warn: "Warn | None" = None,
) -> tuple[Scope, ...]:
    """Read the ``scopes:`` list into rules, warning about what it cannot honour.

    ``channels_config`` is the ``channels`` block, used for one check only:
    whether a key a scope sets is also set on the connector it sits above.

    ``people`` and ``roles`` are the two top-level blocks of the same name, read
    here rather than by each caller so that the directory and the rules that
    depend on it are compiled together and warn together. A caller that has
    already built a :class:`~jiuwenswarm.common.scopes.people.PeopleDirectory`
    passes it as ``directory`` instead; passing both prefers the built one, and
    a caller passing neither gets a config in which every ``role:`` is
    undeclared -- which is why the two call sites that read a whole config file
    pass them and no caller may quietly skip them.

    ``warn`` is injected rather than taken from this module's logger so a test
    can read what was said. jiuwenswarm's loggers do not propagate, so ``caplog``
    sees nothing under the pytest CI runs on, and a test written against it
    would pass locally and assert nothing where it matters.
    """
    emit: Warn = warn if warn is not None else logger.warning
    config = channels_config if isinstance(channels_config, Mapping) else {}
    if directory is None:
        directory = (
            compile_people(people, roles, warn=emit)
            if (people is not None or roles is not None)
            else EMPTY_DIRECTORY
        )

    if raw is None:
        return ()
    if not isinstance(raw, (list, tuple)):
        emit(
            "scopes=%r is not a list of rules; ignoring it. Every rule is a"
            " list entry pairing a match with the sections it sets",
            raw,
        )
        return ()

    compiled: list[Scope] = []
    for index, entry in enumerate(raw):
        position = f"[{index}]"
        if entry is None:
            continue
        if not isinstance(entry, Mapping):
            emit(
                "scopes%s=%r is not a rule; a rule is a mapping with a match and"
                " the sections it sets. Ignoring it",
                position,
                entry,
            )
            continue

        for unknown in sorted(
            str(key) for key in entry if str(key) not in ENTRY_KEYS
        ):
            emit(
                "scopes%s.%s is not part of a rule and has no effect; a rule"
                " carries match and %s",
                position,
                unknown,
                ", ".join(SUPPORTED_SECTIONS),
            )

        match = _compile_match(entry.get("match"), position=position, warn=emit)
        if match is None:
            continue
        if not _refuse_chat_with_chat_type(match, position=position, warn=emit):
            continue
        if not _require_channel_for_chat(match, position=position, warn=emit):
            continue
        if not _require_channel_for_workspace(match, position=position, warn=emit):
            continue
        if not _require_channel_for_identity(match, position=position, warn=emit):
            continue
        # Before resolution, so that a warning names the spelling the author
        # wrote: after it, a role-only match holds a ``users`` tuple and
        # would be reported as a ``user:`` line that is not in their file.
        _check_axes_against_capabilities(match, position=position, warn=emit)
        # Taken here, before the roles are folded into ``users``, so that a
        # refusal names the spelling the author wrote -- the same reason the
        # check above runs before the fold.
        matched_axes = _match_axes(match)
        resolved = _resolve_roles(
            match, directory=directory, position=position, warn=emit
        )
        if resolved is None:
            continue
        match = resolved

        for deferred in DEFERRED_SECTIONS:
            if entry.get(deferred) is not None:
                emit(
                    "scopes%s.%s is not read in this version and has no effect."
                    " Nothing enforces it, so do not rely on it to restrict"
                    " anything",
                    position,
                    deferred,
                )

        sections: dict[str, Mapping[str, Any]] = {}
        for name in SUPPORTED_SECTIONS:
            if name not in entry:
                continue
            settled = _compile_section(
                name,
                entry.get(name),
                match,
                position=position,
                matched_axes=matched_axes,
                channels_config=config,
                directory=directory,
                warn=emit,
            )
            if settled:
                sections[name] = settled

        if not sections:
            continue
        compiled.append(Scope(match=match, sections=sections, index=index))

    return tuple(compiled)


def matching_scopes(
    scopes: Sequence[Scope],
    *,
    channel: "str | None",
    chat: "str | None" = None,
    chat_type: "str | None" = None,
    workspace: "str | None" = None,
    user: "str | None" = None,
    section: str = SECTION_DELIVERY,
) -> tuple[Scope, ...]:
    """The scopes that apply, ordered so that folding them left is the cascade.

    Sorted by specificity first and by file position second. Specificity is the
    grain tuple, compared lexicographically: ``{channel, chat}`` sits above
    ``{channel, chat_type}``, which sits above ``{channel}``, whichever order
    they were written in -- the precedence a per-conversation rule has always
    had against ``channels.<platform>``, now with the kind of conversation
    between the two, and with ``workspace`` above all three. What each component
    means is in :attr:`ScopeMatch.specificity`.

    Position second settles what the tuple deliberately leaves tied, which is
    every pair that cannot be ordered without knowing something the rules do not
    say: a role against a list of ids, or a pure negative against either.

    ``user`` is the sender, read out of the identity bag by whoever is asking
    and not assumed to be any particular field. ``chat_type`` is the kind of
    conversation, from the vocabulary that platform declares -- one kind, since
    a request comes from one conversation, matched against however many kinds a
    rule names. Both are left
    ``None`` by every caller that has none to offer, and both then fail closed:
    scopes naming people or naming a kind do not fire, scopes excluding people
    still do. ``workspace`` is the installation the request arrived from and is
    the third of them, with the same answer for a caller that has none.
    """
    applicable = [
        scope
        for scope in scopes
        if scope.section(section)
        and scope.selects(
            channel=channel,
            chat=chat,
            chat_type=chat_type,
            workspace=workspace,
            user=user,
        )
    ]
    applicable.sort(key=lambda scope: (scope.layer, scope.index))
    return tuple(applicable)
