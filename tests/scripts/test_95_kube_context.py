# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Kube-context guard (FT-4 finding).  RED until implemented.

Why: the scripts act on the *current* kubectl context (the router toggle applies server-side to ``s-*`` namespaces).  On the operator's laptop the current context can be another cluster.  Contract:

* ``scripts/lib/common.sh`` defines a kubectl wrapper that appends ``--context "$KUBE_CONTEXT"``
  when ``KUBE_CONTEXT`` is non-empty; every kubectl call of every script goes through it
  (``kubectl kustomize ...`` and ``--local`` calls are offline and exempt, they may still carry it).
* The flag is ONE argv token, ``--context=<value>``.  Reason: the harness ``kubectl`` stub (and any
  naive parser) takes the first non-dash token as the sub-command, so the two-token form
  ``--context VALUE`` makes ``VALUE`` look like the verb.  It is also valid kubectl syntax.
* Scripts that touch the cluster (``11-prefetch.sh 13-prepull.sh 50-toggle-router.sh``) refuse to run - non-zero exit, a message naming ``KUBE_CONTEXT``, and **no**
  kubectl call and no delegated command - when ``KUBE_CONTEXT`` is unset/empty AND
  ``KUBERNETES_SERVICE_HOST`` is unset/empty.  In-cluster runs are allowed
  without it, and then no ``--context`` flag is added.  ``preflight-flags.sh`` is offline and exempt.
* With the platform config's ``CLUSTER_NAME`` (always, in this sample) the context must name that cluster (name or EKS ARN): the
  guard itself is tested in ``test_130_q38_profile.py``. The "context with spaces stays one argv token" case left with the
  free-form contexts it needed; ``test_every_kubectl_call_carries_the_context`` still pins the single-token ``--context=<value>``.
* "Unset" means unset or empty (the harness cannot remove a variable from a child, it blanks it).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

import lib_scripts as S

NS_COMPS = {
    "s-agg": [("worker-bbb-one", "10.0.1.101", "worker"), ("worker-ccc-two", "10.0.1.102", "worker")],
    "s-agg-hic": [("worker-bbb-one", "10.0.1.101", "worker"), ("worker-ccc-two", "10.0.1.102", "worker")],
    "s-dis": [("prefill-ddd-one", "10.0.2.101", "prefill"), ("decode-eee-one", "10.0.2.102", "decode")],
    "s-dis-hic": [("prefill-ddd-one", "10.0.2.101", "prefill"), ("decode-eee-one", "10.0.2.102", "decode")],
}
CASES: dict[str, tuple[str, ...]] = {
    "50-toggle-router.sh": ("rr", "s-agg"),
    "11-prefetch.sh": (),
    "13-prepull.sh": (),
}
OFF_CLUSTER = {"KUBE_CONTEXT": "", "KUBERNETES_SERVICE_HOST": ""}
CTX = "arn:aws:eks:us-east-2:123456789012:cluster/dynamo-sample-use2"  # the CLUSTER_NAME of the placeholder platform.env, as an EKS ARN


def _sandbox(tmp: Path) -> S.Sandbox:
    pods = {
        ns: [S.pod("fe-frontend-aaa", "10.0.9.201", "frontend", ns)] + [S.pod(n, ip, t, ns) for n, ip, t in comps]
        for ns, comps in NS_COMPS.items()
    }
    return S.Sandbox(tmp, {"pods": pods, "markers": "present"})


def _exempt(call: dict) -> bool:
    argv = call["argv"]
    return "--local" in argv or [a for a in argv if not a.startswith("-")][:1] == ["kustomize"]


def _checked(res: S.Result) -> list[dict]:
    return [c for c in res.kubectl_calls() if not _exempt(c)]


# ----------------------------------------------------------------------------- static
def test_wrapper_defined_in_common_sh() -> None:
    text = (S.SCRIPTS_DIR / "lib" / "common.sh").read_text()
    assert "KUBE_CONTEXT" in text and "--context=" in text, "no --context=<value> wrapper in lib/common.sh"
    bodies = re.findall(r"^\s*(?:function\s+)?\w+\s*\(\)\s*\{\s*\n(.*?)^\}", text, re.S | re.M)
    assert any("kubectl" in b and "--context=" in b for b in bodies), (
        "the wrapper must be a shell function that calls kubectl with --context=<value>"
    )


_BARE = re.compile(r"(^|[|&;({`]|\$\(|\b(?:if|then|do|else|elif|while|until|exec)\b|!)\s*kubectl\b")


def _script_files() -> list[Path]:
    root = S.SCRIPTS_DIR
    files = sorted(root.glob("*.sh")) + sorted((root / "lib").glob("*.sh"))
    return [f for f in files if f.name != "preflight-flags.sh"]


def test_scripts_exist_to_scan() -> None:
    assert [f for f in _script_files() if f.name == "50-toggle-router.sh"], f"no scripts under {S.SCRIPTS_DIR}"


@pytest.mark.parametrize("path", _script_files(), ids=lambda p: p.name)
def test_no_bare_kubectl_call_outside_the_wrapper(path: Path) -> None:
    """Every kubectl invocation goes through the wrapper (offline `kustomize`/`--local` lines exempt)."""
    in_wrapper_file = path.name == "common.sh"
    bad = []
    for n, raw in enumerate(path.read_text().splitlines(), 1):
        line = raw.strip()
        if line.startswith("#") or "kubectl kustomize" in line or "--local" in line:
            continue
        if _BARE.search(line):
            if in_wrapper_file and re.search(r"kubectl\s+(--context|\"\$@\"|\$\{?\w+\}?)", line):
                continue  # the wrapper body itself
            bad.append(f"{path.name}:{n}: {line}")
    assert not bad, "bare kubectl calls:\n" + "\n".join(bad)


# ----------------------------------------------------------------------------- the guard
@pytest.mark.parametrize("script", list(CASES))
def test_refuses_off_cluster_without_context(tmp_path: Path, script: str) -> None:
    res = _sandbox(tmp_path).run(script, *CASES[script], env=OFF_CLUSTER)
    assert res.rc != 0, "must refuse to run without KUBE_CONTEXT outside a cluster"
    assert "KUBE_CONTEXT" in res.stdout + res.stderr, "the message must name KUBE_CONTEXT"
    assert res.kubectl_calls() == [], [c["argv"] for c in res.kubectl_calls()]


@pytest.mark.parametrize("script", list(CASES))
def test_in_cluster_runs_without_context_and_adds_no_flag(tmp_path: Path, script: str) -> None:
    res = _sandbox(tmp_path).run(script, *CASES[script], env={"KUBE_CONTEXT": "", "KUBERNETES_SERVICE_HOST": "10.96.0.1"})
    assert res.rc == 0, res.stdout + res.stderr
    assert res.kubectl_calls()
    for c in res.kubectl_calls():
        assert not S.has_arg(c["argv"], "--context"), c["argv"]


# ----------------------------------------------------------------------------- the flag
@pytest.mark.parametrize("script", list(CASES))
def test_every_kubectl_call_carries_the_context(tmp_path: Path, script: str) -> None:
    res = _sandbox(tmp_path).run(script, *CASES[script], env={"KUBE_CONTEXT": CTX, "KUBERNETES_SERVICE_HOST": ""})
    assert res.rc == 0, res.stdout + res.stderr
    calls = _checked(res)
    assert calls, "no kubectl call to check"
    for c in calls:
        argv = c["argv"]
        assert f"--context={CTX}" in argv, f"missing/incorrect single-token --context=<value> in: {argv}"
        if "--" in argv:
            assert argv.index(f"--context={CTX}") < argv.index("--"), f"--context after the `--` terminator: {argv}"


def test_context_wins_when_both_are_present(tmp_path: Path) -> None:
    res = _sandbox(tmp_path).run("50-toggle-router.sh", "rr", "s-dis", env={"KUBE_CONTEXT": CTX, "KUBERNETES_SERVICE_HOST": "10.96.0.1"})
    assert res.rc == 0, res.stdout + res.stderr
    applies = [c for c in res.kubectl_calls() if "apply" in c["argv"]]
    assert applies and S.arg_value(applies[0]["argv"], "--context") == CTX


def test_preflight_is_offline_and_needs_no_context(tmp_path: Path) -> None:
    help_f = tmp_path / "h.txt"
    help_f.write_text("  --model-path X\n")
    m = tmp_path / "m.yaml"
    m.write_text("apiVersion: v1\nkind: ConfigMap\nmetadata: {name: x}\n")
    res = S.Sandbox(tmp_path / "sb", {}).run("preflight-flags.sh", "--help-file", str(help_f), str(m), env=OFF_CLUSTER)
    assert res.rc == 0, res.stdout + res.stderr
