# Dashboards test contract (RED until `dashboards/` exists)

Run from the repository root: `uv run --with-requirements tests/requirements-test.txt pytest tests/dashboards -v`
Needs the Dynamo checkout at `../dynamo` (override with `DYNAMO_REPO`) to read tag `v1.5.1` via `git show`.

## Required layout
- `dashboards/dynamo/grafana-dynamo-dashboard-configmap.yaml`, `grafana-disagg-dashboard-configmap.yaml`: copies of
  `deploy/observability/<file>` at `v1.5.1`. Body from the first `apiVersion:` line byte-identical (label line ignored);
  header comment before it must contain `Apache-2.0`, `NVIDIA`, `v1.5.1` and the upstream path; label `grafana_dashboard: "1"`, namespace `monitoring`.
- `dashboards/sglang/sglang.json`: Dynamo `dev/observability/grafana_dashboards/sglang.json` with no `cache_type` anywhere,
  evicted-tokens panel without `cache_type` (no `pool` assertion: `sglang:evicted_tokens_total` has no `pool` label in SGLang 0.5.18; see `test_sglang.py` docstring), no `sglang:cache_hit_rate`, and every `dynamo_*`/`sglang:*` metric in the allowlists
  (this means dropping/replacing `dynamo_component_gpu_cache_usage_percent`, `dynamo_component_total_blocks`, `dynamo_request_plane_roundtrip_ttft_seconds`, `dynamo_tokio_worker_busy_ratio`, or adding them to the allowlist with a primary-source citation).
- `dashboards/demo/matrix.json`: unique `uid` (not `dynamo-dashboard`), `title`, variables `datasource` (type datasource, query `prometheus`) and `namespace`
  (type query, `label_values(...)`, datasource `${datasource}`); every panel and target `datasource.uid == "${datasource}"`.
  Every metric selector carries `namespace=~"$namespace"` or `namespace="$namespace"`, except `NVLink throughput (DCGM)` and `Node disk reads (md*)` (pod / node labels).
- `dashboards/demo/matrix-configmap.yaml`: ConfigMap `grafana-demo-matrix-dashboard`, namespace `monitoring`, label `grafana_dashboard: "1"`, `data["matrix.json"]` == matrix.json.

## Exact panel titles (spec section 11)
Frontend TTFT p50/p95; Engine hit rate per worker; Router predicted hit; KV events applied; Load spread: running/queued requests;
Load spread: active decode blocks / prefill tokens; ITL; NIXL bytes transferred; NIXL failed transfers; NVLink throughput (DCGM);
Tokens by cache source; HiCache host occupancy; HiCache load-back; HiCache prefetch; Node disk reads (md*).

## Forbidden
`sglang:cache_hit_rate`, `dynamo_frontend_worker_active_requests`, bare `dynamo_component_router_kv_hit_rate` (only `_sum`/`_count`/`_bucket`),
`rate()`/`increase()` or range selectors on `DCGM_FI_PROF_NVLINK_*`, `cache_source` aggregation without `!="total"`.

## Metric allowlist
`allowlist_dynamo.txt` = Dynamo v1.5.1 metrics-catalog.mdx (verified against the tag by a test). `allowlist_sglang.txt` = spec section 11/5.3 names plus names
in the Dynamo dev sglang.json (provenance in file header). Unknown names fail; extend an allowlist only with a primary-source citation.

## Metric extraction (`_lib.metric_selectors`)
Label names are not metrics: identifiers inside `by/without/on/ignoring/group_left/group_right(...)` and label keys inside `{...}` (e.g. `dynamo_component_type`, `dynamo_graph_deployment`, added by the Dynamo v1.5.1 operator PodMonitors) are skipped; `${var}` inside a matcher is handled. Tests: `test_extractor_*` in `test_matrix.py` (label accepted; unknown `dynamo_*` metric still flagged by scope and allowlist).

## Open questions
- Does `sglang:cached_tokens_total` also emit a `cache_source="total"` series next to the per-tier ones? If so the engine hit-rate numerator double counts. Being verified in the real image (FT-8); not encoded in tests yet.
- Dashboard `sglang.json` still uses `sum by (pool)` on evicted tokens (harmless, but the `{{pool}}` legend is blank); suggest dropping the grouping.

## Track C fixes (`test_trackc_fixes.py`, REPORT.md item 4)
- dcgm-exporter labels its series `hostname` (lowercase): no `Hostname` in `matrix.json`/configmap; `gpu_node` is `label_values(DCGM_FI_DEV_GPU_UTIL, hostname)`; the NVLink panel filters, groups and legends by `hostname`.
- `sglang:prefetch_bandwidth_*` is removed from the "HiCache prefetch" panel. It is declared in SGLang 0.5.18 (`observability/metrics_collector.py:1907`, `StorageMetricsCollector`) but only observed by storage backends that report `StorageMetrics`; the nixl backend produced no series in the real run (the allowlist keeps the name, dashboards must not use it). The panel uses `sglang:prefetched_tokens_total` (storage->host) and may add `sglang:backuped_tokens_total` (host->storage); the GPU<->host counters do not belong in it.
- "Node disk reads (md*)" and the `md_instance` variable match `md.*|nvme.*` (instance-store nodes without a RAID expose nvme devices); the device regex must not match arbitrary disks.
- New panel "Cache tier occupancy (L1/L2/L3)": L1 = (`kv_used_tokens` + `kv_evictable_tokens`) / `max_total_num_tokens`, L2 = `hicache_host_used_tokens` / `hicache_host_total_tokens`, L3 = `sglang:backuped_tokens_total` (host->storage; `hicache_backup_bytes_total` is GPU->host = L2, NOT L3); legends start with `L1`/`L2`/`L3`.
- `sglang.json` has a `namespace` variable (query, `label_values`) and every `sglang:`/`dynamo_` selector carries `namespace=~"$namespace"` (it mixed every DGD in the cluster).
- Direction table (image sglang-runtime 1.5.1, SGLang 0.5.18): `hicache_backup_bytes_total`, `hicache_backup_tokens_total` GPU->host (`metrics_collector.py:2094/2114`, incremented `hiradix_cache.py:1073-1077`, `unified_radix_cache.py:2172-2176`); `backuped_tokens_total` host->storage (`:1868`, `hiradix_cache.py:669`, `unified_radix_cache.py:1900`); `prefetched_tokens_total` storage->host (`:1862`, `hiradix_cache.py:1686`, `unified_radix_cache.py:1683`); `load_back_tokens_total`, `load_back_bytes_total` host->GPU (`:2077/2104`, `hiradix_cache.py:1107-1111`, `unified_radix_cache.py:2217-2221`). The storage counters (`backuped_`, `prefetched_`) are logged only when storage metrics are enabled (`enable_storage_metrics`).
- Changed existing tests: `test_matrix.py` (HiCache prefetch no longer requires `prefetch_bandwidth`; the disk regex accepts `md.*|nvme.*`; node panels filter on lowercase `hostname`), `allowlist_sglang.txt` (+`hicache_backup_bytes_total`, provenance).

## Demo take view (`test_take.py`, RED until implemented)
`demo/take.json` (uid `demo-take`) + `take-configmap.yaml` (`grafana-demo-take-dashboard`, label `grafana_dashboard`): the hall-screen view of ONE take, one single-value `namespace` variable (the URL sets `var-namespace`), refresh 5 s, last 10 min. Hero `stat` panels over `$__range` (valueSize >= 64): `turns with TTFT > 10 s` (= count - `increase(..._bucket{le="10.0"})`) and `TTFT p95 since replay start` (= `histogram_quantile(0.95, sum by (le) (increase(..._bucket[$__range])))`, never a `rate[1m]` p95); `% of turns with TTFT < 1 s` is a smaller SECONDARY stat; `Requests waiting for first token` (= `sum(dynamo_frontend_stage_requests{stage=~"preprocess|route|dispatch"})`; in-flight/queued are deprecated and a mixed signal); `TTFT p50` on a log axis fixed 0.05-200 s; `KV cache hit rate` (router predicted + engine actual); `Output tokens/s`; collapsed rows `HiCache tiers` (L1/L2/L3 occupancy) and `PD workers` (running/queued `by (engine_type)`); one text footer `demo recording · 1x p6-b200 · not the benchmark (see slides)`.
Verified live (Prometheus, read-only): the TTFT histogram has `le` boundaries 0.0 0.0022 0.0047 0.01 0.022 0.047 0.1 0.22 0.47 **1.0** 2.2 4.7 **10.0** 22.0 48.0 100.0 220.0 480.0 +Inf (label VALUES are the strings "1.0" and "10.0"; `le="1"` matches nothing); SGLang series carry `engine_type` in {prefill, decode, unified} (also for in-pod PD, where `dynamo_component_type` is `worker`). The highest finite TTFT bucket is 480 s (above the 173 s slowest turn of the recorded S0 storm), so `histogram_quantile` does not cap the storm and the p95 title has no "capped" suffix; buckets are coarse above 100 s (100, 220, 480). `dynamo_frontend_stage_requests` (labels `stage` in preprocess|route|dispatch, `phase`) is live. Every name used was already in the allowlists; none was added.

## Known spec conflict
spec 8.3 says "fix the vendored disagg panel" (NVLink `rate()`), but vendored files must be byte-identical to v1.5.1; the fix is covered only in `matrix.json`.
