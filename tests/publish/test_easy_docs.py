# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Easy docs: the Auto Mode tag limitation and the Grafana edge default, checked against the code.

What is checked
===============
* Tag limitation: every NodeClass sets exactly ``Project`` and ``nodeclass``, and the ``auto-ebs-sc`` StorageClass sets no
  tag, so the Auto Mode instances and the Prometheus volume do not carry the stack ``tags``.  If the code changes, these
  tests ask for the docs to change with it.  docs/TROUBLESHOOTING.md links the ``Tags`` section of terraform/README.md.
* Grafana edge: ``enable_grafana_edge`` defaults to ``false`` (the tfvars example does not turn it on).  The
  CloudFront-to-NLB hop is plain HTTP: ``grafana_edge_allow_plain_http_origin`` defaults to ``false``.  terraform/README.md
  lists these variables and ``grafana_edge_domain`` and ``grafana_edge_viewer_certificate_arn`` (default empty) in its
  variables table; docs/TROUBLESHOOTING.md has an entry for each refused plan and links the Grafana section.
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
VARIABLES = ROOT / "terraform/cluster/variables.tf"
TFVARS_EXAMPLE = ROOT / "terraform/cluster/terraform.tfvars.example"
NODEPOOL_TF = ROOT / "terraform/cluster/nodepool.tf"
GPU_POOLS_TF = ROOT / "terraform/cluster/modules/gpu-pools/main.tf"


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


# ---------------------------------------------------------------- the TROUBLESHOOTING entry links the Tags section

def test_troubleshooting_tags_entry_links_the_terraform_readme_section() -> None:
    sec = _trouble_tags_section()
    assert re.search(r"\]\(\.\./terraform/README\.md#tags\)", sec), "link the Tags section of terraform/README.md"


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


def test_troubleshooting_explains_the_refused_custom_domain_certificate() -> None:
    sec = ds._section(_read(TROUBLE), r"grafana_edge_viewer_certificate_arn", level=3)
    assert sec.strip(), "docs/TROUBLESHOOTING.md needs an entry for the refused `grafana_edge_viewer_certificate_arn`"
    flat = _flat(sec)
    for token in ("`grafana_edge_domain`", "`grafana_edge_viewer_certificate_arn`", "`us-east-1`"):
        assert token in flat, f"the entry names {token}"
    assert re.search(r"(?i)together", flat), "the entry says the two variables are set together"
    assert re.search(r"\]\(\.\./terraform/README\.md#grafana\)", sec), "the entry links the Grafana section of terraform/README.md"

