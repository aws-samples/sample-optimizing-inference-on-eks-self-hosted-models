# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Fixtures for the static Terraform tests (the helpers in hcl.py are importable by plain name)."""

from __future__ import annotations

from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="session")
def tf_root() -> Path:
    """Return ``<repo>/terraform``."""
    return REPO / "terraform"
