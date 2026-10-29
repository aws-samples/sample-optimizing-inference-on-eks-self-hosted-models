# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""demo/matrix.json: every panel of spec section 11, scoped by $namespace, on ${datasource}."""
from __future__ import annotations

import re

import pytest
import yaml

import _lib as L

MATRIX = L.DASH / "demo" / "matrix.json"
CM = L.DASH / "demo" / "matrix-configmap.yaml"

# Panels whose queries filter by node/pod labels instead of the namespace.
NODE_SCOPED = {"NVLink throughput (DCGM)", "Node disk reads (md*)"}

REQUIRED = {  # title -> regexes that must each match some expr in the panel
    "Frontend TTFT p50/p95": [r"dynamo_frontend_time_to_first_token_seconds_bucket", r"histogram_quantile\(\s*0\.5\b", r"histogram_quantile\(\s*0\.95\b"],
    "Engine hit rate per worker": [r"(rate|increase)\(\s*sglang:cached_tokens_total", r"(rate|increase)\(\s*sglang:prompt_tokens_total"],
    "Router predicted hit": [r"dynamo_component_router_kv_hit_rate_sum", r"dynamo_component_router_kv_hit_rate_count"],
    "KV events applied": [r"rate\(\s*dynamo_component_kv_cache_events_applied"],
    "Load spread: running/queued requests": [r"sglang:num_running_reqs", r"sglang:num_queue_reqs"],
    "Load spread: active decode blocks / prefill tokens": [r"dynamo_frontend_worker_active_decode_blocks", r"dynamo_frontend_worker_active_prefill_tokens"],
    "ITL": [r"sglang:inter_token_latency_seconds_bucket"],
    "NIXL bytes transferred": [r"nixl_bytes_transferred_count"],
    "NIXL failed transfers": [r"nixl_num_failed_transfers_total"],
    "NVLink throughput (DCGM)": [r"DCGM_FI_PROF_NVLINK_TX_BYTES", r"DCGM_FI_PROF_NVLINK_RX_BYTES", r"\bpod\b"],
    "Tokens by cache source": [r"sum by \(cache_source\)", r"sglang:cached_tokens_total", r'cache_source\s*!=\s*"total"'],
    "HiCache host occupancy": [r"sglang:hicache_host_used_tokens", r"sglang:hicache_host_total_tokens"],
    "HiCache load-back": [r"rate\(\s*sglang:load_back_tokens_total"],
    # FT-4c: sglang:prefetch_bandwidth is never observed with the nixl backend (see test_trackc_fixes.py)
    "HiCache prefetch": [r"sglang:prefetched_tokens_total"],
    "Node disk reads (md*)": [r"rate\(\s*node_disk_read_bytes_total", r'device\s*=~\s*"[^"]*md\.\*', r"\b(node|instance|nodename)\b\s*=~?"],
}

FORBIDDEN = [
    r"sglang:cache_hit_rate",
    r"dynamo_frontend_worker_active_requests",
    r"dynamo_component_router_kv_hit_rate(?!_sum|_count|_bucket)",
]


@pytest.fixture(scope="module")
def dash():
    assert MATRIX.is_file(), f"missing {MATRIX}"
    return L.load_json(MATRIX)


def test_valid_json_with_identity(dash):
    assert dash["title"] and dash["uid"]
    assert dash["uid"] != "dynamo-dashboard"


@pytest.mark.parametrize("title", sorted(REQUIRED))
def test_panel_present_and_queries(dash, title):
    panels = L.panels_by_title(dash)
    assert title in panels, f"panel {title!r} missing; have {sorted(panels)}"
    joined = "\n".join(L.exprs(panels[title]))
    for rx in REQUIRED[title]:
        assert re.search(rx, joined), f"{title}: no expr matches /{rx}/ in:\n{joined}"


@pytest.mark.parametrize("rx", FORBIDDEN)
def test_forbidden_metric_absent(dash, rx):
    hits = [e for e in L.all_exprs(dash) if re.search(rx, e)]
    assert not hits, hits
    assert not re.search(rx, MATRIX.read_text()), "forbidden name appears in JSON text"


def test_every_expr_scoped_by_namespace(dash):
    bad = []
    for p in L.iter_panels(dash):
        if p.get("title") in NODE_SCOPED:
            continue
        for e in L.exprs(p):
            if not L.NS_RE.search(e):
                bad.append((p.get("title"), e))
    assert not bad, bad


def test_every_selector_scoped(dash):
    """Each metric selector (not just the expression) must carry the namespace matcher."""
    bad = []
    for p in L.iter_panels(dash):
        if p.get("title") in NODE_SCOPED:
            continue
        for e in L.exprs(p):
            for name, matcher in L.metric_selectors(e):
                if not (matcher and L.NS_RE.search(matcher)):
                    bad.append((p.get("title"), name))
    assert not bad, bad


def test_node_panels_filter_by_node_or_pod(dash):
    panels = L.panels_by_title(dash)
    for t in NODE_SCOPED:
        assert t in panels
        for e in L.exprs(panels[t]):
            assert re.search(r"\b(pod|node|instance|nodename|hostname)\b\s*=~?", e), (t, e)


def test_no_rate_on_nvlink_gauges(dash):
    for e in L.all_exprs(dash):
        assert not re.search(r"(rate|irate|increase|delta|deriv)\s*\([^()]*DCGM_FI_PROF_NVLINK", e), e
        assert not re.search(r"DCGM_FI_PROF_NVLINK_\w+(\{[^}]*\})?\s*\[", e), e


def test_cache_source_excludes_total_in_all_cache_source_queries(dash):
    for e in L.all_exprs(dash):
        if "cache_source" in e and "sglang:cached_tokens_total" in e and "by (cache_source)" in e:
            assert re.search(r'cache_source\s*!=\s*"total"', e) or re.search(r'cache_source\s*!~\s*"[^"]*total', e), e


def test_templating_variables(dash):
    v = {x["name"]: x for x in dash["templating"]["list"]}
    assert v["datasource"]["type"] == "datasource" and v["datasource"]["query"] == "prometheus"
    ns = v["namespace"]
    assert ns["type"] == "query" and "label_values" in str(ns["query"])
    assert ns["datasource"]["uid"] == "${datasource}"


def test_all_queries_use_datasource_variable(dash):
    for p in L.iter_panels(dash):
        assert p["datasource"]["uid"] == "${datasource}", p["title"]
        for t in p.get("targets", []):
            assert t["datasource"]["uid"] == "${datasource}", p["title"]


def test_metric_lint(dash):
    allowed = L.allowlist()
    unknown = sorted({m for e in L.all_exprs(dash) for m in L.metric_names(e) if not L.is_known(m, allowed)})
    assert not unknown, f"unknown metrics: {unknown}"


def test_configmap_wraps_matrix(dash):
    assert CM.is_file(), f"missing {CM}"
    cm = yaml.safe_load(CM.read_text())
    assert cm["kind"] == "ConfigMap"
    assert cm["metadata"]["name"] == "grafana-demo-matrix-dashboard"
    assert cm["metadata"]["namespace"] == "monitoring"
    assert cm["metadata"]["labels"]["grafana_dashboard"] == "1"
    import json
    assert json.loads(cm["data"]["matrix.json"]) == dash


# --- metric extractor: label names are not metrics --------------------------------------------
# Labels `dynamo_component_type` / `dynamo_graph_deployment` are added by the Dynamo v1.5.1 operator
# PodMonitors (deploy/helm/charts/platform/components/operator/templates/prometheus.yaml @ v1.5.1).
NS = '{namespace="${namespace}"}'


def test_extractor_accepts_label_names_in_by_clause():
    e = f"sum by (dynamo_component_type) (rate(dynamo_frontend_requests_total{NS}[1m]))"
    assert L.metric_selectors(e) == [("dynamo_frontend_requests_total", NS)]


def test_extractor_skips_other_label_lists_and_matcher_keys():
    e = (
        f'sum without (dynamo_component_type) (dynamo_a{NS}) / on (dynamo_graph_deployment) '
        f'group_left (dynamo_component_type) sum(dynamo_b{{namespace="${{namespace}}", '
        f'dynamo_graph_deployment="x"}})'
    )
    assert [n for n, _ in L.metric_selectors(e)] == ["dynamo_a", "dynamo_b"]


def test_extractor_still_flags_unknown_dynamo_metric_unscoped():
    # An unknown dynamo_* *metric* (metric position) is still extracted, so the scope and
    # allowlist checks still see it: no namespace matcher -> would fail test_every_selector_scoped.
    e = "sum by (dynamo_component_type) (rate(dynamo_made_up_total[1m]))"
    sel = L.metric_selectors(e)
    assert sel == [("dynamo_made_up_total", None)]
    assert not (sel[0][1] and L.NS_RE.search(sel[0][1]))
    assert not L.is_known(sel[0][0], L.allowlist())


def test_extractor_flags_unknown_dynamo_metric_even_when_scoped():
    e = f"sum by (dynamo_component_type) (dynamo_made_up_total{NS})"
    names = [n for n, _ in L.metric_selectors(e)]
    assert names == ["dynamo_made_up_total"]
    assert not L.is_known(names[0], L.allowlist())
