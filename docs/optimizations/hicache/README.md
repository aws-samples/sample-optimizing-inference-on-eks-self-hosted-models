# KV cache offloading with SGLang HiCache

HiCache is SGLang's hierarchical KV cache. When the KV of a reused prefix no longer fits in GPU memory, the engine
keeps copies in host RAM (L2) and in files on the node's local disk (L3), and loads them back instead of computing the
prefill again (SGLang's [HiCache design][d-why] page, "design" below). In this sample HiCache is on in
[`02-hicache`](../../../examples/02-hicache/), [`04-kv-router-hicache`](../../../examples/04-kv-router-hicache/),
[`06-pd-hicache`](../../../examples/06-pd-hicache/) and [`07-all-three`](../../../examples/07-all-three/). The SGLang
statements below are read from the [SGLang `v0.5.21`](https://github.com/sgl-project/sglang/tree/v0.5.21) source, the
SGLang version of the runtime image ([`versions.env`](../../../versions.env)); the Dynamo statements from
[Dynamo `v1.6.1`](https://github.com/ai-dynamo/dynamo/tree/v1.6.1).

## How it works

| Tier | Medium | Private to | Size |
|---|---|---|---|
| L1 | GPU memory | one engine | the engine's KV pool, `sglang:max_total_num_tokens` |
| L2 | pinned host RAM | one engine ([design][d-scope]) | `--hicache-size` GB per rank; it overrides `--hicache-ratio` ([memory.py][s-size]) |
| L3 | files on the node's local disk, through the NIXL `POSIX` plugin | one pod in this sample (one directory per pod) | free space on that filesystem ([Pitfalls](#pitfalls)) |

- A request is matched against L1 and L2 first. For the rest SGLang asks L3 and, when the L3 hit is longer than a
  threshold (256 tokens by default), prefetches it into L2 ([design][d-prefetch]). When the prefetch stops, the
  prefill uses what was fetched together with L1 and L2 ([design][d-after]).
- The examples use `write_through`, SGLang's default write policy ([memory.py][s-policy]): every access is written
  back to the next tier ([design][d-wt]); by our reading L2 then also holds what L1 holds. Dynamo counts the
  capacity that L2 adds under `write_through` as host tokens minus device tokens ([capacity.py][dy-cap]). Writes from
  L2 to L3 skip blocks already in L3 ([design][d-skip]).
- Dynamo passes the HiCache flags to SGLang unchanged ([hicache.md][dy-pass]). Its support matrix lists KV events for
  SGLang's GPU and host tiers and no disk tier ([offloading-support-matrix.md][dy-matrix]); how the KV router credits
  L2 is in the [KV-aware routing guide](../kv-router/README.md). Dynamo's KV block manager (KVBM), deprecated in
  Dynamo v1.5.0 ([deprecations.mdx][dy-kvbm]), is not used: `grep -ci kvbm examples/*/manifest.yaml` prints `0` for
  every file.

## What the examples set

`grep -n -i hicache examples/02-hicache/manifest.yaml` lists the lines, and the command of
[examples/README.md](../../../examples/README.md) on `02-hicache` shows that only the namespace (`s-agg-hic`) and the
Worker change.

| Setting | Value | What it does |
|---|---|---|
| `--enable-hierarchical-cache` | set | HiCache on |
| `--hicache-size` | `200`; `400` on the prefill workers of 06 and 07 | GB (10^9 B) of L2 per rank ([design][d-size]); one rank per engine here (`--tp-size 1`) |
| `--hicache-mem-layout`, `--hicache-io-backend` | `page_first`, `kernel` | SGLang's defaults ([memory.py][s-layout]) |
| `--hicache-write-policy` | `write_through` | see above |
| `--hicache-storage-backend` | `nixl` | L3 through NIXL |
| `--hicache-storage-prefetch-policy` | `timeout` | stop waiting for an L3 prefetch after a time limit ([design][d-timeout]) |
| `--engine-route clear_hicache_storage:tm` | set | adds `/engine/clear_hicache_storage` on the worker's system port ([reference-guide.md][dy-route]) |
| `SGLANG_HICACHE_NIXL_BACKEND_PLUGIN` | `POSIX` | NIXL file plugin ([nixl_utils.py][s-plugins]) |
| `SGLANG_HICACHE_NIXL_USE_DIRECT_IO` | `"0"` | buffered I/O; SGLang's default is O_DIRECT ([environ.py][s-env]) |
| `SGLANG_HICACHE_NIXL_BACKEND_STORAGE_DIR` | `/kvcache/l3` | L3 directory in the container ([hicache_nixl.py][s-dir]) |
| volume `hicache-l3` | hostPath `/mnt/.ephemeral/hicache-l3/qwen38-matrix/<namespace>`, mounted with `subPathExpr: $(POD_NAME)` | one directory per pod |
| memory request / limit | `200Gi` / `224Gi`; `386Gi` / `410Gi` with 400 GB; `64Gi` / `128Gi` without HiCache | room for the pinned L2 ([What it costs](#what-it-costs)) |

- `04-kv-router-hicache` is 02 with `DYN_ROUTER_MODE=kv` on the Frontend, the only differing non-comment line.
- `06-pd-hicache` and `07-all-three` run HiCache on the 4 prefill workers only, one of the two PD setups in SGLang's
  [best practices][bp-pd]; their decode workers carry no HiCache flag. Eight engines with 200 GB (02, 04) and four
  with 400 GB (06, 07) have the same 1,600 GB of L2.

## When it helps and when it does not

- It can help when requests reuse long prefixes (multi-turn chats, shared documents) whose KV no longer fits in L1
  ([design][d-why]). A larger L2 gives a higher hit rate, but not linearly: once the reused prefixes fit, more L2 adds
  little ([design][d-note]).
- Under `write_through`, an L2 no larger than L1 adds no capacity; SGLang then logs
  `host pool (<N> tokens) is smaller than the device pool` at start ([base.py][s-warn]).
- L1 and L2 belong to one engine and L3 to one pod here. With round-robin (02, 06) a turn that lands on another engine
  misses its cached prefix; the KV router (04, 07) favours the engine that holds it
  ([KV-aware routing guide](../kv-router/README.md)).
- A prompt with no reused prefix has nothing to load, and an L3 hit below the threshold is not prefetched. When a
  load-back or an L3 prefetch is faster than recomputing on your GPU is not tested here.
- Under load a request can be admitted without what L3 holds for it. SGLang re-issues the L3 lookup of a queued
  request a limited number of times; past that cap the request is admitted with whatever the device holds
  ([memory.py][s-retry]) and SGLang logs
  `HiCache storage prefetch reissue cap reached ... the request is admitted without further L3 lookups`
  ([scheduler.py][s-reissue]). Observed by us once, with a variant of `07-all-three` on one p5en.48xlarge node (not
  published): the prefill engines logged this warning at the highest concurrency we ran.

## What it costs

- **Pinned host RAM**: `--hicache-size` GB per engine, allocated at start. SGLang checks it against the host memory
  the container may still use (bounded by its cgroup limits, minus a 10 GiB reserve) and stops with
  `Not enough host memory available` if it does not fit ([base.py][s-budget]), so the pod memory limit must cover the
  pool plus the process. `--hicache-size` counts 10^9 B and Kubernetes `Gi` counts 2^30 B ([memory units][k8s-mem]):
  200 GB is 186.3 GiB (`python3 -c 'print(200e9/2**30)'`).
- **Local disk**: L3 sits under `/mnt/.ephemeral`, like the model weights (see the `hostPath` volumes of the
  manifest).
- **Copy traffic**: under `write_through` each new block is copied from GPU to L2, then from L2 to L3 ([design][d-wt]).

## Check that it works

Boot log of one worker (container `main`):

```
kubectl --context "$KUBE_CONTEXT" -n s-agg-hic logs <worker-pod> -c main | grep -E 'hierarchical KV host pool|hierarchical Mamba cache|smaller than the device pool'
```

- `Allocating kv hierarchical KV host pool: <N> tokens, <X> GB host memory.` is the host KV pool: the L2 of the
  attention KV, `<N>` tokens ([base.py][s-kvlog]).
- `Allocating <Y> GB host memory for hierarchical Mamba cache (layout=page_first).` is the host pool of the Mamba
  state. The model of the examples mixes `full_attention` and `linear_attention` layers (`layer_types` in its
  [config.json][hf-config]), so SGLang splits `--hicache-size` between the two pools in proportion to their GPU pools
  ([hybrid_pool_assembler.py][s-split], [mamba.py][s-mamba]): `<X>` plus `<Y>` is about `--hicache-size`, and L2
  holds fewer KV tokens than `--hicache-size` alone suggests.
- `... host pool (<N> tokens) is smaller than the device pool ...` means L2 adds nothing under `write_through`.

Observed by us on one p5en.48xlarge node, with the examples applied with only the node pool name changed (not
published): every worker of 02 and 04 and every prefill worker of 06 and 07 printed the first two lines and not the
third. The PD workers of 06 and 07 were later changed to run unprivileged and without the GPU pin; whether this boot
log was taken on that form was not checked. For what was run unprivileged, see the
[PD guide](../pd-disaggregation/README.md).

Metrics on the worker's system port 9090:

```
kubectl --context "$KUBE_CONTEXT" -n s-agg-hic exec <worker-pod> -c main -- curl -s localhost:9090/metrics | grep -E '^sglang:(cached_tokens_total|prompt_tokens_total|hicache_host_|load_back_tokens_total|hicache_backup_tokens_total|backuped_tokens_total|prefetched_tokens_total)'
```

| Metric | Meaning |
|---|---|
| `sglang:cached_tokens_total{cache_source}` | reused prompt tokens: `device` L1, `host` L2, `storage` L3; `total` only when there is no breakdown ([metrics_collector.py][s-cached]) |
| `sglang:hicache_backup_tokens_total`, `sglang:load_back_tokens_total` | GPU to L2, L2 to GPU ([metrics_collector.py][s-l2]) |
| `sglang:backuped_tokens_total`, `sglang:prefetched_tokens_total` | L2 to L3, L3 to L2 ([metrics_collector.py][s-l3]) |
| `sglang:hicache_host_used_tokens`, `sglang:hicache_host_total_tokens` | L2 occupancy, in KV tokens ([metrics_collector.py][s-host]) |

The hit rate of each tier over a window `w`, summed over the workers the selectors match, is
`sum by (cache_source) (increase(sglang:cached_tokens_total{cache_source!="total"}[w])) / on() group_left sum(increase(sglang:prompt_tokens_total[w]))`;
`sglang:cache_hit_rate` is a gauge of the most recent value ([metrics_collector.py][s-gauge]), not a count over a
window. The HiCache rows of `dashboards/demo/matrix.json` and `dashboards/sglang/sglang.json` plot these series
except `sglang:hicache_backup_tokens_total` ([dashboards/README.md](../../../dashboards/README.md)).

## Reset between runs

```
kubectl --context "$KUBE_CONTEXT" -n s-agg-hic exec <worker-pod> -c main -- curl -sS -X POST -H 'Content-Type: application/json' -d '{}' http://127.0.0.1:9090/engine/flush_cache
kubectl --context "$KUBE_CONTEXT" -n s-agg-hic exec <worker-pod> -c main -- curl -sS -X POST -H 'Content-Type: application/json' -d '{}' http://127.0.0.1:9090/engine/clear_hicache_storage
```

- `flush_cache` (every worker of every example sets `--engine-route flush_cache:tm`) empties L1 and L2 and emits a
  KV event that clears the worker's blocks, but it does not touch L3 ([unified_radix_cache.py][s-reset]). It does
  nothing while requests are pending and logs `Cache not flushed because there are pending requests`
  ([scheduler.py][s-flush]). After a flush the router predicts no hit for a prefix the worker still serves from L3
  ([TROUBLESHOOTING](../../TROUBLESHOOTING.md#router-does-not-see-the-l3-tier)).
- `clear_hicache_storage` (only the HiCache workers set `--engine-route clear_hicache_storage:tm`: every worker of 02
  and 04, the prefill workers of 06 and 07) deletes every file under this pod's L3 directory ([scheduler.py][s-clear],
  [nixl_utils.py][s-nixlclear]).
- Observed by us once, with a variant of `07-all-three` on one p5en.48xlarge node (not published): both calls
  returned `"success":true` on the prefill workers. With L3 holding the files of earlier runs, `clear_hicache_storage`
  kept the engines busy long enough that one of them answered its readiness and liveness probes with 503 for a few
  seconds (no restart).
- Dynamo says to treat each configured route as remote administrative access and to restrict network access to the
  system server ([reference-guide.md][dy-admin]).

## Pitfalls

- **A buffered L3 hit can come from RAM.** Without O_DIRECT (which tries to minimize cache effects, [open(2)][open2])
  reads normally go through the Linux page cache, so a `storage` hit may be served from memory. Compare it with the
  "Node disk reads (md*)" panel of `dashboards/demo/matrix.json` over the same window.
- **L3 directories pile up.** Each pod writes to a directory named after itself; `clear_hicache_storage` and SGLang's
  L3 cleaner work only on that directory ([hicache_nixl.py][s-cleanerdirs]), and no script here removes the others
  (`git grep hicache-l3 -- scripts terraform` finds nothing). By our reading, directories of deleted pods stay under
  `/mnt/.ephemeral/hicache-l3/qwen38-matrix/<namespace>` until the node goes away. We did not test deleting them.
- **The L3 cleaner measures the whole filesystem.** When the filesystem of the L3 directory is 80% used, SGLang deletes
  this pod's oldest L3 files until it is below 70% ([nixl_cleaner.py][s-cleaner]); it is on by default
  ([nixl_utils.py][s-cleaneron]). By our reading of the `hostPath` volumes, that filesystem also holds the model
  weights.
- **`--shared-cache-type hicache` on the Frontend is for a Mooncake shared pool** ([hicache.md][dy-mooncake]), not
  for the per-pod files used here.

## What we did not verify

- Restore correctness: the sample ships no check that KV restored from L2 or L3 gives the same output as recomputing
  it, on this model and image. Check it on yours before you rely on it.
- Speed: when a load-back or an L3 prefetch beats recomputing, and whether L3 hits come from NVMe or from the page
  cache under load.
- NIXL telemetry (`NIXL_TELEMETRY_ENABLE`) together with the `nixl` storage backend; the manifests leave it unset.
- A way to delete the L3 directories of deleted pods.
- That the SGLang build in the image matches the `v0.5.21` tag line for line: the SGLang statements come from the tag.
- The hit-rate query on a live Prometheus: we evaluated it only with `promtool test rules` on synthetic series.
- The `kubectl` commands of this page were run by us on one p5en.48xlarge node only: `kubectl logs` in `s-agg-hic`
  and `s-dis-hic`, the `kubectl exec ... curl` commands in `s-dis-hic`. The `grep`, `diff` and `python3` checks were
  run on this repository.

[d-why]: https://github.com/sgl-project/sglang/blob/v0.5.21/docs/docs/advanced_features/hicache_design.mdx?plain=1#L10-L12
[d-scope]: https://github.com/sgl-project/sglang/blob/v0.5.21/docs/docs/advanced_features/hicache_design.mdx?plain=1#L25-L28
[d-prefetch]: https://github.com/sgl-project/sglang/blob/v0.5.21/docs/docs/advanced_features/hicache_design.mdx?plain=1#L95-L96
[d-timeout]: https://github.com/sgl-project/sglang/blob/v0.5.21/docs/docs/advanced_features/hicache_design.mdx?plain=1#L101
[d-after]: https://github.com/sgl-project/sglang/blob/v0.5.21/docs/docs/advanced_features/hicache_design.mdx?plain=1#L103
[d-wt]: https://github.com/sgl-project/sglang/blob/v0.5.21/docs/docs/advanced_features/hicache_design.mdx?plain=1#L126
[d-skip]: https://github.com/sgl-project/sglang/blob/v0.5.21/docs/docs/advanced_features/hicache_design.mdx?plain=1#L130
[d-size]: https://github.com/sgl-project/sglang/blob/v0.5.21/docs/docs/advanced_features/hicache_design.mdx?plain=1#L183
[d-note]: https://github.com/sgl-project/sglang/blob/v0.5.21/docs/docs/advanced_features/hicache_design.mdx?plain=1#L185
[bp-pd]: https://github.com/sgl-project/sglang/blob/v0.5.21/docs/docs/advanced_features/hicache_best_practices.mdx?plain=1#L77-L82
[s-size]: https://github.com/sgl-project/sglang/blob/v0.5.21/python/sglang/srt/arg_groups/fields/memory.py#L111-L114
[s-policy]: https://github.com/sgl-project/sglang/blob/v0.5.21/python/sglang/srt/arg_groups/fields/memory.py#L119-L125
[s-layout]: https://github.com/sgl-project/sglang/blob/v0.5.21/python/sglang/srt/arg_groups/fields/memory.py#L126-L145
[s-retry]: https://github.com/sgl-project/sglang/blob/v0.5.21/python/sglang/srt/arg_groups/fields/memory.py#L178-L201
[s-warn]: https://github.com/sgl-project/sglang/blob/v0.5.21/python/sglang/srt/mem_cache/pool_host/base.py#L212-L220
[s-budget]: https://github.com/sgl-project/sglang/blob/v0.5.21/python/sglang/srt/mem_cache/pool_host/base.py#L85-L102
[s-kvlog]: https://github.com/sgl-project/sglang/blob/v0.5.21/python/sglang/srt/mem_cache/pool_host/base.py#L222-L252
[s-mamba]: https://github.com/sgl-project/sglang/blob/v0.5.21/python/sglang/srt/mem_cache/pool_host/mamba.py#L143-L147
[s-split]: https://github.com/sgl-project/sglang/blob/v0.5.21/python/sglang/srt/mem_cache/hybrid_cache/hybrid_pool_assembler.py#L179-L194
[s-reissue]: https://github.com/sgl-project/sglang/blob/v0.5.21/python/sglang/srt/managers/scheduler.py#L3193-L3209
[s-clear]: https://github.com/sgl-project/sglang/blob/v0.5.21/python/sglang/srt/managers/scheduler.py#L4905-L4913
[s-flush]: https://github.com/sgl-project/sglang/blob/v0.5.21/python/sglang/srt/managers/scheduler.py#L5187-L5215
[s-reset]: https://github.com/sgl-project/sglang/blob/v0.5.21/python/sglang/srt/mem_cache/unified_radix_cache.py#L368-L402
[s-plugins]: https://github.com/sgl-project/sglang/blob/v0.5.21/python/sglang/srt/mem_cache/storage/nixl/nixl_utils.py#L118-L124
[s-cleaneron]: https://github.com/sgl-project/sglang/blob/v0.5.21/python/sglang/srt/mem_cache/storage/nixl/nixl_utils.py#L49-L55
[s-nixlclear]: https://github.com/sgl-project/sglang/blob/v0.5.21/python/sglang/srt/mem_cache/storage/nixl/nixl_utils.py#L236-L253
[s-dir]: https://github.com/sgl-project/sglang/blob/v0.5.21/python/sglang/srt/mem_cache/storage/nixl/hicache_nixl.py#L87-L93
[s-cleanerdirs]: https://github.com/sgl-project/sglang/blob/v0.5.21/python/sglang/srt/mem_cache/storage/nixl/hicache_nixl.py#L162-L183
[s-cleaner]: https://github.com/sgl-project/sglang/blob/v0.5.21/python/sglang/srt/mem_cache/storage/nixl/nixl_cleaner.py#L148-L210
[s-env]: https://github.com/sgl-project/sglang/blob/v0.5.21/python/sglang/srt/environ.py#L785-L789
[s-gauge]: https://github.com/sgl-project/sglang/blob/v0.5.21/python/sglang/srt/observability/metrics_collector.py#L305-L310
[s-host]: https://github.com/sgl-project/sglang/blob/v0.5.21/python/sglang/srt/observability/metrics_collector.py#L655-L666
[s-cached]: https://github.com/sgl-project/sglang/blob/v0.5.21/python/sglang/srt/observability/metrics_collector.py#L1800-L1821
[s-l3]: https://github.com/sgl-project/sglang/blob/v0.5.21/python/sglang/srt/observability/metrics_collector.py#L1893-L1904
[s-l2]: https://github.com/sgl-project/sglang/blob/v0.5.21/python/sglang/srt/observability/metrics_collector.py#L2231-L2283
[dy-pass]: https://github.com/ai-dynamo/dynamo/blob/v1.6.1/docs/fern/pages/developer-guide/knowledge-base/modular-components/backends/sglang/hicache.md?plain=1#L45-L46
[dy-mooncake]: https://github.com/ai-dynamo/dynamo/blob/v1.6.1/docs/fern/pages/developer-guide/knowledge-base/modular-components/backends/sglang/hicache.md?plain=1#L145-L148
[dy-route]: https://github.com/ai-dynamo/dynamo/blob/v1.6.1/docs/fern/pages/developer-guide/knowledge-base/modular-components/backends/sglang/reference-guide.md?plain=1#L144-L155
[dy-admin]: https://github.com/ai-dynamo/dynamo/blob/v1.6.1/docs/fern/pages/developer-guide/knowledge-base/modular-components/backends/sglang/reference-guide.md?plain=1#L174-L175
[dy-matrix]: https://github.com/ai-dynamo/dynamo/blob/v1.6.1/docs/fern/pages/developer-guide/knowledge-base/modular-components/router/offloading-support-matrix.md?plain=1#L14-L17
[dy-kvbm]: https://github.com/ai-dynamo/dynamo/blob/v1.6.1/docs/fern/pages/reference/general/releases/deprecations.mdx?plain=1#L36
[dy-cap]: https://github.com/ai-dynamo/dynamo/blob/v1.6.1/components/src/dynamo/sglang/capacity.py#L219-L224
[open2]: https://man7.org/linux/man-pages/man2/open.2.html
[k8s-mem]: https://kubernetes.io/docs/concepts/configuration/manage-resources-containers/#meaning-of-memory
[hf-config]: https://huggingface.co/Qwen/Qwen3.8-27B-FP8/blob/017b9c7af6b5689d5dd426a76e0bc077eb5ca20a/config.json

License: MIT-0, see [LICENSE](../../../LICENSE).
