# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.

"""``clicks`` at the two Slack buttons: the approval, then the stop.

Approvals first, because they are the security-relevant half. The approval
button is the gate on a tool call the agent was told to stop at, and in a
deployment that sets no ``allow_from`` anyone who can see the message can press
it. The stop button follows the same path and its worst failure is a turn
somebody has to start again.

Three clauses, and every test below is one of them:

* **No rule** -- layer 0 decides and nothing here changes. The first two tests
  are the whole of that guarantee: a deployment that has written no ``clicks:``
  block behaves exactly as it did.
* **A rule matches and the clicker cannot be placed**, so the click is refused
  with a reason naming the conversation. Two ways to be unplaceable, and both
  arrive as an id that is in no list: none came, or one came and no ``people:``
  entry maps it into the role the rule names.
* **The starter's allowance is a floor.** A ``clicks.stop`` rule narrows who
  *else* may stop a turn and can never lock the starter out of stopping their
  own.

One thing these tests do not cover because it is not this section's: the sibling
guard on the answer path skips the allow list when the payload holds no user
id. That is deliberately unfixed. What is asserted here is the interaction --
where a ``clicks`` rule exists, the same payload is refused by this gate.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import pytest

from jiuwenswarm.common.schema.message import EventType, Message, ReqMethod
from jiuwenswarm.common.scopes import (
    SECTION_CLICKS,
    channel_capabilities,
    compile_scopes,
)
from jiuwenswarm.gateway.channel_manager.base import RobotMessageRouter
from jiuwenswarm.gateway.channel_manager.im_platforms.slack import slack_connect
from jiuwenswarm.gateway.channel_manager.im_platforms.slack.slack_connect import (
    SlackChannel,
    SlackChannelConfig,
)

OPERATOR = "U0OPERATOR"
MEMBER = "U0MEMBER01"
OUTSIDER = "U0OUTSIDER"
TEAM = "T1"
ROOM = "C1"
THREAD = "1710000000.000100"
SESSION = f"slack_{TEAM}_{ROOM}_{THREAD}"
REQUEST_ID = "req-clicks"

PEOPLE = {"olive": {"slack": OPERATOR}, "mel": {"slack": MEMBER}}
ROLES = {"operator": ["olive"], "member": ["olive", "mel"]}


@pytest.fixture(autouse=True)
def _isolated_dedup_store(tmp_path, monkeypatch):
    real_init = slack_connect.SlackEventDedupStore.__init__
    monkeypatch.setattr(
        slack_connect.SlackEventDedupStore,
        "__init__",
        lambda self, path=None, **kw: real_init(
            self, path or tmp_path / "slack_seen_events.json", **kw
        ),
    )


class _RecordingSlackClient:
    def __init__(self) -> None:
        self.posts: list[dict[str, Any]] = []
        self.updates: list[dict[str, Any]] = []
        self.ephemerals: list[dict[str, Any]] = []

    async def chat_postMessage(self, **kwargs: Any) -> dict[str, str]:
        self.posts.append(kwargs)
        return {"ts": f"1710000099.{len(self.posts):06d}"}

    async def chat_update(self, **kwargs: Any) -> dict[str, str]:
        self.updates.append(kwargs)
        return {"ts": kwargs.get("ts", "")}

    async def chat_postEphemeral(self, **kwargs: Any) -> dict[str, str]:
        self.ephemerals.append(kwargs)
        return {"ok": "true"}


class _Ack:
    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self, *_args: Any, **_kwargs: Any) -> None:
        self.calls += 1


def _scopes(*entries: dict[str, Any]):
    """Compile rules the way the connector does, people and roles included."""
    return compile_scopes(list(entries), people=PEOPLE, roles=ROLES, warn=lambda *_a: None)


def _channel(**overrides: Any) -> tuple[SlackChannel, _RecordingSlackClient, list[Message]]:
    overrides.setdefault("activity_card_delay_seconds", 0.0)
    overrides.setdefault("activity_card_min_edit_seconds", 0.0)
    channel = SlackChannel(
        SlackChannelConfig(enabled=True, default_channel_id=ROOM, **overrides),
        RobotMessageRouter(),
    )
    client = _RecordingSlackClient()
    channel._client = client
    channel._running = True
    dispatched: list[Message] = []
    channel.on_message(dispatched.append)
    return channel, client, dispatched


# ── the approval button ──────────────────────────────────────────────────────


def _question() -> Message:
    return Message(
        id="ask-1",
        type="event",
        channel_id="slack",
        session_id=SESSION,
        params={},
        timestamp=time.time(),
        ok=True,
        payload={
            "event_type": "chat.ask_user_question",
            "request_id": "call_abc123",
            "source": "permission_interrupt",
            "questions": [
                {
                    "question": "Tool `bash` needs permission to run",
                    "header": "Permission",
                    "options": [
                        {"label": "Approve", "value": "approve"},
                        {"label": "Reject", "value": "reject"},
                    ],
                    "multi_select": False,
                }
            ],
        },
        event_type=EventType.CHAT_ASK_USER_QUESTION,
        metadata={"slack_channel_id": ROOM, "slack_thread_ts": THREAD},
    )


def _answer_click(user_id: str | None) -> tuple[dict[str, Any], dict[str, Any]]:
    action = {
        "type": "button",
        "action_id": "jiuwenswarm_answer:0",
        "value": json.dumps(
            {
                "request_id": "call_abc123",
                "index": 0,
                "value": "approve",
                "source": "permission_interrupt",
                "session_id": SESSION,
            },
            separators=(",", ":"),
        ),
        "text": {"type": "plain_text", "text": "Approve"},
    }
    body: dict[str, Any] = {
        "type": "block_actions",
        "team": {"id": TEAM},
        "channel": {"id": ROOM, "name": "general"},
        "container": {
            "type": "message",
            "channel_id": ROOM,
            "message_ts": "1710000099.000001",
        },
        "message": {"ts": "1710000099.000001", "blocks": []},
        "actions": [action],
    }
    if user_id is not None:
        body["user"] = {"id": user_id}
    return body, action


async def _ask_then_click(channel: SlackChannel, user_id: str | None) -> _Ack:
    await channel.send(_question())
    ack = _Ack()
    body, action = _answer_click(user_id)
    await channel._handle_question_action(ack, body, action)
    return ack


def _approve_rule(**criteria: Any) -> dict[str, Any]:
    return {
        "match": {"channel": "slack", "chat": ROOM},
        "clicks": {"approve": criteria},
    }


@pytest.mark.asyncio
async def test_with_no_clicks_rule_anyone_in_the_channel_still_answers():
    """No rule, which is the whole compatibility guarantee of this section."""
    channel, _client, dispatched = _channel()

    await _ask_then_click(channel, OUTSIDER)

    assert len(dispatched) == 1
    assert dispatched[0].params["request_id"] == "call_abc123"


@pytest.mark.asyncio
async def test_a_rule_on_another_conversation_leaves_this_one_alone():
    channel, _client, dispatched = _channel(
        scopes=_scopes(
            {
                "match": {"channel": "slack", "chat": "C_SOMEWHERE_ELSE"},
                "clicks": {"approve": {"role": "operator"}},
            }
        )
    )

    await _ask_then_click(channel, OUTSIDER)

    assert len(dispatched) == 1


@pytest.mark.asyncio
async def test_a_clicks_rule_lets_the_people_it_names_answer():
    channel, _client, dispatched = _channel(scopes=_scopes(_approve_rule(role="operator")))

    await _ask_then_click(channel, OPERATOR)

    assert len(dispatched) == 1


@pytest.mark.asyncio
async def test_a_clicks_rule_refuses_everybody_else():
    channel, client, dispatched = _channel(
        scopes=_scopes(_approve_rule(role="operator"))
    )

    ack = await _ask_then_click(channel, MEMBER)

    assert dispatched == []
    # Acknowledged all the same: Slack shows the clicker an error otherwise, and
    # the acknowledgement says the click arrived, not that it was accepted.
    assert ack.calls == 1
    # The question is left standing, so somebody the rule does name still can.
    assert "call_abc123" in channel._pending_questions
    assert client.updates == []
    assert client.ephemerals
    assert "not permitted" in client.ephemerals[-1]["text"]


@pytest.mark.asyncio
async def test_a_click_that_names_nobody_is_refused_where_a_rule_matches():
    """An unplaceable clicker, in the case that meets the known sibling guard.

    ``if user_id and not self.is_allowed(user_id)`` skips the allow list on a
    payload with no user, which is the permissive reading of exactly this
    case and is deliberately left alone. The ``clicks`` gate below it is not
    written that way: it applies the rule to the empty id rather than skipping
    it, so where a rule exists this payload is refused.
    """
    channel, _client, dispatched = _channel(
        allow_from=[OPERATOR], scopes=_scopes(_approve_rule(role="operator"))
    )

    await _ask_then_click(channel, None)

    assert dispatched == []


@pytest.mark.asyncio
async def test_without_a_rule_that_same_payload_is_unchanged():
    """The gate above closes nothing it was not written to close.

    Same config, same payload, no ``clicks`` rule: the sibling guard still
    behaves exactly as it did, which is what makes this an addition rather than
    a fix smuggled in beside one.
    """
    channel, _client, dispatched = _channel(allow_from=[OPERATOR])

    await _ask_then_click(channel, None)

    assert len(dispatched) == 1


@pytest.mark.asyncio
async def test_a_rule_cannot_admit_somebody_the_allow_list_keeps_out():
    channel, _client, dispatched = _channel(
        allow_from=[OPERATOR], scopes=_scopes(_approve_rule(role="member"))
    )

    await _ask_then_click(channel, MEMBER)

    assert dispatched == []


@pytest.mark.asyncio
async def test_a_role_that_names_nobody_here_refuses_every_answer():
    """An unplaceable clicker: the rule is honourable and it admits nobody."""
    channel, _client, dispatched = _channel(
        scopes=compile_scopes(
            [_approve_rule(role="ghosts")],
            people={"nowhere": {"feishu": "ou_x"}},
            roles={"ghosts": ["nowhere"]},
            warn=lambda *_a: None,
        )
    )

    await _ask_then_click(channel, OPERATOR)

    assert dispatched == []


# ── the stop button ──────────────────────────────────────────────────────────


def _tool_call() -> Message:
    return Message(
        id=REQUEST_ID,
        type="event",
        channel_id="slack",
        session_id=SESSION,
        params={},
        timestamp=time.time(),
        ok=True,
        payload={
            "event_type": EventType.CHAT_TOOL_CALL.value,
            "tool_call": {
                "name": "bash_tool",
                "arguments": {"command": "ls"},
                "tool_call_id": "call-1",
            },
        },
        event_type=EventType.CHAT_TOOL_CALL,
        metadata={"slack_channel_id": ROOM},
    )


def _stop_click(user_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    action = {
        "type": "button",
        "action_id": slack_connect._STOP_ACTION_ID,
        "value": json.dumps(
            {"session_id": SESSION, "request_id": REQUEST_ID, "channel_id": ROOM},
            separators=(",", ":"),
        ),
        "text": {"type": "plain_text", "text": "Stop"},
    }
    body: dict[str, Any] = {
        "type": "block_actions",
        "team": {"id": TEAM},
        "channel": {"id": ROOM},
        "container": {
            "type": "message",
            "channel_id": ROOM,
            "message_ts": "1780000000.000001",
        },
        "message": {"ts": "1780000000.000001", "blocks": []},
        "actions": [action],
    }
    if user_id:
        body["user"] = {"id": user_id}
    return body, action


async def _card(channel: SlackChannel, client: _RecordingSlackClient) -> None:
    await channel.send(_tool_call())
    for _ in range(4):
        await asyncio.sleep(0)
    assert client.posts


def _cancels(dispatched: list[Message]) -> list[Message]:
    return [msg for msg in dispatched if msg.req_method == ReqMethod.CHAT_CANCEL]


def _stop_rule(**criteria: Any) -> dict[str, Any]:
    return {"match": {"channel": "slack", "chat": ROOM}, "clicks": {"stop": criteria}}


@pytest.mark.asyncio
async def test_clicks_stop_narrows_who_else_may_stop_a_turn():
    channel, client, dispatched = _channel(scopes=_scopes(_stop_rule(role="operator")))
    channel._remember_turn_initiator(SESSION, MEMBER, REQUEST_ID, is_dm=False, chat_type="channel")
    await _card(channel, client)

    body, action = _stop_click(OUTSIDER)
    await channel._handle_stop_action(_Ack(), body, action)

    assert _cancels(dispatched) == []
    assert "not permitted" in client.ephemerals[-1]["text"]


@pytest.mark.asyncio
async def test_the_starter_stops_their_own_turn_through_a_rule_naming_others():
    """The floor. The rule narrows who *else* may; it cannot reach this.

    The starter is not in the ``operator`` role and would be refused by the rule
    read on its own. The order of the three clauses is what keeps that from
    happening, so there is no configuration an operator can write that locks
    somebody out of stopping their own work.
    """
    channel, client, dispatched = _channel(scopes=_scopes(_stop_rule(role="operator")))
    channel._remember_turn_initiator(SESSION, MEMBER, REQUEST_ID, is_dm=False, chat_type="channel")
    await _card(channel, client)

    body, action = _stop_click(MEMBER)
    await channel._handle_stop_action(_Ack(), body, action)

    assert len(_cancels(dispatched)) == 1


@pytest.mark.asyncio
async def test_a_rule_naming_nobody_here_still_cannot_reach_the_starter():
    """The floor holds against the strictest rule expressible, not only a lax one."""
    channel, client, dispatched = _channel(
        scopes=compile_scopes(
            [_stop_rule(role="ghosts")],
            people={"nowhere": {"feishu": "ou_x"}},
            roles={"ghosts": ["nowhere"]},
            warn=lambda *_a: None,
        )
    )
    channel._remember_turn_initiator(SESSION, MEMBER, REQUEST_ID, is_dm=False, chat_type="channel")
    await _card(channel, client)

    body, action = _stop_click(MEMBER)
    await channel._handle_stop_action(_Ack(), body, action)

    assert len(_cancels(dispatched)) == 1


@pytest.mark.asyncio
async def test_somebody_the_rule_names_may_stop_a_turn_they_did_not_start():
    channel, client, dispatched = _channel(scopes=_scopes(_stop_rule(role="operator")))
    channel._remember_turn_initiator(SESSION, MEMBER, REQUEST_ID, is_dm=False, chat_type="channel")
    await _card(channel, client)

    body, action = _stop_click(OPERATOR)
    await channel._handle_stop_action(_Ack(), body, action)

    assert len(_cancels(dispatched)) == 1


@pytest.mark.asyncio
async def test_with_no_stop_rule_the_allow_list_decides_as_before():
    channel, client, dispatched = _channel()
    await _card(channel, client)

    body, action = _stop_click(OUTSIDER)
    await channel._handle_stop_action(_Ack(), body, action)

    assert len(_cancels(dispatched)) == 1


@pytest.mark.asyncio
async def test_a_stop_rule_cannot_admit_somebody_the_allow_list_keeps_out():
    channel, client, dispatched = _channel(
        allow_from=[OPERATOR], scopes=_scopes(_stop_rule(role="member"))
    )
    await _card(channel, client)

    body, action = _stop_click(MEMBER)
    await channel._handle_stop_action(_Ack(), body, action)

    assert _cancels(dispatched) == []


# ── the declaration ──────────────────────────────────────────────────────────


def test_slack_declares_both_gestures():
    declared = channel_capabilities("slack")

    assert declared.reads(SECTION_CLICKS)
    assert declared.acts_on(SECTION_CLICKS, "approve")
    assert declared.acts_on(SECTION_CLICKS, "stop")


def test_slack_identifies_the_clicker():
    # Without this a rule here would refuse every click rather than gate one:
    # an unplaceable clicker, rather than an ineffective section.
    declared = channel_capabilities("slack")

    assert declared.populates("user")
    assert declared.identity_keys == ("user",)
