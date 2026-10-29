# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Shared helpers for the dashboard tests (parsing, PromQL scanning, allowlists)."""
from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any, Iterator

import pytest
import yaml

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
DASH = ROOT / "dashboards"
DYNAMO_REPO = Path(os.environ.get("DYNAMO_REPO", ROOT.parent / "dynamo"))
TAG = "v1.5.1"
CATALOG_PATH = "docs/fern/pages/reference/observability/metrics-catalog.mdx"

VENDORED = {
    "grafana-dynamo-dashboard-configmap.yaml": "deploy/observability/grafana-dynamo-dashboard-configmap.yaml",
    "grafana-disagg-dashboard-configmap.yaml": "deploy/observability/grafana-disagg-dashboard-configmap.yaml",
}

NS_RE = re.compile(r'namespace\s*=~?\s*"\$\{?namespace\}?"')
METRIC_RE = re.compile(r"(?<![A-Za-z0-9_:])(dynamo_[A-Za-z0-9_]+|sglang:[A-Za-z0-9_]+)")
SUFFIX_RE = re.compile(r"_(bucket|sum|count)$")


def git_show(path: str) -> str:
    """Return a file from the Dynamo repo at tag v1.5.1.

    Skips the calling test, with a reason naming ``DYNAMO_REPO``, when there is no Dynamo checkout (as tests/manifests and
    tests/publish do); a checkout that lacks the tag or the file still fails loudly.
    """
    if not (DYNAMO_REPO / ".git").exists():
        pytest.skip(f"needs a Dynamo checkout at {TAG}: set DYNAMO_REPO (default: a sibling directory named 'dynamo')")
    out = subprocess.run(
        ["git", "-C", str(DYNAMO_REPO), "show", f"{TAG}:{path}"],
        capture_output=True, text=True, check=False,
    )
    assert out.returncode == 0, f"cannot read {TAG}:{path} from {DYNAMO_REPO}: {out.stderr}"
    return out.stdout


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text())  # type: ignore[no-any-return]


def configmap_dashboards(path: Path) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    """Return (configmap, {data_key: parsed dashboard JSON})."""
    cm = yaml.safe_load(path.read_text())
    return cm, {k: json.loads(v) for k, v in cm["data"].items()}


def iter_panels(node: dict[str, Any]) -> Iterator[dict[str, Any]]:
    """Yield all non-row panels, descending into collapsed rows."""
    for p in node.get("panels", []):
        if p.get("type") != "row":
            yield p
        yield from (q for q in iter_panels(p) if q is not p)


def panels_by_title(dash: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {p["title"]: p for p in iter_panels(dash) if "title" in p}


def exprs(panel: dict[str, Any]) -> list[str]:
    return [t["expr"] for t in panel.get("targets", []) if "expr" in t]


def all_exprs(dash: dict[str, Any]) -> list[str]:
    return [e for p in iter_panels(dash) for e in exprs(p)]


def metric_names(expr: str) -> set[str]:
    """Extract dynamo_*/sglang:* metric names, ignoring label names and matcher bodies."""
    s = re.sub(r"\b(by|without)\s*\([^)]*\)", " ", expr)
    s = re.sub(r"\{[^}]*\}", "{}", s)
    return set(METRIC_RE.findall(s))


# A matcher body may contain Grafana `${var}` interpolations, so `}` alone does not end it.
BRACES = r"\{(?:[^{}]|\$\{[^}]*\})*\}"
SELECTOR_RE = re.compile(rf"(?<![A-Za-z0-9_:])((?:dynamo_|sglang:|nixl_)[A-Za-z0-9_:]+)({BRACES})?")
# Label-name lists: by(...)/without(...)/on(...)/ignoring(...)/group_left(...)/group_right(...).
LABEL_LIST_RE = re.compile(r"\b(?:by|without|on|ignoring|group_left|group_right)\s*\([^)]*\)")


def metric_selectors(expr: str) -> list[tuple[str, str | None]]:
    """Return ``(metric_name, matcher_body_or_None)`` for each metric selector in a PromQL string.

    Label names are NOT metrics: identifiers inside ``by/without/on/ignoring/group_*(...)`` and
    label keys inside ``{...}`` matchers (e.g. ``dynamo_component_type``,
    ``dynamo_graph_deployment``, labels added by the Dynamo v1.5.1 operator PodMonitors) are
    skipped. Only identifiers in metric position are returned.
    """
    masked = LABEL_LIST_RE.sub(lambda m: " " * len(m.group(0)), expr)
    braces = [m.span() for m in re.finditer(BRACES, masked)]
    out: list[tuple[str, str | None]] = []
    for m in SELECTOR_RE.finditer(masked):
        if any(a < m.start() < b for a, b in braces):
            continue  # a label key (or value) inside someone's {...} matcher
        out.append((m.group(1), m.group(2)))
    return out


def _read_list(path: Path) -> set[str]:
    return {ln.strip() for ln in path.read_text().splitlines() if ln.strip() and not ln.startswith("#")}


def allowlist() -> set[str]:
    return _read_list(HERE / "allowlist_dynamo.txt") | _read_list(HERE / "allowlist_sglang.txt")


def is_known(name: str, allowed: set[str]) -> bool:
    return name in allowed or SUFFIX_RE.sub("", name) in allowed
