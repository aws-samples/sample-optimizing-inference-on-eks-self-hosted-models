# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The runtime image comes from NVIDIA NGC: ``terraform/cluster`` creates no ECR repository and no KMS key.

The workers pull ``runtime_image`` from nvcr.io through the NAT gateway. No resource of the stack is an ECR or KMS
resource, and ``platform_env`` (the env contract of scripts/gen_env.py) carries no ECR key.
"""

from __future__ import annotations

import re
from pathlib import Path

from hcl import block_body, strip_comments

REPO = Path(__file__).resolve().parents[2]
CLUSTER = REPO / "terraform" / "cluster"


def _sources() -> dict[str, str]:
    """Return the comment-stripped ``.tf`` files of ``terraform/cluster`` and of its local modules."""
    paths = [*CLUSTER.glob("*.tf"), *(CLUSTER / "modules").rglob("*.tf")]
    return {p.relative_to(CLUSTER).as_posix(): strip_comments(p.read_text()) for p in sorted(paths)}


def test_the_stack_has_no_ecr_and_no_kms_resource() -> None:
    sources = _sources()
    assert "outputs.tf" in sources and "eks.tf" in sources, sorted(sources)
    found = [
        f"{name}: {kind}.{label}"
        for name, text in sources.items()
        for kind, label in re.findall(r'(?m)^resource\s+"(aws_(?:ecr|kms)_[a-z_]+)"\s+"([^"]+)"', text)
    ]
    assert not found, f"the runtime image comes from NGC: no ECR repository and no KMS key in terraform/cluster: {found}"


def test_platform_env_has_no_ecr_key() -> None:
    outputs = strip_comments((CLUSTER / "outputs.tf").read_text())
    m = re.search(r"(?m)^\s*platform_env\s*=\s*merge\(", outputs)
    assert m, "outputs.tf defines local.platform_env"
    keys = re.findall(r"(?m)^\s*([A-Z][A-Z0-9_]*)\s*=", block_body(outputs, m.start()))
    assert "CLUSTER_NAME" in keys, f"the key scan sees the platform_env map: {keys}"
    ecr_keys = [k for k in keys if "ECR" in k or k == "SGLANG_RUNTIME_REPO"]
    assert not ecr_keys, f"platform_env carries no ECR key: {ecr_keys}"
