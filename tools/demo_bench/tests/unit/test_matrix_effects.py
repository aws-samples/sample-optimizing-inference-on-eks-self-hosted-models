# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests for ``application.matrix.matrix_effects`` (spec 10.6, 12.1)."""

from __future__ import annotations

import itertools
import math
import random
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

KEYS = ["R", "O", "D", "RO", "RD", "OD", "ROD"]
# Effect = difference between mean log(KPI) at +1 and -1 of the (product) contrast.
PLANTED = {"R": 0.30, "O": 0.10, "D": -0.20, "RO": 0.15, "RD": -0.05, "OD": 0.08, "ROD": 0.12}


def _synthetic(rounds: int, sigma: float, seed: int, planted: dict[str, float] | None = None) -> list[Any]:
    from demo_bench.application.matrix import Observation

    planted = PLANTED if planted is None else planted
    rng = random.Random(seed)
    obs = []
    for rnd in range(rounds):
        for r, o, d in itertools.product((0, 1), repeat=3):
            x = {"R": 2 * r - 1, "O": 2 * o - 1, "D": 2 * d - 1}
            log_y = math.log(5.0)
            for key, eff in planted.items():
                term = 1
                for ch in key:
                    term *= x[ch]
                log_y += (eff / 2.0) * term  # +-1 coding: contrast difference == eff
            log_y += rng.gauss(0.0, sigma)
            obs.append(Observation(r=r, o=o, d=d, round=rnd, kpi=math.exp(log_y)))
    return obs


def test_noiseless_data_recovers_planted_effects_exactly() -> None:
    from demo_bench.application.matrix import matrix_effects

    res = matrix_effects(_synthetic(3, 0.0, 0), n_boot=200, seed=1)
    assert set(res) == set(KEYS)
    for k in KEYS:
        assert res[k].estimate == pytest.approx(PLANTED[k], abs=1e-9)
        assert res[k].ratio == pytest.approx(math.exp(PLANTED[k]), rel=1e-9)


def test_noisy_data_recovers_effects_within_tolerance() -> None:
    from demo_bench.application.matrix import matrix_effects

    res = matrix_effects(_synthetic(12, 0.03, 42), n_boot=300, seed=1)
    for k in KEYS:
        assert res[k].estimate == pytest.approx(PLANTED[k], abs=0.03), k


def test_bootstrap_ci_contains_planted_value_most_of_the_time() -> None:
    from demo_bench.application.matrix import matrix_effects

    covered = total = 0
    for s in range(30):
        res = matrix_effects(_synthetic(10, 0.03, 1000 + s), n_boot=300, seed=s)
        for k in KEYS:
            assert res[k].ci_low <= res[k].ci_high
            covered += res[k].ci_low <= PLANTED[k] <= res[k].ci_high
            total += 1
    assert covered / total >= 0.75, f"coverage {covered}/{total}"


def test_ci_excludes_zero_for_large_effect_and_mostly_covers_zero_for_null_effects() -> None:
    from demo_bench.application.matrix import matrix_effects

    planted = {"R": 0.5, "O": 0.0, "D": 0.0, "RO": 0.0, "RD": 0.0, "OD": 0.0, "ROD": 0.0}
    nulls = [k for k in KEYS if k != "R"]
    covered = total = 0
    for s in range(20):
        res = matrix_effects(_synthetic(10, 0.02, 500 + s, planted), n_boot=300, seed=s)
        assert res["R"].ci_low > 0
        for k in nulls:
            covered += res[k].ci_low <= 0 <= res[k].ci_high
            total += 1
    assert covered / total >= 0.75, f"null coverage {covered}/{total}"


def test_seeded_bootstrap_is_deterministic() -> None:
    from demo_bench.application.matrix import matrix_effects

    obs = _synthetic(5, 0.05, 9)
    a = matrix_effects(obs, n_boot=200, seed=5)
    b = matrix_effects(obs, n_boot=200, seed=5)
    assert {k: (v.ci_low, v.ci_high) for k, v in a.items()} == {
        k: (v.ci_low, v.ci_high) for k, v in b.items()}


def test_missing_cell_raises() -> None:
    from demo_bench.application.matrix import matrix_effects

    obs = [o for o in _synthetic(3, 0.0, 0) if not (o.r == 1 and o.o == 1 and o.d == 1)]
    with pytest.raises(ValueError, match="cell"):
        matrix_effects(obs, n_boot=50, seed=0)


def test_non_positive_kpi_raises() -> None:
    from demo_bench.application.matrix import Observation, matrix_effects

    obs = _synthetic(3, 0.0, 0)
    obs[0] = Observation(r=obs[0].r, o=obs[0].o, d=obs[0].d, round=obs[0].round, kpi=0.0)
    with pytest.raises(ValueError):
        matrix_effects(obs, n_boot=50, seed=0)


@settings(max_examples=25, deadline=None)
@given(effects=st.lists(st.floats(-1.0, 1.0), min_size=7, max_size=7), seed=st.integers(0, 1000))
def test_property_noiseless_recovery_for_any_planted_effects(
    effects: list[float], seed: int
) -> None:
    from demo_bench.application.matrix import matrix_effects

    planted = dict(zip(KEYS, effects, strict=True))
    res = matrix_effects(_synthetic(3, 0.0, seed, planted), n_boot=50, seed=seed)
    for k in KEYS:
        assert res[k].estimate == pytest.approx(planted[k], abs=1e-9)
