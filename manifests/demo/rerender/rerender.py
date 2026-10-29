# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Re-render the demo takes of the Qwen3.8 matrix from the recorded Prometheus series (CPU only, in-cluster).

Why: the live capture of take q38-e7-s7-rep1 recorded 'No data'. It opened the demo-take dashboard ~240 s before the
replay with an absolute range in the future, so the time-range-dependent namespace variable (label_values, refresh on
time range change) only offered s-agg and Grafana replaced var-namespace=s-dis-hic with it. The series are intact in
Prometheus, so this tool rebuilds the take view from history, one frame per step, instead of recording it live.
Proven on the E7, W3-16K r32 and r25 takes; this copy lives next to manifests/demo/capture.

One Job bench/demo-rerender-<source take id> per take (both run in parallel on cpu-demo), containers in order:
  check   (AWS CLI)     list the keys already under takes/q38-e7-rerender/<take>/ (append-only pre-check)
  tools   (ffmpeg)      the encoder and ffprobe exist (pulls the image before the long render)
  render  (Playwright)  for t = 0, S, 2S, ... up to the replay span + drain: open
                        /d/demo-take?orgId=1&from=<replay start>&to=<replay start + t>&kiosk&var-namespace=<ns>
                        (basic auth header from the Secret demo-grafana-auth, never printed), wait for the panels,
                        reject the frame when the namespace picker does not show <ns> or an expected panel says
                        'No data' (from EXPECT_DATA_AFTER_S on), retry it (MAX_ATTEMPTS), screenshot 1920x1080 as
                        frame-NNNNN.png. A frame that stays bad fails the Job: never a bad frame in the video.
  encode  (ffmpeg)      the frames at 1/S frames per second -> <take>.webm (the video lasts as long as the take)
  meta    (Playwright image, python only)  <take>-capture.json (video_t0_ms = replay start, video_end_ms: what
                        compose.sh reads) and the take record <take>-take.json (result_of_record false, method,
                        source take id)
  upload  (AWS CLI)     append-only under s3://$RESULTS_BUCKET/takes/q38-e7-rerender/<take>/, the record last

The script reaches the pod inline (env RERENDER_PY), so a re-render Job needs no ConfigMap. The compose Job
(compose_job(): fetch -> render -> upload, one compose.sh phase each) is fed with the re-rendered webms; compose.sh
ships in the ConfigMap SCRIPTS_CM, rendered by `kubectl kustomize manifests/demo/rerender` (applied once).

Workstation side. RESULTS_BUCKET, AWS_REGION and BENCH_REPO (the bench ECR repository; the ffmpeg image is mirrored
there) are read from the environment when a Job is built, never from this file; --platform-env <platform.env of the
deployment's config component> fills the unset ones. Optional RERENDER_RUN_ID and RERENDER_NOTE (read at import)
select another re-render set than E7, e.g. q38-w3-r32-rerender -> takes/q38-w3-r32-rerender/:
  python3 rerender.py jobs --source-compose inputs/compose.json --out-dir jobs [--platform-env FILE] [--drain-s 0]
      writes the two re-render Jobs, the compose spec and the compose Job as JSON (kubectl create -f), nothing
      when a setting is missing
  python3 rerender.py compose-record --spec jobs/compose-spec.json --source-compose-uri <s3 uri> --out <file>
      the compose marker compose.json (as the matrix driver's compose step writes it), once the compose Job is
      complete

A re-rendered take is NOT a result of record: it shows the recorded series of the take, not a live recording.
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import json
import logging
import os
import re
import shlex
import sys
import time
from collections.abc import Callable, Iterable, Iterator, Mapping
from pathlib import Path
from typing import Any

# RERENDER_RUN_ID / RERENDER_NOTE (read at import; the Job passes them to the in-pod steps) select another re-render
# set than E7: its run id, S3 prefix takes/<run id>/ and the note of its records. Unset: the E7 values.
RUN_ID = os.environ.get("RERENDER_RUN_ID", "").strip() or "q38-e7-rerender"
if not re.fullmatch(r"q38-[a-z0-9]+(-[a-z0-9]+)*-rerender", RUN_ID):
    raise ValueError(f"RERENDER_RUN_ID {RUN_ID!r} must look like q38-<name>-rerender (lowercase, no '/')")
S3_PREFIX = f"takes/{RUN_ID}"
METHOD = "rendered from recorded Prometheus series"
NOTE = os.environ.get("RERENDER_NOTE", "").strip() or (
    f"TAKE - NOT A RESULT OF RECORD (spec section 8): {METHOD} after the live takes; the clip shows the same "
    "traffic on both stacks; slide numbers come from the matrix (plan-e4..e6)")
if "NOT A RESULT OF RECORD" not in NOTE or METHOD not in NOTE:
    raise ValueError(f"RERENDER_NOTE must say 'NOT A RESULT OF RECORD' and {METHOD!r}")
NAMESPACES = ("s-agg", "s-agg-hic", "s-dis", "s-dis-hic")
DEFAULT_DRAIN_S = 60  # rendered beyond the replay end; compose keeps a 30 s tail after the later replay end
MIN_RANGE_MS = 1000  # the t = 0 frame still needs a non-empty Grafana range
MAX_ATTEMPTS = 3
EXPECT_DATA_AFTER_S = 60  # before this a panel may legitimately say 'No data' (range shorter than its windows)
RETRY_SLEEP_S = 2.0  # x attempt number, between two attempts of one frame
STEP_CHOICES_S = range(4, 11)
COMPOSE_TARGET_S = 170  # the ~2:50 deck clip
DASHBOARD_UID = "demo-take"
DEFAULT_GRAFANA_URL = "http://prometheus-grafana.monitoring.svc"  # in-cluster Service; GRAFANA_URL overrides it
# The visible (non-collapsed) panels of the take view; each must render, and show data from EXPECT_DATA_AFTER_S on.
EXPECTED_PANELS = ("turns with TTFT > 10 s", "TTFT p95 since replay start", "% of turns with TTFT < 1 s",
                   "Requests waiting for first token", "TTFT p50", "KV cache hit rate", "Output tokens/s")
PANEL_TESTID = "data-testid Panel header "  # Grafana 11.5 panel <section> test id prefix (checked live)
VIEW = {"width": 1920, "height": 1080}
PANEL_WAIT_S = 60
NAV_TIMEOUT_MS = 60_000
RERENDER_DEADLINE_S = 3600
PLAYWRIGHT_PIP = "playwright==1.64.0"  # the Python package of the pinned image tag v1.64.0-noble
WORK = "/work"
RECORD_SUFFIX = "-take.json"
# Settings of the workstation side, read from the environment when a Job is built (never literals of this file).
SETTINGS = ("RESULTS_BUCKET", "AWS_REGION", "BENCH_REPO")
# From the Qwen3.8 matrix driver v4 (the takes it recorded): labels on screen, take cells and their scenario
# namespaces, the take namespace and the compose tail.
PART_OF = "qwen38-matrix"
TAKES_NS = "bench"
TAKE_LABELS = {"S0": "Baseline: aggregated + round-robin", "S7": "Router + HiCache + PD disaggregation"}
CELL_NAMESPACE = {"S0": "s-agg", "S7": "s-dis-hic"}
COMPOSE_TAIL_S = 30  # kept after the later of the two replay ends in the full clip
COMPOSE_DEADLINE_S = 5400
SCRIPTS_CM = "demo-rerender-compose"  # compose.sh, rendered by manifests/demo/rerender/kustomization.yaml
GRAFANA_SECRET = {"name": "demo-grafana-auth", "key": "password"}  # existing Secret in bench, never read here
# Images pinned by digest. Render/meta: the demo capture image (manifests/demo/capture/job.yaml). S3 legs: AWS CLI
# (public ECR). ffmpeg 9.0.2 (linuxserver/ffmpeg:9.0.2-cli-ls86, index digest) mirrored into $BENCH_REPO.
CAPTURE_IMAGE = ("mcr.microsoft.com/playwright/python:v1.64.0-noble"
                 "@sha256:684df257a1fa0ef3fa288239819984008f560e8f8db5b635ee9e2a50cbcd2e23")
CAPTURE_RESOURCES: dict[str, Any] = {"requests": {"cpu": "1", "memory": "3Gi"}, "limits": {"cpu": "3", "memory": "6Gi"}}
COMPOSE_AWS_IMAGE = ("public.ecr.aws/aws-cli/aws-cli:2.38.1"
                     "@sha256:733d5be1015825e4038c6016f3fa9954868ddee1f611b73be795a9cc2e706bfd")
FFMPEG_TAG = "ffmpeg-9.0.2-cli-ls86@sha256:742e17516dd4d7b76b939d774bd08f0f6b7cab8bce1c014b95ab2fb992fd0a1f"
CPU_NODE = {"karpenter.sh/nodepool": "cpu-demo"}
CTR_SECURITY: dict[str, Any] = {"allowPrivilegeEscalation": False, "capabilities": {"drop": ["ALL"]}}

# Reads what the frame shows (Grafana 11.5 Scenes DOM, checked live on demo-take): the namespace picker text and,
# per expected panel title, whether the panel shows 'No data'.
PROBE_JS = r"""(titles) => {
  const LABEL = 'data-testid Dashboard template variables submenu Label Namespace';
  const VALUE = 'data-testid Dashboard template variables Variable Value DropDown value link text ';
  const HEADER = 'data-testid Panel header ';
  const id = (e) => e.getAttribute('data-testid') || '';
  let picker = '';
  for (const v of document.querySelectorAll('[data-testid="data-testid template variable"]')) {
    if (![...v.querySelectorAll('[data-testid]')].some((e) => id(e) === LABEL)) continue;
    const value = [...v.querySelectorAll('[data-testid]')]
      .find((e) => id(e).startsWith(VALUE) && !id(e).endsWith('-input'));
    picker = value ? (value.innerText || '').trim() : '';
  }
  const sections = new Map([...document.querySelectorAll('[data-testid]')]
    .filter((e) => id(e).startsWith(HEADER)).map((e) => [id(e).slice(HEADER.length), e]));
  const panels = {};
  for (const t of titles) {
    const s = sections.get(t);
    if (!s) continue;
    const noData = [...s.querySelectorAll('*')]
      .some((e) => e.childElementCount === 0 && (e.textContent || '').trim() === 'No data');
    panels[t] = {no_data: noData};
  }
  return {namespace_picker: picker, panels: panels};
}"""

CHECK_SH = """set -euo pipefail
mkdir -p "$HOME"
aws s3api list-objects-v2 --bucket "$RESULTS_BUCKET" --prefix "$S3_PREFIX/$TAKE_ID/" --query 'Contents[].Key' \\
  --output json >"$WORK_DIR/existing-keys.json"
found="$(tr -d '\\n ' <"$WORK_DIR/existing-keys.json")"
echo "check: keys already under s3://$RESULTS_BUCKET/$S3_PREFIX/$TAKE_ID/: $found"
"""

TOOLS_SH = """set -euo pipefail
v="$(ffmpeg -hide_banner -version)"
echo "${v%%$'\\n'*}"
e="$(ffmpeg -hide_banner -encoders)"
grep -q ' libvpx-vp9 ' <<<"$e"
p="$(ffprobe -hide_banner -version)"
echo "${p%%$'\\n'*}"
echo tools-ok
"""

UPLOAD_SH = """set -euo pipefail
mkdir -p "$HOME"
read -ra keys <<<"$OUTPUT_KEYS"
for key in "${keys[@]}"; do
  file="$WORK_DIR/out/${key##*/}"
  if [ ! -s "$file" ]; then
    echo "upload: missing or empty $file" >&2
    exit 3
  fi
  if aws s3api head-object --bucket "$RESULTS_BUCKET" --key "$key" >/dev/null 2>&1; then
    echo "upload: refusing to overwrite s3://$RESULTS_BUCKET/$key (append-only)" >&2
    exit 3
  fi
  aws s3 cp --only-show-errors "$file" "s3://$RESULTS_BUCKET/$key"
  echo "upload: s3://$RESULTS_BUCKET/$key ($(stat -c %s "$file") bytes)"
done
"""

LOG = logging.getLogger("rerender")


class ConfigError(Exception):
    """A required setting is missing or malformed."""


class FrameError(Exception):
    """A frame stayed bad (wrong namespace, 'No data', render failure) after MAX_ATTEMPTS."""


class AppendOnlyError(Exception):
    """An output key already exists in S3 (never overwritten)."""


def log(event: str, **fields: Any) -> None:
    """Emit one structured (JSON) log line; callers never pass a secret.

    Args:
        event: Short event name.
        **fields: Extra JSON-serializable fields.
    """
    LOG.info(json.dumps({"event": event, **fields}, sort_keys=True, default=str))


# ================================================================ pure helpers (unit-tested)
def total_ms(replay_start_ms: int, replay_end_ms: int, drain_s: int) -> int:
    """Length of the rendered range: the replay span plus the drain.

    Args:
        replay_start_ms: Replay start, epoch milliseconds (the dashboard ``from`` of every frame).
        replay_end_ms: Replay end, epoch milliseconds.
        drain_s: Seconds rendered after the replay end.

    Returns:
        The range length in milliseconds.

    Raises:
        ValueError: The replay end is not after its start, or the drain is negative.
    """
    if replay_end_ms <= replay_start_ms or drain_s < 0:
        raise ValueError(f"need end > start and drain >= 0, got {replay_start_ms}..{replay_end_ms}, drain {drain_s}")
    return replay_end_ms - replay_start_ms + drain_s * 1000


def frame_schedule(span_ms: int, step_s: int) -> list[int]:
    """Offsets of the frames from the replay start: ``min(i * step, span)`` for i = 0..ceil(span / step).

    Args:
        span_ms: Rendered range length in milliseconds.
        step_s: Seconds between two frames.

    Returns:
        Strictly increasing offsets in milliseconds, from 0 to exactly ``span_ms``.

    Raises:
        ValueError: A non-positive span or step.
    """
    if span_ms <= 0 or step_s <= 0:
        raise ValueError(f"need span > 0 and step > 0, got {span_ms} ms, {step_s} s")
    step_ms = step_s * 1000
    n = -(-span_ms // step_ms)
    return [min(i * step_ms, span_ms) for i in range(n + 1)]


def choose_step_s(span_ms: int, per_frame_s: float, budget_s: float) -> int:
    """Smallest step in STEP_CHOICES_S whose frames all render within the time budget.

    Args:
        span_ms: Rendered range length in milliseconds.
        per_frame_s: Expected render time of one frame (seconds, retries included).
        budget_s: Seconds the render may take.

    Returns:
        The step in seconds.

    Raises:
        ValueError: Even the largest step does not fit the budget.
    """
    for step in STEP_CHOICES_S:
        if len(frame_schedule(span_ms, step)) * per_frame_s <= budget_s:
            return step
    raise ValueError(f"no step in {STEP_CHOICES_S.start}..{STEP_CHOICES_S.stop - 1} s renders {span_ms} ms "
                     f"at {per_frame_s} s/frame within {budget_s} s")


def _check_namespace(namespace: str) -> str:
    if namespace not in NAMESPACES:
        raise ValueError(f"namespace {namespace!r} is not one of {NAMESPACES}")
    return namespace


def frame_url(base: str, uid: str, replay_start_ms: int, t_ms: int, namespace: str) -> str:
    """Kiosk URL of one frame: the range [replay start, replay start + t], no auto refresh, no credentials.

    Args:
        base: Grafana base URL.
        uid: Dashboard uid.
        replay_start_ms: Replay start, epoch milliseconds.
        t_ms: Frame offset from the replay start (0 gives a MIN_RANGE_MS wide range).
        namespace: Scenario namespace (one of NAMESPACES).

    Returns:
        The URL.

    Raises:
        ValueError: An unknown namespace or a negative offset.
    """
    _check_namespace(namespace)
    if t_ms < 0:
        raise ValueError(f"negative frame offset {t_ms}")
    to_ms = replay_start_ms + max(t_ms, MIN_RANGE_MS)
    return f"{base.rstrip('/')}/d/{uid}?orgId=1&from={replay_start_ms}&to={to_ms}&kiosk&var-namespace={namespace}"


def judge_frame(frame: Mapping[str, Any], *, namespace: str, t_ms: int) -> str | None:
    """Why a rendered frame must be retried, or None when it is good.

    Args:
        frame: What the frame shows: ``namespace_picker`` (text) and ``panels`` (title -> {"no_data": bool}).
        namespace: The take's namespace.
        t_ms: Frame offset from the replay start.

    Returns:
        "wrong-namespace" (always checked first), "no-data" (no panel info at all, or a panel without data from
        EXPECT_DATA_AFTER_S on), else None.
    """
    if frame.get("namespace_picker") != namespace:
        return "wrong-namespace"
    panels = frame.get("panels") or {}
    if not panels:
        return "no-data"
    if t_ms >= EXPECT_DATA_AFTER_S * 1000 and any((p or {}).get("no_data", True) for p in panels.values()):
        return "no-data"
    return None


def render_with_retry(render_fn: Callable[[int], dict[str, Any]], *, namespace: str, t_ms: int,
                      max_attempts: int = MAX_ATTEMPTS, sleep: Callable[[float], Any] = time.sleep,
                      on_reject: Callable[[int, str], None] | None = None) -> tuple[dict[str, Any], int]:
    """Render one frame until it is good, at most ``max_attempts`` times.

    Args:
        render_fn: Renders the frame (attempt number, from 1) and returns what it shows; an exception counts as a
            failed attempt.
        namespace: The take's namespace.
        t_ms: Frame offset from the replay start.
        max_attempts: Attempts before giving up.
        sleep: Called between two attempts (RETRY_SLEEP_S x the attempt just failed).
        on_reject: Called with (attempt, reason) for every rejected attempt.

    Returns:
        The good frame and the attempt that produced it.

    Raises:
        FrameError: Every attempt was rejected (the reasons are in the message).
    """
    reasons: list[str] = []
    for attempt in range(1, max_attempts + 1):
        if attempt > 1:
            sleep(RETRY_SLEEP_S * (attempt - 1))
        try:
            frame = render_fn(attempt)
        except Exception as exc:  # noqa: BLE001 - a browser failure is one failed attempt
            reason: str | None = f"render-error: {type(exc).__name__}: {str(exc)[:200]}"
        else:
            reason = judge_frame(frame, namespace=namespace, t_ms=t_ms)
            if reason is None:
                return frame, attempt
        reasons.append(str(reason))
        if on_reject is not None:
            on_reject(attempt, str(reason))
    raise FrameError(f"frame t={t_ms} ms rejected {max_attempts} times: {'; '.join(reasons)}")


def encode_command(frames_dir: str, step_s: int, out_path: str) -> list[str]:
    """The ffmpeg argv that turns frame-NNNNN.png into a webm lasting ``frames x step_s`` seconds (never overwrites).

    Args:
        frames_dir: Directory of the numbered PNG frames.
        step_s: Seconds between two frames (the input frame rate is 1/step_s).
        out_path: Output path, a .webm.

    Returns:
        The command line.

    Raises:
        ValueError: A non-positive step or an output that is not a .webm.
    """
    if step_s <= 0:
        raise ValueError(f"step must be > 0, got {step_s}")
    if not out_path.endswith(".webm"):
        raise ValueError(f"output must be a .webm, got {out_path}")
    return ["ffmpeg", "-hide_banner", "-nostats", "-loglevel", "warning", "-n",
            "-framerate", f"1/{step_s}", "-i", f"{frames_dir.rstrip('/')}/frame-%05d.png",
            "-c:v", "libvpx-vp9", "-crf", "24", "-b:v", "0", "-row-mt", "1", "-deadline", "good", "-cpu-used", "4",
            "-pix_fmt", "yuv420p", "-an", out_path]


def rerender_take_id(source_take_id: str) -> str:
    """Id of the re-rendered take of ``source_take_id``."""
    return f"{source_take_id}-rerender"


def check_key(key: str) -> str:
    """Refuse an S3 key outside the append-only re-render prefix.

    Args:
        key: Object key (no bucket, no leading slash).

    Returns:
        The key.

    Raises:
        ValueError: The key is empty, absolute, has an empty or '..' segment, or is outside S3_PREFIX/.
    """
    parts = key.split("/")
    if not key.startswith(S3_PREFIX + "/") or any(p in ("", ".", "..") for p in parts) or len(parts) < 3:
        raise ValueError(f"key {key!r} is outside {S3_PREFIX}/")
    return key


def s3_key(take_id: str, filename: str) -> str:
    """Key of one file of a re-rendered take (checked by check_key)."""
    return check_key(f"{S3_PREFIX}/{take_id}/{filename}")


def output_keys(take_id: str) -> dict[str, str]:
    """The keys a re-render Job writes, in upload order (the record last).

    Args:
        take_id: The re-rendered take id.

    Returns:
        webm, capture (``<take>-capture.json``, next to the webm as compose.sh expects) and record keys.
    """
    return {"webm": s3_key(take_id, f"{take_id}.webm"), "capture": s3_key(take_id, f"{take_id}-capture.json"),
            "record": s3_key(take_id, f"{take_id}{RECORD_SUFFIX}")}


def check_append_only(keys: Iterable[str], exists: Callable[[str], bool]) -> None:
    """Refuse when any of the keys already exists (every key is checked).

    Args:
        keys: Keys about to be written.
        exists: Whether a key exists.

    Raises:
        AppendOnlyError: Names every existing key.
        ValueError: A key outside S3_PREFIX/.
    """
    present = [k for k in keys if exists(check_key(k))]
    if present:
        raise AppendOnlyError(f"already in S3 (append-only, not overwritten): {present}")


def capture_meta(replay_start_ms: int, n_frames: int, step_s: int) -> dict[str, int]:
    """The video instants compose.sh syncs on: the webm starts at the replay start and lasts n_frames x step_s.

    Args:
        replay_start_ms: Replay start, epoch milliseconds.
        n_frames: Frames in the webm.
        step_s: Seconds per frame.

    Returns:
        video_t0_ms and video_end_ms (epoch milliseconds).
    """
    return {"video_t0_ms": int(replay_start_ms), "video_end_ms": int(replay_start_ms) + n_frames * step_s * 1000}


def take_record(*, source: Mapping[str, Any], n_frames: int, retries: int, webm_uri: str, bytes_: int,
                extra: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """The take record of a re-rendered take (never a result of record, never a secret).

    Args:
        source: The source take: cell, take_id, namespace, replay_start_ms, replay_end_ms, sync_ms, total_ms,
            webm_uri (the live recording) and step_s.
        n_frames: Frames rendered.
        retries: Rejected attempts over all frames.
        webm_uri: S3 URI of the re-rendered webm.
        bytes_: Size of the webm.
        extra: Additional fields (render statistics); they never override the fields above.

    Returns:
        The JSON-serializable record.
    """
    start, step = int(source["replay_start_ms"]), int(source["step_s"])
    core = {"take_id": rerender_take_id(source["take_id"]), "source_take_id": source["take_id"],
            "cell": source.get("cell"), "namespace": source["namespace"], "method": METHOD,
            "result_of_record": False, "note": NOTE, "replay_start_ms": start,
            "replay_end_ms": source["replay_end_ms"], "sync_ms": source["sync_ms"], "from_ms": start,
            "to_ms": start + int(source["total_ms"]), "step_s": step, "frames": n_frames, "retries": retries,
            **capture_meta(start, n_frames, step), "webm_uri": webm_uri, "webm_bytes": bytes_,
            "source_webm_uri": source.get("webm_uri"), "dashboard_uid": DASHBOARD_UID}
    return {**dict(extra or {}), **core}


# ================================================================ settings (workstation side)
def _setting(name: str, default: str | None = None) -> str:
    value = os.environ.get(name, "").strip()
    if value:
        return value
    if default is None:
        raise ConfigError(f"{name} is not set")
    return default


def read_platform_env(path: Path) -> dict[str, str]:
    """The ``KEY=VALUE`` lines of a platform config file (blank lines and ``#`` comments skipped).

    Args:
        path: The file, e.g. the platform.env of the deployment's config component under manifests/.

    Returns:
        Key -> value.

    Raises:
        ConfigError: The file cannot be read.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"cannot read the platform config {path}: {exc}") from exc
    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            out[key.strip()] = value.strip()
    return out


@contextlib.contextmanager
def platform_defaults(path: str | None) -> Iterator[None]:
    """Fill the unset SETTINGS from a platform config file for the duration of the block (the environment wins).

    Args:
        path: The platform config file, or None (nothing is filled).

    Yields:
        Nothing; the previous environment is restored on exit.

    Raises:
        ConfigError: The file cannot be read.
    """
    values = read_platform_env(Path(path)) if path else {}
    filled = {k: os.environ.get(k) for k in SETTINGS if not os.environ.get(k, "").strip() and values.get(k)}
    for key in filled:
        os.environ[key] = values[key]
    if path:
        log("platform-defaults", path=path, filled=sorted(filled))  # names only, never the values
    try:
        yield
    finally:
        for key, previous in filled.items():
            if previous is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = previous


def ffmpeg_image() -> str:
    """The pinned ffmpeg image in the bench ECR repository.

    Returns:
        ``$BENCH_REPO:<FFMPEG_TAG>``.

    Raises:
        ConfigError: BENCH_REPO is not set.
    """
    return f"{_setting('BENCH_REPO')}:{FFMPEG_TAG}"


# ================================================================ Job manifests (workstation side)
def compose_job_name(run_id: str) -> str:
    """Name of the compose Job of a re-render set."""
    return f"q38-take-compose-{run_id}"


def _pod_security(uid: int) -> dict[str, Any]:
    return {"runAsNonRoot": True, "runAsUser": uid, "runAsGroup": uid, "fsGroup": uid,
            "seccompProfile": {"type": "RuntimeDefault"}}


def compose_spec(takes: Mapping[str, Mapping[str, Any]], target_s: int = COMPOSE_TARGET_S) -> dict[str, Any]:
    """What the compose Job renders from the re-rendered takes (baseline S0 left, candidate S7 right).

    The sync instants are the live takes' (replay_start_ms, replay_end_ms, sync_ms); the inputs are the re-rendered
    webms; the outputs go append-only under takes/<run id>/compose/.

    Args:
        takes: Per cell (S0 and S7): take_id (source), replay_start_ms, replay_end_ms, sync_ms, rerender_webm_uri.
        target_s: Length of the sped-up deck clip.

    Returns:
        The spec compose.sh reads (COMPOSE_SPEC).

    Raises:
        ConfigError: RESULTS_BUCKET is not set.
        KeyError: A take cell is missing.
        ValueError: An input webm is not a re-rendered one.
    """
    base = f"s3://{_setting('RESULTS_BUCKET')}/{S3_PREFIX}/compose/"
    inputs = []
    for cell, label in TAKE_LABELS.items():
        t = takes[cell]
        uri = str(t["rerender_webm_uri"])
        check_key(uri.removeprefix("s3://").split("/", 1)[-1])
        inputs.append({"cell": cell, "take_id": rerender_take_id(t["take_id"]), "source_take_id": t["take_id"],
                       "label": label, "webm_uri": uri, "replay_start_ms": t["replay_start_ms"],
                       "replay_end_ms": t["replay_end_ms"], "sync_ms": t["sync_ms"]})
    return {"run_id": RUN_ID, "target_s": target_s, "tail_s": COMPOSE_TAIL_S, "inputs": inputs,
            "outputs": {"full": base + "side-by-side-full.mp4", "deck": base + "side-by-side-deck.mp4",
                        "still": base + "three-panel-still.png"},
            "result_of_record": False, "method": METHOD, "note": NOTE}


def _deadline(hard_stop_epoch: int, now: float, what: str, cap_s: int = RERENDER_DEADLINE_S) -> int:
    left = int(hard_stop_epoch - now)
    if left <= 0:
        raise ValueError(f"{what}: the hard stop {hard_stop_epoch} has passed")
    return min(cap_s, left)


def compose_job(spec: dict[str, Any], *, hard_stop_epoch: int, now: float) -> dict[str, Any]:
    """The compose Job: fetch (AWS CLI) -> render (ffmpeg) -> upload (AWS CLI), one compose.sh phase each, on cpu-demo.

    compose.sh comes from the ConfigMap SCRIPTS_CM; COMPOSE_SPEC carries the spec as JSON. The deadline never passes
    the hard stop. Same Job as the matrix driver v4 built, except the script ConfigMap.

    Args:
        spec: compose_spec().
        hard_stop_epoch: The deadline never passes this instant (epoch seconds).
        now: Current instant (epoch seconds).

    Returns:
        The Job manifest.

    Raises:
        ValueError: The hard stop has passed.
        ConfigError: AWS_REGION or BENCH_REPO is not set.
    """
    name = compose_job_name(spec["run_id"])
    ads = _deadline(hard_stop_epoch, now, name, COMPOSE_DEADLINE_S)
    region, ffmpeg = _setting("AWS_REGION"), ffmpeg_image()
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
            "backoffLimit": 0, "ttlSecondsAfterFinished": 86400, "activeDeadlineSeconds": ads,
            "template": {"metadata": {"labels": dict(labels)}, "spec": {
                "serviceAccountName": "matrix-driver", "restartPolicy": "Never", "nodeSelector": dict(CPU_NODE),
                "securityContext": _pod_security(10001),
                "initContainers": [ctr("fetch", COMPOSE_AWS_IMAGE, light),
                                   ctr("render", ffmpeg,
                                       {"requests": {"cpu": "6", "memory": "8Gi"}, "limits": {"memory": "12Gi"}})],
                "containers": [ctr("upload", COMPOSE_AWS_IMAGE, light)],
                "volumes": [{"name": "scripts", "configMap": {"name": SCRIPTS_CM}},
                            {"name": "work", "emptyDir": {"sizeLimit": "16Gi"}}],
            }},
        },
    }


def rerender_job(take: Mapping[str, Any], *, hard_stop_epoch: int, now: float) -> dict[str, Any]:
    """The re-render Job of one take: check -> tools -> render -> encode -> meta -> upload, CPU only on cpu-demo.

    Args:
        take: cell, take_id (source), namespace, replay_start_ms, replay_end_ms, sync_ms, total_ms, webm_uri
            (the live recording) and step_s.
        hard_stop_epoch: The deadline never passes this instant (epoch seconds).
        now: Current instant (epoch seconds).

    Returns:
        The Job manifest (the Grafana password only through the Secret demo-grafana-auth).

    Raises:
        ValueError: An unknown namespace or the hard stop has passed.
        ConfigError: RESULTS_BUCKET, AWS_REGION or BENCH_REPO is not set.
    """
    source, cell = str(take["take_id"]), str(take["cell"])
    tid = rerender_take_id(source)
    namespace = _check_namespace(str(take["namespace"]))
    step, start = int(take["step_s"]), int(take["replay_start_ms"])
    frame_schedule(int(take["total_ms"]), step)  # validates span and step
    ads = _deadline(hard_stop_epoch, now, f"rerender {tid}")
    region, bucket, ffmpeg = _setting("AWS_REGION"), _setting("RESULTS_BUCKET"), ffmpeg_image()
    keys = output_keys(tid)
    out_webm = f"{WORK}/out/{keys['webm'].rsplit('/', 1)[-1]}"
    labels = {"app.kubernetes.io/name": "demo-rerender", "app.kubernetes.io/part-of": PART_OF,
              "demo.kubecon/take-id": tid, "q38-matrix/run-id": RUN_ID, "q38-matrix/cell": cell}
    aws_env: list[dict[str, Any]] = [{"name": "HOME", "value": f"{WORK}/home"}, {"name": "WORK_DIR", "value": WORK},
               {"name": "TAKE_ID", "value": tid}, {"name": "S3_PREFIX", "value": S3_PREFIX},
               {"name": "RESULTS_BUCKET", "value": bucket}, {"name": "OUTPUT_KEYS", "value": " ".join(keys.values())},
               {"name": "AWS_REGION", "value": region}, {"name": "AWS_DEFAULT_REGION", "value": region}]
    take_env: list[dict[str, Any]] = [{"name": "HOME", "value": "/home/pwuser"}, {"name": "WORK_DIR", "value": WORK},
                {"name": "TAKE_ID", "value": tid}, {"name": "SOURCE_TAKE_ID", "value": source},
                {"name": "CELL", "value": cell}, {"name": "NAMESPACE", "value": namespace},
                {"name": "FROM_MS", "value": str(start)},
                {"name": "TO_MS", "value": str(start + int(take["total_ms"]))},
                {"name": "REPLAY_END_MS", "value": str(int(take["replay_end_ms"]))},
                {"name": "SYNC_MS", "value": str(int(take["sync_ms"]))}, {"name": "STEP_S", "value": str(step)},
                {"name": "SOURCE_WEBM_URI", "value": str(take["webm_uri"])},
                {"name": "S3_PREFIX", "value": S3_PREFIX}, {"name": "RESULTS_BUCKET", "value": bucket},
                {"name": "GRAFANA_URL", "value": os.environ.get("GRAFANA_URL") or DEFAULT_GRAFANA_URL},
                {"name": "DASHBOARD_UID", "value": DASHBOARD_UID}, {"name": "GRAFANA_USER", "value": "admin"},
                {"name": "RERENDER_RUN_ID", "value": RUN_ID}, {"name": "RERENDER_NOTE", "value": NOTE}]
    render_env: list[dict[str, Any]] = [*take_env, {"name": "PIP_BREAK_SYSTEM_PACKAGES", "value": "1"},
                  {"name": "GRAFANA_PASSWORD", "valueFrom": {"secretKeyRef": dict(GRAFANA_SECRET)}},
                  {"name": "RERENDER_PY", "value": Path(__file__).read_text(encoding="utf-8")}]
    work = {"name": "work", "mountPath": WORK}
    light = {"requests": {"cpu": "100m", "memory": "256Mi"}, "limits": {"memory": "1Gi"}}

    def ctr(name: str, image: str, command: list[str], env: list[dict[str, Any]], resources: dict[str, Any], *,
            mounts: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        return {"name": name, "image": image, "imagePullPolicy": "IfNotPresent", "command": command, "env": env,
                "resources": json.loads(json.dumps(resources)), "securityContext": dict(CTR_SECURITY),
                "volumeMounts": mounts or [dict(work)]}

    render_sh = ("set -euo pipefail\n"
                 f"printf '%s' \"$RERENDER_PY\" >{WORK}/rerender.py\n"
                 "python3 -m pip install --user --no-cache-dir --disable-pip-version-check --quiet "
                 f"{PLAYWRIGHT_PIP}\nexec python3 {WORK}/rerender.py render\n")
    encode_sh = ("set -euo pipefail\n" + shlex.join(encode_command(f"{WORK}/frames", step, out_webm)) + "\n"
                 f"d=\"$(ffprobe -v error -show_entries format=duration -of default=noprint_wrappers=1:nokey=1 "
                 f"{out_webm})\"\nprintf '%s\\n' \"$d\" >{WORK}/webm-duration.txt\n"
                 "echo \"encode: webm duration $d s\"\n")
    tmp_home: list[dict[str, Any]] = [{"name": "HOME", "value": "/tmp"}]
    init = [
        ctr("check", COMPOSE_AWS_IMAGE, ["/bin/bash", "-c", CHECK_SH], aws_env, light),
        ctr("tools", ffmpeg, ["/bin/bash", "-c", TOOLS_SH], tmp_home, light),
        ctr("render", CAPTURE_IMAGE, ["/bin/bash", "-c", render_sh], render_env, CAPTURE_RESOURCES,
            mounts=[dict(work), {"name": "dshm", "mountPath": "/dev/shm"}]),
        ctr("encode", ffmpeg, ["/bin/bash", "-c", encode_sh], tmp_home,
            {"requests": {"cpu": "2", "memory": "2Gi"}, "limits": {"memory": "4Gi"}}),
        ctr("meta", CAPTURE_IMAGE, ["python3", "-I", f"{WORK}/rerender.py", "meta"], take_env, light),
    ]
    return {
        "apiVersion": "batch/v1", "kind": "Job",
        "metadata": {"name": f"demo-rerender-{source}", "namespace": TAKES_NS, "labels": labels},
        "spec": {
            "backoffLimit": 0, "ttlSecondsAfterFinished": 86400, "activeDeadlineSeconds": ads,
            "template": {
                "metadata": {"labels": dict(labels), "annotations": {"karpenter.sh/do-not-disrupt": "true"}},
                "spec": {
                    "serviceAccountName": "matrix-driver", "restartPolicy": "Never", "nodeSelector": dict(CPU_NODE),
                    "terminationGracePeriodSeconds": 10, "securityContext": _pod_security(1001),
                    "initContainers": init,
                    "containers": [ctr("upload", COMPOSE_AWS_IMAGE, ["/bin/bash", "-c", UPLOAD_SH], aws_env, light)],
                    "volumes": [{"name": "work", "emptyDir": {"sizeLimit": "4Gi"}},
                                {"name": "dshm", "emptyDir": {"medium": "Memory", "sizeLimit": "1Gi"}}],
                },
            },
        },
    }


# ================================================================ in-pod steps
def _whole(name: str) -> int:
    raw = _setting(name)
    if not raw.isdigit():
        raise ConfigError(f"{name} must be a whole number, got {raw!r}")
    return int(raw)


def _take_from_env() -> dict[str, Any]:
    """The source take as the Job describes it (env), in the shape take_record() reads.

    Raises:
        ConfigError: A setting is missing or inconsistent.
    """
    take: dict[str, Any] = {
        "cell": _setting("CELL"), "take_id": _setting("SOURCE_TAKE_ID"), "namespace": _setting("NAMESPACE"),
        "replay_start_ms": _whole("FROM_MS"), "replay_end_ms": _whole("REPLAY_END_MS"), "sync_ms": _whole("SYNC_MS"),
        "step_s": _whole("STEP_S"), "webm_uri": _setting("SOURCE_WEBM_URI")}
    take["total_ms"] = _whole("TO_MS") - take["replay_start_ms"]
    if _setting("TAKE_ID") != rerender_take_id(take["take_id"]):
        raise ConfigError("TAKE_ID is not the re-render id of SOURCE_TAKE_ID")
    if _setting("S3_PREFIX") != S3_PREFIX:
        raise ConfigError(f"S3_PREFIX must be {S3_PREFIX}")
    if take["namespace"] not in NAMESPACES or take["total_ms"] <= 0 or take["step_s"] <= 0:
        raise ConfigError("NAMESPACE, FROM_MS..TO_MS or STEP_S is invalid")
    return take


def render_main() -> int:
    """Render every frame of the take into WORK_DIR/frames (exit 0), or fail the Job.

    Returns:
        0 rendered, 1 bad configuration, 2 a frame stayed bad, 3 an output key already exists.
    """
    try:
        take = _take_from_env()
        tid = rerender_take_id(take["take_id"])
        base, uid = _setting("GRAFANA_URL", DEFAULT_GRAFANA_URL), _setting("DASHBOARD_UID", DASHBOARD_UID)
        auth = "Basic " + base64.b64encode(f"{_setting('GRAFANA_USER', 'admin')}:{_setting('GRAFANA_PASSWORD')}"
                                           .encode()).decode()
        work = Path(_setting("WORK_DIR", WORK))
        existing = set(json.loads((work / "existing-keys.json").read_text()) or [])
        check_append_only(output_keys(tid).values(), exists=lambda k: k in existing)
    except (ConfigError, OSError, ValueError) as exc:
        log("config-error", error=str(exc)[:300])
        return 1
    except AppendOnlyError as exc:
        log("append-only", error=str(exc)[:300])
        return 3
    namespace, start, step = take["namespace"], take["replay_start_ms"], take["step_s"]
    schedule = frame_schedule(take["total_ms"], step)
    frames, out = work / "frames", work / "out"
    frames.mkdir(parents=True, exist_ok=True)
    out.mkdir(parents=True, exist_ok=True)
    pending = frames / "pending.png"
    retried: list[dict[str, Any]] = []
    began = time.monotonic()
    log("render-start", take_id=tid, namespace=namespace, frames=len(schedule), step_s=step, span_ms=take["total_ms"])

    # Installed in the render container only (PLAYWRIGHT_PIP), not on the workstation that builds the Jobs.
    from playwright.sync_api import sync_playwright  # type: ignore[import-not-found,unused-ignore]

    status, error = "ok", ""
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        try:
            context = browser.new_context(viewport={"width": VIEW["width"], "height": VIEW["height"]},
                                          extra_http_headers={"Authorization": auth}, color_scheme="dark")
            page = context.new_page()
            page.set_default_navigation_timeout(NAV_TIMEOUT_MS)
            for i, t in enumerate(schedule):
                url = frame_url(base, uid, start, t, namespace)

                def attempt(n: int, url: str = url) -> dict[str, Any]:
                    page.goto(url, wait_until="networkidle")
                    for title in EXPECTED_PANELS:
                        page.get_by_test_id(PANEL_TESTID + title).wait_for(state="visible",
                                                                           timeout=PANEL_WAIT_S * 1000)
                    probe = page.evaluate(PROBE_JS, list(EXPECTED_PANELS))
                    page.screenshot(path=str(pending))
                    return {"namespace_picker": probe.get("namespace_picker"),
                            "panels": {k: probe.get("panels", {}).get(k, {"no_data": True, "missing": True})
                                       for k in EXPECTED_PANELS}}

                def rejected(n: int, reason: str, i: int = i, t: int = t) -> None:
                    log("frame-rejected", frame=i, t_ms=t, attempt=n, reason=reason)

                try:
                    frame, attempts = render_with_retry(attempt, namespace=namespace, t_ms=t, on_reject=rejected)
                except FrameError as exc:
                    status, error = "failed", str(exc)[:500]
                    log("frame-failed", frame=i, t_ms=t, error=error)
                    break
                pending.replace(frames / f"frame-{i:05d}.png")
                if attempts > 1:
                    retried.append({"frame": i, "t_ms": t, "attempts": attempts})
                if i % 20 == 0 or i == len(schedule) - 1:
                    spent = time.monotonic() - began
                    log("frame", frame=i, of=len(schedule), t_ms=t, attempts=attempts,
                        picker=frame["namespace_picker"], elapsed_s=round(spent),
                        eta_s=round(spent / (i + 1) * (len(schedule) - i - 1)))
        finally:
            browser.close()
    report = {"status": status, "take_id": tid, "frames": len(schedule) if status == "ok" else 0,
              "retries": sum(r["attempts"] - 1 for r in retried), "retried_frames": retried, "step_s": step,
              "span_ms": take["total_ms"], "elapsed_s": round(time.monotonic() - began, 1), "error": error}
    (work / "render.json").write_text(json.dumps(report, indent=1))
    log("render-done", **{k: v for k, v in report.items() if k != "retried_frames"})
    return 0 if status == "ok" else 2


def _webm_duration_s(path: Path) -> float | None:
    try:
        return float(path.read_text().strip())
    except (OSError, ValueError):
        return None


def meta_main() -> int:
    """Write <take>-capture.json and the take record next to the encoded webm in WORK_DIR/out.

    Returns:
        0 written, 1 bad configuration, 3 no usable render or webm.
    """
    try:
        take = _take_from_env()
        bucket = _setting("RESULTS_BUCKET")
        work = Path(_setting("WORK_DIR", WORK))
        render = json.loads((work / "render.json").read_text())
    except (ConfigError, OSError, ValueError) as exc:
        log("config-error", error=str(exc)[:300])
        return 1
    tid, step, start = rerender_take_id(take["take_id"]), take["step_s"], take["replay_start_ms"]
    keys = output_keys(tid)
    out = work / "out"
    webm = out / keys["webm"].rsplit("/", 1)[-1]
    size = webm.stat().st_size if webm.exists() else 0
    n = int(render.get("frames") or 0)
    duration = _webm_duration_s(work / "webm-duration.txt")
    if render.get("status") != "ok" or n == 0 or size == 0:
        log("meta-refused", render_status=render.get("status"), frames=n, webm_bytes=size)
        return 3
    if duration is not None and abs(duration - n * step) > step:
        log("meta-refused", reason="webm duration is not frames x step", webm_duration_s=duration, want_s=n * step)
        return 3
    stats = {"webm_duration_s": duration, "render_elapsed_s": render.get("elapsed_s"),
             "retried_frames": render.get("retried_frames"), "rendered_at": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                                                                         time.gmtime())}
    capture = {"take_id": tid, "status": "ok", **capture_meta(start, n, step), "from_ms": start,
               "to_ms": start + take["total_ms"], "namespace": take["namespace"], "dashboard_uid": DASHBOARD_UID,
               "step_s": step, "frames": n, "webm_bytes": size, "webm_duration_s": duration, "method": METHOD,
               "source_take_id": take["take_id"], "result_of_record": False}
    record = take_record(source=take, n_frames=n, retries=int(render.get("retries") or 0),
                         webm_uri=f"s3://{bucket}/{keys['webm']}", bytes_=size, extra=stats)
    (out / keys["capture"].rsplit("/", 1)[-1]).write_text(json.dumps(capture, indent=1))
    (out / keys["record"].rsplit("/", 1)[-1]).write_text(json.dumps(record, indent=1))
    log("meta-done", take_id=tid, frames=n, webm_bytes=size, webm_duration_s=duration)
    return 0


# ================================================================ workstation commands
def jobs_main(args: argparse.Namespace) -> int:
    """Write the two re-render Jobs, the compose spec and the compose Job (JSON) from the live compose.json.

    Every manifest is built before the first file is written, so a missing setting or a bad input writes nothing.

    Args:
        args: source_compose, out_dir, drain_s, per_frame_s, budget_s, target_s, hard_stop_epoch, platform_env.

    Returns:
        0.

    Raises:
        ConfigError: A setting (SETTINGS) is missing from both the environment and the platform config.
        ValueError: An input cell has no take (TAKE_LABELS) or an input range is invalid.
        KeyError: A take cell is missing from the input.
    """
    with platform_defaults(args.platform_env):
        missing = [k for k in SETTINGS if not os.environ.get(k, "").strip()]
        if missing:
            raise ConfigError(f"{', '.join(missing)} not set (environment or --platform-env)")
        bucket = _setting("RESULTS_BUCKET")
        src = json.loads(Path(args.source_compose).read_text())
        takes: dict[str, dict[str, Any]] = {}
        for i in src["inputs"]:
            cell = str(i["cell"])
            if cell not in CELL_NAMESPACE:
                raise ValueError(f"input cell {cell!r} has no take (take cells: {sorted(CELL_NAMESPACE)})")
            takes[cell] = {"cell": cell, "take_id": i["take_id"], "namespace": CELL_NAMESPACE[cell],
                           "replay_start_ms": int(i["replay_start_ms"]), "replay_end_ms": int(i["replay_end_ms"]),
                           "sync_ms": int(i["sync_ms"]), "webm_uri": i["webm_uri"],
                           "total_ms": total_ms(int(i["replay_start_ms"]), int(i["replay_end_ms"]), args.drain_s)}
        step = max(choose_step_s(t["total_ms"], args.per_frame_s, args.budget_s) for t in takes.values())
        now = time.time()
        hard_stop = args.hard_stop_epoch or int(now) + 4 * 3600
        jobs: list[dict[str, Any]] = []
        for t in takes.values():
            t["step_s"] = step
            t["rerender_webm_uri"] = f"s3://{bucket}/{output_keys(rerender_take_id(t['take_id']))['webm']}"
            jobs.append(rerender_job(t, hard_stop_epoch=hard_stop, now=now))
        spec = compose_spec(takes, target_s=args.target_s)
        compose = compose_job(spec, hard_stop_epoch=hard_stop, now=now)
    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    for t, job in zip(takes.values(), jobs, strict=True):
        (out / f"{job['metadata']['name']}.json").write_text(json.dumps(job, indent=1))
        frames = len(frame_schedule(t["total_ms"], step))
        log("rerender-job", job=job["metadata"]["name"], namespace=t["namespace"], step_s=step, frames=frames,
            estimate_s=round(frames * args.per_frame_s))
    (out / "takes.json").write_text(json.dumps(takes, indent=1))
    (out / "compose-spec.json").write_text(json.dumps(spec, indent=1))
    (out / f"{compose['metadata']['name']}.json").write_text(json.dumps(compose, indent=1))
    log("compose-job", job=compose["metadata"]["name"], outputs=spec["outputs"], hard_stop_epoch=hard_stop)
    return 0


def compose_record_main(args: argparse.Namespace) -> int:
    """Write the compose marker (compose.json) of a complete compose Job.

    Args:
        args: spec, source_compose_uri, out, platform_env.

    Returns:
        0.

    Raises:
        ConfigError: BENCH_REPO is missing from both the environment and the platform config.
    """
    spec = json.loads(Path(args.spec).read_text())
    with platform_defaults(args.platform_env):
        images = {"fetch_upload": COMPOSE_AWS_IMAGE, "render": ffmpeg_image()}
    record = {"status": "ok", "run_id": spec["run_id"], "inputs": spec["inputs"], "outputs": spec["outputs"],
              "target_s": spec["target_s"], "job": f"{TAKES_NS}/{compose_job_name(spec['run_id'])}",
              "images": images, "result_of_record": False, "method": METHOD, "note": spec["note"],
              "source_compose": args.source_compose_uri, "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    Path(args.out).write_text(json.dumps(record, indent=1))
    log("compose-record", out=args.out)
    return 0


def main(argv: list[str] | None = None) -> int:
    """Command-line entry point.

    Args:
        argv: Arguments (default: sys.argv[1:]).

    Returns:
        The process exit code.
    """
    logging.basicConfig(level=logging.INFO, format="%(asctime)s rerender %(message)s", stream=sys.stdout)
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("render", help="in-pod: render the frames")
    sub.add_parser("meta", help="in-pod: capture.json + take record")
    jobs = sub.add_parser("jobs", help="workstation: write the Job manifests")
    jobs.add_argument("--source-compose", required=True, help="compose.json of the live takes")
    jobs.add_argument("--out-dir", required=True)
    jobs.add_argument("--drain-s", type=int, default=DEFAULT_DRAIN_S)
    jobs.add_argument("--per-frame-s", type=float, default=4.0, help="expected render time of one frame")
    jobs.add_argument("--budget-s", type=float, default=1700.0, help="render time budget per take")
    jobs.add_argument("--target-s", type=int, default=COMPOSE_TARGET_S)
    jobs.add_argument("--hard-stop-epoch", type=int, default=0, help="default: now + 4 h")
    rec = sub.add_parser("compose-record", help="workstation: write compose.json of a complete compose Job")
    rec.add_argument("--spec", required=True)
    rec.add_argument("--source-compose-uri", required=True)
    rec.add_argument("--out", required=True)
    for p in (jobs, rec):
        p.add_argument("--platform-env", metavar="FILE", default=None,
                       help=f"KEY=VALUE file filling the unset {', '.join(SETTINGS)} (the environment wins)")
    args = parser.parse_args(argv)
    if args.cmd == "render":
        return render_main()
    if args.cmd == "meta":
        return meta_main()
    try:
        if args.cmd == "jobs":
            return jobs_main(args)
        return compose_record_main(args)
    except ConfigError as exc:
        log("config-error", error=str(exc)[:300])
        parser.exit(1, f"rerender {args.cmd}: {exc}\n")


if __name__ == "__main__":
    sys.exit(main())
