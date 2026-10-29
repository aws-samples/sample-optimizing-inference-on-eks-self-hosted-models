# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""RED contract for ``demo-bench trace multiturn`` (Track D WP6a): the multi-turn mooncake trace.

Workload (study 4.3, plan WP6): N sessions x T turns. Every request of every session starts with
the same ``S_sys``-token system prompt (shared leading ``hash_ids`` blocks); each session then has
a private context of ``C0`` tokens (``C0`` includes the system prompt) that grows by ``G`` tokens
per turn. Turn k+1 is the same conversation as turn k plus ``G`` new tokens, so its ``hash_ids``
extend turn k's EXACTLY (that is what makes the prefix cache reusable). Sessions share only the
system prompt. The OSL is fixed (``ignore_eos`` is left to aiperf). Arrivals are a fixed schedule:
turn k of session s is at ``phase_s + k * Tp`` ms.

Contract (module ``demo_bench.application.trace_multiturn``, stdlib only):

* ``generate_multiturn_trace(*, sessions=40, turns=8, system_tokens=2048, context_tokens=30720,
  growth_tokens=1536, output_tokens=128, turn_period_ms, seed, block_size=512)
  -> list[dict]``; ``turn_period_ms`` and ``seed`` have no default.
* Each row has exactly the keys ``timestamp`` (int, ms), ``input_length``, ``output_length``,
  ``hash_ids`` (no ``session_id``/``delay``/``messages``: aiperf would accumulate turns).
* Turn k: ``input_length = context_tokens + k * growth_tokens``; ``len(hash_ids) ==
  input_length // block_size``; ``output_length == output_tokens``.
* ``hash_ids[:system_tokens // block_size]`` is identical for every row of every session. The
  remaining ids are unique to the session (disjoint across sessions and from the system ids) and
  there are no duplicate ids inside a row.
* ``hash_ids`` of turn k+1 start with ``hash_ids`` of turn k (same session).
* Phases: the per-session phase offsets are exactly ``{Tp * j // sessions}`` for
  ``j in range(sessions)`` (a seeded assignment of sessions to those slots), so the first-turn
  timestamps lie in ``[0, Tp)`` and the k-th turn of a session is exactly ``k * Tp`` after its
  first. Rows are sorted by ``timestamp`` (ties by session) so the file is a valid fixed schedule.
* Deterministic: the same arguments give identical rows; a different seed gives a different
  session-to-phase assignment (sessions >= 3).
* ``ValueError`` for: any size <= 0, ``turn_period_ms <= 0``, ``context_tokens <= system_tokens``,
  a size that is not a multiple of ``block_size``.
* CLI: ``demo-bench trace multiturn --out P --seed S --turn-period-ms TP [--sessions N --turns T
  --system-tokens N --context-tokens N --growth-tokens N --output-tokens N --block-size N]``
  writes the JSONL (compact separators, one ``\\n`` per row), prints one JSON object
  ``{"path", "rows", "sha256"}`` (sha256 of the file) and exits 0; invalid input exits 2 and no
  file is written. ``demo-bench trace --sessions ...`` (legacy) and ``trace schedule`` still work.
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

KEYS = {"timestamp", "input_length", "output_length", "hash_ids"}


def _gen(**kw: Any) -> list[dict[str, Any]]:
    from demo_bench.application.trace_multiturn import generate_multiturn_trace

    return generate_multiturn_trace(**kw)


def _cli(argv: list[str]) -> int:
    from demo_bench.cli import main

    try:
        return int(main(argv))
    except SystemExit as exc:
        return int(exc.code or 0)


def _by_session(rows: list[dict[str, Any]], sys_blocks: int) -> dict[int, list[dict[str, Any]]]:
    groups: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        groups[r["hash_ids"][sys_blocks]].append(r)  # first private block identifies the session
    return {k: sorted(v, key=lambda r: r["timestamp"]) for k, v in groups.items()}


def _check_invariants(rows: list[dict[str, Any]], *, sessions: int, turns: int, sys_tok: int,
                      c0: int, g: int, osl: int, tp: int, block: int) -> None:
    sys_blocks = sys_tok // block
    assert len(rows) == sessions * turns
    assert all(set(r) == KEYS for r in rows)
    assert all(isinstance(r["timestamp"], int) and not isinstance(r["timestamp"], bool)
               for r in rows)
    stamps = [r["timestamp"] for r in rows]
    assert stamps == sorted(stamps)
    assert all(r["output_length"] == osl for r in rows)
    system = rows[0]["hash_ids"][:sys_blocks]
    assert len(system) == sys_blocks
    groups = _by_session(rows, sys_blocks)
    assert len(groups) == sessions
    private_sets = []
    phases = []
    for srows in groups.values():
        assert len(srows) == turns
        phases.append(srows[0]["timestamp"])
        for k, r in enumerate(srows):
            assert r["input_length"] == c0 + k * g
            assert len(r["hash_ids"]) == r["input_length"] // block
            assert r["hash_ids"][:sys_blocks] == system
            assert len(set(r["hash_ids"])) == len(r["hash_ids"])
            assert r["timestamp"] == srows[0]["timestamp"] + k * tp
            if k:
                prev = srows[k - 1]["hash_ids"]
                assert r["hash_ids"][: len(prev)] == prev
        private_sets.append(set(srows[-1]["hash_ids"][sys_blocks:]))
    assert sorted(phases) == [tp * j // sessions for j in range(sessions)]
    all_private = [i for s in private_sets for i in s]
    assert len(all_private) == len(set(all_private)), "sessions must not share private blocks"
    assert not set(all_private) & set(system)


# ------------------------------------------------------------------ Track D defaults
def test_defaults_are_the_track_d_workload() -> None:
    rows = _gen(turn_period_ms=10_000, seed=1)
    _check_invariants(rows, sessions=40, turns=8, sys_tok=2048, c0=30720, g=1536, osl=128,
                      tp=10_000, block=512)
    assert max(r["input_length"] for r in rows) == 30720 + 7 * 1536
    assert len(rows[0]["hash_ids"]) == 60  # 30720 / 512


def test_system_prompt_blocks_are_shared_across_sessions_and_turns() -> None:
    rows = _gen(turn_period_ms=5_000, seed=3, sessions=5, turns=3)
    assert len({tuple(r["hash_ids"][:4]) for r in rows}) == 1
    assert len({tuple(r["hash_ids"][:5]) for r in rows}) == 5  # block 5 is already private


def test_turn_k_plus_1_extends_turn_k_exactly() -> None:
    rows = _gen(turn_period_ms=5_000, seed=9, sessions=6, turns=5)
    for srows in _by_session(rows, 4).values():
        for a, b in zip(srows, srows[1:], strict=False):
            assert b["hash_ids"][: len(a["hash_ids"])] == a["hash_ids"]
            assert len(b["hash_ids"]) - len(a["hash_ids"]) == 1536 // 512


def test_first_turn_timestamps_are_spread_over_one_period() -> None:
    rows = _gen(turn_period_ms=40_000, seed=2, sessions=40, turns=2)
    first = sorted(s[0]["timestamp"] for s in _by_session(rows, 4).values())
    assert first == [1000 * j for j in range(40)]


# ------------------------------------------------------------------ determinism / seed
def test_same_arguments_same_rows() -> None:
    kw: dict[str, Any] = {"turn_period_ms": 7_000, "seed": 11, "sessions": 10, "turns": 4}
    assert _gen(**kw) == _gen(**kw)


def test_seed_changes_the_session_to_phase_assignment_only() -> None:
    kw: dict[str, Any] = {"turn_period_ms": 9_000, "sessions": 9, "turns": 3}
    a, b = _gen(seed=1, **kw), _gen(seed=2, **kw)
    assert a != b
    pa = {s[0]["hash_ids"][4]: s[0]["timestamp"] for s in _by_session(a, 4).values()}
    pb = {s[0]["hash_ids"][4]: s[0]["timestamp"] for s in _by_session(b, 4).values()}
    assert sorted(pa.values()) == sorted(pb.values())


# ------------------------------------------------------------------ property based
@settings(max_examples=60, deadline=None)
@given(
    sessions=st.integers(1, 12), turns=st.integers(1, 6), sys_b=st.integers(1, 4),
    extra_b=st.integers(1, 6), growth_b=st.integers(1, 4), osl=st.integers(1, 512),
    block=st.sampled_from([64, 256, 512]), seed=st.integers(0, 10**6), tp_extra=st.integers(0, 5000),
)
def test_property_prefix_sharing_and_schedule(
    *, sessions: int, turns: int, sys_b: int, extra_b: int, growth_b: int, osl: int, block: int,
    seed: int, tp_extra: int,
) -> None:
    tp = sessions + tp_extra
    rows = _gen(sessions=sessions, turns=turns, system_tokens=sys_b * block,
                context_tokens=(sys_b + extra_b) * block, growth_tokens=growth_b * block,
                output_tokens=osl, turn_period_ms=tp, seed=seed, block_size=block)
    _check_invariants(rows, sessions=sessions, turns=turns, sys_tok=sys_b * block,
                      c0=(sys_b + extra_b) * block, g=growth_b * block, osl=osl, tp=tp,
                      block=block)


# ------------------------------------------------------------------ validation
@pytest.mark.parametrize(
    "override",
    [
        {"sessions": 0}, {"turns": 0}, {"system_tokens": 0}, {"growth_tokens": 0},
        {"output_tokens": 0}, {"turn_period_ms": 0}, {"turn_period_ms": -5},
        {"context_tokens": 2048},  # not larger than the system prompt
        {"context_tokens": 1024},
        {"system_tokens": 2000}, {"context_tokens": 30000}, {"growth_tokens": 1500},
        {"block_size": 0},
    ],
)
def test_invalid_arguments_raise_value_error(override: dict[str, Any]) -> None:
    kw: dict[str, Any] = {"turn_period_ms": 10_000, "seed": 1} | override
    with pytest.raises(ValueError):
        _gen(**kw)


# ------------------------------------------------------------------ CLI
def test_cli_writes_jsonl_and_prints_summary(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "sub" / "mt.jsonl"
    argv = ["trace", "multiturn", "--out", str(out), "--seed", "5", "--turn-period-ms", "8000"]
    assert _cli(argv) == 0
    rec = json.loads(capsys.readouterr().out)
    data = out.read_bytes()
    assert rec["rows"] == 320
    assert rec["path"] == str(out)
    assert rec["sha256"] == hashlib.sha256(data).hexdigest()
    lines = data.decode().split("\n")
    assert lines[-1] == "" and len(lines) == 321
    rows = [json.loads(line) for line in lines[:-1]]
    _check_invariants(rows, sessions=40, turns=8, sys_tok=2048, c0=30720, g=1536, osl=128,
                      tp=8000, block=512)
    assert lines[0] == json.dumps(rows[0], separators=(",", ":"))


def test_cli_is_deterministic_and_honours_overrides(tmp_path: Path) -> None:
    a, b = tmp_path / "a.jsonl", tmp_path / "b.jsonl"
    flags = ["--seed", "7", "--turn-period-ms", "3000", "--sessions", "6", "--turns", "3",
             "--system-tokens", "1024", "--context-tokens", "4096", "--growth-tokens", "512",
             "--output-tokens", "64", "--block-size", "512"]
    assert _cli(["trace", "multiturn", "--out", str(a), *flags]) == 0
    assert _cli(["trace", "multiturn", "--out", str(b), *flags]) == 0
    assert a.read_bytes() == b.read_bytes()
    rows = [json.loads(x) for x in a.read_text().splitlines()]
    _check_invariants(rows, sessions=6, turns=3, sys_tok=1024, c0=4096, g=512, osl=64, tp=3000,
                      block=512)


@pytest.mark.parametrize(
    "extra",
    [["--sessions", "0"], ["--turns", "0"], ["--turn-period-ms", "0"],
     ["--context-tokens", "2048"], ["--growth-tokens", "100"], ["--output-tokens", "-1"]],
)
def test_cli_invalid_input_exits_2_without_writing(tmp_path: Path, extra: list[str]) -> None:
    out = tmp_path / "x.jsonl"
    argv = ["trace", "multiturn", "--out", str(out), "--seed", "1", "--turn-period-ms", "9000"]
    control = tmp_path / "control.jsonl"
    assert _cli([*argv[:3], str(control), *argv[4:]]) == 0  # control: defaults are valid
    assert _cli([*argv, *extra]) == 2
    assert not out.exists()


def test_cli_requires_turn_period_and_seed(tmp_path: Path) -> None:
    out = tmp_path / "x.jsonl"
    full = ["trace", "multiturn", "--out", str(tmp_path / "ok.jsonl"), "--seed", "1",
            "--turn-period-ms", "1000"]
    assert _cli(full) == 0  # control
    assert _cli(["trace", "multiturn", "--out", str(out), "--seed", "1"]) == 2
    assert _cli(["trace", "multiturn", "--out", str(out), "--turn-period-ms", "1000"]) == 2
    assert not out.exists()


def test_legacy_trace_and_schedule_still_work(tmp_path: Path) -> None:
    out = tmp_path / "legacy.jsonl"
    argv = ["trace", "--sessions", "3", "--prefix-tokens", "1024", "--passes", "2", "--seed",
            "1", "--out", str(out)]
    assert _cli(argv) == 0
    assert len(out.read_text().splitlines()) == 6
