# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""RED contract: ``--l2-mode inclusive`` and ``--rules d41`` of the W2 gate (q38-tooling item a).

Why.  SGLang HiCache runs ``write_through``: every device node keeps a host copy, so GPU u L2 holds
``L2`` distinct tokens per target, not ``C_t + L2``.  The tool models L2 as an exclusive victim tier (the
default, kept for backward compatibility).  The d41 amendment replaced the node-level ``w_over_sum_*``
checks by composed per-target checks; the W2-QWEN38 verdict (matrix/20261022T223800Z-w2-freeze/verdict-t8.json,
copied to ``fixtures/w2_qwen38``) was produced by an offline script that emulated inclusive mode with
three ``run_gate`` calls.  The tool must now give the same verdict in one call.

Contract.

``run_gate(..., l2_mode="exclusive", rules="plan", l3_tokens_per_target=0, context_len=None)``

* ``l2_mode="exclusive"`` (default) and ``rules="plan"`` (default): behaviour and output unchanged.
* ``l2_mode="inclusive"``: ``hit_gpu_*`` stay the GPU-only hits (tier of ``gpu_tokens_per_target``);
  ``hit_l2_*`` / ``interaction_l2`` are the hits of GPU u L2 whose distinct capacity is
  ``l2_tokens_per_target`` (one LRU of that size, same eviction rule), not ``gpu + l2``.  With an L2 of 0
  there is no L2: ``hit_l2_* == hit_gpu_*``.  An unknown mode raises ``ValueError``.
* ``rules="d41"`` requires ``l2_mode="inclusive"``, ``l3_tokens_per_target > 0`` and ``context_len > 0``
  (``ValueError`` otherwise).  Extra metrics: ``hit_l3_rr|kv`` (GPU u L2 u L3, distinct capacity
  ``l2 + l3``), ``l3_only_share_rr|kv`` (= ``hit_l3 - hit_l2``), ``min_margin``.  The non-informational
  checks are exactly the d41 composed set below (no ``w_over_sum_*`` among them); each check has the
  name, value, lo and hi of the offline verdict-t8.json.
* CLI: ``demo-bench w2-gate ... [--l2-mode inclusive|exclusive] [--rules plan|d41]
  [--l3-tokens-per-target N] [--context-len N]``; invalid combinations exit 2, a failing gate exits 1.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

B = 512
FIX = Path(__file__).resolve().parent.parent / "fixtures" / "w2_qwen38"
C_T, L2_POD, L3_POD, MAMBA_GPU, MAMBA_HOST, CONTEXT = (
    2_100_224,
    3_213_760,
    113_000_000,
    401,
    661,
    131_072,
)

D41_CHECKS = [
    "ceiling_single_cache",
    "rr_gpu_only",
    "kv_sticky_gpu_only",
    "kv_sticky_plus_l2_inclusive",
    "kv_minus_rr_with_l2_inclusive",
    "rr_revisit_same_target",
    "uncacheable_prefill",
    "overlapped_turns",
    "live_states_gpu",
    "live_states_host",
    "rr_footprint_over_gpu",
    "rr_footprint_over_l2",
    "l3_window_rr",
    "populate_over_gpu",
    "sessions_floor",
    "d41_c_t_lt_fkv_sessions",
    "d41_fkv_sessions_le_l2",
    "context_fit_rows",
]


def _jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(x) for x in path.read_text().splitlines() if x.strip()]


def _kwargs(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "targets": 8,
        "gpu_tokens_per_target": C_T,
        "l2_tokens_per_target": L2_POD,
        "turn_duration_s": 30.0,
        "warmup_s": 540.0,
        "max_mamba_gpu": MAMBA_GPU,
        "max_mamba_host": MAMBA_HOST,
        "l2_mode": "inclusive",
        "rules": "d41",
        "l3_tokens_per_target": L3_POD,
        "context_len": CONTEXT,
    }
    return {**base, **over}


def _run(rows: list[dict[str, Any]] | None = None, **over: Any) -> Any:
    from demo_bench.application.w2_gate import run_gate

    return run_gate(
        rows or _jsonl(FIX / "window.jsonl"), _jsonl(FIX / "window.side.jsonl"), **_kwargs(**over)
    )


@pytest.fixture(scope="module")
def verdict() -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads((FIX / "verdict-t8.json").read_text())
    return loaded


@pytest.fixture(scope="module")
def report() -> Any:
    return _run()


def _by_name(rep: Any) -> dict[str, Any]:
    return {c.name: c for c in rep.checks if not c.informational}


# ------------------------------------------------------------------ W2-QWEN38 verdict reproduced
def test_the_w2_qwen38_verdict_is_reproduced_in_one_call(
    report: Any, verdict: dict[str, Any]
) -> None:
    assert verdict["passed"] is True and verdict["failed"] == []
    assert report.passed is True, [
        c.name for c in report.checks if not c.passed and not c.informational
    ]


def test_d41_check_set_replaces_the_w_over_sum_node_checks(report: Any) -> None:
    names = _by_name(report)
    assert sorted(names) == sorted(D41_CHECKS)
    assert not [n for n in names if n.startswith("w_over_sum")]


def test_every_check_matches_verdict_t8(report: Any, verdict: dict[str, Any]) -> None:
    expected = {c["name"]: c for c in verdict["checks"] if not c["informational"]}
    got = _by_name(report)
    assert sorted(expected) == sorted(got)
    for name, want in expected.items():
        have = got[name]
        assert have.value == pytest.approx(want["value"], rel=1e-9, abs=1e-12), name
        assert (
            have.lo == pytest.approx(want["lo"]) if want["lo"] is not None else have.lo is None
        ), name
        assert (
            have.hi == pytest.approx(want["hi"]) if want["hi"] is not None else have.hi is None
        ), name
        assert have.passed is want["passed"], name


def test_hits_per_tier_match_the_offline_composition(report: Any, verdict: dict[str, Any]) -> None:
    m, h = report.metrics, verdict["hits"]
    assert m["ceiling_hit"] == pytest.approx(h["ceiling"], rel=1e-9)
    for key, tier in (("gpu", "gpu_only"), ("l2", "gpu_l2_inclusive"), ("l3", "gpu_l2_l3")):
        assert m[f"hit_{key}_rr"] == pytest.approx(h[tier]["rr"], rel=1e-9), tier
        assert m[f"hit_{key}_kv"] == pytest.approx(h[tier]["kv"], rel=1e-9), tier
    assert m["l3_only_share_rr"] == pytest.approx(h["l3_only_share"]["rr"], rel=1e-9)
    assert m["l3_only_share_kv"] == pytest.approx(h["l3_only_share"]["kv"], abs=1e-12)
    assert m["interaction_l2"] == pytest.approx(
        0.708343505859375, rel=1e-9
    )  # kv - rr with GPU u L2


def test_peak_live_states_are_gpu_only_and_gpu_u_l2(report: Any, verdict: dict[str, Any]) -> None:
    m, live = report.metrics, verdict["live_states"]
    assert m["peak_live_states_gpu_kv"] == live["peak_gpu_kv"]
    assert m["peak_live_states_gpu_rr"] == live["peak_gpu_rr"]
    assert m["peak_live_states_host_kv"] == live["peak_gpu_u_l2_kv"]
    assert m["peak_live_states_host_rr"] == live["peak_gpu_u_l2_rr"]


def test_min_margin_is_the_uncacheable_share_over_its_floor(
    report: Any, verdict: dict[str, Any]
) -> None:
    assert report.metrics["min_margin"] == pytest.approx(verdict["min_margin"], rel=1e-9)
    assert report.metrics["min_margin"] == pytest.approx(
        report.metrics["uncacheable_prefill_fraction"] - 0.08
    )


def test_params_record_the_mode_rules_and_l3(report: Any) -> None:
    p = report.to_dict()["params"]
    assert (p["l2_mode"], p["rules"], p["l3_tokens_per_target"], p["context_len"]) == (
        "inclusive",
        "d41",
        L3_POD,
        CONTEXT,
    )


# ------------------------------------------------------------------ the d41 checks fail when they should
def test_a_gpu_tier_above_the_session_footprint_fails_the_band_and_c_t_lt_fkv() -> None:
    rep = _run(gpu_tokens_per_target=20_000_000)
    failed = {n for n, c in _by_name(rep).items() if not c.passed}
    assert rep.passed is False
    assert {"kv_sticky_gpu_only", "d41_c_t_lt_fkv_sessions"} <= failed


def test_a_row_that_does_not_fit_the_context_fails_context_fit_rows() -> None:
    rows = _jsonl(FIX / "window.jsonl")
    rows[5]["output_length"] = CONTEXT - rows[5]["input_length"]
    rep = _run(rows)
    c = _by_name(rep)["context_fit_rows"]
    assert c.value == 1.0 and c.hi == 0.0 and c.passed is False and rep.passed is False


def test_a_tiny_l3_leaves_no_l3_window_under_rr() -> None:
    rep = _run(l3_tokens_per_target=B)
    c = _by_name(rep)["l3_window_rr"]
    assert (
        c.lo == pytest.approx(0.03) and c.value is not None and c.value < 0.03 and c.passed is False
    )


def test_sessions_floor_is_eight_per_engine() -> None:
    rep = _run(targets=32)
    c = _by_name(rep)["sessions_floor"]
    assert c.lo == 8.0 * 32 and c.value == 195.0 and c.passed is False


# ------------------------------------------------------------------ inclusive vs exclusive (synthetic, plan rules)
LEAF_FIRST = [(0, 0, 0, [1, 2, 3, 4]), (1000, 1, 0, [7, 8]), (2000, 0, 1, [1, 2, 3, 4])]


def _tiny(**over: Any) -> Any:
    from demo_bench.application.w2_gate import run_gate

    rows = [
        {"timestamp": ts, "input_length": len(ids) * B, "output_length": 1, "hash_ids": ids}
        for ts, _, _, ids in LEAF_FIRST
    ]
    side = [
        {
            "line": i,
            "session": s,
            "turn": t,
            "class": "entry" if t == 0 else "continuation",
            "input_length": 0,
        }
        for i, (_, s, t, _) in enumerate(LEAF_FIRST)
    ]
    args: dict[str, Any] = {
        "targets": 1,
        "gpu_tokens_per_target": 2 * B,
        "l2_tokens_per_target": 4 * B,
        "turn_duration_s": 1.0,
    }
    return run_gate(rows, side, **{**args, **over})


def test_exclusive_is_the_default_and_unions_gpu_plus_l2() -> None:
    default, explicit = _tiny(), _tiny(l2_mode="exclusive")
    assert default.metrics["hit_l2_rr"] == pytest.approx(4 / 10)  # GPU 2 + L2 4 = 6 blocks resident
    assert default.to_dict() == explicit.to_dict()


def test_inclusive_write_through_holds_only_l2_distinct_blocks() -> None:
    m = _tiny(l2_mode="inclusive").metrics
    assert m["hit_gpu_rr"] == pytest.approx(0.0)  # the real GPU tier is 2 blocks
    assert m["hit_l2_rr"] == pytest.approx(
        2 / 10
    )  # GPU u L2 = 4 distinct blocks, leaf-first: 1 2 7 8
    assert m["hit_l2_kv"] == pytest.approx(2 / 10)
    assert m["interaction_l2"] == pytest.approx(0.0)


def test_inclusive_without_l2_is_gpu_only() -> None:
    m = _tiny(l2_mode="inclusive", l2_tokens_per_target=0).metrics
    assert m["hit_l2_rr"] == pytest.approx(m["hit_gpu_rr"])
    assert m == _tiny(l2_tokens_per_target=0).metrics


def test_plan_rules_keep_the_w_over_sum_checks_in_inclusive_mode() -> None:
    names = {c.name for c in _tiny(l2_mode="inclusive").checks}
    assert {"w_over_sum_gpu", "w_over_sum_l2"} <= names


@pytest.mark.parametrize(
    "over",
    [
        {"l2_mode": "write-through"},
        {"rules": "d42"},
        {
            "l2_mode": "exclusive",
            "rules": "d41",
            "l3_tokens_per_target": L3_POD,
            "context_len": CONTEXT,
        },
        {"l2_mode": "inclusive", "rules": "d41", "context_len": CONTEXT},
        {"l2_mode": "inclusive", "rules": "d41", "l3_tokens_per_target": L3_POD},
        {"l2_mode": "inclusive", "rules": "d41", "l3_tokens_per_target": 0, "context_len": CONTEXT},
    ],
    ids=[
        "unknown-mode",
        "unknown-rules",
        "d41-needs-inclusive",
        "d41-needs-l3",
        "d41-needs-context",
        "d41-l3-zero",
    ],
)
def test_invalid_mode_rule_combinations_raise(over: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        _tiny(**over)


# ------------------------------------------------------------------ CLI
def _cli(argv: list[str]) -> int:
    from demo_bench.cli import main

    try:
        return int(main(argv))
    except SystemExit as exc:
        return int(exc.code or 0)


def _argv(*extra: str) -> list[str]:
    return [
        "w2-gate", "--trace", str(FIX / "window.jsonl"), "--sidecar", str(FIX / "window.side.jsonl"),
        "--targets", "8", "--gpu-tokens-per-target", str(C_T), "--l2-tokens-per-target", str(L2_POD),
        "--turn-duration-s", "30", "--warmup-s", "540", "--max-mamba-gpu", str(MAMBA_GPU),
        "--max-mamba-host", str(MAMBA_HOST), *extra,
    ]  # fmt: skip


D41_FLAGS = [
    "--l2-mode",
    "inclusive",
    "--rules",
    "d41",
    "--l3-tokens-per-target",
    str(L3_POD),
    "--context-len",
    str(CONTEXT),
]


def test_cli_d41_inclusive_reproduces_the_verdict(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], verdict: dict[str, Any]
) -> None:
    out = tmp_path / "gate.json"
    assert _cli(_argv(*D41_FLAGS, "--out", str(out))) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["passed"] is True and json.loads(out.read_text()) == doc
    assert sorted(c["name"] for c in doc["checks"] if not c["informational"]) == sorted(D41_CHECKS)
    assert doc["metrics"]["min_margin"] == pytest.approx(verdict["min_margin"], rel=1e-9)


def test_cli_failing_d41_gate_exits_1(capsys: pytest.CaptureFixture[str]) -> None:
    argv = _argv(*D41_FLAGS)
    argv[argv.index("--gpu-tokens-per-target") + 1] = "20000000"
    assert _cli(argv) == 1
    assert json.loads(capsys.readouterr().out)["passed"] is False


def test_cli_default_is_exclusive_plan_and_unchanged(capsys: pytest.CaptureFixture[str]) -> None:
    rc_default = _cli(_argv())
    default_out = capsys.readouterr().out
    rc_explicit = _cli(_argv("--l2-mode", "exclusive", "--rules", "plan"))
    assert rc_default == rc_explicit
    assert default_out == capsys.readouterr().out


def test_cli_inclusive_with_plan_rules(capsys: pytest.CaptureFixture[str]) -> None:
    assert _cli(_argv("--l2-mode", "inclusive")) in (0, 1)
    doc = json.loads(capsys.readouterr().out)
    assert {"w_over_sum_gpu", "w_over_sum_l2"} <= {c["name"] for c in doc["checks"]}
    assert doc["metrics"]["hit_l2_rr"] == pytest.approx(
        0.167724609375, rel=1e-9
    )  # gpu = L2_pod, l2 = 0 emulation


@pytest.mark.parametrize(
    "flags",
    [
        ["--l2-mode", "write-through"],
        ["--rules", "d42"],
        [
            "--rules",
            "d41",
            "--l3-tokens-per-target",
            str(L3_POD),
            "--context-len",
            str(CONTEXT),
        ],  # exclusive
        ["--l2-mode", "inclusive", "--rules", "d41", "--context-len", str(CONTEXT)],  # no L3
        [
            "--l2-mode",
            "inclusive",
            "--rules",
            "d41",
            "--l3-tokens-per-target",
            str(L3_POD),
        ],  # no context
    ],
    ids=["bad-mode", "bad-rules", "d41-exclusive", "d41-no-l3", "d41-no-context"],
)
def test_cli_invalid_combinations_exit_2(flags: list[str]) -> None:
    assert (
        _cli(_argv(*D41_FLAGS)) == 0
    )  # control: the valid combination is accepted (argparse rejection alone is not enough)
    assert _cli(_argv(*flags)) == 2
