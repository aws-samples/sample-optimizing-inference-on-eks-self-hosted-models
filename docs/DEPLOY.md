# Deploy guide

The full procedure behind the quick start of the [README](../README.md): buckets, cluster, generated env, model staging,
prefetch, one cell, Grafana and the teardown. When something fails, see [TROUBLESHOOTING.md](TROUBLESHOOTING.md).

Run every command from the repository root unless a step says otherwise.

## Prerequisites

- **Tools.** Terraform >= 1.12 (tested with 1.12.2 and 1.17.0), the AWS CLI v2, `kubectl`, `python3`, `jq` and
  [`crane`](https://github.com/google/go-containerregistry/blob/v0.21.3/cmd/crane/README.md) (image digest and size).
  `terraform apply` downloads its Helm charts from helm.ngc.nvidia.com, prometheus-community.github.io and
  nvidia.github.io (`terraform/cluster/dynamo-platform.tf`, `terraform/cluster/observability.tf`).
- **IAM.** Terraform, the `aws eks get-token` of its providers and the scripts use the AWS identity your shell resolves:
  select it before step 1 (`aws sts get-caller-identity` prints it). It creates the VPC (one NAT gateway, an S3 gateway
  endpoint), the EKS cluster with its add-ons, IAM roles and policies, the two S3 buckets and the CloudWatch Logs group
  of the EKS audit log; with `enable_grafana_edge = true`, also CloudFront and AWS WAF resources. `terraform plan`
  also calls `ec2:DescribeInstanceTypes`, `ec2:DescribeInstanceTypeOfferings` and `ec2:DescribeCapacityReservations`
  (`terraform/cluster/gpu-discovery.tf`). That identity becomes cluster admin through an EKS access entry
  (`terraform/cluster/eks.tf`). The workloads get AWS credentials from EKS Pod Identity only: no file holds AWS keys.
- **GPU capacity: you buy the capacity.** Terraform never buys, reserves or extends it. Pick one purchase model per stack:

  | `gpu_capacity_type` | What you do first | Notes |
  |---|---|---|
  | `capacity-block` | Buy an EC2 Capacity Block for ML and pass its ID | The pool the examples select is of this type. See the paragraph under this table. |
  | `odcr` | Create a targeted On-Demand Capacity Reservation and pass its ID | Also set `gpu_az_ids` and `gpu_max_nodes`. It bills whether or not a node runs ([pricing](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/capacity-reservations-pricing-billing.html)). |
  | `on-demand` | Nothing | The node starts when a pod needs it, if EC2 has free capacity ([`InsufficientInstanceCapacity`](TROUBLESHOOTING.md#gpu-node-never-starts-insufficientinstancecapacity)). Karpenter removes it once it has been empty for 15 minutes ([capacity types](../terraform/README.md#capacity-types)), and a new node needs step 5 (prefetch) again. Whether the helper pod of step 5 keeps the node: not tested. |
  | `spot` | Nothing | EC2 can reclaim the node at any time ([Spot interruptions](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/spot-interruptions.html)); a new node needs step 5 (prefetch) again. |

  From the EC2 documentation: Capacity Blocks exist for some instance types in some Regions only
  ([supported types and Regions](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/ec2-capacity-blocks.html#capacity-blocks-prerequisites)).
  You buy one for a start date up to 8 weeks ahead
  ([how Capacity Blocks work](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/capacity-blocks-how.html)), pay it
  up front and cannot cancel it, and it ends at 11:30 UTC
  ([find and purchase](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/capacity-blocks-purchase.html)). Its
  instances start terminating 30 minutes before the end time
  ([how Capacity Blocks work](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/capacity-blocks-how.html)), and its
  capacity is available only in the state `active`
  ([states](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/capacity-blocks-view.html)). Buy it in the Region of
  `aws_region`, for a type of `gpu_instance_types`. In this stack, `terraform plan` reads the block
  (`ec2:DescribeCapacityReservations`), refuses one of another type and accepts one that is still `scheduled`
  (`terraform/cluster/modules/gpu-pools/`). The AZ of the GPU nodes and the pool size come from the block
  ([what Terraform derives](../terraform/README.md#the-minimum-tfvars)), and it must be `active` when step 5 (prefetch)
  starts the GPU node.
- **Node pool.** The examples (`examples/*/manifest.yaml`), the prefetch Job and its helper pod (`manifests/node-ops/`)
  select `karpenter.sh/nodepool: gpu-b200-cb`: the pool Terraform names for `p6-b200.48xlarge` on a Capacity Block,
  8 GPUs per node. The name is `gpu-<gpu>-<cb|odcr|od|spot>` ([GPU types](../terraform/README.md#gpu-types)), so another
  GPU type or `gpu_capacity_type` gives another pool, for example `gpu-h200-cb` for `p5en.48xlarge` on a Capacity Block;
  step 2 prints it. With another pool, pass `--allow-untested` to `scripts/gen_env.py` in step 3, replace the pool name
  in `manifests/node-ops/` before step 5 (prefetch) with
  `(cd manifests/node-ops && sed -i.bak 's/gpu-b200-cb/<pool>/' *.yaml && rm *.bak)` (the prefetch ran on `gpu-h200-cb` on
  2026-11-07; other pools are not tested; `git checkout manifests/node-ops` restores the files), and apply each example with the name replaced in
  step 6. The examples run 8 workers of one GPU each, so on a node with fewer GPUs some workers stay Pending.
- **Runtime image.** The Frontend and the workers run the public NGC image
  `nvcr.io/nvidia/ai-dynamo/sglang-runtime:1.6.1-efa@sha256:bc30725d441e32406f2ee237bb3f0b720b105ca3b7f0c43b3d68918a23a5a954`
  (SGLang 0.5.21, Dynamo 1.6.1, NIXL 1.5.0, EFA 1.50.0), pinned in `versions.env`. The examples set no image pull
  secret. The `linux/amd64` layers of the image, what a node downloads, add up to 14,808,498,029 bytes (about 14.8 GB;
  measured by us on 2026-11-06 with this command, which reads the registry with no login):

  ```bash
  DOCKER_CONFIG="$(mktemp -d)" crane manifest --platform linux/amd64 "$(sed -n 's/^RUNTIME_IMAGE=//p' versions.env)" | jq '[.layers[].size] | add'
  ```

  The GPU nodes sit in private subnets (`terraform/cluster/modules/gpu-pools/main.tf`), so the pull from NGC goes
  through the NAT gateway (inferred, not tested), which bills per GB ([VPC pricing](https://aws.amazon.com/vpc/pricing/)).
  As with any third-party image, scan it with your own tools before you use it. Every component of the examples sets
  `runtimeVersionOverride: 1.6.1`, the `DYNAMO_RUNTIME_VERSION` of `versions.env`; without it, admission reads the
  runtime version from the image tag, which must be a semantic version
  ([Dynamo v1.6.1](https://github.com/ai-dynamo/dynamo/blob/v1.6.1/docs/fern/pages/developer-guide/advanced-customizations/writing-custom-backends/runtime-containers.md#L72-L80)).
  Running without the override is not tested. The tfvars key `runtime_image` only fills `RUNTIME_IMAGE` of
  `platform.env`: the examples carry the image in each manifest.

## Security notes

What the examples and the Terraform stacks allow, as shipped. This is not a hardening guide.

- **Containers.** No file in `examples/`, `manifests/` or `terraform/` sets `privileged: true`, `hostPID`, `hostIPC` or
  `hostNetwork`. The frozen test references `tests/manifests/fixtures/cells_reference/S3.yaml`, `S5.yaml`, `S6.yaml` and
  `S7.yaml` still carry `privileged: true`; nothing applies them. The `prometheus-node-exporter` of the
  `kube-prometheus-stack` chart (version 69.7.4, `terraform/cluster/variables.tf`) defaults to `hostNetwork: true` and
  `hostPID: true`, and Terraform does not change them, so those pods run in `monitoring`. The
  prefill and decode workers of the PD examples (03, 05, 06, 07) add no capabilities; the aggregated workers (00, 01, 02,
  04) add `IPC_LOCK` and `SYS_RESOURCE`. The workers and the `weights-gate` init container run as root
  (`runAsUser: 0`). `dcgm-exporter` runs as root with `SYS_ADMIN`: `terraform/cluster/observability.tf` keeps the default of the chart
  (version 4.8.4), whose
  [values.yaml](https://github.com/NVIDIA/dcgm-exporter/blob/4.8.4/deployment/values.yaml) sets `runAsUser: 0` and
  `capabilities.add: ["SYS_ADMIN"]`. That the PD workers work without `privileged` is one observation on one
  `p5en.48xlarge` node, against Dynamo's EFA guide:
  [Privileged pods](optimizations/pd-disaggregation/README.md#privileged-pods).
- **Namespaces.** The examples mount `hostPath` volumes: the weights (`/mnt/.ephemeral/models`), the whole `/mnt/.ephemeral` in the
  `weights-gate` init container (read-only) and, in 02, 04, 06 and 07, the HiCache L3 directory under
  `/mnt/.ephemeral/hicache-l3`. The prefetch Job and the helper pod in `manifests/node-ops` run as root with
  `/mnt/.ephemeral/models` and `/mnt/.ephemeral` (read-write) mounted. The compile cache is an `emptyDir`. The
  [Pod Security Standards](https://kubernetes.io/docs/concepts/security/pod-security-standards/) forbid `hostPath` at
  the Baseline level, so Terraform creates `s-agg`, `s-agg-hic`, `s-dis`, `s-dis-hic`, `node-ops` and `monitoring` at
  the `privileged` level and `dynamo-system` at `baseline` (`terraform/cluster/namespaces.tf`). The examples do not run
  in a namespace at the Baseline or Restricted level. Replacing the `hostPath` volumes was not tested.
- **Network.** The repository creates no `NetworkPolicy`; in Kubernetes a pod that no NetworkPolicy selects is
  non-isolated ([network policies](https://kubernetes.io/docs/concepts/services-networking/network-policies/)). The
  Frontend Service is created by the Dynamo operator, not by these manifests (`kubectl get svc -n <namespace>` shows it);
  in our run of 2026-11-07 it was a ClusterIP Service on port 8000, an output not kept in this repository. The manifests
  configure no authentication in front of it (read from the manifests, not tested).

## 1. Buckets (`terraform/bootstrap`)

```bash
cd terraform/bootstrap
cp terraform.tfvars.example terraform.tfvars   # same aws_region and project_name as the cluster (and cluster_name, if you set it)
terraform init && terraform apply
cd ../..
```

Edit `terraform.tfvars` before the apply: set `aws_region`. `cluster_name` is optional: while it stays commented, both
stacks derive `<project_name>-<region code>`, for example `dynamo-sample-usw2`
(`terraform/bootstrap/main.tf`, `terraform/cluster/main.tf`). Set it to run a second stack in an account and Region
that already has one (the derived name would be the same as the first stack's; inferred, not tested), and give the same
value in step 2.

It creates the model and results buckets (encrypted, Block Public Access, TLS only; the results bucket is versioned).
The cluster stack does not read the buckets: it computes their names from these values (`terraform/cluster/main.tf`),
so a mismatch passes both applies and fails in step 4 (inferred, not tested). After step 2,
`terraform -chdir=terraform/cluster output -raw model_bucket_name` must print the `model_storage_bucket` of
`terraform -chdir=terraform/bootstrap output`. The state is local. To keep it in S3, copy `backend.tf.example` to
`backend.tf` first, in this folder and in `terraform/cluster`.

## 2. Cluster (`terraform/cluster`)

Copy `terraform/cluster/terraform.tfvars.example` to `terraform/cluster/terraform.tfvars` and edit these keys **in
place**: the example already has each of them, set to a placeholder or a default. Do not add a second line for a key
that is already there. If you set `cluster_name` in step 1, uncomment it in this file with the same value. The example also sets `runtime_image` to the image of
`versions.env`. A `terraform plan` on the unedited example fails on the placeholder CIDR and on the empty
`capacity_reservation_ids` (seen on 2026-11-06).

```hcl
aws_region               = "<region>"
eks_public_access_cidrs  = ["203.0.113.10/32"]   # your egress IPs; a /0 is refused
gpu_instance_types       = ["p6-b200.48xlarge"]
gpu_capacity_type        = "capacity-block"      # capacity-block | odcr | on-demand | spot
capacity_reservation_ids = ["cr-<17 hex>"]       # [] for on-demand and spot
model_id                 = "Qwen/Qwen3.8-27B-FP8"
model_revision           = "017b9c7af6b5689d5dd426a76e0bc077eb5ca20a"
```

`eks_public_access_cidrs` must hold the public IPv4 address of every machine that runs `terraform apply`, `kubectl` or
the scripts: the Kubernetes, Helm and kubectl providers (`terraform/cluster/providers.tf`) and `kubectl` call the public
endpoint of the cluster, which accepts only these CIDRs
([cluster endpoint access](https://docs.aws.amazon.com/eks/latest/userguide/cluster-endpoint.html)).
`echo "$(curl -s -4 https://checkip.amazonaws.com)/32"` prints the /32 CIDR of the machine that runs it. From another
address the apply creates the cluster, then fails on its first Kubernetes or Helm resource, and `kubectl` cannot reach
the cluster (inferred from `providers.tf`, not tested). To recover, correct the list and rerun `terraform apply` (not
tested).

```bash
terraform -chdir=terraform/cluster init
terraform -chdir=terraform/cluster apply
eval "$(terraform -chdir=terraform/cluster output -raw configure_kubectl)"  # kubectl context = cluster name
terraform -chdir=terraform/cluster output -json gpu_pools | jq -r 'keys[]'   # the GPU pool name
```

The stack exports the three outputs that `scripts/gen_env.py` requires: `platform_env`, `sample_env` and `gpu_pools`.
Every variable is described in [terraform/README.md](../terraform/README.md).

## 3. Generated env (`scripts/gen_env.py`)

```bash
python3 scripts/gen_env.py
export KUBE_CONTEXT="$(terraform -chdir=terraform/cluster output -raw cluster_name)"
```

`gen_env.py` reads `terraform output -json` and writes `manifests/config/platform.env`, gitignored and overwritten on
every run: keep nothing of your own in it. It refuses an empty `RUNTIME_IMAGE`, and any GPU shape or model other than
the tested one unless you pass `--allow-untested`. When it refuses, it writes nothing. Run it again after every
`terraform apply`. The scripts refuse to run without `KUBE_CONTEXT`, and it must name the cluster of `platform.env`.
Like `STAGE_IMAGE` and `HF_TOKEN` below, it lives in this shell only: export it again in a new terminal.

## 4. Stage the model (`scripts/10-stage-model.sh`)

The script runs the Job `node-ops/model-stage` on `cpu-demo`. It copies `MODEL_ID` at `MODEL_REVISION` from Hugging Face
to `MODEL_S3_URI` (weights, config, tokenizer and license files) and writes the `.done` marker last. Step 5 then copies
from that bucket to the disk of the GPU node. The Job runs in a Python container image, which `STAGE_IMAGE` names.

```bash
export STAGE_IMAGE="public.ecr.aws/docker/library/python:3.12-slim@$(crane digest public.ecr.aws/docker/library/python:3.12-slim)"
scripts/10-stage-model.sh --dry-run   # prints the ConfigMap and the Job, no cluster call
scripts/10-stage-model.sh             # applies the Job and waits for it
```

In our run on 2026-11-06 the Job took 5 min 31 s and wrote 80 objects to the bucket: 79 files, 30,889,982,778 bytes
(28.8 GiB), and the `.done` marker.

- `STAGE_IMAGE` must be pinned by digest, the hash that identifies one exact image, not by the tag that can move. The
  script refuses its placeholder default and checks only the format. `crane digest` prints the digest with its
  `sha256:` prefix (observed), so the command above joins it after the `@` as it is; we ran `crane digest` and used what
  it printed, and the one-line form only joins the two. `crane manifest "$STAGE_IMAGE" >/dev/null`
  fails if the image does not exist. With a missing image the pod stays in
  `ImagePullBackOff` until the Job deadline of 6 hours (`activeDeadlineSeconds` in `scripts/10-stage-model.sh`;
  inferred, not tested).
- Public models need no token: the Job never sends one to them.
- For a gated model, accept the license on the model page with the Hugging Face account that owns the token, then
  export the token in the shell that runs the script: `export HF_TOKEN=<read token>`. The script reads it from the
  process environment. Never write it to `platform.env`, which `gen_env.py` overwrites, nor to any tracked file.
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
aws ec2 describe-capacity-reservations --capacity-reservation-ids <cr-id> --region <region> \
  --query 'CapacityReservations[].State' --output text   # a Capacity Block must print active
scripts/11-prefetch.sh --wait
```

The Job `node-ops/model-prefetch` runs on the GPU node, copies the model from S3 to the local NVMe at
`/mnt/.ephemeral/models` and writes the marker that the workers' init container waits for. Submitting it starts the GPU
node, so a Capacity Block must be `active` by then ([Prerequisites](#prerequisites)). It reads `MODEL_ID` and
`MODEL_REVISION` from the generated env; `--force` copies again. `--wait` gives up after 90 minutes
(`PREFETCH_TIMEOUT_MIN`) with a non-zero exit. On success it prints the marker, whose `elapsed_s` is the copy time, then
`result=prefetched`.

The script also applies the pod `node-ops/ephemeral-shell`, which stays `Pending` until the GPU node exists, and prints
`Warning: Pod/ephemeral-shell does not have any containers matching "prefetch"`. Both appeared in our run on 2026-11-07,
which ended with `result=prefetched` 2 min 49 s after the submit (the GPU node start included), with `elapsed_s=8` for
79 files, 30,889,982,778 bytes.

## 6. Deploy one cell (`examples/`)

```bash
kubectl --context "$KUBE_CONTEXT" apply --server-side -f examples/00-baseline/manifest.yaml
kubectl --context "$KUBE_CONTEXT" -n s-agg get pods   # 1 Frontend and 8 workers, once the operator creates them
kubectl --context "$KUBE_CONTEXT" -n s-agg wait --for=condition=Ready pod --all --timeout=25m
```

The cell is ready when `kubectl wait` returns. Before the operator creates the pods it fails with "no matching resources
found". The time to Ready we measured is in [BENCHMARKING.md](BENCHMARKING.md#reset-between-rates).

On another pool ([Prerequisites](#prerequisites)), replace the pool name as you apply:

```bash
sed 's/gpu-b200-cb/<pool>/' examples/00-baseline/manifest.yaml | kubectl --context "$KUBE_CONTEXT" apply --server-side -f -
```

We applied S0 with this replacement on one `p5en.48xlarge` Capacity Block (2026-11-07), after step 3 with
`--allow-untested` and the `node-ops` replacement of the [Prerequisites](#prerequisites). The pool `gpu-h200-cb` was the
one this Terraform generated; S0 is the only cell applied on it. The other cells ran earlier on `p5en.48xlarge` in a
pool created by hand (see `docs/optimizations/`); other shapes are not tested.

| Folder | Cell | Namespace | What it adds |
|---|---|---|---|
| `examples/00-baseline` | S0 | `s-agg` | 8 aggregated TP1 workers, round-robin |
| `examples/01-kv-router` | S1 | `s-agg` | R: the KV router |
| `examples/02-hicache` | S2 | `s-agg-hic` | O: HiCache L2 + L3 |
| `examples/03-pd-disaggregation` | S3 | `s-dis` | D: 4 prefill + 4 decode workers |
| `examples/04-kv-router-hicache` | S4 | `s-agg-hic` | R + O |
| `examples/05-kv-router-pd` | S5 | `s-dis` | R + D |
| `examples/06-pd-hicache` | S6 | `s-dis-hic` | O + D |
| `examples/07-all-three` | S7 | `s-dis-hic` | R + O + D |

Only one cell fits on the GPU node: run `kubectl delete -f examples/<folder>/manifest.yaml` before you apply the next one.
R is a choice of folder: in `00-baseline` and `01-kv-router` (likewise `02`/`04`, `03`/`05`, `06`/`07`) the only
differing non-comment line is the Frontend's `DYN_ROUTER_MODE`; [examples/README.md](../examples/README.md) has the
command that shows it.
Each folder's README says what the technique changes.

To load the cell and measure it, edit these values in the `env` of `benchmarks/perf.yaml`, then run its Job in the
namespace of the cell (the full text is in [BENCHMARKING.md](BENCHMARKING.md#run-one-point)):

- `RATE`: req/s, one of the files in `benchmarks/traces/` (we ran S0 at `5`).
- `CELL`: the Cell column above, for example `S0`.
- `RESULTS_BUCKET` and `AWS_REGION`: the keys of the same name in `manifests/config/platform.env`.
- `TRACE_BASE_URL`: the default points to `benchmarks/traces/` on the `main` branch of
  `aws-samples/sample-optimizing-inference-on-eks-self-hosted-models`, which has that folder only once these files are
  merged there. Before that, on a fork or a copy, serve the folder yourself and set `TRACE_BASE_URL`, as the section
  "Run one point" of [BENCHMARKING.md](BENCHMARKING.md#run-one-point) shows.

```bash
kubectl --context "$KUBE_CONTEXT" -n s-agg apply -f benchmarks/perf.yaml
kubectl --context "$KUBE_CONTEXT" -n s-agg logs -f job/perf | grep -E 'Rate .* done|Uploaded|Upload failed'
aws s3 sync "s3://<results bucket>/perf/S0/" ./results --region <region>
kubectl --context "$KUBE_CONTEXT" -n s-agg delete job perf
```

The log is mostly `pip install` output; the `grep` keeps the line that ends the run and the upload result. For another
cell, use its namespace and its `CELL`. Our S0 run at 5 req/s uploaded 8 objects, 25,862,064 bytes; the time of a run is
in [BENCHMARKING.md](BENCHMARKING.md#run-one-point).

## 7. Observe (Grafana)

```bash
kubectl --context "$KUBE_CONTEXT" -n monitoring get secret prometheus-grafana -o jsonpath='{.data.admin-password}' | base64 -d
kubectl --context "$KUBE_CONTEXT" -n monitoring port-forward svc/prometheus-grafana 3000:80   # runs until Ctrl-C
```

Open `http://localhost:3000` and log in as `admin`. `kubectl port-forward` keeps Grafana private; the CloudFront + WAF
edge (`enable_grafana_edge`) is off by default. Its hop from CloudFront to the internal NLB is plain HTTP, so turning
it on also needs `grafana_edge_allow_plain_http_origin = true`: see
[terraform/README.md](../terraform/README.md#grafana). The Application Load Balancer in front of Grafana in the
architecture figure is a reference design that Terraform does not build: the edge it implements is still CloudFront + NLB.
Terraform loads the dashboards of `dashboards/`: the R / D / O matrix, Dynamo, SGLang and DCGM. [dashboards/README.md](../dashboards/README.md) describes each panel.

## Teardown

`scripts/90-teardown.sh` runs the order below. It is a dry run by default: it prints every command and runs nothing
until you pass `--yes`. It needs `KUBE_CONTEXT` set to the cluster of `manifests/config/platform.env`, and it takes the
GPU pools, the region and the bucket names from that generated file only. After a failed or partial apply that file
does not exist: see [After a failed or partial apply](#after-a-failed-or-partial-apply). In this section, steps 1 to 5
are the teardown steps, numbered as the script prints them (`1/5` to `5/5`), not the deploy steps.

```bash
scripts/90-teardown.sh                          # dry run of steps 1 to 3
scripts/90-teardown.sh --yes                    # steps 1 and 2, then prints the cluster destroy command
scripts/90-teardown.sh --destroy-cluster --yes  # steps 1 to 3; Terraform still asks before it destroys
scripts/90-teardown.sh --after-destroy --yes    # step 4 for the results bucket, then prints step 5
```

- Step 2 deletes the NodeClaims of every pool in `GPU_POOLS` (or of each `--pool gpu-<name>`) and each `cpu-demo`
  NodeClaim whose node runs only DaemonSet pods. It then waits up to `--timeout` seconds (default 1800) until no GPU
  node is left, and stops before the destroy if one stays.
- `--after-destroy` empties the results bucket, every object version and delete marker included, in batches of at
  most 1000. **It deletes the benchmark results: run the `aws s3 sync` of
  [BENCHMARKING.md](BENCHMARKING.md#run-one-point) first.** Add `--empty-models` to empty the model bucket too:
  restaging the weights takes time. The script never runs the bootstrap destroy, and at the end it prints the checks of
  the last paragraph.

To do it by hand, keep this order, or the destroy stalls on resources that Terraform does not own or cannot delete:

1. Delete the cells: `kubectl delete -f examples/<folder>/manifest.yaml` for each one you applied, and any other DGD in
   `s-agg`, `s-agg-hic`, `s-dis` and `s-dis-hic`.
2. Delete the GPU NodeClaims, `kubectl delete nodeclaims -l karpenter.sh/nodepool=<pool>`, so no GPU node and no EFA
   ENI is left. Delete any extra `cpu-demo` NodeClaim too: that pool never consolidates.
3. Destroy the cluster: `(cd terraform/cluster && terraform destroy)`.
4. Copy the benchmark results you keep first (`aws s3 sync "s3://<results bucket>/perf/" ./results --region <region>`,
   [BENCHMARKING.md](BENCHMARKING.md#run-one-point)). Then empty both buckets: `aws s3 rm s3://<bucket> --recursive`
   deletes the current objects. The results bucket is versioned, so also delete its object versions and delete markers
   (the S3 console "Empty" action does both). The buckets have `force_destroy = false`.
5. Destroy the buckets: `(cd terraform/bootstrap && terraform destroy)`, only when you no longer need the weights and the
   results.

Until step 3 ends, the idle cluster bills: the EKS control plane, the NAT gateway (per hour), its public IPv4 address,
the `cpu-demo` and system nodes and their EBS volumes. Without a GPU node that is about US$ 0.43 per hour in us-east-2,
where the stack was tested, at On-Demand list prices as of 2026-10-27 from the AWS Price List API (pricing pages:
[EKS](https://aws.amazon.com/eks/pricing/), [VPC](https://aws.amazon.com/vpc/pricing/),
[EC2 On-Demand](https://aws.amazon.com/ec2/pricing/on-demand/), [EBS](https://aws.amazon.com/ebs/pricing/)). The
figure excludes data transfer (the NAT gateway also bills per GB processed), GPU nodes, S3, CloudWatch Logs and the
optional Grafana edge:

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
pools and the bucket names from `terraform output -json` of `terraform/cluster` and, with `--after-destroy`, of
`terraform/bootstrap`. These reads are the only commands its dry run runs. They need `terraform init` of the stack in
this checkout; without it Terraform prints "Required plugins are not installed" and the script stops (observed for
`terraform/cluster` in a new checkout). The cluster destroy removes the cluster outputs, so the `--after-destroy` run
takes the cluster name and the region from `TEARDOWN_CLUSTER_NAME` and `TEARDOWN_AWS_REGION`; the first run prints
both. `TEARDOWN_RESULTS_BUCKET` and `TEARDOWN_MODEL_BUCKET` work the same way if no bootstrap state is in your
checkout. The script reads these variables only with `--from-terraform`, stops when two sources name different values,
and refuses to run when nothing names the cluster.

```bash
scripts/90-teardown.sh --from-terraform                          # dry run of steps 1 to 3
scripts/90-teardown.sh --from-terraform --destroy-cluster --yes  # steps 1 to 3; Terraform still asks before it destroys
export TEARDOWN_CLUSTER_NAME=<cluster name> TEARDOWN_AWS_REGION=<region>   # both printed by the previous run
scripts/90-teardown.sh --after-destroy --from-terraform --yes    # step 4, bucket names from terraform/bootstrap
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
