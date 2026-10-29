# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Materialize the gitignored ``manifests/config*/platform.env`` from its tracked ``platform.env.example``.

``platform.env`` is generated from the Terraform outputs and holds account values, so a fresh ``git clone`` has only
the example.  Every render, golden and plan hash the suites pin is computed from the example's placeholder values
(account 123456789012).  ``materialize()`` writes the file only when it is absent and ``cleanup()`` removes exactly
those files again; an existing file (a real deployment's) is never modified or deleted.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
_created: list[Path] = []


def examples() -> list[Path]:
    return sorted((REPO_ROOT / "manifests").glob("config*/platform.env.example"))


def materialize() -> None:
    for example in examples():
        target = example.with_name("platform.env")
        if not target.exists():
            target.write_text(example.read_text())
            _created.append(target)


def cleanup() -> None:
    while _created:
        _created.pop().unlink(missing_ok=True)


def differing_local_files() -> list[Path]:
    """Existing generated files whose content differs from the example (not created by ``materialize``)."""
    return [
        e.with_name("platform.env")
        for e in examples()
        if e.with_name("platform.env").exists()
        and e.with_name("platform.env") not in _created
        and e.with_name("platform.env").read_text() != e.read_text()
    ]
