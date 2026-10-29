# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Track Q committed renders, drift gate, regeneration script, goldens and ownership.  RED until implemented.

* ``manifests/track-q-qwen38/rendered/S0..S7.yaml`` are the committed output of ``kubectl kustomize cells/S<n>``; a render that
  drifts from its committed file fails here (regenerate deliberately with ``scripts/render-track-q.sh``).
* ``golden_trackq_renders.json`` pins the sha256 of every new Track Q render (8 cells + 4 overlays + 2 frontends).  The developer
  creates it as the LAST GREEN step with

      cd tests/manifests && uv run --with-requirements ../requirements-test.txt python -c \\
        "import json, lib_trackq as Q; print(json.dumps(Q.render_hashes(), indent=1, sort_keys=True))" > golden_trackq_renders.json
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from pathlib import Path

import pytest

import lib_manifests as L
import lib_proc
import lib_trackq as Q


# ----------------------------------------------------------------------------- committed renders
def test_rendered_directory_holds_exactly_s0_to_s7() -> None:
    assert Q.RENDERED_DIR.is_dir(), "manifests/track-q-qwen38/rendered missing"
    assert sorted(p.name for p in Q.RENDERED_DIR.iterdir() if p.name != ".DS_Store") == [f"{c}.yaml" for c in Q.CELL_IDS]


@pytest.mark.parametrize("cell", Q.CELL_IDS)
def test_committed_render_equals_kustomize_build(cell: str) -> None:
    committed = Q.RENDERED_DIR / f"{cell}.yaml"
    assert committed.is_file(), f"{committed} missing"
    assert committed.read_text() == L.render_text(Q.cell_rel(cell)), (
        f"rendered/{cell}.yaml drifted from `kubectl kustomize {Q.cell_rel(cell)}`: run scripts/render-track-q.sh"
    )


@pytest.mark.parametrize("cell", Q.CELL_IDS)
def test_committed_render_is_the_reference_cell(cell: str) -> None:
    import yaml

    docs = [d for d in yaml.safe_load_all((Q.RENDERED_DIR / f"{cell}.yaml").read_text()) if d]
    assert len(docs) == 1
    assert Q.diff_paths(Q.normalised(docs[0]), Q.normalised(Q.reference_doc(cell))) == []


# ----------------------------------------------------------------------------- regeneration script
def test_regeneration_script_is_a_clean_executable_bash_script() -> None:
    path = Q.REGEN_SCRIPT
    assert path.is_file(), f"missing {path.relative_to(Q.L.REPO_ROOT)}"
    assert os.access(path, os.X_OK)
    text = path.read_text()
    assert text.splitlines()[0] in ("#!/usr/bin/env bash", "#!/bin/bash") and "set -euo pipefail" in text
    assert lib_proc.run(["bash", "-n", str(path)], capture_output=True, text=True).returncode == 0
    if shutil.which("shellcheck"):
        proc = lib_proc.run(["shellcheck", "-x", "--shell=bash", str(path)], capture_output=True, text=True)
        assert proc.returncode == 0, proc.stdout + proc.stderr


def test_regeneration_script_reproduces_the_committed_files(tmp_path: Path) -> None:
    out = tmp_path / "out"
    proc = lib_proc.run([str(Q.REGEN_SCRIPT), str(out)], capture_output=True, text=True, timeout=300, cwd=tmp_path)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert sorted(p.name for p in out.iterdir()) == [f"{c}.yaml" for c in Q.CELL_IDS]
    for cell in Q.CELL_IDS:
        assert (out / f"{cell}.yaml").read_bytes() == (Q.RENDERED_DIR / f"{cell}.yaml").read_bytes(), cell


def test_regeneration_script_default_target_is_the_rendered_directory_and_touches_nothing_else() -> None:
    text = Q.REGEN_SCRIPT.read_text()
    assert "track-q-qwen38/rendered" in text and "kubectl kustomize" in text
    for forbidden in ("kubectl apply", "kubectl delete", "terraform", "aws "):
        assert forbidden not in text, forbidden


# ----------------------------------------------------------------------------- goldens
def test_golden_exists_and_lists_exactly_the_track_q_renders() -> None:
    assert Q.GOLDEN.is_file(), f"{Q.GOLDEN.name} missing: create it as the last GREEN step (see this module's docstring)"
    assert sorted(json.loads(Q.GOLDEN.read_text())) == sorted(Q.RENDER_RELS)


def test_track_q_renders_are_byte_identical_to_the_golden() -> None:
    golden = json.loads(Q.GOLDEN.read_text())
    changed = [r for r, h in sorted(golden.items()) if hashlib.sha256(L.render_text(r).encode()).hexdigest() != h]
    assert not changed, f"Track Q renders changed (refresh {Q.GOLDEN.name} only if intended): {changed}"


def test_track_q_renders_are_deterministic() -> None:
    first = Q.render_hashes()
    L.render_text.cache_clear()
    assert Q.render_hashes() == first


def test_cell_golden_hash_matches_the_committed_file() -> None:
    golden = json.loads(Q.GOLDEN.read_text())
    for cell in Q.CELL_IDS:
        assert golden[Q.cell_rel(cell)] == hashlib.sha256((Q.RENDERED_DIR / f"{cell}.yaml").read_bytes()).hexdigest()
