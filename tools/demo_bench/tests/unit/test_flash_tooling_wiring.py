# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""RED contract for the wiring, the README and the neutral defaults of the Flash tooling.

* Item (a): the tools brought back from 0ebf04a (``restore_probe``, ``trace_multiturn``,
  ``cost_ledger``, their CLI help and README sections) carry no gpt-oss / L4 / g6 wording: the
  sample runs on B200, so a copied example must not name another model or GPU.
* CLI: ``trace w2``, ``w2-gate``, ``slo-goodput`` and ``image-gate`` exist (``--help`` exits 0);
  ``restore-probe`` documents ``--flush-url`` and ``--noise-k``.
* README: one ``###`` section per new command, listing every flag of the contract.
* The new modules are stdlib only (the bench image carries no extra dependency).
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src" / "demo_bench"
README = (ROOT / "README.md").read_text(encoding="utf-8")
TESTS_README = (ROOT / "tests" / "README.md").read_text(encoding="utf-8")

FORBIDDEN = re.compile(r"gpt[-_ ]?oss|\bL4\b|gpu-l4|\bg6e?\.", re.IGNORECASE)
NEUTRAL_FILES = [
    SRC / "application" / "restore_probe.py",
    SRC / "application" / "trace_multiturn.py",
    SRC / "application" / "cost_ledger.py",
    SRC / "cli.py",
    ROOT / "README.md",
]
NEW_MODULES = ["trace_w2", "w2_gate", "slo_goodput", "image_gate"]

FLAGS: dict[str, list[str]] = {
    "trace w2": ["--out", "--sidecar", "--seed", "--sessions", "--turns", "--global-prefix-tokens",
                 "--entry-tokens", "--growth-tokens", "--rate", "--gap-sigma", "--populate-s",
                 "--cold-fraction", "--cold-min-tokens", "--cold-max-tokens", "--osl",
                 "--osl-file", "--osl-cap", "--max-isl", "--block-size", "--expect-sha256"],
    "w2-gate": ["--trace", "--sidecar", "--targets", "--gpu-tokens-per-target",
                "--l2-tokens-per-target", "--turn-duration-s", "--block-size", "--warmup-s",
                "--uncacheable-tokens", "--max-mamba-gpu", "--max-mamba-host", "--out"],
    "slo-goodput": ["--profile-export", "--sidecar", "--cold-prefill-tok-s", "--window-s",
                    "--ttft-s", "--itl-ms", "--cold-factor", "--itl-grid", "--ttft-grid",
                    "--n-boot", "--seed", "--alpha", "--accept-band", "--out"],
    "image-gate": ["--derived", "--control", "--accept-length-ratio", "--prefill-ratio", "--out"],
    "restore-probe": ["--flush-url", "--noise-k"],
}


def _sections() -> dict[str, str]:
    parts = re.split(r"^### ", README, flags=re.MULTILINE)
    return {p.split("\n", 1)[0].strip(): p for p in parts[1:]}


def _cli(argv: list[str]) -> int:
    from demo_bench.cli import main

    try:
        return int(main(argv))
    except SystemExit as exc:
        return int(exc.code or 0)


@pytest.mark.parametrize("path", NEUTRAL_FILES, ids=lambda p: p.name)
def test_no_other_model_or_gpu_in_the_restored_tools(path: Path) -> None:
    hits = [m.group(0) for m in FORBIDDEN.finditer(path.read_text(encoding="utf-8"))]
    assert hits == []


@pytest.mark.parametrize("command", ["trace w2", "w2-gate", "slo-goodput", "image-gate"])
def test_new_commands_have_help(command: str, capsys: pytest.CaptureFixture[str]) -> None:
    assert _cli([*command.split(), "--help"]) == 0
    help_text = capsys.readouterr().out
    for flag in FLAGS[command]:
        assert flag in help_text, flag


def test_restore_probe_help_lists_the_noise_floor_flags(capsys: pytest.CaptureFixture[str]) -> None:
    assert _cli(["restore-probe", "--help"]) == 0
    help_text = capsys.readouterr().out
    assert "--flush-url" in help_text and "--noise-k" in help_text


def test_the_restored_commands_still_exist(capsys: pytest.CaptureFixture[str]) -> None:
    for argv in (["trace", "multiturn", "--help"], ["cost-ledger", "--help"],
                 ["trace", "schedule", "--help"]):
        assert _cli(argv) == 0
        capsys.readouterr()


@pytest.mark.parametrize("command", ["trace w2", "w2-gate", "slo-goodput", "image-gate"])
def test_readme_has_a_section_per_command_with_every_flag(command: str) -> None:
    sections = _sections()
    assert command in sections, f"README.md has no '### {command}' section"
    for flag in FLAGS[command]:
        assert flag in sections[command], f"{flag} missing from README section '{command}'"


def test_readme_documents_the_noise_floor_in_the_restore_probe_section() -> None:
    section = _sections()["restore-probe"]
    for needle in ("--flush-url", "--noise-k", "max(1e-3", "cold", "both"):
        assert needle in section, needle


def test_readme_image_gate_section_names_the_four_outcomes() -> None:
    section = _sections()["image-gate"]
    for state in ("derived_only_pass", "both_pass", "control_only_pass", "both_fail"):
        assert state in section, state


def test_readme_w2_gate_section_lists_the_plan_thresholds() -> None:
    section = _sections()["w2-gate"]
    for needle in ("0.85", "0.40", "0.65", "0.80", "0.15", "0.08", "0.10", "1.8", "2.2", "footprint"):
        assert needle in section, needle


def test_tests_readme_lists_the_new_modules() -> None:
    for name in ("test_trace_w2", "test_w2_gate", "test_slo_goodput", "test_restore_noise_floor"):
        assert name in TESTS_README, name


@pytest.mark.parametrize("module", NEW_MODULES)
def test_new_modules_are_stdlib_only(module: str) -> None:
    path = SRC / "application" / f"{module}.py"
    assert path.exists(), f"{path} does not exist"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    allowed = set(sys.stdlib_module_names) | {"demo_bench", "__future__"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots = [a.name.split(".")[0] for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            roots = [(node.module or "").split(".")[0]]
        else:
            continue
        assert set(roots) <= allowed, (module, roots)
