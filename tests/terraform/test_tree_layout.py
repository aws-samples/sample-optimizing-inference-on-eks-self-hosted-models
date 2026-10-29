# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""RED tests: shape of ``terraform/`` after the refactor (decision 4, 2026-10-24).

``terraform/`` keeps exactly: ``cluster`` (one parametrized root), ``bootstrap`` and README.md.
Every lab stack (all regions, Mumbai included) and the legacy folder leave the tree and stay in git history, tagged
``lab-terraform-snapshot-pre-refactor`` (tag created on the commit BEFORE the first refactor commit; push the
tag to ``origin`` only, never ``--tags``, never github-old). The ``_lab-backup-ap-south-1`` copy of the Mumbai
stack was dropped too (decision of 2026-10-24): the Mumbai stack is read from the tag.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
TF = REPO / "terraform"
TAG = "lab-terraform-snapshot-pre-refactor"
REMOVED = [
    "_lab-backup-ap-south-1",
    "terraform-us-west-2",
    "terraform-us-west-2-bootstrap",
    "terraform-eu-west-3",
    "terraform-eu-west-3-bootstrap",
    "terraform-eu-north-1",
    "terraform-ap-south-1",
    "terraform-ap-south-1-bootstrap",
    "terraform",
]


def git(*args: str) -> subprocess.CompletedProcess[str]:
    """Run git in the repo and return the completed process (never raises)."""
    return subprocess.run(["git", "-C", str(REPO), *args], capture_output=True, text=True, check=False)


def test_terraform_dir_holds_only_the_new_layout() -> None:
    entries = {p.name for p in TF.iterdir() if not p.name.startswith(".")}
    assert entries == {"cluster", "bootstrap", "README.md"}, sorted(entries)


@pytest.mark.parametrize("name", REMOVED)
def test_old_folders_are_gone(name: str) -> None:
    assert not (TF / name).exists(), f"terraform/{name} must leave the tree (it stays in git history)"


def test_pre_refactor_tag_exists_and_preserves_the_old_tree() -> None:
    assert git("rev-parse", "--verify", "-q", f"refs/tags/{TAG}^{{commit}}").returncode == 0, f"tag {TAG} missing"
    assert git("merge-base", "--is-ancestor", TAG, "HEAD").returncode == 0, "tag must be an ancestor of HEAD"
    for old in (
        "terraform-us-west-2",
        "terraform-eu-west-3",
        "terraform-eu-north-1",
        "terraform-ap-south-1",
        "terraform-ap-south-1-bootstrap",
        "terraform",
    ):
        assert git("cat-file", "-e", f"{TAG}:terraform/{old}").returncode == 0, f"{TAG} lacks terraform/{old}"
    assert git("cat-file", "-e", f"{TAG}:terraform/cluster").returncode != 0, "tag must be BEFORE the refactor"


def test_readme_describes_the_new_layout() -> None:
    text = (TF / "README.md").read_text()
    assert "as-is, unrefactored snapshot" not in text
    assert "_lab-backup-ap-south-1" not in text, "the backup folder left the tree; point to the tag instead"
    for token in (
        "cluster",
        "bootstrap",
        "terraform.tfvars.example",
        "gpu_instance_types",
        "gpu_capacity_type",
        "enable_grafana_edge",
        "gen_env.py",
        "manifests untested",
    ):
        assert token in text, f"terraform/README.md must mention {token!r}"


def test_generated_platform_env_is_not_tracked_but_its_example_is() -> None:
    """The generator writes manifests/config/platform.env (read by kustomize and scripts/lib/common.sh)."""
    ignored = git("check-ignore", "-q", "manifests/config/platform.env").returncode == 0
    assert ignored, "manifests/config/platform.env (generated) must be gitignored"
    assert git("ls-files", "--error-unmatch", "manifests/config/platform.env").returncode != 0, "must be untracked"
    assert (REPO / "manifests" / "config" / "platform.env.example").is_file()
    assert git("check-ignore", "-q", "config/sample.env").returncode == 0, "config/sample.env (generated) is gitignored"
