# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Put an emoji reaction on a Slack message, or take one off.

The smallest thing this deployment can say in Slack without saying it in
words. A reaction marks a message in place: it acknowledges, it records that
something was handled, and it does none of it in a way that notifies a channel
the way another post would. The connector has marked messages this way since
it was written -- ``eyes`` on arrival, ``heavy_check_mark`` when the turn ends
-- and this module is the same act made available to the model rather than
reserved for the request lifecycle.

**The conversation is not an argument.**

There is no ``chat_id`` here, and adding one is not a small extension. The
conversation comes from the same trusted request metadata the history tool
binds to, so the tool writes where the request came from and nowhere else. The
membership rule that lets ``read_slack_conversation`` name another
conversation is a rule about *disclosure* -- may what is in T be shown to the
people in S -- and it says nothing at all about whether a mark may be left in
T. Writing into a conversation the request did not come from is acting where
nobody asked, and the read rule is not the authorisation for it. Should a
reason to name a target ever appear, the argument is additive and the rule it
needs is a write rule that does not exist yet.

**Which turns may react.**

The two request shapes that already name a Slack conversation, and no third:
an inbound Slack turn, whose conversation the transport stamps, and a
scheduled run, whose conversation the scheduler stamps and marks as its own.
The predicate is the one ``interface_deep`` already applies to the history
tool, reused rather than restated.

Not a condition: the history policy word. That word says how far a
conversation's *record* may be read out, and a deployment that reads no
history still marks its own messages -- the connector does it on every turn
already. Coupling the two would take reactions away from an operator who
turned history off, for a reason that has nothing to do with reactions, and an
unexplained limitation is worse than an absent feature.

**The emoji is named, never drawn.**

``reactions.add`` and ``reactions.remove`` take a shortcode name and refuse a
Unicode character with ``invalid_name``. Models pass the character anyway,
because the argument is called ``emoji`` and a character is what that word
means; the field is therefore one field that accepts both, and the conversion
happens here. What cannot be converted is refused with the shortcode form
named in the refusal, rather than forwarded to Slack for an opaque
``invalid_name`` the caller cannot act on in the same turn. A *shortcode* this
module does not know is a different case entirely and passes straight through:
a workspace's own custom emoji has a name and no character at all, and a
recognition list would be the one thing standing between the model and it.

The glyph table below is short and hand-written. It is deliberately not
generated from Unicode's own names: Slack's vocabulary diverges from CLDR in
exactly the common cases -- ``+1``, ``tada``, ``100`` are not CLDR names -- so
a generated table would be confidently wrong about the emoji most likely to be
asked for. ``emoji.list`` is not consulted either, and holding ``emoji:read``
does not change that: it returns a workspace's *custom* emoji only, so it can
neither confirm a standard shortcode nor supply a character for one.

**Both no-ops are successes.**

``already_reacted`` and ``no_reaction`` come back as ``ok: false`` bodies and
are reported here as success. The tool exists to be used for marking, and
marking has to be repeatable: a scheduled run that marks the messages it has
dealt with must be able to run again over the same messages without every
second pass reading as a failure. Nothing was left undone in either case --
the reaction is on the message, or it is off it, which is what was asked for.

**The result says what was done, not what the message now carries.**

``reactions.add`` answers ``{"ok": true}`` and nothing else, so the reaction
list after the call is not in hand. It is not fetched: a second API call per
reaction, to return something the caller did not ask for, is a cost paid on
every use for the sake of a field. Reading a conversation is where reactions
are read.

``file`` and ``file_comment`` are absent from the request because Slack
deprecated them, not because they were not wanted: *"Now that file threads
work the way you'd expect, the ``file`` and ``file_comment`` arguments are
deprecated. Specify only ``channel`` and ``timestamp`` instead."* A file
shared in Slack is a message, and the message's own ``ts`` is how it is
reacted to.

Scope: ``reactions:write``.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from openjiuwen.core.foundation.tool import LocalFunction, Tool, ToolCard

from jiuwenswarm.agents.harness.common.tools.slack_history import (
    SlackWorkspaceClients,
    _as_mapping,
    _safe_error_code,
    _SlackCallFailure,
    redact_credentials,
    shared_slack_workspaces,
)
from jiuwenswarm.common.slack_history_policy import (
    METADATA_TEAM_KEY,
    SlackWorkspaceUnresolved,
)


# Where the connector leaves the conversation and the message this request
# arrived on. ``message_ts`` is the raw, unconverted identifier half of Slack's
# ts; ``timestamp_ms`` beside it is the time half and is lossy, so it is never
# read here.
SLACK_CHANNEL_ID_KEY = "slack_channel_id"
SLACK_MESSAGE_TS_KEY = "message_ts"

# The two Slack methods this tool calls.
_ADD_METHOD = "reactions.add"
_REMOVE_METHOD = "reactions.remove"

# Refusals that mean the reaction is already in the state that was asked for.
_ALREADY_ADDED = "already_reacted"
_ALREADY_REMOVED = "no_reaction"

# How much of a model-supplied value is echoed back inside a refusal. Long
# enough to show what was passed, short enough that a runaway argument does not
# become the whole tool result.
_MAX_ECHOED_CHARS = 60

# Presentation selectors carry no meaning for Slack, which names the emoji
# rather than drawing it, and their presence is invisible to whoever typed it.
_VARIATION_SELECTORS = frozenset({"\ufe0f", "\ufe0e"})

# Slack spells a skin tone as a suffix on the name rather than as part of the
# character: ``+1::skin-tone-4``. The five modifiers map to 2..6; 1 is the
# yellow default and has no modifier.
_SKIN_TONES: dict[str, int] = {
    "\U0001f3fb": 2,
    "\U0001f3fc": 3,
    "\U0001f3fd": 4,
    "\U0001f3fe": 5,
    "\U0001f3ff": 6,
}

#: Characters a model is likely to pass, and the name Slack knows each by.
#:
#: Seeded from the emoji this deployment already uses on its own messages --
#: the acknowledgement, queued and rejected marks, the completed, failed and
#: stopped marks, and the run- and todo-state glyphs the activity card is built
#: from -- so that a model asked to mark a message the way the connector marks
#: one is naming the same emoji rather than a near neighbour. The rest are the
#: reactions people actually leave on a work message: agreement, disagreement,
#: done, attention, a question, waiting, and a short tail of social ones.
#:
#: Kept small on purpose. Every entry is one this module asserts a Slack name
#: for, and a wrong entry is worse than an absent one: an absent character is
#: refused with an instruction the caller can follow, while a wrong name is a
#: reaction that silently lands as something else or not at all.
_GLYPH_SHORTCODES: dict[str, str] = {
    # The connector's own lifecycle marks.
    "\N{EYES}": "eyes",
    "\N{NO ENTRY SIGN}": "no_entry_sign",
    "\N{HOURGLASS WITH FLOWING SAND}": "hourglass_flowing_sand",
    "\N{HOURGLASS}": "hourglass",
    "\N{HEAVY CHECK MARK}": "heavy_check_mark",
    "\N{CROSS MARK}": "x",
    "\N{BLACK SQUARE FOR STOP}": "black_square_for_stop",
    "\N{HIGH VOLTAGE SIGN}": "zap",
    "\N{WHITE HEAVY CHECK MARK}": "white_check_mark",
    "\N{WHITE QUESTION MARK ORNAMENT}": "grey_question",
    # Agreement and its opposite.
    "\N{THUMBS UP SIGN}": "+1",
    "\N{THUMBS DOWN SIGN}": "-1",
    "\N{OK HAND SIGN}": "ok_hand",
    "\N{RAISED HAND}": "raised_hand",
    "\N{WAVING HAND SIGN}": "wave",
    "\N{CLAPPING HANDS SIGN}": "clap",
    "\N{PERSON RAISING BOTH HANDS IN CELEBRATION}": "raised_hands",
    "\N{HANDSHAKE}": "handshake",
    "\N{FLEXED BICEPS}": "muscle",
    "\N{PERSON WITH FOLDED HANDS}": "pray",
    # Done, and marking done.
    "\N{BALLOT BOX WITH CHECK}": "ballot_box_with_check",
    "\N{PARTY POPPER}": "tada",
    "\N{HUNDRED POINTS SYMBOL}": "100",
    "\N{FIRE}": "fire",
    "\N{ROCKET}": "rocket",
    "\N{DIRECT HIT}": "dart",
    "\N{WHITE MEDIUM STAR}": "star",
    "\N{GLOWING STAR}": "star2",
    # Attention, trouble and questions.
    "\N{WARNING SIGN}": "warning",
    "\N{BLACK QUESTION MARK ORNAMENT}": "question",
    "\N{HEAVY EXCLAMATION MARK SYMBOL}": "exclamation",
    "\N{OCTAGONAL SIGN}": "octagonal_sign",
    "\N{BUG}": "bug",
    "\N{ELECTRIC LIGHT BULB}": "bulb",
    "\N{LEFT-POINTING MAGNIFYING GLASS}": "mag",
    "\N{ALARM CLOCK}": "alarm_clock",
    "\N{LARGE RED CIRCLE}": "red_circle",
    "\N{LARGE GREEN CIRCLE}": "large_green_circle",
    "\N{LARGE YELLOW CIRCLE}": "large_yellow_circle",
    # Repetition and reference.
    "\N{ANTICLOCKWISE DOWNWARDS AND UPWARDS OPEN CIRCLE ARROWS}": (
        "arrows_counterclockwise"
    ),
    "\N{CLOCKWISE RIGHTWARDS AND LEFTWARDS OPEN CIRCLE ARROWS}": "repeat",
    "\N{PUSHPIN}": "pushpin",
    "\N{PAPERCLIP}": "paperclip",
    "\N{LINK SYMBOL}": "link",
    "\N{MEMO}": "memo",
    "\N{SPEECH BALLOON}": "speech_balloon",
    # The social tail.
    "\N{HEAVY BLACK HEART}": "heart",
    "\N{GRINNING FACE}": "grinning",
    "\N{SMILING FACE WITH OPEN MOUTH AND SMILING EYES}": "smile",
    "\N{SMILING FACE WITH OPEN MOUTH AND COLD SWEAT}": "sweat_smile",
    "\N{FACE WITH TEARS OF JOY}": "joy",
    "\N{SLIGHTLY SMILING FACE}": "slightly_smiling_face",
    "\N{SMILING FACE WITH SMILING EYES}": "blush",
    "\N{SMILING FACE WITH SUNGLASSES}": "sunglasses",
    "\N{CRYING FACE}": "cry",
    "\N{THINKING FACE}": "thinking_face",
    "\N{SHRUG}": "shrug",
    "\N{FACE WITH PARTY HORN AND PARTY HAT}": "partying_face",
    "\N{ROBOT FACE}": "robot_face",
    "\N{BRAIN}": "brain",
}


class _ReactionRefused(RuntimeError):
    """A refusal decided here, carrying the code and the text to report."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


def slack_reaction_request_metadata(
    metadata: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Return trusted metadata for a request that may react, or fail closed.

    Takes the metadata alone. Whether the *request shape* may name a Slack
    conversation at all is settled by the caller, which already asks that
    question for the history tool and would otherwise be asked it twice with
    two chances to answer differently.

    The one condition left is the one this tool needs: a conversation stamped
    by the transport or the scheduler. Absent, it is not that a reaction is
    disallowed -- there is nowhere to put one.
    """
    if not isinstance(metadata, Mapping):
        return {}
    if not str(metadata.get(SLACK_CHANNEL_ID_KEY) or "").strip():
        return {}
    return dict(metadata)


def _echo(value: Any) -> str:
    """One model-supplied value, safe and short enough to quote in a refusal."""
    text, _redacted, _truncated = redact_credentials(value, cap=_MAX_ECHOED_CHARS)
    return text.strip()


def _emoji_name(raw: Any) -> str:
    """The Slack shortcode for what the caller passed, or a refusal.

    Four passes, in order, and the order is what makes it predictable:

    1. Whitespace and surrounding colons go. ``:tada:``, ``tada`` and
       ``" :tada: "`` are the same request, and a model writes all three.
       ``:wave::skin-tone-2:`` survives this intact, because only the outer
       colons are on the ends.
    2. Anything still ASCII is a name and is passed through lowercased. This is
       the branch a workspace custom emoji takes, so nothing here may reject a
       name for being unfamiliar.
    3. Otherwise it is a character. Presentation selectors are dropped and skin
       tone modifiers are lifted out into Slack's ``::skin-tone-N`` suffix,
       which is where Slack keeps them.
    4. What is left is looked up. A miss is refused, naming the form to use,
       rather than sent to Slack to come back as ``invalid_name``.
    """
    text = str(raw or "").strip()
    if text:
        text = text.strip(":").strip()
    if not text:
        raise _ReactionRefused(
            "emoji_required",
            "emoji is required and was empty. Pass a Slack shortcode without "
            "colons, such as eyes, white_check_mark or tada.",
        )

    if text.isascii():
        return text.lower()

    tones: list[int] = []
    base_chars: list[str] = []
    for char in text:
        if char in _VARIATION_SELECTORS:
            continue
        tone = _SKIN_TONES.get(char)
        if tone is not None:
            tones.append(tone)
            continue
        base_chars.append(char)

    base = "".join(base_chars)
    name = _GLYPH_SHORTCODES.get(base)
    if not name:
        raise _ReactionRefused(
            "emoji_character_not_recognised",
            f"Slack names an emoji and never draws it, so {_echo(raw)} cannot "
            "be sent as it stands, and this tool does not know which name it "
            "goes by. Pass the shortcode instead, without colons -- tada "
            "rather than the party popper, white_check_mark rather than the "
            "tick. Any shortcode is accepted, including one this workspace "
            "added itself, so a name that looks unfamiliar is still worth "
            "passing.",
        )
    return name + "".join(f"::skin-tone-{tone}" for tone in tones)


def _error(code: str, detail: str = "") -> str:
    payload: dict[str, Any] = {"ok": False, "error": code}
    if detail:
        payload["detail"] = detail
    return json.dumps(payload, ensure_ascii=False)


class SlackReactionToolkit:
    """Toolkit scoped to one request's Slack conversation."""

    def __init__(
        self,
        *,
        metadata: dict[str, Any] | None = None,
        metadata_provider: Any | None = None,
        client: Any | None = None,
        workspaces: "SlackWorkspaceClients | None" = None,
    ) -> None:
        self._request_metadata = dict(metadata) if metadata else {}
        self._metadata_provider = metadata_provider
        self._client = client
        self._workspaces = workspaces or shared_slack_workspaces()
        self._bot_token = ""

    def update_runtime_context(
        self,
        *,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Refresh the request-scoped metadata without recreating the tool."""
        self._request_metadata = dict(metadata) if metadata else {}

    def _runtime_metadata(self) -> dict[str, Any]:
        """The trusted metadata for *this* invocation, or nothing.

        Read per call rather than captured at construction: the toolkit is
        built once and answers every request for the life of the process, so a
        conversation captured at construction would be one conversation's id
        forever. A provider that raises answers nothing, which is a refusal on
        the way out and never a fallback conversation.
        """
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
        """Bind this request to the Slack install it arrived from.

        Takes the metadata the caller already read rather than reading it
        again. A provider is read once per request on purpose: it is live, and
        two reads of it are two requests as far as it is concerned.

        A reaction is a write, and one made in the wrong workspace is a mark
        left under somebody's message in a room nobody here asked about, so an
        install that cannot be settled is refused rather than guessed at.
        """
        slack = await self._workspaces.settings_for(
            str(metadata.get(METADATA_TEAM_KEY) or "").strip()
        )
        self._bot_token = str(slack.get("bot_token") or "").strip()

    def _get_client(self) -> Any:
        if self._client is not None:
            return self._client
        try:
            return self._workspaces.client_for(self._bot_token)
        except _SlackCallFailure as exc:
            # The shared resolver speaks the history toolkit's refusal type;
            # this tool answers in its own.
            raise _ReactionRefused(exc.code) from None

    async def _call(
        self, remove: bool, channel: str, timestamp: str, name: str
    ) -> None:
        """Make the one call, and reduce anything that comes back to a code.

        Slack's own argument names are used on the wire -- ``channel``,
        ``timestamp``, ``name`` -- while the model-facing surface uses this
        deployment's vocabulary. The rename is of the card, not of the API.

        Both refusals that mean "already in the state you asked for" return
        here rather than raising, and they arrive by two routes: as a raised
        ``SlackApiError`` carrying an ``ok: false`` body, which is what the SDK
        does, and as a plain ``ok: false`` body, which some versions hand back
        instead.
        """
        client = self._get_client()
        method = client.reactions_remove if remove else client.reactions_add
        settled = _ALREADY_REMOVED if remove else _ALREADY_ADDED
        try:
            response = await method(
                channel=channel, timestamp=timestamp, name=name
            )
        except Exception as exc:  # noqa: BLE001 - SDK error types vary.
            code = _safe_error_code(
                _as_mapping(getattr(exc, "response", None)).get("error")
                or str(exc),
                self._bot_token,
            )
            if code == settled:
                return
            raise _ReactionRefused(code) from None

        data = _as_mapping(response)
        if data.get("ok", True) is False:
            code = _safe_error_code(data.get("error"), self._bot_token)
            if code == settled:
                return
            raise _ReactionRefused(code)

    async def react_to_message(
        self,
        emoji: str,
        message_id: str | None = None,
        remove: bool | None = None,
    ) -> str:
        """Add or remove one emoji reaction on one message in this conversation."""
        request_metadata = self._runtime_metadata()
        try:
            await self._load_settings(request_metadata)
        except SlackWorkspaceUnresolved as unresolved:
            return _error(unresolved.code, unresolved.detail)
        chat_id = str(request_metadata.get(SLACK_CHANNEL_ID_KEY) or "").strip()
        if not chat_id:
            # The provider failed closed, or this turn never had a Slack
            # conversation. Either way there is no conversation to fall back
            # to, and inventing one would be the whole failure this tool is
            # built to make impossible.
            return _error(
                "trusted_slack_channel_context_required",
                "This turn carries no Slack conversation, so there is no "
                "message here to react to. Nothing about the request can "
                "supply one.",
            )

        target = str(message_id or "").strip()
        if not target:
            target = str(request_metadata.get(SLACK_MESSAGE_TS_KEY) or "").strip()
            if not target:
                return _error(
                    "message_id_required",
                    "message_id was not given and this turn was not started by "
                    "a Slack message, so there is no message it could have "
                    "meant. A scheduled run is the usual case. Pass the ts of "
                    "the message to react to.",
                )

        try:
            name = _emoji_name(emoji)
        except _ReactionRefused as refusal:
            return _error(refusal.code, refusal.detail)

        removing = bool(remove)
        try:
            await self._call(removing, chat_id, target, name)
        except _ReactionRefused as refusal:
            return _error(refusal.code, self._detail_for(refusal, name))

        return json.dumps(
            {
                "ok": True,
                "chat_id": chat_id,
                "message_id": target,
                "emoji": name,
                "removed": removing,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )

    def _detail_for(self, refusal: _ReactionRefused, name: str) -> str:
        """What to say about a Slack refusal, where saying more helps.

        Only the codes a caller can do something about get a sentence. The rest
        travel as the code alone: inventing an explanation for a refusal whose
        cause is not known here would be a guess presented as a finding.
        """
        if refusal.detail:
            return refusal.detail
        if refusal.code == "invalid_name":
            return (
                f"Slack does not know an emoji called {name} in this "
                "workspace. Check the spelling of the shortcode; a custom "
                "emoji has to already exist here to be reacted with."
            )
        if refusal.code == "missing_scope":
            return (
                "This Slack app has not been granted reactions:write. An "
                "operator adds it and reinstalls the app; no wording of the "
                "request works around it."
            )
        if refusal.code == "message_not_found":
            return (
                "No message with that message_id is in this conversation. A "
                "ts identifies a message within one conversation only, so one "
                "taken from somewhere else will not be found here."
            )
        if refusal.code in {"not_in_channel", "channel_not_found"}:
            return (
                "This Slack app is not in that conversation and cannot react "
                "in it. Someone already there has to invite it."
            )
        if refusal.code == "is_archived":
            return (
                "That conversation is archived. An archived conversation "
                "takes no reactions until it is unarchived."
            )
        if refusal.code == "too_many_reactions":
            return (
                "That message already carries as many distinct reactions as "
                "Slack allows. One has to come off before another goes on."
            )
        return ""

    def get_tools(self) -> list[Tool]:
        """Return the request-scoped Slack reaction tool."""
        card = ToolCard(
            name="react_to_message",
            description=(
                "Put an emoji reaction on a Slack message, or take one off. "
                "Use it to acknowledge a message, to mark one as dealt with, "
                "or to record an outcome on the message itself. A reaction is "
                "the quiet way to answer: it marks the message in place and "
                "does not notify the conversation the way another post does, "
                "so it is the right response when the point is that something "
                "was seen or handled rather than that something is being said."
                "\nWhich conversation. It acts in the conversation this "
                "request came from and in no other. There is no argument for "
                "naming a different one, and no way to reach a message "
                "elsewhere: a message_id from another conversation is not "
                "found here rather than reacted to somewhere else."
                "\nWhich message. The message that started this turn is the "
                "default. Omit message_id and that is the message marked, "
                "which is what acknowledging a request means. Give message_id "
                "only to mark a different message in this same conversation. "
                "A run that no message started -- a scheduled one -- has no "
                "default to fall back on, and there message_id has to be "
                "given or the call is refused; it is never silently applied "
                "to some other message."
                "\nMessage identifiers. A message_id is an opaque Slack "
                "identifier and not a date. Use one exactly as some result "
                "reported it, and never work one out from a time."
                "\nNaming the emoji. The emoji is named rather than drawn. "
                "Slack takes a shortcode -- eyes, white_check_mark, tada, +1 "
                "-- with or without surrounding colons, and a shortcode is "
                "the only way to name an emoji this workspace added itself, "
                "which has no character form at all. An emoji character is "
                "accepted for the common ones and converted to its name; one "
                "that cannot be converted is refused, and the refusal says to "
                "pass the shortcode. A shortcode is never refused for looking "
                "unfamiliar."
                "\nAdding and removing. One emoji per call, and removing "
                "takes the same emoji that was added; there is no way to "
                "clear every reaction at once."
                "\nRepeating a call. Calling twice is safe. Adding a "
                "reaction that is already there succeeds and changes nothing, "
                "and so does removing one that was never there, so a pass "
                "that marks messages as handled can be run again over the "
                "same messages without the second pass reading as a failure."
                "\nWhat comes back. The result reports what was done and "
                "never what the message now carries. Slack answers a reaction "
                "with an acknowledgement and nothing else, so the reactions "
                "on a message after this call are not available from here and "
                "are not fetched; read the message to see them."
            ),
            input_params={
                "type": "object",
                "required": ["emoji"],
                "properties": {
                    "emoji": {
                        "type": "string",
                        "description": (
                            "Which emoji, named as a Slack shortcode without "
                            "colons -- eyes, white_check_mark, tada, +1. "
                            "Colons around it are accepted and stripped. An "
                            "emoji character is also accepted for the common "
                            "ones and converted to its name, but a shortcode "
                            "always works and is the only way to name one of "
                            "this workspace's own custom emoji."
                        ),
                    },
                    "message_id": {
                        "type": "string",
                        "description": (
                            "Which message, as the ts a result reported for "
                            "it. Omit it to react to the message that started "
                            "this turn, which is the usual case. It must be a "
                            "message in this conversation. When nothing "
                            "started this turn -- a scheduled run -- there is "
                            "no default and this has to be given."
                        ),
                    },
                    "remove": {
                        "type": "boolean",
                        "default": False,
                        "description": (
                            "Take the reaction off instead of putting it on. "
                            "Removes only this app's own reaction, never "
                            "somebody else's."
                        ),
                    },
                },
            },
        )
        return [LocalFunction(card=card, func=self.react_to_message)]


__all__ = [
    "SLACK_CHANNEL_ID_KEY",
    "SLACK_MESSAGE_TS_KEY",
    "SlackReactionToolkit",
    "slack_reaction_request_metadata",
]
