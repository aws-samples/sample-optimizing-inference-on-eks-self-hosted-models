# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Sub-cycle F guard (8): ``scripts/10-stage-model.sh`` reads HF_TOKEN from the process environment ONLY.

``config/sample.env`` is generated and overwritten by ``scripts/gen_env.py`` and may be shared or committed by mistake,
so a token found there must never reach the cluster.  The docs already say "export it in the shell".
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from stage_model_harness import SCRIPT, TOKEN, Box, Run

FILE_TOKEN = "hf_" + "FILEONLYTOKEN" + "0123456789abcdefghijklmnop"


@pytest.fixture
def box(tmp_path: Path) -> Box:
    return Box(tmp_path)


def _everything(res: Run) -> str:
    return "\n".join([res.stdout, res.stderr, res.argv_text(), *(c["payload"] for c in res.calls)])


def test_a_token_in_sample_env_is_never_sent_to_the_cluster(box: Box) -> None:
    box.sample_env.write_text(box.sample_env.read_text() + f"HF_TOKEN={FILE_TOKEN}\n")
    res = box.run()
    assert res.rc == 0, res.stderr  # the token is optional: public models stage without it
    assert res.secret_creates() == [], "an HF_TOKEN line in sample.env must not create the hf-token Secret"
    assert FILE_TOKEN not in _everything(res)


def test_a_token_in_sample_env_is_not_rendered_by_dry_run(box: Box) -> None:
    box.sample_env.write_text(box.sample_env.read_text() + f"HF_TOKEN={FILE_TOKEN}\n")
    res = box.run("--dry-run")
    assert res.rc == 0, res.stderr
    assert FILE_TOKEN not in _everything(res)


def test_process_env_token_still_works_when_sample_env_also_has_one(box: Box) -> None:
    box.sample_env.write_text(box.sample_env.read_text() + f"HF_TOKEN={FILE_TOKEN}\n")
    res = box.run(HF_TOKEN=TOKEN)
    assert res.rc == 0, res.stderr
    assert len(res.secret_creates()) == 1
    everything = _everything(res)
    assert TOKEN in everything and FILE_TOKEN not in everything


def test_script_source_has_no_file_token_fallback() -> None:
    text = SCRIPT.read_text()
    assert "file_hf_token" not in text, "the SAMPLE_ENV reader must not collect HF_TOKEN"
    assert not re.search(r"(?m)^\s*HF_TOKEN\)", text), "no `HF_TOKEN)` arm in the SAMPLE_ENV key reader"
    block = re.search(r"(?s)#\s+SAMPLE_ENV\b.*?(?=#\s+PLATFORM_ENV\b)", text)
    assert block, "the header documents SAMPLE_ENV"
    assert "HF_TOKEN" not in block.group(0), "the header must not list HF_TOKEN among the SAMPLE_ENV keys"
