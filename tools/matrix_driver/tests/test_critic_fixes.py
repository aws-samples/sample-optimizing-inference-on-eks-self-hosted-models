# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""v4 critic-fix behaviours on the fake cluster (promoted from raw/v4-61-verify_fixes.py; no driver code touched).

Run from this directory:
  PYTHONPATH=../wheel/demo_bench-0.1.0-py3-none-any.whl WHEEL_DIR=../wheel python3 -m unittest -v test_critic_fixes
"""

from __future__ import annotations

import contextlib
import json
import shutil
import subprocess
import tempfile
import time
import unittest
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest import mock

import fake_cluster as fc
import matrix_driver as md
import test_failure_paths as tfp


class Cluster(fc.FakeCluster):
    """FakeCluster + a driver-Job list, delete job, and a capture.json written when a capture Job is created."""

    other_driver_active = False
    meta: dict[str, Any] | None = None

    def _kubectl(self, a: list[str], stdin: str | None) -> subprocess.CompletedProcess[str]:
        if a[:2] == ["get", "job"] and "app.kubernetes.io/name=q38-matrix-driver" in a:
            items: list[dict[str, Any]] = [{"metadata": {"name": "q38-matrix-test"}, "status": {"active": 1}}]
            if self.other_driver_active:
                items.append({"metadata": {"name": "q38-matrix-e6"}, "status": {"active": 1}})
            items.append({"metadata": {"name": "q38-matrix-e4"},
                          "status": {"conditions": [{"type": "Complete", "status": "True"}]}})
            return fc.done(a, json.dumps({"items": items}))
        if a[:2] == ["get", "pods"] and self._ns(a) == "bench":
            return fc.done(a, json.dumps({"items": []}))
        if a[:2] == ["delete", "job"]:
            sel = a[a.index("-l") + 1]
            gone = [n for n, j in self.jobs.items()
                    if all(j["metadata"]["labels"].get(k) == v for k, v in (kv.split("=", 1) for kv in sel.split(",")))
                    or (sel.startswith("batch.kubernetes.io/job-name=") and n == sel.split("=", 1)[1])]
            for n in gone:
                self.jobs.pop(n)
            self.events.append(("delete-job", sel, *a[a.index("--ignore-not-found") + 1:]))
            return fc.done(a, "".join(f'job.batch "{n}" deleted\n' for n in gone))
        return super()._kubectl(a, stdin)

    def _create_job(self, a: list[str], job: dict[str, Any]) -> subprocess.CompletedProcess[str]:
        r = super()._create_job(a, job)
        env = {e["name"]: e.get("value") for e in job["spec"]["template"]["spec"]["containers"][0].get("env", [])}
        if r.returncode == 0 and "TAKE_ID" in env and self.meta is not None and self.s3 is not None:
            key = f"{env['S3_PREFIX']}/{env['TAKE_ID']}/{env['TAKE_ID']}-capture.json"
            self.s3.objects[key] = json.dumps({**self.meta, "take_id": env["TAKE_ID"]}).encode()
        return r


class Base(unittest.TestCase):
    @contextlib.contextmanager
    def offline(self, text: str) -> Iterator[Any]:
        tmp = Path(tempfile.mkdtemp(prefix="v4crit-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        with mock.patch.object(fc, "FakeCluster", Cluster), fc.offline(text, tmp) as t:
            yield t


def summary(s3: fc.FakeS3) -> dict[str, Any]:
    return json.loads(next(v for k, v in s3.objects.items() if "/_driver/driver-summary-" in k))


class CheckNoOtherDriverTests(Base):
    def test_another_active_driver_job_refuses_before_any_cluster_change(self) -> None:
        with self.offline(tfp.plan("take S0 1")) as (d, cl, s3):
            cl.other_driver_active = True
            cl.s3 = s3
            rc = d.execute()
            sm = summary(s3)
        self.assertEqual(rc, 2)
        self.assertIn("q38-matrix-e6", sm.get("refused", ""))
        self.assertEqual([e for e in cl.events if e[0] in tfp.MUTATING], [])
        self.assertEqual(cl.jobs, {})

    def test_own_and_finished_driver_jobs_are_accepted(self) -> None:
        with self.offline(tfp.plan("take S0 1")) as (d, cl, s3):
            cl.s3 = s3
            rc = d.execute()
            sm = summary(s3)
        self.assertEqual(rc, 0)
        self.assertEqual(sm["preflight"]["driver_jobs"], ["q38-matrix-e4", "q38-matrix-test"])


class CaptureJobTests(Base):
    def test_a_stale_capture_job_is_replaced_once_with_a_foreground_delete(self) -> None:
        with self.offline(tfp.plan("take S0 1")) as (d, cl, s3):
            cl.s3 = s3
            stale = md.capture_job(run_id="q38-t", cell="S0", rep=1, window=md.take_window(time.time(), 10, 1),
                                   hard_stop_epoch=int(time.time()) + 9999, now=time.time())
            cl.jobs[stale["metadata"]["name"]] = stale
            rc = d.execute()
            run = json.loads(s3.objects["matrix/q38-t/S0/rep1/run.json"])
            ev = list(cl.events)
        dels = [e for e in ev if e[0] == "delete-job"]
        self.assertEqual(rc, 0)
        self.assertEqual(run["take"]["capture"]["status"], "ok")
        self.assertEqual(len(dels), 1)
        self.assertIn("--cascade=foreground", dels[0])
        self.assertEqual([e[1] for e in ev if e[0] == "create-job"], ["demo-capture-q38-t-s0-rep1"])

    def test_an_empty_webm_fails_the_capture_but_not_the_run(self) -> None:
        with self.offline(tfp.plan("take S0 1")) as (d, cl, s3):
            cl.s3 = s3
            cl.meta = {"status": "ok", "video_t0_ms": 1, "video_end_ms": 2, "webm_bytes": 0}
            rc = d.execute()
            run = json.loads(s3.objects["matrix/q38-t/S0/rep1/run.json"])
        self.assertEqual(rc, 0)
        self.assertEqual(run["take"]["capture"]["status"], "failed")
        self.assertIn("empty", run["take"]["capture"]["reason"])
        self.assertTrue(run["validity"]["valid"])


class ResultOfRecordTests(Base):
    def test_a_take_is_not_a_result_of_record_and_carries_media_and_sync(self) -> None:
        with self.offline(tfp.plan("take S0 1")) as (d, cl, s3):
            cl.s3 = s3
            now = int(time.time() * 1000)
            cl.meta = {"status": "ok", "video_t0_ms": now, "video_end_ms": now + 5000, "webm_bytes": 1234,
                       "frames": 7, "frame_chunks": 1, "frame_chunks_failed": 0, "frames_bytes": 99,
                       "data_check": {"ok": True}}
            rc = d.execute()
            run = json.loads(s3.objects["matrix/q38-t/S0/rep1/run.json"])
            step = summary(s3)["steps"][0]
        cap = run["take"]["capture"]
        self.assertEqual(rc, 0)
        self.assertIs(run["result_of_record"], False)
        self.assertEqual(cap["media"]["webm_bytes"], 1234)
        self.assertEqual(cap["sync"]["sync_source"], "aiperf start")
        self.assertEqual(step["sync_ms"], cap["sync"]["sync_ms"])

    def test_a_plain_run_is_a_result_of_record_without_a_take_block(self) -> None:
        with self.offline(tfp.plan("run S0 1")) as (d, _cl, s3):
            rc = d.execute()
            run = json.loads(s3.objects["matrix/q38-t/S0/rep1/run.json"])
        self.assertEqual(rc, 0)
        self.assertIs(run["result_of_record"], True)
        self.assertNotIn("take", run)


if __name__ == "__main__":
    unittest.main()
