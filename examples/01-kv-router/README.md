# 01-kv-router: KV-aware routing (cell S1)

## What it does
Same aggregated topology as the baseline (cell S0), but the Dynamo Frontend sends each request to the
worker whose KV cache already holds the longest matching prefix, using the KV events the workers publish.
Repeated prefixes (multi-turn chats, shared system prompts) then reuse a cached prefix instead of being
recomputed on whichever worker is next in the rotation.

## What it changes
One field on the Frontend container: `DYN_ROUTER_MODE` goes from `round-robin` to `kv`. The workers, the
namespace `s-agg` and every resource stay identical: the command of [examples/README.md](../README.md) shows the one
changed non-comment line.

## How to apply
```
kubectl --context "$KUBE_CONTEXT" apply --server-side -f examples/01-kv-router/manifest.yaml
```
It uses the baseline's namespace `s-agg` and DGD name `q38`; against the baseline, the only differing
non-comment line is the Frontend's `DYN_ROUTER_MODE`. Applying it over a running baseline was not tested.

## Where to look
- `dashboards/demo/matrix.json`, row "R: KV-aware routing": watch "Engine hit rate per worker" and
  "Router predicted hit" across the switch (the router panels fill only with `kv`).
- `dashboards/sglang/sglang.json`: the "Dynamo KV Router" row (router KV hit rate distribution, per-worker
  inflight requests) is where the routing decisions show up.
