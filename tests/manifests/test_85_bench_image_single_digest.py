# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Bench image rebuild: one bench image digest across every manifest that pins it.

``manifests/bench/deployment.yaml`` (and any future manifest; the shell matrix-driver Job left the tree)
must bind the SAME ``dynamo-sample/bench@sha256:<64 hex>``, and it must not be a digest whose ECR scan carries an
unaccepted CRITICAL.  After a rebuild the developer adds the old digest to ``REJECTED_DIGESTS`` here only if it is
newly found vulnerable; the single-digest rule needs no edit.
"""

from __future__ import annotations

import re

from lib_manifests import MANIFESTS

BENCH_REF = re.compile(r"dynamo-sample/bench@sha256:([0-9a-f]{64})")
# Digests with an unaccepted CRITICAL in the ECR scan.
REJECTED_DIGESTS = {
    # certifi 2026.10.18 (GHSA-6q48-6c7w-9gw2) + pillow 12.3.0 (CVE-2026-112403).
    "ea15ab745b7b719d3720436b27cf2a13de96e2075fb028335a40f379b73fc4a2",
}


def _pins() -> dict[str, set[str]]:
    pins: dict[str, set[str]] = {}
    for f in sorted(MANIFESTS.rglob("*.yaml")):
        for line in f.read_text().splitlines():
            if line.lstrip().startswith("#"):
                continue
            for m in BENCH_REF.finditer(line):
                pins.setdefault(m.group(1), set()).add(str(f.relative_to(MANIFESTS)))
    return pins


def test_bench_image_is_pinned_by_digest_in_at_least_the_two_known_manifests() -> None:
    files = {f for fs in _pins().values() for f in fs}
    assert {"bench/deployment.yaml"} <= files, files


def test_every_manifest_pins_the_same_bench_digest() -> None:
    pins = _pins()
    assert len(pins) == 1, f"bench image bound to {len(pins)} different digests: {pins}"


def test_bench_digest_is_not_a_rejected_one() -> None:
    bad = REJECTED_DIGESTS & set(_pins())
    assert not bad, f"bench digest with unaccepted CRITICALs still pinned: {bad}"
