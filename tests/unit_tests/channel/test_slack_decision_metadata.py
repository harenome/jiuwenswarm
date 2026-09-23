# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Decision inputs preserve message facts and exclude policy conclusions."""

import asyncio

import pytest

from jiuwenswarm.common.typed_decision import TypedDecisionClient
from jiuwenswarm.gateway.channel_manager.im_platforms.slack import decision_questions as dq
from tests.unit_tests.channel.test_slack_decision_shadow import (
    ASKER, TS, StubAdapter, body, channel, event, quiet_answers,
    _isolated_dedup_store, _no_workspace_config,
)


@pytest.mark.parametrize("chat_type,expected", [
    ("im", "one_to_one_dm"), ("mpim", "group_dm"),
    ("channel", "public_channel"), ("group", "private_channel"),
    ("", "unknown"), ("unexpected", "unknown"),
])
def test_conversation_kind_uses_explicit_type(chat_type, expected):
    state = dq.build_state(
        channel="C001", bot_name="assistant", recent_messages=[],
        last_message={"author": "user", "text": "hello"}, chat_type=chat_type,
    )
    assert state["conversation_kind"] == expected
    assert state["event_kind"] == "message"
    assert state["history_scope"] == "channel"


def test_message_copy_preserves_facts_and_drops_policy_fields():
    entry = {
        "author": "user", "text": "check this", "ts": "1700000001.000001",
        "thread_ts": "1700000000.000001", "file_count": 2,
        "attachment_count": 1, "addressed": True, "trigger": "mention",
        "response_requested": True, "thread_position": "root",
    }
    state = dq.build_state(
        channel="C001", bot_name="assistant", recent_messages=[entry],
        last_message=entry,
    )
    expected = {key: value for key, value in entry.items()
                if key not in dq.FORBIDDEN_STATE_KEYS}
    expected["thread_position"] = "reply"
    assert state["last_message"] == expected
    assert state["recent_messages"] == [expected]


@pytest.mark.parametrize("ts,thread_ts,expected", [
    ("1.000001", "", "root"), ("1.000001", "1.000001", "root"),
    ("2.000001", "1.000001", "reply"), ("", "1.000001", "unknown"),
])
def test_thread_position_distinguishes_parent_from_reply(ts, thread_ts, expected):
    entry = dq.message_entry("user", "text", ts=ts, thread_ts=thread_ts)
    assert entry["thread_position"] == expected


async def test_connector_supplies_message_facts_to_model():
    adapter = StubAdapter(answers=quiet_answers())
    built, _client, _received, _acknowledged, gate = channel(
        make_gate=lambda window: dq.ShadowDecisionGate(
            TypedDecisionClient(adapter, attempts=1, timeout=1),
            recent_messages=window,
        ),
        history=[{
            "user": ASKER, "text": "earlier", "ts": "1700000000.000001",
            "thread_ts": "1700000000.000001", "files": [{"id": "F001"}],
        }],
    )
    incoming = event()
    incoming.update(channel_type="group", thread_ts="1700000000.000001")
    await built._handle_slack_event(incoming, body(), is_dm=False, trigger="all")
    await asyncio.sleep(0.05)
    await gate.close()
    state = adapter.states[0]
    assert state["conversation_kind"] == "private_channel"
    assert state["last_message"]["ts"] == TS
    assert state["last_message"]["thread_position"] == "reply"
    assert state["last_message"]["file_count"] == 0
    assert state["last_message"]["attachment_count"] == 0
    previous = state["recent_messages"][0]
    assert previous["ts"] == "1700000000.000001"
    assert previous["thread_position"] == "root"
    assert previous["file_count"] == 1


def test_log_versions_identify_state_separately_from_questions():
    record = dq.decision_record(
        identity={}, answers=None, outcome="declined", adapter="stub",
        latency_ms=0, window=0,
    )
    assert record["state_version"] == 2
    assert record["question_set_version"] == 2
    assert record["v"] == 1
