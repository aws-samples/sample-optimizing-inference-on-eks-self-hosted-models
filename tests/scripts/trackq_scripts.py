# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Shared helpers of the Track Q (Qwen3.8-27B-FP8) script tests (test_130 .. test_132).

Track Q is the only profile of the scripts: there is no ``DEMO_TRACK``.  Nothing here talks to AWS or to a cluster: scripts run
against the stub executables of ``lib_scripts``.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import lib_proc
import lib_scripts as S

CONTEXT = "dynamo-sample-use2"  # CLUSTER_NAME of the placeholder platform.env
ARN = f"arn:aws:eks:us-east-2:123456789012:cluster/{CONTEXT}"
ENV_Q = {"KUBE_CONTEXT": CONTEXT, "KUBERNETES_SERVICE_HOST": ""}
MODEL = "Qwen/Qwen3.8-27B-FP8"
REVISION = "017b9c7af6b5689d5dd426a76e0bc077eb5ca20a"  # also pinned by add-worker.yaml and the staging job
CACHE_REPO = "models--Qwen--Qwen3.8-27B-FP8"
MARKER = f"/mnt/.ephemeral/models/hub/{CACHE_REPO}/.done"
DGD = "q38"
TQ = "track-q-qwen38"
MANIFESTS = S.REPO_ROOT / "manifests"
PLATFORM_ENV = MANIFESTS / "config" / "platform.env"


def platform_config(path: Path = PLATFORM_ENV) -> dict[str, str]:
    """KEY=VALUE pairs of a platform.env (the placeholder config the suite materializes from platform.env.example)."""
    return dict(ln.split("=", 1) for ln in path.read_text().splitlines() if "=" in ln and not ln.startswith("#"))


CFG = platform_config()
NAMESPACES = ("s-agg", "s-agg-hic", "s-dis", "s-dis-hic")
TOPOLOGY_NS = {"t-agg": "s-agg", "t-agg-hic": "s-agg-hic", "t-dis": "s-dis", "t-dis-hic": "s-dis-hic"}
L3_ROOT = "/mnt/.ephemeral/hicache-l3/qwen38-matrix"

# namespace -> {component type: replicas}
LAYOUT = {
    "s-agg": {"worker": 8},
    "s-agg-hic": {"worker": 8},
    "s-dis": {"prefill": 4, "decode": 4},
    "s-dis-hic": {"prefill": 4, "decode": 4},
}


def q38_pods(ns: str, dgd: str = DGD, layout: dict[str, int] | None = None) -> list[dict[str, Any]]:
    """One Frontend + the engine pods of DGD q38 in ``ns`` (agg: 8 x TP1 workers; PD: 4 prefill + 4 decode)."""
    pods = [S.pod("fe-frontend-aaa", "10.0.9.201", "frontend", ns, dgd=dgd)]
    for i, (kind, count) in enumerate((layout or LAYOUT[ns]).items()):
        for n in range(count):
            pods.append(S.pod(f"{kind}-xyz-{n}", f"10.0.{i + 1}.{101 + n}", kind, ns, dgd=dgd))
    return pods


def all_q38_pods() -> dict[str, list[dict[str, Any]]]:
    return {ns: q38_pods(ns) for ns in LAYOUT}


def cluster_calls(res: S.Result) -> list[dict[str, Any]]:
    """kubectl calls that reach the cluster (not ``kustomize`` and not ``--local``)."""
    return [c for c in res.kubectl_calls() if "kustomize" not in c["argv"] and "--local" not in c["argv"]]


def apply_calls(res: S.Result) -> list[dict[str, Any]]:
    return [c for c in res.kubectl_calls() if "apply" in c["argv"] and "--local" not in c["argv"]]


def delete_calls(res: S.Result) -> list[dict[str, Any]]:
    return [c for c in res.kubectl_calls() if "delete" in c["argv"]]


def ns_of(argv: list[str]) -> list[str]:
    out = []
    for i, a in enumerate(argv):
        if a in ("-n", "--namespace") and i + 1 < len(argv):
            out.append(argv[i + 1])
        elif a.startswith("--namespace="):
            out.append(a.split("=", 1)[1])
    return out


def dir_arg_ending(res: S.Result, suffix: str) -> list[dict[str, Any]]:
    """kubectl calls whose argv has a directory path ending in ``suffix``."""
    return [c for c in res.kubectl_calls() if any(a.rstrip("/").endswith(suffix) for a in c["argv"])]


def exec_calls(res: S.Result) -> list[dict[str, Any]]:
    return [c for c in res.kubectl_calls() if "exec" in c["argv"]]


# ----------------------------------------------------------------------------- sourcing lib/common.sh
def run_common(sb: S.Sandbox, snippet: str, env: dict[str, str] | None = None) -> S.Result:
    """Source ``scripts/lib/common.sh`` in a bash with the stubs on PATH and run ``snippet``."""
    sb.reset()
    full = sb._prepare(env or {})
    scripts = S.SCRIPTS_DIR
    cmd = f'SCRIPT_DIR="{scripts}"; . "{scripts}/lib/common.sh"; {snippet}'
    proc = lib_proc.run(
        ["bash", "-c", cmd],
        cwd=sb.work_dir,
        env=full,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    return S.Result(proc.returncode, proc.stdout, proc.stderr, sb.calls(), 0.0)


def common_value(tmp: Path, expr: str, env: dict[str, str] | None = None) -> str:
    """Value printed by ``printf %s <expr>`` after sourcing common.sh (``expr`` is a shell word)."""
    res = run_common(S.Sandbox(tmp, {}), f'printf "%s" {expr}', env)
    assert res.rc == 0, res.stdout + res.stderr
    return res.stdout
