# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Pod-security placement and the node-ops prefetch Job."""

from __future__ import annotations

import pytest
import yaml

import lib_manifests as L

# the kustomize directories and the written example manifests (examples/<folder>/manifest.yaml)
ALL = L.all_render_dirs() + [f"examples/{f}" for f in L.example_folders()]


def _docs(rel: str) -> list[dict]:
    return L.example_docs(rel.removeprefix("examples/")) if rel.startswith("examples/") else L.render(rel)


@pytest.mark.parametrize("rel", ALL, ids=lambda r: r)
def test_privileged_pod_features_only_in_privileged_namespaces(rel: str) -> None:
    """hostPath/hostIPC/privileged/added capabilities only in PSA-privileged namespaces (s-*, node-ops)."""
    docs = _docs(rel)
    for doc in docs:
        ns = doc.get("metadata", {}).get("namespace")
        for label, ps in L.iter_pod_specs([doc]):
            feats = L.privileged_features(ps)
            if feats:
                assert ns in L.PRIVILEGED_NAMESPACES, f"{label} uses {feats} in namespace {ns!r}"


# --------------------------------------------------------------------- node-ops
def _prefetch():
    docs = L.render("node-ops")
    jobs = [d for d in docs if d["kind"] == "Job" and d["metadata"]["name"] == "model-prefetch"]
    assert len(jobs) == 1, "node-ops must render Job/model-prefetch"
    return docs, jobs[0]


def test_prefetch_job_placement_and_identity() -> None:
    """Job model-prefetch in node-ops, serviceAccountName model-prefetch, pinned to the GPU node."""
    _, job = _prefetch()
    assert job["metadata"]["namespace"] == "node-ops"
    ps = job["spec"]["template"]["spec"]
    assert ps["serviceAccountName"] == "model-prefetch"
    assert ps["nodeSelector"]["karpenter.sh/nodepool"] == "gpu-b200-cb"
    assert any(t.get("key") == "nvidia.com/gpu" and t.get("operator") == "Exists" for t in ps["tolerations"])


def test_prefetch_job_defaults_are_the_config_model_and_no_other() -> None:
    """The Job defaults are the platform config's model (MODEL_ID, MODEL_REVISION, the prefix of MODEL_S3_URI): no other model literal is left."""
    docs, job = _prefetch()
    env = {e["name"]: e.get("value") for e in job["spec"]["template"]["spec"]["containers"][0]["env"]}
    assert env["MODEL_S3_URI"].endswith(f"/{L.cfg('MODEL_S3_PREFIX')}/"), env["MODEL_S3_URI"]
    assert env["MODEL_REVISION"] == L.cfg("MODEL_REVISION")
    assert env["MODEL_CACHE_REPO"] == "models--" + L.cfg("MODEL_ID").replace("/", "--")
    source = (L.MANIFESTS / "node-ops" / "job-model-prefetch.yaml").read_text()
    for needle in ("G" + "LM", "g" + "lm", "Radix" + "Ark"):
        assert needle not in source, f"job-model-prefetch.yaml still names {needle}"
    assert "Radix" + "Ark" not in yaml.safe_dump(docs)


def test_prefetch_job_copies_from_s3_to_hostpath_and_writes_done_last() -> None:
    """S3 -> hostPath /mnt/.ephemeral/models, `.done` marker written LAST."""
    _, job = _prefetch()
    ps = job["spec"]["template"]["spec"]
    assert any(
        v.get("hostPath", {}).get("path", "").startswith("/mnt/.ephemeral/models") or v.get("hostPath", {}).get("path") == "/mnt/.ephemeral"
        for v in ps["volumes"]
    ), ps["volumes"]
    main = ps["containers"][0]
    text = L.text_of(main) + yaml.safe_dump(main.get("env", []))
    assert "MODEL_S3_URI" in L.text_of(main), "the copy must read its source from MODEL_S3_URI"
    done_at = text.rfind(".done")
    assert done_at != -1, "no .done marker"
    last_copy = max(text.rfind("s3 sync"), text.rfind("s3 cp"), text.rfind("sync "), text.rfind("cp "))
    assert last_copy != -1 and done_at > last_copy, ".done must be written after the last copy command"
    assert ps.get("restartPolicy") in ("Never", "OnFailure")
