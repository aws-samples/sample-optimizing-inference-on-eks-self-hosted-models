# Examples: one folder per cell

A readable, per-technique view of the eight Qwen/Qwen3.8-27B-FP8 cells.
Read and apply each folder's `manifest.yaml`
(`kubectl --context "$KUBE_CONTEXT" apply --server-side -f examples/<folder>/manifest.yaml`).

| folder | cell | namespace | technique | R | O | D |
|---|---|---|---|---|---|---|
| `00-baseline` | S0 | `s-agg` | aggregated, round-robin, no HiCache | off | off | off |
| `01-kv-router` | S1 | `s-agg` | KV-aware routing on the Frontend | on | off | off |
| `02-hicache` | S2 | `s-agg-hic` | HiCache: L2 host RAM + L3 local storage via NIXL | off | on | off |
| `03-pd-disaggregation` | S3 | `s-dis` | prefill/decode split over NIXL LIBFABRIC/EFA | off | off | on |
| `04-kv-router-hicache` | S4 | `s-agg-hic` | KV router + HiCache, aggregated | on | on | off |
| `05-kv-router-pd` | S5 | `s-dis` | KV router + PD, no HiCache | on | off | on |
| `06-pd-hicache` | S6 | `s-dis-hic` | PD + HiCache (prefill), round-robin | off | on | on |
| `07-all-three` | S7 | `s-dis-hic` | PD + HiCache (prefill) + KV router | on | on | on |

R = KV router (`DYN_ROUTER_MODE=kv`), O = HiCache offload, D = PD disaggregation. Folder N holds cell SN: 00 is
the baseline, 01 to 03 add one technique each, 04 to 06 are the three pairs (R + O, R + D, O + D) and 07 adds all
three.

Each folder holds:
- `manifest.yaml`: the cell, applied as is;
- `README.md`: what the cell does, what it changes, how to apply it and which dashboard shows it.

The manifests carry comments, so a plain `diff` of two of them also prints comment lines. This command (bash or zsh)
leaves the comment lines out and shows exactly the knobs a cell flips:
```
diff <(grep -v '^ *#' examples/00-baseline/manifest.yaml) <(grep -v '^ *#' examples/<folder>/manifest.yaml)
```

Only one GPU topology fits on the node at a time, and the manifests do not create namespaces: delete the
folder you applied before you apply the next one. In two folders with the same namespace the only differing
non-comment line is the Frontend's `DYN_ROUTER_MODE` (same DGD name `q38`; run the command above on the two
folders). Applying one over the other on a
running cell was not tested. A delete does not empty the HiCache L3 directories of a folder, and deleting them was
not tested; on a running pod, `clear_hicache_storage` empties its own directory
([Reset between runs](../docs/optimizations/hicache/README.md#reset-between-runs)).
