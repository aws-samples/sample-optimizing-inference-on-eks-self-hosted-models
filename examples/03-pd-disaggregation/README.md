# 03-pd-disaggregation: prefill/decode split over NIXL LIBFABRIC/EFA (cell S3)

## What it does
The single aggregated Worker becomes two components: four prefill workers compute the prompt's KV cache and
four decode workers generate the tokens, one GPU each. The KV cache moves from the prefill GPU to the decode
GPU over NIXL with the LIBFABRIC backend on EFA, so prompt processing does not interrupt token generation.
Routing stays round-robin and there is no HiCache.

## What it changes
In namespace `s-dis`, the Frontend is unchanged and:
- `Worker` (`replicas: 8`) becomes `prefill` and `decode` (`replicas: 4` each);
- flags `--disaggregation-mode` (`prefill` or `decode`), `--disaggregation-transfer-backend nixl` and
  `--disaggregation-bootstrap-port`;
- env `SGLANG_DISAGGREGATION_NIXL_BACKEND=LIBFABRIC`, `FI_PROVIDER=efa`, `FI_EFA_USE_DEVICE_RDMA=1`,
  `FI_EFA_ENABLE_SHM` and `FI_EFA_ENABLE_SHM_TRANSFER` off, and the EFA `LD_LIBRARY_PATH`;
- one `vpc.amazonaws.com/efa` device per pod;
- no `IPC_LOCK` and `SYS_RESOURCE` capabilities, which the baseline Worker adds. The containers are not privileged;
  see [Privileged pods](../../docs/optimizations/pd-disaggregation/README.md#privileged-pods) in the PD guide.

## How to apply
```
kubectl --context "$KUBE_CONTEXT" apply --server-side -f examples/03-pd-disaggregation/manifest.yaml
```
The GPU node must advertise `vpc.amazonaws.com/efa` (EFA device plugin). Check the transport: every engine logs that
LIBFABRIC was instantiated.
```
kubectl -n s-dis logs -l nvidia.com/dynamo-component-type=prefill -c main --tail=-1 | grep -E "Backend LIBFABRIC was instantiated|KVManager initialized with backend: LIBFABRIC"
```

## Where to look
- the command of [examples/README.md](../README.md): everything this folder changes against the baseline.
- `dashboards/demo/matrix.json` with `namespace` = `s-dis`: the "Latency (all scenarios)" row.
- `dashboards/sglang/sglang.json`: the "P/D Disagg Queues" row (prefill and decode queues).
