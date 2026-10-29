# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""``tests/lib_xml.py``: XML is parsed with entity declarations and external entities refused.

The refused documents are the classic attacks: the billion-laughs entity bomb, an external entity (XXE), an external
parameter entity and an unparsed entity. The accepted ones cover what the tests parse: the SVG matplotlib writes
(DOCTYPE, namespaces, character references, mixed content) and a JUnit-shaped report. The helper parses with
defusedxml (pinned in the ``dev`` group of ``pyproject.toml``) and imports nothing from the standard library ``xml``
package (static analysis, Semgrep rule ``python.lang.security.use-defused-xml``).
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

import lib_xml

SVG = "{http://www.w3.org/2000/svg}"


def _billion_laughs() -> str:
    """Return the entity bomb: nine levels of ten references each, about 3 GB of text once expanded."""
    decls = ['<!ENTITY lol0 "lol">'] + [f'<!ENTITY lol{i} "{f"&lol{i - 1};" * 10}">' for i in range(1, 10)]
    return '<?xml version="1.0"?>\n<!DOCTYPE lolz [\n' + "\n".join(decls) + "\n]>\n<lolz>&lol9;</lolz>\n"


REFUSED = {
    "billion-laughs": _billion_laughs(),
    "external-entity": '<?xml version="1.0"?><!DOCTYPE x [<!ENTITY e SYSTEM "file:///dev/null">]><x>&e;</x>',
    "external-parameter-entity": '<?xml version="1.0"?><!DOCTYPE x [<!ENTITY % p SYSTEM "file:///dev/null"> %p;]><x/>',
    "unparsed-entity": '<!DOCTYPE x [<!NOTATION n SYSTEM "n"><!ENTITY e SYSTEM "file:///dev/null" NDATA n>]><x/>',
    "internal-entity": '<!DOCTYPE x [<!ENTITY e "text">]><x>&e;</x>',
}


def _write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "doc.xml"
    path.write_text(text, encoding="utf-8")
    return path


@pytest.mark.parametrize("name", sorted(REFUSED))
def test_entity_declarations_and_external_entities_are_refused(tmp_path: Path, name: str) -> None:
    with pytest.raises(lib_xml.ForbiddenXmlError):
        lib_xml.parse(_write(tmp_path, REFUSED[name]))


def test_the_billion_laughs_sample_is_the_real_bomb() -> None:
    """Guards the guard: the sample declares the ten nested levels, so the refusal above is not vacuous."""
    text = REFUSED["billion-laughs"]
    assert text.count("<!ENTITY") == 10 and text.count("&lol8;") == 10 and "<lolz>&lol9;</lolz>" in text


def test_a_doctype_without_declarations_namespaces_and_mixed_content_parse_as_elementtree_does(tmp_path: Path) -> None:
    doc = (
        '<?xml version="1.0" encoding="utf-8" standalone="no"?>\n'
        '<!DOCTYPE svg PUBLIC "-//W3C//DTD SVG 1.1//EN" "http://www.w3.org/Graphics/SVG/1.1/DTD/svg11.dtd">\n'
        '<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" width="16">'
        '<text x="1">A &amp; &#66;<tspan>C</tspan> tail</text><use xlink:href="#m"/><!-- note --></svg>'
    )
    root = lib_xml.parse(_write(tmp_path, doc))
    assert isinstance(root, lib_xml.Element)
    assert (root.tag, root.get("width")) == (f"{SVG}svg", "16")
    text, use = list(root)
    assert (text.tag, text.text, "".join(text.itertext())) == (f"{SVG}text", "A & B", "A & BC tail")
    assert text[0].tail == " tail"
    assert use.get("{http://www.w3.org/1999/xlink}href") == "#m"


def test_a_junit_report_parses_with_its_attributes(tmp_path: Path) -> None:
    doc = '<testsuites><testsuite><testcase classname="m" name="t"><skipped message="why"/></testcase></testsuite></testsuites>'
    root = lib_xml.parse(_write(tmp_path, doc))
    case = next(root.iter("testcase"))
    skipped = case.find("skipped")
    assert skipped is not None and (case.get("classname"), skipped.get("message")) == ("m", "why")


def test_a_malformed_document_raises_the_parse_error(tmp_path: Path) -> None:
    with pytest.raises(lib_xml.ParseError):
        lib_xml.parse(_write(tmp_path, "<a><b></a>"))


def test_lib_xml_parses_with_defusedxml_and_imports_nothing_from_the_xml_package() -> None:
    """Static analysis, Semgrep rule ``python.lang.security.use-defused-xml``: the helper imports defusedxml, not the stdlib ``xml``."""
    source = Path(lib_xml.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    modules = [a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names]
    modules += [n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)]
    assert not [m for m in modules if m == "xml" or m.startswith("xml.")], modules
    assert [m for m in modules if m == "defusedxml" or m.startswith("defusedxml.")], modules
    assert "nosemgrep" not in source and "nosec" not in source, "nothing left to suppress"


@pytest.mark.parametrize("name", sorted(REFUSED))
def test_the_refusals_are_the_defusedxml_exceptions(tmp_path: Path, name: str) -> None:
    import defusedxml  # pinned in tests/requirements-test.txt and in the dev group of tools/demo_bench

    assert issubclass(lib_xml.ForbiddenXmlError, defusedxml.DefusedXmlException)
    with pytest.raises(defusedxml.DefusedXmlException):
        lib_xml.parse(_write(tmp_path, REFUSED[name]))
