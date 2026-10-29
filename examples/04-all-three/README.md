# 04-all-three: PD + HiCache + KV router (cell S7)

## What it does
All three techniques at once: the prefill/decode split of `03-pd-disaggregation`, the HiCache of
`02-hicache` on the prefill workers only (prefill is where prompt KV is computed and reused), and the
KV router of `01-kv-router` on the Frontend.

## What it changes
The union of the three diffs, in namespace `s-dis-hic` (`manifests/track-q-qwen38/overlays/t-dis-hic`
plus `manifests/track-q-qwen38/components/router-kv`):
- Frontend: `DYN_ROUTER_MODE=kv`;
- prefill: `--disaggregation-mode prefill`, `--disaggregation-transfer-backend nixl`, the EFA env, plus
  `--enable-hierarchical-cache` and the other HiCache flags with `--hicache-size 400` (GB of host RAM per
  engine), the `SGLANG_HICACHE_NIXL_BACKEND_STORAGE_DIR` env and the `hicache-l3` volume;
- decode: `--disaggregation-mode decode` and the same EFA settings as `03-pd-disaggregation`, no HiCache.

## How to apply
```
kubectl apply -k examples/04-all-three
```
The GPU node must advertise `vpc.amazonaws.com/efa`. A plain apply does not empty the L3 directory; the
matrix driver (`tools/matrix_driver`) empties it when it tears the topology down.

## Where to look
- `diff-vs-baseline.diff`, per component: Frontend, then baseline `Worker` vs `prefill` and vs `decode`.
  A change decode shares with prefill is shown once, in prefill (a `# ... omitted` note in decode); the GPU-pin
  code is collapsed.
- `dashboards/demo/compare.json`: Baseline `s-agg`, Optimized `s-dis-hic`; every row applies.
- `dashboards/demo/take.json`: the "HiCache tiers" and "PD workers" rows.
- `dashboards/sglang/sglang.json`: the "Dynamo KV Router", "P/D Disagg Queues" and HiCache rows.
