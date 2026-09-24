# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.

"""What a room keeps of the Slack events that are not messages, and how.

Reactions, pins and membership changes are **state changes, not utterances**,
and somebody opening this app's App Home is an arrival rather than either.
Nobody addressed the bot by adding an emoji, and nobody addressed it by walking
into the room. The first thing a room can do with
one is keep it, as context a turn started for its own reasons would have been
better for: the model can already read reactions through
``read_slack_conversation``, so the problem that solves is *salience* rather
than capability -- it will not fetch what it does not think to fetch.

The second thing a room can do with one, where an operator asked for it, is
start a turn over it. That is the narrower and the later of the two, and what
it does not cover is written out below rather than left to be discovered.

Two things live here rather than in the connector.

**The vocabulary**, because three readers have to agree on it: the scope loader,
which checks what an operator wrote; the connector, which acts on it; and the
config template, which documents it. ``scope_capabilities`` in particular may
import nothing heavy -- it runs during registry discovery in every process,
Slack configured or not -- and this module imports only the standard library.

**The buffer**, because it is the half with behaviour worth testing on its own.
It has no Slack client, no bolt, no config: events in, one payload out.

Families, not event types
=========================

The keys are *families*. Three of them cover the pair of Slack events that
report the two directions of one state change: ``reaction`` covers
``reaction_added`` and ``reaction_removed``, ``pin`` covers ``pin_added`` and
``pin_removed``, ``member`` covers ``member_joined_channel`` and
``member_left_channel``.

Nobody wants one half. A room that records additions and drops removals leaves
the model believing a stale picture is the current one -- it sees the emoji that
was taken back, the pin that was removed, the person who left -- which is worse
than seeing neither, because it looks like knowledge.

The fourth family covers one event type, and that is a fact about Slack rather
than a gap here: ``app_home`` covers ``app_home_opened``, and Slack publishes
nothing for the other direction -- there is no closing event, so there is no
half to lose. The family is still the unit an operator writes, because an
operator writes one word per subject rather than one word per event type, and
the subject is somebody arriving in this app's own space.

Three words
===========

``off`` drops on arrival. ``context`` buffers, for the next turn to carry.
``turn`` starts a turn over the event itself.

``turn`` is the word this module used to say did not exist. The reason given
for its absence was that an event-woken turn has no message to anchor it and
therefore nowhere obvious to put an answer. The first half of that is true of
one family; the second half was wrong about all three.

**Two anchors, not one.** A reaction and a pin are *about a message*:
``item.ts`` names it, so there is a thread to answer in, and the turn is routed
exactly the way an inbound message in that thread is routed -- the same
delivery metadata, the same session key, the same reply thread. A membership
change is about a *room*. It names no message and none is invented for it:
:func:`event_anchor` returns nothing for ``member``, which is the correct
answer and stays the correct answer. What a member turn answers into is the
channel itself, at top level, through ``post_as_root`` -- a destination this
connector already has, already names and already delivers to, rather than one
invented here.

So :data:`EVENT_TURN_FAMILIES` is those three. It stays written out as an
explicit set rather than as "every family", because a new family arrives with
neither anchor settled, and the gate is what makes somebody decide which of the
two it is. The fourth family is what that gate was written for.

The family that does not take ``turn``
--------------------------------------

``app_home`` takes ``off`` and ``context``. ``turn`` is refused for it where an
operator writes it, and the refusal carries the reason below rather than a
sentence about families in general -- see :data:`EVENT_TURN_WITHHELD`.

**A turn woken by it has no message to answer.** So far that is ``member``'s
position too, and it is not what settles this: ``member`` answers at the top of
the channel it is about. The natural answer to somebody opening the Home tab is
to publish a view into that tab, and ``publish_slack_home_tab`` does exactly that.
What the family still lacks is a *destination*: the two a turn here is routed
to are a message's thread and a channel's top level, and a published view is
neither.

**And the coherent answer differs by ``tab``, which nothing else here does.**
With ``tab: "messages"`` the direct message channel is a real destination and a
reply in it is the same reply a message there would get. With ``tab: "home"``
the person is looking at a published view, and replying in the direct message
because somebody clicked a tab answers a question nobody asked, somewhere they
were not looking. Splitting one family's behaviour on a payload field is
something no family does, and it is a decision to take deliberately rather than
one to fall into now that a tool publishes the view.

So the word is **absent, not unimplemented**, which is the stance the rest of
this module takes: what is not covered is written down rather than left to be
discovered. ``context`` is the whole of what this family does, and it is the
setting the family was added for.

**Nothing this bot does produces one of these events.** The self-filter below
exists for a real loop in the other three families -- this connector marks every
inbound message with a reaction, so a room set to ``context`` would otherwise be
fed the bot's own acknowledgement of the message that started the turn. App Home
is opened by a person in a Slack client, and this app has no client; publishing
a view writes the tab and does not open it. The filter still runs, because it
runs before the family is consulted at all, and it is a floor here rather than a
fix for anything.

What ``turn`` does not cover
----------------------------

**Not a message that is not there.** An event whose family anchors on a message
and which still names none -- a reaction added to a *file* is the case -- wakes
no turn. The family says where an answer would go; that event has no message
for the family's answer to be about, and the channel root is the member
family's destination rather than a fallback for the other two.

**Not a second turn.** An event arriving while the session it would wake is
already working is folded into the context buffer instead, and the turn already
running carries it. Two turns in one conversation is the failure a reaction
storm would produce most, and there is no mid-turn word for an event: ``queue``,
``cancel`` and ``steer`` are defined for messages, and an event is not one.

**Not a reply obligation, and not an exemption from one.** Where the
conversation's ``delivery.reply`` is ``required`` -- which is the default --
``turn`` is treated as ``context``. A required reply would oblige the turn to
say something about an emoji nobody asked about. That treatment is conservative
rather than settled; the connector holds it in one place and says so there.

**Not a licence to act anywhere else.** A turn woken by an event may answer
where its family's anchor says -- the message's thread, or the channel's top
level -- or stay silent. There is no other destination a model can reach from a
turn, and nothing this module or the connector appends to such a turn says
otherwise.

**Unset means ``off``**, so a deployment that never writes the key sees no
change at all. That is true of all three words: ``turn`` costs nothing until an
operator writes it.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Mapping
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Any

logger = logging.getLogger(__name__)

#: The ``delivery`` key this module's vocabulary belongs to. Spelled here, the
#: way ``KEY_HISTORY`` is spelled in ``slack_history_policy``, so that the
#: declaration, the connector and the config template name one string.
KEY_EVENTS = "events"

EVENT_FAMILY_REACTION = "reaction"
EVENT_FAMILY_PIN = "pin"
EVENT_FAMILY_MEMBER = "member"
#: Somebody opening this app's App Home -- the space holding the Home tab and
#: the direct message with this app. Named for the surface rather than for the
#: event type, the way the other three are: ``app_home`` is Slack's own word for
#: it, and it is the word an operator has already read in the app's settings.
EVENT_FAMILY_APP_HOME = "app_home"

#: The families a rule may name, in the order they are documented.
EVENT_FAMILIES: tuple[str, ...] = (
    EVENT_FAMILY_REACTION,
    EVENT_FAMILY_PIN,
    EVENT_FAMILY_MEMBER,
    EVENT_FAMILY_APP_HOME,
)

EVENT_OFF = "off"
EVENT_CONTEXT = "context"
EVENT_TURN = "turn"

#: What a family may be set to, widest last. See the module docstring for what
#: each word does and for what ``turn`` deliberately does not do.
EVENT_DISPOSITIONS: tuple[str, ...] = (EVENT_OFF, EVENT_CONTEXT, EVENT_TURN)

#: The families ``turn`` may be written on: three of the four.
#:
#: **Written out rather than spelled "every family", and the difference is the
#: point.** A turn woken by an event has to answer somewhere, and where is a
#: property of the family: see :data:`EVENT_ANCHORED_FAMILIES` for the two that
#: answer in a message's thread and the one that answers at the top of the
#: channel. A new family arrives with that question unanswered, and a membership
#: test it is outside is what makes somebody answer it instead of inheriting
#: whichever destination the code happened to reach first.
#:
#: ``app_home`` is the family that answered it the other way. Neither of the two
#: destinations is right for somebody opening a tab, and which of them is even
#: coherent depends on the ``tab`` field. :data:`EVENT_TURN_WITHHELD` carries
#: that reason in the words an operator is shown, and the module docstring
#: carries the argument.
#:
#: Spelled out one name at a time rather than built from :data:`EVENT_FAMILIES`.
#: Deriving it would make a new family take ``turn`` the moment it was declared,
#: which is the decision this set exists to force somebody to take -- and the
#: fourth family is the first one to have taken it the other way.
EVENT_TURN_FAMILIES: frozenset[str] = frozenset(
    {EVENT_FAMILY_REACTION, EVENT_FAMILY_PIN, EVENT_FAMILY_MEMBER}
)

#: The sentence an operator gets when ``turn`` is refused for a family, keyed by
#: the family it is about.
#:
#: One clause per family outside :data:`EVENT_TURN_FAMILIES`, so a refusal says
#: what is wrong with *this* family rather than repeating a general statement
#: about event turns. The reason lives here rather than in the loader because
#: the decision lives here: this is the module that says which families take the
#: word, and the two would drift if one of them were written somewhere else.
EVENT_TURN_WITHHELD: Mapping[str, str] = MappingProxyType(
    {
        EVENT_FAMILY_APP_HOME: (
            "a turn woken by somebody opening App Home has no message to"
            " answer, and which destination would even be coherent depends on"
            " the tab that was opened: the Messages tab is the direct message"
            " conversation, while a reply there because somebody clicked the"
            " Home tab answers a question nobody asked. No family splits on a"
            " payload field, and nobody has decided that this one should"
        ),
    }
)

#: What a family nobody wrote a reason for is told. The refusal stands whether
#: or not the reason was written; what is missing in that case is the argument,
#: not the gate, and saying so is more honest than inventing a specific reason.
TURN_WITHHELD_UNDECIDED = (
    "a turn woken by an event has to answer somewhere, and nothing says where a"
    " turn woken by one of these would post"
)


def turn_withheld_reason(family: str) -> str:
    """Why ``family`` cannot be given ``turn``, in a sentence for an operator.

    Answers for any family, including one that can take the word: the caller is
    a refusal path that has already decided the word does not hold, and it
    wants a sentence rather than a second opinion.
    """
    return EVENT_TURN_WITHHELD.get(family) or TURN_WITHHELD_UNDECIDED

#: The families whose events are about a message, as against about a room.
#:
#: A reaction and a pin both name the message they are about, and that message
#: is the destination: a turn woken by one answers in its thread. A membership
#: change names a room and nothing in it, so a turn woken by one answers at the
#: channel's top level, through the connector's existing ``post_as_root``.
#:
#: **Nothing is fabricated for an unanchored family.** :func:`event_anchor`
#: returns no ts for ``member`` and none for ``app_home``, and standing in the
#: event's own timestamp would put an identifier in the model's hands that
#: addresses no message and that no tool accepts back.
#:
#: ``app_home`` is unanchored and, unlike ``member``, has no answering
#: destination either. It is outside :data:`EVENT_TURN_FAMILIES` for that
#: reason, so nothing ever asks this set about it on a path that would post.
#:
#: The narrow set, for the reason above: a new family has to be classed here
#: before a turn woken by it knows where to answer.
EVENT_ANCHORED_FAMILIES: frozenset[str] = frozenset(
    {EVENT_FAMILY_REACTION, EVENT_FAMILY_PIN}
)


def family_anchors_on_a_message(family: str) -> bool:
    """Whether a turn woken by this family answers in a message's thread.

    ``False`` means the channel's top level, which is where a turn about the
    room itself belongs. Asked in one place so the routing, the instruction
    text and the metadata cannot come to three different answers about one
    event.
    """
    return family in EVENT_ANCHORED_FAMILIES


#: What a family nobody spoke about does. ``off``, so the feature costs nothing
#: -- not a turn, not a byte of context -- until somebody asks for it.
EVENT_DISPOSITION_DEFAULT = EVENT_OFF

#: Which family each Slack event type belongs to. The connector registers one
#: bolt listener per key here, so this mapping is also the list of event types
#: this connector reads at all. Both members of every pair are present, which is
#: the property the family idea exists to keep true; ``app_home`` is one event
#: type because Slack publishes one, not because a direction was dropped.
EVENT_TYPE_FAMILIES: Mapping[str, str] = MappingProxyType(
    {
        "reaction_added": EVENT_FAMILY_REACTION,
        "reaction_removed": EVENT_FAMILY_REACTION,
        "pin_added": EVENT_FAMILY_PIN,
        "pin_removed": EVENT_FAMILY_PIN,
        "member_joined_channel": EVENT_FAMILY_MEMBER,
        "member_left_channel": EVENT_FAMILY_MEMBER,
        # No scope. Slack delivers this one to any app with a bot user, which
        # makes it the only entry here whose absence is a subscription and
        # nothing else -- see slack_scope_policy for what that costs an install.
        "app_home_opened": EVENT_FAMILY_APP_HOME,
    }
)

#: How many events of each family one conversation may hold between two turns.
#:
#: **Per family, never one shared cap.** The families burst at wildly different
#: rates: a single popular message collects reactions for as long as people keep
#: reading it, while a pin or a join is something somebody decided to do. One
#: queue would let the noisy family starve the quiet one -- an emoji storm would
#: evict the join that explains who the new person in the room is, which is the
#: event most worth keeping.
#:
#: The numbers are chosen against what a burst of each actually looks like and
#: against what the fold costs in context, not against a round number.
#:
#: ``reaction`` 30: an emoji storm on one or two messages, which is the shape
#: reaction traffic has. Past thirty the room is reacting rather than saying
#: anything, and the thirty most recent say so as well as three hundred would.
#:
#: ``pin`` 10: a pin is deliberate and rare. Ten between two turns is a room
#: reorganising its pins, and the dropped count says that happened better than
#: the eleventh record would.
#:
#: ``member`` 20: membership changes come in bulk exactly once, when a batch of
#: people is invited at the same time, and twenty covers an invite round.
#:
#: ``app_home`` 5, and it is the smallest bound for the family with the highest
#: rate, which is not a contradiction. This is the only event here that nobody
#: did *to* anything: it fires on every entry to App Home, for both tabs, so
#: somebody moving between the Home tab and the Messages tab while they think
#: produces a record per move -- a rate no other family reaches. What the
#: records say does not grow with the count. An App Home belongs to one person,
#: so every record in one conversation names that same person, and the only
#: field that varies is ``tab``. The fifth entry tells the model nothing the
#: first did not, and the dropped count states the volume better than a sixth
#: identical record would. Five rather than one, so that a change of tab is
#: still visible as a change and a burst still reads as a burst.
#:
#: Worst case the whole fold is sixty-five records, which is a few kilobytes of
#: context on a turn that was going to run anyway.
EVENT_BUFFER_LIMITS: Mapping[str, int] = MappingProxyType(
    {
        EVENT_FAMILY_REACTION: 30,
        EVENT_FAMILY_PIN: 10,
        EVENT_FAMILY_MEMBER: 20,
        EVENT_FAMILY_APP_HOME: 5,
    }
)

#: How long a buffered event stays worth folding. One hour.
#:
#: A count bound alone is not enough, and the two answer different questions. A
#: bound says the room was busier than the buffer; a deadline says the events are
#: stale. In a quiet conversation the bound never fires at all, so without this
#: a reaction from two days ago would be folded into tomorrow morning's first
#: turn as though it had just happened -- noise regardless of whether there was
#: room for it.
#:
#: An hour is where a reaction stops being a response to the live conversation
#: and becomes archaeology. It is deliberately not tied to a session lifetime:
#: a session can outlive its relevance by days.
EVENT_BUFFER_TTL_SECONDS = 3600.0

#: Why a record is not in the fold. Reported per family so the two stay apart:
#: ``overflow`` says the room out-ran the bound, ``expired`` says the events were
#: older than the deadline, and the reader does different things about each.
DROP_OVERFLOW = "overflow"
DROP_EXPIRED = "expired"


def normalize_event_policy(raw: Any) -> "dict[str, str] | None":
    """One ``delivery.events`` mapping as families to dispositions, or ``None``.

    ``None`` for anything that is not a mapping at all, which is the shape the
    connector reads as "no layer spoke about this". An entry naming an unknown
    family or an unknown disposition is dropped from the result rather than
    defaulted: the loader has already refused such a value with a warning naming
    it, and this is the second gate for a mapping that reached here from
    somewhere other than the loader.

    Case, surrounding space and the YAML 1.1 reading of ``off`` are settled by
    ``disposition_word``, unlike the connector ``validator``, which may only say
    whether a value is usable. A mapping that passes through here is canonical.
    """
    if not isinstance(raw, Mapping):
        return None
    settled: dict[str, str] = {}
    for family, disposition in raw.items():
        name = str(family or "").strip().lower()
        word = disposition_word(disposition)
        if name in EVENT_FAMILIES and disposition_holds(name, word):
            settled[name] = word
    return settled


def disposition_word(value: Any) -> str:
    """One raw disposition as the word it was written as, or ``""``.

    Case and surrounding space are settled here, and so is the one value that
    reaches every reader as something other than the word on the page:
    ``config.yaml`` is loaded with a YAML 1.1 parser, under which an unquoted
    ``off`` is the boolean ``False``. Reading it as a plain string refuses the
    whole mapping, so ``events`` written the way the config template's own
    example writes it cost an operator the families they had spelled correctly
    beside it -- ``reaction: turn`` dropped because ``member: off`` came out a
    boolean.

    ``True`` is left unrecognised rather than mapped. The vocabulary has no
    ``on``, so there is no word for it to have been written as, and a refusal
    naming it is the honest answer.

    One function for the three readers the module docstring names, so the
    loader's check, the normaliser and the read gate cannot come to differ about
    which values are the same value.
    """
    if isinstance(value, bool):
        return EVENT_OFF if value is False else ""
    return str(value or "").strip().lower()


def disposition_holds(family: str, word: str) -> bool:
    """Whether ``word`` is a disposition ``family`` can actually be given.

    Both halves of the vocabulary in one predicate, so the loader's validator,
    the normaliser and the read gate cannot disagree about ``member: turn``.
    A word outside the three is not a disposition at all; ``turn`` on a family
    with no message to anchor a reply to is a disposition that family cannot
    have, and the two are one question at every reader.
    """
    if word not in EVENT_DISPOSITIONS:
        return False
    return word != EVENT_TURN or family in EVENT_TURN_FAMILIES


def event_disposition(policy: "Mapping[str, str] | None", family: str) -> str:
    """What one conversation does with one family. ``off`` unless told otherwise.

    A word this family cannot have falls back to the default rather than to the
    next word down. ``member: turn`` is refused where it is written, with a
    warning naming it, so reaching here means the mapping was built by something
    other than the loader -- and the honest answer to a mapping nobody vetted is
    the floor, not a value guessed at from what the operator seemed to mean.
    """
    if not isinstance(policy, Mapping):
        return EVENT_DISPOSITION_DEFAULT
    word = disposition_word(policy.get(family))
    return word if disposition_holds(family, word) else EVENT_DISPOSITION_DEFAULT


def iso_utc(value: "float | None") -> "str | None":
    """One instant as ``2026-09-11T12:00:00+00:00`` spelled with a ``Z``.

    The same rendering ``slack_history`` gives its ``*_iso_utc`` fields, spelled
    again here rather than imported because that module belongs to the runtime
    and pulls its whole toolkit in with it, while this one is read during
    registry discovery. Three lines in two places is the cheaper of the two
    costs, and the format is fixed by what has already shipped.
    """
    if value is None:
        return None
    return datetime.fromtimestamp(value, tz=timezone.utc).isoformat().replace(
        "+00:00", "Z"
    )


def _float_or_none(value: Any) -> "float | None":
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def event_family(event: "Mapping[str, Any] | None") -> "str | None":
    """The family one Slack event belongs to, or ``None`` for a type not read here."""
    if not isinstance(event, Mapping):
        return None
    return EVENT_TYPE_FAMILIES.get(str(event.get("type") or "").strip())


def event_actor_id(event: "Mapping[str, Any] | None") -> str:
    """Who *did* the thing, which is never the author of the thing done to.

    The reacting user, the pinning user, the joining or leaving user, the user
    who opened App Home. Slack writes all five into ``user`` on these seven
    event types.

    The distinction is the one the self-filter depends on. ``reaction_added``
    also carries ``item_user`` -- the author of the message reacted to -- and a
    filter written against that one would be filtering the wrong person: it
    would drop everyone else's reactions to the bot's own answers and keep the
    bot's own reactions to everyone else's messages, which is exactly backwards
    and is the loop that matters, since this connector marks every inbound
    message with a reaction of its own.
    """
    if not isinstance(event, Mapping):
        return ""
    return str(event.get("user") or "").strip()


def event_chat_id(event: "Mapping[str, Any] | None") -> str:
    """Which conversation the event happened in.

    Three spellings because Slack uses three. A reaction names the conversation
    inside ``item``, since what was reacted to may be a file rather than a
    message; a pin names it as ``channel_id``; a membership change names it as
    ``channel``. Read in that order, so the ``item`` spelling wins where both
    are present and the record is filed under the conversation the *item* is in.

    An ``app_home_opened`` uses the third spelling, and what it names is the
    **direct message channel between that person and this app**. That is the id
    worth having and it is why this needs no new shape: Slack models App Home as
    one space with the Messages tab and the Home tab in it, the Messages tab
    *is* that direct message, and filing the record under that channel puts it
    in the conversation the person is standing in rather than in one of its own.
    A separate space would leave the Home tab knowing nothing about the
    conversation one tab across.
    """
    if not isinstance(event, Mapping):
        return ""
    item = event.get("item")
    if isinstance(item, Mapping):
        from_item = str(item.get("channel") or "").strip()
        if from_item:
            return from_item
    return (
        str(event.get("channel_id") or "").strip()
        or str(event.get("channel") or "").strip()
    )


def render_event(event: "Mapping[str, Any]", *, family: str) -> "dict[str, Any]":
    """One Slack event as the record a model reads.

    **The field names are the ones the Slack tools already use** -- ``ts``,
    ``ts_iso_utc``, ``author_name``, ``author_user_id``, ``is_author_bot``,
    ``emoji_name`` -- and that is the whole of the rule. The same fact must not
    reach the model in one shape from ``read_slack_conversation`` and another
    shape from an event, or it has two vocabularies for one room.

    ``ts`` stays raw because it is an identifier: it is the argument a tool
    takes back to address that message. Every other instant is spelled
    ``*_iso_utc``, because a model handed a Slack ts reads it as a date and gets
    it wrong.

    ``ts``/``ts_iso_utc`` name the *message acted upon*, not the event: that is
    the identifier worth having, and it is the one a follow-up tool call needs.
    When the event happened is ``event_ts_iso_utc``, which is a date and not an
    identifier, so it has no raw twin.

    ``author_*`` name the **actor** -- who reacted, who pinned, who joined. An
    event has no author but the person who caused it.

    ``author_name`` falls back to the id, which is what ``slack_history`` does
    with an author it could not resolve: an id is visibly not a name. Nothing
    here calls Slack to resolve it. Recording an event must cost no API call --
    that is what makes it safe to do on every event in every room -- and a name
    lookup per reaction would be one call per emoji.

    ``is_author_bot`` is written only when the payload states it, which is the
    idiom the rest of this connector's metadata follows: absent is how a reader
    tells "not stated" from "stated false", and writing ``false`` onto every
    record would be asserting a fact Slack did not send.

    ``tab`` is written for ``app_home`` and is the field the record exists to
    carry. It separates two quite different situations -- ``messages`` is the
    direct message conversation itself, ``home`` is the published view beside it
    -- and a record that dropped it would report the two as one thing. Slack's
    own field name and Slack's own two values, passed through unchanged.

    **``view`` is dropped, and the alternatives with it.** An
    ``app_home_opened`` carries the whole currently published Home tab: every
    block, its state, its metadata. It is the largest thing any event here could
    put in a fold, it is repeated identically on every entry, and this is the
    family that fires on every entry -- the exact combination
    :data:`EVENT_BUFFER_LIMITS` bounds the other families against. What it
    describes is also this app's own output rather than anything the person did.
    Nor is a cheaper summary of it written, such as a flag saying a view is
    published: this module would be fixing a field name for a surface it does
    not own, and its stance is that what is not covered stays absent rather
    than being approximated. What of the view a record should carry is a
    question for whoever wants it carried, in the names the publishing tools
    already use.

    **Nothing here tells the model to do anything.** What to do about a
    reaction is a policy, and policy belongs in a scope's ``prompt_append``
    where an operator wrote it and can read it back.
    """
    record: dict[str, Any] = {
        "family": family,
        "event_type": str(event.get("type") or "").strip(),
        "event_ts_iso_utc": iso_utc(_float_or_none(event.get("event_ts"))),
    }
    actor = event_actor_id(event)
    if actor:
        record["author_user_id"] = actor
        record["author_name"] = actor
    if event.get("bot_id") or event.get("bot_profile"):
        record["is_author_bot"] = True

    if family == EVENT_FAMILY_APP_HOME:
        tab = str(event.get("tab") or "").strip()
        if tab:
            # Written only when Slack stated it, the idiom is_author_bot
            # follows: a record with no tab says the payload named none, which
            # is a different thing from saying the Home tab was opened.
            record["tab"] = tab

    if family == EVENT_FAMILY_REACTION:
        emoji = str(event.get("reaction") or "").strip()
        if emoji:
            # emoji_name rather than Slack's own ``reaction``, because this is
            # the name the reaction tool takes as an argument and the history
            # tool returns; one word for one thing across read and write.
            record["emoji_name"] = emoji

    item_ts = _item_ts(event, family=family)
    if item_ts:
        record["ts"] = item_ts
        record["ts_iso_utc"] = iso_utc(_float_or_none(item_ts))
    return record


def event_anchor(
    event: "Mapping[str, Any] | None", *, family: str
) -> "tuple[str, str]":
    """``(ts, thread_ts)`` of the message an event is about, or two empty strings.

    The message a turn woken by this event answers, and the thread that answer
    is posted in.

    **Two empty strings is a real answer, not a failure, and it means two
    different things.** For ``member`` it is the permanent answer: a membership
    change is about a room, so there is no message and none is invented -- a
    turn woken by one answers at the channel's top level instead, and
    :func:`family_anchors_on_a_message` is what says so. For a family that does
    anchor on a message it means *this* event named none, which is why the
    question is asked per event and not only per family: a reaction added to a
    *file* is in the ``reaction`` family and names no message at all, and it
    wakes no turn.

    ``thread_ts`` falls back to ``ts``, so the pair is either both empty or both
    set. That fallback is a statement about the payload rather than about
    Slack: a pin carries the pinned message, whose ``thread_ts`` says whether it
    sits in a thread, while a reaction carries a bare ``item.ts`` and says
    nothing about where that message sits. Answering in the thread rooted at the
    anchor is the treatment that is right whenever the payload is silent and
    that Slack itself resolves into the parent thread when the anchor turns out
    to be a reply.

    **No API call.** Resolving a reaction's anchor to its thread root exactly
    would cost one ``conversations.replies`` per event, and it is asked on the
    path that decides whether to *start* a turn, which must be answerable from
    the payload for the same reason every trigger is.
    """
    if not isinstance(event, Mapping):
        return "", ""
    ts = _item_ts(event, family=family)
    if not ts:
        return "", ""
    return ts, _item_thread_ts(event) or ts


def _item_thread_ts(event: "Mapping[str, Any]") -> str:
    """The thread the acted-upon message sits in, where the payload states it."""
    item = event.get("item")
    if not isinstance(item, Mapping):
        return ""
    message = item.get("message")
    if isinstance(message, Mapping):
        return str(message.get("thread_ts") or "").strip()
    return str(item.get("thread_ts") or "").strip()


def _item_ts(event: "Mapping[str, Any]", *, family: str) -> str:
    """The ts of the message the event is about, or ``""`` if there is none.

    Asked of :data:`EVENT_ANCHORED_FAMILIES` rather than of a list of families
    to exclude, so a family declared tomorrow has no ``ts`` until somebody puts
    it in that set. A membership change is about a room and an App Home opening
    is about neither a room nor a message; both get no ``ts`` at all -- an empty
    one would read as an identifier that went missing on the way here.

    A pin nests the message one level deeper than a reaction does: a reaction
    names ``item.ts`` directly, a pin carries the whole pinned message under
    ``item.message``. Both spellings are read, so neither shape loses the field.
    """
    if family not in EVENT_ANCHORED_FAMILIES:
        return ""
    item = event.get("item")
    if not isinstance(item, Mapping):
        return ""
    direct = str(item.get("ts") or "").strip()
    if direct:
        return direct
    message = item.get("message")
    if isinstance(message, Mapping):
        return str(message.get("ts") or "").strip()
    return ""


class InboundEventBuffer:
    """What one process holds of the events it was told to keep.

    Keyed per conversation and, within a conversation, per family. Nothing here
    is shared between rooms: a busy channel must not be able to push a quiet
    one's events out.

    **In memory, and lost on restart. Deliberately.** A buffered event is
    context for the *next* turn, and after a restart there may be no next turn
    for hours, by which time the events are stale on the TTL's own argument.
    Persisting them would buy a folded payload describing a room as it was
    before a deployment. Nobody should "fix" this by adding a file: the state
    this holds is worth exactly as much as the process that collected it.

    No locking. Every caller is on the connector's event loop, and every method
    here runs to completion without awaiting, so there is no point at which two
    of them interleave.
    """

    def __init__(
        self,
        *,
        limits: "Mapping[str, int] | None" = None,
        ttl_seconds: float = EVENT_BUFFER_TTL_SECONDS,
        now: "Any | None" = None,
    ) -> None:
        self._limits = dict(limits or EVENT_BUFFER_LIMITS)
        self._ttl_seconds = float(ttl_seconds)
        # Injectable so a test can age an entry without sleeping through a TTL.
        self._now = now or time.time
        # (chat id, family) -> the records held, oldest first.
        self._held: dict[tuple[str, str], list[tuple[float, dict[str, Any]]]] = {}
        # (chat id, family) -> {reason: count}, cleared when the fold reports it.
        self._dropped: dict[tuple[str, str], dict[str, int]] = {}

    def record(self, chat_id: str, family: str, record: "Mapping[str, Any]") -> None:
        """Keep one rendered event, dropping the oldest if the family is full.

        **Oldest, never newest.** These are state changes, so the recent ones are
        the ones that describe the room as it now stands; dropping the newest
        would leave the model reading the oldest half of a burst and calling it
        current.

        The drop is counted, not swallowed. A fold that quietly holds forty of
        the sixty reactions a message collected is a lie about the room, and a
        silent cut is the exact defect the history tool was fixed for.
        """
        chat_id = str(chat_id or "").strip()
        if not chat_id or family not in EVENT_FAMILIES:
            return
        key = (chat_id, family)
        held = self._held.setdefault(key, [])
        now = float(self._now())
        # Expiry is swept on the way in as well as at the fold, so a room that
        # collects one reaction an hour never holds a record it will not fold.
        self._expire(key, held, now)
        limit = max(int(self._limits.get(family, 0)), 0)
        if limit <= 0:
            return
        while len(held) >= limit:
            held.pop(0)
            self._count_drop(key, DROP_OVERFLOW)
        held.append((now, dict(record)))

    def fold(self, chat_id: str) -> "dict[str, Any] | None":
        """Everything held for ``chat_id``, as one payload, and forget it.

        ``None`` when there is nothing to say: no records and no drops to
        report. A turn in a room where nothing happened carries nothing.

        The records are merged across families and ordered oldest first, which
        is the order they happened in and the order anything else in a
        conversation is read in. They are **not** coalesced: twenty reactions
        stay twenty records rather than becoming "five people reacted", because
        that is a derived representation and the derivation is the connector
        deciding what mattered.
        """
        chat_id = str(chat_id or "").strip()
        if not chat_id:
            return None
        now = float(self._now())
        entries: list[tuple[float, dict[str, Any]]] = []
        dropped: dict[str, dict[str, int]] = {}
        for family in EVENT_FAMILIES:
            key = (chat_id, family)
            held = self._held.pop(key, None)
            if held is not None:
                # Swept even on the way out. The bound can hold a record for
                # longer than the deadline allows in a room that went quiet, and
                # an expiry noticed only at the fold is still an expiry to
                # report rather than a record to hand over.
                self._expire(key, held, now)
                entries.extend(held)
            counts = self._dropped.pop(key, None)
            if counts:
                dropped[family] = counts
        if not entries and not dropped:
            return None
        entries.sort(key=lambda item: item[0])
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "events": [record for _, record in entries],
        }
        if dropped:
            payload["dropped"] = dropped
        return payload

    def forget(self, chat_id: str = "") -> None:
        """Drop what is held for one conversation, or for every one of them.

        Called when the channel stops. A stopped channel has no turn to fold
        into and a restarted one must not open with a description of a room as
        it was before the reconfiguration.
        """
        chat_id = str(chat_id or "").strip()
        if not chat_id:
            self._held.clear()
            self._dropped.clear()
            return
        for family in EVENT_FAMILIES:
            self._held.pop((chat_id, family), None)
            self._dropped.pop((chat_id, family), None)

    def _expire(
        self,
        key: tuple[str, str],
        held: list[tuple[float, dict[str, Any]]],
        now: float,
    ) -> None:
        if self._ttl_seconds <= 0:
            return
        cutoff = now - self._ttl_seconds
        expired = 0
        while held and held[0][0] < cutoff:
            held.pop(0)
            expired += 1
        for _ in range(expired):
            self._count_drop(key, DROP_EXPIRED)

    def _count_drop(self, key: tuple[str, str], reason: str) -> None:
        counts = self._dropped.setdefault(key, {})
        counts[reason] = counts.get(reason, 0) + 1


__all__ = [
    "DROP_EXPIRED",
    "KEY_EVENTS",
    "DROP_OVERFLOW",
    "EVENT_BUFFER_LIMITS",
    "EVENT_BUFFER_TTL_SECONDS",
    "EVENT_CONTEXT",
    "EVENT_DISPOSITIONS",
    "EVENT_DISPOSITION_DEFAULT",
    "EVENT_FAMILIES",
    "EVENT_FAMILY_APP_HOME",
    "EVENT_FAMILY_MEMBER",
    "EVENT_FAMILY_PIN",
    "EVENT_FAMILY_REACTION",
    "EVENT_OFF",
    "EVENT_ANCHORED_FAMILIES",
    "EVENT_TURN",
    "EVENT_TURN_FAMILIES",
    "EVENT_TURN_WITHHELD",
    "EVENT_TYPE_FAMILIES",
    "TURN_WITHHELD_UNDECIDED",
    "InboundEventBuffer",
    "disposition_holds",
    "disposition_word",
    "event_actor_id",
    "event_anchor",
    "event_chat_id",
    "event_disposition",
    "event_family",
    "family_anchors_on_a_message",
    "iso_utc",
    "normalize_event_policy",
    "render_event",
    "turn_withheld_reason",
]
