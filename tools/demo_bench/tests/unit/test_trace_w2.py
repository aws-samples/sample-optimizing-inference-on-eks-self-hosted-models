# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""RED contract for ``demo-bench trace w2`` (Flash tooling, item b): the W2 agentic trace.

Plan (W2 trace, section 4.2): agentic sessions that share a global prefix, enter
with a large context and grow per turn; idle gaps are lognormal; 8-10 % of the lines are cold
ingestions with unique ``hash_ids``; the OSL is fixed or sampled from an empirical list;
arrivals are open loop (pre-computed ``timestamp`` ms); a sidecar maps every line to its
session and class; everything is deterministic by seed and the sha256 is recorded.

Module ``demo_bench.application.trace_w2`` (stdlib only):

* ``generate_w2_trace(*, sessions, turns, global_prefix_tokens, entry_tokens, growth_tokens,
  rate_per_s, gap_sigma=1.2, populate_s, cold_fraction, cold_min_tokens, cold_max_tokens,
  osl, osl_cap=12000, max_isl=200000, seed, block_size=512) -> W2Trace``
  (``W2Trace.rows`` list of dicts, ``W2Trace.sidecar`` list of dicts). All keyword-only.
* Session rows: ``sessions * turns``. Turn 0 is the ``entry`` (``input_length ==
  entry_tokens``); turn k is ``entry_tokens + k * growth_tokens`` and its ``hash_ids`` extend
  turn k-1 exactly. Every session row starts with the same ``global_prefix_tokens //
  block_size`` ids; the rest is private to the session (disjoint across sessions).
* Entry timestamps are spread (seeded) over ``[0, populate_s * 1000)`` ms. Every later turn
  comes ``gap`` ms after the previous turn of the same session (>= 1 ms), with ``gap``
  lognormal: ``sigma = gap_sigma`` and mean ``sessions / rate_per_s`` seconds (the aggregate
  continuation rate is ``rate_per_s`` turns/s).
* Cold ingestions: ``round(cold_fraction / (1 - cold_fraction) * sessions * turns)`` extra rows,
  ``input_length`` in ``[cold_min_tokens, cold_max_tokens]`` (multiple of ``block_size``),
  ``hash_ids`` never seen in any other row (not even the global prefix), timestamps spread over
  the whole trace span.
* ``osl`` is an int (every row) or a non-empty sequence of ints (sampled with the seed, clipped
  at ``osl_cap``).
* Rows have exactly ``timestamp``, ``input_length``, ``output_length``, ``hash_ids`` and are
  sorted by ``timestamp`` (ties by session; cold last). The sidecar has one entry per row:
  ``line`` (index in rows), ``session`` (int, ``None`` for cold), ``turn`` (int, ``None`` for
  cold), ``class`` in ``entry`` / ``continuation`` / ``cold_ingestion`` and ``input_length``.
* ``ValueError`` for non-positive sizes/rate, ``gap_sigma < 0``, ``cold_fraction`` outside
  ``[0, 1)``, ``cold_min_tokens > cold_max_tokens``, sizes not multiples of ``block_size``,
  ``entry_tokens <= global_prefix_tokens``, an input longer than ``max_isl``, an empty ``osl``.
* CLI ``demo-bench trace w2 --out P --sidecar P --seed S --sessions N --turns T
  --global-prefix-tokens N --entry-tokens N --growth-tokens N --rate X [--gap-sigma X]
  --populate-s X --cold-fraction X --cold-min-tokens N --cold-max-tokens N
  (--osl N | --osl-file PATH) [--osl-cap N] [--max-isl N] [--block-size N]
  [--expect-sha256 HEX]``: writes both JSONL files (compact separators, one ``\\n`` per row),
  prints ``{"path", "rows", "sha256", "sidecar", "sidecar_sha256"}`` and exits 0. A trace whose
  sha256 differs from ``--expect-sha256`` exits 1 and leaves no file; invalid input exits 2 and
  leaves no file.
"""

from __future__ import annotations

import hashlib
import json
import math
import statistics
from collections import defaultdict
from pathlib import Path
from typing import Any

import pytest

KEYS = {"timestamp", "input_length", "output_length", "hash_ids"}
BLOCK = 512

BASE: dict[str, Any] = {
    "sessions": 21,
    "turns": 5,
    "global_prefix_tokens": 8192,
    "entry_tokens": 65536,
    "growth_tokens": 4096,
    "rate_per_s": 0.5,
    "gap_sigma": 1.2,
    "populate_s": 60.0,
    "cold_fraction": 0.09,
    "cold_min_tokens": 102400,
    "cold_max_tokens": 153600,
    "osl": 300,
    "seed": 7,
}


def _gen(**over: Any) -> Any:
    from demo_bench.application.trace_w2 import generate_w2_trace

    return generate_w2_trace(**{**BASE, **over})


def _cli(argv: list[str]) -> int:
    from demo_bench.cli import main

    try:
        return int(main(argv))
    except SystemExit as exc:
        return int(exc.code or 0)


def _sessions(trace: Any) -> dict[int, list[tuple[dict[str, Any], dict[str, Any]]]]:
    groups: dict[int, list[tuple[dict[str, Any], dict[str, Any]]]] = defaultdict(list)
    for row, side in zip(trace.rows, trace.sidecar, strict=True):
        if side["session"] is not None:
            groups[side["session"]].append((row, side))
    return {s: sorted(v, key=lambda p: p[1]["turn"]) for s, v in groups.items()}


# --------------------------------------------------------------------------- structure
def test_row_shape_and_counts() -> None:
    t = _gen()
    cold = [s for s in t.sidecar if s["class"] == "cold_ingestion"]
    assert len(t.rows) == len(t.sidecar) == 21 * 5 + len(cold)
    assert len(cold) == round(0.09 / 0.91 * 21 * 5)
    assert all(set(r) == KEYS for r in t.rows)
    assert all(isinstance(r["timestamp"], int) and r["timestamp"] >= 0 for r in t.rows)
    stamps = [r["timestamp"] for r in t.rows]
    assert stamps == sorted(stamps)
    assert [s["line"] for s in t.sidecar] == list(range(len(t.rows)))
    assert {s["class"] for s in t.sidecar} == {"entry", "continuation", "cold_ingestion"}


def test_cold_share_is_between_8_and_10_percent_for_the_plan_setting() -> None:
    t = _gen(sessions=229, turns=8, cold_fraction=0.09)
    share = sum(1 for s in t.sidecar if s["class"] == "cold_ingestion") / len(t.rows)
    assert 0.08 <= share <= 0.10


def test_cold_fraction_zero_has_no_cold_rows() -> None:
    t = _gen(cold_fraction=0.0)
    assert len(t.rows) == 21 * 5
    assert all(s["class"] != "cold_ingestion" for s in t.sidecar)


def test_session_context_grows_and_extends_previous_turn() -> None:
    t = _gen()
    g_blocks = 8192 // BLOCK
    for turns in _sessions(t).values():
        assert [s["turn"] for _, s in turns] == list(range(5))
        assert turns[0][1]["class"] == "entry"
        assert all(s["class"] == "continuation" for _, s in turns[1:])
        for k, (row, side) in enumerate(turns):
            assert row["input_length"] == 65536 + k * 4096 == side["input_length"]
            assert len(row["hash_ids"]) == row["input_length"] // BLOCK
            assert len(set(row["hash_ids"])) == len(row["hash_ids"])
            if k:
                prev = turns[k - 1][0]["hash_ids"]
                assert row["hash_ids"][: len(prev)] == prev
        assert g_blocks == 16


def test_global_prefix_shared_and_private_ids_disjoint_across_sessions() -> None:
    t = _gen()
    g_blocks = 8192 // BLOCK
    sess = _sessions(t)
    prefixes = {tuple(r["hash_ids"][:g_blocks]) for turns in sess.values() for r, _ in turns}
    assert len(prefixes) == 1
    shared = set(next(iter(prefixes)))
    private: dict[int, set[int]] = {
        s: {h for r, _ in turns for h in r["hash_ids"][g_blocks:]} for s, turns in sess.items()
    }
    for s, ids in private.items():
        assert not ids & shared
        for other, oids in private.items():
            if other != s:
                assert not ids & oids


def test_cold_ingestions_are_unique_and_in_range() -> None:
    t = _gen()
    sess_ids = {h for r, s in zip(t.rows, t.sidecar, strict=True)
                if s["class"] != "cold_ingestion" for h in r["hash_ids"]}
    seen: set[int] = set()
    for row, side in zip(t.rows, t.sidecar, strict=True):
        if side["class"] != "cold_ingestion":
            continue
        assert side["session"] is None and side["turn"] is None
        assert 102400 <= row["input_length"] <= 153600 and row["input_length"] % BLOCK == 0
        assert len(row["hash_ids"]) == row["input_length"] // BLOCK
        ids = set(row["hash_ids"])
        assert len(ids) == len(row["hash_ids"])
        assert not ids & sess_ids and not ids & seen
        seen |= ids


# --------------------------------------------------------------------------- timing
def test_entries_fall_in_the_populate_window_and_turns_are_strictly_ordered() -> None:
    t = _gen()
    for turns in _sessions(t).values():
        stamps = [r["timestamp"] for r, _ in turns]
        assert 0 <= stamps[0] < 60_000
        assert all(b > a for a, b in zip(stamps, stamps[1:], strict=False))


def test_gaps_are_lognormal_with_the_forced_mean() -> None:
    t = _gen(sessions=301, turns=6)
    gaps_s = []
    for turns in _sessions(t).values():
        stamps = [r["timestamp"] for r, _ in turns]
        gaps_s += [(b - a) / 1000 for a, b in zip(stamps, stamps[1:], strict=False)]
    assert len(gaps_s) == 301 * 5
    logs = [math.log(g) for g in gaps_s]
    assert statistics.pstdev(logs) == pytest.approx(1.2, abs=0.1)
    mean = statistics.fmean(gaps_s)
    assert mean == pytest.approx(301 / 0.5, rel=0.25)
    assert statistics.median(gaps_s) == pytest.approx(301 / 0.5 * math.exp(-1.2**2 / 2), rel=0.15)


def test_gap_sigma_zero_gives_constant_gaps() -> None:
    t = _gen(gap_sigma=0.0)
    gaps = {
        round((b[0]["timestamp"] - a[0]["timestamp"]) / 1000)
        for turns in _sessions(t).values()
        for a, b in zip(turns, turns[1:], strict=False)
    }
    assert gaps == {round(21 / 0.5)}


# --------------------------------------------------------------------------- OSL
def test_fixed_osl() -> None:
    assert {r["output_length"] for r in _gen().rows} == {300}


def test_empirical_osl_is_sampled_clipped_and_seeded() -> None:
    pool = [50, 399, 2400, 50000]
    t = _gen(osl=pool, osl_cap=12000)
    values = {r["output_length"] for r in t.rows}
    assert values <= {50, 399, 2400, 12000}
    assert len(values) > 1
    assert [r["output_length"] for r in t.rows] == [r["output_length"] for r in _gen(osl=pool).rows]


# --------------------------------------------------------------------------- determinism
def test_deterministic_and_seed_sensitive() -> None:
    a, b, c = _gen(), _gen(), _gen(seed=8)
    assert a.rows == b.rows and a.sidecar == b.sidecar
    assert a.rows != c.rows


@pytest.mark.parametrize(
    "over",
    [
        {"sessions": 0}, {"turns": 0}, {"growth_tokens": 0}, {"rate_per_s": 0.0},
        {"populate_s": 0.0}, {"gap_sigma": -0.1}, {"cold_fraction": 1.0},
        {"cold_fraction": -0.1}, {"cold_min_tokens": 160000},
        {"entry_tokens": 65537}, {"growth_tokens": 4097}, {"global_prefix_tokens": 8193},
        {"entry_tokens": 8192}, {"max_isl": 70000}, {"cold_max_tokens": 204800},
        {"osl": []}, {"osl": 0}, {"block_size": 0},
    ],
)
def test_invalid_arguments(over: dict[str, Any]) -> None:
    with pytest.raises((ValueError, TypeError)):
        _gen(**over)


# --------------------------------------------------------------------------- CLI
def _argv(tmp_path: Path, *extra: str) -> list[str]:
    return [
        "trace", "w2", "--out", str(tmp_path / "w2.jsonl"), "--sidecar", str(tmp_path / "w2.side"),
        "--seed", "7", "--sessions", "21", "--turns", "5", "--global-prefix-tokens", "8192",
        "--entry-tokens", "65536", "--growth-tokens", "4096", "--rate", "0.5",
        "--populate-s", "60", "--cold-fraction", "0.09", "--cold-min-tokens", "102400",
        "--cold-max-tokens", "153600", "--osl", "300", *extra,
    ]


def test_cli_roundtrip(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert _cli(_argv(tmp_path)) == 0
    info = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    trace = _gen()
    out, side = tmp_path / "w2.jsonl", tmp_path / "w2.side"
    assert info["rows"] == len(trace.rows)
    assert info["sha256"] == hashlib.sha256(out.read_bytes()).hexdigest()
    assert info["sidecar_sha256"] == hashlib.sha256(side.read_bytes()).hexdigest()
    lines = out.read_text().splitlines()
    assert [json.loads(x) for x in lines] == trace.rows
    assert [json.loads(x) for x in side.read_text().splitlines()] == trace.sidecar
    assert lines[0] == json.dumps(trace.rows[0], separators=(",", ":"))
    assert out.read_bytes().endswith(b"\n") and not out.read_bytes().endswith(b"\n\n")


def test_cli_osl_file(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    pool = tmp_path / "osl.json"
    pool.write_text(json.dumps([50, 399, 60000]))
    argv = _argv(tmp_path)
    argv[argv.index("--osl"):argv.index("--osl") + 2] = ["--osl-file", str(pool), "--osl-cap", "1000"]
    assert _cli(argv) == 0
    rows = [json.loads(x) for x in (tmp_path / "w2.jsonl").read_text().splitlines()]
    assert {r["output_length"] for r in rows} <= {50, 399, 1000}


def test_cli_expect_sha256(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert _cli(_argv(tmp_path)) == 0
    sha = json.loads(capsys.readouterr().out.strip().splitlines()[-1])["sha256"]
    assert _cli(_argv(tmp_path, "--expect-sha256", sha)) == 0
    for p in tmp_path.iterdir():
        p.unlink()
    assert _cli(_argv(tmp_path, "--expect-sha256", "0" * 64)) == 1
    assert not (tmp_path / "w2.jsonl").exists() and not (tmp_path / "w2.side").exists()


def test_cli_invalid_input_exits_2_and_writes_nothing(tmp_path: Path) -> None:
    ctl = tmp_path / "ctl"
    ctl.mkdir()
    assert _cli(_argv(ctl)) == 0  # control: the same invocation, valid
    argv = _argv(tmp_path)
    argv[argv.index("--cold-fraction") + 1] = "1.5"
    assert _cli(argv) == 2
    assert not (tmp_path / "w2.jsonl").exists() and not (tmp_path / "w2.side").exists()
