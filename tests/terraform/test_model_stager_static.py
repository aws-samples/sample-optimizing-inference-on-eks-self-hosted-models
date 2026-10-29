# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""RED tests: static contract of the model-stager Pod Identity role (the plan-level proof is ``model_stager.tftest.hcl``).

The staging Job (node-ops/model-stager) needs write access that the read-only ``model-reader`` role of the prefetch Job
must never get, so ``iam-model-access.tf`` gets a SECOND role, ServiceAccount and Pod Identity association in the file's own
style. ``var.model_s3_prefix`` (default null) overrides the ``<model_id>/<model_revision>`` object prefix; the same value
reaches ``scripts/gen_env.py`` through the ``sample_env`` output as ``MODEL_S3_PREFIX``.
"""

from __future__ import annotations

import os
import re
import shutil
from pathlib import Path

import lib_proc
import pytest
from hcl import blocks, read_tf

REPO = Path(__file__).resolve().parents[2]
CLUSTER = REPO / "terraform" / "cluster"
IAM = "iam-model-access.tf"


@pytest.fixture(scope="module")
def tf() -> dict[str, str]:
    return read_tf(CLUSTER)


@pytest.fixture(scope="module")
def iam(tf: dict[str, str]) -> str:
    return tf[IAM]


@pytest.fixture(scope="module")
def resources(iam: str) -> dict[str, str]:
    return blocks(iam, "resource", 2)


def test_stager_resources_exist_next_to_the_reader(resources: dict[str, str]) -> None:
    for name in (
        "aws_iam_role.model_stager",
        "aws_iam_policy.model_stager",
        "aws_iam_role_policy_attachment.model_stager",
        "kubernetes_service_account_v1.model_stager",
        "aws_eks_pod_identity_association.model_stager",
    ):
        assert name in resources, f"{name} must be declared in {IAM}"


def test_the_reader_resources_are_still_there_and_read_only(resources: dict[str, str]) -> None:
    for name in ("aws_iam_role.model_reader", "aws_iam_policy.model_reader", "aws_eks_pod_identity_association.model_prefetch"):
        assert name in resources
    assert not re.search(r"s3:(Put|Abort|Delete)", resources["aws_iam_policy.model_reader"])


def test_association_binds_the_stager_role_and_service_account(resources: dict[str, str]) -> None:
    body = resources["aws_eks_pod_identity_association.model_stager"]
    assert "aws_iam_role.model_stager.arn" in body and "model_reader" not in body
    assert "kubernetes_service_account_v1.model_stager" in body


def test_service_account_lives_in_node_ops_and_waits_for_the_namespace(resources: dict[str, str]) -> None:
    body = resources["kubernetes_service_account_v1.model_stager"]
    assert re.search(r'name\s*=\s*"model-stager"', body) or "local.model_stager_service_account" in body
    assert "kubectl_manifest.namespace" in body, "depends_on the namespace like model_prefetch does"


def test_policy_is_attached_only_to_the_stager_role(resources: dict[str, str]) -> None:
    body = resources["aws_iam_role_policy_attachment.model_stager"]
    assert "aws_iam_role.model_stager" in body and "aws_iam_policy.model_stager" in body


def test_stager_has_no_secrets_manager_or_wildcard_anywhere(iam: str, resources: dict[str, str]) -> None:
    for name, body in resources.items():
        if "model_stager" in name and name != "aws_eks_pod_identity_association.model_stager":
            assert "secretsmanager" not in body.lower() and "hf_token_secret_arn" not in body, name
    for m in re.finditer(r'resource\s+"aws_iam_role_policy"\s+"(\w+)"', iam):
        assert "model_stager" not in resources.get(f"aws_iam_role_policy.{m.group(1)}", ""), "no inline policy on the stager role"
    policy = resources["aws_iam_policy.model_stager"]
    assert not re.search(r'"(\*|s3:\*)"', policy), "no wildcard action or resource"
    assert "local.model_s3_prefix" in policy and "local.model_bucket_arn" in policy


def test_model_s3_prefix_local_and_variable(tf: dict[str, str]) -> None:
    assert re.search(r'model_s3_prefix\s*=\s*coalesce\(\s*var\.model_s3_prefix\s*,\s*"\$\{var\.model_id\}/\$\{var\.model_revision\}"\s*\)', "\n".join(tf.values())), (
        "local.model_s3_prefix = coalesce(var.model_s3_prefix, \"${var.model_id}/${var.model_revision}\"): the same default as gen_env.py"
    )
    var = blocks(tf["variables.tf"], "variable", 1)["model_s3_prefix"]
    assert re.search(r"default\s*=\s*null", var) and "type" in var and "description" in var
    assert "validation" in var and ".." in var, "the validation must reject '..' segments (a prefix escape)"


def test_sample_env_output_carries_the_prefix(tf: dict[str, str]) -> None:
    assert "MODEL_S3_PREFIX" in tf["outputs.tf"], "gen_env.py reads MODEL_S3_PREFIX from sample_env"


def test_the_hf_token_secret_arn_option_stays_on_the_read_only_prefetch_role(iam: str, resources: dict[str, str]) -> None:
    hf = blocks(iam, "resource", 2)["aws_iam_role_policy.model_reader_hf_token"]
    assert "var.hf_token_secret_arn" in hf


def test_tftest_file_exists() -> None:
    assert (CLUSTER / "tests" / "model_stager.tftest.hcl").is_file()


def _terraform() -> str | None:
    return os.environ.get("TERRAFORM_BIN") or shutil.which("terraform")


def test_terraform_test_passes_for_the_model_stager_role() -> None:
    """Runs the plan-level test (mock providers, no AWS) when a Terraform >= 1.12 and an initialised ``terraform/cluster`` exist."""
    binary = _terraform()
    if binary is None or not (CLUSTER / ".terraform").is_dir():
        pytest.skip("terraform binary or `terraform init -backend=false` in terraform/cluster missing")
    ver = lib_proc.run([binary, "version"], capture_output=True, text=True).stdout
    m = re.search(r"Terraform v1\.(\d+)", ver)
    if not m or int(m.group(1)) < 12:
        pytest.skip(f"Terraform >= 1.12 required, found: {ver.splitlines()[0] if ver else 'none'} (set TERRAFORM_BIN)")
    proc = lib_proc.run([binary, "test", "-no-color", "-filter=tests/model_stager.tftest.hcl"], cwd=CLUSTER, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stdout[-4000:] + proc.stderr[-2000:]
