# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""``support.run_argv``: a child process of the driver suite is an argv list of ``str``, never a shell command."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from typing import Any

from support import run_argv


class RunArgv(unittest.TestCase):
    def test_each_argument_is_handed_over_as_is_and_the_keywords_pass_through(self) -> None:
        p = run_argv([sys.executable, "-c", "import sys; print(sys.argv[1])", "a b; echo c"],
                     capture_output=True, text=True, check=True)
        self.assertEqual(p.stdout, "a b; echo c\n", "one argument: no word splitting, no command separator")

    def test_a_command_string_or_a_non_str_argument_is_refused(self) -> None:
        refused: list[Any] = ["true", b"true", [], ["echo", Path("x")], ("echo", 1)]
        for argv in refused:
            with self.subTest(argv=argv), self.assertRaisesRegex(TypeError, "argv"):
                run_argv(argv)

    def test_the_shell_argument_is_refused_whatever_its_value(self) -> None:
        for shell in (True, False):
            options = {"shell": shell}  # any shell key is refused, True or False
            with self.subTest(options=options), self.assertRaisesRegex(ValueError, "shell"):
                run_argv(["true"], **options)


if __name__ == "__main__":
    unittest.main()
