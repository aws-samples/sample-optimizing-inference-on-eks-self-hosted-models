# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Scanner used by the publish guards: finds deployment-specific identifiers in the git-tracked tree.

The patterns are built from fragments so that this file (and the guard tests) never contain a
forbidden literal themselves.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

PLACEHOLDER_ACCOUNT = "1234" + "56789012"  # the documented placeholder (AWS documentation convention)

# Hits that remain on purpose: docs/sdd/* is append-only history and is excluded from the public export.
ALLOWLIST_PREFIX = "docs/sdd/"

_PUBLIC_AMAZON_SUBDOMAINS = {"aws", "docs.aws", "console.aws", "www", "us-east-1.console.aws"}
_PUBLIC_EMAIL_DOMAINS = {"example.com", "example.org", "users.noreply.github.com", "noreply.github.com"}
# The one address of the standard aws-samples CONTRIBUTING.md (the code-of-conduct sentence). Allowed by exact,
# case-sensitive match of the whole address only: a look-alike (prefix, suffix or another host) is still flagged.
ALLOWED_EMAILS = {"opensource-codeofconduct" + "@amazon.com"}

ACCOUNT_RE = re.compile(r"(?<![\w.])(\d{12})(?!\w|\.\w)")
ECR_RE = re.compile(r"(\d{12})\.dkr\.ecr\.")
USERS_PATH_RE = re.compile("/Us" + "ers/|[A-Za-z]:\\\\Us" + "ers\\\\")
# The leading look-behind anchors the local part at the start of its run of local-part characters: the engine no
# longer retries the greedy ``[\w.+-]+`` at every offset inside a long run with no ``@`` (the base64 of
# docs/static/architecture.drawio), which turned the scan quadratic (~120 s). The matches are identical: ``finditer``
# already starts a match only at that run boundary.
EMAIL_RE = re.compile(r"(?<![\w.+-])([\w.+-]+)@([\w-]+(?:\.[\w-]+)*\.(?:com|org|net|io|dev|edu|gov|br))\b", re.IGNORECASE)
INTERNAL_HOST_RE = re.compile(
    r"\b(?:[\w-]+\.)*(?:corp\.amazon\.com|a2z\.com|aws\.dev|amazon\.dev)\b"
    r"|\b((?:[\w-]+\.)+)amazon\.com\b",
    re.IGNORECASE,
)


def _glued(text: str, start: int, end: int) -> bool:
    """True when the match continues an address or host on either side (``x.addr``, ``addr.evil``)."""
    before = text[start - 1] if start else ""
    after = text[end:end + 2]
    return bool(re.match(r"[\w.@+-]", before) or re.match(r"[.@-]\w|\w", after))


def scan_text(text: str) -> list[tuple[str, str]]:
    """Return ``(category, matched text)`` for every non-allowed identifier in ``text``."""
    hits: list[tuple[str, str]] = []
    for m in ACCOUNT_RE.finditer(text):
        if m.group(1) != PLACEHOLDER_ACCOUNT:
            hits.append(("account-id", m.group(1)))
    for m in ECR_RE.finditer(text):
        if m.group(1) != PLACEHOLDER_ACCOUNT:
            hits.append(("ecr-host", m.group(0)))
    for m in USERS_PATH_RE.finditer(text):
        hits.append(("local-path", m.group(0)))
    for m in EMAIL_RE.finditer(text):
        if m.group(0) in ALLOWED_EMAILS and not _glued(text, m.start(), m.end()):
            continue
        if m.group(2).lower() not in _PUBLIC_EMAIL_DOMAINS and m.group(1) != "git":  # git@host is an ssh remote
            hits.append(("email", m.group(0)))
    for m in INTERNAL_HOST_RE.finditer(text):
        sub = (m.group(1) or "").rstrip(".").lower()
        if m.group(1) is not None and sub in _PUBLIC_AMAZON_SUBDOMAINS:
            continue
        hits.append(("internal-host", m.group(0)))
    return hits


def tracked_files() -> list[str]:
    """Tracked paths (``git ls-files``) that exist in the working tree, symlinks included (a staged deletion is skipped)."""
    out = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "ls-files", "-z"], capture_output=True, check=True
    ).stdout.decode()
    return [p for p in out.split("\0") if p and ((REPO_ROOT / p).is_file() or (REPO_ROOT / p).is_symlink())]


def read_text(rel: str) -> str | None:
    """File text, or ``None`` for binary files (NUL byte) and symlinks."""
    path = REPO_ROOT / rel
    if path.is_symlink() or not path.is_file():
        return None
    data = path.read_bytes()
    if b"\0" in data:
        return None
    return data.decode("utf-8", errors="ignore")


def scan_tree(*, include_allowlisted: bool) -> dict[str, list[tuple[str, str]]]:
    """Scan the tracked tree; ``{path: hits}`` for files with at least one hit."""
    result: dict[str, list[tuple[str, str]]] = {}
    for rel in tracked_files():
        if rel.startswith(ALLOWLIST_PREFIX) != include_allowlisted:
            continue
        text = read_text(rel)
        if text is None:
            continue
        hits = scan_text(text)
        if hits:
            result[rel] = hits
    return result
