# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Resolving ``models.decision``, and the off switch that has no key.

Unset environment variables resolve to the empty string rather than vanishing,
so a template that lists every key -- which the key allow-list forces -- still
has to read as unconfigured on a deployment that never filled one in.
"""

from __future__ import annotations

import pytest

from jiuwenswarm.common.typed_decision import (
    JevAdapter,
    TypedDecisionClient,
    build_client,
    decision_model,
    select_adapter_factory,
)

CONFIGURED = {
    "models": {
        "decision": {
            "api_base": "https://openrouter.example/api/v1",
            "api_key": "sk-live",
            "model_name": "typesafe/jev-1.13",
            "model_config_obj": {"extra_body": {"provider": {"order": ["TypeSafe"]}}},
        }
    }
}


def test_a_filled_block_resolves_whole() -> None:
    settings = decision_model(CONFIGURED)
    assert settings is not None
    assert settings.model_name == "typesafe/jev-1.13"
    assert settings.extra_body == {"provider": {"order": ["TypeSafe"]}}


@pytest.mark.parametrize(
    "config",
    [
        None,
        {},
        {"models": {}},
        {"models": {"decision": None}},
        # What the shipped template resolves to with no DECISION_* variable
        # set: every key present, every value empty.
        {
            "models": {
                "decision": {
                    "api_base": "",
                    "api_key": "",
                    "model_name": "",
                    "model_config_obj": {
                        "extra_body": {"provider": {"order": ["TypeSafe"]}}
                    },
                }
            }
        },
        {"models": {"decision": {"api_base": "https://x", "api_key": "k"}}},
    ],
    ids=[
        "no-config",
        "empty-config",
        "no-decision-key",
        "decision-is-blank",
        "template-with-nothing-filled-in",
        "no-model-name",
    ],
)
def test_nothing_configured_builds_no_client(config) -> None:
    """The absence of a client is the whole contract of the off switch."""
    assert decision_model(config) is None
    assert build_client(config) is None


def test_a_filled_block_builds_a_client_over_the_claiming_adapter() -> None:
    client = build_client(CONFIGURED)
    assert isinstance(client, TypedDecisionClient)
    assert client.name == "jev"
    assert isinstance(client.adapter, JevAdapter)
    assert client.adapter.url.endswith("/api/alpha/decisions")


def test_the_adapter_is_chosen_by_the_model_name_and_never_by_the_caller() -> None:
    class Stub:
        name = "stub"

        @staticmethod
        def claims(model_name: str) -> bool:
            return model_name.startswith("stub/")

    registry = (Stub, JevAdapter)
    assert select_adapter_factory("stub/one", registry) is Stub
    assert select_adapter_factory("typesafe/jev-1.13", registry) is JevAdapter
    # Nothing claimed it, so the last entry answers. A second implementation
    # is a second entry here and no change at any call site.
    assert select_adapter_factory("something/else", registry) is JevAdapter
