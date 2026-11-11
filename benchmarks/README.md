# Benchmarks

- `perf.yaml`: the Kubernetes Job that replays one trace with AIPerf against a cell and uploads the results to the
  results bucket. One Job is one rate.
- `traces/`: the benchmark trace, one file per rate, and `SHA256SUMS`, which the Job checks.

`ENDPOINT`, `TARGET_MODEL` and `MODEL_REVISION` in `perf.yaml` are set for the Qwen3.8-27B-FP8 cells of this sample (the
DynamoGraphDeployment `q38`); change them only for another deployment. `CELL`, `RESULTS_BUCKET` and `AWS_REGION` are
placeholders you set before the first run. The steps to run it, reset between rates and read the result are in
[docs/BENCHMARKING.md](../docs/BENCHMARKING.md).

## Use a trace

The number in the file name is the offered rate in req/s. `perf.yaml` downloads the trace of its `RATE`, checks it and
replays it; to take a trace by hand, check it and unzip it:

```bash
cd benchmarks/traces
grep ' trace-r10.jsonl.gz$' SHA256SUMS | sha256sum -c -
gunzip -k trace-r10.jsonl.gz
```

Then run the `aiperf profile` command of `perf.yaml` with `--input-file` pointing at the unzipped file. The flags that
make AIPerf send each request at its `timestamp` are `--custom-dataset-type mooncake_trace --fixed-schedule
--fixed-schedule-auto-offset`.
