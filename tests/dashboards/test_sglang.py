# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Fixed sglang.json: no cache_type grouping (SGLang 0.5.18 dropped it; see the evicted test)."""
from __future__ import annotations

import re

import _lib as L

P = L.DASH / "sglang" / "sglang.json"


def test_valid_json():
    assert P.is_file(), f"missing {P}"
    assert L.load_json(P)["panels"]


def test_no_cache_type_anywhere():
    assert "cache_type" not in P.read_text()


def test_evicted_tokens_panel_present_without_cache_type():
    """The evicted-tokens panel exists and never groups by ``cache_type``.

    Deliberately NOT asserting ``by (pool)``: primary source
    sgl-project/sglang `python/sglang/srt/observability/metrics_collector.py` @ v0.5.18
    (line ~2060) creates ``sglang:evicted_tokens_total`` with ``labelnames=labels.keys()`` only
    (no ``pool``); only ``sglang:load_back_tokens_total`` adds ``pool`` (line ~2080). Dynamo
    v1.5.1 `dev/observability/grafana_dashboards/sglang.json:4019` still groups by the older
    ``cache_type`` label. No primary source shows a ``pool`` label on evicted tokens.
    """
    d = L.load_json(P)
    ev = [e for e in L.all_exprs(d) if "sglang:evicted_tokens_total" in e]
    assert ev, "evicted_tokens_total panel missing"
    assert not any("cache_type" in e for e in ev)


def test_no_cache_hit_rate_gauge():
    assert "sglang:cache_hit_rate" not in P.read_text()


def test_metric_lint():
    d = L.load_json(P)
    allowed = L.allowlist()
    unknown = sorted({m for e in L.all_exprs(d) for m in L.metric_names(e) if not L.is_known(m, allowed)})
    assert not unknown, f"metrics not in primary-source allowlist: {unknown}"
