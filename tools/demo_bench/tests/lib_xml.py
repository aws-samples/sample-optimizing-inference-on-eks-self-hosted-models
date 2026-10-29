# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Parse an XML file into an ElementTree ``Element`` with defusedxml (as ``tests/publish/lib_xml.py``).

``xml.etree.ElementTree.parse`` expands the entities a document declares, so a crafted file can blow up into
gigabytes (billion laughs) or pull in an external resource (XXE). ``parse`` below is ``defusedxml.ElementTree.parse``
with its defaults: any entity declaration (internal, external or unparsed) and any external entity reference raises
``ForbiddenXmlError`` (``defusedxml.DefusedXmlException``) before anything is expanded. A DOCTYPE without
declarations is accepted (the SVG files matplotlib writes carry one) and the external DTD it names is never read.
The tree is the one ``xml.etree.ElementTree.parse`` builds, with its ``{namespace}tag`` names. defusedxml is pinned
in the ``dev`` group of ``pyproject.toml``; this module imports nothing from the standard library ``xml`` package.
"""

from __future__ import annotations

from pathlib import Path

from defusedxml import DefusedXmlException
from defusedxml.ElementTree import ParseError, fromstring
from defusedxml.ElementTree import parse as _defused_parse

# The class of the elements ``parse`` returns, ``xml.etree.ElementTree.Element``, read from a parsed document so that
# this module needs no import from the ``xml`` package.
Element = type(fromstring("<root/>"))

# The base class of what defusedxml raises for a refused document: EntitiesForbidden and ExternalReferenceForbidden.
ForbiddenXmlError = DefusedXmlException

__all__ = ["Element", "ForbiddenXmlError", "ParseError", "parse"]


def parse(path: Path) -> Element:
    """Parse the XML file ``path`` and return its root element.

    Args:
        path: The file to parse.

    Returns:
        The root element, as ``xml.etree.ElementTree.parse(path).getroot()`` returns it.

    Raises:
        ForbiddenXmlError: The document declares an entity or references an external one.
        ParseError: The document is not well-formed.
    """
    return _defused_parse(path).getroot()
