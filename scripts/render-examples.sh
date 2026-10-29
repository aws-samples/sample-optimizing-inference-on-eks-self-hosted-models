#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
# render-examples.sh [OUT_ROOT]
#
# Regenerates the committed per-technique views under examples/ (one folder per Track Q cell):
#   OUT_ROOT/<dir>/rendered.yaml         = `kubectl kustomize examples/<dir>` (the folder only points at its cell)
#   OUT_ROOT/<dir>/diff-vs-baseline.diff = per-component diff of 00-baseline/rendered.yaml vs <dir>/rendered.yaml
#                                          (every folder except 00-baseline; layout below)
# Default OUT_ROOT: examples (the files tests/manifests/test_420 drift-checks). The kustomizations, READMEs and
# the Track Q tree are inputs only. Offline: it only renders and never talks to a cluster. Every folder is
# rendered and diffed first; OUT_ROOT is written only once all of them succeeded.
#
# Diff layout. Each render is cut into parts: `metadata` (everything before `  components:`) and one part per
# `  - name: X` component. The diff holds one section per changed part: metadata first, then the variant's
# components in render order (identical parts get no section). Frontend is compared with the baseline Frontend,
# every other component (Worker, prefill, decode) with the baseline Worker. Section headers:
#   --- a/00-baseline/rendered.yaml#<baseline part>
#   +++ b/<dir>/rendered.yaml#<variant part>
# * One worker component (01-kv-router, 02-hicache, 05-kv-router-hicache): each section is the plain `diff -u`
#   of the two parts.
# * Several worker components (03-pd-disaggregation, 04-all-three, 06-kv-router-pd, 07-pd-hicache: prefill and
#   decode): `diff -U1`, and
#   - the GPU-pin preamble of the launch script (components/pd-efa) becomes
#     `# ... <N> added lines omitted (GPU-pin preamble)`: the first worker section (prefill) still shows its
#     leading comment and its tail from the `export` line, a later one (decode) collapses all of it;
#   - a later worker section (decode) replaces the changed lines the first one (prefill) already shows by
#     `# ... <N> changed lines identical to prefill omitted`; a hunk left without a changed line is dropped
#     and only its notes remain.
#   Sections without a note stay complete diffs; rendered.yaml next to the diff has the full YAML.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
EXAMPLES_DIR="$REPO_ROOT/examples"
BASELINE="00-baseline"
FOLDERS="00-baseline 01-kv-router 02-hicache 03-pd-disaggregation 04-all-three 05-kv-router-hicache 06-kv-router-pd 07-pd-hicache"
PREAMBLE_MIN=20 # shortest code run of the GPU-pin preamble worth collapsing

if [ $# -gt 1 ]; then
  echo "usage: $(basename "$0") [OUT_ROOT]" >&2
  exit 2
fi
out_root="${1:-$EXAMPLES_DIR}"

# split_parts RENDERED OUT_DIR: writes OUT_DIR/metadata and OUT_DIR/<component>, prints the component names in
# render order. A component block ends at the first non-blank line indented by fewer than three spaces.
split_parts() {
  awk -v out="$2" '
    BEGIN { part = "metadata" }
    stop { next }
    part == "metadata" {
      if ($0 == "  components:") { part = ""; next }
      print > (out "/metadata")
      next
    }
    /^  - name: / {
      part = substr($0, 11)
      if (part !~ /^[A-Za-z0-9_-]+$/ || part == "metadata") { print "bad component name: " part > "/dev/stderr"; exit 1 }
      print part
      print > (out "/" part)
      next
    }
    part == "" || ($0 ~ /[^ \t]/ && $0 !~ /^   /) { stop = 1; next }
    { print > (out "/" part) }
  ' "$1"
}

# compact_section REF REF_FILE SHOWN_FILE: filters one `diff -U1` body (stdin, no file headers) of a worker part.
#   REF        earlier worker section whose shown changed lines are omitted here ("" for the first one)
#   REF_FILE   the changed lines REF shows, one per line ("" for the first one)
#   SHOWN_FILE where this section writes the changed lines it shows ("" to skip)
compact_section() {
  awk -v ref="$1" -v reffile="$2" -v shownfile="$3" -v minrun="$PREAMBLE_MIN" '
    function flush_note() {
      if (pending > 0) print "# ... " pending " changed lines identical to " ref " omitted"
      pending = 0
    }
    function flush_hunk(   i, c, e, k, hb, kept) {
      if (n == 0) return
      for (i = 1; i <= n; i++) kind[i] = (substr(line[i], 1, 1) == " ") ? "ctx" : "chg"
      # GPU-pin preamble: an added run opening with "# GPU pin:". The first worker section collapses the code
      # between its comment and its export; a later one, which REF already shows, collapses the whole run.
      for (i = 1; i <= n && !collapsed; i++) {
        if (line[i] !~ /^\+[ \t]*# GPU pin:/ || (i > 1 && substr(line[i - 1], 1, 1) == "+")) continue
        c = i
        if (ref == "") while (c <= n && line[c] ~ /^\+[ \t]*#/) c++
        e = c
        while (e <= n && substr(line[e], 1, 1) == "+" && (ref != "" || line[e] !~ /^\+[ \t]*export /)) e++
        if (e - c >= minrun) {
          for (k = c; k < e; k++) kind[k] = "blk"
          hb = e - c
          collapsed = 1
        }
      }
      kept = 0
      for (i = 1; i <= n; i++) {
        if (kind[i] == "chg") {
          if ((line[i] in avail) && avail[line[i]] > 0) { avail[line[i]]--; kind[i] = "omit" } else kind[i] = "show"
        }
        if (kind[i] == "show") kept = 1
      }
      if (!kept) {
        for (i = 1; i <= n; i++) if (kind[i] == "omit") pending++
        if (hb) { flush_note(); print "# ... " hb " added lines omitted (GPU-pin preamble)" }
        n = 0
        return
      }
      flush_note()
      print hdr
      for (i = 1; i <= n; i++) {
        if (kind[i] == "omit") { pending++; continue }
        flush_note()
        if (kind[i] == "blk") {
          if (kind[i - 1] != "blk") print "# ... " hb " added lines omitted (GPU-pin preamble)"
          continue
        }
        print line[i]
        if (kind[i] == "show" && shownfile != "") print line[i] > shownfile
      }
      n = 0
    }
    BEGIN { if (reffile != "") while ((getline l < reffile) > 0) avail[l]++ }
    /^@@/ { flush_hunk(); delete kind; hdr = $0; next }
    { line[++n] = $0 }
    END { flush_hunk(); flush_note() }
  '
}

work="$(mktemp -d "${TMPDIR:-/tmp}/render-examples.XXXXXX")"
trap 'rm -rf "$work"' EXIT
for dir in $FOLDERS; do
  mkdir -p "$work/$dir" "$work/parts/$dir"
  kubectl kustomize "$EXAMPLES_DIR/$dir" </dev/null >"$work/$dir/rendered.yaml"
done
split_parts "$work/$BASELINE/rendered.yaml" "$work/parts/$BASELINE" >/dev/null
for dir in $FOLDERS; do
  [ "$dir" = "$BASELINE" ] && continue
  comps="$(split_parts "$work/$dir/rendered.yaml" "$work/parts/$dir")"
  [ -n "$comps" ] || { echo "$dir: no components in rendered.yaml" >&2; exit 1; }
  workers="$(printf '%s\n' "$comps" | grep -cvx Frontend || true)"
  context=3
  [ "$workers" -gt 1 ] && context=1
  first_worker=""
  out="$work/$dir/diff-vs-baseline.diff"
  : >"$out"
  for part in metadata $comps; do
    case "$part" in
      metadata | Frontend) base_part="$part" ;;
      *) base_part="Worker" ;;
    esac
    [ -f "$work/parts/$BASELINE/$base_part" ] || { echo "$BASELINE has no $base_part part" >&2; exit 1; }
    # diff exits 1 when the parts differ (the expected case); 0 means identical (no section); above 1 is an error.
    rc=0
    diff -U"$context" "$work/parts/$BASELINE/$base_part" "$work/parts/$dir/$part" >"$work/section" || rc=$?
    if [ "$rc" -gt 1 ]; then
      echo "diff failed for $dir#$part (exit $rc)" >&2
      exit 1
    fi
    [ "$rc" -eq 0 ] && continue
    printf -- '--- a/%s/rendered.yaml#%s\n+++ b/%s/rendered.yaml#%s\n' "$BASELINE" "$base_part" "$dir" "$part" >>"$out"
    if [ "$context" -eq 3 ] || [ "$base_part" != "Worker" ]; then
      tail -n +3 "$work/section" >>"$out"
    elif [ -z "$first_worker" ]; then
      first_worker="$part"
      tail -n +3 "$work/section" | compact_section "" "" "$work/parts/$dir.shown" >>"$out"
    else
      tail -n +3 "$work/section" | compact_section "$first_worker" "$work/parts/$dir.shown" "" >>"$out"
    fi
  done
done
for dir in $FOLDERS; do
  mkdir -p "$out_root/$dir"
  mv "$work/$dir/rendered.yaml" "$out_root/$dir/rendered.yaml"
  if [ "$dir" != "$BASELINE" ]; then
    mv "$work/$dir/diff-vs-baseline.diff" "$out_root/$dir/diff-vs-baseline.diff"
  fi
done
printf '%s %s folders=%s out_root=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$(basename "$0")" "${FOLDERS// /,}" "$out_root" >&2
