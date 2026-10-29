# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Node-ops Job fixes found during block 1 (P-30, P-5/P-22).  The P-39 compile-cache cases left with that tree.

* P-30  ``11-prefetch.sh`` (the config model): its values equal the Job defaults, so ``kubectl set env --local -o yaml``
        prints nothing and ``kubectl apply -f <empty>`` fails ("no objects passed to apply").  The script must apply the
        rendered Job when set-env produced nothing, and the set-env output when it did.
* P-5/P-22  ``11-prefetch.sh`` never deletes a Job that is still active; a terminal Job
        (Complete or Failed) is deleted and replaced; an absent Job is simply submitted; an active Job is refused (non-zero,
        message naming the Job, no delete, no apply).  Contract assumed for reading the Job state: ``kubectl get job
        <name> -n node-ops -o json`` (the same call the ``--wait`` loop of 11-prefetch.sh already makes).
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "manifests"))
import lib_scripts as S

KUBE_ENV = {"KUBE_CONTEXT": "dynamo-sample-use2", "KUBERNETES_SERVICE_HOST": ""}  # CLUSTER_NAME of the placeholder config
RENDERED = "apiVersion: batch/v1\nkind: Job\nmetadata:\n  name: RENDERED-MARKER\n"
CHANGED = "apiVersion: batch/v1\nkind: Job\nmetadata:\n  name: CHANGED-BY-SET-ENV\n"

ACTIVE = {"status": {"active": 1}}
COMPLETE = {"status": {"conditions": [{"type": "Complete", "status": "True"}], "succeeded": 1}}
FAILED = {"status": {"conditions": [{"type": "Failed", "status": "True"}], "failed": 1}}

PREFETCH = ("11-prefetch.sh", "model-prefetch", ())
SCRIPTS = [PREFETCH]
IDS = ["prefetch"]


def _run(tmp: Path, script: str, args: tuple[str, ...], cfg: dict[str, Any]) -> S.Result:
    # Default: set-env changed something, so only the behaviour under test can fail; P-30 tests override it with "".
    base = {"kustomize_output": RENDERED, "apply_requires_objects": True, "set_env_output": CHANGED, **cfg}
    return S.Sandbox(tmp, base).run(script, *args, env={**KUBE_ENV, "MODEL_BUCKET": "qa-models"})


def _cluster_calls(res: S.Result, verb: str) -> list[dict[str, Any]]:
    return [c for c in res.kubectl_calls() if verb in c["argv"] and "--local" not in c["argv"]]


def _job_deletes(res: S.Result, job: str) -> list[dict[str, Any]]:
    return [c for c in _cluster_calls(res, "delete") if job in " ".join(c["argv"])]


def _applied(tmp: Path) -> list[dict[str, str]]:
    f = tmp / "state" / "applied.jsonl"
    return [json.loads(line) for line in f.read_text().splitlines() if line.strip()]


# ----------------------------------------------------------------------------- P-30
def test_p30_prefetch_applies_the_rendered_job_when_set_env_changes_nothing(tmp_path: Path) -> None:
    """The config model's values equal the Job defaults: set env prints nothing; the rendered Job must be applied."""
    res = _run(tmp_path, "11-prefetch.sh", (), {"set_env_output": ""})
    assert res.rc == 0, res.stdout + res.stderr
    assert "no objects passed to apply" not in res.stdout + res.stderr
    contents = [a["content"] for a in _applied(tmp_path)]
    assert contents, "no `kubectl apply -f <file>` of the Job"
    assert any("RENDERED-MARKER" in c for c in contents), contents


def test_p30_prefetch_applies_the_set_env_output_when_it_changed_something(tmp_path: Path) -> None:
    res = _run(tmp_path, "11-prefetch.sh", ("--model-id", "acme/Other-1B", "--revision", "c" * 40), {"set_env_output": CHANGED})
    assert res.rc == 0, res.stdout + res.stderr
    contents = [a["content"] for a in _applied(tmp_path)]
    assert any("CHANGED-BY-SET-ENV" in c for c in contents), contents
    assert not any("RENDERED-MARKER" in c for c in contents), "the unmodified render must not replace the set-env output"


def test_p30_prefetch_still_passes_the_job_parameters_to_set_env(tmp_path: Path) -> None:
    res = _run(tmp_path, "11-prefetch.sh", (), {})
    sets = [c for c in res.kubectl_calls() if "set" in c["argv"] and "env" in c["argv"]]
    assert sets and any(a.startswith("MODEL_S3_URI=s3://qa-models/") for a in sets[0]["argv"]), [c["argv"] for c in sets]


# ----------------------------------------------------------------------------- P-5 / P-22
ACTIVE_CASES = [
    ("11-prefetch.sh", "model-prefetch", ()),
    ("11-prefetch.sh", "model-prefetch", ("--force",)),
]


@pytest.mark.parametrize(("script", "job", "args"), ACTIVE_CASES, ids=["prefetch", "prefetch-force"])
def test_active_job_is_never_deleted_and_the_run_is_refused(tmp_path: Path, script: str, job: str, args: tuple[str, ...]) -> None:
    res = _run(tmp_path, script, args, {"jobs": {job: ACTIVE}})
    out = res.stdout + res.stderr
    assert res.rc != 0, out
    assert job in out and re.search(r"(?i)active|running|in progress|still", out), out
    assert _job_deletes(res, job) == [], "an active Job must never be deleted"
    assert _applied(tmp_path) == [], "nothing may be submitted over an active Job"
    assert not [c for c in _cluster_calls(res, "apply") if "-f" in c["argv"]]


@pytest.mark.parametrize(("script", "job", "args"), SCRIPTS, ids=IDS)
@pytest.mark.parametrize("state", [COMPLETE, FAILED], ids=["complete", "failed"])
def test_terminal_job_is_deleted_then_replaced(tmp_path: Path, script: str, job: str, args: tuple[str, ...], state: dict[str, Any]) -> None:
    res = _run(tmp_path, script, args, {"jobs": {job: state}})
    assert res.rc == 0, res.stdout + res.stderr
    deletes = _job_deletes(res, job)
    assert deletes, "the terminal Job (immutable pod template) must be deleted before the re-submit"
    assert all(S.has_arg(d["argv"], "-n", "--namespace") for d in deletes)
    submits = [c for c in _cluster_calls(res, "apply") if "-f" in c["argv"]]
    assert submits and max(d["seq"] for d in deletes) < submits[-1]["seq"]


@pytest.mark.parametrize(("script", "job", "args"), SCRIPTS, ids=IDS)
def test_absent_job_is_simply_submitted(tmp_path: Path, script: str, job: str, args: tuple[str, ...]) -> None:
    res = _run(tmp_path, script, args, {"jobs": {}})
    assert res.rc == 0, res.stdout + res.stderr
    assert _applied(tmp_path), "the Job was not submitted"
    assert _cluster_calls(res, "get"), "the Job state must be read (kubectl get job) before any delete"


@pytest.mark.parametrize(("script", "job", "args"), SCRIPTS, ids=IDS)
def test_job_state_is_read_before_any_delete(tmp_path: Path, script: str, job: str, args: tuple[str, ...]) -> None:
    res = _run(tmp_path, script, args, {"jobs": {job: COMPLETE}})
    gets = [c for c in _cluster_calls(res, "get") if job in " ".join(c["argv"])]
    deletes = _job_deletes(res, job)
    assert gets and deletes and min(g["seq"] for g in gets) < min(d["seq"] for d in deletes)
