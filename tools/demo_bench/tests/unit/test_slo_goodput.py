# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""RED contract for SLO goodput from aiperf raw exports (Flash tooling, item d; plan 4.5).

Module ``demo_bench.application.slo_goodput`` (stdlib only):

* ``load_samples(profile_export: Path, sidecar: list[dict]) -> list[Sample]``: the profiling rows
  of aiperf's per-request ``profile_export.jsonl`` (``metadata.benchmark_phase == "profiling"``;
  warm-up never counts) sorted by ``metadata.request_start_ns`` pair 1:1 with the W2 sidecar in
  line order (the same positional pairing as ``application.validity``; fewer rows than sidecar
  entries are accepted, more raise ``ValueError``). ``Sample`` has ``session``, ``turn``,
  ``klass`` (``entry`` / ``continuation`` / ``cold_ingestion``), ``input_length``, ``ttft_s``
  (``metrics.time_to_first_token.value`` ms / 1000, ``None`` if absent), ``itl_ms``
  (``metrics.inter_token_latency.value``, ``None`` if absent) and ``error`` (row has ``error``).
* ``SloThresholds(*, cold_prefill_tok_s, ttft_continuation_s=2.0, itl_ms=25.0, cold_factor=1.4)``.
* ``slo_met(sample, thresholds) -> bool``: an errored sample, or one without TTFT or ITL, misses.
  ``continuation``: ``ttft_s <= ttft_continuation_s`` and ``itl_ms <= itl_ms``. ``cold_ingestion``:
  ``ttft_s <= cold_factor * input_length / cold_prefill_tok_s`` and ``itl_ms <= itl_ms``.
  ``entry`` samples are the populate phase: never judged.
* ``evaluate(samples, thresholds, *, window_s) -> SloResult``: ``goodput`` (SLO-met turns per
  second = met / ``window_s``, continuation + cold), ``classes`` = ``{"continuation": {"n",
  "met", "attainment"}, "cold_ingestion": {...}}`` (attainment ``None`` when n is 0),
  ``excluded_entry`` (count of entry samples), ``attainment`` (met / n over both classes).
* ``sensitivity_grid(samples, thresholds, *, window_s, itl_ms_values, ttft_s_values) ->
  list[dict]``: one cell per (itl, ttft) with ``itl_ms``, ``ttft_continuation_s``, ``goodput``,
  ``attainment_continuation``, ``attainment_cold`` (cold rule unchanged; ITL applies to both).
* ``bootstrap_goodput_ci(samples, thresholds, *, window_s, n_boot=1000, seed=0, alpha=0.05)
  -> tuple[float, float]``: percentile bootstrap of the goodput where the resampling unit is the
  SESSION (all of a session's samples move together; each cold ingestion is its own unit).
  Deterministic by seed.
* ``calibration(samples, thresholds, *, band=(0.4, 0.8)) -> dict``: ``attainment`` (overall) and
  ``in_band`` (band inclusive), for the S0 pilot calibration.
* CLI ``demo-bench slo-goodput --profile-export P --sidecar P --cold-prefill-tok-s X
  [--window-s X] [--ttft-s 2] [--itl-ms 25] [--cold-factor 1.4] [--itl-grid 15,25,50]
  [--ttft-grid 1,2,4] [--n-boot 1000] [--seed 0] [--alpha 0.05] [--accept-band LO,HI] [--out P]``:
  prints ``thresholds``, ``window_s``, ``classes``, ``excluded_entry``, ``goodput``,
  ``goodput_ci`` ([lo, hi]), ``sensitivity`` (grid cells) and, with ``--accept-band``,
  ``calibration``. Default ``window_s`` = last request end - first request start of the
  profiling rows. Exit 0; 1 when ``--accept-band`` is given and the attainment is outside it;
  2 for invalid input (missing file/flag, unpaired rows, non-positive numbers).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

NS = 1_000_000_000

# (class, session, turn, input_length, ttft_ms, itl_ms)
Spec = tuple[str, int | None, int | None, int, float | None, float | None]


def _side(specs: list[Spec]) -> list[dict[str, Any]]:
    return [{"line": i, "session": s, "turn": t, "class": c, "input_length": isl}
            for i, (c, s, t, isl, _, _) in enumerate(specs)]


def _export(tmp_path: Path, specs: list[Spec], *, errors: set[int] = frozenset(),  # type: ignore[assignment]
            shuffle: bool = False, warmup: int = 0) -> Path:
    lines: list[str] = []
    for w in range(warmup):
        lines.append(json.dumps({"metadata": {"benchmark_phase": "warmup",
                     "request_start_ns": w * NS, "request_end_ns": w * NS + NS},
                     "metrics": {"time_to_first_token": {"value": 1.0, "unit": "ms"}}}))
    rows = []
    for i, (_, _, _, _, ttft, itl) in enumerate(specs):
        start = (100 + i) * NS
        metrics: dict[str, Any] = {}
        if ttft is not None:
            metrics["time_to_first_token"] = {"value": ttft, "unit": "ms"}
        if itl is not None:
            metrics["inter_token_latency"] = {"value": itl, "unit": "ms"}
        row: dict[str, Any] = {"metadata": {"benchmark_phase": "profiling",
                               "request_start_ns": start, "request_end_ns": start + NS},
                               "metrics": metrics}
        if i in errors:
            row["error"] = {"code": 500, "message": "boom"}
        rows.append(json.dumps(row))
    if shuffle:
        rows.reverse()
    path = tmp_path / "profile_export.jsonl"
    path.write_text("\n".join([*lines, *rows]) + "\n")
    return path


def _samples(tmp_path: Path, specs: list[Spec], **kw: Any) -> list[Any]:
    from demo_bench.application.slo_goodput import load_samples

    return load_samples(_export(tmp_path, specs, **kw), _side(specs))


def _thr(**kw: Any) -> Any:
    from demo_bench.application.slo_goodput import SloThresholds

    return SloThresholds(**{"cold_prefill_tok_s": 50000.0, **kw})


# ------------------------------------------------------------------ pairing
def test_load_pairs_by_start_time_not_file_order_and_skips_warmup(tmp_path: Path) -> None:
    specs: list[Spec] = [("entry", 0, 0, 65536, 9000.0, 10.0),
                         ("continuation", 0, 1, 69632, 500.0, 12.0),
                         ("cold_ingestion", None, None, 100000, 2000.0, 14.0)]
    s = _samples(tmp_path, specs, shuffle=True, warmup=2)
    assert [(x.klass, x.session, x.turn, x.input_length) for x in s] == [
        ("entry", 0, 0, 65536), ("continuation", 0, 1, 69632), ("cold_ingestion", None, None, 100000)]
    assert [x.ttft_s for x in s] == [9.0, 0.5, 2.0]
    assert [x.itl_ms for x in s] == [10.0, 12.0, 14.0]
    assert [x.error for x in s] == [False] * 3


def test_more_profiling_rows_than_sidecar_entries_is_refused(tmp_path: Path) -> None:
    from demo_bench.application.slo_goodput import load_samples

    specs: list[Spec] = [("continuation", 0, 1, 1, 1.0, 1.0)] * 3
    path = _export(tmp_path, specs)
    with pytest.raises(ValueError):
        load_samples(path, _side(specs)[:2])


def test_fewer_rows_than_sidecar_entries_is_accepted(tmp_path: Path) -> None:
    from demo_bench.application.slo_goodput import load_samples

    specs: list[Spec] = [("continuation", 0, 1, 1, 1.0, 1.0)] * 3
    assert len(load_samples(_export(tmp_path, specs), _side(specs) + _side(specs))) == 3


def test_load_marks_errors_and_missing_metrics(tmp_path: Path) -> None:
    specs: list[Spec] = [("continuation", 0, 1, 1, 1.0, 1.0), ("continuation", 1, 1, 1, None, 1.0),
                         ("continuation", 2, 1, 1, 1.0, None)]
    s = _samples(tmp_path, specs, errors={0})
    assert [x.error for x in s] == [True, False, False]
    assert s[1].ttft_s is None and s[2].itl_ms is None


# ------------------------------------------------------------------ the SLO rule
@pytest.mark.parametrize(
    ("ttft_ms", "itl_ms", "met"),
    [(1500.0, 20.0, True), (2000.0, 25.0, True), (2001.0, 25.0, False), (1500.0, 25.1, False),
     (None, 10.0, False), (500.0, None, False)],
)
def test_continuation_rule(tmp_path: Path, ttft_ms: float | None, itl_ms: float | None,
                           met: bool) -> None:
    from demo_bench.application.slo_goodput import slo_met

    [s] = _samples(tmp_path, [("continuation", 0, 1, 70000, ttft_ms, itl_ms)])
    assert slo_met(s, _thr()) is met


@pytest.mark.parametrize(("ttft_ms", "met"), [(2799.0, True), (2801.0, False)])
def test_cold_rule_scales_with_isl_over_the_calibrated_prefill_rate(
    tmp_path: Path, ttft_ms: float, met: bool
) -> None:
    from demo_bench.application.slo_goodput import slo_met

    [s] = _samples(tmp_path, [("cold_ingestion", None, None, 100000, ttft_ms, 10.0)])
    assert slo_met(s, _thr()) is met  # 1.4 * 100000 / 50000 = 2.8 s
    assert slo_met(s, _thr(cold_factor=2.0)) is True
    assert slo_met(s, _thr(cold_prefill_tok_s=100000.0)) is (ttft_ms <= 1399.0)


def test_errored_sample_misses_even_with_good_latencies(tmp_path: Path) -> None:
    from demo_bench.application.slo_goodput import slo_met

    [s] = _samples(tmp_path, [("continuation", 0, 1, 1, 1.0, 1.0)], errors={0})
    assert slo_met(s, _thr()) is False


def test_threshold_defaults_and_validation() -> None:
    t = _thr()
    assert (t.ttft_continuation_s, t.itl_ms, t.cold_factor) == (2.0, 25.0, 1.4)
    for bad in ({"cold_prefill_tok_s": 0.0}, {"itl_ms": 0.0}, {"ttft_continuation_s": -1.0},
                {"cold_factor": 0.0}):
        with pytest.raises(ValueError):
            _thr(**bad)


# ------------------------------------------------------------------ goodput
MIX: list[Spec] = [
    ("entry", 0, 0, 65536, 30000.0, 50.0),
    ("continuation", 0, 1, 69632, 500.0, 10.0),     # met
    ("continuation", 0, 2, 73728, 3000.0, 10.0),    # ttft miss
    ("continuation", 1, 1, 69632, 900.0, 40.0),     # itl miss
    ("continuation", 1, 2, 73728, 1000.0, 20.0),    # met
    ("cold_ingestion", None, None, 100000, 2500.0, 20.0),  # met (limit 2.8 s)
    ("cold_ingestion", None, None, 150000, 4300.0, 20.0),  # miss (limit 4.2 s)
]


def test_evaluate_counts_classes_and_excludes_entry(tmp_path: Path) -> None:
    from demo_bench.application.slo_goodput import evaluate

    r = evaluate(_samples(tmp_path, MIX), _thr(), window_s=10.0)
    assert r.excluded_entry == 1
    assert r.classes["continuation"] == {"n": 4, "met": 2, "attainment": 0.5}
    assert r.classes["cold_ingestion"] == {"n": 2, "met": 1, "attainment": 0.5}
    assert r.goodput == pytest.approx(3 / 10.0)
    assert r.attainment == pytest.approx(3 / 6)


def test_evaluate_with_an_empty_class_reports_none(tmp_path: Path) -> None:
    from demo_bench.application.slo_goodput import evaluate

    r = evaluate(_samples(tmp_path, MIX[:5]), _thr(), window_s=4.0)
    assert r.classes["cold_ingestion"] == {"n": 0, "met": 0, "attainment": None}


def test_evaluate_rejects_a_bad_window(tmp_path: Path) -> None:
    from demo_bench.application.slo_goodput import evaluate

    with pytest.raises(ValueError):
        evaluate(_samples(tmp_path, MIX), _thr(), window_s=0.0)


# ------------------------------------------------------------------ sensitivity
GRID_SPECS: list[Spec] = [
    ("continuation", 0, 1, 1, 500.0, 10.0), ("continuation", 1, 1, 1, 1500.0, 20.0),
    ("continuation", 2, 1, 1, 3000.0, 40.0), ("continuation", 3, 1, 1, 5000.0, 60.0),
]


def test_sensitivity_grid_cells(tmp_path: Path) -> None:
    from demo_bench.application.slo_goodput import sensitivity_grid

    cells = sensitivity_grid(_samples(tmp_path, GRID_SPECS), _thr(), window_s=10.0,
                             itl_ms_values=(15, 25, 50), ttft_s_values=(1, 2, 4))
    assert len(cells) == 9
    got = {(c["itl_ms"], c["ttft_continuation_s"]): c for c in cells}
    expected = {(15, 1): 1, (15, 2): 1, (15, 4): 1, (25, 1): 1, (25, 2): 2, (25, 4): 2,
                (50, 1): 1, (50, 2): 2, (50, 4): 3}
    for key, met in expected.items():
        assert got[key]["goodput"] == pytest.approx(met / 10.0), key
        assert got[key]["attainment_continuation"] == pytest.approx(met / 4), key
        assert got[key]["attainment_cold"] is None


def test_sensitivity_is_monotone_and_applies_itl_to_cold(tmp_path: Path) -> None:
    from demo_bench.application.slo_goodput import sensitivity_grid

    specs = [*GRID_SPECS, ("cold_ingestion", None, None, 100000, 2000.0, 30.0)]
    cells = sensitivity_grid(_samples(tmp_path, specs), _thr(), window_s=10.0,
                             itl_ms_values=(15, 25, 50), ttft_s_values=(1, 2, 4))
    by = {(c["itl_ms"], c["ttft_continuation_s"]): c for c in cells}
    assert by[(25, 2)]["attainment_cold"] == 0.0 and by[(50, 2)]["attainment_cold"] == 1.0
    for ttft in (1, 2, 4):
        assert by[(15, ttft)]["goodput"] <= by[(25, ttft)]["goodput"] <= by[(50, ttft)]["goodput"]
    for itl in (15, 25, 50):
        assert by[(itl, 1)]["goodput"] <= by[(itl, 2)]["goodput"] <= by[(itl, 4)]["goodput"]


# ------------------------------------------------------------------ bootstrap by session
def _session_specs(met_sessions: int, miss_sessions: int, per: int = 5) -> list[Spec]:
    out: list[Spec] = []
    for s in range(met_sessions + miss_sessions):
        ttft = 500.0 if s < met_sessions else 9000.0
        out += [("continuation", s, k, 70000, ttft, 10.0) for k in range(1, per + 1)]
    return out


def test_bootstrap_resamples_sessions_not_requests(tmp_path: Path) -> None:
    from demo_bench.application.slo_goodput import bootstrap_goodput_ci, evaluate

    samples = _samples(tmp_path, _session_specs(5, 5))
    point = evaluate(samples, _thr(), window_s=100.0).goodput
    assert point == pytest.approx(0.25)
    lo, hi = bootstrap_goodput_ci(samples, _thr(), window_s=100.0, n_boot=2000, seed=1)
    # sessions are all-or-nothing: k ~ Bin(10, 1/2) sessions met -> goodput 5k/100 in ~[0.10, 0.40];
    # a per-request bootstrap would give ~[0.19, 0.31].
    assert 0.05 <= lo <= 0.15
    assert 0.35 <= hi <= 0.45


def test_bootstrap_is_deterministic_and_seed_sensitive(tmp_path: Path) -> None:
    from demo_bench.application.slo_goodput import bootstrap_goodput_ci

    s = _samples(tmp_path, _session_specs(3, 3))
    a = bootstrap_goodput_ci(s, _thr(), window_s=50.0, n_boot=300, seed=4)
    assert a == bootstrap_goodput_ci(s, _thr(), window_s=50.0, n_boot=300, seed=4)
    lo, hi = a
    assert 0.0 <= lo <= hi <= 0.6


def test_bootstrap_identical_sessions_give_a_point_interval(tmp_path: Path) -> None:
    from demo_bench.application.slo_goodput import bootstrap_goodput_ci

    s = _samples(tmp_path, _session_specs(6, 0))
    assert bootstrap_goodput_ci(s, _thr(), window_s=60.0, n_boot=200, seed=0) == pytest.approx(
        (0.5, 0.5))


def test_bootstrap_treats_each_cold_ingestion_as_its_own_unit(tmp_path: Path) -> None:
    from demo_bench.application.slo_goodput import bootstrap_goodput_ci, evaluate

    specs = [*_session_specs(1, 1),
             *[("cold_ingestion", None, None, 100000, 1000.0, 10.0)] * 4]
    s = _samples(tmp_path, specs)
    point = evaluate(s, _thr(), window_s=10.0).goodput
    lo, hi = bootstrap_goodput_ci(s, _thr(), window_s=10.0, n_boot=500, seed=2)
    assert lo <= point <= hi
    assert hi - lo > 0.0


def test_bootstrap_validates_parameters(tmp_path: Path) -> None:
    from demo_bench.application.slo_goodput import bootstrap_goodput_ci

    s = _samples(tmp_path, _session_specs(2, 2))
    for bad in ({"n_boot": 0}, {"alpha": 0.0}, {"alpha": 1.0}, {"window_s": 0.0}):
        with pytest.raises(ValueError):
            bootstrap_goodput_ci(s, _thr(), **{"window_s": 10.0, **bad})


# ------------------------------------------------------------------ calibration
@pytest.mark.parametrize(("met_sessions", "miss_sessions", "in_band"),
                         [(5, 5, True), (9, 1, False), (3, 7, False), (4, 6, True), (8, 2, True)])
def test_calibration_band(tmp_path: Path, met_sessions: int, miss_sessions: int,
                          in_band: bool) -> None:
    from demo_bench.application.slo_goodput import calibration

    c = calibration(_samples(tmp_path, _session_specs(met_sessions, miss_sessions)), _thr())
    assert c["attainment"] == pytest.approx(met_sessions / (met_sessions + miss_sessions))
    assert c["in_band"] is in_band
    assert tuple(c["band"]) == (0.4, 0.8)


def test_calibration_custom_band(tmp_path: Path) -> None:
    from demo_bench.application.slo_goodput import calibration

    s = _samples(tmp_path, _session_specs(9, 1))
    assert calibration(s, _thr(), band=(0.5, 0.95))["in_band"] is True


# ------------------------------------------------------------------ CLI
def _cli(argv: list[str]) -> int:
    from demo_bench.cli import main

    try:
        return int(main(argv))
    except SystemExit as exc:
        return int(exc.code or 0)


def _argv(tmp_path: Path, specs: list[Spec], *extra: str) -> list[str]:
    export = _export(tmp_path, specs)
    side = tmp_path / "w2.side"
    side.write_text("".join(json.dumps(s) + "\n" for s in _side(specs)))
    return ["slo-goodput", "--profile-export", str(export), "--sidecar", str(side),
            "--cold-prefill-tok-s", "50000", "--n-boot", "200", *extra]


def test_cli_report(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    out = tmp_path / "slo.json"
    assert _cli(_argv(tmp_path, MIX, "--window-s", "10", "--out", str(out))) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["goodput"] == pytest.approx(0.3)
    assert doc["classes"]["continuation"]["met"] == 2 and doc["excluded_entry"] == 1
    assert doc["window_s"] == 10.0
    assert doc["thresholds"]["ttft_continuation_s"] == 2.0 and doc["thresholds"]["itl_ms"] == 25.0
    lo, hi = doc["goodput_ci"]
    assert lo <= doc["goodput"] <= hi
    assert len(doc["sensitivity"]) == 9
    assert "calibration" not in doc
    assert json.loads(out.read_text()) == doc


def test_cli_default_window_is_the_profiling_span(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _cli(_argv(tmp_path, MIX)) == 0
    assert json.loads(capsys.readouterr().out)["window_s"] == pytest.approx(7.0)


def test_cli_thresholds_and_grids_are_parameters(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    argv = _argv(tmp_path, MIX, "--window-s", "10", "--ttft-s", "4", "--itl-ms", "50",
                 "--itl-grid", "10,20", "--ttft-grid", "1")
    assert _cli(argv) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["thresholds"]["ttft_continuation_s"] == 4.0 and doc["thresholds"]["itl_ms"] == 50.0
    assert doc["classes"]["continuation"]["met"] == 4
    assert len(doc["sensitivity"]) == 2


def test_cli_accept_band(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert _cli(_argv(tmp_path, MIX, "--window-s", "10", "--accept-band", "0.4,0.8")) == 0
    assert json.loads(capsys.readouterr().out)["calibration"]["in_band"] is True
    assert _cli(_argv(tmp_path, MIX, "--window-s", "10", "--accept-band", "0.6,0.9")) == 1
    assert json.loads(capsys.readouterr().out)["calibration"]["in_band"] is False


def test_cli_invalid_input_exits_2(tmp_path: Path) -> None:
    argv = _argv(tmp_path, MIX)
    assert _cli(argv) == 0  # control: the valid invocation
    assert _cli([*argv, "--window-s", "0"]) == 2
    assert _cli([a for a in argv if a not in ("--cold-prefill-tok-s", "50000")]) == 2
    side = Path(argv[argv.index("--sidecar") + 1])
    side.write_text(side.read_text().splitlines()[0] + "\n")  # fewer sidecar rows than requests
    assert _cli(argv) == 2
    side.unlink()
    assert _cli(argv) == 2
