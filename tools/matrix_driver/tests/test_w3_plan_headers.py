# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The W3-16K plans carry neutral headers; the driver README marks what this sample does not support.

Contract
========
* Every W3-16K plan keeps every line that is not a ``#`` comment byte for byte (run_id ``q38-w3-*``, hard_stop,
  on_failure, the w2/tokenizer/slo/estimate lines and the steps): the sha256 of those lines is pinned below, so a
  re-pinned golden of the expanded plans can only come from a comment change.
* The header comments name no process document (amendment), no decision timestamp or session clock time, no
  dropped rate (r50) and no "result of record" wording; the provenance is the one factual sentence
  ``PROVENANCE``, exactly once per plan, and "pooled" appears nowhere else.  The first line stays
  ``# <plan name> (W3-16K``.  The W2 plans (plan-e3..e7) are not touched: their goldens pin them.
* ``tools/matrix_driver/README.md``: ``take`` and ``compose`` (grammar table) and the take scripts (file table) are
  marked "not supported in this sample" (the capture Job they need is not shipped); the W3 plans are described
  with ``PROVENANCE``, not as "not results of record".
* ``matrix_driver.py`` cites no ``manifests/demo/capture`` path (that manifest is not shipped in this sample).
"""

from __future__ import annotations

import hashlib
import re
import unittest

from support import PLANS_DIR, TOOL, W3_PLAN_NAMES

PROVENANCE = "n=1 per cell and rate; a different workload from W2, never pooled with it"
NOT_SUPPORTED = "not supported in this sample"

# sha256 of the lines that are not '#' comments (each followed by '\n'), as shipped before the header clean-up.
BODY_SHA256 = {
    "plan-w3-r10-cells": "57b533c8207e0e8ea7c26bf9a66384c62f02d7625c0ec91fd2b77811cbe06e23",
    "plan-w3-r10": "e41ed1396425bd5a599c1cee5f898982cf7ce62e607bdca6d88faf9506610105",
    "plan-w3-r25-cells": "c33832db417de23ac91849ee8151a3c49ef020332d3dc733ee0d991149a578a0",
    "plan-w3-r25": "8c24907deab0ae813ee7163976e6cf54d7de4e27056e2dc6072420ccc9e7a871",
    "plan-w3-r32-cells": "294ef91615c21fa915acf93201b34036d1f3a72c3bf74018f7d790124c9f616b",
    "plan-w3-r32": "94c13c970ace0b1a9eb02ffb8955a8b8ae5e002d09199ba437e044ee5302a01c",
}
COMMENT_DENY = {
    "process document": r"(?i)amendment",
    "decision timestamp": r"\d{8}T\d{6}Z",
    "UTC instant": r"\b20\d\d-\d\d-\d\dT\d\d:\d\d",
    "session clock time": r"\b\d\d:\d\dZ\b",
    "dropped rate": r"\br?50\b(?! ?%)",
    "record wording": r"(?i)of record",
    "wheel or experiment id": r"(?i)\bwheel\b|q38-e9|\bdmodel\b|section \d",
}


def _text(name: str) -> str:
    return (PLANS_DIR / f"{name}.txt").read_text()


def _comments(name: str) -> str:
    return "\n".join(ln for ln in _text(name).splitlines() if ln.startswith("#"))


class W3PlanHeaders(unittest.TestCase):
    def test_every_non_comment_line_is_unchanged(self) -> None:
        self.assertEqual(set(BODY_SHA256), set(W3_PLAN_NAMES))
        for name in W3_PLAN_NAMES:
            body = "".join(ln + "\n" for ln in _text(name).splitlines() if not ln.startswith("#"))
            with self.subTest(plan=name):
                self.assertEqual(hashlib.sha256(body.encode()).hexdigest(), BODY_SHA256[name])

    def test_header_comments_are_neutral(self) -> None:
        for name in W3_PLAN_NAMES:
            comments = _comments(name)
            for what, pat in COMMENT_DENY.items():
                with self.subTest(plan=name, what=what):
                    self.assertIsNone(re.search(pat, comments), f"{name}: {what}: {re.search(pat, comments)}")

    def test_provenance_is_the_one_factual_sentence(self) -> None:
        for name in W3_PLAN_NAMES:
            comments = " ".join(ln.lstrip("# ").strip() for ln in _comments(name).splitlines())
            with self.subTest(plan=name):
                self.assertEqual(comments.count(PROVENANCE), 1, name)
                self.assertNotIn("pooled", comments.replace(PROVENANCE, ""), name)
                self.assertNotIn("result of record", comments.lower(), name)

    def test_first_line_names_the_plan_and_the_workload(self) -> None:
        for name in W3_PLAN_NAMES:
            with self.subTest(plan=name):
                self.assertTrue(_text(name).splitlines()[0].startswith(f"# {name} (W3-16K"))

    def test_comments_carry_no_placeholder(self) -> None:
        # render_driver.expand_plan refuses any '${' that is not a ${KEY}: a comment must not carry one.
        for name in W3_PLAN_NAMES:
            with self.subTest(plan=name):
                self.assertNotIn("${", _comments(name))


class DriverReadme(unittest.TestCase):
    def setUp(self) -> None:
        self.text = (TOOL / "README.md").read_text()
        self.rows = [ln for ln in self.text.splitlines() if ln.startswith("|")]

    def _row(self, start: str) -> str:
        rows = [r for r in self.rows if r.startswith(start)]
        self.assertEqual(len(rows), 1, f"one table row starting with {start!r}")
        return rows[0]

    def test_take_and_compose_are_marked_not_supported_in_the_grammar_table(self) -> None:
        for start in ("| `take ", "| `compose "):
            with self.subTest(row=start):
                self.assertIn(NOT_SUPPORTED, self._row(start))

    def test_the_take_scripts_are_marked_not_supported_in_the_file_table(self) -> None:
        self.assertIn(NOT_SUPPORTED, self._row("| `takes/capture.py`"))

    def test_the_w3_plans_are_described_by_their_provenance(self) -> None:
        self.assertFalse("not results of record" in self.text, "README: 'not results of record'")
        self.assertTrue(PROVENANCE in self.text, f"README lacks {PROVENANCE!r}")

    def test_the_driver_cites_no_capture_manifest_of_the_original_repo(self) -> None:
        cited = "manifests/demo/capture" in (TOOL / "matrix_driver.py").read_text()
        self.assertFalse(cited, "matrix_driver.py cites manifests/demo/capture, which this sample does not ship")


if __name__ == "__main__":
    unittest.main()
