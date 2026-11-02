# 03-pd-disaggregation: prefill/decode split over NIXL LIBFABRIC/EFA (cell S3)

## What it does
The single aggregated Worker becomes two components: four prefill workers compute the prompt's KV cache and
four decode workers generate the tokens, one GPU each. The KV cache moves from the prefill GPU to the decode
GPU over NIXL, so prompt processing does not interrupt token generation. Routing stays round-robin and there is
no HiCache. The KV path has two options on this page: EFA (Elastic Fabric Adapter, the AWS network interface
that moves data between nodes) is the default; NVLink (NVIDIA's GPU-to-GPU interconnect inside one node, not a
network and not NVMe storage) is a design reference in `nvlink/`.

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
In namespace `s-dis` (`manifests/track-q-qwen38/components/pd-efa`), the Frontend is unchanged and:
- `Worker` (`replicas: 8`) becomes `prefill` and `decode` (`replicas: 4` each);
- flags `--disaggregation-mode` (`prefill` or `decode`), `--disaggregation-transfer-backend nixl` and
  `--disaggregation-bootstrap-port`;
- env `SGLANG_DISAGGREGATION_NIXL_BACKEND=LIBFABRIC`, `FI_PROVIDER=efa`, `FI_EFA_USE_DEVICE_RDMA=1`,
  `FI_EFA_ENABLE_SHM` and `FI_EFA_ENABLE_SHM_TRANSFER` off, the EFA `LD_LIBRARY_PATH` and `EXPECTED_GPUS`;
- one `vpc.amazonaws.com/efa` device per pod, `privileged: true`, and a GPU-pin preamble in the launch
  script that sets `CUDA_VISIBLE_DEVICES` (a privileged pod sees every GPU of the node).

## How to apply
Both options deploy the DGD `q38` in namespace `s-dis`, so delete the one you applied before you apply the other.
The GPU node must advertise `vpc.amazonaws.com/efa` (EFA device plugin) for both.

### Option A - EFA (default, works across nodes)
```
kubectl apply -k examples/03-pd-disaggregation
```
Check the transport: every engine logs that LIBFABRIC was instantiated.
```
kubectl -n s-dis logs -l nvidia.com/dynamo-component-type=prefill -c main --tail=-1 | grep -E "Backend LIBFABRIC was instantiated|KVManager initialized with backend: LIBFABRIC"
```

### Option B - NVLink (same node only, design reference)
```
kubectl apply -k examples/03-pd-disaggregation/nvlink
```
What it changes (`manifests/track-q-qwen38/components/pd-nvlink`, see `nvlink/diff-vs-efa.diff`), on `prefill` and
`decode` only: `SGLANG_DISAGGREGATION_NIXL_BACKEND=UCX`, `UCX_TLS=cuda_ipc,cuda_copy,tcp`, `UCX_PROTO_INFO` and
`UCX_LOG_LEVEL`, and `hostPID: true`. The EFA device, the `FI_*` env and `privileged: true` stay, because EFA is
still the path between nodes. Prerequisites: a namespace that enforces the Pod Security `privileged` level, and
prefill and decode on the same node. It was not run on p6-b200 and is not one of the recorded runs.
Check the transport: the UCX protocol table in the prefill log must list `cuda_ipc`.
```
kubectl -n s-dis logs -l nvidia.com/dynamo-component-type=prefill -c main --tail=-1 | grep cuda_ipc
```
Without the recipe (a default pod spec), UCX silently falls back to TCP: correct answers, no error, and a TTFT
that grows with the prompt. When to use which, and the evidence:
[PD guide, 3.1.1](../../docs/optimizations/pd-disaggregation/README.md#311-nvlink-on-one-node-design-reference-not-run-on-p6-b200)
and [TROUBLESHOOTING](../../docs/TROUBLESHOOTING.md#kv-transfer-silently-on-tcp-how-to-detect-and-what-changes-it).

## Where to look
- `diff-vs-baseline.diff`, per component: baseline `Worker` vs `prefill`, then vs `decode`. A change decode
  shares with prefill is shown once, in prefill (a `# ... omitted` note in decode); the GPU-pin code is collapsed.
- `nvlink/diff-vs-efa.diff`: the plain diff of `rendered.yaml` (EFA) against `nvlink/rendered.yaml` (NVLink).
- `dashboards/demo/compare.json`: Baseline `s-agg`, Optimized `s-dis`; the "Latency over time" row.
- `dashboards/demo/take.json`: the "PD workers" row ("Prefill vs decode: running/queued requests").
- `dashboards/sglang/sglang.json`: the "P/D Disagg Queues" row (prefill and decode queues).
