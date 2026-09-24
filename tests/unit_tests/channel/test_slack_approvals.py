"""Unit tests for rendering ask-user questions as Slack Block Kit buttons."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

import pytest

from jiuwenswarm.agents.harness.common.rails.interrupt.interrupt_helpers import (
    _default_interrupt_options,
)
from jiuwenswarm.agents.harness.common.rails.interrupt.permission_options import (
    ALLOW_ONCE,
    ALWAYS_ALLOW,
    REJECT,
    SESSION_ALLOW,
)
from jiuwenswarm.common.schema.message import EventType, Message, ReqMethod
from jiuwenswarm.gateway.channel_manager.base import RobotMessageRouter
from jiuwenswarm.gateway.channel_manager.im_platforms.slack import slack_connect
from jiuwenswarm.gateway.channel_manager.im_platforms.slack.slack_connect import (
    SlackChannel,
    SlackChannelConfig,
    SlackDeliveryError,
)


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


_DEFAULT: Any = object()


class _RecordingSlackClient:
    """Records every posted and updated message."""

    def __init__(self) -> None:
        self.posts: list[dict[str, Any]] = []
        self.updates: list[dict[str, Any]] = []
        self.fail_post = False

    async def chat_postMessage(self, **kwargs: Any) -> dict[str, str]:
        self.posts.append(kwargs)
        if self.fail_post:
            raise RuntimeError("channel_not_found")
        return {"ts": f"1710000099.{len(self.posts):06d}"}

    async def chat_update(self, **kwargs: Any) -> dict[str, str]:
        self.updates.append(kwargs)
        return {"ts": kwargs.get("ts", "")}


def _channel(**overrides: Any) -> tuple[SlackChannel, _RecordingSlackClient]:
    config = SlackChannelConfig(enabled=True, enable_streaming=True, **overrides)
    channel = SlackChannel(config, RobotMessageRouter())
    client = _RecordingSlackClient()
    channel._client = client
    channel._running = True
    return channel, client


def _question_message(
    *,
    request_id: str = "call_abc123",
    source: str = "permission_interrupt",
    question: str = "Tool `bash` needs permission to run",
    header: str = "Permission",
    options: list[dict[str, Any]] | None = None,
    session_id: str = "slack_T1_C1_1710000000.000100",
    metadata: dict[str, Any] | None = _DEFAULT,
) -> Message:
    if metadata is _DEFAULT:
        metadata = {
            "slack_channel_id": "C1",
            "slack_thread_ts": "1710000000.000100",
        }
    if options is None:
        options = [
            {"label": "Approve", "value": "approve", "description": "Run it once"},
            {"label": "Reject", "value": "reject"},
        ]
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
            "questions": [
                {
                    "question": question,
                    "header": header,
                    "options": options,
                    "multi_select": False,
                }
            ],
        },
        event_type=EventType.CHAT_ASK_USER_QUESTION,
        metadata=metadata,
    )


def _buttons(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        element
        for block in blocks
        if block.get("type") == "actions"
        for element in block["elements"]
    ]


def _styles(blocks: list[dict[str, Any]]) -> dict[str, str | None]:
    """Each button's label mapped to the style it was sent with, ``None`` for none.

    ``None`` rather than "" so that a button holding no ``style`` key is not
    confused with one holding an empty value, which Slack would refuse.
    """
    return {
        button["text"]["text"]: button.get("style") for button in _buttons(blocks)
    }


def _legend(blocks: list[dict[str, Any]]) -> str | None:
    """The options' descriptions, or ``None`` when no block holds them.

    The first section is the question; the legend follows it when any option
    had a description to show.
    """
    sections = [block for block in blocks if block.get("type") == "section"]
    if len(sections) < 2:
        return None
    return sections[1]["text"]["text"]


@pytest.mark.asyncio
async def test_question_with_options_renders_buttons_carrying_the_request_id() -> None:
    channel, client = _channel()

    await channel.send(_question_message())

    assert len(client.posts) == 1
    post = client.posts[0]
    assert post["channel"] == "C1"
    assert post["thread_ts"] == "1710000000.000100"
    # The plain text is still supplied: it is all a mobile push notification
    # shows, and all a screen reader reads.
    assert "needs permission to run" in post["text"]

    blocks = post["blocks"]
    assert blocks[0]["type"] == "section"
    assert "*Permission*" in blocks[0]["text"]["text"]

    buttons = _buttons(blocks)
    assert [button["text"]["text"] for button in buttons] == ["Approve", "Reject"]
    assert [button["action_id"] for button in buttons] == [
        "jiuwenswarm_answer:0",
        "jiuwenswarm_answer:1",
    ]
    for index, button in enumerate(buttons):
        value = json.loads(button["value"])
        assert value["request_id"] == "call_abc123"
        assert value["index"] == index
        assert value["source"] == "permission_interrupt"
        assert value["session_id"] == "slack_T1_C1_1710000000.000100"
    assert json.loads(buttons[0]["value"])["value"] == "approve"
    assert json.loads(buttons[1]["value"])["value"] == "reject"


@pytest.mark.asyncio
async def test_posted_question_is_remembered_for_the_click_that_answers_it() -> None:
    channel, _client = _channel()

    await channel.send(_question_message())

    pending = channel._pending_questions["call_abc123"]
    assert pending.session_id == "slack_T1_C1_1710000000.000100"
    assert pending.source == "permission_interrupt"
    assert pending.question == "Tool `bash` needs permission to run"
    assert pending.labels == ["Approve", "Reject"]
    assert pending.values == ["approve", "reject"]


@pytest.mark.asyncio
async def test_option_without_a_value_answers_with_its_label() -> None:
    channel, client = _channel()

    await channel.send(
        _question_message(options=[{"label": "Ship it"}, {"label": "Hold"}])
    )

    buttons = _buttons(client.posts[0]["blocks"])
    assert [json.loads(button["value"])["value"] for button in buttons] == [
        "Ship it",
        "Hold",
    ]


@pytest.mark.asyncio
async def test_free_form_option_is_not_rendered_as_a_button() -> None:
    """"Other" offers a text box on a rich client and answers nothing here."""
    channel, client = _channel()

    await channel.send(
        _question_message(
            options=[
                {"label": "Approve", "value": "approve"},
                {"label": "Other", "description": "Custom input"},
            ]
        )
    )

    buttons = _buttons(client.posts[0]["blocks"])
    assert [button["text"]["text"] for button in buttons] == ["Approve"]
    # It has a description, but it has no button, and a line describing a choice
    # that is not offered would read as one that is.
    assert _legend(client.posts[0]["blocks"]) is None


@pytest.mark.asyncio
async def test_every_default_approval_option_is_described() -> None:
    """The four an approval offers read alike; the descriptions separate them.

    Driven off the builder rather than a copy of its text, which keeps this
    honest when the wording or its language changes.
    """
    options = _default_interrupt_options()
    channel, client = _channel()

    await channel.send(_question_message(options=options))

    legend = _legend(client.posts[0]["blocks"])
    assert legend is not None
    for option in options:
        assert option["description"] in legend
        assert option["label"] in legend
    # One line per option, and the option that writes a rule to disk is
    # distinguishable from the one that does not.
    assert len(legend.splitlines()) == len(options)
    assert len(set(legend.splitlines())) == len(options)


@pytest.mark.asyncio
async def test_an_option_without_a_description_is_left_out_of_the_legend() -> None:
    """Not an empty bullet and not a dangling separator: no line at all."""
    channel, client = _channel()

    await channel.send(
        _question_message(
            options=[
                {"label": "Approve", "value": "approve", "description": "Run it once"},
                {"label": "Reject", "value": "reject"},
            ]
        )
    )

    legend = _legend(client.posts[0]["blocks"])
    assert legend == "*Approve* — Run it once"
    assert "Reject" not in legend
    # The button is still offered; only its description is absent.
    buttons = _buttons(client.posts[0]["blocks"])
    assert [button["text"]["text"] for button in buttons] == ["Approve", "Reject"]


@pytest.mark.asyncio
async def test_options_without_descriptions_post_no_legend_block() -> None:
    channel, client = _channel()

    await channel.send(
        _question_message(options=[{"label": "Ship it"}, {"label": "Hold"}])
    )

    blocks = client.posts[0]["blocks"]
    assert [block["type"] for block in blocks] == ["section", "actions"]
    assert _legend(blocks) is None


@pytest.mark.asyncio
async def test_the_legend_does_not_change_what_a_button_answers_with() -> None:
    """The descriptions are rendered beside the buttons, never encoded into one."""
    channel, client = _channel()

    await channel.send(_question_message())

    buttons = _buttons(client.posts[0]["blocks"])
    assert [button["action_id"] for button in buttons] == [
        "jiuwenswarm_answer:0",
        "jiuwenswarm_answer:1",
    ]
    for index, button in enumerate(buttons):
        decoded = json.loads(button["value"])
        assert decoded == {
            "request_id": "call_abc123",
            "index": index,
            "value": ["approve", "reject"][index],
            "source": "permission_interrupt",
            "session_id": "slack_T1_C1_1710000000.000100",
        }


@pytest.mark.asyncio
async def test_the_legend_does_not_clamp_the_question_any_sooner() -> None:
    """The legend is its own block, so it does not spend the question's budget."""
    options = _default_interrupt_options()
    channel, client = _channel()

    await channel.send(
        _question_message(question="q" * 5000, header="", options=options)
    )

    blocks = client.posts[0]["blocks"]
    # Clamped exactly as it is without a legend, rather than sooner to make room.
    assert len(blocks[0]["text"]["text"]) == 3000
    legend = _legend(blocks)
    assert legend is not None
    for option in options:
        assert option["description"] in legend


@pytest.mark.asyncio
async def test_a_long_description_is_clamped_without_crowding_out_the_others() -> None:
    channel, client = _channel()

    await channel.send(
        _question_message(
            options=[
                {"label": "First", "value": "a", "description": "d" * 5000},
                {"label": "Second", "value": "b", "description": "kept"},
            ]
        )
    )

    legend = _legend(client.posts[0]["blocks"])
    first, second = legend.splitlines()
    assert first.endswith("…")
    assert len(first) <= len("*First* — ") + 200
    assert second == "*Second* — kept"
    assert len(legend) <= 3000


@pytest.mark.asyncio
async def test_each_permission_action_is_styled_by_what_it_does() -> None:
    """The four canonical actions, each holding the style its effect earns."""
    channel, client = _channel()

    await channel.send(
        _question_message(
            options=[
                {"label": "Allow once", "value": ALLOW_ONCE},
                {"label": "Session allow", "value": SESSION_ALLOW},
                {"label": "Always allow", "value": ALWAYS_ALLOW},
                {"label": "Reject", "value": REJECT},
            ]
        )
    )

    assert _styles(client.posts[0]["blocks"]) == {
        "Allow once": "primary",
        # Allowing every later call of the same kind is not the narrow yes the
        # affirmative style is meant to point at, so neither takes it.
        "Session allow": None,
        "Always allow": None,
        "Reject": "danger",
    }


@pytest.mark.asyncio
async def test_a_permission_prompt_affirms_at_most_one_button() -> None:
    """Slack's guidance is one "primary" per set; three allows must not take it.

    Driven off the builder rather than a copy of its options, so that adding a
    fifth way to say yes cannot quietly turn the styling back into noise.
    """
    channel, client = _channel()

    await channel.send(_question_message(options=_default_interrupt_options()))

    styles = list(_styles(client.posts[0]["blocks"]).values())
    assert styles.count("primary") == 1
    assert styles.count("danger") == 1


@pytest.mark.asyncio
async def test_localized_options_are_styled_like_their_english_names() -> None:
    """The vocabulary is language independent, and reading it is the point.

    A localized deployment sends the Chinese labels, and several builders send
    them as the answer value too. Matching English literals would leave every
    one of those prompts unstyled while every English test still passed.
    """
    channel, client = _channel()

    await channel.send(
        _question_message(
            options=[
                {"label": "本次允许", "description": "仅本次授权执行"},
                {"label": "会话内记住"},
                {"label": "永久记住"},
                {"label": "拒绝", "description": "拒绝执行此工具"},
            ]
        )
    )

    assert _styles(client.posts[0]["blocks"]) == {
        "本次允许": "primary",
        "会话内记住": None,
        "永久记住": None,
        "拒绝": "danger",
    }


@pytest.mark.asyncio
async def test_plan_approval_options_are_styled_from_the_same_vocabulary() -> None:
    """Plan approval shares the alias table, so it is styled without extra work."""
    channel, client = _channel()

    await channel.send(
        _question_message(
            source="confirm_interrupt",
            options=[
                {"label": "批准", "value": "approve"},
                {"label": "拒绝", "value": "reject"},
            ],
        )
    )

    assert _styles(client.posts[0]["blocks"]) == {"批准": "primary", "拒绝": "danger"}


@pytest.mark.asyncio
async def test_an_unrecognized_value_is_left_unstyled() -> None:
    """Unreadable is not destructive.

    An answer the agent cannot resolve is treated as a rejection there, but
    inferring the same here would paint a red button the moment the vocabulary
    drifted, dressing a rendering gap as a deliberate choice.
    """
    channel, client = _channel()

    await channel.send(
        _question_message(
            options=[
                {"label": "Allow once", "value": ALLOW_ONCE},
                {"label": "Escalate", "value": "escalate_to_reviewer"},
            ]
        )
    )

    assert _styles(client.posts[0]["blocks"]) == {
        "Allow once": "primary",
        "Escalate": None,
    }


@pytest.mark.asyncio
async def test_a_model_authored_question_is_left_unstyled() -> None:
    """Its options are not permissions, and none of them is safe or destructive."""
    channel, client = _channel()

    await channel.send(
        _question_message(
            source="ask_user_interrupt",
            question="Which database should the migration target?",
            options=[
                {"label": "Postgres", "value": "postgres"},
                {"label": "SQLite", "value": "sqlite"},
                {"label": "Neither, stop here", "value": "stop"},
            ],
        )
    )

    assert set(_styles(client.posts[0]["blocks"]).values()) == {None}


@pytest.mark.asyncio
async def test_an_intent_stated_on_the_option_wins_over_its_value() -> None:
    """The branch nothing populates yet, kept working so it needs no rework.

    The shared question builder rebuilds every option from a fixed set of keys,
    so no option reaches a connector with an intent of its own today. When one
    does it is an action name, not a Slack style, and it is read through the
    same vocabulary the value is.
    """
    channel, client = _channel()

    await channel.send(
        _question_message(
            options=[
                {"label": "Run it", "value": "run_the_tool", "intent": ALLOW_ONCE},
                {"label": "Stop", "value": "halt", "intent": "拒绝"},
                {"label": "Later", "value": "later", "intent": "unknown_action"},
            ]
        )
    )

    assert _styles(client.posts[0]["blocks"]) == {
        "Run it": "primary",
        "Stop": "danger",
        # An intent that resolves to nothing falls through to the value, which
        # resolves to nothing either: unrecognized twice is still no style.
        "Later": None,
    }


@pytest.mark.asyncio
async def test_question_without_options_posts_a_notice_instead_of_buttons(
    caplog: pytest.LogCaptureFixture,
) -> None:
    channel, client = _channel()

    with caplog.at_level(logging.WARNING, logger=slack_connect.logger.name):
        await channel.send(_question_message(options=[]))

    # Posted, but as a notice: no buttons, and nothing registered to answer.
    assert len(client.posts) == 1
    assert "blocks" not in client.posts[0] or not client.posts[0].get("blocks")
    assert "cannot route" in client.posts[0]["text"]
    assert channel._pending_questions == {}
    assert "free-form" in caplog.text


@pytest.mark.asyncio
async def test_notice_carries_the_question_so_it_can_be_answered_elsewhere() -> None:
    channel, client = _channel()

    await channel.send(_question_message(options=[]))

    # The point of the notice is that someone can act on it, which needs the
    # question itself and not just the fact that one exists.
    assert "needs permission to run" in client.posts[0]["text"]


@pytest.mark.asyncio
async def test_question_whose_only_option_is_free_form_posts_a_notice() -> None:
    channel, client = _channel()

    await channel.send(
        _question_message(options=[{"label": "Other", "description": "Custom input"}])
    )

    assert len(client.posts) == 1
    assert "cannot route" in client.posts[0]["text"]


@pytest.mark.asyncio
async def test_a_notice_that_fails_to_post_does_not_raise() -> None:
    # The turn is not waiting on the notice, and the question was unanswerable
    # before the post failed. Raising would report the wrong cause.
    channel, client = _channel()
    client.fail_post = True

    await channel.send(_question_message(options=[]))


@pytest.mark.asyncio
async def test_question_without_a_request_id_posts_nothing() -> None:
    channel, client = _channel()

    await channel.send(_question_message(request_id=""))

    assert client.posts == []


@pytest.mark.asyncio
async def test_button_label_is_clamped_to_the_block_kit_limit() -> None:
    channel, client = _channel()

    await channel.send(
        _question_message(options=[{"label": "A" * 200, "value": "approve"}])
    )

    button = _buttons(client.posts[0]["blocks"])[0]
    assert len(button["text"]["text"]) == 75
    assert button["text"]["text"].endswith("…")
    # The clamp is presentation only; the answer keeps the full value.
    assert json.loads(button["value"])["value"] == "approve"


@pytest.mark.asyncio
async def test_button_value_stays_within_two_thousand_characters() -> None:
    """A long option value is shed rather than overflowing the field."""
    channel, client = _channel()

    await channel.send(
        _question_message(options=[{"label": "Approve", "value": "v" * 4000}])
    )

    button = _buttons(client.posts[0]["blocks"])[0]
    assert len(button["value"]) <= 2000
    decoded = json.loads(button["value"])
    # Identity survives; the oversized field is what goes.
    assert decoded["request_id"] == "call_abc123"
    assert decoded["index"] == 0
    assert "value" not in decoded
    # The value the agent is answered with is kept out of band instead.
    assert channel._pending_questions["call_abc123"].values == ["v" * 4000]


def test_button_value_sheds_optional_fields_in_order() -> None:
    encode = SlackChannel._encode_button_value

    full = json.loads(
        encode(
            request_id="r1",
            index=0,
            value="approve",
            source="permission_interrupt",
            session_id="slack_T1_C1_1",
        )
    )
    assert set(full) == {"request_id", "index", "value", "source", "session_id"}

    without_value = json.loads(
        encode(
            request_id="r1",
            index=0,
            value="v" * 1990,
            source="permission_interrupt",
            session_id="slack_T1_C1_1",
        )
    )
    assert set(without_value) == {"request_id", "index", "source", "session_id"}

    identity_only = json.loads(
        encode(
            request_id="r1",
            index=0,
            value="v" * 1990,
            source="s" * 1990,
            session_id="x" * 1990,
        )
    )
    assert set(identity_only) == {"request_id", "index"}

    # Nothing left to shed: an identifier that cannot itself fit is refused.
    assert (
        encode(
            request_id="r" * 3000,
            index=0,
            value="approve",
            source="permission_interrupt",
            session_id="slack_T1_C1_1",
        )
        == ""
    )


@pytest.mark.asyncio
async def test_unrenderable_question_raises_rather_than_posting_a_broken_message() -> None:
    channel, client = _channel()

    with pytest.raises(SlackDeliveryError):
        await channel.send(
            _question_message(
                request_id="r" * 3000,
                options=[{"label": "Approve", "value": "approve"}],
            )
        )

    assert client.posts == []


@pytest.mark.asyncio
async def test_failed_question_post_raises_a_delivery_error() -> None:
    channel, _client = _channel()

    class _FailingClient:
        async def chat_postMessage(self, **_kwargs: Any) -> dict[str, str]:
            raise RuntimeError("channel_not_found")

    channel._client = _FailingClient()

    with pytest.raises(SlackDeliveryError) as excinfo:
        await channel.send(_question_message())

    assert "channel_not_found" in str(excinfo.value)
    # Nothing is left waiting for a click on a message that was never posted.
    assert channel._pending_questions == {}


@pytest.mark.asyncio
async def test_question_raises_when_the_channel_has_no_client() -> None:
    channel, _client = _channel()
    channel._client = None

    with pytest.raises(SlackDeliveryError):
        await channel.send(_question_message())


@pytest.mark.asyncio
async def test_question_raises_when_no_target_channel_resolves() -> None:
    channel, client = _channel()

    with pytest.raises(SlackDeliveryError):
        await channel.send(_question_message(metadata={}, session_id="web-session"))

    assert client.posts == []


@pytest.mark.asyncio
async def test_disconnected_channel_stays_quiet_for_an_unanswerable_question() -> None:
    """A no-op case must not raise just because the channel is stopped."""
    channel, _client = _channel()
    channel._client = None

    await channel.send(_question_message(options=[]))


@pytest.mark.asyncio
async def test_question_body_is_converted_to_slack_mrkdwn() -> None:
    channel, client = _channel()

    await channel.send(
        _question_message(question="**Tool** wants to run\n- first\n- second")
    )

    rendered = client.posts[0]["blocks"][0]["text"]["text"]
    assert "*Tool* wants to run" in rendered
    assert "• first" in rendered


@pytest.mark.asyncio
async def test_long_question_body_is_clamped_to_the_section_limit() -> None:
    channel, client = _channel()

    await channel.send(_question_message(question="q" * 5000, header=""))

    assert len(client.posts[0]["blocks"][0]["text"]["text"]) == 3000


@pytest.mark.asyncio
async def test_many_options_are_split_across_action_blocks() -> None:
    channel, client = _channel()

    await channel.send(
        _question_message(
            options=[{"label": f"opt{index}"} for index in range(30)]
        )
    )

    action_blocks = [
        block for block in client.posts[0]["blocks"] if block["type"] == "actions"
    ]
    assert [len(block["elements"]) for block in action_blocks] == [25, 5]


@pytest.mark.asyncio
async def test_only_the_first_question_is_rendered(
    caplog: pytest.LogCaptureFixture,
) -> None:
    channel, client = _channel()
    message = _question_message()
    message.payload["questions"].append(
        {"question": "second", "header": "", "options": [{"label": "Yes"}]}
    )

    with caplog.at_level(logging.WARNING, logger=slack_connect.logger.name):
        await channel.send(message)

    assert len(client.posts) == 1
    # The question that is not posted is named in the log and on the message,
    # against the request it belongs to. The sentence that states it is free to
    # change; the prompt and the id are what an operator searches for.
    assert "second" in caplog.text
    assert "call_abc123" in caplog.text
    note = [
        block
        for block in client.posts[0]["blocks"]
        if block.get("type") == "context"
    ]
    assert len(note) == 1
    assert "second" in note[0]["elements"][0]["text"]


@pytest.mark.asyncio
async def test_unanswered_questions_do_not_accumulate_without_bound() -> None:
    channel, _client = _channel()

    for index in range(40):
        await channel.send(_question_message(request_id=f"call_{index}"))

    assert len(channel._pending_questions) <= 32
    assert "call_39" in channel._pending_questions


@pytest.mark.asyncio
async def test_stopping_the_channel_forgets_pending_questions() -> None:
    channel, _client = _channel()
    await channel.send(_question_message())
    assert channel._pending_questions

    await channel.stop()

    assert channel._pending_questions == {}


@pytest.mark.asyncio
async def test_ordinary_replies_still_post_without_blocks() -> None:
    """The blocks argument is additive; nothing else may start sending one."""
    channel, client = _channel()

    await channel.send(
        Message(
            id="reply-1",
            type="event",
            channel_id="slack",
            session_id="slack_T1_C1_1710000000.000100",
            params={},
            timestamp=time.time(),
            ok=True,
            payload={"content": "done"},
            event_type=EventType.CHAT_FINAL,
            metadata={"slack_channel_id": "C1"},
        )
    )

    assert client.posts[0]["text"] == "done"
    assert "blocks" not in client.posts[0]


def _click(
    *,
    request_id: str = "call_abc123",
    index: int = 0,
    value: str = "approve",
    source: str = "permission_interrupt",
    session_id: str = "slack_T1_C1_1710000000.000100",
    label: str = "Approve",
    channel_id: str = "C1",
    message_ts: str = "1710000099.000001",
    thread_ts: str = "1710000000.000100",
    user_id: str = "U1",
    blocks: list[dict[str, Any]] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Build the ``(body, action)`` Bolt hands a block_actions listener."""
    encoded = json.dumps(
        {
            "request_id": request_id,
            "index": index,
            "value": value,
            "source": source,
            "session_id": session_id,
        },
        separators=(",", ":"),
    )
    action = {
        "type": "button",
        "action_id": f"jiuwenswarm_answer:{index}",
        "value": encoded,
        "text": {"type": "plain_text", "text": label},
    }
    if blocks is None:
        blocks = [
            {
                "type": "section",
                "block_id": "b0",
                "text": {"type": "mrkdwn", "text": "Proceed?"},
            },
            {"type": "actions", "block_id": "b1", "elements": [action]},
        ]
    body = {
        "type": "block_actions",
        "user": {"id": user_id},
        "team": {"id": "T1"},
        "channel": {"id": channel_id, "name": "general"},
        "container": {
            "type": "message",
            "channel_id": channel_id,
            "message_ts": message_ts,
            "thread_ts": thread_ts,
        },
        "message": {
            "ts": message_ts,
            "thread_ts": thread_ts,
            "text": "Proceed?",
            "blocks": blocks,
        },
        "actions": [action],
    }
    return body, action


class _RecordingAck:
    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self, *_args: Any, **_kwargs: Any) -> None:
        self.calls += 1


async def _post_then_click(
    channel: SlackChannel, **click_overrides: Any
) -> tuple[list[Message], _RecordingAck]:
    received: list[Message] = []
    channel.on_message(received.append)
    ack = _RecordingAck()
    body, action = _click(**click_overrides)
    await channel._handle_question_action(ack, body, action)
    return received, ack


@pytest.mark.asyncio
async def test_click_answers_the_waiting_turn_with_the_selected_option() -> None:
    channel, _client = _channel()
    await channel.send(_question_message())

    received, ack = await _post_then_click(channel)

    assert ack.calls == 1
    assert len(received) == 1
    answer = received[0]
    assert answer.params["request_id"] == "call_abc123"
    assert answer.params["source"] == "permission_interrupt"
    assert answer.params["answers"] == [
        {
            "question": "Tool `bash` needs permission to run",
            "selected_options": ["approve"],
            "custom_input": "",
        }
    ]
    # Answered into the session that asked, not one derived from the click.
    assert answer.session_id == "slack_T1_C1_1710000000.000100"
    assert answer.chat_id == "C1"
    assert answer.user_id == "U1"
    assert answer.metadata["slack_channel_id"] == "C1"
    assert answer.metadata["slack_thread_ts"] == "1710000000.000100"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "source",
    [
        "permission_interrupt",
        "confirm_interrupt",
        "ask_user_interrupt",
        "evolution_interrupt",
    ],
)
async def test_an_interrupt_answer_resumes_the_turn_through_chat_send(
    source: str,
) -> None:
    """chat.user_answer would not resume a paused turn; chat.send does."""
    channel, _client = _channel()
    await channel.send(_question_message(source=source))

    received, _ack = await _post_then_click(channel, source=source)

    answer = received[0]
    assert answer.req_method is ReqMethod.CHAT_SEND
    assert answer.is_stream is True
    assert answer.params["query"] == ""
    assert answer.params["supports_user_interaction"] is True


@pytest.mark.asyncio
async def test_a_standalone_approval_answer_uses_chat_user_answer() -> None:
    channel, _client = _channel()
    await channel.send(
        _question_message(
            request_id="skill_evolve_1", source="skill_evolution_approval"
        )
    )

    received, _ack = await _post_then_click(
        channel, request_id="skill_evolve_1", source="skill_evolution_approval"
    )

    answer = received[0]
    assert answer.req_method is ReqMethod.CHAT_ANSWER
    assert answer.is_stream is False
    assert "query" not in answer.params


@pytest.mark.asyncio
async def test_click_rewrites_the_message_without_its_buttons() -> None:
    channel, client = _channel()
    await channel.send(_question_message())

    await _post_then_click(channel)

    assert len(client.updates) == 1
    update = client.updates[0]
    assert update["channel"] == "C1"
    assert update["ts"] == "1710000099.000001"
    assert [block["type"] for block in update["blocks"]] == ["section", "context"]
    assert update["blocks"][-1]["elements"][0]["text"] == "Answered by <@U1>: Approve"
    # The question itself is kept, so the channel still records what was asked.
    assert update["blocks"][0]["text"]["text"] == "Proceed?"


@pytest.mark.asyncio
async def test_a_second_click_answers_nothing() -> None:
    channel, client = _channel()
    await channel.send(_question_message())

    received, _ack = await _post_then_click(channel)
    assert len(received) == 1

    second: list[Message] = []
    channel.on_message(second.append)
    body, action = _click(index=1, value="reject", label="Reject")
    await channel._handle_question_action(_RecordingAck(), body, action)

    assert second == []
    # The stale message is still stripped, so it stops inviting clicks.
    assert len(client.updates) == 2
    assert "no longer waiting" in client.updates[1]["blocks"][-1]["elements"][0]["text"]


@pytest.mark.asyncio
async def test_click_is_acknowledged_before_anything_can_fail() -> None:
    """Slack shows the clicker an error unless the interaction is acked fast."""
    channel, _client = _channel()
    ack = _RecordingAck()
    body, action = _click(request_id="never-posted")

    await channel._handle_question_action(ack, body, action)

    assert ack.calls == 1


@pytest.mark.asyncio
async def test_a_user_outside_the_allow_list_cannot_answer() -> None:
    channel, client = _channel(allow_from=["U1"])
    await channel.send(_question_message())

    received, _ack = await _post_then_click(channel, user_id="U-INTRUDER")

    assert received == []
    assert client.updates == []
    # The question is still answerable by someone who is allowed to.
    assert "call_abc123" in channel._pending_questions


@pytest.mark.asyncio
async def test_a_button_value_that_is_not_ours_is_ignored() -> None:
    channel, _client = _channel()
    received: list[Message] = []
    channel.on_message(received.append)

    for raw in ("", "not json", json.dumps({"index": 0}), json.dumps(["a"])):
        await channel._handle_question_action(
            _RecordingAck(),
            {"user": {"id": "U1"}},
            {"value": raw, "text": {"type": "plain_text", "text": "x"}},
        )

    assert received == []


@pytest.mark.asyncio
async def test_the_full_value_is_answered_even_when_the_button_shed_it() -> None:
    """The record, not the button, is the authority on what an option means."""
    channel, _client = _channel()
    await channel.send(
        _question_message(options=[{"label": "Approve", "value": "v" * 4000}])
    )

    received, _ack = await _post_then_click(channel, value="")

    assert received[0].params["answers"][0]["selected_options"] == ["v" * 4000]


@pytest.mark.asyncio
async def test_a_failed_rewrite_does_not_cost_the_turn_its_answer() -> None:
    channel, _client = _channel()
    await channel.send(_question_message())

    class _FailingUpdates(_RecordingSlackClient):
        async def chat_update(self, **_kwargs: Any) -> dict[str, str]:
            raise RuntimeError("message_not_found")

    channel._client = _FailingUpdates()

    received, _ack = await _post_then_click(channel)

    assert len(received) == 1


@pytest.mark.asyncio
async def test_a_stopped_channel_ignores_a_click() -> None:
    channel, _client = _channel()
    await channel.send(_question_message())
    channel._running = False

    received, _ack = await _post_then_click(channel)

    assert received == []


@pytest.mark.asyncio
async def test_start_registers_the_answer_action_listener(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = asyncio.Event()
    closed = asyncio.Event()
    registered_actions: list[Any] = []

    class _FakeAsyncApp:
        def __init__(self, token: str, logger: Any = None) -> None:
            self.client = _RecordingSlackClient()

        def event(self, _name: str):
            return lambda listener: listener

        def action(self, constraint: Any):
            registered_actions.append(constraint)
            return lambda listener: listener

    class _FakeHandler:
        def __init__(self, app: Any, app_token: str) -> None:
            pass

        async def start_async(self) -> None:
            started.set()
            await closed.wait()

        async def close_async(self) -> None:
            closed.set()

    monkeypatch.setattr(slack_connect, "SLACK_AVAILABLE", True)
    monkeypatch.setattr(slack_connect, "AsyncApp", _FakeAsyncApp)
    monkeypatch.setattr(slack_connect, "AsyncSocketModeHandler", _FakeHandler)

    channel = SlackChannel(
        SlackChannelConfig(enabled=True, bot_token="xoxb-t", app_token="xapp-t"),
        RobotMessageRouter(),
    )
    task = asyncio.create_task(channel.start())
    await asyncio.wait_for(started.wait(), timeout=1)

    # Two listeners, and they are two because a stop is not an answer: this one
    # resolves a click against a question the session is paused on, the other
    # against a turn that is still running. Sharing the prefix would put a stop
    # through the handler that reads every payload as an answer.
    assert len(registered_actions) == 2
    pattern, stop = registered_actions
    assert pattern.match("jiuwenswarm_answer:0")
    assert pattern.match("jiuwenswarm_answer:12")
    assert not pattern.match("some_other_button")
    assert not pattern.match(stop)
    assert stop == slack_connect._STOP_ACTION_ID

    await channel.stop()
    await asyncio.wait_for(task, timeout=1)


def test_startup_says_buttons_are_inactive_when_streaming_is_off(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Approvals have no switch of their own, so the only way to learn they are
    # unavailable is to be told -- otherwise the first sign is a task that
    # paused for an approval that was never shown.
    channel = SlackChannel(
        SlackChannelConfig(enabled=True, enable_streaming=False),
        RobotMessageRouter(),
    )

    with caplog.at_level(logging.WARNING, logger=slack_connect.logger.name):
        channel._log_approval_button_availability()

    assert "approval buttons inactive" in caplog.text
    assert "enable_streaming" in caplog.text


def test_startup_says_buttons_are_active_when_streaming_is_on(
    caplog: pytest.LogCaptureFixture,
) -> None:
    channel = SlackChannel(
        SlackChannelConfig(enabled=True, enable_streaming=True),
        RobotMessageRouter(),
    )

    with caplog.at_level(logging.INFO, logger=slack_connect.logger.name):
        channel._log_approval_button_availability()

    assert "approval buttons active" in caplog.text


def _listening_channel() -> tuple[SlackChannel, _RecordingSlackClient, list[Message]]:
    """A channel whose routed messages are collected, with no acknowledgements.

    Acknowledgements are switched off so that the reaction an inbound message
    would otherwise draw does not have to be stubbed on the recording client.
    """
    channel, client = _channel(acknowledge_mode="off")
    routed: list[Message] = []
    channel.on_message(routed.append)
    return channel, client, routed


async def _ordinary_message(
    channel: SlackChannel,
    *,
    channel_id: str = "C1",
    thread_ts: str = "1710000000.000100",
    message_ts: str = "1710000200.000300",
    text: str = "never mind, look at this instead",
) -> None:
    """Deliver a plain message, which is what withdraws a pending interrupt."""
    await channel._handle_slack_event(
        {
            "type": "message",
            "user": "U1",
            "channel": channel_id,
            "ts": message_ts,
            "thread_ts": thread_ts,
            "text": text,
        },
        {"team_id": "T1", "event_id": f"Ev{message_ts}"},
        is_dm=False,
        trigger="mention",
    )


@pytest.mark.asyncio
async def test_a_message_in_the_same_session_withdraws_the_question() -> None:
    """The harness drops an interrupt the moment a non-answer resumes it."""
    channel, client, routed = _listening_channel()
    await channel.send(_question_message())

    await _ordinary_message(channel)

    assert channel._pending_questions["call_abc123"].withdrawn_at is not None
    assert len(client.updates) == 1
    update = client.updates[0]
    assert update["channel"] == "C1"
    assert update["ts"] == "1710000099.000001"
    # Passed and empty: an empty list is how chat.update takes away the blocks a
    # message already has, and omitting the field would leave the buttons up.
    assert update["blocks"] == []
    assert "no longer waiting for an answer" in update["text"]
    # The question survives, so the channel still records what was asked.
    assert "needs permission to run" in update["text"]
    # The message that withdrew it is still delivered, with the envelope
    # marker every dispatched message gets and nothing else.
    assert [msg.params["content"] for msg in routed] == [
        "never mind, look at this instead\n\n"
        "[ts: 1710000200.000300, thread_ts: 1710000000.000100]"
    ]


@pytest.mark.asyncio
async def test_a_click_on_a_withdrawn_question_routes_nothing(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Routing it would put the option's label into the conversation as text."""
    channel, client, routed = _listening_channel()
    await channel.send(_question_message())
    await _ordinary_message(channel)
    delivered = len(routed)

    ack = _RecordingAck()
    body, action = _click()
    with caplog.at_level(logging.WARNING, logger=slack_connect.logger.name):
        await channel._handle_question_action(ack, body, action)

    assert len(routed) == delivered
    # The click is still acknowledged: Slack shows the clicker an error if not.
    assert ack.calls == 1
    assert "call_abc123" in caplog.text
    assert "withdrawn 0s earlier" in caplog.text
    # And the message it was clicked on loses its buttons a second time.
    assert client.updates[-1]["ts"] == "1710000099.000001"
    assert (
        "no longer waiting"
        in client.updates[-1]["blocks"][-1]["elements"][0]["text"]
    )


@pytest.mark.asyncio
async def test_a_second_click_on_a_withdrawn_question_is_reported_the_same_way(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The record is kept, so every stale click reads as stale rather than lost."""
    channel, _client, routed = _listening_channel()
    await channel.send(_question_message())
    await _ordinary_message(channel)
    delivered = len(routed)

    body, action = _click()
    await channel._handle_question_action(_RecordingAck(), body, action)
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger=slack_connect.logger.name):
        await channel._handle_question_action(_RecordingAck(), body, action)

    assert len(routed) == delivered
    assert "withdrawn" in caplog.text


@pytest.mark.asyncio
async def test_a_prompt_click_still_answers_and_strips_the_buttons() -> None:
    """The ordinary path is untouched: a prompt click answers and closes."""
    channel, client = _channel()
    await channel.send(_question_message())

    received, _ack = await _post_then_click(channel)

    assert len(received) == 1
    assert received[0].params["answers"][0]["selected_options"] == ["approve"]
    assert len(client.updates) == 1
    assert [block["type"] for block in client.updates[0]["blocks"]] == [
        "section",
        "context",
    ]
    assert (
        client.updates[0]["blocks"][-1]["elements"][0]["text"]
        == "Answered by <@U1>: Approve"
    )
    assert "call_abc123" not in channel._pending_questions


@pytest.mark.asyncio
async def test_a_message_in_another_session_leaves_the_question_answerable() -> None:
    """Only the session that is paused is resumed by the message that arrives."""
    channel, client, routed = _listening_channel()
    await channel.send(_question_message())

    await _ordinary_message(channel, thread_ts="1710000000.000999")

    assert channel._pending_questions["call_abc123"].withdrawn_at is None
    assert client.updates == []

    body, action = _click()
    await channel._handle_question_action(_RecordingAck(), body, action)

    assert [msg.params.get("request_id") for msg in routed[1:]] == ["call_abc123"]


@pytest.mark.asyncio
async def test_a_standalone_approval_survives_a_message_in_its_session() -> None:
    """Nothing is paused on it, so no message can withdraw what it is waiting for."""
    channel, client, _routed = _listening_channel()
    await channel.send(
        _question_message(
            request_id="skill_evolve_1", source="skill_evolution_approval"
        )
    )

    await _ordinary_message(channel)

    assert channel._pending_questions["skill_evolve_1"].withdrawn_at is None
    assert client.updates == []


@pytest.mark.asyncio
async def test_an_unanswerable_notice_is_not_rewritten_by_a_later_message() -> None:
    """A free-form question posts a notice and remembers nothing to withdraw."""
    channel, client, _routed = _listening_channel()
    await channel.send(_question_message(options=[]))
    assert channel._pending_questions == {}

    await _ordinary_message(channel)

    assert client.updates == []
    assert len(client.posts) == 1
    assert "cannot route back to the waiting task" in client.posts[0]["text"]


def test_an_option_is_read_into_named_fields() -> None:
    """The structure the two widenings collided over, read by name.

    Both halves that were added independently -- what choosing the option does
    and how its button is drawn -- are asserted here together, because the
    reason this is a dataclass rather than a tuple is that a change wanting one
    of them must not have to know the position of the other.
    """
    options = SlackChannel._question_options(
        {
            "options": [
                {
                    "label": "Allow once",
                    "value": ALLOW_ONCE,
                    "description": "Run it this one time.",
                },
                {"label": "Reject", "value": REJECT},
            ]
        }
    )

    assert [option.label for option in options] == ["Allow once", "Reject"]
    assert [option.value for option in options] == [ALLOW_ONCE, REJECT]
    assert [option.description for option in options] == ["Run it this one time.", ""]
    assert [option.style for option in options] == ["primary", "danger"]


def test_an_option_carries_no_positional_contract() -> None:
    """A field is reached by name and never by index.

    Stated as a test because the cost this replaced was invisible until a
    second change arrived: a tuple widened once looks fine, and the breakage
    lands on whoever widens it next. The two fields that were bolted on both
    default, so a caller wanting neither states neither.
    """
    option = slack_connect._QuestionOption(label="Approve", value="approve")

    assert option.description == ""
    assert option.style == ""
    with pytest.raises(TypeError):
        option[0]  # type: ignore[index]
