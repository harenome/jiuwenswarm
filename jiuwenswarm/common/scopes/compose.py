# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.

"""Folding the scopes that match into one answer.

Composition follows the value's **type**, not its key name. That is easier to
remember than a table of per-key rules, and it generalises to keys which do not
exist yet: a connector adding a setting gets its composition rule from the
value's type, with no entry to add here.

===========  ==========================================================
type         rule
===========  ==========================================================
scalar       replaces. A later layer's value is the value.
list         a set. Replaces by default; a list whose every entry is
             signed mutates the inherited set instead, ``+x`` adding and
             ``-x`` removing.
prose        replaces, with a separate ``<key>_append`` that appends to
             whatever the layer above settled.
mapping      merges per key, so two layers each naming a different
             sub-key both apply.
===========  ==========================================================

Union-only set semantics would make a trigger unremovable; replace-only would
force restating the whole base to add one entry.

Composition is per section. ``delivery`` and ``agent`` are folded separately
and never see each other's keys, which keeps "three sections, three readers"
true of the values as well as of the names: two sections could name the same key
and mean two different things without either fold noticing.

**The result holds only the keys some layer spoke about.** A key no scope set
is absent, not filled in from layer 0. The distinction matters where this is
consumed: an absent ``mode`` means the connector's own chain still
runs -- ``group_chat_mode``, then legacy membership -- while a ``mode`` settled
to the same value means a scope said so and the chain is over. Baking layer 0
into every answer would erase that difference and silently switch off the legacy
paths that layer 0 still owns.

Layer 0 is passed in all the same, because ``+x`` and ``-x`` need something to
mutate: a scope that adds one trigger to the platform default has to be able to
see the platform default.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from jiuwenswarm.common.scopes.schema import (
    APPEND_SUFFIX,
    CLICK_APPROVE,
    CONNECTOR_SECTIONS,
    SECTION_CLICKS,
    SECTION_DELIVERY,
    ClickRule,
    Scope,
    matching_scopes,
    signed_entries,
)

_MISSING = object()


def _as_set(value: Any) -> frozenset[str]:
    if value is None:
        return frozenset()
    if isinstance(value, str):
        return frozenset({value})
    if isinstance(value, (frozenset, set, list, tuple)):
        return frozenset(str(item) for item in value)
    return frozenset()


def _apply_signed(base: frozenset[str], entries: Sequence[str]) -> frozenset[str]:
    result = set(base)
    for entry in entries:
        sign, name = entry[0], entry[1:].strip()
        if not name:
            continue
        if sign == "+":
            result.add(name)
        else:
            result.discard(name)
    return frozenset(result)


def compose_values(
    layers: Sequence[Mapping[str, Any]],
    *,
    base: "Mapping[str, Any] | None" = None,
) -> dict[str, Any]:
    """Fold ``layers`` left into the keys they settle.

    ``base`` is layer 0. It seeds set mutation and prose appending and is never
    itself copied into the result: see the module docstring for why an untouched
    key must stay absent.
    """
    seed: Mapping[str, Any] = base if isinstance(base, Mapping) else {}
    result: dict[str, Any] = {}

    for layer in layers:
        # Plain keys before appends, so a layer that both replaces a prompt and
        # appends to it in the same breath appends to its own new text rather
        # than to the one it just replaced.
        plain = {k: v for k, v in layer.items() if not k.endswith(APPEND_SUFFIX)}
        appends = {k: v for k, v in layer.items() if k.endswith(APPEND_SUFFIX)}

        for key, value in plain.items():
            current = result.get(key, _MISSING)
            inherited = current if current is not _MISSING else seed.get(key, _MISSING)

            if isinstance(value, (list, tuple)):
                signed = signed_entries(value)
                if signed:
                    start = _as_set(None if inherited is _MISSING else inherited)
                    result[key] = _apply_signed(start, signed)
                else:
                    result[key] = frozenset(str(item) for item in value)
                continue

            if isinstance(value, Mapping):
                merged: dict[str, Any] = {}
                if isinstance(inherited, Mapping):
                    merged.update(inherited)
                merged.update(value)
                result[key] = merged
                continue

            result[key] = value

        for key, value in appends.items():
            target = key[: -len(APPEND_SUFFIX)]
            current = result.get(target, _MISSING)
            inherited = current if current is not _MISSING else seed.get(target, _MISSING)
            existing = "" if inherited is _MISSING or inherited is None else str(inherited)
            addition = str(value)
            # Two blank-line-separated paragraphs rather than a bare
            # concatenation: a single newline lets a renderer and a model read
            # two standing instructions as one run-on sentence.
            result[target] = f"{existing}\n\n{addition}".strip() if existing else addition.strip()

    return result


def compose_section(
    scopes: Sequence[Scope],
    *,
    channel: "str | None",
    chat: "str | None" = None,
    chat_type: "str | None" = None,
    workspace: "str | None" = None,
    user: "str | None" = None,
    section: str = SECTION_DELIVERY,
    layer0: "Mapping[str, Any] | None" = None,
) -> dict[str, Any]:
    """What ``scopes`` settle for one conversation, layered per key.

    Returns only the keys some matching scope spoke about. An empty result means
    no scope had anything to say here, which every caller must read as "carry on
    exactly as before" rather than as a set of empty values.

    ``user`` is the sender. It is optional because a caller may genuinely have
    none -- a startup summary settles a conversation before anyone has spoken in
    it, and pseudo-channels never have one -- and omitting it composes the
    answer for an unidentified sender rather than an answer for everybody: a
    scope naming people does not contribute, and one excluding people does.

    ``chat_type`` is the kind of conversation and is optional for the same
    reason and with the same consequence: a caller that has none composes the
    answer for a conversation of no named kind, where a scope naming a kind does
    not contribute.

    ``workspace`` is the installation the request arrived from, and is the third
    of the same shape. A caller that has none -- a connector in one workspace, a
    pseudo-channel, a startup summary -- composes the answer for a request in no
    named workspace, so a scope naming one does not contribute and every scope
    naming none still does.
    """
    applicable = matching_scopes(
        scopes,
        channel=channel,
        chat=chat,
        chat_type=chat_type,
        workspace=workspace,
        user=user,
        section=section,
    )
    return compose_values(
        [scope.section(section) for scope in applicable], base=layer0
    )


def click_rule(
    scopes: Sequence[Scope],
    *,
    channel: "str | None",
    chat: "str | None" = None,
    kind: str = CLICK_APPROVE,
) -> "ClickRule | None":
    """Who may make one kind of click in one conversation, or ``None``.

    ``None`` is not an empty rule: with no ``clicks`` rule matching, layer 0
    decides -- ``allow_from`` on Slack -- and layer 0 is not
    always "anyone". A caller must read ``None`` as "carry on exactly as before"
    and never as "nobody may", which is what an empty :class:`ClickRule` means.

    **Composed with no sender, and that is not an omission.** The ``clicks``
    section is folded per conversation and never per clicker: the question it
    answers is who may press this button *here*, so folding it for the very
    person being checked would make every rule match its own subject and gate
    nothing. A rule whose match names a sender never settles a ``clicks`` clause
    at all -- ``_compile_section`` reports and drops it -- so there is none to
    lose here.

    Per gesture, because the ``clicks`` map merges per key: a rule naming only
    ``stop`` leaves ``approve`` on whatever the layer above settled, and a later
    layer naming ``approve`` replaces that clause rather than widening it by
    union, which is the only direction a section that exists to tighten may
    move.
    """
    settled = compose_section(
        scopes, channel=channel, chat=chat, section=SECTION_CLICKS
    )
    rule = settled.get(kind)
    return rule if isinstance(rule, ClickRule) else None


def scoped_chats(
    scopes: Sequence[Scope],
    *,
    channel: str,
    sections: Sequence[str] = CONNECTOR_SECTIONS,
) -> tuple[str, ...]:
    """Every conversation on ``channel`` that some scope names, sorted.

    A connector needs this to know which conversations to settle in advance, and
    to list them in a startup summary: a mistyped-but-well-formed id shows up
    there beside the ids that work, and looks wrong there.

    Across every section a *connector* settles rather than one, because a caller
    asking this question is asking which conversations exist at all. A scope
    that names a conversation and sets only ``agent.model_name`` is as much a
    conversation to settle as one that sets ``delivery.mode``, and a per-section
    default would have silently dropped it from the list the connector
    iterates. The setting would be lost rather than reported, which is the
    failure this module is otherwise written to avoid.

    **``permissions`` and ``clicks`` are deliberately not in that default, and
    this is a security property rather than a tidiness one.** Naming a
    conversation here opts it in: on Slack the returned ids are exactly what
    exempts a channel from ``allowed_channel_ids``. Both of those sections are
    written to *take* something away, so letting either into this list would
    mean a rule saying "deny bash in C0…" -- or "only operators may approve in
    C0…" -- also said "and start answering messages in C0…": a widening
    performed by a restriction, which is the one direction nothing here may
    move. ``permissions`` is also resolved in the other process entirely, and
    ``clicks`` is read when a button is pressed in a
    conversation the connector is already talking in, so neither is something a
    connector settles in advance.
    """
    wanted = tuple(sections)
    named = {
        scope.match.chat
        for scope in scopes
        if scope.match.chat is not None
        and scope.match.channel in (None, channel)
        and any(scope.section(name) for name in wanted)
    }
    return tuple(sorted(named))
