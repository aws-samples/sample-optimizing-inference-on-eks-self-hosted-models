# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""RED tests: non-EFA instance types carry ``efa_maximum_interfaces = null`` and Terraform >= 1.12 is required.

The ``aws_ec2_instance_type`` data source returns ``efa_maximum_interfaces = null`` when ``efa_supported`` is false.
Terraform < 1.12 does not short-circuit ``||`` in a validation, so the module failed to plan for g5/g6e. The fix raises
the minimum version to 1.12 (and the docs recommend the latest, 1.17) and guards the expression with ``coalesce``.
"""

from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
TERRAFORM = REPO / "terraform"
FIXTURES = TERRAFORM / "cluster" / "tests" / "fixtures"
MIN_MINOR = 12
RECOMMENDED = "1.17"


def _live_tf_files() -> list[Path]:
    """All .tf files of the live stacks (the ``.terraform/`` init dirs, which hold third-party registry modules, are
    excluded)."""
    return sorted(p for p in TERRAFORM.rglob("*.tf") if not any(part.startswith(".") for part in p.relative_to(TERRAFORM).parts))


def _required_versions() -> list[tuple[Path, str]]:
    out = []
    for path in _live_tf_files():
        for m in re.finditer(r'(?m)^\s*required_version\s*=\s*"([^"]+)"', path.read_text()):
            out.append((path, m.group(1)))
    return out


def test_every_root_and_module_declares_required_version() -> None:
    declared = {p.parent.relative_to(TERRAFORM).as_posix() for p, _ in _required_versions()}
    assert {"cluster", "bootstrap", "cluster/modules/gpu-pools"} <= declared, f"missing required_version: {declared}"


@pytest.mark.parametrize(("path", "constraint"), _required_versions(), ids=lambda v: str(v) if isinstance(v, str) else v.parent.name)
def test_required_version_is_at_least_1_12(path: Path, constraint: str) -> None:
    m = re.fullmatch(r">=\s*1\.(\d+)(?:\.\d+)?", constraint.strip())
    assert m, f"{path}: unexpected constraint {constraint!r}"
    assert int(m.group(1)) >= MIN_MINOR, f"{path.relative_to(REPO)}: required_version {constraint!r} is below 1.{MIN_MINOR} (no || short-circuit before 1.12)"


@pytest.mark.parametrize("readme", [REPO / "README.md", TERRAFORM / "README.md"], ids=["README.md", "terraform/README.md"])
def test_readmes_recommend_the_latest_terraform(readme: Path) -> None:
    text = readme.read_text()
    assert re.search(rf"(?i)terraform[^\n]{{0,80}}{re.escape(RECOMMENDED)}|{re.escape(RECOMMENDED)}[^\n]{{0,80}}terraform", text), f"{readme.name} must name Terraform {RECOMMENDED} as recommended"
    assert not re.search(r"(?i)terraform\s*>=\s*1\.(9|10|11)\b", text), f"{readme.name} still documents a minimum below 1.12"


def test_non_efa_fixtures_hold_null_like_the_data_source() -> None:
    types = json.loads((FIXTURES / "instance-types.json").read_text())
    for name in ("g5.xlarge", "g6e.xlarge", "g6e.2xlarge", "c7i.large"):
        assert types[name]["efa_supported"] is False
        assert types[name]["efa_maximum_interfaces"] is None, f"{name}: the data source returns null, not 0, for non-EFA types"
    for name, rec in types.items():
        if rec["efa_supported"]:
            assert isinstance(rec["efa_maximum_interfaces"], int) and rec["efa_maximum_interfaces"] > 0, name


def test_capture_maps_missing_efa_info_to_null() -> None:
    spec = importlib.util.spec_from_file_location("capture_instance_types", FIXTURES / "capture_instance_types.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    base = {
        "VCpuInfo": {"DefaultVCpus": 4},
        "MemoryInfo": {"SizeInMiB": 16384},
        "ProcessorInfo": {"SupportedArchitectures": ["x86_64"]},
    }
    no_efa = mod.to_data_source({**base, "NetworkInfo": {"MaximumNetworkCards": 1, "EfaSupported": False}})
    assert no_efa["efa_maximum_interfaces"] is None
    efa = mod.to_data_source({**base, "NetworkInfo": {"MaximumNetworkCards": 8, "EfaSupported": True, "EfaInfo": {"MaximumEfaInterfaces": 8}}})
    assert efa["efa_maximum_interfaces"] == 8
