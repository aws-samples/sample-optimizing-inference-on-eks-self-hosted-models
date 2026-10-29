# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""``scripts/13-prepull.sh`` (PREPULL-B1).  RED until implemented.

    13-prepull.sh [--delete]

* ``set -euo pipefail``, sources ``lib/common.sh``, every cluster call through ``kube()`` (KUBE_CONTEXT guard: off-cluster it
  refuses to run without ``KUBE_CONTEXT``; in-cluster no ``--context`` is added; single-token ``--context=<value>``).
* default: ``kubectl apply`` of ``manifests/node-ops/prepull`` (the image mirror overlay is gone: ``IMAGE_MIRROR`` has no effect), then
  ``kubectl rollout status daemonset/prepull-sglang-runtime -n node-ops --timeout=${PREPULL_TIMEOUT:-1800s}`` (a number with an
  optional s/m/h suffix); a rollout that times out -> non-zero exit.
* ``--delete``: removes the DaemonSet (no apply, no rollout status).
* No kubectl output piped into another kubectl (the harness stub never reads stdin).
"""

from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

import lib_scripts as S
from test_95_kube_context import _BARE

SCRIPT = "13-prepull.sh"
DS = "prepull-sglang-runtime"
KUBE_ENV = {"KUBE_CONTEXT": "dynamo-sample-use2", "KUBERNETES_SERVICE_HOST": ""}  # CLUSTER_NAME of the placeholder config


def _run(tmp: Path, *args: str, env: dict[str, str] | None = None, cfg: dict | None = None) -> S.Result:
    return S.Sandbox(tmp, cfg or {}).run(SCRIPT, *args, env={**KUBE_ENV, **(env or {})})


def _kcalls(res: S.Result) -> list[dict]:
    return [c for c in res.kubectl_calls() if "--local" not in c["argv"] and "kustomize" not in c["argv"]]


def _joined(c: dict) -> str:
    return " ".join(c["argv"])


# ----------------------------------------------------------------------------- hygiene
def test_script_exists_executable_strict_and_sources_common() -> None:
    p = S.SCRIPTS_DIR / SCRIPT
    assert p.is_file(), f"missing scripts/{SCRIPT}"
    assert os.access(p, os.X_OK), "not executable"
    text = p.read_text()
    assert text.splitlines()[0] in ("#!/usr/bin/env bash", "#!/bin/bash")
    assert "set -euo pipefail" in text and "lib/common.sh" in text


def test_script_bash_syntax_and_shellcheck() -> None:
    p = S.SCRIPTS_DIR / SCRIPT
    assert p.is_file()
    assert subprocess.run(["bash", "-n", str(p)], capture_output=True, text=True).returncode == 0
    if shutil.which("shellcheck") is None:
        pytest.skip("shellcheck binary not installed")
    proc = subprocess.run(["shellcheck", "-x", "--shell=bash", str(p)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_every_kubectl_call_goes_through_the_kube_wrapper_and_nothing_is_piped() -> None:
    text = (S.SCRIPTS_DIR / SCRIPT).read_text()
    bad = [ln for ln in text.splitlines() if not ln.strip().startswith("#") and "kubectl kustomize" not in ln and "--local" not in ln and _BARE.search(ln)]
    assert not bad, bad
    assert not re.search(r"(?:kube|kubectl)[^\n|]*\|\s*\\?\s*\n?\s*(?:kube|kubectl)\s", text), "a kubectl call is piped into another"


# ----------------------------------------------------------------------------- usage
@pytest.mark.parametrize("args", [("--bogus",), ("--delete", "extra"), ("apply",), ("--delete", "--delete-all")], ids=["unknown-flag", "extra-arg", "positional", "unknown-long"])
def test_bad_usage_exits_non_zero_before_any_kubectl_call(tmp_path: Path, args: tuple[str, ...]) -> None:
    res = _run(tmp_path, *args)
    assert res.rc != 0 and res.kubectl_calls() == []


@pytest.mark.parametrize("value", ["abc", "-5", "10x", "1.5m"], ids=["word", "negative", "bad-unit", "decimal"])
def test_invalid_timeout_is_rejected_before_any_kubectl_call(tmp_path: Path, value: str) -> None:
    res = _run(tmp_path, env={"PREPULL_TIMEOUT": value})
    assert res.rc != 0 and res.kubectl_calls() == []


# ----------------------------------------------------------------------------- apply + rollout
def test_default_applies_the_base_then_waits_for_the_rollout(tmp_path: Path) -> None:
    res = _run(tmp_path, env={"IMAGE_MIRROR": ""})
    assert res.rc == 0, res.stdout + res.stderr
    calls = _kcalls(res)
    applies = [c for c in calls if "apply" in c["argv"]]
    assert len(applies) == 1, [c["argv"] for c in applies]
    blob = " ".join(_joined(c) for c in res.kubectl_calls())
    assert re.search(r"node-ops/prepull(?:/|\s|$)", blob) and "mirror/prepull" not in blob
    assert not [c for c in calls if "patch" in c["argv"] or "replace" in c["argv"]]
    rollouts = [c for c in calls if "rollout" in c["argv"] and "status" in c["argv"]]
    assert len(rollouts) == 1
    r = rollouts[0]["argv"]
    assert f"daemonset/{DS}" in r or ("daemonset" in r and DS in r), r
    assert S.arg_value(r, "-n") == "node-ops" or S.arg_value(r, "--namespace") == "node-ops", r
    assert S.arg_value(r, "--timeout") == "1800s", r
    assert applies[0]["seq"] < rollouts[0]["seq"], "the rollout is watched after the apply"


@pytest.mark.parametrize("value", ["0", "1"])
def test_image_mirror_has_no_effect_there_is_no_mirror_overlay(tmp_path: Path, value: str) -> None:
    res = _run(tmp_path, env={"IMAGE_MIRROR": value})
    blob = " ".join(_joined(c) for c in res.kubectl_calls())
    assert res.rc == 0 and "node-ops/prepull" in blob and "mirror" not in blob


@pytest.mark.parametrize("value", ["900s", "45m", "2h", "600"])
def test_prepull_timeout_overrides_the_default(tmp_path: Path, value: str) -> None:
    res = _run(tmp_path, env={"PREPULL_TIMEOUT": value})
    assert res.rc == 0, res.stdout + res.stderr
    (r,) = [c for c in _kcalls(res) if "rollout" in c["argv"]]
    assert S.arg_value(r["argv"], "--timeout") == value


def test_a_rollout_that_times_out_is_a_failure(tmp_path: Path) -> None:
    """The harness stub cannot fail one verb: a wrapper in front of it makes `rollout status` exit 1 (stub still logs the call)."""
    sb = S.Sandbox(tmp_path, {})
    wrapper_dir = tmp_path / "wrap"
    wrapper_dir.mkdir()
    w = wrapper_dir / "kubectl"
    w.write_text(
        f'#!/usr/bin/env bash\n"{sb.stub_dir}/kubectl" "$@"\nrc=$?\ncase " $* " in *" rollout "*)'
        ' echo "error: timed out waiting for the rollout to finish" >&2; exit 1;; esac\nexit $rc\n'
    )
    w.chmod(w.stat().st_mode | stat.S_IEXEC)
    res = sb.run(SCRIPT, env={**KUBE_ENV, "PATH": f"{wrapper_dir}:{sb.stub_dir}:{os.environ['PATH']}"})
    assert res.rc != 0
    assert [c for c in res.kubectl_calls() if "rollout" in c["argv"]], "the rollout was never watched"


# ----------------------------------------------------------------------------- delete
def test_delete_removes_the_daemonset_and_does_not_apply_or_wait(tmp_path: Path) -> None:
    res = _run(tmp_path, "--delete")
    assert res.rc == 0, res.stdout + res.stderr
    calls = _kcalls(res)
    deletes = [c for c in calls if "delete" in c["argv"]]
    assert len(deletes) == 1, [c["argv"] for c in calls]
    j = _joined(deletes[0])
    assert (DS in j or "prepull" in j) and "node-ops" in j
    assert S.has_arg(deletes[0]["argv"], "--ignore-not-found"), "deleting twice must not fail"
    assert not [c for c in calls if "apply" in c["argv"] or "rollout" in c["argv"]]


# ----------------------------------------------------------------------------- kube-context guard
def test_refuses_off_cluster_without_context(tmp_path: Path) -> None:
    for args in ((), ("--delete",)):
        res = _run(tmp_path / "-".join(args or ("run",)), *args, env={"KUBE_CONTEXT": "", "KUBERNETES_SERVICE_HOST": ""})
        assert res.rc != 0 and "KUBE_CONTEXT" in res.stdout + res.stderr
        assert res.kubectl_calls() == []


def test_in_cluster_needs_no_context_and_adds_no_flag(tmp_path: Path) -> None:
    res = _run(tmp_path, env={"KUBE_CONTEXT": "", "KUBERNETES_SERVICE_HOST": "10.96.0.1"})
    assert res.rc == 0, res.stdout + res.stderr
    assert res.kubectl_calls() and not any(S.has_arg(c["argv"], "--context") for c in res.kubectl_calls())


@pytest.mark.parametrize("args", [(), ("--delete",)], ids=["apply", "delete"])
def test_context_flag_on_every_cluster_call(tmp_path: Path, args: tuple[str, ...]) -> None:
    res = _run(tmp_path, *args, env={"KUBE_CONTEXT": "dynamo-sample-use2"})
    assert res.rc == 0, res.stdout + res.stderr
    calls = _kcalls(res)
    assert calls and all("--context=dynamo-sample-use2" in c["argv"] for c in calls), [c["argv"] for c in calls]
