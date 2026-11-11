# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Tests: shape of ``terraform/`` and of the generated config files.

``terraform/`` holds exactly ``cluster`` (one parametrized root), ``bootstrap`` and README.md.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
TF = REPO / "terraform"


def git(*args: str) -> subprocess.CompletedProcess[str]:
    """Run git in the repo and return the completed process (never raises)."""
    return subprocess.run(["git", "-C", str(REPO), *args], capture_output=True, text=True, check=False)


def test_terraform_dir_holds_exactly_cluster_bootstrap_and_the_readme() -> None:
    entries = {p.name for p in TF.iterdir() if not p.name.startswith(".")}
    assert entries == {"cluster", "bootstrap", "README.md"}, sorted(entries)


def test_generated_platform_env_is_not_tracked_but_its_example_is() -> None:
    """The generator writes manifests/config/platform.env (read by the scripts)."""
    ignored = git("check-ignore", "-q", "manifests/config/platform.env").returncode == 0
    assert ignored, "manifests/config/platform.env (generated) must be gitignored"
    assert git("ls-files", "--error-unmatch", "manifests/config/platform.env").returncode != 0, "must be untracked"
    assert (REPO / "manifests" / "config" / "platform.env.example").is_file()
    assert git("check-ignore", "-q", "config/sample.env").returncode == 0, "config/sample.env (generated) is gitignored"
