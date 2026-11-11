# 07-all-three: PD + HiCache + KV router (cell S7)

## What it does
All three techniques at once: the prefill/decode split of `03-pd-disaggregation`, the HiCache of
`02-hicache` on the prefill workers only (prefill is where prompt KV is computed and reused), and the
KV router of `01-kv-router` on the Frontend.

## What it changes
The union of the three diffs, in namespace `s-dis-hic`:
- Frontend: `DYN_ROUTER_MODE=kv`;
- prefill: `--disaggregation-mode prefill`, `--disaggregation-transfer-backend nixl`, the EFA env, plus
  `--enable-hierarchical-cache` and the other HiCache flags with `--hicache-size 400` (GB of host RAM per
  engine), the `SGLANG_HICACHE_NIXL_BACKEND_STORAGE_DIR` env and the `hicache-l3` volume;
- decode: `--disaggregation-mode decode` and the same EFA settings as `03-pd-disaggregation`, no HiCache.

## How to apply
```
kubectl --context "$KUBE_CONTEXT" apply --server-side -f examples/07-all-three/manifest.yaml
```
The GPU node must advertise `vpc.amazonaws.com/efa`. A plain apply does not empty the L3 directories left by earlier
runs. On a running prefill pod, `clear_hicache_storage` empties that pod's own directory
([Reset between runs](../../docs/optimizations/hicache/README.md#reset-between-runs)); deleting the directories of
deleted pods was not tested. Check the transport: every engine logs that LIBFABRIC was instantiated.
```
kubectl -n s-dis-hic logs -l nvidia.com/dynamo-component-type=prefill -c main --tail=-1 | grep -E "Backend LIBFABRIC was instantiated|KVManager initialized with backend: LIBFABRIC"
```

## Where to look
- the command of [examples/README.md](../README.md): everything this folder changes against the baseline.
- `dashboards/demo/matrix.json` with `namespace` = `s-dis-hic`: the "Latency (all scenarios)", "R: KV-aware
  routing" and "O: HiCache tiers" rows.
- `dashboards/sglang/sglang.json`: the "Dynamo KV Router", "P/D Disagg Queues" and HiCache rows.
