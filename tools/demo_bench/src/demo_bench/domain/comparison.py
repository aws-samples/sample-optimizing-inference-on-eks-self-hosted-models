# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Run artifacts and A/B comparison tables (spec 10.5, 12.1)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

MISSING = "MISSING"


@dataclass(frozen=True)
class RunArtifact:
    """Summary statistics of one aiperf run.

    Attributes:
        label: Arm label, e.g. ``rr`` or ``kv``.
        metrics: ``metric -> stat -> value`` (e.g. ``time_to_first_token -> p50 -> 812.0``).
        error_count: Failed requests reported by aiperf, plus collapsed outputs when the
            per-request files are available (P-37, ``application.validity``).
        aiperf_version: aiperf version that produced the export (defines the series).
    """

    label: str
    metrics: Mapping[str, Mapping[str, float]]
    error_count: int
    aiperf_version: str

    def value(self, metric: str, stat: str) -> float | None:
        """Return one statistic, or ``None`` when absent.

        Args:
            metric: Metric name.
            stat: Statistic name (``p50``, ``avg``...).

        Returns:
            The value or ``None``.
        """
        stats = self.metrics.get(metric)
        if stats is None:
            return None
        return stats.get(stat)


@dataclass(frozen=True)
class ComparisonRow:
    """One ``metric.stat`` line of a comparison.

    Attributes:
        metric: Metric name.
        stat: Statistic name.
        baseline: Baseline value, ``None`` if missing.
        candidate: Candidate value, ``None`` if missing.
        ratio: ``candidate / baseline``; ``None`` if either is missing or the baseline is 0.
        missing: Whether either side lacks the value.
    """

    metric: str
    stat: str
    baseline: float | None
    candidate: float | None
    ratio: float | None
    missing: bool


def _fmt(value: float | None) -> str:
    if value is None:
        return MISSING
    return f"{value:.6g}"


@dataclass(frozen=True)
class Comparison:
    """Baseline vs candidate table.

    Attributes:
        rows: Comparison rows in display order.
        baseline_errors: Baseline error count.
        candidate_errors: Candidate error count.
        missing: Metric names with at least one missing value on either side.
        baseline_label: Baseline arm label.
        candidate_label: Candidate arm label.
    """

    rows: tuple[ComparisonRow, ...]
    baseline_errors: int
    candidate_errors: int
    missing: frozenset[str]
    baseline_label: str = "baseline"
    candidate_label: str = "candidate"

    def to_markdown(self) -> str:
        """Render the comparison as a Markdown table followed by the error counts.

        Returns:
            Markdown text.
        """
        lines = [
            "| metric | stat | baseline | candidate | ratio |",
            "|---|---|---:|---:|---:|",
        ]
        for row in self.rows:
            ratio = MISSING if row.missing else ("n/a" if row.ratio is None else f"{row.ratio:.2f}")
            lines.append(
                f"| {row.metric} | {row.stat} | {_fmt(row.baseline)} | "
                f"{_fmt(row.candidate)} | {ratio} |"
            )
        lines.append("")
        lines.append(
            f"errors: baseline ({self.baseline_label}) = {self.baseline_errors}, "
            f"candidate ({self.candidate_label}) = {self.candidate_errors}"
        )
        if self.missing:
            lines.append(f"{MISSING} (either side): {', '.join(sorted(self.missing))}")
        return "\n".join(lines) + "\n"
