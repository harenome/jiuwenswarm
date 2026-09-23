# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.

"""``scopes:``, ``people:`` and ``roles:`` in the shipped templates.

The template merge used to rebuild an operator's file from the template's key
set: a key the code reads but no template ships was **deleted from their
config** on the next start, silently. That is not hypothetical here -- it has
already destroyed a per-channel settings block and about 140 lines of other
operator configuration, and it had happened once before that.

The merge is additive now and deletes nothing by default, so an omitted key is
no longer an erasure. The guard stands anyway, for the reason it was written:
``prune=True`` still deletes, it is the path a deprecation mechanism would use,
and the template is where a new option's default reaches a config written
before that option existed. A ``scopes:`` the templates do not list is a key
that never acquires a default and is the first thing a pruning pass removes.

So the guard is two-part, and both parts matter:

* the key is present in every shipped template;
* an operator's rules survive the merge unchanged, which is what decides the
  shape the template ships the key in.

The shape differs between the three keys and the difference is the point.
``scopes:`` is a list of whole rules, so there are no operator-named sub-keys
for a prune to measure against and it may ship as ``[]``. ``people:`` and
``roles:`` are maps whose every key is a name the operator chose, so an empty
mapping would say "no name is allowed here" and delete all of them. They ship
bare.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from jiuwenswarm.common.config import migrate_config_from_template

_RESOURCES = Path(__file__).resolve().parents[3] / "jiuwenswarm" / "resources"
_SHIPPED_TEMPLATES = (
    "config.yaml",
    "config.team.distributed.leader.yaml",
    "config.team.distributed.teammate.yaml",
)


@pytest.mark.parametrize("template_name", _SHIPPED_TEMPLATES)
def test_every_shipped_template_carries_the_key(template_name: str):
    data = yaml.safe_load((_RESOURCES / template_name).read_text(encoding="utf-8"))
    assert "scopes" in data, template_name


@pytest.mark.parametrize("template_name", _SHIPPED_TEMPLATES)
def test_the_key_ships_as_an_empty_list(template_name: str):
    # A list, not a mapping. Entries are whole rules rather than sub-keys the
    # operator names, so there is no open-ended key set for the merge to prune
    # -- which is why this may ship as [] where an operator-keyed map may not.
    data = yaml.safe_load((_RESOURCES / template_name).read_text(encoding="utf-8"))
    assert data["scopes"] == [], template_name


@pytest.mark.parametrize("template_name", _SHIPPED_TEMPLATES)
def test_the_key_is_top_level_rather_than_under_channels(template_name: str):
    # Rules about requests, not properties of a connector. A pseudo-channel such
    # as __cron__ has no channels block to live in at all.
    data = yaml.safe_load((_RESOURCES / template_name).read_text(encoding="utf-8"))
    assert "scopes" not in data.get("channels", {})


def test_an_operators_rules_survive_the_merge(tmp_path: Path):
    template_path = tmp_path / "template.yaml"
    user_config_path = tmp_path / "config.yaml"
    template_path.write_text("scopes: []\n", encoding="utf-8")
    # Both sections, because the merge sees a rule as one opaque list entry and
    # a test written against only the section that existed first would go on
    # passing if it ever stopped being opaque.
    user_config_path.write_text(
        "scopes:\n"
        "  - match: {channel: slack, chat: \"C-RESEARCH\"}\n"
        "    delivery:\n"
        "      mode: [mention, url]\n"
        "      prompt: \"Fetch the link first.\"\n"
        "    agent:\n"
        "      model_name: \"pretend-v1\"\n"
        "  - match: {channel: slack}\n"
        "    delivery: {mode: [+has_file]}\n",
        encoding="utf-8",
    )

    migrate_config_from_template(template_path, user_config_path)
    merged = yaml.safe_load(user_config_path.read_text(encoding="utf-8"))

    assert merged["scopes"] == [
        {
            "match": {"channel": "slack", "chat": "C-RESEARCH"},
            "delivery": {"mode": ["mention", "url"], "prompt": "Fetch the link first."},
            "agent": {"model_name": "pretend-v1"},
        },
        {"match": {"channel": "slack"}, "delivery": {"mode": ["+has_file"]}},
    ]


def _config_missing_the_key(tmp_path: Path) -> tuple[Path, Path]:
    template_path = tmp_path / "template.yaml"
    user_config_path = tmp_path / "config.yaml"
    template_path.write_text("preferred_language: zh\n", encoding="utf-8")
    user_config_path.write_text(
        "preferred_language: zh\n"
        "scopes:\n"
        "  - match: {channel: slack}\n"
        "    delivery: {mode: [all]}\n",
        encoding="utf-8",
    )
    return template_path, user_config_path


def test_a_template_without_the_key_would_delete_them_when_pruning(tmp_path: Path):
    # Not a supported configuration -- the guard that says why the key has to
    # ship in the same commit that introduces it. Pruning is where the deletion
    # lives now, and is the path a deprecation mechanism would use.
    template_path, user_config_path = _config_missing_the_key(tmp_path)

    migrate_config_from_template(template_path, user_config_path, prune=True)
    merged = yaml.safe_load(user_config_path.read_text(encoding="utf-8"))

    assert "scopes" not in merged


def test_the_default_merge_leaves_them_alone(tmp_path: Path):
    """Stated here so the guard above cannot be read as today's default."""
    template_path, user_config_path = _config_missing_the_key(tmp_path)

    migrate_config_from_template(template_path, user_config_path)
    merged = yaml.safe_load(user_config_path.read_text(encoding="utf-8"))

    assert merged["scopes"] == [{"match": {"channel": "slack"}, "delivery": {"mode": ["all"]}}]


def test_the_shipped_template_compiles_to_no_rules_and_says_nothing():
    from jiuwenswarm.common.scopes import compile_scopes

    data = yaml.safe_load((_RESOURCES / "config.yaml").read_text(encoding="utf-8"))
    said: list[str] = []
    assert compile_scopes(data["scopes"], warn=lambda m, *a: said.append(m)) == ()
    assert said == []


# --------------------------------------------------------------------------
# people: and roles:
# --------------------------------------------------------------------------


@pytest.mark.parametrize("template_name", _SHIPPED_TEMPLATES)
@pytest.mark.parametrize("key", ["people", "roles"])
def test_every_shipped_template_carries_the_directory_keys(
    template_name: str, key: str
):
    # A key the code reads and the template omits is the one a pruning pass
    # deletes, and these two are read by ``compile_scopes`` on both sides of
    # the wire.
    data = yaml.safe_load((_RESOURCES / template_name).read_text(encoding="utf-8"))
    assert key in data, f"{template_name}: {key}"


@pytest.mark.parametrize("template_name", _SHIPPED_TEMPLATES)
@pytest.mark.parametrize("key", ["people", "roles"])
def test_the_directory_keys_ship_bare_rather_than_as_an_empty_mapping(
    template_name: str, key: str
):
    # Every key under them is a name the operator invented, which this template
    # cannot predict. Shipped as ``{}`` the merge would read it as the complete
    # set of allowed sub-keys and delete every person and every role.
    data = yaml.safe_load((_RESOURCES / template_name).read_text(encoding="utf-8"))
    assert data[key] is None, f"{template_name}: {key}"


def test_an_operators_people_and_roles_survive_the_merge(tmp_path: Path):
    template_path = tmp_path / "template.yaml"
    user_config_path = tmp_path / "config.yaml"
    template_path.write_text("people:\nroles:\n", encoding="utf-8")
    user_config_path.write_text(
        "people:\n"
        "  harenome: {slack: \"U000000AAAA\", feishu: \"ou_a1b2c3\"}\n"
        "  boss: {slack: \"U_boss\"}\n"
        "roles:\n"
        "  admin: [harenome, boss]\n",
        encoding="utf-8",
    )

    migrate_config_from_template(template_path, user_config_path, prune=True)
    merged = yaml.safe_load(user_config_path.read_text(encoding="utf-8"))

    assert merged["people"] == {
        "harenome": {"slack": "U000000AAAA", "feishu": "ou_a1b2c3"},
        "boss": {"slack": "U_boss"},
    }
    assert merged["roles"] == {"admin": ["harenome", "boss"]}


def test_the_shipped_template_has_no_people_or_roles_to_complain_about():
    from jiuwenswarm.common.scopes import compile_people

    data = yaml.safe_load((_RESOURCES / "config.yaml").read_text(encoding="utf-8"))
    said: list[str] = []
    directory = compile_people(
        data["people"], data["roles"], warn=lambda m, *a: said.append(m)
    )

    assert directory.roles == {}
    assert said == []
