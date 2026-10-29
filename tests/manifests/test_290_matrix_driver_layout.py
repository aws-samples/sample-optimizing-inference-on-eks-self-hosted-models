# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Matrix driver ported into the repo (layout, parametrization, README).

The in-cluster driver that produced every recorded result lives in ``tools/matrix_driver`` (flat modules, as the
ConfigMap mounts them); its behaviour tests run offline on a fake cluster and sit in ``tools/matrix_driver/tests``
(run: ``uv run --with-requirements tests/requirements-test.txt pytest tools/matrix_driver/tests -q -p no:cacheprovider``).
This file pins
what the repo-level suites own:

* the files exist where the README says (tools/matrix_driver, plans/w3-16k = the W3-16K plans,
  plans/w2-long-context = plan-e3..e7, manifests/track-q-qwen38/driver; plan-w3-r50 is dropped);
* region / account / bucket / ECR host appear in none of them (only ``manifests/config/platform.env``; the driver
  image tags and digests, which are not region/account values, live in ``driver/images.env``), plans carry
  ``${RESULTS_BUCKET}`` placeholders;
* the DGDs are NOT copied: the driver renders them from ``manifests/track-q-qwen38/rendered``;
* the README documents the launch sequence in order: base -> watchdog -> (take preflight) -> plan Job;
* the ported suite was not trimmed.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

import lib_manifests as L

REPO = L.MANIFESTS.parent
TOOL = REPO / "tools" / "matrix_driver"
PLANS = REPO / "plans" / "w3-16k"
W2_PLANS = REPO / "plans" / "w2-long-context"
DRIVER_DIR = L.MANIFESTS / "track-q-qwen38" / "driver"

W2_PLAN_NAMES = ["plan-e3", "plan-e4", "plan-e5", "plan-e6", "plan-e7"]
W3_PLAN_NAMES = ["plan-w3-r10", "plan-w3-r10-cells", "plan-w3-r25", "plan-w3-r25-cells", "plan-w3-r32", "plan-w3-r32-cells"]
PLAN_NAMES = [*W2_PLAN_NAMES, *W3_PLAN_NAMES]
MODULES = ["matrix_driver.py", "watchdog.py", "fake_cluster.py", "dryrun_plan.py", "render_driver.py"]
PORTED_TESTS = ["test_critic_fixes.py", "test_failure_paths.py", "test_matrix_driver.py", "test_offline_pipeline.py",
                "test_v3_fixes.py", "test_v4_takes.py", "test_watchdog.py"]
PORTED_TEST_COUNT = 173  # def test_ methods of the original suite at q38-matrix-driver/4

REGION = re.compile(r"(?<![A-Za-z0-9])(?:us|eu|ap|sa|ca|me|af|il)-(?:north|south|east|west|central|northeast|southeast)-[0-9](?![A-Za-z0-9])")
ACCOUNT = re.compile(r"(?<![A-Za-z0-9])[0-9]{12}(?![A-Za-z0-9])")
ECR_HOST = re.compile(r"\.dkr\.ecr\.")
PROJECT_BUCKET = re.compile(r"(?:kubecon" + r"-dynamo|dynamo-sample)-[a-z]")
# any region short code (use2 of the placeholder config, the codes of earlier deployments)
SHORT = re.compile(r"(?<![A-Za-z0-9])(?:use|usw|euw|euc|eun|aps|apn|sae|cac)[0-9](?![A-Za-z0-9])")
S3_URI = re.compile(r"s3://([^/\s\"']+)")


def plan_file(name: str) -> Path:
    """plans/w2-long-context for plan-e3..e7, plans/w3-16k for the W3-16K plans."""
    return (W2_PLANS if name in W2_PLAN_NAMES else PLANS) / f"{name}.txt"


def owned_files() -> list[Path]:
    """Every file this port adds, except the golden fixtures (they hold ${TOKENS}, scanned separately) and caches."""
    roots = [TOOL, PLANS, W2_PLANS, DRIVER_DIR]
    out = []
    for root in roots:
        if root.is_dir():
            out += [p for p in sorted(root.rglob("*")) if p.is_file() and "__pycache__" not in p.parts
                    and p.name != ".DS_Store" and "fixtures" not in p.parts and p.suffix != ".whl"]
    return out


@pytest.mark.parametrize("name", MODULES)
def test_driver_module_exists(name: str) -> None:
    assert (TOOL / name).is_file(), f"tools/matrix_driver/{name} missing"


@pytest.mark.parametrize("name", ["capture.py", "compose.sh"])
def test_take_scripts_exist(name: str) -> None:
    assert (TOOL / "takes" / name).is_file(), f"tools/matrix_driver/takes/{name} missing"


@pytest.mark.parametrize("name", W3_PLAN_NAMES)
def test_shipped_w3_plan_exists(name: str) -> None:
    assert (PLANS / f"{name}.txt").is_file(), f"plans/w3-16k/{name}.txt missing"


@pytest.mark.parametrize("name", W2_PLAN_NAMES)
def test_shipped_w2_plan_exists(name: str) -> None:
    assert (W2_PLANS / f"{name}.txt").is_file(), f"plans/w2-long-context/{name}.txt missing"
    assert not (PLANS / f"{name}.txt").exists(), f"{name} must have moved out of plans/w3-16k"


def test_plan_dirs_hold_exactly_their_plans_and_r50_is_dropped() -> None:
    assert sorted(p.name for p in PLANS.iterdir()) == sorted(f"{n}.txt" for n in W3_PLAN_NAMES)
    assert sorted(p.name for p in W2_PLANS.iterdir()) == sorted(f"{n}.txt" for n in W2_PLAN_NAMES)
    assert not list((REPO / "plans").rglob("*r50*"))


def test_template_dir_has_readme_and_image_pins() -> None:
    assert (DRIVER_DIR / "README.md").is_file()
    assert (DRIVER_DIR / "images.env").is_file()


def test_tool_has_readme_and_pytest_config() -> None:
    assert (TOOL / "README.md").is_file()
    cfg = tomllib.loads((TOOL / "pyproject.toml").read_text())
    assert cfg["tool"]["pytest"]["ini_options"]["testpaths"] == ["tests"]


def test_cell_render_generator_is_not_ported() -> None:
    """DGDs come from manifests/track-q-qwen38/rendered; the original cell generator must not live on in the repo."""
    assert not (TOOL / "render_cells.py").exists()
    assert not list(TOOL.rglob("render_cells*"))


def test_no_dgd_copy_next_to_the_driver() -> None:
    for p in owned_files():
        assert not re.fullmatch(r"S[0-7]\.ya?ml", p.name), f"{p}: DGDs are rendered from track-q-qwen38/rendered"
        if p.suffix in {".yaml", ".yml"}:
            assert "DynamoGraphDeployment" not in p.read_text(), p


def test_ported_suite_was_not_trimmed() -> None:
    total = 0
    for name in PORTED_TESTS:
        path = TOOL / "tests" / name
        assert path.is_file(), f"tools/matrix_driver/tests/{name} missing"
        total += len(re.findall(r"^    def test_", path.read_text(), re.M))
    assert total >= PORTED_TEST_COUNT, f"{total} ported tests < the original {PORTED_TEST_COUNT}"


# --------------------------------------------------------------------------- no literals outside platform.env
def test_the_port_adds_files() -> None:
    assert len(owned_files()) >= 20, "tools/matrix_driver, plans/w3-16k and track-q-qwen38/driver are not populated yet"


@pytest.mark.parametrize("pattern", [REGION, ACCOUNT, ECR_HOST, PROJECT_BUCKET, SHORT], ids=["region", "account", "ecr-host", "project-bucket", "short-suffix"])
def test_no_region_account_registry_or_bucket_literal_in_the_port(pattern: re.Pattern[str]) -> None:
    files = owned_files()
    assert files
    for p in files:
        hits = pattern.findall(p.read_text(errors="ignore"))
        assert not hits, f"{p.relative_to(REPO)}: {hits[:2]} outside manifests/config/platform.env"


@pytest.mark.parametrize("name", PLAN_NAMES)
def test_plans_reference_the_results_bucket_only_through_the_placeholder(name: str) -> None:
    text = plan_file(name).read_text()
    buckets = set(S3_URI.findall(text))
    assert buckets == {"${RESULTS_BUCKET}"}, buckets
    assert set(re.findall(r"\$\{([A-Za-z0-9_]+)\}", text)) == {"RESULTS_BUCKET"}


def test_every_placeholder_key_exists_in_platform_env() -> None:
    env = {}
    for raw in (L.MANIFESTS / "config" / "platform.env").read_text().splitlines():
        if raw.strip() and not raw.startswith("#"):
            k, v = raw.split("=", 1)
            env[k] = v
    for name in PLAN_NAMES:
        for key in re.findall(r"\$\{([A-Za-z0-9_]+)\}", plan_file(name).read_text()):
            assert key in env, f"{name}: ${{{key}}} is not a platform.env key"


# --------------------------------------------------------------------------- image pins
def test_images_env_pins_the_two_driver_images_by_tag_and_digest_without_registry() -> None:
    env = {}
    for raw in (DRIVER_DIR / "images.env").read_text().splitlines():
        line = raw.strip()
        if line and not line.startswith("#"):
            assert re.fullmatch(r"[A-Z][A-Z0-9_]*=[^\s\"'#$`]*", line), raw
            k, v = line.split("=", 1)
            env[k] = v
    assert set(env) == {"MATRIX_BENCH_TAG", "MATRIX_BENCH_DIGEST", "MATRIX_FFMPEG_TAG", "MATRIX_FFMPEG_DIGEST"}
    assert env["MATRIX_BENCH_TAG"] == "1517a23" and env["MATRIX_FFMPEG_TAG"] == "ffmpeg-9.0.2-cli-ls86"
    for key in ("MATRIX_BENCH_DIGEST", "MATRIX_FFMPEG_DIGEST"):
        assert re.fullmatch(r"sha256:[0-9a-f]{64}", env[key]), key
    assert env["MATRIX_BENCH_DIGEST"] == "sha256:ff31929ce363efec1d24f603cee4ae47c5fc44374a6ec1d397cc25697750637a"
    assert env["MATRIX_FFMPEG_DIGEST"] == "sha256:742e17516dd4d7b76b939d774bd08f0f6b7cab8bce1c014b95ab2fb992fd0a1f"
    assert "latest" not in "".join(env.values())


# --------------------------------------------------------------------------- README: launch sequence
def readme() -> str:
    return (TOOL / "README.md").read_text()


def test_readme_documents_the_launch_sequence_in_order() -> None:
    text = readme()
    marks = ["render_driver.py", "00-base.yaml", "20-watchdog.yaml", "05-take-preflight.yaml", "--launch", "plan-e3"]
    idx = []
    for m in marks:
        assert m in text, f"README does not mention {m}"
        idx.append(text.index(m))
    assert idx == sorted(idx), f"README order must be base -> watchdog -> take preflight -> plan Job: {idx}"


def test_readme_documents_the_relative_hard_stop_and_the_plan_dirs() -> None:
    text = readme()
    for needle in ("--hard-stop-in", "--plans-dir", "plans/w2-long-context"):
        assert needle in text, f"README does not mention {needle}"
    assert "r50" not in text, "plan-w3-r50 was dropped: the README must not list it"


def test_readme_covers_the_inputs_an_operator_must_supply() -> None:
    text = readme()
    for needle in ("--wheel", "--cb-node", "--watchdog-windows", "--watchdog-schedule", "platform.env", "images.env",
                   "kubectl apply --server-side", "kubectl create -f", "dryrun_plan.py",
                   "pytest tools/matrix_driver/tests", "manifests/track-q-qwen38/rendered", "plans/w3-16k"):
        assert needle in text, f"README does not mention {needle}"


def test_readme_says_the_watchdog_is_applied_before_the_first_driver_job_and_names_every_plan() -> None:
    text = readme()
    assert re.search(r"watchdog[^\n]*(before|first)", text, re.I) or re.search(r"(before|first)[^\n]*watchdog", text, re.I)
    for name in PLAN_NAMES:
        assert name in text, f"README does not list {name}"


def test_readme_has_no_secret_values() -> None:
    for p in (TOOL / "README.md", DRIVER_DIR / "README.md"):
        text = p.read_text()
        assert not re.search(r"(?i)(password|secret|token)\s*[:=]\s*\S{8,}", text), p
        assert not re.search(r"AKIA[0-9A-Z]{16}", text)
