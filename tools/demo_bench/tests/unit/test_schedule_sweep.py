# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Tests for ``demo-bench schedule --sweep-concurrency`` (closed-loop concurrency sweep, block 1).

Contract (output stays the 6 legacy TSV columns, plus a 7th ``concurrency=<N>``, so consumers that
index columns 0/1/3 as round/topology/scenario keep working):

    round  topology  router  scenario  mode  namespace  concurrency=<N>

Ordering: rounds (reps) in order; topologies chained exactly as in ``--topologies`` (the last
topology of round r is the first of round r+1, no redeploy, so deploys = k*R - (R-1)); inside a
topology deployment the concurrencies run in ascending order and, for each concurrency, the two
router arms are adjacent, in the round's first-arm order for that topology, so consecutive rows
of a block always switch arm. Without ``--sweep-concurrency`` the output is byte-identical to the
legacy plan (golden fixtures; see test_schedule_subset.py).
"""

from __future__ import annotations

import contextlib
import io
import json
from collections import Counter
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
ALL = ["t-agg", "t-agg-hic", "t-dis", "t-dis-hic"]
SCENARIO = {("t-agg", "rr"): "S0", ("t-agg", "kv"): "S1", ("t-agg-hic", "rr"): "S2",
            ("t-agg-hic", "kv"): "S4", ("t-dis", "rr"): "S3", ("t-dis", "kv"): "S5",
            ("t-dis-hic", "rr"): "S6", ("t-dis-hic", "kv"): "S7"}
NAMESPACE = {"t-agg": "s-agg", "t-agg-hic": "s-agg-hic", "t-dis": "s-dis",
             "t-dis-hic": "s-dis-hic"}
CONC = [1, 4, 8, 16, 32, 64, 128]
BLOCK1 = ["t-agg", "t-agg-hic"]


def _sweep(conc: list[int], rounds: int = 1, topologies: list[str] | None = None) -> list[Any]:
    from demo_bench.application.schedule import run_sweep_schedule

    return run_sweep_schedule(conc, rounds=rounds, topologies=topologies)


def _cli(argv: list[str]) -> tuple[int, str]:
    from demo_bench.cli import main

    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            rc = int(main(argv))
    except SystemExit as exc:
        rc = int(exc.code or 0)
    return rc, buf.getvalue()


def _check(rows: list[Any], conc: list[int], rounds: int, topologies: list[str]) -> None:
    k, n = len(topologies), len(conc)
    assert len(rows) == 2 * k * n * rounds
    # every (scenario, concurrency, rep) exactly once
    seen = Counter((r.scenario, r.concurrency, r.round) for r in rows)
    expected = {(SCENARIO[(t, a)], c, rd) for t in topologies for a in ("rr", "kv")
                for c in conc for rd in range(1, rounds + 1)}
    assert set(seen) == expected and set(seen.values()) == {1}
    assert all(r.scenario == SCENARIO[(r.topology, r.router)] for r in rows)
    assert all(r.namespace == NAMESPACE[r.topology] for r in rows)
    assert [r.round for r in rows] == sorted(r.round for r in rows)
    # blocks = maximal runs of the same (round, topology); concurrency ascending inside a block,
    # both arms adjacent per concurrency, arm differs from the previous row
    i = 0
    while i < len(rows):
        j = i
        while j < len(rows) and (rows[j].round, rows[j].topology) == (rows[i].round, rows[i].topology):
            j += 1
        block = rows[i:j]
        assert len(block) == 2 * n
        assert [b.concurrency for b in block[0::2]] == sorted(conc)
        for p in range(0, len(block), 2):
            assert block[p].concurrency == block[p + 1].concurrency
            assert {block[p].router, block[p + 1].router} == {"rr", "kv"}
        assert all(a.router != b.router for a, b in zip(block, block[1:], strict=False))
        i = j
    # deploys: a new deployment only when the topology changes
    prev = None
    for r in rows:
        assert r.new_deploy == (prev is None or r.topology != prev)
        prev = r.topology
    assert sum(r.new_deploy for r in rows) == k * rounds - (rounds - 1)
    # first arm of every topology alternates between consecutive rounds
    first: dict[tuple[str, int], str] = {}
    for r in rows:
        first.setdefault((r.topology, r.round), r.router)
    for t in topologies:
        for rd in range(1, rounds):
            assert first[(t, rd)] != first[(t, rd + 1)]


# ============================================================================ API
def test_block1_sweep_28_rows_2_deploys() -> None:
    rows = _sweep(CONC, rounds=1, topologies=BLOCK1)
    assert len(rows) == 28 and sum(r.new_deploy for r in rows) == 2
    assert {r.scenario for r in rows} == {"S0", "S1", "S2", "S4"}
    _check(rows, CONC, 1, BLOCK1)


@pytest.mark.parametrize("rounds", [1, 2, 3])
@pytest.mark.parametrize("topologies", [["t-agg"], BLOCK1, ["t-dis", "t-dis-hic"], ALL])
def test_sweep_invariants(topologies: list[str], rounds: int) -> None:
    _check(_sweep([1, 8, 64], rounds, topologies), [1, 8, 64], rounds, topologies)


def test_sweep_default_topologies_is_all_four() -> None:
    rows = _sweep([4, 16])
    assert {r.topology for r in rows} == set(ALL)
    _check(rows, [4, 16], 1, ALL)


def test_sweep_concurrency_input_order_is_irrelevant_output_ascending() -> None:
    assert _sweep([64, 1, 16, 4], topologies=BLOCK1) == _sweep([1, 4, 16, 64], topologies=BLOCK1)


@settings(max_examples=40, deadline=None)
@given(conc=st.lists(st.integers(1, 512), min_size=1, max_size=8, unique=True),
       subset=st.lists(st.sampled_from(ALL), min_size=1, max_size=4, unique=True),
       rounds=st.integers(1, 4))
def test_property_sweep(conc: list[int], subset: list[str], rounds: int) -> None:
    _check(_sweep(conc, rounds, subset), conc, rounds, subset)


@pytest.mark.parametrize("bad", [[], [0], [-1], [1, 1], [2, 4, 4]])
def test_api_rejects_invalid_concurrencies(bad: list[int]) -> None:
    with pytest.raises(ValueError):
        _sweep(bad, topologies=BLOCK1)


def test_api_rejects_bad_topologies_and_rounds() -> None:
    with pytest.raises(ValueError):
        _sweep([1], topologies=["t-nope"])
    with pytest.raises(ValueError):
        _sweep([1], rounds=0, topologies=BLOCK1)


def test_sweep_is_deterministic() -> None:
    assert _sweep(CONC, 2, BLOCK1) == _sweep(CONC, 2, BLOCK1)


# ============================================================================ CLI
def test_cli_sweep_tsv_has_legacy_columns_plus_concurrency() -> None:
    rc, out = _cli(["schedule", "--sweep-concurrency", "1,4,8,16,32,64,128", "--topologies",
                    "t-agg,t-agg-hic", "--rounds", "1"])
    assert rc == 0 and out.endswith("\n")
    rows = [ln.split("\t") for ln in out.splitlines()]
    assert len(rows) == 28 and all(len(r) == 7 for r in rows)
    expect = _sweep(CONC, 1, BLOCK1)
    for cols, run in zip(rows, expect, strict=True):
        assert cols == [str(run.round), run.topology, run.router, run.scenario,
                        "deploy" if run.new_deploy else "toggle", NAMESPACE[run.topology],
                        f"concurrency={run.concurrency}"]
    assert sum(1 for r in rows if r[4] == "deploy") == 2


def test_cli_sweep_json_has_integer_concurrency() -> None:
    rc, out = _cli(["schedule", "--sweep-concurrency", "1,8", "--topologies", "t-agg",
                    "--rounds", "1", "--format", "json"])
    assert rc == 0
    rows = json.loads(out)
    assert len(rows) == 4
    assert {"round", "topology", "router", "scenario", "new_deploy", "namespace",
            "concurrency"} <= set(rows[0])
    assert [r["concurrency"] for r in rows] == [1, 1, 8, 8]


def test_cli_sweep_without_topologies_uses_all_four() -> None:
    rc, out = _cli(["schedule", "--sweep-concurrency", "2,4", "--rounds", "1"])
    assert rc == 0
    rows = [ln.split("\t") for ln in out.splitlines()]
    assert len(rows) == 16 and {r[1] for r in rows} == set(ALL)


def test_cli_without_sweep_flag_is_byte_identical_to_legacy() -> None:
    rc, out = _cli(["schedule", "--rounds", "3"])
    assert rc == 0 and out == (FIXTURES / "schedule_full_rounds3.tsv").read_text()


@pytest.mark.parametrize("arg", ["0", "-1", "a", "1,,4", "", "1,1", "1.5", "1,4,", ",1", "1;4"])
def test_cli_invalid_sweep_concurrency_exit_2(arg: str) -> None:
    rc_ok, out_ok = _cli(["schedule", "--sweep-concurrency", "1,4", "--topologies", "t-agg",
                          "--rounds", "1"])
    assert rc_ok == 0 and out_ok  # positive control: the option exists
    rc, out = _cli(["schedule", "--sweep-concurrency", arg, "--topologies", "t-agg",
                    "--rounds", "1"])
    assert rc == 2 and out == ""


def test_cli_sweep_validates_topologies_and_rounds() -> None:
    assert _cli(["schedule", "--sweep-concurrency", "1", "--topologies", "t-agg"])[0] == 0
    assert _cli(["schedule", "--sweep-concurrency", "1", "--topologies", "t-nope"])[0] == 2
    assert _cli(["schedule", "--sweep-concurrency", "1", "--topologies", "t-agg",
                 "--rounds", "0"])[0] == 2
