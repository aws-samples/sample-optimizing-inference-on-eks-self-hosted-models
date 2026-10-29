# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Paths for the ported matrix driver suite (offline: fake cluster + fake S3, no AWS, no kubectl, no network).

Run from the repo root::

    uv run --with-requirements tests/requirements-test.txt pytest tools/matrix_driver/tests -q -p no:cacheprovider

The driver modules are flat (``import matrix_driver``), exactly as the in-cluster ConfigMap mounts them, and
``demo_bench`` (the 0.5 % rule, the output collapse, slo-goodput) is imported from the repo's ``tools/demo_bench/src``.
"""

from __future__ import annotations

import sys
from pathlib import Path

TESTS = Path(__file__).resolve().parent
TOOL = TESTS.parent
REPO = TOOL.parent.parent

for _p in (REPO / "tools" / "demo_bench" / "src", REPO / "tests", TESTS, TOOL):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))


# A fresh clone has only manifests/config/platform.env.example (the generated platform.env is gitignored): the plan
# and driver renders and their goldens are pinned to the example's placeholder values, so materialize it for the session.
import platform_env  # tests/platform_env.py


def pytest_configure(config) -> None:  # type: ignore[no-untyped-def]
    if not hasattr(config, "workerinput"):
        platform_env.materialize()


def pytest_unconfigure(config) -> None:  # type: ignore[no-untyped-def]
    if not hasattr(config, "workerinput"):
        platform_env.cleanup()
