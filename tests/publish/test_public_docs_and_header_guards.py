# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Guards of the public docs, the file headers and the Terraform test mocks.

* ``test_public_docs_and_terraform_carry_no_lab_region_or_stack_names``: no name, short id or city name (any case) of the
  lab region in the READMEs, the docs and the whole of terraform/, its tests and their fixtures included (tfvars examples,
  variable descriptions, comments, the mocked ``.tftest.hcl`` values and the captured fixtures use us-east-2).  It skips
  ``ALLOWED_REGION_PREFIXES``, which the region guard of test_public_names_and_region_guards reads; ``tests/`` and
  ``tools/*/tests/`` are not read.  The patterns are built from fragments, so this file does not carry them.
* ``test_no_public_file_names_the_excluded_internal_tree``: no exported file, test code included, names the docs tree
  that the export leaves behind (``INTERNAL_TREE_TERMS``, built from fragments).
* ``test_every_path_and_script_named_in_a_public_doc_exists``: same path/script checks as test_docs_set, over every
  public Markdown file.
* ``test_hf_token_docs_say_to_export_it_in_the_shell``: the token is exported in the shell (or comes from
  hf_token_secret_arn); manifests/config/platform.env is generated and overwritten.
* ``test_dashboard_namespaces_agree``: the s-* namespaces of the dashboards README, the topologies and
  terraform/cluster/namespaces.tf are the same set.
* ``test_every_py_sh_tf_file_has_the_spdx_header`` (+ ``test_spdx_skip_list_*``): header position, shebang, encoding
  line, ``from __future__`` placement and one-header-only.
* ``test_readme_and_deploy_share_the_single_step_runtime_image_flow``.
* ``test_scanner_allows_exactly_the_code_of_conduct_address``: the address is allowed by the tree scanner by exact
  match only.
* ``test_tftest_files_mock_the_aliased_us_east_1_aws_provider`` and ``test_terraform_test_runs_without_credentials``
  (+ ``test_conftest_has_no_unused_noqa``).

The e-mail address of the standard code-of-conduct sentence (``ALLOWED_CODE_OF_CONDUCT_ADDRESS``) is the one exemption.
"""

from __future__ import annotations

import glob
import json
import os
import re
import shutil
from pathlib import Path

import lib_proc
import pytest
import sanitize_scan
import test_docs_set as ds

ROOT = sanitize_scan.REPO_ROOT

# ----------------------------------------------------------------------------------------------------------------- scope

README_TESTS = "tests/README.md"


def _is_private_test(rel: str) -> bool:
    if rel == README_TESTS:
        return False
    return rel.startswith("tests/") or bool(re.match(r"tools/[^/]+/tests/", rel))


def _public_text_files() -> list[tuple[str, str]]:
    out = []
    for rel in sanitize_scan.public_files():
        if _is_private_test(rel):
            continue
        text = sanitize_scan.read_text(rel)
        if text is not None:
            out.append((rel, text))
    return out


def _markdown_files() -> list[str]:
    return [r for r in sanitize_scan.public_files() if r.endswith(".md") and not r.startswith("tests/") or r == README_TESTS]


# Built from fragments so this file does not carry the literals it forbids.
REGION_NAMES: list[tuple[str, str]] = [
    ("ap-" + "south", "lab region name"),
    ("ap" + "s1", "short region id"),
    (r"(?i)mum" + "bai", "lab city name (any case)"),
]
ALLOWED_REGION_PREFIXES = ("manifests/", "examples/", "tests/publish/fixtures/", "tools/", "scripts/", "dashboards/")


def _hits(text: str, patterns: list[tuple[str, str]]) -> list[str]:
    found = []
    for pat, what in patterns:
        for m in re.finditer(pat, text):
            found.append(f"{text.count(chr(10), 0, m.start()) + 1}: {what} {m.group(0)!r}")
    return found


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


# ------------------------------------------------------------------------------------------- the excluded internal tree
# Built from fragments so this file does not carry what it forbids: the folder name as a word (``docs/<name>/``, a
# relative link ``../<name>/``) and the two-word name of that tree, in any case.
INTERNAL_TREE_TERMS = re.compile(r"(?i:\bs" + r"dd\b|design[ -]" + r"log)")


def test_no_public_file_names_the_excluded_internal_tree() -> None:
    bad = {}
    for rel in sanitize_scan.public_files():
        text = sanitize_scan.read_text(rel) or ""
        hits = [f"{text.count(chr(10), 0, m.start()) + 1}: {m.group(0)!r}" for m in INTERNAL_TREE_TERMS.finditer(text)]
        if hits:
            bad[rel] = hits
    shown = "\n".join(f"  {p} ({len(h)}): {h[:3]}" for p, h in sorted(bad.items()))
    assert not bad, f"{len(bad)} exported file(s) name the internal tree the export leaves behind:\n{shown}"


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


# -------------------------------------------------------------------------------------------------------------- HF_TOKEN

@pytest.mark.parametrize("p", [ds.DEPLOY, ds.TROUBLE], ids=lambda p: p.name)
def test_hf_token_docs_say_to_export_it_in_the_shell(p: Path) -> None:
    text = p.read_text(encoding="utf-8")
    assert re.search(r"export HF_TOKEN=", text), f"{p.name}: show `export HF_TOKEN=...` (scripts/stage-model.sh reads the environment)"
    if p == ds.DEPLOY:
        assert "hf_token_secret_arn" in text
        assert re.search(r"(?i)platform\.env[^.\n]*(generated|overwritten)|(generated|overwritten)[^.\n]*platform\.env", text), (
            "DEPLOY must say platform.env is generated and overwritten, so the token cannot live there")


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


# ----------------------------------------------------------------------------------------------------------- dashboards

def test_dashboard_namespaces_agree() -> None:
    readme = (ROOT / "dashboards/README.md").read_text(encoding="utf-8")
    row = next(ln for ln in readme.splitlines() if ln.startswith("| `namespace`"))
    documented = set(re.findall(r"`(s-[a-z-]+)`", row))
    topo = {m.group(1) for p in (ROOT / "examples").glob("*/manifest.yaml")
            for m in re.finditer(r"^  namespace: (s-[a-z-]+)$", p.read_text(), re.MULTILINE)}
    created = set(re.findall(r'^\s*"(s-[a-z-]+)"\s*=', (ROOT / "terraform/cluster/namespaces.tf").read_text(), re.MULTILINE))
    assert topo, "no example namespaces found"
    assert documented == topo == created, (
        f"dashboards README {sorted(documented)} | examples {sorted(topo)} | namespaces.tf {sorted(created)}")


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
