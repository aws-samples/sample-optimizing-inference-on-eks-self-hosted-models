# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Offline tests of the matrix driver's pure logic (stdlib unittest; demo_bench on sys.path for the 0.5 % rule).

Run: PYTHONPATH=<demo_bench> python3 -m unittest -v test_matrix_driver.py   (from this directory)
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import unittest
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar

import matrix_driver as md
from support import shipped_plan_text
import watchdog as wd

HERE = Path(__file__).resolve().parent
SHA = "a" * 64
W2_TRACE_SHA = "7b061c7b3128e4cbdffe3e9134b5001108ad6558d06e4ba63a09670f2eea9f09"
W2_SIDECAR_SHA = "f70c8b6248d1a44f79b7de1e37115778e31b4686307c38f50fdce294c99b5bf1"
HARD_STOP = "2026-10-24T04:30:00Z"


def plan_text(steps: str, slo: str = "slo ttft_s=2 itl_ms=25 cold_factor=1.4 cold_prefill_tok_s=30000",
              extra: str = "") -> str:
    return (
        f"run_id q38-test\nhard_stop {HARD_STOP}\n{extra}"
        f"w2 trace=s3://b/t.jsonl trace_sha256={SHA} sidecar=s3://b/s.jsonl sidecar_sha256={SHA} frozen=yes\n"
        f"tokenizer prefix=s3://b/tok/ sums_sha256={SHA}\n{slo}\n{steps}\n"
    )


def keys(plan: md.Plan) -> list[str]:
    return [s.key for s in plan.steps if s.kind == "run"]


class PlanTests(unittest.TestCase):
    def test_shipped_plans_are_frozen_with_the_w2_of_record_and_the_0430_hard_stop(self) -> None:
        for name in ("plan-e3", "plan-e4", "plan-e5", "plan-e6"):
            plan = md.parse_plan(shipped_plan_text(name))
            with self.subTest(name):
                self.assertEqual(plan.hard_stop, HARD_STOP)
                self.assertEqual(plan.w2["frozen"], "yes")
                self.assertEqual(plan.w2["trace_sha256"], W2_TRACE_SHA)
                self.assertEqual(plan.w2["sidecar_sha256"], W2_SIDECAR_SHA)
                self.assertTrue(plan.slo_frozen)

    def test_e3_probe_pilot_calibrate_smoke(self) -> None:
        plan = md.parse_plan(shipped_plan_text("plan-e3"))
        self.assertEqual([s.kind for s in plan.steps], ["probe-cold-prefill", "run", "calibrate-slo", "run", "teardown"])
        self.assertEqual([(s.key, s.label) for s in plan.steps if s.kind == "run"], [("S0/rep1", "pilot"),
                                                                                     ("S7/rep1", "smoke")])
        self.assertEqual(plan.on_failure, "abort")
        self.assertIsNone(plan.slo_ref)

    def test_every_plan_ends_with_a_teardown(self) -> None:
        for name in ("plan-e3", "plan-e4", "plan-e5", "plan-e6"):
            self.assertEqual(md.parse_plan(shipped_plan_text(name)).steps[-1].kind, "teardown", name)

    def test_e4_e5_counterbalanced_and_e4_aborts_on_failure(self) -> None:
        e4 = md.parse_plan(shipped_plan_text("plan-e4"))
        self.assertEqual(keys(e4), ["S0/rep1", "S7/rep1", "S7/rep2", "S0/rep2", "S0/rep3", "S7/rep3"])
        self.assertEqual(e4.on_failure, "abort")
        e5 = md.parse_plan(shipped_plan_text("plan-e5"))
        self.assertEqual(keys(e5), ["S1/rep1", "S4/rep1", "S4/rep2", "S1/rep2", "S1/rep3", "S4/rep3"])

    def test_e6_counterbalanced_rounds_and_cut_order(self) -> None:
        plan = md.parse_plan(shipped_plan_text("plan-e6"))
        self.assertEqual(keys(plan), ["S2/rep1", "S3/rep1", "S5/rep1", "S6/rep1",
                                      "S6/rep2", "S5/rep2", "S3/rep2", "S2/rep2",
                                      "S2/rep3", "S3/rep3", "S5/rep3", "S6/rep3"])
        self.assertEqual(plan.cut_order, ["S6", "S3", "S5"])
        self.assertEqual(plan.steps[-1].kind, "teardown")

    def test_e4_e5_e6_pin_the_e3_plan_and_its_slo_lock(self) -> None:
        e3_sha = hashlib.sha256(shipped_plan_text("plan-e3").encode()).hexdigest()
        for name in ("plan-e4", "plan-e5", "plan-e6"):
            plan = md.parse_plan(shipped_plan_text(name))
            assert plan.slo_ref is not None
            with self.subTest(name):
                self.assertEqual(plan.slo_ref["e3_plan_sha256"], e3_sha)
                self.assertTrue(plan.slo_ref["from"].endswith("/matrix/q38-e3/_slo/slo-calibration.json"))
                self.assertTrue(plan.slo_ref["lock"].endswith("/matrix/q38-e3/_slo/slo-lock.json"))
                self.assertEqual(plan.slo, {})

    def test_refusals(self) -> None:
        bad = {
            "missing header": "run_id q38-x\nrun S0 1\n",
            "unknown cell": plan_text("run S9 1"),
            "duplicate run": plan_text("run S0 1\nrun S0 1"),
            "calibrate before run": plan_text("calibrate-slo after=S0/rep1\nrun S0 1"),
            "probe needed": plan_text("run S0 1", "slo ttft_s=2 itl_ms=25 cold_factor=1.4 cold_prefill_tok_s=probe"),
            "bad rep": plan_text("run S0 0"),
            "unknown directive": plan_text("run S0 1\nreboot"),
            "no runs": plan_text("teardown"),
            "bad on_failure": plan_text("run S0 1", extra="on_failure maybe\n"),
            "cut_order unknown cell": plan_text("run S0 1", extra="cut_order S9\n"),
            "cut_order cell not in plan": plan_text("run S0 1", extra="cut_order S6\n"),
            "slo lock with a calibration step": plan_text(
                "run S0 1\ncalibrate-slo after=S0/rep1",
                f"slo from=s3://b/_slo/slo-calibration.json lock=s3://b/_slo/slo-lock.json e3_plan_sha256={SHA}"),
            "slo lock bad sha": plan_text(
                "run S0 1", "slo from=s3://b/c.json lock=s3://b/l.json e3_plan_sha256=FREEZE_AT_E3"),
            "placeholder slo": plan_text("run S0 1", "slo ttft_s=FREEZE_AT_E3 itl_ms=25 cold_factor=1.4 "
                                                      "cold_prefill_tok_s=1"),
        }
        for what, text in bad.items():
            with self.subTest(what), self.assertRaises(md.ConfigError):
                md.parse_plan(text)
        with self.assertRaises(md.ConfigError):
            md.parse_plan(plan_text("run S0 1").replace(SHA, "deadbeef", 1))

    def test_probe_before_first_run_is_accepted(self) -> None:
        plan = md.parse_plan(plan_text("probe-cold-prefill S0\nrun S0 1",
                                       "slo ttft_s=2 itl_ms=25 cold_factor=1.4 cold_prefill_tok_s=probe"))
        self.assertEqual(plan.slo["cold_prefill_tok_s"], "probe")
        self.assertEqual(plan.on_failure, "continue")

    def test_iso_epoch(self) -> None:
        want = int(dt.datetime(2026, 10, 24, 4, 30, tzinfo=dt.UTC).timestamp())
        self.assertEqual(md.iso_epoch(HARD_STOP), want)
        for bad in ("2026-10-24 09:30:00Z", "2026-02-30T00:00:00Z"):
            with self.subTest(bad), self.assertRaises(md.ConfigError):
                md.iso_epoch(bad)


class CutOrderTests(unittest.TestCase):
    EST: ClassVar[dict[str, int]] = {"boot_s": 900, "reset_s": 240, "drain_s": 600, "probe_s": 300}
    SPAN = 1740.0

    def runs(self) -> list[md.Step]:
        return [s for s in md.parse_plan(shipped_plan_text("plan-e6")).steps if s.kind == "run"]

    def need(self, runs: list[md.Step], topo: str | None) -> int:
        total, cur = 0, topo
        for s in runs:
            total += md.run_estimate_s(self.EST, self.SPAN, md.CELLS[s.cell][0] != cur)
            cur = md.CELLS[s.cell][0]
        return total

    def test_nothing_cut_when_everything_fits(self) -> None:
        runs = self.runs()
        self.assertEqual(md.plan_cuts(runs, None, self.EST, self.SPAN, self.need(runs, None), cut_order=["S6", "S3", "S5"]), [])

    def test_cuts_follow_s6_s3_s5_reps_2_3_and_never_rep1_or_s2(self) -> None:
        runs = self.runs()
        full = self.need(runs, None)
        cut = md.plan_cuts(runs, None, self.EST, self.SPAN, full - 1, cut_order=["S6", "S3", "S5"])
        self.assertEqual(cut, ["S6/rep3"])
        deep = md.plan_cuts(runs, None, self.EST, self.SPAN, 0, cut_order=["S6", "S3", "S5"])
        self.assertEqual(deep, ["S6/rep3", "S6/rep2", "S3/rep3", "S3/rep2", "S5/rep3", "S5/rep2"])
        self.assertFalse(any(k.endswith("rep1") or k.startswith("S2") for k in deep))

    def test_no_cut_order_means_no_priority_cut(self) -> None:
        runs = self.runs()
        self.assertEqual(md.plan_cuts(runs, None, self.EST, self.SPAN, 0, cut_order=[]), [])


class SloLockTests(unittest.TestCase):
    CAL: ClassVar[dict[str, Any]] = {"rule": "slo-calibration", "slo": {"ttft_s": 4.2, "itl_ms": 25.0, "cold_factor": 1.4, "cold_prefill_tok_s": 30000.0}}

    def ref(self) -> dict[str, str]:
        return {"from": "s3://b/matrix/q38-e3/_slo/slo-calibration.json", "lock": "s3://b/matrix/q38-e3/_slo/slo-lock.json",
                "e3_plan_sha256": SHA}

    def lock(self, cal_bytes: bytes) -> dict[str, object]:
        return {"plan_sha256": SHA, "slo_calibration_key": "matrix/q38-e3/_slo/slo-calibration.json",
                "slo_calibration_sha256": hashlib.sha256(cal_bytes).hexdigest(), "slo": self.CAL["slo"]}

    def test_matching_lock_gives_the_calibrated_slo(self) -> None:
        cal = json.dumps(self.CAL).encode()
        self.assertEqual(md.verify_slo_lock(self.ref(), self.lock(cal), cal), self.CAL["slo"])

    def test_any_mismatch_refuses(self) -> None:
        cal = json.dumps(self.CAL).encode()
        tampered = json.dumps({**self.CAL, "slo": {**self.CAL["slo"], "ttft_s": 9.9}}).encode()
        cases = {
            "calibration file changed after the lock": (self.ref(), self.lock(cal), tampered),
            "lock from another e3 plan": (self.ref(), {**self.lock(cal), "plan_sha256": "b" * 64}, cal),
            "lock names another file": (self.ref(), {**self.lock(cal), "slo_calibration_key": "matrix/x.json"}, cal),
            "lock slo differs from the file": (self.ref(), {**self.lock(cal), "slo": {"ttft_s": 1.0}}, cal),
        }
        for what, (ref, lock, data) in cases.items():
            with self.subTest(what), self.assertRaises(md.ConfigError):
                md.verify_slo_lock(ref, lock, data)


class JobDeadlineTests(unittest.TestCase):
    def test_aligned_deadline_is_accepted_and_a_late_one_refused(self) -> None:
        hs = md.iso_epoch(HARD_STOP)
        job: dict[str, Any] = {"status": {"startTime": "2026-10-23T00:00:30Z"},
               "spec": {"activeDeadlineSeconds": hs - md.iso_epoch("2026-10-23T00:00:00Z")}}
        self.assertEqual(md.job_deadline_check(job, hs)["deadline"], "2026-10-24T04:30:30Z")
        late = {**job, "spec": {"activeDeadlineSeconds": job["spec"]["activeDeadlineSeconds"] + 3600}}
        with self.assertRaises(md.ConfigError):
            md.job_deadline_check(late, hs)
        with self.assertRaises(md.ConfigError):
            md.job_deadline_check({"status": {"startTime": "2026-10-23T00:00:30Z"}, "spec": {}}, hs)


PROM_A = """# HELP x
sglang:cached_tokens_total{cache_source="device",worker_id="a"} 100
sglang:cached_tokens_total{cache_source="host",worker_id="a"} 10
sglang:prompt_tokens_total{worker_id="a"} 1000
sglang:time_to_first_token_seconds_bucket{le="1"} 3
sglang:load_back_tokens_total{pool="kv"} 5
dynamo_component_router_kv_hit_rate_sum 1.0
dynamo_component_router_kv_hit_rate_count 4
dynamo_component_router_kv_hit_rate_bucket{le="0.5"} 2
unrelated_metric 7
"""
PROM_B = PROM_A.replace(' 100\n', ' 400\n').replace('"} 10\n', '"} 60\n').replace("} 1000", "} 2000") \
    .replace("_sum 1.0", "_sum 3.0").replace("_count 4", "_count 8").replace('le="0.5"} 2', 'le="0.5"} 5')


class MetricTests(unittest.TestCase):
    def test_parse_keeps_relevant_series_and_only_the_router_hit_buckets(self) -> None:
        m = md.parse_metrics(PROM_A)
        self.assertNotIn("unrelated_metric", m)
        self.assertFalse(any(k.startswith("sglang:time_to_first_token_seconds_bucket") for k in m))
        self.assertIn('dynamo_component_router_kv_hit_rate_bucket{le="0.5"}', m)
        self.assertEqual(md.series_sum(m, "sglang:cached_tokens_total", cache_source="host"), 10)

    def test_engine_kpis_hit_rate_excludes_decode(self) -> None:
        a, b = md.parse_metrics(PROM_A), md.parse_metrics(PROM_B)
        k = md.engine_kpis({"w:p": a, "w:d": a}, {"w:p": b, "w:d": b}, {"w:p": "prefill", "w:d": "decode"})
        self.assertAlmostEqual(k["total"]["engine_hit_rate"], 0.35)
        self.assertEqual(k["total"]["by_label"]["sglang:cached_tokens_total"], {"device": 600.0, "host": 100.0})
        self.assertAlmostEqual(k["total"]["host_plus_storage_share_of_cached"], 50 / 350)

    def test_frontend_predicted_mean_and_histogram(self) -> None:
        f = md.frontend_kpis(md.parse_metrics(PROM_A), md.parse_metrics(PROM_B))
        self.assertAlmostEqual(f["router_predicted_hit_mean"], 0.5)
        self.assertEqual(f["router_predicted_hit_hist_delta"], {"0.5": 3.0})
        self.assertIsNone(md.frontend_kpis({}, {})["router_predicted_hit_mean"])


class NodeCounterTests(unittest.TestCase):
    def test_net_dev(self) -> None:
        text = ("Inter-|   Receive\n face |bytes packets errs drop fifo frame compressed multicast|bytes packets\n"
                "    lo: 1 2 0 0 0 0 0 0 3 4 0 0 0 0 0 0\n  eth0: 100 5 0 0 0 0 0 0 200 6 0 0 0 0 0 0\n")
        self.assertEqual(md.parse_net_dev(text), {"rx_bytes": 100, "rx_packets": 5, "tx_bytes": 200, "tx_packets": 6})
        self.assertIsNone(md.parse_net_dev("nothing"))

    def test_diskstats_md127_and_nvme_members(self) -> None:
        text = (" 259 0 nvme0n1 1 0 8 0 0 0 0 0 0 0 0\n 259 1 nvme0n1p1 1 0 8 0 0 0 0 0 0 0 0\n"
                "   9 127 md127 10 0 2048 0 3 0 4096 0 0 0 0\n   7 0 loop0 1 0 2 0 0 0 0 0 0 0 0\n")
        self.assertEqual(md.parse_diskstats(text), {"reads": 10, "read_bytes": 2048 * 512, "writes": 3,
                                                    "write_bytes": 4096 * 512})
        self.assertEqual(sorted(md.parse_diskstats_all(text)), ["md127", "nvme0n1"])

    def test_meminfo(self) -> None:
        text = "MemTotal:       2097152000 kB\nMemFree: 1000 kB\nCached: 20 kB\nHugePages_Total: 0\n"
        self.assertEqual(md.parse_meminfo(text), {"MemTotal": 2097152000, "MemFree": 1000, "Cached": 20})

    def test_l3_proxy_threshold(self) -> None:
        nvme = md.l3_page_cache_proxy(disk_read_bytes=900, storage_tokens=10, backup_bytes=1000, backup_tokens_kv=10)
        self.assertEqual(nvme["label"], "nvme")
        pc = md.l3_page_cache_proxy(disk_read_bytes=100, storage_tokens=10, backup_bytes=1000, backup_tokens_kv=10)
        self.assertEqual(pc["label"], "page-cache assisted")
        self.assertAlmostEqual(pc["page_cache_share_proxy_I"], 0.9)
        self.assertEqual(md.l3_page_cache_proxy(None, 10, 1, 1)["status"], "n/a")


class GpuIdentityTests(unittest.TestCase):
    SMI8 = "\n".join(f"{i}, GPU-{i:08d}-0000, 00000000:{0x50 + i:02X}:00.0" for i in range(8))

    def test_privileged_pod_uses_the_pinned_uuid(self) -> None:
        g = md.effective_gpu("CUDA_VISIBLE_DEVICES=GPU-00000003-0000\nNVIDIA_VISIBLE_DEVICES=/var/run/x\n",
                             self.SMI8, privileged=True)
        self.assertEqual(g["uuids"], ["GPU-00000003-0000"])
        self.assertEqual(g["host_index"], [3])

    def test_unprivileged_pod_sees_only_its_gpu(self) -> None:
        g = md.effective_gpu("NVIDIA_VISIBLE_DEVICES=/var/run/x\n", "0, GPU-00000005-0000, 00000000:55:00.0",
                             privileged=False)
        self.assertEqual(g["uuids"], ["GPU-00000005-0000"])

    def test_privileged_without_pin_is_unknown(self) -> None:
        self.assertEqual(md.effective_gpu("", self.SMI8, privileged=True)["uuids"], [])

    def test_uniqueness(self) -> None:
        ok = {f"p{i}": {"uuids": [f"GPU-{i}"]} for i in range(8)}
        self.assertTrue(md.gpu_uniqueness(ok, 8)["ok"])
        dup = {**ok, "p7": {"uuids": ["GPU-0"]}}
        r = md.gpu_uniqueness(dup, 8)
        self.assertFalse(r["ok"])
        self.assertEqual(r["duplicates"], {"GPU-0": ["p0", "p7"]})
        self.assertFalse(md.gpu_uniqueness({**ok, "p7": {"uuids": []}}, 8)["ok"])
        self.assertFalse(md.gpu_uniqueness(dict(list(ok.items())[:7]), 8)["ok"])


BOOT_LOG = """2026 INFO dynamo_llm::kv_router::publisher: LocalKvIndexer enabled for worker 194172077015596 on endpoint x
GPU pin: indices 2 -> CUDA_VISIBLE_DEVICES=GPU-4a386bb8-419b-d695-7637-a379b79aa166
NIXL INFO    _api.py:405 Backend LIBFABRIC was instantiated
INFO base._allocate_host_buffer: Allocating kv hierarchical KV host pool: 6427520 tokens, 210.62 GB host memory.
INFO mamba.__init__: Allocating 189.66 GB host memory for hierarchical Mamba cache (layout=page_first).
plugins ['GDS', 'LIBFABRIC', 'POSIX']
"""


class BootFactTests(unittest.TestCase):
    def test_boot_facts(self) -> None:
        f = md.parse_boot_facts(BOOT_LOG)
        self.assertEqual(f["worker_ids"], [194172077015596])
        self.assertTrue(f["libfabric_backend"])
        self.assertEqual(f["gpu_pin"], "GPU-4a386bb8-419b-d695-7637-a379b79aa166")
        self.assertEqual(f["l2_kv_tokens"], 6427520)
        self.assertAlmostEqual(f["l2_kv_gb"], 210.62)
        self.assertAlmostEqual(f["l2_mamba_gb"], 189.66)

    def test_plugin_list_is_not_a_libfabric_backend(self) -> None:
        self.assertFalse(md.parse_boot_facts("plugins ['GDS', 'LIBFABRIC', 'POSIX']\n")["libfabric_backend"])


def raw_line(rid: str, pw: int, dw: int, *, with_nvext: bool = True) -> str:
    chunk: dict[str, Any] = {"choices": [{"delta": {"content": "x"}}]}
    if with_nvext:
        chunk["nvext"] = {"worker_id": {"prefill_worker_id": pw, "decode_worker_id": dw}}
    return json.dumps({"metadata": {"x_request_id": rid, "conversation_id": f"c-{rid}", "turn_index": 0,
                                    "benchmark_phase": "profiling", "request_start_ns": 1},
                       "payload": {"messages": [{"role": "user", "content": "never logged"}]},
                       "responses": [{"perf_ns": 1, "packets": [{"name": "data", "value": json.dumps(chunk)}]},
                                     {"perf_ns": 2, "packets": [{"name": "data", "value": "[DONE]"}]}]})


class WorkerIdTests(unittest.TestCase):
    def test_extract_and_summarise_without_prompt_content(self) -> None:
        lines = [raw_line("r1", 11, 21), raw_line("r2", 12, 21), raw_line("r3", 11, 22), raw_line("r4", 0, 0, with_nvext=False)]
        recs = md.extract_worker_ids(lines)
        self.assertEqual([r["x_request_id"] for r in recs], ["r1", "r2", "r3", "r4"])
        self.assertNotIn("never logged", json.dumps(recs))
        self.assertIsNone(recs[3]["prefill_worker_id"])
        s = md.worker_id_summary(recs, {11: "p-a", 12: "p-b", 21: "d-a", 22: "d-b"})
        self.assertEqual(s["requests"], 4)
        self.assertEqual(s["with_worker_id"], 3)
        self.assertEqual(s["prefill_pods"], {"p-a": 2, "p-b": 1})
        self.assertEqual(s["decode_pods"], {"d-a": 2, "d-b": 1})
        self.assertEqual(s["pd_pairs"], {"p-a->d-a": 1, "p-b->d-a": 1, "p-a->d-b": 1})


class ValidityTests(unittest.TestCase):
    @staticmethod
    def exceeds(errors: int, total: int) -> bool:
        from demo_bench.application.validity import exceeds_error_limit

        return exceeds_error_limit(errors, total)

    def base(self, **kw: object) -> dict[str, object]:
        args: dict[str, object] = {"aiperf_rc": 0, "trace_rows": 1000, "profiling_rows": 1000, "errored_rows": 0,
                                   "collapsed": 0, "restarted": [], "reset_ok": True, "cut_by_hard_stop": False,
                                   "exceeds": self.exceeds}
        args.update(kw)
        return md.validity(**args)  # type: ignore[arg-type]

    def test_clean_run_is_valid(self) -> None:
        self.assertTrue(self.base()["valid"])

    def test_half_percent_boundary_counts_collapsed_and_missing(self) -> None:
        self.assertTrue(self.base(errored_rows=2, collapsed=2, profiling_rows=999)["valid"])  # 5/1000 = 0.5 %
        v = self.base(errored_rows=2, collapsed=2, profiling_rows=998)  # 6/1000
        self.assertFalse(v["valid"])
        self.assertEqual(v["missing_rows"], 2)

    def test_restart_reset_cut_rc_gpu_transport(self) -> None:
        self.assertFalse(self.base(restarted=["w"])["valid"])
        self.assertFalse(self.base(reset_ok=False)["valid"])
        self.assertFalse(self.base(cut_by_hard_stop=True)["valid"])
        self.assertFalse(self.base(aiperf_rc=1)["valid"])
        self.assertFalse(self.base(extra_reasons=["GPU uniqueness failed"])["valid"])

    def test_restarted_pods(self) -> None:
        a = [{"name": "x", "uid": "1", "restarts": 0}, {"name": "y", "uid": "2", "restarts": 0}]
        b = [{"name": "x", "uid": "1", "restarts": 1}, {"name": "z", "uid": "3", "restarts": 0}]
        self.assertEqual(md.restarted_pods(a, b), ["x", "y", "z"])
        self.assertEqual(md.restarted_pods(a, a), [])


@dataclass
class S:
    klass: str
    ttft_s: float | None
    itl_ms: float | None
    error: bool = False


class CalibrationTests(unittest.TestCase):
    TH: ClassVar[dict[str, float]] = {"ttft_s": 2.0, "itl_ms": 25.0, "cold_factor": 1.4, "cold_prefill_tok_s": 1.0}

    def test_in_band_keeps_the_candidate(self) -> None:
        r = md.calibrate_rule([], dict(self.TH), (0.4, 0.8), lambda th: 0.5)
        self.assertFalse(r["adjusted"])
        self.assertTrue(r["in_band"])
        self.assertIn("kept", r["rule_applied"])

    def samples(self) -> list[S]:
        return [S("continuation", float(t), 30.0) for t in range(1, 11)] + [S("entry", 99.0, 99.0)]

    def att(self, t: dict[str, float]) -> float:
        judged = [s for s in self.samples() if s.klass != "entry"]
        ok = [s for s in judged if s.ttft_s is not None and s.itl_ms is not None
              and s.ttft_s <= t["ttft_s"] and s.itl_ms <= t["itl_ms"]]
        return len(ok) / len(judged)

    def test_out_of_band_moves_ttft_to_p60_and_itl_when_binding(self) -> None:
        r = md.calibrate_rule(self.samples(), dict(self.TH), (0.4, 0.8), self.att)
        self.assertTrue(r["adjusted"])
        self.assertAlmostEqual(r["slo"]["ttft_s"], md.percentile([float(t) for t in range(1, 11)], 0.6))
        self.assertTrue(r["itl_binding"])  # every ITL is 30 > 25 ms: ITL-only attainment 0 < TTFT-only
        self.assertAlmostEqual(r["slo"]["itl_ms"], 30.0)
        self.assertAlmostEqual(r["attainment"], 0.6)
        self.assertIn("p60", r["rule_applied"])
        self.assertTrue(r["frozen_after_one_adjustment"])

    def test_deterministic(self) -> None:
        a = md.calibrate_rule(self.samples(), dict(self.TH), (0.4, 0.8), self.att)
        b = md.calibrate_rule(list(reversed(self.samples())), dict(self.TH), (0.4, 0.8), self.att)
        self.assertEqual(json.dumps(a, sort_keys=True), json.dumps(b, sort_keys=True))


class WatchdogTests(unittest.TestCase):
    def test_window(self) -> None:
        hs, end = md.iso_epoch("2026-10-24T04:30:00Z"), md.iso_epoch("2026-10-24T05:00:00Z")
        self.assertEqual(wd.decide(hs - 1, hs, end), "before-hard-stop")
        self.assertEqual(wd.decide(hs, hs, end), "act")
        self.assertEqual(wd.decide(end - 1, hs, end), "act")
        self.assertEqual(wd.decide(end, hs, end), "after-window")

    def test_delete_commands_are_label_scoped(self) -> None:
        # v4 contract: DGDs per namespace first, then the capture, compose and driver Jobs (driver last); selectors only.
        cmds = wd.delete_commands(["s-agg", "s-dis-hic"])
        tail = ["--ignore-not-found", "--wait=false"]
        part_of = "app.kubernetes.io/part-of=qwen38-matrix"
        self.assertEqual(cmds, [
            ["kubectl", "delete", "dgd", "-n", "s-agg", "-l", part_of, *tail],
            ["kubectl", "delete", "dgd", "-n", "s-dis-hic", "-l", part_of, *tail],
            ["kubectl", "delete", "job", "-n", "bench", "-l", "app.kubernetes.io/name=demo-capture", *tail],
            ["kubectl", "delete", "job", "-n", "bench", "-l", "app.kubernetes.io/name=take-compose", *tail],
            ["kubectl", "delete", "job", "-n", "bench", "-l", "app.kubernetes.io/name=q38-matrix-driver", *tail],
        ])
        self.assertEqual(cmds[0], ["kubectl", "delete", "dgd", "-n", "s-agg", "-l",
                                   "app.kubernetes.io/part-of=qwen38-matrix", "--ignore-not-found", "--wait=false"])
        self.assertEqual(cmds[-1], ["kubectl", "delete", "job", "-n", "bench", "-l",
                                    "app.kubernetes.io/name=q38-matrix-driver", "--ignore-not-found", "--wait=false"])
        self.assertEqual(len(cmds), 5)
        for c in cmds:  # never a name: kubectl refuses name + selector
            self.assertEqual(c.count("-l"), 1)


class MiscTests(unittest.TestCase):
    def test_canary_is_deterministic_and_in_range(self) -> None:
        a = md.canary_tokens("k", 2048)
        self.assertEqual(a, md.canary_tokens("k", 2048))
        self.assertNotEqual(a, md.canary_tokens("k2", 2048))
        self.assertTrue(all(md.TOKEN_ID_LOW <= t <= md.TOKEN_ID_HIGH for t in a))

    def test_estimate(self) -> None:
        est = dict(md.DEFAULT_ESTIMATE)
        self.assertEqual(md.run_estimate_s(est, 100.0, False), est["reset_s"] + 100 + est["drain_s"])
        self.assertEqual(md.run_estimate_s(est, 100.0, True) - md.run_estimate_s(est, 100.0, False), est["boot_s"])

    def test_cells_cover_the_factorial(self) -> None:
        self.assertEqual(sorted(md.CELLS), [f"S{i}" for i in range(8)])
        for topology in md.TOPOLOGIES:
            modes = sorted(m for t, m in md.CELLS.values() if t == topology)
            self.assertEqual(modes, ["kv", "round-robin"])

    def test_aiperf_asks_for_nvext_worker_ids_and_raw_export(self) -> None:
        cmd = md.aiperf_cmd("http://fe:8000", md.MODEL, Path("/w2.jsonl"), Path("/art"), 43)
        self.assertEqual(cmd[cmd.index("--tokenizer") + 1], "Qwen/Qwen3.8-27B-FP8")  # repo id, never a path
        i = cmd.index("--extra-inputs")
        self.assertEqual(cmd[i + 1], "ignore_eos:true")
        self.assertEqual(json.loads(cmd[i + 2]), {"nvext": {"extra_fields": ["worker_id", "timing"]}})
        self.assertEqual(cmd[cmd.index("--export-level") + 1], "raw")

    def test_hf_cache_layout_for_aiperf_offline(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "tok"
            src.mkdir()
            for name in ("tokenizer.json", "tokenizer_config.json", "SHA256SUMS"):
                (src / name).write_text(name)
            snap = md.hf_cache_layout(src, Path(tmp) / "hub", md.MODEL, md.MODEL_REVISION)
            root = Path(tmp) / "hub" / "models--Qwen--Qwen3.8-27B-FP8"
            self.assertEqual(snap, root / "snapshots" / md.MODEL_REVISION)
            self.assertEqual((root / "refs" / "main").read_text(), md.MODEL_REVISION)
            self.assertEqual(sorted(p.name for p in snap.iterdir()), ["tokenizer.json", "tokenizer_config.json"])

    def test_l3_cleanup_pod_is_scoped_to_the_namespace_dir(self) -> None:
        pod = md.l3_cleanup_pod("s-dis-hic", "q38-l3-cleanup-x", "img@sha256:1", "/mnt/.ephemeral/hicache-l3/qwen38-matrix",
                                "node-1")
        vol = pod["spec"]["volumes"][0]["hostPath"]
        self.assertEqual(vol["path"], "/mnt/.ephemeral/hicache-l3/qwen38-matrix/s-dis-hic")
        self.assertEqual(pod["spec"]["nodeSelector"], {"kubernetes.io/hostname": "node-1"})
        self.assertNotIn("nvidia.com/gpu", json.dumps(pod["spec"]["containers"][0]["resources"]))  # tolerates, never requests
        self.assertEqual(pod["metadata"]["labels"]["app.kubernetes.io/part-of"], "qwen38-matrix")
        with self.assertRaises(ValueError):
            md.l3_cleanup_pod("s-agg", "x", "img", "/mnt/.ephemeral/hicache-l3/qwen38-matrix", "node-1")


if __name__ == "__main__":
    unittest.main()
