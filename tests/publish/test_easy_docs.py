# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Easy docs: the Auto Mode tag limitation, the Grafana edge default and the incident-response runbook.

What is checked
===============
* Tag limitation, in terraform/README.md (section ``Tags``) and docs/TROUBLESHOOTING.md (the Auto Mode tags entry): the
  Auto Mode instances and the Prometheus volume do not carry the stack ``tags``, only ``Project`` and ``nodeclass`` come
  from the NodeClass, so a cost-allocation report by tag and a tag-filtered ``aws ec2 describe-instances`` miss them; they
  are found by instance ID (``spec.providerID`` of the node) or by the tag ``eks:eks-cluster-name``; and a change of the
  NodeClass tags replaces the nodes.  The facts are pinned to the code: every NodeClass sets exactly ``Project`` and
  ``nodeclass``, and the ``auto-ebs-sc`` StorageClass sets no tag.  If the code changes, these tests ask for the docs to
  change with it.
* Grafana edge: ``enable_grafana_edge`` defaults to ``false`` (the tfvars example does not turn it on) and
  terraform/README.md says so, with ``kubectl port-forward`` as the default path.  The edge's CloudFront-to-NLB hop is
  plain HTTP: terraform/README.md documents the ``grafana_edge_allow_plain_http_origin`` acknowledgement (default
  ``false``), the data that crosses the hop, the private VPC origin path, why the NLB cannot take a TLS listener and
  what TLS on that hop needs, as an accepted trade-off; docs/TROUBLESHOOTING.md explains the refused plan.  Viewer TLS:
  terraform/README.md documents ``grafana_edge_domain`` and ``grafana_edge_viewer_certificate_arn`` (default empty),
  the ``TLSv1`` policy CloudFront forces on the default certificate, ``TLSv1.2_2021`` over SNI with an ACM certificate
  in ``us-east-1``, the advice to set both for production, the DNS record and the ``dashboards`` default root object;
  docs/TROUBLESHOOTING.md explains the refused certificate.
* docs/INCIDENT-RESPONSE.md: exists, is linked from SECURITY.md and links back to it; it says to rotate/revoke the
  credential first, covers the history purge (GitHub support; ``git filter-repo`` is not enough on its own), CloudTrail,
  the report, the re-scan (tests/publish, detect-secrets) and the secret surfaces of this sample; its relative links
  resolve, and it names no internal host or ticket system and passes the forbidden-string list of the public docs.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import sanitize_scan
import test_docs_set as ds

ROOT = sanitize_scan.REPO_ROOT
TF_README = ROOT / "terraform/README.md"
TROUBLE = ROOT / "docs/TROUBLESHOOTING.md"
SECURITY = ROOT / "SECURITY.md"
INCIDENT = ROOT / "docs/INCIDENT-RESPONSE.md"
VARIABLES = ROOT / "terraform/cluster/variables.tf"
TFVARS_EXAMPLE = ROOT / "terraform/cluster/terraform.tfvars.example"
NODEPOOL_TF = ROOT / "terraform/cluster/nodepool.tf"
GPU_POOLS_TF = ROOT / "terraform/cluster/modules/gpu-pools/main.tf"

# Internal hosts and ticket systems the public runbook must not name.  Built from fragments so that this file does not
# carry them (the tree scanner of sanitize_scan.py reads the tests too).
INTERNAL_NAMES: list[tuple[str, str]] = [
    (re.escape("w." + "amazon.com"), "internal wiki"),
    (re.escape("a2z" + ".com"), "internal host"),
    (r"(?i)\bqu" + r"ip\b", "internal documents"),
    (r"(?i)\btask" + r"ei\b", "internal ticket system"),
    (re.escape("sim." + "amazon"), "internal ticket system"),
    (re.escape("t.corp." + "amazon"), "internal ticket system"),
    (re.escape("code." + "amazon"), "internal code browser"),
    (re.escape("gitlab.aws" + ".dev"), "internal git host"),
]


def _read(p: Path) -> str:
    """Return the text of ``p``, failing the test when the file is missing.

    Args:
        p: Absolute path of a tracked file.

    Returns:
        The UTF-8 content of the file.
    """
    assert p.is_file(), f"{p.relative_to(ROOT)} missing"
    return p.read_text(encoding="utf-8")


def _flat(text: str) -> str:
    """Collapse every run of whitespace (Markdown line breaks included) into one space."""
    return " ".join(text.split())


def _sentences(text: str) -> list[str]:
    """Split flattened Markdown text into sentences (and list items), so a fact must sit in one sentence."""
    return [s for s in re.split(r"(?<=[.:;])\s+(?=[A-Z`(*])|\s+-\s+|\n", _flat(text)) if s]


def _has_sentence(text: str, *patterns: str) -> bool:
    """True when one sentence of ``text`` matches every regex of ``patterns``."""
    return any(all(re.search(p, s) for p in patterns) for s in _sentences(text))


def _tf_tags_section() -> str:
    sec = ds._section(_read(TF_README), r"Tags")
    assert sec.strip(), "terraform/README.md needs a '## Tags' section (the Auto Mode tag limitation)"
    return sec


def _trouble_tags_section() -> str:
    sec = ds._section(_read(TROUBLE), r"Auto Mode.*tags", level=3)
    assert sec.strip(), "docs/TROUBLESHOOTING.md needs its '### Auto Mode ... tags' entry"
    return sec


# ---------------------------------------------------------------- the facts in the code (PT-27)

def _tag_blocks(text: str) -> list[set[str]]:
    """Keys of every ``tags = { ... }`` map in a Terraform file."""
    return [set(re.findall(r"(?m)^\s*\"?([\w./:-]+)\"?\s*=", m.group(1))) for m in re.finditer(r"\btags\s*=\s*\{([^}]*)\}", text)]


@pytest.mark.parametrize("p", [NODEPOOL_TF, GPU_POOLS_TF], ids=lambda p: str(p.relative_to(ROOT)))
def test_every_nodeclass_sets_only_project_and_nodeclass_tags(p: Path) -> None:
    """The documented limitation holds only while the NodeClasses do not merge the stack tags."""
    text = _read(p)
    blocks = _tag_blocks(text)
    assert blocks, f"{p.name}: no NodeClass tags map found"
    assert all(b == {"Project", "nodeclass"} for b in blocks), (
        f"{p.name}: the NodeClass tags changed ({blocks}); update the Tags section of terraform/README.md and TROUBLESHOOTING")
    assert not re.search(r"\b(?:local|var)\.tags\b", text), f"{p.name} now merges the stack tags: the limitation docs are stale"


def test_auto_mode_storage_class_sets_no_tags() -> None:
    """The Prometheus volume comes from ``auto-ebs-sc``; with no tagSpecification it carries none of the stack tags."""
    text = _read(NODEPOOL_TF)
    assert "name: auto-ebs-sc" in text, "the auto-ebs-sc StorageClass moved: update this test and the Tags docs"
    assert not re.search(r"(?i)tagSpecification", text), "auto-ebs-sc now tags its volumes: update the Tags docs"
    assert re.search(r'storageClassName\s*=\s*"auto-ebs-sc"', _read(ROOT / "terraform/cluster/observability.tf")), (
        "the Prometheus volume no longer uses auto-ebs-sc: update the Tags docs")


# ---------------------------------------------------------------- the limitation in both docs (PT-27)

LIMITATION_FACTS: list[tuple[str, tuple[str, ...]]] = [
    ("instances do not carry the stack tags", (r"(?i)instances", r"`tags`", r"(?i)\bnot\b|\bonly\b")),
    ("the Prometheus volume does not carry them either", (r"(?i)Prometheus", r"(?i)volume")),
    ("only Project and nodeclass come from the NodeClass", (r"`Project`", r"`nodeclass`", r"NodeClass")),
    ("cost allocation by tag misses them", (r"(?i)cost", r"(?i)\btags?\b")),
    ("a tag-filtered describe-instances does not list them", (r"aws ec2 describe-instances", r"(?i)\bnot\b|\bmiss")),
    ("find them by the instance ID of the node", (r"providerID", r"kubectl get node")),
    ("or by the eks:eks-cluster-name tag", (r"`eks:eks-cluster-name`",)),
    ("changing the NodeClass tags replaces the nodes", (r"NodeClass", r"(?i)\btags?\b", r"(?i)replac")),
]


@pytest.mark.parametrize(("what", "patterns"), LIMITATION_FACTS, ids=[w for w, _ in LIMITATION_FACTS])
@pytest.mark.parametrize("doc", ["terraform/README.md", "docs/TROUBLESHOOTING.md"])
def test_tag_limitation_is_documented(doc: str, what: str, patterns: tuple[str, ...]) -> None:
    sec = _tf_tags_section() if doc == "terraform/README.md" else _trouble_tags_section()
    assert _has_sentence(sec, *patterns), f"{doc}: the Auto Mode tag limitation must say that {what} (one sentence: {patterns})"


def test_troubleshooting_tags_entry_links_the_terraform_readme_section() -> None:
    sec = _trouble_tags_section()
    assert re.search(r"\]\(\.\./terraform/README\.md#tags\)", sec), "link the Tags section of terraform/README.md"


def test_volume_of_a_persistent_volume_is_not_said_to_carry_project() -> None:
    """The PV volume is created by the StorageClass, not by a NodeClass: it does not get ``Project`` or ``nodeclass``."""
    for doc, sec in (("terraform/README.md", _tf_tags_section()), ("docs/TROUBLESHOOTING.md", _trouble_tags_section())):
        wrong = [s for s in _sentences(sec) if re.search(r"(?i)volumes? of persistent volumes? carry only", s)]
        assert not wrong, f"{doc}: the PV volume does not carry the NodeClass tags: {wrong}"


# ---------------------------------------------------------------- Grafana edge off by default (PT-30)

def test_enable_grafana_edge_defaults_to_false() -> None:
    m = re.search(r'variable "enable_grafana_edge"\s*\{(.*?)\n\}', _read(VARIABLES), re.DOTALL)
    assert m, "terraform/cluster/variables.tf: variable enable_grafana_edge"
    default = re.search(r"(?m)^\s*default\s*=\s*(\S+)", m.group(1))
    assert default and default.group(1) == "false", "enable_grafana_edge must default to false (port-forward is the path)"
    assert not re.search(r"(?m)^\s*enable_grafana_edge\s*=\s*true", _read(TFVARS_EXAMPLE)), "the tfvars example turns the edge on"


def test_terraform_readme_says_the_edge_is_off_and_port_forward_is_the_default() -> None:
    text = _read(TF_README)
    rows = [ln for ln in text.splitlines() if ln.startswith("| `enable_grafana_edge` |")]
    assert len(rows) == 1, "terraform/README.md variables table: one row for enable_grafana_edge"
    assert "| `false` |" in rows[0] and "kubectl port-forward" in rows[0], f"the row must say default `false` and port-forward: {rows[0]}"
    grafana = _flat(ds._section(text, r"Grafana"))
    assert re.search(r"^Default: `kubectl -n monitoring port-forward ", grafana), "the Grafana section starts with the port-forward"
    assert "`enable_grafana_edge = true`" in grafana, "the Grafana section says the edge needs enable_grafana_edge = true"


# ---------------------------------------------------------------- Grafana edge: the plain-HTTP origin hop

def _tf_grafana_section() -> str:
    sec = ds._section(_read(TF_README), r"Grafana")
    assert sec.strip(), "terraform/README.md needs its '## Grafana' section"
    return sec


def test_terraform_readme_documents_the_plain_http_origin_acknowledgement() -> None:
    rows = [ln for ln in _read(TF_README).splitlines() if ln.startswith("| `grafana_edge_allow_plain_http_origin` |")]
    assert len(rows) == 1, "terraform/README.md variables table: one row for grafana_edge_allow_plain_http_origin"
    assert "| `false` |" in rows[0], f"the acknowledgement defaults to `false`: {rows[0]}"
    assert "`grafana_edge_allow_plain_http_origin = true`" in _flat(_tf_grafana_section()), (
        "the Grafana section says the edge also needs grafana_edge_allow_plain_http_origin = true")


EDGE_HOP_FACTS: list[tuple[str, tuple[str, ...]]] = [
    ("the CloudFront-to-NLB hop is plain HTTP", (r"(?i)plain HTTP", r"CloudFront", r"\bNLB\b")),
    ("the Grafana login crosses it", (r"(?i)\blogin\b", r"(?i)password")),
    ("the session cookie crosses it", (r"(?i)session cookie",)),
    ("the API calls and their query results cross it", (r"(?i)\bAPI\b", r"(?i)quer")),
    ("the hop stays on the private VPC origin path to the internal NLB", (r"(?i)VPC origin", r"(?i)\binternal\b", r"(?i)private")),
    ("only the CloudFront VPC origins security group reaches the listener", (r"`CloudFront-VPCOrigins-Service-SG`",)),
    ("an NLB with a TLS listener cannot be a VPC origin", (r"(?i)TLS listener", r"(?i)VPC origin", r"(?i)\bnot\b|cannot")),
    ("TLS on the hop needs an internal ALB with an ACM certificate for a domain you own",
     (r"(?i)Application Load Balancer", r"\bACM\b", r"(?i)domain")),
    ("the origin domain, not the viewer Host header, must match that certificate", (r"(?i)origin domain", r"`Host`")),
    ("the plain-HTTP hop is an accepted trade-off of the optional edge", (r"(?i)accepted trade-off",)),
]


@pytest.mark.parametrize(("what", "patterns"), EDGE_HOP_FACTS, ids=[w for w, _ in EDGE_HOP_FACTS])
def test_terraform_readme_grafana_section_explains_the_plain_http_hop(what: str, patterns: tuple[str, ...]) -> None:
    assert _has_sentence(_tf_grafana_section(), *patterns), f"terraform/README.md, Grafana: say that {what} (one sentence: {patterns})"


def test_troubleshooting_explains_the_refused_edge_plan() -> None:
    sec = ds._section(_read(TROUBLE), r"enable_grafana_edge", level=3)
    assert sec.strip(), "docs/TROUBLESHOOTING.md needs an entry for the refused `enable_grafana_edge = true` plan"
    flat = _flat(sec)
    assert "`grafana_edge_allow_plain_http_origin = true`" in flat, "the entry names the acknowledgement"
    assert re.search(r"(?i)port-forward", flat), "the entry names the default path (kubectl port-forward)"
    assert re.search(r"\]\(\.\./terraform/README\.md#grafana\)", sec), "the entry links the Grafana section of terraform/README.md"


def test_tfvars_example_shows_the_acknowledgement_off() -> None:
    text = _read(TFVARS_EXAMPLE)
    assert re.search(r"(?m)^\s*grafana_edge_allow_plain_http_origin\s*=\s*false\b", text), (
        "the tfvars example sets grafana_edge_allow_plain_http_origin = false next to enable_grafana_edge")


# ---------------------------------------------------------------- Grafana edge: viewer TLS and the root URL

@pytest.mark.parametrize("name", ["grafana_edge_domain", "grafana_edge_viewer_certificate_arn"])
def test_terraform_readme_documents_the_custom_domain_variables(name: str) -> None:
    rows = [ln for ln in _read(TF_README).splitlines() if ln.startswith(f"| `{name}` |")]
    assert len(rows) == 1, f"terraform/README.md variables table: one row for {name}"
    assert '| `""` |' in rows[0], f"{name} is optional and defaults to empty: {rows[0]}"


EDGE_TLS_FACTS: list[tuple[str, tuple[str, ...]]] = [
    ("on the default *.cloudfront.net certificate CloudFront forces the TLSv1 security policy",
     (r"\*\.cloudfront\.net", r"`TLSv1`", r"(?i)security policy", r"(?i)CloudFront (?:sets|forces)")),
    ("a custom domain with its certificate gives viewers TLSv1.2_2021 over SNI",
     (r"`grafana_edge_domain`", r"`grafana_edge_viewer_certificate_arn`", r"`TLSv1\.2_2021`", r"\bSNI\b")),
    ("the certificate is an issued ACM certificate in us-east-1 that covers the domain",
     (r"\bACM\b", r"`us-east-1`", r"(?i)\bcovers\b")),
    ("for production use, set a domain and its certificate", (r"(?i)\bproduction\b", r"(?i)\bdomain\b", r"(?i)certificate")),
    ("a DNS record points the domain at the distribution", (r"(?i)\bDNS\b", r"`grafana_cloudfront_domain_name`")),
    ("the root URL is served from the Grafana page dashboards, never login",
     (r"(?i)default root object", r"`dashboards`", r"`login`")),
]


@pytest.mark.parametrize(("what", "patterns"), EDGE_TLS_FACTS, ids=[w for w, _ in EDGE_TLS_FACTS])
def test_terraform_readme_grafana_section_explains_the_viewer_tls(what: str, patterns: tuple[str, ...]) -> None:
    assert _has_sentence(_tf_grafana_section(), *patterns), f"terraform/README.md, Grafana: say that {what} (one sentence: {patterns})"


def test_troubleshooting_explains_the_refused_custom_domain_certificate() -> None:
    sec = ds._section(_read(TROUBLE), r"grafana_edge_viewer_certificate_arn", level=3)
    assert sec.strip(), "docs/TROUBLESHOOTING.md needs an entry for the refused `grafana_edge_viewer_certificate_arn`"
    flat = _flat(sec)
    for token in ("`grafana_edge_domain`", "`grafana_edge_viewer_certificate_arn`", "`us-east-1`"):
        assert token in flat, f"the entry names {token}"
    assert re.search(r"(?i)together", flat), "the entry says the two variables are set together"
    assert re.search(r"\]\(\.\./terraform/README\.md#grafana\)", sec), "the entry links the Grafana section of terraform/README.md"


# ---------------------------------------------------------------- incident response runbook (PT-57)

def test_incident_response_runbook_exists_and_security_links_it() -> None:
    assert _read(INCIDENT).strip()
    assert re.search(r"\]\((?:\./)?docs/INCIDENT-RESPONSE\.md\)", _read(SECURITY)), "SECURITY.md links docs/INCIDENT-RESPONSE.md"
    assert re.search(r"\]\(\.\./SECURITY\.md\)", _read(INCIDENT)), "the runbook points back to SECURITY.md for the report channel"


INCIDENT_FACTS: list[tuple[str, tuple[str, ...]]] = [
    ("what it covers: a secret or an AWS account ID", (r"(?i)secret", r"(?i)account ID")),
    ("rotate the credential", (r"(?i)\brotat",)),
    ("revoke the credential", (r"(?i)\brevok",)),
    ("remove it from the default branch", (r"(?i)default branch",)),
    ("the git history keeps it", (r"(?i)\bhistory\b",)),
    ("git filter-repo is not enough on its own", (r"git filter-repo", r"(?i)not enough")),
    ("GitHub support purges cached views and pull request refs", (r"(?i)GitHub Support", r"(?i)cached", r"(?i)pull request")),
    ("check CloudTrail for use of the key", (r"CloudTrail",)),
    ("report it", (r"(?i)\breport",)),
    ("re-scan with the publish guards", (r"tests/publish",)),
    ("re-scan with detect-secrets", (r"detect-secrets",)),
    ("HF_TOKEN only from the process environment", (r"HF_TOKEN", r"(?i)process environment")),
    ("never in the generated env files or the tfvars",
     (r"(?i)\bnever\b", r"config/sample\.env", r"manifests/config/platform\.env", r"terraform\.tfvars")),
    ("the Secrets Manager option", (r"hf_token_secret_arn",)),
    ("the account ID placeholder", (r"123456789012",)),
]


@pytest.mark.parametrize(("what", "patterns"), INCIDENT_FACTS, ids=[w for w, _ in INCIDENT_FACTS])
def test_incident_response_runbook_covers(what: str, patterns: tuple[str, ...]) -> None:
    assert _has_sentence(_read(INCIDENT), *patterns), f"docs/INCIDENT-RESPONSE.md must cover: {what} (one sentence: {patterns})"


def test_incident_response_rotates_before_it_cleans_up() -> None:
    """Stop using the credential first: rotating or revoking comes before the branch and history clean-up."""
    text = _flat(_read(INCIDENT))
    first = re.search(r"(?i)\b(?:rotat|revok)", text)
    assert first, "rotate/revoke missing"
    for later in (r"(?i)default branch", r"git filter-repo"):
        m = re.search(later, text)
        assert m and first.start() < m.start(), f"rotate/revoke must come before {later!r}"


@pytest.mark.parametrize(("pattern", "what"), INTERNAL_NAMES, ids=[w + ":" + str(i) for i, (_, w) in enumerate(INTERNAL_NAMES)])
def test_incident_response_names_no_internal_host_or_ticket_system(pattern: str, what: str) -> None:
    hits = re.findall(pattern, _read(INCIDENT))
    assert not hits, f"docs/INCIDENT-RESPONSE.md is public: no {what} ({hits[:3]})"


def test_incident_response_passes_the_public_docs_deny_list() -> None:
    text = _read(INCIDENT)
    bad = [f"{what}: {m.group(0)[:50]!r}" for pat, what in ds.FORBIDDEN for m in re.finditer(pat, text)]
    assert not bad, f"docs/INCIDENT-RESPONSE.md: {bad[:10]}"
    assert not sanitize_scan.scan_text(text), "docs/INCIDENT-RESPONSE.md carries an identifier the tree scanner flags"


def test_incident_response_relative_links_resolve() -> None:
    text = _read(INCIDENT)
    bad = []
    for m in re.finditer(r"(?<!!)\[[^\]]*\]\(([^)\s]+)\)", text):
        target = m.group(1)
        if re.match(r"[a-z]+:", target):
            continue
        path_part, _, anchor = target.partition("#")
        dest = (INCIDENT.parent / path_part).resolve() if path_part else INCIDENT
        if not dest.exists():
            bad.append(f"{target} (no such file)")
        elif anchor and dest.suffix == ".md":
            heads = {ds._slug(h) for h in re.findall(r"^#+ (.+)$", dest.read_text(encoding="utf-8"), re.MULTILINE)}
            if anchor not in heads:
                bad.append(f"{target} (no such heading)")
    assert not bad, f"docs/INCIDENT-RESPONSE.md: broken links {bad}"
