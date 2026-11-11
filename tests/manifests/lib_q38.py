# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Helpers for the manifest contract of the Qwen3.8-27B-FP8 cells (on B200).

Reference: frozen manifests of the eight cells, rendered with the placeholder config of
``manifests/config/platform.env.example`` before the move to the public runtime image, kept as
``fixtures/cells_reference`` (sha256 list included and verified).  Model: Qwen3.8-27B-FP8 TP1; HiCache L2: agg
8 x 200 GB, PD 4 prefill x 400 GB, decode none.

The published manifests are the eight written files ``examples/<folder>/manifest.yaml``, one per cell (``EXAMPLES``).
``manifests/config/platform.env`` is the generated platform config (reference: ``platform.env.example``).

Nothing here weakens an expectation: a missing file/directory is a failure, never a skip.
"""

from __future__ import annotations

import copy
import ipaddress
import re
from pathlib import Path
from typing import Any

import yaml

import lib_manifests as L

CONFIG_REL = "config"
# Placeholder deployment values of the committed config (manifests/config/platform.env.example).
PLACEHOLDER = {
    "AWS_ACCOUNT_ID": "123456789012",
    "AWS_REGION": "us-east-2",
    "MODEL_BUCKET": "dynamo-sample-models-123456789012-use2",
    "RESULTS_BUCKET": "dynamo-sample-results-123456789012-use2",
}
OREGON_NEEDLES = ("us-west-2", "usw2", "eu-west-3", "euw3")
CONFIG_ENV = L.MANIFESTS / CONFIG_REL / "platform.env"
FIXTURES = Path(__file__).with_name("fixtures") / "cells_reference"

DGD_NAME = "q38"
PART_OF = "qwen38-matrix"
MODEL = "Qwen/Qwen3.8-27B-FP8"
REVISION = "017b9c7af6b5689d5dd426a76e0bc077eb5ca20a"
# The public NGC image of the sample (RUNTIME_IMAGE of versions.env and of manifests/config/platform.env.example).
IMAGE = (
    "nvcr.io/nvidia/ai-dynamo/sglang-runtime:1.6.1-efa"
    "@sha256:bc30725d441e32406f2ee237bb3f0b720b105ca3b7f0c43b3d68918a23a5a954"
)
# runtimeVersionOverride of the public image (the examples) and of the image of the frozen reference cells.
RUNTIME_VERSION = "1.6.1"
REFERENCE_RUNTIME_VERSION = "1.6.0"
# The runtime image the frozen fixtures/cells_reference carry (an earlier build, not the public image).
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
# cell -> the examples/ folder whose manifest.yaml is that cell
EXAMPLES: dict[str, str] = {
    "S0": "00-baseline", "S1": "01-kv-router", "S2": "02-hicache", "S3": "03-pd-disaggregation",
    "S4": "04-kv-router-hicache", "S5": "05-kv-router-pd", "S6": "06-pd-hicache", "S7": "07-all-three",
}

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
    "FI_EFA_ENABLE_SHM_TRANSFER": "0", "FI_EFA_ENABLE_SHM": "0", "LD_LIBRARY_PATH": EFA_LD,
}
# The launch script of every worker of the examples (the aggregated reference cells ran this one).
WRAPPER = 'set -euo pipefail\nulimit -l unlimited || true\nulimit -n 1048576 || true\nexec python3 -m dynamo.sglang "$@"\n'
# memory (request, limit) per kind (reference v2 MEMORY table, node-fit sized)
MEMORY = {
    "plain": ("64Gi", "128Gi"), "decode": ("64Gi", "64Gi"), "hicache-agg": ("200Gi", "224Gi"),
    "hicache-pd": ("386Gi", "410Gi"), "frontend": ("16Gi", "16Gi"),
}
HICACHE_SIZE = {"agg": "200", "prefill": "400"}


def l3_path(topology: str) -> str:
    return f"{L3_ROOT}/{TOPOLOGIES[topology]}"


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


def example_file(cell: str) -> Path:
    """The published manifest of ``cell``: ``examples/<folder>/manifest.yaml``."""
    return L.EXAMPLES / EXAMPLES[cell] / "manifest.yaml"


def example_docs(cell: str) -> list[dict[str, Any]]:
    """The parsed documents of the published manifest of ``cell``."""
    return L.example_docs(EXAMPLES[cell])


def cell_doc(cell: str) -> dict[str, Any]:
    """The one DynamoGraphDeployment of the published manifest of ``cell``."""
    found = L.dgds(example_docs(cell))
    assert len(found) == 1, f"{example_file(cell).relative_to(L.REPO_ROOT)} must hold exactly one DynamoGraphDeployment"
    return found[0]


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


def _mask_runtime_image(node: Any, runtime_version: str) -> None:
    if isinstance(node, dict):
        for k, v in node.items():
            if k == "image" and v in (IMAGE, REFERENCE_IMAGE):
                node[k] = "<runtime image>"
            elif k == "runtimeVersionOverride" and v == runtime_version:
                node[k] = "<runtime version>"
            else:
                _mask_runtime_image(v, runtime_version)
    elif isinstance(node, list):
        for v in node:
            _mask_runtime_image(v, runtime_version)


def _unprivileged_pd(dgd: dict[str, Any]) -> None:
    """Give the prefill and decode workers of a reference cell the launch of the examples (see ``normalised``).

    Each replaced value is checked first, so any other drift in the reference launch still fails the comparison.
    """
    head, tail = WRAPPER.split("exec ")[0], 'exec python3 -m dynamo.sglang "$@"'
    for comp in workers_of(dgd):
        if comp["type"] in ("prefill", "decode"):
            main, name = main_of(comp), comp["name"]
            old = main["args"][0]
            assert old.startswith(head) and old.rstrip().endswith(tail) and "GPU pin" in old, name
            main["args"][0] = WRAPPER
            pin_env = [e for e in main["env"] if e["name"] == "EXPECTED_GPUS"]
            assert pin_env == [{"name": "EXPECTED_GPUS", "value": "1"}], (name, pin_env)
            main["env"] = [e for e in main["env"] if e["name"] != "EXPECTED_GPUS"]
            sc = main["securityContext"]
            assert sc.pop("privileged") is True, name
            assert sc.pop("capabilities") == {"add": ["IPC_LOCK", "SYS_RESOURCE"]}, name


def normalised(dgd: dict[str, Any], *, reference: bool = False) -> dict[str, Any]:
    """Drop what is allowed to differ between a published manifest and the reference render: annotations, comment-like metadata.

    Kept: name, namespace, the part-of label, and the whole ``spec`` (every flag, env, volume, resource, probe).
    Two declared differences:

    * the runtime image: the reference names REFERENCE_IMAGE (runtime 1.6.0), the examples run the
      public image (IMAGE, runtime 1.6.1); both images are replaced by a token, and so is ``runtimeVersionOverride`` when it
      is the value of its own side (REFERENCE_RUNTIME_VERSION with ``reference=True``, else RUNTIME_VERSION). Every other
      ``image`` and override value is kept, so a stale override still differs;
    * the launch of the prefill and decode workers: the reference ran them with ``privileged: true``, added capabilities and
      a GPU-pin preamble fed by ``EXPECTED_GPUS``; the examples run them with none of these and with the WRAPPER of the
      aggregated workers. With ``reference=True`` those four are replaced by the examples' values.
    """
    out = copy.deepcopy(dgd)
    _mask_runtime_image(out, REFERENCE_RUNTIME_VERSION if reference else RUNTIME_VERSION)
    if reference:
        _unprivileged_pd(out)
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
