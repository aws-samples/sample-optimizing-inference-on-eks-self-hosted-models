# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Publish guards: no lab identifier is committed (M1 of the publish readiness list).

Scans every git-tracked file (``git ls-files``) for:

* a 12-digit AWS account id other than the documented placeholder 123456789012 (bare, in an ARN, in an S3 bucket name),
* an ECR host (``<12 digits>.dkr.ecr.``) of a real account,
* a personal e-mail address,
* a local home-directory path of the macOS kind,
* an internal host name (the corporate intranet domains of the company, listed in ``sanitize_scan.INTERNAL_HOST_RE``;
  public ``aws.amazon.com`` / ``docs.aws.amazon.com`` references are fine).

The only allowlisted files are ``docs/sdd/*`` (append-only logs, excluded from the public export); the hits that remain
there are pinned exactly below, the account id by count and never by value.  The scanner lives in ``sanitize_scan.py``.

The scanner self-tests use ``FAKE_ACCOUNT``, the synthetic 1111-2222-3333 id of the AWS documentation (dashed here, or
the line scan would flag it), never a real one: the scanner matches the shape of an id, so it needs no real value. No
tracked Python may build an account id from fragments (``"a" + "b"`` dodges the line scan) except that synthetic one.
"""

from __future__ import annotations

import ast
import re
from collections import Counter
from pathlib import Path

import pytest
import sanitize_scan as S

FAKE_ACCOUNT = "1111" + "22223333"  # synthetic id of the AWS documentation; the scanner must catch it (no real id here)
USERS = "/Us" + "ers/"
ORIGINAL_PROJECT = "kubecon" + "-dynamo"  # the project name of the original deployment
HARMONY_HOST = "console.harmony." + "a2" + "z.com"  # the internal self-certification wizard host
SDD_ACCOUNT = "<the lab account id>"  # stands for the one account id the design log names; never spelled in this file

CATEGORIES = ("account-id", "ecr-host", "email", "local-path", "internal-host")

# docs/sdd/{plan,spec}.md and docs/sdd/publish/*.md are append-only history and byte-identical on purpose: these are
# ALL the hits that may remain (docs/sdd/publish/Task.md has none, so it has no entry). The account-id hits are pinned
# by count with the value masked as SDD_ACCOUNT, and they must all be one and the same id.
SDD_ALLOWED_HITS = {
    "docs/sdd/plan.md": Counter({("account-id", SDD_ACCOUNT): 2}),
    "docs/sdd/spec.md": Counter({("account-id", SDD_ACCOUNT): 4}),
    # the publication spec names the internal wizard URL (process step, links) and quotes the PG-03 gate grep regex
    "docs/sdd/publish/spec.md": Counter({("internal-host", HARMONY_HOST): 2, ("local-path", USERS): 1}),
}


# --------------------------------------------------------------------------- the scanner itself (so a green guard means something)
@pytest.mark.parametrize(
    "text,category",
    [
        (f"AWS_ACCOUNT_ID={FAKE_ACCOUNT}", "account-id"),
        (f"arn:aws:eks:ap-south-1:{FAKE_ACCOUNT}:cluster/x", "account-id"),
        (f"{ORIGINAL_PROJECT}-models-{FAKE_ACCOUNT}-aps1", "account-id"),
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
        f"{S.PLACEHOLDER_ACCOUNT}.dkr.ecr.ap-south-1.amazonaws.com/{ORIGINAL_PROJECT}/bench",
        f"arn:aws:eks:ap-south-1:{S.PLACEHOLDER_ACCOUNT}:cluster/{ORIGINAL_PROJECT}-aps1",
        f"{ORIGINAL_PROJECT}-models-{S.PLACEHOLDER_ACCOUNT}-aps1",
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
    return S.scan_tree(include_allowlisted=False)


@pytest.mark.parametrize("category", CATEGORIES)
def test_tracked_tree_has_no_lab_identifier(tree_hits: dict[str, list[tuple[str, str]]], category: str) -> None:
    offenders = {
        path: sorted({m for c, m in hits if c == category})
        for path, hits in tree_hits.items()
        if any(c == category for c, _ in hits)
    }
    shown = "\n".join(f"  {p}: {m[:3]}" for p, m in sorted(offenders.items())[:60])
    assert not offenders, f"{len(offenders)} tracked file(s) carry a {category} (allowlist: docs/sdd only):\n{shown}"


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
    for rel in S.tracked_files():
        text = S.read_text(rel) if rel.endswith(".py") and not rel.startswith(S.ALLOWLIST_PREFIX) else None
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


def test_docs_sdd_keeps_exactly_the_documented_hits() -> None:
    raw = S.scan_tree(include_allowlisted=True)
    distinct = len({m for hits in raw.values() for c, m in hits if c == "account-id"})
    assert distinct == 1, "docs/sdd names exactly one account id (the lab one)"  # a count: the output never shows it
    found = {path: Counter((c, SDD_ACCOUNT if c == "account-id" else m) for c, m in hits) for path, hits in raw.items()}
    assert found == SDD_ALLOWED_HITS


def test_allowlist_is_only_docs_sdd() -> None:
    assert S.ALLOWLIST_PREFIX == "docs/sdd/"
    assert all(p.startswith("docs/sdd/") for p in SDD_ALLOWED_HITS)


# --------------------------------------------------------------------------- sanitizing must keep the committed artifacts meaningful
def _tracked(prefix_glob: str) -> list[str]:
    import fnmatch

    return [p for p in S.tracked_files() if fnmatch.fnmatch(p, prefix_glob)]


@pytest.mark.parametrize(
    "glob",
    ["examples/*/rendered.yaml", "manifests/track-q-qwen38/rendered/S*.yaml"],
)
def test_committed_renders_carry_the_pinned_runtime_image(glob: str) -> None:
    """The renders keep the pinned public runtime image of versions.env (no account-specific registry): not stripped of it."""
    pinned = next(ln.split("=", 1)[1] for ln in (S.REPO_ROOT / "versions.env").read_text().splitlines() if ln.startswith("RUNTIME_IMAGE="))
    files = _tracked(glob)
    assert len(files) >= 5, files
    missing = [p for p in files if f"image: {pinned}" not in (S.read_text(p) or "")]
    assert not missing, f"{missing} lack the pinned runtime image {pinned}"


def test_committed_diffs_are_sanitized_but_not_emptied() -> None:
    files = _tracked("examples/*/diff-vs-baseline.diff")
    assert len(files) == 7, files  # one per variant folder of examples/ (all eight cells, baseline has none)
    assert all(S.read_text(p) for p in files)

