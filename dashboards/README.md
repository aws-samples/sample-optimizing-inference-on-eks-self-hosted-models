# Dashboards

Grafana dashboards for the sample. The panels illustrate what is happening. The `demo-bench` tables are the numbers of record (spec §11).

| File | What it shows | Origin |
|---|---|---|
| `dynamo/grafana-dynamo-dashboard-configmap.yaml` | Dynamo frontend and worker overview (requests, TTFT/ITL, KV router) | Vendored, unmodified |
| `dynamo/grafana-disagg-dashboard-configmap.yaml` | Disaggregated serving: prefill vs decode latency and throughput, GPU, NIXL/NVLink | Vendored, unmodified |
| `sglang/sglang.json` | SGLang engine detail: batching, KV pools, speculative decoding, PD queues, HiCache tiers | Modified from Dynamo |
| `demo/matrix.json` (+ `matrix-configmap.yaml`) | The recorded demo board: one row per technique, R (KV routing), D (PD disaggregation), O (HiCache), plus latency | New, for this sample |
| `demo/compare.json` (+ `compare-configmap.yaml`) | Baseline vs optimized, side by side: one scenario namespace per arm | New, for this sample |
| `dcgm/dcgm-grafana-dashboard.json` | GPU telemetry from DCGM Exporter: utilisation, memory, power, temperature and clocks | Modified from NVIDIA/dcgm-exporter (Apache-2.0) |

**Provenance of `dcgm/dcgm-grafana-dashboard.json`.** Copied from [NVIDIA/dcgm-exporter](https://github.com/NVIDIA/dcgm-exporter) (`grafana/dcgm-exporter-dashboard.json`, Apache-2.0), modified: the `__inputs` import block was removed, its 10 `${DS_PROMETHEUS}` datasource references were replaced by the datasource uid `prometheus`, and the trailing newline was dropped; panels and queries are unchanged. Full attribution is in [../THIRD-PARTY-LICENSES.md](../THIRD-PARTY-LICENSES.md) and [../NOTICE](../NOTICE).

## `demo/matrix.json`

Every panel implements a spec §11 query. The `[30s]` windows in the spec are written as `$__rate_interval` here.

- **Latency:** Frontend TTFT p50/p95 and engine ITL.
- **R:**
  - Engine hit rate per worker, from `rate(sglang:cached_tokens_total) / rate(sglang:prompt_tokens_total)`. The last-batch gauge is never used.
  - Router predicted hit, from the `_sum`/`_count` of the router's hit-rate histogram.
  - KV events applied.
  - Load spread, two panels: engine running/queued requests, and the router's active decode blocks / prefill tokens.
- **D:** NIXL bytes and failed transfers, and NVLink throughput (DCGM). The DCGM PROF NVLink fields are bytes/s gauges, so there is no `rate()`. dcgm-exporter labels its series `hostname` (lowercase).
- **O:**
  - Tokens by cache source (`cache_source!="total"`), stacked over the computed (miss) tokens.
  - HiCache host occupancy and load-back.
  - HiCache prefetch: L3 to L2 prefetched tokens and L2 to L3 backed-up tokens. `sglang:prefetch_bandwidth` is not used, because only storage backends that report `StorageMetrics` observe it, and the nixl backend produced no series.
  - Cache tier occupancy (L1/L2/L3), per worker pod:
    - L1 = `(kv_used + kv_evictable) / max_total_num_tokens`;
    - L2 = host `used / total`;
    - L3 = `sglang:backuped_tokens_total`, the tokens written to L3 since the worker started. This is an upper bound: SGLang 0.5.18 has no L3 occupancy gauge.
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
- per-`dp_rank` hit rate, until T-OBS-1 confirms the label;
- the `DCGM_FI_DEV_NVLINK_BANDWIDTH_TOTAL` fallback.

On a Dynamo **mocker** DGD, only the frontend and router panels have data: TTFT, router predicted hit, KV events applied, and active decode blocks / prefill tokens. The router panels fill only in the `kv` arm. The panels that need `sglang:*`, NIXL, DCGM or local NVMe stay empty, including Cache tier occupancy.

## `demo/compare.json`

Two arms on one board. Each arm is one scenario namespace, so the board compares topologies:

| Variable | Label | Scopes |
|---|---|---|
| `datasource` | Data source | Every panel. |
| `namespace_a` | Baseline | Every baseline query, with `namespace="$namespace_a"`. Single value. |
| `namespace_b` | Optimized | Every optimized query, with `namespace="$namespace_b"`. Single value. |

Every panel has one query per arm. Baseline is grey or dashed, optimized is green or solid.

- **Headline:** TTFT p50 and p95, engine hit rate and output tokens/s over the selected range (`$__range`). The TTFT quantiles use `increase()` of the frontend histogram over the range. The hit rate is `increase(sglang:cached_tokens_total) / increase(sglang:prompt_tokens_total)`.
- **Latency over time:** TTFT p50 and p95 at the frontend, and engine ITL p95.
- **Load and cache reuse:** in-flight requests, engine hit rate and output tokens/s, per arm.
- **HiCache tiers:** the same queries as the matrix O row, summed per arm instead of per pod: tokens by cache source (stacked per arm, `cache_source!="total"`), host occupancy, load-back, prefetch, and cache tier occupancy (L1/L2/L3). An arm without HiCache has no L2/L3 series.
- **Annotation:** "Concurrency level" from `matrix.json`, scoped to `namespace=~"$namespace_a|$namespace_b"`.

The two runs usually happened at different times. Set the time range to cover both. The headline stats count everything in the range, including warm-ups and other reps, so narrow the range to the runs you compare.

The arms are namespaces, so two scenarios that share a namespace cannot be split here. An example is the R toggle, S0 vs S1, both on `s-agg`. Use the `demo-bench` tables for those pairs.

## Loading

The kube-prometheus-stack Grafana sidecar imports every ConfigMap labelled `grafana_dashboard: "1"` in the `monitoring` namespace:

```bash
kubectl apply --server-side -f dashboards/dynamo/ -f dashboards/demo/matrix-configmap.yaml \
  -f dashboards/demo/compare-configmap.yaml
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

`compare-configmap.yaml` is generated the same way from `compare.json`: ConfigMap `grafana-demo-compare-dashboard`, key `compare.json`.

## Re-render a demo take from Prometheus history

Why: a live capture of a take can record "No data". The capture opened the `demo-take` dashboard about 240 s before the replay, with an absolute time range in the future. The namespace variable (`namespace`) then depended on the time range, so it only offered the other scenario's namespace and Grafana replaced `var-namespace` with it. `demo/take.json` now uses a fixed custom list (`s-agg,s-agg-hic,s-dis,s-dis-hic`) that does not depend on the range, but the takes recorded before the fix stay affected. The series themselves are intact in Prometheus, so the take view can be rebuilt from history instead of being recorded live.

What it does (`manifests/demo/rerender/rerender.py`, CPU only, in-cluster). One Job `bench/demo-rerender-<source take id>` per take, with these containers in order:

1. `check` lists the keys already under `takes/<re-render run id>/<source take id>-rerender/` (the upload is append-only).
2. `tools` confirms the encoder and `ffprobe` exist, so the image is pulled before the long render.
3. `render` steps the `demo-take` dashboard range through the take: for t = 0, S, 2S, ... up to the replay span plus the drain, it opens `/d/demo-take?orgId=1&from=<replay start>&to=<replay start + t>&kiosk&var-namespace=<ns>` with the Grafana credentials from the existing Secret (never printed). A frame is accepted only if the namespace picker shows `<ns>` and, from 60 s on, no expected panel says "No data". A bad frame is retried up to 3 times; a frame that stays bad fails the Job, so a bad frame never reaches the video.
4. `encode` turns the frames into a real-time webm, one frame every S seconds shown for S seconds, so the video lasts as long as the take.
5. `meta` writes `<source take id>-rerender-capture.json` (video start = replay start, which the compose script reads) and a take record with `result_of_record: false`.
6. `upload` copies everything append-only to `s3://$RESULTS_BUCKET/takes/<re-render run id>/<source take id>-rerender/`, the record last.

A compose Job (`bench/q38-take-compose-<re-render run id>`: fetch, render, upload) then builds the side-by-side clips from the two re-rendered webms. Baseline (S0) is on the left, candidate (S7) on the right. Outputs go under `takes/<re-render run id>/compose/`.

How to run:

Apply the compose script ConfigMap once, then build and create the Jobs (`RESULTS_BUCKET`, `AWS_REGION` and `BENCH_REPO` come from the environment or `--platform-env`):

```bash
kubectl kustomize manifests/demo/rerender | kubectl apply -f -

python3 manifests/demo/rerender/rerender.py jobs \
  --source-compose compose.json --out-dir jobs \
  --platform-env manifests/config/platform.env [--drain-s 0]

kubectl create -f jobs/demo-rerender-<source take id>.json

kubectl create -f jobs/q38-take-compose-<re-render run id>.json

python3 manifests/demo/rerender/rerender.py compose-record \
  --spec jobs/compose-spec.json --source-compose-uri <s3 uri of the live compose.json> --out compose.json
```

Create one re-render Job per take (both run in parallel), and the compose Job only when both are Complete.

- Input: `compose.json` of the live takes (cell, take id, `webm_uri`, `replay_start_ms`, `replay_end_ms`, `sync_ms`). `jobs` writes nothing if a setting is missing.
- `--drain-s` is the time rendered after the replay end (default 60). The frame step S (4 to 10 s) is the smallest one that fits the render budget (`--budget-s`, default 1700 s, at `--per-frame-s`, default 4 s per frame); both takes use the larger of the two.
- `compose-record` writes the `compose.json` marker once the compose Job is complete.
- `RERENDER_RUN_ID` (form `q38-<name>-rerender`) and `RERENDER_NOTE` select another re-render set than the default `q38-e7-rerender`.
- Outputs: per take `<source take id>-rerender.webm`, `-rerender-capture.json` and `-rerender-take.json` in the take's own prefix, and the three compose clips under `compose/`.

Limits:

- A re-render is NOT A RESULT OF RECORD. It shows the recorded Prometheus series of the take, not a live recording, and every record and the compose note say so. Slide numbers come from the matrix tables.
- It needs the series still in Prometheus retention, the `demo-take` dashboard and the `demo-grafana-auth` Secret in `bench`.
- Each Job has a deadline of at most one hour, and never beyond the hard stop (`--hard-stop-epoch`, default now + 4 h).
- Existing output keys are never overwritten: to repeat a re-render, use another `RERENDER_RUN_ID`.

## Provenance and licence

The files under `dynamo/` and `sglang/` come from [NVIDIA Dynamo](https://github.com/ai-dynamo/dynamo), tag `v1.5.1` (commit `2093662a4d4e5fd318732935213dde08dc7be818`). They are © NVIDIA CORPORATION & AFFILIATES, licensed [Apache-2.0](http://www.apache.org/licenses/LICENSE-2.0), and must be listed in the repository NOTICE.

- **`dynamo/*.yaml`** are copies of `deploy/observability/<file>`.
  - Everything from `apiVersion:` on is byte-identical. Only the provenance comment header was added.
  - Known upstream issue, kept as-is: the disagg "NVLink Bandwidth (GB/s)" panel applies `rate()` to bytes/s gauges. Use the matrix NVLink panel instead.
- **`sglang/sglang.json`** is `dev/observability/grafana_dashboards/sglang.json`, modified as follows (also stated in its `description`):
  - HiCache load-back is grouped by `pool`, the SGLang 0.5.18 label. In 0.5.18 only `load_back_tokens_total` carries `pool`, so evicted tokens and the latency histograms are plotted as single series.
  - Both engine hit-rate panels are derived from `cached_tokens_total / prompt_tokens_total`.
  - "Total KV Blocks" now uses `dynamo_frontend_model_total_kv_blocks`.
  - Three panels on metrics absent from the v1.5.1 metrics catalog were removed: GPU KV cache usage, Tokio busy ratio and request-plane RTT.
  - A `namespace` variable was added, and every Dynamo and SGLang selector is scoped by it. Upstream mixed every DGD in the cluster.
  - Panels fed by forward-pass metrics are empty by design, because the recipe sets `DYN_FORWARDPASS_METRIC_PORT=""`.
- **`demo/`** is original to this sample and is covered by the repository licence.
