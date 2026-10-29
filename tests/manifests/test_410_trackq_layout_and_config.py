# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Track Q (Qwen3.8-27B-FP8): layout, config consumption, CRD validity, images, namespaces.

See ``lib_trackq`` for the layout.  Everything is black-box on ``kubectl kustomize`` output and on the files the layout promises.
"""

from __future__ import annotations

import re

import lib_manifests as L
import lib_trackq as Q
import pytest
import yaml

TQ_DIR = L.MANIFESTS / Q.TQ


# ----------------------------------------------------------------------------- layout
@pytest.mark.parametrize(
    "rel",
    [f"{Q.TQ}/base", f"{Q.TQ}/components/hicache-agg", f"{Q.TQ}/components/hicache-prefill", f"{Q.TQ}/components/pd-efa"]
    + [f"{Q.TQ}/topologies/{t}" for t in Q.TOPOLOGIES],
)
def test_building_blocks_exist(rel: str) -> None:
    assert (L.MANIFESTS / rel / "kustomization.yaml").is_file(), f"manifests/{rel}/kustomization.yaml missing"


@pytest.mark.parametrize("rel", Q.RENDER_RELS, ids=lambda r: r)
def test_every_track_q_directory_renders(rel: str) -> None:
    assert L.render(rel), f"manifests/{rel} rendered no documents"


def test_components_are_kustomize_components() -> None:
    for name in ("hicache-agg", "hicache-prefill", "pd-efa"):
        doc = yaml.safe_load((TQ_DIR / "components" / name / "kustomization.yaml").read_text())
        assert doc["kind"] == "Component", name


def test_track_q_has_no_config_link_and_its_root_pulls_the_config_directly() -> None:
    """The ``track-q-qwen38/config`` symlink (to the old config directory) is gone; frontend/ uses ``../../config``."""
    assert not (TQ_DIR / "config").exists() and not (TQ_DIR / "config").is_symlink()
    doc = yaml.safe_load((TQ_DIR / "frontend" / "kustomization.yaml").read_text())
    assert [(TQ_DIR / "frontend" / c).resolve() for c in doc["components"]] == [(L.MANIFESTS / Q.CONFIG_REL).resolve()]


def test_some_track_q_kustomization_consumes_the_config() -> None:
    uses = 0
    for path in sorted(TQ_DIR.rglob("kustomization.yaml")):
        cfg = yaml.safe_load(path.read_text()) or {}
        for entry in list(cfg.get("components") or []) + list(cfg.get("resources") or []):
            target = (path.parent / entry).resolve()
            uses += target == (L.MANIFESTS / Q.CONFIG_REL).resolve()
    assert uses, "no Track Q kustomization lists the config component"


def test_track_q_does_not_leak_into_node_ops() -> None:
    assert "track-q" not in (L.MANIFESTS / "node-ops" / "kustomization.yaml").read_text()


# ----------------------------------------------------------------------------- config keys
def test_config_carries_the_runtime_keys() -> None:
    cfg = Q.config()
    assert cfg.get("MODEL_ID") == Q.MODEL
    assert cfg.get("MODEL_REVISION") == Q.REVISION
    assert cfg.get("RUNTIME_IMAGE") == Q.IMAGE, "one runtime-image digest, one config line"
    assert cfg.get("CLUSTER_NAME"), "CLUSTER_NAME is a config key (no bucket-name parsing to find the cluster)"


def test_config_keeps_every_placeholder_value() -> None:
    cfg = Q.config()
    assert {k: cfg.get(k) for k in Q.PLACEHOLDER} == Q.PLACEHOLDER
    assert cfg.get("SGLANG_RUNTIME_REPO") == Q.RUNTIME_REPO and cfg.get("BENCH_REPO") == Q.BENCH_REPO


def test_config_stays_plain_key_value_without_secrets() -> None:
    for n, raw in enumerate(Q.CONFIG_ENV.read_text().splitlines(), 1):
        line = raw.strip()
        if line and not line.startswith("#"):
            assert re.fullmatch(r"[A-Z][A-Z0-9_]*=[^\s\"'#$`]*", line), f"config/platform.env:{n}: {raw!r}"
    assert not [k for k in Q.config() if re.search(r"TOKEN|SECRET|PASSWORD|PRIVATE", k)]


# ----------------------------------------------------------------------------- DGD shape
@pytest.mark.parametrize("rel", Q.CELL_RELS + Q.OVERLAY_RELS, ids=lambda r: r)
def test_dgd_validates_against_the_v151_crd_and_is_named(rel: str) -> None:
    dgd = L.one_dgd(rel)
    assert dgd["apiVersion"] == "nvidia.com/v1beta1" and dgd["spec"]["backendFramework"] == "sglang"
    assert dgd["metadata"]["name"] == Q.DGD_NAME
    assert dgd["metadata"]["labels"]["app.kubernetes.io/part-of"] == Q.PART_OF
    assert L.schema_errors(dgd) == []


@pytest.mark.parametrize("cell", Q.CELL_IDS)
def test_components_replicas_and_namespace(cell: str) -> None:
    topology, _ = Q.CELLS[cell]
    dgd = Q.cell_doc(cell)
    assert dgd["metadata"]["namespace"] == Q.TOPOLOGIES[topology]
    got = [(c["name"], c["type"], c.get("replicas")) for c in L.components(dgd)]
    if topology in Q.AGG:
        assert got == [("Frontend", "frontend", 1), ("Worker", "worker", 8)], got
    else:
        assert got == [("Frontend", "frontend", 1), ("prefill", "prefill", 4), ("decode", "decode", 4)], got


@pytest.mark.parametrize("cell", Q.CELL_IDS)
def test_one_gpu_per_worker_and_eight_gpus_per_cell(cell: str) -> None:
    dgd = Q.cell_doc(cell)
    total = 0
    for comp in Q.workers_of(dgd):
        res = Q.main_of(comp)["resources"]
        assert res["limits"]["nvidia.com/gpu"] == "1" == res["requests"]["nvidia.com/gpu"]
        total += int(comp["replicas"])
    assert total == 8, "one topology at a time on 8 GPUs"


@pytest.mark.parametrize("topology", list(Q.TOPOLOGIES))
def test_topology_overlay_is_the_round_robin_arm_of_its_cells(topology: str) -> None:
    rr_cell = Q.ARMS[topology][0]
    assert L.render_text(Q.overlay_rel(topology)) == L.render_text(Q.cell_rel(rr_cell))


@pytest.mark.parametrize("mode", list(Q.ROUTERS))
def test_frontend_overlays_carry_only_the_frontend_without_namespace(mode: str) -> None:
    docs = L.render(Q.frontend_rel(mode))
    (dgd,) = L.dgds(docs)
    assert "namespace" not in dgd["metadata"] and dgd["metadata"]["name"] == Q.DGD_NAME
    assert [c["name"] for c in L.components(dgd)] == ["Frontend"]
    assert L.schema_errors(dgd) == []
    fe = Q.main_of(L.components(dgd)[0])
    assert L.env_value(fe, "DYN_ROUTER_MODE") == Q.ROUTERS[mode]


@pytest.mark.parametrize("mode", list(Q.ROUTERS))
def test_frontend_overlay_equals_the_cell_frontend(mode: str) -> None:
    """The toggle must not change anything but the router: its Frontend is byte-for-byte the cells' Frontend."""
    (dgd,) = L.dgds(L.render(Q.frontend_rel(mode)))
    cell = Q.ARMS["t-agg"][0 if mode == "rr" else 1]
    assert L.components(dgd)[0] == Q.frontend_of(Q.cell_doc(cell))
    for topology, arms in Q.ARMS.items():
        assert Q.frontend_of(Q.cell_doc(arms[0 if mode == "rr" else 1])) == L.components(dgd)[0], topology


# ----------------------------------------------------------------------------- images, config consumption, literals
@pytest.mark.parametrize("rel", Q.CELL_RELS + Q.OVERLAY_RELS + Q.FRONTEND_RELS, ids=lambda r: r)
def test_every_container_runs_the_single_config_image(rel: str) -> None:
    refs = Q.images(L.render(rel))
    assert refs, rel
    assert set(refs) == {Q.config().get("RUNTIME_IMAGE")} == {Q.IMAGE}, set(refs)
    for ref in refs:
        assert re.fullmatch(r".+@sha256:[0-9a-f]{64}", ref) and ":latest" not in ref
        assert Q.HEX64.match(ref.rsplit(":", 1)[1])


def test_image_literal_lives_only_in_the_config_and_the_committed_renders() -> None:
    assert Q.trackq_tree_files(), "manifests/track-q-qwen38 does not exist yet"
    for p in Q.trackq_tree_files():
        if Q.RENDERED_DIR in p.parents:
            continue
        text = p.read_text(errors="ignore")
        assert "d574951b0dbd" not in text, f"{p.relative_to(L.MANIFESTS)}: image digest hard-coded outside platform.env"
        assert not re.search(r"[0-9]{12}\.dkr\.ecr", text), p
        assert not re.search(r"\b(?:us|eu|ap)-(?:west|east|south|central)-[0-9]\b", text), p


@pytest.mark.parametrize("rel", Q.CELL_RELS + Q.OVERLAY_RELS + Q.FRONTEND_RELS, ids=lambda r: r)
def test_render_carries_only_placeholder_values(rel: str) -> None:
    text = L.render_text(rel)
    region = re.compile(r"(?<![A-Za-z0-9])(?:us|eu|ap|sa|ca|me|af|il)-(?:north|south|east|west|central|northeast|southeast)-[0-9](?![A-Za-z0-9])")
    assert set(region.findall(text)) <= {Q.PLACEHOLDER["AWS_REGION"]}
    assert set(re.findall(r"(?<![A-Za-z0-9])[0-9]{12}(?![A-Za-z0-9])", text)) <= {Q.PLACEHOLDER["AWS_ACCOUNT_ID"]}
    for needle in Q.OREGON_NEEDLES:
        assert needle not in text, needle
    for d in L.render(rel):
        assert not (d["kind"] == "ConfigMap" and "platform" in d["metadata"]["name"]), "config ConfigMap must be dropped"


@pytest.mark.parametrize("rel", Q.CELL_RELS, ids=lambda r: r)
def test_no_forbidden_content_in_cells(rel: str) -> None:
    text = L.render_text(rel)
    for needle in ("cache_salt", "hf-token-secret", "HF_TOKEN", "restore-status", "NIXL_TELEMETRY_ENABLE", ":latest"):
        assert needle not in text, needle
    assert "DYN_FORWARDPASS_METRIC_PORT" in text, "workers disable the forward-pass metric port (empty value)"


@pytest.mark.parametrize("cell", Q.CELL_IDS)
def test_forward_pass_metric_port_is_the_empty_string_on_every_worker(cell: str) -> None:
    for comp in Q.workers_of(Q.cell_doc(cell)):
        assert L.env_value(Q.main_of(comp), "DYN_FORWARDPASS_METRIC_PORT") == ""


@pytest.mark.parametrize("cell", Q.CELL_IDS)
def test_weights_gate_waits_for_the_pinned_revision_marker(cell: str) -> None:
    for comp in Q.workers_of(Q.cell_doc(cell)):
        (gate,) = L.pod_spec(comp)["initContainers"]
        assert gate["name"] == "weights-gate"
        assert L.env_value(gate, "MODEL_REVISION") == Q.REVISION
        assert Q.CACHE_REPO in gate["args"][0] and "revision=$MODEL_REVISION" in gate["args"][0]
