# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""``examples/08-pd-nvlink``: the prefill/decode recipe with the same-node KV path over NVLink.  RED until implemented.

``examples/03-pd-disaggregation`` (cell S3) moves the KV cache between the prefill and the decode pods over NIXL LIBFABRIC on
EFA.  ``examples/08-pd-nvlink`` is the same recipe with the KV path of the prefill and decode pods of ONE node over NVLink
(NIXL UCX, ``cuda_ipc``).  It is a design reference: it is the NVLink alternative of cell S3, it is not one of the recorded
runs and it was not run on p6-b200.  The published cells and renders stay on EFA; the NVLink material lives in a separate
tree (``variants-nvlink/``, ``rendered-nvlink/``, see ``test_422_variants_nvlink.py``).  What this file pins:

* the folder holds exactly ``kustomization.yaml``, ``rendered.yaml``, ``diff-vs-baseline.diff`` and ``README.md``; the
  kustomization has one resource, ``manifests/track-q-qwen38/variants-nvlink/S3`` (no new cell, no new topology);
* the render equals ``kubectl kustomize`` of the folder, the output of ``scripts/render-examples.sh`` and
  ``rendered-nvlink/S3.yaml``;
* the example sits on the p6-b200 baseline of the sample (pool ``gpu-b200-cb``, ``trtllm_mha``, fp8 KV, the pinned runtime
  image) and equals ``examples/03-pd-disaggregation`` except for the transport knobs: the NIXL backend ``UCX`` instead of
  ``LIBFABRIC``, ``UCX_TLS=cuda_ipc,cuda_copy,tcp`` (+ ``UCX_PROTO_INFO``, ``UCX_LOG_LEVEL``) and ``hostPID: true`` on the
  prefill and decode pods.  The EFA device request, ``FI_*`` and the GPU-pin wrapper stay: the NVLink arm that was measured
  (one EFA device per pod) kept them, and EFA stays for the traffic between nodes;
* the published surface is byte-identical (``lib_frozen``): ``rendered/S0..S7``, the goldens, the example folders 00 to 07,
  the driver, the plans and ``components/pd-efa``.

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

FOLDER = E.EXTRA
EX = E.EX
D = EX / FOLDER
TQ = L.MANIFESTS / Q.TQ
VARIANT = TQ / "variants-nvlink" / "S3"
RENDERED_NVL = TQ / "rendered-nvlink"
BASE_FOLDER = "03-pd-disaggregation"
NAMESPACE = "s-dis"
POOL = "gpu-b200-cb"

UCX_ENV = {"UCX_TLS": "cuda_ipc,cuda_copy,tcp", "UCX_PROTO_INFO": "y", "UCX_LOG_LEVEL": "info"}
ROLES = ("prefill", "decode")

LABEL = re.compile(r"(?i)design reference|reference design")


def _read(name: str) -> str:
    p = D / name
    assert p.is_file(), f"missing examples/{FOLDER}/{name}"
    return p.read_text()


def _dgd(folder: str) -> dict[str, Any]:
    docs = [d for d in yaml.safe_load_all((EX / folder / "rendered.yaml").read_text()) if d]
    assert len(docs) == 1, f"{folder}: one document"
    return docs[0]


def _components(dgd: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {c["name"]: c for c in dgd["spec"]["components"]}


def _main(comp: dict[str, Any]) -> dict[str, Any]:
    return next(c for c in comp["podTemplate"]["spec"]["containers"] if c["name"] == "main")


def _env(comp: dict[str, Any]) -> dict[str, str]:
    return {e["name"]: e.get("value", "") for e in _main(comp)["env"]}


# ----------------------------------------------------------------------------- layout
def test_folder_holds_exactly_its_four_files() -> None:
    assert D.is_dir(), f"examples/{FOLDER} missing"
    assert sorted(p.name for p in D.iterdir() if p.name != ".DS_Store") == sorted(E.FILES)


def test_kustomization_has_one_resource_the_s3_nvlink_variant() -> None:
    k = yaml.safe_load(_read("kustomization.yaml"))
    assert k["kind"] == "Kustomization"
    assert set(k) <= {"apiVersion", "kind", "resources"}, f"extra kustomize fields: {sorted(k)}"
    assert [(D / r).resolve() for r in k["resources"]] == [VARIANT.resolve()], k["resources"]


def test_kustomization_says_it_is_a_design_reference_not_run_on_p6_b200() -> None:
    head = " ".join(ln.lstrip("# ") for ln in _read("kustomization.yaml").splitlines() if ln.startswith("#"))
    assert LABEL.search(head), "the header carries the design reference label"
    assert re.search(r"(?i)not[^.]*\b(run|measured|recorded)\b[^.]*p6-b200|p6-b200[^.]*\bnot\b[^.]*\b(run|measured|recorded)\b", head)
    assert "rendered-nvlink" in head or "variants-nvlink" in head, "the header points at the NVLink tree"


# ----------------------------------------------------------------------------- rendered.yaml and the script
def test_rendered_equals_kubectl_kustomize_of_the_folder() -> None:
    assert _read("rendered.yaml") == E._kustomize(FOLDER), f"examples/{FOLDER}/rendered.yaml drifted: run scripts/render-examples.sh"


def test_rendered_is_the_s3_render_of_the_nvlink_tree_byte_for_byte() -> None:
    committed = RENDERED_NVL / "S3.yaml"
    assert committed.is_file(), "manifests/track-q-qwen38/rendered-nvlink/S3.yaml missing"
    assert _read("rendered.yaml") == committed.read_text(), "examples/08 and rendered-nvlink/S3.yaml are the same render"


def test_render_script_reproduces_the_nvlink_files(tmp_path: Path) -> None:
    out = tmp_path / "out"
    proc = lib_proc.run([str(E.SCRIPT), str(out)], capture_output=True, text=True, timeout=300, cwd=tmp_path, check=False)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert sorted(p.name for p in (out / FOLDER).iterdir()) == ["diff-vs-baseline.diff", "rendered.yaml"]
    for name in ("rendered.yaml", "diff-vs-baseline.diff"):
        assert (out / FOLDER / name).read_bytes() == (D / name).read_bytes(), f"{FOLDER}/{name} drifted"


def test_nvlink_render_is_not_a_published_render() -> None:
    """It has no cell and its render is not in ``rendered/``: that directory still holds exactly S0..S7 (the EFA runs of record)."""
    assert sorted(p.name for p in Q.RENDERED_DIR.iterdir() if p.name != ".DS_Store") == [f"S{n}.yaml" for n in range(8)]
    assert sorted(p.name for p in (TQ / "cells").iterdir() if p.name != ".DS_Store") == [f"S{n}" for n in range(8)]


def test_one_dgd_lands_in_the_s_dis_namespace_as_q38() -> None:
    text = _read("rendered.yaml")
    docs = [d for d in yaml.safe_load_all(text) if d]
    assert [d["kind"] for d in docs] == ["DynamoGraphDeployment"]
    assert docs[0]["metadata"]["name"] == "q38" and docs[0]["metadata"]["namespace"] == NAMESPACE
    assert set(re.findall(r"(?m)^\s*namespace:\s*(\S+)\s*$", text)) == {NAMESPACE}


def test_rendered_runs_the_configured_runtime_image_and_nothing_is_latest() -> None:
    text = _read("rendered.yaml")
    image = next(ln.split("=", 1)[1].strip().strip("\"'") for ln in Q.CONFIG_ENV.read_text().splitlines() if ln.startswith("RUNTIME_IMAGE="))
    images = set(re.findall(r"(?m)^\s*image:\s*(\S+)\s*$", text))
    assert images == {image}, images
    assert ":latest" not in text


def test_no_literal_account_region_bucket_or_secret_in_the_rendered_outputs() -> None:
    image = next(ln.split("=", 1)[1].strip().strip("\"'") for ln in Q.CONFIG_ENV.read_text().splitlines() if ln.startswith("RUNTIME_IMAGE="))
    for name in ("rendered.yaml", "diff-vs-baseline.diff"):
        text = _read(name).replace(image, "<IMAGE>")
        assert not E.ACCOUNT_RE.search(text), f"{name}: account id outside the configured image"
        assert not E.REGION_RE.search(text), f"{name}: region literal outside the configured image"
        assert not E.BUCKET_RE.search(text)
    for p in D.iterdir():
        assert not re.search(r"AKIA[0-9A-Z]{16}|aws_secret|BEGIN [A-Z ]*PRIVATE KEY|hf_[A-Za-z0-9]{20,}", p.read_text()), p


# ----------------------------------------------------------------------------- built on the p6-b200 baseline
@pytest.mark.parametrize("role", ROLES)
def test_roles_sit_on_the_b200_pool_with_the_baseline_engine_flags(role: str) -> None:
    comps = _components(_dgd(FOLDER))
    assert list(comps) == ["Frontend", "prefill", "decode"]
    comp = comps[role]
    assert comp["replicas"] == 4
    spec = comp["podTemplate"]["spec"]
    text = yaml.safe_dump(spec)
    assert POOL in text, f"{role}: NodePool {POOL}"
    assert "gpu-h200" not in text, f"{role}: no H200 pool name"
    args = " ".join(str(a) for a in _main(comp)["args"])
    assert "--attention-backend trtllm_mha" in args, f"{role}: the B200 attention backend"
    assert "--kv-cache-dtype fp8_e4m3" in args
    assert f"--disaggregation-mode {role}" in args and "--disaggregation-transfer-backend nixl" in args
    assert _main(comp)["resources"]["limits"]["nvidia.com/gpu"] in (1, "1")


@pytest.mark.parametrize("role", ROLES)
def test_roles_move_the_kv_over_ucx_cuda_ipc_with_host_pid(role: str) -> None:
    comp = _components(_dgd(FOLDER))[role]
    env = _env(comp)
    assert env["SGLANG_DISAGGREGATION_NIXL_BACKEND"] == "UCX"
    for name, value in UCX_ENV.items():
        assert env.get(name) == value, f"{role}: {name}"
    spec = comp["podTemplate"]["spec"]
    assert spec.get("hostPID") is True, f"{role}: hostPID (UCX cuda_ipc needs one PID namespace)"
    assert _main(comp)["securityContext"]["privileged"] is True, f"{role}: privileged"
    assert "LIBFABRIC" not in "".join(env.values()), f"{role}: no LIBFABRIC backend left"
    # The pod keeps the GPU-pin wrapper of pd-efa (a privileged pod sees every GPU of the node).
    script = str(_main(comp)["args"][0])
    assert "GPU pin" in script and "CUDA_VISIBLE_DEVICES" in script and env["EXPECTED_GPUS"] == "1"


@pytest.mark.parametrize("role", ROLES)
def test_roles_keep_the_efa_device_for_traffic_between_nodes(role: str) -> None:
    """The measured NVLink arm kept one EFA device per pod (and the FI_* env); EFA stays for the traffic between nodes."""
    comp = _components(_dgd(FOLDER))[role]
    res = _main(comp)["resources"]
    assert res["requests"]["vpc.amazonaws.com/efa"] in (1, "1") and res["limits"]["vpc.amazonaws.com/efa"] in (1, "1")
    assert _env(comp)["FI_PROVIDER"] == "efa"


def test_nvlink_example_equals_the_efa_example_except_for_the_transport_knobs() -> None:
    """Undo the transport knobs on the NVLink render: what is left must be the 03 render, key for key."""
    efa = _dgd(BASE_FOLDER)
    nvl = copy.deepcopy(_dgd(FOLDER))
    for comp in _components(nvl).values():
        if comp["name"] not in ROLES:
            continue
        comp["podTemplate"]["spec"].pop("hostPID", None)
        env = _main(comp)["env"]
        env[:] = [e for e in env if e["name"] not in UCX_ENV]
        for e in env:
            if e["name"] == "SGLANG_DISAGGREGATION_NIXL_BACKEND":
                e["value"] = "LIBFABRIC"
    assert nvl == efa, "08 differs from 03 by more than the transport knobs: " + ", ".join(Q.diff_paths(efa, nvl))


def test_diff_against_the_efa_example_is_exactly_the_transport_knobs() -> None:
    efa, nvl = _dgd(BASE_FOLDER), _dgd(FOLDER)
    paths = Q.diff_paths(efa, nvl)
    assert paths, "08 must differ from 03"
    # env lists change length (3 env vars added) and the pod spec gains hostPID: nothing else moves.
    allowed = re.compile(r"^\.spec\.components\[(1|2)\]\.podTemplate\.spec(\.hostPID|\.containers\[0\]\.env)$")
    stray = [p for p in paths if not allowed.match(p)]
    assert not stray, f"paths outside the transport knobs: {stray}"


# ----------------------------------------------------------------------------- diff-vs-baseline.diff
def test_diff_names_baseline_and_variant_and_shows_the_transport_knobs() -> None:
    body = _read("diff-vs-baseline.diff")
    assert E._diff_lines(FOLDER), "diff has no changed lines"
    first = body.splitlines()
    assert "00-baseline" in first[0] and FOLDER in first[1]
    headers = [ln for ln in first if E.HEADER_RE.match(ln)]
    assert [E.HEADER_RE.match(h).group(1) for h in headers] == ["metadata", "Worker", "Worker"]  # type: ignore[union-attr]
    for token in ("SGLANG_DISAGGREGATION_NIXL_BACKEND", "UCX", "UCX_TLS", "cuda_ipc", "hostPID", "vpc.amazonaws.com/efa", "privileged"):
        assert token in body, f"diff lacks {token}"
    assert "s-dis" in body


def test_diff_stays_short_enough_to_read() -> None:
    assert len(_read("diff-vs-baseline.diff").splitlines()) <= 150


# ----------------------------------------------------------------------------- the README of the example
README_FORBIDDEN = re.compile(r"speed-?up|faster|slower|throughput of|latency of|reduces? (?:ttft|latency)|beats|outperform", re.I)


def test_readme_length_language_and_sections() -> None:
    text = _read("README.md")
    assert 5 <= len(text.splitlines()) <= 30, f"{len(text.splitlines())} lines"
    heads = [ln for ln in text.splitlines() if ln.startswith("#")]
    assert heads and heads[0].startswith("# ") and FOLDER in heads[0]
    for section in E.README_REQUIRED:
        assert any(section.lower() in h.lower() and h.startswith("##") for h in heads), f"missing '## {section}'"
    assert text.isascii()


def test_readme_names_the_namespace_the_apply_command_and_a_dashboard() -> None:
    text = _read("README.md")
    assert f"`{NAMESPACE}`" in text
    assert f"kubectl apply -k examples/{FOLDER}" in text
    assert "kubectl delete" not in text
    assert any(d in text for d in ("dashboards/demo/take.json", "dashboards/demo/compare.json", "dashboards/sglang"))
    for ref in re.findall(r"dashboards/[\w./-]+", text):
        assert (L.REPO_ROOT / ref.rstrip(".,)")).exists(), f"README cites non-existent {ref}"


def test_readme_has_no_measured_results_and_no_speed_claim() -> None:
    text = _read("README.md")
    hits = [m.group(0) for m in E.UNIT_RESULT.finditer(text)]
    assert not hits, f"README looks like it quotes measured numbers: {hits}"
    assert not README_FORBIDDEN.search(text)


def test_readme_labels_the_example_as_a_design_reference_never_measured_on_p6() -> None:
    text = _read("README.md")
    flat = re.sub(r"\s+", " ", text)
    assert LABEL.search(flat)
    assert re.search(r"(?i)not[^.]*\b(run|measured|recorded)\b[^.]*p6-b200|p6-b200[^.]*\bnot\b[^.]*\b(run|measured|recorded)\b", flat), (
        "the README says the recipe was not run on p6-b200"
    )
    assert re.search(r"(?i)\brendered/|Track Q cell|recorded runs", flat), "the README says it is not one of the recorded runs / cells"
    assert re.search(r"(?i)\bNVLink\b", flat) and "UCX" in flat and "cuda_ipc" in flat


def test_readme_states_the_security_and_placement_limits() -> None:
    flat = re.sub(r"\s+", " ", _read("README.md"))
    assert "hostPID" in flat and "privileged" in flat and "Pod Security" in flat
    assert re.search(r"(?i)(same|one|single) node", flat), "works within one node only"
    assert re.search(r"(?i)\bEFA\b[^.]*\b(between|across|other) nodes", flat), "EFA stays for the traffic between nodes"


def test_readme_says_efa_stays_the_default_and_names_the_nvlink_tree() -> None:
    flat = re.sub(r"\s+", " ", _read("README.md"))
    assert re.search(r"(?i)\bEFA\b[^.]*\bdefault\b|\bdefault\b[^.]*\bEFA\b", flat), "EFA remains the default transport"
    assert "rendered-nvlink/S3.yaml" in flat or "variants-nvlink/S3" in flat, "names the NVLink variant this example is built on"


def test_readme_says_to_delete_the_other_pd_cell_first_and_how_to_compare_with_the_efa_example() -> None:
    text = _read("README.md")
    assert re.search(r"(?i)\bdelete\b", text)
    assert BASE_FOLDER in text, "names the EFA example, which shares the s-dis DGD name"
    assert "06-kv-router-pd" in text or "S5" in text, "names the other folder that shares the s-dis DGD name"
    assert f"diff examples/{BASE_FOLDER}/rendered.yaml examples/{FOLDER}/rendered.yaml" in text


def test_every_flag_and_env_named_in_the_readme_appears_in_the_diff() -> None:
    text, body = _read("README.md"), _read("diff-vs-baseline.diff")
    named = set(E.FLAG_RE.findall(text)) | {e for e in E.ENV_RE.findall(text) if "_" in e}
    named -= {"README_MD"}
    assert {"SGLANG_DISAGGREGATION_NIXL_BACKEND", "UCX_TLS"} <= named, f"the README must name the transport knobs: {sorted(named)}"
    missing = sorted(n for n in named if n not in body)
    assert not missing, f"named in the README but absent from the diff: {missing}"


# ----------------------------------------------------------------------------- examples/README.md and DEPLOY.md
def test_examples_readme_lists_the_nvlink_example_outside_the_cell_table() -> None:
    text = (EX / "README.md").read_text()
    assert len(text.splitlines()) <= 60 and text.isascii()
    assert f"examples/{FOLDER}" in text or f"`{FOLDER}`" in text
    rows = [ln for ln in text.splitlines() if ln.lstrip().startswith("|")]
    assert len([r for r in rows if re.search(r"\|\s*S\d\s*\|", r)]) == 8, "the cell table keeps its eight S rows"
    mention = [ln for ln in text.splitlines() if FOLDER in ln]
    assert mention and not any(re.search(r"\|\s*S\d\s*\|", ln) for ln in mention)
    flat = re.sub(r"\s+", " ", text)
    assert LABEL.search(flat) and re.search(r"(?i)rendered-nvlink|not (a|one of the) (Track Q )?cell|no render", flat)


def test_deploy_examples_table_keeps_its_eight_cell_rows() -> None:
    sec = E._deploy_section_6()
    rows = [ln for ln in sec.splitlines() if ln.lstrip().startswith("|") and "examples/" in ln]
    assert len(rows) == 8, f"{len(rows)} rows"


# ----------------------------------------------------------------------------- the frozen surface
@pytest.mark.parametrize("rel,digest", F.FROZEN_TREES.items())
def test_published_directories_are_byte_identical(rel: str, digest: str) -> None:
    assert (L.REPO_ROOT / rel).is_dir(), rel
    assert F.tree_digest(rel) == digest, f"{rel} changed: the NVLink alternative must not touch it"


@pytest.mark.parametrize("folder,digest", F.FROZEN_EXAMPLES.items())
def test_published_example_folders_are_byte_identical(folder: str, digest: str) -> None:
    assert F.tree_digest(f"examples/{folder}", F.EXAMPLE_FILES) == digest, f"examples/{folder} changed"


@pytest.mark.parametrize("rel,digest", F.FROZEN_FILES.items())
def test_published_files_are_byte_identical(rel: str, digest: str) -> None:
    p = L.REPO_ROOT / rel
    assert p.is_file(), rel
    assert hashlib.sha256(p.read_bytes()).hexdigest() == digest, f"{rel} changed: the NVLink alternative must not touch it"


def test_no_cell_or_topology_was_added_and_the_published_render_directory_holds_s0_to_s7() -> None:
    assert sorted(p.name for p in (TQ / "cells").iterdir() if p.is_dir()) == [f"S{n}" for n in range(8)]
    assert sorted(p.name for p in (TQ / "topologies").iterdir() if p.is_dir()) == sorted(L.TOPOLOGIES)
    assert sorted(p.name for p in Q.RENDERED_DIR.iterdir() if p.name != ".DS_Store") == [f"S{n}.yaml" for n in range(8)]
