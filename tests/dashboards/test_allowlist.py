# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The allowlist itself must be faithful to primary sources and exclude forbidden names."""
from __future__ import annotations

import re

import _lib as L

FORBIDDEN = {"sglang:cache_hit_rate", "dynamo_frontend_worker_active_requests", "dynamo_component_router_kv_hit_rate_bucket_bare"}


def test_dynamo_allowlist_matches_catalog_at_v151():
    cat = set(re.findall(r'<ParamField path="([a-z_0-9]+)"', L.git_show(L.CATALOG_PATH)))
    snap = L._read_list(L.HERE / "allowlist_dynamo.txt")
    assert snap == cat


def test_allowlist_excludes_forbidden():
    assert not (L.allowlist() & FORBIDDEN)


def test_spec_names_are_allowed():
    spec_names = [
        "sglang:cached_tokens_total", "sglang:prompt_tokens_total", "sglang:num_running_reqs",
        "sglang:num_queue_reqs", "sglang:inter_token_latency_seconds_bucket",
        "sglang:hicache_host_used_tokens", "sglang:hicache_host_total_tokens",
        "sglang:load_back_tokens_total", "sglang:prefetched_tokens_total", "sglang:prefetch_bandwidth",
        "dynamo_frontend_time_to_first_token_seconds_bucket", "dynamo_component_router_kv_hit_rate_sum",
        "dynamo_component_router_kv_hit_rate_count", "dynamo_component_kv_cache_events_applied",
        "dynamo_frontend_worker_active_decode_blocks", "dynamo_frontend_worker_active_prefill_tokens",
    ]
    a = L.allowlist()
    assert [n for n in spec_names if not L.is_known(n, a)] == []


def test_metric_extractor_ignores_labels():
    e = 'sum by (dynamo_endpoint) (rate(dynamo_component_requests_total{dynamo_endpoint="generate"}[5m]))'
    assert L.metric_names(e) == {"dynamo_component_requests_total"}
