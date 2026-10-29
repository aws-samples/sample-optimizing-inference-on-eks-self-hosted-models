# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Matrix run schedule: rounds x topologies x 2 router arms (spec 10.2).

Full plan (no subset): each round visits the 4 topologies in a different order (rows of a 4x4
Williams Latin square, so every topology precedes every other once across 4 rounds) and runs both
R arms back to back on one deploy. The first arm of every topology alternates between consecutive
rounds, and within a round half of the topologies start with ``rr``.

Subset plan (``topologies=[...]``): rounds are chained so the last topology of round ``r`` is the
first of round ``r + 1`` and is not redeployed, giving the minimum ``k * R - (R - 1)`` deploys for
``k`` topologies and ``R`` rounds. Round 1 visits the topologies in the order they are listed;
later rounds alternate reversed/listed (so consecutive rounds differ for ``k >= 2``), and the
first-arm rule is the same as in the full plan.

Sweep plan (``run_sweep_schedule``): the subset plan's chained topology blocks, each one
running every concurrency level (ascending) on both router arms.
"""

from __future__ import annotations

import itertools
from collections.abc import Sequence
from dataclasses import dataclass

from demo_bench.domain.scenario import ROUTERS, TOPOLOGIES, Router, Topology, scenario_for

_NAMES: tuple[str, ...] = tuple(t.name for t in TOPOLOGIES)

# Williams design for n=4 over topology indices (t-agg, t-agg-hic, t-dis, t-dis-hic).
_WILLIAMS: tuple[tuple[int, ...], ...] = ((0, 1, 3, 2), (1, 2, 0, 3), (2, 3, 1, 0), (3, 0, 2, 1))
_ORDERS: tuple[tuple[int, ...], ...] = _WILLIAMS + tuple(
    p for p in itertools.permutations(range(len(TOPOLOGIES))) if p not in _WILLIAMS
)


@dataclass(frozen=True)
class PlannedRun:
    """One planned matrix run.

    Attributes:
        round: Round number, 1-based.
        topology: Topology id (``t-agg``, ``t-agg-hic``, ``t-dis``, ``t-dis-hic``).
        router: Router arm (``rr`` or ``kv``).
        scenario: Scenario id ``S0``..``S7``.
        new_deploy: Whether this run starts with a topology deploy (first arm of the pair).
    """

    round: int
    topology: str
    router: Router
    scenario: str
    new_deploy: bool

    @property
    def namespace(self) -> str:
        """Namespace of the topology's DGD."""
        return next(t.namespace for t in TOPOLOGIES if t.name == self.topology)


def _arms(topology_index: int, round_index: int) -> tuple[Router, Router]:
    first = ROUTERS[(topology_index + round_index) % 2]
    second = ROUTERS[(topology_index + round_index + 1) % 2]
    return (first, second)


def _resolve_subset(topologies: Sequence[str]) -> list[int]:
    """Canonical indices of a topology subset, in the listed order (the round-1 order).

    Raises:
        ValueError: On an empty list, an unknown (case-sensitive) name or a duplicate.
    """
    names = list(topologies)
    if not names:
        raise ValueError("topologies must name at least one topology")
    unknown = [n for n in names if n not in _NAMES]
    if unknown:
        raise ValueError(f"unknown topology {unknown}; expected a subset of {list(_NAMES)}")
    if len(set(names)) != len(names):
        raise ValueError(f"duplicate topology in {names}")
    return [_NAMES.index(n) for n in names]


def _full_orders(rounds: int) -> list[tuple[int, ...]]:
    return [_ORDERS[r % len(_ORDERS)] for r in range(rounds)]


def _chained_orders(indices: list[int], rounds: int) -> list[tuple[int, ...]]:
    forward = tuple(indices)
    return [forward if r % 2 == 0 else forward[::-1] for r in range(rounds)]


def run_schedule(rounds: int = 3, topologies: Sequence[str] | None = None) -> list[PlannedRun]:
    """Plan the matrix runs.

    Args:
        rounds: Number of rounds.
        topologies: Optional subset of topology ids (``t-agg``, ``t-agg-hic``, ``t-dis``,
            ``t-dis-hic``). ``None`` keeps the full 4-topology plan unchanged.

    Returns:
        ``2 * k * rounds`` runs in execution order (``k`` = number of topologies); deterministic.
        ``new_deploy`` is true exactly when the topology changes from the previous run.

    Raises:
        ValueError: If ``rounds`` < 1 or the subset is empty, unknown or duplicated.
    """
    if rounds < 1:
        raise ValueError(f"rounds must be >= 1, got {rounds}")
    orders = (
        _full_orders(rounds)
        if topologies is None
        else _chained_orders(_resolve_subset(topologies), rounds)
    )
    runs: list[PlannedRun] = []
    previous: str | None = None
    for round_index, order in enumerate(orders):
        for topology_index in order:
            topology: Topology = TOPOLOGIES[topology_index]
            for arm_index, router in enumerate(_arms(topology_index, round_index)):
                # Full plan: one deploy per (round, topology). Subset: chained rounds, deploy only
                # when the topology changes.
                new_deploy = arm_index == 0 if topologies is None else topology.name != previous
                runs.append(
                    PlannedRun(
                        round=round_index + 1,
                        topology=topology.name,
                        router=router,
                        scenario=scenario_for(topology, router).id,
                        new_deploy=new_deploy,
                    )
                )
                previous = topology.name
    return runs


@dataclass(frozen=True)
class SweepRun(PlannedRun):
    """One planned closed-loop run of a concurrency sweep.

    Attributes:
        concurrency: aiperf ``--concurrency`` of the run.
    """

    concurrency: int


def _validate_concurrencies(concurrencies: Sequence[int]) -> list[int]:
    values = list(concurrencies)
    if not values:
        raise ValueError("concurrencies must contain at least one value")
    bad = [c for c in values if isinstance(c, bool) or not isinstance(c, int) or c < 1]
    if bad:
        raise ValueError(f"concurrencies must be integers >= 1, got {bad}")
    if len(set(values)) != len(values):
        raise ValueError(f"duplicate concurrency in {values}")
    return sorted(values)


def run_sweep_schedule(
    concurrencies: Sequence[int],
    *,
    rounds: int = 1,
    topologies: Sequence[str] | None = None,
) -> list[SweepRun]:
    """Plan a closed-loop concurrency sweep.

    Topology blocks are chained across rounds exactly as in the subset plan (the last topology of
    round ``r`` is the first of round ``r + 1`` and is not redeployed, so deploys =
    ``k * rounds - (rounds - 1)``). Inside a block the concurrencies ascend and, for each one,
    both router arms run back to back in the round's first-arm order for that topology, so
    consecutive rows of a block always switch arm.

    Args:
        concurrencies: Concurrency levels (input order is irrelevant).
        rounds: Number of rounds (reps).
        topologies: Topology subset; ``None`` means all four.

    Returns:
        ``2 * k * len(concurrencies) * rounds`` runs in execution order; deterministic.

    Raises:
        ValueError: On empty, non-positive or duplicate concurrencies, ``rounds`` < 1 or an
            invalid topology subset.
    """
    levels = _validate_concurrencies(concurrencies)
    if rounds < 1:
        raise ValueError(f"rounds must be >= 1, got {rounds}")
    names = list(_NAMES) if topologies is None else list(topologies)
    orders = _chained_orders(_resolve_subset(names), rounds)
    runs: list[SweepRun] = []
    previous: str | None = None
    for round_index, order in enumerate(orders):
        for topology_index in order:
            topology: Topology = TOPOLOGIES[topology_index]
            arms = _arms(topology_index, round_index)
            for concurrency in levels:
                for router in arms:
                    runs.append(
                        SweepRun(
                            round=round_index + 1,
                            topology=topology.name,
                            router=router,
                            scenario=scenario_for(topology, router).id,
                            new_deploy=topology.name != previous,
                            concurrency=concurrency,
                        )
                    )
                    previous = topology.name
    return runs
