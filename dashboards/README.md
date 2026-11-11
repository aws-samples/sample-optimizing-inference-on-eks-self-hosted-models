# Dashboards

Grafana dashboards for the sample. The panels illustrate what is happening.

| File | What it shows | Origin |
|---|---|---|
| `dynamo/grafana-dynamo-dashboard-configmap.yaml` | Dynamo frontend and worker overview (requests, TTFT/ITL, KV router) | Vendored, unmodified |
| `dynamo/grafana-disagg-dashboard-configmap.yaml` | Disaggregated serving: prefill vs decode latency and throughput, GPU, NIXL/NVLink | Vendored, unmodified |
| `sglang/sglang.json` | SGLang engine detail: batching, KV pools, speculative decoding, PD queues, HiCache tiers | Modified from Dynamo |
| `demo/matrix.json` (+ `matrix-configmap.yaml`) | One row per technique, R (KV routing), D (PD disaggregation), O (HiCache), plus latency | New, for this sample |
| `dcgm/dcgm-grafana-dashboard.json` | GPU telemetry from DCGM Exporter: utilisation, memory, power, temperature and clocks | Modified from NVIDIA/dcgm-exporter (Apache-2.0) |

**Provenance of `dcgm/dcgm-grafana-dashboard.json`.** Copied from [NVIDIA/dcgm-exporter](https://github.com/NVIDIA/dcgm-exporter) (`grafana/dcgm-exporter-dashboard.json`, Apache-2.0), modified: the `__inputs` import block was removed, its 10 `${DS_PROMETHEUS}` datasource references were replaced by the datasource uid `prometheus`, and the trailing newline was dropped; panels and queries are unchanged. Full attribution is in [../THIRD-PARTY-LICENSES.md](../THIRD-PARTY-LICENSES.md) and [../NOTICE](../NOTICE).

## `demo/matrix.json`

Every rate window is `$__rate_interval`.

- **Latency:** Frontend TTFT p50/p95 and engine ITL.
- **R:**
  - Engine hit rate per worker, from `rate(sglang:cached_tokens_total) / rate(sglang:prompt_tokens_total)`. The last-batch gauge is never used.
  - Router predicted hit, from the `_sum`/`_count` of the router's hit-rate histogram.
  - KV events applied.
  - Load spread, two panels: engine running/queued requests, and the router's active decode blocks / prefill tokens.
- **D:** NIXL bytes and failed transfers, and NVLink throughput (DCGM). The DCGM PROF NVLink fields are bytes/s gauges, so there is no `rate()`. dcgm-exporter labels its series `hostname` (lowercase).
- **O:**
  - Tokens by cache source, stacked over the computed (miss) tokens. SGLang 0.5.21 sets `cache_source` to `device`, `host` or `storage`, and to `total` only for a request without a per-tier breakdown ([metrics_collector.py L1803-L1821](https://github.com/sgl-project/sglang/blob/v0.5.21/python/sglang/srt/observability/metrics_collector.py#L1803-L1821)). The panel drops `total` with `cache_source!="total"`.
  - HiCache host occupancy and load-back.
  - HiCache prefetch: L3 to L2 prefetched tokens and L2 to L3 backed-up tokens. `sglang:prefetch_bandwidth` is not used, because only storage backends that report `StorageMetrics` observe it, and the nixl backend produced no series.
  - Cache tier occupancy (L1/L2/L3), per worker pod:
    - L1 = `(kv_used + kv_evictable) / max_total_num_tokens`;
    - L2 = host `used / total`;
    - L3 = `sglang:backuped_tokens_total`, the tokens written to L3 since the worker started. This is an upper bound: SGLang 0.5.21 has no L3 occupancy gauge.
  - Node disk reads, per device, on the `md*` RAID0 or, on nodes without a RAID, the whole `nvme` disks. On Nitro the EBS root volume is an nvme device too.

Variables:

| Variable | Scopes |
|---|---|
| `datasource` | Every panel. |
| `namespace` | Every Dynamo, SGLang and NIXL selector. There is one DGD per namespace: `s-agg`, `s-agg-hic`, `s-dis`, `s-dis-hic`. |
| `gpu_node` | The DCGM panel, by `hostname`. |
| `md_instance` | The node-exporter panel, by `instance` of nodes with `md*` or `nvme` disks. |

Node metrics carry no namespace, which is why the last two panels have their own variables.

There are hidden queries you can enable in the panel editor:
- per-`dp_rank` hit rate, if the engine exports the `dp_rank` label;
- the `DCGM_FI_DEV_NVLINK_BANDWIDTH_TOTAL` fallback.

## Loading

The kube-prometheus-stack Grafana sidecar imports every ConfigMap labelled `grafana_dashboard: "1"` in the `monitoring` namespace:

```bash
kubectl apply --server-side -f dashboards/dynamo/ -f dashboards/demo/matrix-configmap.yaml
kubectl create configmap grafana-sglang-dashboard -n monitoring \
  --from-file=sglang.json=dashboards/sglang/sglang.json --dry-run=client -o yaml \
  | kubectl label --local -f - grafana_dashboard=1 -o yaml | kubectl apply --server-side -f -
```

`matrix.json` is the source of truth. After editing it, regenerate the ConfigMap. `tests/dashboards` checks that the two match:

```bash
cd dashboards/demo && {
  printf '# Generated from dashboards/demo/matrix.json (single source of truth); do not edit by hand.\n# Regenerate: see dashboards/README.md.\n'
  kubectl create configmap grafana-demo-matrix-dashboard -n monitoring --from-file=matrix.json=matrix.json \
    --dry-run=client -o yaml | kubectl label --local -f - grafana_dashboard=1 -o yaml
} > matrix-configmap.yaml
```

## Provenance and licence

The files under `dynamo/` and `sglang/` come from [NVIDIA Dynamo](https://github.com/ai-dynamo/dynamo), tag `v1.6.1` (commit `0d9c24a8bf6db13c1a1223cf15fcbee9140d1ac3`). They are © NVIDIA CORPORATION & AFFILIATES, licensed [Apache-2.0](http://www.apache.org/licenses/LICENSE-2.0), and must be listed in the repository NOTICE.

- **`dynamo/*.yaml`** are copies of `deploy/observability/<file>`.
  - Everything from `apiVersion:` on is byte-identical. Only the provenance comment header was added.
  - Known upstream issue, kept as-is: the disagg "NVLink Bandwidth (GB/s)" panel applies `rate()` to bytes/s gauges. Use the matrix NVLink panel instead.
- **`sglang/sglang.json`** is `dev/observability/grafana_dashboards/sglang.json`, modified as follows (also stated in its `description`):
  - HiCache load-back is grouped by `pool`, the SGLang 0.5.18 label. In 0.5.18 only `load_back_tokens_total` carries `pool`, so evicted tokens and the latency histograms are plotted as single series.
  - Both engine hit-rate panels are derived from `cached_tokens_total / prompt_tokens_total`.
  - "Total KV Blocks" now uses `dynamo_frontend_model_total_kv_blocks`.
  - Three panels on metrics absent from the v1.6.1 metrics catalog were removed: GPU KV cache usage, Tokio busy ratio and request-plane RTT.
  - A `namespace` variable was added, and every Dynamo and SGLang selector is scoped by it. Upstream mixed every DGD in the cluster.
  - Panels fed by forward-pass metrics are empty by design, because the recipe sets `DYN_FORWARDPASS_METRIC_PORT=""`.
- **`demo/`** is original to this sample and is covered by the repository licence.
