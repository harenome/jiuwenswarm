# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The questions, the state and the rule that reads the answers.

Every number quoted below was measured against the labelled transcript and is
recorded in the design note. The tests pin the shapes and the clauses rather
than the numbers: a threshold is re-fittable from the shadow log, and a rule
whose clause order changed would be a different rule.
"""

from __future__ import annotations

import pytest

from jiuwenswarm.common.typed_decision import Answer, Question
from jiuwenswarm.gateway.channel_manager.im_platforms.slack import (
    decision_questions as dq,
)


def noul(value: float) -> Answer:
    return Answer(type="noul", value=value)


def audience(choice: str) -> Answer:
    return Answer(type="choice", value=choice, confidence=0.8)


def answers(**overrides: Answer) -> dict[str, Answer]:
    base = {
        dq.AUDIENCE: audience(dq.AUDIENCE_NOBODY),
        dq.ALREADY_ANSWERED: noul(0.05),
        dq.RESPONSE_REQUESTED: noul(0.8),
        dq.CLAIM: noul(0.10),
        dq.NEEDS_WORK: noul(0.05),
        dq.WORTH_REMEMBERING: noul(0.01),
        dq.DESERVES_EMOJI: noul(0.02),
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# The questions
# ---------------------------------------------------------------------------


def test_every_question_is_valid_and_states_its_meaning_in_the_wording() -> None:
    """A question id never reaches the model, so none may lean on its own name."""
    for question_id, question in dq.QUESTIONS.items():
        assert isinstance(question, Question)
        assert question_id.replace("_", " ") not in question.instructions.lower()
        assert len(question.instructions) > 30


def test_audience_is_a_choice_with_every_option_described() -> None:
    """Naming is not addressing, and a noul over that scores the ambiguity."""
    question = dq.QUESTIONS[dq.AUDIENCE]
    assert question.type == "choice"
    assert set(question.criteria) == {
        dq.AUDIENCE_NOBODY,
        dq.AUDIENCE_ROOM,
        dq.AUDIENCE_PERSON,
        dq.AUDIENCE_BOT,
    }
    for description in question.criteria.values():
        assert len(description) > 40


def test_the_rest_are_nouls_and_take_no_criteria() -> None:
    for question_id, question in dq.QUESTIONS.items():
        if question_id == dq.AUDIENCE:
            continue
        assert question.type == "noul"
        assert question.criteria is None


def test_the_emoji_choice_is_not_asked() -> None:
    """Its criteria are one workspace's emoji list and no key holds one.

    Shadow sends no reaction either, so there is nothing an answer would feed.
    """
    assert "emoji" not in dq.QUESTIONS
    assert dq.DESERVES_EMOJI in dq.QUESTIONS


def test_no_question_names_a_kind_of_message() -> None:
    """A wording that names its subject scores the naming.

    A ``deserves_emoji`` wording listing good news, finished work, a surprising
    result and a joke produced 5.5% where the neutral wording produced 0.8%.
    """
    baited = ("good news", "joke", "congratul", "surprising", "corpus", "paper")
    lowered = dq.QUESTIONS[dq.DESERVES_EMOJI].instructions.lower()
    assert not any(word in lowered for word in baited)


# ---------------------------------------------------------------------------
# The rule
# ---------------------------------------------------------------------------


def test_a_request_aimed_at_one_person_is_recorded_and_nothing_else() -> None:
    assert dq.disposition(answers(audience=audience(dq.AUDIENCE_PERSON))) == (
        dq.DISPOSITION_CONTEXT
    )


def test_an_answered_question_is_recorded_and_nothing_else() -> None:
    assert dq.disposition(
        answers(already_answered=noul(0.9), audience=audience(dq.AUDIENCE_ROOM))
    ) == dq.DISPOSITION_CONTEXT


@pytest.mark.parametrize("requested", [0.0, 0.5, 1.0])
def test_response_request_is_observed_without_changing_disposition(requested) -> None:
    """A repeated request keeps its outcome while the new answer is logged."""
    result = answers(
        audience=audience(dq.AUDIENCE_BOT),
        already_answered=noul(0.59),
        response_requested=noul(requested),
    )
    assert dq.disposition(result) == dq.DISPOSITION_CONTEXT


def test_work_asked_of_the_bot_or_the_room_runs_a_turn_and_posts_nothing() -> None:
    """``needs_work`` is the only route to ``silent`` and is read with
    ``audience``: alone it fires on "I'll take the harness section".
    """
    for who in (dq.AUDIENCE_BOT, dq.AUDIENCE_ROOM):
        assert dq.disposition(
            answers(needs_work=noul(0.94), audience=audience(who))
        ) == dq.DISPOSITION_SILENT


def test_work_somebody_claims_for_themselves_does_not_reach_silent() -> None:
    assert dq.disposition(
        answers(needs_work=noul(0.94), audience=audience(dq.AUDIENCE_NOBODY))
    ) == dq.DISPOSITION_LATER


def test_a_claim_defers_rather_than_speaks() -> None:
    """It reads 0.77 on a true status update and 0.74 on a false one.

    No threshold separates those, so it is sound as a veto against going quiet
    and unsound as a reason to speak. Used as a reason to speak it forced two
    posts, which is the only error a reader of the channel ever sees.
    """
    assert dq.disposition(
        answers(claim=noul(0.77), audience=audience(dq.AUDIENCE_NOBODY))
    ) == dq.DISPOSITION_LATER
    assert dq.disposition(
        answers(claim=noul(0.74), audience=audience(dq.AUDIENCE_ROOM))
    ) == dq.DISPOSITION_LATER


def test_a_request_aimed_at_the_bot_is_answered_where_it_arrived() -> None:
    assert dq.disposition(answers(audience=audience(dq.AUDIENCE_BOT))) == (
        dq.DISPOSITION_HERE
    )


def test_everything_else_defers_to_the_sentinel_that_already_exists() -> None:
    """``later`` is the default rather than the exception: it costs a turn and
    reaches behaviour that is already there, so being wrong is cheap.
    """
    assert dq.disposition(answers()) == dq.DISPOSITION_LATER
    assert dq.disposition(answers(audience=audience(dq.AUDIENCE_ROOM))) == (
        dq.DISPOSITION_LATER
    )


def test_the_floor_is_context_and_nothing_discards_a_message() -> None:
    """The rung below ``context`` was removed. The question that fed it fired
    on zero of thirty lines and every line it would have taken was already
    taken by another clause.
    """
    assert dq.DISPOSITIONS[0] == dq.DISPOSITION_CONTEXT
    assert "ignore" not in dq.DISPOSITIONS
    assert "elsewhere" not in dq.DISPOSITIONS


def test_a_missing_or_wrongly_typed_answer_is_refused() -> None:
    incomplete = answers()
    del incomplete[dq.CLAIM]
    with pytest.raises(ValueError):
        dq.disposition(incomplete)
    wrong_type = answers(claim=Answer(type="choice", value="yes"))
    with pytest.raises(ValueError):
        dq.disposition(wrong_type)


# ---------------------------------------------------------------------------
# The state
# ---------------------------------------------------------------------------


def test_the_state_is_a_named_object_with_message_context() -> None:
    state = dq.build_state(
        channel="C0PLAN",
        bot_name=dq.bot_identity("jiuwen", "U0BOT"),
        recent_messages=[
            {"author": "<@U01>", "text": "standup in five"},
            {"author": "jiuwen", "text": "the harness run finished green"},
        ],
        last_message={"author": "<@U02>", "text": "anyone got the link"},
    )
    assert set(state) == {
        "channel",
        "members",
        "recent_messages",
        "last_message",
        "bot_name",
        "conversation_kind",
        "event_kind",
        "history_scope",
    }
    assert state["last_message"] == {
        "author": "<@U02>",
        "text": "anyone got the link",
    }


def test_the_window_holds_the_bot_s_own_turns() -> None:
    """Their absence is why two labelled lines' real reason for silence -- that
    the bot had already spoken -- was invisible to every measurement taken.
    """
    state = dq.build_state(
        channel="C1",
        bot_name=dq.bot_identity("jiuwen", "U0BOT"),
        recent_messages=[{"author": "jiuwen", "text": "already answered above"}],
        last_message={"author": "<@U02>", "text": "same question again"},
    )
    assert state["recent_messages"][0]["author"] == "jiuwen"
    assert "jiuwen" in state["members"]


def test_the_bot_travels_with_the_token_it_is_mentioned_by() -> None:
    """Slack text holds the token and never the name, so without it the model
    cannot connect the two and ``audience`` cannot be answered at all.
    """
    assert dq.bot_identity("jiuwen", "U0BOT") == "jiuwen (mentioned as <@U0BOT>)"
    assert dq.bot_identity("", "") == "the assistant"
    assert dq.bot_identity("jiuwen", "") == "jiuwen"


def test_the_state_describes_no_corpus() -> None:
    """A constant sentence saying a body of knowledge exists invites exactly
    the guess the gate is kept away from. What the bot knows is settled by
    retrieval inside a turn.
    """
    state = dq.build_state(
        channel="C1",
        bot_name="jiuwen",
        recent_messages=[],
        last_message={"author": "<@U02>", "text": "what did we settle on"},
    )
    rendered = repr(state).lower()
    for word in ("corpus", "paper", "knowledge", "document", "arxiv"):
        assert word not in rendered


def test_the_state_never_states_the_answer_to_a_question_being_asked() -> None:
    """An early probe sent ``bot_was_addressed: false`` and then asked whether
    the bot was addressed. That run is not trusted.
    """
    state = dq.build_state(
        channel="C1",
        bot_name="jiuwen",
        recent_messages=[],
        last_message={"author": "<@U02>", "text": "hello"},
    )
    assert not dq.FORBIDDEN_STATE_KEYS & set(state)
    assert set(dq.QUESTIONS) <= dq.FORBIDDEN_STATE_KEYS


def test_members_default_to_whoever_spoke_in_the_window() -> None:
    state = dq.build_state(
        channel="C1",
        bot_name="jiuwen",
        recent_messages=[
            {"author": "<@U01>", "text": "one"},
            {"author": "<@U01>", "text": "two"},
            {"author": "jiuwen", "text": "three"},
        ],
        last_message={"author": "<@U02>", "text": "four"},
    )
    assert state["members"] == ["<@U01>", "jiuwen", "<@U02>"]
