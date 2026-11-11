# Troubleshooting

Known issues from the end-to-end tests of this sample, with the fix for each. The procedure itself is in
[DEPLOY.md](DEPLOY.md).

## Terraform

### `argument must not be null` when you plan a GPU type without EFA

Terraform older than 1.12 does not short-circuit `||`. For a type without EFA (for example `g5.xlarge`) the
`aws_ec2_instance_type` data source returns `efa_maximum_interfaces = null`, and the plan fails. Use Terraform 1.12 or
newer (tested with 1.12.2 and 1.17.0). The stacks declare `required_version = ">= 1.12"`.

### GPU node never starts: `InsufficientInstanceCapacity`

The NodeClaims fail with `InsufficientInstanceCapacity` (or `NoCompatibleInstanceTypes`) and no instance runs. With
`on-demand` and `spot`, the GPU AZ is the first VPC AZ that offers every requested type: offered means the type exists
in that AZ, not that capacity is free there. Set `gpu_az_ids` to several AZ IDs (the pools become multi-AZ), try
another type, or buy reserved capacity (a Capacity Block or an ODCR). Nothing is billed while no instance starts.

### Capacity Block or ODCR not picked up as expected

What each purchase model needs from you is in [DEPLOY.md](DEPLOY.md#prerequisites). What Terraform renders for it (the
`capacityReservationSelectorTerms` of the NodeClass, the pool size once a block expires) is in
[Capacity types](../terraform/README.md#capacity-types).

### A GPU pool is renamed or replaced after you change `gpu_instance_types`

When two groups of types share a GPU name, both pool names get a suffix, so adding or removing a type can recreate a
pool and drain its nodes. The naming rule is in [terraform/README.md](../terraform/README.md#gpu-types). Check
`terraform plan` before you apply.

### Auto Mode instances, the Prometheus volume and the Grafana edge NLB do not carry your `tags`

The instances that EKS Auto Mode launches (CPU and GPU), the Prometheus volume and, with `enable_grafana_edge = true`,
the internal NLB of the Grafana edge do not carry the stack `tags` of the tfvars. The instances carry only the tags of
their NodeClass, `Project` and `nodeclass`, plus the `eks:*` tags of Auto Mode; the volume, created by the
`auto-ebs-sc` StorageClass, carries only the Auto Mode tags. The NLB, which Auto Mode creates for the Service
`monitoring/grafana-edge`, carries only `eks:eks-cluster-name`, `service.eks.amazonaws.com/stack` and
`service.eks.amazonaws.com/resource`. So a cost-allocation report by tag misses them, and `aws ec2 describe-instances`
filtered by one of the stack tags does not list them. In the tests, `aws ec2 describe-instances --filters` did not list
the running Auto Mode instances at all, not even filtered by the tag `eks:eks-cluster-name`, by `instance-id` or by
`vpc-id`. Find an instance by its ID, the last part of `kubectl get node <node> -o jsonpath='{.spec.providerID}'`, with
`aws ec2 describe-instances --instance-ids <id>`. To list them all, ask the Resource Groups Tagging API for the
resources that carry the tag `eks:eks-cluster-name` with the cluster name (the edge NLB carries it too):

```bash
aws resourcegroupstaggingapi get-resources --region <region> --tag-filters Key=eks:eks-cluster-name,Values=<cluster name> \
  --resource-type-filters ec2:instance elasticloadbalancing:loadbalancer --query 'ResourceTagMappingList[].ResourceARN'
```

That listing also holds instances that ended a short time ago: check the state of each one with
`aws ec2 describe-instances --instance-ids <id>`. Merging the stack tags into the NodeClass `tags` would replace every
node of that NodeClass (drift), so the sample keeps this as a known limitation: see
[terraform/README.md](../terraform/README.md#tags).

### `terraform plan` refuses `enable_grafana_edge = true`

The plan fails on `var.enable_grafana_edge` with a message that starts with `enable_grafana_edge = true sends Grafana
traffic from CloudFront to the internal NLB over plain HTTP`. The optional edge carries the Grafana login, the session
cookie and the API calls in plain HTTP between CloudFront and the internal NLB, on the private VPC origin path, and a
CloudFront VPC origin does not accept an NLB with a TLS listener. If you accept that hop for your demo, set
`grafana_edge_allow_plain_http_origin = true` next to `enable_grafana_edge = true` and plan again; otherwise keep the
edge off and use `kubectl port-forward`. What crosses the hop, and what TLS on it would need, is in
[terraform/README.md](../terraform/README.md#grafana).

### `terraform plan` refuses `grafana_edge_viewer_certificate_arn`

The plan fails on `var.grafana_edge_viewer_certificate_arn` when only one of `grafana_edge_domain` and
`grafana_edge_viewer_certificate_arn` is set, or when the certificate ARN is not in `us-east-1`. Set both together:
the domain you own and the ARN of an issued ACM certificate in `us-east-1` that covers it (CloudFront only uses ACM
certificates from that Region). Or leave both empty to keep the default `*.cloudfront.net` certificate, on which
CloudFront forces the `TLSv1` security policy. See [terraform/README.md](../terraform/README.md#grafana).

### `terraform destroy` of the buckets fails: `BucketNotEmpty`

The buckets have `force_destroy = false`. Empty both first, including every object version and delete marker of the
versioned results bucket (see [DEPLOY.md](DEPLOY.md#teardown)), then destroy again.

### The VPC is not deleted on `terraform destroy`

An ENI left in the VPC blocks the delete: usually a GPU node that still runs (its EFA ENIs). Delete the GPU NodeClaims
before the destroy. If your account runs Amazon GuardDuty runtime monitoring, it can add a VPC endpoint tagged
`GuardDutyManaged=true` that Terraform does not own: look for it with
`aws ec2 describe-vpc-endpoints --filters Name=vpc-id,Values=<vpc-id> Name=tag:GuardDutyManaged,Values=true`.

## Cluster and pods

### `manifests/config/platform.env` is missing

The scripts read the generated file `manifests/config/platform.env`. A fresh clone has only `platform.env.example`. Run
`python3 scripts/gen_env.py` after `terraform apply` in `terraform/cluster` (step 3 of [DEPLOY.md](DEPLOY.md)), and
again after every later apply.

If `gen_env.py` refuses with `terraform output platform_env ... missing` (the message lists every missing output), the
last terraform apply of `terraform/cluster` failed or stopped before it created those outputs: the cluster can be up, but
no env file is written, so the scripts cannot run. Read the errors of that apply, fix them and
re-run it. To tear the partial stack down instead, run `scripts/90-teardown.sh --from-terraform`: it reads the values
from the terraform outputs ([DEPLOY.md](DEPLOY.md#teardown)).

### Pods stay Pending on another GPU shape

The manifests select the pool `gpu-b200-cb` and 8 GPUs per node. On any other shape `gen_env.py` refuses without
`--allow-untested`, and the pods stay Pending until you adapt their node selectors and GPU counts
([DEPLOY.md](DEPLOY.md#prerequisites)).

### An extra `cpu-demo` node stays after the staging Job

If the staging Job does not fit next to monitoring, Karpenter adds a second `cpu-demo` node. That pool never
consolidates, so the node stays and bills after the Job ends. Find the node that runs only DaemonSet pods and delete its
NodeClaim: `kubectl get nodeclaims -l karpenter.sh/nodepool=cpu-demo`, then `kubectl delete nodeclaim <name>`.

### `FailedCreatePodSandBox ... failed to setup network policy`

Seen on the first pod of a new GPU node in Auto Mode. It is transient: the pod started 13 s later with no action.
Investigate only if it repeats.

### Image pull is slow through the NAT

On a new node, the workers and the Frontend wait until the node has pulled the public runtime image from NGC through
the NAT gateway, which bills per GB. The size of the image, and the command that reads it, are in
[DEPLOY.md](DEPLOY.md#prerequisites). The `Pulled` event of the pod (`kubectl -n <namespace> get events`) says how long
the pull took.

## Serving

### Router does not see the L3 tier

After a cache flush (`flush_cache`), the KV router of the Frontend predicts a hit of 0 for a prefix that the worker
still serves from the HiCache storage tier (L3): the flush empties the GPU and host tiers but not L3, and the router
does not see L3 ([HiCache guide](optimizations/hicache/README.md#how-it-works)). Observed by us once on a single
worker (not published): the router predicted 0 while the engine served the prefix from L3. Nothing is broken; to see
the reuse from L3, read the engine metric `sglang:cached_tokens_total{cache_source="storage"}` (SGLang 0.5.21), not
the router hit rate. To empty L3 too, see [Reset between runs](optimizations/hicache/README.md#reset-between-runs).

### First request after boot is slow

The first request after a worker starts pays a warm-up (just-in-time compilation): in the tests an uncached 8K prompt
had a TTFT of 1.88 s on the first request and 0.85 s once the worker was warm. Send one request before you measure.

### KV transfer silently on TCP: how to detect and what changes it

Symptom: a prefill/decode (PD) cell answers correctly and logs no error, but the TTFT is high (Dynamo's EFA guide says
about 100 s) and, by our reading of the mechanism (not tested), the bytes on eth0 of the prefill pod grow by about the
KV bytes. Nothing fails, because TCP is a valid transport.

Cause: the NIXL backend the pods run is not one they can use at speed. On EFA, UCX or an unset
`SGLANG_DISAGGREGATION_NIXL_BACKEND` falls back to TCP; the EFA cells of this repository set `LIBFABRIC`.

Detect it: each engine's boot log must show `Backend LIBFABRIC was instantiated`; the eth0 bytes of the prefill pod in
`/proc/net/dev` should not grow by about the KV bytes (not tested; see
[Check that it works in the PD guide](optimizations/pd-disaggregation/README.md#check-that-it-works)).

What changes it: the prefill and decode workers of the PD examples set LIBFABRIC, the EFA environment and one EFA
interface per pod. Apply a PD example from `examples/<folder>/manifest.yaml` (for example
`examples/03-pd-disaggregation/manifest.yaml`).

## Model staging exit codes

When the Job fails, `scripts/10-stage-model.sh` stops and prints the `kubectl logs` command of the Job. The log line says
what happened, and the exit code of the Job's container (`EXIT_*` in `tools/stage_model/stage_model.py`) is shown as
`Exit Code` by `kubectl -n node-ops describe pods -l job-name=model-stage`. The Job does not retry codes 2 to 5.

| Exit | Meaning | Fix |
|---|---|---|
| `2` | Configuration error: a malformed `MODEL_ID`, `MODEL_REVISION`, `MODEL_S3_URI` or `HF_TOKEN` | Fix the value in the tfvars, apply, run `gen_env.py` again; for the token, export a valid one. |
| `3` | Gated model and no token: `HF_TOKEN` is not set | Accept the license on the model page, then `export HF_TOKEN=<read token>` in the shell that runs `scripts/10-stage-model.sh` (not in `platform.env`: `gen_env.py` overwrites it). |
| `4` | Access denied: Hugging Face refused the token (HTTP 401 or 403) | Accept the license with the account that owns the token (some models wait for the author's approval); use a valid read token. |
| `5` | Not found: wrong model or revision (HTTP 404), or no weights in the repository | Check `model_id` and `model_revision` (a commit SHA) on the model page. |
| `6` | Verify failed: a file size or sha256 differs from what the Hub lists | Run the script again; the files already in S3 are kept. |
| `7` | Transfer error, including S3 AccessDenied outside the applied prefix | Usually a second model: one model per apply. Change `model_id` and `model_revision`, apply, run `gen_env.py`, then stage. |
