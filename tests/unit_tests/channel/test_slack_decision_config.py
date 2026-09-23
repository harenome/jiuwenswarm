# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Operator questions preserve defaults and guard dependent rules."""

import json
import logging
from unittest.mock import AsyncMock

import pytest

from jiuwenswarm.common.typed_decision import Answer, Question
from jiuwenswarm.gateway.channel_manager.im_platforms.slack import decision_questions as dq


def load(tmp_path, value):
    path = tmp_path / "questions.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    return dq.load_questions(path)


def test_defaults_and_whole_definition_replacement(tmp_path):
    assert dq.load_questions() == dq.QUESTIONS
    questions = load(tmp_path, {
        "audience": {"type": "noul", "instructions": "A response fits."},
        "claim": None,
        "custom": {"type": "score", "instructions": "Rate usefulness.",
                   "criteria": ["low", "high"]},
    })
    assert questions["audience"].criteria is None
    assert "claim" not in questions
    assert questions["custom"].type == "score"
    assert questions["deserves_emoji"] == dq.QUESTIONS["deserves_emoji"]
    assert dq.QUESTIONS["audience"].type == "choice"


@pytest.mark.parametrize("value", [[], {"": None}, {"x": []},
    {"x": {"type": "noul", "instructions": 42}},
    {"x": {"type": "noul", "instructions": "ok", "extra": True}},
    {"x": {"type": "choice", "instructions": "ok", "criteria": {"a": 1}}},
    {"x": {"type": "score", "instructions": "ok", "criteria": ["one"]}},
    {"x": {"type": "noul", "instructions": "ok", "criteria": []}},
    {"x": {"type": "unknown", "instructions": "ok"}},
])
def test_invalid_definitions_are_rejected(tmp_path, value):
    with pytest.raises(dq.QuestionConfigurationError):
        load(tmp_path, value)


@pytest.mark.parametrize("text", ['{"x":null,"x":null}', '{"x":NaN}', '{broken', '\udcff'])
def test_invalid_json_is_rejected_without_echoing_content(tmp_path, text):
    path = tmp_path / "bad.json"
    path.write_bytes(text.encode("utf-8", errors="surrogatepass"))
    with pytest.raises(dq.QuestionConfigurationError) as exc:
        dq.load_questions(path)
    assert text not in str(exc.value)


def test_required_definition_guard(tmp_path):
    assert dq.disposition_is_compatible(load(tmp_path, {
        "audience": dq.QUESTIONS["audience"].as_request_value(),
        "deserves_emoji": None,
        "extra": {"type": "noul", "instructions": "A response fits."},
    }))
    for key in (dq.AUDIENCE, dq.ALREADY_ANSWERED, dq.NEEDS_WORK, dq.CLAIM):
        assert not dq.disposition_is_compatible(load(tmp_path, {key: None}))
        definition = dq.QUESTIONS[key].as_request_value()
        definition["instructions"] += " Changed."
        assert not dq.disposition_is_compatible(load(tmp_path, {key: definition}))
    definition = dq.QUESTIONS[dq.AUDIENCE].as_request_value()
    definition["criteria"]["you"] = "Changed option."
    assert not dq.disposition_is_compatible(load(tmp_path, {dq.AUDIENCE: definition}))


def test_hash_ignores_object_order_but_preserves_score_order(tmp_path):
    questions = dq.load_questions()
    assert dq.question_set_hash(questions) == dq.question_set_hash(dict(reversed(list(questions.items()))))
    first = load(tmp_path, {"x": {"type": "score", "instructions": "Rate it.", "criteria": ["low", "high"]}})
    second = load(tmp_path, {"x": {"type": "score", "instructions": "Rate it.", "criteria": ["high", "low"]}})
    assert dq.question_set_hash(first) != dq.question_set_hash(second)


def test_question_count_has_no_application_cap(tmp_path):
    questions = load(tmp_path, {f"q{i}": {"type": "noul", "instructions": "A response fits."} for i in range(1000)})
    assert len(questions) == 1000 + len(dq.QUESTIONS)


async def test_changed_required_question_retains_answers_without_outcome(tmp_path, caplog):
    questions = load(tmp_path, {"audience": None})
    client = AsyncMock()
    client.name = "test"
    client.decide.return_value = {"claim": Answer(type="noul", value=.7)}
    with caplog.at_level(logging.INFO, logger=dq.__name__):
        gate = dq.ShadowDecisionGate(client, questions=questions)
        await gate._observe(channel="C1", bot_name="bot", last_message={"text": "PRIVATE"}, identity={})
    records = [json.loads(record.message.split(dq.LOG_PREFIX, 1)[1]) for record in caplog.records if dq.LOG_PREFIX in record.message]
    assert records[0]["outcome"] is None
    assert records[0]["outcome_reason"] == "question_definitions_changed"
    assert records[0]["answers"]["claim"]["value"] == .7
    assert records[0]["question_set_hash"] == dq.question_set_hash(questions)
    assert "PRIVATE" not in caplog.text
    assert '"questions"' in caplog.text


@pytest.fixture
def connector(tmp_path, monkeypatch):
    from jiuwenswarm.common import config, typed_decision, utils
    from jiuwenswarm.gateway.channel_manager.im_platforms.slack import slack_connect as sc
    from jiuwenswarm.gateway.channel_manager.base import RobotMessageRouter

    monkeypatch.setattr(config, "get_config", lambda: {})
    monkeypatch.setattr(utils, "get_config_dir", lambda: tmp_path)
    monkeypatch.setattr(typed_decision, "build_client", lambda _: AsyncMock(name="client"))
    original = sc.SlackEventDedupStore.__init__
    monkeypatch.setattr(sc.SlackEventDedupStore, "__init__", lambda self, path=None, **kw: original(self, tmp_path / "dedup.json", **kw))
    return sc.SlackChannel(sc.SlackChannelConfig(decision_questions_file="questions.json"), RobotMessageRouter())


def test_connector_loads_relative_path_once(connector, tmp_path):
    load(tmp_path, {"claim": None})
    gate = connector._shadow_decision_gate()
    assert "claim" not in gate._questions
    load(tmp_path, {})
    assert connector._shadow_decision_gate() is gate
    assert "claim" not in gate._questions


def test_connector_logs_effective_question_changes_once(connector, tmp_path, caplog):
    from jiuwenswarm.gateway.channel_manager.im_platforms.slack import slack_connect as sc

    changed_claim = dq.QUESTIONS["claim"].as_request_value()
    changed_claim["instructions"] = "PRIVATE INSTRUCTION"
    load(tmp_path, {
        "audience": dq.QUESTIONS["audience"].as_request_value(),
        "claim": changed_claim,
        "deserves_emoji": None,
        "custom\nquestion": {"type": "noul", "instructions": "PRIVATE INSTRUCTION"},
    })
    with caplog.at_level(logging.INFO, logger=sc.__name__):
        gate = connector._shadow_decision_gate()
        assert connector._shadow_decision_gate() is gate
    messages = [
        record.getMessage() for record in caplog.records
        if record.name == sc.__name__ and "decision question " in record.getMessage()
    ]
    assert messages == [
        '[SlackChannel] decision question replaced: "claim"',
        '[SlackChannel] decision question added: "custom\\nquestion"',
        '[SlackChannel] decision question disabled: "deserves_emoji"',
    ]
    assert all("PRIVATE INSTRUCTION" not in message for message in messages)


@pytest.mark.parametrize("contents", ["invalid SECRET", "disable_all"])
def test_invalid_or_empty_config_disables_only_gate(connector, tmp_path, caplog, contents):
    if contents == "disable_all":
        load(tmp_path, dict.fromkeys(dq.QUESTIONS))
    else:
        (tmp_path / "questions.json").write_text(contents)
    with caplog.at_level(logging.INFO):
        assert connector._shadow_decision_gate() is None
        assert connector._shadow_decision_gate() is None
    assert connector._decision_gate is False
    assert "decision disabled" in caplog.text
    assert "SECRET" not in caplog.text


def test_gate_owns_definitions_snapshot():
    questions = dq.load_questions()
    gate = dq.ShadowDecisionGate(AsyncMock(), questions=questions)
    digest = gate._question_set_hash
    questions["audience"].criteria["you"] = "Changed."
    assert dq.question_set_hash(gate._questions) == digest
    assert gate._questions["audience"] == dq.QUESTIONS["audience"]


def test_choice_descriptions_accept_structured_json(tmp_path):
    questions = load(tmp_path, {"custom": {
        "type": "choice", "instructions": "Choose a reaction.",
        "criteria": {"a": {"meaning": "acknowledge"}, "b": ["joy", "surprise"]},
    }})
    assert questions["custom"].criteria["a"] == {"meaning": "acknowledge"}


async def test_start_loads_questions_before_sdk_and_ignores_later_edits(connector, tmp_path, monkeypatch):
    from jiuwenswarm.gateway.channel_manager.im_platforms.slack import slack_connect as sc

    class StopBeforeNetwork(Exception):
        pass

    connector.config.enabled = True
    connector.config.bot_token = "test-token"
    connector.config.app_token = "test-app-token"
    load(tmp_path, {"claim": None})
    monkeypatch.setattr(sc, "SLACK_AVAILABLE", True)

    def stop(**kwargs):
        assert isinstance(connector._decision_gate, dq.ShadowDecisionGate)
        assert "claim" not in connector._decision_gate._questions
        raise StopBeforeNetwork

    monkeypatch.setattr(sc, "AsyncApp", stop)
    with pytest.raises(StopBeforeNetwork):
        await connector.start()
    load(tmp_path, {})
    assert "claim" not in connector._shadow_decision_gate()._questions
