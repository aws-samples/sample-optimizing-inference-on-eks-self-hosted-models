# 06-kv-router-pd: KV router + PD disaggregation (cell S5)

## What it does
The prefill/decode split of `03-pd-disaggregation` (cell S3) with the KV router of `01-kv-router` (cell S1) on the
Frontend. Four prefill and four decode workers, one GPU each, move the KV cache from prefill to decode over NIXL
with the LIBFABRIC backend on EFA. The router makes the KV-aware pick among the prefill workers; the decode hop
is routed on load only. There is no HiCache: a prefix that left GPU memory is recomputed.

## What it changes
In namespace `s-dis` (`manifests/track-q-qwen38/overlays/t-dis` plus `manifests/track-q-qwen38/components/router-kv`):
- Frontend: `DYN_ROUTER_MODE` goes from `round-robin` to `kv`, the only difference from `03-pd-disaggregation` (S3);
- `Worker` (`replicas: 8`) becomes `prefill` and `decode` (`replicas: 4` each) with `--disaggregation-mode`,
  `--disaggregation-transfer-backend nixl`, `--disaggregation-bootstrap-port`, the EFA env, one
  `vpc.amazonaws.com/efa` device per pod, `privileged: true` and the GPU-pin preamble, as in S3;
- compared with `04-all-three` (S7), the prefill workers run without HiCache and the namespace is `s-dis`.

## How to apply
```
kubectl apply -k examples/06-kv-router-pd
```
It shares the namespace `s-dis` and the DGD name `q38` with `03-pd-disaggregation`, so it updates a running S3 in
place (only the Frontend changes). Coming from any other folder, delete that folder first. The GPU node must
advertise `vpc.amazonaws.com/efa` (EFA device plugin).

## Where to look
- `diff-vs-baseline.diff`, per component: Frontend, then baseline `Worker` vs `prefill` and vs `decode`. A change
  decode shares with prefill is shown once, in prefill (a `# ... omitted` note in decode); the GPU-pin code is collapsed.
- `dashboards/demo/compare.json`: Baseline `s-agg`, Optimized `s-dis`; the "Latency over time" row.
- `dashboards/sglang/sglang.json`: the "Dynamo KV Router" and "P/D Disagg Queues" rows.
