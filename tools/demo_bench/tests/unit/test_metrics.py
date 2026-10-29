# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests for ``infrastructure.metrics`` PromQL builders, window and Prometheus adapter."""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

import pytest


# --------------------------------------------------------------------------- PromQL strings
def test_engine_hit_rate_query_exact() -> None:
    from demo_bench.infrastructure.metrics import engine_hit_rate_query

    assert engine_hit_rate_query("s-agg", 900) == (
        'sum(increase(sglang:cached_tokens_total{namespace="s-agg"}[900s])) / '
        'sum(increase(sglang:prompt_tokens_total{namespace="s-agg"}[900s]))'
    )


def test_engine_hit_rate_never_uses_cache_hit_rate_gauge() -> None:
    from demo_bench.infrastructure.metrics import engine_hit_rate_query

    assert "cache_hit_rate" not in engine_hit_rate_query("s-dis", 60)


def test_router_predicted_hit_query_exact() -> None:
    """Router-predicted hit is the ratio of increases over the run window (spec 11, corrected).

    The old form divided by ``clamp_min(sum(rate(..._count)), 1)``: below 1 req/s the clamp makes
    the denominator larger than the true count and understates the hit rate. No ``clamp_min``.
    """
    from demo_bench.infrastructure.metrics import router_predicted_hit_query

    q = router_predicted_hit_query("s-agg-hic", 120)
    assert q == (
        'sum(increase(dynamo_component_router_kv_hit_rate_sum{namespace="s-agg-hic"}[120s])) / '
        'sum(increase(dynamo_component_router_kv_hit_rate_count{namespace="s-agg-hic"}[120s]))'
    )
    assert "clamp_min" not in q and "rate(" not in q.replace("increase(", "")


def test_tier_mix_query_exact_excludes_total() -> None:
    from demo_bench.infrastructure.metrics import tier_mix_query

    assert tier_mix_query("s-dis-hic", 300) == (
        'sum by (cache_source) (increase(sglang:cached_tokens_total'
        '{namespace="s-dis-hic",cache_source!="total"}[300s]))'
    )


@pytest.mark.parametrize("ns", ['bad"ns', "a}b", "x\ny"])
def test_namespace_is_validated_against_promql_injection(ns: str) -> None:
    from demo_bench.infrastructure.metrics import engine_hit_rate_query

    with pytest.raises(ValueError):
        engine_hit_rate_query(ns, 60)


@pytest.mark.parametrize("w", [0, -5])
def test_non_positive_window_rejected(w: int) -> None:
    from demo_bench.infrastructure.metrics import tier_mix_query

    with pytest.raises(ValueError):
        tier_mix_query("s-agg", w)


# --------------------------------------------------------------------------- window
def test_window_from_per_request_export_excludes_warmup(fixtures_dir: Path) -> None:
    from demo_bench.infrastructure.aiperf_reader import read_profile_window

    w = read_profile_window(fixtures_dir / "aiperf_run_window" / "profile_export.jsonl")
    # warmup rows (benchmark_phase == "warmup") start at +0 s; profiling starts at +10 s, ends +36.5 s
    assert w.start_s == pytest.approx(1_760_000_010.0)
    assert w.end_s == pytest.approx(1_760_000_036.5)
    assert w.seconds == 27  # ceil(26.5)


def test_window_with_only_warmup_rows_raises(fixtures_dir: Path) -> None:
    from demo_bench.infrastructure.aiperf_reader import read_profile_window

    with pytest.raises(ValueError, match="profiling"):
        read_profile_window(fixtures_dir / "aiperf_run_window" / "profile_export_warmup_only.jsonl")


# --------------------------------------------------------------------------- Prometheus client
def _prom_ok(result: list[dict[str, Any]]) -> dict[str, Any]:
    return {"status": "success", "data": {"resultType": "vector", "result": result}}


def test_prometheus_client_issues_instant_query_through_transport(
    fake_http_client_cls: Any, fake_response_cls: Any
) -> None:
    from demo_bench.infrastructure.metrics import PrometheusClient

    http = fake_http_client_cls(
        lambda req: fake_response_cls(200, _prom_ok([{"metric": {"pod": "w0"},
                                                       "value": [1760000036.5, "0.42"]}]))
    )
    prom = PrometheusClient("http://prometheus.monitoring.svc:9090/", http)
    samples = prom.query("up", at=1_760_000_036.5)
    req = http.requests[0]
    assert req.method == "GET"
    assert req.url == "http://prometheus.monitoring.svc:9090/api/v1/query"
    assert req.params is not None and req.params["query"] == "up"
    assert float(req.params["time"]) == pytest.approx(1_760_000_036.5)
    assert samples[0].labels == {"pod": "w0"} and samples[0].value == pytest.approx(0.42)


def test_prometheus_client_raises_on_http_error(
    fake_http_client_cls: Any, fake_response_cls: Any
) -> None:
    from demo_bench.infrastructure.metrics import PrometheusClient, PrometheusError

    http = fake_http_client_cls(lambda req: fake_response_cls(503, {}, "unavailable"))
    with pytest.raises(PrometheusError):
        PrometheusClient("http://p:9090", http).query("up", at=1.0)


def test_prometheus_client_raises_on_error_status_body(
    fake_http_client_cls: Any, fake_response_cls: Any
) -> None:
    from demo_bench.infrastructure.metrics import PrometheusClient, PrometheusError

    http = fake_http_client_cls(
        lambda req: fake_response_cls(200, {"status": "error", "errorType": "bad_data",
                                            "error": "parse error"})
    )
    with pytest.raises(PrometheusError, match="parse error"):
        PrometheusClient("http://p:9090", http).query("up{", at=1.0)


def test_query_scalar_none_for_empty_and_nan(
    fake_http_client_cls: Any, fake_response_cls: Any
) -> None:
    from demo_bench.infrastructure.metrics import PrometheusClient

    empty = PrometheusClient("http://p", fake_http_client_cls(
        lambda r: fake_response_cls(200, _prom_ok([]))))
    nan = PrometheusClient("http://p", fake_http_client_cls(
        lambda r: fake_response_cls(200, _prom_ok([{"metric": {}, "value": [1, "NaN"]}]))))
    val = PrometheusClient("http://p", fake_http_client_cls(
        lambda r: fake_response_cls(200, _prom_ok([{"metric": {}, "value": [1, "0.7"]}]))))
    assert empty.query_scalar("q", at=1.0) is None  # absent is not zero
    assert nan.query_scalar("q", at=1.0) is None
    assert val.query_scalar("q", at=1.0) == pytest.approx(0.7)
    assert not math.isnan(val.query_scalar("q", at=1.0) or 0.0)


def test_router_predicted_hit_zero_count_increase_is_none_never_zero(
    fake_http_client_cls: Any, fake_response_cls: Any, fixtures_dir: Path
) -> None:
    """0/0 (no router observations in the window) is reported as ``None``, not ``0.0``.

    Prometheus answers ``NaN`` for ``increase(_sum)/increase(_count)`` with a zero count, or an
    empty vector when the series is absent. Both must map to ``None`` so a missing router
    signal is never confused with a 0% predicted hit.
    """
    from demo_bench.application.collect_metrics import collect_run_metrics
    from demo_bench.infrastructure.aiperf_reader import read_profile_window
    from demo_bench.infrastructure.metrics import PrometheusClient

    window = read_profile_window(fixtures_dir / "aiperf_run_window" / "profile_export.jsonl")
    for router_result in ([{"metric": {}, "value": [1, "NaN"]}], []):

        def handler(req: Any, router_result: Any = router_result) -> Any:
            if "router_kv_hit_rate" in req.params["query"]:
                return fake_response_cls(200, _prom_ok(router_result))
            if "by (cache_source)" in req.params["query"]:
                return fake_response_cls(200, _prom_ok([]))
            return fake_response_cls(200, _prom_ok([{"metric": {}, "value": [1, "0.3"]}]))

        m = collect_run_metrics(
            PrometheusClient("http://p:9090", fake_http_client_cls(handler)), "s-agg", window
        )
        assert m.router_predicted_hit is None
        assert m.router_predicted_hit != 0.0
        assert m.engine_hit_rate == pytest.approx(0.3)


# --------------------------------------------------------------------------- collect_run_metrics
def test_collect_run_metrics_uses_exact_queries_and_window_end(
    fake_http_client_cls: Any, fake_response_cls: Any, fixtures_dir: Path
) -> None:
    from demo_bench.application.collect_metrics import collect_run_metrics
    from demo_bench.infrastructure.aiperf_reader import read_profile_window
    from demo_bench.infrastructure.metrics import (
        PrometheusClient,
        engine_hit_rate_query,
        router_predicted_hit_query,
        tier_mix_query,
    )

    window = read_profile_window(fixtures_dir / "aiperf_run_window" / "profile_export.jsonl")
    eng = engine_hit_rate_query("s-agg-hic", window.seconds)
    rtr = router_predicted_hit_query("s-agg-hic", window.seconds)
    tier = tier_mix_query("s-agg-hic", window.seconds)

    def handler(req: Any) -> Any:
        q = req.params["query"]
        assert float(req.params["time"]) == pytest.approx(window.end_s)
        if q == eng:
            return fake_response_cls(200, _prom_ok([{"metric": {}, "value": [1, "0.55"]}]))
        if q == rtr:
            return fake_response_cls(200, _prom_ok([{"metric": {}, "value": [1, "0.61"]}]))
        if q == tier:
            return fake_response_cls(200, _prom_ok([
                {"metric": {"cache_source": "device"}, "value": [1, "1000"]},
                {"metric": {"cache_source": "host"}, "value": [1, "400"]},
                {"metric": {"cache_source": "storage_nixl"}, "value": [1, "100"]},
            ]))
        raise AssertionError(f"unexpected query: {q}")

    http = fake_http_client_cls(handler)
    m = collect_run_metrics(PrometheusClient("http://p:9090", http), "s-agg-hic", window)
    assert m.engine_hit_rate == pytest.approx(0.55)
    assert m.router_predicted_hit == pytest.approx(0.61)
    assert m.tier_tokens == {"device": 1000.0, "host": 400.0, "storage_nixl": 100.0}
    assert len(http.requests) == 3


def test_collect_run_metrics_round_robin_has_no_router_metric_reported_as_none(
    fake_http_client_cls: Any, fake_response_cls: Any, fixtures_dir: Path
) -> None:
    from demo_bench.application.collect_metrics import collect_run_metrics
    from demo_bench.infrastructure.aiperf_reader import read_profile_window
    from demo_bench.infrastructure.metrics import PrometheusClient

    window = read_profile_window(fixtures_dir / "aiperf_run_window" / "profile_export.jsonl")

    def handler(req: Any) -> Any:
        if "router_kv_hit_rate" in req.params["query"]:
            return fake_response_cls(200, _prom_ok([]))  # histogram never observed in rr
        if "by (cache_source)" in req.params["query"]:
            return fake_response_cls(200, _prom_ok([]))
        return fake_response_cls(200, _prom_ok([{"metric": {}, "value": [1, "0.1"]}]))

    m = collect_run_metrics(PrometheusClient("http://p:9090", fake_http_client_cls(handler)),
                            "s-agg", window)
    assert m.router_predicted_hit is None  # not 0.0
    assert m.tier_tokens == {}
    assert m.engine_hit_rate == pytest.approx(0.1)
