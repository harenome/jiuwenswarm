# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.

"""One missing ``files:read``, one explanation, whichever half of the path ran.

Resolving an inbound attachment has two halves and they both need that one
grant. The ordinary one fetches ``url_private`` with the bot token, and it named
the scope when Slack refused. The other runs only when a ``file_share`` event
arrives without the URL embedded: ``files.info`` is called to look it up, and
that call's ``missing_scope`` was not caught at all. It reached the caller's
generic handler, which reported "the download from Slack failed" to the user and
an unexpected exception with a traceback to the log.

So the same absent grant produced two different accounts of itself, and which
one an operator got depended on whether the event happened to embed the URL --
a distinction they cannot see, act on, or learn about.

These tests pin that both halves now name the scope, and that a refusal which is
*not* about a grant is not given an explanation this code cannot stand behind.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from jiuwenswarm.gateway.channel_manager.base import RobotMessageRouter
from jiuwenswarm.gateway.channel_manager.im_platforms.slack import slack_connect
from jiuwenswarm.gateway.channel_manager.im_platforms.slack.slack_connect import (
    SlackAttachmentError,
    SlackChannel,
    SlackChannelConfig,
)

_FILE_ID = "F0ATTACH01"

# A partial file_share record: Slack named the file and left the URL out, which
# is the only shape that reaches files.info.
_PARTIAL = {"id": _FILE_ID, "name": "notes.txt"}


class _SlackApiError(Exception):
    def __init__(self, error: str) -> None:
        super().__init__(f"The request to the Slack API failed. (error: {error})")
        self.response = SimpleNamespace(data={"ok": False, "error": error})


class _RefusingClient:
    def __init__(self, error: str) -> None:
        self.error = error

    async def files_info(self, **kwargs: Any) -> Any:
        raise _SlackApiError(self.error)


def _channel(client: Any) -> SlackChannel:
    channel = SlackChannel(
        SlackChannelConfig(enabled=True, bot_token="xoxb-test"), RobotMessageRouter()
    )
    channel._running = True
    channel._client = client
    return channel


@pytest.mark.asyncio
async def test_a_refused_lookup_names_the_scope_the_download_path_names() -> None:
    """The two halves give one account of one absent grant."""
    channel = _channel(_RefusingClient("missing_scope"))

    with pytest.raises(SlackAttachmentError) as excinfo:
        await channel._resolve_attachment(_PARTIAL)

    assert excinfo.value.reason == slack_connect._FILE_REASON_UNAUTHORIZED
    assert "files:read" in excinfo.value.reason
    # And the log detail says which call was refused and what Slack answered,
    # rather than arriving as an unexpected exception with a traceback.
    assert "files.info" in str(excinfo.value)
    assert "missing_scope" in str(excinfo.value)


@pytest.mark.asyncio
async def test_the_two_halves_give_the_user_the_same_sentence(monkeypatch) -> None:
    """The property this file exists for, asserted against both paths at once.

    The lookup half is driven through ``files.info``; the download half through
    a 403 on ``url_private``, which is what an unscoped bearer GET earns. Neither
    reason is written out here -- they are compared to each other, so the test
    fails if the two ever come apart again however they are worded.
    """
    channel = _channel(_RefusingClient("missing_scope"))

    with pytest.raises(SlackAttachmentError) as lookup:
        await channel._resolve_attachment(_PARTIAL)

    def refuse(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, content=b"")

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        slack_connect.httpx,
        "AsyncClient",
        lambda **kwargs: real_client(
            **{**kwargs, "transport": httpx.MockTransport(refuse)}
        ),
    )

    with pytest.raises(SlackAttachmentError) as download:
        await channel._fetch_attachment_bytes(
            "https://files.slack.com/files-pri/T1-F1/download/notes.txt",
            "text/plain",
        )

    assert lookup.value.reason == download.value.reason


@pytest.mark.asyncio
async def test_a_refusal_that_is_not_about_a_grant_keeps_the_generic_reason() -> None:
    """No explanation is invented for a code that names nothing to grant.

    ``file_not_found`` is the file being gone or invisible to this app, which is
    not something an operator fixes by adding a scope. It keeps the reason it
    already had, and the code still reaches the log.
    """
    channel = _channel(_RefusingClient("file_not_found"))

    with pytest.raises(SlackAttachmentError) as excinfo:
        await channel._resolve_attachment(_PARTIAL)

    assert excinfo.value.reason == slack_connect._FILE_REASON_DOWNLOAD_FAILED
    assert "file_not_found" in str(excinfo.value)


@pytest.mark.asyncio
async def test_a_transport_failure_keeps_the_generic_reason() -> None:
    """A lookup that never reached Slack is not reported as a missing grant."""

    class _Dropped:
        async def files_info(self, **kwargs: Any) -> Any:
            raise TimeoutError("read timed out")

    channel = _channel(_Dropped())

    with pytest.raises(SlackAttachmentError) as excinfo:
        await channel._resolve_attachment(_PARTIAL)

    assert excinfo.value.reason == slack_connect._FILE_REASON_DOWNLOAD_FAILED


@pytest.mark.asyncio
async def test_an_embedded_url_never_reaches_the_lookup_at_all() -> None:
    """The ordinary shape is unchanged: files.info is a fallback, not a step."""

    class _NeverCalled:
        async def files_info(self, **kwargs: Any) -> Any:
            raise AssertionError("files.info was called with a URL already in hand")

    channel = _channel(_NeverCalled())
    url = "https://files.slack.com/files-pri/T1-F1/download/notes.txt"

    resolved, record = await channel._resolve_attachment(
        {"id": _FILE_ID, "name": "notes.txt", "url_private_download": url}
    )

    assert resolved == url
    assert record["id"] == _FILE_ID
