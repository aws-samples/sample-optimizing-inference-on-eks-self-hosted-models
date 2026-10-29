# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Docs set contract (sub-cycle D, RED until the docs are written).

The repository root carries README.md, LICENSE, CONTRIBUTING.md and SECURITY.md.  The two operator guides live in ONE place:
``docs/DEPLOY.md`` and ``docs/TROUBLESHOOTING.md`` (not at the root; ``test_guides_live_under_docs_only`` pins that choice).
The sample is for educational use and not for production: the README says so in a block near the top.

What is checked
===============
* LICENSE is the MIT-0 text unmodified (MIT No Attribution); CONTRIBUTING.md is the standard AWS-samples guide with no internal
  links; SECURITY.md points to the AWS vulnerability reporting page.
* README.md: 120-220 lines, H2 sections in a fixed order, disclaimer block, R/O/D definitions (KVBM at most once, as not used),
  versions table, the four user variables and the tfvars -> platform.env flow, a 5-8 command quick start, results summary and
  links to the other documents.
* DEPLOY.md and TROUBLESHOOTING.md carry the real procedure and the known issues of the live tests.
* Cross-checks over README, DEPLOY and TROUBLESHOOTING: every repository path or script named exists in the tree, relative links
  and anchors resolve, forbidden strings are absent, the design log is never named nor linked.
* No tracked file outside the design log and its guard machinery references the design log
  (``test_no_tracked_file_references_the_design_log``; allowlist and the public-docs guard in test_design_log_unreferenced.py).
* The optimization guides, tests/README.md, terraform/README.md and dashboards/README.md use the Track Q names (no DEMO_TRACK,
  no s-lab opt-in switch, no old config directory name, no QWEN38_ keys) and terraform/README.md documents the pool-name suffix ``[-nc<n>]``.
"""

from __future__ import annotations

import glob
import re
from pathlib import Path

import pytest
import test_design_log_unreferenced as design_log

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

OLD_CONFIG = "config" + "-aps1"  # built from fragments: this file must not carry the literal

# Strings that must not appear in README / DEPLOY / TROUBLESHOOTING (nor in CONTRIBUTING / SECURITY, where they apply).
FORBIDDEN: list[tuple[str, str]] = [
    (r"\bD-(?:\d{2}|xx)\b", "decision id (D-xx)"),
    (r"kubecon-artifacts", "internal artifacts path"),
    ("/Us" + r"ers/|/home/[a-z]|~/Documents", "personal path"),
    ("fer" + r"nasch|[\w.+-]+@(?!example\.)[\w-]+\.(?:com|org|net)", "personal or unlisted e-mail"),
    (r"\bTrack [ABF]\b|\bTracks [ABF]\b", "internal track name"),
    (r"GLM", "GLM claim"),
    (r"(?<!\d)(?!123456789012)\d{12}(?!\d)", "account id"),
    (r"(?i)exploratory", "EXPLORATORY label"),
    (r"flash-mumbai|Mumbai block", "internal branch or block name"),
    (r"\bcr-[0-9a-f]{8,}", "reservation id"),
    (r"hf_[A-Za-z0-9]{20,}", "Hugging Face token"),
    (r"DEMO_TRACK|" + OLD_CONFIG + r"|QWEN38_(?:IMAGE|MODEL|REVISION)", "stale name"),
    (r"https?://[^\s)>\]]*(?:a2z\.com|aws\.dev|amazon\.dev|corp\.amazon\.com|w\.amazon\.com|code\.amazon\.com|gitlab)", "internal link"),
]
# Allowed e-mail in the standard code-of-conduct sentence of CONTRIBUTING.md only.
ALLOWED_EMAIL = "opensource-codeofconduct" + "@amazon.com"

# Paths a document may name that are generated, gitignored or belong to a later phase.  Later-phase ones must carry a marker.
GENERATED = {"manifests/config/platform.env", "config/sample.env", "terraform/cluster/terraform.tfvars", "terraform/bootstrap/terraform.tfvars",
             "terraform/cluster/backend.tf", "terraform/bootstrap/backend.tf", "config/"}
LATER_PHASE = {"Dockerfile"}
LATER_MARKER = re.compile(r"(?i)coming|later|planned|next phase")

TOP_DIRS = ("scripts", "tools", "terraform", "manifests", "examples", "plans", "docs", "dashboards", "workloads", "tests", "config")
PATH_RE = re.compile(rf"(?<![\w./@-])((?:{'|'.join(TOP_DIRS)})/[\w./*-]*[\w*/])")
FILE_RE = re.compile(r"(?<![\w./@/-])([\w-]+\.(?:sh|py|md|csv|env|tfvars|example|yaml))(?![\w/-])")


def _read(p: Path) -> str:
    assert p.is_file(), f"{p.relative_to(ROOT)} missing"
    return p.read_text(encoding="utf-8")


def _h2(text: str) -> list[tuple[str, int]]:
    return [(m.group(1).strip(), m.start()) for m in re.finditer(r"^## (.+)$", text, re.M)]


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


def test_contributing_is_the_standard_aws_samples_guide() -> None:
    text = _read(CONTRIB)
    heads = [h.lower() for h in re.findall(r"^#+ (.+)$", text, re.M)]
    for needle in ("reporting bugs", "contributing via pull requests", "code of conduct", "security issue notifications", "licensing"):
        assert any(needle in h for h in heads), f"CONTRIBUTING.md lacks a heading '{needle}'"
    assert "https://aws.github.io/code-of-conduct" in text
    assert "https://aws.amazon.com/security/vulnerability-reporting/" in text
    assert re.search(r"(?i)do not create a public (github )?issue", text), "security issues must not go to a public issue"
    low = text.lower()
    assert "license" in low and "confirm the licensing" in low, "licensing section must ask to confirm the licensing of contributions"
    assert re.search(r"(?i)pull request|merge request", text)
    assert not re.search(r"(?i)\b(todo|tbd)\b|\{\{|<repo", text), "no placeholders"


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


@pytest.mark.parametrize("p", SET_DOCS, ids=lambda p: p.name)
def test_set_docs_never_reference_the_design_log(p: Path) -> None:
    bad = [f"{p.name}:{n}: {ln.strip()[:80]}" for n, ln in enumerate(_read(p).splitlines(), 1) if design_log.PUBLIC_TERMS.search(ln)]
    assert not bad, f"the design log is internal; the public docs must not name it or link into it: {bad[:5]}"


def test_no_tracked_file_references_the_design_log() -> None:
    """The design log is internal: no tracked file (test code included) names its directory or links into it, except the
    design log itself and the guard machinery listed in ``design_log.SDD_MACHINERY``."""
    bad = design_log.offenders(design_log.SDD_REF, lambda rel: rel not in design_log.SDD_MACHINERY)
    shown = "\n".join(f"  {p} ({len(h)}): {h[:3]}" for p, h in sorted(bad.items()))
    assert not bad, f"{len(bad)} tracked file(s) reference the design log (allowlist: design_log.SDD_MACHINERY):\n{shown}"


# ---------------------------------------------------------------- README

def test_readme_length_120_to_220_lines() -> None:
    n = len(_read(README).splitlines())
    assert 120 <= n <= 220, f"README.md has {n} lines, want 120-220"


SECTIONS = [  # (label, heading regex): H2 sections in this order
    ("what it is", r"what"), ("architecture", r"architect"), ("versions", r"version"), ("prerequisites", r"prereq"),
    ("variables", r"variable|configur|inputs"), ("quick start", r"quick ?start"), ("results", r"result"),
    ("repository layout", r"layout"), ("cost and teardown", r"cost|teardown"), ("documentation links", r"doc|links|more"),
    ("license", r"licen"),
]


def test_readme_sections_in_order() -> None:
    heads = [h for h, _ in _h2(_read(README))]
    i = 0
    missing = []
    for label, pat in SECTIONS:
        j = next((k for k in range(i, len(heads)) if re.search(pat, heads[k], re.I)), None)
        if j is None:
            missing.append(label)
        else:
            i = j + 1
    assert not missing, f"README.md H2 sections missing or out of order: {missing}; found {heads}"


def test_readme_disclaimer_block_near_the_top() -> None:
    text = _read(README)
    head = "\n".join(text.splitlines()[:30])
    block = "\n".join(ln for ln in head.splitlines() if ln.startswith(">"))
    assert block, "a '>' disclaimer block must sit in the first 30 lines"
    low = block.lower()
    assert "educational purposes only" in low and "not for production use" in low
    assert "aws sample" in low or "sample code" in low
    for w in ("hugging face", "ngc", "terms", "cost"):
        assert w in low, f"disclaimer must mention {w!r}"
    assert head.index(block.splitlines()[0]) < head.find("## "), "the disclaimer comes before the first H2 section"


def test_readme_what_it_is_defines_r_o_d_and_kvbm_once_at_most() -> None:
    text = _read(README)
    sec = _section(text, r"what")
    assert sec.strip()
    assert re.search(r"\bR\b[^\n]{0,80}(?i:router)", sec), "R = KV router"
    assert re.search(r"\bO\b[^\n]{0,80}SGLang HiCache", sec), "O = SGLang HiCache"
    assert re.search(r"\bD\b[^\n]{0,80}(?i:disaggregat)", sec), "D = prefill/decode disaggregation"
    assert "Qwen/Qwen3.8-27B-FP8" in sec, "the one public model is named in the first section"
    kv = [ln for ln in text.splitlines() if "KVBM" in ln]
    assert len(kv) <= 1 and all(re.search(r"(?i)not used|does not use|unused", ln) for ln in kv), f"KVBM at most once, as not used: {kv}"


def test_readme_versions_table() -> None:
    sec = _section(_read(README), r"version")
    rows = [ln for ln in sec.splitlines() if ln.startswith("|")]
    assert len(rows) >= 7, "a markdown table of versions"
    def row(pat: str) -> str:
        r = [ln for ln in rows if re.search(pat, ln)]
        assert r, f"versions table lacks a row for {pat!r}"
        return r[0]
    sg = row(r"SGLang")
    assert "0.5.21" in sg and "digest" in sg and "versions.env" in sg and "sglang-runtime:1.6.0-efa" in sg, (
        "the public NGC image sglang-runtime:1.6.0-efa (SGLang 0.5.21), pinned by digest in versions.env")
    assert not LATER_MARKER.search(sg), "versions.env exists now: the row carries no later-phase marker"
    op = row(r"(?i)operator")
    assert "1.5.1" in op and "runtimeVersionOverride" in op and "1.6.0" in op
    assert "1.5.0" in row(r"NIXL")
    assert "1.50.0" in row(r"(?i)EFA")
    ngc = row(r"\bNGC\b")  # the stock NGC image (SGLang 0.5.18 on Dynamo 1.5.1) that the results do not use
    assert "0.5.18" in ngc and "1.5.1" in ngc
    assert re.search(r"1\.17", _read(README)) and re.search(r"(?i)terraform", _read(README)), "Terraform 1.17 recommended (test_efa_null_and_versions)"


def test_readme_prerequisites_name_terraform_and_tools() -> None:
    sec = _section(_read(README), r"prereq")
    for w in ("Terraform", "kubectl", "AWS CLI", "uv", "Hugging Face", "GPU"):
        assert w in sec, f"prerequisites lack {w}"


def test_readme_four_user_variables_and_the_env_flow() -> None:
    sec = _section(_read(README), r"variable|configur|inputs")
    for w in ("MODEL", "WORKLOAD", "GPU", "REGION"):
        assert re.search(rf"\b{w}\b", sec), f"the four user variables must name {w}"
    bad = _in_order(sec, ["tfvars", "terraform output", "scripts/gen_env.py", "manifests/config/platform.env"])
    assert bad is None, f"flow tfvars -> terraform output -> scripts/gen_env.py -> platform.env is missing or out of order at {bad!r}"
    assert "config/sample.env" in sec
    for k in ("model_id", "gpu_instance_types", "aws_region", "workload_rate"):
        assert k in sec, f"tfvars key {k} must be named"


def test_readme_quick_start_is_5_to_8_commands_and_links_deploy() -> None:
    sec = _section(_read(README), r"quick ?start")
    blocks = re.findall(r"```(?:bash|sh|shell)?\n(.*?)```", sec, re.S)
    assert blocks, "a fenced command block"
    cmds = []
    for b in blocks:
        joined = re.sub(r"\\\n\s*", " ", b)
        cmds += [ln for ln in joined.splitlines() if ln.strip() and not ln.strip().startswith("#")]
    assert 5 <= len(cmds) <= 8, f"quick start has {len(cmds)} commands, want 5-8"
    assert re.search(r"\]\((?:\./)?docs/DEPLOY\.md[^)]*\)", sec), "the quick start links docs/DEPLOY.md"


def test_readme_results_summary() -> None:
    sec = _section(_read(README), r"result")
    for w in ("W3-16K", "n=1", "window", "SLO"):
        assert w in sec, f"results summary lacks {w!r}"
    assert re.search(r"docs/results/[\w-]*\.csv", sec), "link a docs/results/*.csv"
    assert (ROOT / "docs/results/w3-16k-cells.csv").is_file()
    assert re.search(r"(?i)not (a )?(statistic|significan|proof|proven)|single run|one run", sec), "state that n=1 is not statistically established"
    assert re.search(r"(?i)attainment|TTFT|ITL", sec), "the SLO definition names its metrics"


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
             "examples/00-baseline", "tools/matrix_driver", "port-forward", "terraform destroy"]
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
    for w in ("HF_TOKEN", "config/sample.env", "STAGE_IMAGE", "--dry-run", "hf_token_secret_arn", "gated"):
        assert w in text, f"staging section lacks {w!r}"
    assert re.search(r"(?i)one model per (terraform )?apply", text)
    assert re.search(r"(?i)public models? (need|require)s? no token|public[^\n]{0,60}no token", text)
    assert re.search(r"(?i)licen[cs]e", text)


def test_deploy_matrix_and_observing() -> None:
    text = _read(DEPLOY)
    for w in ("plans/w3-16k", "--hard-stop-in", "render_driver.py", "examples/04-all-three", "dashboards/", "kubectl port-forward", "Grafana"):
        assert w in text, f"DEPLOY.md lacks {w!r}"
    for n in ("00-baseline", "01-kv-router", "02-hicache", "03-pd-disaggregation", "04-all-three"):
        assert n in text


def test_deploy_teardown_order_and_idle_cost() -> None:
    sec = _section(_read(DEPLOY), r"teardown|tear down|clean ?up", level=2)
    assert sec.strip(), "a Teardown section"
    assert _in_order(sec, ["kubectl delete", "terraform/cluster", "terraform/bootstrap"]) is None, "teardown order: DGDs, cluster, bootstrap"
    assert re.search(r"(?i)empty[^\n]{0,60}buckets?|buckets?[^\n]{0,60}empty", sec) and re.search(r"aws s3 (rm|rb)|s3 rm", sec)
    assert re.search(r"(?i)NAT", sec) and re.search(r"(?i)\bENI", sec) and "EKS" in sec, "idle cost: EKS, NAT, ENI"


# ---------------------------------------------------------------- TROUBLESHOOTING.md

def test_troubleshooting_covers_the_known_issues() -> None:
    text = _read(TROUBLE)
    checks = {
        "terraform < 1.12": r"(?s)1\.12.*efa_maximum_interfaces|efa_maximum_interfaces.*1\.12",
        "insufficient capacity": r"InsufficientInstanceCapacity",
        "az chosen by offered": r"gpu_az_ids",
        "extra cpu-demo node": r"cpu-demo",
        "auto mode tags": r"(?s)Auto Mode.*tags|tags.*Auto Mode",
        "sandbox": r"FailedCreatePodSandBox",
        "image pull": r"14\.8 ?GB",
        "bucket not empty": r"(?i)BucketNotEmpty|not empty",
        "reservation selector": r"(?i)capacityReservationSelectorTerms|selector terms",
        "platform.env missing": r"platform\.env",
    }
    missing = [k for k, pat in checks.items() if not re.search(pat, text)]
    assert not missing, f"TROUBLESHOOTING.md lacks entries: {missing}"
    assert re.search(r"(?i)offered", text) and re.search(r"(?i)\bNAT\b", text)
    assert re.search(r"gen_env\.py", text), "the missing platform.env entry names the fix (scripts/gen_env.py)"
    assert re.search(r"(?i)capacity block", text) and re.search(r"(?i)\bODCR\b", text)
    assert re.search(r"(?i)transient|retr", text), "FailedCreatePodSandBox is described as transient"


@pytest.mark.parametrize(("code", "pattern"), [
    ("3", r"(?i)token"), ("4", r"(?i)denied|403"), ("5", r"(?i)not found|404"), ("7", r"(?i)transfer"),
])
def test_troubleshooting_stage_job_exit_codes(code: str, pattern: str) -> None:
    text = _read(TROUBLE)
    rows = [ln for ln in text.splitlines() if re.search(rf"(?<![\w.])(?:exit(?: code)? )?`?{code}`?(?![\w.])", ln) and re.search(pattern, ln)]
    assert rows, f"TROUBLESHOOTING.md must map stage-job exit code {code} ({pattern})"
    assert re.search(r"(?i)one model per apply|prefix", text), "exit 7 cause: the one-model-per-apply prefix limit"


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


@pytest.mark.parametrize("p", MAIN_DOCS, ids=lambda p: p.name)
def test_later_phase_files_carry_a_marker(p: Path) -> None:
    for n, ln in enumerate(_read(p).splitlines(), 1):
        for name in LATER_PHASE:
            if re.search(rf"(?<![\w/.-]){re.escape(name)}(?![\w.-])", ln) and not LATER_MARKER.search(ln):
                pytest.fail(f"{p.name}:{n} names {name} (not in the tree yet) without saying it is coming in a later phase")


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


# ---------------------------------------------------------------- consistency of the other READMEs with Track Q

STALE_OTHERS = [
    (r"DEMO_TRACK", "DEMO_TRACK (Track Q is the only profile)"),
    (r"SCENARIO_INCLUDE_LAB", "s-lab opt-in switch"),
    (r"s-lab[^\n]{0,80}opt-in|opt-in[^\n]{0,80}s-lab", "s-lab opt-in"),
    (OLD_CONFIG, "renamed manifests/" + OLD_CONFIG),
    (r"QWEN38_(?:IMAGE|MODEL|REVISION)", "renamed QWEN38_ platform keys"),
]


@pytest.mark.parametrize("p", [*GUIDES, ROOT / "tests/README.md", ROOT / "terraform/README.md", ROOT / "dashboards/README.md"],
                         ids=lambda p: str(p.relative_to(ROOT)))
def test_other_readmes_use_track_q_names(p: Path) -> None:
    text = _read(p)
    bad = [f"{p.relative_to(ROOT)}:{text.count(chr(10), 0, m.start()) + 1} {what}" for pat, what in STALE_OTHERS for m in re.finditer(pat, text)]
    assert not bad, "\n".join(bad[:10])


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


def test_terraform_readme_documents_the_pool_name_suffix() -> None:
    text = _read(ROOT / "terraform/README.md")
    assert "[-nc<n>]" in text, "terraform/README.md must document the pool-name suffix `[-nc<n>]` (network cards)"
    assert re.search(r"(?i)network cards?", text)
