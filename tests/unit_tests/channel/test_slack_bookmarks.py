# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The bookmark bar of a Slack conversation, read and written.

Three groups of claims. The first is that each action makes the Slack call it
says it makes, in the conversation the gateway named and nowhere else. The
second is that ``edit`` does read-modify-write: Slack appeared to preserve the
fields an edit omitted when this was probed on 2026-09-14, that behaviour is
undocumented, and the tool must not depend on it. The third is what the tools
will not do -- reach Slack before their own arguments are established, drop an
argument the chosen action cannot carry, or cut a listing short.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

import jiuwenswarm.common.config as config_module
from jiuwenswarm.agents.harness.common.tools.slack_bookmarks import (
    SlackBookmarkToolkit,
    slack_bookmark_request_metadata,
)
from jiuwenswarm.common.slack_history_policy import (
    METADATA_ORIGIN_KEY,
    ORIGIN_CRON_JOB,
)

_CHAT = "C-ROOM"
_OTHER = "C-ELSEWHERE"
_BOOKMARK = "Bk01ABCDEF"


def _slack_bookmark(**overrides: Any) -> dict[str, Any]:
    """One bookmark as Slack states it, with Slack's own spelling throughout."""
    record = {
        "id": _BOOKMARK,
        "channel_id": _CHAT,
        "title": "Runbook",
        "link": "https://example.com/probe?a=1#f",
        "emoji": ":books:",
        "icon_url": "https://example.com/icon.png",
        "type": "link",
        "entity_id": None,
        "date_created": 1_757_000_000,
        "date_updated": 1_757_100_000,
        "rank": "g",
        "last_updated_by_user_id": "U-AUTHOR",
        "shortcut_id": None,
        "app_id": None,
        "parent_id": None,
    }
    record.update(overrides)
    return record


class _FakeResponse:
    """What the SDK hangs off a refusal: a body naming the error and nothing else."""

    def __init__(self, error: str) -> None:
        self.status_code = 200
        self.data = {"ok": False, "error": error}
        self.headers: dict[str, Any] = {}


class _FakeSlackError(Exception):
    def __init__(self, error: str) -> None:
        super().__init__("sanitized fake failure")
        self.response = _FakeResponse(error)


class _Workspace:
    """A fake Slack that records what was asked of it and can refuse.

    ``bookmarks.remove`` answers ``{"ok": true}`` and nothing else, which is
    what the live API does; ``add`` and ``edit`` answer with the bookmark, and
    ``list`` with the whole bar, because there is no cursor to follow.
    """

    def __init__(
        self,
        *,
        fail: "dict[str, str] | None" = None,
        bar: "list[dict[str, Any]] | None" = None,
    ) -> None:
        self.fail = fail or {}
        self.bar = bar if bar is not None else [_slack_bookmark()]
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def _record(self, method: str, kwargs: dict[str, Any]) -> None:
        self.calls.append((method, kwargs))
        error = self.fail.get(method)
        if error:
            raise _FakeSlackError(error)

    @property
    def methods(self) -> list[str]:
        return [method for method, _ in self.calls]

    async def bookmarks_add(self, **kwargs: Any) -> dict[str, Any]:
        self._record("bookmarks_add", kwargs)
        made = _slack_bookmark(
            id="Bk02NEWNEW",
            title=kwargs.get("title") or "",
            link=kwargs.get("link") or "",
            emoji=kwargs.get("emoji") or "",
            type=kwargs.get("type") or "link",
        )
        return {"ok": True, "bookmark": made}

    async def bookmarks_edit(self, **kwargs: Any) -> dict[str, Any]:
        self._record("bookmarks_edit", kwargs)
        return {"ok": True, "bookmark": _slack_bookmark()}

    async def bookmarks_remove(self, **kwargs: Any) -> dict[str, Any]:
        self._record("bookmarks_remove", kwargs)
        return {"ok": True}

    async def bookmarks_list(self, **kwargs: Any) -> dict[str, Any]:
        self._record("bookmarks_list", kwargs)
        return {"ok": True, "bookmarks": list(self.bar)}


@pytest.fixture(autouse=True)
def _config(monkeypatch: pytest.MonkeyPatch) -> None:
    """The token this toolkit reads. Never the conversation: that arrives stamped."""
    monkeypatch.setattr(
        config_module,
        "get_config",
        lambda: {"channels": {"slack": {"bot_token": "xoxb-config-secret"}}},
    )


def _toolkit(workspace: _Workspace, **metadata: Any) -> SlackBookmarkToolkit:
    base: dict[str, Any] = {"slack_channel_id": _CHAT}
    base.update(metadata)
    return SlackBookmarkToolkit(metadata=base, client=workspace)


def _write(toolkit: SlackBookmarkToolkit, **kwargs: Any) -> dict[str, Any]:
    return json.loads(asyncio.run(toolkit.write_bookmark(**kwargs)))


def _read(toolkit: SlackBookmarkToolkit) -> dict[str, Any]:
    return json.loads(asyncio.run(toolkit.read_bookmarks()))


def _cards(toolkit: SlackBookmarkToolkit) -> dict[str, Any]:
    cards = {}
    for tool in toolkit.get_tools():
        card = getattr(tool, "card", None) or tool._card
        cards[card.name] = card
    return cards


def _kwargs(workspace: _Workspace, method: str) -> dict[str, Any]:
    for name, kwargs in workspace.calls:
        if name == method:
            return kwargs
    raise AssertionError(f"{method} was never called")


# ── 1. the three calls that do the work ──────────────────────────────────────


def test_add_writes_into_the_conversation_the_request_came_from() -> None:
    workspace = _Workspace()
    result = _write(
        _toolkit(workspace),
        action="add",
        title="Runbook",
        url="https://example.com/probe?a=1#f",
        emoji_name="books",
    )

    assert result["ok"] is True
    assert result["action"] == "add"
    assert workspace.methods == ["bookmarks_add"]
    assert _kwargs(workspace, "bookmarks_add") == {
        "channel_id": _CHAT,
        "title": "Runbook",
        # Slack requires a type and link is the only one it took when this was
        # probed, so it is the default rather than an omitted argument.
        "type": "link",
        "link": "https://example.com/probe?a=1#f",
        "emoji": ":books:",
    }


def test_a_url_keeps_its_query_and_fragment() -> None:
    """``link`` is an arbitrary URL and is passed through as written.

    Observed on 2026-09-14: a probe URL carrying both came back with both. The
    field is spelled ``url`` here because ``permalink`` already means *link to
    a message* in this connector.
    """
    workspace = _Workspace()
    _write(
        _toolkit(workspace),
        action="add",
        title="Probe",
        url="https://example.com/probe?a=1&b=2#frag",
    )

    assert _kwargs(workspace, "bookmarks_add")["link"] == (
        "https://example.com/probe?a=1&b=2#frag"
    )


def test_add_returns_the_new_id_because_adding_is_not_idempotent() -> None:
    """Two identical adds made two bookmarks when this was probed.

    Nothing here deduplicates, because deduplicating is not what the tool says
    it does. What the caller gets instead is the id of the one just made.
    """
    workspace = _Workspace()
    first = _write(
        _toolkit(workspace), action="add", title="Runbook", url="https://e/1"
    )
    second = _write(
        _toolkit(workspace), action="add", title="Runbook", url="https://e/1"
    )

    assert first["bookmark_id"] == "Bk02NEWNEW"
    assert second["bookmark_id"] == "Bk02NEWNEW"
    assert workspace.methods == ["bookmarks_add", "bookmarks_add"]


def test_remove_names_the_bookmark_and_nothing_else() -> None:
    workspace = _Workspace()
    result = _write(_toolkit(workspace), action="remove", bookmark_id=_BOOKMARK)

    assert result == {
        "ok": True,
        "chat_id": _CHAT,
        "action": "remove",
        "bookmark_id": _BOOKMARK,
    }
    assert _kwargs(workspace, "bookmarks_remove") == {
        "channel_id": _CHAT,
        "bookmark_id": _BOOKMARK,
    }


def test_remove_accepts_a_quip_section_as_the_other_way_to_name_one() -> None:
    workspace = _Workspace()
    result = _write(_toolkit(workspace), action="remove", quip_section_id="S-42")

    assert result["ok"] is True
    assert result["quip_section_id"] == "S-42"
    assert _kwargs(workspace, "bookmarks_remove") == {
        "channel_id": _CHAT,
        "quip_section_id": "S-42",
    }


def test_a_bookmark_type_the_caller_names_is_passed_through() -> None:
    """Slack answers for it. ``invalid_bookmark_type`` is surfaced unchanged.

    Observed on 2026-09-14: ``message`` and ``file`` were both refused that
    way. The tool keeps no list of what works, because the set is Slack's to
    change and a local copy would be wrong silently.
    """
    workspace = _Workspace(fail={"bookmarks_add": "invalid_bookmark_type"})
    result = _write(
        _toolkit(workspace),
        action="add",
        title="A message",
        bookmark_type="message",
        entity_id="1758123456.123456",
    )

    assert _kwargs(workspace, "bookmarks_add")["type"] == "message"
    assert _kwargs(workspace, "bookmarks_add")["entity_id"] == "1758123456.123456"
    assert result["ok"] is False
    assert result["error"] == "invalid_bookmark_type"


def test_a_parent_puts_the_new_bookmark_inside_a_folder() -> None:
    """Folders cannot be made here, but one made in the Slack UI can be used.

    Observed on 2026-09-14: a linkless bookmark and ``type=folder`` both came
    back ``invalid_arguments``. ``parent_bookmark_id`` is exposed all the same,
    because nesting under an existing folder is real capability.
    """
    workspace = _Workspace()
    _write(
        _toolkit(workspace),
        action="add",
        title="Inside",
        url="https://example.com/x",
        parent_bookmark_id="Bk00FOLDER",
    )

    assert _kwargs(workspace, "bookmarks_add")["parent_id"] == "Bk00FOLDER"


def test_access_level_reaches_slack_under_slacks_own_name() -> None:
    workspace = _Workspace()
    _write(
        _toolkit(workspace),
        action="add",
        title="Shared",
        url="https://example.com/x",
        access_level="write",
    )

    assert _kwargs(workspace, "bookmarks_add")["access_level"] == "write"


# ── 2. edit is read-modify-write ─────────────────────────────────────────────


def test_edit_reads_the_bookmark_before_it_writes_it() -> None:
    """The fields the caller did not name are sent explicitly, not omitted.

    Slack appeared to preserve omitted fields when this was probed on
    2026-09-14. That is undocumented, so it is not relied on: the current
    record is read, the change is merged over it, and the editable fields go
    out in full. The call is then correct whichever way Slack behaves.
    """
    workspace = _Workspace()
    result = _write(
        _toolkit(workspace), action="edit", bookmark_id=_BOOKMARK, title="Runbook v2"
    )

    assert workspace.methods == ["bookmarks_list", "bookmarks_edit"]
    assert _kwargs(workspace, "bookmarks_list") == {"channel_id": _CHAT}
    assert _kwargs(workspace, "bookmarks_edit") == {
        "channel_id": _CHAT,
        "bookmark_id": _BOOKMARK,
        "title": "Runbook v2",
        # Carried over from the listing rather than left to Slack's mercy.
        "link": "https://example.com/probe?a=1#f",
        "emoji": ":books:",
    }
    assert result["ok"] is True
    assert result["action"] == "edit"


def test_edit_carries_over_a_field_the_caller_did_not_name() -> None:
    workspace = _Workspace()
    _write(
        _toolkit(workspace),
        action="edit",
        bookmark_id=_BOOKMARK,
        url="https://example.com/moved",
    )

    sent = _kwargs(workspace, "bookmarks_edit")
    assert sent["link"] == "https://example.com/moved"
    assert sent["title"] == "Runbook"
    assert sent["emoji"] == ":books:"


def test_edit_omits_a_field_that_was_empty_and_was_not_named() -> None:
    """Sending an empty value Slack may refuse buys nothing.

    The field was empty before the call and is empty after it whichever way
    Slack treats an omitted argument, so leaving it out cannot change the
    outcome. This is the one place the read-modify-write does not send all
    three, and the reason is that there is nothing to preserve.
    """
    workspace = _Workspace(bar=[_slack_bookmark(emoji=None)])
    _write(
        _toolkit(workspace), action="edit", bookmark_id=_BOOKMARK, title="Runbook v2"
    )

    assert "emoji" not in _kwargs(workspace, "bookmarks_edit")


def test_edit_sends_an_empty_value_the_caller_asked_for() -> None:
    """Clearing a field is the caller's request and goes to Slack as one."""
    workspace = _Workspace()
    _write(_toolkit(workspace), action="edit", bookmark_id=_BOOKMARK, emoji_name="")

    assert _kwargs(workspace, "bookmarks_edit")["emoji"] == ""


def test_an_edit_of_a_bookmark_that_is_not_there_never_writes() -> None:
    """The listing is already in hand, so the answer is certain without a call."""
    workspace = _Workspace()
    result = _write(
        _toolkit(workspace), action="edit", bookmark_id="Bk09MISSING", title="x"
    )

    assert workspace.methods == ["bookmarks_list"]
    assert result["ok"] is False
    assert result["error"] == "bookmark_not_found"


def test_a_refused_listing_is_not_reported_as_a_failed_write() -> None:
    """The read half failed, so the write never happened and never will here."""
    workspace = _Workspace(fail={"bookmarks_list": "missing_scope"})
    result = _write(
        _toolkit(workspace), action="edit", bookmark_id=_BOOKMARK, title="x"
    )

    assert workspace.methods == ["bookmarks_list"]
    assert result["ok"] is False
    assert result["error"] == "missing_scope"
    assert "bookmarks.list" in result["detail"]
    # The scope named is the one the refused call needs, not the tool's.
    assert "bookmarks:read" in result["detail"]


def test_an_edit_that_names_no_change_never_reaches_slack() -> None:
    workspace = _Workspace()
    result = _write(_toolkit(workspace), action="edit", bookmark_id=_BOOKMARK)

    assert result["ok"] is False
    assert result["error"] == "nothing_to_edit"
    assert workspace.calls == []


# ── 3. the refusals a caller has to see ──────────────────────────────────────


def test_a_full_bar_is_a_failure_and_says_what_resolves_it() -> None:
    """Not retryable, and no number is quoted anywhere.

    Slack documents that a conversation's bookmark count is bounded. The bound
    is Slack's, so ``too_many_bookmarks`` is surfaced as it came rather than
    pre-empted by a count kept here.
    """
    workspace = _Workspace(fail={"bookmarks_add": "too_many_bookmarks"})
    result = _write(
        _toolkit(workspace), action="add", title="One more", url="https://e/1"
    )

    assert result["ok"] is False
    assert result["error"] == "too_many_bookmarks"
    assert result["chat_id"] == _CHAT
    assert "removes one" in result["detail"]
    assert "calling again will not help" in result["detail"]


def test_a_write_missing_scope_names_the_write_scope() -> None:
    workspace = _Workspace(fail={"bookmarks_add": "missing_scope"})
    result = _write(_toolkit(workspace), action="add", title="x", url="https://e/1")

    assert result["error"] == "missing_scope"
    assert "bookmarks.add" in result["detail"]
    assert "bookmarks:write" in result["detail"]


def test_a_folder_refusal_explains_what_cannot_be_created() -> None:
    workspace = _Workspace(fail={"bookmarks_add": "invalid_arguments"})
    result = _write(
        _toolkit(workspace), action="add", title="Folder", bookmark_type="folder"
    )

    assert result["error"] == "invalid_arguments"
    assert "cannot be created through the API" in result["detail"]


def test_a_refusal_claims_nothing_about_what_the_bar_now_holds() -> None:
    """A refused call establishes nothing about the state of the conversation."""
    cases = (
        (
            "bookmarks_add",
            "too_many_bookmarks",
            {"action": "add", "title": "x", "url": "https://e/1"},
        ),
        (
            "bookmarks_remove",
            "bookmark_not_found",
            {"action": "remove", "bookmark_id": _BOOKMARK},
        ),
    )
    for method, error, arguments in cases:
        workspace = _Workspace(fail={method: error})
        result = _write(_toolkit(workspace), **arguments)
        assert result["ok"] is False
        assert result["error"] == error
        assert "bookmark" not in result


# ── 4. what is refused before Slack is reached at all ────────────────────────


def test_a_request_with_no_conversation_is_refused_and_calls_nothing() -> None:
    """Fails closed. There is no conversation to fall back to and none is invented."""
    workspace = _Workspace()
    toolkit = SlackBookmarkToolkit(metadata={}, client=workspace)
    result = json.loads(
        asyncio.run(toolkit.write_bookmark(action="add", title="x", url="https://e/1"))
    )

    assert result["ok"] is False
    assert result["error"] == "trusted_slack_channel_context_required"
    assert workspace.calls == []


def test_a_read_with_no_conversation_is_refused_and_calls_nothing() -> None:
    workspace = _Workspace()
    toolkit = SlackBookmarkToolkit(metadata={}, client=workspace)
    result = json.loads(asyncio.run(toolkit.read_bookmarks()))

    assert result["ok"] is False
    assert result["error"] == "trusted_slack_channel_context_required"
    # The empty list is on the refusal so that a refusal and a listing are one
    # shape for a caller to read.
    assert result["bookmarks"] == []
    assert workspace.calls == []


def test_a_provider_that_raises_is_a_refusal_rather_than_a_default() -> None:
    def _explode() -> dict[str, Any]:
        raise RuntimeError("no context here")

    workspace = _Workspace()
    toolkit = SlackBookmarkToolkit(metadata_provider=_explode, client=workspace)
    result = json.loads(asyncio.run(toolkit.write_bookmark(action="remove")))

    assert result["ok"] is False
    assert result["error"] == "trusted_slack_channel_context_required"
    assert workspace.calls == []


@pytest.mark.parametrize("action", ["", "   ", "delete", "list", "ADD ME"])
def test_an_action_that_is_not_one_never_reaches_slack(action: str) -> None:
    workspace = _Workspace()
    result = _write(_toolkit(workspace), action=action, bookmark_id=_BOOKMARK)

    assert result["ok"] is False
    assert result["error"] in {"action_required", "action_unknown"}
    assert workspace.calls == []


def test_an_add_without_a_title_never_reaches_slack() -> None:
    workspace = _Workspace()
    result = _write(_toolkit(workspace), action="add", url="https://e/1")

    assert result["error"] == "title_required"
    assert workspace.calls == []


@pytest.mark.parametrize("action", ["edit", "remove"])
def test_naming_no_bookmark_never_reaches_slack(action: str) -> None:
    workspace = _Workspace()
    result = _write(_toolkit(workspace), action=action, title="x")

    assert result["ok"] is False
    assert result["error"] in {"bookmark_id_required", "argument_not_valid_for_action"}
    assert workspace.calls == []


def test_an_argument_the_action_cannot_carry_is_refused_by_name() -> None:
    """Not dropped. Slack's edit takes the title, the link and the emoji.

    An ``access_level`` passed to an edit is a change the caller asked for and
    would not get, and swallowing it would report that change as made.
    """
    workspace = _Workspace()
    result = _write(
        _toolkit(workspace),
        action="edit",
        bookmark_id=_BOOKMARK,
        title="x",
        access_level="write",
        parent_bookmark_id="Bk00FOLDER",
    )

    assert result["ok"] is False
    assert result["error"] == "argument_not_valid_for_action"
    assert "access_level" in result["detail"]
    assert "parent_bookmark_id" in result["detail"]
    assert workspace.calls == []


def test_no_action_specific_argument_declares_a_default() -> None:
    """Because a default is an argument the card sends on the caller's behalf.

    ``bookmark_type`` declared ``default: link``. A model that honours a schema
    default writes the argument on every call, so an edit and a remove -- which
    cannot carry a type -- were refused ``argument_not_valid_for_action`` for
    an argument the caller never chose. The rule is general: an argument only
    some actions accept may not be defaulted on the shared surface, and the
    default belongs on the add path that applies it.
    """
    card = _cards(_toolkit(_Workspace()))["write_bookmark"]
    properties = card.input_params["properties"]
    shared_by_every_action = {"action"}

    for name, schema in properties.items():
        if name in shared_by_every_action:
            continue
        assert "default" not in schema, name


def test_an_add_that_names_no_type_still_makes_a_link_bookmark() -> None:
    """The default moved to the add path and did not go missing."""
    workspace = _Workspace()
    _write(_toolkit(workspace), action="add", title="Runbook", url="https://e/x")

    assert _kwargs(workspace, "bookmarks_add")["type"] == "link"


@pytest.mark.parametrize("action", ["edit", "remove"])
def test_the_type_argument_is_refused_only_when_the_caller_wrote_it(
    action: str,
) -> None:
    """The refusal survives; what is gone is the card writing the argument.

    A caller that genuinely passes a type to an edit or a remove asked for
    something that would not happen and is told so by name. A caller that
    passed nothing sends nothing and is never in this branch.
    """
    workspace = _Workspace()
    refused = _write(
        _toolkit(workspace),
        action=action,
        bookmark_id=_BOOKMARK,
        title="x" if action == "edit" else None,
        bookmark_type="link",
    )

    assert refused["error"] == "argument_not_valid_for_action"
    assert "bookmark_type" in refused["detail"]
    assert workspace.calls == []

    allowed = _write(
        _toolkit(_Workspace()),
        action=action,
        bookmark_id=_BOOKMARK,
        title="x" if action == "edit" else None,
    )
    assert allowed["ok"] is True


@pytest.mark.parametrize(
    ("action", "article"), [("add", "an"), ("edit", "an"), ("remove", "a")]
)
def test_the_refusal_gives_each_action_the_article_it_takes(
    action: str, article: str
) -> None:
    """Model-facing prose. "a edit" and "a add" were both being written."""
    result = _write(
        _toolkit(_Workspace()),
        action=action,
        bookmark_id=_BOOKMARK,
        title="x",
        url="https://e/x",
        access_level="write",
    )

    assert result["error"] == "argument_not_valid_for_action"
    detail = result["detail"]
    assert f"cannot be applied by {article} {action}" in detail
    assert f"{article.capitalize()} {action} takes" in detail
    wrong = "a" if article == "an" else "an"
    assert f" {wrong} {action}" not in detail


def test_an_add_cannot_name_a_bookmark_id() -> None:
    """The id is what adding produces; one passed in would be silently ignored."""
    workspace = _Workspace()
    result = _write(
        _toolkit(workspace), action="add", title="x", bookmark_id=_BOOKMARK
    )

    assert result["error"] == "argument_not_valid_for_action"
    assert workspace.calls == []


def test_an_emoji_that_cannot_be_named_never_reaches_slack() -> None:
    """The same normaliser the reaction tool uses, so emoji_name means one thing."""
    workspace = _Workspace()
    result = _write(
        _toolkit(workspace),
        action="add",
        title="x",
        url="https://e/1",
        emoji_name="\N{SNOWMAN}",
    )

    assert result["ok"] is False
    assert workspace.calls == []


def test_an_emoji_character_is_converted_to_the_name_slack_knows() -> None:
    workspace = _Workspace()
    _write(
        _toolkit(workspace),
        action="add",
        title="x",
        url="https://e/1",
        emoji_name="\N{PARTY POPPER}",
    )

    assert _kwargs(workspace, "bookmarks_add")["emoji"] == ":tada:"


def test_surrounding_colons_are_accepted_on_the_way_in() -> None:
    workspace = _Workspace()
    _write(
        _toolkit(workspace),
        action="add",
        title="x",
        url="https://e/1",
        emoji_name=":books:",
    )

    assert _kwargs(workspace, "bookmarks_add")["emoji"] == ":books:"


# ── 5. the listing returns everything ────────────────────────────────────────


def test_the_listing_returns_every_bookmark_and_says_how_many() -> None:
    """No bound of ours. ``bookmarks.list`` cannot page, so there is nothing to
    resume and a limit here would leave a caller short with no way to ask for
    the rest."""
    bar = [
        _slack_bookmark(id=f"Bk{index:08d}", title=f"Link {index}")
        for index in range(250)
    ]
    workspace = _Workspace(bar=bar)
    result = _read(_toolkit(workspace))

    assert result["ok"] is True
    assert result["chat_id"] == _CHAT
    assert result["bookmarks_returned"] == 250
    assert len(result["bookmarks"]) == 250
    assert workspace.methods == ["bookmarks_list"]
    assert _kwargs(workspace, "bookmarks_list") == {"channel_id": _CHAT}


def test_the_listing_reports_no_coverage_status_it_could_not_honour() -> None:
    """There is nothing to page and nothing was cut, so there is no partial
    state to claim. A listing that reported ``complete`` over a list it had
    trimmed would be worse than no listing."""
    result = _read(_toolkit(_Workspace(bar=[_slack_bookmark()])))

    assert "coverage" not in result
    assert "warnings" not in result


def test_a_listed_bookmark_is_in_this_connectors_vocabulary() -> None:
    result = _read(_toolkit(_Workspace()))

    assert result["bookmarks"] == [
        {
            "bookmark_id": _BOOKMARK,
            "title": "Runbook",
            "url": "https://example.com/probe?a=1#f",
            # Without its colons, which is what emoji_name means everywhere
            # else in this connector.
            "emoji_name": "books",
            "bookmark_type": "link",
            "rank": "g",
            "icon_url": "https://example.com/icon.png",
            "updated_by_user_id": "U-AUTHOR",
            "created_iso_utc": "2025-09-04T15:33:20Z",
            "updated_iso_utc": "2025-09-05T19:20:00Z",
        }
    ]


def test_rank_stays_the_string_it_is() -> None:
    """A lexicographic sort key, not a number. Read as one it sorts the bar wrongly."""
    workspace = _Workspace(bar=[_slack_bookmark(rank="p")])
    result = _read(_toolkit(workspace))

    assert result["bookmarks"][0]["rank"] == "p"
    assert isinstance(result["bookmarks"][0]["rank"], str)


def test_fields_slack_left_empty_are_absent_rather_than_null() -> None:
    result = _read(_toolkit(_Workspace()))
    record = result["bookmarks"][0]

    for absent in ("entity_id", "access_level", "parent_bookmark_id", "shortcut_id"):
        assert absent not in record


def test_fields_slack_filled_in_are_kept() -> None:
    workspace = _Workspace(
        bar=[
            _slack_bookmark(
                entity_id="1758123456.123456",
                parent_id="Bk00FOLDER",
                shortcut_id="Sc123",
                app_id="A123",
                access_level="read",
            )
        ]
    )
    record = _read(_toolkit(workspace))["bookmarks"][0]

    assert record["entity_id"] == "1758123456.123456"
    assert record["parent_bookmark_id"] == "Bk00FOLDER"
    assert record["shortcut_id"] == "Sc123"
    assert record["app_id"] == "A123"
    assert record["access_level"] == "read"


def test_an_empty_bar_is_a_successful_empty_listing() -> None:
    result = _read(_toolkit(_Workspace(bar=[])))

    assert result["ok"] is True
    assert result["bookmarks_returned"] == 0
    assert result["bookmarks"] == []


def test_a_refused_listing_keeps_the_shape_a_listing_has() -> None:
    workspace = _Workspace(fail={"bookmarks_list": "channel_not_found"})
    result = _read(_toolkit(workspace))

    assert result["ok"] is False
    assert result["error"] == "channel_not_found"
    assert result["chat_id"] == _CHAT
    assert result["bookmarks"] == []


def test_too_many_bookmarks_is_surfaced_unchanged_on_a_listing() -> None:
    workspace = _Workspace(fail={"bookmarks_list": "too_many_bookmarks"})
    result = _read(_toolkit(workspace))

    assert result["error"] == "too_many_bookmarks"


# ── 6. the cards ─────────────────────────────────────────────────────────────


def test_the_conversation_is_never_taken_from_an_argument() -> None:
    """There is no ``chat_id`` on either card, and the schemas are the whole of it."""
    cards = _cards(_toolkit(_Workspace()))

    assert set(cards) == {"write_bookmark", "read_bookmarks"}
    assert set(cards["write_bookmark"].input_params["properties"]) == {
        "action",
        "bookmark_id",
        "quip_section_id",
        "title",
        "url",
        "emoji_name",
        "bookmark_type",
        "entity_id",
        "access_level",
        "parent_bookmark_id",
    }
    assert cards["write_bookmark"].input_params["required"] == ["action"]
    assert cards["read_bookmarks"].input_params["properties"] == {}
    assert cards["read_bookmarks"].input_params["required"] == []
    for card in cards.values():
        assert "chat_id" not in card.description


def test_the_read_card_says_rank_is_text() -> None:
    """So that nobody sorts it numerically."""
    description = _cards(_toolkit(_Workspace()))["read_bookmarks"].description

    assert "rank is a sort key and it is text, not a number" in description


def test_the_read_card_promises_the_whole_bar() -> None:
    description = _cards(_toolkit(_Workspace()))["read_bookmarks"].description

    assert "nothing is cut" in description


def test_the_write_card_says_adding_is_not_idempotent() -> None:
    description = _cards(_toolkit(_Workspace()))["write_bookmark"].description

    assert "two identical calls make two bookmarks" in description


def test_the_cards_name_no_other_tool() -> None:
    """A model holding these cards may hold no others.

    The pair is mounted on its own decision, so a sentence explaining one by
    reference to a sibling would, on a turn that has only these, describe
    something the model cannot reach.
    """
    for card in _cards(_toolkit(_Workspace())).values():
        for name in (
            "pin_message",
            "read_pinned_messages",
            "read_slack_conversation",
            "download_slack_file",
            "search_slack_workspace",
        "read_slack_canvas",
        "write_slack_canvas",
        "write_slack_channel_canvas",
        "edit_slack_canvas",
        "share_slack_canvas",
        "delete_slack_canvas",
        "read_slack_list",
        "write_slack_list",
        "edit_slack_list",
        "share_slack_list",
        "delete_slack_list",
        ):
            assert name not in card.description


# ── 7. which requests may touch bookmarks at all ─────────────────────────────


def test_an_inbound_slack_turn_may_touch_bookmarks() -> None:
    assert slack_bookmark_request_metadata("slack", {"slack_channel_id": _CHAT}) == {
        "slack_channel_id": _CHAT
    }


def test_a_scheduled_run_may_where_the_scheduler_marked_it() -> None:
    """A job posts into the conversation it was created in; the bar is that room's."""
    metadata = {
        "slack_channel_id": _CHAT,
        METADATA_ORIGIN_KEY: ORIGIN_CRON_JOB,
    }
    assert slack_bookmark_request_metadata("__cron__", metadata) == metadata


@pytest.mark.parametrize(
    ("channel_id", "metadata"),
    [
        # Slack-looking metadata on another transport is not a Slack request.
        ("web", {"slack_channel_id": _OTHER}),
        # A cron request without the scheduler's own marker.
        ("__cron__", {"slack_channel_id": _OTHER}),
        # The marker without a conversation to act in.
        ("__cron__", {METADATA_ORIGIN_KEY: ORIGIN_CRON_JOB}),
        # An inbound Slack turn the connector stamped no conversation on.
        ("slack", {}),
        ("slack", None),
    ],
)
def test_everything_else_mounts_nothing(
    channel_id: str, metadata: "dict[str, Any] | None"
) -> None:
    assert slack_bookmark_request_metadata(channel_id, metadata) == {}


def test_the_history_policy_does_not_decide_whether_a_turn_may_bookmark() -> None:
    """A bookmark is not scrollback, and the word that governs scrollback is not
    the word that governs this.

    ``permissions.tools`` is where each of the two names is allowed or refused.
    Reading the history word here would take both tools from every ``origin``
    deployment, which is most of them, and hand them to an operator who widened
    their reads and asked for nothing else.
    """
    metadata = {"slack_channel_id": _CHAT, "slack_history_policy": "disabled"}
    assert slack_bookmark_request_metadata("slack", metadata) == metadata
