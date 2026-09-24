# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Publish the asker's Slack App Home tab, and empty it again.

Slack's App Home is a private space between one app and one person. It holds
three tabs: **Messages**, which is the ordinary direct message conversation;
**About**, which is static metadata Slack renders from the app manifest; and
**Home**, a page of Block Kit the app publishes. Only Home is publishable, and
``views.publish`` is the only method that publishes it. ``views.open``,
``views.push`` and ``views.update`` operate on modals: each of them needs a
``trigger_id``, which exists only after somebody interacts with the app and
expires shortly afterwards, so none of the three is reachable from a tool call.

**The Home tab is per person.** Two people opening the same app are shown two
independent views, and publishing one says nothing about the other.

**The surface is gated by the manifest rather than by a scope.** ``views.publish``
requires no OAuth scope at all, which is unusual enough to be worth stating:
what decides whether it works is ``features.app_home.home_tab_enabled`` in the
app's own configuration, and Slack refuses a call made while it is off with
``not_enabled``. That is the analogue here of a missing scope elsewhere in this
package, and the refusal names the flag for the same reason a missing scope is
named: the code alone tells nobody what to go and change.

**Always the asker's own tab, and there is no argument for anybody else's.**
``views.publish`` accepts any ``user_id``, and the omission is deliberate rather
than unimplemented. Two reasons, and the second is the load-bearing one:

* A Home tab publish sends **no notification of any kind**. A cross-user publish
  is therefore a silent write into a private surface its owner may not open for
  days, and there is no message, badge or mark anywhere that they could notice.
* Every legitimate case reduces to the requester anyway. A welcome published
  when somebody joins is published on a turn that person's own event woke, and
  the connector stamps the actor behind such an event as the requester, so the
  requester is already the right target.

A model asked to show something to *somebody else* has to send a direct message
instead, and the card says so in those words. Saying it matters: a model that
reached for a ``user_id`` argument, found none, and got a refusal it could not
interpret would try again rather than change approach.

The consequence for this module is that nothing here names a destination. There
is no reach question, so ``channels.slack.write`` does not apply to either tool
and no confirmation rail is involved. Neither is wired in, and neither should
be: a rail that asks whether to widen an audience has nothing to ask about a
surface whose audience is one person and is always the person who asked.

**Publishing replaces the whole view.** There is no partial update, no append,
and no history: whatever the tab held is gone, and Slack keeps no copy to go
back to. ``blocks: []`` is the only way to empty a tab, because no method
deletes one -- which is why ``clear_slack_home_tab`` publishes an empty list rather
than calling something else.

**Neither ``text`` nor ``blocks`` is a refusal and not a clear.** This is the one
place where this module deliberately diverges from Slack's own semantics, and
the reasoning belongs beside the code rather than in a commit message: a model
that computed ``text`` and got an empty string has failed at something upstream,
and under Slack's semantics that failure would wipe the person's tab, with no
history and no undo. A destructive outcome must not be reachable by omission.
The refusal names ``clear_slack_home_tab`` as the alternative, because the model's
next move differs entirely depending on whether it meant to clear the tab or
failed to compute what should be on it.

**Two tools rather than one with a ``clear`` flag**, and the argument is the one
:mod:`jiuwenswarm.agents.harness.common.tools.slack_pins` records for keeping
its write apart from the reading tool next door. ``permissions.tools`` is keyed
by tool name and matched exactly, so one tool is one policy: folding publishing
and clearing into a single name would make *may publish, may not clear*
permanently inexpressible, and an operator asked to choose between all of it and
none of it picks none.

**``hash`` is deliberately not exposed.** Slack offers it as an optimistic
concurrency check, and it only exists for a caller that already holds a prior
view: there is no ``views.get``, so a hash comes either from a previous
``views.publish`` response or from an ``app_home_opened`` event payload. A model
calling a tool holds neither, so an argument for it would demand state that does
not exist on this side. The cost of leaving it out is last-write-wins between
two concurrent publishes to one person's tab -- the later call wins and the
earlier view is gone -- and ``hash_conflict``, the refusal Slack raises when a
supplied hash is stale, is unreachable from here for the same reason.

**``interactivity_pointer`` is modal plumbing** and is not applicable to a
published view.

**What an image can show, and what it cannot.** A Block Kit ``image`` block
takes either ``image_url``, which Slack documents as "The URL for a publicly
hosted image", or a ``slack_file`` object, whose reference page says its ``url``
"can be the ``url_private`` or the ``permalink`` of the Slack file" and that "the
user posting these blocks must have access to this file". So a Slack file that
this app can already see is showable by reference, and making a file public with
``files.sharedPublicURL`` is not a prerequisite for it. Nothing here implements
any file path: this module uploads nothing, shares nothing and resolves no file
reference. A ``slack_file`` an author wrote by hand reaches Slack through the
declared ``blocks`` argument or a fenced ``blockkit`` payload exactly as any
other block does, under the same allow-list, and the card states the limit
plainly so a model does not expect an upload it will not get.

**Not offered to a subagent.** Both tools act outward, onto a surface a person
opens. A subagent's output is read by the agent that started it rather than by a
person, so a subagent publishing a Home tab is a page nobody asked for appearing
in somebody's private space from a turn they cannot see. Nothing special is
arranged for this: the cards are added to the main agent's ability manager, as
every other Slack tool's are, and a subagent is built with its own.

Only cross-cutting primitives are borrowed, and from the two modules that own
them. ``slack_history`` holds the reading of a Slack response, the rule for when
a refusal is worth retrying, the pass that keeps a credential out of an error
code, the failure type that names the refused method the way Slack's own
documentation spells it, and the per-request choice of workspace. ``slack_post``
holds the one reading of the three Block Kit configuration keys: the fenced path
and the declared ``blocks`` argument are two spellings of one thing here exactly
as they are there, and a second reading of either gate would eventually admit
through one surface what the other refuses.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Mapping
from typing import Any

from openjiuwen.core.foundation.tool import LocalFunction, Tool, ToolCard

from jiuwenswarm.agents.harness.common.tools.slack_history import (
    _DEFAULT_RETRY_AFTER_SECONDS,
    SlackWorkspaceClients,
    _SlackCallFailure,
    _as_mapping,
    _retry_after_seconds,
    _safe_error_code,
    shared_slack_workspaces,
)
from jiuwenswarm.agents.harness.common.tools.slack_post import _blockkit_settings
from jiuwenswarm.common import slack_blocks
from jiuwenswarm.common.slack_history_policy import (
    METADATA_ASKER_KEY,
    METADATA_TEAM_KEY,
    SlackWorkspaceUnresolved,
)
from jiuwenswarm.common.slack_text import normalize_slack_mrkdwn, split_text


logger = logging.getLogger(__name__)

#: The two tool names, in the order the cards are built. Nothing reads it: the
#: registration mounts the built tools and takes each one's own ``name``, and
#: each card spells its name itself. It stays exported as this module's
#: statement of the pair -- the two names ``permissions.tools`` is written with
#: -- readable without building the toolkit to get them.
HOME_TAB_TOOL_NAMES: tuple[str, ...] = (
    "publish_slack_home_tab",
    "clear_slack_home_tab",
)

#: The SDK method name, in the SDK's spelling. ``_SlackCallFailure.where`` turns
#: it back into the ``views.publish`` an operator looks up.
_VIEWS_PUBLISH = "views_publish"

#: The one view type this module publishes. Slack's other view types are modals,
#: which ``views.publish`` does not open.
_HOME_VIEW_TYPE = "home"

#: Wall clock for one publish, retries included. A single call with no
#: pagination, so this is a ceiling on the tool rather than a scan budget, and a
#: rate-limit wait is spent from it rather than added to it.
_TIMEOUT_SECONDS = 30.0

#: How many times one call will wait out a rate limit before giving up.
_MAX_RATE_LIMIT_RETRIES = 2

#: What a refusal means, for the codes where the code alone is not actionable.
#: ``hash_conflict`` is deliberately absent: it is raised only for a stale
#: ``hash`` argument, this module never sends one, and a detail written for a
#: refusal that cannot happen is a sentence nobody can ever check.
_PUBLISH_FAILURE_DETAIL: Mapping[str, str] = {
    "not_enabled": (
        "this Slack app has no Home tab to publish into. It is turned on by"
        " features.app_home.home_tab_enabled in the app manifest, which is the"
        " app's own configuration rather than an OAuth scope, so switching it"
        " on needs no reinstall. Until somebody does, nothing can be published"
        " and calling again will not help"
    ),
    "view_too_large": (
        "Slack refused the view for its size; a published view may be at most"
        " 250 KB. Publish less, or publish a summary and put the rest in a"
        " message"
    ),
}


def slack_home_tab_request_metadata(
    channel_id: str | None,
    metadata: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Return trusted metadata for a request that may publish, or fail closed.

    Two conditions, and both are about the request rather than about anything a
    model wrote.

    * The request arrived on the ``slack`` transport, so a Slack install can be
      resolved for it and a Slack token exists to publish with.
    * It names the person whose turn this is. That person *is* the destination
      -- there is no argument for a tab and no fallback to anybody else's -- so
      a request that names nobody has nowhere to publish and mounts nothing.

    A scheduled run is not accepted, and the exclusion is the second condition
    rather than a rule of its own: the cron scheduler delivers a job with no
    requester, because a job is started by a clock rather than by a person, and
    a tab belonging to nobody does not exist. The pin tool next door accepts a
    marked cron run because it acts in the conversation the job was created in,
    which a job does have; there is no comparable fact here.
    """
    if str(channel_id or "").strip().lower() != "slack":
        return {}
    if not isinstance(metadata, Mapping):
        return {}
    if not str(metadata.get(METADATA_ASKER_KEY) or "").strip():
        return {}
    return dict(metadata)


def _home_tab_refusal_json(
    code: str,
    detail: str = "",
    *,
    user_id: str = "",
) -> str:
    """One refusal, in a shape that never claims to know what it does not.

    No ``blocks`` count and no ``replaced_a_view``. A call Slack refused
    establishes nothing about what the tab holds now -- a view refused for its
    size did not thereby empty the tab -- and a zero or a ``false`` there would
    read as a claim about the surface rather than about this call.
    """
    payload: dict[str, Any] = {"ok": False, "error": code}
    if detail:
        payload["detail"] = detail
    if user_id:
        payload["user_id"] = user_id
    logger.warning(
        "slack home tab refused: %s%s", code, f" -- {detail}" if detail else ""
    )
    return json.dumps(payload, ensure_ascii=False)


class SlackHomeTabToolkit:
    """The two Home tab tools, scoped to the Slack request in flight."""

    def __init__(
        self,
        *,
        metadata: dict[str, Any] | None = None,
        metadata_provider: Any | None = None,
        client: Any | None = None,
        workspaces: "SlackWorkspaceClients | None" = None,
        timeout_seconds: float = _TIMEOUT_SECONDS,
        sleep: Any = asyncio.sleep,
        monotonic: Any = time.monotonic,
    ) -> None:
        self._request_metadata = dict(metadata) if metadata else {}
        self._metadata_provider = metadata_provider
        self._client = client
        self._workspaces = workspaces or shared_slack_workspaces()
        self._timeout_seconds = max(1.0, float(timeout_seconds))
        self._sleep = sleep
        self._monotonic = monotonic
        self._bot_token = ""
        self._allowed_block_types: tuple[str, ...] = ()
        self._allow_interactive = slack_blocks.DEFAULT_ALLOW_INTERACTIVE_BLOCKS
        self._render_tables = slack_blocks.RENDER_TABLES_DEFAULT

    # ── request context ──────────────────────────────────────────────────

    def update_runtime_context(
        self,
        *,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Refresh the request-scoped metadata without recreating the tools."""
        self._request_metadata = dict(metadata) if metadata else {}

    def _runtime_metadata(self) -> dict[str, Any]:
        if self._metadata_provider is None:
            return dict(self._request_metadata)
        try:
            provided = self._metadata_provider()
        except Exception:  # noqa: BLE001 - providers must fail closed.
            return {}
        if not isinstance(provided, Mapping):
            return {}
        return dict(provided)

    async def _load_settings(self, metadata: Mapping[str, Any]) -> None:
        """Bind this request to the install it arrived from, and read the config.

        Takes the metadata the caller already read rather than reading it again.
        A provider is read once per request on purpose: it is live, and two
        reads of it are two requests as far as it is concerned.

        Read per call rather than captured at construction: this toolkit is
        built once and answers every request for the life of the process, a
        token rotated underneath it must take effect without a restart, and with
        several installs configured which token serves a call is a property of
        the request rather than of start-up.

        A publish into the wrong workspace publishes into a stranger's private
        surface and cannot be taken back, so an unresolvable install is refused
        rather than served from whichever block happens to hold a token.
        """
        slack = await self._workspaces.settings_for(
            str(metadata.get(METADATA_TEAM_KEY) or "").strip()
        )
        self._bot_token = str(slack.get("bot_token") or "").strip()
        (
            self._allowed_block_types,
            self._allow_interactive,
            self._render_tables,
        ) = _blockkit_settings(slack)

    def _get_client(self) -> Any:
        if self._client is not None:
            return self._client
        return self._workspaces.client_for(self._bot_token)

    async def _call(self, method: str, **kwargs: Any) -> dict[str, Any]:
        """Make one Slack call, reducing any refusal to a credential-free code.

        Both failure shapes are read. Slack answers a refused publish with HTTP
        200 and ``{"ok": false, "error": ...}``; the SDK raises that as
        ``SlackApiError``, and an older one hands the body back instead, so a
        code arriving one way on one deployment and the other way on the next
        would otherwise be two behaviours.
        """
        try:
            client = self._get_client()
        except _SlackCallFailure as exc:
            exc.method = exc.method or method
            raise
        deadline = self._monotonic() + self._timeout_seconds
        retries = 0
        while True:
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                raise _SlackCallFailure("slack_call_timed_out", method)
            try:
                response = await asyncio.wait_for(
                    getattr(client, method)(**kwargs), timeout=remaining
                )
            except TimeoutError:
                raise _SlackCallFailure("slack_call_timed_out", method) from None
            except Exception as exc:  # noqa: BLE001 - SDK types vary by version.
                delay = _retry_after_seconds(exc)
                if delay is not None and retries < _MAX_RATE_LIMIT_RETRIES:
                    retries += 1
                    await self._sleep(delay)
                    continue
                data = _as_mapping(getattr(exc, "response", None))
                code = _safe_error_code(data.get("error"), self._bot_token)
                raise _SlackCallFailure(code, method) from None

            data = _as_mapping(response)
            if data.get("ok", True) is False:
                code = _safe_error_code(data.get("error"), self._bot_token)
                if code == "ratelimited" and retries < _MAX_RATE_LIMIT_RETRIES:
                    retries += 1
                    await self._sleep(_DEFAULT_RETRY_AFTER_SECONDS)
                    continue
                raise _SlackCallFailure(code, method)
            return data

    # ── the target, which is never an argument ───────────────────────────

    @staticmethod
    def _requester(metadata: Mapping[str, Any]) -> str:
        """Whose tab this is, or ``""`` when the request names nobody.

        The sender the connector stamped, which for an event-woken turn is the
        person whose action produced it. That reading is the connector's rather
        than this module's, and reusing it is deliberate: every other Slack tool
        that needs to know who is asking reads the same field, and two notions
        of *who asked* in one codebase would eventually disagree about a turn
        that a reaction or a join started.
        """
        return str(metadata.get(METADATA_ASKER_KEY) or "").strip()

    # ── the blocks a view is made of ─────────────────────────────────────

    def _view_blocks(self, text: str) -> list[dict[str, Any]]:
        """One piece of text as the blocks a Home tab view is made of.

        The same passes an ordinary reply goes through, in the same order:
        convert the narrow Markdown subset to mrkdwn, split into pieces, then
        offer each piece to the Block Kit renderer. A fenced ``mermaid``,
        ``vega-lite`` or ``blockkit`` therefore behaves here exactly as it does
        in a message, which is the whole point of not writing a second renderer.

        One difference from the message path, and it is forced by the surface. A
        message has a ``text`` field, so a piece the renderer declines is posted
        as plain text and nothing is lost. A view has no such field: blocks are
        all it holds. So a declined piece becomes ``section`` blocks holding the
        same mrkdwn, which is what the renderer itself would have wrapped prose
        in, and the content reaching the tab is the content that would have
        reached a channel.
        """
        rendered: list[dict[str, Any]] = []
        for chunk in split_text(normalize_slack_mrkdwn(text)):
            blocks = slack_blocks.render_blocks(
                chunk,
                render_tables=self._render_tables,
                allowed_block_types=self._allowed_block_types,
                allow_interactive=self._allow_interactive,
            )
            rendered.extend(blocks or slack_blocks.section_blocks(chunk))
        return rendered

    def _declared_blocks(self, blocks: Any) -> "list[dict[str, Any]] | None":
        """A declared Block Kit payload, through the checks a fence goes through.

        ``None`` for a payload the checks refuse, which the caller reports. The
        checks are ``slack_blocks.check_blocks`` and nothing else: the fence and
        this argument are two spellings of one thing, and a second reading of
        either check would eventually admit through one what the other refuses.
        """
        if blocks is None:
            return None
        return slack_blocks.check_blocks(
            blocks,
            allowed_block_types=self._allowed_block_types,
            allow_interactive=self._allow_interactive,
        )

    def _slack_refusal(self, exc: _SlackCallFailure, *, user_id: str) -> str:
        """One Slack refusal as the shape every failure here returns."""
        detail = _PUBLISH_FAILURE_DETAIL.get(exc.code, "")
        if not detail:
            detail = f"Slack refused {exc.where}"
        return _home_tab_refusal_json(exc.code, detail, user_id=user_id)

    @staticmethod
    def _no_requester_refusal() -> str:
        """The refusal for a request that names nobody.

        Its own method because both tools reach it, and because it is the one
        refusal here that is about the request rather than about what was
        asked for. The registration gate already declines to mount either tool
        for such a request, so this is defence in depth: the provider fails
        closed per request, and a toolkit mounted for one turn answers the
        next.
        """
        return _home_tab_refusal_json(
            "trusted_slack_requester_required",
            "this request names nobody, so there is no Home tab to act on. A"
            " Home tab belongs to one person and this tool only ever acts on"
            " the tab of whoever asked; there is no other tab to fall back to",
        )

    async def _publish(
        self, blocks: list[dict[str, Any]], *, user_id: str
    ) -> str:
        """Publish one view for *user_id*, or say why it was not published.

        Shared by both tools because they differ in exactly one thing: the list
        of blocks, which is empty for a clear. Everything from here on -- the
        block ceiling, the call, which refusals mean what, what a success says
        -- is one behaviour, and writing it twice would be two behaviours
        waiting to diverge.

        The install is expected to be bound already, by the caller, because the
        caller needed the same settings to judge its own arguments against this
        deployment's Block Kit rules. Binding it a second time here would read
        a live provider twice for one request.
        """
        if len(blocks) > slack_blocks.MAX_BLOCKS_PER_VIEW:
            return _home_tab_refusal_json(
                "home_tab_too_many_blocks",
                f"a published view holds at most"
                f" {slack_blocks.MAX_BLOCKS_PER_VIEW} blocks and this one came"
                f" to {len(blocks)}; publish less, or publish a summary and put"
                f" the rest in a message",
                user_id=user_id,
            )

        try:
            await self._call(
                _VIEWS_PUBLISH,
                user_id=user_id,
                view={"type": _HOME_VIEW_TYPE, "blocks": blocks},
            )
        except _SlackCallFailure as exc:
            return self._slack_refusal(exc, user_id=user_id)

        logger.info(
            "slack home tab: published %d block(s) for %s", len(blocks), user_id
        )
        return json.dumps(
            {
                "ok": True,
                "user_id": user_id,
                "blocks": len(blocks),
                # Unconditional, and the constant is the fact rather than a
                # placeholder for one. ``views.publish`` has no partial mode and
                # keeps no history, so every call that succeeds replaces the
                # whole of whatever the tab held. Slack's response says nothing
                # about what that was, and nothing here pretends to know: the
                # claim is about this call, not about the view it displaced.
                "replaced_a_view": True,
            },
            ensure_ascii=False,
        )

    # ── the two tools ────────────────────────────────────────────────────

    async def publish_slack_home_tab(
        self,
        text: str | None = None,
        blocks: Any = None,
    ) -> str:
        """Publish a view onto the asker's Home tab, replacing what is there."""
        metadata = self._runtime_metadata()
        user_id = self._requester(metadata)
        if not user_id:
            return self._no_requester_refusal()

        body = str(text or "").strip()
        # The config has to be read before either argument can be judged: both
        # the allow-list and the interactive gate live in it, and a payload
        # checked against the defaults would be checked against the wrong rules.
        try:
            await self._load_settings(metadata)
        except SlackWorkspaceUnresolved as unresolved:
            return _home_tab_refusal_json(
                unresolved.code, unresolved.detail, user_id=user_id
            )

        declared = self._declared_blocks(blocks) if blocks is not None else None
        if blocks is not None and declared is None:
            # Not silently dropped, and not silently replaced by the text
            # either. A model that wrote blocks asked for a rendering, and
            # publishing something else while saying nothing would leave it
            # believing the rendering happened.
            if not body:
                return _home_tab_refusal_json(
                    "blocks_refused",
                    "the blocks were refused by this deployment's Block Kit"
                    " rules -- a block type that is not on"
                    " channels.slack.blockkit_allowed_block_types, or an"
                    " element a reader could click where"
                    " channels.slack.blockkit_allow_interactive is off -- and"
                    " no text was given to publish in their place",
                    user_id=user_id,
                )
            logger.warning(
                "slack home tab: the declared blocks were refused by this"
                " deployment's Block Kit rules; publishing the text instead"
            )

        if not body and declared is None:
            # The one place this module diverges from Slack's semantics on
            # purpose. Slack would read an empty view as *empty the tab*, so a
            # model whose text came out blank because something upstream failed
            # would wipe somebody's page, with no history and no way back. A
            # destructive outcome must not be reachable by omission, and the
            # refusal names the tool that clears deliberately, because what the
            # caller should do next depends entirely on which of the two it
            # meant.
            return _home_tab_refusal_json(
                "nothing_to_publish",
                "neither text nor blocks was given. Nothing was published and"
                " the Home tab still holds whatever it held before. If the tab"
                " should be emptied, call clear_slack_home_tab, which does that"
                " deliberately; otherwise work out what should be on the page"
                " and pass it as text or blocks",
                user_id=user_id,
            )

        view_blocks = declared if declared is not None else self._view_blocks(body)
        if not view_blocks:
            # Reachable where the text was all whitespace the renderer dropped.
            # Publishing the empty list this produced would clear the tab, which
            # is the outcome the refusal above exists to prevent, so it is
            # refused here on the same grounds and with the same alternative.
            return _home_tab_refusal_json(
                "nothing_to_publish",
                "the text given held nothing that could be put on a page."
                " Nothing was published and the Home tab still holds whatever"
                " it held before. If the tab should be emptied, call"
                " clear_slack_home_tab",
                user_id=user_id,
            )
        return await self._publish(view_blocks, user_id=user_id)

    async def clear_slack_home_tab(self) -> str:
        """Empty the asker's Home tab, leaving Slack's empty state on it."""
        metadata = self._runtime_metadata()
        user_id = self._requester(metadata)
        if not user_id:
            return self._no_requester_refusal()
        try:
            await self._load_settings(metadata)
        except SlackWorkspaceUnresolved as unresolved:
            return _home_tab_refusal_json(
                unresolved.code, unresolved.detail, user_id=user_id
            )
        # An empty block list is the whole of the mechanism. Slack has no method
        # that deletes a published view, so emptying one is publishing nothing
        # into it, and that call is a publish like any other.
        return await self._publish([], user_id=user_id)

    # ── cards ────────────────────────────────────────────────────────────

    def get_tools(self) -> list[Tool]:
        """Return the two request-scoped Home tab tools."""
        return [
            LocalFunction(card=_publish_card(), func=self.publish_slack_home_tab),
            LocalFunction(card=_clear_card(), func=self.clear_slack_home_tab),
        ]


def _publish_card() -> ToolCard:
    """The card for ``publish_slack_home_tab``.

    Unconditional: there is one argument shape and it varies with no setting.

    It names ``clear_slack_home_tab``, which is the one cross-reference here, and it
    is earned: the two are mounted on one decision and always arrive together,
    and the refusal a model gets for calling this with nothing names that tool
    as the alternative. A card that did not mention it would leave that refusal
    pointing at something the model has no reason to believe exists.
    """
    return ToolCard(
        name="publish_slack_home_tab",
        description=(
            "Put a page of content on your Home tab in Slack. The Home tab is "
            "a private space between this app and one person: they open it by "
            "clicking the app in the Slack sidebar, and nobody else can see "
            "it."
            "\nWhose tab. Always the tab of the person who asked for this "
            "turn. There is no argument for anybody else's and there will not "
            "be one: publishing to a Home tab sends no notification of any "
            "kind, so a page put on somebody else's tab is a silent change to "
            "a private space they may not open for days. To show something to "
            "another person, send them a direct message instead."
            "\nIt replaces everything. Each call replaces the whole page. "
            "There is no partial update and no append, Slack keeps no history, "
            "and what was there before cannot be recovered. Publish the whole "
            "page you want somebody to see, every time."
            "\nNo notification. Publishing tells nobody. The person sees the "
            "page the next time they open the tab, which may be much later or "
            "never. Say in a message that there is something new on the tab if "
            "it matters that they look."
            "\nWhat to publish. Pass text for ordinary content: it is written "
            "and rendered exactly as a Slack reply is, so a fenced mermaid, "
            "vega-lite or blockkit block draws here the way it draws in a "
            "message. Pass blocks instead to hand over a Block Kit payload you "
            "composed yourself. Both go through the same rules, so a payload "
            "this deployment refuses in a message is refused here too."
            "\nCalling with neither is refused and changes nothing. It does "
            "not empty the tab, because an empty page is almost never what an "
            "absent argument meant. Use clear_slack_home_tab to empty the tab on "
            "purpose."
            "\nWhat a page can hold. At most 100 blocks. An image block shows "
            "either a publicly reachable URL or a Slack file this app can "
            "already see; nothing here uploads a file or makes one shareable."
            "\nWhen the tab is switched off. This surface is turned on in the "
            "Slack app's own configuration rather than by a permission grant, "
            "so it can be off in a workspace where everything else works. A "
            "call made then fails and says so, and nothing but an operator "
            "change will fix it."
            "\nWhat comes back. Whose tab was published to, how many blocks "
            "the page holds now, and that a view was replaced. Nothing about "
            "what the tab held before comes back, because Slack does not "
            "report it."
        ),
        input_params={
            "type": "object",
            "properties": {
                "text": {
                    "type": "string",
                    "description": (
                        "The page, written the way a Slack reply is written. "
                        "Markdown, and fenced mermaid, vega-lite or blockkit "
                        "blocks render as they would in a message."
                    ),
                },
                "blocks": {
                    "type": "array",
                    "items": {"type": "object"},
                    "description": (
                        "A Block Kit payload, as a list of blocks. Use it "
                        "instead of text when the page is a layout rather than "
                        "prose. It is checked against the same rules a fenced "
                        "blockkit payload is checked against."
                    ),
                },
            },
            "required": [],
        },
    )


def _clear_card() -> ToolCard:
    """The card for ``clear_slack_home_tab``.

    A tool of its own rather than a flag on the one above, because
    ``permissions.tools`` is keyed by name: one tool is one policy, and folding
    the two together would make *may publish, may not clear* inexpressible.
    """
    return ToolCard(
        name="clear_slack_home_tab",
        description=(
            "Empty your Home tab in Slack, leaving the blank state a person "
            "sees before anything has ever been published there."
            "\nWhose tab. Always the tab of the person who asked for this "
            "turn, as publish_slack_home_tab is. There is no argument for anybody "
            "else's."
            "\nIt cannot be undone. Slack keeps no history of a Home tab, so "
            "whatever the page held is gone and can only come back by being "
            "published again. Clear the tab when its content is finished with "
            "or is misleading, not to make room for something you are about "
            "to publish -- publishing already replaces the whole page."
            "\nNo notification. Clearing tells nobody, exactly as publishing "
            "tells nobody."
            "\nIt takes no arguments and always does the same thing. Calling "
            "it twice is safe: the second call leaves an already empty tab "
            "empty."
        ),
        input_params={"type": "object", "properties": {}, "required": []},
    )


__all__ = [
    "HOME_TAB_TOOL_NAMES",
    "SlackHomeTabToolkit",
    "slack_home_tab_request_metadata",
]
