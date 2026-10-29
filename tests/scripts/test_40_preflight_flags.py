# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""`scripts/preflight-flags.sh --help-file FILE <rendered.yaml|kustomize-dir>...` (spec 9.1)."""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest
import yaml

import lib_scripts as S

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "manifests"))
import lib_manifests as L

HELP = """\
usage: python3 -m dynamo.sglang [-h] [--config CONFIG] [--model-path MODEL_PATH]
  --config CONFIG          Path to a YAML config file
  --model-path MODEL_PATH  Model path
  --tensor-parallel-size TP
                           Tensor parallel size
  --trust-remote-code      Trust remote code
  --engine-route ROUTE [ROUTE ...]
                           Expose an engine route, e.g. flush_cache:tm
  --dyn-tool-call-parser P Tool call parser
  --hicache-size N         HiCache size in GB
"""

DGD = """\
apiVersion: nvidia.com/v1beta1
kind: DynamoGraphDeployment
metadata: {{name: q38-flags, namespace: s-agg}}
spec:
  backendFramework: sglang
  components:
    - name: agg
      type: worker
      podTemplate:
        spec:
          containers:
            - name: main
              image: x
              command: [python3, -m, dynamo.sglang]
              args: [{args}]
"""


def _write(tmp: Path, args: str, extra_docs: str = "") -> tuple[Path, Path]:
    manifest = tmp / "rendered.yaml"
    manifest.write_text(DGD.format(args=args) + extra_docs)
    helpf = tmp / "help.txt"
    helpf.write_text(HELP)
    return manifest, helpf


def _run(tmp: Path, manifest: Path, helpf: Path) -> S.Result:
    return S.Sandbox(tmp / "sb", {}).run("preflight-flags.sh", "--help-file", str(helpf), str(manifest))


def test_passes_when_every_flag_is_known(tmp_path: Path) -> None:
    """spec 9.1: all rendered `--flags` exist in the provided --help output -> exit 0."""
    m, h = _write(tmp_path, "--config, /etc/sglang/agg.yaml, --trust-remote-code, --engine-route, 'flush_cache:tm'")
    res = _run(tmp_path, m, h)
    assert res.rc == 0, res.stdout + res.stderr


def test_fails_on_unknown_flag_and_names_it(tmp_path: Path) -> None:
    """spec 9.1: an unknown flag fails the preflight and is printed so the developer can fix it."""
    m, h = _write(tmp_path, "--config, /etc/sglang/agg.yaml, --bogus-flag, 1")
    res = _run(tmp_path, m, h)
    assert res.rc != 0
    assert "--bogus-flag" in res.stdout + res.stderr


def test_flag_must_match_exactly_not_as_prefix(tmp_path: Path) -> None:
    """spec 9.1: `--tensor-parallel` is NOT accepted just because `--tensor-parallel-size` exists."""
    m, h = _write(tmp_path, "--tensor-parallel, '4'")
    res = _run(tmp_path, m, h)
    assert res.rc != 0
    assert "--tensor-parallel" in res.stdout + res.stderr


def test_extracts_flag_equals_value_form(tmp_path: Path) -> None:
    """spec 9.1: `--flag=value` tokens are checked by flag name."""
    m, h = _write(tmp_path, "'--bogus-eq=3'")
    res = _run(tmp_path, m, h)
    assert res.rc != 0 and "--bogus-eq" in res.stdout + res.stderr


def test_extracts_flags_from_bash_lc_script_body(tmp_path: Path) -> None:
    """spec 9.2: flags inside the `bash -lc 'exec python3 -m dynamo.sglang \\ --flag ...'` body are extracted too."""
    manifest = tmp_path / "rendered.yaml"
    body = "set -euo pipefail\\nulimit -l unlimited || true\\nexec python3 -m dynamo.sglang \\\\\\n  --config /etc/sglang/agg.yaml \\\\\\n  --not-a-real-flag x\\n"
    manifest.write_text(
        DGD.format(args="''").replace("command: [python3, -m, dynamo.sglang]", "command: [/bin/bash, -lc]").replace("args: ['']", f'args: ["{body}"]')
    )
    helpf = tmp_path / "help.txt"
    helpf.write_text(HELP)
    res = _run(tmp_path, manifest, helpf)
    assert res.rc != 0 and "--not-a-real-flag" in res.stdout + res.stderr


def test_checks_config_map_keys_as_flags(tmp_path: Path) -> None:
    """spec 9.1/9.2: keys of the `--config` ConfigMap YAML are SGLang flags and are checked as well."""
    cm = (
        "---\napiVersion: v1\nkind: ConfigMap\nmetadata: {name: cfg, namespace: s-agg}\n"
        "data:\n  agg.yaml: |-\n    model-path: x\n    made-up-option: true\n"
    )
    m, h = _write(tmp_path, "--config, /etc/sglang/agg.yaml", cm)
    res = _run(tmp_path, m, h)
    assert res.rc != 0 and "made-up-option" in res.stdout + res.stderr


def test_missing_help_file_is_an_error(tmp_path: Path) -> None:
    """spec 9.1: a missing --help file is a usage error, never a silent pass."""
    m, _ = _write(tmp_path, "--config, x")
    res = S.Sandbox(tmp_path / "sb", {}).run("preflight-flags.sh", "--help-file", str(tmp_path / "nope.txt"), str(m))
    assert res.rc != 0


def test_real_overlay_passes_with_complete_help_and_fails_without_a_flag(tmp_path: Path) -> None:
    """spec 9.1: running on a real rendered overlay passes with a complete help file and fails when one used flag is removed."""
    rel = L.topology_rel("t-agg-hic")
    docs = L.render(rel)
    flags: set[str] = set()
    for comp in L.workers(L.dgds(docs)[0]):
        flags |= {f"--{k}" for k in L.effective_flags(docs, comp)}
        flags |= {f"--{k}" for k in L.parse_cli_flags(L.cli_tokens(L.main_container(comp)))}
    flags |= {"--config"}
    full = tmp_path / "full.txt"
    full.write_text("\n".join(f"  {f} X" for f in sorted(flags)) + "\n")
    overlay = L.overlay_path(rel)
    ok = S.Sandbox(tmp_path / "a", {})
    # a real kubectl is needed to render the directory: put the real one back by not stubbing it
    res_ok = _run_real_kubectl(tmp_path / "a", full, overlay)
    assert res_ok.returncode == 0, res_ok.stdout + res_ok.stderr
    missing = "--mamba-radix-cache-strategy"  # a flag of the Track Q engine (--dyn-tool-call-parser left with the other models)
    assert missing in flags
    partial = tmp_path / "partial.txt"
    partial.write_text("\n".join(f"  {f} X" for f in sorted(flags - {missing})) + "\n")
    res_bad = _run_real_kubectl(tmp_path / "b", partial, overlay)
    assert res_bad.returncode != 0
    assert missing in res_bad.stdout + res_bad.stderr
    del ok


def _run_real_kubectl(tmp: Path, helpf: Path, overlay: Path):
    import os

    import lib_proc

    tmp.mkdir(parents=True, exist_ok=True)
    return lib_proc.run(
        [str(S.SCRIPTS_DIR / "preflight-flags.sh"), "--help-file", str(helpf), str(overlay)],
        cwd=tmp,
        capture_output=True,
        text=True,
        env={**os.environ, "HOME": str(tmp)},
        timeout=120,
    )
