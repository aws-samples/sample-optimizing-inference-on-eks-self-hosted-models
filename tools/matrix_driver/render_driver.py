# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Render the Qwen3.8 matrix driver objects from the repo: the driver half of render_cells.py (v4),
the renderer of the recorded runs.

That renderer (artifact 20261023T214411Z-w3-16k) produced the objects of every recorded run. This port keeps
them unchanged except for four parametrization deltas:
  * the eight cell DGDs are the committed Track Q renders (manifests/track-q-qwen38/rendered/S*.yaml) plus the
    ``q38-matrix/workers-sha256`` annotation the driver compares before adopting a live DGD (sha256 of
    ``json.dumps(spec.components[1:], sort_keys=True)``); no DGD is generated here;
  * account, region, buckets and registry come from manifests/config/platform.env (``--env``), the driver
    image tags and digests from manifests/track-q-qwen38/driver/images.env (``--images``); the shipped plans
    (``--plans-dir``: plans/w3-16k, the W3-16K plans, by default; plans/w2-long-context holds plan-e3..e7) carry
    ``${KEY}`` placeholders, expanded byte-exact;
  * the watchdog windows and CronJob schedule are parameters (defaults: the values of the recorded runs);
  * the driver Job of a plan with take steps carries COMPOSE_FFMPEG_IMAGE (the ported driver reads it from its env).
The one addition is ``--hard-stop-in``: a launch whose hard stop is ``ceil(now) + DURATION`` instead of the plan's.

Usage::

    render_driver.py --out DIR --wheel WHEEL [--env F] [--images F] [--plans-dir DIR]
                     [--watchdog-windows START/END[,START/END...]] [--watchdog-schedule CRON]
        -> DIR/00-base.yaml  DIR/05-take-preflight.yaml  DIR/20-watchdog.yaml
    render_driver.py --out DIR --launch PLAN --cb-node ID [--now EPOCH] [--env F] [--images F] [--plans-dir DIR]
                     [--hard-stop-in <N>s|m|h]
        -> DIR/10-job-PLAN-<UTC %Y%m%dT%H%M%SZ>.yaml   (activeDeadlineSeconds = plan hard stop - now)
           with --hard-stop-in also DIR/08-plans-PLAN-<same ts>.yaml (ConfigMap q38-matrix-plans: the plan with only
           its hard_stop line set to ceil(now) + DURATION) and DIR/20-watchdog.yaml (schedule ``*/5 * * * *``, one
           window from that hard stop to 30 min after it)

Needs PyYAML (it parses the committed renders); everything else is stdlib.
"""

from __future__ import annotations

import argparse
import base64
import calendar
import contextlib
import hashlib
import importlib.util
import json
import math
import os
import re
import sys
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any

import yaml

TOOL = Path(__file__).resolve().parent
REPO = TOOL.parent.parent
PLATFORM_ENV = REPO / "manifests" / "config" / "platform.env"
IMAGES_ENV = REPO / "manifests" / "track-q-qwen38" / "driver" / "images.env"
RENDERED_DGDS = REPO / "manifests" / "track-q-qwen38" / "rendered"
PLANS_DIR = REPO / "plans" / "w3-16k"

CELL_FILES = [f"S{i}.yaml" for i in range(8)]
WORKERS_SHA = "q38-matrix/workers-sha256"
PART_OF_VALUE = "qwen38-matrix"
PART_OF = {"app.kubernetes.io/part-of": PART_OF_VALUE}
GPU_NODE = {"karpenter.sh/nodepool": "gpu-b200-cb"}
TOLERATIONS = [{"key": "nvidia.com/gpu", "operator": "Exists", "effect": "NoSchedule"}]
# Watchdog of the recorded runs (v4): it acts in [04:30Z, 05:00Z) (matrix plans e3-e6) and [09:00Z, 10:00Z) (E7' takes) of
# 2026-10-24 and fires every 5 min at 04:xx and 09:xx (the 04:00-04:25 firings are no-ops). HARD_STOP/WINDOW_END stay
# the legacy pair = the first window.
WATCHDOG_WINDOWS = "2026-10-24T04:30:00Z/2026-10-24T05:00:00Z,2026-10-24T09:00:00Z/2026-10-24T10:00:00Z"
WATCHDOG_SCHEDULE = "*/5 4,9 24 10 *"
# --hard-stop-in: the launch's own watchdog fires every 5 min and acts from the hard stop to 30 min after it.
RELATIVE_SCHEDULE = "*/5 * * * *"
RELATIVE_WINDOW_S = 1800
DURATION = re.compile(r"([0-9]+)([smh])")
DURATION_UNIT_S = {"s": 1, "m": 60, "h": 3600}
GRAFANA_URL = "http://prometheus-grafana.monitoring.svc.cluster.local"
PROMETHEUS_URL = "http://prometheus-kube-prometheus-prometheus.monitoring.svc:9090"
L3_ROOT = f"/mnt/.ephemeral/hicache-l3/{PART_OF_VALUE}"
DRIVER_POD = ("16Gi", "32Gi")
WATCHDOG_POD = ("128Mi", "256Mi")
DRIVER_LABELS = {"app.kubernetes.io/name": "q38-matrix-driver", **PART_OF}
PLACEHOLDER = re.compile(r"\$\{([A-Za-z0-9_]+)\}")
ISO_Z = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")
TAKE_STEP = re.compile(r"(?m)^take\s")


@dataclass(frozen=True)
class Platform:
    """The account/region values of one render (platform.env + images.env).

    Attributes:
        bucket: The results bucket (RESULTS_BUCKET).
        region: The AWS region of the bucket and the cluster (AWS_REGION).
        bench_image: Driver and watchdog image, BENCH_REPO:MATRIX_BENCH_TAG@MATRIX_BENCH_DIGEST.
        ffmpeg_image: Compose render image, BENCH_REPO:MATRIX_FFMPEG_TAG@MATRIX_FFMPEG_DIGEST.
        cleanup_image: Image of the L3 cleanup pod: the Track Q image (RUNTIME_IMAGE).
    """

    bucket: str
    region: str
    bench_image: str
    ffmpeg_image: str
    cleanup_image: str


def log(msg: str, **kw: Any) -> None:
    """One structured JSON log line on stdout.

    Args:
        msg: The event.
        **kw: Event fields.
    """
    print(json.dumps({"tool": "render_driver", "msg": msg, **kw}, default=str), flush=True)


# ---------------------------------------------------------------- inputs
def load_platform_env(path: Path) -> dict[str, str]:
    """Read a plain KEY=VALUE file (platform.env, images.env); blank lines and ``#`` comments are skipped.

    Args:
        path: The file.

    Returns:
        The key/value pairs (the value is everything after the first ``=``).

    Raises:
        ValueError: A line is not KEY=VALUE.
    """
    out: dict[str, str] = {}
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if line and not line.startswith("#"):
            key, sep, value = line.partition("=")
            if not sep:
                raise ValueError(f"{path}: not a KEY=VALUE line: {line!r}")
            out[key] = value
    return out


def expand_plan(text: str, env: dict[str, str]) -> str:
    """Replace every ``${KEY}`` of a shipped plan by ``env[KEY]``; every other byte is kept.

    Args:
        text: The plan text.
        env: The platform.env values.

    Returns:
        The plan the driver gets: the plan text with only its ``${KEY}`` placeholders replaced.

    Raises:
        KeyError: A placeholder names a key absent from ``env``.
        ValueError: A ``${`` that is not a ``${KEY}`` placeholder (no silent hole).
    """

    def value(m: re.Match[str]) -> str:
        if m.group(1) not in env:
            raise KeyError(f"plan placeholder ${{{m.group(1)}}} is not a platform.env key")
        return env[m.group(1)]

    if "${" in PLACEHOLDER.sub("", text):
        raise ValueError("plan has a '${' that is not a ${KEY} placeholder")
    return PLACEHOLDER.sub(value, text)


def platform(env: dict[str, str], images: dict[str, str]) -> Platform:
    """The render values from platform.env and images.env.

    Args:
        env: platform.env values.
        images: images.env values.

    Returns:
        The values every object takes its account/region parts from.
    """
    repo = env["BENCH_REPO"]
    return Platform(bucket=env["RESULTS_BUCKET"], region=env["AWS_REGION"],
                    bench_image=f"{repo}:{images['MATRIX_BENCH_TAG']}@{images['MATRIX_BENCH_DIGEST']}",
                    ffmpeg_image=f"{repo}:{images['MATRIX_FFMPEG_TAG']}@{images['MATRIX_FFMPEG_DIGEST']}",
                    cleanup_image=env["RUNTIME_IMAGE"])


def annotated_dgds(src_dir: Path) -> dict[str, str]:
    """The committed cell renders with the ``q38-matrix/workers-sha256`` annotation the driver requires.

    The annotation is the identity of everything but the Frontend (equal for the two router arms of a topology): the
    driver adopts a live DGD only when it matches, else it tears it down and deploys the file. The text of each
    render is kept; only the annotation block is inserted under the top-level ``metadata:``.

    Args:
        src_dir: manifests/track-q-qwen38/rendered.

    Returns:
        ``{"S0.yaml": text, ..., "S7.yaml": text}``.

    Raises:
        ValueError: A cell file is missing, already has metadata annotations, or the insertion did not parse back to
            the render plus the annotation.
    """
    out: dict[str, str] = {}
    for name in CELL_FILES:
        path = src_dir / name
        text = path.read_text()
        doc = yaml.safe_load(text)
        sha = hashlib.sha256(json.dumps(doc["spec"]["components"][1:], sort_keys=True).encode()).hexdigest()
        top = re.search(r"(?m)^metadata:\n", text)
        if "annotations" in doc["metadata"] or top is None:
            raise ValueError(f"{path}: expected a top-level metadata block without annotations")
        new = f'{text[: top.end()]}  annotations:\n    {WORKERS_SHA}: "{sha}"\n{text[top.end():]}'
        want = {**doc, "metadata": {"annotations": {WORKERS_SHA: sha}, **doc["metadata"]}}
        if yaml.safe_load(new) != want:
            raise ValueError(f"{path}: the {WORKERS_SHA} annotation did not land under metadata")
        out[name] = new
    return out


def load_driver() -> ModuleType:
    """matrix_driver.py next to this file, loaded by path (also under ``python3 -I``; nothing left in sys.modules).

    Returns:
        The driver module (its tables and take_preflight_job).

    Raises:
        SystemExit: The driver source cannot be loaded.
    """
    name = "_q38_matrix_driver_render"
    spec = importlib.util.spec_from_file_location(name, TOOL / "matrix_driver.py")
    if spec is None or spec.loader is None:
        raise SystemExit(f"cannot load {TOOL / 'matrix_driver.py'}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod  # dataclasses resolve string annotations through sys.modules while the module executes
    prev, sys.dont_write_bytecode = sys.dont_write_bytecode, True  # no __pycache__ from a render
    try:
        spec.loader.exec_module(mod)
    finally:
        sys.dont_write_bytecode = prev
        del sys.modules[name]
    return mod


@contextlib.contextmanager
def environ(**values: str) -> Iterator[None]:
    """Set environment variables for the block and restore the previous values after it.

    Args:
        **values: Variables to set.

    Yields:
        Nothing.
    """
    saved = {k: os.environ.get(k) for k in values}
    os.environ.update(values)
    try:
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


# ---------------------------------------------------------------- driver objects (as render_cells.py v4)
def env(name: str, value: str) -> dict[str, Any]:
    """One literal env entry.

    Args:
        name: Variable name.
        value: Variable value.

    Returns:
        The container env entry.
    """
    return {"name": name, "value": value}


def iso_epoch(ts: str) -> int:
    """Epoch seconds of a YYYY-MM-DDTHH:MM:SSZ instant.

    Args:
        ts: The instant.

    Returns:
        Seconds since the epoch (UTC).
    """
    return calendar.timegm(time.strptime(ts, "%Y-%m-%dT%H:%M:%SZ"))


def binding(kind: str, name: str, ns: str | None) -> dict[str, Any]:
    """(Cluster)RoleBinding of the matrix-driver ServiceAccount.

    Args:
        kind: ``Role`` or ``ClusterRole``.
        name: Name of the role and of the binding.
        ns: Namespace of a RoleBinding; None for a ClusterRoleBinding.

    Returns:
        The binding object.
    """
    meta: dict[str, Any] = {"name": name, "labels": DRIVER_LABELS}
    if ns:
        meta["namespace"] = ns
    return {"apiVersion": "rbac.authorization.k8s.io/v1", "kind": f"{kind}Binding", "metadata": meta,
            "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": kind, "name": name},
            "subjects": [{"kind": "ServiceAccount", "name": "matrix-driver", "namespace": "bench"}]}


def driver_objects(md: ModuleType, cm_driver: dict[str, Any], cm_dgds: dict[str, Any], cm_plans: dict[str, Any],
                   cm_takes: dict[str, Any]) -> list[dict[str, Any]]:
    """ServiceAccounts, least-privilege RBAC and the four ConfigMaps (Jobs and the watchdog are separate files).

    Args:
        md: The driver module (its scenario namespace tables).
        cm_driver: ConfigMap q38-matrix-driver.
        cm_dgds: ConfigMap q38-matrix-dgds.
        cm_plans: ConfigMap q38-matrix-plans.
        cm_takes: ConfigMap q38-take-capture.

    Returns:
        The 00-base.yaml documents.
    """
    namespaces: list[str] = md.NAMESPACES
    hicache_namespaces: list[str] = md.HICACHE_NAMESPACES
    objs: list[dict[str, Any]] = []
    for sa in ("matrix-driver", "bench"):
        # Referenced by the Pod Identity associations (the results-writer role of the platform).
        objs.append({"apiVersion": "v1", "kind": "ServiceAccount",
                     "metadata": {"name": sa, "namespace": "bench", "labels": {"app.kubernetes.io/name": sa}}})
    for ns in namespaces:
        # pods create only where the L3 cleanup pod runs (HiCache namespaces); delete: the T-RTR-2 Frontend
        # recreate fallback and the cleanup pod
        pod_verbs = ["get", "list", "watch", "delete", *(["create"] if ns in hicache_namespaces else [])]
        objs.append({"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "Role",
                     "metadata": {"name": "q38-matrix-driver", "namespace": ns, "labels": DRIVER_LABELS},
                     "rules": [
                         {"apiGroups": ["nvidia.com"], "resources": ["dynamographdeployments"],
                          "verbs": ["get", "list", "watch", "create", "patch", "delete"]},
                         {"apiGroups": [""], "resources": ["pods"], "verbs": pod_verbs},
                         {"apiGroups": [""], "resources": ["pods/log"], "verbs": ["get"]},
                         {"apiGroups": [""], "resources": ["pods/exec"], "verbs": ["create"]},
                         {"apiGroups": [""], "resources": ["events"], "verbs": ["get", "list"]},
                     ]})
        objs.append(binding("Role", "q38-matrix-driver", ns))
    # bench: the driver reads its own Job (deadline check) and the watchdog CronJob; the watchdog (same SA) deletes
    # the driver Jobs; the driver creates the capture and compose Jobs of the E7' takes (create is the v4 addition).
    objs.append({"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "Role",
                 "metadata": {"name": "q38-matrix-bench", "namespace": "bench", "labels": DRIVER_LABELS},
                 "rules": [{"apiGroups": ["batch"], "resources": ["jobs"],
                            "verbs": ["get", "list", "delete", "create"]},
                           {"apiGroups": ["batch"], "resources": ["cronjobs"], "verbs": ["get"]}]})
    objs.append(binding("Role", "q38-matrix-bench", "bench"))
    # Read-only, cluster-wide: GPU-free check (pods on the CB node, any namespace), the node, any q38 DGD anywhere.
    objs.append({"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "ClusterRole",
                 "metadata": {"name": "q38-matrix-driver-read", "labels": DRIVER_LABELS},
                 "rules": [{"apiGroups": [""], "resources": ["pods", "nodes"], "verbs": ["get", "list"]},
                           {"apiGroups": ["nvidia.com"], "resources": ["dynamographdeployments"],
                            "verbs": ["get", "list"]}]})
    objs.append(binding("ClusterRole", "q38-matrix-driver-read", None))
    return [*objs, cm_driver, cm_dgds, cm_plans, cm_takes]


def take_preflight_objects(md: ModuleType, plans: dict[str, str], cm_takes: dict[str, Any],
                           p: Platform) -> list[dict[str, Any]]:
    """05-take-preflight.yaml: the take scripts ConfigMap and one preflight Job per run_id of a plan with take steps.

    Built by the driver's own take_preflight_job (same images, pins and Secret ref as the takes).

    Args:
        md: The driver module.
        plans: Expanded plans by file name.
        cm_takes: ConfigMap q38-take-capture (identical to the one in 00-base.yaml).
        p: Render values.

    Returns:
        The 05-take-preflight.yaml documents.
    """
    run_ids = sorted({m.group(1) for text in plans.values()
                      if TAKE_STEP.search(text) and (m := re.search(r"(?m)^run_id\s+(\S+)", text))})
    with environ(COMPOSE_FFMPEG_IMAGE=p.ffmpeg_image):  # the driver reads the ffmpeg image from its Job env
        jobs: list[dict[str, Any]] = [md.take_preflight_job(run_id=r, bucket=p.bucket, region=p.region,
                                                            grafana_url=GRAFANA_URL) for r in run_ids]
    return [cm_takes, *jobs]


def plan_hard_stop(text: str) -> str:
    """The plan's ``hard_stop`` header instant.

    Args:
        text: The plan.

    Returns:
        The YYYY-MM-DDTHH:MM:SSZ instant.

    Raises:
        SystemExit: The plan has no hard_stop line.
    """
    m = re.search(r"(?m)^hard_stop\s+([^\s#]+)", text)
    if m is None:
        raise SystemExit("plan has no hard_stop line")
    return m.group(1)


def iso_z(epoch: int) -> str:
    """The YYYY-MM-DDTHH:MM:SSZ instant of an epoch.

    Args:
        epoch: Seconds since the epoch (UTC).

    Returns:
        The instant.
    """
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


def parse_duration(text: str) -> int:
    """Seconds of a ``--hard-stop-in`` value: a positive whole number with a unit, ``s``, ``m`` or ``h``.

    Args:
        text: The value, e.g. ``8h``, ``90m``, ``3600s``.

    Returns:
        The duration in seconds (> 0).

    Raises:
        ValueError: Anything else (no unit, another unit, a sign, a fraction, spaces, zero).
    """
    m = DURATION.fullmatch(text)
    if m is None or int(m.group(1)) == 0:
        raise ValueError(f"expected a positive whole number with s, m or h (e.g. 8h), got {text!r}")
    return int(m.group(1)) * DURATION_UNIT_S[m.group(2)]


def with_hard_stop(text: str, hard_stop: str) -> str:
    """The plan with only the instant of its ``hard_stop`` line replaced; every other byte is kept.

    Args:
        text: The expanded plan.
        hard_stop: The new YYYY-MM-DDTHH:MM:SSZ instant.

    Returns:
        The re-timed plan.

    Raises:
        SystemExit: The plan has no hard_stop line.
    """
    new, n = re.subn(r"(?m)^(hard_stop[ \t]+)[^\s#]+", lambda m: m.group(1) + hard_stop, text, count=1)
    if n != 1:
        raise SystemExit("plan has no hard_stop line")
    return new


def job(plan: str, text: str, now: float, p: Platform, cb_node: str) -> dict[str, Any]:
    """One driver Job per plan, ending at the plan's hard stop.

    activeDeadlineSeconds counts from the Job start, so the Job is rendered at launch. No automatic retry: a relaunch
    of the same plan resumes (done runs skipped).

    Args:
        plan: Plan name (``plan-eN``).
        text: The expanded plan.
        now: Launch instant (epoch seconds).
        p: Render values.
        cb_node: Instance id of the Capacity Block GPU node.

    Returns:
        The Job.

    Raises:
        SystemExit: The plan's hard stop has already passed.
    """
    hard_stop = plan_hard_stop(text)
    ads = iso_epoch(hard_stop) - math.ceil(now)
    if ads <= 0:
        raise SystemExit(f"{plan}: hard stop {hard_stop} already passed")
    name = f"q38-matrix-{plan.removeprefix('plan-')}"
    labels = {**DRIVER_LABELS, "q38-matrix/plan": plan}
    takes = [env("COMPOSE_FFMPEG_IMAGE", p.ffmpeg_image)] if TAKE_STEP.search(text) else []
    return {
        "apiVersion": "batch/v1", "kind": "Job",
        "metadata": {"name": name, "namespace": "bench", "labels": labels,
                     "annotations": {"q38-matrix/rendered-at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
                                     "q38-matrix/hard-stop": hard_stop}},
        "spec": {
            "backoffLimit": 0, "completions": 1, "parallelism": 1, "ttlSecondsAfterFinished": 172800,
            "activeDeadlineSeconds": ads,
            "template": {
                "metadata": {"labels": labels, "annotations": {"karpenter.sh/do-not-disrupt": "true"}},
                "spec": {
                    "serviceAccountName": "matrix-driver", "restartPolicy": "Never",
                    "terminationGracePeriodSeconds": 600,  # SIGTERM -> the driver tears down (<= 240 s wait)
                    # Same node as the DGDs: the path E1's in-cluster drivers proved (pod IP :9090 scrapes).
                    "nodeSelector": dict(GPU_NODE), "tolerations": TOLERATIONS,
                    "securityContext": {"runAsNonRoot": True, "runAsUser": 10001, "runAsGroup": 10001,
                                        "fsGroup": 10001, "seccompProfile": {"type": "RuntimeDefault"}},
                    "containers": [{
                        "name": "driver", "image": p.bench_image, "imagePullPolicy": "IfNotPresent",
                        "command": ["python3", "-I", "/etc/q38/driver/matrix_driver.py"],
                        "args": ["--plan", f"/etc/q38/plans/{plan}.txt"],
                        "env": [env("AWS_REGION", p.region), env("AWS_DEFAULT_REGION", p.region),
                                env("RESULTS_BUCKET", p.bucket), env("WORK", "/work"),
                                env("DGD_DIR", "/etc/q38/dgds"), env("DRIVER_DIR", "/etc/q38/driver"),
                                env("CB_NODE", cb_node), env("HARD_STOP", hard_stop),
                                {"name": "JOB_NAME", "valueFrom": {"fieldRef": {
                                    "fieldPath": "metadata.labels['batch.kubernetes.io/job-name']"}}},
                                env("L3_CLEANUP_IMAGE", p.cleanup_image), env("L3_ROOT", L3_ROOT),
                                env("PROMETHEUS_URL", PROMETHEUS_URL), env("GRAFANA_URL", GRAFANA_URL), *takes,
                                env("HOME", "/work/home"), env("HF_HUB_OFFLINE", "1"),
                                env("TRANSFORMERS_OFFLINE", "1")],
                        "resources": {"requests": {"cpu": "4", "memory": DRIVER_POD[0]},
                                      "limits": {"cpu": "8", "memory": DRIVER_POD[1]}},
                        "securityContext": {"allowPrivilegeEscalation": False, "capabilities": {"drop": ["ALL"]}},
                        "volumeMounts": [
                            {"name": "driver", "mountPath": "/etc/q38/driver", "readOnly": True},
                            {"name": "dgds", "mountPath": "/etc/q38/dgds", "readOnly": True},
                            {"name": "plans", "mountPath": "/etc/q38/plans", "readOnly": True},
                            {"name": "work", "mountPath": "/work"},
                        ],
                    }],
                    "volumes": [
                        {"name": "driver", "configMap": {"name": "q38-matrix-driver"}},
                        {"name": "dgds", "configMap": {"name": "q38-matrix-dgds"}},
                        {"name": "plans", "configMap": {"name": "q38-matrix-plans"}},
                        {"name": "work", "emptyDir": {"sizeLimit": "40Gi"}},
                    ],
                },
            },
        },
    }


def legacy_pair(windows: str) -> tuple[str, str]:
    """The watchdog's legacy HARD_STOP/WINDOW_END pair: the first window.

    Args:
        windows: ``START/END[,START/END...]`` in YYYY-MM-DDTHH:MM:SSZ.

    Returns:
        (start, end) of the first window.

    Raises:
        SystemExit: A window is not START/END in that format.
    """
    pairs = [w.partition("/") for w in windows.split(",")]
    if not all(ISO_Z.fullmatch(a) and sep and ISO_Z.fullmatch(b) for a, sep, b in pairs):
        raise SystemExit(f"--watchdog-windows {windows!r}: expected START/END[,START/END...] as YYYY-MM-DDTHH:MM:SSZ")
    return pairs[0][0], pairs[0][2]


def watchdog_objects(watchdog_py: str, windows: str, schedule: str, p: Platform) -> list[dict[str, Any]]:
    """The watchdog's own ConfigMap and CronJob: independent of the driver's ConfigMap, Job and process.

    Args:
        watchdog_py: Source of watchdog.py.
        windows: WINDOWS of the CronJob (``START/END[,START/END...]``).
        schedule: Cron schedule (Etc/UTC).
        p: Render values.

    Returns:
        The 20-watchdog.yaml documents.
    """
    hard_stop, window_end = legacy_pair(windows)
    labels = {"app.kubernetes.io/name": "q38-matrix-watchdog", **PART_OF}
    cm = configmap("q38-matrix-watchdog", {"watchdog.py": watchdog_py})
    cm["metadata"]["labels"] = labels
    pod = {
        "serviceAccountName": "matrix-driver", "restartPolicy": "Never",
        "nodeSelector": dict(GPU_NODE), "tolerations": TOLERATIONS,
        "securityContext": {"runAsNonRoot": True, "runAsUser": 10001, "runAsGroup": 10001,
                            "seccompProfile": {"type": "RuntimeDefault"}},
        "containers": [{
            "name": "watchdog", "image": p.bench_image, "imagePullPolicy": "IfNotPresent",
            "command": ["python3", "-I", "/etc/q38/watchdog/watchdog.py"],
            "env": [env("HARD_STOP", hard_stop), env("WINDOW_END", window_end),
                    env("WINDOWS", windows), env("HOME", "/tmp")],
            "resources": {"requests": {"cpu": "100m", "memory": WATCHDOG_POD[0]},
                          "limits": {"memory": WATCHDOG_POD[1]}},
            "securityContext": {"allowPrivilegeEscalation": False, "capabilities": {"drop": ["ALL"]}},
            "volumeMounts": [{"name": "watchdog", "mountPath": "/etc/q38/watchdog", "readOnly": True}],
        }],
        "volumes": [{"name": "watchdog", "configMap": {"name": "q38-matrix-watchdog"}}],
    }
    cj = {"apiVersion": "batch/v1", "kind": "CronJob",
          "metadata": {"name": "q38-matrix-watchdog", "namespace": "bench", "labels": labels},
          "spec": {"schedule": schedule, "timeZone": "Etc/UTC", "concurrencyPolicy": "Forbid",
                   "startingDeadlineSeconds": 120, "successfulJobsHistoryLimit": 6, "failedJobsHistoryLimit": 6,
                   "jobTemplate": {"metadata": {"labels": labels},
                                   "spec": {"backoffLimit": 1, "activeDeadlineSeconds": 900,
                                            "ttlSecondsAfterFinished": 172800,
                                            "template": {"metadata": {"labels": labels}, "spec": pod}}}}}
    return [cm, cj]


# ---------------------------------------------------------------- minimal YAML emitter (as render_cells.py v4)
_SAFE = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_./-")
_AMBIG = {"true", "false", "yes", "no", "on", "off", "null", "y", "n", "~", ""}


def scalar(v: Any) -> str:
    """YAML scalar: plain when unambiguous, else JSON-quoted.

    Args:
        v: The value.

    Returns:
        Its YAML text.
    """
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    s = str(v)
    plain = set(s) <= _SAFE and s.lower() not in _AMBIG and not s[0].isdigit() and not s.startswith(("-", "."))
    return s if plain else json.dumps(s)


def emit(obj: Any, ind: int = 0) -> list[str]:
    """Block-style YAML lines.

    Args:
        obj: A mapping, a list or a scalar.
        ind: Indentation in spaces.

    Returns:
        The lines.
    """
    pad = " " * ind
    out: list[str] = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, (dict, list)) and v:
                out.append(f"{pad}{k}:")
                out += emit(v, ind + 2)
            elif isinstance(v, str) and "\n" in v:
                out.append(f"{pad}{k}: |")
                out += [f"{pad}  {line}" if line else "" for line in v.rstrip("\n").split("\n")]
            else:
                empty = ("{}" if isinstance(v, dict) else "[]") if isinstance(v, (dict, list)) else scalar(v)
                out.append(f"{pad}{k}: {empty}")
    elif isinstance(obj, list):
        for v in obj:
            if isinstance(v, dict) and v:
                sub = emit(v, ind + 2)
                out.append(f"{pad}- {sub[0].lstrip()}")
                out += sub[1:]
            elif isinstance(v, str) and "\n" in v:
                out.append(f"{pad}- |")
                out += [f"{pad}  {line}" if line else "" for line in v.rstrip("\n").split("\n")]
            else:
                out.append(f"{pad}- {scalar(v)}")
    return out


def write_yaml(path: Path, header: str, docs: list[dict[str, Any]]) -> None:
    """Write one or more documents with a comment header.

    Args:
        path: Output file.
        header: Comment text (one ``#`` line per line).
        docs: The documents.
    """
    head = "".join(f"# {line}\n" if line else "#\n" for line in header.strip("\n").split("\n"))
    body = "\n---\n".join("\n".join(emit(d)) for d in docs)
    path.write_text(head + body + "\n")
    log("wrote", path=str(path), docs=len(docs))


def configmap(name: str, data: dict[str, str], binary: dict[str, bytes] | None = None) -> dict[str, Any]:
    """A ConfigMap in bench with text data and optional binaryData (base64).

    Args:
        name: ConfigMap name.
        data: Text keys.
        binary: Binary keys.

    Returns:
        The ConfigMap.
    """
    cm: dict[str, Any] = {"apiVersion": "v1", "kind": "ConfigMap",
                          "metadata": {"name": name, "namespace": "bench", "labels": dict(DRIVER_LABELS)},
                          "data": data}
    if binary:
        cm["binaryData"] = {k: base64.b64encode(v).decode() for k, v in binary.items()}
    return cm


# ---------------------------------------------------------------- CLI
def write_watchdog(out: Path, windows: str, schedule: str, p: Platform) -> None:
    """Write 20-watchdog.yaml.

    Args:
        out: Output directory.
        windows: Watchdog windows.
        schedule: Watchdog CronJob schedule.
        p: Render values.
    """
    write_yaml(out / "20-watchdog.yaml",
               f"Independent hard-stop watchdog: in the windows {windows} it deletes every q38 matrix DGD,\n"
               "then the take capture/compose Jobs and the driver Jobs (an earlier window holds while a driver Job\n"
               "with a later hard stop is active); outside them it does nothing. Apply before the first driver Job.",
               watchdog_objects((TOOL / "watchdog.py").read_text(), windows, schedule, p))


def render_base(out: Path, wheel: Path, env_values: dict[str, str], p: Platform, *, plans_dir: Path, windows: str,
                schedule: str) -> None:
    """Write 00-base.yaml, 05-take-preflight.yaml and 20-watchdog.yaml.

    Args:
        out: Output directory.
        wheel: The demo_bench wheel the driver installs.
        env_values: platform.env values (plan placeholders).
        p: Render values.
        plans_dir: Directory of the shipped plans (``plan-*.txt``) the plans ConfigMap holds.
        windows: Watchdog windows.
        schedule: Watchdog CronJob schedule.
    """
    legacy_pair(windows)  # refuse a malformed --watchdog-windows before writing anything
    md = load_driver()
    wheel_bytes = wheel.read_bytes()
    cm_driver = configmap("q38-matrix-driver",
                          {"matrix_driver.py": (TOOL / "matrix_driver.py").read_text(),
                           "wheel.sha256": f"{hashlib.sha256(wheel_bytes).hexdigest()}  {wheel.name}\n"},
                          {wheel.name: wheel_bytes})
    cm_dgds = configmap("q38-matrix-dgds", annotated_dgds(RENDERED_DGDS))
    plans = {f.name: expand_plan(f.read_text(), env_values) for f in sorted(plans_dir.glob("plan-*.txt"))}
    cm_plans = configmap("q38-matrix-plans", plans)
    cm_takes = configmap("q38-take-capture", {"capture.py": (TOOL / "takes" / "capture.py").read_text(),
                                              "compose.sh": (TOOL / "takes" / "compose.sh").read_text()})
    write_yaml(out / "00-base.yaml",
               "q38 matrix driver base objects: ServiceAccounts (Pod Identity), RBAC, ConfigMaps (driver +\n"
               "demo_bench wheel, the 8 cell DGDs = manifests/track-q-qwen38/rendered + the workers-sha256\n"
               f"annotation, the plans of {plans_dir.parent.name}/{plans_dir.name} expanded from platform.env, the take\n"
               "capture/compose scripts). Rendered by tools/matrix_driver/render_driver.py; never hand-edit. Apply\n"
               "with --server-side (ConfigMaps > 256 KiB).",
               driver_objects(md, cm_driver, cm_dgds, cm_plans, cm_takes))
    write_yaml(out / "05-take-preflight.yaml",
               "E7' take preflight: the take scripts ConfigMap (identical to the one in 00-base.yaml) and Job\n"
               "bench/q38-take-preflight-<run_id> on cpu-demo, no GPU. Apply with --server-side once Secret\n"
               "bench/demo-grafana-auth and the ffmpeg mirror exist; the Job must Complete before the takes are\n"
               f"launched (its report: s3://{p.bucket}/takes/<run_id>/_preflight/). Re-run: delete the Job first.",
               take_preflight_objects(md, plans, cm_takes, p))
    write_watchdog(out, windows, schedule, p)


def render_launch(out: Path, plan: str, env_values: dict[str, str], p: Platform, *, plans_dir: Path, cb_node: str,
                  now: float, hard_stop_in: int | None = None) -> None:
    """Write the launch-time driver Job of one shipped plan.

    With ``hard_stop_in`` the hard stop is ``ceil(now) + hard_stop_in`` instead of the plan's: the Job, a plans
    ConfigMap holding only the re-timed plan and a watchdog whose one window opens at that instant are written, so the
    four places the driver checks (plan, Job env/annotation, Job deadline, watchdog window) agree.

    Args:
        out: Output directory.
        plan: Plan name (``plan-eN``, a file of ``plans_dir``).
        env_values: platform.env values (plan placeholders).
        p: Render values.
        plans_dir: Directory of the shipped plans.
        cb_node: Instance id of the Capacity Block GPU node.
        now: Launch instant (epoch seconds).
        hard_stop_in: Seconds from ``ceil(now)`` to the hard stop; None keeps the plan's own hard stop.

    Raises:
        SystemExit: The plan is not shipped in ``plans_dir``, or its hard stop has passed.
    """
    path = plans_dir / f"{plan}.txt"
    if not path.is_file():
        raise SystemExit(f"no {plan}.txt in {plans_dir}")
    text = expand_plan(path.read_text(), env_values)
    if hard_stop_in is not None:
        text = with_hard_stop(text, iso_z(math.ceil(now) + hard_stop_in))
    hard_stop = plan_hard_stop(text)
    the_job = job(plan, text, now, p, cb_node)
    ts = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(now))
    write_yaml(out / f"10-job-{plan}-{ts}.yaml",
               f"Launch-time driver Job for {plan}: activeDeadlineSeconds ends the Job at {hard_stop}.\n"
               "Create it right away (kubectl create -f); re-render if more than a few minutes pass.", [the_job])
    if hard_stop_in is None:
        return
    write_yaml(out / f"08-plans-{plan}-{ts}.yaml",
               f"Plans ConfigMap for this launch of {plan} (--hard-stop-in): {plan}.txt with hard_stop {hard_stop}, every\n"
               "other line as shipped. Same name as the plans ConfigMap of 00-base.yaml (re-apply that file to get the\n"
               "shipped plans back). Apply with --server-side, with 20-watchdog.yaml, before the Job.",
               [configmap("q38-matrix-plans", {f"{plan}.txt": text})])
    write_watchdog(out, f"{hard_stop}/{iso_z(iso_epoch(hard_stop) + RELATIVE_WINDOW_S)}", RELATIVE_SCHEDULE, p)


def main(argv: list[str] | None = None) -> int:
    """Render the base, take preflight and watchdog files, or (``--launch``) the launch-time Job of one plan.

    Args:
        argv: Command line without the program name (default: sys.argv[1:]).

    Returns:
        0 on success (refusals raise SystemExit).
    """
    ap = argparse.ArgumentParser(prog="render_driver.py", description=__doc__.split("\n\n")[0] if __doc__ else None)
    ap.add_argument("--out", type=Path, required=True, help="output directory")
    ap.add_argument("--wheel", type=Path, help="the demo_bench wheel the driver installs (base render)")
    ap.add_argument("--env", type=Path, default=PLATFORM_ENV, help="platform.env (default: manifests/config)")
    ap.add_argument("--images", type=Path, default=IMAGES_ENV, help="driver image pins (default: driver/images.env)")
    ap.add_argument("--watchdog-windows", default=WATCHDOG_WINDOWS, help="START/END[,START/END...] (UTC, ...Z)")
    ap.add_argument("--watchdog-schedule", default=WATCHDOG_SCHEDULE, help="watchdog CronJob schedule (Etc/UTC)")
    ap.add_argument("--plans-dir", type=Path, default=PLANS_DIR,
                    help="directory of the shipped plans (default: plans/w3-16k; plan-e3..e7: plans/w2-long-context)")
    ap.add_argument("--launch", metavar="PLAN", help="render only the launch-time Job of <plans-dir>/PLAN.txt")
    ap.add_argument("--cb-node", help="--launch: instance id of the Capacity Block GPU node")
    ap.add_argument("--now", type=float, help="--launch: launch instant in epoch seconds (default: now)")
    ap.add_argument("--hard-stop-in", metavar="DURATION",
                    help="--launch: hard stop = ceil(now) + DURATION (<N>s|m|h, e.g. 8h) instead of the plan's; "
                         "also writes the re-timed plan ConfigMap and its watchdog")
    a = ap.parse_args(argv)
    if a.launch and not a.cb_node:
        ap.error("--launch needs --cb-node (instance id of the Capacity Block GPU node)")
    if not a.launch and a.wheel is None:
        ap.error("--wheel is required (the demo_bench wheel the driver installs)")
    if not a.plans_dir.is_dir():
        ap.error(f"--plans-dir {a.plans_dir}: not a directory")
    hard_stop_in = None
    if a.hard_stop_in is not None:
        if not a.launch:
            ap.error("--hard-stop-in needs --launch")
        try:
            hard_stop_in = parse_duration(a.hard_stop_in)
        except ValueError as exc:
            ap.error(f"--hard-stop-in: {exc}")
    env_values = load_platform_env(a.env)
    p = platform(env_values, load_platform_env(a.images))
    a.out.mkdir(parents=True, exist_ok=True)
    if a.launch:
        render_launch(a.out, a.launch, env_values, p, plans_dir=a.plans_dir, cb_node=a.cb_node,
                      now=time.time() if a.now is None else a.now, hard_stop_in=hard_stop_in)
    else:
        render_base(a.out, a.wheel, env_values, p, plans_dir=a.plans_dir, windows=a.watchdog_windows,
                    schedule=a.watchdog_schedule)
    return 0


if __name__ == "__main__":
    sys.exit(main())
