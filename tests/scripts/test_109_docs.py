# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Docs contract of the Qwen3.8-27B-FP8 cells: the three optimization guides.

Contract
========
* The three guides carry no account id, bucket URL, price, internal link or path, or reservation id.
  The headings, wording and quoted numbers of the guides are NOT pinned here: the guides may be rewritten freely.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
DOCS = {
    "kv-router": ROOT / "docs/optimizations/kv-router/README.md",
    "hicache": ROOT / "docs/optimizations/hicache/README.md",
    "pd-disaggregation": ROOT / "docs/optimizations/pd-disaggregation/README.md",
}
BAN = [
    (r"\b\d{12}\b", "12-digit account id"), (r"s3://", "bucket URL"), (r"US\$|USD|\$\s?\d", "price"),
    (r"https?://[^\s)]*(?:amazon|a2z|aws\.dev|gitlab)", "internal link"), (r"kubecon" + r"-artifacts", "internal path"),
    (r"\bcr-[0-9a-f]{8,}", "reservation id"),
]


def _text(p: Path) -> str:
    assert p.exists(), f"{p.relative_to(ROOT)} missing"
    return p.read_text(encoding="utf-8")


# ---------------------------------------------------------------- guides

@pytest.mark.parametrize("doc", list(DOCS))
def test_guides_have_no_banned_terms(doc: str) -> None:
    t = _text(DOCS[doc])
    for pat, what in BAN:
        m = re.search(pat, t)
        assert not m, f"{doc}: contains a {what}: {m.group(0) if m else ''}"
