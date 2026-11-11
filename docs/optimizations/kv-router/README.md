# KV-aware routing

The Dynamo Frontend picks a worker for every request. With `round-robin`, the default, it rotates through the workers.
With `kv` it gives each worker a cost made of the prompt work that worker would still have to do, after crediting the
prefix it already holds in its KV cache, plus the load it already carries, and sends the request to the lowest cost.
In this sample the switch is one Frontend env value, `DYN_ROUTER_MODE` (`--router-mode`,
[frontend-configuration.mdx:106-114](https://github.com/ai-dynamo/dynamo/blob/v1.6.1/docs/fern/pages/reference/components/frontend-configuration.mdx?plain=1#L106-L114)).

Dynamo links below point at tag `v1.6.1` of [ai-dynamo/dynamo](https://github.com/ai-dynamo/dynamo/tree/v1.6.1), the
Dynamo version of the runtime image (`DYNAMO_RUNTIME_VERSION` in [`versions.env`](../../../versions.env)).

## How it works

- **KV events.** An SGLang worker publishes the KV blocks it stores and removes only when it is started with
  `--kv-events-config` ([router-operations.md:138](https://github.com/ai-dynamo/dynamo/blob/v1.6.1/docs/fern/pages/developer-guide/knowledge-base/modular-components/router/router-operations.md?plain=1#L138)).
  Every example sets it on every worker component, in both modes
  ([`examples/00-baseline/manifest.yaml`](../../../examples/00-baseline/manifest.yaml)). The router block size follows
  SGLang's `--page-size` ([capacity.py:122-131](https://github.com/ai-dynamo/dynamo/blob/v1.6.1/components/src/dynamo/sglang/capacity.py#L122-L131)),
  64 tokens in the examples.
- **Index.** The Frontend keeps a prefix tree of block hashes with the worker id on each node, so one lookup returns
  the matched blocks per worker ([router-design.md:152-156](https://github.com/ai-dynamo/dynamo/blob/v1.6.1/docs/fern/pages/developer-guide/knowledge-base/modular-components/router/router-design.md?plain=1#L152-L156)).
- **Cost.** Per worker, from [routing-concepts.md:28-36](https://github.com/ai-dynamo/dynamo/blob/v1.6.1/docs/fern/pages/developer-guide/knowledge-base/modular-components/router/routing-concepts.md?plain=1#L28-L36):

  ```text
  adjusted_prefill_blocks = max(0, active_prefill_blocks + incoming_prompt_blocks - overlap_credit_blocks)
  cost = prefill_load_scale * adjusted_prefill_blocks + active_decode_blocks + incoming_active_blocks
         + decode_active_request_weight * active_requests     # weight 0 by default
  ```

  `overlap_credit_blocks` combines the device, host, disk and shared-cache credits for the prompt blocks the worker
  already holds. The lowest cost wins
  ([routing-concepts.md:66](https://github.com/ai-dynamo/dynamo/blob/v1.6.1/docs/fern/pages/developer-guide/knowledge-base/modular-components/router/routing-concepts.md?plain=1#L66)).
  Cache credit and load sit in one sum, so a worker that holds the prefix can lose to a less loaded one.
- Without worker KV events, `kv` scores load only
  ([dynamo-frontend.md:63](https://github.com/ai-dynamo/dynamo/blob/v1.6.1/docs/fern/pages/kubernetes/kv-aware-routing/dynamo-frontend.md?plain=1#L63)).
- With prefill/decode disaggregation, `kv` builds this router for the prefill workers
  ([activation.rs:368-381](https://github.com/ai-dynamo/dynamo/blob/v1.6.1/lib/llm/src/kv_router/prefill_router/activation.rs#L368-L381)),
  and the decode hop turns overlap scoring off
  ([disaggregated-serving.md:26-29](https://github.com/ai-dynamo/dynamo/blob/v1.6.1/docs/fern/pages/developer-guide/knowledge-base/modular-components/router/disaggregated-serving.md?plain=1#L26-L29)).
  See the [PD guide](../pd-disaggregation/README.md).
- With HiCache the router also credits host-memory (CPU-tier) hits, at `--router-host-cache-hit-weight`
  ([frontend-configuration.mdx:177-181](https://github.com/ai-dynamo/dynamo/blob/v1.6.1/docs/fern/pages/reference/components/frontend-configuration.mdx?plain=1#L177-L181)).
  Dynamo's support matrix lists SGLang's host tier as HiCache plus KV events, with host-tier events from SGLang 0.5.11
  ([offloading-support-matrix.md:17](https://github.com/ai-dynamo/dynamo/blob/v1.6.1/docs/fern/pages/developer-guide/knowledge-base/modular-components/router/offloading-support-matrix.md?plain=1#L17),
  [:86](https://github.com/ai-dynamo/dynamo/blob/v1.6.1/docs/fern/pages/developer-guide/knowledge-base/modular-components/router/offloading-support-matrix.md?plain=1#L86)).
  See the [HiCache guide](../hicache/README.md).

## What the sample changes

`examples/01-kv-router` is `examples/00-baseline` with one non-comment line changed; the command of
[examples/README.md](../../../examples/README.md) prints it:

```console
$ diff <(grep -v '^ *#' examples/00-baseline/manifest.yaml) <(grep -v '^ *#' examples/01-kv-router/manifest.yaml)
36c36
<             value: round-robin
---
>             value: kv
```

The pairs `02-hicache` / `04-kv-router-hicache`, `03-pd-disaggregation` / `05-kv-router-pd` and `06-pd-hicache` /
`07-all-three` differ in the same non-comment line. No example sets a router tuning flag or session affinity. Apply a
folder as in [DEPLOY.md](../../DEPLOY.md).

## Session affinity is a separate option

`kv` scores every request again; it does not bind a session to a worker (in our runs below, most sessions moved).
Session affinity does, and it is off unless both of these hold ([configuration-and-tuning.md:329-333](https://github.com/ai-dynamo/dynamo/blob/v1.6.1/docs/fern/pages/developer-guide/knowledge-base/modular-components/router/configuration-and-tuning.md?plain=1#L329-L333)):

1. The Frontend runs with `--router-session-affinity-ttl-secs <seconds>` (env `DYN_ROUTER_SESSION_AFFINITY_TTL_SECS`,
   [router_args.py:247-260](https://github.com/ai-dynamo/dynamo/blob/v1.6.1/components/src/dynamo/common/configuration/groups/router_args.py#L247-L260)).
   The value is an idle timeout. In the default mode, `hard`, the first request binds the session to its worker and
   later requests go to that worker ([configuration-and-tuning.md:335-340](https://github.com/ai-dynamo/dynamo/blob/v1.6.1/docs/fern/pages/developer-guide/knowledge-base/modular-components/router/configuration-and-tuning.md?plain=1#L335-L340)).
2. Each request carries the header `X-Dynamo-Session-ID`. The header alone gives the session an identity but does not
   turn affinity on.

For multi-turn trajectories, Dynamo's RL routing page says to start with KV-aware routing and to "add session affinity
only when strict stickiness is required", and warns that "Affinity can pin work to an overloaded or replaced worker"
([routing.md:19](https://github.com/ai-dynamo/dynamo/blob/v1.6.1/docs/fern/pages/use-cases/reinforcement-learning/routing.md?plain=1#L19)).

To turn it on in an example, append two args to the Frontend container (600 is the value we ran):

```yaml
        - args:
          - -m
          - dynamo.frontend
          - --http-host
          - 0.0.0.0
          - --http-port
          - "8000"
          - --router-session-affinity-ttl-secs
          - "600"
```

AIPerf 0.13.0 sets `X-Dynamo-Session-ID` to each session's correlation id when
`AIPERF_HTTP_X_DYNAMO_SESSION_ID_FROM_CORRELATION_ID=true`; the default is `false`
([environment.py:848](https://github.com/ai-dynamo/aiperf/blob/v0.13.0/src/aiperf/common/environment.py#L848),
[:956-962](https://github.com/ai-dynamo/aiperf/blob/v0.13.0/src/aiperf/common/environment.py#L956-L962),
[base_transports.py:163-170](https://github.com/ai-dynamo/aiperf/blob/v0.13.0/src/aiperf/transports/base_transports.py#L163-L170)).
[`benchmarks/perf.yaml`](../../../benchmarks/perf.yaml) does not set it.

## What we observed

Observed by us, not published: one `p5en.48xlarge` (8x H200), `examples/00-baseline` and `examples/01-kv-router` with
the `nodeSelector` set to that cluster's node pool and one bookkeeping label added, two runs per setting, each run
after deleting and re-applying the DGD. The client was AIPerf 0.13.0 in a CPU-only pod in namespace `s-agg`: 48
sessions of 5 turns that all start with the same 4096-token system prompt, 16 at a time. The affinity runs added the
two Frontend args above and set the AIPerf env value to `true`.

Run it from a pod in `s-agg`: the `--url` resolves only inside the cluster. We installed AIPerf in a venv in that pod
(`pip install aiperf==0.13.0`); AIPerf downloaded the tokenizer from Hugging Face, so the pod needs internet access.

```bash
python3 -m aiperf profile --model Qwen/Qwen3.8-27B-FP8 --url http://q38-frontend:8000 --endpoint-type chat --streaming \
  --tokenizer Qwen/Qwen3.8-27B-FP8 --tokenizer-revision 017b9c7af6b5689d5dd426a76e0bc077eb5ca20a \
  --num-sessions 48 --num-dataset-entries 48 --conversation-turn-mean 5 --conversation-turn-stddev 0 \
  --conversation-turn-delay-mean 500 --shared-system-prompt-length 4096 --user-context-prompt-length 512 \
  --synthetic-input-tokens-mean 200 --output-tokens-mean 128 --concurrency 16 --warmup-request-count 16 \
  --random-seed 42 --extra-inputs '{"nvext":{"extra_fields":["worker_id"]}}' --export-level raw \
  --output-artifact-dir ./aiperf-out --no-gpu-telemetry --ui-type simple
```

- `kv` alone did not keep sessions on one worker: most sessions had turns on more than one worker. Compared with
  `round-robin`, a larger share of the prompt tokens came from the cache and the engines computed fewer prefill
  tokens. TTFT p50 was a little lower (two runs per setting, not called significant); TTFT mean, p99 and ITL stayed
  within the spread between runs.
- With affinity, every later turn went to the worker of the session's first turn, in the `kv` runs and in the
  `round-robin` runs. The cache share rose and the prefill work fell more than with `kv` alone. TTFT p50 was lower
  than with `round-robin`; with two runs per setting we do not call that significant. TTFT p99 was a little higher in
  every affinity run than in every `round-robin` run. TTFT mean and ITL did not change.
- These runs do not tell how much of the change with affinity comes from overlap scoring and how much from affinity.
- Not tested: load near saturation, other session counts, prompt shapes or seeds, more than one Frontend replica
  (bindings sync between replicas on a best-effort basis,
  [configuration-and-tuning.md:367-383](https://github.com/ai-dynamo/dynamo/blob/v1.6.1/docs/fern/pages/developer-guide/knowledge-base/modular-components/router/configuration-and-tuning.md?plain=1#L367-L383)),
  the `soft` mode, other nodes. These runs used neither HiCache nor PD.

## How to check

We read these on the runs above:

```bash
F=$(kubectl --context "$KUBE_CONTEXT" -n s-agg get pod -l nvidia.com/dynamo-component-type=frontend -o name)
kubectl --context "$KUBE_CONTEXT" -n s-agg logs "$F" -c main | grep -m1 'Selected worker'
kubectl --context "$KUBE_CONTEXT" -n s-agg exec "$F" -c main -- sh -c \
  "curl -s localhost:8000/metrics | grep -E '^dynamo_component_router_(kv_hit_rate|non_max_overlap)'"
```

- In our aggregated runs the Frontend logged one `Selected worker` line at INFO for each request that was not pinned,
  with `router_mode="kv"` ([selector/mod.rs:326-354](https://github.com/ai-dynamo/dynamo/blob/v1.6.1/lib/kv-router/src/scheduling/selector/mod.rs#L326-L354))
  or `router_mode="round-robin"` ([push_router.rs:922-929](https://github.com/ai-dynamo/dynamo/blob/v1.6.1/lib/runtime/src/pipeline/network/egress/push_router.rs#L922-L929)).
  In `kv` mode a request pinned to a worker logs `Selected pinned worker` instead
  ([selector/mod.rs:316-325](https://github.com/ai-dynamo/dynamo/blob/v1.6.1/lib/kv-router/src/scheduling/selector/mod.rs#L316-L325));
  in our `kv` runs with affinity, that was every turn after the first of each session. In our `round-robin` runs with
  affinity, the turns after the first of each session logged no selection line.
- `dynamo_component_router_kv_hit_rate` is the router's predicted hit at routing time
  ([metrics-catalog.mdx:292-294](https://github.com/ai-dynamo/dynamo/blob/v1.6.1/docs/fern/pages/reference/observability/metrics-catalog.mdx?plain=1#L292-L294)),
  a prediction, not the hit the engine served. We saw samples only in `kv`.
- `dynamo_component_router_non_max_overlap_selections_total` counts prefill selections sent to a worker with less KV
  overlap than another eligible worker; pinned requests are excluded
  ([metrics-catalog.mdx:296-298](https://github.com/ai-dynamo/dynamo/blob/v1.6.1/docs/fern/pages/reference/observability/metrics-catalog.mdx?plain=1#L296-L298)).
  Our reading of the v1.6.1 source: only the prefill router of a disaggregated deployment increments it
  ([embedded.rs:319-327](https://github.com/ai-dynamo/dynamo/blob/v1.6.1/lib/llm/src/kv_router/embedded.rs#L319-L327)).
  In our aggregated runs it stayed at 0.
- To see which worker served a request, add `"nvext": {"extra_fields": ["worker_id"]}` to the request body; the
  response `nvext.worker_id` carries the worker ids
  ([nvext.md:40](https://github.com/ai-dynamo/dynamo/blob/v1.6.1/docs/fern/pages/developer-guide/additional-resources/nvidia-request-extensions-nvext.md?plain=1#L40),
  [:260](https://github.com/ai-dynamo/dynamo/blob/v1.6.1/docs/fern/pages/developer-guide/additional-resources/nvidia-request-extensions-nvext.md?plain=1#L260)).
  The AIPerf command above does it; in our aggregated runs the id was in `decode_worker_id`.
- With PD, Dynamo's tuning guide says to check the Frontend's `Activating prefill router` log line for the resolved
  router configuration of each hop ([configuration-and-tuning.md:22](https://github.com/ai-dynamo/dynamo/blob/v1.6.1/docs/fern/pages/developer-guide/knowledge-base/modular-components/router/configuration-and-tuning.md?plain=1#L22)).

## Pitfalls

- A worker set that advertises its own router configuration replaces the Frontend's for that set; the settings do not
  merge ([configuration-and-tuning.md:16-22](https://github.com/ai-dynamo/dynamo/blob/v1.6.1/docs/fern/pages/developer-guide/knowledge-base/modular-components/router/configuration-and-tuning.md?plain=1#L16-L22)).
  The examples set router config only on the Frontend. If you add it to a worker, restate there every non-default
  setting that worker set needs ([configuration-and-tuning.md:20](https://github.com/ai-dynamo/dynamo/blob/v1.6.1/docs/fern/pages/developer-guide/knowledge-base/modular-components/router/configuration-and-tuning.md?plain=1#L20)).
- At v1.6.1 the scoring flags (`--router-kv-overlap-score-credit`, `--router-temperature` and others) are deprecated,
  with removal in v1.7, in favour of `--router-policy-config`
  ([frontend-configuration.mdx:138](https://github.com/ai-dynamo/dynamo/blob/v1.6.1/docs/fern/pages/reference/components/frontend-configuration.mdx?plain=1#L138),
  [:148-149](https://github.com/ai-dynamo/dynamo/blob/v1.6.1/docs/fern/pages/reference/components/frontend-configuration.mdx?plain=1#L148-L149),
  [:205-206](https://github.com/ai-dynamo/dynamo/blob/v1.6.1/docs/fern/pages/reference/components/frontend-configuration.mdx?plain=1#L205-L206)).
  The examples set none of them, and we tested none.
- We deleted the DGD and applied it again before every run, so each run started with new workers and a new Frontend.
  Applying `01-kv-router` over a running baseline was not tested.

---

License: MIT-0, see [LICENSE](../../../LICENSE).
