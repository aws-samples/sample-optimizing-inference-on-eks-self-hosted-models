# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The optimization guides name the source of the figures they quote.

The data shipped with the sample is W3-16K only: ``docs/results/w3-16k-cells.csv`` and
``docs/results/w3-16k-per-minute.csv``, one run per cell and rate (n=1), method in ``workloads/w3-16k/METHODOLOGY.md``.
The guides also quote W2 figures (n=3 per cell, the results of record, run with the plans of ``plans/w2-long-context``)
and E1 figures (n=1, the functional checks before the matrix), whose exports are not in the repository.

What is checked, in each guide of ``GUIDES``
============================================
* Its Evidence section holds exactly one source paragraph (it starts with ``**Source``), placed before the first W2
  paragraph (it starts with ``**W2``), which must exist.
* The source paragraph links both CSV files and the methodology (the links resolve), names the W2 plans directory, gives
  n=1 for W3-16K and for E1 and n=3 for W2, names the derived SGLang 0.5.22 build, and says that the W2 and E1 figures
  of the guide are not in the shipped data.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import sanitize_scan

ROOT = sanitize_scan.REPO_ROOT
GUIDES = {name: ROOT / "docs/optimizations" / name / "README.md"
          for name in ("kv-router", "hicache", "pd-disaggregation")}
EVIDENCE_HEADING = "Evidence (W3-16K, n=1)"
LINKED = ("docs/results/w3-16k-cells.csv", "docs/results/w3-16k-per-minute.csv", "workloads/w3-16k/METHODOLOGY.md")
REQUIRED = ("plans/w2-long-context", "n=1", "n=3", "W2", "E1", "SGLang 0.5.22", "this guide")
NOT_SHIPPED = re.compile(r"(?i)not in the shipped data")


def _evidence(path: Path) -> str:
    """Body of the Evidence section of a guide (up to the next heading of the same or a higher level)."""
    text = path.read_text(encoding="utf-8")
    m = re.search(rf"^(#+) .*{re.escape(EVIDENCE_HEADING)}.*$", text, re.MULTILINE)
    assert m, f"{path.relative_to(ROOT)}: no '{EVIDENCE_HEADING}' heading"
    rest = text[m.end():]
    end = re.search(rf"^#{{1,{len(m.group(1))}}} ", rest, re.MULTILINE)
    return rest[:end.start()] if end else rest


def _paragraphs(body: str) -> list[str]:
    return [p.strip() for p in re.split(r"\n\s*\n", body) if p.strip()]


def _source(name: str) -> tuple[list[str], int]:
    paras = _paragraphs(_evidence(GUIDES[name]))
    found = [i for i, p in enumerate(paras) if p.startswith("**Source")]
    assert len(found) == 1, f"{name}: the Evidence section needs exactly one '**Source' paragraph, found {len(found)}"
    return paras, found[0]


@pytest.mark.parametrize("name", sorted(GUIDES))
def test_source_paragraph_comes_before_the_first_w2_figures(name: str) -> None:
    paras, i = _source(name)
    w2 = [j for j, p in enumerate(paras) if p.startswith("**W2")]
    assert w2, f"{name}: no '**W2' paragraph in the Evidence section"
    assert i < w2[0], f"{name}: the source paragraph must come before the first W2 paragraph"


@pytest.mark.parametrize("name", sorted(GUIDES))
def test_source_paragraph_names_the_data_the_runs_and_the_build(name: str) -> None:
    paras, i = _source(name)
    para = " ".join(paras[i].split())
    missing = [t for t in REQUIRED if t not in para]
    assert not missing, f"{name}: the source paragraph lacks {missing}"
    assert NOT_SHIPPED.search(para), f"{name}: say that the W2 and E1 figures are not in the shipped data"
    targets = {(GUIDES[name].parent / t).resolve() for t in re.findall(r"\]\(([^)#\s]+)(?:#[^)]*)?\)", para)}
    bad = [t for t in targets if not t.exists()]
    assert not bad, f"{name}: broken links in the source paragraph: {bad}"
    linked = {t.relative_to(ROOT.resolve()).as_posix() for t in targets if t.is_relative_to(ROOT.resolve())}
    absent = [t for t in LINKED if t not in linked]
    assert not absent, f"{name}: the source paragraph must link {absent}"
