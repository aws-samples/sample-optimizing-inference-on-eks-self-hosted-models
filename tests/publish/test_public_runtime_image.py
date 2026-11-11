# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The sample runs on the public NGC runtime image.

The workers use ``nvcr.io/nvidia/ai-dynamo/sglang-runtime:1.6.1-efa`` pinned by digest (Dynamo 1.6.1, SGLang 0.5.21,
NIXL 1.5.0, EFA installer 1.50.0).  No Dockerfile and no image build ship in the sample.

Contract pinned here
====================
1. ``test_versions_env_*``                  root ``versions.env`` is the single source of the pins.
2. ``test_*_matches_versions_env``          the same values everywhere they appear (docs, tfvars example, platform env
                                            example, renders, goldens); any digest-pinned runtime image is the pinned one.
3. ``test_*_single_apply*``                 one cluster apply with ``runtime_image`` set.

The wording of the optimization guides and of the other docs is NOT pinned here.

Fully offline: git ls-files plus text reads, no AWS, no cluster, no network.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import sanitize_scan as S

ROOT = S.REPO_ROOT
THIS = Path(__file__).resolve().relative_to(ROOT).as_posix()

RUNTIME_IMAGE = (
    "nvcr.io/nvidia/ai-dynamo/sglang-runtime:1.6.1-efa@sha256:"
    "bc30725d441e32406f2ee237bb3f0b720b105ca3b7f0c43b3d68918a23a5a954"
)
PINS = {
    "RUNTIME_IMAGE": RUNTIME_IMAGE,
    "DYNAMO_RUNTIME_VERSION": "1.6.1",
    "SGLANG_VERSION": "0.5.21",
    "NIXL_VERSION": "1.5.0",
    "EFA_INSTALLER_VERSION": "1.50.0",
    "DYNAMO_OPERATOR_CHART_VERSION": "1.6.1",
}
DIGEST_SHORT = "bc30725d"
TAG_PART = "sglang-runtime:1.6.1-efa"

README = ROOT / "README.md"
DEPLOY = ROOT / "docs" / "DEPLOY.md"


# ----------------------------------------------------------------------------------------------------------- helpers
def _read(p: Path | str) -> str:
    path = ROOT / p if isinstance(p, str) else p
    assert path.is_file(), f"{path.relative_to(ROOT)} missing"
    return path.read_text(encoding="utf-8")


def _flat(text: str) -> str:
    """Markdown emphasis dropped and whitespace collapsed, so a wrapped or bolded sentence still matches."""
    return " ".join(text.replace("*", "").replace("`", "").split())


def _section(text: str, heading_re: str) -> str:
    m = re.search(rf"^(#+) .*(?:{heading_re}).*$", text, re.M | re.I)
    if not m:
        return ""
    rest = text[m.end():]
    end = re.search(rf"^#{{1,{len(m.group(1))}}} ", rest, re.M)
    return rest[: end.start()] if end else rest


def _public_text_files(*, include_tests: bool = True) -> dict[str, str]:
    """Tracked text files the public tree ships (this file aside)."""
    out: dict[str, str] = {}
    for rel in S.public_files():
        if rel == THIS:
            continue
        if not include_tests and (rel.startswith("tests/") or "/tests/" in rel):
            continue
        text = S.read_text(rel)
        if text is not None:
            out[rel] = text
    return out


def _versions_env() -> dict[str, str]:
    out: dict[str, str] = {}
    for ln in _read("versions.env").splitlines():
        if ln.strip() and not ln.lstrip().startswith("#"):
            key, sep, value = ln.partition("=")
            assert sep, f"versions.env: not KEY=value: {ln!r}"
            out[key.strip()] = value.strip()
    return out


def _tf_default(name: str) -> str:
    for tf in sorted((ROOT / "terraform" / "cluster").glob("*.tf")):
        m = re.search(rf'variable\s+"{name}"\s*\{{(.*?)\n\}}', tf.read_text(encoding="utf-8"), re.S)
        if m:
            d = re.search(r'default\s*=\s*"([^"]*)"', m.group(1))
            assert d, f"variable {name} has no string default"
            return d.group(1)
    raise AssertionError(f"variable {name!r} not found in terraform/cluster")


# ---------------------------------------------------------------------------------------- (1) versions.env is the source
def test_versions_env_is_a_tracked_root_file_with_the_mit0_header() -> None:
    assert "versions.env" in S.tracked_files(), "versions.env must be tracked at the repository root"
    head = _read("versions.env").splitlines()[:6]
    assert head and all(ln.startswith("#") for ln in head[:2]), "the file starts with a comment header"
    joined = "\n".join(head)
    assert "SPDX-License-Identifier: MIT-0" in joined and "Copyright Amazon.com" in joined


def test_versions_env_is_plain_key_value_lines() -> None:
    for ln in _read("versions.env").splitlines():
        if not ln.strip() or ln.startswith("#"):
            continue
        assert re.fullmatch(r"[A-Z][A-Z0-9_]*=[^\s\"'$]+", ln), f"KEY=value without quotes, spaces or $: {ln!r}"


@pytest.mark.parametrize("key,value", sorted(PINS.items()))
def test_versions_env_pins(key: str, value: str) -> None:
    assert _versions_env().get(key) == value


def test_operator_chart_pin_equals_the_terraform_default() -> None:
    """The chart pin and the terraform default never disagree."""
    assert _versions_env()["DYNAMO_OPERATOR_CHART_VERSION"] == _tf_default("dynamo_version")


def test_the_terraform_validation_accepts_the_public_image() -> None:
    """gen_env and Terraform already take any registry@sha256: pin it, so a tightening regex is caught here."""
    text = _read("terraform/cluster/variables.tf")
    block = re.search(r'variable\s+"runtime_image"\s*\{(.*?)\n\}', text, re.S)
    assert block
    rx = re.search(r'regex\("((?:[^"\\]|\\.)*)",\s*var\.runtime_image\)', block.group(1))
    assert rx, "runtime_image validation regex not found"
    assert re.search(rx.group(1).replace("\\\\", "\\"), RUNTIME_IMAGE)


# ------------------------------------------------------------------------------------------- (1b) same value everywhere
@pytest.mark.parametrize(
    "rel",
    ["README.md", "docs/DEPLOY.md", "terraform/cluster/terraform.tfvars.example", "manifests/config/platform.env.example"],
)
def test_runtime_image_matches_versions_env(rel: str) -> None:
    assert _versions_env()["RUNTIME_IMAGE"] in _read(rel), f"{rel} must carry the pinned RUNTIME_IMAGE verbatim"


def test_tfvars_example_sets_the_public_image_on_a_live_line() -> None:
    live = [ln for ln in _read("terraform/cluster/terraform.tfvars.example").splitlines() if not ln.lstrip().startswith("#")]
    values = [m.group(1) for ln in live if (m := re.match(r'\s*runtime_image\s*=\s*"([^"]*)"', ln))]
    assert values == [RUNTIME_IMAGE], values


def test_platform_env_example_sets_RUNTIME_IMAGE_exactly() -> None:
    cfg = dict(
        ln.split("=", 1) for ln in _read("manifests/config/platform.env.example").splitlines() if ln.strip() and not ln.startswith("#")
    )
    assert cfg["RUNTIME_IMAGE"] == _versions_env()["RUNTIME_IMAGE"]


def test_the_generated_env_fixture_carries_the_pinned_image() -> None:
    expected = _read("tests/publish/fixtures/platform.env.expected")
    assert f"RUNTIME_IMAGE={RUNTIME_IMAGE}" in expected.splitlines()


# Deliberately frozen: the reference cells were rendered for another runtime image before the move to the public one
# (sha256 list verified by tests/manifests), and lib_q38.REFERENCE_IMAGE names that image to mask it when it compares
# them with the repo renders.
FROZEN_REFERENCE = ("tests/manifests/fixtures/cells_reference/", "tests/manifests/lib_q38.py")

_RUNTIME_REF = re.compile(r"[\w./-]*sglang-runtime[\w.-]*(?::[\w.-]+)?@sha256:[0-9a-f]{64}")


def test_every_digest_pinned_runtime_reference_is_the_pinned_image() -> None:
    """READMEs, tfvars, renders, goldens, fixtures, reference cells: one runtime image, nowhere else."""
    bad = {
        rel: sorted({m for m in _RUNTIME_REF.findall(text) if m != RUNTIME_IMAGE})
        for rel, text in _public_text_files().items()
        if not rel.startswith(FROZEN_REFERENCE) and any(m != RUNTIME_IMAGE for m in _RUNTIME_REF.findall(text))
    }
    shown = "\n".join(f"  {p}: {v[:2]}" for p, v in sorted(bad.items())[:40])
    assert not bad, f"{len(bad)} tracked file(s) pin another runtime image:\n{shown}"


@pytest.mark.parametrize("glob", ["examples/*/manifest.yaml"])
def test_committed_renders_run_the_pinned_image(glob: str) -> None:
    import fnmatch

    files = [p for p in S.tracked_files() if fnmatch.fnmatch(p, glob)]
    assert len(files) >= 5, files
    for rel in files:
        images = set(re.findall(r"(?m)^\s*image:\s*(\S+)\s*$", _read(rel)))
        assert images == {RUNTIME_IMAGE}, f"{rel}: {sorted(images)}"


def test_every_runtime_version_override_is_the_pinned_runtime_version() -> None:
    """Every runtimeVersionOverride in the tracked YAML (the frozen reference cells aside) is DYNAMO_RUNTIME_VERSION."""
    want = _versions_env()["DYNAMO_RUNTIME_VERSION"]
    rx = re.compile(r"""(?m)^\s*runtimeVersionOverride:\s*["']?([^"'\s#]+)""")
    found = {
        rel: rx.findall(text)
        for rel, text in _public_text_files().items()
        if rel.endswith(".yaml") and not rel.startswith(FROZEN_REFERENCE[0])
    }
    found = {rel: values for rel, values in found.items() if values}
    assert found, "no runtimeVersionOverride found in the tracked YAML"
    bad = {rel: sorted(set(values)) for rel, values in found.items() if set(values) != {want}}
    assert not bad, f"runtimeVersionOverride must be {want} (versions.env): {bad}"


def test_readme_versions_table_rows_match_versions_env() -> None:
    rows = [ln for ln in _section(_read(README), "version").splitlines() if ln.startswith("|")]

    def row(pat: str) -> str:
        found = [r for r in rows if re.search(pat, r)]
        assert found, f"README versions table lacks a row for {pat!r}"
        return found[0]

    sg = row(r"SGLang runtime")
    assert "0.5.21" in sg and TAG_PART in sg and DIGEST_SHORT in sg and "versions.env" in sg, sg
    assert not re.search(r"(?i)coming|planned|next phase|image phase|private build|not published", sg), sg
    assert "1.6.1" in row(r"(?i)^\|\s*Dynamo runtime|ai-dynamo")
    op = row(r"(?i)operator")
    assert "1.6.1" in op and "runtimeVersionOverride" in op
    assert "1.5.0" in row(r"NIXL") and "1.50.0" in row(r"(?i)\bEFA\b")


def test_deploy_names_the_same_pins() -> None:
    text = _flat(_read(DEPLOY))
    assert RUNTIME_IMAGE in text and "versions.env" in text
    for v in ("0.5.21", "1.6.1"):
        assert v in text, f"DEPLOY lacks {v}"


# --------------------------------------------------------------------------------------- (3) one apply, public image
def _cluster_applies(text: str) -> list[str]:
    return [ln for ln in text.splitlines() if re.search(r"terraform\b.*-chdir=terraform/cluster\b.*\bapply\b|^\s*terraform apply\b", ln)]


def test_readme_quick_start_single_apply() -> None:
    quick = _section(_read(README), "Quick start")
    assert quick.strip(), "README has no Quick start section"
    assert len(_cluster_applies(quick)) == 1, _cluster_applies(quick)
    assert not re.search(r"(?i)second apply|apply[^.\n]*again|two[- ]step|\bempty\b|runtime_image\s*=\s*\"\"", quick)
    assert "runtime_image" in quick and (re.search(r"nvcr\.io", quick) or "versions.env" in quick or "terraform.tfvars.example" in quick)


def test_deploy_cluster_section_single_apply() -> None:
    sec = _section(_read(DEPLOY), r"2\. Cluster")
    assert sec.strip()
    assert len(_cluster_applies(sec)) == 1, _cluster_applies(sec)
    assert not re.search(r"(?i)apply[^.\n]*again|second apply|first apply|empty for the first", sec)
    assert not re.search(r'runtime_image\s*=\s*""', _read(DEPLOY))
    # The image is quoted once in DEPLOY.md, in Prerequisites (test_runtime_image_matches_versions_env); step 2 names the key.
    assert "runtime_image" in sec

