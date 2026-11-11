# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Fixtures for the static Terraform tests (the helpers in hcl.py are importable by plain name)."""

from __future__ import annotations

import fcntl
import json
from collections.abc import Callable
from pathlib import Path

import lib_proc
import pytest

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="session")
def tf_root() -> Path:
    """Return ``<repo>/terraform``."""
    return REPO / "terraform"


@pytest.fixture(scope="session")
def terraform_init(request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory) -> Callable[[str, Path], tuple[int, str]]:
    """Return ``init(tf, folder) -> (returncode, stderr_tail)``: ``terraform init`` runs once per folder per pytest run.

    Several tests need an initialised ``terraform/cluster`` (or ``bootstrap``). Two ``terraform init`` in the same folder
    at once corrupt ``.terraform`` (``Module not installed``, provider checksum mismatches), and a ``terraform test`` that
    starts while another worker's init is half way fails the same way. The first caller initialises under an exclusive
    ``flock``; later callers, in any xdist worker, wait for the lock and reuse the recorded result.
    """
    base = tmp_path_factory.getbasetemp()
    shared = base.parent if hasattr(request.config, "workerinput") else base  # xdist workers share the parent dir

    def init(tf: str, folder: Path) -> tuple[int, str]:
        result = shared / f"terraform-init-{folder.name}.json"
        with (shared / f"terraform-init-{folder.name}.lock").open("a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if not result.is_file():
                proc = lib_proc.run([tf, f"-chdir={folder}", "init", "-backend=false", "-input=false"], capture_output=True, text=True, check=False)
                result.write_text(json.dumps([proc.returncode, proc.stderr[-2000:]]))
            returncode, stderr = json.loads(result.read_text())
        return returncode, stderr

    return init
