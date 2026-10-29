# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Guard: a Hugging Face token lives in the process environment only, and nothing the sample generates persists it.

One token-shaped dummy (built from fragments at run time, so no token-shaped literal exists in the repository) is
followed end to end through the four places a token could be written:

1. the files ``scripts/gen_env.py`` generates (``platform.env``, ``sample.env``), with the token exported in the shell,
   left by hand in an older ``sample.env``, or carried by a Terraform output (``HF_TOKEN`` is in ``FORBIDDEN_KEYS``);
2. what ``scripts/10-stage-model.sh`` renders (``--dry-run``) and applies, its stdout and stderr, every kubectl argv and
   payload, and every file under its ``TMPDIR``, ``HOME`` and working directory, also under ``bash -x`` and
   ``SHELLOPTS=xtrace``; the only allowed sink is the stdin of the one ``kubectl create secret`` call;
3. every tracked Terraform file: no variable can hold the token value (only ``hf_token_secret_arn``, a reference),
   no resource, data source or output reads or carries it, and the one IAM grant names only that ARN;
4. the argv of the staging Job (container command and args) and of the staging tool (it starts no child process).

Besides the raw value, every check also looks for the base64 encodings of the token (a Kubernetes Secret's ``data``).

Existing tests this file deliberately does not repeat (they keep running in their own suites):

* ``tests/scripts/test_150_stage_model_env.py``: ``HF_TOKEN`` in a Terraform output is refused and nothing is written
  (here: the refusal also leaves earlier generated files byte-identical and no temporary file behind);
* ``tests/scripts/test_151_stage_model_script.py``: the raw token never reaches argv, stdout, stderr, the dry-run or
  a temporary file in a plain run, the shell trace never prints it, the Job reads it through an optional
  ``secretKeyRef`` and the Secret is deleted on every exit path (here: the same sinks under the traces, the base64
  forms, the files under HOME and the working directory, and the Job argv);
* ``tests/scripts/test_152_stage_model_hygiene.py``: no tracked file holds a token-shaped string;
* ``tests/scripts/test_153_stage_model_token_env_only.py``: a token in ``sample.env`` is ignored by the script;
* ``tests/terraform/test_cluster_static.py`` and ``test_model_stager_static.py``, ``model_stager.tftest.hcl``: no
  ``aws_secretsmanager_secret_version`` in ``terraform/cluster``, ``GetSecretValue`` is never on ``*``, the stager
  role has no Secrets Manager access (here: every stack, the exact grant, variables, outputs and Kubernetes Secrets).
"""

from __future__ import annotations

import base64
import importlib
import re
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import lib_proc
import pytest

REPO = Path(__file__).resolve().parents[2]


def _helper(name: str, suite: str) -> ModuleType:
    """Import a helper module of another test suite (its directory goes on sys.path, as pytest does for that suite).

    Args:
        name: Module name.
        suite: Directory under ``tests/`` that holds it.

    Returns:
        The imported module.
    """
    path = str(REPO / "tests" / suite)
    if path not in sys.path:
        sys.path.insert(0, path)
    return importlib.import_module(name)


hcl = _helper("hcl", "terraform")  # comment stripping and block extraction of the static Terraform tests
H = _helper("stage_model_harness", "scripts")  # fake kubectl that records argv and payloads, isolated run box
G = _helper("test_140_gen_env", "scripts")  # gen_env.py runner and the terraform output fixtures

TOKEN = "hf_" + "A" * 34  # token-shaped dummy, never a real credential
TRACES = ("plain", "bash -x", "SHELLOPTS=xtrace")
GEN_CLUSTER = G.platform_env()["CLUSTER_NAME"]  # the KUBE_CONTEXT require_kube_target accepts for the generated env


def forms(secret: str) -> list[str]:
    """Return the raw secret and the core of its base64 encoding at each of the three byte alignments.

    A value embedded in a larger base64 blob (a Secret's ``data``) shows one of these cores whatever precedes it.

    Args:
        secret: The value to look for.

    Returns:
        The raw value followed by three base64 substrings.
    """
    out = [secret]
    for pad in range(3):
        enc = base64.b64encode(b"\0" * pad + secret.encode()).decode()
        out.append(enc[4 if pad else 0 : -4])
    return out


def leaks(text: str) -> list[str]:
    """Return the forms of TOKEN found in ``text`` (empty when none)."""
    return [f for f in forms(TOKEN) if f in text]


def files_holding_the_token(root: Path, *, skip: tuple[Path, ...] = ()) -> list[str]:
    """Return the files under ``root`` (outside ``skip``) whose bytes hold any form of TOKEN.

    Args:
        root: Directory to scan recursively.
        skip: Directories excluded from the scan (the fake kubectl's own call recorder).

    Returns:
        Paths relative to ``root``.
    """
    hits = []
    for p in sorted(root.rglob("*")):
        if not p.is_file() or any(p.is_relative_to(s) for s in skip):
            continue
        if leaks(p.read_bytes().decode("utf-8", errors="replace")):
            hits.append(str(p.relative_to(root)))
    return hits


def stage(box: H.Box, mode: list[str], trace: str, **env: str | None) -> H.Run:
    """Run scripts/10-stage-model.sh in ``box`` with HF_TOKEN set, optionally under a shell trace.

    Args:
        box: The isolated run box of the harness.
        mode: ``["--dry-run"]`` or ``[]`` (apply against the fake kubectl).
        trace: One of TRACES.
        **env: Extra environment (None removes a variable).

    Returns:
        The run (exit code, stdout, stderr, recorded kubectl calls).
    """
    box.reset()
    environ = box.env(HF_TOKEN=TOKEN, PS4="+ ", KUBERNETES_SERVICE_HOST=None, **env)
    argv = [str(H.SCRIPT), *mode]
    if trace == "bash -x":
        argv = ["bash", "-x", *argv]
    elif trace == "SHELLOPTS=xtrace":
        environ["SHELLOPTS"] = "xtrace"
    proc = lib_proc.run(argv, cwd=box.root / "cwd", env=environ, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=90, check=False)
    res = H.Run(proc.returncode, proc.stdout, proc.stderr, box.calls(), box.tmp)
    if trace != "plain":
        assert re.search(r"(?m)^\++ ", proc.stderr), "the trace must be on when the script starts, else the check is vacuous"
    return res


def job_argv(docs: list[dict[str, Any]]) -> list[str]:
    """Return command + args of every container of the one Job in ``docs``."""
    jobs = [d for d in docs if d.get("kind") == "Job"]
    assert len(jobs) == 1, [d.get("kind") for d in docs]
    conts = jobs[0]["spec"]["template"]["spec"]["containers"]
    return [str(a) for c in conts for a in [*c.get("command", []), *c.get("args", [])]]


@pytest.fixture
def box(tmp_path: Path) -> H.Box:
    return H.Box(tmp_path / "box")


# ------------------------------------------------------------------------------------------------- (1) gen_env.py files
def test_gen_env_never_copies_a_shell_token_into_the_generated_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HF_TOKEN", TOKEN)
    monkeypatch.setenv("HUGGING_FACE_HUB_TOKEN", TOKEN)
    proc, root = G.run(tmp_path, G.tf_output())
    assert proc.returncode == 0, proc.stderr
    plat, samp = G.outputs(root)
    for path in (plat, samp):
        assert not leaks(path.read_text()), f"{path.name} holds the token"
        assert "HF_TOKEN" not in G.parse_env(path), f"{path.name} has an HF_TOKEN key"
    assert not leaks(proc.stdout + proc.stderr)
    assert files_holding_the_token(tmp_path) == []
    assert sorted(p.name for p in plat.parent.iterdir()) == ["platform.env"] and sorted(p.name for p in samp.parent.iterdir()) == ["sample.env"], "no temporary file is left next to the outputs"


def test_regenerating_drops_a_token_written_by_hand_into_sample_env(tmp_path: Path) -> None:
    _, samp = G.outputs(tmp_path / "repo")
    samp.parent.mkdir(parents=True)
    samp.write_text(f"MODEL_ID=x/y\nHF_TOKEN={TOKEN}\n")
    proc, root = G.run(tmp_path, G.tf_output())
    assert proc.returncode == 0, proc.stderr
    assert not leaks(G.outputs(root)[1].read_text()), "sample.env is generated and overwritten: a hand-written token must not survive"


@pytest.mark.parametrize("where", ["platform_env", "sample_env"])
def test_a_token_output_is_refused_and_earlier_files_stay_byte_identical(tmp_path: Path, where: str) -> None:
    proc, root = G.run(tmp_path, G.tf_output())
    assert proc.returncode == 0, proc.stderr
    before = {p: p.read_bytes() for p in G.outputs(root)}
    bad = G.tf_output(G.platform_env(HF_TOKEN=TOKEN), None) if where == "platform_env" else G.tf_output(None, G.sample_env(HF_TOKEN=TOKEN))
    proc, _ = G.run(tmp_path, bad)
    assert proc.returncode != 0 and "HF_TOKEN" in proc.stderr
    assert not leaks(proc.stdout + proc.stderr), "a refusal names the key, never the value"
    assert {p: p.read_bytes() for p in G.outputs(root)} == before, "a refusal rewrites nothing"
    assert files_holding_the_token(root) == [], "no temporary file holds the token"


# --------------------------------------------------------------------------------------- (2) 10-stage-model.sh renders
@pytest.mark.parametrize("trace", TRACES)
def test_dry_run_renders_no_secret_and_no_sink_holds_the_token(box: H.Box, trace: str) -> None:
    res = stage(box, ["--dry-run"], trace)
    assert res.rc == 0, res.stderr
    assert sorted(d["kind"] for d in res.stdout_docs()) == ["ConfigMap", "Job"], "the dry-run never renders the Secret"
    assert not leaks(res.stdout), "rendered objects"
    assert not leaks(res.stderr), "stderr (log and trace)"
    assert not leaks(res.argv_text()) and not any(leaks(c["payload"]) for c in res.calls), "kubectl argv and payloads"
    assert files_holding_the_token(box.root, skip=(box.fake,)) == [], "files under TMPDIR, HOME or the working directory"


@pytest.mark.parametrize("trace", TRACES)
def test_apply_hands_the_token_to_one_secret_create_on_stdin_and_nowhere_else(box: H.Box, trace: str) -> None:
    res = stage(box, [], trace)
    assert res.rc == 0, res.stderr
    creates = res.secret_creates()
    assert len(creates) == 1 and leaks(res.calls[creates[0]]["payload"]), "the token reaches kubectl once, on stdin"
    assert not leaks(res.argv_text()), "no kubectl argv (visible in ps) holds the token"
    others = [c["argv"] for i, c in enumerate(res.calls) if i != creates[0] and leaks(c["payload"])]
    assert others == [], f"only the Secret create may receive the token: {others}"
    assert [d for d in res.applied_docs() if d.get("kind") == "Secret"] == [], "the Secret is never part of an applied manifest"
    assert not leaks(res.stdout + res.stderr)
    assert files_holding_the_token(box.root, skip=(box.fake,)) == []
    assert box.state()["secret"] is False, "the ephemeral Secret is gone when the script exits"


# ------------------------------------------------------------------------------------------- (4) the staging Job argv
@pytest.mark.parametrize("mode", [["--dry-run"], []], ids=["dry-run", "apply"])
def test_the_staging_job_argv_never_carries_the_token(box: H.Box, mode: list[str]) -> None:
    res = stage(box, mode, "plain")
    assert res.rc == 0, res.stderr
    argv = job_argv(res.stdout_docs() if mode else res.applied_docs())
    assert argv, "the Job has a command"
    assert not leaks(" ".join(argv)) and not any("HF_TOKEN" in a for a in argv), argv


def test_the_staging_tool_starts_no_child_process() -> None:
    """The token stays in the Job's environment: stage_model.py hands it to the Hub client, never to a process argv."""
    text = (REPO / "tools" / "stage_model" / "stage_model.py").read_text()
    for pat in (r"\bsubprocess\b", r"\bos\.(system|popen|exec\w*|spawn\w*|posix_spawn\w*)\b", r"\bpty\b", r"\bPopen\b"):
        assert not re.search(pat, text), f"stage_model.py must not start a child process ({pat})"


def test_end_to_end_gen_env_then_staging_keeps_the_token_in_the_process_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, box: H.Box) -> None:
    monkeypatch.setenv("HF_TOKEN", TOKEN)
    proc, _ = G.run(tmp_path, G.tf_output(), "--platform-out", str(box.platform_env), "--sample-out", str(box.sample_env))
    assert proc.returncode == 0, proc.stderr
    for mode in (["--dry-run"], []):
        res = stage(box, mode, "plain", KUBE_CONTEXT=GEN_CLUSTER)
        assert res.rc == 0, res.stderr
        assert not leaks(res.stdout + res.stderr + res.argv_text())
        allowed = res.secret_creates()
        assert len(allowed) == (0 if mode else 1)
        assert not any(leaks(c["payload"]) for i, c in enumerate(res.calls) if i not in allowed)
    assert files_holding_the_token(tmp_path, skip=(box.fake,)) == [], "no generated, rendered or temporary file holds it"


# ------------------------------------------------------------------------------------------------------- (3) Terraform
def _tracked_terraform() -> dict[str, str]:
    """Return ``{path: comment-stripped text}`` of every tracked .tf, .tfvars(.example) and .tftest.hcl file."""
    out = lib_proc.run(["git", "-C", str(REPO), "ls-files", "-z", "terraform"], capture_output=True, check=True).stdout.decode()
    rels = [r for r in out.split("\0") if re.search(r"\.(tf|tfvars|tfvars\.example|tftest\.hcl)$", r)]
    assert any(r.endswith("/variables.tf") for r in rels), "the Terraform stacks are tracked"
    return {r: hcl.strip_comments((REPO / r).read_text()) for r in rels if (REPO / r).is_file()}


# A credential-like name part (``workload_context_tokens`` counts LLM tokens and is not one).
TOKENISH = re.compile(r"(?i)(^|_)(hf|hugging\w*|token|secret|password|api_key)(_|$)")
# A Secrets Manager action as an IAM policy string (not the ARN validation regex or a description).
SM_ACTION = re.compile(r'"secretsmanager:[A-Za-z*]+"')


def test_no_terraform_variable_can_hold_the_token_value() -> None:
    names = {}
    for rel, text in _tracked_terraform().items():
        if rel.endswith(".tf"):
            for name, body in hcl.blocks(text, "variable").items():
                names[name] = (rel, body)
    tokenish = sorted(n for n in names if TOKENISH.search(n))
    assert tokenish == ["hf_token_secret_arn"], f"the only token-related variable is the Secrets Manager reference: {tokenish}"
    rel, body = names["hf_token_secret_arn"]
    assert rel == "terraform/cluster/variables.tf"
    assert re.search(r"(?m)^\s*default\s*=\s*null\s*$", body), "no default value"
    assert re.search(r"(?m)^\s*type\s*=\s*string\s*$", body)
    assert not re.search(r"(?m)^\s*sensitive\s*=", body), "an ARN is a reference, not a secret value"
    assert "secretsmanager" in body, "the validation accepts a Secrets Manager ARN only"
    sensitive = sorted(n for n, (_, b) in names.items() if re.search(r"(?m)^\s*sensitive\s*=\s*true", b))
    assert sensitive == [], f"no variable carries a secret value: {sensitive}"


def test_no_tfvars_or_terraform_test_sets_a_token_value() -> None:
    for rel, text in _tracked_terraform().items():
        for m in re.finditer(r"(?m)^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=", text):
            if TOKENISH.search(m.group(1)) and not rel.endswith(".tf"):
                assert m.group(1) == "hf_token_secret_arn", f"{rel}: {m.group(1)} is set"


def test_terraform_never_reads_stores_or_outputs_the_token() -> None:
    tf = _tracked_terraform()
    joined = "\n".join(t for r, t in tf.items() if r.endswith(".tf"))
    assert not re.search(r'(resource|data|ephemeral)\s+"aws_secretsmanager_secret_version"', joined), "the value would enter the state"
    assert not re.search(r'resource\s+"kubernetes_secret(_v1)?"', joined), "the hf-token Secret is ephemeral, created by scripts/10-stage-model.sh"
    for name, body in hcl.blocks(joined, "resource", 2).items():
        if name.startswith("kubectl_manifest."):
            assert not re.search(r"kind:\s*Secret|kind\s*=\s*\"Secret\"", body), f"{name} manages a Secret"
    assert not re.search(r'(?<![A-Za-z0-9_])"?HF_TOKEN"?\s*[=:]', joined), "no output or local map has an HF_TOKEN key (only HF_TOKEN_SECRET_ARN)"
    for name, body in hcl.blocks(joined, "output").items():
        assert not re.search(r"(?m)^\s*sensitive\s*=\s*true", body), f"output {name} is sensitive: no output carries a secret"


def test_the_only_token_grant_is_get_secret_value_on_the_variable_arn() -> None:
    hits = {r: t for r, t in _tracked_terraform().items() if r.endswith(".tf") and SM_ACTION.search(t)}
    assert list(hits) == ["terraform/cluster/iam-model-access.tf"], f"one Secrets Manager grant, in one file: {list(hits)}"
    policies = {n: b for n, b in hcl.blocks(hits["terraform/cluster/iam-model-access.tf"], "resource", 2).items() if SM_ACTION.search(b)}
    assert list(policies) == ["aws_iam_role_policy.model_reader_hf_token"], list(policies)
    body = policies["aws_iam_role_policy.model_reader_hf_token"]
    assert re.search(r"count\s*=\s*var\.hf_token_secret_arn\s*==\s*null\s*\?\s*0\s*:\s*1", body), "no grant without the ARN"
    assert re.search(r"role\s*=\s*aws_iam_role\.model_reader\.id", body), "the read-only prefetch role only"
    assert re.findall(r"Action\s*=\s*\[([^\]]*)\]", body) == ['"secretsmanager:GetSecretValue"'], "GetSecretValue only"
    assert [s.strip() for s in re.findall(r"Resource\s*=\s*\[([^\]]*)\]", body)] == ["var.hf_token_secret_arn"], "that ARN only"
