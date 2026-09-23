# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""What the decision model is asked about a Slack message, and what it is told.

This is the platform half of the typed-decision feature. It holds the
questions, the state built from a Slack event, the rule our own code applies to
the answers, and the log line the result is written to. The generic half
underneath it knows none of this and never sees the word Slack.

**Everything here runs in shadow.** The gate asks, records what it would have
decided, and changes nothing: no turn is skipped, no output withheld, no
reaction sent, nothing forwarded to the turn. The rule below was fitted to
thirty lines of one fixture and has never met real traffic, so shadow is how it
earns enforcement. The log line is therefore the deliverable rather than a
debug aid, and it holds every raw answer so a later analysis can re-fit any
threshold without asking the model again.

Three rules govern the questions, each of them measured rather than reasoned.

**Question ids never reach the model.** ``audience`` and ``claim`` are names
for our code. All the meaning has to sit inside ``instructions``, so a question
may not lean on its own name.

**Criteria decide the answer, not only the vocabulary.** The same state and the
same underlying question, asked with substantive option descriptions and then
with bare labels, came back opposite. Question wordings need the care a prompt
gets.

**Write every question about the message, never about the situation.** A
question asking how much a message would help "this channel right now" reads
the room's unresolved state and attributes it to whatever message happens to be
last. Two lines of the labelled transcript that said nothing at all scored
above a true positive that way. Adding "judge ``last_message`` on its own"
moved one margin from -0.13 to +0.48 and another from +0.08 to +0.76.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import time
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from copy import deepcopy
from pathlib import Path
from typing import Any

from jiuwenswarm.common.typed_decision import (
    Answer,
    Question,
    TypedDecisionClient,
    TypedDecisionError,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# The questions
# ---------------------------------------------------------------------------

#: Who a request is directed at. A choice rather than a pair of nouls, because
#: naming is not addressing: "I will ask alice about that" names alice and
#: addresses the room, and "charlie said the same" names charlie and addresses
#: nobody. A noul over "the message names someone other than the bot" scores
#: that ambiguity instead of resolving it. Mutually exclusive alternatives are
#: what a choice is for, and it measured 28 of 30 against the noul pair.
AUDIENCE = "audience"

#: Whether someone already gave what this message asks for. It decides
#: ``context`` and it is the v2 wording, which asks about ``last_message``
#: specifically; the v1 wording sat inside its own jitter at +0.08.
ALREADY_ANSWERED = "already_answered"

#: Whether this occurrence requests an answer or acknowledgement. An earlier
#: answer can exist while a repeated request still calls for a response.
#: This answer is independent of ``already_answered``. No disposition clause
#: uses it yet. The model does not decide whether DMs receive replies.
RESPONSE_REQUESTED = "response_requested"

#: Whether the message asserts something as fact. It exists because of a
#: failure the measurements found: a rule keyed on ``audience`` alone drops a
#: false claim that is stated rather than asked, since asserting something asks
#: nothing of anybody. In the labelled transcript the planted false claim
#: survived only because it ended in "right?".
CLAIM = "claim"

#: Whether the message asks for something to be done rather than said. It is
#: the only route to ``silent``, and it is read together with ``audience``:
#: alone its gap is -0.22, and read only where the request is for the bot or
#: the room the two genuine cases read 0.93 and 0.95 while nothing else exceeds
#: 0.12.
NEEDS_WORK = "needs_work"

#: Whether a later turn may need this. It fired on zero of thirty lines in
#: every variant tried, which is why the ``ignore`` rung was removed rather
#: than gated on it. It is asked and logged so shadow can say whether that
#: holds against real traffic.
WORTH_REMEMBERING = "worth_remembering"

#: Whether a person would react to this message. Worded about what a member
#: would do and naming no kind of message: a wording listing good news,
#: finished work, a surprising result and a joke produced 5.5% where the
#: neutral wording produced 0.8%. Asked and logged only. Shadow sends no
#: reaction, and the companion question choosing *which* reaction is not asked
#: at all, because its criteria are a list of one workspace's emoji and no
#: configuration key holds one.
DESERVES_EMOJI = "deserves_emoji"

#: The four values ``audience`` may take. They are the platform-facing half of
#: the vocabulary: the core defines the shape and the connector maps a value
#: onto an actual conversation.
AUDIENCE_NOBODY = "nobody"
AUDIENCE_ROOM = "the room"
AUDIENCE_PERSON = "a person"
AUDIENCE_BOT = "you"

QUESTIONS: Mapping[str, Question] = {
    AUDIENCE: Question(
        type="choice",
        instructions=(
            "Who is the request in `last_message` directed at? Judge"
            " `last_message` on its own. An earlier message that was directed"
            " at someone is not a reason to answer for this one."
        ),
        criteria={
            AUDIENCE_NOBODY: (
                "It asks nothing of anybody. A remark, a joke, an observation,"
                " or a statement of what the sender has done or intends to do."
            ),
            AUDIENCE_ROOM: (
                "It puts something to everyone present, and any of them could"
                " take it up. Nobody in particular is named."
            ),
            AUDIENCE_PERSON: (
                "It is aimed at one named participant other than the one named"
                " in `bot_name`, and nobody else is expected to answer."
            ),
            AUDIENCE_BOT: (
                "It is aimed at the participant named in `bot_name`. Mentioning"
                " that name in passing is not this: the request itself has to"
                " be for that participant."
            ),
        },
    ),
    ALREADY_ANSWERED: Question(
        type="noul",
        instructions=(
            "Someone in `recent_messages` already gave what `last_message`"
            " specifically asks for."
        ),
    ),
    RESPONSE_REQUESTED: Question(
        type="noul",
        instructions=(
            "`last_message` requests an answer or acknowledgement for this"
            " occurrence. Merely referring to an earlier request or answer"
            " does not count."
        ),
    ),
    CLAIM: Question(
        type="noul",
        instructions="`last_message` asserts something as fact.",
    ),
    NEEDS_WORK: Question(
        type="noul",
        instructions=(
            "`last_message` asks for something to be done or changed, not only"
            " for something to be said."
        ),
    ),
    WORTH_REMEMBERING: Question(
        type="noul",
        instructions=(
            "`last_message` holds something that is still needed later in this"
            " conversation."
        ),
    ),
    DESERVES_EMOJI: Question(
        type="noul",
        instructions=(
            "A member of this conversation would add an emoji reaction to"
            " `last_message`."
        ),
    ),
}

class QuestionConfigurationError(ValueError):
    """The operator question file is invalid or unreadable."""


def _json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise QuestionConfigurationError("duplicate JSON key")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise QuestionConfigurationError("non-finite JSON number")


def _json_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise QuestionConfigurationError("non-finite JSON number")
    return number


def load_questions(path: str | Path | None = None) -> dict[str, Question]:
    """Merge an optional JSON object into the defaults. Null disables a question."""
    questions = deepcopy(dict(QUESTIONS))
    if path is None or path == "":
        return questions
    if not isinstance(path, (str, Path)):
        raise QuestionConfigurationError("question file path must be a string")
    try:
        with Path(path).expanduser().open(encoding="utf-8") as stream:
            overrides = json.load(
                stream,
                object_pairs_hook=_json_object,
                parse_constant=_reject_constant,
                parse_float=_json_float,
            )
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise QuestionConfigurationError("cannot read question file as JSON") from exc
    if not isinstance(overrides, dict):
        raise QuestionConfigurationError("question file must contain an object")
    for question_id, definition in overrides.items():
        if not question_id.strip():
            raise QuestionConfigurationError("question ID must not be empty")
        if definition is None:
            questions.pop(question_id, None)
            continue
        if not isinstance(definition, dict):
            raise QuestionConfigurationError("question definition must be an object or null")
        if set(definition) - {"type", "instructions", "criteria"}:
            raise QuestionConfigurationError("unknown question field")
        if not isinstance(definition.get("type"), str):
            raise QuestionConfigurationError("question type must be a string")
        if not isinstance(definition.get("instructions"), str):
            raise QuestionConfigurationError("question instructions must be a string")
        criteria = definition.get("criteria")
        if definition["type"] == "choice" and isinstance(criteria, dict):
            if any(
                not key.strip()
                or not isinstance(value, (str, dict, list))
                or (isinstance(value, str) and not value.strip())
                for key, value in criteria.items()
            ):
                raise QuestionConfigurationError(
                    "choice options need names and string, object, or array descriptions"
                )
        try:
            questions[question_id] = Question(**definition)
        except (TypedDecisionError, TypeError) as exc:
            raise QuestionConfigurationError(
                "invalid question type, instructions, or criteria"
            ) from exc
    return questions


def question_definitions(questions: Mapping[str, Question]) -> dict[str, Any]:
    """Return the effective definitions for observation records."""
    return {key: question.as_request_value() for key, question in questions.items()}


def question_set_hash(questions: Mapping[str, Question]) -> str:
    payload = json.dumps(
        question_definitions(questions), sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def question_changes(questions: Mapping[str, Question]) -> Iterable[tuple[str, str]]:
    """Describe effective differences from the built-in questions."""
    for question_id in sorted(set(QUESTIONS) | set(questions)):
        if question_id not in questions:
            yield "disabled", question_id
        elif question_id not in QUESTIONS:
            yield "added", question_id
        elif (
            questions[question_id].as_request_value()
            != QUESTIONS[question_id].as_request_value()
        ):
            yield "replaced", question_id


def disposition_is_compatible(questions: Mapping[str, Question]) -> bool:
    """Check every definition that the disposition rule reads."""
    return all(
        key in questions
        and questions[key].as_request_value() == QUESTIONS[key].as_request_value()
        for key in (AUDIENCE, ALREADY_ANSWERED, NEEDS_WORK, CLAIM)
    )


# ---------------------------------------------------------------------------
# The rule
# ---------------------------------------------------------------------------

#: Stay quiet on the absence of warrant, never on the presence of knowledge.
#: The gate's authority is over whether anyone is asking, which is checkable
#: from the message. It has no authority over whether the bot knows something
#: useful, which needs a retrieval it has not run.
DISPOSITION_CONTEXT = "context"
DISPOSITION_SILENT = "silent"
DISPOSITION_HERE = "here"
DISPOSITION_LATER = "later"

#: The ladder, floor first. Each rung adds one thing to the rung below, and
#: the only branch is at the last. ``context`` is the floor because the rung
#: below it, which discarded a message outright, was removed: the question
#: feeding it fired on zero of thirty lines and every line it would have taken
#: was already taken by another clause. ``elsewhere`` is reserved for a turn's
#: own use and is a tool call rather than a decision taken here.
DISPOSITIONS = (
    DISPOSITION_CONTEXT,
    DISPOSITION_SILENT,
    DISPOSITION_HERE,
    DISPOSITION_LATER,
)

#: Where a noul counts as high. The midpoint, which sits inside every gap the
#: rule was measured across: ``needs_work`` read with ``audience`` separates
#: 0.93 and 0.95 from nothing above 0.12, and ``already_answered`` v2 measured
#: a margin of +0.76. It is a convenience rather than a fitted value, and the
#: log holds every raw answer precisely so a threshold can be re-fitted from
#: real traffic without asking the model again. **Before this rule is ever
#: enforced, this number belongs in configuration with its provenance beside
#: it**, in the way every other workspace-local number does.
HIGH = 0.5


def _noul(answers: Mapping[str, Answer], question_id: str) -> float:
    answer = answers.get(question_id)
    if answer is None or answer.number is None:
        raise ValueError(f"the rule needs a numeric {question_id!r} answer")
    return float(answer.number)


def disposition(answers: Mapping[str, Answer], *, high: float = HIGH) -> str:
    """What our code decides, from the answers the model gave.

    The clauses are ordered and the first match wins. Two of them exist
    because of errors an earlier rule made, and both are worth naming.

    ``claim`` routes to ``later`` rather than to ``here``. It reads 0.77 on a
    true status update and 0.74 on the false claim it exists to catch, so no
    threshold separates the two. It is sound as a veto against going quiet and
    unsound as a reason to speak; used as a reason to speak it forced two
    posts, which is the only error a reader of the channel ever sees.

    ``needs_work`` is read together with ``audience``, because on its own it
    fires on "I'll take the harness section" and "I still owe you slides". The
    question was never wrong. The rule was reading it as an answer to *whose*
    work, which ``audience`` already gives.
    """
    audience = answers.get(AUDIENCE)
    if audience is None or not isinstance(audience.value, str):
        raise ValueError("the rule needs an audience answer")
    if audience.value == AUDIENCE_PERSON:
        return DISPOSITION_CONTEXT
    if _noul(answers, ALREADY_ANSWERED) >= high:
        return DISPOSITION_CONTEXT
    if _noul(answers, NEEDS_WORK) >= high and audience.value in (
        AUDIENCE_BOT,
        AUDIENCE_ROOM,
    ):
        return DISPOSITION_SILENT
    if _noul(answers, CLAIM) >= high:
        return DISPOSITION_LATER
    if audience.value == AUDIENCE_BOT:
        return DISPOSITION_HERE
    return DISPOSITION_LATER


# ---------------------------------------------------------------------------
# The state
# ---------------------------------------------------------------------------

#: How many prior lines go into the state. Depth 0 won on the labelled
#: transcript for a reason that does not generalise -- both of its positives
#: happen to stand alone -- while depth 12 measured +0.79 and does not rest on
#: that accident. The model does read the window: the two hardest negatives
#: climb from 0.06 at depth zero to 1.15 by depth six.
RECENT_MESSAGE_DEPTH = 12

#: Keys a state may never hold, because each one states the answer to a
#: question being asked. An early probe sent ``bot_was_addressed: false`` and
#: then asked whether the bot was addressed; the 0.03 it returned may have been
#: reading the field rather than the text, and that run is not trusted. Naming
#: the bot is what lets the model work the answer out. Telling it the answer is
#: not.
FORBIDDEN_STATE_KEYS = frozenset(
    {
        "addressed",
        "bot_was_addressed",
        "is_mention",
        "trigger",
        *QUESTIONS,
    }
)


def bot_identity(bot_name: str, bot_user_id: str) -> str:
    """What the state calls the bot, so ``audience`` can be answered.

    The mention token travels with the name because Slack message text holds
    the token and never the name. Without it the model reads ``<@U08ABC>`` in
    one field and a name in another and cannot connect them, which is the one
    thing ``audience`` needs to do.
    """
    name = str(bot_name or "").strip() or "the assistant"
    token = str(bot_user_id or "").strip()
    return f"{name} (mentioned as <@{token}>)" if token else name


def message_entry(
    author: str,
    text: str,
    *,
    ts: str = "",
    thread_ts: str | None = None,
    file_count: int | None = None,
    attachment_count: int | None = None,
) -> dict[str, Any]:
    """Keep message facts that the model can use to interpret the text."""
    entry: dict[str, Any] = {
        "author": str(author or "someone"), "text": str(text or "")
    }
    if ts:
        entry["ts"] = str(ts)
    if thread_ts is not None:
        entry["thread_ts"] = str(thread_ts)
        if not thread_ts or thread_ts == ts:
            entry["thread_position"] = "root"
        else:
            entry["thread_position"] = "reply" if ts else "unknown"
    for key, count in (("file_count", file_count), ("attachment_count", attachment_count)):
        if isinstance(count, int) and not isinstance(count, bool) and count >= 0:
            entry[key] = count
    return entry


def _copy_message(entry: Mapping[str, Any]) -> dict[str, Any]:
    """Copy only supported message facts into the model input."""
    return message_entry(
        entry.get("author", ""),
        entry.get("text", ""),
        ts=entry.get("ts", ""),
        thread_ts=entry.get("thread_ts"),
        file_count=entry.get("file_count"),
        attachment_count=entry.get("attachment_count"),
    )


def build_state(
    *,
    channel: str,
    bot_name: str,
    recent_messages: Sequence[Mapping[str, Any]],
    last_message: Mapping[str, Any],
    members: Iterable[str] | None = None,
    chat_type: str = "",
) -> dict[str, Any]:
    """The named object the questions are answered against.

    A record rather than flat text, so each part has a name the instructions
    can reference by path and the relationships between the parts stay clear.

    ``recent_messages`` **includes the bot's own turns**. They were absent from
    every measurement taken so far, which is why the real reason two labelled
    lines were silent, that the bot had already spoken, was invisible to the
    model; it inferred from the human replies instead.

    Three things are kept out. No description of any corpus: a constant
    sentence saying a body of knowledge exists invites exactly the guess the
    gate is kept away from, and what the bot knows is settled by retrieval
    inside a turn. No key that answers a question being asked, which is checked
    rather than trusted. And no field the caller has not named, so a future
    addition is a deliberate one.
    """
    window = [_copy_message(entry) for entry in recent_messages]
    if members is None:
        seen: list[str] = []
        for entry in (*window, last_message):
            author = str(entry.get("author") or "").strip()
            if author and author not in seen:
                seen.append(author)
        members = seen
    state = {
        "channel": str(channel or ""),
        "members": list(members),
        "recent_messages": window,
        "last_message": _copy_message(last_message),
        "bot_name": str(bot_name or ""),
        "conversation_kind": {
            "im": "one_to_one_dm",
            "mpim": "group_dm",
            "channel": "public_channel",
            "group": "private_channel",
        }.get(chat_type, "unknown"),
        "event_kind": "message",
        # Channel history can omit thread replies.
        "history_scope": "channel",
    }
    leaked = FORBIDDEN_STATE_KEYS & set(state)
    if leaked:
        raise ValueError(f"the state must not answer a question it asks: {leaked}")
    return state


# ---------------------------------------------------------------------------
# The log line
# ---------------------------------------------------------------------------

#: The prefix every shadow record opens with, so the lines can be selected out of
#: a mixed log in bulk. The payload after it is one JSON object on one line.
LOG_PREFIX = "[SlackChannel] decision shadow:"

#: The payload's own version. An analysis reading months of these needs to know
#: which shape it has in front of it, and a record with no version cannot say.
LOG_VERSION = 1

#: Distinguish questionnaire revisions while preserving the log envelope.
#: Revision 2 adds response_requested for observation. The outcome rule stays
#: unchanged. Logs without this field used the original six questions.
QUESTION_SET_VERSION = 2

# Records without this field used the original text-only state.
STATE_VERSION = 2


def answer_record(answer: Answer) -> dict[str, Any]:
    """One answer, with everything an analysis needs and nothing invented.

    The raw value travels, never a word band of our own. A number alone has no
    scale a reader shares, and converting it to "high" invents a mapping that
    is lossy, inconsistent between question types, and one more thing to get
    wrong. ``confidence``, ``probabilities`` and ``legend`` travel where the
    implementation returned them and are absent where it did not.
    """
    record: dict[str, Any] = {"type": answer.type, "value": answer.value}
    if answer.confidence is not None:
        record["confidence"] = answer.confidence
    if answer.probabilities is not None:
        record["probabilities"] = dict(answer.probabilities)
    if answer.legend is not None:
        record["legend"] = dict(answer.legend)
    return record


def decision_record(
    *,
    identity: Mapping[str, Any],
    answers: Mapping[str, Answer] | None,
    outcome: str | None,
    adapter: str,
    latency_ms: int,
    window: int,
    error: str = "",
    question_hash: str = "",
    outcome_reason: str = "",
) -> dict[str, Any]:
    """One shadow decision, as the log holds it.

    **No message text, ever.** These lines persist and the content is the
    users'. What travels is the identity needed to find the message again --
    the workspace, the conversation, the timestamp -- and the answers.

    A failed call is recorded in the same shape as a successful one, with
    ``outcome`` naming the failure. An analysis that saw only the successes
    would have no denominator and could not tell a rule that holds from a model
    that answered a third of the time.
    """
    # The identity goes in first so the fields below always win. A caller
    # passing a key this record already names would otherwise overwrite the
    # disposition with a Slack field of the same name, silently.
    record: dict[str, Any] = {str(key): value for key, value in identity.items()}
    record.update(
        {
            "v": LOG_VERSION,
            "question_set_version": QUESTION_SET_VERSION,
            "state_version": STATE_VERSION,
            # Stated in the record itself. A line that does not say it changed
            # nothing is a line somebody can read as enforcement.
            "shadow": True,
            "outcome": outcome,
            "adapter": adapter,
            "latency_ms": latency_ms,
            "window": window,
        }
    )
    if question_hash:
        record["question_set_hash"] = question_hash
    if outcome_reason:
        record["outcome_reason"] = outcome_reason
    if error:
        record["error"] = error
    if answers:
        record["answers"] = {
            question_id: answer_record(answer)
            for question_id, answer in sorted(answers.items())
        }
    return record


def format_decision(record: Mapping[str, Any]) -> str:
    """The payload as one line, ordered so a diff of two lines is readable."""
    return json.dumps(record, sort_keys=True, separators=(",", ":"), default=str)


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------

#: Seconds a shadow decision may take before it is abandoned. It is a second
#: ceiling under the client's own deadline: the client's covers its attempts,
#: and this one covers everything the task does, the conversation window
#: included.
SHADOW_BUDGET_SECONDS = 8.0

RecentMessages = Callable[[str, int, str], Awaitable[Sequence[Mapping[str, Any]]]]


class ShadowDecisionGate:
    """Ask the decision model, record the answer, and change nothing.

    **The isolation is structural rather than careful.** ``observe`` is an
    ordinary function that returns ``None``: there is no value for a caller to
    read and no awaitable for a caller to wait on. It contains no ``await``, so
    the handler that calls it does not yield. Everything it does runs in a task
    of its own, under a budget, with every exception caught, and the only thing
    that task can produce is a line in the log.
    """

    def __init__(
        self,
        client: TypedDecisionClient,
        *,
        recent_messages: RecentMessages | None = None,
        depth: int = RECENT_MESSAGE_DEPTH,
        budget: float = SHADOW_BUDGET_SECONDS,
        questions: Mapping[str, Question] = QUESTIONS,
    ) -> None:
        self._client = client
        self._recent_messages = recent_messages
        self._depth = depth
        self._budget = budget
        self._questions = deepcopy(dict(questions))
        self._question_set_hash = question_set_hash(self._questions)
        self._disposition_compatible = disposition_is_compatible(self._questions)
        logger.info(
            "[SlackChannel] decision questions: %s",
            format_decision({
                "question_set_hash": self._question_set_hash,
                "questions": question_definitions(self._questions),
            }),
        )
        # Held so a shutdown can cancel what is still in flight, and discarded
        # on completion so the set cannot grow without bound. Nothing reads a
        # task's result.
        self._tasks: set[asyncio.Task[None]] = set()

    def observe(
        self,
        *,
        channel: str,
        bot_name: str,
        last_message: Mapping[str, Any],
        identity: Mapping[str, Any],
        chat_type: str = "",
    ) -> None:
        """Start one shadow decision. Returns nothing, raises nothing.

        The return type is the guarantee. A caller has nothing to read, so no
        caller can branch on what the model said.
        """
        try:
            task = asyncio.get_running_loop().create_task(
                self._observe(
                    channel=channel,
                    bot_name=bot_name,
                    last_message=last_message,
                    identity=identity,
                    chat_type=chat_type,
                )
            )
        except Exception:  # noqa: BLE001 - a shadow may not raise into a turn
            logger.debug("[SlackChannel] shadow decision not started", exc_info=True)
            return
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _observe(
        self,
        *,
        channel: str,
        bot_name: str,
        last_message: Mapping[str, Any],
        identity: Mapping[str, Any],
        chat_type: str = "",
    ) -> None:
        """The whole of what a shadow decision does, and it cannot leave here."""
        started = time.monotonic()
        window: Sequence[Mapping[str, Any]] = ()
        answers: Mapping[str, Answer] | None = None
        outcome: str | None = "error"
        error = ""
        try:
            async with asyncio.timeout(self._budget):
                window = await self._window(channel, str(identity.get("ts") or ""))
                state = build_state(
                    channel=channel,
                    bot_name=bot_name,
                    recent_messages=window,
                    last_message=last_message,
                    chat_type=chat_type,
                )
                answers = await self._client.decide(state, self._questions)
                outcome = (
                    disposition(answers) if self._disposition_compatible else None
                )
        except asyncio.CancelledError:
            # A shutdown took the task away. Nothing is logged and nothing is
            # retried: the message it was about has already been handled.
            raise
        except Exception as exc:  # noqa: BLE001 - a shadow may not raise into a turn
            # Every failure lands here and stops here: a timeout, an
            # unreachable endpoint, an unreadable body, a malformed state.
            # ``TimeoutError`` and ``TypedDecisionError`` are named in the
            # comment rather than in the clause because the clause has to be
            # total. A shadow that let one class of failure past would be a
            # shadow that could reach a turn.
            outcome = "declined"
            error = f"{type(exc).__name__}: {exc}"
        latency_ms = int((time.monotonic() - started) * 1000)
        try:
            logger.info(
                "%s %s",
                LOG_PREFIX,
                format_decision(
                    decision_record(
                        identity=identity,
                        answers=answers,
                        outcome=outcome,
                        adapter=self._client.name,
                        latency_ms=latency_ms,
                        window=len(window),
                        error=error,
                        question_hash=self._question_set_hash,
                        outcome_reason=(
                            "question_definitions_changed"
                            if outcome is None else ""
                        ),
                    )
                ),
            )
        except Exception:  # noqa: BLE001 - a log line may not raise either
            logger.debug("[SlackChannel] shadow decision unlogged", exc_info=True)

    async def _window(self, channel: str, before_ts: str) -> Sequence[Mapping[str, Any]]:
        """The prior lines, or none of them.

        An empty window is a worse state rather than a failure. The record says
        how many lines went in, so an analysis can separate the decisions taken
        with context from the ones taken without it.
        """
        if self._recent_messages is None or self._depth <= 0:
            return ()
        try:
            return await self._recent_messages(channel, self._depth, before_ts)
        except Exception:  # noqa: BLE001
            logger.debug(
                "[SlackChannel] shadow decision read no window: channel=%s",
                channel,
                exc_info=True,
            )
            return ()

    async def close(self) -> None:
        """Cancel whatever is still in flight, and wait for it to stop."""
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
