# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""RED tests: publish gates for the NEW Terraform code (INTERNAL-RULES): no account IDs / personal data / real
resource IDs / non-documentation IPs, MIT-0 headers, Checkov, terraform fmt/validate/test.

``*.lock.hcl`` is excluded on purpose: lock files carry hashes that match digit patterns.
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
import shutil
from pathlib import Path

import lib_proc
import pytest

REPO = Path(__file__).resolve().parents[2]
SCOPE_DIRS = [REPO / "terraform" / "cluster", REPO / "terraform" / "bootstrap"]
EXTRA_FILES = [
    REPO / "scripts" / "gen_env.py",
    REPO / "tests" / "scripts" / "test_140_gen_env.py",
    REPO / "manifests" / "config" / "platform.env.example",
    *sorted((REPO / "tests" / "terraform").glob("*.py")),
]
TEXT_SUFFIXES = {".tf", ".py", ".sh", ".hcl", ".example", ".md", ".json", ".yaml", ".yml", ".txt", ".env"}
SKIP_PARTS = {".terraform", "__pycache__", ".pytest_cache"}
# Built from parts so this file does not contain the patterns it forbids. The event token excludes the AWS CLI
# subcommand `aws eks update-kubeconfig` (not deployment data).
PERSONAL = re.compile(
    "|".join(["fern" + "asch", "schr" + "oder", "@amazon" + r"\.com", "kube" + "con(?!fig)"]), re.IGNORECASE
)
PLACEHOLDER_ACCOUNT = "1234" + "56789012"  # documented placeholder (AWS documentation convention), not a real account
ACCOUNT_ID = re.compile(r"(?<![0-9A-Za-z.\-_/])[0-9]{12}(?![0-9A-Za-z])")
RESOURCE_ID = re.compile(r"\b(?:cr|sg|subnet|vpc|rtb|igw|eni|vpce|snap|vol|ami)-[0-9a-f]{8,}\b")
IPV4 = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}(?:/\d{1,2})?")
# IPv4Address(0) is 0.0.0.0, the unspecified address (as a bare literal it reads like a bind address to the scanners)
IP_ALLOWED = (str(ipaddress.IPv4Address(0)), "192.0.2.", "198.51.100.", "203.0.113.", "10.", "192.168.", "127.0.0.1")


def files(*, include_tests_dir: bool = True) -> list[Path]:
    """Text files in scope (new Terraform code, generator, new tests)."""
    out: list[Path] = []
    for d in SCOPE_DIRS:
        assert d.is_dir(), f"{d.relative_to(REPO)} must exist"
        for p in d.rglob("*"):
            rel = p.relative_to(d).parts
            if not p.is_file() or SKIP_PARTS & set(rel) or p.name.endswith(".lock.hcl") or "*" in p.name:
                continue
            if p.suffix in TEXT_SUFFIXES or p.name.endswith(".example"):
                if not include_tests_dir and "tests" in rel:
                    continue
                out.append(p)
    for p in EXTRA_FILES:
        assert p.is_file(), f"{p.relative_to(REPO)} must exist"
        out.append(p)
    return out


def test_no_account_ids_or_personal_data() -> None:
    bad = []
    for p in files():
        text = p.read_text(errors="replace")
        for rx, what in ((ACCOUNT_ID, "12-digit id"), (PERSONAL, "personal/deployment data")):
            # the documented placeholder account (platform.env.example, fixtures) is the only 12-digit id allowed
            if any(m.group(0) != PLACEHOLDER_ACCOUNT or rx is PERSONAL for m in rx.finditer(text)):
                bad.append(f"{p.relative_to(REPO)}: {what}")
    assert bad == []


def test_no_real_resource_ids_or_public_ips_in_code_and_examples() -> None:
    bad = []
    for p in files(include_tests_dir=False):
        if p.suffix in {".py"} and p.parts[-2:-1] == ("terraform",) and "tests" in p.parts:
            continue
        for ln_no, ln in enumerate(p.read_text(errors="replace").splitlines(), 1):
            if p.name == "instance-types.json":
                continue
            if RESOURCE_ID.search(ln):
                bad.append(f"{p.relative_to(REPO)}:{ln_no}: resource id")
            for m in IPV4.finditer(ln):
                if not m.group(0).startswith(IP_ALLOWED) and not re.match(r"172\.(1[6-9]|2\d|3[01])\.", m.group(0)):
                    bad.append(f"{p.relative_to(REPO)}:{ln_no}: public-looking IP {m.group(0)}")
    assert bad == []


def test_mit0_spdx_header_on_new_code() -> None:
    want = ("Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.", "SPDX-License-Identifier: MIT-0")
    bad = []
    for p in files():
        if p.suffix not in {".tf", ".py", ".sh"} and not p.name.endswith(".tftest.hcl"):
            continue
        head = "\n".join(p.read_text(errors="replace").splitlines()[:6])
        if not all(w in head for w in want):
            bad.append(str(p.relative_to(REPO)))
    assert bad == []


def terraform_bin() -> str | None:
    """Return a Terraform >= 1.10 binary (TERRAFORM_BIN or PATH) or None."""
    cand = os.environ.get("TERRAFORM_BIN") or shutil.which("terraform")
    if not cand:
        return None
    proc = lib_proc.run([cand, "version", "-json"], capture_output=True, text=True, check=False)
    try:
        ver = json.loads(proc.stdout)["terraform_version"]
    except (ValueError, KeyError):
        return None
    major, minor = (int(x) for x in ver.split(".")[:2])
    return cand if (major, minor) >= (1, 10) else None


@pytest.mark.slow
def test_checkov_clean() -> None:
    if shutil.which("checkov") is None:
        pytest.skip("checkov not installed")
    for d in SCOPE_DIRS:
        assert d.is_dir()
    args = ["checkov", "--framework", "terraform", "--quiet", "--compact", "--skip-path", "tests/"]
    proc = lib_proc.run(
        [*args, "-d", str(SCOPE_DIRS[0]), "-d", str(SCOPE_DIRS[1])], capture_output=True, text=True, check=False
    )
    assert proc.returncode == 0, proc.stdout[-3000:]


@pytest.mark.slow
@pytest.mark.parametrize("folder", ["cluster", "bootstrap"])
def test_terraform_fmt_and_validate(folder: str) -> None:
    tf = terraform_bin()
    if tf is None:
        pytest.skip("needs Terraform >= 1.10 (set TERRAFORM_BIN)")
    d = REPO / "terraform" / folder
    assert d.is_dir()
    fmt = lib_proc.run([tf, f"-chdir={d}", "fmt", "-check", "-recursive"], capture_output=True, text=True, check=False)
    assert fmt.returncode == 0, fmt.stdout
    init = lib_proc.run([tf, f"-chdir={d}", "init", "-backend=false", "-input=false"], capture_output=True, text=True, check=False)
    assert init.returncode == 0, init.stderr[-2000:]
    val = lib_proc.run([tf, f"-chdir={d}", "validate"], capture_output=True, text=True, check=False)
    assert val.returncode == 0, val.stdout[-2000:] + val.stderr[-2000:]


@pytest.mark.slow
@pytest.mark.parametrize("folder", ["cluster", "bootstrap"])
def test_terraform_test_suite_green(folder: str) -> None:
    """Run ``terraform test`` in terraform/<folder> (the ``.tftest.hcl`` files of its ``tests/``).

    Args:
        folder: ``cluster`` (gpu_pools_*, root_validation and the others) or ``bootstrap`` (tags_validation).
    """
    tf = terraform_bin()
    if tf is None:
        pytest.skip("needs Terraform >= 1.10 (set TERRAFORM_BIN)")
    d = REPO / "terraform" / folder
    assert d.is_dir()
    assert sorted((d / "tests").glob("*.tftest.hcl")), f"terraform/{folder}/tests holds no .tftest.hcl: nothing would run"
    init = lib_proc.run([tf, f"-chdir={d}", "init", "-backend=false", "-input=false"], capture_output=True, text=True, check=False)
    assert init.returncode == 0, init.stderr[-2000:]
    proc = lib_proc.run([tf, f"-chdir={d}", "test", "-no-color"], capture_output=True, text=True, check=False)
    assert proc.returncode == 0, proc.stdout[-4000:] + proc.stderr[-2000:]
