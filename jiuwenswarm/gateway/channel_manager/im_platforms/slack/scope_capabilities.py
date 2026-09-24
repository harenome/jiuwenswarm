# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.

"""Slack's ``scopes`` declaration -- what it can be matched on, and what it reads.

Found by filename: the registry globs for ``scope_capabilities.py`` beside each
connector, so a connector opting in is one new file and no edit anywhere else.

**Nothing heavy may be imported at module level here.** This runs during
registry discovery, in both the gateway and the runtime, whether or not Slack is
configured -- so importing ``slack_connect`` would drag ``slack_sdk`` into a
process that has no Slack in it. Two of the three validators below need it and
import it inside their own bodies, where the cost is paid only by a config that
names a model or a trigger. The third, ``history``, checks the word list in
``common/slack_history_policy``. That module imports nothing beyond the standard
library, so it can be read at module level here, and it is already where the
connector, the runtime and the history toolkit read the five words from. A copy
spelled here would be a second list to keep equal to that one.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from jiuwenswarm.common.scopes import (
    AXIS_CHANNEL,
    AXIS_CHAT,
    AXIS_CHAT_TYPE,
    AxisRestriction,
    ChannelCapabilities,
    ScopeMatch,
    register_channel,
)
from jiuwenswarm.common.slack_events_policy import (
    EVENT_DISPOSITIONS,
    EVENT_FAMILIES,
    EVENT_FAMILY_APP_HOME,
    EVENT_TURN,
    EVENT_TURN_FAMILIES,
    KEY_EVENTS,
    disposition_holds,
    disposition_word,
    turn_withheld_reason,
)
from jiuwenswarm.common.slack_history_policy import HISTORY_POLICY_VALUES

#: Slack's four kinds of conversation, split by whether the conversation is a
#: direct message. Their union is the ``chat_type`` vocabulary declared below,
#: and the vocabulary is built from them rather than written out a second time.
#:
#: **Two frozensets rather than one, so that a kind cannot be added without
#: being classed.** Which side a kind falls on decides whether a rule naming it
#: can ever see an App Home event -- see :func:`_caution_events` -- and deriving
#: the halves from the vocabulary is impossible in the direction that matters: a
#: fifth kind would join the vocabulary and belong to neither side until
#: somebody said which, and the set that forces the saying is the point.
#:
#: ``mpim`` is a direct message here because Slack's word for it is one: it is a
#: group *instant message*, with no channel behind it. A stricter reading would
#: note that App Home opens in the app's one-to-one conversation with a person,
#: so an ``mpim`` rule is unreachable too -- but that is one inference past what
#: the vocabulary states, and the caution below is deliberately confined to what
#: the operator's own words already prove.
_DM_CHAT_TYPES: "frozenset[str]" = frozenset({"im", "mpim"})
_ROOM_CHAT_TYPES: "frozenset[str]" = frozenset({"channel", "group"})

# The five words, their definitions and the publicness asymmetry are in
# ``common/slack_history_policy``, which is where the list imported above is
# written. Two things about the declaration are settled here.
#
# ``origin`` grants nothing ``members`` does not: for ``T = S`` the subset test
# is trivially satisfied. It is kept because it is the only value whose
# correctness does not depend on our own gate being right -- there is no target
# argument on the tool card, so there is nothing to probe -- and because it is
# the cheap one: every ``members`` call naming a target costs a
# ``conversations.info`` and two paginated ``conversations.members``. Its purpose
# is capability and cost; the privacy boundary is the subset test.
#
# The list is that module's rather than the shared schema's because ``agent`` has
# no shared key list: a key exists in that section because a connector named it
# in ``sections``, and this is the only connector that names this one. If a
# second implements it the list should move to the schema, the way ``mid_turn``'s
# did.


def _trigger_name(entry: str) -> str:
    """One ``mode`` entry with its sign taken off, if it had one."""
    entry = entry.strip()
    return entry[1:].strip() if entry[:1] in ("+", "-") else entry


def _check_mode(value: Any) -> "str | None":
    """Whether every entry in a ``mode`` list is a trigger name.

    Reached through the declaration because the shared loader knows only that a
    list is a set and that its entries may be signed; what a Slack trigger is
    called is this connector's business.

    Reported for the whole key rather than per entry, because the shared loader
    drops a key it cannot honour instead of editing it. Refusing the whole list
    leaves the conversation on the layer below, which can answer more than the
    refused list would have, so the warning names the conversation and every
    entry it did not recognise.
    """
    from jiuwenswarm.gateway.channel_manager.im_platforms.slack.slack_connect import (
        CHANNEL_MODE_TRIGGERS,
    )

    if isinstance(value, str):
        # A bare string is the legacy group_chat_mode spelling. It denotes a
        # list of one, so say so rather than accepting a shape whose composition
        # rule would be "scalar".
        return (
            f"={value!r} is a single word where a list of triggers is wanted;"
            f" write [{value}]. Ignoring it"
        )
    if not isinstance(value, (list, tuple)):
        return f"={value!r} is not a list of triggers; ignoring it"

    # One sign, then the name, which is how the fold reads a signed entry:
    # ``_apply_signed`` takes ``entry[0]`` as the sign and ``entry[1:]`` as the
    # trigger. Stripping every leading sign instead would pass ``++url`` and
    # leave the fold adding a trigger called ``+url`` -- a name nothing matches,
    # in a set nobody wrote, with no warning anywhere.
    unknown = [
        str(entry)
        for entry in value
        if _trigger_name(str(entry)) not in CHANNEL_MODE_TRIGGERS
    ]
    if unknown:
        return (
            f" names {', '.join(repr(name) for name in unknown)}, which"
            f" {'are' if len(unknown) > 1 else 'is'} not"
            f" {'triggers' if len(unknown) > 1 else 'a trigger'}."
            f" The triggers are {'/'.join(CHANNEL_MODE_TRIGGERS)}. Ignoring the"
            f" whole list, which leaves that conversation on the layer below"
            f" rather than on a set nobody wrote"
        )
    return None


def _check_model_name(value: Any) -> "str | None":
    """Whether ``model_name`` is one of the models actually configured.

    Checked rather than passed through because ``_resolve_model_by_name`` returns
    the default model for any name it does not have, and logs nothing. An
    unchecked typo runs that conversation on a model nobody chose, with nothing
    in the log to say so.

    Nothing to check against is not a reason to refuse: a config read that fails
    would otherwise drop a correct setting, and the operator would have no way to
    see why.
    """
    from jiuwenswarm.gateway.channel_manager.im_platforms.slack.slack_connect import (
        configured_models,
    )

    known = configured_models()
    if value is None or isinstance(value, bool) or not isinstance(value, str):
        return f"={value!r} is not a model name; that conversation runs on {known.describe_fallback()}"
    name = value.strip()
    if not name:
        # Unlike a prompt, an empty value here is not a request for "nothing":
        # every turn runs on some model, so there is no empty state to mean.
        return f"is empty; a turn always runs on some model, so that conversation runs on {known.describe_fallback()}"
    if not known.known:
        return None
    if not known.accepts(name):
        return (
            f"={value!r} is not one of the models configured in models.defaults"
            f" ({known.describe()}); dropping it, so that conversation runs on"
            f" {known.describe_fallback()}"
        )
    return None


def _check_history(value: Any) -> "str | None":
    """Whether ``history`` is exactly one of the five words.

    Refuses rather than narrows, the idiom every closed vocabulary in this design
    follows: an unrecognised word drops the key and leaves that conversation on
    the layer below, whose floor is ``channels.slack.history`` and whose default
    is ``disabled``. A typo therefore costs a reach somebody wrote; it cannot
    grant one nobody did.

    **Exactly, where ``mid_turn`` settles case and surrounding space.** The
    difference comes from the mechanism. ``mid_turn`` is settled by a branch in
    the shared loader, which can replace the value with the word it recognised;
    this is a connector ``validator``, which may only say whether a value is
    usable. Accepting ``Members`` here would store ``Members``, and the contract
    with the connector is that the resolved value is one of the five words. The
    warning names all five, so the operator's next edit is correct.

    An ``_append`` reaches this too, with whatever was to be appended, and is
    refused by the same test: appending to one of five words produces a sixth
    that is not one of them. The refusal says so.
    """
    if isinstance(value, str) and value in HISTORY_POLICY_VALUES:
        return None
    return (
        f"={value!r} is not one of {', '.join(HISTORY_POLICY_VALUES)}, written"
        f" exactly and in lower case; ignoring it, which leaves that conversation"
        f" on the layer below rather than on a reach nobody wrote. There is"
        f" nothing to append to a word from a closed vocabulary either -- write"
        f" history with the value you want"
    )


def _check_agent_names(value: Any) -> "str | None":
    if not isinstance(value, (list, tuple)):
        return f"={value!r} is not a list of agent or skill names; ignoring it"
    if any(
        not isinstance(name, str) or not name or name != name.strip()
        or name.startswith(("+", "-"))
        for name in value
    ):
        return (
            f"={value!r} must contain non-empty names without surrounding"
            " spaces or +/- prefixes; ignoring it"
        )
    if len(set(value)) != len(value):
        return f"={value!r} repeats a name; ignoring it"
    return None


def _check_subagents(value: Any) -> "str | None":
    return _check_agent_names(value)


def _check_skills(value: Any) -> "str | None":
    if not isinstance(value, Mapping) or not value:
        return f"={value!r} must be a mapping with available or required; ignoring it"
    unknown = set(value) - {"available", "required"}
    if unknown:
        return f" names unsupported skill settings {[str(key) for key in unknown]!r}; ignoring it"
    for key, names in value.items():
        reason = _check_agent_names(names)
        if reason:
            return f".{key}{reason}"
    return None


def _check_events(value: Any) -> "str | None":
    """Whether ``events`` is a mapping of families to dispositions.

    A mapping rather than a list or a word, and the shape is the point: the
    composition rule follows the value's type, and only a mapping merges per
    key. A conversation naming ``pin`` alone therefore keeps whatever the
    platform layer said about ``reaction``, where a list would have replaced the
    whole set and a scalar would have had nothing to say about a second family.

    Refuses the whole key rather than the offending entry, the idiom every
    closed vocabulary here follows: dropping the key leaves that conversation on
    the layer below, whose floor is ``off``. A typo therefore costs a reach
    somebody wrote and cannot grant one nobody did. The warning names every
    entry it did not recognise, so the operator's next edit is correct.

    An ``_append`` reaches this as a string and is refused by the first branch,
    which is the right answer: there is nothing to append to a mapping, and
    ``events`` merges per key already.

    Dispositions are read through ``disposition_word`` rather than compared as
    written, so an unquoted ``off`` -- the boolean ``False`` under the YAML 1.1
    parser this file is loaded with, and the spelling the config template's own
    worked example uses -- is the word it was written as. Without that, one
    family written that way refused the whole mapping and took the families
    spelled correctly beside it down with it.

    **Two refusals, not one.** A word outside the three is a typo. ``turn`` on a
    family that has no settled destination is spelled correctly and still
    refused, and ``app_home`` is that family: somebody opening a tab has no
    message to answer, the answer it would want -- a published view -- has no
    tool behind it, and which destination would even be coherent depends on
    which tab was opened. The reason is read out of ``EVENT_TURN_WITHHELD``
    rather than written here, so the module that decides which families take the
    word is also the module that says why one does not.

    The second refusal exists because ``EVENT_TURN_FAMILIES`` is written out
    rather than derived: a family arriving with nobody having decided where a
    turn woken by it answers lands outside the set, and this is the gate that
    says so instead of letting it inherit whichever destination the connector
    reached first.
    """
    if not isinstance(value, Mapping):
        return (
            f"={value!r} is not a mapping of event families to dispositions;"
            f" write events with the families you want, one per line."
            f" The families are {', '.join(EVENT_FAMILIES)} and each takes"
            f" {' or '.join(EVENT_DISPOSITIONS)}. Ignoring it, which leaves"
            f" that conversation keeping no events rather than keeping a set"
            f" nobody wrote"
        )
    unknown_families = [
        str(family)
        for family in value
        if str(family).strip() not in EVENT_FAMILIES
    ]
    if unknown_families:
        return (
            f" names {', '.join(repr(name) for name in unknown_families)},"
            f" which {'are' if len(unknown_families) > 1 else 'is'} not"
            f" {'event families' if len(unknown_families) > 1 else 'an event family'}."
            f" The families are {'/'.join(EVENT_FAMILIES)}, each covering both"
            f" directions of one state change -- reaction covers added and"
            f" removed together, and so do the other two. Ignoring the whole"
            f" mapping"
        )
    bad_words = [
        f"{family}={disposition!r}"
        for family, disposition in value.items()
        if disposition_word(disposition) not in EVENT_DISPOSITIONS
    ]
    if bad_words:
        return (
            f" sets {', '.join(bad_words)}; a family takes"
            f" {' or '.join(EVENT_DISPOSITIONS)}, written exactly and in lower"
            f" case. off drops the event, context keeps it for the next turn,"
            f" and turn starts one over the event itself. Ignoring the whole"
            f" mapping, which leaves that conversation keeping no events"
        )
    # Spelled right, and still not a thing that family can be. The predicate is
    # the one the normaliser and the connector's read gate use, so the three
    # cannot come to differ about which families take which word.
    undestined = sorted(
        str(family).strip()
        for family, disposition in value.items()
        if not disposition_holds(str(family).strip(), disposition_word(disposition))
    )
    if undestined:
        # One reason per family, deduplicated: two families refused for the same
        # written reason say it once, and a family with a reason of its own says
        # that one rather than a sentence about event turns in general.
        why = "; ".join(
            dict.fromkeys(turn_withheld_reason(name) for name in undestined)
        )
        return (
            f" sets {', '.join(f'{name}={EVENT_TURN}' for name in undestined)},"
            f" and {' and '.join(undestined)} cannot start a turn:"
            f" {why}. The families that take"
            f" {EVENT_TURN} are {'/'.join(sorted(EVENT_TURN_FAMILIES))}; write"
            f" context for the rest, which keeps the event for the next turn"
            f" that runs anyway. Ignoring the whole mapping, which leaves that"
            f" conversation keeping no events"
        )
    return None


def _caution_events(value: Any, match: ScopeMatch) -> "str | None":
    """Whether an ``events`` mapping can reach the rule it is written on.

    One family has a destination of its own. ``reaction``, ``pin`` and
    ``member`` all arrive in the conversation they are about, so a rule that
    names a conversation or a kind of one sees them; ``app_home_opened`` arrives
    in this app's direct message with whoever opened the tab, whatever they were
    looking at and whichever channels they are in. A rule that has said the
    conversation is a channel has therefore excluded every App Home event there
    will ever be, and the setting sits in the config doing nothing with nothing
    to show for it.

    **Warns and keeps, which is why it is a caution rather than a refusal.** The
    value is spelled correctly and is a thing this connector implements; what is
    wrong is the pairing. Dropping the mapping the way the two refusals above do
    would take a ``reaction`` rule written beside it down as well -- and that one
    works on a channel -- so an inert line would cost the operator a working
    setting. Nothing accumulates at runtime either: an event that never arrives
    keeps no buffer and wakes no turn.

    **Only the case the operator's own words prove.** ``chat_type`` is written
    in Slack's vocabulary, so a rule naming ``channel`` states that this rule is
    not about a direct message and the conclusion needs nothing else. A rule
    naming ``chat: C…`` is left alone deliberately: reading the kind out of the
    id's prefix is what ``_chat_types`` in the connector refuses to do -- it
    holds Slack's word and never this connector's -- and asking
    ``conversations.info`` would buy one typo at the price of a network call per
    scope at load. The config template carries that case instead.
    """
    if not isinstance(value, Mapping):
        return None
    families = {str(family).strip() for family in value}
    kinds = match.chat_type
    if EVENT_FAMILY_APP_HOME not in families or not kinds:
        return None
    if any(kind in _DM_CHAT_TYPES for kind in kinds):
        return None
    return (
        f" sets {EVENT_FAMILY_APP_HOME} on a rule matching"
        f" {AXIS_CHAT_TYPE}: {', '.join(kinds)}, which will never see one: an"
        f" App Home event arrives in this app's direct message with the person"
        f" who opened it, and never in a conversation of that kind. Keeping the"
        f" mapping, since the other families do arrive where this rule is"
        f" looking, but nothing will come of that entry. Write"
        f" {EVENT_FAMILY_APP_HOME} on a rule that matches the direct message --"
        f" {{{AXIS_CHAT_TYPE}: im}}, or that conversation by id -- or on"
        f" {{{AXIS_CHANNEL}: slack}}, which matches whatever conversation an"
        f" event arrives in"
    )


SLACK_CAPABILITIES = ChannelCapabilities(
    channel="slack",
    # An identity axis needs a field only the platform writes. event["user"] is
    # that: Slack sets it on every user message and is its only writer, so the
    # matcher reads it. role, the same axis spelled as a named set of people, is
    # not readable on every section; that is a property of the matcher rather
    # than of this connector, and the schema module states it.
    #
    # chat_type is event["channel_type"], which Slack writes and nothing else
    # does. It is declared because the kinds are Slack's own words and only this
    # file knows them; the axis is in the schema, the vocabulary is here.
    #
    # workspace is the Slack team id of the installation this connector is
    # connected to, learned from ``auth.test`` at startup and held as
    # ``_workspace_team_id``. It is declared now that the gateway builds one
    # ``SlackChannel`` per credential block: two instances answer for two teams,
    # and the team id is the only thing that tells them apart on a request.
    #
    # **It is the connection's team and not the event's, and that difference is
    # the reason the axis is worth declaring.** Most of what this connector
    # settles per conversation it settles on paths holding no event at all -- a
    # cron push, a resumed turn, a heartbeat follow-up -- and an event-derived
    # value is empty on every one of them, so a workspace rule would fail closed
    # exactly where an operator expects it to hold. The connection's team is
    # also the right value in a Slack Connect channel, where the event names the
    # *sender's* home team while the rule is written about the installation that
    # received the message. And it is the value ``claims_message`` already
    # routes an outbound reply on, so the matcher and the router cannot come to
    # disagree about which workspace a request belongs to.
    #
    # No ``axis_values`` entry, and there never will be: a team id is an opaque
    # id Slack issued rather than a word from a vocabulary this file could hold,
    # which is the same reason ``chat`` and ``user`` have none.
    axes=frozenset({"channel", "chat", "chat_type", "user", "workspace"}),
    # mode, prompt, mid_turn, session and model_name, split across the two
    # sections by who acts on the value rather than by who reads the config. This
    # connector resolves all five; what splits them is what it then does with the
    # value. It *consumes* mode, prompt, mid_turn and session -- mode decides
    # whether a message is answered at all, prompt is spliced into the message
    # text and nothing named prompt ever leaves here, mid_turn decides whether
    # the message is sent, sent as a steer, or held here until the session goes
    # idle, and session is the shape of the id this connector mints and then
    # keys its own queue, initiator and question maps on -- while model_name it
    # only carries, onto the request as params["model_name"], for the runtime to
    # act on. So the first four are delivery and the fifth is agent.
    #
    # session is delivery on that test even though the id it settles reaches the
    # runtime, because what the runtime is given is a session id and not a word
    # named session: the connector consumes the setting here and hands on its
    # result, which is the same relationship prompt has with the message text.
    #
    # reply is delivery on the same test and is consumed twice over. ``optional``
    # makes this connector append its own instruction to an inbound message, and
    # makes it withhold an outbound reply that is the token that instruction
    # names. Nothing called reply reaches the runtime.
    #
    # events is delivery on the same test, and all three of its words are
    # consumed here. ``context`` makes this connector keep a reaction, a pin or
    # a membership change in a buffer of its own and fold what accumulated into
    # the next inbound message's text; ``off`` makes it drop the event where it
    # arrived; ``turn`` makes it build a request of its own and route it, back
    # into the thread of the message the event was about, or -- for a
    # membership change, which is about no message -- to the top level of the
    # channel, through the same ``post_as_root`` key a scheduled run's result
    # is delivered with. Nothing named events leaves here under any of the
    # three -- what reaches the runtime is a rendered payload spliced into a
    # request, which is the relationship prompt already has with the message
    # text.
    #
    # mid_turn, session and reply are the three whose values this file does not
    # check. mode names Slack triggers and model_name names configured models,
    # both of which belong to this connector; the other three name vocabularies
    # the schema defines, so the schema checks them and a validator here would be
    # a second copy of a word list. Declaring the keys is still this file's job:
    # the declaration is the opt-in, and it says Slack has actually implemented
    # the words rather than merely recognising them.
    #
    # prompt_append needs no entry of its own: declaring prompt declares the
    # thing being appended to.
    #
    # permissions is the odd one out and is declared as "all of it": Slack
    # neither consumes nor carries it. It is resolved in the runtime, from the
    # channel and chat ids the request arrived with, and the connector never
    # sees the section at all. The declaration is here because the declaration
    # is the opt-in and because attended is here: Slack renders interactive
    # approvals, so an ask written for a Slack conversation is a question
    # somebody can answer, and the fold therefore leaves it as an ask instead of
    # degrading it to a deny. Its keys are left to the shared loader,
    # which knows the allow/ask/deny vocabulary; naming them again here would be
    # a second place to keep that list correct.
    #
    # clicks is named with both of its gestures, because Slack renders both and
    # the list is a statement about this connector rather than about the
    # section: an approval button on every ask, and a stop button on the
    # activity card of a running turn. Declaring it separates two failures: a
    # channel that renders no buttons does not name the section, so a rule
    # against it reports as having no effect, while a rule here has a click to
    # refuse.
    sections={
        "delivery": frozenset(
            {"mode", "prompt", "mid_turn", "session", "reply", KEY_EVENTS}
        ),
        "agent": frozenset(
            {
                "model_name",
                # Carried, never consumed here, exactly as model_name is. This
                # connector settles the word per conversation and stamps it onto
                # the request as params["slack_history_policy"]. Only the
                # runtime's history toolkit can act on it, because the word buys
                # a membership comparison taken at read time against Slack, which
                # nothing can settle at load. Config states the policy, the
                # toolkit enforces it -- the same split permissions makes between
                # a declared level and the engine's decision.
                "history",
                "subagents",
                "skills",
            }
        ),
        "permissions": None,
        "clicks": frozenset({"approve", "stop"}),
    },
    # Slack renders interactive approvals. Read by the permissions fold, which
    # degrades an ask to a deny where nobody can answer it.
    attended=True,
    # One id per person, the one on the event, on a block_actions payload as much
    # as on a message. That is what makes a clicks rule enforceable here: a
    # channel naming no clicker refuses every click rather than gating one. Slack
    # also sends a team id, which is part of the identity bag and is not an
    # identity: it names an installation rather than a person, so it is matched
    # on the ``workspace`` axis declared above and has no entry here. The tuple
    # is ordered for the platforms where one person has several ids, such as
    # Feishu's three.
    identity_keys=("user",),
    # Slack's four kinds of conversation, in its own words, exactly as they
    # arrive on event["channel_type"]. A public channel, a private one, a
    # one-to-one direct message and a group direct message.
    #
    # Declared rather than inferred, for the reason ``axes`` is declared: this
    # is the only file that knows the list, and the shared loader is what has to
    # tell an operator that "dm" is not a word here and that "im" is. Spelled
    # out rather than derived from the id prefix -- D for a direct message, C
    # and G for the rest -- because the prefix cannot separate a private channel
    # from a group direct message, and a vocabulary that silently conflates two
    # kinds is worse than one that has neither.
    #
    # "group" and "mpim" are in it though nothing in this connector branches on
    # them. A rule is about requests rather than about this file's branches, and
    # Slack sends both words: leaving either out would refuse a rule for a
    # conversation type that exists and report it as a typo.
    #
    # Assembled from the two halves at the top of this file rather than written
    # out a second time here. Which half a kind sits in is what settles whether
    # a rule naming it can ever see an App Home event, so a fifth kind has to be
    # classed before it can be offered.
    axis_values={
        AXIS_CHAT_TYPE: _DM_CHAT_TYPES | _ROOM_CHAT_TYPES,
    },
    # The connector settings that answer the same questions. A scope above one
    # of them is the cascade working as intended, and is worth a line only
    # because the value then has two homes.
    #
    # delivery.mode and agent.history have one each. agent.model_name,
    # delivery.prompt, delivery.mid_turn, delivery.session and delivery.events
    # deliberately have none and must not gain any: no channels.slack key pins a
    # model, a standing prompt, what a mid-turn message does, how wide a session
    # is, or which events a room keeps, so
    # inventing a layer-0 key here would fire the "two homes for one value"
    # warning against a setting that does not exist, and would make the resolver
    # report a home an operator could edit to no effect. reply_in_thread is the
    # near miss worth naming: it says whether the bot opens a thread on a
    # root-level message, which is about where an answer is posted rather than
    # about which messages share a conversation, and pairing the two would make
    # a delivery setting look like a session one.
    #
    # channels.slack.history asks the same question in the same five words, one
    # layer down: it is where a deployment that wants the feature on everywhere
    # says so without writing a scope. Registering it as the twin buys the
    # existing two-homes warning and one vocabulary. group_chat_mode is the
    # precedent, and it also shows the twin need not share a type -- this pair
    # happens to.
    layer0_keys={
        "delivery": {"mode": "group_chat_mode"},
        "agent": {"history": "history"},
    },
    validators={
        "delivery.mode": _check_mode,
        # Checked here rather than by the shared loader because the families are
        # this connector's: they name Slack event types, the way mode names
        # Slack triggers. The two words a family takes are generic, but a
        # vocabulary with one half of it per-platform is one validator, not two.
        f"delivery.{KEY_EVENTS}": _check_events,
        "agent.model_name": _check_model_name,
        "agent.history": _check_history,
        "agent.subagents": _check_subagents,
        "agent.skills": _check_skills,
    },
    # One entry, and the hook is the narrower of the two for a reason: a
    # validator asks whether a value can be honoured, and this asks whether any
    # request will reach one that can. Only ``events`` has a family whose
    # destination is fixed by Slack rather than by the conversation the rule
    # names, so only ``events`` can be written correctly and addressed at
    # nothing. The warning is written where the event vocabulary is read.
    cautions={
        f"delivery.{KEY_EVENTS}": _caution_events,
    },
    # Four entries, and in each the argument is about what the key means rather
    # than about Slack: this connector does populate a sender (``axes`` says so,
    # and ``model_name`` is matched on one), and none of these keys may be
    # addressed that way. Each restriction is stated where it is written, on the
    # entry itself; agent.history's is the longer one and is below.
    #
    # agent.history takes one axis. user and role are barred because the asker is already handled dynamically:
    # members(S) subset-of members(T) is the correct treatment of who is asking,
    # taken per request against the room as it stands. A second, static treatment
    # on the same axis adds nothing, and can grant exactly what membership was
    # withholding. Barring them also keeps ``not`` safe here: negation on an
    # identity axis fails open, since a role that resolves to nobody excludes
    # nobody and hands the grant to the people the rule was written to exempt. On
    # ``channel`` a negation names a platform rather than a person, so the axis
    # that remains admits one.
    #
    # chat was argued for, on the ground that without it the switch reaches every
    # DM, where the subset rule takes its most permissive form. That counts
    # eligible targets rather than risk: a DM source discloses to one person whom
    # the rule has just established is a member of the target, and a DM's
    # membership cannot grow, which is the one hole the rule has. The hazard
    # belongs to channels. Rollout and API budget remained, and neither is a
    # reason to put an authorization key on a second axis.
    axis_restrictions={
        # Three axes, and the argument is about what a session is. A session is
        # a property of a conversation, not of whoever is typing into it: two
        # people writing in the same thread under different answers would be
        # posting into two different sessions, so the room would hold one
        # history for the people a rule named and another for everybody else,
        # with neither able to see what the other asked. Nothing warns about
        # that and nothing in Slack shows it. Addressed on the conversation, the
        # key says one thing about one room, which is the only reading that has
        # a meaning.
        #
        # chat_type passes that test and is admitted. The test the bar states is
        # about senders, not about how narrowly a rule is written: a kind is
        # settled per conversation, so everyone in a given room gets the same
        # answer and the room still says one thing. It was excluded only because
        # an allow-list names axes rather than the property it cares about, and
        # this axis did not exist when the list was written.
        "delivery.session": AxisRestriction.only_on(
            AXIS_CHANNEL,
            AXIS_CHAT,
            AXIS_CHAT_TYPE,
            because=(
                "How wide a session is belongs to the conversation rather than"
                " to whoever is typing in it: settled per sender, one room"
                " would hold one history for the people the rule names and"
                " another for everybody else. Write it on {channel: slack}, on"
                " one conversation, or on a kind of conversation -- every room"
                " of that kind then says the same thing about itself"
            ),
        ),
        # Three axes, for the reason delivery.session takes three, and one more
        # of its own. Whether an answer is owed is a property of the room:
        # settled per sender, the same message would be answerable or not
        # depending on who typed it, which nothing in the room shows. And the
        # two halves of the feature see different things -- the instruction is
        # appended to an inbound message, where the sender is known, while the
        # reply is matched on the way out, where nothing says which sender it
        # answers -- so a rule naming people would license a silence the
        # delivery path cannot recognise, and post the token at the very people
        # told to write it.
        #
        # chat_type passes both halves. It is settled per conversation, so every
        # room of a kind gets one answer; and unlike a sender it survives to the
        # outbound half, where the reply carries the kind of the conversation it
        # came from in its own metadata. The two halves therefore read the same
        # fact, which is what the second argument above asks of any axis this
        # key admits.
        "delivery.reply": AxisRestriction.only_on(
            AXIS_CHANNEL,
            AXIS_CHAT,
            AXIS_CHAT_TYPE,
            because=(
                "Whether an answer is owed belongs to the conversation rather"
                " than to whoever is typing in it, and the reply is matched with"
                " no sender to read, so a rule naming people would have the"
                " token posted at the very people told to write it. Write it on"
                " {channel: slack}, on one conversation, or on a kind of"
                " conversation -- the kind reaches the outbound half, where a"
                " sender does not"
            ),
        ),
        # Three axes, for the reason delivery.session and delivery.reply take
        # three. What a room feeds the model is a property of the room, not of
        # whoever is typing in it: settled per sender, one conversation would
        # buffer a reaction when one person had last spoken and drop it when
        # another had, and the fold lands on whichever turn happens to run next
        # -- which need not be that sender's turn at all. There is nothing in
        # Slack that would show either half of that.
        #
        # The events themselves make the same point from the other side. A
        # reaction and a join carry an actor, and that actor is nobody's
        # "sender": nothing ties the person who reacted to the person whose
        # message starts the turn the record is folded into, so a rule naming
        # people could not even be read as being about the people it names.
        #
        # chat_type passes that test -- a kind is settled per conversation, and
        # every room of a kind gets one answer -- and is admitted, with one
        # consequence worth stating plainly rather than leaving to be found.
        #
        # **A rule on a kind is accepted here and fires on no event today.** The
        # disposition is settled on the arriving event, and none of the three
        # families carries a kind the matcher can use: reaction and pin send no
        # channel_type at all, and member_joined_channel sends "C" or "G",
        # Slack's one-letter code rather than one of the four words a message
        # uses. The axis fails closed on both, which is its stated behaviour
        # where a platform names no kind, so such a rule leaves those events on
        # the layer below rather than misfiring.
        #
        # Admitted all the same, and the distinction from the two bars below is
        # what makes that consistent. permissions and clicks are barred because
        # their consumers are handed a channel and a chat and can never be
        # handed anything else; this key's consumer is handed the event, and an
        # event that names its kind would match. What Slack puts on one family
        # today is a fact about that payload, not an argument about what the key
        # means, and the bar states an argument about senders that this axis is
        # not.
        f"delivery.{KEY_EVENTS}": AxisRestriction.only_on(
            AXIS_CHANNEL,
            AXIS_CHAT,
            AXIS_CHAT_TYPE,
            because=(
                "What a room feeds the model belongs to the conversation rather"
                " than to whoever is typing in it: under context the events are"
                " folded into whichever turn runs next, which need not be the"
                " turn of the sender the rule names, and under turn the event"
                " starts a turn of its own that no sender asked for. The person"
                " who reacted is not a sender either way. Write it on"
                " {channel: slack}, on one conversation, or on a kind of"
                " conversation -- though an event Slack sends without a kind on"
                " it is matched by no rule naming one"
            ),
        ),
        # One axis, and it stays one now that the three above take three. This
        # is the authorization key of the four, and its own reason already
        # refuses chat deliberately: the membership rule decides per target,
        # per request, and a static second answer on the same question can grant
        # what membership was withholding. A kind of conversation is a coarser
        # statement than a conversation, so admitting it would be admitting more
        # than the axis this key has already turned down.
        "agent.history": AxisRestriction.only_on(
            AXIS_CHANNEL,
            because=(
                "Who is asking is settled per request, by the membership rule,"
                " which is the dynamic and correct treatment of it -- a second"
                " static one on the same axis can grant exactly what membership"
                " was withholding. A conversation is not the unit either: the"
                " membership rule already decides per target. Write the rule on"
                " {channel: slack} alone, or set channels.slack.history"
            ),
        ),
    },
)

register_channel(SLACK_CAPABILITIES)
