# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""``tests/lib_proc.py``: a child process of the tests is an argv list of ``str``, never a shell."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

import lib_proc

ECHO_ARGV = "import sys; print(sys.argv[1])"


def test_each_argument_is_handed_over_as_is_and_the_keywords_pass_through() -> None:
    proc = lib_proc.run([sys.executable, "-c", ECHO_ARGV, "a b; echo c"], capture_output=True,
                        text=True, check=True)
    assert proc.stdout == "a b; echo c\n", "one argument: no word splitting, no command separator"


@pytest.mark.parametrize("argv", ["true", b"true", [], ["echo", Path("x")], ("echo", 1)], ids=repr)
def test_a_command_string_or_a_non_str_argument_is_refused(argv: Any) -> None:
    with pytest.raises(TypeError, match="argv"):
        lib_proc.run(argv)


@pytest.mark.parametrize("shell", [True, False])
def test_the_shell_argument_is_refused_whatever_its_value(shell: bool) -> None:
    options = {"shell": shell}  # any shell key is refused, True or False
    with pytest.raises(ValueError, match="shell"):
        lib_proc.run(["true"], **options)
