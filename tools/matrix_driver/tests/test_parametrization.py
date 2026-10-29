# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The only behaviour change of the port: region, ffmpeg mirror image and bucket come from the environment.

The original driver hard-coded its region as a default (S3 client) and its ECR ffmpeg mirror as a constant.
In the repo those values live only in manifests/config/platform.env (+ driver/images.env), so the driver must
read them from the Job environment and refuse to start without them (no silent default to a real region).
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import fake_cluster as fc
import matrix_driver as md
from support import TOOL, shipped_plan_text

LITERALS = (
    re.compile(r"(?<![A-Za-z0-9])(?:us|eu|ap|sa|ca|me|af|il)-(?:north|south|east|west|central|northeast|southeast)-[0-9](?![A-Za-z0-9])"),
    re.compile(r"(?<![A-Za-z0-9])[0-9]{12}(?![A-Za-z0-9])"),
    re.compile(r"\.dkr\.ecr\."),
    re.compile(r"(?:kubecon" + r"-dynamo|dynamo-sample)-[a-z]"),
    re.compile(r"(?<![A-Za-z0-9])(?:use|usw|euw|euc|eun|aps|apn|sae|cac)[0-9](?![A-Za-z0-9])"),  # any region short code
)
BASE_ENV = {"RESULTS_BUCKET": "test-bucket", "AWS_REGION": fc.REGION, "COMPOSE_FFMPEG_IMAGE": fc.COMPOSE_FFMPEG_IMAGE}
FAR_FUTURE = "2099-01-01T00:00:00Z"


def far_plan(name: str) -> str:
    """A shipped plan whose hard stop is far in the future (so the start-up refusals under test are not 'passed')."""
    return re.sub(r"(?m)^hard_stop \S+$", f"hard_stop {FAR_FUTURE}", shipped_plan_text(name))


class NoLiteralsInThePortedSources(unittest.TestCase):
    def test_no_region_account_registry_or_bucket_literal_in_any_ported_python_or_shell_file(self) -> None:
        own = Path(__file__).name  # it holds the patterns themselves
        files = sorted(f for f in [*TOOL.glob("*.py"), *TOOL.glob("takes/*"), *(TOOL / "tests").glob("*.py")] if f.name != own)
        self.assertTrue([f for f in files if f.name == "matrix_driver.py"], "tools/matrix_driver/matrix_driver.py missing")
        for f in files:
            text = f.read_text(errors="ignore")
            for pat in LITERALS:
                with self.subTest(file=f.name, pattern=pat.pattern):
                    self.assertEqual(pat.findall(text), [], f"{f.relative_to(TOOL)}: literal outside platform.env")


class RegionComesFromTheEnvironment(unittest.TestCase):
    def build(self, env: dict[str, str]) -> md.Driver:
        plan = md.parse_plan(shipped_plan_text("plan-e3"))
        keep = {"WORK": tempfile.mkdtemp(prefix="q38-param-"), "DRIVER_DIR": str(TOOL), "DGD_DIR": str(fc.DGD_DIR)}
        with mock.patch.dict(os.environ, {**keep, **env}, clear=True):
            return md.Driver(plan, None)

    def test_the_s3_client_uses_the_configured_region(self) -> None:
        d = self.build(BASE_ENV)
        self.assertEqual(d.s3.region, fc.REGION)
        self.assertEqual(d.s3.bucket, "test-bucket")

    def test_missing_region_is_refused_not_defaulted(self) -> None:
        env = {k: v for k, v in BASE_ENV.items() if k != "AWS_REGION"}
        with self.assertRaises((KeyError, md.ConfigError)) as cm:
            self.build(env)
        self.assertIn("AWS_REGION", str(cm.exception))


class FfmpegMirrorComesFromTheEnvironment(unittest.TestCase):
    NOW = float(md.iso_epoch("2026-10-23T22:00:00Z"))

    def spec(self) -> dict[str, object]:
        from test_v4_takes import ComposeSpecAndJobTests

        return ComposeSpecAndJobTests().spec()

    def test_the_compose_render_container_runs_the_configured_ffmpeg_image(self) -> None:
        with mock.patch.dict(os.environ, BASE_ENV):
            job = md.compose_job(self.spec(), hard_stop_epoch=int(self.NOW) + 20000, now=self.NOW)
        images = {c["name"]: c["image"] for c in job["spec"]["template"]["spec"]["containers"]}
        images.update({c["name"]: c["image"] for c in job["spec"]["template"]["spec"].get("initContainers", [])})
        self.assertIn(fc.COMPOSE_FFMPEG_IMAGE, images.values())

    def test_the_take_preflight_job_checks_the_configured_ffmpeg_image(self) -> None:
        with mock.patch.dict(os.environ, BASE_ENV):
            job = md.take_preflight_job(run_id="q38-e7", bucket="test-bucket", region=fc.REGION,
                                        grafana_url="http://grafana.test")
        inits = {c["name"]: c["image"] for c in job["spec"]["template"]["spec"]["initContainers"]}
        self.assertEqual(inits["ffmpeg"], fc.COMPOSE_FFMPEG_IMAGE)

    def test_compose_without_the_image_is_refused_and_names_the_variable(self) -> None:
        env = {k: v for k, v in BASE_ENV.items() if k != "COMPOSE_FFMPEG_IMAGE"}
        with mock.patch.dict(os.environ, env, clear=True), self.assertRaises(Exception) as cm:
            md.compose_job(self.spec(), hard_stop_epoch=int(self.NOW) + 20000, now=self.NOW)
        self.assertIn("COMPOSE_FFMPEG_IMAGE", str(cm.exception))


class StartUpPreflightChecksTheNewVariables(unittest.TestCase):
    """``main`` refuses (exit 2, before any cluster call) when a variable the Job must carry is missing."""

    FULL = {**BASE_ENV, "CB_NODE": "i-test", "HARD_STOP": FAR_FUTURE, "JOB_NAME": "q38-matrix-test",
            "L3_CLEANUP_IMAGE": "img@sha256:test", "GRAFANA_URL": "http://grafana.test", "WORK": "/nonexistent-work"}

    def run_main(self, plan_name: str, env: dict[str, str]) -> tuple[int, str]:
        import contextlib
        import io

        buf = io.StringIO()
        with tempfile.TemporaryDirectory(prefix="q38-param-") as tmp:
            plan_file = Path(tmp) / "plan.txt"
            plan_file.write_text(far_plan(plan_name))
            with (mock.patch.dict(os.environ, env, clear=True),
                  mock.patch.object(md, "kubectl", side_effect=AssertionError("cluster touched before the refusal")),
                  contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf)):
                rc = md.main(["--plan", str(plan_file)])
        return rc, buf.getvalue()

    def test_a_plan_with_takes_needs_the_ffmpeg_image(self) -> None:
        env = {k: v for k, v in self.FULL.items() if k != "COMPOSE_FFMPEG_IMAGE"}
        rc, out = self.run_main("plan-e7", env)
        self.assertEqual(rc, 2, out)
        self.assertIn("COMPOSE_FFMPEG_IMAGE", out)

    def test_every_plan_needs_the_region(self) -> None:
        env = {k: v for k, v in self.FULL.items() if k != "AWS_REGION"}
        rc, out = self.run_main("plan-e4", env)
        self.assertEqual(rc, 2, out)
        self.assertIn("AWS_REGION", out)


if __name__ == "__main__":
    unittest.main()
