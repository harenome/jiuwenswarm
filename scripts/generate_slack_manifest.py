#!/usr/bin/env python3
# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Write the tiered Slack app manifests from the scope policy.

    python3 scripts/generate_slack_manifest.py            # write
    python3 scripts/generate_slack_manifest.py --check    # report drift only

The output is committed rather than built on demand, because the people who
need it are operators pasting a file into Slack rather than developers running
a script. ``tests/unit_tests/channel/test_slack_app_manifest.py`` regenerates
and compares, so a committed file cannot fall behind the policy module.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT))

from jiuwenswarm.common.slack_scope_policy import (  # noqa: E402
    TIERS,
    manifest_filename,
    render_manifest,
)

#: Beside the other resources MANIFEST.in ships whole, so a pip-installed
#: deployment has the files an operator pastes.
OUTPUT_DIR = _REPO_ROOT / "jiuwenswarm" / "resources" / "slack"


def outputs() -> dict[Path, str]:
    """Every manifest this generator owns, mapped to the text it should hold."""
    return {OUTPUT_DIR / manifest_filename(tier): render_manifest(tier) for tier in TIERS}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="exit non-zero if any committed manifest differs, writing nothing",
    )
    args = parser.parse_args(argv)

    stale: list[Path] = []
    for path, text in outputs().items():
        current = path.read_text(encoding="utf-8") if path.exists() else None
        if current == text:
            continue
        stale.append(path)
        if args.check:
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        print(f"wrote {path.relative_to(_REPO_ROOT)}")

    if args.check and stale:
        for path in stale:
            print(f"stale: {path.relative_to(_REPO_ROOT)}", file=sys.stderr)
        print(
            "run scripts/generate_slack_manifest.py to bring them up to date",
            file=sys.stderr,
        )
        return 1
    if not stale:
        print("manifests are up to date")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
