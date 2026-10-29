# Examples: one folder per cell

A readable, per-technique view of the eight Track Q (Qwen/Qwen3.8-27B-FP8) cells.
`manifests/track-q-qwen38` is the source of truth: each folder's `kustomization.yaml` has exactly one
resource, its cell under `manifests/track-q-qwen38/cells/`, so `kubectl apply -k examples/<folder>`
deploys exactly that cell.

| folder | cell | namespace | technique | R | O | D |
|---|---|---|---|---|---|---|
| `00-baseline` | S0 | `s-agg` | aggregated, round-robin, no HiCache | off | off | off |
| `01-kv-router` | S1 | `s-agg` | KV-aware routing on the Frontend | on | off | off |
| `02-hicache` | S2 | `s-agg-hic` | HiCache: L2 host RAM + L3 local storage via NIXL | off | on | off |
| `03-pd-disaggregation` | S3 | `s-dis` | prefill/decode split over NIXL LIBFABRIC/EFA | off | off | on |
| `04-all-three` | S7 | `s-dis-hic` | PD + HiCache (prefill) + KV router | on | on | on |
| `05-kv-router-hicache` | S4 | `s-agg-hic` | KV router + HiCache, aggregated | on | on | off |
| `06-kv-router-pd` | S5 | `s-dis` | KV router + PD, no HiCache | on | off | on |
| `07-pd-hicache` | S6 | `s-dis-hic` | PD + HiCache (prefill), round-robin | off | on | on |

R = KV router (`DYN_ROUTER_MODE=kv`), O = HiCache offload, D = PD disaggregation. The folders 00 to 04 add
one technique at a time (04 adds all three); 05 to 07 are the remaining pairs.

Each folder holds:
- `kustomization.yaml`: the pointer to the cell;
- `rendered.yaml`: `kubectl kustomize` of the folder (identical to `manifests/track-q-qwen38/rendered/S<n>.yaml`);
- `diff-vs-baseline.diff` (not in `00-baseline`): the unified diff of `rendered.yaml` against
  `00-baseline/rendered.yaml`, i.e. exactly the knobs the cell flips;
- `README.md`: what the cell does, what it changes, how to apply it and which dashboard shows it.

Do not edit `rendered.yaml` or `diff-vs-baseline.diff` by hand. Change `manifests/track-q-qwen38`, then run
`scripts/render-track-q.sh` and `scripts/render-examples.sh` (both offline: they only render).

Only one GPU topology fits on the node at a time, and the manifests do not create namespaces: delete the
folder you applied before you apply the next one. Two folders with the same namespace differ only in the
router mode of the Frontend (same DGD name `q38`), so applying one over the other updates it in place. For the
recorded matrix, the matrix driver (`tools/matrix_driver`) tears each topology down, and empties its HiCache L3
directory, before the next one.
