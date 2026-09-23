# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Turning ``models.decision`` into a client, or into nothing.

```yaml
models:
  decision:
    api_base: ${DECISION_API_BASE}
    api_key: ${DECISION_API_KEY}
    model_name: ${DECISION_MODEL_NAME}
    model_config_obj:
      extra_body:
        provider:
          order: [TypeSafe]
```

**There is no enable flag, and there must not be one.** An absent or unfilled
``models.decision`` is already the off switch. A second switch would create a
state where the model is configured and ignored, which is a state somebody has
to debug.

Unset environment variables resolve to the empty string rather than
disappearing, so the template can list every key -- which it has to, since the
template is a key allow-list and an unlisted key is removed from an operator's
configuration on upgrade -- while an untouched deployment still reads as off.
**Empty is off.** A block naming no model, no host or no key cannot produce a
decision, and pretending otherwise would turn a missing variable into a
request that fails once per message.

``model_config_obj.extra_body`` is merged into the request body at the top
level, which is where an OpenRouter-compatible endpoint reads ``provider``.
``require_parameters`` is deliberately absent from the template: a model
declaring no supported parameters routes nowhere under it.

Selecting the implementation costs no key of its own. Each adapter declares
which model names it claims, and the first claimant answers; with no claimant
the default adapter does. A second implementation is a second entry in the
registry below, and no caller changes.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Protocol

from jiuwenswarm.common.typed_decision.adapters.base import DecisionAdapter
from jiuwenswarm.common.typed_decision.adapters.jev import JevAdapter
from jiuwenswarm.common.typed_decision.client import (
    DEFAULT_ATTEMPTS,
    DEFAULT_TIMEOUT_SECONDS,
    TypedDecisionClient,
)

logger = logging.getLogger(__name__)

#: Where the block sits. Under ``models`` rather than under any one platform,
#: because every caller of the protocol reaches it -- a connector, the cron
#: scheduler and the permissions rail ask the same kind of question.
CONFIG_SECTION = "models"
CONFIG_KEY = "decision"


@dataclass(frozen=True)
class DecisionModel:
    """The connection identity of a decision model, and nothing more."""

    model_name: str
    api_base: str
    api_key: str
    extra_body: Mapping[str, Any]


class AdapterFactory(Protocol):
    """How an implementation is built from a resolved configuration block."""

    def claims(self, model_name: str) -> bool: ...

    def __call__(self, **kwargs: Any) -> DecisionAdapter: ...


#: Every implementation, in the order they are offered a model name. The last
#: entry is the default and answers for a name nothing else claimed.
ADAPTERS: tuple[Any, ...] = (JevAdapter,)


def decision_model(config: Mapping[str, Any] | None) -> DecisionModel | None:
    """Read ``models.decision``, or return ``None`` where it says nothing.

    ``None`` is the off switch and the caller's whole contract: with nothing
    configured, no client is built and no request is ever made.
    """
    if not isinstance(config, Mapping):
        return None
    models = config.get(CONFIG_SECTION)
    if not isinstance(models, Mapping):
        return None
    block = models.get(CONFIG_KEY)
    if not isinstance(block, Mapping):
        return None
    model_name = str(block.get("model_name") or "").strip()
    api_base = str(block.get("api_base") or "").strip()
    api_key = str(block.get("api_key") or "").strip()
    if not model_name or not api_base or not api_key:
        return None
    model_config_obj = block.get("model_config_obj")
    extra_body: Mapping[str, Any] = {}
    if isinstance(model_config_obj, Mapping):
        raw = model_config_obj.get("extra_body")
        if isinstance(raw, Mapping):
            extra_body = MappingProxyType(dict(raw))
    return DecisionModel(
        model_name=model_name,
        api_base=api_base,
        api_key=api_key,
        extra_body=extra_body,
    )


def select_adapter_factory(
    model_name: str,
    adapters: Sequence[Any] = ADAPTERS,
) -> Any:
    """The implementation that answers for this model name.

    Nothing about the caller reaches this choice, which is what keeps a second
    implementation from being a second call site.
    """
    if not adapters:
        raise LookupError("no typed-decision adapters are registered")
    for factory in adapters:
        claims = getattr(factory, "claims", None)
        if callable(claims) and claims(model_name):
            return factory
    return adapters[-1]


def build_client(
    config: Mapping[str, Any] | None,
    *,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    attempts: int = DEFAULT_ATTEMPTS,
    adapters: Sequence[Any] = ADAPTERS,
    **adapter_kwargs: Any,
) -> TypedDecisionClient | None:
    """Build the client ``models.decision`` describes, or return ``None``.

    ``None`` means nothing is configured. It is not an error and is not
    logged at warning level: an operator who has not turned this on has
    nothing to fix.
    """
    settings = decision_model(config)
    if settings is None:
        return None
    factory = select_adapter_factory(settings.model_name, adapters)
    adapter = factory(
        model_name=settings.model_name,
        api_base=settings.api_base,
        api_key=settings.api_key,
        timeout=timeout,
        extra_body=settings.extra_body,
        **adapter_kwargs,
    )
    logger.info(
        "[typed_decision] decision model configured: adapter=%s model=%s",
        getattr(adapter, "name", "unknown"),
        settings.model_name,
    )
    return TypedDecisionClient(adapter, timeout=timeout, attempts=attempts)
