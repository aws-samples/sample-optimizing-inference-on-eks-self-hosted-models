# Results: W3-16K, all eight cells at 10, 25 and 32 req/s

> **W3-16K, n=1. Not results of record.** One run per cell and rate: no noise floor, no
> confidence interval, no minimum detectable effect. Treat differences of a few points or a few percent as noise.
> Never pool these rows with the W2 runs of record (100K context, 0.5 req/s, n=3, pre-registered).

These files are the per-cell table behind the numbers quoted in the optimization guides
([KV router](../optimizations/kv-router/README.md), [HiCache](../optimizations/hicache/README.md),
[PD disaggregation](../optimizations/pd-disaggregation/README.md)).

| File | Content |
|---|---|
| `w3-16k-cells.csv` | One row per (rate, cell): 24 rows, rates 10, 25, 32 and cells S0 to S7. No prompt or response text. |
| `w3-16k-per-minute.csv` | Continuation TTFT and SLO attainment per minute of the measured window, per (rate, cell) that finished. |

The values are copied verbatim, string for string, from the offline analysis of the run exports (its constant
run-label column is left out). That analysis and the raw exports are kept with the run artifacts, outside this
repository. `tests/scripts/test_109_docs_track_q.py` checks that every figure the guides quote for W3 comes from
`w3-16k-cells.csv`.

## What ran

- **Stack (runs of record).** Track Q: `Qwen/Qwen3.8-27B-FP8` at TP1 on one `p6-b200.48xlarge` (8x B200), measured
  with a derived SGLang 0.5.22 build on a Dynamo 1.6.0 nightly (not distributed), cells from
  `manifests/track-q-qwen38/rendered/`. Those cells now run the public image `nvcr.io/nvidia/ai-dynamo/sglang-runtime:1.6.0-efa`
  (SGLang 0.5.21, Dynamo 1.6.0) with the same flags; the rows below were not re-measured on it.
- **Cells.** R = KV router (`DYN_ROUTER_MODE=kv` instead of round-robin), O = SGLang HiCache (L2 host RAM + L3 local
  NVMe via NIXL), D = prefill/decode disaggregation (4 prefill + 4 decode TP1 workers over NIXL LIBFABRIC on EFA).
  The aggregated cells run 8 TP1 workers.

  | cell | R | O | D | topology |
  |---|---|---|---|---|
  | S0 | off | off | off | aggregated 8x TP1, round-robin |
  | S1 | on | off | off | aggregated 8x TP1 |
  | S2 | off | on | off | aggregated 8x TP1, HiCache 200 GB per engine |
  | S3 | off | off | on | PD 4P4D, round-robin |
  | S4 | on | on | off | aggregated 8x TP1, HiCache 200 GB per engine |
  | S5 | on | off | on | PD 4P4D |
  | S6 | off | on | on | PD 4P4D, HiCache 400 GB per prefill engine |
  | S7 | on | on | on | PD 4P4D, HiCache 400 GB per prefill engine |

- **Workload W3-16K** ([`workloads/w3-16k`](../../workloads/w3-16k/README.md)). Agentic multi-turn sessions of 11
  turns, at most 16,384 tokens of context per request (ISL 5,632 + 1,024 per turn, OSL 500), 3% cold rows of
  15,872 tokens, a mean gap of 75 s between the turns of a session. Session entries arrive in the first 180 s; the
  measured window is [255, 615) s of the trace clock. Offered rates in the window: 10.2, 25.5 and 32.6 req/s.
- **Absolute SLO, fixed before any W3 data** (not calibrated on S0): continuation TTFT ≤ 1 s, cold TTFT ≤ 2 s,
  mean ITL per request ≤ 25 ms. Errors count as misses.
- **Order.** S0 and S7 ran first, in their own plans; S1 to S6 ran a few hours later on the same image, traces and
  SLO. A block effect cannot be separated from the factors.
- **Open loop.** A turn is sent on schedule even if the previous turn of its session has not finished. In
  saturated cells the queue, and the cache reuse lost to it, are worse than a closed-loop agent would see.

## Failures and exclusions (stated, not hidden)

| rate | cell | what happened | data |
|---|---|---|---|
| 50 | S0, S7 | **r50 is inconclusive and excluded.** S0: the load generator exited when its own services were reaped at 50 req/s, a harness limit and not a server result. S7: an engine pod went NotReady under load; the cause was not captured. Not retried. | none in these files |
| 25 | S5 | **Failed the pre-load reset check (T-RTR-2, `repeat_cached_zero`), so no load ran.** On the post-flush repeat the engines counted 2,048 cached tokens while every client response reported 0. It ran on the PD deployment kept from S3, which had just finished its own r25 run with 0 pod restarts. | `status=failed`, no metrics |
| 32 | S3, S5 | **The PD decode workers crashed under load** (PD without HiCache): the step failed with "not every engine pod in s-dis is Ready" after the replay started; the decode pods restarted with last-terminated reason Error, not OOMKilled (read from the Prometheus history during the block, not re-checked offline). **The cause is undetermined.** At r25 S3 ran clean with its decode KV at 0.96 of capacity, and at r32 S6 and S7 also reached 0.96 without restarts, so a full decode KV alone does not explain the crash (INFERRED). | `status=failed`, no metrics |

## Columns of `w3-16k-cells.csv`

| Column | Meaning |
|---|---|
| `rate`, `cell`, `R`, `O`, `D` | Nominal rate (req/s), cell, factor levels (1 = on). |
| `status`, `valid`, `failure_stage`, `failure_reason`, `errors` | Driver step outcome; a failed step has no metrics. |
| `goodput_window_turns_s` | Turns per second inside the window that meet the SLO. |
| `attainment_window` | Share of judged turns in the window that meet the SLO (0-1). |
| `cont_ttft_p50_s`, `cont_ttft_p90_s`, `cont_ttft_p99_s` | TTFT of the continuation turns (turn 2 and later of a session), window, seconds. |
| `entry_ttft_p50_s` | TTFT of the session entries, seconds. |
| `itl_req_p50_ms` | Median over requests of the mean ITL of each request, ms. This is the "ITL" the guides quote. |
| `itl_req_share_le_25ms` | Share of requests whose mean ITL is ≤ 25 ms. |
| `itl_token_p50_ms` | Per-token ITL median, rows with one token per SSE chunk only; lower than `itl_req_p50_ms` under load. Do not quote it as the request ITL. |
| `hit_window_client` | Cached share of the prompt tokens in the window, from the client usage. |
| `hit_engine_full_run`, `hit_gpu_full_run`, `hit_l2_full_run`, `hit_l3_full_run` | Engine hit for the whole run (populate included) and its split by tier: GPU, host L2, storage L3 (page cache or NVMe). |
| `moved_share` | Share of continuations served by a different worker (agg) or prefill worker (PD) than the previous turn of their session. Round-robin chance is 7/8 on agg and 3/4 on PD. |
| `offered_req_s`, `completed_req_s` | Requests per second offered and completed in the window. |
| `goodput_driver_turns_s`, `attainment_driver` | The driver's own goodput over the whole profiling run (populate included, rows paired by start order). The window columns pair rows by trace line and are the ones to quote. |
| `decode_kv_peak_over_cap` | PD cells only: peak decode KV use over the decode KV capacity (2,100,224 tokens per engine). |
| `prefill_queue_max` | Largest prefill queue seen on any engine. |
| `regime` | `holds` (meets the SLO), `flat, below the SLO` (stable but misses it) or `saturated` (TTFT rising over the window). |
| `topology`, `topology_name`, `router`, `run_id` | The topology names of the runs (`t-pd` and `t-pd-hic` are the repo's `t-dis` and `t-dis-hic`), the router mode and the run ID. |

`w3-16k-per-minute.csv`: `minute` 1 to 6 of the window (`lo_s`, `hi_s`), `continuations` judged in that minute,
`ttft_p50_s`, `ttft_p90_s`, `attainment` and `share_lt_1s` (share of continuations with TTFT below 1 s).
