# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Docs set contract.

The repository root carries README.md, LICENSE, CONTRIBUTING.md and SECURITY.md.  The two operator guides live in ONE place:
``docs/DEPLOY.md`` and ``docs/TROUBLESHOOTING.md`` (not at the root; ``test_guides_live_under_docs_only`` pins that choice).

What is checked
===============
* LICENSE is the MIT-0 text unmodified (MIT No Attribution); SECURITY.md points to the AWS vulnerability reporting page.
* README.md: the three user variables and the tfvars -> platform.env flow, a layout row for every top-level directory, and
  links to the other documents.  Its wording is NOT pinned: the README may be rewritten freely (the pins of the versions
  table are checked against versions.env by test_public_runtime_image).
* DEPLOY.md carries the real procedure.
* Cross-checks over README, DEPLOY and TROUBLESHOOTING: every repository path or script named exists in the tree, relative links
  and anchors resolve, forbidden strings are absent.
"""

from __future__ import annotations

import glob
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
README = ROOT / "README.md"
DEPLOY = ROOT / "docs" / "DEPLOY.md"
TROUBLE = ROOT / "docs" / "TROUBLESHOOTING.md"
CONTRIB = ROOT / "CONTRIBUTING.md"
SECURITY = ROOT / "SECURITY.md"
LICENSE = ROOT / "LICENSE"
MAIN_DOCS = [README, DEPLOY, TROUBLE]
SET_DOCS = [README, DEPLOY, TROUBLE, CONTRIB, SECURITY]
GUIDES = [ROOT / "docs/optimizations" / n / "README.md" for n in ("kv-router", "hicache", "pd-disaggregation")]

MIT0_BODY = """
Permission is hereby granted, free of charge, to any person obtaining a copy of this software and associated documentation
files (the "Software"), to deal in the Software without restriction, including without limitation the rights to use, copy,
modify, merge, publish, distribute, sublicense, and/or sell copies of the Software, and to permit persons to whom the Software
is furnished to do so.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES
OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE
LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR
IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.
"""

OLD_CONFIG = "config-" + "ap" + "s1"  # built from fragments, like the stale names below: this file must not carry them

# Strings that must not appear in README / DEPLOY / TROUBLESHOOTING (nor in CONTRIBUTING / SECURITY, where they apply).
FORBIDDEN: list[tuple[str, str]] = [
    (r"\bD-(?:\d{2}|xx)\b", "decision id (D-xx)"),
    (r"kubecon" + r"-artifacts", "internal artifacts path"),
    ("/Us" + r"ers/|/home/[a-z]|~/Documents", "personal path"),
    ("fer" + r"nasch|[\w.+-]+@(?!example\.)[\w-]+\.(?:com|org|net)", "personal or unlisted e-mail"),
    (r"\bTra" + r"ck [ABF]\b|\bTra" + r"cks [ABF]\b", "internal track name"),
    (r"GLM", "GLM claim"),
    (r"(?<!\d)(?!123456789012)\d{12}(?!\d)", "account id"),
    (r"(?i)exploratory", "EXPLORATORY label"),
    (r"flash-" + r"mum" + r"bai|Mum" + r"bai block", "internal branch or block name"),
    (r"\bcr-[0-9a-f]{8,}", "reservation id"),
    (r"hf_[A-Za-z0-9]{20,}", "Hugging Face token"),
    (r"DEMO_" + r"TRACK|" + OLD_CONFIG + r"|QWEN" + r"38_(?:IMAGE|MODEL|REVISION)", "stale name"),
    (r"https?://[^\s)>\]]*(?:a2z\.com|aws\.dev|amazon\.dev|corp\.amazon\.com|w\.amazon\.com|code\.amazon\.com|gitlab)", "internal link"),
]
# Allowed e-mail in the standard code-of-conduct sentence of CONTRIBUTING.md only.
ALLOWED_EMAIL = "opensource-codeofconduct" + "@amazon.com"

# Paths a document may name that are generated or gitignored.
GENERATED = {"manifests/config/platform.env", "terraform/cluster/terraform.tfvars", "terraform/bootstrap/terraform.tfvars",
             "terraform/cluster/backend.tf", "terraform/bootstrap/backend.tf", "config/"}

TOP_DIRS = ("scripts", "tools", "terraform", "manifests", "examples", "plans", "docs", "dashboards", "workloads", "tests", "config")
PATH_RE = re.compile(rf"(?<![\w./@-])((?:{'|'.join(TOP_DIRS)})/[\w./*-]*[\w*/])")
FILE_RE = re.compile(r"(?<![\w./@/-])([\w-]+\.(?:sh|py|md|csv|env|tfvars|example|yaml))(?![\w/-])")


def _read(p: Path) -> str:
    assert p.is_file(), f"{p.relative_to(ROOT)} missing"
    return p.read_text(encoding="utf-8")


def _section(text: str, pattern: str, level: int = 2) -> str:
    m = re.search(rf"^{'#' * level} .*(?:{pattern}).*$", text, re.M | re.I)
    if not m:
        return ""
    rest = text[m.end():]
    end = re.search(rf"^#{{1,{level}}} ", rest, re.M)
    return rest[: end.start()] if end else rest


def _in_order(text: str, tokens: list[str]) -> str | None:
    pos = -1
    for t in tokens:
        i = text.find(t, pos + 1)
        if i < 0:
            return t
        pos = i
    return None


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def _slug(h: str) -> str:
    s = re.sub(r"[`*_]", "", h.strip().lower())
    s = re.sub(r"[^\w\- ]", "", s)
    return s.replace(" ", "-")


# ---------------------------------------------------------------- the five root files and the guides location

@pytest.mark.parametrize("p", [README, LICENSE, CONTRIB, SECURITY, DEPLOY, TROUBLE], ids=lambda p: str(p.relative_to(ROOT)))
def test_required_files_exist_and_are_not_empty(p: Path) -> None:
    assert _read(p).strip(), f"{p.name} is empty"


def test_guides_live_under_docs_only() -> None:
    """One consistent location for the two operator guides: docs/DEPLOY.md and docs/TROUBLESHOOTING.md."""
    for name in ("DEPLOY.md", "TROUBLESHOOTING.md"):
        assert not (ROOT / name).exists(), f"{name} must not also exist at the repo root (single location: docs/{name})"


# ---------------------------------------------------------------- LICENSE, CONTRIBUTING, SECURITY

def test_license_is_mit_0_unmodified() -> None:
    text = _read(LICENSE)
    lines = text.splitlines()
    assert lines[0].strip() == "MIT No Attribution", "first line must be 'MIT No Attribution'"
    assert re.fullmatch(r"Copyright (?:\d{4} )?Amazon\.com, Inc\. or its affiliates\.(?: All Rights Reserved\.)?", lines[2].strip()), (
        f"line 3 must be the Amazon copyright line, got {lines[2]!r}")
    assert _norm("\n".join(lines[3:])) == _norm(MIT0_BODY), "the MIT-0 permission and warranty text was modified"
    assert not re.search(r"(?i)attribution notice|subject to the following conditions", text), "that is the MIT text, not MIT-0"


def test_security_points_to_aws_vulnerability_reporting() -> None:
    text = _read(SECURITY)
    assert "https://aws.amazon.com/security/vulnerability-reporting/" in text
    assert re.search(r"(?i)public (github |gitlab )?issue", text), "must tell reporters not to open a public issue"


@pytest.mark.parametrize("p", SET_DOCS, ids=lambda p: p.name)
def test_forbidden_strings_absent(p: Path) -> None:
    text = _read(p)
    if p == CONTRIB:
        text = text.replace(ALLOWED_EMAIL, "")
    bad = []
    for pat, what in FORBIDDEN:
        for m in re.finditer(pat, text):
            line = text.count("\n", 0, m.start()) + 1
            bad.append(f"{p.name}:{line} {what}: {m.group(0)[:50]!r}")
    assert not bad, "\n".join(bad[:12])


# ---------------------------------------------------------------- README

def test_readme_three_user_variables_and_the_env_flow() -> None:
    sec = _section(_read(README), r"variable|configur|inputs")
    for w in ("MODEL", "GPU", "REGION"):
        assert re.search(rf"\b{w}\b", sec), f"the three user variables must name {w}"
    bad = _in_order(sec, ["tfvars", "terraform output", "scripts/gen_env.py", "manifests/config/platform.env"])
    assert bad is None, f"flow tfvars -> terraform output -> scripts/gen_env.py -> platform.env is missing or out of order at {bad!r}"
    for k in ("model_id", "gpu_instance_types", "aws_region"):
        assert k in sec, f"tfvars key {k} must be named"


def test_readme_layout_lists_every_top_level_directory() -> None:
    sec = _section(_read(README), r"layout")
    dirs = [p.name for p in ROOT.iterdir() if p.is_dir() and not p.name.startswith(".") and p.name not in {"__pycache__", "config"}]
    gone = [d for d in dirs if f"{d}/" not in sec]
    assert not gone, f"repository layout lacks {gone}"


def test_readme_cost_and_teardown_pointer_and_links() -> None:
    text = _read(README)
    sec = _section(text, r"cost|teardown")
    assert re.search(r"(?i)teardown", sec) and "docs/DEPLOY.md" in sec
    assert re.search(r"(?i)idle|NAT|EKS", sec), "cost note names what costs money while idle"
    for target in ("docs/DEPLOY.md", "docs/TROUBLESHOOTING.md", "CONTRIBUTING.md", "SECURITY.md", "LICENSE", "examples/"):
        assert re.search(rf"\]\((?:\./)?{re.escape(target)}[^)]*\)", text), f"README must link {target} as a markdown link"
    lic = _section(text, r"licen")
    assert "MIT-0" in lic


# ---------------------------------------------------------------- DEPLOY.md

def test_deploy_follows_the_real_procedure_in_order() -> None:
    text = _read(DEPLOY)
    steps = ["terraform/bootstrap", "terraform/cluster", "scripts/gen_env.py", "scripts/10-stage-model.sh", "scripts/11-prefetch.sh",
             "examples/00-baseline", "port-forward", "terraform destroy"]
    assert _in_order(text, steps) is None, f"DEPLOY.md: step missing or out of order at {_in_order(text, steps)!r}"


def test_deploy_prerequisites_iam_and_capacity() -> None:
    text = _read(DEPLOY)
    assert re.search(r"(?i)prerequisites", text) and "IAM" in text
    for ct in ("capacity-block", "odcr", "on-demand", "spot"):
        assert ct in text, f"capacity type {ct} must be documented"
    assert re.search(r"(?i)you (buy|purchase|reserve)[^\n]{0,60}capacity|Terraform never (buys|purchases|reserves)", text)
    for key in ("eks_public_access_cidrs", "gpu_instance_types", "gpu_capacity_type", "capacity_reservation_ids", "model_id", "model_revision", "aws_region"):
        assert key in text, f"tfvars minimum must name {key}"
    assert "terraform.tfvars.example" in text


def test_deploy_staging_and_prefetch() -> None:
    text = _read(DEPLOY)
    for w in ("HF_TOKEN", "STAGE_IMAGE", "--dry-run", "hf_token_secret_arn", "gated"):
        assert w in text, f"staging section lacks {w!r}"
    assert re.search(r"(?i)one model per (terraform )?apply", text)
    assert re.search(r"(?i)public models? (need|require)s? no token|public[^\n]{0,60}no token", text)
    assert re.search(r"(?i)licen[cs]e", text)


def test_deploy_examples_and_observing() -> None:
    text = _read(DEPLOY)
    for w in ("examples/07-all-three", "dashboards/", "kubectl port-forward", "Grafana"):
        assert w in text, f"DEPLOY.md lacks {w!r}"
    for n in ("00-baseline", "01-kv-router", "02-hicache", "03-pd-disaggregation", "07-all-three"):
        assert n in text


def test_deploy_teardown_order_and_idle_cost() -> None:
    sec = _section(_read(DEPLOY), r"teardown|tear down|clean ?up", level=2)
    assert sec.strip(), "a Teardown section"
    assert _in_order(sec, ["kubectl delete", "terraform/cluster", "terraform/bootstrap"]) is None, "teardown order: DGDs, cluster, bootstrap"
    assert re.search(r"(?i)empty[^\n]{0,60}buckets?|buckets?[^\n]{0,60}empty", sec) and re.search(r"aws s3 (rm|rb)|s3 rm", sec)
    assert re.search(r"(?i)NAT", sec) and re.search(r"(?i)\bENI", sec) and "EKS" in sec, "idle cost: EKS, NAT, ENI"


# ---------------------------------------------------------------- cross-checks: paths, links, anchors

def _clean_candidates(text: str) -> set[str]:
    """Repository paths named in ``text``; docs/fern/... points into the upstream Dynamo repository and is skipped."""
    out: set[str] = set()
    for m in PATH_RE.finditer(text):
        tok = m.group(1).rstrip(".,;:")
        if not tok.startswith("docs/fern/"):
            out.add(tok)
    return out


@pytest.mark.parametrize("p", MAIN_DOCS, ids=lambda p: p.name)
def test_every_named_repository_path_exists(p: Path) -> None:
    text = _read(p)
    # placeholders and other-repo cases are dropped: <x>, {x}, $VAR
    text = re.sub(r"[\w./-]*[<{$][^\s`)]*", "", text)
    bad = []
    for tok in sorted(_clean_candidates(text)):
        if tok in GENERATED or tok.rstrip("/") in GENERATED:
            continue
        if "*" in tok:
            if not glob.glob(str(ROOT / tok)):
                bad.append(tok)
        elif not (ROOT / tok).exists():
            bad.append(tok)
    assert not bad, f"{p.name} names paths that do not exist in the tree: {bad}"


@pytest.mark.parametrize("p", MAIN_DOCS, ids=lambda p: p.name)
def test_every_named_script_file_exists_somewhere(p: Path) -> None:
    """Bare file names in backticks (gen_env.py, 11-prefetch.sh, ...) must exist somewhere in the tree."""
    text = _read(p)
    names = {m.group(1) for m in re.finditer(r"`([\w.-]+\.(?:sh|py))(?:\s[^`]*)?`", text)}
    have = {q.name for q in ROOT.rglob("*") if q.is_file() and ".git" not in q.parts and "__pycache__" not in q.parts}
    missing = sorted(n for n in names if n not in have)
    assert not missing, f"{p.name} names scripts that do not exist: {missing}"


@pytest.mark.parametrize("p", SET_DOCS, ids=lambda p: p.name)
def test_relative_links_and_anchors_resolve(p: Path) -> None:
    text = _read(p)
    bad = []
    for m in re.finditer(r"(?<!!)\[[^\]]*\]\(([^)\s]+)\)", text):
        target = m.group(1)
        if re.match(r"[a-z]+:", target):
            continue
        path_part, _, anchor = target.partition("#")
        dest = (p.parent / path_part).resolve() if path_part else p
        if not dest.exists():
            bad.append(f"{target} (no such file)")
            continue
        if anchor and dest.suffix == ".md":
            heads = {_slug(h) for h in re.findall(r"^#+ (.+)$", dest.read_text(encoding="utf-8"), re.M)}
            if anchor not in heads:
                bad.append(f"{target} (no such heading)")
    assert not bad, f"{p.name}: broken links {bad}"


def test_documents_link_each_other() -> None:
    deploy, trouble = _read(DEPLOY), _read(TROUBLE)
    assert re.search(r"\]\((?:\./)?TROUBLESHOOTING\.md[^)]*\)", deploy), "DEPLOY.md links TROUBLESHOOTING.md"
    assert re.search(r"\]\((?:\./)?DEPLOY\.md[^)]*\)", trouble), "TROUBLESHOOTING.md links DEPLOY.md"
    assert re.search(r"\]\(\.\./README\.md[^)]*\)", deploy), "DEPLOY.md links back to the README"


# ---------------------------------------------------------------- the optimization guides

@pytest.mark.parametrize("p", GUIDES, ids=lambda p: p.parent.name)
def test_guides_name_only_existing_scripts_and_paths(p: Path) -> None:
    text = re.sub(r"[\w./-]*[<{$][^\s`)]*", "", _read(p))
    bad = []
    for tok in sorted(_clean_candidates(text)):
        if tok in GENERATED or tok.rstrip("/") in GENERATED:
            continue
        if "*" in tok:
            if not glob.glob(str(ROOT / tok)):
                bad.append(tok)
        elif not (ROOT / tok).exists():
            bad.append(tok)
    assert not bad, f"{p.parent.name}/README.md names paths that do not exist: {bad}"


