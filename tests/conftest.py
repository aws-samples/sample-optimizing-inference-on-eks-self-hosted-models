# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Shared pytest configuration for the manifest and script contract tests.

The helper modules live next to the tests that use them
(``tests/manifests/lib_manifests.py`` and ``tests/scripts/lib_scripts.py``);
pytest puts each test directory on ``sys.path`` (rootdir/prepend import mode),
so they are importable by plain module name.
"""

from __future__ import annotations

from pathlib import Path

import platform_env
import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="session")
def repo_root() -> Path:
    """Return the sample repository root (parent of ``tests/``)."""
    return REPO_ROOT


# --------------------------------------------------------------------------- platform.env for a clean clone
# See platform_env.py: a fresh clone has only platform.env.example; the suite materializes platform.env from it for the
# session (only when absent) and removes it again.


def _is_xdist_worker(config: pytest.Config) -> bool:
    return hasattr(config, "workerinput")


def pytest_configure(config: pytest.Config) -> None:
    if not _is_xdist_worker(config):
        platform_env.materialize()


def pytest_unconfigure(config: pytest.Config) -> None:
    if not _is_xdist_worker(config):
        platform_env.cleanup()


def pytest_report_header(config: pytest.Config) -> list[str]:
    """Warn when a local generated platform.env differs from the example (renders/goldens pin the example)."""
    return [
        f"WARNING: {p.relative_to(REPO_ROOT)} differs from platform.env.example; render goldens are pinned to the "
        "example. Run the suite in a clean clone (or move the generated file aside) for a deterministic result."
        for p in platform_env.differing_local_files()
    ]
