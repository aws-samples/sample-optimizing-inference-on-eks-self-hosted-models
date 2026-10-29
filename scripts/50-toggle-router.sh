#!/usr/bin/env bash
# shellcheck source-path=SCRIPTDIR
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
# 50-toggle-router.sh <rr|kv> <namespace>
#
# R toggle (spec 9.6): render manifests/track-q-qwen38/frontends/<mode> and server-side-apply it
# onto the live DGD. spec.components is a list-map keyed by name, so only the Frontend entry
# is touched; never merge-patch the components list (a merge patch replaces it). The worker
# hash excludes frontend changes, so only the frontend rolls. Prints the elapsed seconds of
# the apply; the caller waits for the frontend roll itself.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
source "$SCRIPT_DIR/lib/common.sh"

usage() {
  echo "usage: $SCRIPT_NAME <rr|kv> <s-agg|s-agg-hic|s-dis|s-dis-hic>" >&2
  exit 2
}

[ $# -eq 2 ] || usage
mode="$1"
namespace="$2"
router_env_value "$mode" >/dev/null || usage
case " $SCENARIO_NAMESPACES " in
  *" $namespace "*) ;;
  *) usage ;;
esac

require_kube_target
overlay="$(frontend_overlay "$mode")"
[ -d "$overlay" ] || die "missing overlay $overlay"

start="$(now)"
rendered="$(kubectl kustomize "$overlay" </dev/null)"
printf '%s\n' "$rendered" |
  kube apply --server-side --field-manager=demo-router-toggle --force-conflicts -n "$namespace" -f -
elapsed=$(($(now) - start))
log "router=$mode namespace=$namespace dgd=$DGD_NAME applied=server-side elapsed_s=$elapsed"
echo "elapsed_s=$elapsed"
