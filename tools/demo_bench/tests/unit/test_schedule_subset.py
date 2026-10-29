# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Tests for ``demo-bench schedule --topologies`` (e.g. t-agg + t-agg-hic, 3 rounds: 12 runs).

The subset plan is a proper schedule for the chosen topologies, not a filter of the full plan:
  * Guaranteed minimum number of topology deploys for ``k`` topologies and ``R`` rounds is
    ``k * R - (R - 1)``: consecutive rounds are chained so the last topology of round ``r`` is the
    first of round ``r + 1`` and is NOT redeployed (its first run is a ``toggle``, not a ``deploy``).
    The two-topology subset (k=2, R=3) therefore needs exactly 4 deploys (the filtered full plan needs 6).
  * ``--topologies`` order matters (amended, runbook): the listed order is the round-1 order and the following rounds
    alternate reversed/listed; the canonical argument order ``t-agg,t-agg-hic`` produces the same plan as before.
  * Output without ``--topologies`` is byte-identical to the pre-existing plan (golden fixtures).
  * The scheduler knows only the four topology ids; the in-pod variants (``t-dis-e``,
    ``t-dis-hic-e``) are manifest overlays of ``t-dis``/``t-dis-hic`` selected by
    ``PD_PLACEMENT``, so they are rejected here.
"""

from __future__ import annotations

import json
import os
import sys
from collections import Counter
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

import lib_proc

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
ALL = ["t-agg", "t-agg-hic", "t-dis", "t-dis-hic"]
SCENARIO = {("t-agg", "rr"): "S0", ("t-agg", "kv"): "S1", ("t-agg-hic", "rr"): "S2",
            ("t-agg-hic", "kv"): "S4", ("t-dis", "rr"): "S3", ("t-dis", "kv"): "S5",
            ("t-dis-hic", "rr"): "S6", ("t-dis-hic", "kv"): "S7"}
NAMESPACE = {"t-agg": "s-agg", "t-agg-hic": "s-agg-hic", "t-dis": "s-dis",
             "t-dis-hic": "s-dis-hic"}
BLOCK1 = ["t-agg", "t-agg-hic"]


def _plan(rounds: int, topologies: list[str] | None) -> list[Any]:
    from demo_bench.application.schedule import run_schedule

    return run_schedule(rounds=rounds, topologies=topologies)


def _cli(argv: list[str]) -> tuple[int, str]:
    """Run the in-process CLI; return (exit code, stdout)."""
    import contextlib
    import io

    from demo_bench.cli import main

    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            rc = int(main(argv))
    except SystemExit as exc:
        rc = int(exc.code or 0)
    return rc, buf.getvalue()


def _check_invariants(runs: list[Any], rounds: int, topologies: list[str]) -> None:
    k = len(topologies)
    assert len(runs) == 2 * k * rounds
    # every (scenario, rep) exactly once
    pairs = Counter((r.scenario, r.round) for r in runs)
    expected = {(SCENARIO[(t, a)], rd) for t in topologies for a in ("rr", "kv")
                for rd in range(1, rounds + 1)}
    assert set(pairs) == expected and set(pairs.values()) == {1}
    assert all(r.scenario == SCENARIO[(r.topology, r.router)] for r in runs)
    # rounds are executed in order
    assert [r.round for r in runs] == sorted(r.round for r in runs)
    # both arms of a (round, topology) are adjacent
    for i in range(0, len(runs), 2):
        a, b = runs[i], runs[i + 1]
        assert (a.round, a.topology) == (b.round, b.topology)
        assert {a.router, b.router} == {"rr", "kv"}
    # new_deploy iff the topology changes (or first run): chained rounds are not redeployed
    prev = None
    for r in runs:
        assert r.new_deploy == (prev is None or r.topology != prev), (r, prev)
        prev = r.topology
    assert sum(r.new_deploy for r in runs) == k * rounds - (rounds - 1)
    # first arm of every topology alternates between consecutive rounds
    first: dict[tuple[str, int], str] = {}
    for r in runs:
        first.setdefault((r.topology, r.round), r.router)
    for t in topologies:
        for rd in range(1, rounds):
            assert first[(t, rd)] != first[(t, rd + 1)], (t, rd)
    # topology order differs between consecutive rounds when there is a choice
    if k >= 2:
        orders = []
        for rd in range(1, rounds + 1):
            seen: list[str] = []
            for r in runs:
                if r.round == rd and r.topology not in seen:
                    seen.append(r.topology)
            orders.append(seen)
        for a, b in zip(orders, orders[1:], strict=False):
            assert a != b


# ============================================================================ backward compat
def test_default_plan_unchanged_golden_tsv(capsys: pytest.CaptureFixture[str]) -> None:
    for rounds in (3, 5):
        rc, out = _cli(["schedule", "--rounds", str(rounds)])
        assert rc == 0
        assert out == (FIXTURES / f"schedule_full_rounds{rounds}.tsv").read_text()


def test_default_plan_unchanged_golden_json() -> None:
    rc, out = _cli(["schedule", "--rounds", "2", "--format", "json"])
    assert rc == 0
    assert json.loads(out) == json.loads((FIXTURES / "schedule_full_rounds2.json").read_text())


def test_api_default_equals_topologies_none_and_keeps_24_runs_12_deploys() -> None:
    from demo_bench.application.schedule import run_schedule

    assert run_schedule(3) == run_schedule(3, topologies=None)
    runs = run_schedule(3)
    assert len(runs) == 24 and sum(r.new_deploy for r in runs) == 12


# ============================================================================ block 1
def test_block1_plan_is_12_runs_4_deploys_of_s0_s1_s2_s4() -> None:
    runs = _plan(3, BLOCK1)
    assert len(runs) == 12
    assert {r.scenario for r in runs} == {"S0", "S1", "S2", "S4"}
    assert {r.topology for r in runs} == set(BLOCK1)
    assert sum(r.new_deploy for r in runs) == 4  # guaranteed minimum 2*3-(3-1)
    _check_invariants(runs, 3, BLOCK1)


def test_block1_is_not_a_filter_of_the_full_plan() -> None:
    full = [r for r in _plan(3, None) if r.topology in BLOCK1]
    assert sum(r.new_deploy for r in full) == 6  # the naive filter needs 6 deploys
    assert sum(r.new_deploy for r in _plan(3, BLOCK1)) == 4


def test_block1_chains_boundary_topology_without_redeploy() -> None:
    runs = _plan(3, BLOCK1)
    orders = []
    for rd in (1, 2, 3):
        seen: list[str] = []
        for r in runs:
            if r.round == rd and r.topology not in seen:
                seen.append(r.topology)
        orders.append(seen)
    assert orders[0][-1] == orders[1][0] and orders[1][-1] == orders[2][0]
    for rd in (2, 3):
        first_of_round = next(r for r in runs if r.round == rd)
        assert first_of_round.new_deploy is False


def test_cli_block1_tsv_columns_and_modes() -> None:
    rc, out = _cli(["schedule", "--rounds", "3", "--topologies", "t-agg,t-agg-hic"])
    assert rc == 0
    rows = [ln.split("\t") for ln in out.splitlines()]
    assert len(rows) == 12 and all(len(r) == 6 for r in rows)  # round topology router scenario mode namespace
    runs = _plan(3, BLOCK1)
    for cols, run in zip(rows, runs, strict=True):
        assert cols == [str(run.round), run.topology, run.router, run.scenario,
                        "deploy" if run.new_deploy else "toggle", NAMESPACE[run.topology]]
    assert sum(1 for r in rows if r[4] == "deploy") == 4
    assert out.endswith("\n")


def test_cli_block1_json_format() -> None:
    rc, out = _cli(["schedule", "--rounds", "3", "--topologies", "t-agg,t-agg-hic",
                    "--format", "json"])
    assert rc == 0
    rows = json.loads(out)
    assert len(rows) == 12
    assert {"round", "topology", "router", "scenario", "new_deploy", "namespace"} <= set(rows[0])
    assert sum(1 for r in rows if r["new_deploy"]) == 4


# ============================================================================ other subsets
@pytest.mark.parametrize("topologies", [["t-agg"], ["t-dis"], ["t-dis", "t-dis-hic"],
                                        ["t-agg", "t-dis-hic"], ALL[:3], ALL])
@pytest.mark.parametrize("rounds", [1, 2, 3, 4])
def test_subset_invariants(topologies: list[str], rounds: int) -> None:
    _check_invariants(_plan(rounds, topologies), rounds, topologies)


def test_single_topology_is_one_deploy_with_alternating_first_arm() -> None:
    runs = _plan(3, ["t-dis"])
    assert len(runs) == 6 and sum(r.new_deploy for r in runs) == 1
    firsts = [runs[i].router for i in (0, 2, 4)]
    assert firsts[0] != firsts[1] != firsts[2]


def test_k4_via_flag_meets_the_guaranteed_minimum_of_10_deploys() -> None:
    runs = _plan(3, ALL)
    assert len(runs) == 24 and sum(r.new_deploy for r in runs) == 4 * 3 - 2


@settings(max_examples=60, deadline=None)
@given(subset=st.lists(st.sampled_from(ALL), min_size=1, max_size=4, unique=True),
       rounds=st.integers(1, 6))
def test_property_any_subset_any_rounds(subset: list[str], rounds: int) -> None:
    _check_invariants(_plan(rounds, subset), rounds, subset)


def _round_orders(runs: list[Any], rounds: int) -> list[list[str]]:
    out = []
    for rd in range(1, rounds + 1):
        seen: list[str] = []
        for r in runs:
            if r.round == rd and r.topology not in seen:
                seen.append(r.topology)
        out.append(seen)
    return out


@settings(max_examples=30, deadline=None)
@given(subset=st.lists(st.sampled_from(ALL), min_size=2, max_size=4, unique=True),
       rounds=st.integers(1, 5))
def test_property_listing_order_sets_the_first_round_order(subset: list[str], rounds: int) -> None:
    """Amended (runbook: the 5 h sweep must start on t-agg-hic, where block 1 ends): the topologies come out in the order
    they are listed in round 1 and alternate listed/reversed in the following rounds; every other invariant holds."""
    runs = _plan(rounds, subset)
    _check_invariants(runs, rounds, subset)
    for rd, seen in enumerate(_round_orders(runs, rounds)):
        assert seen == (subset if rd % 2 == 0 else subset[::-1]), (rd, seen)


@pytest.mark.parametrize("rounds", [1, 2, 3])
def test_hic_first_chain(rounds: int) -> None:
    runs = _plan(rounds, ["t-agg-hic", "t-agg"])
    assert runs[0].topology == "t-agg-hic" and runs[0].new_deploy
    assert sum(r.new_deploy for r in runs) == 2 * rounds - (rounds - 1), "rounds stay chained: no redeploy at a boundary"
    assert runs[-1].topology == ("t-agg" if rounds % 2 else "t-agg-hic")


def test_canonical_listing_keeps_the_existing_plan_byte_for_byte() -> None:
    """`t-agg,t-agg-hic` (the block 1 argument order) still yields t-agg first and the same rows as before."""
    runs = _plan(3, ["t-agg", "t-agg-hic"])
    assert [r.topology for r in runs[:2]] == ["t-agg", "t-agg"]
    assert _plan(3, ["t-agg", "t-agg-hic"]) == _plan(3, BLOCK1)
    assert runs[-1].topology == "t-agg-hic", "plans/block1 ends on t-agg-hic"


def test_duplicates_and_unknown_names_are_still_rejected_in_any_order() -> None:
    for bad in (["t-agg-hic", "t-agg-hic"], ["t-agg-hic", "t-nope"], []):
        with pytest.raises(ValueError):
            _plan(1, bad)


# ============================================================================ sweep: topology order
SWEEP_LEVELS = [1, 4, 8, 16, 32, 64, 128]


def _sweep_rows(topologies: list[str]) -> list[Any]:
    from demo_bench.application.schedule import run_sweep_schedule

    return run_sweep_schedule(SWEEP_LEVELS, rounds=1, topologies=topologies)


def test_sweep_hic_first_lists_the_hic_block_first_and_keeps_two_deploys() -> None:
    """Runbook deviation made default: the 28-row sweep starts on t-agg-hic (block 1 ends there) -> 2 deploys, not 3."""
    rows = _sweep_rows(["t-agg-hic", "t-agg"])
    assert len(rows) == 28
    assert [r.topology for r in rows[:14]] == ["t-agg-hic"] * 14 and [r.topology for r in rows[14:]] == ["t-agg"] * 14
    assert sum(r.new_deploy for r in rows) == 2 and rows[0].new_deploy and rows[14].new_deploy
    assert [r.concurrency for r in rows[:14]] == [c for c in SWEEP_LEVELS for _ in (0, 1)]


def test_sweep_blocks_are_the_same_rows_whatever_the_order() -> None:
    """Only the ORDER of the two blocks changes: arms, levels, scenarios and deploy flags inside each block are as before."""
    hic_first, agg_first = _sweep_rows(["t-agg-hic", "t-agg"]), _sweep_rows(["t-agg", "t-agg-hic"])
    for name in ("t-agg-hic", "t-agg"):
        assert [r for r in hic_first if r.topology == name] == [r for r in agg_first if r.topology == name]
    assert agg_first[0].topology == "t-agg"


def test_cli_sweep_honours_the_topologies_order() -> None:
    rc, out = _cli(["schedule", "--rounds", "1", "--topologies", "t-agg-hic,t-agg", "--sweep-concurrency", ",".join(map(str, SWEEP_LEVELS))])
    assert rc == 0
    lines = [ln.split("\t") for ln in out.splitlines() if ln.strip()]
    assert len(lines) == 28
    assert lines[0] == ["1", "t-agg-hic", "kv", "S4", "deploy", "s-agg-hic", "concurrency=1"]
    assert lines[13] == ["1", "t-agg-hic", "rr", "S2", "toggle", "s-agg-hic", "concurrency=128"]
    assert lines[14] == ["1", "t-agg", "rr", "S0", "deploy", "s-agg", "concurrency=1"]
    assert lines[27] == ["1", "t-agg", "kv", "S1", "toggle", "s-agg", "concurrency=128"]


def test_cli_sweep_default_argument_order_is_unchanged() -> None:
    rc, out = _cli(["schedule", "--rounds", "1", "--topologies", "t-agg,t-agg-hic", "--sweep-concurrency", ",".join(map(str, SWEEP_LEVELS))])
    assert rc == 0
    lines = [ln.split("\t") for ln in out.splitlines() if ln.strip()]
    assert lines[0] == ["1", "t-agg", "rr", "S0", "deploy", "s-agg", "concurrency=1"]
    assert lines[14] == ["1", "t-agg-hic", "kv", "S4", "deploy", "s-agg-hic", "concurrency=1"]
    assert lines[27] == ["1", "t-agg-hic", "rr", "S2", "toggle", "s-agg-hic", "concurrency=128"]


def test_cli_plan_without_sweep_also_honours_the_order() -> None:
    rc, out = _cli(["schedule", "--rounds", "1", "--topologies", "t-agg-hic,t-agg"])
    assert rc == 0 and out.splitlines()[0].split("\t")[1] == "t-agg-hic"


# ============================================================================ determinism
def test_plan_is_deterministic() -> None:
    assert _plan(3, BLOCK1) == _plan(3, BLOCK1)


def test_cli_output_independent_of_pythonhashseed() -> None:
    outs = set()
    for hs in ("1", "2", "random"):
        proc = lib_proc.run(
            [sys.executable, "-m", "demo_bench", "schedule", "--rounds", "3", "--topologies",
             "t-agg,t-agg-hic"],
            capture_output=True, text=True, timeout=120, check=False,
            env={**os.environ, "PYTHONHASHSEED": hs})
        assert proc.returncode == 0, proc.stderr
        outs.add(proc.stdout)
    assert len(outs) == 1
    assert len(outs.pop().splitlines()) == 12


# ============================================================================ invalid input
@pytest.mark.parametrize("bad", [["t-nope"], ["t-agg", "t-nope"], ["T-AGG"], [""],
                                 ["t-agg", "t-agg"], ["t-dis-e"], ["t-dis-hic-e"], []])
def test_api_rejects_invalid_topologies(bad: list[str]) -> None:
    with pytest.raises(ValueError):
        _plan(3, bad)


@pytest.mark.parametrize("arg", ["t-nope", "t-agg,t-nope", "T-AGG", "", "t-agg,",
                                 ",t-agg", "t-agg,t-agg", "t-dis-e", "t-dis-hic-e"])
def test_cli_invalid_topologies_exit_2(arg: str) -> None:
    rc_ok, out_ok = _cli(["schedule", "--rounds", "3", "--topologies", "t-agg,t-agg-hic"])
    assert rc_ok == 0 and out_ok  # positive control: the option exists and works
    rc, out = _cli(["schedule", "--rounds", "3", "--topologies", arg])
    assert rc == 2
    assert out == ""  # no partial plan on stdout


def test_rounds_still_validated_with_topologies() -> None:
    rc_ok, _ = _cli(["schedule", "--rounds", "3", "--topologies", "t-agg"])
    assert rc_ok == 0
    rc, _ = _cli(["schedule", "--rounds", "0", "--topologies", "t-agg"])
    assert rc == 2
