# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Tests for the fixed-schedule arrival stamping of a baked trace (``trace schedule``).

aiperf 0.13.0 format (verified in the 0.13.0 wheel source):
  * ``aiperf/dataset/loader/models.py`` ``MooncakeTrace.timestamp: int | float | None`` --
    "The timestamp of a request in milliseconds. Supports floating point, but scheduling
    accuracy is at the millisecond level."
  * ``aiperf/dataset/loader/mooncake_trace.py`` docstring: fixed-schedule row is
    ``{"timestamp": 1000, "input_length": 300, "output_length": 40, "hash_ids": [123, 456]}``;
    a trace whose first record carries ``timestamp`` auto-promotes to ``fixed_schedule``.
  * ``aiperf/config/flags/cli_config.py`` ``--fixed-schedule-start-offset/end-offset``: ms offsets
    from benchmark start (``--fixed-schedule-auto-offset`` shifts the first timestamp to 0).
  * ``base_trace_loader.py`` samples trace datasets sequentially to preserve timestamp order.
So: unit = milliseconds, key = ``timestamp``, rows stay in order, single-turn (no
``session_id``/``delay``/``messages``).
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import statistics
import sys
from pathlib import Path
from typing import Any

import pytest
from hypothesis import assume, given, settings
from hypothesis import strategies as st

import lib_proc

BAKED = (Path(__file__).resolve().parents[3] / "bench-image" / "traces"
         / "64k_400_90kv_agent_new_noschedule_short_15perc.jsonl")
FORBIDDEN = {"session_id", "delay", "messages"}


def _write_src(path: Path, n: int = 12, spaced: bool = False) -> list[str]:
    lines = []
    for i in range(n):
        row = {"input_length": 1000 + i, "output_length": 50 + i,
               "hash_ids": [0, 1, 2, 1_999_229 + i, 7 * i]}
        lines.append(json.dumps(row) if spaced else json.dumps(row, separators=(",", ":")))
    path.write_text("\n".join(lines) + "\n")
    return lines


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _cli(argv: list[str]) -> int:
    from demo_bench.cli import main

    try:
        return int(main(argv))
    except SystemExit as exc:
        return int(exc.code or 0)


# =========================================================================== pure arrivals
def _times(n: int, rate: float, seed: int, arrival: str = "poisson") -> list[float]:
    from demo_bench.application.trace_schedule import arrival_times_ms

    return list(arrival_times_ms(n, rate=rate, seed=seed, arrival=arrival))


@settings(max_examples=60, deadline=None)
@given(n=st.integers(1, 300), rate=st.floats(0.1, 500), seed=st.integers(0, 10**6),
       arrival=st.sampled_from(["poisson", "uniform"]))
def test_property_starts_at_zero_and_is_monotonic(
    n: int, rate: float, seed: int, arrival: str
) -> None:
    ts = _times(n, rate, seed, arrival)
    assert len(ts) == n
    assert ts[0] == 0
    assert all(b >= a for a, b in zip(ts, ts[1:], strict=False))
    assert all(isinstance(t, (int, float)) and not isinstance(t, bool) for t in ts)
    assert all(math.isfinite(t) for t in ts)


@settings(max_examples=40, deadline=None)
@given(n=st.integers(1, 200), rate=st.floats(0.1, 500), seed=st.integers(0, 10**6),
       arrival=st.sampled_from(["poisson", "uniform"]))
def test_property_deterministic_and_prefix_stable(
    n: int, rate: float, seed: int, arrival: str
) -> None:
    full = _times(n + 50, rate, seed, arrival)
    assert _times(n, rate, seed, arrival) == _times(n, rate, seed, arrival)
    assert _times(n, rate, seed, arrival) == full[:n]  # --rows/--duration are pure truncations


@settings(max_examples=40, deadline=None)
@given(a=st.integers(0, 10**6), b=st.integers(0, 10**6), rate=st.floats(0.5, 50))
def test_property_different_seeds_give_different_poisson_schedules(
    a: int, b: int, rate: float
) -> None:
    assume(a != b)
    assert _times(50, rate, a) != _times(50, rate, b)


@settings(max_examples=30, deadline=None)
@given(a=st.integers(0, 10**6), b=st.integers(0, 10**6), rate=st.floats(0.5, 50))
def test_property_uniform_is_seed_independent(a: int, b: int, rate: float) -> None:
    assert _times(40, rate, a, "uniform") == _times(40, rate, b, "uniform")


@pytest.mark.parametrize("rate", [0.5, 5.0, 50.0])
def test_poisson_mean_interarrival_is_one_over_rate(rate: float) -> None:
    ts = _times(5000, rate, seed=3)
    mean_gap_ms = ts[-1] / (len(ts) - 1)
    assert mean_gap_ms == pytest.approx(1000.0 / rate, rel=0.10)


@pytest.mark.parametrize("rate", [0.5, 5.0, 50.0])
def test_uniform_gaps_are_exactly_one_over_rate(rate: float) -> None:
    ts = _times(2000, rate, seed=0, arrival="uniform")
    assert ts[-1] / (len(ts) - 1) == pytest.approx(1000.0 / rate, rel=0.01)
    gaps = [b - a for a, b in zip(ts, ts[1:], strict=False)]
    assert max(gaps) - min(gaps) <= 1.0 + 1e-9  # at most integer-ms rounding jitter


def test_poisson_gaps_are_exponential_not_constant() -> None:
    ts = _times(5000, 5.0, seed=11)
    gaps = [b - a for a, b in zip(ts, ts[1:], strict=False)]
    cv = statistics.pstdev(gaps) / statistics.fmean(gaps)
    assert 0.8 < cv < 1.2  # exponential inter-arrivals have CV == 1


@pytest.mark.parametrize("rate", [0.0, -1.0, float("nan"), float("inf")])
def test_invalid_rate_rejected(rate: float) -> None:
    with pytest.raises(ValueError):
        _times(10, rate, 0)


def test_unknown_arrival_rejected_and_empty_is_empty() -> None:
    with pytest.raises(ValueError):
        _times(10, 1.0, 0, "bursty")
    assert _times(0, 1.0, 0) == []


# =========================================================================== file level
def _schedule(src: Path, out: Path, **kw: Any) -> Any:
    from demo_bench.application.trace_schedule import schedule_trace

    args: dict[str, Any] = {"rate": 2.0, "seed": 5}
    args.update(kw)
    return schedule_trace(src, out, **args)


@pytest.mark.parametrize("spaced", [False, True])
def test_rows_are_byte_identical_except_added_timestamp(tmp_path: Path, spaced: bool) -> None:
    src, out = tmp_path / "in.jsonl", tmp_path / "out.jsonl"
    in_lines = _write_src(src, 12, spaced)
    _schedule(src, out)
    out_lines = out.read_text().splitlines()
    assert len(out_lines) == len(in_lines) == 12
    assert out.read_text().endswith("\n")
    for i_line, o_line in zip(in_lines, out_lines, strict=True):
        assert o_line.startswith(i_line[:-1])  # original bytes kept up to the closing brace
        assert o_line.endswith("}")
        i_row, o_row = json.loads(i_line), json.loads(o_line)
        assert list(o_row)[:-1] == list(i_row) and list(o_row)[-1] == "timestamp"
        assert {k: v for k, v in o_row.items() if k != "timestamp"} == i_row
        assert not FORBIDDEN & set(o_row)


def test_row_order_preserved_and_timestamps_monotonic_from_zero(tmp_path: Path) -> None:
    src, out = tmp_path / "in.jsonl", tmp_path / "out.jsonl"
    _write_src(src, 30)
    _schedule(src, out, rate=4.0)
    rows = [json.loads(ln) for ln in out.read_text().splitlines()]
    assert [r["input_length"] for r in rows] == [1000 + i for i in range(30)]
    ts = [r["timestamp"] for r in rows]
    assert ts[0] == 0 and all(b >= a for a, b in zip(ts, ts[1:], strict=False))
    assert ts == _times(30, 4.0, 5)  # the file uses exactly arrival_times_ms (ms)


def test_result_records_parameters_and_hashes(tmp_path: Path) -> None:
    src, out = tmp_path / "in.jsonl", tmp_path / "out.jsonl"
    _write_src(src, 10)
    before = _sha(src)
    res = _schedule(src, out, rate=3.0, seed=9, arrival="uniform")
    assert _sha(src) == before  # input untouched
    assert res.output_sha256 == _sha(out) and res.input_sha256 == before
    d = res.to_dict()
    json.dumps(d)
    assert d["rate"] == 3.0 and d["seed"] == 9 and d["arrival"] == "uniform"
    assert d["rows"] == 10 and d["input_sha256"] == before and d["output_sha256"] == _sha(out)
    assert d["duration_ms"] == pytest.approx(json.loads(out.read_text().splitlines()[-1])["timestamp"])


def test_identical_inputs_give_identical_bytes_and_seed_changes_them(tmp_path: Path) -> None:
    src = tmp_path / "in.jsonl"
    _write_src(src, 40)
    a, b, c = tmp_path / "a", tmp_path / "b", tmp_path / "c"
    _schedule(src, a, seed=1)
    _schedule(src, b, seed=1)
    _schedule(src, c, seed=2)
    assert a.read_bytes() == b.read_bytes()
    assert a.read_bytes() != c.read_bytes()


def test_rows_limit_is_prefix_of_full_schedule(tmp_path: Path) -> None:
    src = tmp_path / "in.jsonl"
    _write_src(src, 20)
    full, part = tmp_path / "full", tmp_path / "part"
    _schedule(src, full)
    res = _schedule(src, part, rows=7)
    assert res.rows == 7
    assert part.read_text().splitlines() == full.read_text().splitlines()[:7]


def test_duration_limit_keeps_rows_within_window_as_prefix(tmp_path: Path) -> None:
    src = tmp_path / "in.jsonl"
    _write_src(src, 60)
    full, part = tmp_path / "full", tmp_path / "part"
    _schedule(src, full, rate=2.0, arrival="uniform")  # 500 ms apart
    res = _schedule(src, part, rate=2.0, arrival="uniform", duration_s=5.0)
    kept = [json.loads(ln) for ln in part.read_text().splitlines()]
    assert len(kept) == 11  # t = 0, 500, ..., 5000: the boundary is inclusive (as aiperf's end offset)
    assert kept[-1]["timestamp"] == 5000
    assert all(r["timestamp"] <= 5000 for r in kept)
    assert part.read_text().splitlines() == full.read_text().splitlines()[: len(kept)]
    assert res.rows == len(kept)


@pytest.mark.parametrize(
    "kw", [{"rows": 3, "duration_s": 5.0}, {"rows": 99}, {"rows": 0}, {"duration_s": 0.0},
           {"duration_s": -1.0}, {"rate": 0.0}, {"rate": -2.0}]
)
def test_invalid_options_raise_schedule_error_and_write_nothing(
    tmp_path: Path, kw: dict[str, Any]
) -> None:
    from demo_bench.application.trace_schedule import ScheduleError

    src, out = tmp_path / "in.jsonl", tmp_path / "out.jsonl"
    _write_src(src, 12)
    with pytest.raises(ScheduleError):
        _schedule(src, out, **kw)
    assert not out.exists()


@pytest.mark.parametrize(
    "content",
    ["", "\n\n", "{not json}\n",
     '{"input_length":1,"output_length":1,"hash_ids":[1],"timestamp":5}\n',
     '{"input_length":1,"output_length":1,"hash_ids":[1],"session_id":"a"}\n',
     '{"input_length":1,"output_length":1,"hash_ids":[1],"delay":3}\n',
     '{"input_length":1,"output_length":1,"messages":[]}\n'],
    ids=["empty", "blank", "malformed", "has-timestamp", "has-session_id", "has-delay",
         "has-messages"],
)
def test_bad_input_raises_schedule_error(tmp_path: Path, content: str) -> None:
    from demo_bench.application.trace_schedule import ScheduleError

    src, out = tmp_path / "in.jsonl", tmp_path / "out.jsonl"
    src.write_text(content)
    with pytest.raises(ScheduleError):
        _schedule(src, out)
    assert not out.exists()


def test_schedule_error_is_a_value_error() -> None:
    from demo_bench.application.trace_schedule import ScheduleError

    assert issubclass(ScheduleError, ValueError)


def test_missing_input_file_raises_schedule_error(tmp_path: Path) -> None:
    from demo_bench.application.trace_schedule import ScheduleError

    with pytest.raises(ScheduleError):
        _schedule(tmp_path / "nope.jsonl", tmp_path / "out.jsonl")


def test_baked_trace_shaped_input_roundtrip_preserves_every_row(tmp_path: Path) -> None:
    """Real row shapes (3k+ rows, long hash_ids) from the baked trace, any stamping state.

    The baked file may or may not already carry ``timestamp`` (it is a build artifact), so the
    source here is rebuilt without that key; the input under test is always an unscheduled trace.
    """
    assert BAKED.is_file(), f"baked trace missing: {BAKED}"
    src, out = tmp_path / "unscheduled.jsonl", tmp_path / "scheduled.jsonl"
    in_lines = []
    for raw in BAKED.read_text().splitlines():
        row = json.loads(raw)
        row.pop("timestamp", None)
        in_lines.append(json.dumps(row, separators=(",", ":")))
    src.write_text("\n".join(in_lines) + "\n")
    res = _schedule(src, out, rate=1.5, seed=42)
    out_lines = out.read_text().splitlines()
    assert res.rows == len(out_lines) == len(in_lines) >= 3000
    assert res.input_sha256 == _sha(src)
    for i_line, o_line in zip(in_lines, out_lines, strict=True):
        assert o_line.startswith(i_line[:-1]) and o_line.endswith("}")
        o_row = json.loads(o_line)
        assert {k: v for k, v in o_row.items() if k != "timestamp"} == json.loads(i_line)
        assert not FORBIDDEN & set(o_row)


# =========================================================================== CLI
def _argv(src: Path, out: Path, *extra: str) -> list[str]:
    return ["trace", "schedule", "--in", str(src), "--out", str(out), *extra]


def test_cli_writes_schedule_and_prints_json_record(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    src, out = tmp_path / "in.jsonl", tmp_path / "out.jsonl"
    _write_src(src, 15)
    assert _cli(_argv(src, out, "--rate", "2.5", "--seed", "4", "--arrival", "uniform")) == 0
    rec = json.loads(capsys.readouterr().out)
    assert rec["rate"] == 2.5 and rec["seed"] == 4 and rec["arrival"] == "uniform"
    assert rec["rows"] == 15
    assert rec["input_sha256"] == _sha(src) and rec["output_sha256"] == _sha(out)
    assert Path(rec["out"]) == out
    assert [json.loads(ln)["timestamp"] for ln in out.read_text().splitlines()][:3] == [0, 400, 800]


def _positive_control(tmp_path: Path) -> None:
    """A valid call must succeed, so the exit-2 assertions cannot pass vacuously."""
    ctrl_src, ctrl_out = tmp_path / "ctrl_in.jsonl", tmp_path / "ctrl_out.jsonl"
    _write_src(ctrl_src, 5)
    assert _cli(_argv(ctrl_src, ctrl_out, "--rate", "2", "--seed", "1")) == 0
    assert len(ctrl_out.read_text().splitlines()) == 5


def test_cli_default_arrival_is_poisson(tmp_path: Path) -> None:
    src = tmp_path / "in.jsonl"
    _write_src(src, 25)
    a, b = tmp_path / "a", tmp_path / "b"
    assert _cli(_argv(src, a, "--rate", "3", "--seed", "1")) == 0
    assert _cli(_argv(src, b, "--rate", "3", "--seed", "1", "--arrival", "poisson")) == 0
    assert a.read_bytes() == b.read_bytes()


def test_cli_rows_and_duration_options(tmp_path: Path) -> None:
    src = tmp_path / "in.jsonl"
    _write_src(src, 40)
    r, d = tmp_path / "r", tmp_path / "d"
    assert _cli(_argv(src, r, "--rate", "2", "--seed", "1", "--rows", "6")) == 0
    assert len(r.read_text().splitlines()) == 6
    assert _cli(_argv(src, d, "--rate", "2", "--seed", "1", "--arrival", "uniform",
                      "--duration", "3")) == 0
    assert all(json.loads(ln)["timestamp"] <= 3000 for ln in d.read_text().splitlines())


@pytest.mark.parametrize(
    "extra",
    [["--rate", "0"], ["--rate", "-1"], ["--rate", "abc"], ["--rate", "2", "--arrival", "bursty"],
     ["--rate", "2", "--rows", "3", "--duration", "5"], ["--rate", "2", "--rows", "0"],
     ["--rate", "2", "--duration", "0"], []],
    ids=["rate0", "rate-neg", "rate-nan", "bad-arrival", "rows+duration", "rows0", "duration0",
         "rate-missing"],
)
def test_cli_invalid_arguments_exit_2_and_write_nothing(tmp_path: Path, extra: list[str]) -> None:
    _positive_control(tmp_path)
    src, out = tmp_path / "in.jsonl", tmp_path / "out.jsonl"
    _write_src(src, 10)
    assert _cli(_argv(src, out, "--seed", "1", *extra)) == 2
    assert not out.exists()


@pytest.mark.parametrize("content", ["", "\n", "garbage\n"], ids=["empty", "blank", "malformed"])
def test_cli_bad_input_exits_2(tmp_path: Path, content: str) -> None:
    _positive_control(tmp_path)
    src, out = tmp_path / "in.jsonl", tmp_path / "out.jsonl"
    src.write_text(content)
    assert _cli(_argv(src, out, "--rate", "2", "--seed", "1")) == 2
    assert not out.exists()


def test_cli_missing_input_exits_2(tmp_path: Path) -> None:
    _positive_control(tmp_path)
    out = tmp_path / "out.jsonl"
    assert _cli(_argv(tmp_path / "missing.jsonl", out, "--rate", "2", "--seed", "1")) == 2
    assert not out.exists()


def test_cli_schedule_help_exits_zero(capsys: pytest.CaptureFixture[str]) -> None:
    assert _cli(["trace", "schedule", "--help"]) == 0
    out = capsys.readouterr().out
    for flag in ("--in", "--out", "--rate", "--seed", "--arrival", "--duration", "--rows"):
        assert flag in out


def test_cli_legacy_session_trace_form_still_works(tmp_path: Path) -> None:
    out = tmp_path / "t.jsonl"
    assert _cli(["trace", "--sessions", "3", "--prefix-tokens", "1024", "--passes", "2",
                 "--seed", "1", "--out", str(out)]) == 0
    assert len(out.read_text().splitlines()) == 6


def test_output_independent_of_pythonhashseed(tmp_path: Path) -> None:
    src = tmp_path / "in.jsonl"
    _write_src(src, 30)
    digests = set()
    for hs in ("1", "2", "random"):
        out = tmp_path / f"o{hs}"
        env = {**os.environ, "PYTHONHASHSEED": hs}
        proc = lib_proc.run(
            [sys.executable, "-m", "demo_bench", *_argv(src, out, "--rate", "3", "--seed", "8")],
            capture_output=True, text=True, env=env, timeout=120, check=False)
        assert proc.returncode == 0, proc.stderr
        digests.add(_sha(out))
    assert len(digests) == 1
