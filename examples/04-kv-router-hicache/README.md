# 04-kv-router-hicache: KV router + HiCache, aggregated (cell S4)

## What it does
The aggregated HiCache topology of `02-hicache` (cell S2) with the KV router of `01-kv-router` (cell S1) on the
Frontend. The router sends each request to the worker whose cache holds the longest matching prefix, and the
router also credits the prefixes that HiCache keeps in host RAM (L2), so a prefix that left GPU memory can still
be loaded back on that worker instead of recomputed. There is no prefill/decode split.

## What it changes
The union of two diffs, in namespace `s-agg-hic`:
- Frontend: `DYN_ROUTER_MODE` goes from `round-robin` to `kv`, the only difference from `02-hicache` (S2);
- Worker: `--enable-hierarchical-cache` and the other HiCache flags with `--hicache-size 200` (GB of host RAM per
  engine), the `SGLANG_HICACHE_NIXL_BACKEND_STORAGE_DIR` env, the `hicache-l3` volume and a larger memory request
  and limit, which with the namespace is the difference from `01-kv-router` (S1).
The command of [examples/README.md](../README.md) shows both.

## How to apply
```
kubectl --context "$KUBE_CONTEXT" apply --server-side -f examples/04-kv-router-hicache/manifest.yaml
```
It shares the namespace `s-agg-hic` and the DGD name `q38` with `02-hicache`; against it, the only differing non-comment
line is the Frontend's `DYN_ROUTER_MODE`. Applying it over a running S2 was not tested. Coming from any other folder,
delete that folder first. A plain apply does not empty the L3 directories left by earlier runs. On a running pod,
`clear_hicache_storage` empties that pod's own directory
([Reset between runs](../../docs/optimizations/hicache/README.md#reset-between-runs)); deleting the directories of
deleted pods was not tested.

## Where to look
- `dashboards/demo/matrix.json` with `namespace` = `s-agg-hic`: the "R: KV-aware routing" and "O: HiCache tiers"
  rows, across the switch from S2.
- `dashboards/sglang/sglang.json`: the "Dynamo KV Router" row and the HiCache row.
