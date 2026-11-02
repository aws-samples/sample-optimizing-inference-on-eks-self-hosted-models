# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""EFA or NVLink for the prefill/decode (D) cells: the documents explain what each one is and when to use which.  RED until written.

The owner's request: every example folder that has the D switch carries BOTH transports on one page (EFA, the default, and the
NVLink option in ``nvlink/``), there is no ninth example, and the documents explain when to use EFA and when to use NVLink to a
reader who knows neither.  This file pins the text side (the manifests are pinned by ``tests/manifests/test_420`` to ``test_422``):

* README, section "Choosing the PD transport", and the PD guide (sections 3.1 and 3.1.1, plus 7.1): one plain definition of EFA
  (Elastic Fabric Adapter, the AWS network interface with OS-bypass through libfabric/SRD, node to node, needs the EFA device plugin
  and one EFA device per pod) and of NVLink (NVIDIA's GPU-to-GPU interconnect inside a node: not a network); a "when to use which"
  table (EFA: prefill and decode may be on different nodes or you scale beyond one node, the path of the published runs, pods
  that cannot be privileged/hostPID; NVLink: prefill and decode on the SAME node, privileged + hostPID pods in a Pod Security
  privileged namespace, the KV transfer rate being the bottleneck, which is an inference, labelled as one);
* the honest evidence: the published p6-b200 runs used EFA only; NVLink on p6-b200 is a design reference that was not run there; the
  single p5e.48xlarge series is at parity and INCONCLUSIVE; the cuda_ipc path against the silent TCP fallback is a separate result
  (its two bandwidth figures are unpublished internal measurements and live in the PD guide only); for the 27B the KV transfer
  overlaps the prefill; both transports need EFA between nodes; no bandwidth figure of hardware without a source link;
* no sentence says NVLink is faster / beats / wins / outperforms (a negated sentence is not a claim), no price;
* DEPLOY.md, TROUBLESHOOTING.md and examples/README.md link the explanation, and name the ``nvlink/`` sub-folder, not a ninth example;
* no file of the tree names the removed ninth example folder, and no document counts nine examples.

Nothing here weakens an expectation: a missing section is a failure, never a skip.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest
import sanitize_scan
import test_docs_set as ds
import test_pd_nvlink_reference as P

ROOT = P.ROOT
README, GUIDE, TROUBLE, DEPLOY, EXAMPLES_README = P.README, P.GUIDE, P.TROUBLE, P.DEPLOY, P.EXAMPLES_README
PD_EXAMPLES = P.PD_EXAMPLES
MARKER = P.MARKER
NINTH = "08-pd-" + "nvlink"  # built in two pieces: this file must not carry the name either
NINTH_DIR = "examples/0" + "8"
BASE_COMMIT = "f340fa1"  # the pushed state before the change: bandwidth figures that it already carried are not new

FORBIDDEN_NEAR_NVLINK = re.compile(r"\b(faster|quicker|beats?|wins?|winner|outperform\w*)\b", re.I)
BANDWIDTH = re.compile(r"\b\d+(?:[.,]\d+)?\s?(?:GB/s|GBps|Gbps|Gb/s|TB/s|Tbps)\b")


def _read(p: Path) -> str:
    assert p.is_file(), f"{p.relative_to(ROOT)} missing"
    return p.read_text(encoding="utf-8")


def _flat(text: str) -> str:
    return " ".join(text.split())


def _block(text: str, heading: re.Pattern[str], stop_level: int) -> str:
    """Text from the heading that matches ``heading`` up to the next heading of level ``<= stop_level``."""
    m = re.search(heading, text)
    assert m, f"heading {heading.pattern!r} not found"
    nxt = re.search(rf"(?m)^#{{1,{stop_level}}} ", text[m.end():])
    return text[m.start():m.end() + nxt.start()] if nxt else text[m.start():]


def readme_section() -> str:
    """The README section 'Choosing the PD transport' (an h3 of the Architecture section)."""
    return _block(_read(README), re.compile(r"(?m)^### Choosing the PD transport.*$"), 3)


def guide_transport_text() -> str:
    """The PD guide, sections 3.1 (transport) and 3.1.1 (NVLink): from '### 3.1 ' to '### 3.2 '."""
    return _between(_read(GUIDE), r"(?m)^### 3\.1 ", r"(?m)^### 3\.2 ")


def _between(text: str, start: str, end: str) -> str:
    a = re.search(start, text)
    assert a, f"{start!r} not found"
    b = re.search(end, text[a.end():])
    assert b, f"{end!r} not found after {start!r}"
    return text[a.start():a.end() + b.start()]


def guide_71() -> str:
    return _between(_read(GUIDE), r"(?m)^### 7\.1 ", r"(?m)^### 7\.2 ")


def _tables(text: str) -> list[str]:
    """Markdown tables of ``text`` (runs of at least 3 consecutive lines starting with ``|``), each as one string."""
    out, run = [], []
    for ln in text.splitlines() + [""]:
        if ln.lstrip().startswith("|"):
            run.append(ln)
            continue
        if len(run) >= 3:
            out.append("\n".join(run))
        run = []
    return out


BLOCKS = {"readme": readme_section, "guide": guide_transport_text}

EFA_DEFINITION = [
    (r"Elastic Fabric Adapter", "spells out EFA"),
    (r"(?i)OS[- ]bypass", "OS-bypass"),
    (r"(?i)libfabric|SRD", "libfabric / SRD"),
    (r"(?i)(between|across|node[- ]to[- ]node)[^.]*nodes?|node[- ]to[- ]node", "node to node"),
    (r"(?i)device plugin", "needs the EFA device plugin"),
    (r"(?i)one EFA (device|interface)[^.]*per pod|per pod[^.]*EFA (device|interface)", "one EFA device per pod"),
]
NVLINK_DEFINITION = [
    (r"NVLink[^.]*\bNVIDIA", "NVIDIA's interconnect"),
    (r"(?i)GPU[- ]to[- ]GPU", "GPU-to-GPU"),
    (r"(?i)(inside|within) (a|one|the same) node|inside the node", "inside a node"),
    (r"(?i)\bnot a network\b|not a network\b", "not a network"),
]
# rows of the decision table: every needle must appear somewhere in the table
TABLE_NEEDLES = [
    (r"(?i)different nodes|across nodes|more than one node|beyond one node|several nodes|multiple nodes", "EFA: prefill/decode on different nodes, scale beyond one node"),
    (r"(?i)same node", "NVLink: prefill and decode on the same node"),
    (r"hostPID", "NVLink: hostPID"),
    (r"privileged", "NVLink: privileged"),
    (r"Pod Security", "NVLink: Pod Security privileged namespace"),
    (r"(?i)default", "EFA: the default"),
    (r"(?i)published", "EFA: the path of the published runs"),
    (r"(?i)bottleneck", "NVLink: when the KV transfer rate is the bottleneck"),
    (r"(?i)\binfer", "the bottleneck situations are labelled as inference"),
]


@pytest.mark.parametrize("which", BLOCKS)
def test_the_section_defines_efa_for_a_reader_who_does_not_know_it(which: str) -> None:
    text = _flat(BLOCKS[which]())
    for pattern, what in EFA_DEFINITION:
        assert re.search(pattern, text), f"{which}: the EFA definition lacks: {what}"


@pytest.mark.parametrize("which", BLOCKS)
def test_the_section_defines_nvlink_for_a_reader_who_does_not_know_it(which: str) -> None:
    text = _flat(BLOCKS[which]())
    for pattern, what in NVLINK_DEFINITION:
        assert re.search(pattern, text), f"{which}: the NVLink definition lacks: {what}"


@pytest.mark.parametrize("which", BLOCKS)
def test_the_section_has_a_when_to_use_which_table(which: str) -> None:
    block = BLOCKS[which]()
    assert re.search(r"(?i)when to use which|EFA or NVLink\?", block), f"{which}: a 'When to use which' / 'EFA or NVLink?' heading or lead"
    tables = [t for t in _tables(block) if "EFA" in t and "NVLink" in t]
    assert tables, f"{which}: a markdown table that names EFA and NVLink"
    table = _flat(max(tables, key=len))
    missing = [what for pattern, what in TABLE_NEEDLES if not re.search(pattern, table)]
    assert not missing, f"{which}: the decision table lacks: {missing}"
    assert len([ln for t in tables for ln in t.splitlines()]) >= 5, "header, separator and at least three rows"


@pytest.mark.parametrize("which", BLOCKS)
def test_the_decision_table_labels_the_nvlink_situations_as_inference_not_measurement(which: str) -> None:
    flat = _flat(BLOCKS[which]())
    assert re.search(r"(?i)\binfer(ence|red|s)?\b", flat), f"{which}: the situations where NVLink may help are labelled as inference"
    assert re.search(r"(?i)small|fast model", flat) and re.search(r"(?i)skew|P:D|ratio", flat) and re.search(r"(?i)contention|NIC", flat), (
        f"{which}: the three inferred situations (small/fast models, skewed P:D ratio, NIC contention) are named")


@pytest.mark.parametrize("which", BLOCKS)
def test_the_section_states_the_evidence_honestly(which: str) -> None:
    flat = _flat(BLOCKS[which]())
    sentences = P._sentences(BLOCKS[which]())  # split the original text, not the flattened one: a table row is not part of a sentence
    assert any(re.search(r"(?i)published|runs? of record|recorded", s) and re.search(r"EFA|LIBFABRIC", s) and not P.NEGATION.search(s) for s in sentences), (
        f"{which}: the published runs used EFA")
    assert any(P.NEGATION.search(s) and "NVLink" in s and re.search(r"(?i)published|runs? of record|recorded|p6-b200", s) for s in sentences), (
        f"{which}: NVLink was not part of the published runs")
    assert P.NOT_RUN_P6.search(flat), f"{which}: NVLink on p6-b200 was not run"
    assert P.LABEL.search(flat), f"{which}: design reference label"
    assert re.search(r"INCONCLUSIVE|inconclusive", flat) and re.search(r"(?i)parity", flat), f"{which}: the EFA vs NVLink comparison was inconclusive, at parity"
    assert re.search(r"p5e\.48xlarge|H200", flat), f"{which}: the comparison ran on another instance type"
    assert any("EFA" in s and re.search(r"(?i)(between|across|other) nodes", s) and re.search(r"(?i)both|still|stays|remains|need", s) for s in sentences), (
        f"{which}: both transports need EFA between nodes")


def test_the_guide_says_the_kv_transfer_overlaps_the_prefill_so_the_link_rarely_limits_the_27b() -> None:
    flat = _flat(guide_transport_text())
    sentences = P._sentences(guide_transport_text())
    assert any(re.search(r"(?i)overlap", s) and re.search(r"(?i)prefill", s) and re.search(r"27B|link|transfer", s) for s in sentences), (
        "the guide says the KV transfer overlaps the prefill")
    assert any(re.search(r"27B", s) and re.search(r"(?i)overlap|rarely|seldom|not the bottleneck", s) for s in sentences), (
        "for the 27B the link rarely limits end to end")


def test_the_guide_quotes_the_cuda_ipc_and_tcp_bandwidths_only_as_marked_unpublished_measurements() -> None:
    sentences = P._sentences(_read(GUIDE))
    for figure in (r"\b387\b", r"\b0\.26 GB/s"):
        carrying = [s for s in sentences if re.search(figure, s)]
        assert carrying, f"the PD guide quotes {figure} (cuda_ipc across pods against the silent TCP fallback)"
        assert all(MARKER.search(s) for s in carrying), f"{figure} must be marked (unpublished internal measurement): {carrying}"
    joined = _flat(_read(GUIDE))
    assert re.search(r"387 GB/s", joined) and re.search(r"p5e\.48xlarge", joined)
    assert re.search(r"(?i)single series|one series", joined)


def test_the_bandwidth_figures_live_in_the_guide_only() -> None:
    for p in (README, DEPLOY, TROUBLE, EXAMPLES_README, *P.PD_READMES):
        text = _read(p)
        assert not re.search(r"\b387\b[^.]{0,20}GB/s|\b0\.26 GB/s", text), f"{p.relative_to(ROOT)}: the cuda_ipc / TCP bandwidths live in the PD guide"


def _base_text(rel: str) -> str:
    proc = subprocess.run(["git", "-C", str(ROOT), "show", f"{BASE_COMMIT}:{rel}"], capture_output=True, text=True, check=False)
    assert proc.returncode == 0, f"cannot read {rel} at {BASE_COMMIT}: {proc.stderr}"
    return proc.stdout


@pytest.mark.parametrize("which,rel", [("readme", "README.md"), ("guide", "docs/optimizations/pd-disaggregation/README.md")])
def test_a_new_bandwidth_figure_carries_a_source_link_or_the_unpublished_marker(which: str, rel: str) -> None:
    """No invented spec: a GB/s figure that the text did not carry before is an unpublished measurement or sits in a paragraph with an AWS / NVIDIA link."""
    known = {m.group(0) for m in BANDWIDTH.finditer(_base_text(rel))}
    for para in re.split(r"\n\s*\n", BLOCKS[which]()):
        for sentence in P._sentences(para):
            for m in BANDWIDTH.finditer(sentence):
                if m.group(0) in known or MARKER.search(sentence):
                    continue
                assert re.search(r"https://(docs\.aws\.amazon\.com|aws\.amazon\.com|www\.nvidia\.com|docs\.nvidia\.com|developer\.nvidia\.com)", para), (
                    f"{which}: unsourced bandwidth figure {m.group(0)!r}: {sentence!r}")


@pytest.mark.parametrize("which", BLOCKS)
def test_the_section_has_no_price_and_no_customer_data(which: str) -> None:
    text = BLOCKS[which]()
    assert not re.search(r"US\$|\bUSD\b|\bprice\b|\bpricing\b|\$\d", text, re.I)
    assert not re.search(r"(?i)\bcustomer\b", text)


# ----------------------------------------------------------------------------- the README section in detail
def test_readme_section_lists_the_nvlink_option_of_each_pd_example() -> None:
    flat = _flat(readme_section())
    generic = "examples/<folder>/nvlink" in flat
    named = all(f in flat or re.search(rf"\b{f[:2]}\b", flat) for f in PD_EXAMPLES)
    assert "examples/03-pd-disaggregation/nvlink" in flat or generic, "the NVLink option of an example is examples/<folder>/nvlink"
    assert generic or named, "the pattern examples/<folder>/nvlink is given, or the four PD examples (03, 04, 06, 07) are named"
    assert "rendered-nvlink/S5.yaml" in flat and "rendered/S5.yaml" in flat
    assert re.search(r"(?i)\bdelete\b", flat) and "q38" in flat and "`s-dis`" in flat


def test_readme_section_does_not_use_the_removed_example() -> None:
    assert NINTH not in _read(README)


# ----------------------------------------------------------------------------- the PD guide in detail
def test_guide_has_the_definitions_and_the_table_in_3_1_or_3_1_1_and_7_1_points_to_them() -> None:
    sec = guide_71()
    flat = _flat(sec)
    assert "EFA" in flat and "NVLink" in flat
    assert re.search(r"\]\(#311-[^)]*\)|\]\(#31-[^)]*\)", sec), "7.1 links the 'when to use which' of 3.1 / 3.1.1"
    assert "nvlink/" in sec, "7.1 names the nvlink/ sub-folder option"
    assert NINTH not in sec


def test_guide_never_says_nvlink_is_faster_beats_wins_or_outperforms() -> None:
    for p in (README, GUIDE, TROUBLE, DEPLOY, EXAMPLES_README, *P.PD_READMES):
        for sentence in P._sentences(_read(p)):
            if "NVLink" in sentence and FORBIDDEN_NEAR_NVLINK.search(sentence):
                assert P.NEGATION.search(sentence), f"{p.relative_to(ROOT)}: {sentence!r}"
    assert not P.forbidden_claims(_read(GUIDE))


# ----------------------------------------------------------------------------- cross-links from the other documents
def _links(text: str) -> list[str]:
    return re.findall(r"\]\((?!https?://|mailto:)([^)\s]*)\)", text)


@pytest.mark.parametrize("p", [EXAMPLES_README, DEPLOY, TROUBLE], ids=lambda p: str(p.relative_to(ROOT)))
def test_the_document_links_the_explanation_of_efa_or_nvlink(p: Path) -> None:
    text = _read(p)
    targets = _links(text)
    assert re.search(r"(?i)when to use|EFA or NVLink|choos(e|ing) (the )?(PD )?transport", text), "the document says it explains the choice"
    ok = [t for t in targets if re.search(r"README\.md#choosing-the-pd-transport$", t) or re.search(r"pd-disaggregation/README\.md#31", t)]
    assert ok, f"{p.relative_to(ROOT)} links the README section 'Choosing the PD transport' or section 3.1 / 3.1.1 of the PD guide"
    for t in ok:
        target, _, frag = t.partition("#")
        dest = (p.parent / target).resolve()
        assert dest.is_file(), t
        heads = [ds._slug(h) for h in re.findall(r"(?m)^#{1,6} (.+)$", dest.read_text(encoding="utf-8"))]
        assert frag in heads, f"broken anchor {t}"


@pytest.mark.parametrize("p", [EXAMPLES_README, DEPLOY, TROUBLE], ids=lambda p: str(p.relative_to(ROOT)))
def test_the_document_names_the_nvlink_subfolder_and_not_a_ninth_example(p: Path) -> None:
    text = _read(p)
    assert NINTH not in text and not re.search(rf"{NINTH_DIR}\b", text)
    assert re.search(r"examples/(<folder>|\d\d-[a-z-]+)/nvlink", text), f"{p.relative_to(ROOT)} names the nvlink/ sub-folder of the PD examples"


def test_deploy_section_6_says_the_four_pd_examples_carry_both_options() -> None:
    sec = _read(DEPLOY)
    m = re.search(r"(?ms)^## 6\. Deploy one cell.*?(?=^## )", sec)
    assert m
    flat = _flat(m.group(0))
    assert re.search(r"\bEFA\b[^.]*\bdefault\b|\bdefault\b[^.]*\bEFA\b", flat)
    assert "NVLink" in flat and "nvlink" in flat
    assert re.search(r"(?i)\bdelete\b", flat)
    assert not re.search(r"(?i)it is not one of the eight cells", flat), "the NVLink option is no longer a separate example outside the eight"


def test_troubleshooting_tells_how_to_apply_the_nvlink_option_of_an_example() -> None:
    flat = _flat(_read(TROUBLE))
    assert re.search(r"examples/(<folder>|03-pd-disaggregation)/nvlink", flat), "the TCP fallback entry names the nvlink/ sub-folder of the examples"
    assert re.search(r"examples/03-pd-disaggregation(?![\w/-])|rendered/", flat), "and the EFA one"


# ----------------------------------------------------------------------------- the ninth example is gone from the whole tree
def _tree_files() -> list[Path]:
    out = subprocess.run(["git", "-C", str(ROOT), "ls-files", "-z", "--cached", "--others", "--exclude-standard"], capture_output=True, check=True).stdout.decode()
    return [ROOT / p for p in out.split("\0") if p and (ROOT / p).is_file()]


def test_no_file_or_path_of_the_tree_names_the_removed_ninth_example() -> None:
    hits = []
    for p in _tree_files():
        rel = p.relative_to(ROOT).as_posix()
        if NINTH in rel or rel.startswith(NINTH_DIR):
            hits.append(rel)
            continue
        try:
            text = p.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        if NINTH in text or re.search(rf"{NINTH_DIR}\b", text):
            hits.append(rel)
    assert not hits, f"the ninth example is still named in: {hits}"


def test_no_document_counts_nine_examples_or_numbers_an_example_08() -> None:
    pattern = re.compile(r"(?i)\b(nine|9) (example|folder|cell)s?\b|\b00 (to|-|\.\.) ?08\b|\bexamples? 08\b|\bfolders? `?08`?")
    hits = [p.relative_to(ROOT).as_posix() for p in (README, GUIDE, TROUBLE, DEPLOY, EXAMPLES_README, *P.PD_READMES, ROOT / "tests/README.md")
            if p.is_file() and pattern.search(p.read_text(encoding="utf-8"))]
    assert not hits, hits


def test_readme_repository_layout_row_for_examples_says_eight_folders_with_an_nvlink_option() -> None:
    rows = [ln for ln in _read(README).splitlines() if ln.lstrip().startswith("|") and "(examples/)" in ln]
    assert len(rows) == 1, rows
    row = rows[0]
    assert re.search(r"(?i)\beight\b", row) and "nvlink" in row.lower(), f"the layout row says eight folders and the nvlink/ option: {row!r}"
    assert not re.search(r"\b0?8\b", row.replace("(S0 to S7)", "")), row
