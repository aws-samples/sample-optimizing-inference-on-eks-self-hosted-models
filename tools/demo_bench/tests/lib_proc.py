# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The one place where the demo_bench tests start a child process (same contract as ``tests/lib_proc.py``).

The tests that run ``python -m demo_bench`` call ``run`` instead of ``subprocess``: the command is a list of
``str`` and no shell is ever started (a command string, a non-``str`` element or a ``shell`` argument is refused),
so the scanners' "subprocess call without a static string" finding is reviewed once here. Every other keyword
argument goes to ``subprocess.run`` unchanged.
"""

from __future__ import annotations

import subprocess
from collections.abc import Sequence
from typing import Any


def run(argv: Sequence[str], **kwargs: Any) -> subprocess.CompletedProcess[Any]:
    """Run ``argv`` to completion without a shell.

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
        raise ValueError("lib_proc never starts a shell: drop the shell argument")
    check = kwargs.pop("check", False)  # subprocess.run's own default, spelled out
    return subprocess.run(list(argv), check=check, **kwargs)  # nosec B603 # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-audit, dangerous-subprocess-use-audit -- argv is a checked list of str, never a shell
