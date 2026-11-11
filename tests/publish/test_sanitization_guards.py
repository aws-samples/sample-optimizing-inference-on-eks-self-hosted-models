# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Publish guards: no lab identifier is committed.

Scans every git-tracked file the public tree ships (``sanitize_scan.public_files``) for:

* a 12-digit AWS account id other than the documented placeholder 123456789012 (bare, in an ARN, in an S3 bucket name),
* an ECR host (``<12 digits>.dkr.ecr.``) of a real account,
* a personal e-mail address,
* a local home-directory path of the macOS kind,
* an internal host name (the corporate intranet domains of the company, listed in ``sanitize_scan.INTERNAL_HOST_RE``;
  public ``aws.amazon.com`` / ``docs.aws.amazon.com`` references are fine).

No file is allowlisted.  The scanner lives in ``sanitize_scan.py``.

The scanner self-tests use ``FAKE_ACCOUNT``, the synthetic 1111-2222-3333 id of the AWS documentation (dashed here, or
the line scan would flag it), never a real one: the scanner matches the shape of an id, so it needs no real value. No
tracked Python may build an account id from fragments (``"a" + "b"`` dodges the line scan) except that synthetic one.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest
import sanitize_scan as S

FAKE_ACCOUNT = "1111" + "22223333"  # synthetic id of the AWS documentation; the scanner must catch it (no real id here)
USERS = "/Us" + "ers/"
PROJECT = "example-project"  # any project name: the scanner matches the account id, not the name around it

CATEGORIES = ("account-id", "ecr-host", "email", "local-path", "internal-host")


# --------------------------------------------------------------------------- the scanner itself (so a green guard means something)
@pytest.mark.parametrize(
    "text,category",
    [
        (f"AWS_ACCOUNT_ID={FAKE_ACCOUNT}", "account-id"),
        (f"arn:aws:eks:us-west-2:{FAKE_ACCOUNT}:cluster/x", "account-id"),
        (f"{PROJECT}-models-{FAKE_ACCOUNT}-usw2", "account-id"),
        (f"account {FAKE_ACCOUNT}.", "account-id"),
        (f"image: {FAKE_ACCOUNT}.dkr.ecr.us-west-2.amazonaws.com/p/x@sha256:ab", "ecr-host"),
        ("authors = [{ email = \"someone@" + "amazon.com\" }]", "email"),
        ("contact me at jane.doe@" + "gmail.com", "email"),
        # An e-mail glued inside a base64-like run is still found after the look-behind made the scan linear.
        (("QUFB" * 200) + "/" + "deploy-admin" + "@" + "lab-host" + ".io" + "/" + ("Qg==" * 200), "email"),
        (f"DYNAMO_REPO={USERS}someone/dynamo", "local-path"),
        ("see https://w." + "corp." + "amazon.com/bin/view/X", "internal-host"),
        ("see https://wiki." + "a2" + "z.com/page", "internal-host"),
        ("see https://x.y." + "aws" + ".dev/z", "internal-host"),
        ("see https://team." + "amazon.com/x", "internal-host"),
    ],
)
def test_scanner_flags_lab_identifiers(text: str, category: str) -> None:
    assert category in {c for c, _ in S.scan_text(text)}, text


@pytest.mark.parametrize(
    "text",
    [
        f"AWS_ACCOUNT_ID={S.PLACEHOLDER_ACCOUNT}",
        f"{S.PLACEHOLDER_ACCOUNT}.dkr.ecr.us-west-2.amazonaws.com/{PROJECT}/bench",
        f"arn:aws:eks:us-west-2:{S.PLACEHOLDER_ACCOUNT}:cluster/{PROJECT}-usw2",
        f"{PROJECT}-models-{S.PLACEHOLDER_ACCOUNT}-usw2",
        "sha256 5a/a5/723340838581bbed0590429662750dc70d67ba6719",  # digits inside a hex hash
        "version 20261022234914 build",  # 14 digits
        "Copyright Amazon.com, Inc. or its affiliates.",
        "https://docs.aws.amazon.com/eks/latest/userguide/x.html",
        "https://aws.amazon.com/ec2/",
        "git@github.com:aws-samples/x.git",
        "user@example.com",
        "<ACCOUNT_ID>.dkr.ecr.<REGION>.amazonaws.com",
        "page@fake.webm",
    ],
)
def test_scanner_accepts_placeholders_and_public_references(text: str) -> None:
    assert S.scan_text(text) == [], text


# --------------------------------------------------------------------------- the tracked tree
@pytest.fixture(scope="module")
def tree_hits() -> dict[str, list[tuple[str, str]]]:
    return S.scan_tree()


@pytest.mark.parametrize("category", CATEGORIES)
def test_tracked_tree_has_no_lab_identifier(tree_hits: dict[str, list[tuple[str, str]]], category: str) -> None:
    offenders = {
        path: sorted({m for c, m in hits if c == category})
        for path, hits in tree_hits.items()
        if any(c == category for c, _ in hits)
    }
    shown = "\n".join(f"  {p}: {m[:3]}" for p, m in sorted(offenders.items())[:60])
    assert not offenders, f"{len(offenders)} tracked file(s) carry a {category}:\n{shown}"


def _strings(source: str) -> set[str]:
    """Every str the module spells, each ``"a" + "b"`` chain joined: a literal split to dodge the scan is seen whole."""

    def fold(node: ast.AST) -> str | None:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            left, right = fold(node.left), fold(node.right)
            return None if left is None or right is None else left + right
        return None

    return {s for node in ast.walk(ast.parse(source)) if (s := fold(node)) is not None}


def test_no_tracked_python_builds_an_account_id_from_fragments() -> None:
    """Only the placeholder and the synthetic FAKE_ACCOUNT may be built from fragments (the scanner self-tests need one)."""
    offenders: dict[str, int] = {}
    for rel in S.public_files():
        text = S.read_text(rel) if rel.endswith(".py") else None
        if text is None:
            continue
        ids = {m for s in _strings(text) for c, m in S.scan_text(s) if c in ("account-id", "ecr-host")}
        ids -= {FAKE_ACCOUNT, f"{FAKE_ACCOUNT}.dkr.ecr."}
        if ids:
            offenders[rel] = len(ids)  # a count, never the value: the failure output must not leak it either
    assert not offenders, f"tracked Python builds an account id but {FAKE_ACCOUNT} from fragments: {offenders}"


def test_scanner_catches_account_ids_by_shape_not_by_value() -> None:
    """The scanner spells no account id but the placeholder, yet catches the synthetic one: no real id is needed."""
    spelled = {d for s in _strings(Path(S.__file__).read_text()) for d in re.findall(r"\d{12}", s)}
    assert spelled <= {S.PLACEHOLDER_ACCOUNT}
    assert S.scan_text(f"account {FAKE_ACCOUNT}") == [("account-id", FAKE_ACCOUNT)]


# --------------------------------------------------------------------------- sanitizing must keep the committed artifacts meaningful
def _tracked(prefix_glob: str) -> list[str]:
    import fnmatch

    return [p for p in S.tracked_files() if fnmatch.fnmatch(p, prefix_glob)]


@pytest.mark.parametrize(
    "glob",
    ["examples/*/manifest.yaml"],
)
def test_committed_renders_carry_the_pinned_runtime_image(glob: str) -> None:
    """The renders keep the pinned public runtime image of versions.env (no account-specific registry): not stripped of it."""
    pinned = next(ln.split("=", 1)[1] for ln in (S.REPO_ROOT / "versions.env").read_text().splitlines() if ln.startswith("RUNTIME_IMAGE="))
    files = _tracked(glob)
    assert len(files) >= 5, files
    missing = [p for p in files if f"image: {pinned}" not in (S.read_text(p) or "")]
    assert not missing, f"{missing} lack the pinned runtime image {pinned}"
