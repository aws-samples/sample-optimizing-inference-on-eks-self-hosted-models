# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Vendored Dynamo ConfigMaps: byte-identical to v1.5.1 apart from header/label."""
from __future__ import annotations

import re

import pytest

import _lib as L

LABEL_RE = re.compile(r'^[ \t]+grafana_dashboard: "1"\n', re.M)


def body(text: str) -> str:
    """Text from the first top-level 'apiVersion:' line (drops any header comments)."""
    m = re.search(r"^apiVersion:", text, re.M)
    assert m, "no apiVersion line"
    return text[m.start():]


@pytest.fixture(params=sorted(L.VENDORED))
def vendored(request):
    path = L.DASH / "dynamo" / request.param
    assert path.is_file(), f"missing {path}"
    return request.param, path.read_text()


def test_dashboards_dir_exists():
    assert (L.DASH / "dynamo").is_dir()


def test_body_identical_to_v151(vendored):
    name, text = vendored
    upstream = L.git_show(L.VENDORED[name])
    norm = lambda t: LABEL_RE.sub("", body(t))
    assert norm(text) == norm(upstream), f"{name} differs from {L.TAG} beyond header/label"


def test_apache_attribution_header(vendored):
    name, text = vendored
    header = text[: re.search(r"^apiVersion:", text, re.M).start()]
    assert "Apache-2.0" in header
    assert "NVIDIA" in header
    assert L.TAG in header
    assert L.VENDORED[name] in header, "header must cite the upstream path"


def test_label_and_namespace(vendored):
    name, text = vendored
    cm, dashes = L.configmap_dashboards(L.DASH / "dynamo" / name)
    assert cm["kind"] == "ConfigMap"
    assert cm["metadata"]["labels"]["grafana_dashboard"] == "1"
    assert cm["metadata"]["namespace"] == "monitoring"
    assert dashes, "no dashboard JSON in data"
