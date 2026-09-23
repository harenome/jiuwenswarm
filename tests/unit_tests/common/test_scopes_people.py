# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.

"""``people:`` and ``roles:``, and the ``role`` axis they stand behind.

Warnings are collected through the injected ``warn`` rather than through
``caplog``: this project's loggers do not propagate, so ``caplog`` sees nothing
under the pytest CI runs on, and a test written against it would pass locally
and assert nothing where it matters.
"""

from __future__ import annotations

from dataclasses import fields
from typing import Any

import pytest

from jiuwenswarm.common.scopes import (
    ChannelCapabilities,
    PeopleDirectory,
    Scope,
    ScopeMatch,
    compile_people,
    compile_scopes,
    matching_scopes,
    register_channel,
)
from jiuwenswarm.common.scopes import capabilities as caps


@pytest.fixture
def warnings() -> list[str]:
    return []


@pytest.fixture
def warn(warnings: list[str]):
    def record(message: str, *args: Any) -> None:
        warnings.append(message % args if args else message)

    return record


@pytest.fixture
def demo_channel():
    """A channel that reads everything and names a sender."""
    snapshot = caps.snapshot_registry()
    register_channel(
        ChannelCapabilities(
            channel="demo",
            axes=frozenset({"channel", "chat", "user"}),
            sections={"delivery": None, "agent": None, "permissions": None},
            identity_keys=("user",),
        )
    )
    yield
    caps.restore_registry(snapshot)


@pytest.fixture
def quiet_channel():
    """A channel that reads sections but identifies no sender."""
    snapshot = caps.snapshot_registry()
    register_channel(
        ChannelCapabilities(
            channel="quiet",
            axes=frozenset({"channel", "chat"}),
            sections={"delivery": None},
            identity_keys=(),
        )
    )
    yield
    caps.restore_registry(snapshot)


PEOPLE = {
    "harenome": {"demo": "U_HAR", "other": "O_HAR"},
    "boss": {"demo": "U_BOSS"},
    "remote": {"other": "O_REMOTE"},
}
ROLES = {"admin": ["harenome", "boss"], "elsewhere": ["remote"]}


def _compile(entries: Any, warn, **kwargs: Any) -> tuple[Scope, ...]:
    kwargs.setdefault("people", PEOPLE)
    kwargs.setdefault("roles", ROLES)
    return compile_scopes(entries, warn=warn, **kwargs)


# --------------------------------------------------------------------------
# The directory itself
# --------------------------------------------------------------------------


def test_a_person_is_a_name_with_one_id_per_platform(warn, warnings):
    directory = compile_people(PEOPLE, ROLES, warn=warn)

    assert warnings == []
    assert directory.knows_role("admin")
    assert directory.members("admin") == ("boss", "harenome")
    assert directory.ids_for_role("admin", channel="demo") == ("U_BOSS", "U_HAR")
    # The same role on another platform is a different set of ids, which is why
    # a role cannot be resolved without knowing which platform is being asked.
    assert directory.ids_for_role("admin", channel="other") == ("O_HAR",)


def test_a_platform_may_carry_several_ids_for_one_person(warn, warnings):
    directory = compile_people(
        {"carol": {"feishu": ["ou_a1b2c3", "on_d4e5f6"]}}, {"team": ["carol"]}, warn=warn
    )

    # Feishu has three ids for one person and the matcher is handed one string,
    # whichever the event held. Listing both is the only shape that can be
    # honoured: a map keyed by identity_key would need the matcher to know which
    # key the sender came from, and it is given a sender rather than a bag.
    assert warnings == []
    assert directory.ids_for_role("team", channel="feishu") == ("on_d4e5f6", "ou_a1b2c3")


def test_neither_block_written_is_an_empty_directory(warn, warnings):
    directory = compile_people(None, None, warn=warn)

    assert warnings == []
    assert directory.roles == {}
    assert not directory.knows_role("admin")


def test_an_id_is_stripped_and_deduplicated(warn, warnings):
    directory = compile_people(
        {"alice": {"demo": [" U1 ", "U1", "U2"]}}, {"team": ["alice"]}, warn=warn
    )

    assert warnings == []
    assert directory.ids_for_role("team", channel="demo") == ("U1", "U2")


def test_an_empty_id_is_never_kept(warn, warnings):
    directory = compile_people(
        {"alice": {"demo": ["", "U1"]}}, {"team": ["alice"]}, warn=warn
    )

    # An empty string in a resolved list would match a sender the connector
    # could not name -- the exact case both halves of the identity axis are
    # written to fail closed on.
    assert directory.ids_for_role("team", channel="demo") == ("U1",)
    assert any("has an empty id in it" in line for line in warnings)


def test_a_person_with_no_usable_ids_is_still_a_declared_person(warn, warnings):
    directory = compile_people(
        {"ghost": {"demo": 12345}}, {"team": ["ghost"]}, warn=warn
    )

    # Kept as a name so that the role naming them is still a role. Dropping the
    # person would turn one unquoted id into an undeclared-person error and take
    # the whole role -- and every scope using it -- down with it.
    assert directory.knows_role("team")
    assert directory.ids_for_role("team", channel="demo") == ()
    assert any("is not an id or a list of ids" in line for line in warnings)


# --------------------------------------------------------------------------
# Shapes the directory refuses
# --------------------------------------------------------------------------


def test_a_role_carrying_permissions_is_refused_with_that_reason(warn, warnings):
    directory = compile_people(
        {"alice": {"demo": "U1"}},
        {"admin": {"people": ["alice"], "tools": {"bash": "allow"}}},
        warn=warn,
    )

    # This is the shape every other RBAC system uses, so an author reaching
    # for it is following a habit rather than making a typo, and the warning has
    # to say where authority actually lives rather than just "not a list".
    assert not directory.knows_role("admin")
    assert any(
        "carries no permissions" in line and "put what they may do in a scope" in line
        for line in warnings
    )


def test_a_role_naming_somebody_who_is_not_a_person_drops_the_whole_role(
    warn, warnings
):
    directory = compile_people(
        {"alice": {"demo": "U1"}}, {"admin": ["alice", "nobody"]}, warn=warn
    )

    # The whole role, not the one name. A role quietly missing a member would
    # apply a restriction to exactly the person a not: {role: admin} was written
    # to exempt -- which is the failure the fail-closed rules exist to prevent,
    # and it is invisible.
    assert not directory.knows_role("admin")
    assert any("who is not in people" in line for line in warnings)


def test_a_role_naming_another_role_is_reported_as_nesting_not_as_a_typo(
    warn, warnings
):
    directory = compile_people(
        {"alice": {"demo": "U1"}},
        {"admin": ["alice"], "everyone": ["admin"]},
        warn=warn,
    )

    # Deferred rather than wrong (section 12): flat sets stay auditable, and an
    # author who wrote it had a reason. Telling them it is not a person's name
    # would send them looking for a spelling mistake.
    assert directory.knows_role("admin")
    assert not directory.knows_role("everyone")
    assert any("nesting is not supported" in line for line in warnings)


def test_an_empty_role_is_dropped_rather_than_read_as_everyone(warn, warnings):
    directory = compile_people({"alice": {"demo": "U1"}}, {"admin": []}, warn=warn)

    assert not directory.knows_role("admin")
    assert any("names nobody" in line for line in warnings)


def test_a_bare_name_where_a_list_is_wanted_drops_the_role(warn, warnings):
    directory = compile_people(
        {"alice": {"demo": "U1"}}, {"admin": "alice"}, warn=warn
    )

    assert not directory.knows_role("admin")
    assert any("is a single name where a list is wanted" in line for line in warnings)


def test_a_people_block_that_is_not_a_mapping_leaves_every_role_unresolvable(
    warn, warnings
):
    directory = compile_people(["alice"], {"admin": ["alice"]}, warn=warn)

    assert directory.people == {}
    assert not directory.knows_role("admin")
    assert any("is not a mapping of person to their ids" in line for line in warnings)


def test_a_person_whose_ids_are_not_a_mapping_is_dropped(warn, warnings):
    directory = compile_people({"alice": "U1"}, None, warn=warn)

    assert directory.people == {}
    assert any("is not a mapping of platform to id" in line for line in warnings)


def test_a_platform_this_deployment_does_not_run_is_not_warned_about(warn, warnings):
    """A directory of humans legitimately names platforms this host has never
    heard of -- a config shared between two deployments always will -- so a
    warning there would fire on every correct file. The actionable warning is
    emitted on the scope instead, where a role resolves to nobody."""
    compile_people({"alice": {"a-platform-nobody-declared": "X1"}}, None, warn=warn)

    assert warnings == []


# --------------------------------------------------------------------------
# The role axis
# --------------------------------------------------------------------------


def test_a_role_selects_the_people_in_it(demo_channel, warn, warnings):
    scopes = _compile(
        [{"match": {"channel": "demo", "role": "admin"}, "delivery": {"mode": ["all"]}}],
        warn,
    )

    assert warnings == []
    assert scopes[0].selects(channel="demo", chat="C1", user="U_HAR")
    assert scopes[0].selects(channel="demo", chat="C1", user="U_BOSS")
    assert not scopes[0].selects(channel="demo", chat="C1", user="U_OTHER")


def test_a_role_is_resolved_into_the_ids_user_would_have_enumerated(
    demo_channel, warn, warnings
):
    by_role = _compile(
        [{"match": {"channel": "demo", "role": "admin"}, "delivery": {"mode": ["all"]}}],
        warn,
    )
    by_id = _compile(
        [
            {
                "match": {"channel": "demo", "user": ["U_HAR", "U_BOSS"]},
                "delivery": {"mode": ["all"]},
            }
        ],
        warn,
    )

    # Stated as an equality: a role is sugar over user for the matcher, so the
    # two spellings must resolve to the same ids and not merely behave alike.
    assert by_role[0].match.users == by_id[0].match.users

    # The layer is where they differ, and deliberately. How many people the
    # role holds is settled in roles: rather than in the rule, so it cannot be
    # ordered against a written-out list without reading a second block --
    # which is why the role takes the coarse identity grain and the ids the
    # fine one. It is a fact about the config here that the two happen to name
    # the same people; the rule cannot say so.
    assert by_role[0].layer == (1, 0, 0, 1)
    assert by_id[0].layer == (1, 0, 0, 2)


def test_user_and_role_or_rather_than_and(demo_channel, warn, warnings):
    scopes = _compile(
        [
            {
                "match": {"channel": "demo", "user": ["U_GUEST"], "role": "admin"},
                "delivery": {"mode": ["all"]},
            }
        ],
        warn,
    )

    # AND would mean "U_GUEST, but only if also an admin", which nobody wants
    # and which a config author would write by accident.
    assert warnings == []
    for sender in ("U_GUEST", "U_HAR", "U_BOSS"):
        assert scopes[0].selects(channel="demo", chat="C1", user=sender), sender
    assert not scopes[0].selects(channel="demo", chat="C1", user="U_OTHER")


def test_a_not_on_a_role_is_the_read_only_except_admins_rule(
    demo_channel, warn, warnings
):
    scopes = _compile(
        [
            {
                "match": {"channel": "demo", "chat": "C1", "not": {"role": "admin"}},
                "delivery": {"prompt": "Read-only here."},
            }
        ],
        warn,
    )

    # Section 4.3's motivating case, and the reason there is no override:
    # directive. An admin matches no restricting scope and keeps whatever the
    # ceiling gives them.
    #
    # Written against ``delivery`` because the matcher is what is under test
    # here and ``permissions`` is barred from the identity axes for as long as
    # its consumer is handed no sender -- see the axis-restrictions file. The
    # matcher half of 4.3 is ready for the section; the runtime is not.
    assert warnings == []
    assert scopes[0].selects(channel="demo", chat="C1", user="U_OTHER")
    assert not scopes[0].selects(channel="demo", chat="C1", user="U_HAR")


def test_a_not_may_exclude_ids_and_a_role_together(demo_channel, warn, warnings):
    scopes = _compile(
        [
            {
                "match": {
                    "channel": "demo",
                    "not": {"user": ["U_GUEST"], "role": "admin"},
                },
                "delivery": {"mode": ["all"]},
            }
        ],
        warn,
    )

    # The union, not the last key written. Both spellings name one axis, so a
    # not: block naming both excludes both.
    assert scopes[0].match.not_users == ("U_BOSS", "U_GUEST", "U_HAR")
    assert not scopes[0].selects(channel="demo", chat="C1", user="U_GUEST")
    assert not scopes[0].selects(channel="demo", chat="C1", user="U_HAR")
    assert scopes[0].selects(channel="demo", chat="C1", user="U_OTHER")


def test_a_role_minus_one_person_is_expressible(demo_channel, warn, warnings):
    scopes = _compile(
        [
            {
                "match": {
                    "channel": "demo",
                    "role": "admin",
                    "not": {"user": ["U_BOSS"]},
                },
                "delivery": {"mode": ["all"]},
            }
        ],
        warn,
    )

    # The case that justifies allowing a positive and a negative together:
    # without it, "the team minus one" means enumerating the team,
    # which rots the moment the team changes.
    assert warnings == []
    assert scopes[0].selects(channel="demo", chat="C1", user="U_HAR")
    assert not scopes[0].selects(channel="demo", chat="C1", user="U_BOSS")


def test_several_roles_may_be_named_and_they_or(demo_channel, warn, warnings):
    scopes = _compile(
        [
            {
                "match": {"channel": "demo", "role": ["admin", "elsewhere"]},
                "delivery": {"mode": ["all"]},
            }
        ],
        warn,
    )

    # "elsewhere" holds nobody with a demo id, so it adds nothing here and says
    # so; naming it alongside a role that does resolve must not lose the ids
    # that one contributed.
    assert scopes[0].match.users == ("U_BOSS", "U_HAR")
    assert any("adds nobody on demo" in line for line in warnings)


# --------------------------------------------------------------------------
# Specificity: identity counts once, however it is spelled
# --------------------------------------------------------------------------


def test_a_role_does_not_outrank_an_id_on_the_same_axis(demo_channel, warn, warnings):
    scopes = _compile(
        [
            {
                "match": {"channel": "demo", "chat": "C1", "user": ["U_HAR"]},
                "delivery": {"prompt": "by id"},
            },
            {
                "match": {"channel": "demo", "chat": "C1", "role": "admin"},
                "delivery": {"prompt": "by role"},
            },
            {
                "match": {
                    "channel": "demo",
                    "chat": "C1",
                    "role": "admin",
                    "not": {"user": ["U_BOSS"]},
                },
                "delivery": {"prompt": "by role, minus one"},
            },
        ],
        warn,
    )

    # One axis, one component of the tuple. Counting the keys would have put
    # the third scope a whole layer up for carrying an exemption, which is the
    # promotion the grain is written to prevent: the two role rules sit at the
    # same grain whether or not one of them carves people out.
    #
    # The id rule outranks both, and that is the correction this scheme makes.
    # "admin" resolves to the two ids here, so it is no narrower than the list
    # -- but nothing in the rule says how big the role is, and on a config
    # where it holds the whole workspace the old tie would have let the wider
    # rule win on file position alone.
    assert [scope.layer for scope in scopes] == [(1, 0, 2, 2), (1, 0, 2, 1), (1, 0, 2, 1)]

    ordered = matching_scopes(scopes, channel="demo", chat="C1", user="U_HAR")
    assert [scope.index for scope in ordered] == [1, 2, 0]
    assert ordered[-1].section("delivery") == {"prompt": "by id"}


def test_a_chat_scope_outranks_a_role_scope_either_way_round(
    demo_channel, warn, warnings
):
    role_first = _compile(
        [
            {"match": {"channel": "demo", "role": "admin"}, "delivery": {"prompt": "r"}},
            {"match": {"channel": "demo", "chat": "C1"}, "delivery": {"prompt": "c"}},
        ],
        warn,
    )
    chat_first = _compile(
        [
            {"match": {"channel": "demo", "chat": "C1"}, "delivery": {"prompt": "c"}},
            {"match": {"channel": "demo", "role": "admin"}, "delivery": {"prompt": "r"}},
        ],
        warn,
    )

    # The same ordering {channel, user} and {channel, chat} have, and a role
    # must not escape it: the conversation is read before the sender however
    # the sender is spelled, so the chat rule wins from either position.
    assert [scope.layer for scope in role_first] == [(1, 0, 0, 1), (1, 0, 2, 0)]
    assert [scope.layer for scope in chat_first] == [(1, 0, 2, 0), (1, 0, 0, 1)]

    def _last(scopes):
        return matching_scopes(scopes, channel="demo", chat="C1", user="U_HAR")[-1]

    assert _last(role_first).section("delivery") == {"prompt": "c"}
    assert _last(chat_first).section("delivery") == {"prompt": "c"}


def test_a_role_that_resolves_to_nobody_here_still_constrains_identity(
    demo_channel, warn, warnings
):
    scopes = _compile(
        [
            {
                "match": {"channel": "demo", "chat": "C1", "role": "elsewhere"},
                "delivery": {"prompt": "nobody here"},
            }
        ],
        warn,
    )

    # It matches nobody, and that is not the same as saying nothing about who is
    # asking. Reading it as unconstrained would drop its identity grain to 0 for
    # having matched nobody, and it would then start beating rules it had been
    # sitting under.
    assert scopes[0].match.users == ()
    assert scopes[0].layer == (1, 0, 2, 1)
    assert not scopes[0].selects(channel="demo", chat="C1", user="U_HAR")


# --------------------------------------------------------------------------
# Fail-closed
# --------------------------------------------------------------------------


def test_a_person_with_no_id_here_is_not_in_the_role_here(demo_channel, warn, warnings):
    scopes = _compile(
        [
            {
                "match": {"channel": "demo", "not": {"role": "elsewhere"}},
                # ``delivery`` rather than ``permissions``: the rule under test
                # is addressed on an identity axis, which that section may not
                # be until its consumer is handed a sender.
                "delivery": {"prompt": "Restricted here."},
            }
        ],
        warn,
    )

    # Section 8.3's second rule. "remote" is in the role and has an id on
    # "other" but not on "demo", so the exemption does not reach them here and
    # the restriction applies: restrictions hold where nobody could be
    # identified rather than lapsing. Exactly what the user axis already answers
    # for a sender the connector could not name.
    assert scopes[0].match.not_users == ()
    assert scopes[0].selects(channel="demo", chat="C1", user="O_REMOTE")
    assert scopes[0].selects(channel="demo", chat="C1", user=None)
    assert any(
        "excludes nobody on demo" in line and "they are identified on other" in line
        for line in warnings
    )


def test_an_unidentified_sender_answers_a_role_the_way_it_answers_an_id(
    demo_channel, warn, warnings
):
    scopes = _compile(
        [
            {"match": {"channel": "demo", "role": "admin"}, "delivery": {"prompt": "in"}},
            {
                "match": {"channel": "demo", "not": {"role": "admin"}},
                "delivery": {"prompt": "out"},
            },
        ],
        warn,
    )

    for absent in (None, "", "   "):
        assert not scopes[0].selects(channel="demo", chat="C1", user=absent)
        assert scopes[1].selects(channel="demo", chat="C1", user=absent)


def test_an_undeclared_role_drops_the_scope_rather_than_naming_nobody(
    demo_channel, warn, warnings
):
    scopes = _compile(
        [
            {
                "match": {"channel": "demo", "chat": "C1", "not": {"role": "admins"}},
                "permissions": {"tools": {"bash": "deny"}},
            }
        ],
        warn,
    )

    # A plural typo for "admin". Read as "nobody", the not: would exclude nobody
    # and land the restriction on exactly the people it was written to exempt.
    # Dropped, the conversation falls to the layer below -- which for a
    # restricting rule means the restriction is not applied, so the warning says
    # so rather than reading as housekeeping.
    assert scopes == ()
    assert any(
        "does not declare" in line and "leaves that conversation on the layer below"
        in line
        for line in warnings
    )


def test_a_role_used_with_no_people_block_at_all_drops_the_scope(
    demo_channel, warn, warnings
):
    scopes = compile_scopes(
        [{"match": {"channel": "demo", "role": "admin"}, "delivery": {"mode": ["all"]}}],
        warn=warn,
    )

    # The default for a caller that passes no directory. Stated as a test
    # because it is what a call site forgetting to pass people/roles looks
    # like: every role becomes undeclared and every scope naming one is
    # dropped, loudly, rather than matching everybody.
    assert scopes == ()
    assert any("does not declare" in line for line in warnings)


def test_a_role_named_without_a_platform_drops_the_scope(demo_channel, warn, warnings):
    scopes = _compile([{"match": {"role": "admin"}, "delivery": {"mode": ["all"]}}], warn)

    # A role resolves to different ids on each platform its members are on, so
    # "the admins, everywhere" is not a set this can look up. Stricter than the
    # user axis's version of the same rule rather than looser.
    assert scopes == ()
    assert any("without saying which platform" in line for line in warnings)


def test_a_role_on_a_channel_that_names_no_sender_warns_in_the_authors_spelling(
    quiet_channel, warn, warnings
):
    scopes = _compile(
        [{"match": {"channel": "quiet", "role": "admin"}, "delivery": {"mode": ["all"]}}],
        warn,
    )

    # Warned rather than dropped, and warned as "role" rather than as "user":
    # this is the channel failing to fill an axis, not a config that cannot be
    # read, and it is the class of mistake the capability check reports for
    # every spelling of every axis. Naming "user" here would send an operator
    # looking for a line that is not in their file.
    assert len(scopes) == 1
    assert any("match.role is set but quiet does not identify a sender" in line for line in warnings)


# --------------------------------------------------------------------------
# Shapes the axis refuses
# --------------------------------------------------------------------------


def test_an_empty_role_list_drops_the_scope(demo_channel, warn, warnings):
    assert (
        _compile(
            [{"match": {"channel": "demo", "role": []}, "delivery": {"mode": ["all"]}}],
            warn,
        )
        == ()
    )
    assert any("names no role" in line for line in warnings)


def test_a_role_that_is_not_a_name_drops_the_scope(demo_channel, warn, warnings):
    assert (
        _compile(
            [{"match": {"channel": "demo", "role": 7}, "delivery": {"mode": ["all"]}}],
            warn,
        )
        == ()
    )
    assert any("is not a role name or a list of them" in line for line in warnings)


def test_no_axis_is_deferred_any_more():
    # Pinned as an equality rather than as "role not in it", so that an axis
    # added ahead of its reader has to be thought about here rather than
    # joining the list silently. The name is kept for exactly that case.
    from jiuwenswarm.common.scopes import DEFERRED_AXES, NOT_AXES, SUPPORTED_AXES

    assert DEFERRED_AXES == ()
    assert SUPPORTED_AXES == (
        "channel",
        "workspace",
        "chat",
        "chat_type",
        "user",
        "role",
    )
    assert NOT_AXES == ("user", "role")


def test_the_description_shows_the_ids_a_role_resolved_to(demo_channel, warn):
    scopes = _compile(
        [
            {
                "match": {"channel": "demo", "chat": "C1", "role": "admin"},
                "delivery": {"mode": ["all"]},
            }
        ],
        warn,
    )

    # Both, because they can differ: a role resolving to fewer people than it
    # reads as is the case worth being able to see in a log line.
    described = scopes[0].match.describe()
    assert "role: [admin]" in described
    assert "user: [U_BOSS, U_HAR]" in described


def test_a_directory_can_be_handed_over_already_built(demo_channel, warn, warnings):
    directory = compile_people(PEOPLE, ROLES, warn=warn)
    scopes = compile_scopes(
        [{"match": {"channel": "demo", "role": "admin"}, "delivery": {"mode": ["all"]}}],
        directory=directory,
        warn=warn,
    )

    assert isinstance(directory, PeopleDirectory)
    assert scopes[0].match.users == ("U_BOSS", "U_HAR")
    assert warnings == []


def test_role_resolution_carries_every_axis_it_did_not_resolve(warn, warnings):
    """Resolution rebuilds the match, so an axis left out stops constraining.

    That has been shipped once. When ``chat_type`` was added, the rebuild in
    ``_resolve_roles`` carried every axis except the new one, so ``{channel,
    chat_type: direct, role: admin}`` compiled to the admins *everywhere* on the
    platform -- a rule reaching further than it was written to reach, and
    sitting a layer lower while it did so, decided by nothing more than whether
    it happened to name a role.

    Written over the dataclass's own fields rather than over a list of axis
    names, so the next axis is covered by the comparison rather than by somebody
    remembering this file. The guard below is what keeps that true: an axis
    neither exercised by the rules here nor folded by resolution fails it, which
    is the reminder to name the new axis in the rules above.
    """
    snapshot = caps.snapshot_registry()
    register_channel(
        ChannelCapabilities(
            channel="every",
            axes=frozenset({"channel", "workspace", "chat", "chat_type", "user"}),
            sections={"delivery": None},
            identity_keys=("user",),
            axis_values={"chat_type": frozenset({"room", "direct"})},
        )
    )
    # ``chat`` and ``chat_type`` are refused together, so the axes take two
    # rules between them rather than one rule naming all of them.
    written = (
        {"channel": "every", "workspace": "T1", "chat": "C1"},
        {"channel": "every", "chat_type": "direct"},
    )
    people = {"boss": {"every": "U_BOSS"}}
    roles = {"admin": ["boss"]}
    try:
        with_role = compile_scopes(
            [
                {"match": {**match, "role": "admin"}, "delivery": {"mode": ["all"]}}
                for match in written
            ],
            people=people,
            roles=roles,
            warn=warn,
        )
        without_role = compile_scopes(
            [
                {"match": dict(match), "delivery": {"mode": ["all"]}}
                for match in written
            ],
            people=people,
            roles=roles,
            warn=warn,
        )
    finally:
        caps.restore_registry(snapshot)

    assert warnings == []
    assert len(with_role) == len(without_role) == len(written)

    # The only fields resolution may write: the ids a role folds into, and the
    # names it keeps beside them for the description.
    folded = {"users", "roles"}
    for resolved, plain in zip(with_role, without_role):
        for field in fields(ScopeMatch):
            if field.name in folded:
                continue
            assert getattr(resolved.match, field.name) == getattr(
                plain.match, field.name
            ), f"role resolution dropped {field.name}"

    named = {
        field.name
        for field in fields(ScopeMatch)
        if any(getattr(scope.match, field.name) is not None for scope in with_role)
    }
    # Every field is either addressed by the rules above or one of the halves of
    # the identity axis they do not write. An axis added later lands in neither
    # and fails here, which is the point: the comparison above can only protect
    # an axis some rule in this test actually names.
    assert {field.name for field in fields(ScopeMatch)} - named == {
        "not_users",
        "not_roles",
    }

    # And the behaviour the dropped axis used to give away, said once in the
    # terms an operator would have met it in.
    assert with_role[1].match.chat_type == ("direct",)
    assert with_role[1].selects(
        channel="every", chat="D1", chat_type="direct", user="U_BOSS"
    )
    assert not with_role[1].selects(
        channel="every", chat="C9", chat_type="room", user="U_BOSS"
    )
