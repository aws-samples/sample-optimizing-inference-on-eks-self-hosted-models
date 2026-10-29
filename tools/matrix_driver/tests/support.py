# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Shared helpers of the ported driver suite."""

from __future__ import annotations

import subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import Any

TESTS = Path(__file__).resolve().parent
TOOL = TESTS.parent
REPO = TOOL.parent.parent
PLANS_DIR = REPO / "plans" / "w3-16k"  # the W3-16K plans (the default plans dir of render_driver)
W2_PLANS_DIR = REPO / "plans" / "w2-long-context"  # plan-e3..e7, frozen: render_driver --plans-dir
PLATFORM_ENV = REPO / "manifests" / "config" / "platform.env"
DRIVER_TEMPLATES = REPO / "manifests" / "track-q-qwen38" / "driver"
RENDERED_DGDS = REPO / "manifests" / "track-q-qwen38" / "rendered"
FIXTURES = TESTS / "fixtures"

W2_PLAN_NAMES = ("plan-e3", "plan-e4", "plan-e5", "plan-e6", "plan-e7")
W3_PLAN_NAMES = (
    "plan-w3-r10", "plan-w3-r10-cells", "plan-w3-r25", "plan-w3-r25-cells",
    "plan-w3-r32", "plan-w3-r32-cells",
)
PLAN_NAMES = (*W2_PLAN_NAMES, *W3_PLAN_NAMES)  # plan-w3-r50 was dropped from the public sample


def plans_dir_of(name: str) -> Path:
    """The directory that ships ``name``: plans/w2-long-context for plan-e3..e7, plans/w3-16k for the W3-16K plans."""
    return W2_PLANS_DIR if name in W2_PLAN_NAMES else PLANS_DIR


def plan_path(name: str) -> Path:
    return plans_dir_of(name) / f"{name}.txt"


def shipped_plan_text(name: str) -> str:
    """The shipped plan ``<plans dir>/<name>.txt`` (see ``plans_dir_of``) with its ``${KEY}`` placeholders expanded from platform.env.

    The expansion is byte-exact with the plan of the recorded run (the e3 plan sha256 pinned by e4..e7 depends on it).
    """
    import render_driver as rd

    env = rd.load_platform_env(PLATFORM_ENV)
    return rd.expand_plan(plan_path(name).read_text(), env)


def run_argv(argv: Sequence[str], **kwargs: Any) -> subprocess.CompletedProcess[Any]:
    """Run ``argv`` to completion without a shell: the one place the suite starts a child process.

    Same contract as ``tests/lib_proc.py``: the command is a list of ``str`` (a command string, a non-``str`` element or
    a ``shell`` argument is refused), so the scanners' "subprocess call without a static string" finding is reviewed
    here once. Every other keyword argument goes to ``subprocess.run`` unchanged.

    Args:
        argv: Program and arguments, each a ``str``.
        **kwargs: Keyword arguments of ``subprocess.run`` (``shell`` excepted).

    Returns:
        The completed process.

    Raises:
        TypeError: ``argv`` is a single string, is empty or holds a non-``str`` element.
        ValueError: ``kwargs`` holds ``shell``.
    """
    if isinstance(argv, str | bytes) or not argv or not all(isinstance(a, str) for a in argv):
        raise TypeError(f"argv must be a non-empty sequence of str, got {argv!r}")
    if "shell" in kwargs:
        raise ValueError("run_argv never starts a shell: drop the shell argument")
    check = kwargs.pop("check", False)  # subprocess.run's own default, spelled out
    return subprocess.run(list(argv), check=check, **kwargs)  # nosec B603 # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-audit, dangerous-subprocess-use-audit -- argv is a checked list of str, never a shell
