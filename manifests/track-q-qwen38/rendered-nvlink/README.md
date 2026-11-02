# rendered-nvlink: committed renders of the NVLink alternative (S3, S5, S6, S7)

The shipped matrix driver and the published results use `rendered/` (EFA, NIXL LIBFABRIC). This directory is the
NVLink alternative of the reference design: the renders of `variants-nvlink/`, a design reference that was not
run on p6-b200. It needs `privileged` + `hostPID` (a Pod Security `privileged` namespace) and prefill and decode
on the same node. EFA stays the default transport and the only one for traffic between nodes.

Pick one transport per cell:
- EFA (default): `kubectl apply -f manifests/track-q-qwen38/rendered/S5.yaml`;
- NVLink: `kubectl apply -f manifests/track-q-qwen38/rendered-nvlink/S5.yaml`.

Both use the namespace `s-dis` and the DGD `q38`: delete the one you applied before you apply the other.
Regenerate with `scripts/render-track-q-nvlink.sh`; do not edit the files by hand.
