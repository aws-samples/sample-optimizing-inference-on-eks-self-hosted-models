# variants-nvlink: NVLink alternative of the prefill/decode cells

Overlays for S3, S5, S6 and S7: each is the published cell plus `components/pd-nvlink`, so the KV path between
the prefill and decode pods of one node goes over NVLink (NIXL UCX, `cuda_ipc`) instead of LIBFABRIC on EFA.

This is a design reference. The matrix driver and the published results use `rendered/` (EFA); this tree is the
NVLink alternative of the reference architecture and it was not run on p6-b200. EFA stays the default transport
and the only one for traffic between nodes. The pods need `privileged` + `hostPID` (a Pod Security `privileged`
namespace) and prefill and decode on the same node.

The committed renders are in `rendered-nvlink/`; regenerate them with `scripts/render-track-q-nvlink.sh`.
