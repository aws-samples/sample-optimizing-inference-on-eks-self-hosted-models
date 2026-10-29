# Terraform: EKS Auto Mode cluster for the Dynamo sample

One parametrized root for any region and any P or G GPU instance type, plus a bootstrap stack for the S3 buckets.

| Folder | What it is |
|---|---|
| `bootstrap/` | The model and results S3 buckets. Apply it first and destroy it last. |
| `cluster/` | VPC, EKS Auto Mode, the `cpu-demo` pool, one GPU NodeClass/NodePool per group of GPU types, namespaces, Pod Identity roles, the bench ECR repository and an optional runtime-image mirror repository (encrypted with a KMS key of the stack), observability, `dynamo-platform` and an optional Grafana edge. |
| `cluster/modules/gpu-pools/` | Pure module (no providers or resources): it groups the GPU types and renders the NodeClass/NodePool manifests. `cluster/tests/` tests it with `terraform test`. |

Both ECR repositories set `scan_on_push = true`, the repository setting of basic scanning: in a registry set to enhanced scanning (Amazon Inspector), as in the account of the tests, the registry filters decide what is scanned and the repository setting has no effect, and `aws ecr describe-images` shows no `imageScanStatus`; read the findings with `aws ecr describe-image-scan-findings` or in Amazon Inspector.

## Before you start

- **Terraform >= 1.12** (tested with 1.12.2 and 1.17.0), the AWS CLI, `kubectl` and `python3`. Older versions do not short-circuit `||`, and the plan fails on GPU types without EFA.
- **You buy the GPU capacity.** Terraform never purchases a Capacity Block or an ODCR. With `capacity-block` or `odcr` you reserve it first and pass its ID. With `on-demand` or `spot` the instance is launched when a pod needs it, and P/G capacity is not guaranteed.
- The identity that runs `plan` needs `ec2:DescribeInstanceTypes`, `ec2:DescribeInstanceTypeOfferings` and `ec2:DescribeCapacityReservations`, on top of what the resources need.
- Local state by default. To keep it in S3, copy `backend.tf.example` to `backend.tf` in each folder. It uses an S3 lock file (`use_lockfile`), with no DynamoDB table.

## The minimum tfvars

Copy `cluster/terraform.tfvars.example` to `cluster/terraform.tfvars`. Every user-facing choice is in that one file. Only these are required. Everything else is derived or has a default.

```hcl
aws_region               = "us-east-2"
eks_public_access_cidrs  = ["203.0.113.10/32"]      # your egress IPs
gpu_instance_types       = ["p6-b200.48xlarge"]
gpu_capacity_type        = "capacity-block"         # capacity-block | odcr | on-demand | spot
capacity_reservation_ids = ["cr-<17 hex>"]          # [] for on-demand and spot
model_id                 = "Qwen/Qwen3.8-27B-FP8"
model_revision           = "017b9c7af6b5689d5dd426a76e0bc077eb5ca20a"
runtime_image            = "nvcr.io/nvidia/ai-dynamo/sglang-runtime:1.6.0-efa@sha256:d574951b0dbd037bdcaabe121431a83cd047a5c106cb243fe6f2f97f2e456cd2"
```

`runtime_image` is already set in the example: the public NGC image of `versions.env`, pinned by digest.

What Terraform derives:

| Value | Derived from |
|---|---|
| Account ID, ECR registry | `data.aws_caller_identity`; no account ID is in any file. |
| `cluster_name` | `<project_name>-<region code>`, e.g. `dynamo-sample-use2`. The region code is the prefix of the AZ IDs. |
| Bucket names | `<prefix>-models-<account>-<suffix>` and `<prefix>-results-<account>-<suffix>` for the cluster `<prefix>-<suffix>`. `bootstrap/` uses the same rule, so keep `project_name` and `cluster_name` identical in both. `scripts/lib/common.sh` derives the Track Q cluster from `MODEL_BUCKET` the same way. |
| GPU facts | `data.aws_ec2_instance_type` for each type: network cards, EFA interfaces, GPU name, count and memory, vCPU, memory, instance store. |
| GPU AZ | `capacity-block`: the AZ of each block. `on-demand` and `spot`: the first VPC AZ that offers every type. `odcr`: required in `gpu_az_ids`. |
| Pool size (`limits`) | `capacity-block`: the instance count of the live blocks. `on-demand` and `spot`: 1 node per pool. `odcr`: required in `gpu_max_nodes`. Output `gpu_pools[*].max_nodes` shows the value. |
| `cpu-demo` AZ | The first GPU AZ, so frontends, bench and GPU workers share an AZ. |
| Labels | `nvidia.com/gpu.present=true` and `nvidia.com/gpu.product=NVIDIA-<GPU NAME>`. |

## Variables

| Variable | Default | What it does |
|---|---|---|
| `aws_region` | required | Region of the stack. |
| `project_name` | `dynamo-sample` | ECR repository prefix (`<project_name>/bench` and the optional `<project_name>/sglang-runtime`), `Project` tag and default name prefix. |
| `cluster_name` | derived | EKS cluster name, `<prefix>-<suffix>`. Also the prefix of the IAM roles. |
| `eks_public_access_cidrs` | required | CIDRs allowed on the public EKS API endpoint. A zero-length prefix (any `/0`, including `::/0`) is rejected. |
| `allow_open_eks_api` | `false` | Accepts a `/0` in `eks_public_access_cidrs`. Authentication still needs IAM plus an EKS access entry. Demo-only escape hatch. |
| `gpu_instance_types` | required | P or G types the GPU pools may launch. Types with the same GPU name, GPU memory, GPU count, network cards and EFA interfaces share one pool. |
| `gpu_capacity_type` | `on-demand` | `capacity-block`, `odcr`, `on-demand` or `spot`. One purchase model per stack. |
| `capacity_reservation_ids` | `[]` | IDs of the Capacity Blocks or targeted ODCRs you bought. Required for `capacity-block` and `odcr`, rejected for `on-demand` and `spot`. |
| `gpu_az_ids` | `[]` | AZ IDs of the GPU nodes. Empty means derived. Several AZs make the pools multi-AZ. Required for `odcr`. |
| `gpu_max_nodes` | derived | Maximum nodes per pool (the NodePool `limits`). Required for `odcr`. |
| `gpu_efa` | `auto` | `auto` renders the static EFA layout on every type with EFA. `off` renders no `advancedNetworking`, and Auto Mode manages the ENIs. |
| `gpu_pod_ipv4_prefix_count` | `4` | `/28` prefixes on the primary ENI of an EFA node: 16 pod IPs each. With a static layout Auto Mode adds no IPs after launch. |
| `gpu_ephemeral_storage_size` | `256Gi` | `ephemeralStorage.size`. Below the instance store, Auto Mode builds an NVMe RAID0 at `/mnt/.ephemeral`. Above it, the node uses EBS: a `check` warns, and `gpu_pools[*].ephemeral_backing` says `ebs` (e.g. `g6e.xlarge`, 250 GB). |
| `gpu_expire_after` | derived | `expireAfter`: `480h` for reserved pools, `168h` for on-demand and spot. |
| `gpu_consolidate_after` | `15m` | `consolidateAfter` of on-demand and spot pools. Reserved pools always use `Never` with a budget of `0`. |
| `gpu_product_overrides` | `{}` | `nvidia.com/gpu.product` per EC2 GPU name, where the derived `NVIDIA-<NAME>` differs from GPU feature discovery (e.g. `{ H100 = "NVIDIA-H100-80GB-HBM3" }`). |
| `primary_az_id` | derived | AZ ID of `cpu-demo`. Pin it to keep `cpu-demo` in place when the GPU AZ changes: otherwise its nodes and AZ-bound volumes move. |
| `vpc_cidr`, `vpc_az_ids`, `eks_control_plane_excluded_az_ids` | `10.70.0.0/16`, every AZ, `[]` | VPC. It spans every AZ, so a reservation in another AZ needs no VPC change. |
| `model_id`, `model_revision` | required | Hugging Face repo ID and the 40-character commit SHA, never a branch. |
| `hf_token_secret_arn` | `null` | Optional Secrets Manager ARN of a Hugging Face token. Terraform grants `secretsmanager:GetSecretValue` on that ARN only, to the model-prefetch role. It never creates or reads the secret. |
| `runtime_image` | `""` | Runtime image of the Frontend and the workers, pinned by digest: the public NGC image of `versions.env` (set in the example), or its copy in `<project_name>/sglang-runtime` (`create_runtime_mirror_repository = true`). `scripts/gen_env.py` refuses an empty value. |
| `create_runtime_mirror_repository` | `false` | Creates `<project_name>/sglang-runtime`, the ECR repository of the optional copy of the runtime image (`docs/DEPLOY.md`, "Mirror the image to your ECR"). Off, the workers pull the public NGC image, the output `sglang_runtime_ecr_repository_url` is null and `SGLANG_RUNTIME_REPO` is empty. A stack applied before this variable existed plans to destroy the repository unless you set it to `true`. |
| `workload_rate`, `workload_context_tokens`, `workload_sessions_per_rate` | `10`, `16384`, `75` | `RATE`, `MAX_ISL` and `SESSIONS_PER_RATE` of `config/sample.env` (the W3-16K workload shape). |
| `model_bucket_name`, `results_bucket_name` | derived | Override the bucket names. |
| `enable_grafana_edge` | `false` | Publishes Grafana through CloudFront (VPC origin) and AWS WAF. Off: use `kubectl port-forward`. On, it also needs `grafana_edge_allow_plain_http_origin = true` (see [Grafana](#grafana)). |
| `grafana_edge_allow_plain_http_origin` | `false` | Accepts that the hop from CloudFront to the internal NLB of the edge is plain HTTP. Required with the edge on: the plan refuses `enable_grafana_edge = true` without it. |
| `grafana_edge_domain` | `""` | Optional domain name you own for the edge, set together with `grafana_edge_viewer_certificate_arn`: viewers then get the `TLSv1.2_2021` security policy over SNI. Empty: the default `*.cloudfront.net` certificate (see [Grafana](#grafana)). |
| `grafana_edge_viewer_certificate_arn` | `""` | ARN of an issued ACM certificate in `us-east-1` that covers `grafana_edge_domain`. The plan refuses one of the two without the other, and a certificate in another Region. |
| `vpc_cni_addon_version`, `kube_proxy_addon_version`, `node_monitoring_agent_addon_version` | pinned | Add-on builds for Kubernetes 1.35. Review them when you change `kubernetes_version`. |
| `image_mirror_source_region` | `null` | Optional region of same-account source ECR repositories the `node-ops/image-mirror` role may read: `bench`, and `sglang-runtime` with `create_runtime_mirror_repository = true`. The repository ships no Job for that role: it is for an in-cluster copy Job you add yourself (`cluster/iam-image-mirror.tf`). |
| `kubernetes_version`, `dynamo_version`, chart versions, `prometheus_*`, `ecr_untagged_expire_days`, `grafana_*`, `tags` | see `variables.tf` | Pins and tuning. |

## GPU types

The pool name is `gpu-<gpu>-<cb|odcr|od|spot>`. When two groups share a GPU name, both get a `-<gpus>x<GiB>g[-efa<n>][-nc<n>]` suffix: `-efa<n>` when the type has EFA interfaces, `-nc<n>` when its number of network cards differs from its EFA interfaces (or from 1 without EFA). Adding or removing a type can therefore rename, and so recreate, a pool.

| Types | GPU | Pool shape | Status |
|---|---|---|---|
| `p6-b200.48xlarge` | B200, 8 | 8 network cards, 8 EFA | **Validated**: EFA, Capacity Block. The rendered NodeClass/NodePool equal the live objects (`gpu_pools_golden.tftest.hcl`). |
| `g7e.4xlarge` | RTX PRO Server 6000, 1 | no EFA | **Validated**: no EFA, ODCR (test pool). |
| `p5.48xlarge`, `p5en.48xlarge`, `p6-b300.48xlarge`, `p4d.24xlarge`, `p4de.24xlarge` | H100, H200, B300, A100 | 4 to 32 EFA | Supported by Terraform, manifests untested. The `p6-b300` (17 cards, 16 EFA) and `p5` (32 EFA) layouts follow the same rule but never ran. |
| `g6e.*`, `g7e.48xlarge`, `g6.48xlarge`, `g5.*` | L40S, RTX PRO Server 6000, L4, A10G | no EFA, or 1 to 4 EFA | Supported by Terraform, manifests untested. |

Rejected at plan: types that are not P or G, without NVIDIA GPUs (e.g. `g4ad`), arm64-only (e.g. `g5g`), or with more network cards than EFA interfaces + 1.

The Track Q manifests select `karpenter.sh/nodepool: gpu-b200-cb` and 8 GPUs per node. Any other shape is **supported by Terraform, manifests untested**: the pods of the manifests stay Pending until you adapt their node selectors and GPU counts.

## Capacity types

| `gpu_capacity_type` | NodeClass | NodePool | Notes |
|---|---|---|---|
| `capacity-block` | selects the blocks | `capacity-type: reserved`, never consolidates, `480h` | AZ and size come from the blocks. An expired block does not break the plan: it stops counting, and the pool falls back to 1 node or `gpu_max_nodes`. Once `DescribeCapacityReservations` no longer returns it, remove its ID. |
| `odcr` | selects the ODCRs | `reserved`, never consolidates, `480h` | Set `gpu_az_ids` and `gpu_max_nodes`: no data source exposes ODCR facts. A `gpu_max_nodes` below the reservation leaves part of it unused, and it still bills. |
| `on-demand` | no reservation terms | `on-demand`, consolidates when empty after `15m`, `168h` | Default 1 node per pool. |
| `spot` | no reservation terms | `spot`, as on-demand | P/G spot is scarce, and the workers do not handle interruptions. |

A NodeClass that selects a reservation turns off the automatic use of open ODCRs for the whole cluster. That is why one stack has one purchase model.

## Run order

1. **Bootstrap.** `cd bootstrap && cp terraform.tfvars.example terraform.tfvars`, set the same region, `project_name` and `cluster_name` as the cluster, then `terraform init && terraform apply`.
2. **Cluster.** `cd ../cluster && cp terraform.tfvars.example terraform.tfvars` and fill it in (`runtime_image` is already the public image of `versions.env`), then `terraform init && terraform apply`. Run the `configure_kubectl` output.
3. **Optional: copy the image to ECR.** To keep the image pull off the NAT gateway, set `create_runtime_mirror_repository = true` and apply, which creates `<project_name>/sglang-runtime`. Copy the image into it with its digest (for example with `crane copy`, see `docs/DEPLOY.md`), set `runtime_image` to the copy and apply.
4. **Generate the env.** `python3 scripts/gen_env.py` from the repo root. It reads `terraform output -json` and writes `manifests/config/platform.env` (read by kustomize and `scripts/lib/common.sh`) and `config/sample.env`. Both are gitignored. See below.
5. **Stage and prefetch the weights.** `scripts/10-stage-model.sh` (Hugging Face to S3), then `scripts/11-prefetch.sh` (S3 to the GPU node); see `docs/DEPLOY.md`.
6. **Deploy a cell.** `kubectl --context <cluster> apply --server-side -k examples/00-baseline`, then the other folders of `examples/`.

## Generated env (`scripts/gen_env.py`)

Terraform exports `platform_env`, `sample_env` and `gpu_pools`. Nothing secret is in them. `gen_env.py` validates them and writes plain `KEY=VALUE` files under a `# GENERATED` header. It writes nothing when it refuses, and it never prints a value.

- `platform.env`: `AWS_ACCOUNT_ID`, `AWS_REGION`, `CLUSTER_NAME`, `MODEL_BUCKET`, `RESULTS_BUCKET`, `ECR_REGISTRY`, `SGLANG_RUNTIME_REPO` (empty unless `create_runtime_mirror_repository = true`), `BENCH_REPO`, `GPU_CAPACITY_TYPE`, `GPU_POOLS` (`<pool>:<gpus>:<efa>,...`), `PRIMARY_AZ`, `GRAFANA_EDGE_ENABLED`. With exactly one pool it adds `GPU_NODEPOOL`, `GPU_INSTANCE_TYPES`, `GPU_PRODUCT`, `GPUS_PER_NODE`, `EFA_INTERFACES_PER_NODE`, `GPU_MEMORY_GIB_PER_GPU`, `HOST_MEMORY_GIB`, `EPHEMERAL_MIN_GIB` and `GPU_AZ`. It also adds `MODEL_ID`, `MODEL_REVISION` and `RUNTIME_IMAGE` of `sample_env`. `manifests/config/platform.env.example` shows the result.
- `config/sample.env`: `MODEL_ID`, `MODEL_REVISION`, `RUNTIME_IMAGE`, `RATE`, `MAX_ISL`, `SESSIONS_PER_RATE` and, when set, `HF_TOKEN_SECRET_ARN`. Source it in your shell. The scripts read `RATE`.
- **Track Q gate.** It refuses any shape other than one pool `gpu-b200-cb` with 8 GPUs per node, and any model other than `Qwen/Qwen3.8-27B-FP8`, unless you pass `--allow-untested`. With the flag it warns `manifests untested` and, for another shape, writes `GPU_SHAPE_SUPPORTED=terraform-only`. An empty `RUNTIME_IMAGE` is always refused.

## Grafana

Default: `kubectl -n monitoring port-forward svc/prometheus-grafana 3000:80`. The admin password is in the Secret `monitoring/prometheus-grafana`, key `admin-password`. Terraform does not manage it.

With `enable_grafana_edge = true`, Grafana is published through CloudFront, a VPC origin, an internal NLB and AWS WAF. The NLB is created by the load balancing capability of EKS Auto Mode (the stack does not install the AWS Load Balancer Controller). One `terraform apply` creates the whole edge, with no `-target` phases: Terraform creates the LoadBalancer Service after the `monitoring` namespace, waits for the NLB that Auto Mode gives it, then creates the VPC origin on that NLB and the distribution. Expect the apply that turns the edge on to take about 12 minutes more, most of it on the VPC origin (about 9.5 minutes; the distribution takes under 3). The header of `cluster/grafana-edge.tf` lists the order.

**The hop from CloudFront to the NLB is plain HTTP.** Viewers reach CloudFront over HTTPS (HTTP is redirected), but CloudFront opens plain HTTP to the NLB listener (`grafana_edge_listener_port`, default `80`), and the NLB forwards plain TCP to the Grafana pod. What crosses that hop unencrypted: the Grafana login with its user name and password, the session cookie of every request, and every API call with its response, the dashboards and the query results (the metrics of the cluster) included. The hop stays on a private path, never on the public internet: CloudFront reaches the internal NLB through its VPC origin, network interfaces that CloudFront manages in the private subnets of the VPC. The security group of the NLB admits only `CloudFront-VPCOrigins-Service-SG`, the security group of those interfaces, on the listener port.

A certificate on the NLB does not change this: a CloudFront VPC origin does not accept a Network Load Balancer with a TLS listener, so the `aws-load-balancer-ssl-cert` annotation cannot encrypt the hop. TLS on that hop needs another origin: an internal Application Load Balancer with an HTTPS listener and a public ACM certificate, in the cluster Region, for a domain name you own. CloudFront checks that certificate against the origin domain of the distribution, or against the `Host` header when it forwards the viewer's, so that origin also needs the managed origin request policy that does not forward the viewer `Host` header (`Managed-AllViewerExceptHostHeader`) instead of `Managed-AllViewer`. The name needs no DNS record that points at the load balancer: a VPC origin routes by the ARN of the load balancer. This sample does not ship that variant.

For this optional demo edge the plain-HTTP hop is an accepted trade-off: it stays on the private VPC origin path, the WAF and the Grafana login still guard the edge, and the default path, `kubectl port-forward`, carries Grafana inside the TLS connection to the EKS API endpoint. So the plan refuses `enable_grafana_edge = true` until you also set `grafana_edge_allow_plain_http_origin = true`.

**Viewer TLS.** On the default `*.cloudfront.net` certificate CloudFront sets the `TLSv1` security policy itself, and that minimum cannot be raised while the distribution uses that certificate. With `grafana_edge_domain` and `grafana_edge_viewer_certificate_arn` set, the distribution serves that domain as its alternate domain name over SNI with the `TLSv1.2_2021` security policy. The certificate must be an issued ACM certificate in `us-east-1` that covers the domain, because CloudFront only uses ACM certificates from that Region. For production use, set a domain you own and its certificate; the default certificate is meant for a quick demo. Then create a DNS record (a CNAME, or a Route 53 alias record) from the domain to the `grafana_cloudfront_domain_name` output; the `grafana_url` output and the Grafana `root_url` use the domain.

**Root URL.** The default root object of the distribution is the Grafana page `dashboards`, not `login`, because Grafana redirects a signed-in viewer from `login` back to `/`, which CloudFront would turn into `login` again, a redirect loop. When a viewer opens `/`, CloudFront asks Grafana for `/dashboards`: a signed-in viewer gets the same single-page app as for `/` (the browser stays on `/`, so the home dashboard opens), and a signed-out viewer goes to the login page and, after signing in, to the list of dashboards.

## Costs

You pay for what runs, including idle time:

- GPU instances, or the reservation: a Capacity Block is paid up front, and an ODCR bills whether or not a node runs.
- The EKS control plane, the Auto Mode management fee on each node, the `cpu-demo` nodes and the single NAT gateway (hourly and per GB).
- EBS: the node volumes and the 50 GiB Prometheus volume.
- S3 and ECR storage, and the EKS audit logs in CloudWatch Logs.
- The KMS key of the ECR repositories: US$ 1 a month, and each of its first two yearly rotations adds US$ 1 a month (us-east-2 list prices as of 2026-10-27, [AWS KMS pricing](https://aws.amazon.com/kms/pricing/)), plus requests beyond the free tier. No charge while it is pending deletion after `terraform destroy`.
- With the Grafana edge: CloudFront requests and transfer, and the WAF web ACL with its rules and requests.

The S3 gateway endpoint keeps model downloads off the NAT gateway. Get prices from the AWS Pricing Calculator for your region.

## Tags

The AWS provider puts the stack tags on every resource Terraform creates: `Project`, `Environment`, `Region`, `Stack` and `ManagedBy`, merged with your `tags`. The keys and values of `tags` may use only letters, digits, spaces and `_ . : / = + - @`, the characters KMS and IAM accept: the plan of either stack refuses any other one, which would fail the apply with `TagException: Invalid tag`. Three kinds of resource are created by EKS Auto Mode, not by Terraform, and miss them (a known limitation of the sample):

- **The instances**, CPU and GPU, do not carry the stack `tags`, only the tags of their NodeClass, `Project` and `nodeclass` (`cluster/nodepool.tf`, `cluster/modules/gpu-pools/main.tf`), plus the tags Auto Mode adds, such as `eks:eks-cluster-name`. The nodes of the built-in `system` pool use the Auto Mode `default` NodeClass and carry no `Project` either.
- **The Prometheus volume** comes from the `auto-ebs-sc` StorageClass, which sets no tags: it carries only the tags Auto Mode adds.
- **The internal NLB of the Grafana edge**, with `enable_grafana_edge = true`, is created by Auto Mode for the Service `monitoring/grafana-edge`: it carries only `eks:eks-cluster-name`, `service.eks.amazonaws.com/stack` and `service.eks.amazonaws.com/resource`.

So a cost-allocation report by tag misses them (by `Project` it still misses the `system` nodes, the Prometheus volume and the edge NLB), and `aws ec2 describe-instances` filtered by `Stack`, `Environment` or one of your `tags` does not list them. In the tests, `aws ec2 describe-instances --filters` did not list the running Auto Mode instances at all, not even filtered by the tag `eks:eks-cluster-name`, by `instance-id` or by `vpc-id`. Find an instance by its ID, the last part of `kubectl get node <node> -o jsonpath='{.spec.providerID}'`, with `aws ec2 describe-instances --instance-ids <id>`. To list them all, ask the Resource Groups Tagging API for the resources that carry the tag `eks:eks-cluster-name` with the cluster name (the edge NLB carries it too):

```bash
aws resourcegroupstaggingapi get-resources --region <region> --tag-filters Key=eks:eks-cluster-name,Values=<cluster name> \
  --resource-type-filters ec2:instance elasticloadbalancing:loadbalancer --query 'ResourceTagMappingList[].ResourceARN'
```

That listing also holds instances that ended a short time ago: check the state of each one with `aws ec2 describe-instances --instance-ids <id>`. The volume ID of the Prometheus volume is in `kubectl get pv -o custom-columns=CLAIM:.spec.claimRef.name,VOLUME:.spec.csi.volumeHandle`.

Terraform does not merge the stack tags into the NodeClass: a change of the NodeClass `tags` marks every node of that NodeClass as drifted, and Auto Mode replaces them: the GPU workers restart on a new node, whose NVMe needs the weights prefetched again (`scripts/11-prefetch.sh`). On `cpu-demo` and the reserved GPU pools, whose disruption budget is `0`, the drifted nodes stay until they expire or you delete their NodeClaims.

## Teardown order

1. Delete the workloads: `kubectl delete -k examples/<folder>`, plus anything else you applied.
2. Delete the GPU NodeClaims (`kubectl delete nodeclaims -l karpenter.sh/nodepool=<pool>`) so no GPU node and no ENI is left.
3. Empty the ECR repositories `<project_name>/bench` and, with `create_runtime_mirror_repository = true`, `<project_name>/sglang-runtime`: `aws ecr list-images`, then `aws ecr batch-delete-image` by digest, at most 100 per call, until the listing is empty (step 3 of `scripts/90-teardown.sh`, or the commands in [docs/DEPLOY.md](../docs/DEPLOY.md#teardown)). Both keep `force_delete = false` on purpose, so that Terraform never deletes an image on its own: the destroy, or a replacement after a change, stops with `RepositoryNotEmptyException` while a repository holds one.
4. `terraform destroy` in `cluster/`.
5. `terraform destroy` in `bootstrap/`, only if you no longer need the weights and results. The buckets have `force_destroy = false`, so empty them first. The results bucket is versioned, so delete its versions too.

After a failed or partial apply of `cluster/`, fix the error and re-run the apply if you can. To tear the partial stack down instead, keep the same order: `scripts/90-teardown.sh` finds no generated env file then, so pass `--from-terraform` and it reads the cluster name, the region, the GPU pools, the bucket names and the ECR repositories from `terraform output -json`. See [After a failed or partial apply](../docs/DEPLOY.md#after-a-failed-or-partial-apply) in docs/DEPLOY.md.
