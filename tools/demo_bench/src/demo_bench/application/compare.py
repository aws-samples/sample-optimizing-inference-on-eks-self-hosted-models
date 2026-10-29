# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""A/B comparison of two aiperf runs (spec 10.5: percentiles, never average-only)."""

from __future__ import annotations

from collections.abc import Sequence

from demo_bench.domain.comparison import Comparison, ComparisonRow, RunArtifact

PERCENTILES: tuple[str, ...] = ("p50", "p95", "p99")
LATENCY_METRICS: tuple[str, ...] = ("time_to_first_token", "inter_token_latency", "request_latency")
THROUGHPUT_METRICS: tuple[str, ...] = (
    "request_throughput",
    "output_token_throughput",
    "goodput",
)
DEFAULT_METRICS: tuple[str, ...] = LATENCY_METRICS + THROUGHPUT_METRICS


def _stats_for(metric: str, baseline: RunArtifact, candidate: RunArtifact) -> tuple[str, ...]:
    if metric in LATENCY_METRICS:
        return PERCENTILES
    if metric in THROUGHPUT_METRICS:
        return ("avg",)
    present = set(baseline.metrics.get(metric, {})) | set(candidate.metrics.get(metric, {}))
    return PERCENTILES if present & set(PERCENTILES) else ("avg",)


def compare_runs(
    baseline: RunArtifact,
    candidate: RunArtifact,
    metrics: Sequence[str] | None = None,
) -> Comparison:
    """Build the baseline vs candidate table.

    Latency metrics are compared at p50/p95/p99, throughput metrics at ``avg``. A metric missing
    on either side is flagged (``missing=True``, value ``None``), never reported as 0.

    Args:
        baseline: Baseline run (e.g. ``rr``).
        candidate: Candidate run (e.g. ``kv``).
        metrics: Metric names; defaults to :data:`DEFAULT_METRICS`.

    Returns:
        The comparison.
    """
    rows: list[ComparisonRow] = []
    missing: set[str] = set()
    for metric in metrics if metrics is not None else DEFAULT_METRICS:
        for stat in _stats_for(metric, baseline, candidate):
            base = baseline.value(metric, stat)
            cand = candidate.value(metric, stat)
            is_missing = base is None or cand is None
            if is_missing:
                missing.add(metric)
            ratio = None if base is None or cand is None or base == 0 else cand / base
            rows.append(ComparisonRow(metric, stat, base, cand, ratio, is_missing))
    return Comparison(
        rows=tuple(rows),
        baseline_errors=baseline.error_count,
        candidate_errors=candidate.error_count,
        missing=frozenset(missing),
        baseline_label=baseline.label,
        candidate_label=candidate.label,
    )
