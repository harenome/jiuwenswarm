# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""``models.decision`` in the shipped template, key by key.

The template doubles as the key allow-list on upgrade: ``_deep_merge`` deletes
an operator's key that the template does not list. That has cost this
deployment its configuration three times, so the block is checked here rather
than trusted to review.

The second thing checked is that the shipped block reads as **off**. Every
value is an environment-variable placeholder, and an unset variable resolves
to the empty string rather than vanishing, so a deployment that never set one
has to come out with no decision model at all.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from jiuwenswarm.common.config import resolve_env_vars
from jiuwenswarm.common.typed_decision import decision_model

TEMPLATE = (
    Path(__file__).resolve().parents[2]
    / "jiuwenswarm"
    / "resources"
    / "config.yaml"
)

EXPECTED_KEYS = {"api_base", "api_key", "model_name", "model_config_obj"}


def shipped_block() -> dict:
    loaded = yaml.safe_load(TEMPLATE.read_text(encoding="utf-8"))
    return loaded["models"]["decision"]


def test_every_key_of_the_block_ships_in_the_template() -> None:
    assert set(shipped_block()) == EXPECTED_KEYS


def test_the_provider_block_ships_and_requires_no_parameters() -> None:
    """A model declaring no supported parameters routes nowhere under that flag."""
    model_config_obj = shipped_block()["model_config_obj"]
    assert model_config_obj["extra_body"]["provider"]["order"] == ["TypeSafe"]
    assert "require_parameters" not in yaml.safe_dump(model_config_obj)


def test_the_block_has_no_enable_flag() -> None:
    """Absence is the off switch. A second switch would allow a live
    configuration the runtime ignores, which is a state somebody has to debug.
    """
    block = shipped_block()
    assert "enabled" not in block
    assert "enable" not in block


def test_the_shipped_template_reads_as_off(monkeypatch) -> None:
    for name in ("DECISION_API_BASE", "DECISION_API_KEY", "DECISION_MODEL_NAME"):
        monkeypatch.delenv(name, raising=False)
    resolved = resolve_env_vars(shipped_block())
    assert resolved["api_base"] == ""
    assert resolved["model_name"] == ""
    assert decision_model({"models": {"decision": resolved}}) is None


def test_the_template_turns_on_when_the_variables_are_set(monkeypatch) -> None:
    monkeypatch.setenv("DECISION_API_BASE", "https://openrouter.example/api/v1")
    monkeypatch.setenv("DECISION_API_KEY", "sk-live")
    monkeypatch.setenv("DECISION_MODEL_NAME", "typesafe/jev-1.13")
    resolved = resolve_env_vars(shipped_block())
    settings = decision_model({"models": {"decision": resolved}})
    assert settings is not None
    assert settings.model_name == "typesafe/jev-1.13"
    assert settings.extra_body == {"provider": {"order": ["TypeSafe"]}}
