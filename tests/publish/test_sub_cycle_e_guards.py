# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Sub-cycle E guards (RED until the publish cleanup of the public docs, headers and tftest mocks lands).

Covers the final qa and reviewer findings of the publish cleanup (finding numbers in brackets):

* [1][2][5][12] ``test_public_files_carry_no_internal_vocabulary``: no GLM, Track A/B/F, decision ids (D-nn), lab, Mumbai,
  s-lab, kubecon-artifacts, EXPLORATORY, NVFP4/DTP and "Block 1" in any tracked public file.  [2][12]
  ``test_public_docs_and_terraform_carry_no_lab_region_or_stack_names``: no ap-south-1 / aps1 / Mumbai (any case) in the
  READMEs, the docs and the whole of terraform/, its tests and their fixtures included (tfvars examples, variable
  descriptions, comments, the mocked ``.tftest.hcl`` values and the captured fixtures use us-east-2).
* [4][5][12] ``test_every_path_and_script_named_in_a_public_doc_exists``: same path/script checks as test_docs_set, over every
  tracked Markdown file outside docs/sdd.  ``test_tests_readme_is_short_and_true`` pins the tests/README.md content.
* [3] ``test_hf_token_docs_*`` and ``test_gen_env_comment_*``: the token is exported in the shell (or comes from
  hf_token_secret_arn); config/sample.env is generated and overwritten, so no document may tell the reader to edit it.
* [6] ``test_dashboard_namespaces_agree``: the s-* namespaces of the dashboards README, the topologies, the rerender helper
  and terraform/cluster/namespaces.tf are the same set.
* [7] ``test_every_py_sh_tf_file_has_the_spdx_header`` (+ ``test_spdx_skip_list_*``): header position, shebang, encoding
  line, ``from __future__`` placement and one-header-only.
* [8] ``test_readme_and_deploy_share_the_single_step_runtime_image_flow``.
* [9] ``test_scanner_*`` / ``test_contributing_*``: the code-of-conduct address is allowed by the tree scanner by exact
  match only, and CONTRIBUTING.md carries the standard sentence.
* [11] ``test_tftest_files_mock_the_aliased_us_east_1_aws_provider`` and ``test_terraform_test_runs_without_credentials``
  (+ ``test_conftest_has_no_unused_noqa``).

Allowlist (documented, nothing else is exempt)
==============================================
* ``docs/sdd/`` is the append-only design log (excluded from the public export, byte-identical, never edited).
* ``tests/`` and ``tools/*/tests/`` are excluded from the vocabulary guard: several of them are themselves the guards and
  name the removed things in their deny lists (tests/README.md is NOT excluded).  They are reported as a remainder.
* the region guard here reads the Markdown files and terraform/ and skips ``ALLOWED_REGION_PREFIXES`` (manifests,
  examples, tools, workloads, plans, scripts, dashboards, tests/publish/fixtures), which the original-region guard of
  sub-cycle F reads; ``terraform/*/tests/`` (tftest mocks and fixtures) is NOT exempt.
* the e-mail address of the standard code-of-conduct sentence (see ``ALLOWED_CODE_OF_CONDUCT_ADDRESS``).
* ``docs/static/architecture.drawio``, reason "diagram shipped as drawn": exactly the hits pinned in ``AS_DRAWN_HITS``
  (by guard label and matched text, count included), honoured by the vocabulary guard here and by the vocabulary and
  original-region guards of sub-cycle F; test_static_architecture.py checks the pin both ways and the sha256 of the file.
  The PNG export is binary, so the text guards never read it and it needs no entry.
"""

from __future__ import annotations

import glob
import json
import os
import re
import shutil
from collections import Counter
from pathlib import Path

import lib_proc
import pytest
import sanitize_scan
import test_docs_set as ds

ROOT = sanitize_scan.REPO_ROOT

# ----------------------------------------------------------------------------------------------------------------- scope

PRIVATE_TEST_TREES = ("tests/",)  # plus tools/*/tests/ (see _is_private_test)
README_TESTS = "tests/README.md"


def _is_private_test(rel: str) -> bool:
    if rel == README_TESTS:
        return False
    return rel.startswith("tests/") or bool(re.match(r"tools/[^/]+/tests/", rel))


def _public_text_files() -> list[tuple[str, str]]:
    out = []
    for rel in sanitize_scan.tracked_files():
        if rel.startswith(sanitize_scan.ALLOWLIST_PREFIX) or _is_private_test(rel):
            continue
        text = sanitize_scan.read_text(rel)
        if text is not None:
            out.append((rel, text))
    return out


def _markdown_files() -> list[str]:
    return [r for r in sanitize_scan.tracked_files()
            if r.endswith(".md") and not r.startswith(sanitize_scan.ALLOWLIST_PREFIX) and not r.startswith("tests/")
            or r == README_TESTS]


# Built from fragments so this file does not carry the literals it forbids.
VOCABULARY: list[tuple[str, str]] = [
    (r"(?i)" + "g" + "lm", "GLM model name"),
    (r"\bTracks? [ABF]\b", "internal track name"),
    (r"\bD-\d{1,3}\b", "decision id"),
    (r"\b[Ll]" + "ab\\b", "lab identity"),
    ("Mum" + "bai", "Mumbai"),
    ("s-" + "lab", "s-lab namespace"),
    ("kubecon" + "-artifacts", "internal artifacts path"),
    (r"(?i)explor" + "atory", "EXPLORATORY label"),
    (r"NV" + r"FP4|\bDTP\d\b", "GLM-5.3 deployment detail"),
    (r"\bBlock 1\b", "Block 1 (GLM-5.3) reference section"),
    ("lab-terraform" + "-snapshot|terraform-ap-" + "south", "lab stack tag or directory"),
]
REGION_NAMES: list[tuple[str, str]] = [
    ("ap-" + "south", "ap-south region"),
    ("ap" + "s1", "aps1 identifier"),
    (r"(?i)mum" + "bai", "Mumbai (any case)"),
]
ALLOWED_REGION_PREFIXES = ("manifests/", "examples/", "tests/publish/fixtures/", "tools/", "workloads/", "plans/",
                           "scripts/", "dashboards/")


def _hits(text: str, patterns: list[tuple[str, str]]) -> list[str]:
    found = []
    for pat, what in patterns:
        for m in re.finditer(pat, text):
            found.append(f"{text.count(chr(10), 0, m.start()) + 1}: {what} {m.group(0)!r}")
    return found


# ------------------------------------------------------------------------------------- diagram shipped as drawn
# Reason: diagram shipped as drawn.  ALL the guard hits it may carry: (guard label, matched text) -> count (fragments).
AS_DRAWN_HITS: dict[str, Counter[tuple[str, str]]] = {
    "docs/static/architecture.drawio": Counter({
        ("GLM model name", "Gl" + "M"): 3,  # base64 letters in the 3 embedded copies of the SGLang logo SVG, not a word
    }),
}


def as_drawn(rel: str, text: str, patterns: list[tuple[str, str]]) -> bool:
    """True when ``rel`` is in ``AS_DRAWN_HITS`` and its hits of ``patterns`` are exactly the pinned ones for those labels.

    Args:
        rel: Tracked path relative to the repository root.
        text: Content of ``rel``.
        patterns: ``(regex, label)`` pairs of the calling guard.

    Returns:
        False for any other file, or when a hit was added or a pinned one is gone (then the guard reports the file).
    """
    pinned = AS_DRAWN_HITS.get(rel)
    if pinned is None:
        return False
    labels = {what for _, what in patterns}
    found = Counter((what, m.group(0)) for pat, what in patterns for m in re.finditer(pat, text))
    return found == Counter({key: n for key, n in pinned.items() if key[0] in labels})


def test_public_files_carry_no_internal_vocabulary() -> None:
    bad = {}
    for rel, text in _public_text_files():
        hits = _hits(text, VOCABULARY)
        if hits and not as_drawn(rel, text, VOCABULARY):
            bad[rel] = hits
    shown = "\n".join(f"  {p} ({len(h)}): {h[:2]}" for p, h in sorted(bad.items())[:80])
    assert not bad, f"{len(bad)} public file(s) name internal things (docs/sdd and test trees are exempt):\n{shown}"


def test_public_docs_and_terraform_carry_no_lab_region_or_stack_names() -> None:
    bad = {}
    for rel, text in _public_text_files():
        if rel.startswith(ALLOWED_REGION_PREFIXES):
            continue
        if not (rel.endswith(".md") or rel.startswith("terraform/")):
            continue
        hits = _hits(text, REGION_NAMES)
        if hits:
            bad[rel] = hits
    shown = "\n".join(f"  {p} ({len(h)}): {h[:2]}" for p, h in sorted(bad.items())[:60])
    assert not bad, f"{len(bad)} file(s) name the lab region or stack (use us-east-2 or a placeholder):\n{shown}"


# ----------------------------------------------------------------------------------------- paths and scripts in the docs

@pytest.mark.parametrize("rel", sorted(_markdown_files()))
def test_every_path_and_script_named_in_a_public_doc_exists(rel: str) -> None:
    text = (ROOT / rel).read_text(encoding="utf-8")
    text = re.sub(r"[\w./-]*[<{$][^\s`)]*", "", text)
    bad = []
    for tok in sorted(ds._clean_candidates(text)):
        if tok in ds.GENERATED or tok.rstrip("/") in ds.GENERATED:
            continue
        here = (ROOT / rel).parent  # a README may also name paths relative to its own folder
        if "*" in tok:
            if not glob.glob(str(ROOT / tok)) and not glob.glob(str(here / tok)):
                bad.append(tok)
        elif not (ROOT / tok).exists() and not (here / tok).exists():
            bad.append(tok)
    names = {m.group(1) for m in re.finditer(r"`([\w.-]+\.(?:sh|py))(?:\s[^`]*)?`", text)}
    have = {q.name for q in ROOT.rglob("*") if q.is_file() and ".git" not in q.parts and "__pycache__" not in q.parts}
    bad += sorted(n for n in names if n not in have)
    assert not bad, f"{rel} names paths or scripts that do not exist: {bad}"


def test_tests_readme_is_short_and_true() -> None:
    text = (ROOT / README_TESTS).read_text(encoding="utf-8")
    assert len(text.splitlines()) <= 90, "tests/README.md must be short"
    for stale in ("track-a-glm", "20-topology-up", "30-run.sh", "99-run-matrix", "s-" + "lab", "GL" + "M"):
        assert stale not in text, f"tests/README.md still mentions {stale}"
    for suite in ("manifests", "scripts", "terraform", "publish", "dashboards"):
        assert f"tests/{suite}" in text, f"tests/README.md does not describe tests/{suite}"
    assert "DYNAMO_REPO" in text, "document the tests skipped without a Dynamo checkout (DYNAMO_REPO)"
    assert re.search(r"platform\.env\.example", text), "document the placeholder env (platform.env.example)"
    assert re.search(r">=\s*1\.12", text), "terraform test needs Terraform >= 1.12"
    assert re.search(r"(?i)no (AWS )?credentials|without (AWS )?credentials", text), "terraform test needs no credentials"
    assert re.search(r"(?i)one (suite|at a time)|not in parallel|sequential", text), "suites run one at a time (shared platform.env)"


# -------------------------------------------------------------------------------------------------------------- HF_TOKEN

HF_DOCS = [ds.README, ds.DEPLOY, ds.TROUBLE]


@pytest.mark.parametrize("p", HF_DOCS, ids=lambda p: p.name)
def test_hf_token_docs_do_not_tell_to_edit_sample_env(p: Path) -> None:
    text = p.read_text(encoding="utf-8")
    bad = []
    for sentence in re.split(r"(?<=[.!?])\s+|\n\s*\n|\|", text):
        flat = " ".join(sentence.split())
        if "sample.env" in flat and "HF_TOKEN" in flat and not re.search(r"(?i)\b(never|not|overwrit|do n[o']t)", flat):
            bad.append(flat[:140])
    assert not bad, f"{p.name} tells the reader to put HF_TOKEN in the generated config/sample.env: {bad}"


@pytest.mark.parametrize("p", [ds.DEPLOY, ds.TROUBLE], ids=lambda p: p.name)
def test_hf_token_docs_say_to_export_it_in_the_shell(p: Path) -> None:
    text = p.read_text(encoding="utf-8")
    assert re.search(r"export HF_TOKEN=", text), f"{p.name}: show `export HF_TOKEN=...` (scripts/10-stage-model.sh reads the environment)"
    if p == ds.DEPLOY:
        assert "hf_token_secret_arn" in text
        assert re.search(r"(?i)sample\.env[^.\n]*(generated|overwritten)|(generated|overwritten)[^.\n]*sample\.env", text), (
            "DEPLOY must say sample.env is generated and overwritten, so the token cannot live there")


def test_gen_env_comment_does_not_point_at_an_env_file_for_the_token() -> None:
    text = (ROOT / "scripts/gen_env.py").read_text(encoding="utf-8")
    m = re.search(r"``HF_TOKEN`` is never.*?\n\n", text, re.DOTALL)
    assert m, "the HF_TOKEN paragraph of the gen_env.py docstring is gone"
    para = " ".join(m.group(0).split())
    assert not re.search(r"(?i)untracked env", para), "the token does not live in an env file: gen_env.py overwrites them"
    assert re.search(r"(?i)exported|shell|process environment|hf_token_secret_arn", para), "say where the token does live"


# ------------------------------------------------------------------------------------------- README and DEPLOY two-step flow

def test_readme_and_deploy_share_the_single_step_runtime_image_flow() -> None:
    readme = ds.README.read_text(encoding="utf-8")
    deploy = ds.DEPLOY.read_text(encoding="utf-8")
    quick = ds._section(readme, "Quick start")
    assert quick, "README has no Quick start section"
    assert re.search(r"runtime_image", quick), "quick start: runtime_image is set in the tfvars before the one cluster apply"
    assert not re.search(r"(?i)(second apply|apply[^.\n]*again|two[- ]step|\bempty\b)", quick), "the cluster applies once"
    assert not re.search(r'runtime_image\s*=\s*""', deploy) and not re.search(r"(?i)apply[^.\n]*again", deploy)
    for out in ("platform_env", "sample_env", "gpu_pools"):  # gen_env.REQUIRED_OUTPUTS
        assert out in deploy, f"DEPLOY must list the output {out} that gen_env.py requires"


# ------------------------------------------------------------------------------------------------ code-of-conduct address

ALLOWED_CODE_OF_CONDUCT_ADDRESS = "opensource-codeofconduct" + "@amazon.com"


def test_scanner_allows_exactly_the_code_of_conduct_address() -> None:
    assert sanitize_scan.scan_text(f"contact {ALLOWED_CODE_OF_CONDUCT_ADDRESS} with any questions") == []
    for other in (f"x{ALLOWED_CODE_OF_CONDUCT_ADDRESS}", "security" + "@amazon.com", "opensource-codeofconduct@" + "corp." + "amazon.com",
                  ALLOWED_CODE_OF_CONDUCT_ADDRESS + ".evil.com"):
        assert any(c == "email" for c, _ in sanitize_scan.scan_text(f"mail {other}")), f"{other} must still be flagged"


def test_contributing_carries_the_standard_code_of_conduct_sentence() -> None:
    text = " ".join(ds.CONTRIB.read_text(encoding="utf-8").split())
    assert ALLOWED_CODE_OF_CONDUCT_ADDRESS in text
    assert re.search(r"(?i)code of conduct faq(?:\]\(https://aws\.github\.io/code-of-conduct-faq\))? or contact " + re.escape(ALLOWED_CODE_OF_CONDUCT_ADDRESS) + r" with any additional questions or comments", text)


# ----------------------------------------------------------------------------------------------------------- dashboards

def test_dashboard_namespaces_agree() -> None:
    readme = (ROOT / "dashboards/README.md").read_text(encoding="utf-8")
    row = next(ln for ln in readme.splitlines() if ln.startswith("| `namespace`"))
    documented = set(re.findall(r"`(s-[a-z-]+)`", row))
    topo = {m.group(1) for p in (ROOT / "manifests/track-q-qwen38/topologies").glob("*/kustomization.yaml")
            for m in re.finditer(r"^namespace: (s-[a-z-]+)$", p.read_text(), re.MULTILINE)}
    created = set(re.findall(r'^\s*"(s-[a-z-]+)"\s*=', (ROOT / "terraform/cluster/namespaces.tf").read_text(), re.MULTILINE))
    rerender = set(re.findall(r'"(s-[a-z-]+)"', next(ln for ln in (ROOT / "manifests/demo/rerender/rerender.py").read_text().splitlines()
                                                    if ln.startswith("NAMESPACES"))))
    assert topo, "no topology namespaces found"
    assert documented == topo == created == rerender, (
        f"dashboards README {sorted(documented)} | topologies {sorted(topo)} | namespaces.tf {sorted(created)} | rerender {sorted(rerender)}")


# -------------------------------------------------------------------------------------------------------------- SPDX

SPDX_LINE = "# SPDX-License-Identifier: MIT-0"
COPYRIGHT_LINE = "# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved."
SPDX_SKIP_FILE = ROOT / "tests/publish/spdx_skip.txt"  # optional: "<path>\t<reason>" per line; '#' comments; vendored/generated only


def _spdx_skips() -> dict[str, str]:
    if not SPDX_SKIP_FILE.is_file():
        return {}
    out = {}
    for ln in SPDX_SKIP_FILE.read_text(encoding="utf-8").splitlines():
        if ln.strip() and not ln.startswith("#"):
            path, _, reason = ln.partition("\t")
            out[path.strip()] = reason.strip()
    return out


def _spdx_candidates() -> list[str]:
    return sorted(r for r in sanitize_scan.tracked_files() if r.endswith((".py", ".sh", ".tf")))


def _header_problem(rel: str) -> str | None:
    data = (ROOT / rel).read_bytes()
    if not data.strip():
        return None
    lines = data.decode("utf-8").splitlines()
    head = lines[:6]
    if SPDX_LINE not in head:
        return "no SPDX line in the first 6 lines"
    i = head.index(SPDX_LINE)
    if i == 0 or head[i - 1] != COPYRIGHT_LINE:
        return "the SPDX line is not directly preceded by the copyright line"
    for n, ln in enumerate(lines[: i - 1]):
        if not ((n == 0 and ln.startswith("#!")) or re.match(r"#.*coding[:=]\s*[-\w.]+", ln)
                or re.match(r"# shellcheck ", ln)):
            return f"line {n + 1} before the header is neither a shebang (line 1), an encoding line nor a shellcheck directive"
    if sum(ln == SPDX_LINE for ln in lines) != 1:
        return "SPDX line appears more than once"
    if any(ln.startswith("#!") for ln in lines[1:]):
        return "shebang is not on line 1"
    if rel.endswith(".py"):
        try:
            compile(data, rel, "exec", dont_inherit=True)
        except SyntaxError as exc:
            return f"no longer compiles: {exc}"
    return None


def test_spdx_skip_list_is_documented_and_not_stale() -> None:
    tracked = set(_spdx_candidates())
    bad = []
    for path, reason in _spdx_skips().items():
        if path not in tracked:
            bad.append(f"{path}: not a tracked .py/.sh/.tf file")
        elif len(reason) < 10 or not re.search(r"(?i)vendored|generated|third[- ]party", reason):
            bad.append(f"{path}: reason must say vendored or generated")
        elif _header_problem(path) is None and SPDX_LINE in (ROOT / path).read_text().splitlines()[:6]:
            bad.append(f"{path}: has the header already, drop it from the skip list")
    assert not bad, bad


def test_every_py_sh_tf_file_has_the_spdx_header() -> None:
    skips = _spdx_skips()
    bad = {}
    for rel in _spdx_candidates():
        if rel in skips:
            continue
        problem = _header_problem(rel)
        if problem:
            bad[rel] = problem
    shown = "\n".join(f"  {p}: {m}" for p, m in sorted(bad.items())[:40])
    assert not bad, f"{len(bad)} tracked .py/.sh/.tf file(s) lack a valid MIT-0 header:\n{shown}"


def test_shell_scripts_still_parse_after_the_header() -> None:
    bad = []
    for rel in _spdx_candidates():
        if rel.endswith(".sh"):
            proc = lib_proc.run(["bash", "-n", str(ROOT / rel)], capture_output=True, text=True, check=False)
            if proc.returncode:
                bad.append(f"{rel}: {proc.stderr.strip()[:100]}")
    assert not bad, bad


# ---------------------------------------------------------------------------------------------------------- terraform tests

TFTEST_DIR = ROOT / "terraform/cluster/tests"


def test_tftest_files_mock_the_aliased_us_east_1_aws_provider() -> None:
    """Root-module tftest files mock the default aws provider; the aliased one (grafana-edge.tf) needs its own mock."""
    bad = []
    for p in sorted(TFTEST_DIR.glob("*.tftest.hcl")):
        text = p.read_text(encoding="utf-8")
        mocks_default = re.search(r'^mock_provider "aws" \{', text, re.MULTILINE)
        mocks_alias = re.search(r'^mock_provider "aws" \{[^}]*alias\s*=\s*"us_east_1"', text, re.MULTILINE | re.DOTALL)
        if mocks_default and not mocks_alias:
            bad.append(p.name)
    assert not bad, f"{bad} mock the default aws provider but not aws.us_east_1 (terraform/cluster/grafana-edge.tf:74)"


def _terraform_bin() -> str | None:
    cand = os.environ.get("TERRAFORM_BIN") or shutil.which("terraform")
    if not cand:
        return None
    proc = lib_proc.run([cand, "version", "-json"], capture_output=True, text=True, check=False)
    try:
        major, minor = (int(x) for x in json.loads(proc.stdout)["terraform_version"].split(".")[:2])
    except (ValueError, KeyError):
        return None
    return cand if (major, minor) >= (1, 12) else None


@pytest.mark.slow
def test_terraform_test_runs_without_credentials(tmp_path: Path) -> None:
    tf = _terraform_bin()
    if tf is None:
        pytest.skip("needs Terraform >= 1.12 (set TERRAFORM_BIN)")
    repo = tmp_path / "repo"
    repo.mkdir()
    # tests the working tree, not HEAD: overlay the tracked terraform files and the dashboards the module reads
    for sub in ("terraform", "dashboards"):
        shutil.copytree(ROOT / sub, repo / sub, dirs_exist_ok=True, ignore=shutil.ignore_patterns(".terraform", "*.tfstate*"))
    env = {"HOME": str(tmp_path), "PATH": "/usr/bin:/bin", "AWS_EC2_METADATA_DISABLED": "true",
           "AWS_SHARED_CREDENTIALS_FILE": os.devnull, "AWS_CONFIG_FILE": os.devnull, "TF_IN_AUTOMATION": "1"}
    cluster = repo / "terraform/cluster"
    init = lib_proc.run([tf, f"-chdir={cluster}", "init", "-backend=false", "-input=false", "-no-color"],
                        capture_output=True, text=True, check=False, env=env)
    if init.returncode != 0:
        pytest.skip(f"terraform init needs the provider registry (offline?): {init.stderr[-200:]}")
    proc = lib_proc.run([tf, f"-chdir={cluster}", "test", "-no-color"], capture_output=True, text=True, check=False, env=env)
    assert proc.returncode == 0, proc.stdout[-3500:] + proc.stderr[-1500:]


def test_conftest_has_no_unused_noqa() -> None:
    ruff = shutil.which("ruff")
    if ruff is None:
        pytest.skip("ruff not installed")
    proc = lib_proc.run([ruff, "check", "--isolated", "--no-cache", "--select", "RUF100", str(ROOT / "tests/conftest.py")],
                        capture_output=True, text=True, check=False)
    assert proc.returncode == 0, proc.stdout[-1500:]
