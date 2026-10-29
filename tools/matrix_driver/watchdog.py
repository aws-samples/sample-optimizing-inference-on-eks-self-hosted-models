"""Independent hard-stop watchdog of the Qwen3.8 matrix (CronJob bench/q38-matrix-watchdog, SA matrix-driver).

Stdlib + kubectl only; it never reads the driver's state, ConfigMap or process. Inside an action window it deletes
every q38 matrix DGD (label app.kubernetes.io/part-of=qwen38-matrix) in the 4 scenario namespaces, then the take
capture Jobs (app.kubernetes.io/name=demo-capture), the take compose Jobs (app.kubernetes.io/name=take-compose) and
the driver Jobs (app.kubernetes.io/name=q38-matrix-driver) in bench, waits until they and the DGD pods are gone and
logs the state. Outside every window it does nothing.

v4 (E7' takes): the windows come from WINDOWS ("<start>/<end>[,...]", the matrix 04:30-05:00Z and the E7'
09:00-10:00Z of 2026-10-24), else from the legacy single [HARD_STOP, WINDOW_END). A window does not act while a
driver Job whose q38-matrix/hard-stop annotation is LATER than the window start is still active: that later plan
(E7', started ~20:00Z of 10-23) is legitimately running and its own window stops it. If the driver Jobs cannot be
listed: when a later window exists (so a later plan may be running) the firing holds and exits 1 (the CronJob retries,
then the next 5-min firing) for the first LIST_FAIL_HOLD_S of the window, then acts; in the last window it acts at
once (fail-safe towards freeing the GPU: no later plan can exist, the driver refuses a plan without its own window).
Exit 0 = nothing left or no action due, 1 = something still present after the wait, or held on a listing failure.
"""

from __future__ import annotations

import calendar
import json
import os
import subprocess
import sys
import time
from collections.abc import Mapping
from typing import Any

NAMESPACES = ["s-agg", "s-agg-hic", "s-dis", "s-dis-hic"]
PART_OF = "app.kubernetes.io/part-of=qwen38-matrix"
DRIVER_JOB = "app.kubernetes.io/name=q38-matrix-driver"
CAPTURE_JOB = "app.kubernetes.io/name=demo-capture"
COMPOSE_JOB = "app.kubernetes.io/name=take-compose"
JOB_SELECTORS = (CAPTURE_JOB, COMPOSE_JOB, DRIVER_JOB)  # deleted in this order, after the DGDs
HARD_STOP_ANNOTATION = "q38-matrix/hard-stop"
DGD_POD = "nvidia.com/dynamo-graph-deployment-name=q38"
WAIT_S = 600
LIST_FAIL_HOLD_S = 900  # first 15 min of a non-last window: hold on a driver-Job listing failure (3 firings)


def iso_epoch(ts: str) -> int:
    """Epoch seconds of a YYYY-MM-DDTHH:MM:SSZ instant (ValueError when malformed)."""
    return calendar.timegm(time.strptime(ts, "%Y-%m-%dT%H:%M:%SZ"))


def log(msg: str, **kw: Any) -> None:
    """One JSON log line."""
    print(json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "watchdog": "q38-matrix", "msg": msg,
                      **kw}, default=str), flush=True)


def decide(now: float, hard_stop: int, window_end: int) -> str:
    """before-hard-stop | act | after-window (one window, v3)."""
    if now < hard_stop:
        return "before-hard-stop"
    if now >= window_end:
        return "after-window"
    return "act"


def parse_windows(text: str) -> list[tuple[int, int]]:
    """``<start>/<end>[,<start>/<end>...]`` (UTC instants) as sorted (start_epoch, end_epoch) pairs.

    Raises:
        ValueError: Empty text, a malformed item or instant, an empty window (end <= start) or a duplicate start.
    """
    out: list[tuple[int, int]] = []
    for item in text.split(","):
        parts = item.strip().split("/")
        if len(parts) != 2:
            raise ValueError(f"window {item.strip()!r} is not <start>/<end>")
        start, end = iso_epoch(parts[0]), iso_epoch(parts[1])
        if end <= start:
            raise ValueError(f"window {item.strip()!r} is empty (end <= start)")
        if any(s == start for s, _ in out):
            raise ValueError(f"duplicate window start {parts[0]}")
        out.append((start, end))
    return sorted(out)


def windows_from_env(env: Mapping[str, str]) -> list[tuple[int, int]]:
    """WINDOWS when set, else the legacy single [HARD_STOP, WINDOW_END) (KeyError when neither is set)."""
    if env.get("WINDOWS", "").strip():
        return parse_windows(env["WINDOWS"])
    return [(iso_epoch(env["HARD_STOP"]), iso_epoch(env["WINDOW_END"]))]


def decide_windows(now: float, windows: list[tuple[int, int]]) -> str:
    """act inside any window, before-hard-stop while a window still lies ahead, after-window when all passed."""
    if any(s <= now < e for s, e in windows):
        return "act"
    if any(now < s for s, _ in windows):
        return "before-hard-stop"
    return "after-window"


def current_window(now: float, windows: list[tuple[int, int]]) -> tuple[int, int]:
    """The window ``now`` falls in (call only when decide_windows says act)."""
    return next((s, e) for s, e in windows if s <= now < e)


def window_applies(window_start: int, active_driver_job_hard_stops: list[int]) -> bool:
    """False while a driver Job with a later hard stop than this window is active (do not kill that plan here)."""
    return not any(hs > window_start for hs in active_driver_job_hard_stops)


def hold_on_list_failure(now: float, window_start: int, windows: list[tuple[int, int]]) -> bool:
    """True when the driver Jobs could not be listed and acting might kill a later plan: a later window exists and
    the window opened less than LIST_FAIL_HOLD_S ago (fail closed, bounded); False in the last window or after it."""
    return any(s > window_start for s, _ in windows) and now < window_start + LIST_FAIL_HOLD_S


def delete_commands(namespaces: list[str]) -> list[list[str]]:
    """The label-scoped deletes (never a name: kubectl refuses name + selector): DGDs first, then the capture,
    compose and driver Jobs."""
    cmds = [["kubectl", "delete", "dgd", "-n", ns, "-l", PART_OF, "--ignore-not-found", "--wait=false"]
            for ns in namespaces]
    return [*cmds, *(["kubectl", "delete", "job", "-n", "bench", "-l", sel, "--ignore-not-found", "--wait=false"]
                     for sel in JOB_SELECTORS)]


def run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    """Run one kubectl command (never through a shell)."""
    # The one process call site. Every argv is a list of module constants (NAMESPACES, the selectors, "bench"):
    # nothing from the environment, the API or a plan reaches it.
    # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-audit, dangerous-subprocess-use-audit -- argv of module constants only
    return subprocess.run(cmd, capture_output=True, text=True, timeout=120, check=False)  # nosec B603 -- constant argv, no shell


def remaining() -> dict[str, list[str]]:
    """What is still present: q38 matrix DGDs, their pods, driver/capture/compose Jobs."""
    out: dict[str, list[str]] = {"dgds": [], "pods": [], "jobs": []}
    for ns in NAMESPACES:
        for kind, sel, key in (("dgd", PART_OF, "dgds"), ("pods", DGD_POD, "pods")):
            p = run(["kubectl", "get", kind, "-n", ns, "-l", sel, "-o", "name"])
            out[key] += [f"{ns}/{x}" for x in p.stdout.split()] if p.returncode == 0 else [f"{ns}: get failed"]
    for sel in JOB_SELECTORS:
        p = run(["kubectl", "get", "job", "-n", "bench", "-l", sel, "-o", "name"])
        out["jobs"] += p.stdout.split() if p.returncode == 0 else [f"bench {sel}: get failed"]
    return out


def active_driver_hard_stops() -> list[int] | None:
    """Hard stops (annotation q38-matrix/hard-stop) of the driver Jobs that have not finished; None when the
    Jobs cannot be listed. A Job without a parseable annotation counts as no later plan."""
    p = run(["kubectl", "get", "job", "-n", "bench", "-l", DRIVER_JOB, "-o", "json"])
    if p.returncode != 0:
        return None
    out: list[int] = []
    for job in json.loads(p.stdout).get("items", []):
        conds = (job.get("status") or {}).get("conditions") or []
        if any(c.get("type") in ("Complete", "Failed") and c.get("status") == "True" for c in conds):
            continue
        try:
            out.append(iso_epoch((job.get("metadata", {}).get("annotations") or {}).get(HARD_STOP_ANNOTATION, "")))
        except ValueError:
            continue
    return out


def main() -> int:
    """One CronJob firing."""
    windows = windows_from_env(os.environ)
    now = time.time()
    what = decide_windows(now, windows)
    log("decision", decision=what, windows=[[time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t)) for t in w]
                                            for w in windows])
    if what != "act":
        return 0
    start, _ = current_window(now, windows)
    later = active_driver_hard_stops()
    if later is None:
        if hold_on_list_failure(now, start, windows):
            log("held: driver Jobs not listable and a later window exists; retried by the next firing",
                window_start=start, hold_until=start + LIST_FAIL_HOLD_S)
            return 1
        log("driver Jobs not listable: acting (fail-safe)")
    elif not window_applies(start, later):
        log("held: a driver Job with a later hard stop is active", window_start=start, active_hard_stops=later)
        return 0
    log("before", **remaining())
    for cmd in delete_commands(NAMESPACES):
        p = run(cmd)
        log("delete", cmd=" ".join(cmd[1:]), rc=p.returncode, out=p.stdout.strip()[:500], err=p.stderr.strip()[:300])
    deadline = time.time() + WAIT_S
    left = remaining()
    while any(left.values()) and time.time() < deadline:
        time.sleep(10)
        left = remaining()
    log("after", clean=not any(left.values()), **left)
    return 0 if not any(left.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
