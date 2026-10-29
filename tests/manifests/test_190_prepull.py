# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Pre-pull DaemonSet for the sglang-runtime image on every cpu-demo node (PREPULL-B1).  RED until implemented.

Why: in the g7e A/B the Frontend landed on a cpu-demo node without the 13.75 GB image and lost 7m40s pulling it.  All
cpu-demo nodes have it cached today (NodePool cpu-demo: budget nodes=0, consolidateAfter Never), so the real risk is a NEW
cpu-demo node appearing during a GPU block.

Contract
========
``manifests/node-ops/prepull/``  (own kustomization; NOT listed in ``manifests/node-ops/kustomization.yaml``, so
``kubectl apply -k manifests/node-ops`` keeps its behaviour)
* exactly one workload, ``DaemonSet/prepull-sglang-runtime`` in ``node-ops``; nodeSelector ``karpenter.sh/nodepool: cpu-demo``
  and nothing else; no toleration for the GPU taints (no ``nvidia.com/gpu`` key, no catch-all ``operator: Exists``);
* image: the init container ``pull`` (a no-op: ``true``) and the single main container ``hold`` (``sleep infinity``) use EXACTLY
  ``RUNTIME_IMAGE`` of ``manifests/config`` (the Track Q runtime image, bound by a replacement; the DaemonSet carries no
  registry, repository or digest literal) and therefore the same string as the Track Q Frontend; ``imagePullPolicy:
  IfNotPresent``.  ``hold`` keeps the image referenced so kubelet image GC cannot drop it;
* resources: requests == limits on both containers, cpu <= 10m, memory <= 32Mi;
* security: pod ``automountServiceAccountToken: false``, ``runAsNonRoot: true``, ``runAsUser: 65534``, seccomp RuntimeDefault;
  each container ``allowPrivilegeEscalation: false``, ``capabilities.drop: [ALL]``, ``readOnlyRootFilesystem: true``;
  ``terminationGracePeriodSeconds`` <= 5 (``sleep`` as PID 1 ignores SIGTERM); nothing from the node (no hostPath/host*).
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import lib_manifests as L

BASE = "node-ops/prepull"
BOTH = [BASE]
NAME = "prepull-sglang-runtime"
FRONTEND = "track-q-qwen38/frontend"
NOOP_COMMANDS = (["true"], ["/bin/true"], ["/usr/bin/true"], ["/bin/sh", "-c", "true"], ["sh", "-c", "true"], ["/bin/sh", "-c", ":"])


# ----------------------------------------------------------------------------- helpers
def _frontend_image() -> str:
    """Image of the Track Q Frontend ``main`` container, rendered (never hardcoded)."""
    (dgd,) = L.dgds(L.render(FRONTEND))
    (fe,) = [c for c in dgd["spec"]["components"] if c.get("type") == "frontend"]
    (main,) = [c for c in fe["podTemplate"]["spec"]["containers"] if c["name"] == "main"]
    return str(main["image"])


def _ds(rel: str) -> dict[str, Any]:
    docs = L.render(rel)
    sets = [d for d in docs if d["kind"] == "DaemonSet"]
    assert len(sets) == 1, f"{rel}: expected exactly one DaemonSet, got {[d['kind'] for d in docs]}"
    others = [d for d in docs if d["kind"] in ("Deployment", "StatefulSet", "Job", "CronJob", "Pod", "ReplicaSet")]
    assert not others, f"{rel}: unexpected workloads {[(d['kind'], d['metadata']['name']) for d in others]}"
    return sets[0]


def _ps(rel: str) -> dict[str, Any]:
    return _ds(rel)["spec"]["template"]["spec"]


def _quantity(q: Any) -> float:
    m = re.fullmatch(r"([0-9]*\.?[0-9]+)(m|Ki|Mi|Gi|Ti)?", str(q))
    assert m, q
    return float(m.group(1)) * {None: 1, "m": 1e-3, "Ki": 2**10, "Mi": 2**20, "Gi": 2**30, "Ti": 2**40}[m.group(2)]


def _containers(rel: str) -> list[dict[str, Any]]:
    ps = _ps(rel)
    return list(ps.get("initContainers") or []) + list(ps.get("containers") or [])


# ----------------------------------------------------------------------------- shape
@pytest.mark.parametrize("rel", BOTH)
def test_one_daemonset_in_node_ops_with_the_expected_name(rel: str) -> None:
    ds = _ds(rel)
    assert ds["metadata"]["name"] == NAME
    for d in L.render(rel):
        if d["kind"] != "Namespace":
            assert d["metadata"].get("namespace") == "node-ops", f"{d['kind']}/{d['metadata']['name']}"
    sel = ds["spec"]["selector"]["matchLabels"]
    labels = ds["spec"]["template"]["metadata"]["labels"]
    assert sel and all(labels.get(k) == v for k, v in sel.items()), "selector must match the pod template labels"


@pytest.mark.parametrize("rel", BOTH)
def test_runs_on_every_cpu_demo_node_and_never_on_gpu_nodes(rel: str) -> None:
    ps = _ps(rel)
    assert ps.get("nodeSelector") == {"karpenter.sh/nodepool": "cpu-demo"}, ps.get("nodeSelector")
    for t in ps.get("tolerations") or []:
        assert t.get("key") != "nvidia.com/gpu", t
        assert not (t.get("operator") == "Exists" and not t.get("key")), f"catch-all toleration {t}"
        assert not str(t.get("key", "")).startswith("nvidia.com"), t


def test_base_is_not_part_of_the_node_ops_render() -> None:
    names = {d["metadata"]["name"] for d in L.render("node-ops") if d["kind"] in ("DaemonSet", "Job", "Pod")}
    assert NAME not in names, names
    text = (L.MANIFESTS / "node-ops" / "kustomization.yaml").read_text()
    assert "prepull" not in text, "node-ops/kustomization.yaml must not list the pre-pull directory"


def test_no_other_kustomization_includes_the_base() -> None:
    own = {(L.MANIFESTS / BASE).resolve()}
    hits = [
        str(k.relative_to(L.MANIFESTS))
        for k in sorted(L.MANIFESTS.rglob("kustomization.yaml"))
        if k.parent.resolve() not in own and k.parent.name != "prepull" and "prepull" in k.read_text()
    ]
    assert hits == [], hits


# ----------------------------------------------------------------------------- image
def test_frontend_image_to_follow_is_the_configured_runtime_image() -> None:
    assert _frontend_image() == L.cfg("RUNTIME_IMAGE")


def test_init_and_main_container_names_and_commands() -> None:
    ps = _ps(BASE)
    inits, mains = ps.get("initContainers") or [], ps.get("containers") or []
    assert [c["name"] for c in inits] == ["pull"], [c["name"] for c in inits]
    assert [c["name"] for c in mains] == ["hold"], [c["name"] for c in mains]
    pull_cmd = list(inits[0].get("command") or []) + list(inits[0].get("args") or [])
    assert pull_cmd in NOOP_COMMANDS, f"`pull` must be a no-op, got {pull_cmd}"
    hold_cmd = list(mains[0].get("command") or []) + list(mains[0].get("args") or [])
    assert hold_cmd[-2:] == ["sleep", "infinity"] or hold_cmd == ["/bin/sh", "-c", "sleep infinity"], hold_cmd


def test_prepull_uses_exactly_the_configured_runtime_image() -> None:
    """Pre-pulls what Track Q runs (RUNTIME_IMAGE), not the image of a removed track."""
    expected = L.cfg("RUNTIME_IMAGE")
    assert expected == _frontend_image()
    for c in _containers(BASE):
        assert c["image"] == expected, f"{c['name']}: {c['image']}"
        assert c.get("imagePullPolicy") == "IfNotPresent", c["name"]


def test_prepull_source_carries_no_registry_or_digest_literal() -> None:
    text = (L.MANIFESTS / BASE / "daemonset.yaml").read_text()
    assert "nvcr.io" not in text and "dkr.ecr" not in text and "@sha256:" not in text


# ----------------------------------------------------------------------------- resources
@pytest.mark.parametrize("rel", BOTH)
def test_requests_equal_limits_and_tiny(rel: str) -> None:
    for c in _containers(rel):
        res = c.get("resources") or {}
        req, lim = res.get("requests") or {}, res.get("limits") or {}
        assert req and req == lim, f"{c['name']}: requests {req} != limits {lim}"
        assert _quantity(req["cpu"]) <= 0.010 + 1e-9, f"{c['name']}: cpu {req['cpu']} > 10m"
        assert _quantity(req["memory"]) <= 32 * 2**20, f"{c['name']}: memory {req['memory']} > 32Mi"


# ----------------------------------------------------------------------------- security
@pytest.mark.parametrize("rel", BOTH)
def test_pod_security_context(rel: str) -> None:
    ps = _ps(rel)
    assert ps.get("automountServiceAccountToken") is False
    sc = ps.get("securityContext") or {}
    assert sc.get("runAsNonRoot") is True and sc.get("runAsUser") == 65534
    assert (sc.get("seccompProfile") or {}).get("type") == "RuntimeDefault"
    assert 0 <= int(ps.get("terminationGracePeriodSeconds", 30)) <= 5, "sleep as PID 1 ignores SIGTERM"
    assert "terminationGracePeriodSeconds" in ps
    assert L.privileged_features(ps) == [] and not ps.get("volumes"), "nothing from the node"


@pytest.mark.parametrize("rel", BOTH)
def test_container_security_context(rel: str) -> None:
    for c in _containers(rel):
        sc = c.get("securityContext") or {}
        assert sc.get("allowPrivilegeEscalation") is False, c["name"]
        assert (sc.get("capabilities") or {}).get("drop") == ["ALL"], c["name"]
        assert not (sc.get("capabilities") or {}).get("add"), c["name"]
        assert sc.get("readOnlyRootFilesystem") is True, c["name"]
        assert not sc.get("privileged"), c["name"]
