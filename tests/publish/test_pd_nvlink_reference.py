# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The NVLink alternative in the public text, and the reference architecture: what the documents and the figure may say.  RED until written.

The sample keeps EFA (NIXL LIBFABRIC) as the default transport of the prefill/decode (D) cells: the published runs on p6-b200
used it.  It adds an NVLink alternative for the same-node KV path (``components/pd-nvlink``, ``variants-nvlink/``,
``rendered-nvlink/``, ``examples/08-pd-nvlink``) and a reference architecture picture.  Both are a DESIGN REFERENCE, not a
measurement.  This file pins the text side of that work (the manifests are pinned by ``tests/manifests/test_421`` and ``test_422``):

* PD guide: the EFA example and the NVLink example side by side, how to select one (``rendered/S5.yaml`` or
  ``rendered-nvlink/S5.yaml``), the design reference label, "the published measurements used EFA", the p5e.48xlarge parity
  figures exactly (each one marked as an unpublished internal measurement) and the INCONCLUSIVE state, the 1P1D TCP-fallback
  figures, the privileged + hostPID sentence, "same node only", "EFA between nodes", and none of the claims the honesty rules forbid.
* TROUBLESHOOTING: an entry that shows how to detect the silent TCP fallback of default pods.
* README: the architecture section puts an ALB (not an NLB) in front of Grafana, says the Terraform edge still implements
  CloudFront + NLB (off by default, the ALB is the design still to be implemented), and carries the design reference label.
* ``docs/static/architecture.drawio`` / ``.png``: no CloudFront, WAF, VPC origin or NLB; an ALB; NVLink inside the GPU node; EFA
  between nodes; the design reference label in the legend; a PNG that is a render of THIS drawing, not the old one.
* ``terraform/cluster/grafana-edge.tf`` is byte-identical (the scope of the Grafana change is the diagram and the text only).
* every new or changed text: no account id, no internal vocabulary, no figure of the unfinished p5e series, every path it names
  exists, every relative link and anchor resolves.

Nothing here weakens an expectation: a missing document or section is a failure, never a skip.
"""

from __future__ import annotations

import glob
import hashlib
import html
import re
import struct
from pathlib import Path

import lib_xml
import pytest
import sanitize_scan
import test_docs_set as ds
import test_quoted_figures_show_their_work as qf
import test_sub_cycle_e_guards as e_guards
import test_sub_cycle_f_guards as f_guards

ROOT = sanitize_scan.REPO_ROOT
README = ROOT / "README.md"
GUIDE = ROOT / "docs/optimizations/pd-disaggregation/README.md"
TROUBLE = ROOT / "docs/TROUBLESHOOTING.md"
DEPLOY = ROOT / "docs/DEPLOY.md"
EXAMPLES_README = ROOT / "examples/README.md"
DRAWIO = ROOT / "docs/static/architecture.drawio"
PNG = ROOT / "docs/static/architecture.png"
GRAFANA_EDGE = ROOT / "terraform/cluster/grafana-edge.tf"
GRAFANA_EDGE_SHA256 = "5f5418d4d9205fd55c4efc5f3362e2dfed2e527653ef13d1e4f3a2dc0435c216"
# The figure as it was published (CloudFront + NLB, EFA only).  The new drawing and its PNG must differ from it.
OLD_DRAWIO_SHA256 = "e0dd479e90a2a8ac681d63bea99b10c7691511d08d2416ba5310f1ec4bf24650"
OLD_PNG_SHA256 = "17ae02d904e2f4df35b589d75fe1975f7528d927ed9ef838d69b7128dd2f904f"
TQ = ROOT / "manifests/track-q-qwen38"
NEW_TEXT = [
    README, GUIDE, TROUBLE, DEPLOY, EXAMPLES_README,
    ROOT / "examples/08-pd-nvlink/README.md", ROOT / "examples/08-pd-nvlink/kustomization.yaml",
    TQ / "variants-nvlink/README.md", TQ / "rendered-nvlink/README.md",
    TQ / "components/pd-nvlink/kustomization.yaml", TQ / "components/pd-nvlink/patch.yaml",
    ROOT / "scripts/render-track-q-nvlink.sh",
]
# Documents the honesty rules apply to (everything a reader can take a claim from).
CLAIM_TEXT = [README, GUIDE, TROUBLE, DEPLOY, EXAMPLES_README, ROOT / "examples/08-pd-nvlink/README.md",
              TQ / "variants-nvlink/README.md", TQ / "rendered-nvlink/README.md"]

LABEL = re.compile(r"(?i)design reference|reference design")
NEGATION = re.compile(r"(?i)\b(not|no|never|nor|without|neither|cannot|can't|n't|none)\b|n't\b")
MARKER = re.compile(r"\(unpublished internal measurements?\)")
NOT_RUN_P6 = re.compile(
    r"(?i)not[^.]*\b(run|measured|recorded|tested)\b[^.]*p6-b200|p6-b200[^.]*\bnot\b[^.]*\b(run|measured|recorded|tested)\b")

# The p5e.48xlarge T1 figures (SGLang 0.5.21 public image, Qwen3.8-27B-FP8, 4P4D, rate r6, n=3 per arm, single series) and the
# 1P1D TCP-fallback figures: the only measured numbers the text may carry.  Each is followed, in its sentence, by the marker.
P5E_FIGURES = ["0.1386", "0.1395", "1.0066", "153.8", "150.8"]
TCP_FIGURES = [r"\b9\.9 s\b", r"\b2\.0 s\b"]

# Claims the honesty rules forbid.  A sentence that carries a negation ("we do not claim NVLink is faster") is not a claim.
FORBIDDEN_CLAIMS = [
    (r"nvlink\b[^.;]*\b(is|are|was|were|gives?|delivers?|makes?|gets?|provides?)\b[^.;]*\b(faster|quicker|better|lower[- ]latency|higher[- ]throughput)\b",
     "NVLink is faster / better"),
    (r"nvlink\s+(beats|outperforms?|wins|improves|speeds?)", "NVLink beats EFA"),
    (r"\b(faster|quicker|better|lower[- ]latency)\s+than\s+(the\s+)?efa\b", "faster than EFA"),
    (r"efa\b[^.;]*\b(is|are)\b[^.;]*\b(not needed|unnecessary|obsolete|redundant|no longer needed)\b", "EFA is not needed"),
    (r"\b(removes?|drops?|replaces?|replacing)\s+(the\s+)?(need for\s+)?efa\b", "NVLink replaces EFA"),
    (r"\b(published|recorded|runs? of record)\b[^.;]*\b(measured|ran|run|used|use)\b[^.;]*\bnvlink\b", "the published runs used NVLink"),
    (r"nvlink\b[^.;]*\b(measured|recorded|ran|tested)\b[^.;]*\bp6-b200\b", "NVLink measured on p6-b200"),
]
SERIES_NOT_FINAL = re.compile(r"(?i)\bJ[23]\b|\bladder\b|\bj2\b")


def _read(p: Path) -> str:
    assert p.is_file(), f"{p.relative_to(ROOT)} missing"
    return p.read_text(encoding="utf-8")


def _flat(text: str) -> str:
    return " ".join(text.split())


def _sentences(text: str) -> list[str]:
    return qf._sentences(text)


def forbidden_claims(text: str) -> list[tuple[str, str]]:
    """Return ``(label, sentence)`` for every sentence of ``text`` that makes a claim the honesty rules forbid.

    A negation in front of the claim ("we do not claim that NVLink is faster") makes the sentence a non-claim.  The pattern
    "EFA is not needed" carries its own negation, so only a negation before it counts.
    """
    hits = []
    for sentence in _sentences(text):
        for pattern, label in FORBIDDEN_CLAIMS:
            m = re.search(pattern, sentence, re.I)
            if not m:
                continue
            own_negation = label == "EFA is not needed"
            if NEGATION.search(sentence[:m.start()] if own_negation else sentence):
                continue
            hits.append((label, sentence))
    return hits


def _h2_sections(text: str) -> list[str]:
    parts = re.split(r"(?m)^## ", text)
    return ["## " + p for p in parts[1:]]


# ----------------------------------------------------------------------------- the detector itself is not vacuous
@pytest.mark.parametrize("claim", [
    "NVLink is faster than EFA for the KV transfer.",
    "With NVLink the TTFT is lower: NVLink beats EFA.",
    "EFA is not needed when you use NVLink.",
    "The published runs used NVLink between the prefill and decode pods.",
    "NVLink was measured on p6-b200.",
    "NVLink gives better latency than EFA.",
])
def test_the_claim_detector_flags_what_the_honesty_rules_forbid(claim: str) -> None:
    assert forbidden_claims(claim), claim


@pytest.mark.parametrize("fine", [
    "We do not claim that NVLink is faster than EFA.",
    "None of the published runs used NVLink; they used EFA.",
    "The recipe was not run on p6-b200.",
    "EFA stays the transport between nodes.",
    "Avoiding the silent TCP fallback is the large measured effect.",
])
def test_the_claim_detector_accepts_the_honest_statements(fine: str) -> None:
    assert not forbidden_claims(fine), fine


# ----------------------------------------------------------------------------- the PD guide
def test_guide_shows_the_efa_example_and_the_nvlink_example_side_by_side() -> None:
    sections = [s for s in _h2_sections(_read(GUIDE)) if "examples/03-pd-disaggregation" in s and "examples/08-pd-nvlink" in s]
    assert sections, "one section of the PD guide shows both `examples/03-pd-disaggregation` (EFA) and `examples/08-pd-nvlink` (NVLink)"
    commands = [ln for s in sections for ln in s.splitlines() if "kubectl" in ln and "apply" in ln]
    assert any("examples/03-pd-disaggregation" in c for c in commands) and any("examples/08-pd-nvlink" in c for c in commands), (
        "both examples appear as `kubectl apply -k` commands")


def test_guide_explains_how_to_select_the_transport() -> None:
    text = _read(GUIDE)
    flat = _flat(text)
    for needle in ("rendered/S5.yaml", "rendered-nvlink/S5.yaml", "components/pd-nvlink", "variants-nvlink"):
        assert needle in flat, f"the PD guide names {needle}"
    assert re.search(r"(?i)\bEFA\b[^.]*\b(default|stays|remains)\b|\b(default|stays|remains)\b[^.]*\bEFA\b", flat), "EFA remains the default"
    assert re.search(r"(?i)\bdelete\b[^.]*\b(other|first|before)\b|\b(other|first|before)\b[^.]*\bdelete\b", flat), (
        "both trees deploy the same namespace and DGD name: delete one before applying the other")


def test_guide_labels_the_nvlink_recipe_a_design_reference_not_run_on_p6_b200() -> None:
    flat = _flat(_read(GUIDE))
    assert LABEL.search(flat), "design reference label"
    assert NOT_RUN_P6.search(flat), "the recipe was not run on p6-b200"
    assert any(NEGATION.search(s) and re.search(r"(?i)published|runs of record|recorded", s) and "NVLink" in s for s in _sentences(flat)), (
        "a sentence says the published runs were not run with NVLink")
    assert any(re.search(r"(?i)published|runs of record", s) and re.search(r"EFA|LIBFABRIC", s) and not NEGATION.search(s)
               for s in _sentences(flat)), "a sentence says the published measurements used EFA (LIBFABRIC)"


def test_guide_records_the_p5e_parity_result_exactly_and_as_inconclusive() -> None:
    text = _read(GUIDE)
    flat = _flat(text)
    sentences = _sentences(text)
    for figure in P5E_FIGURES:
        carrying = [s for s in sentences if figure in s]
        assert carrying, f"the PD guide quotes {figure}"
        assert all(MARKER.search(s) for s in carrying), f"{figure} is not marked (unpublished internal measurement): {carrying}"
    for needle in ("p5e.48xlarge", "H200", "Qwen3.8-27B", "n=3"):
        assert needle in flat, f"the PD guide states the scope: {needle}"
    assert re.search(r"4P4D|4 prefill \+ 4 decode", flat), "4P4D"
    assert re.search(r"(?i)single series|one series", flat), "a single series"
    assert re.search(r"INCONCLUSIVE|inconclusive", flat), "the pre-registered rule left the result INCONCLUSIVE"
    assert re.search(r"(?i)parity|no detectable difference|not distinguishable|indistinguishable", flat), "parity of EFA and NVLink"
    assert "2.4" in flat and "2.8" in flat, "the 95% interval of the ratio (about -2.4% to +2.8%)"
    assert re.search(r"(?i)SGLang 0\.5\.21|public image", flat), "the engine and image of the measurement"


def test_guide_records_the_tcp_fallback_figures_of_the_one_prefill_one_decode_runs() -> None:
    text = _read(GUIDE)
    sentences = _sentences(text)
    for figure in TCP_FIGURES:
        carrying = [s for s in sentences if re.search(figure, s)]
        assert carrying, f"the PD guide quotes {figure}"
        assert any(MARKER.search(s) for s in carrying), f"{figure}: marked as an unpublished internal measurement"
    flat = _flat(text)
    assert re.search(r"(?i)silent[^.]*TCP|TCP[^.]*silent", flat), "the TCP fallback is silent"
    assert re.search(r"32K", flat) and "1P1D" in flat


def test_guide_says_the_large_measured_effect_is_avoiding_the_tcp_fallback_not_nvlink_over_efa() -> None:
    flat = _flat(_read(GUIDE))
    assert any(re.search(r"(?i)TCP", s) and re.search(r"(?i)fall ?back", s) and re.search(r"(?i)avoid|prevent|instead of|compared with", s)
               for s in _sentences(flat)), "the measured win is avoiding the silent TCP fallback"


def test_guide_states_the_security_and_placement_limits() -> None:
    text = _read(GUIDE)
    blocks = qf._blocks(text)
    assert any("hostPID" in b and "privileged" in b and "Pod Security" in b for b in blocks), (
        "one paragraph says the recipe needs privileged + hostPID, i.e. a Pod Security `privileged` namespace")
    flat = _flat(text)
    assert any(re.search(r"(?i)same[- ]node", b) and re.search(r"(?i)\bonly\b", b) for b in blocks), "works on the same node only"
    assert any("EFA" in s and re.search(r"(?i)(between|across|other) nodes", s) for s in _sentences(flat)), "EFA stays for traffic between nodes"


def test_guide_scopes_the_old_nvlink_gate_result_and_draws_the_nvlink_path() -> None:
    text = _read(GUIDE)
    paragraph = next((b for b in qf._blocks(text) if "Mooncake" in b), "")
    assert paragraph, "the transport-gate paragraph is still there"
    assert "pd-nvlink" in paragraph or re.search(r"(?i)NVLink (recipe|alternative)", paragraph), (
        "the 'Mooncake over NVLink failed' paragraph points to the NVLink recipe, which that gate did not include")
    diagrams = re.findall(r"(?s)```mermaid\n(.*?)```", text)
    assert any("NVLink" in d for d in diagrams), "a mermaid diagram of the guide shows the NVLink path"
    assert any("pd-nvlink" in d for d in diagrams), "the repo-pieces diagram shows components/pd-nvlink next to pd-efa"


# ----------------------------------------------------------------------------- TROUBLESHOOTING
def test_troubleshooting_has_an_entry_to_detect_the_silent_tcp_fallback() -> None:
    text = _read(TROUBLE)
    heads = [m for m in re.finditer(r"(?m)^#{2,4} (.+)$", text) if re.search(r"(?i)tcp", m.group(1)) and re.search(r"(?i)silent|fall", m.group(1))]
    assert heads, "a heading names the silent TCP fallback of the KV transfer"
    start = heads[0].end()
    nxt = re.search(r"(?m)^#{1,4} ", text[start:])
    body = text[start:start + nxt.start()] if nxt else text[start:]
    flat = _flat(body)
    assert "cuda_ipc" in flat and re.search(r"(?i)\btcp\b", flat) and "UCX_PROTO_INFO" in flat, "how to read the UCX protocol table"
    assert re.search(r"kubectl[^`]*logs", body), "the command that shows it"
    assert "hostPID" in flat and "privileged" in flat, "what changes it (the recipe) and what it needs"
    assert "pd-disaggregation" in flat, "links to the PD guide"


# ----------------------------------------------------------------------------- README
def _architecture() -> str:
    return ds._section(_read(README), r"architect")


def test_readme_architecture_puts_an_alb_not_an_nlb_in_front_of_grafana() -> None:
    sec = _architecture()
    sentences = _sentences(sec)
    alb = [s for s in sentences if re.search(r"\bALB\b|Application Load Balancer", s) and "Grafana" in s]
    assert alb, "a sentence of the Architecture section puts an ALB in front of Grafana"
    assert LABEL.search(sec), "the Architecture section carries the design reference label"
    for s in sentences:
        if re.search(r"(?i)cloudfront|\bNLB\b|network load balancer", s):
            assert "Terraform" in s, f"CloudFront / NLB are named only as what Terraform still builds: {s!r}"
    alt = re.search(r"!\[([^\]]*)\]\(docs/static/architecture\.png\)", sec)
    assert alt and not re.search(r"(?i)cloudfront|\bNLB\b", alt.group(1)), "the picture alt text does not describe the old edge"


def test_readme_says_the_terraform_edge_is_still_cloudfront_plus_nlb_and_the_alb_is_still_to_build() -> None:
    sentences = _sentences(_read(README))
    terraform = [s for s in sentences if "Terraform" in s and re.search(r"CloudFront", s) and re.search(r"\bNLB\b", s)]
    assert terraform, "a sentence says the Terraform edge is CloudFront + NLB"
    assert any(re.search(r"(?i)off by default|disabled by default|default(s)? (is |to )?(off|false)", s) for s in terraform), "off by default"
    assert any(re.search(r"(?i)still", s) for s in terraform), "the Terraform edge STILL implements it"
    assert any(re.search(r"\bALB\b|Application Load Balancer", s) and re.search(
        r"(?i)to be implemented|not implemented|not built|not (yet )?in Terraform|still to (be )?(build|implement)|not part of|does not (build|create)", s)
        for s in sentences), "a sentence says the ALB is the reference design, not implemented by the Terraform"
    assert "kubectl port-forward" in _flat(_read(README)), "port-forward stays the default way to Grafana"


def test_readme_says_what_nvlink_is_for_and_what_the_published_runs_used() -> None:
    sentences = _sentences(_read(README))
    assert any("NVLink" in s and re.search(r"(?i)same[- ]node", s) and re.search(r"(?i)prefill", s) and re.search(r"(?i)decode", s) for s in sentences), (
        "NVLink is the same-node prefill-to-decode KV path")
    assert any("EFA" in s and re.search(r"(?i)(between|across|other) nodes", s) for s in sentences), "EFA stays for traffic between nodes"
    assert any(NEGATION.search(s) and "NVLink" in s and re.search(r"(?i)published|runs of record|measured|recorded", s) for s in sentences), (
        "the published runs (EFA / LIBFABRIC) were not run with NVLink, and the recipe was not run on p6-b200")
    assert NOT_RUN_P6.search(_flat(_read(README))), "not run on p6-b200"
    assert not forbidden_claims(_read(README))


def test_readme_explains_how_to_select_the_transport() -> None:
    flat = _flat(_read(README))
    assert "rendered/S5.yaml" in flat and "rendered-nvlink/S5.yaml" in flat
    assert re.search(r"(?i)\bEFA\b[^.]*\b(default|remains|stays)\b|\b(default|remains|stays)\b[^.]*\bEFA\b", flat)
    assert "examples/08-pd-nvlink" in flat


# ----------------------------------------------------------------------------- the honesty rules over every document
@pytest.mark.parametrize("p", CLAIM_TEXT, ids=lambda p: str(p.relative_to(ROOT)))
def test_no_document_makes_a_claim_the_honesty_rules_forbid(p: Path) -> None:
    if not p.is_file():
        pytest.fail(f"{p.relative_to(ROOT)} missing")
    assert not forbidden_claims(p.read_text(encoding="utf-8"))


@pytest.mark.parametrize("p", CLAIM_TEXT, ids=lambda p: str(p.relative_to(ROOT)))
def test_no_figure_of_the_unfinished_p5e_series_is_quoted(p: Path) -> None:
    """Only the T1 parity figures and the 1P1D figures are allowed: nothing of the J2, J3 or ladder results."""
    if not p.is_file():
        pytest.fail(f"{p.relative_to(ROOT)} missing")
    text = p.read_text(encoding="utf-8")
    assert not SERIES_NOT_FINAL.search(text), SERIES_NOT_FINAL.findall(text)
    assert not re.search(r"kubecon" + r"-artifacts|nvlink-tests|matrix-p5e|PREREG|T1-RESULT", text)


def test_the_p5e_figures_appear_only_in_the_pd_guide_with_their_scope() -> None:
    for p in (README, DEPLOY, TROUBLE, EXAMPLES_README):
        text = _read(p)
        assert not any(f in text for f in ("0.1386", "0.1395", "1.0066")), f"{p.relative_to(ROOT)}: the T1 figures live in the PD guide"


# ----------------------------------------------------------------------------- the diagram
def _plain(value: str) -> str:
    return _flat(re.sub(r"<[^>]+>", " ", html.unescape(value)))


def _cells() -> dict[str, lib_xml.Element]:
    return {c.get("id") or "": c for c in lib_xml.parse(DRAWIO).iter("mxCell")}


def _box(cell: lib_xml.Element) -> tuple[float, float, float, float]:
    geo = cell.find("mxGeometry")
    assert geo is not None, cell.get("id")
    x, y = float(geo.get("x", "0")), float(geo.get("y", "0"))
    return x, y, x + float(geo.get("width", "0")), y + float(geo.get("height", "0"))


def _within(inner: lib_xml.Element, outer: lib_xml.Element) -> bool:
    ix0, iy0, ix1, iy1 = _box(inner)
    ox0, oy0, ox1, oy1 = _box(outer)
    return ox0 <= ix0 and oy0 <= iy0 and ix1 <= ox1 and iy1 <= oy1


def _texts() -> dict[str, str]:
    return {cid: _plain(c.get("value") or "") for cid, c in _cells().items() if c.get("value")}


def test_drawio_parses_and_has_no_cloudfront_waf_vpc_origin_or_nlb() -> None:
    cells = _cells()
    assert len(cells) > 10
    old_edge = r"(?i)cloud\s*front|\bWAF\b|vpc[ -]origin|\bNLB\b|network load balancer"
    legend_panel = cells["legend-panel"]
    for cid, text in _texts().items():
        cell = cells[cid]
        in_legend = cell.get("edge") != "1" and cell.find("mxGeometry") is not None and _within(cell, legend_panel)
        if in_legend:  # the legend may say what Terraform still builds; the components may not draw it
            if re.search(old_edge, text):
                assert "Terraform" in text and "off by default" in text, f"{cid}: the legend names the old edge only as what Terraform builds: {text!r}"
            continue
        assert not re.search(old_edge, text), f"{cid}: {text!r}"
    for cid, cell in cells.items():
        style = cell.get("style") or ""
        assert not re.search(r"(?i)cloudfront|\bwaf\b|network_load_balancer", style), f"{cid}: style names the old edge"
    assert "cloudfront" not in cells and "cf-sub" not in cells and "nlb" not in cells


def test_drawio_has_the_alb_in_front_of_grafana_labelled_as_reference_design() -> None:
    cells = _cells()
    texts = _texts()
    labels = [cid for cid, t in texts.items() if re.search(r"Application Load Balancer|\bALB\b", t)]
    assert labels, "an ALB label"
    icons = [cid for cid, c in cells.items() if "mxgraph.aws4.application_load_balancer" in (c.get("style") or "")]
    assert len(icons) == 1, icons
    edges = [c for c in cells.values() if c.get("edge") == "1" and icons[0] in (c.get("source"), c.get("target"))]
    assert any("grafana" in (c.get("source") or "") + (c.get("target") or "") for c in edges), "the ALB is tied to Grafana"
    assert all("dashed=1" in (c.get("style") or "") for c in edges), "optional, so dashed"


def test_drawio_draws_nvlink_between_prefill_and_decode_inside_the_gpu_node() -> None:
    cells = _cells()
    texts = _texts()
    nvlink = [cid for cid, t in texts.items() if "NVLink" in t]
    assert nvlink, "an NVLink label"
    for cid in nvlink:  # a drawn NVLink label is inside the node; the legend may name it too; the title caption is not a drawn label
        if cid != "subtitle" and cells[cid].get("edge") != "1" and cells[cid].find("mxGeometry") is not None and cells[cid].get("parent") == "1":
            assert _within(cells[cid], cells["gpu-node"]) or _within(cells[cid], cells["legend-panel"]), (
                f"{cid}: NVLink is drawn inside the p6-b200 node (or named in the legend)")
    kv_edges = [c for c in cells.values() if c.get("edge") == "1" and {c.get("source"), c.get("target")} == {"s7-prefill", "s7-decode"}]
    assert kv_edges, "the prefill-to-decode edge"
    on_edge = any("NVLink" in _plain(c.get("value") or "") for c in kv_edges)
    on_label = any(cells[cid].get("edge") != "1" and _within(cells[cid], cells["s7-panel"]) for cid in nvlink)
    assert on_edge or on_label, "the prefill-to-decode path is labelled NVLink"


def test_drawio_keeps_efa_for_the_traffic_between_nodes() -> None:
    texts = _texts()
    assert any(re.search(r"\bEFA\b", t) and re.search(r"(?i)(between|across|other) nodes", t) for t in texts.values()), (
        "a label says EFA is the path between nodes")
    cells = _cells()
    assert any("elastic_fabric_adapter" in (c.get("style") or "") for c in cells.values()), "the EFA icon stays"


def test_drawio_legend_carries_the_design_reference_label_and_the_honest_scope() -> None:
    cells = _cells()
    panel = cells["legend-panel"]
    legend = {cid: t for cid, t in _texts().items() if cells[cid].find("mxGeometry") is not None and cells[cid].get("edge") != "1"
              and _within(cells[cid], panel)}
    joined = _flat(" ".join(legend.values()))
    assert LABEL.search(joined), "the legend says 'reference design'"
    assert re.search(r"(?i)not (run|measured|recorded)", joined), "the legend says the NVLink path was not run (on p6-b200)"
    assert any(re.search(r"(?i)published", t) and "EFA" in t for t in legend.values()), "the legend says the published runs used EFA"
    assert "optional" in legend["lg-t8"] and "port-forward" in legend["lg-t8"]
    assert not forbidden_claims(" ".join(t + "." for t in _texts().values()))


def test_png_is_a_render_of_the_new_drawing_and_not_the_old_one() -> None:
    assert hashlib.sha256(DRAWIO.read_bytes()).hexdigest() != OLD_DRAWIO_SHA256, "the drawing still is the CloudFront + NLB one"
    data = PNG.read_bytes()
    assert hashlib.sha256(data).hexdigest() != OLD_PNG_SHA256, "the PNG still is the old export: re-export the new drawing"
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    width, height = struct.unpack(">II", data[16:24])
    boxes = [_box(c) for c in _cells().values() if c.get("vertex") == "1" and c.find("mxGeometry") is not None and c.get("parent") == "1"]
    content_ratio = (max(b[2] for b in boxes) - min(b[0] for b in boxes)) / (max(b[3] for b in boxes) - min(b[1] for b in boxes))
    assert abs((width / height) / content_ratio - 1) < 0.04, (
        f"PNG {width}x{height} does not match the extent of the drawing (ratio {content_ratio:.3f})")


def test_licenses_names_the_alb_icon_and_no_longer_the_old_edge_icons() -> None:
    text = _read(ROOT / "docs/static/LICENSES.md")
    assert "Application Load Balancer" in text
    assert "CloudFront" not in text and "Network Load Balancer" not in text


# ----------------------------------------------------------------------------- the Terraform edge is untouched
def test_grafana_edge_terraform_is_byte_identical_and_still_off_by_default() -> None:
    assert hashlib.sha256(GRAFANA_EDGE.read_bytes()).hexdigest() == GRAFANA_EDGE_SHA256, "terraform/cluster/grafana-edge.tf must not change"
    text = GRAFANA_EDGE.read_text(encoding="utf-8")
    assert "cloudfront" in text.lower() and re.search(r"(?i)load_balancer|\bnlb\b", text)
    variables = _read(ROOT / "terraform/cluster/variables.tf")
    assert re.search(r'variable "enable_grafana_edge" \{[^}]*default\s*=\s*false', variables)


# ----------------------------------------------------------------------------- hygiene of the new text
@pytest.mark.parametrize("p", NEW_TEXT, ids=lambda p: str(p.relative_to(ROOT)))
def test_new_text_carries_no_account_id_email_internal_host_or_local_path(p: Path) -> None:
    if not p.is_file():
        pytest.fail(f"{p.relative_to(ROOT)} missing")
    assert sanitize_scan.scan_text(p.read_text(encoding="utf-8")) == []


@pytest.mark.parametrize("p", NEW_TEXT, ids=lambda p: str(p.relative_to(ROOT)))
def test_new_text_carries_no_internal_vocabulary(p: Path) -> None:
    if not p.is_file():
        pytest.fail(f"{p.relative_to(ROOT)} missing")
    text = p.read_text(encoding="utf-8")
    patterns = [*e_guards.VOCABULARY, *f_guards.PRODUCT_EXTRA, *f_guards.ORIGINAL_REGION_PATTERNS]  # (pattern, label) pairs
    hits = [(what, m.group(0)) for pat, what in patterns for m in re.finditer(pat, text)]
    hits += [(what, m.group(0)) for what, pat in f_guards.PROCESS_WORDING.items() for m in re.finditer(pat, text)]
    assert not hits, hits
    assert not re.search(r"(?i)\bguardian\b|\bPREREG\b|\bSPEC-\d|transport_proof|\bG1[a-c]\b", text)


@pytest.mark.parametrize("p", NEW_TEXT, ids=lambda p: str(p.relative_to(ROOT)))
def test_every_path_and_script_a_new_text_names_exists(p: Path) -> None:
    """The check of ``test_sub_cycle_e_guards.test_every_path_and_script_named_in_a_public_doc_exists``, also for the files that are not tracked yet."""
    if not p.is_file():
        pytest.fail(f"{p.relative_to(ROOT)} missing")
    text = re.sub(r"[\w./-]*[<{$][^\s`)]*", "", p.read_text(encoding="utf-8"))
    bad = []
    for tok in sorted(ds._clean_candidates(text)):
        if tok in ds.GENERATED or tok.rstrip("/") in ds.GENERATED:
            continue
        if "*" in tok:
            if not glob.glob(str(ROOT / tok)) and not glob.glob(str(p.parent / tok)):
                bad.append(tok)
        elif not (ROOT / tok).exists() and not (p.parent / tok).exists():
            bad.append(tok)
    names = {m.group(1) for m in re.finditer(r"`([\w.-]+\.(?:sh|py))(?:\s[^`]*)?`", text)}
    have = {q.name for q in ROOT.rglob("*") if q.is_file() and ".git" not in q.parts and "__pycache__" not in q.parts}
    bad += sorted(n for n in names if n not in have)
    assert not bad, f"{p.relative_to(ROOT)} names paths or scripts that do not exist: {bad}"


@pytest.mark.parametrize("p", [GUIDE, TROUBLE, README, EXAMPLES_README, ROOT / "examples/08-pd-nvlink/README.md",
                               TQ / "variants-nvlink/README.md", TQ / "rendered-nvlink/README.md"], ids=lambda p: str(p.relative_to(ROOT)))
def test_every_relative_link_and_anchor_of_a_new_text_resolves(p: Path) -> None:
    if not p.is_file():
        pytest.fail(f"{p.relative_to(ROOT)} missing")
    text = p.read_text(encoding="utf-8")
    bad = []
    for m in re.finditer(r"\]\((?!https?://|mailto:)([^)\s]*)\)", text):
        target, _, frag = m.group(1).partition("#")
        dest = (p.parent / target).resolve() if target else p
        if target and not dest.exists():
            bad.append(m.group(1))
            continue
        if frag and dest.suffix == ".md":
            heads = [ds._slug(h) for h in re.findall(r"(?m)^#{1,6} (.+)$", dest.read_text(encoding="utf-8"))]
            if frag not in heads:
                bad.append(m.group(1))
    assert not bad, bad


def test_new_diagram_text_follows_the_inclusive_language_rules() -> None:
    for cid, text in _texts().items():
        assert not re.search(r"(?i)\b(master|slave|whitelist|blacklist)\b", text), f"{cid}: {text!r}"
