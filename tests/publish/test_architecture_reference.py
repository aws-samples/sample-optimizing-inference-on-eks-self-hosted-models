# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The reference architecture: the figure, plus the hygiene of the PD documents.

* ``docs/static/architecture.drawio`` / ``.png``: no CloudFront, WAF, VPC origin or NLB; an ALB in front of Grafana; the
  KV edge from the prefill to the decode pod is a solid EFA-orange line inside the GPU node; a PNG that is a render of THIS
  drawing, not the old one.
* ``terraform/cluster/grafana-edge.tf`` is byte-identical (the scope of the Grafana change is the diagram and the text only).
* the README, the guides and the PD example READMEs: no account id, every path they name exists, every relative link and
  anchor resolves.

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
EFA_ORANGE = "#ED7100"  # the colour of the EFA path between the prefill and the decode pods
# The figure as it was published (CloudFront + NLB, EFA only).  The new drawing and its PNG must differ from it.
OLD_DRAWIO_SHA256 = "e0dd479e90a2a8ac681d63bea99b10c7691511d08d2416ba5310f1ec4bf24650"
OLD_PNG_SHA256 = "17ae02d904e2f4df35b589d75fe1975f7528d927ed9ef838d69b7128dd2f904f"
PD_EXAMPLES = ["03-pd-disaggregation", "05-kv-router-pd", "06-pd-hicache", "07-all-three"]
PD_READMES = [ROOT / "examples" / f / "README.md" for f in PD_EXAMPLES]
NEW_TEXT = [README, GUIDE, TROUBLE, DEPLOY, EXAMPLES_README, *PD_READMES]
MARKER = re.compile(r"\(unpublished internal measurements?\)")


def _read(p: Path) -> str:
    assert p.is_file(), f"{p.relative_to(ROOT)} missing"
    return p.read_text(encoding="utf-8")


def _flat(text: str) -> str:
    return " ".join(text.split())


def _sentences(text: str) -> list[str]:
    return qf._sentences(text)


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


def test_drawio_draws_the_kv_edge_between_prefill_and_decode_in_efa_orange_inside_the_gpu_node() -> None:
    cells = _cells()
    edge = cells.get("e-s7-kv")
    assert edge is not None and edge.get("edge") == "1", "the prefill-to-decode edge e-s7-kv"
    assert (edge.get("source"), edge.get("target")) == ("s7-prefill", "s7-decode")
    for cid in ("s7-prefill", "s7-decode"):
        assert _within(cells[cid], cells["gpu-node"]), f"{cid} is drawn inside the p6-b200 node"
    style = dict(kv.split("=", 1) for kv in (edge.get("style") or "").split(";") if "=" in kv)  # draw.io: the last key wins
    assert style.get("dashed", "0") != "1", "the KV path is a solid line"
    assert style.get("strokeColor", "").upper() == EFA_ORANGE, f"the EFA path is drawn in {EFA_ORANGE}: {style.get('strokeColor')}"


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
def test_every_path_and_script_a_new_text_names_exists(p: Path) -> None:
    """The check of ``test_public_docs_and_header_guards.test_every_path_and_script_named_in_a_public_doc_exists``, also for the files that are not tracked yet."""
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


@pytest.mark.parametrize("p", [GUIDE, TROUBLE, README, DEPLOY, EXAMPLES_README, *PD_READMES], ids=lambda p: str(p.relative_to(ROOT)))
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


