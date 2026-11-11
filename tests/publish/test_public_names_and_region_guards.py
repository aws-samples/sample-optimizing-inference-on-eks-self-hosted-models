# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Guards of the project name, the region of the docs and the env contract.

* ``test_project_name_*``, ``test_part_of_label_*``: the part-of label equals the Terraform default ``project_name``.
* ``test_docs_agree_on_one_region``, ``test_example_config_*region*``, ``test_committed_renders_*``: the placeholder config
  and the example manifests use the region of the docs (the ``aws_region`` of the tfvars examples) and the placeholder
  account.
* ``test_no_original_region_in_public_config_renders_goldens_or_docs``: no exported file, test code included, carries
  the code or the short id of the lab region, in its text or its path (``LAB_REGION``, built from fragments so that no
  file is exempt).
* ``test_gen_env_keeps_hf_token_forbidden``.
* ``test_dashboards_tests_skip_without_a_dynamo_checkout``.
* ``test_no_unused_noqa_in_tracked_python``.
"""

from __future__ import annotations

import importlib.util
import os
import re
import shutil
import sys
from pathlib import Path

import lib_proc
import pytest
import sanitize_scan

ROOT = sanitize_scan.REPO_ROOT


def _files() -> list[str]:
    return sanitize_scan.public_files()


def _text(rel: str) -> str:
    return sanitize_scan.read_text(rel) or ""


def _project_name() -> str:
    text = (ROOT / "terraform/cluster/variables.tf").read_text()
    m = re.search(r'variable "project_name"\s*\{[^}]*?default\s*=\s*"([^"]+)"', text, re.DOTALL)
    assert m, "terraform/cluster/variables.tf: project_name default"
    return m.group(1)


def _doc_region() -> str:
    text = (ROOT / "terraform/cluster/terraform.tfvars.example").read_text()
    m = re.search(r'(?m)^aws_region\s*=\s*"([a-z0-9-]+)"', text)
    assert m, "terraform/cluster/terraform.tfvars.example: aws_region"
    return m.group(1)


def _region_code(region: str) -> str:
    a, b, c = region.split("-")
    return f"{a}{b[0]}{c}"  # us-east-2 -> use2 (the prefix of the AZ IDs)


def _platform_example() -> dict[str, str]:
    out = {}
    for line in (ROOT / "manifests/config/platform.env.example").read_text().splitlines():
        if line.strip() and not line.startswith("#"):
            k, _, v = line.partition("=")
            out[k] = v
    return out


# ------------------------------------------------------------------------------------------------ names
def test_project_name_default_is_the_same_in_both_stacks() -> None:
    boot = (ROOT / "terraform/bootstrap/variables.tf").read_text()
    m = re.search(r'variable "project_name"\s*\{[^}]*?default\s*=\s*"([^"]+)"', boot, re.DOTALL)
    assert m and m.group(1) == _project_name() == "dynamo-sample"


PART_OF_FILES = (
    "manifests/node-ops/job-model-prefetch.yaml", "manifests/node-ops/pod-ephemeral-shell.yaml", "scripts/10-stage-model.sh",
)


def test_part_of_label_equals_the_project_name_everywhere() -> None:
    """The node-ops/staging objects carry the Terraform ``project_name`` default as part-of (the cells keep
    their own ``qwen38-matrix`` label, which names a workload, not the project)."""
    project = _project_name()
    bad = {}
    for rel in PART_OF_FILES:
        vals = re.findall(r"app\.kubernetes\.io/part-of:\s*([^\s#]+)", _text(rel))
        assert vals, f"{rel}: no part-of label"
        wrong = sorted({v for v in vals if v.strip("'\"") != project})
        if wrong:
            bad[rel] = wrong
    assert not bad, f"part-of must be '{project}' (the Terraform project_name default): {bad}"


# ------------------------------------------------------------------------------------------------ region
def test_docs_agree_on_one_region() -> None:
    region = _doc_region()
    boot = re.search(r'(?m)^aws_region\s*=\s*"([^"]+)"', (ROOT / "terraform/bootstrap/terraform.tfvars.example").read_text())
    assert boot and boot.group(1) == region
    # terraform/README.md links the minimum tfvars of docs/DEPLOY.md instead of repeating them: any region it quotes agrees.
    readme = re.findall(r'aws_region\s*=\s*"([^"]+)"', (ROOT / "terraform/README.md").read_text())
    assert set(readme) <= {region}, readme
    assert f"cluster `dynamo-sample-{_region_code(region)}`" in (ROOT / "terraform/README.md").read_text() or \
        f"dynamo-sample-{_region_code(region)}" in (ROOT / "terraform/README.md").read_text()
    assert region in (ROOT / "README.md").read_text()


def test_example_config_region_equals_the_docs_region() -> None:
    region, code, project = _doc_region(), _region_code(_doc_region()), _project_name()
    cfg = _platform_example()
    account = sanitize_scan.PLACEHOLDER_ACCOUNT
    assert cfg["AWS_ACCOUNT_ID"] == account, "the placeholder account stays (a real account id is still forbidden)"
    assert cfg["AWS_REGION"] == region
    assert cfg["CLUSTER_NAME"] == f"{project}-{code}"
    for key in ("GPU_AZ", "PRIMARY_AZ"):
        assert cfg[key].startswith(region), (key, cfg[key])
    for key, kind in (("MODEL_BUCKET", "models"), ("RESULTS_BUCKET", "results")):  # terraform/bootstrap/main.tf names
        assert cfg[key] == f"{project}-{kind}-{account}-{code}", (key, cfg[key])
    assert cfg["MODEL_S3_URI"].startswith(f"s3://{cfg['MODEL_BUCKET']}/")


def test_committed_renders_and_diffs_use_the_docs_region() -> None:
    region = _doc_region()
    pins = [r for r in _files() if re.fullmatch(r"examples/[^/]+/manifest\.yaml", r)]
    assert len(pins) == 8, pins
    bad = {}
    for rel in pins:
        regions = set(re.findall(r"\.dkr\.ecr\.([a-z0-9-]+)\.amazonaws\.com", _text(rel)))
        if regions - {region}:
            bad[rel] = sorted(regions)
    assert not bad, f"committed renders must be rendered from the example config of region {region}: {bad}"
    # The runtime image is public (no ECR host), so a manifest.yaml may carry no ECR host at all. What is asserted
    # instead: every ECR host that does appear is the docs region (checked above), and
    # no manifest.yaml may be left with an ECR host of another region or a real account.
    for rel in (r for r in pins if r.endswith("manifest.yaml")):
        hosts = re.findall(r"([\w-]+)\.dkr\.ecr\.([a-z0-9-]+)\.amazonaws\.com", _text(rel))
        assert all(acct == sanitize_scan.PLACEHOLDER_ACCOUNT and reg == region for acct, reg in hosts), (rel, hosts)


LAB_REGION = r"ap-" + r"south-1|\bap" + r"s1\b|ap" + r"s1(?![A-Za-z0-9])"  # built from fragments


def _hits(pattern: str, rel: str, text: str) -> list[str]:
    out = [f"path: {m.group(0)!r}" for m in re.finditer(pattern, rel)]
    out += [f"{text.count(chr(10), 0, m.start()) + 1}: {m.group(0)!r}" for m in re.finditer(pattern, text)]
    return out


def test_no_original_region_in_public_config_renders_goldens_or_docs() -> None:
    bad = {}
    for rel in _files():
        hits = _hits(LAB_REGION, rel, _text(rel))
        if hits:
            bad[rel] = hits
    shown = "\n".join(f"  {p} ({len(h)}): {h[:2]}" for p, h in sorted(bad.items())[:60])
    assert not bad, f"{len(bad)} file(s) carry the lab region (no file is exempt):\n{shown}"


def test_region_guard_of_test_95_still_forbids_the_deployment_values() -> None:
    """test_95 forbids any region literal and any short code outside platform.env(.example): the docs region and its
    short code are in that forbidden set, so the guard protects the placeholder deployment values."""
    region, code = _doc_region(), _region_code(_doc_region())
    src = (ROOT / "tests/manifests/test_95_region_config.py").read_text()
    assert "REGION_RE" in src and "SHORT_RE" in src
    short = re.search(r"SHORT_RE\s*=\s*re\.compile\(r\"([^\"]+)\"\)", src)
    assert short and re.search(short.group(1), code), f"SHORT_RE must match the short code {code}"
    assert re.search(r"(?<![A-Za-z0-9])(?:us|eu|ap)-(?:north|south|east|west)", region)
    leaks = []
    for root in (ROOT / "manifests", ROOT / "scripts"):
        for p in sorted(root.rglob("*")):
            if not p.is_file() or p.name == "platform.env" or "__pycache__" in p.parts or ".DS_Store" in p.name:
                continue
            rel = p.relative_to(ROOT).as_posix()
            if rel.endswith("platform.env.example"):
                continue
            try:
                text = p.read_text()
            except UnicodeDecodeError:
                continue
            if re.search(rf"(?<![A-Za-z0-9]){re.escape(region)}(?![A-Za-z0-9])|(?<![A-Za-z0-9]){code}(?![A-Za-z0-9])", text):
                leaks.append(rel)
    assert not leaks, f"the deployment region/short code leaks outside the config: {leaks}"


# ------------------------------------------------------------------------------------------------ HF_TOKEN
def test_gen_env_keeps_hf_token_forbidden() -> None:
    spec = importlib.util.spec_from_file_location("gen_env_hf_token_check", ROOT / "scripts/gen_env.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["gen_env_hf_token_check"] = mod
    spec.loader.exec_module(mod)
    assert "HF_TOKEN" in mod.FORBIDDEN_KEYS
    assert "HF_TOKEN" not in (ROOT / "manifests/config/platform.env.example").read_text().replace("# Never in this file: HF_TOKEN", "")


# ------------------------------------------------------------------------------------------------ dashboards
def test_dashboards_tests_skip_without_a_dynamo_checkout(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The three tests that read tag v1.6.1 (test_allowlist, test_vendored x2) go through ``_lib.git_show``."""
    monkeypatch.setenv("DYNAMO_REPO", str(tmp_path / "no-dynamo-here"))
    spec = importlib.util.spec_from_file_location("dash_lib_skip_check", ROOT / "tests/dashboards/_lib.py")
    assert spec and spec.loader
    lib = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(lib)
    with pytest.raises(pytest.skip.Exception) as excinfo:
        lib.git_show(lib.CATALOG_PATH)
    assert "DYNAMO_REPO" in str(excinfo.value), "the skip reason names DYNAMO_REPO"


# ------------------------------------------------------------------------------------------------ ruff
def test_no_unused_noqa_in_tracked_python() -> None:
    ruff = shutil.which("ruff")
    if ruff is None:
        pytest.skip("ruff not installed")
    files = [r for r in _files() if r.endswith(".py")]
    proc = lib_proc.run(
        [ruff, "check", "--no-cache", "--extend-select", "RUF100", "--output-format", "concise", *files],
        capture_output=True, text=True, check=False, cwd=ROOT, env={**os.environ, "RUFF_NO_CACHE": "1"},
    )
    unused = [ln for ln in proc.stdout.splitlines() if "RUF100" in ln]
    assert not unused, "unused noqa directives:\n" + "\n".join(unused)
