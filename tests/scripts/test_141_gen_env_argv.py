# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Process audit of ``scripts/gen_env.py`` (static analysis: Semgrep dangerous-subprocess-use-audit, Bandit B603), written
before the fix (RED).

Contract: without ``--from-json`` the script runs ``[<terraform on PATH>, -chdir=<repo>/terraform/cluster, output,
-json]``: the executable resolved with ``shutil.which`` (absent: the refusal "terraform is not on PATH", nothing
started), a list argv and never a shell. The only variable element is the ``-chdir=`` token, so an injection-shaped
``--repo-root`` ('--kubeconfig=x', '; rm', '$(x)', a newline) reaches terraform as part of that one inert argument.
"""

from __future__ import annotations

import importlib.util
import json
import os
import stat
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import lib_proc
import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "gen_env.py"
INJECTED_ROOTS = ["--kubeconfig=x", "r; rm -rf x", "r$(touch hit)", "r`touch hit`", "r\nb"]


def load_gen_env() -> ModuleType:
    """scripts/gen_env.py as a module (it is a script, not a package)."""
    spec = importlib.util.spec_from_file_location("gen_env_under_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    old, sys.dont_write_bytecode = sys.dont_write_bytecode, True  # leave no scripts/__pycache__ behind
    try:
        spec.loader.exec_module(mod)
    finally:
        sys.dont_write_bytecode = old
    return mod


def test_terraform_is_run_through_its_resolved_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    gen_env = load_gen_env()
    calls: list[tuple[list[str], dict[str, Any]]] = []

    def fake_run(argv: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        calls.append((list(argv), kw))
        return subprocess.CompletedProcess("fake", 0, "{}", "")  # a result record only: the argv is in calls

    monkeypatch.setattr(gen_env.shutil, "which", lambda name: f"/opt/test/bin/{name}")
    monkeypatch.setattr(gen_env.subprocess, "run", fake_run)
    assert gen_env.load_outputs(None, tmp_path) == {}
    argv, kw = calls[0]
    assert argv == ["/opt/test/bin/terraform", f"-chdir={tmp_path / 'terraform' / 'cluster'}", "output", "-json"]
    assert kw.get("shell") is None
    assert (kw.get("capture_output"), kw.get("text"), kw.get("check")) == (True, True, False)


def test_a_missing_terraform_is_refused_before_anything_runs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    gen_env = load_gen_env()

    def no_run(*args: Any, **kw: Any) -> None:
        raise AssertionError("a process was started")

    monkeypatch.setattr(gen_env.shutil, "which", lambda name: None)
    monkeypatch.setattr(gen_env.subprocess, "run", no_run)
    with pytest.raises(gen_env.RefusalError, match="terraform is not on PATH"):
        gen_env.load_outputs(None, tmp_path)


@pytest.mark.parametrize("root_name", INJECTED_ROOTS)
def test_an_injection_shaped_repo_root_stays_inside_the_chdir_argument(tmp_path: Path, root_name: str) -> None:
    """A fake terraform on PATH logs its argv NUL-separated: exactly three arguments, no shell expansion."""
    work = tmp_path / "work"
    (work / root_name).mkdir(parents=True)
    argv_log = tmp_path / "argv.bin"
    fake = tmp_path / "bin" / "terraform"
    fake.parent.mkdir()
    fake.write_text(f'#!/bin/sh\nprintf "%s\\0" "$@" > "{argv_log}"\nprintf "%s" "$PAYLOAD"\n')
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    env = {**os.environ, "PATH": f"{fake.parent}{os.pathsep}{os.environ['PATH']}", "PAYLOAD": json.dumps({})}
    proc = lib_proc.run(
        [sys.executable, "-I", str(SCRIPT), f"--repo-root={root_name}"],
        capture_output=True,
        text=True,
        check=False,
        env=env,
        cwd=work,
    )
    args = argv_log.read_bytes().decode().split("\0")[:-1]
    assert args == [f"-chdir={Path(root_name) / 'terraform' / 'cluster'}", "output", "-json"]
    assert proc.returncode == 1  # the empty outputs are refused (missing platform_env): terraform itself did run
    assert "platform_env" in proc.stderr
    assert not (work / "hit").exists()
    assert not (work / root_name / "manifests").exists()
