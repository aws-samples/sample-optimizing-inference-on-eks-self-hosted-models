# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Dashboard fixes from the real-GPU Track C test (REPORT.md item 4).  RED until the dashboards are fixed.

* dcgm-exporter 4.6.1 labels its series ``hostname`` (lowercase); ``Hostname`` does not exist, so the ``gpu_node`` variable and the
  NVLink panel returned 0 series ({Hostname=~".+"} 0 series vs {hostname=~".+"} 5 series).
* ``sglang:prefetch_bandwidth_*`` is not exported on the nixl backend: the metric is declared in SGLang 0.5.18
  (``observability/metrics_collector.py:1907``, ``StorageMetricsCollector``) but only observed by storage backends that report
  ``StorageMetrics``; the real run with L3 nixl, 460K storage tokens, produced no series.  The "HiCache prefetch" panel uses
  series that exist: ``sglang:prefetched_tokens_total`` (L3->L2; incremented at ``mem_cache/hiradix_cache.py:1686``,
  ``unified_radix_cache.py:1683``).
* The node-disk panel also matches NVMe (``nvme.*``): g7e / B200 nodes expose their instance store as nvme devices when there is no md RAID.
* New panel "Cache tier occupancy (L1/L2/L3)": L1 = (kv_used + kv_evictable) / max_total_num_tokens (:343-355, :1007),
  L2 = hicache_host_used / hicache_host_total (:632-638), L3 = ``sglang:backuped_tokens_total`` (host -> storage, incremented in
  ``hiradix_cache.py:669`` / ``unified_radix_cache.py:1900`` through ``StorageMetricsCollector.log_backuped_tokens``,
  ``metrics_collector.py:1924-1926``).  ``sglang:hicache_backup_bytes_total`` is NOT L3: it counts GPU -> host (L2) bytes
  (``metrics_collector.py:2094-2102``; ``hiradix_cache.py:1077``, ``unified_radix_cache.py:2176``).
  Direction table (sglang-runtime 1.5.1 image, SGLang 0.5.18): hicache_backup_bytes_total / hicache_backup_tokens_total =
  GPU->host; backuped_tokens_total = host->storage; prefetched_tokens_total = storage->host; load_back_tokens_total /
  load_back_bytes_total = host->GPU.
* ``sglang.json`` had no namespace filter and mixed every DGD in the cluster: variable ``namespace`` + scoped selectors.
"""
from __future__ import annotations

import re

import pytest
import yaml

import _lib as L

MATRIX = L.DASH / "demo" / "matrix.json"
CM = L.DASH / "demo" / "matrix-configmap.yaml"
SGLANG = L.DASH / "sglang" / "sglang.json"
OCC = "Cache tier occupancy (L1/L2/L3)"


@pytest.fixture(scope="module")
def matrix():
    return L.load_json(MATRIX)


@pytest.fixture(scope="module")
def sglang():
    return L.load_json(SGLANG)


def _vars(d):
    return {v["name"]: v for v in d["templating"]["list"]}


def _query(v) -> str:
    q = v.get("query")
    return q["query"] if isinstance(q, dict) else str(q)


# ----------------------------------------------------------------------------- hostname
@pytest.mark.parametrize("path", [MATRIX, CM], ids=["matrix.json", "matrix-configmap.yaml"])
def test_no_capitalised_hostname_label(path):
    assert "Hostname" not in path.read_text(), "dcgm-exporter emits the label `hostname`, not `Hostname`"


def test_gpu_node_variable_reads_the_hostname_label(matrix):
    q = _query(_vars(matrix)["gpu_node"])
    assert re.search(r"label_values\(\s*DCGM_FI_DEV_GPU_UTIL\s*,\s*hostname\s*\)", q), q


def test_nvlink_panel_filters_and_groups_by_hostname(matrix):
    exprs = L.exprs(L.panels_by_title(matrix)["NVLink throughput (DCGM)"])
    assert exprs
    for e in exprs:
        assert re.search(r'hostname\s*=~\s*"\$gpu_node"', e), e
        assert re.search(r"by \([^)]*\bhostname\b", e), e
    legends = [t.get("legendFormat", "") for t in L.panels_by_title(matrix)["NVLink throughput (DCGM)"]["targets"]]
    assert all("{{hostname}}" in lg and "Hostname" not in lg for lg in legends), legends


# ----------------------------------------------------------------------------- prefetch panel
@pytest.mark.parametrize("path", [MATRIX, CM], ids=["matrix.json", "matrix-configmap.yaml"])
def test_prefetch_bandwidth_is_gone(path):
    assert "prefetch_bandwidth" not in path.read_text()


def test_prefetch_panel_uses_series_that_exist(matrix):
    exprs = L.exprs(L.panels_by_title(matrix)["HiCache prefetch"])
    text = "\n".join(exprs)
    assert "sglang:prefetched_tokens_total" in text
    names = {m for e in exprs for m in L.metric_names(e)}
    # prefetch = storage->host (L3->L2); only series of the L3 path belong here, never the GPU->host backup counters
    assert names <= {"sglang:prefetched_tokens_total", "sglang:backuped_tokens_total"}, names


def test_allowlist_knows_the_real_names():
    allowed = L.allowlist()
    for name in ("sglang:prefetched_tokens_total", "sglang:backuped_tokens_total", "sglang:hicache_backup_bytes_total",
                 "sglang:hicache_backup_tokens_total", "sglang:load_back_bytes_total", "sglang:hicache_host_used_tokens",
                 "sglang:hicache_host_total_tokens", "sglang:kv_used_tokens", "sglang:kv_evictable_tokens", "sglang:kv_available_tokens",
                 "sglang:max_total_num_tokens"):
        assert L.is_known(name, allowed), name


# ----------------------------------------------------------------------------- node disks
def _device_regex(expr: str) -> str:
    m = re.search(r'device\s*=~\s*"([^"]+)"', expr)
    assert m, expr
    return m.group(1)


def test_node_disk_panel_matches_md_and_nvme_devices(matrix):
    exprs = L.exprs(L.panels_by_title(matrix)["Node disk reads (md*)"])
    assert exprs
    for e in exprs:
        rx = _device_regex(e)
        for dev in ("md0", "md127", "nvme0n1", "nvme2n1"):
            assert re.fullmatch(rx, dev), f"device regex {rx!r} does not match {dev}"
        assert not re.fullmatch(rx, "sda1") and not re.fullmatch(rx, "dm-0"), "must not match arbitrary disks"


def test_instance_variable_sees_nvme_only_nodes(matrix):
    q = _query(_vars(matrix)["md_instance"])
    rx = _device_regex(q)
    assert re.fullmatch(rx, "md0") and re.fullmatch(rx, "nvme1n1"), q


# ----------------------------------------------------------------------------- occupancy panel
def _targets_by_tier(panel):
    out = {}
    for t in panel["targets"]:
        legend = t.get("legendFormat", "")
        for tier in ("L1", "L2", "L3"):
            if legend.startswith(tier) or f" {tier}" in legend or f"{tier} " in legend:
                out[tier] = t["expr"]
    return out


def test_occupancy_panel_has_one_series_per_tier(matrix):
    panels = L.panels_by_title(matrix)
    assert OCC in panels, f"panel {OCC!r} missing"
    tiers = _targets_by_tier(panels[OCC])
    assert set(tiers) == {"L1", "L2", "L3"}, tiers
    l1, l2, l3 = tiers["L1"], tiers["L2"], tiers["L3"]
    for name in ("sglang:kv_used_tokens", "sglang:kv_evictable_tokens", "sglang:max_total_num_tokens"):
        assert name in l1, (name, l1)
    assert re.search(r"\)\s*/\s*[^)]*sglang:max_total_num_tokens|/\s*sum[^/]*sglang:max_total_num_tokens", l1) or "/" in l1
    assert "sglang:hicache_host_used_tokens" in l2 and "sglang:hicache_host_total_tokens" in l2 and "/" in l2
    assert "sglang:backuped_tokens_total" in l3, "L3 = host->storage tokens (backuped_tokens_total)"
    for l2_only in ("hicache_backup_bytes_total", "hicache_backup_tokens_total", "load_back_"):
        assert l2_only not in l3, f"{l2_only} is an L2 (GPU<->host) series, not L3"


def test_occupancy_panel_is_scoped_and_on_the_datasource_variable(matrix):
    p = L.panels_by_title(matrix)[OCC]
    assert p["datasource"]["uid"] == "${datasource}"
    for t in p["targets"]:
        assert t["datasource"]["uid"] == "${datasource}"
        for name, matcher in L.metric_selectors(t["expr"]):
            assert matcher and L.NS_RE.search(matcher), (name, t["expr"])


def test_configmap_carries_every_change():
    cm = yaml.safe_load(CM.read_text())
    assert cm["data"]["matrix.json"] == MATRIX.read_text() or yaml.safe_load(cm["data"]["matrix.json"]) == L.load_json(MATRIX)
    assert OCC in cm["data"]["matrix.json"]


# ----------------------------------------------------------------------------- sglang.json namespace
def test_sglang_dashboard_has_a_namespace_variable(sglang):
    v = _vars(sglang)
    assert "namespace" in v, sorted(v)
    assert v["namespace"]["type"] == "query" and "label_values" in _query(v["namespace"])
    assert v["namespace"]["datasource"]["uid"] in ("${datasource}", "$datasource")


def test_every_sglang_dashboard_selector_is_scoped_by_namespace(sglang):
    bad = []
    for p in L.iter_panels(sglang):
        for e in L.exprs(p):
            for name, matcher in L.metric_selectors(e):
                if not (matcher and L.NS_RE.search(matcher)):
                    bad.append((p.get("title"), name))
    assert not bad, bad[:10]
