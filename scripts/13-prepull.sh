#!/usr/bin/env bash
# shellcheck source-path=SCRIPTDIR
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
# 13-prepull.sh [--delete]
#
# PREPULL-B1: keeps the sglang-runtime image cached on every cpu-demo node through the
# prepull-sglang-runtime DaemonSet (node-ops), so a cpu-demo node created during a GPU block
# never makes the Frontend pull 13.75 GB at boot.
#   default   apply manifests/node-ops/prepull (the RUNTIME_IMAGE of the platform config), then
#             watch the rollout with --timeout=$PREPULL_TIMEOUT (default 1800s; a whole number
#             >= 1 with an optional s, m or h suffix). A rollout that does not finish exits 1.
#   --delete  delete the DaemonSet (cached images stay on the nodes until kubelet image GC).
# Runs against the demo cluster (KUBE_CONTEXT guard) or in-cluster. kubectl calls are chained
# through temp files, never piped into each other (the stub never reads stdin).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
source "$SCRIPT_DIR/lib/common.sh"

PREPULL_DS="prepull-sglang-runtime"
PREPULL_TIMEOUT="${PREPULL_TIMEOUT:-1800s}"

usage() {
  echo "usage: $SCRIPT_NAME [--delete]" >&2
  exit 2
}

mode="apply"
case $# in
  0) ;;
  1)
    [ "$1" = "--delete" ] || usage
    mode="delete"
    ;;
  *) usage ;;
esac

if [ "$mode" = "delete" ]; then
  require_kube_target
  log "delete daemonset=$NODE_OPS_NS/$PREPULL_DS"
  kube delete daemonset "$PREPULL_DS" -n "$NODE_OPS_NS" --ignore-not-found </dev/null
  exit 0
fi

timeout_value="${PREPULL_TIMEOUT%[smh]}"
if ! is_uint "$timeout_value" || [ "$((10#$timeout_value))" -lt 1 ]; then
  die "PREPULL_TIMEOUT must be a whole number >= 1 with an optional s, m or h suffix (got '$PREPULL_TIMEOUT')"
fi

overlay="$MANIFESTS_DIR/node-ops/prepull"
[ -d "$overlay" ] || die "missing overlay $overlay"

require_kube_target

work="$(mktemp -d "${TMPDIR:-/tmp}/prepull.XXXXXX")"
trap 'rm -rf "$work"' EXIT

log "apply overlay=$overlay daemonset=$NODE_OPS_NS/$PREPULL_DS timeout=$PREPULL_TIMEOUT"
kubectl kustomize "$overlay" </dev/null >"$work/rendered.yaml"
kube apply --server-side --field-manager=demo-prepull -f "$work/rendered.yaml" </dev/null
if ! kube rollout status "daemonset/$PREPULL_DS" -n "$NODE_OPS_NS" --timeout="$PREPULL_TIMEOUT" </dev/null; then
  die "daemonset/$PREPULL_DS did not finish rolling out within $PREPULL_TIMEOUT (pods: kubectl get pods -n $NODE_OPS_NS -l app.kubernetes.io/name=$PREPULL_DS)"
fi
log "result=ready daemonset=$NODE_OPS_NS/$PREPULL_DS"
