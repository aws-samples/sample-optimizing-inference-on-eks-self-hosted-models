# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The Qwen3.8-27B-FP8 cells: the platform config, and the CRD validity, images and namespaces of the published manifests.

The published manifests are the eight written files ``examples/<folder>/manifest.yaml`` (``lib_q38.EXAMPLES``).
"""

from __future__ import annotations

import re

import lib_manifests as L
import lib_q38 as Q
import pytest


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


def test_config_stays_plain_key_value_without_secrets() -> None:
    for n, raw in enumerate(Q.CONFIG_ENV.read_text().splitlines(), 1):
        line = raw.strip()
        if line and not line.startswith("#"):
            assert re.fullmatch(r"[A-Z][A-Z0-9_]*=[^\s\"'#$`]*", line), f"config/platform.env:{n}: {raw!r}"
    assert not [k for k in Q.config() if re.search(r"TOKEN|SECRET|PASSWORD|PRIVATE", k)]


# ----------------------------------------------------------------------------- DGD shape
@pytest.mark.parametrize("cell", Q.CELL_IDS)
def test_dgd_validates_against_the_v161_crd_and_is_named(cell: str) -> None:
    dgd = Q.cell_doc(cell)
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


# ----------------------------------------------------------------------------- images, config consumption, literals
@pytest.mark.parametrize("cell", Q.CELL_IDS)
def test_every_container_runs_the_single_config_image(cell: str) -> None:
    refs = Q.images(Q.example_docs(cell))
    assert refs, cell
    assert set(refs) == {Q.config().get("RUNTIME_IMAGE")} == {Q.IMAGE}, set(refs)
    for ref in refs:
        assert re.fullmatch(r".+@sha256:[0-9a-f]{64}", ref) and ":latest" not in ref
        assert Q.HEX64.match(ref.rsplit(":", 1)[1])


@pytest.mark.parametrize("cell", Q.CELL_IDS)
def test_render_carries_only_placeholder_values(cell: str) -> None:
    text = Q.example_file(cell).read_text()
    region = re.compile(r"(?<![A-Za-z0-9])(?:us|eu|ap|sa|ca|me|af|il)-(?:north|south|east|west|central|northeast|southeast)-[0-9](?![A-Za-z0-9])")
    assert set(region.findall(text)) <= {Q.PLACEHOLDER["AWS_REGION"]}
    assert set(re.findall(r"(?<![A-Za-z0-9])[0-9]{12}(?![A-Za-z0-9])", text)) <= {Q.PLACEHOLDER["AWS_ACCOUNT_ID"]}
    for needle in Q.OREGON_NEEDLES:
        assert needle not in text, needle
    for d in Q.example_docs(cell):
        assert not (d["kind"] == "ConfigMap" and "platform" in d["metadata"]["name"]), "config ConfigMap must be dropped"


@pytest.mark.parametrize("cell", Q.CELL_IDS)
def test_no_forbidden_content_in_cells(cell: str) -> None:
    text = Q.example_file(cell).read_text()
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
