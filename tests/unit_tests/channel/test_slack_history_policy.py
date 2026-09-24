# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The one word that says how far Slack history may read, and how it is settled.

Five words replacing an allow-list of conversation ids. The tests here cover the
resolver -- which word a written config settles on, including the deprecated key
it retires -- and the contract with the two processes that read the answer: the
names it travels under on request metadata, and the fact that the connector, the
cron path and the runtime toolkit agree about them.

The gate the word *governs* is in ``test_slack_history_gate.py``. This file is
about how the word is arrived at and held, which is a separate failure: a
correct gate applied to a policy nobody wrote is not a safe deployment.
"""

from __future__ import annotations

import logging
import re
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

import pytest
import yaml

from jiuwenswarm.common import slack_history_policy as policy_module
from jiuwenswarm.common.slack_history_policy import (
    HISTORY_DISABLED,
    HISTORY_MEMBERS,
    HISTORY_OPEN,
    HISTORY_ORIGIN,
    HISTORY_POLICY_DEFAULT,
    HISTORY_POLICY_NAMES_A_TARGET,
    HISTORY_POLICY_VALUES,
    HISTORY_VISIBLE,
    KEY_HISTORY,
    KEY_HISTORY_EXEMPT_MEMBERS,
    KEY_HISTORY_NEVER_READ,
    LEGACY_KEY_HISTORY_CHANNEL_IDS,
    METADATA_ASKER_KEY,
    METADATA_EXEMPT_MEMBERS_KEY,
    METADATA_NEVER_READ_KEY,
    METADATA_POLICY_KEY,
    history_policy_from_legacy_channel_ids,
    history_policy_metadata,
    resolve_history_exempt_members,
    resolve_history_never_read,
    resolve_history_policy,
)


@contextmanager
def _captured(logger_name: str) -> Iterator[list[logging.LogRecord]]:
    """Records emitted by one logger, taken off that logger directly.

    Not ``caplog``: the handler pytest installs sits on the root logger, so what
    it sees depends on whether this package's loggers propagate -- which is a
    property of whatever configured logging first, not of the code under test.
    """
    records: list[logging.LogRecord] = []
    logger = logging.getLogger(logger_name)
    handler = logging.Handler()
    handler.emit = records.append  # type: ignore[method-assign]
    previous_level = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    try:
        yield records
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous_level)


def _warnings(records: list[logging.LogRecord]) -> list[str]:
    return [r.getMessage() for r in records if r.levelno >= logging.WARNING]


@pytest.fixture(autouse=True)
def _forget_translation_warnings() -> Iterator[None]:
    """The resolver says a deprecation once per process; tests are not one run.

    Cleared around every test so that one asserting the warning is emitted does
    not depend on having run before one that merely resolves the same config.
    """
    policy_module._WARNED_LEGACY_TRANSLATIONS.clear()
    yield
    policy_module._WARNED_LEGACY_TRANSLATIONS.clear()


# ── 1. The vocabulary ────────────────────────────────────────────────────────


def test_the_five_words_are_ordered_and_the_default_is_the_narrowest() -> None:
    """The order is the design's claim about them and is asserted, not assumed.

    Each word is strictly wider than the one above it, which is what lets an
    operator read the key without a table and what makes "at least members" a
    membership test rather than a lookup.

    ``visible`` sits between ``members`` and ``open`` and its position is the
    argument for it being a word at all. It is wider than ``members``, which
    asks only whether somebody is in the target; it is narrower than ``open``,
    which relaxes a public target for everybody including guests. Folding it
    into ``members`` instead would have widened, on upgrade, every deployment
    that had written the membership rule down.
    """
    assert HISTORY_POLICY_VALUES == (
        HISTORY_DISABLED,
        HISTORY_ORIGIN,
        HISTORY_MEMBERS,
        HISTORY_VISIBLE,
        HISTORY_OPEN,
    )
    assert HISTORY_POLICY_DEFAULT == HISTORY_DISABLED
    # Only the three widest let a request name a conversation other than its own.
    assert HISTORY_POLICY_NAMES_A_TARGET == {
        HISTORY_MEMBERS,
        HISTORY_VISIBLE,
        HISTORY_OPEN,
    }


def test_an_upgrade_leaves_a_deployment_on_the_word_it_wrote() -> None:
    """The constraint ``visible`` was shaped by, asserted rather than asserted of.

    A config saying ``members`` resolves to ``members`` and to nothing wider.
    The repair that would have been cheaper -- teaching ``members`` about
    public channels in place -- fails exactly here: it cannot be expressed as a
    resolution at all, because the written word and the enforced rule would
    have come apart.
    """
    assert resolve_history_policy({"history": "members"}) == HISTORY_MEMBERS
    assert resolve_history_policy({"history": "visible"}) == HISTORY_VISIBLE
    assert resolve_history_policy({}) == HISTORY_DISABLED
    # The legacy key is translated no further than it ever reached.
    assert resolve_history_policy({"history_digest_channel_ids": ["*"]}) == (
        HISTORY_ORIGIN
    )


# ── 2. The migration, all three rows ─────────────────────────────────────────


@pytest.mark.parametrize(
    ("written", "word", "loses_something"),
    [
        ([], HISTORY_DISABLED, False),
        (["*"], HISTORY_ORIGIN, False),
        (["C0A", "C0B"], HISTORY_DISABLED, True),
    ],
    ids=[
        "empty-is-disabled",
        "wildcard-is-origin",
        "a-list-is-disabled-and-says-so",
    ],
)
def test_the_legacy_allow_list_translates_row_by_row(
    written: list[str], word: str, loses_something: bool
) -> None:
    """``["*"]`` becomes ``origin`` and not ``members``.

    The legacy key never let a request name another conversation, so a
    translation to a word that does would widen a deployment on upgrade -- the
    one direction a migration must never take on its own.
    """
    resolved, note = history_policy_from_legacy_channel_ids(written)
    assert resolved == word
    assert bool(note) is loses_something
    if loses_something:
        # The note is the whole of what an operator can act on: which capability
        # went, and the one line that brings it back for every conversation.
        assert f"{KEY_HISTORY}: {HISTORY_ORIGIN}" in note


@pytest.mark.parametrize(
    ("written", "word"),
    [
        ({LEGACY_KEY_HISTORY_CHANNEL_IDS: []}, HISTORY_DISABLED),
        ({LEGACY_KEY_HISTORY_CHANNEL_IDS: ["*"]}, HISTORY_ORIGIN),
        ({LEGACY_KEY_HISTORY_CHANNEL_IDS: ["C0A", "C0B"]}, HISTORY_DISABLED),
    ],
    ids=["empty", "wildcard", "a-list"],
)
def test_the_resolver_reads_the_legacy_key_when_history_is_unset(
    written: dict[str, Any], word: str
) -> None:
    with _captured(policy_module.__name__) as records:
        assert resolve_history_policy(written) == word
    said = _warnings(records)
    assert len(said) == 1
    assert LEGACY_KEY_HISTORY_CHANNEL_IDS in said[0]
    assert f"{KEY_HISTORY}: {word}" in said[0]


def test_a_deprecation_is_said_once_however_often_it_is_resolved() -> None:
    """The cron path resolves on every run, and every run is not a new fact.

    A per-run deprecation line is how a warning worth reading gets filtered out
    of a log, so the resolver dedupes on what it translated.
    """
    written = {LEGACY_KEY_HISTORY_CHANNEL_IDS: ["*"]}
    with _captured(policy_module.__name__) as records:
        for _ in range(5):
            assert resolve_history_policy(written) == HISTORY_ORIGIN
    assert len(_warnings(records)) == 1


def test_changing_the_legacy_value_is_a_new_fact_and_is_said_again() -> None:
    with _captured(policy_module.__name__) as records:
        resolve_history_policy({LEGACY_KEY_HISTORY_CHANNEL_IDS: ["*"]})
        resolve_history_policy({LEGACY_KEY_HISTORY_CHANNEL_IDS: ["C0A"]})
    assert len(_warnings(records)) == 2


def test_history_wins_over_the_legacy_key_and_silences_it() -> None:
    """Both keys written is answered by the new one, and quietly.

    The same precedence ``acknowledge_mode`` takes over ``acknowledge_requests``:
    setting the new key is what stops the deprecation warning, so an operator
    who has migrated is not warned about the value they left behind for the
    upgrade's sake.
    """
    written = {
        KEY_HISTORY: HISTORY_MEMBERS,
        LEGACY_KEY_HISTORY_CHANNEL_IDS: ["C0A", "C0B"],
    }
    with _captured(policy_module.__name__) as records:
        assert resolve_history_policy(written) == HISTORY_MEMBERS
    assert _warnings(records) == []


# ── 3. Fail-closed resolution ────────────────────────────────────────────────


def test_a_misspelled_word_lands_on_the_default_and_skips_the_legacy_key() -> None:
    """A typo is a typo, not a request to fall back.

    Falling through would let ``histry: members`` -- or any other word that is
    not one of the four -- silently buy whatever the legacy list happened to
    say, which is a reading an operator would never guess from what they wrote.
    """
    written = {KEY_HISTORY: "shared", LEGACY_KEY_HISTORY_CHANNEL_IDS: ["*"]}
    with _captured(policy_module.__name__) as records:
        assert resolve_history_policy(written) == HISTORY_DISABLED
    said = _warnings(records)
    assert len(said) == 1
    assert "shared" in said[0]
    assert LEGACY_KEY_HISTORY_CHANNEL_IDS not in said[0]


@pytest.mark.parametrize(
    "written",
    [None, [], "channels.slack", 7],
    ids=["none", "a-list", "a-string", "a-number"],
)
def test_anything_that_is_not_a_config_block_reads_no_history(written: Any) -> None:
    assert resolve_history_policy(written) == HISTORY_DISABLED
    assert resolve_history_never_read(written) == ()
    assert resolve_history_exempt_members(written) == ()


def test_a_key_written_with_nothing_after_the_colon_is_unset() -> None:
    """``history:`` with no value is how the template ships a key it must keep.

    A value-less key parses as ``None``, which names none of the four words. It
    has to read as *unset* rather than as an error, or a config upgraded against
    the template would warn on a key the template itself wrote.
    """
    assert resolve_history_policy({KEY_HISTORY: None}) == HISTORY_DISABLED
    assert (
        resolve_history_policy(
            {KEY_HISTORY: None, LEGACY_KEY_HISTORY_CHANNEL_IDS: ["*"]}
        )
        == HISTORY_ORIGIN
    )


# ── 4. The two lists ─────────────────────────────────────────────────────────


def test_absent_and_empty_both_deny_nothing() -> None:
    """A deny-list's empty state has only one sensible reading.

    It is what dissolves the absent-versus-empty ambiguity a per-scope allow-
    list would have held: for a list of things that are *forbidden*, writing
    nothing and writing ``[]`` are the same statement.
    """
    assert resolve_history_never_read({}) == ()
    assert resolve_history_never_read({KEY_HISTORY_NEVER_READ: []}) == ()
    assert resolve_history_exempt_members({}) == ()
    assert resolve_history_exempt_members({KEY_HISTORY_EXEMPT_MEMBERS: []}) == ()


def test_the_lists_are_cleaned_deduplicated_and_keep_the_written_order() -> None:
    written = {
        KEY_HISTORY_NEVER_READ: ["  C0SECRET ", "", None, "C0SECRET", "C0OTHER"],
        KEY_HISTORY_EXEMPT_MEMBERS: ["B-PAGERDUTY"],
    }
    assert resolve_history_never_read(written) == ("C0SECRET", "C0OTHER")
    assert resolve_history_exempt_members(written) == ("B-PAGERDUTY",)


def test_a_bare_string_is_the_single_id_it_plainly_is() -> None:
    """Iterating a string would make a list of letters, silently.

    The corruption has no symptom an operator could read: a carve-out of
    ``C0SECRET`` would become a carve-out of eight one-letter conversation ids
    and would stop protecting anything.
    """
    assert resolve_history_never_read({KEY_HISTORY_NEVER_READ: "C0SECRET"}) == (
        "C0SECRET",
    )


# ── 5. What one request holds ──────────────────────────────────────────────


def test_all_three_keys_are_stamped_as_values_even_at_the_default() -> None:
    """An absent word and ``disabled`` are different answers and must stay so.

    Absent means *no side that has the configuration settled this request*,
    which the runtime reads as a refusal. ``disabled`` means a decision was
    taken and it said no. Only the second should ever be reachable from a path that has a
    Slack conversation in hand, and collapsing them would hide a metadata path
    nobody meant to exist.
    """
    stamped = history_policy_metadata(HISTORY_DISABLED)
    assert stamped == {
        METADATA_POLICY_KEY: HISTORY_DISABLED,
        METADATA_NEVER_READ_KEY: [],
        METADATA_EXEMPT_MEMBERS_KEY: [],
    }


def test_the_lists_travel_as_json_shaped_values() -> None:
    """Request metadata crosses a process boundary; a frozenset does not.

    Stamped rather than read from config on the far side, because the runtime
    must not read connector config: a deployment where the two disagreed would
    be a gate evaluated against a policy nobody wrote.
    """
    stamped = history_policy_metadata(
        HISTORY_OPEN,
        never_read=("C0SECRET",),
        exempt_members={"B-PAGERDUTY"},
    )
    assert stamped[METADATA_POLICY_KEY] == HISTORY_OPEN
    assert stamped[METADATA_NEVER_READ_KEY] == ["C0SECRET"]
    assert stamped[METADATA_EXEMPT_MEMBERS_KEY] == ["B-PAGERDUTY"]


def test_an_unknown_word_reaching_the_stamp_is_written_as_the_narrow_one() -> None:
    """The stamp is the last place a word can be wrong on the way out.

    Refusing here would fail a request that a config check should have caught;
    widening it would be the one direction that cannot be undone. It narrows.
    """
    assert history_policy_metadata("shared")[METADATA_POLICY_KEY] == HISTORY_DISABLED


# ── 6. The contract between the three processes ──────────────────────────────


def test_the_metadata_names_are_the_ones_the_design_settled() -> None:
    """Pinned as literals because they are the contract with a sibling change.

    A rename here is a wire break: the connector would stamp one name and the
    runtime read another, and the only symptom would be history quietly
    switching itself off.
    """
    assert METADATA_POLICY_KEY == "slack_history_policy"
    assert METADATA_NEVER_READ_KEY == "slack_history_never_read"
    assert METADATA_EXEMPT_MEMBERS_KEY == "slack_history_exempt_members"
    assert METADATA_ASKER_KEY == "slack_user_id"


# ── 7. Every honoured key ships in the template ──────────────────────────────


def _template_text(name: str) -> str:
    root = Path(__file__).resolve().parents[3]
    return (root / "jiuwenswarm" / "resources" / name).read_text()


def _template_slack_block(name: str) -> dict[str, Any]:
    return yaml.safe_load(_template_text(name))["channels"]["slack"]


@pytest.mark.parametrize(
    "name",
    [
        "config.yaml",
        "config.team.distributed.leader.yaml",
        "config.team.distributed.teammate.yaml",
    ],
)
def test_every_history_key_this_change_honours_is_in_the_shipped_template(
    name: str,
) -> None:
    """A key honoured but not shipped is deleted from the operator's file.

    The upgrade path merges the template over an operator's config, so a key the
    template does not name is dropped from theirs -- silently, and with nothing
    in the diff to see. That is exactly why the deprecated key is still here.
    """
    slack = _template_slack_block(name)
    for key in (KEY_HISTORY, KEY_HISTORY_NEVER_READ, KEY_HISTORY_EXEMPT_MEMBERS):
        assert key in slack, f"{name} does not ship channels.slack.{key}"
    assert LEGACY_KEY_HISTORY_CHANNEL_IDS in slack, (
        f"{name} dropped the deprecated key, which deletes it from an"
        " operator's config on upgrade"
    )


def test_the_template_ships_the_narrow_word_and_two_empty_lists() -> None:
    """An upgrade must not turn a capability on.

    The shipped ``history_digest_channel_ids`` was ``[]``, which read as no
    history; the word that replaces it has to read the same on a config nobody
    has edited.
    """
    slack = _template_slack_block("config.yaml")
    # Asserted as the word rather than as whatever the loader returned: a
    # template that stopped shipping a string here would be a silent False, or
    # a silent None, resolving to the default by accident rather than by intent.
    assert slack[KEY_HISTORY] == HISTORY_DISABLED
    assert slack[KEY_HISTORY_NEVER_READ] == []
    assert slack[KEY_HISTORY_EXEMPT_MEMBERS] == []
    assert slack[LEGACY_KEY_HISTORY_CHANNEL_IDS] == []
    assert resolve_history_policy(slack) == HISTORY_DISABLED


@pytest.mark.parametrize(
    "name",
    [
        "config.yaml",
        "config.team.distributed.leader.yaml",
        "config.team.distributed.teammate.yaml",
    ],
)
def test_every_word_survives_yaml_reading_it_unquoted(name: str) -> None:
    """The narrowest word is written bare in the template and stays a string.

    This is the whole reason it is ``disabled`` and not ``off``. YAML 1.1 --
    which is what ``safe_load`` implements -- resolves ``off``, ``no`` and
    ``n`` to the boolean false, so ``off`` was the one word of the four an
    operator could not write without quoting it, and the shipped templates had
    to quote it to stay honest. Nothing in YAML resolves ``disabled``, so the
    template writes it bare and an operator copying it writes it bare too.

    Asserted against the file as written rather than only against the parsed
    value: quoting is invisible once the loader has run, so a template that
    re-acquired quotes would go on passing a value assertion.
    """
    text = _template_text(name)
    assert f"\n    {KEY_HISTORY}: {HISTORY_DISABLED}\n" in text
    assert _template_slack_block(name)[KEY_HISTORY] == HISTORY_DISABLED
    for word in HISTORY_POLICY_VALUES:
        assert yaml.safe_load(f"{KEY_HISTORY}: {word}") == {KEY_HISTORY: word}


def _comment_block_above(name: str, key: str) -> str:
    """The run of comment lines immediately above ``key`` in one template.

    Where an operator reads the vocabulary. The three templates write it in two
    shapes -- ``config.yaml`` enumerates a word per line, the two distributed
    ones fit it on one -- so this hands back the block and the caller asks what
    is in it rather than how it is laid out.
    """
    lines = _template_text(name).splitlines()
    start = next(i for i, line in enumerate(lines) if line.rstrip() == "  slack:")
    at = next(
        i
        for i, line in enumerate(lines[start:], start)
        if re.match(rf"^\s*{key}:", line)
    )
    block: list[str] = []
    for line in reversed(lines[:at]):
        if not line.lstrip().startswith("#"):
            break
        block.append(line)
    assert block, f"{name} documents nothing above channels.slack.{key}"
    return "\n".join(reversed(block))


@pytest.mark.parametrize(
    "name",
    [
        "config.yaml",
        "config.team.distributed.leader.yaml",
        "config.team.distributed.teammate.yaml",
    ],
)
def test_every_template_documents_every_word(name: str) -> None:
    """A word the shipped comment does not name is a word nobody can write.

    The vocabulary is not a key, so an operator never gets a warning about the
    one they could not find: they read the ladder where their own config file
    prints it, and write one of the words it lists. A template that lists four
    of five hides the fifth from every deployment that reads that file, and
    ``visible`` was added to ``config.yaml`` alone while the two distributed
    templates went on printing the four-word ladder.

    Asserted against the comment rather than against the value, because the
    value is ``disabled`` in all three and says nothing about what else may be
    written there.
    """
    block = _comment_block_above(name, KEY_HISTORY)
    for word in HISTORY_POLICY_VALUES:
        assert re.search(rf"\b{word}\b", block), (
            f"{name} does not name {word!r} where it documents"
            f" channels.slack.{KEY_HISTORY}"
        )


@pytest.mark.parametrize("written", [True, False], ids=["true", "false"])
def test_a_boolean_is_a_mistake_and_not_a_word(written: bool) -> None:
    """Neither boolean is read as a word, and the false one is the change.

    While the narrowest word was ``off`` the resolver read a bare ``False`` as
    it, because an operator writing the documented word by hand would not quote
    it and would otherwise hand us a type rather than a word. ``disabled``
    cannot be misparsed, so that compensation no longer earns its place: both
    booleans now fail as what they are, which is an unrecognised value, and the
    warning names the four words the operator can write instead.

    It still lands on ``disabled`` -- but by the fail-closed default rather than
    by a reading, and with a warning rather than in silence.
    """
    with _captured(policy_module.__name__) as records:
        assert resolve_history_policy({KEY_HISTORY: written}) == HISTORY_DISABLED
    said = _warnings(records)
    assert len(said) == 1
    assert "/".join(HISTORY_POLICY_VALUES) in said[0]



# ── 8. What the connector settles for one request ────────────────────────────


def _slack_config(**overrides: Any) -> Any:
    from jiuwenswarm.gateway.channel_manager.im_platforms.slack.slack_connect import (
        SlackChannelConfig,
    )

    return SlackChannelConfig(enabled=True, **overrides)


def test_layer_zero_answers_when_no_scope_speaks() -> None:
    from jiuwenswarm.gateway.channel_manager.im_platforms.slack.slack_connect import (
        slack_history_request_metadata,
    )

    stamped = slack_history_request_metadata(
        _slack_config(
            history=HISTORY_MEMBERS,
            history_never_read=("C0SECRET",),
            history_exempt_members=("B-PAGERDUTY",),
        ),
        "C-ANY",
    )
    assert stamped[METADATA_POLICY_KEY] == HISTORY_MEMBERS
    assert stamped[METADATA_NEVER_READ_KEY] == ["C0SECRET"]
    assert stamped[METADATA_EXEMPT_MEMBERS_KEY] == ["B-PAGERDUTY"]


def test_a_scope_narrows_one_conversation_and_leaves_the_rest() -> None:
    """The word is per conversation; the two lists are per workspace.

    Neither list belongs in a scope: if a channel must never travel that is true
    whichever room asks, and writing it per scope means repeating it in every
    rule and forgetting it in one.
    """
    from jiuwenswarm.gateway.channel_manager.im_platforms.slack.slack_connect import (
        SlackChannelOverride,
        slack_history_request_metadata,
    )

    config = _slack_config(
        history=HISTORY_OPEN,
        history_never_read=("C0SECRET",),
        conversation_overrides={"C-QUIET": SlackChannelOverride(history=HISTORY_DISABLED)},
    )
    assert (
        slack_history_request_metadata(config, "C-QUIET")[METADATA_POLICY_KEY]
        == HISTORY_DISABLED
    )
    assert (
        slack_history_request_metadata(config, "C-OTHER")[METADATA_POLICY_KEY]
        == HISTORY_OPEN
    )
    # The carve-out is not a scope key and reaches both.
    for channel_id in ("C-QUIET", "C-OTHER"):
        stamped = slack_history_request_metadata(config, channel_id)
        assert stamped[METADATA_NEVER_READ_KEY] == ["C0SECRET"]


def test_an_unvetted_scope_word_is_dropped_rather_than_carried() -> None:
    """A read gate re-checks what a delivery key would take as settled.

    A word the scope loader has not vetted -- because the capability
    declaration that vets it is a separate change, or because something other
    than the loader built the mapping -- must land on the narrow side. Dropped
    to unset, so layer 0 answers rather than the unknown word.
    """
    from jiuwenswarm.gateway.channel_manager.im_platforms.slack.slack_connect import (
        sections_as_override,
    )

    assert sections_as_override({"agent": {KEY_HISTORY: "shared"}}).history is None
    assert (
        sections_as_override({"agent": {KEY_HISTORY: HISTORY_OPEN}}).history
        == HISTORY_OPEN
    )
