# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""One word for how far a Slack turn may post, and the names it travels under.

The reading ladder's shape, applied to writing, with its rule turned round.
Four words, each strictly wider than the one above it:

``disabled``
    No posting tool at all.
``origin``
    This conversation only: the three cards declare no ``chat_id``, and the
    conversation comes from trusted request metadata.
``members``
    Another conversation may be named. A target whose members are all already
    in this conversation proceeds silently; a target that reaches anybody else
    is confirmed with the person who started the turn before it is written.
``open``
    Any conversation may be named, and nothing is confirmed.

**Why this is a key of its own rather than a widening of ``history``.** The
widest word inverts between the two. For a read, a public target is the *safe*
case: membership there is self-serve, so the content was already reachable by
anybody who cared to join, which is why ``history: open`` relaxes the
membership rule for one. For a write, a public target is the *widest possible
audience* and therefore the strictest case, the one an operator is least likely
to want reached without being asked. One word cannot carry both readings, and a
single key would have to pick one of them and be wrong about the other
direction for every deployment that set it.

**Why the membership rule inverts with it.** The read rule is ``members(S)
subset-of members(T)``, and it is a statement about disclosure into ``S``: the
answer is posted in ``S`` in front of everybody there, so nobody in ``S`` may
learn anything they could not already learn. Posting sends content the other
way. What lands in ``T`` came out of ``S``, so the test becomes

    members(T) subset-of members(S)

read as *post only where everybody who will see it could already have seen the
conversation it came from*. The two rules are the same principle evaluated in
the direction the content is travelling, which is why neither can be reused for
the other and why the same four words mean different things under each key.

**Widening asks rather than refuses, and the question has to be answerable.**
Under ``members`` a target reaching people outside ``S`` is not forbidden; it is
put to the person who started the turn, with the widening stated -- how many
people will see it who are not in this conversation, and which conversation it
is going to. "Post to #general?" is not a question anybody can answer without
already knowing the thing being asked about.

**A turn with nobody to ask refuses instead.** A scheduled run is the only
turn in that position: it has no requester, so a widening write under
``members`` has nobody to put the question to. An event-woken turn does have
one -- the connector stamps the person whose action produced the event, and the
question goes to them, the same as it would on a turn they had spoken to start.
Refusing is the only answer that is not a decision taken on somebody's behalf
while they are not there.

**Deleting never asks.** It removes a message and widens nothing, so there is no
audience question to put. It still respects ``disabled`` and ``origin``, which
are about reach rather than about audience: a deployment that has said its bot
posts only in the conversation it is spoken to has said where its bot acts at
all.

Two processes need the same four words and the same resolution: the Slack
connector, which settles the word and stamps it onto every inbound request, and
the runtime's posting toolkit, which acts on the stamp. The runtime may not
import the connector -- it pulls in ``slack_bolt`` -- and must not read
connector config, so the mapping-in, word-out resolution lives here, exactly as
the reading ladder's does. Reading ``channels.slack`` at all is
``slack_history_policy.slack_config``'s job and is not repeated here; so is
choosing which install a request belongs to.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

logger = logging.getLogger(__name__)

#: The four words, in widening order, so that "is this at least X" is an index
#: comparison.
WRITE_DISABLED = "disabled"
WRITE_ORIGIN = "origin"
WRITE_MEMBERS = "members"
WRITE_OPEN = "open"
WRITE_POLICY_VALUES: tuple[str, ...] = (
    WRITE_DISABLED,
    WRITE_ORIGIN,
    WRITE_MEMBERS,
    WRITE_OPEN,
)

#: What a deployment that has written none of this gets. ``disabled`` rather
#: than ``origin``, so that upgrading past the release that introduced the key
#: changes nothing at all: a deployment that posted nothing on a model's
#: instruction yesterday posts nothing today, and turning that on is a line an
#: operator writes rather than a default they inherit.
WRITE_POLICY_DEFAULT = WRITE_DISABLED

#: The words that let a request name a conversation other than its own. Read by
#: the toolkit at registration to decide whether the cards declare ``chat_id``
#: at all, and again at call time to decide whether a named one is honoured.
WRITE_POLICY_NAMES_A_TARGET: frozenset[str] = frozenset({WRITE_MEMBERS, WRITE_OPEN})

#: The words under which a widening target is put to the requester rather than
#: written straight away. One word today; a frozenset because the question the
#: predicate answers is "does this word confirm", not "is this word members".
WRITE_POLICY_CONFIRMS_WIDENING: frozenset[str] = frozenset({WRITE_MEMBERS})

#: The layer-0 key, under ``channels.slack``, beside ``history``.
KEY_WRITE = "write"

#: What the connector stamps onto request metadata and the runtime toolkit
#: reads. A literal rather than a shared enum on the wire, because request
#: metadata crosses a process boundary as JSON; a test pins the connector, the
#: cron path and the toolkit to this one name.
METADATA_WRITE_POLICY_KEY = "slack_write_policy"


def normalize_write_policy(raw: Any) -> "str | None":
    """One written value as a policy word, ``None`` for absent, ``""`` for wrong.

    Three answers rather than two, and the caller acts on each differently.
    ``None`` and ``""`` are not errors and are not words: they are a key nobody
    wrote, or wrote with nothing after the colon, which is how a template ships
    a key an upgrade must not delete. Both mean *no value here*, leaving
    whatever is below to answer. The empty string is returned for a value that
    was written and is not one of the four, so that a caller can say so before
    falling back.

    A boolean gets no special reading, which is why the narrowest word is
    ``disabled`` and not ``off``. YAML 1.1 -- what ``safe_load`` implements --
    resolves a bare ``off`` to the boolean false, so ``off`` would be the one
    word of the four an operator could not write unquoted. Both booleans fail
    here as what they are: an unrecognised value, warned about by the resolver
    and settled to the shipped default.
    """
    if raw is None:
        return None
    word = str(raw).strip().lower()
    if not word:
        return None
    return word if word in WRITE_POLICY_VALUES else ""


def resolve_write_policy(
    slack_conf: "Mapping[str, Any] | None",
    *,
    warn: Any = None,
) -> str:
    """The layer-0 word for ``channels.slack``.

    No legacy key to translate, unlike the reading ladder: nothing before this
    could post to a conversation a model named, so there is no earlier spelling
    of the setting and no deployment whose existing configuration has to keep
    meaning what it meant.

    Fails closed everywhere. Anything that is not a mapping, and any word that
    is not one of the four, lands on ``disabled``. A misspelling is not read as
    a request to fall back to something wider: it is a line the operator meant
    and that nothing can honour, so it buys the narrowest word and says so.
    """
    emit = warn if callable(warn) else logger.warning
    if not isinstance(slack_conf, Mapping):
        return WRITE_POLICY_DEFAULT

    word = normalize_write_policy(slack_conf.get(KEY_WRITE))
    if word:
        return word
    if word == "":
        emit(
            "channels.slack.%s=%r is not one of %s; no Slack posting tool is"
            " offered (%s)",
            KEY_WRITE,
            slack_conf.get(KEY_WRITE),
            "/".join(WRITE_POLICY_VALUES),
            WRITE_POLICY_DEFAULT,
        )
    return WRITE_POLICY_DEFAULT


def write_policy_metadata(policy: Any) -> dict[str, Any]:
    """The one key a request holds, always present.

    Stamped as a value rather than left absent even when it is the default. An
    absent word means *no Slack connector settled this request*, which the
    runtime gate reads as a refusal; a connector that settled it to ``disabled``
    is a different thing, and only one of the two should be reachable from a
    path that holds a Slack conversation.
    """
    return {
        METADATA_WRITE_POLICY_KEY: normalize_write_policy(policy)
        or WRITE_POLICY_DEFAULT
    }


def write_policy_at_least(policy: Any, word: str) -> bool:
    """Whether a settled word is *word* or wider.

    An unknown word answers ``False`` for every comparison, which is what fails
    closed: a request carrying something that is not one of the four is not
    thereby at least the narrowest of them.
    """
    settled = normalize_write_policy(policy)
    if not settled or word not in WRITE_POLICY_VALUES:
        return False
    return WRITE_POLICY_VALUES.index(settled) >= WRITE_POLICY_VALUES.index(word)


__all__ = [
    "KEY_WRITE",
    "METADATA_WRITE_POLICY_KEY",
    "WRITE_DISABLED",
    "WRITE_MEMBERS",
    "WRITE_OPEN",
    "WRITE_ORIGIN",
    "WRITE_POLICY_CONFIRMS_WIDENING",
    "WRITE_POLICY_DEFAULT",
    "WRITE_POLICY_NAMES_A_TARGET",
    "WRITE_POLICY_VALUES",
    "normalize_write_policy",
    "resolve_write_policy",
    "write_policy_at_least",
    "write_policy_metadata",
]
