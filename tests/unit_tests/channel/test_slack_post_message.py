# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Posting, editing and deleting a Slack message, and how far each may reach.

Three tools that write, under one word an operator writes down. The tests fall
into four groups and the second is the one the whole design turns on:

* the three calls doing what they say, into the conversation the turn came from;
* the four words of ``channels.slack.write`` -- what each mounts, what each lets
  a call name, and which of them asks before a post reaches further;
* the two restrictions that are in code rather than in a prompt: an ephemeral
  message goes to the requester or to nobody, and a widening write with nobody
  to ask is refused rather than taken;
* the two ways a message is written -- text and declared blocks -- going through
  one renderer and one set of Block Kit checks.

The confirmation itself is asked by a rail, because a tool body cannot ask
anything, and the rail's own decisions are exercised at the end of this file
against the same toolkit the tools use.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

import jiuwenswarm.common.config as config_module
from jiuwenswarm.agents.harness.common.rails.slack_write_confirmation_rail import (
    SlackWriteConfirmationRail,
)
from jiuwenswarm.agents.harness.common.tools.slack_post import (
    DELETE_MESSAGE,
    EDIT_MESSAGE,
    POST_MESSAGE,
    SlackPostToolkit,
    _blockkit_settings,
    slack_post_request_metadata,
)
from jiuwenswarm.common import slack_blocks
from jiuwenswarm.common.slack_history_policy import (
    METADATA_ASKER_KEY,
    METADATA_ORIGIN_KEY,
    ORIGIN_CRON_JOB,
)
from jiuwenswarm.common.slack_write_policy import (
    METADATA_WRITE_POLICY_KEY,
    WRITE_DISABLED,
    WRITE_MEMBERS,
    WRITE_OPEN,
    WRITE_ORIGIN,
)

_HERE = "C0HERE"
_NARROW = "C0NARROW"
_WIDE = "C0WIDE"
_PUBLIC = "C0PUBLIC"
_PERSON = "U0ASKER"
_STRANGER = "U0STRANGER"
_DM = "D0STRANGER"
_TS = "1758123456.123456"
_TS2 = "1758123499.123456"
_URL = "https://acme.slack.com/"

#: Who is in which conversation. ``C0NARROW`` is a subset of ``C0HERE`` and is
#: written to silently; ``C0WIDE`` holds one person who is not here and is what
#: a confirmation is asked about.
_MEMBERS = {
    _HERE: [_PERSON, "U0B", "U0C"],
    _NARROW: [_PERSON, "U0B"],
    _WIDE: [_PERSON, "U0B", "U0C", _STRANGER],
    _PUBLIC: [_PERSON, "U0B"],
    _DM: [_PERSON, _STRANGER],
}


class _FakeResponse:
    def __init__(self, error: str) -> None:
        self.status_code = 200
        self.data = {"ok": False, "error": error}
        self.headers: dict[str, Any] = {}


class _FakeSlackError(Exception):
    def __init__(self, error: str) -> None:
        super().__init__("sanitized fake failure")
        self.response = _FakeResponse(error)


class _Workspace:
    """A fake Slack that records what it was asked and can be told to refuse."""

    def __init__(self, fail: "dict[str, str] | None" = None) -> None:
        self.fail = fail or {}
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.next_ts = iter([_TS, _TS2, "1758123500.123456", "1758123501.123456"])

    def _record(self, method: str, kwargs: dict[str, Any]) -> None:
        self.calls.append((method, kwargs))
        error = self.fail.get(method)
        if error:
            raise _FakeSlackError(error)

    def methods(self) -> list[str]:
        return [method for method, _ in self.calls]

    def sent(self, method: str) -> list[dict[str, Any]]:
        return [kwargs for name, kwargs in self.calls if name == method]

    async def auth_test(self, **kwargs: Any) -> dict[str, Any]:
        self._record("auth_test", kwargs)
        return {"ok": True, "url": _URL, "user_id": "U0BOT", "team_id": "T0ONE"}

    async def chat_postMessage(self, **kwargs: Any) -> dict[str, Any]:
        self._record("chat_postMessage", kwargs)
        return {"ok": True, "ts": next(self.next_ts), "channel": kwargs["channel"]}

    async def chat_postEphemeral(self, **kwargs: Any) -> dict[str, Any]:
        self._record("chat_postEphemeral", kwargs)
        return {"ok": True, "message_ts": next(self.next_ts)}

    async def chat_update(self, **kwargs: Any) -> dict[str, Any]:
        self._record("chat_update", kwargs)
        return {"ok": True, "ts": kwargs["ts"], "channel": kwargs["channel"]}

    async def chat_delete(self, **kwargs: Any) -> dict[str, Any]:
        self._record("chat_delete", kwargs)
        return {"ok": True, "ts": kwargs["ts"], "channel": kwargs["channel"]}

    async def conversations_open(self, **kwargs: Any) -> dict[str, Any]:
        self._record("conversations_open", kwargs)
        return {"ok": True, "channel": {"id": _DM}}

    async def conversations_members(self, **kwargs: Any) -> dict[str, Any]:
        self._record("conversations_members", kwargs)
        return {"ok": True, "members": list(_MEMBERS.get(kwargs["channel"], []))}

    async def conversations_info(self, **kwargs: Any) -> dict[str, Any]:
        self._record("conversations_info", kwargs)
        channel = str(kwargs["channel"])
        return {
            "ok": True,
            "channel": {
                "id": channel,
                "is_channel": True,
                "is_private": channel != _PUBLIC,
            },
        }


@pytest.fixture(autouse=True)
def _config(monkeypatch: pytest.MonkeyPatch) -> None:
    """The token and the Block Kit settings this toolkit reads.

    Never the conversation and never the policy word: both arrive stamped on
    the request, which is the whole reason the runtime may not read them here.
    """
    monkeypatch.setattr(
        config_module,
        "get_config",
        lambda: {"channels": {"slack": {"bot_token": "xoxb-config-secret"}}},
    )


def _toolkit(
    workspace: _Workspace,
    *,
    policy: str = WRITE_MEMBERS,
    asker: "str | None" = _PERSON,
    cron: bool = False,
    chat: str = _HERE,
) -> SlackPostToolkit:
    metadata: dict[str, Any] = {
        "slack_channel_id": chat,
        METADATA_WRITE_POLICY_KEY: policy,
    }
    if asker:
        metadata[METADATA_ASKER_KEY] = asker
    if cron:
        metadata[METADATA_ORIGIN_KEY] = ORIGIN_CRON_JOB
    return SlackPostToolkit(metadata=metadata, client=workspace)


def _run(coro: Any) -> dict[str, Any]:
    return json.loads(asyncio.run(coro))


def _cards(toolkit: SlackPostToolkit) -> dict[str, Any]:
    cards = {}
    for tool in toolkit.get_tools():
        card = getattr(tool, "card", None) or tool._card
        cards[card.name] = card
    return cards


# ── 1. the three calls ───────────────────────────────────────────────────────


def test_a_message_is_posted_into_the_conversation_the_turn_came_from() -> None:
    workspace = _Workspace()
    result = _run(_toolkit(workspace).post_message(text="the report is ready"))

    assert result["ok"] is True
    assert result["chat_id"] == _HERE
    assert result["message_id"] == _TS
    # Built rather than asked for: auth.test already answers the workspace url
    # and needs no scope, where chat.getPermalink would be a method to classify
    # and a grant to ask an operator for.
    assert result["permalink"] == f"{_URL}archives/{_HERE}/p1758123456123456"
    # Not split, so nothing claims it was.
    assert "parts" not in result
    posted = workspace.sent("chat_postMessage")[0]
    assert posted["channel"] == _HERE
    assert posted["text"] == "the report is ready"
    # None of the four identity arguments, whatever else is sent.
    for withheld in ("username", "icon_emoji", "icon_url", "as_user"):
        assert withheld not in posted


def test_a_message_is_edited_in_place() -> None:
    workspace = _Workspace()
    result = _run(
        _toolkit(workspace).edit_message(message_id=_TS, text="the report is late")
    )

    assert result == {
        "ok": True,
        "chat_id": _HERE,
        "message_id": _TS,
        "permalink": f"{_URL}archives/{_HERE}/p1758123456123456",
    }
    assert workspace.sent("chat_update")[0] == {
        "channel": _HERE,
        "ts": _TS,
        "text": "the report is late",
    }


def test_a_message_is_deleted() -> None:
    workspace = _Workspace()
    result = _run(_toolkit(workspace).delete_message(message_id=_TS))

    assert result == {
        "ok": True,
        "chat_id": _HERE,
        "message_id": _TS,
        "deleted": True,
    }
    assert workspace.sent("chat_delete")[0] == {"channel": _HERE, "ts": _TS}


def test_a_thread_reply_names_the_message_it_answers() -> None:
    workspace = _Workspace()
    _run(
        _toolkit(workspace).post_message(
            text="answering", reply_to_message_id=_TS, also_post_to_chat=True
        )
    )
    posted = workspace.sent("chat_postMessage")[0]
    assert posted["thread_ts"] == _TS
    assert posted["reply_broadcast"] is True


def test_a_long_message_is_split_and_the_parts_are_threaded_under_the_first() -> None:
    """The same splitter an ordinary reply goes through, and it says what it did.

    The parts after the first hang under the first, so a long answer is one
    message with a thread rather than several loose ones competing for the
    conversation.
    """
    workspace = _Workspace()
    long_text = "\n\n".join("paragraph " + "x" * 2000 for _ in range(30))
    result = _run(_toolkit(workspace).post_message(text=long_text))

    assert result["parts"] == 2
    assert result["message_id"] == _TS
    assert result["part_message_ids"] == [_TS, _TS2]
    second = workspace.sent("chat_postMessage")[1]
    assert second["thread_ts"] == _TS


def test_an_edit_too_long_for_slack_is_refused_rather_than_cut() -> None:
    """Slack takes a tenth as much text in an edit, and the rest has nowhere to go.

    A post is split because the pieces are messages. An edit's overflow is not a
    second message; it is text nobody asked to have posted.
    """
    workspace = _Workspace()
    result = _run(
        _toolkit(workspace).edit_message(message_id=_TS, text="y" * 5000)
    )
    assert result["ok"] is False
    assert result["error"] == "edit_text_too_long"
    assert workspace.methods() == []


# ── 2. the four words ────────────────────────────────────────────────────────


def test_disabled_mounts_nothing_and_refuses_anything_that_reaches_the_tool() -> None:
    """Two gates, and the second is defence in depth behind the first.

    The registration provider declines to mount the tools at all for a request
    whose word is ``disabled``; the tool refuses one anyway, so a metadata path
    nobody meant to exist cannot quietly post.
    """
    assert (
        slack_post_request_metadata(
            "slack",
            {"slack_channel_id": _HERE, METADATA_WRITE_POLICY_KEY: WRITE_DISABLED},
        )
        == {}
    )
    workspace = _Workspace()
    result = _run(
        _toolkit(workspace, policy=WRITE_DISABLED).post_message(text="hello")
    )
    assert result["ok"] is False
    assert result["error"] == "write_policy_forbids_posting"
    assert workspace.methods() == []


def test_origin_declares_no_chat_id_on_any_of_the_three_cards() -> None:
    """The narrowest word that posts at all, and the card is where it is enforced.

    The argument grants nothing on its own -- the gate is what decides -- but a
    card that does not declare it is the difference between a model choosing not
    to name a conversation and a model having no way to.
    """
    cards = _cards(_toolkit(_Workspace(), policy=WRITE_ORIGIN))
    assert set(cards) == {POST_MESSAGE, EDIT_MESSAGE, DELETE_MESSAGE}
    for name, card in cards.items():
        assert "chat_id" not in card.input_params["properties"], name
        assert "no way to name another one" in card.description, name


def test_origin_refuses_a_chat_id_that_reaches_the_tool_anyway() -> None:
    workspace = _Workspace()
    result = _run(
        _toolkit(workspace, policy=WRITE_ORIGIN).post_message(
            chat_id=_NARROW, text="hello"
        )
    )
    assert result["ok"] is False
    assert result["error"] == "write_policy_forbids_other_conversations"
    assert workspace.methods() == []


@pytest.mark.parametrize("word", [WRITE_MEMBERS, WRITE_OPEN])
def test_the_two_widest_words_declare_chat_id(word: str) -> None:
    cards = _cards(_toolkit(_Workspace(), policy=word))
    for name, card in cards.items():
        assert "chat_id" in card.input_params["properties"], name


def test_members_writes_silently_where_everybody_there_is_already_here() -> None:
    """``members(T) subset-of members(S)``: nobody new sees it, so nothing is asked.

    The inverted rule, and the direction is the point. The read rule asks
    whether everybody *here* is in T, because a read is disclosed into this
    room. A write travels the other way, so the question is whether everybody in
    T is already here.
    """
    workspace = _Workspace()
    result = _run(
        _toolkit(workspace).post_message(chat_id=_NARROW, text="for the record")
    )
    assert result["ok"] is True
    assert result["chat_id"] == _NARROW
    assert asyncio.run(
        _toolkit(workspace).widening_question(POST_MESSAGE, {"chat_id": _NARROW})
    ) is None


def test_members_asks_before_a_post_reaches_somebody_who_is_not_here() -> None:
    """The question names the widening rather than the destination.

    "Post to #general?" cannot be answered by somebody who does not already know
    what is at stake. A count of the people who would see it, and who they are,
    can be.
    """
    question = asyncio.run(
        _toolkit(_Workspace()).widening_question(POST_MESSAGE, {"chat_id": _WIDE})
    )
    assert question is not None
    assert _WIDE in question
    assert "1 person who is not in this conversation" in question
    assert _STRANGER in question


def test_an_edit_into_a_wider_conversation_asks_for_the_same_reason() -> None:
    """The new text comes out of this conversation just as a post's does."""
    question = asyncio.run(
        _toolkit(_Workspace()).widening_question(
            EDIT_MESSAGE, {"chat_id": _WIDE, "message_id": _TS}
        )
    )
    assert question is not None
    assert _WIDE in question


def test_delete_never_asks_and_reads_no_membership_to_decide() -> None:
    """It removes a message and widens nothing, so there is no audience question.

    Asserted twice over: no question is produced for it, and the call itself
    spends no ``conversations.members`` read working one out. The second is what
    says the absence is by design rather than by an accident of ordering.
    """
    assert asyncio.run(
        _toolkit(_Workspace()).widening_question(
            DELETE_MESSAGE, {"chat_id": _WIDE, "message_id": _TS}
        )
    ) is None
    workspace = _Workspace()
    result = _run(_toolkit(workspace).delete_message(chat_id=_WIDE, message_id=_TS))
    assert result["ok"] is True
    assert result["chat_id"] == _WIDE
    assert "conversations_members" not in workspace.methods()


def test_open_names_any_conversation_and_asks_nothing() -> None:
    """The widest word, and the one that inverts against the reading ladder.

    Under ``history: open`` a public target *relaxes* the rule, because its
    membership is self-serve. Under ``write: open`` there is no rule left to
    relax: naming any conversation is the whole of what the word buys, and an
    operator writing it has said they do not want to be asked.
    """
    workspace = _Workspace()
    result = _run(
        _toolkit(workspace, policy=WRITE_OPEN).post_message(
            chat_id=_WIDE, text="announcement"
        )
    )
    assert result["ok"] is True
    assert result["chat_id"] == _WIDE
    assert "conversations_members" not in workspace.methods()
    assert asyncio.run(
        _toolkit(workspace, policy=WRITE_OPEN).widening_question(
            POST_MESSAGE, {"chat_id": _WIDE}
        )
    ) is None


# ── 3. the two restrictions that are in code ─────────────────────────────────


def test_a_widening_write_with_nobody_to_ask_is_refused() -> None:
    """A scheduled run cannot answer a question, so it does not proceed unasked.

    Under ``members`` a widening target is a decision somebody takes. With
    nobody there to take it, taking it for them is the one option that is not
    available.
    """
    workspace = _Workspace()
    result = _run(
        _toolkit(workspace, asker=None, cron=True).post_message(
            chat_id=_WIDE, text="the nightly summary"
        )
    )
    assert result["ok"] is False
    assert result["error"] == "write_target_widens_audience"
    assert "scheduled run has no requester" in result["detail"]
    assert "chat_postMessage" not in workspace.methods()


def test_a_scheduled_run_still_writes_where_nobody_new_would_see_it() -> None:
    """The refusal above is about widening, not about being a scheduled run."""
    workspace = _Workspace()
    result = _run(
        _toolkit(workspace, asker=None, cron=True).post_message(
            chat_id=_NARROW, text="the nightly summary"
        )
    )
    assert result["ok"] is True
    assert result["chat_id"] == _NARROW


def test_a_scheduled_run_writes_into_a_public_target_nobody_new_is_in() -> None:
    """Membership decides a write, and a public target is no exception.

    A nightly job delivering into a large private room drops a summary into a
    small public ``C0PUBLIC`` whose members are all already in that room. The
    subset rule holds, so the write goes through unasked, as it always has. An
    operator who wants a public target confirmed or refused writes a narrower
    word; ``members`` is not that word.
    """
    workspace = _Workspace()
    result = _run(
        _toolkit(workspace, asker=None, cron=True).post_message(
            chat_id=_PUBLIC, text="the nightly summary"
        )
    )
    assert result["ok"] is True
    assert result["chat_id"] == _PUBLIC
    assert asyncio.run(
        _toolkit(workspace).widening_question(POST_MESSAGE, {"chat_id": _PUBLIC})
    ) is None


def test_an_ephemeral_goes_to_the_requester() -> None:
    workspace = _Workspace()
    result = _run(
        _toolkit(workspace).post_message(
            text="only you need this", visible_only_to_user_id=_PERSON
        )
    )
    assert result["ok"] is True
    assert result["ephemeral"] is True
    assert result["visible_only_to_user_id"] == _PERSON
    # No permalink, and the absence is the fact: an ephemeral is in nobody's
    # archive, so there is no url that would reach it.
    assert "permalink" not in result
    assert workspace.sent("chat_postEphemeral")[0]["user"] == _PERSON


def test_an_ephemeral_to_anybody_else_is_refused_in_code() -> None:
    """Not distrust of the model: an unobservable mistake needs an observable gate.

    An ephemeral cannot be edited, cannot be deleted, never enters the
    conversation's history, and is invisible to everybody but its one reader --
    including the person who asked. A wrong one sent to somebody else is
    therefore a mistake the one person who would care cannot see, and no
    instruction restores observability after the fact.
    """
    workspace = _Workspace()
    result = _run(
        _toolkit(workspace).post_message(
            text="psst", visible_only_to_user_id=_STRANGER
        )
    )
    assert result["ok"] is False
    assert result["error"] == "ephemeral_requires_the_requester"
    assert workspace.methods() == []


def test_an_ephemeral_is_unavailable_to_a_turn_nobody_started() -> None:
    """No requester, so nothing to compare, and no special rule needed for it."""
    workspace = _Workspace()
    result = _run(
        _toolkit(workspace, asker=None, cron=True).post_message(
            text="psst", visible_only_to_user_id=_PERSON
        )
    )
    assert result["ok"] is False
    assert result["error"] == "ephemeral_requires_a_requester"
    assert workspace.methods() == []


def test_a_chat_id_that_names_a_person_opens_the_direct_message() -> None:
    """The one thing this repository could not do before: start a conversation.

    ``conversations.open`` answers with the existing ``D…`` where there is one,
    so the call creates nothing that was not going to exist the moment the
    message was posted.
    """
    workspace = _Workspace()
    result = _run(
        _toolkit(workspace, policy=WRITE_OPEN).post_message(
            chat_id=_STRANGER, text="a quiet word"
        )
    )
    assert result["ok"] is True
    assert result["chat_id"] == _DM
    assert workspace.sent("conversations_open")[0] == {"users": _STRANGER}
    assert workspace.sent("chat_postMessage")[0]["channel"] == _DM


def test_a_direct_message_to_somebody_else_asks_under_members() -> None:
    """A private message reaches one person and nobody here can see it afterwards.

    Worded apart from the channel case for that reason: the count that makes the
    channel question answerable says nothing here, and who it is says everything.
    """
    question = asyncio.run(
        _toolkit(_Workspace()).widening_question(POST_MESSAGE, {"chat_id": _STRANGER})
    )
    assert question is not None
    assert _STRANGER in question
    assert "privately" in question
    # And a direct message to the person the turn is being run for reaches
    # nobody the turn did not already reach.
    assert asyncio.run(
        _toolkit(_Workspace()).widening_question(POST_MESSAGE, {"chat_id": _PERSON})
    ) is None


# ── 4. text, blocks, and the one set of checks ───────────────────────────────


def test_text_goes_through_the_renderer_an_ordinary_reply_goes_through() -> None:
    """Markdown becomes mrkdwn, and a table becomes the block it becomes in a reply.

    The promise the card makes is that nothing behaves differently for being
    sent through a tool, and this is the line that fails if a second converter
    or a second renderer appears on this path.
    """
    workspace = _Workspace()
    _run(
        _toolkit(workspace).post_message(
            text="## Results\n\n**done**\n\n| a | b |\n| --- | --- |\n| 1 | 2 |\n"
        )
    )
    posted = workspace.sent("chat_postMessage")[0]
    # mrkdwn, not Markdown: a heading is bold and ``**`` is a single asterisk.
    assert "*Results*" in posted["text"]
    assert "**done**" not in posted["text"]
    # And the table reached Block Kit rather than the conversation as pipes.
    assert any(
        block.get("type") in {"table", "data_table"} for block in posted["blocks"]
    )


def test_declared_blocks_are_sent_and_the_text_becomes_the_fallback() -> None:
    """The one thing the fence form cannot reach.

    Through a fence the notification line is whatever prose happened to surround
    it. Declared, ``text`` is written for the job: the line in the popup, in the
    sidebar, and to a screen reader.
    """
    workspace = _Workspace()
    blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": "*hi*"}}]
    result = _run(
        _toolkit(workspace).post_message(text="Deploy finished", blocks=blocks)
    )
    assert result["ok"] is True
    posted = workspace.sent("chat_postMessage")[0]
    assert posted["blocks"] == blocks
    assert posted["text"] == "Deploy finished"


def test_declared_blocks_go_through_the_gate_a_fence_goes_through() -> None:
    """One checker, reached two ways, and the argument is not the weaker way.

    Interactive elements are refused by default because this connector posts
    real approval buttons into the same conversations, and a reader cannot tell
    one a model wrote from one the approval flow wrote. A second reading of that
    check behind the tool argument would be a way round it.
    """
    interactive = [
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Approve"},
                    "action_id": "approve",
                }
            ],
        }
    ]
    # The same payload, refused by the same function the fence path calls.
    assert slack_blocks.check_blocks(interactive) is None

    workspace = _Workspace()
    result = _run(
        _toolkit(workspace).post_message(text="Deploy finished", blocks=interactive)
    )
    # Refused, and the text is posted in their place rather than silently
    # dropping what the model asked for.
    assert result["ok"] is True
    posted = workspace.sent("chat_postMessage")[0]
    assert "blocks" not in posted
    assert posted["text"] == "Deploy finished"


def test_refused_blocks_with_no_text_are_a_refusal_rather_than_a_blank_message() -> None:
    workspace = _Workspace()
    result = _run(
        _toolkit(workspace).post_message(
            blocks=[{"type": "input", "element": {"type": "plain_text_input"}}]
        )
    )
    assert result["ok"] is False
    assert result["error"] == "blocks_refused"
    assert workspace.methods() == []


def test_neither_text_nor_blocks_is_refused_before_slack_is_asked() -> None:
    workspace = _Workspace()
    result = _run(_toolkit(workspace).post_message())
    assert result["ok"] is False
    assert result["error"] == "nothing_to_post"
    assert workspace.methods() == []


def test_the_declared_fallback_is_never_split() -> None:
    """A fallback cut in half fails at exactly the moment it is needed.

    With blocks set the text is not on screen at all -- it is the notification
    line -- so splitting it would post the blocks once and the notification
    twice.
    """
    workspace = _Workspace()
    blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": "*hi*"}}]
    result = _run(
        _toolkit(workspace).post_message(text="z" * 60000, blocks=blocks)
    )
    assert result["ok"] is True
    assert "parts" not in result
    assert len(workspace.sent("chat_postMessage")) == 1


@pytest.mark.parametrize(
    ("written", "allowed", "interactive", "tables"),
    [
        ({}, (), False, slack_blocks.RENDER_TABLES_DEFAULT),
        ({"blockkit_allowed_block_types": "section"}, ("section",), False,
         slack_blocks.RENDER_TABLES_DEFAULT),
        ({"blockkit_allowed_block_types": ["section", " table "]},
         ("section", "table"), False, slack_blocks.RENDER_TABLES_DEFAULT),
        ({"blockkit_allow_interactive": True}, (), True,
         slack_blocks.RENDER_TABLES_DEFAULT),
        ({"blockkit_allow_interactive": "yes"}, (), False,
         slack_blocks.RENDER_TABLES_DEFAULT),
        ({"render_tables": False}, (), False, slack_blocks.RENDER_TABLES_OFF),
        ({"render_tables": "basic"}, (), False, slack_blocks.RENDER_TABLES_BASIC),
        ({"render_tables": "nonsense"}, (), False,
         slack_blocks.RENDER_TABLES_DEFAULT),
    ],
)
def test_the_three_block_kit_settings_are_read_the_way_the_connector_reads_them(
    written: dict[str, Any],
    allowed: tuple[str, ...],
    interactive: bool,
    tables: str,
) -> None:
    """Two processes, one config block, and one reading of it.

    The connector's resolvers are the reference and cannot be imported here --
    importing the connector pulls in ``slack_bolt``. This is the table that
    keeps the two readings the same, and the row that matters most is
    ``blockkit_allow_interactive`` written as something that is not a boolean:
    it fails closed, because it is the one of the three that is a safety control
    rather than a rendering choice.
    """
    assert _blockkit_settings(written) == (allowed, interactive, tables)


# ── 5. the request the tools will and will not serve ─────────────────────────


def test_the_metadata_provider_fails_closed() -> None:
    """Four conditions, each of which alone mounts nothing."""
    settled = {
        "slack_channel_id": _HERE,
        METADATA_WRITE_POLICY_KEY: WRITE_ORIGIN,
    }
    assert slack_post_request_metadata("slack", settled) == settled

    # Not a Slack request at all.
    assert slack_post_request_metadata("web", settled) == {}
    # A cron request that the scheduler did not mark as its own: a stray
    # slack_channel_id left on some other request cannot reach these tools.
    assert slack_post_request_metadata("__cron__", settled) == {}
    assert slack_post_request_metadata(
        "__cron__", {**settled, METADATA_ORIGIN_KEY: ORIGIN_CRON_JOB}
    ) == {**settled, METADATA_ORIGIN_KEY: ORIGIN_CRON_JOB}
    # No conversation, so nowhere to post and nothing to fall back to.
    assert slack_post_request_metadata(
        "slack", {METADATA_WRITE_POLICY_KEY: WRITE_ORIGIN}
    ) == {}
    # No word: nobody with the configuration settled this request, which is a
    # different thing from settling it to "disabled" and mounts nothing either.
    assert slack_post_request_metadata("slack", {"slack_channel_id": _HERE}) == {}
    assert slack_post_request_metadata(
        "slack", {"slack_channel_id": _HERE, METADATA_WRITE_POLICY_KEY: "louder"}
    ) == {}
    # Nothing at all.
    assert slack_post_request_metadata(None, None) == {}
    assert slack_post_request_metadata("slack", None) == {}


def test_a_provider_that_raises_mounts_no_target_argument() -> None:
    """A read that fails is not a licence; it is a gate answering no."""

    def _explode() -> dict[str, Any]:
        raise RuntimeError("the context is gone")

    toolkit = SlackPostToolkit(metadata_provider=_explode, client=_Workspace())
    assert toolkit.names_a_target() is False
    assert toolkit.confirms_widening() is False
    result = _run(toolkit.post_message(text="hello"))
    assert result["ok"] is False
    assert result["error"] == "write_policy_unsettled"


def test_a_malformed_message_id_never_reaches_slack() -> None:
    workspace = _Workspace()
    for call in (
        _toolkit(workspace).edit_message(message_id="yesterday", text="x"),
        _toolkit(workspace).delete_message(message_id="yesterday"),
    ):
        result = _run(call)
        assert result["error"] == "message_id_malformed"
    assert workspace.methods() == []


def test_a_slack_refusal_names_the_scope_it_wants() -> None:
    """``missing_scope`` reads the same whichever call was declined.

    The code alone is not actionable, and the calls here are fixed by different
    grants, so the refusal says which one.
    """
    workspace = _Workspace(fail={"conversations_open": "missing_scope"})
    result = _run(
        _toolkit(workspace, policy=WRITE_OPEN).post_message(
            chat_id=_STRANGER, text="a quiet word"
        )
    )
    assert result["ok"] is False
    assert result["error"] == "missing_scope"
    assert "im:write" in result["detail"]
    assert "reinstalling the Slack app" in result["detail"]


# ── 6. the rail that puts the question ───────────────────────────────────────


class _Inputs:
    def __init__(self, tool_name: str, tool_args: Any) -> None:
        self.tool_name = tool_name
        self.tool_args = tool_args


class _Ctx:
    def __init__(self, tool_name: str, tool_args: Any) -> None:
        self.inputs = _Inputs(tool_name, tool_args)
        self.extra: dict[str, Any] = {}
        self.session = None


def _decide(
    toolkit: SlackPostToolkit,
    tool_name: str,
    arguments: Any,
    user_input: Any = None,
) -> Any:
    rail = SlackWriteConfirmationRail(toolkit)
    return asyncio.run(
        rail.resolve_interrupt(_Ctx(tool_name, arguments), None, user_input)
    )


def test_the_rail_approves_a_call_that_widens_nothing() -> None:
    decision = _decide(_toolkit(_Workspace()), POST_MESSAGE, {"chat_id": _NARROW})
    assert type(decision).__name__ == "ApproveResult"


def test_the_rail_interrupts_with_the_question_the_toolkit_wrote() -> None:
    """One judgement, reached one way.

    The rail asks what the toolkit says to ask and computes nothing of its own,
    so the question somebody answers and the write that follows cannot be about
    two different things.
    """
    toolkit = _toolkit(_Workspace())
    decision = _decide(toolkit, POST_MESSAGE, json.dumps({"chat_id": _WIDE}))
    assert type(decision).__name__ == "InterruptResult"
    expected = asyncio.run(toolkit.widening_question(POST_MESSAGE, {"chat_id": _WIDE}))
    assert decision.request.message == expected
    assert [option["label"] for option in decision.request.ui_options] == [
        "Post it",
        "Do not post it",
    ]
    # Nothing is remembered: what is approved is an audience, and an audience is
    # exactly what differs between one call and the next.
    assert decision.request.auto_confirm_key == ""


def test_the_rail_approves_once_the_person_agrees() -> None:
    decision = _decide(
        _toolkit(_Workspace()), POST_MESSAGE, {"chat_id": _WIDE}, "allow_once"
    )
    assert type(decision).__name__ == "ApproveResult"


def test_a_declined_confirmation_is_reported_as_a_refusal_the_model_knows() -> None:
    decision = _decide(
        _toolkit(_Workspace()), POST_MESSAGE, {"chat_id": _WIDE}, "reject"
    )
    assert type(decision).__name__ == "RejectResult"
    result = json.loads(decision.tool_result)
    assert result["ok"] is False
    assert result["error"] == "write_confirmation_declined"


def test_an_unreadable_answer_is_asked_again_rather_than_read_as_either() -> None:
    """A mis-parsed yes posts what nobody agreed to; a mis-parsed no invents a refusal."""
    decision = _decide(
        _toolkit(_Workspace()), POST_MESSAGE, {"chat_id": _WIDE}, object()
    )
    assert type(decision).__name__ == "InterruptResult"


def test_the_rail_leaves_delete_alone() -> None:
    assert DELETE_MESSAGE not in SlackWriteConfirmationRail(
        _toolkit(_Workspace())
    ).get_tools()
