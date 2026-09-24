# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.

"""Every word the shipped template documents must reach its reader as itself.

``jiuwenswarm/resources/config.yaml`` is loaded with ``yaml.safe_load``, which is
YAML 1.1: a bare ``off`` is the boolean ``False``, and so are ``no`` and ``~``,
while ``on``, ``yes`` and ``true`` are ``True``. A key whose vocabulary holds one
of those words and whose reader treats the value as a string therefore answers a
config line with something other than what it says -- and the operator cannot
see it, because the word they wrote is the word they meant.

``channels.slack.acknowledge_mode: off`` resolved to ``reaction``, and
``channels.slack.group_chat_mode: off`` to ``mention``: each the opposite of the
line that produced it, with nothing in the log. These tests are the ones that
were missing. They write each documented word into YAML exactly as the template
documents it, parse it with the parser the runtime uses, and require the reader
to answer with the word.

The vocabularies are read out of the template rather than restated here. The
template is the thing that was wrong, so the template is the thing under test: a
word added to a documented list is covered the moment it is documented, and a
template that comes to list a word its reader does not accept fails here rather
than in somebody's deployment.
"""

from __future__ import annotations

import re
import textwrap
from pathlib import Path

import pytest
import yaml

from jiuwenswarm.common.reasoning_config import (
    SUPPORTED_REASONING_LEVELS,
    normalize_reasoning_level,
)
from jiuwenswarm.common.slack_blocks import RENDER_TABLES_MODES
from jiuwenswarm.common.slack_events_policy import (
    EVENT_DISPOSITIONS,
    EVENT_FAMILIES,
    event_disposition,
    normalize_event_policy,
)
from jiuwenswarm.gateway.channel_manager.im_platforms.slack.scope_capabilities import (
    _check_events,
)
from jiuwenswarm.gateway.channel_manager.im_platforms.slack.slack_connect import (
    ACKNOWLEDGE_MODES,
    BLOCKKIT_TABLES_MODES,
    BLOCKKIT_VALIDATE_MODES,
    GROUP_CHAT_MODES,
    _slack_layer0_triggers,
    resolve_acknowledge_mode,
    resolve_blockkit_tables_mode,
    resolve_blockkit_validate,
    resolve_group_chat_mode,
    resolve_render_tables,
)
#: The words a YAML 1.1 loader resolves to something that is not a string. The
#: list the audit ran against, spelled here so a failure below names what it is
#: looking for rather than a regex nobody can read.
YAML_BOOLEAN_WORDS = frozenset(
    {"off", "on", "yes", "no", "true", "false", "null", "~"}
)

TEMPLATE = (
    Path(__file__).resolve().parents[2]
    / "jiuwenswarm"
    / "resources"
    / "config.yaml"
)

#: One enumerated value in a template comment block. The enumeration indent is
#: ``#`` plus exactly three spaces the whole way through the file, and a line
#: continuing one is indented past the word it continues, so the column is what
#: separates a value from prose about it.
_VOCABULARY_LINE = re.compile(r"^\s*#   ([a-z_]+)\s+-{1,2}\s")


def _template_lines() -> list[str]:
    return TEMPLATE.read_text(encoding="utf-8").splitlines()


def _key_line(key: str, *, under: str = "") -> int:
    """Where ``key`` is written, optionally below the section line ``under``.

    ``group_chat_mode`` is written twice -- once for Telegram and once for
    Slack -- so a key alone does not always name a place in this file.
    """
    lines = _template_lines()
    start = 0
    if under:
        start = next(i for i, line in enumerate(lines) if line.rstrip() == under)
    at = next(
        (
            i
            for i, line in enumerate(lines[start:], start)
            if re.match(rf"^\s*{key}:", line)
        ),
        None,
    )
    assert at is not None, f"{key} is not in {TEMPLATE.name} under {under!r}"
    return at


def _documented_words(key: str, *, under: str = "") -> tuple[str, ...]:
    """The vocabulary the template lists in the comment block above ``key``.

    The block is the run of comment lines immediately above the key, which is
    how every documented vocabulary in this file is written.
    """
    lines = _template_lines()
    words: list[str] = []
    for line in reversed(lines[: _key_line(key, under=under)]):
        if not line.lstrip().startswith("#"):
            break
        found = _VOCABULARY_LINE.match(line)
        if found:
            words.append(found.group(1))
    assert words, f"no vocabulary is documented above {key}"
    return tuple(reversed(words))


def _as_written(key: str, word: str):
    """``word`` written under ``key`` the way the template documents it: bare.

    Round-tripped through the loader the runtime uses, so the reader is handed
    what a config file holding that line would hand it.
    """
    return yaml.safe_load(f"{key}: {word}\n")[key]


#: Each documented Slack key, its vocabulary in the code, the reader that
#: answers it, and what a bare ``true`` names there. The reader takes whatever
#: the loader produced and must return the word, so one line covers a key.
#:
#: The last column is not always the default. Four of these keys answer a true
#: with it, because a true names no single word among three that are all "not
#: off"; ``blockkit_validate`` answers with its widest setting, on the argument
#: written at ``resolve_blockkit_validate`` that spending more on diagnosis is
#: what somebody writing a boolean into a key that takes words asked for.
_SLACK_KEYS = (
    (
        "acknowledge_mode",
        ACKNOWLEDGE_MODES,
        lambda value: resolve_acknowledge_mode({"acknowledge_mode": value}),
        "reaction",
    ),
    (
        "group_chat_mode",
        GROUP_CHAT_MODES,
        resolve_group_chat_mode,
        "mention",
    ),
    (
        "blockkit_tables",
        BLOCKKIT_TABLES_MODES,
        lambda value: resolve_blockkit_tables_mode({"blockkit_tables": value}),
        "auto",
    ),
    (
        "render_tables",
        RENDER_TABLES_MODES,
        lambda value: resolve_render_tables({"render_tables": value}),
        "data_table",
    ),
    (
        "blockkit_validate",
        BLOCKKIT_VALIDATE_MODES,
        lambda value: resolve_blockkit_validate({"blockkit_validate": value}),
        "all",
    ),
)

_SLACK_IDS = tuple(entry[0] for entry in _SLACK_KEYS)


@pytest.mark.parametrize("key, vocabulary, reader, bare_true", _SLACK_KEYS, ids=_SLACK_IDS)
def test_template_documents_the_vocabulary_the_code_accepts(
    key, vocabulary, reader, bare_true
):
    """The words in the template and the words in the code are one list."""
    assert sorted(_documented_words(key, under="  slack:")) == sorted(vocabulary)


@pytest.mark.parametrize("key, vocabulary, reader, bare_true", _SLACK_KEYS, ids=_SLACK_IDS)
def test_every_documented_word_reaches_its_reader_as_itself(
    key, vocabulary, reader, bare_true
):
    """Bare, quoted and upper case all name the same mode."""
    for word in _documented_words(key, under="  slack:"):
        assert reader(_as_written(key, word)) == word, f"{key}: {word}"
        assert reader(word) == word, f"{key}: '{word}'"
        assert reader(word.upper()) == word, f"{key}: {word.upper()}"


@pytest.mark.parametrize("key, vocabulary, reader, bare_true", _SLACK_KEYS, ids=_SLACK_IDS)
def test_a_bare_true_names_no_word_and_is_not_guessed_at(
    key, vocabulary, reader, bare_true
):
    """``on``, ``yes`` and ``true`` are not vocabulary anywhere here.

    None of these keys has a word for a true, so each answers with the one word
    it has decided a true means. Three spellings, one answer: a reader that
    settled ``on`` and not ``true`` would still be reading the page rather than
    what the loader handed it.
    """
    for word in ("on", "yes", "true"):
        assert reader(_as_written(key, word)) == bare_true, f"{key}: {word}"


def test_group_chat_mode_off_silences_the_mentions_it_names():
    """The word has to reach the triggers, not only the resolver.

    ``group_chat_mode`` is the key whose value the connector never uses as a
    word: what it wants is a set of triggers. A bare ``off`` fell through to
    ``mention``'s set and left the bot answering the mentions that line was
    written to silence, which no log line reported.
    """
    written = _as_written("group_chat_mode", "off")
    assert _slack_layer0_triggers(written) == frozenset()
    assert _slack_layer0_triggers(written) == _slack_layer0_triggers("off")
    for word in _documented_words("group_chat_mode", under="  slack:"):
        assert _slack_layer0_triggers(
            _as_written("group_chat_mode", word)
        ) == _slack_layer0_triggers(word), word


def test_event_dispositions_reach_the_loader_as_written():
    """``delivery.events`` takes its three words through the same loader.

    All three readers the events module names -- the scope loader's check, the
    normaliser and the read gate -- have to answer a bare word with that word,
    or they disagree about a mapping nobody wrote wrongly.
    """
    for word in EVENT_DISPOSITIONS:
        written = yaml.safe_load(f"reaction: {word}\n")
        assert _check_events(written) is None, f"reaction: {word}"
        assert normalize_event_policy(written) == {"reaction": word}
        assert (
            event_disposition(normalize_event_policy(written), "reaction") == word
        )


def test_one_bare_off_does_not_refuse_the_families_beside_it():
    """The refusal is per mapping, so an unreadable word costs its neighbours.

    This is what the template's own worked example ran into: ``member: off``
    reached the validator as ``False``, the whole mapping was refused, and the
    ``reaction: turn`` written above it went with it.
    """
    written = yaml.safe_load("reaction: turn\nmember: off\n")
    assert written == {"reaction": "turn", "member": False}
    assert _check_events(written) is None
    assert normalize_event_policy(written) == {"reaction": "turn", "member": "off"}


def test_the_templates_worked_events_example_is_accepted():
    """The mapping printed in the template loads, validates and normalises.

    Read out of the template rather than retyped: the example is what an
    operator copies, so the example is what has to work.
    """
    lines = _template_lines()
    at = next(
        i
        for i, line in enumerate(lines)
        if line.lstrip("#").strip() == "events:" and line.lstrip().startswith("#")
    )
    block: list[str] = []
    for line in lines[at:]:
        body = line.lstrip()
        if not body.startswith("#"):
            break
        body = body[1:]
        if not body.strip():
            break
        block.append(body)
    example = yaml.safe_load(textwrap.dedent("\n".join(block)))["events"]
    assert set(example) == set(EVENT_FAMILIES)
    assert _check_events(example) is None
    settled = normalize_event_policy(example)
    assert set(settled) == set(EVENT_FAMILIES)
    for family in example:
        assert event_disposition(settled, family) == settled[family]
    assert settled["member"] == "off"


def test_reasoning_level_reads_its_two_boolean_words():
    """``reasoning_level`` is the vocabulary with a word at either end.

    It is not in the shipped template, but a live config holds it, and it is the
    only vocabulary in this audit whose ``off`` has an ``on`` beside it: a YAML
    1.1 loader turns one into ``False`` and the other into ``True``, and both
    have to come back as the word that was written.
    """
    for word in SUPPORTED_REASONING_LEVELS:
        assert (
            normalize_reasoning_level(_as_written("reasoning_level", word)) == word
        ), word


def test_the_audit_is_still_the_whole_audit():
    """Which documented words are YAML booleans, named rather than derived.

    The parametrised tests above are what catches a new trapped word: every word
    a key documents has to survive the round trip whether or not it is on this
    list. This one records what the audit found, so a sixth key gaining an
    ``off`` -- or one of these losing it -- is a deliberate edit rather than a
    silent change to what the suite is checking.
    """
    trapped = {
        key: [
            word
            for word in _documented_words(key, under="  slack:")
            if word in YAML_BOOLEAN_WORDS
        ]
        for key in _SLACK_IDS
    }
    assert trapped == {
        "acknowledge_mode": ["off"],
        "group_chat_mode": ["off"],
        "blockkit_tables": ["off"],
        "render_tables": ["off"],
        "blockkit_validate": ["off"],
    }
    assert [
        word for word in EVENT_DISPOSITIONS if word in YAML_BOOLEAN_WORDS
    ] == ["off"]
    assert [
        word for word in SUPPORTED_REASONING_LEVELS if word in YAML_BOOLEAN_WORDS
    ] == ["off", "on"]
