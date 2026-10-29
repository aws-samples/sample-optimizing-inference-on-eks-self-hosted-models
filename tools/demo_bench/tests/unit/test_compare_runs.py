# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests for aiperf artifact reading and ``application.compare.compare_runs``."""

from __future__ import annotations

from pathlib import Path


def _load(fixtures_dir: Path):  # type: ignore[no-untyped-def]
    from demo_bench.infrastructure.aiperf_reader import read_run_artifact

    base = read_run_artifact(fixtures_dir / "aiperf_baseline" / "profile_export_aiperf.json",
                             label="rr")
    cand = read_run_artifact(fixtures_dir / "aiperf_candidate" / "profile_export_aiperf.json",
                             label="kv")
    return base, cand


def _row(cmp, metric: str, stat: str):  # type: ignore[no-untyped-def]
    matches = [r for r in cmp.rows if r.metric == metric and r.stat == stat]
    assert len(matches) == 1, f"expected exactly one row for {metric}.{stat}"
    return matches[0]


def test_read_run_artifact_parses_stats_and_errors(fixtures_dir: Path) -> None:
    base, cand = _load(fixtures_dir)
    assert base.label == "rr" and cand.label == "kv"
    assert base.aiperf_version == "0.13.0"
    assert base.metrics["time_to_first_token"]["p50"] == 1000
    assert base.metrics["time_to_first_token"]["p99"] == 4000
    assert base.metrics["request_throughput"]["avg"] == 0.80
    assert base.error_count == 5  # sum of error_summary[].count (3 + 2)
    assert cand.error_count == 0


def test_compare_runs_table_has_percentiles_and_ratio(fixtures_dir: Path) -> None:
    from demo_bench.application.compare import compare_runs

    base, cand = _load(fixtures_dir)
    cmp = compare_runs(base, cand)
    ttft50 = _row(cmp, "time_to_first_token", "p50")
    assert (ttft50.baseline, ttft50.candidate) == (1000, 500)
    assert ttft50.ratio == 0.5  # candidate / baseline
    assert _row(cmp, "time_to_first_token", "p95").ratio == 0.5
    assert _row(cmp, "time_to_first_token", "p99").ratio == 0.25
    assert _row(cmp, "request_latency", "p50").ratio == 0.75
    assert _row(cmp, "request_throughput", "avg").ratio == 1.25
    assert not ttft50.missing


def test_compare_runs_reports_error_counts(fixtures_dir: Path) -> None:
    from demo_bench.application.compare import compare_runs

    cmp = compare_runs(*_load(fixtures_dir))
    assert cmp.baseline_errors == 5
    assert cmp.candidate_errors == 0


def test_missing_metrics_are_reported_not_zero(fixtures_dir: Path) -> None:
    from demo_bench.application.compare import compare_runs

    cmp = compare_runs(*_load(fixtures_dir))
    # candidate export has no inter_token_latency; neither export has goodput in candidate
    assert "inter_token_latency" in cmp.missing
    assert "goodput" in cmp.missing
    for stat in ("p50", "p95", "p99"):
        r = _row(cmp, "inter_token_latency", stat)
        assert r.missing is True
        assert r.baseline is not None and r.candidate is None
        assert r.ratio is None  # never 0.0 / inf
    g = _row(cmp, "goodput", "avg")
    assert g.missing and g.candidate is None and g.ratio is None


def test_present_metrics_are_not_flagged_missing(fixtures_dir: Path) -> None:
    from demo_bench.application.compare import compare_runs

    cmp = compare_runs(*_load(fixtures_dir))
    assert "time_to_first_token" not in cmp.missing
    assert "request_throughput" not in cmp.missing


def test_zero_baseline_gives_no_ratio(fixtures_dir: Path) -> None:
    from demo_bench.application.compare import compare_runs
    from demo_bench.domain.comparison import RunArtifact

    base = RunArtifact(label="a", metrics={"request_throughput": {"avg": 0.0}}, error_count=0,
                       aiperf_version="0.13.0")
    cand = RunArtifact(label="b", metrics={"request_throughput": {"avg": 1.0}}, error_count=0,
                       aiperf_version="0.13.0")
    cmp = compare_runs(base, cand, metrics=["request_throughput"])
    r = _row(cmp, "request_throughput", "avg")
    assert r.ratio is None
    assert not r.missing  # present, just not comparable


def test_markdown_table(fixtures_dir: Path) -> None:
    from demo_bench.application.compare import compare_runs

    md = compare_runs(*_load(fixtures_dir)).to_markdown()
    header = md.splitlines()[0].lower()
    for col in ("metric", "stat", "baseline", "candidate", "ratio"):
        assert col in header
    line = next(ln for ln in md.splitlines()
                if "time_to_first_token" in ln and "p50" in ln)
    assert "0.50" in line
    itl = [ln for ln in md.splitlines() if "inter_token_latency" in ln]
    assert itl and all("MISSING" in ln for ln in itl)
    assert "errors" in md.lower() and "5" in md
