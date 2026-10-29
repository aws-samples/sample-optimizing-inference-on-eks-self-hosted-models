#!/usr/bin/env bash
# shellcheck source-path=SCRIPTDIR
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
# preflight-flags.sh --help-file [LABEL=]FILE [--help-file LABEL=FILE]... <rendered.yaml | kustomize-dir>...
#
# Spec 9.1: every flag a manifest passes must exist in the `--help` of its entrypoint in the
# pinned image. Capture one help per entrypoint once, CPU only, e.g.
#   docker run --rm <image> python3 -m dynamo.sglang --help   > sglang.help.txt
#   docker run --rm <image> python3 -m dynamo.frontend --help > frontend.help.txt
#   docker run --rm <image> python3 -m dynamo.mocker --help   > mocker.help.txt
# LABEL is sglang | frontend | mocker. Each container (command/args, including `bash -lc`
# bodies; a body with several processes is split per entrypoint module) is checked against the
# capture of ITS entrypoint only: exact match, --flag=value by name. SGLang also gets the keys of
# the ConfigMap file passed with --config, and --engine-route descriptors must be
# <path>[=<method>][:engine|tm]. Containers with no known entrypoint are skipped.
# Labelled mode: an entrypoint without a capture is an error. Back-compat: a single unlabelled
# --help-file FILE means sglang only (other entrypoints ignored).
# Exit 0 when everything is known, 1 on a problem (each one printed), 2 on usage.
# Offline (no cluster). Needs python3 with PyYAML; falls back to `uv run --with pyyaml==6.0.3`
# (the PyYAML of tests/requirements-test.txt).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/common.sh
source "$SCRIPT_DIR/lib/common.sh"

usage() {
  echo "usage: $SCRIPT_NAME --help-file [sglang|frontend|mocker=]FILE [--help-file LABEL=FILE]... <rendered.yaml | kustomize-dir>..." >&2
  exit 2
}

original_args=("$@")
helps=()
labels=" "
mode="unlabelled"
while [ $# -gt 0 ]; do
  case "$1" in
    --help-file)
      [ $# -ge 2 ] || usage
      spec="$2"
      shift 2
      label=""
      path="$spec"
      case "$spec" in
        *=*)
          prefix="${spec%%=*}"
          if printf '%s' "$prefix" | grep -Eq '^[a-z]+$'; then
            label="$prefix"
            path="${spec#*=}"
          fi
          ;;
      esac
      if [ -n "$label" ]; then
        case "$label" in
          sglang | frontend | mocker) mode="labelled" ;;
          *)
            log "error: unknown --help-file label '$label' (sglang, frontend, mocker)"
            exit 2
            ;;
        esac
      else
        label="sglang"
      fi
      case "$labels" in
        *" $label "*)
          log "error: more than one --help-file for $label"
          exit 2
          ;;
      esac
      labels="$labels$label "
      if [ ! -f "$path" ] || [ ! -s "$path" ]; then
        log "error: help file '$path' ($label) is missing or empty"
        exit 2
      fi
      helps+=("$label=$path")
      ;;
    --*) usage ;;
    *) break ;;
  esac
done
if [ "${#helps[@]}" -eq 0 ] || [ $# -lt 1 ]; then usage; fi

if ! python3 -c 'import yaml' >/dev/null 2>&1; then
  if [ -z "${PREFLIGHT_REEXEC:-}" ] && command -v uv >/dev/null 2>&1; then
    PREFLIGHT_REEXEC=1 exec uv run --quiet --with pyyaml==6.0.3 -- bash "$0" "${original_args[@]}"
  fi
  log "error: python3 with PyYAML is required (try: uv run --with pyyaml==6.0.3 $0 ...)"
  exit 2
fi

work="$(mktemp -d "${TMPDIR:-/tmp}/preflight-flags.XXXXXX")"
trap 'rm -rf "$work"' EXIT
inputs=()
n=0
for src in "$@"; do
  n=$((n + 1))
  if [ -d "$src" ]; then
    if ! kubectl kustomize "$src" </dev/null >"$work/$n.yaml"; then
      log "error: kubectl kustomize $src failed"
      exit 2
    fi
    inputs+=("$src=$work/$n.yaml")
  elif [ -f "$src" ]; then
    inputs+=("$src=$src")
  else
    log "error: no such file or directory: $src"
    exit 2
  fi
done

python3 - "$mode" "${helps[@]}" -- "${inputs[@]}" <<'PY'
"""Compare the flags used by rendered manifests with one --help capture per entrypoint."""

import re
import shlex
import sys

import yaml

FLAG_RE = re.compile(r"(?<![\w-])--[A-Za-z0-9][A-Za-z0-9_-]*")
ROUTE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*(/[A-Za-z0-9][A-Za-z0-9._-]*)*(=[A-Za-z_][A-Za-z0-9_]*)?(:(engine|tm))?$")
MODULES = {"dynamo.sglang": "sglang", "dynamo.frontend": "frontend", "dynamo.mocker": "mocker"}


def tokens(container):
    out = []
    for part in list(container.get("command") or []) + list(container.get("args") or []):
        text = str(part)
        if any(ch.isspace() for ch in text):
            try:
                out.extend(shlex.split(text.replace("\\\n", " ")))
            except ValueError:
                out.extend(text.split())
        else:
            out.append(text)
    return out


def segments(toks):
    """Split tokens at every entrypoint module: [(label, tokens after the module)]."""
    segs, cur = [], None
    for tok in toks:
        label = MODULES.get(tok) or next((v for k, v in MODULES.items() if tok.endswith("/" + k)), None)
        if label:
            cur = (label, [])
            segs.append(cur)
        elif cur is not None:
            cur[1].append(tok)
    return segs


def _keys(text):
    loaded = yaml.safe_load(text) or {}
    return [str(k).replace("_", "-") for k in loaded]


def config_keys(cfg_path, container, volumes, configmaps):
    for mount in container.get("volumeMounts") or []:
        mp = str(mount.get("mountPath", "")).rstrip("/")
        if not cfg_path.startswith(mp + "/"):
            continue
        vol = volumes.get(mount.get("name"))
        if not vol or "configMap" not in vol:
            continue
        data = (configmaps.get(vol["configMap"].get("name")) or {}).get("data") or {}
        fname = cfg_path[len(mp) + 1:]
        if fname in data:
            return _keys(data[fname])
    # Not resolvable through a mount (raw rendered file): ConfigMaps carrying that file name.
    base = cfg_path.rsplit("/", 1)[-1]
    found = [_keys(cm["data"][base]) for cm in configmaps.values() if base in (cm.get("data") or {})]
    return [k for keys in found for k in keys] if found else None


def main():
    mode = sys.argv[1]
    sep = sys.argv.index("--")
    known = {}
    for spec in sys.argv[2:sep]:
        label, _, path = spec.partition("=")
        known[label] = set(FLAG_RE.findall(open(path, encoding="utf-8", errors="replace").read()))
    problems, checked = [], 0
    for spec in sys.argv[sep + 1:]:
        src, _, path = spec.partition("=")
        docs = [d for d in yaml.safe_load_all(open(path, encoding="utf-8")) if isinstance(d, dict)]
        configmaps = {d["metadata"]["name"]: d for d in docs if d.get("kind") == "ConfigMap"}
        for doc in docs:
            if doc.get("kind") != "DynamoGraphDeployment":
                continue
            for comp in doc.get("spec", {}).get("components") or []:
                pod = (comp.get("podTemplate") or {}).get("spec") or {}
                volumes = {v.get("name"): v for v in pod.get("volumes") or []}
                for container in pod.get("containers") or []:
                    where = f"{src}:{doc['metadata'].get('name')}/{comp.get('name')}/{container.get('name')}"
                    for label, toks in segments(tokens(container)):
                        if mode == "unlabelled" and label != "sglang":
                            continue
                        if label not in known:
                            problems.append(f"{where}: no --help-file capture for entrypoint {label} (dynamo.{label})")
                            continue
                        used = []
                        for i, tok in enumerate(toks):
                            if not tok.startswith("--") or len(tok) <= 2:
                                continue
                            name, eq, value = tok.partition("=")
                            used.append(name)
                            if label != "sglang":
                                continue
                            if name == "--engine-route":
                                route = value if eq else (toks[i + 1] if i + 1 < len(toks) else "")
                                if not ROUTE_RE.match(route):
                                    problems.append(f"{where} [{label}]: invalid --engine-route descriptor {route!r}")
                            if name == "--config" and not eq and i + 1 < len(toks):
                                keys = config_keys(toks[i + 1], container, volumes, configmaps)
                                if keys is None:
                                    print(f"warning: {where}: --config {toks[i + 1]} is not a mounted ConfigMap; keys not checked", file=sys.stderr)
                                else:
                                    used.extend(f"--{k}" for k in keys)
                        for flag in used:
                            checked += 1
                            if flag not in known[label]:
                                problems.append(f"{where} [{label}]: unknown flag {flag}")
    for line in sorted(set(problems)):
        print(line)
    print(f"preflight-flags: mode={mode} entrypoints={','.join(sorted(known))} {checked} flag uses checked, "
          f"{len(set(problems))} problem(s)", file=sys.stderr)
    return 1 if problems else 0


sys.exit(main())
PY
