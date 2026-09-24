# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The one word that says how far a Slack turn may post, and how it is settled.

Four words with the reading ladder's shape and the opposite rule. The tests here
cover the resolver -- which word a written config settles on -- and the contract
with the two processes that read the answer: the name it travels under on
request metadata, and the fact that the connector and the cron path agree about
it.

The gate the word *governs* is in ``test_slack_post_message.py``. This file is
about how the word is arrived at and held, which is a separate failure: a
correct gate applied to a policy nobody wrote is not a safe deployment.

One property is asserted here and nowhere else, because it is the reason the key
exists at all: ``write`` and ``history`` are two keys, and the widest word means
opposite things under them.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

import pytest
import yaml

from jiuwenswarm.common.slack_history_policy import KEY_HISTORY
from jiuwenswarm.common.slack_write_policy import (
    KEY_WRITE,
    METADATA_WRITE_POLICY_KEY,
    WRITE_DISABLED,
    WRITE_MEMBERS,
    WRITE_OPEN,
    WRITE_ORIGIN,
    WRITE_POLICY_CONFIRMS_WIDENING,
    WRITE_POLICY_DEFAULT,
    WRITE_POLICY_NAMES_A_TARGET,
    WRITE_POLICY_VALUES,
    normalize_write_policy,
    resolve_write_policy,
    write_policy_at_least,
    write_policy_metadata,
)

_TEMPLATES = (
    "config.yaml",
    "config.team.distributed.leader.yaml",
    "config.team.distributed.teammate.yaml",
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


def _template_text(name: str) -> str:
    root = Path(__file__).resolve().parents[3]
    return (root / "jiuwenswarm" / "resources" / name).read_text()


def _template_slack_block(name: str) -> dict[str, Any]:
    return yaml.safe_load(_template_text(name))["channels"]["slack"]


# ── 1. The vocabulary ────────────────────────────────────────────────────────


def test_the_four_words_are_ordered_and_the_default_is_the_narrowest() -> None:
    """The order is the design's claim about them and is asserted, not assumed."""
    assert WRITE_POLICY_VALUES == (
        WRITE_DISABLED,
        WRITE_ORIGIN,
        WRITE_MEMBERS,
        WRITE_OPEN,
    )
    assert WRITE_POLICY_DEFAULT == WRITE_DISABLED
    # Only the two widest let a request name a conversation other than its own.
    assert WRITE_POLICY_NAMES_A_TARGET == {WRITE_MEMBERS, WRITE_OPEN}
    # And only the narrower of those two confirms a widening target. ``open``
    # naming a conversation and asking about it would be a word that never
    # means what it says.
    assert WRITE_POLICY_CONFIRMS_WIDENING == {WRITE_MEMBERS}


def test_write_is_a_key_of_its_own_and_not_a_reading_of_history() -> None:
    """The two ladders share their four words and share no key.

    The reason is the widest word. Under ``history`` a public target relaxes the
    membership rule, because membership of a public channel is self-serve and
    the content was already reachable by anybody who cared to join. Under
    ``write`` a public target is the widest audience in the workspace and the
    case an operator is least likely to want reached unasked. One key settling
    both would have to pick a direction and be wrong about the other one.

    Asserted as two distinct key names, which is the whole of what the code can
    check: the argument lives in the module docstring, and this is the line that
    fails if somebody folds one key into the other.
    """
    assert KEY_WRITE != KEY_HISTORY
    written = {KEY_HISTORY: WRITE_OPEN}
    # A config that widened reads has said nothing at all about writes.
    assert resolve_write_policy(written) == WRITE_DISABLED


@pytest.mark.parametrize("word", WRITE_POLICY_VALUES)
def test_at_least_is_an_order_comparison_over_the_four(word: str) -> None:
    index = WRITE_POLICY_VALUES.index(word)
    for other in WRITE_POLICY_VALUES:
        expected = WRITE_POLICY_VALUES.index(other) <= index
        assert write_policy_at_least(word, other) is expected


@pytest.mark.parametrize("written", ["", "  ", None, "louder", True, False, 3])
def test_at_least_fails_closed_for_anything_that_is_not_a_word(
    written: Any,
) -> None:
    """A request carrying a non-word is not thereby at least the narrowest word."""
    for word in WRITE_POLICY_VALUES:
        assert write_policy_at_least(written, word) is False


# ── 2. The resolver ──────────────────────────────────────────────────────────


@pytest.mark.parametrize("word", WRITE_POLICY_VALUES)
def test_every_word_is_honoured_as_written(word: str) -> None:
    assert resolve_write_policy({KEY_WRITE: word}) == word


@pytest.mark.parametrize(
    "written", ["Members", " OPEN ", "Origin"], ids=["case", "space", "mixed"]
)
def test_a_word_is_read_without_regard_to_case_or_surrounding_space(
    written: str,
) -> None:
    assert resolve_write_policy({KEY_WRITE: written}) == written.strip().lower()


def test_a_misspelled_word_lands_on_the_default_and_says_so() -> None:
    """A typo buys the narrowest word rather than something wider.

    The alternative -- reading an unrecognised value as "nothing was set here"
    and falling through -- is the same behaviour as an absent key today, and
    would stop being so the moment anything sat below this key. A misspelling is
    a line the operator meant that nothing can honour, and it is worth a warning
    naming the four words that would have worked.
    """
    with _captured("jiuwenswarm.common.slack_write_policy") as records:
        assert resolve_write_policy({KEY_WRITE: "everyone"}) == WRITE_DISABLED
    said = " ".join(_warnings(records))
    assert "everyone" in said
    for word in WRITE_POLICY_VALUES:
        assert word in said


@pytest.mark.parametrize("written", [True, False], ids=["true", "false"])
def test_a_boolean_is_a_mistake_and_not_a_word(written: bool) -> None:
    """Neither boolean is read as a word.

    This is why the narrowest word is ``disabled`` and not ``off``: YAML 1.1 --
    which is what ``safe_load`` implements -- resolves a bare ``off`` to the
    boolean false, so ``off`` would be the one word of the four an operator
    could not write unquoted. Both booleans fail here as what they are.
    """
    with _captured("jiuwenswarm.common.slack_write_policy") as records:
        assert resolve_write_policy({KEY_WRITE: written}) == WRITE_DISABLED
    assert _warnings(records)


@pytest.mark.parametrize("written", [None, "", "   "], ids=["absent", "empty", "space"])
def test_a_key_written_with_nothing_after_the_colon_is_unset(written: Any) -> None:
    """Not an error and not a word: it is how a template ships a key quietly.

    No warning either, which is the difference from a misspelling. A key that
    the shipped template carries with no value is the normal state of a config
    nobody has edited, and warning about it would warn every deployment.
    """
    assert normalize_write_policy(written) is None
    with _captured("jiuwenswarm.common.slack_write_policy") as records:
        assert resolve_write_policy({KEY_WRITE: written}) == WRITE_DISABLED
    assert not _warnings(records)


@pytest.mark.parametrize("written", [None, "", [], 7, "slack"], ids=lambda v: repr(v))
def test_anything_that_is_not_a_config_block_writes_nothing(written: Any) -> None:
    assert resolve_write_policy(written) == WRITE_DISABLED


def test_an_empty_block_writes_nothing() -> None:
    """A deployment that configured Slack and said nothing about writing.

    The default has to be the narrowest word rather than ``origin``, because
    every Slack deployment in existence before this key was added is exactly
    this shape, and an upgrade must not hand a bot the ability to post on a
    model's instruction.
    """
    assert resolve_write_policy({}) == WRITE_DISABLED


# ── 3. What travels on a request ─────────────────────────────────────────────


@pytest.mark.parametrize("word", WRITE_POLICY_VALUES)
def test_the_word_is_stamped_as_a_value_even_at_the_default(word: str) -> None:
    """Present always, so that absent can mean something else.

    An absent key means *no Slack connector settled this request*, which the
    runtime reads as a refusal. A connector that settled it to ``disabled`` is a
    different fact, and a stamp that omitted the default would make the two
    indistinguishable on the wire.
    """
    assert write_policy_metadata(word) == {METADATA_WRITE_POLICY_KEY: word}


@pytest.mark.parametrize("written", [None, "", "shout", True, 2])
def test_an_unknown_word_reaching_the_stamp_is_written_as_the_narrow_one(
    written: Any,
) -> None:
    assert write_policy_metadata(written) == {
        METADATA_WRITE_POLICY_KEY: WRITE_DISABLED
    }


def test_the_metadata_name_is_the_one_the_design_settled() -> None:
    """Pinned as a literal, because it crosses a process boundary as JSON.

    The connector stamps it, the cron path stamps it, and the runtime toolkit
    reads it. None of the three imports the others' modules, so the only thing
    holding them together is this string; renaming it in one place would mount
    no tool and log nothing.
    """
    assert METADATA_WRITE_POLICY_KEY == "slack_write_policy"
    assert KEY_WRITE == "write"


# ── 4. The shipped templates ─────────────────────────────────────────────────


@pytest.mark.parametrize("name", _TEMPLATES)
def test_every_template_ships_the_key_at_the_narrow_word(name: str) -> None:
    """A key honoured but not shipped is deleted from the operator's file.

    The upgrade path merges the template over an operator's config, so a key the
    template does not name is dropped from theirs -- silently, and with nothing
    in the diff to see.
    """
    slack = _template_slack_block(name)
    assert KEY_WRITE in slack, f"{name} does not ship channels.slack.{KEY_WRITE}"
    # Asserted as the word rather than as whatever the loader returned: a
    # template that stopped shipping a string here would be a silent False, or a
    # silent None, resolving to the default by accident rather than by intent.
    assert slack[KEY_WRITE] == WRITE_DISABLED
    assert resolve_write_policy(slack) == WRITE_DISABLED


@pytest.mark.parametrize("name", _TEMPLATES)
def test_the_word_is_written_bare_and_survives_yaml_reading_it(name: str) -> None:
    """Asserted against the file as written, not only against the parsed value.

    Quoting is invisible once the loader has run, so a template that re-acquired
    quotes would go on passing a value assertion while telling an operator to
    write something they then have to quote.
    """
    assert f"\n    {KEY_WRITE}: {WRITE_DISABLED}\n" in _template_text(name)
    for word in WRITE_POLICY_VALUES:
        assert yaml.safe_load(f"{KEY_WRITE}: {word}") == {KEY_WRITE: word}
