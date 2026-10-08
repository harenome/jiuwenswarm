"""Approval prompts remain visible in non-streaming rounds."""

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
    """Bind request context before parsing the interaction chunk."""
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
    assert "call_1" not in text
