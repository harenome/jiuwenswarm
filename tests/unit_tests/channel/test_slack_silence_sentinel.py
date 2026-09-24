# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.

"""What a turn does when it has nothing to say, and how it says so.

Two replies were observed being posted into a channel as ordinary text::

    (no reply)
    (No reply needed - just an acknowledgment, nothing to add.)

The judgement behind both was right. There was no licensed way to express it, so
the model described its silence and the delivery path posted the description.
``delivery.reply: optional`` is that licence, and ``NO_REPLY`` is the form it
takes: the connector appends an instruction naming the token, and a reply that is
the token and nothing else is withheld.

Three properties are pinned here, each of which would be silently wrong.

**The matcher errs strict, because a match deletes a reply.** Too relaxed
destroys a real answer with nothing on screen to say so; too strict posts a
visible token a reader notices within a message or two. Everything that is not
exactly the token is delivered, the two observed strings included.

**The detector is loose, because it decides only a log line.** It is deliberately
not the matcher and is never defined in terms of it: a first version tested for
the token alone and would have counted zero on both observed failures, which
spell the words with a space where the token has an underscore.

**A message that opens by addressing the bot is offered nothing and is matched
anyway.** Those are two decisions, and one variable used to take both. The
fragment is withheld, because somebody who names the bot should not be met with
an unexplained silence. The matcher stays armed, because a model that has read
the fragment elsewhere in the conversation knows the token whether or not this
message carried it -- and one wrote it, after a person said the answer had been
enough, into a turn whose matcher had been disarmed along with the offer. The
bare word went into the channel. So an addressed message can now get nothing
back, where the room licensed silence and the turn chose it.

Warnings and info lines are captured by replacing the module logger's methods
rather than through ``caplog``: this project's loggers do not propagate, so
``caplog`` sees nothing under the pytest CI runs on.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest

from jiuwenswarm.common.schema.message import EventType, Message
from jiuwenswarm.common.scopes import (
    REPLY_DEFAULT,
    REPLY_OPTIONAL,
    REPLY_REQUIRED,
    compile_scopes,
)
from jiuwenswarm.gateway.channel_manager.base import RobotMessageRouter
from jiuwenswarm.gateway.channel_manager.im_platforms.slack import slack_connect
from jiuwenswarm.gateway.channel_manager.im_platforms.slack.slack_connect import (
    NO_REPLY_INSTRUCTION,
    NO_REPLY_SENTINEL,
    SLACK_REPLY_OPTIONAL_KEY,
    SlackChannel,
    SlackChannelConfig,
    apply_scopes_to_slack_overrides,
    describe_configured_channels,
    is_no_reply_sentinel,
    looks_like_a_declined_reply,
)

ASKER = "U0ASKER001"
BOT = "U0BOTUSER0"
TEAM = "T0TESTTEAM"
DM = "D0DIRECT01"
# Reads every message and may decline to answer.
OPTIONAL = "C0OPTIONAL"
# Reads every message and owes an answer to each, which is the default.
REQUIRED = "C0REQUIRED"
# Answers only when addressed, and may still decline. The pair with OPTIONAL is
# the point of the key: which trigger woke a turn and whether that turn owes an
# answer are separate questions.
ADDRESSED_ONLY = "C0MENTION1"
THREAD = "1710000001.000100"

# The two replies as they were seen, quoted so a change that would post either
# one, or stop noticing it, fails here rather than in a channel.
DESCRIBED_SILENCE = "(no reply)"
DESCRIBED_SILENCE_AT_LENGTH = (
    "(No reply needed - just an acknowledgment, nothing to add.)"
)

_SCOPES = (
    {
        "match": {"channel": "slack", "chat": OPTIONAL},
        "delivery": {"mode": ["all"], "reply": REPLY_OPTIONAL},
    },
    {
        "match": {"channel": "slack", "chat": REQUIRED},
        "delivery": {"mode": ["all"]},
    },
    {
        "match": {"channel": "slack", "chat": ADDRESSED_ONLY},
        "delivery": {"mode": ["mention"], "reply": REPLY_OPTIONAL},
    },
)


@pytest.fixture(autouse=True)
def _isolated_dedup_store(tmp_path, monkeypatch):
    """Give every test its own dedup file, never the real workspace's."""
    real_init = slack_connect.SlackEventDedupStore.__init__
    monkeypatch.setattr(
        slack_connect.SlackEventDedupStore,
        "__init__",
        lambda self, path=None, **kw: real_init(
            self, path or tmp_path / "slack_seen_events.json", **kw
        ),
    )


@pytest.fixture(autouse=True)
def _fast_streaming(monkeypatch):
    """Collapse the debounce and the per-channel spacing a preview waits out."""
    monkeypatch.setattr(slack_connect, "_STREAM_DEBOUNCE_MS", 0)
    monkeypatch.setattr(slack_connect, "_STREAM_MIN_UPDATE_INTERVAL_SECONDS", 0.0)
    monkeypatch.setattr(slack_connect, "_STREAM_APPEND_DEBOUNCE_MS", 0)
    monkeypatch.setattr(slack_connect, "_STREAM_APPEND_MIN_INTERVAL_SECONDS", 0.0)


@pytest.fixture
def warnings(monkeypatch) -> list[str]:
    recorded: list[str] = []

    def record(message: str, *args: Any, **kwargs: Any) -> None:
        recorded.append(message % args if args else message)

    monkeypatch.setattr(slack_connect.logger, "warning", record)
    return recorded


@pytest.fixture
def notes(monkeypatch) -> list[str]:
    """Every INFO line, for the one that says the turn stayed quiet."""
    recorded: list[str] = []

    def record(message: str, *args: Any, **kwargs: Any) -> None:
        recorded.append(message % args if args else message)

    monkeypatch.setattr(slack_connect.logger, "info", record)
    return recorded


class _RecordingSlackClient:
    """Just enough Slack to see what was posted, edited, deleted and reacted."""

    def __init__(self) -> None:
        self.posts: list[dict[str, Any]] = []
        self.updates: list[dict[str, Any]] = []
        self.deleted: list[tuple[str, str]] = []
        self.added: list[tuple[str, str, str]] = []
        self.removed: list[tuple[str, str, str]] = []

    async def chat_postMessage(self, **kwargs: Any) -> dict[str, str]:
        self.posts.append(kwargs)
        return {"ts": f"1710000099.{len(self.posts):06d}"}

    async def chat_update(self, **kwargs: Any) -> dict[str, str]:
        self.updates.append(kwargs)
        return {"ts": str(kwargs.get("ts") or "")}

    async def chat_delete(self, **kwargs: Any) -> dict[str, bool]:
        self.deleted.append((kwargs.get("channel", ""), kwargs.get("ts", "")))
        return {"ok": True}

    async def chat_postEphemeral(self, **kwargs: Any) -> dict[str, bool]:
        return {"ok": True}

    async def api_call(self, method: str, **kwargs: Any) -> dict[str, bool]:
        """Refuse the streaming methods, as a workspace without them does.

        That refusal is what puts the preview on the documented second rung of
        the ladder: the reply is shown with edits instead, in a message posted
        with chat.postMessage. Both surfaces hand ``_close_stream`` the same
        thing -- the ts of the message the preview was written into -- so the
        deletion below is the same call either way.
        """
        raise RuntimeError(f"{method} is not available in this workspace")

    async def reactions_add(self, **kwargs: Any) -> dict[str, bool]:
        self.added.append(
            (
                kwargs.get("channel", ""),
                kwargs.get("timestamp", ""),
                kwargs.get("name", ""),
            )
        )
        return {"ok": True}

    async def reactions_remove(self, **kwargs: Any) -> dict[str, bool]:
        self.removed.append(
            (
                kwargs.get("channel", ""),
                kwargs.get("timestamp", ""),
                kwargs.get("name", ""),
            )
        )
        return {"ok": True}


def _config(entries: Any = _SCOPES, **overrides: Any) -> SlackChannelConfig:
    """A config built the way ``app_gateway`` builds one, from written scopes."""
    group_chat_mode = overrides.pop("group_chat_mode", "mention")
    scopes = compile_scopes(list(entries), warn=lambda *a: None)
    platform, per_chat = apply_scopes_to_slack_overrides(
        {"group_chat_mode": group_chat_mode}, scopes=scopes
    )
    return SlackChannelConfig(
        enabled=True,
        allow_from=[ASKER],
        group_chat_mode=group_chat_mode,
        conversation_overrides=per_chat,
        platform_override=platform,
        scopes=scopes,
        **overrides,
    )


def _channel(
    entries: Any = _SCOPES, **overrides: Any
) -> tuple[SlackChannel, list[Message]]:
    channel = SlackChannel(_config(entries, **overrides), RobotMessageRouter())
    channel._running = True
    channel._bot_user_id = BOT
    channel._client = _RecordingSlackClient()
    received: list[Message] = []
    channel.on_message(received.append)
    return channel, received


def _room_event(
    *, channel_id: str = OPTIONAL, ts: str, text: str = "shipping it now"
) -> dict:
    return {
        "type": "message",
        "channel_type": "channel",
        "channel": channel_id,
        "user": ASKER,
        "text": text,
        "ts": ts,
        "thread_ts": THREAD,
    }


async def _post(
    channel: SlackChannel, event: dict, event_id: str, *, trigger: str = "all"
) -> str:
    return await channel._handle_slack_event(
        event, {"event_id": event_id, "team_id": TEAM}, is_dm=False, trigger=trigger
    )


def _reply_to(request: Message, content: str) -> Message:
    """The answer to ``request``, addressed the way the request addressed itself.

    Holds the request's own metadata, which is where the connector recorded
    whether this turn was offered the contract. The gateway merges request
    metadata into every response it builds, so this is the shape a reply arrives
    in.
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


async def _answer(
    channel: SlackChannel,
    received: list[Message],
    content: str,
    *,
    channel_id: str = OPTIONAL,
    trigger: str = "all",
    text: str = "shipping it now",
    ts: str = "1710000010.000100",
) -> list[dict[str, Any]]:
    """Run one message through, answer it, and return what was posted."""
    await _post(
        channel,
        _room_event(channel_id=channel_id, ts=ts, text=text),
        f"Ev{ts}",
        trigger=trigger,
    )
    channel._client.posts.clear()
    await channel.send(_reply_to(received[-1], content))
    return channel._client.posts


async def _settle() -> None:
    """Let the debounced preview writes run."""
    for _ in range(10):
        await asyncio.sleep(0)


# --------------------------------------------------------------------------
# The matcher, which decides delivery and is strict
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "reply",
    [
        NO_REPLY_SENTINEL,
        NO_REPLY_SENTINEL.lower(),
        f"`{NO_REPLY_SENTINEL}`",
        f"[{NO_REPLY_SENTINEL}]",
        f"({NO_REPLY_SENTINEL})",
        f'"{NO_REPLY_SENTINEL}"',
        f"'{NO_REPLY_SENTINEL}'",
        f"{NO_REPLY_SENTINEL}.",
        f"{NO_REPLY_SENTINEL}!",
        f"  {NO_REPLY_SENTINEL}\n",
        f"​{NO_REPLY_SENTINEL}﻿",
        f"⁠ {NO_REPLY_SENTINEL} ⁠",
        f"`{NO_REPLY_SENTINEL}`.",
        f"[ {NO_REPLY_SENTINEL} ]",
    ],
)
def test_the_token_alone_is_recognised_however_it_is_wrapped_or_padded(reply):
    assert is_no_reply_sentinel(reply)


@pytest.mark.parametrize(
    "reply",
    [
        DESCRIBED_SILENCE,
        DESCRIBED_SILENCE_AT_LENGTH,
        "no reply",
        f"{NO_REPLY_SENTINEL} {NO_REPLY_SENTINEL}",
        f"The answer is 42. {NO_REPLY_SENTINEL}",
        f"{NO_REPLY_SENTINEL}\nand one more thing",
        f"Write {NO_REPLY_SENTINEL} when you have nothing to say.",
        "",
    ],
)
def test_anything_that_is_not_the_token_alone_is_left_for_delivery(reply):
    assert not is_no_reply_sentinel(reply)


# --------------------------------------------------------------------------
# The detector, which decides a log line and is loose
# --------------------------------------------------------------------------


def test_both_observed_replies_are_noticed_by_the_widened_detector():
    """The measurement that motivated the feature has to count the failures.

    A detector testing for the token alone counted zero on both of these, which
    is the one thing it must not do: they are the evidence the whole design is
    being judged against.
    """
    assert looks_like_a_declined_reply(DESCRIBED_SILENCE)
    assert looks_like_a_declined_reply(DESCRIBED_SILENCE_AT_LENGTH)


@pytest.mark.parametrize(
    "reply",
    [
        f"{NO_REPLY_SENTINEL} {NO_REPLY_SENTINEL}",
        f"The answer is 42. {NO_REPLY_SENTINEL}",
        f"Write {NO_REPLY_SENTINEL} on its own line.",
        "NO REPLY",
        "no-reply",
        "[no reply]",
        "(nothing to say)",
        "Nothing to add.",
        "no reply needed",
    ],
)
def test_a_turn_reaching_for_any_of_these_shapes_is_counted(reply):
    assert looks_like_a_declined_reply(reply)


@pytest.mark.parametrize(
    "reply",
    [
        "Two checks failed on the last run.",
        "I updated the handler (nothing to add to the tests) and pushed.",
        (
            "The deploy is green; there is nothing to add to the report beyond"
            " the two failures listed above, which are both known flakes."
        ),
        "",
    ],
)
def test_prose_that_merely_uses_the_words_is_not_counted(reply):
    """A false positive costs one log line, so the detector may be loose.

    It may not be careless: a reply using the words while answering is an answer,
    and counting it would bury the shapes worth reading.
    """
    assert not looks_like_a_declined_reply(reply)


def test_the_matcher_and_the_detector_disagree_on_purpose():
    """Nothing the detector notices is thereby deleted.

    They decide different things -- one delivery, one a log line -- so the strict
    one is never widened to the loose one's shape.
    """
    for reply in (DESCRIBED_SILENCE, "Nothing to add.", "no-reply"):
        assert looks_like_a_declined_reply(reply)
        assert not is_no_reply_sentinel(reply)


# --------------------------------------------------------------------------
# delivery.reply, and which conversations it reaches
# --------------------------------------------------------------------------


def test_a_conversation_nobody_wrote_the_key_for_owes_an_answer():
    channel, _ = _channel()

    assert REPLY_DEFAULT == REPLY_REQUIRED
    assert not channel._channel_reply_is_optional(REQUIRED)
    assert not channel._channel_reply_is_optional("C0UNNAMED1")


def test_a_conversation_the_key_licenses_may_decline():
    channel, _ = _channel()

    assert channel._channel_reply_is_optional(OPTIONAL)


def test_the_licence_is_independent_of_what_wakes_the_turn():
    """Which trigger fired and whether an answer is owed are separate questions.

    Two rooms read every message and want opposite things; a third answers only
    when addressed and is still licensed to decline. One word could not say all
    three, which is why this is a key rather than a trigger.
    """
    channel, _ = _channel()

    assert channel._channel_triggers(OPTIONAL) == channel._channel_triggers(REQUIRED)
    assert channel._channel_reply_is_optional(OPTIONAL)
    assert not channel._channel_reply_is_optional(REQUIRED)
    assert channel._channel_reply_is_optional(ADDRESSED_ONLY)


def test_a_conversation_rule_beats_a_platform_rule():
    channel, _ = _channel(
        (
            {"match": {"channel": "slack"}, "delivery": {"reply": REPLY_OPTIONAL}},
            {
                "match": {"channel": "slack", "chat": REQUIRED},
                "delivery": {"reply": REPLY_REQUIRED},
            },
        )
    )

    assert channel._channel_reply_is_optional(OPTIONAL)
    assert not channel._channel_reply_is_optional(REQUIRED)


def test_a_word_the_vocabulary_does_not_hold_leaves_the_layer_below_answering():
    warned: list[str] = []
    scopes = compile_scopes(
        [
            {
                "match": {"channel": "slack", "chat": OPTIONAL},
                "delivery": {"reply": "sometimes"},
            }
        ],
        warn=lambda message, *args: warned.append(message % args if args else message),
    )
    platform, per_chat = apply_scopes_to_slack_overrides(
        {"group_chat_mode": "mention"}, scopes=scopes
    )
    channel = SlackChannel(
        SlackChannelConfig(
            enabled=True,
            conversation_overrides=per_chat,
            platform_override=platform,
            scopes=scopes,
        ),
        RobotMessageRouter(),
    )

    assert not channel._channel_reply_is_optional(OPTIONAL)
    assert any("reply" in line and "sometimes" in line for line in warned)


def test_a_rule_naming_a_sender_is_refused():
    """The two halves see different things, so the key takes no identity axis.

    The instruction is appended where the sender is known; the reply is matched
    where nothing says which sender it answers. Settled per person, the token
    would be posted at the very people told to write it.
    """
    warned: list[str] = []
    scopes = compile_scopes(
        [
            {
                "match": {"channel": "slack", "chat": OPTIONAL, "user": [ASKER]},
                "delivery": {"reply": REPLY_OPTIONAL},
            }
        ],
        warn=lambda message, *args: warned.append(message % args if args else message),
    )
    platform, per_chat = apply_scopes_to_slack_overrides(
        {"group_chat_mode": "mention"}, scopes=scopes
    )

    assert (per_chat.get(OPTIONAL) or platform).reply is None
    assert any("reply" in line for line in warned)


def test_a_direct_message_is_never_licensed_to_stay_silent():
    """Even where a platform rule licensed everything.

    In a one-to-one conversation an unanswered message reads as the bot being
    broken rather than as a judgement, and there is no third party for the
    silence to spare. The id prefix refuses it ahead of the settled word, so no
    configuration reaches past it.
    """
    channel, _ = _channel(
        ({"match": {"channel": "slack"}, "delivery": {"reply": REPLY_OPTIONAL}},)
    )

    assert not channel._channel_reply_is_optional(DM)


def test_only_the_conversations_that_wrote_the_key_are_reached():
    """The blast radius, enumerated rather than argued.

    The instruction is appended to every message in a licensed conversation, so
    one that never asked for it would pay for it on every turn.
    """
    channel, _ = _channel()
    conversations = (
        OPTIONAL,
        REQUIRED,
        ADDRESSED_ONLY,
        "C0NEVERNAMED",
        "G0PRIVATE001",
        DM,
    )

    reached = [
        chat for chat in conversations if channel._channel_reply_is_optional(chat)
    ]

    assert reached == [OPTIONAL, ADDRESSED_ONLY]


def test_the_startup_summary_names_a_conversation_that_may_decline():
    channel, _ = _channel()

    summary = describe_configured_channels(channel.config)

    assert f"{OPTIONAL} " in summary
    assert f"reply={REPLY_OPTIONAL}" in summary
    # The room that wrote nothing is named without the key, because an unset key
    # is already the default rather than a departure from it.
    assert f"{REQUIRED} mode=[all] prompt=none model=default via=" in summary


# --------------------------------------------------------------------------
# The instruction
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_instruction_reaches_a_licensed_conversation():
    channel, received = _channel()

    await _post(channel, _room_event(ts="1710000010.000100"), "Ev01")

    assert NO_REPLY_INSTRUCTION in received[0].params["content"]
    assert received[0].metadata[SLACK_REPLY_OPTIONAL_KEY] is True


@pytest.mark.asyncio
async def test_the_instruction_stays_out_of_a_conversation_that_owes_an_answer():
    channel, received = _channel()

    await _post(
        channel, _room_event(channel_id=REQUIRED, ts="1710000010.000100"), "Ev01"
    )

    assert NO_REPLY_SENTINEL not in received[0].params["content"]
    assert SLACK_REPLY_OPTIONAL_KEY not in received[0].metadata


@pytest.mark.asyncio
async def test_the_instruction_sits_below_an_operator_prompt_rather_than_replacing_it():
    """Both texts are present, in that order, separated by a blank line.

    Exactly one standing prompt is appended, and this is not one: it is
    connector-authored text about the delivery path, so it composes by sitting
    beside whichever operator prompt won. Last, because the appended text is the
    more recent and recency helps compliance, and because the two answer
    different questions -- the operator's says when to speak, this one says how
    to say nothing.
    """
    channel, received = _channel(
        (
            {
                "match": {"channel": "slack", "chat": OPTIONAL},
                "delivery": {
                    "mode": ["all"],
                    "reply": REPLY_OPTIONAL,
                    "prompt": "Answer in French.",
                },
            },
        )
    )

    await _post(channel, _room_event(ts="1710000010.000100"), "Ev01")

    content = received[0].params["content"]
    assert content.endswith(f"Answer in French.\n\n{NO_REPLY_INSTRUCTION}")


# --------------------------------------------------------------------------
# A message that addressed the bot
# --------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "chat, addressed",
    [(OPTIONAL, False), (OPTIONAL, True), (REQUIRED, False), (REQUIRED, True)],
)
async def test_the_offer_and_the_matcher_are_settled_separately(chat, addressed):
    """The four combinations, and both halves of each.

    The offer is the room's licence *and* the fact that nothing addressed the
    bot. The matcher is the room's licence alone. Nothing the model writes can
    reach either: a room that licensed no silence posts the token as text
    whoever sent the message, which is the key's "narrow, never grant" rule
    read from the outside.
    """
    channel, received = _channel()
    licensed = chat == OPTIONAL

    posts = await _answer(
        channel,
        received,
        NO_REPLY_SENTINEL,
        channel_id=chat,
        trigger="mention" if addressed else "all",
        text=f"<@{BOT}> what broke?" if addressed else "shipping it now",
    )

    assert (NO_REPLY_INSTRUCTION in received[-1].params["content"]) is (
        licensed and not addressed
    )
    assert received[-1].metadata.get(SLACK_REPLY_OPTIONAL_KEY, False) is licensed
    assert [post["text"] for post in posts] == ([] if licensed else [NO_REPLY_SENTINEL])


@pytest.mark.asyncio
async def test_a_message_opening_with_a_mention_is_offered_no_contract():
    """The offer, and only the offer, turns on the mention.

    The matcher is armed on the same message: the turn is not told the token
    exists, and a turn that knows it anyway from the rest of the conversation
    has it honoured.
    """
    channel, received = _channel()

    await _post(
        channel,
        _room_event(ts="1710000010.000100", text=f"<@{BOT}> what broke?"),
        "Ev01",
        trigger="mention",
    )

    assert NO_REPLY_SENTINEL not in received[0].params["content"]
    assert received[0].metadata[SLACK_REPLY_OPTIONAL_KEY] is True


@pytest.mark.asyncio
async def test_the_mention_is_read_before_it_is_stripped_from_the_text():
    """The ordering this feature has to get right.

    A ``mention`` dispatch strips the leading mention out of the text before the
    instruction would be appended, so a check made at the append site would
    answer "not addressed" for every message that was. The dispatched text no
    longer holds the mention, and the fragment is still withheld.
    """
    channel, received = _channel()

    await _post(
        channel,
        _room_event(ts="1710000010.000100", text=f"<@{BOT}> what broke?"),
        "Ev01",
        trigger="mention",
    )

    content = received[0].params["content"]
    assert f"<@{BOT}>" not in content
    assert content.startswith("what broke?")
    assert NO_REPLY_INSTRUCTION not in content


@pytest.mark.asyncio
async def test_a_mention_further_into_a_message_is_a_reference_and_stays_optional():
    """A reference to the bot is not an address to it.

    Both surveyed systems draw the line at the opening token, and
    ``_has_leading_bot_mention`` is anchored so that it draws the same one.
    """
    channel, received = _channel()

    await _post(
        channel,
        _room_event(ts="1710000010.000100", text=f"ask <@{BOT}> if you like"),
        "Ev01",
    )

    assert NO_REPLY_INSTRUCTION in received[0].params["content"]
    assert received[0].metadata[SLACK_REPLY_OPTIONAL_KEY] is True


@pytest.mark.asyncio
async def test_an_addressed_message_answered_with_the_token_posts_nothing(warnings):
    """The case the split exists for.

    A person wrote that the answer had been enough. The turn judged that there
    was nothing left to say and wrote the token, which was posted into the
    channel as the bare word because the matcher had been disarmed along with
    the offer. The judgement was right and had no way to be expressed. It does
    now.
    """
    channel, received = _channel()

    posts = await _answer(
        channel,
        received,
        NO_REPLY_SENTINEL,
        trigger="mention",
        text=f"<@{BOT}> what broke?",
    )

    assert posts == []
    assert warnings == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "addressed, stamped",
    [(True, "addressed=yes"), (False, "addressed=no")],
)
async def test_the_withheld_line_says_whether_the_bot_was_addressed(
    addressed, stamped, notes
):
    """The silence an operator hunts for is the addressed one.

    Both are licensed and neither is an error, so the line is the same INFO
    line either way -- with one field that tells them apart, because only one
    of them ends with a person who wrote the bot's name seeing nothing back.
    """
    channel, received = _channel()

    posts = await _answer(
        channel,
        received,
        NO_REPLY_SENTINEL,
        trigger="mention" if addressed else "all",
        text=f"<@{BOT}> what broke?" if addressed else "shipping it now",
    )

    assert posts == []
    written = [line for line in notes if "nothing posted" in line]
    assert len(written) == 1
    assert stamped in written[0]


# --------------------------------------------------------------------------
# Delivery
# --------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reply",
    [
        NO_REPLY_SENTINEL,
        NO_REPLY_SENTINEL.lower(),
        f"`{NO_REPLY_SENTINEL}`",
        f"({NO_REPLY_SENTINEL})",
        f"{NO_REPLY_SENTINEL}.",
        f"​ {NO_REPLY_SENTINEL} ​",
    ],
)
async def test_the_token_posts_nothing(reply):
    channel, received = _channel()

    assert await _answer(channel, received, reply) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("reply", [DESCRIBED_SILENCE, DESCRIBED_SILENCE_AT_LENGTH])
async def test_a_reply_describing_its_own_silence_is_posted_and_counted(
    reply, warnings
):
    """Delivered unchanged, and noticed. Both halves matter.

    Delivered, because it is not the token and the matcher deletes nothing else.
    Noticed, because these two are exactly the evidence the design is judged on.
    """
    channel, received = _channel()

    posts = await _answer(channel, received, reply)

    assert [post["text"] for post in posts] == [reply]
    assert any(repr(reply) in line for line in warnings)


@pytest.mark.asyncio
async def test_a_real_answer_ending_in_the_token_is_posted_whole(warnings):
    """The anchors are the safety property, and this is what they buy.

    A model that appended the token to a genuine answer would lose that answer to
    a matcher without them.
    """
    channel, received = _channel()
    reply = f"Deploy is green, two checks pending.\n\n{NO_REPLY_SENTINEL}"

    posts = await _answer(channel, received, reply)

    assert [post["text"] for post in posts] == [reply]
    assert any(repr(reply) in line and "near miss" in line for line in warnings)


@pytest.mark.asyncio
async def test_a_reply_discussing_the_token_in_prose_is_posted_and_counted(warnings):
    channel, received = _channel()
    reply = f"Write {NO_REPLY_SENTINEL} on its own line when you have nothing to add."

    posts = await _answer(channel, received, reply)

    assert [post["text"] for post in posts] == [reply]
    assert any("near miss" in line for line in warnings)


@pytest.mark.asyncio
async def test_two_tokens_in_a_row_are_posted_as_text(warnings):
    channel, received = _channel()
    reply = f"{NO_REPLY_SENTINEL} {NO_REPLY_SENTINEL}"

    posts = await _answer(channel, received, reply)

    assert [post["text"] for post in posts] == [reply]
    assert any("near miss" in line for line in warnings)


@pytest.mark.asyncio
async def test_an_ordinary_reply_is_posted_unchanged_and_says_nothing_extra(warnings):
    channel, received = _channel()

    posts = await _answer(channel, received, "Two checks failed on the last run.")

    assert [post["text"] for post in posts] == ["Two checks failed on the last run."]
    assert warnings == []


@pytest.mark.asyncio
async def test_a_conversation_that_owes_an_answer_posts_the_token_as_text(warnings):
    """Neither half is in force there, so the reverse direction has no half-state.

    No instruction was appended, so a reply that happens to be the token is text
    somebody wrote. Nothing is counted either: the contract was never in play, so
    there is no near miss to count.
    """
    channel, received = _channel()

    posts = await _answer(
        channel, received, NO_REPLY_SENTINEL, channel_id=REQUIRED
    )

    assert [post["text"] for post in posts] == [NO_REPLY_SENTINEL]
    assert warnings == []


@pytest.mark.asyncio
async def test_a_reply_with_no_text_still_posts_nothing():
    """The path this feature sits beside, unchanged."""
    channel, received = _channel()

    assert await _answer(channel, received, "") == []


@pytest.mark.asyncio
async def test_a_reply_that_never_named_a_licensed_turn_is_posted():
    """A push with no inbound message behind it was offered no contract.

    A scheduled run, a health check, or a reply whose metadata did not survive
    the round trip: absent means the answer is owed, so every way this can be
    wrong ends in text being posted.
    """
    channel, _ = _channel()
    reply = Message(
        id="cron-1",
        type="event",
        channel_id="slack",
        session_id=f"slack_{TEAM}_{OPTIONAL}",
        params={},
        timestamp=time.time(),
        ok=True,
        payload={
            "event_type": EventType.CHAT_FINAL.value,
            "content": NO_REPLY_SENTINEL,
        },
        event_type=EventType.CHAT_FINAL,
        metadata={"slack_channel_id": OPTIONAL},
    )

    await channel.send(reply)

    assert [post["text"] for post in channel._client.posts] == [NO_REPLY_SENTINEL]


# --------------------------------------------------------------------------
# What a withheld reply leaves behind
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_withheld_reply_is_said_once_in_the_log(notes):
    channel, received = _channel()

    await _answer(channel, received, NO_REPLY_SENTINEL)

    written = [line for line in notes if NO_REPLY_SENTINEL in line and OPTIONAL in line]
    assert len(written) == 1
    assert "nothing posted" in written[0]


@pytest.mark.asyncio
async def test_a_silent_turn_is_marked_exactly_like_one_that_replied():
    """Silence ends a turn, and the mark says so without saying which turn it was.

    The acknowledgement comes off and the completion mark goes on, the same as
    for a turn that spoke. That answers both halves of the question at once. The
    message is not left bare, which is what would claim it was never processed
    and is the state a person re-sends into; and the model's judgement is not
    broadcast, because the mark is the one every finished turn gets. A reader
    cannot tell the two apart, which is the point -- a distinct emoji for silence
    would be a reply by other means.
    """
    silent, silent_received = _channel()
    silent_posts = await _answer(
        silent, silent_received, NO_REPLY_SENTINEL, ts="1710000010.000100"
    )

    spoken, spoken_received = _channel()
    spoken_posts = await _answer(
        spoken, spoken_received, "Here is the answer.", ts="1710000020.000100"
    )

    def marks(client: _RecordingSlackClient) -> tuple[list[str], list[str]]:
        return (
            [added[2] for added in client.added],
            [removed[2] for removed in client.removed],
        )

    assert marks(silent._client) == marks(spoken._client)
    assert marks(silent._client) == (["eyes", "heavy_check_mark"], ["eyes"])
    assert [added[0] for added in silent._client.added] == [OPTIONAL, OPTIONAL]
    assert silent_posts == []
    assert spoken_posts != []


@pytest.mark.asyncio
async def test_a_withheld_reply_takes_its_streamed_preview_off_the_screen(notes):
    """A silence that leaves the token on screen is not a silence.

    Streaming is on in the conversations this runs in, so the deltas have already
    drawn the token into a message by the time the turn declines. Returning
    without posting would leave that message standing.
    """
    channel, received = _channel(enable_streaming=True)
    await _post(channel, _room_event(ts="1710000010.000100"), "Ev01")
    request = received[-1]

    await channel.send(_delta(request, f"{NO_REPLY_SENTINEL}\n"))
    await _settle()
    preview = list(channel._client.posts)
    channel._client.posts.clear()

    await channel.send(_reply_to(request, NO_REPLY_SENTINEL))

    assert len(preview) == 1
    assert channel._client.posts == []
    assert channel._client.deleted == [(OPTIONAL, "1710000099.000001")]
    assert any("preview deleted" in line for line in notes)


@pytest.mark.asyncio
async def test_a_delivered_reply_keeps_the_preview_it_was_written_into():
    """The preview is deleted only on the turns that decline.

    Never opening one under the contract would cost every turn in the room its
    live preview to handle the minority that say nothing.
    """
    channel, received = _channel(enable_streaming=True)
    await _post(channel, _room_event(ts="1710000010.000100"), "Ev01")
    request = received[-1]

    await channel.send(_delta(request, "Deploy is \n"))
    await _settle()
    await channel.send(_reply_to(request, "Deploy is green."))

    assert channel._client.deleted == []
    assert [update["text"] for update in channel._client.updates][-1] == (
        "Deploy is green."
    )


@pytest.mark.asyncio
async def test_a_withheld_reply_leaves_no_open_stream_behind():
    """Closing the stream is what stops the entry leaking until the map is reaped.

    It is also what makes the delete safe: the close waits out any write still in
    flight, so nothing can land on the message after it is gone.
    """
    channel, received = _channel(enable_streaming=True)
    await _post(channel, _room_event(ts="1710000010.000100"), "Ev01")
    request = received[-1]

    await channel.send(_delta(request, f"{NO_REPLY_SENTINEL}\n"))
    await _settle()
    await channel.send(_reply_to(request, NO_REPLY_SENTINEL))

    assert channel._streams == {}


def _delta(request: Message, content: str) -> Message:
    """One token of a reply being written, addressed like the reply itself."""
    return Message(
        id=request.id,
        type="event",
        channel_id="slack",
        session_id=request.session_id,
        params={},
        timestamp=time.time(),
        ok=True,
        payload={"event_type": EventType.CHAT_DELTA.value, "content": content},
        event_type=EventType.CHAT_DELTA,
        metadata=dict(request.metadata or {}),
    )


# --------------------------------------------------------------------------
# The activity card a silent turn leaves behind, and does not
# --------------------------------------------------------------------------


def _tool_call(request: Message, call_id: str = "call-1") -> Message:
    """One ordinary tool call, addressed like the reply to ``request``.

    Any tracked work will do -- the card exists for the turn, not for the tool
    -- and one call is the cheapest way to put a card on screen.
    """
    return Message(
        id=request.id,
        type="event",
        channel_id="slack",
        session_id=request.session_id,
        params={},
        timestamp=time.time(),
        ok=True,
        payload={
            "event_type": EventType.CHAT_TOOL_CALL.value,
            "tool_call": {
                "name": "read_file",
                "arguments": {"path": "/etc/hosts"},
                "tool_call_id": call_id,
            },
        },
        event_type=EventType.CHAT_TOOL_CALL,
        metadata=dict(request.metadata or {}),
    )


def _error_final(request: Message, error: str) -> Message:
    """The turn dying, shaped as the gateway shapes it: ``payload["error"]``."""
    return Message(
        id=request.id,
        type="event",
        channel_id="slack",
        session_id=request.session_id,
        params={},
        timestamp=time.time(),
        ok=False,
        payload={"event_type": EventType.CHAT_ERROR.value, "error": error},
        event_type=EventType.CHAT_ERROR,
        metadata=dict(request.metadata or {}),
    )


async def _turn_with_a_card(
    channel_id: str = OPTIONAL,
    ts: str = "1710000010.000100",
    **overrides: Any,
) -> tuple[SlackChannel, Message]:
    """A licensed turn that has done tracked work, with its card on screen."""
    channel, received = _channel(
        activity_card_delay_seconds=0.0,
        activity_card_min_edit_seconds=0.0,
        **overrides,
    )
    await _post(channel, _room_event(channel_id=channel_id, ts=ts), f"Ev{ts}")
    request = received[-1]
    await channel.send(_tool_call(request))
    await _settle()
    assert [post.get("channel") for post in channel._client.posts] == [channel_id]
    channel._client.posts.clear()
    channel._client.updates.clear()
    return channel, request


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reply",
    [
        NO_REPLY_SENTINEL,
        f"`{NO_REPLY_SENTINEL}`",
        "",
        "   \n\t ",
    ],
)
async def test_a_silent_turn_takes_its_activity_card_off_the_screen(reply, notes):
    """Every way a licensed turn renders to no message loses the card.

    The token is the licensed form and the empty reply is its sibling: both
    return from ``send`` without posting, and a card left behind either one says
    the same thing -- that a turn ran and there is nothing beside it. A rule
    firing on one and not the other would leave the card standing on the cases
    that look identical from the channel.
    """
    channel, request = await _turn_with_a_card()

    await channel.send(_reply_to(request, reply))

    assert channel._client.deleted == [(OPTIONAL, "1710000099.000001")]
    assert channel._client.posts == []
    assert channel._client.updates == []
    assert channel._activity_records == {}
    assert any("activity card deleted" in line for line in notes)


@pytest.mark.asyncio
async def test_the_card_is_deleted_rather_than_settled_and_then_deleted():
    """One call, not two. The delete replaces the settling rewrite.

    The decision is taken before the card is closed, which is the whole reason
    it can replace a write instead of following one.
    """
    channel, request = await _turn_with_a_card()

    await channel.send(_reply_to(request, NO_REPLY_SENTINEL))

    assert len(channel._client.updates) == 0
    assert len(channel._client.deleted) == 1


@pytest.mark.asyncio
async def test_a_turn_that_replied_keeps_the_card_it_settled():
    channel, request = await _turn_with_a_card()

    await channel.send(_reply_to(request, "Two checks failed on the last run."))

    assert channel._client.deleted == []
    assert len(channel._client.updates) == 1
    assert [post["text"] for post in channel._client.posts] == [
        "Two checks failed on the last run."
    ]


@pytest.mark.asyncio
async def test_a_silent_turn_that_never_posted_a_card_makes_no_call():
    """The delay is the answer to "when to emit nothing", and it still is.

    A turn that finishes inside ``activity_card_delay_seconds`` has nothing on
    screen, so there is nothing to delete and no call worth spending to find
    that out -- and nothing is posted just so that it can be taken down again.
    """
    channel, received = _channel(
        activity_card_delay_seconds=30.0, activity_card_min_edit_seconds=0.0
    )
    await _post(channel, _room_event(ts="1710000010.000100"), "Ev01")
    request = received[-1]
    await channel.send(_tool_call(request))
    await _settle()
    assert channel._client.posts == []

    await channel.send(_reply_to(request, NO_REPLY_SENTINEL))

    assert channel._client.posts == []
    assert channel._client.updates == []
    assert channel._client.deleted == []
    assert channel._activity_records == {}


@pytest.mark.asyncio
async def test_a_refused_delete_leaves_a_settled_card_and_warns(warnings):
    """Best effort, and the fallback is what the card did before this existed.

    A workspace that refuses the delete must not be left with a card captioned
    mid-turn above a message that never came, which is the one failure the
    record exists to prevent and is worse than the card the delete wanted gone.
    """
    channel, request = await _turn_with_a_card()

    async def refuse(**kwargs: Any) -> dict[str, bool]:
        raise RuntimeError("cant_delete_message")

    channel._client.chat_delete = refuse

    await channel.send(_reply_to(request, NO_REPLY_SENTINEL))

    assert channel._client.deleted == []
    assert len(channel._client.updates) == 1
    assert channel._client.posts == []
    assert any("could not be deleted" in line for line in warnings)


@pytest.mark.asyncio
async def test_a_failed_turn_keeps_its_card_and_the_failure_is_not_reposted():
    """A deleted card on a failed turn would suppress the failure outright.

    ``_close_activity_card``'s return value is what lets ``send`` stop posting
    the error as a message of its own. Delete the card and that caller believes
    a card is showing a failure that is no longer on screen, and the reader
    watches the turn vanish.
    """
    channel, request = await _turn_with_a_card()

    await channel.send(_error_final(request, "[181001] model call failed"))

    assert channel._client.deleted == []
    assert channel._client.posts == []
    assert len(channel._client.updates) == 1
    assert "model call failed" in str(channel._client.updates[-1])


@pytest.mark.asyncio
async def test_no_error_event_can_ask_for_a_discard():
    """The gate is the event type, not anything the payload says.

    ``_turn_says_nothing`` is what the caller passes, and a ``chat.error`` can
    never make it answer yes -- whatever text the failure happens to carry, the
    sentinel included.
    """
    channel, received = _channel()
    await _post(channel, _room_event(ts="1710000010.000100"), "Ev01")
    request = received[-1]

    assert channel._turn_says_nothing(_reply_to(request, NO_REPLY_SENTINEL), None)
    assert not channel._turn_says_nothing(
        _error_final(request, NO_REPLY_SENTINEL), None
    )
    assert not channel._turn_says_nothing(_error_final(request, ""), None)


@pytest.mark.asyncio
async def test_a_record_carrying_a_failure_refuses_the_delete_on_its_own_account():
    """The second guard, independent of the caller's.

    Unreachable through ``send`` -- nothing resumes a request after an error --
    which is why it is written down rather than left to the two gates above it.
    """
    channel, request = await _turn_with_a_card()
    key = channel._activity_key(_reply_to(request, ""), None)
    record = channel._activity_records[key]
    record.failed = True
    record.harness_error = "[181001] model call failed"

    assert not await channel._discard_activity_card(key, record, reason="silence")
    assert channel._client.deleted == []
    assert channel._activity_records[key] is record


@pytest.mark.asyncio
async def test_a_stopped_turn_keeps_the_card_that_says_it_was_stopped():
    """A stop settles the record itself, so the terminal that follows adds nothing.

    The card is what shows the reader what was in flight when the stop landed,
    which is the same thing worth keeping that a failure's card is.
    """
    channel, request = await _turn_with_a_card()
    key = channel._activity_key(_reply_to(request, ""), None)
    record = channel._activity_records[key]
    record.stop_requested = True
    await channel._settle_stopped_card(record)
    assert len(channel._client.updates) == 1

    await channel.send(_reply_to(request, NO_REPLY_SENTINEL))

    assert channel._client.deleted == []
    assert len(channel._client.updates) == 1
    assert channel._activity_records[key].stopped


@pytest.mark.asyncio
async def test_a_room_that_never_licensed_a_silence_keeps_its_card():
    """Outside the contract an empty reply is not a judgement anybody made.

    It is an event that came back wrong, and the card is then the only record
    that the turn ran at all.
    """
    channel, request = await _turn_with_a_card(channel_id=REQUIRED)

    await channel.send(_reply_to(request, ""))

    assert channel._client.deleted == []
    assert len(channel._client.updates) == 1
