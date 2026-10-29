# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Watchdog tests for the E7' hard stop (v4), written before the implementation (RED).

The v3 watchdog knew one window [HARD_STOP, WINDOW_END) = [04:30Z, 05:00Z) of 2026-10-24 and acted on every q38 DGD and
driver Job in it. E7' (plan-e7, hard stop 2026-10-24T09:00:00Z) needs the watchdog to act at 09:00Z too, without killing a
legitimately running E7 at 04:30Z (E7' now starts as soon as E6' ends, ~20:00Z of 10-23, long before 04:30Z).

Contract:
  parse_windows("<start>/<end>[,<start>/<end>...]") -> [(start_epoch, end_epoch), ...]   ValueError on any malformed or
      empty window (end <= start), duplicate start, or empty text
  windows_from_env(env) -> the WINDOWS list when set, else the legacy single [HARD_STOP, WINDOW_END)
  decide_windows(now, windows) -> "act" inside any window, "before-hard-stop" while a window still lies ahead,
      "after-window" when every window has passed
  window_applies(window_start, active_driver_job_hard_stops) -> False when a driver Job with a LATER hard stop is still
      active (a later plan is legitimately running: do not kill it at the earlier window), True otherwise
  delete_commands(namespaces) also deletes the capture Jobs (label app.kubernetes.io/name=demo-capture) and the compose
      Jobs (app.kubernetes.io/name=take-compose) in bench, after the DGDs; remaining() reports them under "jobs".

Run (from this directory): python3 -m unittest -v test_watchdog
"""

from __future__ import annotations

import subprocess
import unittest
from typing import Any
from unittest import mock

import watchdog as wd

M_START, M_END = "2026-10-24T04:30:00Z", "2026-10-24T05:00:00Z"
E_START, E_END = "2026-10-24T09:00:00Z", "2026-10-24T10:00:00Z"
BOTH = f"{M_START}/{M_END},{E_START}/{E_END}"


def ep(ts: str) -> int:
    return wd.iso_epoch(ts)


class WindowParsingTests(unittest.TestCase):
    def test_parse_two_windows(self) -> None:
        self.assertEqual(wd.parse_windows(BOTH), [(ep(M_START), ep(M_END)), (ep(E_START), ep(E_END))])

    def test_parse_one_window_with_spaces_around_commas(self) -> None:
        self.assertEqual(wd.parse_windows(f" {E_START}/{E_END} "), [(ep(E_START), ep(E_END))])

    def test_malformed_windows_are_rejected(self) -> None:
        bad = ["", "garbage", f"{E_START}", f"{E_START}/", f"/{E_END}", f"{E_END}/{E_START}", f"{E_START}/{E_START}",
               f"{E_START}/2026-10-24 10:00", f"{BOTH},{M_START}/{M_END}", f"{M_START}/{M_END};{E_START}/{E_END}"]
        for text in bad:
            with self.subTest(text=text), self.assertRaises(ValueError):
                wd.parse_windows(text)

    def test_windows_from_env_prefers_windows_and_falls_back_to_the_legacy_pair(self) -> None:
        self.assertEqual(wd.windows_from_env({"HARD_STOP": M_START, "WINDOW_END": M_END, "WINDOWS": BOTH}),
                         wd.parse_windows(BOTH))
        self.assertEqual(wd.windows_from_env({"HARD_STOP": M_START, "WINDOW_END": M_END}), [(ep(M_START), ep(M_END))])
        with self.assertRaises((KeyError, ValueError)):
            wd.windows_from_env({})


class DecisionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.W = wd.parse_windows(BOTH)

    def test_acts_at_the_e7_hard_stop_and_through_the_window(self) -> None:
        for ts in (E_START, "2026-10-24T09:00:01Z", "2026-10-24T09:30:00Z", "2026-10-24T09:59:59Z"):
            with self.subTest(ts=ts):
                self.assertEqual(wd.decide_windows(ep(ts), self.W), "act")

    def test_does_not_act_one_second_before_the_e7_hard_stop(self) -> None:
        self.assertEqual(wd.decide_windows(ep(E_START) - 1, self.W), "before-hard-stop")
        self.assertEqual(wd.decide_windows(ep(E_START) - 3600 * 5, self.W), "before-hard-stop")

    def test_the_matrix_window_still_acts(self) -> None:
        self.assertEqual(wd.decide_windows(ep(M_START), self.W), "act")
        self.assertEqual(wd.decide_windows(ep(M_START) - 1, self.W), "before-hard-stop")

    def test_between_the_windows_a_later_window_is_still_ahead(self) -> None:
        self.assertEqual(wd.decide_windows(ep(M_END), self.W), "before-hard-stop")
        self.assertEqual(wd.decide_windows(ep("2026-10-24T07:00:00Z"), self.W), "before-hard-stop")

    def test_after_the_last_window_it_does_nothing(self) -> None:
        self.assertEqual(wd.decide_windows(ep(E_END), self.W), "after-window")
        self.assertEqual(wd.decide_windows(ep(E_END) + 86400, self.W), "after-window")

    def test_the_v3_decide_is_unchanged(self) -> None:
        self.assertEqual(wd.decide(ep(M_START), ep(M_START), ep(M_END)), "act")
        self.assertEqual(wd.decide(ep(M_START) - 1, ep(M_START), ep(M_END)), "before-hard-stop")
        self.assertEqual(wd.decide(ep(M_END), ep(M_START), ep(M_END)), "after-window")


class RunningLaterPlanTests(unittest.TestCase):
    def test_the_matrix_window_does_not_kill_a_running_e7(self) -> None:
        self.assertFalse(wd.window_applies(ep(M_START), [ep(E_START)]))

    def test_the_e7_window_applies_when_only_e7_or_nothing_is_active(self) -> None:
        self.assertTrue(wd.window_applies(ep(E_START), [ep(E_START)]))
        self.assertTrue(wd.window_applies(ep(E_START), []))  # orphan DGDs, no driver left: clean them

    def test_the_matrix_window_applies_to_matrix_jobs_and_to_no_jobs(self) -> None:
        self.assertTrue(wd.window_applies(ep(M_START), [ep(M_START)]))
        self.assertTrue(wd.window_applies(ep(M_START), []))
        self.assertTrue(wd.window_applies(ep(M_START), [ep(M_START), ep(M_START) - 3600]))

    def test_a_single_later_job_is_enough_to_hold_the_earlier_window(self) -> None:
        self.assertFalse(wd.window_applies(ep(M_START), [ep(M_START), ep(E_START)]))


class CommandTests(unittest.TestCase):
    NS = ("s-agg", "s-agg-hic", "s-dis", "s-dis-hic")

    def test_capture_and_compose_jobs_are_deleted_after_the_dgds(self) -> None:
        cmds = wd.delete_commands(list(self.NS))
        flat = [" ".join(c) for c in cmds]
        dgd = [i for i, c in enumerate(flat) if " dgd " in c]
        jobs = [i for i, c in enumerate(flat) if " job " in c]
        self.assertEqual(len(dgd), 4)
        self.assertTrue(any("app.kubernetes.io/name=q38-matrix-driver" in flat[i] for i in jobs))
        self.assertTrue(any("app.kubernetes.io/name=demo-capture" in flat[i] for i in jobs))
        self.assertTrue(any("app.kubernetes.io/name=take-compose" in flat[i] for i in jobs))
        self.assertLess(max(dgd), min(jobs))
        for c in cmds:
            self.assertEqual(c[:2], ["kubectl", "delete"])
            self.assertIn("--ignore-not-found", c)
            self.assertIn("-l", c)  # selector only, never a name (kubectl refuses name + selector)

    def test_remaining_reports_capture_and_compose_jobs(self) -> None:
        def fake_run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
            out = ""
            if "job" in cmd and "app.kubernetes.io/name=demo-capture" in cmd:
                out = "job.batch/demo-capture-q38-e7-s0-rep1\n"
            if "job" in cmd and "app.kubernetes.io/name=take-compose" in cmd:
                out = "job.batch/q38-take-compose-q38-e7\n"
            return subprocess.CompletedProcess("fake", 0, out, "")  # a result record of the fake runner, no command

        with mock.patch.object(wd, "run", fake_run):
            left: dict[str, Any] = wd.remaining()
        self.assertIn("job.batch/demo-capture-q38-e7-s0-rep1", left["jobs"])
        self.assertIn("job.batch/q38-take-compose-q38-e7", left["jobs"])
        self.assertEqual(left["dgds"], [])
        self.assertEqual(left["pods"], [])


if __name__ == "__main__":
    unittest.main()
