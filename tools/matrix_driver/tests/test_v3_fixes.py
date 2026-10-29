# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""v3 fixes of the matrix driver (E3 first in-cluster run, 2026-10-23).

1. T-RTR-2 positive control: on the Qwen3.8 hybrid engine a prefix hits only from its 3rd send to the same engine
   (measured live on S0, raw/v3-10). v2 sent each canary twice, so the control could never hit (rr or kv). v3 re-sends
   full passes until a pass hits; under rr the post-reset repeat must land on the owners that hit.
2. Teardown: v2 ran ``kubectl delete dgd q38 -l ...`` with check=False; kubectl refuses a name together with a
   selector, so the DGD was never deleted. v3 deletes by label + field selector, fails on a refused delete and waits
   for the DGD object, not only for its pods.

Run: WHEEL_DIR=../wheel PYTHONPATH=<demo_bench wheel> python3 -m unittest -v test_v3_fixes   (from this directory)
"""

from __future__ import annotations

import json
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

import fake_cluster as fc
import matrix_driver as md


def plan(steps: str, *, extra: str = "") -> str:
    sha = "a" * 64
    hs = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 86400))
    return (f"run_id q38-t\nhard_stop {hs}\n{extra}"
            f"w2 trace=s3://test-bucket/t.jsonl trace_sha256={sha} sidecar=s3://test-bucket/s.jsonl "
            f"sidecar_sha256={sha} frozen=yes\ntokenizer prefix=s3://test-bucket/tok/ sums_sha256={sha}\n"
            "slo ttft_s=2 itl_ms=25 cold_factor=1.4 cold_prefill_tok_s=30000\n"
            "estimate boot_s=1 reset_s=1 drain_s=1 probe_s=1\n" + steps + "\n")


class RrEngines:
    """n engines behind a strictly cyclic round-robin cursor; a prompt hits from its ``hit_on``-th send to an engine."""

    def __init__(self, n: int, hit_on: int = 3) -> None:
        self.n, self.hit_on, self.cursor = n, hit_on, 0
        self.seen: dict[tuple[int, tuple[int, ...]], int] = {}

    def send(self, prompt: list[int]) -> dict[str, Any]:
        w = self.cursor % self.n
        self.cursor += 1
        k = (w, tuple(prompt))
        seen = self.seen.get(k, 0)
        self.seen[k] = seen + 1
        cached = len(prompt) - 64 if seen >= self.hit_on - 1 else 0
        return {"ok": True, "cached_tokens": cached, "worker_id": {"prefill_worker_id": 100 + w, "decode_worker_id": 100 + w}}


def usage_pass_hit(n: int) -> Any:
    return lambda recs: sum(r["cached_tokens"] for r in recs) >= 0.5 * n * 2048


class PositiveControlUnitTests(unittest.TestCase):
    def setUp(self) -> None:
        self.canaries = [md.canary_tokens(f"c-{i}", 2048) for i in range(8)]

    def test_rr_hits_on_the_third_send_with_aligned_owners(self) -> None:
        eng = RrEngines(8)
        first = [eng.send(c) for c in self.canaries]
        out = md.positive_control(eng.send, self.canaries, first, usage_pass_hit(8))
        self.assertTrue(out["hit"])
        self.assertEqual(out["passes"], 3)  # 1st pass + 2 control passes: the 3rd send of each canary hits
        self.assertEqual(out["requests"], 16)
        self.assertEqual(out["ok_requests"], 16)
        self.assertEqual(out["hit_owner"], [100 + i for i in range(8)])  # rr: canary i on engine i in every pass
        self.assertEqual([s[1] for s in out["sends"][0]], [0, 0, 1984])

    def test_rr_wraps_around_even_when_the_cursor_starts_mid_cycle(self) -> None:
        eng = RrEngines(8)
        eng.cursor = 5  # warm-up and the cold-prefill probe advanced the cursor
        first = [eng.send(c) for c in self.canaries]
        out = md.positive_control(eng.send, self.canaries, first, usage_pass_hit(8))
        self.assertTrue(out["hit"])
        self.assertEqual(out["hit_owner"], [100 + (5 + i) % 8 for i in range(8)])

    def test_the_v2_two_send_control_cannot_hit_on_this_engine(self) -> None:
        eng = RrEngines(8)
        first = [eng.send(c) for c in self.canaries]
        out = md.positive_control(eng.send, self.canaries, first, usage_pass_hit(8), max_sends=2)
        self.assertFalse(out["hit"])
        self.assertEqual(out["hit_owner"], [None] * 8)

    def test_no_hit_within_the_budget_is_reported_not_raised(self) -> None:
        eng = RrEngines(8, hit_on=99)
        first = [eng.send(c) for c in self.canaries]
        out = md.positive_control(eng.send, self.canaries, first, usage_pass_hit(8))
        self.assertFalse(out["hit"])
        self.assertEqual(out["passes"], md.CONTROL_MAX_SENDS)
        self.assertEqual(len(out["sends"][0]), md.CONTROL_MAX_SENDS)

    def test_owner_id_reads_the_prefill_worker(self) -> None:
        self.assertEqual(md.owner_id({"worker_id": {"prefill_worker_id": 7, "decode_worker_id": 9}}), 7)
        self.assertEqual(md.owner_id({"worker_id": 5}), 5)
        self.assertIsNone(md.owner_id({}))


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="q38-v3-"))

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp)


class ResetUnderRrAndKvTests(Base):
    def check_cell(self, cell: str) -> dict[str, Any]:
        topology = md.CELLS[cell][0]
        with fc.offline(plan(f"run {cell} 1"), self.tmp) as (d, cluster, _):
            self.assertEqual(cluster.hit_on_send, 3)  # the measured engine semantics
            d.ensure(cell, self.tmp / "run")
            doc = d.reset_and_verify(topology, f"{cell}/rep1", self.tmp / "run")
        self.assertTrue(doc["ok"], doc["checks"])
        self.assertTrue(doc["checks"]["positive_control_hit"])
        self.assertEqual(doc["positive_control"]["passes"], 3)
        self.assertGreaterEqual(doc["positive_control_cached_tokens"], 0.5 * doc["canaries"] * 2048)
        self.assertEqual(doc["repeat_usage_cached_tokens"], [0] * doc["canaries"])
        self.assertEqual(doc["repeat_engine_cached_tokens_delta"], 0)
        return doc

    def test_s0_agg_rr_passes_the_reset_check(self) -> None:
        doc = self.check_cell("S0")
        self.assertEqual(doc["router_mode"], "round-robin")
        self.assertEqual(doc["repeat_on_hit_owner"], 8)  # the zero is measured on the engines that hit
        self.assertTrue(doc["checks"]["repeat_on_hit_owner_rr"])

    def test_s1_agg_kv_passes_the_reset_check(self) -> None:
        doc = self.check_cell("S1")
        self.assertEqual(doc["router_mode"], "kv")
        self.assertTrue(doc["checks"]["router_predicted_zero"])

    def test_s7_pd_hicache_kv_passes_the_reset_check(self) -> None:
        doc = self.check_cell("S7")
        self.assertEqual(doc["canaries"], 4)  # one per prefill engine
        self.assertTrue(doc["checks"]["l3_zero_final"])

    def test_s0_full_run_reaches_aiperf(self) -> None:
        with fc.offline(plan("run S0 1\nteardown", extra="on_failure abort\n"), self.tmp) as (d, _, s3):
            rc = d.execute()
        self.assertEqual(rc, 0)
        run = json.loads(s3.objects["matrix/q38-t/S0/rep1/run.json"])
        self.assertTrue(run["reset"]["ok"])
        self.assertTrue(run["validity"]["valid"], run["validity"])

    def test_broken_flush_is_still_caught_after_a_three_send_control(self) -> None:
        with fc.offline(plan("run S0 1"), self.tmp) as (d, cluster, _):
            cluster.flush_broken.add("s-agg")
            d.ensure("S0", self.tmp / "run")
            with self.assertRaises(md.StepError) as ctx:
                d.reset_and_verify("t-agg", "S0/rep1", self.tmp / "run")
        self.assertIn("repeat_cached_zero", str(ctx.exception))
        self.assertNotIn("positive_control_hit", str(ctx.exception))

    def test_rr_repeat_off_the_hit_owners_is_not_accepted_as_a_zero(self) -> None:
        with fc.offline(plan("run S0 1"), self.tmp) as (d, cluster, _):
            d.ensure("S0", self.tmp / "run")
            real = cluster.http_req
            bumped: list[str] = []

            def bump(method: str, url: str, body: dict[str, Any] | None = None, timeout: float = 60) -> tuple[int, bytes]:
                if url.endswith("/engine/flush_cache") and not bumped:
                    bumped.append(url)
                    cluster.rr["s-agg"] = cluster.rr.get("s-agg", 0) + 1  # ONE foreign request moved the rr cursor
                return real(method, url, body, timeout)

            with mock.patch.object(md, "http_req", bump), self.assertRaises(md.StepError) as ctx:
                d.reset_and_verify("t-agg", "S0/rep1", self.tmp / "run")
        self.assertIn("repeat_on_hit_owner_rr", str(ctx.exception))


class TeardownTests(Base):
    def test_kubectl_refuses_a_name_with_a_selector(self) -> None:
        """The fake mirrors real kubectl (raw/v3-50: 'error: name cannot be provided when a selector is specified')."""
        with fc.offline(plan("run S0 1"), self.tmp) as (d, cluster, _):
            d.ensure("S0", self.tmp / "run")
            p = cluster.kubectl("delete", "dgd", md.DGD_NAME, "-n", "s-agg", "-l",
                                f"app.kubernetes.io/part-of={md.PART_OF}", "--ignore-not-found", "--wait=false",
                                check=False)
            self.assertEqual(p.returncode, 1)
            self.assertIn("name cannot be provided when a selector is specified", p.stderr)
            self.assertIn("s-agg", cluster.dgds)  # nothing deleted

    def test_teardown_deletes_the_dgd_by_selector_and_waits_for_it(self) -> None:
        with fc.offline(plan("run S0 1"), self.tmp) as (d, cluster, _):
            d.ensure("S0", self.tmp / "run")
            doc = d.teardown("t-agg")
            argv = cluster.deletes[-1]
            self.assertNotIn(md.DGD_NAME, argv)  # no positional name
            self.assertIn(f"app.kubernetes.io/part-of={md.PART_OF}", argv)
            self.assertEqual(argv[argv.index("--field-selector") + 1], f"metadata.name={md.DGD_NAME}")
            self.assertNotIn("s-agg", cluster.dgds)  # the DGD itself is gone
            self.assertFalse(d.dgd_present("s-agg"))
            self.assertEqual(doc["delete"]["rc"], 0)
            self.assertIn("deleted", doc["delete"]["stdout"])

    def test_a_refused_delete_fails_fast(self) -> None:
        with fc.offline(plan("run S0 1"), self.tmp) as (d, cluster, _):
            d.ensure("S0", self.tmp / "run")
            refused = fc.done(["delete"], "", 1, "error: name cannot be provided when a selector is specified")
            with mock.patch.object(cluster, "_delete_dgd", return_value=refused), \
                    self.assertRaises(md.StepError) as ctx:
                d.teardown("t-agg", wait_s=30)
            self.assertIn("delete refused", str(ctx.exception))

    def test_pods_gone_but_dgd_present_is_a_teardown_failure(self) -> None:
        with fc.offline(plan("run S0 1"), self.tmp) as (d, cluster, _):
            d.ensure("S0", self.tmp / "run")
            cluster.dgd_finalizer_stuck.add("s-agg")
            with self.assertRaises(md.StepError) as ctx:
                d.teardown("t-agg", wait_s=30)
            self.assertIn("dgd_present=True pods_present=False", str(ctx.exception))

    def test_teardown_after_a_failed_reset_leaves_no_dgd(self) -> None:
        with fc.offline(plan("run S0 1\nrun S7 1", extra="on_failure abort\n"), self.tmp) as (d, cluster, s3):
            cluster.broken_deploys["s-agg"] = 1
            rc = d.execute()
        self.assertEqual(rc, 1)
        self.assertEqual(cluster.dgds, {})
        td = [json.loads(v) for k, v in s3.objects.items() if "/_teardown/" in k]
        self.assertTrue(td and all(t["delete"]["rc"] == 0 for t in td))


if __name__ == "__main__":
    unittest.main()
