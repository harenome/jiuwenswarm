# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""The one test that reads both Slack tool cards at once.

Each tool's own suite asserts that its card states the three shared rules, and
that is not enough on its own: while the two cards held separate copies of
the wording, editing one card and the copy beside it left the other card's
suite passing against its own untouched copy, and the shared wording split with
nothing failing. Retiring the copies for
``slack_card_rules.CANONICAL_CARD_SENTENCES`` removes the way that happened,
and this file removes the way it could happen again: it is the only place where
a change to one card is checked against the other card rather than against a
constant.

It is also where the limit of the sharing is pinned. A rule may be stated in
both cards only while it is about the tool whose card it is on; the two tools
are switched on independently, so a card that explained itself by reference to
its sibling would, on a deployment running one of them, describe a tool the
model has not been given. Neither card may claim the other exists.
"""

from __future__ import annotations

from jiuwenswarm.agents.harness.common.tools.slack_history import SlackHistoryToolkit
from jiuwenswarm.agents.harness.common.tools.slack_search import SlackSearchToolkit

from tests.unit_tests.channel.slack_card_rules import CANONICAL_CARD_SENTENCES


def _history_card_description() -> str:
    """The ``read_slack_conversation`` card, built without touching Slack.

    The toolkit's client is optional and no request is made to render a card,
    so the metadata is only what the toolkit needs to bind to a conversation.
    """
    toolkit = SlackHistoryToolkit(metadata={"slack_channel_id": "C1"})
    return str(toolkit.get_tools()[0]._card.description)


def _search_card_description() -> str:
    """The workspace-search card, built the same way."""
    (tool,) = SlackSearchToolkit().get_tools()
    return str(tool.card.description)


def test_both_cards_state_every_shared_rule() -> None:
    """Neither card may drop a rule the other one states.

    A rule that survives in one card and vanishes from the other is the failure
    this file exists for: the model still reads the rule on one tool and not on
    the other, which is exactly the inconsistency the shared wording was
    introduced to remove.
    """
    history = _history_card_description()
    search = _search_card_description()
    for sentence in CANONICAL_CARD_SENTENCES:
        assert sentence in history, f"missing from the history card: {sentence}"
        assert sentence in search, f"missing from the search card: {sentence}"


def test_the_shared_rules_are_stated_in_one_wording() -> None:
    """The two cards state each rule with the same bytes, not merely the same rule.

    Asserting containment separately in each card would pass on two wordings
    that both happened to contain the constant. What is pinned here is that the
    text lifted out of each card around the shared sentence is the sentence
    itself -- the same substring, character for character, in both.
    """
    history = _history_card_description()
    search = _search_card_description()
    for sentence in CANONICAL_CARD_SENTENCES:
        start_h = history.index(sentence)
        start_s = search.index(sentence)
        assert history[start_h : start_h + len(sentence)] == (
            search[start_s : start_s + len(sentence)]
        ), sentence


def test_the_two_cards_are_not_the_same_card() -> None:
    """The shared wording is a shared subset, never the whole description.

    A guard on the guard: were both helpers above to return one card by
    accident -- a copied import, a renamed toolkit resolving to the same class
    -- every assertion in this file would pass while checking nothing.
    """
    assert _history_card_description() != _search_card_description()


def test_neither_card_asserts_that_the_other_tool_exists() -> None:
    """A card is read by a model that may hold only that card.

    ``search_enabled`` ships as the default and the history tool's own default
    is disabled, so the single-card turn is the shipped case rather than an
    edge one. A sentence that reaches across -- "the two Slack tools", the
    other tool's paging direction, its name used as a comparison -- then names
    something the model has no way to check for and cannot use, and the rule it
    holds arrives attached to a tool that is not there.

    Search naming ``read_slack_conversation`` as the way to turn a result into
    content is deliberately not caught here. That is this tool's own output
    telling the caller what to do with it, and a reader without the history
    tool learns the results are references rather than being told about a
    sibling's paging. What is refused is the comparison, which is why the
    history card may not name the search tool at all: it has no such step to
    describe.
    """
    history = _history_card_description()
    search = _search_card_description()

    for description in (history, search):
        assert "The two Slack tools" not in description

    # Each card's paging sentence stays on its own card, in its own terms.
    history_paging = "Paging walks backwards in time"
    search_paging = "Paging walks down the relevance ranking"
    assert history_paging in history
    assert history_paging not in search
    assert search_paging in search
    assert search_paging not in history

    # The history card has no read step to name, so the search tool has no
    # business appearing in it at all.
    assert "search_slack_workspace" not in history
