#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
# upload-inputs.sh [--dry-run|--yes] <rate> <trace-dir> <model-dir-or-tokenizer-dir> [results-bucket]
#
# Uploads the inputs a W3-16K plan reads to the exact S3 locations of its header,
# plans/w3-16k/plan-w3-r<rate>.txt (its -cells plan reads the same ones):
#   1. from <trace-dir>, the three files regen.sh writes: w3-16k-r<rate>.jsonl to `w2 trace=`,
#      w3-16k-r<rate>.side.jsonl to `sidecar=` and their SHA256SUMS next to the trace;
#   2. from <model-dir-or-tokenizer-dir> (a model snapshot of the revision, or a directory with only its tokenizer),
#      the tokenizer files (those of TOKENIZER_FILES that are present; never the weights), then a SHA256SUMS of
#      them, under `tokenizer prefix=`.
# Before any upload the trace and the sidecar must hash to trace_sha256 / sidecar_sha256 of the plan and the
# SHA256SUMS of <trace-dir> must list exactly those two lines; otherwise nothing is uploaded.
# The plan pins sums_sha256, the sha256 of the tokenizer SHA256SUMS: stdout carries `sums_sha256=<sha256>` of the
# one written here. When it differs (another file set), put that value in both plans of the rate before rendering.
#
# Bucket: [results-bucket], else $RESULTS_BUCKET, else RESULTS_BUCKET of the generated platform.env ($PLATFORM_ENV,
# default manifests/config/platform.env); it replaces ${RESULTS_BUCKET} of the plan. --region is the AWS_REGION of
# that platform.env when it names one (the caller's ambient AWS_REGION is not used), else the AWS CLI default.
# Default and --dry-run: print the `aws s3 cp` commands and upload nothing. --yes: print and run them, in order.
# Exit codes: 0 ok, 1 an input failed its check or an upload failed, 2 usage error.
# Portable to macOS bash 3.2 and Linux bash 5.
set -euo pipefail
export LC_ALL=C

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$HERE/../.." && pwd)"
SCRIPT_NAME="$(basename "$0")"
PLANS_DIR="$REPO_ROOT/plans/w3-16k"
PLATFORM_ENV="${PLATFORM_ENV:-$REPO_ROOT/manifests/config/platform.env}"
RATES="10 25 32" # the rates with a plan in plans/w3-16k
# What a tokenizer loader reads (config.json names the tokenizer class when tokenizer_config.json does not).
# Sorted: the tokenizer SHA256SUMS lists them in this order.
TOKENIZER_FILES="added_tokens.json chat_template.jinja config.json merges.txt special_tokens_map.json tokenizer.json
tokenizer.model tokenizer_config.json vocab.json"
# shellcheck disable=SC2016 # the literal placeholder written in the plans, not an expansion
PLACEHOLDER='${RESULTS_BUCKET}'

# log <message...>: one structured line on stderr.
log() {
  printf '%s %s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$SCRIPT_NAME" "$*" >&2
}

# die <message...>: log and exit 1.
die() {
  log "error: $*"
  exit 1
}

# usage_error [message]: print the usage on stderr and exit 2.
usage_error() {
  if [ $# -gt 0 ]; then
    log "error: $*"
  fi
  echo "usage: $SCRIPT_NAME [--dry-run|--yes] <rate: ${RATES// /|}> <trace-dir> <model-dir-or-tokenizer-dir> [results-bucket]" >&2
  exit 2
}

# sha256_sums <file...>: `<sha256>  <name>` lines (sha256sum on Linux, shasum on macOS).
sha256_sums() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$@"
  else
    shasum -a 256 "$@"
  fi
}

# sha256_of <file>: the sha256 hex digest of one file.
sha256_of() {
  sha256_sums "$1" | awk '{ print $1 }'
}

# plan_field <directive> <key>: the value of <key>= on the single <directive> line of $plan (`#` starts a comment).
plan_field() {
  awk -v d="$1" -v k="$2" '
    { sub(/#.*/, "") }
    $1 == d { n++; for (i = 2; i <= NF; i++) if (index($i, k "=") == 1) v = substr($i, length(k) + 2) }
    END { if (n != 1 || v == "") exit 1; print v }
  ' "$plan" || die "$plan: no single '$1' line with $2="
}

# to_uri <plan value>: the plan value with ${RESULTS_BUCKET} replaced by $bucket (the value must carry it).
to_uri() {
  case "$1" in
    *"$PLACEHOLDER"*) printf '%s\n' "${1//"$PLACEHOLDER"/$bucket}" ;;
    *) die "$plan: '$1' does not carry $PLACEHOLDER" ;;
  esac
}

# upload <src> <dst>: print the aws command; with --yes, run it (the first failure stops the script).
upload() {
  local cmd line
  if [ -n "$region" ]; then
    cmd=(aws s3 cp "$1" "$2" --only-show-errors --region "$region")
  else
    cmd=(aws s3 cp "$1" "$2" --only-show-errors)
  fi
  line="$(printf '%q ' "${cmd[@]}")"
  printf '%s\n' "${line% }"
  if [ "$mode" = yes ]; then
    "${cmd[@]}" || die "upload failed (exit $?): $1 -> $2; nothing after it was uploaded"
  fi
}

mode=dry-run
mode_flag=""
positional=()
for arg in "$@"; do
  case "$arg" in
    --dry-run | --yes)
      [ -z "$mode_flag" ] || usage_error "--dry-run and --yes are exclusive"
      mode_flag="$arg"
      ;;
    --*) usage_error "unknown option '$arg'" ;;
    *) positional+=("$arg") ;;
  esac
done
if [ "$mode_flag" = --yes ]; then
  mode=yes
fi
case "${#positional[@]}" in
  3 | 4) ;;
  *) usage_error ;;
esac
rate="${positional[0]}"
trace_dir="${positional[1]}"
tok_dir="${positional[2]}"
bucket_arg=""
if [ "${#positional[@]}" -eq 4 ]; then
  bucket_arg="${positional[3]}"
fi
case "$rate" in
  10 | 25 | 32) ;; # = RATES
  *) usage_error "rate must be one of $RATES (the rates with a plan in plans/w3-16k), got '$rate'" ;;
esac

# Bucket and region: never a literal of this script.
cfg_bucket=""
region=""
if [ -f "$PLATFORM_ENV" ]; then
  while IFS='=' read -r key value; do
    case "$key" in
      RESULTS_BUCKET) cfg_bucket="$value" ;;
      AWS_REGION) region="$value" ;;
    esac
  done <"$PLATFORM_ENV"
fi
bucket="${bucket_arg:-${RESULTS_BUCKET:-$cfg_bucket}}"
[ -n "$bucket" ] ||
  usage_error "no results bucket: pass it as the last argument, export RESULTS_BUCKET or generate $PLATFORM_ENV (scripts/gen_env.py)"
case "$bucket" in
  *[!a-z0-9.-]* | [.-]* | *[.-]) usage_error "invalid S3 bucket name '$bucket'" ;;
esac
if [ "${#bucket}" -lt 3 ] || [ "${#bucket}" -gt 63 ]; then
  usage_error "invalid S3 bucket name '$bucket' (3 to 63 characters)"
fi
case "$region" in
  *[!a-z0-9-]*) die "invalid AWS_REGION '$region' in $PLATFORM_ENV" ;;
esac

# Destinations and pins: the plan header, verbatim.
plan="$PLANS_DIR/plan-w3-r$rate.txt"
[ -f "$plan" ] || die "missing $plan"
raw="$(plan_field w2 trace)"
trace_uri="$(to_uri "$raw")"
raw="$(plan_field w2 sidecar)"
side_uri="$(to_uri "$raw")"
raw="$(plan_field tokenizer prefix)"
tok_prefix="$(to_uri "$raw")"
tok_prefix="${tok_prefix%/}/"
trace_sha="$(plan_field w2 trace_sha256)"
side_sha="$(plan_field w2 sidecar_sha256)"
sums_pin="$(plan_field tokenizer sums_sha256)"

# Verify every input before the first upload.
name="w3-16k-r$rate"
for f in "$name.jsonl" "$name.side.jsonl" SHA256SUMS; do
  [ -f "$trace_dir/$f" ] || die "missing $trace_dir/$f (workloads/w3-16k/regen.sh $rate <dir> writes it); nothing uploaded"
done
got="$(sha256_of "$trace_dir/$name.jsonl")"
[ "$got" = "$trace_sha" ] || die "$trace_dir/$name.jsonl: sha256 $got differs from trace_sha256=$trace_sha of $plan; nothing uploaded"
got="$(sha256_of "$trace_dir/$name.side.jsonl")"
[ "$got" = "$side_sha" ] || die "$trace_dir/$name.side.jsonl: sha256 $got differs from sidecar_sha256=$side_sha of $plan; nothing uploaded"
want="$(printf '%s  %s\n%s  %s' "$trace_sha" "$name.jsonl" "$side_sha" "$name.side.jsonl")"
[ "$(cat "$trace_dir/SHA256SUMS")" = "$want" ] ||
  die "$trace_dir/SHA256SUMS does not list exactly the trace_sha256 and sidecar_sha256 of $plan; nothing uploaded"
[ -f "$tok_dir/tokenizer_config.json" ] ||
  die "no tokenizer_config.json in '$tok_dir': pass the model snapshot (or tokenizer directory) of the revision; nothing uploaded"
tok_files=()
for f in $TOKENIZER_FILES; do
  if [ -f "$tok_dir/$f" ]; then
    tok_files+=("$f")
  fi
done

tmp_root="${TMPDIR:-/tmp}"
work="$(mktemp -d "${tmp_root%/}/upload-inputs.XXXXXX")"
trap 'rm -rf "$work"' EXIT
(cd "$tok_dir" && sha256_sums "${tok_files[@]}") >"$work/SHA256SUMS"
sums_sha="$(sha256_of "$work/SHA256SUMS")"
printf 'sums_sha256=%s\n' "$sums_sha"
if [ "$sums_sha" = "$sums_pin" ]; then
  log "the tokenizer SHA256SUMS (${#tok_files[@]} files) matches sums_sha256 of $plan"
else
  log "warning: the tokenizer SHA256SUMS (${#tok_files[@]} files, sha256 $sums_sha) differs from sums_sha256=$sums_pin of $plan:" \
    "put sums_sha256=$sums_sha in plans/w3-16k/plan-w3-r$rate.txt and plans/w3-16k/plan-w3-r$rate-cells.txt before rendering," \
    "or the driver refuses the tokenizer"
fi

# Trace files first, each SHA256SUMS after the files it lists.
upload "$trace_dir/$name.jsonl" "$trace_uri"
upload "$trace_dir/$name.side.jsonl" "$side_uri"
upload "$trace_dir/SHA256SUMS" "${trace_uri%/*}/SHA256SUMS"
for f in "${tok_files[@]}"; do
  upload "$tok_dir/$f" "$tok_prefix$f"
done
upload "$work/SHA256SUMS" "${tok_prefix}SHA256SUMS"
if [ "$mode" = yes ]; then
  log "uploaded $((3 + ${#tok_files[@]} + 1)) files for rate=$rate to $bucket"
else
  log "dry run: nothing uploaded (the tokenizer SHA256SUMS above is a temporary file); re-run with --yes to upload" \
    "these $((3 + ${#tok_files[@]} + 1)) files"
fi
