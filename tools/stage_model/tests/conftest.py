# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Fixtures of the stage_model unit tests (offline: fake Hugging Face API and fake S3, no network, no AWS).

Run from the repo root (the versions are the ones the contract was validated against)::

    uv run --with pytest==9.1.1 --with huggingface_hub==2.3.0 --with boto3==1.43.118 \\
        pytest tools/stage_model/tests -q -p no:cacheprovider

Contract of ``tools/stage_model/stage_model.py`` (the tests are the spec; the module does not exist yet):

* ``main(argv=None, *, environ=None, hf=None, s3=None, work_dir=None) -> int``: the only entry point. ``environ``
  defaults to ``os.environ``; ``hf`` and ``s3`` are the two ports (defaults: the real ``huggingface_hub.HfApi`` /
  ``hf_hub_download`` and a ``boto3`` S3 client). Input env: MODEL_ID, MODEL_REVISION (40-hex commit sha),
  MODEL_S3_URI (``s3://<bucket>/<prefix>``), HF_TOKEN (optional), WORK_DIR (optional, overridden by ``work_dir``).
* Exit codes, module constants (distinct, all but EXIT_OK non-zero): EXIT_OK, EXIT_CONFIG, EXIT_GATED_NO_TOKEN,
  EXIT_ACCESS_DENIED, EXIT_NOT_FOUND, EXIT_VERIFY, EXIT_TRANSFER.
* ``hf`` port (the real ``HfApi`` method names): ``model_info(repo_id, *, revision, token, ...)`` -> ``.gated``
  (False | "auto" | "manual"); ``list_repo_tree(repo_id, *, recursive, revision, token, ...)`` -> RepoFile-like
  (``.path``, ``.size``, ``.lfs`` with ``.sha256`` or None); ``hf_hub_download(repo_id, filename, *, revision,
  local_dir, token, ...)`` -> local path. ``s3`` port: the boto3 client methods ``head_object``,
  ``list_objects_v2``, ``put_object``, ``upload_file``, ``get_object``.
* One file at a time: download, sha256 check (LFS), ``upload_file`` (multipart, ``ChecksumAlgorithm=SHA256``), delete
  the local copy. The disk need is the largest shard, not the model.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path
from types import ModuleType

import pytest

TOOL = Path(__file__).resolve().parents[1]
# STAGE_MODEL_PATH: debug override, like SCRIPTS_DIR for the script tests
MODULE_PATH = Path(os.environ.get("STAGE_MODEL_PATH", TOOL / "stage_model.py"))
TESTS = Path(__file__).resolve().parent

if str(TESTS) not in sys.path:
    sys.path.insert(0, str(TESTS))


@pytest.fixture(scope="session")
def sm() -> ModuleType:
    """Import ``tools/stage_model/stage_model.py`` by path (a missing file fails every test: RED)."""
    assert MODULE_PATH.is_file(), "tools/stage_model/stage_model.py must exist"
    spec = importlib.util.spec_from_file_location("stage_model", MODULE_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod
