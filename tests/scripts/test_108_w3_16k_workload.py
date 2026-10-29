# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""W3-16K workload folder (n=1): reproducible from the repo.  RED until ``workloads/w3-16k/`` exists.

Contract
========
``workloads/w3-16k/`` holds:

* ``params.json``: the W3 shape (generator flags), the rates with ``N_s = 75 x rate``, the cut at 615 s, the
  measured window, the absolute SLO, the cells (S0 and S7) and the recorded sha256 of every window trace and sidecar.
  Hashes only: no bucket, account or region literal (the inputs JSON of the run, which has them, stays outside).
* ``regen.sh <rate> <outdir> [--dry-run]``: regenerates the trace with the existing ``demo-bench trace w2``
  (unchanged), cuts it at rows with ``timestamp < cut_ms`` (trace and sidecar, same K), writes
  ``w3-16k-r<rate>.jsonl``, ``w3-16k-r<rate>.side.jsonl`` and a ``SHA256SUMS`` (``sha256sum`` format) in ``outdir``.
  ``--dry-run`` prints the exact generator command (one line, ``demo-bench trace w2 --out ... ``) and writes nothing.
  The generator command is ``$DEMO_BENCH`` (word-split) when set, else
  ``uv run --no-dev --project tools/demo_bench demo-bench`` (the trace needs no dev dependency).
  Offline: no network, no AWS.
* ``README.md``: labelled n=1, not a result of record, never pooled with the W2 of record (``params.json`` ``status``
  starts with ``W3-16K, n=1``).

The truth to match is the inputs JSON recorded by the run (``W3_INPUTS_JSON``; the tests that need it skip when it is unset).
When that file is absent the cross-checks against it are skipped (they are the only ones that need it); the
checks against the amendment values written below always run.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
W3 = REPO_ROOT / "workloads" / "w3-16k"
PARAMS = W3 / "params.json"
REGEN = W3 / "regen.sh"
README = W3 / "README.md"
DEMO_SRC = REPO_ROOT / "tools" / "demo_bench" / "src"

# The inputs JSON recorded by the run is not part of the repository: point W3_INPUTS_JSON at it to run the tests
# that compare against the recorded run; without it they skip with a reason.
INPUTS_JSON = Path(os.environ.get("W3_INPUTS_JSON", "/nonexistent/w3-16k-inputs.json"))
needs_inputs = pytest.mark.skipif(
    not INPUTS_JSON.exists(), reason="W3_INPUTS_JSON is not set (the w3-16k-inputs.json of the run is not in the repository)"
)

# W3-16K-AMENDMENT section 1 (the shape is identical at every rate).
SHAPE_FLAGS = {
    "--seed": "1111",
    "--turns": "11",
    "--global-prefix-tokens": "2048",
    "--entry-tokens": "5632",
    "--growth-tokens": "1024",
    "--gap-sigma": "0.3",
    "--populate-s": "180",
    "--cold-fraction": "0.03",
    "--cold-min-tokens": "15872",
    "--cold-max-tokens": "15872",
    "--osl": "500",
    "--max-isl": "16384",
    "--block-size": "512",
}
RATES = {10: 750, 25: 1875, 32: 2400, 50: 3750}  # rate -> N_s = 75 x rate
# Recorded (inputs JSON, amendment): rows K of the cut trace and the sha256 of window trace / sidecar.
RECORDED = {
    10: (5769, "906debd423abaa3338fad4687a814db2affb25caaf7fae5156d8e2eb6849730c", "daecaa73cc8e7af82d2d611a20ee75a3734bafd298337562a9dec23751efad96"),
    25: (14472, "49d83f37410f6205eccfe090f50dd47082cac2580ba410c1a11cc39743f83f21", "ef4bdd276b8cc9497d9a760f94fccabf00622a34458b2c0b92e53a9cc314ec28"),
    32: (18507, "cb872dc5c0aa9553d85b0f839387f56a6195f726ce4c9c29479e2b9cd4590301", "fa68d96658c45eeead8446479154d8189eb5327a1a46afc67b5bfd16a16a4279"),
    50: (28905, "12be497f5c5a2cf262005966e70ce1d36f7d26733e66eab12777cf2dd38b41fb", "df08a7b531a6223105bb7b2967ebcb85cf908df946744ba6be043e8a9c44e4ee"),
}
CUT_MS = 615_000

ACCOUNT_RE = re.compile(r"(?<![A-Za-z0-9])[0-9]{12}(?![A-Za-z0-9])")
REGION_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:us|eu|ap|sa|ca|me|af|il|mx)-(?:north|south|east|west|central|northeast|southeast|northwest|southwest)-[0-9](?![A-Za-z0-9])"
)
SHORT_RE = re.compile(r"(?<![A-Za-z0-9])(?:use|usw|euw|euc|eun|aps|apn|sae|cac)[0-9](?![A-Za-z0-9])")


def _params() -> dict:
    assert PARAMS.is_file(), f"missing {PARAMS}"
    return json.loads(PARAMS.read_text())


def _inputs() -> dict:
    return json.loads(INPUTS_JSON.read_text())


def _env() -> dict[str, str]:
    env = dict(os.environ)
    env["DEMO_BENCH"] = f"{shlex.quote(sys.executable)} -m demo_bench"
    env["PYTHONPATH"] = str(DEMO_SRC)
    return env


def _regen(*args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    assert REGEN.is_file(), f"missing {REGEN}"
    return subprocess.run(
        ["bash", str(REGEN), *args], capture_output=True, text=True, env=env or _env(), cwd=REPO_ROOT, timeout=600, check=False
    )


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ------------------------------------------------------------------------------------------- layout
def test_folder_has_readme_params_and_executable_regen() -> None:
    for f in (README, PARAMS, REGEN):
        assert f.is_file(), f"missing {f.relative_to(REPO_ROOT)}"
    assert os.access(REGEN, os.X_OK), "regen.sh must be executable"


def test_regen_passes_shellcheck() -> None:
    if shutil.which("shellcheck") is None:
        pytest.skip("shellcheck not installed")
    assert REGEN.is_file()
    res = subprocess.run(["shellcheck", "-x", "--shell=bash", str(REGEN)], capture_output=True, text=True, check=False)
    assert res.returncode == 0, res.stdout + res.stderr


# ------------------------------------------------------------------------------------------- params vs amendment
def test_params_shape_matches_the_amendment() -> None:
    p = _params()
    shape = p["shape"]
    assert {k: str(v) for k, v in shape.items()} == {k.removeprefix("--"): v for k, v in SHAPE_FLAGS.items()}


def test_params_cut_window_and_session_rule() -> None:
    p = _params()
    assert p["cut_ms"] == CUT_MS
    assert p["measured_window_s"] == [255, 615]
    assert p["sessions_per_rate"] == 75  # N_s = 75 x rate
    assert {int(r) for r in p["rates"]} == set(RATES)
    for rate, n_s in RATES.items():
        assert p["rates"][str(rate)]["sessions"] == n_s == 75 * rate


def test_params_record_the_hashes_of_every_window() -> None:
    p = _params()
    for rate, (k, trace, side) in RECORDED.items():
        r = p["rates"][str(rate)]
        assert (r["window_rows"], r["window_sha256"], r["sidecar_sha256"]) == (k, trace, side)


def test_params_absolute_slo() -> None:
    slo = _params()["slo"]
    assert slo["continuation_ttft_s"] == 1.0
    assert slo["cold_ttft_s"] == 2.0
    assert slo["itl_ms"] == 25.0
    assert slo["plan_header"] == "slo ttft_s=1.0 itl_ms=25 cold_factor=1 cold_prefill_tok_s=7936"
    assert 15872 / slo["cold_prefill_tok_s"] == slo["cold_ttft_s"]  # the cold rule: factor x ISL / rate


def test_params_cells_and_rate_outcomes() -> None:
    p = _params()
    assert p["cells"] == ["S0", "S7"]
    # The "not a result of record" disclaimer was dropped; the status now states the factual provenance.
    assert p["status"].startswith("W3-16K, n=1 per cell and rate")
    assert "never pooled with the W2" in p["status"]
    assert p["rates"]["50"]["outcome"] == "inconclusive"  # harness limit, not a server result
    for rate in ("10", "25", "32"):
        assert p["rates"][rate]["outcome"] != "inconclusive"


@needs_inputs
def test_params_match_the_recorded_inputs_json() -> None:
    p, rec = _params(), _inputs()
    for rate in RATES:
        w = rec["traces"][f"r{rate}"]["window"]
        r = p["rates"][str(rate)]
        assert (r["window_rows"], r["window_sha256"], r["sidecar_sha256"]) == (w["K"], w["trace_sha256"], w["sidecar_sha256"])
        assert r["sessions"] == rec["traces"][f"r{rate}"]["N_s"]
        assert r["full_sha256"] == rec["traces"][f"r{rate}"]["full_sha256"]
    assert p["cut_ms"] == CUT_MS


# ------------------------------------------------------------------------------------------- regen.sh
def test_dry_run_prints_the_exact_generator_invocation_and_writes_nothing(tmp_path: Path) -> None:
    out = tmp_path / "o"
    res = _regen("--dry-run", "10", str(out))
    assert res.returncode == 0, res.stdout + res.stderr
    assert not out.exists() or not any(out.iterdir())
    line = next(ln for ln in res.stdout.splitlines() if "trace w2" in ln)
    tokens = shlex.split(line)
    i = tokens.index("w2")
    assert tokens[i - 1] == "trace"
    args = dict(zip(tokens[i + 1 :: 2], tokens[i + 2 :: 2], strict=False))
    for flag, val in SHAPE_FLAGS.items():
        assert args.get(flag) == val, f"{flag}: {args.get(flag)!r} != {val!r}"
    assert args["--sessions"] == "750" and args["--rate"] == "10"
    assert "--out" in args and "--sidecar" in args


def test_default_generator_runs_without_the_dev_dependency_group(tmp_path: Path) -> None:
    """Without ``DEMO_BENCH`` the generator is ``uv run --no-dev --project <repo>/tools/demo_bench demo-bench``: the trace
    needs only the standard library, so uv must not install the dev group (pytest, ruff, mypy...) of tools/demo_bench."""
    env = {k: v for k, v in _env().items() if k != "DEMO_BENCH"}
    res = _regen("--dry-run", "10", str(tmp_path / "o"), env=env)
    assert res.returncode == 0, res.stdout + res.stderr
    tokens = shlex.split(next(ln for ln in res.stdout.splitlines() if "trace w2" in ln))
    head = tokens[: tokens.index("demo-bench")]
    assert head[:2] == ["uv", "run"], head
    assert "--no-dev" in head, f"uv run installs the dev group of tools/demo_bench: {head}"
    assert head[head.index("--project") + 1] == str(REPO_ROOT / "tools" / "demo_bench"), head


@needs_inputs
def test_dry_run_matches_the_recorded_command_flags() -> None:
    rec = _inputs()
    for rate in RATES:
        res = _regen("--dry-run", str(rate), "/nonexistent-qa-out")
        assert res.returncode == 0, res.stdout + res.stderr
        got = shlex.split(next(ln for ln in res.stdout.splitlines() if "trace w2" in ln))
        want = shlex.split(rec["traces"][f"r{rate}"]["command"])

        def flags(toks: list[str]) -> dict[str, str]:
            j = toks.index("w2")
            d = dict(zip(toks[j + 1 :: 2], toks[j + 2 :: 2], strict=False))
            d.pop("--out", None)
            d.pop("--sidecar", None)
            return d

        assert flags(got) == flags(want), f"r{rate}"


def test_regen_r10_reproduces_recorded_window_and_sidecar_sha256(tmp_path: Path) -> None:
    res = _regen("10", str(tmp_path))
    assert res.returncode == 0, res.stdout + res.stderr
    k, trace_sha, side_sha = RECORDED[10]
    trace, side = tmp_path / "w3-16k-r10.jsonl", tmp_path / "w3-16k-r10.side.jsonl"
    assert _sha(trace) == trace_sha
    assert _sha(side) == side_sha
    assert len(trace.read_bytes().splitlines()) == len(side.read_bytes().splitlines()) == k
    sums = (tmp_path / "SHA256SUMS").read_text().splitlines()
    assert sums == [f"{trace_sha}  w3-16k-r10.jsonl", f"{side_sha}  w3-16k-r10.side.jsonl"]


@needs_inputs
def test_regen_r10_matches_the_inputs_json_and_its_sha256sums(tmp_path: Path) -> None:
    res = _regen("10", str(tmp_path))
    assert res.returncode == 0, res.stdout + res.stderr
    w = _inputs()["traces"]["r10"]["window"]
    assert _sha(tmp_path / "w3-16k-r10.jsonl") == w["trace_sha256"]
    assert _sha(tmp_path / "w3-16k-r10.side.jsonl") == w["sidecar_sha256"]
    recorded_sums = INPUTS_JSON.parent / "w3-16k-r10-906debd4" / "SHA256SUMS"
    if recorded_sums.exists():
        assert (tmp_path / "SHA256SUMS").read_text() == recorded_sums.read_text()


def test_regen_window_obeys_the_cut_rule(tmp_path: Path) -> None:
    res = _regen("10", str(tmp_path))
    assert res.returncode == 0, res.stdout + res.stderr
    rows = [json.loads(ln) for ln in (tmp_path / "w3-16k-r10.jsonl").read_text().splitlines()]
    ts = [r["timestamp"] for r in rows]
    assert ts == sorted(ts) and ts[-1] < CUT_MS
    assert ts[-1] == 614957  # last_row_ms recorded for r10
    assert max(r["input_length"] + r["output_length"] for r in rows) == 16372
    assert {r["output_length"] for r in rows} == {500}
    side = [json.loads(ln) for ln in (tmp_path / "w3-16k-r10.side.jsonl").read_text().splitlines()]
    assert [s["line"] for s in side] == list(range(len(rows)))


@pytest.mark.parametrize("rate", [25, 32, 50])
def test_regen_other_rates_reproduce_recorded_hashes(tmp_path: Path, rate: int) -> None:
    res = _regen(str(rate), str(tmp_path))
    assert res.returncode == 0, res.stdout + res.stderr
    k, trace_sha, side_sha = RECORDED[rate]
    assert _sha(tmp_path / f"w3-16k-r{rate}.jsonl") == trace_sha
    assert _sha(tmp_path / f"w3-16k-r{rate}.side.jsonl") == side_sha


def test_regen_is_deterministic(tmp_path: Path) -> None:
    a, b = tmp_path / "a", tmp_path / "b"
    assert _regen("10", str(a)).returncode == 0
    assert _regen("10", str(b)).returncode == 0
    assert (a / "SHA256SUMS").read_text() == (b / "SHA256SUMS").read_text()


@pytest.mark.parametrize("bad", ["7", "0", "abc", "-5", ""])
def test_regen_refuses_a_rate_outside_the_params(tmp_path: Path, bad: str) -> None:
    res = _regen(bad, str(tmp_path / "o"))
    assert res.returncode != 0
    assert not (tmp_path / "o").exists() or not any((tmp_path / "o").iterdir())


def test_regen_without_arguments_is_a_usage_error() -> None:
    res = _regen()
    assert res.returncode == 2, res.stdout + res.stderr
    assert "usage" in (res.stdout + res.stderr).lower()


def test_regen_generator_failure_is_propagated_and_leaves_no_partial_output(tmp_path: Path) -> None:
    env = _env()
    env["DEMO_BENCH"] = "false"
    res = _regen("10", str(tmp_path / "o"), env=env)
    assert res.returncode != 0
    assert not list((tmp_path / "o").glob("*.jsonl")) if (tmp_path / "o").exists() else True


def test_regen_is_offline_no_network_or_aws_tools(tmp_path: Path) -> None:
    text = REGEN.read_text()
    for tool in ("aws ", "curl", "wget", "kubectl", "s3://"):
        assert tool not in text, f"regen.sh must stay offline ({tool!r})"


# ------------------------------------------------------------------------------------------- docs
def test_readme_labels_the_workload_n1_and_states_its_provenance() -> None:
    assert README.is_file()
    text = README.read_text()
    low = text.lower()
    assert "n=1" in low
    # The "not a result of record" disclaimer was dropped; the README states the factual provenance instead.
    assert "n=1 per cell and rate" in low
    assert "never pooled" in low or "never pool" in low
    assert "w2" in low  # a different workload from the W2 of record


def test_readme_documents_the_regeneration_command_and_workload_facts() -> None:
    text = README.read_text()
    assert "regen.sh" in text
    for needle in ("demo-bench trace w2", "615", "75 x rate", "S0", "S7", "1.0 s", "2.0 s", "25 ms", "10", "25", "32", "50", "inconclusive"):
        assert needle in text, f"README missing {needle!r}"
    for needle in ("11 turns", "2048", "5632", "1024", "15872", "16384", "500"):
        assert needle in text.replace(",", ""), f"README missing {needle!r}"


def test_folder_has_no_account_bucket_or_region_literal() -> None:
    assert W3.is_dir()
    for f in sorted(W3.rglob("*")):
        if not f.is_file():
            continue
        text = f.read_text()
        rel = f.relative_to(REPO_ROOT)
        assert not ACCOUNT_RE.search(text), f"{rel}: account id"
        assert not REGION_RE.search(text), f"{rel}: region literal"
        assert not SHORT_RE.search(text), f"{rel}: short region code"
        assert "s3://" not in text, f"{rel}: bucket URI"
        assert "kubecon" + "-dynamo-" not in text and "dynamo-sample-" not in text, f"{rel}: bucket name"
