"""The stop button: Slack's second gesture, and who is allowed to use it.

Slack has one ambient action -- posting a message -- and the gateway already
reads that as "cancel the running turn". So "stop, this is wrong" and "here is
one more detail" arrive identically and are resolved identically, in favour of
the first, and there is nowhere to refuse either of them. A message cannot be
gated on who sent it without also gating the conversation.

The button is the second gesture. It means only "stop", and because it is a
click rather than a message it holds a principal the connector can check. What
it checks is two clauses:

* whoever started a turn may stop it -- an invariant floor, not a rule anybody
  wrote and not one any rule can take away; and
* failing that, the allow list, which is what layer 0 already gates.

The floor needs a *recorded* starter and an identified clicker. Where either is
missing the floor does not arise and the allow list decides alone -- which is a
different thing from a refusal, and the tests below hold that distinction: a
connector that cannot say who started a turn has not said that nobody may stop
it.

Nothing here changes the ungated path. An ordinary message into a running
session still cancels it with no check at all, and in a shared channel thread
that is still a second person's message cancelling the first person's turn.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Iterator

import pytest

from jiuwenswarm.common.schema.message import EventType, Message, ReqMethod
from jiuwenswarm.common.scopes import MID_TURN_CANCEL
from jiuwenswarm.gateway.channel_manager.base import RobotMessageRouter
from jiuwenswarm.gateway.channel_manager.im_platforms.slack import slack_connect
from jiuwenswarm.gateway.channel_manager.im_platforms.slack.slack_connect import (
    SlackChannel,
    SlackChannelConfig,
)

ASKER = "U0ASKER001"
BYSTANDER = "U0OTHER002"
OUTSIDER = "U0OUTSIDE3"
TEAM = "T0TESTTEAM"
ROOM = "C0CHANNEL1"
THREAD = "1710000001.000100"
SESSION = f"slack_{TEAM}_{ROOM}_{THREAD}"
REQUEST_ID = "req-7f3a"


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


class _RecordingSlackClient:
    def __init__(self) -> None:
        self.posts: list[dict[str, Any]] = []
        self.updates: list[dict[str, Any]] = []
        self.ephemerals: list[dict[str, Any]] = []
        self.added: list[tuple[str, str, str]] = []
        self.removed: list[tuple[str, str, str]] = []
        self.ts_counter = 0

    async def chat_postMessage(self, **kwargs: Any) -> dict[str, str]:
        self.posts.append(kwargs)
        self.ts_counter += 1
        return {"ts": f"1780000000.{self.ts_counter:06d}"}

    async def chat_update(self, **kwargs: Any) -> dict[str, str]:
        self.updates.append(kwargs)
        return {"ts": kwargs["ts"]}

    async def chat_postEphemeral(self, **kwargs: Any) -> dict[str, str]:
        self.ephemerals.append(kwargs)
        return {"ok": "true"}

    async def reactions_add(self, **kwargs: Any) -> dict[str, bool]:
        self.added.append(
            (kwargs.get("channel", ""), kwargs.get("timestamp", ""), kwargs.get("name", ""))
        )
        return {"ok": True}

    async def reactions_remove(self, **kwargs: Any) -> dict[str, bool]:
        self.removed.append(
            (kwargs.get("channel", ""), kwargs.get("timestamp", ""), kwargs.get("name", ""))
        )
        return {"ok": True}


class _Ack:
    async def __call__(self, *_args: Any, **_kwargs: Any) -> None:
        return None


def _channel(
    *, mid_turn: str = MID_TURN_CANCEL, **config: Any
) -> tuple[SlackChannel, _RecordingSlackClient, list[Message]]:
    config.setdefault("activity_card_delay_seconds", 0.0)
    config.setdefault("activity_card_min_edit_seconds", 0.0)
    client = _RecordingSlackClient()
    channel = SlackChannel(
        SlackChannelConfig(enabled=True, default_channel_id=ROOM, **config),
        RobotMessageRouter(),
    )
    # Set, not inherited. Everything in this file about a message superseding a
    # running turn needs the conversation to be cancelling, and cancel stopped
    # being what an unconfigured conversation does when the default became
    # queue. Patched at the seam rather than written as a scope so that these
    # tests keep testing the cancel path and not the settling cascade, which
    # test_slack_scopes covers.
    channel._mid_turn_mode = lambda *_a, **_kw: mid_turn  # type: ignore[assignment]
    channel._client = client
    channel._running = True
    dispatched: list[Message] = []
    channel.on_message(dispatched.append)
    return channel, client, dispatched


def _event(
    event_type: EventType,
    payload: dict[str, Any],
    *,
    request_id: str = REQUEST_ID,
    session_id: str = SESSION,
) -> Message:
    return Message(
        id=request_id,
        type="event",
        channel_id="slack",
        session_id=session_id,
        params={},
        timestamp=time.time(),
        ok=True,
        payload={"event_type": event_type.value, **payload},
        event_type=event_type,
        metadata={"slack_channel_id": ROOM},
    )


def _tool_call(call_id: str = "call-1", **kw: Any) -> Message:
    """One ordinary tool call, which is what opens a turn's activity record."""
    return _event(
        EventType.CHAT_TOOL_CALL,
        {
            "tool_call": {
                "name": "bash_tool",
                "arguments": {"command": "ls"},
                "tool_call_id": call_id,
            }
        },
        **kw,
    )


def _final(**kw: Any) -> Message:
    return _event(EventType.CHAT_FINAL, {"content": "Done."}, **kw)


async def _settle() -> None:
    """Let the delayed-post task run. The delay is zero in these tests."""
    for _ in range(4):
        await asyncio.sleep(0)


async def _card(channel: SlackChannel, client: _RecordingSlackClient) -> None:
    """Put a card on screen for a turn that is running."""
    await channel.send(_tool_call())
    await _settle()
    assert client.posts, "the card should have been posted"


def _last_write(client: _RecordingSlackClient) -> dict[str, Any]:
    return client.updates[-1] if client.updates else client.posts[-1]


def _stop_button(call: dict[str, Any]) -> dict[str, Any] | None:
    for block in call.get("blocks") or []:
        if block.get("type") != "actions":
            continue
        for element in block.get("elements") or []:
            if element.get("action_id") == slack_connect._STOP_ACTION_ID:
                return element
    return None


def _click(
    *,
    user_id: str,
    value: str | None = None,
    channel_id: str = ROOM,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """A ``block_actions`` payload for the stop button, as Slack sends one."""
    if value is None:
        value = json.dumps(
            {
                "session_id": SESSION,
                "request_id": REQUEST_ID,
                "channel_id": channel_id,
            },
            separators=(",", ":"),
        )
    action = {
        "type": "button",
        "action_id": slack_connect._STOP_ACTION_ID,
        "value": value,
        "text": {"type": "plain_text", "text": "Stop"},
    }
    body: dict[str, Any] = {
        "type": "block_actions",
        "team": {"id": TEAM},
        "channel": {"id": channel_id},
        "container": {
            "type": "message",
            "channel_id": channel_id,
            "message_ts": "1780000000.000001",
        },
        "message": {"ts": "1780000000.000001", "blocks": []},
        "actions": [action],
    }
    if user_id:
        body["user"] = {"id": user_id}
    return body, action


def _cancels(dispatched: list[Message]) -> list[Message]:
    return [msg for msg in dispatched if msg.req_method == ReqMethod.CHAT_CANCEL]


# ── The gesture ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_running_turn_gets_a_stop_button() -> None:
    """The card is the per-turn surface, so the control lives on it."""
    channel, client, _dispatched = _channel()

    await _card(channel, client)

    button = _stop_button(client.posts[0])
    assert button is not None
    assert button["text"]["text"] == "Stop"
    # Red, because it destroys work.
    assert button["style"] == "danger"


@pytest.mark.asyncio
async def test_the_button_names_the_session_it_would_cancel() -> None:
    """The value is what the click comes back with, and it is ours end to end.

    The session is what the gateway cancels on -- it has no notion of a request
    to cancel -- and the request and channel are what the click is matched back
    to its record with. None of the three is read from model output.
    """
    channel, client, _dispatched = _channel()

    await _card(channel, client)

    button = _stop_button(client.posts[0])
    assert button is not None
    assert json.loads(button["value"]) == {
        "session_id": SESSION,
        "request_id": REQUEST_ID,
        "channel_id": ROOM,
    }


@pytest.mark.asyncio
async def test_the_button_goes_when_the_turn_ends() -> None:
    """A control that outlived its turn would stop whatever ran next.

    The session in a channel thread is shared, so the next turn on it may be
    somebody else's work entirely.
    """
    channel, client, _dispatched = _channel()
    await _card(channel, client)
    assert _stop_button(client.posts[0]) is not None

    await channel.send(_final())
    await _settle()

    assert client.updates, "the card is rewritten when the turn ends"
    assert _stop_button(_last_write(client)) is None


@pytest.mark.asyncio
async def test_a_card_carrying_a_control_is_not_treated_as_chrome() -> None:
    """A refused rendering costs a status line, or costs the only stop there is.

    The two are not the same failure, and the block kind is what tells them
    apart: a button's value is this connector's internals and must never be
    quoted back at a reader.
    """
    channel, client, _dispatched = _channel()
    await _card(channel, client)

    key = (REQUEST_ID, ROOM)
    record = channel._activity_records[key]
    assert channel._activity_block_kind(record) == (
        slack_connect.slack_blocks.BLOCK_KIND_INTERACTIVE
    )

    record.closed = True
    assert channel._activity_block_kind(record) == (
        slack_connect.slack_blocks.BLOCK_KIND_CHROME
    )


# ── The floor: whoever started a turn may stop it ────────────────────────────


@pytest.mark.asyncio
async def test_the_starter_may_stop_their_own_turn() -> None:
    """The floor holds where the allow list alone would refuse.

    ``allow_from`` here names someone else entirely, which is the shape of an
    allow list that changed while a turn was in flight. The starter is permitted
    anyway: their right is a floor, not something the configuration confers.
    """
    channel, client, dispatched = _channel(allow_from=[BYSTANDER])
    await _card(channel, client)
    channel._remember_turn_initiator(SESSION, ASKER, REQUEST_ID, is_dm=False, chat_type="channel")

    body, action = _click(user_id=ASKER)
    await channel._handle_stop_action(_Ack(), body, action)

    (cancel,) = _cancels(dispatched)
    assert cancel.session_id == SESSION
    assert cancel.params["intent"] == "cancel"


@pytest.mark.asyncio
async def test_a_stop_reaches_the_runtime_as_chat_interrupt() -> None:
    """The same request Web, CLI, TUI and ACP send; only this half was missing."""
    channel, client, dispatched = _channel()
    await _card(channel, client)
    channel._remember_turn_initiator(SESSION, ASKER, REQUEST_ID, is_dm=False, chat_type="channel")

    body, action = _click(user_id=ASKER)
    await channel._handle_stop_action(_Ack(), body, action)

    (cancel,) = _cancels(dispatched)
    assert cancel.req_method.value == "chat.interrupt"
    assert cancel.type == "req"
    # Answered once, not streamed.
    assert cancel.is_stream is False
    assert cancel.user_id == ASKER
    assert cancel.chat_id == ROOM
    assert cancel.metadata["slack_channel_id"] == ROOM
    assert cancel.metadata["slack_team_id"] == TEAM


@pytest.mark.asyncio
async def test_the_starter_is_permitted_even_after_the_card_says_nothing() -> None:
    """A steer or a quiet stretch does not cost the starter their floor.

    The check is against the initiator entry, which is opened at dispatch and
    closed by the turn's terminal event -- not against anything the card holds.
    """
    channel, client, dispatched = _channel(allow_from=[BYSTANDER])
    await _card(channel, client)
    channel._remember_turn_initiator(SESSION, ASKER, REQUEST_ID, is_dm=False, chat_type="channel")
    channel._activity_records[(REQUEST_ID, ROOM)].tool_runs.clear()

    body, action = _click(user_id=ASKER)
    await channel._handle_stop_action(_Ack(), body, action)

    assert len(_cancels(dispatched)) == 1


@pytest.mark.asyncio
async def test_someone_else_is_refused_where_the_allow_list_refuses_them() -> None:
    """The floor is the starter's alone. Everyone else is layer 0's business."""
    channel, client, dispatched = _channel(allow_from=[ASKER])
    await _card(channel, client)
    channel._remember_turn_initiator(SESSION, ASKER, REQUEST_ID, is_dm=False, chat_type="channel")

    body, action = _click(user_id=OUTSIDER)
    await channel._handle_stop_action(_Ack(), body, action)

    assert _cancels(dispatched) == []
    # Told to the person who clicked, and to nobody else: for everyone else in
    # the channel nothing happened.
    (note,) = client.ephemerals
    assert note["user"] == OUTSIDER
    assert note["text"] == slack_connect._STOP_REFUSED_NOTICE
    # And the button is still there, because the turn is still running.
    assert _stop_button(_last_write(client)) is not None


# ── The allowance needs a recorded starter ──────────────────────────────────


@pytest.mark.asyncio
async def test_with_no_recorded_initiator_the_allow_list_decides_alone() -> None:
    """No entry, so no clicker can claim to be the starter.

    Both halves of "decides alone" are asserted, because only the pair says the
    allow list is what is deciding: the member is permitted and the outsider is
    refused, on a session with nothing recorded either way.
    """
    channel, client, dispatched = _channel(allow_from=[BYSTANDER])
    await _card(channel, client)
    assert channel.turn_initiator(SESSION) is None

    body, action = _click(user_id=OUTSIDER)
    await channel._handle_stop_action(_Ack(), body, action)
    assert _cancels(dispatched) == []

    body, action = _click(user_id=BYSTANDER)
    await channel._handle_stop_action(_Ack(), body, action)
    assert len(_cancels(dispatched)) == 1


@pytest.mark.asyncio
async def test_no_recorded_initiator_is_not_read_as_a_refusal() -> None:
    """``None`` means this connector cannot say, never "nobody may stop it".

    It is the ordinary state for a session whose entry aged out, and reading it
    as a refusal would turn an aged-out entry into a silent denial. With no
    allow list -- the default -- layer 0 admits everyone and the stop goes
    through on a session with no initiator at all.
    """
    channel, client, dispatched = _channel()
    await _card(channel, client)
    assert channel.turn_initiator(SESSION) is None

    body, action = _click(user_id=OUTSIDER)
    await channel._handle_stop_action(_Ack(), body, action)

    assert len(_cancels(dispatched)) == 1
    assert client.ephemerals[-1]["text"] == slack_connect._STOP_ACCEPTED_NOTICE


@pytest.mark.asyncio
async def test_an_aged_out_entry_does_not_deny_the_person_it_named() -> None:
    """The same claim from the other side: the starter is not locked out either.

    An entry retired by the timeout leaves the turn exactly where a turn nobody
    recorded starts -- with the allow list deciding -- rather than in a state
    where the stop button refuses everyone.
    """
    channel, client, dispatched = _channel(allow_from=[ASKER])
    await _card(channel, client)
    channel._remember_turn_initiator(SESSION, ASKER, REQUEST_ID, is_dm=False, chat_type="channel")
    entry = channel._turn_initiators[SESSION]
    entry.started_at -= slack_connect._TURN_INITIATOR_TIMEOUT_SECONDS + 1
    assert channel.turn_initiator(SESSION) is None

    body, action = _click(user_id=ASKER)
    await channel._handle_stop_action(_Ack(), body, action)

    assert len(_cancels(dispatched)) == 1


@pytest.mark.asyncio
async def test_an_unnamed_clicker_cannot_be_the_starter() -> None:
    """The allowance belongs to an identified person.

    Written as an equality on two ids, an entry holding no user and a payload
    holding no user compare equal, and "whoever started it may stop it" becomes
    "anyone may stop it" -- on exactly the payload that has nothing to attribute
    the stop to. ``_remember_turn_initiator`` refuses to record an empty user
    today; the check must not be resting on that.
    """
    channel, client, dispatched = _channel(allow_from=[ASKER])
    await _card(channel, client)
    channel._turn_initiators[SESSION] = slack_connect._SlackTurnInitiator(
        user_id="", request_id=REQUEST_ID, is_dm=False, chat_type="channel"
    )
    assert channel.turn_initiator(SESSION) is not None

    body, action = _click(user_id="")
    await channel._handle_stop_action(_Ack(), body, action)

    assert _cancels(dispatched) == []


@pytest.mark.asyncio
async def test_an_unnamed_clicker_is_refused_by_the_allow_list_not_skipped() -> None:
    """The refusing shape, which is not the one the answer path takes.

    ``_handle_question_action`` guards with ``if user_id and not
    self.is_allowed(user_id)``, so a payload with no user skips the allow list
    there rather than failing it. That discrepancy is known and is not resolved
    here -- but a new control that destroys work does not inherit it.
    """
    channel, client, dispatched = _channel(allow_from=[ASKER, BYSTANDER])
    await _card(channel, client)

    body, action = _click(user_id="")
    await channel._handle_stop_action(_Ack(), body, action)

    assert _cancels(dispatched) == []
    assert channel._activity_records[(REQUEST_ID, ROOM)].stop_requested is False


# ── One stop per turn ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_second_click_stops_nothing_more() -> None:
    """Two people pressing, or one pressing twice, resolve to one cancel.

    The record is claimed before the dispatch, so the second click finds it
    taken. It matters more than a duplicate usually would: the cancel is
    addressed to a session, so a second one arriving later reaches whatever that
    session is running by then.
    """
    channel, client, dispatched = _channel()
    await _card(channel, client)

    body, action = _click(user_id=ASKER)
    await channel._handle_stop_action(_Ack(), body, action)
    await channel._handle_stop_action(_Ack(), body, action)

    assert len(_cancels(dispatched)) == 1
    assert client.ephemerals[-1]["text"] == slack_connect._STOP_STALE_NOTICE


@pytest.mark.asyncio
async def test_the_button_comes_off_the_card_once_a_stop_is_dispatched() -> None:
    """The claim is what stops the second click; the rewrite is what shows it."""
    channel, client, dispatched = _channel()
    await _card(channel, client)

    body, action = _click(user_id=ASKER)
    await channel._handle_stop_action(_Ack(), body, action)

    assert client.updates, "the card is rewritten without its button"
    assert _stop_button(_last_write(client)) is None


@pytest.mark.asyncio
async def test_a_stop_on_a_turn_that_has_ended_is_refused() -> None:
    """A card whose rewrite never landed still must not stop the next turn."""
    channel, client, dispatched = _channel()
    await _card(channel, client)
    await channel.send(_final())
    await _settle()

    body, action = _click(user_id=ASKER)
    await channel._handle_stop_action(_Ack(), body, action)

    assert _cancels(dispatched) == []
    assert client.ephemerals[-1]["text"] == slack_connect._STOP_STALE_NOTICE


@pytest.mark.asyncio
async def test_a_stop_naming_a_turn_this_process_never_tracked_is_refused() -> None:
    """A card posted by a process that has since restarted names nothing here.

    Forwarding it anyway would cancel by session on no evidence that the turn
    the button meant is still the one running.
    """
    channel, client, dispatched = _channel()
    await _card(channel, client)
    channel._activity_records.clear()

    body, action = _click(user_id=ASKER)
    await channel._handle_stop_action(_Ack(), body, action)

    assert _cancels(dispatched) == []


# ── Payloads this connector did not post ─────────────────────────────────────


@pytest.mark.asyncio
async def test_a_button_value_that_does_not_decode_stops_nothing() -> None:
    channel, client, dispatched = _channel()
    await _card(channel, client)

    for value in ("", "not json", "[]", json.dumps({"session_id": SESSION})):
        body, action = _click(user_id=ASKER, value=value)
        await channel._handle_stop_action(_Ack(), body, action)

    assert _cancels(dispatched) == []


@pytest.mark.asyncio
async def test_a_stopped_channel_acts_on_nothing() -> None:
    channel, client, dispatched = _channel()
    await _card(channel, client)
    channel._running = False

    body, action = _click(user_id=ASKER)
    await channel._handle_stop_action(_Ack(), body, action)

    assert _cancels(dispatched) == []


# ── Availability ─────────────────────────────────────────────────────────────


@pytest.fixture
def logged(monkeypatch) -> dict[str, list[str]]:
    """Everything this module logs, by level.

    The connector's logger does not propagate, so ``caplog`` sees nothing; the
    idiom the other Slack tests use is to record the calls themselves.
    """
    recorded: dict[str, list[str]] = {"info": [], "warning": []}

    def record(level: str):
        def sink(message: str, *args: Any, **kwargs: Any) -> None:
            recorded[level].append(message % args if args else message)

        return sink

    monkeypatch.setattr(slack_connect.logger, "info", record("info"))
    monkeypatch.setattr(slack_connect.logger, "warning", record("warning"))
    return recorded


def test_startup_says_the_stop_button_is_inactive_when_the_card_is_off(
    logged: dict[str, list[str]],
) -> None:
    """The button has no switch of its own, so it is told rather than found out.

    ``activity_card`` is where it lives and therefore what decides whether Slack
    has a gesture that means only "stop". Off, the channel is not without a way
    to halt a turn -- a message still cancels one -- but it is without the one
    that can be refused, and the first sign of that would otherwise be nobody
    ever being refused.
    """
    channel = SlackChannel(
        SlackChannelConfig(enabled=True, activity_card=False), RobotMessageRouter()
    )

    channel._log_stop_button_availability()

    (line,) = logged["warning"]
    assert "stop button inactive" in line
    assert "activity_card" in line


def test_startup_says_the_stop_button_is_active_when_the_card_is_on(
    logged: dict[str, list[str]],
) -> None:
    channel = SlackChannel(
        SlackChannelConfig(enabled=True, activity_card=True), RobotMessageRouter()
    )

    channel._log_stop_button_availability()

    assert logged["warning"] == []
    assert any("stop button active" in line for line in logged["info"])


# ── The card a stop leaves behind ────────────────────────────────────────────
#
# Every other ending on the card arrives as an event. A stop arrives as nothing:
# the cancel reaches ``_cancel_agent_work_for_session``, which cancels the
# gateway's own ``process_stream`` task, and that coroutine catches its
# ``CancelledError`` and returns without publishing a terminal. Its ``finally``
# emits a ``chat.processing_status``, which settles nothing, and the
# ``chat.interrupt_result`` that follows renders to no text. So the connector
# hears nothing further about the turn at all.
#
# That is what these tests hold: the card settles on the strength of the click,
# and no test below sends a terminal event before asserting that it did. In
# production one was waited for, and the card said "Working..." until somebody
# scrolled past it hours later.


def _plan(call: dict[str, Any]) -> dict[str, Any] | None:
    for block in call.get("blocks") or []:
        if block.get("type") == "plan":
            return block
    return None


def _title(client: _RecordingSlackClient) -> str:
    plan = _plan(_last_write(client))
    assert plan is not None, "the card should still carry a plan block"
    return str(plan["title"])


def _tool_result(call_id: str = "call-1", **kw: Any) -> Message:
    """The ordinary tool result, so a run is not left in flight."""
    return _event(
        EventType.CHAT_TOOL_RESULT,
        {"tool_name": "bash_tool", "tool_call_id": call_id, "result": "ok"},
        **kw,
    )


@pytest.mark.asyncio
async def test_a_stopped_turn_settles_its_card_with_no_terminal_event() -> None:
    """The regression. Nothing is sent to ``send`` after the click, ever.

    This is the production failure exactly: the runtime cancelled its tasks and
    the connector was never told anything again. A card that waits for a
    terminal here waits for one that the cancel path guarantees will not come.
    """
    channel, client, dispatched = _channel()
    await _card(channel, client)
    await channel.send(_tool_result())
    await _settle()

    body, action = _click(user_id=ASKER)
    await channel._handle_stop_action(_Ack(), body, action)
    await _settle()

    assert len(_cancels(dispatched)) == 1
    record = channel._activity_records[(REQUEST_ID, ROOM)]
    assert record.closed is True
    assert record.stopped is True
    assert client.updates, "the card must be rewritten by the stop itself"
    assert _title(client) == slack_connect._ACTIVITY_TITLE_STOPPED


@pytest.mark.asyncio
async def test_a_stopped_card_is_neither_finished_nor_failed() -> None:
    """A stop is a decision, not a completion and not a harness failure.

    "Turn finished" would claim the work ran to its answer, which is the one
    thing a stop guarantees it did not. "Turn failed" is the turn dying
    underneath itself, which reports somebody's decision as an accident.
    """
    channel, client, _dispatched = _channel()
    await _card(channel, client)
    await channel.send(_tool_result())
    await _settle()

    body, action = _click(user_id=ASKER)
    await channel._handle_stop_action(_Ack(), body, action)
    await _settle()

    title = _title(client)
    assert title == "Turn stopped"
    assert slack_connect._ACTIVITY_TITLE_DONE not in title
    assert slack_connect._ACTIVITY_TITLE_FAILED not in title


def test_the_stopped_title_is_ascii() -> None:
    """One non-ASCII character in a text element has taken a message down."""
    slack_connect._ACTIVITY_TITLE_STOPPED.encode("ascii")


@pytest.mark.asyncio
async def test_the_stopped_title_names_nobody() -> None:
    """These titles are harness-side statements, and a name is not one.

    The clicker is told by the ephemeral, which reaches exactly them. The card
    is read by everyone in the conversation, and a ``clicks.stop`` rule can let
    somebody other than the starter stop a turn -- publishing who exercised that
    would turn a permitted gesture into an attribution.
    """
    channel, client, _dispatched = _channel()
    await _card(channel, client)
    channel._remember_turn_initiator(SESSION, ASKER, REQUEST_ID, is_dm=False, chat_type="channel")

    body, action = _click(user_id=BYSTANDER)
    await channel._handle_stop_action(_Ack(), body, action)
    await _settle()

    written = json.dumps(_last_write(client))
    assert ASKER not in written
    assert BYSTANDER not in written
    # The one place the person who acted is named is addressed to them alone.
    assert client.ephemerals[-1]["user"] == BYSTANDER


@pytest.mark.asyncio
async def test_a_run_still_in_flight_at_the_stop_did_not_fail() -> None:
    """An in-flight tool was never heard from; it did not report a failure.

    The count named "n failed" means a tool told the runtime it had failed, and
    a run cancelled underneath itself never said any such thing. Saying it did
    is the kind of claim nothing established.
    """
    channel, client, _dispatched = _channel()
    await _card(channel, client)

    body, action = _click(user_id=ASKER)
    await channel._handle_stop_action(_Ack(), body, action)
    await _settle()

    title = _title(client)
    assert title == "Turn stopped, 1 reported no result"
    assert "failed" not in title
    record = channel._activity_records[(REQUEST_ID, ROOM)]
    assert record.tool_totals() == {slack_connect._RUN_UNREPORTED: 1}


@pytest.mark.asyncio
async def test_a_late_terminal_after_a_stop_neither_reopens_nor_relabels() -> None:
    """Settling twice must be harmless, whichever order it happens in.

    A terminal is not expected on this path at all -- that is the whole defect --
    but one arriving from somewhere unforeseen must not spend an edit re-saying
    what the card says, and must never caption a stopped turn "Turn finished".
    """
    channel, client, _dispatched = _channel()
    await _card(channel, client)
    await channel.send(_tool_result())
    await _settle()

    body, action = _click(user_id=ASKER)
    await channel._handle_stop_action(_Ack(), body, action)
    await _settle()
    writes_after_the_stop = len(client.updates)

    await channel.send(_final())
    await _settle()

    assert len(client.updates) == writes_after_the_stop
    record = channel._activity_records[(REQUEST_ID, ROOM)]
    assert record.closed is True
    assert record.stopped is True
    assert _title(client) == slack_connect._ACTIVITY_TITLE_STOPPED


@pytest.mark.asyncio
async def test_settling_a_stopped_card_twice_writes_once() -> None:
    """Idempotent from this side too, not only from the terminal's."""
    channel, client, _dispatched = _channel()
    await _card(channel, client)

    body, action = _click(user_id=ASKER)
    await channel._handle_stop_action(_Ack(), body, action)
    await _settle()
    writes = len(client.updates)

    record = channel._activity_records[(REQUEST_ID, ROOM)]
    await channel._settle_stopped_card(record)

    assert len(client.updates) == writes


@pytest.mark.asyncio
async def test_a_request_that_resumes_after_its_stop_is_not_still_stopped() -> None:
    """``stopped`` says why the card settled, so fresh work must clear it.

    Unlike ``failed``, which is kept because the card holds the only copy of the
    error, a stop leaves nothing to lose -- and a request that resumed and then
    finished really did finish. The button stays off, because ``stop_requested``
    is sticky and that claim is not returned.
    """
    channel, client, _dispatched = _channel()
    await _card(channel, client)
    await channel.send(_tool_result())
    await _settle()

    body, action = _click(user_id=ASKER)
    await channel._handle_stop_action(_Ack(), body, action)
    await _settle()
    assert _title(client) == slack_connect._ACTIVITY_TITLE_STOPPED

    await channel.send(_tool_call(call_id="call-2"))
    await _settle()
    record = channel._activity_records[(REQUEST_ID, ROOM)]
    assert record.closed is False
    assert record.stopped is False
    assert record.stop_requested is True
    assert _title(client) == slack_connect._ACTIVITY_TITLE_WORKING
    assert _stop_button(_last_write(client)) is None

    await channel.send(_tool_result(call_id="call-2"))
    await channel.send(_final())
    await _settle()

    assert _title(client).startswith(slack_connect._ACTIVITY_TITLE_DONE)


@pytest.mark.asyncio
async def test_a_stopped_card_carries_no_harness_error() -> None:
    """A stop is not a failure, so the card grows no ``Harness error`` section.

    The section exists for a turn that died with something to report. Nobody
    stopped that turn, and nobody has anything to read here.
    """
    channel, client, _dispatched = _channel()
    await _card(channel, client)

    body, action = _click(user_id=ASKER)
    await channel._handle_stop_action(_Ack(), body, action)
    await _settle()

    record = channel._activity_records[(REQUEST_ID, ROOM)]
    assert record.failed is False
    assert record.harness_error == ""
    plan = _plan(_last_write(client))
    assert plan is not None
    titles = [str(card.get("title") or "") for card in plan["tasks"]]
    assert slack_connect._ERROR_CARD_TITLE not in titles


# ── The stream a stop leaves open ────────────────────────────────────────────
#
# The card was one half. The other is the message the reply was being written
# into. ``_close_stream`` is reached from ``send()`` and from nowhere else, so
# the same missing terminal that left the card captioned "Working..." leaves
# the streamed message in streaming state, holding whatever the turn had
# written when the cancel landed.
#
# What was observed: a stream opened, a stop ten seconds later, and no
# ``chat.stopStream`` in the sixty-six seconds before an unrelated restart --
# which is also the only reason it ended. So these tests send no terminal event
# either, and assert on the stop call the click itself makes.


class _StreamingStopClient(_RecordingSlackClient):
    """The recording client, plus the three streaming methods."""

    def __init__(self, *, stop_error: Exception | None = None) -> None:
        super().__init__()
        self.starts: list[dict[str, Any]] = []
        self.appends: list[dict[str, Any]] = []
        self.stops: list[dict[str, Any]] = []
        self._stop_error = stop_error

    async def chat_startStream(self, **kwargs: Any) -> dict[str, str]:
        self.starts.append(kwargs)
        return {"ts": "1780000000.777777"}

    async def chat_appendStream(self, **kwargs: Any) -> dict[str, str]:
        self.appends.append(kwargs)
        return {"ok": "true"}

    async def chat_stopStream(self, **kwargs: Any) -> dict[str, str]:
        if self._stop_error is not None:
            raise self._stop_error
        self.stops.append(kwargs)
        return {"ts": str(kwargs.get("ts") or "")}


@pytest.fixture
def _no_stream_debounce(monkeypatch) -> None:
    """Flush every append on the next loop pass rather than a second later."""
    monkeypatch.setattr(slack_connect, "_STREAM_APPEND_DEBOUNCE_MS", 0)
    monkeypatch.setattr(slack_connect, "_STREAM_APPEND_MIN_INTERVAL_SECONDS", 0.0)
    monkeypatch.setattr(slack_connect, "_STREAM_DEBOUNCE_MS", 0)
    monkeypatch.setattr(slack_connect, "_STREAM_MIN_UPDATE_INTERVAL_SECONDS", 0.0)


def _streaming_channel(
    **config: Any,
) -> tuple[SlackChannel, _StreamingStopClient, list[Message]]:
    """A channel that streams, with the recipient a channel stream needs."""
    client = _StreamingStopClient(**config.pop("client", {}))
    channel, _old, dispatched = _channel(enable_streaming=True, **config)
    channel._client = client
    channel._remember_stream_recipient(SESSION, ASKER, TEAM)
    return channel, client, dispatched


def _delta(text: str, **kw: Any) -> Message:
    """One fragment of the reply, in the thread a stream has to live in."""
    msg = _event(EventType.CHAT_DELTA, {"content": text}, **kw)
    msg.metadata = {"slack_channel_id": ROOM, "slack_thread_ts": THREAD}
    return msg


def _threaded_tool_call(**kw: Any) -> Message:
    msg = _tool_call(**kw)
    msg.metadata = {"slack_channel_id": ROOM, "slack_thread_ts": THREAD}
    return msg


def _threaded_final(**kw: Any) -> Message:
    """The terminal, addressed to the same thread the stream was opened in.

    ``_close_stream`` keys on the thread as well as the request, so a terminal
    that resolved to a different delivery would not find the stream at all --
    which is why the stop path matches on the request and the channel and
    leaves the thread out of it.
    """
    msg = _final(**kw)
    msg.metadata = {"slack_channel_id": ROOM, "slack_thread_ts": THREAD}
    return msg


async def _streaming_card(
    channel: SlackChannel, client: _StreamingStopClient
) -> None:
    """A card on screen and a stream open, which is the production shape."""
    await channel.send(_threaded_tool_call())
    await _settle()
    await channel.send(_delta("The answer begins here.\n"))
    await _settle()
    assert client.starts, "the reply should have opened a stream"


@pytest.mark.asyncio
async def test_a_stop_closes_the_open_stream_with_no_terminal_event(
    _no_stream_debounce,
) -> None:
    """The regression. Nothing reaches ``send`` after the click, ever.

    A click that dispatched a cancel and returned would leave the message in
    streaming state until Slack's own expiry ran out on it. The stop is the last
    thing the connector hears about the turn, so the stop is what has to close
    it.
    """
    channel, client, dispatched = _streaming_channel()
    await _streaming_card(channel, client)
    assert client.stops == []

    body, action = _click(user_id=ASKER)
    await channel._handle_stop_action(_Ack(), body, action)
    await _settle()

    assert len(_cancels(dispatched)) == 1
    assert len(client.stops) == 1
    assert client.stops[0]["channel"] == ROOM
    assert client.stops[0]["ts"] == "1780000000.777777"


@pytest.mark.asyncio
async def test_a_stopped_stream_leaves_no_entry_behind(
    _no_stream_debounce,
) -> None:
    """The map is bounded by eviction, which is not the same as being emptied.

    ``_prune_streams`` only runs when the *next* stream is created, and only
    drops what is fifteen minutes idle or over the cap of thirty-two, so an
    abandoned entry sits there holding a surface and a session for as long as
    the channel is quiet. The stop takes it out rather than waiting to be
    evicted, which is also what makes the close exactly-once.
    """
    channel, client, _dispatched = _streaming_channel()
    await _streaming_card(channel, client)
    assert channel._streams, "the stream should be tracked while it is open"

    body, action = _click(user_id=ASKER)
    await channel._handle_stop_action(_Ack(), body, action)
    await _settle()

    assert channel._streams == {}


@pytest.mark.asyncio
async def test_the_partial_text_stays_and_is_marked_as_stopped(
    _no_stream_debounce,
) -> None:
    """What streamed is kept, and told apart from an answer that finished.

    Keeping it is not a choice the close gets to make -- an append cannot be
    taken back -- but leaving it unmarked would be: a reply that stops
    mid-clause and says nothing else reads as the whole answer. So the close
    repeats the same two words the card's title does, appended below what the
    reader already watched arrive rather than replacing any of it.
    """
    channel, client, _dispatched = _streaming_channel()
    await _streaming_card(channel, client)
    streamed = "".join(
        str(chunk.get("text") or "")
        for call in [*client.starts, *client.appends]
        for chunk in call.get("chunks") or []
    )
    assert "The answer begins here." in streamed

    body, action = _click(user_id=ASKER)
    await channel._handle_stop_action(_Ack(), body, action)
    await _settle()

    (stop,) = client.stops
    tail = "".join(
        str(chunk.get("text") or "")
        for chunk in stop.get("chunks") or []
        if chunk.get("type") == "markdown_text"
    )
    assert tail == slack_connect._STREAM_STOPPED_TAIL
    assert slack_connect._ACTIVITY_TITLE_STOPPED in tail
    # Nothing was re-sent: the mark is additive, so the streamed prefix is
    # nowhere in what the close holds.
    assert "The answer begins here." not in tail


def test_the_streamed_mark_says_what_the_card_says() -> None:
    """One ending, one wording, and neither names anybody.

    The tail is derived from the title so the two cannot drift, and it is ASCII
    for the same reason every other string on the card is.
    """
    assert slack_connect._ACTIVITY_TITLE_STOPPED in slack_connect._STREAM_STOPPED_TAIL
    slack_connect._STREAM_STOPPED_TAIL.encode("ascii")
    assert ASKER not in slack_connect._STREAM_STOPPED_TAIL
    assert "stopped by" not in slack_connect._STREAM_STOPPED_TAIL.lower()


@pytest.mark.asyncio
async def test_a_terminal_arriving_after_the_stop_does_not_close_it_twice(
    _no_stream_debounce,
) -> None:
    """The entry is popped under the lock, so there is one close in the map.

    A terminal cannot arrive on the cancel path -- that is the whole defect --
    but it must be harmless if one ever does on a path this has not seen. It
    finds no stream to finish and posts the answer as a message of its own,
    which is exactly what a stream dropped by ``_prune_streams`` has always
    done, and it does not stop a message that has already been stopped.
    """
    channel, client, _dispatched = _streaming_channel()
    await _streaming_card(channel, client)

    body, action = _click(user_id=ASKER)
    await channel._handle_stop_action(_Ack(), body, action)
    await _settle()
    assert len(client.stops) == 1

    await channel.send(_threaded_final())
    await _settle()

    assert len(client.stops) == 1, "the close must not be spent twice"


@pytest.mark.asyncio
async def test_a_turn_that_ended_on_its_own_keeps_its_own_ending(
    _no_stream_debounce,
) -> None:
    """A stop that lost the race to a terminal must not undo it.

    The terminal took ``send()`` through ``_close_stream`` with the whole
    answer, which is a better ending than this one. ``_settle_stopped_card``
    leaves ``stopped`` unset on a record it finds already closed, and that is
    what the stream half reads to stand aside.
    """
    channel, client, _dispatched = _streaming_channel()
    await _streaming_card(channel, client)

    await channel.send(_threaded_final())
    await _settle()
    closes_by_the_turn = len(client.stops)
    assert closes_by_the_turn == 1

    body, action = _click(user_id=ASKER)
    await channel._handle_stop_action(_Ack(), body, action)
    await _settle()

    assert len(client.stops) == closes_by_the_turn
    assert channel._streams == {}


@pytest.mark.asyncio
async def test_a_close_refused_at_the_stop_is_reported_and_not_raised(
    _no_stream_debounce, logged: dict[str, list[str]]
) -> None:
    """There is no reply owed here, so a failed close cannot fail a delivery.

    ``_finish_streamed_reply`` raises when the tail of a finished answer cannot
    be delivered. A stopped turn has no finished answer: what the close would
    have delivered is what the cancel destroyed. So a refusal costs the message
    its mark and nothing else, and the click still reports the stop it made.
    """
    channel, client, dispatched = _streaming_channel(
        client={"stop_error": RuntimeError("message_not_in_streaming_state")}
    )
    await _streaming_card(channel, client)

    body, action = _click(user_id=ASKER)
    await channel._handle_stop_action(_Ack(), body, action)
    await _settle()

    assert len(_cancels(dispatched)) == 1
    assert client.stops == []
    assert channel._streams == {}
    assert any("streaming stopped with the turn" in line for line in logged["info"])


@pytest.mark.asyncio
async def test_a_stop_with_no_stream_open_closes_nothing(
    _no_stream_debounce,
) -> None:
    """Most turns never stream at all, and the stop path must not mind.

    A reply short enough to arrive whole, a channel with streaming off, a turn
    stopped before it wrote a word: none of them has a message in streaming
    state, and none of them should produce a call saying otherwise.
    """
    channel, client, dispatched = _streaming_channel()
    await channel.send(_threaded_tool_call())
    await _settle()
    assert client.starts == []

    body, action = _click(user_id=ASKER)
    await channel._handle_stop_action(_Ack(), body, action)
    await _settle()

    assert len(_cancels(dispatched)) == 1
    assert client.stops == []


@pytest.mark.asyncio
async def test_a_resumed_turns_stream_is_found_under_its_own_id(
    _no_stream_debounce,
) -> None:
    """The card folds a resumed id onto the original; the stream map does not.

    So the two are compared through the same fold. Without it the one turn most
    likely to be stopped -- one already interrupted once -- would be the one
    whose stream the stop could not find.
    """
    channel, client, _dispatched = _streaming_channel()
    await _streaming_card(channel, client)

    resumed = "req-7f3a-resumed"
    channel._remember_activity_alias(resumed, REQUEST_ID)
    await channel.send(_delta(" And carries on.\n", request_id=resumed))
    await _settle()

    record = channel._activity_records[(REQUEST_ID, ROOM)]
    keys = channel._stream_keys_for(record)
    assert sorted(key[0] for key in keys) == sorted([REQUEST_ID, resumed])

    body, action = _click(user_id=ASKER)
    await channel._handle_stop_action(_Ack(), body, action)
    await _settle()

    assert len(client.stops) == 2
    assert channel._streams == {}


# ── Releasing the session ────────────────────────────────────────────────────
#
# The third consumer of the terminal event a stop never sends. The card was the
# first and the open stream the second; this one is the initiator record, and
# unlike those two it is not a display defect. ``turn_initiator`` is the
# liveness signal the inbound path reads, so an entry left standing after a stop
# tells every later message that a turn is running -- and under
# ``mid_turn: queue`` every later message is therefore held, behind a turn that
# ended minutes ago, waiting for an event that is never coming.


def _queueing_channel(
    **config: Any,
) -> tuple[SlackChannel, _RecordingSlackClient, list[Message]]:
    """A channel that holds mid-turn messages instead of cancelling the turn.

    ``_mid_turn_mode`` is patched rather than configured for the reason the
    mid-turn tests patch it: what chooses between the three modes is a
    per-conversation setting, and it is not this file's subject.
    """
    channel, client, dispatched = _channel(**config)
    channel._mid_turn_mode = (  # type: ignore[assignment]
        lambda *_a, **_kw: slack_connect.MID_TURN_QUEUE
    )
    return channel, client, dispatched


def _room_message(*, ts: str, user: str = ASKER, text: str = "and one more thing") -> dict:
    return {
        "type": "message",
        "channel_type": "channel",
        "channel": ROOM,
        "user": user,
        "text": text,
        "ts": ts,
        "thread_ts": THREAD,
    }


async def _inbound(channel: SlackChannel, ts: str, event_id: str, **kw: Any) -> str:
    return await channel._handle_slack_event(
        _room_message(ts=ts, **kw),
        {"event_id": event_id, "team_id": TEAM},
        is_dm=False,
        trigger="mention",
    )


async def _running_turn(
    channel: SlackChannel, client: _RecordingSlackClient
) -> None:
    """A turn with a card on screen and an initiator entry, as a real one has."""
    await _card(channel, client)
    channel._remember_turn_initiator(SESSION, ASKER, REQUEST_ID, is_dm=False, chat_type="channel")


def _sends(dispatched: list[Message]) -> list[Message]:
    return [msg for msg in dispatched if msg.req_method == ReqMethod.CHAT_SEND]


@pytest.mark.asyncio
async def test_a_message_after_a_stop_is_dispatched_rather_than_queued() -> None:
    """The production failure, stated as a test.

    A DM turn was stopped and nothing was running afterwards. Eight minutes
    later four messages arrived and all four were held -- including the first,
    which had nothing in front of it. The initiator entry had survived the stop,
    because the only thing that retires one is a terminal event and the cancel
    guarantees there is none, so the inbound path read "a turn is running" and
    queued. The queue drains on a terminal too, so nothing ever came out of it;
    the conversation was wedged until the entry aged out an hour later.
    """
    channel, client, dispatched = _queueing_channel()
    await _running_turn(channel, client)

    body, action = _click(user_id=ASKER)
    await channel._handle_stop_action(_Ack(), body, action)
    await _settle()

    assert len(_cancels(dispatched)) == 1
    # The entry is what the inbound path reads, and it is gone.
    assert channel.turn_initiator(SESSION) is None

    outcome = await _inbound(channel, "1780000100.000100", "Ev-after-stop")

    assert outcome.startswith("dispatched:")
    assert channel._queued_messages == {}
    assert len(_sends(dispatched)) == 1


@pytest.mark.asyncio
async def test_a_stop_releases_the_message_already_waiting_behind_it() -> None:
    """A held message is a promise, and a stop is the turn it is waiting on ending.

    The alternative -- dropping the queue, on the grounds that a stop means stop
    -- was rejected. The button names one turn; the messages behind it are their
    senders' own, and in a thread the person who pressed it need not be the
    person whose message is waiting. Each holds a reaction saying it will run.
    """
    channel, client, dispatched = _queueing_channel()
    await _running_turn(channel, client)

    held = await _inbound(channel, "1780000050.000100", "Ev-held")
    assert held.startswith("queued:")
    assert channel._queued_messages[SESSION]
    assert client.added, "the held message should have been marked"

    body, action = _click(user_id=ASKER)
    await channel._handle_stop_action(_Ack(), body, action)
    await _settle()

    # Dispatched by the stop itself, with no further message needed to shake it
    # loose -- which is what the incident lacked.
    assert len(_sends(dispatched)) == 1
    assert channel._queued_messages == {}
    assert client.removed, "the waiting mark should have come off"


@pytest.mark.asyncio
async def test_a_stop_drains_one_held_message_and_not_the_queue() -> None:
    """Each held message is its own turn, so the second waits on the first.

    The same rule the terminal path drains by. Releasing the whole queue at once
    would start four turns on one session, three of which would cancel the two
    before them.
    """
    channel, client, dispatched = _queueing_channel()
    await _running_turn(channel, client)

    assert (await _inbound(channel, "1780000050.000100", "Ev-1")).startswith("queued:")
    assert (await _inbound(channel, "1780000051.000100", "Ev-2")).startswith("queued:")
    assert len(channel._queued_messages[SESSION]) == 2

    body, action = _click(user_id=ASKER)
    await channel._handle_stop_action(_Ack(), body, action)
    await _settle()

    assert len(_sends(dispatched)) == 1
    assert len(channel._queued_messages[SESSION]) == 1
    # The one that left is the one that arrived first: waiting must not reorder
    # a conversation.
    # startswith, because the envelope marker the connector appends trails it.
    assert _sends(dispatched)[0].params["query"].startswith("and one more thing")


@pytest.mark.asyncio
async def test_a_stop_does_not_retire_a_newer_turns_initiator() -> None:
    """The guard that keeps one person's ending off another person's turn.

    A second message into a shared thread cancels the running turn and starts
    its own, so by the time a stop for the first is handled the session's entry
    may already belong to the second. Retiring it would leave a live turn with
    no starter -- no stop floor, and a queue that drains into it.
    """
    channel, client, dispatched = _queueing_channel()
    await _running_turn(channel, client)
    # The session has moved on to somebody else's turn.
    channel._remember_turn_initiator(SESSION, BYSTANDER, "req-newer", is_dm=False, chat_type="channel")

    body, action = _click(user_id=ASKER)
    await channel._handle_stop_action(_Ack(), body, action)
    await _settle()

    entry = channel.turn_initiator(SESSION)
    assert entry is not None
    assert entry.request_id == "req-newer"
    assert entry.user_id == BYSTANDER

    # And the drain reads the same entry, so nothing is let out in front of it.
    assert (await _inbound(channel, "1780000100.000100", "Ev-after")).startswith(
        "queued:"
    )
    assert len(_sends(dispatched)) == 0


@pytest.mark.asyncio
async def test_a_resumed_turns_initiator_is_found_through_the_alias() -> None:
    """The card folds a resumed id onto the original; the initiator map does not.

    A request resumed after an interrupt runs under a fresh id and opens its
    entry under that one, while the card -- and so the button's value -- keeps
    the id the request started under. Compared raw, the stop would miss exactly
    the turn that had already been interrupted once, which is the turn most
    likely to be stopped.
    """
    channel, client, dispatched = _queueing_channel()
    await _running_turn(channel, client)

    resumed = "req-7f3a-resumed"
    channel._remember_activity_alias(resumed, REQUEST_ID)
    channel._remember_turn_initiator(SESSION, ASKER, resumed, is_dm=False, chat_type="channel")

    body, action = _click(user_id=ASKER)
    await channel._handle_stop_action(_Ack(), body, action)
    await _settle()

    assert len(_cancels(dispatched)) == 1
    assert channel.turn_initiator(SESSION) is None


@pytest.mark.asyncio
async def test_a_terminal_arriving_after_a_stop_changes_nothing() -> None:
    """Settling twice is harmless, here as on the card and the stream.

    Nothing observed sends a terminal after a cancel, but the stop path must not
    depend on that: an ending on some path this has not seen finds the entry
    already gone, and ``_forget_turn_initiator`` says nothing about an entry it
    does not own.
    """
    channel, client, dispatched = _queueing_channel()
    await _running_turn(channel, client)

    body, action = _click(user_id=ASKER)
    await channel._handle_stop_action(_Ack(), body, action)
    await _settle()
    assert channel.turn_initiator(SESSION) is None

    await channel.send(_final())
    await _settle()

    assert channel.turn_initiator(SESSION) is None
    assert channel._queued_messages == {}
    assert len(_sends(dispatched)) == 0


@pytest.mark.asyncio
async def test_a_stop_says_it_released_the_session(
    logged: dict[str, list[str]],
) -> None:
    """The retire is the half of a stop with no surface of its own.

    A settled card and a closed stream are both visible in the channel. This one
    is a dictionary entry, and the only evidence it was ever retired -- or the
    only sign it was not -- is the log.
    """
    channel, client, _dispatched = _queueing_channel()
    await _running_turn(channel, client)

    body, action = _click(user_id=ASKER)
    await channel._handle_stop_action(_Ack(), body, action)
    await _settle()

    assert any(
        "turn initiator retired by a stop" in line and REQUEST_ID in line
        for line in logged["info"]
    )


@pytest.mark.asyncio
async def test_a_stop_with_nothing_recorded_releases_nothing() -> None:
    """A turn nobody recorded a starter for is still stoppable, and still ends here.

    The entry is not a precondition for anything on this path: ``_may_stop``
    reads its absence as "this connector cannot say", not as a refusal, and the
    release has simply nothing to retire.
    """
    channel, client, dispatched = _queueing_channel(allow_from=[ASKER])
    await _card(channel, client)
    assert channel.turn_initiator(SESSION) is None

    body, action = _click(user_id=ASKER)
    await channel._handle_stop_action(_Ack(), body, action)
    await _settle()

    assert len(_cancels(dispatched)) == 1
    assert channel.turn_initiator(SESSION) is None
    assert channel._queued_messages == {}


# ── The other trigger: a message that cancels the turn in front of it ────────
#
# The button is not the only way a turn ends without saying so. Slack's ambient
# gesture is posting a message, and the gateway reads that as "cancel the
# running turn and run this one instead" -- which is the default, what every
# conversation that writes no ``mid_turn`` does, and how almost every cancelled
# turn on a busy channel actually ends. That cancel is the same cancel the
# button dispatches and it reports the same nothing, but it never goes near
# ``_handle_stop_action``, so none of the endings written there were written
# for it. Four mentions in quick succession opened three streams and closed
# one, and left three cards saying "Working..." about turns that had finished.


@pytest.mark.asyncio
async def test_the_next_message_ends_the_turn_it_cancels() -> None:
    """The production failure on the commoner of the two triggers.

    No button is pressed here and none exists to press: this is what happens
    when somebody types twice, and it predates the stop button entirely.
    """
    channel, client, _dispatched = _channel()
    await _running_turn(channel, client)
    assert _title(client) == slack_connect._ACTIVITY_TITLE_WORKING

    assert (await _inbound(channel, "1780000100.000100", "Ev-second")).startswith(
        "dispatched:"
    )
    await _settle()

    record = channel._activity_records[(REQUEST_ID, ROOM)]
    assert record.closed is True
    assert record.stopped is True
    # The tool that was still running when the cancel landed is counted as
    # having reported nothing, never as having failed -- the same reading the
    # button's stop gets.
    assert _title(client).startswith(slack_connect._ACTIVITY_TITLE_STOPPED)


@pytest.mark.asyncio
async def test_the_next_message_closes_the_stream_it_cancels(
    _no_stream_debounce,
) -> None:
    """Three streams opened and one closed is what this looked like in Slack.

    The two that were cancelled by the messages behind them stayed in streaming
    state until Slack's own expiry, holding whatever the turn had written when
    the cancel landed.
    """
    channel, client, _dispatched = _streaming_channel()
    await _streaming_card(channel, client)
    channel._remember_turn_initiator(SESSION, ASKER, REQUEST_ID, is_dm=False, chat_type="channel")
    assert client.stops == []

    await _inbound(channel, "1780000100.000100", "Ev-second")
    await _settle()

    assert len(client.stops) == 1
    assert client.stops[0]["ts"] == "1780000000.777777"
    assert channel._streams == {}


@pytest.mark.asyncio
async def test_a_message_into_an_idle_session_ends_nothing() -> None:
    """The ordinary case, and the one this must not touch.

    With nothing running the gateway cancels nothing, so there is no ending to
    write -- and writing one would settle the card of a turn that is about to
    go on producing events.
    """
    channel, client, _dispatched = _channel()
    await _card(channel, client)
    # No initiator entry: as far as this connector can say, nothing is running.
    assert channel.turn_initiator(SESSION) is None
    before = len(client.updates)

    await _inbound(channel, "1780000100.000100", "Ev-first")
    await _settle()

    record = channel._activity_records[(REQUEST_ID, ROOM)]
    assert record.closed is False
    assert len(client.updates) == before


@pytest.mark.asyncio
async def test_a_steered_message_does_not_end_the_turn_it_joins() -> None:
    """Steering is contributing to a turn, so there is no cancel and no ending.

    The gateway is asked to skip the cancel it performs before every other
    ``chat.send``, so the turn carries on and its card must carry on with it.
    """
    channel, client, _dispatched = _channel()
    await _running_turn(channel, client)
    channel._mid_turn_mode = (  # type: ignore[assignment]
        lambda *_a, **_kw: slack_connect.MID_TURN_STEER
    )

    assert (await _inbound(channel, "1780000100.000100", "Ev-steer")).startswith(
        "steered:"
    )
    await _settle()

    record = channel._activity_records[(REQUEST_ID, ROOM)]
    assert record.closed is False
    assert _title(client) == slack_connect._ACTIVITY_TITLE_WORKING


@pytest.mark.asyncio
async def test_a_held_message_does_not_end_the_turn_it_waits_for() -> None:
    """Under ``queue`` the message is not sent, so nothing is cancelled."""
    channel, client, _dispatched = _queueing_channel()
    await _running_turn(channel, client)

    assert (await _inbound(channel, "1780000100.000100", "Ev-held")).startswith(
        "queued:"
    )
    await _settle()

    record = channel._activity_records[(REQUEST_ID, ROOM)]
    assert record.closed is False
    assert _title(client) == slack_connect._ACTIVITY_TITLE_WORKING


@pytest.mark.asyncio
async def test_a_resumed_turns_card_is_found_when_the_next_message_cancels_it() -> None:
    """The same fold, on the same asymmetry, for the same reason.

    The initiator entry of a resumed turn holds the fresh id it runs under; its
    card is keyed on the id the request started under.
    """
    channel, client, _dispatched = _channel()
    await _card(channel, client)
    resumed = "req-7f3a-resumed"
    channel._remember_activity_alias(resumed, REQUEST_ID)
    channel._remember_turn_initiator(SESSION, ASKER, resumed, is_dm=False, chat_type="channel")

    await _inbound(channel, "1780000100.000100", "Ev-second")
    await _settle()

    record = channel._activity_records[(REQUEST_ID, ROOM)]
    assert record.closed is True
    assert record.stopped is True


@pytest.fixture
def records() -> Iterator[list[logging.LogRecord]]:
    """Everything the connector's own logger emits, taken off it directly.

    Not ``caplog``, whose handler sits on the root logger: whether anything
    reaches it depends on whether this package's loggers propagate, which is a
    property of whatever configured logging first rather than of the code under
    test.
    """
    collected: list[logging.LogRecord] = []
    logger = logging.getLogger(slack_connect.__name__)
    handler = logging.Handler()
    handler.emit = collected.append  # type: ignore[method-assign]
    previous_level = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    try:
        yield collected
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous_level)


@pytest.mark.asyncio
async def test_the_clicker_being_told_is_recorded_as_its_own_notice(records) -> None:
    """The click paths share the refusal's helper, and so share its record.

    The log already says what the connector decided about a click; it said
    nothing about whether the person who clicked ever heard. ``notice``
    distinguishes the four things a clicker can be told, so an accepted stop
    cannot be read off the log as a refused one.
    """
    channel, client, _dispatched = _channel()
    await _card(channel, client)
    channel._remember_turn_initiator(SESSION, ASKER, REQUEST_ID, is_dm=False, chat_type="channel")

    body, action = _click(user_id=ASKER)
    await channel._handle_stop_action(_Ack(), body, action)
    await _settle()

    told = [r for r in records if "what became of their message" in r.getMessage()]
    assert len(told) == 1
    assert told[0].levelno == logging.INFO
    assert ASKER in told[0].getMessage()
    assert "stop-accepted" in told[0].getMessage()
    assert slack_connect._STOP_ACCEPTED_NOTICE not in told[0].getMessage()
