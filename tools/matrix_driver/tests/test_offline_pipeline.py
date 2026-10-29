# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Offline pipeline test: the driver's post-run path on synthetic aiperf output (no cluster, no S3).

Exercises install_demo_bench (wheel sha + pip --target + import precedence), check_trace on the candidate W2 window
trace, run_validity (demo_bench collapsed outputs + 0.5 % rule), goodput (demo_bench slo-goodput subprocess,
pilot band) and kpis on a real E1 worker scrape. Env: W2_TRACE, W2_SIDECAR (window files), E1_PROM (a worker
/metrics dump), WHEEL_DIR (demo_bench wheel + demo_bench.whl.sha256). Synthetic latencies only, no real data.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import shutil
import tempfile
import unittest
from pathlib import Path

import matrix_driver as md
from support import shipped_plan_text

NEEDS = ("WHEEL_DIR", "W2_TRACE", "W2_SIDECAR", "E1_PROM")


@unittest.skipUnless(all(os.environ.get(k) for k in NEEDS),
                     "needs the recorded E1/W2 inputs (not in the repo): " + ", ".join(NEEDS))
class OfflinePipeline(unittest.TestCase):
    tmp: Path
    plan: md.Plan
    d: md.Driver

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = Path(tempfile.mkdtemp(prefix="q38-offline-"))
        drv = cls.tmp / "driver"
        drv.mkdir()
        wheel_dir = Path(os.environ["WHEEL_DIR"])
        sha, name = (wheel_dir / "demo_bench.whl.sha256").read_text().split()
        shutil.copy(wheel_dir / name, drv / name)
        (drv / "wheel.sha256").write_text(f"{sha}  {name}\n")
        os.environ.update({"WORK": str(cls.tmp / "work"), "DRIVER_DIR": str(drv), "RESULTS_BUCKET": "offline-test",
                           "CB_NODE": "offline", "AWS_REGION": "test-region-1"})
        text = shipped_plan_text("plan-e3")
        cls.plan = md.parse_plan(text)
        cls.d = md.Driver(cls.plan, argparse.Namespace())  # v2 plan-e3 (frozen W2, explicit candidate SLO)
        cls.d.trace, cls.d.sidecar = Path(os.environ["W2_TRACE"]), Path(os.environ["W2_SIDECAR"])
        cls.d.work.mkdir(parents=True)

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.tmp)

    def fake_run(self, run_dir: Path, *, collapse: int, drop: int, ttft_s: float) -> None:
        rows = [json.loads(x) for x in self.d.trace.read_text().splitlines()]
        rng = random.Random(7)
        out = []
        t = 1_000_000_000
        for i, r in enumerate(rows[: len(rows) - drop]):
            osl = 1 if i < collapse else r["output_length"]
            out.append({"metadata": {"benchmark_phase": "profiling", "request_start_ns": t + i * 1_000_000,
                                     "request_end_ns": t + i * 1_000_000 + 5_000_000_000},
                        "metrics": {"time_to_first_token": {"value": rng.uniform(0.2, 2 * ttft_s) * 1000, "unit": "ms"},
                                    "inter_token_latency": {"value": rng.uniform(5, 12), "unit": "ms"},
                                    "output_sequence_length": {"value": osl, "unit": "tokens"}},
                        "error": None})
        (run_dir / "aiperf").mkdir(parents=True)
        (run_dir / "aiperf" / "profile_export.jsonl").write_text("".join(json.dumps(x) + "\n" for x in out))

    def test_pipeline(self) -> None:
        info = self.d.install_demo_bench()
        self.assertIn(str(self.d.work / "pylib"), info["path"])
        tr = self.d.check_trace()
        self.assertEqual(tr["rows"], 920)
        self.assertEqual(self.d.trace_rows, 920)

        run_dir = self.d.work / "runs" / "S0" / "rep1"
        self.fake_run(run_dir, collapse=4, drop=0, ttft_s=2.0)
        rec = {"aiperf": {"rc": 0}, "restarted": [], "reset": {"ok": True}, "kpis": {}}
        v = self.d.run_validity(rec, run_dir, cut=False)
        self.assertEqual(v["collapsed"], 4)
        self.assertTrue(v["valid"], v)  # 4/920 <= 0.5 %

        run2 = self.d.work / "runs" / "S0" / "rep2"
        self.fake_run(run2, collapse=4, drop=2, ttft_s=2.0)
        v2 = self.d.run_validity(rec, run2, cut=False)
        self.assertFalse(v2["valid"])  # 4 collapsed + 2 missing = 6/920 > 0.5 %
        self.assertEqual(v2["missing_rows"], 2)

        self.d.slo = {"ttft_s": "2.0", "itl_ms": "25", "cold_factor": "1.4", "cold_prefill_tok_s": "25000"}
        g = self.d.goodput(run_dir, md.Step("run", "S0", 1, "pilot"))
        self.assertIsNotNone(g)
        assert g is not None
        self.assertIsNotNone(g["goodput"], g)
        self.assertIn("calibration", g)
        self.assertEqual(g["classes"]["continuation"]["n"] + g["classes"]["cold_ingestion"]["n"], 920 - 195)

        prom = md.parse_metrics(Path(os.environ["E1_PROM"]).read_text())
        post = {k: v * 2 for k, v in prom.items()}
        disk = {"reads": 0, "read_bytes": 0, "writes": 0, "write_bytes": 0}
        k = self.d.kpis("t-agg-hic", pre={"w:a": prom, "frontend": {}}, post={"w:a": post, "frontend": {}},
                        roles={"w:a": "worker"}, node_pre={"diskstats": {"md127": disk}, "meminfo_kb": {"Cached": 1}},
                        node_post={"diskstats": {"md127": {**disk, "read_bytes": 10**9}}, "meminfo_kb": {"Cached": 2}},
                        run_dir=run_dir)
        tot = k["engine"]["total"]
        self.assertGreater(tot["engine_prompt_tokens"], 0)
        st = k["storage_tier"]
        self.assertEqual(st["label"], "storage tier (page cache or NVMe)")
        self.assertEqual(st["md127_read_bytes_delta"], 10**9)
        self.assertEqual(st["page_cache_proxy_I"]["status"], "computed (I)")
        self.assertIn(st["page_cache_proxy_I"]["label"], ("nvme", "page-cache assisted"))


if __name__ == "__main__":
    unittest.main()
