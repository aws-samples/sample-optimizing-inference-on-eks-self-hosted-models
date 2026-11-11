# Model staging Job (Hugging Face -> S3)

`scripts/10-stage-model.sh` copies `MODEL_ID@MODEL_REVISION` from Hugging Face to `MODEL_S3_URI`: the files land flat under the prefix and `<MODEL_S3_URI>/.done` is written last, the layout `scripts/11-prefetch.sh` reads. It runs `stage_model.py` as Job `node-ops/model-stage` on the `cpu-demo` pool with the Pod Identity role `model-stager`, which may write under the model prefix only.

How to run it, the image, the Hugging Face token and the Secrets Manager option: [docs/DEPLOY.md, step 4](../../docs/DEPLOY.md#4-stage-the-model-scripts10-stage-modelsh). The script reads the model from `manifests/config/platform.env` (`scripts/gen_env.py`); the process environment wins.

```bash
aws s3 cp <MODEL_S3_URI>/.done - --region <AWS_REGION>   # model_id, revision, file_count, total_bytes
```

- **Token Secret.** An exported `HF_TOKEN` reaches the cluster on stdin as the ephemeral Secret `node-ops/hf-token`, which the script deletes when it exits; every run also deletes a Secret left by a killed run before it applies the Job.
- **Idempotent.** A `.done` of the same model and revision ends the Job at once. Files already in S3 with the size the Hub lists are skipped, so a failed run resumes.
- **One model per apply.** The `model-stager` role may list, read and write only under the prefix applied in `terraform/cluster` (`model_s3_prefix`, default `<model_id>/<model_revision>`). To stage another model or revision, change `model_id` and `model_revision` (or `model_s3_prefix`) in `terraform/cluster/terraform.tfvars`, run `terraform apply` and `python3 scripts/gen_env.py`, then `scripts/10-stage-model.sh`. A `MODEL_ID` or `MODEL_S3_URI` override alone points the Job outside the role's prefix, and it fails with a transfer error (S3 AccessDenied).
- **Node.** The Job requests 1 vCPU and 2 GiB, so it fits next to monitoring on the one `cpu-demo` node. If Karpenter still adds a second `cpu-demo` node for it, that node stays after the Job ends: the pool never consolidates. Once the Job is done, find the node that runs only DaemonSet pods (`kubectl get pods -A -o wide --field-selector spec.nodeName=<node>`) and delete its NodeClaim (`kubectl get nodeclaims -l karpenter.sh/nodepool=cpu-demo`, then `kubectl delete nodeclaim <name>`).

**Do not reuse a custom prefix across revisions.** The skip compares sizes only and nothing is deleted, so files of two revisions can mix under one prefix. A `.done` of another revision does not end the Job: it restages and rewrites the marker. Use a prefix that includes the revision, as the default does.

**Known limitation.** The Job installs `requirements.txt` with pip at run time. Only the two top-level packages (`huggingface_hub`, `boto3`) are pinned; their transitive dependencies are not hash-pinned.
