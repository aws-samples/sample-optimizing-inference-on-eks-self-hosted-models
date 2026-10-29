# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""RED tests: the Grafana edge applies in one ``terraform apply`` on a new stack.

A real first apply failed with ``namespaces "monitoring" not found``: ``kubernetes_service_v1.grafana_edge`` had no
dependency on the namespace and was created before it (the mock providers of ``terraform test`` cannot see that
ordering). Here: every ``kubernetes_*`` resource of ``grafana-edge.tf`` in the ``monitoring`` namespace depends on
``kubectl_manifest.namespace``, which creates ``monitoring`` after ``module.eks`` (cluster and creator access entry).
With that order a single apply creates the whole edge (measured: about 12 minutes, most of it the VPC origin), so the
header of ``grafana-edge.tf``, ``terraform/README.md`` and ``terraform.tfvars.example`` no longer prescribe a two-phase
apply with ``-target``.
"""

from __future__ import annotations

import re
from pathlib import Path

from hcl import blocks, read_tf

REPO = Path(__file__).resolve().parents[2]
CLUSTER = REPO / "terraform" / "cluster"
EDGE = "grafana-edge.tf"
NAMESPACE_RESOURCE = "kubectl_manifest.namespace"
TWO_PHASE = re.compile(r"two[- ]phases?|-target\s*=", re.IGNORECASE)


def depends_on(body: str) -> list[str]:
    """Return the addresses listed in the ``depends_on`` of a resource body (instance keys stripped)."""
    m = re.search(r"\bdepends_on\s*=\s*\[(.*?)\]", body, re.DOTALL)
    return [re.sub(r"\[.*\]$", "", a.strip()) for a in m.group(1).split(",") if a.strip()] if m else []


def edge_namespace(tf: dict[str, str]) -> str:
    """Return the value of ``local.grafana_edge_namespace``."""
    m = re.search(r'\bgrafana_edge_namespace\s*=\s*"([^"]+)"', tf[EDGE])
    assert m, "grafana-edge.tf must define local.grafana_edge_namespace"
    return m.group(1)


def test_every_monitoring_kubernetes_resource_of_the_edge_waits_for_the_namespace() -> None:
    tf = read_tf(CLUSTER)
    ns = edge_namespace(tf)
    assert ns == "monitoring"
    in_ns = {
        addr: body
        for addr, body in blocks(tf[EDGE], "resource", 2).items()
        if addr.startswith("kubernetes_")
        and re.search(rf'\bnamespace\s*=\s*(local\.grafana_edge_namespace|"{ns}")', body)
    }
    assert "kubernetes_service_v1.grafana_edge" in in_ns, "the edge Service must target the monitoring namespace"
    missing = [addr for addr, body in in_ns.items() if NAMESPACE_RESOURCE not in depends_on(body)]
    assert not missing, f"{missing} must depend on {NAMESPACE_RESOURCE} (first apply: namespaces {ns!r} not found)"


def test_the_namespace_resource_creates_monitoring_after_the_cluster() -> None:
    tf = read_tf(CLUSTER)
    body = blocks(tf["namespaces.tf"], "resource", 2)["kubectl_manifest.namespace"]
    assert "for_each" in body and "local.namespaces" in body
    assert re.search(r'"monitoring"\s*=', tf["namespaces.tf"]), "local.namespaces must create monitoring"
    assert "module.eks" in depends_on(body), "the namespaces wait for the cluster and its creator access entry"


def test_docs_describe_a_single_apply_for_the_edge() -> None:
    header = (CLUSTER / EDGE).read_text().split("# Providers")[0]
    readme = (REPO / "terraform" / "README.md").read_text()
    grafana = readme.split("## Grafana", 1)[1].split("\n## ", 1)[0]
    example = (CLUSTER / "terraform.tfvars.example").read_text()
    docs = {"grafana-edge.tf header": header, "terraform/README.md": readme, "tfvars.example": example}
    for name, text in docs.items():
        assert not TWO_PHASE.search(text), f"{name} still prescribes a two-phase apply with -target"
    assert "Single apply" in header and "12 minutes" in header, "the header states one apply and its ~12 min wait"
    assert "One `terraform apply`" in grafana and "12 minutes" in grafana, "README Grafana: one apply, ~12 minutes"
    assert "edge (one apply, about 12 min" in example, "tfvars.example: the edge takes one apply, about 12 min"
