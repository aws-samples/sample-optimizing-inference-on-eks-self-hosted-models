# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""2^3 factorial contrasts on log(KPI) with a round-level percentile bootstrap (spec 10.6).

Effect of a contrast = mean of the cell means of ``log(kpi)`` where the (product of) +-1 signs is
+1, minus the same where it is -1. ``exp(effect)`` is the multiplicative change of the KPI.
"""

from __future__ import annotations

import logging
import math
import random
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

logger = logging.getLogger(__name__)

CONTRASTS: tuple[str, ...] = ("R", "O", "D", "RO", "RD", "OD", "ROD")
Cell = tuple[int, int, int]
CELLS: tuple[Cell, ...] = tuple((r, o, d) for r in (0, 1) for o in (0, 1) for d in (0, 1))
_FACTOR_INDEX = {"R": 0, "O": 1, "D": 2}


@dataclass(frozen=True)
class Observation:
    """One KPI measurement of one scenario in one round.

    Attributes:
        r: Router factor (1 = ``kv``).
        o: HiCache factor (1 = on).
        d: Disaggregation factor (1 = PD).
        round: Repetition round id.
        kpi: Strictly positive KPI value.
    """

    r: int
    o: int
    d: int
    round: int
    kpi: float

    @property
    def cell(self) -> Cell:
        """Factor levels ``(r, o, d)``."""
        return (self.r, self.o, self.d)


@dataclass(frozen=True)
class Effect:
    """A factorial contrast on log(KPI) with its bootstrap CI.

    Attributes:
        estimate: Point estimate (log scale).
        ci_low: Lower percentile-bootstrap bound (log scale).
        ci_high: Upper percentile-bootstrap bound (log scale).
    """

    estimate: float
    ci_low: float
    ci_high: float

    @property
    def ratio(self) -> float:
        """Multiplicative effect on the KPI, ``exp(estimate)``."""
        return math.exp(self.estimate)

    @property
    def ratio_ci(self) -> tuple[float, float]:
        """CI of :attr:`ratio`."""
        return (math.exp(self.ci_low), math.exp(self.ci_high))


def _sign(cell: Cell, contrast: str) -> int:
    sign = 1
    for factor in contrast:
        sign *= 2 * cell[_FACTOR_INDEX[factor]] - 1
    return sign


_SIGNS: dict[str, dict[Cell, int]] = {k: {c: _sign(c, k) for c in CELLS} for k in CONTRASTS}


def _effects(cell_means: Mapping[Cell, float]) -> dict[str, float]:
    """Contrasts from the 8 cell means (balanced: 4 cells per sign)."""
    out: dict[str, float] = {}
    for contrast, signs in _SIGNS.items():
        total = sum(signs[cell] * mean for cell, mean in cell_means.items())
        out[contrast] = total / (len(CELLS) / 2)
    return out


def _cell_means(
    logs_by_round: Mapping[int, Mapping[Cell, list[float]]], rounds: Sequence[int]
) -> dict[Cell, float]:
    acc: dict[Cell, list[float]] = defaultdict(list)
    for rnd in rounds:
        for cell, values in logs_by_round[rnd].items():
            acc[cell].extend(values)
    return {cell: sum(v) / len(v) for cell, v in acc.items()}


def _percentile(sorted_values: Sequence[float], q: float) -> float:
    """Linear-interpolation percentile (numpy's default) of pre-sorted values, ``q`` in [0, 1]."""
    pos = q * (len(sorted_values) - 1)
    lo = math.floor(pos)
    hi = min(lo + 1, len(sorted_values) - 1)
    return sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * (pos - lo)


def _validate(observations: Sequence[Observation]) -> dict[int, dict[Cell, list[float]]]:
    by_round: dict[int, dict[Cell, list[float]]] = defaultdict(lambda: defaultdict(list))
    for obs in observations:
        if obs.cell not in CELLS:
            raise ValueError(f"factor levels must be 0 or 1, got cell {obs.cell}")
        if not (math.isfinite(obs.kpi) and obs.kpi > 0):
            raise ValueError(f"kpi must be finite and > 0, got {obs.kpi} in cell {obs.cell}")
        by_round[obs.round][obs.cell].append(math.log(obs.kpi))
    if not by_round:
        raise ValueError("no observations: every cell is missing")
    for rnd, cells in sorted(by_round.items()):
        absent = [c for c in CELLS if c not in cells]
        if absent:
            raise ValueError(f"round {rnd} is missing cell(s) (r, o, d) = {absent}")
    return by_round


def matrix_effects(
    observations: Sequence[Observation],
    *,
    n_boot: int = 1000,
    seed: int = 0,
    alpha: float = 0.05,
) -> dict[str, Effect]:
    """Main effects and interactions of R, O, D on log(KPI).

    Every round must contain all 8 cells (full factorial per round). Repeated observations of a
    cell within a round are averaged on the log scale. The CI is a percentile bootstrap that
    resamples whole rounds with replacement.

    Args:
        observations: Measurements, one or more per cell and round.
        n_boot: Bootstrap resamples.
        seed: Bootstrap RNG seed (same seed, same CIs).
        alpha: Two-sided CI level ``1 - alpha``.

    Returns:
        ``{"R", "O", "D", "RO", "RD", "OD", "ROD"} -> Effect``.

    Raises:
        ValueError: On a missing cell, a non-positive KPI or invalid bootstrap parameters.
    """
    if n_boot < 1:
        raise ValueError(f"n_boot must be >= 1, got {n_boot}")
    if not 0.0 < alpha < 1.0:
        raise ValueError(f"alpha must be in (0, 1), got {alpha}")
    by_round = _validate(observations)
    rounds = sorted(by_round)
    point = _effects(_cell_means(by_round, rounds))

    rng = random.Random(seed)
    draws: dict[str, list[float]] = {k: [] for k in CONTRASTS}
    for _ in range(n_boot):
        sample = rng.choices(rounds, k=len(rounds))
        for k, v in _effects(_cell_means(by_round, sample)).items():
            draws[k].append(v)

    effects: dict[str, Effect] = {}
    for k in CONTRASTS:
        values = sorted(draws[k])
        effects[k] = Effect(
            estimate=point[k],
            ci_low=_percentile(values, alpha / 2),
            ci_high=_percentile(values, 1 - alpha / 2),
        )
    logger.info(
        "factorial effects computed",
        extra={"rounds": len(rounds), "n_boot": n_boot, "observations": len(observations)},
    )
    return effects
