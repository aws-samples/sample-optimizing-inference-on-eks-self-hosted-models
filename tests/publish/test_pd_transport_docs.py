# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The PD guide carries no unsourced transport claim.

* the whole PD guide: no bandwidth figure without a source link or the unpublished-measurement marker, no price, no customer
  data (these checks read the whole guide so that its headings are not pinned).

A missing guide is a failure, never a skip.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import test_architecture_reference as P

ROOT = P.ROOT
GUIDE = P.GUIDE
MARKER = P.MARKER

BANDWIDTH = re.compile(r"\b\d+(?:[.,]\d+)?\s?(?:GB/s|GBps|Gbps|Gb/s|TB/s|Tbps)\b")
# Figures the guide carried without a link or the marker when this check first read them ("37 GB/s", "1-3 GB/s") were
# removed with the rewrite of the guide; no figure is exempt.
UNSOURCED_BEFORE_THE_CHECK: dict[str, set[str]] = {"guide": set()}


def _read(p: Path) -> str:
    assert p.is_file(), f"{p.relative_to(ROOT)} missing"
    return p.read_text(encoding="utf-8")


def _as_quoted(sentence: str, m: re.Match[str]) -> str:
    """The figure ``m`` as the sentence quotes it: with the first number of a range in front of it (``1-3 GB/s``)."""
    low = re.search(r"\d+(?:[.,]\d+)?-$", sentence[:m.start()])
    return (low.group(0) if low else "") + m.group(0)


BLOCKS = {"guide": lambda: _read(GUIDE)}


@pytest.mark.parametrize("which", BLOCKS)
def test_a_bandwidth_figure_carries_a_source_link_or_the_unpublished_marker(which: str) -> None:
    """No invented spec: a GB/s figure is an unpublished measurement or sits in a paragraph with an AWS / NVIDIA link."""
    exempt = UNSOURCED_BEFORE_THE_CHECK[which]
    for para in re.split(r"\n\s*\n", BLOCKS[which]()):
        for sentence in P._sentences(para):
            for m in BANDWIDTH.finditer(sentence):
                if MARKER.search(sentence) or m.group(0) in exempt or _as_quoted(sentence, m) in exempt:
                    continue
                assert re.search(r"https://(docs\.aws\.amazon\.com|aws\.amazon\.com|www\.nvidia\.com|docs\.nvidia\.com|developer\.nvidia\.com)", para), (
                    f"{which}: unsourced bandwidth figure {m.group(0)!r}: {sentence!r}")


@pytest.mark.parametrize("which", BLOCKS)
def test_the_guide_has_no_price_and_no_customer_data(which: str) -> None:
    text = BLOCKS[which]()
    assert not re.search(r"US\$|\bUSD\b|\bprice\b|\bpricing\b|\$\d", text, re.I)
    assert not re.search(r"(?i)\bcustomer\b", text)
