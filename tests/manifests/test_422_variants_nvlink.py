# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The NVLink alternative of the four prefill/decode cells: ``components/pd-nvlink``, ``variants-nvlink/`` and ``rendered-nvlink/``.

RED until implemented.  The published prefill/decode cells (S3, S5, S6, S7) move the KV cache between the prefill and the
decode pods over NIXL LIBFABRIC on EFA, and ``manifests/track-q-qwen38/rendered/S0..S7`` are the EFA renders behind the
published numbers.  Next to them, in a SEPARATE tree, the sample ships the same four cells with the same-node KV path over
NVLink (NIXL UCX, ``cuda_ipc``).  Both transports are selectable (apply ``rendered/S5.yaml`` for EFA, the default, or
``rendered-nvlink/S5.yaml`` for NVLink); the NVLink tree is a DESIGN REFERENCE: it was not run on p6-b200.

What this file pins:

* ``components/pd-nvlink`` is a thin ``Component`` (two files) that is applied after ``components/pd-efa``;
* ``variants-nvlink/{S3,S5,S6,S7}`` are kustomize overlays: the published cell plus the component, nothing else;
* ``rendered-nvlink/{S3,S5,S6,S7}.yaml`` are their committed renders (``kubectl kustomize``), and nothing else is in there;
* each NVLink render equals the matching ``rendered/S<n>.yaml`` except for the transport knobs (NIXL backend ``UCX``,
  ``UCX_TLS``/``UCX_PROTO_INFO``/``UCX_LOG_LEVEL``, ``hostPID: true`` on the prefill and decode pods), on the p6-b200 baseline
  (NodePool ``gpu-b200-cb``, ``trtllm_mha``, fp8 KV); the Frontend does not change;
* the EFA device request, ``FI_*``, ``privileged`` and the GPU-pin wrapper STAY: the NVLink arm that was measured on another
  shape kept one EFA device per pod, and EFA stays the path for the traffic between nodes;
* the two new directories carry a README that says which tree the matrix driver and the published numbers use;
* the published tree is untouched (``test_421_example_pd_nvlink.py`` holds the digests).

Nothing here weakens an expectation: a missing file is a failure, never a skip (only the CRD check needs the Dynamo checkout).
"""

from __future__ import annotations

import copy
import re
from pathlib import Path
from typing import Any

import lib_manifests as L
import lib_proc
import lib_trackq as Q
import pytest
import test_420_examples as E
import yaml

TQ = L.MANIFESTS / Q.TQ
COMPONENT = TQ / "components" / "pd-nvlink"
VARIANTS = TQ / "variants-nvlink"
RENDERED = TQ / "rendered-nvlink"
PUBLISHED = TQ / "rendered"
CELLS = ("S3", "S5", "S6", "S7")
NAMESPACE = {"S3": "s-dis", "S5": "s-dis", "S6": "s-dis-hic", "S7": "s-dis-hic"}
KV_ROUTER = {"S3": False, "S5": True, "S6": False, "S7": True}
HICACHE = {"S3": False, "S5": False, "S6": True, "S7": True}
POOL = "gpu-b200-cb"
ROLES = ("prefill", "decode")
UCX_ENV = {"UCX_TLS": "cuda_ipc,cuda_copy,tcp", "UCX_PROTO_INFO": "y", "UCX_LOG_LEVEL": "info"}
LABEL = re.compile(r"(?i)design reference|reference design")
NOT_RUN_P6 = re.compile(
    r"(?i)not[^.]*\b(run|measured|recorded|tested)\b[^.]*p6-b200|p6-b200[^.]*\bnot\b[^.]*\b(run|measured|recorded|tested)\b")
FORBIDDEN_SPEED = re.compile(r"(?i)speed-?up|faster|slower|beats|outperform|lower latency")


def _kustomize(path: Path) -> str:
    proc = lib_proc.run(["kubectl", "kustomize", str(path)], capture_output=True, text=True, timeout=120, check=False)
    assert proc.returncode == 0, f"{path}: {proc.stderr}"
    return proc.stdout


def _dgd_of(text: str) -> dict[str, Any]:
    docs = [d for d in yaml.safe_load_all(text) if d]
    assert [d["kind"] for d in docs] == ["DynamoGraphDeployment"], [d["kind"] for d in docs]
    return docs[0]


def _comps(dgd: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {c["name"]: c for c in dgd["spec"]["components"]}


def _main(comp: dict[str, Any]) -> dict[str, Any]:
    return next(c for c in comp["podTemplate"]["spec"]["containers"] if c["name"] == "main")


def _env(comp: dict[str, Any]) -> dict[str, str]:
    return {e["name"]: e.get("value", "") for e in _main(comp)["env"]}


def _nvl(cell: str) -> dict[str, Any]:
    p = RENDERED / f"{cell}.yaml"
    assert p.is_file(), f"manifests/track-q-qwen38/rendered-nvlink/{cell}.yaml missing"
    return _dgd_of(p.read_text())


def _efa(cell: str) -> dict[str, Any]:
    return _dgd_of((PUBLISHED / f"{cell}.yaml").read_text())


def _names(path: Path) -> list[str]:
    assert path.is_dir(), f"{path.relative_to(L.MANIFESTS)} missing"
    return sorted(p.name for p in path.iterdir() if p.name != ".DS_Store")


# ----------------------------------------------------------------------------- the component
def test_component_is_a_thin_layer_of_two_files() -> None:
    assert _names(COMPONENT) == ["kustomization.yaml", "patch.yaml"]
    k = yaml.safe_load((COMPONENT / "kustomization.yaml").read_text())
    assert k["kind"] == "Component"
    patch = (COMPONENT / "patch.yaml").read_text()
    for token in ("hostPID", "UCX_TLS", "UCX_PROTO_INFO", "UCX_LOG_LEVEL"):
        assert token in patch, f"patch.yaml lacks {token}"
    # The role check fails the render unless pd-efa (applied first) put the prefill and decode roles in place.
    assert "op: test" in patch and "prefill" in patch and "decode" in patch


def test_component_selects_the_backend_env_by_name_so_it_works_with_and_without_hicache() -> None:
    text = (COMPONENT / "kustomization.yaml").read_text()
    assert "SGLANG_DISAGGREGATION_NIXL_BACKEND" in text and "UCX" in text
    assert "replacements" in text, "the backend is switched by a replacement that selects the env var by name"


def test_component_header_says_what_it_is_and_what_it_needs() -> None:
    head = " ".join(ln.lstrip("# ") for ln in (COMPONENT / "kustomization.yaml").read_text().splitlines() if ln.startswith("#"))
    flat = re.sub(r"\s+", " ", head)
    assert LABEL.search(flat), "the header carries the design reference label"
    assert NOT_RUN_P6.search(flat), "the header says the recipe was not run on p6-b200"
    assert "hostPID" in flat and "privileged" in flat and re.search(r"(?i)(same|one|single) node", flat)
    assert "pd-efa" in flat, "the header says it is applied after pd-efa"


def test_component_text_carries_no_internal_vocabulary() -> None:
    text = "\n".join((COMPONENT / n).read_text() for n in ("kustomization.yaml", "patch.yaml"))
    for banned in (r"\bSPEC\b", r"p5e", r"transport_proof", r"PREREG", r"Guardian", r"kubecon", r"\bT1\b", r"\bJ[123]\b", r"\bG1[a-c]\b", r"\bS15\b"):
        assert not re.search(banned, text), f"internal wording {banned!r} in components/pd-nvlink"
    assert text.isascii()


# ----------------------------------------------------------------------------- the overlays
def test_variants_tree_holds_exactly_the_four_overlays_and_a_readme() -> None:
    assert _names(VARIANTS) == sorted([*CELLS, "README.md"])
    for cell in CELLS:
        assert _names(VARIANTS / cell) == ["kustomization.yaml"], cell


@pytest.mark.parametrize("cell", CELLS)
def test_overlay_is_the_published_cell_plus_the_nvlink_component_and_nothing_else(cell: str) -> None:
    k = yaml.safe_load((VARIANTS / cell / "kustomization.yaml").read_text())
    assert k["kind"] == "Kustomization"
    assert set(k) <= {"apiVersion", "kind", "resources", "components"}, f"{cell}: extra kustomize fields {sorted(k)}"
    assert [(VARIANTS / cell / r).resolve() for r in k["resources"]] == [(TQ / "cells" / cell).resolve()], k["resources"]
    assert [(VARIANTS / cell / c).resolve() for c in k["components"]] == [COMPONENT.resolve()], k["components"]


# ----------------------------------------------------------------------------- the committed renders
def test_rendered_tree_holds_exactly_the_four_renders_and_a_readme() -> None:
    assert _names(RENDERED) == sorted([*(f"{c}.yaml" for c in CELLS), "README.md"])


@pytest.mark.parametrize("cell", CELLS)
def test_committed_render_equals_kubectl_kustomize_of_its_overlay(cell: str) -> None:
    committed = RENDERED / f"{cell}.yaml"
    assert committed.is_file(), f"rendered-nvlink/{cell}.yaml missing"
    assert committed.read_text() == _kustomize(VARIANTS / cell), f"rendered-nvlink/{cell}.yaml drifted: run scripts/render-track-q-nvlink.sh"


@pytest.mark.parametrize("cell", CELLS)
def test_published_render_is_still_the_kustomize_output_of_its_cell(cell: str) -> None:
    """Adding the component changed nothing for the cells: the EFA render of record still equals the live build."""
    assert (PUBLISHED / f"{cell}.yaml").read_text() == _kustomize(TQ / "cells" / cell)


@pytest.mark.parametrize("cell", CELLS)
def test_one_dgd_in_the_namespace_of_the_cell(cell: str) -> None:
    text = (RENDERED / f"{cell}.yaml").read_text() if (RENDERED / f"{cell}.yaml").is_file() else ""
    assert text, f"rendered-nvlink/{cell}.yaml missing"
    dgd = _dgd_of(text)
    assert dgd["metadata"]["name"] == "q38" and dgd["metadata"]["namespace"] == NAMESPACE[cell]
    assert set(re.findall(r"(?m)^\s*namespace:\s*(\S+)\s*$", text)) == {NAMESPACE[cell]}
    assert list(_comps(dgd)) == ["Frontend", "prefill", "decode"]


# ----------------------------------------------------------------------------- the transport knobs, and only they
def _undo_transport_knobs(dgd: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(dgd)
    for comp in _comps(out).values():
        if comp["name"] not in ROLES:
            continue
        comp["podTemplate"]["spec"].pop("hostPID", None)
        env = _main(comp)["env"]
        env[:] = [e for e in env if e["name"] not in UCX_ENV]
        for e in env:
            if e["name"] == "SGLANG_DISAGGREGATION_NIXL_BACKEND":
                e["value"] = "LIBFABRIC"
    return out


@pytest.mark.parametrize("cell", CELLS)
def test_nvlink_render_equals_the_published_render_except_for_the_transport_knobs(cell: str) -> None:
    efa, nvl = _efa(cell), _nvl(cell)
    assert _undo_transport_knobs(nvl) == efa, f"{cell}: differs from rendered/{cell}.yaml by more than the transport knobs: " + ", ".join(
        Q.diff_paths(efa, _undo_transport_knobs(nvl)))


@pytest.mark.parametrize("cell", CELLS)
def test_diff_paths_against_the_published_render_are_exactly_the_transport_knobs(cell: str) -> None:
    paths = Q.diff_paths(_efa(cell), _nvl(cell))
    assert paths, f"{cell}: the NVLink render must differ from the published one"
    allowed = re.compile(r"^\.spec\.components\[(1|2)\]\.podTemplate\.spec(\.hostPID|\.containers\[0\]\.env)$")
    stray = [p for p in paths if not allowed.match(p)]
    assert not stray, f"{cell}: paths outside the transport knobs: {stray}"


@pytest.mark.parametrize("cell", CELLS)
@pytest.mark.parametrize("role", ROLES)
def test_roles_move_the_kv_over_ucx_cuda_ipc_with_host_pid(cell: str, role: str) -> None:
    comp = _comps(_nvl(cell))[role]
    env = _env(comp)
    assert env["SGLANG_DISAGGREGATION_NIXL_BACKEND"] == "UCX"
    for name, value in UCX_ENV.items():
        assert env.get(name) == value, f"{cell}/{role}: {name}"
    assert comp["podTemplate"]["spec"].get("hostPID") is True, f"{cell}/{role}: hostPID (UCX cuda_ipc needs one PID namespace)"
    assert _main(comp)["securityContext"]["privileged"] is True, f"{cell}/{role}: privileged"
    assert "LIBFABRIC" not in "".join(env.values()), f"{cell}/{role}: no LIBFABRIC backend left"
    script = str(_main(comp)["args"][0])
    assert "GPU pin" in script and "CUDA_VISIBLE_DEVICES" in script and env["EXPECTED_GPUS"] == "1", f"{cell}/{role}: GPU-pin wrapper kept"


@pytest.mark.parametrize("cell", CELLS)
def test_frontend_is_untouched_by_the_transport_change(cell: str) -> None:
    efa, nvl = _comps(_efa(cell))["Frontend"], _comps(_nvl(cell))["Frontend"]
    assert nvl == efa
    assert "hostPID" not in nvl["podTemplate"]["spec"] and not (set(_env(nvl)) & set(UCX_ENV))


@pytest.mark.parametrize("cell", CELLS)
@pytest.mark.parametrize("role", ROLES)
def test_roles_keep_the_efa_device_and_fi_env_for_traffic_between_nodes(cell: str, role: str) -> None:
    """The measured NVLink arm kept one EFA device per pod and the FI_* env; EFA stays the path between nodes."""
    comp = _comps(_nvl(cell))[role]
    res = _main(comp)["resources"]
    assert res["requests"]["vpc.amazonaws.com/efa"] in (1, "1") and res["limits"]["vpc.amazonaws.com/efa"] in (1, "1")
    env = _env(comp)
    assert env["FI_PROVIDER"] == "efa" and "LD_LIBRARY_PATH" in env


@pytest.mark.parametrize("cell", CELLS)
@pytest.mark.parametrize("role", ROLES)
def test_roles_sit_on_the_b200_pool_with_the_baseline_engine_flags(cell: str, role: str) -> None:
    comp = _comps(_nvl(cell))[role]
    text = yaml.safe_dump(comp["podTemplate"]["spec"])
    assert POOL in text and "gpu-h200" not in text, f"{cell}/{role}: NodePool {POOL}"
    args = " ".join(str(a) for a in _main(comp)["args"])
    assert "--attention-backend trtllm_mha" in args and "--kv-cache-dtype fp8_e4m3" in args
    assert f"--disaggregation-mode {role}" in args and "--disaggregation-transfer-backend nixl" in args
    assert comp["replicas"] == 4 and _main(comp)["resources"]["limits"]["nvidia.com/gpu"] in (1, "1")


@pytest.mark.parametrize("cell", CELLS)
def test_router_and_hicache_arms_of_the_cell_are_kept(cell: str) -> None:
    comps = _comps(_nvl(cell))
    assert (_env(comps["Frontend"]).get("DYN_ROUTER_MODE") == "kv") is KV_ROUTER[cell]
    has_hicache = "--enable-hierarchical-cache" in " ".join(str(a) for a in _main(comps["prefill"])["args"])
    assert has_hicache is HICACHE[cell]
    if HICACHE[cell]:
        assert _env(comps["prefill"])["SGLANG_HICACHE_NIXL_BACKEND_PLUGIN"] == "POSIX", "the HiCache tier keeps its own NIXL backend"


@pytest.mark.parametrize("cell", CELLS)
def test_nvlink_render_validates_against_the_dgd_crd(cell: str) -> None:
    L.require_dynamo_repo()
    errors = L.schema_errors(_nvl(cell))
    assert not errors, errors


# ----------------------------------------------------------------------------- images and literals
@pytest.mark.parametrize("cell", CELLS)
def test_renders_run_the_single_configured_image_and_carry_no_literal_account_region_or_bucket(cell: str) -> None:
    text = (RENDERED / f"{cell}.yaml").read_text() if (RENDERED / f"{cell}.yaml").is_file() else ""
    assert text, f"rendered-nvlink/{cell}.yaml missing"
    assert set(re.findall(r"(?m)^\s*image:\s*(\S+)\s*$", text)) == {Q.IMAGE}
    assert ":latest" not in text
    stripped = text.replace(Q.IMAGE, "<IMAGE>")
    assert not E.ACCOUNT_RE.search(stripped) and not E.REGION_RE.search(stripped) and not E.BUCKET_RE.search(stripped)
    assert not re.search(r"AKIA[0-9A-Z]{16}|aws_secret|BEGIN [A-Z ]*PRIVATE KEY|hf_[A-Za-z0-9]{20,}", text)


# ----------------------------------------------------------------------------- the published tree stays EFA
@pytest.mark.parametrize("cell", [f"S{n}" for n in range(8)])
def test_published_renders_know_nothing_about_the_nvlink_transport(cell: str) -> None:
    text = (PUBLISHED / f"{cell}.yaml").read_text()
    assert "hostPID" not in text and "UCX_" not in text and "cuda_ipc" not in text
    backend = re.search(r"SGLANG_DISAGGREGATION_NIXL_BACKEND\s+value: (\S+)", text)
    assert backend is None or backend.group(1) == "LIBFABRIC"


def test_the_matrix_driver_and_the_plans_know_nothing_about_the_nvlink_tree() -> None:
    for root in (L.REPO_ROOT / "tools" / "matrix_driver", L.REPO_ROOT / "plans"):
        for p in root.rglob("*"):
            if p.is_file() and "__pycache__" not in p.parts and p.suffix in {".py", ".md", ".json", ".yaml", ".toml", ".csv"}:
                text = p.read_text(errors="ignore")
                assert not re.search(r"rendered-nvlink|variants-nvlink|pd-nvlink", text), f"{p.relative_to(L.REPO_ROOT)} mentions the NVLink tree"


# ----------------------------------------------------------------------------- the README of each new directory
README_DIRS = {"variants-nvlink": VARIANTS, "rendered-nvlink": RENDERED}


def _readme(name: str) -> str:
    p = README_DIRS[name] / "README.md"
    assert p.is_file(), f"manifests/track-q-qwen38/{name}/README.md missing"
    return re.sub(r"\s+", " ", p.read_text())


@pytest.mark.parametrize("name", README_DIRS)
def test_directory_readme_says_which_tree_the_driver_and_the_published_numbers_use(name: str) -> None:
    flat = _readme(name)
    assert re.search(r"(?i)matrix driver", flat) and re.search(r"(?i)published (numbers|results|runs)", flat)
    assert "rendered/" in flat and re.search(r"\bEFA\b", flat)
    assert re.search(r"(?i)(driver|published)[^.]*\brendered/[^.]*\bEFA\b|\bEFA\b[^.]*\brendered/", flat), (
        "the sentence ties the matrix driver and the published numbers to rendered/ (EFA)")


@pytest.mark.parametrize("name", README_DIRS)
def test_directory_readme_labels_the_tree_a_design_reference_never_run_on_p6_b200(name: str) -> None:
    flat = _readme(name)
    assert LABEL.search(flat), "design reference label"
    assert NOT_RUN_P6.search(flat), "not run on p6-b200"
    assert "NVLink" in flat and "rendered-nvlink/" in flat


@pytest.mark.parametrize("name", README_DIRS)
def test_directory_readme_states_the_security_and_placement_limits(name: str) -> None:
    flat = _readme(name)
    assert "privileged" in flat and "hostPID" in flat
    assert re.search(r"(?i)(same|one|single) node", flat), "prefill and decode must share a node"
    assert re.search(r"(?i)\bEFA\b[^.]*\b(default|between nodes|across nodes|other nodes)", flat), "EFA is the default / the path between nodes"


@pytest.mark.parametrize("name", README_DIRS)
def test_directory_readme_is_short_ascii_without_literals_or_measured_numbers(name: str) -> None:
    text = (README_DIRS[name] / "README.md").read_text() if (README_DIRS[name] / "README.md").is_file() else ""
    assert text, f"{name}/README.md missing"
    assert 5 <= len(text.splitlines()) <= 40 and text.isascii()
    assert text.splitlines()[0].startswith("# ")
    assert not E.ACCOUNT_RE.search(text) and not E.REGION_RE.search(text)
    assert not E.UNIT_RESULT.search(text), f"looks like measured numbers: {[m.group(0) for m in E.UNIT_RESULT.finditer(text)]}"
    assert not FORBIDDEN_SPEED.search(text)


def test_rendered_readme_explains_how_to_pick_a_transport_and_to_delete_the_other_first() -> None:
    flat = _readme("rendered-nvlink")
    assert "rendered/S5.yaml" in flat and "rendered-nvlink/S5.yaml" in flat, "apply rendered/S5.yaml for EFA, rendered-nvlink/S5.yaml for NVLink"
    assert re.search(r"(?i)\bdelete\b", flat) and "q38" in flat, "both trees use the same namespace and DGD name: delete one before the other"
    assert re.search(r"(?i)\bEFA\b[^.]*\bdefault\b|\bdefault\b[^.]*\bEFA\b", flat)
