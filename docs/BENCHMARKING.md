# Benchmarking a cell with AIPerf

How to load one cell of this sample (S0 to S7, defined in [examples/README.md](../examples/README.md)) with a multi-turn
workload and read goodput. Everything here is done by hand with `kubectl` and the AWS CLI: reset and the sweep over rates are manual steps.

## The trace

A trace of chat sessions that AIPerf replays at the timestamps in the file ([`--fixed-schedule`](https://github.com/ai-dynamo/aiperf/blob/v0.13.0/docs/cli-options.md), Mooncake format, one JSON line per request).
Read from the files in [benchmarks/traces/](../benchmarks/traces/):

- Every session request starts with the same 2,048 tokens (4 blocks of 512); the one-shot prompts below do not.
- A session's first request has 5,632 tokens and each next one adds 1,024, up to 15,872 tokens at the 11th request. The 5 and 10 req/s files stop at 14,848 (the 10th), because the cut at 615 s comes first. Output is 500 tokens (`ignore_eos`).
- About 2% of the rows (2.05% to 2.19% in these files) are one-shot prompts of 15,872 tokens that share nothing.
- One file per offered rate: 5, 10, 15, 20, 25, 30, 35, 40 req/s. Each file spans 615 s: session entries arrive in the first 180 s and continuations ramp up until 255 s; the measured window is [255, 615) s.

The generator of these files is not part of this repository. `SHA256SUMS` lists the checksum of each file and the Job verifies it.

## Run one point

```bash
NS=s-agg                                    # namespace of the cell (examples/README.md)
kubectl apply --server-side -f examples/00-baseline/manifest.yaml
# `kubectl wait` fails with "no matching resources found" until the operator has created the pods: check `kubectl -n "$NS" get pods` first.
kubectl -n "$NS" wait --for=condition=Ready pod --all --timeout=25m
```

1. Edit the env of [benchmarks/perf.yaml](../benchmarks/perf.yaml) (the layout of [NVIDIA's recipe](https://github.com/ai-dynamo/dynamo/blob/v1.6.1/recipes/qwen3.8-flash-next/perf/perf.yaml): one Job, one value of the load knob; here the rate, there the concurrency): `RATE` and `CELL` (S0 to S7) for each point, and once `RESULTS_BUCKET` and `AWS_REGION`, the keys of the same name in `manifests/config/platform.env`. While one of these three is empty or still a `<placeholder>`, the Job fails before it installs anything.
2. `kubectl -n "$NS" apply -f benchmarks/perf.yaml` and follow it with `kubectl -n "$NS" logs -f job/perf`. The pod runs as the ServiceAccount `perf` of the file; in each cell namespace Terraform binds it to a role that can only write under `perf/` of the results bucket ([terraform/cluster/iam-perf-results.tf](../terraform/cluster/iam-perf-results.tf)). On S0 at 10 req/s, installing AIPerf and fetching the trace took about 1 minute and AIPerf about 11.5 minutes, so the Job takes about 12.5 minutes plus the upload. Those runs installed no boto3 and uploaded nothing; the upload time of a full-length run was not measured ([Not tested](#not-tested)).
3. When the Job is Complete, copy the results with your own AWS credentials (`RESULTS_BUCKET` and `CELL` as in `perf.yaml`): `aws s3 sync "s3://$RESULTS_BUCKET/perf/$CELL/" ./results --region <region>`. Each run is in `rate-<RATE>/<stamp>/`, `<stamp>` being the UTC start of the AIPerf run. If the upload fails, the log shows the error and the pod stays up 30 minutes (`HOLD_SECONDS`) for `kubectl -n "$NS" cp <pod>:/results ./results`, then the Job fails.
4. `kubectl -n "$NS" delete job perf`.

The default `TRACE_BASE_URL` of `perf.yaml` points to `benchmarks/traces/` on the `main` branch of `aws-samples/sample-optimizing-inference-on-eks-self-hosted-models` on GitHub. That branch has the folder only once these files are merged there. Before that, on a fork or a copy, serve the folder yourself and set `TRACE_BASE_URL`. On 2026-11-07 we served `benchmarks/traces/` from a pod in the cell's namespace and set `TRACE_BASE_URL` to `http://trace-server:8080` in `perf.yaml`; these are the commands we ran, from the repository root (the `sha256sum -c SHA256SUMS` of the files in the pod printed OK for the 8 traces):

```bash
kubectl -n "$NS" run trace-server --restart=Never --image=public.ecr.aws/docker/library/python:3.12-slim \
  --overrides='{"spec":{"nodeSelector":{"karpenter.sh/nodepool":"cpu-demo"}}}' \
  --command -- sh -c 'mkdir -p /t && cd /t && exec python3 -m http.server 8080'
kubectl -n "$NS" wait --for=condition=Ready pod/trace-server --timeout=5m
kubectl -n "$NS" cp benchmarks/traces/. trace-server:/t
kubectl -n "$NS" expose pod trace-server --port=8080
```

Delete them with `kubectl -n "$NS" delete pod/trace-server service/trace-server` before the teardown: the pod runs on the `cpu-demo` node, and step 2 of `scripts/teardown.sh` deletes a `cpu-demo` NodeClaim only when its node runs DaemonSet pods only (from the script's printed plan; not run with the pod present).

## Reset between rates

The files of different rates reuse block ids (63% of the distinct ids of the 15 req/s file are also in the 10 req/s file), so a point run on warm caches can reuse what the previous point stored (not measured on the engine). NVIDIA says the same for its concurrency sweep: [restart the workers between runs so the KV and prefix caches are reset](https://github.com/ai-dynamo/dynamo/blob/v1.6.1/recipes/qwen3.8-flash-next/perf/perf.yaml#L11-L13). Either delete the worker pods (about 9 minutes to Ready in our P5en runs, the cold start of the whole cell), or flush the engines:

```bash
for p in $(kubectl -n "$NS" get pods -l nvidia.com/dynamo-graph-deployment-name=q38 -o name | grep -E 'worker|prefill|decode'); do
  kubectl -n "$NS" exec "$p" -c main -- curl -s -X POST -H 'Content-Type: application/json' -d '{}' http://127.0.0.1:9090/engine/flush_cache
done
```

Measured by us: on S0 every worker answered `"success":true`. Not verified: that the cache is empty afterwards. On HiCache cells (S2, S4, S6, S7) `flush_cache` does not clear L3 ([TROUBLESHOOTING.md](TROUBLESHOOTING.md#router-does-not-see-the-l3-tier)); the [Reset between runs](optimizations/hicache/README.md#reset-between-runs) section of the HiCache guide also posts `/engine/clear_hicache_storage` to the HiCache workers. We did not run that on these cells.

## Read the result

Results are in `results/rate-<RATE>/<stamp>/`. `profile_export_aiperf.json` has `goodput` and `good_request_fraction` for the whole run, with the SLO of `perf.yaml` (`GOODPUT`: TTFT at most 1000 ms and mean ITL at most 25 ms per request; [AIPerf definition](https://github.com/ai-dynamo/aiperf/blob/v0.13.0/docs/tutorials/goodput.md)). A request that errors is not good. This SLO applies TTFT <= 1000 ms to every request, the cold 15,872-token ones included. To count only the measured window (after the first 255 s):

```bash
jq -s '(map(.metadata.request_start_ns)|min) as $t0
 | map(select((.metadata.request_start_ns-$t0)/1e9 >= 255 and (.metadata.request_start_ns-$t0)/1e9 < 615))
 | {requests: length, good: (map(select((.metrics.time_to_first_token.value // 1e12) <= 1000 and (.metrics.inter_token_latency.value // 1e12) <= 25)) | length)}' \
  results/rate-10/<stamp>/profile_export.jsonl
```

Raise `RATE` until the good fraction drops; the knee of a cell is where it does. Then `kubectl -n "$NS" delete dgd q38` and apply the next cell.

## Not tested

- The default trace URL of `perf.yaml` (`raw.githubusercontent.com`, branch `main`), which serves the traces only once these files are merged there ([Run one point](#run-one-point)): our runs served the same files from an HTTP server inside the cluster. `SHA256SUMS` comes from the same place as the traces, so it catches corruption, not tampering. What the Job does with the default URL was not run.
- `perf.yaml` ran in full on S0 only, at 5, 10 and 15 req/s; the other rates and cells did not run in full. The manifest was applied with the node pool name changed from `gpu-b200-cb` to `gpu-h200-cb` (P5en).
- That `flush_cache` empties the caches (the answer `"success":true` does not show it), the reset by deleting pods, and any reset on the PD and HiCache cells.
- The 30-minute hold after a failed upload, and the upload time of a full-length run. On 2026-11-07, S0 at 5 req/s uploaded 8 objects (25,862,064 bytes) with the role and the Pod Identity associations that `terraform apply` of `terraform/cluster/iam-perf-results.tf` created. What ran on 2026-11-06, with the role and associations created by hand with the same permissions: this `perf.yaml` on a cluster with a 40-second part of the 5 req/s trace (87 requests) against S7. It uploaded 8 files (about 1.4 MB) in about 1 second. A separate pod with the same ServiceAccount uploaded a 20 MiB object in multiple parts and was denied writes outside `perf/`, reads, listing, deletes and writes to another bucket. A pod with the default ServiceAccount got no credentials.
- The `jq` count on requests that failed, and a cluster without internet: the Job pulls from Debian, PyPI, Hugging Face and GitHub.
- Output length is forced with `ignore_eos`; the effect on ITL against a free-running model was not measured.
