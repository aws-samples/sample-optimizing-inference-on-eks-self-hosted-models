# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The published surface stays byte-identical (digests in ``lib_frozen``).

``tools/stage_model`` and the other files of ``lib_frozen`` match their byte digests; the eight
``examples/<folder>/manifest.yaml`` match the digest of their parsed content (comments may change, values may not).

Nothing here weakens an expectation: a missing file is a failure, never a skip.
"""

from __future__ import annotations

import hashlib

import lib_frozen as F
import lib_manifests as L
import pytest


# ----------------------------------------------------------------------------- the frozen surface
@pytest.mark.parametrize("rel,digest", F.FROZEN_TREES.items())
def test_published_directories_are_byte_identical(rel: str, digest: str) -> None:
    assert (L.REPO_ROOT / rel).is_dir(), rel
    assert F.tree_digest(rel) == digest, f"{rel} changed"


@pytest.mark.parametrize("folder,digest", F.FROZEN_EXAMPLES.items())
def test_the_published_example_manifests_parse_to_the_frozen_content(folder: str, digest: str) -> None:
    p = L.EXAMPLES / folder / "manifest.yaml"
    assert p.is_file(), f"examples/{folder}/manifest.yaml missing"
    assert F.parsed_digest(p) == digest, f"examples/{folder}/manifest.yaml changed"


@pytest.mark.parametrize("rel,digest", F.FROZEN_FILES.items())
def test_published_files_are_byte_identical(rel: str, digest: str) -> None:
    p = L.REPO_ROOT / rel
    assert p.is_file(), rel
    assert hashlib.sha256(p.read_bytes()).hexdigest() == digest, f"{rel} changed"
