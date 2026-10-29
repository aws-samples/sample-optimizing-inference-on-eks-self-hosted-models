# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""M1: a fresh ``git clone`` runs the suite with no untracked file.

Builds a scratch clone (``git clone`` of this repository, so tags and history are real), overlays the CURRENT
tracked + not-ignored files of this working tree (so uncommitted work is verified too) and removes everything that
is deleted here.  The clone has no ``manifests/config/platform.env``, no ``config/sample.env`` and no
``HF_TOKEN``/``PLATFORM_ENV``/``MANIFESTS_DIR`` in the environment.  The publish-critical suites are then run in it:

* zero failures and zero errors (a missing generated file must SKIP with a reason, never fail),
* every skip has a reason,
* the publish-critical modules (test_95, 410, 411, 412, 420) are not skipped wholesale.

``CLEAN_CLONE_SCOPE=full`` runs everything under ``tests/`` plus the tool suites (about 10 minutes);
the default scope is the publish-critical subset.  ``SKIP_CLEAN_CLONE=1`` disables the test.
"""

from __future__ import annotations

import os
import shutil
from collections import Counter
from collections.abc import Iterator
from pathlib import Path

import lib_proc
import lib_xml
import pytest
import sanitize_scan as S

REPO = S.REPO_ROOT

CRITICAL_MODULES = (
    "test_95_region_config", "test_410_trackq_layout_and_config", "test_411_trackq_cells_vs_reference_and_knobs",
    "test_412_trackq_rendered_drift_and_goldens", "test_420_examples",
)
CRITICAL_SCOPE = [
    "tests/manifests/test_95_region_config.py", "tests/manifests/test_410_trackq_layout_and_config.py",
    "tests/manifests/test_411_trackq_cells_vs_reference_and_knobs.py", "tests/manifests/test_412_trackq_rendered_drift_and_goldens.py",
    "tests/manifests/test_420_examples.py", "tests/manifests/test_190_prepull.py", "tests/manifests/test_290_matrix_driver_layout.py",
    "tests/manifests/test_300_demo_rerender.py", "tests/scripts/test_108_w3_16k_workload.py", "tests/scripts/test_132_q38_prefetch.py",
    "tests/scripts/test_140_gen_env.py", "tests/scripts/test_152_stage_model_hygiene.py", "tests/terraform",
    "tests/publish/test_sanitization_guards.py", "tests/publish/test_clean_clone_config.py",
]
FULL_SCOPE = ["tests/manifests", "tests/scripts", "tests/terraform", "tests/publish/test_sanitization_guards.py",
              "tests/publish/test_clean_clone_config.py"]
MAX_SKIPPED_FRACTION_IN_CRITICAL = 0.05  # cases that need inputs outside the repository are the only expected skips
MIN_PASSED = {"critical": 600, "full": 3000}


def _git(*args: str, cwd: Path) -> str:
    return lib_proc.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


def _dynamo_repo() -> Path | None:
    for cand in (os.environ.get("DYNAMO_REPO"), str(REPO.parent / "dynamo")):  # same default as lib_manifests
        if cand and (Path(cand) / ".git").exists():
            return Path(cand)
    return None


def _overlay_working_tree(clone: Path) -> None:
    """Make the clone's files equal to this working tree's tracked + untracked-not-ignored files."""
    mine = set(S.tracked_files()) | {
        p for p in _git("ls-files", "-z", "--others", "--exclude-standard", cwd=REPO).split("\0") if p
    }
    theirs = {p for p in _git("ls-files", "-z", cwd=clone).split("\0") if p}
    for rel in theirs - mine:
        (clone / rel).unlink(missing_ok=True)
    for rel in mine:
        src, dst = REPO / rel, clone / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        if dst.is_symlink() or dst.exists():
            dst.unlink()
        if src.is_symlink():
            os.symlink(os.readlink(src), dst)
        else:
            shutil.copy2(src, dst)


@pytest.fixture(scope="module")
def clean_clone_report(tmp_path_factory: pytest.TempPathFactory) -> Iterator[lib_xml.Element]:
    if os.environ.get("CLEAN_CLONE_CHILD"):
        pytest.skip("already running inside the clean-clone child")
    if os.environ.get("SKIP_CLEAN_CLONE"):
        pytest.skip("SKIP_CLEAN_CLONE is set")
    dynamo = _dynamo_repo()
    if dynamo is None:
        pytest.skip("the CRD tests need a Dynamo checkout at v1.5.1: set DYNAMO_REPO (not available here)")
    work = tmp_path_factory.mktemp("clean-clone")
    clone = work / "clone"
    lib_proc.run(["git", "clone", "--quiet", "--no-hardlinks", str(REPO), str(clone)], check=True)
    _overlay_working_tree(clone)
    for untracked in ("manifests/config/platform.env", "config/sample.env"):
        assert not (clone / untracked).exists(), f"{untracked} must not exist in a fresh clone"

    scope = FULL_SCOPE if os.environ.get("CLEAN_CLONE_SCOPE") == "full" else CRITICAL_SCOPE
    junit = work / "junit.xml"
    env = {k: v for k, v in os.environ.items() if k not in {"PLATFORM_ENV", "MANIFESTS_DIR", "SCRIPTS_DIR", "HF_TOKEN", "KUBE_CONTEXT"}}
    env.update({"CLEAN_CLONE_CHILD": "1", "DYNAMO_REPO": str(dynamo), "PYTHONDONTWRITEBYTECODE": "1"})
    cmd = ["uv", "run", "--with", "pytest", "--with", "pytest-xdist", "--with", "pyyaml", "--with", "jsonschema",
           "pytest", "-q", "-n", "6", "-p", "no:cacheprovider", f"--junitxml={junit}", "-rs", *scope]
    proc = lib_proc.run(cmd, check=False, cwd=clone, env=env, capture_output=True, text=True, timeout=1500)
    (work / "stdout.txt").write_text(proc.stdout + proc.stderr)
    assert junit.is_file(), f"the clean-clone pytest run produced no report:\n{(proc.stdout + proc.stderr)[-3000:]}"
    root = lib_xml.parse(junit)
    root.set("log_tail", (proc.stdout + proc.stderr)[-3000:])
    yield root
    shutil.rmtree(work, ignore_errors=True)


def _cases(root: lib_xml.Element) -> list[lib_xml.Element]:
    return list(root.iter("testcase"))


def test_clean_clone_has_no_failures_or_errors(clean_clone_report: lib_xml.Element) -> None:
    bad = []
    for c in _cases(clean_clone_report):
        problem = c.find("failure") if c.find("failure") is not None else c.find("error")
        if problem is not None:
            bad.append((f"{c.get('classname')}::{c.get('name')}", (problem.get("message") or "").strip().splitlines()[:2]))
    lines = [f"{name}: {' | '.join(msg)[:300]}" for name, msg in bad[:25]]
    assert not bad, (
        f"{len(bad)} test(s) fail in a fresh clone (a test that needs a generated file must skip with a reason):\n"
        + "\n".join(lines)
    )


def test_clean_clone_skips_always_carry_a_reason(clean_clone_report: lib_xml.Element) -> None:
    skipped = [c for c in _cases(clean_clone_report) if c.find("skipped") is not None]
    reasons = Counter((c.find("skipped").get("message") or "").strip() for c in skipped)  # type: ignore[union-attr]
    assert "" not in reasons, f"{reasons['']} skipped test(s) without a reason"


def test_clean_clone_ran_a_real_number_of_tests(clean_clone_report: lib_xml.Element) -> None:
    passed = [c for c in _cases(clean_clone_report) if c.find("skipped") is None and c.find("failure") is None and c.find("error") is None]
    floor = MIN_PASSED["full" if os.environ.get("CLEAN_CLONE_SCOPE") == "full" else "critical"]
    assert len(passed) >= floor, f"only {len(passed)} tests passed in the clean clone (floor {floor})"


@pytest.mark.parametrize("module", CRITICAL_MODULES)
def test_publish_critical_modules_are_not_skipped_wholesale(clean_clone_report: lib_xml.Element, module: str) -> None:
    cases = [c for c in _cases(clean_clone_report) if (c.get("classname") or "").endswith(module)]
    assert cases, f"{module} did not run in the clean clone"
    skipped = [c for c in cases if c.find("skipped") is not None]
    assert len(skipped) <= MAX_SKIPPED_FRACTION_IN_CRITICAL * len(cases), (
        f"{module}: {len(skipped)}/{len(cases)} skipped in the clean clone"
    )
