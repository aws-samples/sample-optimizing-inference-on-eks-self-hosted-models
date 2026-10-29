# Qwen3.8 matrix driver

The in-cluster driver that produced every recorded result of the Qwen3.8 matrix (driver version `q38-matrix-driver/4`). It runs a **plan** (an ordered list of cells and repetitions) entirely inside the cluster as a Kubernetes Job, so a laptop disconnect does not stop a run. It is a ported copy of the driver of the recorded runs; behaviour is unchanged except for the parametrization described below.

## What is in this directory

| File | Role |
|---|---|
| `matrix_driver.py` | The driver: plan parser, preflight, per-run flow, S3 upload, take and compose steps. Stdlib plus the tools of the bench image (`kubectl`, `aws`, `aiperf`). |
| `watchdog.py` | The independent hard-stop watchdog (CronJob). It never reads the driver's state. |
| `render_driver.py` | Renders every Kubernetes object of the driver from this repo (needs PyYAML). |
| `dryrun_plan.py` | Runs a shipped plan offline on the fake cluster, with injected failures. |
| `fake_cluster.py` | Fake cluster and fake S3 used by the dry-run and the tests. |
| `takes/capture.py`, `takes/compose.sh` | The recorded-take scripts (headless Chromium capture of the Grafana take view; ffmpeg compose). Shipped in the ConfigMap `q38-take-capture`. The take features are not supported in this sample: they need a capture Job that is not shipped. |
| `tests/` | The offline suite (see [Tests](#tests)). |

Plans live in `plans/w3-16k/` (the W3-16K plans, the default `--plans-dir`) and `plans/w2-long-context/` (the W2 long-context plans), and the image pins in `manifests/track-q-qwen38/driver/images.env` (see that directory's README).

## What a run does

For each `run` step the driver: deploys the cell's DynamoGraphDeployment (or only toggles the router mode when the topology is already up), waits for the workers, resets the caches and verifies the reset with canary requests, takes a pre snapshot of the Prometheus counters, replays the frozen trace with `aiperf` on a fixed schedule, takes a post snapshot, and computes KPIs, validity and SLO goodput. Everything is uploaded append-only, with `run.json` last. A DGD is never reused after a failed or invalid run. No prompt content is logged or uploaded.

The eight cells are S0 to S7 (topology times router mode): S0 aggregated + round-robin, S1 aggregated + KV router, S2 aggregated + HiCache + round-robin, S4 aggregated + HiCache + KV, S3 PD + round-robin, S5 PD + KV, S6 PD + HiCache + round-robin, S7 PD + HiCache + KV.

### Where the cells come from

The driver does **not** generate DGDs. `render_driver.py` reads the eight committed renders `manifests/track-q-qwen38/rendered/S0.yaml` to `S7.yaml`, adds one annotation (`q38-matrix/workers-sha256`, the hash of everything but the Frontend) and ships them in the ConfigMap `q38-matrix-dgds`. The driver adopts a live DGD only when the annotation matches, otherwise it tears it down and deploys the file. Change a cell by re-rendering Track Q, not here.

## Plan grammar

A plan is a text file; `#` starts a comment. Header lines, then steps in execution order.

| Line | Meaning |
|---|---|
| `run_id <id>` | Required. Prefix of the results and base of the Job names. |
| `hard_stop <UTC instant>` | Required, `YYYY-MM-DDTHH:MM:SSZ`. Nothing runs after it. |
| `on_failure abort\|continue` | Optional, default `continue`. `abort` ends the plan at the first failed or invalid run. |
| `cut_order <cell> ...` | Optional. Cells whose later repetitions are dropped first when the remaining time no longer fits. |
| `w2 trace=... trace_sha256=... sidecar=... sidecar_sha256=... frozen=yes` | Required. The frozen trace; refused unless `frozen=yes`. |
| `tokenizer prefix=... sums_sha256=...` | Required. |
| `slo ttft_s= itl_ms= cold_factor= cold_prefill_tok_s=<n\|probe>` | Required, explicit SLO. `probe` needs a `probe-cold-prefill` step before the first run. |
| `slo from=<s3 uri> lock=<s3 uri> e3_plan_sha256=<sha>` | Alternative: use the SLO calibrated by an earlier plan, checked against its lock. Cannot be combined with `calibrate-slo` or `probe-cold-prefill`. |
| `estimate boot_s= reset_s= drain_s= probe_s=` | Optional. Seconds used only by the "does the next step still fit before the hard stop" guard. |
| `run <cell> <rep> [label=...]` | One measured run. |
| `take <S0\|S7> <rep> [label=] [retake_of=<cell>/rep<k> min_left_s=<n>]` | A run recorded on video by a capture Job: not supported in this sample, which does not ship that Job. `retake_of` and `min_left_s` go together: the take is re-run only if the named one is unusable and enough time is left. |
| `compose [target_s=<n>]` | Once, after at least one take of S0 and of S7: side-by-side clips. Not used without takes, so not supported in this sample either. |
| `probe-cold-prefill <cell> [n=] [tokens=]` | Measures cold prefill throughput on a deployment. |
| `calibrate-slo after=<cell>/rep<k> [band=lo,hi]` | Writes the SLO calibration and its lock from an earlier run. |
| `teardown` | Deletes the matrix DGDs. |

Placeholders `${KEY}` in a shipped plan (only `${RESULTS_BUCKET}` is used) are expanded from `manifests/config/platform.env` when the plans ConfigMap is rendered. Each run gets one results prefix, so a duplicate `run` or `take` is refused.

## Launch sequence

Render with `render_driver.py` and apply in this order. All objects live in the `bench` namespace and the four scenario namespaces. Parameters that depend on the deployment come from `manifests/config/platform.env` (`--env`) and `manifests/track-q-qwen38/driver/images.env` (`--images`), both the defaults.

1. **Base.** Build a `demo_bench` wheel from `tools/demo_bench` and render:

   ```bash
   python3 tools/matrix_driver/render_driver.py --out out --wheel path/to/demo_bench-*.whl
   kubectl apply --server-side -f out/00-base.yaml
   ```

   `00-base.yaml` holds the ServiceAccounts (`matrix-driver`, `bench`), the least-privilege Roles for the scenario namespaces and four ConfigMaps (driver and wheel, the eight DGDs, the expanded plans, the take scripts). `--server-side` is needed because the ConfigMaps exceed 256 KiB.

2. **Watchdog, before the first driver Job.** The driver refuses to start (exit 2) unless the CronJob `bench/q38-matrix-watchdog` exists, is not suspended and has a window that opens at the plan's `hard_stop`.

   ```bash
   kubectl apply --server-side -f out/20-watchdog.yaml
   ```

   `20-watchdog.yaml` is rendered together with the base. Set the windows and schedule at that render with `--watchdog-windows START/END[,START/END...]` (UTC) and `--watchdog-schedule CRON`; the defaults are the values of the recorded runs. A launch with `--hard-stop-in` (step 4) renders its own `20-watchdog.yaml` instead.

3. **Take preflight (plans with `take` steps only, e.g. `plan-e7`; its Job is rendered when the base render uses `--plans-dir plans/w2-long-context`, since no W3-16K plan takes).** Needs the Secret `bench/demo-grafana-auth` and the ffmpeg image mirrored into `BENCH_REPO`. The Job `bench/q38-take-preflight-<run_id>` checks the Grafana login, the dashboard and its namespace variable, the Prometheus datasource and one panel render, and reports to `takes/<run_id>/_preflight/`. It must Complete before the takes are launched. To re-run it, delete the Job first.

   ```bash
   kubectl apply --server-side -f out/05-take-preflight.yaml
   ```

4. **Plan Job.** Render at launch time, because `activeDeadlineSeconds` is the plan's hard stop minus now, and create it right away. `--cb-node` is the instance id of the Capacity Block GPU node (passed to the driver as `CB_NODE`); the Job itself runs on the `gpu-b200-cb` node pool, next to the DGDs.

   ```bash
   python3 tools/matrix_driver/render_driver.py --out out --launch plan-w3-r32 --cb-node <instance-id> --hard-stop-in 8h
   kubectl apply --server-side -f out/08-plans-plan-w3-r32-<UTC timestamp>.yaml -f out/20-watchdog.yaml
   kubectl create -f out/10-job-plan-w3-r32-<UTC timestamp>.yaml
   ```

   The shipped `hard_stop` instants are those of the original recorded session, in the past for any new session, so a new launch passes `--hard-stop-in <N>s|m|h` (a positive whole number, e.g. `8h`, `90m`). The hard stop becomes the launch instant, rounded up to the second, plus that duration. Next to the Job the render writes `08-plans-<plan>-<UTC timestamp>.yaml`, the plans ConfigMap `q38-matrix-plans` holding only this plan with its `hard_stop` line set to that instant (every other line as shipped), and a `20-watchdog.yaml` whose one window opens at it (schedule `*/5 * * * *`, window of 30 minutes). Apply both before the Job, as above: the driver refuses to start unless the plan, the Job and a watchdog window agree on the hard stop. Re-applying `00-base.yaml` restores the shipped plans. Without `--hard-stop-in` only the Job is written, ending at the plan's own `hard_stop` (refused once it has passed).

   `--plans-dir` selects the plan directory for the base render and for `--launch`: `plans/w3-16k` (default) or `plans/w2-long-context`, e.g. `--launch plan-e3 --plans-dir plans/w2-long-context`.

   The Job is `bench/q38-matrix-<plan name without "plan-">`, with no retries. Re-render if more than a few minutes pass. Relaunching the same plan resumes: runs whose `run.json` exists are skipped. Only one driver Job may be active at a time (preflight refuses otherwise).

   Plans that use `slo from=...` (`plan-e4` to `plan-e7`) need the `plan-e3` calibration and lock in S3 first, so launch `plan-e3` before them. They pin the sha256 of the shipped `plan-e3` (`e3_plan_sha256`), and `--hard-stop-in` changes the sha256 of the plan it re-times: a lock written by a re-timed `plan-e3` is refused by them. The pinned value is the sha256 of `plan-e3` after its `${...}` placeholders are expanded from `platform.env`, so editing `plan-e3` or changing `platform.env` (for example `RESULTS_BUCKET`) requires re-pinning `e3_plan_sha256` in `plan-e4` to `plan-e7`. A plan with `take` steps gets the `COMPOSE_FFMPEG_IMAGE` variable in the Job env.

Order summary: `render_driver.py` base render, apply `00-base.yaml`, apply `20-watchdog.yaml`, apply `05-take-preflight.yaml` (takes only), then `--launch` the plan (with `--hard-stop-in`: apply its `08-plans-*` and `20-watchdog.yaml`, then create the Job).

### Preflight and exit codes

Before any cluster change the driver checks RBAC (`kubectl auth can-i`), the Job's `activeDeadlineSeconds` against the plan hard stop, the watchdog, that no other driver Job is active, and, for `slo from=`, the SLO lock. The plan lock `_lock/plan-lock-<launch>.json` is the first object written to S3.

| Exit | Meaning |
|---|---|
| 0 | Every step done and every run valid. |
| 1 | A step failed, a run was invalid, or a fatal condition. |
| 2 | Configuration refusal, before any cluster change. |
| 3 | Hard stop or SIGTERM reached: remaining steps cut, topology torn down. |

## Shipped plans (`plans/w2-long-context/`, `plans/w3-16k/`)

`plan-e3` to `plan-e7` are in `plans/w2-long-context/`, the W3-16K plans in `plans/w3-16k/`.

| Plan | Content |
|---|---|
| `plan-e3` | S0 pilot (cold-prefill probe, SLO calibration and lock), then S7 smoke. |
| `plan-e4` | S0 and S7, three repetitions each, with the SLO of `plan-e3`. |
| `plan-e5` | S1 and S4, three repetitions each, with the SLO of `plan-e3`. |
| `plan-e6` | S2, S3, S5, S6, three repetitions each, with the SLO of `plan-e3`; `cut_order` S6 S3 S5. |
| `plan-e7` | Recorded takes of S0 and S7, with retakes, then `compose` (`take` and `compose` are not supported in this sample). |
| `plan-w3-r10`, `plan-w3-r25`, `plan-w3-r32` | W3-16K, S0 vs S7 at 10, 25 and 32 req/s, n=1. |
| `plan-w3-r10-cells`, `plan-w3-r25-cells`, `plan-w3-r32-cells` | The remaining cells S1 to S6 at the same rates, n=1. |

The W3 plans carry `label=w3-16k`: n=1 per cell and rate; a different workload from W2, never pooled with it. Their inputs are uploaded by `workloads/w3-16k/upload-inputs.sh`. The `hard_stop` instants in the shipped plans are those of the original recorded session: they are in the past for any new session. Launch with `--hard-stop-in` (see the launch sequence), which re-times only the `hard_stop` line and renders the matching watchdog.

## Hard stop and watchdog

Two independent mechanisms end a plan at its `hard_stop`:

- The driver's own Job deadline (`activeDeadlineSeconds`) and a SIGTERM handler: on SIGTERM the driver tears the topology down (waiting up to 240 s inside a 600 s grace period), uploads what it has and exits 3. It also never starts a step whose estimate no longer fits before the hard stop.
- `watchdog.py`, a CronJob that does not read the driver's state. Inside an action window it deletes every matrix DGD (label `app.kubernetes.io/part-of=qwen38-matrix`) in `s-agg`, `s-agg-hic`, `s-dis` and `s-dis-hic`, then the capture, compose and driver Jobs in `bench`, waits until they and the DGD pods are gone, and logs the state. Outside every window it does nothing. A window does not act while a driver Job whose `q38-matrix/hard-stop` annotation is later than the window start is still active. It exits 1 if something is still present after the wait.

## Results in S3

All keys are append-only; an existing prefix without `run.json` is not reused.

```text
s3://<RESULTS_BUCKET>/matrix/<run_id>/
  _lock/plan-lock-<launch>.json        plan sha256, SLO, trace, driver version (first object)
  _slo/slo-calibration.json, slo-lock.json    plans with calibrate-slo
  _probe/cold-prefill-<cell>.json      plans with probe-cold-prefill
  _teardown/<topology>-<launch>-<n>.json
  _failed/<cell>/rep<k>/<launch>/      evidence of a failed attempt
  <cell>/rep<k>/                       one run: aiperf export, snapshots, evidence, run.json last
s3://<RESULTS_BUCKET>/takes/<run_id>/
  <take_id>/                           webm, capture JSON, frame chunks (take steps)
  compose/                             side-by-side clips, compose.json last
  _preflight/                          take preflight report
```

`RESULTS_BUCKET` and `AWS_REGION` come from `platform.env`; no account, region, bucket or registry value is written in this directory.

## Parametrization

- `manifests/config/platform.env`: results bucket, region, bench ECR repository (`BENCH_REPO`), and the Track Q image (`RUNTIME_IMAGE`, used by the L3 cleanup pod). Plans carry `${RESULTS_BUCKET}`.
- `manifests/track-q-qwen38/driver/images.env`: tags and digests of the driver image and the ffmpeg image, joined to `BENCH_REPO` as `<BENCH_REPO>:<TAG>@<DIGEST>`.
- `--watchdog-windows`, `--watchdog-schedule`: the watchdog windows and CronJob schedule.
- `--plans-dir`: the plan directory (`plans/w3-16k` by default, or `plans/w2-long-context`).
- `--cb-node`: the Capacity Block node, at launch.
- `--hard-stop-in`: a hard stop relative to the launch instead of the plan's, at launch.

## Tests

The suite runs offline on the fake cluster and fake S3: no AWS, no `kubectl`, no network. From the repo root:

```bash
uv run --with-requirements tests/requirements-test.txt pytest tools/matrix_driver/tests -q -p no:cacheprovider
```

It covers the driver and watchdog behaviour, the failure paths, the take and compose flow, and golden checks that the rendered objects and expanded plans are byte-stable. The sha256 of `watchdog.py`, `takes/capture.py` and `takes/compose.sh` is pinned in `tests/fixtures/golden_plans_and_verbatim.json`, so any edit of those files, a comment included, fails the suite until the pin is updated on purpose in the same change. Repo-level checks (layout, no region/account/bucket literals, plan placeholders, image pins, this README) are in `tests/manifests/test_290_matrix_driver_layout.py`.

To exercise a shipped plan end to end without a cluster, with injected failures:

```bash
PYTHONPATH=tools/demo_bench/src python3 tools/matrix_driver/dryrun_plan.py --plan plans/w2-long-context/plan-e4.txt \
  [--broken-deploys s-agg=1] [--hard-stop-in-s N] [--sigterm-at S7/rep1]
```

It prints the driver's JSON log, the cluster actions, the S3 keys and the step statuses. The latencies are synthetic and the `plan-e3` SLO lock it needs is a fake made for the dry-run: its output is not data.
