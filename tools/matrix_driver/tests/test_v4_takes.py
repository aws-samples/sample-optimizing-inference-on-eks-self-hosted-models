# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""v4 (E7' recorded takes) tests of the matrix driver, written BEFORE the implementation (RED).

Contract under test (everything below is what the developer must implement in matrix_driver.py; no test touches a
real cluster, S3 or GPU - the fake cluster of fake_cluster.py answers every kubectl call):

Plan grammar (parse_plan)
  take <S0|S7> <rep> [label=] [retake_of=<cell>/rep<k>] [min_left_s=<n>]   Step.kind == "take", same key as a run
  compose [target_s=<n>]                                                     Step.kind == "compose" (default 170)
  retake_of and min_left_s come together; a plan may have take steps and no `run` step.

Pure helpers
  DRIVER_VERSION == "q38-matrix-driver/4"; TAKE_LABELS; CAPTURE_LEAD_S; COMPOSE_TARGET_S == 170
  take_id(run_id, cell, rep) -> "<run_id>-<cell lower>-rep<k>"
  take_window(replay_start_epoch, trace_span_s, drain_s, lead_s=CAPTURE_LEAD_S) -> {from_ms, to_ms, duration_s, lead_s}
  capture_job(*, run_id, cell, rep, window, hard_stop_epoch, now) -> Job dict (bench/demo-capture-<take_id>)
  select_takes(entries) -> {"S0": entry, "S7": entry}   (StepError when a cell has no valid take with a good capture)
  compose_spec(run_id, selected) -> dict ; compose_job(spec, *, hard_stop_epoch, now) -> Job dict (name has "compose")

Driver behaviour (Driver.execute on the fake cluster)
  take = run + one capture Job created (kubectl create -f - with the Job as JSON on stdin, namespace bench) BEFORE aiperf
  starts; run.json gains a "take" block; capture failure never invalidates the run; retake_of / min_left_s gating;
  compose creates one Job and writes outputs + compose.json under takes/<run_id>/compose/ (append-only).

Run (from this directory):
  PYTHONPATH=../wheel/demo_bench-0.1.0-py3-none-any.whl WHEEL_DIR=../wheel python3 -m unittest -v test_v4_takes
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

import fake_cluster as fc
import matrix_driver as md
from support import shipped_plan_text
import test_failure_paths as tfp

HERE = Path(__file__).resolve().parent
DIGEST = re.compile(r"@sha256:[0-9a-f]{64}$")
DNS1123 = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")
CAPTURE_DIGEST = "sha256:684df257a1fa0ef3fa288239819984008f560e8f8db5b635ee9e2a50cbcd2e23"  # CAPTURE_IMAGE of matrix_driver.py


def plan(steps: str, **kw: Any) -> str:
    """Same plan template as the failure-path tests (explicit numeric SLO)."""
    return tfp.plan(steps, **kw)


def env_of(job: dict[str, Any]) -> dict[str, Any]:
    ctr = job["spec"]["template"]["spec"]["containers"][0]
    return {e["name"]: e for e in ctr["env"]}


class Base(tfp.Base):
    def scratch(self) -> Path:
        """A temp dir removed after the test (the Base one is for the first driver of a test)."""
        path = Path(tempfile.mkdtemp(prefix="q38-take-"))
        self.addCleanup(shutil.rmtree, path, ignore_errors=True)
        return path

    def run_json(self, s3: fc.FakeS3, key: str, run_id: str = "q38-t") -> dict[str, Any]:
        return json.loads(s3.objects[f"matrix/{run_id}/{key}/run.json"])

    def jobs_created(self, cluster: fc.FakeCluster) -> list[str]:
        return [e[1] for e in cluster.events if e[0] == "create-job"]


# ================================================================ plan grammar
class PlanGrammarTests(unittest.TestCase):
    def test_the_shipped_plan_e7_parses_with_the_agreed_shape(self) -> None:
        p = md.parse_plan(shipped_plan_text("plan-e7"))
        self.assertEqual(p.run_id, "q38-e7")
        self.assertEqual(p.hard_stop, "2026-10-24T09:00:00Z")
        self.assertEqual(p.on_failure, "continue")  # an invalid take must not abort the retakes
        self.assertEqual([(s.kind, s.cell, s.rep) for s in p.steps],
                         [("take", "S0", 1), ("take", "S0", 2), ("take", "S7", 1), ("take", "S7", 2),
                          ("compose", "", 0), ("teardown", "", 0)])
        self.assertEqual(p.steps[1].opts["retake_of"], "S0/rep1")
        self.assertEqual(p.steps[3].opts["retake_of"], "S7/rep1")
        self.assertTrue(all(int(s.opts["min_left_s"]) > 0 for s in p.steps[1::2][:2]))
        self.assertEqual(p.steps[4].opts.get("target_s"), "170")

    def test_plan_e7_uses_the_same_frozen_inputs_and_slo_lock_as_the_matrix(self) -> None:
        e7, e4 = md.parse_plan(shipped_plan_text("plan-e7")), md.parse_plan(shipped_plan_text("plan-e4"))
        self.assertEqual(e7.w2, e4.w2)  # same frozen trace
        self.assertEqual(e7.tokenizer, e4.tokenizer)
        self.assertEqual(e7.slo_ref, e4.slo_ref)  # same SLO lock (e3 calibration)
        self.assertEqual(e7.estimate, e4.estimate)

    def test_take_is_a_run_like_step_with_a_run_key(self) -> None:
        p = md.parse_plan(plan("take S0 1 label=first\ntake S7 1"))
        self.assertEqual([s.kind for s in p.steps], ["take", "take"])
        self.assertEqual(p.steps[0].key, "S0/rep1")
        self.assertEqual(p.steps[0].label, "first")

    def test_a_plan_with_only_takes_is_valid(self) -> None:
        md.parse_plan(plan("take S0 1"))

    def test_compose_after_both_cells_parses(self) -> None:
        p = md.parse_plan(plan("take S0 1\ntake S7 1\ncompose\nteardown"))
        self.assertEqual(p.steps[2].kind, "compose")
        p2 = md.parse_plan(plan("take S0 1\ntake S7 1\ncompose target_s=200"))
        self.assertEqual(p2.steps[2].opts["target_s"], "200")

    def test_retake_with_a_time_guard_parses(self) -> None:
        p = md.parse_plan(plan("take S0 1\ntake S0 2 retake_of=S0/rep1 min_left_s=3600"))
        self.assertEqual(p.steps[1].opts, {"retake_of": "S0/rep1", "min_left_s": "3600"})

    def test_ill_formed_steps_are_rejected(self) -> None:
        bad = {
            "no args": "take",
            "no rep": "take S0",
            "rep zero": "take S0 0",
            "rep not a number": "take S0 x",
            "unknown cell": "take S9 1",
            "cell without a take dashboard (S3)": "take S3 1",
            "cell not a take cell (S1)": "take S1 1",
            "unknown option": "take S0 1 speed=2",
            "duplicate take": "take S0 1\ntake S0 1",
            "take duplicates a run key": "run S0 1\ntake S0 1",
            "retake without min_left_s": "take S0 1\ntake S0 2 retake_of=S0/rep1",
            "min_left_s without retake_of": "take S0 1\ntake S0 2 min_left_s=3600",
            "min_left_s zero": "take S0 1\ntake S0 2 retake_of=S0/rep1 min_left_s=0",
            "min_left_s not a number": "take S0 1\ntake S0 2 retake_of=S0/rep1 min_left_s=soon",
            "retake_of names no earlier take": "take S0 2 retake_of=S0/rep1 min_left_s=3600",
            "retake_of names a later take": "take S0 2 retake_of=S0/rep3 min_left_s=3600\ntake S0 3",
            "retake_of names another cell": "take S0 1\ntake S7 1\ntake S7 2 retake_of=S0/rep1 min_left_s=3600",
            "retake_of names a plain run": "run S0 1\ntake S0 2 retake_of=S0/rep1 min_left_s=3600",
            "retake_of malformed": "take S0 1\ntake S0 2 retake_of=S0 min_left_s=3600",
            "compose with positional args": "take S0 1\ntake S7 1\ncompose S0 S7",
            "compose unknown option": "take S0 1\ntake S7 1\ncompose speed=4",
            "compose target_s zero": "take S0 1\ntake S7 1\ncompose target_s=0",
            "compose target_s not a number": "take S0 1\ntake S7 1\ncompose target_s=fast",
            "compose before any take": "compose\ntake S0 1\ntake S7 1",
            "compose with only S0 takes": "take S0 1\ncompose",
            "compose with only S7 takes": "take S7 1\ncompose",
            "compose twice": "take S0 1\ntake S7 1\ncompose\ncompose",
        }
        for name, steps in bad.items():
            with self.subTest(name), self.assertRaises(md.ConfigError):
                md.parse_plan(plan(steps))

    def test_compose_without_any_take_or_run_is_still_a_plan_error(self) -> None:
        with self.assertRaises(md.ConfigError):
            md.parse_plan(plan("compose"))
        with self.assertRaises(md.ConfigError):
            md.parse_plan(plan("teardown"))

    def test_probe_slo_needs_the_probe_before_the_first_take(self) -> None:
        bad = plan("take S0 1\nprobe-cold-prefill S0", slo="slo ttft_s=2 itl_ms=25 cold_factor=1.4 cold_prefill_tok_s=probe")
        with self.assertRaises(md.ConfigError):
            md.parse_plan(bad)


# ================================================================ pure helpers
class ConstantsTests(unittest.TestCase):
    def test_driver_version_is_4(self) -> None:
        self.assertEqual(md.DRIVER_VERSION, "q38-matrix-driver/4")

    def test_take_labels_are_the_agreed_wording(self) -> None:
        self.assertEqual(md.TAKE_LABELS["S0"], "Baseline: aggregated + round-robin")
        self.assertEqual(md.TAKE_LABELS["S7"], "Router + HiCache + PD disaggregation")
        self.assertEqual(set(md.TAKE_LABELS), {"S0", "S7"})

    def test_lead_and_target(self) -> None:
        self.assertTrue(60 <= md.CAPTURE_LEAD_S <= 300, md.CAPTURE_LEAD_S)
        self.assertEqual(md.COMPOSE_TARGET_S, 170)

    def test_permissions_include_creating_jobs_in_bench(self) -> None:
        self.assertIn(("create", "jobs.batch", "", "bench"), md.required_permissions())
        self.assertIn(("get", "jobs.batch", "", "bench"), md.required_permissions())  # unchanged


class TakeIdAndWindowTests(unittest.TestCase):
    def test_take_id(self) -> None:
        self.assertEqual(md.take_id("q38-e7", "S0", 1), "q38-e7-s0-rep1")
        self.assertEqual(md.take_id("q38-e7", "S7", 12), "q38-e7-s7-rep12")
        self.assertTrue(DNS1123.match(f"demo-capture-{md.take_id('q38-e7', 'S7', 12)}"))
        self.assertLessEqual(len(f"demo-capture-{md.take_id('q38-e7', 'S7', 12)}"), 63)

    def test_window_is_derived_from_the_trace_not_the_block_1_default(self) -> None:
        # W2: 920 rows at 0.5 rows/s = 1840 s; drain 600 s (the plan estimate)
        w = md.take_window(1_790_000_000.0, 1840.0, 600)
        self.assertEqual(w["from_ms"], 1_790_000_000_000)
        self.assertEqual(w["to_ms"] - w["from_ms"], (1840 + 600) * 1000)
        self.assertEqual(w["lead_s"], md.CAPTURE_LEAD_S)
        self.assertEqual(w["duration_s"], 1840 + 600 + md.CAPTURE_LEAD_S)
        self.assertNotEqual(w["duration_s"], 1020)
        self.assertGreater(w["duration_s"], 31 * 60)  # covers the whole ~31 min replay + drain

    def test_window_rounds_up_fractional_spans(self) -> None:
        w = md.take_window(1000.0, 9.5, 1, lead_s=10)
        self.assertEqual(w["duration_s"], math.ceil(9.5 + 1) + 10)
        self.assertEqual(w["to_ms"] - w["from_ms"], 10_500)

    def test_window_rejects_nonsense(self) -> None:
        for span, drain in ((0.0, 10), (-1.0, 10), (100.0, -1)):
            with self.subTest(span=span, drain=drain), self.assertRaises(ValueError):
                md.take_window(1000.0, span, drain)


class CaptureJobTests(unittest.TestCase):
    NOW = 1_790_000_000.0

    def make(self, cell: str = "S0", rep: int = 1, *, hard_stop_in: int = 20000) -> dict[str, Any]:
        window = md.take_window(self.NOW + md.CAPTURE_LEAD_S, 1840.0, 600)
        with mock.patch.dict("os.environ", {"RESULTS_BUCKET": "test-bucket", "AWS_REGION": fc.REGION,
                                                  "COMPOSE_FFMPEG_IMAGE": fc.COMPOSE_FFMPEG_IMAGE}):
            return md.capture_job(run_id="q38-e7", cell=cell, rep=rep, window=window,
                                  hard_stop_epoch=int(self.NOW) + hard_stop_in, now=self.NOW)

    def test_identity_and_links_to_the_run(self) -> None:
        job = self.make("S7", 2)
        self.assertEqual(job["kind"], "Job")
        self.assertEqual(job["metadata"]["namespace"], "bench")
        self.assertEqual(job["metadata"]["name"], "demo-capture-q38-e7-s7-rep2")
        for labels in (job["metadata"]["labels"], job["spec"]["template"]["metadata"]["labels"]):
            self.assertEqual(labels["app.kubernetes.io/name"], "demo-capture")
            self.assertEqual(labels["demo.kubecon/take-id"], "q38-e7-s7-rep2")
            self.assertEqual(labels["q38-matrix/run-id"], "q38-e7")
            self.assertEqual(labels["q38-matrix/cell"], "S7")
            self.assertEqual(labels["q38-matrix/rep"], "2")

    def test_environment_carries_the_cell_namespace_and_the_derived_window(self) -> None:
        for cell, ns in (("S0", "s-agg"), ("S7", "s-dis-hic")):
            job = self.make(cell)
            env = env_of(job)
            window = md.take_window(self.NOW + md.CAPTURE_LEAD_S, 1840.0, 600)
            self.assertEqual(env["NAMESPACE"]["value"], ns)
            self.assertEqual(env["FROM_MS"]["value"], str(window["from_ms"]))
            self.assertEqual(env["TO_MS"]["value"], str(window["to_ms"]))
            self.assertEqual(env["DURATION_S"]["value"], str(window["duration_s"]))
            self.assertNotEqual(env["DURATION_S"]["value"], "1020")
            self.assertEqual(env["TAKE_ID"]["value"], f"q38-e7-{cell.lower()}-rep1")
            self.assertEqual(env["DASHBOARD_UID"]["value"], "demo-take")
            self.assertEqual(env["S3_PREFIX"]["value"], "takes/q38-e7")  # webm lands in takes/<run_id>/<take_id>/
            self.assertEqual(env["RESULTS_BUCKET"]["value"], "test-bucket")
            self.assertEqual(env["AWS_REGION"]["value"], fc.REGION)

    def test_grafana_credentials_come_only_from_the_existing_secret(self) -> None:
        env = env_of(self.make())
        ref = env["GRAFANA_PASSWORD"]["valueFrom"]["secretKeyRef"]
        self.assertEqual((ref["name"], ref["key"]), ("demo-grafana-auth", "password"))
        self.assertNotIn("value", env["GRAFANA_PASSWORD"])
        self.assertEqual(env["GRAFANA_USER"]["value"], "admin")

    def test_image_pinned_by_digest_and_pod_hardening(self) -> None:
        job = self.make()
        pod = job["spec"]["template"]["spec"]
        ctr = pod["containers"][0]
        self.assertTrue(ctr["image"].endswith(CAPTURE_DIGEST), ctr["image"])
        self.assertEqual(pod["serviceAccountName"], "matrix-driver")  # Pod Identity writes the results bucket
        self.assertEqual(pod["nodeSelector"], {"karpenter.sh/nodepool": "cpu-demo"})  # never the GPU node
        self.assertNotIn("nvidia.com/gpu", json.dumps(ctr["resources"]))
        self.assertTrue(pod["securityContext"]["runAsNonRoot"])
        self.assertEqual(pod["restartPolicy"], "Never")
        self.assertEqual(job["spec"]["backoffLimit"], 0)
        self.assertEqual(ctr["securityContext"]["allowPrivilegeEscalation"], False)
        vols = {v["name"]: v for v in pod["volumes"]}
        self.assertEqual(vols["capture-script"]["configMap"]["name"], "q38-take-capture")

    def test_deadline_covers_the_recording_and_never_passes_the_hard_stop(self) -> None:
        job = self.make(hard_stop_in=20000)
        ads = job["spec"]["activeDeadlineSeconds"]
        self.assertGreater(ads, 1840 + 600 + md.CAPTURE_LEAD_S)
        self.assertLessEqual(ads, 20000)
        tight = self.make(hard_stop_in=3000)
        self.assertLessEqual(tight["spec"]["activeDeadlineSeconds"], 3000)

    def test_a_hard_stop_already_passed_refuses(self) -> None:
        with self.assertRaises(md.StepError):
            self.make(hard_stop_in=-5)


def entry(cell: str, rep: int, *, valid: bool = True, capture: str = "ok", status: str = "done",
          start_ms: int = 1_000) -> dict[str, Any]:
    tid = md.take_id("q38-e7", cell, rep)
    return {"cell": cell, "rep": rep, "kind": "take", "status": status, "valid": valid, "take_capture": capture,
            "take_id": tid, "webm_uri": f"s3://test-bucket/takes/q38-e7/{tid}/{tid}.webm", "replay_start_ms": start_ms + rep}


class SelectTakesTests(unittest.TestCase):
    def test_picks_the_latest_valid_take_with_a_good_capture_per_cell(self) -> None:
        got = md.select_takes([entry("S0", 1), entry("S0", 2), entry("S7", 1)])
        self.assertEqual({c: e["rep"] for c, e in got.items()}, {"S0": 2, "S7": 1})

    def test_a_retake_replaces_a_failed_capture(self) -> None:
        got = md.select_takes([entry("S0", 1, capture="failed"), entry("S0", 2), entry("S7", 1)])
        self.assertEqual(got["S0"]["rep"], 2)

    def test_invalid_runs_failed_captures_and_skipped_steps_are_never_selected(self) -> None:
        got = md.select_takes([entry("S0", 1), entry("S0", 2, valid=False), entry("S0", 3, capture="failed"),
                               entry("S0", 4, status="skipped-not-needed", capture=""), entry("S7", 1)])
        self.assertEqual(got["S0"]["rep"], 1)

    def test_a_cell_without_a_usable_take_is_an_error_naming_the_cell(self) -> None:
        with self.assertRaises(md.StepError) as ctx:
            md.select_takes([entry("S0", 1), entry("S7", 1, capture="failed")])
        self.assertIn("S7", str(ctx.exception))
        with self.assertRaises(md.StepError) as ctx2:
            md.select_takes([entry("S7", 1)])
        self.assertIn("S0", str(ctx2.exception))


class ComposeSpecAndJobTests(unittest.TestCase):
    NOW = 1_790_000_000.0

    def selected(self) -> dict[str, dict[str, Any]]:
        return {"S0": entry("S0", 2), "S7": entry("S7", 1)}

    def spec(self, target_s: int | None = None) -> dict[str, Any]:
        with mock.patch.dict("os.environ", {"RESULTS_BUCKET": "test-bucket", "AWS_REGION": fc.REGION,
                                                  "COMPOSE_FFMPEG_IMAGE": fc.COMPOSE_FFMPEG_IMAGE}):
            if target_s is None:
                return md.compose_spec("q38-e7", self.selected())
            return md.compose_spec("q38-e7", self.selected(), target_s=target_s)

    def test_inputs_are_the_two_take_webms_baseline_left_candidate_right(self) -> None:
        spec = self.spec()
        self.assertEqual([i["cell"] for i in spec["inputs"]], ["S0", "S7"])
        sel = self.selected()
        for inp in spec["inputs"]:
            self.assertEqual(inp["webm_uri"], sel[inp["cell"]]["webm_uri"])
            self.assertEqual(inp["take_id"], sel[inp["cell"]]["take_id"])
            self.assertEqual(inp["replay_start_ms"], sel[inp["cell"]]["replay_start_ms"])  # sync on replay start
            self.assertEqual(inp["label"], md.TAKE_LABELS[inp["cell"]])
        self.assertEqual(spec["run_id"], "q38-e7")
        self.assertEqual(spec["target_s"], 170)
        self.assertEqual(self.spec(200)["target_s"], 200)

    def test_outputs_are_three_distinct_append_only_paths_outside_the_inputs(self) -> None:
        spec = self.spec()
        self.assertEqual(set(spec["outputs"]), {"full", "deck", "still"})
        uris = list(spec["outputs"].values())
        self.assertEqual(len(set(uris)), 3)
        for uri in uris:
            self.assertTrue(uri.startswith("s3://test-bucket/takes/q38-e7/compose/"), uri)
        base = "s3://test-bucket/takes/q38-e7/compose/"
        self.assertEqual(spec["outputs"], {"full": base + "side-by-side-full.mp4", "deck": base + "side-by-side-deck.mp4",
                                           "still": base + "three-panel-still.png"})
        for inp in spec["inputs"]:
            self.assertNotIn(inp["webm_uri"], uris)

    def job(self, hard_stop_in: int = 20000) -> dict[str, Any]:
        with mock.patch.dict("os.environ", {"RESULTS_BUCKET": "test-bucket", "AWS_REGION": fc.REGION,
                                                  "COMPOSE_FFMPEG_IMAGE": fc.COMPOSE_FFMPEG_IMAGE}):
            return md.compose_job(self.spec(), hard_stop_epoch=int(self.NOW) + hard_stop_in, now=self.NOW)

    def test_job_identity_links_and_the_spec_travels_in_the_environment(self) -> None:
        job = self.job()
        self.assertEqual(job["kind"], "Job")
        self.assertEqual(job["metadata"]["namespace"], "bench")
        self.assertIn("compose", job["metadata"]["name"])
        self.assertLessEqual(len(job["metadata"]["name"]), 63)
        self.assertTrue(DNS1123.match(job["metadata"]["name"]))
        labels = job["metadata"]["labels"]
        self.assertEqual(labels["app.kubernetes.io/name"], "take-compose")
        self.assertEqual(labels["q38-matrix/run-id"], "q38-e7")
        self.assertEqual(json.loads(env_of(job)["COMPOSE_SPEC"]["value"]), self.spec())

    def test_image_is_pinned_by_digest_from_a_public_or_mirrored_ecr(self) -> None:
        image = self.job()["spec"]["template"]["spec"]["containers"][0]["image"]
        self.assertRegex(image, DIGEST)
        self.assertTrue(image.startswith(("public.ecr.aws/", "mirror.test/")), image)
        self.assertNotIn(":latest", image)

    def test_hardening_cpu_node_and_deadline(self) -> None:
        job = self.job(hard_stop_in=20000)
        pod = job["spec"]["template"]["spec"]
        self.assertEqual(pod["serviceAccountName"], "matrix-driver")
        self.assertEqual(pod["nodeSelector"], {"karpenter.sh/nodepool": "cpu-demo"})
        self.assertNotIn("nvidia.com/gpu", json.dumps(pod["containers"][0]["resources"]))
        self.assertTrue(pod["securityContext"]["runAsNonRoot"])
        self.assertEqual(job["spec"]["backoffLimit"], 0)
        self.assertTrue(0 < job["spec"]["activeDeadlineSeconds"] <= 20000)
        self.assertLessEqual(self.job(hard_stop_in=900)["spec"]["activeDeadlineSeconds"], 900)
        vols = {v["name"]: v for v in pod["volumes"]}
        self.assertTrue(any(v.get("configMap", {}).get("name") == "q38-take-capture" for v in vols.values()))

    def test_a_hard_stop_already_passed_refuses(self) -> None:
        with self.assertRaises(md.StepError):
            self.job(hard_stop_in=-1)


# ================================================================ driver behaviour on the fake cluster
class TakeRunTests(Base):
    def fresh(self, steps: str, **kw: Any) -> Any:
        return fc.offline(plan(steps, **kw), self.tmp)

    def test_take_is_a_normal_run_plus_a_take_block(self) -> None:
        with fc.offline(plan("run S0 1"), self.scratch()) as (d, _, s3):
            self.assertEqual(d.execute(), 0)
            base = self.run_json(s3, "S0/rep1")
        with self.fresh("take S0 1") as (d, cluster, s3):
            cluster.s3 = s3
            rc = d.execute()
            took = self.run_json(s3, "S0/rep1")
        self.assertEqual(rc, 0)
        self.assertTrue(took["validity"]["valid"], took["validity"])
        self.assertEqual(set(took) - set(base), {"take"})  # same record, one extra block
        self.assertTrue(set(base) <= set(took))
        self.assertTrue(took["reset"]["ok"])  # reset-and-verify ran like for a run
        self.assertEqual(took["slo_lock"], base["slo_lock"])
        self.assertEqual(took["driver"], "q38-matrix-driver/4")

    def test_take_block_links_the_capture_job_to_the_run(self) -> None:
        with self.fresh("take S0 1") as (d, cluster, s3):
            cluster.s3 = s3
            self.assertEqual(d.execute(), 0)
            run = self.run_json(s3, "S0/rep1")
            span = d.trace_span_s
        take = run["take"]
        self.assertEqual(take["take_id"], "q38-t-s0-rep1")
        self.assertIs(take["result_of_record"], False)  # spec section 8: takes are not results of record
        cap = take["capture"]
        self.assertEqual(cap["status"], "ok")
        self.assertEqual(cap["job"], "bench/demo-capture-q38-t-s0-rep1")
        self.assertEqual(cap["namespace"], "s-agg")
        self.assertEqual(cap["webm_uri"], "s3://test-bucket/takes/q38-t/q38-t-s0-rep1/q38-t-s0-rep1.webm")
        started = md.iso_epoch(run["aiperf_started_at"])
        self.assertLessEqual(abs(cap["from_ms"] / 1000 - started), md.CAPTURE_LEAD_S + 5)
        self.assertEqual(cap["to_ms"] - cap["from_ms"], round((span + 1) * 1000))  # span + plan drain_s (1)
        self.assertEqual(cap["duration_s"], math.ceil(span + 1) + md.CAPTURE_LEAD_S)
        self.assertEqual(cap["replay_start_ms"], cap["from_ms"])

    def test_exactly_one_capture_job_created_before_aiperf_starts(self) -> None:
        with self.fresh("take S7 1") as (d, cluster, s3):
            cluster.s3 = s3
            self.assertEqual(d.execute(), 0)
            ev = list(cluster.events)
            jobs = dict(cluster.jobs)
        self.assertEqual(self.jobs_created(cluster), ["demo-capture-q38-t-s7-rep1"])
        names = [e[0] for e in ev]
        self.assertLess(names.index("create-job"), names.index("aiperf-start"))
        self.assertLess(names.index("apply"), names.index("create-job"))  # the DGD is up first
        env = env_of(jobs["demo-capture-q38-t-s7-rep1"])
        self.assertEqual(env["NAMESPACE"]["value"], "s-dis-hic")
        self.assertEqual(env["TAKE_ID"]["value"], "q38-t-s7-rep1")

    def test_capture_job_deadline_never_passes_the_plan_hard_stop(self) -> None:
        hs = tfp.hard_stop_in(7200)
        with self.fresh("take S0 1", hs=hs) as (d, cluster, s3):
            cluster.s3 = s3
            self.assertEqual(d.execute(), 0)
            job = next(iter(cluster.jobs.values()))
        self.assertLessEqual(job["spec"]["activeDeadlineSeconds"], md.iso_epoch(hs) - int(time.time()) + 5)

    def test_a_plain_run_creates_no_capture_job(self) -> None:
        with self.fresh("run S0 1") as (d, cluster, _):
            self.assertEqual(d.execute(), 0)
        self.assertEqual(self.jobs_created(cluster), [])

    def test_plan_lock_lists_the_takes_among_the_runs(self) -> None:
        with self.fresh("take S0 1") as (d, cluster, s3):
            cluster.s3 = s3
            d.execute()
        lock = json.loads(s3.objects[next(k for k in s3.puts if "/_lock/plan-lock-" in k)])
        self.assertIn("S0/rep1", lock["runs"])
        self.assertEqual(lock["driver"], "q38-matrix-driver/4")

    def test_driver_summary_entry_reports_the_capture(self) -> None:
        with self.fresh("take S0 1") as (d, cluster, s3):
            cluster.s3 = s3
            d.execute()
        step = self.summary(s3)["steps"][0]
        self.assertEqual((step["kind"], step["status"], step["valid"], step["take_capture"]), ("take", "done", True, "ok"))
        # what select_takes / compose read back
        self.assertEqual(step["take_id"], "q38-t-s0-rep1")
        self.assertEqual(step["webm_uri"], "s3://test-bucket/takes/q38-t/q38-t-s0-rep1/q38-t-s0-rep1.webm")
        self.assertIsInstance(step["replay_start_ms"], int)

    def test_a_take_already_in_s3_is_skipped_and_records_no_new_capture(self) -> None:
        with self.fresh("take S0 1") as (d, cluster, s3):
            s3.objects["matrix/q38-t/S0/rep1/run.json"] = b"{}"
            self.assertEqual(d.execute(), 0)
            self.assertEqual(self.summary(s3)["steps"][0]["status"], "skipped-done")
        self.assertEqual(self.jobs_created(cluster), [])
        self.assertEqual(self.events(cluster), [])

    def test_existing_take_media_without_a_run_json_is_never_overwritten(self) -> None:
        with self.fresh("take S0 1") as (d, cluster, s3):
            s3.objects["takes/q38-t/q38-t-s0-rep1/q38-t-s0-rep1.webm"] = b"earlier launch"
            rc = d.execute()
            step = self.summary(s3)["steps"][0]
        self.assertEqual(rc, 1)
        self.assertEqual(step["status"], "failed")
        self.assertIn("append-only", step["error"])
        self.assertEqual(self.jobs_created(cluster), [])
        self.assertEqual(self.events(cluster), [])  # refused before any cluster change
        self.assertEqual(s3.objects["takes/q38-t/q38-t-s0-rep1/q38-t-s0-rep1.webm"], b"earlier launch")

    def test_hard_stop_guard_refuses_a_take_that_cannot_finish(self) -> None:
        text = plan("take S0 1", hs=tfp.hard_stop_in(60)).replace("boot_s=1 reset_s=1 drain_s=1", "boot_s=900 reset_s=1 drain_s=1")
        with fc.offline(text, self.tmp) as (d, cluster, s3):
            rc = d.execute()
        self.assertEqual(rc, 3)
        self.assertEqual(self.jobs_created(cluster), [])
        self.assertEqual(self.events(cluster), [])
        self.assertEqual(self.summary(s3)["steps"][0]["status"], "cut-by-hard-stop")

    def test_the_lead_is_part_of_the_guard(self) -> None:
        # time left is enough for boot/reset/span/drain (1+1+9.5+1 s) but not for the capture lead as well
        text = plan("take S0 1", hs=tfp.hard_stop_in(md.CAPTURE_LEAD_S - 10))
        with fc.offline(text, self.tmp) as (d, cluster, _):
            rc = d.execute()
        self.assertEqual(rc, 3)
        self.assertEqual(self.jobs_created(cluster), [])

    def test_a_hard_stop_cut_take_is_marked_and_tears_down(self) -> None:
        hs = tfp.hard_stop_in(md.CAPTURE_LEAD_S + 120)  # passes the guard (which includes the lead), then the cut
        with fc.offline(plan("take S0 1\ntake S7 1", hs=hs), self.tmp) as (d, cluster, s3):
            cluster.s3 = s3
            d.aiperf_duration_s = 10**6
            rc = d.execute()
        self.assertEqual(rc, 3)
        run = self.run_json(s3, "S0/rep1")
        self.assertIn("cut by the hard stop", run["validity"]["reasons"])
        self.assertNotEqual(run["take"]["capture"]["status"], "ok")
        self.assertEqual(self.events(cluster, ("apply", "delete-dgd"))[-1], ("delete-dgd", "s-agg"))


class CaptureFailureTests(Base):
    def assert_valid_run_with_failed_capture(self, cluster: fc.FakeCluster, s3: fc.FakeS3, rc: int) -> dict[str, Any]:
        self.assertEqual(rc, 0)  # the run is fine; only the take is not usable
        run = self.run_json(s3, "S0/rep1")
        self.assertTrue(run["validity"]["valid"], run["validity"])
        self.assertEqual(run["take"]["capture"]["status"], "failed")
        self.assertTrue(run["take"]["capture"]["reason"])
        step = self.summary(s3)["steps"][0]
        self.assertEqual((step["status"], step["valid"], step["take_capture"]), ("done", True, "failed"))
        self.assertEqual(self.events(cluster, ("delete-dgd",)), [])  # no teardown: the DGD is healthy
        return run

    def test_capture_job_creation_failure_does_not_invalidate_the_run(self) -> None:
        with fc.offline(plan("take S0 1"), self.tmp) as (d, cluster, s3):
            cluster.capture_outcome = "create-fails"
            rc = d.execute()
            run = self.assert_valid_run_with_failed_capture(cluster, s3, rc)
        self.assertIn("Forbidden", run["take"]["capture"]["reason"])

    def test_capture_job_that_fails_after_the_replay_does_not_invalidate_the_run(self) -> None:
        with fc.offline(plan("take S0 1"), self.tmp) as (d, cluster, s3):
            cluster.s3 = s3
            cluster.capture_outcome = "failed"
            rc = d.execute()
            self.assert_valid_run_with_failed_capture(cluster, s3, rc)
        self.assertFalse(any(k.endswith(".webm") for k in s3.objects))

    def test_a_job_that_completes_without_uploading_the_webm_is_a_failed_capture(self) -> None:
        with fc.offline(plan("take S0 1"), self.tmp) as (d, cluster, s3):
            cluster.s3 = None  # the fake pod uploads nothing
            rc = d.execute()
            self.assert_valid_run_with_failed_capture(cluster, s3, rc)

    def test_the_measured_numbers_of_the_run_are_unaffected_by_the_capture_outcome(self) -> None:
        results = []
        for outcome in ("complete", "failed"):
            with fc.offline(plan("take S0 1"), self.scratch()) as (d, cluster, s3):
                cluster.s3 = s3
                cluster.capture_outcome = outcome
                d.execute()
                run = self.run_json(s3, "S0/rep1")
            results.append({"valid": run["validity"]["valid"], "reasons": run["validity"]["reasons"], "reset_ok": run["reset"]["ok"]})
        self.assertEqual(results[0], results[1])


class RetakeTests(Base):
    PLAN = "take S0 1\ntake S0 2 retake_of=S0/rep1 min_left_s=60"

    def test_retake_runs_when_the_capture_failed_and_time_remains(self) -> None:
        with fc.offline(plan(self.PLAN), self.tmp) as (d, cluster, s3):
            cluster.s3 = s3
            cluster.capture_outcome_seq = ["failed", "complete"]
            rc = d.execute()
            steps = self.summary(s3)["steps"]
        self.assertEqual(rc, 0)
        self.assertEqual([s["status"] for s in steps], ["done", "done"])
        self.assertEqual([s["take_capture"] for s in steps], ["failed", "ok"])
        self.assertEqual(self.jobs_created(cluster), ["demo-capture-q38-t-s0-rep1", "demo-capture-q38-t-s0-rep2"])
        self.assertIn("takes/q38-t/q38-t-s0-rep2/q38-t-s0-rep2.webm", s3.objects)
        # the first take's media prefix was not touched by the retake (append-only)
        self.assertFalse(any(k.startswith("takes/q38-t/q38-t-s0-rep1/") for k in s3.objects))
        self.assertEqual(len(self.events(cluster, ("apply",))), 1)  # same healthy DGD, no reboot

    def test_retake_is_skipped_when_the_first_take_is_good(self) -> None:
        with fc.offline(plan(self.PLAN), self.tmp) as (d, cluster, s3):
            cluster.s3 = s3
            rc = d.execute()
            steps = self.summary(s3)["steps"]
        self.assertEqual(rc, 0)
        self.assertEqual([s["status"] for s in steps], ["done", "skipped-not-needed"])
        self.assertEqual(self.jobs_created(cluster), ["demo-capture-q38-t-s0-rep1"])
        self.assertNotIn("matrix/q38-t/S0/rep2/run.json", s3.objects)

    def test_retake_is_skipped_when_there_is_not_enough_time(self) -> None:
        text = plan("take S0 1\ntake S0 2 retake_of=S0/rep1 min_left_s=200000")  # plan() gives 86400 s
        with fc.offline(text, self.tmp) as (d, cluster, s3):
            cluster.s3 = s3
            cluster.capture_outcome_seq = ["failed", "complete"]
            rc = d.execute()
            steps = self.summary(s3)["steps"]
        self.assertEqual(rc, 0)
        self.assertEqual([s["status"] for s in steps], ["done", "skipped-no-time"])
        self.assertEqual(self.jobs_created(cluster), ["demo-capture-q38-t-s0-rep1"])
        self.assertNotIn("matrix/q38-t/S0/rep2/run.json", s3.objects)

    def test_retake_after_an_invalid_take_reboots_and_records_again(self) -> None:
        with fc.offline(plan(self.PLAN), self.tmp) as (d, cluster, s3):
            cluster.s3 = s3
            cluster.broken_deploys["s-agg"] = 1  # take 1 fails T-RTR-2 before the replay
            rc = d.execute()
            steps = self.summary(s3)["steps"]
            run2 = self.run_json(s3, "S0/rep2")
        self.assertEqual(rc, 1)  # the failed first take is still reported
        self.assertEqual([s["status"] for s in steps], ["failed", "done"])
        self.assertEqual(self.jobs_created(cluster), ["demo-capture-q38-t-s0-rep2"])  # no capture for a run that never replayed
        self.assertTrue(run2["validity"]["valid"], run2["validity"])
        self.assertEqual(run2["take"]["capture"]["status"], "ok")
        self.assertEqual([e[0] for e in self.events(cluster, ("apply", "delete-dgd"))], ["apply", "delete-dgd", "apply"])

    def test_retake_after_a_completed_but_invalid_run(self) -> None:
        with fc.offline(plan(self.PLAN), self.tmp) as (d, cluster, s3):
            cluster.s3 = s3
            d.aiperf_rc = 1  # aiperf exits non-zero -> run invalid (completed)
            d.execute()
            steps = self.summary(s3)["steps"]
            run1 = self.run_json(s3, "S0/rep1")
        self.assertFalse(run1["validity"]["valid"])
        self.assertEqual(steps[1]["kind"], "take")
        self.assertNotEqual(steps[1]["status"], "skipped-not-needed")  # an invalid take is retaken

    def test_a_retake_whose_predecessor_was_skipped_done_is_judged_from_its_run_json(self) -> None:
        # relaunch: take 1 is already in S3 with a failed capture -> the retake still runs
        with fc.offline(plan(self.PLAN), self.tmp) as (d, cluster, s3):
            cluster.s3 = s3
            first = {"validity": {"valid": True}, "take": {"take_id": "q38-t-s0-rep1", "result_of_record": False,
                                                          "capture": {"status": "failed", "reason": "earlier launch"}}}
            s3.objects["matrix/q38-t/S0/rep1/run.json"] = json.dumps(first).encode()
            rc = d.execute()
            steps = self.summary(s3)["steps"]
        self.assertEqual(rc, 0)
        self.assertEqual([s["status"] for s in steps], ["skipped-done", "done"])
        self.assertEqual(self.jobs_created(cluster), ["demo-capture-q38-t-s0-rep2"])


class ComposeStepTests(Base):
    FULL = "take S0 1\ntake S7 1\ncompose\nteardown"

    def test_compose_creates_one_job_with_both_take_webms_and_writes_the_outputs(self) -> None:
        with fc.offline(plan(self.FULL), self.tmp) as (d, cluster, s3):
            cluster.s3 = s3
            rc = d.execute()
            steps = self.summary(s3)["steps"]
            jobs = dict(cluster.jobs)
            r0, r7 = self.run_json(s3, "S0/rep1"), self.run_json(s3, "S7/rep1")
        self.assertEqual(rc, 0)
        self.assertEqual([s["status"] for s in steps], ["done", "done", "done", "done"])
        compose = [n for n in jobs if "compose" in n]
        self.assertEqual(len(compose), 1)
        spec = json.loads(env_of(jobs[compose[0]])["COMPOSE_SPEC"]["value"])
        self.assertEqual([i["webm_uri"] for i in spec["inputs"]],
                         [r0["take"]["capture"]["webm_uri"], r7["take"]["capture"]["webm_uri"]])
        self.assertEqual([i["replay_start_ms"] for i in spec["inputs"]],
                         [r0["take"]["capture"]["replay_start_ms"], r7["take"]["capture"]["replay_start_ms"]])
        self.assertEqual([i["label"] for i in spec["inputs"]],
                         ["Baseline: aggregated + round-robin", "Router + HiCache + PD disaggregation"])
        for uri in spec["outputs"].values():
            self.assertIn(uri.removeprefix("s3://test-bucket/"), s3.objects)
        marker = json.loads(s3.objects["takes/q38-t/compose/compose.json"])
        self.assertEqual(marker["status"], "ok")
        self.assertEqual(marker["inputs"], spec["inputs"])
        self.assertEqual(marker["outputs"], spec["outputs"])
        self.assertEqual(marker["run_id"], "q38-t")
        # port: the render image is the Job environment value, not a constant of the driver
        self.assertEqual(marker["images"]["render"], fc.COMPOSE_FFMPEG_IMAGE)
        # compose.json is the last object of the compose prefix (as run.json is for a run)
        self.assertIn("takes/q38-t/compose/compose.json", s3.puts)

    def test_compose_uses_the_retake_when_the_first_capture_failed(self) -> None:
        text = plan("take S0 1\ntake S0 2 retake_of=S0/rep1 min_left_s=60\ntake S7 1\ncompose")
        with fc.offline(text, self.tmp) as (d, cluster, s3):
            cluster.s3 = s3
            cluster.capture_outcome_seq = ["failed", "complete", "complete"]
            rc = d.execute()
            job = next(j for n, j in cluster.jobs.items() if "compose" in n)
        self.assertEqual(rc, 0)
        spec = json.loads(env_of(job)["COMPOSE_SPEC"]["value"])
        self.assertEqual(spec["inputs"][0]["take_id"], "q38-t-s0-rep2")
        self.assertEqual(spec["inputs"][1]["take_id"], "q38-t-s7-rep1")

    def test_compose_without_a_usable_s7_take_fails_cleanly_without_a_job(self) -> None:
        with fc.offline(plan("take S0 1\ntake S7 1\ncompose"), self.tmp) as (d, cluster, s3):
            cluster.s3 = s3
            cluster.capture_outcome_seq = ["complete", "failed"]
            rc = d.execute()
            steps = self.summary(s3)["steps"]
        self.assertEqual(rc, 1)
        self.assertEqual([s["status"] for s in steps], ["done", "done", "failed"])
        self.assertIn("S7", steps[2]["error"])
        self.assertFalse([n for n in cluster.jobs if "compose" in n])
        self.assertNotIn("takes/q38-t/compose/compose.json", s3.objects)

    def test_a_failed_compose_job_leaves_no_marker_and_reports_failure(self) -> None:
        with fc.offline(plan(self.FULL), self.tmp) as (d, cluster, s3):
            cluster.s3 = s3
            cluster.compose_outcome = "failed"
            rc = d.execute()
            steps = self.summary(s3)["steps"]
        self.assertEqual(rc, 1)
        self.assertEqual(steps[2]["status"], "failed")
        self.assertNotIn("takes/q38-t/compose/compose.json", s3.objects)
        self.assertEqual(steps[0]["valid"], True)  # the takes of record are untouched
        self.assertIn("matrix/q38-t/S0/rep1/run.json", s3.objects)
        self.assertIn("matrix/q38-t/S7/rep1/run.json", s3.objects)

    def test_compose_job_creation_failure_is_a_failed_step(self) -> None:
        with fc.offline(plan(self.FULL), self.tmp) as (d, cluster, s3):
            cluster.s3 = s3
            cluster.compose_outcome = "create-fails"
            rc = d.execute()
            steps = self.summary(s3)["steps"]
        self.assertEqual(rc, 1)
        self.assertEqual(steps[2]["status"], "failed")

    def test_a_compose_job_that_completes_without_the_outputs_is_a_failure(self) -> None:
        with fc.offline(plan(self.FULL), self.tmp) as (d, cluster, s3):
            cluster.s3 = s3
            cluster.skip_compose_outputs = True
            rc = d.execute()
            steps = self.summary(s3)["steps"]
        self.assertEqual(rc, 1)
        self.assertEqual(steps[2]["status"], "failed")
        self.assertNotIn("takes/q38-t/compose/compose.json", s3.objects)

    def test_compose_after_a_relaunch_reads_the_takes_from_their_run_json(self) -> None:
        with fc.offline(plan(self.FULL), self.tmp) as (d, cluster, s3):
            cluster.s3 = s3
            for cell, ms in (("S0", 111), ("S7", 222)):
                tid = md.take_id("q38-t", cell, 1)
                uri = f"s3://test-bucket/takes/q38-t/{tid}/{tid}.webm"
                s3.objects[f"takes/q38-t/{tid}/{tid}.webm"] = b"media"
                s3.objects[f"matrix/q38-t/{cell}/rep1/run.json"] = json.dumps({
                    "validity": {"valid": True}, "take": {"take_id": tid, "result_of_record": False, "capture": {
                        "status": "ok", "webm_uri": uri, "replay_start_ms": ms, "from_ms": ms}}}).encode()
            rc = d.execute()
            steps = self.summary(s3)["steps"]
            job = next(j for n, j in cluster.jobs.items() if "compose" in n)
        self.assertEqual(rc, 0)
        self.assertEqual([s["status"] for s in steps[:2]], ["skipped-done", "skipped-done"])
        spec = json.loads(env_of(job)["COMPOSE_SPEC"]["value"])
        self.assertEqual([(i["take_id"], i["replay_start_ms"]) for i in spec["inputs"]],
                         [("q38-t-s0-rep1", 111), ("q38-t-s7-rep1", 222)])

    def test_compose_already_done_is_skipped(self) -> None:
        with fc.offline(plan(self.FULL), self.tmp) as (d, cluster, s3):
            cluster.s3 = s3
            s3.objects["takes/q38-t/compose/compose.json"] = b'{"status": "ok"}'
            rc = d.execute()
            steps = self.summary(s3)["steps"]
        self.assertEqual(rc, 0)
        self.assertEqual(steps[2]["status"], "skipped-done")
        self.assertFalse([n for n in cluster.jobs if "compose" in n])

    def test_a_partial_compose_prefix_is_never_reused(self) -> None:
        with fc.offline(plan(self.FULL), self.tmp) as (d, cluster, s3):
            cluster.s3 = s3
            s3.objects["takes/q38-t/compose/side-by-side-full.mp4"] = b"half"
            rc = d.execute()
            steps = self.summary(s3)["steps"]
        self.assertEqual(rc, 1)
        self.assertEqual(steps[2]["status"], "failed")
        self.assertIn("append-only", steps[2]["error"])
        self.assertFalse([n for n in cluster.jobs if "compose" in n])
        self.assertEqual(s3.objects["takes/q38-t/compose/side-by-side-full.mp4"], b"half")

    def test_compose_needs_no_gpu_and_leaves_the_topology_to_the_teardown_step(self) -> None:
        with fc.offline(plan(self.FULL), self.tmp) as (d, cluster, s3):
            cluster.s3 = s3
            d.execute()
        order = [e for e in cluster.events if e[0] in ("create-job", "delete-dgd")]
        self.assertTrue("compose" in order[-2][1] and order[-1] == ("delete-dgd", "s-dis-hic"), order)  # teardown step last


class SloLockForTakesTests(Base):
    def lock_plan(self) -> str:
        return plan("take S0 1", run_id="q38-t7", slo="slo from=s3://test-bucket/matrix/q38-e3/_slo/slo-calibration.json "
                    f"lock=s3://test-bucket/matrix/q38-e3/_slo/slo-lock.json e3_plan_sha256={tfp.SHA}")

    def test_take_refuses_without_the_e3_lock_before_any_cluster_change(self) -> None:
        with fc.offline(self.lock_plan(), self.tmp) as (d, cluster, _):
            rc = d.execute()
        self.assertEqual(rc, 2)
        self.assertEqual(self.events(cluster), [])
        self.assertEqual(self.jobs_created(cluster), [])

    def test_take_with_a_matching_lock_runs_under_the_locked_slo(self) -> None:
        cal = json.dumps({"slo": {"ttft_s": 4.2, "itl_ms": 25.0, "cold_factor": 1.4, "cold_prefill_tok_s": 3e4}}).encode()
        with fc.offline(self.lock_plan(), self.tmp) as (d, cluster, s3):
            s3.objects["matrix/q38-e3/_slo/slo-calibration.json"] = cal
            s3.objects["matrix/q38-e3/_slo/slo-lock.json"] = json.dumps({
                "plan_sha256": tfp.SHA, "slo_calibration_key": "matrix/q38-e3/_slo/slo-calibration.json",
                "slo_calibration_sha256": hashlib.sha256(cal).hexdigest(), "slo": json.loads(cal)["slo"]}).encode()
            cluster.s3 = s3
            rc = d.execute()
            self.assertEqual(d.slo["ttft_s"], 4.2)
            run = self.run_json(s3, "S0/rep1", "q38-t7")
        self.assertEqual(rc, 0)
        self.assertTrue(run["slo_lock"]["slo_calibration_sha256"])


class WatchdogPreflightTests(Base):
    E7_HS = "2026-10-24T09:00:00Z"
    MATRIX_HS = "2026-10-24T04:30:00Z"

    def run_with(self, cron_env: list[dict[str, str]] | None) -> tuple[int, dict[str, Any]]:
        with fc.offline(plan("take S0 1", hs=self.E7_HS), self.scratch()) as (d, cluster, s3):
            cluster.cron_env = cron_env
            cluster.s3 = s3
            rc = d.execute()
            return rc, self.summary(s3)

    def test_a_watchdog_without_the_e7_window_is_refused(self) -> None:
        for env in ([{"name": "HARD_STOP", "value": self.MATRIX_HS}],
                    [{"name": "HARD_STOP", "value": self.MATRIX_HS},
                     {"name": "WINDOWS", "value": f"{self.MATRIX_HS}/2026-10-24T05:00:00Z"}]):
            with self.subTest(env=env):
                rc, summary = self.run_with(env)
                self.assertEqual(rc, 2)
                self.assertIn("refused", summary)

    def test_a_watchdog_with_the_e7_window_is_accepted(self) -> None:
        env = [{"name": "HARD_STOP", "value": self.MATRIX_HS},
               {"name": "WINDOWS", "value": f"{self.MATRIX_HS}/2026-10-24T05:00:00Z,{self.E7_HS}/2026-10-24T10:00:00Z"}]
        _, summary = self.run_with(env)
        self.assertNotIn("refused", summary)
        self.assertEqual(summary["preflight"]["watchdog"]["hard_stop"], self.E7_HS)

    def test_a_single_window_watchdog_on_the_e7_stop_is_accepted(self) -> None:
        _, summary = self.run_with([{"name": "HARD_STOP", "value": self.E7_HS}])
        self.assertNotIn("refused", summary)

    def test_the_matrix_plans_still_see_their_own_04_30_window(self) -> None:
        with fc.offline(plan("run S0 1", hs=self.MATRIX_HS), self.tmp) as (d, cluster, s3):
            cluster.cron_env = [{"name": "HARD_STOP", "value": self.MATRIX_HS},
                                {"name": "WINDOWS", "value": f"{self.MATRIX_HS}/2026-10-24T05:00:00Z,{self.E7_HS}/2026-10-24T10:00:00Z"}]
            d.execute()
            summary = self.summary(s3)
        self.assertNotIn("refused", summary)


if __name__ == "__main__":
    unittest.main()
