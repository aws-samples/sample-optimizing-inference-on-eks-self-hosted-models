# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The ECR repositories of terraform/cluster at teardown and their scan setting, in the docs (live test 3b).

* D14: both repositories keep ``force_delete = false`` (a destroy or a replacement never deletes an image), so the
  cluster destroy fails with ``RepositoryNotEmptyException`` while one holds an image. terraform/README.md
  ("Teardown order") empties them before the cluster destroy, says why ``force_delete`` stays ``false`` and names
  step 3 of ``scripts/90-teardown.sh``; docs/TROUBLESHOOTING.md has an entry for that destroy error.
* D15: the failed apply that turns the mirror off on a repository with images has already destroyed
  ``aws_ecr_lifecycle_policy.sglang_runtime[0]``; docs/TROUBLESHOOTING.md ("ECR mirror repository") says the
  repository has no lifecycle policy until the next apply succeeds.
* O1: ``scan_on_push = true`` is a repository setting of basic scanning; terraform/README.md says that a registry set
  to enhanced scanning (Amazon Inspector) ignores it and that ``describe-images`` then shows no ``imageScanStatus``.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from hcl import blocks, read_tf

REPO = Path(__file__).resolve().parents[2]
CLUSTER = REPO / "terraform" / "cluster"
REPOSITORIES = ("aws_ecr_repository.bench", "aws_ecr_repository.sglang_runtime")


def _read(rel: str) -> str:
    path = REPO / rel
    assert path.is_file(), f"{rel} missing"
    return path.read_text(encoding="utf-8")


def _flat(text: str) -> str:
    return " ".join(text.split())


def _section(text: str, heading_re: str) -> str:
    """The body of the first heading matching ``heading_re`` up to the next heading of the same or a higher level."""
    m = re.search(rf"(?m)^(#+) {heading_re}\s*$", text)
    assert m, f"no heading matching {heading_re!r}"
    rest = text[m.end():]
    end = re.search(rf"(?m)^#{{1,{len(m.group(1))}}} ", rest)
    return rest[: end.start()] if end else rest


@pytest.fixture(scope="module")
def resources() -> dict[str, str]:
    return blocks("\n".join(read_tf(CLUSTER).values()), "resource", 2)


# ----------------------------------------------------------------------------------- D14: force_delete stays false
@pytest.mark.parametrize("name", REPOSITORIES)
def test_the_repositories_keep_force_delete_false_and_scan_on_push(resources: dict[str, str], name: str) -> None:
    body = resources[name]
    assert re.search(r"(?m)^\s*force_delete\s*=\s*false\s*$", body), f"{name}: force_delete = false (safety)"
    assert re.search(r"(?m)^\s*scan_on_push\s*=\s*true\s*$", body), f"{name}: scan_on_push stays on"


def test_terraform_readme_empties_the_repositories_before_the_cluster_destroy_and_says_why() -> None:
    sec = _section(_read("terraform/README.md"), r"Teardown order")
    flat = _flat(sec)
    nodeclaims, images, destroy = (flat.find("nodeclaims"), flat.find("batch-delete-image"),
                                   flat.find("`terraform destroy` in `cluster/`"))
    assert 0 <= nodeclaims < images < destroy, "order: the GPU NodeClaims, the ECR images, then the cluster destroy"
    for needle in ("force_delete = false", "RepositoryNotEmptyException", "scripts/90-teardown.sh", "<project_name>/bench",
                   "<project_name>/sglang-runtime", "../docs/DEPLOY.md#teardown"):
        assert needle in flat, f"terraform/README.md, Teardown order, names {needle!r}"
    reason = flat[flat.find("force_delete = false"):]
    reason = reason[: reason.find(". ") + 1]
    assert re.search(r"(?i)\b(never|so that|on purpose)\b", reason), f"say why force_delete stays false: {reason!r}"


def test_troubleshooting_covers_the_cluster_destroy_of_a_repository_with_images() -> None:
    sec = _flat(_section(_read("docs/TROUBLESHOOTING.md"), r".*terraform destroy.*cluster.*RepositoryNotEmptyException.*"))
    for needle in ("<project_name>/bench", "sglang-runtime", "force_delete = false", "scripts/90-teardown.sh",
                   "DEPLOY.md#teardown", "terraform destroy"):
        assert needle in sec, f"the TROUBLESHOOTING entry names {needle!r}"


# ----------------------------------------------------------------------------------- D15: the lifecycle policy
def test_troubleshooting_says_the_failed_apply_already_removed_the_lifecycle_policy() -> None:
    sec = _flat(_section(_read("docs/TROUBLESHOOTING.md"), r"ECR mirror repository"))
    assert "aws_ecr_lifecycle_policy.sglang_runtime[0]" in sec
    fact = sec[sec.find("aws_ecr_lifecycle_policy.sglang_runtime[0]"):]
    assert re.search(r"(?i)no lifecycle policy until", fact), "until the next apply succeeds, no lifecycle policy"


# ----------------------------------------------------------------------------------- O1: enhanced scanning
def test_terraform_readme_says_enhanced_scanning_ignores_scan_on_push() -> None:
    paragraphs = [p for p in re.split(r"\n\s*\n", _read("terraform/README.md")) if "scan_on_push" in p]
    assert len(paragraphs) == 1, "one paragraph of terraform/README.md explains scan_on_push"
    flat = _flat(paragraphs[0])
    for needle in ("enhanced scanning", "Amazon Inspector", "imageScanStatus", "describe-image-scan-findings"):
        assert needle in flat, f"the scan_on_push paragraph names {needle!r}"
