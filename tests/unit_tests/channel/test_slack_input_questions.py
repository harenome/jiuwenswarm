"""Unit tests for questions answered with entered values rather than a button.

The option-button lifecycle these ride on is covered by
``test_slack_approvals``; what is tested here is the part that differs -- the
elements, the submit that commits them, and the ways a submit can fail to be an
answer -- plus the parts that must *not* differ, which is everything about
withdrawal, staleness and dispatch.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

import pytest

from jiuwenswarm.agents.harness.common.rails.interrupt.interrupt_helpers import (
    convert_interactions_to_ask_user_question,
)
from jiuwenswarm.common.schema.message import EventType, Message, ReqMethod
from jiuwenswarm.gateway.channel_manager.base import RobotMessageRouter
from jiuwenswarm.gateway.channel_manager.im_platforms.slack import slack_connect
from jiuwenswarm.gateway.channel_manager.im_platforms.slack.slack_connect import (
    SlackChannel,
    SlackChannelConfig,
)

_SUBMIT_ACTION_ID = "jiuwenswarm_answer:submit"
_INPUT_PREFIX = "jiuwenswarm_answer:input:"


@pytest.fixture(autouse=True)
def _isolated_dedup_store(tmp_path, monkeypatch):
    """Keep the durable dedup store out of the real workspace."""
    real_init = slack_connect.SlackEventDedupStore.__init__
    monkeypatch.setattr(
        slack_connect.SlackEventDedupStore,
        "__init__",
        lambda self, path=None, **kw: real_init(
            self, path or tmp_path / "slack_seen_events.json", **kw
        ),
    )


class _RecordingSlackClient:
    """Records every posted, updated and ephemeral message."""

    def __init__(self) -> None:
        self.posts: list[dict[str, Any]] = []
        self.updates: list[dict[str, Any]] = []
        self.ephemeral: list[dict[str, Any]] = []

    async def chat_postMessage(self, **kwargs: Any) -> dict[str, str]:
        self.posts.append(kwargs)
        return {"ts": f"1710000099.{len(self.posts):06d}"}

    # Not this file's subject, but a withdrawal ends a turn and an ending
    # writes its outcome onto the message that started it. Answered rather than
    # left missing so the connector is not swallowing an AttributeError and
    # logging a warning here. What the marks say is pinned in
    # test_slack_turn_marks.py.
    async def reactions_add(self, **kwargs: Any) -> dict[str, bool]:
        return {"ok": True}

    async def reactions_remove(self, **kwargs: Any) -> dict[str, bool]:
        return {"ok": True}

    async def chat_update(self, **kwargs: Any) -> dict[str, str]:
        self.updates.append(kwargs)
        return {"ts": kwargs.get("ts", "")}

    async def chat_postEphemeral(self, **kwargs: Any) -> dict[str, str]:
        self.ephemeral.append(kwargs)
        return {"ok": "true"}


class _RecordingAck:
    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self, *_args: Any, **_kwargs: Any) -> None:
        self.calls += 1


def _channel() -> tuple[SlackChannel, _RecordingSlackClient]:
    channel = SlackChannel(
        SlackChannelConfig(enabled=True, enable_streaming=True), RobotMessageRouter()
    )
    client = _RecordingSlackClient()
    channel._client = client
    channel._running = True
    return channel, client


def _question_message(
    *,
    request_id: str = "call_abc123",
    source: str = "ask_user_interrupt",
    question: str = "When should the digest run?",
    header: str = "Schedule",
    session_id: str = "slack_T1_C1_1710000000.000100",
    **extra: Any,
) -> Message:
    payload_question: dict[str, Any] = {
        "question": question,
        "header": header,
        "options": [],
    }
    payload_question.update(extra)
    return Message(
        id="ask-1",
        type="event",
        channel_id="slack",
        session_id=session_id,
        params={},
        timestamp=time.time(),
        ok=True,
        payload={
            "event_type": "chat.ask_user_question",
            "request_id": request_id,
            "source": source,
            "questions": [payload_question],
        },
        event_type=EventType.CHAT_ASK_USER_QUESTION,
        metadata={"slack_channel_id": "C1", "slack_thread_ts": "1710000000.000100"},
    )


def _blocks_of_type(blocks: list[dict[str, Any]], block_type: str) -> list[dict[str, Any]]:
    return [block for block in blocks if block.get("type") == block_type]


def _submit_button(blocks: list[dict[str, Any]]) -> dict[str, Any]:
    actions = _blocks_of_type(blocks, "actions")
    assert len(actions) == 1
    assert len(actions[0]["elements"]) == 1
    return actions[0]["elements"][0]


def _click(
    *,
    action_id: str = _SUBMIT_ACTION_ID,
    request_id: str = "call_abc123",
    source: str = "ask_user_interrupt",
    session_id: str = "slack_T1_C1_1710000000.000100",
    state_values: dict[str, Any] | None = None,
    blocks: list[dict[str, Any]] | None = None,
    user_id: str = "U1",
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Build the ``(body, action)`` Bolt hands a block_actions listener."""
    action: dict[str, Any] = {"type": "button", "action_id": action_id}
    if action_id == _SUBMIT_ACTION_ID:
        action["value"] = json.dumps(
            {
                "request_id": request_id,
                "index": -1,
                "value": "",
                "source": source,
                "session_id": session_id,
            },
            separators=(",", ":"),
        )
        action["text"] = {"type": "plain_text", "text": "Submit"}
    body = {
        "type": "block_actions",
        "user": {"id": user_id},
        "team": {"id": "T1"},
        "channel": {"id": "C1", "name": "general"},
        "container": {
            "type": "message",
            "channel_id": "C1",
            "message_ts": "1710000099.000001",
            "thread_ts": "1710000000.000100",
        },
        "message": {
            "ts": "1710000099.000001",
            "thread_ts": "1710000000.000100",
            "text": "When should the digest run?",
            "blocks": blocks if blocks is not None else [],
        },
        "state": {"values": state_values or {}},
        "actions": [action],
    }
    return body, action


def _state(**by_action_id: dict[str, Any]) -> dict[str, Any]:
    """``state.values`` keyed by a generated block id, as Slack sends it."""
    return {
        f"generated{index}": {action_id: entry}
        for index, (action_id, entry) in enumerate(by_action_id.items())
    }


def _datetime_state(
    date: str = "2026-08-20", at: str = "09:30", zone: str = "Europe/Paris"
) -> dict[str, Any]:
    return {
        "gen0": {f"{_INPUT_PREFIX}0.date": {"selected_date": date}},
        "gen1": {
            f"{_INPUT_PREFIX}0.time": {"selected_time": at, "timezone": zone}
        },
    }


async def _post_then(channel: SlackChannel, **click: Any) -> tuple[list[Message], _RecordingAck]:
    received: list[Message] = []
    channel.on_message(received.append)
    ack = _RecordingAck()
    body, action = _click(**click)
    await channel._handle_question_action(ack, body, action)
    return received, ack


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_plain_ask_user_query_posts_text_input_and_resumes_with_answer() -> None:
    channel, client = _channel()
    message = _question_message()
    message.payload = convert_interactions_to_ask_user_question(
        [
            {
                "id": "call_abc123",
                "value": {
                    "tool_name": "ask_user",
                    "tool_args": {"query": "What should the digest include?"},
                },
            }
        ]
    )

    await channel.send(message)

    assert len(client.posts) == 1
    blocks = client.posts[0]["blocks"]
    assert [block["type"] for block in blocks] == [
        "section",
        "divider",
        "input",
        "actions",
    ]
    assert blocks[2]["element"]["type"] == "plain_text_input"
    assert blocks[2]["element"]["multiline"] is True
    assert _submit_button(blocks)["action_id"] == _SUBMIT_ACTION_ID

    received, ack = await _post_then(
        channel,
        state_values=_state(**{f"{_INPUT_PREFIX}0": {"value": "Sales and support"}}),
        blocks=blocks,
    )

    assert ack.calls == 1
    assert len(received) == 1
    assert received[0].params["answers"] == [
        {
            "question": "What should the digest include?",
            "selected_options": ["Sales and support"],
            "custom_input": "",
        }
    ]
    assert received[0].params["request_id"] == "call_abc123"
    assert received[0].params["source"] == "ask_user_interrupt"
    assert received[0].req_method is ReqMethod.CHAT_SEND
    assert received[0].session_id == message.session_id
    assert channel._pending_questions == {}


@pytest.mark.asyncio
async def test_ask_user_without_options_declaration_uses_text_input() -> None:
    channel, client = _channel()
    message = _question_message()
    del message.payload["questions"][0]["options"]

    await channel.send(message)

    input_block = _blocks_of_type(client.posts[0]["blocks"], "input")[0]
    assert input_block["element"]["type"] == "plain_text_input"


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["permission_interrupt", "confirm_interrupt"])
async def test_other_empty_option_interrupts_still_post_notice(source: str) -> None:
    channel, client = _channel()

    await channel.send(_question_message(source=source))

    assert "cannot route" in client.posts[0]["text"]
    assert "blocks" not in client.posts[0]
    assert channel._pending_questions == {}


@pytest.mark.asyncio
async def test_ask_user_other_choice_is_not_replaced_with_text_input() -> None:
    channel, client = _channel()
    message = _question_message()
    message.payload["questions"][0]["options"] = [
        {"label": "Other", "description": "Custom input"}
    ]

    await channel.send(message)

    assert "cannot route" in client.posts[0]["text"]
    assert channel._pending_questions == {}


@pytest.mark.asyncio
async def test_a_datetime_question_posts_pickers_and_a_submit() -> None:
    channel, client = _channel()

    await channel.send(_question_message(inputs=[{"type": "datetime", "label": "Run at"}]))

    assert len(client.posts) == 1
    blocks = client.posts[0]["blocks"]
    assert [block["type"] for block in blocks] == [
        "section",
        "divider",
        "input",
        "input",
        "actions",
    ]
    # The question reads exactly as an option question's does.
    assert "*Schedule*" in blocks[0]["text"]["text"]
    assert [block["element"]["type"] for block in _blocks_of_type(blocks, "input")] == [
        "datepicker",
        "timepicker",
    ]
    # A picker has no commit point of its own, so the submit is what makes any
    # of it answerable -- and it holds the question's identity, as an option
    # button does, because the click has to say what it belongs to.
    submit = _submit_button(blocks)
    assert submit["action_id"] == _SUBMIT_ACTION_ID
    assert submit["text"]["text"] == "Submit"
    assert json.loads(submit["value"])["request_id"] == "call_abc123"
    assert json.loads(submit["value"])["source"] == "ask_user_interrupt"
    # The plain text is still supplied: it is all a mobile push notification
    # shows and all a screen reader reads.
    assert "When should the digest run?" in client.posts[0]["text"]


@pytest.mark.asyncio
async def test_the_posted_inputs_are_remembered_for_the_submit_that_reads_them() -> None:
    channel, _client = _channel()

    await channel.send(
        _question_message(inputs=[{"type": "date", "name": "starts"}, "text"])
    )

    pending = channel._pending_questions["call_abc123"]
    assert [entry.name for entry in pending.inputs] == ["starts", "text"]
    assert pending.question == "When should the digest run?"


@pytest.mark.asyncio
async def test_a_declared_submit_label_and_note_are_rendered() -> None:
    channel, client = _channel()

    await channel.send(
        _question_message(
            inputs=["date"], submit_label="Schedule it", note="Times are local."
        )
    )

    blocks = client.posts[0]["blocks"]
    assert _submit_button(blocks)["text"]["text"] == "Schedule it"
    context = _blocks_of_type(blocks, "context")
    assert [element["text"] for element in context[0]["elements"]] == [
        "Times are local."
    ]


@pytest.mark.asyncio
async def test_header_block_promotes_the_header_and_is_off_by_default() -> None:
    """Opt-in, because a header block is plain_text and clamps far sooner."""
    channel, client = _channel()

    await channel.send(_question_message(inputs=["date"]))
    await channel.send(
        _question_message(request_id="call_two", inputs=["date"], header_block=True)
    )

    assert client.posts[0]["blocks"][0]["type"] == "section"
    promoted = client.posts[1]["blocks"]
    assert promoted[0]["type"] == "header"
    assert promoted[0]["text"] == {
        "type": "plain_text",
        "text": "Schedule",
        "emoji": True,
    }
    assert promoted[1]["type"] == "section"


@pytest.mark.asyncio
async def test_a_prompt_derived_from_the_header_is_shown_once() -> None:
    """A question declaring inputs need not hold a sentence of its own.

    Its fields are labelled and the header names the group, so the prompt is
    derived from the header upstream. Rendering the derivation beneath its own
    source posts the title twice, in the message and in the notification.
    """
    channel, client = _channel()

    await channel.send(
        _question_message(question="Follow-up", header="Follow-up", inputs=["date"])
    )

    post = client.posts[0]
    assert post["blocks"][0]["text"]["text"] == "*Follow-up*"
    assert post["text"] == "Follow-up"


@pytest.mark.asyncio
async def test_a_question_that_differs_from_its_header_keeps_both() -> None:
    channel, client = _channel()

    await channel.send(_question_message(inputs=["date"]))

    post = client.posts[0]
    assert post["blocks"][0]["text"]["text"] == "*Schedule*\nWhen should the digest run?"
    assert post["text"] == "Schedule: When should the digest run?"


@pytest.mark.asyncio
async def test_header_block_is_available_to_an_option_question_too() -> None:
    channel, client = _channel()
    message = _question_message(header_block=True)
    message.payload["questions"][0]["options"] = [{"label": "Yes", "value": "yes"}]

    await channel.send(message)

    blocks = client.posts[0]["blocks"]
    assert blocks[0]["type"] == "header"
    assert _blocks_of_type(blocks, "actions")[0]["elements"][0]["text"]["text"] == "Yes"


@pytest.mark.asyncio
async def test_inputs_win_over_options_when_a_question_declares_both(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """They are alternatives: buttons beside the fields would offer a second answer."""
    channel, client = _channel()
    message = _question_message(inputs=["date"])
    message.payload["questions"][0]["options"] = [{"label": "Yes", "value": "yes"}]

    with caplog.at_level(logging.WARNING, logger=slack_connect.logger.name):
        await channel.send(message)

    blocks = client.posts[0]["blocks"]
    assert _submit_button(blocks)["action_id"] == _SUBMIT_ACTION_ID
    assert "options are not rendered" in caplog.text


@pytest.mark.asyncio
async def test_an_unrenderable_declaration_posts_the_unanswerable_notice(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The turn is stuck either way; the difference is whether anyone finds out why."""
    channel, client = _channel()

    with caplog.at_level(logging.WARNING, logger=slack_connect.logger.name):
        await channel.send(_question_message(inputs=[{"type": "colour_wheel"}]))

    assert "cannot render the inputs" in caplog.text
    assert "blocks" not in client.posts[0]
    assert "this channel cannot route back" in client.posts[0]["text"]
    assert channel._pending_questions == {}


# ---------------------------------------------------------------------------
# Intermediate change events
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_picker_change_is_acknowledged_and_otherwise_ignored() -> None:
    """Slack sends one per change and each holds the whole of state.values."""
    channel, client = _channel()
    await channel.send(_question_message(inputs=[{"type": "datetime"}]))

    received, ack = await _post_then(
        channel,
        action_id=f"{_INPUT_PREFIX}0.date",
        state_values=_datetime_state(),
    )

    assert ack.calls == 1
    assert received == []
    assert client.updates == []
    # Still waiting: a change is not an answer, and nothing has been consumed.
    assert "call_abc123" in channel._pending_questions


@pytest.mark.asyncio
async def test_the_listener_pattern_matches_every_action_id_this_posts() -> None:
    """An unmatched action id is never acked, which Slack shows as an error."""
    pattern = slack_connect._QUESTION_ACTION_ID_RE

    assert pattern.match("jiuwenswarm_answer:0")
    assert pattern.match("jiuwenswarm_answer:12")
    assert pattern.match(_SUBMIT_ACTION_ID)
    assert pattern.match(f"{_INPUT_PREFIX}0")
    assert pattern.match(f"{_INPUT_PREFIX}3.date")
    assert pattern.match(f"{_INPUT_PREFIX}3.time")
    assert not pattern.match("jiuwenswarm_answer:")
    assert not pattern.match("someone_elses_button")


# ---------------------------------------------------------------------------
# Submitting
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_submit_answers_with_one_iso_instant_carrying_its_zone() -> None:
    channel, _client = _channel()
    await channel.send(_question_message(inputs=[{"type": "datetime"}]))

    received, ack = await _post_then(channel, state_values=_datetime_state())

    assert ack.calls == 1
    assert len(received) == 1
    answer = received[0]
    assert answer.params["answers"] == [
        {
            "question": "When should the digest run?",
            "selected_options": ["2026-08-20T09:30:00+02:00[Europe/Paris]"],
            "custom_input": "",
        }
    ]
    # Delivered exactly where a pressed button's answer is delivered, and as the
    # same kind of resume.
    assert answer.req_method is ReqMethod.CHAT_SEND
    assert answer.is_stream is True
    assert answer.session_id == "slack_T1_C1_1710000000.000100"
    assert answer.params["request_id"] == "call_abc123"
    assert channel._pending_questions == {}


@pytest.mark.asyncio
async def test_a_time_with_no_zone_reported_still_answers_without_inventing_one() -> None:
    channel, _client = _channel()
    await channel.send(_question_message(inputs=["time"]))

    received, _ack = await _post_then(
        channel,
        state_values=_state(**{f"{_INPUT_PREFIX}0": {"selected_time": "09:30"}}),
    )

    assert received[0].params["answers"][0]["selected_options"] == ["09:30:00"]


@pytest.mark.asyncio
async def test_several_inputs_answer_with_one_json_object() -> None:
    channel, _client = _channel()
    await channel.send(
        _question_message(
            inputs=[
                {"type": "date", "name": "starts"},
                {"type": "channel", "name": "into"},
            ]
        )
    )

    received, _ack = await _post_then(
        channel,
        state_values=_state(
            **{
                f"{_INPUT_PREFIX}0": {"selected_date": "2026-08-20"},
                f"{_INPUT_PREFIX}1": {"selected_channel": "C0DIGEST99"},
            }
        ),
    )

    selected = received[0].params["answers"][0]["selected_options"]
    assert len(selected) == 1
    assert json.loads(selected[0]) == {
        "starts": "2026-08-20",
        "into": "C0DIGEST99",
    }


@pytest.mark.asyncio
async def test_a_standalone_approval_with_inputs_uses_chat_user_answer() -> None:
    """The source decides how the answer travels; the elements do not."""
    channel, _client = _channel()
    await channel.send(
        _question_message(
            request_id="skill_evolve_1",
            source="skill_evolution_approval",
            inputs=["date"],
        )
    )

    received, _ack = await _post_then(
        channel,
        request_id="skill_evolve_1",
        source="skill_evolution_approval",
        state_values=_state(**{f"{_INPUT_PREFIX}0": {"selected_date": "2026-08-20"}}),
    )

    assert received[0].req_method is ReqMethod.CHAT_ANSWER
    assert received[0].is_stream is False


# ---------------------------------------------------------------------------
# Submits that are not answers
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_submit_with_nothing_picked_leaves_the_question_waiting() -> None:
    """Claiming it would leave the turn paused behind a message nobody can finish."""
    channel, client = _channel()
    await channel.send(_question_message(inputs=[{"type": "datetime", "label": "Run at"}]))

    received, ack = await _post_then(channel, state_values={})

    assert ack.calls == 1
    assert received == []
    # Still pending and still on screen: the fields are there to be filled in.
    assert "call_abc123" in channel._pending_questions
    assert client.updates == []
    # And the person who pressed it is told why, and only them.
    assert len(client.ephemeral) == 1
    assert client.ephemeral[0]["user"] == "U1"
    assert client.ephemeral[0]["channel"] == "C1"
    assert "Run at" in client.ephemeral[0]["text"]


@pytest.mark.asyncio
async def test_a_submit_with_half_a_datetime_says_which_half(
    caplog: pytest.LogCaptureFixture,
) -> None:
    channel, client = _channel()
    await channel.send(_question_message(inputs=[{"type": "datetime", "label": "Run at"}]))

    with caplog.at_level(logging.INFO, logger=slack_connect.logger.name):
        received, _ack = await _post_then(
            channel,
            state_values={"gen0": {f"{_INPUT_PREFIX}0.date": {"selected_date": "2026-08-20"}}},
        )

    assert received == []
    assert "no time was picked" in client.ephemeral[0]["text"]
    assert "submit refused as incomplete" in caplog.text


@pytest.mark.asyncio
async def test_a_submit_carrying_a_malformed_value_is_refused() -> None:
    """Slack promises YYYY-MM-DD; anyone can build a client that does not send it."""
    channel, client = _channel()
    await channel.send(_question_message(inputs=[{"type": "date", "label": "Run on"}]))

    received, _ack = await _post_then(
        channel,
        state_values=_state(**{f"{_INPUT_PREFIX}0": {"selected_date": "20/08/2026"}}),
    )

    assert received == []
    assert "call_abc123" in channel._pending_questions
    assert "YYYY-MM-DD" in client.ephemeral[0]["text"]


@pytest.mark.asyncio
async def test_an_optional_input_may_be_left_blank() -> None:
    channel, client = _channel()
    await channel.send(
        _question_message(
            inputs=[
                {"type": "date", "name": "starts"},
                {"type": "text", "name": "note", "optional": True},
            ]
        )
    )

    received, _ack = await _post_then(
        channel,
        state_values=_state(**{f"{_INPUT_PREFIX}0": {"selected_date": "2026-08-20"}}),
    )

    assert client.ephemeral == []
    assert json.loads(received[0].params["answers"][0]["selected_options"][0]) == {
        "starts": "2026-08-20"
    }


@pytest.mark.asyncio
async def test_a_refused_submit_that_cannot_be_reported_still_refuses() -> None:
    """The explanation is a courtesy; the refusal is not."""
    channel, client = _channel()
    await channel.send(_question_message(inputs=["date"]))

    async def _fail(**_kwargs: Any) -> None:
        raise RuntimeError("channel_not_found")

    client.chat_postEphemeral = _fail  # type: ignore[assignment]

    received, _ack = await _post_then(channel, state_values={})

    assert received == []
    assert "call_abc123" in channel._pending_questions


# ---------------------------------------------------------------------------
# Retirement, staleness and timeout, inherited unchanged
# ---------------------------------------------------------------------------


async def _ordinary_message(channel: SlackChannel) -> None:
    """Deliver a plain message, which is what withdraws a pending interrupt."""
    await channel._handle_slack_event(
        {
            "type": "message",
            "user": "U1",
            "channel": "C1",
            "ts": "1710000200.000300",
            "thread_ts": "1710000000.000100",
            "text": "never mind, look at this instead",
        },
        {"team_id": "T1", "event_id": "Ev1710000200.000300"},
        is_dm=False,
        trigger="mention",
    )


@pytest.mark.asyncio
async def test_withdrawal_takes_the_pickers_away_exactly_as_it_takes_buttons() -> None:
    """chat.update rewriting the blocks is block-type agnostic, so this is free."""
    channel, client = _channel()
    await channel.send(_question_message(inputs=[{"type": "datetime"}]))

    await _ordinary_message(channel)

    entry = channel._pending_questions["call_abc123"]
    assert entry.withdrawn_at is not None
    rewrite = client.updates[-1]
    assert rewrite["ts"] == "1710000099.000001"
    # No blocks at all: no pickers, no submit, and the question survives as text.
    assert rewrite["blocks"] == []
    assert "no longer waiting for an answer" in rewrite["text"]
    assert "When should the digest run?" in rewrite["text"]


@pytest.mark.asyncio
async def test_a_submit_on_a_withdrawn_question_is_dropped(
    caplog: pytest.LogCaptureFixture,
) -> None:
    channel, _client = _channel()
    await channel.send(_question_message(inputs=[{"type": "datetime"}]))
    await _ordinary_message(channel)

    with caplog.at_level(logging.WARNING, logger=slack_connect.logger.name):
        received, ack = await _post_then(channel, state_values=_datetime_state())

    assert ack.calls == 1
    assert received == []
    assert "withdrawn 0s earlier" in caplog.text
    # Marked rather than dropped, so a second late submit is recognised too.
    assert channel._pending_questions["call_abc123"].withdrawn_at is not None


@pytest.mark.asyncio
async def test_a_picker_change_after_withdrawal_is_still_only_acknowledged() -> None:
    channel, _client = _channel()
    await channel.send(_question_message(inputs=[{"type": "datetime"}]))
    await _ordinary_message(channel)

    received, ack = await _post_then(
        channel, action_id=f"{_INPUT_PREFIX}0.time", state_values=_datetime_state()
    )

    assert ack.calls == 1
    assert received == []


@pytest.mark.asyncio
async def test_a_submit_for_a_question_this_process_never_posted_answers_nothing() -> None:
    channel, _client = _channel()

    received, ack = await _post_then(channel, state_values=_datetime_state())

    assert ack.calls == 1
    assert received == []


@pytest.mark.asyncio
async def test_answering_rewrites_the_message_without_its_inputs_or_its_submit() -> None:
    """An input block is not an actions block, and both have to go."""
    channel, client = _channel()
    await channel.send(_question_message(inputs=[{"type": "datetime"}]))
    posted = client.posts[0]["blocks"]

    await _post_then(channel, blocks=posted, state_values=_datetime_state())

    rewrite = client.updates[-1]
    assert [block["type"] for block in rewrite["blocks"]] == [
        "section",
        "divider",
        "context",
    ]
    assert "Answered by <@U1>" in rewrite["blocks"][-1]["elements"][0]["text"]
    assert "2026-08-20T09:30:00+02:00[Europe/Paris]" in (
        rewrite["blocks"][-1]["elements"][0]["text"]
    )


@pytest.mark.asyncio
async def test_a_second_submit_answers_nothing() -> None:
    channel, _client = _channel()
    await channel.send(_question_message(inputs=[{"type": "datetime"}]))

    received: list[Message] = []
    channel.on_message(received.append)
    body, action = _click(state_values=_datetime_state())
    await channel._handle_question_action(_RecordingAck(), body, action)
    await channel._handle_question_action(_RecordingAck(), body, action)

    assert len(received) == 1


@pytest.mark.asyncio
async def test_a_user_outside_the_allow_list_cannot_submit() -> None:
    channel = SlackChannel(
        SlackChannelConfig(enabled=True, enable_streaming=True, allow_from=["U9"]),
        RobotMessageRouter(),
    )
    channel._client = _RecordingSlackClient()
    channel._running = True
    await channel.send(_question_message(inputs=[{"type": "datetime"}]))

    received, _ack = await _post_then(
        channel, user_id="U1", state_values=_datetime_state()
    )

    assert received == []
    assert "call_abc123" in channel._pending_questions


@pytest.mark.asyncio
async def test_a_submit_logs_the_shape_of_what_arrived_and_not_its_contents(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The one thing a real click confirms is that Slack's payload matches the table."""
    channel, _client = _channel()
    await channel.send(_question_message(inputs=[{"type": "text", "name": "note"}]))

    with caplog.at_level(logging.DEBUG, logger=slack_connect.logger.name):
        await _post_then(
            channel,
            state_values=_state(**{f"{_INPUT_PREFIX}0": {"value": "a secret"}}),
        )

    assert f"{_INPUT_PREFIX}0={{value:str}}" in caplog.text
    # The shape, never the value: this line must be safe to leave on.
    assert "a secret" not in caplog.text


@pytest.mark.asyncio
async def test_a_submit_against_an_option_question_answers_nothing(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Otherwise the shared resolver would answer with the word on the button."""
    channel, _client = _channel()
    message = _question_message()
    message.payload["questions"][0]["options"] = [{"label": "Yes", "value": "yes"}]
    await channel.send(message)

    with caplog.at_level(logging.WARNING, logger=slack_connect.logger.name):
        received, ack = await _post_then(channel, state_values={})

    assert ack.calls == 1
    assert received == []
    assert "nothing to read" in caplog.text
    assert "call_abc123" in channel._pending_questions


# ---------------------------------------------------------------------------
# A call holding several questions
# ---------------------------------------------------------------------------


def _three_question_message() -> Message:
    """The shape observed in production: three questions, one input each."""
    message = _question_message(inputs=[{"type": "date", "label": "Date", "name": "date"}])
    message.payload["questions"].extend(
        [
            {
                "question": "Pick a time",
                "header": "Time",
                "options": [],
                "inputs": [{"type": "time", "label": "Time", "name": "time"}],
            },
            {
                "question": "How many people are coming?",
                "header": "People",
                "options": [],
                "inputs": [{"type": "text", "label": "People", "name": "people"}],
            },
        ]
    )
    return message


@pytest.mark.asyncio
async def test_the_questions_this_message_does_not_hold_are_named_on_it() -> None:
    """A dropped question that nothing names is a question nobody can recover."""
    channel, client = _channel()

    await channel.send(_three_question_message())

    assert len(client.posts) == 1
    contexts = _blocks_of_type(client.posts[0]["blocks"], "context")
    assert len(contexts) == 1
    note = contexts[0]["elements"][0]["text"]
    assert "Pick a time" in note
    assert "How many people are coming?" in note
    # The count as well as the names, so the note stands on its own.
    assert "2" in note


@pytest.mark.asyncio
async def test_the_dropped_questions_are_named_in_the_log(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A count alone cannot be matched against what the model says it asked."""
    channel, _client = _channel()

    with caplog.at_level(logging.WARNING, logger=slack_connect.logger.name):
        await channel.send(_three_question_message())

    # The prompts and the request id, rather than the sentence around them. An
    # operator grepping for one of these needs the question text and the id.
    assert "Pick a time" in caplog.text
    assert "How many people are coming?" in caplog.text
    assert "call_abc123" in caplog.text
    assert [record.levelname for record in caplog.records] == ["WARNING"]


@pytest.mark.asyncio
async def test_a_single_question_gets_no_note_about_dropped_ones() -> None:
    """The note must not appear on the shape every ordinary call has."""
    channel, client = _channel()

    await channel.send(_question_message(inputs=[{"type": "text", "name": "note"}]))

    assert _blocks_of_type(client.posts[0]["blocks"], "context") == []
