# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Phase 2 of the publication: the sample runs on the PUBLIC NGC runtime image, not on a derived build (RED first).

Decision (2026-10-26): the workers use ``nvcr.io/nvidia/ai-dynamo/sglang-runtime:1.6.0-efa`` pinned by digest (Dynamo 1.6.0
stable, SGLang 0.5.21, NIXL 1.5.0, EFA installer 1.50.0).  No Dockerfile, no image build and no scan gate for the runtime
image ship in the sample.  Every measured result (W2, W3-16K, the demo) came from a derived SGLang 0.5.22 build, and the
docs say so.  The recipe of that build is kept as an INTERNAL backup inside the design-log folder (never exported).

Contract pinned here
====================
1. ``test_versions_env_*``                  root ``versions.env`` is the single source of the pins.
2. ``test_*_matches_versions_env``          the same values everywhere they appear (docs, tfvars example, platform env
                                            example, renders, goldens, guides).
3. ``test_no_derived_image_*``              nothing outside the design log names the derived image or how to build it;
                                            0.5.22 appears only as "the version of the measured results".
4. ``test_*_single_apply*``                 one cluster apply with ``runtime_image`` set; an optional "Mirror" section.
5. ``test_derived_image_backup_*``          the internal backup folder, its README, its ``.txt`` recipe files, no real
                                            account id, never exported.
6. ``test_troubleshooting_*``               the 14.8 GB image-pull entry; nothing about building the runtime image.
7. ``test_bench_image_*``                   the matrix-driver bench image keeps its Dockerfile and its DEPLOY build step.
8. ``test_guides_*``                        "Restore correctness: measured on ..." replaces "Why SGLang 0.5.22".

Forbidden literals are assembled from fragments, and this file is the only one the scans skip, so a green scan means
something.  Fully offline: git ls-files plus text reads, no AWS, no cluster, no network.
"""

from __future__ import annotations

import re
import stat
import subprocess
from pathlib import Path

import pytest
import sanitize_scan as S

ROOT = S.REPO_ROOT
THIS = Path(__file__).resolve().relative_to(ROOT).as_posix()

SDD = "docs/" + "sdd"
BACKUP = f"{SDD}/derived-image-0522"

RUNTIME_IMAGE = (
    "nvcr.io/nvidia/ai-dynamo/sglang-runtime:1.6.0-efa@sha256:"
    "d574951b0dbd037bdcaabe121431a83cd047a5c106cb243fe6f2f97f2e456cd2"
)
PINS = {
    "RUNTIME_IMAGE": RUNTIME_IMAGE,
    "DYNAMO_RUNTIME_VERSION": "1.6.0",
    "SGLANG_VERSION": "0.5.21",
    "NIXL_VERSION": "1.5.0",
    "EFA_INSTALLER_VERSION": "1.50.0",
    "DYNAMO_OPERATOR_CHART_VERSION": "1.5.1",
}
DIGEST_SHORT = "d574951b"
TAG_PART = "sglang-runtime:1.6.0-efa"

README = ROOT / "README.md"
DEPLOY = ROOT / "docs" / "DEPLOY.md"
TROUBLE = ROOT / "docs" / "TROUBLESHOOTING.md"
GUIDES = {
    "hicache": ROOT / "docs/optimizations/hicache/README.md",
    "kv-router": ROOT / "docs/optimizations/kv-router/README.md",
    "pd-disaggregation": ROOT / "docs/optimizations/pd-disaggregation/README.md",
}


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
    """Tracked text files outside the design log (and outside this file)."""
    out: dict[str, str] = {}
    for rel in S.tracked_files():
        if rel.startswith(SDD + "/") or rel == THIS:
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
    """Keep 1.5.1 unless a 1.6.0 chart is proven to exist; either way the two sources never disagree."""
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


# Deliberately frozen: the reference cells are the recorded output of the derived 0.5.22 build (sha256 list verified by
# tests/manifests), and lib_trackq.REFERENCE_IMAGE names it to mask it when it compares them with the repo renders.
# manifests/stage-mocker runs the stock NGC 1.5.1 image on purpose (a CPU mocker of the staging flow, pinned by tests/manifests).
FROZEN_REFERENCE = ("tests/manifests/fixtures/cells_reference/", "tests/manifests/lib_trackq.py", "manifests/stage-mocker/")

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


@pytest.mark.parametrize("glob", ["examples/*/rendered.yaml", "manifests/track-q-qwen38/rendered/S*.yaml"])
def test_committed_renders_run_the_pinned_image(glob: str) -> None:
    import fnmatch

    files = [p for p in S.tracked_files() if fnmatch.fnmatch(p, glob)]
    assert len(files) >= 5, files
    for rel in files:
        images = set(re.findall(r"(?m)^\s*image:\s*(\S+)\s*$", _read(rel)))
        assert images == {RUNTIME_IMAGE}, f"{rel}: {sorted(images)}"


def test_readme_versions_table_rows_match_versions_env() -> None:
    rows = [ln for ln in _section(_read(README), "version").splitlines() if ln.startswith("|")]

    def row(pat: str) -> str:
        found = [r for r in rows if re.search(pat, r)]
        assert found, f"README versions table lacks a row for {pat!r}"
        return found[0]

    sg = row(r"SGLang runtime")
    assert "0.5.21" in sg and TAG_PART in sg and DIGEST_SHORT in sg and "versions.env" in sg, sg
    assert not re.search(r"(?i)coming|planned|next phase|image phase|private build|not published", sg), sg
    assert "1.6.0" in row(r"(?i)^\|\s*Dynamo runtime|ai-dynamo")
    op = row(r"(?i)operator")
    assert "1.5.1" in op and "runtimeVersionOverride" in op and "1.6.0" in op
    assert "1.5.0" in row(r"NIXL") and "1.50.0" in row(r"(?i)\bEFA\b")


def test_deploy_names_the_same_pins() -> None:
    text = _flat(_read(DEPLOY))
    assert RUNTIME_IMAGE in text and "versions.env" in text
    for v in ("0.5.21", "1.6.0"):
        assert v in text, f"DEPLOY lacks {v}"


@pytest.mark.parametrize("doc", list(GUIDES))
def test_guides_versions_section_names_the_public_image(doc: str) -> None:
    sec = _flat(_section(_read(GUIDES[doc]), "Versions"))
    assert sec, f"{doc}: Versions section missing"
    assert TAG_PART in sec and DIGEST_SHORT in sec, f"{doc}: Versions must name the public image tag and digest"
    for tok in ("0.5.21", "1.6.0", "1.5.1"):
        assert tok in sec, f"{doc}: Versions lacks {tok!r}"
    assert re.search(r"(?i)measured with a derived SGLang 0\.5\.22", sec), f"{doc}: say the results came from the derived 0.5.22 build"


# --------------------------------------------------------------- (2) the derived image is not described outside the log
_BANNED = {
    "derived image digest": "03bf" + "4090",
    "upstream image repo": "lmsys" + "org/sglang",
    "dev build tag": "1.6.0.dev" + "20261022",
    "kaniko": "kani" + "ko",
    "recipe path": "images/sglang" + "-dynamo",
    "phase wording": "Phase 2 " + "image",
    "later-phase wording": "coming in the " + "image phase",
}


@pytest.mark.parametrize("what,literal", sorted(_BANNED.items()))
def test_no_derived_image_literal_outside_the_design_log(what: str, literal: str) -> None:
    hits = [rel for rel, text in _public_text_files().items() if literal.lower() in text.lower()]
    assert not hits, f"{what} ({literal!r}) still named outside the design log: {hits}"


def test_nobody_says_sglang_0522_is_required() -> None:
    rx = re.compile(r"(?i)(?:0\.5\.22[^.\n]{0,40}\b(?:is |are )?(?:required|mandatory|needed|must)|"
                    r"(?:require[sd]?|need[s]?|must (?:use|run))[^.\n]{0,40}0\.5\.22)")
    hits = {rel: rx.findall(_flat(text)) for rel, text in _public_text_files().items() if rx.search(_flat(text))}
    assert not hits, f"SGLang 0.5.22 is not required any more: {hits}"


def test_readme_says_the_results_came_from_a_derived_0522_build() -> None:
    text = _flat(_read(README))
    assert re.search(r"(?i)measured with a derived SGLang 0\.5\.22 build", text), (
        "README: say that the results were measured with a derived SGLang 0.5.22 build")
    sentences = [s for s in re.split(r"(?<=[.!?])\s+", text) if re.search(r"(?i)restore", s)]
    assert any("0.5.21" in s and "0.5.22" in s and re.search(r"(?i)validated", s) for s in sentences), (
        "README: restore correctness was validated on 0.5.21 and on 0.5.22")


def test_other_public_docs_mark_0522_as_the_measured_version() -> None:
    """Every markdown line outside the log that names 0.5.22 also says it is the version of the results or the probes."""
    ok = re.compile(r"(?i)measured|derived|results|runs of record|validated|probe|restore|16/16|tested|build|W2|W3|demo|cells?")
    bad = []
    for rel, text in _public_text_files(include_tests=False).items():
        if not rel.endswith(".md"):
            continue
        for n, ln in enumerate(text.splitlines(), 1):
            if "0.5.22" in ln and not ok.search(ln):
                bad.append(f"{rel}:{n}")
    assert not bad, bad


# --------------------------------------------------------------------------------------- (3) one apply, public image
def _cluster_applies(text: str) -> list[str]:
    return [ln for ln in text.splitlines() if re.search(r"terraform\b.*-chdir=terraform/cluster\b.*\bapply\b|^\s*terraform apply\b", ln)]


def test_readme_quick_start_single_apply() -> None:
    quick = _section(_read(README), "Quick start")
    assert quick.strip(), "README has no Quick start section"
    assert len(_cluster_applies(quick)) == 1, _cluster_applies(quick)
    assert not re.search(r"(?i)second apply|apply[^.\n]*again|two[- ]step|\bempty\b|runtime_image\s*=\s*\"\"", quick)
    assert "runtime_image" in quick and ("nvcr.io" in quick or "versions.env" in quick or "terraform.tfvars.example" in quick)


def test_deploy_cluster_section_single_apply() -> None:
    sec = _section(_read(DEPLOY), r"2\. Cluster")
    assert sec.strip()
    assert len(_cluster_applies(sec)) == 1, _cluster_applies(sec)
    assert not re.search(r"(?i)apply[^.\n]*again|second apply|first apply|empty for the first", sec)
    assert not re.search(r'runtime_image\s*=\s*""', _read(DEPLOY))
    assert f'runtime_image            = "{RUNTIME_IMAGE}"' in sec or RUNTIME_IMAGE in sec


def test_terraform_docs_drop_the_two_step_flow() -> None:
    readme = _flat(_read("terraform/README.md"))
    assert not re.search(r"(?i)empty for the first apply|first apply|runtime_image\s*=\s*\"\"|Mirror the runtime image", readme), (
        "terraform/README.md still describes the two-step runtime_image flow")
    variables = _flat(_read("terraform/cluster/variables.tf"))
    assert not re.search(r"(?i)empty until the image is mirrored", variables), "runtime_image description still says it starts empty"
    tfvars = _read("terraform/cluster/terraform.tfvars.example")
    assert not re.search(r"(?i)leave \"\" for the first apply|apply again", tfvars)


def test_deploy_keeps_an_optional_mirror_section() -> None:
    text = _read(DEPLOY)
    sec = _section(text, "Mirror the image to your ECR")
    assert sec.strip(), "DEPLOY needs a section 'Mirror the image to your ECR'"
    assert re.search(r"(?i)optional", sec) and "sglang_runtime_ecr_repository_url" in sec and "runtime_image" in sec
    assert re.search(r"(?i)crane|docker buildx imagetools|skopeo", sec), "show one digest-preserving copy command"
    assert "sha256" in sec


def test_deploy_and_readme_do_not_tell_the_reader_to_build_the_runtime_image() -> None:
    for p in (README, DEPLOY):
        text = _flat(_read(p))
        assert not re.search(r"(?i)you build and scan it yourself|build an equivalent image|vulnerability-scan gate", text), p.name
        assert not re.search(r"(?i)\bDockerfile\b[^.\n]*runtime", text), p.name


# ------------------------------------------------------------------------------------ (4) the internal backup folder
RECIPE_FILES = [
    "Dockerfile.txt",
    "requirements-dynamo.txt",
    "install_nixl_ucx_compat.sh.txt",
    "discover_nixl_ucx_layout.py.txt",
    "build-job.yaml.txt",
    "probe.sh.txt",
]


def _backup_files() -> list[str]:
    return [p for p in S.tracked_files() if p.startswith(BACKUP + "/")]


def test_derived_image_backup_is_tracked_with_readme_and_recipe() -> None:
    names = {p[len(BACKUP) + 1:] for p in _backup_files()}
    assert "README.md" in names, sorted(names)
    missing = [f for f in RECIPE_FILES if f not in names]
    assert not missing, f"recipe files missing (tracked): {missing}"


def test_derived_image_backup_has_no_code_extensions_and_no_exec_bit() -> None:
    """.txt only (README.md aside), so no code, shell, SPDX or shellcheck guard scans them; never executable."""
    for rel in _backup_files():
        name = rel.rsplit("/", 1)[-1]
        assert name == "README.md" or name.endswith(".txt"), f"{rel}: recipe files carry a .txt suffix"
        assert not (ROOT / rel).stat().st_mode & stat.S_IXUSR, f"{rel} must not be executable"
    modes = subprocess.run(["git", "-C", str(ROOT), "ls-files", "-s", "--", BACKUP], capture_output=True, text=True, check=True).stdout
    assert modes and all(ln.startswith("100644 ") for ln in modes.splitlines()), modes


def test_derived_image_backup_leaves_out_the_model_only_patch() -> None:
    names = [p.rsplit("/", 1)[-1] for p in _backup_files()]
    assert not [n for n in names if "0007" in n or n.endswith(".patch") or n.endswith(".patch.txt")], names
    dockerfile = _read(f"{BACKUP}/Dockerfile.txt")
    assert "0007" not in dockerfile and ("gl" + "m-vision") not in dockerfile, "the model-only patch is not part of the backup recipe"
    assert "0007" not in _read(f"{BACKUP}/build-job.yaml.txt")


def test_derived_image_backup_readme_says_how_to_reach_and_rebuild() -> None:
    text = _flat(_read(f"{BACKUP}/README.md"))
    digests = set(re.findall(r"sha256:[0-9a-f]{64}", text))
    assert "sha256:" + "03bf" + "4090ab89ac516a6837a47df81752a6e4e354006b41c9f7d25f7fefc59c83" in digests, "base image digest"
    assert "sha256:614f0b96dacab741adf42a8db98eaf59d844327b96ffc4fe5083a57d21e11a4b" in digests, "digest of the image of the results"
    assert "<LA" + "B_ACCOUNT>" in text and ".dkr.ecr." in text and "sglang-runtime" in text, "internal ECR repo with the placeholder account"
    assert "0.5.22" in text and "1.6.0.dev" + "20261022" in text
    assert "images/sglang-0522-dynamo" in text, "where the original recipe lives in the internal artifacts folder"
    for word in ("requirements", "build", "Dockerfile.txt", "build-job.yaml.txt"):
        assert word in text, f"README of the backup should mention {word!r}"
    assert re.search(r"(?i)internal", text) and re.search(r"(?i)not (?:part of|shipped|exported|published)|never (?:shipped|exported|published)", text)


def test_derived_image_backup_adds_no_real_account_id_or_local_path() -> None:
    offenders = {}
    for rel in _backup_files():
        hits = S.scan_text(S.read_text(rel) or "")
        if hits:
            offenders[rel] = hits[:3]
    assert not offenders, f"the backup must use the placeholder account, no real id, path or host: {offenders}"


def test_derived_image_backup_is_excluded_from_the_public_export() -> None:
    """The export allowlist skips the whole design-log folder (sanitize_scan.ALLOWLIST_PREFIX); the backup lives inside it."""
    assert S.ALLOWLIST_PREFIX == SDD + "/"
    assert BACKUP.startswith(S.ALLOWLIST_PREFIX)
    assert not [p for p in _backup_files() if not p.startswith(S.ALLOWLIST_PREFIX)]


def test_public_files_do_not_point_at_the_backup() -> None:
    hits = [rel for rel, text in _public_text_files().items() if "derived-image-0522" in text]
    assert not hits, f"the backup is internal; public files must not mention it: {hits}"


def test_design_log_top_level_files_are_untouched() -> None:
    """plan/spec/Task at the top level of the log are append-only and byte-identical to origin/main."""
    for name in ("plan.md", "spec.md", "Task.md"):
        diff = subprocess.run(
            ["git", "-C", str(ROOT), "diff", "--quiet", "origin/main", "--", f"{SDD}/{name}"], capture_output=True, check=False
        )
        assert diff.returncode == 0, f"{SDD}/{name} was edited"


# -------------------------------------------------------------------------------------------- (5) TROUBLESHOOTING
def test_troubleshooting_has_the_image_pull_entry() -> None:
    text = _read(TROUBLE)
    sec = _flat(_section(text, r"image pull"))
    assert sec, "TROUBLESHOOTING needs an entry about the image pull"
    assert "14.8 GB" in sec and "NAT" in sec and re.search(r"(?i)about 7 min", sec), sec
    assert re.search(r"(?i)mirror[^.]*ECR", sec), "tell the reader to mirror the image to ECR"
    assert "13.7" not in text, "the old size (13.7 GB) is gone"


def test_troubleshooting_has_nothing_about_building_the_runtime_image() -> None:
    text = _flat(_read(TROUBLE))
    assert not re.search(r"(?i)build(?:ing)? (?:the |your |a )?(?:runtime|worker|sglang)[ -]?(?:image)?|kani" + "ko|Dockerfile|docker build", text)


# ---------------------------------------------------------------------------------------------- (6) the bench image
def test_bench_image_dockerfile_and_deploy_build_step_stay() -> None:
    assert "tools/bench-image/Dockerfile" in S.tracked_files()
    deploy = _read(DEPLOY)
    assert re.search(r"docker buildx build -f tools/bench-image/Dockerfile", deploy), "DEPLOY keeps the bench image build step"
    assert "docker buildx" in _read(README), "README prerequisites still list docker buildx for the bench image"


# ----------------------------------------------------------------------------------------------- (7) the guides
RESTORE_LINE = (
    "Restore correctness: measured on 0.5.18 (15/16 L3), 0.5.21 (16/16 L2 and L3 on Qwen3.8 TP1, bs=1, RTX PRO 6000) "
    "and 0.5.22 (16/16)"
)


@pytest.mark.parametrize("doc", list(GUIDES))
def test_guides_replace_why_sglang_0522_with_restore_correctness(doc: str) -> None:
    text = _flat(_read(GUIDES[doc]))
    assert "Why SGLang 0.5.22" not in text, f"{doc}: the 'Why SGLang 0.5.22' note must go"
    assert _flat(RESTORE_LINE) in text, f"{doc}: expected the sentence {RESTORE_LINE!r}"


@pytest.mark.parametrize("doc", list(GUIDES))
def test_guides_keep_the_corrupted_restore_gate_lesson(doc: str) -> None:
    text = _flat(_read(GUIDES[doc]))
    assert re.search(r"0\.5\.21[^.]{0,80}corrupt[^.]{0,120}another hybrid model", text), f"{doc}: keep the 0.5.21 corruption fact"
    assert re.search(r"(?i)measure restore correctness before speed", text), f"{doc}: keep the gate lesson"
