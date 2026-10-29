# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""``tests/lib_proc.py``: a child process of the suites is an argv list of ``str``, never a shell command."""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import lib_proc
import pytest

ECHO_ARGV = "import sys; print(sys.argv[1])"
STARTERS: list[Callable[..., Any]] = [lib_proc.run, lib_proc.start]


def test_run_hands_each_argument_over_as_is_and_passes_the_keywords_through() -> None:
    proc = lib_proc.run([sys.executable, "-c", ECHO_ARGV, "a b; echo c"], capture_output=True, text=True, check=True)
    assert proc.stdout == "a b; echo c\n", "one argument: no word splitting, no command separator"


def test_start_returns_the_running_process() -> None:
    proc = lib_proc.start([sys.executable, "-c", ECHO_ARGV, "x"], stdout=subprocess.PIPE, text=True)
    out, _ = proc.communicate(timeout=60)
    assert (proc.returncode, out) == (0, "x\n")


@pytest.mark.parametrize("fn", STARTERS)
@pytest.mark.parametrize("argv", ["true", b"true", [], ["echo", Path("x")], ("echo", 1)], ids=repr)
def test_a_command_string_or_a_non_str_argument_is_refused(fn: Callable[..., Any], argv: Any) -> None:
    with pytest.raises(TypeError, match="argv"):
        fn(argv)


@pytest.mark.parametrize("fn", STARTERS)
@pytest.mark.parametrize("shell", [True, False])
def test_the_shell_argument_is_refused_whatever_its_value(fn: Callable[..., Any], shell: bool) -> None:
    options = {"shell": shell}  # any shell key is refused, True or False
    with pytest.raises(ValueError, match="shell"):
        fn(["true"], **options)
