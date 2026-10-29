# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Tests for ``application.sweep.build_sweep_data`` -- the stdlib-only data layer of ``plot sweep``.

Contract (the JSON written as ``sweep_data.json`` is exactly this dict, ``json.dumps(sort_keys=True,
indent=2)``; it holds no paths or timestamps so identical inputs give identical bytes):

    {"schema_version": 1, "setup": str,
     "concurrencies": [ascending ints present in at least one valid run],
     "scenarios": [sorted scenario ids with at least one valid run],
     "colors": {scenario: "#rrggbb"}              # fixed per scenario id, distinct
     "series": {S: {"ttft_p50_ms": [...], "ttft_p90_ms": [...], "itl_p50_ms": [...],
                    "itl_p90_ms": [...], "output_tok_s": [...], "n": [...]}},   # one entry per concurrency, None = gap
     "pairs": ["S0:S1", ...],
     "speedup": {"S0:S1": [baseline ttft_p50 / candidate ttft_p50 | None per concurrency]},
     "missing": [{"scenario": S, "concurrency": c}, ...],      # grid points with n == 0
     "excluded": [{"scenario", "concurrency", "rep", "reason"}, ...]}   # invalid runs

Runs are discovered recursively (any directory depth) from ``run.json`` files with
``mode == "closed-loop"`` and an integer ``concurrency``; other runs are ignored. The aiperf export
is ``<dir of run.json>/aiperf/profile_export_aiperf.json``. Several reps of one point aggregate by
median. Missing data is ``None`` -- never 0, never interpolated.
"""

from __future__ import annotations

import json
import shutil
import statistics
from pathlib import Path
from typing import Any

import pytest

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
SWEEP = FIXTURES / "sweep"
CONC = [1, 4, 8, 16, 32, 64, 128]
SCENARIOS = ["S0", "S1", "S2", "S4"]
SETUP = "1x p6-b200 · Qwen3.8-27B-FP8 · Dynamo 1.5.1 · n=1"


def _build(runs: Path, pairs: list[tuple[str, str]] | None = None, setup: str = SETUP) -> dict[str, Any]:
    from demo_bench.application.sweep import build_sweep_data

    return build_sweep_data(runs, setup=setup, pairs=pairs or [])


def _export(scenario: str, c: int, rep: int = 1, root: Path = SWEEP) -> dict[str, Any]:
    p = root / f"c{c}" / "ceiling" / scenario / f"rep{rep}" / "aiperf" / "profile_export_aiperf.json"
    return json.loads(p.read_text())  # type: ignore[no-any-return]


def _copy(tmp_path: Path) -> Path:
    dst = tmp_path / "runs"
    shutil.copytree(SWEEP, dst)
    return dst


def _run_dir(root: Path, scenario: str, c: int, rep: int = 1) -> Path:
    return root / f"c{c}" / "ceiling" / scenario / f"rep{rep}"


# ============================================================================ happy path
def test_full_grid_values_match_the_aiperf_exports() -> None:
    d = _build(SWEEP)
    assert d["schema_version"] == 1 and d["setup"] == SETUP
    assert d["concurrencies"] == CONC and d["scenarios"] == SCENARIOS
    for s in SCENARIOS:
        ser = d["series"][s]
        for i, c in enumerate(CONC):
            e = _export(s, c)
            assert ser["ttft_p50_ms"][i] == e["time_to_first_token"]["p50"]
            assert ser["ttft_p90_ms"][i] == e["time_to_first_token"]["p90"]
            assert ser["itl_p50_ms"][i] == e["inter_token_latency"]["p50"]
            assert ser["itl_p90_ms"][i] == e["inter_token_latency"]["p90"]
            assert ser["output_tok_s"][i] == e["output_token_throughput"]["avg"]
            assert ser["n"][i] == 1
    assert d["missing"] == [] and d["excluded"] == []


def test_openloop_runs_are_ignored() -> None:
    d = _build(SWEEP)
    # the fixture holds a fixed-schedule S0 run with ttft p50 99999 ms: it must not leak in
    assert 99999.0 not in [v for s in d["series"].values() for v in s["ttft_p50_ms"]]
    assert d["series"]["S0"]["n"] == [1] * 7


def test_json_serialisable_and_path_free() -> None:
    d = _build(SWEEP)
    text = json.dumps(d, sort_keys=True, ensure_ascii=False)
    assert str(SWEEP) not in text and "rep1" not in text and "/" not in text


def test_colors_fixed_per_scenario_and_distinct(tmp_path: Path) -> None:
    full = _build(SWEEP)
    sub = _copy(tmp_path)
    for s in ("S2", "S4"):
        for c in CONC:
            shutil.rmtree(_run_dir(sub, s, c))
    part = _build(sub)
    assert part["scenarios"] == ["S0", "S1"]
    assert len(set(full["colors"].values())) == len(full["colors"]) == 4
    assert all(len(v) == 7 and v.startswith("#") for v in full["colors"].values())
    assert part["colors"]["S0"] == full["colors"]["S0"] and part["colors"]["S1"] == full["colors"]["S1"]


def test_deterministic_and_independent_of_directory_location(tmp_path: Path) -> None:
    a, b = tmp_path / "x", tmp_path / "deeper" / "y"
    shutil.copytree(SWEEP, a)
    shutil.copytree(SWEEP, b)
    assert json.dumps(_build(a), sort_keys=True) == json.dumps(_build(b), sort_keys=True)
    assert _build(a) == _build(SWEEP)


@pytest.mark.parametrize("layout", ["deeper", "flat"])
def test_layout_agnostic_discovery(tmp_path: Path, layout: str) -> None:
    """S3 layouts differ (`<RUN_ID>/ceiling/<S>/rep<k>/`, `matrix/<S>/rep<k>/`): only run.json matters."""
    dst = tmp_path / "other"
    for s in SCENARIOS:
        for c in CONC:
            src = _run_dir(SWEEP, s, c)
            target = (dst / "RUN-42" / f"c{c}" / "ceiling" / s / "rep1" if layout == "deeper"
                      else dst / f"{s}-c{c}")
            shutil.copytree(src, target)
    assert _build(dst) == _build(SWEEP)


def test_real_aiperf_0_13_0_export_is_readable(tmp_path: Path) -> None:
    """Verbatim export from a real run (trackc/ab-run/off-2): real key names incl. p90."""
    run = tmp_path / "r" / "S0-c4"
    (run / "aiperf").mkdir(parents=True)
    shutil.copy(FIXTURES / "real_profile_export_aiperf.json", run / "aiperf" / "profile_export_aiperf.json")
    meta = json.loads((_run_dir(SWEEP, "S0", 4) / "run.json").read_text())
    (run / "run.json").write_text(json.dumps(meta))
    d = _build(tmp_path / "r")
    assert d["concurrencies"] == [4] and d["scenarios"] == ["S0"]
    s = d["series"]["S0"]
    assert s["ttft_p50_ms"] == [15161.5468955] and s["ttft_p90_ms"] == [22588.138178999998]
    assert s["itl_p50_ms"] == [236.43389564516127] and s["itl_p90_ms"] == [491.2805953225806]
    assert s["output_tok_s"] == [5.533883854577091]


# ============================================================================ gaps, never interpolated
def test_missing_run_is_a_gap_not_zero_not_interpolated(tmp_path: Path) -> None:
    sub = _copy(tmp_path)
    shutil.rmtree(_run_dir(sub, "S4", 64))
    shutil.rmtree(_run_dir(sub, "S4", 128))
    d = _build(sub)
    s4 = d["series"]["S4"]
    for key in ("ttft_p50_ms", "ttft_p90_ms", "itl_p50_ms", "itl_p90_ms", "output_tok_s"):
        assert s4[key][5:] == [None, None], key
        assert None not in s4[key][:5]
    assert s4["n"][5:] == [0, 0]
    assert {"scenario": "S4", "concurrency": 64} in d["missing"]
    assert {"scenario": "S4", "concurrency": 128} in d["missing"]
    assert d["concurrencies"] == CONC  # other scenarios still define the axis


def test_invalid_runs_are_excluded_and_reported(tmp_path: Path) -> None:
    sub = _copy(tmp_path)
    bad_rc = _run_dir(sub, "S1", 16)
    meta = json.loads((bad_rc / "run.json").read_text())
    meta["aiperf"]["rc"] = 1
    (bad_rc / "run.json").write_text(json.dumps(meta))
    bad_restart = _run_dir(sub, "S2", 8)
    meta = json.loads((bad_restart / "run.json").read_text())
    meta["restarted_pods"] = ["q38-decode-0"]
    (bad_restart / "run.json").write_text(json.dumps(meta))
    (_run_dir(sub, "S0", 32) / "aiperf" / "profile_export_aiperf.json").unlink()
    d = _build(sub)
    assert d["series"]["S1"]["ttft_p50_ms"][3] is None
    assert d["series"]["S2"]["ttft_p50_ms"][2] is None
    assert d["series"]["S0"]["ttft_p50_ms"][4] is None
    excl = {(e["scenario"], e["concurrency"]): e for e in d["excluded"]}
    assert set(excl) == {("S1", 16), ("S2", 8), ("S0", 32)}
    assert all(e["reason"] and e["rep"] == 1 for e in excl.values())
    for key in (("S1", 16), ("S2", 8), ("S0", 32)):
        assert {"scenario": key[0], "concurrency": key[1]} in d["missing"]


def test_missing_metric_in_a_valid_run_is_none_for_that_metric_only(tmp_path: Path) -> None:
    sub = _copy(tmp_path)
    p = _run_dir(sub, "S1", 8) / "aiperf" / "profile_export_aiperf.json"
    e = json.loads(p.read_text())
    del e["inter_token_latency"]
    p.write_text(json.dumps(e))
    d = _build(sub)
    s1 = d["series"]["S1"]
    assert s1["itl_p50_ms"][2] is None and s1["itl_p90_ms"][2] is None
    assert s1["ttft_p50_ms"][2] == _export("S1", 8)["time_to_first_token"]["p50"]
    assert s1["n"][2] == 1 and d["excluded"] == []


def test_reps_aggregate_by_median(tmp_path: Path) -> None:
    sub = _copy(tmp_path)
    base = _run_dir(sub, "S0", 4)
    for rep, ttft in ((1, 300.0), (2, 100.0), (3, 200.0)):
        dst = _run_dir(sub, "S0", 4, rep)
        if rep != 1:
            shutil.copytree(base, dst)
        meta = json.loads((dst / "run.json").read_text())
        meta["rep"] = rep
        (dst / "run.json").write_text(json.dumps(meta))
        p = dst / "aiperf" / "profile_export_aiperf.json"
        e = json.loads(p.read_text())
        e["time_to_first_token"]["p50"] = ttft
        p.write_text(json.dumps(e))
    d = _build(sub)
    i = CONC.index(4)
    assert d["series"]["S0"]["ttft_p50_ms"][i] == statistics.median([300.0, 100.0, 200.0]) == 200.0
    assert d["series"]["S0"]["n"][i] == 3
    assert d["series"]["S0"]["n"][0] == 1


# ============================================================================ speedup
def test_speedup_is_baseline_over_candidate_ttft_p50() -> None:
    d = _build(SWEEP, [("S0", "S1"), ("S0", "S2"), ("S0", "S4")])
    assert d["pairs"] == ["S0:S1", "S0:S2", "S0:S4"]
    for cand in ("S1", "S2", "S4"):
        got = d["speedup"][f"S0:{cand}"]
        for i, c in enumerate(CONC):
            want = _export("S0", c)["time_to_first_token"]["p50"] / _export(cand, c)["time_to_first_token"]["p50"]
            assert got[i] == pytest.approx(want, rel=1e-9)
    assert d["speedup"]["S0:S1"][0] == pytest.approx(2.0)  # S1 is half of S0 by construction


def test_speedup_gap_when_either_side_is_missing(tmp_path: Path) -> None:
    sub = _copy(tmp_path)
    shutil.rmtree(_run_dir(sub, "S1", 16))
    shutil.rmtree(_run_dir(sub, "S0", 128))
    d = _build(sub, [("S0", "S1"), ("S0", "S2")])
    s01, s02 = d["speedup"]["S0:S1"], d["speedup"]["S0:S2"]
    assert s01[CONC.index(16)] is None and s01[CONC.index(128)] is None
    assert s02[CONC.index(128)] is None and s02[CONC.index(16)] is not None
    assert all(v is None or v > 0 for v in s01 + s02)


def test_no_pairs_means_empty_speedup() -> None:
    d = _build(SWEEP)
    assert d["pairs"] == [] and d["speedup"] == {}


# ============================================================================ errors
def test_blank_setup_is_an_error() -> None:
    from demo_bench.application.sweep import SweepError

    for setup in ("", "   ", "\n"):
        with pytest.raises(SweepError):
            _build(SWEEP, setup=setup)
    assert _build(SWEEP)["setup"] == SETUP  # positive control


def test_no_closed_loop_runs_is_an_error(tmp_path: Path) -> None:
    from demo_bench.application.sweep import SweepError

    assert _build(SWEEP)["scenarios"] == SCENARIOS  # positive control
    only_open = tmp_path / "open"
    shutil.copytree(SWEEP / "openloop", only_open)
    with pytest.raises(SweepError):
        _build(only_open)
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(SweepError):
        _build(empty)
    with pytest.raises(SweepError):
        _build(tmp_path / "does-not-exist")


@pytest.mark.parametrize("pairs", [[("S0", "S0")], [("S0", "S1"), ("S0", "S1")]])
def test_invalid_pairs_are_errors(pairs: list[tuple[str, str]]) -> None:
    from demo_bench.application.sweep import SweepError

    assert _build(SWEEP, [("S0", "S1")])["pairs"] == ["S0:S1"]  # positive control
    with pytest.raises(SweepError):
        _build(SWEEP, pairs)


def test_sweep_error_is_a_value_error() -> None:
    from demo_bench.application.sweep import SweepError

    assert issubclass(SweepError, ValueError)


# ============================================================================ P-26: pairs without a valid run
def _warnings(caplog: pytest.LogCaptureFixture) -> str:
    return " ".join(
        f"{r.getMessage()} {r.__dict__}" for r in caplog.records if r.levelname == "WARNING"
    )


@pytest.mark.parametrize("pair", [("S0", "S3"), ("S3", "S0"), ("S0", "S9"), ("S9", "S0")])
def test_pair_without_any_run_is_dropped_with_a_warning(
    pair: tuple[str, str], caplog: pytest.LogCaptureFixture
) -> None:
    """Supersedes the old 'no valid sweep runs for [...]' error (P-26): warn, drop, keep the rest."""
    name = f"{pair[0]}:{pair[1]}"
    with caplog.at_level("WARNING"):
        d = _build(SWEEP, [("S0", "S1"), pair, ("S0", "S2")])
    assert d["pairs"] == ["S0:S1", "S0:S2"]
    assert sorted(d["speedup"]) == ["S0:S1", "S0:S2"]
    assert name in _warnings(caplog)


def test_pair_whose_runs_are_all_invalid_is_dropped(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    sub = _copy(tmp_path)
    for c in CONC:
        meta_path = _run_dir(sub, "S1", c) / "run.json"
        meta = json.loads(meta_path.read_text())
        meta["aiperf"]["rc"] = 1
        meta_path.write_text(json.dumps(meta))
    with caplog.at_level("WARNING"):
        d = _build(sub, [("S0", "S1"), ("S0", "S2")])
    assert d["pairs"] == ["S0:S2"] and "S1" not in d["scenarios"]
    assert len(d["excluded"]) == len(CONC)
    assert "S0:S1" in _warnings(caplog)


def test_every_pair_dropped_leaves_no_speedup_and_no_error() -> None:
    d = _build(SWEEP, [("S0", "S3"), ("S9", "S1")])
    assert d["pairs"] == [] and d["speedup"] == {}
    assert d["scenarios"] == SCENARIOS


def test_dropped_pair_does_not_change_the_surviving_data() -> None:
    clean = _build(SWEEP, [("S0", "S1")])
    dropped = _build(SWEEP, [("S0", "S3"), ("S0", "S1")])
    assert dropped == clean


def test_pair_with_runs_emits_no_pair_warning(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level("WARNING"):
        _build(SWEEP, [("S0", "S1")])
    assert "S0:S1" not in _warnings(caplog)
