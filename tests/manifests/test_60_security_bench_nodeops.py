# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Pod-security placement, bench pod and node-ops prefetch Job (spec 7.2, 8.4, 10.1)."""

from __future__ import annotations

import pytest
import yaml

import lib_manifests as L

ALL = L.all_render_dirs()


@pytest.mark.parametrize("rel", ALL, ids=lambda r: r)
def test_privileged_pod_features_only_in_privileged_namespaces(rel: str) -> None:
    """spec 7.2: hostPath/hostIPC/privileged/added capabilities only in PSA-privileged namespaces (s-*, node-ops), never bench."""
    docs = L.render(rel)
    for doc in docs:
        ns = doc.get("metadata", {}).get("namespace")
        for label, ps in L.iter_pod_specs([doc]):
            feats = L.privileged_features(ps)
            if feats:
                assert ns in L.PRIVILEGED_NAMESPACES, f"{label} uses {feats} in namespace {ns!r}"
                assert ns != "bench"


# ------------------------------------------------------------------------- bench
def _bench():
    docs = L.render("bench")
    pods = [(lbl, ps, d) for d in docs for lbl, ps in L.iter_pod_specs([d])]
    assert pods, "manifests/bench renders no pod-bearing object"
    return docs, pods


def test_bench_namespace_and_placement() -> None:
    """spec 6.3/10.1: bench pod in namespace `bench`, cpu-demo pool, do-not-disrupt."""
    docs, pods = _bench()
    for d in docs:
        if d["kind"] not in ("Namespace",):
            assert d["metadata"].get("namespace") == "bench", f"{d['kind']}/{d['metadata']['name']}"
    for lbl, ps, d in pods:
        assert (ps.get("nodeSelector") or {}).get("karpenter.sh/nodepool") == "cpu-demo", lbl
        tmpl = d["spec"].get("template", d)
        ann = (tmpl.get("metadata", {}) or {}).get("annotations", {}) or d["metadata"].get("annotations", {}) or {}
        assert ann.get("karpenter.sh/do-not-disrupt") == "true", lbl


def test_bench_pins_aiperf_0_13_0_and_requests_no_gpu() -> None:
    """spec 8.1/10.1: aiperf 0.13.0 pinned; bench is CPU-only."""
    text = L.render_text("bench")
    assert "aiperf==0.13.0" in text
    assert "nvidia.com/gpu" not in text
    assert "hostPath" not in text


# --------------------------------------------------------------------- node-ops
def _prefetch():
    docs = L.render("node-ops")
    jobs = [d for d in docs if d["kind"] == "Job" and d["metadata"]["name"] == "model-prefetch"]
    assert len(jobs) == 1, "node-ops must render Job/model-prefetch"
    return docs, jobs[0]


def test_prefetch_job_placement_and_identity() -> None:
    """spec 8.4: Job model-prefetch in node-ops, serviceAccountName model-prefetch, pinned to the GPU node."""
    _, job = _prefetch()
    assert job["metadata"]["namespace"] == "node-ops"
    ps = job["spec"]["template"]["spec"]
    assert ps["serviceAccountName"] == "model-prefetch"
    assert ps["nodeSelector"]["karpenter.sh/nodepool"] == "gpu-b200-cb"
    assert any(t.get("key") == "nvidia.com/gpu" and t.get("operator") == "Exists" for t in ps["tolerations"])


def test_prefetch_job_defaults_are_the_config_model_and_no_other() -> None:
    """The Job defaults are the platform config's model (MODEL_ID/MODEL_REVISION/MODEL_S3_URI): no other model literal is left."""
    docs, job = _prefetch()
    env = {e["name"]: e.get("value") for e in job["spec"]["template"]["spec"]["containers"][0]["env"]}
    assert env["MODEL_S3_URI"] == L.S3_SOURCE, env["MODEL_S3_URI"]
    assert env["MODEL_REVISION"] == L.cfg("MODEL_REVISION")
    assert env["MODEL_CACHE_REPO"] == "models--" + L.cfg("MODEL_ID").replace("/", "--")
    assert env["AWS_REGION"] == L.cfg("AWS_REGION")
    source = (L.MANIFESTS / "node-ops" / "job-model-prefetch.yaml").read_text()
    for needle in ("G" + "LM", "g" + "lm", "RadixArk"):
        assert needle not in source, f"job-model-prefetch.yaml still names {needle}"
    assert "RadixArk" not in yaml.safe_dump(docs)


def test_prefetch_job_copies_from_s3_to_hostpath_and_writes_done_last() -> None:
    """spec 8.4: S3 -> hostPath /mnt/.ephemeral/models, `.done` marker written LAST."""
    _, job = _prefetch()
    ps = job["spec"]["template"]["spec"]
    assert any(
        v.get("hostPath", {}).get("path", "").startswith("/mnt/.ephemeral/models") or v.get("hostPath", {}).get("path") == "/mnt/.ephemeral"
        for v in ps["volumes"]
    ), ps["volumes"]
    main = ps["containers"][0]
    text = L.text_of(main) + yaml.safe_dump(main.get("env", []))
    assert L.S3_SOURCE in text, "source URI missing"
    done_at = text.rfind(".done")
    assert done_at != -1, "no .done marker"
    last_copy = max(text.rfind("s3 sync"), text.rfind("s3 cp"), text.rfind("sync "), text.rfind("cp "))
    assert last_copy != -1 and done_at > last_copy, ".done must be written after the last copy command"
    assert ps.get("restartPolicy") in ("Never", "OnFailure")
