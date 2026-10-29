# W3-16K benchmark method

How the W3-16K numbers in [docs/results](../../docs/results/README.md) were measured, how the method compares with
the NVIDIA Dynamo benchmarking rules, and how to run the same method with your own input length, output length and
rate.

## What this benchmark is and is not

It is our own method, built on the NVIDIA tools:

- **Load generator.** AIPerf 0.13.0 replays a frozen trace file in the `mooncake_trace` format with `--fixed-schedule`
  (open loop: every row is sent at its timestamp, whatever the latency of earlier rows), streaming, against the Dynamo
  Frontend of one cell. The exact command is `aiperf_cmd` in `tools/matrix_driver/matrix_driver.py`; it adds
  `--extra-inputs ignore_eos:true`, so every request decodes its full output length.
- **Workload.** The trace comes from `demo-bench trace w2` ([tools/demo_bench](../../tools/demo_bench)), a generic
  generator of agentic sessions (the name `w2` is historical). Sessions share a global prefix, each turn extends the
  hash ids of the previous turn of its session, and a share of the rows are cold ingestions with never-seen hash ids.
  AIPerf synthesizes the prompt text from the hash ids, so equal ids give equal content. The W3-16K shape, rates, cut
  and hashes are in [params.json](params.json); [regen.sh](regen.sh) rebuilds every trace offline.
- **Design.** An 8-cell ablation of three factors on one node: R (KV router instead of round-robin), O (SGLang HiCache)
  and D (prefill/decode disaggregation, 4 prefill + 4 decode workers). S0 has none of them and S7 has all three; the
  cell table is in [docs/results](../../docs/results/README.md). Every cell runs TP1 workers of `Qwen/Qwen3.8-27B-FP8`
  on one `p6-b200.48xlarge` (8 B200 GPUs).
- **SLO.** Absolute, fixed before any W3-16K data and not calibrated on S0: continuation TTFT ≤ 1.0 s, cold TTFT ≤
  2.0 s, per-request mean ITL ≤ 25 ms; errors count as misses and session entries are never judged. The headline
  metric is SLO goodput: turns per second inside the measured window that meet the SLO.
- **Driver.** The in-cluster driver `tools/matrix_driver/matrix_driver.py` runs a plan of
  [plans/w3-16k](../../plans/w3-16k) as a Kubernetes Job. Per run it deploys the cell and warms its engines (or
  flips only the router mode of a running topology), resets the caches and verifies the reset, replays the trace
  with AIPerf, takes Prometheus snapshots before and after, applies its validity rule and uploads everything
  append-only.

It is not:

- an NVIDIA recipe benchmark, nor output of the NVIDIA agent skills (we read them and compare below);
- a concurrency sweep or a capacity curve;
- a repeated measurement: each cell ran once per rate (n=1);
- a result for another model, GPU type, node count or workload, or for the image this sample ships (see Limits).

The rules compared below are the eight files of
[`agent-docs/rules/benchmarking`](https://github.com/ai-dynamo/dynamo/tree/934d09aaff2a4a4e7c2049ebfb8197a9d930e3b2/agent-docs/rules/benchmarking),
the three AIPerf skills of `.agents/skills` and the AIPerf guide of the Dynamo CLI docs, as of commit `934d09a`
(2026-10-17) of `github.com/ai-dynamo/dynamo`.

## Adherence to the NVIDIA Dynamo benchmarking rules

`followed` is claimed only where this repository shows it; the cited file is the evidence.

| Rule | Requirement | Status | Evidence or reason |
|---|---|---|---|
| `benchmark-isolation.md` | One benchmark per endpoint; no other GPU work on the serving GPUs | followed | Before each deploy and run the driver aborts unless its DGD is the only matrix DGD and no other namespace holds a GPU on the node (`assert_exclusive`), and preflight refuses while another driver Job is active (tools/matrix_driver/matrix_driver.py). |
| `benchmark-isolation.md` | Keep the AIPerf client off the serving GPU nodes when practical | deviates | The driver Job that runs AIPerf is pinned to the GPU node itself, CPU only, 4 to 8 cores (`nodeSelector` in tools/matrix_driver/render_driver.py). At 50 req/s S0 was invalid because the load generator's own services were reaped (README.md). |
| `benchmark-isolation.md` | Record the Job, endpoint, serving pods, GPU allocation and competing-work check | followed | Each `run.json` holds the GPU requests per namespace (`exclusive`), the GPU UUID per engine, the pods, and the AIPerf command with the Frontend URL (tools/matrix_driver/matrix_driver.py); the file is `run.json`, not `benchmark_execution.json`. |
| `comparison-uncertainty.md` | One measured run per candidate by default | followed | Every `run` line of plans/w3-16k has repetition 1. |
| `comparison-uncertainty.md` | Each measured run finishes in 30 minutes or less | deviates | The schedule is 615 s (`cut_ms` in workloads/w3-16k/params.json), but AIPerf waits up to `--request-timeout-seconds 7200` for the drain, so a saturated cell is bounded by the plan's hard stop, not by a 30-minute limit. |
| `comparison-uncertainty.md` | Noise-floor pilot (n=3) once per series; paired repetitions inside the noise band; a fresh confirmatory run of the finalist | deviates | n=1 per cell and rate: no pilot, no paired or confirmatory repetition, so no measured noise floor or minimum detectable effect (docs/results/README.md). Only wide-margin and categorical outcomes (holds, saturated, failed) are supported. |
| `comparison-uncertainty.md` | Controlled or randomized run order | deviates | S0 and S7 ran first, S1 to S6 a few hours later on the same image, traces and SLO; the order was fixed, so a time effect cannot be separated from the factors (docs/results/README.md). |
| `comparison-uncertainty.md` | Record neighbour occupancy with every measurement | deviates | The node is exclusive for GPUs (first row) and `run.json` records the GPU-holding pods per namespace, but not every CPU-only pod on the node. |
| `comparison-uncertainty.md` | No fleet or full-node projection without a co-located confirmation | deviates | Every cell runs on a single node of 8 B200 GPUs; nothing was measured on more nodes, so no result here projects to a fleet, another node count or another GPU shape. |
| `comparison-uncertainty.md` | Reset caches between points; rank on cold, identical content | followed | Before each run the driver flushes every engine and the HiCache storage, then requires 0 cached tokens on a repeated canary and 0 L3 files; a failed check fails the run (`reset_and_verify` in tools/matrix_driver/matrix_driver.py; S5 at 25 req/s in docs/results/README.md). |
| `comparison-uncertainty.md` | Confirm the finalist once at warm steady state before promising an absolute number | deviates | No separate warm run: each run starts from reset caches and the measured window [255, 615) s sits inside the same replay, after the entries. The absolute numbers are cold-start, 6-minute figures. |
| `concurrency-grid.md` | Bounded powers-of-two concurrency grid; at most 4x concurrency requests per point | not applicable | No concurrency series: every run replays a fixed schedule (`--fixed-schedule` in tools/matrix_driver/matrix_driver.py). |
| `concurrency-grid.md` | Choice of load points | deviates | Open loop at fixed request rates (10, 25, 32 and 50 req/s, chosen to bracket the knee of S0; README.md) instead of a concurrency grid: agentic turns arrive on their own clock whatever the server latency, so the question is SLO goodput at an arrival rate, and a closed-loop grid would slow the arrivals as latency grows and hide the queue. One AIPerf process per point, not a native AIPerf sweep. |
| `concurrency-grid.md` | Keep non-load inputs fixed; record planned and executed points, order and omissions | followed | Only `--sessions` and `--rate` change with the rate (workloads/w3-16k/regen.sh) and every plan of plans/w3-16k has the same SLO line; the outcome of each rate is in workloads/w3-16k/params.json, the order in the plan headers and every failed point in docs/results/README.md. |
| `evidence-eligibility.md` | Canonical AIPerf path; raw outputs unchanged and traceable | followed | AIPerf `profile --custom-dataset-type mooncake_trace --fixed-schedule` (`aiperf_cmd` in tools/matrix_driver/matrix_driver.py), uploaded append-only with `run.json` last (tools/matrix_driver/README.md). The raw exports are kept with the run artifacts, not in this repository. |
| `evidence-eligibility.md` | An audit marks the run valid before any claim | deviates | The driver's own validity rule replaces `benchmark_audit.json`: AIPerf exit 0, errors ≤ 0.5 % of the rows, no pod restarted, reset verified, not cut by the hard stop (`validity` in tools/matrix_driver/matrix_driver.py). Failed and invalid runs are kept and listed, never used for a gain (docs/results/README.md). |
| `evidence-eligibility.md` | Candidate identity recorded and matching what is claimed | deviates | The runs measured a derived SGLang 0.5.22 build on a Dynamo 1.6.0 nightly (not distributed); the sample ships the public SGLang 0.5.21 image (versions.env) with the same flags, and the published rows were not re-measured on it (docs/results/README.md). |
| `proxy-workload-selection.md` | Input order: user trace, user shape, then a Dynamo recipe proxy | deviates | A public sample has no user trace, and the question (KV routing, HiCache and PD on agentic sessions) needs a known prefix structure, so the input is our own synthetic trace from `demo-bench trace w2`, not a recipe proxy. The results are scoped to that workload. |
| `proxy-workload-selection.md` | Record source, path, sha256, selection reason and known mismatches; keep the input unchanged | followed | Generator, flags, seed, cut and the sha256 of every window and full trace per rate are in workloads/w3-16k/params.json and workloads/w3-16k/regen.sh rebuilds them; each plan pins `trace_sha256` with `frozen=yes` and the driver refuses a mismatch (`fetch_inputs` in tools/matrix_driver/matrix_driver.py). The mismatches are under Limits. |
| `result-storage.md` | Evidence layers under the deployment root (raw AIPerf, audit, summary, analysis) | deviates | Another layout: one results prefix per run with the AIPerf export, snapshots, evidence and `run.json` (tools/matrix_driver/README.md); the normalized per-cell table is docs/results/w3-16k-cells.csv; there are no performance-analysis files. |
| `result-storage.md` | Comparison history: series baseline, previous valid, best prior | not applicable | A fixed 2^3 factorial, not an iterative candidate loop: each cell is compared with S0 on the same trace at the same rate. |
| `series-boundaries.md` | Freeze content, schedule, load, endpoint, model, tokenizer, SLO and tool for the runs of one comparison | followed | At each rate the eight cells replay the same trace with the same tokenizer sums and SLO line: plans/w3-16k/plan-w3-r10.txt and plans/w3-16k/plan-w3-r10-cells.txt carry the same `w2`, `tokenizer` and `slo` lines (likewise at 25 and 32). |
| `series-boundaries.md` | No gain, loss or Pareto claim across series | followed | W3-16K is never pooled with the W2 runs (workloads/w3-16k/README.md, docs/results/README.md). |
| `tool-version.md` | One AIPerf version for every run of the series, recorded per run | followed | aiperf 0.13.0 is pinned in the bench image, whose build asserts the installed version (tools/bench-image/Dockerfile); the driver image is pinned by digest (manifests/track-q-qwen38/driver/images.env) and each `run.json` records `aiperf==0.13.0` (tools/matrix_driver/matrix_driver.py). |
| `tool-version.md` | Resolve the latest stable AIPerf at plan time; record the mechanism and the date in the plan | deviates | The plans do not record how or when 0.13.0 was chosen; the pin lives in the image, not in the plan. Whether 0.13.0 was the latest stable release on the run dates is not recorded here. |
| `configure-aiperf-benchmark` | Validate the trace (pairing, order, rows over the context); never filter or clip rows silently | followed | The driver checks trace and sidecar pairing, timestamp order and ISL + OSL + 256 ≤ 131,072 for every row and refuses the trace otherwise (`check_trace` in tools/matrix_driver/matrix_driver.py); the 615 s cut is part of the frozen trace (workloads/w3-16k/regen.sh), not a run-time filter. |
| `run-aiperf-benchmark` | Never lower the load or change the trace to make a failed run finish | followed | Failed runs were recorded and not retried with a lighter load: 50 req/s, S5 at 25 req/s, S3 and S5 at 32 req/s (docs/results/README.md); the plans run with `on_failure continue` (plans/w3-16k). |
| `analyze-aiperf-results` | Audit, summary and analysis files next to the raw outputs | deviates | The analysis behind docs/results/w3-16k-cells.csv (window goodput, TTFT, ITL, hit rate) is kept with the run artifacts, not in this repository; the driver's own goodput covers the whole profiling run, populate phase included. |
| `benchmarking-with-aiperf.mdx` | Change one deployment setting at a time on the same request shape and load | followed | Full 2^3 factorial of R, O and D on one trace and SLO per rate: S1, S2 and S3 each differ from S0 by one factor (cell table in docs/results/README.md). |

## How to run with your own ISL, OSL and rate

Three steps: generate a trace, cut it to the replay window, write a plan. The first two run offline, from the
repository root.

| Flag of `demo-bench trace w2` | Controls | W3-16K |
|---|---|---|
| `--entry-tokens` | ISL of turn 0, global prefix included | 5632 |
| `--growth-tokens` | Tokens added per turn: turn k has entry + k x growth | 1024 |
| `--turns` | Turns per session, entry included: max ISL = entry + (turns - 1) x growth | 11 |
| `--global-prefix-tokens` | Prefix shared by every session row (system prompt and tools) | 2048 |
| `--max-isl` | Largest allowed input: a last turn or a cold row above it is refused (exit 2, nothing written) | 16384 |
| `--cold-fraction`, `--cold-min-tokens`, `--cold-max-tokens` | Share of all rows that are cold ingestions, and their length range | 0.03, 15872, 15872 |
| `--osl` or `--osl-file` | A fixed output length, or a JSON list of lengths sampled per row | `--osl 500` |
| `--osl-cap` | Upper clip of every output length | 12000 (default) |
| `--rate` | Continuation turns per second | 10, 25, 32, 50 |
| `--sessions` | Live sessions: the mean inter-turn gap is sessions / rate, so sessions = gap x rate | 75 x rate |
| `--gap-sigma` | Lognormal sigma of the inter-turn gap | 0.3 |
| `--populate-s` | Seconds over which the session entries are spread | 180 |
| `--seed` | Random seed: the same seed and flags give the same bytes | 1111 |
| `--expect-sha256` | Refuses a trace whose sha256 differs (exit 1, nothing written) | `full_sha256` of params.json |
| `--block-size` | Tokens per hash block; every token size must be a multiple of it | 512 |

- **Fit.** max ISL + OSL + 256 must fit the served context: the shipped cells serve 131,072 tokens
  (`--context-length` in `manifests/track-q-qwen38/rendered/S0.yaml`) and the driver refuses a trace with a longer
  row. Keep `--block-size 512`: the driver's AIPerf command sets no block size of its own.
- **Window.** The generator has no window flag; the window is a cut you apply after it. Start the window one mean gap
  after the entries (`--populate-s` + gap), so it holds no entry, and end it no later than (turns - 1) x gap, when
  the earliest sessions end on average and the rate starts to decay. W3-16K: [255, 615) s, with 180 + 75 = 255 and 615 ≤ 10 x 75. Replay the
  rows with a timestamp below the end; `regen.sh` does this for the W3-16K rates. The offered rate in the window is
  the rate plus the cold rows.
- **SLO line of the plan.** `slo ttft_s=<s> itl_ms=<ms> cold_factor=<f> cold_prefill_tok_s=<tokens/s>`: `ttft_s`
  bounds the TTFT of continuation turns, `itl_ms` the mean ITL of every judged request, and a cold row must reach its
  first token within `cold_factor` x ISL / `cold_prefill_tok_s`. `cold_prefill_tok_s=probe` measures the rate
  instead and needs a `probe-cold-prefill <cell>` step before the first run. Fix the SLO before any data; W3-16K uses
  `slo ttft_s=1.0 itl_ms=25 cold_factor=1 cold_prefill_tok_s=7936`.

### Worked example

A 32,768-token target context and a 1,024-token output: `--max-isl` is 32,768 - 1,024 = 31,744. Sessions of 10 turns
share a 4,096-token prefix, enter with 8,192 tokens and grow by 2,560 tokens per turn, so the last turn has
8,192 + 9 x 2,560 = 31,232 tokens, within 31,744. 2 % of the rows are cold ingestions of 16,384 to 31,232 tokens.
4 continuation turns/s with a 60 s mean gap need 60 x 4 = 240 sessions.

```bash
uv run --project tools/demo_bench demo-bench trace w2 \
  --out /tmp/my-trace/my.jsonl --sidecar /tmp/my-trace/my.side.jsonl \
  --seed 7 --sessions 240 --rate 4 --turns 10 \
  --global-prefix-tokens 4096 --entry-tokens 8192 --growth-tokens 2560 \
  --gap-sigma 0.3 --populate-s 120 \
  --cold-fraction 0.02 --cold-min-tokens 16384 --cold-max-tokens 31232 \
  --osl 1024 --max-isl 31744 --block-size 512 \
  --expect-sha256 fd92af75c78b89befd7b00134b7a9af51d9646a81b4018afeb59a3e4d1d42656
```

It writes 2,449 rows. The entries end before 120 s, so the measured window is [180, 540) s: 120 + 60 = 180, and
540 = 9 x 60. The cut keeps 2,066 rows (timestamps below 540,000 ms):

```bash
cd /tmp/my-trace
cut=540000
k=$(awk -v cut="$cut" 'match($0, /"timestamp":[0-9]+/) { if (substr($0, RSTART + 12, RLENGTH - 12) + 0 >= cut) exit; n++ } END { print n + 0 }' my.jsonl)
head -n "$k" my.jsonl > my-window.jsonl
head -n "$k" my.side.jsonl > my-window.side.jsonl
sha256sum my-window.jsonl my-window.side.jsonl 2>/dev/null || shasum -a 256 my-window.jsonl my-window.side.jsonl
```

Upload both window files and save a plan as `plan-<name>.txt` in a plan directory. The header keyword is `w2`
whatever the trace; the driver refuses the trace unless both hashes match and `frozen=yes`. The `tokenizer` line
stays the one of the shipped plans, since the model does not change. With
`slo ttft_s=1.0 itl_ms=25 cold_factor=1 cold_prefill_tok_s=7808` the largest cold row (31,232 tokens) must start
within 4.0 s; the value 7808 is an example, not a measurement:

```text
run_id my-trace-r4
hard_stop 2026-12-01T00:00:00Z
on_failure continue
w2 trace=<URI of my-window.jsonl> trace_sha256=<its sha256> sidecar=<URI of my-window.side.jsonl> sidecar_sha256=<its sha256> frozen=yes
tokenizer prefix=<tokenizer prefix> sums_sha256=<sha256 of its SHA256SUMS>
slo ttft_s=1.0 itl_ms=25 cold_factor=1 cold_prefill_tok_s=7808
run S0 1
run S7 1
teardown
```

Render and launch it as in [docs/DEPLOY.md](../../docs/DEPLOY.md) section 7, adding `--plans-dir <plan directory>`
to both `render_driver.py` calls and launching `plan-<name>` instead of `plan-w3-r25`. The driver
replays the whole cut trace and its own goodput covers the whole run; compute window figures from the per-request
AIPerf export, pairing rows with the sidecar by trace line. The driver serves the model of the cells; only the trace,
the SLO and the cells are plan inputs.

## Limits

What the measured results cannot support:

- **Small differences.** n=1 per cell and rate, no noise floor: a difference of a few percent, or a ranking of cells
  that are that close, is not a finding. Only wide-margin and categorical outcomes (holds, saturated, failed) are.
- **Other setups.** One model (`Qwen/Qwen3.8-27B-FP8`, TP1 workers), one GPU type, a single node of 8 GPUs: nothing
  here transfers to other models, parallelism, node counts, multi-node PD or fleets.
- **The shipped image.** The numbers come from a derived SGLang 0.5.22 build; the public SGLang 0.5.21 image this
  sample ships was not benchmarked.
- **Real agent traffic.** The prompts are synthesized from hash ids, outputs have a fixed length with `ignore_eos`,
  and the schedule is open loop: in saturated cells the queue, and the cache reuse lost to it, are worse than a
  closed-loop agent would see. The shape is the W3-16K one; other prefix shares, turn counts or gaps can change the
  ranking of the cells.
- **Steady state.** Each run starts from reset caches (HiCache L3 included) and is judged over 6 minutes; a warm
  production steady state was not measured.
- **Causes.** The 50 req/s point is inconclusive, and the cause of the decode-worker restarts of S3 and S5 at 32 req/s
  is undetermined (docs/results/README.md). The client shares the GPU node, and the run order was fixed.
- **Re-audit.** The traces can be rebuilt from this repository; the raw AIPerf exports and the window analysis cannot,
  they are kept with the run artifacts.
