# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Per-technique ``examples/`` folders: the published manifests, one folder per cell.

``examples/<folder>/manifest.yaml`` is a written file, applied as is; it is the only source of the cell:

    examples/00-baseline          S0  aggregated, round-robin, no HiCache
    examples/01-kv-router         S1  + KV router (DYN_ROUTER_MODE=kv)
    examples/02-hicache           S2  + HiCache L2 host RAM / L3 local storage via NIXL, round-robin
    examples/03-pd-disaggregation S3  prefill/decode split over NIXL LIBFABRIC/EFA, round-robin
    examples/04-kv-router-hicache S4  KV router + HiCache, aggregated
    examples/05-kv-router-pd      S5  KV router + PD, no HiCache
    examples/06-pd-hicache        S6  PD + HiCache on prefill, round-robin
    examples/07-all-three         S7  PD + HiCache + KV router

Each folder: ``manifest.yaml`` and ``README.md``.  ``examples/README.md`` holds one table folder -> cell -> R/O/D switches, and
the examples table of ``docs/DEPLOY.md`` section 6 lists every folder with its cell and namespace.  What a folder changes is
the diff of its ``manifest.yaml`` against the baseline's (computed here with ``difflib``, as a reader runs ``diff``).

README table switches: R = KV router, O = HiCache offload, D = PD disaggregation; each cell value is ``on`` or ``off``.

The READMEs: every engine flag or env named in a README is in that folder's diff against the baseline (the baseline's: in its
manifest), and the tables of examples/README.md and docs/DEPLOY.md section 6 match the
folders.  Their length, sections and the knobs they explain are NOT pinned here.

Nothing here weakens an expectation: a missing file is a failure, never a skip.
"""

from __future__ import annotations

import difflib
import re
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

import lib_manifests as L
import lib_q38 as Q

EX = L.EXAMPLES
FOLDERS: dict[str, str] = {
    "00-baseline": "S0",
    "01-kv-router": "S1",
    "02-hicache": "S2",
    "03-pd-disaggregation": "S3",
    "04-kv-router-hicache": "S4",
    "05-kv-router-pd": "S5",
    "06-pd-hicache": "S6",
    "07-all-three": "S7",
}
SWITCHES: dict[str, tuple[str, str, str]] = {  # folder -> (R, O, D)
    "00-baseline": ("off", "off", "off"),
    "01-kv-router": ("on", "off", "off"),
    "02-hicache": ("off", "on", "off"),
    "03-pd-disaggregation": ("off", "off", "on"),
    "04-kv-router-hicache": ("on", "on", "off"),
    "05-kv-router-pd": ("on", "off", "on"),
    "06-pd-hicache": ("off", "on", "on"),
    "07-all-three": ("on", "on", "on"),
}
NAMESPACES: dict[str, str] = {  # folder -> the one namespace its DGD lands in
    "00-baseline": "s-agg",
    "01-kv-router": "s-agg",
    "02-hicache": "s-agg-hic",
    "03-pd-disaggregation": "s-dis",
    "04-kv-router-hicache": "s-agg-hic",
    "05-kv-router-pd": "s-dis",
    "06-pd-hicache": "s-dis-hic",
    "07-all-three": "s-dis-hic",
}
VARIANTS = [f for f in FOLDERS if f != "00-baseline"]
BASELINE = "00-baseline"
FILES = ("manifest.yaml", "README.md")
SINGLE_WORKER = ["01-kv-router", "02-hicache", "04-kv-router-hicache"]  # one Worker component
PD = ["03-pd-disaggregation", "05-kv-router-pd", "06-pd-hicache", "07-all-three"]  # prefill + decode components
# Combination folders -> (folder that differs by the Frontend only, folder that differs by the other technique).
COMBOS: dict[str, tuple[str, str]] = {
    "04-kv-router-hicache": ("02-hicache", "01-kv-router"),
    "05-kv-router-pd": ("03-pd-disaggregation", "07-all-three"),
    "06-pd-hicache": ("07-all-three", "03-pd-disaggregation"),
}


def _text(folder: str, name: str) -> str:
    p = EX / folder / name
    assert p.is_file(), f"missing examples/{folder}/{name}"
    return p.read_text()


def _code(folder: str) -> str:
    """The manifest without its full-line comments: the lines a parser reads, which the text checks below look at."""
    return "\n".join(ln for ln in _text(folder, "manifest.yaml").splitlines() if not ln.lstrip().startswith("#"))


def _dgd(folder: str) -> dict[str, Any]:
    docs = [d for d in yaml.safe_load_all(_text(folder, "manifest.yaml")) if d]
    assert len(docs) == 1
    return docs[0]


def _comps(folder: str) -> dict[str, dict[str, Any]]:
    return {c["name"]: c for c in _dgd(folder)["spec"]["components"]}


def _main(comp: dict[str, Any]) -> dict[str, Any]:
    return next(c for c in comp["podTemplate"]["spec"]["containers"] if c["name"] == "main")


def _diff_body(folder: str) -> str:
    """Unified diff of the baseline's manifest.yaml against the folder's, comment lines left out."""
    base, var = _code(BASELINE).splitlines(), _code(folder).splitlines()
    return "\n".join(difflib.unified_diff(base, var, f"a/{BASELINE}/manifest.yaml", f"b/{folder}/manifest.yaml", lineterm=""))


def _diff_lines(folder: str) -> list[str]:
    """Changed lines (+/-) of the diff, without the file headers."""
    return [ln for ln in _diff_body(folder).splitlines() if ln[:1] in "+-" and not ln.startswith(("+++", "---"))]


# ----------------------------------------------------------------------------- layout and cell mapping
def test_examples_directory_holds_exactly_the_eight_cell_folders_and_a_readme() -> None:
    assert EX.is_dir(), "examples/ missing"
    names = sorted(p.name for p in EX.iterdir() if p.name != ".DS_Store")
    assert names == sorted([*FOLDERS, "README.md"])


def test_the_folders_cover_every_cell_exactly_once() -> None:
    assert sorted(FOLDERS.values()) == sorted(Q.CELL_IDS) == [f"S{n}" for n in range(8)]
    assert len(set(FOLDERS.values())) == len(FOLDERS)
    assert {folder: cell for cell, folder in Q.EXAMPLES.items()} == FOLDERS, "lib_q38.EXAMPLES must map the same folders"


@pytest.mark.parametrize("folder,cell", FOLDERS.items())
def test_switches_and_namespace_match_the_manifest(folder: str, cell: str) -> None:
    """R/O/D of the folder, read from its manifest.yaml: kv router, a HiCache flag on a worker, prefill/decode components."""
    comps = _comps(folder)
    kv = L.env_value(_main(comps["Frontend"]), "DYN_ROUTER_MODE") == "kv"
    hic = any("--enable-hierarchical-cache" in L.cli_tokens(_main(c)) for n, c in comps.items() if n != "Frontend")
    dis = "prefill" in comps and "decode" in comps
    on = {True: "on", False: "off"}
    assert SWITCHES[folder] == (on[kv], on[hic], on[dis]), f"{folder}: manifest has R={kv} O={hic} D={dis}"
    topology = {(False, False): "t-agg", (True, False): "t-agg-hic", (False, True): "t-dis", (True, True): "t-dis-hic"}[(hic, dis)]
    assert Q.CELLS[cell] == (topology, "kv" if kv else "rr"), f"{folder} is {topology} with router {kv=}, not {cell}"
    assert NAMESPACES[folder] == L.TOPOLOGIES[topology] == _dgd(folder)["metadata"]["namespace"], folder


@pytest.mark.parametrize("folder", FOLDERS)
def test_folder_holds_exactly_its_files(folder: str) -> None:
    d = EX / folder
    assert d.is_dir(), f"examples/{folder} missing"
    assert sorted(p.name for p in d.iterdir() if p.name != ".DS_Store") == sorted(FILES)


# ----------------------------------------------------------------------------- manifest.yaml
@pytest.mark.parametrize("folder", FOLDERS)
def test_folder_applies_only_its_cell_namespace(folder: str) -> None:
    text = _text(folder, "manifest.yaml")
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
    images = set(re.findall(r"(?m)^\s*(?:- )?image:\s*(\S+)\s*$", _text(folder, "manifest.yaml")))
    assert images == {pinned}, f"{folder}: {sorted(images)} != versions.env RUNTIME_IMAGE"
    for name in FILES:
        if (EX / folder / name).is_file():
            assert ":latest" not in _text(folder, name), f"examples/{folder}/{name} references a :latest tag"


def test_single_worker_and_pd_lists_split_the_variants_by_their_components() -> None:
    assert sorted(SINGLE_WORKER + PD) == sorted(VARIANTS)
    for folder in VARIANTS:
        workers = [n for n in _comps(folder) if n != "Frontend"]
        assert workers == (["Worker"] if folder in SINGLE_WORKER else ["prefill", "decode"]), (folder, workers)


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
    assert "hicache" not in _code("03-pd-disaggregation").lower()
    assert "DYN_ROUTER_MODE" not in "\n".join(_diff_lines("03-pd-disaggregation"))


def test_all_three_diff_is_the_union_of_the_three_techniques() -> None:
    folder = "07-all-three"
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


# Neighbour cells (left, right): the same topology with the router off and on.
NEIGHBOURS = [
    ("00-baseline", "01-kv-router"),
    ("02-hicache", "04-kv-router-hicache"),
    ("03-pd-disaggregation", "05-kv-router-pd"),
    ("06-pd-hicache", "07-all-three"),
]


def _router_mode_value_line(folder: str) -> str:
    """The comment-free line holding the value of the Frontend's DYN_ROUTER_MODE, taken from the manifest."""
    lines = _code(folder).splitlines()
    at = [i for i, ln in enumerate(lines) if ln.strip() == "- name: DYN_ROUTER_MODE"]
    assert len(at) == 1, f"{folder}: DYN_ROUTER_MODE must be set exactly once (on the Frontend)"
    value = lines[at[0] + 1]
    assert value.strip() == f"value: {L.env_value(_main(_comps(folder)['Frontend']), 'DYN_ROUTER_MODE')}"
    return value


@pytest.mark.parametrize("left,right", NEIGHBOURS)
def test_documented_comment_free_diff_of_neighbour_cells_changes_only_the_router_mode_line(left: str, right: str) -> None:
    """Runs the command of examples/README.md as documented, on two folders: `grep -v '^ *#'` on both, then `diff`."""
    command = f"diff <(grep -v '^ *#' {EX / left / 'manifest.yaml'}) <(grep -v '^ *#' {EX / right / 'manifest.yaml'})"
    run = subprocess.run(["bash", "-c", command], capture_output=True, text=True, check=False)
    assert run.returncode == 1, f"diff exit {run.returncode} (1 = the files differ): {run.stderr}"
    changed = [ln for ln in run.stdout.splitlines() if ln[:1] in "<>"]
    assert changed == [f"< {_router_mode_value_line(left)}", f"> {_router_mode_value_line(right)}"]


def test_kv_router_hicache_is_the_kv_router_frontend_on_the_hicache_workers() -> None:
    folder = "04-kv-router-hicache"
    var = _comps(folder)
    assert list(var) == ["Frontend", "Worker"]
    assert var["Frontend"] == _comps("01-kv-router")["Frontend"]
    assert var["Worker"] == _comps("02-hicache")["Worker"]
    assert L.env_value(_main(var["Frontend"]), "DYN_ROUTER_MODE") == "kv"
    toks = L.cli_tokens(_main(var["Worker"]))
    assert "--enable-hierarchical-cache" in toks and toks[toks.index("--hicache-size") + 1] == Q.HICACHE_SIZE["agg"]
    assert not any("disaggregation" in t for t in toks)
    assert "vpc.amazonaws.com/efa" not in _code(folder)


def test_kv_router_pd_is_the_kv_router_frontend_on_the_pd_workers() -> None:
    folder = "05-kv-router-pd"
    var, pd = _comps(folder), _comps("03-pd-disaggregation")
    assert list(var) == ["Frontend", "prefill", "decode"]
    assert var["Frontend"] == _comps("01-kv-router")["Frontend"]
    assert var["prefill"] == pd["prefill"] and var["decode"] == pd["decode"]
    assert L.env_value(_main(var["Frontend"]), "DYN_ROUTER_MODE") == "kv"
    assert "hicache" not in _code(folder).lower(), "R + D must not enable HiCache"


def test_pd_hicache_is_all_three_with_the_baseline_frontend() -> None:
    folder = "06-pd-hicache"
    var, all3 = _comps(folder), _comps("07-all-three")
    assert list(var) == ["Frontend", "prefill", "decode"]
    assert var["Frontend"] == _comps(BASELINE)["Frontend"], "O + D keeps the round-robin Frontend"
    assert var["prefill"] == all3["prefill"] and var["decode"] == all3["decode"]
    pre, dec = L.cli_tokens(_main(var["prefill"])), L.cli_tokens(_main(var["decode"]))
    assert "--enable-hierarchical-cache" in pre and pre[pre.index("--hicache-size") + 1] == Q.HICACHE_SIZE["prefill"]
    assert "--enable-hierarchical-cache" not in dec and dec[dec.index("--disaggregation-mode") + 1] == "decode"
    assert "DYN_ROUTER_MODE" not in "\n".join(_diff_lines(folder))


# ----------------------------------------------------------------------------- READMEs
FLAG_RE = re.compile(r"(?<![\w-])--[a-z][a-z0-9-]+")
ENV_RE = re.compile(r"\b[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+\b")


@pytest.mark.parametrize("folder,cell", FOLDERS.items())
def test_readme_names_its_cell_and_the_apply_command(folder: str, cell: str) -> None:
    text = _text(folder, "README.md")
    assert re.search(rf"\b{cell}\b", text)
    assert f'kubectl --context "$KUBE_CONTEXT" apply --server-side -f examples/{folder}/manifest.yaml' in text
    assert "kubectl delete" not in text


@pytest.mark.parametrize("folder", FOLDERS)
def test_readme_names_the_namespace_of_its_cell(folder: str) -> None:
    assert f"`{NAMESPACES[folder]}`" in _text(folder, "README.md")


@pytest.mark.parametrize("folder", VARIANTS)
def test_every_flag_named_in_the_readme_appears_in_the_diff(folder: str) -> None:
    text = _text(folder, "README.md")
    body = _diff_body(folder)
    text = re.sub(r"(?m)^.*\bkubectl\b.*$|`[^`\n]*\bkubectl\b[^`\n]*`", "", text)  # kubectl command lines carry CLI flags (--tail), not engine flags
    named = set(FLAG_RE.findall(text)) | {e for e in ENV_RE.findall(text) if "_" in e}
    named -= {"README_MD"}
    missing = sorted(n for n in named if n not in body)
    assert not missing, f"named in README but absent from the diff against the baseline manifest: {missing}"


def test_every_flag_named_in_the_baseline_readme_is_in_its_render() -> None:
    prose = re.sub(r"(?m)^.*\bkubectl\b.*$", "", _text(BASELINE, "README.md"))  # the apply line carries CLI flags, not engine flags
    for flag in set(FLAG_RE.findall(prose)):
        assert flag in _text(BASELINE, "manifest.yaml"), f"{flag} named in the baseline README but absent from its manifest"


# ----------------------------------------------------------------------------- examples/README.md
def test_examples_readme_table_maps_folder_to_cell_and_switches() -> None:
    p = EX / "README.md"
    assert p.is_file(), "examples/README.md missing"
    text = p.read_text()
    rows = [ln for ln in text.splitlines() if ln.lstrip().startswith("|")]
    head = [c.strip() for c in rows[0].strip().strip("|").split("|")]
    for col in ("folder", "cell"):
        assert col in [h.lower() for h in head], f"column {col!r} missing: {head}"
    for sw in ("R", "O", "D"):
        assert sw in head, f"column {sw} missing: {head}"
    for folder, cell in FOLDERS.items():
        row = next((r for r in rows if f"`{folder}`" in r), None)
        assert row is not None, f"no table row for {folder}"
        cols = [c.strip().strip("`") for c in row.strip().strip("|").split("|")]
        assert len(cols) == len(head), f"{folder}: {len(cols)} cells for {len(head)} columns"
        by = dict(zip(head, cols))
        assert cell in cols, f"{folder} row must name {cell}: {cols}"
        assert tuple(by[sw].lower() for sw in ("R", "O", "D")) == SWITCHES[folder], f"{folder} R/O/D switches wrong: {cols}"
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


# ----------------------------------------------------------------------------- literal scan
ACCOUNT_RE = re.compile(r"(?<![0-9A-Za-z.])[0-9]{12}(?![0-9])")
REGION_RE = re.compile(r"\b(?:us|eu|ap|sa|ca|me|af)-(?:north|south|east|west|central|northeast|southeast)-\d\b")
BUCKET_RE = re.compile(r"(?:kubecon" + r"-dynamo|dynamo-sample)-(?:models|results)-")
EXEMPT = {"manifest.yaml"}  # the files that embed the pinned image by design


def _example_files() -> list[Path]:
    return [p for p in sorted(EX.rglob("*")) if p.is_file() and p.name != ".DS_Store"]


def test_only_the_manifests_may_carry_an_image_reference() -> None:
    files = _example_files()
    assert files, "examples/ empty"
    offenders = []
    for p in files:
        if p.name in EXEMPT:
            continue
        for n, ln in enumerate(p.read_text().splitlines(), 1):
            if ACCOUNT_RE.search(ln) or REGION_RE.search(ln) or BUCKET_RE.search(ln) or ".dkr.ecr." in ln:
                offenders.append(f"{p.relative_to(L.REPO_ROOT)}:{n}: {ln.strip()[:100]}")
    assert not offenders, "region/account/bucket literal outside the manifests:\n" + "\n".join(offenders)


@pytest.mark.parametrize("folder", FOLDERS)
def test_manifest_carries_only_the_configured_image_and_no_region_or_bucket_literal(folder: str) -> None:
    cfg = Q.CONFIG_ENV.read_text()
    image = next(ln.split("=", 1)[1].strip().strip("\"'") for ln in cfg.splitlines() if ln.startswith("RUNTIME_IMAGE="))
    text = _text(folder, "manifest.yaml")
    stripped = text.replace(image, "<IMAGE>")
    assert not ACCOUNT_RE.search(stripped), f"{folder}/manifest.yaml: account id outside the configured image"
    assert not REGION_RE.search(stripped), f"{folder}/manifest.yaml: region literal outside the configured image"
    assert not BUCKET_RE.search(text) and "eu-west-3" not in text and "euw3" not in text


def test_no_secret_like_content_in_examples() -> None:
    for p in _example_files():
        assert not re.search(r"AKIA[0-9A-Z]{16}|aws_secret|BEGIN [A-Z ]*PRIVATE KEY|hf_[A-Za-z0-9]{20,}", p.read_text()), p
