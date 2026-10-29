# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The design log is internal: nothing in the public tree points at it.

``docs/sdd/`` (plan, specification and task log of the work behind the sample) stays in the repository byte-identical,
but it is excluded from the public export, so every public sentence must read true without it.

Guards
======
* ``test_public_docs_do_not_point_at_the_design_log``: no public file contains ``docs/sdd``, a link into ``sdd/``
  (``../sdd/spec.md``, ``../../sdd/``) or the words "design log".  Public means every tracked file outside docs/sdd and
  outside the test code (``tests/``, ``tools/*/tests/``); a README is public wherever it sits.  That covers README*,
  docs/**, examples/**, terraform/** (its tftest fixtures included), dashboards/**, manifests/** and tools/**.
* ``test_docs_set.test_no_tracked_file_references_the_design_log``: no tracked file at all, test code included, names
  docs/sdd or links into ``sdd/``, except docs/sdd itself and ``SDD_MACHINERY``.
* ``test_sdd_machinery_allowlist_is_exact``: every ``SDD_MACHINERY`` entry exists, is test code and still names docs/sdd
  (a stale entry fails).

Documented allowlist (nothing else is exempt)
=============================================
* ``docs/sdd/`` itself (``sanitize_scan.ALLOWLIST_PREFIX``).
* ``SDD_MACHINERY``: the guard machinery, test code only, that names docs/sdd as the path it excludes or protects.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path

import sanitize_scan

ROOT = sanitize_scan.REPO_ROOT
THIS = Path(__file__).resolve().relative_to(ROOT).as_posix()

SDD_REF = re.compile(r"docs/sdd|\bsdd/")  # the directory, or a relative link into it
PUBLIC_TERMS = re.compile(r"docs/sdd|\bsdd/|(?i:design[ -]log)")

SDD_MACHINERY: dict[str, str] = {
    "tests/publish/sanitize_scan.py": "ALLOWLIST_PREFIX, the one path the tree scanner skips",
    "tests/publish/test_sanitization_guards.py": "pins that allowlist and the hits that remain inside it",
    "tests/publish/test_cleanup_layout.py": "skips docs/sdd when it scans the tree",
    "tests/publish/test_sub_cycle_e_guards.py": "documents docs/sdd as exempt from its guards",
    "tests/publish/test_sub_cycle_f_guards.py": "documents docs/sdd as exempt from its guards",
    "tests/scripts/test_109_docs_track_q.py": "keeps the original tracker lines inside docs/sdd in order",
    THIS: "this guard",
}


def is_test_code(rel: str) -> bool:
    """True for the private test trees (``tests/``, ``tools/*/tests/``); a README there is a document, not test code."""
    if Path(rel).name.startswith("README"):
        return False
    return rel.startswith("tests/") or bool(re.match(r"tools/[^/]+/tests/", rel))


def offenders(pattern: re.Pattern[str], include: Callable[[str], bool]) -> dict[str, list[str]]:
    """Map each tracked text file outside docs/sdd that ``include`` keeps to its ``line: match`` hits of ``pattern``."""
    out: dict[str, list[str]] = {}
    for rel in sanitize_scan.tracked_files():
        if rel.startswith(sanitize_scan.ALLOWLIST_PREFIX) or not include(rel):
            continue
        text = sanitize_scan.read_text(rel)
        hits = [f"{text.count(chr(10), 0, m.start()) + 1}: {m.group(0)!r}" for m in pattern.finditer(text or "")]
        if hits:
            out[rel] = hits
    return out


def test_public_docs_do_not_point_at_the_design_log() -> None:
    bad = offenders(PUBLIC_TERMS, lambda rel: not is_test_code(rel))
    shown = "\n".join(f"  {p} ({len(h)}): {h[:3]}" for p, h in sorted(bad.items()))
    assert not bad, f"{len(bad)} public file(s) point at the internal design log:\n{shown}"


def test_sdd_machinery_allowlist_is_exact() -> None:
    missing = sorted(rel for rel in SDD_MACHINERY if not (ROOT / rel).is_file())
    assert not missing, f"SDD_MACHINERY names files that do not exist: {missing}"
    public = sorted(rel for rel in SDD_MACHINERY if not is_test_code(rel))
    assert not public, f"SDD_MACHINERY may hold test code only: {public}"
    stale = sorted(rel for rel in SDD_MACHINERY if not SDD_REF.search((ROOT / rel).read_text(encoding="utf-8")))
    assert not stale, f"SDD_MACHINERY entries that no longer name docs/sdd (drop them): {stale}"
