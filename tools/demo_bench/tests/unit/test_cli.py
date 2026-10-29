# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""CLI tests for the ``demo-bench`` console script (``demo_bench.cli.main``)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

import lib_proc

SUBCOMMANDS = ["sizing", "trace", "metrics", "reset", "compare", "matrix", "schedule"]


def test_help_lists_seven_subcommands(capsys: pytest.CaptureFixture[str]) -> None:
    from demo_bench.cli import main

    with pytest.raises(SystemExit) as ei:
        main(["--help"])
    assert ei.value.code == 0
    out = capsys.readouterr().out
    for name in SUBCOMMANDS:
        assert name in out


@pytest.mark.parametrize("sub", SUBCOMMANDS)
def test_each_subcommand_has_help(sub: str, capsys: pytest.CaptureFixture[str]) -> None:
    from demo_bench.cli import main

    with pytest.raises(SystemExit) as ei:
        main([sub, "--help"])
    assert ei.value.code == 0
    assert "usage" in capsys.readouterr().out.lower()


def test_trace_writes_valid_jsonl(tmp_path: Path) -> None:
    from demo_bench.cli import main

    out = tmp_path / "trace.jsonl"
    rc = main(["trace", "--sessions", "4", "--prefix-tokens", "2048", "--passes", "2",
               "--seed", "1", "--out", str(out)])
    assert rc == 0
    rows = [json.loads(line) for line in out.read_text().splitlines()]
    assert len(rows) == 8
    for r in rows:
        assert {"timestamp", "input_length", "output_length", "hash_ids"} <= set(r)
        assert not ({"session_id", "delay", "messages"} & set(r))
        assert r["input_length"] == len(r["hash_ids"]) * 512


def test_trace_invalid_sessions_exits_nonzero(tmp_path: Path) -> None:
    from demo_bench.cli import main

    try:
        rc = main(["trace", "--sessions", "0", "--prefix-tokens", "2048", "--passes", "2",
                   "--seed", "1", "--out", str(tmp_path / "x.jsonl")])
    except SystemExit as exc:
        rc = int(exc.code or 0)
    assert rc != 0
    assert not (tmp_path / "x.jsonl").exists()


def test_schedule_subcommand_prints_24_runs(capsys: pytest.CaptureFixture[str]) -> None:
    from demo_bench.cli import main

    assert main(["schedule"]) == 0
    out = capsys.readouterr().out
    assert out.count("t-agg-hic") >= 6  # 2 runs x 3 rounds at minimum


def test_python_dash_m_entrypoint_works() -> None:
    proc = lib_proc.run([sys.executable, "-m", "demo_bench", "--help"], capture_output=True,
                        text=True, timeout=60, check=False)
    assert proc.returncode == 0
    for name in SUBCOMMANDS:
        assert name in proc.stdout
