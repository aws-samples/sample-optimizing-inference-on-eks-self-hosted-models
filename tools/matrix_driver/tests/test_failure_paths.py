# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Failure paths of the matrix driver on the fake cluster (critic fixes 1, 5, 6, 7): forced non-zero canary after the
reset (run invalid, topology torn down, next run blocked or re-booted), hard stop and SIGTERM (teardown), pre-run
exclusivity (fatal), preflight refusals (RBAC, watchdog, Job deadline, SLO lock) before any cluster change.

Run: PYTHONPATH=<demo_bench> python3 -m unittest -v test_failure_paths.py   (from this directory)
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

import fake_cluster as fc
import matrix_driver as md

SHA = "a" * 64
MUTATING = ("apply", "delete-dgd", "toggle", "recreate-frontend", "create-cleanup-pod", "delete-cleanup-pod")


def hard_stop_in(seconds: int) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + seconds))


def plan(steps: str, *, extra: str = "", hs: str | None = None, run_id: str = "q38-t",
         slo: str = "slo ttft_s=2 itl_ms=25 cold_factor=1.4 cold_prefill_tok_s=30000") -> str:
    return (f"run_id {run_id}\nhard_stop {hs or hard_stop_in(86400)}\n{extra}"
            f"w2 trace=s3://test-bucket/t.jsonl trace_sha256={SHA} sidecar=s3://test-bucket/s.jsonl "
            f"sidecar_sha256={SHA} frozen=yes\ntokenizer prefix=s3://test-bucket/tok/ sums_sha256={SHA}\n{slo}\n"
            "estimate boot_s=1 reset_s=1 drain_s=1 probe_s=1\n" + steps + "\n")


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="q38-fail-"))

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp)

    def events(self, cluster: fc.FakeCluster, kinds: tuple[str, ...] = MUTATING) -> list[tuple[str, ...]]:
        return [e for e in cluster.events if e[0] in kinds]

    def summary(self, s3: fc.FakeS3, run_id: str = "q38-t") -> dict[str, Any]:
        key = next(k for k in s3.objects if k.startswith(f"matrix/{run_id}/_driver/driver-summary-"))
        return json.loads(s3.objects[key])


class ResetFailureTests(Base):
    def test_unit_nonzero_canary_after_reset_fails_verification_after_one_frontend_recreate(self) -> None:
        with fc.offline(plan("run S1 1"), self.tmp) as (d, cluster, _):
            cluster.flush_broken.add("s-agg")
            d.ensure("S1", self.tmp / "run")
            fe_before = next(p["uid"] for p in cluster.dgds["s-agg"]["pods"] if p["role"] == "frontend")
            with self.assertRaises(md.StepError) as ctx:
                d.reset_and_verify("t-agg", "S1/rep1", self.tmp / "run")
            self.assertIn("repeat_cached_zero", str(ctx.exception))
            doc = json.loads((self.tmp / "run" / "reset.json").read_text())
            self.assertFalse(doc["ok"])
            self.assertGreater(sum(doc["repeat_usage_cached_tokens"]), 0)
            self.assertEqual(doc["frontend_uid_before"], fe_before)
            self.assertNotEqual(doc["frontend_uid_after"], fe_before)  # T-RTR-2 fallback recreated it once
            self.assertEqual(len(self.events(cluster, ("recreate-frontend",))), 1)

    def test_forced_canary_hit_marks_the_run_invalid_and_blocks_the_next_run(self) -> None:
        with fc.offline(plan("run S0 1\nrun S7 1", extra="on_failure abort\n"), self.tmp) as (d, cluster, s3):
            cluster.broken_deploys["s-agg"] = 1
            rc = d.execute()
        self.assertEqual(rc, 1)
        summ = self.summary(s3)
        steps = summ["steps"]
        self.assertEqual(steps[0]["status"], "failed")
        self.assertIn("T-RTR-2", steps[0]["error"])
        self.assertTrue(steps[0]["invalid"])
        self.assertEqual(steps[0]["teardown_after_failure"]["torn_down"], ["t-agg"])
        self.assertEqual(steps[1]["status"], "blocked")  # never started
        self.assertIn("on_failure=abort", summ["aborted"])
        # evidence of the invalid run kept apart; its results prefix untouched (no claim, no run.json)
        self.assertTrue(any(k.startswith("matrix/q38-t/_failed/S0/rep1/") and k.endswith("reset.json") for k in s3.objects))
        self.assertFalse(any(k.startswith("matrix/q38-t/S0/rep1/") for k in s3.objects))
        # S0 is rr: no router-predicted hit, so no Frontend recreate; the poisoned DGD is torn down, S7 never applied
        self.assertEqual(self.events(cluster), [("apply", "s-agg", "S0"), ("delete-dgd", "s-agg")])

    def test_continue_mode_tears_down_and_reboots_before_the_next_run_of_that_cell(self) -> None:
        with fc.offline(plan("run S0 1\nrun S0 2"), self.tmp) as (d, cluster, s3):
            cluster.broken_deploys["s-agg"] = 1
            rc = d.execute()
        self.assertEqual(rc, 1)
        kinds = [e[0] for e in self.events(cluster, ("apply", "delete-dgd"))]
        self.assertEqual(kinds, ["apply", "delete-dgd", "apply"])  # re-boot, never reuse the poisoned DGD
        run2 = json.loads(s3.objects["matrix/q38-t/S0/rep2/run.json"])
        self.assertEqual(run2["deploy"]["action"], "deploy")
        self.assertTrue(run2["validity"]["valid"], run2["validity"])
        self.assertTrue(run2["reset"]["ok"])

    def test_completed_but_invalid_run_also_forces_a_reboot(self) -> None:
        with fc.offline(plan("run S2 1\nrun S4 1"), self.tmp) as (d, cluster, s3):
            d.aiperf_rc = 1
            rc = d.execute()
        self.assertEqual(rc, 1)
        run1 = json.loads(s3.objects["matrix/q38-t/S2/rep1/run.json"])
        self.assertFalse(run1["validity"]["valid"])
        self.assertEqual([e[0] for e in self.events(cluster, ("apply", "delete-dgd", "toggle"))],
                         ["apply", "delete-dgd", "apply", "delete-dgd"])  # S4 not a Frontend toggle on the old DGD
        cleanups = self.events(cluster, ("create-cleanup-pod",))
        self.assertGreaterEqual(len(cleanups), 2)  # L3 dir cleaned before each deploy and at each teardown
        self.assertTrue(all(e[1] == "s-agg-hic" for e in cleanups))


class HardStopTests(Base):
    def test_hard_stop_cuts_aiperf_marks_the_run_and_tears_down(self) -> None:
        with fc.offline(plan("run S0 1\nrun S1 1", hs=hard_stop_in(120)), self.tmp) as (d, cluster, s3):
            d.aiperf_duration_s = 10**6
            rc = d.execute()
        self.assertEqual(rc, 3)
        run = json.loads(s3.objects["matrix/q38-t/S0/rep1/run.json"])
        self.assertIn("cut by the hard stop", run["validity"]["reasons"])
        self.assertEqual(self.events(cluster, ("apply", "delete-dgd"))[-1], ("delete-dgd", "s-agg"))
        self.assertIn("hard-stop", self.summary(s3)["aborted"])

    def test_guard_refuses_a_run_that_cannot_finish_and_tears_down_live_dgds(self) -> None:
        text = plan("run S0 1", hs=hard_stop_in(60)).replace("boot_s=1 reset_s=1 drain_s=1", "boot_s=900 reset_s=1 drain_s=1")
        with fc.offline(text, self.tmp) as (d, cluster, s3):
            cluster.kubectl("apply", "-f", str(fc.DGD_DIR / "S0.yaml"))  # a q38 DGD left by an earlier launch
            rc = d.execute()
        self.assertEqual(rc, 3)
        self.assertEqual(self.events(cluster, ("apply", "delete-dgd")), [("apply", "s-agg", "S0"), ("delete-dgd", "s-agg")])
        self.assertEqual(self.summary(s3)["steps"][0]["status"], "cut-by-hard-stop")

    def test_sigterm_from_the_job_deadline_or_the_watchdog_tears_down(self) -> None:
        with fc.offline(plan("run S0 1\nrun S1 1"), self.tmp) as (d, cluster, s3):
            d.aiperf_sigterm = True
            d.aiperf_duration_s = 10**6
            rc = d.execute()
        self.assertEqual(rc, 3)
        self.assertIn("SIGTERM", self.summary(s3)["aborted"])
        self.assertEqual(self.events(cluster, ("apply", "delete-dgd"))[-1], ("delete-dgd", "s-agg"))
        run = json.loads(s3.objects["matrix/q38-t/S0/rep1/run.json"])
        self.assertFalse(run["validity"]["valid"])


class CutOrderExecutionTests(Base):
    def test_priority_cut_is_decided_per_run_and_never_drops_rep1_or_unlisted_cells(self) -> None:
        text = plan("run S6 1\nrun S6 2\nrun S2 1", extra="cut_order S6\n", hs=hard_stop_in(1500)).replace(
            "drain_s=1", "drain_s=1000")
        with fc.offline(text, self.tmp) as (d, _, s3):
            rc = d.execute()
        self.assertEqual(rc, 0)
        steps = self.summary(s3)["steps"]
        self.assertEqual([s["status"] for s in steps], ["done", "cut-by-priority (I)", "done"])
        self.assertEqual(self.summary(s3)["cut_by_priority_I"], ["S6/rep2"])


class PreflightTests(Base):
    def assert_refused_without_cluster_change(self, cluster: fc.FakeCluster, rc: int) -> None:
        self.assertEqual(rc, 2)
        self.assertEqual(self.events(cluster), [])

    def test_rbac_denied_refuses(self) -> None:
        with fc.offline(plan("run S0 1"), self.tmp) as (d, cluster, _):
            cluster.deny.add(("delete", "pods", "s-dis-hic"))
            rc = d.execute()
        self.assert_refused_without_cluster_change(cluster, rc)

    def test_missing_watchdog_refuses(self) -> None:
        with fc.offline(plan("run S0 1"), self.tmp) as (d, cluster, _):
            cluster.cronjob_present = False
            rc = d.execute()
        self.assert_refused_without_cluster_change(cluster, rc)

    def test_job_deadline_after_the_hard_stop_refuses(self) -> None:
        with fc.offline(plan("run S0 1"), self.tmp) as (d, cluster, _):
            cluster.job["spec"]["activeDeadlineSeconds"] += 7200
            rc = d.execute()
        self.assert_refused_without_cluster_change(cluster, rc)

    def lock_plan(self) -> str:
        return plan("run S0 1", run_id="q38-t4", slo="slo from=s3://test-bucket/matrix/q38-e3/_slo/slo-calibration.json "
                    f"lock=s3://test-bucket/matrix/q38-e3/_slo/slo-lock.json e3_plan_sha256={SHA}")

    def seed_lock(self, s3: fc.FakeS3, cal: bytes, lock_cal: bytes | None = None) -> None:
        s3.objects["matrix/q38-e3/_slo/slo-calibration.json"] = cal
        s3.objects["matrix/q38-e3/_slo/slo-lock.json"] = json.dumps({
            "plan_sha256": SHA, "slo_calibration_key": "matrix/q38-e3/_slo/slo-calibration.json",
            "slo_calibration_sha256": hashlib.sha256(lock_cal or cal).hexdigest(),
            "slo": json.loads(lock_cal or cal)["slo"]}).encode()

    def test_e4_refuses_without_the_e3_lock(self) -> None:
        with fc.offline(self.lock_plan(), self.tmp) as (d, cluster, _):
            rc = d.execute()
        self.assert_refused_without_cluster_change(cluster, rc)

    def test_e4_refuses_a_slo_file_that_does_not_match_the_e3_lock(self) -> None:
        good = json.dumps({"slo": {"ttft_s": 4.2, "itl_ms": 25.0, "cold_factor": 1.4, "cold_prefill_tok_s": 3e4}}).encode()
        bad = json.dumps({"slo": {"ttft_s": 9.0, "itl_ms": 25.0, "cold_factor": 1.4, "cold_prefill_tok_s": 3e4}}).encode()
        with fc.offline(self.lock_plan(), self.tmp) as (d, cluster, s3):
            self.seed_lock(s3, bad, lock_cal=good)
            rc = d.execute()
        self.assert_refused_without_cluster_change(cluster, rc)

    def test_e4_with_a_matching_lock_runs_and_records_the_plan_lock_first(self) -> None:
        cal = json.dumps({"slo": {"ttft_s": 4.2, "itl_ms": 25.0, "cold_factor": 1.4, "cold_prefill_tok_s": 3e4}}).encode()
        with fc.offline(self.lock_plan(), self.tmp) as (d, _, s3):
            self.seed_lock(s3, cal)
            rc = d.execute()
            self.assertEqual(d.slo["ttft_s"], 4.2)
        self.assertEqual(rc, 0)
        first = s3.puts[0]
        self.assertTrue(first.startswith("matrix/q38-t4/_lock/plan-lock-"), s3.puts[:3])
        lock = json.loads(s3.objects[first])
        self.assertEqual(lock["slo_calibration_sha256"], hashlib.sha256(cal).hexdigest())
        self.assertEqual(lock["plan_sha256"], d.plan.source_sha256)


class PreflightRefusalMessageTests(Base):
    """The preflight refusals name this repo's renderer (tools/matrix_driver/render_driver.py), not the original
    render_cells.py or its rendered/driver/ output directory."""

    RENDER = "tools/matrix_driver/render_driver.py"

    def assert_names_the_repo_renderer(self, msg: str) -> None:
        self.assertIn(self.RENDER, msg)
        self.assertNotIn("render_cells.py", msg)
        self.assertNotIn("rendered/driver/", msg)

    def test_an_unset_job_name_points_to_the_render_driver_launch(self) -> None:
        with fc.offline(plan("run S0 1"), self.tmp) as (d, _, _s3), mock.patch.dict(os.environ):
            os.environ.pop("JOB_NAME")
            with self.assertRaises(md.ConfigError) as ctx:
                d.check_job_deadline()
        self.assert_names_the_repo_renderer(str(ctx.exception))
        self.assertIn(f"{self.RENDER} --launch", str(ctx.exception))

    def test_a_missing_watchdog_points_to_the_rendered_20_watchdog(self) -> None:
        with fc.offline(plan("run S0 1"), self.tmp) as (d, cluster, _):
            cluster.cronjob_present = False
            with self.assertRaises(md.ConfigError) as ctx:
                d.check_watchdog()
        self.assert_names_the_repo_renderer(str(ctx.exception))
        self.assertIn("20-watchdog.yaml", str(ctx.exception))

    def test_a_watchdog_without_the_plan_window_points_to_the_rendered_20_watchdog(self) -> None:
        with fc.offline(plan("run S0 1"), self.tmp) as (d, cluster, _):
            cluster.cron_env = [{"name": "HARD_STOP", "value": "2000-01-01T00:00:00Z"}]
            with self.assertRaises(md.ConfigError) as ctx:
                d.check_watchdog()
        self.assert_names_the_repo_renderer(str(ctx.exception))
        self.assertIn("20-watchdog.yaml", str(ctx.exception))


class SloLockWriteTests(Base):
    def test_e3_writes_the_slo_lock_before_the_first_s7_run(self) -> None:
        text = plan("run S0 1 label=pilot\ncalibrate-slo after=S0/rep1\nrun S7 1 label=smoke", run_id="q38-e3x",
                    extra="on_failure abort\n")
        with fc.offline(text, self.tmp) as (d, _, s3):
            rc = d.execute()
        self.assertEqual(rc, 0)
        lock_key = "matrix/q38-e3x/_slo/slo-lock.json"
        lock = json.loads(s3.objects[lock_key])
        cal = s3.objects["matrix/q38-e3x/_slo/slo-calibration.json"]
        self.assertEqual(lock["slo_calibration_sha256"], hashlib.sha256(cal).hexdigest())
        self.assertEqual(lock["plan_sha256"], hashlib.sha256(text.encode()).hexdigest())
        self.assertLess(s3.puts.index(lock_key), s3.puts.index("matrix/q38-e3x/S7/rep1/claim.json"))
        self.assertEqual(lock["slo"], fc.TDriver.calibration_slo)


class ExclusivityTests(Base):
    def test_another_q38_dgd_anywhere_is_fatal(self) -> None:
        with fc.offline(plan("run S0 1\nrun S1 1"), self.tmp) as (d, cluster, s3):
            cluster.foreign_dgds.append({"metadata": {"name": "q38", "namespace": "q38-other", "labels": {}}})
            rc = d.execute()
        self.assertEqual(rc, 1)
        self.assertEqual(self.events(cluster, ("apply",)), [])
        self.assertIn("fatal", self.summary(s3)["aborted"])

    def test_foreign_gpu_pods_are_fatal(self) -> None:
        with fc.offline(plan("run S0 1"), self.tmp) as (d, cluster, _):
            cluster.foreign_gpu_pods = 1
            rc = d.execute()
        self.assertEqual(rc, 1)
        self.assertEqual(self.events(cluster, ("apply",)), [])

    def test_two_engines_on_one_gpu_fail_the_deploy_and_tear_it_down(self) -> None:
        with fc.offline(plan("run S3 1"), self.tmp) as (d, cluster, s3):
            cluster.dup_gpu = True
            rc = d.execute()
        self.assertEqual(rc, 1)
        step = self.summary(s3)["steps"][0]
        self.assertIn("GPU uniqueness", step["error"])
        self.assertEqual([e[0] for e in self.events(cluster, ("apply", "delete-dgd"))], ["apply", "delete-dgd"])


class EvidenceTests(Base):
    def test_a_valid_pd_hicache_run_records_the_critic_evidence(self) -> None:
        with fc.offline(plan("run S7 1\nteardown"), self.tmp) as (d, _, s3):
            rc = d.execute()
        self.assertEqual(rc, 0)
        run = json.loads(s3.objects["matrix/q38-t/S7/rep1/run.json"])
        self.assertTrue(run["validity"]["valid"], run["validity"])
        ev = run["evidence"]
        self.assertTrue(ev["gpu_identity"]["uniqueness"]["ok"])
        self.assertEqual(len(ev["gpu_identity"]["per_pod"]), 8)
        self.assertEqual(len(ev["boot_facts"]), 8)
        self.assertTrue(all(f["libfabric_backend"] for f in ev["boot_facts"].values()))
        self.assertIn("frontend_uid_before", run["reset"])
        self.assertIn("frontend_uid_after", run["reset"])
        k = run["kpis"]
        self.assertEqual(sorted(k["pd_transport"]["per_engine_kv_transfer"]), sorted(ev["boot_facts"]))
        self.assertIn("eth0_delta", k["pd_transport"])
        self.assertIn("storage_tier", k)
        self.assertEqual(k["storage_tier"]["label"], "storage tier (page cache or NVMe)")
        self.assertIn("md127", k["node"]["diskstats_delta"])
        self.assertIn("MemFree", k["node"]["meminfo_kb"]["pre"])
        self.assertEqual(k["router_requests"]["with_worker_id"], 20)
        self.assertEqual(sum(k["router_requests"]["prefill_pods"].values()), 20)
        self.assertTrue(any(key.endswith("S7/rep1/worker-ids.jsonl") for key in s3.objects))
        self.assertFalse(any("raw_records" in key for key in s3.objects))  # raw export (prompts) never uploaded
        td = [json.loads(v) for key, v in s3.objects.items() if "/_teardown/" in key]
        self.assertTrue(any(t["topology"] == "t-pd-hic" and t["l3_cleanup"]["files_after"] == 0
                            and "df_bytes_avail" in t["l3_cleanup"] for t in td))


if __name__ == "__main__":
    unittest.main()
