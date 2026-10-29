# 05-kv-router-hicache: KV router + HiCache, aggregated (cell S4)

## What it does
The aggregated HiCache topology of `02-hicache` (cell S2) with the KV router of `01-kv-router` (cell S1) on the
Frontend. The router sends each request to the worker whose cache holds the longest matching prefix, and the
router also credits the prefixes that HiCache keeps in host RAM (L2), so a prefix that left GPU memory can still
be loaded back on that worker instead of recomputed. There is no prefill/decode split.

## What it changes
The union of two diffs, in namespace `s-agg-hic` (`manifests/track-q-qwen38/overlays/t-agg-hic` plus
`manifests/track-q-qwen38/components/router-kv`):
- Frontend: `DYN_ROUTER_MODE` goes from `round-robin` to `kv`, the only difference from `02-hicache` (S2);
- Worker: `--enable-hierarchical-cache` and the other HiCache flags with `--hicache-size 200` (GB of host RAM per
  engine), the `SGLANG_HICACHE_NIXL_BACKEND_STORAGE_DIR` env, the `hicache-l3` volume and a larger memory request
  and limit, which with the namespace is the difference from `01-kv-router` (S1).
`diff-vs-baseline.diff` is organised per component: plain `diff -u` sections for the metadata, Frontend and Worker.

## How to apply
```
kubectl apply -k examples/05-kv-router-hicache
```
It shares the namespace `s-agg-hic` and the DGD name `q38` with `02-hicache`, so it updates a running S2 in place
(only the Frontend changes). Coming from any other folder, delete that folder first. A plain apply does not empty
the L3 directory; the matrix driver (`tools/matrix_driver`) empties it when it tears the topology down.

## Where to look
- `dashboards/demo/take.json`: "KV cache hit rate" and the "HiCache tiers" row, across the switch from S2.
- `dashboards/demo/compare.json`: Baseline `s-agg`, Optimized `s-agg-hic`; the "HiCache tiers" row.
- `dashboards/sglang/sglang.json`: the "Dynamo KV Router" row and the HiCache row.
