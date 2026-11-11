# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Script presence, hygiene and ShellCheck."""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest

import lib_scripts as S


@pytest.mark.parametrize("name", S.REQUIRED_SCRIPTS)
def test_script_exists_and_is_executable(name: str) -> None:
    """The script exists under scripts/ with an executable bit and a bash shebang."""
    path = S.SCRIPTS_DIR / name
    assert path.is_file(), f"missing scripts/{name}"
    assert os.access(path, os.X_OK), f"scripts/{name} is not executable (chmod +x)"
    first = path.read_text().splitlines()[0]
    assert first in ("#!/usr/bin/env bash", "#!/bin/bash"), first


@pytest.mark.parametrize("name", S.REQUIRED_SCRIPTS)
def test_script_uses_strict_mode(name: str) -> None:
    """Scripts fail fast (`set -euo pipefail`)."""
    path = S.SCRIPTS_DIR / name
    assert path.is_file(), f"missing scripts/{name}"
    assert "set -euo pipefail" in path.read_text()


@pytest.mark.parametrize("name", S.REQUIRED_SCRIPTS)
def test_script_bash_syntax(name: str) -> None:
    """`bash -n` parses the script."""
    path = S.SCRIPTS_DIR / name
    assert path.is_file(), f"missing scripts/{name}"
    proc = subprocess.run(["bash", "-n", str(path)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr


@pytest.mark.parametrize("name", S.REQUIRED_SCRIPTS)
def test_script_passes_shellcheck(name: str) -> None:
    """Every script is ShellCheck-clean (skipped only when the binary is not installed)."""
    if shutil.which("shellcheck") is None:
        pytest.skip("shellcheck binary not installed (brew install shellcheck); not installing system packages")
    path = S.SCRIPTS_DIR / name
    assert path.is_file(), f"missing scripts/{name}"
    proc = subprocess.run(
        ["shellcheck", "-x", "--shell=bash", str(path)], capture_output=True, text=True
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr


# ----------------------------------------------------------------------------- what scripts/ holds
KEPT_SCRIPTS = {
    "10-stage-model.sh",
    "11-prefetch.sh",
    "90-teardown.sh",
    "gen_env.py",
    "export-public.sh",      # maintainer tooling for the public export (kept internal, not shipped)
    "export-allowlist.txt",  # the allowlist read by export-public.sh
}


def _every_shell_script() -> list[str]:
    return sorted(str(p.relative_to(S.SCRIPTS_DIR)) for p in [*S.SCRIPTS_DIR.glob("*.sh"), *(S.SCRIPTS_DIR / "lib").glob("*.sh")])


def test_scripts_dir_holds_exactly_the_kept_scripts() -> None:
    """scripts/ holds exactly ``KEPT_SCRIPTS`` and lib/common.sh, nothing else."""
    assert {p.name for p in S.SCRIPTS_DIR.iterdir() if p.name != "__pycache__"} == KEPT_SCRIPTS | {"lib"}
    assert {p.name for p in (S.SCRIPTS_DIR / "lib").iterdir() if p.name != "__pycache__"} == {"common.sh"}


@pytest.mark.parametrize("rel", _every_shell_script())
def test_every_shell_script_passes_shellcheck(rel: str) -> None:
    """ShellCheck-clean, lib/common.sh included (REQUIRED_SCRIPTS covers only the operator scripts)."""
    if shutil.which("shellcheck") is None:
        pytest.skip("shellcheck binary not installed (brew install shellcheck); not installing system packages")
    proc = subprocess.run(["shellcheck", "-x", "--shell=bash", str(S.SCRIPTS_DIR / rel)], capture_output=True, text=True, check=False)
    assert proc.returncode == 0, proc.stdout + proc.stderr


@pytest.mark.parametrize("rel", _every_shell_script())
def test_every_shell_script_parses(rel: str) -> None:
    proc = subprocess.run(["bash", "-n", str(S.SCRIPTS_DIR / rel)], capture_output=True, text=True, check=False)
    assert proc.returncode == 0, proc.stderr
