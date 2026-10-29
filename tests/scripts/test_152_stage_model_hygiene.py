# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""RED tests: hygiene of every file the model staging Job adds (no mutable image tag, no credential-looking string).

Scope: ``scripts/10-stage-model.sh``, ``tools/stage_model/`` (code, requirements, tests), the model-stager Terraform test, the
env templates the feature touches, and the new test files themselves. The tokens used by the tests are dummies built at run
time from fragments, so no token-shaped literal exists in any file.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import lib_scripts as S
import pytest

REPO = S.REPO_ROOT
CODE = [
    REPO / "scripts" / "10-stage-model.sh",
    REPO / "tools" / "stage_model" / "stage_model.py",
    REPO / "tools" / "stage_model" / "requirements.txt",
]
TOUCHED = [
    REPO / "scripts" / "gen_env.py",
    REPO / "scripts" / "11-prefetch.sh",
    REPO / "manifests" / "config" / "platform.env.example",
    REPO / "terraform" / "cluster" / "iam-model-access.tf",
    REPO / "terraform" / "cluster" / "variables.tf",
    REPO / "terraform" / "cluster" / "outputs.tf",
    REPO / "terraform" / "cluster" / "terraform.tfvars.example",
    REPO / "terraform" / "cluster" / "tests" / "model_stager.tftest.hcl",
]
TESTS = [
    *sorted((REPO / "tools" / "stage_model" / "tests").glob("*.py")),
    *sorted((REPO / "tests" / "scripts").glob("*stage_model*.py")),
    REPO / "tests" / "scripts" / "test_150_stage_model_env.py",
    REPO / "tests" / "scripts" / "test_152_stage_model_hygiene.py",
    REPO / "tests" / "terraform" / "test_model_stager_static.py",
]
CREDENTIAL = {
    "hugging face token": re.compile(r"\bhf_[A-Za-z0-9]{20,}"),
    "aws access key id": re.compile(r"\b(AKIA|ASIA)[0-9A-Z]{16}\b"),
    "github token": re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}"),
    "private key": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    "aws secret key assignment": re.compile(r"(?i)aws_secret_access_key\s*[=:]\s*[A-Za-z0-9/+]{30,}"),
    "bearer literal": re.compile(r"(?i)bearer\s+[A-Za-z0-9._-]{20,}"),
}
# HF_TOKEN= followed by a literal value (placeholders, empty values, shell expansions and printf formats are fine)
LITERAL_TOKEN_ASSIGNMENT = re.compile(r"HF_TOKEN=(?![\s$<{%\"'`]|$)[A-Za-z0-9_.-]{8,}")


def existing(paths: list[Path]) -> list[Path]:
    return [p for p in paths if p.is_file()]


@pytest.mark.parametrize("path", CODE, ids=lambda p: str(p.relative_to(REPO)))
def test_the_deliverables_exist(path: Path) -> None:
    assert path.is_file(), f"{path.relative_to(REPO)} must exist"


@pytest.mark.parametrize("path", existing(CODE + TOUCHED + TESTS), ids=lambda p: str(p.relative_to(REPO)))
def test_no_credential_shaped_string(path: Path) -> None:
    text = path.read_text(errors="replace")
    for what, rx in CREDENTIAL.items():
        assert not rx.search(text), f"{path.relative_to(REPO)}: {what}"


@pytest.mark.parametrize("path", existing(CODE + TOUCHED), ids=lambda p: str(p.relative_to(REPO)))
def test_no_literal_hf_token_assignment(path: Path) -> None:
    for n, line in enumerate(path.read_text(errors="replace").splitlines(), 1):
        assert not LITERAL_TOKEN_ASSIGNMENT.search(line), f"{path.relative_to(REPO)}:{n}: a literal HF_TOKEN value"


@pytest.mark.parametrize("path", existing(CODE), ids=lambda p: str(p.relative_to(REPO)))
def test_no_mutable_image_tag(path: Path) -> None:
    """``:latest`` may only appear in a refusal (case pattern, grep, die/log message), never as an image to run."""
    for n, line in enumerate(path.read_text(errors="replace").splitlines(), 1):
        if re.search(r":latest\b", line):
            assert re.search(r"\b(die|case|grep|log|echo|printf|refus|reject|raise)\b|\*:latest|\[\[|!=|==|^\s*#", line), f"{path.relative_to(REPO)}:{n}: :latest outside a refusal: {line.strip()}"
        assert not re.search(r"(?i)\bimage\s*[:=]\s*\S+:latest", line), f"{path.relative_to(REPO)}:{n}"


def test_every_image_reference_in_the_script_is_digest_pinned_or_a_placeholder() -> None:
    script = REPO / "scripts" / "10-stage-model.sh"
    if not script.is_file():
        pytest.fail("scripts/10-stage-model.sh must exist")
    for n, line in enumerate(script.read_text().splitlines(), 1):
        for ref in re.findall(r"\b[\w./-]+\.(?:amazonaws\.com|io|com|dev)/[\w./-]+:[\w.-]+(?:@sha256:[^\s\"']+)?", line):
            assert "@sha256:" in ref, f"scripts/10-stage-model.sh:{n}: image reference without a digest: {ref}"


def test_requirements_are_pinned_without_ranges_urls_or_vcs() -> None:
    req = REPO / "tools" / "stage_model" / "requirements.txt"
    assert req.is_file()
    for ln in req.read_text().splitlines():
        ln = ln.strip()
        if ln and not ln.startswith("#"):
            assert re.fullmatch(r"[A-Za-z0-9_.\[\]-]+==[0-9][0-9A-Za-z.]*", ln), f"unpinned requirement: {ln}"
            assert not re.search(r"git\+|https?://|[<>~]", ln)


def test_the_env_files_that_may_hold_a_token_are_gitignored() -> None:
    for rel in ("config/sample.env", "manifests/config/platform.env"):
        proc = subprocess.run(["git", "check-ignore", "-q", rel], cwd=REPO, check=False)
        assert proc.returncode == 0, f"{rel} may carry HF_TOKEN and must be gitignored"


def test_no_tracked_file_other_than_the_example_holds_a_token_shaped_string() -> None:
    out = subprocess.run(["git", "ls-files", "-z"], cwd=REPO, capture_output=True, check=True).stdout.decode().split("\0")
    bad = []
    for rel in filter(None, out):
        p = REPO / rel
        if not p.is_file() or p.suffix in {".png", ".pdf", ".gz", ".zip", ".pptx", ".jpg", ".svg"}:
            continue
        try:
            text = p.read_text()
        except (UnicodeDecodeError, OSError):
            continue
        if CREDENTIAL["hugging face token"].search(text):
            bad.append(rel)
    assert bad == []


def test_env_template_lists_the_model_path_keys() -> None:
    text = (REPO / "manifests" / "config" / "platform.env.example").read_text()
    for key in ("MODEL_S3_PREFIX", "MODEL_S3_URI"):
        assert re.search(rf"(?m)^{key}=", text), f"platform.env.example must show {key}"
    assert not re.search(r"(?m)^HF_TOKEN=\S", text), "the example never carries a token value"


def test_docs_explain_the_job_gated_models_and_the_secrets_manager_option() -> None:
    docs = [p for p in [REPO / "README.md", *(REPO / "docs").rglob("*.md"), REPO / "terraform" / "README.md", REPO / "tools" / "stage_model" / "README.md"] if p.is_file()]
    text = "\n".join(p.read_text(errors="replace") for p in docs)
    assert "10-stage-model.sh" in text, "document how to run the staging Job"
    assert "HF_TOKEN" in text and re.search(r"licen[cs]e", text, re.I), "document gated models: accept the licence, set HF_TOKEN"
    assert "hf_token_secret_arn" in text, "document the Secrets Manager option"
    assert "--dry-run" in text
