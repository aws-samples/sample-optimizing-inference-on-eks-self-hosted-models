# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""RED tests: the model path in the generated env (MODEL_S3_PREFIX / MODEL_S3_URI) and its consumers.

Contract (model staging job):

* ``scripts/gen_env.py`` writes ``MODEL_S3_PREFIX`` and ``MODEL_S3_URI`` into BOTH ``platform.env`` (the only file
  ``scripts/lib/common.sh`` reads, as ``CFG_*``) and ``sample.env``. ``MODEL_S3_PREFIX`` is the ``sample_env`` output when
  Terraform sets it (``var.model_s3_prefix``), else ``<MODEL_ID>/<MODEL_REVISION>``; ``MODEL_S3_URI`` is always
  ``s3://<MODEL_BUCKET>/<MODEL_S3_PREFIX>``. A prefix with a leading or trailing slash, ``//``, ``..`` or characters outside
  ``[A-Za-z0-9._/-]`` is refused (nothing written; the message names the key, never the value).
* ``HF_TOKEN`` is never generated: a token in any Terraform output is refused like a sensitive output.
* ``scripts/11-prefetch.sh`` (no ``--model``: the model is ``MODEL_ID``/``MODEL_REVISION`` of platform.env) reads
  ``CFG_MODEL_S3_PREFIX`` as its default source prefix, so what the staging Job writes is what the prefetch reads;
  ``MODEL_PREFIX`` in the environment still wins.
"""

from __future__ import annotations

from pathlib import Path

import lib_scripts as S
import pytest
from test_140_gen_env import (
    outputs,
    parse_env,
    platform_env,
    run,
    sample_env,
    tf_output,
)

MODEL_ID = "Qwen/Qwen3.8-27B-FP8"
REVISION = "0123456789abcdef0123456789abcdef01234567"
BUCKET = f"demo-models-{'1' * 12}-use2"


def _generated(tmp_path: Path, samp: dict[str, str] | None = None) -> tuple[dict[str, str], dict[str, str]]:
    proc, root = run(tmp_path, tf_output(None, samp))
    assert proc.returncode == 0, proc.stderr
    plat_f, samp_f = outputs(root)
    return parse_env(plat_f), parse_env(samp_f)


def test_prefix_defaults_to_model_id_slash_revision_in_both_files(tmp_path: Path) -> None:
    plat, samp = _generated(tmp_path)
    for env in (plat, samp):
        assert env["MODEL_S3_PREFIX"] == f"{MODEL_ID}/{REVISION}"
        assert env["MODEL_S3_URI"] == f"s3://{BUCKET}/{MODEL_ID}/{REVISION}"


def test_prefix_from_terraform_wins_and_the_uri_follows(tmp_path: Path) -> None:
    plat, samp = _generated(tmp_path, sample_env(MODEL_S3_PREFIX="Qwen3.8-27B-FP8"))
    for env in (plat, samp):
        assert env["MODEL_S3_PREFIX"] == "Qwen3.8-27B-FP8"
        assert env["MODEL_S3_URI"] == f"s3://{BUCKET}/Qwen3.8-27B-FP8"


def test_uri_has_no_trailing_slash_and_is_a_valid_env_value(tmp_path: Path) -> None:
    plat, _ = _generated(tmp_path)
    assert not plat["MODEL_S3_URI"].endswith("/")
    assert " " not in plat["MODEL_S3_URI"] and "$" not in plat["MODEL_S3_URI"]


@pytest.mark.parametrize("prefix", ["/lead", "trail/", "a//b", "a/../b", "a b", "a$b", "..", "a/./b", ""], ids=["lead", "trail", "double", "dotdot", "space", "dollar", "only-dotdot", "dot", "empty"])
def test_unsafe_prefix_is_refused_and_nothing_is_written(tmp_path: Path, prefix: str) -> None:
    proc, root = run(tmp_path, tf_output(None, sample_env(MODEL_S3_PREFIX=prefix)))
    assert proc.returncode != 0
    assert "MODEL_S3_PREFIX" in proc.stderr
    assert not any(f.exists() for f in outputs(root))
    if prefix:
        assert prefix not in proc.stderr.replace("MODEL_S3_PREFIX", ""), "refusals never echo a value"


@pytest.mark.parametrize("where", ["platform", "sample"])
def test_hf_token_in_a_terraform_output_is_refused_and_never_written(tmp_path: Path, where: str) -> None:
    secret = "hf_" + "DUMMYTOKEN" + "0123456789abcdefghijklmnop"
    plat = platform_env(HF_TOKEN=secret) if where == "platform" else None
    samp = sample_env(HF_TOKEN=secret) if where == "sample" else None
    proc, root = run(tmp_path, tf_output(plat, samp))
    assert proc.returncode != 0
    assert "HF_TOKEN" in proc.stderr and secret not in proc.stderr + proc.stdout
    assert not any(f.exists() for f in outputs(root))


def test_hf_token_secret_arn_is_not_a_token_and_stays_allowed(tmp_path: Path) -> None:
    arn = "arn:aws:secretsmanager:us-east-2:" + "1" * 12 + ":secret:hf-token-AbCdEf"
    plat, samp = _generated(tmp_path, sample_env(HF_TOKEN_SECRET_ARN=arn))
    assert samp["HF_TOKEN_SECRET_ARN"] == arn and "HF_TOKEN" not in samp and "HF_TOKEN" not in plat


# ----------------------------------------------------------------------------------- 11-prefetch consistency
CHANGED = "apiVersion: batch/v1\nkind: Job\nmetadata:\n  name: CHANGED-BY-SET-ENV\n"


def _prefetch(tmp: Path, cfg_lines: list[str], env: dict[str, str] | None = None) -> list[str]:
    penv = tmp / "platform.env"
    keys = ["AWS_REGION=us-east-2", f"MODEL_BUCKET={BUCKET}", f"MODEL_ID={MODEL_ID}", f"MODEL_REVISION={REVISION}"]
    penv.write_text("\n".join([*keys, *cfg_lines]) + "\n")
    base = {"kustomize_output": "apiVersion: batch/v1\nkind: Job\nmetadata:\n  name: R\n", "apply_requires_objects": True, "set_env_output": CHANGED}
    res = S.Sandbox(tmp / "sb", base).run("11-prefetch.sh", env={"PLATFORM_ENV": str(penv), "MODEL_BUCKET": "", **(env or {})})
    assert res.rc == 0, res.stdout + res.stderr
    calls = [c for c in res.kubectl_calls() if "set" in c["argv"] and "env" in c["argv"]]
    assert calls, "no `kubectl set env` call carrying the Job parameters"
    return calls[0]["argv"]


def test_prefetch_reads_the_generated_prefix_for_the_generated_model(tmp_path: Path) -> None:
    argv = _prefetch(tmp_path, [f"MODEL_S3_PREFIX={MODEL_ID}/{REVISION}"])
    assert f"MODEL_S3_URI=s3://{BUCKET}/{MODEL_ID}/{REVISION}/" in argv, argv


def test_prefetch_model_prefix_env_still_wins(tmp_path: Path) -> None:
    argv = _prefetch(tmp_path, ["MODEL_S3_PREFIX=from/config"], {"MODEL_PREFIX": "from-env"})
    assert f"MODEL_S3_URI=s3://{BUCKET}/from-env/" in argv, argv
