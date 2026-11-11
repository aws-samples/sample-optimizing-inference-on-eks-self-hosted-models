# 05-kv-router-pd: KV router + PD disaggregation (cell S5)

## What it does
The prefill/decode split of `03-pd-disaggregation` (cell S3) with the KV router of `01-kv-router` (cell S1) on the
Frontend. Four prefill and four decode workers, one GPU each, move the KV cache from prefill to decode over NIXL
with the LIBFABRIC backend on EFA. The router makes the KV-aware pick among the prefill workers; decode routing
turns off overlap scoring and prefill-token tracking ([PD guide](../../docs/optimizations/pd-disaggregation/README.md#how-it-works)).
There is no HiCache: a prefix that left GPU memory is recomputed.

## What it changes
In namespace `s-dis`:
- Frontend: `DYN_ROUTER_MODE` goes from `round-robin` to `kv`, the only difference from `03-pd-disaggregation` (S3);
- `Worker` (`replicas: 8`) becomes `prefill` and `decode` (`replicas: 4` each) with `--disaggregation-mode`,
  `--disaggregation-transfer-backend nixl`, `--disaggregation-bootstrap-port`, the EFA env, one
  `vpc.amazonaws.com/efa` device per pod and no added capabilities, as in S3;
- compared with `07-all-three` (S7), the prefill workers run without HiCache and the namespace is `s-dis`.

## How to apply
```
kubectl --context "$KUBE_CONTEXT" apply --server-side -f examples/05-kv-router-pd/manifest.yaml
```
It shares the namespace `s-dis` and the DGD name `q38` with `03-pd-disaggregation`; against it, the only
differing non-comment line is the Frontend's `DYN_ROUTER_MODE`. Applying it over a running S3 was not tested.
Coming from any other folder, delete that folder first. The GPU node must
advertise `vpc.amazonaws.com/efa` (EFA device plugin). Check the transport: every engine logs that LIBFABRIC was
instantiated.
```
kubectl -n s-dis logs -l nvidia.com/dynamo-component-type=prefill -c main --tail=-1 | grep -E "Backend LIBFABRIC was instantiated|KVManager initialized with backend: LIBFABRIC"
```

## Where to look
- the command of [examples/README.md](../README.md): everything this folder changes against the baseline.
- `dashboards/demo/matrix.json` with `namespace` = `s-dis`: the "Latency (all scenarios)" row.
- `dashboards/sglang/sglang.json`: the "Dynamo KV Router" and "P/D Disagg Queues" rows.
