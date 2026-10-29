# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Presentation charts of the concurrency sweep (matplotlib adapter, optional ``plot`` extra).

matplotlib is imported lazily inside :func:`render_sweep_charts`, so importing this module (or the
CLI) never pulls it in. Rendering is deterministic: Agg for PNG, SVG text kept as text
(``svg.fonttype = "none"``), a fixed SVG hash salt and no date or creator metadata, so identical
data give identical bytes. Missing points are NaN: the line breaks and no marker is drawn.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

from demo_bench.domain.scenario import scenario_by_id

if TYPE_CHECKING:  # pragma: no cover - typing only
    from matplotlib.axes import Axes
    from matplotlib.figure import Figure
    from matplotlib.typing import RcKeyType

logger = logging.getLogger(__name__)

WIDTH_IN, HEIGHT_IN, DPI = 16.0, 9.0, 120  # 1920 x 1080 px, 16:9
CHART_NAMES = (
    "ttft_vs_concurrency",
    "throughput_vs_concurrency",
    "itl_vs_concurrency",
    "speedup_vs_concurrency",
)
_RC: dict[RcKeyType, Any] = {
    "svg.fonttype": "none",
    "svg.hashsalt": "demo-bench-sweep",
    "axes.unicode_minus": False,
    "font.family": "DejaVu Sans",
    "font.size": 14,
    "axes.titlesize": 15,
    "axes.labelsize": 16,
    "figure.titlesize": 22,
    "legend.fontsize": 13,
    "xtick.labelsize": 14,
    "ytick.labelsize": 14,
}
_SVG_METADATA = {"Date": None, "Creator": "demo-bench"}
_PNG_METADATA = {"Software": None}


class PlotDependencyError(RuntimeError):
    """matplotlib is not installed (install the ``plot`` extra)."""


def _scenario_label(scenario_id: str) -> str:
    scenario = scenario_by_id(scenario_id)
    return f"{scenario_id} {scenario.topology.name} {scenario.router}"


def _nan(values: Sequence[float | None]) -> list[float]:
    return [math.nan if v is None else float(v) for v in values]


def _new_figure(title: str, data: dict[str, Any]) -> tuple[Figure, Axes]:
    from matplotlib.figure import Figure

    fig = Figure(figsize=(WIDTH_IN, HEIGHT_IN), dpi=DPI, layout="constrained")
    fig.suptitle(title, fontweight="bold")
    ax = fig.add_subplot()
    ax.set_title(str(data["setup"]), parse_math=False, color="#333333")
    concurrencies: list[int] = data["concurrencies"]
    ax.set_xticks(range(len(concurrencies)), [str(c) for c in concurrencies])
    ax.set_xlabel("Concurrency (requests in flight)")
    ax.grid(visible=True, axis="y", alpha=0.3)
    ax.ticklabel_format(axis="y", style="plain", useOffset=False)
    if data["missing"]:
        fig.supxlabel(
            f"Gaps: {len(data['missing'])} point(s) without a valid run are left empty "
            "(no interpolation).",
            fontsize=11,
            color="#555555",
        )
    return fig, ax


def _lines(ax: Axes, data: dict[str, Any], keys: Sequence[tuple[str, str, str, str]]) -> None:
    """Plot one line per (scenario, key); ``keys`` = (series key, suffix, linestyle, marker)."""
    x = list(range(len(data["concurrencies"])))
    for scenario in data["scenarios"]:
        color = data["colors"][scenario]
        for key, suffix, linestyle, marker in keys:
            hollow = linestyle != "-"
            ax.plot(
                x,
                _nan(data["series"][scenario][key]),
                color=color,
                linestyle=linestyle,
                linewidth=2.5,
                marker=marker,
                markersize=8,
                markerfacecolor="none" if hollow else color,
                label=f"{_scenario_label(scenario)} {suffix}".rstrip(),
            )
    ax.set_ylim(bottom=0)


def _legend(fig: Figure, ax: Axes) -> None:
    fig.legend(*ax.get_legend_handles_labels(), loc="outside right upper", frameon=False)


def _ttft(data: dict[str, Any]) -> Figure:
    fig, ax = _new_figure("Time to first token (TTFT) vs concurrency", data)
    _lines(ax, data, (("ttft_p50_ms", "p50", "-", "o"), ("ttft_p90_ms", "p90", "--", "s")))
    ax.set_ylabel("TTFT (ms)")
    _legend(fig, ax)
    return fig


def _itl(data: dict[str, Any]) -> Figure:
    fig, ax = _new_figure("Inter-token latency (ITL) vs concurrency", data)
    _lines(ax, data, (("itl_p50_ms", "p50", "-", "o"), ("itl_p90_ms", "p90", "--", "s")))
    ax.set_ylabel("ITL (ms)")
    _legend(fig, ax)
    return fig


def _throughput(data: dict[str, Any]) -> Figure:
    fig, ax = _new_figure("Output throughput vs concurrency", data)
    _lines(ax, data, (("output_tok_s", "", "-", "o"),))
    ax.set_ylabel("Output throughput (tok/s)")
    _legend(fig, ax)
    return fig


def _speedup(data: dict[str, Any]) -> Figure:
    fig, ax = _new_figure("TTFT p50 speedup vs concurrency", data)
    pairs: list[str] = data["pairs"]
    width = 0.8 / max(len(pairs), 1)
    for index, pair in enumerate(pairs):
        baseline, candidate = pair.split(":")
        offset = (index - (len(pairs) - 1) / 2) * width
        points = [(i + offset, v) for i, v in enumerate(data["speedup"][pair]) if v is not None]
        bars = ax.bar(
            [p[0] for p in points],
            [p[1] for p in points],
            width=width * 0.95,
            color=data["colors"][candidate],
            label=f"{candidate} vs {baseline}",
        )
        ax.bar_label(bars, fmt="%.2fx", padding=2, fontsize=11)
    ax.axhline(1.0, color="#555555", linestyle="--", linewidth=1.2)
    ax.set_ylim(bottom=0)
    ax.set_ylabel("Speedup (TTFT p50, baseline / candidate)")
    _legend(fig, ax)
    return fig


def _save(fig: Figure, out_dir: Path, name: str) -> list[Path]:
    png, svg = out_dir / f"{name}.png", out_dir / f"{name}.svg"
    fig.savefig(png, format="png", dpi=DPI, metadata=_PNG_METADATA)
    fig.savefig(svg, format="svg", metadata=_SVG_METADATA)
    return [png, svg]


def render_sweep_charts(data: dict[str, Any], out_dir: Path) -> list[Path]:
    """Render the sweep charts as PNG and SVG.

    Writes ``ttft_vs_concurrency``, ``throughput_vs_concurrency``, ``itl_vs_concurrency`` and,
    when the data has pairs, ``speedup_vs_concurrency``.

    Args:
        data: Result of :func:`demo_bench.application.sweep.build_sweep_data`.
        out_dir: Output directory (created if needed).

    Returns:
        Written files, in order.

    Raises:
        PlotDependencyError: If matplotlib is not installed (nothing is written).
    """
    try:
        import matplotlib
        from matplotlib.backends.backend_agg import FigureCanvasAgg
    except ImportError as exc:
        raise PlotDependencyError(
            "matplotlib is not installed: install the 'plot' extra from tools/demo_bench "
            "(uv sync --extra plot, then uv run --extra plot demo-bench plot sweep ...)"
        ) from exc
    builders = [("ttft_vs_concurrency", _ttft), ("throughput_vs_concurrency", _throughput)]
    builders.append(("itl_vs_concurrency", _itl))
    if data["pairs"]:
        builders.append(("speedup_vs_concurrency", _speedup))
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    with matplotlib.rc_context(_RC):
        for name, build in builders:
            fig = build(data)
            FigureCanvasAgg(fig)
            written += _save(fig, out_dir, name)
    logger.info("sweep charts written", extra={"out": str(out_dir), "files": len(written)})
    return written
