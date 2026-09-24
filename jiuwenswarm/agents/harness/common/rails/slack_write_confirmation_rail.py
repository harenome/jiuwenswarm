# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Ask before a Slack post reaches people the conversation it came from cannot.

``channels.slack.write: members`` lets a turn name another conversation and
holds every named one to ``members(T) subset-of members(S)`` -- post only where
everybody who will see it could already have seen the conversation it came from.
A target that satisfies that is written silently. A target that reaches anybody
else is not refused: it is put to the person who started the turn, and written
only if they agree.

**This is a rail because a tool body cannot ask anything.** A tool returns a
string; it has no way to stop, put a question in front of somebody, and carry on
with their answer. The interrupt rails do exactly that, before the tool runs, and
this is one of them.

**It decides nothing.** The judgement lives in the toolkit, in
``SlackPostToolkit.widening_question``, which answers either the sentence to ask
or ``None``. That is deliberate: the same membership read has to settle what the
rail asks about and what the tool then does, and two readings of the rule would
eventually disagree about one call -- the worst shape of which is a question
answered "yes" about a post the tool then makes somewhere else.

**The question states what is widening.** "Post to #general?" cannot be answered
by somebody who does not already know what is at stake. "This will also be seen
by four people who are not in this conversation: …" can. The toolkit writes that
sentence for the same reason it computes the membership.

**Nothing is remembered.** Every other confirmation in this repository offers
"remember this", and this one does not. What is being approved is an audience --
these people, this once -- and an audience is exactly what changes between one
call and the next. A remembered approval would carry a decision about four named
people onto a later post reaching forty different ones, which is the decision
nobody took.

A refused confirmation is reported to the model as a refusal in the shape the
posting tools already use, so a model handling one refusal handles them all.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

from openjiuwen.core.foundation.llm.schema.tool_call import ToolCall
from openjiuwen.core.single_agent.interrupt.response import InterruptRequest
from openjiuwen.core.single_agent.rail.base import AgentCallbackContext
from openjiuwen.harness.rails.interrupt.confirm_rail import ConfirmPayload
from openjiuwen.harness.rails.interrupt.interrupt_base import (
    BaseInterruptRail,
    InterruptDecision,
)

from jiuwenswarm.agents.harness.common.rails.interrupt.permission_options import (
    ALLOW_ONCE,
    REJECT,
    resolve_permission_action,
)
from jiuwenswarm.agents.harness.common.tools.slack_post import (
    WIDENING_TOOL_NAMES,
    SlackPostToolkit,
)

logger = logging.getLogger(__name__)

#: Above the permission rail's 90, so that a turn which has to answer both
#: questions is asked the specific one -- "these four people will see it" --
#: rather than the generic "approve post_message?", which says nothing the
#: specific one does not.
RAIL_PRIORITY = 95

#: The two buttons. Values come from the shared vocabulary rather than being
#: written here, because the side that renders them and the side that parses the
#: answer back have to agree, and an unrecognised answer is treated as a refusal
#: -- a person clicking "yes" and having the call refused is the failure this
#: shared table exists to prevent.
_OPTIONS: list[dict[str, str]] = [
    {
        "value": ALLOW_ONCE,
        "label": "Post it",
        "description": "Send the message to that conversation",
    },
    {
        "value": REJECT,
        "label": "Do not post it",
        "description": "Leave the message unsent",
    },
]

_DECLINED = (
    "the person who asked was shown what this message would widen to and said"
    " not to post it. Do not call this tool again with the same conversation;"
    " post into the conversation this turn came from, or ask them where it"
    " should go"
)


class SlackWriteConfirmationRail(BaseInterruptRail):
    """Put a widening Slack post to the person who asked for the turn."""

    priority: int = RAIL_PRIORITY

    def __init__(self, toolkit: SlackPostToolkit) -> None:
        super().__init__(tool_names=sorted(WIDENING_TOOL_NAMES))
        self._toolkit = toolkit

    async def resolve_interrupt(
        self,
        ctx: AgentCallbackContext,
        tool_call: Optional[ToolCall],
        user_input: Optional[Any],
        auto_confirm_config: Optional[dict] = None,
    ) -> InterruptDecision:
        """Approve, ask, or refuse one call of ``post_message`` or ``edit_message``.

        ``auto_confirm_config`` is accepted and not read, for the reason the
        module docstring gives: what is approved here is an audience, and an
        audience is what differs between two calls.

        Everything that is not a question approves. A call whose arguments are
        malformed, whose policy word does not confirm, or whose membership could
        not be read is passed through to the tool, which refuses it in its own
        shape with its own code. Asking somebody to approve a call that was never
        going to happen wastes their attention and teaches them to click through.
        """
        tool_name = str(getattr(ctx.inputs, "tool_name", "") or "")
        arguments = self._arguments(ctx, tool_call)

        try:
            question = await self._toolkit.widening_question(tool_name, arguments)
        except Exception:  # noqa: BLE001 - a rail must not take the turn down.
            # The toolkit re-runs the same check and refuses the call itself, so
            # approving here is not a decision to post: it is a decision not to
            # ask about something that is about to fail.
            logger.exception(
                "[SlackWriteConfirmationRail] the audience check raised for"
                " tool=%s; the tool itself will refuse the call",
                tool_name,
            )
            return self.approve()

        if not question:
            return self.approve()
        if user_input is None:
            return self.interrupt(
                InterruptRequest(
                    message=question,
                    payload_schema=ConfirmPayload.to_schema(),
                    ui_options=list(_OPTIONS),
                )
            )

        approved = self._approved(user_input)
        if approved is None:
            # The answer came back in a shape nothing here recognises. Asked
            # again rather than read as either answer: a mis-parsed "yes" posts
            # something nobody agreed to, and a mis-parsed "no" reports a
            # refusal the person never made.
            logger.warning(
                "[SlackWriteConfirmationRail] the answer to a widening"
                " confirmation could not be read (%s); asking again",
                type(user_input).__name__,
            )
            return self.interrupt(
                InterruptRequest(
                    message=question,
                    payload_schema=ConfirmPayload.to_schema(),
                    ui_options=list(_OPTIONS),
                )
            )
        if approved:
            return self.approve()
        return self.reject(
            tool_result=json.dumps(
                {
                    "ok": False,
                    "error": "write_confirmation_declined",
                    "detail": _DECLINED,
                },
                ensure_ascii=False,
            )
        )

    @staticmethod
    def _arguments(
        ctx: AgentCallbackContext, tool_call: Optional[ToolCall]
    ) -> "dict[str, Any] | None":
        """The call's arguments as a mapping, or ``None`` if they cannot be read.

        Both places they can be: the rail's own ``tool_args``, which an earlier
        rail may have rewritten, and the tool call itself. The first wins,
        because the arguments that will actually be sent are the ones worth
        asking about.
        """
        raw = getattr(ctx.inputs, "tool_args", None)
        if raw is None and tool_call is not None:
            raw = getattr(tool_call, "arguments", None)
        if isinstance(raw, dict):
            return raw
        if isinstance(raw, (str, bytes)):
            try:
                parsed = json.loads(raw)
            except (ValueError, TypeError):
                return None
            return parsed if isinstance(parsed, dict) else None
        return None

    @staticmethod
    def _approved(user_input: Any) -> "bool | None":
        """Whether the person agreed, or ``None`` when the answer is unreadable.

        Three shapes reach here, one per client family: the structured
        ``ConfirmPayload`` the web and ACP paths build, the mapping a JSON
        transport leaves it as, and the bare option string Slack's buttons and
        the TUI send back. The last is resolved through the shared vocabulary
        rather than compared against a word written here, so a client sending
        the label where another sends the value is understood either way.
        """
        if isinstance(user_input, ConfirmPayload):
            return bool(user_input.approved)
        if isinstance(user_input, dict):
            if "approved" in user_input:
                try:
                    return bool(ConfirmPayload.model_validate(user_input).approved)
                except Exception:  # noqa: BLE001 - pydantic error types vary.
                    return None
            for key in ("value", "label", "option", "answer"):
                if user_input.get(key):
                    return SlackWriteConfirmationRail._approved(user_input[key])
            return None
        if isinstance(user_input, str):
            action = resolve_permission_action(user_input)
            if action is None:
                return None
            return action != REJECT
        return None


__all__ = ["RAIL_PRIORITY", "SlackWriteConfirmationRail"]
