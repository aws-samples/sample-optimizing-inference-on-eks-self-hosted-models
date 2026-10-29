# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Docs contract for Track Q (Qwen3.8-27B-FP8): the three optimization guides, docs/results and the design-log trackers.

Source of the numbers: ``docs/results/w3-16k-cells.csv`` (a no-prompt copy of the W3-16K per-cell table, n=1); the tests
parse the numbers they expect from that CSV and require them in the guides, so a guide can never quote a figure the table
does not hold.  The CSV itself is checked against the analysis file of the run when present (``W3_CELLS_CSV``; those
cross-checks skip when it is unset).

Contract
========
* ``docs/results/w3-16k-cells.csv``: 24 rows (rates 10/25/32 x cells S0..S7), columns ``rate, cell, R, O, D, status, valid,
  goodput_window_turns_s, attainment_window, cont_ttft_p50_s, cont_ttft_p90_s, itl_req_p50_ms, hit_window_client, moved_share``
  (extra columns allowed except prompt/text ones).  ``docs/results/README.md``: n=1, r50 inconclusive, the three failures.
* Each guide has headings ``Evidence (W3-16K, n=1)``, ``When it helps / when it does not`` and ``Versions``; ``Versions``
  names the runs of record.  Numbers: percent as integer or one decimal, seconds with 1-2 decimals, ms as integer or one
  decimal, each followed by its unit.
* The original lines of the design-log trackers (docs/sdd/Task.md, spec.md) stay in order (baseline = tip 240b23a).
"""

from __future__ import annotations

import csv
import os
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SDD = ROOT / "docs" / "sdd"  # spec.md, plan.md and Task.md live here (moved from the repo root)
BASE = "240b23a"
DOCS = {
    "kv-router": ROOT / "docs/optimizations/kv-router/README.md",
    "hicache": ROOT / "docs/optimizations/hicache/README.md",
    "pd-disaggregation": ROOT / "docs/optimizations/pd-disaggregation/README.md",
}
RESULTS = ROOT / "docs/results"
CELLS = RESULTS / "w3-16k-cells.csv"
SOURCE = Path(os.environ.get("W3_CELLS_CSV", "/nonexistent/w3-full-cells.csv"))  # analysis output of the run, not in the repository
COLS = ["rate", "cell", "R", "O", "D", "status", "valid", "goodput_window_turns_s", "attainment_window", "cont_ttft_p50_s",
        "cont_ttft_p90_s", "itl_req_p50_ms", "hit_window_client", "moved_share"]
NUMERIC = COLS[7:]
BAN = [
    (r"\b\d{12}\b", "12-digit account id"), (r"s3://", "bucket URL"), (r"US\$|USD|\$\s?\d", "price"),
    (r"https?://[^\s)]*(?:amazon|a2z|aws\.dev|gitlab)", "internal link"), (r"kubecon" + r"-artifacts", "internal path"),
    (r"\bcr-[0-9a-f]{8,}", "reservation id"),
]
ARROW = r"\s*(?:->|→)\s*"
EVIDENCE_HEADING = "Evidence (W3-16K, n=1)"
EVIDENCE_RE = re.escape(EVIDENCE_HEADING)


def _text(p: Path) -> str:
    assert p.exists(), f"{p.relative_to(ROOT)} missing"
    return p.read_text(encoding="utf-8")


def _section(text: str, heading_re: str) -> str:
    m = re.search(rf"^(#+) .*{heading_re}.*$", text, re.M | re.I)
    if not m:
        return ""
    level = len(m.group(1))
    rest = text[m.end():]
    end = re.search(rf"^#{{1,{level}}} ", rest, re.M)
    return rest[: end.start()] if end else rest


def _rows() -> dict[tuple[int, str], dict[str, str]]:
    assert CELLS.exists(), "docs/results/w3-16k-cells.csv missing"
    with CELLS.open(newline="", encoding="utf-8") as f:
        return {(int(r["rate"]), r["cell"]): r for r in csv.DictReader(f)}


def _fv(x: str) -> float | None:
    return float(x) if x.strip() else None


# ---------------------------------------------------------------- docs/results

def test_cells_csv_has_24_rows_and_required_columns_and_no_prompt_text() -> None:
    assert CELLS.exists(), "docs/results/w3-16k-cells.csv missing"
    with CELLS.open(newline="", encoding="utf-8") as f:
        rd = csv.DictReader(f)
        header = rd.fieldnames or []
        rows = list(rd)
    assert [c for c in COLS if c not in header] == [], f"missing columns {[c for c in COLS if c not in header]}"
    assert not [c for c in header if re.search(r"prompt|text|content|message", c, re.I)], "no prompt text in the table"
    assert len(rows) == 24
    assert {(int(r["rate"]), r["cell"]) for r in rows} == {(r, f"S{i}") for r in (10, 25, 32) for i in range(8)}


def test_cells_csv_matches_the_analysis_source() -> None:
    if not SOURCE.exists():
        pytest.skip("analysis source not available")
    with SOURCE.open(newline="", encoding="utf-8") as f:
        src = {(int(r["rate"]), r["cell"]): r for r in csv.DictReader(f)}
    mine = _rows()
    assert mine.keys() == src.keys()
    bad = []
    for k, s in src.items():
        m = mine[k]
        for c in ("R", "O", "D", "status", "valid"):
            if m[c] != s[c]:
                bad.append((k, c, m[c], s[c]))
        for c in NUMERIC:
            a, b = _fv(m[c]), _fv(s[c])
            if (a is None) != (b is None) or (a is not None and b is not None and abs(a - b) > 1e-9 * max(1.0, abs(b))):
                bad.append((k, c, m[c], s[c]))
    assert not bad, f"values differ from the source: {bad[:5]}"


def test_results_readme_labels_n1_and_lists_failures() -> None:
    t = _text(RESULTS / "README.md")
    low = t.lower()
    for w in ("n=1", "r50", "inconclusive", "s5", "reset", "s3", "crash", "w3-16k-cells.csv"):
        assert w in low, f"docs/results/README.md lacks {w!r}"
    assert "undetermined" in low, "r32 crash cause must be stated as undetermined"
    for pat, what in BAN:
        assert not re.search(pat, t), f"docs/results/README.md contains a {what}"


def test_results_files_have_no_banned_terms() -> None:
    for p in RESULTS.glob("*"):
        t = p.read_text(encoding="utf-8")
        for pat, what in BAN:
            assert not re.search(pat, t), f"{p.name} contains a {what}"


# ---------------------------------------------------------------- guides

def _pct(v: float) -> list[str]:
    return [f"{v * 100:.0f}%", f"{v * 100:.1f}%"]


def _sec(v: float) -> list[str]:
    return [f"{v:.1f} s", f"{v:.2f} s"]


def _ms(v: float) -> list[str]:
    return [f"{v:.0f} ms", f"{v:.1f} ms"]


# (rate, cell, column, renderer): the figures each guide must carry, read from the CSV.
Need = tuple[int, str, str]
NEEDS: dict[str, list[Need]] = {
    "kv-router": [
        (25, "S0", "attainment_window"), (25, "S0", "cont_ttft_p50_s"), (25, "S0", "itl_req_p50_ms"),
        (25, "S1", "cont_ttft_p50_s"), (25, "S1", "attainment_window"),
        (25, "S7", "attainment_window"), (25, "S7", "cont_ttft_p50_s"),
        (10, "S7", "moved_share"), (25, "S7", "moved_share"), (32, "S7", "moved_share"),
        (25, "S0", "moved_share"),
    ],
    "hicache": [
        (25, "S0", "cont_ttft_p50_s"), (25, "S0", "itl_req_p50_ms"),
        (25, "S2", "cont_ttft_p50_s"), (25, "S2", "itl_req_p50_ms"), (25, "S2", "hit_window_client"),
        (25, "S4", "attainment_window"), (25, "S4", "cont_ttft_p50_s"), (25, "S4", "itl_req_p50_ms"),
        (25, "S6", "attainment_window"), (25, "S6", "cont_ttft_p50_s"), (25, "S6", "itl_req_p50_ms"),
    ],
    "pd-disaggregation": [
        (25, "S3", "attainment_window"), (25, "S3", "cont_ttft_p50_s"), (25, "S3", "itl_req_p50_ms"),
        (25, "S6", "attainment_window"), (25, "S6", "cont_ttft_p50_s"), (25, "S6", "itl_req_p50_ms"),
        (25, "S7", "attainment_window"), (25, "S7", "cont_ttft_p50_s"), (25, "S7", "itl_req_p50_ms"),
        (25, "S0", "itl_req_p50_ms"),
    ],
}


def _renderings(col: str, v: float) -> list[str]:
    if col in ("attainment_window", "hit_window_client", "moved_share"):
        return _pct(v)
    if col.endswith("_s") and "goodput" not in col:
        return _sec(v)
    if col.endswith("_ms"):
        return _ms(v)
    return [f"{v:.1f}", f"{v:.2f}"]


def _doc_params() -> list[tuple[str, Need]]:
    return [(d, n) for d, needs in NEEDS.items() for n in needs]


@pytest.mark.parametrize("doc", list(DOCS))
def test_guide_has_required_headings(doc: str) -> None:
    t = _text(DOCS[doc])
    heads = [m.group(2).strip() for m in re.finditer(r"^(#+) (.*)$", t, re.M)]
    for h in (EVIDENCE_HEADING, "When it helps / when it does not", "Versions"):
        assert any(h.lower() in x.lower() for x in heads), f"{doc}: no heading '{h}'"


@pytest.mark.parametrize("doc", list(DOCS))
def test_versions_section_names_the_runs_of_record(doc: str) -> None:
    sec = _section(_text(DOCS[doc]), r"Versions")
    assert sec.strip(), f"{doc}: Versions section missing"
    for tok in ("0.5.22", "0.5.21", "1.6.0", "1.5.1", "0.5.18", "Qwen3.8-27B-FP8", "TP1", "p6-b200"):
        assert tok in sec, f"{doc}: Versions lacks {tok!r}"
    assert re.search(r"runs of record", sec, re.I), f"{doc}: Versions must name the runs of record"
    flat = " ".join(sec.replace("*", "").split())
    assert "runtimeVersionOverride" in sec, f"{doc}: the operator override is missing"
    assert re.search(r"measured with a derived SGLang 0\.5\.22", flat), f"{doc}: say that the results came from the derived 0.5.22 build"


@pytest.mark.parametrize("doc", list(DOCS))
def test_evidence_section_labels_n1_and_failures(doc: str) -> None:
    sec = _section(_text(DOCS[doc]), EVIDENCE_RE).lower()
    assert sec.strip(), f"{doc}: Evidence section missing"
    for w in ("n=1", "r50", "inconclusive", "w2", "pre-registered", "docs/results/w3-16k-cells.csv"):
        assert w in sec, f"{doc}: Evidence section lacks {w!r}"
    assert re.search(r"ttft\s*(?:<=|≤)\s*1 s", sec) and re.search(r"itl\s*(?:<=|≤)\s*25 ms", sec) and re.search(r"(?:<=|≤)\s*2 s", sec), (
        f"{doc}: absolute SLO (TTFT<=1 s, ITL<=25 ms, cold<=2 s) missing")


@pytest.mark.parametrize("doc,need", _doc_params(), ids=lambda x: str(x) if not isinstance(x, str) else x)
def test_guide_quotes_the_csv_numbers(doc: str, need: Need) -> None:
    rate, cell, col = need
    row = _rows()[(rate, cell)]
    v = _fv(row[col])
    assert v is not None, f"CSV has no {col} for r{rate} {cell}"
    t = _text(DOCS[doc])
    body = _section(t, EVIDENCE_RE) + "\n" + _section(t, r"When it helps / when it does not")
    options = _renderings(col, v)
    assert any(o in body for o in options), f"{doc}: expected one of {options} ({col}, r{rate} {cell}) in Evidence / When it helps"


def test_pd_guide_reports_the_failed_cells_and_split_limits() -> None:
    t = _text(DOCS["pd-disaggregation"])
    body = _section(t, EVIDENCE_RE) + _section(t, r"When it helps / when it does not")
    low = body.lower()
    assert re.search(r"s5[^.]*reset", low), "S5 r25 reset-check failure (no load) missing"
    assert re.search(r"r32[^.]*s3[^.]*s5|s3[^.]*s5[^.]*r32", low) and "crash" in low and "undetermined" in low
    assert "entry prefill" in low and "continuation decode" in low and "static" in low
    assert re.search(r"dynamic[^.]*untested|untested[^.]*dynamic", low), "dynamic P:D must be stated as untested"
    assert re.search(r"p\s?:\s?d", low)


@pytest.mark.parametrize("doc", list(DOCS))
def test_mechanism_claims_marked_inferred(doc: str) -> None:
    t = _text(DOCS[doc])
    assert len(re.findall(r"INFERRED", t)) >= 2, f"{doc}: mechanism claims need INFERRED markers (at least 2)"
    assert "INFERRED" in _section(t, r"When it helps / when it does not"), f"{doc}: INFERRED marker missing in 'When it helps'"


@pytest.mark.parametrize("doc", list(DOCS))
def test_adoption_order_r_o_d(doc: str) -> None:
    t = _text(DOCS[doc])
    assert re.search(rf"R{ARROW}O{ARROW}D", t), f"{doc}: adoption order 'R -> O -> D' missing"


@pytest.mark.parametrize("doc", list(DOCS))
def test_guides_have_no_banned_terms(doc: str) -> None:
    t = _text(DOCS[doc])
    for pat, what in BAN:
        m = re.search(pat, t)
        assert not m, f"{doc}: contains a {what}: {m.group(0) if m else ''}"


# ---------------------------------------------------------------- Task.md / spec.md

def _base(name: str) -> list[str]:
    r = subprocess.run(["git", "show", f"{BASE}:{name}"], cwd=ROOT, capture_output=True, text=True)
    if r.returncode != 0:
        pytest.skip(f"baseline {BASE} not available")
    return r.stdout.splitlines()


@pytest.mark.parametrize("name", ["Task.md", "spec.md"])
def test_original_lines_are_kept_in_order(name: str) -> None:
    new = (SDD / name).read_text(encoding="utf-8").splitlines()
    it = iter(new)
    for i, line in enumerate(_base(name)):
        assert any(line == n for n in it), f"{name}: baseline line {i + 1} was edited or moved: {line[:90]!r}"
