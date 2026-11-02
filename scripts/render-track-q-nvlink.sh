#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
# render-track-q-nvlink.sh [OUT_DIR]
#
# Regenerates the committed NVLink renders of the four prefill/decode cells (design reference, not run on p6-b200):
#   OUT_DIR/S<n>.yaml = `kubectl kustomize manifests/track-q-qwen38/variants-nvlink/S<n>`, for S3, S5, S6, S7.
# Default OUT_DIR: manifests/track-q-qwen38/rendered-nvlink. The published EFA renders (manifests/track-q-qwen38/rendered,
# written by render-track-q.sh) are never touched. Offline: it only renders and never talks to a cluster. Every overlay
# is rendered first; OUT_DIR is written only once all four renders succeeded.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
VARIANTS_DIR="$REPO_ROOT/manifests/track-q-qwen38/variants-nvlink"
CELLS="S3 S5 S6 S7"

if [ $# -gt 1 ]; then
  echo "usage: $(basename "$0") [OUT_DIR]" >&2
  exit 2
fi
out_dir="${1:-$REPO_ROOT/manifests/track-q-qwen38/rendered-nvlink}"

work="$(mktemp -d "${TMPDIR:-/tmp}/render-track-q-nvlink.XXXXXX")"
trap 'rm -rf "$work"' EXIT
for cell in $CELLS; do
  kubectl kustomize "$VARIANTS_DIR/$cell" </dev/null >"$work/$cell.yaml"
done
mkdir -p "$out_dir"
for cell in $CELLS; do
  mv "$work/$cell.yaml" "$out_dir/$cell.yaml"
done
printf '%s %s cells=%s out_dir=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$(basename "$0")" "${CELLS// /,}" "$out_dir" >&2
