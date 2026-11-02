# 06-kv-router-pd: KV router + PD disaggregation (cell S5)

## What it does
The prefill/decode split of `03-pd-disaggregation` (cell S3) with the KV router of `01-kv-router` (cell S1) on the
Frontend. Four prefill and four decode workers, one GPU each, move the KV cache from prefill to decode over NIXL.
The router makes the KV-aware pick among the prefill workers; the decode hop is routed on load only. There is no
HiCache: a prefix that left GPU memory is recomputed. The KV path has two options on this page: EFA (Elastic
Fabric Adapter, the AWS network interface that moves data between nodes) is the default; NVLink (NVIDIA's
GPU-to-GPU interconnect inside one node, not a network and not NVMe storage) is a design reference in `nvlink/`.

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
In namespace `s-dis` (`manifests/track-q-qwen38/overlays/t-dis` plus `manifests/track-q-qwen38/components/router-kv`):
- Frontend: `DYN_ROUTER_MODE` goes from `round-robin` to `kv`, the only difference from `03-pd-disaggregation` (S3);
- `Worker` (`replicas: 8`) becomes `prefill` and `decode` (`replicas: 4` each) with `--disaggregation-mode`,
  `--disaggregation-transfer-backend nixl`, `--disaggregation-bootstrap-port`, the EFA env, one
  `vpc.amazonaws.com/efa` device per pod, `privileged: true` and the GPU-pin preamble, as in S3;
- compared with `04-all-three` (S7), the prefill workers run without HiCache and the namespace is `s-dis`.

## How to apply
Both options deploy the DGD `q38` in namespace `s-dis`, so delete the one you applied before you apply the other.
It shares that namespace and DGD with `03-pd-disaggregation`, so it updates a running S3 of the same option in
place (only the Frontend changes). Coming from any other folder, delete that folder first. The GPU node must
advertise `vpc.amazonaws.com/efa` (EFA device plugin) for both options.

### Option A - EFA (default, works across nodes)
```
kubectl apply -k examples/06-kv-router-pd
```
Check the transport: every engine logs that LIBFABRIC was instantiated.
```
kubectl -n s-dis logs -l nvidia.com/dynamo-component-type=prefill -c main --tail=-1 | grep -E "Backend LIBFABRIC was instantiated|KVManager initialized with backend: LIBFABRIC"
```

### Option B - NVLink (same node only, design reference)
```
kubectl apply -k examples/06-kv-router-pd/nvlink
```
What it changes (`manifests/track-q-qwen38/components/pd-nvlink`, see `nvlink/diff-vs-efa.diff`), on `prefill` and
`decode` only: `SGLANG_DISAGGREGATION_NIXL_BACKEND=UCX`, `UCX_TLS=cuda_ipc,cuda_copy,tcp`, `UCX_PROTO_INFO` and
`UCX_LOG_LEVEL`, and `hostPID: true`. The EFA device, the `FI_*` env and `privileged: true` stay, because EFA is
still the path between nodes; the KV router does not change. Prerequisites: a namespace that enforces the Pod
Security `privileged` level, and prefill and decode on the same node. It was not run on p6-b200 and is not one of
the recorded runs.
Check the transport: the UCX protocol table in the prefill log must list `cuda_ipc`.
```
kubectl -n s-dis logs -l nvidia.com/dynamo-component-type=prefill -c main --tail=-1 | grep cuda_ipc
```
Without the recipe (a default pod spec), UCX silently falls back to TCP: correct answers, no error, and a TTFT
that grows with the prompt. When to use which, and the evidence:
[PD guide, 3.1.1](../../docs/optimizations/pd-disaggregation/README.md#311-nvlink-on-one-node-design-reference-not-run-on-p6-b200)
and [TROUBLESHOOTING](../../docs/TROUBLESHOOTING.md#kv-transfer-silently-on-tcp-how-to-detect-and-what-changes-it).

## Where to look
- `diff-vs-baseline.diff`, per component: Frontend, then baseline `Worker` vs `prefill` and vs `decode`. A change
  decode shares with prefill is shown once, in prefill (a `# ... omitted` note in decode); the GPU-pin code is collapsed.
- `nvlink/diff-vs-efa.diff`: the plain diff of `rendered.yaml` (EFA) against `nvlink/rendered.yaml` (NVLink).
- `dashboards/demo/compare.json`: Baseline `s-agg`, Optimized `s-dis`; the "Latency over time" row.
- `dashboards/sglang/sglang.json`: the "Dynamo KV Router" and "P/D Disagg Queues" rows.
