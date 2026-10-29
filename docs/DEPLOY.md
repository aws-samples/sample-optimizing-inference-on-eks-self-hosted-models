# Deploy guide

The full procedure behind the quick start of the [README](../README.md): buckets, cluster, generated env, model staging,
prefetch, one cell, the matrix, Grafana and the teardown. Steps 1 to 4 were run end to end in a new region with
on-demand GPU types and small public and gated models; the cells and the matrix ran on an earlier version of this
stack (one `p6-b200.48xlarge` on a Capacity Block). When something fails, see [TROUBLESHOOTING.md](TROUBLESHOOTING.md).

Run every command from the repository root unless a step says otherwise.

## Prerequisites

- **Tools.** Terraform 1.12 or newer (1.17.0 tested and recommended), the AWS CLI v2, `kubectl`, `python3`, `uv`, `jq`,
  and `docker buildx` for the bench image (step 7 only); `crane` or `skopeo` only for the optional image mirror.
- **IAM.** The identity that runs Terraform creates VPC, EKS, IAM roles and policies, S3, ECR, CloudWatch Logs and Helm
  releases. `terraform plan` also calls `ec2:DescribeInstanceTypes`, `ec2:DescribeInstanceTypeOfferings` and
  `ec2:DescribeCapacityReservations`. That identity becomes cluster admin through an EKS access entry. The workloads use
  EKS Pod Identity only: there are no static AWS keys anywhere.
- **GPU capacity: you buy the capacity.** Terraform never buys, reserves or extends it. Pick one purchase model per stack:

  | `gpu_capacity_type` | What you do first | Notes |
  |---|---|---|
  | `capacity-block` | Buy an EC2 Capacity Block for ML and pass its ID | The tested setup. The AZ and the pool size come from the block. It is paid up front, and EC2 reclaims the instance before the block ends. |
  | `odcr` | Create a targeted On-Demand Capacity Reservation and pass its ID | Also set `gpu_az_ids` and `gpu_max_nodes`. It bills whether or not a node runs. |
  | `on-demand` | Nothing | The node starts when a pod needs it; P and G capacity is not guaranteed. |
  | `spot` | Nothing | Scarce for P and G types, and the workers do not handle interruptions. |

- **Tested shape.** The manifests select the pool `gpu-b200-cb` (`p6-b200.48xlarge` on a Capacity Block) and 8 GPUs per
  node. Other types work in Terraform, but the pods stay Pending until you adapt their node selectors and GPU counts.
- **Runtime image.** The Frontend and the workers run the public NGC image
  `nvcr.io/nvidia/ai-dynamo/sglang-runtime:1.6.0-efa@sha256:d574951b0dbd037bdcaabe121431a83cd047a5c106cb243fe6f2f97f2e456cd2`
  (SGLang 0.5.21, Dynamo 1.6.0, NIXL 1.5.0, EFA 1.50.0), pinned in `versions.env`; the pull needs no NGC login. As with
  any third-party image, scan it with your own tools before you use it. The recorded results were measured with a
  derived SGLang 0.5.22 build; HiCache restore correctness was validated on both versions. Every component sets
  `runtimeVersionOverride: "1.6.0"` so that the 1.5.1 operator runs the 1.6.0 runtime. With this pinned image the
  override is optional (the operator admits the components without it, because it can parse the `1.6.0-efa` tag); the
  cells keep it.

## 1. Buckets (`terraform/bootstrap`)

```bash
cd terraform/bootstrap
cp terraform.tfvars.example terraform.tfvars   # same aws_region, project_name and cluster_name as the cluster
terraform init && terraform apply              # about 1 minute
cd ../..
```

It creates the model and results buckets (encrypted, Block Public Access, TLS only; the results bucket is versioned).
The state is local. To keep it in S3, copy `backend.tf.example` to `backend.tf` first, in this folder and in
`terraform/cluster`.

## 2. Cluster (`terraform/cluster`)

Copy `terraform/cluster/terraform.tfvars.example` to `terraform/cluster/terraform.tfvars` and set at least these keys:

```hcl
aws_region               = "<region>"
eks_public_access_cidrs  = ["203.0.113.10/32"]   # your egress IPs; a /0 is refused
gpu_instance_types       = ["p6-b200.48xlarge"]
gpu_capacity_type        = "capacity-block"      # capacity-block | odcr | on-demand | spot
capacity_reservation_ids = ["cr-<17 hex>"]       # [] for on-demand and spot
model_id                 = "Qwen/Qwen3.8-27B-FP8"
model_revision           = "017b9c7af6b5689d5dd426a76e0bc077eb5ca20a"
runtime_image            = "nvcr.io/nvidia/ai-dynamo/sglang-runtime:1.6.0-efa@sha256:d574951b0dbd037bdcaabe121431a83cd047a5c106cb243fe6f2f97f2e456cd2"  # already in the example (versions.env)
```

```bash
terraform -chdir=terraform/cluster init
terraform -chdir=terraform/cluster apply                                   # about 20 minutes
eval "$(terraform -chdir=terraform/cluster output -raw configure_kubectl)"  # kubectl context = cluster name
```

The stack exports the three outputs that `scripts/gen_env.py` requires: `platform_env`, `sample_env` and `gpu_pools`.
Every variable is described in [terraform/README.md](../terraform/README.md). To pull the runtime image from your own
ECR instead of NGC, do the optional [mirror](#mirror-the-image-to-your-ecr-optional) before step 3.

## Mirror the image to your ECR (optional)

Optional. Pulled from NGC, the 14.8 GB image goes through the NAT gateway, which bills per GB, on every node that pulls
it. Pulled from a copy in ECR, the layers come from S3 through the S3 gateway endpoint of the VPC and only the small ECR
API calls cross the NAT gateway. The stack creates the `sglang-runtime` ECR repository for that copy only on request:
first set `create_runtime_mirror_repository = true` in `terraform/cluster/terraform.tfvars` and run
`terraform -chdir=terraform/cluster apply`. The apply creates the repository and its output
`sglang_runtime_ecr_repository_url` (without the variable the output does not exist, see
[TROUBLESHOOTING.md](TROUBLESHOOTING.md#ecr-mirror-repository)). Then copy the image from your workstation with a tool
that keeps the digest:

```bash
REPO="$(terraform -chdir=terraform/cluster output -raw sglang_runtime_ecr_repository_url)"
aws ecr get-login-password --region <region> | crane auth login "${REPO%%/*}" -u AWS --password-stdin
crane copy nvcr.io/nvidia/ai-dynamo/sglang-runtime:1.6.0-efa@sha256:d574951b0dbd037bdcaabe121431a83cd047a5c106cb243fe6f2f97f2e456cd2 "$REPO:1.6.0-efa"
crane digest "$REPO:1.6.0-efa"   # prints the same sha256:d574951b...
```

`skopeo copy --all --preserve-digests` works too. The repository has immutable tags, so use a tag that is not there
yet. Then set `runtime_image = "<repository>@sha256:<the same digest>"` in `terraform/cluster/terraform.tfvars`, run
`terraform -chdir=terraform/cluster apply` and continue with step 3.

## 3. Generated env (`scripts/gen_env.py`)

```bash
python3 scripts/gen_env.py
export KUBE_CONTEXT="$(terraform -chdir=terraform/cluster output -raw cluster_name)"
```

`gen_env.py` reads `terraform output -json` and writes `manifests/config/platform.env` and `config/sample.env`, both
gitignored. `config/sample.env` is generated and overwritten on every run, like `platform.env`: keep nothing of your
own in either file. It refuses an empty `RUNTIME_IMAGE`, and any GPU shape or model other than the tested one unless
you pass `--allow-untested`. When it refuses, it writes nothing. Run it again after every `terraform apply`. The scripts
refuse to run without `KUBE_CONTEXT`, and it must name the cluster of `platform.env`.

## 4. Stage the model (`scripts/10-stage-model.sh`)

The script runs the Job `node-ops/model-stage` on `cpu-demo`. It copies `MODEL_ID` at `MODEL_REVISION` from Hugging Face
to `MODEL_S3_URI` (weights, config, tokenizer and license files) and writes the `.done` marker last.

```bash
export STAGE_IMAGE=public.ecr.aws/docker/library/python:3.12-slim@sha256:<digest>
scripts/10-stage-model.sh --dry-run   # prints the ConfigMap and the Job, no cluster call
scripts/10-stage-model.sh             # applies the Job and waits for it
```

- `STAGE_IMAGE` must be pinned by digest; the script refuses its placeholder default. Resolve the digest once, for
  example with `docker buildx imagetools inspect public.ecr.aws/docker/library/python:3.12-slim`.
- Public models need no token: the Job never sends one to them.
- For a gated model, accept the license on the model page with the Hugging Face account that owns the token, then
  export the token in the shell that runs the script: `export HF_TOKEN=<read token>`. The script reads it from the
  process environment. Never write it to `config/sample.env`, which `gen_env.py` overwrites, nor to any tracked file.
  The token reaches the cluster only as an ephemeral Secret that the script deletes when it exits. The Secrets
  Manager option is `hf_token_secret_arn` in the cluster tfvars: Terraform grants read access on that secret to the
  model-prefetch role only. The staging Job reads `HF_TOKEN` only.
- One model per apply: the staging role may write only under the prefix that `terraform/cluster` applied. To stage
  another model or revision, change `model_id` and `model_revision`, run `terraform apply` and `scripts/gen_env.py`,
  then this script.
- The Job is idempotent: a `.done` of the same revision ends it at once, and files already in S3 are skipped.
- Exit codes: [TROUBLESHOOTING.md](TROUBLESHOOTING.md#model-staging-exit-codes).

## 5. Prefetch the weights to the GPU node (`scripts/11-prefetch.sh`)

```bash
scripts/11-prefetch.sh --wait
```

The Job `node-ops/model-prefetch` runs on the GPU node, copies the model from S3 to the local NVMe at
`/mnt/.ephemeral/models` and writes the marker that the workers' init container waits for. Submitting it starts the GPU
node, so your reservation must be active. It reads `MODEL_ID` and `MODEL_REVISION` from the generated env; `--force`
copies again.

## 6. Deploy one cell (`examples/`)

```bash
kubectl --context "$KUBE_CONTEXT" apply --server-side -k examples/00-baseline
kubectl --context "$KUBE_CONTEXT" -n s-agg get pods -w
```

| Folder | Cell | Namespace | What it adds |
|---|---|---|---|
| `examples/00-baseline` | S0 | `s-agg` | 8 aggregated TP1 workers, round-robin |
| `examples/01-kv-router` | S1 | `s-agg` | R: the KV router |
| `examples/02-hicache` | S2 | `s-agg-hic` | O: HiCache L2 + L3 |
| `examples/03-pd-disaggregation` | S3 | `s-dis` | D: 4 prefill + 4 decode workers |
| `examples/04-all-three` | S7 | `s-dis-hic` | R + O + D |
| `examples/05-kv-router-hicache` | S4 | `s-agg-hic` | R + O |
| `examples/06-kv-router-pd` | S5 | `s-dis` | R + D |
| `examples/07-pd-hicache` | S6 | `s-dis-hic` | O + D |

Only one cell fits on the GPU node: run `kubectl delete -k examples/<folder>` before you apply the next one. On a
running cell, `scripts/50-toggle-router.sh kv s-agg` flips R without restarting the workers. Each folder's README
says what the technique changes.

## 7. Run the matrix (`tools/matrix_driver`)

The driver runs a plan (cells and repetitions) as a Job in the `bench` namespace, resets the caches before each run,
replays the trace with aiperf and uploads the results to the results bucket. Its full description is in
[tools/matrix_driver/README.md](../tools/matrix_driver/README.md).

1. **Bench image.** Build it from the repository root, push it to `BENCH_REPO` (from `platform.env`), put its tag and
   digest in `manifests/track-q-qwen38/driver/images.env` and its digest in `manifests/bench/deployment.yaml`:

   ```bash
   docker buildx build -f tools/bench-image/Dockerfile --platform linux/amd64 --build-arg VCS_REF="$(git rev-parse HEAD)" -t <BENCH_REPO>:<tag> --push .
   ```

   Keep `--platform linux/amd64`: the bench pods run on the `cpu-demo` nodes, which are amd64 only, and without it a Mac
   with Apple silicon builds an arm64 image. The build needs BuildKit (`docker buildx`, or `buildctl` in a cluster):
   `tools/bench-image/Dockerfile` uses a heredoc `COPY`, `RUN --mount` and the automatic `TARGETARCH`. The legacy
   builder and the builders that parse the file themselves instead of through BuildKit fail on it; one in-cluster
   builder stopped with `TARGETARCH: parameter not set`. The first line of the file pins its BuildKit syntax frontend,
   `docker/dockerfile:1`, by digest.

2. **Inputs.** Before its first run the driver downloads, from the S3 prefixes in the header of its plan, the trace of
   the rate and its sidecar, each checked against the sha256 that the plan pins, and the tokenizer files of the model
   revision without the weights, checked against `sums_sha256`, the sha256 of their `SHA256SUMS`. aiperf runs offline
   in the cluster and only tokenizes, so these files are all it needs, and the pinned hashes make every run replay the
   trace of the recorded runs: the driver stops before the first cell when one of them differs.

   Each rate, 10, 25 or 32 req/s, has two plans in `plans/w3-16k` that read the same inputs: `plan-w3-r<rate>.txt`
   runs S0 and S7, and `plan-w3-r<rate>-cells.txt` runs S1 to S6. The commands below use rate 25, like step 3. Run
   them from the repository root, with [uv](https://docs.astral.sh/uv/) installed (`regen.sh` runs `demo-bench`
   through it) and AWS credentials that can read the model bucket and write to the results bucket. The first line
   defines `RESULTS_BUCKET`, `AWS_REGION` and `MODEL_S3_URI` from `manifests/config/platform.env` and
   `config/sample.env`, the two files that `scripts/gen_env.py` generated in section 3. They exist only after an apply
   that finished: after a failed one, fix its error and re-run it first
   ([TROUBLESHOOTING.md](TROUBLESHOOTING.md#manifestsconfigplatformenv-is-missing)):

   ```bash
   set -a; source manifests/config/platform.env; source config/sample.env; set +a
   W3_RATE=25                                 # 10, 25 or 32
   TRACE_DIR="$HOME/w3-16k/trace-r$W3_RATE"   # regen.sh writes the trace, its sidecar and their SHA256SUMS here
   MODEL_DIR="$HOME/w3-16k/model"             # the files of the staged model, without the weights
   mkdir -p "$TRACE_DIR" "$MODEL_DIR"
   workloads/w3-16k/regen.sh "$W3_RATE" "$TRACE_DIR"
   aws s3 cp --recursive "$MODEL_S3_URI/" "$MODEL_DIR/" --exclude '*.safetensors' --exclude '*.bin' --region "$AWS_REGION"
   workloads/w3-16k/upload-inputs.sh "$W3_RATE" "$TRACE_DIR" "$MODEL_DIR"        # dry run: prints the aws commands
   workloads/w3-16k/upload-inputs.sh --yes "$W3_RATE" "$TRACE_DIR" "$MODEL_DIR"  # uploads
   ```

   `upload-inputs.sh` reads the destinations from `plans/w3-16k/plan-w3-r<rate>.txt` and the bucket from
   `RESULTS_BUCKET`, and uploads nothing unless the trace and the sidecar match the sha256 that the plan pins. It
   uploads the tokenizer files with a `SHA256SUMS` and prints `sums_sha256=<sha256>` of that file.

   > **Check `sums_sha256` before step 3.** If the `sums_sha256` that `upload-inputs.sh` prints differs from the one
   > in `plans/w3-16k/plan-w3-r<rate>.txt`, put the printed value on the `tokenizer prefix=... sums_sha256=...` line
   > of both `plan-w3-r<rate>.txt` and `plan-w3-r<rate>-cells.txt`, then go to step 3. The value in the plans belongs
   > to the file set of the recorded runs, so yours very likely differs (the script warns when it does); with the old
   > value the driver refuses the tokenizer.

   The `tokenizer prefix=` and the `sums_sha256` of the shipped plans belong to the model of the recorded runs, the Track
   Q model `Qwen/Qwen3.8-27B-FP8` at revision `017b9c7a…`: the prefix ends in `tokenizer-qwen38-27b-fp8-017b9c7a/`.
   `upload-inputs.sh` uploads the tokenizer of the model in `MODEL_DIR`, whatever it is, under that prefix. With another
   staged model, first rename the prefix on the `tokenizer` line of both plans of the rate after that model and
   revision (for example `tokenizer-<model>-<first 8 characters of the revision>/`), then run `upload-inputs.sh` and put
   the `sums_sha256` it prints on the same line: for another model it always differs. The trace and the sidecar do not
   depend on the model.

3. **Render and launch.** The `hard_stop` of the shipped plans is in the past, so always pass `--hard-stop-in`:

   ```bash
   uv build tools/demo_bench --wheel --out-dir dist
   python3 tools/matrix_driver/render_driver.py --out out --wheel dist/<demo_bench wheel>
   kubectl apply --server-side -f out/00-base.yaml -f out/20-watchdog.yaml
   python3 tools/matrix_driver/render_driver.py --out out --launch plan-w3-r25 --cb-node <GPU instance id> --hard-stop-in 8h
   kubectl apply --server-side -f out/08-plans-plan-w3-r25-<timestamp>.yaml -f out/20-watchdog.yaml
   kubectl create -f out/10-job-plan-w3-r25-<timestamp>.yaml
   ```

   `--cb-node` is the EC2 instance ID of the GPU node (the end of its `spec.providerID`). `plan-w3-r10`, `plan-w3-r25`
   and `plan-w3-r32` run S0 and S7; the `-cells` plans run S1 to S6. The eight cells map one-to-one to the eight
   examples, `examples/00-baseline` to `examples/07-pd-hicache`. A watchdog CronJob tears the cells down at the
   hard stop even if the driver hangs.

## 8. Observe (Grafana)

```bash
kubectl --context "$KUBE_CONTEXT" -n monitoring port-forward svc/prometheus-grafana 3000:80
kubectl --context "$KUBE_CONTEXT" -n monitoring get secret prometheus-grafana -o jsonpath='{.data.admin-password}' | base64 -d
```

Open `http://localhost:3000` and log in as `admin`. `kubectl port-forward` keeps Grafana private; the CloudFront + WAF
edge (`enable_grafana_edge`) is off by default. Its hop from CloudFront to the internal NLB is plain HTTP, so turning
it on also needs `grafana_edge_allow_plain_http_origin = true`: see
[terraform/README.md](../terraform/README.md#grafana). Terraform loads the dashboards of `dashboards/`: the demo matrix and
compare views, Dynamo, SGLang and DCGM. [dashboards/README.md](../dashboards/README.md) describes each panel.

## Teardown

`scripts/90-teardown.sh` runs the order below. It is a dry run by default: it prints every command and runs nothing
until you pass `--yes`. It needs `KUBE_CONTEXT` set to the cluster of `manifests/config/platform.env`, and it takes the
GPU pools, the region, the bucket names and the ECR repositories from that generated file only. After a failed or
partial apply that file does not exist: see [After a failed or partial apply](#after-a-failed-or-partial-apply).

```bash
scripts/90-teardown.sh                          # dry run of steps 1 to 4
scripts/90-teardown.sh --yes                    # steps 1 to 3, then prints the cluster destroy command
scripts/90-teardown.sh --destroy-cluster --yes  # steps 1 to 4; Terraform still asks before it destroys
scripts/90-teardown.sh --after-destroy --yes    # step 5 for the results bucket, then prints step 6
```

- Step 2 deletes the NodeClaims of every pool in `GPU_POOLS` (or of each `--pool gpu-<name>`) and each `cpu-demo`
  NodeClaim whose node runs only DaemonSet pods. It then waits up to `--timeout` seconds (default 1800) until no GPU
  node is left, and stops before the destroy if one stays.
- Step 3 deletes every image of the bench repository, `BENCH_REPO`, and of the mirror, `SGLANG_RUNTIME_REPO` when it is
  set, so `--yes` also deletes the bench image of section 7 and your copy of the runtime image: build or mirror them
  again if you keep the stack. The repositories keep `force_delete = false` on purpose, so that Terraform never deletes
  an image on its own: a destroy, or a replacement after a change, stops with `RepositoryNotEmptyException` while a
  repository holds an image. The script lists the images with `aws ecr list-images` and deletes them by digest with
  `aws ecr batch-delete-image`, at most 100 per call, until the listing is empty. It touches no other repository: it
  refuses a value that is not `<ECR_REGISTRY>/<project_name>/bench` (or `<ECR_REGISTRY>/<project_name>/sglang-runtime`),
  `ECR_REGISTRY` being `<account>.dkr.ecr.<region>.amazonaws.com` of the region of the run. A repository that does not
  exist is skipped; any other error stops the run before the destroy.
- `--after-destroy` empties the results bucket, every object version and delete marker included, in batches of at
  most 1000. Add `--empty-models` to empty the model bucket too: restaging the weights takes time. The script never runs
  the bootstrap destroy, and at the end it prints the checks of the last paragraph.

To do it by hand, keep this order, or the destroy stalls on resources that Terraform does not own or cannot delete:

1. Delete the cells: `kubectl delete -k examples/<folder>` for each one you applied, and any DGD the driver left in
   `s-agg`, `s-agg-hic`, `s-dis` and `s-dis-hic`.
2. Delete the GPU NodeClaims, `kubectl delete nodeclaims -l karpenter.sh/nodepool=<pool>`, so no GPU node and no EFA
   ENI is left. Delete any extra `cpu-demo` NodeClaim too: that pool never consolidates.
3. Empty the ECR repositories: `<project_name>/bench` (`BENCH_REPO`, after the image push of section 7) and, with
   `create_runtime_mirror_repository = true`, `<project_name>/sglang-runtime` (`SGLANG_RUNTIME_REPO`). With
   `force_delete = false` the destroy fails with `RepositoryNotEmptyException` while one holds an image. One
   `batch-delete-image` call takes at most 100 images, so repeat the two lines until `list-images` returns no image. A
   child image of a multi-platform index fails with `ImageReferencedByManifestList` until its index is gone: the next
   round deletes it.

   ```bash
   BATCH="$(mktemp)"   # one batch of image digests
   aws ecr list-images --repository-name <project_name>/bench --region <region> --output json \
     | jq '[.imageIds[].imageDigest] | unique | map({imageDigest: .}) | .[:100]' >"$BATCH"
   aws ecr batch-delete-image --repository-name <project_name>/bench --region <region> --image-ids "file://$BATCH"
   ```

4. Destroy the cluster: `(cd terraform/cluster && terraform destroy)`.
5. Empty both buckets: `aws s3 rm s3://<bucket> --recursive` deletes the current objects. The results bucket is
   versioned, so also delete its object versions and delete markers (the S3 console "Empty" action does both). The
   buckets have `force_destroy = false`.
6. Destroy the buckets: `(cd terraform/bootstrap && terraform destroy)`, only when you no longer need the weights and the
   results.

Until step 4 ends, the idle cluster bills: the EKS control plane, the NAT gateway (per hour), its public IPv4 address,
the `cpu-demo` and system nodes and their EBS volumes. Without a GPU node that is about US$ 0.43 per hour in us-east-2,
where the stack was tested, at On-Demand list prices as of 2026-10-27 from the AWS Price List API (pricing pages:
[EKS](https://aws.amazon.com/eks/pricing/), [VPC](https://aws.amazon.com/vpc/pricing/),
[EC2 On-Demand](https://aws.amazon.com/ec2/pricing/on-demand/), [EBS](https://aws.amazon.com/ebs/pricing/)). The
figure excludes data transfer (the NAT gateway also bills per GB processed), GPU nodes, S3, ECR, KMS, CloudWatch Logs
and the optional Grafana edge:

| Component (us-east-2, On-Demand, 2026-10-27) | Rate (US$) | US$ per hour |
|---|---|---|
| EKS control plane, standard support | 0.10 per cluster-hour | 0.100 |
| System node `c6g.large` (Auto Mode built-in `system` pool): EC2 + Auto Mode management fee | 0.068 + 0.00816 per hour | 0.076 |
| `cpu-demo` node `c6a.xlarge`: EC2 + Auto Mode management fee | 0.153 + 0.01836 per hour | 0.171 |
| NAT gateway (one, `single_nat_gateway = true`) | 0.045 per hour | 0.045 |
| Public IPv4 address of the NAT gateway | 0.005 per hour | 0.005 |
| EBS gp3 at the baseline 3,000 IOPS and 125 MB/s: the 256 GiB data volume of the `cpu-demo` node and the 50 GiB Prometheus volume | 0.08 per GB-month × 306 GiB ÷ 730 hours | 0.034 |
| **Total** | | **0.431** |

Auto Mode picks the instance types: the test deployments got `c6g.large` and `c6a.xlarge`, and the `cpu-demo` pool
accepts c, m and r types with 4, 8 or 16 vCPUs. The table leaves out the volumes Auto Mode sizes itself (the root
volumes and the data volume of the system node). For another region or other types, use the
[AWS Pricing Calculator](https://calculator.aws/). A Capacity Block or an ODCR bills until it ends, with or without a
node. After the destroy, check that no ENI is left in the VPC and that the 50 GiB Prometheus volume is gone.

### After a failed or partial apply

An apply of `terraform/cluster` that stops on an error can leave a running cluster without the `platform_env` output.
`gen_env.py` then writes no env file, and the script refuses to run. If you can, fix the error and re-run the apply.
To tear the partial stack down instead, pass `--from-terraform`: the script reads the cluster name, the region, the GPU
pools, the bucket names and the ECR repositories (`bench_ecr_repository_url`, `sglang_runtime_ecr_repository_url`)
from `terraform output -json` of `terraform/cluster` and, with `--after-destroy`, of `terraform/bootstrap`. These
reads are the only commands its dry run runs. When no output names a repository, step 3 empties none: if
`aws ecr list-images` shows images in one, delete them with the commands of step 3 above. The cluster destroy removes the cluster
outputs, so the `--after-destroy` run takes the cluster name and the region from `TEARDOWN_CLUSTER_NAME` and
`TEARDOWN_AWS_REGION`; the first run prints both. `TEARDOWN_RESULTS_BUCKET` and `TEARDOWN_MODEL_BUCKET` work the same
way if no bootstrap state is in your checkout. The script reads these variables only with `--from-terraform`, stops
when two sources name different values, and refuses to run when nothing names the cluster.

```bash
scripts/90-teardown.sh --from-terraform                          # dry run of steps 1 to 4
scripts/90-teardown.sh --from-terraform --destroy-cluster --yes  # steps 1 to 4; Terraform still asks before it destroys
export TEARDOWN_CLUSTER_NAME=<cluster name> TEARDOWN_AWS_REGION=<region>   # both printed by the previous run
scripts/90-teardown.sh --after-destroy --from-terraform --yes    # step 5, bucket names from terraform/bootstrap
```

To empty the buckets by hand, take their names from `terraform -chdir=terraform/bootstrap output`. Delete the current
objects of the results bucket, then its object versions and its delete markers (`Versions` and `DeleteMarkers` of the
listing). One `delete-objects` call takes at most 1000 of them, so repeat the `list-object-versions` and
`delete-objects` lines until the listing holds neither. The model bucket is not versioned:

```bash
BATCH="$(mktemp)"   # one batch of versions and delete markers
aws s3 rm "s3://<results bucket>" --recursive --region <region>
aws s3api list-object-versions --bucket <results bucket> --region <region> --output json \
  | jq '{Objects: [(.Versions // [])[], (.DeleteMarkers // [])[] | {Key, VersionId}][:1000], Quiet: true}' >"$BATCH"
aws s3api delete-objects --bucket <results bucket> --region <region> --delete "file://$BATCH"
aws s3 rm "s3://<model bucket>" --recursive --region <region>
```
