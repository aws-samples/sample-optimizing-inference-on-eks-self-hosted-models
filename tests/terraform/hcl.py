# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Tiny HCL helpers for the static Terraform tests (block extraction only; no evaluation)."""

from __future__ import annotations

import re
from pathlib import Path


def strip_comments(text: str) -> str:
    """Remove ``#``, ``//`` and ``/* */`` comments (string-unaware; fine for the keywords we search)."""
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    return re.sub(r"(?m)^\s*(#|//).*$|\s+#.*$", "", text)


def block_body(text: str, start: int) -> str:
    """Return the body of the ``{...}`` block whose opening brace is the first ``{`` at/after ``start``."""
    i = text.index("{", start)
    depth = 0
    for j in range(i, len(text)):
        if text[j] == "{":
            depth += 1
        elif text[j] == "}":
            depth -= 1
            if depth == 0:
                return text[i + 1 : j]
    raise ValueError("unbalanced braces")


def blocks(text: str, kind: str, labels: int = 1) -> dict[str, str]:
    """Return ``{label(s) joined by '.': body}`` for every top-level-looking ``kind "a" "b" {`` block."""
    label_re = r"\s+".join([r'"([^"]+)"'] * labels)
    out: dict[str, str] = {}
    for m in re.finditer(rf"(?m)^{kind}\s+{label_re}\s*\{{", text):
        out[".".join(m.groups())] = block_body(text, m.start())
    return out


def read_tf(directory: Path, pattern: str = "*.tf") -> dict[str, str]:
    """Return ``{filename: comment-stripped text}`` for the Terraform files of ``directory`` (not recursive)."""
    return {p.name: strip_comments(p.read_text()) for p in sorted(directory.glob(pattern))}
