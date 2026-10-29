# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Output collapse: a response whose output collapsed to <= 1 token while more were requested is an error.

Evidence (block 1, S3): a 388,620-token prompt was aborted by the engine (input longer than the KV
pool), the engine answered with ONE token and aiperf recorded a success ("OSL mismatch: got 1
tokens (requested 7155)"). ``error_summary`` stayed empty, so every validity check passed.

Contract (module ``demo_bench.application.validity``, stdlib only; the single definition):

* ``ERROR_RATE_LIMIT == 0.005`` (spec 13.5: errors <= 0.5 % of the requests stay valid).
* ``is_collapsed(observed, requested) -> bool``: True iff ``requested > 1`` and ``observed`` is a
  number ``<= 1`` (0 and 1 collapse). ``observed is None`` (metric absent) is never counted, and a
  request that asked for <= 1 token cannot collapse.
* ``exceeds_error_limit(errors, total) -> bool``: ``errors / total > ERROR_RATE_LIMIT``; with
  ``total == 0`` it is True only when ``errors > 0``. Exactly 0.5 % is still valid.
* ``count_collapsed_outputs(profile_export, requested_trace) -> CollapsedOutputs`` with fields
  ``collapsed`` and ``total``. ``profile_export`` is aiperf's ``profile_export.jsonl``;
  ``requested_trace`` is the mooncake trace the run replayed (``scheduled-trace.jsonl``), whose
  ``output_length`` is the requested OSL (aiperf sends ``ignore_eos: true``). Only
  ``benchmark_phase == "profiling"`` rows count (warm-up never does); blank lines are skipped.
  The profiling rows, ordered by ``metadata.request_start_ns``, pair 1:1 with the trace rows in
  file order. Observed tokens = ``metrics.output_sequence_length.value`` (fallback
  ``metrics.output_token_count.value``). A row that already carries ``error`` is NOT counted as
  collapsed (it is counted once, as an aiperf error) but is part of ``total``.
  More profiling rows than trace rows -> ``ValueError``.
* ``collapsed_outputs_reason(result) -> str``: human reason naming the word "collapsed", the count
  and the total.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest


def _row(i: int, observed: int | None, *, phase: str = "profiling", error: Any = None,
         metric: str = "output_sequence_length") -> dict[str, Any]:
    start = 1_760_000_000_000_000_000 + i * 1_000_000_000
    row: dict[str, Any] = {
        "metadata": {"benchmark_phase": phase, "request_start_ns": start,
                     "request_end_ns": start + 500_000_000, "was_cancelled": False},
        "metrics": {"time_to_first_token": {"value": 100.0, "unit": "ms"}},
    }
    if observed is not None:
        row["metrics"][metric] = {"value": observed, "unit": "tokens"}
    if error is not None:
        row["error"] = error
    return row


def _trace_row(i: int, osl: int) -> dict[str, Any]:
    return {"timestamp": i * 1000, "input_length": 4096, "output_length": osl, "hash_ids": [i]}


def _write(path: Path, rows: list[Any]) -> Path:
    path.write_text("".join((r if isinstance(r, str) else json.dumps(r)) + "\n" for r in rows))
    return path


def _files(tmp_path: Path, observed: Sequence[int | None], requested: Sequence[int]) -> tuple[Path, Path]:
    prof = _write(tmp_path / "profile_export.jsonl", [_row(i, o) for i, o in enumerate(observed)])
    trace = _write(tmp_path / "scheduled-trace.jsonl", [_trace_row(i, r) for i, r in enumerate(requested)])
    return prof, trace


# ----------------------------------------------------------------------------- the predicate
@pytest.mark.parametrize(
    ("observed", "requested", "expected"),
    [
        (1, 7155, True),      # the block-1 evidence
        (0, 7155, True),
        (1.0, 32, True),
        (2, 7155, False),     # short but not collapsed: a different problem, not this rule
        (32, 32, False),
        (7155, 7155, False),
        (1, 1, False),        # asked for one token, got one token
        (1, 0, False),
        (0, 1, False),
        (None, 7155, False),  # metric absent: unknown, never counted
    ],
)
def test_is_collapsed_table(observed: float | None, requested: int, expected: bool) -> None:
    from demo_bench.application.validity import is_collapsed

    assert is_collapsed(observed, requested) is expected


def test_is_collapsed_property() -> None:
    from hypothesis import given
    from hypothesis import strategies as st

    from demo_bench.application.validity import is_collapsed

    @given(st.integers(min_value=0, max_value=10**6), st.integers(min_value=0, max_value=10**6))
    def check(observed: int, requested: int) -> None:
        assert is_collapsed(observed, requested) is (requested > 1 and observed <= 1)

    check()


def test_error_limit_is_half_a_percent_and_inclusive() -> None:
    from demo_bench.application.validity import ERROR_RATE_LIMIT, exceeds_error_limit

    assert ERROR_RATE_LIMIT == 0.005
    assert exceeds_error_limit(0, 1000) is False
    assert exceeds_error_limit(1, 200) is False      # exactly 0.5 %
    assert exceeds_error_limit(1, 199) is True       # just above
    assert exceeds_error_limit(5, 1000) is False
    assert exceeds_error_limit(6, 1000) is True
    assert exceeds_error_limit(0, 0) is False
    assert exceeds_error_limit(1, 0) is True


# ----------------------------------------------------------------------------- the counter
def test_one_collapsed_response_among_many_is_counted(tmp_path: Path) -> None:
    from demo_bench.application.validity import count_collapsed_outputs

    observed: list[int | None] = [7155] * 200
    observed[37] = 1
    prof, trace = _files(tmp_path, observed, [7155] * 200)
    result = count_collapsed_outputs(prof, trace)
    assert (result.collapsed, result.total) == (1, 200)


def test_clean_run_has_zero_collapsed(tmp_path: Path) -> None:
    from demo_bench.application.validity import count_collapsed_outputs

    prof, trace = _files(tmp_path, [32] * 10, [32] * 10)
    result = count_collapsed_outputs(prof, trace)
    assert (result.collapsed, result.total) == (0, 10)


def test_requested_osl_comes_from_the_matching_trace_row(tmp_path: Path) -> None:
    """Row 1 asked for 1 token (observed 1: fine); row 2 asked for 500 (observed 1: collapsed)."""
    from demo_bench.application.validity import count_collapsed_outputs

    prof, trace = _files(tmp_path, [64, 1, 1], [64, 1, 500])
    assert count_collapsed_outputs(prof, trace).collapsed == 1


def test_pairing_follows_request_start_not_file_order(tmp_path: Path) -> None:
    from demo_bench.application.validity import count_collapsed_outputs

    rows = [_row(i, 1 if i == 2 else 50) for i in range(4)]
    prof = _write(tmp_path / "profile_export.jsonl", [rows[3], rows[0], rows[2], rows[1]])
    # only trace row 2 (start order) asks for more than one token
    trace = _write(tmp_path / "scheduled-trace.jsonl",
                   [_trace_row(0, 1), _trace_row(1, 1), _trace_row(2, 900), _trace_row(3, 1)])
    assert count_collapsed_outputs(prof, trace).collapsed == 1


def test_warmup_rows_and_blank_lines_are_ignored(tmp_path: Path) -> None:
    from demo_bench.application.validity import count_collapsed_outputs

    prof = _write(tmp_path / "profile_export.jsonl",
                  [_row(-5, 1, phase="warmup"), "", _row(0, 40), "   ", _row(1, 1)])
    trace = _write(tmp_path / "scheduled-trace.jsonl", [_trace_row(0, 40), _trace_row(1, 40)])
    result = count_collapsed_outputs(prof, trace)
    assert (result.collapsed, result.total) == (1, 2)


def test_a_row_with_error_is_not_double_counted(tmp_path: Path) -> None:
    from demo_bench.application.validity import count_collapsed_outputs

    prof = _write(tmp_path / "profile_export.jsonl",
                  [_row(0, 1, error={"code": 502, "type": "HTTPError"}), _row(1, 1), _row(2, 40)])
    trace = _write(tmp_path / "scheduled-trace.jsonl", [_trace_row(i, 40) for i in range(3)])
    result = count_collapsed_outputs(prof, trace)
    assert (result.collapsed, result.total) == (1, 3)


def test_missing_metric_is_not_counted_and_fallback_metric_is_used(tmp_path: Path) -> None:
    from demo_bench.application.validity import count_collapsed_outputs

    prof = _write(tmp_path / "profile_export.jsonl",
                  [_row(0, None), _row(1, 1, metric="output_token_count"), _row(2, 40)])
    trace = _write(tmp_path / "scheduled-trace.jsonl", [_trace_row(i, 40) for i in range(3)])
    assert count_collapsed_outputs(prof, trace).collapsed == 1


def test_more_profiling_rows_than_trace_rows_is_an_error(tmp_path: Path) -> None:
    from demo_bench.application.validity import count_collapsed_outputs

    prof, _ = _files(tmp_path, [40] * 3, [40] * 3)
    short = _write(tmp_path / "short.jsonl", [_trace_row(0, 40)])
    with pytest.raises(ValueError, match="trace"):
        count_collapsed_outputs(prof, short)


def test_reason_names_collapsed_count_and_total(tmp_path: Path) -> None:
    from demo_bench.application.validity import collapsed_outputs_reason, count_collapsed_outputs

    prof, trace = _files(tmp_path, [1, 1, 40, 40], [900] * 4)
    reason = collapsed_outputs_reason(count_collapsed_outputs(prof, trace))
    assert "collapsed" in reason and "2" in reason and "4" in reason


# ----------------------------------------------------------------------------- sweep validity
SWEEP = Path(__file__).resolve().parents[1] / "fixtures" / "sweep"


def _sweep_copy(tmp_path: Path) -> Path:
    import shutil

    dst = tmp_path / "runs"
    shutil.copytree(SWEEP, dst)
    return dst


def _inject(run: Path, observed: Sequence[int | None], requested: Sequence[int]) -> None:
    _write(run / "aiperf" / "profile_export.jsonl", [_row(i, o) for i, o in enumerate(observed)])
    _write(run / "scheduled-trace.jsonl", [_trace_row(i, r) for i, r in enumerate(requested)])


def _build(runs: Path) -> dict[str, Any]:
    from demo_bench.application.sweep import build_sweep_data

    return build_sweep_data(runs, setup="qa")


def test_sweep_excludes_a_run_whose_collapsed_share_is_above_the_limit(tmp_path: Path) -> None:
    runs = _sweep_copy(tmp_path)
    run = runs / "c8" / "ceiling" / "S1" / "rep1"
    _inject(run, [7155] * 198 + [1, 1], [7155] * 200)  # 2 of 200 = 1 % > 0.5 %
    data = _build(runs)
    entries = [e for e in data["excluded"] if (e["scenario"], e["concurrency"]) == ("S1", 8)]
    assert len(entries) == 1, data["excluded"]
    assert "collapsed" in entries[0]["reason"] and "2" in entries[0]["reason"]
    assert data["series"]["S1"]["n"][data["concurrencies"].index(8)] == 0
    assert {"scenario": "S1", "concurrency": 8} in data["missing"]


def test_sweep_keeps_a_run_at_exactly_the_limit_and_without_collapse(tmp_path: Path) -> None:
    runs = _sweep_copy(tmp_path)
    at_limit = runs / "c8" / "ceiling" / "S1" / "rep1"
    _inject(at_limit, [7155] * 199 + [1], [7155] * 200)  # 1 of 200 = 0.5 %
    clean = runs / "c8" / "ceiling" / "S2" / "rep1"
    _inject(clean, [7155] * 200, [7155] * 200)
    data = _build(runs)
    i = data["concurrencies"].index(8)
    assert data["series"]["S1"]["n"][i] == 1 and data["series"]["S2"]["n"][i] == 1
    assert data["excluded"] == []


def test_sweep_without_per_request_files_is_unchanged(tmp_path: Path) -> None:
    """Positive control: the existing fixtures carry no profile_export.jsonl and stay valid."""
    data = _build(_sweep_copy(tmp_path))
    assert data["excluded"] == [] and data["missing"] == []


# ----------------------------------------------------------------------------- compare / analysis
def _summary(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "aiperf_version": "0.13.0", "error_summary": [{"code": 500, "message": "boom", "count": 2}],
        "request_throughput": {"unit": "requests/sec", "avg": 1.0},
    }))
    return path


def test_read_run_artifact_adds_collapsed_outputs_to_error_count(tmp_path: Path) -> None:
    from demo_bench.infrastructure.aiperf_reader import read_run_artifact

    run = tmp_path / "run"
    summary = _summary(run / "aiperf" / "profile_export_aiperf.json")
    _write(run / "aiperf" / "profile_export.jsonl", [_row(i, 1 if i < 3 else 40) for i in range(10)])
    _write(run / "scheduled-trace.jsonl", [_trace_row(i, 40) for i in range(10)])
    assert read_run_artifact(summary, "a").error_count == 2 + 3


def test_read_run_artifact_without_per_request_files_keeps_the_summary_count(tmp_path: Path) -> None:
    from demo_bench.infrastructure.aiperf_reader import read_run_artifact

    summary = _summary(tmp_path / "run" / "aiperf" / "profile_export_aiperf.json")
    assert read_run_artifact(summary, "a").error_count == 2
