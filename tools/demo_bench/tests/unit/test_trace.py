# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests for ``demo_bench.application.trace`` (spec 10.1, 10.3 HiCache trace)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

BLOCK = 512


def _gen(**kw: Any) -> list[dict[str, Any]]:
    from demo_bench.application.trace import generate_session_trace

    args: dict[str, Any] = {"sessions": 5, "prefix_tokens": 2048, "passes": 3, "seed": 7}
    args.update(kw)
    return generate_session_trace(**args)


def _prefix_key(row: dict[str, Any], prefix_tokens: int = 2048) -> tuple[int, ...]:
    return tuple(row["hash_ids"][: prefix_tokens // BLOCK])


def _passes(rows: list[dict[str, Any]], sessions: int) -> list[list[tuple[int, ...]]]:
    return [
        [_prefix_key(r) for r in rows[i : i + sessions]] for i in range(0, len(rows), sessions)
    ]


def test_row_schema_and_forbidden_keys() -> None:
    rows = _gen()
    assert len(rows) == 15
    for r in rows:
        assert {"timestamp", "input_length", "output_length", "hash_ids"} <= set(r)
        assert not ({"session_id", "delay", "messages"} & set(r))
        assert all(isinstance(h, int) for h in r["hash_ids"])


def test_input_length_equals_blocks_times_block_size_default_512() -> None:
    for r in _gen():
        assert r["input_length"] == len(r["hash_ids"]) * BLOCK
        assert r["input_length"] >= 2048


def test_custom_block_size_is_respected() -> None:
    rows = _gen(block_size=256, prefix_tokens=2048)
    for r in rows:
        assert r["input_length"] == len(r["hash_ids"]) * 256


def test_prefix_tokens_not_multiple_of_block_size_rejected() -> None:
    with pytest.raises(ValueError):
        _gen(prefix_tokens=2000)


@pytest.mark.parametrize("bad", [{"sessions": 0}, {"passes": 0}, {"prefix_tokens": 0}])
def test_invalid_arguments_rejected(bad: dict[str, int]) -> None:
    with pytest.raises(ValueError):
        _gen(**bad)


def test_phase_one_visits_each_session_exactly_once() -> None:
    rows = _gen(sessions=6, passes=2)
    first = [_prefix_key(r) for r in rows[:6]]
    assert len(set(first)) == 6


def test_every_pass_visits_every_session_once() -> None:
    rows = _gen(sessions=6, passes=4)
    populate = set(_passes(rows, 6)[0])
    for p in _passes(rows, 6):
        assert len(p) == 6 and set(p) == populate


def test_revisit_passes_are_not_the_populate_order_and_are_independent() -> None:
    rows = _gen(sessions=8, passes=6, seed=3)
    ps = _passes(rows, 8)
    populate, revisits = ps[0], ps[1:]
    for rv in revisits:
        assert rv != populate
    # independent shuffles: they must not all be the same permutation
    assert len({tuple(rv) for rv in revisits}) > 1


@settings(max_examples=40, deadline=None)
@given(n=st.integers(min_value=3, max_value=12), seed=st.integers(0, 10_000),
       passes=st.integers(2, 4))
def test_property_revisit_never_equals_populate_for_n_ge_3(n: int, seed: int, passes: int) -> None:
    ps = _passes(_gen(sessions=n, passes=passes, seed=seed), n)
    for rv in ps[1:]:
        assert rv != ps[0]
        assert sorted(rv) == sorted(ps[0])


def test_prefix_shared_within_session_and_disjoint_across_sessions() -> None:
    rows = _gen(sessions=5, passes=3)
    by_session: dict[tuple[int, ...], int] = {}
    for r in rows:
        by_session[_prefix_key(r)] = by_session.get(_prefix_key(r), 0) + 1
    assert len(by_session) == 5
    assert all(c == 3 for c in by_session.values())
    all_prefix_ids = [set(k) for k in by_session]
    for i, a in enumerate(all_prefix_ids):
        for b in all_prefix_ids[i + 1 :]:
            assert a.isdisjoint(b)


def test_per_request_blocks_are_unique_and_disjoint_from_prefixes() -> None:
    rows = _gen(sessions=4, passes=3)
    prefix_ids = {h for r in rows for h in _prefix_key(r)}
    unique_ids = [h for r in rows for h in r["hash_ids"][2048 // BLOCK :]]
    assert unique_ids, "each request must carry at least one unique block"
    assert len(unique_ids) == len(set(unique_ids))
    assert prefix_ids.isdisjoint(unique_ids)


def test_timestamps_monotonic_with_requested_spacing() -> None:
    rows = _gen(spacing_ms=250)
    ts = [r["timestamp"] for r in rows]
    assert ts[0] == 0
    assert all(b - a == 250 for a, b in zip(ts, ts[1:], strict=False))


def test_seed_is_deterministic_and_matters() -> None:
    assert _gen(seed=11) == _gen(seed=11)
    assert _gen(sessions=8, passes=3, seed=1) != _gen(sessions=8, passes=3, seed=2)


def test_output_length_parameter() -> None:
    assert {r["output_length"] for r in _gen(output_tokens=64)} == {64}


def test_write_trace_jsonl_roundtrip(tmp_path: Path) -> None:
    from demo_bench.application.trace import write_trace_jsonl

    rows = _gen()
    out = tmp_path / "t.jsonl"
    write_trace_jsonl(rows, out)
    loaded = [json.loads(line) for line in out.read_text().splitlines()]
    assert loaded == rows
