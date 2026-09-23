# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.

"""What each channel can be addressed by, and what it will act on.

Applicability is a property of the implementation, not a deployment choice. An
operator cannot make a connector support an axis it does not populate, and
letting them declare one would move the failure from a load-time warning to
silent non-matching: the scope is present and well formed in the config and
never matches anything.

So there is no ``scopes_enabled`` boolean anywhere. **The declaration is the
opt-in**: a connector adopts ``scopes`` by saying what it supports, in a file
that already knows the answer, and a channel with no declaration is not opted in.
Scopes naming it warn as inert rather than being quietly ignored.

The registry is static because two processes read it. The gateway resolves the
``delivery`` section and, for now, the ``agent`` section too -- resolving is not
acting: the gateway settles ``agent.model_name`` at load and puts it on the
request, and the runtime is what does anything with it. ``permissions`` is
resolved in the runtime, where it is enforced, from the channel and chat ids the
request arrived with. Both halves import this package, so both see the
same table with nothing crossing the wire, and a capability can never disagree
between the two halves of one deployment.

Connectors declare by dropping a ``scope_capabilities.py`` beside themselves
under ``jiuwenswarm/gateway/channel_manager/im_platforms/<name>/``; that module
calls :func:`register_channel` at import and this module finds it. Discovery is
by filename rather than by a list kept here, so adding a connector is one new
file and no edit to this one -- which is also what lets the connector-agnostic
half of this feature and each connector's half live in separate commits.

Pseudo-channels have no connector to hang a declaration on and are registered
below as module-level entries.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Callable, Iterable, Mapping, TypeVar

if TYPE_CHECKING:  # pragma: no cover - resolving a name, not importing a module
    # Typing only, and it stays that way. This module is the one in the package
    # with no intra-package imports at runtime -- every other module here needs
    # it and none of them is below it -- and a caution's second argument is a
    # ``schema`` type, which is above it.
    from jiuwenswarm.common.scopes.schema import ScopeMatch

logger = logging.getLogger(__name__)

# A validator inspects one settled value and returns ``None`` when it is usable,
# or the reason it is not. A reason means "warn with this text and drop the
# key", never "raise": see the module docstring of ``schema`` for why nothing
# here may fail a load.
ValueValidator = Callable[[Any], "str | None"]

# A caution inspects one settled value together with the rule that sets it, and
# returns ``None`` when there is nothing to say, or the sentence an operator
# should read.
#
# **A reason means "warn and keep", and that is the whole of what separates it
# from a ``ValueValidator``.** A validator answers "is this value usable", and
# an unusable value is dropped so the conversation falls to the layer below. A
# caution answers a different question -- "will any request ever reach this
# value" -- about a value that is perfectly usable. There is nothing to fall
# back to, and dropping would take the rest of a well-written rule down over an
# entry that costs nothing at runtime.
#
# It is handed the match because reachability is a property of the pairing
# rather than of the value: the same setting is right on one rule and
# unreachable on another. That is the only thing on the match side a channel
# gets to read, and it is read to warn rather than to decide -- which axes a key
# may be addressed on stays declarative, in ``axis_restrictions``.
ValueCaution = Callable[[Any, "ScopeMatch"], "str | None"]

# How this package reports a config it cannot honour. Declared here, in the
# module with no intra-package imports, because every other module in the
# package needs it and none of them is below the others.
Warn = Callable[..., None]

_FrozenValue = TypeVar("_FrozenValue")

#: The key that stands for "every key in this section" in an
#: ``axis_restrictions`` table. A section-wide entry and a per-key one are the
#: same statement at two granularities, so they share one table and one lookup
#: rather than becoming two mechanisms that have to agree.
ANY_KEY = "*"


@dataclass(frozen=True)
class AxisRestriction:
    """Which match axes one key -- or one whole section -- may be written under.

    A ``match`` says which requests a rule is about; a section says what happens
    to them. Most keys do not care how the rule was addressed. Some do, and the
    reason is always the same shape: the key's meaning already accounts for one
    of the axes, so a rule that *also* addresses that axis is either inert or,
    worse, a second and static answer to a question something else is answering
    dynamically per request.

    **An allow-list, and deliberately not a deny-list.** "Not ``user``, not
    ``role``" and "``channel`` and ``chat`` only" name the same set today and
    stop naming the same set the moment an axis is added: a deny-list would
    silently admit the new axis, and an allow-list would silently refuse it.
    Only one of those two failures can be found by reading a warning, and it is
    the second -- so the allow-list is the fail-closed shape and is the only one
    offered. There is no ``excluding()`` constructor for that reason; write out
    what is permitted, and an axis added later is barred until somebody says
    otherwise.

    **Absent is not the same as empty.** A key with no entry in a table is
    unrestricted -- ``delivery.prompt`` is the example, and it says nothing
    about how a rule was addressed. An entry whose ``allowed`` is empty says the
    key may appear in no rule that matches on anything at all, which is a
    stronger statement than any table currently makes and is kept expressible
    because it is the fail-closed end of the same axis.

    ``because`` is the sentence a warning gives an operator after it has named
    the rule and the axis. It is kept here rather than composed at the
    warning site so that the reason and the restriction cannot drift: whoever
    changes what is permitted is editing the explanation in the same expression.

    A malformed declaration raises rather than warning, which is the one place
    in this package that does. Nothing an operator writes can reach it -- it is
    a connector's own file, evaluated at import -- and the failure it produces
    is already a state this design has a meaning for: :func:`_discover` catches
    it, the connector is not opted in, and scopes naming it warn as inert. That
    is fail-closed. Warning instead would leave a restriction that reads as
    enforced and is not.
    """

    allowed: frozenset[str] = frozenset()
    because: str = ""

    def __post_init__(self) -> None:
        axes = tuple(self.allowed)
        for axis in axes:
            if not isinstance(axis, str) or not axis.strip():
                raise ValueError(
                    f"an axis restriction names {axis!r}, which is not an axis"
                )
        object.__setattr__(self, "allowed", frozenset(axis.strip() for axis in axes))

    @classmethod
    def only_on(cls, *axes: str, because: str = "") -> "AxisRestriction":
        """The restriction permitting exactly ``axes`` and nothing else."""
        return cls(allowed=frozenset(axes), because=because)

    def permits(self, axis: str) -> bool:
        return axis in self.allowed

    def refuses(self, axes: "Iterable[str]") -> tuple[str, ...]:
        """The axes in ``axes`` this restriction bars, sorted, for a warning.

        Every one of them, rather than the first: a rule addressed on two barred
        axes is two things to fix, and an operator told about one of them comes
        back for the second warning after the next reload.
        """
        return tuple(sorted(axis for axis in axes if not self.permits(axis)))

    def narrowed_by(self, other: "AxisRestriction | None") -> "AxisRestriction":
        """This restriction and ``other`` both, which is the intersection.

        Used where a shared table and a connector's declaration both speak about
        one key. Intersection is what makes "a connector may narrow, never
        widen" true by construction rather than by a rule somebody has to
        enforce, and it keeps the two tables from needing a precedence order.

        ``because`` is taken from whichever side is the binding constraint --
        the one whose allow-set *is* the intersection, because that is the side
        whose sentence explains the refusal. Where both are (they agree) or
        neither is (they are incomparable, and the intersection is narrower than
        either) this side wins, since it is the more specific declaration. The
        intersection has no reason of its own and inventing one would put a
        sentence in a warning that no file contains, so the other side's text is
        used only as a fallback when the binding one is silent.
        """
        if other is None:
            return self
        allowed = self.allowed & other.allowed
        if allowed == other.allowed and allowed != self.allowed:
            return AxisRestriction(allowed=allowed, because=other.because or self.because)
        return AxisRestriction(allowed=allowed, because=self.because or other.because)

    def describe(self) -> str:
        """The permitted axes as one clause, for a warning."""
        return ", ".join(sorted(self.allowed)) or "nothing"


def _freeze_sections(
    value: Mapping[str, "frozenset[str] | None"],
) -> Mapping[str, "frozenset[str] | None"]:
    """A section map whose key sets cannot be added to after registration.

    The values are rebuilt as frozensets rather than passed through, because a
    declaration written with a plain ``set`` -- which reads identically at the
    call site and is what a connector author reaches for -- would otherwise
    leave a mutable set inside the shared registry.
    """
    return MappingProxyType(
        {
            str(k): (None if v is None else frozenset(v))
            for k, v in value.items()
        }
    )


def freeze_nested(
    value: Mapping[str, Mapping[str, _FrozenValue]],
) -> Mapping[str, Mapping[str, _FrozenValue]]:
    """A two-level mapping, read-only at both levels.

    Shared by the two registries this package builds at import: a channel's
    ``layer0_keys`` and the people directory. Both are one dict per process and
    both are read from everywhere, which is the case where an accidental write
    is found long after it happens and nowhere near the code that made it.
    """
    return MappingProxyType(
        {str(k): MappingProxyType(dict(v)) for k, v in value.items()}
    )


@dataclass(frozen=True)
class ChannelCapabilities:
    """One channel's declaration.

    ``axes`` is what the channel can *populate*. The matcher may understand
    fewer, and does while the identity axes are still deferred. Keeping the two
    apart is deliberate: a connector states the truth about itself, and
    ``schema`` states the version-scoped restriction. Conflating them would make
    every connector's declaration need editing when the matcher grows an axis.

    ``sections`` maps a section name to the keys the channel acts on, or to
    ``None`` for "all of them". A section the channel does not name at all is a
    section it ignores, and a scope setting it warns as ineffective -- which is
    how cron gets told that ``delivery`` means nothing to it without cron
    needing any code that knows what ``delivery`` is.

    ``attended`` records whether a human can answer an approval here, and the
    ``permissions`` fold reads it: a scope's ``ask`` is enforced as ``deny`` where
    nobody can click, because an ask on an unattended surface is a turn that
    waits forever rather than a question. Declaring it ``False`` is therefore a
    live restriction. ``identity_keys`` is still
    declared-but-unread, its ordering saying which of a platform's several ids
    for one person will be canonical for matching.

    ``layer0_keys`` names, per section, the key in ``channels.<name>`` that
    already sets the same thing. It exists for one warning: a scope setting
    a key the connector block also sets leaves two places to look for one value,
    which is an authoring slip rather than a layering feature.

    ``validators`` is how a connector keeps its own value checks -- "is this one
    of the models actually configured?" -- without either duplicating them in
    the shared loader or forcing the shared loader to import a connector.

    ``cautions`` is the same hook for what a value cannot *reach*. It is handed
    the match as well as the value, and its reason is warned and the value kept:
    a setting that no request will arrive at is an authoring slip rather than an
    unusable value, and dropping it would take the rest of a well-written rule
    with it. Slack has the one entry -- an App Home event is delivered in a
    direct message, so a rule that has named some other kind of conversation
    can never see one.

    ``axis_restrictions`` answers the next question over: how a rule setting the
    key may be addressed. Keyed
    ``"<section>.<key>"`` like ``validators``, with ``"<section>.*"`` for a
    statement about the whole section.

    **A restriction lives where the key is declared, and that is the whole
    rule.** The ``agent`` and ``delivery`` sections have no shared key list --
    a key exists in them because a connector named it in ``sections`` -- so the
    axes it may be addressed on are part of that same declaration. Keys whose
    meaning the schema itself defines, such as the ``clicks`` gestures, are
    restricted in the schema's own table instead. The two are intersected, so a
    connector can narrow what the schema permits and can never widen it.

    ``axis_values`` is the legal vocabulary of one axis on this platform, for
    the axes whose values are a closed set rather than an opaque id. ``chat`` and
    ``user`` have no entry and never will: an id is whatever the platform issued,
    and nothing here can hold the list. ``chat_type`` does, because a platform
    has a fixed and small number of kinds of conversation and knows their names.

    **Declarative, in the spirit of ``axes``, and deliberately not a
    ``validators`` callback.** The match side is already declarative -- a
    connector says which axes it populates and the shared loader does the rest --
    and a vocabulary is the same kind of statement one level down. A callback
    would let two connectors accept the same word for different reasons, put the
    reason for a refusal in a function body rather than in the declaration, and
    give the shared loader no way to name the alternatives in its warning. The
    frozenset can be printed, which is what lets a bad value be reported with the
    words that would have worked.

    An axis with no entry has no vocabulary, and a value on it is taken as
    written. That is not a gap: it is the answer for every axis whose values are
    ids, and it is why this is a mapping rather than a list of pairs on every
    axis.

    A channel that populates ``user`` and declares a key barred from ``user`` is
    consistent: the sender is matchable, and that particular key is not the place
    to match on them.
    """

    channel: str
    axes: frozenset[str]
    sections: Mapping[str, "frozenset[str] | None"] = field(default_factory=dict)
    attended: bool = True
    identity_keys: tuple[str, ...] = ()
    layer0_keys: Mapping[str, Mapping[str, str]] = field(default_factory=dict)
    validators: Mapping[str, ValueValidator] = field(default_factory=dict)
    cautions: Mapping[str, ValueCaution] = field(default_factory=dict)
    axis_restrictions: Mapping[str, AxisRestriction] = field(default_factory=dict)
    axis_values: Mapping[str, frozenset[str]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # Frozen protects the binding, not the mapping behind it. A registry
        # every process shares is easy to mutate by accident and slow to debug
        # afterwards.
        object.__setattr__(self, "axes", frozenset(self.axes))
        object.__setattr__(self, "sections", _freeze_sections(self.sections))
        object.__setattr__(self, "layer0_keys", freeze_nested(self.layer0_keys))
        object.__setattr__(self, "validators", MappingProxyType(dict(self.validators)))
        object.__setattr__(self, "cautions", MappingProxyType(dict(self.cautions)))
        object.__setattr__(
            self, "axis_restrictions", MappingProxyType(dict(self.axis_restrictions))
        )
        object.__setattr__(
            self,
            "axis_values",
            MappingProxyType(
                {str(k): frozenset(v) for k, v in self.axis_values.items()}
            ),
        )

    def populates(self, axis: str) -> bool:
        return axis in self.axes

    def reads(self, section: str) -> bool:
        return section in self.sections

    def acts_on(self, section: str, key: str) -> bool:
        """Whether ``section.key`` is something this channel does anything with."""
        if section not in self.sections:
            return False
        allowed = self.sections[section]
        return allowed is None or key in allowed

    def keys_for(self, section: str) -> "frozenset[str] | None":
        return self.sections.get(section)

    def layer0_key(self, section: str, key: str) -> "str | None":
        return self.layer0_keys.get(section, {}).get(key)

    def validator(self, section: str, key: str) -> "ValueValidator | None":
        return self.validators.get(f"{section}.{key}")

    def caution(self, section: str, key: str) -> "ValueCaution | None":
        """What this channel wants said about ``section.key`` without dropping it."""
        return self.cautions.get(f"{section}.{key}")

    def axis_restriction(
        self, section: str, key: str = ANY_KEY
    ) -> "AxisRestriction | None":
        """What this channel says about how ``section.key`` may be addressed."""
        return restriction_in(self.axis_restrictions, section, key)

    def values_for(self, axis: str) -> "frozenset[str] | None":
        """The legal values of ``axis`` here, or ``None`` if it has no vocabulary.

        ``None`` and an empty frozenset are different answers and the caller must
        keep them apart. ``None`` is "this axis takes ids, match what is
        written"; empty is a declared vocabulary with nothing in it, which
        refuses every value and is the fail-closed end of the same axis.
        """
        return self.axis_values.get(axis)


def restriction_in(
    table: Mapping[str, AxisRestriction], section: str, key: str = ANY_KEY
) -> "AxisRestriction | None":
    """Read one axis restriction out of a table keyed ``"<section>.<key>"``.

    A section-wide entry and a per-key one are both consulted and both apply,
    intersected, because they are two true statements rather than two
    candidates: a section barred from an axis stays barred whatever one of its
    keys says, which is what stops a per-key entry being a way to widen a
    section-wide one written in the same file.

    A function rather than a method because two tables are read this way -- the
    schema's, which speaks for the keys it defines, and each channel's -- and
    the rule for reading them has to be one rule.
    """
    section_wide = table.get(f"{section}.{ANY_KEY}")
    if key == ANY_KEY:
        return section_wide
    per_key = table.get(f"{section}.{key}")
    if per_key is None:
        return section_wide
    return per_key.narrowed_by(section_wide)


_REGISTRY: dict[str, ChannelCapabilities] = {}
_DISCOVERED = False

# A connector declares by dropping this filename beside itself. Kept to a
# constant so the convention is stated once and greppable.
DECLARATION_MODULE_NAME = "scope_capabilities"
_DECLARATION_SEARCH_PACKAGE = "jiuwenswarm.gateway.channel_manager.im_platforms"


def _check_identity_declaration(capabilities: ChannelCapabilities) -> None:
    """Say when ``axes`` and ``identity_keys`` disagree about the sender.

    They are two statements about one fact, so they are cross-checked.
    ``identity_keys`` names which of a platform's several ids for
    one person is canonical for matching, so an empty tuple says the channel has
    no sender to match on -- which contradicts an ``axes`` that claims the
    ``user`` axis, and is contradicted by a non-empty tuple on one that does
    not.

    Warned on the module logger rather than through an injected ``warn``,
    because this is not a config mistake. Nobody deploying can cause it and
    nobody deploying can fix it: it is a connector's own declaration disagreeing
    with itself, and the audience is whoever is editing that file.
    """
    declares_axis = "user" in capabilities.axes
    if declares_axis and not capabilities.identity_keys:
        logger.warning(
            "scopes: %s declares the user axis but names no identity_keys, so"
            " there is no id to match a sender on; scopes naming a user there"
            " will never match",
            capabilities.channel,
        )
    elif capabilities.identity_keys and not declares_axis:
        logger.warning(
            "scopes: %s names identity_keys (%s) but does not declare the user"
            " axis, so those ids are never matched against anything",
            capabilities.channel,
            ", ".join(capabilities.identity_keys),
        )


def _check_axis_restriction_declaration(capabilities: ChannelCapabilities) -> None:
    """Say when a restriction is written against a key the channel never reads.

    Such an entry enforces nothing: the key is dropped earlier, by ``acts_on``,
    with the ordinary "has no effect" warning, and the restriction is never
    consulted. The declaration still reads as a live protection, so nothing in
    the file shows that the protection is absent.

    On the module logger for the reason :func:`_check_identity_declaration`
    gives: nobody deploying can cause this and nobody deploying can fix it. It
    is one file disagreeing with itself, and the audience is whoever is editing
    it.
    """
    for entry in sorted(capabilities.axis_restrictions):
        section, _, key = entry.partition(".")
        if not capabilities.reads(section):
            logger.warning(
                "scopes: %s restricts the axes of %s but does not read the %s"
                " section at all, so that restriction enforces nothing",
                capabilities.channel,
                entry,
                section,
            )
            continue
        if key != ANY_KEY and not capabilities.acts_on(section, key):
            logger.warning(
                "scopes: %s restricts the axes of %s but does not name %s among"
                " the %s keys it acts on, so that restriction enforces nothing;"
                " the key is dropped before it is reached",
                capabilities.channel,
                entry,
                key,
                section,
            )


def _check_axis_values_declaration(capabilities: ChannelCapabilities) -> None:
    """Say when a vocabulary is written for an axis the channel cannot fill.

    Such an entry settles nothing: the axis is never populated here, so no rule
    written on it can match and the vocabulary is never consulted. The
    declaration still reads as the list an operator's value is checked against,
    which is the state worth a line.

    On the module logger for the reason :func:`_check_identity_declaration`
    gives: nobody deploying can cause this and nobody deploying can fix it. It
    is one file disagreeing with itself, and the audience is whoever is editing
    it.
    """
    for axis in sorted(capabilities.axis_values):
        if not capabilities.populates(axis):
            logger.warning(
                "scopes: %s declares the legal values of the %s axis but does"
                " not populate that axis, so nothing is ever matched against"
                " them and that vocabulary checks nothing",
                capabilities.channel,
                axis,
            )


def register_channel(capabilities: ChannelCapabilities) -> None:
    """Add ``capabilities`` to the registry, replacing any earlier declaration.

    Replacing rather than refusing a duplicate is what makes a module reload
    survivable, and a duplicate can only come from one channel's own file being
    imported twice -- there is nowhere else for a second declaration of the same
    name to come from.
    """
    _check_identity_declaration(capabilities)
    _check_axis_restriction_declaration(capabilities)
    _check_axis_values_declaration(capabilities)
    _REGISTRY[capabilities.channel] = capabilities


def _discover() -> None:
    """Import every connector's declaration module, once.

    Tolerant by construction. A connector whose declaration cannot be imported
    is a connector that has not opted in, which is a state this design already
    has a meaning for; turning it into a startup failure would let one broken
    optional connector take down a gateway that was not using it.
    """
    global _DISCOVERED
    if _DISCOVERED:
        return
    # Set first: a failure below must not leave discovery re-running on every
    # lookup, which would turn one bad import into a per-message cost.
    _DISCOVERED = True

    try:
        import importlib
        from pathlib import Path

        package = importlib.import_module(_DECLARATION_SEARCH_PACKAGE)
        roots = [Path(p) for p in getattr(package, "__path__", [])]
    except Exception:  # pragma: no cover - the package is part of the install
        logger.debug("scopes: no connector package to search", exc_info=True)
        return

    seen: set[str] = set()
    for root in roots:
        try:
            candidates = sorted(root.glob(f"*/{DECLARATION_MODULE_NAME}.py"))
        except OSError:  # pragma: no cover - unreadable install tree
            continue
        for candidate in candidates:
            name = f"{_DECLARATION_SEARCH_PACKAGE}.{candidate.parent.name}.{DECLARATION_MODULE_NAME}"
            if name in seen:
                continue
            seen.add(name)
            try:
                importlib.import_module(name)
            except Exception:
                logger.warning(
                    "scopes: %s could not be imported, so that connector is not"
                    " opted in and scopes naming it will warn as inert",
                    name,
                    exc_info=True,
                )


def channel_capabilities(channel: str) -> "ChannelCapabilities | None":
    """The declaration for ``channel``, or ``None`` if it has not opted in."""
    _discover()
    return _REGISTRY.get(channel)


def known_channels() -> tuple[str, ...]:
    """Every channel that has declared, sorted, for use in warning text."""
    _discover()
    return tuple(sorted(_REGISTRY))


def snapshot_registry() -> dict[str, ChannelCapabilities]:
    """A copy of the registry, for a test that installs a synthetic channel.

    Snapshot-and-restore rather than clear-and-rediscover, because
    ``importlib.import_module`` returns an already-imported module without
    re-running it: a cleared registry would never see the real connectors again
    for the life of the process, and the tests that ran after the one that
    cleared it would be asserting against an empty table.
    """
    _discover()
    return dict(_REGISTRY)


def restore_registry(snapshot: Mapping[str, ChannelCapabilities]) -> None:
    """Put back what :func:`snapshot_registry` returned."""
    _REGISTRY.clear()
    _REGISTRY.update(snapshot)


# ---------------------------------------------------------------------------
# Pseudo-channels.
#
# These arrive with a ``channel_id`` and drive real turns, but there is no
# ``channels.__cron__`` block and no connector class, so their declarations live
# here rather than beside code that does not exist.
# ---------------------------------------------------------------------------

# The web UI is one conversation with the bot, so there is no ``chat`` axis to
# match on and no per-conversation delivery to configure. Declared all the same:
# saying "channel only" out loud is what turns ``{channel: web, chat: X}`` into
# a warning instead of a rule that silently never fires.
register_channel(
    ChannelCapabilities(
        channel="web",
        axes=frozenset({"channel"}),
        sections={"delivery": None, "permissions": None},
        attended=True,
        identity_keys=(),
    )
)

# Cron and the heartbeat have no trigger, no mention and no thread, so
# ``delivery`` is meaningless for them and is declared absent rather than empty:
# absent is what produces the "has no effect" warning. ``agent`` is left absent
# too, and that is a statement about these two and not about the section: a
# ``__cron__`` scope's ``model_name`` would be a useful default for the jobs
# that pin none, but nothing resolves scopes for cron yet, so declaring the
# section here would promise a resolution no code performs.
#
# ``permissions`` is different, and is declared, because the promise is kept:
# these runs reach the runtime with ``channel_id="__cron__"`` and the permission
# rail resolves the section from that id like any other. It is also what
# ``attended=False`` does here: an unattended run's ``ask`` becomes a ``deny``,
# so a scheduled job that touches a restricted tool fails with a reason rather
# than blocking until it is cancelled.
for _pseudo in ("__cron__", "__heartbeat__"):
    register_channel(
        ChannelCapabilities(
            channel=_pseudo,
            axes=frozenset({"channel"}),
            sections={"permissions": None},
            attended=False,
            identity_keys=(),
        )
    )
