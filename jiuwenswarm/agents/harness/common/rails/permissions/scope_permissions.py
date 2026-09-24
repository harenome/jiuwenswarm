# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.

"""The ``permissions`` section, resolved where it is enforced.

``delivery`` and ``agent`` are settled in the gateway, because that is where
``channels.<platform>`` is loaded. ``permissions`` is settled here instead, in
the runtime, at the tool call. That is the only place that knows the tool and its
arguments, and the only place that can turn a refusal into something the model
reads. Both processes import the same ``common/scopes`` package, so the
capability table and the composition rules are identical on either side of the
wire and nothing about a scope is serialised between them.

The conversation comes from two ContextVars the request handler sets. The
``PermissionContext`` beside this module cannot supply it: ``setup_permission_context``
returns ``None`` for an ordinary turn, because it builds a context only for the
digital-avatar scene, or when memory is off. An ordinary Slack conversation has
no ``PermissionContext`` at all, so a rule hung off one would never fire for the
conversations it was written for. ``TOOL_PERMISSION_CHANNEL_ID`` is set
unconditionally at every runtime entry point, and ``TOOL_PERMISSION_CHAT_ID`` is
set beside it.
"""

from __future__ import annotations

import logging
from typing import Any, Mapping

from jiuwenswarm.agents.harness.common.rails.permissions.tool_permission_context import (
    TOOL_PERMISSION_CHANNEL_ID,
    TOOL_PERMISSION_CHAT_ID,
)
from jiuwenswarm.common.scopes import (
    PEOPLE_KEY,
    ROLES_KEY,
    Scope,
    compile_scopes,
    denied_tool_level,
    narrow_config_for_scopes,
)

logger = logging.getLogger(__name__)

# One entry, keyed on what it was compiled from. The permission rail asks on
# every first check of every tool call, and recompiling there would re-emit the
# whole warning set each time; keying on the raw input means a config reload
# recompiles once and emits the warnings once.
#
# The key spans all three blocks, because all three are compiled together: a
# ``roles:`` edit changes which ids a scope resolves to without touching the
# ``scopes:`` list, and a key that watched only the list would keep serving the
# old answer after such an edit.
_compiled_key: "str | None" = None
_compiled: tuple[Scope, ...] = ()


#: What both entry points say when a scope cannot be settled. Both are answers
#: to a callback agent-core owns: the scene hook and the permissions snapshot.
#: Neither has a caller that would catch an exception, so one raised from here
#: takes the turn down.
#: The permission block is evaluable without any scope, and a deployment that
#: has written no scope is already running on it, so that is what both answer
#: with.
_UNAVAILABLE = (
    "[scopes] %s could not be settled for this conversation; no scope narrows"
    " permissions and the permissions block applies as written"
)


def runtime_scopes() -> tuple[Scope, ...]:
    """The compiled ``scopes:`` list, or ``()`` if it cannot be read.

    Never raises. Reading the config and compiling it are both inside the guard.
    ``compile_scopes`` warns about what it cannot honour instead of raising, but
    that is a property of the module next door, which this one may not assume,
    and the two callers below have no guard of their own.
    """
    global _compiled_key, _compiled
    try:
        from jiuwenswarm.common.config import get_config

        data = get_config()
        if not isinstance(data, Mapping):
            return ()

        raw = data.get("scopes")
        people = data.get(PEOPLE_KEY)
        roles = data.get(ROLES_KEY)
        key = repr((raw, people, roles))
        if key == _compiled_key:
            return _compiled

        channels = data.get("channels")
        compiled = compile_scopes(
            raw,
            channels_config=channels if isinstance(channels, Mapping) else None,
            people=people,
            roles=roles,
        )
    except Exception:
        logger.warning(_UNAVAILABLE, "the top-level scopes list", exc_info=True)
        return ()
    # Published together, so the key always describes the tuple beside it.
    _compiled, _compiled_key = compiled, key
    return _compiled


def current_conversation() -> "tuple[str | None, str | None]":
    """The ``(channel, chat)`` this turn is running for, or ``(None, None)``."""
    channel = (TOOL_PERMISSION_CHANNEL_ID.get() or "").strip()
    chat = (TOOL_PERMISSION_CHAT_ID.get() or "").strip()
    return (channel or None, chat or None)


def _scoped_conversation() -> "tuple[tuple[Scope, ...], str | None, str | None] | None":
    """The scopes to fold and the conversation to fold them for, or ``None``.

    ``None`` means no scope narrows anything here, and every caller answers it
    with the thing it was handed. It covers all three ways that happens: a turn
    with no conversation on it at all, a deployment whose ``scopes:`` is empty,
    and a config that could not be read.
    """
    channel, chat = current_conversation()
    if channel is None and chat is None:
        return None
    scopes = runtime_scopes()
    if not scopes:
        return None
    return scopes, channel, chat


def narrow_permission_config(permission_config: Any) -> Any:
    """Narrow ``permission_config`` by whatever scopes match this conversation.

    Returns the argument unchanged when nothing matches, which is every
    deployment with an empty ``scopes:``, and when the narrowing cannot be
    computed at all.
    """
    scoped = _scoped_conversation()
    if scoped is None:
        return permission_config
    scopes, channel, chat = scoped
    try:
        return narrow_config_for_scopes(
            permission_config, scopes, channel=channel, chat=chat
        )
    except Exception:
        logger.warning(_UNAVAILABLE, "the narrowed permission config", exc_info=True)
        return permission_config


def scope_refuses(tool_name: str) -> bool:
    """Whether a scope denies ``tool_name`` in this conversation.

    Only ``deny``. An ``ask`` is left to :func:`narrow_permission_config`, where
    the engine can still raise the interrupt an ask means; the scene hook this
    feeds has no word for "ask" and answering one there could only turn it into
    an approval or a refusal.

    ``False`` when the levels cannot be settled, which is the same fallback
    :func:`narrow_permission_config` makes and for the same reason. The two must
    agree: a refusal here that the narrowed snapshot does not also express would
    be a denial with nothing behind it.
    """
    scoped = _scoped_conversation()
    if scoped is None:
        return False
    scopes, channel, chat = scoped
    try:
        level = denied_tool_level(scopes, tool_name, channel=channel, chat=chat)
    except Exception:
        logger.warning(_UNAVAILABLE, "the tool levels a scope settles", exc_info=True)
        return False
    return level is not None


def reset_cache() -> None:
    """Forget the compiled scopes. For tests, and for a config reload."""
    global _compiled_key, _compiled
    _compiled_key = None
    _compiled = ()


__all__ = [
    "current_conversation",
    "narrow_permission_config",
    "reset_cache",
    "runtime_scopes",
    "scope_refuses",
]
