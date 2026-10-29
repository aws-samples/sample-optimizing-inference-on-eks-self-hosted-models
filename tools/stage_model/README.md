# Model staging Job (Hugging Face -> S3)

`scripts/10-stage-model.sh` copies `MODEL_ID@MODEL_REVISION` from Hugging Face to `MODEL_S3_URI`: the files land flat under the prefix and `<MODEL_S3_URI>/.done` is written last, the layout `scripts/11-prefetch.sh` reads. It runs `stage_model.py` as Job `node-ops/model-stage` on the `cpu-demo` pool with the Pod Identity role `model-stager`, which may write under the model prefix only.

```bash
# Inputs: config/sample.env and platform.env (scripts/gen_env.py); the process environment wins.
export STAGE_IMAGE=public.ecr.aws/docker/library/python:3.12-slim@sha256:<digest>
scripts/10-stage-model.sh --dry-run   # prints the ConfigMap and the Job, no cluster call
scripts/10-stage-model.sh             # applies the Job and waits for it (KUBE_CONTEXT must be set)
aws s3 cp <MODEL_S3_URI>/.done - --region <AWS_REGION>   # model_id, revision, file_count, total_bytes
```

- **Image.** `STAGE_IMAGE` must be pinned by digest (`<repo>:<tag>@sha256:<64 hex>`). The default is a placeholder the preflight refuses. Resolve the digest once, e.g. `docker buildx imagetools inspect public.ecr.aws/docker/library/python:3.12-slim`.
- **Public models** need no token. The Job never sends one to them, even when `HF_TOKEN` is set.
- **Gated models.** Accept the licence on the model page (`https://huggingface.co/<MODEL_ID>`) with the account that owns the token, then `export HF_TOKEN=<read token>` in the shell that runs `scripts/10-stage-model.sh` (never in `config/sample.env`, which `scripts/gen_env.py` overwrites). The token is optional and only sent for gated repos. It reaches the cluster on stdin as the ephemeral Secret `node-ops/hf-token`, which the script deletes when it exits; every run also deletes a Secret left by a killed run before it applies the Job.
- **Secrets Manager option.** `hf_token_secret_arn` in `terraform/cluster/terraform.tfvars` grants `secretsmanager:GetSecretValue` on that secret to the model-prefetch role only. The staging Job does not use it: it reads `HF_TOKEN` only.
- **Idempotent.** A `.done` of the same model and revision ends the Job at once. Files already in S3 with the size the Hub lists are skipped, so a failed run resumes.
- **One model per apply.** The `model-stager` role may list, read and write only under the prefix applied in `terraform/cluster` (`model_s3_prefix`, default `<model_id>/<model_revision>`). To stage another model or revision, change `model_id` and `model_revision` (or `model_s3_prefix`) in `terraform/cluster/terraform.tfvars`, run `terraform apply` and `python3 scripts/gen_env.py`, then `scripts/10-stage-model.sh`. A `MODEL_ID` or `MODEL_S3_URI` override alone points the Job outside the role's prefix, and it fails with a transfer error (S3 AccessDenied).
- **Node.** The Job requests 1 vCPU and 2 GiB, so it fits next to monitoring on the one `cpu-demo` node. If Karpenter still adds a second `cpu-demo` node for it, that node stays after the Job ends: the pool never consolidates. Once the Job is done, find the node that runs only DaemonSet pods (`kubectl get pods -A -o wide --field-selector spec.nodeName=<node>`) and delete its NodeClaim (`kubectl get nodeclaims -l karpenter.sh/nodepool=cpu-demo`, then `kubectl delete nodeclaim <name>`).

**Migration: the default prefix changed.** `MODEL_S3_PREFIX` now defaults to `<MODEL_ID>/<MODEL_REVISION>`. A bucket laid out the old way (the model under `Qwen3.8-27B-FP8/`) needs `model_s3_prefix = "Qwen3.8-27B-FP8"` in `terraform/cluster/terraform.tfvars`, then `terraform apply` and `python3 scripts/gen_env.py`. Without it the staging Job and `11-prefetch.sh` use the new prefix: the copy already in the bucket is not found and the model is staged again.

**Do not reuse a custom prefix across revisions.** The skip compares sizes only and nothing is deleted, so files of two revisions can mix under one prefix. A `.done` of another revision does not end the Job: it restages and rewrites the marker. Use a prefix that includes the revision, as the default does.

**Known limitation.** The Job installs `requirements.txt` with pip at run time. Only the two top-level packages (`huggingface_hub`, `boto3`) are pinned; their transitive dependencies are not hash-pinned.
