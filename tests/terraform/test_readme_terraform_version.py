# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The Terraform bullet of terraform/README.md ("Before you start") matches ``required_version`` of every stack.

The bullet reads ``Terraform >= <min> (tested with <x.y.z> and <x.y.z>)``: the minimum must be the one every
``versions.tf`` declares, and a patch of that minimum must be among the tested versions (the floor was run, not
assumed).
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
README = REPO / "terraform" / "README.md"
BULLET = re.compile(r"(?m)^- \**Terraform >= (\d+\.\d+)\**\s*\(tested with ([^)]*)\)")


def _version(text: str) -> tuple[int, ...]:
    return tuple(int(x) for x in text.split("."))


def _required_versions() -> dict[str, str]:
    """Return ``{path: required_version}`` of the tracked stacks (``.terraform/`` init dirs excluded)."""
    tf = REPO / "terraform"
    out = {}
    for p in sorted(tf.rglob("versions.tf")):
        if any(part.startswith(".") for part in p.relative_to(tf).parts):
            continue
        m = re.search(r'(?m)^\s*required_version\s*=\s*"([^"]+)"', p.read_text())
        assert m, f"{p.relative_to(REPO)} has no required_version"
        out[str(p.relative_to(REPO))] = m.group(1)
    return out


def test_readme_bullet_names_the_required_version_and_the_tested_releases() -> None:
    m = BULLET.search(README.read_text())
    assert m, "terraform/README.md: the 'Before you start' bullet reads 'Terraform >= <min> (tested with ...)'"
    minimum, tested = m.group(1), re.findall(r"\b\d+\.\d+\.\d+\b", m.group(2))
    required = _required_versions()
    assert {"terraform/cluster/versions.tf", "terraform/bootstrap/versions.tf"} <= set(required)
    assert set(required.values()) == {f">= {minimum}"}, f"README says >= {minimum}, versions.tf say {required}"
    assert tested, "name the tested releases"
    assert all(_version(t) >= _version(minimum) for t in tested), f"a tested release below the minimum: {tested}"
    assert any(t.startswith(f"{minimum}.") for t in tested), f"a {minimum}.x release must be among the tested ones: {tested}"
