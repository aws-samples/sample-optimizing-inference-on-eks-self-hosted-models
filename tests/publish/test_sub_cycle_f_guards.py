# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Sub-cycle F guards (RED until the publish clean-up of process wording, names, region and test trees lands).

Finding -> guard (numbers are those of the review of 4190e87)
==============================================================
* [1][3][4] ``test_no_process_wording`` (parametrized): none of the PROCESS_WORDING patterns (a person's title, the
  work-block number in any spelling, decision attributions, a prototype label, a private-registry tag or digest, the
  original bench image name, the name of the internal scanner) in the content OR the path of any tracked file outside
  docs/sdd.
  ``test_w3_plans_live_in_a_neutral_directory``: the W3-16K plans moved to ``plans/w3-16k``.
  ``test_budget_alert_...`` lives in tools/demo_bench/tests/unit/test_cost_ledger.py.
* [2] ``test_bench_image_dockerfile_names_no_private_registry``.
* [5] ``test_part_of_label_*``, ``test_bench_image_repo_*``, ``test_example_config_bench_repo_*``: the label and the
  bench image repository equal the Terraform default ``project_name`` (``<project_name>/bench`` is the ECR repo that
  terraform/cluster/ecr.tf creates and DEPLOY tells the reader to push to).
* [6] ``test_example_config_*region*``, ``test_committed_renders_*``, ``test_no_original_region_*``: the placeholder config,
  the committed renders and the diffs use the region of the docs (the ``aws_region`` of the tfvars examples).
* [7] ``test_vocabulary_covers_paths_and_test_trees`` (+ allowlist hygiene): the vocabulary guard reads path names and the
  test trees too.
* [8] tests/scripts/test_153_stage_model_token_env_only.py, ``test_platform_env_example_does_not_send_the_token_to_sample_env``.
* [9] ``test_gen_env_keeps_hf_token_forbidden``.
* [10] ``test_dashboards_*_skip_without_a_dynamo_checkout`` and ``test_tests_readme_says_dashboards_skip``.
* [11] ``test_event_name_is_confined_to_the_documented_files``.
* [12] ``test_no_unused_noqa_in_tracked_python``.
* [13] ``test_e4_to_e7_pin_the_expanded_plan_e3`` and ``test_matrix_driver_readme_explains_the_plan_e3_pin``.

Documented allowlist (nothing else is exempt)
=============================================
* ``docs/sdd/``: append-only design log, byte-identical, excluded from the export.
* PRODUCT vocabulary (e_guards.VOCABULARY plus PRODUCT_EXTRA: model, track, decision-id, city and environment names of
  the original deployment) and its region literals are tolerated, as deny lists, ONLY in ``DENYLIST_FILES``: the guards whose job is to assert that those things are gone.
  Every entry must still contain a hit (a stale entry fails), and none may be outside ``tests/publish`` or
  ``tests/terraform/test_tree_layout.py``.
* The original-region literal has no other allowlist: the mocked ``terraform test`` files and their fixtures
  (``terraform/*/tests/``) are read too and use the docs region.
* ``docs/static/architecture.drawio`` (diagram shipped as drawn) may carry exactly the vocabulary and original-region hits
  pinned in ``e_guards.AS_DRAWN_HITS`` and nothing else (``e_guards.as_drawn``; test_static_architecture.py keeps the pin
  exact).
* The PROCESS wording has NO allowlist at all: even the guards spell it with fragments, and so does this file.
* The event name "KubeCon NA 2026" is kept in ``EVENT_NAME_FILES`` only (see ``test_event_name_...``).
"""

from __future__ import annotations

import importlib.util
import os
import re
import shutil
import sys
from pathlib import Path

import lib_proc
import pytest
import sanitize_scan
import test_sub_cycle_e_guards as e_guards

ROOT = sanitize_scan.REPO_ROOT
THIS = Path(__file__).resolve().relative_to(ROOT).as_posix()

DENYLIST_FILES = {
    "tests/publish/test_cleanup_layout.py": "asserts the removed trees/keys are gone",
    "tests/publish/test_docs_set.py": "deny list of claims the public docs must not make",
    "tests/publish/test_sanitization_guards.py": "scanner self-tests with the original region and stack names",
    "tests/publish/test_sub_cycle_e_guards.py": "vocabulary and region deny lists of sub-cycle E",
    "tests/terraform/test_tree_layout.py": "asserts the original stacks left terraform/",
}
EVENT_NAME_FILES = {
    "dashboards/demo/compare.json", "dashboards/demo/compare-configmap.yaml", "dashboards/demo/matrix.json",
    "dashboards/demo/matrix-configmap.yaml", "dashboards/sglang/sglang.json",
    "tools/demo_bench/README.md", "tools/demo_bench/pyproject.toml",
}


def _files() -> list[str]:
    return [r for r in sanitize_scan.tracked_files() if not r.startswith(sanitize_scan.ALLOWLIST_PREFIX)]


def _text(rel: str) -> str:
    return sanitize_scan.read_text(rel) or ""


def _hits(pattern: str, rel: str, text: str) -> list[str]:
    out = [f"path: {m.group(0)!r}" for m in re.finditer(pattern, rel)]
    out += [f"{text.count(chr(10), 0, m.start()) + 1}: {m.group(0)!r}" for m in re.finditer(pattern, text)]
    return out


def _is_test_tree(rel: str) -> bool:
    return rel.startswith("tests/") or bool(re.match(r"tools/[^/]+/tests/", rel))


def _project_name() -> str:
    text = (ROOT / "terraform/cluster/variables.tf").read_text()
    m = re.search(r'variable "project_name"\s*\{[^}]*?default\s*=\s*"([^"]+)"', text, re.DOTALL)
    assert m, "terraform/cluster/variables.tf: project_name default"
    return m.group(1)


def _doc_region() -> str:
    text = (ROOT / "terraform/cluster/terraform.tfvars.example").read_text()
    m = re.search(r'(?m)^aws_region\s*=\s*"([a-z0-9-]+)"', text)
    assert m, "terraform/cluster/terraform.tfvars.example: aws_region"
    return m.group(1)


def _region_code(region: str) -> str:
    a, b, c = region.split("-")
    return f"{a}{b[0]}{c}"  # us-east-2 -> use2 (the prefix of the AZ IDs)


def _platform_example() -> dict[str, str]:
    out = {}
    for line in (ROOT / "manifests/config/platform.env.example").read_text().splitlines():
        if line.strip() and not line.startswith("#"):
            k, _, v = line.partition("=")
            out[k] = v
    return out


# ------------------------------------------------------------------------------------------------ process wording
# Built from fragments so this file never carries what it forbids.
PROCESS_WORDING = {
    "person title": r"(?i)\bspeak" + r"er\b",
    "work-block number (any spelling)": r"(?i)block[ _-]?" + r"2(?!\d)",
    "decision attribution wording": r"(?i)le" + r"ad decision|decision:\s*le" + r"ad|le" + r"ad correction|\ble" + r"ad,\s*20\d\d",
    "prototype label": r"(?i)\bspi" + r"ke\b",
    "private registry tag or digest": r"be71" + r"fd0c|54af" + r"e3d|(?i:ECR enhanced" + r" scan of)",
    "bench image name of the original registry": r"kubecon" + r"-dynamo|kubecon" + r"-na26",
    # The internal content-review scanner: the public files say "static analysis" (and name the public rule).
    "internal scanner name": r"(?i)\bhol" + r"mes\b",
}


@pytest.mark.parametrize("what", sorted(PROCESS_WORDING))
def test_no_process_wording(what: str) -> None:
    bad = {}
    for rel in _files():
        hits = _hits(PROCESS_WORDING[what], rel, _text(rel))
        if hits:
            bad[rel] = hits
    shown = "\n".join(f"  {p} ({len(h)}): {h[:2]}" for p, h in sorted(bad.items())[:60])
    assert not bad, f"{len(bad)} tracked file(s) (paths included) carry '{what}' (no allowlist):\n{shown}"


def test_w3_plans_live_in_a_neutral_directory() -> None:
    names = [f"plan-w3-r{r}{s}.txt" for r in (10, 25, 32) for s in ("", "-cells")]
    missing = [n for n in names if not (ROOT / "plans/w3-16k" / n).is_file()]
    assert not missing, f"plans/w3-16k lacks {missing}"
    assert not (ROOT / ("plans/" + "block" + "2")).exists(), "the old plans directory must be gone"
    render = (ROOT / "tools/matrix_driver/render_driver.py").read_text()
    assert re.search(r'PLANS_DIR\s*=\s*REPO\s*/\s*"plans"\s*/\s*"w3-16k"', render), "render_driver default plans dir"
    for cells in names[1::2]:
        head = (ROOT / "plans/w3-16k" / cells).read_text().splitlines()[0]
        assert head.startswith("# " + cells.removesuffix(".txt") + " (W3-16K"), head


def test_bench_image_dockerfile_names_no_private_registry() -> None:
    text = (ROOT / "tools/bench-image/Dockerfile").read_text()
    assert not re.search(r"sha256:[0-9a-f]{6,}\.\.\.", text), "no truncated digest of a registry image"
    assert not re.search(r"(?i)bench:[0-9a-f]{7}\b", text), "no image tag of a registry image"
    assert not re.search(r"(?m)^#.*\bdecision\b.*\b20\d\d-\d\d-\d\d", text), "no dated decision attribution"


# ------------------------------------------------------------------------------------------------ names (finding 5)
def test_project_name_default_is_the_same_in_both_stacks() -> None:
    boot = (ROOT / "terraform/bootstrap/variables.tf").read_text()
    m = re.search(r'variable "project_name"\s*\{[^}]*?default\s*=\s*"([^"]+)"', boot, re.DOTALL)
    assert m and m.group(1) == _project_name() == "dynamo-sample"


PART_OF_FILES = (
    "manifests/bench/deployment.yaml", "manifests/bench/serviceaccount.yaml", "manifests/node-ops/job-model-prefetch.yaml",
    "manifests/node-ops/pod-ephemeral-shell.yaml", "manifests/node-ops/prepull/daemonset.yaml", "scripts/10-stage-model.sh",
)


def test_part_of_label_equals_the_project_name_everywhere() -> None:
    """The bench/node-ops/staging objects carry the Terraform ``project_name`` default as part-of (the Track Q cells keep
    their own ``qwen38-matrix`` label, which names a workload, not the project)."""
    project = _project_name()
    bad = {}
    for rel in PART_OF_FILES:
        vals = re.findall(r"app\.kubernetes\.io/part-of:\s*([^\s#]+)", _text(rel))
        assert vals, f"{rel}: no part-of label"
        wrong = sorted({v for v in vals if v.strip("'\"") != project})
        if wrong:
            bad[rel] = wrong
    assert not bad, f"part-of must be '{project}' (the Terraform project_name default): {bad}"


def test_bench_image_repo_equals_the_terraform_ecr_repo() -> None:
    project = _project_name()
    ecr = (ROOT / "terraform/cluster/ecr.tf").read_text()
    assert re.search(r'resource "aws_ecr_repository" "bench"\s*\{[^}]*name\s*=\s*"\$\{var\.project_name\}/bench"', ecr, re.DOTALL)
    images = re.findall(r"image:\s*(\S+/bench)@sha256:[0-9a-f]{64}", (ROOT / "manifests/bench/deployment.yaml").read_text())
    assert images, "manifests/bench/deployment.yaml: a bench image pinned by digest"
    assert all(i.endswith(f"/{project}/bench") for i in images), images
    dockerfile = (ROOT / "tools/bench-image/Dockerfile").read_text()
    pushed = re.findall(r"-t\s+\S*?/([\w.-]+/bench):<tag>", dockerfile)
    assert pushed == [f"{project}/bench"], f"the Dockerfile build command must tag <registry>/{project}/bench: {pushed}"


def test_example_config_bench_repo_matches_what_deploy_tells_the_reader_to_push() -> None:
    project = _project_name()
    cfg = _platform_example()
    assert cfg["BENCH_REPO"] == f"{cfg['ECR_REGISTRY']}/{project}/bench"
    # The default stack has no sglang-runtime repository (create_runtime_mirror_repository = false): empty key.
    assert cfg["SGLANG_RUNTIME_REPO"] == ""
    deploy = (ROOT / "docs/DEPLOY.md").read_text()
    assert "<BENCH_REPO>" in deploy  # the push target is the BENCH_REPO output, i.e. <project_name>/bench
    assert "kubecon" + "-dynamo" not in deploy


# ------------------------------------------------------------------------------------------------ region (finding 6)
def test_docs_agree_on_one_region() -> None:
    region = _doc_region()
    boot = re.search(r'(?m)^aws_region\s*=\s*"([^"]+)"', (ROOT / "terraform/bootstrap/terraform.tfvars.example").read_text())
    assert boot and boot.group(1) == region
    readme = re.search(r'aws_region\s*=\s*"([^"]+)"', (ROOT / "terraform/README.md").read_text())
    assert readme and readme.group(1) == region
    assert f"cluster `dynamo-sample-{_region_code(region)}`" in (ROOT / "terraform/README.md").read_text() or \
        f"dynamo-sample-{_region_code(region)}" in (ROOT / "terraform/README.md").read_text()
    assert region in (ROOT / "README.md").read_text()


def test_example_config_region_equals_the_docs_region() -> None:
    region, code, project = _doc_region(), _region_code(_doc_region()), _project_name()
    cfg = _platform_example()
    account = sanitize_scan.PLACEHOLDER_ACCOUNT
    assert cfg["AWS_ACCOUNT_ID"] == account, "the placeholder account stays (a real account id is still forbidden)"
    assert cfg["AWS_REGION"] == region
    assert cfg["ECR_REGISTRY"] == f"{account}.dkr.ecr.{region}.amazonaws.com"
    assert cfg["CLUSTER_NAME"] == f"{project}-{code}"
    for key in ("GPU_AZ", "PRIMARY_AZ"):
        assert cfg[key].startswith(region), (key, cfg[key])
    for key, kind in (("MODEL_BUCKET", "models"), ("RESULTS_BUCKET", "results")):  # terraform/bootstrap/main.tf names
        assert cfg[key] == f"{project}-{kind}-{account}-{code}", (key, cfg[key])
    assert cfg["MODEL_S3_URI"].startswith(f"s3://{cfg['MODEL_BUCKET']}/")
    # RUNTIME_IMAGE is the public NGC image now (pinned by test_platform_env_example_sets_RUNTIME_IMAGE_exactly), not ECR.
    assert cfg["BENCH_REPO"].startswith(f"{cfg['ECR_REGISTRY']}/"), cfg["BENCH_REPO"]
    assert cfg["SGLANG_RUNTIME_REPO"] == "", "the optional runtime mirror repository is off in the example"


def test_committed_renders_and_diffs_use_the_docs_region() -> None:
    region = _doc_region()
    pins = [r for r in _files() if re.fullmatch(r"examples/[^/]+/(rendered\.yaml|diff-vs-baseline\.diff)", r)
            or re.fullmatch(r"manifests/track-q-qwen38/rendered/S\d\.yaml", r)]
    assert len(pins) >= 12, pins
    bad = {}
    for rel in pins:
        regions = set(re.findall(r"\.dkr\.ecr\.([a-z0-9-]+)\.amazonaws\.com", _text(rel)))
        if regions - {region}:
            bad[rel] = sorted(regions)
    assert not bad, f"committed renders must be rendered from the example config of region {region}: {bad}"
    # The runtime image is public (no ECR host), so a rendered.yaml may carry no ECR host at all. What is asserted
    # instead: every ECR host that does appear (bench and other images) is the docs region (checked above), and
    # no rendered.yaml may be left with an ECR host of another region or a real account.
    for rel in (r for r in pins if r.endswith("rendered.yaml")):
        hosts = re.findall(r"([\w-]+)\.dkr\.ecr\.([a-z0-9-]+)\.amazonaws\.com", _text(rel))
        assert all(acct == sanitize_scan.PLACEHOLDER_ACCOUNT and reg == region for acct, reg in hosts), (rel, hosts)


ORIGINAL_REGION = r"ap-" + r"south-1|\bap" + r"s1\b|ap" + r"s1(?![A-Za-z0-9])"
ORIGINAL_REGION_PATTERNS = [(ORIGINAL_REGION, "original region")]  # the label of e_guards.AS_DRAWN_HITS


def test_no_original_region_in_public_config_renders_goldens_or_docs() -> None:
    bad = {}
    for rel in _files():
        if rel in DENYLIST_FILES:
            continue
        hits = _hits(ORIGINAL_REGION, rel, _text(rel))
        if hits and not e_guards.as_drawn(rel, _text(rel), ORIGINAL_REGION_PATTERNS):
            bad[rel] = hits
    shown = "\n".join(f"  {p} ({len(h)}): {h[:2]}" for p, h in sorted(bad.items())[:60])
    assert not bad, (
        f"{len(bad)} file(s) carry the original region (allowed: DENYLIST_FILES only):\n{shown}"
    )


def test_region_guard_of_test_95_still_forbids_the_deployment_values() -> None:
    """test_95 forbids any region literal and any short code outside platform.env(.example): after the switch the
    forbidden set is the docs region and its short code, so the placeholder deployment is what the guard protects."""
    region, code = _doc_region(), _region_code(_doc_region())
    src = (ROOT / "tests/manifests/test_95_region_config.py").read_text()
    assert "REGION_RE" in src and "SHORT_RE" in src
    short = re.search(r"SHORT_RE\s*=\s*re\.compile\(r\"([^\"]+)\"\)", src)
    assert short and re.search(short.group(1), code), f"SHORT_RE must match the short code {code}"
    assert re.search(r"(?<![A-Za-z0-9])(?:us|eu|ap)-(?:north|south|east|west)", region)
    leaks = []
    for root in (ROOT / "manifests", ROOT / "scripts"):
        for p in sorted(root.rglob("*")):
            if not p.is_file() or p.name == "platform.env" or "__pycache__" in p.parts or ".DS_Store" in p.name:
                continue
            rel = p.relative_to(ROOT).as_posix()
            if rel.endswith("platform.env.example") or "/rendered/" in rel:
                continue
            try:
                text = p.read_text()
            except UnicodeDecodeError:
                continue
            if re.search(rf"(?<![A-Za-z0-9]){re.escape(region)}(?![A-Za-z0-9])|(?<![A-Za-z0-9]){code}(?![A-Za-z0-9])", text):
                leaks.append(rel)
    assert not leaks, f"the deployment region/short code leaks outside the config: {leaks}"


# ------------------------------------------------------------------------------------------------ vocabulary (finding 7)
PRODUCT_EXTRA = [
    (r"(?i)(?<![a-z])" + "la" + r"b(?![a-z])", "environment identity (also inside identifiers)"),
    (r"flash-?" + r"rod-?plan", "internal plan name"),
]
TEST_TREE_EXTRA = [(r"\bP-" + r"37\b", "internal rule id")]


def _vocabulary_patterns(rel: str) -> list[tuple[str, str]]:
    return list(e_guards.VOCABULARY) + PRODUCT_EXTRA + (TEST_TREE_EXTRA if _is_test_tree(rel) else [])


def _vocabulary_hits(rel: str, text: str) -> list[str]:
    pats = _vocabulary_patterns(rel)
    out = []
    for pat, what in pats:
        out += [f"path: {what} {m.group(0)!r}" for m in re.finditer(pat, rel)]
        out += [f"{text.count(chr(10), 0, m.start()) + 1}: {what} {m.group(0)!r}" for m in re.finditer(pat, text)]
    return out


def test_vocabulary_covers_paths_and_test_trees() -> None:
    bad = {}
    for rel in _files():
        if rel in DENYLIST_FILES:
            continue
        hits = _vocabulary_hits(rel, _text(rel))
        if hits and not e_guards.as_drawn(rel, _text(rel), _vocabulary_patterns(rel)):
            bad[rel] = hits
    shown = "\n".join(f"  {p} ({len(h)}): {h[:2]}" for p, h in sorted(bad.items())[:80])
    assert not bad, (
        f"{len(bad)} tracked file(s) name internal things in content or path (only DENYLIST_FILES are exempt):\n{shown}"
    )


def test_denylist_allowlist_is_documented_minimal_and_not_stale() -> None:
    doc = __doc__ or ""
    for rel, why in DENYLIST_FILES.items():
        assert (ROOT / rel).is_file(), f"stale allowlist entry (file gone): {rel}"
        assert rel.startswith("tests/publish/") or rel == "tests/terraform/test_tree_layout.py", rel
        assert why and rel in (Path(__file__).read_text()), rel
        assert _vocabulary_hits(rel, _text(rel)) or _hits(ORIGINAL_REGION, rel, _text(rel)), (
            f"{rel} no longer needs its allowlist entry: remove it"
        )
    assert "DENYLIST_FILES" in doc


def test_this_guard_file_spells_forbidden_words_with_fragments() -> None:
    text = Path(__file__).read_text()
    own = [w for pat in PROCESS_WORDING.values() for w in re.findall(pat, text)]
    assert not own, f"{THIS} must not carry what it forbids: {own[:3]}"


# ------------------------------------------------------------------------------------------------ HF_TOKEN (8, 9)
def test_platform_env_example_does_not_send_the_token_to_sample_env() -> None:
    text = (ROOT / "manifests/config/platform.env.example").read_text()
    assert "HF_TOKEN" in text, "keep the warning that the token never goes in this file"
    assert not re.search(r"(?s)HF_TOKEN.{0,400}sample\.env|sample\.env.{0,400}HF_TOKEN", text), (
        "the token lives in the process environment only; sample.env is generated and is not a place for it"
    )


def test_gen_env_keeps_hf_token_forbidden() -> None:
    spec = importlib.util.spec_from_file_location("gen_env_f_guard", ROOT / "scripts/gen_env.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["gen_env_f_guard"] = mod
    spec.loader.exec_module(mod)
    assert "HF_TOKEN" in mod.FORBIDDEN_KEYS
    assert "HF_TOKEN" not in (ROOT / "manifests/config/platform.env.example").read_text().replace("# Never in this file: HF_TOKEN", "")


# ------------------------------------------------------------------------------------------------ dashboards (10)
def test_dashboards_tests_skip_without_a_dynamo_checkout(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The three tests that read tag v1.5.1 (test_allowlist, test_vendored x2) go through ``_lib.git_show``."""
    monkeypatch.setenv("DYNAMO_REPO", str(tmp_path / "no-dynamo-here"))
    spec = importlib.util.spec_from_file_location("dash_lib_f_guard", ROOT / "tests/dashboards/_lib.py")
    assert spec and spec.loader
    lib = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(lib)
    with pytest.raises(pytest.skip.Exception) as excinfo:
        lib.git_show(lib.CATALOG_PATH)
    assert "DYNAMO_REPO" in str(excinfo.value), "the skip reason names DYNAMO_REPO"


def test_tests_readme_says_dashboards_skip() -> None:
    text = (ROOT / "tests/README.md").read_text()
    bullet = re.search(r"(?s)`DYNAMO_REPO`.*?(?=\n- |\n\n|\Z)", text)
    assert bullet, "tests/README.md documents DYNAMO_REPO"
    assert "tests/dashboards" in bullet.group(0) and not re.search(r"(?i)\bfail", bullet.group(0)), bullet.group(0)


# ------------------------------------------------------------------------------------------------ event name (11)
def test_event_name_is_confined_to_the_documented_files() -> None:
    holders = {r for r in _files() if "KubeCon" in _text(r) and r != THIS and r not in DENYLIST_FILES}
    holders = {r for r in holders if not _is_test_tree(r)}
    assert holders <= EVENT_NAME_FILES, f"'KubeCon' outside the documented files: {sorted(holders - EVENT_NAME_FILES)}"
    for rel in EVENT_NAME_FILES:
        assert (ROOT / rel).is_file(), rel
        assert not _vocabulary_hits(rel, _text(rel)), f"{rel} carries an internal identifier besides the event name"


# ------------------------------------------------------------------------------------------------ ruff (12)
def test_no_unused_noqa_in_tracked_python() -> None:
    ruff = shutil.which("ruff")
    if ruff is None:
        pytest.skip("ruff not installed")
    files = [r for r in _files() if r.endswith(".py")]
    proc = lib_proc.run(
        [ruff, "check", "--no-cache", "--extend-select", "RUF100", "--output-format", "concise", *files],
        capture_output=True, text=True, check=False, cwd=ROOT, env={**os.environ, "RUFF_NO_CACHE": "1"},
    )
    unused = [ln for ln in proc.stdout.splitlines() if "RUF100" in ln]
    assert not unused, "unused noqa directives:\n" + "\n".join(unused)


# ------------------------------------------------------------------------------------------------ plan-e3 pin (13)
def test_e4_to_e7_pin_the_expanded_plan_e3() -> None:
    import hashlib

    sys.path.insert(0, str(ROOT / "tools/matrix_driver"))
    try:
        import render_driver as rd
    finally:
        sys.path.remove(str(ROOT / "tools/matrix_driver"))
    env = rd.load_platform_env(ROOT / "manifests/config/platform.env")
    plans = ROOT / "plans/w2-long-context"
    e3 = hashlib.sha256(rd.expand_plan((plans / "plan-e3.txt").read_text(), env).encode()).hexdigest()
    for name in ("plan-e4", "plan-e5", "plan-e6", "plan-e7"):
        pinned = re.findall(r"e3_plan_sha256=([0-9a-f]{64})", (plans / f"{name}.txt").read_text())
        assert pinned == [e3], f"{name} pins {pinned}, the expanded plan-e3 is {e3} (re-pin after editing plan-e3 or the config)"


def test_matrix_driver_readme_explains_the_plan_e3_pin() -> None:
    text = (ROOT / "tools/matrix_driver/README.md").read_text()
    sentences = re.split(r"(?<=[.!?])\s+", text)
    ok = [s for s in sentences if "plan-e3" in s and "platform.env" in s and re.search(r"(?i)re-?pin", s) and "plan-e7" in s]
    assert ok, (
        "document in one sentence that plan-e4..plan-e7 pin the sha256 of plan-e3 AFTER ${...} expansion from "
        "platform.env, so editing plan-e3 or changing the config requires re-pinning them (plan-e7 included)"
    )
