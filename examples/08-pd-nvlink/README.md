# 08-pd-nvlink: prefill/decode with the same-node KV path over NVLink (design reference)

## What it does
Cell S3 (`03-pd-disaggregation`) with one change: the KV cache moves from the prefill GPU to the decode GPU over
NIXL with the UCX backend (`cuda_ipc`, NVLink) instead of LIBFABRIC on EFA. This is a design reference: not one
of the recorded runs (those use `rendered/`, EFA) and the recipe was not run on p6-b200. EFA stays the default.
Built on `manifests/track-q-qwen38/variants-nvlink/S3` (render: `rendered-nvlink/S3.yaml`).

## What it changes
In namespace `s-dis` (`manifests/track-q-qwen38/components/pd-nvlink`), on `prefill` and `decode` only:
- env `SGLANG_DISAGGREGATION_NIXL_BACKEND=UCX`, `UCX_TLS=cuda_ipc,cuda_copy,tcp`, `UCX_PROTO_INFO` and `UCX_LOG_LEVEL`;
- `hostPID: true`. The EFA device, `FI_*` env and `privileged: true` stay: EFA is still the path between nodes.

## How to apply
```
kubectl apply -k examples/08-pd-nvlink
```
Needs a Pod Security `privileged` namespace (`privileged` + `hostPID`) and prefill and decode on one node.
Only one PD example fits on the node: delete whichever one is applied (03-pd-disaggregation, 04-all-three,
06-kv-router-pd or 07-pd-hicache) before applying this one. Compare with 03-pd-disaggregation (EFA) or
06-kv-router-pd (S5 on EFA) to see the same cell with the other transport:
`diff examples/03-pd-disaggregation/rendered.yaml examples/08-pd-nvlink/rendered.yaml`.

## Where to look
- `diff-vs-baseline.diff`: baseline `Worker` vs `prefill`, then vs `decode`.
- `dashboards/demo/take.json`: the "PD workers" row ("Prefill vs decode: running/queued requests").
