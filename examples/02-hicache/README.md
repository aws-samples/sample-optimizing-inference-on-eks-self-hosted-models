# 02-hicache: hierarchical KV cache, L2 host RAM + L3 local storage (cell S2)

## What it does
Same aggregated, round-robin topology as the baseline, with SGLang HiCache on every worker. KV pages are
also kept in host RAM (L2, write-through) and backed up to the node's local NVMe instance store (L3) through
the NIXL POSIX backend, so a prefix that left GPU memory is loaded or prefetched back instead of recomputed.

## What it changes
Only the Worker, in its own namespace `s-agg-hic`:
- flags `--enable-hierarchical-cache`, `--hicache-size 200` (GB of host RAM per engine), `--hicache-mem-layout`,
  `--hicache-io-backend`, `--hicache-write-policy`, `--hicache-storage-backend nixl`,
  `--hicache-storage-prefetch-policy` and `--engine-route clear_hicache_storage:tm`;
- env `SGLANG_HICACHE_NIXL_BACKEND_PLUGIN=POSIX`, `SGLANG_HICACHE_NIXL_USE_DIRECT_IO=0` and
  `SGLANG_HICACHE_NIXL_BACKEND_STORAGE_DIR=/kvcache/l3`;
- a `hicache-l3` hostPath volume (one subdirectory per pod) and a larger pod memory request and limit for L2.
The Frontend and its round-robin routing are unchanged:
the command of [examples/README.md](../README.md) shows the namespace and Worker lines only.

## How to apply
```
kubectl --context "$KUBE_CONTEXT" apply --server-side -f examples/02-hicache/manifest.yaml
```
A plain apply does not empty the L3 directories left by earlier runs. On a running pod, `clear_hicache_storage`
empties that pod's own directory ([Reset between runs](../../docs/optimizations/hicache/README.md#reset-between-runs));
deleting the directories of deleted pods was not tested.

## Where to look
- `dashboards/demo/matrix.json` with `namespace` = `s-agg-hic`: the "O: HiCache tiers" row
  ("Tokens by cache source", "HiCache load-back", "HiCache prefetch", "Cache tier occupancy (L1/L2/L3)").
- `dashboards/sglang/sglang.json`: the HiCache row.
