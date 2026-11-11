# 06-pd-hicache: PD disaggregation + HiCache on prefill (cell S6)

## What it does
The topology of `07-all-three` (cell S7) with the round-robin Frontend of the baseline: the prefill/decode split
of `03-pd-disaggregation` (cell S3), plus the HiCache of `02-hicache` on the prefill workers only, where prompt KV
is computed and reused. The Frontend spreads requests round-robin, so it ignores which prefill worker already
holds a prompt's prefix.

## What it changes
In namespace `s-dis-hic`; the Frontend is the baseline's, which is the only difference from `07-all-three` (S7):
- prefill: `--disaggregation-mode prefill`, `--disaggregation-transfer-backend nixl`, the EFA env, plus
  `--enable-hierarchical-cache` and the other HiCache flags with `--hicache-size 400` (GB of host RAM per
  engine), the `SGLANG_HICACHE_NIXL_BACKEND_STORAGE_DIR` env and the `hicache-l3` volume;
- decode: `--disaggregation-mode decode` and the same EFA settings, no HiCache, as in `03-pd-disaggregation` (S3).
The command of [examples/README.md](../README.md) shows them; no Frontend line changes.

## How to apply
```
kubectl --context "$KUBE_CONTEXT" apply --server-side -f examples/06-pd-hicache/manifest.yaml
```
It shares the namespace `s-dis-hic` and the DGD name `q38` with `07-all-three`. Coming from any other folder,
delete that folder first. The GPU node must advertise `vpc.amazonaws.com/efa`. A plain apply does not empty the
L3 directories left by earlier runs. On a running prefill pod, `clear_hicache_storage` empties that pod's own
directory ([Reset between runs](../../docs/optimizations/hicache/README.md#reset-between-runs)); deleting the
directories of deleted pods was not tested. Check the transport: every engine logs that LIBFABRIC was instantiated.
```
kubectl -n s-dis-hic logs -l nvidia.com/dynamo-component-type=prefill -c main --tail=-1 | grep -E "Backend LIBFABRIC was instantiated|KVManager initialized with backend: LIBFABRIC"
```

## Where to look
- `dashboards/demo/matrix.json` with `namespace` = `s-dis-hic`: the "O: HiCache tiers" row.
- `dashboards/sglang/sglang.json`: the "P/D Disagg Queues" and HiCache rows.
