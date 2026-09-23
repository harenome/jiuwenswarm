# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.

"""``people:`` and ``roles:`` -- who a name refers to, and nothing else.

Two top-level blocks, siblings of ``scopes:`` rather than anything under
``channels``:

.. code-block:: yaml

    people:
      alice: {slack: U000000AAAA, feishu: ou_a1b2c3}
      bob:   {slack: U000000BBBB}

    roles:
      admin: [alice, bob]

**Why they are not per connector.** A person is one human with an id on each
platform they are reachable on, so the mapping from a name to an id must not be
written once per connector: adding a platform to a person is one line here, and
every role they are in follows. Putting the block under ``channels.slack`` would
give the same human a separate identity per connector and make "the admins" mean
a different set of people on each. That is the shape ``roles`` exists to remove.

**The value under a platform is an id, or a list of them.** Identity is an
opaque per-channel bag, and a platform may have several ids for one
person -- Feishu's ``open_id`` and ``union_id`` are the standing example. What
reaches the matcher is one string: whichever of the platform's ``identity_keys``
the event held. So a person who is reachable under two ids on one platform
lists both, and matches whichever one arrives. A nested map keyed by
``identity_key`` would read better and could not be honoured: the matcher is
handed a sender, not a bag, and cannot know which key it came from.

**Roles carry no permissions, and that is a boundary rather than a gap.**
A role is a named set of people. All authority stays in ``scopes``, so there is
exactly one place to answer "what can happen here" and no role definition can
quietly raise a ceiling. The conventional RBAC shape -- ``admin: {tools: ...}``
-- does the opposite: authority is in two places, and a role definition becomes
a second route to escalation. A ``roles:`` entry written that way is refused,
with that reason.

**Nothing here raises, and nothing here can fail a load** -- the same contract as
``schema``. A broken entry is warned about and dropped, and what a dropped entry
costs is stated at each site, because for a role the two directions are not
symmetric: a scope that names a role nobody could resolve is dropped by
``schema``, which for a *restricting* scope means the restriction is not applied.

**A platform key nobody has declared is not warned about.** A directory of
humans legitimately lists ids for platforms this deployment does not run -- a
config shared between two deployments always will -- so a warning there would
fire on every correct file. The warning is emitted where an operator can act on
it instead: on the scope, when a role it names resolves to nobody on the channel
that scope is written for.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Mapping

from jiuwenswarm.common.scopes.capabilities import Warn, freeze_nested

logger = logging.getLogger(__name__)

#: The two top-level blocks this module reads. Named so the config key and the
#: warning text cannot drift apart.
PEOPLE_KEY = "people"
ROLES_KEY = "roles"


@dataclass(frozen=True)
class PeopleDirectory:
    """Who each name refers to, per platform, and which names a role holds.

    ``people`` maps a person's config name to ``{channel: (id, ...)}``. A person
    with no entry for a channel has no id there, which is the whole of section
    8.3's second fail-closed rule: they are not in any role *there*, so a
    restriction written as ``not: {role: admin}`` applies to them on that
    platform. Restrictions hold where nobody could be identified rather than
    lapsing, and that answer is the same one the ``user`` axis already gives for
    a sender the connector could not name.

    ``roles`` maps a role name to the person names in it, and to nothing else:
    the type is a tuple of names, so there is nowhere for a permission to be
    written even by accident.

    Flat by construction. Role nesting and inheritance are deferred (section
    12): flat sets stay auditable, and a hierarchy is where "who can actually do
    this" stops being answerable by reading.
    """

    people: Mapping[str, Mapping[str, tuple[str, ...]]] = field(default_factory=dict)
    roles: Mapping[str, tuple[str, ...]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "people", freeze_nested(self.people))
        object.__setattr__(
            self, "roles", MappingProxyType({str(k): tuple(v) for k, v in self.roles.items()})
        )

    def knows_role(self, role: str) -> bool:
        """Whether ``role`` was declared at all.

        Distinct from resolving to nobody, and the two are answered differently.
        An undeclared name is a config that cannot be honoured and the scope
        naming it is dropped; a declared role whose members happen to have no id
        on one platform is a config that *is* honoured, and its answer there is
        the fail-closed one.
        """
        return role in self.roles

    def members(self, role: str) -> tuple[str, ...]:
        return self.roles.get(role, ())

    def ids_for_role(self, role: str, *, channel: "str | None") -> tuple[str, ...]:
        """Every id the members of ``role`` have on ``channel``, sorted.

        ``()`` for an undeclared role, for a declared one none of whose members
        is identified on this platform, and for no channel at all. Those three
        configs all answer the same here, so a caller that has to tell them
        apart asks :meth:`knows_role` as well.
        """
        if not channel:
            return ()
        found: set[str] = set()
        for person in self.members(role):
            found.update(self.people.get(person, {}).get(channel, ()))
        return tuple(sorted(found))

    def channels_for_role(self, role: str) -> tuple[str, ...]:
        """Every platform some member of ``role`` is identified on, sorted.

        For one warning: a role that resolves to nobody *here* is worth telling
        an operator about, and naming the platforms it does resolve on turns
        "this rule never fires" into "you wrote the ids for the other one".
        """
        found: set[str] = set()
        for person in self.members(role):
            found.update(self.people.get(person, {}))
        return tuple(sorted(found))


#: What a config with neither block compiles to. Shared rather than rebuilt so
#: that the common case allocates nothing.
EMPTY_DIRECTORY = PeopleDirectory()


def _compile_person_ids(
    raw: Any, *, person: str, channel: str, warn: Warn
) -> tuple[str, ...]:
    """One person's ids on one platform, or ``()`` if none of them is usable.

    Both spellings are accepted here, and this is the one place in this feature
    where a bare scalar is not refused for a list. ``match.user`` refuses it
    because promoting an inline list to a role has to stay a pure refactor, so
    the axis must have one shape. Here the two shapes mean different things --
    one id, or several ids for the same human -- and one person having two ids
    on one platform is a property of that platform rather than an authoring
    style, so refusing the scalar would make the common entry the verbose one.
    """
    if isinstance(raw, str):
        values = [raw]
    elif isinstance(raw, (list, tuple)):
        values = list(raw)
    else:
        warn(
            "%s.%s.%s=%r is not an id or a list of ids; %s has no identity on"
            " %s, so no role they are in matches them there",
            PEOPLE_KEY,
            person,
            channel,
            raw,
            person,
            channel,
        )
        return ()

    ids: list[str] = []
    for item in values:
        if not isinstance(item, str):
            warn(
                "%s.%s.%s=%r is not an id; quote ids so YAML leaves them as"
                " written. Ignoring it",
                PEOPLE_KEY,
                person,
                channel,
                item,
            )
            continue
        text = item.strip()
        if not text:
            # Never kept. An empty string in a resolved id list would match a
            # sender the connector could not name -- the exact case both halves
            # of the identity axis are written to fail closed on.
            warn(
                "%s.%s.%s has an empty id in it; ignoring it, so it cannot match"
                " a sender the platform did not name",
                PEOPLE_KEY,
                person,
                channel,
            )
            continue
        ids.append(text)
    return tuple(sorted(set(ids)))


def _compile_people(raw: Any, *, warn: Warn) -> dict[str, dict[str, tuple[str, ...]]]:
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        warn(
            "%s=%r is not a mapping of person to their ids per platform;"
            " ignoring it, so every role resolves to nobody",
            PEOPLE_KEY,
            raw,
        )
        return {}

    settled: dict[str, dict[str, tuple[str, ...]]] = {}
    for raw_name, raw_ids in raw.items():
        name = str(raw_name).strip()
        if not name:
            warn("%s has an entry with no name; ignoring it", PEOPLE_KEY)
            continue
        if not isinstance(raw_ids, Mapping):
            warn(
                "%s.%s=%r is not a mapping of platform to id; ignoring that"
                " person, so no role they are named in includes them",
                PEOPLE_KEY,
                name,
                raw_ids,
            )
            continue
        per_channel: dict[str, tuple[str, ...]] = {}
        for raw_channel, value in raw_ids.items():
            channel = str(raw_channel).strip()
            if not channel:
                warn(
                    "%s.%s has an id under no platform; ignoring it, because an"
                    " id only means someone on the platform that issued it",
                    PEOPLE_KEY,
                    name,
                )
                continue
            ids = _compile_person_ids(value, person=name, channel=channel, warn=warn)
            if ids:
                per_channel[channel] = ids
        # Kept even with nothing usable in it. A person with no usable id
        # matches nothing, which is already the fail-closed answer. Dropping the
        # name as well would make every role naming them an undeclared-person
        # error, which drops the whole of each such role.
        settled[name] = per_channel
    return settled


def _compile_roles(
    raw: Any, *, people: Mapping[str, Mapping[str, tuple[str, ...]]], warn: Warn
) -> dict[str, tuple[str, ...]]:
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        warn(
            "%s=%r is not a mapping of role to the people in it; ignoring it, so"
            " every scope naming a role is dropped",
            ROLES_KEY,
            raw,
        )
        return {}

    declared = {str(key).strip() for key in raw}
    settled: dict[str, tuple[str, ...]] = {}
    for raw_name, raw_members in raw.items():
        name = str(raw_name).strip()
        if not name:
            warn("%s has an entry with no name; ignoring it", ROLES_KEY)
            continue
        if isinstance(raw_members, Mapping):
            # This is the shape every other RBAC system uses, so an author
            # reaching for it is following a habit rather than making a mistake,
            # and being told "not a list" would leave them looking for a syntax
            # error.
            warn(
                "%s.%s is a mapping. A role is a named set of people and carries"
                " no permissions: write it as a list of names from %s, and put"
                " what they may do in a scope matching %s: %s. Ignoring that"
                " role, so scopes naming it are dropped rather than half-applied",
                ROLES_KEY,
                name,
                PEOPLE_KEY,
                "role",
                name,
            )
            continue
        if isinstance(raw_members, str):
            warn(
                "%s.%s=%r is a single name where a list is wanted; write [%s]."
                " Ignoring that role",
                ROLES_KEY,
                name,
                raw_members,
                raw_members,
            )
            continue
        if not isinstance(raw_members, (list, tuple)):
            warn(
                "%s.%s=%r is not a list of names from %s; ignoring that role",
                ROLES_KEY,
                name,
                raw_members,
                PEOPLE_KEY,
            )
            continue

        members: list[str] = []
        broken = False
        for item in raw_members:
            if not isinstance(item, str) or not item.strip():
                warn(
                    "%s.%s=%r names something that is not a person's name;"
                    " ignoring that role",
                    ROLES_KEY,
                    name,
                    raw_members,
                )
                broken = True
                break
            member = item.strip()
            if member in people:
                members.append(member)
                continue
            if member in declared:
                # Named separately from a typo because it is not one: it is the
                # feature section 12 defers, and an author who wrote it had a
                # reason rather than a slip.
                warn(
                    "%s.%s names the role %s. A role holds people, not other"
                    " roles -- nesting is not supported, because a hierarchy is"
                    " where 'who can actually do this' stops being answerable by"
                    " reading. Ignoring that role; list the people out",
                    ROLES_KEY,
                    name,
                    member,
                )
            else:
                warn(
                    "%s.%s names %s, who is not in %s. Ignoring the whole role"
                    " rather than the one name: a role missing a member would"
                    " apply a restriction to exactly the person a"
                    " not: {role: %s} was written to exempt. Scopes naming %s"
                    " are dropped and their conversations fall to the layer"
                    " below, so a restriction written that way is not applied"
                    " either -- add %s to %s",
                    ROLES_KEY,
                    name,
                    member,
                    PEOPLE_KEY,
                    name,
                    name,
                    member,
                    PEOPLE_KEY,
                )
            broken = True
            break
        if broken:
            continue

        if not members:
            warn(
                "%s.%s is empty, which names nobody; ignoring that role rather"
                " than reading it as everyone or as nobody, either of which"
                " would be a rule nobody wrote",
                ROLES_KEY,
                name,
            )
            continue
        settled[name] = tuple(sorted(set(members)))
    return settled


def compile_people(
    people: Any = None,
    roles: Any = None,
    *,
    warn: "Warn | None" = None,
) -> PeopleDirectory:
    """Read the ``people:`` and ``roles:`` blocks into one directory.

    ``warn`` is injected for the same reason it is in ``schema``: this project's
    loggers do not propagate, so a test written against ``caplog`` would assert
    nothing under the pytest CI runs on.

    ``roles`` is compiled against the settled ``people``, so a role naming
    somebody the ``people:`` block dropped is caught here rather than resolving
    to a smaller set than it reads as.
    """
    emit: Warn = warn if warn is not None else logger.warning
    if people is None and roles is None:
        return EMPTY_DIRECTORY
    settled_people = _compile_people(people, warn=emit)
    return PeopleDirectory(
        people=settled_people,
        roles=_compile_roles(roles, people=settled_people, warn=emit),
    )
