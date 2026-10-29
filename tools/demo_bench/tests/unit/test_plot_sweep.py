# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Tests for ``demo-bench plot sweep`` (PPT-style charts for the concurrency sweep).

CLI: ``demo-bench plot sweep --runs DIR --out DIR --setup "<label>" [--pairs S0:S1,S0:S2]``
(requires the ``[plot]`` extra: ``uv run --extra plot pytest``; matplotlib is imported lazily and
never by the core).

Outputs in ``--out`` (created if needed): for each chart ``<name>.png`` and ``<name>.svg`` with
names ``ttft_vs_concurrency`` (p50 solid, p90 dashed), ``throughput_vs_concurrency`` (output
tok/s), ``itl_vs_concurrency`` (p50 solid, p90 dashed) and, only when ``--pairs`` is given,
``speedup_vs_concurrency`` (grouped bars, legend ``"<candidate> vs <baseline>"``), plus
``sweep_data.json`` (see test_sweep_data.py).

Rendering contract: English labels; SVG text kept as text (``svg.fonttype = "none"``) so labels
are searchable; the setup label is printed on every chart; one fixed colour per scenario id
(``colors`` of the data JSON, written lowercase ``#rrggbb`` in the SVG); 16:9 canvas, width >= 1600
px; fixed DPI/hash salt/metadata so PNG and SVG bytes are identical for identical input.
"""

from __future__ import annotations

import contextlib
import io
import json
import shutil
import struct
import sys
import tomllib
from pathlib import Path
from typing import Any

import pytest

import lib_proc
import lib_xml

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
SWEEP = FIXTURES / "sweep"
PYPROJECT = Path(__file__).resolve().parents[2] / "pyproject.toml"
SETUP = "1x p6-b200 · Qwen3.8-27B-FP8 · Dynamo 1.5.1 · n=1"
PAIRS = "S0:S1,S0:S2,S0:S4"
CHARTS = ["ttft_vs_concurrency", "throughput_vs_concurrency", "itl_vs_concurrency",
          "speedup_vs_concurrency"]
_CACHE: dict[str, Path] = {}


def _cli(argv: list[str]) -> tuple[int, str]:
    from demo_bench.cli import main

    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            rc = int(main(argv))
    except SystemExit as exc:
        rc = int(exc.code or 0)
    return rc, buf.getvalue()


def _plot(runs: Path, out: Path, setup: str | None = SETUP, pairs: str | None = PAIRS) -> int:
    argv = ["plot", "sweep", "--runs", str(runs), "--out", str(out)]
    if setup is not None:
        argv += ["--setup", setup]
    if pairs is not None:
        argv += ["--pairs", pairs]
    return _cli(argv)[0]


def _standard(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Render the standard block-1 charts once per session (lazy so import errors FAIL tests)."""
    if "std" not in _CACHE:
        out = tmp_path_factory.mktemp("charts") / "out"
        assert _plot(SWEEP, out) == 0
        _CACHE["std"] = out
    return _CACHE["std"]


def _markers(svg: str, color: str) -> int:
    """Number of `<use ...>` marker elements styled with the given scenario colour."""
    import re

    return sum(1 for m in re.findall(r"<use [^>]*>", svg) if color.lower() in m.lower())


def _png_size(path: Path) -> tuple[int, int]:
    raw = path.read_bytes()
    assert raw[:8] == b"\x89PNG\r\n\x1a\n", "not a PNG"
    assert raw[12:16] == b"IHDR"
    w, h = struct.unpack(">II", raw[16:24])
    return int(w), int(h)


def _svg_texts(path: Path) -> list[str]:
    root = lib_xml.parse(path)
    out = []
    for el in root.iter():
        if el.tag.split("}")[-1] == "text":
            out.append("".join(el.itertext()).strip())
    return [t for t in out if t]


# ============================================================================ outputs
def test_writes_all_charts_as_png_and_svg_plus_data(tmp_path_factory: pytest.TempPathFactory) -> None:
    out = _standard(tmp_path_factory)
    for name in CHARTS:
        assert (out / f"{name}.png").stat().st_size > 5_000, name
        assert (out / f"{name}.svg").stat().st_size > 2_000, name
    data = json.loads((out / "sweep_data.json").read_text())
    assert data["setup"] == SETUP and data["pairs"] == ["S0:S1", "S0:S2", "S0:S4"]


def test_png_is_16_9_and_presentation_sized(tmp_path_factory: pytest.TempPathFactory) -> None:
    out = _standard(tmp_path_factory)
    sizes = {name: _png_size(out / f"{name}.png") for name in CHARTS}
    assert len(set(sizes.values())) == 1, sizes
    w, h = next(iter(sizes.values()))
    assert w >= 1600 and abs(w / h - 16 / 9) < 0.02, (w, h)


def test_svgs_are_well_formed(tmp_path_factory: pytest.TempPathFactory) -> None:
    out = _standard(tmp_path_factory)
    for name in CHARTS:
        root = lib_xml.parse(out / f"{name}.svg")
        assert root.tag.endswith("svg")


def test_setup_label_is_printed_on_every_chart(tmp_path_factory: pytest.TempPathFactory) -> None:
    out = _standard(tmp_path_factory)
    for name in CHARTS:
        texts = _svg_texts(out / f"{name}.svg")
        assert any(SETUP in t for t in texts), (name, texts[:12])


def test_english_labels_per_chart(tmp_path_factory: pytest.TempPathFactory) -> None:
    out = _standard(tmp_path_factory)
    expect = {
        "ttft_vs_concurrency": ("TTFT", "ms", "Concurrency"),
        "throughput_vs_concurrency": ("tok/s", "Concurrency"),
        "itl_vs_concurrency": ("ITL", "ms", "Concurrency"),
        "speedup_vs_concurrency": ("Speedup", "TTFT", "Concurrency"),
    }
    for name, words in expect.items():
        blob = " ".join(_svg_texts(out / f"{name}.svg"))
        for w in words:
            assert w in blob, (name, w, blob[:300])
        # no non-ASCII text other than the setup label itself (no accented/other-language labels)
        for t in _svg_texts(out / f"{name}.svg"):
            if SETUP not in t:
                assert t.isascii(), (name, t)


def test_scenarios_are_labelled_and_coloured_consistently(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    out = _standard(tmp_path_factory)
    data = json.loads((out / "sweep_data.json").read_text())
    for name in ("ttft_vs_concurrency", "throughput_vs_concurrency", "itl_vs_concurrency"):
        svg = (out / f"{name}.svg").read_text().lower()
        blob = " ".join(_svg_texts(out / f"{name}.svg"))
        for s in data["scenarios"]:
            assert s in blob, (name, s)
            assert data["colors"][s].lower() in svg, (name, s)


def test_percentile_styles_solid_p50_dashed_p90(tmp_path_factory: pytest.TempPathFactory) -> None:
    out = _standard(tmp_path_factory)
    for name in ("ttft_vs_concurrency", "itl_vs_concurrency"):
        svg = (out / f"{name}.svg").read_text()
        assert "stroke-dasharray" in svg, name
        blob = " ".join(_svg_texts(out / f"{name}.svg"))
        assert "p50" in blob and "p90" in blob, name


def test_speedup_legend_names_candidate_vs_baseline(tmp_path_factory: pytest.TempPathFactory) -> None:
    out = _standard(tmp_path_factory)
    blob = " ".join(_svg_texts(out / "speedup_vs_concurrency.svg"))
    for cand in ("S1", "S2", "S4"):
        assert f"{cand} vs S0" in blob


def test_without_pairs_no_speedup_chart(tmp_path: Path) -> None:
    out = tmp_path / "o"
    assert _plot(SWEEP, out, pairs=None) == 0
    assert not list(out.glob("speedup_vs_concurrency.*"))
    for name in CHARTS[:3]:
        assert (out / f"{name}.png").exists() and (out / f"{name}.svg").exists()
    assert json.loads((out / "sweep_data.json").read_text())["speedup"] == {}


# ============================================================================ determinism
def test_output_bytes_are_deterministic_across_runs_and_locations(tmp_path: Path) -> None:
    a_runs, b_runs = tmp_path / "ra", tmp_path / "deeper" / "rb"
    shutil.copytree(SWEEP, a_runs)
    shutil.copytree(SWEEP, b_runs)
    a, b = tmp_path / "oa", tmp_path / "ob" / "nested"
    assert _plot(a_runs, a) == 0 and _plot(b_runs, b) == 0
    for name in CHARTS:
        assert (a / f"{name}.svg").read_bytes() == (b / f"{name}.svg").read_bytes(), name
        assert (a / f"{name}.png").read_bytes() == (b / f"{name}.png").read_bytes(), name
    assert (a / "sweep_data.json").read_bytes() == (b / "sweep_data.json").read_bytes()


def test_rerun_overwrites_with_identical_bytes(tmp_path: Path) -> None:
    out = tmp_path / "o"
    assert _plot(SWEEP, out) == 0
    first = {p.name: p.read_bytes() for p in out.iterdir()}
    assert _plot(SWEEP, out) == 0
    assert {p.name: p.read_bytes() for p in out.iterdir()} == first


def test_svg_has_no_dates_or_absolute_paths(tmp_path_factory: pytest.TempPathFactory) -> None:
    out = _standard(tmp_path_factory)
    for name in CHARTS:
        svg = (out / f"{name}.svg").read_text()
        assert "<dc:date>" not in svg and str(SWEEP) not in svg and ("/Us" + "ers/") not in svg


# ============================================================================ gaps
def test_gaps_are_reported_in_data_and_charts_still_render(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    shutil.copytree(SWEEP, runs)
    for c in (64, 128):
        shutil.rmtree(runs / f"c{c}" / "ceiling" / "S4" / "rep1")
    shutil.rmtree(runs / "c16" / "ceiling" / "S1" / "rep1")
    out = tmp_path / "o"
    assert _plot(runs, out) == 0
    data = json.loads((out / "sweep_data.json").read_text())
    assert data["series"]["S4"]["ttft_p50_ms"][5:] == [None, None]
    assert data["series"]["S1"]["ttft_p50_ms"][3] is None
    assert data["speedup"]["S0:S1"][3] is None and data["speedup"]["S0:S4"][5] is None
    for name in CHARTS:
        assert _png_size(out / f"{name}.png")[0] >= 1600
    # a chart with gaps differs from the full one (points are really missing)
    full = tmp_path / "full"
    assert _plot(SWEEP, full) == 0
    assert (out / "ttft_vs_concurrency.svg").read_bytes() != (full / "ttft_vs_concurrency.svg").read_bytes()
    # every plotted point is a marker (<use> styled with the scenario colour); missing points draw
    # nothing: no zero, no fill-in. S4 lost 2 points, S1 lost 1; S0 and S2 are untouched.
    colors = data["colors"]
    for name in ("ttft_vs_concurrency", "itl_vs_concurrency", "throughput_vs_concurrency"):
        gap_svg = (out / f"{name}.svg").read_text()
        full_svg = (full / f"{name}.svg").read_text()
        assert _markers(gap_svg, colors["S4"]) < _markers(full_svg, colors["S4"]), name
        assert _markers(gap_svg, colors["S1"]) < _markers(full_svg, colors["S1"]), name
        assert _markers(gap_svg, colors["S0"]) == _markers(full_svg, colors["S0"]), name
        assert _markers(gap_svg, colors["S2"]) == _markers(full_svg, colors["S2"]), name


def test_closed_loop_rows_from_ceiling_sh_are_accepted(tmp_path: Path) -> None:
    """`series/ceiling.sh` rows: run.json.mode == closed-loop with integer `concurrency`."""
    meta = json.loads((SWEEP / "c8" / "ceiling" / "S0" / "rep1" / "run.json").read_text())
    assert meta["mode"] == "closed-loop" and meta["series"] == "ceiling" and meta["concurrency"] == 8
    out = tmp_path / "o"
    assert _plot(SWEEP, out) == 0


# ============================================================================ errors
def test_setup_label_is_mandatory(tmp_path: Path) -> None:
    assert _plot(SWEEP, tmp_path / "ok") == 0  # positive control
    for setup in (None, "", "   "):
        out = tmp_path / f"bad{setup!r}".replace(" ", "_").replace("'", "")
        assert _plot(SWEEP, out, setup=setup) == 2
        assert not list(out.glob("*.png")) and not list(out.glob("*.svg"))


@pytest.mark.parametrize("pairs", ["S0S1", "S0:S0", "S0:S1,", ":S1", "S0:", "S0:S1:S2"])
def test_invalid_pairs_exit_2_and_write_no_charts(tmp_path: Path, pairs: str) -> None:
    assert _plot(SWEEP, tmp_path / "ok", pairs="S0:S1") == 0  # positive control
    out = tmp_path / "bad"
    assert _plot(SWEEP, out, pairs=pairs) == 2
    assert not list(out.glob("*.png")) and not list(out.glob("*.svg"))


@pytest.mark.parametrize("missing_pair", ["S0:S3", "S3:S0", "S0:S9", "S9:S0"])
def test_pair_without_a_valid_run_is_dropped_exit_0_and_the_rest_is_plotted(
    tmp_path: Path, missing_pair: str, caplog: pytest.LogCaptureFixture
) -> None:
    """P-26: warn and drop the pair instead of exiting 2; the remaining pairs are still plotted."""
    out = tmp_path / "o"
    with caplog.at_level("WARNING"):
        assert _plot(SWEEP, out, pairs=f"S0:S1,{missing_pair},S0:S2") == 0
    data = json.loads((out / "sweep_data.json").read_text())
    assert data["pairs"] == ["S0:S1", "S0:S2"] and sorted(data["speedup"]) == ["S0:S1", "S0:S2"]
    for name in CHARTS:
        assert (out / f"{name}.png").exists() and (out / f"{name}.svg").exists()
    assert any(missing_pair in f"{r.getMessage()} {r.__dict__}" for r in caplog.records
               if r.levelname == "WARNING")
    texts = " ".join(_svg_texts(out / "speedup_vs_concurrency.svg"))
    assert "S2 vs S0" in texts and "S1 vs S0" in texts


def test_all_pairs_dropped_still_exit_0_without_speedup_chart(tmp_path: Path) -> None:
    out = tmp_path / "o"
    assert _plot(SWEEP, out, pairs="S0:S3,S9:S1") == 0
    assert not list(out.glob("speedup_vs_concurrency.*"))
    for name in CHARTS[:3]:
        assert (out / f"{name}.png").exists()
    data = json.loads((out / "sweep_data.json").read_text())
    assert data["pairs"] == [] and data["speedup"] == {}


def test_no_sweep_runs_exit_2(tmp_path: Path) -> None:
    assert _plot(SWEEP, tmp_path / "ok") == 0  # positive control
    empty = tmp_path / "empty"
    empty.mkdir()
    only_open = tmp_path / "open"
    shutil.copytree(SWEEP / "openloop", only_open)
    for runs in (empty, only_open, tmp_path / "missing"):
        out = tmp_path / f"o-{runs.name}"
        assert _plot(runs, out) == 2
        assert not list(out.glob("*.png")) if out.exists() else True


def test_plot_help_lists_sweep(capsys: pytest.CaptureFixture[str]) -> None:
    from demo_bench.cli import main

    with pytest.raises(SystemExit) as ei:
        main(["plot", "--help"])
    assert ei.value.code == 0
    assert "sweep" in capsys.readouterr().out
    with pytest.raises(SystemExit) as ei:
        main(["plot", "sweep", "--help"])
    out = capsys.readouterr().out
    assert ei.value.code == 0
    for flag in ("--runs", "--out", "--setup", "--pairs"):
        assert flag in out
    with pytest.raises(SystemExit) as ei:
        main(["--help"])
    assert "plot" in capsys.readouterr().out


# ============================================================================ optional extra
def test_pyproject_keeps_core_stdlib_and_declares_plot_extra() -> None:
    cfg = tomllib.loads(PYPROJECT.read_text())
    assert not any("matplotlib" in d.lower() for d in cfg["project"].get("dependencies", []))
    extras = cfg["project"]["optional-dependencies"]
    assert any(d.lower().startswith("matplotlib") for d in extras["plot"])


def test_core_import_does_not_pull_matplotlib() -> None:
    code = ("import sys\nimport demo_bench.cli, demo_bench.application.sweep\n"
            "bad=[m for m in sys.modules if m.split('.')[0] in ('matplotlib','numpy','PIL')]\n"
            "assert not bad, bad\n")
    proc = lib_proc.run([sys.executable, "-c", code], capture_output=True, text=True,
                        timeout=120, check=False)
    assert proc.returncode == 0, proc.stderr


def test_missing_matplotlib_fails_cleanly_and_names_the_extra(tmp_path: Path) -> None:
    code = ("import sys\nsys.modules['matplotlib'] = None\n"
            "from demo_bench.cli import main\n"
            f"sys.exit(main(['plot','sweep','--runs',{str(SWEEP)!r},'--out',{str(tmp_path / 'o')!r},"
            f"'--setup',{SETUP!r}]))\n")
    proc = lib_proc.run([sys.executable, "-c", code], capture_output=True, text=True,
                        timeout=120, check=False)
    assert proc.returncode not in (0, None)
    assert "Traceback" not in proc.stderr
    assert "plot" in proc.stderr and "matplotlib" in proc.stderr
    assert not list((tmp_path / "o").glob("*.png")) if (tmp_path / "o").exists() else True


def test_missing_matplotlib_hint_is_the_uv_command_of_the_tool_not_pip(monkeypatch: pytest.MonkeyPatch,
                                                                       tmp_path: Path) -> None:
    """demo-bench is not published on PyPI: the hint names ``uv sync --extra plot`` from tools/demo_bench."""
    from demo_bench.infrastructure import sweep_charts

    monkeypatch.setitem(sys.modules, "matplotlib", None)
    with pytest.raises(sweep_charts.PlotDependencyError) as err:
        sweep_charts.render_sweep_charts({"pairs": []}, tmp_path / "o")
    msg = str(err.value)
    assert "uv sync --extra plot" in msg and "tools/demo_bench" in msg, msg
    assert "pip install" not in msg and "demo-bench[plot]" not in msg, msg
    assert not (tmp_path / "o").exists(), "nothing is written"
