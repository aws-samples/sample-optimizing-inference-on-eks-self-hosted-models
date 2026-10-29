#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
# regen.sh [--dry-run] <rate> <outdir>
#
# Regenerates the W3-16K window trace of one rate (10, 25, 32 or 50 req/s) offline, with the
# existing generator `demo-bench trace w2` (unchanged), then cuts it the way the runs replayed it:
#   1. in a scratch directory under <outdir>, run the generator with the W3 shape (params.json "shape"),
#      --sessions = 75 x rate and --rate = rate; it writes the full trace and its sidecar;
#   2. keep the first K rows with timestamp < 615000 ms (rows are sorted: head -n K of the trace and of the
#      sidecar, original bytes and `line` values kept);
#   3. write <outdir>/w3-16k-r<rate>.jsonl, <outdir>/w3-16k-r<rate>.side.jsonl and <outdir>/SHA256SUMS
#      (sha256sum format, trace first). Compare it with the hashes recorded in params.json.
# Outputs reach <outdir> only once every step succeeded; the scratch directory is always removed.
#
# Generator: $DEMO_BENCH (word-split) when set, else `uv run --no-dev --project <repo>/tools/demo_bench demo-bench`
# (--no-dev: the trace needs only the standard library, not the dev group of tools/demo_bench).
# --dry-run prints the generator command on one line (paths relative to the scratch directory) and writes
# nothing. Exit codes: 0 ok, 1 generation failed, 2 usage error or a rate outside the list.
# Portable to macOS bash 3.2 and Linux bash 5.
set -euo pipefail
export LC_ALL=C

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$HERE/../.." && pwd)"
SCRIPT_NAME="$(basename "$0")"
RATES="10 25 32 50"
SESSIONS_PER_RATE=75 # N_s = 75 x rate: the mean inter-turn gap N_s / rate is 75 s at every rate
CUT_MS=615000
# The W3 shape, flag for flag; params.json "shape" holds the same values (tests/scripts/test_108 checks both).
SHAPE_FLAGS=(
  --seed 1111 --turns 11 --global-prefix-tokens 2048 --entry-tokens 5632 --growth-tokens 1024
  --gap-sigma 0.3 --populate-s 180 --cold-fraction 0.03 --cold-min-tokens 15872 --cold-max-tokens 15872
  --osl 500 --max-isl 16384 --block-size 512
)

# log <message...>: one structured line on stderr.
log() {
  printf '%s %s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$SCRIPT_NAME" "$*" >&2
}

# usage_error [message]: print the usage on stderr and exit 2.
usage_error() {
  if [ $# -gt 0 ]; then
    log "error: $*"
  fi
  echo "usage: $SCRIPT_NAME [--dry-run] <rate: ${RATES// /|}> <outdir>" >&2
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

dry_run=0
positional=()
for arg in "$@"; do
  case "$arg" in
    --dry-run) dry_run=1 ;;
    --*) usage_error "unknown option '$arg'" ;;
    *) positional+=("$arg") ;;
  esac
done
[ "${#positional[@]}" -eq 2 ] || usage_error
rate="${positional[0]}"
outdir="${positional[1]}"
case "$rate" in
  10 | 25 | 32 | 50) ;; # = RATES
  *) usage_error "rate must be one of $RATES (got '$rate')" ;;
esac

name="w3-16k-r$rate"
full="r$rate-full.jsonl"
full_side="r$rate-full.side.jsonl"
if [ -n "${DEMO_BENCH:-}" ]; then
  read -r -a gen <<<"$DEMO_BENCH"
else
  gen=(uv run --no-dev --project "$REPO_ROOT/tools/demo_bench" demo-bench)
fi
cmd=("${gen[@]}" trace w2 --out "$full" --sidecar "$full_side"
  --sessions "$((SESSIONS_PER_RATE * rate))" --rate "$rate" "${SHAPE_FLAGS[@]}")

if [ "$dry_run" -eq 1 ]; then
  line="$(printf '%q ' "${cmd[@]}")"
  printf '%s\n' "${line% }"
  log "dry run: the command runs in a scratch directory under $outdir, then the trace is cut at timestamp < $CUT_MS ms"
  exit 0
fi

mkdir -p "$outdir"
stage="$(mktemp -d "$outdir/.regen.XXXXXX")"
trap 'rm -rf "$stage"' EXIT

log "generating rate=$rate sessions=$((SESSIONS_PER_RATE * rate)) into $stage"
(cd "$stage" && "${cmd[@]}") >&2 || {
  rc=$?
  log "error: generator failed (exit $rc); nothing written to $outdir"
  exit 1
}

# K = the leading rows with timestamp < CUT_MS (the generator sorts the rows by timestamp).
k="$(awk -v cut="$CUT_MS" '
  {
    if (!match($0, /"timestamp": *[0-9]+/)) { bad = 1; exit }
    ts = substr($0, RSTART, RLENGTH)
    sub(/^"timestamp": */, "", ts)
    if (ts + 0 >= cut) exit
    n++
  }
  END { if (bad) exit 1; print n + 0 }
' "$stage/$full")" || {
  log "error: a trace row without a timestamp in $stage/$full"
  exit 1
}
head -n "$k" "$stage/$full" >"$stage/$name.jsonl"
head -n "$k" "$stage/$full_side" >"$stage/$name.side.jsonl"
(cd "$stage" && sha256_sums "$name.jsonl" "$name.side.jsonl") >"$stage/SHA256SUMS"
mv -f "$stage/$name.jsonl" "$stage/$name.side.jsonl" "$stage/SHA256SUMS" "$outdir/"
log "wrote rate=$rate rows=$k cut_ms=$CUT_MS outdir=$outdir sums=$(tr '\n' ' ' <"$outdir/SHA256SUMS")"
