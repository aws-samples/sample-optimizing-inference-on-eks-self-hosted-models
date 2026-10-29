# W3-16K workload (n=1)

W3-16K is an agentic multi-turn workload with at most 16,384 tokens of context per request, short outputs and
request rates that bracket the knee of the baseline cell. It was defined after the W2 runs of record, and its shape
and SLO were fixed before any W3 data.

Provenance: n=1 per cell and rate; a different workload from W2, never pooled with it, so no W3-16K run changes a W2
result of record. Each cell ran once per rate, measured over the window [255, 615) s of the replay, against an
absolute SLO fixed before any W3 data. Its runs carry run_id `q38-w3-*` and `label=w3-16k`.

This folder makes the workload reproducible from the repo:

| file | content |
|---|---|
| `params.json` | generator shape, session rule, cut, measured window, recorded sha256 per rate, cells, absolute SLO, outcome per rate |
| `regen.sh` | regenerates the window trace and sidecar of one rate, plus a `SHA256SUMS` (offline) |
| `upload-inputs.sh` | uploads the trace files of one rate and the tokenizer files to the S3 locations its plan reads |

## How it works

The load is a fixed, open-loop trace: every request has a timestamp and is sent at that instant whatever the
latency of the server, so a slow cell does not slow the load down. `demo-bench trace w2` generates it (the subcommand
name is historical: it is the generic agentic-session generator, and W3-16K only passes it other flags, the ones in
`params.json`). `regen.sh` cuts it at 615 s, `upload-inputs.sh` puts it where the plans in `plans/w3-16k` read it,
and the matrix driver replays it with AIPerf `--fixed-schedule`: the same rows at the same instants for every cell.

## Regenerate a trace

```bash
workloads/w3-16k/regen.sh 10 /tmp/w3-16k            # writes w3-16k-r10.jsonl, w3-16k-r10.side.jsonl, SHA256SUMS
workloads/w3-16k/regen.sh --dry-run 32 /tmp/w3-16k  # prints the generator command only, writes nothing
```

- The generator is the existing `demo-bench trace w2` of [`tools/demo_bench`](../../tools/demo_bench), unchanged.
  `regen.sh` runs `uv run --no-dev --project tools/demo_bench demo-bench` by default (`--no-dev`: the trace needs
  only the standard library, so uv skips the dev dependencies of `tools/demo_bench`); set `DEMO_BENCH` to use another
  command (it is word-split), e.g. `DEMO_BENCH="python3 -m demo_bench"` with `PYTHONPATH=tools/demo_bench/src`.
- The generator writes the full trace in a scratch directory. `regen.sh` keeps the first K rows with
  `timestamp < 615000` ms (head -n K of the trace and of the sidecar, so the original bytes and `line` values are
  kept), then writes the three files to the output directory. The scratch directory is always removed.
- The `SHA256SUMS` it writes must equal the window hashes of that rate in `params.json` (and the table below).
  The full-trace hashes (`full_sha256`, `full_sidecar_sha256`) are there too, for anyone who runs the dry-run
  command by hand.
- Offline: no network and no AWS. Rates outside 10, 25, 32 and 50 exit 2 and write nothing; a failing generator
  exits non-zero and leaves no trace in the output directory.

## Upload the inputs

```bash
workloads/w3-16k/upload-inputs.sh 10 /tmp/w3-16k <model dir>        # dry run: prints the aws commands only
workloads/w3-16k/upload-inputs.sh --yes 10 /tmp/w3-16k <model dir>  # uploads them
```

- The destinations are those of `plans/w3-16k/plan-w3-r<rate>.txt` (`w2 trace=`, `sidecar=`, `tokenizer prefix=`;
  the `-cells` plan of the rate reads the same ones), with `${RESULTS_BUCKET}` replaced by the bucket: the last
  argument, else `RESULTS_BUCKET` from the environment, else the one of the generated `manifests/config/platform.env`.
- Before any upload the trace and the sidecar must match the sha256 the plan pins and the `SHA256SUMS` of the
  directory must list exactly those two hashes. Anything else exits 1 and uploads nothing.
- `<model dir>` is a snapshot of the model revision (or a directory with only its tokenizer). The script takes the
  tokenizer files (`tokenizer.json`, `tokenizer_config.json`, `vocab.json`, `merges.txt`, `config.json` and the like,
  never the weights) and writes a `SHA256SUMS` for them. It prints `sums_sha256=<sha256>` of that file: the plan pins
  the value of the recorded runs, so when yours differs put the printed value in `plan-w3-r<rate>.txt` and
  `plan-w3-r<rate>-cells.txt` before you render the plans.
- The `tokenizer prefix=` and `sums_sha256` of the plans are those of the recorded model, `Qwen/Qwen3.8-27B-FP8` at
  revision `017b9c7a…` (`tokenizer-qwen38-27b-fp8-017b9c7a/`). The script uploads the tokenizer of the model directory
  it gets under that prefix, whatever the model: for another model, rename the prefix in both plans of the rate first
  ([docs/DEPLOY.md](../../docs/DEPLOY.md), section 7 step 2); its `sums_sha256` always differs. The trace does not
  depend on the model.

## Shape (the same at every rate)

`--seed 1111 --turns 11 --global-prefix-tokens 2048 --entry-tokens 5632 --growth-tokens 1024 --gap-sigma 0.3
--populate-s 180 --cold-fraction 0.03 --cold-min-tokens 15872 --cold-max-tokens 15872 --osl 500 --max-isl 16384
--block-size 512`

- **Sessions:** 11 turns each, sharing a 2,048-token global prefix. Turn k has an ISL of 5,632 + 1,024 k
  (k = 0..10), so the last turn is 15,872 tokens.
- **Cold rows:** 3 % of all rows are cold ingestions of exactly 15,872 tokens (never-seen hash ids).
- **Output:** OSL is a fixed 500 tokens (replayed with ignore_eos), so max ISL + OSL is 16,372, within 16,384.
  The hash block size is 512 tokens.
- **Pacing:** only `--sessions` and `--rate` move with the rate: N_s = 75 x rate sessions and `--rate` = rate
  (continuation turns/s). The mean inter-turn gap N_s / rate is then 75 s at every rate (lognormal, sigma 0.3).
  Session entries are spread over the first 180 s and none arrives later.
- **Window:** the replayed trace is cut at 615 s (rows with timestamp < 615,000 ms). The measured window is
  [255, 615) s (6 min): it starts one mean gap after the last entry and holds no entry.

| rate (req/s) | N_s | window rows K | window trace sha256 | sidecar sha256 | outcome |
|---|---|---|---|---|---|
| 10 | 750 | 5,769 | `906debd4…` | `daecaa73…` | valid |
| 25 | 1,875 | 14,472 | `49d83f37…` | `ef4bdd27…` | valid |
| 32 | 2,400 | 18,507 | `cb872dc5…` | `fa68d966…` | valid |
| 50 | 3,750 | 28,905 | `12be497f…` | `df08a7b5…` | inconclusive |

The full hashes are in `params.json`. The offered rate in the window is about 2 % above the generator rate
because of the cold rows.

## Absolute SLO (fixed before any W3 data, not calibrated on S0)

- Continuation TTFT ≤ 1.0 s.
- Cold TTFT ≤ 2.0 s: the cold rule factor × ISL / cold prefill rate gives 1 × 15,872 / 7,936 tok/s = 2.0 s for
  every cold row.
- Per-request mean ITL ≤ 25 ms.
- Errors count as misses. Entry rows (the first 180 s) are never judged in goodput.
- Driver plan header: `slo ttft_s=1.0 itl_ms=25 cold_factor=1 cold_prefill_tok_s=7936`.

## Cells and outcomes

The first runs compare two cells, unchanged: **S0** (round-robin routing, aggregated workers, no HiCache) and **S7**
(kv routing, HiCache and prefill/decode disaggregation 4P4D). Each ran once (n=1) per rate; `plan-w3-r10`,
`plan-w3-r25` and `plan-w3-r32` are their plans (the 50 req/s plan is not shipped).

- **10, 25, 32 req/s:** valid runs for S0 and S7.
- **50 req/s: inconclusive.** S0 was invalid because the load generator's own services were reaped at 50 req/s
  (a harness limit, not a server result). In S7 an engine pod went NotReady under load. Neither was retried.

Later the other six cells (S1 to S6) were replayed on the same traces with the same SLO at 10, 25 and 32 req/s
(the `-cells` plans), under the same n=1 label. Three of those steps failed:

- S5 at 25 req/s failed the pre-load reset check, so no load ran.
- S3 and S5 at 32 req/s (prefill/decode disaggregation without HiCache) failed: their decode workers restarted
  under load. The cause was not determined.

The per-cell results (goodput, TTFT, ITL, hit rate) are in the run analysis, which is kept with the run artifacts
and not in this repo.
- Method and adherence to the NVIDIA Dynamo benchmarking rules: [METHODOLOGY.md](METHODOLOGY.md)
