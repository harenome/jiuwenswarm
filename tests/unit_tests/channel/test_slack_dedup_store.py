"""The Slack dedup window has to survive a restart.

Slack keeps retrying an event it believes went unacked, and Socket Mode replays
across reconnects. The connector always dropped duplicates, but with a
process-local LRU -- so a restart forgot everything exactly when those retries
arrive, and the bot answered a message it had already answered.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from jiuwenswarm.gateway.channel_manager.base import RobotMessageRouter
from jiuwenswarm.gateway.channel_manager.im_platforms.slack.slack_connect import (
    SlackChannel,
    SlackChannelConfig,
)
from jiuwenswarm.gateway.channel_manager.im_platforms.slack.slack_dedup import (
    SlackEventDedupStore,
    get_slack_dedup_path,
)


@pytest.mark.asyncio
async def test_remember_reports_first_sighting_then_duplicates(tmp_path):
    store = SlackEventDedupStore(tmp_path / "seen.json")

    assert await store.remember("T1:C1:1710000000.000100") is True
    assert await store.remember("T1:C1:1710000000.000100") is False
    assert await store.remember("T1:C1:1710000000.000200") is True


@pytest.mark.asyncio
async def test_window_survives_a_restart(tmp_path):
    """The whole point: a fresh store on the same path still knows the key."""
    path = tmp_path / "seen.json"
    assert await SlackEventDedupStore(path).remember("T1:C1:ts-1") is True

    # A new instance is what a restart produces.
    restarted = SlackEventDedupStore(path)
    assert await restarted.remember("T1:C1:ts-1") is False
    assert await restarted.remember("T1:C1:ts-2") is True


@pytest.mark.asyncio
async def test_oldest_entries_are_evicted_at_the_cap(tmp_path):
    path = tmp_path / "seen.json"
    store = SlackEventDedupStore(path, max_entries=3)

    for i in range(3):
        assert await store.remember(f"key-{i}") is True
    assert await store.remember("key-3") is True  # evicts key-0

    stored = json.loads(path.read_text(encoding="utf-8"))["events"]
    assert stored == ["key-1", "key-2", "key-3"]
    # key-0 fell out of the window, so it reads as new again.
    assert await store.remember("key-0") is True
    # ...and the cap still holds after that.
    assert len(json.loads(path.read_text(encoding="utf-8"))["events"]) == 3


@pytest.mark.asyncio
async def test_a_corrupt_file_starts_a_fresh_window_instead_of_failing(tmp_path):
    """A truncated or hand-edited file must cost one duplicate, not every message."""
    path = tmp_path / "seen.json"
    path.write_text("{not json at all", encoding="utf-8")

    store = SlackEventDedupStore(path)
    assert await store.remember("T1:C1:ts-1") is True
    assert await store.remember("T1:C1:ts-1") is False
    assert json.loads(path.read_text(encoding="utf-8"))["events"] == ["T1:C1:ts-1"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "content", ["", "   ", "[]", '{"events": "not-a-list"}', '{"version": 1}']
)
async def test_unexpected_shapes_are_tolerated(tmp_path, content):
    path = tmp_path / "seen.json"
    path.write_text(content, encoding="utf-8")
    assert await SlackEventDedupStore(path).remember("k") is True


@pytest.mark.asyncio
async def test_persistence_failure_still_admits_the_message(tmp_path, monkeypatch):
    """Losing durability is bad; dropping a user's message is worse."""
    store = SlackEventDedupStore(tmp_path / "seen.json")

    def _explode(*args, **kwargs):
        raise OSError("read-only file system")

    monkeypatch.setattr(store, "_remember_under_file_lock", _explode)

    assert await store.remember("k1") is True
    # Still de-duplicates within the process, just not across a restart.
    assert await store.remember("k1") is False
    assert await store.remember("k2") is True


@pytest.mark.asyncio
async def test_concurrent_callers_admit_a_key_exactly_once(tmp_path):
    store = SlackEventDedupStore(tmp_path / "seen.json")

    results = await asyncio.gather(*(store.remember("same-key") for _ in range(12)))

    assert results.count(True) == 1, "a duplicate slipped through the lock"
    assert results.count(False) == 11


@pytest.mark.asyncio
async def test_two_stores_on_one_file_see_each_other(tmp_path):
    """Cross-process behaviour, approximated by two independent instances."""
    path = tmp_path / "seen.json"
    first = SlackEventDedupStore(path)
    second = SlackEventDedupStore(path)

    assert await first.remember("shared") is True
    assert await second.remember("shared") is False


@pytest.mark.asyncio
async def test_empty_key_is_never_recorded(tmp_path):
    path = tmp_path / "seen.json"
    store = SlackEventDedupStore(path)
    assert await store.remember("") is True
    assert not path.exists()


def test_default_path_lives_beside_the_other_gateway_state():
    assert get_slack_dedup_path().parent.name == "gateway"
    assert get_slack_dedup_path().name == "slack_seen_events.json"


@pytest.mark.asyncio
async def test_connector_does_not_reprocess_a_retry_after_restart(tmp_path):
    """End to end: same event, same workspace, brand-new SlackChannel."""
    path = tmp_path / "seen.json"
    event = {
        "type": "app_mention",
        "user": "U1",
        "channel": "C1",
        "channel_type": "channel",
        "text": "<@U-BOT> summarize this",
        "ts": "1710000000.000100",
    }
    body = {"event_id": "Ev1", "team_id": "T1"}

    def _make() -> tuple[SlackChannel, list]:
        channel = SlackChannel(
            SlackChannelConfig(enabled=True),
            RobotMessageRouter(),
            dedup_store=SlackEventDedupStore(path),
        )
        channel._running = True
        channel._bot_user_id = "U-BOT"
        received: list = []
        channel.on_message(received.append)
        return channel, received

    before, received_before = _make()
    await before._handle_app_mention(event, body)
    assert len(received_before) == 1

    # Restart: new channel, new store object, same file. Slack redelivers.
    after, received_after = _make()
    await after._handle_app_mention(event, body)
    assert received_after == [], "a restart re-answered an already-handled message"
