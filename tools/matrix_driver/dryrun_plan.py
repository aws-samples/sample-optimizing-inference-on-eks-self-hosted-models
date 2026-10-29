# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Offline dry-run of a shipped plan on the fake cluster (no cluster, no S3, no GPU): the driver's real control flow
with injected failures, printing its JSON log, the cluster actions, the S3 keys and the step statuses.

Run: PYTHONPATH=<demo_bench> python3 dryrun_plan.py --plan ../../plans/w2-long-context/plan-e4.txt [--broken-deploys s-agg=1]
     [--hard-stop-in-s N] [--sigterm-at S7/rep1] [--trace w2.jsonl --sidecar w2.side.jsonl] [--env platform.env]
The plan's ${KEY} placeholders are expanded from manifests/config/platform.env (--env) first, as render_driver.py
does for the in-cluster plans ConfigMap.
The e3 SLO lock that e4/e5/e6 need is a FAKE object made here (dry-run only); every latency is synthetic.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

import fake_cluster as fc
import matrix_driver as md
import render_driver as rd


class DryDriver(fc.TDriver):
    """TDriver that can send itself SIGTERM during one run's aiperf."""

    sigterm_at = ""

    def start_aiperf(self, cmd: list[str], log_fh: Any, cwd: Path) -> Any:
        self.aiperf_sigterm = bool(self.sigterm_at) and str(cwd).endswith(self.sigterm_at)
        if self.aiperf_sigterm:
            self.aiperf_duration_s = 10**6
        return super().start_aiperf(cmd, log_fh, cwd)


def seed_fake_e3_lock(s3: fc.FakeS3, plan: md.Plan) -> None:
    """A fake e3 calibration + lock matching the plan's pin (dry-run only)."""
    assert plan.slo_ref is not None
    cal = json.dumps({"rule": "slo-calibration", "status": "FAKE dry-run calibration, not data",
                      "slo": {"ttft_s": 2.0, "itl_ms": 25.0, "cold_factor": 1.4, "cold_prefill_tok_s": 1.0}}).encode()
    s3.objects[md.s3_key(plan.slo_ref["from"])] = cal
    s3.objects[md.s3_key(plan.slo_ref["lock"])] = json.dumps({
        "plan_sha256": plan.slo_ref["e3_plan_sha256"], "slo_calibration_key": md.s3_key(plan.slo_ref["from"]),
        "slo_calibration_sha256": hashlib.sha256(cal).hexdigest(), "slo": json.loads(cal)["slo"],
        "note": "FAKE dry-run lock"}).encode()


def main() -> int:
    """One dry-run."""
    ap = argparse.ArgumentParser()
    ap.add_argument("--plan", type=Path, required=True)
    ap.add_argument("--broken-deploys", default="", help="ns=N: the next N fresh deploys of ns keep the cache on flush")
    ap.add_argument("--hard-stop-in-s", type=int, default=0, help="replace the plan hard stop by now + N s")
    ap.add_argument("--sigterm-at", default="", help="cell/repK whose aiperf receives SIGTERM")
    ap.add_argument("--trace", type=Path)
    ap.add_argument("--sidecar", type=Path)
    ap.add_argument("--env", type=Path, default=rd.PLATFORM_ENV, help="platform.env the ${KEY} placeholders come from")
    a = ap.parse_args()
    text = rd.expand_plan(a.plan.read_text(), rd.load_platform_env(a.env))
    if a.hard_stop_in_s:
        hs = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + a.hard_stop_in_s))
        text = re.sub(r"(?m)^hard_stop \S+$", f"hard_stop {hs}", text)
        print(f"# dry-run: hard_stop replaced by {hs}")
    plan = md.parse_plan(text)
    bucket = plan.w2["trace"].split("/")[2]
    DryDriver.sigterm_at = a.sigterm_at
    with tempfile.TemporaryDirectory(prefix="q38-dryrun-") as tmp:
        trace = (a.trace, a.sidecar) if a.trace and a.sidecar else None
        with fc.offline(text, Path(tmp), bucket=bucket, trace=trace, driver_cls=DryDriver) as (d, cluster, s3):
            if plan.slo_ref:
                seed_fake_e3_lock(s3, plan)
            if a.broken_deploys:
                ns, n = a.broken_deploys.split("=")
                cluster.broken_deploys[ns] = int(n)
            print(f"# dry-run: plan {a.plan.name} sha256 {plan.source_sha256} trace rows {len(d.trace.read_text().splitlines())}")
            rc = d.execute()
            summary = json.loads(next(v for k, v in s3.objects.items() if "/_driver/driver-summary-" in k))
    print("=== cluster actions (in order)")
    for e in cluster.events:
        print("  ", " ".join(e))
    print("=== S3 puts (in order)")
    for k in s3.puts:
        print("  ", k)
    print("=== steps")
    for s in summary["steps"]:
        print("  ", json.dumps({k: s.get(k) for k in ("line", "kind", "cell", "rep", "status", "valid", "error",
                                                       "teardown_after_failure") if s.get(k) is not None}))
    print("=== aborted:", summary.get("aborted"), "| cut_by_priority_I:", summary.get("cut_by_priority_I"),
          "| refused:", summary.get("refused"))
    print("=== rc", rc)
    return 0


if __name__ == "__main__":
    sys.exit(main())
