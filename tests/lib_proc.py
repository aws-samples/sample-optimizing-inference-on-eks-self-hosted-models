# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The one place where the offline suites start a child process.

The test modules that run a script, ``git``, ``terraform`` or another tool call ``run`` (wait for the process) or
``start`` (the caller manages it) instead of ``subprocess``. Both take the command as a list of ``str`` and never
start a shell: a command string, a non-``str`` element or a ``shell`` argument is refused. The programs come from
the repository, ``PATH`` or the documented inputs of the suites (``TERRAFORM_BIN``, ``SCRIPTS_DIR``), so the
scanners' "subprocess call without a static string" finding is reviewed once here, not at every call site.
Every other keyword argument goes to ``subprocess.run`` / ``subprocess.Popen`` unchanged.
"""

from __future__ import annotations

import subprocess
from collections.abc import Sequence
from typing import Any


def _checked(argv: Sequence[str], kwargs: dict[str, Any]) -> list[str]:
    """Return ``argv`` as a new list once it is a non-empty sequence of ``str`` and no shell is asked for.

    Args:
        argv: Program and arguments.
        kwargs: The keyword arguments for ``subprocess``.

    Returns:
        A copy of ``argv``.

    Raises:
        TypeError: ``argv`` is a single string, is empty or holds a non-``str`` element.
        ValueError: ``kwargs`` holds ``shell``.
    """
    if isinstance(argv, str | bytes) or not argv or not all(isinstance(a, str) for a in argv):
        raise TypeError(f"argv must be a non-empty sequence of str, got {argv!r}")
    if "shell" in kwargs:
        raise ValueError("lib_proc never starts a shell: drop the shell argument")
    return list(argv)


def run(argv: Sequence[str], **kwargs: Any) -> subprocess.CompletedProcess[Any]:
    """Run ``argv`` to completion without a shell.

    Args:
        argv: Program and arguments, each a ``str``.
        **kwargs: Keyword arguments of ``subprocess.run`` (``shell`` excepted).

    Returns:
        The completed process.

    Raises:
        TypeError: ``argv`` is not a non-empty sequence of ``str``.
        ValueError: A ``shell`` argument was given.
    """
    args = _checked(argv, kwargs)
    check = kwargs.pop("check", False)  # subprocess.run's own default, spelled out
    return subprocess.run(args, check=check, **kwargs)  # nosec B603 # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-audit, dangerous-subprocess-use-audit -- argv is a checked list of str, never a shell


def start(argv: Sequence[str], **kwargs: Any) -> subprocess.Popen[Any]:
    """Start ``argv`` without a shell; the caller waits for it, signals it or reads its pipes.

    Args:
        argv: Program and arguments, each a ``str``.
        **kwargs: Keyword arguments of ``subprocess.Popen`` (``shell`` excepted).

    Returns:
        The running process.

    Raises:
        TypeError: ``argv`` is not a non-empty sequence of ``str``.
        ValueError: A ``shell`` argument was given.
    """
    args = _checked(argv, kwargs)
    return subprocess.Popen(args, **kwargs)  # nosec B603 # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-audit, dangerous-subprocess-use-audit -- argv is a checked list of str, never a shell
