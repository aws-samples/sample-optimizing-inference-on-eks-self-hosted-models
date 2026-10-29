# 01-kv-router: KV-aware routing (cell S1)

## What it does
Same aggregated topology as the baseline (cell S0), but the Dynamo Frontend sends each request to the
worker whose KV cache already holds the longest matching prefix, using the KV events the workers publish.
Repeated prefixes (multi-turn chats, shared system prompts) then reuse a cached prefix instead of being
recomputed on whichever worker is next in the rotation.

## What it changes
One field on the Frontend container: `DYN_ROUTER_MODE` goes from `round-robin` to `kv`
(`manifests/track-q-qwen38/components/router-kv`). The workers, the namespace `s-agg` and every resource
stay identical. `diff-vs-baseline.diff` is organised per component (a plain `diff -u` section per changed
part): here a single Frontend section with a one-line change.

## How to apply
```
kubectl apply -k examples/01-kv-router
```
It uses the baseline's namespace `s-agg` and DGD name `q38`, so it updates a running baseline in place;
only the Frontend spec changes.

## Where to look
- `dashboards/demo/take.json`: watch "KV cache hit rate" and "Requests waiting for first token" across the switch.
- `dashboards/sglang/sglang.json`: the "Dynamo KV Router" row (router KV hit rate distribution, per-worker
  inflight requests) is where the routing decisions show up.
