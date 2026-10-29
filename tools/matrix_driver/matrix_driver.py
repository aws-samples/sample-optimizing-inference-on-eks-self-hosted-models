# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Qwen3.8 matrix driver v2. Runs a PLAN fully in-cluster (Job bench/q38-matrix-*, SA matrix-driver).

Stdlib only plus the tools of the bench image (kubectl, aws, aiperf 0.13.0) and the demo_bench wheel
mounted from the ConfigMap (installed into $WORK/pylib, imported first). Ports the logic of the earlier shell
drivers: the matrix loop (ordered plan, deadline, resume by skipping done runs, abort rules), the per-run script
(router arm wait, per-engine reset with busy retries, aiperf fixed-schedule, restart check, run.json + S3) and
demo_bench (reset response semantics, validity/P-37 collapsed outputs, slo-goodput, Prometheus metrics).

v2 (critic fixes, 2026-10-22): preflight before any cluster change (RBAC via kubectl auth can-i, the Job's
activeDeadlineSeconds aligned with the plan hard stop, the independent watchdog CronJob present, the e3 SLO lock for
e4/e5/e6, a plan lock written to S3 first); SIGTERM (Job deadline, watchdog delete) = hard stop; pre-run exclusivity
(no other q38 DGD anywhere, no foreign GPU pod: fatal); a DGD is never reused after a failed or invalid run nor
adopted from an earlier launch (teardown + re-boot); on_failure=abort ends the plan at the first failed/invalid run;
cut_order drops reps 2-3 of the listed cells first when the (I) estimate no longer fits; per-run evidence (GPU UUID
per engine + uniqueness, boot facts incl. LIBFABRIC and HiCache pool lines, per-request worker ids from nvext, eth0
per engine, md127/NVMe diskstats, meminfo, kv_transfer per PD engine, storage-tier counters, Frontend UID around the
reset); L3 namespace directory cleaned (+ df) before each HiCache deploy and at each HiCache teardown.

v3 (E3 first in-cluster run, 2026-10-23): (1) the T-RTR-2 positive control re-sends the canaries in full passes
until a pass hits on the engine counters (Qwen3.8 hybrid engine: a prefix hits from its 3rd send to the same
engine, measured live), at most CONTROL_MAX_SENDS sends; under rr the post-reset repeat must land on the owners of
the hitting pass. (2) teardown deletes the DGD by label + field selector (v2 passed a name with -l, which kubectl
refuses, under check=False), fails on a refused delete and waits until the DGD object and its pods are gone.

v4 (E7' recorded takes, spec section 6 E7'/E8' and section 8, plan-e7): ``take <S0|S7> <rep>`` is one normal run (same
frozen trace, SLO lock, reset-and-verify, validity and teardown-after-invalid) while a capture Job
(bench/demo-capture-<take_id>, headless Chromium, ConfigMap q38-take-capture) records the Grafana take view of the
cell's namespace from the replay start (CAPTURE_LEAD_S after the Job is created) to the trace span + drain; the
webm lands in takes/<run_id>/<take_id>/ and run.json gains a ``take`` block (result_of_record false). A capture
failure never invalidates the run. ``retake_of``/``min_left_s`` re-run a take only when the named one is unusable and
time remains. ``compose`` runs an ffmpeg Job on cpu-demo (side-by-side full clip synced on replay start, a ~target_s
deck clip, a 3-panel still) into takes/<run_id>/compose/ (append-only, compose.json last). The watchdog preflight
accepts a CronJob whose WINDOWS open at the plan hard stop (E7: 09:00Z).

v4 critic fixes (2026-10-23, DRIVER-V4-AMENDMENT section 9): preflight refuses while another driver Job is active;
during the lead the capture pod is watched and a pod blocked on a missing Secret or image (two polls) is a fatal
configuration error before the replay; compose and capture waits end early on such a blocker; a take Job left by an
earlier launch (AlreadyExists) is deleted and re-created once; the take records its actual replay start (first
profiling request) and the compose Job syncs on it (``sync``, degraded beyond CAPTURE_SKEW_TOL_S); capture.json is
read back (video start, sizes; an empty webm fails the capture); run.json carries ``result_of_record`` (False for a
take) and compose.json the TAKE_NOTE marker; the capture pod gets 3Gi/6Gi.

Per run: topology (delete other q38 DGDs, assert exclusive, clean L3, apply, boot watchdog, GPU check, warm-up) or
Frontend-only R toggle (worker UIDs must not change) -> reset + T-RTR-2 verify (canary before reset, flush_cache +
clear_hicache_storage, L3 0 files, the repeated canary must get 0 cached tokens on every engine and 0 router-predicted
hit, on the hit owners under rr, final reset) -> evidence + pre snapshot -> aiperf mooncake_trace --fixed-schedule on the frozen W2 window trace
-> post snapshot -> KPIs, validity, slo-goodput -> s3://$RESULTS_BUCKET/matrix/<run_id>/<cell>/rep<k>/ (append-only,
run.json last). No prompt content is ever logged or uploaded (synthetic token IDs / aiperf synthesis; no PHI here).

Exit: 0 every step done and every run valid, 1 a step failed / a run invalid / fatal, 2 configuration refusal
(before any cluster change), 3 hard stop or SIGTERM reached (remaining steps cut, topology torn down).
"""

from __future__ import annotations

import argparse
import calendar
import errno
import hashlib
import http.client
import itertools
import json
import math
import os
import random
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.parse
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DRIVER_VERSION = "q38-matrix-driver/4"
MODEL = "Qwen/Qwen3.8-27B-FP8"
MODEL_REVISION = "017b9c7af6b5689d5dd426a76e0bc077eb5ca20a"
DGD_NAME = "q38"
PART_OF = "qwen38-matrix"
LABEL_DGD = "nvidia.com/dynamo-graph-deployment-name"
LABEL_TYPE = "nvidia.com/dynamo-component-type"
SYSTEM_PORT = 9090
CONTEXT_LENGTH = 131072
CONTEXT_MARGIN = 256  # chat template + special tokens head-room for the ISL + OSL <= context check (I)
GPUS_PER_NODE = 8
TOKEN_ID_LOW, TOKEN_ID_HIGH = 1000, 150000  # Qwen3.8 regular token IDs (E1 rd_drive.py)

# topology -> namespace, PD, HiCache; cell -> (topology, DYN_ROUTER_MODE). Same table as render_cells.py.
TOPOLOGIES: dict[str, dict[str, Any]] = {
    "t-agg": {"namespace": "s-agg", "pd": False, "hicache": False},
    "t-agg-hic": {"namespace": "s-agg-hic", "pd": False, "hicache": True},
    "t-pd": {"namespace": "s-dis", "pd": True, "hicache": False},
    "t-pd-hic": {"namespace": "s-dis-hic", "pd": True, "hicache": True},
}
CELLS: dict[str, tuple[str, str]] = {
    "S0": ("t-agg", "round-robin"), "S1": ("t-agg", "kv"),
    "S2": ("t-agg-hic", "round-robin"), "S4": ("t-agg-hic", "kv"),
    "S3": ("t-pd", "round-robin"), "S5": ("t-pd", "kv"),
    "S6": ("t-pd-hic", "round-robin"), "S7": ("t-pd-hic", "kv"),
}
NAMESPACES = [t["namespace"] for t in TOPOLOGIES.values()]
HICACHE_NAMESPACES = [t["namespace"] for t in TOPOLOGIES.values() if t["hicache"]]
WATCHDOG = "q38-matrix-watchdog"
# (I) estimates for the "does the next step still fit before the hard stop" guard; overridable by `estimate`.
DEFAULT_ESTIMATE = {"boot_s": 900, "reset_s": 240, "drain_s": 900, "probe_s": 300}
BOOT_TIMEOUT_S = {"agg": 1500, "pd": 1800}  # watchdog (I): E1 measured ~5.5 min apply -> ready per TP1 replica
PENDING_MAX_S = 600
GPU_FREE_TIMEOUT_S = 300  # after our own teardown; then fatal
TEARDOWN_TIMEOUT_S = 900
SIGTERM_TEARDOWN_WAIT_S = 240  # inside the Job's terminationGracePeriodSeconds (600)
JOB_DEADLINE_TOLERANCE_S = 600
MONITOR_PERIOD_S = 30
LOG_HEAD_BYTES = 8 * 1024 * 1024  # boot lines (LIBFABRIC, worker id, HiCache pool) sit in the first ~0.3 MB (E1)
NVEXT_FIELDS = ["worker_id", "timing"]
STORAGE_LABEL = "storage tier (page cache or NVMe)"
SLO_GRID_ITL_MS = (15.0, 25.0, 50.0)  # SLO calibration: sensitivity grid
SLO_GRID_TTFT_S = (0.5, 1.0, 2.0)
DEFAULT_BAND = (0.4, 0.8)  # SLO calibration: acceptance band of the S0 pilot attainment
# T-RTR-2 positive control (v3). Measured live on the S0 deployment (2026-10-23T02:10Z, raw/v3-10): on the Qwen3.8
# hybrid engine a 2048-token prefix is a reusable hit only from its 3rd send to the same engine (sends 1-2: cached 0;
# send 3: 1984 tokens, sglang:cached_tokens_total{cache_source="device"} +1984 on that engine), under rr and pinned
# alike. The canaries are re-sent in full passes until a pass hits, at most CONTROL_MAX_SENDS sends of each.
CANARY_TOKENS = 2048
CONTROL_MAX_SENDS = 5
# v4 takes (E7'). Labels are the agreed on-screen wording; only these two cells have a take.
TAKE_LABELS = {"S0": "Baseline: aggregated + round-robin", "S7": "Router + HiCache + PD disaggregation"}
TAKES_NS = "bench"
TAKE_SCRIPTS_CM = "q38-take-capture"  # capture.py + compose.sh (rendered into 00-base.yaml)
DASHBOARD_UID = "demo-take"  # Grafana "Dynamo demo recording: take view" (var-namespace = the cell namespace)
GRAFANA_SECRET = {"name": "demo-grafana-auth", "key": "password"}  # existing Secret in bench, never read by the driver
CAPTURE_LEAD_S = 240  # capture Job created -> replay start: cpu-demo scheduling, image pull, pip, panel render (I)
CAPTURE_SLACK_S = 900  # capture Job deadline beyond the recording: start-up + PANEL_WAIT_S (180) + upload (I)
CAPTURE_INTERVAL_S = 2  # PNG fallback frame period
COMPOSE_TARGET_S = 170  # the ~2:50 deck clip
COMPOSE_TAIL_S = 30  # kept after the later of the two replay ends in the full clip
COMPOSE_DEADLINE_S = 5400
COMPOSE_ESTIMATE_S = 1800  # (I) guard: fetch + render of 2 x ~40 min 1080p + upload
JOB_POLL_S = 15
# v4 critic fixes. Sync: |actual replay start - planned from_ms| above this flags the take's sync as degraded (E4
# measured: first aiperf request 2.9-12.5 s after the aiperf start, raw/v4-60). Capture pod: request 3Gi so it does
# not land next to Prometheus on a 2nd c6a.xlarge (2.96 GiB free), limit 6Gi as margin for a ~43 min 1080p record
# (the 1Gi Memory-medium /dev/shm counts toward it).
CAPTURE_SKEW_TOL_S = 30
CAPTURE_RESOURCES: dict[str, Any] = {"requests": {"cpu": "1", "memory": "3Gi"}, "limits": {"cpu": "3", "memory": "6Gi"}}
# Waiting reasons of a take pod that never clear by themselves (missing Secret/ConfigMap key, image not pullable).
POD_BLOCKERS = frozenset({"CreateContainerConfigError", "ErrImagePull", "ImagePullBackOff", "InvalidImageName"})
DRIVER_JOB_SELECTOR = "app.kubernetes.io/name=q38-matrix-driver"
TAKE_NOTE = ("TAKE - NOT A RESULT OF RECORD (spec section 8): the clip shows the same traffic on both stacks; "
             "slide numbers come from the matrix (plan-e4..e6)")
# Images pinned by digest. Capture: the capture image of the recorded takes (its Job is not shipped here) with the python
# packages its script needs pinned. Compose: AWS CLI (public ECR, already used by the image mirror) for the S3 legs,
# ffmpeg 9.0.2 (linuxserver/ffmpeg:9.0.2-cli-ls86, index digest) mirrored into the regional ECR (jobs/image-mirror-
# job-ffmpeg.yaml) for the render leg: its reference comes from the Job env COMPOSE_FFMPEG_IMAGE (compose_ffmpeg_image).
CAPTURE_IMAGE = ("mcr.microsoft.com/playwright/python:v1.64.0-noble"
                 "@sha256:684df257a1fa0ef3fa288239819984008f560e8f8db5b635ee9e2a50cbcd2e23")
CAPTURE_PIP = "playwright==1.64.0 boto3==1.43.116"
COMPOSE_AWS_IMAGE = ("public.ecr.aws/aws-cli/aws-cli:2.38.1"
                     "@sha256:733d5be1015825e4038c6016f3fa9954868ddee1f611b73be795a9cc2e706bfd")
CPU_NODE = {"karpenter.sh/nodepool": "cpu-demo"}
RETAKE_OF = re.compile(r"^(S[0-7])/rep([1-9][0-9]*)$")

HEX64 = re.compile(r"^[0-9a-f]{64}$")
ISO_Z = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
RUN_ID = re.compile(r"^[a-z0-9][a-z0-9.-]{2,62}$")
# Values from outside the plan that reach a kubectl / aws argv (static analysis subprocess audit), matched with fullmatch: none
# admits a leading '-' (an option), whitespace, '/' or a shell character. Pod, Job and node names are DNS-1123 names.
K8S_NAME = re.compile(r"[a-z0-9]([-a-z0-9]*[a-z0-9])?(\.[a-z0-9]([-a-z0-9]*[a-z0-9])?)*")
S3_BUCKET = re.compile(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]")
AWS_REGION_NAME = re.compile(r"[a-z]+(-[a-z]+)*-[0-9]+")
ARGV_ENV = {"RESULTS_BUCKET": S3_BUCKET, "AWS_REGION": AWS_REGION_NAME, "CB_NODE": K8S_NAME, "JOB_NAME": K8S_NAME}
ARGV_PATH_ENV = ("WORK", "DGD_DIR", "DRIVER_DIR")  # their files are aws / pip / kubectl arguments: absolute only
METRIC_RE = re.compile(r"^([a-zA-Z_:][a-zA-Z0-9_:]*)(\{[^}]*\})?\s+([-+0-9.eE]+|NaN|\+Inf|-Inf)(\s+\d+)?$")
KEEP = re.compile(r"^(sglang:[a-z_]+|dynamo_component_[a-z_]+|dynamo_frontend_[a-z_]+|dynamo_backend_[a-z_]+)$")
KEEP_BUCKETS = {"dynamo_component_router_kv_hit_rate_bucket"}  # the router predicted-hit histogram

_LOCK = threading.Lock()


class ConfigError(Exception):
    """A plan or configuration problem found before any cluster change (exit 2)."""


class StepError(Exception):
    """A step failed (deploy, reset verification, aiperf...)."""


def compose_ffmpeg_image() -> str:
    """The ffmpeg image of the compose render leg: env COMPOSE_FFMPEG_IMAGE (the regional ECR mirror, by digest).

    Raises:
        ConfigError: COMPOSE_FFMPEG_IMAGE is not set (the repo keeps registry values out of the driver source).
    """
    image = os.environ.get("COMPOSE_FFMPEG_IMAGE", "")
    if not image:
        raise ConfigError("COMPOSE_FFMPEG_IMAGE is not set")
    return image


class FatalError(Exception):
    """The plan cannot continue safely (another q38 DGD, foreign GPU pods, teardown failure): abort, exit 1."""


class HardStop(Exception):
    """The plan's hard stop (or SIGTERM) was reached."""


def utcnow() -> str:
    """Current UTC instant, ISO with Z."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def iso_epoch(ts: str) -> int:
    """Epoch seconds of a YYYY-MM-DDTHH:MM:SSZ instant (calendar-checked)."""
    if not ISO_Z.match(ts):
        raise ConfigError(f"not a UTC instant YYYY-MM-DDTHH:MM:SSZ: {ts!r}")
    try:
        return calendar.timegm(time.strptime(ts, "%Y-%m-%dT%H:%M:%SZ"))
    except ValueError as exc:
        raise ConfigError(f"impossible calendar value: {ts!r}") from exc


def epoch_iso(t: float) -> str:
    """YYYY-MM-DDTHH:MM:SSZ of an epoch."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))


def log(msg: str, **kw: Any) -> None:
    """One structured JSON log line on stdout (never prompt content)."""
    with _LOCK:
        print(json.dumps({"ts": utcnow(), "driver": DRIVER_VERSION, "msg": msg, **kw}, default=str), flush=True)


def pause(seconds: float) -> None:
    """Sleep (the one seam the offline tests replace)."""
    time.sleep(seconds)


# ================================================================ plan
@dataclass
class Step:
    """One plan step."""

    kind: str  # run | take | compose | probe-cold-prefill | calibrate-slo | teardown
    cell: str = ""
    rep: int = 0
    label: str = ""
    opts: dict[str, str] = field(default_factory=dict)
    line: int = 0

    @property
    def key(self) -> str:
        """Results key of a run or take step (<cell>/rep<k>)."""
        return f"{self.cell}/rep{self.rep}"


@dataclass
class Plan:
    """A validated plan."""

    run_id: str
    hard_stop: str
    w2: dict[str, str]
    tokenizer: dict[str, str]
    slo: dict[str, str]
    estimate: dict[str, int]
    steps: list[Step]
    source_sha256: str
    slo_ref: dict[str, str] | None = None  # e4/e5/e6: the e3 calibration + lock (verified at preflight)
    on_failure: str = "continue"
    cut_order: list[str] = field(default_factory=list)

    @property
    def slo_frozen(self) -> bool:
        """Explicit numeric SLO (e3 candidate) or a lock reference: both are frozen in the plan text."""
        return True

    @property
    def calibrate_line(self) -> int | None:
        """Line of the first calibrate-slo step (runs after it need the SLO lock)."""
        return next((s.line for s in self.steps if s.kind == "calibrate-slo"), None)


def _kv_fields(tokens: list[str], line: int, allowed: set[str], required: set[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for tok in tokens:
        if "=" not in tok:
            raise ConfigError(f"plan line {line}: expected key=value, got {tok!r}")
        k, v = tok.split("=", 1)
        if k not in allowed:
            raise ConfigError(f"plan line {line}: unknown field {k!r} (allowed {sorted(allowed)})")
        if k in out:
            raise ConfigError(f"plan line {line}: {k} given twice")
        out[k] = v
    missing = required - set(out)
    if missing:
        raise ConfigError(f"plan line {line}: missing {sorted(missing)}")
    return out


def _positive_float(name: str, value: str, line: int) -> float:
    try:
        x = float(value)
    except ValueError as exc:
        raise ConfigError(f"plan line {line}: {name} must be a number, got {value!r}") from exc
    if not math.isfinite(x) or x <= 0:
        raise ConfigError(f"plan line {line}: {name} must be > 0, got {value!r}")
    return x


def _positive_int(name: str, value: str, line: int) -> int:
    if not value.isdigit() or int(value) < 1:
        raise ConfigError(f"plan line {line}: {name} must be a positive integer, got {value!r}")
    return int(value)


def _take_step(rest: list[str], line: int, seen_runs: set[str], seen_takes: set[str]) -> Step:
    """``take <S0|S7> <rep> [label=] [retake_of=<cell>/rep<k> min_left_s=<n>]`` (v4).

    Raises:
        ConfigError: Unknown or non-take cell, bad rep, unknown option, duplicate key, retake_of without
            min_left_s (or the reverse), or a retake_of that names no earlier take of the same cell.
    """
    if len(rest) < 2:
        raise ConfigError(f"plan line {line}: take <{'|'.join(TAKE_LABELS)}> <rep> [label=] [retake_of= min_left_s=]")
    cell, rep = rest[0], rest[1]
    if cell not in TAKE_LABELS:
        raise ConfigError(f"plan line {line}: take cell must be one of {sorted(TAKE_LABELS)} (the take dashboard "
                          f"labels), got {cell!r}")
    st = Step("take", cell, _positive_int("rep", rep, line), "", {}, line)
    opts = _kv_fields(rest[2:], line, {"label", "retake_of", "min_left_s"}, set())
    st.label, st.opts = opts.get("label", ""), opts
    if st.key in seen_runs:
        raise ConfigError(f"plan line {line}: duplicate run or take {st.key}")
    if ("retake_of" in opts) != ("min_left_s" in opts):
        raise ConfigError(f"plan line {line}: retake_of and min_left_s go together")
    if "retake_of" in opts:
        _positive_int("min_left_s", opts["min_left_s"], line)
        m = RETAKE_OF.match(opts["retake_of"])
        if m is None or opts["retake_of"] not in seen_takes:
            raise ConfigError(f"plan line {line}: retake_of={opts['retake_of']} names no earlier take (<cell>/rep<k>)")
        if m.group(1) != cell:
            raise ConfigError(f"plan line {line}: retake_of={opts['retake_of']} is a take of another cell than {cell}")
    return st


def _compose_step(rest: list[str], line: int, earlier: list[Step]) -> Step:
    """``compose [target_s=<n>]`` (v4): once, after at least one take of every take cell.

    Raises:
        ConfigError: Positional arguments, an unknown option, a non-positive target_s, a second compose, or no
            earlier take of S0 or of S7.
    """
    opts = _kv_fields(rest, line, {"target_s"}, set())
    if "target_s" in opts:
        _positive_int("target_s", opts["target_s"], line)
    if any(s.kind == "compose" for s in earlier):
        raise ConfigError(f"plan line {line}: compose given twice")
    missing = [c for c in TAKE_LABELS if not any(s.kind == "take" and s.cell == c for s in earlier)]
    if missing:
        raise ConfigError(f"plan line {line}: compose needs an earlier take of {missing}")
    return Step("compose", "", 0, "", opts, line)


def _check_take_names(run_id: str, steps: list[Step]) -> None:
    """Job names derived from the run_id must fit Kubernetes' 63-character limit (label values too)."""
    names = [f"demo-capture-{take_id(run_id, s.cell, s.rep)}" for s in steps if s.kind == "take"]
    names += [compose_job_name(run_id)] if any(s.kind == "compose" for s in steps) else []
    long = [n for n in names if len(n) > 63]
    if long:
        raise ConfigError(f"plan: run_id {run_id!r} makes Job names longer than 63 characters: {long}")


def parse_plan(text: str) -> Plan:
    """Parse and validate a plan (grammar in the plans' headers).

    Raises:
        ConfigError: On any grammar or consistency problem.
    """
    hdr: dict[str, Any] = {}
    steps: list[Step] = []
    seen_runs: set[str] = set()  # run and take keys (one results prefix each)
    seen_takes: set[str] = set()
    for n, raw in enumerate(text.splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        tok = line.split()
        head, rest = tok[0], tok[1:]
        if head in ("run_id", "hard_stop", "on_failure", "cut_order", "slo") and head in hdr:
            raise ConfigError(f"plan line {n}: {head} given twice")
        if head == "run_id":
            if len(rest) != 1 or not RUN_ID.match(rest[0]):
                raise ConfigError(f"plan line {n}: run_id must match {RUN_ID.pattern}")
            hdr["run_id"] = rest[0]
        elif head == "hard_stop":
            if len(rest) != 1:
                raise ConfigError(f"plan line {n}: hard_stop takes one UTC instant")
            iso_epoch(rest[0])
            hdr["hard_stop"] = rest[0]
        elif head == "on_failure":
            if rest not in (["abort"], ["continue"]):
                raise ConfigError(f"plan line {n}: on_failure abort|continue")
            hdr["on_failure"] = rest[0]
        elif head == "cut_order":
            if not rest or any(c not in CELLS for c in rest) or len(set(rest)) != len(rest):
                raise ConfigError(f"plan line {n}: cut_order <cell> [<cell>...] (known, distinct cells)")
            hdr["cut_order"] = rest
        elif head == "w2":
            hdr["w2"] = _kv_fields(rest, n, {"trace", "trace_sha256", "sidecar", "sidecar_sha256", "frozen"},
                                   {"trace", "trace_sha256", "sidecar", "sidecar_sha256", "frozen"})
        elif head == "tokenizer":
            hdr["tokenizer"] = _kv_fields(rest, n, {"prefix", "sums_sha256"}, {"prefix", "sums_sha256"})
        elif head == "slo":
            if any(t.startswith("from=") for t in rest):
                ref = _kv_fields(rest, n, {"from", "lock", "e3_plan_sha256"}, {"from", "lock", "e3_plan_sha256"})
                if not HEX64.match(ref["e3_plan_sha256"]):
                    raise ConfigError(f"plan line {n}: e3_plan_sha256 must be a sha256 hex digest")
                for k in ("from", "lock"):
                    if not ref[k].startswith("s3://"):
                        raise ConfigError(f"plan line {n}: slo {k} must be an s3:// URI")
                hdr["slo"] = {"ref": ref}
            else:
                hdr["slo"] = {"values": _kv_fields(rest, n, {"ttft_s", "itl_ms", "cold_factor", "cold_prefill_tok_s"},
                                                   {"ttft_s", "itl_ms", "cold_factor", "cold_prefill_tok_s"})}
        elif head == "estimate":
            est = _kv_fields(rest, n, set(DEFAULT_ESTIMATE), set())
            hdr["estimate"] = {k: int(_positive_float(k, v, n)) for k, v in est.items()}
        elif head == "run":
            if len(rest) < 2:
                raise ConfigError(f"plan line {n}: run <cell> <rep> [label=...]")
            cell, rep = rest[0], rest[1]
            if cell not in CELLS:
                raise ConfigError(f"plan line {n}: unknown cell {cell!r}")
            if not rep.isdigit() or int(rep) < 1:
                raise ConfigError(f"plan line {n}: rep must be a positive integer")
            opts = _kv_fields(rest[2:], n, {"label"}, set())
            st = Step("run", cell, int(rep), opts.get("label", ""), opts, n)
            if st.key in seen_runs:
                raise ConfigError(f"plan line {n}: duplicate run {st.key}")
            seen_runs.add(st.key)
            steps.append(st)
        elif head == "take":
            st = _take_step(rest, n, seen_runs, seen_takes)
            seen_runs.add(st.key)
            seen_takes.add(st.key)
            steps.append(st)
        elif head == "compose":
            steps.append(_compose_step(rest, n, steps))
        elif head == "probe-cold-prefill":
            if not rest or rest[0] not in CELLS:
                raise ConfigError(f"plan line {n}: probe-cold-prefill <cell> [n=3] [tokens=102400]")
            opts = _kv_fields(rest[1:], n, {"n", "tokens"}, set())
            steps.append(Step("probe-cold-prefill", rest[0], 0, "", opts, n))
        elif head == "calibrate-slo":
            opts = _kv_fields(rest, n, {"after", "band"}, {"after"})
            if opts["after"] not in seen_runs:
                raise ConfigError(f"plan line {n}: calibrate-slo after={opts['after']} names no earlier run")
            steps.append(Step("calibrate-slo", "", 0, "", opts, n))
        elif head == "teardown":
            if rest:
                raise ConfigError(f"plan line {n}: teardown takes no arguments")
            steps.append(Step("teardown", line=n))
        else:
            raise ConfigError(f"plan line {n}: unknown directive {head!r}")
    for req in ("run_id", "hard_stop", "w2", "tokenizer", "slo"):
        if req not in hdr:
            raise ConfigError(f"plan: missing header line {req!r}")
    for k in ("trace_sha256", "sidecar_sha256"):
        if not HEX64.match(hdr["w2"][k]):
            raise ConfigError(f"plan: w2 {k} must be a sha256 hex digest, got {hdr['w2'][k]!r}")
    if hdr["w2"]["frozen"] not in ("yes", "no"):
        raise ConfigError("plan: w2 frozen must be yes or no")
    if not HEX64.match(hdr["tokenizer"]["sums_sha256"]):
        raise ConfigError("plan: tokenizer sums_sha256 must be a sha256 hex digest")
    for uri in (hdr["w2"]["trace"], hdr["w2"]["sidecar"], hdr["tokenizer"]["prefix"]):
        if not uri.startswith("s3://"):
            raise ConfigError(f"plan: {uri!r} is not an s3:// URI")
    if not any(s.kind in ("run", "take") for s in steps):
        raise ConfigError("plan: no run or take steps")
    _check_take_names(hdr["run_id"], steps)
    slo: dict[str, str] = {}
    slo_ref: dict[str, str] | None = hdr["slo"].get("ref")
    if slo_ref is not None:
        if any(s.kind in ("calibrate-slo", "probe-cold-prefill") for s in steps):
            raise ConfigError("plan: a plan either calibrates (explicit slo) or uses the e3 SLO lock (slo from=...)")
    else:
        slo = dict(hdr["slo"]["values"])
        for k in ("ttft_s", "itl_ms", "cold_factor"):
            _positive_float(k, slo[k], 0)
        if slo["cold_prefill_tok_s"] == "probe":
            first_probe = next((s for s in steps if s.kind == "probe-cold-prefill"), None)
            first_judged = next((s for s in steps if s.kind in ("run", "take")), None)
            if first_probe is None or (first_judged is not None and first_judged.line < first_probe.line):
                raise ConfigError("plan: cold_prefill_tok_s=probe needs a probe-cold-prefill step before the first run")
        else:
            _positive_float("cold_prefill_tok_s", slo["cold_prefill_tok_s"], 0)
    cut = hdr.get("cut_order", [])
    run_cells = {s.cell for s in steps if s.kind == "run"}
    if any(c not in run_cells for c in cut):
        raise ConfigError(f"plan: cut_order names cells without runs: {sorted(set(cut) - run_cells)}")
    return Plan(run_id=hdr["run_id"], hard_stop=hdr["hard_stop"], w2=hdr["w2"], tokenizer=hdr["tokenizer"],
                slo=slo, estimate={**DEFAULT_ESTIMATE, **hdr.get("estimate", {})}, steps=steps,
                source_sha256=hashlib.sha256(text.encode()).hexdigest(), slo_ref=slo_ref,
                on_failure=hdr.get("on_failure", "continue"), cut_order=list(cut))


# ================================================================ pure helpers (unit-tested)
def parse_metrics(text: str) -> dict[str, float]:
    """KEEP series of one Prometheus text exposition (full label set as key); histogram buckets dropped except
    the router predicted-hit histogram."""
    out: dict[str, float] = {}
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        m = METRIC_RE.match(line)
        if not m or not KEEP.match(m.group(1)):
            continue
        if m.group(1).endswith("_bucket") and m.group(1) not in KEEP_BUCKETS:
            continue
        try:
            out[m.group(1) + (m.group(2) or "")] = float(m.group(3))
        except ValueError:
            continue
    return out


def label_value(key: str, label: str) -> str | None:
    """Value of ``label`` in a series key."""
    m = re.search(r'[{,]' + re.escape(label) + r'="([^"]*)"', key)
    return m.group(1) if m else None


def series_sum(series: dict[str, float], name: str, **labels: str) -> float:
    """Sum of every sample of ``name`` whose labels match ``labels`` exactly."""
    total = 0.0
    for k, v in series.items():
        if k.split("{", 1)[0] != name:
            continue
        if all(label_value(k, lk) == lv for lk, lv in labels.items()):
            total += v if math.isfinite(v) else 0.0
    return total


def by_label_delta(pre: dict[str, float], post: dict[str, float], name: str, label: str) -> dict[str, float]:
    """Per value of ``label``: delta of ``name`` (non-zero only)."""
    out: dict[str, float] = {}
    for k in set(pre) | set(post):
        if k.split("{", 1)[0] != name:
            continue
        lv = label_value(k, label) or "none"
        d = post.get(k, 0.0) - pre.get(k, 0.0)
        if d:
            out[lv] = out.get(lv, 0.0) + d
    return out


COUNTERS = (
    "sglang:num_requests_total", "sglang:prompt_tokens_total", "sglang:generation_tokens_total",
    "sglang:evicted_tokens_total", "sglang:load_back_bytes_total", "sglang:hicache_backup_bytes_total",
    "sglang:backuped_tokens_total", "sglang:prefetched_tokens_total", "sglang:storage_prefetch_hit_tokens_total",
    "sglang:num_transfer_failed_reqs_total", "sglang:num_bootstrap_failed_reqs_total",
    "sglang:num_prefill_retries_total", "sglang:time_to_first_token_seconds_sum",
    "sglang:time_to_first_token_seconds_count", "sglang:inter_token_latency_seconds_sum",
    "sglang:inter_token_latency_seconds_count", "sglang:kv_transfer_total_mb_sum", "sglang:kv_transfer_total_mb_count",
    "sglang:kv_transfer_latency_ms_sum", "sglang:kv_transfer_latency_ms_count",
    "sglang:kv_transfer_speed_gb_s_sum", "sglang:kv_transfer_speed_gb_s_count",
)
BY_LABEL = (
    ("sglang:cached_tokens_total", "cache_source"), ("sglang:load_back_tokens_total", "pool"),
    ("sglang:hicache_backup_tokens_total", "pool"), ("sglang:hicache_dropped_tokens_total", "reason"),
    ("sglang:storage_prefetch_unfulfilled_tokens_total", "reason"),
)
FRONTEND_COUNTERS = (
    "dynamo_component_router_kv_hit_rate_sum", "dynamo_component_router_kv_hit_rate_count",
    "dynamo_component_router_time_to_first_token_seconds_sum", "dynamo_component_router_time_to_first_token_seconds_count",
    "dynamo_component_router_inter_token_latency_seconds_sum", "dynamo_component_router_inter_token_latency_seconds_count",
    "dynamo_component_router_requests_total", "dynamo_component_router_non_max_overlap_selections_total",
    "dynamo_frontend_router_admission_rejected_total",
)
KV_TRANSFER = ("sglang:kv_transfer_total_mb_sum", "sglang:kv_transfer_total_mb_count", "sglang:kv_transfer_latency_ms_sum",
               "sglang:kv_transfer_latency_ms_count", "sglang:kv_transfer_speed_gb_s_sum",
               "sglang:kv_transfer_speed_gb_s_count", "sglang:num_transfer_failed_reqs_total")
STORAGE_COUNTERS = ("sglang:storage_prefetch_hit_tokens_total", "sglang:prefetched_tokens_total",
                    "sglang:backuped_tokens_total", "sglang:hicache_backup_bytes_total", "sglang:load_back_bytes_total")


def engine_kpis(pre: dict[str, dict[str, float]], post: dict[str, dict[str, float]],
                roles: dict[str, str]) -> dict[str, Any]:
    """Per-engine and total counter deltas between two snapshots, plus the engine hit rate.

    ``roles`` maps a target name ("w:<pod>") to worker|prefill|decode. The hit rate counts only the engines that
    own a prefix cache (worker, prefill): the PD decode has no radix cache and would double count prompts.
    """
    per: dict[str, Any] = {}
    tot: dict[str, Any] = {"by_label": {}}
    for tgt, role in sorted(roles.items()):
        a, b = pre.get(tgt, {}), post.get(tgt, {})
        d: dict[str, Any] = {"role": role}
        for name in COUNTERS:
            dv = series_sum(b, name) - series_sum(a, name)
            if dv:
                d[name] = dv
                tot[name] = tot.get(name, 0.0) + dv
        for name, label in BY_LABEL:
            dl = by_label_delta(a, b, name, label)
            if dl:
                d[name] = dl
                agg = tot["by_label"].setdefault(name, {})
                for lv, x in dl.items():
                    agg[lv] = agg.get(lv, 0.0) + x
        per[tgt] = d
    cache_owners = [t for t, r in roles.items() if r in ("worker", "prefill")]
    by_source: dict[str, float] = {}
    for t in cache_owners:
        for src, x in by_label_delta(pre.get(t, {}), post.get(t, {}), "sglang:cached_tokens_total",
                                     "cache_source").items():
            by_source[src] = by_source.get(src, 0.0) + x
    cached = sum(by_source.values())
    prompt = sum(series_sum(post.get(t, {}), "sglang:prompt_tokens_total")
                 - series_sum(pre.get(t, {}), "sglang:prompt_tokens_total") for t in cache_owners)
    tot["engine_hit_rate"] = cached / prompt if prompt > 0 else None
    tot["engine_cached_tokens"] = cached
    tot["engine_prompt_tokens"] = prompt
    tot["engine_cached_tokens_by_source"] = by_source
    tot["host_plus_storage_share_of_cached"] = ((by_source.get("host", 0.0) + by_source.get("storage", 0.0)) / cached
                                                if cached > 0 else None)
    return {"per_engine": per, "total": tot}


def frontend_kpis(pre: dict[str, float], post: dict[str, float]) -> dict[str, Any]:
    """Frontend/router counter deltas, the router-predicted hit (request-weighted histogram mean) and the
    predicted-hit histogram (bucket deltas, non-zero only)."""
    d: dict[str, Any] = {n: series_sum(post, n) - series_sum(pre, n) for n in FRONTEND_COUNTERS}

    def mean(prefix: str) -> float | None:
        c, s_ = d[prefix + "_count"], d[prefix + "_sum"]
        return float(s_ / c) if c > 0 else None

    d["router_predicted_hit_mean"] = mean("dynamo_component_router_kv_hit_rate")
    d["router_ttft_mean_s"] = mean("dynamo_component_router_time_to_first_token_seconds")
    d["router_itl_mean_s"] = mean("dynamo_component_router_inter_token_latency_seconds")
    d["router_predicted_hit_hist_delta"] = by_label_delta(pre, post, "dynamo_component_router_kv_hit_rate_bucket", "le")
    return d


def parse_net_dev(text: str, iface: str = "eth0") -> dict[str, int] | None:
    """rx/tx bytes and packets of ``iface`` from /proc/net/dev."""
    for line in text.splitlines():
        if line.strip().startswith(iface + ":"):
            f = line.split(":", 1)[1].split()
            return {"rx_bytes": int(f[0]), "rx_packets": int(f[1]), "tx_bytes": int(f[8]), "tx_packets": int(f[9])}
    return None


def _disk_row(f: list[str]) -> dict[str, int]:
    return {"reads": int(f[3]), "read_bytes": int(f[5]) * 512, "writes": int(f[7]), "write_bytes": int(f[9]) * 512}


def parse_diskstats(text: str, device: str = "md127") -> dict[str, int] | None:
    """Sectors/bytes read and written of ``device`` from /proc/diskstats (512-byte sectors)."""
    for line in text.splitlines():
        f = line.split()
        if len(f) >= 10 and f[2] == device:
            return _disk_row(f)
    return None


def parse_diskstats_all(text: str) -> dict[str, dict[str, int]]:
    """/proc/diskstats rows of the md RAID devices and the whole NVMe namespaces (no partitions, no loops)."""
    out: dict[str, dict[str, int]] = {}
    for line in text.splitlines():
        f = line.split()
        if len(f) >= 10 and re.fullmatch(r"md\d+|nvme\d+n\d+", f[2]):
            out[f[2]] = _disk_row(f)
    return out


def parse_meminfo(text: str) -> dict[str, int]:
    """/proc/meminfo lines in kB (host-wide inside the container)."""
    out: dict[str, int] = {}
    for line in text.splitlines():
        m = re.match(r"^(\w+):\s+(\d+) kB$", line.strip())
        if m:
            out[m.group(1)] = int(m.group(2))
    return out


def parse_counter_lines(text: str) -> dict[str, int]:
    """'<path> <int>' lines (EFA sysfs hw_counters dump) -> dict; other lines ignored."""
    out: dict[str, int] = {}
    for line in text.splitlines():
        parts = line.rsplit(" ", 1)
        if len(parts) == 2 and parts[1].strip().isdigit():
            out[parts[0]] = int(parts[1])
    return out


def dict_delta(pre: dict[str, int] | None, post: dict[str, int] | None) -> dict[str, int] | None:
    """Key-wise post - pre (keys of post)."""
    if pre is None or post is None:
        return None
    return {k: post[k] - pre.get(k, 0) for k in post}


def l3_page_cache_proxy(disk_read_bytes: int | None, storage_tokens: float, backup_bytes: float,
                        backup_tokens_kv: float) -> dict[str, Any]:
    """(I) proxy of the L3 restore source (T-HIC-5 rule): NVMe share = md127 read bytes / expected L3 bytes.

    Expected bytes = storage-hit tokens x (HiCache backup bytes / backup KV tokens) of the same run, i.e. the host
    page footprint per token (KV + amortised mamba). nvme_share >= 0.8 -> "nvme", else "page-cache assisted".
    Claims use the wording of STORAGE_LABEL; this proxy only qualifies them (I).
    """
    if disk_read_bytes is None or storage_tokens <= 0 or backup_tokens_kv <= 0 or backup_bytes <= 0:
        return {"status": "n/a", "storage_tokens": storage_tokens, "disk_read_bytes": disk_read_bytes}
    bpt = backup_bytes / backup_tokens_kv
    expected = storage_tokens * bpt
    share = disk_read_bytes / expected
    return {"status": "computed (I)", "storage_tokens": storage_tokens, "bytes_per_token_I": bpt,
            "expected_l3_read_bytes_I": expected, "disk_read_bytes_md127": disk_read_bytes,
            "nvme_share_I": share, "page_cache_share_proxy_I": max(0.0, min(1.0, 1.0 - share)),
            "label": "nvme" if share >= 0.8 else "page-cache assisted"}


def validity(*, aiperf_rc: int, trace_rows: int, profiling_rows: int, errored_rows: int, collapsed: int,
             restarted: list[str], reset_ok: bool, cut_by_hard_stop: bool, exceeds: Callable[[int, int], bool],
             extra_reasons: Iterable[str] = ()) -> dict[str, Any]:
    """Run validity: aiperf ok, errors (aiperf errors + collapsed outputs + rows never exported) <= 0.5 % of the
    trace rows, no pod restarted or replaced, reset verified, not cut by the hard stop, plus ``extra_reasons``
    (GPU uniqueness, declared PD transport). ``exceeds`` is demo_bench.application.validity.exceeds_error_limit
    (the single definition of the 0.5 % rule)."""
    missing = max(0, trace_rows - profiling_rows)
    errors = errored_rows + collapsed + missing
    reasons = []
    if aiperf_rc != 0:
        reasons.append(f"aiperf exited {aiperf_rc}")
    if exceeds(errors, trace_rows):
        reasons.append(f"errors {errors}/{trace_rows} > 0.5 % (aiperf {errored_rows}, collapsed {collapsed}, "
                       f"missing {missing})")
    if restarted:
        reasons.append(f"pods restarted or replaced: {','.join(restarted)}")
    if not reset_ok:
        reasons.append("reset not verified (T-RTR-2)")
    if cut_by_hard_stop:
        reasons.append("cut by the hard stop")
    reasons += list(extra_reasons)
    return {"valid": not reasons, "reasons": reasons, "errors_total": errors, "errored_rows": errored_rows,
            "collapsed": collapsed, "missing_rows": missing, "trace_rows": trace_rows,
            "profiling_rows": profiling_rows, "error_rate": errors / trace_rows if trace_rows else None}


def restarted_pods(before: list[dict[str, Any]], after: list[dict[str, Any]]) -> list[str]:
    """Pods (by name) whose UID changed, that vanished, or whose restart count grew."""
    a = {p["name"]: p for p in before}
    b = {p["name"]: p for p in after}
    bad = []
    for name, p in a.items():
        q = b.get(name)
        if q is None or q["uid"] != p["uid"] or q["restarts"] != p["restarts"]:
            bad.append(name)
    bad += [n for n in b if n not in a]
    return sorted(bad)


def canary_tokens(seed: str, n_tokens: int) -> list[int]:
    """Deterministic synthetic token IDs (never logged)."""
    rng = random.Random(seed)
    return [rng.randint(TOKEN_ID_LOW, TOKEN_ID_HIGH) for _ in range(n_tokens)]


def owner_id(rec: dict[str, Any]) -> Any:
    """The engine that holds a request's prompt KV (agg worker or PD prefill), from the nvext worker_id."""
    wid = rec.get("worker_id")
    return wid.get("prefill_worker_id") if isinstance(wid, dict) else wid


def positive_control(send: Callable[[list[int]], dict[str, Any]], canaries: list[list[int]],
                     first: list[dict[str, Any]], pass_hit: Callable[[list[dict[str, Any]]], bool],
                     max_sends: int = CONTROL_MAX_SENDS) -> dict[str, Any]:
    """T-RTR-2 positive control, valid under rr and kv without touching the Frontend mode.

    The canaries are re-sent in FULL passes (n requests each, so the round-robin cursor stays aligned: canary i lands
    on the same engine in every pass; kv follows the overlap) until ``pass_hit`` says a pass hit, at most ``max_sends``
    sends of each canary including the first pass. The owners of the hitting pass are what the post-reset repeat must
    land on under rr for the zero check to be meaningful.

    Args:
        send: One streamed completion of a canary (a ``stream_completion`` record).
        canaries: The n canary prompts (n = KV-owning engines).
        first: The records of the first pass, already sent.
        pass_hit: Called with each control pass's records; True when that pass hit (engine counters).
        max_sends: Most sends of each canary, the first pass included.

    Returns:
        hit, passes, requests and ok_requests of the control passes, per canary the (owner, usage cached tokens) of
        every send, and hit_owner (the owner of each canary in the hitting pass, None when no pass hit).
    """
    sends: list[list[tuple[Any, int]]] = [[(owner_id(r), int(r.get("cached_tokens") or 0))] for r in first]
    out: dict[str, Any] = {"hit": False, "passes": 1, "requests": 0, "ok_requests": 0,
                           "hit_owner": [None] * len(canaries)}
    for _ in range(max(0, max_sends - 1)):
        recs = [send(c) for c in canaries]
        out["passes"] += 1
        out["requests"] += len(recs)
        out["ok_requests"] += sum(1 for r in recs if r.get("ok"))
        for i, r in enumerate(recs):
            sends[i].append((owner_id(r), int(r.get("cached_tokens") or 0)))
        if pass_hit(recs):
            out["hit"] = True
            out["hit_owner"] = [owner_id(r) for r in recs]
            break
    out["sends"] = sends
    return out


def run_estimate_s(est: dict[str, int], trace_span_s: float, needs_boot: bool) -> int:
    """(I) wall time of one run: [boot] + reset/verify + trace span + drain."""
    return int((est["boot_s"] if needs_boot else 0) + est["reset_s"] + trace_span_s + est["drain_s"])


def plan_cuts(runs: list[Step], current_topology: str | None, est: dict[str, int], span_s: float,
              time_left_s: float, *, cut_order: list[str]) -> list[str]:
    """Keys of the remaining runs to drop so that their (I) estimate fits ``time_left_s``.

    Cut rule: reps 2-3 of the ``cut_order`` cells leave, the first listed cell first and its highest rep
    first; rep 1 and unlisted cells are never dropped here (if it still does not fit, the hard-stop guard cuts
    the tail). A boot is counted at every topology change of the remaining sequence.
    """
    def need(seq: list[Step]) -> int:
        total, cur = 0, current_topology
        for s in seq:
            topo = CELLS[s.cell][0]
            total += run_estimate_s(est, span_s, topo != cur)
            cur = topo
        return total

    keep = [s for s in runs if s.kind == "run"]
    candidates = sorted((s for s in keep if s.cell in cut_order and s.rep >= 2),
                        key=lambda s: (cut_order.index(s.cell), -s.rep))
    dropped: list[str] = []
    for c in [None, *candidates]:
        if c is not None:
            dropped.append(c.key)
            keep = [s for s in keep if s.key != c.key]
        if need(keep) <= time_left_s:
            return dropped
    return dropped


def percentile(values: list[float], q: float) -> float:
    """Linear-interpolation percentile (same definition as demo_bench.application.matrix._percentile)."""
    s = sorted(values)
    pos = q * (len(s) - 1)
    lo = math.floor(pos)
    hi = min(lo + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (pos - lo)


def calibrate_rule(samples: list[Any], thresholds: dict[str, float], band: tuple[float, float],
                   attainment_of: Callable[[dict[str, float]], float | None]) -> dict[str, Any]:
    """SLO calibration on the S0 pilot: keep the candidate SLO if its attainment is in the band; otherwise move
    the continuation TTFT to the pilot's continuation TTFT p60 and, if ITL is the binding term, the ITL to the
    p60 of the judged ITLs (one adjustment, then frozen whatever the new attainment). Deterministic: percentiles
    sort their inputs; ``rule_applied`` states what was done."""
    a0 = attainment_of(thresholds)
    doc: dict[str, Any] = {"rule": "slo-calibration", "band": list(band), "candidate": dict(thresholds), "attainment_candidate": a0,
                           "frozen_after_one_adjustment": True}
    if a0 is not None and band[0] <= a0 <= band[1]:
        return {**doc, "adjusted": False, "slo": dict(thresholds), "attainment": a0, "in_band": True,
                "rule_applied": f"in band ({a0:.4f} in {band[0]}-{band[1]}): candidate SLO kept"}
    cont_ttft = [s.ttft_s for s in samples if s.klass == "continuation" and not s.error and s.ttft_s is not None]
    judged_itl = [s.itl_ms for s in samples if s.klass in ("continuation", "cold_ingestion") and not s.error
                  and s.itl_ms is not None]
    if not cont_ttft or not judged_itl:
        return {**doc, "adjusted": False, "slo": dict(thresholds), "attainment": a0, "in_band": False,
                "note": "no judged samples to calibrate on", "rule_applied": "no judged samples: candidate SLO kept"}
    new = dict(thresholds)
    new["ttft_s"] = percentile(cont_ttft, 0.6)
    itl_only = attainment_of({**thresholds, "ttft_s": 1e9, "cold_factor": 1e9})
    ttft_only = attainment_of({**thresholds, "itl_ms": 1e9})
    itl_binding = itl_only is not None and ttft_only is not None and itl_only < ttft_only
    rule = (f"out of band ({a0}): continuation TTFT -> pilot continuation TTFT p60 = {new['ttft_s']:.4f} s "
            f"(n={len(cont_ttft)})")
    if itl_binding:
        new["itl_ms"] = percentile(judged_itl, 0.6)
        rule += f"; ITL binding (ITL-only {itl_only} < TTFT-only {ttft_only}): ITL -> judged ITL p60 = {new['itl_ms']:.4f} ms"
    else:
        rule += "; ITL not binding: ITL kept"
    a1 = attainment_of(new)
    return {**doc, "adjusted": True, "itl_binding": itl_binding, "attainment_itl_only": itl_only,
            "attainment_ttft_only": ttft_only, "slo": new, "attainment": a1,
            "in_band": a1 is not None and band[0] <= a1 <= band[1], "rule_applied": rule + "; frozen (one adjustment)"}


def s3_key(uri: str) -> str:
    """Key part of an s3://bucket/key URI."""
    return uri.split("://", 1)[1].split("/", 1)[1]


def verify_slo_lock(ref: dict[str, str], lock: dict[str, Any], calibration_bytes: bytes) -> dict[str, Any]:
    """The e4/e5/e6 SLO of record: the e3 calibration file, only if it matches the lock e3 wrote before its S7 run.

    Raises:
        ConfigError: When the lock comes from another e3 plan, names another file, the file's sha256 differs from
            the one e3 recorded, or the file's SLO differs from the lock's.
    """
    if lock.get("plan_sha256") != ref["e3_plan_sha256"]:
        raise ConfigError(f"SLO lock plan_sha256 {lock.get('plan_sha256')} != pinned e3 plan {ref['e3_plan_sha256']}")
    if lock.get("slo_calibration_key") != s3_key(ref["from"]):
        raise ConfigError(f"SLO lock names {lock.get('slo_calibration_key')}, the plan {s3_key(ref['from'])}")
    got = hashlib.sha256(calibration_bytes).hexdigest()
    if got != lock.get("slo_calibration_sha256"):
        raise ConfigError(f"SLO file sha256 {got} != {lock.get('slo_calibration_sha256')} recorded by e3")
    slo = json.loads(calibration_bytes).get("slo")
    if not isinstance(slo, dict) or slo != lock.get("slo"):
        raise ConfigError("SLO file slo differs from the e3 lock")
    return dict(slo)


def job_deadline_check(job: dict[str, Any], hard_stop_epoch: int) -> dict[str, Any]:
    """The Job's activeDeadlineSeconds must end within JOB_DEADLINE_TOLERANCE_S of the plan hard stop."""
    start = (job.get("status") or {}).get("startTime")
    ads = (job.get("spec") or {}).get("activeDeadlineSeconds")
    if not start or not isinstance(ads, int):
        raise ConfigError(f"driver Job has no startTime/activeDeadlineSeconds ({start}, {ads})")
    deadline = iso_epoch(start) + ads
    delta = deadline - hard_stop_epoch
    if abs(delta) > JOB_DEADLINE_TOLERANCE_S:
        raise ConfigError(f"Job deadline {epoch_iso(deadline)} is {delta:+d}s from the hard stop {epoch_iso(hard_stop_epoch)}")
    return {"start": start, "active_deadline_s": ads, "deadline": epoch_iso(deadline), "delta_vs_hard_stop_s": delta}


def watchdog_windows(env: dict[str, str | None]) -> list[tuple[str, str | None]]:
    """The watchdog CronJob's action windows from its env: WINDOWS (``<start>/<end>[,...]``, v4) when set, else the
    legacy single (HARD_STOP, WINDOW_END). Same syntax as watchdog.parse_windows; the driver never imports it.

    Raises:
        ConfigError: A malformed WINDOWS value.
    """
    text = (env.get("WINDOWS") or "").strip()
    if not text:
        hs = env.get("HARD_STOP")
        return [(hs, env.get("WINDOW_END"))] if hs else []
    out: list[tuple[str, str | None]] = []
    for item in text.split(","):
        parts = item.strip().split("/")
        if len(parts) != 2 or iso_epoch(parts[1]) <= iso_epoch(parts[0]):
            raise ConfigError(f"watchdog WINDOWS malformed: {text!r}")
        out.append((parts[0], parts[1]))
    return out


def effective_gpu(env_text: str, smi_text: str, privileged: bool) -> dict[str, Any]:
    """GPU(s) one engine uses: the pinned CUDA_VISIBLE_DEVICES (privileged PD pods see every host GPU) or, in an
    unprivileged pod, the only GPU nvidia-smi lists. Host NVML index only where the pod sees the host's list."""
    env: dict[str, str] = {}
    for ln in env_text.splitlines():
        k, sep, v = ln.strip().partition("=")
        if sep:
            env[k] = v
    smi: list[dict[str, Any]] = []
    for ln in smi_text.splitlines():
        parts = [x.strip() for x in ln.split(",")]
        if len(parts) >= 3 and parts[0].isdigit() and parts[1].startswith("GPU-"):
            smi.append({"index": int(parts[0]), "uuid": parts[1], "bus_id": parts[2]})
    by_uuid = {r["uuid"]: r for r in smi}
    by_index = {r["index"]: r for r in smi}
    cvd = env.get("CUDA_VISIBLE_DEVICES")
    uuids: list[str] = []
    if cvd:
        for t in (x.strip() for x in cvd.split(",") if x.strip()):
            if t.startswith("GPU-"):
                uuids.append(t)
            elif t.isdigit() and int(t) in by_index:
                uuids.append(by_index[int(t)]["uuid"])
    elif not privileged:
        uuids = [r["uuid"] for r in smi]
    return {"cuda_visible_devices": cvd, "nvidia_visible_devices": env.get("NVIDIA_VISIBLE_DEVICES"), "smi": smi,
            "uuids": uuids, "bus_ids": [by_uuid[u]["bus_id"] for u in uuids if u in by_uuid],
            "host_index": [by_uuid[u]["index"] for u in uuids if u in by_uuid] if privileged else None}


def gpu_uniqueness(per_pod: dict[str, dict[str, Any]], expected: int) -> dict[str, Any]:
    """Every engine on exactly one GPU and no GPU shared (so PD pairs sit on distinct GPUs)."""
    owners: dict[str, list[str]] = {}
    missing = sorted(p for p, g in per_pod.items() if len(g.get("uuids") or []) != 1)
    for p, g in sorted(per_pod.items()):
        for u in g.get("uuids") or []:
            owners.setdefault(u, []).append(p)
    dup = {u: ps for u, ps in owners.items() if len(ps) > 1}
    ok = len(per_pod) == expected and not missing and not dup
    return {"ok": ok, "engines": len(per_pod), "expected": expected, "distinct_gpus": len(owners),
            "duplicates": dup, "missing": missing}


BOOT_PATTERNS = {
    "worker_id": re.compile(r"for worker (\d+)"),
    "libfabric": re.compile(r"Backend LIBFABRIC was instantiated|KVManager initialized with backend: LIBFABRIC"),
    "gpu_pin": re.compile(r"GPU pin: .*CUDA_VISIBLE_DEVICES=(\S+)"),
    "l2_kv": re.compile(r"Allocating kv hierarchical KV host pool: (\d+) tokens, ([\d.]+) GB host memory"),
    "l2_mamba": re.compile(r"Allocating ([\d.]+) GB host memory for hierarchical Mamba cache"),
}


def parse_boot_facts(text: str) -> dict[str, Any]:
    """Boot-log facts of one engine (no request content): Dynamo worker instance id, NIXL LIBFABRIC backend
    instantiated (not merely listed among plugins), GPU pin, HiCache host pool sizes (L2 capacity of record)."""
    ids = sorted({int(m) for m in BOOT_PATTERNS["worker_id"].findall(text)})
    lf = [ln.strip()[:240] for ln in text.splitlines() if BOOT_PATTERNS["libfabric"].search(ln)][:5]
    pin = BOOT_PATTERNS["gpu_pin"].search(text)
    kv = BOOT_PATTERNS["l2_kv"].search(text)
    mamba = BOOT_PATTERNS["l2_mamba"].search(text)
    return {"worker_ids": ids, "libfabric_backend": bool(lf), "libfabric_lines": lf,
            "gpu_pin": pin.group(1) if pin else None, "l2_kv_tokens": int(kv.group(1)) if kv else None,
            "l2_kv_gb": float(kv.group(2)) if kv else None, "l2_mamba_gb": float(mamba.group(1)) if mamba else None}


def extract_worker_ids(lines: Iterable[str]) -> list[dict[str, Any]]:
    """Per request (aiperf raw export): metadata ids plus nvext prefill/decode worker ids and the router's
    kv_hit_rate when present. Request payloads (prompts) and response text are never kept."""
    out: list[dict[str, Any]] = []
    for line in lines:
        if not line.strip():
            continue
        rec = json.loads(line)
        meta = rec.get("metadata") or {}
        row: dict[str, Any] = {k: meta.get(k) for k in ("x_request_id", "conversation_id", "turn_index",
                                                         "benchmark_phase", "request_start_ns")}
        row.update(prefill_worker_id=None, decode_worker_id=None, kv_hit_rate=None)
        for resp in rec.get("responses") or []:
            values = [pk.get("value") for pk in resp.get("packets") or []] + [resp.get("text")]
            for v in values:
                if not isinstance(v, str) or '"nvext"' not in v:
                    continue
                try:
                    nv = (json.loads(v).get("nvext") or {})
                except (json.JSONDecodeError, AttributeError):
                    continue
                wid = nv.get("worker_id")
                if isinstance(wid, dict) and row["prefill_worker_id"] is None:
                    row["prefill_worker_id"] = wid.get("prefill_worker_id")
                    row["decode_worker_id"] = wid.get("decode_worker_id")
                hit = (nv.get("timing") or {}).get("kv_hit_rate")
                if hit is not None:
                    row["kv_hit_rate"] = hit
        out.append(row)
    return out


def worker_id_summary(recs: list[dict[str, Any]], id_to_pod: dict[int, str]) -> dict[str, Any]:
    """Per-pod request counts from the nvext worker ids, and the P->D pairs (requests, PD only)."""
    def name(i: Any) -> str:
        return id_to_pod.get(i, f"id:{i}") if isinstance(i, int) else f"id:{i}"

    pre: dict[str, int] = {}
    dec: dict[str, int] = {}
    pairs: dict[str, int] = {}
    n = 0
    for r in recs:
        if r.get("prefill_worker_id") is None:
            continue
        n += 1
        p, d = name(r["prefill_worker_id"]), name(r["decode_worker_id"])
        pre[p] = pre.get(p, 0) + 1
        dec[d] = dec.get(d, 0) + 1
        if r["prefill_worker_id"] != r["decode_worker_id"]:
            pairs[f"{p}->{d}"] = pairs.get(f"{p}->{d}", 0) + 1
    return {"requests": len(recs), "with_worker_id": n, "prefill_pods": pre, "decode_pods": dec, "pd_pairs": pairs,
            "source": "aiperf --export-level raw, nvext.extra_fields worker_id (Frontend)"}


def hf_cache_layout(src: Path, hub: Path, repo: str, revision: str) -> Path:
    """Expose the tokenizer files as an HF hub cache snapshot (models--<org>--<name>/{refs/main,snapshots/<rev>}).

    aiperf 0.13.0 with HF_HUB_OFFLINE/TRANSFORMERS_OFFLINE resolves --tokenizer through
    snapshot_download(local_files_only=True), which rejects a plain directory path (HFValidationError); a repo id
    over this layout loads offline. Returns the snapshot directory.
    """
    root = hub / ("models--" + repo.replace("/", "--"))
    snap = root / "snapshots" / revision
    snap.mkdir(parents=True, exist_ok=True)
    (root / "refs").mkdir(exist_ok=True)
    (root / "refs" / "main").write_text(revision)
    for f in sorted(src.iterdir()):
        if f.is_file() and f.name != "SHA256SUMS":
            shutil.copyfile(f, snap / f.name)
    return snap


def aiperf_cmd(frontend: str, tokenizer: str, trace: Path, art: Path, seed: int) -> list[str]:
    """aiperf 0.13.0 fixed-schedule replay of the W2 window trace, asking the Frontend for nvext worker ids.
    ``tokenizer`` is the repo id resolved offline from the HF cache layout (hf_cache_layout)."""
    nvext = json.dumps({"nvext": {"extra_fields": NVEXT_FIELDS}}, separators=(",", ":"))
    return ["aiperf", "profile", "--model", MODEL, "--url", frontend, "--tokenizer", tokenizer,
            "--tokenizer-revision", MODEL_REVISION, "--random-seed", str(seed), "--use-server-token-count", "--num-dataset-entries", "4096",
            "--wait-for-model-timeout", "900", "--wait-for-model-mode", "models", "--ui-type", "none",
            "--artifact-dir", str(art), "--input-file", str(trace), "--fixed-schedule", "--endpoint-type", "chat",
            "--streaming", "--custom-dataset-type", "mooncake_trace", "--fixed-schedule-auto-offset",
            "--extra-inputs", "ignore_eos:true", nvext, "--export-level", "raw", "--request-timeout-seconds", "7200"]


L3_CLEANUP_SH = """set -eu
d=/l3
echo "files_before=$(find "$d" -type f | wc -l)"
echo "bytes_before=$(du -sb "$d" | cut -f1)"
echo "subdirs_before=$(find "$d" -mindepth 1 -maxdepth 1 -type d | wc -l)"
find "$d" -mindepth 1 -delete
echo "files_after=$(find "$d" -type f | wc -l)"
df -P -B1 "$d" | awk 'NR==2 {print "df_bytes_total="$2" df_bytes_used="$3" df_bytes_avail="$4" df_mount="$6}'
"""


def l3_cleanup_pod(ns: str, name: str, image: str, l3_root: str, node: str) -> dict[str, Any]:
    """A no-GPU pod that empties the namespace's whole L3 directory (every pod subPath, stale ones included)."""
    if ns not in HICACHE_NAMESPACES:
        raise ValueError(f"{ns} has no HiCache L3 directory")
    return {"apiVersion": "v1", "kind": "Pod",
            "metadata": {"name": name, "namespace": ns,
                         "labels": {"app.kubernetes.io/name": "q38-l3-cleanup", "app.kubernetes.io/part-of": PART_OF}},
            "spec": {"restartPolicy": "Never", "activeDeadlineSeconds": 600, "nodeSelector": {"kubernetes.io/hostname": node},
                     "tolerations": [{"key": "nvidia.com/gpu", "operator": "Exists", "effect": "NoSchedule"}],
                     "containers": [{"name": "cleanup", "image": image, "imagePullPolicy": "IfNotPresent",
                                     "command": ["/bin/sh", "-c", L3_CLEANUP_SH],
                                     "resources": {"requests": {"cpu": "100m", "memory": "64Mi"},
                                                   "limits": {"memory": "256Mi"}},
                                     "securityContext": {"runAsUser": 0, "runAsGroup": 0},
                                     "volumeMounts": [{"name": "l3", "mountPath": "/l3"}]}],
                     "volumes": [{"name": "l3", "hostPath": {"path": f"{l3_root}/{ns}", "type": "DirectoryOrCreate"}}]}}


# ================================================================ v4 takes (pure helpers, unit-tested)
def take_id(run_id: str, cell: str, rep: int) -> str:
    """Id of one take: ``<run_id>-<cell lower>-rep<k>`` (capture Job name suffix and S3 media folder)."""
    return f"{run_id}-{cell.lower()}-rep{rep}"


def compose_job_name(run_id: str) -> str:
    """Name of the plan's compose Job."""
    return f"q38-take-compose-{run_id}"


def take_window(replay_start_epoch: float, trace_span_s: float, drain_s: float,
                lead_s: int = CAPTURE_LEAD_S) -> dict[str, int]:
    """Absolute Grafana range and recording length of one take.

    Args:
        replay_start_epoch: When aiperf starts the replay (the capture Job is created ``lead_s`` before).
        trace_span_s: First-to-last timestamp of the frozen trace.
        drain_s: The plan's drain estimate after the last row.
        lead_s: Seconds between the Job creation and the replay start (recorded too).

    Returns:
        from_ms (= the replay start), to_ms (span + drain later), duration_s (recording length: the lead plus the
        rounded-up span + drain, so a recording that starts at the Job creation still reaches to_ms) and lead_s.

    Raises:
        ValueError: A non-positive span or a negative drain.
    """
    if not trace_span_s > 0 or drain_s < 0:
        raise ValueError(f"take window needs span > 0 and drain >= 0, got {trace_span_s}, {drain_s}")
    from_ms = round(replay_start_epoch * 1000)
    return {"from_ms": from_ms, "to_ms": from_ms + round((trace_span_s + drain_s) * 1000),
            "duration_s": math.ceil(trace_span_s + drain_s) + lead_s, "lead_s": lead_s}


def _deadline_left(hard_stop_epoch: int, now: float, what: str) -> int:
    left = int(hard_stop_epoch - now)
    if left <= 0:
        raise StepError(f"{what}: the hard stop {epoch_iso(hard_stop_epoch)} has passed")
    return left


def _pod_security(uid: int) -> dict[str, Any]:
    return {"runAsNonRoot": True, "runAsUser": uid, "runAsGroup": uid, "fsGroup": uid,
            "seccompProfile": {"type": "RuntimeDefault"}}


CTR_SECURITY: dict[str, Any] = {"allowPrivilegeEscalation": False, "capabilities": {"drop": ["ALL"]}}


def capture_job(*, run_id: str, cell: str, rep: int, window: dict[str, int], hard_stop_epoch: int,
                now: float) -> dict[str, Any]:
    """The capture Job of one take, rendered per take (the take features are not supported in this sample).

    Headless Chromium as pwuser records the take view of the cell's namespace over [from_ms, to_ms] (after a
    Prometheus-through-Grafana data check of the namespace) and uploads ``<take_id>.webm``, the PNG frames as
    ``<take_id>-frames-NNNN.tar`` chunks during the recording and ``<take_id>-capture.json`` (video start/end,
    sizes) to s3://$RESULTS_BUCKET/takes/<run_id>/<take_id>/. Grafana basic auth comes only from the Secret
    demo-grafana-auth (key password). Never on the GPU node; the deadline never passes the hard stop.

    Raises:
        StepError: The hard stop has passed.
    """
    tid = take_id(run_id, cell, rep)
    left = _deadline_left(hard_stop_epoch, now, f"capture {tid}")
    region = os.environ["AWS_REGION"]
    labels = {"app.kubernetes.io/name": "demo-capture", "app.kubernetes.io/part-of": PART_OF,
              "demo.kubecon/take-id": tid, "q38-matrix/run-id": run_id, "q38-matrix/cell": cell, "q38-matrix/rep": str(rep)}
    env = [{"name": "HOME", "value": "/home/pwuser"}, {"name": "PIP_BREAK_SYSTEM_PACKAGES", "value": "1"},
           {"name": "TAKE_ID", "value": tid}, {"name": "FROM_MS", "value": str(window["from_ms"])},
           {"name": "TO_MS", "value": str(window["to_ms"])},
           {"name": "NAMESPACE", "value": TOPOLOGIES[CELLS[cell][0]]["namespace"]},
           {"name": "DURATION_S", "value": str(window["duration_s"])},
           {"name": "INTERVAL_S", "value": str(CAPTURE_INTERVAL_S)},
           {"name": "GRAFANA_URL", "value": os.environ.get("GRAFANA_URL", "")},
           {"name": "GRAFANA_USER", "value": "admin"},
           {"name": "GRAFANA_PASSWORD", "valueFrom": {"secretKeyRef": dict(GRAFANA_SECRET)}},
           {"name": "DASHBOARD_UID", "value": DASHBOARD_UID}, {"name": "OUT_DIR", "value": "/out"},
           {"name": "S3_PREFIX", "value": f"takes/{run_id}"},
           {"name": "RESULTS_BUCKET", "value": os.environ["RESULTS_BUCKET"]},
           {"name": "AWS_REGION", "value": region}, {"name": "AWS_DEFAULT_REGION", "value": region}]
    return {
        "apiVersion": "batch/v1", "kind": "Job",
        "metadata": {"name": f"demo-capture-{tid}", "namespace": TAKES_NS, "labels": labels},
        "spec": {
            "backoffLimit": 0, "ttlSecondsAfterFinished": 86400,
            "activeDeadlineSeconds": min(window["duration_s"] + CAPTURE_SLACK_S, left),
            "template": {"metadata": {"labels": dict(labels)}, "spec": {
                "serviceAccountName": "matrix-driver", "restartPolicy": "Never", "nodeSelector": dict(CPU_NODE),
                "terminationGracePeriodSeconds": 10, "securityContext": _pod_security(1001),
                "containers": [_capture_container(env)], "volumes": _capture_volumes(),
            }},
        },
    }


def _capture_container(env: list[dict[str, Any]]) -> dict[str, Any]:
    """The capture container (pinned image + pip pins, /capture script, /dev/shm, /out), shared with the preflight."""
    return {"name": "capture", "image": CAPTURE_IMAGE, "imagePullPolicy": "IfNotPresent",
            "command": ["/bin/bash", "-c"],
            "args": ["set -euo pipefail\n"
                     "python3 -m pip install --user --no-cache-dir --disable-pip-version-check --quiet "
                     f"{CAPTURE_PIP}\nexec python3 /capture/capture.py\n"],
            "env": env, "resources": json.loads(json.dumps(CAPTURE_RESOURCES)), "securityContext": dict(CTR_SECURITY),
            "volumeMounts": [{"name": "capture-script", "mountPath": "/capture", "readOnly": True},
                             {"name": "dshm", "mountPath": "/dev/shm"}, {"name": "out", "mountPath": "/out"}]}


def _capture_volumes() -> list[dict[str, Any]]:
    return [{"name": "capture-script", "configMap": {"name": TAKE_SCRIPTS_CM}},
            {"name": "dshm", "emptyDir": {"medium": "Memory", "sizeLimit": "1Gi"}},
            {"name": "out", "emptyDir": {"sizeLimit": "8Gi"}}]


def pod_state(pods: dict[str, Any]) -> str:
    """State of a take Job's pod (``kubectl get pods -l batch.kubernetes.io/job-name=...``): none | pending |
    running | waiting:<reason> | terminated:<reason> (main container; the last pod listed)."""
    items = pods.get("items") or []
    if not items:
        return "none"
    st = items[-1].get("status") or {}
    for cs in st.get("containerStatuses") or []:
        state = cs.get("state") or {}
        if "running" in state:
            return "running"
        for kind in ("waiting", "terminated"):
            if kind in state:
                return f"{kind}:{(state[kind] or {}).get('reason') or '?'}"
    return "pending"


def pod_blocker(pods: dict[str, Any]) -> str | None:
    """A waiting reason of any (init) container of a take Job's pod that never clears by itself (POD_BLOCKERS: a
    missing Secret or ConfigMap key, an image that cannot be pulled), as ``<container>: <reason>: <message>``."""
    for it in pods.get("items") or []:
        st = it.get("status") or {}
        for cs in (st.get("initContainerStatuses") or []) + (st.get("containerStatuses") or []):
            waiting = (cs.get("state") or {}).get("waiting") or {}
            if waiting.get("reason") in POD_BLOCKERS:
                return f"{cs.get('name')}: {waiting['reason']}: {str(waiting.get('message') or '')[:200]}"
    return None


def first_request_ms(export: Path) -> int | None:
    """Epoch ms of the first profiling request of an aiperf records export (min ``request_start_ns``, the wall clock
    time.time_ns() of aiperf, as 70-demo-take.sh's t0); None when the export or the field is absent."""
    if not export.exists():
        return None
    starts = []
    for line in export.read_text().splitlines():
        if not line.strip():
            continue
        meta = json.loads(line).get("metadata") or {}
        ns = meta.get("request_start_ns")
        if meta.get("benchmark_phase") == "profiling" and isinstance(ns, int) and ns > 0:
            starts.append(ns)
    return min(starts) // 1_000_000 if starts else None


def take_sync(cap: dict[str, Any], first_ms: int | None) -> dict[str, Any]:
    """How the take's replay lines up with its recording (the compose Job syncs on ``sync_ms``).

    sync_ms is the first profiling request when it is plausible (between the aiperf start - 5 s and the replay end),
    else the aiperf start. The sync is degraded (informational: the take stays usable) when the actual start is
    more than CAPTURE_SKEW_TOL_S from the planned from_ms, when the video started after the replay (the compose
    pads the head with the first frame) or when the replay ended after the dashboard range (to_ms).

    Args:
        cap: The run.json ``take.capture`` block (from_ms, to_ms, aiperf_start_ms, replay_end_ms, media).
        first_ms: first_request_ms() of the run's aiperf export.

    Returns:
        planned_start_ms, aiperf_start_ms, first_request_ms, sync_ms, sync_source, skew_s, degraded, reasons.
    """
    start, end = cap.get("aiperf_start_ms"), cap.get("replay_end_ms")
    plausible = (first_ms is not None and isinstance(start, int) and isinstance(end, int)
                 and start - 5000 <= first_ms <= end)
    sync_ms = first_ms if plausible else start
    reasons = []
    skew_s = None if sync_ms is None else round((sync_ms - cap["from_ms"]) / 1000, 3)
    if skew_s is None:
        reasons.append("no aiperf start instant")
    elif abs(skew_s) > CAPTURE_SKEW_TOL_S:
        reasons.append(f"replay started {skew_s:+.1f} s from the planned from_ms (tolerance {CAPTURE_SKEW_TOL_S} s)")
    t0 = (cap.get("media") or {}).get("video_t0_ms")
    if isinstance(t0, int) and sync_ms is not None and t0 > sync_ms:
        reasons.append(f"video started {(t0 - sync_ms) / 1000:.1f} s after the replay (head padded by compose)")
    if isinstance(end, int) and end > cap["to_ms"]:
        reasons.append(f"replay ended {(end - cap['to_ms']) / 1000:.1f} s after the dashboard range to_ms")
    return {"planned_start_ms": cap["from_ms"], "aiperf_start_ms": start, "first_request_ms": first_ms,
            "sync_ms": sync_ms, "sync_source": "first profiling request" if plausible else "aiperf start",
            "skew_s": skew_s, "degraded": bool(reasons), "reasons": reasons}


def job_state(job: dict[str, Any]) -> str:
    """complete | failed | running, from a Job's terminal conditions."""
    for c in (job.get("status") or {}).get("conditions") or []:
        if c.get("status") == "True" and c.get("type") == "Complete":
            return "complete"
        if c.get("status") == "True" and c.get("type") == "Failed":
            return "failed"
    return "running"


def take_record_entry(doc: dict[str, Any]) -> dict[str, Any]:
    """The summary fields of a take already in S3 (its run.json), as select_takes and the retake rule read them."""
    take = doc.get("take") or {}
    cap = take.get("capture") or {}
    return {"valid": (doc.get("validity") or {}).get("valid") is True, "take_id": take.get("take_id"),
            "take_capture": cap.get("status") or "missing", "webm_uri": cap.get("webm_uri"),
            "replay_start_ms": cap.get("replay_start_ms"), "replay_end_ms": cap.get("replay_end_ms"),
            "sync_ms": (cap.get("sync") or {}).get("sync_ms")}


def take_usable(entry: dict[str, Any]) -> bool:
    """A take step that ran (now or in an earlier launch) with a valid run and a good capture."""
    return (entry.get("kind") == "take" and entry.get("status") in ("done", "skipped-done")
            and entry.get("valid") is True and entry.get("take_capture") == "ok" and bool(entry.get("webm_uri")))


def select_takes(entries: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """The latest usable take of each take cell (plan order), from the driver summary entries.

    Raises:
        StepError: A take cell has no valid take with a good capture (the message names the cell).
    """
    out: dict[str, dict[str, Any]] = {}
    for cell in TAKE_LABELS:
        usable = [e for e in entries if e.get("cell") == cell and take_usable(e)]
        if not usable:
            raise StepError(f"compose: no usable take of {cell} (a valid run with a good capture)")
        out[cell] = usable[-1]
    return out


def compose_spec(run_id: str, selected: dict[str, dict[str, Any]], target_s: int = COMPOSE_TARGET_S) -> dict[str, Any]:
    """What the compose Job renders: baseline (S0) left, candidate (S7) right, synced on each ACTUAL replay start
    (``sync_ms``: the first profiling request, see take_sync; the planned replay_start_ms when an older run.json
    has no sync block).

    Outputs (append-only, under takes/<run_id>/compose/): the full-length side-by-side, the ~target_s sped-up deck
    clip and the 3-panel still (the side-by-side at 1/3, 2/3 and the end of the clip).
    """
    base = f"s3://{os.environ['RESULTS_BUCKET']}/takes/{run_id}/compose/"
    inputs = [{"cell": c, "take_id": selected[c]["take_id"], "label": TAKE_LABELS[c], "webm_uri": selected[c]["webm_uri"],
               "replay_start_ms": selected[c]["replay_start_ms"], "replay_end_ms": selected[c].get("replay_end_ms"),
               "sync_ms": selected[c].get("sync_ms") or selected[c]["replay_start_ms"]}
              for c in TAKE_LABELS]
    return {"run_id": run_id, "target_s": target_s, "tail_s": COMPOSE_TAIL_S, "inputs": inputs,
            "outputs": {"full": base + "side-by-side-full.mp4", "deck": base + "side-by-side-deck.mp4",
                        "still": base + "three-panel-still.png"},
            "result_of_record": False, "note": TAKE_NOTE}


def compose_job(spec: dict[str, Any], *, hard_stop_epoch: int, now: float) -> dict[str, Any]:
    """The compose Job: fetch (AWS CLI) -> render (ffmpeg) -> upload (AWS CLI), one compose.sh phase each, on
    cpu-demo; COMPOSE_SPEC carries the spec as JSON. The deadline never passes the hard stop.

    Raises:
        StepError: The hard stop has passed.
    """
    name = compose_job_name(spec["run_id"])
    left = _deadline_left(hard_stop_epoch, now, name)
    region = os.environ["AWS_REGION"]
    ffmpeg_image = compose_ffmpeg_image()
    labels = {"app.kubernetes.io/name": "take-compose", "app.kubernetes.io/part-of": PART_OF,
              "q38-matrix/run-id": spec["run_id"]}
    env = [{"name": "COMPOSE_SPEC", "value": json.dumps(spec, sort_keys=True)}, {"name": "WORK_DIR", "value": "/work"},
           {"name": "HOME", "value": "/work/home"}, {"name": "AWS_REGION", "value": region},
           {"name": "AWS_DEFAULT_REGION", "value": region}]
    mounts = [{"name": "scripts", "mountPath": "/scripts", "readOnly": True}, {"name": "work", "mountPath": "/work"}]

    def ctr(phase: str, image: str, resources: dict[str, Any]) -> dict[str, Any]:
        return {"name": phase, "image": image, "imagePullPolicy": "IfNotPresent",
                "command": ["/bin/bash", "/scripts/compose.sh", phase], "env": env, "resources": resources,
                "securityContext": dict(CTR_SECURITY), "volumeMounts": mounts}

    light = {"requests": {"cpu": "500m", "memory": "1Gi"}, "limits": {"memory": "2Gi"}}
    return {
        "apiVersion": "batch/v1", "kind": "Job",
        "metadata": {"name": name, "namespace": TAKES_NS, "labels": labels},
        "spec": {
            "backoffLimit": 0, "ttlSecondsAfterFinished": 86400, "activeDeadlineSeconds": min(COMPOSE_DEADLINE_S, left),
            "template": {"metadata": {"labels": dict(labels)}, "spec": {
                "serviceAccountName": "matrix-driver", "restartPolicy": "Never", "nodeSelector": dict(CPU_NODE),
                "securityContext": _pod_security(10001),
                "initContainers": [ctr("fetch", COMPOSE_AWS_IMAGE, light),
                                   ctr("render", ffmpeg_image,
                                       {"requests": {"cpu": "6", "memory": "8Gi"}, "limits": {"memory": "12Gi"}})],
                "containers": [ctr("upload", COMPOSE_AWS_IMAGE, light)],
                "volumes": [{"name": "scripts", "configMap": {"name": TAKE_SCRIPTS_CM}},
                            {"name": "work", "emptyDir": {"sizeLimit": "16Gi"}}],
            }},
        },
    }


FFMPEG_CHECK_SH = """set -euo pipefail
v="$(ffmpeg -hide_banner -version)"; echo "${v%%$'\\n'*}"
f="$(ffmpeg -hide_banner -filters)"; for x in drawtext hstack vstack tpad trim; do grep -q " $x " <<<"$f"; done
e="$(ffmpeg -hide_banner -encoders)"; grep -q libx264 <<<"$e"
test -s /usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf
jq --version
echo ffmpeg-ok
"""


def take_preflight_job(*, run_id: str, bucket: str, region: str, grafana_url: str) -> dict[str, Any]:
    """bench/q38-take-preflight-<run_id>: the E7' take prerequisites proven on cpu-demo without the GPU.

    A launch step (rendered/driver/05-take-preflight.yaml, created by the operator; the driver never creates it).
    Init containers pull the compose images by digest (AWS CLI: aws/jq/stat; ffmpeg: version, the filters, libx264,
    the label font, jq). The capture container is the take's (same image, pip pins, Secret ref and resources, so a
    missing Secret demo-grafana-auth stops it in CreateContainerConfigError and the pull warms a cpu-demo node) and
    runs capture.py with CHECK_ONLY=1: Grafana login as admin, the demo-take dashboard and its namespace variable,
    the Prometheus datasource through Grafana, the namespace label values of every take cell, one Chromium render
    of the panel, then the report is uploaded under s3://<bucket>/takes/<run_id>/_preflight/ (append-only).
    """
    labels = {"app.kubernetes.io/name": "take-preflight", "app.kubernetes.io/part-of": PART_OF, "q38-matrix/run-id": run_id}
    env: list[dict[str, Any]] = [{"name": "HOME", "value": "/home/pwuser"}, {"name": "PIP_BREAK_SYSTEM_PACKAGES", "value": "1"},
           {"name": "CHECK_ONLY", "value": "1"}, {"name": "TAKE_ID", "value": "preflight"},
           {"name": "CHECK_NAMESPACES", "value": ",".join(TOPOLOGIES[CELLS[c][0]]["namespace"] for c in TAKE_LABELS)},
           {"name": "GRAFANA_URL", "value": grafana_url}, {"name": "GRAFANA_USER", "value": "admin"},
           {"name": "GRAFANA_PASSWORD", "valueFrom": {"secretKeyRef": dict(GRAFANA_SECRET)}},
           {"name": "DASHBOARD_UID", "value": DASHBOARD_UID}, {"name": "OUT_DIR", "value": "/out"},
           {"name": "S3_PREFIX", "value": f"takes/{run_id}/_preflight"}, {"name": "RESULTS_BUCKET", "value": bucket},
           {"name": "AWS_REGION", "value": region}, {"name": "AWS_DEFAULT_REGION", "value": region}]
    light = {"requests": {"cpu": "100m", "memory": "256Mi"}, "limits": {"memory": "1Gi"}}

    def init(name: str, image: str, script: str) -> dict[str, Any]:
        return {"name": name, "image": image, "imagePullPolicy": "IfNotPresent", "command": ["/bin/bash", "-c", script],
                "env": [{"name": "HOME", "value": "/tmp"}], "resources": light, "securityContext": dict(CTR_SECURITY)}

    return {
        "apiVersion": "batch/v1", "kind": "Job",
        "metadata": {"name": f"q38-take-preflight-{run_id}", "namespace": TAKES_NS, "labels": labels},
        "spec": {
            "backoffLimit": 0, "ttlSecondsAfterFinished": 86400, "activeDeadlineSeconds": 900,
            "template": {"metadata": {"labels": dict(labels)}, "spec": {
                "serviceAccountName": "matrix-driver", "restartPolicy": "Never", "nodeSelector": dict(CPU_NODE),
                "terminationGracePeriodSeconds": 10, "securityContext": _pod_security(1001),
                "initContainers": [
                    init("aws-cli", COMPOSE_AWS_IMAGE, "set -euo pipefail\naws --version\njq --version\nstat --version >/dev/null\n"),
                    init("ffmpeg", compose_ffmpeg_image(), FFMPEG_CHECK_SH)],
                "containers": [_capture_container(env)], "volumes": _capture_volumes(),
            }},
        },
    }


# ================================================================ process / cluster / S3 adapters
def resolve_argv(cmd: list[str]) -> list[str]:
    """``cmd`` with its executable resolved on PATH (``shutil.which``), for the two audited process call sites.

    Every argv the driver runs is a list built from module constants, plan fields checked by parse_plan (run_id,
    cells, s3:// URIs, sha256 digests), Job env values checked by main (ARGV_ENV, absolute ARGV_PATH_ENV) and pod
    names checked by pods_of; free text (the k_exec scripts) only follows ``--`` or ``-c``. Never a shell.

    Raises:
        FileNotFoundError: The executable is not on PATH (nothing is started).
    """
    exe = shutil.which(cmd[0])
    if exe is None:
        raise FileNotFoundError(errno.ENOENT, f"{cmd[0]} is not on PATH", cmd[0])
    return [exe, *cmd[1:]]


def run_argv(cmd: list[str], *, timeout: float, stdin: str | None = None, env: dict[str, str] | None = None,
             cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    """The driver's one ``subprocess.run`` site (sh, the tokenizer probe, demo_bench): text in/out captured, no check."""
    # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-audit, dangerous-subprocess-use-audit -- list argv of constants and checked values (resolve_argv), resolved executable
    return subprocess.run(resolve_argv(cmd), input=stdin, capture_output=True, text=True,  # nosec B603 -- list argv, no shell
                          timeout=timeout, check=False, env=env, cwd=None if cwd is None else str(cwd))


def sh(cmd: list[str], *, timeout: float = 300, check: bool = True, stdin: str | None = None) -> subprocess.CompletedProcess[str]:
    """Run a command; never through a shell."""
    try:
        p = run_argv(cmd, timeout=timeout, stdin=stdin)
    except subprocess.TimeoutExpired as exc:
        raise StepError(f"timeout after {timeout}s: {cmd[:4]}") from exc
    if check and p.returncode != 0:
        raise StepError(f"{' '.join(cmd[:6])} exited {p.returncode}: {p.stderr.strip()[:500]}")
    return p


def kubectl(*args: str, timeout: float = 120, check: bool = True, stdin: str | None = None) -> subprocess.CompletedProcess[str]:
    """kubectl with the in-cluster ServiceAccount (no kubeconfig in the pod)."""
    return sh(["kubectl", *args], timeout=timeout, check=check, stdin=stdin)


def kjson(*args: str) -> Any:
    """kubectl ... -o json."""
    return json.loads(kubectl(*args, "-o", "json").stdout)


def k_exec(ns: str, pod: str, script: str, timeout: float = 60) -> str:
    """Run a read-only shell snippet in a worker's main container; returns stdout ('' on failure)."""
    p = kubectl("exec", "-n", ns, pod, "-c", "main", "--", "sh", "-c", script, timeout=timeout, check=False)
    return p.stdout if p.returncode == 0 else ""


class S3:
    """Append-only access to the results bucket through the aws CLI (Pod Identity credentials)."""

    def __init__(self, bucket: str, region: str) -> None:
        if not S3_BUCKET.fullmatch(bucket) or not AWS_REGION_NAME.fullmatch(region):
            raise ConfigError(f"S3 bucket {bucket!r} / region {region!r}: not a plain bucket and region name")
        self.bucket, self.region = bucket, region

    def uri(self, key: str) -> str:
        """s3:// URI of a key in the results bucket."""
        return f"s3://{self.bucket}/{key}"

    def exists_key(self, key: str) -> bool:
        """True when the exact object exists (HeadObject)."""
        p = sh(["aws", "s3api", "head-object", "--bucket", self.bucket, "--key", key, "--region", self.region],
               check=False, timeout=120)
        return p.returncode == 0

    def exists_prefix(self, prefix: str) -> bool:
        """True when any object exists under the prefix (ListObjectsV2)."""
        p = sh(["aws", "s3api", "list-objects-v2", "--bucket", self.bucket, "--prefix", prefix, "--max-keys", "1",
                "--query", "KeyCount", "--output", "text", "--region", self.region], timeout=120)
        return p.stdout.strip() not in ("", "0", "None")

    def get(self, uri: str, dest: Path) -> None:
        """Download one object."""
        dest.parent.mkdir(parents=True, exist_ok=True)
        sh(["aws", "s3", "cp", uri, str(dest), "--region", self.region, "--only-show-errors"], timeout=900)

    def get_dir(self, uri: str, dest: Path) -> None:
        """Download a prefix."""
        dest.mkdir(parents=True, exist_ok=True)
        sh(["aws", "s3", "cp", uri.rstrip("/") + "/", str(dest), "--recursive", "--region", self.region,
            "--only-show-errors"], timeout=1800)

    def put(self, src: Path, key: str) -> None:
        """Upload one file; refuses to overwrite an existing key (append-only)."""
        if self.exists_key(key):
            raise StepError(f"refusing to overwrite {self.uri(key)} (append-only)")
        sh(["aws", "s3", "cp", str(src), self.uri(key), "--region", self.region, "--only-show-errors"], timeout=900)

    def put_run_dir(self, local: Path, prefix: str, marker: str = "run.json", skip: frozenset[str] = frozenset()) -> None:
        """Upload a run directory, the top-level marker file last; ``skip`` = top-level names already uploaded."""
        files = sorted(p for p in local.rglob("*") if p.is_file())
        top = local / marker
        for p in [f for f in files if f != top and not (f.parent == local and f.name in skip)] + [top]:
            if p.exists():
                self.put(p, f"{prefix.rstrip('/')}/{p.relative_to(local).as_posix()}")


def http_req(method: str, url: str, body: dict[str, Any] | None = None, timeout: float = 60) -> tuple[int, bytes]:
    """One HTTP request (status, body)."""
    parts = urllib.parse.urlsplit(url)
    conn = http.client.HTTPConnection(parts.hostname or "127.0.0.1", parts.port or 80, timeout=timeout)
    try:
        path = parts.path or "/"
        conn.request(method, path, body=None if body is None else json.dumps(body),
                     headers={"Content-Type": "application/json"})
        r = conn.getresponse()
        return r.status, r.read()
    finally:
        conn.close()


def scrape(url: str) -> dict[str, float]:
    """GET /metrics; {} when unreachable."""
    try:
        st, raw = http_req("GET", url, None, 30)
    except OSError:
        return {}
    return parse_metrics(raw.decode("utf-8", "replace")) if st == 200 else {}


def stream_completion(frontend: str, prompt: list[int], max_tokens: int, timeout: float = 900) -> dict[str, Any]:
    """Streamed /v1/completions (token-ID prompt): TTFT, usage, nvext (worker ids, router kv_hit_rate)."""
    body = {"model": MODEL, "prompt": prompt, "max_tokens": max_tokens, "temperature": 0, "stream": True,
            "ignore_eos": True, "stream_options": {"include_usage": True},
            "nvext": {"extra_fields": NVEXT_FIELDS}}
    parts = urllib.parse.urlsplit(frontend)
    conn = http.client.HTTPConnection(parts.hostname or "", parts.port or 80, timeout=timeout)
    rec: dict[str, Any] = {"ok": False}
    nvext: dict[str, Any] = {}
    usage: dict[str, Any] = {}
    t0 = time.perf_counter()
    ttft = None
    try:
        conn.request("POST", "/v1/completions", body=json.dumps(body), headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        rec["http_status"] = resp.status
        if resp.status != 200:
            rec["error"] = resp.read().decode("utf-8", "replace")[:300]
            return rec
        while True:
            line = resp.readline()
            if not line:
                break
            txt = line.decode("utf-8", "replace").strip()
            if not txt.startswith("data:"):
                continue
            data = txt[5:].strip()
            if data == "[DONE]":
                break
            try:
                chunk = json.loads(data)
            except json.JSONDecodeError:
                continue
            if chunk.get("nvext"):
                nvext.update(chunk["nvext"])
            if chunk.get("usage"):
                usage = chunk["usage"]
            ch = chunk.get("choices") or []
            if ch and ch[0].get("text") and ttft is None:
                ttft = time.perf_counter() - t0
        rec["ok"] = True
    except (OSError, http.client.HTTPException) as exc:
        rec["error"] = f"{type(exc).__name__}: {exc}"[:300]
    finally:
        conn.close()
    rec.update({"ttft_s": ttft, "e2e_s": time.perf_counter() - t0, "prompt_tokens": usage.get("prompt_tokens"),
                "completion_tokens": usage.get("completion_tokens"),
                "cached_tokens": int(((usage.get("prompt_tokens_details") or {}).get("cached_tokens")) or 0),
                "kv_hit_rate": (nvext.get("timing") or {}).get("kv_hit_rate"), "worker_id": nvext.get("worker_id")})
    return rec


# ================================================================ the driver
@dataclass
class Pod:
    """One DGD pod."""

    name: str
    uid: str
    role: str
    ip: str
    ready: bool
    restarts: int
    terminating: bool
    router_mode: str | None
    image_ids: list[str]

    def as_dict(self) -> dict[str, Any]:
        """JSON view."""
        return {"name": self.name, "uid": self.uid, "role": self.role, "ip": self.ip, "restarts": self.restarts,
                "image_ids": self.image_ids}


def pods_of(ns: str) -> list[Pod]:
    """Pods of the q38 DGD in a namespace."""
    doc = kjson("get", "pods", "-n", ns, "-l", f"{LABEL_DGD}={DGD_NAME}")
    out = []
    for it in doc.get("items", []):
        if not K8S_NAME.fullmatch(it["metadata"]["name"]):  # it becomes a kubectl argument and a file name
            raise StepError(f"pod name {it['metadata']['name']!r} in {ns} is not a DNS-1123 name")
        st = it.get("status", {})
        conds = {c.get("type"): c.get("status") for c in st.get("conditions", []) or []}
        mode = None
        for c in it["spec"].get("containers", []):
            if c.get("name") == "main":
                for e in c.get("env", []) or []:
                    if e.get("name") == "DYN_ROUTER_MODE":
                        mode = e.get("value")
        out.append(Pod(
            name=it["metadata"]["name"], uid=it["metadata"]["uid"],
            role=it["metadata"].get("labels", {}).get(LABEL_TYPE, "?"), ip=st.get("podIP", ""),
            ready=conds.get("Ready") == "True",
            restarts=sum(int(cs.get("restartCount", 0)) for cs in st.get("containerStatuses", []) or []),
            terminating=it["metadata"].get("deletionTimestamp") is not None, router_mode=mode,
            image_ids=sorted({cs.get("imageID", "") for cs in st.get("containerStatuses", []) or []})))
    return out


def required_permissions() -> list[tuple[str, str, str, str]]:
    """(verb, resource, subresource, namespace or '' = all namespaces) the driver needs."""
    req: list[tuple[str, str, str, str]] = []
    for ns in NAMESPACES:
        req += [(v, "dynamographdeployments.nvidia.com", "", ns) for v in ("get", "list", "create", "patch", "delete")]
        req += [("get", "pods", "", ns), ("list", "pods", "", ns), ("delete", "pods", "", ns),
                ("create", "pods", "exec", ns), ("get", "pods", "log", ns), ("list", "events", "", ns)]
        if ns in HICACHE_NAMESPACES:
            req.append(("create", "pods", "", ns))
    # v4: the capture and compose Jobs of the E7' takes are created in bench (and polled with get).
    req += [("get", "jobs.batch", "", "bench"), ("create", "jobs.batch", "", "bench"), ("get", "cronjobs.batch", "", "bench"),
            ("list", "pods", "", ""), ("get", "nodes", "", ""), ("list", "dynamographdeployments.nvidia.com", "", "")]
    return req


class Driver:
    """Executes one plan."""

    def __init__(self, plan: Plan, args: argparse.Namespace | None) -> None:
        self.plan = plan
        self.args = args
        self.work = Path(os.environ.get("WORK", "/work"))
        self.dgd_dir = Path(os.environ.get("DGD_DIR", "/etc/q38/dgds"))
        self.driver_dir = Path(os.environ.get("DRIVER_DIR", "/etc/q38/driver"))
        self.s3 = S3(os.environ["RESULTS_BUCKET"], os.environ["AWS_REGION"])
        self.cb_node = os.environ.get("CB_NODE", "")
        self.prom = os.environ.get("PROMETHEUS_URL", "")
        self.l3_root = os.environ.get("L3_ROOT", f"/mnt/.ephemeral/hicache-l3/{PART_OF}")
        self.cleanup_image = os.environ.get("L3_CLEANUP_IMAGE", "")
        self.hard_stop_epoch = iso_epoch(plan.hard_stop)
        self.prefix = f"matrix/{plan.run_id}"
        self.trace = self.work / "inputs" / "w2.jsonl"
        self.sidecar = self.work / "inputs" / "w2.side.jsonl"
        self.tokenizer = self.work / "inputs" / "tokenizer"
        self.hf_hub = self.work / "hf-hub"
        self.trace_rows = 0
        self.trace_span_s = 0.0
        self.slo: dict[str, Any] = dict(plan.slo)
        self.slo_source = "plan"
        self.slo_locked = False
        self.slo_lock: dict[str, Any] | None = None
        self.cold_probe: dict[str, Any] | None = None
        self.topology: str | None = None
        self.deploy_info: dict[str, Any] = {}
        self.boot_cache: dict[str, dict[str, Any]] = {}
        self.launch = utcnow().replace(":", "").replace("-", "")
        self.seq = itertools.count(1)
        self.failed = 0
        self.consecutive_failures = 0
        self.teardowns: list[dict[str, Any]] = []
        self.stop_reason: str | None = None

    # ---------------------------------------------------------------- setup
    def install_demo_bench(self) -> dict[str, Any]:
        """pip install --no-deps the mounted wheel into $WORK/pylib after checking its sha256."""
        sums = (self.driver_dir / "wheel.sha256").read_text().split()
        want, name = sums[0], sums[1]
        wheel = self.driver_dir / name
        got = hashlib.sha256(wheel.read_bytes()).hexdigest()
        if got != want:
            raise ConfigError(f"demo_bench wheel sha256 {got} != {want}")
        lib = self.work / "pylib"
        if not lib.exists():
            sh([sys.executable, "-m", "pip", "install", "--no-deps", "--no-index", "--target", str(lib), str(wheel)],
               timeout=300)
        sys.path.insert(0, str(lib))
        import demo_bench  # from the freshly installed wheel

        where = Path(demo_bench.__file__).resolve()
        if lib.resolve() not in where.parents:
            raise ConfigError(f"demo_bench imported from {where}, not from {lib}")
        return {"wheel": name, "sha256": got, "path": str(where)}

    def fetch_inputs(self) -> dict[str, Any]:
        """Download the W2 window trace + sidecar and the tokenizer from S3; verify every sha256."""
        w2, tk = self.plan.w2, self.plan.tokenizer
        self.s3.get(w2["trace"], self.trace)
        self.s3.get(w2["sidecar"], self.sidecar)
        for path, want in ((self.trace, w2["trace_sha256"]), (self.sidecar, w2["sidecar_sha256"])):
            got = hashlib.sha256(path.read_bytes()).hexdigest()
            if got != want:
                raise ConfigError(f"{path.name}: sha256 {got} != plan {want}")
        self.s3.get_dir(tk["prefix"], self.tokenizer)
        sums = self.tokenizer / "SHA256SUMS"
        if hashlib.sha256(sums.read_bytes()).hexdigest() != tk["sums_sha256"]:
            raise ConfigError("tokenizer SHA256SUMS does not match the plan")
        for line in sums.read_text().splitlines():
            digest, fname = line.split(maxsplit=1)
            if hashlib.sha256((self.tokenizer / fname.strip()).read_bytes()).hexdigest() != digest:
                raise ConfigError(f"tokenizer file {fname} sha256 mismatch")
        # aiperf loads it through its own offline resolver: prove exactly that works before any GPU time is spent.
        hf_cache_layout(self.tokenizer, self.hf_hub, MODEL, MODEL_REVISION)
        probe = run_argv([sys.executable, "-c", "from aiperf.common.tokenizer import Tokenizer; "
                          f"t = Tokenizer.from_pretrained({MODEL!r}, revision={MODEL_REVISION!r}); print(type(t._tokenizer).__name__)"],
                         timeout=300, env=self.aiperf_env())
        if probe.returncode != 0:
            raise ConfigError(f"aiperf cannot load the tokenizer offline: {probe.stderr.strip()[-400:]}")
        doc = self.check_trace()
        doc["tokenizer"] = {"aiperf_loader": probe.stdout.strip(), "hf_hub_cache": str(self.hf_hub),
                            "repo": MODEL, "revision": MODEL_REVISION}
        return doc

    def aiperf_env(self) -> dict[str, str]:
        """aiperf's environment: offline, tokenizer from the HF cache layout under $WORK."""
        return {**os.environ, "HF_HUB_CACHE": str(self.hf_hub), "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1"}

    def check_trace(self) -> dict[str, Any]:
        """Trace/sidecar pairing, order and context checks (also used by --check)."""
        rows = [json.loads(x) for x in self.trace.read_text().splitlines() if x.strip()]
        side = [json.loads(x) for x in self.sidecar.read_text().splitlines() if x.strip()]
        if len(rows) != len(side):
            raise ConfigError(f"trace has {len(rows)} rows, sidecar {len(side)}")
        ts = [r["timestamp"] for r in rows]
        if ts != sorted(ts):
            raise ConfigError("trace timestamps are not sorted")
        for i, (r, s) in enumerate(zip(rows, side, strict=True)):
            if s.get("input_length") != r.get("input_length"):
                raise ConfigError(f"row {i}: sidecar input_length != trace input_length")
        over = sum(1 for r in rows if r["input_length"] + r["output_length"] + CONTEXT_MARGIN > CONTEXT_LENGTH)
        if over:
            raise ConfigError(f"{over} rows exceed the context length {CONTEXT_LENGTH} (ISL + OSL + {CONTEXT_MARGIN})")
        self.trace_rows = len(rows)
        self.trace_span_s = (ts[-1] - ts[0]) / 1000.0
        classes: dict[str, int] = {}
        for s in side:
            classes[s["class"]] = classes.get(s["class"], 0) + 1
        return {"rows": len(rows), "span_s": self.trace_span_s, "classes": classes,
                "max_isl_plus_osl": max(r["input_length"] + r["output_length"] for r in rows)}

    # ---------------------------------------------------------------- preflight (reads only, then the plan lock)
    def check_rbac(self) -> list[dict[str, Any]]:
        """kubectl auth can-i for every permission the driver uses (as its own ServiceAccount)."""
        out = []
        for verb, res, sub, ns in required_permissions():
            args = ["auth", "can-i", verb, res, *(["--subresource", sub] if sub else []),
                    *(["-n", ns] if ns else ["--all-namespaces"])]
            p = kubectl(*args, check=False, timeout=60)
            out.append({"verb": verb, "resource": res + (f"/{sub}" if sub else ""), "namespace": ns or "*",
                        "allowed": p.stdout.strip() == "yes"})
        denied = [f"{r['verb']} {r['resource']} in {r['namespace']}" for r in out if not r["allowed"]]
        if denied:
            raise ConfigError(f"RBAC: ServiceAccount cannot {denied}")
        return out

    def check_job_deadline(self) -> dict[str, Any]:
        """The driver's own Job must end (activeDeadlineSeconds) at the plan hard stop."""
        name = os.environ.get("JOB_NAME", "")
        if not name:
            raise ConfigError("JOB_NAME is not set: launch only through the rendered Job "
                              "(tools/matrix_driver/render_driver.py --launch)")
        return job_deadline_check(kjson("get", "job", name, "-n", "bench"), self.hard_stop_epoch)

    def check_no_other_driver(self) -> list[str]:
        """No other driver Job may be active in bench: a later plan would tear down an earlier plan's DGD as a
        leftover (ensure never adopts) and race it for the GPUs (e.g. plan-e7 launched before E6' ended).

        Raises:
            ConfigError: Another driver Job has not finished.
        """
        own = os.environ.get("JOB_NAME", "")
        jobs = kjson("get", "job", "-n", "bench", "-l", DRIVER_JOB_SELECTOR).get("items", [])
        names = sorted(j["metadata"]["name"] for j in jobs)
        active = [j["metadata"]["name"] for j in jobs if j["metadata"]["name"] != own and job_state(j) == "running"]
        if active:
            raise ConfigError(f"driver Job(s) {active} still active in bench: wait for them to end (one plan at a time)")
        return names

    def check_watchdog(self) -> dict[str, Any]:
        """The independent watchdog CronJob must exist, be active and open a window at the plan hard stop.

        v4: the CronJob may carry several windows (env WINDOWS ``<start>/<end>,...``, e.g. the matrix 04:30Z and the
        E7' 09:00Z); without WINDOWS its legacy HARD_STOP must equal the plan hard stop (v3 rule).
        """
        p = kubectl("get", "cronjob", WATCHDOG, "-n", "bench", "-o", "json", check=False)
        if p.returncode != 0:
            raise ConfigError(f"watchdog CronJob bench/{WATCHDOG} missing: apply the 20-watchdog.yaml rendered by "
                              "tools/matrix_driver/render_driver.py first")
        spec = json.loads(p.stdout).get("spec", {})
        if spec.get("suspend"):
            raise ConfigError(f"watchdog CronJob bench/{WATCHDOG} is suspended")
        ctrs = spec["jobTemplate"]["spec"]["template"]["spec"]["containers"]
        env = {e["name"]: e.get("value") for c in ctrs for e in c.get("env", [])}
        windows = watchdog_windows(env)
        match = next((w for w in windows if iso_epoch(w[0]) == self.hard_stop_epoch), None)
        if match is None:
            raise ConfigError(f"watchdog windows {windows} (HARD_STOP {env.get('HARD_STOP')}) do not open at the plan "
                              f"hard stop {self.plan.hard_stop}: re-render 20-watchdog.yaml with "
                              "tools/matrix_driver/render_driver.py --watchdog-windows and re-apply it")
        return {"cronjob": f"bench/{WATCHDOG}", "hard_stop": match[0], "window_end": match[1],
                "windows": env.get("WINDOWS")}

    def load_slo_lock(self) -> dict[str, Any]:
        """e4/e5/e6: the SLO of record from the e3 calibration file, verified against the e3 lock."""
        ref = self.plan.slo_ref
        assert ref is not None
        lock_path, cal_path = self.work / "slo" / "slo-lock.json", self.work / "slo" / "slo-calibration.json"
        try:
            self.s3.get(ref["lock"], lock_path)
            self.s3.get(ref["from"], cal_path)
        except StepError as exc:
            raise ConfigError(f"e3 SLO lock or calibration missing ({ref['lock']}): run plan-e3 first ({exc})") from exc
        lock = json.loads(lock_path.read_text())
        data = cal_path.read_bytes()
        self.slo = verify_slo_lock(ref, lock, data)
        self.slo_source = f"e3 lock {ref['lock']}"
        self.slo_locked = True
        self.slo_lock = {**ref, "slo_calibration_sha256": hashlib.sha256(data).hexdigest(), "slo": dict(self.slo)}
        return dict(self.slo_lock)

    def put_json(self, doc: dict[str, Any], key: str) -> str:
        """Write a JSON doc locally under $WORK/meta and upload it (append-only)."""
        local = self.work / "meta" / key.replace("/", "__")
        local.parent.mkdir(parents=True, exist_ok=True)
        local.write_text(json.dumps(doc, indent=1, default=str))
        self.s3.put(local, key)
        return key

    def preflight(self) -> dict[str, Any]:
        """Every check that refuses (exit 2) before any cluster change, then the plan lock (first S3 object)."""
        doc: dict[str, Any] = {"rbac": self.check_rbac(), "job_deadline": self.check_job_deadline(),
                               "watchdog": self.check_watchdog(), "driver_jobs": self.check_no_other_driver()}
        if self.plan.slo_ref is not None:
            doc["slo_lock"] = self.load_slo_lock()
        lock = {"run_id": self.plan.run_id, "plan_sha256": self.plan.source_sha256, "hard_stop": self.plan.hard_stop,
                "on_failure": self.plan.on_failure, "cut_order": self.plan.cut_order, "w2": self.plan.w2,
                "slo": self.slo, "slo_source": self.slo_source,
                "slo_calibration_sha256": (self.slo_lock or {}).get("slo_calibration_sha256"),
                "e3_plan_sha256": (self.plan.slo_ref or {}).get("e3_plan_sha256"), "launch": self.launch,
                "driver": DRIVER_VERSION, "driver_sha256": self.own_sha256(), "at": utcnow(),
                "runs": [s.key for s in self.plan.steps if s.kind in ("run", "take")],
                "takes": [s.key for s in self.plan.steps if s.kind == "take"]}
        doc["plan_lock"] = self.put_json(lock, f"{self.prefix}/_lock/plan-lock-{self.launch}.json")
        return doc

    def own_sha256(self) -> str | None:
        """sha256 of the mounted driver file (None offline)."""
        p = self.driver_dir / "matrix_driver.py"
        return hashlib.sha256(p.read_bytes()).hexdigest() if p.exists() else None

    # ---------------------------------------------------------------- deadline
    def guard(self, need_s: int, what: str) -> None:
        """Raise HardStop when ``what`` (needing ``need_s`` seconds, I) cannot finish before the hard stop."""
        if self.stop_reason:
            raise HardStop(f"{what}: {self.stop_reason}")
        now = time.time()
        if now + need_s > self.hard_stop_epoch:
            raise HardStop(f"{what}: needs ~{need_s}s (I), {int(self.hard_stop_epoch - now)}s left to {self.plan.hard_stop}")

    def on_sigterm(self, signum: int, frame: Any) -> None:
        """SIGTERM (Job activeDeadlineSeconds, watchdog Job delete, eviction) = hard stop, once."""
        if self.stop_reason is None:
            self.stop_reason = "SIGTERM (Job activeDeadlineSeconds, watchdog or eviction)"
            raise HardStop(self.stop_reason)

    # ---------------------------------------------------------------- topology
    def live_topologies(self) -> list[str]:
        """Topologies whose q38 DGD (ours, by label) currently exists."""
        return list(self.live_dgds())

    def live_dgds(self) -> dict[str, str]:
        """Live q38 DGDs of this driver: topology -> q38-matrix/workers-sha256 annotation."""
        live: dict[str, str] = {}
        for t, spec in TOPOLOGIES.items():
            p = kubectl("get", "dgd", DGD_NAME, "-n", spec["namespace"], "-o", "json", check=False)
            if p.returncode == 0:
                meta = json.loads(p.stdout)["metadata"]
                if meta.get("labels", {}).get("app.kubernetes.io/part-of") == PART_OF:
                    live[t] = (meta.get("annotations") or {}).get("q38-matrix/workers-sha256", "")
        return live

    def rendered_workers_sha(self, cell: str) -> str:
        """The workers-sha256 annotation of the cell's rendered DGD file."""
        m = re.search(r'q38-matrix/workers-sha256: "?([0-9a-f]{64})"?', (self.dgd_dir / f"{cell}.yaml").read_text())
        if not m:
            raise ConfigError(f"{cell}.yaml has no q38-matrix/workers-sha256 annotation")
        return m.group(1)

    def dgd_present(self, ns: str) -> bool:
        """True while the q38 DGD object exists in ``ns`` (also while it is terminating); True on an API error."""
        p = kubectl("get", "dgd", DGD_NAME, "-n", ns, "-o", "name", "--ignore-not-found", check=False)
        return p.returncode != 0 or bool(p.stdout.strip())

    def teardown(self, topology: str, wait_s: int = TEARDOWN_TIMEOUT_S) -> dict[str, Any]:
        """Delete the q38 DGD of a topology, wait until the DGD object AND its pods are gone, then (HiCache) empty
        its L3 directory.

        v3: kubectl refuses a resource name together with ``-l`` ("name cannot be provided when a selector is
        specified"), which v2 sent with check=False, so the delete never reached the API. The DGD is now selected by
        label + ``--field-selector metadata.name`` (only our q38 object), a refused delete is an error, and the
        teardown waits for the DGD itself, not only for its pods.

        Raises:
            StepError: The delete is refused, or the DGD or its pods are still present after ``wait_s``.
        """
        ns = TOPOLOGIES[topology]["namespace"]
        log("teardown", topology=topology, namespace=ns)
        t0 = time.time()
        p = kubectl("delete", "dgd", "-n", ns, "-l", f"app.kubernetes.io/part-of={PART_OF}",
                    "--field-selector", f"metadata.name={DGD_NAME}", "--ignore-not-found", "--wait=false",
                    check=False)
        delete = {"rc": p.returncode, "stdout": p.stdout.strip()[:300], "stderr": p.stderr.strip()[:300]}
        log("teardown delete", topology=topology, **delete)
        if p.returncode != 0:
            raise StepError(f"teardown of {topology}: delete refused (rc {p.returncode}): {delete['stderr']}")
        dgd, pods = True, True
        for _ in range(max(1, wait_s // 10)):
            dgd, pods = self.dgd_present(ns), bool(pods_of(ns))
            if not dgd and not pods:
                break
            pause(10)
        else:
            raise StepError(f"teardown of {topology}: after {wait_s}s dgd_present={dgd} pods_present={pods}")
        if self.topology == topology:
            self.topology = None
        doc: dict[str, Any] = {"topology": topology, "namespace": ns, "at": utcnow(), "launch": self.launch,
                               "delete": delete, "dgd_and_pods_gone_s": round(time.time() - t0, 1)}
        if TOPOLOGIES[topology]["hicache"]:
            doc["l3_cleanup"] = self.l3_cleanup(ns)
        self.teardowns.append(doc)
        try:
            self.put_json(doc, f"{self.prefix}/_teardown/{topology}-{self.launch}-{next(self.seq)}.json")
        except StepError as exc:
            log("teardown record upload failed", error=str(exc)[:300])
        return doc

    def l3_cleanup(self, ns: str) -> dict[str, Any]:
        """Empty the namespace's whole L3 directory with a no-GPU pod; returns its counts and df."""
        name = f"q38-l3-cleanup-{self.launch.lower()}-{next(self.seq)}"
        pod = l3_cleanup_pod(ns, name, self.cleanup_image, self.l3_root, self.cb_node)
        kubectl("create", "-f", "-", stdin=json.dumps(pod))
        phase = ""
        for _ in range(60):
            phase = kjson("get", "pod", name, "-n", ns).get("status", {}).get("phase", "")
            if phase in ("Succeeded", "Failed"):
                break
            pause(5)
        text = kubectl("logs", "-n", ns, name, check=False).stdout
        kubectl("delete", "pod", "-n", ns, name, "--ignore-not-found", "--wait=false", check=False)
        doc: dict[str, Any] = {"pod": name, "phase": phase, "dir": f"{self.l3_root}/{ns}"}
        for k, v in re.findall(r"(\w+)=(\S+)", text):
            doc[k] = int(v) if v.isdigit() else v
        if phase != "Succeeded" or doc.get("files_after") != 0:
            raise StepError(f"L3 cleanup of {ns} failed: {doc}")
        log("l3 cleanup", namespace=ns, files_before=doc.get("files_before"), df_bytes_avail=doc.get("df_bytes_avail"))
        return doc

    def gpu_use(self) -> tuple[int, dict[str, int]]:
        """Allocatable GPUs of the CB node and the GPU requests of every non-finished pod there, by namespace."""
        node = kjson("get", "node", self.cb_node)
        alloc = int(node["status"].get("allocatable", {}).get("nvidia.com/gpu", "0"))
        pods = kjson("get", "pods", "-A", "--field-selector", f"spec.nodeName={self.cb_node}")
        used: dict[str, int] = {}
        for it in pods.get("items", []):
            if it.get("status", {}).get("phase") in ("Succeeded", "Failed"):
                continue
            n = 0
            for c in it["spec"].get("containers", []) + it["spec"].get("initContainers", []):
                n += int((c.get("resources", {}).get("requests", {}) or {}).get("nvidia.com/gpu", 0) or 0)
            if n:
                ns = it["metadata"].get("namespace", "?")
                used[ns] = used.get(ns, 0) + n
        return alloc, used

    def assert_exclusive(self, topology: str | None) -> dict[str, Any]:
        """Fatal unless the only q38 DGD in the cluster is ``topology``'s (none when deploying) and no other pod
        holds a GPU on the CB node (deploying: all 8 free, waiting for our own pods to release them)."""
        items = kjson("get", "dgd", "-A").get("items", [])
        q38 = sorted({(it["metadata"].get("namespace", "?"), it["metadata"]["name"]) for it in items
                      if it["metadata"]["name"] == DGD_NAME
                      or (it["metadata"].get("labels") or {}).get("app.kubernetes.io/part-of") == PART_OF})
        allowed = {(TOPOLOGIES[topology]["namespace"], DGD_NAME)} if topology else set()
        others = [f"{ns}/{n}" for ns, n in q38 if (ns, n) not in allowed]
        if others:
            raise FatalError(f"another q38 DGD exists: {others}")
        own = TOPOLOGIES[topology]["namespace"] if topology else None
        for _ in range(max(1, GPU_FREE_TIMEOUT_S // 10)):
            alloc, used = self.gpu_use()
            foreign = {ns: n for ns, n in used.items() if ns != own}
            if alloc >= GPUS_PER_NODE and not foreign:
                return {"q38_dgds": [f"{ns}/{n}" for ns, n in q38], "gpus_allocatable": alloc, "gpu_requests": used}
            if topology:
                break  # a live topology: foreign GPU pods are a hard error, no wait
            log("waiting for free GPUs", used=used)
            pause(10)
        raise FatalError(f"GPUs not exclusive on {self.cb_node}: allocatable {alloc}, requests by namespace {used}")

    def apply_cell(self, cell: str) -> str:
        """Server-side apply of the cell's rendered DGD; returns its sha256."""
        path = self.dgd_dir / f"{cell}.yaml"
        kubectl("apply", "--server-side", "--field-manager=q38-matrix-driver", "--force-conflicts", "-f", str(path),
                timeout=120)
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def expected_roles(self, topology: str) -> dict[str, int]:
        """Expected pod count per component type."""
        if TOPOLOGIES[topology]["pd"]:
            return {"frontend": 1, "prefill": 4, "decode": 4}
        return {"frontend": 1, "worker": 8}

    def boot_watchdog(self, topology: str, boot_dir: Path) -> float:
        """Wait until every expected pod is Ready; fail fast on restarts, long Pending or the timeout."""
        ns = TOPOLOGIES[topology]["namespace"]
        want = self.expected_roles(topology)
        t0 = time.time()
        limit = BOOT_TIMEOUT_S["pd" if TOPOLOGIES[topology]["pd"] else "agg"]
        for i in itertools.count():
            pods = [p for p in pods_of(ns) if not p.terminating]
            counts: dict[str, int] = {}
            for p in pods:
                if p.ready:
                    counts[p.role] = counts.get(p.role, 0) + 1
            if counts == want and len(pods) == sum(want.values()):
                return time.time() - t0
            bad = [p.name for p in pods if p.restarts > 0]
            elapsed = max(time.time() - t0, 15.0 * i)
            reason = None
            if bad:
                reason = f"restarts during boot: {bad}"
            elif elapsed > PENDING_MAX_S and any(not p.ip for p in pods):
                reason = f"pods still Pending (unscheduled) after {PENDING_MAX_S}s"
            elif elapsed > limit:
                reason = f"boot timeout {limit}s (ready {counts}, want {want})"
            if reason:
                self.collect_boot_evidence(ns, boot_dir)
                raise StepError(f"boot watchdog ({topology}): {reason}")
            if i % 4 == 0:
                log("booting", topology=topology, ready=counts, want=want, elapsed_s=int(elapsed))
            pause(15)
        raise AssertionError("unreachable")

    def collect_boot_evidence(self, ns: str, boot_dir: Path) -> None:
        """Events and log tails of every pod (no request content in them)."""
        boot_dir.mkdir(parents=True, exist_ok=True)
        (boot_dir / "events.txt").write_text(kubectl("get", "events", "-n", ns, "--sort-by=.lastTimestamp",
                                                     check=False).stdout)
        for p in pods_of(ns):
            (boot_dir / f"{p.name}.log").write_text(
                kubectl("logs", "-n", ns, p.name, "-c", "main", "--tail=400", check=False).stdout)

    def frontend_url(self, topology: str) -> str:
        """Frontend service URL of a topology."""
        return f"http://{DGD_NAME}-frontend.{TOPOLOGIES[topology]['namespace']}.svc.cluster.local:8000"

    def wait_model(self, topology: str, timeout: float = 900) -> None:
        """Wait until the Frontend lists the model."""
        url = self.frontend_url(topology) + "/v1/models"
        for _ in range(int(timeout // 5)):
            try:
                st, raw = http_req("GET", url, None, 10)
                if st == 200 and MODEL.encode() in raw:
                    return
            except OSError:
                pass
            pause(5)
        raise StepError(f"model not listed at {url} after {timeout}s")

    def targets(self, topology: str) -> tuple[list[Pod], dict[str, str], dict[str, str]]:
        """Ready engine pods, their metrics URLs and roles (keyed 'w:<pod>')."""
        ns = TOPOLOGIES[topology]["namespace"]
        eng = [p for p in pods_of(ns) if p.role in ("worker", "prefill", "decode") and not p.terminating]
        if not eng or not all(p.ready and p.ip for p in eng):
            raise StepError(f"not every engine pod in {ns} is Ready")
        urls = {f"w:{p.name}": f"http://{p.ip}:{SYSTEM_PORT}/metrics" for p in eng}
        roles = {f"w:{p.name}": p.role for p in eng}
        return eng, urls, roles

    def snapshot(self, topology: str) -> dict[str, dict[str, float]]:
        """Scrape every engine and the Frontend in parallel."""
        _, urls, _ = self.targets(topology)
        urls = {**urls, "frontend": self.frontend_url(topology) + "/metrics"}
        with ThreadPoolExecutor(len(urls)) as ex:
            return dict(zip(urls, ex.map(scrape, urls.values()), strict=True))

    def warm_up(self, topology: str) -> dict[str, Any]:
        """After a boot: rounds of N concurrent distinct 32K-token requests (N = router targets) until every engine
        served one (max 4 rounds). E1: the first request per worker took 7.1-11.9 s (JIT), so no run starts cold."""
        eng, _, roles = self.targets(topology)
        n = sum(1 for p in eng if p.role in ("worker", "prefill"))
        url = self.frontend_url(topology)
        pre = self.snapshot(topology)
        recs: list[dict[str, Any]] = []
        served: dict[str, float] = {}
        rounds = 0
        for rnd in range(4):
            rounds = rnd + 1
            with ThreadPoolExecutor(n) as ex:
                recs += list(ex.map(lambda i, r=rnd: stream_completion(
                    url, canary_tokens(f"warm-{self.launch}-{r}-{i}", 32768), 16), range(n)))
            post = self.snapshot(topology)
            served = {t: series_sum(post[t], "sglang:num_requests_total") - series_sum(pre[t], "sglang:num_requests_total")
                      for t in roles}
            if rnd >= 1 and all(v > 0 for v in served.values()):
                break
        ok = sum(1 for r in recs if r["ok"])
        if ok == 0:
            raise StepError("warm-up: every request failed")
        return {"rounds": rounds, "requests": len(recs), "ok": ok, "prompt_tokens": 32768,
                "engines_served": sum(1 for v in served.values() if v > 0), "engines": len(served),
                "ttft_s_max": max((r["ttft_s"] or 0.0) for r in recs)}

    def parallel_exec(self, ns: str, pods: list[Pod], script: str) -> dict[str, str]:
        """The same read-only snippet in several engine pods at once."""
        with ThreadPoolExecutor(max(1, len(pods))) as ex:
            return dict(zip([p.name for p in pods], ex.map(lambda p: k_exec(ns, p.name, script), pods), strict=True))

    def gpu_identity(self, topology: str) -> dict[str, Any]:
        """CUDA/NVIDIA_VISIBLE_DEVICES of each engine process + nvidia-smi, and the uniqueness check."""
        ns = TOPOLOGIES[topology]["namespace"]
        eng, _, _ = self.targets(topology)
        script = ("tr '\\0' '\\n' < /proc/1/environ | grep -E '^(CUDA|NVIDIA)_VISIBLE_DEVICES=' ; echo ---; "
                  "nvidia-smi --query-gpu=index,uuid,pci.bus_id --format=csv,noheader")
        raw = self.parallel_exec(ns, eng, script)
        per = {}
        for name, out in raw.items():
            env_text, _, smi_text = out.partition("---\n")
            per[name] = effective_gpu(env_text, smi_text, privileged=TOPOLOGIES[topology]["pd"])
        return {"per_pod": per, "uniqueness": gpu_uniqueness(per, len(eng)), "at": utcnow()}

    def boot_facts(self, topology: str) -> dict[str, dict[str, Any]]:
        """parse_boot_facts of every engine's log head (cached by pod UID: the head never changes)."""
        ns = TOPOLOGIES[topology]["namespace"]
        eng, _, _ = self.targets(topology)
        out = {}
        for p in eng:
            if p.uid not in self.boot_cache:
                text = kubectl("logs", "-n", ns, p.name, "-c", "main", f"--limit-bytes={LOG_HEAD_BYTES}", check=False,
                               timeout=180).stdout
                self.boot_cache[p.uid] = {"pod": p.name, "role": p.role, **parse_boot_facts(text)}
            out[p.name] = self.boot_cache[p.uid]
        return out

    def ensure(self, cell: str, run_dir: Path) -> dict[str, Any]:
        """Bring the cell's topology up (or toggle R on the one this launch deployed). Returns what was done.

        A DGD is reused only when this launch deployed it and nothing failed since (a failure tears it down); a
        DGD left by an earlier launch is torn down and re-booted, never adopted.
        """
        topology, mode = CELLS[cell]
        ns = TOPOLOGIES[topology]["namespace"]
        live_map = self.live_dgds()
        live = list(live_map)
        info: dict[str, Any] = {"cell": cell, "topology": topology, "namespace": ns, "router_mode": mode}
        if live == [topology] and self.topology == topology and live_map[topology] == self.rendered_workers_sha(cell):
            info["exclusive"] = self.assert_exclusive(topology)
            pods = pods_of(ns)
            fe = [p for p in pods if p.role == "frontend" and not p.terminating]
            if fe and all(p.router_mode == mode for p in fe):
                info["action"] = "none"
                return info
            workers_before = {p.name: p.uid for p in pods if p.role != "frontend"}
            sha = self.apply_cell(cell)
            t0 = time.time()
            for _ in range(120):
                # A changed env replaces the Frontend pod: done when the only Frontend left is Ready on the new mode.
                fe = [p for p in pods_of(ns) if p.role == "frontend"]
                if len(fe) == 1 and fe[0].ready and not fe[0].terminating and fe[0].router_mode == mode:
                    break
                pause(5)
            else:
                raise StepError(f"Frontend not serving DYN_ROUTER_MODE={mode} after 600s")
            workers_after = {p.name: p.uid for p in pods_of(ns) if p.role != "frontend"}
            if workers_after != workers_before:
                raise StepError("R toggle redeployed workers (must be Frontend-only)")
            self.wait_model(topology)
            pause(15)  # router discovers the workers
            info.update(action="router-toggle", dgd_sha256=sha, toggle_s=round(time.time() - t0, 1),
                        workers_unchanged=True)
            return info
        if live:
            info["torn_down_before_deploy"] = live
        for t in live:
            self.teardown(t)
        info["exclusive"] = self.assert_exclusive(None)
        if TOPOLOGIES[topology]["hicache"]:
            info["l3_cleanup_before_deploy"] = self.l3_cleanup(ns)
        sha = self.apply_cell(cell)
        self.topology = topology  # from here a failure tears it down (live_dgds sees it)
        boot_s = self.boot_watchdog(topology, run_dir / "boot")
        self.wait_model(topology)
        gi = self.gpu_identity(topology)
        if not gi["uniqueness"]["ok"]:
            raise StepError(f"GPU uniqueness failed at deploy: {gi['uniqueness']}")
        facts = self.boot_facts(topology)
        if TOPOLOGIES[topology]["pd"]:
            missing = sorted(n for n, f in facts.items() if not f["libfabric_backend"])
            if missing:
                raise StepError(f"PD transport: NIXL LIBFABRIC backend not instantiated on {missing}")
        warm = self.warm_up(topology)
        info.update(action="deploy", dgd_sha256=sha, boot_s=round(boot_s, 1), warm_up=warm, gpu_identity=gi,
                    boot_facts=facts)
        self.deploy_info[topology] = info
        return info

    # ---------------------------------------------------------------- reset + T-RTR-2
    def engine_route(self, pod: Pod, route: str) -> dict[str, Any]:
        """POST /engine/<route> until success (busy while requests are pending); StepError after 60 attempts."""
        url = f"http://{pod.ip}:{SYSTEM_PORT}/engine/{route}"
        txt, st = "", 0
        for attempt in range(1, 61):
            try:
                st, raw = http_req("POST", url, {}, 120)
                txt = raw.decode("utf-8", "replace")[:300]
            except OSError as exc:
                st, txt = 0, str(exc)[:300]
            ok = False
            if st == 200:
                try:
                    doc = json.loads(txt)
                    ok = isinstance(doc, dict) and (doc.get("success") is True or doc.get("status") in ("ok", "success"))
                except json.JSONDecodeError:
                    ok = False
            if ok:
                return {"pod": pod.name, "route": route, "attempts": attempt, "http": st}
            pause(1)
        raise StepError(f"{route} failed on {pod.name}: {st} {txt}")

    def wait_idle(self, pod: Pod, timeout: float = 180) -> None:
        """Wait until the engine has no running or queued request; StepError otherwise."""
        for _ in range(int(timeout // 2)):
            m = scrape(f"http://{pod.ip}:{SYSTEM_PORT}/metrics")
            if m and series_sum(m, "sglang:num_running_reqs") + series_sum(m, "sglang:num_queue_reqs") == 0:
                return
            pause(2)
        raise StepError(f"{pod.name} not idle after {timeout}s")

    def hicache_pods(self, topology: str, eng: list[Pod]) -> list[Pod]:
        """Engines that run HiCache in this topology (agg workers, or the PD prefill)."""
        t = TOPOLOGIES[topology]
        if not t["hicache"]:
            return []
        return [p for p in eng if p.role == ("prefill" if t["pd"] else "worker")]

    def reset_all(self, topology: str, eng: list[Pod]) -> list[dict[str, Any]]:
        """flush_cache on every engine (+ clear_hicache_storage on HiCache engines)."""
        hic = {p.name for p in self.hicache_pods(topology, eng)}
        out = []
        for p in eng:
            self.wait_idle(p)
            out.append(self.engine_route(p, "flush_cache"))
            if p.name in hic:
                out.append(self.engine_route(p, "clear_hicache_storage"))
        return out

    def l3_files(self, topology: str, eng: list[Pod]) -> dict[str, Any]:
        """Files and bytes under /kvcache/l3 of every HiCache engine (its own subPath; the namespace directory was
        emptied before the deploy, so no other pod directory can exist)."""
        ns = TOPOLOGIES[topology]["namespace"]
        out = {}
        for p in self.hicache_pods(topology, eng):
            raw = k_exec(ns, p.name, "find /kvcache/l3 -type f | wc -l; du -sb /kvcache/l3 | cut -f1").split()
            out[p.name] = {"files": int(raw[0]) if raw else None, "bytes": int(raw[1]) if len(raw) > 1 else None}
        return out

    def frontend_uid(self, topology: str) -> str | None:
        """UID of the live Frontend pod."""
        fe = [p for p in pods_of(TOPOLOGIES[topology]["namespace"]) if p.role == "frontend" and not p.terminating]
        return fe[0].uid if fe else None

    def reset_and_verify(self, topology: str, run_key: str, run_dir: Path) -> dict[str, Any]:
        """Reset and verify T-RTR-2 (see module doc); the record is always written to run_dir/reset.json.

        Raises:
            StepError: When the verification fails (the run is invalid; the caller tears the topology down).
        """
        doc: dict[str, Any] = {"frontend_uid_before": self.frontend_uid(topology), "ok": False}
        try:
            return self._reset_and_verify(topology, run_key, doc)
        finally:
            doc["frontend_uid_after"] = self.frontend_uid(topology)
            run_dir.mkdir(parents=True, exist_ok=True)
            (run_dir / "reset.json").write_text(json.dumps(doc, indent=1, default=str))

    def _reset_and_verify(self, topology: str, run_key: str, doc: dict[str, Any]) -> dict[str, Any]:
        eng, _, roles = self.targets(topology)
        n = sum(1 for p in eng if p.role in ("worker", "prefill"))
        url = self.frontend_url(topology)
        mode = self.current_mode(topology) or ""
        canaries = [canary_tokens(f"canary-{self.plan.run_id}-{run_key}-{self.launch}-{i}", CANARY_TOKENS)
                    for i in range(n)]
        doc.update({"canaries": n, "canary_tokens": CANARY_TOKENS, "router_mode": mode,
                    "cell": CELLS_BY_TOPOLOGY_MODE.get((topology, mode), "")})
        first = [stream_completion(url, c, 2) for c in canaries]
        doc["pre_reset_canary_ok"] = sum(1 for r in first if r["ok"])
        # Positive control (v3): the same canaries in full passes BEFORE the reset until a pass hits on the engine
        # counters (the hybrid engine hits from the 3rd send, see CONTROL_MAX_SENDS); then a zero after the reset,
        # on the same owners under rr, means the reset worked.
        owners = [t for t, r in roles.items() if r in ("worker", "prefill")]
        snaps = [self.snapshot(topology)]
        per_pass: list[float] = []

        def pass_hit(_recs: list[dict[str, Any]]) -> bool:
            pause(2)
            snaps.append(self.snapshot(topology))
            per_pass.append(sum(sum(by_label_delta(snaps[-2].get(t, {}), snaps[-1].get(t, {}),
                                                   "sglang:cached_tokens_total", "cache_source").values())
                                for t in owners))
            return per_pass[-1] >= 0.5 * n * CANARY_TOKENS

        control = positive_control(lambda c: stream_completion(url, c, 2), canaries, first, pass_hit)
        doc["positive_control"] = {**control, "engine_cached_tokens_per_pass": per_pass}
        doc["positive_control_cached_tokens"] = per_pass[-1] if per_pass else 0.0
        doc["positive_control_ok_requests"] = control["ok_requests"]
        pause(3)  # write-through to host and storage
        doc["reset1"] = self.reset_all(topology, eng)
        doc["l3_after_reset1"] = self.l3_files(topology, eng)
        snap_a = self.snapshot(topology)
        repeat = [stream_completion(url, c, 2) for c in canaries]
        pause(2)
        snap_b = self.snapshot(topology)
        cached_delta = sum(sum(by_label_delta(snap_a.get(t, {}), snap_b.get(t, {}), "sglang:cached_tokens_total",
                                              "cache_source").values()) for t in roles)
        fe = frontend_kpis(snap_a.get("frontend", {}), snap_b.get("frontend", {}))
        predicted = [r["kv_hit_rate"] for r in repeat if r.get("kv_hit_rate") is not None]
        on_hit_owner = sum(1 for r, o in zip(repeat, control["hit_owner"], strict=True)
                           if o is not None and owner_id(r) == o)
        doc.update({
            "repeat_ok": sum(1 for r in repeat if r["ok"]),
            "repeat_usage_cached_tokens": [r["cached_tokens"] for r in repeat],
            "repeat_engine_cached_tokens_delta": cached_delta,
            "repeat_router_predicted_hit": predicted,
            "repeat_router_hist_sum_delta": fe["dynamo_component_router_kv_hit_rate_sum"],
            "repeat_on_hit_owner": on_hit_owner,
        })
        checks = {
            "pre_reset_canaries_ok": doc["pre_reset_canary_ok"] == n and control["ok_requests"] == control["requests"],
            "positive_control_hit": bool(control["hit"]),
            # rr: the repeat must land on the engines that hit, or its zero proves nothing (kv: the router decides).
            "repeat_on_hit_owner_rr": mode != "round-robin" or on_hit_owner == n,
            "l3_zero_after_reset": all(v["files"] == 0 for v in doc["l3_after_reset1"].values()),
            "repeat_requests_ok": doc["repeat_ok"] == n,
            "repeat_cached_zero": all(c == 0 for c in doc["repeat_usage_cached_tokens"]) and cached_delta == 0,
            "router_predicted_zero": all(p == 0 for p in predicted) and fe["dynamo_component_router_kv_hit_rate_sum"] == 0,
        }
        if not checks["router_predicted_zero"]:
            # Pre-registered T-RTR-2 fallback: recreate the Frontend pod (fresh router index), then re-verify once.
            ns = TOPOLOGIES[topology]["namespace"]
            fe_pods = [p for p in pods_of(ns) if p.role == "frontend"]
            for p in fe_pods:
                kubectl("delete", "pod", "-n", ns, p.name, "--wait=false")
            old = {q.uid for q in fe_pods}
            for _ in range(120):
                if any(p.ready and p.uid not in old for p in pods_of(ns) if p.role == "frontend"):
                    break
                pause(5)
            else:
                raise StepError("frontend recreate (T-RTR-2 fallback) not Ready after 600s")
            self.wait_model(topology)
            pause(15)
            self.reset_all(topology, eng)
            sa = self.snapshot(topology)
            again = [stream_completion(url, c, 2) for c in canaries]
            pause(2)
            sb = self.snapshot(topology)
            fe2 = frontend_kpis(sa.get("frontend", {}), sb.get("frontend", {}))
            pr2 = [r["kv_hit_rate"] for r in again if r.get("kv_hit_rate") is not None]
            doc["router_fallback"] = {"action": "frontend-recreate", "predicted": pr2,
                                      "usage_cached": [r["cached_tokens"] for r in again]}
            checks["router_predicted_zero"] = all(p == 0 for p in pr2) and fe2["dynamo_component_router_kv_hit_rate_sum"] == 0
            checks["repeat_cached_zero"] = checks["repeat_cached_zero"] and all(r["cached_tokens"] == 0 for r in again)
        doc["reset2"] = self.reset_all(topology, eng)
        doc["l3_after_reset2"] = self.l3_files(topology, eng)
        checks["l3_zero_final"] = all(v["files"] == 0 for v in doc["l3_after_reset2"].values())
        final = self.snapshot(topology)
        gauges = {}
        for t in roles:
            gauges[t] = {g: series_sum(final.get(t, {}), g) for g in (
                "sglang:kv_evictable_tokens", "sglang:kv_used_tokens", "sglang:mamba_evictable_tokens",
                "sglang:hicache_host_used_tokens", "sglang:hicache_host_total_tokens", "sglang:num_running_reqs",
                "sglang:num_queue_reqs")}
        doc["gauges_after_final_reset_reported"] = gauges
        doc["checks"] = checks
        doc["ok"] = all(checks.values())
        if not doc["ok"]:
            raise StepError(f"T-RTR-2 reset verification failed: {[k for k, v in checks.items() if not v]}")
        return doc

    def current_mode(self, topology: str) -> str | None:
        """DYN_ROUTER_MODE of the live Frontend."""
        fe = [p for p in pods_of(TOPOLOGIES[topology]["namespace"]) if p.role == "frontend" and not p.terminating]
        return fe[0].router_mode if fe else None

    # ---------------------------------------------------------------- node-level counters
    def node_counters(self, topology: str) -> dict[str, Any]:
        """eth0 of every engine, md/NVMe diskstats and meminfo of the node (host-wide /proc), EFA (best effort)."""
        ns = TOPOLOGIES[topology]["namespace"]
        eng, _, _ = self.targets(topology)
        out: dict[str, Any] = {"at": time.time()}
        out["eth0"] = {k: parse_net_dev(v) for k, v in self.parallel_exec(ns, eng, "cat /proc/net/dev").items()}
        out["diskstats"] = parse_diskstats_all(k_exec(ns, eng[0].name, "cat /proc/diskstats"))
        out["meminfo_kb"] = parse_meminfo(k_exec(ns, eng[0].name, "cat /proc/meminfo"))
        if TOPOLOGIES[topology]["pd"]:
            pf = next(p for p in eng if p.role == "prefill")
            raw = k_exec(ns, pf.name, "for f in /sys/class/infiniband/*/ports/*/hw_counters/* "
                         "/sys/class/infiniband/*/hw_counters/*; do [ -f \"$f\" ] && echo \"$f $(cat \"$f\")\"; done; "
                         "true")
            efa = parse_counter_lines(raw)
            out["efa"] = efa if efa else None
            out["efa_status"] = "sysfs hw_counters" if efa else "unavailable: no hw_counters in sysfs on this node"
        return out

    # ---------------------------------------------------------------- one run
    def run_step(self, st: Step) -> dict[str, Any]:
        """One measured run of a plan entry (``run``, or ``take`` = a run recorded by a capture Job)."""
        cell, rep = st.cell, st.rep
        topology = CELLS[cell][0]
        prefix = f"{self.prefix}/{cell}/rep{rep}"
        if self.s3.exists_key(f"{prefix}/run.json"):
            log("skip (done in S3)", key=st.key)
            done: dict[str, Any] = {"status": "skipped-done", "key": st.key}
            if st.kind == "take":  # select_takes and the retake rule judge it from its run.json
                local = self.work / "done" / cell / f"rep{rep}" / "run.json"
                self.s3.get(self.s3.uri(f"{prefix}/run.json"), local)
                done.update(take_record_entry(json.loads(local.read_text() or "{}")))
            return done
        if self.s3.exists_prefix(f"{prefix}/"):
            raise StepError(f"{self.s3.uri(prefix)}/ exists without run.json (partial); append-only: not reused")
        if st.kind == "take":
            media = f"takes/{self.plan.run_id}/{take_id(self.plan.run_id, cell, rep)}/"
            if self.s3.exists_prefix(media):
                raise StepError(f"{self.s3.uri(media)} already holds media while {prefix}/run.json is absent; "
                                "append-only: never overwritten (use a new rep)")
        lock_line = self.plan.calibrate_line
        if lock_line is not None and st.line > lock_line and not self.slo_locked:
            raise StepError(f"blocked: run {st.key} needs the SLO lock written by calibrate-slo (line {lock_line})")
        need = run_estimate_s(self.plan.estimate, self.trace_span_s, self.topology != topology)
        self.guard(need + (CAPTURE_LEAD_S if st.kind == "take" else 0), f"{st.kind} {st.key}")
        run_dir = self.work / "runs" / cell / f"rep{rep}"
        if run_dir.exists():
            shutil.rmtree(run_dir)
        run_dir.mkdir(parents=True)
        try:
            return self._run(st, run_dir, prefix)
        except StepError:
            # Before the claim the results prefix is untouched (a relaunch may retry); keep the evidence apart.
            if run_dir.exists() and not (run_dir / "claim.json").exists():
                self.s3.put_run_dir(run_dir, f"{self.prefix}/_failed/{cell}/rep{rep}/{self.launch}", marker="none")
            raise

    def start_aiperf(self, cmd: list[str], log_fh: Any, cwd: Path) -> Any:
        """Start aiperf (Popen; the offline tests replace it). The driver's one Popen site: aiperf_cmd's list argv
        (constants, the Frontend URL, paths under the absolute WORK), the executable resolved by resolve_argv."""
        # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-audit, dangerous-subprocess-use-audit -- aiperf_cmd list argv (constants and checked values), resolved executable
        return subprocess.Popen(resolve_argv(cmd), stdout=log_fh, stderr=subprocess.STDOUT,  # nosec B603 -- list argv, no shell
                                cwd=str(cwd), env=self.aiperf_env())

    def worker_ids(self, art: Path, run_dir: Path, topology: str) -> dict[str, Any]:
        """Per-request worker ids from the aiperf raw export -> worker-ids.jsonl; the raw files (prompts) are
        deleted before the upload."""
        # aiperf 0.13.0 merges the per-processor raw_records/*.jsonl into profile_export_raw.jsonl at the end
        # (verified on a local mock, raw/44): read the merged file when present, never both (no double count).
        merged = sorted(art.glob("*_raw.jsonl"))
        raw_files = merged or sorted(art.glob("raw_records/*.jsonl"))
        recs: list[dict[str, Any]] = []
        raw_bytes = 0
        for f in raw_files:
            raw_bytes += f.stat().st_size
            with open(f, encoding="utf-8") as fh:
                recs += extract_worker_ids(fh)
        with open(run_dir / "worker-ids.jsonl", "w", encoding="utf-8") as fh:
            for r in recs:
                fh.write(json.dumps(r) + "\n")
        id_to_pod = {i: f["pod"] for f in self.boot_facts(topology).values() for i in f["worker_ids"]}
        summary = worker_id_summary(recs, id_to_pod)
        summary["raw_export"] = {"files": len(raw_files), "bytes": raw_bytes, "uploaded": False,
                                 "reason": "request payloads (synthetic prompts) and size; worker-ids.jsonl kept"}
        for f in raw_files:
            f.unlink()
        if (art / "raw_records").is_dir():
            shutil.rmtree(art / "raw_records")
        return summary

    def _run(self, st: Step, run_dir: Path, prefix: str) -> dict[str, Any]:
        """Body of run_step (deploy/toggle, reset + verify, evidence, aiperf, KPIs, upload)."""
        cell, rep = st.cell, st.rep
        topology, mode = CELLS[cell]
        ns = TOPOLOGIES[topology]["namespace"]
        rec: dict[str, Any] = {"run_id": self.plan.run_id, "cell": cell, "rep": rep, "label": st.label,
                               "topology": topology, "namespace": ns, "router_mode": mode, "dgd": DGD_NAME,
                               "plan_sha256": self.plan.source_sha256, "driver": DRIVER_VERSION,
                               "hard_stop": self.plan.hard_stop,
                               "w2": {**self.plan.w2, "K": self.trace_rows, "span_s": self.trace_span_s},
                               "slo_lock": self.slo_lock, "started_at": utcnow()}
        rec["deploy"] = self.ensure(cell, run_dir)
        rec["reset"] = self.reset_and_verify(topology, st.key, run_dir)
        _, _, roles = self.targets(topology)
        rec["evidence"] = {"gpu_identity": self.gpu_identity(topology), "boot_facts": self.boot_facts(topology)}
        pods_before = [p.as_dict() for p in pods_of(ns)]
        node_pre = self.node_counters(topology)
        pre = self.snapshot(topology)
        seed = 42 + rep
        art = run_dir / "aiperf"
        cmd = aiperf_cmd(self.frontend_url(topology), MODEL, self.trace, art, seed)
        rec["aiperf"] = {"cmd": cmd, "seed": seed, "requirement": "aiperf==0.13.0"}
        take = self.start_capture(st) if st.kind == "take" else None
        if take is not None and take["capture"]["status"] == "recording":
            self.lead_watch(take["capture"], st)  # the lead: the recording starts (FatalError on a blocked pod)
        claim = run_dir / "claim.json"
        claim.write_text(json.dumps({"claimed_at": utcnow(), "launch": self.launch, "driver": DRIVER_VERSION}))
        self.s3.put(claim, f"{prefix}/claim.json")
        stop = threading.Event()
        mon = threading.Thread(target=self.monitor, args=(topology, run_dir / "timeline.jsonl", stop), daemon=True)
        t0 = time.time()
        rec["aiperf_started_at"] = utcnow()
        mon.start()
        cut = False
        stopped: HardStop | None = None
        with open(run_dir / "aiperf.log", "w", encoding="utf-8") as fh:
            proc = self.start_aiperf(cmd, fh, run_dir)
            try:
                rc = proc.wait(timeout=max(60.0, self.hard_stop_epoch - time.time()))
            except (subprocess.TimeoutExpired, HardStop) as exc:
                stopped = exc if isinstance(exc, HardStop) else None
                proc.terminate()
                try:
                    rc = proc.wait(timeout=120)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    rc = proc.wait()
                cut = True
        stop.set()
        mon.join(timeout=60)
        rec["aiperf"]["rc"] = rc
        rec["aiperf_ended_at"] = utcnow()
        rec["aiperf_wall_s"] = round(time.time() - t0, 1)
        if take is not None:
            take["capture"].update(aiperf_start_ms=round(t0 * 1000), replay_end_ms=round(time.time() * 1000))
        pause(5)  # engine counters settle (prefill transfer histograms observe after completion)
        post = self.snapshot(topology)
        node_post = self.node_counters(topology)
        pods_after = [p.as_dict() for p in pods_of(ns)]
        (run_dir / "metrics-pre.json").write_text(json.dumps(pre))
        (run_dir / "metrics-post.json").write_text(json.dumps(post))
        (run_dir / "node-counters.json").write_text(json.dumps({"pre": node_pre, "post": node_post}))
        rec["pods"] = pods_before
        rec["restarted"] = restarted_pods(pods_before, pods_after)
        router = self.worker_ids(art, run_dir, topology)
        rec["kpis"] = self.kpis(topology, pre=pre, post=post, roles=roles, node_pre=node_pre, node_post=node_post,
                                run_dir=run_dir, router=router, facts=rec["evidence"]["boot_facts"])
        extra = []
        if not rec["evidence"]["gpu_identity"]["uniqueness"]["ok"]:
            extra.append(f"GPU uniqueness failed: {rec['evidence']['gpu_identity']['uniqueness']}")
        if TOPOLOGIES[topology]["pd"]:
            missing = sorted(n for n, f in rec["evidence"]["boot_facts"].items() if not f["libfabric_backend"])
            if missing:
                extra.append(f"PD transport not LIBFABRIC on {missing}")
        rec["validity"] = self.run_validity(rec, run_dir, cut, extra)
        rec["slo_goodput"] = self.goodput(run_dir, st)
        rec["prometheus"] = self.prom_metrics(ns, run_dir)
        if take is not None:  # after the measurements; never changes validity (spec section 8)
            stopped = self.finish_capture(take["capture"], cut=cut) or stopped
            take["capture"]["sync"] = take_sync(take["capture"], first_request_ms(art / "profile_export.jsonl"))
            rec["take"] = take
        rec["result_of_record"] = st.kind == "run"  # spec section 8: a take never is (TAKE_NOTE)
        rec["ended_at"] = utcnow()
        (run_dir / "run.json").write_text(json.dumps(rec, indent=1, default=str))
        self.s3.put_run_dir(run_dir, prefix, skip=frozenset({"claim.json"}))
        log("run done", key=st.key, valid=rec["validity"]["valid"], reasons=rec["validity"]["reasons"],
            goodput=(rec["slo_goodput"] or {}).get("goodput"), engine_hit=rec["kpis"]["engine"]["total"]["engine_hit_rate"],
            take_capture=None if take is None else take["capture"]["status"])
        if stopped is not None:
            raise HardStop(f"{st.kind} {st.key} cut: {stopped}")
        if cut:
            raise HardStop(f"{st.kind} {st.key} cut at the hard stop")
        out: dict[str, Any] = {"status": "done", "key": st.key, "valid": rec["validity"]["valid"],
                               "reasons": rec["validity"]["reasons"]}
        if take is not None:
            cap = take["capture"]
            out.update(take_id=take["take_id"], take_capture=cap["status"], webm_uri=cap["webm_uri"],
                       replay_start_ms=cap["replay_start_ms"], replay_end_ms=cap.get("replay_end_ms"),
                       sync_ms=cap["sync"]["sync_ms"], sync_degraded=cap["sync"]["degraded"])
        return out

    # ---------------------------------------------------------------- v4 takes
    def start_capture(self, st: Step) -> dict[str, Any]:
        """Create the take's capture Job (replay start = now + CAPTURE_LEAD_S); returns the run.json ``take`` block.

        A failure to create the Job is recorded (capture status failed) and never fails the run.
        """
        tid = take_id(self.plan.run_id, st.cell, st.rep)
        window = take_window(time.time() + CAPTURE_LEAD_S, self.trace_span_s, self.plan.estimate["drain_s"])
        media = self.s3.uri(f"takes/{self.plan.run_id}/{tid}/{tid}")
        cap: dict[str, Any] = {
            "status": "recording", "reason": "", "job": f"{TAKES_NS}/demo-capture-{tid}",
            "namespace": TOPOLOGIES[CELLS[st.cell][0]]["namespace"], "dashboard_uid": DASHBOARD_UID,
            "webm_uri": media + ".webm", "frames_uri": media + "-frames-*.tar", "meta_uri": media + "-capture.json",
            "from_ms": window["from_ms"], "to_ms": window["to_ms"], "duration_s": window["duration_s"],
            "lead_s": window["lead_s"], "replay_start_ms": window["from_ms"], "image": CAPTURE_IMAGE,
            "created_ms": round(time.time() * 1000)}
        try:
            job = capture_job(run_id=self.plan.run_id, cell=st.cell, rep=st.rep, window=window,
                              hard_stop_epoch=self.hard_stop_epoch, now=time.time())
            cap["active_deadline_s"] = job["spec"]["activeDeadlineSeconds"]
            p = self.create_job(job, f"demo.kubecon/take-id={tid},q38-matrix/run-id={self.plan.run_id}")
            if p.returncode != 0:
                cap.update(status="failed", reason=f"capture Job create exited {p.returncode}: {p.stderr.strip()[:300]}")
        except (StepError, OSError) as exc:
            cap.update(status="failed", reason=f"capture Job not created: {str(exc)[:300]}")
        log("capture", take_id=tid, status=cap["status"], reason=cap["reason"], from_ms=cap["from_ms"],
            to_ms=cap["to_ms"], namespace=cap["namespace"])
        return {"take_id": tid, "result_of_record": False, "note": TAKE_NOTE, "capture": cap}

    def job_pods(self, name: str) -> dict[str, Any] | None:
        """The pods of a bench take Job (None when they cannot be listed)."""
        try:
            doc: dict[str, Any] = kjson("get", "pods", "-n", TAKES_NS, "-l", f"batch.kubernetes.io/job-name={name}")
        except (StepError, ValueError) as exc:
            log("job pods not listable", job=name, error=str(exc)[:200])
            return None
        return doc

    def delete_jobs(self, selector: str, *, wait: bool) -> subprocess.CompletedProcess[str]:
        """Delete bench take Jobs by label (never by name), their pods with them when ``wait``."""
        extra = ["--cascade=foreground", "--wait=true", "--timeout=180s"] if wait else ["--wait=false"]
        return kubectl("delete", "job", "-n", TAKES_NS, "-l", selector, "--ignore-not-found", *extra,
                       timeout=240, check=False)

    def create_job(self, job: dict[str, Any], selector: str) -> subprocess.CompletedProcess[str]:
        """kubectl create a take Job; a Job of the same name left by an earlier launch (AlreadyExists: same take id
        and run id, its run.json and media absent, no other driver active) is deleted with its pod and the create
        retried once."""
        body = json.dumps(job)
        p = kubectl("create", "-f", "-", stdin=body, check=False)
        if p.returncode != 0 and "AlreadyExists" in p.stderr:
            d = self.delete_jobs(selector, wait=True)
            log("stale take Job replaced", job=job["metadata"]["name"], selector=selector, delete_rc=d.returncode,
                delete_err=d.stderr.strip()[:200])
            p = kubectl("create", "-f", "-", stdin=body, check=False)
        return p

    def lead_watch(self, cap: dict[str, Any], st: Step) -> None:
        """Wait out the capture lead while watching the capture pod, then record its state at the replay start.

        A blocker (POD_BLOCKERS: Secret demo-grafana-auth or an image missing) seen on two consecutive polls is a
        configuration error that every take would hit: the Job is deleted and FatalError ends the plan before the
        replay (torn down, exit 1; fix and relaunch). Polls are bounded by count, not by the clock.

        Raises:
            FatalError: The capture pod is blocked.
        """
        name = cap["job"].split("/", 1)[1]
        start = cap["replay_start_ms"] / 1000
        seen: str | None = None
        for _ in range(max(1, math.ceil(cap["lead_s"] / JOB_POLL_S))):
            pods = self.job_pods(name)
            blocker = pod_blocker(pods) if pods is not None else None
            if blocker is not None and blocker == seen:
                d = self.delete_jobs(f"demo.kubecon/take-id={take_id(self.plan.run_id, st.cell, st.rep)}", wait=False)
                raise FatalError(f"capture {cap['job']} blocked before the replay ({blocker}); Job deleted (rc "
                                 f"{d.returncode}). Fix the prerequisite (DRIVER-V4-AMENDMENT launch steps) and relaunch")
            seen = blocker
            left = start - time.time()
            if left <= 0:
                break
            pause(min(float(JOB_POLL_S), left))
        pause(max(0.0, start - time.time()))
        pods = self.job_pods(name)
        cap["pod_at_replay_start"] = pod_state(pods) if pods is not None else "unknown"

    def wait_job(self, name: str, timeout_s: float) -> str:
        """Poll a bench Job until Complete/Failed: complete | failed | timeout | blocked (<reason>) (HardStop on
        SIGTERM). A failed or timed-out poll (API error, kubectl timeout, unparsable output) is retried until the
        deadline; a pod blocker (POD_BLOCKERS) seen on two consecutive polls ends the wait (the caller deletes it)."""
        deadline = time.time() + max(0.0, timeout_s)
        seen: str | None = None
        while True:
            try:
                p = kubectl("get", "job", name, "-n", TAKES_NS, "-o", "json", check=False)
                state = job_state(json.loads(p.stdout)) if p.returncode == 0 else "running"
            except (StepError, ValueError) as exc:
                log("job poll failed", job=name, error=str(exc)[:200])
                state = "running"
            if state != "running":
                return state
            pods = self.job_pods(name)
            blocker = pod_blocker(pods) if pods is not None else None
            if blocker is not None and blocker == seen:
                return f"blocked ({blocker})"
            seen = blocker
            if time.time() >= deadline:
                return "timeout"
            pause(JOB_POLL_S)

    def finish_capture(self, cap: dict[str, Any], *, cut: bool) -> HardStop | None:
        """Wait for the capture Job and check its webm in S3; updates ``cap`` (ok | failed | cut).

        Returns:
            The HardStop that interrupted the wait (the caller raises it after run.json is uploaded), else None.
        """
        if cap["status"] != "recording":
            return None
        if cut:
            cap.update(status="cut", reason="the run was cut by the hard stop; the capture Job ends at its own deadline")
            return None
        name = cap["job"].split("/", 1)[1]
        waited = time.time() - cap["created_ms"] / 1000
        try:
            state = self.wait_job(name, cap["active_deadline_s"] + 60 - waited)
        except HardStop as exc:
            cap.update(status="cut", reason=f"capture wait interrupted: {exc}")
            return exc
        reason = ""
        if state.startswith("blocked"):
            self.delete_jobs(f"batch.kubernetes.io/job-name={name}", wait=False)
        if state != "complete":
            reason = f"capture Job {cap['job']} {state} (kubectl logs -n {TAKES_NS} job/{name})"
        else:
            try:
                if not self.s3.exists_key(s3_key(cap["webm_uri"])):
                    reason = f"capture Job complete but no webm at {cap['webm_uri']}"
            except StepError as exc:
                reason = f"capture Job complete but the webm check failed: {str(exc)[:200]}"
        if not reason:
            cap["media"] = self.capture_meta(cap)
            if cap["media"].get("webm_bytes") == 0:
                reason = f"capture Job complete but the webm is empty ({cap['webm_uri']})"
        cap.update(status="failed" if reason else "ok", reason=reason, finished_at=utcnow())
        log("capture finished", job=cap["job"], status=cap["status"], reason=cap["reason"])
        return None

    def capture_meta(self, cap: dict[str, Any]) -> dict[str, Any]:
        """The capture pod's <take_id>-capture.json (video start/end, sizes, frame chunks, data check), best effort:
        {"missing": reason} when it cannot be read (the webm check already passed)."""
        local = self.work / "takes" / Path(s3_key(cap["meta_uri"])).name
        try:
            self.s3.get(cap["meta_uri"], local)
            doc = json.loads(local.read_text())
        except (StepError, ValueError, OSError) as exc:
            return {"missing": str(exc)[:200]}
        keep = ("video_t0_ms", "panel_rendered_ms", "video_end_ms", "webm_bytes", "frames", "frame_chunks",
                "frame_chunks_failed", "frames_bytes", "data_check")
        return {k: doc.get(k) for k in keep if k in doc}

    def retake_skip(self, st: Step, done: list[dict[str, Any]]) -> dict[str, Any] | None:
        """Gate of a ``retake_of`` take: skipped-not-needed when the named take is usable (or was itself not
        needed), skipped-no-time when less than min_left_s remain before the hard stop, else None (run it)."""
        ref = st.opts["retake_of"]
        prev = next((e for e in reversed(done) if e.get("kind") == "take"
                     and f"{e.get('cell')}/rep{e.get('rep')}" == ref), None)
        if prev is not None and (take_usable(prev) or prev.get("status") == "skipped-not-needed"):
            return {"status": "skipped-not-needed", "retake_of": ref, "reason": f"{ref} is usable or was not needed"}
        left = self.hard_stop_epoch - time.time()
        if left < int(st.opts["min_left_s"]):
            return {"status": "skipped-no-time", "retake_of": ref,
                    "reason": f"{int(left)}s left before {self.plan.hard_stop} < min_left_s {st.opts['min_left_s']}"}
        return None

    def compose_step(self, st: Step, done: list[dict[str, Any]]) -> dict[str, Any]:
        """The side-by-side clips from the latest usable S0 and S7 takes (append-only, compose.json last).

        Raises:
            StepError: A partial compose prefix, no usable take of a cell, a missing input webm, a Job that cannot
                be created, fails or times out, or a complete Job whose outputs are missing.
        """
        prefix = f"takes/{self.plan.run_id}/compose"
        marker = f"{prefix}/compose.json"
        if self.s3.exists_key(marker):
            log("skip compose (done in S3)", marker=self.s3.uri(marker))
            return {"status": "skipped-done", "marker": self.s3.uri(marker)}
        if self.s3.exists_prefix(f"{prefix}/"):
            raise StepError(f"{self.s3.uri(prefix)}/ exists without compose.json (partial); append-only: not reused")
        spec = compose_spec(self.plan.run_id, select_takes(done),
                            target_s=int(st.opts.get("target_s", COMPOSE_TARGET_S)))
        missing = [i["webm_uri"] for i in spec["inputs"] if not self.s3.exists_key(s3_key(i["webm_uri"]))]
        if missing:
            raise StepError(f"compose: input webm missing in S3: {missing}")
        self.guard(COMPOSE_ESTIMATE_S, "compose")
        job = compose_job(spec, hard_stop_epoch=self.hard_stop_epoch, now=time.time())
        name = job["metadata"]["name"]
        selector = f"app.kubernetes.io/name=take-compose,q38-matrix/run-id={self.plan.run_id}"
        p = self.create_job(job, selector)
        if p.returncode != 0:
            raise StepError(f"compose Job create exited {p.returncode}: {p.stderr.strip()[:300]}")
        log("compose", job=f"{TAKES_NS}/{name}", inputs=[i["take_id"] for i in spec["inputs"]])
        state = self.wait_job(name, job["spec"]["activeDeadlineSeconds"] + 60)
        if state.startswith("blocked"):
            self.delete_jobs(selector, wait=False)
        if state != "complete":
            raise StepError(f"compose Job {TAKES_NS}/{name} {state} (kubectl logs -n {TAKES_NS} job/{name} --all-containers)")
        absent = [u for u in spec["outputs"].values() if not self.s3.exists_key(s3_key(u))]
        if absent:
            raise StepError(f"compose Job {TAKES_NS}/{name} complete but outputs missing: {absent}")
        self.put_json({"status": "ok", "run_id": self.plan.run_id, "inputs": spec["inputs"], "outputs": spec["outputs"],
                       "target_s": spec["target_s"], "job": f"{TAKES_NS}/{name}", "images": {
                           "fetch_upload": COMPOSE_AWS_IMAGE, "render": compose_ffmpeg_image()},
                       "result_of_record": False, "note": TAKE_NOTE, "driver": DRIVER_VERSION, "launch": self.launch,
                       "at": utcnow()},
                      marker)
        return {"status": "done", "inputs": [i["take_id"] for i in spec["inputs"]], "outputs": spec["outputs"]}

    def monitor(self, topology: str, path: Path, stop: threading.Event) -> None:
        """Every MONITOR_PERIOD_S: engine load gauges and pod restart counts (timeline.jsonl)."""
        ns = TOPOLOGIES[topology]["namespace"]
        gauges = ("sglang:num_running_reqs", "sglang:num_queue_reqs", "sglang:token_usage", "sglang:kv_used_tokens",
                  "sglang:hicache_host_used_tokens", "sglang:mamba_used_tokens", "sglang:num_retracted_reqs")
        while not stop.wait(MONITOR_PERIOD_S):
            try:
                snap = self.snapshot(topology)
                row = {"at": time.time(), "engines": {t: {g: series_sum(m, g) for g in gauges}
                                                      for t, m in snap.items() if t != "frontend"},
                       "restarts": {p.name: p.restarts for p in pods_of(ns)}}
                with open(path, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(row) + "\n")
            except (StepError, OSError, ValueError) as exc:
                log("monitor sample failed", error=str(exc)[:200])

    def kpis(self, topology: str, *, pre: dict[str, Any], post: dict[str, Any], roles: dict[str, str],
             node_pre: dict[str, Any], node_post: dict[str, Any], run_dir: Path, router: dict[str, Any] | None = None,
             facts: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
        """Engine, router, node, transport, storage-tier and occupancy KPIs of one run."""
        eng = engine_kpis(pre, post, roles)
        per = eng["per_engine"]
        out: dict[str, Any] = {"engine": eng, "frontend": frontend_kpis(pre.get("frontend", {}), post.get("frontend", {}))}
        tot = eng["total"]
        out["router_predicted_vs_engine"] = {
            "router_predicted_hit_mean_request_weighted": out["frontend"]["router_predicted_hit_mean"],
            "engine_hit_rate_token_weighted": tot["engine_hit_rate"],
            "note": "different weights (request vs token); per-request worker ids in worker-ids.jsonl"}
        out["router_requests"] = router
        out["engine_requests"] = {t.removeprefix("w:"): d.get("sglang:num_requests_total", 0.0) for t, d in per.items()}
        disk_pre, disk_post = node_pre.get("diskstats") or {}, node_post.get("diskstats") or {}
        eth0 = {k: dict_delta((node_pre.get("eth0") or {}).get(k), v) for k, v in (node_post.get("eth0") or {}).items()}
        out["node"] = {"diskstats_delta": {d: dict_delta(disk_pre.get(d), v) for d, v in disk_post.items()},
                       "meminfo_kb": {"pre": node_pre.get("meminfo_kb"), "post": node_post.get("meminfo_kb")},
                       "eth0_delta": eth0}
        if TOPOLOGIES[topology]["pd"]:
            out["pd_transport"] = {
                "per_engine_kv_transfer": {t.removeprefix("w:"): {k: d.get(k, 0.0) for k in KV_TRANSFER}
                                           for t, d in per.items()},
                "eth0_delta": eth0,
                "efa_delta": dict_delta(node_pre.get("efa"), node_post.get("efa")),
                "efa_status": node_post.get("efa_status"),
                "libfabric_backend": {n: f["libfabric_backend"] for n, f in (facts or {}).items()},
                "libfabric_lines": {n: f["libfabric_lines"] for n, f in (facts or {}).items()},
                "pd_pairs_requests": (router or {}).get("pd_pairs"),
                "kv_transfer_total_mb": tot.get("sglang:kv_transfer_total_mb_sum"),
                "kv_transfers": tot.get("sglang:kv_transfer_total_mb_count"),
                "failed_transfers": tot.get("sglang:num_transfer_failed_reqs_total", 0.0),
                "failed_bootstraps": tot.get("sglang:num_bootstrap_failed_reqs_total", 0.0),
                "note": "EFA byte counters unavailable on this node; claim set = kv_transfer_* per engine, eth0 "
                        "deltas, LIBFABRIC lines; P->D pairs are request counts (bytes per pair not exported)"}
        if TOPOLOGIES[topology]["hicache"]:
            md127 = out["node"]["diskstats_delta"].get("md127") or {}
            nvme_read = sum((v or {}).get("read_bytes", 0) for d, v in out["node"]["diskstats_delta"].items()
                            if d.startswith("nvme"))
            tiers = tot["engine_cached_tokens_by_source"]
            backup_kv = tot["by_label"].get("sglang:hicache_backup_tokens_total", {}).get("kv", 0.0)
            out["storage_tier"] = {
                "label": STORAGE_LABEL,
                "per_engine": {t.removeprefix("w:"): {"cached_by_source": d.get("sglang:cached_tokens_total", {}),
                                                      **{k: d.get(k, 0.0) for k in STORAGE_COUNTERS}}
                               for t, d in per.items() if d.get("role") in ("worker", "prefill")},
                "storage_cached_tokens": tiers.get("storage", 0.0),
                "md127_read_bytes_delta": md127.get("read_bytes"), "nvme_read_bytes_delta": nvme_read,
                "meminfo_kb": out["node"]["meminfo_kb"],
                "page_cache_proxy_I": l3_page_cache_proxy(md127.get("read_bytes"), tiers.get("storage", 0.0),
                                                          tot.get("sglang:hicache_backup_bytes_total", 0.0), backup_kv)}
        tl = run_dir / "timeline.jsonl"
        if tl.exists():
            rows = [json.loads(x) for x in tl.read_text().splitlines() if x.strip()]
            occ: dict[str, list[float]] = {}
            for r in rows:
                for t, g in r["engines"].items():
                    occ.setdefault(roles.get(t, "?"), []).append(g["sglang:num_running_reqs"])
            out["occupancy_mean_running_per_engine"] = {k: sum(v) / len(v) for k, v in occ.items() if v}
            out["max_retracted_gauge"] = max((g["sglang:num_retracted_reqs"] for r in rows for g in r["engines"].values()),
                                             default=None)
        return out

    def run_validity(self, rec: dict[str, Any], run_dir: Path, cut: bool, extra: list[str] | None = None) -> dict[str, Any]:
        """Validity from the aiperf export and the demo_bench rules (P-37 collapsed outputs, 0.5 %)."""
        from demo_bench.application.validity import count_collapsed_outputs, exceeds_error_limit

        export = run_dir / "aiperf" / "profile_export.jsonl"
        profiling = errored = collapsed = 0
        note = None
        if export.exists():
            rows = [json.loads(x) for x in export.read_text().splitlines() if x.strip()]
            prof = [r for r in rows if (r.get("metadata") or {}).get("benchmark_phase") == "profiling"]
            profiling = len(prof)
            errored = sum(1 for r in prof if r.get("error") is not None)
            try:
                collapsed = count_collapsed_outputs(export, self.trace).collapsed
            except ValueError as exc:
                note = f"collapsed-output pairing refused: {exc}"
                collapsed = profiling
        else:
            note = "no aiperf profile_export.jsonl"
        v = validity(aiperf_rc=rec["aiperf"]["rc"], trace_rows=self.trace_rows, profiling_rows=profiling,
                     errored_rows=errored, collapsed=collapsed, restarted=rec["restarted"],
                     reset_ok=rec["reset"]["ok"], cut_by_hard_stop=cut, exceeds=exceeds_error_limit,
                     extra_reasons=extra or [])
        if note:
            v["note"] = note
        pd = rec["kpis"].get("pd_transport")
        v["guards_reported"] = {"failed_transfers": pd["failed_transfers"] if pd else None,
                                "max_retracted_gauge": rec["kpis"].get("max_retracted_gauge")}
        return v

    def demo_bench(self, *args: str, timeout: float = 600) -> subprocess.CompletedProcess[str]:
        """python3 -m demo_bench from the installed wheel (PYTHONPATH first)."""
        env = {**os.environ, "PYTHONPATH": str(self.work / "pylib")}
        try:
            return run_argv([sys.executable, "-m", "demo_bench", *args], timeout=timeout, env=env, cwd=self.work)
        except subprocess.TimeoutExpired as exc:
            raise StepError(f"demo_bench {args[0]} timed out") from exc

    def slo_args(self) -> list[str]:
        """slo-goodput threshold flags from the current SLO (calibration grid included)."""
        s = self.slo
        itl = sorted({*SLO_GRID_ITL_MS, float(s["itl_ms"])})
        ttft = sorted({*SLO_GRID_TTFT_S, float(s["ttft_s"])})
        return ["--cold-prefill-tok-s", str(s["cold_prefill_tok_s"]), "--ttft-s", str(s["ttft_s"]),
                "--itl-ms", str(s["itl_ms"]), "--cold-factor", str(s["cold_factor"]),
                "--itl-grid", ",".join(f"{x:g}" for x in itl), "--ttft-grid", ",".join(f"{x:g}" for x in ttft),
                "--n-boot", "1000", "--seed", "0"]

    def goodput(self, run_dir: Path, st: Step) -> dict[str, Any] | None:
        """slo-goodput of the run (pilot: with the calibration acceptance band). Compared across cells only."""
        export = run_dir / "aiperf" / "profile_export.jsonl"
        if not export.exists() or self.slo["cold_prefill_tok_s"] == "probe":
            return None
        out = run_dir / "slo-goodput.json"
        extra = ["--accept-band", f"{DEFAULT_BAND[0]},{DEFAULT_BAND[1]}"] if st.label == "pilot" else []
        p = self.demo_bench("slo-goodput", "--profile-export", str(export), "--sidecar", str(self.sidecar),
                            *self.slo_args(), *extra, "--out", str(out))
        (run_dir / "slo-goodput.log").write_text(p.stderr[-20000:])
        if not out.exists():
            return {"error": f"slo-goodput exited {p.returncode}"}
        doc = json.loads(out.read_text())
        return {"goodput": doc.get("goodput"), "goodput_ci": doc.get("goodput_ci"), "attainment": doc.get("attainment"),
                "classes": doc.get("classes"), "slo": dict(self.slo), "slo_source": self.slo_source,
                "calibration": doc.get("calibration"), "rc": p.returncode,
                "reading": "SLO goodput is compared across cells only, never read as absolute (S0 ~ band by the SLO calibration)"}

    def prom_metrics(self, ns: str, run_dir: Path) -> dict[str, Any] | None:
        """demo-bench metrics (Prometheus, namespace-scoped) over the aiperf window; best effort."""
        export = run_dir / "aiperf" / "profile_export.jsonl"
        if not self.prom or not export.exists():
            return None
        out = run_dir / "prometheus-metrics.json"
        p = self.demo_bench("metrics", "--namespace", ns, "--profile-export", str(export), "--prometheus-url",
                            self.prom, "--out", str(out), timeout=180)
        return json.loads(out.read_text()) if out.exists() else {"error": f"rc {p.returncode}", "stderr": p.stderr[-500:]}

    # ---------------------------------------------------------------- other steps
    def probe_cold_prefill(self, st: Step) -> dict[str, Any]:
        """Cold prefill rate on the cell's deployment: N cold prompts of T tokens, max_tokens 1, reset before each."""
        key = f"{self.prefix}/_probe/cold-prefill-{st.cell}.json"
        local = self.work / "probe" / f"cold-prefill-{st.cell}.json"
        if self.s3.exists_key(key):
            self.s3.get(self.s3.uri(key), local)
            doc = json.loads(local.read_text())
        else:
            self.guard(self.plan.estimate["probe_s"] + (self.plan.estimate["boot_s"]
                       if self.topology != CELLS[st.cell][0] else 0), "probe-cold-prefill")
            n, tokens = int(st.opts.get("n", "3")), int(st.opts.get("tokens", "102400"))
            topology = CELLS[st.cell][0]
            local.parent.mkdir(parents=True, exist_ok=True)
            deploy = self.ensure(st.cell, local.parent)
            eng, _, _ = self.targets(topology)
            reps = []
            for i in range(n):
                self.reset_all(topology, eng)
                r = stream_completion(self.frontend_url(topology), canary_tokens(f"cold-{self.plan.run_id}-{i}", tokens), 1)
                if not r["ok"] or not r["ttft_s"] or r["cached_tokens"] != 0:
                    raise StepError(f"cold prefill probe request {i} invalid: ok={r['ok']} cached={r['cached_tokens']}")
                reps.append({"prompt_tokens": r["prompt_tokens"], "ttft_s": r["ttft_s"],
                             "tok_per_s": r["prompt_tokens"] / r["ttft_s"]})
            self.reset_all(topology, eng)
            med = percentile([x["tok_per_s"] for x in reps], 0.5)
            doc = {"cell": st.cell, "tokens": tokens, "n": n, "reps": reps, "cold_prefill_tok_s_median": med,
                   "method": "Frontend /v1/completions stream, token-ID prompt, max_tokens 1, flush before each; "
                             "tok/s = prompt_tokens / TTFT (includes HTTP + tokenization), as the E1 gate perf",
                   "deploy": deploy, "at": utcnow()}
            local.write_text(json.dumps(doc, indent=1, default=str))
            self.s3.put(local, key)
        self.cold_probe = doc
        if self.slo["cold_prefill_tok_s"] == "probe":
            self.slo["cold_prefill_tok_s"] = round(doc["cold_prefill_tok_s_median"], 1)
            self.slo_source = "plan + cold-prefill probe"
        log("cold prefill probe", cell=st.cell, tok_per_s=doc["cold_prefill_tok_s_median"])
        return dict(doc)

    def compute_calibration(self, st: Step) -> dict[str, Any]:
        """SLO calibration rule on the pilot's aiperf export (demo_bench samples and attainment)."""
        from demo_bench.application.slo_goodput import SloThresholds, calibration, load_samples
        from demo_bench.application.trace import read_trace_jsonl

        cell, rep = st.opts["after"].split("/rep")
        export = self.work / "runs" / cell / f"rep{rep}" / "aiperf" / "profile_export.jsonl"
        if not export.exists():
            self.s3.get(self.s3.uri(f"{self.prefix}/{cell}/rep{rep}/aiperf/profile_export.jsonl"), export)
        band = DEFAULT_BAND
        if "band" in st.opts:
            lo, hi = (float(x) for x in st.opts["band"].split(","))
            band = (lo, hi)
        samples = load_samples(export, read_trace_jsonl(self.sidecar))

        def attainment_of(th: dict[str, Any]) -> float | None:
            t = SloThresholds(cold_prefill_tok_s=float(th["cold_prefill_tok_s"]), ttft_continuation_s=float(th["ttft_s"]),
                              itl_ms=float(th["itl_ms"]), cold_factor=float(th["cold_factor"]))
            a = calibration(samples, t, band=band)["attainment"]
            return None if a is None else float(a)

        return calibrate_rule(samples, {k: float(v) for k, v in self.slo.items()}, band, attainment_of)

    def calibrate_slo(self, st: Step) -> dict[str, Any]:
        """SLO calibration on the pilot named by after=<cell>/rep<k>, then the SLO lock (before any later run)."""
        key = f"{self.prefix}/_slo/slo-calibration.json"
        local = self.work / "slo" / "slo-calibration.json"
        if self.s3.exists_key(key):
            self.s3.get(self.s3.uri(key), local)
            data = local.read_bytes()
            doc = json.loads(data)
        else:
            doc = self.compute_calibration(st)
            doc.update({"pilot": st.opts["after"], "at": utcnow(), "plan_sha256": self.plan.source_sha256,
                        "status": "SLO of record for e4/e5/e6 through slo-lock.json (pinned by their plans)"})
            data = json.dumps(doc, indent=1).encode()
            local.parent.mkdir(parents=True, exist_ok=True)
            local.write_bytes(data)
            self.s3.put(local, key)
        self.slo = dict(doc["slo"])
        self.slo_source = "SLO calibration of " + st.opts["after"]
        self.slo_lock = self.write_slo_lock(key, data, doc)
        log("slo calibrated", slo=self.slo, in_band=doc.get("in_band"), adjusted=doc.get("adjusted"),
            rule_applied=doc.get("rule_applied"), slo_calibration_sha256=self.slo_lock["slo_calibration_sha256"])
        return dict(doc)

    def write_slo_lock(self, key: str, data: bytes, doc: dict[str, Any]) -> dict[str, Any]:
        """slo-lock.json next to the calibration: its sha256, the SLO and this plan's sha256 (before S7)."""
        lock_key = f"{self.prefix}/_slo/slo-lock.json"
        lock = {"plan_sha256": self.plan.source_sha256, "run_id": self.plan.run_id, "slo_calibration_key": key,
                "slo_calibration_sha256": hashlib.sha256(data).hexdigest(), "slo": doc["slo"],
                "rule_applied": doc.get("rule_applied"), "in_band": doc.get("in_band"), "adjusted": doc.get("adjusted"),
                "pilot": doc.get("pilot"), "driver": DRIVER_VERSION, "at": utcnow(),
                "written_before": [s.key for s in self.plan.steps if s.kind == "run"
                                   and s.line > (self.plan.calibrate_line or 0)]}
        if self.s3.exists_key(lock_key):
            local = self.work / "slo" / "slo-lock.json"
            self.s3.get(self.s3.uri(lock_key), local)
            old = json.loads(local.read_text())
            try:
                verify_slo_lock({"from": self.s3.uri(key), "lock": self.s3.uri(lock_key),
                                 "e3_plan_sha256": self.plan.source_sha256}, old, data)
            except ConfigError as exc:
                raise StepError(f"existing SLO lock does not match: {exc}") from exc
            lock = old
        else:
            self.put_json(lock, lock_key)
        self.slo_locked = True
        return {"lock": self.s3.uri(lock_key), "slo_calibration_sha256": lock["slo_calibration_sha256"],
                "slo": lock["slo"], "plan_sha256": lock["plan_sha256"]}

    # ---------------------------------------------------------------- main loop
    def after_failure(self, entry: dict[str, Any], reason: str) -> None:
        """Tear down every live q38 topology (never reuse a DGD after a failed or invalid run)."""
        torn = []
        for t in self.live_topologies():
            try:
                self.teardown(t)
            except StepError as exc:
                raise FatalError(f"teardown after failure of {t} failed: {exc}") from exc
            torn.append(t)
        self.topology = None
        entry["teardown_after_failure"] = {"torn_down": torn, "reason": reason[:300]}

    def cut_now(self, idx: int, summary: dict[str, Any]) -> bool:
        """Priority cut (cut_order), re-evaluated before every run on the time really left: True when the current
        run is among the runs the cut rule drops so that the rest fits (I). Nothing is cut in advance."""
        if not self.plan.cut_order:
            return False
        st = self.plan.steps[idx]
        rest = [s for s in self.plan.steps[idx:] if s.kind == "run"]
        drop = plan_cuts(rest, self.topology, self.plan.estimate, self.trace_span_s,
                         self.hard_stop_epoch - time.time(), cut_order=self.plan.cut_order)
        if drop:
            log("cut by priority (I)", run=st.key, would_drop=drop, cut_order=self.plan.cut_order)
        if st.key in drop:
            summary.setdefault("cut_by_priority_I", []).append(st.key)
            return True
        return False

    def execute(self) -> int:
        """Run every step in order. Exit codes: see the module doc."""
        summary: dict[str, Any] = {"run_id": self.plan.run_id, "launch": self.launch, "hard_stop": self.plan.hard_stop,
                                   "plan_sha256": self.plan.source_sha256, "on_failure": self.plan.on_failure,
                                   "driver": DRIVER_VERSION, "steps": []}
        rc = 0
        aborted: str | None = None
        Path(os.environ.get("HOME", str(self.work / "home"))).mkdir(parents=True, exist_ok=True)
        previous = signal.signal(signal.SIGTERM, self.on_sigterm)
        try:
            summary["demo_bench"] = self.install_demo_bench()
            summary["inputs"] = self.fetch_inputs()
            summary["preflight"] = self.preflight()
            for idx, st in enumerate(self.plan.steps):
                entry: dict[str, Any] = {"line": st.line, "kind": st.kind, "cell": st.cell, "rep": st.rep}
                if aborted:
                    entry["status"] = "blocked"
                    summary["steps"].append(entry)
                    continue
                if st.kind == "run" and self.cut_now(idx, summary):
                    entry["status"] = "cut-by-priority (I)"
                    summary["steps"].append(entry)
                    continue
                if st.kind == "take" and "retake_of" in st.opts:
                    skip = self.retake_skip(st, summary["steps"])
                    if skip is not None:
                        log("retake skipped", key=st.key, **skip)
                        entry.update(skip)
                        summary["steps"].append(entry)
                        continue
                try:
                    if st.kind in ("run", "take"):
                        entry.update(self.run_step(st))
                        if entry.get("status") == "done" and not entry.get("valid"):
                            rc = 1
                            self.after_failure(entry, f"invalid: {entry.get('reasons')}")
                            if self.plan.on_failure == "abort":
                                aborted = f"on_failure=abort: run {st.key} invalid"
                    elif st.kind == "compose":
                        entry.update(self.compose_step(st, summary["steps"]))
                    elif st.kind == "probe-cold-prefill":
                        entry["cold_prefill_tok_s"] = self.probe_cold_prefill(st)["cold_prefill_tok_s_median"]
                    elif st.kind == "calibrate-slo":
                        c = self.calibrate_slo(st)
                        entry.update(slo=c["slo"], in_band=c.get("in_band"), rule_applied=c.get("rule_applied"),
                                     slo_lock=self.slo_lock)
                    elif st.kind == "teardown":
                        for t in self.live_topologies():
                            self.teardown(t)
                    entry["status"] = entry.get("status", "done")
                    self.consecutive_failures = 0
                except HardStop:
                    entry["status"] = "cut-by-hard-stop"
                    summary["steps"].append(entry)
                    raise
                except StepError as exc:
                    entry.update(status="failed", error=str(exc)[:800], invalid=st.kind in ("run", "take"))
                    log("step failed", line=st.line, kind=st.kind, cell=st.cell, error=str(exc)[:800])
                    self.failed += 1
                    self.consecutive_failures += 1
                    rc = 1
                    if st.kind != "compose":  # a CPU-only Job: the GPU topology is untouched and stays for teardown
                        self.after_failure(entry, str(exc))
                    if self.plan.on_failure == "abort":
                        what = st.key if st.kind in ("run", "take") else st.line
                        aborted = f"on_failure=abort: {st.kind} {what} failed"
                    elif self.consecutive_failures >= 3:
                        aborted = "consecutive-failures"
                    elif st.kind in ("probe-cold-prefill", "calibrate-slo"):
                        aborted = f"{st.kind}-failed"
                summary["steps"].append(entry)
            if aborted:
                summary["aborted"] = aborted
        except ConfigError as exc:
            summary["refused"] = str(exc)
            log("refusing", error=str(exc))
            rc = 2
        except HardStop as exc:
            summary["aborted"] = f"hard-stop: {exc}"
            log("hard stop", reason=str(exc))
            wait = SIGTERM_TEARDOWN_WAIT_S if self.stop_reason else TEARDOWN_TIMEOUT_S
            try:
                for t in self.live_topologies():
                    self.teardown(t, wait_s=wait)
            except StepError as texc:
                summary["teardown_error"] = str(texc)[:500]
            rc = 3
        except FatalError as exc:
            summary["aborted"] = f"fatal: {exc}"
            log("fatal", error=str(exc))
            try:
                for t in self.live_topologies():
                    self.teardown(t)
            except StepError as texc:
                summary["teardown_error"] = str(texc)[:500]
            rc = 1
        except StepError as exc:
            summary["aborted"] = f"setup-failed: {exc}"
            log("setup failed", error=str(exc)[:800])
            rc = 1
        finally:
            signal.signal(signal.SIGTERM, previous)
            summary["teardowns"] = self.teardowns
            summary["ended_at"] = utcnow()
            summary["rc"] = rc
            path = self.work / f"driver-summary-{self.launch}.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(summary, indent=1, default=str))
            try:
                self.s3.put(path, f"{self.prefix}/_driver/{path.name}")
            except StepError as exc:
                log("summary upload failed", error=str(exc)[:300])
        return rc


CELLS_BY_TOPOLOGY_MODE = {(t, m): c for c, (t, m) in CELLS.items()}


def main(argv: list[str] | None = None) -> int:
    """Entry point."""
    ap = argparse.ArgumentParser(description=(__doc__ or "").split("\n", 1)[0])
    ap.add_argument("--plan", type=Path, required=True)
    ap.add_argument("--check", action="store_true", help="validate the plan (and --trace/--sidecar) only, no side effects")
    ap.add_argument("--trace", type=Path, help="--check: local trace to validate")
    ap.add_argument("--sidecar", type=Path, help="--check: local sidecar to validate")
    args = ap.parse_args(argv)
    try:
        plan = parse_plan(args.plan.read_text())
        if args.check:
            out: dict[str, Any] = {"plan": str(args.plan), "run_id": plan.run_id, "plan_sha256": plan.source_sha256,
                                   "steps": len(plan.steps), "runs": [s.key for s in plan.steps if s.kind == "run"],
                                   "takes": [s.key for s in plan.steps if s.kind == "take"],
                                   "compose": any(s.kind == "compose" for s in plan.steps),
                                   "w2_frozen": plan.w2["frozen"], "on_failure": plan.on_failure,
                                   "cut_order": plan.cut_order, "slo": plan.slo or None, "slo_ref": plan.slo_ref,
                                   "runnable_offline": plan.w2["frozen"] == "yes",
                                   "needs_at_runtime": ["e3 SLO lock in S3"] if plan.slo_ref else [],
                                   "hard_stop": plan.hard_stop}
            if args.trace and args.sidecar:
                for p, want in ((args.trace, plan.w2["trace_sha256"]), (args.sidecar, plan.w2["sidecar_sha256"])):
                    got = hashlib.sha256(p.read_bytes()).hexdigest()
                    if got != want:
                        raise ConfigError(f"{p}: sha256 {got} != plan {want}")
                d = Driver.__new__(Driver)
                d.trace, d.sidecar = args.trace, args.sidecar
                out["trace"] = Driver.check_trace(d)
                hours = sum(run_estimate_s(plan.estimate, d.trace_span_s, True) + (CAPTURE_LEAD_S if s.kind == "take" else 0)
                            for s in plan.steps if s.kind in ("run", "take"))
                out["upper_bound_hours_if_every_run_boots_I"] = round(hours / 3600, 2)
            print(json.dumps(out, indent=1))
            return 0
        if plan.w2["frozen"] != "yes":
            raise ConfigError("w2 frozen=no: freeze the W2 trace (E2) and set frozen=yes in the plan before running")
        takes = any(s.kind == "take" for s in plan.steps)
        for var in ("RESULTS_BUCKET", "AWS_REGION", "CB_NODE", "HARD_STOP", "JOB_NAME", "L3_CLEANUP_IMAGE",
                    *(["GRAFANA_URL", "COMPOSE_FFMPEG_IMAGE"] if takes else [])):
            if not os.environ.get(var):
                raise ConfigError(f"{var} is not set")
        for var, pattern in ARGV_ENV.items():  # they reach kubectl / aws arguments: plain names only
            if not pattern.fullmatch(os.environ[var]):
                raise ConfigError(f"{var} {os.environ[var]!r} is not a plain name (must match {pattern.pattern})")
        for var in ARGV_PATH_ENV:
            if var in os.environ and not os.path.isabs(os.environ[var]):
                raise ConfigError(f"{var} {os.environ[var]!r} must be an absolute path")
        if os.environ["HARD_STOP"] != plan.hard_stop:
            raise ConfigError(f"Job HARD_STOP {os.environ['HARD_STOP']} != plan hard_stop {plan.hard_stop}")
        if time.time() >= iso_epoch(plan.hard_stop):
            raise ConfigError(f"hard stop {plan.hard_stop} already passed")
    except ConfigError as exc:
        log("refusing", error=str(exc))
        return 2
    log("start", run_id=plan.run_id, steps=len(plan.steps), hard_stop=plan.hard_stop, plan_sha256=plan.source_sha256)
    return Driver(plan, args).execute()


if __name__ == "__main__":
    sys.exit(main())
