# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""RED tests: ``scripts/10-stage-model.sh [--dry-run]``, the Hugging Face -> S3 model staging Job.

Contract (model staging job; names are fixed so the Terraform, the script and the tests agree):

* Inputs: process env wins over the env files. ``SAMPLE_ENV`` (default ``config/sample.env``) provides MODEL_ID,
  MODEL_REVISION, MODEL_S3_PREFIX and MODEL_S3_URI; ``PLATFORM_ENV`` (the file ``common.sh`` already reads) provides
  MODEL_BUCKET and AWS_REGION. The optional HF_TOKEN comes from the process environment only (test_153). MODEL_S3_URI defaults to ``s3://<bucket>/<MODEL_ID>/<MODEL_REVISION>``.
  ``STAGE_IMAGE`` is the Job image (``repo:tag@sha256:<64 hex>``); the script's default is a placeholder digest it refuses.
* Objects, all in ``node-ops``: ConfigMap ``model-stage-code`` (``kubectl create configmap --from-file --dry-run=client``:
  ``stage_model.py`` and ``requirements.txt`` of ``tools/stage_model``), Job ``model-stage`` (ServiceAccount ``model-stager``,
  Terraform-owned), and, only when HF_TOKEN is set, Secret ``hf-token`` (key HF_TOKEN) that the Job reads with an OPTIONAL
  ``secretKeyRef``. The Secret is created before the Job, reaches kubectl through stdin or a file (never argv) and is deleted
  by a trap on EXIT / INT / TERM once the Job ends or the script dies. The script waits for the Job and exits with its result.
* ``--dry-run``: renders ConfigMap + Job to stdout (and nothing else on stdout), runs no cluster call except the offline
  ``create configmap --dry-run=client``, and never renders the token.
"""

from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
import time
from pathlib import Path
from typing import Any

import lib_proc
import lib_scripts as S
import pytest
import yaml
from stage_model_harness import BUCKET, IMAGE, JOB, MODEL, NS, REVISION, SCRIPT, SECRET, TOKEN, URI, Box, Run

TOOL = S.REPO_ROOT / "tools" / "stage_model"
PLACEHOLDER_HINT = "digest"
SWITCH_ON = "1"  # FAKE_SECRET_EXISTS=1: the fake kubectl starts with a Secret left over by an earlier run
EMPTY = ""  # HF_TOKEN exported but empty, which the script treats as unset


@pytest.fixture
def box(tmp_path: Path) -> Box:
    return Box(tmp_path)


def job_of(docs: list[dict[str, Any]]) -> dict[str, Any]:
    jobs = [d for d in docs if d.get("kind") == "Job"]
    assert len(jobs) == 1, [d.get("kind") for d in docs]
    return jobs[0]


def container(job: dict[str, Any]) -> dict[str, Any]:
    conts = job["spec"]["template"]["spec"]["containers"]
    assert len(conts) == 1
    return conts[0]


def env_of(job: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {e["name"]: e for e in container(job).get("env", [])}


def job_apply_index(res: Run) -> int:
    """Index of the call that applies the Job."""
    return next(i for i, c in enumerate(res.calls) if "apply" in c["argv"] and not res.is_secret_create(c) and ("kind: Job" in c["payload"] or '"kind": "Job"' in c["payload"]))


def only_the_stale_secret_cleanup(res: Run) -> bool:
    """True when the one Secret deletion is the pre-apply cleanup of a left-over Secret (``--ignore-not-found``)."""
    deletes = res.secret_deletes()
    return len(deletes) == 1 and deletes[0] < job_apply_index(res) and "--ignore-not-found" in res.calls[deletes[0]]["argv"]


# ----------------------------------------------------------------------------------- script hygiene
def test_script_exists_executable_strict_and_parses() -> None:
    assert SCRIPT.is_file(), "scripts/10-stage-model.sh must exist"
    assert os.access(SCRIPT, os.X_OK)
    text = SCRIPT.read_text()
    assert text.splitlines()[0] in ("#!/usr/bin/env bash", "#!/bin/bash")
    assert "set -euo pipefail" in text
    assert "SPDX" not in text or "MIT-0" in text
    assert lib_proc.run(["bash", "-n", str(SCRIPT)], capture_output=True, text=True).returncode == 0


def test_script_is_portable_to_macos_bash_3_2() -> None:
    text = SCRIPT.read_text()
    for banned in (r"\bmapfile\b", r"\breadarray\b", r"declare\s+-A", r"\$\{[A-Za-z_]+,,\}", r"\bsetsid\b"):
        assert not re.search(banned, text), banned


def test_script_passes_shellcheck() -> None:
    if shutil.which("shellcheck") is None:
        pytest.skip("shellcheck binary not installed")
    assert SCRIPT.is_file()
    proc = lib_proc.run(["shellcheck", "-x", "--shell=bash", str(SCRIPT)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_script_sources_the_shared_helpers_like_its_siblings() -> None:
    text = SCRIPT.read_text()
    assert "lib/common.sh" in text and "require_kube_target" in text


# ----------------------------------------------------------------------------------- dry-run render
def test_dry_run_renders_configmap_and_job_only_and_touches_nothing(box: Box) -> None:
    res = box.run("--dry-run")
    assert res.rc == 0, res.stderr
    assert sorted(d["kind"] for d in res.stdout_docs()) == ["ConfigMap", "Job"]
    assert res.mutating() == [], f"--dry-run must not reach the cluster: {res.mutating()}"
    assert [c for c in res.calls if "create" in c["argv"] and "configmap" in c["argv"]], "the code ConfigMap is rendered with kubectl create configmap --dry-run=client"


def test_dry_run_is_deterministic(box: Box) -> None:
    assert box.run("--dry-run").stdout == box.run("--dry-run").stdout != ""


def test_configmap_carries_the_tool_files_verbatim(box: Box) -> None:
    cm = next(d for d in box.run("--dry-run").stdout_docs() if d["kind"] == "ConfigMap")
    assert cm["metadata"]["namespace"] == NS
    assert set(cm["data"]) == {"stage_model.py", "requirements.txt"}
    assert cm["data"]["stage_model.py"] == (TOOL / "stage_model.py").read_text()
    assert cm["data"]["requirements.txt"] == (TOOL / "requirements.txt").read_text()


def test_job_identity_and_environment(box: Box) -> None:
    job = job_of(box.run("--dry-run").stdout_docs())
    assert job["metadata"]["name"] == JOB and job["metadata"]["namespace"] == NS
    spec = job["spec"]["template"]["spec"]
    assert spec["serviceAccountName"] == "model-stager"
    assert spec["restartPolicy"] == "Never"
    assert spec.get("automountServiceAccountToken") is False, "Pod Identity needs no Kubernetes API token"
    c = container(job)
    assert c["image"] == IMAGE
    env = env_of(job)
    assert env["MODEL_ID"]["value"] == MODEL and env["MODEL_REVISION"]["value"] == REVISION
    assert env["MODEL_S3_URI"]["value"] == URI and env["AWS_REGION"]["value"] == "us-east-2"
    assert not {"AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"} & set(env), "credentials come from Pod Identity"


def test_job_token_is_an_optional_secret_key_ref_never_a_literal(box: Box) -> None:
    for token in (None, TOKEN):
        job = job_of(box.run("--dry-run", HF_TOKEN=token).stdout_docs())
        entry = env_of(job)["HF_TOKEN"]
        assert "value" not in entry
        ref = entry["valueFrom"]["secretKeyRef"]
        assert ref["name"] == SECRET and ref["key"] == "HF_TOKEN" and ref.get("optional") is True


def test_job_limits_retries_and_cleanup(box: Box) -> None:
    job = job_of(box.run("--dry-run").stdout_docs())["spec"]
    assert isinstance(job["activeDeadlineSeconds"], int) and 0 < job["activeDeadlineSeconds"] <= 86400
    assert 0 <= job["backoffLimit"] <= 2
    assert isinstance(job["ttlSecondsAfterFinished"], int) and job["ttlSecondsAfterFinished"] > 0


def test_job_runs_on_the_cpu_pool_without_gpu_and_is_not_consolidated(box: Box) -> None:
    out = box.run("--dry-run")
    job = job_of(out.stdout_docs())
    pod = job["spec"]["template"]
    assert pod["spec"]["nodeSelector"] == {"karpenter.sh/nodepool": "cpu-demo"}
    assert pod["metadata"]["annotations"]["karpenter.sh/do-not-disrupt"] == "true"
    assert "nvidia.com" not in out.stdout and "gpu" not in out.stdout.lower().replace("no gpu", "")
    assert not pod["spec"].get("tolerations")


def test_job_resources_and_security_context(box: Box) -> None:
    job = job_of(box.run("--dry-run").stdout_docs())
    c = container(job)
    res = c["resources"]
    for key in ("cpu", "memory", "ephemeral-storage"):
        assert key in res["requests"], key
    assert "memory" in res["limits"] and "ephemeral-storage" in res["limits"]
    sc = c["securityContext"]
    assert sc["allowPrivilegeEscalation"] is False and sc["capabilities"]["drop"] == ["ALL"]
    pod_sc = job["spec"]["template"]["spec"]["securityContext"]
    assert pod_sc["runAsNonRoot"] is True and pod_sc["runAsUser"] > 0
    assert pod_sc["seccompProfile"]["type"] == "RuntimeDefault"


def _cpu_cores(q: str) -> float:
    return float(q[:-1]) / 1000 if q.endswith("m") else float(q)


def _gib(q: str) -> float:
    units = {"Ki": 2**-20, "Mi": 2**-10, "Gi": 1.0}
    return float(q[:-2]) * units[q[-2:]] if q[-2:] in units else float(q) / 2**30


def test_job_requests_fit_next_to_monitoring_on_the_one_cpu_demo_node(box: Box) -> None:
    """Live test 2 (D3): 2 vCPU / 4Gi did not fit next to monitoring on the single cpu-demo node, so Karpenter added a
    second node that the pool never consolidates. One file at a time needs about 1 vCPU and 2Gi."""
    res = container(job_of(box.run("--dry-run").stdout_docs()))["resources"]
    assert _cpu_cores(str(res["requests"]["cpu"])) <= 1
    assert _gib(res["requests"]["memory"]) <= 2
    assert _gib(res["limits"]["memory"]) >= _gib(res["requests"]["memory"])


def test_code_is_mounted_from_the_configmap_and_no_script_lives_in_yaml(box: Box) -> None:
    docs = box.run("--dry-run").stdout_docs()
    job, cm = job_of(docs), next(d for d in docs if d["kind"] == "ConfigMap")
    spec = job["spec"]["template"]["spec"]
    vols = {v["name"]: v for v in spec["volumes"]}
    code = [v for v in vols.values() if v.get("configMap", {}).get("name") == cm["metadata"]["name"]]
    assert code, "a volume must mount the code ConfigMap"
    assert any(v["name"] in {m["name"] for m in container(job)["volumeMounts"]} for v in code)
    work = [v for v in vols.values() if "emptyDir" in v]
    assert work and all("sizeLimit" in v["emptyDir"] for v in work), "every emptyDir has a sizeLimit (the disk is bounded)"
    argv = [*container(job).get("command", []), *container(job).get("args", [])]
    assert argv and all("\n" not in a and len(a) <= 300 for a in argv), f"no inline script in the YAML: {argv}"
    assert any(a.endswith("stage_model.py") for a in argv)
    joined = " ".join(argv)
    if "pip" in joined:
        assert "-r" in joined and "requirements.txt" in joined, "dependencies come only from the pinned requirements file"


def test_dry_run_equals_what_a_real_run_applies(box: Box) -> None:
    dry = box.run("--dry-run").stdout_docs()
    real = box.run()
    assert real.rc == 0, real.stderr
    assert sorted(real.applied_docs(), key=lambda d: d["kind"]) == sorted(dry, key=lambda d: d["kind"])


def test_s3_uri_is_derived_from_the_bucket_when_the_env_has_no_path(box: Box) -> None:
    box.write_sample(MODEL_S3_URI=None, MODEL_S3_PREFIX=None)
    env = env_of(job_of(box.run("--dry-run").stdout_docs()))
    assert env["MODEL_S3_URI"]["value"] == f"s3://{BUCKET}/{MODEL}/{REVISION}"


def test_process_env_wins_over_the_env_file(box: Box) -> None:
    other = "b" * 40
    env = env_of(job_of(box.run("--dry-run", MODEL_REVISION=other, MODEL_S3_URI=f"s3://{BUCKET}/x/{other}").stdout_docs()))
    assert env["MODEL_REVISION"]["value"] == other and env["MODEL_S3_URI"]["value"] == f"s3://{BUCKET}/x/{other}"


# ----------------------------------------------------------------------------------- preflight refusals
BAD_IMAGES = {
    "placeholder-default": None,
    "latest": "public.ecr.aws/docker/library/python:latest",
    "tag-only": "public.ecr.aws/docker/library/python:3.12-slim",
    "untagged": "public.ecr.aws/docker/library/python",
    "digest-too-short": "python:3.12-slim@sha256:" + "a" * 63,
    "digest-not-hex": "python:3.12-slim@sha256:" + "z" * 64,
    "zero-digest": "python:3.12-slim@sha256:" + "0" * 64,
    "latest-with-digest": "python:latest@sha256:" + "a" * 64,
}


@pytest.mark.parametrize("mode", [["--dry-run"], []], ids=["dry-run", "apply"])
@pytest.mark.parametrize("image", list(BAD_IMAGES.values()), ids=list(BAD_IMAGES))
def test_unpinned_or_placeholder_image_is_refused(box: Box, mode: list[str], image: str | None) -> None:
    res = box.run(*mode, STAGE_IMAGE=image)
    assert res.rc != 0
    assert "STAGE_IMAGE" in res.stderr and PLACEHOLDER_HINT in res.stderr.lower()
    assert res.mutating() == [] and res.stdout.strip() == "", "nothing rendered or applied"


def test_the_default_image_is_a_placeholder_and_never_latest() -> None:
    text = SCRIPT.read_text()
    m = re.search(r"STAGE_IMAGE=\"?\$\{STAGE_IMAGE:-([^}\"]+)\}", text)
    assert m, "STAGE_IMAGE needs a documented default (a placeholder the preflight refuses)"
    assert ":latest" not in m.group(1)
    assert not re.search(r"@sha256:(?!0{64}\b)[0-9a-f]{64}", m.group(1)) or "<" in m.group(1), "the default must not be a real-looking digest"


@pytest.mark.parametrize("revision", ["main", "master", "refs/heads/main", "v1.0", "0123456", "0123456789ABCDEF0123456789ABCDEF01234567", "0123456789abcdef0123456789abcdef0123456", "0123456789abcdef0123456789abcdef012345678"])
@pytest.mark.parametrize("mode", [["--dry-run"], []], ids=["dry-run", "apply"])
def test_branch_names_and_non_sha_revisions_are_refused(box: Box, mode: list[str], revision: str) -> None:
    res = box.run(*mode, MODEL_REVISION=revision)
    assert res.rc != 0
    assert "MODEL_REVISION" in res.stderr and "sha" in res.stderr.lower()
    assert res.mutating() == []


@pytest.mark.parametrize(("name", "value"), [("MODEL_ID", None), ("MODEL_ID", "no-slash"), ("MODEL_ID", "a/b/c"), ("MODEL_ID", "a b/c"), ("MODEL_S3_URI", "https://x/y"), ("MODEL_S3_URI", "s3://bucket-only")])
def test_invalid_model_inputs_are_refused(box: Box, name: str, value: str | None) -> None:
    if value is None:  # absent from the env file and from the environment
        box.write_sample(**{name: None})
        res = box.run("--dry-run")
    else:
        res = box.run("--dry-run", **{name: value})
    assert res.rc != 0 and name in res.stderr
    assert res.mutating() == []


@pytest.mark.parametrize("token", ["tok en", "tok\nen", "tok'en", 'tok"en', "tok$en", "a" * 4097], ids=["space", "newline", "squote", "dquote", "dollar", "too-long"])
def test_malformed_token_is_refused_without_echoing_it(box: Box, token: str) -> None:
    res = box.run(HF_TOKEN=token)
    assert res.rc != 0 and "HF_TOKEN" in res.stderr
    assert res.mutating() == []
    if len(token) < 100:
        assert token not in res.stderr + res.stdout


PATHISH_MODEL_IDS = ["../x", "./x", "x/..", "x/.", "../..", "/x", "x/", "x//y"]


@pytest.mark.parametrize("model_id", PATHISH_MODEL_IDS)
@pytest.mark.parametrize("mode", [["--dry-run"], []], ids=["dry-run", "apply"])
def test_path_ish_model_ids_are_refused(box: Box, mode: list[str], model_id: str) -> None:
    """The default prefix is <MODEL_ID>/<MODEL_REVISION>: '.', '..' and empty parts are refused (a valid URI is given)."""
    res = box.run(*mode, MODEL_ID=model_id, MODEL_S3_URI=URI)
    assert res.rc != 0 and "MODEL_ID" in res.stderr, res.stderr
    assert res.mutating() == [] and res.stdout.strip() == ""


def test_dotted_model_names_are_accepted(box: Box) -> None:
    env = env_of(job_of(box.run("--dry-run", MODEL_ID="Qwen/Qwen3.8-27B-FP8").stdout_docs()))
    assert env["MODEL_ID"]["value"] == "Qwen/Qwen3.8-27B-FP8"
    assert env["MODEL_S3_URI"]["value"] == f"s3://{BUCKET}/Qwen/Qwen3.8-27B-FP8/{REVISION}"


def test_unknown_option_is_a_usage_error(box: Box) -> None:
    res = box.run("--bogus")
    assert res.rc == 2 and "usage" in res.stderr.lower()
    assert res.mutating() == []


# ----------------------------------------------------------------------------------- apply: public model, no token
def test_run_without_token_creates_no_secret_at_all(box: Box) -> None:
    res = box.run()
    assert res.rc == 0, res.stderr
    assert res.secret_creates() == []
    assert only_the_stale_secret_cleanup(res), "without a token the only Secret call is the pre-apply cleanup of a left-over one"
    assert [d["kind"] for d in res.applied_docs() if d["kind"] == "Job"] == ["Job"]


def test_a_stale_secret_is_deleted_before_the_job_even_without_a_token(box: Box) -> None:
    """A run killed with SIGKILL skips its trap: the next run removes the left-over Secret first, token or not, since the Job
    would read the stale value through its optional secretKeyRef."""
    res = box.run(FAKE_SECRET_EXISTS=SWITCH_ON)
    assert res.rc == 0, res.stderr
    assert res.secret_creates() == []
    assert only_the_stale_secret_cleanup(res)
    argv = res.calls[res.secret_deletes()[0]]["argv"]
    assert SECRET in argv and NS in argv
    assert box.state()["secret"] is False


def test_run_waits_for_the_job_and_prints_logs_hint_and_final_uri(box: Box) -> None:
    res = box.run()
    assert res.rc == 0, res.stderr
    assert any(c for c in res.calls if "wait" in c["argv"] or ("get" in c["argv"] and any(t.startswith("job") for t in c["argv"]))), "the script follows the Job to its end"
    assert f"job/{JOB}" in res.stdout and NS in res.stdout and "logs" in res.stdout
    assert URI in res.stdout


def test_failed_job_fails_the_script_and_the_uri_is_not_announced_as_done(box: Box) -> None:
    res = box.run(FAKE_JOB_RESULT="failed")
    assert res.rc != 0
    assert f"job/{JOB}" in res.stdout + res.stderr and "logs" in res.stdout + res.stderr


@pytest.mark.parametrize("pending", [0, 1], ids=["failed-at-once", "failed-after-a-poll"])
def test_a_failed_job_ends_the_script_within_seconds(box: Box, pending: int) -> None:
    """Live test 2 (D1): a wait that woke on Complete only noticed a Failed Job ~50 s late. The fake's ``kubectl wait``
    blocks until its ``--timeout`` on a condition the Job never reaches, as the real one does."""
    start = time.monotonic()
    res = box.run(FAKE_JOB_RESULT="failed", FAKE_JOB_PENDING_GETS=str(pending), timeout=45)
    elapsed = time.monotonic() - start
    assert res.rc != 0
    assert "failed" in res.stderr and "staged:" not in res.stdout
    assert elapsed < 15, f"a Failed Job must end the script within one poll (took {elapsed:.1f}s)"


def test_a_job_that_completes_after_a_poll_is_followed_to_its_end(box: Box) -> None:
    res = box.run(FAKE_JOB_PENDING_GETS="1", timeout=45)
    assert res.rc == 0, res.stderr
    assert f"staged: {URI}/" in res.stdout
    gets = [c for c in res.calls if "get" in c["argv"] and any(t.startswith("job") for t in c["argv"])]
    assert len(gets) >= 3, "the pre-apply state read, then at least two polls of the Job"


# ----------------------------------------------------------------------------------- apply: token hygiene
def test_token_creates_an_ephemeral_secret_before_the_job_and_deletes_it_after(box: Box) -> None:
    res = box.run(HF_TOKEN=TOKEN)
    assert res.rc == 0, res.stderr
    creates, deletes = res.secret_creates(), res.secret_deletes()
    assert len(creates) == 1 and deletes, (creates, deletes)
    job_apply = next(i for i, c in enumerate(res.calls) if "apply" in c["argv"] and i not in creates and ("kind: Job" in c["payload"] or '"kind": "Job"' in c["payload"]))
    assert creates[0] < job_apply < deletes[-1], "Secret first, Job next, deletion last"
    sc = res.calls[creates[0]]
    assert SECRET in " ".join(sc["argv"]) + sc["payload"] and NS in " ".join(sc["argv"]) + sc["payload"]
    assert "HF_TOKEN" in " ".join(sc["argv"]) + sc["payload"]
    assert TOKEN in sc["payload"], "the value reaches kubectl through stdin or a file"
    assert box.state()["secret"] is False, "no Secret is left in the cluster"


def test_token_never_appears_in_argv_stdout_stderr_or_the_job_manifest(box: Box) -> None:
    res = box.run(HF_TOKEN=TOKEN)
    assert res.rc == 0
    assert TOKEN not in res.argv_text(), "a token in argv is visible in `ps`"
    assert TOKEN not in res.stdout and TOKEN not in res.stderr
    for i, c in enumerate(res.calls):
        if i not in res.secret_creates():
            assert TOKEN not in c["payload"], c["argv"]
    assert TOKEN not in yaml.safe_dump(res.applied_docs())


def test_token_never_appears_in_dry_run(box: Box) -> None:
    res = box.run("--dry-run", HF_TOKEN=TOKEN)
    assert res.rc == 0
    assert TOKEN not in res.stdout + res.stderr + res.argv_text()
    assert res.secret_creates() == [] and res.secret_deletes() == []
    assert all(TOKEN not in c["payload"] for c in res.calls)


@pytest.mark.parametrize("how", ["bash -x", "SHELLOPTS=xtrace"])
@pytest.mark.parametrize("mode", [["--dry-run"], []], ids=["dry-run", "apply"])
def test_shell_tracing_never_prints_the_token(box: Box, how: str, mode: list[str]) -> None:
    """``bash -x`` or SHELLOPTS=xtrace in the environment: the trace is on when the script starts, never with the token."""
    box.reset()
    if how == "bash -x":
        argv, env = ["bash", "-x", str(SCRIPT), *mode], box.env(HF_TOKEN=TOKEN, PS4="+ ")
    else:
        argv, env = [str(SCRIPT), *mode], box.env(HF_TOKEN=TOKEN, PS4="+ ", SHELLOPTS="xtrace")
    proc = lib_proc.run(argv, cwd=box.root / "cwd", env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60, check=False)
    out = proc.stdout + proc.stderr
    assert out.count(TOKEN) == 0, "the token was printed by the shell trace"
    assert proc.returncode == 0, proc.stderr
    assert re.search(r"(?m)^\++ ", proc.stderr), "tracing must be on when the script starts, else this test proves nothing"
    if not mode:
        calls = box.calls()
        assert any(TOKEN in c["payload"] for c in calls), "the token was really handed to kubectl (stdin)"
        assert box.state()["secret"] is False


def test_token_leaves_no_temporary_file_behind(box: Box) -> None:
    box.run(HF_TOKEN=TOKEN)
    leaked = [p for p in box.tmp.rglob("*") if p.is_file() and TOKEN in p.read_text(errors="ignore")]
    assert leaked == [], leaked


def test_process_env_token_beats_the_file_token(box: Box) -> None:
    box.sample_env.write_text(box.sample_env.read_text() + "HF_TOKEN=hf_FILE_TOKEN_VALUE\n")
    res = box.run(HF_TOKEN=TOKEN)
    payload = res.calls[res.secret_creates()[0]]["payload"]
    assert TOKEN in payload and "hf_FILE_TOKEN_VALUE" not in payload


@pytest.mark.parametrize(("token", "auth"), [(TOKEN, "bearer"), (EMPTY, "none"), (None, "none")],
                         ids=["token", "empty-token", "no-token"])
def test_the_start_log_line_says_auth_bearer_or_none_never_token(box: Box, token: str | None, auth: str) -> None:
    """The field has the name and the values of the Job's own ``event=model`` line (tools/stage_model, ``auth=``):
    ``bearer`` when a token goes to the Job (which sends it to a gated repository only), else ``none``."""
    res = box.run("--dry-run", HF_TOKEN=token)
    assert res.rc == 0, res.stderr
    lines = [ln for ln in res.stderr.splitlines() if f"model={MODEL} revision={REVISION}" in ln]
    assert len(lines) == 1, res.stderr
    assert lines[0].endswith(f" auth={auth}"), lines[0]
    assert "token=" not in res.stderr, "no log field is named token"


def test_empty_token_counts_as_unset(box: Box) -> None:
    res = box.run(HF_TOKEN=EMPTY)
    assert res.rc == 0 and res.secret_creates() == [] and only_the_stale_secret_cleanup(res)


def test_a_secret_left_by_a_previous_run_does_not_break_the_run_and_is_removed(box: Box) -> None:
    res = box.run(HF_TOKEN=TOKEN, FAKE_SECRET_EXISTS=SWITCH_ON)
    assert res.rc == 0, res.stderr
    assert box.state()["secret"] is False


def test_secret_is_deleted_when_the_job_fails(box: Box) -> None:
    res = box.run(HF_TOKEN=TOKEN, FAKE_JOB_RESULT="failed")
    assert res.rc != 0
    assert res.secret_creates() and res.secret_deletes()
    assert box.state()["secret"] is False
    assert TOKEN not in res.stdout + res.stderr + res.argv_text()


def test_secret_is_deleted_when_the_job_cannot_be_applied(box: Box, tmp_path: Path) -> None:
    """kubectl apply of the Job fails after the Secret exists: the trap still removes the Secret."""
    kubectl = box.bin / "kubectl"
    kubectl.write_text(kubectl.read_text().replace('if verbs[:1] == ["apply"]:', 'if verbs[:1] == ["apply"] and ("kind: Job" in pl or \'"kind": "Job"\' in pl):\n    sys.stderr.write("error: forbidden\\n")\n    sys.exit(1)\nif verbs[:1] == ["apply"]:', 1))
    res = box.run(HF_TOKEN=TOKEN)
    assert res.rc != 0
    assert res.secret_creates() and res.secret_deletes()
    assert box.state()["secret"] is False


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGINT], ids=["TERM", "INT"])
def test_secret_is_deleted_when_the_script_is_interrupted_while_waiting(box: Box, sig: int) -> None:
    proc = box.start(HF_TOKEN=TOKEN, FAKE_JOB_RESULT="hang")
    try:
        box.wait_for_waiting(proc)
        assert box.state()["secret"] is True
        res = box.kill_group(proc, sig)
    finally:
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGKILL)
    assert res.rc != 0
    assert res.secret_deletes(), "the trap must delete the Secret on INT/TERM"
    assert box.state()["secret"] is False
    assert TOKEN not in res.stdout + res.stderr + res.argv_text()


def test_no_token_means_the_wait_for_the_job_does_not_need_a_secret_even_if_interrupted(box: Box) -> None:
    proc = box.start(FAKE_JOB_RESULT="hang")
    try:
        box.wait_for_waiting(proc)
        res = box.kill_group(proc, signal.SIGTERM)
    finally:
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGKILL)
    assert res.secret_creates() == [] and only_the_stale_secret_cleanup(res), "the trap deletes nothing this run did not create"


def test_script_source_never_inlines_a_token_on_a_command_line() -> None:
    text = SCRIPT.read_text()
    assert "--from-literal" not in text, "--from-literal puts the value in argv"
    assert not re.search(r"(?m)^\s*(echo|printf|log|die)\b(?![^\n]*[>|])[^\n]*\$\{?HF_TOKEN", text), "never print or log the token"
    assert "set -x" not in text and "xtrace" not in text
