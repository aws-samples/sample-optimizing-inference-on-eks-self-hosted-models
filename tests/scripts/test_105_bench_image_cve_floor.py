# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Bench image rebuild: CVE floor for the pip layer, held by exact pins.

The ECR scan of bench@sha256:ea15ab74... reported CRITICAL GHSA-6q48-6c7w-9gw2 (certifi 2026.10.18, fixed in
2026.10.19) and CVE-2026-112403 (pillow 12.3.0, fixed in 13.0.0).  Both arrive transitively through
``pip install aiperf==0.13.0 demo_bench[plot]``; the image installs from PyPI at build time and no lock file
drives that RUN (tools/demo_bench/uv.lock is not used by the Dockerfile and has no certifi entry), so the
Dockerfile itself must pin both packages to one exact version at or above the fixed release (an open range would
make the image non-reproducible) and fail the build if the installed version is below the floor.  The Dockerfile
is parsed as text only (no image build).  Override the path with ``BENCH_DOCKERFILE``.

The Trivy 0.69.3 scan of an image built from main at d91d6cf (DB 2026-10-28) added two more contracts, below: pip
leaves the final image (its vendored libraries carried the only CRITICAL and five HIGH) and kubectl is pinned to a
verified upstream build whose Go toolchain has the stdlib fixes; the "Accepted scan findings" header names the scan
it quotes.
"""

from __future__ import annotations

import ast
import json
import os
import re
from pathlib import Path
from typing import Any, Iterator

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
DOCKERFILE = Path(os.environ.get("BENCH_DOCKERFILE", REPO_ROOT / "tools" / "bench-image" / "Dockerfile"))
FLOORS = {"certifi": (2026, 10, 19), "pillow": (13, 0, 0)}
GOLDEN_DRIVER_OBJECTS = REPO_ROOT / "tools" / "matrix_driver" / "tests" / "fixtures" / "golden_driver_objects.json"
MATRIX_DRIVER = REPO_ROOT / "tools" / "matrix_driver" / "matrix_driver.py"
BENCH_DEPLOYMENT = REPO_ROOT / "manifests" / "bench" / "deployment.yaml"
CLUSTER_VARIABLES = REPO_ROOT / "terraform" / "cluster" / "variables.tf"
BENCH_IMAGE_REF = "${BENCH_IMAGE}"
PIP_CALL = re.compile(r"\bpip3?\b")


def _instructions() -> list[str]:
    assert DOCKERFILE.is_file(), f"missing {DOCKERFILE}"
    joined: list[str] = []
    buf = ""
    for raw in DOCKERFILE.read_text().splitlines():
        if not buf and raw.lstrip().startswith("#"):
            continue
        line = raw.rstrip()
        if line.endswith("\\"):
            buf += line[:-1] + " "
            continue
        joined.append((buf + line).strip())
        buf = ""
    return [i for i in joined if i]


def _ver(text: str) -> tuple[int, ...]:
    return tuple(int(p) for p in text.split("."))


def _pip_install_runs() -> list[str]:
    return [i for i in _instructions() if i.upper().startswith("RUN ") and re.search(r"pip\s+install", i)]


@pytest.mark.parametrize("pkg", sorted(FLOORS))
def test_pip_install_pins_the_package_exactly_at_or_above_the_fixed_version(pkg: str) -> None:
    """The RUN that installs aiperf pins <pkg> to ONE exact version (`"<pkg>==<v>"`, an install argument or the uv
    override line) at or above the fixed release, in the SAME resolution as aiperf/demo_bench, and holds no open range
    for it: the resolver cannot pick the vulnerable release and the image is reproducible."""
    floor = FLOORS[pkg]
    runs = [r for r in _pip_install_runs() if "aiperf" in r]
    assert runs, "no pip install RUN that installs aiperf"
    exact_re = re.compile(rf"[\"']{pkg}==([0-9]+(?:\.[0-9]+)*)[\"']", re.IGNORECASE)
    range_re = re.compile(rf"\b{pkg}\s*(?:>=|<=|~=|!=|>|<)\s*[0-9]", re.IGNORECASE)
    exact = [m.group(1) for r in runs for m in exact_re.finditer(r)]
    ranges = [m.group(0) for r in runs for m in range_re.finditer(r)]
    assert exact, f"{pkg} is not pinned in the aiperf install (needs '{pkg}==<release >= {'.'.join(map(str, floor))}>')"
    assert not ranges, f"{pkg} must be pinned with == only, not a range: {ranges}"
    assert len(set(exact)) == 1, f"{pkg} is pinned to more than one version: {sorted(set(exact))}"
    assert _ver(exact[0]) >= floor, f"{pkg}=={exact[0]} is below the fixed release {'.'.join(map(str, floor))}"


@pytest.mark.parametrize("pkg", sorted(FLOORS))
def test_build_fails_if_the_installed_version_is_below_the_floor(pkg: str) -> None:
    """The Dockerfile asserts the installed version of <pkg> after pip (importlib.metadata) so a resolver that
    ignores the specifier fails the build instead of shipping the CRITICAL again."""
    text = "\n".join(_pip_install_runs() + [i for i in _instructions() if i.upper().startswith("RUN ") and "importlib.metadata" in i])
    assert re.search(rf"version\(\s*['\"]{pkg}['\"]\s*\)", text, re.IGNORECASE), f"no build-time check of m.version('{pkg}')"
    nums = ".".join(map(str, FLOORS[pkg]))
    assert nums in text or str(FLOORS[pkg]) in text, f"the build-time check of {pkg} does not compare with the floor {nums}"


def test_demo_bench_lock_does_not_regress_pillow() -> None:
    """Guard: the demo_bench lock (dev/CI resolution) must not hold the vulnerable pillow either."""
    lock = (REPO_ROOT / "tools" / "demo_bench" / "uv.lock").read_text()
    m = re.search(r'name = "pillow"\nversion = "([0-9.]+)"', lock)
    assert m, "pillow not in uv.lock"
    assert _ver(m.group(1)) >= FLOORS["pillow"]


# ----------------------------------------------------------------------------- pip: the vendored libraries
# Trivy reads pip's own SBOM (pip/_vendor/bom.cdx.json) and reported the libraries vendored by pip 26.2.1: certifi
# 2026.6.17 (CRITICAL GHSA-6q48-6c7w-9gw2, fixed in 2026.10.18), urllib3 2.7.0 (CVE-2026-97687, CVE-2026-97689), msgpack
# 1.1.2 (CVE-2026-114613, GHSA-6v7p-g79w-8964) and setuptools 70.3.0 (CVE-2025-47273), while the top-level certifi,
# urllib3 and setuptools of the image are fixed.  pip cannot leave the image: the matrix driver, which runs in it,
# installs the mounted demo_bench wheel with pip at the start of every run.  The newest pip on PyPI on 2026-10-28, 26.2.2,
# vendors a fixed certifi; its other vendored libraries are unchanged (read from an install: pip/_vendor/vendor.txt and
# bom.cdx.json).  The values below are what that install showed; a pip release not in the table has not been checked.
PIP_VERIFIED = {
    "26.2.2": {"certifi": "2026.10.19", "urllib3": "2.7.0", "msgpack": "1.1.2", "setuptools": "70.3.0"},
}


def _final_stage() -> list[str]:
    ins = _instructions()
    froms = [n for n, i in enumerate(ins) if i.upper().startswith("FROM ")]
    assert froms, "no FROM"
    return ins[froms[-1]:]


def _pip_pin() -> str:
    pins = sorted({m.group(1) for i in _final_stage() for m in re.finditer(r"[\"']pip==([0-9.]+)[\"']", i)})
    assert len(pins) == 1, f"the final stage must pin pip to exactly one release, got {pins}"
    return pins[0]


def test_pip_is_pinned_to_a_checked_release_whose_vendored_certifi_is_fixed() -> None:
    pin = _pip_pin()
    assert pin in PIP_VERIFIED, f"pip=={pin}: read its pip/_vendor/vendor.txt and bom.cdx.json, then add it to PIP_VERIFIED"
    vendored = PIP_VERIFIED[pin]["certifi"]
    assert _ver(vendored) >= FLOORS["certifi"], f"pip=={pin} vendors certifi {vendored}, below the fixed release"


def test_the_build_asserts_the_pip_release_and_its_vendored_certifi() -> None:
    """The build fails if pip is not the pinned release or if pip's own certifi (pip._vendor.certifi) is below the floor:
    an upgrade of pip inside the build cannot bring the CRITICAL back unnoticed."""
    pin = _pip_pin()
    text = "\n".join(i for i in _final_stage() if i.upper().startswith("RUN "))
    assert re.search(rf"version\(\s*['\"]pip['\"]\s*\)\s*==\s*['\"]{re.escape(pin)}['\"]", text), f"assert m.version('pip') == '{pin}'"
    floor = ".".join(map(str, FLOORS["certifi"]))
    check = [i for i in text.splitlines() if "pip._vendor" in i and "certifi" in i]
    assert check, "no build-time check of pip._vendor.certifi"
    assert re.search(rf"__version__[^\n]*>=\s*V\(\s*['\"]{re.escape(floor)}['\"]\s*\)", check[0]), (
        f"compare pip._vendor.certifi.__version__ with the floor {floor}")


def _containers(obj: Any) -> Iterator[dict[str, Any]]:
    """Every container and init container of a rendered object tree."""
    if isinstance(obj, dict):
        for key in ("containers", "initContainers"):
            for c in obj.get(key) or []:
                if isinstance(c, dict) and "image" in c:
                    yield c
        for value in obj.values():
            yield from _containers(value)
    elif isinstance(obj, list):
        for value in obj:
            yield from _containers(value)


def _runs_pip(container: dict[str, Any]) -> bool:
    return bool(PIP_CALL.search(json.dumps([container.get("command"), container.get("args")])))


def test_no_bench_image_container_command_calls_pip() -> None:
    """No object the matrix driver renders (golden fixture) that runs the bench image calls pip in its command, and the
    bench Deployment does not either: the network pip installs of the repository run in other images (the capture
    container installs its pins in the Playwright image)."""
    objects = json.loads(GOLDEN_DRIVER_OBJECTS.read_text(encoding="utf-8"))
    containers = list(_containers(objects))
    bench = [c for c in containers if c["image"] == BENCH_IMAGE_REF]
    assert bench, f"no {BENCH_IMAGE_REF} container in {GOLDEN_DRIVER_OBJECTS.name}"
    assert any(_runs_pip(c) for c in containers if c["image"] != BENCH_IMAGE_REF), (
        "sanity: the capture container's pip call is no longer found, update this check")
    bad = [c["name"] for c in bench if _runs_pip(c)]
    assert not bad, f"bench-image containers call pip: {bad}"
    assert not PIP_CALL.search(BENCH_DEPLOYMENT.read_text(encoding="utf-8")), f"{BENCH_DEPLOYMENT.name} calls pip"


def test_the_driver_runs_pip_offline_or_in_another_image() -> None:
    """Every function of matrix_driver.py that calls pip either builds a CAPTURE_IMAGE container or runs pip with
    --no-index and --no-deps (the offline install of the mounted, sha256-checked demo_bench wheel).  That is the only
    use of pip in the bench image at run time, so pip's vendored network stack (urllib3, certifi) and its HTTP cache
    serialiser (msgpack) are not reached there."""
    tree = ast.parse(MATRIX_DRIVER.read_text(encoding="utf-8"))
    callers = []
    for fn in (n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)):
        strings = [s.value for s in ast.walk(fn) if isinstance(s, ast.Constant) and isinstance(s.value, str)]
        if "pip" in strings or any(re.search(r"-m\s+pip\b|\bpip3?\s+install\b", s) for s in strings):
            names = {n.id for n in ast.walk(fn) if isinstance(n, ast.Name)}
            offline = {"--no-index", "--no-deps"} <= set(strings)
            callers.append((fn.name, "CAPTURE_IMAGE" in names or offline))
    assert len(callers) >= 2, f"sanity: expected the capture container and the wheel install, found {callers}"
    assert all(ok for _, ok in callers), f"pip called with network access in the driver pod: {callers}"


# ----------------------------------------------------------------------------- kubectl: a verified build with the Go fixes
# kubectl v1.35.9 was built with go1.26.8: Trivy reported the stdlib HIGHs CVE-2026-111943 (fixed in go1.26.10) and
# CVE-2026-78667, CVE-2026-78669, CVE-2026-97031 (fixed in go1.26.9).  v1.35.10 is the first 1.35 patch built with
# go1.26.10 (`go version -m`, both archs).  The values below were verified on 2026-10-28: sha256 of the binary downloaded
# from https://dl.k8s.io/release/<v>/bin/linux/<arch>/kubectl equal to the published <...>/kubectl.sha256 for amd64 and
# arm64.  A version that is not in this table has not been verified: verify it the same way and add it.
KUBECTL_FLOOR = (1, 35, 10)
KUBECTL_VERIFIED = {
    "v1.35.10": {
        "go": "go1.26.10",
        "amd64": "c49a28f1f4efc4d475ed05f47f2ce8e563bf7bef69ed77fe1b924ea6d83b6327",
        "arm64": "283f1be6a46b3695b625e25030106b8ffe18bd0d3a900f5b65c259ca407f04f5",
    },
}


def _arg(name: str) -> str:
    m = re.search(rf"^ARG {name}=(\S+)$", DOCKERFILE.read_text(), re.MULTILINE)
    assert m, f"ARG {name}=<value> missing"
    return m.group(1)


def _cluster_minor() -> int:
    m = re.search(r'variable "kubernetes_version"\s*\{[^}]*?default\s*=\s*"1\.([0-9]+)"', CLUSTER_VARIABLES.read_text(), re.DOTALL)
    assert m, "terraform/cluster/variables.tf: kubernetes_version default"
    return int(m.group(1))


def test_kubectl_is_at_or_above_the_go_fix_floor_and_within_the_version_skew() -> None:
    version = _arg("KUBECTL_VERSION")
    parts = _ver(version.removeprefix("v"))
    assert parts >= KUBECTL_FLOOR, f"kubectl {version} is below v{'.'.join(map(str, KUBECTL_FLOOR))} (built with go1.26.8)"
    assert abs(parts[1] - _cluster_minor()) <= 1, f"kubectl {version} is outside the +/-1 minor skew of the cluster 1.{_cluster_minor()}"


def test_kubectl_checksums_are_the_verified_upstream_values() -> None:
    version = _arg("KUBECTL_VERSION")
    assert version in KUBECTL_VERIFIED, f"kubectl {version} is not a verified build: check sha256 and `go version -m`, add it"
    want = KUBECTL_VERIFIED[version]
    assert _arg("KUBECTL_SHA256_AMD64") == want["amd64"], "KUBECTL_SHA256_AMD64 is not the published dl.k8s.io value"
    assert _arg("KUBECTL_SHA256_ARM64") == want["arm64"], "KUBECTL_SHA256_ARM64 is not the published dl.k8s.io value"


# ----------------------------------------------------------------------------- the header quotes the scan it relies on
def _accepted_findings() -> str:
    """The "Accepted scan findings" comment block of the Dockerfile header, up to the first instruction, flattened."""
    lines = DOCKERFILE.read_text().splitlines()
    start = next((n for n, ln in enumerate(lines) if "Accepted scan findings" in ln), None)
    assert start is not None, "the Dockerfile header has no 'Accepted scan findings' block"
    end = next(n for n in range(start, len(lines)) if not lines[n].startswith("#"))
    return " ".join(ln.lstrip("#").strip() for ln in lines[start:end])


def test_accepted_findings_name_the_scanner_the_db_and_the_kubectl_build() -> None:
    text = _accepted_findings()
    version = _arg("KUBECTL_VERSION")
    assert re.search(r"\bTrivy v?0\.[0-9]+\.[0-9]+\b", text), "name the scanner and its version (e.g. Trivy 0.69.3)"
    assert re.search(r"\bDB\b[^.;]*\b20[0-9]{2}-[0-9]{2}-[0-9]{2}\b", text), "name the vulnerability DB date"
    assert version in text, f"name the kubectl release the counts are for ({version})"
    assert KUBECTL_VERIFIED.get(version, {}).get("go", "go?") in text, "name the Go toolchain of that kubectl build"
    stale = sorted(set(re.findall(r"\bv1\.35\.[0-9]+\b", text)) - {version})
    assert not stale, f"the block still quotes another kubectl 1.35 release: {stale}"


def test_accepted_findings_cover_the_libraries_vendored_by_the_pinned_pip() -> None:
    """The block says that the remaining python-pkg findings are pip's vendored copies, names the pinned pip release and
    the vendored versions it accepts, and why pip stays (the offline wheel install of the driver)."""
    text = _accepted_findings()
    pin = _pip_pin()
    assert "pip/_vendor" in text and f"pip {pin}" in text, f"name pip {pin} and its pip/_vendor libraries"
    for lib in ("urllib3", "msgpack", "setuptools"):
        version = PIP_VERIFIED.get(pin, {}).get(lib, "?")
        assert f"{lib} {version}" in text, f"name the vendored {lib} {version} that stays with pip {pin}"
    assert "--no-index" in text, "say that pip runs offline (--no-index) at run time"
