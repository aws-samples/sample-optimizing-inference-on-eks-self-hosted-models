# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The NVLink option of the four prefill/decode example folders: ``examples/<03|04|06|07>/nvlink/``.  RED until implemented.

The sample has eight examples, ``00`` to ``07``, one per Track Q cell; there is no ninth.  The four folders that have the D switch on
(``03-pd-disaggregation`` S3, ``04-all-three`` S7, ``06-kv-router-pd`` S5, ``07-pd-hicache`` S6) carry BOTH prefill/decode
transports on one page:

* Option A, EFA, the default: the folder itself (NIXL LIBFABRIC, works across nodes; the recorded runs used it);
* Option B, NVLink, a design reference that was not run on p6-b200: the sub-folder ``nvlink/`` (NIXL UCX with ``cuda_ipc`` over
  NVLink, prefill and decode on the SAME node).

What this file pins, for each of the four folders:

* ``nvlink/`` holds exactly ``kustomization.yaml`` (one resource, ``manifests/track-q-qwen38/variants-nvlink/<cell>``),
  ``rendered.yaml`` (= ``kubectl kustomize`` of ``nvlink/`` = ``rendered-nvlink/<cell>.yaml``, byte for byte) and
  ``diff-vs-efa.diff``;
* ``diff-vs-efa.diff`` is a plain ``diff -u`` of the folder's EFA ``rendered.yaml`` against ``nvlink/rendered.yaml``: the first two
  lines are ``--- a/<folder>/rendered.yaml`` and ``+++ b/<folder>/nvlink/rendered.yaml`` (no timestamps, so it is
  deterministic), ``patch`` applied to the EFA render reproduces the NVLink render, no line is omitted, and what it changes is
  exactly the transport knobs (NIXL backend ``UCX``, ``UCX_TLS=cuda_ipc,cuda_copy,tcp``, ``UCX_PROTO_INFO``, ``UCX_LOG_LEVEL``,
  ``hostPID: true`` on the prefill and decode pods); the EFA device request, ``FI_*``, ``privileged`` and the GPU-pin wrapper
  stay (EFA stays the path between nodes);
* the NVLink option sits on the p6-b200 baseline of the sample (pool ``gpu-b200-cb``, ``trtllm_mha``, fp8 KV, the pinned runtime
  image) and equals the folder's EFA render except for those knobs;
* the README of the folder is ONE page with both options: headings ``Option A - EFA`` and ``Option B - NVLink``, each with its
  apply command, what B changes, its prerequisites, the delete-before-apply warning (same namespace, same DGD ``q38``) and how to
  check the transport that was really used (LIBFABRIC instantiated for EFA; ``cuda_ipc`` in the UCX protocol table for NVLink),
  with the warning that a pod without the recipe silently falls back to TCP (the figures live in the PD guide, not here: the
  example READMEs quote no measured number);
* there is no ninth example folder; the published surface (``lib_frozen``) is byte-identical: ``rendered/S0..S7``, the
  goldens, the top-level files of the example folders 00 to 07, the driver, the plans and ``components/pd-efa``.

Nothing here weakens an expectation: a missing file is a failure, never a skip.
"""

from __future__ import annotations

import copy
import hashlib
import re
from pathlib import Path
from typing import Any

import lib_frozen as F
import lib_manifests as L
import lib_proc
import lib_trackq as Q
import pytest
import test_420_examples as E
import yaml

EX = E.EX
TQ = L.MANIFESTS / Q.TQ
RENDERED_NVL = TQ / "rendered-nvlink"
NVL = E.NVL_DIR
PD_CELLS: dict[str, str] = {folder: E.FOLDERS[folder] for folder in E.PD}
CASES = list(PD_CELLS.items())
IDS = [f for f, _ in CASES]
POOL = "gpu-b200-cb"
UCX_ENV = {"UCX_TLS": "cuda_ipc,cuda_copy,tcp", "UCX_PROTO_INFO": "y", "UCX_LOG_LEVEL": "info"}
ROLES = ("prefill", "decode")
LABEL = re.compile(r"(?i)design reference|reference design")
NOT_RUN_P6 = re.compile(
    r"(?i)not[^.]*\b(run|measured|recorded|tested)\b[^.]*p6-b200|p6-b200[^.]*\bnot\b[^.]*\b(run|measured|recorded|tested)\b")
SPEED_WORDS = re.compile(r"speed-?up|faster|slower|quicker|throughput of|latency of|reduces? (?:ttft|latency)|beats|outperform|wins\b", re.I)


def _nvl_path(folder: str, name: str) -> Path:
    return EX / folder / NVL / name


def _read(folder: str, name: str) -> str:
    p = _nvl_path(folder, name)
    assert p.is_file(), f"missing examples/{folder}/{NVL}/{name}"
    return p.read_text()


def _dgd(folder: str, sub: str = "") -> dict[str, Any]:
    p = EX / folder / sub / "rendered.yaml"
    assert p.is_file(), f"missing {p.relative_to(L.REPO_ROOT)}"
    docs = [d for d in yaml.safe_load_all(p.read_text()) if d]
    assert len(docs) == 1, f"{p}: one document"
    return docs[0]


def _components(dgd: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {c["name"]: c for c in dgd["spec"]["components"]}


def _main(comp: dict[str, Any]) -> dict[str, Any]:
    return next(c for c in comp["podTemplate"]["spec"]["containers"] if c["name"] == "main")


def _env(comp: dict[str, Any]) -> dict[str, str]:
    return {e["name"]: e.get("value", "") for e in _main(comp)["env"]}


def _runtime_image() -> str:
    return next(ln.split("=", 1)[1].strip().strip("\"'") for ln in Q.CONFIG_ENV.read_text().splitlines() if ln.startswith("RUNTIME_IMAGE="))


# ----------------------------------------------------------------------------- there is no ninth example
def test_there_is_no_ninth_example_folder_and_no_other_extra_folder() -> None:
    ninth = "08-pd-" + "nvlink"  # built in two pieces: no file of the tree may carry that name, this one included
    assert not (EX / ninth).exists(), f"examples/{ninth} must be deleted: the NVLink option lives in nvlink/ of the PD folders"
    assert not list(EX.glob("08*")) and not list(EX.glob("09*"))
    assert sorted(p.name for p in EX.iterdir() if p.is_dir()) == sorted(E.FOLDERS)


def test_only_the_four_pd_folders_have_an_nvlink_subfolder() -> None:
    have = sorted(p.parent.name for p in EX.glob(f"*/{NVL}") if p.is_dir())
    assert have == sorted(E.PD), f"nvlink/ sub-folders: {have}"
    assert set(E.PD) == {"03-pd-disaggregation", "04-all-three", "06-kv-router-pd", "07-pd-hicache"}
    assert all(E.SWITCHES[f][2] == "on" for f in E.PD) and all(E.SWITCHES[f][2] == "off" for f in E.FOLDERS if f not in E.PD)


# ----------------------------------------------------------------------------- layout
@pytest.mark.parametrize("folder,cell", CASES, ids=IDS)
def test_nvlink_folder_holds_exactly_its_three_files(folder: str, cell: str) -> None:
    d = EX / folder / NVL
    assert d.is_dir(), f"examples/{folder}/{NVL} missing"
    assert sorted(p.name for p in d.iterdir() if p.name != ".DS_Store") == sorted(E.NVL_FILES)


@pytest.mark.parametrize("folder,cell", CASES, ids=IDS)
def test_kustomization_has_one_resource_the_nvlink_variant_of_the_cell(folder: str, cell: str) -> None:
    k = yaml.safe_load(_read(folder, "kustomization.yaml"))
    assert k["kind"] == "Kustomization"
    assert set(k) <= {"apiVersion", "kind", "resources"}, f"extra kustomize fields: {sorted(k)}"
    assert [(EX / folder / NVL / r).resolve() for r in k["resources"]] == [(TQ / "variants-nvlink" / cell).resolve()], k["resources"]


@pytest.mark.parametrize("folder,cell", CASES, ids=IDS)
def test_kustomization_says_it_is_a_design_reference_not_run_on_p6_b200(folder: str, cell: str) -> None:
    head = " ".join(ln.lstrip("# ") for ln in _read(folder, "kustomization.yaml").splitlines() if ln.startswith("#"))
    assert LABEL.search(head), "the header carries the design reference label"
    assert NOT_RUN_P6.search(head)
    assert "rendered-nvlink" in head or "variants-nvlink" in head, "the header points at the NVLink tree"


# ----------------------------------------------------------------------------- rendered.yaml
@pytest.mark.parametrize("folder,cell", CASES, ids=IDS)
def test_rendered_equals_kubectl_kustomize_of_the_nvlink_folder(folder: str, cell: str) -> None:
    assert _read(folder, "rendered.yaml") == E._kustomize(f"{folder}/{NVL}"), (
        f"examples/{folder}/{NVL}/rendered.yaml drifted: run scripts/render-examples.sh")


@pytest.mark.parametrize("folder,cell", CASES, ids=IDS)
def test_rendered_is_the_nvlink_tree_render_of_the_cell_byte_for_byte(folder: str, cell: str) -> None:
    committed = RENDERED_NVL / f"{cell}.yaml"
    assert committed.is_file(), f"manifests/track-q-qwen38/rendered-nvlink/{cell}.yaml missing"
    assert _read(folder, "rendered.yaml") == committed.read_text(), f"{folder}/{NVL} and rendered-nvlink/{cell}.yaml are the same render"


def test_nvlink_render_is_not_a_published_render() -> None:
    """No cell and no render of record for NVLink: ``rendered/`` holds exactly S0..S7 (the EFA runs of record)."""
    assert sorted(p.name for p in Q.RENDERED_DIR.iterdir() if p.name != ".DS_Store") == [f"S{n}.yaml" for n in range(8)]
    assert sorted(p.name for p in (TQ / "cells").iterdir() if p.name != ".DS_Store") == [f"S{n}" for n in range(8)]


@pytest.mark.parametrize("folder,cell", CASES, ids=IDS)
def test_one_dgd_lands_in_the_namespace_of_the_cell_as_q38(folder: str, cell: str) -> None:
    text = _read(folder, "rendered.yaml")
    docs = [d for d in yaml.safe_load_all(text) if d]
    assert [d["kind"] for d in docs] == ["DynamoGraphDeployment"]
    assert docs[0]["metadata"]["name"] == "q38" and docs[0]["metadata"]["namespace"] == E.NAMESPACES[folder]
    assert set(re.findall(r"(?m)^\s*namespace:\s*(\S+)\s*$", text)) == {E.NAMESPACES[folder]}


@pytest.mark.parametrize("folder,cell", CASES, ids=IDS)
def test_rendered_runs_the_configured_runtime_image_and_nothing_is_latest(folder: str, cell: str) -> None:
    text = _read(folder, "rendered.yaml")
    assert set(re.findall(r"(?m)^\s*(?:- )?image:\s*(\S+)\s*$", text)) == {_runtime_image()}
    for name in E.NVL_FILES:
        assert ":latest" not in _read(folder, name)


@pytest.mark.parametrize("folder,cell", CASES, ids=IDS)
def test_no_literal_account_region_bucket_or_secret_in_the_nvlink_folder(folder: str, cell: str) -> None:
    image = _runtime_image()
    for name in ("rendered.yaml", "diff-vs-efa.diff"):
        text = _read(folder, name).replace(image, "<IMAGE>")
        assert not E.ACCOUNT_RE.search(text), f"{name}: account id outside the configured image"
        assert not E.REGION_RE.search(text), f"{name}: region literal outside the configured image"
        assert not E.BUCKET_RE.search(text)
    for p in (EX / folder / NVL).iterdir():
        assert not re.search(r"AKIA[0-9A-Z]{16}|aws_secret|BEGIN [A-Z ]*PRIVATE KEY|hf_[A-Za-z0-9]{20,}", p.read_text()), p


# ----------------------------------------------------------------------------- built on the p6-b200 baseline
@pytest.mark.parametrize("role", ROLES)
@pytest.mark.parametrize("folder,cell", CASES, ids=IDS)
def test_roles_sit_on_the_b200_pool_with_the_baseline_engine_flags(folder: str, cell: str, role: str) -> None:
    comps = _components(_dgd(folder, NVL))
    assert list(comps) == ["Frontend", "prefill", "decode"]
    comp = comps[role]
    assert comp["replicas"] == _components(_dgd(folder))[role]["replicas"] == 4
    spec = comp["podTemplate"]["spec"]
    text = yaml.safe_dump(spec)
    assert POOL in text and "gpu-h200" not in text
    args = " ".join(str(a) for a in _main(comp)["args"])
    assert "--attention-backend trtllm_mha" in args and "--kv-cache-dtype fp8_e4m3" in args
    assert f"--disaggregation-mode {role}" in args and "--disaggregation-transfer-backend nixl" in args
    assert _main(comp)["resources"]["limits"]["nvidia.com/gpu"] in (1, "1")


@pytest.mark.parametrize("role", ROLES)
@pytest.mark.parametrize("folder,cell", CASES, ids=IDS)
def test_roles_move_the_kv_over_ucx_cuda_ipc_with_host_pid(folder: str, cell: str, role: str) -> None:
    comp = _components(_dgd(folder, NVL))[role]
    env = _env(comp)
    assert env["SGLANG_DISAGGREGATION_NIXL_BACKEND"] == "UCX"
    for name, value in UCX_ENV.items():
        assert env.get(name) == value, f"{role}: {name}"
    spec = comp["podTemplate"]["spec"]
    assert spec.get("hostPID") is True, f"{role}: hostPID (UCX cuda_ipc needs one PID namespace)"
    assert _main(comp)["securityContext"]["privileged"] is True
    assert "LIBFABRIC" not in "".join(env.values())
    script = str(_main(comp)["args"][0])
    assert "GPU pin" in script and "CUDA_VISIBLE_DEVICES" in script and env["EXPECTED_GPUS"] == "1"


@pytest.mark.parametrize("role", ROLES)
@pytest.mark.parametrize("folder,cell", CASES, ids=IDS)
def test_roles_keep_the_efa_device_for_traffic_between_nodes(folder: str, cell: str, role: str) -> None:
    comp = _components(_dgd(folder, NVL))[role]
    res = _main(comp)["resources"]
    assert res["requests"]["vpc.amazonaws.com/efa"] in (1, "1") and res["limits"]["vpc.amazonaws.com/efa"] in (1, "1")
    assert _env(comp)["FI_PROVIDER"] == "efa"


def _undo_knobs(dgd: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(dgd)
    for comp in _components(out).values():
        if comp["name"] not in ROLES:
            continue
        comp["podTemplate"]["spec"].pop("hostPID", None)
        env = _main(comp)["env"]
        env[:] = [e for e in env if e["name"] not in UCX_ENV]
        for e in env:
            if e["name"] == "SGLANG_DISAGGREGATION_NIXL_BACKEND":
                e["value"] = "LIBFABRIC"
    return out


@pytest.mark.parametrize("folder,cell", CASES, ids=IDS)
def test_nvlink_equals_the_efa_render_of_the_folder_except_for_the_transport_knobs(folder: str, cell: str) -> None:
    efa, nvl = _dgd(folder), _dgd(folder, NVL)
    assert _undo_knobs(nvl) == efa, f"{folder}/{NVL} differs from the EFA render by more than the knobs: " + ", ".join(Q.diff_paths(efa, _undo_knobs(nvl)))


@pytest.mark.parametrize("folder,cell", CASES, ids=IDS)
def test_paths_that_differ_from_the_efa_render_are_exactly_the_transport_knobs(folder: str, cell: str) -> None:
    paths = Q.diff_paths(_dgd(folder), _dgd(folder, NVL))
    assert paths, f"{folder}: the NVLink option must differ from the EFA render"
    allowed = re.compile(r"^\.spec\.components\[(1|2)\]\.podTemplate\.spec(\.hostPID|\.containers\[0\]\.env)$")
    stray = [p for p in paths if not allowed.match(p)]
    assert not stray, f"paths outside the transport knobs: {stray}"


@pytest.mark.parametrize("folder,cell", CASES, ids=IDS)
def test_frontend_and_hicache_flags_are_untouched_by_the_transport_change(folder: str, cell: str) -> None:
    efa, nvl = _components(_dgd(folder)), _components(_dgd(folder, NVL))
    assert nvl["Frontend"] == efa["Frontend"]
    for role in ROLES:
        assert L.cli_tokens(_main(nvl[role])) == L.cli_tokens(_main(efa[role])), f"{role}: engine flags unchanged"


# ----------------------------------------------------------------------------- diff-vs-efa.diff
def _diff_text(folder: str) -> str:
    return _read(folder, "diff-vs-efa.diff")


def _changed(lines: list[str]) -> list[str]:
    return [ln for ln in lines if ln[:1] in "+-" and not ln.startswith(("+++", "---"))]


@pytest.mark.parametrize("folder,cell", CASES, ids=IDS)
def test_diff_header_names_the_efa_render_and_the_nvlink_render_without_timestamps(folder: str, cell: str) -> None:
    lines = _diff_text(folder).splitlines()
    assert lines[0] == f"--- a/{folder}/rendered.yaml" and lines[1] == f"+++ b/{folder}/{NVL}/rendered.yaml", lines[:2]
    assert not any(re.search(r"\d{4}-\d{2}-\d{2}", ln) for ln in lines[:2]), "a timestamp in the header makes the file non-deterministic"
    assert lines[2].startswith("@@"), "ordinary unified hunks follow the two header lines"


@pytest.mark.parametrize("folder,cell", CASES, ids=IDS)
def test_diff_patches_the_efa_render_into_the_nvlink_render(folder: str, cell: str, tmp_path: Path) -> None:
    out = tmp_path / "out.yaml"
    proc = lib_proc.run(["patch", "-s", "-o", str(out), str(EX / folder / "rendered.yaml"), str(_nvl_path(folder, "diff-vs-efa.diff"))],
                        capture_output=True, text=True, check=False)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert out.read_text() == _read(folder, "rendered.yaml"), "patch(EFA render, diff-vs-efa.diff) is not the NVLink render"


@pytest.mark.parametrize("folder,cell", CASES, ids=IDS)
def test_diff_changed_lines_are_exactly_the_plain_diff_u_of_the_two_renders(folder: str, cell: str) -> None:
    proc = lib_proc.run(["diff", "-u", str(EX / folder / "rendered.yaml"), str(_nvl_path(folder, "rendered.yaml"))],
                        capture_output=True, text=True, check=False)
    assert proc.returncode == 1, proc.stderr
    plain = _changed(proc.stdout.splitlines())
    got_lines = _diff_text(folder).splitlines()
    assert not any(ln.startswith("# ...") for ln in got_lines), "nothing is omitted: the diff is small enough to show in full"
    assert _changed(got_lines) == plain


@pytest.mark.parametrize("folder,cell", CASES, ids=IDS)
def test_diff_shows_the_knobs_the_nvlink_option_flips_and_nothing_else(folder: str, cell: str) -> None:
    body = _diff_text(folder)
    changed = "\n".join(_changed(body.splitlines()))
    assert "SGLANG_DISAGGREGATION_NIXL_BACKEND" in body, "the diff names the backend variable (as context of the changed value)"
    for token in ("UCX", "LIBFABRIC", "UCX_TLS", "cuda_ipc,cuda_copy,tcp", "UCX_PROTO_INFO", "UCX_LOG_LEVEL", "hostPID"):
        assert token in changed, f"the diff lacks {token}"
    for stay in ("vpc.amazonaws.com/efa", "FI_PROVIDER", "privileged", "DYN_ROUTER_MODE", "--disaggregation-mode", "hicache", "image:", "namespace:"):
        assert stay not in changed, f"{stay!r} is not a transport knob and must not be in the diff"
    assert len(body.splitlines()) <= 150


# ----------------------------------------------------------------------------- the README: one page, both options
def _readme(folder: str) -> str:
    return (EX / folder / "README.md").read_text()


def _option(text: str, label: str) -> str:
    """Body of the section whose heading starts with ``label`` (up to the next heading of the same or a higher level)."""
    m = re.search(rf"(?m)^(#{{2,4}}) {re.escape(label)}\b.*$", text)
    assert m, f"no heading starting with {label!r}"
    nxt = re.search(rf"(?m)^#{{1,{len(m.group(1))}}} ", text[m.end():])
    return text[m.start():m.end() + nxt.start()] if nxt else text[m.start():]


def _slug(h: str) -> str:
    """GitHub-style anchor of a heading (same rule as ``tests/publish/test_docs_set.py``)."""
    s = re.sub(r"[`*_]", "", h.strip().lower())
    s = re.sub(r"[^\w\- ]", "", s)
    return s.replace(" ", "-")


@pytest.mark.parametrize("folder,cell", CASES, ids=IDS)
def test_readme_has_both_options_as_headings_in_order(folder: str, cell: str) -> None:
    text = _readme(folder)
    a = re.search(r"(?m)^#{2,4} Option A - EFA\b.*$", text)
    b = re.search(r"(?m)^#{2,4} Option B - NVLink\b.*$", text)
    assert a, "heading 'Option A - EFA ...'"
    assert b, "heading 'Option B - NVLink ...'"
    assert a.start() < b.start()
    assert re.search(r"(?i)default", a.group(0)) and re.search(r"(?i)across nodes|between nodes|different nodes|other nodes", a.group(0)), (
        "Option A is labelled default and says it works across nodes")
    assert re.search(r"(?i)same node", b.group(0)) and re.search(r"(?i)design reference", b.group(0)), (
        "Option B is labelled same node only and design reference")


@pytest.mark.parametrize("folder,cell", CASES, ids=IDS)
def test_readme_has_one_apply_command_per_option(folder: str, cell: str) -> None:
    text = _readme(folder)
    a, b = _option(text, "Option A - EFA"), _option(text, "Option B - NVLink")
    efa_cmd = re.compile(rf"kubectl apply -k examples/{re.escape(folder)}(?![\w/-])")
    nvl_cmd = re.compile(rf"kubectl apply -k examples/{re.escape(folder)}/{NVL}(?![\w/-])")
    assert efa_cmd.search(a) and not nvl_cmd.search(a), "Option A applies the folder itself"
    assert nvl_cmd.search(b) and not efa_cmd.search(b), "Option B applies the nvlink/ sub-folder"
    assert "kubectl delete" not in text, "the README warns in words, it carries no delete command"


@pytest.mark.parametrize("folder,cell", CASES, ids=IDS)
def test_readme_option_b_states_what_it_changes_and_its_prerequisites(folder: str, cell: str) -> None:
    b = re.sub(r"\s+", " ", _option(_readme(folder), "Option B - NVLink"))
    for needle in ("SGLANG_DISAGGREGATION_NIXL_BACKEND=UCX", "UCX_TLS=cuda_ipc,cuda_copy,tcp", "hostPID", "privileged", "Pod Security", "diff-vs-efa.diff"):
        assert needle in b, f"Option B lacks {needle!r}"
    assert re.search(r"(?i)prefill and decode[^.]*\bsame node\b|\bsame node\b[^.]*prefill and decode", b), "prefill and decode on the SAME node"
    assert re.search(r"(?i)not[^.]*\b(run|measured|recorded)\b[^.]*p6-b200|p6-b200[^.]*\bnot\b[^.]*\b(run|measured|recorded)\b", b), "not run on p6-b200"
    assert re.search(r"(?i)\bEFA\b[^.]*\b(between|across|other) nodes", b), "the EFA device stays for traffic between nodes"
    assert "vpc.amazonaws.com/efa" in b or "EFA device" in b


@pytest.mark.parametrize("folder,cell", CASES, ids=IDS)
def test_readme_says_to_delete_one_before_applying_the_other_naming_namespace_and_dgd(folder: str, cell: str) -> None:
    flat = re.sub(r"\s+", " ", _readme(folder))
    m = re.search(r"(?i)[^.]*\bdelete\b[^.]*\b(before|first)\b[^.]*\.|[^.]*\b(before|first)\b[^.]*\bdelete\b[^.]*\.", flat)
    assert m, "a sentence says to delete one option before applying the other"
    sentence_zone = flat[max(0, m.start() - 200):m.end() + 200]
    assert f"`{E.NAMESPACES[folder]}`" in sentence_zone and "q38" in sentence_zone, "the warning names the namespace and the DGD q38"


@pytest.mark.parametrize("folder,cell", CASES, ids=IDS)
def test_readme_tells_how_to_check_the_transport_that_was_really_used(folder: str, cell: str) -> None:
    text = _readme(folder)
    a, b = _option(text, "Option A - EFA"), _option(text, "Option B - NVLink")
    assert "LIBFABRIC" in a and re.search(r"(?i)instantiated", a), "EFA: the engines log that LIBFABRIC was instantiated"
    assert "cuda_ipc" in b and re.search(r"(?i)protocol table", b), "NVLink: the UCX protocol table lists cuda_ipc"
    assert "kubectl" in a and "logs" in a and "kubectl" in b and "logs" in b, "each check is a kubectl logs command"
    flat = re.sub(r"\s+", " ", text)
    assert re.search(r"(?i)silent[^.]*TCP|TCP[^.]*silent|fall(s)? back to TCP", flat), "the warning about the silent TCP fallback"
    assert not re.search(r"\d\s*GB/s", flat), "the figures live in the PD guide: an example README quotes no measured number"


@pytest.mark.parametrize("folder,cell", CASES, ids=IDS)
def test_readme_links_the_guides_that_hold_the_evidence_and_the_links_resolve(folder: str, cell: str) -> None:
    p = EX / folder / "README.md"
    text = p.read_text()
    assert "docs/optimizations/pd-disaggregation/README.md" in text or "optimizations/pd-disaggregation/README.md" in text, "links the PD guide"
    assert "TROUBLESHOOTING.md" in text, "links TROUBLESHOOTING"
    for m in re.finditer(r"\]\((?!https?://|mailto:)([^)\s]*)\)", text):
        target, _, frag = m.group(1).partition("#")
        dest = (p.parent / target).resolve() if target else p
        assert dest.exists(), f"broken link {m.group(1)}"
        if frag and dest.suffix == ".md":
            heads = [_slug(h) for h in re.findall(r"(?m)^#{1,6} (.+)$", dest.read_text(encoding="utf-8"))]
            assert frag in heads, f"broken anchor {m.group(1)}"


@pytest.mark.parametrize("folder,cell", CASES, ids=IDS)
def test_readme_has_a_small_comparison_table_of_the_two_options(folder: str, cell: str) -> None:
    rows = [ln for ln in _readme(folder).splitlines() if ln.lstrip().startswith("|")]
    assert len(rows) >= 4, "header, separator and at least two comparison rows"
    assert "EFA" in rows[0] and "NVLink" in rows[0], f"the table header names both options: {rows[0]!r}"
    assert len(rows) <= 12, "keep it small"


@pytest.mark.parametrize("folder,cell", CASES, ids=IDS)
def test_readme_is_ascii_short_and_makes_no_speed_claim(folder: str, cell: str) -> None:
    text = _readme(folder)
    assert text.isascii()
    assert 20 <= len(text.splitlines()) <= E.README_MAX_LINES["pd"]
    assert not SPEED_WORDS.search(text), SPEED_WORDS.findall(text)
    hits = [m.group(0) for m in E.UNIT_RESULT.finditer(text)]
    assert not hits, f"README looks like it quotes measured numbers: {hits}"
    flat = re.sub(r"\s+", " ", text)
    assert re.search(r"(?i)\bNVLink\b[^.]*\b(NVIDIA|GPU-to-GPU|GPU to GPU|interconnect)\b", flat), "the page says in a few words what NVLink is"
    assert re.search(r"(?i)\bElastic Fabric Adapter\b|\bEFA\b[^.]*\b(network|AWS)\b", flat), "the page says in a few words what EFA is"


# ----------------------------------------------------------------------------- the frozen surface
@pytest.mark.parametrize("rel,digest", F.FROZEN_TREES.items())
def test_published_directories_are_byte_identical(rel: str, digest: str) -> None:
    assert (L.REPO_ROOT / rel).is_dir(), rel
    assert F.tree_digest(rel) == digest, f"{rel} changed: the NVLink option must not touch it"


@pytest.mark.parametrize("folder,digest", F.FROZEN_EXAMPLES.items())
def test_top_level_files_of_the_published_example_folders_are_byte_identical(folder: str, digest: str) -> None:
    assert F.tree_digest(f"examples/{folder}", F.EXAMPLE_FILES, top_level_only=True) == digest, f"examples/{folder} changed"


@pytest.mark.parametrize("rel,digest", F.FROZEN_FILES.items())
def test_published_files_are_byte_identical(rel: str, digest: str) -> None:
    p = L.REPO_ROOT / rel
    assert p.is_file(), rel
    assert hashlib.sha256(p.read_bytes()).hexdigest() == digest, f"{rel} changed"


def test_no_cell_or_topology_was_added_and_the_published_render_directory_holds_s0_to_s7() -> None:
    assert sorted(p.name for p in (TQ / "cells").iterdir() if p.is_dir()) == [f"S{n}" for n in range(8)]
    assert sorted(p.name for p in (TQ / "topologies").iterdir() if p.is_dir()) == sorted(L.TOPOLOGIES)
    assert sorted(p.name for p in Q.RENDERED_DIR.iterdir() if p.name != ".DS_Store") == [f"S{n}.yaml" for n in range(8)]
