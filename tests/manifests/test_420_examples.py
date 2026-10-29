# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Per-technique ``examples/`` folders (readable view of the Track Q cells).  RED until implemented.

``manifests/track-q-qwen38`` stays the source of truth; ``examples/`` re-exposes all eight cells, one folder per cell:

    examples/00-baseline          S0  aggregated, round-robin, no HiCache
    examples/01-kv-router         S1  + KV router (DYN_ROUTER_MODE=kv)
    examples/02-hicache           S2  + HiCache L2 host RAM / L3 local storage via NIXL, round-robin
    examples/03-pd-disaggregation S3  prefill/decode split over NIXL LIBFABRIC/EFA, round-robin
    examples/04-all-three         S7  PD + HiCache + KV router
    examples/05-kv-router-hicache S4  KV router + HiCache, aggregated
    examples/06-kv-router-pd      S5  KV router + PD, no HiCache
    examples/07-pd-hicache        S6  PD + HiCache on prefill, round-robin

Each folder: ``kustomization.yaml`` (only resource = ``../../manifests/track-q-qwen38/cells/S<n>``), ``rendered.yaml``
(= ``kubectl kustomize`` of the folder), ``diff-vs-baseline.diff`` (unified diff of rendered.yaml vs the baseline's; absent
for 00-baseline) and ``README.md``.  ``examples/README.md`` holds one table folder -> cell -> R/O/D switches, and the
examples table of ``docs/DEPLOY.md`` section 6 lists every folder with its cell and namespace.

Contract pinned for ``scripts/render-examples.sh`` (mirrors ``scripts/render-track-q.sh``): ``render-examples.sh [OUT_ROOT]``
writes ``OUT_ROOT/<dir>/rendered.yaml`` and ``OUT_ROOT/<dir>/diff-vs-baseline.diff`` for the eight folders (default OUT_ROOT:
``examples``), rendering from the repo's ``examples/<dir>`` kustomizations.  The diff format is ``diff -u`` with ordinary
unified hunks; besides byte-equality with the script, the test checks that ``patch`` applied to the baseline reproduces the
variant, so the file is a real, format-correct diff.

README table switches: R = KV router, O = HiCache offload, D = PD disaggregation; each cell value is ``on`` or ``off``.

Nothing here weakens an expectation: a missing file is a failure, never a skip.
"""

from __future__ import annotations

import os
import re
import shutil
from pathlib import Path
from typing import Any

import pytest
import yaml

import lib_manifests as L
import lib_proc
import lib_trackq as Q

EX = L.REPO_ROOT / "examples"
SCRIPT = L.REPO_ROOT / "scripts" / "render-examples.sh"
FOLDERS: dict[str, str] = {
    "00-baseline": "S0",
    "01-kv-router": "S1",
    "02-hicache": "S2",
    "03-pd-disaggregation": "S3",
    "04-all-three": "S7",
    "05-kv-router-hicache": "S4",
    "06-kv-router-pd": "S5",
    "07-pd-hicache": "S6",
}
SWITCHES: dict[str, tuple[str, str, str]] = {  # folder -> (R, O, D)
    "00-baseline": ("off", "off", "off"),
    "01-kv-router": ("on", "off", "off"),
    "02-hicache": ("off", "on", "off"),
    "03-pd-disaggregation": ("off", "off", "on"),
    "04-all-three": ("on", "on", "on"),
    "05-kv-router-hicache": ("on", "on", "off"),
    "06-kv-router-pd": ("on", "off", "on"),
    "07-pd-hicache": ("off", "on", "on"),
}
NAMESPACES: dict[str, str] = {  # folder -> the one namespace its DGD lands in
    "00-baseline": "s-agg",
    "01-kv-router": "s-agg",
    "02-hicache": "s-agg-hic",
    "03-pd-disaggregation": "s-dis",
    "04-all-three": "s-dis-hic",
    "05-kv-router-hicache": "s-agg-hic",
    "06-kv-router-pd": "s-dis",
    "07-pd-hicache": "s-dis-hic",
}
VARIANTS = [f for f in FOLDERS if f != "00-baseline"]
BASELINE = "00-baseline"
FILES = ("kustomization.yaml", "rendered.yaml", "diff-vs-baseline.diff", "README.md")
SINGLE_WORKER = ["01-kv-router", "02-hicache", "05-kv-router-hicache"]  # one Worker component: plain ``diff -u`` sections
PD = ["03-pd-disaggregation", "04-all-three", "06-kv-router-pd", "07-pd-hicache"]  # prefill + decode components
# Combination folders -> (folder that differs by the Frontend only, folder that differs by the other technique).
COMBOS: dict[str, tuple[str, str]] = {
    "05-kv-router-hicache": ("02-hicache", "01-kv-router"),
    "06-kv-router-pd": ("03-pd-disaggregation", "04-all-three"),
    "07-pd-hicache": ("04-all-three", "03-pd-disaggregation"),
}


def _text(folder: str, name: str) -> str:
    p = EX / folder / name
    assert p.is_file(), f"missing examples/{folder}/{name}"
    return p.read_text()


def _kustomize(folder: str) -> str:
    assert (EX / folder / "kustomization.yaml").is_file(), f"examples/{folder}/kustomization.yaml missing"
    proc = lib_proc.run(["kubectl", "kustomize", str(EX / folder)], capture_output=True, text=True, timeout=120, check=False)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


def _dgd(folder: str) -> dict[str, Any]:
    docs = [d for d in yaml.safe_load_all(_text(folder, "rendered.yaml")) if d]
    assert len(docs) == 1
    return docs[0]


def _comps(folder: str) -> dict[str, dict[str, Any]]:
    return {c["name"]: c for c in _dgd(folder)["spec"]["components"]}


def _main(comp: dict[str, Any]) -> dict[str, Any]:
    return next(c for c in comp["podTemplate"]["spec"]["containers"] if c["name"] == "main")


def _diff_lines(folder: str) -> list[str]:
    """Changed lines (+/-) of the diff, without the file headers."""
    out = []
    for ln in _text(folder, "diff-vs-baseline.diff").splitlines():
        if ln.startswith(("+++", "---")):
            continue
        if ln[:1] in "+-" and ln[:1]:
            out.append(ln)
    return out


def _diff_body(folder: str) -> str:
    return _text(folder, "diff-vs-baseline.diff")


# ----------------------------------------------------------------------------- layout and cell mapping
def test_examples_directory_holds_exactly_the_eight_folders_and_a_readme() -> None:
    assert EX.is_dir(), "examples/ missing"
    names = sorted(p.name for p in EX.iterdir() if p.name != ".DS_Store")
    assert names == sorted([*FOLDERS, "README.md"])


def test_the_folders_cover_every_track_q_cell_exactly_once() -> None:
    assert sorted(FOLDERS.values()) == sorted(Q.CELL_IDS) == [f"S{n}" for n in range(8)]
    assert len(set(FOLDERS.values())) == len(FOLDERS)


def _cell_composition(cell: str) -> tuple[str, bool]:
    """(topology overlay, KV router on) read from the cell's own kustomization, the source of truth."""
    k = yaml.safe_load((L.MANIFESTS / Q.TQ / "cells" / cell / "kustomization.yaml").read_text())
    (overlay,) = k["resources"]
    topology = Path(overlay).name
    comps = [Path(c).name for c in k.get("components") or []]
    assert set(comps) <= {"router-kv"}, comps
    return topology, "router-kv" in comps


@pytest.mark.parametrize("folder,cell", FOLDERS.items())
def test_switches_and_namespace_match_the_composition_of_the_cell(folder: str, cell: str) -> None:
    topology, kv = _cell_composition(cell)
    assert topology in Q.ARMS and cell == Q.ARMS[topology][1 if kv else 0], (cell, topology, kv)
    on = {True: "on", False: "off"}
    assert SWITCHES[folder] == (on[kv], on[topology in Q.HIC], on[topology in Q.DIS]), f"{folder}: R/O/D of {cell} is {topology}+{kv=}"
    assert NAMESPACES[folder] == L.TOPOLOGIES[topology], f"{folder}: {cell} lands in {L.TOPOLOGIES[topology]}"


@pytest.mark.parametrize("folder", FOLDERS)
def test_folder_holds_exactly_its_files(folder: str) -> None:
    d = EX / folder
    assert d.is_dir(), f"examples/{folder} missing"
    expected = [f for f in FILES if not (folder == BASELINE and f == "diff-vs-baseline.diff")]
    assert sorted(p.name for p in d.iterdir() if p.name != ".DS_Store") == sorted(expected)


def test_baseline_has_no_diff_file() -> None:
    assert not (EX / BASELINE / "diff-vs-baseline.diff").exists()


@pytest.mark.parametrize("folder,cell", FOLDERS.items())
def test_kustomization_has_the_cell_as_only_resource(folder: str, cell: str) -> None:
    k = yaml.safe_load(_text(folder, "kustomization.yaml"))
    assert k["kind"] == "Kustomization"
    assert set(k) <= {"apiVersion", "kind", "resources"}, f"extra kustomize fields: {sorted(k)}"
    assert len(k["resources"]) == 1
    target = (EX / folder / k["resources"][0]).resolve()
    assert target == (L.MANIFESTS / Q.TQ / "cells" / cell).resolve(), f"{folder} must point at cells/{cell}, got {k['resources'][0]}"


# ----------------------------------------------------------------------------- rendered.yaml
@pytest.mark.parametrize("folder", FOLDERS)
def test_rendered_equals_kubectl_kustomize_of_the_folder(folder: str) -> None:
    assert _text(folder, "rendered.yaml") == _kustomize(folder), f"examples/{folder}/rendered.yaml drifted: run scripts/render-examples.sh"


@pytest.mark.parametrize("folder,cell", FOLDERS.items())
def test_rendered_equals_the_committed_track_q_render(folder: str, cell: str) -> None:
    committed = (Q.RENDERED_DIR / f"{cell}.yaml").read_bytes()
    assert (EX / folder / "rendered.yaml").read_bytes() == committed, f"{folder} differs from rendered/{cell}.yaml"


@pytest.mark.parametrize("folder", FOLDERS)
def test_folder_applies_only_its_cell_namespace(folder: str) -> None:
    text = _text(folder, "rendered.yaml")
    docs = [d for d in yaml.safe_load_all(text) if d]
    assert [d["kind"] for d in docs] == ["DynamoGraphDeployment"], "one DGD, no Namespace or other object"
    assert docs[0]["metadata"]["namespace"] == NAMESPACES[folder]
    namespaces = set(re.findall(r"(?m)^\s*namespace:\s*(\S+)\s*$", text))
    assert namespaces == {NAMESPACES[folder]}, f"{folder} renders into {sorted(namespaces)}"


def _pinned_runtime_image() -> str:
    pins = (L.REPO_ROOT / "versions.env").read_text().splitlines()
    return next(ln.split("=", 1)[1].strip() for ln in pins if ln.startswith("RUNTIME_IMAGE="))


@pytest.mark.parametrize("folder", FOLDERS)
def test_rendered_runs_only_the_versions_env_runtime_image_and_nothing_is_latest(folder: str) -> None:
    pinned = _pinned_runtime_image()
    assert "@sha256:" in pinned, pinned
    images = set(re.findall(r"(?m)^\s*(?:- )?image:\s*(\S+)\s*$", _text(folder, "rendered.yaml")))
    assert images == {pinned}, f"{folder}: {sorted(images)} != versions.env RUNTIME_IMAGE"
    for name in FILES:
        if (EX / folder / name).is_file():
            assert ":latest" not in _text(folder, name), f"examples/{folder}/{name} references a :latest tag"


# ----------------------------------------------------------------------------- diff-vs-baseline.diff
@pytest.mark.parametrize("folder", VARIANTS)
def test_diff_is_not_empty_and_names_baseline_and_variant(folder: str) -> None:
    body = _diff_body(folder)
    assert _diff_lines(folder), "diff has no changed lines"
    assert "00-baseline" in body.splitlines()[0] and folder in body.splitlines()[1]


# ----------------------------------------------------------------------------- per-component diff (readable)
# diff-vs-baseline.diff is a concatenation of SECTIONS, one per changed part, each a ``diff -u`` of two text blocks cut from the
# rendered files: the DGD head (everything before ``  components:``, label ``metadata``) and each ``  - name: X`` component block.
# Pairing: Frontend vs Frontend; every other component (Worker, prefill, decode) vs the baseline's Worker.  Section order:
# metadata, then the variant's components in render order.  Identical pairs produce no section.  Section headers:
#   --- a/00-baseline/rendered.yaml#<baseline part>
#   +++ b/<folder>/rendered.yaml#<variant part>
# A section may replace changed lines that another (earlier) section of the same file already shows by ONE note line
#   # ... <N> changed lines identical to <part> omitted
# (<part> = the section that still shows them).  A section may also collapse ONE contiguous run of >= 20 added lines (the GPU-pin
# shell preamble of the launch script, which is the same in every PD component and lives in the Track Q component) into
#   # ... <N> added lines omitted (GPU-pin preamble)
# Hunk context is free for 03/04 (any ``diff -U<n>``, n >= 0); only the changed lines are checked.  Nothing else may be dropped or invented.
MAX_LINES = {folder: 150 for folder in PD}
NOTE_RE = re.compile(r"^# \.\.\. (\d+) changed lines identical to (\w+) omitted$")
BLOCK_RE = re.compile(r"^# \.\.\. (\d+) added lines omitted \(GPU-pin preamble\)$")
HEADER_RE = re.compile(r"^--- a/00-baseline/rendered\.yaml#(\w+)$")


def _split_parts(text: str) -> tuple[list[str], dict[str, list[str]]]:
    lines = text.splitlines(keepends=True)
    i = lines.index("  components:\n")
    parts: dict[str, list[str]] = {}
    name: str | None = None
    for ln in lines[i + 1:]:
        if ln.startswith("  - name: "):
            name = ln.split(": ", 1)[1].strip()
            parts[name] = []
        elif name is None or (ln.strip() and not ln.startswith("   ")):
            break
        parts[name].append(ln)
    return lines[:i], parts


def _plain_diff(a: list[str], b: list[str], tmp: Path) -> list[str]:
    (tmp / "a").write_text("".join(a))
    (tmp / "b").write_text("".join(b))
    proc = lib_proc.run(["diff", "-u", "--label", "A", "--label", "B", str(tmp / "a"), str(tmp / "b")], capture_output=True, text=True, check=False)
    assert proc.returncode in (0, 1), proc.stderr
    return proc.stdout.splitlines()[2:]  # drop the two header lines


def _expected_sections(folder: str, tmp: Path) -> list[tuple[str, str, list[str]]]:
    """(baseline part, variant part, plain unified body) for every non-identical pair."""
    bh, bp = _split_parts(_text(BASELINE, "rendered.yaml"))
    vh, vp = _split_parts(_text(folder, "rendered.yaml"))
    out = []
    pairs = [("metadata", "metadata", bh, vh)] + [("Frontend" if n == "Frontend" else "Worker", n, bp["Frontend" if n == "Frontend" else "Worker"], v) for n, v in vp.items()]
    for bn, vn, b, v in pairs:
        body = _plain_diff(b, v, tmp)
        if body:
            out.append((bn, vn, body))
    return out


def _file_sections(folder: str) -> list[tuple[str, str, list[str]]]:
    lines = _text(folder, "diff-vs-baseline.diff").splitlines()
    out: list[tuple[str, str, list[str]]] = []
    i = 0
    while i < len(lines):
        m = HEADER_RE.match(lines[i])
        assert m, f"line {i + 1}: expected a section header '--- a/00-baseline/rendered.yaml#<part>', got {lines[i]!r}"
        assert i + 1 < len(lines) and lines[i + 1].startswith(f"+++ b/{folder}/rendered.yaml#"), f"line {i + 2}: bad +++ header"
        vn = lines[i + 1].split("#", 1)[1]
        j = i + 2
        while j < len(lines) and not lines[j].startswith("--- a/"):
            j += 1
        out.append((m.group(1), vn, lines[i + 2:j]))
        i = j
    return out


def _changed(body: list[str]) -> list[str]:
    return [ln for ln in body if ln[:1] in "+-"]


@pytest.mark.parametrize("folder", VARIANTS)
def test_diff_sections_follow_the_per_component_layout(folder: str, tmp_path: Path) -> None:
    exp = _expected_sections(folder, tmp_path)
    got = _file_sections(folder)
    assert [(b, v) for b, v, _ in got] == [(b, v) for b, v, _ in exp], "sections must be metadata then components in render order, identical pairs omitted"
    assert not any(v == "Worker" and folder in PD for _, v, _ in got)


def test_single_worker_and_pd_lists_split_the_variants_by_their_components() -> None:
    assert sorted(SINGLE_WORKER + PD) == sorted(VARIANTS)
    for folder in VARIANTS:
        workers = [n for n in _comps(folder) if n != "Frontend"]
        assert workers == (["Worker"] if folder in SINGLE_WORKER else ["prefill", "decode"]), (folder, workers)


@pytest.mark.parametrize("folder", SINGLE_WORKER)
def test_small_diffs_are_byte_equal_to_the_plain_per_component_diff(folder: str, tmp_path: Path) -> None:
    expected = ""
    for bn, vn, body in _expected_sections(folder, tmp_path):
        expected += f"--- a/00-baseline/rendered.yaml#{bn}\n+++ b/{folder}/rendered.yaml#{vn}\n" + "\n".join(body) + "\n"
    assert _text(folder, "diff-vs-baseline.diff") == expected


@pytest.mark.parametrize("folder", PD)
def test_pd_diffs_are_the_plain_per_component_diff_with_only_declared_omissions(folder: str, tmp_path: Path) -> None:
    exp = {(b, v): body for b, v, body in _expected_sections(folder, tmp_path)}
    shown: dict[str, list[str]] = {}
    omitted_any = False
    for bn, vn, body in _file_sections(folder):
        plain = exp[(bn, vn)]
        plain_changed = [ln for ln in plain if ln[:1] in "+-"]
        got_changed = _changed(body)
        it = iter(plain_changed)  # the shown changed lines are an in-order subsequence of the plain ones: nothing invented or reordered
        assert all(any(g == p for p in it) for g in got_changed), f"{folder}#{vn}: changed lines not a subsequence of the plain diff"
        left = collections_counter(plain_changed) - collections_counter(got_changed)
        notes = [m for m in (NOTE_RE.match(ln) for ln in body) if m]
        blocks = [int(m.group(1)) for m in (BLOCK_RE.match(ln) for ln in body) if m]
        assert len(blocks) <= 1, f"{folder}#{vn}: at most one preamble block note per section"
        declared = sum(int(m.group(1)) for m in notes) + sum(blocks)
        assert declared == sum(left.values()), f"{folder}#{vn}: notes declare {declared} omitted lines, {sum(left.values())} are missing"
        for m in notes:
            assert m.group(2) in shown, f"{folder}#{vn}: note points at {m.group(2)}, which is not an earlier section"
        rest = left
        for n in blocks:
            assert n >= 20, "a preamble block note must collapse >= 20 lines"
            run = _added_run(plain_changed, left, n)
            assert run is not None, f"{folder}#{vn}: no contiguous run of {n} added lines is missing from the section"
            rest = rest - collections_counter(run)
            omitted_any = True
        if notes:
            omitted_any = True
            pool = collections_counter([ln for m in notes for ln in shown[m.group(2)]])
            assert not (rest - pool), f"{folder}#{vn}: omitted lines are not shown in the referenced section(s)"
        shown[vn] = got_changed
    assert omitted_any, "the identical prefill/decode blocks must be shown once, not twice"


def _added_run(plain_changed: list[str], left: Any, n: int) -> list[str] | None:
    """First window of ``n`` consecutive changed lines, all '+', that is entirely covered by the missing lines."""
    for i in range(len(plain_changed) - n + 1):
        win = plain_changed[i:i + n]
        if all(w.startswith("+") for w in win) and not (collections_counter(win) - left):
            return win
    return None


def collections_counter(items: list[str]) -> Any:
    import collections

    return collections.Counter(items)


@pytest.mark.parametrize("folder", VARIANTS)
def test_each_section_without_notes_patches_the_baseline_part_into_the_variant_part(folder: str, tmp_path: Path) -> None:
    bh, bp = _split_parts(_text(BASELINE, "rendered.yaml"))
    vh, vp = _split_parts(_text(folder, "rendered.yaml"))
    base = {"metadata": bh, **{k: v for k, v in bp.items()}}
    var = {"metadata": vh, **vp}
    checked = 0
    for bn, vn, body in _file_sections(folder):
        if any(NOTE_RE.match(ln) or BLOCK_RE.match(ln) for ln in body):
            continue
        src, dst, pf = tmp_path / "src", tmp_path / "out", tmp_path / "p.diff"
        src.write_text("".join(base[bn]))
        pf.write_text("\n".join(["--- a", "+++ b", *body]) + "\n")
        proc = lib_proc.run(["patch", "-s", "-o", str(dst), str(src), str(pf)], capture_output=True, text=True, check=False)
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert dst.read_text() == "".join(var[vn]), f"{folder}#{vn}"
        checked += 1
    assert checked, "at least one section must be a complete, patchable diff"


@pytest.mark.parametrize("folder,limit", MAX_LINES.items())
def test_pd_diffs_stay_short_enough_to_read(folder: str, limit: int) -> None:
    n = len(_text(folder, "diff-vs-baseline.diff").splitlines())
    assert n <= limit, f"{folder}/diff-vs-baseline.diff has {n} lines (> {limit})"


def _flag_map(c: dict[str, Any]) -> dict[str, str | None]:
    toks = L.cli_tokens(c)
    out: dict[str, str | None] = {}
    for i, t in enumerate(toks):
        if t.startswith("--") and len(t) > 2:
            nxt = toks[i + 1] if i + 1 < len(toks) and not toks[i + 1].startswith("--") else None
            out[t] = nxt
    return out


def _resources(c: dict[str, Any]) -> dict[str, str]:
    r = c.get("resources", {}) or {}
    return {f"{kind}.{k}": str(v) for kind in ("limits", "requests") for k, v in (r.get(kind) or {}).items()}


@pytest.mark.parametrize("folder", VARIANTS)
def test_every_changed_flag_env_and_resource_between_s0_and_the_cell_is_in_the_diff(folder: str) -> None:
    base, var = _comps(BASELINE), _comps(folder)
    body = _diff_body(folder)
    seen = 0
    for name, comp in var.items():
        b, v = _main(base["Frontend" if name == "Frontend" else "Worker"]), _main(comp)
        bf, vf = _flag_map(b), _flag_map(v)
        for flag, val in vf.items():
            if bf.get(flag, "<absent>") != val:
                seen += 1
                assert flag in body, f"{name}: changed flag {flag} missing from the diff"
                if val is not None:
                    assert val in body, f"{name}: value {val!r} of {flag} missing from the diff"
        be, ve = dict(Q.env_pairs(b)), dict(Q.env_pairs(v))
        for env, val in ve.items():
            if be.get(env, "<absent>") != val:
                seen += 1
                assert env in body, f"{name}: changed env {env} missing from the diff"
        br, vr = _resources(b), _resources(v)
        for res, val in vr.items():
            if br.get(res) != val:
                seen += 1
                key = res.split(".", 1)[1]
                assert re.search(rf"^[+-]\s*{re.escape(key)}: \"?{re.escape(val)}\"?$", body, re.M), f"{name}: resource {res}={val} missing from the diff"
    assert seen, "nothing changed between S0 and the cell?"


def test_pd_cells_show_the_efa_resource_and_gpu_env_in_the_diff() -> None:
    for folder in PD:
        body = _diff_body(folder)
        for needle in ("vpc.amazonaws.com/efa", "EXPECTED_GPUS", "SGLANG_DISAGGREGATION_NIXL_BACKEND", "--disaggregation-bootstrap-port"):
            assert needle in body, f"{folder}: {needle} missing"


@pytest.mark.parametrize("folder", VARIANTS)
def test_variant_readme_says_how_the_diff_is_organised(folder: str) -> None:
    text = _text(folder, "README.md")
    assert "diff-vs-baseline.diff" in text, "README must explain the diff file"
    assert re.search(r"per[- ]component", text, re.I), "README must say the diff is organised per component"
    if folder in PD:
        assert all(w in text for w in ("Worker", "prefill", "decode")), "README must name the Worker/prefill/decode pairing"
        assert re.search(r"once|omitted|identical", text, re.I), "README must say that identical blocks are shown once"


# ----------------------------------------------------------------------------- root README
def _section(text: str, heading_prefix: str) -> str:
    m = re.search(rf"^#+ {re.escape(heading_prefix)}.*?(?=^#{{1,3}} |\Z)", text, re.M | re.S)
    assert m, f"section {heading_prefix!r} not found"
    return m.group(0)


def test_root_readme_links_the_examples_folder() -> None:
    text = (L.REPO_ROOT / "README.md").read_text()
    assert re.search(r"\]\(examples/(?:README\.md)?\)", text), "README.md must link examples/ as a markdown link"
    where = [h for h in ("Repository layout", "Quick start") if "(examples/" in _section(text, h)]
    assert where, "the examples/ pointer must sit in the Repository layout or Quick start section"


# ----------------------------------------------------------------------------- the script
def test_render_script_is_a_clean_executable_offline_bash_script() -> None:
    assert SCRIPT.is_file(), f"missing {SCRIPT.relative_to(L.REPO_ROOT)}"
    assert os.access(SCRIPT, os.X_OK)
    text = SCRIPT.read_text()
    assert text.splitlines()[0] in ("#!/usr/bin/env bash", "#!/bin/bash") and "set -euo pipefail" in text
    assert lib_proc.run(["bash", "-n", str(SCRIPT)], capture_output=True, text=True).returncode == 0
    if shutil.which("shellcheck"):
        proc = lib_proc.run(["shellcheck", "-x", "--shell=bash", str(SCRIPT)], capture_output=True, text=True)
        assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "kubectl kustomize" in text
    code = "\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith("#"))
    for forbidden in ("kubectl apply", "kubectl delete", "kubectl create", "terraform", "aws ", "helm "):
        assert forbidden not in code, f"{forbidden!r} in render-examples.sh"
    assert not re.search(r"\brm\b(?!.*\"\$work\")", code.replace("rm -rf \"$work\"", "")), "rm outside the temp dir cleanup"


def test_render_script_reproduces_every_committed_file(tmp_path: Path) -> None:
    out = tmp_path / "out"
    proc = lib_proc.run([str(SCRIPT), str(out)], capture_output=True, text=True, timeout=300, cwd=tmp_path, check=False)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert sorted(p.name for p in out.iterdir()) == sorted(FOLDERS)
    for folder in FOLDERS:
        produced = sorted(p.name for p in (out / folder).iterdir())
        expected = ["rendered.yaml"] if folder == BASELINE else ["diff-vs-baseline.diff", "rendered.yaml"]
        assert produced == expected, folder
        for name in expected:
            assert (out / folder / name).read_bytes() == (EX / folder / name).read_bytes(), f"{folder}/{name} drifted"


def test_render_script_header_documents_the_diff_layout_of_every_variant() -> None:
    header = "\n".join(ln for ln in SCRIPT.read_text().splitlines()[1:] if ln.startswith("#"))
    single = re.search(r"One worker component \(([^)]*)\)", header)
    several = re.search(r"Several worker components \(([^:)]*)[:)]", header)
    assert single and several, "the header must list the one-worker and several-worker folders"
    assert sorted(re.findall(r"\d\d-[a-z-]+", single.group(1))) == sorted(SINGLE_WORKER)
    assert sorted(re.findall(r"\d\d-[a-z-]+", several.group(1))) == sorted(PD)


def test_render_script_default_target_is_examples_and_it_does_not_touch_manifests() -> None:
    text = SCRIPT.read_text()
    assert "examples" in text
    assert "manifests/track-q-qwen38/rendered" not in re.sub(r"#.*", "", text), "script must not write the Track Q renders"


# ----------------------------------------------------------------------------- each diff touches only its technique
def _path_set(folder: str) -> list[str]:
    return Q.diff_paths(_dgd(BASELINE), _dgd(folder))


def test_kv_router_diff_is_exactly_dyn_router_mode() -> None:
    assert _diff_lines("01-kv-router") == ["-            value: round-robin", "+            value: kv"]
    base, var = _comps(BASELINE), _comps("01-kv-router")
    assert L.env_value(_main(var["Frontend"]), "DYN_ROUTER_MODE") == "kv"
    assert L.env_value(_main(base["Frontend"]), "DYN_ROUTER_MODE") == "round-robin"
    assert _dgd(BASELINE)["metadata"] == _dgd("01-kv-router")["metadata"]
    assert base["Worker"] == var["Worker"]
    assert len(_path_set("01-kv-router")) == 1


def test_hicache_diff_touches_only_hicache_flags_l3_env_volumes_and_memory() -> None:
    base, var = _comps(BASELINE), _comps("02-hicache")
    assert sorted(var) == sorted(base) == ["Frontend", "Worker"]
    assert var["Frontend"] == base["Frontend"], "HiCache must not touch the Frontend / router"
    assert L.env_value(_main(var["Frontend"]), "DYN_ROUTER_MODE") == "round-robin"
    assert _dgd("02-hicache")["metadata"]["namespace"] == "s-agg-hic"
    bm, vm = _main(base["Worker"]), _main(var["Worker"])
    added_env = sorted(set(n for n, _ in Q.env_pairs(vm)) - set(n for n, _ in Q.env_pairs(bm)))
    assert added_env and all(n.startswith("SGLANG_HICACHE_NIXL_") for n in added_env), added_env
    assert [n for n, _ in Q.env_pairs(bm)] == [n for n, _ in Q.env_pairs(vm) if n in {m for m, _ in Q.env_pairs(bm)}]
    base_tokens, var_tokens = L.cli_tokens(bm), L.cli_tokens(vm)
    assert all(t in var_tokens for t in base_tokens), "HiCache must only add flags, never drop or change the baseline ones"
    new_flags = {t for t in var_tokens if t.startswith("--")} - {t for t in base_tokens if t.startswith("--")}
    assert "--enable-hierarchical-cache" in new_flags
    assert all("hicache" in f or f in {"--enable-hierarchical-cache", "--engine-route"} for f in new_flags), new_flags
    assert not any("disaggregation" in t for t in var_tokens)
    vols = {v["name"] for v in var["Worker"]["podTemplate"]["spec"]["volumes"]} - {
        v["name"] for v in base["Worker"]["podTemplate"]["spec"]["volumes"]
    }
    assert vols == {"hicache-l3"}
    # the only changed top-level container fields are args/env/resources/volumeMounts
    changed = {k for k in set(bm) | set(vm) if bm.get(k) != vm.get(k)}
    assert changed <= {"args", "env", "resources", "volumeMounts"}, changed
    assert not re.search(r"disaggregation|prefill|decode|LIBFABRIC|DYN_ROUTER_MODE", "\n".join(_diff_lines("02-hicache")))


def test_pd_diff_touches_only_the_topology_components() -> None:
    base, var = _comps(BASELINE), _comps("03-pd-disaggregation")
    assert list(var) == ["Frontend", "prefill", "decode"] and "Worker" not in var
    assert _dgd("03-pd-disaggregation")["metadata"]["namespace"] == "s-dis"
    assert var["Frontend"] == base["Frontend"], "PD must not touch the Frontend / router"
    assert L.env_value(_main(var["Frontend"]), "DYN_ROUTER_MODE") == "round-robin"
    for name, mode in (("prefill", "prefill"), ("decode", "decode")):
        toks = L.cli_tokens(_main(var[name]))
        assert toks[toks.index("--disaggregation-mode") + 1] == mode
        assert toks[toks.index("--disaggregation-transfer-backend") + 1] == "nixl"
        assert not any("hicache" in t or t == "--enable-hierarchical-cache" for t in toks), "PD alone must not enable HiCache"
        assert "LIBFABRIC" in "".join(str(v) for _, v in Q.env_pairs(_main(var[name])))
    assert "hicache" not in _text("03-pd-disaggregation", "rendered.yaml").lower()
    assert "DYN_ROUTER_MODE" not in "\n".join(_diff_lines("03-pd-disaggregation"))


def test_all_three_diff_is_the_union_of_the_three_techniques() -> None:
    folder = "04-all-three"
    base, var = _comps(BASELINE), _comps(folder)
    assert list(var) == ["Frontend", "prefill", "decode"]
    assert _dgd(folder)["metadata"]["namespace"] == "s-dis-hic"
    assert L.env_value(_main(var["Frontend"]), "DYN_ROUTER_MODE") == "kv"
    pre, dec = L.cli_tokens(_main(var["prefill"])), L.cli_tokens(_main(var["decode"]))
    assert "--enable-hierarchical-cache" in pre and "--disaggregation-mode" in pre and "--enable-hierarchical-cache" not in dec
    assert dec[dec.index("--disaggregation-mode") + 1] == "decode"
    lines = set(_diff_lines(folder))
    for other in ("01-kv-router", "02-hicache", "03-pd-disaggregation"):
        solo = {ln for ln in _diff_lines(other) if ln.startswith("+") and ln.strip("+ ") and not ln.lstrip("+ ").startswith("namespace:")}
        # every flag / env line a single technique adds shows up when all three are on (indentation may differ: compare stripped)
        stripped = {ln.strip() for ln in lines}
        missing = {ln for ln in solo if ln.strip() not in stripped and "memory" not in ln}
        # the HiCache size differs on purpose (agg 200 vs prefill 400) and the volume path carries the topology namespace
        missing = {ln for ln in missing if not re.search(r'^\+\s*- "?\d+"?$|/s-agg-hic|mountPath|subPathExpr|memory', ln)}
        assert not missing, f"{other} lines absent from the union diff: {sorted(missing)[:5]}"
    assert base["Frontend"] != var["Frontend"]


# ----------------------------------------------------------------------------- the three combination folders (S4, S5, S6)
@pytest.mark.parametrize("folder,neighbour", [(f, n) for f, (n, _) in COMBOS.items()])
def test_combination_differs_from_its_neighbour_by_the_router_only(folder: str, neighbour: str) -> None:
    mine, theirs = _dgd(folder), _dgd(neighbour)
    assert mine["metadata"] == theirs["metadata"], "same namespace, name and labels as the neighbour"
    mc, tc = _comps(folder), _comps(neighbour)
    assert list(mc) == list(tc)
    assert {n for n in mc if mc[n] != tc[n]} == {"Frontend"}, f"{folder} vs {neighbour}: only the Frontend may differ"
    modes = {L.env_value(_main(mc["Frontend"]), "DYN_ROUTER_MODE"), L.env_value(_main(tc["Frontend"]), "DYN_ROUTER_MODE")}
    assert modes == {"kv", "round-robin"}
    a, b = _main(mc["Frontend"]), _main(tc["Frontend"])
    assert {k for k in set(a) | set(b) if a.get(k) != b.get(k)} == {"env"}
    assert [e for e in a["env"] if e["name"] != "DYN_ROUTER_MODE"] == [e for e in b["env"] if e["name"] != "DYN_ROUTER_MODE"]


def test_kv_router_hicache_is_the_kv_router_frontend_on_the_hicache_workers() -> None:
    folder = "05-kv-router-hicache"
    var = _comps(folder)
    assert list(var) == ["Frontend", "Worker"]
    assert var["Frontend"] == _comps("01-kv-router")["Frontend"]
    assert var["Worker"] == _comps("02-hicache")["Worker"]
    assert L.env_value(_main(var["Frontend"]), "DYN_ROUTER_MODE") == "kv"
    toks = L.cli_tokens(_main(var["Worker"]))
    assert "--enable-hierarchical-cache" in toks and toks[toks.index("--hicache-size") + 1] == Q.HICACHE_SIZE["agg"]
    assert not any("disaggregation" in t for t in toks)
    assert "vpc.amazonaws.com/efa" not in _text(folder, "rendered.yaml")


def test_kv_router_pd_is_the_kv_router_frontend_on_the_pd_workers() -> None:
    folder = "06-kv-router-pd"
    var, pd = _comps(folder), _comps("03-pd-disaggregation")
    assert list(var) == ["Frontend", "prefill", "decode"]
    assert var["Frontend"] == _comps("01-kv-router")["Frontend"]
    assert var["prefill"] == pd["prefill"] and var["decode"] == pd["decode"]
    assert L.env_value(_main(var["Frontend"]), "DYN_ROUTER_MODE") == "kv"
    assert "hicache" not in _text(folder, "rendered.yaml").lower(), "R + D must not enable HiCache"


def test_pd_hicache_is_all_three_with_the_baseline_frontend() -> None:
    folder = "07-pd-hicache"
    var, all3 = _comps(folder), _comps("04-all-three")
    assert list(var) == ["Frontend", "prefill", "decode"]
    assert var["Frontend"] == _comps(BASELINE)["Frontend"], "O + D keeps the round-robin Frontend"
    assert var["prefill"] == all3["prefill"] and var["decode"] == all3["decode"]
    pre, dec = L.cli_tokens(_main(var["prefill"])), L.cli_tokens(_main(var["decode"]))
    assert "--enable-hierarchical-cache" in pre and pre[pre.index("--hicache-size") + 1] == Q.HICACHE_SIZE["prefill"]
    assert "--enable-hierarchical-cache" not in dec and dec[dec.index("--disaggregation-mode") + 1] == "decode"
    assert "DYN_ROUTER_MODE" not in "\n".join(_diff_lines(folder))
    assert not any(bn == "Frontend" for bn, _, _ in _file_sections(folder)), "an unchanged Frontend gets no section"


@pytest.mark.parametrize("folder,pair", COMBOS.items())
def test_combination_readme_names_the_cells_it_differs_from_and_says_to_delete_first(folder: str, pair: tuple[str, str]) -> None:
    text = _text(folder, "README.md")
    for other in pair:
        assert f"`{other}`" in text, f"README must name the folder {other}"
        assert re.search(rf"\b{FOLDERS[other]}\b", text), f"README must name the cell {FOLDERS[other]} of {other}"
    assert re.search(r"(?i)\bdelete\b", text), "README must say to delete the folder before the next one is applied"


# ----------------------------------------------------------------------------- READMEs
README_REQUIRED = ("What it does", "What it changes", "How to apply", "Where to look")
UNIT_RESULT = re.compile(
    r"\d\s*(?:ms|us|µs|sec|seconds?|tok(?:ens)?/s|req(?:uests)?/s|[KMGT]B/s|GB/s|%)(?![A-Za-z])|\b\d+(?:\.\d+)?\s*[x×](?![A-Za-z0-9])|\bp\d{2}\b",
    re.I,
)
FLAG_RE = re.compile(r"(?<![\w-])--[a-z][a-z0-9-]+")
ENV_RE = re.compile(r"\b[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+\b")


@pytest.mark.parametrize("folder", FOLDERS)
def test_readme_length_language_and_sections(folder: str) -> None:
    text = _text(folder, "README.md")
    assert 5 <= len(text.splitlines()) <= 30, f"{len(text.splitlines())} lines"
    heads = [ln for ln in text.splitlines() if ln.startswith("#")]
    assert heads and heads[0].startswith("# ")
    for section in README_REQUIRED:
        assert any(section.lower() in h.lower() and h.startswith("##") for h in heads), f"missing '## {section}'"
    assert text.isascii(), "English/ASCII only"
    assert re.search(r"\b(the|and|which|is)\b", text), "README must be prose in English"


@pytest.mark.parametrize("folder,cell", FOLDERS.items())
def test_readme_names_its_cell_and_the_apply_command(folder: str, cell: str) -> None:
    text = _text(folder, "README.md")
    assert re.search(rf"\b{cell}\b", text)
    assert f"kubectl apply -k examples/{folder}" in text
    assert "kubectl delete" not in text


@pytest.mark.parametrize("folder", FOLDERS)
def test_readme_names_the_namespace_of_its_cell(folder: str) -> None:
    assert f"`{NAMESPACES[folder]}`" in _text(folder, "README.md")


@pytest.mark.parametrize("folder", FOLDERS)
def test_readme_points_at_a_demo_dashboard(folder: str) -> None:
    text = _text(folder, "README.md")
    assert any(d in text for d in ("dashboards/demo/take.json", "dashboards/demo/compare.json", "dashboards/sglang")), (
        "README must name take.json, compare.json or the sglang dashboard"
    )
    for ref in re.findall(r"dashboards/[\w./-]+", text):
        ref = ref.rstrip(".,)")
        assert (L.REPO_ROOT / ref).exists(), f"README cites non-existent {ref}"


@pytest.mark.parametrize("folder", FOLDERS)
def test_readme_has_no_measured_results(folder: str) -> None:
    text = _text(folder, "README.md")
    hits = [m.group(0) for m in UNIT_RESULT.finditer(text)]
    assert not hits, f"README looks like it quotes measured numbers: {hits}"
    assert not re.search(r"speed-?up|faster|slower|throughput of|latency of|reduces? (?:ttft|latency)", text, re.I)


@pytest.mark.parametrize("folder", VARIANTS)
def test_every_flag_named_in_the_readme_appears_in_the_diff(folder: str) -> None:
    text = _text(folder, "README.md")
    body = _diff_body(folder)
    named = set(FLAG_RE.findall(text)) | {e for e in ENV_RE.findall(text) if "_" in e}
    named -= {"README_MD"}
    assert named, "a technique README must name the exact flags/fields it changes"
    missing = sorted(n for n in named if n not in body)
    assert not missing, f"named in README but absent from diff-vs-baseline.diff: {missing}"


EXPECTED_KNOBS = {
    "01-kv-router": {"DYN_ROUTER_MODE"},
    "02-hicache": {"--enable-hierarchical-cache", "SGLANG_HICACHE_NIXL_BACKEND_STORAGE_DIR"},
    "03-pd-disaggregation": {"--disaggregation-mode", "--disaggregation-transfer-backend"},
    "04-all-three": {"DYN_ROUTER_MODE", "--enable-hierarchical-cache", "--disaggregation-mode"},
    "05-kv-router-hicache": {"DYN_ROUTER_MODE", "--enable-hierarchical-cache", "SGLANG_HICACHE_NIXL_BACKEND_STORAGE_DIR"},
    "06-kv-router-pd": {"DYN_ROUTER_MODE", "--disaggregation-mode", "--disaggregation-transfer-backend"},
    "07-pd-hicache": {"--enable-hierarchical-cache", "--disaggregation-mode", "SGLANG_HICACHE_NIXL_BACKEND_STORAGE_DIR"},
}


def test_every_variant_names_its_main_knobs() -> None:
    assert sorted(EXPECTED_KNOBS) == sorted(VARIANTS)


@pytest.mark.parametrize("folder,knobs", EXPECTED_KNOBS.items())
def test_readme_names_the_main_knobs_of_its_technique(folder: str, knobs: set[str]) -> None:
    text = _text(folder, "README.md")
    assert not {k for k in knobs if k not in text}, f"README lacks {sorted(k for k in knobs if k not in text)}"


def test_baseline_readme_states_it_is_the_reference_with_every_technique_off() -> None:
    text = _text(BASELINE, "README.md").lower()
    assert "baseline" in text and "round-robin" in text
    assert "hicache" in text and "kv router" in text.replace("-", " ") or "kv-router" in text
    for flag in set(FLAG_RE.findall(_text(BASELINE, "README.md"))):
        assert flag in _text(BASELINE, "rendered.yaml"), f"{flag} named in the baseline README but absent from its render"


# ----------------------------------------------------------------------------- examples/README.md
def test_examples_readme_table_maps_folder_to_cell_and_switches() -> None:
    p = EX / "README.md"
    assert p.is_file(), "examples/README.md missing"
    text = p.read_text()
    assert len(text.splitlines()) <= 60 and text.isascii()
    assert "manifests/track-q-qwen38" in text and re.search(r"source of truth", text, re.I)
    rows = [ln for ln in text.splitlines() if ln.lstrip().startswith("|")]
    header = rows[0]
    for col in ("folder", "cell"):
        assert col in header.lower()
    for sw in ("R", "O", "D"):
        assert re.search(rf"\|\s*{sw}\s*\|", header + "|") or re.search(rf"\b{sw}\b", header)
    for folder, cell in FOLDERS.items():
        row = next((r for r in rows if folder in r), None)
        assert row is not None, f"no table row for {folder}"
        cols = [c.strip().strip("`") for c in row.strip().strip("|").split("|")]
        assert cell in cols, f"{folder} row must name {cell}: {cols}"
        assert tuple(c.lower() for c in cols[-3:]) == SWITCHES[folder], f"{folder} R/O/D switches wrong: {cols[-3:]}"
    assert len([r for r in rows if re.search(r"\|\s*S\d\s*\|", r)]) == len(FOLDERS) == 8


# ----------------------------------------------------------------------------- docs/DEPLOY.md, section 6 (the examples table)
def _deploy_section_6() -> str:
    text = (L.REPO_ROOT / "docs" / "DEPLOY.md").read_text()
    m = re.search(r"^## 6\. Deploy one cell.*?(?=^## )", text, re.M | re.S)
    assert m, "docs/DEPLOY.md lacks '## 6. Deploy one cell'"
    return m.group(0)


def test_deploy_examples_table_lists_every_folder_with_its_cell_and_namespace() -> None:
    sec = _deploy_section_6()
    rows = [ln for ln in sec.splitlines() if ln.lstrip().startswith("|")]
    assert rows and [c.strip().lower() for c in rows[0].strip().strip("|").split("|")][:3] == ["folder", "cell", "namespace"]
    body = [r for r in rows[1:] if "examples/" in r]
    assert len(body) == len(FOLDERS), f"{len(body)} example rows, expected {len(FOLDERS)}"
    for folder, cell in FOLDERS.items():
        row = next((r for r in body if f"`examples/{folder}`" in r), None)
        assert row is not None, f"DEPLOY.md section 6 has no row for examples/{folder}"
        cols = [c.strip().strip("`") for c in row.strip().strip("|").split("|")]
        assert cols[1:3] == [cell, NAMESPACES[folder]], f"{folder}: {cols[1:3]}"
    assert not re.search(r"S4\s+to\s+S6\s+are\s+in|S4\.\.S6", sec), "S4 to S6 have folders now: drop the 'only in cells/' sentence"


# ----------------------------------------------------------------------------- literal scan, as tight as track-q rendered
ACCOUNT_RE = re.compile(r"(?<![0-9A-Za-z.])[0-9]{12}(?![0-9])")
REGION_RE = re.compile(r"\b(?:us|eu|ap|sa|ca|me|af)-(?:north|south|east|west|central|northeast|southeast)-\d\b")
BUCKET_RE = re.compile(r"(?:kubecon" + r"-dynamo|dynamo-sample)-(?:models|results)-")
EXEMPT = {"rendered.yaml", "diff-vs-baseline.diff"}  # the same files-by-design that embed the placeholder image in track-q rendered/


def _example_files() -> list[Path]:
    return [p for p in sorted(EX.rglob("*")) if p.is_file() and p.name != ".DS_Store"]


def test_only_rendered_outputs_may_carry_the_mumbai_image_reference() -> None:
    files = _example_files()
    assert files, "examples/ empty"
    offenders = []
    for p in files + ([SCRIPT] if SCRIPT.is_file() else []):
        if p.name in EXEMPT:
            continue
        for n, ln in enumerate(p.read_text().splitlines(), 1):
            if ACCOUNT_RE.search(ln) or REGION_RE.search(ln) or BUCKET_RE.search(ln) or ".dkr.ecr." in ln:
                offenders.append(f"{p.relative_to(L.REPO_ROOT)}:{n}: {ln.strip()[:100]}")
    assert not offenders, "region/account/bucket literal outside rendered outputs:\n" + "\n".join(offenders)


@pytest.mark.parametrize("folder", FOLDERS)
def test_rendered_outputs_carry_only_the_configured_image_and_no_paris_or_bucket_literal(folder: str) -> None:
    cfg = Q.CONFIG_ENV.read_text()
    image = next(ln.split("=", 1)[1].strip().strip("\"'") for ln in cfg.splitlines() if ln.startswith("RUNTIME_IMAGE="))
    for name in ("rendered.yaml", "diff-vs-baseline.diff"):
        if folder == BASELINE and name.endswith(".diff"):
            continue
        text = _text(folder, name)
        stripped = text.replace(image, "<IMAGE>")
        assert not ACCOUNT_RE.search(stripped), f"{folder}/{name}: account id outside the configured image"
        assert not REGION_RE.search(stripped), f"{folder}/{name}: region literal outside the configured image"
        assert not BUCKET_RE.search(text) and "eu-west-3" not in text and "euw3" not in text


def test_no_secret_like_content_in_examples() -> None:
    for p in _example_files() + ([SCRIPT] if SCRIPT.is_file() else []):
        assert not re.search(r"AKIA[0-9A-Z]{16}|aws_secret|BEGIN [A-Z ]*PRIVATE KEY|hf_[A-Za-z0-9]{20,}", p.read_text()), p
