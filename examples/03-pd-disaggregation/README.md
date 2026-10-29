# 03-pd-disaggregation: prefill/decode split over NIXL LIBFABRIC/EFA (cell S3)

## What it does
The single aggregated Worker becomes two components: four prefill workers compute the prompt's KV cache and
four decode workers generate the tokens, one GPU each. The KV cache moves from the prefill GPU to the decode
GPU over NIXL with the LIBFABRIC backend on EFA, so prompt processing does not interrupt token generation.
Routing stays round-robin and there is no HiCache.

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
```
kubectl apply -k examples/03-pd-disaggregation
```
The GPU node must advertise `vpc.amazonaws.com/efa` (EFA device plugin).

## Where to look
- `diff-vs-baseline.diff`, per component: baseline `Worker` vs `prefill`, then vs `decode`. A change decode
  shares with prefill is shown once, in prefill (a `# ... omitted` note in decode); the GPU-pin code is collapsed.
- `dashboards/demo/compare.json`: Baseline `s-agg`, Optimized `s-dis`; the "Latency over time" row.
- `dashboards/demo/take.json`: the "PD workers" row ("Prefill vs decode: running/queued requests").
- `dashboards/sglang/sglang.json`: the "P/D Disagg Queues" row (prefill and decode queues).
