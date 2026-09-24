# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.

"""What a turn keeps when a permission prompt interrupts it.

A bare ``NO_REPLY`` was posted into a live channel, twice::

    15:06:58  delivered question   source=permission_interrupt   channel=C0BK...
    15:07:08  answered question    (approved)
    15:07:08  dispatch             slack-answer-chatcmpl-tool-...
    15:07:10  delivered text       chunk=1/1  mode=post

The same conversation withheld three replies correctly in the half hour that
followed, so the silence contract itself was sound everywhere it was armed. The
resume was the one path that disarmed it.

An answered question resumes its turn as a brand new request, and that request's
metadata was built from the click alone. A click reports the conversation it was
pressed in and the person who pressed it; it says nothing about the message that
started the work. So three facts the interrupted turn had been dispatched with
were gone by the time it carried on:

``slack_reply_optional`` is the matcher half of the silence contract: it says
that a reply consisting of ``NO_REPLY`` alone is a decision to say nothing
rather than a word to post, and the outbound half looks for the token only
where the request carries it. Without the key the resumed turn obeyed the
instruction it had been given and the token was delivered as ordinary text.

``post_as_root`` is routing, and the louder failure of the two. An event turn
with nothing to anchor to is addressed to the room; the resume routes by the
clicked card's container instead, so a question posted inside a thread would
have put a turn about the room into that thread. The incident landed at the root
only because the card happened to be there.

``slack_trigger`` is diagnostic. No dispatch path writes an empty one, so a
resumed turn arriving without it reads as a turn nothing woke.

The repair is to hold the dispatch's own answers on the pending question and
stamp them back. What is held is the matcher fact and never the offer: the
fragment is withheld from a message that addressed the bot, and carrying that
answer across the pause would disarm the matcher on exactly the resumes this
file is about.

Settling the fact again from config is the obvious alternative and is wrong. The
read needs the kind of room the original message arrived in, which a click does
not carry, and it would answer from the config as it stands rather than as the
turn was dispatched under it -- with a person's approval, and however long they
took, in between. :func:`test_a_resume_is_not_granted_a_contract_its_turn_lacked`
pins the half that matters most: a room that licensed nothing grants nothing on
the way back.
"""

from __future__ import annotations

import time
from typing import Any

import pytest

from jiuwenswarm.common.schema.message import EventType, Message
from jiuwenswarm.common.scopes import compile_scopes
from jiuwenswarm.gateway.channel_manager.base import RobotMessageRouter
from jiuwenswarm.gateway.channel_manager.im_platforms.slack import slack_connect
from jiuwenswarm.gateway.channel_manager.im_platforms.slack.slack_connect import (
    NO_REPLY_SENTINEL,
    POST_AS_ROOT_KEY,
    SLACK_REPLY_OPTIONAL_KEY,
    SlackChannel,
    SlackChannelConfig,
)

CHAT = "C1"
TEAM = "T1"
ASKER = "U0ASKER001"
# Whoever happened to be watching when the prompt went up. Routinely not the
# person who asked for the work, which is why the resume cannot be settled for
# them.
CLICKER = "U0CLICKER1"
BOT = "U-BOT"
QUESTION_ID = "chatcmpl-tool-0123456789abcdef"
BODY = {"event_id": "Ev1", "team_id": TEAM}
# The card the prompt was posted on, and the thread that card sits in. The pair
# is what a resume routes by when nothing outranks it.
CARD_TS = "1710000016.000100"
CARD_THREAD = "1710000001.000100"

_real_store_init = slack_connect.SlackEventDedupStore.__init__


@pytest.fixture(autouse=True)
def _isolated_dedup_store(tmp_path, monkeypatch):
    """Give every test its own dedup file, never the real workspace's."""
    monkeypatch.setattr(
        slack_connect.SlackEventDedupStore,
        "__init__",
        lambda self, path=None, **kw: _real_store_init(
            self, path or tmp_path / "slack_seen_events.json", **kw
        ),
    )


class _RecordingSlackClient:
    """Just enough Slack to see what was posted and what was edited."""

    def __init__(self) -> None:
        self.posts: list[dict[str, Any]] = []
        self.updates: list[dict[str, Any]] = []

    async def chat_postMessage(self, **kwargs: Any) -> dict[str, str]:
        self.posts.append(kwargs)
        return {"ts": f"1710000099.{len(self.posts):06d}"}

    async def chat_update(self, **kwargs: Any) -> dict[str, str]:
        self.updates.append(kwargs)
        return {"ts": str(kwargs.get("ts") or "")}

    async def chat_delete(self, **kwargs: Any) -> dict[str, bool]:
        return {"ok": True}

    async def chat_postEphemeral(self, **kwargs: Any) -> dict[str, bool]:
        return {"ok": True}

    async def reactions_add(self, **kwargs: Any) -> dict[str, bool]:
        return {"ok": True}

    async def reactions_remove(self, **kwargs: Any) -> dict[str, bool]:
        return {"ok": True}

    async def api_call(self, method: str, **kwargs: Any) -> dict[str, bool]:
        raise RuntimeError(f"{method} is not available in this workspace")


def _channel(
    policy: dict | None = None, *, reply: str = "optional", group_chat_mode: str = "all"
) -> tuple[SlackChannel, list[Message]]:
    """A conversation licensed to wake turns, and the requests it dispatches."""
    config = SlackChannelConfig(
        enabled=True,
        allowed_channel_ids=[CHAT],
        group_chat_mode=group_chat_mode,
        conversation_overrides={
            CHAT: slack_connect.SlackChannelOverride(events=policy, reply=reply)
        },
    )
    channel = SlackChannel(config, RobotMessageRouter())
    channel._running = True
    channel._bot_user_id = BOT
    channel._client = _RecordingSlackClient()
    received: list[Message] = []
    channel.on_message(received.append)
    return channel, received


def _member_event(user: str = "U-NEW") -> dict:
    """A membership change: the family with no message to anchor to."""
    return {
        "type": "member_joined_channel",
        "user": user,
        "channel": CHAT,
        "channel_type": "C",
        "team": TEAM,
        "event_ts": "1710000300.000400",
    }


def _message_event(text: str = "shipping it now") -> dict:
    return {
        "type": "message",
        "channel_type": "channel",
        "channel": CHAT,
        "user": ASKER,
        "text": text,
        "ts": "1710000010.000100",
        "thread_ts": CARD_THREAD,
    }


async def _woken_by_an_event(channel: SlackChannel) -> None:
    await channel._route_inbound_event(_member_event(), BODY)


async def _woken_by_a_message(channel: SlackChannel, text: str) -> None:
    await channel._handle_slack_event(
        _message_event(text), BODY, is_dm=False, trigger="all"
    )


async def _ask(channel: SlackChannel, request: Message) -> None:
    """Put a permission prompt up against ``request``'s turn."""
    await channel.send(
        Message(
            id=request.id,
            type="event",
            channel_id="slack",
            session_id=request.session_id,
            params={},
            timestamp=time.time(),
            ok=True,
            payload={
                "event_type": EventType.CHAT_ASK_USER_QUESTION.value,
                "request_id": QUESTION_ID,
                "source": "permission_interrupt",
                "questions": [
                    {
                        "question": "Allow the subagent to write that file?",
                        "options": [
                            {"label": "Approve", "value": "approve"},
                            {"label": "Reject", "value": "reject"},
                        ],
                        "multi_select": False,
                    }
                ],
            },
            event_type=EventType.CHAT_ASK_USER_QUESTION,
            metadata={"slack_channel_id": CHAT},
        )
    )


async def _approve(
    channel: SlackChannel, *, thread_ts: str = CARD_THREAD, question_id: str = QUESTION_ID
) -> None:
    """Somebody presses Approve on the card, from inside its thread."""
    await channel._dispatch_answer(
        channel._pending_questions[question_id],
        values=["approve"],
        body={
            "container": {
                "channel_id": CHAT,
                "message_ts": CARD_TS,
                "thread_ts": thread_ts,
            },
            "channel": {"id": CHAT},
        },
        user_id=CLICKER,
    )


async def _interrupt_and_resume(channel: SlackChannel, received: list[Message]) -> Message:
    """Pause the latest turn on a permission prompt and approve it."""
    await _ask(channel, received[-1])
    channel._client.posts.clear()
    await _approve(channel)
    return received[-1]


def _reply_to(request: Message, content: str) -> Message:
    """The answer to ``request``, holding the metadata the request held.

    The gateway merges request metadata into every response it builds, so this
    is the shape a reply arrives in -- and the whole of why a fact dropped on
    the way in cannot be recovered on the way out.
    """
    return Message(
        id=request.id,
        type="event",
        channel_id="slack",
        session_id=request.session_id,
        params={},
        timestamp=time.time(),
        ok=True,
        payload={"event_type": EventType.CHAT_FINAL.value, "content": content},
        event_type=EventType.CHAT_FINAL,
        metadata=dict(request.metadata or {}),
    )


# ----------------------------------------------------------------------
# slack_reply_optional: the contract the incident lost
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_resumed_turn_that_says_nothing_posts_nothing() -> None:
    """The incident, end to end: approve a prompt, then decline to answer."""
    channel, received = _channel({"member": "turn"})

    await _woken_by_an_event(channel)
    resumed = await _interrupt_and_resume(channel, received)

    assert resumed.id.startswith("slack-answer-")
    await channel.send(_reply_to(resumed, NO_REPLY_SENTINEL))

    assert channel._client.posts == []


@pytest.mark.asyncio
async def test_a_resume_holds_the_contract_its_turn_was_dispatched_with() -> None:
    channel, received = _channel({"member": "turn"})

    await _woken_by_an_event(channel)
    assert received[0].metadata[SLACK_REPLY_OPTIONAL_KEY] is True
    resumed = await _interrupt_and_resume(channel, received)

    assert resumed.metadata[SLACK_REPLY_OPTIONAL_KEY] is True


@pytest.mark.asyncio
async def test_a_resume_uses_the_initiators_agent_scope() -> None:
    channel, received = _channel(group_chat_mode="mention")
    channel.config.scopes = compile_scopes([
        {"match": {"channel": "slack", "chat": CHAT, "user": [ASKER]},
         "agent": {"subagents": ["research_agent"],
                   "skills": {"available": ["ambient"], "required": ["ambient"]}}},
        {"match": {"channel": "slack", "chat": CHAT, "user": [CLICKER]},
         "agent": {"subagents": [],
                   "skills": {"available": [], "required": []}}},
    ], warn=lambda *args: None)

    await _woken_by_a_message(channel, f"<@{BOT}> ship it")
    first = received[0]
    resumed = await _interrupt_and_resume(channel, received)

    for request in (first, resumed):
        assert request.params["agent_subagents_available"] == ["research_agent"]
        assert request.params["agent_skills_available"] == ["ambient"]
        assert request.params["agent_skills_required"] == ["ambient"]
    assert resumed.user_id == CLICKER


@pytest.mark.asyncio
async def test_a_resume_is_not_granted_a_contract_its_turn_lacked() -> None:
    """A room that licensed nothing grants nothing on the way back.

    ``delivery.reply`` here is the default, so no turn in this conversation may
    decline. The dispatch stamps no key, the resume stamps none either, and the
    token is posted as the ordinary text it is. Nothing on the resume path can
    widen the contract: the key it carries is the dispatch's own answer, and the
    outbound reader requires the live config to agree with it besides.
    """
    channel, received = _channel(reply="required", group_chat_mode="mention")

    await _woken_by_a_message(channel, f"<@{BOT}> ship it")
    assert SLACK_REPLY_OPTIONAL_KEY not in received[0].metadata
    resumed = await _interrupt_and_resume(channel, received)

    assert SLACK_REPLY_OPTIONAL_KEY not in resumed.metadata
    # And the behaviour that key decides is unchanged: the token is text.
    await channel.send(_reply_to(resumed, NO_REPLY_SENTINEL))
    assert [post.get("text") for post in channel._client.posts] == [NO_REPLY_SENTINEL]


@pytest.mark.asyncio
async def test_a_resumed_addressed_turn_still_holds_the_matcher() -> None:
    """The matcher fact crosses the pause, and the offer's rule does not.

    The message named the bot, so its own prompt carried no fragment. What the
    room licensed is what the resume inherits, and a turn that writes the token
    after the approval has it honoured rather than posted.
    """
    channel, received = _channel(group_chat_mode="mention")

    await _woken_by_a_message(channel, f"<@{BOT}> ship it")
    assert received[0].metadata[SLACK_REPLY_OPTIONAL_KEY] is True
    resumed = await _interrupt_and_resume(channel, received)

    assert resumed.metadata[SLACK_REPLY_OPTIONAL_KEY] is True
    # The address is diagnostic and crosses the pause with it, so the line
    # recording the silence reads as the addressed case it is.
    assert resumed.metadata[slack_connect.SLACK_ADDRESSED_KEY] is True
    await channel.send(_reply_to(resumed, NO_REPLY_SENTINEL))
    assert channel._client.posts == []


@pytest.mark.asyncio
async def test_a_turn_interrupted_twice_still_holds_the_contract() -> None:
    """The second prompt reads the entry the first resume opened."""
    channel, received = _channel({"member": "turn"})

    await _woken_by_an_event(channel)
    await _interrupt_and_resume(channel, received)
    await _ask(channel, received[-1])
    await _approve(channel, question_id=QUESTION_ID)

    assert received[-1].metadata[SLACK_REPLY_OPTIONAL_KEY] is True


# ----------------------------------------------------------------------
# post_as_root: where a resumed event turn answers
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_resumed_event_turn_still_answers_at_the_channel_root() -> None:
    """The card was clicked inside a thread; the turn is still about the room."""
    channel, received = _channel({"member": "turn"})

    await _woken_by_an_event(channel)
    assert received[0].metadata[POST_AS_ROOT_KEY] is True
    resumed = await _interrupt_and_resume(channel, received)

    assert resumed.metadata[POST_AS_ROOT_KEY] is True
    # The click's own thread is on the bag and is outranked, which is the whole
    # of what the key is for. Asked of the send path rather than of the ladder:
    # the ladder is free of the key by design, and ``_extract_delivery`` is the
    # one place it is applied.
    assert resumed.metadata["slack_thread_ts"] == CARD_THREAD
    reply = _reply_to(resumed, "Two people joined this week.")
    assert channel._extract_delivery(reply, None) == (CHAT, "")


@pytest.mark.asyncio
async def test_a_resumed_message_turn_is_not_moved_to_the_root() -> None:
    """A turn with a message behind it never held the key and does not gain one."""
    channel, received = _channel(group_chat_mode="mention")

    await _woken_by_a_message(channel, f"<@{BOT}> ship it")
    assert POST_AS_ROOT_KEY not in received[0].metadata
    resumed = await _interrupt_and_resume(channel, received)

    assert POST_AS_ROOT_KEY not in resumed.metadata
    reply = _reply_to(resumed, "Shipped.")
    assert channel._extract_delivery(reply, None) == (CHAT, CARD_THREAD)


# ----------------------------------------------------------------------
# slack_trigger: what woke the turn being resumed
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_resume_names_the_trigger_that_woke_its_turn() -> None:
    channel, received = _channel({"member": "turn"})

    await _woken_by_an_event(channel)
    resumed = await _interrupt_and_resume(channel, received)

    assert resumed.metadata["slack_trigger"] == slack_connect.TRIGGER_EVENT


@pytest.mark.asyncio
async def test_a_resumed_message_turn_names_its_own_trigger() -> None:
    channel, received = _channel(group_chat_mode="mention")

    await _woken_by_a_message(channel, f"<@{BOT}> ship it")
    resumed = await _interrupt_and_resume(channel, received)

    assert resumed.metadata["slack_trigger"] == received[0].metadata["slack_trigger"]


# ----------------------------------------------------------------------
# the record in between
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_question_holds_what_the_dispatch_decided() -> None:
    """The pending question is the only place the two ends are both in reach."""
    channel, received = _channel({"member": "turn"})

    await _woken_by_an_event(channel)
    await _ask(channel, received[-1])

    pending = channel._pending_questions[QUESTION_ID]
    assert pending.initiator_silence_is_honoured is True
    assert pending.initiator_post_as_root is True
    assert pending.initiator_trigger == slack_connect.TRIGGER_EVENT
    # Nothing addresses the bot with a membership change, and the field says so
    # rather than being left to the reader of a log line to guess.
    assert pending.initiator_addressed is False


@pytest.mark.asyncio
async def test_a_question_with_no_initiator_on_record_claims_nothing() -> None:
    """An entry that aged out leaves a resume exactly as bare as it was."""
    channel, received = _channel({"member": "turn"})

    await _woken_by_an_event(channel)
    channel._turn_initiators.clear()
    await _ask(channel, received[-1])

    pending = channel._pending_questions[QUESTION_ID]
    assert pending.initiator_silence_is_honoured is False
    assert pending.initiator_post_as_root is False
    assert pending.initiator_trigger == ""

    await _approve(channel)
    resumed = received[-1]
    assert SLACK_REPLY_OPTIONAL_KEY not in resumed.metadata
    assert POST_AS_ROOT_KEY not in resumed.metadata
    assert "slack_trigger" not in resumed.metadata
