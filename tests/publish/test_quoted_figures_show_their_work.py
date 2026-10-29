# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""A quoted number shows its work next to it: the cost figures and the unpublished W2 and E1 figures.

What is checked
===============
* Cost figures (``US$ <n>``) in ``COST_DOCS``: the paragraph or list item of each one names an AWS region, the pricing
  date (``as of YYYY-MM-DD``) and links an AWS pricing page (``https://aws.amazon.com/<service>/pricing/``), the AWS
  Pricing Calculator or the breakdown in docs/DEPLOY.md (``DEPLOY.md#teardown``).
* The idle cost of docs/DEPLOY.md (section Teardown): its paragraph says On-Demand and what it leaves out (data transfer,
  GPU nodes) and links the EKS, VPC, EC2 and EBS pricing pages; the breakdown table lists the EKS control plane, the NAT
  gateway, the public IPv4 address, at least two instance types and EBS; each rate cell (``a + b`` or ``a × b ÷ c``)
  gives its per-hour cell, the rows add up to the total and the total rounds to the figure of the paragraph.  The EBS
  row names the two volume sizes Terraform sets (the ``cpu-demo`` data volume and the Prometheus volume) and bills their
  sum.  The section also points to the AWS Pricing Calculator for another region.  README.md quotes the same figure.
* The W2 and E1 figures of the KV router and HiCache guides, and the W2 moved-continuation shares the PD guide repeats,
  have no raw data in the repository: every occurrence of each figure in ``UNPUBLISHED`` is followed, in the same
  sentence (or table cell), by the marker ``(unpublished internal measurement)`` or its plural, and the source paragraph
  of the two guides says that these figures are unpublished internal measurements that cannot be reproduced from this
  repository.  The W2 pairs that show their raw values (``RAW_PAIRS``) stay as they are.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import sanitize_scan

ROOT = sanitize_scan.REPO_ROOT
README = ROOT / "README.md"
DEPLOY = ROOT / "docs/DEPLOY.md"
TF_README = ROOT / "terraform/README.md"
COST_DOCS = [README, DEPLOY, TF_README]
NODEPOOL_TF = ROOT / "terraform/cluster/nodepool.tf"
VARIABLES_TF = ROOT / "terraform/cluster/variables.tf"
GUIDE = {name: ROOT / "docs/optimizations" / name / "README.md" for name in ("kv-router", "hicache", "pd-disaggregation")}

COST = re.compile(r"US\$ ?\d[\d,]*(?:\.\d+)?")
REGION = re.compile(r"\b(?:us|eu|ap|sa|ca|me|af|il|mx)-(?:north|south|east|west|central)(?:east|west)?-\d\b")
PRICED_AS_OF = re.compile(r"\bas of 20\d\d-\d\d-\d\d\b")
PRICING_LINK = re.compile(
    r"\]\((?:https://aws\.amazon\.com/[a-z0-9-]+/pricing/[^)]*|https://calculator\.aws/?|(?:\.\./)*(?:docs/)?DEPLOY\.md#teardown)\)")
SERVICE_PAGES = ("eks", "vpc", "ec2", "ebs")
INSTANCE_TYPE = re.compile(r"`([a-z]\d+[a-z-]*\.(?:\d+x)?(?:x?large|medium|small|micro|nano|metal))`")
MARKER = re.compile(r"\(unpublished internal measurements?\)")

# Figures without raw data in the repository, by guide (exact text as quoted).
UNPUBLISHED: dict[str, list[str]] = {
    "hicache": ["÷1.03", "+17.2 points", "49% of the cached tokens", "1.31x", "1.27x", "87% of the cached tokens",
                "54.5% of the prompt tokens", "0.8% of the prompt tokens"],
    "kv-router": ["+6.4 points", "S0 85.9%", "S1 78.7%", "S4 65.9%", "S7 12.0%", "72.5%", "1.02x without O",
                  "1.28x with it", "10/10 sessions", "30/30", "0.102 s", "0/30", "1.27 s", "40/40", "66.8%", "87.6%",
                  "23.3K tokens"],
    "pd-disaggregation": ["12.0% of S7 continuations", "65.9% in S4"],
}
SOURCE_SENTENCE_GUIDES = ("hicache", "kv-router")
RAW_PAIRS = {
    "hicache": "goodput 1.02x (0.264 vs 0.260 turns/s)",
    "kv-router": "S1 vs S0 goodput 1.07x (0.279 vs 0.260 turns/s)",
}


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


def _blocks(text: str) -> list[str]:
    """Split Markdown into paragraphs and list items, each flattened to one line.

    Args:
        text: Markdown source.

    Returns:
        One string per paragraph, per list item and per table.
    """
    out: list[str] = []
    for para in re.split(r"\n\s*\n", text):
        item: list[str] = []
        for ln in para.splitlines():
            if item and re.match(r"\s{0,3}(?:[-*]|\d+\.)\s", ln):
                out.append(_flat("\n".join(item)))
                item = []
            item.append(ln)
        if item:
            out.append(_flat("\n".join(item)))
    return out


def _sentences(text: str) -> list[str]:
    """Split Markdown into sentences; a table cell is its own unit, so a sentence never spans two cells.

    Args:
        text: Markdown source.

    Returns:
        The sentences, each flattened to one line.
    """
    out: list[str] = []
    for para in re.split(r"\n\s*\n", text):
        lines = para.splitlines()
        units = [c for ln in lines for c in ln.split("|")] if lines and all(ln.lstrip().startswith("|") for ln in lines) \
            else [para]
        for unit in units:
            out += [s for s in re.split(r"(?<=[.!?])\s+", _flat(unit)) if s]
    return out


def _section(text: str, heading: str) -> str:
    """Body of the H2 section whose title contains ``heading`` (subsections included)."""
    m = re.search(rf"^## .*{re.escape(heading)}.*$", text, re.MULTILINE)
    assert m, f"no '## {heading}' section"
    rest = text[m.end():]
    end = re.search(r"^#{1,2} ", rest, re.MULTILINE)
    return rest[:end.start()] if end else rest


def _number(cell: str) -> float:
    """The first decimal number of a table cell (bold and spaces ignored)."""
    m = re.search(r"\d+(?:\.\d+)?", cell)
    assert m, f"no number in {cell!r}"
    return float(m.group(0))


def _evaluate(rate: str) -> float:
    """Evaluate a rate cell left to right: numbers joined by ``+``, ``×`` or ``÷`` (words and units ignored).

    Args:
        rate: The rate cell, e.g. ``0.068 + 0.00816 per hour`` or ``0.08 per GB-month × 306 GiB ÷ 730 hours``.

    Returns:
        The value of the expression.
    """
    tokens = re.findall(r"\d+(?:\.\d+)?|[+×÷]", rate)
    assert tokens and tokens[0] not in "+×÷", f"rate {rate!r} does not start with a number"
    value, op = float(tokens[0]), ""
    for tok in tokens[1:]:
        if tok in "+×÷":
            op = tok
            continue
        assert op, f"rate {rate!r}: two numbers without an operator"
        value = value + float(tok) if op == "+" else value * float(tok) if op == "×" else value / float(tok)
        op = ""
    return value


def _gib(value: str) -> int:
    """Size in GiB of a Terraform size literal such as ``256Gi`` or ``1Ti``."""
    m = re.fullmatch(r"(\d+)(Gi|Ti)", value)
    assert m, f"unexpected size literal {value!r}"
    return int(m.group(1)) * (1024 if m.group(2) == "Ti" else 1)


def _idle_paragraph() -> str:
    paras = [b for b in _blocks(_section(_read(DEPLOY), "Teardown")) if COST.search(b) and "per hour" in b]
    assert len(paras) == 1, f"docs/DEPLOY.md Teardown: want one paragraph quoting the idle cost per hour, found {len(paras)}"
    return paras[0]


def _idle_figure() -> float:
    m = re.search(r"US\$ ?(\d+\.\d+) per hour", _idle_paragraph())
    assert m, "the idle cost paragraph must say 'US$ <n> per hour'"
    return float(m.group(1))


def _breakdown() -> list[list[str]]:
    """Rows of the idle-cost table of the Teardown section: the first table whose header names a per-hour column."""
    sec = _section(_read(DEPLOY), "Teardown")
    for para in re.split(r"\n\s*\n", sec):
        lines = [ln.strip() for ln in para.splitlines() if ln.strip().startswith("|")]
        if len(lines) > 2 and re.search(r"(?i)per hour", lines[0]):
            return [[c.strip() for c in ln.strip("|").split("|")] for ln in lines[2:]]
    pytest.fail("docs/DEPLOY.md Teardown: no breakdown table with a per-hour column")


# ----------------------------------------------------------------------------------------------------------- cost figures

@pytest.mark.parametrize("p", COST_DOCS, ids=lambda p: p.relative_to(ROOT).as_posix())
def test_every_cost_figure_names_region_date_and_pricing_source(p: Path) -> None:
    bad = []
    for block in _blocks(_read(p)):
        cost = COST.search(block)
        if cost is None:
            continue
        missing = [what for what, rx in (("an AWS region", REGION), ("'as of YYYY-MM-DD'", PRICED_AS_OF),
                                         ("a pricing link", PRICING_LINK)) if not rx.search(block)]
        if missing:
            bad.append(f"{cost.group(0)!r} lacks {', '.join(missing)}: {block[:160]}")
    assert not bad, f"{p.relative_to(ROOT)}: cost figures without their source:\n  " + "\n  ".join(bad)


def test_deploy_idle_cost_paragraph_says_what_it_covers() -> None:
    para = _idle_paragraph()
    assert re.search(r"On-Demand", para), "say the prices are On-Demand"
    for left_out in (r"data transfer", r"GPU nodes?"):
        assert re.search(rf"(?i)exclud\w*[^.]*{left_out}|{left_out}[^.]*exclud\w*", para), f"say it excludes {left_out}"
    links = set(re.findall(r"\]\(https://aws\.amazon\.com/([a-z0-9-]+)/pricing/", para))
    assert set(SERVICE_PAGES) <= links, f"link the pricing pages of {sorted(set(SERVICE_PAGES) - links)}"


def test_deploy_idle_cost_breakdown_adds_up() -> None:
    rows = _breakdown()
    total = [r for r in rows if re.search(r"(?i)total", r[0])]
    items = [r for r in rows if not re.search(r"(?i)total", r[0])]
    assert len(total) == 1 and items, "the breakdown needs component rows and one Total row"
    names = " ".join(r[0] for r in items)
    for comp in (r"EKS control plane", r"NAT gateway", r"public IPv4", r"EBS"):
        assert re.search(rf"(?i){comp}", names), f"the breakdown lacks {comp}"
    assert len(set(INSTANCE_TYPE.findall(names))) >= 2, "name the instance types of the idle nodes"
    for r in items:
        assert round(_evaluate(r[1]), 3) == pytest.approx(_number(r[-1]), abs=1e-9), f"{r[0]}: {r[1]} is not {r[-1]}"
    hourly = sum(_number(r[-1]) for r in items)
    assert hourly == pytest.approx(_number(total[0][-1]), abs=0.0005 * len(items)), "the rows do not add up to the total"
    assert round(_number(total[0][-1]), 2) == pytest.approx(_idle_figure()), "the total does not round to the quoted figure"


def test_deploy_ebs_row_bills_the_volumes_terraform_sets() -> None:
    ebs = next((r for r in _breakdown() if "EBS" in r[0]), None)
    assert ebs, "no EBS row"
    data_m = re.search(r'ephemeralStorage = \{\s*size\s*=\s*"(\w+)"', _read(NODEPOOL_TF))
    prom_m = re.search(r'variable "prometheus_storage_size" \{[^}]*default\s*=\s*"(\w+)"', _read(VARIABLES_TF))
    assert data_m and prom_m, "the cpu-demo ephemeralStorage size or the prometheus_storage_size default moved"
    data, prom = _gib(data_m.group(1)), _gib(prom_m.group(1))
    for size in (data, prom):
        assert re.search(rf"\b{size} GiB\b", ebs[0]), f"the EBS row must name the {size} GiB volume"
    assert re.search(rf"\b{data + prom} GiB\b", ebs[1]), f"the EBS rate must bill {data + prom} GiB"


def test_deploy_points_to_the_pricing_calculator_and_readme_quotes_the_same_figure() -> None:
    assert "https://calculator.aws" in _section(_read(DEPLOY), "Teardown"), "point to the AWS Pricing Calculator"
    m = re.search(r"US\$ ?(\d+\.\d+) per hour", _flat(_read(README)))
    assert m, "README.md quotes the idle cost per hour"
    assert float(m.group(1)) == pytest.approx(_idle_figure()), "README.md and docs/DEPLOY.md quote different idle costs"


# --------------------------------------------------------------------------------------------- unpublished W2 and E1 figures

@pytest.mark.parametrize(("guide", "figure"), [(g, f) for g, figs in UNPUBLISHED.items() for f in figs])
def test_unpublished_figure_carries_the_marker(guide: str, figure: str) -> None:
    hits = [s for s in _sentences(_read(GUIDE[guide])) if figure in s]
    assert hits, f"{guide}: the figure {figure!r} is gone (mark it, do not delete it)"
    bad = [s for s in hits if not all(MARKER.search(s, m.end()) for m in re.finditer(re.escape(figure), s))]
    assert not bad, f"{guide}: {figure!r} lacks a later '(unpublished internal measurement)' in: {bad[0][:200]}"


@pytest.mark.parametrize("guide", SOURCE_SENTENCE_GUIDES)
def test_source_paragraph_says_the_figures_cannot_be_reproduced(guide: str) -> None:
    paras = [_flat(p) for p in re.split(r"\n\s*\n", _read(GUIDE[guide])) if p.strip().startswith("**Source")]
    assert len(paras) == 1, f"{guide}: want one '**Source' paragraph"
    assert re.search(r"unpublished internal measurements?", paras[0]), f"{guide}: call them unpublished internal measurements"
    assert "cannot be reproduced from this repository" in paras[0], f"{guide}: say they cannot be reproduced from it"


@pytest.mark.parametrize("guide", sorted(RAW_PAIRS))
def test_w2_pair_with_raw_values_is_kept(guide: str) -> None:
    assert RAW_PAIRS[guide] in _flat(_read(GUIDE[guide])), f"{guide}: keep {RAW_PAIRS[guide]!r} as it is"
