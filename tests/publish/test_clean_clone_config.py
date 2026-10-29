# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The tracked platform.env.example is a complete, renderable placeholder config (M1: clean clone).

A fresh ``git clone`` has no ``manifests/config/platform.env`` (generated, gitignored).  The suites materialize it
from ``platform.env.example`` (``tests/platform_env.py``), so the example must hold concrete placeholder values (no
``<...>`` holes), exactly the canonical set pinned in ``fixtures/platform.env.expected``.
"""

from __future__ import annotations

import re
from pathlib import Path

import sanitize_scan as S

REPO = S.REPO_ROOT
EXPECTED = Path(__file__).with_name("fixtures") / "platform.env.expected"
LINE_RE = re.compile(r"[A-Z][A-Z0-9_]*=[^\s\"'#$`]*")


def _pairs(text: str) -> list[tuple[str, str]]:
    return [tuple(ln.split("=", 1)) for ln in text.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]  # type: ignore[misc]


def _example() -> Path:
    found = sorted((REPO / "manifests").glob("config*/platform.env.example"))
    assert len(found) == 1, f"exactly one platform.env.example expected, got {found}"
    return found[0]


def test_example_is_tracked_and_the_generated_file_is_not() -> None:
    tracked = set(S.tracked_files())
    assert _example().relative_to(REPO).as_posix() in tracked
    generated = [p for p in tracked if p.endswith("platform.env") and "config" in p]
    assert not generated, f"the generated platform.env must stay untracked: {generated}"
    assert "config/sample.env" not in tracked


def test_example_lines_are_plain_key_value() -> None:
    bad = [ln for ln in _example().read_text().splitlines() if ln.strip() and not ln.startswith("#") and not LINE_RE.fullmatch(ln)]
    assert not bad, bad


def test_example_has_no_unfilled_placeholders() -> None:
    holes = [ln for ln in _example().read_text().splitlines() if not ln.startswith("#") and re.search(r"<[A-Za-z_]+>", ln)]
    assert not holes, f"values still hold <...> holes, the example must render as is: {holes}"


def test_example_holds_exactly_the_canonical_placeholder_config() -> None:
    got = dict(_pairs(_example().read_text()))
    want = dict(_pairs(EXPECTED.read_text()))
    assert got == want, {k: (got.get(k), want.get(k)) for k in sorted(set(got) | set(want)) if got.get(k) != want.get(k)}


def test_canonical_config_uses_the_placeholder_account_everywhere() -> None:
    account = S.PLACEHOLDER_ACCOUNT
    cfg = dict(_pairs(EXPECTED.read_text()))
    assert cfg["AWS_ACCOUNT_ID"] == account
    for key in ("ECR_REGISTRY", "BENCH_REPO"):
        assert cfg[key].startswith(f"{account}.dkr.ecr."), key
    assert cfg["SGLANG_RUNTIME_REPO"] == "", "create_runtime_mirror_repository is off by default: the key is empty"
    for key in ("MODEL_BUCKET", "RESULTS_BUCKET", "MODEL_S3_URI"):
        assert f"-{account}-" in cfg[key], key
    assert re.fullmatch(r".+@sha256:[0-9a-f]{64}", cfg["RUNTIME_IMAGE"])


def test_example_keeps_the_keys_scripts_and_renders_read() -> None:
    keys = {k for k, _ in _pairs(_example().read_text())}
    needed = {
        "AWS_ACCOUNT_ID", "AWS_REGION", "BENCH_REPO", "CLUSTER_NAME", "ECR_REGISTRY", "MODEL_BUCKET", "MODEL_S3_PREFIX",
        "MODEL_S3_URI", "RUNTIME_IMAGE", "MODEL_ID", "MODEL_REVISION", "RESULTS_BUCKET", "SGLANG_RUNTIME_REPO",
    }
    assert needed <= keys, sorted(needed - keys)


def test_example_never_carries_a_token() -> None:
    assert not re.search(r"(?im)^(HF_TOKEN|NGC_API_KEY|.*SECRET.*|.*PASSWORD.*)=\S", _example().read_text())
