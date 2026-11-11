# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Static contract of the Pod Identity role that lets the benchmark Job (benchmarks/perf.yaml) upload its results."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml
from hcl import blocks, read_tf

REPO = Path(__file__).resolve().parents[2]
CLUSTER = REPO / "terraform" / "cluster"
IAM = "iam-perf-results.tf"


@pytest.fixture(scope="module")
def tf() -> dict[str, str]:
    return read_tf(CLUSTER)


@pytest.fixture(scope="module")
def resources(tf: dict[str, str]) -> dict[str, str]:
    assert IAM in tf, f"{IAM} must exist in terraform/cluster"
    return blocks(tf[IAM], "resource", 2)


def _local(tf: dict[str, str], name: str) -> str:
    """Return the single-line value of ``local.<name>``."""
    m = re.search(rf"(?m)^\s*{name}\s*=\s*(.+)$", "\n".join(tf.values()))
    assert m, f"local.{name} not found"
    return m.group(1).strip()


def _perf_service_account_in_manifest() -> str:
    docs = yaml.safe_load_all((REPO / "benchmarks" / "perf.yaml").read_text())
    return next(d["metadata"]["name"] for d in docs if d and d.get("kind") == "ServiceAccount")


def test_the_role_policy_attachment_and_associations_exist(resources: dict[str, str]) -> None:
    for name in (
        "aws_iam_role.perf_results",
        "aws_iam_policy.perf_results",
        "aws_iam_role_policy_attachment.perf_results",
        "aws_eks_pod_identity_association.perf_results",
    ):
        assert name in resources, f"{name} must be declared in {IAM}"


def test_policy_allows_only_put_and_abort_multipart_under_perf(resources: dict[str, str]) -> None:
    policy = resources["aws_iam_policy.perf_results"]
    actions = set(re.findall(r'"(s3:[A-Za-z*]+|\*)"', policy))
    assert actions == {"s3:PutObject", "s3:AbortMultipartUpload"}
    resource_lists = re.findall(r"Resource\s*=\s*\[(.*?)\]", policy, flags=re.DOTALL)
    assert resource_lists, "the policy must name its resources"
    for items in resource_lists:
        assert [i.strip() for i in items.split(",") if i.strip()] == ['"${local.results_bucket_arn}/perf/*"']
    assert "kms:" not in policy and "Condition" not in policy


def test_results_bucket_arn_local_sits_next_to_the_model_one(tf: dict[str, str]) -> None:
    arn = _local(tf, "results_bucket_arn")
    assert arn == '"arn:${data.aws_partition.current.partition}:s3:::${local.results_bucket_name}"'


def test_trust_is_limited_to_the_cell_namespaces_and_the_perf_service_account(resources: dict[str, str]) -> None:
    trust = resources["aws_iam_role.perf_results"]
    assert re.search(r'Principal\s*=\s*\{\s*Service\s*=\s*"pods\.eks\.amazonaws\.com"\s*\}', trust) and "sts:TagSession" in trust
    assert "aws:RequestTag/kubernetes-namespace" in trust
    sa_tag = re.search(r'"aws:RequestTag/kubernetes-service-account"\s*=\s*(.+)', trust)
    assert sa_tag, "the trust must pin the service account"
    assert "default" not in sa_tag.group(1)
    assert "s-agg" in trust or "namespaces" in trust, "the trust must name the cell namespaces"


def test_association_loops_over_the_s_namespaces_and_binds_the_perf_service_account(tf: dict[str, str], resources: dict[str, str]) -> None:
    body = resources["aws_eks_pod_identity_association.perf_results"]
    for_each = re.search(r"for_each\s*=\s*(.+)", body)
    assert for_each, "one association per cell namespace needs for_each"
    expr = for_each.group(1)
    local_ref = re.fullmatch(r"\s*local\.(\w+)\s*", expr)
    if local_ref:
        expr = _local(tf, local_ref.group(1))
    assert "local.namespaces" in expr and "s-" in expr, "for_each must come from the s-* keys of local.namespaces"
    assert "aws_iam_role.perf_results.arn" in body
    assert "each.key" in body or "each.value" in body

    sa = re.search(r"service_account\s*=\s*(.+)", body)
    assert sa
    value = sa.group(1).strip()
    local_ref = re.fullmatch(r"local\.(\w+)", value)
    if local_ref:
        value = _local(tf, local_ref.group(1))
    assert value.strip('"') == _perf_service_account_in_manifest() != "default"


def test_the_removed_results_writer_names_are_not_reused(resources: dict[str, str]) -> None:
    assert not [name for name in resources if "results_writer" in name]
