#!/usr/bin/env bash
# shellcheck source-path=SCRIPTDIR
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
#
# 10-stage-model.sh [--dry-run]
#
# Hugging Face -> S3 model staging: runs Job node-ops/model-stage (tools/stage_model/stage_model.py
# and its pinned requirements.txt, mounted from the ConfigMap model-stage-code) on the cpu-demo pool
# and waits for it. The Job writes the weights, config, tokenizer and licence files of
# MODEL_ID@MODEL_REVISION flat under MODEL_S3_URI and <MODEL_S3_URI>/.done last: the layout
# 11-prefetch.sh reads. Idempotent: a .done of the same revision ends the Job at once and files already
# in S3 with the size the Hub lists are skipped, so a failed run resumes. A previous Job is replaced
# only once it is finished.
#   --dry-run  print the ConfigMap and the Job (nothing else on stdout); no cluster call
# Inputs (the process environment wins over the files):
#   SAMPLE_ENV      default config/sample.env: MODEL_ID, MODEL_REVISION (40-hex commit sha, never a
#                   branch), MODEL_S3_PREFIX and MODEL_S3_URI (default s3://<MODEL_BUCKET>/<prefix>,
#                   prefix default <MODEL_ID>/<MODEL_REVISION>)
#   PLATFORM_ENV    MODEL_BUCKET and AWS_REGION (read by lib/common.sh)
#   STAGE_IMAGE     Python image of the Job pinned by digest, <repo>:<tag>@sha256:<64 hex>; the
#                   default is a placeholder the preflight refuses
#   STAGE_DISK_GIB  scratch disk of the pod (default 50): the largest file of the repo must fit,
#                   since the Job keeps one file at a time on disk
#   HF_TOKEN        optional, gated models only: from the process environment ONLY (export it in the
#                   shell). The env files are generated and may be shared, so a token in them is ignored.
# HF_TOKEN (gated models only) never reaches argv, a manifest, stdout, a log or a shell trace: when it
# is set it goes to kubectl on stdin as the ephemeral Secret node-ops/hf-token, which the Job reads
# through an optional secretKeyRef, and a trap deletes the Secret when this script exits (Job done or
# failed, error, INT or TERM). Every run deletes a Secret left by a killed run before it applies the
# Job, token or not. The Job sends it to gated models only. The other option, already in Terraform,
# is var.hf_token_secret_arn (Secrets Manager, granted to the model-prefetch role only).
# The Job's AWS credentials: Pod Identity role model-stager (terraform/cluster/iam-model-access.tf),
# write access under the model prefix only.
# One model per apply: that prefix is the model_s3_prefix applied in terraform/cluster (default
# <model_id>/<model_revision>), so the role can list, read and write the applied model only. To stage
# another model or revision, change model_id and model_revision (or model_s3_prefix) in
# terraform/cluster/terraform.tfvars, run terraform apply and python3 scripts/gen_env.py, then this
# script. A MODEL_ID or MODEL_S3_URI override alone points the Job outside the role's prefix: it fails
# with a transfer error (S3 AccessDenied).

# Shell tracing (bash -x, or SHELLOPTS in the environment) would print HF_TOKEN: it is turned off
# before the token is read and stays off for the whole run.
case $- in *x*) set +x ;; esac
set -euo pipefail

# Read before lib/common.sh, which defaults MODEL_ID and MODEL_REVISION to the platform config's.
env_model_id="${MODEL_ID:-}"
env_model_revision="${MODEL_REVISION:-}"
env_model_s3_prefix="${MODEL_S3_PREFIX:-}"
env_model_s3_uri="${MODEL_S3_URI:-}"
env_hf_token="${HF_TOKEN:-}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
source "$SCRIPT_DIR/lib/common.sh"

SAMPLE_ENV="${SAMPLE_ENV:-$REPO_ROOT/config/sample.env}"
STAGE_IMAGE="${STAGE_IMAGE:-public.ecr.aws/docker/library/python:3.12-slim@sha256:<DIGEST>}"
STAGE_DISK_GIB="${STAGE_DISK_GIB:-50}"
TOOL_DIR="$REPO_ROOT/tools/stage_model"
STAGE_JOB="model-stage"
STAGE_CONFIGMAP="model-stage-code"
STAGE_SA="model-stager"
STAGE_SECRET="hf-token"

usage() {
  echo "usage: $SCRIPT_NAME [--dry-run]" >&2
  exit 2
}

dry_run=0
case $# in
  0) ;;
  1)
    [ "$1" = "--dry-run" ] || usage
    dry_run=1
    ;;
  *) usage ;;
esac

# ---------------------------------------------------------------- inputs (env file, then process env)
file_model_id=""
file_model_revision=""
file_model_s3_prefix=""
file_model_s3_uri=""
if [ -f "$SAMPLE_ENV" ]; then
  while IFS='=' read -r key value || [ -n "$key" ]; do
    case "$key" in
      MODEL_ID) file_model_id="$value" ;;
      MODEL_REVISION) file_model_revision="$value" ;;
      MODEL_S3_PREFIX) file_model_s3_prefix="$value" ;;
      MODEL_S3_URI) file_model_s3_uri="$value" ;;
    esac
  done <"$SAMPLE_ENV"
fi
model_id="${env_model_id:-$file_model_id}"
revision="${env_model_revision:-$file_model_revision}"
hf_token="$env_hf_token"
bucket="${MODEL_BUCKET:-${CFG_MODEL_BUCKET:-}}"
# Region of the bucket, from the config (never the caller's ambient AWS_REGION), as in 11-prefetch.sh.
region="${MODEL_BUCKET_REGION:-${CFG_AWS_REGION:-}}"

# ---------------------------------------------------------------- preflight (before any kubectl call)
image_ok=0
if printf '%s' "$STAGE_IMAGE" | grep -Eq '^[a-z0-9][a-z0-9._/:-]*@sha256:[0-9a-f]{64}$'; then
  image_ok=1
fi
case "$STAGE_IMAGE" in
  *:latest@* | *@sha256:0000000000000000000000000000000000000000000000000000000000000000) image_ok=0 ;;
esac
[ "$image_ok" = 1 ] ||
  die "STAGE_IMAGE must be pinned by digest, <repo>:<tag>@sha256:<64 hex> and never :latest (got '$STAGE_IMAGE'; the default is a placeholder): resolve the digest once, e.g. docker buildx imagetools inspect public.ecr.aws/docker/library/python:3.12-slim, then export STAGE_IMAGE=<repo>:<tag>@sha256:<digest>"
if ! is_uint "$STAGE_DISK_GIB" || [ "$((10#$STAGE_DISK_GIB))" -lt 1 ]; then
  die "STAGE_DISK_GIB must be a whole number of GiB >= 1 (got '$STAGE_DISK_GIB')"
fi
disk_gib="$((10#$STAGE_DISK_GIB))"

case "$model_id" in
  '') die "MODEL_ID is not set (<org>/<name>, in $SAMPLE_ENV or the environment)" ;;
  # Path-ish parts are refused too: the default S3 prefix is <MODEL_ID>/<MODEL_REVISION>.
  */*/* | /* | */ | ./* | ../* | */. | */.. | *[!A-Za-z0-9._/-]*)
    die "MODEL_ID must be <org>/<name> of [A-Za-z0-9._-], neither part . or .. (got '$model_id')"
    ;;
  */*) ;;
  *) die "MODEL_ID must be <org>/<name> of [A-Za-z0-9._-], neither part . or .. (got '$model_id')" ;;
esac
case "$revision" in
  '' | *[!0-9a-f]*) revision_ok=0 ;;
  *) revision_ok=1 ;;
esac
if [ "$revision_ok" = 0 ] || [ "${#revision}" -ne 40 ]; then
  die "MODEL_REVISION must be the 40-character lowercase hex commit sha of the model, never a branch or tag such as main (got '$revision')"
fi
# The token is validated without ever being printed.
if [ -n "$hf_token" ]; then
  case "$hf_token" in
    *[!A-Za-z0-9._-]*) die "HF_TOKEN is malformed: expected 8 to 4096 characters of A-Za-z0-9._- (the value is not printed)" ;;
  esac
  if [ "${#hf_token}" -lt 8 ] || [ "${#hf_token}" -gt 4096 ]; then
    die "HF_TOKEN is malformed: expected 8 to 4096 characters of A-Za-z0-9._- (the value is not printed)"
  fi
fi

generated_uri="${file_model_s3_uri%/}"
if [ -n "$env_model_id$env_model_revision" ] && [ -z "$env_model_s3_uri$env_model_s3_prefix" ]; then
  # A model or revision override never reuses the env file's path: it names the file's revision.
  file_model_s3_uri=""
  file_model_s3_prefix=""
fi
uri="${env_model_s3_uri:-$file_model_s3_uri}"
if [ -z "$uri" ]; then
  [ -n "$bucket" ] || die "MODEL_S3_URI is not set and MODEL_BUCKET is empty (fill $PLATFORM_ENV or set MODEL_S3_URI)"
  uri="s3://$bucket/${env_model_s3_prefix:-${file_model_s3_prefix:-$model_id/$revision}}"
fi
uri="${uri%/}"
case "$uri" in
  s3://?*/?*) ;;
  *) die "MODEL_S3_URI must be s3://<bucket>/<prefix> (got '$uri')" ;;
esac
uri_bucket="${uri#s3://}"
uri_bucket="${uri_bucket%%/*}"
uri_prefix="${uri#s3://*/}"
case "$uri_bucket" in
  *[!a-z0-9.-]*) die "MODEL_S3_URI: invalid bucket '$uri_bucket'" ;;
esac
case "/$uri_prefix/" in
  *//* | */./* | */../* | *[!A-Za-z0-9._/-]*) die "MODEL_S3_URI: invalid prefix '$uri_prefix' (segments of A-Za-z0-9._- separated by single slashes)" ;;
esac
if [ -n "$generated_uri" ] && [ "$uri" != "$generated_uri" ]; then
  log "warning: target $uri differs from MODEL_S3_URI of $SAMPLE_ENV; the model-stager role writes only under the model_s3_prefix applied in terraform/cluster (else AccessDenied)"
fi
[ -n "$region" ] || die "AWS_REGION is empty: fill $PLATFORM_ENV (or set MODEL_BUCKET_REGION)"
case "$region" in
  *[!a-z0-9-]*) die "invalid AWS_REGION '$region'" ;;
esac
for f in stage_model.py requirements.txt; do
  [ -f "$TOOL_DIR/$f" ] || die "missing $TOOL_DIR/$f"
done
ctx=""
[ -z "${KUBE_CONTEXT:-}" ] || ctx="--context=$KUBE_CONTEXT "

# ---------------------------------------------------------------- render (offline)
work="$(mktemp -d "${TMPDIR:-/tmp}/stage-model.XXXXXX")"
secret_created=0
# Deletes the ephemeral token Secret (when this run created it) and the render directory.
cleanup() {
  if [ "$secret_created" = 1 ]; then
    secret_created=0
    if kube delete secret "$STAGE_SECRET" -n "$NODE_OPS_NS" --ignore-not-found </dev/null >&2; then
      log "step=secret-deleted secret=$NODE_OPS_NS/$STAGE_SECRET"
    else
      log "warning: could not delete Secret $NODE_OPS_NS/$STAGE_SECRET; delete it by hand: kubectl ${ctx}delete secret $STAGE_SECRET -n $NODE_OPS_NS"
    fi
  fi
  rm -rf "$work"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

# render_job: the Job manifest (every value interpolated here was validated above; HF_TOKEN is only a
# reference to the optional Secret).
render_job() {
  cat <<EOF
apiVersion: batch/v1
kind: Job
metadata:
  name: $STAGE_JOB
  namespace: $NODE_OPS_NS
  labels:
    app.kubernetes.io/name: $STAGE_JOB
    app.kubernetes.io/part-of: dynamo-sample
spec:
  backoffLimit: 1
  activeDeadlineSeconds: 21600
  ttlSecondsAfterFinished: 86400
  podFailurePolicy:
    rules:
      # EXIT_CONFIG, EXIT_GATED_NO_TOKEN, EXIT_ACCESS_DENIED, EXIT_NOT_FOUND of stage_model.py:
      # a retry cannot fix them.
      - action: FailJob
        onExitCodes:
          containerName: stage
          operator: In
          values: [2, 3, 4, 5]
  template:
    metadata:
      labels:
        app.kubernetes.io/name: $STAGE_JOB
      annotations:
        karpenter.sh/do-not-disrupt: "true"
    spec:
      serviceAccountName: $STAGE_SA
      automountServiceAccountToken: false
      restartPolicy: Never
      nodeSelector:
        karpenter.sh/nodepool: cpu-demo
      securityContext:
        runAsNonRoot: true
        runAsUser: 65532
        runAsGroup: 65532
        fsGroup: 65532
        seccompProfile:
          type: RuntimeDefault
      containers:
        - name: stage
          image: "$STAGE_IMAGE"
          imagePullPolicy: IfNotPresent
          command: ["/bin/sh", "-c"]
          args:
            - "python -m pip install --quiet --no-cache-dir --disable-pip-version-check --only-binary=:all: --target /work/deps -r /code/requirements.txt && exec python /code/stage_model.py"
          env:
            - name: MODEL_ID
              value: "$model_id"
            - name: MODEL_REVISION
              value: "$revision"
            - name: MODEL_S3_URI
              value: "$uri"
            - name: AWS_REGION
              value: "$region"
            - name: HF_TOKEN
              valueFrom:
                secretKeyRef:
                  name: $STAGE_SECRET
                  key: HF_TOKEN
                  optional: true
            - name: WORK_DIR
              value: /work/files
            - name: PYTHONPATH
              value: /work/deps
            - name: HOME
              value: /work
            - name: TMPDIR
              value: /work
            - name: HF_HOME
              value: /work/hf
            # The token is passed explicitly, to gated models only; never picked up implicitly.
            - name: HF_HUB_DISABLE_IMPLICIT_TOKEN
              value: "1"
            - name: HF_HUB_DISABLE_PROGRESS_BARS
              value: "1"
          # One file at a time (pip, the hf_xet download, boto3's 10 x 8 MiB upload parts) needs about
          # 1 vCPU and well under 2Gi: sized to fit next to monitoring on the one cpu-demo node.
          resources:
            requests:
              cpu: "1"
              memory: 2Gi
              ephemeral-storage: ${disk_gib}Gi
            limits:
              memory: 8Gi
              ephemeral-storage: ${disk_gib}Gi
          securityContext:
            allowPrivilegeEscalation: false
            readOnlyRootFilesystem: true
            capabilities:
              drop: ["ALL"]
          volumeMounts:
            - name: code
              mountPath: /code
              readOnly: true
            - name: work
              mountPath: /work
      volumes:
        - name: code
          configMap:
            name: $STAGE_CONFIGMAP
        - name: work
          emptyDir:
            sizeLimit: ${disk_gib}Gi
EOF
}

# Client-side render (no API call); through kube like every kubectl call, so a set KUBE_CONTEXT must
# exist in the kubeconfig.
kube create configmap "$STAGE_CONFIGMAP" -n "$NODE_OPS_NS" \
  --from-file="stage_model.py=$TOOL_DIR/stage_model.py" \
  --from-file="requirements.txt=$TOOL_DIR/requirements.txt" \
  --dry-run=client -o yaml </dev/null >"$work/configmap.yaml"
render_job >"$work/job.yaml"
{
  cat "$work/configmap.yaml"
  echo "---"
  cat "$work/job.yaml"
} >"$work/render.yaml"
# auth= has the name and values of the Job's own event=model line (tools/stage_model): bearer when a
# token goes to the Job, which sends it to a gated repository only; none otherwise.
auth_state="none"
[ -z "$hf_token" ] || auth_state="bearer"
log "model=$model_id revision=$revision target=$uri/ image=$STAGE_IMAGE disk_gib=$disk_gib auth=$auth_state"
if [ "$dry_run" = 1 ]; then
  cat "$work/render.yaml"
  exit 0
fi

# ---------------------------------------------------------------- apply and wait
require_kube_target
replace_finished_job "$STAGE_JOB"
# A Secret left by a killed run (SIGKILL skips the trap) is never reused, also when this run has no
# token: the Job would read the stale value through its optional secretKeyRef.
kube delete secret "$STAGE_SECRET" -n "$NODE_OPS_NS" --ignore-not-found </dev/null >&2
if [ -n "$hf_token" ]; then
  secret_created=1
  printf 'HF_TOKEN=%s\n' "$hf_token" |
    kube create secret generic "$STAGE_SECRET" -n "$NODE_OPS_NS" --from-env-file=/dev/stdin >&2
  log "step=secret secret=$NODE_OPS_NS/$STAGE_SECRET lifetime=until-this-script-exits"
fi
kube apply --server-side --field-manager=demo-stage-model -f "$work/render.yaml" </dev/null >&2
log "result=submitted job=$NODE_OPS_NS/$STAGE_JOB"
echo "follow: kubectl ${ctx}logs -n $NODE_OPS_NS -f job/$STAGE_JOB"

# Polls the Job's conditions every 5 s, so a Failed Job is seen as fast as a Complete one (kubectl wait
# follows a single condition). No deadline of its own: activeDeadlineSeconds fails a Job that hangs.
last_log=0
while :; do
  json="$(kube get job "$STAGE_JOB" -n "$NODE_OPS_NS" -o json --ignore-not-found </dev/null)" ||
    die "cannot read the state of Job $NODE_OPS_NS/$STAGE_JOB"
  state="$(jq -r '
    if any((.status.conditions // [])[]; .type == "Complete" and .status == "True") then "complete"
    elif any((.status.conditions // [])[]; .type == "Failed" and .status == "True") then "failed"
    else "running" end' <<<"$json")" || die "cannot parse the state of Job $NODE_OPS_NS/$STAGE_JOB"
  case "$state" in
    complete) break ;;
    running)
      if [ "$(($(now) - last_log))" -ge 60 ]; then
        log "step=wait job=$NODE_OPS_NS/$STAGE_JOB status=running"
        last_log="$(now)"
      fi
      sleep 5
      ;;
    failed)
      kube logs -n "$NODE_OPS_NS" "job/$STAGE_JOB" --tail=30 </dev/null >&2 || true
      die "Job $NODE_OPS_NS/$STAGE_JOB failed; logs: kubectl ${ctx}logs -n $NODE_OPS_NS job/$STAGE_JOB"
      ;;
    *) die "Job $NODE_OPS_NS/$STAGE_JOB disappeared before it finished" ;;
  esac
done
log "result=staged uri=$uri/"
echo "staged: $uri/ (marker $uri/.done; check: aws s3 cp $uri/.done - --region $region)"
