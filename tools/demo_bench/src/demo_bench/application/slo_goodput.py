# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""SLO goodput of a W2 run from aiperf's per-request export (Flash plan 4.5, critic fixes).

The headline metric is the number of turns per second that meet their SLO at the fixed rate:

* ``continuation`` turns: TTFT <= ``ttft_continuation_s`` and ITL <= ``itl_ms``;
* ``cold_ingestion`` turns: TTFT <= ``cold_factor * input_length / cold_prefill_tok_s`` (the
  cold prefill rate measured in the pilot) and ITL <= ``itl_ms``;
* ``entry`` turns are the populate phase and are never judged.

An errored request, or one without TTFT or ITL, misses. ITL is aiperf's per-request mean
``inter_token_latency``.

Pairing: the profiling rows of ``profile_export.jsonl`` sorted by ``request_start_ns`` pair 1:1
with the sidecar in line order (the positional rule of ``application.validity``); fewer rows than
sidecar entries are accepted, more are refused.

Uncertainty: a percentile bootstrap whose resampling unit is the session (a session's turns are
correlated through its cache state, so they move together); each cold ingestion is its own unit.
``calibration`` checks the overall attainment of the S0 pilot against an acceptance band, so the
pre-registered SLO is neither almost always missed nor almost always met.
"""

from __future__ import annotations

import logging
import math
import random
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

# Shared on purpose: the same percentile as the matrix bootstrap and the same positional pairing
# as the validity rule.
from demo_bench.application.matrix import _percentile
from demo_bench.application.validity import PROFILING_PHASE, _jsonl, _start_key

logger = logging.getLogger(__name__)

ENTRY, CONTINUATION, COLD_INGESTION = "entry", "continuation", "cold_ingestion"
CLASSES = (ENTRY, CONTINUATION, COLD_INGESTION)
JUDGED = (CONTINUATION, COLD_INGESTION)
MS_PER_S = 1000.0
DEFAULT_BAND = (0.4, 0.8)


@dataclass(frozen=True)
class Sample:
    """One profiled request paired with its sidecar entry.

    Attributes:
        session: Session index (``None`` for a cold ingestion).
        turn: Turn index (``None`` for a cold ingestion).
        klass: ``entry``, ``continuation`` or ``cold_ingestion``.
        input_length: Input tokens (from the sidecar).
        ttft_s: Time to first token, seconds (``None`` when absent).
        itl_ms: Mean inter-token latency of the request, ms (``None`` when absent).
        error: Whether aiperf recorded an error for the request.
    """

    session: int | None
    turn: int | None
    klass: str
    input_length: int
    ttft_s: float | None
    itl_ms: float | None
    error: bool


def _positive(name: str, value: float) -> None:
    if isinstance(value, bool) or not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be a finite number > 0, got {value!r}")


@dataclass(frozen=True, kw_only=True)
class SloThresholds:
    """Pre-registered SLO.

    Attributes:
        cold_prefill_tok_s: Cold prefill rate measured in the pilot, tokens/s.
        ttft_continuation_s: TTFT limit of a continuation turn, seconds.
        itl_ms: Mean ITL limit of every judged turn, ms.
        cold_factor: Slack on the cold ingestion TTFT (``factor * ISL / rate``).
    """

    cold_prefill_tok_s: float
    ttft_continuation_s: float = 2.0
    itl_ms: float = 25.0
    cold_factor: float = 1.4

    def __post_init__(self) -> None:
        """Validate the thresholds.

        Raises:
            ValueError: If a threshold is not a finite number > 0.
        """
        for name in ("cold_prefill_tok_s", "ttft_continuation_s", "itl_ms", "cold_factor"):
            _positive(name, getattr(self, name))

    def to_dict(self) -> dict[str, float]:
        """Return a JSON-serialisable view.

        Returns:
            Every threshold.
        """
        return {
            "cold_prefill_tok_s": self.cold_prefill_tok_s,
            "ttft_continuation_s": self.ttft_continuation_s,
            "itl_ms": self.itl_ms,
            "cold_factor": self.cold_factor,
        }


@dataclass(frozen=True)
class SloResult:
    """Goodput of one run.

    Attributes:
        goodput: SLO-met judged turns per second.
        classes: Per judged class: ``n``, ``met`` and ``attainment`` (``None`` when ``n`` is 0).
        excluded_entry: Entry (populate) samples, never judged.
        attainment: ``met / n`` over both judged classes (``None`` when there is none).
    """

    goodput: float
    classes: dict[str, dict[str, Any]] = field(default_factory=dict)
    excluded_entry: int = 0
    attainment: float | None = None


def _metric_ms(row: Mapping[str, Any], name: str) -> float | None:
    """Return ``metrics.<name>.value`` in ms (``None`` when absent or not a finite number)."""
    metrics = row.get("metrics")
    metric = metrics.get(name) if isinstance(metrics, dict) else None
    if not isinstance(metric, dict):
        return None
    unit = metric.get("unit", "ms")
    if unit != "ms":
        raise ValueError(f"metrics.{name}: expected unit 'ms', got {unit!r}")
    value = metric.get("value")
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        return None
    return float(value)


def _sidecar_entry(line: int, side: Any) -> tuple[int | None, int | None, str, int]:
    if not isinstance(side, dict):
        raise ValueError(f"sidecar line {line}: expected a JSON object")
    klass, length = side.get("class"), side.get("input_length")
    if klass not in CLASSES:
        raise ValueError(f"sidecar line {line}: class must be one of {CLASSES}, got {klass!r}")
    if isinstance(length, bool) or not isinstance(length, int) or length < 0:
        raise ValueError(f"sidecar line {line}: input_length must be an integer >= 0")
    return side.get("session"), side.get("turn"), str(klass), length


def profiling_rows(profile_export: Path) -> list[dict[str, Any]]:
    """Return the profiling rows of an aiperf per-request export, sorted by request start.

    Args:
        profile_export: aiperf's ``profile_export.jsonl``.

    Returns:
        The rows with ``metadata.benchmark_phase == "profiling"`` (warm-up excluded).

    Raises:
        ValueError: On a malformed line.
        OSError: When the file cannot be read.
    """
    rows = [
        row
        for row in _jsonl(profile_export)
        if isinstance(row, dict)
        and isinstance(row.get("metadata"), dict)
        and row["metadata"].get("benchmark_phase") == PROFILING_PHASE
    ]
    rows.sort(key=_start_key)
    return rows


def load_samples(profile_export: Path, sidecar: Sequence[Any]) -> list[Sample]:
    """Pair the profiling rows with the W2 sidecar.

    Args:
        profile_export: aiperf's per-request ``profile_export.jsonl``.
        sidecar: W2 sidecar entries in line order.

    Returns:
        One sample per profiling row.

    Raises:
        ValueError: On a malformed line or sidecar entry, a unit other than ms, or more
            profiling rows than sidecar entries.
        OSError: When the export cannot be read.
    """
    rows = profiling_rows(profile_export)
    if len(rows) > len(sidecar):
        raise ValueError(
            f"{profile_export}: {len(rows)} profiling rows but the sidecar has only "
            f"{len(sidecar)} entries; cannot pair them"
        )
    samples: list[Sample] = []
    for line, (row, side) in enumerate(zip(rows, sidecar, strict=False)):
        session, turn, klass, length = _sidecar_entry(line, side)
        ttft_ms = _metric_ms(row, "time_to_first_token")
        samples.append(
            Sample(
                session=session,
                turn=turn,
                klass=klass,
                input_length=length,
                ttft_s=None if ttft_ms is None else ttft_ms / MS_PER_S,
                itl_ms=_metric_ms(row, "inter_token_latency"),
                error=row.get("error") is not None,
            )
        )
    return samples


def slo_met(sample: Sample, thresholds: SloThresholds) -> bool:
    """Tell whether one judged sample meets its SLO.

    Args:
        sample: A ``continuation`` or ``cold_ingestion`` sample.
        thresholds: The SLO.

    Returns:
        True iff the request did not error, has TTFT and ITL, and both are within the limits of
        its class.

    Raises:
        ValueError: For an ``entry`` sample (the populate is never judged).
    """
    if sample.klass not in JUDGED:
        raise ValueError(f"{sample.klass} samples are not judged")
    if sample.error or sample.ttft_s is None or sample.itl_ms is None:
        return False
    if sample.klass == CONTINUATION:
        ttft_limit = thresholds.ttft_continuation_s
    else:
        ttft_limit = thresholds.cold_factor * sample.input_length / thresholds.cold_prefill_tok_s
    return sample.ttft_s <= ttft_limit and sample.itl_ms <= thresholds.itl_ms


def _judged(samples: Sequence[Sample]) -> list[Sample]:
    return [s for s in samples if s.klass in JUDGED]


def evaluate(samples: Sequence[Sample], thresholds: SloThresholds, *, window_s: float) -> SloResult:
    """Compute the goodput and the attainment per class.

    Args:
        samples: Paired samples of one run.
        thresholds: The SLO.
        window_s: Measured window, seconds.

    Returns:
        The result.

    Raises:
        ValueError: If ``window_s`` is not a finite number > 0.
    """
    _positive("window_s", window_s)
    classes: dict[str, dict[str, Any]] = {}
    met_total = n_total = 0
    for klass in JUDGED:
        group = [s for s in samples if s.klass == klass]
        met = sum(slo_met(s, thresholds) for s in group)
        classes[klass] = {
            "n": len(group),
            "met": met,
            "attainment": met / len(group) if group else None,
        }
        met_total += met
        n_total += len(group)
    return SloResult(
        goodput=met_total / window_s,
        classes=classes,
        excluded_entry=sum(1 for s in samples if s.klass == ENTRY),
        attainment=met_total / n_total if n_total else None,
    )


def sensitivity_grid(
    samples: Sequence[Sample],
    thresholds: SloThresholds,
    *,
    window_s: float,
    itl_ms_values: Sequence[float],
    ttft_s_values: Sequence[float],
) -> list[dict[str, Any]]:
    """Re-evaluate the goodput over the pre-registered ITL x TTFT grid.

    Args:
        samples: Paired samples of one run.
        thresholds: The SLO; its cold rule is kept, ITL and continuation TTFT are replaced.
        window_s: Measured window, seconds.
        itl_ms_values: ITL limits, ms (applied to both judged classes).
        ttft_s_values: Continuation TTFT limits, seconds.

    Returns:
        One cell per (ITL, TTFT), ITL-major: ``itl_ms``, ``ttft_continuation_s``, ``goodput``,
        ``attainment_continuation`` and ``attainment_cold``.

    Raises:
        ValueError: On a non-positive limit or window.
    """
    cells: list[dict[str, Any]] = []
    for itl in itl_ms_values:
        for ttft in ttft_s_values:
            result = evaluate(
                samples,
                replace(thresholds, itl_ms=itl, ttft_continuation_s=ttft),
                window_s=window_s,
            )
            cells.append(
                {
                    "itl_ms": itl,
                    "ttft_continuation_s": ttft,
                    "goodput": result.goodput,
                    "attainment_continuation": result.classes[CONTINUATION]["attainment"],
                    "attainment_cold": result.classes[COLD_INGESTION]["attainment"],
                }
            )
    return cells


def bootstrap_goodput_ci(
    samples: Sequence[Sample],
    thresholds: SloThresholds,
    *,
    window_s: float,
    n_boot: int = 1000,
    seed: int = 0,
    alpha: float = 0.05,
) -> tuple[float, float]:
    """Percentile bootstrap CI of the goodput, resampling sessions.

    Args:
        samples: Paired samples of one run.
        thresholds: The SLO.
        window_s: Measured window, seconds.
        n_boot: Bootstrap replicates.
        seed: RNG seed (same seed, same interval).
        alpha: Two-sided level (0.05 gives a 95 % interval).

    Returns:
        ``(lo, hi)``; ``(0.0, 0.0)`` when no sample is judged.

    Raises:
        ValueError: If ``n_boot < 1``, ``alpha`` is outside ``(0, 1)`` or ``window_s <= 0``.
    """
    if isinstance(n_boot, bool) or not isinstance(n_boot, int) or n_boot < 1:
        raise ValueError(f"n_boot must be an integer >= 1, got {n_boot!r}")
    if not 0 < alpha < 1:
        raise ValueError(f"alpha must be in (0, 1), got {alpha!r}")
    _positive("window_s", window_s)
    per_session: dict[int, int] = {}
    units: list[int] = []
    for sample in _judged(samples):
        met = int(slo_met(sample, thresholds))
        if sample.session is None:
            units.append(met)
        else:
            per_session[sample.session] = per_session.get(sample.session, 0) + met
    units += per_session.values()
    if not units:
        return 0.0, 0.0
    rng = random.Random(seed)
    k = len(units)
    values = sorted(sum(rng.choices(units, k=k)) / window_s for _ in range(n_boot))
    return _percentile(values, alpha / 2), _percentile(values, 1 - alpha / 2)


def calibration(
    samples: Sequence[Sample],
    thresholds: SloThresholds,
    *,
    band: tuple[float, float] = DEFAULT_BAND,
) -> dict[str, Any]:
    """Check the S0 pilot attainment against the acceptance band.

    Args:
        samples: Paired samples of the pilot.
        thresholds: The SLO under calibration.
        band: Inclusive ``(lo, hi)`` band of acceptable overall attainment.

    Returns:
        ``attainment`` (``None`` without judged samples), ``band`` and ``in_band``.

    Raises:
        ValueError: If the band is not ``0 <= lo <= hi <= 1``.
    """
    lo, hi = band
    if not 0 <= lo <= hi <= 1:
        raise ValueError(f"band must satisfy 0 <= lo <= hi <= 1, got {band!r}")
    judged = _judged(samples)
    attainment = sum(slo_met(s, thresholds) for s in judged) / len(judged) if judged else None
    return {
        "attainment": attainment,
        "band": [lo, hi],
        "in_band": attainment is not None and lo <= attainment <= hi,
    }
