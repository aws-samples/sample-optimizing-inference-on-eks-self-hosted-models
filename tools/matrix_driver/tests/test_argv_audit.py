# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Process and URL audit of the driver, the watchdog and the take capture script (static analysis: Semgrep
dangerous-subprocess-use-audit, Bandit B603 and B310), written before the fix (RED).

Contract:
  matrix_driver: one ``subprocess.run`` site (``run_argv``: ``sh``, the tokenizer probe and ``demo_bench`` go through
      it) and one ``subprocess.Popen`` site (``start_aiperf``), both on ``resolve_argv(cmd)``: the executable resolved
      with ``shutil.which`` (FileNotFoundError naming it when absent, nothing started), the argv a list, never a shell.
      Every value that reaches an argv from outside the plan is checked before use: ``main`` refuses (exit 2, before
      any cluster call) a RESULTS_BUCKET / AWS_REGION / CB_NODE / JOB_NAME that is not a plain name and a WORK /
      DGD_DIR / DRIVER_DIR that is not absolute; ``S3`` refuses a bucket or region that is not a plain name
      (ConfigError); ``pods_of`` refuses a pod name from the API that is not a DNS-1123 name (StepError).
  watchdog: one ``subprocess.run`` site, every argv made of module constants only (comment-only audit, no change).
  capture: ``http_url`` admits http(s) URLs with a host only; ``grafana_json`` checks its URL before opening it and
      does not follow a redirect to another scheme; ``main`` refuses (exit 1, before any request or process) a
      GRAFANA_URL that is not http(s), a RESULTS_BUCKET / AWS_REGION / TAKE_ID / S3_PREFIX that is not a plain name or
      key prefix and a relative OUT_DIR; one ``subprocess.run`` site, the upload argv unchanged.
Injection-shaped values ('--kubeconfig=/x', '; rm', '$(x)', a newline) are refused, or travel as one inert argv element.
"""

from __future__ import annotations

import ast
import contextlib
import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import ModuleType
from typing import Any, ClassVar
from unittest import mock

import fake_cluster as fc
import matrix_driver as md
import watchdog as wd
from support import TOOL, shipped_plan_text

INJECTIONS = ["--kubeconfig=/x", "; rm -rf /tmp/x", "$(touch hit)", "`touch hit`", "a\nb", "x | y", "-n"]
FAR_FUTURE = "2099-01-01T00:00:00Z"


def subprocess_calls(path: Path) -> dict[str, int]:
    """Number of ``subprocess.<name>(...)`` calls in a module's source, by name."""
    out: dict[str, int] = {}
    for node in ast.walk(ast.parse(path.read_text())):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name) and node.func.value.id == "subprocess"):
            out[node.func.attr] = out.get(node.func.attr, 0) + 1
    return out


def load_capture() -> ModuleType:
    """takes/capture.py as a module (it is mounted alone in the capture Job, not importable from the driver dir)."""
    spec = importlib.util.spec_from_file_location("take_capture", TOOL / "takes" / "capture.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    old, sys.dont_write_bytecode = sys.dont_write_bytecode, True  # no takes/__pycache__ (the takes/* scans read files)
    try:
        spec.loader.exec_module(mod)
    finally:
        sys.dont_write_bytecode = old
    return mod


class _Recorder:
    """Stands in for subprocess.run / Popen: records argv and keyword arguments; answers ``rcs`` (then 0), ``out``."""

    def __init__(self, rcs: list[int] | None = None, out: str = "") -> None:
        self.calls: list[tuple[list[str], dict[str, Any]]] = []
        self.rcs = list(rcs or [])
        self.out = out

    def __call__(self, argv: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append((list(argv), kw))
        # A result record only (the argv is in calls): its args is a placeholder, not a command.
        return subprocess.CompletedProcess("fake", self.rcs.pop(0) if self.rcs else 0, self.out, "")


class _Redirects(BaseHTTPRequestHandler):
    """``/to?<url>`` answers 302 to ``<url>``; anything else 200 ``{}``."""

    def do_GET(self) -> None:
        if self.path.startswith("/to?"):
            self.send_response(302)
            self.send_header("Location", self.path[len("/to?"):])
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, *args: object) -> None:
        """Silence the request log."""


@contextlib.contextmanager
def local_server() -> Iterator[str]:
    """A 127.0.0.1 HTTP server (no network); yields its base URL."""
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Redirects)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}"
    finally:
        srv.shutdown()
        srv.server_close()


# ================================================================ matrix_driver
class DriverProcessSites(unittest.TestCase):
    def test_one_run_site_and_one_popen_site(self) -> None:
        calls = subprocess_calls(TOOL / "matrix_driver.py")
        self.assertEqual(calls.get("run"), 1, calls)
        self.assertEqual(calls.get("Popen"), 1, calls)

    def test_resolve_argv_uses_the_path_lookup(self) -> None:
        with mock.patch.object(md.shutil, "which", return_value="/opt/test/bin/kubectl") as which:
            self.assertEqual(md.resolve_argv(["kubectl", "get", "pods"]), ["/opt/test/bin/kubectl", "get", "pods"])
        which.assert_called_once_with("kubectl")

    def test_a_missing_executable_is_a_clear_error_and_nothing_runs(self) -> None:
        with (mock.patch.object(md.shutil, "which", return_value=None),
              mock.patch.object(md.subprocess, "run", side_effect=AssertionError("a process was started")),
              self.assertRaises(FileNotFoundError) as cm):
            md.sh(["kubectl", "version"])
        self.assertIn("kubectl is not on PATH", str(cm.exception))

    def test_sh_runs_the_resolved_executable_with_the_same_arguments(self) -> None:
        rec = _Recorder()
        with mock.patch.object(md.shutil, "which", return_value="/opt/test/bin/aws"), \
                mock.patch.object(md.subprocess, "run", rec):
            md.sh(["aws", "s3", "ls", "s3://test-bucket/"], timeout=120, stdin="in")
        argv, kw = rec.calls[0]
        self.assertEqual(argv, ["/opt/test/bin/aws", "s3", "ls", "s3://test-bucket/"])
        self.assertEqual({k: kw.get(k) for k in ("input", "capture_output", "text", "timeout", "check")},
                         {"input": "in", "capture_output": True, "text": True, "timeout": 120, "check": False})

    def test_injection_shaped_values_stay_single_inert_argv_elements(self) -> None:
        with tempfile.TemporaryDirectory(prefix="q38-argv-") as tmp, contextlib.chdir(tmp):
            p = md.sh([sys.executable, "-I", "-c", "import json, sys; print(json.dumps(sys.argv[1:]))", *INJECTIONS],
                      timeout=60)
            self.assertEqual(json.loads(p.stdout), INJECTIONS)
            self.assertFalse(Path("hit").exists(), "a value was run by a shell")

    def test_demo_bench_goes_through_the_audited_site(self) -> None:
        d = md.Driver.__new__(md.Driver)
        d.work = Path("/work-test")
        rec = _Recorder()
        with mock.patch.object(md.shutil, "which", return_value="/opt/test/bin/python3"), \
                mock.patch.object(md.subprocess, "run", rec):
            d.demo_bench("slo-goodput", "--seed", "0", timeout=30)
        argv, kw = rec.calls[0]
        self.assertEqual(argv, ["/opt/test/bin/python3", "-m", "demo_bench", "slo-goodput", "--seed", "0"])
        self.assertEqual((kw["timeout"], kw["cwd"], kw["env"]["PYTHONPATH"]), (30, "/work-test", "/work-test/pylib"))

    def test_start_aiperf_starts_the_resolved_executable(self) -> None:
        d = md.Driver.__new__(md.Driver)
        d.hf_hub = Path("/work-test/hf")
        rec = _Recorder()
        with mock.patch.object(md.shutil, "which", return_value="/opt/test/bin/aiperf"), \
                mock.patch.object(md.subprocess, "Popen", rec):
            d.start_aiperf(["aiperf", "profile", "--url", "http://fe.test:8000"], None, Path("/work-test/run"))
        argv, kw = rec.calls[0]
        self.assertEqual(argv, ["/opt/test/bin/aiperf", "profile", "--url", "http://fe.test:8000"])
        self.assertEqual((kw["cwd"], kw["env"]["HF_HUB_OFFLINE"]), ("/work-test/run", "1"))


class DriverArgvInputs(unittest.TestCase):
    """Values from the Job env and the Kubernetes API that reach a kubectl / aws / pip argv."""

    FULL: ClassVar[dict[str, str]] = {
        "RESULTS_BUCKET": "test-bucket", "AWS_REGION": fc.REGION, "COMPOSE_FFMPEG_IMAGE": fc.COMPOSE_FFMPEG_IMAGE,
        "CB_NODE": "node-1.test", "HARD_STOP": FAR_FUTURE, "JOB_NAME": "q38-matrix-test-abcde",
        "L3_CLEANUP_IMAGE": "img@sha256:test", "WORK": "/nonexistent-work"}

    def run_main(self, env: dict[str, str]) -> tuple[int, str, mock.MagicMock]:
        buf = io.StringIO()
        driver = mock.MagicMock()
        driver.return_value.execute.return_value = 0
        with tempfile.TemporaryDirectory(prefix="q38-argv-") as tmp, contextlib.chdir(tmp):
            plan_file = Path(tmp) / "plan.txt"
            plan = re.sub(r"(?m)^hard_stop \S+$", f"hard_stop {FAR_FUTURE}", shipped_plan_text("plan-e4"))
            plan_file.write_text(plan)
            with (mock.patch.dict(os.environ, {**env, "HOME": str(Path(tmp) / "home")}, clear=True),
                  mock.patch.object(md, "kubectl", side_effect=AssertionError("cluster touched before the refusal")),
                  mock.patch.object(md, "sh", side_effect=AssertionError("process started before the refusal")),
                  mock.patch.object(md, "Driver", driver),
                  contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf)):
                rc = md.main(["--plan", str(plan_file)])
        return rc, buf.getvalue(), driver

    def test_main_refuses_env_values_that_are_not_plain_names_or_absolute_paths(self) -> None:
        bad = [("JOB_NAME", "--kubeconfig=/x"), ("JOB_NAME", "q38; rm -rf /"), ("JOB_NAME", "q38-matrix-test\n"),
               ("CB_NODE", "$(x)"), ("CB_NODE", "node-1\n--kubeconfig=/x"), ("CB_NODE", "-node"),
               ("RESULTS_BUCKET", "--endpoint-url=http://x"), ("RESULTS_BUCKET", "Test_Bucket"),
               ("RESULTS_BUCKET", "b`id`"), ("AWS_REGION", "-x"), ("AWS_REGION", f"{fc.REGION}\n--debug"),
               ("AWS_REGION", "r; rm"), ("WORK", "-x/work"), ("WORK", "relative/work"),
               ("DRIVER_DIR", "--index-url=http://x"), ("DGD_DIR", "dgds")]
        for var, value in bad:
            with self.subTest(var=var, value=value):
                rc, out, driver = self.run_main({**self.FULL, var: value})
                self.assertEqual(rc, 2, out)
                self.assertIn(var, out)
                driver.assert_not_called()

    def test_main_accepts_the_job_values(self) -> None:
        for env in (self.FULL, {**self.FULL, "RESULTS_BUCKET": "a.b-c1", "CB_NODE": "node-1",
                                "WORK": "/work", "DGD_DIR": "/etc/q38/dgds", "DRIVER_DIR": "/etc/q38/driver"}):
            with self.subTest(env=env):
                rc, out, driver = self.run_main(env)
                self.assertEqual(rc, 0, out)
                driver.assert_called_once()

    def test_s3_refuses_a_bucket_or_region_that_is_not_a_plain_name(self) -> None:
        for bucket, region in (("--endpoint-url=http://x", fc.REGION), ("test-bucket", "x; rm"),
                               ("test-bucket\n", fc.REGION), ("test-bucket", "-r")):
            with self.subTest(bucket=bucket, region=region), self.assertRaises(md.ConfigError):
                md.S3(bucket, region)
        self.assertEqual(md.S3("test-bucket", fc.REGION).uri("k"), "s3://test-bucket/k")

    def test_pods_of_refuses_a_pod_name_that_is_not_a_dns_name(self) -> None:
        def doc(name: str) -> dict[str, Any]:
            return {"items": [{"metadata": {"name": name, "uid": "u1", "labels": {}}, "spec": {"containers": []},
                               "status": {}}]}

        for name in ("--kubeconfig=/x", "q38; rm", "../q38", "q38\nx", "Q38"):
            with self.subTest(name=name), mock.patch.object(md, "kjson", return_value=doc(name)), \
                    self.assertRaises(md.StepError):
                md.pods_of("s-agg")
        with mock.patch.object(md, "kjson", return_value=doc("q38-worker-0001")):
            self.assertEqual([p.name for p in md.pods_of("s-agg")], ["q38-worker-0001"])


# ================================================================ watchdog
class WatchdogProcessSite(unittest.TestCase):
    def test_one_run_site(self) -> None:
        self.assertEqual(subprocess_calls(TOOL / "watchdog.py"), {"run": 1})

    def test_every_argv_is_made_of_module_constants(self) -> None:
        allowed = {"kubectl", "delete", "get", "dgd", "pods", "job", "-n", "-l", "-o", "name", "json", "bench",
                   "--ignore-not-found", "--wait=false", *wd.NAMESPACES, wd.PART_OF, wd.DGD_POD, *wd.JOB_SELECTORS}
        rec = _Recorder(out='{"items": []}')
        with mock.patch.object(wd.subprocess, "run", rec):
            wd.remaining()
            wd.active_driver_hard_stops()
            for cmd in wd.delete_commands(wd.NAMESPACES):
                wd.run(cmd)
        self.assertTrue(rec.calls)
        for argv, kw in rec.calls:
            self.assertLessEqual(set(argv), allowed, argv)
            self.assertNotIn("shell", kw)


# ================================================================ takes/capture.py
class CaptureUrlAndArgv(unittest.TestCase):
    cap: ModuleType

    @classmethod
    def setUpClass(cls) -> None:
        cls.cap = load_capture()

    def test_one_run_site(self) -> None:
        self.assertEqual(subprocess_calls(TOOL / "takes" / "capture.py"), {"run": 1})

    def test_http_url_admits_http_and_https_with_a_host_only(self) -> None:
        for url in ("http://grafana.test", "https://grafana.test:3000/api/user"):
            self.assertEqual(self.cap.http_url(url), url)
        for url in ("file:///etc/passwd", "ftp://grafana.test/x", "http:///no-host", "data:text/plain,x",
                    "grafana.test", "/api/user"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                self.cap.http_url(url)

    def test_grafana_json_refuses_a_file_url_before_opening_anything(self) -> None:
        with mock.patch.object(urllib.request.OpenerDirector, "open") as opened, self.assertRaises(ValueError):
            self.cap.grafana_json("file:///etc", "/passwd", "Basic x")
        opened.assert_not_called()

    def test_grafana_json_does_not_follow_a_redirect_to_ftp(self) -> None:
        ftp = mock.Mock(side_effect=urllib.error.URLError("capture opened an ftp URL"))
        with local_server() as base, mock.patch.object(urllib.request.FTPHandler, "ftp_open", ftp):
            self.assertEqual(self.cap.grafana_json(base, "/api/user", "Basic x", timeout=5), {})
            with self.assertRaises(urllib.error.HTTPError) as cm:
                self.cap.grafana_json(base, "/to?ftp://127.0.0.1:9/x", "Basic x", timeout=5)
        self.assertEqual(cm.exception.code, 302)
        ftp.assert_not_called()

    def capture_env(self, out: Path, **over: str) -> dict[str, str]:
        env = {"TAKE_ID": "q38-e7-s0-rep1", "GRAFANA_URL": "http://grafana.test", "GRAFANA_PASSWORD": "pw-test",
               "RESULTS_BUCKET": "test-bucket", "AWS_REGION": fc.REGION, "S3_PREFIX": "takes/q38-e7",
               "FROM_MS": "1000", "TO_MS": "2000", "NAMESPACE": "s-agg", "OUT_DIR": str(out)}
        return {**env, **over}

    def run_main(self, env: dict[str, str]) -> tuple[int, str]:
        buf = io.StringIO()
        grafana = AssertionError("Grafana queried before the refusal")
        with (mock.patch.dict(os.environ, env, clear=True),
              mock.patch.object(self.cap, "data_check", side_effect=grafana),
              mock.patch.object(self.cap, "preflight", side_effect=grafana),
              mock.patch.object(self.cap, "upload", side_effect=AssertionError("upload before the refusal")),
              mock.patch.object(self.cap.subprocess, "run", side_effect=AssertionError("process before the refusal")),
              contextlib.redirect_stdout(buf)):
            rc = self.cap.main()
        return rc, buf.getvalue()

    def test_main_refuses_injection_shaped_settings(self) -> None:
        bad = [("GRAFANA_URL", "file:///etc/passwd"), ("GRAFANA_URL", "ftp://grafana.test/"),
               ("GRAFANA_URL", "grafana.test"), ("RESULTS_BUCKET", "--endpoint-url=http://x"),
               ("RESULTS_BUCKET", "b; rm"), ("AWS_REGION", "-x"), ("AWS_REGION", "r$(x)"),
               ("TAKE_ID", "--kubeconfig=/x"), ("TAKE_ID", "../x"), ("TAKE_ID", "a\nb"), ("S3_PREFIX", "-x/y"),
               ("S3_PREFIX", "takes/../x"), ("S3_PREFIX", "takes//x"), ("S3_PREFIX", "takes/$(x)"),
               ("OUT_DIR", "relative-out"), ("OUT_DIR", "-out")]
        with tempfile.TemporaryDirectory(prefix="q38-capture-") as tmp, contextlib.chdir(tmp):
            for var, value in bad:
                with self.subTest(var=var, value=value):
                    rc, out = self.run_main(self.capture_env(Path(tmp) / "out", **{var: value}))
                    self.assertEqual(rc, 1, out)
                    self.assertIn(var, out)
            self.assertEqual(sorted(os.listdir(tmp)), [], "a refused configuration wrote local files")

    def test_main_accepts_the_job_settings(self) -> None:
        buf = io.StringIO()
        with tempfile.TemporaryDirectory(prefix="q38-capture-") as tmp:
            env = self.capture_env(Path(tmp) / "out")
            with (mock.patch.dict(os.environ, env, clear=True),
                  mock.patch.object(self.cap, "data_check", return_value={"ok": False, "reason": "offline test"}),
                  mock.patch.object(self.cap, "upload", return_value=True) as up,
                  contextlib.redirect_stdout(buf)):
                rc = self.cap.main()
        self.assertEqual(rc, 4, buf.getvalue())  # past the configuration checks: the (faked) data check failed
        self.assertEqual(up.call_args.args[1:], ("test-bucket", "takes/q38-e7/q38-e7-s0-rep1", fc.REGION))

    def test_upload_argv_is_unchanged_and_runs_the_resolved_aws(self) -> None:
        rec = _Recorder(rcs=[1, 0])  # head-object: absent, then cp: ok
        with tempfile.TemporaryDirectory(prefix="q38-capture-") as tmp:
            path = Path(tmp) / "q38-e7-s0-rep1-capture.json"
            path.write_text("{}")
            with mock.patch.object(self.cap.shutil, "which", return_value="/opt/test/bin/aws"), \
                    mock.patch.object(self.cap.subprocess, "run", rec), contextlib.redirect_stdout(io.StringIO()):
                self.assertTrue(self.cap.upload([path], "test-bucket", "takes/q38-e7/q38-e7-s0-rep1", fc.REGION))
        key = "takes/q38-e7/q38-e7-s0-rep1/q38-e7-s0-rep1-capture.json"
        self.assertEqual(rec.calls[0][0], ["/opt/test/bin/aws", "s3api", "head-object", "--bucket", "test-bucket",
                                           "--key", key, "--region", fc.REGION])
        self.assertEqual((rec.calls[0][1].get("check"), rec.calls[0][1].get("capture_output")), (False, True))
        self.assertEqual(rec.calls[1][0], ["/opt/test/bin/aws", "s3", "cp", str(path), f"s3://test-bucket/{key}",
                                           "--region", fc.REGION, "--only-show-errors"])
        # the cp is not captured: the aws CLI error output reaches the Job log
        self.assertEqual((rec.calls[1][1].get("check"), bool(rec.calls[1][1].get("capture_output"))), (False, False))


if __name__ == "__main__":
    unittest.main()
