# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Run validity rules shared by the analysis tools and the demo take (spec 13.5, P-37).

A run stays valid while its errors are at most 0.5 % of its requests. P-37 adds collapsed
outputs to the errors: an engine that aborts a request (e.g. an input longer than the KV pool)
answers with one token and aiperf records a success ("OSL mismatch: got 1 tokens (requested
7155)"), so ``error_summary`` stays empty. A response is collapsed when it returned <= 1 output
token although the request asked for more. The requested OSL is the ``output_length`` of the
mooncake trace the run replayed (``scheduled-trace.jsonl``, written by ``scripts/30-run.sh`` for
fixed-schedule runs; aiperf sends ``ignore_eos: true``, so the full length is expected).

Pairing is positional, not by id: the profiling rows sorted by ``request_start_ns`` are matched
1:1 with the trace rows in file order. That is exact when the fixed schedule sends every row in
trace order and aiperf exports every request. Risk: two requests that start out of trace order
(near-equal timestamps) swap their pairs, and a request missing from the export (dropped, or a
run cut short) shifts every later pair by one. A wrong pairing changes the count only when the
trace rows differ in whether they request more than one token (some ``output_length`` <= 1 or
absent); when every row asks for more than one token (the block-1 trace does), any pairing gives
the same count. Fewer profiling rows than trace rows are accepted (the unpaired trace tail is
ignored); more are refused with ``ValueError``.

This module is the only definition of the rule. ``application.sweep``,
``infrastructure.aiperf_reader`` (``compare``) and ``scripts/70-demo-take.sh`` (``take.json``)
all call it. It is standard library only and imports nothing else from ``demo_bench``, so the
take script can load it from the installed package or from a source tree.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Any

ERROR_RATE_LIMIT = 0.005
PROFILING_PHASE = "profiling"
# Per-request aiperf export and the trace the run replayed, relative to the run directory
# (``<run dir>/aiperf/profile_export.jsonl`` and ``<run dir>/scheduled-trace.jsonl``).
PROFILE_EXPORT_RELPATH = Path("aiperf") / "profile_export.jsonl"
SCHEDULED_TRACE_NAME = "scheduled-trace.jsonl"
# Observed output tokens of one request, in order of preference.
_OBSERVED_METRICS: tuple[str, ...] = ("output_sequence_length", "output_token_count")
# Exact limit (1/200): float division would put 1 of 200 a rounding error away from it.
_LIMIT = Fraction(str(ERROR_RATE_LIMIT))


@dataclass(frozen=True)
class CollapsedOutputs:
    """Collapsed responses of one run.

    Attributes:
        collapsed: Profiling responses with <= 1 output token although more were requested
            (rows that already carry an aiperf ``error`` are not counted here).
        total: Profiling requests of the run (warm-up excluded, errored rows included).
    """

    collapsed: int
    total: int


def _number(value: Any) -> float | None:
    """Return ``value`` as a finite float; ``None`` for non-numbers, booleans, NaN and inf."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def is_collapsed(observed: float | None, requested: float | None) -> bool:
    """Tell whether one response collapsed.

    Args:
        observed: Output tokens the response returned; ``None`` when the metric is absent
            (unknown, never counted).
        requested: Output tokens the request asked for (the trace ``output_length``).

    Returns:
        True iff ``requested > 1`` and ``observed <= 1`` (0 and 1 both collapse).
    """
    seen, asked = _number(observed), _number(requested)
    if seen is None or asked is None:
        return False
    return asked > 1 and seen <= 1


def exceeds_error_limit(errors: int, total: int) -> bool:
    """Apply the 0.5 % error rule (spec 13.5).

    Args:
        errors: Errored requests (aiperf errors and/or collapsed responses).
        total: Requests of the run.

    Returns:
        True when ``errors / total > ERROR_RATE_LIMIT``; exactly 0.5 % is still valid. With
        ``total <= 0`` it is True only when ``errors > 0``.
    """
    if total <= 0:
        return errors > 0
    return Fraction(errors, total) > _LIMIT


def _jsonl(path: Path) -> list[Any]:
    """Parse a JSON-lines file, skipping blank lines (a malformed line raises ``ValueError``)."""
    rows: list[Any] = []
    for number, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except ValueError as exc:
            raise ValueError(f"{path}:{number}: not a JSON line ({exc})") from exc
    return rows


def _start_key(row: dict[str, Any]) -> tuple[bool, int]:
    """Sort key: ``metadata.request_start_ns`` (rows without one go last, in file order)."""
    start = row["metadata"].get("request_start_ns")
    if isinstance(start, bool) or not isinstance(start, int):
        number = _number(start)
        if number is None:
            return True, 0
        start = int(number)
    return False, start


def _observed(row: dict[str, Any]) -> float | None:
    """Output tokens of one row: ``output_sequence_length``, else ``output_token_count``."""
    metrics = row.get("metrics")
    if not isinstance(metrics, dict):
        return None
    for name in _OBSERVED_METRICS:
        metric = metrics.get(name)
        value = _number(metric.get("value")) if isinstance(metric, dict) else None
        if value is not None:
            return value
    return None


def count_collapsed_outputs(profile_export: Path, requested_trace: Path) -> CollapsedOutputs:
    """Count the collapsed responses of one run.

    The profiling rows (``metadata.benchmark_phase == "profiling"``; warm-up never counts),
    ordered by ``metadata.request_start_ns``, pair 1:1 with the trace rows in file order. A row
    that already carries ``error`` is part of ``total`` but not of ``collapsed`` (aiperf counts
    it once, as an error).

    Args:
        profile_export: aiperf's per-request ``profile_export.jsonl``.
        requested_trace: The mooncake trace the run replayed (``scheduled-trace.jsonl``); its
            ``output_length`` is the requested OSL.

    Returns:
        The collapsed and total profiling requests.

    Raises:
        ValueError: On a malformed line, or when there are more profiling rows than trace rows
            (the pairing would be wrong).
        OSError: When a file cannot be read.
    """
    profiling = [
        row
        for row in _jsonl(profile_export)
        if isinstance(row, dict)
        and isinstance(row.get("metadata"), dict)
        and row["metadata"].get("benchmark_phase") == PROFILING_PHASE
    ]
    profiling.sort(key=_start_key)
    trace = _jsonl(requested_trace)
    if len(profiling) > len(trace):
        raise ValueError(
            f"{profile_export}: {len(profiling)} profiling rows but the requested trace "
            f"{requested_trace} has only {len(trace)} rows; cannot pair them"
        )
    collapsed = 0
    for row, trace_row in zip(profiling, trace, strict=False):
        if row.get("error") is not None:
            continue
        requested = trace_row.get("output_length") if isinstance(trace_row, dict) else None
        if is_collapsed(_observed(row), requested):
            collapsed += 1
    return CollapsedOutputs(collapsed=collapsed, total=len(profiling))


def run_collapsed_outputs(run_dir: Path) -> CollapsedOutputs | None:
    """Count the collapsed responses of a run directory written by ``scripts/30-run.sh``.

    Args:
        run_dir: Directory of ``run.json``.

    Returns:
        The count, or ``None`` when ``aiperf/profile_export.jsonl`` or ``scheduled-trace.jsonl``
        is absent (closed-loop runs replay the raw trace and write no scheduled trace).

    Raises:
        ValueError: As :func:`count_collapsed_outputs`.
        OSError: As :func:`count_collapsed_outputs`.
    """
    profile_export = Path(run_dir) / PROFILE_EXPORT_RELPATH
    requested_trace = Path(run_dir) / SCHEDULED_TRACE_NAME
    if not profile_export.is_file() or not requested_trace.is_file():
        return None
    return count_collapsed_outputs(profile_export, requested_trace)


def collapsed_outputs_reason(result: CollapsedOutputs) -> str:
    """Human reason for a run whose collapsed responses exceed the limit.

    Args:
        result: Result of :func:`count_collapsed_outputs`.

    Returns:
        One line naming "collapsed", the count and the total.
    """
    return (
        f"{result.collapsed} of {result.total} response(s) collapsed to <= 1 output token "
        f"although more were requested (limit {ERROR_RATE_LIMIT:.1%})"
    )
