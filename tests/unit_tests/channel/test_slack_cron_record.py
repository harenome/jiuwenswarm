"""The cron status record: one Slack message per scheduled run.

Every push a run makes holds the same ``payload.cron.run_id``; at most one is
a placeholder and at most one is terminal, and the terminal one is the
placeholder's own event reaching its conclusion. These tests are about the
Slack half of that contract -- the placeholder posted as a ``task_card`` and
the result rewriting it in place -- and about the four ways the rewrite does
not happen, which all end the same way: a new message, exactly as before.
"""

from __future__ import annotations

import time
from typing import Any

import pytest

from jiuwenswarm.common.schema.message import EventType, Message
from jiuwenswarm.gateway.channel_manager.base import RobotMessageRouter
from jiuwenswarm.gateway.channel_manager.im_platforms.slack import slack_connect
from jiuwenswarm.gateway.channel_manager.im_platforms.slack.slack_connect import (
    SlackChannel,
    SlackChannelConfig,
    SlackDeliveryError,
)

RUN_ID = "job-1:1780000000"
JOB_NAME = "Daily digest"
# The prompt. It holds state-file paths and operator instructions and must
# never reach a channel, let alone a card built from a field next to it.
JOB_DESCRIPTION = (
    "Read $JIUWENSWARM_DATA_DIR/agent/workspace/state/skills/digest/watermark "
    "and report everything since it."
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
    """Answers both writes and remembers which one was used."""

    def __init__(self) -> None:
        self.posts: list[dict[str, Any]] = []
        self.updates: list[dict[str, Any]] = []
        self.ts_counter = 0

    async def chat_postMessage(self, **kwargs: Any) -> dict[str, str]:
        self.posts.append(kwargs)
        self.ts_counter += 1
        return {"ts": f"1780000000.{self.ts_counter:06d}"}

    async def chat_update(self, **kwargs: Any) -> dict[str, str]:
        self.updates.append(kwargs)
        return {"ts": kwargs["ts"]}


def _channel(client: Any, **config: Any) -> SlackChannel:
    channel = SlackChannel(
        SlackChannelConfig(enabled=True, default_channel_id="C1", **config),
        RobotMessageRouter(),
    )
    channel._client = client
    return channel


def _push(
    *,
    text: str,
    is_placeholder: bool,
    status: str,
    run_id: str = RUN_ID,
    job_name: str = JOB_NAME,
) -> Message:
    """One cron push, shaped as ``_push_to_targets`` builds it."""
    return Message(
        id=f"cron-push-{run_id}-slack",
        type="event",
        channel_id="slack",
        session_id="",
        params={},
        timestamp=time.time(),
        ok=True,
        payload={
            "content": text,
            "cron": {
                "job_id": "job-1",
                "job_name": job_name,
                "run_id": run_id,
                "push_at": "2026-08-18T09:00:00+08:00",
                "wake_at": "2026-08-18T08:55:00+08:00",
                "exec_channel_id": "__cron__",
                "exec_session_id": "cron_1780000000_job-1",
                "is_placeholder": is_placeholder,
                "status": status,
            },
        },
        event_type=EventType.CHAT_FINAL,
        metadata={},
    )


def _placeholder() -> Message:
    return _push(
        text=(
            f"{JOB_NAME} is running. Results will be posted when ready "
            "(scheduled_at=2026-08-18T09:00:00+08:00)."
        ),
        is_placeholder=True,
        status="running",
    )


def _card(call: dict[str, Any]) -> dict[str, Any] | None:
    blocks = call.get("blocks") or []
    for block in blocks:
        if block.get("type") == "task_card":
            return block
    return None


def _details_text(card: dict[str, Any]) -> str:
    details = card.get("details") or {}
    out: list[str] = []
    for element in details.get("elements", []):
        for leaf in element.get("elements", []):
            out.append(str(leaf.get("text", "")))
    return "".join(out)


# ── Path B: still running at push time, then succeeds ────────────────────────


@pytest.mark.asyncio
async def test_placeholder_is_a_card_that_the_result_rewrites_in_place() -> None:
    client = _RecordingSlackClient()
    channel = _channel(client)

    await channel.send(_placeholder())
    assert len(client.posts) == 1
    placeholder_ts = "1780000000.000001"
    posted = _card(client.posts[0])
    assert posted is not None
    assert posted["status"] == "in_progress"
    assert posted["title"] == JOB_NAME
    assert posted["task_id"] == f"cron-{RUN_ID}"
    # The prose still rides along as the notification string, which is the one
    # thing it is good at, and is not repeated as the body.
    assert "is running" in client.posts[0]["text"]
    assert len(client.posts[0]["blocks"]) == 1

    await channel.send(
        _push(text="All quiet.", is_placeholder=False, status="succeeded")
    )

    # One message, rewritten -- not a second one posted underneath.
    assert len(client.posts) == 1
    assert len(client.updates) == 1
    assert client.updates[0]["ts"] == placeholder_ts
    rewritten = _card(client.updates[0])
    assert rewritten is not None
    assert rewritten["status"] == "complete"
    assert rewritten["task_id"] == f"cron-{RUN_ID}"
    # The report is under the card rather than instead of it.
    assert client.updates[0]["blocks"][1]["text"]["text"] == "All quiet."


@pytest.mark.asyncio
async def test_the_card_is_built_from_the_job_name_never_the_prompt() -> None:
    client = _RecordingSlackClient()
    channel = _channel(client)

    message = _placeholder()
    # A payload that also holds the prompt must not tempt the renderer.
    message.payload["cron"]["description"] = JOB_DESCRIPTION

    await channel.send(message)

    rendered = repr(client.posts[0])
    assert JOB_NAME in rendered
    assert "JIUWENSWARM_DATA_DIR" not in rendered


# ── Path C: still running at push time, then fails ───────────────────────────


@pytest.mark.asyncio
async def test_a_failure_rewrites_the_record_to_error_with_the_reason() -> None:
    client = _RecordingSlackClient()
    channel = _channel(client)

    await channel.send(_placeholder())
    await channel.send(
        _push(
            text="[cron] 任务执行失败: OSError",
            is_placeholder=False,
            status="failed",
        )
    )

    assert len(client.updates) == 1
    card = _card(client.updates[0])
    assert card is not None
    assert card["status"] == "error"
    assert "OSError" in _details_text(card)
    # The reason is the card's own words; it is not also repeated underneath.
    assert len(client.updates[0]["blocks"]) == 1


# ── Path A: the run finished before push time, so there is no placeholder ────


@pytest.mark.asyncio
async def test_a_run_that_never_placeheld_posts_a_terminal_record_directly() -> None:
    client = _RecordingSlackClient()
    channel = _channel(client)

    await channel.send(
        _push(text="All quiet.", is_placeholder=False, status="succeeded")
    )

    assert client.updates == []
    assert len(client.posts) == 1
    card = _card(client.posts[0])
    assert card is not None
    assert card["status"] == "complete"


@pytest.mark.asyncio
async def test_a_failure_without_a_placeholder_is_still_structured() -> None:
    """The operator's decision: the error case is where structure earns its place."""
    client = _RecordingSlackClient()
    channel = _channel(client)

    await channel.send(
        _push(
            text="[cron] 任务执行失败: OSError",
            is_placeholder=False,
            status="failed",
        )
    )

    card = _card(client.posts[0])
    assert card is not None
    assert card["status"] == "error"


# ── Path E: the run finished and the delivery did not ────────────────────────


@pytest.mark.asyncio
async def test_an_abandoned_delivery_says_so_in_the_details() -> None:
    """``error`` is honest, but only the details say the work may be gone."""
    client = _RecordingSlackClient()
    channel = _channel(client)
    notice = (
        "[cron] 任务执行超时（网关等待 10min 后放弃）：已向 AgentServer 发送取消；"
        "任务可能已在后端执行完成，其结果无人接收而被丢弃。"
    )

    await channel.send(_placeholder())
    await channel.send(_push(text=notice, is_placeholder=False, status="failed"))

    card = _card(client.updates[0])
    assert card is not None
    assert card["status"] == "error"
    # Verbatim from the scheduler. Nothing here parses English -- or Chinese --
    # out of a result body to decide what happened.
    assert "被丢弃" in _details_text(card)


# ── The four ways the rewrite does not happen ────────────────────────────────


@pytest.mark.asyncio
async def test_a_refused_rewrite_posts_the_result_as_a_new_message() -> None:
    class _RefusingUpdates(_RecordingSlackClient):
        async def chat_update(self, **kwargs: Any) -> dict[str, str]:
            self.updates.append(kwargs)
            raise RuntimeError("message_not_found")

    client = _RefusingUpdates()
    channel = _channel(client)

    await channel.send(_placeholder())
    await channel.send(
        _push(text="All quiet.", is_placeholder=False, status="succeeded")
    )

    assert len(client.updates) == 1
    assert len(client.posts) == 2
    card = _card(client.posts[1])
    assert card is not None
    assert card["status"] == "complete"


@pytest.mark.asyncio
async def test_a_record_from_another_channel_is_not_rewritten() -> None:
    """The key is the pair: a run's card belongs to the channel it landed in."""
    client = _RecordingSlackClient()
    channel = _channel(client)

    await channel.send(_placeholder())
    channel.config.default_channel_id = "C2"
    await channel.send(
        _push(text="All quiet.", is_placeholder=False, status="succeeded")
    )

    assert client.updates == []
    assert [call["channel"] for call in client.posts] == ["C1", "C2"]


@pytest.mark.asyncio
async def test_a_forgotten_record_posts_a_new_message() -> None:
    client = _RecordingSlackClient()
    channel = _channel(client)

    await channel.send(_placeholder())
    # What a restart, or an eviction, leaves behind.
    channel._cron_records.clear()
    await channel.send(
        _push(text="All quiet.", is_placeholder=False, status="succeeded")
    )

    assert client.updates == []
    assert len(client.posts) == 2


@pytest.mark.asyncio
async def test_an_unrecognised_status_supersedes_without_claiming_an_outcome() -> None:
    client = _RecordingSlackClient()
    channel = _channel(client)

    await channel.send(_placeholder())
    await channel.send(
        _push(text="Something happened.", is_placeholder=False, status="perplexed")
    )

    # The placeholder must not be left asserting that a finished run is live...
    assert len(client.updates) == 1
    # ...but no chip is invented for an outcome nobody established.
    assert _card(client.updates[0]) is None
    assert client.updates[0]["text"] == "Something happened."


# ── The record as the run's anchor ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_threaded_report_hangs_off_the_placeholder() -> None:
    client = _RecordingSlackClient()
    channel = _channel(client)
    marker = slack_connect._SLACK_THREAD_DETAILS_MARKER

    await channel.send(_placeholder())
    await channel.send(
        _push(
            text=f"Summary line.\n{marker}\nThe long detail.",
            is_placeholder=False,
            status="succeeded",
        )
    )

    placeholder_ts = "1780000000.000001"
    # The root is the rewritten placeholder...
    assert client.updates[0]["ts"] == placeholder_ts
    # ...and the detail hangs under it rather than under a fresh root.
    assert len(client.posts) == 2
    assert client.posts[1]["thread_ts"] == placeholder_ts
    assert client.posts[1]["text"] == "The long detail."


@pytest.mark.asyncio
async def test_a_result_too_long_for_one_message_threads_under_its_card() -> None:
    client = _RecordingSlackClient()
    channel = _channel(client)

    await channel.send(_placeholder())
    # An edit is held to 4,000 characters, so this is two chunks.
    await channel.send(
        _push(
            text="\n\n".join(["paragraph " * 200] * 4),
            is_placeholder=False,
            status="succeeded",
        )
    )

    placeholder_ts = "1780000000.000001"
    assert len(client.updates) == 1
    assert len(client.posts) == 2
    assert client.posts[1]["thread_ts"] == placeholder_ts


@pytest.mark.asyncio
async def test_a_body_that_fills_the_message_keeps_the_body_and_drops_the_card(
    monkeypatch,
) -> None:
    """Blocks replace a message's text, so the card is what gives way.

    A rendering that already uses every block Slack allows leaves no room for
    the card. Adding it anyway would be rejected whole; dropping the body to
    make room would lose the result the reader was promised.
    """
    monkeypatch.setattr(
        slack_connect.slack_blocks,
        "render_blocks_with_kind",
        # Keyword-only arguments swallowed: the double stands in for a renderer
        # whose signature grows, and this test is about how many blocks come
        # back, not about how the connector asked for them.
        lambda text, **kwargs: (
            [
                {"type": "section", "text": {"type": "mrkdwn", "text": "row"}}
                for _ in range(slack_connect.slack_blocks.MAX_BLOCKS_PER_MESSAGE)
            ],
            slack_connect.slack_blocks.BLOCK_KIND_DATA,
        ),
    )
    client = _RecordingSlackClient()
    channel = _channel(client)

    await channel.send(
        _push(text="a table", is_placeholder=False, status="succeeded")
    )

    assert client.posts
    first = client.posts[0]
    assert _card(first) is None
    assert len(first["blocks"]) == slack_connect.slack_blocks.MAX_BLOCKS_PER_MESSAGE
    assert first["text"] == "a table"


@pytest.mark.asyncio
async def test_a_long_result_is_rendered_whole_beneath_the_card() -> None:
    """The card fronts the report; it never replaces part of it."""
    client = _RecordingSlackClient()
    channel = _channel(client)
    body = "\n\n".join(f"paragraph {index} " + "word " * 400 for index in range(3))

    await channel.send(_push(text=body, is_placeholder=False, status="succeeded"))

    blocks = client.posts[0]["blocks"]
    assert blocks[0]["type"] == "task_card"
    rendered = "".join(block["text"]["text"] for block in blocks[1:])
    assert "paragraph 0" in rendered
    assert "paragraph 2" in rendered
    assert all(
        len(block["text"]["text"]) <= slack_connect._MAX_SECTION_TEXT_LENGTH
        for block in blocks[1:]
    )


# ── Everything that is not a cron push ───────────────────────────────────────


@pytest.mark.asyncio
async def test_an_ordinary_reply_is_untouched() -> None:
    client = _RecordingSlackClient()
    channel = _channel(client)

    await channel.send(
        Message(
            id="reply-1",
            type="event",
            channel_id="slack",
            session_id="",
            params={},
            timestamp=time.time(),
            ok=True,
            payload={"content": "hello"},
            event_type=EventType.CHAT_FINAL,
            metadata={},
        )
    )

    assert client.updates == []
    assert len(client.posts) == 1
    assert client.posts[0].get("blocks") is None
    assert channel._cron_records == {}


@pytest.mark.asyncio
async def test_a_push_without_a_run_id_is_delivered_as_text() -> None:
    client = _RecordingSlackClient()
    channel = _channel(client)
    message = _placeholder()
    message.payload["cron"]["run_id"] = ""

    await channel.send(message)

    assert _card(client.posts[0]) is None
    assert channel._cron_records == {}


# ── The map is bounded, aged and dropped ─────────────────────────────────────


@pytest.mark.asyncio
async def test_the_record_map_is_bounded() -> None:
    client = _RecordingSlackClient()
    channel = _channel(client)

    for index in range(slack_connect._MAX_CRON_RECORDS + 5):
        await channel.send(
            _push(
                text="running",
                is_placeholder=True,
                status="running",
                run_id=f"job-1:{index}",
            )
        )

    assert len(channel._cron_records) <= slack_connect._MAX_CRON_RECORDS


@pytest.mark.asyncio
async def test_an_aged_record_is_pruned() -> None:
    client = _RecordingSlackClient()
    channel = _channel(client)

    await channel.send(_placeholder())
    for record in channel._cron_records.values():
        record.touched_at -= slack_connect._CRON_RECORD_TIMEOUT_SECONDS + 1
    await channel.send(
        _push(
            text="running",
            is_placeholder=True,
            status="running",
            run_id="job-1:other",
        )
    )

    assert (RUN_ID, "C1") not in channel._cron_records


@pytest.mark.asyncio
async def test_stop_forgets_every_record() -> None:
    client = _RecordingSlackClient()
    channel = _channel(client)

    await channel.send(_placeholder())
    assert channel._cron_records
    await channel.stop()

    assert channel._cron_records == {}


# ── A failed delivery is still a failed delivery ─────────────────────────────


@pytest.mark.asyncio
async def test_a_placeholder_that_cannot_be_posted_raises() -> None:
    class _FailingClient(_RecordingSlackClient):
        async def chat_postMessage(self, **kwargs: Any) -> dict[str, str]:
            raise RuntimeError("channel_not_found")

    channel = _channel(_FailingClient())

    with pytest.raises(SlackDeliveryError):
        await channel.send(_placeholder())
    assert channel._cron_records == {}
