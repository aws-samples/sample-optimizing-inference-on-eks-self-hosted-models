# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""RED tests: every literal tag key and value of the Terraform tree uses only characters KMS and IAM accept.

KMS ``CreateKey`` rejects a tag whose key or value has a character outside letters, digits, spaces and
``_ . : / = + - @`` (``TagException: Invalid tag``); IAM tags follow the same set. The mock providers of
``terraform test`` do not enforce it, so a real apply was the first to fail, on the ``Purpose`` tag of the ECR key in
``terraform/cluster/ecr.tf`` (it held ``/*``). Scope: every ``tags`` / ``*_tags`` map literal of ``terraform/**/*.tf``
and of the ``terraform.tfvars.example`` files, the maps the ``default_tags`` of each provider resolve to included.
Interpolations ``${...}`` are replaced by a safe placeholder: the variables they read carry their own validations.

The user's ``var.tags`` of both stacks is merged into ``local.tags`` (the ``default_tags`` of every provider), so it
reaches the KMS key and the IAM roles too: its validation must refuse the same characters at plan time, with the
character class of the IAM ``Tag`` API (``[\\p{L}\\p{Z}\\p{N}_.:/=+\\-@]*``), and both stacks must use the same
condition. ``terraform/{bootstrap,cluster}/tests`` exercise it with ``terraform test`` (an unsafe key, an unsafe value
and, in bootstrap, a safe map that plans).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from hcl import block_body, strip_comments

REPO = Path(__file__).resolve().parents[2]
TF_ROOT = REPO / "terraform"
SAFE = re.compile(r"[\w .:/=+@-]*")
# The character class of the IAM Tag API (Key and Value), anchored, with the hyphen last instead of escaped.
IAM_TAG_REGEX = r"^[\p{L}\p{Z}\p{N}_.:/=+@-]*$"
STACKS = ("bootstrap", "cluster")
TAG_ATTR = re.compile(r"(?m)^[ \t]*(\w*tags)[ \t]*=[ \t]*(.*)$")
PAIR = re.compile(r'(?m)^[ \t]*(?:"([^"]*)"|([A-Za-z_][\w-]*))[ \t]*=[ \t]*(.+?)[ \t]*$')
STRING = re.compile(r'"((?:[^"\\]|\\.)*)"')
INTERPOLATION = re.compile(r"\$\{[^}]*\}")
NUMBER = re.compile(r"-?\d+(?:\.\d+)?")
DEFAULT_TAGS = re.compile(r"(?m)^[ \t]*default_tags[ \t]*\{")


def scanned_files() -> list[Path]:
    """Return the ``.tf`` files under ``terraform/`` (no ``.terraform`` dirs) and the tfvars examples."""
    tf = [p for p in TF_ROOT.rglob("*.tf") if ".terraform" not in p.parts]
    examples = list(TF_ROOT.rglob("terraform.tfvars.example"))
    return sorted(tf + examples)


def tag_maps(text: str) -> list[tuple[str, str]]:
    """Return ``(attribute, body)`` for every ``tags``/``*_tags`` attribute whose value contains a map literal.

    Args:
        text: Comment-stripped HCL.

    Returns:
        One entry per map literal, e.g. ``tags = {...}`` or ``tags = merge(local.tags, {...})``. Pure references
        such as ``tags = local.tags`` are skipped: their target map is scanned where it is defined.
    """
    out: list[tuple[str, str]] = []
    for m in TAG_ATTR.finditer(text):
        if "{" in m.group(2):
            out.append((m.group(1), block_body(text, m.start(2))))
    return out


def literals(value: str) -> list[str]:
    """Return the literal strings of a tag value expression, interpolations replaced by ``x``.

    Args:
        value: The right-hand side of one ``key = value`` line of a tag map.

    Returns:
        The string literals of the expression, or the number itself for a bare number; empty for a reference.
    """
    strings = STRING.findall(value)
    if strings:
        return [INTERPOLATION.sub("x", s) for s in strings]
    return [value] if NUMBER.fullmatch(value) else []


def tag_pairs(path: Path) -> list[tuple[str, str, list[str]]]:
    """Return ``(attribute, key, value literals)`` for every literal tag pair of ``path``.

    Args:
        path: A Terraform or tfvars example file.

    Returns:
        One entry per ``key = value`` line of every tag map literal of the file.
    """
    out: list[tuple[str, str, list[str]]] = []
    for attr, body in tag_maps(strip_comments(path.read_text())):
        for m in PAIR.finditer(body):
            key = m.group(1) if m.group(1) is not None else m.group(2)
            out.append((attr, key, literals(m.group(3))))
    return out


def unsafe(s: str) -> bool:
    """Return whether ``s`` has a character outside the KMS/IAM tag set."""
    return SAFE.fullmatch(s) is None


def test_scanner_sees_the_tag_maps_of_the_tree() -> None:
    found = {p.relative_to(TF_ROOT).as_posix(): tag_pairs(p) for p in scanned_files()}
    keys = {f: {k for _, k, _ in pairs} for f, pairs in found.items()}
    # the files that set tags today; the scanner must keep seeing them (a regex that matches nothing passes vacuously)
    expected = {
        "bootstrap/main.tf": {"Project", "Environment", "Region", "Stack", "ManagedBy"},
        "bootstrap/buckets.tf": {"Purpose"},
        "cluster/main.tf": {"Project", "Environment", "Region", "Stack", "ManagedBy"},
        "cluster/ecr.tf": {"Name", "Purpose"},
        "cluster/vpc.tf": {"kubernetes.io/role/elb", "kubernetes.io/role/internal-elb", "eks.amazonaws.com/discovery"},
        "cluster/nodepool.tf": {"Project", "nodeclass"},
        "cluster/modules/gpu-pools/main.tf": {"Project", "nodeclass"},
        "cluster/iam-image-mirror.tf": {"Purpose"},
        "cluster/grafana-edge.tf": {"Name"},
    }
    missing = {f: sorted(want - keys.get(f, set())) for f, want in expected.items() if want - keys.get(f, set())}
    assert not missing, f"the tag scanner no longer sees these tag keys: {missing}"
    ecr_purpose = [lit for _, k, lits in found["cluster/ecr.tf"] if k == "Purpose" for lit in lits]
    assert len(ecr_purpose) == 3, f"expected the Purpose of the KMS key and of both ECR repositories: {ecr_purpose}"


def test_every_literal_tag_key_and_value_is_kms_and_iam_safe() -> None:
    bad = []
    for p in scanned_files():
        for attr, key, values in tag_pairs(p):
            where = f"{p.relative_to(REPO)} {attr}[{key!r}]"
            if unsafe(key):
                bad.append(f"{where}: key")
            bad.extend(f"{where}: value {v!r}" for v in values if unsafe(v))
    assert not bad, (
        "tag keys and values may only use letters, digits, spaces and _ . : / = + - @ "
        "(KMS CreateKey fails with TagException: Invalid tag otherwise): " + "; ".join(bad)
    )


def test_default_tags_resolve_to_a_scanned_literal_map() -> None:
    """Each provider ``default_tags`` reads ``local.tags``, whose literal map in the same stack is safe."""
    seen = 0
    for p in sorted(p for p in TF_ROOT.rglob("*.tf") if ".terraform" not in p.parts):
        text = strip_comments(p.read_text())
        for m in DEFAULT_TAGS.finditer(text):
            seen += 1
            ref = re.search(r"\btags\s*=\s*(\S+)", block_body(text, m.start()))
            assert ref and ref.group(1) == "local.tags", f"{p.relative_to(REPO)}: default_tags must read local.tags"
            main = p.parent / "main.tf"
            local_tags = {k: vs for attr, k, vs in tag_pairs(main) if attr == "tags"}
            missing = {"Project", "Environment", "Region", "Stack", "ManagedBy"} - local_tags.keys()
            assert not missing, f"{main.relative_to(REPO)}: local.tags lost its literal keys {sorted(missing)}"
            bad = [f"{k}={v!r}" for k, vs in local_tags.items() for v in vs if unsafe(k) or unsafe(v)]
            assert not bad, f"{main.relative_to(REPO)} local.tags (default_tags): {bad}"
    assert seen >= 3, "expected default_tags in the bootstrap and cluster providers and the us_east_1 alias"


def tags_validation(stack: str) -> tuple[str, str]:
    """Return ``(condition, error_message)`` of the validation of ``variable "tags"`` in ``<stack>/variables.tf``.

    Args:
        stack: ``bootstrap`` or ``cluster``.

    Returns:
        The condition expression and the error message, as written (an empty string for a missing part).
    """
    text = strip_comments((TF_ROOT / stack / "variables.tf").read_text())
    var = re.search(r'(?m)^variable\s+"tags"\s*\{', text)
    assert var, f"terraform/{stack}/variables.tf: variable tags"
    body = block_body(text, var.start())
    val = re.search(r"(?m)^\s*validation\s*\{", body)
    if val is None:
        return "", ""
    rule = block_body(body, val.start())
    cond = re.search(r"(?m)^\s*condition\s*=\s*(.+?)\s*$", rule)
    msg = re.search(r'(?m)^\s*error_message\s*=\s*"((?:[^"\\]|\\.)*)"', rule)
    return (cond.group(1) if cond else ""), (msg.group(1) if msg else "")


def hcl_unescape(s: str) -> str:
    """Return the value of the body of an HCL string literal without interpolations (only ``\\\\`` and ``\\"``)."""
    return s.replace("\\\\", "\x00").replace('\\"', '"').replace("\x00", "\\")


@pytest.mark.parametrize("stack", STACKS)
def test_the_tags_variable_refuses_what_kms_and_iam_reject(stack: str) -> None:
    cond, msg = tags_validation(stack)
    assert cond, f"terraform/{stack}/variables.tf: variable tags needs a validation (KMS CreateKey refused '*' at apply)"
    assert re.search(r"\bfor\s+(\w+)\s*,\s*(\w+)\s+in\s+var\.tags\b", cond), f"check every key and value of var.tags: {cond}"
    regexes = [hcl_unescape(s) for s in STRING.findall(cond)]
    assert regexes and set(regexes) == {IAM_TAG_REGEX}, f"use the IAM Tag character class {IAM_TAG_REGEX}: {regexes}"
    assert "_ . : / = + - @" in msg and "letters" in msg and "spaces" in msg, f"name the allowed characters: {msg!r}"


def test_both_stacks_validate_the_tags_with_the_same_condition() -> None:
    conds = {stack: tags_validation(stack)[0] for stack in STACKS}
    assert all(conds.values()) and len(set(conds.values())) == 1, f"one rule for both stacks (the bootstrap test plans a safe map): {conds}"
