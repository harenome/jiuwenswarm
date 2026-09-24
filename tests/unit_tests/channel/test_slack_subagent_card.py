"""The turn activity card: one Slack message per turn that does any work.

A turn that hands work to subagents, or grinds through a long sequence of tool
calls, or that simply reasons for minutes on end, produces no output at all
while it does so, so the channel cannot tell it from a hung one. The card closes
that gap: a ``plan`` holding up to four sections, its thinking, its todos, its
subagents and its tools, only the ones that have anything in them.

These tests are about what it has to get right: it appears when a turn outlives
the delay, it is rewritten in place rather than posted twice, its sections reach
``error`` when something fails or the turn does, it never holds a brief, a
result or a line of reasoning, and it never claims the *work* succeeded -- only
that the harness saw something run, is running, or came back.

The signal is the ordinary ``chat.tool_call`` / ``chat.tool_result`` pair that
every tool produces, the ``todo.updated`` snapshot the rail emits after every
todo write, and the ``chat.reasoning`` chunks a thinking model streams;
nothing card-specific crosses the wire, and these fixtures are shaped exactly as
``JiuSwarmStreamEventRail`` emits them.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import pytest
from openjiuwen.harness.subagent_runtime.models import SubagentStatus

from jiuwenswarm.common.schema.message import EventType, Message
from jiuwenswarm.gateway.channel_manager.base import RobotMessageRouter
from jiuwenswarm.gateway.channel_manager.im_platforms.slack import slack_connect
from jiuwenswarm.gateway.channel_manager.im_platforms.slack.slack_connect import (
    SlackChannel,
    SlackChannelConfig,
)
from jiuwenswarm.server.runtime.agent_adapter.task_tool_events import _build_projection

REQUEST_ID = "req-7f3a"
# What the gateway calls the same request once an interrupt answered from Slack
# has resumed it: a new id, built from the question's id and the click's
# timestamp, for work the person who asked still thinks of as one request.
RESUMED_REQUEST_ID = "slack-answer-chatcmpl-tool-0123456789abcdef-1710000016.000100"
# The subagent's brief. ``TaskTool`` does not even log it -- it logs a length
# and a hash -- so a card built from the arguments beside it must not publish it
# to a channel.
TASK_DESCRIPTION = (
    "Read $JIUWENSWARM_DATA_DIR/agent/workspace/state/skills/digest/watermark, "
    "then sweep every repository listed there and report what changed."
)
# What the model thinks to itself on the way to an answer. High-volume, unvetted
# and addressed to nobody; the tests below hunt for it across every block the
# card ever writes, because the one thing the thinking section must not do is
# put any of it on screen.
REASONING_TEXT = (
    "The user probably means the staging cluster. I should check the watermark "
    "under $JIUWENSWARM_DATA_DIR before answering, and if it is stale I will "
    "have to say so rather than guess. Actually, wait -- the token is wrong."
)


@pytest.fixture(autouse=True)
def _isolated_dedup_store(tmp_path, monkeypatch):
    monkeypatch.setattr(
        slack_connect.SlackEventDedupStore,
        "__init__",
        lambda self, path=None, **kw: _real_store_init(
            self, path or tmp_path / "slack_seen_events.json", **kw
        ),
    )


_real_store_init = slack_connect.SlackEventDedupStore.__init__


class _RecordingSlackClient:
    def __init__(self) -> None:
        self.posts: list[dict[str, Any]] = []
        self.updates: list[dict[str, Any]] = []
        self.deleted: list[tuple[str, str]] = []
        self.ts_counter = 0

    async def chat_postMessage(self, **kwargs: Any) -> dict[str, str]:
        self.posts.append(kwargs)
        self.ts_counter += 1
        return {"ts": f"1780000000.{self.ts_counter:06d}"}

    async def chat_update(self, **kwargs: Any) -> dict[str, str]:
        self.updates.append(kwargs)
        return {"ts": kwargs["ts"]}

    async def chat_delete(self, **kwargs: Any) -> dict[str, bool]:
        self.deleted.append((kwargs.get("channel", ""), kwargs.get("ts", "")))
        return {"ok": True}


class _BlockRefusingSlackClient(_RecordingSlackClient):
    """Refuses every message that holds blocks, and takes plain text.

    Stands for the card that cannot be posted at all -- Slack down for that
    call, the workspace rate-limited past its retries, a rendering it will not
    accept. Not a Block Kit rejection: those are retried without the blocks one
    layer down and the message still lands.
    """

    async def chat_postMessage(self, **kwargs: Any) -> dict[str, str]:
        if "blocks" in kwargs:
            raise RuntimeError("Slack refused the card")
        return await super().chat_postMessage(**kwargs)


class _UpdateRefusingSlackClient(_RecordingSlackClient):
    """Takes the card, then refuses every edit to it."""

    async def chat_update(self, **kwargs: Any) -> dict[str, str]:
        raise RuntimeError("Slack refused the edit")


def _channel(client: Any, **config: Any) -> SlackChannel:
    config.setdefault("activity_card_delay_seconds", 0.0)
    config.setdefault("activity_card_min_edit_seconds", 0.0)
    channel = SlackChannel(
        SlackChannelConfig(enabled=True, default_channel_id="C1", **config),
        RobotMessageRouter(),
    )
    channel._client = client
    return channel


def _event(
    event_type: EventType,
    payload: dict[str, Any],
    *,
    request_id: str = REQUEST_ID,
) -> Message:
    return Message(
        id=request_id,
        type="event",
        channel_id="slack",
        session_id="slack_T1_C1_root",
        params={},
        timestamp=time.time(),
        ok=True,
        payload={"event_type": event_type.value, **payload},
        event_type=event_type,
        metadata={"slack_channel_id": "C1"},
    )


def _dispatch(
    call_id: str,
    subagent_type: str = "general_agent",
    *,
    request_id: str = REQUEST_ID,
) -> Message:
    """One ``task_tool`` call, as ``_emit_tool_call`` shapes it."""
    return _event(
        EventType.CHAT_TOOL_CALL,
        {
            "tool_call": {
                "name": "task_tool",
                "arguments": {
                    "subagent_type": subagent_type,
                    "task_description": TASK_DESCRIPTION,
                },
                "tool_call_id": call_id,
            }
        },
        request_id=request_id,
    )


def _persistent_call(
    call_id: str,
    subagent_type: str = "code_agent",
    *,
    request_id: str = REQUEST_ID,
) -> Message:
    return _event(
        EventType.CHAT_TOOL_CALL,
        {
            "tool_call": {
                "name": "subagent_spawn",
                "arguments": {
                    "subagent_type": subagent_type,
                    "task_description": TASK_DESCRIPTION,
                },
                "tool_call_id": call_id,
            }
        },
        request_id=request_id,
    )


def _roster(
    subagent_id: str,
    subagent_type: str = "code_agent",
    *,
    status: str = "running",
    turn_outcome: str | None = None,
    dispatch_source: str | None = None,
    request_id: str = REQUEST_ID,
) -> Message:
    payload = {
        "subagent_id": subagent_id,
        "subagent_type": subagent_type,
        "task_description": TASK_DESCRIPTION,
        "status": status,
        "turn_outcome": turn_outcome,
    }
    if dispatch_source is not None:
        payload["dispatch_source"] = dispatch_source
    return _event(EventType.CHAT_SUBTASK_UPDATE, payload, request_id=request_id)


def _result(
    call_id: str,
    *,
    failed: bool = False,
    tool_name: str = "task_tool",
    result: str = "Swept 12 repositories.",
    request_id: str = REQUEST_ID,
) -> Message:
    """One subagent or tool returning, as ``_emit_tool_result`` shapes it."""
    payload: dict[str, Any] = {
        "tool_name": tool_name,
        "tool_call_id": call_id,
        "result": result,
    }
    if failed:
        # The three flags the rail sets once ``_infer_tool_result_error`` has
        # classified the structured result. Nothing else says "this failed".
        payload["success"] = False
        payload["status"] = "error"
        payload["is_error"] = True
    return _event(EventType.CHAT_TOOL_RESULT, payload, request_id=request_id)


def _tool_call(
    call_id: str, name: str = "read_file", *, request_id: str = REQUEST_ID
) -> Message:
    """One ordinary tool call the parent made itself."""
    return _event(
        EventType.CHAT_TOOL_CALL,
        {
            "tool_call": {
                "name": name,
                "arguments": {"path": "/etc/hosts"},
                "tool_call_id": call_id,
            }
        },
        request_id=request_id,
    )


def _todos(*states: str, request_id: str = REQUEST_ID) -> Message:
    """One ``todo.updated`` snapshot: the whole list, every time."""
    return _event(
        EventType.TODO_UPDATED,
        {
            "todos": [
                {
                    "id": f"todo-{index}",
                    "content": "Sweep the repositories listed in the watermark",
                    "activeForm": "Sweeping the repositories",
                    "status": state,
                }
                for index, state in enumerate(states)
            ]
        },
        request_id=request_id,
    )


def _reasoning(*, request_id: str = REQUEST_ID) -> Message:
    """One ``chat.reasoning`` chunk, as a thinking model streams them.

    A turn emits hundreds of these; the card reads that they arrived and not one
    character of what is in them.
    """
    return _event(
        EventType.CHAT_REASONING, {"content": REASONING_TEXT}, request_id=request_id
    )


def _final(text: str = "Done.", *, request_id: str = REQUEST_ID) -> Message:
    return _event(EventType.CHAT_FINAL, {"content": text}, request_id=request_id)


# What the compressor wrote about the conversation it folded up. The model's own
# prose about a whole session, as unvetted and as high-volume as a line of
# reasoning, and the card must not put a character of it on screen.
COMPACT_SUMMARY = (
    "The user is deploying from $JIUWENSWARM_DATA_DIR and has been chasing a "
    "token that does not match the workspace they think it belongs to. Earlier "
    "rounds established that the staging cluster is the one that matters."
)


def _compaction(
    *,
    status: str = "started",
    operation_id: str = "op-dialogue",
    processor: str = "DialogueCompressor",
    before: dict[str, Any] | None = None,
    saved: dict[str, Any] | None = None,
    duration_ms: int | None = None,
    error: str | None = None,
    request_id: str = REQUEST_ID,
) -> Message:
    """One ``context.compression_state``, shaped as the runtime emits it.

    ``event_type`` is unset on the message and carried in the payload alone,
    which is not this fixture taking a shortcut: ``EventType`` has no member for
    this event, so the gateway's parse raises, it logs the string at debug and
    leaves the field ``None``. Every channel that reads this event -- the web
    one included -- matches the payload string instead.

    The figures are the ones from the turn this section was written for: two
    hundred and one messages at sixty-five per cent of the window, folded to
    three messages in six minutes and nineteen seconds.
    """
    payload: dict[str, Any] = {
        "event_type": "context.compression_state",
        "type": "context.compression_state",
        "operation_id": operation_id,
        "status": status,
        "phase": "get_context_window",
        "processor": processor,
        "model": "qwen-max",
        "before": before
        if before is not None
        else {"messages": 201, "tokens": 685440, "context_percent": 65},
        "compact_summary": COMPACT_SUMMARY,
        "summary": "Context compacted: 30.0K/685.4K tokens (95.6% saved)",
    }
    if saved is not None:
        payload["saved"] = saved
        payload["after"] = {"messages": 3, "tokens": 30003, "context_percent": 3}
    if duration_ms is not None:
        payload["duration_ms"] = duration_ms
    if error is not None:
        payload["error"] = error
    return Message(
        id=request_id,
        type="event",
        channel_id="slack",
        session_id="slack_T1_C1_root",
        params={},
        timestamp=time.time(),
        ok=True,
        payload=payload,
        event_type=None,
        metadata={"slack_channel_id": "C1"},
    )


def _compaction_started(*, request_id: str = REQUEST_ID) -> Message:
    return _compaction(status="started", request_id=request_id)


def _compaction_completed(*, request_id: str = REQUEST_ID) -> Message:
    return _compaction(
        status="completed",
        saved={"messages": 198, "tokens": 655437, "percent": 95.6},
        duration_ms=379_000,
        request_id=request_id,
    )


def _compaction_noop(*, request_id: str = REQUEST_ID) -> Message:
    """A processor that ran and had nothing to save, which announces no start.

    The offloader on the turn this was written for reported exactly this and
    nothing before it, which is why a terminal state opens a run as readily as a
    ``started`` one does.
    """
    return _compaction(
        status="noop",
        operation_id="op-offloader",
        processor="MessageSummaryOffloader",
        saved={"messages": 0, "tokens": 0, "percent": 0},
        duration_ms=4,
        request_id=request_id,
    )


def _compaction_failed(*, request_id: str = REQUEST_ID) -> Message:
    """A compaction a cancel reached, as the runtime reports one.

    ``failed`` with the error ``cancelled``: the runtime catches the
    ``CancelledError``, emits this, and re-raises.
    """
    return _compaction(
        status="failed",
        saved={"messages": 0, "tokens": 0, "percent": 0},
        duration_ms=12_000,
        error="cancelled",
        request_id=request_id,
    )


# A turn dying, shaped as the gateway shapes it: payload["error"] and nothing
# the connector can attribute to any one piece of work. This one is real -- the
# model call that never streamed a token, which is also the case that reaches
# _close_activity_card with no card and no record to amend.
STREAM_TIMEOUT_ERROR = (
    "[181001] model call failed, reason: openAI API async stream error: "
    "no data received for 900s"
)


def _error(
    text: str = STREAM_TIMEOUT_ERROR, *, request_id: str = REQUEST_ID
) -> Message:
    return _event(EventType.CHAT_ERROR, {"error": text}, request_id=request_id)


def _plan(call: dict[str, Any]) -> dict[str, Any] | None:
    for block in call.get("blocks") or []:
        if block.get("type") == "plan":
            return block
    return None


def _sections(call: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """The plan's cards, keyed by the section their ``task_id`` names."""
    plan = _plan(call)
    if plan is None:
        return {}
    return {
        str(card.get("task_id", "")).split("-", 1)[0]: card
        for card in plan.get("tasks", [])
    }


def _section(call: dict[str, Any], name: str) -> dict[str, Any] | None:
    return _sections(call).get(name)


def _order(call: dict[str, Any]) -> list[str]:
    plan = _plan(call)
    if plan is None:
        return []
    return [
        str(card.get("task_id", "")).split("-", 1)[0] for card in plan.get("tasks", [])
    ]


def _flatten(field: dict[str, Any] | None) -> str:
    """A rich_text field flattened to a line, an emoji written ``:name:``.

    Only these assertions spell an emoji that way. The block itself holds an
    ``emoji`` element, which is the one form ``rich_text`` renders -- a
    shortcode inside a ``text`` element arrives as literal text.
    """
    out: list[str] = []
    for element in (field or {}).get("elements", []):
        for leaf in element.get("elements", []):
            if leaf.get("type") == "emoji":
                out.append(f":{leaf.get('name', '')}:")
            else:
                out.append(str(leaf.get("text", "")))
    return "".join(out)


def _details_text(card: dict[str, Any] | None) -> str:
    return _flatten((card or {}).get("details"))


def _output_text(card: dict[str, Any] | None) -> str:
    return _flatten((card or {}).get("output"))


def _preformatted(card: dict[str, Any] | None) -> str:
    """The text of the card's preformatted block, or ``""`` if it has none."""
    out: list[str] = []
    for element in (card or {}).get("details", {}).get("elements", []):
        if element.get("type") != "rich_text_preformatted":
            continue
        out.extend(str(leaf.get("text", "")) for leaf in element.get("elements", []))
    return "".join(out)


def _calls(client: _RecordingSlackClient) -> list[dict[str, Any]]:
    """Every write the channel made, posts and edits alike."""
    return [*client.posts, *client.updates]


def _leaves(field: dict[str, Any] | None) -> list[dict[str, Any]]:
    """The leaf elements of a rich_text field, in order."""
    out: list[dict[str, Any]] = []
    for element in (field or {}).get("elements", []):
        out.extend(element.get("elements", []))
    return out


async def _settle() -> None:
    """Let the delayed-post task run. The delay is zero in these tests."""
    for _ in range(4):
        await asyncio.sleep(0)


# ── The wrapper ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_one_section_is_still_wrapped_in_a_plan() -> None:
    """A turn that only ran tools is a plan of one card, not a bare card.

    Nested in a plan a task_card may be ``pending``; standalone Slack refuses
    that value outright. Dropping the wrapper for the one-section case would
    mean the same turn rendered two different ways, and the queued state
    disappearing in one of them.
    """
    client = _RecordingSlackClient()
    channel = _channel(client)

    await channel.send(_tool_call("call-1", "bash_tool"))
    await _settle()

    assert len(client.posts) == 1
    blocks = client.posts[0]["blocks"]
    # The plan, and the stop button beneath it: the card is the only per-turn
    # surface Slack has, so the control that stops the turn rides on it. It is
    # its own block rather than anything inside the plan -- a task_card takes no
    # accessory -- and it goes away when the turn does.
    assert [block["type"] for block in blocks] == ["plan", "actions"]
    assert _order(client.posts[0]) == ["tools"]
    # A bare string, whatever Slack's own table says the field takes.
    assert blocks[0]["title"] == "Working..."
    # Undocumented, accepted and inert: the client derives the chip from the
    # children, so setting it would be a claim nothing reads.
    assert "status" not in blocks[0]


@pytest.mark.asyncio
async def test_the_work_sections_read_intent_then_delegation_then_execution() -> None:
    client = _RecordingSlackClient()
    channel = _channel(client)

    await channel.send(_todos("completed", "in_progress", "pending"))
    await channel.send(_dispatch("call-1"))
    await channel.send(_tool_call("call-2", "bash_tool"))
    await _settle()

    assert _order(client.updates[-1] if client.updates else client.posts[0]) == [
        "todos",
        "subagents",
        "tools",
    ]
    last = client.updates[-1] if client.updates else client.posts[0]
    assert [card["title"] for card in _plan(last)["tasks"]] == [
        "Todos",
        "Subagents",
        "Tools",
    ]
    assert _details_text(_section(last, "todos")) == (
        ":white_check_mark: 1  :zap: 1  :hourglass_flowing_sand: 1"
    )
    assert _details_text(_section(last, "subagents")) == ":zap: 1"
    assert _details_text(_section(last, "tools")) == "bash_tool  :zap: 1"


@pytest.mark.asyncio
async def test_a_section_with_nothing_in_it_is_left_off_entirely() -> None:
    client = _RecordingSlackClient()
    channel = _channel(client)

    await channel.send(_dispatch("call-1"))
    await _settle()

    # No todos were written and the parent ran no tool of its own, so neither
    # section exists. An empty card would be a row saying nothing.
    assert _order(client.posts[0]) == ["subagents"]


@pytest.mark.asyncio
async def test_a_section_appearing_mid_turn_joins_the_card_already_up() -> None:
    """Sections arrive as the turn discovers them; the card grows in place."""
    client = _RecordingSlackClient()
    channel = _channel(client)

    await channel.send(_tool_call("call-1", "bash_tool"))
    await _settle()
    assert _order(client.posts[0]) == ["tools"]

    await channel.send(_dispatch("call-2"))
    assert _order(client.updates[-1]) == ["subagents", "tools"]

    await channel.send(_todos("in_progress", "pending"))
    assert _order(client.updates[-1]) == ["todos", "subagents", "tools"]

    # One message throughout: three sections, not three cards.
    assert len(client.posts) == 1


# ── Thinking ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_turn_that_only_thinks_still_gets_a_card() -> None:
    """The case the card was missing: no tool, no subagent, no todo, no text.

    Every other section is built from work this turn has not done, so without
    this one the channel shows an empty thread for as long as the model
    deliberates -- which is the seventeen-minute stall the card exists for.
    """
    client = _RecordingSlackClient()
    channel = _channel(client)

    await channel.send(_reasoning())
    await _settle()

    assert len(client.posts) == 1
    assert _order(client.posts[0]) == ["thinking"]
    card = _section(client.posts[0], "thinking")
    assert card["title"] == "Thinking"
    assert card["status"] == "in_progress"
    assert _details_text(card) == ":zap: thinking for 0s"


@pytest.mark.asyncio
async def test_thinking_leads_the_card_and_the_rest_keep_their_order() -> None:
    """Thinking, then intent, then delegation, then execution.

    First because it is first in the turn, and because it is the section that
    exists for the case where none of the others do: a reader scanning a card
    that appeared out of a silent thread wants to know whether anything is
    happening at all before anything else.
    """
    client = _RecordingSlackClient()
    channel = _channel(client)

    await channel.send(_reasoning())
    await channel.send(_todos("completed", "in_progress", "pending"))
    await channel.send(_dispatch("call-1"))
    await channel.send(_tool_call("call-2", "bash_tool"))
    await _settle()

    last = client.updates[-1] if client.updates else client.posts[0]
    assert _order(last) == ["thinking", "todos", "subagents", "tools"]
    assert [card["title"] for card in _plan(last)["tasks"]] == [
        "Thinking",
        "Todos",
        "Subagents",
        "Tools",
    ]


@pytest.mark.asyncio
async def test_the_card_never_carries_a_line_of_what_the_model_thought() -> None:
    """Not a snippet, not a summary, not the first line. Nothing.

    Reasoning is high-volume model output nobody vetted, and the property that
    makes this card safe to post on any turn at all is that every section is
    assembled from counts and durations. So the payload is never opened, and
    this walks every message the card wrote looking for any of it.
    """
    client = _RecordingSlackClient()
    channel = _channel(client)

    await channel.send(_reasoning())
    await _settle()
    await channel.send(_tool_call("call-1", "bash_tool"))
    await channel.send(_reasoning())
    await channel.send(_final("The staging cluster is fine."))

    # Every message the card itself wrote. The reply beside it is the turn's
    # own answer and is not this card's doing.
    cards = [call for call in [*client.posts, *client.updates] if _plan(call)]
    assert cards
    written = json.dumps(cards)
    # The whole of it, and every word of it long enough to be recognisable.
    assert REASONING_TEXT not in written
    for word in REASONING_TEXT.replace("--", " ").split():
        word = word.strip(".,")
        if len(word) > 5:
            assert word not in written, word


@pytest.mark.asyncio
async def test_the_thinking_time_is_clocked_here_and_not_read_off_the_wire() -> None:
    client = _RecordingSlackClient()
    channel = _channel(client)

    await channel.send(_reasoning())
    await _settle()
    record = channel._activity_records[(REQUEST_ID, "C1")]
    record.reasoning_since = time.monotonic() - 137.0

    # Any edit re-renders the elapsed time from the clock, not from a payload.
    await channel.send(_reasoning())

    card = _section(client.updates[-1], "thinking")
    assert _details_text(card) == ":zap: thinking for 2m 17s"
    assert card["status"] == "in_progress"


@pytest.mark.asyncio
async def test_consecutive_chunks_are_one_stretch_and_not_one_apiece() -> None:
    """A gap between two chunks is the model still producing the same block.

    Clocking each chunk separately would report the last few milliseconds of a
    turn that had been thinking for minutes -- the opposite of the signal.
    """
    client = _RecordingSlackClient()
    channel = _channel(client)

    await channel.send(_reasoning())
    await _settle()
    record = channel._activity_records[(REQUEST_ID, "C1")]
    backdated = record.reasoning_since - 90.0
    record.reasoning_since = backdated

    await channel.send(_reasoning())
    await channel.send(_reasoning())

    assert record.reasoning_since == backdated
    assert _details_text(_section(client.updates[-1], "thinking")) == (
        ":zap: thinking for 1m 30s"
    )


@pytest.mark.asyncio
async def test_a_tool_call_ends_the_stretch_and_the_total_carries_across() -> None:
    """A model calling a tool has stopped thinking and started acting.

    Closing the stretch here rather than only at the end of the turn keeps the
    section from claiming a model is deliberating while the tool section counts
    up beside it. The next chunk opens a fresh stretch, and the number is kept
    rather than restarting: a card whose timer went backwards would read as
    one that had lost track.
    """
    client = _RecordingSlackClient()
    channel = _channel(client)

    await channel.send(_reasoning())
    await _settle()
    record = channel._activity_records[(REQUEST_ID, "C1")]
    record.reasoning_since = time.monotonic() - 42.0

    await channel.send(_tool_call("call-1", "bash_tool"))
    card = _section(client.updates[-1], "thinking")
    assert card["status"] == "complete"
    assert _details_text(card) == ":hourglass: thought for 42s"

    await channel.send(_reasoning())
    card = _section(client.updates[-1], "thinking")
    assert card["status"] == "in_progress"
    # Forty-two seconds kept from before, plus whatever the new stretch has run for.
    assert _details_text(card) == ":zap: thinking for 42s"


@pytest.mark.asyncio
async def test_the_turn_ending_takes_the_section_out_of_in_progress() -> None:
    """A card still saying "thinking" above a delivered answer is its own lie.

    The turn keeps a todo list so that there is a card left to read: one whose
    only section is the thinking one is deleted when the turn ends rather than
    settled, which is the rule the tests below this file's discard heading pin.
    A todo is the work that does not disturb what is being measured here -- it
    neither closes the open stretch of reasoning nor touches the section -- so
    the duration is still the one ``settle`` folded in.
    """
    client = _RecordingSlackClient()
    channel = _channel(client)

    await channel.send(_reasoning())
    await channel.send(_todos("in_progress"))
    await _settle()
    record = channel._activity_records[(REQUEST_ID, "C1")]
    record.reasoning_since = time.monotonic() - 65.0
    await channel.send(_final("The staging cluster is fine."))

    assert client.deleted == []
    card = _section(client.updates[-1], "thinking")
    assert card["status"] == "complete"
    assert _details_text(card) == ":hourglass: thought for 1m 05s"


@pytest.mark.asyncio
async def test_a_turn_that_died_settles_the_same_way_and_claims_no_success() -> None:
    """The thinking section settles the same way however the turn ended.

    ``chat.final`` and ``chat.error`` both close the card, and the failure that
    separates them is the turn's, not the thinking's: the thinking ended either
    way. So a terminal word asserting success would be wrong on every turn that
    died -- and turns that die are most of why this card exists. "thought for"
    is true either way; "thinking completed" would not be.

    ``complete`` is the chip because Slack's task_card vocabulary offers no
    neutral fourth value, so the section says what it means in words instead.
    Saying how the turn ended is left to the parts that report it: the plan's
    own line takes the failed stem, and the error section names the failure.
    """
    client = _RecordingSlackClient()
    channel = _channel(client)

    await channel.send(_reasoning())
    await _settle()
    record = channel._activity_records[(REQUEST_ID, "C1")]
    record.reasoning_since = time.monotonic() - 30.0
    await channel.send(_tool_call("call-1", "bash_tool"))
    await channel.send(_result("call-1", tool_name="bash_tool", failed=True))
    await channel.send(_error())

    last = client.updates[-1]
    card = _section(last, "thinking")
    assert card["status"] == "complete"
    assert _details_text(card) == ":hourglass: thought for 30s"
    # Nothing the thinking section says makes the failure read as anything else.
    assert _plan(last)["title"] == "Turn failed, 1 failed"
    assert _section(last, "tools")["status"] == "error"
    for word in ("success", "succeeded", "completed", "done", "ok"):
        assert word not in _details_text(card)


@pytest.mark.asyncio
async def test_a_hung_turn_freezes_its_duration_rather_than_inventing_one() -> None:
    """Nothing ticks this card. The number is as of the last event heard.

    While the model really is reasoning the chunks keep arriving, so the card is
    rewritten and the number climbs on its own. When the turn hangs the chunks
    stop with everything else, no edit happens, and the number stops where it
    was -- which reads as "nothing has been heard since", the very thing the
    reader needs to know. A background ticker would keep counting past the last
    thing anyone observed, which is a card inventing progress; dropping the time
    entirely would give up the only quantity there is.
    """
    client = _RecordingSlackClient()
    channel = _channel(client)

    await channel.send(_reasoning())
    await _settle()
    record = channel._activity_records[(REQUEST_ID, "C1")]
    record.reasoning_since = time.monotonic() - 1020.0
    await channel.send(_reasoning())

    frozen = _details_text(_section(client.updates[-1], "thinking"))
    assert frozen == ":zap: thinking for 17m 00s"

    # The turn goes on never reaching a terminal event: no further edit is
    # spent, and the card still says exactly what it last said.
    edits = len(client.updates)
    await _settle()
    assert len(client.updates) == edits
    assert _details_text(_section(client.updates[-1], "thinking")) == frozen
    assert record.reasoning_since is not None


@pytest.mark.asyncio
async def test_a_turn_that_thinks_briefly_leaves_nothing_behind() -> None:
    """The delay decides, exactly as it does for every other section."""
    client = _RecordingSlackClient()
    channel = _channel(client, activity_card_delay_seconds=30.0)

    await channel.send(_reasoning())
    await channel.send(_reasoning())
    await channel.send(_final("Yes."))
    await _settle()

    assert len(client.updates) == 0
    assert [post["text"] for post in client.posts] == ["Yes."]
    assert not channel._activity_records


@pytest.mark.asyncio
async def test_reasoning_chunks_wait_out_the_edit_floor() -> None:
    """A long turn emits hundreds of them; one edit apiece would be the budget."""
    client = _RecordingSlackClient()
    channel = _channel(client, activity_card_min_edit_seconds=30.0)

    await channel.send(_reasoning())
    await _settle()
    assert len(client.posts) == 1

    for _ in range(50):
        await channel.send(_reasoning())
    assert client.updates == []

    # Past the floor, the next chunk is what re-renders the duration.
    record = channel._activity_records[(REQUEST_ID, "C1")]
    record.next_edit_at = time.monotonic() - 1.0
    await channel.send(_reasoning())
    assert len(client.updates) == 1


@pytest.mark.asyncio
async def test_reasoning_is_dropped_and_not_narrated_with_the_card_off() -> None:
    """The card can be switched off, and with it off the gate handles this.

    ``chat.reasoning`` stays in ``_INTERMEDIATE_STREAM_EVENTS`` precisely so
    that this holds: switching the card off restores the previous behaviour
    rather than turning the model's deliberation into a running commentary.
    """
    client = _RecordingSlackClient()
    channel = _channel(client, activity_card=False, enable_streaming=True)

    await channel.send(_reasoning())
    await channel.send(_reasoning())
    await _settle()

    assert client.posts == []
    assert client.updates == []
    assert not channel._activity_records


@pytest.mark.asyncio
async def test_the_notification_string_says_how_long_the_turn_has_thought() -> None:
    """What a mobile client shows for a turn that has produced nothing else.

    The running form is read off the edit that carried it. The settled form is
    read off the record instead, because this is the one turn that never gets to
    show it: a card holding nothing but the thinking section comes down when the
    turn ends, so no edit is spent on the wording and there is nothing on screen
    to read it from. The string is still built, and is still what a card with
    any other section on it would carry.
    """
    client = _RecordingSlackClient()
    channel = _channel(client)

    await channel.send(_reasoning())
    await _settle()
    record = channel._activity_records[(REQUEST_ID, "C1")]
    record.reasoning_since = time.monotonic() - 75.0
    await channel.send(_reasoning())

    assert client.updates[-1]["text"] == "Working... - thinking for 1m 15s"
    await channel.send(_final("Yes."))
    assert client.deleted == [("C1", "1780000000.000001")]
    assert SlackChannel._activity_summary(record) == (
        "Turn finished - thought for 1m 15s"
    )


# ── Todos ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_an_untouched_todo_list_is_pending_which_only_a_plan_allows() -> None:
    client = _RecordingSlackClient()
    channel = _channel(client)

    await channel.send(_todos("pending", "pending", "pending"))
    await _settle()

    card = _section(client.posts[0], "todos")
    assert card is not None
    # "written down, not started" has no other spelling, and a standalone card
    # would have been rejected outright for using it.
    assert card["status"] == "pending"
    assert _details_text(card) == ":hourglass_flowing_sand: 3"


@pytest.mark.asyncio
async def test_a_todo_list_part_done_is_in_progress() -> None:
    client = _RecordingSlackClient()
    channel = _channel(client)

    await channel.send(_todos("completed", "pending"))
    await _settle()

    card = _section(client.posts[0], "todos")
    assert card["status"] == "in_progress"
    assert _details_text(card) == ":white_check_mark: 1  :hourglass_flowing_sand: 1"


@pytest.mark.asyncio
async def test_a_finished_todo_list_is_complete() -> None:
    client = _RecordingSlackClient()
    channel = _channel(client)

    await channel.send(_todos("completed", "completed"))
    await _settle()

    card = _section(client.posts[0], "todos")
    assert card["status"] == "complete"
    assert _details_text(card) == ":white_check_mark: 2"


@pytest.mark.asyncio
async def test_the_todo_snapshot_replaces_rather_than_accumulates() -> None:
    """Every todo write re-emits the whole list; adding them up counts it twice.

    ``_emit_todo_updated`` loads the list and pushes all of it after every todo
    tool call, so three writes to a three-item list are three snapshots of three
    items -- not nine items.
    """
    client = _RecordingSlackClient()
    channel = _channel(client)

    await channel.send(_todos("pending", "pending", "pending"))
    await channel.send(_todos("in_progress", "pending", "pending"))
    await channel.send(_todos("completed", "in_progress", "pending"))
    await _settle()

    card = _section(client.updates[-1] if client.updates else client.posts[0], "todos")
    assert _details_text(card) == (
        ":white_check_mark: 1  :zap: 1  :hourglass_flowing_sand: 1"
    )


@pytest.mark.asyncio
async def test_an_empty_todo_list_opens_no_card_of_its_own() -> None:
    client = _RecordingSlackClient()
    channel = _channel(client)

    await channel.send(_todos())
    await _settle()

    # A turn that keeps no todos reports an empty list every time the tool is
    # touched. There is no section to show and no record to open.
    assert client.posts == []
    assert not channel._activity_records


@pytest.mark.asyncio
async def test_the_card_never_carries_what_a_todo_actually_says() -> None:
    client = _RecordingSlackClient()
    channel = _channel(client)

    await channel.send(_todos("in_progress", "pending"))
    await _settle()

    rendered = repr(client.posts) + repr(client.updates)
    assert "Sweeping the repositories" not in rendered
    assert "watermark" not in rendered


# ── Subagents ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_fan_out_posts_one_card_for_the_whole_turn() -> None:
    client = _RecordingSlackClient()
    channel = _channel(client)

    await channel.send(_dispatch("call-1"))
    await channel.send(_dispatch("call-2", "browser_agent"))
    await _settle()

    # Two subagents, one message -- twenty would be a wall.
    assert len(client.posts) == 1
    card = _section(client.posts[0], "subagents")
    assert card is not None
    assert card["status"] == "in_progress"
    assert card["task_id"] == f"subagents-{REQUEST_ID}"
    # The title is a section label; the aggregate is the line beneath it.
    assert card["title"] == "Subagents"
    assert _details_text(card) == ":zap: 2"
    # Two types, so the breakdown says something the aggregate does not.
    assert _output_text(card) == "browser_agent  :zap: 1\ngeneral_agent  :zap: 1"
    # The aggregate rides along as the notification string, which is what a
    # mobile client shows and blocks alone would leave blank.
    assert client.posts[0]["text"] == "Working... - 2 of 2 subagents running"


@pytest.mark.asyncio
async def test_a_single_type_fan_out_names_the_type() -> None:
    """The aggregate count alone does not identify which subagent ran."""
    client = _RecordingSlackClient()
    channel = _channel(client)

    await channel.send(_dispatch("call-1"))
    await channel.send(_dispatch("call-2"))
    await _settle()

    card = _section(client.posts[0], "subagents")
    assert _details_text(card) == ":zap: 2"
    assert _output_text(card) == "general_agent  :zap: 2"


@pytest.mark.asyncio
async def test_persistent_spawn_uses_roster_outcome_and_names_its_type() -> None:
    client = _RecordingSlackClient()
    channel = _channel(client)

    await channel.send(_persistent_call("spawn-1"))
    await channel.send(_roster("sub-1"))
    await _settle()
    card = _section(client.posts[0], "subagents")
    assert _output_text(card) == "code_agent  :zap: 1"
    assert _section(client.posts[0], "tools") is None

    # Spawn only confirmed enqueueing; the subagent is still running.
    await channel.send(_result("spawn-1", tool_name="subagent_spawn"))
    assert _section(client.updates[-1] if client.updates else client.posts[0], "subagents")["status"] == "in_progress"
    await channel.send(_tool_call("wait-1", "subagent_wait"))
    await channel.send(_result("wait-1", tool_name="subagent_wait"))
    await channel.send(_roster("sub-1", status="idle", turn_outcome="completed"))

    card = _section(client.updates[-1], "subagents")
    assert card["status"] == "complete"
    assert _output_text(card) == "code_agent  :white_check_mark: 1"
    assert _section(client.updates[-1], "tools") is None
    assert TASK_DESCRIPTION not in repr(client.posts) + repr(client.updates)


@pytest.mark.asyncio
async def test_persistent_fan_out_tracks_each_instance_and_failure() -> None:
    client = _RecordingSlackClient()
    channel = _channel(client)

    await channel.send(_roster("sub-1", "code_agent"))
    await channel.send(_roster("sub-2", "research_agent"))
    await _settle()
    await channel.send(_roster("sub-1", "code_agent", status="idle", turn_outcome="completed"))
    await channel.send(_roster("sub-2", "research_agent", status="idle", turn_outcome="failed"))
    card = _section(client.updates[-1], "subagents")
    assert card["status"] == "error"
    assert _details_text(card) == ":white_check_mark: 1  :x: 1"
    assert _output_text(card) == (
        "code_agent  :white_check_mark: 1\nresearch_agent  :x: 1"
    )


@pytest.mark.asyncio
async def test_task_tool_roster_is_not_counted_twice() -> None:
    client = _RecordingSlackClient()
    channel = _channel(client)

    await channel.send(_dispatch("call-1", "browser_agent"))
    await channel.send(_roster("sub-1", "browser_agent", dispatch_source="task_tool"))
    await _settle()
    await channel.send(_result("call-1"))
    await channel.send(_roster("sub-1", "browser_agent", status="idle", turn_outcome="completed", dispatch_source="task_tool"))
    card = _section(client.updates[-1], "subagents")
    assert _details_text(card) == ":white_check_mark: 1"
    assert _output_text(card) == "browser_agent  :white_check_mark: 1"


def test_task_tool_roster_marks_its_dispatch_source() -> None:
    projection = _build_projection(
        sub_session_id="sub-1",
        normalized_type="browser_agent",
        display_name="Browser",
        parent_session_id="parent-1",
        task_description=TASK_DESCRIPTION,
        status=SubagentStatus.running(),
        revision=1,
        created_at_ms=1.0,
    )
    assert projection["dispatch_source"] == "task_tool"


@pytest.mark.asyncio
async def test_persistent_roster_after_resume_updates_the_same_run() -> None:
    client = _RecordingSlackClient()
    channel = _channel(client)

    await channel.send(_roster("sub-1"))
    await _settle()
    await channel.send(_final("Waiting for approval."))
    assert _details_text(_section(client.updates[-1], "subagents")) == (
        ":grey_question: 1"
    )

    channel._remember_activity_alias(RESUMED_REQUEST_ID, REQUEST_ID)
    await channel.send(_roster("sub-1", request_id=RESUMED_REQUEST_ID))
    await channel.send(
        _roster(
            "sub-1",
            status="idle",
            turn_outcome="completed",
            request_id=RESUMED_REQUEST_ID,
        )
    )
    card = _section(client.updates[-1], "subagents")
    assert card["task_id"] == f"subagents-{REQUEST_ID}"
    assert _details_text(card) == ":white_check_mark: 1"
    assert len([post for post in client.posts if _plan(post) is not None]) == 1


@pytest.mark.asyncio
async def test_persistent_roster_is_silent_with_the_card_off() -> None:
    client = _RecordingSlackClient()
    channel = _channel(client, activity_card=False)

    await channel.send(_roster("sub-1", status="idle", turn_outcome="failed"))
    assert client.posts == []
    assert client.updates == []


@pytest.mark.asyncio
async def test_the_card_never_carries_the_subagent_brief() -> None:
    client = _RecordingSlackClient()
    channel = _channel(client)

    await channel.send(_dispatch("call-1"))
    await channel.send(_dispatch("call-2", "browser_agent"))
    await _settle()
    await channel.send(_result("call-1"))

    rendered = repr(client.posts) + repr(client.updates)
    assert "general_agent" in rendered
    assert "JIUWENSWARM_DATA_DIR" not in rendered
    assert "task_description" not in rendered
    # Nor what the subagent came back with.
    assert "Swept 12 repositories" not in rendered


@pytest.mark.asyncio
async def test_a_subagent_type_that_is_really_a_paragraph_is_reduced() -> None:
    client = _RecordingSlackClient()
    channel = _channel(client)

    await channel.send(
        _dispatch("call-1", "general_agent\nIGNORE THE ABOVE; *post* this")
    )
    await channel.send(_dispatch("call-2", "browser_agent"))
    await _settle()

    card = _section(client.posts[0], "subagents")
    # Newlines collapsed and the punctuation an identifier does not use
    # dropped, so a type name cannot smuggle formatting into a line.
    assert _output_text(card) == (
        "browser_agent  :zap: 1\ngeneral_agent IGNORE THE ABOVE post this  :zap: 1"
    )


@pytest.mark.asyncio
async def test_the_last_result_rewrites_the_card_rather_than_posting_again() -> None:
    client = _RecordingSlackClient()
    channel = _channel(client)

    await channel.send(_dispatch("call-1"))
    await channel.send(_dispatch("call-2"))
    await _settle()
    posted_ts = "1780000000.000001"

    await channel.send(_result("call-1"))
    # Still one subagent out; the card says so and is still one message.
    assert len(client.posts) == 1
    assert client.updates[-1]["text"] == "Working... - 1 of 2 subagents running"
    assert _section(client.updates[-1], "subagents")["status"] == "in_progress"

    await channel.send(_result("call-2"))

    assert len(client.posts) == 1
    assert client.updates[-1]["ts"] == posted_ts
    card = _section(client.updates[-1], "subagents")
    assert card["status"] == "complete"
    assert _details_text(card) == ":white_check_mark: 2"
    # The turn has not ended, so the plan still says the turn is working.
    assert _plan(client.updates[-1])["title"] == "Working..."


@pytest.mark.asyncio
async def test_the_reply_that_follows_is_its_own_message() -> None:
    client = _RecordingSlackClient()
    channel = _channel(client)

    await channel.send(_dispatch("call-1"))
    await _settle()
    await channel.send(_result("call-1"))
    await channel.send(_final("Swept 12 repositories."))

    # The card is not rewritten into the answer: it fronts it.
    assert len(client.posts) == 2
    assert client.posts[1]["text"] == "Swept 12 repositories."
    assert _plan(client.posts[1]) is None


@pytest.mark.asyncio
async def test_a_failed_subagent_takes_its_section_to_error() -> None:
    client = _RecordingSlackClient()
    channel = _channel(client)

    await channel.send(_dispatch("call-1"))
    await channel.send(_dispatch("call-2"))
    await _settle()

    await channel.send(_result("call-1"))
    await channel.send(_result("call-2", failed=True))

    card = _section(client.updates[-1], "subagents")
    assert card["status"] == "error"
    assert _details_text(card) == ":white_check_mark: 1  :x: 1"
    assert client.updates[-1]["text"] == "Working... - 1 of 2 subagents failed"


@pytest.mark.asyncio
async def test_a_turn_that_ends_without_a_result_is_not_left_running() -> None:
    client = _RecordingSlackClient()
    channel = _channel(client)

    await channel.send(_dispatch("call-1"))
    await channel.send(_dispatch("call-2"))
    await _settle()

    # call-2 never reports: the turn was cancelled under it.
    await channel.send(_result("call-1"))
    await channel.send(
        _event(EventType.CHAT_ERROR, {"error": "the model endpoint went away"})
    )

    card = _section(client.updates[-1], "subagents")
    assert card["status"] == "error"
    # The run nobody heard from is named as such rather than called a failure:
    # nothing established that it failed. The one that did report is left
    # alone -- it returned a result, whatever became of the turn afterwards.
    assert _details_text(card) == ":white_check_mark: 1  :grey_question: 1"
    # The turn itself died, which is a wider statement than any count of runs
    # and replaces the stem rather than joining the notes.
    assert _plan(client.updates[-1])["title"] == (
        "Turn failed, 1 reported no result"
    )


@pytest.mark.asyncio
async def test_each_type_gets_its_own_line_in_alphabetical_order() -> None:
    client = _RecordingSlackClient()
    channel = _channel(client)

    await channel.send(_dispatch("call-1", "general_agent"))
    await channel.send(_dispatch("call-2", "general_agent"))
    await channel.send(_dispatch("call-3", "browser_agent"))
    await _settle()
    await channel.send(_result("call-3"))

    card = _section(client.updates[-1], "subagents")
    # Alphabetical, so a type keeps its line across the edits that rewrite the
    # card in place; dispatch order would move it and means nothing anyway once
    # subagents run at the same time.
    assert _output_text(card) == (
        "browser_agent  :white_check_mark: 1\ngeneral_agent  :zap: 2"
    )
    assert _details_text(card) == ":white_check_mark: 1  :zap: 2"


@pytest.mark.asyncio
async def test_a_state_no_run_is_in_is_left_off_the_line() -> None:
    client = _RecordingSlackClient()
    channel = _channel(client)

    await channel.send(_dispatch("call-1"))
    await channel.send(_dispatch("call-2"))
    await _settle()

    card = _section(client.posts[0], "subagents")
    # Everything is running, so the line is one count. A zero is not
    # information and four states spelt out every time would be a table.
    assert _details_text(card) == ":zap: 2"
    named = {
        leaf["name"] for leaf in _leaves(card.get("details")) if leaf["type"] == "emoji"
    }
    assert named == {"zap"}


@pytest.mark.asyncio
async def test_a_type_is_set_as_code_and_carries_no_numbering() -> None:
    client = _RecordingSlackClient()
    channel = _channel(client)

    for call_id in ("call-1", "call-2", "call-3", "call-4", "call-5"):
        await channel.send(_dispatch(call_id))
    await channel.send(_dispatch("call-6", "browser_agent"))
    await _settle()

    card = _section(client.posts[0], "subagents")
    assert _leaves(card.get("output"))[0] == {
        "type": "text",
        "text": "browser_agent",
        "style": {"code": True},
    }
    # No row per subagent and no numbering: five runs of one type are one line.
    assert "1." not in _output_text(card)


# ── Tools ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_tools_section_counts_one_line_per_tool() -> None:
    client = _RecordingSlackClient()
    channel = _channel(client)

    await channel.send(_tool_call("call-1", "bash_tool"))
    await channel.send(_result("call-1", tool_name="bash_tool", result="ok"))
    await channel.send(_tool_call("call-2", "bash_tool"))
    await channel.send(_result("call-2", tool_name="bash_tool", result="ok"))
    await channel.send(_tool_call("call-3", "read_file"))
    await _settle()

    card = _section(client.updates[-1] if client.updates else client.posts[0], "tools")
    assert card["title"] == "Tools"
    assert card["status"] == "in_progress"
    assert _details_text(card) == (
        "bash_tool  :white_check_mark: 2\nread_file  :zap: 1"
    )


@pytest.mark.asyncio
async def test_task_tool_is_never_counted_among_the_tools() -> None:
    """A dispatch is the subagents section's business; counting it twice is a lie."""
    client = _RecordingSlackClient()
    channel = _channel(client)

    await channel.send(_dispatch("call-1"))
    await channel.send(_tool_call("call-2", "bash_tool"))
    await _settle()
    await channel.send(_result("call-1"))

    card = _section(client.updates[-1], "tools")
    assert _details_text(card) == "bash_tool  :zap: 1"
    assert "task_tool" not in repr(client.posts) + repr(client.updates)


@pytest.mark.asyncio
async def test_a_running_tool_is_named_in_output_with_how_long_it_has_been_out() -> None:
    """No payload holds a duration, so the call is clocked when it arrives."""
    client = _RecordingSlackClient()
    channel = _channel(client)

    await channel.send(_tool_call("call-1", "bash_tool"))
    await _settle()
    record = channel._activity_records[(REQUEST_ID, "C1")]
    record.tool_runs["call-1"].started_at = time.monotonic() - 42.0

    # Any edit re-renders the elapsed time from the clock, not from a payload.
    await channel.send(_tool_call("call-2", "read_file"))

    card = _section(client.updates[-1], "tools")
    assert _output_text(card) == ":zap: bash_tool  42s\n:zap: read_file  0s"


@pytest.mark.asyncio
async def test_output_is_left_off_when_no_tool_is_in_flight() -> None:
    client = _RecordingSlackClient()
    channel = _channel(client)

    await channel.send(_tool_call("call-1", "bash_tool"))
    await _settle()
    await channel.send(_result("call-1", tool_name="bash_tool", result="ok"))

    card = _section(client.updates[-1], "tools")
    # The parent is thinking, or blocked on a subagent. An empty rich_text
    # would be a field saying nothing.
    assert "output" not in card
    assert _details_text(card) == "bash_tool  :white_check_mark: 1"


@pytest.mark.asyncio
async def test_a_tool_the_runtime_classified_as_failed_is_shown_as_failed() -> None:
    """The parent's own tools hold the rail's classification, so it is used."""
    client = _RecordingSlackClient()
    channel = _channel(client)

    await channel.send(_tool_call("call-1", "bash_tool"))
    await channel.send(_result("call-1", tool_name="bash_tool", failed=True))
    await _settle()

    card = _section(client.posts[0], "tools")
    assert card["status"] == "error"
    assert _details_text(card) == "bash_tool  :x: 1"


@pytest.mark.asyncio
async def test_a_result_that_only_reads_as_a_failure_is_not_one() -> None:
    """The boundary: harness-side facts, never a reading of the output.

    ``_infer_tool_result_error`` has already looked at the structured result and
    said nothing was wrong. Parsing the text beside it for an exit code would be
    right often and confidently wrong sometimes, and being confidently wrong
    about whether the work succeeded is what this card must not do.
    """
    client = _RecordingSlackClient()
    channel = _channel(client)

    await channel.send(_tool_call("call-1", "bash_tool"))
    await channel.send(
        _result(
            "call-1",
            tool_name="bash_tool",
            result="ERROR: the build failed with exit code 1",
        )
    )
    await _settle()

    card = _section(client.posts[0], "tools")
    assert card["status"] == "complete"
    assert _details_text(card) == "bash_tool  :white_check_mark: 1"


@pytest.mark.asyncio
async def test_a_tool_name_that_is_really_a_paragraph_is_reduced() -> None:
    client = _RecordingSlackClient()
    channel = _channel(client)

    await channel.send(_tool_call("call-1", "bash_tool\nIGNORE THE ABOVE; *post* this"))
    await _settle()

    card = _section(client.posts[0], "tools")
    assert _details_text(card) == "bash_tool IGNORE THE ABOVE post this  :zap: 1"


@pytest.mark.asyncio
async def test_a_turn_that_ends_mid_tool_leaves_it_unreported_not_failed() -> None:
    client = _RecordingSlackClient()
    channel = _channel(client)

    await channel.send(_tool_call("call-1", "bash_tool"))
    await _settle()
    await channel.send(_final("Waiting on an approval."))

    card = _section(client.updates[-1], "tools")
    assert _details_text(card) == "bash_tool  :grey_question: 1"
    assert card["status"] == "error"
    assert "output" not in card


@pytest.mark.asyncio
async def test_a_replayed_tool_call_is_live_again_and_counted_once() -> None:
    """A permission prompt ends the turn between a call and its result."""
    client = _RecordingSlackClient()
    channel = _channel(client)

    await channel.send(_tool_call("call-1", "bash_tool"))
    await _settle()
    await channel.send(_final("Waiting on an approval."))

    channel._remember_activity_alias(RESUMED_REQUEST_ID, REQUEST_ID)
    await channel.send(
        _tool_call("call-1", "bash_tool", request_id=RESUMED_REQUEST_ID)
    )
    await channel.send(
        _result(
            "call-1",
            tool_name="bash_tool",
            result="ok",
            request_id=RESUMED_REQUEST_ID,
        )
    )

    card = _section(client.updates[-1], "tools")
    assert _details_text(card) == "bash_tool  :white_check_mark: 1"


def test_the_older_tool_runs_survive_as_counts() -> None:
    """A long turn calls hundreds of tools; the run objects are the recent ones."""
    channel = _channel(_RecordingSlackClient())
    record = slack_connect._SlackActivityRecord(
        request_id=REQUEST_ID, channel_id="C1", thread_ts=""
    )

    for index in range(slack_connect._MAX_TOOL_RUNS + 25):
        call_id = f"call-{index}"
        channel._note_tool_call(record, call_id, "bash_tool")
        record.tool_runs[call_id].state = slack_connect._RUN_DONE

    assert len(record.tool_runs) <= slack_connect._MAX_TOOL_RUNS
    # Nothing is lost from the count, only from what is held.
    assert record.tools_by_name() == {
        "bash_tool": {slack_connect._RUN_DONE: slack_connect._MAX_TOOL_RUNS + 25}
    }


def test_an_elapsed_time_is_ascii_at_every_scale() -> None:
    assert SlackChannel._format_elapsed(0.4) == "0s"
    assert SlackChannel._format_elapsed(42.9) == "42s"
    assert SlackChannel._format_elapsed(64) == "1m 04s"
    assert SlackChannel._format_elapsed(3 * 3600 + 7 * 60) == "3h 07m"
    for value in (0.4, 42.9, 64, 3 * 3600 + 7 * 60):
        assert SlackChannel._format_elapsed(value).isascii()


# ── Context compression ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_turn_that_only_compacts_its_context_still_gets_a_card() -> None:
    """The case the section was written for, and the one nothing else covers.

    Compaction runs inside ``get_context_window``, before the model is called
    even once, so the turn emits no tool call, no todo, no dispatch and not one
    chunk of reasoning while it happens. The turn this reproduces spent six
    minutes and nineteen seconds there and then answered in twenty-one seconds,
    and every other section of this card is built from work that had not
    happened yet.
    """
    client = _RecordingSlackClient()
    channel = _channel(client)

    await channel.send(_compaction_started())
    await _settle()

    assert len(client.posts) == 1
    assert _order(client.posts[0]) == ["compression"]
    card = _section(client.posts[0], "compression")
    assert card["title"] == "Context compression"
    assert card["status"] == "in_progress"


@pytest.mark.asyncio
async def test_the_running_line_says_how_big_the_thing_being_compacted_is() -> None:
    """The one answer a spinner cannot give.

    Two hundred messages at sixty-five per cent of the window explains a
    six-minute wait; "working" explains nothing, and the reader is left unable
    to tell a slow turn from a wedged one.
    """
    client = _RecordingSlackClient()
    channel = _channel(client)

    await channel.send(_compaction_started())
    await _settle()

    details = _details_text(_section(client.posts[0], "compression"))
    assert details.startswith(":zap: DialogueCompressor")
    assert "201 messages" in details
    # Grouped, because the size is the whole reason the number is on the card.
    assert "685,440 tokens" in details
    assert "65% of window" in details


@pytest.mark.asyncio
async def test_a_finished_pass_reports_what_it_saved_and_how_long_it_took() -> None:
    """The duration is the runtime's own, being the only one measured correctly.

    Every other duration on this card is clocked here, because no payload holds
    one. This payload does, and it was measured around the work rather than
    around the arrival of the event that reports it.
    """
    client = _RecordingSlackClient()
    channel = _channel(client)

    await channel.send(_compaction_started())
    await _settle()
    await channel.send(_compaction_completed())

    card = _section(client.updates[-1], "compression")
    assert card["status"] == "complete"
    details = _details_text(card)
    assert ":white_check_mark: DialogueCompressor" in details
    assert "saved 198 messages, 655,437 tokens (96%)" in details
    assert "6m 19s" in details


@pytest.mark.asyncio
async def test_a_processor_that_saved_nothing_is_still_shown() -> None:
    """One line per processor, in the order the runtime ran them.

    An operator reading a card with two processors on it, one of which recovered
    nothing, has learnt something about how the deployment is configured that no
    other surface tells them. Summing the two would report one average that
    describes neither.
    """
    client = _RecordingSlackClient()
    channel = _channel(client)

    await channel.send(_compaction_noop())
    await channel.send(_compaction_started())
    await _settle()

    details = _details_text(_section(client.posts[0], "compression"))
    lines = details.split("\n")
    assert lines[0].startswith(":heavy_minus_sign: MessageSummaryOffloader")
    assert "saved nothing" in lines[0]
    assert lines[1].startswith(":zap: DialogueCompressor")


@pytest.mark.asyncio
async def test_a_short_compaction_leaves_nothing_behind() -> None:
    """The delay decides, exactly as it does for every other section.

    A pass that finishes inside it is over before anyone could have read a card
    about it, and a channel the bot works in all day is not the place to narrate
    every threshold the runtime crosses.
    """
    client = _RecordingSlackClient()
    channel = _channel(client, activity_card_delay_seconds=30.0)

    await channel.send(_compaction_started())
    await channel.send(_compaction_completed())
    await channel.send(_final("Yes."))
    await _settle()

    assert len(client.updates) == 0
    assert [post["text"] for post in client.posts] == ["Yes."]
    assert not channel._activity_records


@pytest.mark.asyncio
async def test_the_card_never_carries_the_summary_the_compressor_wrote() -> None:
    """``compact_summary`` is the model's prose about a whole conversation.

    High-volume unvetted output, exactly like a line of reasoning, and this
    section shows counts and durations for exactly the same reason the thinking
    section shows a number and no text.
    """
    client = _RecordingSlackClient()
    channel = _channel(client)

    await channel.send(_compaction_started())
    await _settle()
    await channel.send(_compaction_completed())

    for call in _calls(client):
        assert COMPACT_SUMMARY not in json.dumps(call)


@pytest.mark.asyncio
async def test_a_compaction_that_was_cancelled_says_so_and_not_failed() -> None:
    """The runtime reports a cancel as ``failed`` with the error ``cancelled``.

    Chipped complete, the section would say the compaction finished when it was
    abandoned. Worded "failed", it would report a fault in a turn that did
    exactly what it was told and then went on to answer -- a cancelled
    compaction does not end the turn, the runtime logs a warning and carries on.
    The sentinel is matched rather than the error text printed: no runtime error
    string reaches this section.
    """
    client = _RecordingSlackClient()
    channel = _channel(client)

    await channel.send(_compaction_started())
    await _settle()
    await channel.send(_compaction_failed())

    card = _section(client.updates[-1], "compression")
    assert card["status"] == "error"
    details = _details_text(card)
    assert ":x: DialogueCompressor" in details
    assert "cancelled" in details
    assert "failed" not in details
    assert client.updates[-1]["text"].endswith("context compaction cancelled")


@pytest.mark.asyncio
async def test_a_compaction_that_died_on_its_own_is_a_failure() -> None:
    """Anything that is not the cancel sentinel keeps the plain wording.

    The error text itself never reaches the card. It is the runtime's, not the
    model's, but this section carries counts and durations and the one unvetted
    string this card shows has a preformatted block and a section of its own.
    """
    client = _RecordingSlackClient()
    channel = _channel(client)

    await channel.send(_compaction_started())
    await _settle()
    await channel.send(
        _compaction(
            status="failed",
            duration_ms=9_000,
            error="ConnectionResetError: peer closed the model stream",
        )
    )

    details = _details_text(_section(client.updates[-1], "compression"))
    assert "failed" in details
    assert "ConnectionResetError" not in details


@pytest.mark.asyncio
async def test_an_instant_pass_prints_no_duration() -> None:
    """"saved nothing  0s" reads as a rendering fault, not as a measurement.

    The figure is on the line to explain a wait, so a line with no wait to
    explain does without it.
    """
    client = _RecordingSlackClient()
    channel = _channel(client)

    await channel.send(_compaction_noop())
    await channel.send(_compaction_started())
    await _settle()

    details = _details_text(_section(client.posts[0], "compression"))
    assert details.split("\n")[0].endswith("saved nothing")


@pytest.mark.asyncio
async def test_a_turn_that_ends_mid_compaction_leaves_it_unreported() -> None:
    """Never heard from, which is not the same as having failed.

    The runtime emits a terminal state of its own when a cancel reaches it, so
    this is for the endings that arrive before that one does or instead of it.
    """
    client = _RecordingSlackClient()
    channel = _channel(client)

    await channel.send(_compaction_started())
    await _settle()
    await channel.send(_final("Done."))

    card = _section(client.updates[-1], "compression")
    assert card["status"] == "error"
    assert "reported no result" in _details_text(card)


@pytest.mark.asyncio
async def test_the_compaction_section_leads_the_card() -> None:
    """It happens before the model is called, so it is first on the card."""
    client = _RecordingSlackClient()
    channel = _channel(client)

    await channel.send(_reasoning())
    await channel.send(_compaction_started())
    await channel.send(_tool_call("call-1", "bash_tool"))
    await _settle()

    last = client.updates[-1] if client.updates else client.posts[0]
    assert _order(last) == ["compression", "thinking", "tools"]


@pytest.mark.asyncio
async def test_compacting_stops_the_thinking_clock() -> None:
    """The model is not deliberating while the runtime folds up its context.

    A thinking section counting up through six minutes of compaction beside a
    compaction section counting the same six minutes reports one wait twice and
    attributes it to the wrong thing.
    """
    client = _RecordingSlackClient()
    channel = _channel(client)

    await channel.send(_reasoning())
    await channel.send(_compaction_started())
    await _settle()

    assert _section(client.posts[0], "thinking")["status"] == "complete"
    assert _section(client.posts[0], "compression")["status"] == "in_progress"


@pytest.mark.asyncio
async def test_the_notification_string_says_what_is_being_compacted() -> None:
    """What a mobile client shows for a turn that has produced nothing else."""
    client = _RecordingSlackClient()
    channel = _channel(client)

    await channel.send(_compaction_started())
    await _settle()

    assert client.posts[0]["text"] == (
        "Working... - compacting 201 messages of context"
    )


@pytest.mark.asyncio
async def test_a_failed_compaction_is_never_posted_as_a_bare_error() -> None:
    """The one of the five progress events that was never dropped.

    ``EventType`` has no member for ``context.compression_state``, so the
    gateway leaves ``msg.event_type`` unset and the frozenset of enum members
    that drops the other four cannot hold it. It reached
    ``_extract_outgoing_text``, which reads ``error`` off any payload with no
    ``content`` -- so a cancelled compaction posted the bare word "cancelled"
    into the channel with nothing beside it to say what had been cancelled.

    Consumed with the card off for that reason, unlike the reasoning chunk: what
    switching the card off would restore here is a defect.
    """
    client = _RecordingSlackClient()
    channel = _channel(client, activity_card=False, enable_streaming=True)

    await channel.send(_compaction_started())
    await channel.send(_compaction_failed())
    await _settle()

    assert client.posts == []
    assert client.updates == []
    assert not channel._activity_records


@pytest.mark.asyncio
async def test_a_state_the_connector_has_no_wording_for_is_swallowed() -> None:
    """Rendered as nothing rather than as a word nobody chose.

    The runtime's vocabulary is five values wide today. A sixth must not reach
    the channel as raw text, and it must not reach it down the error path
    either.
    """
    client = _RecordingSlackClient()
    channel = _channel(client)

    await channel.send(_compaction(status="rescheduled"))
    await _settle()

    assert client.posts == []
    assert not channel._activity_records


@pytest.mark.asyncio
async def test_the_card_posted_during_a_compaction_carries_the_stop_button() -> None:
    """A stop during compaction reaches the compaction, so the button appears.

    Established by reading the path rather than assumed from the button
    rendering: ``chat.interrupt`` reaches ``_cancel_scheduler_running_tasks``,
    which cancels the scheduler's exec task; ``get_context_window`` is awaited
    directly inside that task, through ``react_agent.invoke``; and the
    compressor's model call is a plain ``await`` with no thread, no executor and
    no shield between the two. The runtime then catches the ``CancelledError``,
    reports the pass as ``failed`` with the error ``cancelled``, and re-raises.

    This is the only long stretch of a turn where the button used to be
    unreachable, because there was no card to put it on -- which is the half of
    the original problem that cost the reader a control rather than a status
    line.
    """
    client = _RecordingSlackClient()
    channel = _channel(client)

    await channel.send(_compaction_started())
    await _settle()

    blocks = client.posts[0]["blocks"]
    assert [block["type"] for block in blocks] == ["plan", "actions"]
    button = blocks[1]["elements"][0]
    assert json.loads(button["value"])["session_id"] == "slack_T1_C1_root"


@pytest.mark.asyncio
async def test_a_processor_name_that_is_really_a_paragraph_is_reduced() -> None:
    """A label is a label whether the runtime or the model chose it.

    One kind of text on this card must not become two because of where it came
    from.
    """
    client = _RecordingSlackClient()
    channel = _channel(client)

    await channel.send(
        _compaction(processor="Dialogue\nCompressor <b>v2</b> " + "x" * 200)
    )
    await _settle()

    details = _details_text(_section(client.posts[0], "compression"))
    assert "\n" not in details
    assert "<b>" not in details
    assert len(details) < 200


# ── The plan's own line ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_title_says_the_turn_finished_and_not_that_it_went_well() -> None:
    """Harness-side facts only: the turn ended, which is all anyone saw.

    Two subagents running ``sleep 10 && exit 1`` and ``sleep 20 && exit 2``
    correctly show as complete: the subagents ran fine, their commands failed.
    A subagent's tool results never leave the subagent, so no failed-tool event
    ever arrives, and the exit codes reach this connector only as prose inside
    a result nobody parses here.
    """
    client = _RecordingSlackClient()
    channel = _channel(client)

    await channel.send(_dispatch("call-1"))
    await channel.send(_dispatch("call-2"))
    await _settle()
    await channel.send(_result("call-1", result="Command failed: exit code 1"))
    await channel.send(_result("call-2", result="Command failed: exit code 2"))
    await channel.send(_final("Both subagents reported failures."))

    assert _plan(client.updates[-1])["title"] == "Turn finished"
    assert _section(client.updates[-1], "subagents")["status"] == "complete"


@pytest.mark.asyncio
async def test_the_title_names_failures_and_missing_results_separately() -> None:
    client = _RecordingSlackClient()
    channel = _channel(client)

    await channel.send(_tool_call("call-1", "bash_tool"))
    await channel.send(_result("call-1", tool_name="bash_tool", failed=True))
    await channel.send(_tool_call("call-2", "read_file"))
    await _settle()
    await channel.send(_final("Stopped."))

    assert _plan(client.updates[-1])["title"] == (
        "Turn finished, 1 failed, 1 reported no result"
    )


@pytest.mark.asyncio
async def test_the_notification_string_carries_every_section_s_aggregate() -> None:
    client = _RecordingSlackClient()
    channel = _channel(client)

    await channel.send(_todos("completed", "pending", "pending"))
    await channel.send(_dispatch("call-1"))
    await channel.send(_tool_call("call-2", "bash_tool"))
    await _settle()

    assert client.posts[0]["text"] == (
        "Working... - 1 of 3 todos done - 1 of 1 subagent running"
        " - 1 of 1 tool running"
    )


# ── A turn that died ─────────────────────────────────────────────────────────
#
# A stream that stopped arriving, a tool loop the runtime gave up on, a run
# killed by a restart. The card used to chip "complete" and say "Turn finished"
# over every one of them while the error arrived as a plain message beside it,
# which is worse than no card: whoever was waiting on the answer read
# "finished" and stopped watching.


@pytest.mark.asyncio
async def test_a_harness_error_becomes_a_section_of_the_card_that_is_up() -> None:
    client = _RecordingSlackClient()
    channel = _channel(client)

    await channel.send(_tool_call("call-1", "bash_tool"))
    await channel.send(_result("call-1", tool_name="bash_tool", result="ok"))
    await _settle()
    await channel.send(_error())

    card = _section(client.updates[-1], "error")
    assert card["title"] == "Harness error"
    assert card["status"] == "error"
    # A sentence, then the error exactly as it arrived.
    assert _details_text(card).startswith("The turn ended before it answered.")
    assert _preformatted(card) == STREAM_TIMEOUT_ERROR
    assert _plan(client.updates[-1])["title"] == "Turn failed"
    # Last: it is the last thing that happened, and the sections above it are
    # what the turn managed before it did.
    assert _order(client.updates[-1]) == ["tools", "error"]


@pytest.mark.asyncio
async def test_nothing_already_on_the_card_is_called_failed_by_a_harness_error() -> None:
    """Attribution is not available, so none is invented.

    ``[181001]`` belongs to the turn's own model call. The tool that returned
    before it did returned fine, and an error chip on that tool is a claim
    someone will act on.
    """
    client = _RecordingSlackClient()
    channel = _channel(client)

    await channel.send(_todos("completed"))
    await channel.send(_dispatch("call-1"))
    await channel.send(_tool_call("call-2", "bash_tool"))
    await _settle()
    await channel.send(_result("call-1"))
    await channel.send(_result("call-2", tool_name="bash_tool", result="ok"))
    await channel.send(_error())

    last = client.updates[-1]
    assert _section(last, "todos")["status"] == "complete"
    assert _section(last, "subagents")["status"] == "complete"
    assert _section(last, "tools")["status"] == "complete"
    assert _details_text(_section(last, "tools")) == "bash_tool  :white_check_mark: 1"
    assert _section(last, "error")["status"] == "error"


@pytest.mark.asyncio
async def test_the_error_is_not_also_posted_as_a_message_of_its_own() -> None:
    client = _RecordingSlackClient()
    channel = _channel(client)

    await channel.send(_tool_call("call-1", "bash_tool"))
    await _settle()
    await channel.send(_error())

    # One post -- the card -- and one edit, which is the card saying the turn
    # died. Nothing underneath it repeating the error as text.
    assert len(client.posts) == 1
    assert _plan(client.posts[0]) is not None
    assert len(client.updates) == 1
    assert not [call for call in _calls(client) if _plan(call) is None]


@pytest.mark.asyncio
async def test_a_turn_that_died_with_no_card_up_posts_one_standalone() -> None:
    """The ``[181001]`` turn: no tool, no todo, no dispatch, no record.

    There is nothing on screen to amend and nothing to amend it with, and the
    failure cannot ride along with the reply either -- a message holds a plan
    or bare task cards, never both. So the card is posted in its own message,
    which is the only shape Slack leaves available.
    """
    client = _RecordingSlackClient()
    channel = _channel(client, activity_card_delay_seconds=30.0)

    await channel.send(_error())
    await _settle()

    assert len(client.posts) == 1
    assert len(client.updates) == 0
    assert _order(client.posts[0]) == ["error"]
    assert _preformatted(_section(client.posts[0], "error")) == STREAM_TIMEOUT_ERROR
    assert _plan(client.posts[0])["title"] == "Turn failed"


@pytest.mark.asyncio
async def test_a_turn_that_died_inside_the_delay_still_says_so() -> None:
    """The delay is for turns nobody had time to notice. A failure is not one."""
    client = _RecordingSlackClient()
    channel = _channel(client, activity_card_delay_seconds=30.0)

    await channel.send(_tool_call("call-1", "bash_tool"))
    await channel.send(_error())
    await _settle()

    assert len(client.posts) == 1
    assert _order(client.posts[0]) == ["tools", "error"]
    # The tool was still out when the turn died, so it is unreported rather
    # than failed: the harness error says nothing about that call.
    assert _details_text(_section(client.posts[0], "tools")) == (
        "bash_tool  :grey_question: 1"
    )


@pytest.mark.asyncio
async def test_the_notification_line_says_the_turn_failed_and_why() -> None:
    """The one part of the message Slack cannot refuse.

    It is the mobile notification, the search result, and the whole of the
    message if the blocks are rejected -- which is why the error is in it and
    not only in the card.
    """
    client = _RecordingSlackClient()
    channel = _channel(client)

    await channel.send(_tool_call("call-1", "bash_tool"))
    await _settle()
    await channel.send(_error())

    assert client.updates[-1]["text"] == (
        "Turn failed, 1 reported no result"
        " - 1 of 1 tool reported no result"
        f" - {STREAM_TIMEOUT_ERROR}"
    )


@pytest.mark.asyncio
async def test_an_error_too_long_for_the_field_is_cut_and_says_it_was() -> None:
    """2000 characters is the ceiling, and a silent cut reads as the whole of it."""
    client = _RecordingSlackClient()
    channel = _channel(client)

    await channel.send(_tool_call("call-1", "bash_tool"))
    await _settle()
    await channel.send(_error("[153001] tool loop abandoned\n" + "x" * 4000))

    card = _section(client.updates[-1], "error")
    assert len(_details_text(card)) <= slack_connect._MAX_CARD_DETAILS_LENGTH
    # The head is what names the failure, so the tail is what goes.
    assert _preformatted(card).startswith("[153001] tool loop abandoned")
    assert _preformatted(card).endswith("[trimmed]")


@pytest.mark.asyncio
async def test_a_failed_turn_never_leaves_the_channel_empty() -> None:
    """The guard on the whole change: suppression is conditional on the card.

    Every way the card can fail to appear is a way a failed turn could post
    nothing at all, which is strictly worse than the duplicate this replaces.
    So each of them is checked for the one thing that must hold: something
    reached the channel, and it says what went wrong.
    """
    for label, client, config in (
        # The card cannot be posted, so the error goes out as a message.
        ("no card can be posted", _BlockRefusingSlackClient(), {}),
        # The card is up but will not take the edit that would hold the error.
        ("the card will not take the edit", _UpdateRefusingSlackClient(), {}),
        # The operator switched the card off entirely.
        ("the card is switched off", _RecordingSlackClient(), {"activity_card": False}),
    ):
        channel = _channel(client, **config)
        await channel.send(_tool_call("call-1", "bash_tool"))
        await _settle()
        await channel.send(_error())

        carried = [
            call
            for call in _calls(client)
            if STREAM_TIMEOUT_ERROR in str(call.get("text", ""))
            or STREAM_TIMEOUT_ERROR in str(call.get("blocks", ""))
        ]
        assert carried, f"a failed turn wrote nothing when {label}"


@pytest.mark.asyncio
async def test_a_card_that_cannot_be_posted_gives_the_error_back() -> None:
    client = _BlockRefusingSlackClient()
    channel = _channel(client, activity_card_delay_seconds=30.0)

    await channel.send(_error())
    await _settle()

    # The card was refused, so the message is posted exactly as it was before
    # any of this existed: the error, as text, with no blocks.
    assert len(client.posts) == 1
    assert client.posts[0]["text"] == STREAM_TIMEOUT_ERROR
    assert "blocks" not in client.posts[0]


@pytest.mark.asyncio
async def test_an_error_arriving_after_a_final_is_still_a_message() -> None:
    """A settled card is not reopened, so the error has nowhere else to go."""
    client = _RecordingSlackClient()
    channel = _channel(client)

    await channel.send(_tool_call("call-1", "bash_tool"))
    await _settle()
    await channel.send(_result("call-1", tool_name="bash_tool", result="ok"))
    await channel.send(_final("Done."))
    spent = len(client.updates)

    await channel.send(_error("the stream dropped"))

    assert len(client.updates) == spent
    assert client.posts[-1]["text"] == "the stream dropped"


@pytest.mark.asyncio
async def test_a_finished_turn_grows_no_error_section() -> None:
    client = _RecordingSlackClient()
    channel = _channel(client)

    await channel.send(_tool_call("call-1", "bash_tool"))
    await _settle()
    await channel.send(_result("call-1", tool_name="bash_tool", result="ok"))
    await channel.send(_final("Done."))

    assert _section(client.updates[-1], "error") is None
    assert _plan(client.updates[-1])["title"] == "Turn finished"
    assert client.posts[-1]["text"] == "Done."


@pytest.mark.asyncio
async def test_the_error_section_writes_no_string_of_its_own_in_non_ascii() -> None:
    """Its own strings, that is. The error text is whatever the runtime said.

    A multiplication sign in a text element has taken a whole message down
    before now, so every string this connector chooses is ASCII. The error
    itself is not one of those, and is repeated in the notification line as well
    as in the block precisely so that a rendering Slack refuses still delivers
    the failure as text.
    """
    client = _RecordingSlackClient()
    channel = _channel(client, activity_card_delay_seconds=30.0)

    await channel.send(_error("x" * 4000))
    await _settle()

    card = _section(client.posts[0], "error")
    assert card["title"].isascii()
    assert _details_text(card).isascii()
    assert _plan(client.posts[0])["title"].isascii()


# ── Emoji ────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_state_is_an_emoji_element_and_never_a_shortcode() -> None:
    client = _RecordingSlackClient()
    channel = _channel(client)

    await channel.send(_dispatch("call-1"))
    await _settle()
    await channel.send(_result("call-1"))

    card = _section(client.updates[-1], "subagents")
    elements = _leaves(card.get("details"))
    assert {"type": "emoji", "name": "white_check_mark"} in elements
    # rich_text does not expand a shortcode the way mrkdwn does: written into a
    # text element it would reach the channel as the literal characters.
    for leaf in elements:
        if leaf["type"] == "text":
            assert ":" not in leaf["text"]


# Emoji and pictographs, including the U+2600..U+27BF block the state glyphs
# live in and the variation selector that follows them.
_EMOJI_RANGES = (
    (0x2190, 0x2BFF),
    (0xFE00, 0xFE0F),
    (0x1F000, 0x1FAFF),
)


def _emoji_characters(value: Any) -> list[str]:
    """Every literal emoji character anywhere inside a payload."""
    if isinstance(value, str):
        return [
            char
            for char in value
            if any(low <= ord(char) <= high for low, high in _EMOJI_RANGES)
        ]
    if isinstance(value, dict):
        found: list[str] = []
        for key, item in value.items():
            found.extend(_emoji_characters(key))
            found.extend(_emoji_characters(item))
        return found
    if isinstance(value, list):
        found = []
        for item in value:
            found.extend(_emoji_characters(item))
        return found
    return []


@pytest.mark.asyncio
async def test_no_literal_emoji_character_reaches_the_blocks() -> None:
    client = _RecordingSlackClient()
    channel = _channel(client)

    await channel.send(_reasoning())
    await channel.send(_todos("completed", "in_progress", "pending"))
    await channel.send(_dispatch("call-1", "general_agent"))
    await channel.send(_dispatch("call-2", "browser_agent"))
    await channel.send(_dispatch("call-3", "browser_agent"))
    await channel.send(_tool_call("call-4", "bash_tool"))
    await _settle()
    await channel.send(_result("call-1"))
    await channel.send(_result("call-2", failed=True))
    await channel.send(_final())

    # Every state, across every edit the card took. An emoji is named, never
    # written: the glyph would make the card mean whatever this file happens to
    # be encoded as, and it is not the form Slack was probed with.
    assert client.posts and client.updates
    for call in [*client.posts, *client.updates]:
        assert _emoji_characters(call) == []


@pytest.mark.asyncio
async def test_every_string_in_the_blocks_is_ascii() -> None:
    """One multiplication sign in a text element took a whole message down."""
    client = _RecordingSlackClient()
    channel = _channel(client)

    await channel.send(_reasoning())
    await channel.send(_todos("completed", "pending"))
    await channel.send(_dispatch("call-1"))
    await channel.send(_tool_call("call-2", "bash_tool"))
    await _settle()
    await channel.send(_final())

    def _strings(value: Any) -> list[str]:
        if isinstance(value, str):
            return [value]
        if isinstance(value, dict):
            found: list[str] = []
            for key, item in value.items():
                found.append(key)
                found.extend(_strings(item))
            return found
        if isinstance(value, list):
            found = []
            for item in value:
                found.extend(_strings(item))
            return found
        return []

    for call in [*client.posts, *client.updates]:
        for text in _strings(call.get("blocks")) + _strings(call.get("text")):
            assert text.isascii(), text


# ── Nothing is left behind by a turn nobody had time to notice ───────────────


@pytest.mark.asyncio
async def test_a_turn_that_finishes_inside_the_delay_posts_nothing() -> None:
    client = _RecordingSlackClient()
    channel = _channel(client, activity_card_delay_seconds=30.0)

    await channel.send(_dispatch("call-1"))
    await channel.send(_tool_call("call-2", "bash_tool"))
    await channel.send(_result("call-1"))
    await channel.send(_result("call-2", tool_name="bash_tool", result="ok"))
    await channel.send(_final("Nothing to report."))
    await _settle()

    # One message: the answer. No card was ever posted, and the timer that
    # would have posted one is gone.
    assert len(client.updates) == 0
    assert len(client.posts) == 1
    assert client.posts[0]["text"] == "Nothing to report."
    assert not channel._activity_records


@pytest.mark.asyncio
async def test_the_card_can_be_switched_off_without_the_events_leaking() -> None:
    client = _RecordingSlackClient()
    channel = _channel(client, activity_card=False)

    await channel.send(_todos("pending"))
    await channel.send(_dispatch("call-1"))
    await channel.send(_result("call-1"))
    await channel.send(_tool_call("call-2", "bash_tool"))
    await _settle()

    assert client.posts == []
    assert client.updates == []


@pytest.mark.asyncio
async def test_a_dispatch_with_no_call_id_is_ignored() -> None:
    client = _RecordingSlackClient()
    channel = _channel(client)

    message = _dispatch("call-1")
    message.payload["tool_call"]["tool_call_id"] = ""
    await channel.send(message)
    await _settle()

    # A run nothing can close would hold the card open until the turn ended.
    assert client.posts == []
    assert not channel._activity_records


# ── The edit budget ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_every_completion_earns_an_edit_however_short_the_subagent() -> None:
    """A subagent returning is the news; it is never coalesced away.

    Measured in production: two subagents, the first finishing six seconds
    after the card went up and the second twenty seconds later. With the floor
    applied to completions the first was suppressed, and the card showed its
    first frame and its last and nothing in between -- which for short
    subagents is every fan-out there is.
    """
    client = _RecordingSlackClient()
    channel = _channel(client, activity_card_min_edit_seconds=3600.0)

    await channel.send(_dispatch("call-1"))
    await channel.send(_dispatch("call-2"))
    await channel.send(_dispatch("call-3"))
    await _settle()

    await channel.send(_result("call-1"))
    assert client.updates[-1]["text"] == "Working... - 2 of 3 subagents running"
    await channel.send(_result("call-2"))
    assert client.updates[-1]["text"] == "Working... - 1 of 3 subagents running"

    await channel.send(_result("call-3"))
    assert len(client.updates) == 3
    assert _section(client.updates[-1], "subagents")["status"] == "complete"
    # Still one message: three edits of it, not three cards.
    assert len(client.posts) == 1


@pytest.mark.asyncio
async def test_a_dispatch_joining_a_running_fan_out_waits_out_the_floor() -> None:
    """The other half of the budget: a wave of dispatches is churn, not news.

    Twenty subagents dispatched in one breath move the denominator twenty
    times and say nothing a reader acts on, so they coalesce -- which is what
    keeps the floor worth having now that completions bypass it.
    """
    client = _RecordingSlackClient()
    channel = _channel(client, activity_card_min_edit_seconds=3600.0)

    await channel.send(_dispatch("call-1"))
    await _settle()
    assert client.posts[0]["text"] == "Working... - 1 of 1 subagent running"

    await channel.send(_dispatch("call-2"))
    await channel.send(_dispatch("call-3"))
    assert client.updates == []

    # The completion that follows holds the whole fan-out anyway.
    await channel.send(_result("call-1"))
    assert client.updates[-1]["text"] == "Working... - 2 of 3 subagents running"


@pytest.mark.asyncio
async def test_tool_traffic_and_todo_writes_wait_out_the_floor() -> None:
    """A long turn produces these by the dozen; one edit apiece is the budget gone."""
    client = _RecordingSlackClient()
    channel = _channel(client, activity_card_min_edit_seconds=3600.0)

    await channel.send(_tool_call("call-1", "bash_tool"))
    await _settle()
    assert len(client.posts) == 1

    for index in range(1, 12):
        if index > 1:
            await channel.send(_tool_call(f"call-{index}", "bash_tool"))
        await channel.send(
            _result(f"call-{index}", tool_name="bash_tool", result="ok")
        )
    await channel.send(_todos("in_progress", "pending"))

    assert client.updates == []

    # The turn ending is never withheld, whatever the floor says.
    await channel.send(_final("Done."))
    assert len(client.updates) == 1
    assert _plan(client.updates[-1])["title"] == "Turn finished"


@pytest.mark.asyncio
async def test_a_sequential_fan_out_is_one_card_not_one_per_subagent() -> None:
    """Three subagents, one at a time, one card.

    The fan-out is empty between every pair of them, and a card retired on
    "nothing is running" closed three times over. Observed in production as
    three messages for one turn, each reading "1 subagent finished" -- and a
    model that works through its subagents one at a time is the ordinary case,
    not the parallel burst the feature was tested against.
    """
    client = _RecordingSlackClient()
    channel = _channel(client)

    await channel.send(_dispatch("call-1"))
    await _settle()
    await channel.send(_result("call-1"))

    await channel.send(_dispatch("call-2", "browser_agent"))
    await _settle()
    await channel.send(_result("call-2"))

    await channel.send(_dispatch("call-3"))
    await _settle()
    await channel.send(_result("call-3"))
    await channel.send(_final("Swept 12 repositories."))

    # One card and one answer, not one card per subagent.
    assert len(client.posts) == 2
    assert _plan(client.posts[0]) is not None
    assert _plan(client.posts[1]) is None
    card = _section(client.updates[-1], "subagents")
    assert card["status"] == "complete"
    assert _details_text(card) == ":white_check_mark: 3"
    assert _output_text(card) == (
        "browser_agent  :white_check_mark: 1\ngeneral_agent  :white_check_mark: 2"
    )


@pytest.mark.asyncio
async def test_a_dispatch_after_the_section_read_complete_reopens_it_at_once() -> None:
    """The inverse lie is refused as firmly as the original one.

    A card left saying a request is finished while its next subagent runs is
    the same failure as one left saying a finished fan-out is live, so this
    edit is not held back by the floor even though a dispatch normally is.
    """
    client = _RecordingSlackClient()
    channel = _channel(client, activity_card_min_edit_seconds=3600.0)

    await channel.send(_dispatch("call-1"))
    await _settle()
    await channel.send(_result("call-1"))
    assert _section(client.updates[-1], "subagents")["status"] == "complete"

    await channel.send(_dispatch("call-2", "browser_agent"))

    card = _section(client.updates[-1], "subagents")
    assert card["status"] == "in_progress"
    assert client.updates[-1]["text"] == "Working... - 1 of 2 subagents running"


# ── One request, however many turns it took ──────────────────────────────────


@pytest.mark.asyncio
async def test_a_fan_out_spanning_a_turn_boundary_stays_one_card() -> None:
    """A permission prompt pauses the turn; the resumed turn writes the same card.

    The resumed turn is a different request on the wire and replays the
    dispatches it was interrupted in the middle of. Keyed by tool call id those
    collapse onto the runs already tracked -- and the runs the interrupted turn
    left open, marked ``unreported`` when it ended, are live again, because the
    resumed turn is what re-established them.
    """
    client = _RecordingSlackClient()
    channel = _channel(client)

    await channel.send(_dispatch("call-1"))
    await channel.send(_dispatch("call-2"))
    await _settle()
    assert client.posts[0]["text"] == "Working... - 2 of 2 subagents running"

    # The permission prompt ends the turn with both subagents still out.
    await channel.send(_final("Waiting on an approval."))
    assert client.updates[-1]["text"] == (
        "Turn finished, 2 reported no result - 2 of 2 subagents reported no result"
    )

    channel._remember_activity_alias(RESUMED_REQUEST_ID, REQUEST_ID)
    await channel.send(_dispatch("call-1", request_id=RESUMED_REQUEST_ID))
    await channel.send(_dispatch("call-2", request_id=RESUMED_REQUEST_ID))
    await _settle()
    assert client.updates[-1]["text"] == "Working... - 2 of 2 subagents running"

    await channel.send(_result("call-1", request_id=RESUMED_REQUEST_ID))
    await channel.send(_result("call-2", request_id=RESUMED_REQUEST_ID))
    await channel.send(_final("Swept 12 repositories.", request_id=RESUMED_REQUEST_ID))

    # Two answers were posted, and exactly one card, edited throughout.
    assert [post["text"] for post in client.posts] == [
        "Working... - 2 of 2 subagents running",
        "Waiting on an approval.",
        "Swept 12 repositories.",
    ]
    assert {update["ts"] for update in client.updates} == {"1780000000.000001"}
    card = _section(client.updates[-1], "subagents")
    assert card["status"] == "complete"
    assert card["task_id"] == f"subagents-{REQUEST_ID}"
    assert _plan(client.updates[-1])["title"] == "Turn finished"


@pytest.mark.asyncio
async def test_the_count_is_the_request_s_and_not_the_turn_s() -> None:
    """Two subagents asked for, two reported -- not "2 finished" then "1 finished".

    Observed in production: an interrupt split one request into three turns and
    the card counted each turn separately, which is arithmetically right per
    turn and misleading to the person who asked for two things.
    """
    client = _RecordingSlackClient()
    channel = _channel(client)

    await channel.send(_dispatch("call-1"))
    await channel.send(_dispatch("call-2"))
    await _settle()
    await channel.send(_result("call-1"))
    await channel.send(_result("call-2"))
    await channel.send(_final("Both back; needs an approval to continue."))

    channel._remember_activity_alias(RESUMED_REQUEST_ID, REQUEST_ID)
    await channel.send(_dispatch("call-3", request_id=RESUMED_REQUEST_ID))
    await _settle()
    await channel.send(_result("call-3", request_id=RESUMED_REQUEST_ID))
    await channel.send(_final("Done.", request_id=RESUMED_REQUEST_ID))

    assert client.updates[-1]["text"] == "Turn finished - 3 subagents finished"
    assert _details_text(_section(client.updates[-1], "subagents")) == (
        ":white_check_mark: 3"
    )
    # One card for the request, not one per turn of it.
    assert sum(1 for post in client.posts if _plan(post) is not None) == 1


@pytest.mark.asyncio
async def test_a_second_terminal_event_does_not_spend_another_edit() -> None:
    """The record outliving its turn must not make the turn ending repeatable."""
    client = _RecordingSlackClient()
    channel = _channel(client)

    await channel.send(_dispatch("call-1"))
    await _settle()
    await channel.send(_result("call-1"))
    await channel.send(_final("Done."))
    spent = len(client.updates)

    await channel.send(_event(EventType.CHAT_ERROR, {"error": "the stream dropped"}))

    assert len(client.updates) == spent


@pytest.mark.asyncio
async def test_a_closed_record_is_the_first_thing_given_up_under_the_cap() -> None:
    """Kept records are history, not a leak: they yield before a live turn does."""
    client = _RecordingSlackClient()
    channel = _channel(client)

    # The oldest record of the lot, and the only one still running.
    await channel.send(_dispatch("call-live"))
    await _settle()

    for index in range(slack_connect._MAX_ACTIVITY_RECORDS + 4):
        request_id = f"req-{index}"
        await channel.send(_dispatch(f"call-{index}", request_id=request_id))
        await _settle()
        await channel.send(_result(f"call-{index}", request_id=request_id))
        await channel.send(_final(request_id=request_id))

    assert len(channel._activity_records) <= slack_connect._MAX_ACTIVITY_RECORDS
    # Age alone would have evicted it first; being live is what saves it.
    assert (REQUEST_ID, "C1") in channel._activity_records


@pytest.mark.asyncio
async def test_an_interrupt_chain_collapses_to_the_id_it_started_under() -> None:
    channel = _channel(_RecordingSlackClient())

    channel._remember_activity_alias("resume-1", REQUEST_ID)
    channel._remember_activity_alias("resume-2", "resume-1")

    # Resolved at registration, so a request interrupted twice does not leave a
    # chain for the lookup to walk.
    assert channel._activity_request_aliases["resume-2"] == REQUEST_ID


@pytest.mark.asyncio
async def test_answering_an_interrupt_binds_the_resumed_turn_to_the_request() -> None:
    """The wiring: this connector is the only place both ids are known."""
    client = _RecordingSlackClient()
    channel = _channel(client)
    resumed: list[Message] = []
    channel.on_message(resumed.append)

    pending = slack_connect._SlackPendingQuestion(
        request_id="chatcmpl-tool-0123456789abcdef",
        session_id="slack_T1_C1_root",
        source="permission_interrupt",
        question="Allow the subagent to write that file?",
        labels=["Approve"],
        values=["approve"],
        channel_id="C1",
        message_ts="1780000000.000009",
        turn_request_id=REQUEST_ID,
    )
    await channel._dispatch_answer(
        pending,
        values=["approve"],
        body={
            "container": {"channel_id": "C1", "message_ts": "1710000016.000100"},
            "channel": {"id": "C1"},
        },
        user_id="U01ABCDEF",
    )

    assert len(resumed) == 1
    assert resumed[0].id != REQUEST_ID
    assert channel._activity_request_aliases[resumed[0].id] == REQUEST_ID


@pytest.mark.asyncio
async def test_a_question_records_the_turn_that_asked_it() -> None:
    client = _RecordingSlackClient()
    channel = _channel(client)

    await channel.send(
        _event(
            EventType.CHAT_ASK_USER_QUESTION,
            {
                "request_id": "chatcmpl-tool-0123456789abcdef",
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
        )
    )

    pending = channel._pending_questions["chatcmpl-tool-0123456789abcdef"]
    # ``request_id`` names the question; this names the turn it interrupted,
    # which is what the answer's own request has to be tied back to.
    assert pending.turn_request_id == REQUEST_ID


@pytest.mark.asyncio
async def test_an_answer_that_resumes_nothing_registers_no_alias() -> None:
    """A standalone approval leaves the turn it belongs to running under its own id."""
    client = _RecordingSlackClient()
    channel = _channel(client)
    channel.on_message(lambda req: None)

    pending = slack_connect._SlackPendingQuestion(
        request_id="q-1",
        session_id="slack_T1_C1_root",
        source="tool_approval",
        question="Approve?",
        labels=["Approve"],
        values=["approve"],
        channel_id="C1",
        message_ts="1780000000.000009",
        turn_request_id=REQUEST_ID,
    )
    await channel._dispatch_answer(
        pending,
        values=["approve"],
        body={"container": {"channel_id": "C1", "message_ts": "1710000016.000100"}},
        user_id="U01ABCDEF",
    )

    assert channel._activity_request_aliases == {}


def test_the_shipped_templates_carry_the_keys_with_the_code_defaults() -> None:
    """A key missing from a template is deleted on a config upgrade.

    Both halves matter: the key has to be there at all, and the value it ships
    has to be the value the connector would have used anyway, or an operator who
    has never touched the file gets different behaviour from one who has. The
    two team templates are checked as well: an operator on one of those loses
    any key they do not have.
    """
    import yaml
    from pathlib import Path

    import jiuwenswarm

    resources = Path(jiuwenswarm.__file__).parent / "resources"
    defaults = SlackChannelConfig()
    for name in (
        "config.yaml",
        "config.team.distributed.leader.yaml",
        "config.team.distributed.teammate.yaml",
    ):
        shipped = yaml.safe_load((resources / name).read_text())["channels"]["slack"]
        assert shipped["activity_card"] is defaults.activity_card, name
        assert (
            float(shipped["activity_card_delay_seconds"])
            == defaults.activity_card_delay_seconds
        ), name
        assert (
            float(shipped["activity_card_min_edit_seconds"])
            == defaults.activity_card_min_edit_seconds
        ), name


def test_the_old_key_names_are_still_read() -> None:
    """An operator who tuned either knob keeps that setting across the rename."""
    import inspect

    from jiuwenswarm.gateway import app_gateway

    source = inspect.getsource(app_gateway)
    assert 'slack_conf.get("subagent_status_card", True)' in source
    assert '_slack_seconds("subagent_card_delay_seconds", 5.0)' in source
    assert '_slack_seconds("subagent_card_min_edit_seconds", 10.0)' in source


@pytest.mark.asyncio
async def test_a_stopped_channel_leaves_no_timer_behind() -> None:
    client = _RecordingSlackClient()
    channel = _channel(client, activity_card_delay_seconds=30.0)

    await channel.send(_dispatch("call-1"))
    timer = channel._activity_records[(REQUEST_ID, "C1")].post_timer
    assert timer is not None

    await channel.stop()
    await _settle()

    assert timer.cancelled()
    assert not channel._activity_records
    assert client.posts == []


# ── The card a thinking-only turn leaves behind, and does not ────────────────
#
# The card earns its place while the turn runs: it is the liveness signal for a
# model deliberating over a thread with nothing in it, which is the case the
# thinking section exists for. Once the turn has ended a card reading "thought
# for 12s" says nothing the answer beside it does not already imply, and the
# duration is the gap between the question and the reply. So it comes down --
# the whole message, by ``chat.delete``, never by editing the section out of a
# card that holds anything else.
#
# The predicate is asked of the card as it stands at turn end, not of the turn's
# history: whichever order the work arrived in, a card showing a tool, a todo,
# a fan-out or a failure beside its thinking keeps every part of it.


def _thinking_only_turn() -> tuple[_RecordingSlackClient, SlackChannel]:
    """A turn that has done nothing but think, with its card on screen."""
    client = _RecordingSlackClient()
    channel = _channel(client)
    return client, channel


@pytest.mark.asyncio
async def test_a_card_holding_only_thinking_comes_down_when_the_turn_ends() -> None:
    """The rule, on the turn it was written for.

    The reply is posted as it always was; what goes is the card above it, which
    by then reports only that the model thought and for how long.
    """
    client, channel = _thinking_only_turn()

    await channel.send(_reasoning())
    await _settle()
    assert _order(client.posts[0]) == ["thinking"]
    card_ts = client.posts[0]
    client.posts.clear()

    await channel.send(_final("The staging cluster is fine."))

    assert client.deleted == [("C1", "1780000000.000001")]
    assert [post.get("text") for post in client.posts] == [
        "The staging cluster is fine."
    ]
    assert channel._activity_records == {}
    assert card_ts is not None


@pytest.mark.asyncio
async def test_the_thinking_only_card_is_deleted_rather_than_settled_first() -> None:
    """One call, not two. The delete replaces the settling rewrite.

    The decision is taken before the card is closed, which is what lets it
    replace a write instead of following one -- the same property the silent
    turn's discard has, and the reason neither costs an extra call.
    """
    client, channel = _thinking_only_turn()

    await channel.send(_reasoning())
    await _settle()
    client.posts.clear()

    await channel.send(_final("Yes."))

    assert client.updates == []
    assert len(client.deleted) == 1


@pytest.mark.asyncio
async def test_thinking_then_a_tool_keeps_the_card() -> None:
    """Real work followed the thinking, and the card holds both."""
    client, channel = _thinking_only_turn()

    await channel.send(_reasoning())
    await channel.send(_tool_call("call-1"))
    await channel.send(_result("call-1", tool_name="read_file", result="ok"))
    await _settle()

    await channel.send(_final("Yes."))

    assert client.deleted == []
    assert _order(client.updates[-1]) == ["thinking", "tools"]


@pytest.mark.asyncio
async def test_a_tool_then_thinking_keeps_the_card() -> None:
    """The other order, which is the one a terminal-event rule would get wrong.

    A turn that calls a tool and then reasons ends on reasoning, so a rule read
    off the last thing that happened would delete a card holding a tool section.
    The predicate reads the card instead, and the card still shows the tool.
    """
    client, channel = _thinking_only_turn()

    await channel.send(_tool_call("call-1"))
    await channel.send(_result("call-1", tool_name="read_file", result="ok"))
    await channel.send(_reasoning())
    await _settle()

    await channel.send(_final("Yes."))

    assert client.deleted == []
    assert _order(client.updates[-1]) == ["thinking", "tools"]


@pytest.mark.asyncio
async def test_thinking_beside_a_todo_list_keeps_the_card() -> None:
    client, channel = _thinking_only_turn()

    await channel.send(_reasoning())
    await channel.send(_todos("completed", "pending"))
    await _settle()

    await channel.send(_final("Yes."))

    assert client.deleted == []
    assert _order(client.updates[-1]) == ["thinking", "todos"]


@pytest.mark.asyncio
async def test_thinking_beside_a_fan_out_keeps_the_card() -> None:
    client, channel = _thinking_only_turn()

    await channel.send(_reasoning())
    await channel.send(_dispatch("call-1"))
    await channel.send(_result("call-1"))
    await _settle()

    await channel.send(_final("Yes."))

    assert client.deleted == []
    assert _order(client.updates[-1]) == ["thinking", "subagents"]


@pytest.mark.asyncio
async def test_a_turn_that_did_nothing_but_think_and_died_keeps_its_card() -> None:
    """The failure case, which has to stay impossible rather than untested.

    ``_close_activity_card``'s return value is what lets ``send`` stop posting
    the error as a message of its own. Delete the card of a turn that died and
    that caller believes a card is showing a failure that is no longer on
    screen, and the reader watches the turn disappear.

    The first of the three guards is why this one cannot happen:
    ``_card_shows_only_thinking`` counts the sections of the rendered card, the
    caller sets ``failed`` on the record before asking, and ``_error_card`` is
    one of the sections -- so the plan holds two and the predicate says no.
    """
    client, channel = _thinking_only_turn()

    await channel.send(_reasoning())
    await _settle()
    client.posts.clear()

    await channel.send(_error())

    assert client.deleted == []
    assert _order(client.updates[-1]) == ["thinking", "error"]
    # The error reached the card, so it is not posted a second time as a
    # message. That suppression is the whole reason the guard matters.
    assert client.posts == []
    assert STREAM_TIMEOUT_ERROR in str(client.updates[-1])


@pytest.mark.asyncio
async def test_a_failed_thinking_only_turn_reports_a_card_that_carried_it() -> None:
    """The return value itself, read where ``send`` reads it.

    A discard returns ``False`` -- nothing was delivered -- so a rule that
    reached a failed turn would not merely leave the error unshown, it would
    also tell the caller the error had been shown.
    """
    client, channel = _thinking_only_turn()

    await channel.send(_reasoning())
    await _settle()

    assert await channel._close_activity_card(_error(), None) is True
    assert client.deleted == []


@pytest.mark.asyncio
async def test_a_failed_record_is_never_a_thinking_only_card() -> None:
    """The predicate on its own, without the caller in front of it."""
    client, channel = _thinking_only_turn()

    await channel.send(_reasoning())
    await _settle()
    record = channel._activity_records[(REQUEST_ID, "C1")]
    assert SlackChannel._card_shows_only_thinking(record) is True

    record.failed = True
    record.harness_error = STREAM_TIMEOUT_ERROR

    assert SlackChannel._card_shows_only_thinking(record) is False


@pytest.mark.asyncio
async def test_a_stopped_thinking_only_turn_keeps_its_card() -> None:
    """A stop settles the record itself, so the terminal never reaches the rule.

    The card is what shows the reader what was in flight when the stop landed,
    which is worth keeping for the same reason a failed turn's card is.
    """
    client, channel = _thinking_only_turn()

    await channel.send(_reasoning())
    await _settle()
    record = channel._activity_records[(REQUEST_ID, "C1")]
    record.stop_requested = True
    await channel._settle_stopped_card(record)
    assert len(client.updates) == 1

    await channel.send(_final("Yes."))

    assert client.deleted == []
    assert len(client.updates) == 1
    assert channel._activity_records[(REQUEST_ID, "C1")].stopped


@pytest.mark.asyncio
async def test_a_thinking_only_turn_inside_the_delay_makes_no_card_call() -> None:
    """Nothing on screen, so nothing to take down and no call worth spending.

    The delay is still the answer to "when to emit nothing", and a card is never
    posted in order to be deleted a moment later.
    """
    client = _RecordingSlackClient()
    channel = _channel(client, activity_card_delay_seconds=30.0)

    await channel.send(_reasoning())
    await _settle()
    assert client.posts == []

    await channel.send(_final("Yes."))

    assert client.updates == []
    assert client.deleted == []
    assert [post.get("blocks") for post in client.posts] == [None]
    assert channel._activity_records == {}


@pytest.mark.asyncio
async def test_a_refused_delete_leaves_the_thinking_only_card_settled() -> None:
    """Best effort, and the fallback is what the card did before this rule.

    A card captioned mid-turn above an answer that did arrive is the one failure
    the record exists to prevent, and is worse than the card the delete wanted
    gone.
    """
    client, channel = _thinking_only_turn()

    async def refuse(**kwargs: Any) -> dict[str, bool]:
        raise RuntimeError("cant_delete_message")

    client.chat_delete = refuse

    await channel.send(_reasoning())
    await _settle()

    await channel.send(_final("Yes."))

    assert client.deleted == []
    assert _order(client.updates[-1]) == ["thinking"]
    assert client.updates[-1]["text"].startswith("Turn finished")
