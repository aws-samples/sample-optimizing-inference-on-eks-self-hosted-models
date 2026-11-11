# 00-baseline: aggregated, round-robin, no HiCache (cell S0)

## What it does
This is the baseline, cell S0, and every other folder is diffed against its `manifest.yaml`.
Eight aggregated SGLang workers (one GPU each, TP1) serve Qwen/Qwen3.8-27B-FP8 behind one Dynamo Frontend.
The Frontend spreads requests round-robin, so it ignores which worker already holds a prompt's KV cache.

## What it changes
Nothing: it is the reference with every technique off.
- KV router off: the Frontend runs with `DYN_ROUTER_MODE=round-robin`.
- HiCache off: the KV cache lives in GPU memory only (no host RAM or storage tier).
- No prefill/decode split: one `Worker` component with `replicas: 8`.

## How to apply
```
kubectl --context "$KUBE_CONTEXT" apply --server-side -f examples/00-baseline/manifest.yaml
```
The DGD `q38` lands in namespace `s-agg`. The manifests do not create namespaces; the platform does.

## Where to look
- `dashboards/demo/matrix.json` with `namespace` = `s-agg`: the "Latency (all scenarios)" row and
  "Engine hit rate per worker" are the reference curves for the other folders.
