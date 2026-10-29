# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""``scripts/11-prefetch.sh``: model-agnostic S3 -> NVMe prefetch.  RED until the developer rewrites it.

No per-model case table and no ``--model`` flag.  The model is described by ``MODEL_ID`` (``org/name``), ``MODEL_REVISION`` and
the source; the defaults are the keys of the same name in the platform config (``MODEL_ID``, ``MODEL_REVISION``, ``MODEL_S3_URI``).

* ``--model-id ORG/NAME`` and ``--revision SHA`` (each overrides the matching env var / config key); ``--force``; ``--wait``;
  anything else (``--model <name>``, unknown flags, positional arguments) is a usage error (rc 2) before any kubectl call;
* the Job gets ``MODEL_REVISION``, ``MODEL_CACHE_REPO`` (``models--ORG--NAME``), ``MODEL_S3_URI`` (always one trailing ``/``),
  ``AWS_REGION`` (region of the config, never the ambient ``AWS_REGION``) and ``FORCE`` through ``kubectl set env --local``;
* source URI precedence (an empty variable counts as unset): env ``MODEL_S3_URI`` > ``s3://<bucket>/<prefix>/`` when ``MODEL_BUCKET``
  or ``MODEL_PREFIX`` is set in the env (the other half comes from the config: ``MODEL_BUCKET`` / ``MODEL_S3_PREFIX``) > config
  ``MODEL_S3_URI`` > ``s3://<config bucket>/<ORG>-<NAME>/``; the config's URI and prefix belong to the config's model only, so
  another model id always falls to the derived ``<ORG>-<NAME>`` prefix (under the env or config bucket);
* a model id other than the config's needs ``--revision`` (or ``MODEL_REVISION`` in the env): the config revision belongs to the
  config's model, so it is never applied to another one (usage error, nothing applied);
* ``--wait`` verifies ``revision=<revision>`` in ``/mnt/.ephemeral/models/hub/<cache repo>/.done`` and fails without it;
* an active Job is never deleted (P-5); the cluster guard is the one of ``lib/common.sh``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import lib_scripts as S
import pytest
import trackq_scripts as T

CHANGED = "apiVersion: batch/v1\nkind: Job\nmetadata:\n  name: CHANGED-BY-SET-ENV\n"
COMPLETE = {"status": {"conditions": [{"type": "Complete", "status": "True"}], "succeeded": 1}}
ACTIVE = {"status": {"active": 1}}
POLL = {"POLL_S": "1", "NODE_SHELL_TIMEOUT_S": "5", "PREFETCH_TIMEOUT_MIN": "1"}
CFG_URI = T.CFG["MODEL_S3_URI"]
OTHER = ("acme/Other-1B", "c" * 40)


def _run(tmp: Path, *args: str, env: dict[str, str] | None = None, cfg: dict[str, Any] | None = None) -> S.Result:
    base = {"kustomize_output": "apiVersion: batch/v1\nkind: Job\nmetadata:\n  name: R\n", "apply_requires_objects": True, "set_env_output": CHANGED, **(cfg or {})}
    return S.Sandbox(tmp, base).run("11-prefetch.sh", *args, env={**T.ENV_Q, **POLL, "MODEL_BUCKET": "", **(env or {})})


def _set_env(res: S.Result) -> list[str]:
    calls = [c for c in res.kubectl_calls() if "set" in c["argv"] and "env" in c["argv"]]
    assert calls, "no `kubectl set env` call carrying the Job parameters"
    return calls[0]["argv"]


def _config(tmp: Path, **drop_or_set: str | None) -> dict[str, str]:
    """PLATFORM_ENV = the placeholder config with keys replaced (value) or removed (None)."""
    keys = dict(T.CFG)
    for k, v in drop_or_set.items():
        if v is None:
            keys.pop(k, None)
        else:
            keys[k] = v
    out = tmp / "platform-custom.env"
    out.write_text("".join(f"{k}={v}\n" for k, v in keys.items()))
    return {"PLATFORM_ENV": str(out)}


# ----------------------------------------------------------------------------- defaults come from the config
def test_job_parameters_default_to_the_config(tmp_path: Path) -> None:
    res = _run(tmp_path)
    assert res.rc == 0, res.stdout + res.stderr
    argv = _set_env(res)
    assert f"MODEL_S3_URI={CFG_URI}/" in argv, argv
    assert f"MODEL_REVISION={T.CFG['MODEL_REVISION']}" in argv
    assert f"MODEL_CACHE_REPO={T.CACHE_REPO}" in argv
    assert f"AWS_REGION={T.CFG['AWS_REGION']}" in argv, "the harness' ambient AWS_REGION is eu-west-3"
    assert "FORCE=0" in argv


def test_a_config_uri_with_a_trailing_slash_gets_exactly_one(tmp_path: Path) -> None:
    res = _run(tmp_path, env=_config(tmp_path, MODEL_S3_URI=CFG_URI + "/"))
    assert res.rc == 0, res.stdout + res.stderr
    assert f"MODEL_S3_URI={CFG_URI}/" in _set_env(res)


def test_a_config_without_model_s3_uri_derives_the_prefix_from_the_model_id(tmp_path: Path) -> None:
    res = _run(tmp_path, env=_config(tmp_path, MODEL_S3_URI=None, MODEL_S3_PREFIX=None))
    assert res.rc == 0, res.stdout + res.stderr
    assert f"MODEL_S3_URI=s3://{T.CFG['MODEL_BUCKET']}/Qwen-Qwen3.8-27B-FP8/" in _set_env(res)


def test_env_model_s3_uri_wins(tmp_path: Path) -> None:
    res = _run(tmp_path, env={"MODEL_S3_URI": "s3://custom-models/some/prefix"})
    assert res.rc == 0, res.stdout + res.stderr
    assert "MODEL_S3_URI=s3://custom-models/some/prefix/" in _set_env(res)


def test_env_bucket_alone_keeps_the_config_prefix(tmp_path: Path) -> None:
    res = _run(tmp_path, env={"MODEL_BUCKET": "custom-models"})
    assert res.rc == 0, res.stdout + res.stderr
    assert f"MODEL_S3_URI=s3://custom-models/{T.CFG['MODEL_S3_PREFIX']}/" in _set_env(res)


def test_env_prefix_alone_keeps_the_config_bucket(tmp_path: Path) -> None:
    res = _run(tmp_path, env={"MODEL_PREFIX": "custom-prefix"})
    assert res.rc == 0, res.stdout + res.stderr
    assert f"MODEL_S3_URI=s3://{T.CFG['MODEL_BUCKET']}/custom-prefix/" in _set_env(res)


def test_env_bucket_prefix_and_region_override_the_config(tmp_path: Path) -> None:
    res = _run(tmp_path, env={"MODEL_BUCKET": "custom-models", "MODEL_BUCKET_REGION": "eu-central-1", "MODEL_PREFIX": "custom-prefix"})
    assert res.rc == 0, res.stdout + res.stderr
    argv = _set_env(res)
    assert "MODEL_S3_URI=s3://custom-models/custom-prefix/" in argv and "AWS_REGION=eu-central-1" in argv


def test_env_model_id_and_revision_drive_the_job(tmp_path: Path) -> None:
    res = _run(tmp_path, env={"MODEL_ID": OTHER[0], "MODEL_REVISION": OTHER[1]})
    assert res.rc == 0, res.stdout + res.stderr
    argv = _set_env(res)
    assert "MODEL_CACHE_REPO=models--acme--Other-1B" in argv and f"MODEL_REVISION={OTHER[1]}" in argv
    assert f"MODEL_S3_URI=s3://{T.CFG['MODEL_BUCKET']}/acme-Other-1B/" in argv, "the config URI belongs to the config's model"


def test_force_is_passed_through(tmp_path: Path) -> None:
    res = _run(tmp_path, "--force")
    assert res.rc == 0, res.stdout + res.stderr
    assert "FORCE=1" in _set_env(res)


# ----------------------------------------------------------------------------- flags
def test_flags_describe_any_model(tmp_path: Path) -> None:
    res = _run(tmp_path, "--model-id", OTHER[0], "--revision", OTHER[1])
    assert res.rc == 0, res.stdout + res.stderr
    argv = _set_env(res)
    assert "MODEL_CACHE_REPO=models--acme--Other-1B" in argv and f"MODEL_REVISION={OTHER[1]}" in argv
    assert f"MODEL_S3_URI=s3://{T.CFG['MODEL_BUCKET']}/acme-Other-1B/" in argv


def test_flags_beat_env(tmp_path: Path) -> None:
    res = _run(tmp_path, "--model-id", OTHER[0], "--revision", OTHER[1], env={"MODEL_ID": "x/y", "MODEL_REVISION": "d" * 40})
    assert res.rc == 0, res.stdout + res.stderr
    assert "MODEL_CACHE_REPO=models--acme--Other-1B" in _set_env(res)


def test_the_config_model_with_an_explicit_revision(tmp_path: Path) -> None:
    res = _run(tmp_path, "--revision", "e" * 40)
    assert res.rc == 0, res.stdout + res.stderr
    argv = _set_env(res)
    assert f"MODEL_REVISION={'e' * 40}" in argv and f"MODEL_CACHE_REPO={T.CACHE_REPO}" in argv and f"MODEL_S3_URI={CFG_URI}/" in argv


def test_another_model_without_a_revision_is_refused(tmp_path: Path) -> None:
    res = _run(tmp_path, "--model-id", OTHER[0])
    assert res.rc != 0 and "revision" in (res.stdout + res.stderr).lower()
    assert not T.apply_calls(res) and not T.delete_calls(res)


@pytest.mark.parametrize(
    "args",
    [
        ("--model", "qwen38-27b"),
        ("--model", "g" + "lm53"),
        ("--model-id",),
        ("--revision",),
        ("--bogus",),
        ("extra",),
        ("--model-id", "no-slash", "--revision", "a" * 40),
        ("--model-id", "acme/bad name", "--revision", "a" * 40),
        ("--model-id", "acme/x;rm", "--revision", "a" * 40),
        ("--model-id", "../x/y", "--revision", "a" * 40),
        ("--model-id", OTHER[0], "--revision", "a b"),
        ("--model-id", OTHER[0], "--revision", "$(id)"),
    ],
    ids=["old-model-flag", "removed-model-flag", "model-id-no-value", "revision-no-value", "unknown", "positional", "id-no-slash", "id-space", "id-semicolon", "id-dotdot", "rev-space", "rev-subshell"],
)
def test_bad_usage_is_refused_before_any_kubectl_call(tmp_path: Path, args: tuple[str, ...]) -> None:
    res = _run(tmp_path, *args)
    assert res.rc != 0, res.stdout + res.stderr
    assert T.cluster_calls(res) == [] and not T.apply_calls(res)


def test_a_config_without_model_keys_needs_the_flags(tmp_path: Path) -> None:
    env = _config(tmp_path, MODEL_ID=None, MODEL_REVISION=None, MODEL_S3_URI=None, MODEL_S3_PREFIX=None)
    res = _run(tmp_path / "bare", env=env)
    assert res.rc != 0 and not T.apply_calls(res), "no literal model may stand in for a missing config key"
    ok = _run(tmp_path / "flags", "--model-id", OTHER[0], "--revision", OTHER[1], env=env)
    assert ok.rc == 0, ok.stdout + ok.stderr


def test_the_script_has_no_per_model_table() -> None:
    text = (S.SCRIPTS_DIR / "11-prefetch.sh").read_text()
    for needle in ("g" + "lm", "G" + "LM", "qwen3-32b", "qwen38", "Qwen", "RadixArk", "DEMO_TRACK", "TRACKB", "TRACKQ"):
        assert needle not in text, f"scripts/11-prefetch.sh still names {needle}"


# ----------------------------------------------------------------------------- --wait and the active-Job refusal
def test_wait_verifies_the_marker_of_the_requested_revision(tmp_path: Path) -> None:
    res = _run(tmp_path, "--wait", cfg={"jobs": {"model-prefetch": COMPLETE}})
    assert res.rc == 0, res.stdout + res.stderr
    checks = [c["argv"] for c in res.kubectl_calls() if "exec" in c["argv"] and ".done" in " ".join(c["argv"])]
    assert checks, "no marker verification"
    joined = " ".join(checks[0])
    assert f"revision={T.CFG['MODEL_REVISION']}" in joined and T.MARKER in joined, joined


def test_wait_for_another_model_checks_its_own_marker(tmp_path: Path) -> None:
    res = _run(tmp_path, "--model-id", OTHER[0], "--revision", OTHER[1], "--wait", cfg={"jobs": {"model-prefetch": COMPLETE}})
    assert res.rc == 0, res.stdout + res.stderr
    joined = " ".join(" ".join(c["argv"]) for c in res.kubectl_calls() if "exec" in c["argv"] and ".done" in " ".join(c["argv"]))
    assert f"revision={OTHER[1]}" in joined and "/mnt/.ephemeral/models/hub/models--acme--Other-1B/.done" in joined, joined


def test_wait_fails_when_the_marker_is_missing(tmp_path: Path) -> None:
    res = _run(tmp_path, "--wait", cfg={"jobs": {"model-prefetch": COMPLETE}, "markers": "missing"})
    assert res.rc != 0


def test_an_active_prefetch_job_is_never_deleted(tmp_path: Path) -> None:
    res = _run(tmp_path, cfg={"jobs": {"model-prefetch": ACTIVE}})
    assert res.rc != 0
    assert not [c for c in T.delete_calls(res) if "model-prefetch" in " ".join(c["argv"])]
    assert not T.apply_calls(res)
