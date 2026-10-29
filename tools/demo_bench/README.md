# demo-bench

Stdlib-only Python CLI for the KubeCon NA 2026 Dynamo-on-EKS demo. It sizes KV caches, generates and schedules traces (single-turn, multi-turn and the W2 agentic trace), gates a W2 trace offline with a routed LRU replay, compares aiperf runs, computes the SLO goodput of a W2 run, queries Prometheus KPIs, resets worker caches, plans the factorial run matrix, computes matrix effects, plots closed-loop sweeps, probes HiCache restore equivalence (with a measured noise floor), applies the pre-registered image gate, and keeps a cost ledger against a budget.

Layout is hexagonal (`src/demo_bench/`):

| Layer | Contents |
|---|---|
| `domain/` | Pure logic and ports: KV sizing and LRU tier replay, run comparison, PromQL builders, scenarios, run window, `HttpClient` protocol. |
| `application/` | Use cases: `trace`, `trace_schedule`, `trace_multiturn`, `trace_w2`, `w2_gate`, `sizing`, `compare`, `collect_metrics`, `reset`, `matrix`, `schedule`, `sweep`, `validity` (the run validity rule shared with the matrix driver), `slo_goodput`, `restore_probe`, `image_gate` and `cost_ledger`. |
| `infrastructure/` | Adapters: urllib HTTP client, Prometheus client, aiperf and observations readers, JSON logging, sweep charts (matplotlib). |
| `cli.py` | argparse entry point; the only place that wires adapters to use cases. |

## Install and run

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/). Runtime has no dependencies.

```bash
cd tools/demo_bench
uv run demo-bench --help
uv run demo-bench <command> --help

# charts: add the optional extra (matplotlib)
uv run --extra plot demo-bench plot sweep ...
```

Global flag: `--log-level LEVEL` (default `INFO`, env `DEMO_BENCH_LOG_LEVEL`). Put it before the command.

Command output goes to stdout, or to `--out FILE` where available (parent dirs are created). Logs go to stderr.

## Commands

### trace

Generate a single-turn session-revisit trace (JSONL). `--sessions`, `--prefix-tokens`, `--passes`, `--seed` and `--out` are required. Prints `{path, rows, sha256}`.

| Flag | Default | Meaning |
|---|---|---|
| `--sessions N` | required | Sessions (shared prefixes) |
| `--prefix-tokens N` | required | Shared prefix length, tokens |
| `--passes N` | required | Passes, including the populate pass |
| `--seed N` | required | RNG seed |
| `--out PATH` | required | Output JSONL |
| `--block-size N` | 512 | Tokens per hash block |
| `--unique-tokens N` | 512 | Unique suffix per request |
| `--output-tokens N` | 64 | Output tokens per request |
| `--spacing-ms N` | 1000 | Timestamp step, ms |

```bash
uv run demo-bench trace --sessions 64 --prefix-tokens 8192 --passes 3 --seed 42 --out /tmp/trace.jsonl
```

### trace schedule

Add a deterministic `timestamp` (ms) to every row of an unscheduled trace, keeping each row's original bytes, so `aiperf --fixed-schedule` can replay it. Prints the result summary as JSON.

| Flag | Default | Meaning |
|---|---|---|
| `--in PATH` | required | Unscheduled trace |
| `--out PATH` | required | Scheduled trace to write |
| `--rate R` | required | Mean arrival rate, requests/s |
| `--seed N` | required | RNG seed (used by `poisson`) |
| `--arrival {poisson,uniform}` | `poisson` | Arrival process |
| `--duration SEC` | none | Keep rows with timestamp <= SEC*1000 (exclusive with `--rows`) |
| `--rows N` | none | Keep the first N rows (exclusive with `--duration`) |

```bash
uv run demo-bench trace schedule --in /tmp/trace.jsonl --out /tmp/scheduled.jsonl --rate 4 --seed 42 --duration 300
```

### trace multiturn

Generate the Track D multi-turn conversation trace (mooncake JSONL, already scheduled for `aiperf --fixed-schedule`). Prints `{path, rows, sha256}`.

- `--sessions` conversations x `--turns` turns. Every request starts with the same `--system-tokens` system prompt: its leading `hash_ids` blocks are identical in every row of every session.
- Turn 0 of a session has `--context-tokens` input tokens, system prompt included. Each turn adds `--growth-tokens`, so turn k has `context + k * growth` tokens. Its `hash_ids` extend turn k-1's exactly, so the previous turn is a reusable prefix. Sessions share only the system prompt.
- Rows carry only `timestamp` (ms), `input_length`, `output_length` and `hash_ids`. There is no `session_id`, `delay` or `messages`, otherwise aiperf would accumulate turns. The output length is fixed; `ignore_eos` is set by aiperf.
- Schedule: turn k of a session is sent at `phase + k * TURN_PERIOD_MS`. The phases are `TURN_PERIOD_MS * j // sessions` for `j = 0..sessions-1`, spread over one period and assigned to sessions by `--seed`. Rows are sorted by timestamp, ties by session. The `hash_ids` do not depend on the seed.
- Every token size must be a positive multiple of `--block-size`, and the context must be larger than the system prompt. Invalid input exits 2 and writes no file.

| Flag | Default | Meaning |
|---|---|---|
| `--out PATH` | required | Output JSONL (parent dirs are created) |
| `--seed N` | required | Session-to-phase assignment |
| `--turn-period-ms N` | required | Tp: time between two turns of a session, ms (calibrated by the G1 probe) |
| `--sessions N` | 40 | Conversations |
| `--turns N` | 8 | Turns per conversation |
| `--system-tokens N` | 2048 | Shared system prompt, tokens |
| `--context-tokens N` | 30720 | Turn-0 input, system prompt included, tokens |
| `--growth-tokens N` | 1536 | Tokens added per turn |
| `--output-tokens N` | 128 | Output tokens per request |
| `--block-size N` | 512 | Tokens per hash block (aiperf `--isl-block-size`) |

With the defaults, the input length runs from 30,720 to 41,472 tokens (mean 36,096) over 320 rows. Replay with `--custom-dataset-type mooncake_trace --fixed-schedule --isl-block-size 512`.

```bash
uv run demo-bench trace multiturn --out /tmp/mt.jsonl --seed 42 --turn-period-ms 135000
```

### trace w2

Generate the W2 agentic trace (mooncake JSONL, open loop: every row carries its `timestamp` in ms, for `aiperf --fixed-schedule`) and its sidecar. Nothing is model- or GPU-specific: every workload size is a flag, frozen after `w2-gate` and the boot-log re-gate.

- **Sessions:** `--sessions` x `--turns`. Every session row starts with the same `--global-prefix-tokens` blocks (system + tools). Turn 0 (`entry`) has `--entry-tokens` input tokens, the global prefix included; turn k has `entry + k * growth` and its `hash_ids` extend turn k-1's exactly. The rest of a session is private.
- **Arrivals:** entries are spread (seeded, uniform) over `[0, --populate-s)`. Each later turn comes a lognormal idle gap after the previous turn of its session: sigma `--gap-sigma`, mean `SESSIONS / RATE` seconds (so `--rate` is the aggregate continuation rate, turns/s), at least 1 ms.
- **Cold ingestions:** `round(f / (1 - f) * sessions * turns)` extra rows for `--cold-fraction f`, so they are `f` of all rows. Length: a seeded multiple of `--block-size` in `[--cold-min-tokens, --cold-max-tokens]`. Their `hash_ids` appear in no other row (not even the global prefix). Timestamps: uniform over the session span.
- **OSL:** `--osl N` for a fixed length, or `--osl-file` (a JSON list) to sample from, clipped at `--osl-cap`. To sample from the recipe perf trace (mooncake JSONL), extract its output lengths first:

  ```bash
  python3 -c 'import json,sys; print(json.dumps([json.loads(l)["output_length"] for l in open(sys.argv[1]) if l.strip()]))' \
    "$RECIPE_TRACE" > osl.json
  ```

- **Rows** have exactly `timestamp`, `input_length`, `output_length`, `hash_ids`, sorted by timestamp (ties: session rows by session, then cold rows). No `session_id`, `delay` or `messages`.
- **Sidecar:** one JSON line per row: `line`, `session` and `turn` (`null` for cold), `class` (`entry`, `continuation`, `cold_ingestion`) and `input_length`. `w2-gate` and `slo-goodput` read it.
- **Determinism:** same flags and `--seed`, same bytes. Each random quantity has its own seeded stream, so switching between `--osl` and `--osl-file` does not move the arrivals.
- **Freeze:** prints `{path, rows, sha256, sidecar, sidecar_sha256}` on one line. With `--expect-sha256 HEX`, a trace whose sha256 differs exits 1 and writes nothing. Invalid input (non-positive sizes, a size that is not a multiple of `--block-size`, an entry not larger than the global prefix, an input above `--max-isl`, `--cold-fraction` outside `[0, 1)`) exits 2 and writes nothing.

| Flag | Default | Meaning |
|---|---|---|
| `--out PATH` | required | Mooncake JSONL to write |
| `--sidecar PATH` | required | Sidecar JSONL to write |
| `--seed N` | required | RNG seed |
| `--sessions N` | required | Live sessions (`N_s`) |
| `--turns N` | required | Turns per session, entry included |
| `--global-prefix-tokens N` | required | Prefix shared by every session row |
| `--entry-tokens N` | required | Turn-0 input, global prefix included |
| `--growth-tokens N` | required | Tokens added per turn |
| `--rate X` | required | Aggregate continuation rate, turns/s |
| `--gap-sigma X` | 1.2 | Sigma of the lognormal gap (0: constant gaps) |
| `--populate-s X` | required | Window of the entries, seconds |
| `--cold-fraction X` | required | Share of all rows that are cold ingestions, in `[0, 1)` |
| `--cold-min-tokens N` | required | Smallest cold ingestion |
| `--cold-max-tokens N` | required | Largest cold ingestion |
| `--osl N` | one of the two | Fixed output length |
| `--osl-file PATH` | one of the two | JSON list of output lengths to sample from |
| `--osl-cap N` | 12000 | Upper clip of every output length |
| `--max-isl N` | 200000 | Largest allowed input (last turn, largest cold ingestion) |
| `--block-size N` | 512 | Tokens per hash block (aiperf `--isl-block-size`) |
| `--expect-sha256 HEX` | none | Frozen sha256 of the trace file |

```bash
uv run demo-bench trace w2 --out w2.jsonl --sidecar w2.side --seed 1111 --sessions 229 --turns 8 \
  --global-prefix-tokens 8192 --entry-tokens 65536 --growth-tokens 4096 --rate 0.5 \
  --populate-s 540 --cold-fraction 0.09 --cold-min-tokens 102400 --cold-max-tokens 153600 \
  --osl-file osl.json
```

### w2-gate

Offline routed-LRU gate of a W2 trace (plan 4.4), run before the freeze. The trace is replayed on `--targets` independent engines, each with a GPU tier of `--gpu-tokens-per-target` and an L2 tier of `--l2-tokens-per-target` (both from the boot log), under round-robin (`rr`) and kv-sticky (`kv`) routing.

- **Cache model:** a request hits the longest prefix of its `hash_ids` resident in GPU or L2 of its target. Afterwards all its blocks are in GPU with that request's recency. On overflow the least recently used blocks go first; among equal recency the deepest goes first, so a prefix survives. A block evicted from GPU moves to L2 (victim cache, same rule). `--l2-tokens-per-target 0` means no L2.
- **Routing:** `rr` sends row i to `i % targets`. `kv` puts a session on the target with the fewest sessions at its first row (ties: lowest index) and keeps it there; cold rows go round-robin on their own counter. The ceiling is one infinite cache, no routing.
- **Counting:** hit ratios are block weighted over the rows with `timestamp >= --warmup-s`; earlier rows are replayed but not counted. Revisits and overlapped turns use every row.
- **Per-pod distinct footprint** (critic finding): `footprint_tokens_per_target_rr` / `_kv` are the distinct blocks routed to each target, cold rows included. `footprint_over_l2_*` is the largest one over `--l2-tokens-per-target`. Under rr each pod sees most of the working set, so a working set below the sum of the L2 can still thrash every pod's L2.
- **Live states** (hybrid model checkpoints): a block is owned by a session when no other session's row contains it. `peak_live_states_gpu_*` / `peak_live_states_host_*` are, per target, the peak number of sessions that own a resident block in that tier. With `--max-mamba-gpu` / `--max-mamba-host`, the largest kv peak over the limit is checked.

Other metrics: `hit_gpu_rr`, `hit_gpu_kv`, `hit_l2_rr`, `hit_l2_kv`, `interaction_l2` (`hit_l2_kv - hit_l2_rr`), `revisit_same_target_rr` / `_kv`, `uncacheable_prefill_fraction` (counted rows with at least `--uncacheable-tokens` never-seen tokens), `overlapped_turn_fraction` (turns that start less than `--turn-duration-s` after the previous turn of the session), `working_set_tokens` (distinct blocks of the session rows), `w_over_sum_gpu` and `w_over_sum_l2`. A ratio over an L2 of 0 is `null` and its check fails.

Plan thresholds (bounds inclusive):

| Check | Metric | Passes if |
|---|---|---|
| `ceiling_single_cache` | `ceiling_hit` | >= 0.85 |
| `rr_gpu_only` | `hit_gpu_rr` | <= 0.40 |
| `kv_sticky_gpu_only` | `hit_gpu_kv` | 0.30 - 0.65 |
| `kv_sticky_plus_l2` | `hit_l2_kv` | >= 0.80 |
| `kv_minus_rr_with_l2` | `interaction_l2` | >= 0.15 |
| `rr_revisit_same_target` | `revisit_same_target_rr` | 1/targets +- 0.05 |
| `uncacheable_prefill` | `uncacheable_prefill_fraction` | >= 0.08 |
| `overlapped_turns` | `overlapped_turn_fraction` | <= 0.10 |
| `w_over_sum_gpu` | `w_over_sum_gpu` | 1.8 - 2.2 |
| `w_over_sum_l2` | `w_over_sum_l2` | <= 0.85 |
| `live_states_gpu` | `live_states_gpu_ratio` | <= 0.8 (only with `--max-mamba-gpu`) |
| `live_states_host` | `live_states_host_ratio` | <= 0.8 (only with `--max-mamba-host`) |
| `footprint_over_l2_rr` | `footprint_over_l2_rr` | >= 1.0, informational |
| `footprint_over_l2_kv` | `footprint_over_l2_kv` | <= 1.0, informational |

The informational footprint checks are reported for sizing and do not change the verdict. Run the gate once per topology (agg targets, PD prefill targets) with that topology's capacities. If it fails, change `--sessions`, `--gap-sigma` or `--growth-tokens` before the freeze, never after GPU data.

The report `{passed, params, metrics, checks}` goes to stdout, and also to `--out`. Each check has `name`, `metric`, `value`, `lo`, `hi`, `passed` and `informational`. Exit 0 when every non-informational check passes, 1 when one fails, 2 for invalid input (missing or malformed file, mismatched trace and sidecar, non-positive size).

| Flag | Default | Meaning |
|---|---|---|
| `--trace PATH` | required | W2 mooncake JSONL |
| `--sidecar PATH` | required | W2 sidecar JSONL |
| `--targets N` | required | Router targets (engines) |
| `--gpu-tokens-per-target N` | required | GPU KV per engine, tokens (boot log) |
| `--l2-tokens-per-target N` | required | L2 KV per engine, tokens (0: none) |
| `--turn-duration-s X` | required | Expected duration of one turn (overlap metric) |
| `--block-size N` | 512 | Tokens per hash block |
| `--warmup-s X` | 0 | Rows before this time are not counted (the populate) |
| `--uncacheable-tokens N` | 65536 | Never-seen tokens that make a prefill uncacheable |
| `--max-mamba-gpu N` | none | Mamba slots per engine on GPU (`max_mamba_cache_size`) |
| `--max-mamba-host N` | none | Mamba states per engine in the host pool |
| `--out PATH` | none | Also write the report here |

```bash
uv run demo-bench w2-gate --trace w2.jsonl --sidecar w2.side --targets 2 \
  --gpu-tokens-per-target "$GPU_TOKENS" --l2-tokens-per-target "$L2_TOKENS" --turn-duration-s 30 \
  --warmup-s 540 --max-mamba-gpu "$MAMBA_GPU" --max-mamba-host "$MAMBA_HOST" --out gate.json
```

### slo-goodput

SLO goodput of a W2 run (plan 4.5): SLO-met turns per second at the fixed rate. The profiling rows of aiperf's per-request `profile_export.jsonl` (warm-up excluded), sorted by `request_start_ns`, pair 1:1 with the sidecar in line order, as in [Run validity](#run-validity). Fewer rows than sidecar entries are accepted; more exit 2.

- `continuation`: met when TTFT <= `--ttft-s` and the request's mean ITL (`inter_token_latency`) <= `--itl-ms`.
- `cold_ingestion`: met when TTFT <= `--cold-factor` x `input_length` / `--cold-prefill-tok-s` (the cold prefill rate measured in the pilot) and ITL <= `--itl-ms`.
- `entry` turns are the populate and are never judged (`excluded_entry`).
- An errored request, or one without TTFT or ITL, misses. TTFT and ITL must be in ms in the export.
- **Goodput** = met / `--window-s`. The default window is the profiling span (last request end - first request start). When the populate is part of the profiling phase, pass the measured window.
- **Sensitivity grid:** the goodput and attainment per class for every `--itl-grid` x `--ttft-grid` pair (the cold rule keeps its factor; the ITL limit applies to both classes).
- **Bootstrap CI:** percentile bootstrap (`--n-boot`, `--seed`, `--alpha`) whose resampling unit is the session: all turns of a session move together, and each cold ingestion is its own unit.
- **Calibration** (S0 pilot): with `--accept-band LO,HI`, the overall attainment must lie in the band (inclusive), otherwise exit 1, so the pre-registered SLO is neither almost always missed nor almost always met.

The report goes to stdout, and also to `--out`: `thresholds`, `window_s`, `classes` (`n`, `met`, `attainment` per judged class), `excluded_entry`, `attainment`, `goodput`, `goodput_ci` ([lo, hi]), `bootstrap`, `sensitivity` and, with `--accept-band`, `calibration` (`attainment`, `band`, `in_band`). Exit 0; 1 when the attainment is outside `--accept-band`; 2 for invalid input (missing file or flag, unpaired rows, non-positive numbers).

| Flag | Default | Meaning |
|---|---|---|
| `--profile-export PATH` | required | aiperf `profile_export.jsonl` |
| `--sidecar PATH` | required | W2 sidecar JSONL of the replayed trace |
| `--cold-prefill-tok-s X` | required | Cold prefill rate measured in the pilot, tokens/s |
| `--window-s X` | profiling span | Measured window, seconds |
| `--ttft-s X` | 2 | Continuation TTFT limit, seconds |
| `--itl-ms X` | 25 | ITL limit, ms |
| `--cold-factor X` | 1.4 | Slack on the cold TTFT |
| `--itl-grid LIST` | `15,25,50` | ITL values of the grid, ms |
| `--ttft-grid LIST` | `1,2,4` | Continuation TTFT values of the grid, seconds |
| `--n-boot N` | 1000 | Bootstrap replicates |
| `--seed N` | 0 | Bootstrap seed |
| `--alpha X` | 0.05 | Two-sided CI level |
| `--accept-band LO,HI` | none | S0 calibration band of the attainment |
| `--out PATH` | none | Also write the report here |

```bash
uv run demo-bench slo-goodput --profile-export run/aiperf/profile_export.jsonl --sidecar w2.side \
  --cold-prefill-tok-s "$COLD_PREFILL_TOK_S" --window-s 1200 --accept-band 0.4,0.8 --out slo.json
```

### sizing

Per-rank KV capacity and expected LRU tier mix (device / host / storage / miss), as JSON. Needs either `--config` (dense/GQA models) or `--kv-bytes-per-token` (boot log, MLA), and either `--device-tokens` (boot log) or `--config` with `--hbm-gib`, `--mem-fraction` and `--weights-gib`. `--trace` needs `--prefix-tokens`.

| Flag | Default | Meaning |
|---|---|---|
| `--config PATH` | none | Model `config.json` (must carry `head_dim`) |
| `--kv-dtype T` | `fp8_e4m3` | KV-cache dtype (`fp8_e4m3`, `fp8_e5m2`, `bfloat16`, `float16`) |
| `--hbm-gib X` | none | HBM per GPU, GiB |
| `--mem-fraction X` | none | `--mem-fraction-static` |
| `--weights-gib X` | none | Weights per GPU, GiB |
| `--overhead-gib X` | 0.0 | Other static GiB per GPU |
| `--kv-bytes-per-token N` | none | Override, from the boot log |
| `--device-tokens N` | none | Device pool per rank, from the boot log |
| `--hicache-size-gb X` | 0.0 | `--hicache-size` per rank |
| `--storage-tokens N` | 0 | L3 budget per rank, tokens |
| `--write-back` | off | L2 does not mirror L1 (default is write-through) |
| `--session-tokens N` | none | Session length, for sessions/rank |
| `--trace PATH` | none | Trace JSONL to replay through the LRU tiers |
| `--prefix-tokens N` | none | Shared prefix length of `--trace` |
| `--block-size N` | 512 | Tokens per hash block |
| `--out PATH` | stdout | Output file |

```bash
uv run demo-bench sizing --kv-bytes-per-token 70000 --device-tokens 400000 \
  --hicache-size-gb 64 --trace /tmp/trace.jsonl --prefix-tokens 8192
```

### compare

A/B table of two aiperf `profile_export_aiperf.json` files. Logs a warning if the aiperf versions differ. The error count of each side is the `error_summary` count plus, for a run directory (`<run>/aiperf/profile_export_aiperf.json` with `<run>/aiperf/profile_export.jsonl` and `<run>/scheduled-trace.jsonl`), its collapsed outputs (see [Run validity](#run-validity)).

| Flag | Default | Meaning |
|---|---|---|
| `--baseline PATH` | required | Baseline export |
| `--candidate PATH` | required | Candidate export |
| `--baseline-label S` | `baseline` | Label |
| `--candidate-label S` | `candidate` | Label |
| `--metrics LIST` | `time_to_first_token,inter_token_latency,request_latency,request_throughput,output_token_throughput,goodput` | Comma list |
| `--format {markdown,json}` | `markdown` | Output format |
| `--out PATH` | stdout | Output file |

```bash
uv run demo-bench compare --baseline s0/profile_export_aiperf.json --candidate s1/profile_export_aiperf.json \
  --baseline-label S0 --candidate-label S1
```

### metrics

Engine hit rate, router predicted hit and tier token mix from Prometheus, over the aiperf profiling window (warmup rows excluded). Absent data is `null`, never 0.

| Flag | Default | Meaning |
|---|---|---|
| `--namespace NS` | required | DGD namespace |
| `--profile-export PATH` | required | Per-request `profile_export.jsonl` |
| `--prometheus-url URL` | env `PROMETHEUS_URL` | Prometheus base URL |
| `--timeout-s X` | 30.0 | HTTP timeout |
| `--out PATH` | stdout | Output file |

```bash
export PROMETHEUS_URL=http://prometheus.example:9090
uv run demo-bench metrics --namespace my-dgd-ns --profile-export artifacts/profile_export.jsonl
```

### reset

Flush caches on every worker pod (`POST /engine/flush_cache`; HiCache pods also get `/engine/clear_hicache_storage`). A busy engine is retried with exponential backoff. At least one pod is required; both flags repeat. The JSON record is always emitted, including on failure.

| Flag | Default | Meaning |
|---|---|---|
| `--pod NAME=HOST[:PORT]` | none | Worker pod (repeatable) |
| `--hicache-pod NAME=HOST[:PORT]` | none | HiCache worker pod, also clears L3 (repeatable) |
| `--system-port N` | 9090 | Port when a pod spec has none |
| `--max-attempts N` | 6 | Attempts per pod while busy |
| `--backoff-initial-s X` | 1.0 | First backoff |
| `--backoff-factor X` | 2.0 | Backoff multiplier |
| `--max-wait-s X` | 60.0 | Backoff cap |
| `--timeout-s X` | 30.0 | HTTP timeout |
| `--out PATH` | stdout | Output file |

```bash
uv run demo-bench reset --pod worker-0=10.0.0.1 --hicache-pod worker-1=10.0.0.2:9090
```

### matrix

Factorial effects R, O, D, RO, RD, OD, ROD on the log scale with percentile-bootstrap CIs (resampling rounds). The CSV needs `round`, the KPI column, and either `scenario` (`S0`..`S7`) or `r`,`o`,`d` (0/1). KPI values must be > 0 and every cell must be present.

| Flag | Default | Meaning |
|---|---|---|
| `--observations PATH` | required | CSV, one row per run |
| `--kpi NAME` | required | KPI column |
| `--n-boot N` | 1000 | Bootstrap resamples |
| `--seed N` | 0 | RNG seed |
| `--alpha X` | 0.05 | CI level is 1 - alpha |
| `--format {markdown,json}` | `markdown` | Output format |
| `--out PATH` | stdout | Output file |

```bash
uv run demo-bench matrix --observations runs.csv --kpi ttft_p50_ms --format json
```

### schedule

Print the planned matrix runs, one per line (TSV: round, topology, router, scenario, `deploy`/`toggle`, namespace). Deterministic.

| Flag | Default | Meaning |
|---|---|---|
| `--rounds N` | 3 | Rounds |
| `--topologies LIST` | all four | Comma list from `t-agg,t-agg-hic,t-dis,t-dis-hic`, for a chained subset plan |
| `--sweep-concurrency LIST` | none | Comma list of integers: closed-loop sweep plan, adds a 7th TSV column `concurrency=<N>` |
| `--format {tsv,json}` | `tsv` | Output format |
| `--out PATH` | stdout | Output file |

```bash
uv run demo-bench schedule --rounds 3
uv run demo-bench schedule --rounds 1 --topologies t-agg,t-agg-hic --sweep-concurrency 1,4,8,16,32,64,128
```

### plot sweep

TTFT, ITL and throughput (and TTFT speedup) vs concurrency from closed-loop sweep runs. Needs the `plot` extra; without it the command logs an error that names `uv sync --extra plot` (run from `tools/demo_bench`; the package is not on PyPI) and exits 1. Writes PNG/SVG charts plus `sweep_data.json` to `--out` and prints a summary (`files`, `missing_points`, `excluded_runs`).

Invalid runs are excluded and listed in `sweep_data.json` (`excluded`, with a reason): aiperf exit code != 0, pods restarted, missing or cancelled export, and collapsed outputs above 0.5 % of the requests when the run directory has `aiperf/profile_export.jsonl` and `scheduled-trace.jsonl` (see [Run validity](#run-validity)). A `--pairs` pair whose baseline or candidate has no valid run (never ran, unknown id, every run excluded) is dropped with a warning and left out of `pairs`, `speedup` and the speedup chart; the other pairs are still plotted, and with no pair left there is no speedup chart. A malformed list, a pair with the same scenario twice and a duplicate pair still exit 2.

| Flag | Default | Meaning |
|---|---|---|
| `--runs DIR` | required | Directory searched for `run.json` files |
| `--out DIR` | required | Output directory |
| `--setup LABEL` | required | Setup label printed on every chart |
| `--pairs LIST` | none | Comma list `BASELINE:CANDIDATE` (e.g. `S0:S1,S0:S2`) for the speedup chart |

```bash
uv run --extra plot demo-bench plot sweep --runs results/ --out charts/ --setup "t-agg, 1x node" --pairs S0:S1,S0:S2
```

### restore-probe

HiCache restore equivalence (Track D, study §4.5). For every prompt, one request at a time against `POST BASE_URL/v1/completions` (temperature 0, `logprobs`):

1. `cold`;
2. `gpu`: the same prompt again;
3. every `--filler` prompt once, to push it out of GPU memory;
4. `host`: the same prompt again;
5. with `--storage-filler`: every storage filler once, then `storage`.

A prompt passes when all of these hold:

- the `gpu` stage has `usage.prompt_tokens_details.cached_tokens > 0` (otherwise no cache engaged);
- `host` (and `storage`) report the same `cached_tokens` as `gpu`;
- they generate the same number of tokens;
- every top-1 logprob (the max of `choices[0].logprobs.top_logprobs[i]`) is within `--tolerance` of the `gpu` one.

Greedy text is not compared: greedy output can change with the prefix-hit length even without HiCache (SGLang #43055).

The probe cannot see which tier served a request. Size the fillers for the tier you want (study: about 1.5x the GPU KV pool for the host stage, more than L2 for the storage stage), and confirm the tier on the `cache_source` counters.

**Noise floor.** A cached and a cold path can legitimately differ by more than 1e-3 (another chunking or kernel), which would fail both images of the image gate. With `--flush-url`, the probe first measures its own floor on the probe prompts, strictly one request at a time:

1. per prompt, cold vs cold: flush, cold A, flush, cold B; the control is the largest `|top1_A - top1_B|`;
2. per prompt, GPU hit vs cold: flush, cold C, the same prompt again; the control is the largest `|top1_gpu - top1_C|`;
3. `floor` = the largest value of both controls; the tolerance becomes `max(1e-3, k x floor)`, more precisely `max(--tolerance, --noise-k x floor)`;
4. the caches are flushed once more, then the probe runs with that tolerance.

A request that follows a flush and still reports `cached_tokens > 0` (the flush did not empty the caches), or a GPU hit with `cached_tokens == 0`, fails the probe (exit 1). Each `--flush-url` is POSTed with `{}` in the order given; a non-200 answer fails the probe. Repeat the flag when one endpoint does not clear every tier (e.g. the worker's `flush_cache`, which keeps L3, then `clear_hicache_storage`). A real corruption stays far above `k x floor`. Both pass and both fail are explicit outcomes of the [image gate](#image-gate).

Each input file is a non-empty JSON list of non-empty strings. The report goes to stdout, and also to `--out` when given, as `{total, passed, ok, tolerance, model, stages, max_tokens, top_logprobs, prompts}`, plus `noise_floor` (`cold_vs_cold`, `gpu_vs_cold`, `floor`, `k`, `base_tolerance`, `effective_tolerance`) when it was measured. Each `prompts` entry has `index`, `ok`, `reasons`, `cached_tokens` per stage, and `max_logprob_diff` per compared stage.

| Flag | Default | Meaning |
|---|---|---|
| `--base-url URL` | required | OpenAI-compatible endpoint (e.g. the Dynamo frontend) |
| `--model NAME` | required | Model name sent in every request |
| `--prompts PATH` | required | JSON list: probe prompts (study: 16 prompts of ~30K tokens) |
| `--filler PATH` | required | JSON list: evicts each prompt from GPU before the `host` stage |
| `--storage-filler PATH` | none | JSON list: evicts it from host memory before the `storage` stage |
| `--max-tokens N` | 256 | `max_tokens` of every request (fillers included) |
| `--top-logprobs N` | 5 | `logprobs` of every request |
| `--tolerance X` | 1e-3 | Max top-1 logprob difference (the base of the noise floor rule) |
| `--timeout-s X` | 300.0 | Per-request timeout |
| `--flush-url URL` | none | Cache flush endpoint, POSTed with `{}` (repeatable); measures the noise floor first |
| `--noise-k K` | 3.0 | Multiplier of the floor; needs `--flush-url` (exit 2 without it) |
| `--out PATH` | none | Also write the report here |

Exit 0 when every prompt passes. Exit 1 when a prompt fails, when a flush fails or does not empty the caches, or when the endpoint answers non-200, returns no logprobs, or is unreachable. Exit 2 for invalid input.

```bash
uv run demo-bench restore-probe --base-url "$FRONTEND_URL" --model "$SERVED_MODEL_NAME" \
  --prompts probes.json --filler filler-l1.json --storage-filler filler-l2.json \
  --max-tokens 64 --top-logprobs 1 \
  --flush-url "$WORKER_SYSTEM_URL/engine/flush_cache" \
  --flush-url "$WORKER_SYSTEM_URL/engine/clear_hicache_storage" \
  --out restore-probe.json
```

### image-gate

Pre-registered image choice (G-IMG) from two `restore-probe --out` reports, both run with the stages cold, gpu, host and storage on 16 prompts: the derived image and the dev.1 control. A report passes the restore gate iff its `ok` is true (16/16). The derived image is chosen for every cell iff it passes the restore gate and `--accept-length-ratio` >= 0.85 and `--prefill-ratio` >= 0.8 (both inclusive, measured derived / dev.1); otherwise dev.1 is chosen.

| `state` | Restore gate | Meaning |
|---|---|---|
| `derived_only_pass` | derived passes, dev.1 fails | Expected: the probe told corruption from no corruption (`discriminating`) |
| `both_pass` | both pass | No corruption to detect on this pair: the probe was not discriminating |
| `control_only_pass` | dev.1 passes, derived fails | Unexpected: dev.1 is chosen and HiCache stays valid with it |
| `both_fail` | both fail | HiCache (O) is out of the matrix; check the probe's noise floor first |

The decision has `state`, `image` (`derived` or `dev1`), `o_valid` (HiCache may be measured: the chosen image passed the restore gate), `discriminating` (the two images disagree), `reasons`, the two verdicts, the two ratios and the thresholds. It goes to stdout, and also to `--out`. Exit 0 whatever the decision; 2 for invalid input (missing or malformed report, fewer than 16 prompts, no `host` or `storage` stage).

| Flag | Default | Meaning |
|---|---|---|
| `--derived PATH` | required | Probe report of the derived image |
| `--control PATH` | required | Probe report of the dev.1 control |
| `--accept-length-ratio X` | required | Speculative accept length, derived / dev.1 |
| `--prefill-ratio X` | required | Cold prefill throughput at 48K, derived / dev.1 |
| `--out PATH` | none | Also write the decision here |

```bash
uv run demo-bench image-gate --derived probe-derived.json --control probe-dev1.json \
  --accept-length-ratio 0.97 --prefill-ratio 1.02 --out image-gate.json
```

### cost-ledger

Spend of one NodePool plus its ODCRs against a budget, from a `kubectl get nodeclaims -o json` snapshot. It needs no cluster access, only the files.

- **NodeClaims:** only claims whose `karpenter.sh/nodepool` label equals `--nodepool` count. A claim is billed from `metadata.creationTimestamp` to its `deletionTimestamp` (when set and in the past), else to `--now`. The price is the `--prices` entry for its `node.kubernetes.io/instance-type` label. A claim created after `--now` costs 0.
- **ODCRs:** an ODCR bills `hourly_usd` from `created_at` to the earlier of `--now` and `end_at`, used or not, never negative.
- **Status:** `alert` from `--warn-fraction` of the budget; `exceeded` from 100 %.
- **Errors, never a silent 0:** a counted claim without an instance-type label, or without a price, exits 2 and names it.

| Flag | Default | Meaning |
|---|---|---|
| `--nodeclaims PATH` | required | `kubectl get nodeclaims -o json` output |
| `--nodepool NAME` | required | NodePool whose claims count |
| `--prices PATH` | required | JSON object: instance type -> USD/h, EKS Auto Mode fee included |
| `--budget-usd X` | required | Budget, USD (> 0) |
| `--odcr PATH` | none | JSON list of `{"id", "hourly_usd", "created_at", "end_at"?}` (RFC 3339) |
| `--now ISO` | current time | End of the billing window, RFC 3339 with offset |
| `--warn-fraction X` | 0.8 | Alert threshold, in (0, 1] |
| `--out PATH` | none | Also write the ledger here |

The ledger is printed to stdout, and also written to `--out` when given. It has `nodeclaim_usd`, `odcr_usd`, `total_usd`, `node_hours`, `budget_usd`, `fraction`, `status` (`ok`/`alert`/`exceeded`), `alert`, `warn_fraction`, `now`, `claims` and `odcrs`. Exit codes:

- 0 for `ok` and `alert`. An `alert` also logs a line containing `ALERT` on stderr.
- 1 for `exceeded`: stop the experiment and ask the operator.
- 2 for invalid input.

Limits:

- A snapshot lists only live NodeClaims, so a claim that is already gone (consolidated, deleted) no longer counts. Keep every snapshot or ledger, or take them often.
- The ODCR cost already covers the EC2 part of instances that run inside it. To avoid counting it twice, price those NodeClaims at the Auto Mode fee only. The full On-Demand price over-counts, which errs on the safe side.

```bash
kubectl get nodeclaims -o json > nodeclaims.json
uv run demo-bench cost-ledger --nodeclaims nodeclaims.json --nodepool gpu-b200-cb \
  --prices prices.json --odcr odcrs.json --budget-usd 2000 --out ledger.json
```

## Run validity

`application.validity` is the one definition of the run error rule (spec §13.5: errors ≤ 0.5 % of the requests; exactly 0.5 % is valid). P-37 counts a collapsed output as an error: a response with ≤ 1 output token while its request asked for more. An engine that aborts a request (e.g. an input longer than the KV pool) answers with one token and aiperf records a success with an `OSL mismatch` warning, so `error_summary` stays empty.

- Requested OSL: `output_length` of the run's `scheduled-trace.jsonl` (aiperf sends `ignore_eos: true`). Observed: `metrics.output_sequence_length.value` (fallback `output_token_count`) of `aiperf/profile_export.jsonl`.
- Only `benchmark_phase == "profiling"` rows count. Ordered by `metadata.request_start_ns`, they pair 1:1 with the trace rows in file order; more profiling rows than trace rows is an error. A row that already carries an aiperf `error` is counted once, as that error.
- Users: `plot sweep` (excludes the run), `compare` (adds to the error count) and the matrix driver (`tools/matrix_driver/matrix_driver.py`, `run_validity`: the run is invalid above 0.5 %).
- Closed-loop runs replay the raw trace and write no `scheduled-trace.jsonl`, so the check does not apply to them.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | OK |
| 1 | Operation failed (Prometheus query error, cache reset failed, missing `plot` extra, restore probe failed or endpoint error, cost ledger `exceeded`, `trace w2` sha256 differs from `--expect-sha256`, `w2-gate` check failed, `slo-goodput` attainment outside `--accept-band`) |
| 2 | Invalid input (`ValueError` or `OSError`: bad flags combination, malformed or missing file) |

argparse usage errors also exit 2.

## Logging and environment

- Logs are structured JSON, one object per line, on stderr. Stdout carries only command output, so it is safe to pipe.
- `DEMO_BENCH_LOG_LEVEL`: default for `--log-level` (default `INFO`).
- `PROMETHEUS_URL`: default for `metrics --prometheus-url`. No endpoint is baked into the code.

## Tests and quality gates

```bash
cd tools/demo_bench
uv run ruff check
uv run mypy                # strict, configured in pyproject.toml
uv run pytest
```

Tests never touch the network, boto3 or a cluster. See [`tests/README.md`](tests/README.md) for the per-module contract.
