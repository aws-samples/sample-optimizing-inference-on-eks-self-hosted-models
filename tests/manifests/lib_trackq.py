# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Helpers for the Track Q (Qwen3.8-27B-FP8 on B200) manifest contract.

Source of truth for what ran: the cell renders of the recorded runs (produced by the original ``render_cells.py`` v2),
frozen here as ``fixtures/cells_reference`` (sha256 list included and verified) and rendered with the placeholder config
of ``manifests/config/platform.env.example``.  Model: Qwen3.8-27B-FP8 TP1 on the derived image; HiCache L2: agg
8 x 200 GB, PD 4 prefill x 400 GB, decode none.

Layout decided here (the developer implements it; every path is rendered with ``kubectl kustomize``):

* ``manifests/config/platform.env``      the generated platform config (reference: ``platform.env.example``); keys
                                         MODEL_ID / MODEL_REVISION / RUNTIME_IMAGE / CLUSTER_NAME.  Track Q components
                                         use ``../../config`` directly (no ``track-q-qwen38/config`` link).
* ``manifests/track-q-qwen38/base``           the engine definition shared by every cell (Frontend + agg Worker).
* ``manifests/track-q-qwen38/components/``    ``hicache-agg`` (200 GB), ``hicache-prefill`` (400 GB), ``pd-efa``.
* ``manifests/track-q-qwen38/topologies/<t>`` t in {t-agg, t-agg-hic, t-dis, t-dis-hic}; namespace s-agg|s-agg-hic|s-dis|s-dis-hic.
* ``manifests/track-q-qwen38/overlays/<t>``   applied by ``20-topology-up.sh`` (round-robin Frontend, the R-off arm).
* ``manifests/track-q-qwen38/frontends/<m>``  m in {rr, kv}; Frontend-only DGD, no namespace (``50-toggle-router.sh``).
* ``manifests/track-q-qwen38/cells/S0..S7``   one overlay per matrix cell (topology + router arm); what the drift test builds.
* ``manifests/track-q-qwen38/rendered/S0..S7.yaml``  the committed ``kubectl kustomize cells/S<n>`` output.
* ``scripts/render-track-q.sh [OUT_DIR]``     regenerates the rendered files (default ``manifests/track-q-qwen38/rendered``).

Nothing here weakens an expectation: a missing file/directory is a failure, never a skip.
"""

from __future__ import annotations

import copy
import hashlib
import ipaddress
import re
from pathlib import Path
from typing import Any

import yaml

import lib_manifests as L

TQ = "track-q-qwen38"
CONFIG_REL = "config"
# Placeholder deployment values of the committed config (manifests/config/platform.env.example).
PLACEHOLDER = {
    "AWS_ACCOUNT_ID": "123456789012",
    "AWS_REGION": "us-east-2",
    "MODEL_BUCKET": "dynamo-sample-models-123456789012-use2",
    "RESULTS_BUCKET": "dynamo-sample-results-123456789012-use2",
    "ECR_REGISTRY": "123456789012.dkr.ecr.us-east-2.amazonaws.com",
}
# SGLANG_RUNTIME_REPO of the example: empty, the default stack creates no sglang-runtime repository
# (create_runtime_mirror_repository = false; the workers pull the public NGC image).
RUNTIME_REPO = ""
BENCH_REPO = f"{PLACEHOLDER['ECR_REGISTRY']}/dynamo-sample/bench"
OREGON_NEEDLES = ("us-west-2", "usw2", "eu-west-3", "euw3")
CONFIG_ENV = L.MANIFESTS / CONFIG_REL / "platform.env"
FIXTURES = Path(__file__).with_name("fixtures") / "cells_reference"
GOLDEN = Path(__file__).with_name("golden_trackq_renders.json")
RENDERED_DIR = L.MANIFESTS / TQ / "rendered"
REGEN_SCRIPT = L.REPO_ROOT / "scripts" / "render-track-q.sh"

DGD_NAME = "q38"
PART_OF = "qwen38-matrix"
MODEL = "Qwen/Qwen3.8-27B-FP8"
REVISION = "017b9c7af6b5689d5dd426a76e0bc077eb5ca20a"
# The public NGC image of the sample (RUNTIME_IMAGE of versions.env and of manifests/config/platform.env.example).
IMAGE = (
    "nvcr.io/nvidia/ai-dynamo/sglang-runtime:1.6.0-efa"
    "@sha256:d574951b0dbd037bdcaabe121431a83cd047a5c106cb243fe6f2f97f2e456cd2"
)
# The derived SGLang 0.5.22 build that ran the recorded results: it is what the frozen fixtures/cells_reference carry.
REFERENCE_IMAGE = (
    "123456789012.dkr.ecr.us-east-2.amazonaws.com/dynamo-sample/sglang-runtime"
    "@sha256:614f0b96dacab741adf42a8db98eaf59d844327b96ffc4fe5083a57d21e11a4b"
)
CACHE_REPO = "models--Qwen--Qwen3.8-27B-FP8"
S3_PREFIX = "Qwen3.8-27B-FP8"
MARKER = f"/mnt/.ephemeral/models/hub/{CACHE_REPO}/.done"
L3_ROOT = "/mnt/.ephemeral/hicache-l3/qwen38-matrix"

TOPOLOGIES: dict[str, str] = {"t-agg": "s-agg", "t-agg-hic": "s-agg-hic", "t-dis": "s-dis", "t-dis-hic": "s-dis-hic"}
AGG = ("t-agg", "t-agg-hic")
DIS = ("t-dis", "t-dis-hic")
HIC = ("t-agg-hic", "t-dis-hic")
ROUTERS = {"rr": "round-robin", "kv": "kv"}
# cell -> (topology, router mode key)
CELLS: dict[str, tuple[str, str]] = {
    "S0": ("t-agg", "rr"), "S1": ("t-agg", "kv"),
    "S2": ("t-agg-hic", "rr"), "S4": ("t-agg-hic", "kv"),
    "S3": ("t-dis", "rr"), "S5": ("t-dis", "kv"),
    "S6": ("t-dis-hic", "rr"), "S7": ("t-dis-hic", "kv"),
}
CELL_IDS = sorted(CELLS)
# the two arms of one topology: (rr cell, kv cell)
ARMS = {"t-agg": ("S0", "S1"), "t-agg-hic": ("S2", "S4"), "t-dis": ("S3", "S5"), "t-dis-hic": ("S6", "S7")}

# --- the engine definition every worker shares (same as the reference CORE_ARGS) and the declared knobs
ALL_INTERFACES = str(ipaddress.IPv4Address(0))  # 0.0.0.0: the engine listens on every interface of its pod
CORE_ARGS = [
    "--model-path", MODEL, "--revision", REVISION, "--served-model-name", MODEL,
    "--tp-size", "1", "--page-size", "64", "--kv-cache-dtype", "fp8_e4m3", "--mem-fraction-static", "0.85",
    "--context-length", "131072", "--attention-backend", "trtllm_mha", "--mamba-radix-cache-strategy", "extra_buffer",
    "--enable-metrics", "--kv-events-config", '{"publisher":"zmq","topic":"kv-events","endpoint":"tcp://*:5557"}',
    "--host", ALL_INTERFACES, "--engine-route", "flush_cache:tm",
]


def hicache_args(size_gb: str) -> list[str]:
    return [
        "--enable-hierarchical-cache", "--hicache-size", size_gb, "--hicache-mem-layout", "page_first",
        "--hicache-io-backend", "kernel", "--hicache-write-policy", "write_through", "--hicache-storage-backend", "nixl",
        "--hicache-storage-prefetch-policy", "timeout", "--engine-route", "clear_hicache_storage:tm",
    ]


def pd_args(role: str) -> list[str]:
    return ["--disaggregation-mode", role, "--disaggregation-transfer-backend", "nixl", "--disaggregation-bootstrap-port", "12345"]


HICACHE_ENV = {
    "SGLANG_HICACHE_NIXL_BACKEND_PLUGIN": "POSIX",
    "SGLANG_HICACHE_NIXL_USE_DIRECT_IO": "0",
    "SGLANG_HICACHE_NIXL_BACKEND_STORAGE_DIR": "/kvcache/l3",
}
EFA_LD = (
    "/opt/amazon/efa/lib:/opt/dynamo/nixl-ucx-compat:/opt/dynamo/nixl-capi:/usr/local/nvidia/lib:"
    "/usr/local/nvidia/lib64:/usr/local/cuda/lib64"
)
EFA_ENV = {
    "SGLANG_DISAGGREGATION_NIXL_BACKEND": "LIBFABRIC", "FI_PROVIDER": "efa", "FI_EFA_USE_DEVICE_RDMA": "1",
    "FI_EFA_ENABLE_SHM_TRANSFER": "0", "FI_EFA_ENABLE_SHM": "0", "LD_LIBRARY_PATH": EFA_LD, "EXPECTED_GPUS": "1",
}
# memory (request, limit) per kind (reference v2 MEMORY table, node-fit sized)
MEMORY = {
    "plain": ("64Gi", "128Gi"), "decode": ("64Gi", "64Gi"), "hicache-agg": ("200Gi", "224Gi"),
    "hicache-pd": ("386Gi", "410Gi"), "frontend": ("16Gi", "16Gi"),
}
HICACHE_SIZE = {"agg": "200", "prefill": "400"}


def l3_path(topology: str) -> str:
    return f"{L3_ROOT}/{TOPOLOGIES[topology]}"


def cell_rel(cell: str) -> str:
    return f"{TQ}/cells/{cell}"


def overlay_rel(topology: str) -> str:
    return f"{TQ}/overlays/{topology}"


def frontend_rel(mode: str) -> str:
    return f"{TQ}/frontends/{mode}"


CELL_RELS = [cell_rel(c) for c in CELL_IDS]
OVERLAY_RELS = [overlay_rel(t) for t in TOPOLOGIES]
FRONTEND_RELS = [frontend_rel(m) for m in ROUTERS]
RENDER_RELS = CELL_RELS + OVERLAY_RELS + FRONTEND_RELS


def is_trackq_rel(rel: str) -> bool:
    return rel == TQ or rel.startswith(TQ + "/")


def trackq_tree_files() -> list[Path]:
    root = L.MANIFESTS / TQ
    if not root.is_dir():
        return []
    return [p for p in sorted(root.rglob("*")) if p.is_file() and p.name != ".DS_Store"]


# --------------------------------------------------------------------------- config
def config() -> dict[str, str]:
    if not CONFIG_ENV.is_file():
        return {}
    out: dict[str, str] = {}
    for raw in CONFIG_ENV.read_text().splitlines():
        line = raw.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            out[key.strip()] = value.strip()
    return out


# --------------------------------------------------------------------------- documents
def reference_doc(cell: str) -> dict[str, Any]:
    """The frozen reference render of ``cell`` (a single DGD)."""
    return yaml.safe_load((FIXTURES / f"{cell}.yaml").read_text())


def cell_doc(cell: str) -> dict[str, Any]:
    return L.one_dgd(cell_rel(cell))


def comp_of(dgd: dict[str, Any], name: str) -> dict[str, Any]:
    (found,) = [c for c in dgd["spec"]["components"] if c["name"] == name]
    return found


def main_of(comp: dict[str, Any]) -> dict[str, Any]:
    return L.main_container(comp)


def workers_of(dgd: dict[str, Any]) -> list[dict[str, Any]]:
    return [c for c in dgd["spec"]["components"] if c["type"] != "frontend"]


def frontend_of(dgd: dict[str, Any]) -> dict[str, Any]:
    (fe,) = [c for c in dgd["spec"]["components"] if c["type"] == "frontend"]
    return fe


def _mask_runtime_image(node: Any) -> None:
    if isinstance(node, dict):
        for k, v in node.items():
            if k == "image" and v in (IMAGE, REFERENCE_IMAGE):
                node[k] = "<runtime image>"
            else:
                _mask_runtime_image(v)
    elif isinstance(node, list):
        for v in node:
            _mask_runtime_image(v)


def normalised(dgd: dict[str, Any]) -> dict[str, Any]:
    """Drop what is allowed to differ between the repo render and the reference render: annotations, comment-like metadata.

    Kept: name, namespace, the part-of label, and the whole ``spec`` (every flag, env, volume, resource, probe).
    The one declared difference is the runtime image: the reference ran the derived 0.5.22 build (REFERENCE_IMAGE), the
    repo renders run the public image (IMAGE); both are replaced by a token, every other ``image`` value is kept.
    """
    out = copy.deepcopy(dgd)
    _mask_runtime_image(out)
    meta = out["metadata"]
    meta.pop("annotations", None)
    labels = meta.get("labels") or {}
    meta["labels"] = {"app.kubernetes.io/part-of": labels.get("app.kubernetes.io/part-of")}
    return out


def diff_paths(a: Any, b: Any, path: str = "") -> list[str]:
    """Paths at which two parsed documents differ (lists compared index-wise; a length change is one path)."""
    if type(a) is not type(b):
        return [path or "<root>"]
    if isinstance(a, dict):
        out: list[str] = []
        for k in sorted(set(a) | set(b)):
            if k not in a or k not in b:
                out.append(f"{path}.{k}")
            else:
                out += diff_paths(a[k], b[k], f"{path}.{k}")
        return out
    if isinstance(a, list):
        if len(a) != len(b):
            return [path or "<root>"]
        out = []
        for i, (x, y) in enumerate(zip(a, b, strict=True)):
            out += diff_paths(x, y, f"{path}[{i}]")
        return out
    return [] if a == b else [path or "<root>"]


def render_hashes() -> dict[str, str]:
    return {r: hashlib.sha256(L.render_text(r).encode()).hexdigest() for r in RENDER_RELS}


def env_pairs(container: dict[str, Any]) -> list[tuple[str, Any]]:
    return [(e["name"], e.get("value", e.get("valueFrom"))) for e in container.get("env", []) or []]


def images(node: Any) -> list[str]:
    found: list[str] = []
    if isinstance(node, dict):
        for k, v in node.items():
            if k == "image" and isinstance(v, str):
                found.append(v)
            else:
                found.extend(images(v))
    elif isinstance(node, list):
        for v in node:
            found.extend(images(v))
    return found


def strip_flags(args: list[str], block: list[str]) -> tuple[list[str], bool]:
    """Remove the contiguous ``block`` from ``args`` once; return (rest, was_present)."""
    n = len(block)
    for i in range(len(args) - n + 1):
        if args[i : i + n] == block:
            return args[:i] + args[i + n :], True
    return args, False


HEX64 = re.compile(r"^[0-9a-f]{64}$")
