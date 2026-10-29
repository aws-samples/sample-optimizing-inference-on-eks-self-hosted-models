# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""RED contract for the offline routed-LRU gate (Flash tooling, item c; plan 4.4).

Module ``demo_bench.application.w2_gate`` (stdlib only):

* ``run_gate(rows, sidecar, *, targets, gpu_tokens_per_target, l2_tokens_per_target,
  turn_duration_s, block_size=512, warmup_s=0.0, uncacheable_tokens=65536,
  max_mamba_gpu=None, max_mamba_host=None) -> GateReport`` replays the rows (mooncake rows +
  the W2 sidecar, in list order) on ``targets`` independent engines, each with a GPU tier and an
  L2 tier of ``*_tokens_per_target // block_size`` blocks.
* Cache model: a request hits the longest PREFIX of its ``hash_ids`` that is resident in the
  GPU tier or in L2 of its target. After the request every one of its blocks is in the GPU tier
  with the recency of that request. When a tier overflows, the least recently used blocks are
  evicted; among blocks of equal recency the DEEPEST one goes first (radix leaves), so a prefix
  survives. A block evicted from GPU moves to L2 (victim cache, same eviction rule); a block
  found in L2 is promoted to GPU. "GPU only" runs use an L2 of 0 blocks.
* Routing: ``rr`` sends row ``i`` (list order) to ``i % targets``. ``kv`` (sticky) assigns a
  session to the target with the fewest sessions at its first row (ties: lowest index) and keeps
  it there; rows with ``session is None`` (cold) go round-robin on their own counter.
* Hit ratios are token (block) weighted over the rows with ``timestamp >= warmup_s * 1000``;
  earlier rows are simulated but not counted.
* ``GateReport.metrics`` keys: ``targets``, ``ceiling_hit`` (one infinite cache, no routing),
  ``hit_gpu_rr``, ``hit_gpu_kv``, ``hit_l2_rr``, ``hit_l2_kv``, ``interaction_l2``
  (``hit_l2_kv - hit_l2_rr``), ``revisit_same_target_rr`` / ``_kv`` (share of rows with
  ``turn >= 1`` that land on the target of the previous turn of the same session),
  ``uncacheable_prefill_fraction`` (share of counted rows with at least ``uncacheable_tokens``
  worth of blocks never seen in an earlier row), ``overlapped_turn_fraction`` (share of
  ``turn >= 1`` rows whose timestamp is less than ``turn_duration_s`` after the previous turn of
  the session), ``working_set_tokens`` (distinct blocks of the session rows x block_size),
  ``w_over_sum_gpu`` (working set / (targets x gpu tokens)), ``w_over_sum_l2``,
  ``footprint_tokens_per_target_rr`` / ``_kv`` (distinct blocks routed to each target x
  block_size, list), ``footprint_over_l2_rr`` / ``_kv`` (largest footprint / l2 tokens),
  ``peak_live_states_gpu_rr|kv`` and ``peak_live_states_host_rr|kv`` (list per target: the peak,
  measured after every request, of the number of sessions that own at least one resident block
  in that tier; a block is owned by a session when no other session's row contains it) and,
  only when ``max_mamba_gpu`` / ``max_mamba_host`` are given, ``live_states_gpu_ratio`` /
  ``live_states_host_ratio`` (largest kv peak / the limit).
* ``evaluate_thresholds(metrics) -> list[Check]`` applies the plan thresholds (see ``CASES``).
  ``Check`` has ``name``, ``value``, ``lo``, ``hi``, ``passed``, ``informational``. A missing
  required metric raises ``ValueError``; ``live_states_*`` checks exist only when their ratio does.
  ``GateReport.checks`` is that list, ``GateReport.passed`` is True iff every
  non-informational check passed, ``to_dict()`` is JSON-able.
* CLI ``demo-bench w2-gate --trace P --sidecar P --targets N --gpu-tokens-per-target N
  --l2-tokens-per-target N --turn-duration-s X [--block-size N] [--warmup-s X]
  [--uncacheable-tokens N] [--max-mamba-gpu N] [--max-mamba-host N] [--out P]`` prints
  ``to_dict()`` (``passed``, ``metrics``, ``checks``), also to ``--out``; exit 0 when passed,
  1 when a check fails, 2 for invalid input (missing file, malformed JSONL, non-positive size).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

B = 512


def _row(ts: int, ids: list[int]) -> dict[str, Any]:
    return {"timestamp": ts, "input_length": len(ids) * B, "output_length": 1, "hash_ids": ids}


def _side(i: int, session: int | None, turn: int | None) -> dict[str, Any]:
    cls = "cold_ingestion" if session is None else ("entry" if turn == 0 else "continuation")
    return {"line": i, "session": session, "turn": turn, "class": cls, "input_length": 0}


def _trace(spec: list[tuple[int, int | None, int | None, list[int]]]) -> tuple[list, list]:
    rows = [_row(ts, ids) for ts, _, _, ids in spec]
    side = [_side(i, s, t) for i, (_, s, t, _) in enumerate(spec)]
    for r, s in zip(rows, side, strict=True):
        s["input_length"] = r["input_length"]
    return rows, side


def _gate(spec: list[tuple[int, int | None, int | None, list[int]]], **kw: Any) -> Any:
    from demo_bench.application.w2_gate import run_gate

    rows, side = _trace(spec)
    args: dict[str, Any] = {
        "targets": 1, "gpu_tokens_per_target": 4 * B, "l2_tokens_per_target": 0,
        "turn_duration_s": 1.0,
    }
    return run_gate(rows, side, **{**args, **kw})


# ------------------------------------------------------------------ LRU semantics
LEAF_FIRST = [(0, 0, 0, [1, 2, 3, 4]), (1000, 1, 0, [7, 8]), (2000, 0, 1, [1, 2, 3, 4])]


def test_eviction_is_leaf_first_so_the_prefix_survives() -> None:
    m = _gate(LEAF_FIRST).metrics
    assert m["hit_gpu_rr"] == pytest.approx(2 / 10)
    assert m["hit_gpu_kv"] == pytest.approx(2 / 10)
    assert m["ceiling_hit"] == pytest.approx(4 / 10)


def test_l2_is_a_victim_tier_that_restores_the_evicted_suffix() -> None:
    m = _gate(LEAF_FIRST, l2_tokens_per_target=4 * B).metrics
    assert m["hit_l2_rr"] == pytest.approx(4 / 10)
    assert m["hit_l2_kv"] == pytest.approx(4 / 10)
    assert m["hit_gpu_rr"] == pytest.approx(2 / 10)  # "gpu only" ignores the L2


def test_hit_is_a_prefix_hit_and_recency_decides_what_goes() -> None:
    spec = [(0, 0, 0, [1, 2, 3, 4]), (1000, 1, 0, [1, 2, 5, 6]), (2000, 0, 1, [1, 2, 3, 4])]
    m = _gate(spec).metrics
    assert m["hit_gpu_rr"] == pytest.approx(4 / 12)
    m = _gate(spec, l2_tokens_per_target=8 * B).metrics
    assert m["hit_l2_rr"] == pytest.approx(6 / 12)
    assert m["ceiling_hit"] == pytest.approx(6 / 12)


def test_a_block_in_the_middle_does_not_count_without_its_prefix() -> None:
    spec = [(0, 0, 0, [1, 2, 3]), (1000, 1, 0, [9, 2, 3])]
    assert _gate(spec, gpu_tokens_per_target=100 * B).metrics["ceiling_hit"] == 0.0


def test_warmup_rows_are_simulated_but_not_counted() -> None:
    spec = [(0, 0, 0, [1, 2]), (5000, 0, 1, [1, 2])]
    big = {"gpu_tokens_per_target": 100 * B}
    assert _gate(spec, **big).metrics["ceiling_hit"] == pytest.approx(0.5)
    assert _gate(spec, warmup_s=1.0, **big).metrics["ceiling_hit"] == pytest.approx(1.0)


# ------------------------------------------------------------------ routing, footprint
THREE_BY_TWO = [
    (0, 0, 0, [10, 11]), (1, 1, 0, [20, 21]), (2, 2, 0, [30, 31]),
    (1000, 0, 1, [10, 11, 12]), (1001, 1, 1, [20, 21, 22]), (1002, 2, 1, [30, 31, 32]),
]
ROUTED: dict[str, Any] = {"targets": 2, "gpu_tokens_per_target": 100 * B,
                          "l2_tokens_per_target": 8 * B}


def test_rr_scatters_sessions_kv_keeps_them_and_the_ceiling_bounds_both() -> None:
    m = _gate(THREE_BY_TWO, **ROUTED).metrics
    assert m["ceiling_hit"] == pytest.approx(6 / 15)
    assert m["hit_gpu_rr"] == 0.0 and m["hit_l2_rr"] == 0.0
    assert m["hit_gpu_kv"] == pytest.approx(6 / 15) and m["hit_l2_kv"] == pytest.approx(6 / 15)
    assert m["interaction_l2"] == pytest.approx(6 / 15)
    assert m["revisit_same_target_rr"] == 0.0
    assert m["revisit_same_target_kv"] == 1.0
    assert m["targets"] == 2


def test_per_pod_distinct_footprint_under_rr_and_kv() -> None:
    m = _gate(THREE_BY_TWO, **ROUTED).metrics
    assert m["footprint_tokens_per_target_rr"] == [7 * B, 8 * B]
    assert m["footprint_tokens_per_target_kv"] == [6 * B, 3 * B]
    assert m["footprint_over_l2_rr"] == pytest.approx(8 * B / (8 * B))
    assert m["footprint_over_l2_kv"] == pytest.approx(6 * B / (8 * B))
    assert m["working_set_tokens"] == 9 * B
    assert m["w_over_sum_gpu"] == pytest.approx(9 * B / (2 * 100 * B))
    assert m["w_over_sum_l2"] == pytest.approx(9 * B / (2 * 8 * B))


def test_kv_sticky_balances_sessions_over_targets() -> None:
    spec = [(i, i, 0, [100 * i + j for j in range(3)]) for i in range(4)]
    m = _gate(spec, **ROUTED).metrics
    assert m["footprint_tokens_per_target_kv"] == [6 * B, 6 * B]


def test_cold_rows_go_round_robin_in_both_routings() -> None:
    spec = [(i, None, None, [100 * i + j for j in range(3)]) for i in range(4)]
    m = _gate(spec, **ROUTED).metrics
    assert m["footprint_tokens_per_target_rr"] == [6 * B, 6 * B]
    assert m["footprint_tokens_per_target_kv"] == [6 * B, 6 * B]
    assert m["working_set_tokens"] == 0  # cold rows are not part of the session working set


# ------------------------------------------------------------------ other metrics
def test_overlapped_turns() -> None:
    spec = [(0, 0, 0, [1]), (1000, 0, 1, [1, 2]), (10000, 0, 2, [1, 2, 3])]
    m = _gate(spec, turn_duration_s=2.0, gpu_tokens_per_target=100 * B).metrics
    assert m["overlapped_turn_fraction"] == pytest.approx(0.5)


def test_uncacheable_prefill_fraction_counts_never_seen_blocks() -> None:
    spec = [(0, 0, 0, [1, 2, 3]), (1, 0, 1, [1, 2, 3, 4]), (2, None, None, [9, 10]),
            (3, None, None, [9, 10])]
    m = _gate(spec, uncacheable_tokens=2 * B, gpu_tokens_per_target=100 * B).metrics
    assert m["uncacheable_prefill_fraction"] == pytest.approx(2 / 4)


def test_peak_live_states_per_tier() -> None:
    spec = [(0, 0, 0, [100, 101, 102]), (1000, 1, 0, [200, 201, 202])]
    m = _gate(spec, gpu_tokens_per_target=4 * B, l2_tokens_per_target=4 * B,
              max_mamba_gpu=10, max_mamba_host=5).metrics
    assert m["peak_live_states_gpu_kv"] == [2] and m["peak_live_states_gpu_rr"] == [2]
    assert m["peak_live_states_host_kv"] == [1] and m["peak_live_states_host_rr"] == [1]
    assert m["live_states_gpu_ratio"] == pytest.approx(0.2)
    assert m["live_states_host_ratio"] == pytest.approx(0.2)


def test_state_ratios_are_absent_without_limits() -> None:
    m = _gate(LEAF_FIRST).metrics
    assert "live_states_gpu_ratio" not in m and "live_states_host_ratio" not in m


@pytest.mark.parametrize(
    "kw",
    [{"targets": 0}, {"gpu_tokens_per_target": 0}, {"l2_tokens_per_target": -1},
     {"turn_duration_s": -1.0}, {"block_size": 0}, {"warmup_s": -1.0}],
)
def test_invalid_arguments(kw: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        _gate(LEAF_FIRST, **kw)


def test_rows_and_sidecar_must_have_the_same_length() -> None:
    from demo_bench.application.w2_gate import run_gate

    rows, side = _trace(LEAF_FIRST)
    with pytest.raises(ValueError):
        run_gate(rows, side[:-1], targets=1, gpu_tokens_per_target=B, l2_tokens_per_target=0,
                 turn_duration_s=1.0)


# ------------------------------------------------------------------ thresholds
GOOD: dict[str, Any] = {
    "targets": 4, "ceiling_hit": 0.9, "hit_gpu_rr": 0.3, "hit_gpu_kv": 0.5, "hit_l2_kv": 0.85,
    "interaction_l2": 0.2, "revisit_same_target_rr": 0.25, "uncacheable_prefill_fraction": 0.09,
    "overlapped_turn_fraction": 0.05, "w_over_sum_gpu": 2.0, "w_over_sum_l2": 0.6,
    "footprint_over_l2_rr": 1.4, "footprint_over_l2_kv": 0.7,
}
CASES = [
    ("ceiling_single_cache", "ceiling_hit", [0.85, 1.0], [0.849]),
    ("rr_gpu_only", "hit_gpu_rr", [0.0, 0.40], [0.401]),
    ("kv_sticky_gpu_only", "hit_gpu_kv", [0.30, 0.65], [0.299, 0.651]),
    ("kv_sticky_plus_l2", "hit_l2_kv", [0.80, 0.95], [0.799]),
    ("kv_minus_rr_with_l2", "interaction_l2", [0.15, 0.4], [0.149]),
    ("rr_revisit_same_target", "revisit_same_target_rr", [0.21, 0.29], [0.19, 0.31]),
    ("uncacheable_prefill", "uncacheable_prefill_fraction", [0.08, 0.2], [0.079]),
    ("overlapped_turns", "overlapped_turn_fraction", [0.0, 0.10], [0.101]),
    ("w_over_sum_gpu", "w_over_sum_gpu", [1.8, 2.2], [1.79, 2.21]),
    ("w_over_sum_l2", "w_over_sum_l2", [0.5, 0.85], [0.851]),
    ("live_states_gpu", "live_states_gpu_ratio", [0.8, 0.1], [0.81]),
    ("live_states_host", "live_states_host_ratio", [0.8, 0.1], [0.81]),
]


def _checks(**over: Any) -> dict[str, Any]:
    from demo_bench.application.w2_gate import evaluate_thresholds

    return {c.name: c for c in evaluate_thresholds({**GOOD, **over})}


def test_all_good_passes_and_state_checks_are_optional() -> None:
    checks = _checks()
    assert all(c.passed for c in checks.values())
    assert "live_states_gpu" not in checks and "live_states_host" not in checks
    assert checks["footprint_over_l2_rr"].informational
    assert checks["footprint_over_l2_kv"].informational
    assert checks["ceiling_single_cache"].value == 0.9


@pytest.mark.parametrize(("name", "key", "ok", "bad"), CASES)
def test_threshold_boundaries(name: str, key: str, ok: list[float], bad: list[float]) -> None:
    for v in ok:
        assert _checks(**{key: v})[name].passed, (name, v)
    for v in bad:
        c = _checks(**{key: v})[name]
        assert not c.passed, (name, v)
        assert c.value == v


def test_bounds_are_reported() -> None:
    c = _checks()
    assert (c["kv_sticky_gpu_only"].lo, c["kv_sticky_gpu_only"].hi) == (0.30, 0.65)
    assert c["ceiling_single_cache"].lo == 0.85
    assert c["w_over_sum_l2"].hi == 0.85


def test_revisit_band_follows_the_number_of_targets() -> None:
    assert _checks(targets=2, revisit_same_target_rr=0.5)["rr_revisit_same_target"].passed
    assert not _checks(targets=2, revisit_same_target_rr=0.25)["rr_revisit_same_target"].passed


@pytest.mark.parametrize("missing", ["ceiling_hit", "hit_l2_kv", "w_over_sum_l2", "targets"])
def test_missing_required_metric_raises(missing: str) -> None:
    from demo_bench.application.w2_gate import evaluate_thresholds

    metrics = {k: v for k, v in GOOD.items() if k != missing}
    with pytest.raises(ValueError):
        evaluate_thresholds(metrics)


def test_report_passed_follows_the_non_informational_checks() -> None:
    m = _gate(THREE_BY_TWO, **ROUTED)
    assert m.passed is False  # ceiling 0.4 < 0.85
    d = m.to_dict()
    assert json.loads(json.dumps(d)) == d
    assert d["passed"] is False and set(d) >= {"metrics", "checks"}
    names = {c["name"] for c in d["checks"]}
    assert {"ceiling_single_cache", "kv_sticky_plus_l2", "footprint_over_l2_kv"} <= names


# ------------------------------------------------------------------ CLI
def _cli(argv: list[str]) -> int:
    from demo_bench.cli import main

    try:
        return int(main(argv))
    except SystemExit as exc:
        return int(exc.code or 0)


def _files(tmp_path: Path) -> list[str]:
    rows, side = _trace(THREE_BY_TWO)
    (tmp_path / "t.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    (tmp_path / "t.side").write_text("".join(json.dumps(s) + "\n" for s in side))
    return ["w2-gate", "--trace", str(tmp_path / "t.jsonl"), "--sidecar", str(tmp_path / "t.side"),
            "--targets", "2", "--gpu-tokens-per-target", str(100 * B),
            "--l2-tokens-per-target", str(8 * B), "--turn-duration-s", "1"]


def test_cli_reports_failing_checks_with_exit_1(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "gate.json"
    assert _cli([*_files(tmp_path), "--out", str(out)]) == 1
    doc = json.loads(capsys.readouterr().out)
    assert doc["passed"] is False
    assert doc["metrics"]["footprint_tokens_per_target_kv"] == [6 * B, 3 * B]
    assert json.loads(out.read_text()) == doc


def test_cli_state_limits(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _cli([*_files(tmp_path), "--max-mamba-gpu", "10", "--max-mamba-host", "10"])
    doc = json.loads(capsys.readouterr().out)
    assert "live_states_gpu_ratio" in doc["metrics"]
    assert "live_states_gpu" in {c["name"] for c in doc["checks"]}


@pytest.mark.parametrize("bad", [["--targets", "0"], ["--gpu-tokens-per-target", "0"]])
def test_cli_invalid_numbers_exit_2(tmp_path: Path, bad: list[str]) -> None:
    argv = _files(tmp_path)
    assert _cli(argv) == 1  # control: valid input, failing checks
    argv[argv.index(bad[0]) + 1] = bad[1]
    assert _cli(argv) == 2


def test_cli_missing_and_malformed_files_exit_2(tmp_path: Path) -> None:
    argv = _files(tmp_path)
    assert _cli(argv) == 1  # control
    (tmp_path / "t.side").write_text("not json\n")
    assert _cli(argv) == 2
    (tmp_path / "t.side").unlink()
    assert _cli(argv) == 2
