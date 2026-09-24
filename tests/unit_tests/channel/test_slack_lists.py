# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Reading, writing, sharing and deleting Slack Lists.

Five things are pinned here that nothing else can pin.

* **``delete_slack_list`` reads before it deletes.** Slack has no method that
  deletes a List, so the tool goes through ``files.delete``, which destroys any
  file it is handed. Without the ``filetype`` check the tool would be a
  delete-any-file capability wearing a List's name, and the id that proves it
  is a canvas id -- the same ``F`` family, accepted by every check but this one.
* **A text cell is written as rich text or not at all.** Slack does not accept
  a plain string, so ``text`` is Markdown that is rendered, and what it reaches
  Slack as is an array holding a ``rich_text`` block rather than a bare
  elements array.
* **A multi-target grant can half succeed.** Slack refuses the whole call with
  one code and says nothing about which targets took, so the outcome is
  established per target. A single boolean here would be a claim nobody
  checked.
* **A multiple delete cannot.** Slack documents no per-record result for it, so
  the tool says the outcome is not established rather than implying that every
  named row went.
* **There is no server-side query, and no column ever changes.** Both are
  properties of Slack's API rather than of this tool, and both have to reach a
  model through the card: one would otherwise be looked for as a missing
  argument, the other after a List has already been created with the wrong
  columns.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Self

import pytest

import jiuwenswarm.common.config as config_module
from jiuwenswarm.agents.harness.common.tools.slack_lists import (
    ACCESS_LEVELS,
    CELL_VALUE_SHAPES,
    COLUMN_TYPES,
    EDIT_OPERATIONS,
    EXPORT_FORMATS,
    LIST_TOOL_NAMES,
    MAX_EXPORT_BYTES,
    MAX_EXPORT_CHARS,
    MAX_ITEMS_PER_PAGE,
    SlackListToolkit,
    UNDOCUMENTED_CELL_TYPES,
    slack_list_request_metadata,
)
from jiuwenswarm.common.slack_history_policy import (
    METADATA_ORIGIN_KEY,
    METADATA_TEAM_KEY,
    ORIGIN_CRON_JOB,
)
from jiuwenswarm.common.slack_scope_policy import METHOD_REQUIREMENTS, dotted


_LIST = "F1234ABCD"
_OTHER_LIST = "F9999ZZZZ"
_ITEM = "Rec014K005UQJ"
_COLUMN = "Col014K005UQJ"
_CHAT = "C000000AAAA"
_EXPORT_URL = "https://files.slack.com/files-pri/T0-F0/download/list.csv"
_EXPORT_CSV = b"Task,Due\nProbe task 1,2026-09-25\n"
_ALICE = "U000000AAAA"
_BOB = "U000000BBBB"
_CAROL = "U000000CCCC"


class _FakeResponse:
    """What the SDK hangs off a refusal: the body, and nothing else."""

    def __init__(self, error: str, detail: str = "") -> None:
        self.status_code = 200
        self.data: dict[str, Any] = {"ok": False, "error": error}
        if detail:
            self.data["detail"] = detail
        self.headers: dict[str, Any] = {}


class _Body:
    """An httpx-shaped response that yields one export."""

    def __init__(self, payload: bytes, status: int = 200) -> None:
        self.status_code = status
        self.headers: dict[str, str] = {}
        self._payload = payload

    async def aiter_bytes(self) -> Any:
        yield self._payload


class _Stream:
    def __init__(self, body: _Body) -> None:
        self._body = body

    async def __aenter__(self) -> _Body:
        return self._body

    async def __aexit__(self, *_: object) -> bool:
        return False


class _Http:
    """An httpx client serving one file and recording what was asked for."""

    def __init__(self, body: "_Body | None" = None) -> None:
        self._body = _Body(_EXPORT_CSV) if body is None else body
        self.requests: list[tuple[str, dict[str, str]]] = []

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_: object) -> bool:
        return False

    def stream(self, _method: str, url: str, headers: Any = None) -> _Stream:
        self.requests.append((url, dict(headers or {})))
        return _Stream(self._body)


class _FakeSlackError(Exception):
    def __init__(self, error: str, detail: str = "") -> None:
        super().__init__("sanitized fake failure")
        self.response = _FakeResponse(error, detail)


class _Workspace:
    """A fake Slack that records every call and can refuse chosen ones."""

    def __init__(
        self,
        *,
        fail: Any = None,
        file_record: Any = None,
        items: Any = None,
        next_cursor: str = "",
        record: Any = None,
        download: Any = None,
    ) -> None:
        #: method -> (error, detail) or error, or a callable taking
        #: (method, payload).
        self.fail = fail
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.file_record = (
            {"filetype": "list", "title": "Runbook"}
            if file_record is None
            else file_record
        )
        self.items = [{"id": _ITEM}] if items is None else items
        self.next_cursor = next_cursor
        self.record = {"id": _ITEM} if record is None else record
        #: What each successive slackLists.download.get answers with.
        self.download = (
            [{"status": "complete", "download_url": _EXPORT_URL}]
            if download is None
            else list(download)
        )

    def _refusal(self, method: str, payload: dict[str, Any]) -> Any:
        if callable(self.fail):
            return self.fail(method, payload)
        if isinstance(self.fail, dict):
            return self.fail.get(method)
        return self.fail

    def _maybe_fail(self, method: str, payload: dict[str, Any]) -> None:
        refusal = self._refusal(method, payload)
        if not refusal:
            return
        if isinstance(refusal, tuple):
            raise _FakeSlackError(*refusal)
        raise _FakeSlackError(str(refusal))

    async def api_call(self, method: str, json: Any = None) -> dict[str, Any]:
        payload = dict(json or {})
        self.calls.append((method, payload))
        self._maybe_fail(method, payload)
        if method == "slackLists.items.list":
            body: dict[str, Any] = {"ok": True, "items": list(self.items)}
            if self.next_cursor:
                body["response_metadata"] = {"next_cursor": self.next_cursor}
            if payload.get("include_list"):
                body["list"] = {"id": _LIST, "title": "Runbook"}
            return body
        if method == "slackLists.items.info":
            body = {"ok": True, "record": dict(self.record)}
            body["list"] = {"id": _LIST, "schema": [{"id": _COLUMN}]}
            if payload.get("include_is_subscribed"):
                body["is_subscribed"] = True
            return body
        if method == "slackLists.create":
            return {"ok": True, "list": {"id": _OTHER_LIST}}
        if method == "slackLists.items.create":
            return {"ok": True, "record": {"id": _ITEM}}
        if method == "slackLists.download.start":
            return {"ok": True, "job_id": "LeF1234567"}
        if method == "slackLists.download.get":
            if self.download:
                return {"ok": True, **self.download.pop(0)}
            return {"ok": True, "status": "processing"}
        return {"ok": True}

    async def files_info(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("files_info", dict(kwargs)))
        self._maybe_fail("files_info", dict(kwargs))
        if self.file_record is None:
            # Slack answered, and told us nothing about the file. Set
            # ``file_record`` to None after construction to reach this.
            return {"ok": True}
        return {"ok": True, "file": dict(self.file_record)}

    async def files_delete(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("files_delete", dict(kwargs)))
        self._maybe_fail("files_delete", dict(kwargs))
        return {"ok": True}

    def payload(self, method: str) -> dict[str, Any]:
        for name, payload in self.calls:
            if name == method:
                return payload
        raise AssertionError(f"{method} was never called: {self.calls}")

    def payloads(self, method: str) -> list[dict[str, Any]]:
        return [payload for name, payload in self.calls if name == method]

    def methods(self) -> list[str]:
        return [name for name, _ in self.calls]


@pytest.fixture(autouse=True)
def _config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        config_module,
        "get_config",
        lambda: {"channels": {"slack": {"bot_token": "xoxb-config-secret"}}},
    )


def _toolkit(
    workspace: _Workspace, http: Any = None, **metadata: Any
) -> SlackListToolkit:
    base: dict[str, Any] = {METADATA_TEAM_KEY: "T000", "slack_channel_id": _CHAT}
    base.update(metadata)
    return SlackListToolkit(
        metadata=base,
        client=workspace,
        sleep=_no_sleep,
        # Never None: an export that reached a real client would dial Slack.
        http_client_factory=lambda: http if http is not None else _Http(),
    )


async def _no_sleep(_seconds: float) -> None:
    """Take the wait out of a retry without taking the retry out."""


def _run(coro: Any) -> dict[str, Any]:
    return json.loads(asyncio.run(coro))


def _cards(toolkit: SlackListToolkit) -> dict[str, Any]:
    cards: dict[str, Any] = {}
    for tool in toolkit.get_tools():
        card = getattr(tool, "card", None) or tool._card
        cards[card.name] = card
    return cards


def _all_cards() -> dict[str, Any]:
    return _cards(_toolkit(_Workspace()))


# ── 1. the shape of the toolkit ──────────────────────────────────────────────


def test_the_five_tools_are_mounted_under_the_names_the_module_declares() -> None:
    assert tuple(_all_cards()) == LIST_TOOL_NAMES
    assert len(LIST_TOOL_NAMES) == 5


def test_every_slack_method_this_module_calls_is_classified() -> None:
    """The manifest test scans for this too; here it is named per method.

    That test fails with a list of unclassified names and nothing about which
    module added them. This one fails with the method beside the tool family it
    belongs to, which is the thing a reader needs.
    """
    for method in (
        "slackLists_create",
        "slackLists_update",
        "slackLists_access_set",
        "slackLists_access_delete",
        "slackLists_items_create",
        "slackLists_items_info",
        "slackLists_items_list",
        "slackLists_items_update",
        "slackLists_items_delete",
        "slackLists_items_deleteMultiple",
        "slackLists_download_start",
        "slackLists_download_get",
        "files_delete",
    ):
        assert method in METHOD_REQUIREMENTS, method


def test_a_two_dot_method_keeps_the_spelling_an_operator_can_look_up() -> None:
    """The first-underscore rule would make these unfindable in the docs."""
    assert dotted("slackLists_items_list") == "slackLists.items.list"
    assert dotted("slackLists_access_set") == "slackLists.access.set"
    assert (
        dotted("slackLists_items_deleteMultiple")
        == "slackLists.items.deleteMultiple"
    )


# ── 2. which requests may touch a List at all ────────────────────────────────


def test_an_inbound_slack_turn_may_touch_a_list() -> None:
    assert slack_list_request_metadata("slack", {METADATA_TEAM_KEY: "T1"}) == {
        METADATA_TEAM_KEY: "T1"
    }


def test_a_scheduled_run_the_scheduler_marked_may_touch_a_list() -> None:
    metadata = {METADATA_ORIGIN_KEY: ORIGIN_CRON_JOB, METADATA_TEAM_KEY: "T1"}

    assert slack_list_request_metadata("__cron__", metadata) == metadata


def test_a_cron_request_without_the_marker_may_not() -> None:
    """A stray Slack field left on some other request cannot reach these."""
    assert slack_list_request_metadata("__cron__", {METADATA_TEAM_KEY: "T1"}) == {}


def test_a_web_request_may_not() -> None:
    assert slack_list_request_metadata("web", {METADATA_TEAM_KEY: "T1"}) == {}


def test_a_tool_called_on_an_unsettled_request_refuses_rather_than_calling() -> None:
    workspace = _Workspace()
    toolkit = SlackListToolkit(metadata=None, client=workspace)

    result = _run(toolkit.read_slack_list(list_id=_LIST))

    assert result["error"] == "trusted_slack_request_required"
    assert workspace.calls == []


# ── 3. reading ───────────────────────────────────────────────────────────────


def test_a_page_of_rows_comes_back_with_the_cursor_that_continues_it() -> None:
    workspace = _Workspace(next_cursor="dXNlcjpVMDYx")

    result = _run(_toolkit(workspace).read_slack_list(list_id=_LIST))

    assert result["ok"] is True
    assert result["items"] == [{"id": _ITEM}]
    assert result["next_cursor"] == "dXNlcjpVMDYx"
    assert any(word.startswith("more_items:") for word in result["coverage"])


def test_every_listing_says_there_is_no_server_side_query() -> None:
    """Unconditional, because it is a property of Slack's method.

    A model that believed a filter existed would look for the argument rather
    than page the List, and would go on looking.
    """
    result = _run(_toolkit(_Workspace()).read_slack_list(list_id=_LIST))

    assert any(
        word.startswith("no_server_side_query:") for word in result["coverage"]
    )


def test_a_page_larger_than_this_tool_returns_is_reduced_and_says_so() -> None:
    workspace = _Workspace()

    result = _run(
        _toolkit(workspace).read_slack_list(list_id=_LIST, limit=10_000)
    )

    assert workspace.payload("slackLists.items.list")["limit"] == MAX_ITEMS_PER_PAGE
    assert any(word.startswith("limit_reduced:") for word in result["coverage"])


def test_include_list_is_off_unless_it_is_asked_for() -> None:
    workspace = _Workspace()

    _run(_toolkit(workspace).read_slack_list(list_id=_LIST))
    assert "include_list" not in workspace.payload("slackLists.items.list")

    workspace = _Workspace()
    result = _run(
        _toolkit(workspace).read_slack_list(list_id=_LIST, include_list=True)
    )
    assert workspace.payload("slackLists.items.list")["include_list"] is True
    assert result["list"] == {"id": _LIST, "title": "Runbook"}


def test_archived_rows_are_a_separate_page_rather_than_a_mixture() -> None:
    workspace = _Workspace()

    result = _run(
        _toolkit(workspace).read_slack_list(list_id=_LIST, archived=True)
    )

    assert workspace.payload("slackLists.items.list")["archived"] is True
    assert result["archived"] is True


def test_reading_one_row_returns_the_list_because_nothing_else_does() -> None:
    """Slack has no method that returns a List on its own, so this is where a
    schema comes from and the result has to say so.
    """
    workspace = _Workspace()

    result = _run(
        _toolkit(workspace).read_slack_list(list_id=_LIST, item_id=_ITEM)
    )

    payload = workspace.payload("slackLists.items.info")
    assert payload == {"list_id": _LIST, "id": _ITEM}
    assert result["item"] == {"id": _ITEM}
    assert result["list"]["schema"] == [{"id": _COLUMN}]
    assert "no method that returns a List on its own" in result["list_note"]


def test_include_is_subscribed_travels_and_comes_back() -> None:
    workspace = _Workspace()

    result = _run(
        _toolkit(workspace).read_slack_list(
            list_id=_LIST, item_id=_ITEM, include_is_subscribed=True
        )
    )

    assert workspace.payload("slackLists.items.info")["include_is_subscribed"] is True
    assert result["is_subscribed"] is True


def test_include_is_subscribed_without_a_row_is_refused_rather_than_ignored() -> None:
    workspace = _Workspace()

    result = _run(
        _toolkit(workspace).read_slack_list(
            list_id=_LIST, include_is_subscribed=True
        )
    )

    assert result["error"] == "include_is_subscribed_without_item_id"
    assert workspace.calls == []


def test_paging_arguments_alongside_a_row_id_are_refused() -> None:
    """Two different Slack calls. Honouring one and dropping the other would
    report success for a call that was not the one asked for.
    """
    workspace = _Workspace()

    result = _run(
        _toolkit(workspace).read_slack_list(
            list_id=_LIST, item_id=_ITEM, limit=5
        )
    )

    assert result["error"] == "listing_arguments_with_item_id"
    assert workspace.calls == []


def test_a_malformed_list_id_never_reaches_slack() -> None:
    workspace = _Workspace()

    result = _run(_toolkit(workspace).read_slack_list(list_id="not-an-id"))

    assert result["error"] == "list_id_malformed"
    assert workspace.calls == []


def test_a_missing_list_id_is_named_rather_than_guessed_at() -> None:
    workspace = _Workspace()

    result = _run(_toolkit(workspace).read_slack_list(list_id=""))

    assert result["error"] == "list_id_required"
    assert workspace.calls == []


# ── 4. exporting ─────────────────────────────────────────────────────────────


def test_an_export_starts_a_job_and_collects_it() -> None:
    workspace = _Workspace()

    result = _run(
        _toolkit(workspace).read_slack_list(list_id=_LIST, export=True)
    )

    assert workspace.payload("slackLists.download.start") == {
        "list_id": _LIST,
        "format": "csv",
    }
    assert workspace.payload("slackLists.download.get")["job_id"] == "LeF1234567"
    assert result["download_url"] == _EXPORT_URL


def test_the_json_only_options_travel_with_the_json_format() -> None:
    workspace = _Workspace()

    _run(
        _toolkit(workspace).read_slack_list(
            list_id=_LIST,
            export=True,
            export_format="json",
            include_threads=True,
            include_attachments=True,
            include_archived=True,
        )
    )

    assert workspace.payload("slackLists.download.start") == {
        "list_id": _LIST,
        "format": "json",
        "include_archived": True,
        "include_threads": True,
        "include_attachments": True,
    }


def test_the_collecting_call_repeats_what_the_starting_call_was_given() -> None:
    """Slack requires the two to match, so a caller who never sees the second
    call cannot be the one responsible for keeping them in step.
    """
    workspace = _Workspace()

    _run(
        _toolkit(workspace).read_slack_list(
            list_id=_LIST,
            export=True,
            export_format="json",
            include_threads=True,
        )
    )

    assert workspace.payload("slackLists.download.get") == {
        "list_id": _LIST,
        "job_id": "LeF1234567",
        "format": "json",
        "include_threads": True,
    }


def test_a_finished_export_comes_back_as_content_and_not_only_a_url() -> None:
    """A URL and no further word is what sent a model to a shell with curl.

    The hardened transfer path exists for exactly this fetch, and a bot token
    on a command line is outside every check written for one.
    """
    workspace = _Workspace()
    http = _Http()

    result = _run(
        _toolkit(workspace, http).read_slack_list(list_id=_LIST, export=True)
    )

    assert result["content"] == _EXPORT_CSV.decode()
    assert result["content_truncated"] is False
    assert result["content_chars"] == len(_EXPORT_CSV)
    assert result["download_url"] == _EXPORT_URL
    assert "the whole export" in result["download_note"]


def test_the_export_download_carries_the_bot_token_and_nothing_else() -> None:
    """The file is unreadable without it, and it goes to one host only."""
    workspace = _Workspace()
    http = _Http()

    _run(_toolkit(workspace, http).read_slack_list(list_id=_LIST, export=True))

    ((url, headers),) = http.requests
    assert url == _EXPORT_URL
    assert headers == {"Authorization": "Bearer xoxb-config-secret"}


def test_an_export_too_long_to_return_whole_says_so_rather_than_cutting() -> None:
    """A CSV that looks complete and is not would be read as the whole List."""
    workspace = _Workspace()
    http = _Http(_Body(b"row\n" * MAX_EXPORT_CHARS))

    result = _run(
        _toolkit(workspace, http).read_slack_list(list_id=_LIST, export=True)
    )

    assert result["content_truncated"] is True
    assert result["content_chars"] == MAX_EXPORT_CHARS
    assert result["content_chars_total"] > MAX_EXPORT_CHARS
    assert str(MAX_EXPORT_CHARS) in result["download_note"]
    assert result["download_url"] == _EXPORT_URL


def test_an_export_past_the_byte_ceiling_stops_reading_and_says_so() -> None:
    """The count reported is what was decoded rather than the file's length,
    which is a number nothing here can know without pulling the rest down. The
    note says so instead of implying the export was that size.
    """
    workspace = _Workspace()
    http = _Http(_Body(b"x" * (MAX_EXPORT_BYTES + 1)))

    result = _run(
        _toolkit(workspace, http).read_slack_list(list_id=_LIST, export=True)
    )

    assert result["content_truncated"] is True
    assert result["content"] == ""
    assert "floor rather than the file's length" in result["download_note"]


def test_a_download_from_a_host_this_app_will_not_dial_is_not_attempted() -> None:
    """Slack names the host, and a token goes to files.slack.com or nowhere.

    Widening the allow-list is a decision for a person, so a URL somewhere
    else is reported by name rather than followed.
    """
    workspace = _Workspace(
        download=[{"status": "complete", "download_url": "https://elsewhere/x"}]
    )
    http = _Http()

    result = _run(
        _toolkit(workspace, http).read_slack_list(list_id=_LIST, export=True)
    )

    assert http.requests == []
    assert result["ok"] is True
    assert result["content_error"] == "export_host_not_allowed"
    assert result["download_url"] == "https://elsewhere/x"
    assert "needs this app's token" in result["download_note"]


def test_a_download_that_fails_leaves_the_export_reported_as_finished() -> None:
    """The job ran. A fetch this tool could not make is a fact about the
    fetch, and the URL is the caller's remaining way to the file.
    """
    workspace = _Workspace()
    http = _Http(_Body(b"", status=403))

    result = _run(
        _toolkit(workspace, http).read_slack_list(list_id=_LIST, export=True)
    )

    assert result["ok"] is True
    assert "content" not in result
    assert result["content_error"] == "export_read_denied"
    assert result["download_url"] == _EXPORT_URL


def test_a_json_only_option_on_a_csv_export_is_refused_rather_than_dropped() -> None:
    """Slack applies neither to csv, so sending it would produce an export
    silently missing what was asked for.
    """
    workspace = _Workspace()

    result = _run(
        _toolkit(workspace).read_slack_list(
            list_id=_LIST, export=True, include_threads=True
        )
    )

    assert result["error"] == "export_option_needs_json"
    assert workspace.calls == []


def test_an_unknown_export_format_never_reaches_slack() -> None:
    workspace = _Workspace()

    result = _run(
        _toolkit(workspace).read_slack_list(
            list_id=_LIST, export=True, export_format="parquet"
        )
    )

    assert result["error"] == "export_format_unknown"
    assert workspace.calls == []


def test_an_export_that_is_not_ready_hands_back_the_job_rather_than_failing() -> None:
    workspace = _Workspace(download=[{"status": "processing"}] * 5)

    result = _run(
        _toolkit(workspace).read_slack_list(list_id=_LIST, export=True)
    )

    assert result["ok"] is True
    assert result["ready"] is False
    assert result["export_job_id"] == "LeF1234567"
    # Slack's own word, passed through rather than matched against a vocabulary
    # nobody published.
    assert result["status"] == "processing"


def test_a_job_id_resumes_an_export_rather_than_starting_another() -> None:
    workspace = _Workspace()

    result = _run(
        _toolkit(workspace).read_slack_list(
            list_id=_LIST, export_job_id="LeF1234567"
        )
    )

    assert "slackLists.download.start" not in workspace.methods()
    assert result["download_url"] == _EXPORT_URL


def test_export_options_without_an_export_are_refused() -> None:
    workspace = _Workspace()

    result = _run(
        _toolkit(workspace).read_slack_list(
            list_id=_LIST, export_format="json"
        )
    )

    assert result["error"] == "export_arguments_without_export"
    assert workspace.calls == []


def test_an_export_and_a_single_row_are_two_different_reads() -> None:
    workspace = _Workspace()

    result = _run(
        _toolkit(workspace).read_slack_list(
            list_id=_LIST, item_id=_ITEM, export=True
        )
    )

    assert result["error"] == "read_mode_ambiguous"
    assert workspace.calls == []


# ── 5. creating a List ───────────────────────────────────────────────────────


def test_a_create_says_the_columns_can_never_be_changed() -> None:
    """The one fact about this call a caller cannot recover afterwards."""
    workspace = _Workspace()

    result = _run(_toolkit(workspace).write_slack_list(name="Runbook"))

    assert result["ok"] is True
    assert result["list_id"] == _OTHER_LIST
    assert "No Slack method adds, removes, renames or retypes a column" in (
        result["schema_note"]
    )


def test_a_description_written_as_markdown_reaches_slack_as_rich_text() -> None:
    workspace = _Workspace()

    _run(
        _toolkit(workspace).write_slack_list(
            name="Runbook", description="**bold**"
        )
    )

    blocks = workspace.payload("slackLists.create")["description_blocks"]
    assert blocks[0]["type"] == "rich_text"
    section = blocks[0]["elements"][0]
    assert section["elements"][0] == {
        "type": "text",
        "text": "bold",
        "style": {"bold": True},
    }


def test_a_supplied_description_payload_travels_unchanged() -> None:
    workspace = _Workspace()
    supplied = [{"type": "rich_text", "elements": []}]

    _run(
        _toolkit(workspace).write_slack_list(
            name="Runbook", description_blocks=supplied
        )
    )

    assert workspace.payload("slackLists.create")["description_blocks"] == supplied


def test_a_description_given_twice_is_refused_rather_than_resolved() -> None:
    workspace = _Workspace()

    result = _run(
        _toolkit(workspace).write_slack_list(
            name="Runbook", description="x", description_blocks=[{"type": "rich_text"}]
        )
    )

    assert result["error"] == "description_ambiguous"
    assert workspace.calls == []


def test_a_copy_and_a_schema_are_refused_before_the_list_is_made() -> None:
    """Slack's own invalid_copy_and_schema_args, checked here because a create
    is a write and a write refused for a bad argument must not have to happen.
    """
    workspace = _Workspace()

    result = _run(
        _toolkit(workspace).write_slack_list(
            name="Runbook",
            schema=[{"key": "title", "type": "text"}],
            copy_from_list_id=_OTHER_LIST,
        )
    )

    assert result["error"] == "invalid_copy_and_schema_args"
    assert workspace.calls == []


def test_copying_records_without_a_list_to_copy_is_refused() -> None:
    workspace = _Workspace()

    result = _run(
        _toolkit(workspace).write_slack_list(
            name="Runbook", include_copied_list_records=True
        )
    )

    assert result["error"] == "missing_arg_copy_from_list_id"
    assert workspace.calls == []


def test_a_copy_carries_its_records_only_when_asked() -> None:
    workspace = _Workspace()

    _run(
        _toolkit(workspace).write_slack_list(
            name="Runbook",
            copy_from_list_id=_OTHER_LIST,
            include_copied_list_records=True,
        )
    )

    payload = workspace.payload("slackLists.create")
    assert payload["copy_from_list_id"] == _OTHER_LIST
    assert payload["include_copied_list_records"] is True


def test_todo_mode_travels() -> None:
    workspace = _Workspace()

    _run(_toolkit(workspace).write_slack_list(name="Tasks", todo_mode=True))

    assert workspace.payload("slackLists.create")["todo_mode"] is True


def test_a_schema_that_is_not_a_list_of_columns_never_reaches_slack() -> None:
    workspace = _Workspace()

    result = _run(
        _toolkit(workspace).write_slack_list(name="Runbook", schema="text")
    )

    assert result["error"] == "schema_malformed"
    assert workspace.calls == []


def test_a_list_without_a_name_is_refused() -> None:
    workspace = _Workspace()

    result = _run(_toolkit(workspace).write_slack_list(name="  "))

    assert result["error"] == "name_required"
    assert workspace.calls == []


# ── 6. editing: text cells and the renderer ──────────────────────────────────


def test_a_cell_written_as_markdown_reaches_slack_as_a_rich_text_block() -> None:
    """Slack refuses a plain string, and every documented example wraps the
    elements in a rich_text *block* rather than sending the array bare.
    """
    workspace = _Workspace()

    _run(
        _toolkit(workspace).edit_slack_list(
            list_id=_LIST,
            operation="update_cells",
            cells=[{"row_id": _ITEM, "column_id": _COLUMN, "text": "Restart it"}],
        )
    )

    (cell,) = workspace.payload("slackLists.items.update")["cells"]
    assert "text" not in cell
    assert cell["rich_text"] == [
        {
            "type": "rich_text",
            "elements": [
                {
                    "type": "rich_text_section",
                    "elements": [{"type": "text", "text": "Restart it"}],
                }
            ],
        }
    ]


def test_a_supplied_rich_text_payload_travels_unchanged() -> None:
    workspace = _Workspace()
    supplied = [{"type": "rich_text", "elements": [{"type": "rich_text_section"}]}]

    _run(
        _toolkit(workspace).edit_slack_list(
            list_id=_LIST,
            operation="update_cells",
            cells=[
                {"row_id": _ITEM, "column_id": _COLUMN, "rich_text": supplied}
            ],
        )
    )

    (cell,) = workspace.payload("slackLists.items.update")["cells"]
    assert cell["rich_text"] == supplied


def test_a_cell_carrying_both_spellings_is_refused_rather_than_resolved() -> None:
    workspace = _Workspace()

    result = _run(
        _toolkit(workspace).edit_slack_list(
            list_id=_LIST,
            operation="update_cells",
            cells=[
                {
                    "row_id": _ITEM,
                    "column_id": _COLUMN,
                    "text": "x",
                    "rich_text": [{"type": "rich_text"}],
                }
            ],
        )
    )

    assert result["error"] == "cell_malformed"
    assert workspace.calls == []


def test_a_value_key_this_module_knows_nothing_about_travels_unchanged() -> None:
    """The cards document the shapes; nothing checks or rewrites them here.
    A workspace's schema is the authority, and a column type Slack adds after
    this was written has to reach Slack rather than be refused on the way.
    """
    workspace = _Workspace()

    _run(
        _toolkit(workspace).edit_slack_list(
            list_id=_LIST,
            operation="update_cells",
            cells=[
                {"row_id": _ITEM, "column_id": _COLUMN, "rating": [4]},
                {"row_id": _ITEM, "column_id": "Col2", "user": [_ALICE, _BOB]},
            ],
        )
    )

    cells = workspace.payload("slackLists.items.update")["cells"]
    assert cells[0]["rating"] == [4]
    assert cells[1]["user"] == [_ALICE, _BOB]


def test_a_scalar_is_never_wrapped_for_a_column_that_wants_an_array() -> None:
    """The shape is documented, not enforced, and not fixed up either.

    Slack takes a date as ``["2026-09-25"]``. Wrapping a bare string here
    would make the tool accept what Slack refuses, and the caller would write
    the same refused shape the next time against something that does not.
    """
    workspace = _Workspace()

    _run(
        _toolkit(workspace).edit_slack_list(
            list_id=_LIST,
            operation="update_cells",
            cells=[
                {"row_id": _ITEM, "column_id": _COLUMN, "date": "2026-09-25"},
                {"row_id": _ITEM, "column_id": "Col2", "checkbox": True},
            ],
        )
    )

    cells = workspace.payload("slackLists.items.update")["cells"]
    assert cells[0]["date"] == "2026-09-25"
    assert cells[1]["checkbox"] is True


def test_a_column_id_is_forwarded_whatever_it_looks_like() -> None:
    """A caller's schema key reaches Slack as written, and Slack refuses it.

    Recognising a key here would mean holding the schema, which this tool
    never fetches, and guessing at one would send a cell to the wrong column.
    """
    workspace = _Workspace()

    _run(
        _toolkit(workspace).edit_slack_list(
            list_id=_LIST,
            operation="create_item",
            initial_fields=[{"column_id": "task_name", "text": "Probe"}],
        )
    )

    (cell,) = workspace.payload("slackLists.items.create")["initial_fields"]
    assert cell["column_id"] == "task_name"


def test_writing_cells_is_a_batch_across_rows_and_columns() -> None:
    """The method name is singular and the argument is not."""
    workspace = _Workspace()

    result = _run(
        _toolkit(workspace).edit_slack_list(
            list_id=_LIST,
            operation="update_cells",
            cells=[
                {"row_id": "R1", "column_id": "C1", "text": "a"},
                {"row_id": "R2", "column_id": "C2", "text": "b"},
                {"row_id": "R2", "column_id": "C3", "text": "c"},
            ],
        )
    )

    assert len(workspace.payload("slackLists.items.update")["cells"]) == 3
    assert result["cells"] == 3


def test_a_cell_may_create_its_own_row() -> None:
    workspace = _Workspace()

    _run(
        _toolkit(workspace).edit_slack_list(
            list_id=_LIST,
            operation="update_cells",
            cells=[
                {"row_id_to_create": True, "column_id": _COLUMN, "text": "new"}
            ],
        )
    )

    (cell,) = workspace.payload("slackLists.items.update")["cells"]
    assert cell["row_id_to_create"] is True


def test_a_cell_that_names_neither_a_row_nor_a_new_one_is_refused() -> None:
    workspace = _Workspace()

    result = _run(
        _toolkit(workspace).edit_slack_list(
            list_id=_LIST,
            operation="update_cells",
            cells=[{"column_id": _COLUMN, "text": "x"}],
        )
    )

    assert result["error"] == "row_id_required"
    assert workspace.calls == []


def test_a_cell_that_names_no_column_is_refused() -> None:
    workspace = _Workspace()

    result = _run(
        _toolkit(workspace).edit_slack_list(
            list_id=_LIST,
            operation="update_cells",
            cells=[{"row_id": _ITEM, "text": "x"}],
        )
    )

    assert result["error"] == "column_id_required"
    assert workspace.calls == []


def test_an_empty_cells_list_is_refused_rather_than_sent() -> None:
    workspace = _Workspace()

    result = _run(
        _toolkit(workspace).edit_slack_list(
            list_id=_LIST, operation="update_cells", cells=[]
        )
    )

    assert result["error"] == "cells_required"
    assert workspace.calls == []


# ── 7. editing: the other four operations ────────────────────────────────────


def test_the_five_operations_each_send_their_own_method() -> None:
    assert EDIT_OPERATIONS == {
        "create_item": "slackLists.items.create",
        "update_cells": "slackLists.items.update",
        "delete_item": "slackLists.items.delete",
        "delete_items": "slackLists.items.deleteMultiple",
        "update_list": "slackLists.update",
    }


def test_an_unknown_operation_never_reaches_slack() -> None:
    workspace = _Workspace()

    result = _run(
        _toolkit(workspace).edit_slack_list(list_id=_LIST, operation="rename")
    )

    assert result["error"] == "operation_unknown"
    assert workspace.calls == []


def test_an_argument_for_another_operation_is_refused_rather_than_ignored() -> None:
    """A model that passed it meant something, and quietly dropping it would
    report success for a call that did not do what was asked.
    """
    workspace = _Workspace()

    result = _run(
        _toolkit(workspace).edit_slack_list(
            list_id=_LIST, operation="delete_item", item_id=_ITEM, todo_mode=True
        )
    )

    assert result["error"] == "argument_not_for_this_operation"
    assert "todo_mode" in result["detail"]
    assert workspace.calls == []


def test_a_new_row_may_be_a_subtask_of_another_row() -> None:
    """parent_item_id is what makes a List a tree rather than a table."""
    workspace = _Workspace()

    result = _run(
        _toolkit(workspace).edit_slack_list(
            list_id=_LIST,
            operation="create_item",
            parent_item_id="Rec000PARENT",
            initial_fields=[{"column_id": _COLUMN, "text": "child"}],
        )
    )

    payload = workspace.payload("slackLists.items.create")
    assert payload["parent_item_id"] == "Rec000PARENT"
    assert payload["initial_fields"][0]["rich_text"][0]["type"] == "rich_text"
    assert result["parent_item_id"] == "Rec000PARENT"


def test_a_new_row_may_be_a_server_side_copy_of_another() -> None:
    workspace = _Workspace()

    _run(
        _toolkit(workspace).edit_slack_list(
            list_id=_LIST,
            operation="create_item",
            duplicated_item_id=_ITEM,
        )
    )

    assert workspace.payload("slackLists.items.create")["duplicated_item_id"] == _ITEM


def test_deleting_one_row_names_the_row_and_says_it_went() -> None:
    workspace = _Workspace()

    result = _run(
        _toolkit(workspace).edit_slack_list(
            list_id=_LIST, operation="delete_item", item_id=_ITEM
        )
    )

    assert workspace.payload("slackLists.items.delete") == {
        "list_id": _LIST,
        "id": _ITEM,
    }
    assert result["deleted"] is True


def test_deleting_several_rows_refuses_to_claim_they_all_went() -> None:
    """Slack documents no per-record result for this method, so an unrefused
    call establishes nothing about which of the named rows were removed.
    """
    workspace = _Workspace()

    result = _run(
        _toolkit(workspace).edit_slack_list(
            list_id=_LIST, operation="delete_items", item_ids=["R1", "R2"]
        )
    )

    assert workspace.payload("slackLists.items.deleteMultiple")["ids"] == ["R1", "R2"]
    assert result["ok"] is True
    assert "deleted" not in result
    assert "not established by this response" in result["detail"]


def test_an_empty_item_ids_list_is_refused_rather_than_sent() -> None:
    workspace = _Workspace()

    result = _run(
        _toolkit(workspace).edit_slack_list(
            list_id=_LIST, operation="delete_items", item_ids=[]
        )
    )

    assert result["error"] == "item_ids_required"
    assert workspace.calls == []


def test_item_ids_is_an_array_and_a_bare_string_is_not_one() -> None:
    workspace = _Workspace()

    result = _run(
        _toolkit(workspace).edit_slack_list(
            list_id=_LIST, operation="delete_items", item_ids=_ITEM
        )
    )

    assert result["error"] == "item_ids_malformed"
    assert workspace.calls == []


def test_updating_the_list_names_it_the_way_that_one_method_does() -> None:
    """slackLists.update is the one method in the family that calls the List
    ``id`` rather than ``list_id``.
    """
    workspace = _Workspace()

    _run(
        _toolkit(workspace).edit_slack_list(
            list_id=_LIST, operation="update_list", name="Runbook v2"
        )
    )

    assert workspace.payload("slackLists.update") == {
        "id": _LIST,
        "name": "Runbook v2",
    }


def test_an_update_that_changes_nothing_is_refused_and_says_why() -> None:
    workspace = _Workspace()

    result = _run(
        _toolkit(workspace).edit_slack_list(
            list_id=_LIST, operation="update_list"
        )
    )

    assert result["error"] == "nothing_to_update"
    assert "cannot change a column" in result["detail"]
    assert workspace.calls == []


# ── 8. sharing ───────────────────────────────────────────────────────────────


def test_a_grant_names_slacks_own_target_key_and_reports_ours() -> None:
    """chat_ids on the way in, channel_ids on the wire."""
    workspace = _Workspace()

    result = _run(
        _toolkit(workspace).share_slack_list(
            list_id=_LIST, access_level="read", chat_ids=[_CHAT]
        )
    )

    assert workspace.payload("slackLists.access.set") == {
        "list_id": _LIST,
        "channel_ids": [_CHAT],
        "access_level": "read",
    }
    assert result["granted"] == [_CHAT]


def test_targets_are_arrays_and_a_bare_string_is_not_one() -> None:
    workspace = _Workspace()

    result = _run(
        _toolkit(workspace).share_slack_list(
            list_id=_LIST, access_level="read", user_ids=_ALICE
        )
    )

    assert result["error"] == "access_targets_malformed"
    assert workspace.calls == []


def test_conversations_and_people_cannot_both_be_named_in_one_call() -> None:
    workspace = _Workspace()

    result = _run(
        _toolkit(workspace).share_slack_list(
            list_id=_LIST,
            access_level="read",
            chat_ids=[_CHAT],
            user_ids=[_ALICE],
        )
    )

    assert result["error"] == "access_targets_ambiguous"
    assert workspace.calls == []


def test_a_share_with_no_target_is_refused() -> None:
    workspace = _Workspace()

    result = _run(
        _toolkit(workspace).share_slack_list(list_id=_LIST, access_level="read")
    )

    assert result["error"] == "access_targets_required"
    assert workspace.calls == []


def test_owner_is_one_person_and_several_are_refused() -> None:
    """Slack does not document refusing this. It is refused anyway: a List has
    one owner, so a call naming three is incoherent whatever Slack does with
    it, and the outcomes it could have are ones no caller could tell apart.
    """
    workspace = _Workspace()

    result = _run(
        _toolkit(workspace).share_slack_list(
            list_id=_LIST,
            access_level="owner",
            user_ids=[_ALICE, _BOB, _CAROL],
        )
    )

    assert result["error"] == "owner_is_one_person"
    assert workspace.calls == []


def test_owner_cannot_be_a_conversation() -> None:
    workspace = _Workspace()

    result = _run(
        _toolkit(workspace).share_slack_list(
            list_id=_LIST, access_level="owner", chat_ids=[_CHAT]
        )
    )

    assert result["error"] == "owner_must_be_a_person"
    assert workspace.calls == []


def test_one_owner_is_allowed() -> None:
    workspace = _Workspace()

    result = _run(
        _toolkit(workspace).share_slack_list(
            list_id=_LIST, access_level="owner", user_ids=[_ALICE]
        )
    )

    assert result["ok"] is True
    assert workspace.payload("slackLists.access.set")["access_level"] == "owner"


def test_removing_access_sends_the_other_method_and_carries_no_level() -> None:
    workspace = _Workspace()

    result = _run(
        _toolkit(workspace).share_slack_list(
            list_id=_LIST, user_ids=[_ALICE], remove=True
        )
    )

    assert workspace.payload("slackLists.access.delete") == {
        "list_id": _LIST,
        "user_ids": [_ALICE],
    }
    assert result["access_level"] == "none"


def test_a_level_alongside_remove_is_refused() -> None:
    workspace = _Workspace()

    result = _run(
        _toolkit(workspace).share_slack_list(
            list_id=_LIST, access_level="read", user_ids=[_ALICE], remove=True
        )
    )

    assert result["error"] == "access_level_not_accepted"
    assert workspace.calls == []


def test_an_unknown_level_never_reaches_slack() -> None:
    workspace = _Workspace()

    result = _run(
        _toolkit(workspace).share_slack_list(
            list_id=_LIST, access_level="admin", user_ids=[_ALICE]
        )
    )

    assert result["error"] == "access_level_unknown"
    assert workspace.calls == []


def test_a_multi_target_grant_that_half_succeeds_is_reported_per_target() -> None:
    """Slack refuses the whole call with one code and names no target, so the
    outcome is established one call at a time.
    """
    seen: list[int] = []

    def refuse(method: str, payload: dict[str, Any]) -> Any:
        if method != "slackLists.access.set":
            return None
        targets = payload.get("user_ids") or []
        seen.append(len(targets))
        if len(targets) > 1:
            return "failed_to_update_user_ids"
        if targets == [_BOB]:
            return "user_not_found"
        return None

    workspace = _Workspace(fail=refuse)

    result = _run(
        _toolkit(workspace).share_slack_list(
            list_id=_LIST,
            access_level="read",
            user_ids=[_ALICE, _BOB, _CAROL],
        )
    )

    assert result["ok"] is False
    assert result["error"] == "access_partially_applied"
    assert result["granted"] == [_ALICE, _CAROL]
    assert result["failed"] == [{"id": _BOB, "error": "user_not_found"}]
    assert "undetermined" not in result
    # The first call named all three; the three after it named one each.
    assert seen == [3, 1, 1, 1]


def test_targets_the_deadline_never_reached_are_neither_granted_nor_failed() -> None:
    """Reporting an untried target as failed is as wrong as reporting it
    granted, so the third list exists and is used.
    """
    clock = iter([0.0] + [1.0] * 3 + [999.0] * 50)

    def refuse(method: str, payload: dict[str, Any]) -> Any:
        if method != "slackLists.access.set":
            return None
        return "failed_to_update_user_ids" if len(payload["user_ids"]) > 1 else None

    workspace = _Workspace(fail=refuse)
    toolkit = SlackListToolkit(
        metadata={METADATA_TEAM_KEY: "T000"},
        client=workspace,
        sleep=_no_sleep,
        monotonic=lambda: next(clock),
    )

    result = _run(
        toolkit.share_slack_list(
            list_id=_LIST,
            access_level="read",
            user_ids=[_ALICE, _BOB, _CAROL],
        )
    )

    assert result["ok"] is False
    assert result["undetermined"]
    assert "nothing is claimed about them" in result["detail"]


def test_a_single_target_failure_is_not_turned_into_a_per_target_pass() -> None:
    """One target, one answer. Asking again would be a second identical call
    for information the first one already gave.
    """
    workspace = _Workspace(fail={"slackLists.access.set": "user_not_found"})

    result = _run(
        _toolkit(workspace).share_slack_list(
            list_id=_LIST, access_level="read", user_ids=[_ALICE]
        )
    )

    assert result["error"] == "user_not_found"
    assert len(workspace.payloads("slackLists.access.set")) == 1


# ── 9. deleting, and the guard in front of it ────────────────────────────────


def test_deleting_a_list_checks_the_file_first_and_then_deletes_it() -> None:
    workspace = _Workspace()

    result = _run(_toolkit(workspace).delete_slack_list(list_id=_LIST))

    assert workspace.methods() == ["files_info", "files_delete"]
    assert workspace.payload("files_delete") == {"file": _LIST}
    assert result["deleted"] is True


def test_a_file_that_is_not_a_list_is_never_deleted() -> None:
    """The guard, and the reason the tool reads before it writes. files.delete
    destroys whatever it is handed, so an id naming a canvas would otherwise be
    answered with a successful deletion of somebody's document.
    """
    workspace = _Workspace(file_record={"filetype": "canvas", "title": "Notes"})

    result = _run(_toolkit(workspace).delete_slack_list(list_id=_LIST))

    assert result["error"] == "not_a_list"
    assert "files_delete" not in workspace.methods()


def test_a_file_of_unknown_type_is_refused_rather_than_assumed_to_be_a_list() -> None:
    """Fails closed. An absent filetype is not a List having no type; it is
    this tool not knowing, and it is not a reason to delete.
    """
    workspace = _Workspace(file_record={"title": "Mystery"})

    result = _run(_toolkit(workspace).delete_slack_list(list_id=_LIST))

    assert result["error"] == "not_a_list"
    assert "files_delete" not in workspace.methods()


def test_a_file_slack_returns_no_record_for_is_not_deleted() -> None:
    workspace = _Workspace()
    workspace.file_record = None

    result = _run(_toolkit(workspace).delete_slack_list(list_id=_LIST))

    assert result["error"] == "file_not_found"
    assert "files_delete" not in workspace.methods()


def test_a_refused_lookup_stops_the_delete() -> None:
    workspace = _Workspace(fail={"files_info": "file_not_found"})

    result = _run(_toolkit(workspace).delete_slack_list(list_id=_LIST))

    assert result["error"] == "file_not_found"
    assert "files_delete" not in workspace.methods()


def test_a_malformed_id_never_reaches_the_lookup_either() -> None:
    workspace = _Workspace()

    result = _run(_toolkit(workspace).delete_slack_list(list_id="C123"))

    assert result["error"] == "list_id_malformed"
    assert workspace.calls == []


# ── 10. refusals Slack writes ────────────────────────────────────────────────


@pytest.mark.parametrize(
    "code",
    [
        "list_not_found",
        "record_not_found",
        "record_deleted",
        "invalid_column_id",
        "column_not_found",
        "uneditable_column",
        "invalid_input_type",
        "invalid_option_id",
        "over_row_maximum",
        "over_cell_fields_limit",
        "over_title_length_maximum",
        "permission_denied",
        "lists_disabled_user_team",
        "unknown_method",
        "invalid_date",
        "invalid_email",
        "invalid_phone_number",
        "invalid_link",
        "invalid_message",
        "invalid_blocks",
        "invalid_text_block",
        "invalid_vote_value",
        "invalid_arguments",
        "channel_not_found",
        "user_not_found",
    ],
)
def test_every_documented_refusal_comes_back_with_a_sentence(code: str) -> None:
    """A code alone tells a reader nothing it can act on."""
    workspace = _Workspace(fail={"slackLists.items.update": code})

    result = _run(
        _toolkit(workspace).edit_slack_list(
            list_id=_LIST,
            operation="update_cells",
            cells=[{"row_id": _ITEM, "column_id": _COLUMN, "text": "x"}],
        )
    )

    assert result["ok"] is False
    assert result["error"] == code
    assert len(result["detail"]) > 20


@pytest.mark.parametrize(
    "code",
    [
        "over_column_maximum",
        "over_list_file_maximum",
        "invalid_primary_column",
        "invalid_copy_and_schema_args",
    ],
)
def test_every_documented_create_refusal_comes_back_with_a_sentence(
    code: str,
) -> None:
    workspace = _Workspace(fail={"slackLists.create": code})

    result = _run(_toolkit(workspace).write_slack_list(name="Runbook"))

    assert result["error"] == code
    assert len(result["detail"]) > 20


def test_a_refused_cell_write_names_what_to_check_when_slack_will_not() -> None:
    """Slack answers invalid_arguments without naming an argument.

    The detail cannot name one either, so it names the two mistakes known to
    produce it -- a schema key passed where a generated column id belongs, and
    a value outside the array its type takes -- and says it does not know
    which. Both were made in one sitting against a refusal that said only
    that Slack had refused.
    """
    workspace = _Workspace(fail={"slackLists.items.update": "invalid_arguments"})

    result = _run(
        _toolkit(workspace).edit_slack_list(
            list_id=_LIST,
            operation="update_cells",
            cells=[{"row_id": _ITEM, "column_id": _COLUMN, "date": "2026-09-25"}],
        )
    )

    detail = result["detail"]
    assert result["error"] == "invalid_arguments"
    assert "names none of them" in detail
    assert "the id Slack generated for that column" in detail
    assert "include_list true" in detail
    assert "takes its value in an array" in detail
    assert "checkbox is the one Slack documents as a bare boolean" in detail
    assert "worth reporting as one" in detail


def test_the_same_refusal_on_a_create_row_says_the_same_two_things() -> None:
    """The mistake is the cell, and a row is created with cells too."""
    workspace = _Workspace(fail={"slackLists.items.create": "invalid_arguments"})

    result = _run(
        _toolkit(workspace).edit_slack_list(
            list_id=_LIST,
            operation="create_item",
            initial_fields=[{"column_id": "task_name", "text": "Probe"}],
        )
    )

    assert "the id Slack generated for that column" in result["detail"]


def test_a_refusal_on_a_method_that_writes_no_cell_claims_no_cell() -> None:
    """Slack attaches this code to any malformed call at all.

    A create that got it back has no cells to check, so the cell sentence
    would send a reader looking at the wrong argument.
    """
    workspace = _Workspace(fail={"slackLists.create": "invalid_arguments"})

    result = _run(_toolkit(workspace).write_slack_list(name="Runbook"))

    assert result["error"] == "invalid_arguments"
    assert "names none of them" in result["detail"]
    assert "column_id" not in result["detail"]


def test_a_refused_call_claims_nothing_about_the_list() -> None:
    """A create Slack refused did not half-create anything, and a key saying
    ``created: false`` would read as a claim about the List rather than about
    this call.
    """
    workspace = _Workspace(fail={"slackLists.create": "over_list_file_maximum"})

    result = _run(_toolkit(workspace).write_slack_list(name="Runbook"))

    assert set(result) == {"ok", "error", "detail"}


def test_a_missing_scope_names_the_scope_that_is_missing() -> None:
    read = _Workspace(fail={"slackLists.items.list": "missing_scope"})
    write = _Workspace(fail={"slackLists.items.delete": "missing_scope"})

    listing = _run(_toolkit(read).read_slack_list(list_id=_LIST))
    deleting = _run(
        _toolkit(write).edit_slack_list(
            list_id=_LIST, operation="delete_item", item_id=_ITEM
        )
    )

    assert "lists:read" in listing["detail"]
    assert "lists:write" in deleting["detail"]


def test_slacks_own_sentence_is_passed_through_rather_than_summarised() -> None:
    workspace = _Workspace(
        fail={"slackLists.items.update": ("invalid_input_type", "line 3: bad")}
    )

    result = _run(
        _toolkit(workspace).edit_slack_list(
            list_id=_LIST,
            operation="update_cells",
            cells=[{"row_id": _ITEM, "column_id": _COLUMN, "text": "x"}],
        )
    )

    assert "Slack said: line 3: bad" in result["detail"]


def test_a_rate_limit_is_waited_out_rather_than_reported() -> None:
    state = {"calls": 0}

    def refuse(method: str, _payload: dict[str, Any]) -> Any:
        if method != "slackLists.items.list":
            return None
        state["calls"] += 1
        return "ratelimited" if state["calls"] == 1 else None

    workspace = _Workspace(fail=refuse)

    result = _run(_toolkit(workspace).read_slack_list(list_id=_LIST))

    assert result["ok"] is True
    assert state["calls"] == 2


# ── 11. the cards ────────────────────────────────────────────────────────────


def test_the_read_card_says_there_is_no_query() -> None:
    """Without it a model looks for the missing argument rather than paging."""
    description = _all_cards()["read_slack_list"].description

    assert "There is no way to search or sort" in description
    assert "Do not look for a query argument; there is none" in description


def test_the_read_card_says_where_a_schema_comes_from() -> None:
    description = _all_cards()["read_slack_list"].description

    assert "Slack has no method that returns a List on its own" in description


def test_the_read_card_says_an_export_arrives_as_content_with_a_cap() -> None:
    """Silence about the URL is what sent a model to the shell, and a cap
    nobody declared would hand back a partial file that reads as a whole one.
    """
    description = _all_cards()["read_slack_list"].description

    assert "comes back as content, not just a link" in description
    assert str(MAX_EXPORT_CHARS) in description
    assert "needs this app's token" in description
    assert "content_truncated says so" in description
    assert "content_error names why" in description


def test_the_write_card_says_the_columns_are_permanent() -> None:
    """A model that did not know would create first and look for the column
    tool second, by which time the List is made.
    """
    description = _all_cards()["write_slack_list"].description

    assert "cannot be changed later" in description
    assert "No Slack method adds a column" in description


def test_the_edit_card_says_it_cannot_change_a_column_either() -> None:
    description = _all_cards()["edit_slack_list"].description

    assert "It cannot change a column" in description


def test_the_edit_card_says_a_text_cell_cannot_be_plain_text() -> None:
    description = _all_cards()["edit_slack_list"].description

    assert "plain text is not accepted in a request" in description


def test_the_edit_card_names_what_the_markdown_does_not_carry() -> None:
    """A caller must be able to predict what survives. The renderer drops
    several constructs, and a silent reduction is the failure mode here.
    """
    description = _all_cards()["edit_slack_list"].description

    for absent in (
        "A table arrives as its pipe characters",
        "A heading arrives bold with its level gone",
        "An image arrives as a link to itself",
        "a second level of block quote",
        "A mention written as a name",
        "A link needs a scheme",
    ):
        assert absent in description, absent


def test_the_edit_card_gives_the_shape_of_every_documented_cell() -> None:
    """Six refused calls were spent learning that a date cell is an array.

    Every key Slack shows a worked cell for is named with its shape, so a
    model can write a cell of any documented type without a refusal first.
    """
    description = _all_cards()["edit_slack_list"].description

    assert len(CELL_VALUE_SHAPES) == 15
    for key, shape in CELL_VALUE_SHAPES.items():
        assert f"\n  {key} -- {shape}." in description, key
    assert "a text column takes rich_text" in description
    assert "a bare true or false" in description
    assert "Almost every value sits in an array" in description


def test_the_edit_card_names_the_types_slack_publishes_no_cell_for() -> None:
    """Naming them is the difference between *unknown* and *unmentioned*.

    A guess would be indistinguishable from the documented rows above it, and
    assignee, completed and multi_select each have an obvious wrong guess.
    """
    description = _all_cards()["edit_slack_list"].description

    for name in UNDOCUMENTED_CELL_TYPES:
        assert name in description, name
    assert "publishes a key for none of these" in description
    assert not set(UNDOCUMENTED_CELL_TYPES) & set(CELL_VALUE_SHAPES)


def test_the_edit_card_says_the_tool_reshapes_nothing() -> None:
    description = _all_cards()["edit_slack_list"].description

    assert "A scalar is not wrapped in an array" in description


def test_the_cards_say_a_column_id_is_slacks_id_and_not_the_key() -> None:
    """The first two of six refused calls were this, and the card said only
    that column ids come from the schema -- which the caller's own key does
    too.
    """
    cards = _all_cards()
    edit = cards["edit_slack_list"]

    assert "Column ids are Slack's, not yours" in edit.description
    assert "include_list true" in edit.description
    for argument in ("cells", "initial_fields"):
        column = edit.input_params["properties"][argument]["items"][
            "properties"
        ]["column_id"]["description"]
        assert "the id Slack generated for it" in column, argument
        assert "not the key" in column.replace("Not the key", "not the key")

    assert "not what writes it" in cards["write_slack_list"].description
    key = cards["write_slack_list"].input_params["properties"]["schema"][
        "items"
    ]["properties"]["key"]["description"]
    assert "not this" in key

    assert "A column id is Slack's, not yours" in cards[
        "read_slack_list"
    ].description


def test_the_edit_card_says_a_multiple_delete_reports_nothing_per_row() -> None:
    description = _all_cards()["edit_slack_list"].description

    assert "Slack reports no per-row result for delete_items" in description


def test_the_share_card_says_a_partial_result_is_read_from_the_lists() -> None:
    description = _all_cards()["share_slack_list"].description

    assert "Read the lists rather than the overall result" in description


def test_the_delete_card_says_only_a_list_is_deleted() -> None:
    description = _all_cards()["delete_slack_list"].description

    assert "cannot be undone" in description
    assert "is refused and nothing happens to it" in description


def test_the_cards_offer_the_enums_rather_than_letting_a_model_guess() -> None:
    cards = _all_cards()

    assert cards["share_slack_list"].input_params["properties"]["access_level"][
        "enum"
    ] == list(ACCESS_LEVELS)
    assert cards["read_slack_list"].input_params["properties"]["export_format"][
        "enum"
    ] == list(EXPORT_FORMATS)
    assert cards["edit_slack_list"].input_params["properties"]["operation"][
        "enum"
    ] == sorted(EDIT_OPERATIONS)


def test_the_write_card_lists_the_column_types_without_closing_the_set() -> None:
    """A reading of one worked example is not a published enumeration, so it is
    offered as help and enforced nowhere: a type Slack adds tomorrow must not be
    refused by a list transcribed today.
    """
    column = _all_cards()["write_slack_list"].input_params["properties"]["schema"]
    kind = column["items"]["properties"]["type"]

    assert "enum" not in kind
    for name in COLUMN_TYPES:
        assert name in kind["description"], name


def test_every_optional_argument_slack_documents_is_offered() -> None:
    """The naive design drops these, and each exists for a reason nobody here
    is entitled to overrule.
    """
    cards = _all_cards()
    expected = {
        "read_slack_list": {
            "list_id",
            "item_id",
            "include_is_subscribed",
            "archived",
            "include_list",
            "limit",
            "cursor",
            "export",
            "export_format",
            "export_job_id",
            "include_archived",
            "include_threads",
            "include_attachments",
        },
        "write_slack_list": {
            "name",
            "description",
            "description_blocks",
            "schema",
            "copy_from_list_id",
            "include_copied_list_records",
            "todo_mode",
        },
        "edit_slack_list": {
            "list_id",
            "operation",
            "cells",
            "initial_fields",
            "duplicated_item_id",
            "parent_item_id",
            "item_id",
            "item_ids",
            "name",
            "description",
            "description_blocks",
            "todo_mode",
        },
        "share_slack_list": {
            "list_id",
            "access_level",
            "chat_ids",
            "user_ids",
            "remove",
        },
        "delete_slack_list": {"list_id"},
    }
    for name, arguments in expected.items():
        assert set(cards[name].input_params["properties"]) == arguments, name


def test_no_card_writes_channel_id_where_this_package_writes_chat_id() -> None:
    for card in _all_cards().values():
        assert "channel_id" not in json.dumps(card.input_params)
        assert "channel_ids" not in card.description
