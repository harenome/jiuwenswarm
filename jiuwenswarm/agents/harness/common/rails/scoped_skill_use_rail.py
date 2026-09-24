"""Apply a request's Skill catalog and required instructions to the main agent."""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import contextmanager
from contextvars import ContextVar, Token
from typing import Any, Iterator

from openjiuwen.harness.prompts import PromptSection
from openjiuwen.harness.prompts.sections import SectionName
from openjiuwen.harness.rails import SkillUseRail


_AVAILABLE: ContextVar[frozenset[str] | None] = ContextVar(
    "agent_skills_available", default=None
)
_REQUIRED: ContextVar[tuple[str, ...]] = ContextVar(
    "agent_skills_required", default=()
)
_REQUIRED_CONTENT: ContextVar[str] = ContextVar(
    "agent_skills_required_content", default=""
)
SCOPED_SKILLS_RUN_CONTEXT_KEY = "jiuwenswarm.scoped_skills"
_ROUND_TOKENS_KEY = "_scoped_skill_tokens"
_MODEL_TOKENS_KEY = "_scoped_skill_model_tokens"
_TOOL_TOKENS_KEY = "_scoped_skill_tool_tokens"


def with_scoped_skill_run_context(
    inputs: dict[str, Any], params: dict[str, Any]
) -> dict[str, Any]:
    """Send Skill settings to the SDK supervisor's next round."""
    skill_params = {
        key: params[key]
        for key in ("agent_skills_available", "agent_skills_required")
        if key in params
    }
    if not skill_params:
        return inputs
    updated = dict(inputs)
    raw_run = updated.get("run")
    run = dict(raw_run) if isinstance(raw_run, Mapping) else {}
    raw_context = run.get("context")
    context = dict(raw_context) if isinstance(raw_context, Mapping) else {}
    raw_extra = context.get("extra")
    extra = dict(raw_extra) if isinstance(raw_extra, Mapping) else {}
    extra[SCOPED_SKILLS_RUN_CONTEXT_KEY] = skill_params
    context["extra"] = extra
    run["context"] = context
    run.setdefault("kind", "normal")
    updated["run"] = run
    return updated


def bind_scoped_skills(params: dict[str, Any]) -> tuple[Token, Token, Token]:
    """Bind trusted per-turn Skill settings; an absent catalog inherits all."""
    has_available = "agent_skills_available" in params
    raw_available = params.get("agent_skills_available")
    raw_required = params.get("agent_skills_required", ())
    if has_available and (
        not isinstance(raw_available, list)
        or any(
            not isinstance(name, str) or not name.strip() or name != name.strip()
            for name in raw_available
        )
    ):
        raise ValueError("agent_skills_available must be a list of Skill names")
    if not isinstance(raw_required, (list, tuple)) or any(
        not isinstance(name, str) or not name.strip() or name != name.strip()
        for name in raw_required
    ):
        raise ValueError("agent_skills_required must be a list of Skill names")
    available = frozenset(raw_available) if has_available else None
    required = tuple(dict.fromkeys(raw_required))
    if available is not None and any(name not in available for name in required):
        missing = [name for name in required if name not in available]
        raise ValueError(
            "Required Skills are outside agent_skills_available: "
            + ", ".join(missing)
        )
    return (
        _AVAILABLE.set(available),
        _REQUIRED.set(required),
        _REQUIRED_CONTENT.set(""),
    )


def reset_scoped_skills(tokens: tuple[Token, Token, Token]) -> None:
    _AVAILABLE.reset(tokens[0])
    _REQUIRED.reset(tokens[1])
    _REQUIRED_CONTENT.reset(tokens[2])


def scoped_skill_names() -> frozenset[str] | None:
    """Return the active catalog for Symphony's live visibility provider."""
    return _AVAILABLE.get()


def required_skill_text() -> str:
    return _REQUIRED_CONTENT.get()


@contextmanager
def unrestricted_skill_inventory() -> Iterator[None]:
    """Build a session inventory independently of its first request's scope."""
    token = _AVAILABLE.set(None)
    try:
        yield
    finally:
        _AVAILABLE.reset(token)


def _visible(skills: list[Any]) -> list[Any]:
    allowed = _AVAILABLE.get()
    if allowed is None:
        return skills
    return [skill for skill in skills if skill.name in allowed]


def _bind_inner_scope(ctx: Any, key: str) -> None:
    """Restore the round's scope in an SDK model or tool worker task."""
    if key in ctx.extra:
        return
    run_context = ctx.extra.get("run_context")
    extra = getattr(run_context, "extra", None)
    if not isinstance(extra, Mapping):
        return
    params = extra.get(SCOPED_SKILLS_RUN_CONTEXT_KEY)
    if params is None:
        return
    if not isinstance(params, dict):
        raise ValueError("Invalid scoped Skill run context")
    ctx.extra[key] = bind_scoped_skills(params)


def _reset_inner_scope(ctx: Any, key: str) -> None:
    tokens = ctx.extra.pop(key, None)
    if tokens is not None:
        reset_scoped_skills(tokens)


class ScopedSkillUseRail(SkillUseRail):
    """Filter prompt and Skill tools without changing the persisted baseline."""

    async def before_invoke(self, ctx: Any) -> None:
        # SDK hot reload can register the same rail callback twice.
        if _ROUND_TOKENS_KEY in ctx.extra:
            return
        run_context = getattr(getattr(ctx, "inputs", None), "run_context", None)
        extra = getattr(run_context, "extra", None)
        raw_params = (
            extra.get(SCOPED_SKILLS_RUN_CONTEXT_KEY, {})
            if isinstance(extra, Mapping)
            else {}
        )
        if not isinstance(raw_params, dict):
            raise ValueError("Invalid scoped Skill run context")
        tokens = bind_scoped_skills(raw_params)
        ctx.extra[_ROUND_TOKENS_KEY] = tokens
        try:
            await super().before_invoke(ctx)
        except BaseException:
            ctx.extra.pop(_ROUND_TOKENS_KEY, None)
            reset_scoped_skills(tokens)
            raise

    async def after_invoke(self, ctx: Any) -> None:
        try:
            await super().after_invoke(ctx)
        finally:
            tokens = ctx.extra.pop(_ROUND_TOKENS_KEY, None)
            if tokens is not None:
                reset_scoped_skills(tokens)

    async def before_model_call(self, ctx: Any) -> None:
        _bind_inner_scope(ctx, _MODEL_TOKENS_KEY)
        try:
            await super().before_model_call(ctx)
        except BaseException:
            _reset_inner_scope(ctx, _MODEL_TOKENS_KEY)
            raise

    async def after_model_call(self, ctx: Any) -> None:
        _reset_inner_scope(ctx, _MODEL_TOKENS_KEY)

    async def on_model_exception(self, ctx: Any) -> None:
        _reset_inner_scope(ctx, _MODEL_TOKENS_KEY)

    async def before_tool_call(self, ctx: Any) -> None:
        _bind_inner_scope(ctx, _TOOL_TOKENS_KEY)

    async def after_tool_call(self, ctx: Any) -> None:
        _reset_inner_scope(ctx, _TOOL_TOKENS_KEY)

    async def on_tool_exception(self, ctx: Any) -> None:
        _reset_inner_scope(ctx, _TOOL_TOKENS_KEY)

    def get_skills_for_session(self, session: Any = None) -> list[Any]:
        return _visible(super().get_skills_for_session(session))

    def _get_session_baseline(self, ctx: Any) -> list[Any]:
        return _visible(super()._get_session_baseline(ctx))

    def _build_skills_section(
        self, skills: list[Any] | None = None, query: str | None = None
    ):
        return super()._build_skills_section(
            _visible(list(self.skills) if skills is None else skills), query=query
        )

    def _build_runtime_skill_change_content(
        self, additions: list[Any], removals: list[Any], baseline_skills: list[Any]
    ) -> str:
        return super()._build_runtime_skill_change_content(
            _visible(additions), _visible(removals), _visible(baseline_skills)
        )

    async def _sync_skill_prompt_and_attachment(self, ctx: Any) -> None:
        await super()._sync_skill_prompt_and_attachment(ctx)
        required = _REQUIRED.get()
        if not required:
            _REQUIRED_CONTENT.set("")
            return
        available = {skill.name: skill for skill in self.skills}
        missing = [name for name in required if name not in available]
        if missing:
            raise ValueError(
                "Required Skills are not installed or enabled: "
                + ", ".join(missing)
            )
        parts = [
            "# Required Skills\n"
            "The `SKILL.md` text for each required Skill is below. "
            "Follow each instruction on this turn according to its stated conditions. "
            "You do not need `skill_tool` to read this text."
        ]
        for name in required:
            path = available[name].directory / "SKILL.md"
            try:
                content = path.read_text(encoding="utf-8")
            except OSError as exc:
                raise ValueError(f"Required Skill {name!r} cannot be read: {exc}") from exc
            parts.append(f"## Required Skill: {name}\n\n{content}")
        if self.system_prompt_builder is None:
            raise ValueError("Required Skills need a system prompt builder")
        prompt_mode = getattr(self.system_prompt_builder, "mode", None)
        if getattr(prompt_mode, "value", prompt_mode) == "none":
            raise ValueError("Required Skills cannot load with prompt_mode=none")
        required_content = "\n\n".join(parts)
        _REQUIRED_CONTENT.set(required_content)
        language = self.system_prompt_builder.language
        native = self.system_prompt_builder.get_section(SectionName.SKILLS)
        catalog = native.render(language) if native is not None else ""
        self.system_prompt_builder.add_section(
            PromptSection(
                name=SectionName.SKILLS,
                content={language: "\n\n".join(part for part in (catalog, required_content) if part)},
                priority=native.priority if native is not None else 50,
            )
        )
