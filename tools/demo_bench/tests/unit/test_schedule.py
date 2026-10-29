# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests for ``application.schedule.run_schedule`` (spec 10.2)."""

from __future__ import annotations

from collections import defaultdict

TOPOLOGIES = {"t-agg", "t-agg-hic", "t-dis", "t-dis-hic"}
SCENARIO = {("t-agg", "rr"): "S0", ("t-agg", "kv"): "S1", ("t-agg-hic", "rr"): "S2",
            ("t-agg-hic", "kv"): "S4", ("t-dis", "rr"): "S3", ("t-dis", "kv"): "S5",
            ("t-dis-hic", "rr"): "S6", ("t-dis-hic", "kv"): "S7"}


def test_totals_24_runs_12_deploys() -> None:
    from demo_bench.application.schedule import run_schedule

    runs = run_schedule()
    assert len(runs) == 24
    assert sum(1 for r in runs if r.new_deploy) == 12


def test_each_round_covers_all_topologies_with_both_arms() -> None:
    from demo_bench.application.schedule import run_schedule

    by_round: dict[int, list[tuple[str, str]]] = defaultdict(list)
    for r in run_schedule(rounds=3):
        by_round[r.round].append((r.topology, r.router))
    assert sorted(by_round) == [1, 2, 3]
    for pairs in by_round.values():
        assert len(pairs) == 8
        assert {t for t, _ in pairs} == TOPOLOGIES
        assert {(t, a) for t in TOPOLOGIES for a in ("rr", "kv")} == set(pairs)


def test_runs_of_a_topology_are_adjacent_one_deploy_each() -> None:
    from demo_bench.application.schedule import run_schedule

    runs = run_schedule()
    for i in range(0, 24, 2):
        a, b = runs[i], runs[i + 1]
        assert (a.round, a.topology) == (b.round, b.topology)
        assert a.new_deploy and not b.new_deploy
        assert a.router != b.router


def test_topology_order_differs_in_every_round() -> None:
    from demo_bench.application.schedule import run_schedule

    orders = []
    for rnd in (1, 2, 3):
        seen: list[str] = []
        for r in run_schedule():
            if r.round == rnd and r.topology not in seen:
                seen.append(r.topology)
        orders.append(tuple(seen))
    assert len(set(orders)) == 3


def test_router_arm_order_alternates_across_rounds_for_every_topology() -> None:
    from demo_bench.application.schedule import run_schedule

    first: dict[str, dict[int, str]] = defaultdict(dict)
    for r in run_schedule():
        first[r.topology].setdefault(r.round, r.router)
    for topo, per_round in first.items():
        assert per_round[1] != per_round[2], topo
        assert per_round[2] != per_round[3], topo


def test_scenario_ids_match_spec_matrix() -> None:
    from demo_bench.application.schedule import run_schedule

    for r in run_schedule():
        assert r.scenario == SCENARIO[(r.topology, r.router)]


def test_each_scenario_runs_once_per_round() -> None:
    from demo_bench.application.schedule import run_schedule

    counts: dict[str, int] = defaultdict(int)
    for r in run_schedule():
        counts[r.scenario] += 1
    assert set(counts.values()) == {3} and len(counts) == 8


def test_deterministic() -> None:
    from demo_bench.application.schedule import run_schedule

    assert run_schedule() == run_schedule()
