"""An approval prompt raised during a non-streaming round.

Several connectors build their inbound chat.send without ``is_stream``, so the
round runs through ``process_message_impl`` and answers with one text payload.
That collector only kept ``llm_output`` / ``answer`` text, so an interrupt chunk
left the round with empty content while the agent went on waiting for an answer
nobody had been shown. These pin the text stand-in it now collects instead.
"""
from __future__ import annotations

from openjiuwen.core.common.constants.constant import INTERACTION
from openjiuwen.core.session.interaction.interaction import InteractionOutput
from openjiuwen.core.session.stream.base import OutputSchema
from openjiuwen.core.single_agent.interrupt.response import ToolCallInterruptRequest

from jiuwenswarm.agents.harness.common.rails.permissions.root_permission_queue_rail import (
    bind_root_permission_request,
    reset_root_permission_request,
)
from jiuwenswarm.common.interrupt_prompt import render_prompt_as_text
from jiuwenswarm.server.runtime.agent_adapter.interface_deep import JiuWenSwarmDeepAdapter


def _interrupt_chunk() -> OutputSchema:
    return OutputSchema(
        type=INTERACTION,
        index=0,
        payload=InteractionOutput(
            id="call_1",
            value=ToolCallInterruptRequest(
                message="Approve 2 experiences for `demo` (skill)?",
                tool_name="evolve_skill_experiences",
                tool_call_id="call_1",
                ui_options=[
                    {"label": "Allow Once", "value": "allow_once", "description": "Allow this change"},
                    {"label": "Reject", "value": "reject", "description": "Skip this change"},
                ],
            ),
        ),
    )


def _parse() -> dict:
    """Parse one interrupt chunk the way a bound turn does.

    ``_parse_stream_chunk`` reads the root permission queue for every
    ``__interaction__`` frame, and asking for it outside a bound request raises
    rather than answering "no queue". A turn is always bound by the time a
    chunk reaches this, so binding here is what makes the unit test run the
    production path instead of the swallowed-exception one.
    """
    token = bind_root_permission_request(
        root_session_id="non-stream-session",
        request_id="non-stream-request",
        enabled=False,
        queue=None,
    )
    try:
        return JiuWenSwarmDeepAdapter._parse_stream_chunk(_interrupt_chunk())
    finally:
        reset_root_permission_request(token)


def test_an_interrupt_chunk_renders_to_text_a_text_only_round_can_carry():
    parsed = _parse()

    assert parsed is not None
    assert parsed["event_type"] == "chat.ask_user_question"

    text = render_prompt_as_text(parsed)
    assert "Approve 2 experiences for `demo` (skill)?" in text
    assert "Allow Once" in text
    assert "Reject" in text


def test_the_rendered_text_does_not_offer_an_id_the_round_cannot_take_back():
    parsed = _parse()

    assert "call_1" not in render_prompt_as_text(parsed)
