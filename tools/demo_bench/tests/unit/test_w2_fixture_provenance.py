# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Provenance of ``fixtures/w2_qwen38/window.jsonl`` and ``window.side.jsonl``: recorded, then reproduced.

Contract.

* ``tests/README.md`` records how the two files were made: the ``demo-bench trace w2`` command with every flag of
  ``FLAGS`` (seed included), the source of ``--osl-file`` (the upstream agentic request trace of the NVIDIA Dynamo
  recipes, by commit and sha256, and the sha256 of the extracted ``osl.json``), the sha256 of the full trace and
  sidecar, the run-window cut (``timestamp < CUT_MS``, ``K`` rows) and the sha256 of both committed files.  The
  sha256 values of the committed files are computed here, so the README cannot drift from the fixture.
* With the upstream trace at hand (``DYNAMO_REPO``: a Dynamo checkout with its Git LFS objects pulled; default: a
  sibling directory named ``dynamo`` of the repository), the documented steps regenerate both files byte for byte.
  Without it the reproduction is skipped with the reason; the README check always runs.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

HERE = Path(__file__).resolve()
TESTS = HERE.parents[1]
REPO = HERE.parents[4]
FIX = TESTS / "fixtures" / "w2_qwen38"
README = TESTS / "README.md"

UPSTREAM_COMMIT = "66a95d5aa3b5d238fd94b6f8243c4e479cb7242c"
UPSTREAM_PATH = "recipes/kimi-k2.6/perf/traces/64k_400_90kv_agent_new_noschedule_short_15perc.jsonl"
UPSTREAM_SHA = "f20d3f2bc83dd1306cda659fbe34e7c4d85ca5497626c98bc0b1c4d2211379d0"
OSL_SHA = "d1ee8a76984ba5b535f9c7ce8c4a6a59515b5e2f25a9bd2b6eb3bb053372c986"
FULL_SHA = "34b020cddacdd7e757564f3f9e399aaf4248fe46fb3ac195001e5bf1fd942c0f"
FULL_SIDE_SHA = "a9101c8f89b73ab7d7143de42824d973f7ec7df04b8aabafc14a63606b1e0b73"
CUT_MS = 1740000
K = 920
FLAGS = [
    "--seed", "1111", "--sessions", "195", "--turns", "8", "--global-prefix-tokens", "8192",
    "--entry-tokens", "98304", "--growth-tokens", "3072", "--rate", "0.5", "--gap-sigma", "0.3",
    "--populate-s", "540", "--cold-fraction", "0.12", "--cold-min-tokens", "102400",
    "--cold-max-tokens", "102400", "--osl-cap", "12000", "--max-isl", "200000", "--block-size", "512",
]


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _flag_pairs() -> list[str]:
    return [f"{FLAGS[i]} {FLAGS[i + 1]}" for i in range(0, len(FLAGS), 2)]


def _upstream_trace() -> Path | None:
    """The upstream trace of the Dynamo checkout when its content has the expected sha256 (not an LFS pointer)."""
    path = Path(os.environ.get("DYNAMO_REPO") or REPO.parent / "dynamo") / UPSTREAM_PATH
    return path if path.is_file() and _sha(path) == UPSTREAM_SHA else None


def test_readme_records_the_command_seed_source_cut_and_sha256() -> None:
    text = README.read_text(encoding="utf-8")
    assert "fixtures/w2_qwen38/" in text, "tests/README.md must have a section on fixtures/w2_qwen38/"
    assert "demo-bench trace w2" in text
    flat = " ".join(text.replace("\\\n", " ").split())
    missing = [pair for pair in _flag_pairs() if pair not in flat]
    assert not missing, f"tests/README.md does not record the flags {missing}"
    assert "--osl-file osl.json" in flat
    for needle in (UPSTREAM_COMMIT, UPSTREAM_PATH, UPSTREAM_SHA, OSL_SHA, FULL_SHA, FULL_SIDE_SHA,
                   f"timestamp < {CUT_MS}", f"K = {K}"):
        assert needle in flat, f"tests/README.md does not record {needle}"
    for name in ("window.jsonl", "window.side.jsonl"):
        assert f"`{name}`" in text and _sha(FIX / name) in flat, f"tests/README.md: sha256 of {name}"


def test_committed_window_is_the_first_k_rows_before_the_cut() -> None:
    rows = [json.loads(line) for line in (FIX / "window.jsonl").read_text().splitlines()]
    side = (FIX / "window.side.jsonl").read_text().splitlines()
    assert len(rows) == len(side) == K
    stamps = [r["timestamp"] for r in rows]
    assert stamps == sorted(stamps) and stamps[-1] < CUT_MS
    assert [json.loads(line)["line"] for line in side] == list(range(K))


def test_documented_steps_regenerate_the_fixture_byte_for_byte(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from demo_bench.cli import main

    upstream = _upstream_trace()
    if upstream is None:
        pytest.skip(f"upstream trace {UPSTREAM_PATH} (sha256 {UPSTREAM_SHA[:12]}...) not found: set DYNAMO_REPO to a "
                    "Dynamo checkout and run `git lfs pull` there")
    lines = upstream.read_text(encoding="utf-8").splitlines()
    osl = tmp_path / "osl.json"
    osl.write_text(json.dumps([json.loads(ln)["output_length"] for ln in lines if ln.strip()]) + "\n")
    assert _sha(osl) == OSL_SHA
    full, full_side = tmp_path / "w2-full.jsonl", tmp_path / "w2-full.side.jsonl"
    argv = ["trace", "w2", "--out", str(full), "--sidecar", str(full_side), *FLAGS, "--osl-file", str(osl),
            "--expect-sha256", FULL_SHA]
    assert main(argv) == 0, capsys.readouterr().err[-2000:]
    assert _sha(full) == FULL_SHA and _sha(full_side) == FULL_SIDE_SHA
    trace_rows = full.read_text().splitlines(keepends=True)
    k = sum(json.loads(row)["timestamp"] < CUT_MS for row in trace_rows)
    assert k == K
    assert "".join(trace_rows[:k]).encode() == (FIX / "window.jsonl").read_bytes()
    side_rows = full_side.read_text().splitlines(keepends=True)
    assert "".join(side_rows[:k]).encode() == (FIX / "window.side.jsonl").read_bytes()
