# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""dryrun_plan.py: the offline dry-run of a shipped plan (fake cluster + fake S3), as an operator runs it before a launch.

The CLI takes the shipped plan with its ``${KEY}`` placeholders; it expands them from manifests/config/platform.env
(``--env`` overrides) before parsing, so the dry-run sees the exact plan the in-cluster driver gets.
"""

from __future__ import annotations

import os
import subprocess
import sys
import unittest

from support import REPO, TOOL, plan_path, run_argv


class DryRun(unittest.TestCase):
    def run_cli(self, plan: str, *extra: str) -> subprocess.CompletedProcess[str]:
        env = {**os.environ, "PYTHONPATH": os.pathsep.join([str(REPO / "tools" / "demo_bench" / "src")]),
               "PYTHONDONTWRITEBYTECODE": "1"}
        return run_argv([sys.executable, str(TOOL / "dryrun_plan.py"), "--plan", str(plan_path(plan)),
                         "--hard-stop-in-s", "40000", *extra],
                        capture_output=True, text=True, check=False, env=env, timeout=300)

    def test_the_e4_matrix_plan_runs_to_the_end_on_the_fake_cluster(self) -> None:
        p = self.run_cli("plan-e4")
        self.assertEqual(p.returncode, 0, p.stderr[-2000:])
        self.assertIn("=== rc 0", p.stdout)
        self.assertIn("=== aborted: None", p.stdout)
        self.assertIn("=== steps", p.stdout)

    def test_the_dry_run_sees_the_expanded_plan(self) -> None:
        p = self.run_cli("plan-e4")
        self.assertNotIn("${", p.stdout)
        self.assertRegex(p.stdout, r"# dry-run: plan plan-e4\.txt sha256 [0-9a-f]{64}")

    def test_an_injected_failure_aborts_the_plan_exactly_as_the_driver_does(self) -> None:
        p = self.run_cli("plan-e3", "--broken-deploys", "s-agg=1")
        self.assertEqual(p.returncode, 0, p.stderr[-2000:])  # the CLI reports; the driver rc is printed
        self.assertIn("=== rc 1", p.stdout)
        self.assertIn("T-RTR-2 reset verification failed", p.stdout)
        self.assertIn("on_failure=abort: run S0/rep1 failed", p.stdout)


if __name__ == "__main__":
    unittest.main()
