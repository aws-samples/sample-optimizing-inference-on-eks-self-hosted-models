#!/usr/bin/env bash
# shellcheck source-path=SCRIPTDIR
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
# 11-prefetch.sh [--model-id ORG/NAME] [--revision SHA] [--force] [--wait]
#
# S3 -> NVMe weights prefetch on the GPU node (spec 8.4 step 2, T-PLAT-3). Submits the
# node-ops helper pod and Job/model-prefetch, then returns: the copy, its timing (recorded in
# the .done marker) and the skip-if-present check all happen in the Job, in-cluster.
# The workers' init gate verifies the marker later. A previous
# Job/model-prefetch is replaced only once it is Complete or Failed; while it is still active the
# run is refused (non-zero), so a second submit never kills a running copy.
#   --model-id  the model, <org>/<name> (default: MODEL_ID of the env, else of the platform config);
#               the cache dir is models--<org>--<name>
#   --revision  its 40-hex commit sha (default: MODEL_REVISION of the env; the config's MODEL_REVISION
#               only for the config's MODEL_ID, so another model needs one of the two)
#   --force     re-sync even when the marker of the pinned revision is present (never overrides
#               the active-Job refusal)
#   --wait      also follow the Job to completion and verify the marker (interactive use)
# The source (always passed with one trailing /) is, first match wins: MODEL_S3_URI of the env;
# s3://<MODEL_BUCKET>/<MODEL_PREFIX>/ when either is set in the env (the missing half from the config:
# MODEL_BUCKET, MODEL_S3_PREFIX); MODEL_S3_URI of the config; s3://<bucket>/<MODEL_S3_PREFIX of the
# config>/; s3://<bucket>/<org>-<name>/. The config's URI and prefix belong to the config's MODEL_ID:
# another model always gets <org>-<name> unless MODEL_S3_URI or MODEL_PREFIX is set in the env.
set -euo pipefail

# Read before lib/common.sh, which defaults MODEL_REVISION to the config's: an env revision is told apart.
env_model_revision="${MODEL_REVISION:-}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
source "$SCRIPT_DIR/lib/common.sh"

MODEL_BUCKET="${MODEL_BUCKET:-}"
MODEL_PREFIX="${MODEL_PREFIX:-}"
MODEL_S3_URI="${MODEL_S3_URI:-}"
# Region of the bucket, from the config (never the caller's ambient AWS_REGION).
MODEL_BUCKET_REGION="${MODEL_BUCKET_REGION:-${CFG_AWS_REGION:-}}"
PREFETCH_TIMEOUT_MIN="${PREFETCH_TIMEOUT_MIN:-90}"
NODE_SHELL_TIMEOUT_S="${NODE_SHELL_TIMEOUT_S:-600}"
POLL_S="${POLL_S:-30}"

usage() {
  echo "usage: $SCRIPT_NAME [--model-id ORG/NAME] [--revision SHA] [--force] [--wait]" >&2
  exit 2
}

model_id="$MODEL_ID"
revision=""
force=0
follow=0
while [ $# -gt 0 ]; do
  case "$1" in
    --model-id)
      [ $# -ge 2 ] || usage
      model_id="$2"
      shift
      ;;
    --revision)
      [ $# -ge 2 ] || usage
      revision="$2"
      shift
      ;;
    --force) force=1 ;;
    --wait) follow=1 ;;
    *) usage ;;
  esac
  shift
done

case "$model_id" in
  '') die "no model: set MODEL_ID in $PLATFORM_ENV or pass --model-id <org>/<name>" ;;
  */*/* | /* | */ | ./* | ../* | */. | */.. | *[!A-Za-z0-9._/-]*)
    die "the model id must be <org>/<name> of [A-Za-z0-9._-], neither part . or .. (got '$model_id')"
    ;;
  */*) ;;
  *) die "the model id must be <org>/<name> of [A-Za-z0-9._-], neither part . or .. (got '$model_id')" ;;
esac
config_model=0
[ "$model_id" != "${CFG_MODEL_ID:-}" ] || config_model=1
if [ -z "$revision" ]; then
  revision="$env_model_revision"
  [ -n "$revision" ] || [ "$config_model" = 0 ] || revision="${CFG_MODEL_REVISION:-}"
fi
[ -n "$revision" ] ||
  die "no revision for $model_id: pass --revision <40-hex commit sha> (the config's MODEL_REVISION belongs to its MODEL_ID '${CFG_MODEL_ID:-}')"
case "$revision" in
  *[!0-9a-f]*) die "the revision must be the 40-character lowercase hex commit sha (got '$revision')" ;;
esac
[ "${#revision}" -eq 40 ] || die "the revision must be the 40-character lowercase hex commit sha (got '$revision')"
cache_repo="models--${model_id//\//--}"

config_uri=""
config_prefix=""
if [ "$config_model" = 1 ]; then
  config_uri="${CFG_MODEL_S3_URI:-}"
  config_prefix="${CFG_MODEL_S3_PREFIX:-}"
fi
bucket="${MODEL_BUCKET:-${CFG_MODEL_BUCKET:-}}"
if [ -n "$MODEL_S3_URI" ]; then
  source_uri="$MODEL_S3_URI"
elif [ -z "$MODEL_BUCKET$MODEL_PREFIX" ] && [ -n "$config_uri" ]; then
  source_uri="$config_uri"
else
  [ -n "$bucket" ] || die "MODEL_BUCKET is empty (set it or fill $PLATFORM_ENV)"
  source_uri="s3://$bucket/${MODEL_PREFIX:-${config_prefix:-${model_id%%/*}-${model_id#*/}}}"
fi
source_uri="${source_uri%/}/"
case "$source_uri" in
  s3://?*/?*) ;;
  *) die "the source must be s3://<bucket>/<prefix> (got '$source_uri')" ;;
esac
uri_bucket="${source_uri#s3://}"
uri_bucket="${uri_bucket%%/*}"
uri_prefix="${source_uri#s3://*/}"
case "$uri_bucket" in
  *[!a-z0-9.-]*) die "invalid bucket '$uri_bucket' in $source_uri" ;;
esac
case "/$uri_prefix" in
  *//* | */./* | */../* | *[!A-Za-z0-9._/-]*) die "invalid prefix '$uri_prefix' in $source_uri" ;;
esac
marker="/mnt/.ephemeral/models/hub/$cache_repo/.done"
for v in "$PREFETCH_TIMEOUT_MIN" "$NODE_SHELL_TIMEOUT_S" "$POLL_S"; do
  is_uint "$v" || die "timeouts must be whole numbers (got '$v')"
done
node_ops="$MANIFESTS_DIR/node-ops"
require_kube_target
log "model=$model_id source=$source_uri region=$MODEL_BUCKET_REGION revision=$revision force=$force"

# ---------------------------------------------------------------- submit (in-cluster work)
# A Job's pod template is immutable: replace the previous run, but only a finished one. A copy
# that is still running is never deleted (P-5), not even with --force: the run is refused
# before anything is applied.
replace_finished_job model-prefetch
kube apply --server-side --field-manager=demo-prefetch -k "$node_ops" \
  -l app.kubernetes.io/name=ephemeral-shell </dev/null
# Chain through temp files, chained through temp files, not piped (the stub kubectl does
# not read stdin and a pipe under `set -o pipefail` races on BrokenPipe).
work="$(mktemp -d "${TMPDIR:-/tmp}/prefetch.XXXXXX")"
trap 'rm -rf "$work"' EXIT
kubectl kustomize "$node_ops" </dev/null >"$work/rendered.yaml"
kube set env --local -f "$work/rendered.yaml" -c prefetch \
  "MODEL_S3_URI=$source_uri" "MODEL_REVISION=$revision" "MODEL_CACHE_REPO=$cache_repo" \
  "AWS_REGION=$MODEL_BUCKET_REGION" "FORCE=$force" -o yaml >"$work/final.yaml"
# `set env --local` prints nothing when every value already equals the Job's (the config
# model's defaults, P-30): the render is then submitted as is (its ephemeral-shell Pod was applied just
# above with the same field manager, so re-applying it changes nothing).
job_file="$work/final.yaml"
if ! grep -q '[^[:space:]]' "$job_file"; then
  log "step=set-env changed=0 (values equal the Job defaults): submitting the rendered Job"
  job_file="$work/rendered.yaml"
fi
kube apply --server-side --field-manager=demo-prefetch -f "$job_file" </dev/null
log "result=submitted job=$NODE_OPS_NS/model-prefetch"
if [ "$follow" -eq 0 ]; then
  echo "submitted: kubectl logs -n $NODE_OPS_NS -f job/model-prefetch (marker: $marker)"
  exit 0
fi

# ---------------------------------------------------------------- --wait: follow and verify
start="$(now)"
deadline=$((start + PREFETCH_TIMEOUT_MIN * 60))
while :; do
  status="$(kube get job model-prefetch -n "$NODE_OPS_NS" -o json </dev/null | jq -r '
    if any((.status.conditions // [])[]; .type == "Complete" and .status == "True") then "complete"
    elif any((.status.conditions // [])[]; .type == "Failed" and .status == "True") then "failed"
    else "running" end')"
  case "$status" in
    complete) break ;;
    failed)
      kube logs -n "$NODE_OPS_NS" job/model-prefetch --tail=50 </dev/null >&2 || true
      die "Job/model-prefetch failed"
      ;;
  esac
  [ "$(now)" -lt "$deadline" ] || die "Job/model-prefetch not complete after ${PREFETCH_TIMEOUT_MIN} min"
  log "step=prefetch status=running elapsed_s=$(($(now) - start))"
  sleep "$POLL_S"
done
kube wait --for=condition=Ready "pod/$NODE_SHELL" -n "$NODE_OPS_NS" --timeout="${NODE_SHELL_TIMEOUT_S}s" </dev/null ||
  die "$NODE_SHELL is not Ready on the GPU node"
node_shell grep -qx "revision=$revision" "$marker" ||
  die "Job completed but $marker does not record revision $revision"
node_shell cat "$marker"
log "result=prefetched model=$model_id marker=$marker"
