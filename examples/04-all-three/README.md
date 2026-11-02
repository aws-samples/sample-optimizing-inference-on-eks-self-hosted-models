# 04-all-three: PD + HiCache + KV router (cell S7)

## What it does
All three techniques at once: the prefill/decode split of `03-pd-disaggregation`, the HiCache of
`02-hicache` on the prefill workers only (prefill is where prompt KV is computed and reused), and the
KV router of `01-kv-router` on the Frontend. The KV path has two options on this page: EFA (Elastic Fabric
Adapter, the AWS network interface that moves data between nodes) is the default; NVLink (NVIDIA's GPU-to-GPU
interconnect inside one node, not a network and not NVMe storage) is a design reference in `nvlink/`.

| | Option A - EFA (default) | Option B - NVLink (design reference) |
|---|---|---|
| NIXL backend | `LIBFABRIC` on EFA | `UCX` with `cuda_ipc` over NVLink |
| Placement | prefill and decode on any node | prefill and decode on the same node only |
| Pods | `privileged`, one EFA device each | `privileged` + `hostPID`, the same EFA device |
| Evidence | the transport of the recorded runs | not run on p6-b200 |

Use A (EFA) by default: it is the transport of the recorded runs and works across nodes. Use B (NVLink) only when
prefill and decode share a node, your pods may use `hostPID`, and the KV transfer rate is the bottleneck (an inference;
for the 27B the transfer overlaps prefill). See [Choosing the PD transport](../../README.md#choosing-the-pd-transport).

## What it changes
The union of the three diffs, in namespace `s-dis-hic` (`manifests/track-q-qwen38/overlays/t-dis-hic`
plus `manifests/track-q-qwen38/components/router-kv`):
- Frontend: `DYN_ROUTER_MODE=kv`;
- prefill: `--disaggregation-mode prefill`, `--disaggregation-transfer-backend nixl`, the EFA env, plus
  `--enable-hierarchical-cache` and the other HiCache flags with `--hicache-size 400` (GB of host RAM per
  engine), the `SGLANG_HICACHE_NIXL_BACKEND_STORAGE_DIR` env and the `hicache-l3` volume;
- decode: `--disaggregation-mode decode` and the same EFA settings as `03-pd-disaggregation`, no HiCache.

## How to apply
Both options deploy the DGD `q38` in namespace `s-dis-hic`, so delete the one you applied before you apply the other.
The GPU node must advertise `vpc.amazonaws.com/efa` for both. A plain apply does not empty the L3 directory; the
matrix driver (`tools/matrix_driver`) empties it when it tears the topology down.

### Option A - EFA (default, works across nodes)
```
kubectl apply -k examples/04-all-three
```
Check the transport: every engine logs that LIBFABRIC was instantiated.
```
kubectl -n s-dis-hic logs -l nvidia.com/dynamo-component-type=prefill -c main --tail=-1 | grep -E "Backend LIBFABRIC was instantiated|KVManager initialized with backend: LIBFABRIC"
```

### Option B - NVLink (same node only, design reference)
```
kubectl apply -k examples/04-all-three/nvlink
```
What it changes (`manifests/track-q-qwen38/components/pd-nvlink`, see `nvlink/diff-vs-efa.diff`), on `prefill` and
`decode` only: `SGLANG_DISAGGREGATION_NIXL_BACKEND=UCX`, `UCX_TLS=cuda_ipc,cuda_copy,tcp`, `UCX_PROTO_INFO` and
`UCX_LOG_LEVEL`, and `hostPID: true`. The EFA device, the `FI_*` env and `privileged: true` stay, because EFA is
still the path between nodes; the router and HiCache settings do not change. Prerequisites: a namespace that
enforces the Pod Security `privileged` level, and prefill and decode on the same node. It was not run on p6-b200
and is not one of the recorded runs.
Check the transport: the UCX protocol table in the prefill log must list `cuda_ipc`.
```
kubectl -n s-dis-hic logs -l nvidia.com/dynamo-component-type=prefill -c main --tail=-1 | grep cuda_ipc
```
Without the recipe (a default pod spec), UCX silently falls back to TCP: correct answers, no error, and a TTFT
that grows with the prompt. When to use which, and the evidence:
[PD guide, 3.1.1](../../docs/optimizations/pd-disaggregation/README.md#311-nvlink-on-one-node-design-reference-not-run-on-p6-b200)
and [TROUBLESHOOTING](../../docs/TROUBLESHOOTING.md#kv-transfer-silently-on-tcp-how-to-detect-and-what-changes-it).

## Where to look
- `diff-vs-baseline.diff`, per component: Frontend, then baseline `Worker` vs `prefill` and vs `decode`.
  A change decode shares with prefill is shown once, in prefill (a `# ... omitted` note in decode); the GPU-pin
  code is collapsed.
- `nvlink/diff-vs-efa.diff`: the plain diff of `rendered.yaml` (EFA) against `nvlink/rendered.yaml` (NVLink).
- `dashboards/demo/compare.json`: Baseline `s-agg`, Optimized `s-dis-hic`; every row applies.
- `dashboards/demo/take.json`: the "HiCache tiers" and "PD workers" rows.
- `dashboards/sglang/sglang.json`: the "Dynamo KV Router", "P/D Disagg Queues" and HiCache rows.
