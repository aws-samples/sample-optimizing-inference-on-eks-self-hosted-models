# 07-pd-hicache: PD disaggregation + HiCache on prefill (cell S6)

## What it does
The topology of `04-all-three` (cell S7) with the round-robin Frontend of the baseline: the prefill/decode split
of `03-pd-disaggregation` (cell S3), plus the HiCache of `02-hicache` on the prefill workers only, where prompt KV
is computed and reused. The Frontend spreads requests round-robin, so it ignores which prefill worker already
holds a prompt's prefix.

## What it changes
In namespace `s-dis-hic` (`manifests/track-q-qwen38/overlays/t-dis-hic`, no router component); the Frontend is
the baseline's, which is the only difference from `04-all-three` (S7):
- prefill: `--disaggregation-mode prefill`, `--disaggregation-transfer-backend nixl`, the EFA env, plus
  `--enable-hierarchical-cache` and the other HiCache flags with `--hicache-size 400` (GB of host RAM per
  engine), the `SGLANG_HICACHE_NIXL_BACKEND_STORAGE_DIR` env and the `hicache-l3` volume;
- decode: `--disaggregation-mode decode` and the same EFA settings, no HiCache, as in `03-pd-disaggregation` (S3).
`diff-vs-baseline.diff` is organised per component: metadata, then baseline `Worker` vs `prefill` and vs `decode`
(no Frontend section). A change decode shares with prefill is shown once, in prefill (a `# ... omitted` note).

## How to apply
```
kubectl apply -k examples/07-pd-hicache
```
It shares the namespace `s-dis-hic` and the DGD name `q38` with `04-all-three`. Coming from any other folder,
delete that folder first. The GPU node must advertise `vpc.amazonaws.com/efa`. A plain apply does not empty the
L3 directory; the matrix driver (`tools/matrix_driver`) empties it when it tears the topology down.

## Where to look
- `dashboards/demo/compare.json`: Baseline `s-agg`, Optimized `s-dis-hic`; the "HiCache tiers" row.
- `dashboards/demo/take.json`: the "HiCache tiers" and "PD workers" rows.
- `dashboards/sglang/sglang.json`: the "P/D Disagg Queues" and HiCache rows.
