# demo_bench test suite

Status: GREEN. The suite was written test-first by qa (RED), then implemented by dev-bench:
315 tests pass (unit tests, Hypothesis properties, CLI and subprocess tests, byte-for-byte golden
checks), with `ruff check` and `mypy --strict` clean. Do not weaken a test: fix the code.

## How to run
From `tools/demo_bench`:

```
uv run --extra plot pytest          # whole suite (plain `uv run pytest` is equivalent: the dev group also pins matplotlib)
uv run ruff check src tests         # zero errors
uv run mypy                         # mypy --strict over src/demo_bench
```

matplotlib is needed only by `plot sweep` (`test_plot_sweep.py`); the runtime core stays
stdlib-only and a test asserts it never imports matplotlib/numpy/PIL. The tests parse XML (the SVG
charts) with `tools/demo_bench/tests/lib_xml.py`, on defusedxml, which the dev group pins at the
version of `tests/requirements-test.txt`. No test touches the
network, boto3 or the cluster: HTTP is injected (fakes in `conftest.py`) or served by a local
loopback server. Tests import `demo_bench` inside each test body so a missing implementation
fails a test instead of breaking collection.

Test files (`unit/`): `test_kv_sizing`, `test_trace`, `test_trace_schedule`,
`test_compare_runs`, `test_metrics`, `test_reset`, `test_matrix_effects`, `test_schedule`,
`test_schedule_subset`, `test_schedule_sweep`, `test_sweep_data`, `test_plot_sweep`, `test_cli`.

## Package contract

Python 3.11+, src layout `tools/demo_bench/src/demo_bench/`, console script `demo-bench`
(`demo_bench.cli:main`) and `python -m demo_bench`.

### `demo_bench.domain`
- `domain.ports`: `HttpResponse` Protocol (`status_code: int`, `text: str`, `json() -> Any`);
  `HttpClient` Protocol: `request(method: str, url: str, *, json=None, params=None, headers=None, timeout=None) -> HttpResponse`.
- `domain.kv_sizing`:
  - `ModelDims(num_layers, num_kv_heads, head_dim)` frozen dataclass.
  - `load_model_dims(path: Path) -> ModelDims` reads `num_hidden_layers`, `num_key_value_heads`, `head_dim` from config.json; missing `head_dim` -> `ValueError` mentioning `head_dim` (never derive).
  - `kv_bytes_per_token(dims, kv_dtype: str) -> int` = 2 * layers * kv_heads * head_dim * dtype bytes. dtypes: `fp8_e4m3`, `fp8_e5m2` (1), `bfloat16`, `float16` (2); other -> `ValueError("... dtype ...")`.
  - `KvSizing(dims, kv_dtype, hbm_gib, mem_fraction, weights_gib, overhead_gib=0.0)`: `.bytes_per_token`; `.device_tokens_per_rank() -> int` = floor((hbm_gib*mem_fraction - weights_gib - overhead_gib) * 2**30 / bytes_per_token); `ValueError` if mem_fraction not in (0, 1] or no room for KV; `.sessions_per_rank(session_tokens) -> int` (floor; `ValueError` if <= 0); `.host_pool_tokens(hicache_size_gb) -> int` = floor(gb * 1e9 / bytes_per_token); `.unique_tokens_per_rank(hicache_size_gb, write_through=True) -> int` = max(device, host) if write_through else device + host.
  - `TierHits(device, host, storage, miss)` frozen dataclass with `.accesses` (sum).
  - `replay_lru(trace: Sequence[tuple[str, int]], *, device_tokens, host_tokens, storage_tokens) -> TierHits`. Items are `(session_id, prefix_tokens)`. Inclusive tiers: every access inserts/refreshes the entry in all three tiers; each tier is an independent LRU bounded in tokens (entry larger than a tier is never stored there; capacity 0 disables). An access is counted as a hit in the first tier (device, host, storage) that holds the entry, else miss.
- `domain.comparison`: `RunArtifact(label, metrics: Mapping[str, Mapping[str, float]], error_count: int, aiperf_version: str)`, `ComparisonRow(metric, stat, baseline, candidate, ratio, missing)`, `Comparison(rows, baseline_errors, candidate_errors, missing)` with `.to_markdown() -> str`.
- `domain.window`: `RunWindow(start_s: float, end_s: float)` with `.seconds -> int` = ceil(end - start).

### `demo_bench.application`
- `application.trace`: `generate_session_trace(*, sessions, prefix_tokens, passes, seed, block_size=512, unique_tokens=512, output_tokens=64, spacing_ms=1000) -> list[dict]`; `write_trace_jsonl(rows, path)`.
  Rows: `timestamp` (ms, first = 0, step = spacing_ms), `input_length`, `output_length`, `hash_ids` (ints); nothing else. Pass 1 = populate (each session once), passes 2..N = independent seeded permutations, never identical to populate for sessions >= 3. First `prefix_tokens/block_size` ids are the shared per-session prefix; the remaining ids are globally unique. `ValueError` for sessions/passes/prefix_tokens <= 0 or prefix not a multiple of block_size.
- `application.compare`: `compare_runs(baseline: RunArtifact, candidate: RunArtifact, metrics: Sequence[str] | None = None) -> Comparison`. Default metrics: p50/p95/p99 of `time_to_first_token`, `inter_token_latency`, `request_latency`; `avg` of `request_throughput`, `output_token_throughput`, `goodput`. `ratio = candidate / baseline`, `None` if either side missing or baseline == 0. Missing metric/stat on either side: row has `missing=True`, value `None`, and the metric name is in `Comparison.missing`. `to_markdown()`: header line has `metric | stat | baseline | candidate | ratio`; ratio rendered `:.2f`; rows with missing data contain `MISSING`; includes error counts.
- `application.collect_metrics`: `collect_run_metrics(prom: PrometheusClient, namespace: str, window: RunWindow) -> RunMetrics(engine_hit_rate: float | None, router_predicted_hit: float | None, tier_tokens: dict[str, float])`. Evaluates the three builder queries at `window.end_s` with `window.seconds`. Absent/NaN -> `None`, never 0 (also for a zero router `_count` increase, i.e. 0/0).
- `application.reset`: `WorkerPod(name, host, is_hicache, system_port=9090)`; `ResetRecord(pod, endpoint, status_code, attempts, ok)` with `.to_dict()`; `ResetError` (has `.records: list[ResetRecord]`); `reset_caches(pods, http, *, sleep=time.sleep, max_attempts=6, backoff_initial_s=1.0, backoff_factor=2.0, max_wait_s=60.0) -> list[ResetRecord]`.
  POST `http://{host}:{system_port}/engine/flush_cache` (all pods) then `/engine/clear_hicache_storage` (only `is_hicache`), json body `{}`. A response whose text contains `requests are active` (any status), or HTTP 200 with body `{"success": false}` (Dynamo v1.5.1 `flush_cache:tm` route passes SGLang's `FlushCacheReqOutput` through as 200; `sglang/engine_routes.py:348-409`), is "busy": sleep `initial * factor**i` and retry up to `max_attempts`; then `ResetError`. Any other non-200 -> `ResetError` immediately. Empty pod list -> `ResetError`. No `cache_salt`/salt anywhere.
- `application.matrix`: `Observation(r, o, d, round, kpi)` (r/o/d in {0, 1}); `Effect(estimate, ci_low, ci_high)` with `.ratio = exp(estimate)`; `matrix_effects(observations, *, n_boot=1000, seed=0, alpha=0.05) -> dict[str, Effect]` with keys `R O D RO RD OD ROD`. Effect = mean(log kpi) where the +-1 contrast is +1 minus mean where it is -1 (interaction contrast = product of signs). Percentile bootstrap resampling rounds. `ValueError` for missing cell (message contains `cell`) or kpi <= 0.
- `application.schedule`: `run_schedule(rounds=3) -> list[PlannedRun]`; `PlannedRun(round (1-based), topology, router ("rr"|"kv"), scenario ("S0".."S7"), new_deploy)`. 8 runs per round, both arms of a topology adjacent, topology order differs per round, first-arm alternates between consecutive rounds for every topology, deterministic.

### `demo_bench.infrastructure`
- `infrastructure.aiperf_reader`: `read_run_artifact(path, label) -> RunArtifact` for `profile_export_aiperf.json` (stat dicts carrying `unit`; `error_count` = sum of `error_summary[].count` + collapsed outputs (via `application.validity`, only when the export sits in a directory named `aiperf` next to `scheduled-trace.jsonl`); `aiperf_version`); `read_profile_window(path) -> RunWindow` for a per-request `profile_export.jsonl` (rows `{"metadata": {"benchmark_phase": "warmup"|"profiling", "request_start_ns", "request_end_ns", "was_cancelled"}, "metrics": {...}}`; warmup rows excluded; start = min start, end = max end; no profiling rows -> `ValueError` mentioning `profiling`). Assumption (M): the `benchmark_phase` field name is confirmed against a real aiperf 0.13.0 export.
- `infrastructure.metrics`: `engine_hit_rate_query(ns, window_s)`, `router_predicted_hit_query(ns, window_s)`, `tier_mix_query(ns, window_s)` (exact strings asserted in tests); namespace not matching `^[a-z0-9]([-a-z0-9]*[a-z0-9])?$` or window <= 0 -> `ValueError`. `PrometheusClient(base_url, http: HttpClient)`: `.query(promql, at: float) -> list[Sample(labels, value)]` (GET `{base}/api/v1/query`, params `query`, `time`), `.query_scalar(promql, at) -> float | None`; `PrometheusError` on non-200 or `status: error`. Router predicted hit = `sum(increase(..._sum{namespace=NS}[Ws])) / sum(increase(..._count{namespace=NS}[Ws]))` (no `clamp_min`: it understates the hit rate below 1 req/s; spec 11 corrected).

### `demo_bench.cli`
`main(argv: Sequence[str] | None = None) -> int`; argparse subcommands `sizing trace metrics reset compare matrix schedule plot`
(`--help` exits 0 for each; exit codes: 0 ok, 1 operation failed, 2 invalid input). `python -m demo_bench` works.
- `trace --sessions N --prefix-tokens N --passes N --seed N --out PATH [--block-size --spacing-ms --output-tokens]`: session-revisit trace; invalid args -> non-zero exit, no file written.
- `trace schedule --in P --out P --rate R --seed S [--arrival poisson|uniform] [--duration SEC | --rows N]`: stamps `timestamp` (ms) on an unscheduled trace (section below).
- `schedule [--rounds N] [--topologies a,b] [--sweep-concurrency 1,4,8] [--format tsv|json]`: matrix plan (24 runs by default, byte-identical to the legacy plan without the new flags); `--topologies` gives a chained subset plan; `--sweep-concurrency` gives closed-loop sweep rows (sections below).
- `plot sweep --runs DIR --out DIR --setup LABEL [--pairs S0:S1,...]`: PPT-style charts (section below); needs the `plot` extra.
- `reset` against a real local HTTP server answering 200 `{"success": false}`: bounded attempts, non-zero exit, evidence file written with `ok: false`.

## Open questions
- Does `sglang:cached_tokens_total` emit a `cache_source="total"` series next to the per-tier ones? If so the engine hit-rate numerator double counts. Being verified in the real image (FT-8); hit-rate tests unchanged until then.

## Fixtures
`fixtures/qwen3_32b_config.json` (head_dim 128 while hidden/heads = 80), `fixtures/aiperf_baseline|candidate/profile_export_aiperf.json` (aiperf 0.13.0 key layout copied from a real export; candidate lacks `inter_token_latency` and `goodput`), `fixtures/aiperf_run_window/*.jsonl`.

### `fixtures/w2_qwen38/`: the W2 window trace
`window.jsonl` (920 rows) and `window.side.jsonl` are the W2 trace and sidecar that the Qwen3.8-27B-FP8 matrix
runs replayed, cut to the run window; `verdict-t8.json` is the offline gate verdict computed on them
(`unit/test_w2_gate_inclusive.py`). Both files are `demo-bench trace w2` output, made on 2026-10-22 and reproduced
byte for byte on 2026-10-28 with the generator of this repository by these steps:

1. Output lengths: the `output_length` values of the upstream agentic request trace of the NVIDIA Dynamo recipes
   (Apache-2.0), `recipes/kimi-k2.6/perf/traces/64k_400_90kv_agent_new_noschedule_short_15perc.jsonl` as added
   in commit 66a95d5aa3b5d238fd94b6f8243c4e479cb7242c (unchanged in tag v1.5.1): the Git LFS object of 3541 rows,
   sha256 f20d3f2bc83dd1306cda659fbe34e7c4d85ca5497626c98bc0b1c4d2211379d0. It is the upstream file, not the
   3493-row copy in `tools/bench-image/traces/`: one output length of the window comes from the 48 rows that copy
   drops. The extracted list `osl.json` has sha256 d1ee8a76984ba5b535f9c7ce8c4a6a59515b5e2f25a9bd2b6eb3bb053372c986.

   ```bash
   python3 -c 'import json,sys; print(json.dumps([json.loads(l)["output_length"] for l in open(sys.argv[1]) if l.strip()]))' \
     "$DYNAMO_REPO/recipes/kimi-k2.6/perf/traces/64k_400_90kv_agent_new_noschedule_short_15perc.jsonl" > osl.json
   ```

2. Full trace, seed 1111 (from `tools/demo_bench`; 1773 rows, trace sha256
   34b020cddacdd7e757564f3f9e399aaf4248fe46fb3ac195001e5bf1fd942c0f, sidecar sha256
   a9101c8f89b73ab7d7143de42824d973f7ec7df04b8aabafc14a63606b1e0b73; `--expect-sha256` refuses any other trace):

   ```bash
   uv run demo-bench trace w2 --out w2-full.jsonl --sidecar w2-full.side.jsonl \
     --seed 1111 --sessions 195 --turns 8 --global-prefix-tokens 8192 --entry-tokens 98304 --growth-tokens 3072 \
     --rate 0.5 --gap-sigma 0.3 --populate-s 540 --cold-fraction 0.12 --cold-min-tokens 102400 \
     --cold-max-tokens 102400 --osl-file osl.json --osl-cap 12000 --max-isl 200000 --block-size 512 \
     --expect-sha256 34b020cddacdd7e757564f3f9e399aaf4248fe46fb3ac195001e5bf1fd942c0f
   ```

3. Run window: the rows with timestamp < 1740000 ms (540 s of populate plus 1200 s measured). The generator sorts the
   rows by timestamp, so they are the first K = 920 rows of both files:

   ```bash
   head -n 920 w2-full.jsonl > window.jsonl            # sha256 7b061c7b3128e4cbdffe3e9134b5001108ad6558d06e4ba63a09670f2eea9f09
   head -n 920 w2-full.side.jsonl > window.side.jsonl  # sha256 f70c8b6248d1a44f79b7de1e37115778e31b4686307c38f50fdce294c99b5bf1
   ```

`unit/test_w2_fixture_provenance.py` checks these values against the committed files and, when `DYNAMO_REPO` points at
a Dynamo checkout whose Git LFS objects are pulled (default: a sibling directory named `dynamo`), replays the three
steps and compares both files byte for byte; without it the replay is skipped with the reason.

## Fixed-schedule stamping (`trace schedule`) -- `unit/test_trace_schedule.py`
aiperf 0.13.0 format (wheel source): `timestamp` in **milliseconds** (`int | float`), `aiperf/dataset/loader/models.py` `MooncakeTrace.timestamp`; fixed-schedule row example in `aiperf/dataset/loader/mooncake_trace.py`; `--fixed-schedule-start-offset/end-offset` are ms and inclusive (`aiperf/config/flags/cli_config.py`).

- `application.trace_schedule.arrival_times_ms(n, *, rate, seed, arrival="poisson"|"uniform") -> Sequence[int | float]`: ms, `[0] == 0`, non-decreasing, finite; deterministic and prefix-stable in `n`; poisson = exponential gaps with mean `1000/rate` (seeded, independent of `PYTHONHASHSEED`); uniform = constant gap `1000/rate` (seed-independent, integer-ms rounding allowed). `ValueError` for rate <= 0/NaN/inf or unknown arrival.
- `application.trace_schedule.schedule_trace(src, out, *, rate, seed, arrival="poisson", duration_s=None, rows=None) -> ScheduleResult(out, rows, input_sha256, output_sha256, rate, seed, arrival, duration_ms)` with `.to_dict()`. Each output line = the input line up to (excluding) its closing `}` + the `timestamp` member + `}`; row order preserved; one `\n` per row. `rows=n` keeps the first n rows (n > available -> error); `duration_s` keeps rows with `timestamp <= duration_s*1000` (inclusive); both together, `rows<=0`, `duration<=0`, empty/blank input, malformed JSON, missing file, or an input row already holding `timestamp`/`session_id`/`delay`/`messages` -> `ScheduleError(ValueError)` and no output file.
- CLI: `demo-bench trace schedule --in P --out P --rate R --seed S [--arrival poisson|uniform (default poisson)] [--duration SEC | --rows N]` prints one JSON object (`out`, `rows`, `input_sha256`, `output_sha256`, `rate`, `seed`, `arrival`, `duration_ms`) and exits 0; every invalid input exits 2. `demo-bench trace --sessions ...` keeps working.

## Subset schedule (`schedule --topologies`) -- `unit/test_schedule_subset.py`
- `application.schedule.run_schedule(rounds=3, topologies: Sequence[str] | None = None) -> list[PlannedRun]`. `None` keeps today's plan byte-for-byte (golden `fixtures/schedule_full_rounds{3,5}.tsv`, `schedule_full_rounds2.json`).
- Known names: `t-agg`, `t-agg-hic`, `t-dis`, `t-dis-hic` (case sensitive). Unknown, empty, duplicated or empty list -> `ValueError`; the in-pod ids (`t-dis-e`, `t-dis-hic-e`) are manifest overlays, not scheduler topologies, so they are rejected.
- Guarantee for k topologies, R rounds: `2*k*R` runs, every (scenario, rep) once, both arms of a (round, topology) adjacent, first arm alternates between consecutive rounds, topology order differs between consecutive rounds (k >= 2), the `--topologies` listing order is the round-1 order (later rounds alternate reversed/listed; `t-agg-hic,t-agg` starts on t-agg-hic, `t-agg,t-agg-hic` is unchanged), deterministic (no RNG, independent of `PYTHONHASHSEED`), and exactly `k*R - (R-1)` deploys: rounds are chained so the last topology of round r is the first of round r+1 and is not redeployed (`new_deploy=False`, column 5 `toggle`). The two-topology subset (t-agg,t-agg-hic, 3 rounds) = 12 runs, 4 deploys, scenarios S0/S1/S2/S4 (the filtered full plan needs 6).
- CLI: `demo-bench schedule --rounds N [--topologies a,b] [--format tsv|json]`; TSV columns unchanged: `round topology router scenario mode namespace`. Invalid `--topologies` or `--rounds < 1` -> exit 2 with nothing on stdout.

## Concurrency sweep and charts -- `unit/test_schedule_sweep.py`, `test_sweep_data.py`, `test_plot_sweep.py`
Run with the plot extra: `uv run --extra plot pytest` (pyproject must declare `[project.optional-dependencies] plot = ["matplotlib..."]`; `dependencies` stays empty; `demo_bench.cli` and `demo_bench.application.sweep` must not import matplotlib/numpy/PIL).

- `application.schedule.run_sweep_schedule(concurrencies, *, rounds=1, topologies=None) -> list[SweepRun(round, topology, router, scenario, new_deploy, concurrency)]` (+ `.namespace`). Topology blocks chained as in the `--topologies` plan (deploys = k*R - (R-1); `topologies=None` = all four); inside a block concurrencies ascend, both arms of a concurrency are adjacent in the round's first-arm order, so consecutive rows of a block always switch arm; input order of concurrencies irrelevant; `ValueError` for an empty list, values < 1 or duplicates.
- CLI: `demo-bench schedule --sweep-concurrency 1,4,8 [--topologies ..] [--rounds N] [--format tsv|json]`. TSV = the 6 legacy columns plus `concurrency=<N>` (7 columns; columns 0/1/3 keep their meaning); json rows gain an integer `concurrency`. Non-integer, < 1, duplicate, empty entries -> exit 2. Without the flag the output is byte-identical to the legacy plan.
- `application.sweep.build_sweep_data(runs_dir, *, setup, pairs=()) -> dict` and `SweepError(ValueError)`: schema in the module docstring of `test_sweep_data.py` (recursive `run.json` discovery, `mode == "closed-loop"` + integer `concurrency` only, median over reps, `None` gaps, invalid runs (`aiperf.rc != 0`, `restarted_pods`, missing export, `was_cancelled`) listed in `excluded`, speedup = baseline / candidate TTFT p50, fixed colour per scenario id, no paths).
- CLI: `demo-bench plot sweep --runs DIR --out DIR --setup LABEL [--pairs S0:S1,S0:S2]`. Writes `{ttft,throughput,itl}_vs_concurrency.{png,svg}`, `speedup_vs_concurrency.{png,svg}` only with `--pairs`, and `sweep_data.json`. Blank/missing `--setup`, malformed or unknown/duplicate pairs, no sweep runs -> exit 2 and no chart written. Missing matplotlib -> exit 1 with a one-line stderr message naming the `plot` extra and `matplotlib` (no traceback). Rendering: Agg, 16:9 PNG >= 1600 px wide, SVG text kept as text (`svg.fonttype = "none"`), setup label on every chart, English labels (ASCII only apart from the setup label), p50 solid / p90 dashed, one marker per plotted point, NaN for gaps, no dates/paths in the SVG, byte-identical output for identical input.
- Fixtures: `fixtures/sweep/c<N>/ceiling/<S>/rep1/{run.json,aiperf/profile_export_aiperf.json}` (S0/S1/S2/S4 x 1..128, plus `fixtures/sweep/openloop/matrix/S0/rep1`, a fixed-schedule run that must be ignored). `run.json` is the run record the sweep reader expects (schema above); the aiperf exports are synthetic values on the key layout, p-stat set and units of a real aiperf 0.13.0 export (`fixtures/real_profile_export_aiperf.json` is a verbatim copy of the export of a real Qwen3.8-27B run; no credentials, only in-cluster service DNS). The reader finds runs by `run.json` content, not by path, so each concurrency can write to its own results directory.

## Track D tooling (WP6, RED) -- `unit/test_trace_multiturn.py`, `unit/test_restore_probe.py`, `unit/test_cost_ledger.py`
The module docstring of each file is the full contract; summary:

- `trace multiturn` (`application.trace_multiturn.generate_multiturn_trace`): mooncake rows `timestamp` (ms) / `input_length` / `output_length` / `hash_ids` only. Defaults N=40, T=8, S_sys=2048, C0=30720 (includes the system prompt), G=1536, OSL=128, block 512; `--turn-period-ms` and `--seed` required. Turn k+1 `hash_ids` extend turn k exactly; all sessions share only the system-prompt blocks; phases are `{Tp*j//N}` (seeded assignment); rows sorted by timestamp. Hypothesis checks the prefix/schedule invariants over random sizes. Invalid input -> `ValueError` / CLI exit 2, no file.
- `restore-probe` (`application.restore_probe.run_restore_probe`, CLI `demo-bench restore-probe`): cold -> gpu -> filler -> host (-> storage filler -> storage) against `/v1/completions` with `logprobs`; passes iff `cached_tokens` is identical across tiers (and > 0 at the GPU hit) and every top-1 logprob differs by <= 1e-3. Greedy text differences never fail it (#43055). Tests run the real `UrllibHttpClient` against a local fake server with a 3-tier LRU (no mocks of the client). Exit 0 pass, 1 failed/endpoint error, 2 invalid input.
- `cost-ledger` (`application.cost_ledger.compute_cost_ledger`, CLI `demo-bench cost-ledger`): sum of node-hours x price for the NodeClaims of one NodePool (kubectl JSON snapshot) + ODCR hourly cost since creation; `alert` at >= 80 % of the budget (exit 0, `ALERT` on stderr), `exceeded` at >= 100 % (exit 1); a missing price is an error, never 0.
- Note: the "exit 2" CLI tests carry a valid-invocation control so they cannot pass just because the subcommand does not exist yet.

## Flash tooling (RED) -- `unit/test_trace_w2.py`, `test_w2_gate.py`, `test_slo_goodput.py`, `test_restore_noise_floor.py`, `test_flash_tooling_wiring.py`
The module docstring of each file is the full contract; summary:

- `trace w2` (`application.trace_w2.generate_w2_trace`): agentic sessions with a shared global prefix, entry context plus growth per turn (hash_ids extend the previous turn), lognormal gaps with mean `sessions / rate`, `cold_fraction` cold ingestions with never-seen hash_ids, fixed or empirical OSL (clipped), open-loop `timestamp`, sidecar line -> session / turn / class. Deterministic by seed; the CLI prints the sha256 of both files and `--expect-sha256` refuses a different trace (exit 1, no file).
- `w2-gate` (`application.w2_gate`): offline routed LRU (rr vs kv-sticky, GPU tier + L2 victim tier per target, leaf-first eviction, prefix hits). Reports the hit ceilings, the per-pod distinct footprint under rr and under kv, and the plan thresholds as named checks (`evaluate_thresholds`, boundary-tested). Exit 0 / 1 / 2.
- `slo-goodput` (`application.slo_goodput`): SLO-met turns per second from the aiperf per-request export paired with the sidecar (continuation vs cold ingestion; entry rows are the populate and are never judged), sensitivity grid, bootstrap CI whose resampling unit is the session, S0 calibration band.
- Restore-probe noise floor (`application.restore_probe.measure_noise_floor`): cold-vs-cold and GPU-hit-vs-cold controls, `threshold = max(1e-3, k x floor)`; `application.image_gate.decide_image` turns two probe reports (derived image, dev.1 control) into one of four explicit outcomes (derived_only_pass, both_pass, control_only_pass, both_fail).
- The restore-probe tests use a local fake server with a flush route; the pre-existing probe tests are unchanged.
- Wiring tests (`test_flash_tooling_wiring.py`): no gpt-oss / L4 / g6 wording in the restored tools, every new flag in `--help` and in its README section, new modules stdlib only.
- Exit-2 CLI tests carry a valid-invocation control.
