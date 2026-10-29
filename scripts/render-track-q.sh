#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
# render-track-q.sh [OUT_DIR]
#
# Regenerates the committed Track Q (Qwen3.8-27B-FP8) cell renders:
#   OUT_DIR/S<n>.yaml = `kubectl kustomize manifests/track-q-qwen38/cells/S<n>`, for S0..S7.
# Default OUT_DIR: manifests/track-q-qwen38/rendered (the files tests/manifests/test_412 drift-checks
# against the live kustomize build). Offline: it only renders and never talks to a cluster. Every cell
# is rendered first; OUT_DIR is written only once all eight renders succeeded.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
CELLS_DIR="$REPO_ROOT/manifests/track-q-qwen38/cells"
CELLS="S0 S1 S2 S3 S4 S5 S6 S7"

if [ $# -gt 1 ]; then
  echo "usage: $(basename "$0") [OUT_DIR]" >&2
  exit 2
fi
out_dir="${1:-$REPO_ROOT/manifests/track-q-qwen38/rendered}"

work="$(mktemp -d "${TMPDIR:-/tmp}/render-track-q.XXXXXX")"
trap 'rm -rf "$work"' EXIT
for cell in $CELLS; do
  kubectl kustomize "$CELLS_DIR/$cell" </dev/null >"$work/$cell.yaml"
done
mkdir -p "$out_dir"
for cell in $CELLS; do
  mv "$work/$cell.yaml" "$out_dir/$cell.yaml"
done
printf '%s %s cells=%s out_dir=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$(basename "$0")" "${CELLS// /,}" "$out_dir" >&2
