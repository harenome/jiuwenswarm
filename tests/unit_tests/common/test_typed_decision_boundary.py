# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The import boundary that lets ``typed_decision`` move to agent-core.

A commit prefix does not keep a package movable. The import graph does. Every
import inside ``jiuwenswarm.common.typed_decision`` is either the standard
library, a third-party transport, a sibling module of the package itself, or
agent-core's ``ModelClientConfig``. Anything else pins the package where it
is, and a test fixture reaching into ``jiuwenswarm.gateway`` pins it as firmly
as production code does.

``scopes`` is the precedent and stayed clean by care alone. This is the same
rule with a check under it.
"""

from __future__ import annotations

import ast
from pathlib import Path

import jiuwenswarm.common.typed_decision as typed_decision

PACKAGE = "jiuwenswarm.common.typed_decision"
PACKAGE_ROOT = Path(typed_decision.__file__).parent

#: The one import outside the package that is allowed, and the reason it is:
#: a decision client beside agent-core's ``llm_client.py`` is where this
#: package eventually belongs, so depending on that module's own schema costs
#: the move nothing.
ALLOWED_FOREIGN_PREFIXES = ("openjiuwen.core.foundation.llm",)


def _modules() -> list[Path]:
    paths = sorted(PACKAGE_ROOT.rglob("*.py"))
    assert paths, f"no modules found under {PACKAGE_ROOT}"
    return paths


def _imported_names(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                # A relative import cannot leave the package, so it needs no
                # check. It is recorded as the package itself for clarity.
                names.add(PACKAGE)
            elif node.module:
                names.add(node.module)
    return names


def _offending(names: set[str]) -> set[str]:
    offenders = set()
    for name in names:
        if name == PACKAGE or name.startswith(f"{PACKAGE}."):
            continue
        if name.startswith(tuple(f"{p}" for p in ALLOWED_FOREIGN_PREFIXES)):
            continue
        if name == "jiuwenswarm" or name.startswith("jiuwenswarm."):
            offenders.add(name)
    return offenders


def test_the_package_imports_nothing_else_from_jiuwenswarm() -> None:
    """Refuse any import that would have to be rewritten by a move."""
    found: dict[str, set[str]] = {}
    for path in _modules():
        offenders = _offending(_imported_names(path))
        if offenders:
            found[str(path.relative_to(PACKAGE_ROOT))] = offenders
    assert not found, (
        "typed_decision must not import the rest of jiuwenswarm; a move would"
        f" have to rewrite these: {found}"
    )


def test_the_tests_import_nothing_else_from_jiuwenswarm() -> None:
    """The same rule for the fixtures, for the same reason.

    A test that builds its state out of the connector holds the package in
    place whatever the production imports say.
    """
    here = Path(__file__).parent
    suites = sorted(here.glob("test_typed_decision_*.py"))
    assert suites, "the protocol's own suites were not found"
    found: dict[str, set[str]] = {}
    for path in suites:
        offenders = _offending(_imported_names(path))
        if offenders:
            found[path.name] = offenders
    assert not found, (
        "the typed_decision suites must import only the package itself:"
        f" {found}"
    )
