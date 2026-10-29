# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The architecture diagram of docs/static: shipped as drawn, valid, shown in the README and attributed.

* ``architecture.drawio`` (draw.io source) and ``architecture.png`` (its export) are tracked and are the drawing that was
  reviewed, byte for byte (sha256 pinned below: editing the picture is a new review and re-pins it).
* The .drawio parses as XML (``mxfile`` > ``diagram`` > ``mxGraphModel`` > ``root`` of ``mxCell``); the PNG has the PNG
  signature, an IHDR and an IEND chunk and stays at most 1.5 MiB.
* The README Architecture section shows the PNG and links the .drawio and ``docs/static/LICENSES.md``.
* LICENSES.md names every embedded logo image of the .drawio (cell id and the sha256 of its SVG), the three products,
  the AWS Architecture Icons terms and the trademark sentence.  The embedded SVGs are exactly the approved ones of
  ``APPROVED_SVGS`` and LICENSES.md lists no other SVG digest (Grafana is a text label, without its logo).
* The labels that restate a fact of the repository follow it: the Region of the tfvars example, the version pins of
  ``versions.env``, the NodePool of the Frontend manifest, the optional (off by default) CloudFront edge with the
  ``kubectl port-forward`` default of Grafana, and the NGC runtime image with an optional ECR copy.
* ``e_guards.AS_DRAWN_HITS``, the one exemption the diagram has, is exact both ways: the .drawio carries those guard hits,
  no more and no fewer, and no other file is exempt.
"""

from __future__ import annotations

import base64
import hashlib
import re
import struct
from collections import Counter

import lib_xml
import sanitize_scan
import test_docs_set as ds
import test_sub_cycle_e_guards as e_guards
import test_sub_cycle_f_guards as f_guards

ROOT = sanitize_scan.REPO_ROOT
DRAWIO = "docs/static/architecture.drawio"
PNG = "docs/static/architecture.png"
LICENSES = "docs/static/LICENSES.md"
SHA256 = {
    DRAWIO: "e0dd479e90a2a8ac681d63bea99b10c7691511d08d2416ba5310f1ec4bf24650",
    PNG: "17ae02d904e2f4df35b589d75fe1975f7528d927ed9ef838d69b7128dd2f904f",
}
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
PNG_MAX_BYTES = 1536 * 1024
APPROVED_SVGS = {  # product -> sha256 of its embedded SVG, as LICENSES.md lists it; no other SVG may be embedded
    "NVIDIA Dynamo": "4019188fbddfe63ae7a9ac420a19191e951921157d63b773d2ae3b61ff03728e",
    "SGLang": "09ccea391ea4da9d896d6fae0f31e5a52a456bfe837f266a99a69c2242d95fe3",
    "Prometheus": "a96d48cc7f1c91de940dacbf30581850fd2c0ed4afaf1b0b94fb86eb1d0aef6e",
}
PRODUCTS = tuple(APPROVED_SVGS)
OPTIONAL_EDGE = ("e-graf-nlb", "e-nlb-cf", "e-cf-viewer")  # Grafana -> internal NLB -> CloudFront -> browser
TRADEMARK_SENTENCE = (
    "Logos are trademarks of their owners; the project licenses cover the software/artwork as stated upstream and do not "
    "grant any endorsement."
)
SVG_DATA_URI = re.compile(r"image=data:image/svg\+xml(?:;base64)?,([A-Za-z0-9+/=]+)")


def _embedded_svgs() -> dict[str, str]:
    """Map each ``mxCell`` id of the .drawio that embeds an SVG image to the sha256 of the decoded SVG."""
    out: dict[str, str] = {}
    for cell in lib_xml.parse(ROOT / DRAWIO).iter("mxCell"):
        m = SVG_DATA_URI.search(cell.get("style") or "")
        if m:
            payload = m.group(1)
            out[cell.get("id") or ""] = hashlib.sha256(base64.b64decode(payload + "=" * (-len(payload) % 4))).hexdigest()
    return out


def test_diagram_files_are_tracked() -> None:
    tracked = set(sanitize_scan.tracked_files())
    missing = [rel for rel in (DRAWIO, PNG, LICENSES) if rel not in tracked]
    assert not missing, f"not tracked: {missing}"


def test_diagram_is_shipped_as_drawn() -> None:
    """Both files are byte-identical to the reviewed drawing."""
    for rel, want in SHA256.items():
        assert hashlib.sha256((ROOT / rel).read_bytes()).hexdigest() == want, f"{rel} differs from the reviewed drawing"


def test_drawio_parses_as_a_drawio_file() -> None:
    root = lib_xml.parse(ROOT / DRAWIO)
    assert root.tag == "mxfile", root.tag
    models = root.findall("./diagram/mxGraphModel")
    assert len(models) == 1, "one diagram page with an uncompressed mxGraphModel"
    cells = models[0].findall("./root/mxCell")
    assert len(cells) > 10, f"only {len(cells)} cells"


def test_png_is_a_valid_png_of_reasonable_size() -> None:
    data = (ROOT / PNG).read_bytes()
    assert data[:8] == PNG_SIGNATURE, "PNG signature"
    length, kind = struct.unpack(">I4s", data[8:16])
    assert kind == b"IHDR" and length == 13, "the first chunk is IHDR"
    width, height = struct.unpack(">II", data[16:24])
    assert width >= 1000 and height >= 500, f"{width}x{height}: too small to read"
    assert data[-8:-4] == b"IEND", "the last chunk is IEND"
    assert 10 * 1024 < len(data) <= PNG_MAX_BYTES, f"{len(data)} bytes (want at most {PNG_MAX_BYTES})"


def test_readme_architecture_section_shows_the_png_and_links_its_sources() -> None:
    sec = ds._section(ds.README.read_text(encoding="utf-8"), r"architect")
    assert re.search(r"!\[[^\]]+\]\(docs/static/architecture\.png\)", sec), "the PNG, with alt text, in the Architecture section"
    for target in (DRAWIO, LICENSES):
        assert f"]({target})" in sec, f"the Architecture section links {target}"
    for rel in (DRAWIO, PNG, LICENSES):
        assert (ROOT / rel).is_file(), rel


def test_licenses_names_every_embedded_logo() -> None:
    text = (ROOT / LICENSES).read_text(encoding="utf-8")
    svgs = _embedded_svgs()
    assert len(svgs) == 7, f"7 embedded logo cells: {sorted(svgs)}"
    unapproved = {cell: digest for cell, digest in svgs.items() if digest not in APPROVED_SVGS.values()}
    assert not unapproved, f"embedded SVGs that are not an approved logo: {unapproved}"
    assert set(svgs.values()) == set(APPROVED_SVGS.values()), "every approved logo is drawn"
    extra = set(re.findall(r"\b[0-9a-f]{64}\b", text)) - set(APPROVED_SVGS.values())
    assert not extra, f"{LICENSES} lists SVG digests that are not an approved logo: {extra}"
    missing = [cell for cell in svgs if f"`{cell}`" not in text]
    missing += [digest for digest in set(svgs.values()) if digest not in text]
    missing += [product for product in PRODUCTS if product not in text]
    assert not missing, f"{LICENSES} does not name: {missing}"
    assert TRADEMARK_SENTENCE in text
    assert "https://aws.amazon.com/architecture/icons/" in text, "the AWS Architecture Icons terms"


def _cells() -> dict[str, lib_xml.Element]:
    """Map each ``mxCell`` id of the .drawio to its element."""
    return {cell.get("id") or "": cell for cell in lib_xml.parse(ROOT / DRAWIO).iter("mxCell")}


def _inside(inner: lib_xml.Element, outer: lib_xml.Element) -> bool:
    """True when the geometry of ``inner`` lies within the geometry of ``outer`` (both drawn on the top-level layer)."""
    def box(cell: lib_xml.Element) -> tuple[float, float, float, float]:
        geo = cell.find("mxGeometry")
        assert geo is not None, cell.get("id")
        x, y = float(geo.get("x", "0")), float(geo.get("y", "0"))
        return x, y, x + float(geo.get("width", "0")), y + float(geo.get("height", "0"))
    ix0, iy0, ix1, iy1 = box(inner)
    ox0, oy0, ox1, oy1 = box(outer)
    return ox0 <= ix0 and oy0 <= iy0 and ix1 <= ox1 and iy1 <= oy1


def test_diagram_labels_follow_the_repository() -> None:
    """The labels that restate a fact of the repository say what the repository says."""
    cells = _cells()
    value = {cid: cell.get("value") or "" for cid, cell in cells.items()}
    style = {cid: cell.get("style") or "" for cid, cell in cells.items()}

    tfvars = (ROOT / "terraform/cluster/terraform.tfvars.example").read_text(encoding="utf-8")
    region = re.search(r'^aws_region\s*=\s*"([^"]+)"', tfvars, re.M)
    assert region, "aws_region in terraform/cluster/terraform.tfvars.example"
    assert value["region"] == f"Region · {region.group(1)}", value["region"]

    pins = dict(line.split("=", 1) for line in (ROOT / "versions.env").read_text(encoding="utf-8").splitlines()
                if line and not line.startswith("#"))
    for want in (f"SGLang {pins['SGLANG_VERSION']} ", f"NVIDIA Dynamo {pins['DYNAMO_RUNTIME_VERSION']} runtime",
                 f"Dynamo operator {pins['DYNAMO_OPERATOR_CHART_VERSION']} "):
        assert want in value["versions"], f"versions label {value['versions']!r} lacks {want!r} (versions.env)"

    frontend = (ROOT / "manifests/track-q-qwen38/frontend/dgd.yaml").read_text(encoding="utf-8")
    pool = re.search(r"karpenter\.sh/nodepool:\s*(\S+)", frontend)
    assert pool, "the Frontend DGD pins a NodePool"
    assert f"NodePool {pool.group(1)}" in value["gpu-node"], "the GPU node box names the NodePool of the Frontend"
    for fe in ("s0-frontend", "s7-frontend"):
        assert _inside(cells[fe], cells["gpu-node"]), f"{fe} is drawn on the node of NodePool {pool.group(1)}"

    variables = (ROOT / "terraform/cluster/variables.tf").read_text(encoding="utf-8")
    for var in ("enable_grafana_edge", "create_runtime_mirror_repository"):
        assert re.search(rf'variable "{var}" \{{[^}}]*default\s*=\s*false', variables), f"{var} defaults to false"
    for edge in OPTIONAL_EDGE:
        assert "dashed=1" in style[edge], f"{edge}: the CloudFront edge is optional (enable_grafana_edge = false)"
    assert "optional" in value["cf-sub"] and "optional" in value["lg-t8"], "the edge is labelled optional"
    assert "port-forward" in value["lg-t8"], "the legend names the default way to Grafana"
    assert "NGC" in value["lg-t2"] and "optional" in value["ecr-sub"], "runtime image from NGC, ECR copy optional"

    text = (ROOT / DRAWIO).read_text(encoding="utf-8")
    assert not re.search(r"(?i)speak" + r"ers?\b", text), "neutral wording for the browser"


def test_as_drawn_exemption_is_exact() -> None:
    """The pinned hits are all the guard hits of the .drawio, process wording included (which has no exemption)."""
    assert set(e_guards.AS_DRAWN_HITS) == {DRAWIO}, "only the diagram source is exempt"
    assert sanitize_scan.read_text(PNG) is None, "the PNG is binary: the text guards skip it, so it needs no entry"
    text = sanitize_scan.read_text(DRAWIO) or ""
    patterns = [*e_guards.VOCABULARY, *f_guards.PRODUCT_EXTRA, *f_guards.ORIGINAL_REGION_PATTERNS,
                *((pat, what) for what, pat in f_guards.PROCESS_WORDING.items())]
    found = Counter((what, m.group(0)) for pat, what in patterns for m in re.finditer(pat, text))
    assert found == e_guards.AS_DRAWN_HITS[DRAWIO]
