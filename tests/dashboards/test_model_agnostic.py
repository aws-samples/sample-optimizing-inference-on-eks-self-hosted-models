# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Dashboards are model-agnostic: the queries carry no model filter, so no visible text may name a model.

The same boards are shown for recorded runs of different models.  A title that names one model is wrong for the others.
Also: every hand-authored dashboard JSON must stay byte-consistent with its generated ConfigMap (README: JSON is the source of truth).
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Iterator

import pytest
import yaml

import _lib as L

# Model families / names that must never appear in visible dashboard text.
# Architecture examples in upstream help text (e.g. "hybrid SWA models like Gemma-2") explain a metric, not the run, so they are out of scope.
MODEL_RE = re.compile(r"g" + r"lm|qwen|gpt[-_ ]?oss|kimi|llama|deepseek|mistral|mixtral|nemotron|minimax", re.IGNORECASE)

JSONS = sorted(L.DASH.glob("demo/*.json")) + [L.DASH / "sglang" / "sglang.json"]
VENDORED_CMS = sorted(L.DASH.glob("dynamo/*.yaml"))


def _walk(panels: list[dict[str, Any]]) -> Iterator[dict[str, Any]]:
    for p in panels:
        yield p
        yield from _walk(p.get("panels", []))


def visible_texts(dash: dict[str, Any]) -> list[tuple[str, str]]:
    """Return (where, text) for dashboard title/description, every panel/row title and description, and text-panel content."""
    out: list[tuple[str, str]] = [("dashboard title", dash.get("title", "")), ("dashboard description", dash.get("description", ""))]
    for p in _walk(dash.get("panels", [])):
        label = f"{p.get('type', '?')} panel {p.get('title', '')!r}"
        out.append((f"{label} title", p.get("title", "")))
        out.append((f"{label} description", p.get("description", "")))
        if p.get("type") == "text":
            out.append((f"{label} content", p.get("options", {}).get("content", "")))
    return out


def test_dashboard_files_exist() -> None:
    assert len(JSONS) >= 4
    for j in JSONS:
        assert j.is_file(), f"missing {j}"


@pytest.mark.parametrize("path", JSONS, ids=lambda p: p.name)
def test_no_model_named_in_visible_text(path: Path) -> None:
    hits = [(w, t) for w, t in visible_texts(L.load_json(path)) if MODEL_RE.search(t)]
    assert not hits, f"{path.name} names a model: " + "; ".join(f"{w}: {MODEL_RE.search(t).group(0)!r}" for w, t in hits)  # type: ignore[union-attr]


@pytest.mark.parametrize("path", VENDORED_CMS, ids=lambda p: p.name)
def test_vendored_dashboards_name_no_model(path: Path) -> None:
    _, dashes = L.configmap_dashboards(path)
    hits = [(w, t) for d in dashes.values() for w, t in visible_texts(d) if MODEL_RE.search(t)]
    assert not hits, f"{path.name} names a model: {hits}"


def test_scanner_detects_a_model_name() -> None:
    """Guard against a vacuous scanner: it must flag a synthetic title, row title and text panel."""
    d = {"title": "Llama-4 board", "panels": [{"type": "row", "title": "Qwen row", "panels": [{"type": "stat", "title": "x", "description": "gpt-oss"}]},
                                              {"type": "text", "title": "t", "options": {"content": "Kimi K2"}}]}
    assert sum(1 for _, t in visible_texts(d) if MODEL_RE.search(t)) == 4


@pytest.mark.parametrize("path", sorted(L.DASH.glob("demo/*.json")), ids=lambda p: p.name)
def test_configmap_is_byte_consistent_with_json(path: Path) -> None:
    cm_path = path.with_name(path.stem + "-configmap.yaml")
    assert cm_path.is_file(), f"missing generated ConfigMap {cm_path.name}"
    cm = yaml.safe_load(cm_path.read_text())
    assert set(cm["data"]) == {path.name}, f"{cm_path.name} must hold exactly the key {path.name}"
    assert cm["data"][path.name] == path.read_text(), f"{cm_path.name} is stale: regenerate from {path.name} (dashboards/README.md)"
    assert json.loads(cm["data"][path.name]) == L.load_json(path)
