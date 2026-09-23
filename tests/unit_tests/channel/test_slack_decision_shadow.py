# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The decision gate runs in shadow and cannot reach a turn.

This is the test that matters. The rule the gate applies was fitted to thirty
lines of one fixture and has never met real traffic, so it is allowed to
observe and nothing else. A decision model that is absent, slow, broken, or
answering confidently that the bot should stay quiet has to leave the path
byte-identical.

The proof is a trace. One message is driven through ``_handle_slack_event``
under five decision models, and everything the connector does outward -- the
outcome string it returns, the acknowledgement, every Slack API call, and the
request it routes upstream -- is compared against the run with no decision
model at all. A difference anywhere is a shadow that reached a turn.

Two static checks sit beside it, because a trace proves what today's code does
and the structure is what keeps tomorrow's honest: the call site is a bare
statement whose value nothing reads, and the method it calls returns ``None``
and contains no ``await``.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import json
import logging
import textwrap
from typing import Any

import pytest

from jiuwenswarm.common.schema.message import Message
from jiuwenswarm.common.typed_decision import (
    Answer,
    Question,
    TypedDecisionClient,
    TypedDecisionError,
)
from jiuwenswarm.gateway.channel_manager.base import RobotMessageRouter
from jiuwenswarm.gateway.channel_manager.im_platforms.slack import slack_connect
from jiuwenswarm.gateway.channel_manager.im_platforms.slack import (
    decision_questions as dq,
)
from jiuwenswarm.gateway.channel_manager.im_platforms.slack.slack_connect import (
    SlackChannel,
    SlackChannelConfig,
)

ASKER = "U0ASKER001"
BOT = "U0BOT00001"
TEAM = "T0TESTTEAM"
ROOM = "C0CHANNEL1"
TS = "1710000001.000100"


@pytest.fixture(autouse=True)
def _isolated_dedup_store(tmp_path, monkeypatch):
    """A dedup file per connector, never the real workspace's.

    Per connector rather than per test, because every comparison here drives
    the same message twice. One shared file would have the second run report
    the duplicate rather than the behaviour being compared.
    """
    real_init = slack_connect.SlackEventDedupStore.__init__
    built = [0]

    def isolated(self, path=None, **kw):
        built[0] += 1
        return real_init(self, path or tmp_path / f"seen-{built[0]}.json", **kw)

    monkeypatch.setattr(slack_connect.SlackEventDedupStore, "__init__", isolated)


@pytest.fixture(autouse=True)
def _no_workspace_config(monkeypatch):
    """Nothing here reads the operator's configuration.

    Every gate in these tests is injected, so a run must never answer the
    question "is a decision model configured" out of a file on the host.
    """
    import jiuwenswarm.common.config as config_module

    monkeypatch.setattr(config_module, "get_config", lambda *a, **kw: {})


class RecordingSlackClient:
    """Every outward Slack call, in the order it was made."""

    def __init__(self, *, history: list[dict[str, Any]] | None = None) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.history = history if history is not None else []

    async def chat_postMessage(self, **kwargs: Any) -> dict[str, str]:
        self.calls.append(("chat_postMessage", kwargs))
        return {"ts": "1710000099.000001"}

    async def chat_update(self, **kwargs: Any) -> dict[str, str]:
        self.calls.append(("chat_update", kwargs))
        return {"ts": kwargs.get("ts", "")}

    async def reactions_add(self, **kwargs: Any) -> dict[str, bool]:
        self.calls.append(("reactions_add", kwargs))
        return {"ok": True}

    async def reactions_remove(self, **kwargs: Any) -> dict[str, bool]:
        self.calls.append(("reactions_remove", kwargs))
        return {"ok": True}

    async def conversations_history(self, **kwargs: Any) -> dict[str, Any]:
        # Recorded under its own name so the trace can show that a shadow read
        # happened without letting it count as an outward act of the turn.
        self.calls.append(("conversations_history", kwargs))
        return {"messages": list(self.history)}


class StubAdapter:
    """A decision model that answers, fails or never returns."""

    name = "stub"
    modalities = frozenset({"text"})

    def __init__(self, *, answers=None, error=None, delay=0.0) -> None:
        self._answers = answers
        self._error = error
        self._delay = delay
        self.states: list[Any] = []

    async def ask(self, state, questions: dict[str, Question]):
        self.states.append(state)
        if self._delay:
            await asyncio.sleep(self._delay)
        if self._error is not None:
            raise self._error
        return dict(self._answers or {})


def quiet_answers() -> dict[str, Answer]:
    """The answer set that would skip the turn outright under enforcement."""
    return {
        dq.AUDIENCE: Answer(type="choice", value=dq.AUDIENCE_PERSON, confidence=0.97),
        dq.ALREADY_ANSWERED: Answer(type="noul", value=0.96),
        dq.RESPONSE_REQUESTED: Answer(type="noul", value=0.91),
        dq.CLAIM: Answer(type="noul", value=0.02),
        dq.NEEDS_WORK: Answer(type="noul", value=0.01),
        dq.WORTH_REMEMBERING: Answer(type="noul", value=0.00),
        dq.DESERVES_EMOJI: Answer(type="noul", value=0.01),
    }


def channel(
    *, make_gate=None, history=None
) -> tuple[SlackChannel, RecordingSlackClient, list[Message], list[tuple]]:
    """A connector with every outward call recorded, and a gate wired as the
    connector itself wires one: its window comes from ``_decision_window``.
    """
    built = SlackChannel(
        SlackChannelConfig(
            enabled=True, allow_from=[ASKER], bot_name="jiuwen", group_chat_mode="all"
        ),
        RobotMessageRouter(),
    )
    built._running = True
    built._bot_user_id = BOT
    client = RecordingSlackClient(history=history)
    built._client = client
    acknowledged: list[tuple] = []

    async def record_acknowledgement(*args: Any, **kwargs: Any) -> None:
        acknowledged.append((args, tuple(sorted(kwargs.items()))))

    built._acknowledge_request = record_acknowledgement  # type: ignore[method-assign]
    # ``False`` is the connector's own word for "asked, and there is none".
    gate = make_gate(built._decision_window) if make_gate is not None else None
    built._decision_gate = gate if gate is not None else False
    received: list[Message] = []
    built.on_message(received.append)
    return built, client, received, acknowledged, gate


def event() -> dict[str, Any]:
    return {
        "type": "message",
        "channel_type": "channel",
        "channel": ROOM,
        "user": ASKER,
        "text": "anyone remember what the retry budget ended up as",
        "ts": TS,
    }


def body() -> dict[str, Any]:
    return {"team_id": TEAM, "event_id": "Ev0000000001"}


async def trace(make_gate=None, history=None, *, is_dm=False) -> tuple:
    """Everything one message makes this connector do, outward."""
    built, client, received, acknowledged, gate = channel(
        make_gate=make_gate, history=history
    )
    incoming = event()
    if is_dm:
        incoming.update(channel_type="im", channel="D0DIRECT1", text="ping")
    outcome = await built._handle_slack_event(
        incoming, body(), is_dm=is_dm, trigger="dm" if is_dm else "all"
    )
    # Let anything the shadow started run to completion before the trace is
    # read, so a difference it could make has every chance to appear.
    await asyncio.sleep(0.05)
    if gate is not None:
        await gate.close()
    turn_calls = [
        (name, kwargs)
        for name, kwargs in client.calls
        if name != "conversations_history"
    ]
    return (
        outcome,
        acknowledged,
        turn_calls,
        [(msg.req_method, msg.session_id, msg.params) for msg in received],
    )


# ---------------------------------------------------------------------------
# The trace
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "make_gate",
    [
        pytest.param(
            lambda window: dq.ShadowDecisionGate(
                TypedDecisionClient(
                    StubAdapter(answers=quiet_answers()), attempts=1, timeout=1.0
                ),
                recent_messages=window,
            ),
            id="a-model-that-says-stay-quiet",
        ),
        pytest.param(
            lambda window: dq.ShadowDecisionGate(
                TypedDecisionClient(
                    StubAdapter(error=TypedDecisionError("endpoint refused")),
                    attempts=1,
                    timeout=1.0,
                    retry_delay=0.0,
                ),
                recent_messages=window,
            ),
            id="a-model-that-fails",
        ),
        pytest.param(
            lambda window: dq.ShadowDecisionGate(
                TypedDecisionClient(
                    StubAdapter(error=RuntimeError("something nobody anticipated")),
                    attempts=1,
                    timeout=1.0,
                    retry_delay=0.0,
                ),
                recent_messages=window,
            ),
            id="a-model-that-raises-something-unexpected",
        ),
        pytest.param(
            lambda window: dq.ShadowDecisionGate(
                TypedDecisionClient(
                    StubAdapter(delay=30.0), attempts=1, timeout=0.02
                ),
                recent_messages=window,
                budget=0.05,
            ),
            id="a-model-that-never-returns",
        ),
        pytest.param(
            lambda window: dq.ShadowDecisionGate(
                TypedDecisionClient(
                    StubAdapter(answers={}), attempts=1, timeout=1.0
                ),
                recent_messages=window,
            ),
            id="a-model-that-answers-nothing",
        ),
    ],
)
@pytest.mark.parametrize("is_dm", [False, True], ids=["channel", "dm"])
async def test_no_decision_model_changes_what_the_connector_does(make_gate, is_dm) -> None:
    """The trace with a gate equals the trace without one, exactly."""
    without = await trace(None, is_dm=is_dm)
    with_gate = await trace(make_gate, is_dm=is_dm)
    assert with_gate == without


async def test_a_slow_model_does_not_delay_the_acknowledgement() -> None:
    """The gate is above the acknowledgement and must not push it back.

    A shadow that made the bot look slow to notice a message would be changing
    the turn by every measure a person in the channel has.
    """
    built, _client, _received, acknowledged, gate = channel(
        make_gate=lambda window: dq.ShadowDecisionGate(
            TypedDecisionClient(StubAdapter(delay=30.0), attempts=1, timeout=30.0),
            recent_messages=window,
            budget=30.0,
        )
    )
    loop = asyncio.get_running_loop()
    started = loop.time()
    await built._handle_slack_event(event(), body(), is_dm=False, trigger="all")
    elapsed = loop.time() - started
    assert acknowledged, "the acknowledgement was not sent"
    assert elapsed < 1.0, f"the handler waited {elapsed:.2f}s on the decision model"
    await gate.close()


async def test_a_gate_that_cannot_even_be_started_is_swallowed() -> None:
    """``observe`` raises nothing, whatever is wrong under it."""

    class Exploding:
        def observe(self, **kwargs: Any) -> None:
            raise RuntimeError("no loop, no client, no anything")

    built, _client, _received, acknowledged, _gate = channel()
    built._decision_gate = Exploding()  # type: ignore[assignment]
    outcome = await built._handle_slack_event(
        event(), body(), is_dm=False, trigger="all"
    )
    assert outcome == (await trace(None))[0]
    assert acknowledged


# ---------------------------------------------------------------------------
# The structure
# ---------------------------------------------------------------------------


def test_the_call_site_reads_nothing_back() -> None:
    """A bare statement, never an assignment and never an await.

    The trace above proves what today's code does. This is what keeps a later
    edit from quietly making the gate load-bearing.
    """
    source = textwrap.dedent(inspect.getsource(SlackChannel._handle_slack_event))
    handler = ast.parse(source).body[0]
    sites = [
        node
        for node in ast.walk(handler)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "_observe_decision_in_shadow"
    ]
    assert len(sites) == 1, "the gate is called once, or the position moved"
    statements = [
        node
        for node in ast.walk(handler)
        if isinstance(node, ast.Expr) and node.value in sites
    ]
    assert statements, "the gate's result is read by something"


def test_the_gate_method_returns_none_and_never_awaits() -> None:
    method = SlackChannel._observe_decision_in_shadow
    assert not inspect.iscoroutinefunction(method)
    assert inspect.signature(method).return_annotation in (None, "None")
    tree = ast.parse(textwrap.dedent(inspect.getsource(method))).body[0]
    assert not [node for node in ast.walk(tree) if isinstance(node, ast.Await)]
    returns = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Return) and node.value is not None
    ]
    assert not returns, "a shadow gate may not hand anything back"


def test_the_gate_sits_below_the_trigger_checks_and_above_the_acknowledgement() -> None:
    """Three constraints decide the position, and two of them are orderings.

    It must precede the acknowledgement, which is the first outward act of the
    path: a decision taken after it means the bot visibly reacts to a message
    it then ignores. And it must sit outside the dedupe lock, which is held
    across a call and would queue every inbound event in the workspace behind
    a network request.
    """
    source = textwrap.dedent(inspect.getsource(SlackChannel._handle_slack_event))
    gate_at = source.index("_observe_decision_in_shadow")
    assert gate_at > source.index("_remember_event")
    assert gate_at > source.index("self.is_allowed(user_id)")
    assert gate_at < source.index("await self._acknowledge_request(")


# ---------------------------------------------------------------------------
# The log line
# ---------------------------------------------------------------------------


async def test_the_record_holds_the_answers_the_disposition_and_no_text(
    caplog,
) -> None:
    """The log line is the deliverable, so it is checked as one."""
    adapter = StubAdapter(answers=quiet_answers())

    def make_gate(window):
        return dq.ShadowDecisionGate(
            TypedDecisionClient(adapter, attempts=1, timeout=1.0),
            recent_messages=window,
        )

    # Newest first, as Slack returns it.
    history = [
        {"user": BOT, "text": "the retry budget is four seconds", "ts": "1710000001.000099"},
        {"user": ASKER, "text": "standup in five", "ts": "1710000000.000100"},
    ]
    with caplog.at_level(logging.INFO, logger=slack_connect.logger.name):
        await trace(make_gate, history=history)

    lines = [
        message
        for message in caplog.messages
        if message.startswith(dq.LOG_PREFIX)
    ]
    assert len(lines) == 1
    record = json.loads(lines[0][len(dq.LOG_PREFIX) :].strip())

    assert record["v"] == dq.LOG_VERSION
    assert record["question_set_version"] == 2
    assert record["shadow"] is True
    assert record["outcome"] == dq.DISPOSITION_CONTEXT
    assert record["adapter"] == "stub"
    assert record["window"] == 2
    assert isinstance(record["latency_ms"], int)
    # Enough identity to find the message again.
    assert record["team"] == TEAM
    assert record["channel"] == ROOM
    assert record["ts"] == TS
    assert record["user"] == ASKER
    assert record["chat_type"] == "channel"
    # Every answer, with its raw value and whatever scale came back with it.
    assert set(record["answers"]) == set(dq.QUESTIONS)
    assert record["answers"][dq.RESPONSE_REQUESTED] == {
        "type": "noul", "value": 0.91,
    }
    assert record["answers"][dq.AUDIENCE] == {
        "type": "choice",
        "value": dq.AUDIENCE_PERSON,
        "confidence": 0.97,
    }
    assert record["answers"][dq.CLAIM] == {"type": "noul", "value": 0.02}
    # No message text, here or anywhere: these lines persist and the content
    # is the users'.
    rendered = lines[0]
    assert "retry budget" not in rendered
    assert "standup" not in rendered


async def test_a_failed_decision_is_recorded_so_the_denominator_survives(
    caplog,
) -> None:
    """An analysis that saw only the successes could not tell a rule that holds
    from a model that answered a third of the time.
    """
    def make_gate(window):
        return dq.ShadowDecisionGate(
            TypedDecisionClient(
                StubAdapter(error=TypedDecisionError("endpoint refused")),
                attempts=1,
                timeout=1.0,
                retry_delay=0.0,
            ),
            recent_messages=window,
        )

    with caplog.at_level(logging.INFO, logger=slack_connect.logger.name):
        await trace(make_gate)
    lines = [m for m in caplog.messages if m.startswith(dq.LOG_PREFIX)]
    assert len(lines) == 1
    record = json.loads(lines[0][len(dq.LOG_PREFIX) :].strip())
    assert record["outcome"] == "declined"
    assert "TypedDecisionError" in record["error"]
    assert "answers" not in record


async def test_the_window_holds_the_bot_s_own_turns_into_the_state() -> None:
    """Their absence is the single change most likely to move every number the
    design was measured on, so it is checked at the seam that supplies them.
    """
    adapter = StubAdapter(answers=quiet_answers())
    await trace(
        lambda window: dq.ShadowDecisionGate(
            TypedDecisionClient(adapter, attempts=1, timeout=1.0),
            recent_messages=window,
        ),
        # Newest first, which is what ``conversations.history`` returns. The
        # state reads oldest first, in the order the conversation happened.
        history=[
            {"user": BOT, "text": "I do, since tuesday", "ts": "1710000000.000200"},
            {"user": ASKER, "text": "who owns the harness", "ts": "1710000000.000100"},
        ],
    )
    assert adapter.states, "the decision model was never asked"
    state = adapter.states[0]
    authors = [entry["author"] for entry in state["recent_messages"]]
    assert authors == [f"<@{ASKER}>", "jiuwen"]
    assert state["bot_name"] == f"jiuwen (mentioned as <@{BOT}>)"
    assert state["channel"] == ROOM
    assert state["last_message"]["author"] == f"<@{ASKER}>"


async def test_shadow_preserves_the_leading_bot_mention() -> None:
    adapter = StubAdapter(answers=quiet_answers())
    built, _client, received, _acknowledged, gate = channel(
        make_gate=lambda window: dq.ShadowDecisionGate(
            TypedDecisionClient(adapter, attempts=1, timeout=1.0),
            recent_messages=window,
        )
    )
    incoming = event()
    incoming["text"] = f"<@{BOT}> are you available?"
    await built._handle_slack_event(incoming, body(), is_dm=False, trigger="mention")
    await asyncio.gather(*tuple(gate._tasks))
    assert adapter.states[0]["last_message"]["text"] == incoming["text"]
    assert received
    await gate.close()


async def test_shadow_history_excludes_the_target_and_newer_messages() -> None:
    built, client, _received, _acknowledged, _gate = channel(history=[
        {"user": BOT, "text": "new reply", "ts": "1710000001.000101"},
        {"user": ASKER, "text": "target", "ts": TS},
        {"user": BOT, "text": "earlier reply", "ts": "1710000001.000099"},
        {"user": ASKER, "text": "unknown time"},
        {"user": ASKER, "text": "invalid time", "ts": "NaN"},
    ])
    window = await built._decision_window(ROOM, 12, TS)
    assert [entry["text"] for entry in window] == ["earlier reply"]
    assert client.calls == [("conversations_history", {
        "channel": ROOM, "limit": 12, "latest": TS, "inclusive": False,
    })]


@pytest.mark.parametrize("timestamp", ["", "invalid", "NaN", "Infinity", "-1.0"])
async def test_shadow_history_skips_reads_without_a_valid_cutoff(timestamp) -> None:
    built, client, _received, _acknowledged, _gate = channel()
    assert await built._decision_window(ROOM, 12, timestamp) == []
    assert client.calls == []
