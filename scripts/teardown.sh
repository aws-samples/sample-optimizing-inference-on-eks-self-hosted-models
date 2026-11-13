#!/usr/bin/env bash
# shellcheck source-path=SCRIPTDIR
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
# teardown.sh [--yes] [--from-terraform] [--pool gpu-<name>]... [--timeout SECONDS] [--destroy-cluster]
# teardown.sh --after-destroy [--from-terraform] [--empty-models] [--yes]
#
# The teardown order of docs/DEPLOY.md. DRY RUN by default: every command is printed and none is
# run (not even a read); --yes executes.
#   cluster run (default)
#     1. delete every DynamoGraphDeployment of the scenario namespaces (SCENARIO_NAMESPACES)
#     2. delete the NodeClaims of every GPU pool (GPU_POOLS of the generated env, or each --pool) and
#        every cpu-demo NodeClaim whose node runs only DaemonSet pods (that pool never consolidates),
#        then wait up to --timeout seconds (default 1800) until no node and no NodeClaim of those GPU
#        pools is left; a timeout stops the run here
#     3. print terraform -chdir=<repo>/terraform/cluster destroy; it runs only with --destroy-cluster
#        --yes, and Terraform still asks for its own confirmation
#   --after-destroy (the cluster destroy has finished; no cluster call)
#     4. empty the results bucket: aws s3 rm --recursive, then every object version and delete marker
#        (list-object-versions + delete-objects, at most 1000 per batch, until the listing is empty);
#        the model bucket only with --empty-models too (restaging the weights is slow). The results bucket
#        holds the benchmark results under perf/: a WARNING line with the aws s3 sync that copies them comes
#        before its first delete, and after the next-step hint of the cluster run
#     5. print terraform -chdir=<repo>/terraform/bootstrap destroy (never run here)
# The namespaces come from lib/common.sh; the cluster, region, pools and bucket names only
# from the generated platform env (PLATFORM_ENV, written by scripts/gen_env.py), never from the caller's
# environment. KUBE_CONTEXT must name its CLUSTER_NAME; the run is off-cluster only.
# --from-terraform (after a failed or partial terraform apply, when gen_env.py wrote no platform env):
# the values come instead from `terraform -chdir=<repo>/terraform/cluster output -json` (cluster_name,
# region, gpu_pools, results_bucket_name and model_bucket_name) and, with
# --after-destroy, from `terraform -chdir=<repo>/terraform/bootstrap output -json` (results_bucket,
# model_storage_bucket);
# a value that no output holds (after the cluster destroy: the cluster name and the region) comes from
# TEARDOWN_CLUSTER_NAME, TEARDOWN_AWS_REGION, TEARDOWN_RESULTS_BUCKET or TEARDOWN_MODEL_BUCKET. Every
# source that holds a value must hold the same one. Those `terraform output -json` reads run even in a
# dry run (read only); they are its only calls. Without the flag the TEARDOWN_* variables are ignored.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
source "$SCRIPT_DIR/lib/common.sh"

DGD_RESOURCE="dynamographdeployments.nvidia.com"
CPU_POOL="cpu-demo"
POLL_S=15
S3_BATCH=1000

usage() {
  echo "usage: $SCRIPT_NAME [--yes] [--from-terraform] [--pool gpu-<name>]... [--timeout SECONDS] [--destroy-cluster]" >&2
  echo "       $SCRIPT_NAME --after-destroy [--from-terraform] [--empty-models] [--yes]" >&2
  exit 2
}

execute=0
after_destroy=0
destroy_cluster=0
empty_models=0
from_terraform=0
timeout_s=1800
pools=""
while [ $# -gt 0 ]; do
  case "$1" in
    --yes) execute=1 ;;
    --after-destroy) after_destroy=1 ;;
    --destroy-cluster) destroy_cluster=1 ;;
    --empty-models) empty_models=1 ;;
    --from-terraform) from_terraform=1 ;;
    --pool)
      [ $# -ge 2 ] || usage
      pools="$pools $2"
      shift
      ;;
    --timeout)
      [ $# -ge 2 ] || usage
      timeout_s="$2"
      shift
      ;;
    *) usage ;;
  esac
  shift
done
[ "$after_destroy$destroy_cluster" != 11 ] || usage
[ "$empty_models" = 0 ] || [ "$after_destroy" = 1 ] || usage

if ! is_uint "$timeout_s" || [ "$((10#$timeout_s))" -lt 1 ]; then
  die "--timeout must be a whole number of seconds >= 1 (got '$timeout_s')"
fi
timeout_s=$((10#$timeout_s))
for pool in $pools; do
  case "$pool" in
    gpu-*[!a-z0-9-]* | gpu-) die "--pool must be a GPU pool name gpu-<lowercase letters, digits, hyphens> (got '$pool')" ;;
    gpu-*) ;;
    *) die "--pool must be a GPU pool name gpu-<lowercase letters, digits, hyphens> (got '$pool')" ;;
  esac
done

# ---------------------------------------------------------------- the values and the cluster guard
cluster_dir="$REPO_ROOT/terraform/cluster"
bootstrap_dir="$REPO_ROOT/terraform/bootstrap"
[ -z "${KUBERNETES_SERVICE_HOST:-}" ] ||
  die "the teardown runs off-cluster only (KUBERNETES_SERVICE_HOST is set): run it from your workstation with KUBE_CONTEXT"

# lacks <KEY>: stop, no source holds KEY.
lacks() {
  if [ "$from_terraform" = 1 ]; then
    die "no $1: no terraform output holds it and TEARDOWN_$1 is not set (export TEARDOWN_$1=<value>; see docs/DEPLOY.md#teardown)"
  fi
  die "$PLATFORM_ENV lacks $1: run python3 scripts/gen_env.py"
}

# tf_output_json <stack dir>: `terraform output -json` of that stack (read only), or {} when the command
# fails (its error goes to stderr) or prints no JSON object: those outputs then count as absent.
tf_output_json() {
  local json=""
  if json="$(terraform "-chdir=$1" output -json </dev/null)" && jq -e 'type == "object"' >/dev/null 2>&1 <<<"$json"; then
    printf '%s\n' "$json"
  else
    log "warning: no usable terraform output -json in $1: its outputs count as absent"
    echo '{}'
  fi
}

# tf_string <outputs json> <name>: the string value of one output, empty when absent.
tf_string() {
  jq -r --arg n "$2" '.[$n].value // empty | strings' <<<"$1"
}

# pick <KEY> <value>...: set `picked` to the value that every non-empty argument holds (empty when none
# does); two different values stop the run.
pick() {
  local key="$1" v
  shift
  picked=""
  for v in "$@"; do
    [ -n "$v" ] || continue
    [ -z "$picked" ] || [ "$v" = "$picked" ] ||
      die "--from-terraform: the sources disagree on $key ('$picked' and '$v'): the outputs of terraform/cluster and terraform/bootstrap and TEARDOWN_$key must name the same stack"
    picked="$v"
  done
}

if [ "$from_terraform" = 1 ]; then
  # A failed or partial apply: no generated env, the values come from the terraform state (see the header).
  env_src="the terraform outputs (--from-terraform)"
  PLATFORM_ENV="$env_src" # what the context guard of lib/common.sh names as the source of CLUSTER_NAME
  cluster_json="$(tf_output_json "$cluster_dir")"
  bootstrap_json='{}'
  if [ "$after_destroy" = 1 ]; then
    bootstrap_json="$(tf_output_json "$bootstrap_dir")"
  fi
  pick CLUSTER_NAME "$(tf_string "$cluster_json" cluster_name)" "${TEARDOWN_CLUSTER_NAME:-}"
  CFG_CLUSTER_NAME="$picked"
  pick AWS_REGION "$(tf_string "$cluster_json" region)" "${TEARDOWN_AWS_REGION:-}"
  CFG_AWS_REGION="$picked"
  pick RESULTS_BUCKET "$(tf_string "$cluster_json" results_bucket_name)" \
    "$(tf_string "$bootstrap_json" results_bucket)" "${TEARDOWN_RESULTS_BUCKET:-}"
  CFG_RESULTS_BUCKET="$picked"
  pick MODEL_BUCKET "$(tf_string "$cluster_json" model_bucket_name)" \
    "$(tf_string "$bootstrap_json" model_storage_bucket)" "${TEARDOWN_MODEL_BUCKET:-}"
  CFG_MODEL_BUCKET="$picked"
  CFG_GPU_POOLS="$(jq -r '(.gpu_pools.value // {}) | if type == "object" then keys | join(",") else "" end' <<<"$cluster_json")"
else
  [ -f "$PLATFORM_ENV" ] ||
    die "no generated env at $PLATFORM_ENV: run python3 scripts/gen_env.py first; after a failed or partial terraform apply (no platform_env output) pass --from-terraform instead (docs/DEPLOY.md#teardown)"
  first_line=""
  IFS= read -r first_line <"$PLATFORM_ENV" || true
  case "$first_line" in
    "# GENERATED by scripts/gen_env.py"*) ;;
    *) die "$PLATFORM_ENV was not written by scripts/gen_env.py (no GENERATED header): run python3 scripts/gen_env.py" ;;
  esac
  env_src="env $PLATFORM_ENV"
fi
[ -n "${CFG_CLUSTER_NAME:-}" ] || lacks CLUSTER_NAME
case "$CFG_CLUSTER_NAME" in
  *[!A-Za-z0-9_-]*) die "invalid CLUSTER_NAME '$CFG_CLUSTER_NAME' in $env_src" ;;
esac
require_kube_target
region="${CFG_AWS_REGION:-}"
[ -n "$region" ] || lacks AWS_REGION
case "$region" in
  *[!a-z0-9-]*) die "invalid AWS_REGION '$region' in $env_src" ;;
esac

# require_bucket <KEY> <value>: a valid S3 bucket name from the generated env or the terraform outputs.
require_bucket() {
  [ -n "$2" ] || lacks "$1"
  case "$2" in
    *[!a-z0-9.-]*) die "invalid $1 '$2' in $env_src" ;;
  esac
  [ "${#2}" -ge 3 ] && [ "${#2}" -le 63 ] || die "invalid $1 '$2' in $env_src"
}

if [ "$after_destroy" = 1 ]; then
  results_bucket="${CFG_RESULTS_BUCKET:-}"
  models_bucket="${CFG_MODEL_BUCKET:-}"
  require_bucket RESULTS_BUCKET "$results_bucket"
  require_bucket MODEL_BUCKET "$models_bucket"
else
  if [ -z "$pools" ]; then
    if [ -z "${CFG_GPU_POOLS:-}" ]; then
      [ "$from_terraform" = 0 ] ||
        die "no GPU pool: the gpu_pools output of terraform/cluster is absent or empty: pass --pool gpu-<name> for each GPU pool"
      die "$PLATFORM_ENV lacks GPU_POOLS: run python3 scripts/gen_env.py or pass --pool gpu-<name>"
    fi
    old_ifs="$IFS"
    IFS=,
    for entry in $CFG_GPU_POOLS; do
      pools="$pools ${entry%%:*}"
    done
    IFS="$old_ifs"
  fi
  for pool in $pools; do
    case "$pool" in
      gpu-*[!a-z0-9-]* | gpu-) die "invalid GPU pool '$pool' in GPU_POOLS of $env_src" ;;
      gpu-*) ;;
      *) die "invalid GPU pool '$pool' in GPU_POOLS of $env_src" ;;
    esac
  done
fi

work="$(mktemp -d "${TMPDIR:-/tmp}/teardown.XXXXXX")"
trap 'rm -rf "$work"' EXIT

# ---------------------------------------------------------------- printing and running
# show <cmd...>: print a command (kube = kubectl --context=<KUBE_CONTEXT>), shell-quoted.
show() {
  local prefix='+'
  [ "$execute" = 1 ] || prefix='[dry-run]'
  if [ "$1" = kube ]; then
    shift
    set -- kubectl "--context=$KUBE_CONTEXT" "$@"
  fi
  printf '%s' "$prefix"
  printf ' %q' "$@"
  printf '\n'
}

# run <cmd...>: print it, then execute it only with --yes.
run() {
  show "$@"
  [ "$execute" = 1 ] || return 0
  "$@" </dev/null
}

# step <text...>: a step header on stdout and a structured log line.
step() {
  printf '\n== %s\n' "$*"
  log "step=$*"
}

# count_items <kubectl get args...>: number of objects a `kube get ... -o json` lists.
count_items() {
  kube get "$@" -o json </dev/null >"$work/items.json" || die "kubectl get $* failed"
  jq '.items | length' "$work/items.json"
}

# empty_bucket <bucket>: aws s3 rm, then every object version and delete marker, in batches.
empty_bucket() {
  local bucket="$1" batches=0 errors
  run aws s3 rm "s3://$bucket" --recursive --region "$region"
  if [ "$execute" = 0 ]; then
    show aws s3api list-object-versions --bucket "$bucket" --region "$region" --no-paginate --output json
    echo "[dry-run] aws s3api delete-objects --bucket $bucket --region $region --delete file://<batch: up to $S3_BATCH versions and delete markers>"
    echo "  (repeated until the listing holds no version and no delete marker)"
    return 0
  fi
  while :; do
    aws s3api list-object-versions --bucket "$bucket" --region "$region" --no-paginate --output json \
      </dev/null >"$work/versions.json" || die "cannot list the object versions of s3://$bucket"
    jq -c --argjson n "$S3_BATCH" '[(.Versions // [])[], (.DeleteMarkers // [])[] | {Key, VersionId}]
      | . as $all | range(0; length; $n) | {Objects: $all[.:(. + $n)], Quiet: true}' \
      "$work/versions.json" >"$work/batches.jsonl"
    [ -s "$work/batches.jsonl" ] || break
    while IFS= read -r batch; do
      printf '%s\n' "$batch" >"$work/batch.json"
      aws s3api delete-objects --bucket "$bucket" --region "$region" --delete "file://$work/batch.json" \
        --output json </dev/null >"$work/deleted.json" || die "delete-objects failed on s3://$bucket"
      errors="$(jq -s '[.[] | (.Errors // [])[]] | length' "$work/deleted.json")"
      if [ "$errors" != 0 ]; then
        jq -c -s '[.[] | (.Errors // [])[]][:5]' "$work/deleted.json" >&2
        die "delete-objects returned $errors Errors on s3://$bucket (first ones above); nothing else is deleted"
      fi
      batches=$((batches + 1))
    done <"$work/batches.jsonl"
    log "step=empty-bucket bucket=$bucket batches=$batches"
  done
  echo "s3://$bucket is empty ($batches delete-objects batches)"
}

# results_warning <bucket>: step 4 deletes the results that benchmarks/perf.yaml uploads under perf/ of <bucket>.
results_warning() {
  echo "WARNING: --after-destroy --yes deletes every object version of s3://$1, the benchmark results under perf/ included; copy them first: aws s3 sync \"s3://$1/perf/\" ./results --region $region"
}

checklist() {
  printf '\n== checklist (after the cluster destroy)\n'
  echo "- no ENI left in the VPC, so the VPC is gone: aws ec2 describe-vpcs --region $region --filters Name=tag:Name,Values=$CFG_CLUSTER_NAME --query 'Vpcs[].VpcId'"
  echo "  (if one is left: aws ec2 describe-network-interfaces --region $region --filters Name=vpc-id,Values=<vpc-id>)"
  echo "- the 50 GiB Prometheus volume is gone: aws ec2 describe-volumes --region $region --filters Name=size,Values=50 Name=status,Values=available"
  echo "- a Capacity Block or an ODCR keeps billing until its end, with or without a node"
}

echo "teardown of cluster $CFG_CLUSTER_NAME (context $KUBE_CONTEXT, region $region), values from $env_src"
[ "$execute" = 1 ] || echo "DRY-RUN: every command is printed, nothing is run; pass --yes to execute"

# ---------------------------------------------------------------- 4-5: the buckets (--after-destroy)
if [ "$after_destroy" = 1 ]; then
  step "4/5 empty the buckets (the cluster destroy has finished)"
  results_warning "$results_bucket"
  empty_bucket "$results_bucket"
  if [ "$empty_models" = 1 ]; then
    empty_bucket "$models_bucket"
  else
    echo "kept: s3://$models_bucket (the weights; pass --empty-models to empty it too, the bootstrap destroy needs it empty)"
  fi
  step "5/5 destroy the buckets, yourself, when you no longer need the weights and the results"
  printf 'run:'
  printf ' %q' terraform "-chdir=$bootstrap_dir" destroy
  printf '\n'
  checklist
  [ "$execute" = 1 ] || echo "DRY-RUN: nothing was run; pass --yes to execute"
  exit 0
fi

# ---------------------------------------------------------------- 1: the cells
step "1/5 delete the cells: every DynamoGraphDeployment in $SCENARIO_NAMESPACES"
for ns in $SCENARIO_NAMESPACES; do
  run kube delete "$DGD_RESOURCE" --all -n "$ns" --ignore-not-found --wait=true --timeout="${timeout_s}s"
done

# ---------------------------------------------------------------- 2: the GPU nodes
step "2/5 delete the GPU NodeClaims (pools:$pools) and the empty $CPU_POOL NodeClaims"
for pool in $pools; do
  run kube delete nodeclaims -l "karpenter.sh/nodepool=$pool" --wait=false
done
if [ "$execute" = 0 ]; then
  show kube get nodeclaims -l "karpenter.sh/nodepool=$CPU_POOL" -o json
  echo "[dry-run] then, for each one whose node runs only DaemonSet pods: kubectl --context=$KUBE_CONTEXT delete nodeclaim <name> --wait=false"
  for pool in $pools; do
    show kube get nodeclaims -l "karpenter.sh/nodepool=$pool" -o json
    show kube get nodes -l "karpenter.sh/nodepool=$pool" -o json
  done
  echo "  (polled every ${POLL_S}s until both list nothing, for up to ${timeout_s}s)"
else
  kube get nodeclaims -l "karpenter.sh/nodepool=$CPU_POOL" -o json </dev/null >"$work/cpu-claims.json" ||
    die "cannot list the $CPU_POOL NodeClaims"
  jq -r '.items[] | select(.metadata.deletionTimestamp == null) | [.metadata.name, (.status.nodeName // "")] | @tsv' \
    "$work/cpu-claims.json" >"$work/cpu-claims.tsv"
  while IFS="$(printf '\t')" read -r claim node; do
    if [ -z "$node" ]; then
      log "step=cpu-demo nodeclaim=$claim node=none kept=1 (no node yet)"
      continue
    fi
    kube get pods -A --field-selector "spec.nodeName=$node" -o json </dev/null >"$work/pods.json" ||
      die "cannot list the pods of node $node"
    busy="$(jq '[.items[] | select((.status.phase // "") != "Succeeded" and (.status.phase // "") != "Failed")
      | select(([.metadata.ownerReferences[]? | .kind] | index("DaemonSet")) == null)
      | select((.metadata.annotations // {})["kubernetes.io/config.mirror"] == null)] | length' "$work/pods.json")"
    if [ "$busy" = 0 ]; then
      run kube delete nodeclaim "$claim" --wait=false
    else
      log "step=cpu-demo nodeclaim=$claim node=$node kept=1 workload_pods=$busy"
    fi
  done <"$work/cpu-claims.tsv"
  start="$(now)"
  deadline=$((start + timeout_s))
  while :; do
    left=0
    for pool in $pools; do
      claims="$(count_items nodeclaims -l "karpenter.sh/nodepool=$pool")"
      nodes="$(count_items nodes -l "karpenter.sh/nodepool=$pool")"
      left=$((left + claims + nodes))
    done
    [ "$left" -ne 0 ] || break
    [ "$(now)" -lt "$deadline" ] ||
      die "timeout: GPU nodes or NodeClaims of$pools are still there after ${timeout_s}s; stopping before the cluster destroy, look at them with kubectl get nodeclaims -o wide"
    log "step=wait-gpu left=$left elapsed_s=$(($(now) - start))"
    sleep "$POLL_S"
  done
  echo "no GPU node and no GPU NodeClaim left (pools:$pools)"
fi

# ---------------------------------------------------------------- 3: the cluster
step "3/5 destroy the cluster"
if [ "$destroy_cluster" = 1 ]; then
  show terraform "-chdir=$cluster_dir" destroy
  if [ "$execute" = 1 ]; then
    # Interactive on purpose: Terraform shows the plan and asks before it destroys anything.
    terraform "-chdir=$cluster_dir" destroy
  fi
else
  printf 'run (or pass --destroy-cluster --yes):'
  printf ' %q' terraform "-chdir=$cluster_dir" destroy
  printf '\n'
fi
if [ "$from_terraform" = 1 ]; then
  # The destroy removes the cluster outputs: the next run gets the cluster name and the region from these.
  printf 'next, once the cluster destroy has finished:'
  printf ' %q' "TEARDOWN_CLUSTER_NAME=$CFG_CLUSTER_NAME" "TEARDOWN_AWS_REGION=$region" "scripts/$SCRIPT_NAME" \
    --after-destroy --from-terraform
  printf ' [--empty-models] --yes\n'
else
  echo "next, once the cluster destroy has finished: scripts/$SCRIPT_NAME --after-destroy [--empty-models] --yes"
fi
results_warning "${CFG_RESULTS_BUCKET:-<results bucket>}"
printf 'then, for the buckets:'
printf ' %q' terraform "-chdir=$bootstrap_dir" destroy
printf '\n'
checklist
[ "$execute" = 1 ] || echo "DRY-RUN: nothing was run; pass --yes to execute"
