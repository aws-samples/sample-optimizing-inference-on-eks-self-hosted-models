# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Readers for aiperf 0.13.0 exports.

- ``profile_export_aiperf.json``: summary statistics per metric (dicts carrying ``unit``).
- ``profile_export.jsonl``: one record per request with ``metadata.benchmark_phase``.

In a run directory written by ``scripts/30-run.sh`` (``<run>/aiperf/profile_export_aiperf.json``)
the error count also includes the collapsed outputs (P-37, ``application.validity``) when
``<run>/aiperf/profile_export.jsonl`` and ``<run>/scheduled-trace.jsonl`` both exist.

Assumption (M): the ``benchmark_phase`` field name is confirmed against a real 0.13.0 export.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

from demo_bench.application.validity import PROFILE_EXPORT_RELPATH, run_collapsed_outputs
from demo_bench.domain.comparison import RunArtifact
from demo_bench.domain.window import RunWindow

NS_PER_S = 1_000_000_000
PROFILING_PHASE = "profiling"


def _as_number(value: Any) -> float | None:
    """Return ``value`` as a finite float, or ``None`` for non-numbers, booleans, NaN and inf."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _as_ns(value: Any) -> int | None:
    """Return an integer nanosecond timestamp (kept as ``int`` to avoid float rounding)."""
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    number = _as_number(value)
    return None if number is None else int(number)


def read_run_artifact(path: Path, label: str) -> RunArtifact:
    """Read summary statistics from ``profile_export_aiperf.json``.

    Every top-level object that carries a ``unit`` key is a metric; its numeric entries
    (``avg``, ``p50``...) are kept. ``error_count`` is the sum of ``error_summary[].count``
    plus, when ``path`` is ``<run>/aiperf/profile_export_aiperf.json`` and the run has
    ``aiperf/profile_export.jsonl`` and ``scheduled-trace.jsonl``, the collapsed outputs (P-37).

    Args:
        path: Export file.
        label: Arm label for the comparison (e.g. ``rr``).

    Returns:
        The run artifact.

    Raises:
        ValueError: If the file is not an aiperf summary export, or the per-request files
            cannot be paired (see ``application.validity.count_collapsed_outputs``).
    """
    doc = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(doc, dict):
        raise ValueError(f"{path}: not an aiperf summary export (expected a JSON object)")
    metrics: dict[str, dict[str, float]] = {}
    for name, body in doc.items():
        if isinstance(body, dict) and "unit" in body:
            stats = {k: n for k, v in body.items() if (n := _as_number(v)) is not None}
            if stats:
                metrics[name] = stats
    errors = 0
    for entry in doc.get("error_summary") or []:
        count = _as_number(entry.get("count")) if isinstance(entry, dict) else None
        if count is not None:
            errors += int(count)
    export_dir = Path(path).parent
    if export_dir.name == PROFILE_EXPORT_RELPATH.parent.name:
        collapsed = run_collapsed_outputs(export_dir.parent)
        if collapsed is not None:
            errors += collapsed.collapsed
    return RunArtifact(
        label=label,
        metrics=metrics,
        error_count=errors,
        aiperf_version=str(doc.get("aiperf_version", "unknown")),
    )


def read_profile_window(path: Path) -> RunWindow:
    """Profiling window from the per-request export, warmup rows excluded.

    Args:
        path: ``profile_export.jsonl``.

    Returns:
        ``RunWindow(min request start, max request end)`` of the profiling rows, in seconds.

    Raises:
        ValueError: If there is no profiling row with timestamps.
    """
    starts: list[int] = []
    ends: list[int] = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        meta = record.get("metadata") if isinstance(record, dict) else None
        if not isinstance(meta, dict) or meta.get("benchmark_phase") != PROFILING_PHASE:
            continue
        start = _as_ns(meta.get("request_start_ns"))
        end = _as_ns(meta.get("request_end_ns"))
        if start is not None:
            starts.append(start)
        if end is not None:
            ends.append(end)
    if not starts or not ends:
        raise ValueError(f"{path}: no {PROFILING_PHASE!r} rows with request timestamps")
    return RunWindow(start_s=min(starts) / NS_PER_S, end_s=max(ends) / NS_PER_S)
