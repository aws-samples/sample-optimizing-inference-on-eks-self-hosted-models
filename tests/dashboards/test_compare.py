# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""demo/compare.json: baseline vs optimized, side by side, by namespace (FT-6c).  RED until the dashboard exists.

Ported from an earlier `dynamo-bench-overview` layout, adapted to this sample:
* the two arms are two namespaces (one DGD per namespace), picked by the single-value variables `$namespace_a` (Baseline) and
  `$namespace_b` (Optimized); the Stockholm `dgd`/`run`/`arm_*` variables do not exist here;
* SGLang metrics (`sglang:*`), never `vllm:*`;
* HiCache panels instead of the Stockholm KVBM ones;
* no price panels (no `$ per 1M tokens`, no `$price_*` variable);
* the "Concurrency level" annotation of matrix.json, scoped to both namespaces.
Every metric is validated against the allowlists (Dynamo v1.5.1 catalog + SGLang 0.5.18 names), like the other dashboards.
"""
from __future__ import annotations

import json
import re

import pytest
import yaml

import _lib as L

COMPARE = L.DASH / "demo" / "compare.json"
CM = L.DASH / "demo" / "compare-configmap.yaml"
A, B = "namespace_a", "namespace_b"
NS_ARM = {A: re.compile(r'namespace\s*=\s*"\$\{?namespace_a\}?"'), B: re.compile(r'namespace\s*=\s*"\$\{?namespace_b\}?"')}

STATS = {"TTFT p50 (selected range)", "TTFT p95 (selected range)", "Engine hit rate (selected range)", "Output tokens/s (selected range)"}
TIMESERIES = {
    "TTFT p50 over time", "TTFT p95 over time", "ITL p95 over time", "In-flight requests per arm", "Engine hit rate per arm",
    "Output tokens/s", "Tokens by cache source",
    "HiCache host occupancy", "HiCache load-back", "HiCache prefetch", "Cache tier occupancy (L1/L2/L3)",
}
EXPECTED = STATS | TIMESERIES

REQUIRED = {  # title -> regexes that must each match some expr of the panel
    "TTFT p50 (selected range)": [r"histogram_quantile\(\s*0\.5\b", r"dynamo_frontend_time_to_first_token_seconds_bucket", r"\$__range"],
    "TTFT p95 (selected range)": [r"histogram_quantile\(\s*0\.95\b", r"dynamo_frontend_time_to_first_token_seconds_bucket", r"\$__range"],
    "Engine hit rate (selected range)": [r"increase\(\s*sglang:cached_tokens_total", r"increase\(\s*sglang:prompt_tokens_total", r"\$__range"],
    "Output tokens/s (selected range)": [r"dynamo_frontend_output_tokens_total", r"\$__range"],
    "TTFT p50 over time": [r"histogram_quantile\(\s*0\.5\b", r"dynamo_frontend_time_to_first_token_seconds_bucket", r"\$__rate_interval"],
    "TTFT p95 over time": [r"histogram_quantile\(\s*0\.95\b", r"dynamo_frontend_time_to_first_token_seconds_bucket", r"\$__rate_interval"],
    "ITL p95 over time": [r"histogram_quantile\(\s*0\.95\b", r"sglang:inter_token_latency_seconds_bucket", r"\$__rate_interval"],
    "In-flight requests per arm": [r"dynamo_frontend_inflight_requests"],
    "Engine hit rate per arm": [r"rate\(\s*sglang:cached_tokens_total", r"rate\(\s*sglang:prompt_tokens_total"],
    "Output tokens/s": [r"rate\(\s*dynamo_frontend_output_tokens_total"],
    "Tokens by cache source": [r"sum by \(cache_source\)", r"sglang:cached_tokens_total", r'cache_source\s*!=\s*"total"'],
    "HiCache host occupancy": [r"sglang:hicache_host_used_tokens", r"sglang:hicache_host_total_tokens"],
    "HiCache load-back": [r"rate\(\s*sglang:load_back_tokens_total"],
    "HiCache prefetch": [r"sglang:prefetched_tokens_total"],
    "Cache tier occupancy (L1/L2/L3)": [
        r"sglang:kv_used_tokens", r"sglang:kv_evictable_tokens", r"sglang:max_total_num_tokens",
        r"sglang:hicache_host_used_tokens", r"sglang:hicache_host_total_tokens", r"sglang:backuped_tokens_total",
    ],
}

FORBIDDEN = [  # regex, reason
    (r"vllm:", "SGLang metrics only"),
    (r"kvbm", "HiCache replaces KVBM"),
    (r"\bdgd\b", "arms are namespaces here, not DGD names"),
    (r"price", "no price panels or variables"),
    (r"\$\s*per\b|\$/", "no cost panels"),
    (r"sglang:cache_hit_rate", "derive from cached/prompt tokens"),
    (r"sglang:prefetch_bandwidth", "never observed with the nixl backend"),
    (r"dynamo_frontend_worker_active_", "absent live"),
    (r"nixl_", "no NIXL panel in the compare view"),
    (r"dynamo_component_router_kv_hit_rate(?!_sum|_count|_bucket)", "bare gauge name"),
]


@pytest.fixture(scope="module")
def dash():
    assert COMPARE.is_file(), f"missing {COMPARE}"
    return L.load_json(COMPARE)


def _panels(dash):
    return L.panels_by_title(dash)


def test_identity(dash):
    assert dash["uid"] == "dynamo-eks-demo-compare"
    assert "baseline" in dash["title"].lower() and "optimized" in dash["title"].lower()


def test_variables_are_datasource_and_two_single_value_namespaces(dash):
    v = {x["name"]: x for x in dash["templating"]["list"]}
    assert set(v) == {"datasource", A, B}, "no run/arm/price variables"
    assert v["datasource"]["type"] == "datasource" and v["datasource"]["query"] == "prometheus"
    assert v[A]["label"] == "Baseline" and v[B]["label"] == "Optimized"
    for name in (A, B):
        ns = v[name]
        assert ns["type"] == "query" and "label_values" in str(ns["query"]) and ns["datasource"]["uid"] == "${datasource}"
        assert not ns.get("multi") and not ns.get("includeAll"), f"{name} must be single-value (the selectors use `=`)"


def test_exact_panel_set(dash):
    titles = {p["title"] for p in L.iter_panels(dash) if p.get("type") != "text"}
    assert titles == EXPECTED, (sorted(titles - EXPECTED), sorted(EXPECTED - titles))


@pytest.mark.parametrize("title", sorted(STATS))
def test_stat_panels_type_and_range(dash, title):
    assert _panels(dash)[title]["type"] == "stat"


@pytest.mark.parametrize("title", sorted(TIMESERIES))
def test_timeseries_panels_type(dash, title):
    assert _panels(dash)[title]["type"] == "timeseries"


@pytest.mark.parametrize("title", sorted(REQUIRED))
def test_panel_queries(dash, title):
    p = _panels(dash)
    assert title in p, f"panel {title!r} missing"
    joined = "\n".join(L.exprs(p[title]))
    for rx in REQUIRED[title]:
        assert re.search(rx, joined), f"{title}: no expr matches /{rx}/ in:\n{joined}"


@pytest.mark.parametrize("title", sorted(EXPECTED))
def test_every_panel_shows_both_arms_with_their_own_legend(dash, title):
    p = _panels(dash)[title]
    es = L.exprs(p)
    for arm in (A, B):
        assert any(NS_ARM[arm].search(e) for e in es), f"{title}: no query for ${arm}"
    for t in p["targets"]:
        if "expr" in t:
            assert t.get("legendFormat"), f"{title}: every target needs a legendFormat to tell the arms apart"
    legends = " ".join(t.get("legendFormat", "").lower() for t in p["targets"])
    assert "baseline" in legends and "optimized" in legends, (title, legends)


def test_every_selector_is_scoped_to_exactly_one_arm(dash):
    bad = []
    for p in L.iter_panels(dash):
        for e in L.exprs(p):
            sels = L.metric_selectors(e)
            assert sels, (p["title"], e)
            for name, matcher in sels:
                if not matcher or sum(bool(r.search(matcher)) for r in NS_ARM.values()) != 1:
                    bad.append((p["title"], name))
    assert not bad, bad


def test_all_queries_use_datasource_variable(dash):
    for p in L.iter_panels(dash):
        if p.get("type") == "text":
            continue
        assert p["datasource"]["uid"] == "${datasource}", p["title"]
        for t in p.get("targets", []):
            assert t["datasource"]["uid"] == "${datasource}", p["title"]


def test_metric_lint_allowlisted_only(dash):
    allowed = L.allowlist()
    unknown = sorted({m for e in L.all_exprs(dash) for m in L.metric_names(e) if not L.is_known(m, allowed)})
    assert not unknown, f"unknown metrics: {unknown}"


@pytest.mark.parametrize(("rx", "why"), FORBIDDEN)
def test_forbidden_content(rx, why):
    assert COMPARE.is_file(), f"missing {COMPARE}"
    assert not re.search(rx, COMPARE.read_text(), re.IGNORECASE), f"/{rx}/ present: {why}"


def test_no_price_or_cost_panel_titles(dash):
    for p in L.iter_panels(dash):
        assert not re.search(r"\$|price|cost|usd", p["title"], re.IGNORECASE), p["title"]


def test_hicache_replaces_kvbm_in_rows_and_titles(dash):
    rows = [p["title"] for p in dash["panels"] if p.get("type") == "row"]
    assert any("hicache" in r.lower() for r in rows), rows
    assert not any("kvbm" in t.lower() for t in rows + list(_panels(dash)))


def test_tokens_by_cache_source_not_aggregated_with_total(dash):
    (e,) = [e for e in L.exprs(_panels(dash)["Tokens by cache source"]) if NS_ARM[A].search(e)][:1]
    assert 'cache_source!="total"' in e.replace(" ", "")


def test_concurrency_level_annotation(dash):
    anns = [a for a in dash["annotations"]["list"] if a.get("name") == "Concurrency level"]
    assert len(anns) == 1
    (a,) = anns
    assert a["datasource"]["uid"] == "${datasource}" and a.get("enable") is True
    assert "dynamo_frontend_inflight_requests" in a["expr"]
    assert re.search(r"namespace\s*=~\s*\"\$namespace_a\|\$namespace_b\"", a["expr"]), a["expr"]
    assert "$namespace}" not in a["expr"] and "$namespace\"" not in a["expr"], "the matrix `$namespace` variable does not exist here"
    assert a["titleFormat"] == "c = {{c}}" and a["tagKeys"] == "c"
    assert not (set(L.metric_names(a["expr"])) - {"dynamo_frontend_inflight_requests"}) and L.is_known("dynamo_frontend_inflight_requests", L.allowlist())


def test_default_time_range_and_refresh(dash):
    assert dash["time"]["to"] == "now" and dash["time"]["from"].startswith("now-")
    assert dash["refresh"] in ("5s", "10s", "30s", "1m")


def test_configmap_wraps_the_compare_dashboard(dash):
    assert CM.is_file(), f"missing {CM}"
    cm = yaml.safe_load(CM.read_text())
    assert cm["kind"] == "ConfigMap" and cm["metadata"]["name"] == "grafana-demo-compare-dashboard"
    assert cm["metadata"]["namespace"] == "monitoring" and cm["metadata"]["labels"]["grafana_dashboard"] == "1"
    assert json.loads(cm["data"]["compare.json"]) == dash


def test_readme_documents_the_compare_dashboard():
    text = (L.DASH / "README.md").read_text()
    assert "demo/compare.json" in text and "compare-configmap.yaml" in text
    assert "namespace_a" in text and "namespace_b" in text
