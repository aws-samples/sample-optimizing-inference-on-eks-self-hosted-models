# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Data layer of ``demo-bench plot sweep``: closed-loop concurrency sweep -> chart series.

Standard library only (no matplotlib/numpy): the CLI can import it in the core install. The
result is the exact content of ``sweep_data.json``; it holds no paths or timestamps, so identical
inputs give identical bytes.

Runs are discovered recursively from ``run.json`` files written by ``scripts/30-run.sh`` with
``mode == "closed-loop"`` and an integer ``concurrency``; the aiperf summary export is
``<dir of run.json>/aiperf/profile_export_aiperf.json``. Invalid runs (aiperf rc != 0, restarted
pods, missing export, cancelled run, collapsed outputs above 0.5 % of the requests (P-37, rule in
``application.validity``; checked when the run dir has ``aiperf/profile_export.jsonl`` and
``scheduled-trace.jsonl``)) are excluded and reported. Several reps of one point aggregate by
median. Missing data is ``None``: never 0, never interpolated. A ``--pairs`` pair whose baseline or
candidate has no valid run is dropped with a warning (P-26).
"""

from __future__ import annotations

import json
import logging
import math
import statistics
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from demo_bench.application.validity import (
    collapsed_outputs_reason,
    exceeds_error_limit,
    run_collapsed_outputs,
)
from demo_bench.domain.scenario import SCENARIOS

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1
CLOSED_LOOP = "closed-loop"
EXPORT_RELPATH = Path("aiperf") / "profile_export_aiperf.json"

# Fixed colour per scenario id: Okabe-Ito colour-blind safe palette, no black (used by ticks).
SCENARIO_COLORS: dict[str, str] = {
    "S0": "#0072b2",
    "S1": "#d55e00",
    "S2": "#009e73",
    "S3": "#cc79a7",
    "S4": "#e69f00",
    "S5": "#56b4e9",
    "S6": "#7f7f7f",
    "S7": "#8c564b",
}

# Series key -> (aiperf metric, statistic).
METRICS: dict[str, tuple[str, str]] = {
    "ttft_p50_ms": ("time_to_first_token", "p50"),
    "ttft_p90_ms": ("time_to_first_token", "p90"),
    "itl_p50_ms": ("inter_token_latency", "p50"),
    "itl_p90_ms": ("inter_token_latency", "p90"),
    "output_tok_s": ("output_token_throughput", "avg"),
}
_KNOWN_SCENARIOS = frozenset(s.id for s in SCENARIOS)


class SweepError(ValueError):
    """The sweep cannot be built (no setup label, no valid closed-loop run, malformed pairs)."""


@dataclass(frozen=True)
class _Point:
    scenario: str
    concurrency: int
    rep: int | None
    values: dict[str, float | None]


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _invalid_reason(meta: dict[str, Any], export: Any) -> str | None:
    aiperf = meta.get("aiperf") if isinstance(meta.get("aiperf"), dict) else {}
    rc = aiperf.get("rc") if isinstance(aiperf, dict) else None
    if rc != 0:
        return f"aiperf exit code {rc}"
    restarted = meta.get("restarted_pods")
    if restarted:
        return f"pods restarted during the run: {restarted}"
    if not isinstance(export, dict):
        return "missing or unreadable aiperf summary export"
    if export.get("was_cancelled") is True:
        return "aiperf run was cancelled"
    return None


def _collapsed_reason(run_dir: Path) -> str | None:
    """P-37: reason to exclude a run whose collapsed outputs exceed the 0.5 % limit."""
    try:
        result = run_collapsed_outputs(run_dir)
    except (OSError, ValueError) as exc:
        return f"cannot check collapsed outputs: {exc}"
    if result is None or not exceeds_error_limit(result.collapsed, result.total):
        return None
    return collapsed_outputs_reason(result)


def _metric_values(export: dict[str, Any]) -> dict[str, float | None]:
    values: dict[str, float | None] = {}
    for key, (metric, stat) in METRICS.items():
        stats = export.get(metric)
        values[key] = _number(stats.get(stat)) if isinstance(stats, dict) else None
    return values


def _discover(runs_dir: Path) -> tuple[list[_Point], list[dict[str, Any]]]:
    valid: list[_Point] = []
    excluded: list[dict[str, Any]] = []
    for run_json in sorted(runs_dir.rglob("run.json")):
        meta = _load_json(run_json)
        if not isinstance(meta, dict) or meta.get("mode") != CLOSED_LOOP:
            continue
        concurrency, scenario = meta.get("concurrency"), meta.get("scenario")
        if isinstance(concurrency, bool) or not isinstance(concurrency, int) or concurrency < 1:
            continue
        if scenario not in _KNOWN_SCENARIOS:
            logger.warning("skipping run with unknown scenario", extra={"scenario": scenario})
            continue
        rep = meta.get("rep") if isinstance(meta.get("rep"), int) else None
        export_path = run_json.parent / EXPORT_RELPATH
        export = _load_json(export_path) if export_path.is_file() else None
        reason = _invalid_reason(meta, export) or _collapsed_reason(run_json.parent)
        if reason is not None or not isinstance(export, dict):
            excluded.append(
                {
                    "scenario": scenario,
                    "concurrency": concurrency,
                    "rep": rep,
                    "reason": reason or "missing or unreadable aiperf summary export",
                }
            )
            continue
        valid.append(_Point(scenario, concurrency, rep, _metric_values(export)))
    excluded.sort(key=lambda e: (e["scenario"], e["concurrency"], e["rep"] or 0, e["reason"]))
    return valid, excluded


def _median(values: list[float]) -> float | None:
    return statistics.median(values) if values else None


def _select_pairs(pairs: Sequence[tuple[str, str]], scenarios: list[str]) -> list[str]:
    """Validate the pairs and keep those whose two scenarios have a valid run.

    A pair without a valid run on either side (never ran, unknown id, every run excluded) is
    dropped with a warning (P-26); a pair with baseline == candidate or a duplicate is an error.
    """
    names: list[str] = []
    seen: set[str] = set()
    for baseline, candidate in pairs:
        name = f"{baseline}:{candidate}"
        if baseline == candidate:
            raise SweepError(f"pair {name}: baseline and candidate must differ")
        if name in seen:
            raise SweepError(f"duplicate pair {name}")
        seen.add(name)
        without_runs = [s for s in (baseline, candidate) if s not in scenarios]
        if without_runs:
            logger.warning(
                "pair dropped: no valid sweep run",
                extra={"pair": name, "scenarios_without_valid_run": without_runs},
            )
            continue
        names.append(name)
    return names


def _speedup(baseline: float | None, candidate: float | None) -> float | None:
    if baseline is None or candidate is None or baseline <= 0 or candidate <= 0:
        return None
    return baseline / candidate


def build_sweep_data(
    runs_dir: Path, *, setup: str, pairs: Sequence[tuple[str, str]] = ()
) -> dict[str, Any]:
    """Aggregate a closed-loop concurrency sweep into chart series.

    Args:
        runs_dir: Directory searched recursively for ``run.json`` files.
        setup: Setup label printed on every chart (mandatory, non-blank).
        pairs: ``(baseline, candidate)`` scenario pairs for the TTFT p50 speedup
            (``baseline / candidate``). A pair whose baseline or candidate has no valid run is
            dropped with a warning and is absent from ``pairs`` and ``speedup``.

    Returns:
        The ``sweep_data.json`` document (schema in the module docstring of the tests).

    Raises:
        SweepError: On a blank setup label, a missing directory, no valid closed-loop run, a
            pair with baseline == candidate or a duplicate pair.
    """
    if not isinstance(setup, str) or not setup.strip():
        raise SweepError("a non-blank setup label is required (it is printed on every chart)")
    runs_dir = Path(runs_dir)
    if not runs_dir.is_dir():
        raise SweepError(f"runs directory not found: {runs_dir}")
    valid, excluded = _discover(runs_dir)
    if not valid:
        raise SweepError(f"no valid closed-loop sweep runs under {runs_dir}")

    concurrencies = sorted({p.concurrency for p in valid})
    scenarios = sorted({p.scenario for p in valid})
    grouped: dict[tuple[str, int], list[_Point]] = defaultdict(list)
    for point in valid:
        grouped[(point.scenario, point.concurrency)].append(point)

    series: dict[str, dict[str, list[Any]]] = {}
    missing: list[dict[str, Any]] = []
    for scenario in scenarios:
        entry: dict[str, list[Any]] = {key: [] for key in METRICS}
        entry["n"] = []
        for concurrency in concurrencies:
            points = grouped.get((scenario, concurrency), [])
            entry["n"].append(len(points))
            if not points:
                missing.append({"scenario": scenario, "concurrency": concurrency})
            for key in METRICS:
                observed = [v for p in points if (v := p.values[key]) is not None]
                entry[key].append(_median(observed))
        series[scenario] = entry

    pair_names = _select_pairs(pairs, scenarios)
    speedup = {
        name: [
            _speedup(b, c)
            for b, c in zip(
                series[name.split(":")[0]]["ttft_p50_ms"],
                series[name.split(":")[1]]["ttft_p50_ms"],
                strict=True,
            )
        ]
        for name in pair_names
    }
    data: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "setup": setup,
        "concurrencies": concurrencies,
        "scenarios": scenarios,
        "colors": {s: SCENARIO_COLORS[s] for s in scenarios},
        "series": series,
        "pairs": pair_names,
        "speedup": speedup,
        "missing": missing,
        "excluded": excluded,
    }
    logger.info(
        "sweep data built",
        extra={
            "valid_runs": len(valid),
            "excluded_runs": len(excluded),
            "missing_points": len(missing),
            "scenarios": scenarios,
            "concurrencies": concurrencies,
        },
    )
    return data


def dump_sweep_data(data: dict[str, Any]) -> str:
    """Serialise the sweep data deterministically (``sort_keys``, 2-space indent).

    Args:
        data: Result of :func:`build_sweep_data`.

    Returns:
        JSON text ending with a newline.
    """
    return json.dumps(data, sort_keys=True, indent=2) + "\n"
