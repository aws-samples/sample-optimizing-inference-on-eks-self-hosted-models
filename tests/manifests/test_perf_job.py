# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Contract of the benchmark Job (benchmarks/perf.yaml): it uploads its results to S3, and the trace files are named trace-r<rate>."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any

import lib_proc
import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
BENCH = REPO / "benchmarks"
TRACES = BENCH / "traces"
RATES = (5, 10, 15, 20, 25, 30, 35, 40)
UPLOAD_ENV = ("CELL", "RESULTS_BUCKET", "AWS_REGION")


@pytest.fixture(scope="module")
def docs() -> list[dict[str, Any]]:
    return [d for d in yaml.safe_load_all((BENCH / "perf.yaml").read_text()) if d]


@pytest.fixture(scope="module")
def job(docs: list[dict[str, Any]]) -> dict[str, Any]:
    return next(d for d in docs if d["kind"] == "Job")


@pytest.fixture(scope="module")
def container(job: dict[str, Any]) -> dict[str, Any]:
    return job["spec"]["template"]["spec"]["containers"][0]


@pytest.fixture(scope="module")
def script(container: dict[str, Any]) -> str:
    return container["command"][-1]


def test_job_pod_and_container_are_named_perf(job: dict[str, Any], container: dict[str, Any]) -> None:
    assert job["metadata"]["name"] == "perf"
    assert job["spec"]["template"]["metadata"]["labels"]["app"] == "perf"
    assert container["name"] == "perf"


def test_pod_runs_as_the_service_account_declared_in_the_file(docs: list[dict[str, Any]], job: dict[str, Any]) -> None:
    accounts = [d["metadata"]["name"] for d in docs if d["kind"] == "ServiceAccount"]
    assert accounts == ["perf"]
    assert job["spec"]["template"]["spec"]["serviceAccountName"] == "perf"


def test_env_carries_the_upload_settings(container: dict[str, Any]) -> None:
    assert set(UPLOAD_ENV) <= {e["name"] for e in container["env"]}


def test_the_dependencies_are_pinned_and_boto3_knows_pod_identity(script: str) -> None:
    pip = next(line for line in script.splitlines() if "pip install" in line)
    assert "aiperf==0.13.0" in pip
    boto3 = re.search(r"boto3==(\d+)\.(\d+)\.(\d+)", pip)
    assert boto3, "boto3 must be pinned"
    assert tuple(map(int, boto3.groups())) >= (1, 34, 41), "Pod Identity credentials need boto3 >= 1.34.41"


def test_upload_runs_after_aiperf_and_before_the_hold_and_success_skips_the_hold(script: str) -> None:
    for marker in ("aiperf profile", "upload_file", 'sleep "${HOLD_SECONDS}"'):
        assert marker in script, f"the script must contain {marker!r}"
    profile, upload, hold = script.index("aiperf profile"), script.index("upload_file"), script.index('sleep "${HOLD_SECONDS}"')
    assert profile < upload < hold
    assert "exit 0" in script[upload:hold], "a successful upload must end the Job without the hold"
    assert "exit 1" in script[hold:], "a failed upload must hold the pod for kubectl cp and then fail the Job"
    assert "perf/" in script[profile:hold] and "STAMP" in script[profile:hold] and "date -u +%Y%m%dT%H%M%SZ" in script


def _guard_run(container: dict[str, Any], script: str, overrides: dict[str, str]) -> int:
    """Run the script up to the first apt-get with the env of the manifest plus ``overrides``."""
    assert "apt-get" in script
    prefix = script[: script.index("apt-get")]
    env = {"PATH": "/usr/bin:/bin"} | {e["name"]: e["value"] for e in container["env"]} | overrides
    return lib_proc.run(["bash", "-c", prefix], env=env, capture_output=True, text=True).returncode


def test_guard_runs_before_any_install_and_rejects_empty_values_and_the_shipped_placeholders(container: dict[str, Any], script: str) -> None:
    assert "RESULTS_BUCKET" in script, "the guard must check RESULTS_BUCKET"
    assert script.index("RESULTS_BUCKET") < script.index("apt-get") < script.index("aiperf profile")
    filled = {"CELL": "s-agg", "RESULTS_BUCKET": "my-results-bucket", "AWS_REGION": "us-east-2"}
    assert _guard_run(container, script, filled) == 0
    assert _guard_run(container, script, {}) != 0, "the placeholders shipped in the manifest must not pass"
    for name in UPLOAD_ENV:
        assert _guard_run(container, script, filled | {name: ""}) != 0, f"empty {name} must fail before the long run"


def test_traces_are_named_trace_r_rate_and_the_script_fetches_those_names(script: str) -> None:
    assert sorted(p.name for p in TRACES.glob("*.jsonl.gz")) == sorted(f"trace-r{r}.jsonl.gz" for r in RATES)
    assert "trace-r${RATE}.jsonl.gz" in script


def test_sha256sums_lists_exactly_the_trace_files_with_their_digests() -> None:
    listed = dict(reversed(line.split(maxsplit=1)) for line in (TRACES / "SHA256SUMS").read_text().splitlines() if line.strip())
    assert sorted(listed) == sorted(f"trace-r{r}.jsonl.gz" for r in RATES)
    for name, digest in listed.items():
        assert digest.strip() == hashlib.sha256((TRACES / name).read_bytes()).hexdigest(), name
