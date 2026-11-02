# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""``scripts/render-track-q-nvlink.sh [OUT_DIR]``: regenerates ``manifests/track-q-qwen38/rendered-nvlink``.  RED until implemented.

``scripts/render-track-q.sh`` writes exactly S0..S7 into ``rendered/`` (the EFA renders behind the published numbers) and
``tests/manifests/test_412`` pins that.  The NVLink alternative of the four prefill/decode cells therefore has its own script,
modelled on it: ``OUT_DIR/S<n>.yaml = kubectl kustomize manifests/track-q-qwen38/variants-nvlink/S<n>`` for S3, S5, S6, S7,
default ``OUT_DIR`` = ``manifests/track-q-qwen38/rendered-nvlink``.  It is offline (it only renders) and it renders every cell
before it writes any file.  ``scripts/render-examples.sh`` renders the ``nvlink/`` sub-folder of the four prefill/decode example folders (see
``tests/manifests/test_420_examples.py`` and ``test_421_example_pd_nvlink.py``).

Nothing here weakens an expectation: a missing script is a failure, never a skip.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import lib_proc
import lib_scripts as S

SCRIPT = S.SCRIPTS_DIR / "render-track-q-nvlink.sh"
REPO = S.REPO_ROOT
TQ = REPO / "manifests" / "track-q-qwen38"
RENDERED = TQ / "rendered-nvlink"
CELLS = ("S3", "S5", "S6", "S7")


def _script() -> Path:
    assert SCRIPT.is_file(), "scripts/render-track-q-nvlink.sh missing"
    return SCRIPT


def test_script_is_a_clean_executable_bash_script_with_the_license_header() -> None:
    path = _script()
    assert os.access(path, os.X_OK)
    text = path.read_text()
    lines = text.splitlines()
    assert lines[0] in ("#!/usr/bin/env bash", "#!/bin/bash") and "set -euo pipefail" in text
    assert "SPDX-License-Identifier: MIT-0" in "\n".join(lines[:5])
    assert text.isascii()
    assert lib_proc.run(["bash", "-n", str(path)], capture_output=True, text=True, check=False).returncode == 0
    if shutil.which("shellcheck"):
        proc = lib_proc.run(["shellcheck", "-x", "--shell=bash", str(path)], capture_output=True, text=True, check=False)
        assert proc.returncode == 0, proc.stdout + proc.stderr


def test_script_is_offline_and_renders_the_nvlink_overlays_only() -> None:
    text = _script().read_text()
    assert "kubectl kustomize" in text and "variants-nvlink" in text and "rendered-nvlink" in text
    for forbidden in ("kubectl apply", "kubectl delete", "kubectl get", "terraform", "aws ", "docker "):
        assert forbidden not in text, forbidden
    assert "cells/S" not in text.replace("variants-nvlink", ""), "it renders the overlays of variants-nvlink, not the published cells"


def test_script_reproduces_the_committed_renders(tmp_path: Path) -> None:
    out = tmp_path / "out"
    proc = lib_proc.run([str(_script()), str(out)], capture_output=True, text=True, timeout=300, cwd=tmp_path, check=False)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert sorted(p.name for p in out.iterdir()) == [f"{c}.yaml" for c in CELLS], "exactly S3, S5, S6, S7"
    for cell in CELLS:
        committed = RENDERED / f"{cell}.yaml"
        assert committed.is_file(), f"rendered-nvlink/{cell}.yaml missing"
        assert (out / f"{cell}.yaml").read_bytes() == committed.read_bytes(), f"{cell} drifted"


def test_script_does_not_touch_the_published_renders(tmp_path: Path) -> None:
    published = {p.name: p.read_bytes() for p in (TQ / "rendered").iterdir()}
    proc = lib_proc.run([str(_script()), str(tmp_path / "out")], capture_output=True, text=True, timeout=300, cwd=tmp_path, check=False)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert {p.name: p.read_bytes() for p in (TQ / "rendered").iterdir()} == published
    assert sorted(published) == [f"S{n}.yaml" for n in range(8)]


def test_script_default_target_is_the_rendered_nvlink_directory() -> None:
    text = _script().read_text()
    assert "manifests/track-q-qwen38/rendered-nvlink" in text, "the default OUT_DIR"
    assert "manifests/track-q-qwen38/rendered\"" not in text and "track-q-qwen38/rendered}" not in text, "never the published directory"


def test_script_refuses_more_than_one_argument(tmp_path: Path) -> None:
    proc = lib_proc.run([str(_script()), str(tmp_path / "a"), str(tmp_path / "b")], capture_output=True, text=True, cwd=tmp_path, check=False)
    assert proc.returncode == 2 and "usage" in proc.stderr.lower()
    assert not (tmp_path / "a").exists()


def test_the_published_render_script_still_writes_exactly_s0_to_s7(tmp_path: Path) -> None:
    out = tmp_path / "out"
    proc = lib_proc.run([str(REPO / "scripts" / "render-track-q.sh"), str(out)], capture_output=True, text=True, timeout=300, cwd=tmp_path, check=False)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert sorted(p.name for p in out.iterdir()) == [f"S{n}.yaml" for n in range(8)]
    for p in out.iterdir():
        assert p.read_bytes() == (TQ / "rendered" / p.name).read_bytes(), p.name
