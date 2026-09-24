# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Reading, writing, sharing and deleting Slack canvases.

Four things are pinned here that nothing else can pin.

* **A canvas is read as HTML and written as markdown.** The round trip is lossy
  by construction, so there is no read-modify-write, and the card has to say so
  in those words: a model that believed otherwise would send back a mangled
  document and Slack would accept it.
* **``canvas_not_found`` conflates three states.** Absent, deleted, and never
  shared with this app. Only the third is fixable and only by a person, so a
  refusal that named one of the three would send somebody after the wrong fix.
* **A multi-target grant can half succeed.** Slack refuses the whole call with
  one code and says nothing about which targets took, so the outcome is
  established per target. A single boolean here would be a claim nobody checked.
* **Creating is two tools.** ``canvases.create`` and
  ``conversations.canvases.create`` make different objects, nothing converts
  between them, and ``permissions.tools`` is keyed by name -- so the split is
  what makes *may write a canvas, may not create the channel's* expressible.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Self

import pytest

import jiuwenswarm.common.config as config_module
from jiuwenswarm.agents.harness.common.tools.slack_canvases import (
    ACCESS_LEVELS,
    CANVAS_TOOL_NAMES,
    MAX_CONTENT_CHARS,
    MAX_MARKDOWN_CHARS,
    SECTION_TYPES,
    SlackCanvasToolkit,
    slack_canvas_request_metadata,
)
from jiuwenswarm.common.slack_history_policy import (
    METADATA_ORIGIN_KEY,
    METADATA_TEAM_KEY,
    ORIGIN_CRON_JOB,
)

_CANVAS = "F1234ABCD"
_CHAT = "C000000AAAA"
_DM = "D000000AAAA"
_ALICE = "U000000AAAA"
_BOB = "U000000BBBB"
_CAROL = "U000000CCCC"
_HOST = "https://acme.slack.com"
_URL = f"{_HOST}/docs/T000/{_CANVAS}"
_HTML = (
    '<div class="quip-canvas-content">'
    '<h1 id="temp:C:Cbc9">Runbook</h1>'
    '<p id="temp:C:Cbd0">Restart the worker.</p>'
    "</div>"
)


class _FakeResponse:
    """What the SDK hangs off a refusal: the body, and nothing else."""

    def __init__(self, error: str, detail: str = "") -> None:
        self.status_code = 200
        self.data: dict[str, Any] = {"ok": False, "error": error}
        if detail:
            self.data["detail"] = detail
        self.headers: dict[str, Any] = {}


class _FakeSlackError(Exception):
    def __init__(self, error: str, detail: str = "") -> None:
        super().__init__("sanitized fake failure")
        self.response = _FakeResponse(error, detail)


class _Body:
    """An httpx-shaped response that yields one canvas document."""

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
    """An httpx client that serves one document and records what was asked for."""

    def __init__(self, body: _Body) -> None:
        self._body = body
        self.requests: list[tuple[str, dict[str, str]]] = []

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_: object) -> bool:
        return False

    def stream(self, _method: str, url: str, headers: Any = None) -> _Stream:
        self.requests.append((url, dict(headers or {})))
        return self._stream()

    def _stream(self) -> _Stream:
        return _Stream(self._body)


class _Workspace:
    """A fake Slack that records every call and can refuse chosen ones."""

    def __init__(
        self,
        *,
        fail: Any = None,
        file_record: Any = None,
        sections: Any = None,
    ) -> None:
        #: method -> (error, detail), or a callable taking (method, payload).
        self.fail = fail
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.file_record = (
            {"filetype": "canvas", "title": "Runbook", "url_private": _URL}
            if file_record is None
            else file_record
        )
        self.sections = (
            [{"id": "temp:C:Cbc9"}, {"id": "temp:C:Cbd0"}]
            if sections is None
            else sections
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
        if method == "canvases.sections.lookup":
            return {"ok": True, "sections": list(self.sections)}
        return {"ok": True, "canvas_id": _CANVAS}

    async def files_info(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("files_info", dict(kwargs)))
        self._maybe_fail("files_info", dict(kwargs))
        return {"ok": True, "file": dict(self.file_record)}

    async def auth_test(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("auth_test", dict(kwargs)))
        return {"ok": True, "url": f"{_HOST}/", "user_id": "U0BOT"}

    def payload(self, method: str) -> dict[str, Any]:
        for name, payload in self.calls:
            if name == method:
                return payload
        raise AssertionError(f"{method} was never called: {self.calls}")

    def payloads(self, method: str) -> list[dict[str, Any]]:
        return [payload for name, payload in self.calls if name == method]


@pytest.fixture(autouse=True)
def _config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        config_module,
        "get_config",
        lambda: {"channels": {"slack": {"bot_token": "xoxb-config-secret"}}},
    )


def _toolkit(
    workspace: _Workspace, http: Any = None, **metadata: Any
) -> SlackCanvasToolkit:
    base: dict[str, Any] = {METADATA_TEAM_KEY: "T000", "slack_channel_id": _CHAT}
    base.update(metadata)
    return SlackCanvasToolkit(
        metadata=base,
        client=workspace,
        http_client_factory=(lambda: http) if http is not None else None,
    )


def _run(coro: Any) -> dict[str, Any]:
    return json.loads(asyncio.run(coro))


def _cards(toolkit: SlackCanvasToolkit) -> dict[str, Any]:
    cards: dict[str, Any] = {}
    for tool in toolkit.get_tools():
        card = getattr(tool, "card", None) or tool._card
        cards[card.name] = card
    return cards


def _all_cards() -> dict[str, Any]:
    return _cards(_toolkit(_Workspace()))


# ── 1. reading ───────────────────────────────────────────────────────────────


def test_a_read_returns_the_html_and_the_section_ids() -> None:
    workspace = _Workspace()
    http = _Http(_Body(_HTML.encode()))

    result = _run(
        _toolkit(workspace, http).read_slack_canvas(
            canvas_id=_CANVAS, section_types=["any_header"]
        )
    )

    assert result["ok"] is True
    assert result["canvas_id"] == _CANVAS
    assert result["content_format"] == "html"
    assert result["content_html"] == _HTML
    assert result["sections"] == [
        {"section_id": "temp:C:Cbc9"},
        {"section_id": "temp:C:Cbd0"},
    ]


def test_the_download_carries_the_bot_token_and_nothing_else() -> None:
    """The content is unreachable without it: an unauthenticated GET of a
    canvas URL is answered with Slack's sign-in page rather than the document.
    """
    workspace = _Workspace()
    http = _Http(_Body(_HTML.encode()))

    _run(_toolkit(workspace, http).read_slack_canvas(canvas_id=_CANVAS))

    (url, headers), = http.requests
    assert url == _URL
    assert headers == {"Authorization": "Bearer xoxb-config-secret"}


def test_a_read_naming_no_filter_asks_for_no_sections_and_keeps_the_content() -> None:
    """``criteria`` is a required argument of the lookup and Slack answers an
    empty one with ``invalid_arguments``, so there is no call to make. The
    sections are an optional half of this tool and the content is the other, so
    a caller who named no filter gets the canvas rather than a refusal.
    """
    workspace = _Workspace()
    http = _Http(_Body(_HTML.encode()))

    result = _run(_toolkit(workspace, http).read_slack_canvas(canvas_id=_CANVAS))

    assert result["ok"] is True
    assert result["content_html"] == _HTML
    assert "sections" not in result
    assert workspace.payloads("canvases.sections.lookup") == []
    assert len(http.requests) == 1
    assert result["sections_filter"] == "none"


def test_no_filter_and_a_failed_lookup_are_different_words() -> None:
    """One is this caller's choice and the other is a failure somebody may be
    able to fix, so a model has to be able to tell them apart.
    """
    unasked = _run(
        _toolkit(_Workspace(), _Http(_Body(_HTML.encode()))).read_slack_canvas(
            canvas_id=_CANVAS
        )
    )
    refused = _run(
        _toolkit(
            _Workspace(fail={"canvases.sections.lookup": "missing_scope"}),
            _Http(_Body(_HTML.encode())),
        ).read_slack_canvas(canvas_id=_CANVAS, section_types=["any_header"])
    )

    assert any(
        word.startswith("sections_not_requested:") for word in unasked["coverage"]
    )
    assert not any(
        word.startswith("sections_unavailable:") for word in unasked["coverage"]
    )
    assert any(
        word.startswith("sections_unavailable:") for word in refused["coverage"]
    )


def test_contains_text_alone_is_a_filter_and_reaches_slack() -> None:
    """Slack documents the query with no section type as the way to discover
    the section types it will not filter on, so that query has to stay
    available: it drops section_types and keeps contains_text.
    """
    workspace = _Workspace()

    result = _run(
        _toolkit(workspace, _Http(_Body(_HTML.encode()))).read_slack_canvas(
            canvas_id=_CANVAS, contains_text="Runbook"
        )
    )

    assert result["ok"] is True
    assert workspace.payload("canvases.sections.lookup")["criteria"] == {
        "contains_text": "Runbook"
    }


def test_section_types_alone_is_a_filter_and_reaches_slack() -> None:
    workspace = _Workspace()

    result = _run(
        _toolkit(workspace, _Http(_Body(_HTML.encode()))).read_slack_canvas(
            canvas_id=_CANVAS, section_types=["h1"]
        )
    )

    assert result["ok"] is True
    assert workspace.payload("canvases.sections.lookup")["criteria"] == {
        "section_types": ["h1"]
    }


def test_both_criteria_travel_when_both_are_given() -> None:
    workspace = _Workspace()

    _run(
        _toolkit(workspace, _Http(_Body(_HTML.encode()))).read_slack_canvas(
            canvas_id=_CANVAS,
            section_types=["h1", "table"],
            contains_text="CAN Report",
        )
    )

    assert workspace.payload("canvases.sections.lookup")["criteria"] == {
        "section_types": ["h1", "table"],
        "contains_text": "CAN Report",
    }


def test_an_unknown_section_type_never_reaches_slack() -> None:
    workspace = _Workspace()

    result = _run(
        _toolkit(workspace).read_slack_canvas(
            canvas_id=_CANVAS, section_types=["heading"]
        )
    )

    assert result["error"] == "section_type_unknown"
    assert workspace.calls == []


def test_the_nineteen_filterable_section_types_are_offered_as_an_enum() -> None:
    card = _all_cards()["read_slack_canvas"]
    enum = card.input_params["properties"]["section_types"]["items"]["enum"]

    assert len(SECTION_TYPES) == 19
    assert enum == list(SECTION_TYPES)


def test_a_canvas_too_long_to_return_whole_says_so_rather_than_cutting_quietly() -> None:
    workspace = _Workspace()
    body = ("<p>x</p>" * (MAX_CONTENT_CHARS // 4)).encode()
    http = _Http(_Body(body))

    result = _run(_toolkit(workspace, http).read_slack_canvas(canvas_id=_CANVAS))

    assert result["content_truncated"] is True
    assert result["content_chars"] == MAX_CONTENT_CHARS
    assert result["content_chars_total"] > MAX_CONTENT_CHARS
    assert any(word.startswith("content_truncated:") for word in result["coverage"])


def test_a_missing_read_scope_loses_the_sections_and_keeps_the_content() -> None:
    """The two halves need different grants -- canvases:read for the sections,
    files:read for the document -- so an install holding one and not the other
    gets the half it paid for and a note naming what it did not.
    """
    workspace = _Workspace(fail={"canvases.sections.lookup": "missing_scope"})
    http = _Http(_Body(_HTML.encode()))

    result = _run(
        _toolkit(workspace, http).read_slack_canvas(
            canvas_id=_CANVAS, section_types=["any_header"]
        )
    )

    assert result["ok"] is True
    assert result["content_html"] == _HTML
    assert "sections" not in result
    note = "".join(result["coverage"])
    assert "sections_unavailable" in note
    assert "canvases:read" in note


def test_an_unfetchable_document_keeps_the_sections_and_names_the_gap() -> None:
    workspace = _Workspace(file_record={"filetype": "canvas", "url_private": ""})

    result = _run(
        _toolkit(workspace).read_slack_canvas(
            canvas_id=_CANVAS, section_types=["any_header"]
        )
    )

    assert result["ok"] is True
    assert "content_html" not in result
    assert any(
        word.startswith("content_unavailable:") for word in result["coverage"]
    )
    assert result["sections_count"] == 2


def test_a_download_from_a_host_slack_did_not_name_is_refused() -> None:
    """The allow-list is the one thing standing between a file record and a
    live workspace credential going wherever whoever wrote the record chose.
    """
    workspace = _Workspace(
        file_record={
            "filetype": "canvas",
            "url_private": "https://evil.example.com/docs/T/F",
        }
    )

    result = _run(_toolkit(workspace, _Http(_Body(b""))).read_slack_canvas(
        canvas_id=_CANVAS
    ))

    assert result["ok"] is True
    assert "content_html" not in result
    assert "canvas_host_not_allowed" in "".join(result["coverage"])


def test_a_file_that_is_not_a_canvas_is_refused_by_name() -> None:
    workspace = _Workspace(file_record={"filetype": "pdf", "url_private": _URL})

    result = _run(_toolkit(workspace).read_slack_canvas(canvas_id=_CANVAS))

    assert result["error"] == "not_a_canvas"
    assert "pdf" in result["detail"]


# ── 2. canvas_not_found names all three states ───────────────────────────────


@pytest.mark.parametrize(
    "call",
    [
        "read",
        "edit",
        "delete",
        "share",
    ],
)
def test_canvas_not_found_names_every_state_it_conflates(call: str) -> None:
    """Absent, deleted, and never shared with this app. Only the third is
    fixable, and only by a person, so picking one of the three to report would
    send somebody after a fix that cannot work.
    """
    workspace = _Workspace(fail="canvas_not_found")
    toolkit = _toolkit(workspace)
    calls = {
        "read": lambda: toolkit.read_slack_canvas(canvas_id=_CANVAS),
        "edit": lambda: toolkit.edit_slack_canvas(
            canvas_id=_CANVAS,
            changes=[{"operation": "insert_at_end", "markdown": "x"}],
        ),
        "delete": lambda: toolkit.delete_slack_canvas(canvas_id=_CANVAS),
        "share": lambda: toolkit.share_slack_canvas(
            canvas_id=_CANVAS, access_level="read", user_ids=[_ALICE]
        ),
    }

    result = _run(calls[call]())

    assert result["error"] == "canvas_not_found"
    detail = result["detail"]
    assert "does not exist" in detail
    assert "deleted" in detail
    assert "never been shared with this app" in detail


# ── 3. creating, and why it is two tools ─────────────────────────────────────


def test_the_two_creations_are_two_tools_with_two_names() -> None:
    names = set(_all_cards())

    assert "write_slack_canvas" in names
    assert "write_slack_channel_canvas" in names
    assert set(CANVAS_TOOL_NAMES) == names


def test_a_standalone_canvas_is_created_with_slacks_content_wrapper() -> None:
    workspace = _Workspace()

    result = _run(
        _toolkit(workspace).write_slack_canvas(title="Runbook", markdown="# Hi")
    )

    assert result == {
        "ok": True,
        "canvas_id": _CANVAS,
        "is_channel_canvas": False,
    }
    assert workspace.payload("canvases.create") == {
        "title": "Runbook",
        "document_content": {"type": "markdown", "markdown": "# Hi"},
    }


def test_a_chat_id_tabs_a_standalone_canvas_into_a_conversation() -> None:
    workspace = _Workspace()

    result = _run(
        _toolkit(workspace).write_slack_canvas(markdown="hi", chat_id=_CHAT)
    )

    assert result["chat_id"] == _CHAT
    assert workspace.payload("canvases.create")["channel_id"] == _CHAT


def test_a_channel_canvas_goes_to_the_other_method_entirely() -> None:
    workspace = _Workspace()

    result = _run(
        _toolkit(workspace).write_slack_channel_canvas(
            chat_id=_CHAT, markdown="hi"
        )
    )

    assert result["is_channel_canvas"] is True
    assert workspace.payload("conversations.canvases.create")["channel_id"] == _CHAT
    assert workspace.payloads("canvases.create") == []


def test_a_channel_canvas_needs_a_conversation() -> None:
    workspace = _Workspace()

    result = _run(_toolkit(workspace).write_slack_channel_canvas(chat_id=""))

    assert result["error"] == "chat_id_required"
    assert workspace.calls == []


@pytest.mark.parametrize("tool", ["standalone", "channel"])
def test_a_direct_message_holds_no_canvas_and_is_refused_before_the_call(
    tool: str,
) -> None:
    workspace = _Workspace()
    toolkit = _toolkit(workspace)
    call = (
        toolkit.write_slack_canvas(markdown="hi", chat_id=_DM)
        if tool == "standalone"
        else toolkit.write_slack_channel_canvas(chat_id=_DM)
    )

    result = _run(call)

    assert result["error"] == "chat_id_is_a_direct_message"
    assert workspace.calls == []


def test_a_conversation_already_holding_its_canvas_is_told_there_is_no_second() -> None:
    """It used to be told there was no way to delete the one it has, which was
    inferred from this very code rather than documented, and is false.
    """
    workspace = _Workspace(fail="channel_canvas_already_exists")

    result = _run(
        _toolkit(workspace).write_slack_channel_canvas(chat_id=_CHAT)
    )

    assert result["error"] == "channel_canvas_already_exists"
    assert "one at a time" in result["detail"]
    assert "edit the existing canvas" in result["detail"]
    assert "no way to delete this one" not in result["detail"]


def test_the_free_plan_refusal_names_the_argument_that_fixes_it() -> None:
    """``channel_id`` is optional on a paid plan and required on a free one, so
    the code alone leaves a caller with nothing to change.
    """
    workspace = _Workspace(fail="free_teams_cannot_create_standalone_canvases")

    result = _run(_toolkit(workspace).write_slack_canvas(markdown="hi"))

    assert result["error"] == "free_teams_cannot_create_standalone_canvases"
    assert "Pass chat_id" in result["detail"]


@pytest.mark.parametrize(
    "code",
    [
        "free_teams_cannot_edit_standalone_canvases",
        "free_team_canvas_tab_already_exists",
        "canvas_disabled_user_team",
        "canvas_globally_disabled",
        "restricted_action",
    ],
)
def test_every_plan_and_kill_switch_refusal_is_explained(code: str) -> None:
    workspace = _Workspace(fail=code)

    result = _run(_toolkit(workspace).write_slack_canvas(markdown="hi"))

    assert result["error"] == code
    assert result["detail"]
    assert f"Slack refused {code}" not in result["detail"]


def test_content_over_slacks_ceiling_never_reaches_slack() -> None:
    workspace = _Workspace()

    result = _run(
        _toolkit(workspace).write_slack_canvas(
            markdown="x" * (MAX_MARKDOWN_CHARS + 1)
        )
    )

    assert result["error"] == "content_too_large"
    assert str(MAX_MARKDOWN_CHARS) in result["detail"]
    assert workspace.calls == []


# ── 4. editing ───────────────────────────────────────────────────────────────


def test_one_change_is_sent_as_slacks_change_object() -> None:
    workspace = _Workspace()

    result = _run(
        _toolkit(workspace).edit_slack_canvas(
            canvas_id=_CANVAS,
            changes=[
                {
                    "operation": "insert_after",
                    "section_id": "temp:C:Cbc9",
                    "markdown": "more",
                }
            ],
        )
    )

    assert result["ok"] is True
    assert workspace.payload("canvases.edit") == {
        "canvas_id": _CANVAS,
        "changes": [
            {
                "operation": "insert_after",
                "section_id": "temp:C:Cbc9",
                "document_content": {"type": "markdown", "markdown": "more"},
            }
        ],
    }


def test_rename_writes_the_title_wrapper_rather_than_the_document_one() -> None:
    workspace = _Workspace()

    _run(
        _toolkit(workspace).edit_slack_canvas(
            canvas_id=_CANVAS,
            changes=[{"operation": "rename", "title": "New title"}],
        )
    )

    change = workspace.payload("canvases.edit")["changes"][0]
    assert change["title_content"] == {"type": "markdown", "markdown": "New title"}
    assert "document_content" not in change


def test_a_title_is_named_the_same_way_on_every_canvas_tool() -> None:
    """Creating a canvas and renaming it to the same string take the same
    argument name. Slack's two wire shapes are wrapped inside the tool, so the
    difference between them never reaches a caller.
    """
    cards = _all_cards()

    for name in ("write_slack_canvas", "write_slack_channel_canvas"):
        assert "title" in cards[name].input_params["properties"]

    change = cards["edit_slack_canvas"].input_params["properties"]["changes"]
    assert "title" in change["items"]["properties"]
    assert "title_markdown" not in change["items"]["properties"]


def test_a_rename_without_a_title_names_the_argument_it_wanted() -> None:
    workspace = _Workspace()

    result = _run(
        _toolkit(workspace).edit_slack_canvas(
            canvas_id=_CANVAS, changes=[{"operation": "rename"}]
        )
    )

    assert result["error"] == "change_content_required"
    assert "Pass it as title" in result["detail"]
    assert workspace.calls == []


def test_a_rename_to_whitespace_is_refused_like_a_rename_to_nothing() -> None:
    """Spaces do not name a canvas any more than an empty string does.

    The create tools strip a title and the rename path did not, so a title of
    spaces was dropped on one and sent as the canvas's name on the other. That
    was the one input the two disagreed on by accident. A blank title is the
    one they disagree on deliberately -- omitted on create asks Slack for an
    untitled canvas, and there is no rename that takes a title off -- and that
    difference is stated on both cards rather than removed.
    """
    workspace = _Workspace()

    result = _run(
        _toolkit(workspace).edit_slack_canvas(
            canvas_id=_CANVAS, changes=[{"operation": "rename", "title": "   "}]
        )
    )

    assert result["error"] == "change_content_required"
    assert workspace.calls == []


def test_a_rename_title_keeps_its_inner_spacing() -> None:
    """Only the ends are trimmed. A title is one line and the trim is about
    whether it names anything, not about what it is allowed to say.
    """
    workspace = _Workspace()

    _run(
        _toolkit(workspace).edit_slack_canvas(
            canvas_id=_CANVAS,
            changes=[{"operation": "rename", "title": "  Q3  plan  "}],
        )
    )

    change = workspace.payload("canvases.edit")["changes"][0]
    assert change["title_content"]["markdown"] == "Q3  plan"


def test_document_content_is_not_stripped_with_the_rename_title() -> None:
    """Trimming a body would edit it.

    Leading spaces open a code block and a trailing newline closes a list, so
    the strip is scoped to the rename title and every content operation still
    sends what it was given.
    """
    workspace = _Workspace()

    _run(
        _toolkit(workspace).edit_slack_canvas(
            canvas_id=_CANVAS,
            changes=[
                {
                    "operation": "insert_at_end",
                    "markdown": "    indented code\n",
                }
            ],
        )
    )

    change = workspace.payload("canvases.edit")["changes"][0]
    assert change["document_content"]["markdown"] == "    indented code\n"


def test_a_blank_title_on_create_asks_for_an_untitled_canvas() -> None:
    """Dropped rather than sent, and that is the documented request.

    Slack: "The canvas will be created untitled and empty if none of the
    optional parameters are specified." Passing title="" through would be
    asking for something the method does not document, where omitting it asks
    for something it does.
    """
    workspace = _Workspace()

    _run(_toolkit(workspace).write_slack_canvas(title="   ", markdown="body"))

    assert "title" not in workspace.payload("canvases.create")


def test_more_than_one_change_is_refused_in_slacks_own_words() -> None:
    """The array stays an array. When Slack lifts the limit this check is the
    only thing to delete, rather than an argument shape to redesign.
    """
    workspace = _Workspace()

    result = _run(
        _toolkit(workspace).edit_slack_canvas(
            canvas_id=_CANVAS,
            changes=[
                {"operation": "insert_at_end", "markdown": "a"},
                {"operation": "insert_at_end", "markdown": "b"},
            ],
        )
    )

    assert result["error"] == "too_many_changes"
    assert (
        "Only one operation per API call is currently supported"
        in result["detail"]
    )
    assert workspace.calls == []


def test_an_empty_change_list_leaves_the_canvas_alone() -> None:
    workspace = _Workspace()

    result = _run(
        _toolkit(workspace).edit_slack_canvas(canvas_id=_CANVAS, changes=[])
    )

    assert result["error"] == "changes_required"
    assert workspace.calls == []


@pytest.mark.parametrize(
    ("change", "code"),
    [
        ({"operation": "sprinkle", "markdown": "x"}, "operation_unknown"),
        ({"operation": "insert_after", "markdown": "x"}, "section_id_required"),
        ({"operation": "delete"}, "section_id_required"),
        (
            {"operation": "insert_at_end", "section_id": "s", "markdown": "x"},
            "section_id_not_accepted",
        ),
        ({"operation": "insert_at_end"}, "change_content_required"),
        ({"operation": "rename"}, "change_content_required"),
        ("not an object", "change_malformed"),
    ],
)
def test_a_change_slack_would_decline_is_declined_before_the_write(
    change: Any, code: str
) -> None:
    workspace = _Workspace()

    result = _run(
        _toolkit(workspace).edit_slack_canvas(canvas_id=_CANVAS, changes=[change])
    )

    assert result["error"] == code
    assert workspace.calls == []


def test_replace_without_a_section_id_is_allowed_and_documented() -> None:
    """It replaces the whole canvas, which is the one destructive outcome an
    omitted argument can reach here. It is not refused -- replacing everything
    is a real request -- so the card carries the warning instead.
    """
    workspace = _Workspace()

    result = _run(
        _toolkit(workspace).edit_slack_canvas(
            canvas_id=_CANVAS,
            changes=[{"operation": "replace", "markdown": "everything"}],
        )
    )

    assert result["ok"] is True
    assert "section_id" not in workspace.payload("canvases.edit")["changes"][0]
    description = _all_cards()["edit_slack_canvas"].description
    assert "replaces the entire canvas" in description


def test_slacks_own_parse_error_is_passed_through_rather_than_flattened() -> None:
    """The line number and the construct are the whole of what a caller can
    act on, and neither is in the code.
    """
    detail = "'content' error: line 28: Unsupported block type (List) within block quote"
    workspace = _Workspace(fail=("invalid_arguments", detail))

    result = _run(
        _toolkit(workspace).edit_slack_canvas(
            canvas_id=_CANVAS,
            changes=[{"operation": "insert_at_end", "markdown": "> - a"}],
        )
    )

    assert result["error"] == "invalid_arguments"
    assert detail in result["detail"]


def test_a_locked_canvas_is_told_that_retrying_is_the_remedy() -> None:
    """The one refusal here a retry fixes. Slack rejects a concurrent edit
    rather than queueing it, so saying nothing would leave a caller treating a
    transient collision as a permanent failure.
    """
    workspace = _Workspace(fail="canvas_editing_locked")

    result = _run(
        _toolkit(workspace).edit_slack_canvas(
            canvas_id=_CANVAS,
            changes=[{"operation": "insert_at_end", "markdown": "x"}],
        )
    )

    assert result["error"] == "canvas_editing_locked"
    assert "retry" in result["detail"]
    assert "nothing changed" in result["detail"]


def test_a_canvas_too_large_refusal_says_what_has_to_shrink() -> None:
    workspace = _Workspace(fail="canvas_too_large")

    result = _run(
        _toolkit(workspace).edit_slack_canvas(
            canvas_id=_CANVAS,
            changes=[{"operation": "insert_at_end", "markdown": "x"}],
        )
    )

    assert result["error"] == "canvas_too_large"
    assert "smaller" in result["detail"]


# ── 5. sharing ───────────────────────────────────────────────────────────────


def test_a_grant_names_its_targets_as_an_array() -> None:
    workspace = _Workspace()

    result = _run(
        _toolkit(workspace).share_slack_canvas(
            canvas_id=_CANVAS, access_level="read", user_ids=[_ALICE, _BOB]
        )
    )

    assert result["ok"] is True
    assert result["granted"] == [_ALICE, _BOB]
    assert workspace.payload("canvases.access.set") == {
        "canvas_id": _CANVAS,
        "user_ids": [_ALICE, _BOB],
        "access_level": "read",
    }


def test_remove_goes_to_the_other_method_and_carries_no_level() -> None:
    workspace = _Workspace()

    result = _run(
        _toolkit(workspace).share_slack_canvas(
            canvas_id=_CANVAS, chat_ids=[_CHAT], remove=True
        )
    )

    assert result["access_level"] == "none"
    assert workspace.payload("canvases.access.delete") == {
        "canvas_id": _CANVAS,
        "channel_ids": [_CHAT],
    }
    assert workspace.payloads("canvases.access.set") == []


def test_a_conversation_target_reaches_slack_under_slacks_own_name() -> None:
    """The public argument is ``chat_ids`` and Slack's is ``channel_ids``. An
    argument Slack does not define is not refused, it is ignored: the call then
    names no target at all and comes back ``invalid_parameters``, which reads
    as a complaint about the ids. Asserting the tool's own arguments cannot
    catch that, so this asserts what left the process.
    """
    workspace = _Workspace()

    result = _run(
        _toolkit(workspace).share_slack_canvas(
            canvas_id=_CANVAS, access_level="read", chat_ids=[_CHAT]
        )
    )

    assert result["ok"] is True
    payload = workspace.payload("canvases.access.set")
    assert payload["channel_ids"] == [_CHAT]
    assert "chat_ids" not in payload
    assert result["granted"] == [_CHAT]


def test_neither_target_is_refused_and_so_is_both() -> None:
    workspace = _Workspace()
    toolkit = _toolkit(workspace)

    neither = _run(
        toolkit.share_slack_canvas(canvas_id=_CANVAS, access_level="read")
    )
    both = _run(
        toolkit.share_slack_canvas(
            canvas_id=_CANVAS,
            access_level="read",
            chat_ids=[_CHAT],
            user_ids=[_ALICE],
        )
    )

    assert neither["error"] == "access_targets_required"
    assert both["error"] == "access_targets_ambiguous"
    assert workspace.calls == []


def test_owner_cannot_be_given_to_a_conversation() -> None:
    workspace = _Workspace()

    result = _run(
        _toolkit(workspace).share_slack_canvas(
            canvas_id=_CANVAS, access_level="owner", chat_ids=[_CHAT]
        )
    )

    assert result["error"] == "owner_must_be_a_person"
    assert workspace.calls == []


def test_owner_named_for_several_people_is_refused_on_its_arity() -> None:
    """Slack does not document refusing this and it is refused here anyway: a
    canvas has one owner, so the outcomes a multi-owner call could have are not
    ones a caller could tell apart from the response.
    """
    workspace = _Workspace()

    result = _run(
        _toolkit(workspace).share_slack_canvas(
            canvas_id=_CANVAS, access_level="owner", user_ids=[_ALICE, _BOB]
        )
    )

    assert result["error"] == "owner_is_one_person"
    assert "2 were named" in result["detail"]
    assert workspace.calls == []


def test_a_direct_message_is_not_a_share_target() -> None:
    workspace = _Workspace()

    result = _run(
        _toolkit(workspace).share_slack_canvas(
            canvas_id=_CANVAS, access_level="read", chat_ids=[_CHAT, _DM]
        )
    )

    assert result["error"] == "chat_id_is_a_direct_message"
    assert workspace.calls == []


def test_an_unknown_access_level_never_reaches_slack() -> None:
    workspace = _Workspace()

    result = _run(
        _toolkit(workspace).share_slack_canvas(
            canvas_id=_CANVAS, access_level="admin", user_ids=[_ALICE]
        )
    )

    assert result["error"] == "access_level_unknown"
    assert ", ".join(ACCESS_LEVELS) in result["detail"]


def test_remove_and_a_level_together_are_refused() -> None:
    workspace = _Workspace()

    result = _run(
        _toolkit(workspace).share_slack_canvas(
            canvas_id=_CANVAS,
            access_level="read",
            user_ids=[_ALICE],
            remove=True,
        )
    )

    assert result["error"] == "access_level_not_accepted"
    assert workspace.calls == []


def test_a_multi_target_grant_that_half_succeeds_reports_each_target() -> None:
    """Slack refuses the whole call with one code and names no ids, so a single
    boolean would be a claim nobody checked. The outcome is established per
    target instead.
    """

    def fail(method: str, payload: dict[str, Any]) -> Any:
        if method != "canvases.access.set":
            return None
        people = payload.get("user_ids") or []
        if len(people) > 1:
            return "failed_to_update_user_ids"
        return "user_not_found" if people == [_BOB] else None

    workspace = _Workspace(fail=fail)

    result = _run(
        _toolkit(workspace).share_slack_canvas(
            canvas_id=_CANVAS,
            access_level="read",
            user_ids=[_ALICE, _BOB, _CAROL],
        )
    )

    assert result["ok"] is False
    assert result["error"] == "access_partially_applied"
    assert result["granted"] == [_ALICE, _CAROL]
    assert result["failed"] == [{"id": _BOB, "error": "user_not_found"}]
    assert "undetermined" not in result
    # One call for the whole set, then one per target to find out which.
    assert len(workspace.payloads("canvases.access.set")) == 4


def test_a_single_target_failure_is_not_retried_target_by_target() -> None:
    """With one target the code already answers the question, so the per-target
    pass would be a second call that learns nothing.
    """
    workspace = _Workspace(fail="user_not_found")

    result = _run(
        _toolkit(workspace).share_slack_canvas(
            canvas_id=_CANVAS, access_level="read", user_ids=[_ALICE]
        )
    )

    assert result["error"] == "user_not_found"
    assert len(workspace.payloads("canvases.access.set")) == 1


def test_a_refused_user_grant_names_the_step_no_canvas_method_performs() -> None:
    """``canvases.access.set`` sets a level on a canvas somebody already has.
    Sending it to them is a message carrying the permalink, and nothing here
    posts one -- so the refusal has to name the missing move or a model will
    call this again with different arguments.
    """
    workspace = _Workspace(fail="failed_to_update_user_ids")

    result = _run(
        _toolkit(workspace).share_slack_canvas(
            canvas_id=_CANVAS, access_level="read", user_ids=[_ALICE]
        )
    )

    assert "sent to them directly" in result["next_step"]
    assert "does not count" in result["next_step"]


def test_a_channel_canvas_has_no_access_to_set_and_the_refusal_says_so() -> None:
    """Its access follows channel membership, and Slack answers an attempt with
    ``canvas_not_found`` -- a fourth meaning for a code that already had three.
    """
    workspace = _Workspace(fail="canvas_not_found")

    result = _run(
        _toolkit(workspace).share_slack_canvas(
            canvas_id=_CANVAS, access_level="read", chat_ids=[_CHAT]
        )
    )

    assert "follows from who is in that conversation" in result["next_step"]


def test_the_share_tool_posts_nothing() -> None:
    """The whole of the precondition argument, asserted rather than described:
    a sharing tool that posted would be writing into a conversation without
    passing the ladder that governs posting.
    """
    workspace = _Workspace(fail="failed_to_update_user_ids")

    _run(
        _toolkit(workspace).share_slack_canvas(
            canvas_id=_CANVAS, access_level="read", user_ids=[_ALICE]
        )
    )

    assert [name for name, _ in workspace.calls if "chat" in name] == []


# ── 6. deleting ──────────────────────────────────────────────────────────────


def test_a_delete_reports_the_canvas_it_deleted() -> None:
    workspace = _Workspace()

    result = _run(_toolkit(workspace).delete_slack_canvas(canvas_id=_CANVAS))

    assert result == {"ok": True, "canvas_id": _CANVAS, "deleted": True}
    assert workspace.payload("canvases.delete") == {"canvas_id": _CANVAS}


def test_deleting_switched_off_for_a_workspace_is_explained() -> None:
    workspace = _Workspace(fail="canvas_deleting_disabled")

    result = _run(_toolkit(workspace).delete_slack_canvas(canvas_id=_CANVAS))

    assert result["error"] == "canvas_deleting_disabled"
    assert "untouched" in result["detail"]


def test_a_refusal_claims_nothing_about_what_the_canvas_holds() -> None:
    """A call Slack refused establishes nothing about the document. A
    ``deleted: false`` would read as a claim about the canvas rather than about
    this call.
    """
    result = _run(
        _toolkit(_Workspace(fail="canvas_deleting_disabled")).delete_slack_canvas(
            canvas_id=_CANVAS
        )
    )

    assert "deleted" not in result


# ── 7. arguments that never reach Slack ──────────────────────────────────────


@pytest.mark.parametrize("bad", ["", "   ", "C1234ABCD", "f1234abcd", "../etc"])
def test_a_canvas_id_that_is_not_one_is_refused_before_any_call(bad: str) -> None:
    workspace = _Workspace()

    result = _run(_toolkit(workspace).delete_slack_canvas(canvas_id=bad))

    assert result["error"] in {"canvas_id_required", "canvas_id_malformed"}
    assert workspace.calls == []


def test_a_bare_string_is_not_silently_read_as_a_one_element_array() -> None:
    """Accepting the shorthand would make the ``owner`` arity check unreachable
    for exactly the argument it exists to guard.
    """
    workspace = _Workspace()

    result = _run(
        _toolkit(workspace).share_slack_canvas(
            canvas_id=_CANVAS, access_level="read", user_ids=_ALICE
        )
    )

    assert result["error"] == "access_targets_malformed"
    assert workspace.calls == []


def test_changes_that_is_not_a_list_is_refused() -> None:
    workspace = _Workspace()

    result = _run(
        _toolkit(workspace).edit_slack_canvas(
            canvas_id=_CANVAS, changes={"operation": "insert_at_end"}
        )
    )

    assert result["error"] == "changes_malformed"
    assert workspace.calls == []


# ── 8. which requests may touch a canvas at all ──────────────────────────────


def test_an_inbound_slack_turn_may_touch_a_canvas() -> None:
    metadata = {METADATA_TEAM_KEY: "T000"}

    assert slack_canvas_request_metadata("slack", metadata) == metadata


def test_a_marked_scheduled_run_may_touch_a_canvas() -> None:
    """A canvas is the surface a recurring report belongs on -- edited in place
    rather than reposted -- so excluding cron would take the capability away
    from the case it fits best.
    """
    metadata = {METADATA_ORIGIN_KEY: ORIGIN_CRON_JOB, METADATA_TEAM_KEY: "T000"}

    assert slack_canvas_request_metadata("__cron__", metadata) == metadata


def test_the_provider_fails_closed_on_every_wrong_request_shape() -> None:
    for channel_id, metadata in (
        ("web", {METADATA_TEAM_KEY: "T000"}),
        (None, {METADATA_TEAM_KEY: "T000"}),
        ("", {METADATA_TEAM_KEY: "T000"}),
        ("slack", None),
        ("slack", "not a mapping"),
        ("__cron__", {METADATA_TEAM_KEY: "T000"}),
        ("__cron__", None),
    ):
        assert slack_canvas_request_metadata(channel_id, metadata) == {}, (
            channel_id,
            metadata,
        )


def test_a_failing_provider_touches_no_canvas() -> None:
    def _boom() -> dict[str, Any]:
        raise RuntimeError("the worker boundary is gone")

    workspace = _Workspace()
    toolkit = SlackCanvasToolkit(metadata_provider=_boom, client=workspace)

    result = _run(toolkit.delete_slack_canvas(canvas_id=_CANVAS))

    assert result["error"] == "trusted_slack_request_required"
    assert workspace.calls == []


# ── 9. the cards ─────────────────────────────────────────────────────────────


def test_the_read_card_says_a_read_modify_write_is_not_available() -> None:
    """Without it a model would read HTML, edit the HTML, and write that back
    as markdown -- which Slack accepts and which produces a mangled document.
    """
    description = _all_cards()["read_slack_canvas"].description

    assert "comes back as HTML" in description
    assert "written as markdown" in description
    assert "cannot read a canvas, change the text you got back" in description


def test_the_read_card_says_the_html_carries_the_section_ids() -> None:
    """The one thing that survives the conversion, and the reason a reader does
    not need a second call to map content to section.
    """
    description = _all_cards()["read_slack_canvas"].description

    assert "id attribute" in description


def test_the_read_card_says_the_sections_have_to_be_asked_for() -> None:
    """Both halves are load-bearing. A model that expected the sections
    unasked-for would read a filterless result as a canvas that has none; one
    that thought section_types was compulsory could never reach the kinds Slack
    will not filter on.
    """
    description = _all_cards()["read_slack_canvas"].description

    assert "only when a filter asks for it" in description
    assert "Leaving section_types out while passing contains_text" in description


def test_the_two_creation_cards_each_name_the_other() -> None:
    """They make different objects out of the same-looking request and nothing
    converts between them, so a model that picked the wrong one has made
    something permanent.
    """
    cards = _all_cards()

    assert "write_slack_channel_canvas" in cards["write_slack_canvas"].description
    assert "write_slack_canvas" in cards["write_slack_channel_canvas"].description


def test_the_channel_canvas_card_says_a_channel_holds_one_at_a_time() -> None:
    """It used to say the one was permanent and that a channel canvas could
    not be deleted. Both were inferred from ``channel_canvas_already_exists``
    rather than documented, and a live delete disproved them.
    """
    description = _all_cards()["write_slack_channel_canvas"].description

    assert "one of these at a time" in description
    assert "permanently" not in description
    assert "cannot be deleted" not in description


def test_no_card_says_a_channel_canvas_cannot_be_deleted() -> None:
    """The claim was false and it cost a model a refusal it nearly made. A
    card that says *cannot* stops work that would have succeeded, so this
    pins the absence rather than one card's wording.
    """
    for card in _all_cards().values():
        assert "cannot be deleted" not in card.description


def test_the_delete_card_says_a_channel_canvas_goes_too_and_what_is_untried() -> None:
    """Slack documents no exception for a channel canvas and one has been
    deleted this way, so the card says so. Whether a channel accepts a new one
    afterwards is untried, and the card marks it untried rather than guessing:
    a model told *cannot* refuses work it could have done, and a model told
    *can* may empty a channel on the strength of nothing.
    """
    description = _all_cards()["delete_slack_canvas"].description

    assert "deletes a conversation's own canvas as well" in description
    assert "nobody has tried whether it accepts a new one" in description


def test_the_delete_card_says_it_cannot_be_undone_before_anything_else() -> None:
    """That sentence is now doing all the work the false one used to do, and
    nothing asks for confirmation before the call.
    """
    description = _all_cards()["delete_slack_canvas"].description

    assert "It cannot be undone" in description
    assert "there is no way to get it back" in description
    assert description.index("It cannot be undone") < description.index(
        "conversation's own canvas"
    )


def test_the_share_card_names_the_step_it_does_not_perform() -> None:
    description = _all_cards()["share_slack_canvas"].description

    assert "This does not hand anybody the canvas" in description
    assert "sent to them directly" in description
    assert "This tool will not send it" in description


def test_the_share_card_says_owner_is_one_person() -> None:
    description = _all_cards()["share_slack_canvas"].description

    assert "exactly one person" in description


def test_the_delete_card_says_it_cannot_be_undone() -> None:
    description = _all_cards()["delete_slack_canvas"].description

    assert "cannot be undone" in description


def test_the_six_cards_name_no_tool_outside_this_module() -> None:
    """A model holding these cards may hold no others. They are mounted on
    their own decision -- a request a Slack install can serve -- which is
    neither the posting tools' condition nor the reading tools', so a sentence
    explaining one of these by reference to those would describe something the
    model may not be able to reach. The six may name each other, because they
    are mounted together and always arrive together.
    """
    for card in _all_cards().values():
        for name in (
            "post_message",
            "pin_message",
            "read_pinned_messages",
            "read_slack_conversation",
            "download_slack_file",
            "search_slack_workspace",
            "find_by_name",
            "publish_slack_home_tab",
        ):
            assert name not in card.description, card.name
