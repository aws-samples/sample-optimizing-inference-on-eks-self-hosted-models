# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""render_driver.py: the repo-side replacement of the original render_cells.py (driver half only).

Contract (the developer implements it, the tests are not edited):

* ``load_platform_env(path) -> dict[str, str]``   plain KEY=VALUE files (platform.env, images.env), ``#`` comments.
* ``expand_plan(text, env) -> str``               replaces ``${KEY}`` by ``env[KEY]``; any other text byte-for-byte;
                                                  an unknown ``${KEY}`` raises ``KeyError``/``ValueError`` (no silent hole).
* ``annotated_dgds(src_dir) -> dict[str, str]``   ``{"S0.yaml": text, ...}``: the committed Track Q renders with the
                                                  ``q38-matrix/workers-sha256`` annotation the driver requires
                                                  (sha256 of ``json.dumps(spec.components[1:], sort_keys=True)``).
* ``main(argv) -> int`` CLI (publish cleanup: ``--plans-dir`` and ``--hard-stop-in`` are new; plan-e3..e7 live in
  plans/w2-long-context, the W3-16K plans in plans/w3-16k, plan-w3-r50 is gone)::

      render_driver.py --out DIR --wheel WHEEL [--env platform.env] [--images images.env] [--plans-dir DIR]
                       [--watchdog-windows A/B,C/D] [--watchdog-schedule CRON]
        -> DIR/00-base.yaml  DIR/05-take-preflight.yaml  DIR/20-watchdog.yaml
      render_driver.py --out DIR --launch plan-eN --cb-node ID [--now EPOCH] [--env ..] [--images ..] [--plans-dir DIR]
                       [--hard-stop-in DURATION]
        -> DIR/10-job-plan-eN-<UTC %Y%m%dT%H%M%SZ>.yaml   (activeDeadlineSeconds = hard stop - now)
        with --hard-stop-in also DIR/08-plans-plan-eN-<ts>.yaml and DIR/20-watchdog.yaml (see RelativeHardStop)

Expected objects are those of the recorded runs (golden_driver_objects.json, produced by the original render_cells.py v4
with the deployment values replaced by ``${TOKENS}``); ConfigMap payloads are checked against the repo files themselves.
"""

from __future__ import annotations

import base64
import calendar
import contextlib
import hashlib
import io
import json
import re
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any

import yaml
from support import (
    DRIVER_TEMPLATES,
    FIXTURES,
    PLAN_NAMES,
    PLANS_DIR,
    PLATFORM_ENV,
    RENDERED_DGDS,
    TOOL,
    W2_PLAN_NAMES,
    W2_PLANS_DIR,
    W3_PLAN_NAMES,
    plan_path,
    plans_dir_of,
    run_argv,
)

GOLDEN = json.loads((FIXTURES / "golden_driver_objects.json").read_text())
GOLDEN_FILES = json.loads((FIXTURES / "golden_plans_and_verbatim.json").read_text())
NOW = calendar.timegm((2026, 10, 23, 22, 0, 0))  # before every shipped hard stop
CB_NODE = "i-0123456789abcdef0"
CELLS = ("S0", "S1", "S2", "S3", "S4", "S5", "S6", "S7")
W3LATE_WINDOWS = ("2026-10-24T04:30:00Z/2026-10-24T05:00:00Z,2026-10-24T09:00:00Z/2026-10-24T10:00:00Z,"
                  "2026-10-24T10:50:00Z/2026-10-24T11:20:00Z")
W3LATE_SCHEDULE = "*/5 4,9,10,11 24 10 *"


def rd() -> Any:
    import render_driver

    return render_driver


def values(env_path: Path = PLATFORM_ENV) -> dict[str, str]:
    """The token values the golden objects expect (computed independently of render_driver)."""
    env = _parse_env(env_path)
    img = _parse_env(DRIVER_TEMPLATES / "images.env")
    return {
        "BENCH_IMAGE": f"{env['BENCH_REPO']}:{img['MATRIX_BENCH_TAG']}@{img['MATRIX_BENCH_DIGEST']}",
        "FFMPEG_IMAGE": f"{env['BENCH_REPO']}:{img['MATRIX_FFMPEG_TAG']}@{img['MATRIX_FFMPEG_DIGEST']}",
        "RUNTIME_IMAGE": env["RUNTIME_IMAGE"], "RESULTS_BUCKET": env["RESULTS_BUCKET"],
        "ECR_REGISTRY": env["ECR_REGISTRY"], "AWS_REGION": env["AWS_REGION"], "CB_NODE": CB_NODE,
    }


def _parse_env(path: Path) -> dict[str, str]:
    out = {}
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if line and not line.startswith("#"):
            k, v = line.split("=", 1)
            out[k] = v
    return out


def golden(key: str, vals: dict[str, str] | None = None) -> list[dict[str, Any]]:
    text = json.dumps(GOLDEN[key])
    for name, value in (vals or values()).items():
        text = text.replace("${" + name + "}", value)
    return json.loads(text)


def reduce(doc: dict[str, Any]) -> dict[str, Any]:
    if doc["kind"] == "ConfigMap":
        return {"kind": "ConfigMap", "metadata": doc["metadata"], "data_keys": sorted(doc.get("data", {})),
                "binary_keys": sorted(doc.get("binaryData", {}))}
    return doc


def canon(docs: list[dict[str, Any]]) -> list[str]:
    return sorted(json.dumps(d, sort_keys=True) for d in docs)


def load(path: Path) -> list[dict[str, Any]]:
    return [d for d in yaml.safe_load_all(path.read_text()) if d]


class Base(unittest.TestCase):
    wheel_bytes = b"PK\x03\x04 fake demo_bench wheel \x00\xff"
    wheel_name = "demo_bench-0.1.0-py3-none-any.whl"

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="q38-render-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.wheel = self.tmp / self.wheel_name
        self.wheel.write_bytes(self.wheel_bytes)

    def render(self, *extra: str, out: str = "out") -> Path:
        out_dir = self.tmp / out
        rc = rd().main(["--out", str(out_dir), "--wheel", str(self.wheel), *extra])
        self.assertEqual(rc, 0)
        return out_dir

    def launch(self, plan: str, now: int = NOW, *extra: str) -> list[dict[str, Any]]:
        out_dir = self.tmp / "launch"
        rc = rd().main(["--out", str(out_dir), "--launch", plan, "--cb-node", CB_NODE, "--now", str(now),
                        "--plans-dir", str(plans_dir_of(plan)), *extra])
        self.assertEqual(rc, 0)
        (path,) = sorted(out_dir.glob(f"10-job-{plan}-*.yaml"))
        return load(path)


class PlatformEnvAndPlanExpansion(unittest.TestCase):
    def test_load_platform_env_reads_key_value_lines_and_skips_comments(self) -> None:
        p = Path(tempfile.mkdtemp(prefix="q38-env-")) / "x.env"
        self.addCleanup(shutil.rmtree, p.parent, True)
        p.write_text("# c\nA=1\n\nB=x=y\n")
        self.assertEqual(rd().load_platform_env(p), {"A": "1", "B": "x=y"})

    def test_expand_plan_replaces_only_placeholders(self) -> None:
        text = "# keep $HOME and ${NOT_A_KEY_TOKEN_LOWER}x\nw2 trace=s3://${RESULTS_BUCKET}/t\n"
        with self.assertRaises((KeyError, ValueError)):
            rd().expand_plan(text, {"RESULTS_BUCKET": "b"})
        ok = rd().expand_plan("w2 trace=s3://${RESULTS_BUCKET}/t $X\n", {"RESULTS_BUCKET": "b"})
        self.assertEqual(ok, "w2 trace=s3://b/t $X\n")

    def test_an_unknown_placeholder_is_an_error_not_a_hole(self) -> None:
        with self.assertRaises((KeyError, ValueError)):
            rd().expand_plan("trace=s3://${NO_SUCH_KEY}/t\n", {"RESULTS_BUCKET": "b"})

    def test_every_shipped_plan_expands_to_exactly_the_pinned_plan(self) -> None:
        env = rd().load_platform_env(PLATFORM_ENV)
        got = {f"{n}.txt": hashlib.sha256(rd().expand_plan(plan_path(n).read_text(), env).encode()).hexdigest()
               for n in PLAN_NAMES}
        self.assertEqual(got, GOLDEN_FILES["expanded_plan_sha256"])

    def test_the_w3_plans_live_in_w3_16k_and_the_w2_plans_in_w2_long_context(self) -> None:
        self.assertEqual(sorted(p.name for p in PLANS_DIR.glob("*")), sorted(f"{n}.txt" for n in W3_PLAN_NAMES))
        self.assertEqual(sorted(p.name for p in W2_PLANS_DIR.glob("*")), sorted(f"{n}.txt" for n in W2_PLAN_NAMES))
        self.assertEqual(W2_PLANS_DIR.name, "w2-long-context")

    def test_plan_w3_r50_is_dropped_everywhere(self) -> None:
        self.assertEqual(list((PLANS_DIR.parent).rglob("*r50*")), [])
        self.assertNotIn("plan-w3-r50.txt", GOLDEN_FILES["expanded_plan_sha256"])


class DgdsComeFromTheRepoRenders(unittest.TestCase):
    def test_annotated_dgds_are_the_committed_render_plus_the_workers_sha(self) -> None:
        got = rd().annotated_dgds(RENDERED_DGDS)
        self.assertEqual(sorted(got), [f"{c}.yaml" for c in CELLS])
        for cell in CELLS:
            with self.subTest(cell):
                src = yaml.safe_load((RENDERED_DGDS / f"{cell}.yaml").read_text())
                doc = yaml.safe_load(got[f"{cell}.yaml"])
                sha = hashlib.sha256(json.dumps(src["spec"]["components"][1:], sort_keys=True).encode()).hexdigest()
                self.assertEqual(doc["metadata"]["annotations"]["q38-matrix/workers-sha256"], sha)
                del doc["metadata"]["annotations"]["q38-matrix/workers-sha256"]
                if not doc["metadata"]["annotations"]:
                    del doc["metadata"]["annotations"]
                src["metadata"].pop("annotations", None)
                doc["metadata"].pop("annotations", None)
                self.assertEqual(doc, src, "the only change to a committed render is the annotation")
                # the exact pattern the driver parses (rendered_workers_sha) and the fake cluster mirrors
                self.assertRegex(got[f"{cell}.yaml"], r'q38-matrix/workers-sha256: "?([0-9a-f]{64})"?')

    def test_the_workers_sha_values_are_the_ones_of_the_placeholder_config(self) -> None:
        got = {c: re.search(r'workers-sha256: "?([0-9a-f]{64})', t).group(1)[:8]  # type: ignore[union-attr]
               for c, t in rd().annotated_dgds(RENDERED_DGDS).items()}
        self.assertEqual(got, {"S0.yaml": "0792289a", "S1.yaml": "0792289a", "S2.yaml": "32d4d57c", "S4.yaml": "32d4d57c",
                               "S3.yaml": "d02cdaa6", "S5.yaml": "d02cdaa6", "S6.yaml": "8ade8704", "S7.yaml": "8ade8704"})

    def test_the_two_arms_of_a_topology_share_the_workers_sha_and_topologies_differ(self) -> None:
        sha = {c: re.search(r'workers-sha256: "?([0-9a-f]{64})', t).group(1)  # type: ignore[union-attr]
               for c, t in rd().annotated_dgds(RENDERED_DGDS).items()}
        for a, b in (("S0", "S1"), ("S2", "S4"), ("S3", "S5"), ("S6", "S7")):
            self.assertEqual(sha[f"{a}.yaml"], sha[f"{b}.yaml"])
        self.assertEqual(len(set(sha.values())), 4)


class BaseObjects(Base):
    def test_base_objects_equal_the_golden(self) -> None:
        docs = load(self.render() / "00-base.yaml")
        self.assertEqual(canon([reduce(d) for d in docs]), canon(golden("base")))

    def test_driver_configmap_carries_the_driver_source_and_the_wheel(self) -> None:
        cms = {d["metadata"]["name"]: d for d in load(self.render() / "00-base.yaml") if d["kind"] == "ConfigMap"}
        cm = cms["q38-matrix-driver"]
        self.assertEqual(cm["data"]["matrix_driver.py"], (TOOL / "matrix_driver.py").read_text())
        sha = hashlib.sha256(self.wheel_bytes).hexdigest()
        self.assertEqual(cm["data"]["wheel.sha256"], f"{sha}  {self.wheel_name}\n")
        self.assertEqual(base64.b64decode(cm["binaryData"][self.wheel_name]), self.wheel_bytes)

    def test_dgd_configmap_holds_the_eight_annotated_repo_renders(self) -> None:
        cms = {d["metadata"]["name"]: d for d in load(self.render() / "00-base.yaml") if d["kind"] == "ConfigMap"}
        self.assertEqual(cms["q38-matrix-dgds"]["data"], rd().annotated_dgds(RENDERED_DGDS))

    def test_plans_configmap_holds_the_six_expanded_w3_plans_by_default(self) -> None:
        cms = {d["metadata"]["name"]: d for d in load(self.render() / "00-base.yaml") if d["kind"] == "ConfigMap"}
        data = cms["q38-matrix-plans"]["data"]
        want = {f"{n}.txt": GOLDEN_FILES["expanded_plan_sha256"][f"{n}.txt"] for n in W3_PLAN_NAMES}
        self.assertEqual({k: hashlib.sha256(v.encode()).hexdigest() for k, v in data.items()}, want)

    def test_plans_dir_selects_the_plans_of_the_configmap(self) -> None:
        out = self.render("--plans-dir", str(W2_PLANS_DIR), out="w2")
        cms = {d["metadata"]["name"]: d for d in load(out / "00-base.yaml") if d["kind"] == "ConfigMap"}
        want = {f"{n}.txt": GOLDEN_FILES["expanded_plan_sha256"][f"{n}.txt"] for n in W2_PLAN_NAMES}
        self.assertEqual({k: hashlib.sha256(v.encode()).hexdigest() for k, v in cms["q38-matrix-plans"]["data"].items()}, want)

    def test_a_missing_plans_dir_is_refused(self) -> None:
        with self.assertRaises(SystemExit):
            rd().main(["--out", str(self.tmp / "x"), "--wheel", str(self.wheel), "--plans-dir", str(self.tmp / "nope")])

    def test_take_scripts_configmap_is_the_vendored_capture_and_compose(self) -> None:
        cms = {d["metadata"]["name"]: d for d in load(self.render() / "00-base.yaml") if d["kind"] == "ConfigMap"}
        data = cms["q38-take-capture"]["data"]
        self.assertEqual(data["capture.py"], (TOOL / "takes" / "capture.py").read_text())
        self.assertEqual(data["compose.sh"], (TOOL / "takes" / "compose.sh").read_text())
        self.assertEqual(hashlib.sha256(data["capture.py"].encode()).hexdigest(), GOLDEN_FILES["verbatim_file_sha256"]["takes/capture.py"])
        self.assertEqual(hashlib.sha256(data["compose.sh"].encode()).hexdigest(), GOLDEN_FILES["verbatim_file_sha256"]["takes/compose.sh"])

    def test_rbac_lets_the_driver_create_jobs_only_in_bench(self) -> None:
        docs = load(self.render() / "00-base.yaml")
        for d in docs:
            if d["kind"] != "Role":
                continue
            creates_jobs = any("jobs" in r.get("resources", []) and "create" in r["verbs"] for r in d["rules"])
            self.assertEqual(creates_jobs, d["metadata"]["namespace"] == "bench", d["metadata"])
        for d in docs:
            if d["kind"] == "ClusterRole":
                self.assertTrue(all(set(r["verbs"]) <= {"get", "list"} for r in d["rules"]), "cluster-wide access is read-only")

    def test_compose_script_is_shellcheck_clean(self) -> None:
        if shutil.which("shellcheck") is None:
            self.skipTest("shellcheck not installed")
        p = run_argv(["shellcheck", "-x", "--shell=bash", str(TOOL / "takes" / "compose.sh")],
                     capture_output=True, text=True, check=False)
        self.assertEqual(p.returncode, 0, p.stdout)

    def test_the_wheel_is_required(self) -> None:
        with self.assertRaises(SystemExit):
            rd().main(["--out", str(self.tmp / "nowheel")])


class PreflightAndWatchdog(Base):
    def test_take_preflight_objects_equal_the_golden_for_the_w2_plans(self) -> None:
        docs = load(self.render("--plans-dir", str(W2_PLANS_DIR), out="w2") / "05-take-preflight.yaml")
        self.assertEqual(canon([reduce(d) for d in docs]), canon(golden("preflight")))

    def test_the_default_take_preflight_is_only_the_take_scripts_because_no_w3_plan_takes(self) -> None:
        docs = load(self.render() / "05-take-preflight.yaml")
        self.assertEqual([(d["kind"], d["metadata"]["name"]) for d in docs], [("ConfigMap", "q38-take-capture")])

    def test_watchdog_objects_equal_the_golden(self) -> None:
        docs = load(self.render() / "20-watchdog.yaml")
        self.assertEqual(canon([reduce(d) for d in docs]), canon(golden("watchdog")))

    def test_watchdog_configmap_is_the_unchanged_watchdog_source(self) -> None:
        cm = next(d for d in load(self.render() / "20-watchdog.yaml") if d["kind"] == "ConfigMap")
        self.assertEqual(cm["data"]["watchdog.py"], (TOOL / "watchdog.py").read_text())
        self.assertEqual(hashlib.sha256(cm["data"]["watchdog.py"].encode()).hexdigest(),
                         GOLDEN_FILES["verbatim_file_sha256"]["driver/watchdog.py"])

    def test_windows_and_schedule_are_parameters_with_the_recorded_defaults(self) -> None:
        out = self.render("--watchdog-windows", W3LATE_WINDOWS, "--watchdog-schedule", W3LATE_SCHEDULE)
        docs = load(out / "20-watchdog.yaml")
        self.assertEqual(canon([reduce(d) for d in docs]), canon(golden("watchdog_w3late")))

    def test_legacy_pair_follows_the_first_window(self) -> None:
        out = self.render("--watchdog-windows", "2027-03-01T04:30:00Z/2027-03-01T05:00:00Z",
                          "--watchdog-schedule", "*/5 4 1 3 *")
        cj = next(d for d in load(out / "20-watchdog.yaml") if d["kind"] == "CronJob")
        env = {e["name"]: e["value"] for e in cj["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"][0]["env"]}
        self.assertEqual(env["WINDOWS"], "2027-03-01T04:30:00Z/2027-03-01T05:00:00Z")
        self.assertEqual((env["HARD_STOP"], env["WINDOW_END"]), ("2027-03-01T04:30:00Z", "2027-03-01T05:00:00Z"))
        self.assertEqual(cj["spec"]["schedule"], "*/5 4 1 3 *")


class LaunchJobs(Base):
    def test_job_for_plan_e7_equals_the_golden(self) -> None:
        self.assertEqual(self.launch("plan-e7"), golden("job_e7"))

    def test_job_for_a_w3_plan_equals_the_golden(self) -> None:
        self.assertEqual(self.launch("plan-w3-r32-cells"), golden("job_w3_r32_cells"))

    def test_every_shipped_plan_gets_a_job_that_ends_at_its_own_hard_stop(self) -> None:
        for name in PLAN_NAMES:
            with self.subTest(name):
                from support import shipped_plan_text

                hs = re.search(r"(?m)^hard_stop\s+(\S+)", shipped_plan_text(name)).group(1)  # type: ignore[union-attr]
                (job,) = self.launch(name)
                stop = calendar.timegm(time.strptime(hs, "%Y-%m-%dT%H:%M:%SZ"))
                self.assertEqual(job["spec"]["activeDeadlineSeconds"], stop - NOW)
                self.assertEqual(job["metadata"]["name"], f"q38-matrix-{name.removeprefix('plan-')}")
                env = {e["name"]: e.get("value") for e in job["spec"]["template"]["spec"]["containers"][0]["env"]}
                self.assertEqual(env["HARD_STOP"], hs)
                self.assertEqual(env["CB_NODE"], CB_NODE)
                self.assertEqual(job["spec"]["template"]["spec"]["containers"][0]["args"],
                                 ["--plan", f"/etc/q38/plans/{name}.txt"])

    def test_a_plan_whose_hard_stop_has_passed_is_refused(self) -> None:
        with self.assertRaises(SystemExit) as cm:
            self.launch("plan-e3", now=calendar.timegm((2026, 10, 24, 5, 0, 0)))
        self.assertIn("passed", str(cm.exception))

    def test_an_unknown_plan_is_refused(self) -> None:
        with self.assertRaises(SystemExit):
            self.launch("plan-nope")

    def test_a_w2_plan_is_not_found_in_the_default_plans_dir(self) -> None:
        with self.assertRaises(SystemExit):
            rd().main(["--out", str(self.tmp / "l"), "--launch", "plan-e7", "--cb-node", CB_NODE, "--now", str(NOW)])

    def test_the_dropped_r50_plan_cannot_be_launched(self) -> None:
        with self.assertRaises(SystemExit):
            rd().main(["--out", str(self.tmp / "l"), "--launch", "plan-w3-r50", "--cb-node", CB_NODE, "--now", str(NOW)])

    def test_launch_needs_the_capacity_block_node(self) -> None:
        with self.assertRaises(SystemExit):
            rd().main(["--out", str(self.tmp / "l"), "--launch", "plan-e7", "--plans-dir", str(W2_PLANS_DIR), "--now", str(NOW)])


class RelativeHardStop(Base):
    """``--launch PLAN --hard-stop-in DURATION``: the hard stop is ``ceil(--now) + DURATION``, resolved once.

    DURATION is ``<positive integer><s|m|h>``. Four places must agree on the instant (the launch refuses to start otherwise,
    matrix_driver.check_watchdog / the plan lock): the plan's ``hard_stop`` line, the Job (env HARD_STOP, annotation
    q38-matrix/hard-stop, activeDeadlineSeconds = DURATION) and a watchdog window opening exactly at it. Besides the Job
    (``10-job-<plan>-<ts>.yaml``, as without the flag) the launch writes ``08-plans-<plan>-<ts>.yaml`` (ConfigMap
    q38-matrix-plans with ONE key, ``<plan>.txt``: the expanded plan with only its ``hard_stop`` line replaced) and
    ``20-watchdog.yaml`` (the watchdog ConfigMap + CronJob, schedule ``*/5 * * * *``, one window
    ``<hard stop>/<hard stop + 30 min>``). Without the flag only the Job is written, byte-identical to before.
    """

    PLAN = "plan-w3-r25"
    LATE = calendar.timegm((2027, 3, 1, 10, 0, 0))  # long after the plan's absolute hard stop (2026-10-24)

    def go(self, *extra: str, plan: str | None = None, now: float = LATE, out: str = "rel") -> Path:
        plan = plan or self.PLAN
        out_dir = self.tmp / out
        rc = rd().main(["--out", str(out_dir), "--launch", plan, "--cb-node", CB_NODE, "--now", str(now),
                        "--plans-dir", str(plans_dir_of(plan)), *extra])
        self.assertEqual(rc, 0)
        return out_dir

    @staticmethod
    def one(out: Path, pattern: str) -> Path:
        (path,) = sorted(out.glob(pattern))
        return path

    @staticmethod
    def iso(epoch: int) -> str:
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))

    def test_without_the_flag_a_passed_hard_stop_is_still_refused_and_only_the_job_is_written(self) -> None:
        with self.assertRaises(SystemExit) as cm:
            self.go()
        self.assertIn("passed", str(cm.exception))
        out = self.go(now=NOW, out="abs")
        self.assertEqual([p.name for p in out.glob("*")], [next(out.glob("10-job-*.yaml")).name])

    def test_the_job_ends_at_now_plus_the_duration(self) -> None:
        out = self.go("--hard-stop-in", "8h")
        (job,) = load(self.one(out, f"10-job-{self.PLAN}-*.yaml"))
        hs = self.iso(self.LATE + 8 * 3600)
        self.assertEqual(job["spec"]["activeDeadlineSeconds"], 8 * 3600)
        env = {e["name"]: e.get("value") for e in job["spec"]["template"]["spec"]["containers"][0]["env"]}
        self.assertEqual(env["HARD_STOP"], hs)
        self.assertEqual(job["metadata"]["annotations"]["q38-matrix/hard-stop"], hs)
        self.assertEqual(job["metadata"]["name"], "q38-matrix-w3-r25")
        self.assertEqual(env["CB_NODE"], CB_NODE)

    def test_durations(self) -> None:
        for text, seconds in (("8h", 28800), ("1h", 3600), ("90m", 5400), ("3600s", 3600), ("1s", 1), ("12h", 43200)):
            with self.subTest(text):
                out = self.go("--hard-stop-in", text, out=f"d-{text}")
                (job,) = load(self.one(out, "10-job-*.yaml"))
                self.assertEqual(job["spec"]["activeDeadlineSeconds"], seconds)

    def test_a_fractional_now_rounds_up_so_the_deadline_is_exactly_the_duration(self) -> None:
        out = self.go("--hard-stop-in", "8h", now=self.LATE + 0.4)
        (job,) = load(self.one(out, "10-job-*.yaml"))
        self.assertEqual(job["spec"]["activeDeadlineSeconds"], 28800)
        env = {e["name"]: e.get("value") for e in job["spec"]["template"]["spec"]["containers"][0]["env"]}
        self.assertEqual(env["HARD_STOP"], self.iso(self.LATE + 1 + 28800))

    def refused(self, argv: list[str]) -> str:
        """Run main(argv) expecting argparse's own refusal (exit code 2); returns what it wrote to stderr."""
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as cm:
            rd().main(argv)
        self.assertEqual(cm.exception.code, 2)
        return err.getvalue()

    def test_invalid_durations_are_refused_before_anything_is_written(self) -> None:
        for bad in ("", "0h", "0s", "0m", "-1h", "8", "8d", "1.5h", "h", "8H", "8 h", "abc", "+8h", "8hh"):
            out = self.tmp / f"bad-{bad or 'empty'}"
            with self.subTest(bad):
                msg = self.refused(["--out", str(out), "--launch", self.PLAN, "--cb-node", CB_NODE, "--now", str(self.LATE),
                                    "--plans-dir", str(plans_dir_of(self.PLAN)), f"--hard-stop-in={bad}"])
                self.assertIn("--hard-stop-in:", msg)
                self.assertIn("expected a positive whole number with s, m or h", msg)
                self.assertFalse(out.exists(), "nothing is written for a refused duration")

    def test_the_flag_needs_launch(self) -> None:
        out = self.tmp / "x"
        msg = self.refused(["--out", str(out), "--wheel", str(self.wheel), "--hard-stop-in", "8h"])
        self.assertIn("--hard-stop-in needs --launch", msg)
        self.assertFalse(out.exists())

    def test_a_retimed_plan_e3_breaks_the_slo_pin_of_plan_e4_to_e7(self) -> None:
        """Documented limit: plan-e4..e7 pin the sha256 of the SHIPPED plan-e3 (matrix_driver.py compares it with the
        lock's plan_sha256), so a plan-e3 launched with --hard-stop-in writes a lock they refuse. The README says so."""
        from support import W2_PLAN_NAMES as w2
        from support import shipped_plan_text

        env = values()
        pins = set()
        for name in w2:
            if name == "plan-e3":
                continue
            ref = rd().load_driver().parse_plan(rd().expand_plan(shipped_plan_text(name), env)).slo_ref
            self.assertIsNotNone(ref, name)
            pins.add(ref["e3_plan_sha256"])
        self.assertEqual(len(pins), 1, "e4..e7 pin one e3 hash")
        (pin,) = pins
        shipped = hashlib.sha256(rd().expand_plan(shipped_plan_text("plan-e3"), env).encode()).hexdigest()
        self.assertEqual(shipped, pin, "launched as shipped, plan-e3 matches the pin")
        out = self.go("--hard-stop-in", "8h", plan="plan-e3")
        (cm,) = load(self.one(out, "08-plans-plan-e3-*.yaml"))
        retimed = hashlib.sha256(cm["data"]["plan-e3.txt"].encode()).hexdigest()
        self.assertNotEqual(retimed, pin, "a re-timed plan-e3 no longer matches what e4..e7 pin")
        readme = " ".join((TOOL / "README.md").read_text().split())
        for phrase in ("pin the sha256 of the shipped `plan-e3` (`e3_plan_sha256`)",
                       "`--hard-stop-in` changes the sha256 of the plan it re-times",
                       "a lock written by a re-timed `plan-e3` is refused by them"):
            self.assertIn(phrase, readme)

    def test_the_plan_configmap_is_the_plan_with_only_the_hard_stop_line_replaced(self) -> None:
        out = self.go("--hard-stop-in", "8h")
        (cm,) = load(self.one(out, f"08-plans-{self.PLAN}-*.yaml"))
        self.assertEqual((cm["kind"], cm["metadata"]["name"], cm["metadata"]["namespace"]), ("ConfigMap", "q38-matrix-plans", "bench"))
        self.assertEqual(sorted(cm["data"]), [f"{self.PLAN}.txt"])
        from support import shipped_plan_text

        original, new = shipped_plan_text(self.PLAN), cm["data"][f"{self.PLAN}.txt"]
        hs = self.iso(self.LATE + 8 * 3600)
        diff = [(a, b) for a, b in zip(original.splitlines(), new.splitlines(), strict=True) if a != b]
        self.assertEqual(len(original.splitlines()), len(new.splitlines()))
        self.assertEqual(diff, [("hard_stop 2026-10-24T09:00:00Z", f"hard_stop {hs}")])
        self.assertEqual(rd().load_driver().parse_plan(new).hard_stop, hs)
        self.assertNotEqual(hashlib.sha256(original.encode()).hexdigest(), hashlib.sha256(new.encode()).hexdigest())

    def test_the_watchdog_opens_a_window_exactly_at_the_hard_stop(self) -> None:
        out = self.go("--hard-stop-in", "8h")
        cm, cj = load(out / "20-watchdog.yaml")
        self.assertEqual((cm["kind"], cj["kind"]), ("ConfigMap", "CronJob"))
        self.assertEqual(cm["data"]["watchdog.py"], (TOOL / "watchdog.py").read_text())
        self.assertEqual(cj["spec"]["schedule"], "*/5 * * * *")
        env = {e["name"]: e["value"] for e in cj["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"][0]["env"]}
        hs = self.LATE + 8 * 3600
        self.assertEqual(env["WINDOWS"], f"{self.iso(hs)}/{self.iso(hs + 1800)}")
        self.assertEqual((env["HARD_STOP"], env["WINDOW_END"]), (self.iso(hs), self.iso(hs + 1800)))
        md = rd().load_driver()
        windows = md.watchdog_windows(env)
        self.assertTrue(any(md.iso_epoch(w[0]) == hs for w in windows), windows)

    def test_a_w2_plan_launches_with_a_relative_hard_stop_too(self) -> None:
        out = self.go("--hard-stop-in", "2h", plan="plan-e7")
        (job,) = load(self.one(out, "10-job-plan-e7-*.yaml"))
        self.assertEqual(job["spec"]["activeDeadlineSeconds"], 7200)
        (cm,) = load(self.one(out, "08-plans-plan-e7-*.yaml"))
        self.assertEqual(rd().load_driver().parse_plan(cm["data"]["plan-e7.txt"]).hard_stop, self.iso(self.LATE + 7200))

    def test_rendering_twice_gives_identical_files(self) -> None:
        a, b = self.go("--hard-stop-in", "8h", out="a"), self.go("--hard-stop-in", "8h", out="b")
        self.assertEqual(sorted(p.name for p in a.glob("*")), sorted(p.name for p in b.glob("*")))
        self.assertEqual(len(list(a.glob("*"))), 3)
        for p in a.glob("*"):
            self.assertEqual(p.read_bytes(), (b / p.name).read_bytes(), p.name)

    def test_the_relative_job_is_the_absolute_job_with_the_new_instant(self) -> None:
        """Same plan, same Job: launching at NOW with the absolute hard stop as a duration reproduces the absolute render."""
        absolute = self.launch(self.PLAN)
        (rel,) = load(self.one(self.go("--hard-stop-in", f"{39600}s", now=NOW, out="same"), "10-job-*.yaml"))
        self.assertEqual(rel, absolute[0])


class ParametrizedByPlatformEnv(Base):
    """A different platform.env (another account, region, buckets, registry) changes every value; nothing is hard-coded."""

    def alt_env(self) -> Path:
        real = _parse_env(PLATFORM_ENV)
        acct, region = real["AWS_ACCOUNT_ID"], real["AWS_REGION"]
        short = real["RESULTS_BUCKET"].rsplit("-", 1)[-1]
        lines = []
        for raw in PLATFORM_ENV.read_text().splitlines():
            lines.append(raw.replace(acct, "9" * 12).replace(region, "xx-test-1").replace(short, "xxt1")
                         .replace("dynamo-sample-", "other-proj-"))
        path = self.tmp / "alt.env"
        path.write_text("\n".join(lines) + "\n")
        return path

    def test_no_value_of_the_default_platform_env_survives_in_a_render_with_another_one(self) -> None:
        real = _parse_env(PLATFORM_ENV)
        alt = self.alt_env()
        out = self.render("--env", str(alt))
        launch = self.tmp / "launch"
        self.assertEqual(rd().main(["--out", str(launch), "--launch", "plan-e7", "--cb-node", CB_NODE, "--now", str(NOW),
                                    "--plans-dir", str(W2_PLANS_DIR), "--env", str(alt)]), 0)
        needles = (real["AWS_ACCOUNT_ID"], real["AWS_REGION"], real["RESULTS_BUCKET"], real["ECR_REGISTRY"], "aps" + "1")
        files = [*out.glob("*.yaml"), *launch.glob("*.yaml")]
        self.assertGreaterEqual(len(files), 4)
        for f in files:
            docs = [d for d in load(f) if not (d["kind"] == "ConfigMap" and d["metadata"]["name"] == "q38-matrix-dgds")]
            text = yaml.safe_dump(docs)  # the DGD ConfigMap is the committed render (its image pin is by design)
            for needle in needles:
                with self.subTest(file=f.name, needle=needle[:12]):
                    self.assertNotIn(needle, text)
        alt_env = _parse_env(alt)
        job = load(next(launch.glob("10-job-*.yaml")))[0]
        env = {e["name"]: e.get("value") for e in job["spec"]["template"]["spec"]["containers"][0]["env"]}
        self.assertEqual((env["RESULTS_BUCKET"], env["AWS_REGION"]), (alt_env["RESULTS_BUCKET"], alt_env["AWS_REGION"]))
        self.assertEqual(env["L3_CLEANUP_IMAGE"], alt_env["RUNTIME_IMAGE"])
        plans = next(d for d in load(out / "00-base.yaml") if d["kind"] == "ConfigMap" and d["metadata"]["name"] == "q38-matrix-plans")
        for text in plans["data"].values():
            self.assertIn(f"s3://{alt_env['RESULTS_BUCKET']}/", text)
            self.assertNotIn(real["RESULTS_BUCKET"], text)

    def test_default_render_uses_the_platform_env_values(self) -> None:
        env = _parse_env(PLATFORM_ENV)
        job = self.launch("plan-e7")[0]
        penv = {e["name"]: e.get("value") for e in job["spec"]["template"]["spec"]["containers"][0]["env"]}
        self.assertEqual((penv["RESULTS_BUCKET"], penv["AWS_REGION"]), (env["RESULTS_BUCKET"], env["AWS_REGION"]))
        self.assertEqual(penv["L3_CLEANUP_IMAGE"], env["RUNTIME_IMAGE"])


class CommandLine(Base):
    def test_runs_as_a_script_without_the_repo_on_pythonpath(self) -> None:
        out = self.tmp / "cli"
        p = run_argv(["python3", str(TOOL / "render_driver.py"), "--out", str(out), "--wheel", str(self.wheel)],
                     capture_output=True, text=True, check=False, cwd=self.tmp)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(sorted(f.name for f in out.glob("*.yaml")), ["00-base.yaml", "05-take-preflight.yaml", "20-watchdog.yaml"])

    def test_rendering_twice_gives_identical_files(self) -> None:
        a, b = self.render(out="a"), self.render(out="b")
        for name in ("00-base.yaml", "05-take-preflight.yaml", "20-watchdog.yaml"):
            self.assertEqual((a / name).read_bytes(), (b / name).read_bytes(), name)


if __name__ == "__main__":
    unittest.main()
