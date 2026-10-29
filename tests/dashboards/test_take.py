# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""demo/take.json: the hall-screen "demo view" of ONE recorded take (one namespace).  RED until the dashboard exists.

The recording shows one configuration at a time (baseline S0, then the best measured); the two takes are placed side by side in editing, so
this view is for one namespace (`$namespace`, set from the URL: `var-namespace=<ns>`), big panels only, dark theme, kiosk, refresh 5 s.

Verified live (Prometheus of the recording cluster, read-only, 2026-10-20), so the queries below use names and label values that exist:
* `dynamo_frontend_time_to_first_token_seconds_bucket` `le` values: 0.0 0.0022 0.0047 0.01 0.022 0.047 0.1 0.22 0.47 **1.0** 2.2 4.7 **10.0** 22.0 48.0 100.0
  220.0 480.0 +Inf.  So 1 s and 10 s ARE bucket boundaries; the label value is the string "1.0" / "10.0" (`le="1"` matches nothing).
* labels `namespace`, `pod`, `model` on the frontend series; `dynamo_frontend_{inflight,queued}_requests`, `dynamo_frontend_output_tokens_total`,
  `dynamo_component_router_kv_hit_rate_{sum,count}`, `sglang:{cached,prompt}_tokens_total`, `sglang:kv_{used,evictable}_tokens`,
  `sglang:max_total_num_tokens`, `sglang:hicache_host_{used,total}_tokens`, `sglang:backuped_tokens_total`, `sglang:num_{running,queue}_reqs`.
* prefill/decode split: the SGLang series carry `engine_type` in {prefill, decode, unified} (cross-pod AND in-pod; `dynamo_component_type` is `worker`
  for in-pod PD, so it cannot split them).
* highest finite TTFT bucket is 480 s (>= the 173 s slowest turn of the recorded S0 storm): histogram_quantile does not cap there, so the p95 title
  carries no "capped" suffix (it would be required if the top finite bucket were below ~180 s).  Buckets are coarse above 100 s (100, 220, 480).
* waiting for first token = sum of `dynamo_frontend_stage_requests{stage=~"preprocess|route|dispatch"}` (the v1.5.1 catalog: `queued_requests` and
  `inflight_requests` are deprecated; "in flight" is a mixed signal in the recorded runs, S1 is higher at minutes 5-7).  Live labels: `stage`, `phase`.
* NOT used because absent live: `dynamo_frontend_worker_active_*`, NIXL series.
"""
from __future__ import annotations

import json
import re

import pytest
import yaml

import _lib as L

TAKE = L.DASH / "demo" / "take.json"
CM = L.DASH / "demo" / "take-configmap.yaml"
FOOTER = "demo recording · 1x p6-b200 · not the benchmark (see slides)"
HERO_TITLES = ("turns with TTFT > 10 s", "TTFT p95 since replay start")
SECONDARY = "% of turns with TTFT < 1 s"
STAT_TITLES = (*HERO_TITLES, SECONDARY)
TS = "Requests waiting for first token", "TTFT p50", "KV cache hit rate", "Output tokens/s"
TOP_FINITE_BUCKET_S = 480.0  # verified live; below ~180 s a "capped at" suffix would be required in the p95 title
ROWS = {"HiCache tiers": ["Cache tier occupancy (L1/L2/L3)"], "PD workers": ["Prefill vs decode: running/queued requests"]}
NS = r'namespace\s*=~?\s*"\$\{?namespace\}?"'


@pytest.fixture(scope="module")
def dash():
    assert TAKE.is_file(), f"missing {TAKE}"
    return L.load_json(TAKE)


def _panels(dash):
    return L.panels_by_title(dash)


def _text(p):
    return "\n".join(L.exprs(p))


def test_identity_and_demo_label(dash):
    assert dash["uid"] == "demo-take", "scripts/70-demo-take.sh builds /d/demo-take/ URLs"
    assert dash["title"] == "Dynamo demo recording: take view"


def test_dark_kiosk_friendly_defaults(dash):
    assert dash["refresh"] == "5s" and dash["time"]["from"] == "now-10m" and dash["time"]["to"] == "now"
    assert dash.get("style", "dark") == "dark"
    assert dash.get("graphTooltip", 0) in (0, 1, 2)


def test_variables_are_datasource_and_one_single_value_namespace(dash):
    v = {x["name"]: x for x in dash["templating"]["list"]}
    assert set(v) == {"datasource", "namespace"}
    assert v["datasource"]["type"] == "datasource" and v["datasource"]["query"] == "prometheus"
    ns = v["namespace"]
    # Contract changed 2026-10-23: the options no longer come from a time-range-dependent label_values query (see
    # test_take_namespace_variable.py); here only the single-value shape is kept.
    assert ns["type"] == "custom"
    assert not ns.get("multi") and not ns.get("includeAll")


def test_exact_panel_set(dash):
    non_text = {p["title"] for p in L.iter_panels(dash) if p.get("type") != "text"}
    want = set(STAT_TITLES) | set(TS) | {t for ts in ROWS.values() for t in ts}
    assert non_text == want, (sorted(non_text - want), sorted(want - non_text))
    texts = [p for p in L.iter_panels(dash) if p.get("type") == "text"]
    assert len(texts) == 1


def test_footer_text_panel_is_the_last_one_with_the_exact_label(dash):
    (footer,) = [p for p in L.iter_panels(dash) if p.get("type") == "text"]
    assert FOOTER in footer["options"]["content"]
    top = [p for p in dash["panels"] if p.get("type") != "row"]
    assert max(top, key=lambda p: (p["gridPos"]["y"], p["gridPos"]["x"])) is footer
    assert footer["gridPos"]["w"] == 24 and footer["gridPos"]["h"] <= 3


# ----------------------------------------------------------------------------- hero + secondary stats
@pytest.mark.parametrize("title", STAT_TITLES)
def test_stat_panels_are_cumulative_over_the_dashboard_range(dash, title):
    p = _panels(dash)[title]
    assert p["type"] == "stat"
    (e,) = L.exprs(p)
    assert "$__range" in e and "$__rate_interval" not in e and "$__interval" not in e, e
    assert "increase(" in e


@pytest.mark.parametrize("title", HERO_TITLES)
def test_hero_stats_are_big(dash, title):
    p = _panels(dash)[title]
    assert p["gridPos"]["w"] >= 8 and p["gridPos"]["h"] >= 6
    assert p["options"]["text"]["valueSize"] >= 64, "readable from the back of a hall"


def test_pct_under_1s_is_a_secondary_stat(dash):
    panels = _panels(dash)
    sec = panels[SECONDARY]
    for h in HERO_TITLES:
        hero = panels[h]
        assert sec["options"]["text"]["valueSize"] < hero["options"]["text"]["valueSize"]
        assert sec["gridPos"]["w"] * sec["gridPos"]["h"] < hero["gridPos"]["w"] * hero["gridPos"]["h"] or sec["gridPos"]["y"] > hero["gridPos"]["y"]
    assert sec["options"]["text"]["valueSize"] >= 32
    (e,) = L.exprs(sec)
    assert re.search(r'dynamo_frontend_time_to_first_token_seconds_bucket\{[^}]*le="1\.0"', e), e
    assert "dynamo_frontend_time_to_first_token_seconds_count" in e and "/" in e and e.count("increase(") == 2
    d = sec["fieldConfig"]["defaults"]
    assert d["unit"] == "percentunit" or (d["unit"] == "percent" and "100" in e)


def test_turns_over_10s_is_count_minus_the_le_10_bucket(dash):
    p = _panels(dash)[HERO_TITLES[0]]
    (e,) = L.exprs(p)
    assert re.search(r'dynamo_frontend_time_to_first_token_seconds_bucket\{[^}]*le="10\.0"', e), e
    assert "dynamo_frontend_time_to_first_token_seconds_count" in e and " - " in e.replace("\n", " ") and e.count("increase(") == 2
    assert p["fieldConfig"]["defaults"]["unit"] in ("none", "short", "locale")
    assert not re.search(r'le="10"(?!\.)', e), 'the le label value is the string "10.0"'


def test_p95_since_replay_start_is_computed_over_the_range_not_a_1m_rate(dash):
    p = _panels(dash)[HERO_TITLES[1]]
    (e,) = L.exprs(p)
    flat = re.sub(r"\s+", " ", e)
    assert re.search(r"histogram_quantile\(\s*0\.95\s*,\s*sum by \(le\) \(\s*increase\(\s*dynamo_frontend_time_to_first_token_seconds_bucket\{[^}]*\}\[\$__range\]\s*\)\s*\)\s*\)", flat), e
    assert p["fieldConfig"]["defaults"]["unit"] == "s"
    assert "[1m]" not in e and "rate(" not in flat.replace("increase(", "")
    assert TOP_FINITE_BUCKET_S >= 180 or "capped" in p["title"].lower()
    assert "capped" not in p["title"].lower() or TOP_FINITE_BUCKET_S < 180


def test_no_rate_1m_p95_anywhere_as_a_stat(dash):
    for p in L.iter_panels(dash):
        if p.get("type") == "stat":
            for e in L.exprs(p):
                assert "[1m]" not in e and "$__rate_interval" not in e, (p["title"], e)


# ----------------------------------------------------------------------------- time series
def test_waiting_for_first_token_replaces_in_flight(dash):
    p = _panels(dash)["Requests waiting for first token"]
    assert p["type"] == "timeseries"
    (e,) = L.exprs(p)
    assert "dynamo_frontend_stage_requests" in e
    m = re.search(r'stage\s*=~\s*"([^"]+)"', e)
    assert m and set(m.group(1).split("|")) == {"preprocess", "route", "dispatch"}, e
    assert e.lstrip().startswith("sum(") and "by (" not in e, "one total series, the sum across the three stages"
    for deprecated in ("dynamo_frontend_inflight_requests", "dynamo_frontend_queued_requests"):
        assert deprecated not in TAKE.read_text(), deprecated


def test_ttft_p50_log_axis_with_fixed_range(dash):
    p = _panels(dash)["TTFT p50"]
    d = p["fieldConfig"]["defaults"]
    assert p["type"] == "timeseries" and d["unit"] == "s"
    assert d["custom"]["scaleDistribution"]["type"] == "log" and d["custom"]["scaleDistribution"].get("log") in (2, 10)
    assert d["min"] == 0.05 and d["max"] == 200
    (e,) = L.exprs(p)
    assert re.search(r"histogram_quantile\(\s*0\.5\b", e) and "dynamo_frontend_time_to_first_token_seconds_bucket" in e and "$__rate_interval" in e
    assert "sum by (le)" in e.replace("  ", " ")


def test_kv_cache_hit_rate_router_predicted_and_engine_actual(dash):
    p = _panels(dash)["KV cache hit rate"]
    es = L.exprs(p)
    t = _text(p)
    assert len(es) == 2
    assert "dynamo_component_router_kv_hit_rate_sum" in t and "dynamo_component_router_kv_hit_rate_count" in t
    assert "sglang:cached_tokens_total" in t and "sglang:prompt_tokens_total" in t
    legends = " ".join((x.get("legendFormat") or "").lower() for x in p["targets"])
    assert "router" in legends and "engine" in legends
    d = p["fieldConfig"]["defaults"]
    assert d["unit"] == "percentunit" and d["min"] == 0 and d["max"] == 1


def test_output_tokens_per_second(dash):
    p = _panels(dash)["Output tokens/s"]
    assert re.search(r"rate\(\s*dynamo_frontend_output_tokens_total", _text(p)) and p["type"] == "timeseries"


# ----------------------------------------------------------------------------- collapsed rows for O / D takes
@pytest.mark.parametrize("row", sorted(ROWS))
def test_collapsed_row_holds_its_panels(dash, row):
    (r,) = [p for p in dash["panels"] if p.get("type") == "row" and p.get("title") == row]
    assert r.get("collapsed") is True
    assert [p["title"] for p in r["panels"]] == ROWS[row]


def test_hicache_tier_occupancy_has_l1_l2_l3_series(dash):
    p = _panels(dash)["Cache tier occupancy (L1/L2/L3)"]
    t = _text(p)
    for m in ("sglang:kv_used_tokens", "sglang:kv_evictable_tokens", "sglang:max_total_num_tokens", "sglang:hicache_host_used_tokens", "sglang:hicache_host_total_tokens", "sglang:backuped_tokens_total"):
        assert m in t, m
    legends = [(x.get("legendFormat") or "") for x in p["targets"]]
    assert [l.split()[0] for l in legends] == ["L1", "L2", "L3"], legends


def test_pd_workers_split_by_the_engine_type_label(dash):
    p = _panels(dash)["Prefill vs decode: running/queued requests"]
    es = L.exprs(p)
    assert any("sglang:num_running_reqs" in e for e in es) and any("sglang:num_queue_reqs" in e for e in es)
    assert all(re.search(r"by \(\s*engine_type\s*\)", e) for e in es), "dynamo_component_type is `worker` for in-pod PD; engine_type is prefill|decode"
    assert not any("dynamo_component_type" in e.replace("dynamo_component_type=", "") for e in es)


# ----------------------------------------------------------------------------- global rules (as the matrix dashboard)
def test_every_selector_scoped_by_the_single_namespace(dash):
    bad = []
    for p in L.iter_panels(dash):
        for e in L.exprs(p):
            sels = L.metric_selectors(e)
            assert sels, (p["title"], e)
            for name, matcher in sels:
                if not (matcher and re.search(NS, matcher)):
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


@pytest.mark.parametrize("rx", [r"sglang:cache_hit_rate", r"dynamo_frontend_worker_active_", r"nixl_", r"agent_tx_bytes", r"dynamo_component_router_kv_hit_rate(?!_sum|_count|_bucket)"])
def test_forbidden_or_absent_live_names(dash, rx):
    assert not re.search(rx, TAKE.read_text()), rx


def test_configmap_wraps_the_take_dashboard(dash):
    assert CM.is_file(), f"missing {CM}"
    cm = yaml.safe_load(CM.read_text())
    assert cm["kind"] == "ConfigMap" and cm["metadata"]["name"] == "grafana-demo-take-dashboard"
    assert cm["metadata"]["namespace"] == "monitoring" and cm["metadata"]["labels"]["grafana_dashboard"] == "1"
    assert json.loads(cm["data"]["take.json"]) == dash
