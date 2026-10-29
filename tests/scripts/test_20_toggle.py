# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""`scripts/50-toggle-router.sh <rr|kv> <namespace>` (spec 9.6)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

import lib_scripts as S


def _sb(tmp_path: Path) -> S.Sandbox:
    return S.Sandbox(tmp_path, {})


def _rendered_overlay(res: S.Result, mode: str) -> bool:
    for c in res.kubectl_calls():
        if any(a.rstrip("/").endswith(f"frontends/{mode}") for a in c["argv"]):
            return True
    return False


@pytest.mark.parametrize("mode", ["rr", "kv"])
def test_applies_rendered_frontend_overlay_server_side(tmp_path: Path, mode: str) -> None:
    """spec 9.6: render `frontends/<mode>` and `kubectl apply --server-side` it into the target namespace."""
    res = _sb(tmp_path).run("50-toggle-router.sh", mode, "s-agg")
    assert res.rc == 0, res.stdout + res.stderr
    assert _rendered_overlay(res, mode), "frontends/<mode> overlay was never rendered or applied"
    applies = [c for c in res.kubectl_calls() if "apply" in c["argv"]]
    assert len(applies) == 1, [c["argv"] for c in applies]
    assert S.has_arg(applies[0]["argv"], "--server-side")
    joined = " ".join(applies[0]["argv"])
    assert re.search(r"(?:-n|--namespace)[ =]s-agg\b", joined), joined


@pytest.mark.parametrize("mode", ["rr", "kv"])
def test_never_merge_patches_components(tmp_path: Path, mode: str) -> None:
    """spec 9.6: a merge-patch of the v1beta1 `components` list REPLACES it; the script must never call `kubectl patch`."""
    res = _sb(tmp_path).run("50-toggle-router.sh", mode, "s-dis")
    assert res.rc == 0, res.stdout + res.stderr
    assert not [c for c in res.kubectl_calls() if "patch" in c["argv"]], "kubectl patch is forbidden"
    assert not [c for c in res.kubectl_calls() if "replace" in c["argv"]]
    assert "--type" not in " ".join(c for call in res.kubectl_calls() for c in call["argv"])


def test_prints_elapsed_time(tmp_path: Path) -> None:
    """spec 9.6 / 12.2: the toggle is timed and prints the elapsed seconds."""
    res = _sb(tmp_path).run("50-toggle-router.sh", "kv", "s-agg-hic")
    assert res.rc == 0, res.stdout + res.stderr
    assert re.search(r"(?i)elapsed[^\n]*\d", res.stdout + res.stderr), res.stdout


@pytest.mark.parametrize(
    "args",
    [(), ("rr",), ("fast", "s-agg"), ("KV", "s-agg"), ("rr", "default")],
    ids=["no-args", "no-namespace", "unknown-mode", "wrong-case-mode", "non-scenario-namespace"],
)
def test_rejects_invalid_arguments(tmp_path: Path, args: tuple[str, ...]) -> None:
    """spec 6.3/9.6: only rr|kv and an s-* scenario namespace are accepted; nothing is applied otherwise."""
    res = _sb(tmp_path).run("50-toggle-router.sh", *args)
    assert res.rc != 0
    assert not [c for c in res.kubectl_calls() if "apply" in c["argv"]]
