# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""``demo-bench`` command line (spec 12.1).

Command output goes to stdout (or ``--out``); JSON logs go to stderr. Endpoints come from flags
or environment variables (``PROMETHEUS_URL``), never from defaults baked into the code.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
from collections.abc import Callable, Sequence
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from demo_bench.application.collect_metrics import collect_run_metrics
from demo_bench.application.compare import DEFAULT_METRICS, compare_runs
from demo_bench.application.cost_ledger import (
    DEFAULT_WARN_FRACTION,
    compute_cost_ledger,
    parse_timestamp,
)
from demo_bench.application.image_gate import decide_image
from demo_bench.application.matrix import CONTRASTS, matrix_effects
from demo_bench.application.reset import ResetError, WorkerPod, records_to_json, reset_caches
from demo_bench.application.restore_probe import (
    HTTP_OK,
    ProbeError,
    measure_noise_floor,
    run_restore_probe,
)
from demo_bench.application.schedule import (
    PlannedRun,
    SweepRun,
    run_schedule,
    run_sweep_schedule,
)
from demo_bench.application.sizing import sizing_report
from demo_bench.application.slo_goodput import (
    SloThresholds,
    bootstrap_goodput_ci,
    calibration,
    evaluate,
    load_samples,
    sensitivity_grid,
)
from demo_bench.application.sweep import build_sweep_data, dump_sweep_data
from demo_bench.application.trace import (
    generate_session_trace,
    lru_accesses,
    read_trace_jsonl,
    write_trace_jsonl,
)
from demo_bench.application.trace_multiturn import generate_multiturn_trace
from demo_bench.application.trace_schedule import ARRIVALS, schedule_trace
from demo_bench.application.trace_w2 import encode_jsonl, generate_w2_trace, sha256_hex
from demo_bench.application.w2_gate import (
    EXCLUSIVE,
    L2_MODES,
    PLAN_RULES,
    RULE_SETS,
    run_gate,
)
from demo_bench.domain.kv_sizing import KvSizing, kv_bytes_per_token, load_model_dims
from demo_bench.domain.ports import HttpClient, HttpTransportError
from demo_bench.domain.promql import (
    engine_hit_rate_query,
    router_predicted_hit_query,
    tier_mix_query,
)
from demo_bench.infrastructure.aiperf_reader import read_profile_window, read_run_artifact
from demo_bench.infrastructure.http import UrllibHttpClient
from demo_bench.infrastructure.metrics import PrometheusClient, PrometheusError
from demo_bench.infrastructure.observations_reader import read_observations
from demo_bench.infrastructure.structured_logging import configure_logging

logger = logging.getLogger("demo_bench.cli")

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2
DEFAULT_SYSTEM_PORT = 9090
DEFAULT_NOISE_K = 3.0


def _emit(text: str, out: Path | None) -> None:
    if out is None:
        sys.stdout.write(text)
        return
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    logger.info("output written", extra={"path": str(out)})


def _json(doc: Any) -> str:
    return json.dumps(doc, indent=2, sort_keys=False, default=str) + "\n"


def _emit_both(text: str, out: Path | None) -> None:
    """Print ``text`` and, with ``out``, also write it there (evidence files)."""
    sys.stdout.write(text)
    if out is not None:
        _emit(text, out)


def _read_json(path: Path) -> Any:
    """Decode a JSON file; ``OSError`` and ``ValueError`` mean invalid input (exit 2)."""
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except ValueError as exc:
        raise ValueError(f"{path}: not valid JSON: {exc}") from exc


def _parse_floats(text: str, flag: str) -> list[float]:
    """Parse a non-empty comma list of numbers (``ValueError`` names the flag)."""
    try:
        values = [float(item) for item in text.split(",")]
    except ValueError as exc:
        raise ValueError(f"{flag} must be a comma list of numbers, got {text!r}") from exc
    return values


def _read_string_list(path: Path) -> list[str]:
    """Read a non-empty JSON list of non-empty strings."""
    doc = _read_json(path)
    if not isinstance(doc, list) or not doc:
        raise ValueError(f"{path}: expected a non-empty JSON list of strings")
    if not all(isinstance(item, str) and item for item in doc):
        raise ValueError(f"{path}: every item must be a non-empty string")
    return doc


# --------------------------------------------------------------------------- sizing
def _cmd_sizing(args: argparse.Namespace) -> int:
    if args.kv_bytes_per_token is not None:
        bytes_per_token = int(args.kv_bytes_per_token)
    elif args.config is not None:
        bytes_per_token = kv_bytes_per_token(load_model_dims(args.config), args.kv_dtype)
    else:
        raise ValueError("pass --config (dense/GQA models) or --kv-bytes-per-token (boot log)")
    if args.device_tokens is not None:
        device_tokens = int(args.device_tokens)
    else:
        if args.config is None or None in (args.hbm_gib, args.mem_fraction, args.weights_gib):
            raise ValueError(
                "pass --device-tokens (boot log) or --config with --hbm-gib, --mem-fraction "
                "and --weights-gib"
            )
        device_tokens = KvSizing(
            dims=load_model_dims(args.config),
            kv_dtype=args.kv_dtype,
            hbm_gib=args.hbm_gib,
            mem_fraction=args.mem_fraction,
            weights_gib=args.weights_gib,
            overhead_gib=args.overhead_gib,
        ).device_tokens_per_rank()
    accesses = None
    if args.trace is not None:
        if args.prefix_tokens is None:
            raise ValueError("--trace needs --prefix-tokens")
        accesses = lru_accesses(
            read_trace_jsonl(args.trace),
            prefix_tokens=args.prefix_tokens,
            block_size=args.block_size,
        )
    report = sizing_report(
        bytes_per_token=bytes_per_token,
        device_tokens_per_rank=device_tokens,
        hicache_size_gb=args.hicache_size_gb,
        storage_tokens_per_rank=args.storage_tokens,
        write_through=not args.write_back,
        session_tokens=args.session_tokens,
        accesses=accesses,
    )
    _emit(_json(report.to_dict()), args.out)
    return EXIT_OK


# --------------------------------------------------------------------------- trace
_LEGACY_TRACE_FLAGS = (
    ("sessions", "--sessions"),
    ("prefix_tokens", "--prefix-tokens"),
    ("passes", "--passes"),
    ("seed", "--seed"),
    ("out", "--out"),
)


def _cmd_trace(args: argparse.Namespace) -> int:
    missing = [flag for dest, flag in _LEGACY_TRACE_FLAGS if getattr(args, dest) is None]
    if missing:
        raise ValueError(f"trace: missing required arguments {', '.join(missing)}")
    rows = generate_session_trace(
        sessions=args.sessions,
        prefix_tokens=args.prefix_tokens,
        passes=args.passes,
        seed=args.seed,
        block_size=args.block_size,
        unique_tokens=args.unique_tokens,
        output_tokens=args.output_tokens,
        spacing_ms=args.spacing_ms,
    )
    digest = write_trace_jsonl(rows, args.out)
    _emit(_json({"path": str(args.out), "rows": len(rows), "sha256": digest}), None)
    return EXIT_OK


def _cmd_trace_schedule(args: argparse.Namespace) -> int:
    result = schedule_trace(
        args.input,
        args.out,
        rate=args.rate,
        seed=args.seed,
        arrival=args.arrival,
        duration_s=args.duration,
        rows=args.rows,
    )
    _emit(json.dumps(result.to_dict()) + "\n", None)
    return EXIT_OK


def _cmd_trace_multiturn(args: argparse.Namespace) -> int:
    rows = generate_multiturn_trace(
        sessions=args.sessions,
        turns=args.turns,
        system_tokens=args.system_tokens,
        context_tokens=args.context_tokens,
        growth_tokens=args.growth_tokens,
        output_tokens=args.output_tokens,
        turn_period_ms=args.turn_period_ms,
        seed=args.seed,
        block_size=args.block_size,
    )
    digest = write_trace_jsonl(rows, args.out)
    _emit(json.dumps({"path": str(args.out), "rows": len(rows), "sha256": digest}) + "\n", None)
    return EXIT_OK


def _cmd_trace_w2(args: argparse.Namespace) -> int:
    osl: int | list[int] = args.osl
    if args.osl_file is not None:
        osl = _read_json(args.osl_file)
        if not isinstance(osl, list):
            raise ValueError(f"{args.osl_file}: expected a JSON list of output lengths")
    trace = generate_w2_trace(
        sessions=args.sessions,
        turns=args.turns,
        global_prefix_tokens=args.global_prefix_tokens,
        entry_tokens=args.entry_tokens,
        growth_tokens=args.growth_tokens,
        rate_per_s=args.rate,
        gap_sigma=args.gap_sigma,
        populate_s=args.populate_s,
        cold_fraction=args.cold_fraction,
        cold_min_tokens=args.cold_min_tokens,
        cold_max_tokens=args.cold_max_tokens,
        osl=osl,
        osl_cap=args.osl_cap,
        max_isl=args.max_isl,
        seed=args.seed,
        block_size=args.block_size,
    )
    data, side = encode_jsonl(trace.rows), encode_jsonl(trace.sidecar)
    digest = sha256_hex(data)
    if args.expect_sha256 is not None and digest != args.expect_sha256.lower():
        logger.error(
            "trace sha256 differs from the frozen value; nothing written",
            extra={"sha256": digest, "expected": args.expect_sha256},
        )
        return EXIT_FAILED
    for path, payload in ((args.out, data), (args.sidecar, side)):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
    doc = {
        "path": str(args.out),
        "rows": len(trace.rows),
        "sha256": digest,
        "sidecar": str(args.sidecar),
        "sidecar_sha256": sha256_hex(side),
    }
    logger.info("w2 trace written", extra=doc)
    _emit(json.dumps(doc) + "\n", None)
    return EXIT_OK


# --------------------------------------------------------------------------- w2 gate
def _cmd_w2_gate(args: argparse.Namespace) -> int:
    report = run_gate(
        read_trace_jsonl(args.trace),
        read_trace_jsonl(args.sidecar),
        targets=args.targets,
        gpu_tokens_per_target=args.gpu_tokens_per_target,
        l2_tokens_per_target=args.l2_tokens_per_target,
        turn_duration_s=args.turn_duration_s,
        block_size=args.block_size,
        warmup_s=args.warmup_s,
        uncacheable_tokens=args.uncacheable_tokens,
        max_mamba_gpu=args.max_mamba_gpu,
        max_mamba_host=args.max_mamba_host,
        l2_mode=args.l2_mode,
        rules=args.rules,
        l3_tokens_per_target=args.l3_tokens_per_target,
        context_len=args.context_len,
    )
    _emit_both(_json(report.to_dict()), args.out)
    if not report.passed:
        logger.error(
            "w2 gate failed: adjust the trace before the freeze",
            extra={"failed": [c.name for c in report.checks if not (c.passed or c.informational)]},
        )
        return EXIT_FAILED
    return EXIT_OK


# --------------------------------------------------------------------------- slo goodput
def _cmd_slo_goodput(args: argparse.Namespace) -> int:
    thresholds = SloThresholds(
        cold_prefill_tok_s=args.cold_prefill_tok_s,
        ttft_continuation_s=args.ttft_s,
        itl_ms=args.itl_ms,
        cold_factor=args.cold_factor,
    )
    samples = load_samples(args.profile_export, read_trace_jsonl(args.sidecar))
    if args.window_s is None:
        window = read_profile_window(args.profile_export)
        window_s = window.end_s - window.start_s
    else:
        window_s = args.window_s
    result = evaluate(samples, thresholds, window_s=window_s)
    lo, hi = bootstrap_goodput_ci(
        samples, thresholds, window_s=window_s, n_boot=args.n_boot, seed=args.seed, alpha=args.alpha
    )
    doc: dict[str, Any] = {
        "thresholds": thresholds.to_dict(),
        "window_s": window_s,
        "classes": result.classes,
        "excluded_entry": result.excluded_entry,
        "attainment": result.attainment,
        "goodput": result.goodput,
        "goodput_ci": [lo, hi],
        "bootstrap": {
            "unit": "session",
            "n_boot": args.n_boot,
            "seed": args.seed,
            "alpha": args.alpha,
        },
        "sensitivity": sensitivity_grid(
            samples,
            thresholds,
            window_s=window_s,
            itl_ms_values=_parse_floats(args.itl_grid, "--itl-grid"),
            ttft_s_values=_parse_floats(args.ttft_grid, "--ttft-grid"),
        ),
    }
    calib = None
    if args.accept_band is not None:
        band = _parse_floats(args.accept_band, "--accept-band")
        if len(band) != 2:  # noqa: PLR2004 - LO,HI
            raise ValueError(f"--accept-band must be LO,HI, got {args.accept_band!r}")
        calib = calibration(samples, thresholds, band=(band[0], band[1]))
        doc["calibration"] = calib
    _emit_both(_json(doc), args.out)
    if calib is not None and not calib["in_band"]:
        logger.error(
            "SLO attainment outside the acceptance band: recalibrate before the freeze",
            extra={"attainment": calib["attainment"], "band": calib["band"]},
        )
        return EXIT_FAILED
    return EXIT_OK


# --------------------------------------------------------------------------- image gate
def _cmd_image_gate(args: argparse.Namespace) -> int:
    decision = decide_image(
        _read_json(args.derived),
        _read_json(args.control),
        accept_length_ratio=args.accept_length_ratio,
        prefill_ratio=args.prefill_ratio,
    )
    _emit_both(_json(decision.to_dict()), args.out)
    return EXIT_OK


# --------------------------------------------------------------------------- restore probe
def _flush_callable(http: HttpClient, urls: Sequence[str], timeout_s: float) -> Callable[[], None]:
    """Return a callable that POSTs ``{}`` to every URL in order (non-200: ``ProbeError``)."""
    for url in urls:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ValueError(f"--flush-url must be an absolute http(s) URL, got {url!r}")

    def flush() -> None:
        for url in urls:
            response = http.request("POST", url, json={}, timeout=timeout_s)
            if response.status_code != HTTP_OK:
                raise ProbeError(f"flush POST {url} returned HTTP {response.status_code}")

    return flush


def _cmd_restore_probe(args: argparse.Namespace) -> int:
    if args.noise_k is not None and not args.flush_url:
        raise ValueError("--noise-k needs --flush-url (the noise floor is measured with flushes)")
    prompts = _read_string_list(args.prompts)
    fillers = _read_string_list(args.filler)
    storage = None if args.storage_filler is None else _read_string_list(args.storage_filler)
    http = UrllibHttpClient(args.timeout_s)
    flush = _flush_callable(http, args.flush_url, args.timeout_s) if args.flush_url else None
    try:
        floor = None
        if flush is not None:
            floor = measure_noise_floor(
                http,
                base_url=args.base_url,
                model=args.model,
                prompts=prompts,
                flush=flush,
                max_tokens=args.max_tokens,
                top_logprobs=args.top_logprobs,
                timeout_s=args.timeout_s,
                base_tolerance=args.tolerance,
                k=DEFAULT_NOISE_K if args.noise_k is None else args.noise_k,
            )
        report = run_restore_probe(
            http,
            base_url=args.base_url,
            model=args.model,
            prompts=prompts,
            filler_prompts=fillers,
            storage_filler_prompts=storage,
            max_tokens=args.max_tokens,
            top_logprobs=args.top_logprobs,
            tolerance=args.tolerance,
            timeout_s=args.timeout_s,
            flush=flush,
            noise_floor=floor,
        )
    except (ProbeError, HttpTransportError) as exc:
        logger.error("restore probe failed", extra={"error": str(exc)})  # noqa: TRY400 - expected
        return EXIT_FAILED
    _emit_both(_json(report.to_dict()), args.out)
    if not report.ok:
        logger.error(
            "restore probe: prompts failed",
            extra={"passed": report.passed, "total": report.total},
        )
        return EXIT_FAILED
    return EXIT_OK


# --------------------------------------------------------------------------- cost ledger
def _cmd_cost_ledger(args: argparse.Namespace) -> int:
    now = datetime.now(UTC) if args.now is None else parse_timestamp(args.now, what="--now")
    prices = _read_json(args.prices)
    if not isinstance(prices, dict):
        raise ValueError(f"{args.prices}: expected a JSON object instance type -> USD/h")
    odcrs: Any = [] if args.odcr is None else _read_json(args.odcr)
    if not isinstance(odcrs, list):
        raise ValueError(f"{args.odcr}: expected a JSON list of ODCR objects")
    ledger = compute_cost_ledger(
        _read_json(args.nodeclaims),
        nodepool=args.nodepool,
        prices=prices,
        now=now,
        budget_usd=args.budget_usd,
        odcrs=odcrs,
        warn_fraction=args.warn_fraction,
    )
    _emit_both(_json(ledger.to_dict()), args.out)
    fields = {
        "total_usd": round(ledger.total_usd, 2),
        "budget_usd": ledger.budget_usd,
        "percent": round(100 * ledger.fraction, 1),
    }
    if ledger.status == "exceeded":
        logger.error(
            "ALERT: budget exceeded; stop the experiment and ask the operator", extra=fields
        )
        return EXIT_FAILED
    if ledger.status == "alert":
        logger.warning("ALERT: spend reached the warn fraction of the budget", extra=fields)
    return EXIT_OK


# --------------------------------------------------------------------------- metrics
def _cmd_metrics(args: argparse.Namespace) -> int:
    if not args.prometheus_url:
        raise ValueError("pass --prometheus-url or set PROMETHEUS_URL")
    window = read_profile_window(args.profile_export)
    prom = PrometheusClient(
        args.prometheus_url, UrllibHttpClient(args.timeout_s), timeout_s=args.timeout_s
    )
    try:
        metrics = collect_run_metrics(prom, args.namespace, window)
    except PrometheusError:
        logger.exception("prometheus query failed")
        return EXIT_FAILED
    doc = {
        "namespace": args.namespace,
        "window": {"start_s": window.start_s, "end_s": window.end_s, "seconds": window.seconds},
        "queries": {
            "engine_hit_rate": engine_hit_rate_query(args.namespace, window.seconds),
            "router_predicted_hit": router_predicted_hit_query(args.namespace, window.seconds),
            "tier_tokens": tier_mix_query(args.namespace, window.seconds),
        },
        **metrics.to_dict(),
    }
    _emit(_json(doc), args.out)
    return EXIT_OK


# --------------------------------------------------------------------------- reset
def _parse_pod(spec: str, *, is_hicache: bool, default_port: int) -> WorkerPod:
    name, sep, target = spec.partition("=")
    if not sep or not name or not target:
        raise ValueError(f"pod spec {spec!r} must be NAME=HOST[:PORT]")
    host, _, port = target.rpartition(":") if ":" in target else (target, "", "")
    return WorkerPod(
        name=name,
        host=host,
        is_hicache=is_hicache,
        system_port=int(port) if port else default_port,
    )


def _cmd_reset(args: argparse.Namespace) -> int:
    port = args.system_port
    pods = [_parse_pod(s, is_hicache=False, default_port=port) for s in args.pod or []]
    pods += [_parse_pod(s, is_hicache=True, default_port=port) for s in args.hicache_pod or []]
    try:
        records = reset_caches(
            pods,
            UrllibHttpClient(args.timeout_s),
            max_attempts=args.max_attempts,
            backoff_initial_s=args.backoff_initial_s,
            backoff_factor=args.backoff_factor,
            max_wait_s=args.max_wait_s,
            timeout_s=args.timeout_s,
        )
    except ResetError as exc:
        logger.error("cache reset failed", extra={"error": str(exc)})  # noqa: TRY400 - expected
        _emit(records_to_json(exc.records, error=str(exc)), args.out)
        return EXIT_FAILED
    _emit(records_to_json(records), args.out)
    return EXIT_OK


# --------------------------------------------------------------------------- compare
def _cmd_compare(args: argparse.Namespace) -> int:
    baseline = read_run_artifact(args.baseline, label=args.baseline_label)
    candidate = read_run_artifact(args.candidate, label=args.candidate_label)
    metrics = [m.strip() for m in args.metrics.split(",") if m.strip()] if args.metrics else None
    comparison = compare_runs(baseline, candidate, metrics)
    if args.format == "json":
        text = _json(
            {
                "baseline": {"label": baseline.label, "aiperf_version": baseline.aiperf_version},
                "candidate": {"label": candidate.label, "aiperf_version": candidate.aiperf_version},
                "rows": [asdict(r) for r in comparison.rows],
                "baseline_errors": comparison.baseline_errors,
                "candidate_errors": comparison.candidate_errors,
                "missing": sorted(comparison.missing),
            }
        )
    else:
        text = comparison.to_markdown()
    if baseline.aiperf_version != candidate.aiperf_version:
        logger.warning(
            "aiperf versions differ: not the same series",
            extra={"baseline": baseline.aiperf_version, "candidate": candidate.aiperf_version},
        )
    _emit(text, args.out)
    return EXIT_OK


# --------------------------------------------------------------------------- matrix
def _cmd_matrix(args: argparse.Namespace) -> int:
    observations = read_observations(args.observations, args.kpi)
    effects = matrix_effects(observations, n_boot=args.n_boot, seed=args.seed, alpha=args.alpha)
    if args.format == "json":
        doc = {
            "kpi": args.kpi,
            "n_boot": args.n_boot,
            "alpha": args.alpha,
            "effects": {
                k: {**asdict(e), "ratio": e.ratio, "ratio_ci": list(e.ratio_ci)}
                for k, e in effects.items()
            },
        }
        _emit(_json(doc), args.out)
        return EXIT_OK
    level = round(100 * (1 - args.alpha))
    lines = [
        f"KPI: {args.kpi} (effects on log scale; ratio = exp(effect); {level}% bootstrap CI)",
        "",
        "| effect | log estimate | ratio | ratio CI |",
        "|---|---:|---:|---|",
    ]
    for k in CONTRASTS:
        e = effects[k]
        lo, hi = e.ratio_ci
        lines.append(f"| {k} | {e.estimate:+.4f} | {e.ratio:.3f} | [{lo:.3f}, {hi:.3f}] |")
    _emit("\n".join(lines) + "\n", args.out)
    return EXIT_OK


# --------------------------------------------------------------------------- schedule
_POSITIVE_INT = re.compile(r"[0-9]+")
_PAIR = re.compile(r"([A-Za-z0-9]+):([A-Za-z0-9]+)")


def _parse_concurrencies(text: str) -> list[int]:
    items = text.split(",")
    if not all(_POSITIVE_INT.fullmatch(item) for item in items):
        raise ValueError(f"--sweep-concurrency must be a comma list of integers, got {text!r}")
    return [int(item) for item in items]


def _cmd_schedule(args: argparse.Namespace) -> int:
    topologies = None if args.topologies is None else args.topologies.split(",")
    runs: Sequence[PlannedRun]
    if args.sweep_concurrency is None:
        runs = run_schedule(rounds=args.rounds, topologies=topologies)
    else:
        runs = run_sweep_schedule(
            _parse_concurrencies(args.sweep_concurrency),
            rounds=args.rounds,
            topologies=topologies,
        )
    if args.format == "json":
        _emit(_json([{**asdict(r), "namespace": r.namespace} for r in runs]), args.out)
        return EXIT_OK
    lines = []
    for r in runs:
        cols = [
            str(r.round),
            r.topology,
            r.router,
            r.scenario,
            "deploy" if r.new_deploy else "toggle",
            r.namespace,
        ]
        if isinstance(r, SweepRun):
            cols.append(f"concurrency={r.concurrency}")
        lines.append("\t".join(cols))
    _emit("\n".join(lines) + "\n", args.out)
    return EXIT_OK


# --------------------------------------------------------------------------- plot
def _parse_pairs(text: str) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    for item in text.split(","):
        match = _PAIR.fullmatch(item)
        if match is None:
            raise ValueError(f"--pairs items must be BASELINE:CANDIDATE, got {item!r} in {text!r}")
        pairs.append((match.group(1), match.group(2)))
    return pairs


def _cmd_plot_sweep(args: argparse.Namespace) -> int:
    pairs = _parse_pairs(args.pairs) if args.pairs is not None else []
    data = build_sweep_data(args.runs, setup=args.setup, pairs=pairs)
    # Imported here: the chart adapter needs the optional `plot` extra (matplotlib).
    from demo_bench.infrastructure.sweep_charts import (  # noqa: PLC0415 - optional dependency
        PlotDependencyError,
        render_sweep_charts,
    )

    try:
        written = render_sweep_charts(data, args.out)
    except PlotDependencyError as exc:
        logger.error(str(exc))  # noqa: TRY400 - expected, one line, no traceback
        return EXIT_FAILED
    data_path = Path(args.out) / "sweep_data.json"
    data_path.write_text(dump_sweep_data(data), encoding="utf-8")
    summary = {
        "out": str(args.out),
        "files": [p.name for p in [*written, data_path]],
        "missing_points": len(data["missing"]),
        "excluded_runs": len(data["excluded"]),
    }
    _emit(_json(summary), None)
    return EXIT_OK


# --------------------------------------------------------------------------- parser
def _add_out(p: argparse.ArgumentParser) -> None:
    p.add_argument("--out", type=Path, default=None, help="write to this file instead of stdout")


def _add_sizing(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    p = sub.add_parser("sizing", help="per-rank KV capacity and expected LRU tier mix")
    p.add_argument("--config", type=Path, help="model config.json (dense/GQA models)")
    p.add_argument("--kv-dtype", default="fp8_e4m3", help="KV-cache dtype (default fp8_e4m3)")
    p.add_argument("--hbm-gib", type=float, help="HBM per GPU, GiB")
    p.add_argument("--mem-fraction", type=float, help="--mem-fraction-static")
    p.add_argument("--weights-gib", type=float, help="weights per GPU, GiB")
    p.add_argument("--overhead-gib", type=float, default=0.0, help="other static GiB per GPU")
    p.add_argument("--kv-bytes-per-token", type=int, help="override (from the boot log; MLA)")
    p.add_argument("--device-tokens", type=int, help="device pool per rank (boot log)")
    p.add_argument("--hicache-size-gb", type=float, default=0.0, help="--hicache-size per rank")
    p.add_argument("--storage-tokens", type=int, default=0, help="L3 budget per rank, tokens")
    p.add_argument("--write-back", action="store_true", help="L2 does not mirror L1")
    p.add_argument("--session-tokens", type=int, help="session length for sessions/rank")
    p.add_argument("--trace", type=Path, help="trace JSONL to replay through the LRU tiers")
    p.add_argument("--prefix-tokens", type=int, help="shared prefix length of --trace")
    p.add_argument("--block-size", type=int, default=512, help="tokens per hash block")
    _add_out(p)
    p.set_defaults(func=_cmd_sizing)


def _add_trace(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    p = sub.add_parser(
        "trace",
        help="generate a session-revisit trace, or stamp a fixed schedule (trace schedule)",
        description=(
            "Without a subcommand: generate a single-turn session-revisit trace "
            "(--sessions, --prefix-tokens, --passes, --seed and --out are required). "
            "'trace schedule': add deterministic timestamps to an unscheduled trace."
        ),
    )
    p.add_argument("--sessions", type=int, help="sessions (shared prefixes)")
    p.add_argument("--prefix-tokens", type=int, help="shared prefix length, tokens")
    p.add_argument("--passes", type=int, help="passes including populate")
    p.add_argument("--seed", type=int, help="RNG seed")
    p.add_argument("--out", type=Path, help="output JSONL")
    p.add_argument("--block-size", type=int, default=512)
    p.add_argument("--unique-tokens", type=int, default=512, help="unique suffix per request")
    p.add_argument("--output-tokens", type=int, default=64)
    p.add_argument("--spacing-ms", type=int, default=1000)
    p.set_defaults(func=_cmd_trace)

    tsub = p.add_subparsers(dest="trace_command", metavar="{schedule,multiturn,w2}")
    s = tsub.add_parser(
        "schedule",
        help="add fixed-schedule timestamps (ms) to an unscheduled mooncake trace",
        description=(
            "Append a deterministic 'timestamp' (ms) to every row of a trace without one, "
            "keeping each row's original bytes, so aiperf --fixed-schedule can replay it."
        ),
    )
    s.add_argument("--in", dest="input", type=Path, required=True, help="unscheduled trace")
    s.add_argument("--out", type=Path, required=True, help="scheduled trace to write")
    s.add_argument("--rate", type=float, required=True, help="mean arrival rate, requests/s")
    s.add_argument("--seed", type=int, required=True, help="RNG seed (poisson)")
    s.add_argument("--arrival", choices=ARRIVALS, default="poisson", help="default poisson")
    limit = s.add_mutually_exclusive_group()
    limit.add_argument("--duration", type=float, help="keep rows with timestamp <= SEC*1000")
    limit.add_argument("--rows", type=int, help="keep the first N rows")
    s.set_defaults(func=_cmd_trace_schedule)

    m = tsub.add_parser(
        "multiturn",
        help="generate the multi-turn conversation trace (Track D) with a fixed schedule",
        description=(
            "N sessions x T turns sharing one system prompt; turn k+1 extends turn k's hash_ids "
            "exactly; turn k of a session is sent at phase + k * TURN_PERIOD_MS. Defaults are "
            "the Track D workload."
        ),
    )
    m.add_argument("--out", type=Path, required=True, help="output JSONL")
    m.add_argument("--seed", type=int, required=True, help="RNG seed (session-to-phase slots)")
    m.add_argument("--turn-period-ms", type=int, required=True, help="Tp: ms between turns")
    m.add_argument("--sessions", type=int, default=40, help="N sessions (default 40)")
    m.add_argument("--turns", type=int, default=8, help="T turns per session (default 8)")
    m.add_argument("--system-tokens", type=int, default=2048, help="shared system prompt")
    m.add_argument(
        "--context-tokens", type=int, default=30720, help="turn-0 context, system included"
    )
    m.add_argument("--growth-tokens", type=int, default=1536, help="tokens added per turn")
    m.add_argument("--output-tokens", type=int, default=128, help="OSL (default 128)")
    m.add_argument("--block-size", type=int, default=512, help="tokens per hash block")
    m.set_defaults(func=_cmd_trace_multiturn)
    _add_trace_w2(tsub)


def _add_trace_w2(tsub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    w = tsub.add_parser(
        "w2",
        help="generate the W2 agentic trace (open loop) and its session/class sidecar",
        description=(
            "Sessions sharing a global prefix enter with --entry-tokens and grow by "
            "--growth-tokens per turn (hash_ids extend the previous turn); idle gaps are "
            "lognormal with mean SESSIONS / RATE; --cold-fraction of the rows are cold "
            "ingestions with never-seen hash_ids. Deterministic by --seed; prints the sha256 "
            "of both files and refuses a trace that differs from --expect-sha256 (exit 1)."
        ),
    )
    w.add_argument("--out", type=Path, required=True, help="mooncake JSONL to write")
    w.add_argument("--sidecar", type=Path, required=True, help="sidecar JSONL to write")
    w.add_argument("--seed", type=int, required=True, help="RNG seed")
    w.add_argument("--sessions", type=int, required=True, help="live sessions N_s")
    w.add_argument("--turns", type=int, required=True, help="turns per session, entry included")
    w.add_argument(
        "--global-prefix-tokens", type=int, required=True, help="prefix shared by every session"
    )
    w.add_argument(
        "--entry-tokens", type=int, required=True, help="turn-0 input, global prefix included"
    )
    w.add_argument("--growth-tokens", type=int, required=True, help="tokens added per turn")
    w.add_argument("--rate", type=float, required=True, help="continuation rate, turns/s")
    w.add_argument("--gap-sigma", type=float, default=1.2, help="lognormal sigma (default 1.2)")
    w.add_argument("--populate-s", type=float, required=True, help="entry window, seconds")
    w.add_argument("--cold-fraction", type=float, required=True, help="cold share of all rows")
    w.add_argument("--cold-min-tokens", type=int, required=True, help="smallest cold ingestion")
    w.add_argument("--cold-max-tokens", type=int, required=True, help="largest cold ingestion")
    osl = w.add_mutually_exclusive_group(required=True)
    osl.add_argument("--osl", type=int, help="fixed output length")
    osl.add_argument("--osl-file", type=Path, help="JSON list of output lengths to sample from")
    w.add_argument("--osl-cap", type=int, default=12000, help="output length cap (default 12000)")
    w.add_argument("--max-isl", type=int, default=200000, help="largest input (default 200000)")
    w.add_argument("--block-size", type=int, default=512, help="tokens per hash block")
    w.add_argument("--expect-sha256", metavar="HEX", help="frozen sha256 of the trace file")
    w.set_defaults(func=_cmd_trace_w2)


def _add_w2_gate(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    p = sub.add_parser(
        "w2-gate",
        help="offline routed-LRU gate of a W2 trace (hit ceilings, per-pod footprint, thresholds)",
        description=(
            "Replay the trace on --targets engines (GPU tier + L2 tier each) under round-robin "
            "and kv-sticky routing and apply the plan thresholds. --l2-mode inclusive models "
            "SGLang HiCache write-through (GPU u L2 holds the L2 size of distinct tokens); "
            "--rules d41 applies the d41 composed per-target checks instead of the node-level "
            "w_over_sum ones (needs --l2-mode inclusive, --l3-tokens-per-target and "
            "--context-len). Exit 0 when every check passes, 1 when one fails, 2 for invalid "
            "input."
        ),
    )
    p.add_argument("--trace", type=Path, required=True, help="W2 mooncake JSONL")
    p.add_argument("--sidecar", type=Path, required=True, help="W2 sidecar JSONL")
    p.add_argument("--targets", type=int, required=True, help="router targets (engines)")
    p.add_argument(
        "--gpu-tokens-per-target", type=int, required=True, help="GPU KV per engine (boot log)"
    )
    p.add_argument(
        "--l2-tokens-per-target", type=int, required=True, help="L2 KV per engine (0: none)"
    )
    p.add_argument(
        "--turn-duration-s", type=float, required=True, help="expected turn duration (overlap)"
    )
    p.add_argument("--block-size", type=int, default=512, help="tokens per hash block")
    p.add_argument("--warmup-s", type=float, default=0.0, help="rows before are not counted")
    p.add_argument(
        "--uncacheable-tokens",
        type=int,
        default=65536,
        help="never-seen tokens that make a prefill uncacheable (default 65536)",
    )
    p.add_argument("--max-mamba-gpu", type=int, help="mamba slots per engine on GPU")
    p.add_argument("--max-mamba-host", type=int, help="mamba states per engine in the host pool")
    p.add_argument(
        "--l2-mode",
        choices=L2_MODES,
        default=EXCLUSIVE,
        help="exclusive: L2 victim tier (default); inclusive: HiCache write-through copy",
    )
    p.add_argument(
        "--rules",
        choices=RULE_SETS,
        default=PLAN_RULES,
        help="plan: plan 4.4 thresholds (default); d41: the d41 composed per-target checks",
    )
    p.add_argument(
        "--l3-tokens-per-target",
        type=int,
        default=0,
        help="L3 KV per engine, tokens (required > 0 with --rules d41)",
    )
    p.add_argument(
        "--context-len",
        type=int,
        default=None,
        help="engine context length, tokens (required with --rules d41)",
    )
    p.add_argument("--out", type=Path, default=None, help="also write the report here")
    p.set_defaults(func=_cmd_w2_gate)


def _add_slo_goodput(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    p = sub.add_parser(
        "slo-goodput",
        help="SLO goodput of a W2 run (classes, sensitivity grid, session bootstrap CI)",
        description=(
            "Pair aiperf's per-request profile_export.jsonl with the W2 sidecar and count the "
            "SLO-met continuation and cold-ingestion turns per second (entry turns are the "
            "populate and are never judged). Exit 1 when --accept-band is given and the "
            "attainment is outside it, 2 for invalid input."
        ),
    )
    p.add_argument("--profile-export", type=Path, required=True, help="profile_export.jsonl")
    p.add_argument("--sidecar", type=Path, required=True, help="W2 sidecar JSONL")
    p.add_argument(
        "--cold-prefill-tok-s", type=float, required=True, help="cold prefill rate (pilot), tok/s"
    )
    p.add_argument("--window-s", type=float, help="measured window (default: profiling span)")
    p.add_argument("--ttft-s", type=float, default=2.0, help="continuation TTFT limit, s")
    p.add_argument("--itl-ms", type=float, default=25.0, help="ITL limit, ms")
    p.add_argument("--cold-factor", type=float, default=1.4, help="cold TTFT slack factor")
    p.add_argument("--itl-grid", default="15,25,50", help="ITL values of the grid, ms")
    p.add_argument("--ttft-grid", default="1,2,4", help="continuation TTFT values of the grid, s")
    p.add_argument("--n-boot", type=int, default=1000, help="bootstrap replicates")
    p.add_argument("--seed", type=int, default=0, help="bootstrap seed")
    p.add_argument("--alpha", type=float, default=0.05, help="CI level (0.05: 95 %%)")
    p.add_argument("--accept-band", metavar="LO,HI", help="S0 calibration band of attainment")
    p.add_argument("--out", type=Path, default=None, help="also write the report here")
    p.set_defaults(func=_cmd_slo_goodput)


def _add_image_gate(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    p = sub.add_parser(
        "image-gate",
        help="pre-registered image choice from two restore-probe reports (derived vs dev.1)",
        description=(
            "Outcomes: derived_only_pass, both_pass, control_only_pass, both_fail. Exit 0 "
            "whatever the decision, 2 for invalid input."
        ),
    )
    p.add_argument("--derived", type=Path, required=True, help="probe report, derived image")
    p.add_argument("--control", type=Path, required=True, help="probe report, dev.1 control")
    p.add_argument(
        "--accept-length-ratio", type=float, required=True, help="accept length, derived / dev.1"
    )
    p.add_argument(
        "--prefill-ratio", type=float, required=True, help="cold prefill at 48K, derived / dev.1"
    )
    p.add_argument("--out", type=Path, default=None, help="also write the decision here")
    p.set_defaults(func=_cmd_image_gate)


def _add_metrics(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    p = sub.add_parser("metrics", help="engine/router/tier KPIs over the aiperf window")
    p.add_argument("--namespace", required=True, help="DGD namespace")
    p.add_argument("--profile-export", type=Path, required=True, help="profile_export.jsonl")
    p.add_argument(
        "--prometheus-url",
        default=os.environ.get("PROMETHEUS_URL"),
        help="Prometheus base URL (env PROMETHEUS_URL)",
    )
    p.add_argument("--timeout-s", type=float, default=30.0)
    _add_out(p)
    p.set_defaults(func=_cmd_metrics)


def _add_reset(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    p = sub.add_parser("reset", help="flush caches on every worker pod (system port)")
    p.add_argument("--pod", action="append", metavar="NAME=HOST[:PORT]", help="worker pod")
    p.add_argument(
        "--hicache-pod",
        action="append",
        metavar="NAME=HOST[:PORT]",
        help="HiCache worker pod (also clears L3 storage)",
    )
    p.add_argument("--system-port", type=int, default=DEFAULT_SYSTEM_PORT)
    p.add_argument("--max-attempts", type=int, default=6)
    p.add_argument("--backoff-initial-s", type=float, default=1.0)
    p.add_argument("--backoff-factor", type=float, default=2.0)
    p.add_argument("--max-wait-s", type=float, default=60.0)
    p.add_argument("--timeout-s", type=float, default=30.0)
    _add_out(p)
    p.set_defaults(func=_cmd_reset)


def _add_compare(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    p = sub.add_parser("compare", help="A/B table of two aiperf summary exports")
    p.add_argument("--baseline", type=Path, required=True, help="profile_export_aiperf.json")
    p.add_argument("--candidate", type=Path, required=True, help="profile_export_aiperf.json")
    p.add_argument("--baseline-label", default="baseline")
    p.add_argument("--candidate-label", default="candidate")
    p.add_argument("--metrics", help=f"comma list (default {','.join(DEFAULT_METRICS)})")
    p.add_argument("--format", choices=("markdown", "json"), default="markdown")
    _add_out(p)
    p.set_defaults(func=_cmd_compare)


def _add_matrix(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    p = sub.add_parser("matrix", help="factorial effects R/O/D with bootstrap CIs")
    p.add_argument("--observations", type=Path, required=True, help="CSV, one row per run")
    p.add_argument("--kpi", required=True, help="KPI column name")
    p.add_argument("--n-boot", type=int, default=1000)
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--alpha", type=float, default=0.05)
    p.add_argument("--format", choices=("markdown", "json"), default="markdown")
    _add_out(p)
    p.set_defaults(func=_cmd_matrix)


def _add_schedule(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    p = sub.add_parser("schedule", help="print the planned matrix runs, one per line")
    p.add_argument("--rounds", type=int, default=3)
    p.add_argument(
        "--topologies",
        metavar="LIST",
        help="comma list of topologies (t-agg,t-agg-hic,t-dis,t-dis-hic) for a chained subset "
        "plan; default: the full 4-topology plan",
    )
    p.add_argument(
        "--sweep-concurrency",
        metavar="LIST",
        help="comma list of concurrencies: closed-loop sweep plan (adds a 7th TSV column "
        "concurrency=<N>)",
    )
    p.add_argument("--format", choices=("tsv", "json"), default="tsv")
    _add_out(p)
    p.set_defaults(func=_cmd_schedule)


def _add_plot(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    p = sub.add_parser("plot", help="presentation charts (needs the 'plot' extra: matplotlib)")
    psub = p.add_subparsers(dest="plot_command", required=True, metavar="{sweep}")
    s = psub.add_parser(
        "sweep",
        help="TTFT/ITL/throughput (and speedup) vs concurrency from closed-loop sweep runs",
    )
    s.add_argument("--runs", type=Path, required=True, help="directory searched for run.json")
    s.add_argument("--out", type=Path, required=True, help="output directory for PNG/SVG/JSON")
    s.add_argument("--setup", required=True, help="setup label printed on every chart")
    s.add_argument(
        "--pairs",
        metavar="LIST",
        help="comma list BASELINE:CANDIDATE (e.g. S0:S1,S0:S2) for the TTFT speedup chart",
    )
    s.set_defaults(func=_cmd_plot_sweep)


def _add_restore_probe(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    p = sub.add_parser(
        "restore-probe",
        help="HiCache restore equivalence: gpu vs host (vs storage) hit, logprobs + cached_tokens",
        description=(
            "Per prompt: cold, gpu hit, fillers, host hit (and storage fillers, storage hit), "
            "one request at a time against POST BASE_URL/v1/completions. Passes when "
            "cached_tokens is identical and every top-1 logprob is within the tolerance."
        ),
    )
    p.add_argument("--base-url", required=True, help="OpenAI-compatible endpoint base URL")
    p.add_argument("--model", required=True, help="model name sent in every request")
    p.add_argument("--prompts", type=Path, required=True, help="JSON list of probe prompts")
    p.add_argument("--filler", type=Path, required=True, help="JSON list: evict from GPU")
    p.add_argument("--storage-filler", type=Path, help="JSON list: evict from host (L3 stage)")
    p.add_argument("--max-tokens", type=int, default=256)
    p.add_argument("--top-logprobs", type=int, default=5)
    p.add_argument("--tolerance", type=float, default=1e-3, help="max top-1 logprob diff")
    p.add_argument("--timeout-s", type=float, default=300.0, help="per-request timeout")
    p.add_argument(
        "--flush-url",
        action="append",
        metavar="URL",
        help="cache flush endpoint, POSTed with {} (repeatable, in order); measures the noise "
        "floor first and flushes once before the probe",
    )
    p.add_argument(
        "--noise-k",
        type=float,
        help=f"tolerance = max(--tolerance, K x floor) (default {DEFAULT_NOISE_K}; needs "
        "--flush-url)",
    )
    p.add_argument("--out", type=Path, default=None, help="also write the report here")
    p.set_defaults(func=_cmd_restore_probe)


def _add_cost_ledger(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    p = sub.add_parser(
        "cost-ledger",
        help="NodeClaim node-hours x price + ODCR hourly cost against a budget",
        description=(
            "Exit 0 below the budget (an ALERT line on stderr from the warn fraction), "
            "1 when the budget is reached, 2 for invalid input."
        ),
    )
    p.add_argument("--nodeclaims", type=Path, required=True, help="kubectl get nodeclaims -o json")
    p.add_argument("--nodepool", required=True, help="karpenter.sh/nodepool label value")
    p.add_argument("--prices", type=Path, required=True, help="JSON object: type -> USD/h")
    p.add_argument("--budget-usd", type=float, required=True, help="budget, USD")
    p.add_argument("--odcr", type=Path, help="JSON list {id, hourly_usd, created_at, end_at?}")
    p.add_argument("--now", help="end of the billing window, RFC 3339 (default: current time)")
    p.add_argument(
        "--warn-fraction",
        type=float,
        default=DEFAULT_WARN_FRACTION,
        help=f"alert from this fraction of the budget (default {DEFAULT_WARN_FRACTION})",
    )
    p.add_argument("--out", type=Path, default=None, help="also write the ledger here")
    p.set_defaults(func=_cmd_cost_ledger)


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser.

    Returns:
        The parser with the subcommands sizing, trace, metrics, reset, compare, matrix, schedule,
        plot, restore-probe, cost-ledger, w2-gate, slo-goodput and image-gate.
    """
    parser = argparse.ArgumentParser(
        prog="demo-bench",
        description="Benchmark tooling for the Dynamo on EKS demo.",
    )
    parser.add_argument(
        "--log-level",
        default=os.environ.get("DEMO_BENCH_LOG_LEVEL", "INFO"),
        help="log level (env DEMO_BENCH_LOG_LEVEL; default INFO)",
    )
    sub = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")
    for add in (
        _add_sizing,
        _add_trace,
        _add_metrics,
        _add_reset,
        _add_compare,
        _add_matrix,
        _add_schedule,
        _add_plot,
        _add_restore_probe,
        _add_cost_ledger,
        _add_w2_gate,
        _add_slo_goodput,
        _add_image_gate,
    ):
        add(sub)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the CLI.

    Args:
        argv: Arguments without the program name (default ``sys.argv[1:]``).

    Returns:
        Process exit code: 0 ok, 1 operation failed, 2 invalid input.
    """
    args = build_parser().parse_args(argv)
    configure_logging(args.log_level)
    func: Callable[[argparse.Namespace], int] = args.func
    try:
        return func(args)
    except (ValueError, OSError) as exc:
        logger.error(  # noqa: TRY400 - user-facing input error, traceback not useful
            "command failed", extra={"command": args.command, "error": str(exc)}
        )
        return EXIT_USAGE
