# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""``scripts/teardown.sh``: the documented teardown order, automated.

Contract (docs/DEPLOY.md, section Teardown):

* Dry run by default: every command is printed, none is run (no ``kubectl``, ``aws`` or ``terraform`` call at all) and
  the output says ``DRY-RUN``; ``--yes`` executes.
* Cluster run (default): (1) ``kubectl delete dynamographdeployments.nvidia.com --all -n <ns>`` for every namespace of
  ``SCENARIO_NAMESPACES`` in ``scripts/lib/common.sh``, in that order; (2) ``kubectl delete nodeclaims
  -l karpenter.sh/nodepool=<pool>`` for every pool of ``GPU_POOLS`` (``name:gpus:efa,...``) of the generated env, or
  for each ``--pool gpu-...`` instead; every ``cpu-demo`` NodeClaim whose node runs only DaemonSet (or finished) pods
  is deleted by name, a busy one never; then it polls until no node and no NodeClaim of those GPU pools is left,
  up to ``--timeout`` seconds, and a timeout stops the run before step 3; (3) ``terraform -chdir=<repo>/terraform/cluster
  destroy`` runs only with ``--destroy-cluster --yes`` (no ``-auto-approve``: Terraform still asks), otherwise it is
  printed.
* ``--after-destroy`` (the cluster destroy is done): (4) ``aws s3 rm s3://<RESULTS_BUCKET> --recursive`` then every
  object version and delete marker through ``aws s3api list-object-versions`` + ``delete-objects`` in batches of at most
  1000 until the listing is empty (an ``Errors`` answer stops the run); the model bucket only with ``--empty-models``;
  (5) the bootstrap destroy is printed, never run.  No kubectl call in this run.
* Safety: ``KUBE_CONTEXT`` must name the ``CLUSTER_NAME`` of the generated env (the guard of ``lib/common.sh``), the run
  is off-cluster only, the generated env (``PLATFORM_ENV``, first line the ``gen_env.py`` header) must exist, bucket
  names, region and pools come from that file only (never from the caller's environment), and nothing outside the
  named namespaces, pools and buckets is deleted.  Usage errors exit 2 before any call.
* The end of every run prints the checklist: no ENI left in the VPC, the 50 GiB Prometheus volume gone, a Capacity
  Block or ODCR bills until its end.
* After a failed or partial ``terraform apply`` of the cluster there is no generated env (``gen_env.py`` refuses
  without the ``platform_env`` output).  Without it the run still refuses, and names the fallback:
  ``--from-terraform`` reads the values instead from ``terraform -chdir=<repo>/terraform/cluster output -json``
  (``cluster_name``, ``region``, ``gpu_pools``, ``results_bucket_name``, ``model_bucket_name``) and, for
  ``--after-destroy``, from ``terraform -chdir=<repo>/terraform/bootstrap output -json`` (``results_bucket``,
  ``model_storage_bucket``); a value no
  output holds (after the cluster destroy: the cluster name and the region) comes
  from ``TEARDOWN_CLUSTER_NAME``, ``TEARDOWN_AWS_REGION``, ``TEARDOWN_RESULTS_BUCKET`` or ``TEARDOWN_MODEL_BUCKET``.
  Sources that disagree are refused, the ambient ``CLUSTER_NAME``/``AWS_REGION``/``*_BUCKET`` are never read, the
  ``TEARDOWN_*`` variables only with the flag, and with nothing that names the cluster it refuses.  Those
  ``terraform output -json`` reads are the only calls of its dry run.

The fakes below (``kubectl``, ``aws``, ``terraform``) model one small cluster and two buckets in
``world.json``; the harness of ``lib_scripts`` supplies the call log and the fake clock (``sleep``/``date``).
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import lib_scripts as S
import pytest
import q38_scripts as T

SCRIPT = "teardown.sh"
SCRIPT_PATH = S.SCRIPTS_DIR / SCRIPT
DEPLOY = S.REPO_ROOT / "docs" / "DEPLOY.md"
TOOLS = ("kubectl", "aws", "terraform")

CLUSTER = T.CFG["CLUSTER_NAME"]
REGION = T.CFG["AWS_REGION"]
RESULTS = T.CFG["RESULTS_BUCKET"]
MODELS = T.CFG["MODEL_BUCKET"]
GPU_POOL = "gpu-b200-cb"


def _gen_env_header() -> str:
    spec = importlib.util.spec_from_file_location("gen_env_for_teardown", S.REPO_ROOT / "scripts" / "gen_env.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return str(mod.HEADER)


HEADER = _gen_env_header()


def _namespaces_of_common_sh() -> list[str]:
    m = re.search(r'^SCENARIO_NAMESPACES="([^"]+)"', (S.SCRIPTS_DIR / "lib" / "common.sh").read_text(), re.M)
    assert m, "SCENARIO_NAMESPACES in scripts/lib/common.sh"
    return m.group(1).split()


NAMESPACES = _namespaces_of_common_sh()

# ----------------------------------------------------------------------------- the fakes
STUB = r'''#!__PYTHON__
import fcntl, json, os, sys
from pathlib import Path

D = Path(os.environ["QA_STUB_DIR"])
NAME = os.path.basename(sys.argv[0])
ARGV = sys.argv[1:]


def locked(path, fn):
    with open(path, "a+") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        fh.seek(0)
        raw = fh.read()
        data = json.loads(raw) if raw.strip() else {}
        res = fn(data)
        fh.seek(0); fh.truncate(); fh.write(json.dumps(data))
        return res


def log():
    def upd(st):
        st["seq"] = st.get("seq", 0) + 1
        return st["seq"]
    seq = locked(D / "state.json", upd)
    with open(D / "calls.jsonl", "a") as fh:
        fh.write(json.dumps({"seq": seq, "tool": NAME, "argv": ARGV}) + "\n")


def opt(argv, *names):
    for i, a in enumerate(argv):
        for n in names:
            if a == n and i + 1 < len(argv):
                return argv[i + 1]
            if a.startswith(n + "="):
                return a.split("=", 1)[1]
    return None


def out(obj):
    sys.stdout.write(json.dumps(obj))
    return 0


def fail(msg, rc=1):
    sys.stderr.write(msg + "\n")
    return rc


def visible(w, pool):
    """Claims of ``pool``; a deleted claim lingers for ``linger`` reads (never when ``stuck``)."""
    keep, shown = [], []
    for c in w["nodeclaims"]:
        if c["pool"] == pool and c.get("deleting") and not c.get("stuck"):
            if c.get("linger", 0) <= 0:
                continue
            c["linger"] = c.get("linger", 0) - 1
        keep.append(c)
        if c["pool"] == pool:
            shown.append(c)
    w["nodeclaims"] = keep
    return shown


def claim_obj(c):
    meta = {"name": c["name"], "labels": {"karpenter.sh/nodepool": c["pool"]}}
    if c.get("deleting"):
        meta["deletionTimestamp"] = "2026-10-26T00:00:00Z"
    return {"apiVersion": "karpenter.sh/v1", "kind": "NodeClaim", "metadata": meta, "status": {"nodeName": c["node"]}}


def node_obj(c):
    return {"apiVersion": "v1", "kind": "Node", "metadata": {"name": c["node"], "labels": {"karpenter.sh/nodepool": c["pool"]}}}


def pod_obj(p):
    meta = {"name": p["name"], "namespace": p["ns"]}
    if p.get("owner"):
        meta["ownerReferences"] = [{"kind": p["owner"], "name": p["name"] + "-owner"}]
    return {"apiVersion": "v1", "kind": "Pod", "metadata": meta, "status": {"phase": p.get("phase", "Running")}}


def kubectl(w, argv):
    argv = [a for a in argv if not a.startswith("--context=")]
    words = [a for a in argv if not a.startswith("-")]
    verb, res = (words + ["", ""])[:2]
    sel = opt(argv, "-l", "--selector") or ""
    pool = sel.split("=", 1)[1] if sel.startswith("karpenter.sh/nodepool=") else None
    if res == "dynamographdeployments.nvidia.com" and verb == "delete" and "--all" in argv:
        ns = opt(argv, "-n", "--namespace")
        for name in w["dgds"].pop(ns, []):
            print('dynamographdeployment.nvidia.com "%s" deleted' % name)
        return 0
    if res in ("nodeclaims", "nodeclaim") and verb == "get" and pool:
        return out({"apiVersion": "v1", "kind": "List", "items": [claim_obj(c) for c in visible(w, pool)]})
    if res in ("nodeclaims", "nodeclaim") and verb == "delete":
        targets = [c for c in w["nodeclaims"] if (c["pool"] == pool if pool else c["name"] in words[2:])]
        for c in targets:
            c["deleting"] = True
            print('nodeclaim.karpenter.sh "%s" deleted' % c["name"])
        return 0
    if res == "nodes" and verb == "get" and pool:
        return out({"apiVersion": "v1", "kind": "List", "items": [node_obj(c) for c in visible(w, pool)]})
    if res == "pods" and verb == "get" and "-A" in argv:
        field = opt(argv, "--field-selector") or ""
        node = field.split("=", 1)[1] if field.startswith("spec.nodeName=") else None
        return out({"apiVersion": "v1", "kind": "List", "items": [pod_obj(p) for p in w["pods"].get(node, [])]})
    return fail("teardown fake: unmodelled kubectl call: %s" % " ".join(argv))


def aws(w, argv):
    if argv[:2] == ["s3", "rm"] and len(argv) > 2 and argv[2].startswith("s3://"):
        name = argv[2][5:].split("/", 1)[0]
        b = w["buckets"].get(name)
        if b is None:
            return fail("fatal error: An error occurred (NoSuchBucket): %s" % name)
        for key in b["current"]:
            print("delete: s3://%s/%s" % (name, key))
            if b["versioned"]:
                b["markers"].append([key, "dm-" + key])
            else:
                b["versions"] = [v for v in b["versions"] if v[0] != key]
        b["current"] = []
        return 0
    if argv[:2] == ["s3api", "list-object-versions"]:
        b = w["buckets"].get(opt(argv, "--bucket"))
        if b is None:
            return fail("An error occurred (NoSuchBucket)")
        combined = [("v", k, v) for k, v in b["versions"]] + [("m", k, v) for k, v in b["markers"]]
        page = combined[: w.get("page_size", 1000)]
        doc = {"IsTruncated": len(page) < len(combined), "Name": opt(argv, "--bucket")}
        vs = [{"Key": k, "VersionId": v, "IsLatest": False} for t, k, v in page if t == "v"]
        ms = [{"Key": k, "VersionId": v, "IsLatest": True} for t, k, v in page if t == "m"]
        if vs:
            doc["Versions"] = vs
        if ms:
            doc["DeleteMarkers"] = ms
        return out(doc)
    if argv[:2] == ["s3api", "delete-objects"]:
        name = opt(argv, "--bucket")
        b = w["buckets"].get(name)
        spec = opt(argv, "--delete") or ""
        if b is None or not spec.startswith("file://"):
            return fail("An error occurred (NoSuchBucket) or no --delete file://")
        objs = json.loads(Path(spec[7:]).read_text())["Objects"]
        w["batches"].append({"bucket": name, "n": len(objs)})
        if not objs or len(objs) > 1000:
            return fail("An error occurred (MalformedXML): 1 to 1000 objects per request", 254)
        if w.get("delete_errors"):
            return out({"Errors": [{"Key": objs[0]["Key"], "VersionId": objs[0]["VersionId"], "Code": "AccessDenied", "Message": "Access Denied"}]})
        gone = {(o["Key"], o["VersionId"]) for o in objs}
        b["versions"] = [v for v in b["versions"] if (v[0], v[1]) not in gone]
        b["markers"] = [m for m in b["markers"] if (m[0], m[1]) not in gone]
        return 0
    return fail("teardown fake: unmodelled aws call: %s" % " ".join(argv))


def terraform(w, argv):
    """``output -json`` prints ``tf_outputs[<basename of -chdir>]`` ({} when absent; "error" = an uninitialised backend)."""
    if argv[1:] == ["output", "-json"]:
        chdir = argv[0].split("=", 1)[1] if argv[0].startswith("-chdir=") else ""
        outs = w.get("tf_outputs", {}).get(os.path.basename(chdir.rstrip("/")))
        if outs == "error":
            return fail('Error: Backend initialization required, please run "terraform init"')
        return out(outs or {})
    return int(w.get("terraform_rc", 0))


def main():
    log()
    def act(w):
        if NAME == "kubectl":
            return kubectl(w, ARGV)
        if NAME == "aws":
            return aws(w, ARGV)
        return terraform(w, ARGV)
    return locked(D / "world.json", act)


sys.exit(main())
'''


def versioned_bucket(keys: int, extra_versions: int, old_markers: int) -> dict[str, Any]:
    """A versioned bucket: ``keys`` current objects with 2 versions each, ``extra_versions`` more of key 0, old markers."""
    names = [f"runs/r{i:04d}.json" for i in range(keys)]
    versions = [[k, f"v{n}-{i}"] for i, k in enumerate(names) for n in (1, 2)]
    versions += [[names[0], f"v{n}-x"] for n in range(3, 3 + extra_versions)]
    markers = [[f"old/k{i:04d}", f"m{i}"] for i in range(old_markers)]
    return {"versioned": True, "current": names, "versions": versions, "markers": markers}


def plain_bucket(keys: int) -> dict[str, Any]:
    names = [f"Qwen/model/shard-{i:03d}.safetensors" for i in range(keys)]
    return {"versioned": False, "current": names, "versions": [[k, "null"] for k in names], "markers": []}


def world(**over: Any) -> dict[str, Any]:
    """One GPU NodeClaim, a busy and an empty cpu-demo NodeClaim, cells in two namespaces and the two buckets."""
    w: dict[str, Any] = {
        "dgds": {"s-agg": ["q38"], "s-dis-hic": ["q38"]},
        "nodeclaims": [
            {"name": "gpu-b200-cb-x7k2p", "pool": GPU_POOL, "node": "i-0gpu000000000001", "linger": 1},
            {"name": "cpu-demo-busy", "pool": "cpu-demo", "node": "i-0cpu000000000001"},
            {"name": "cpu-demo-empty", "pool": "cpu-demo", "node": "i-0cpu000000000002"},
        ],
        "pods": {
            "i-0cpu000000000001": [
                {"name": "prometheus-0", "ns": "monitoring", "owner": "StatefulSet"},
                {"name": "node-exporter-a", "ns": "monitoring", "owner": "DaemonSet"},
            ],
            "i-0cpu000000000002": [
                {"name": "node-exporter-b", "ns": "monitoring", "owner": "DaemonSet"},
                {"name": "prepull-b", "ns": "node-ops", "owner": "DaemonSet"},
                {"name": "model-stage-abc", "ns": "node-ops", "owner": "Job", "phase": "Succeeded"},
            ],
        },
        "buckets": {RESULTS: versioned_bucket(600, 3, 250), MODELS: plain_bucket(12)},
        "page_size": 1000,
        "batches": [],
    }
    w.update(over)
    return w


def env_file(tmp: Path, header: bool = True, **over: str | None) -> Path:
    """The generated env: the placeholder config with keys replaced (value) or removed (None)."""
    keys = dict(T.CFG)
    for k, v in over.items():
        if v is None:
            keys.pop(k, None)
        else:
            keys[k] = v
    path = tmp / "platform-generated.env"
    path.write_text((HEADER + "\n" if header else "") + "".join(f"{k}={v}\n" for k, v in sorted(keys.items())))
    return path


class Box:
    """A sandbox with the teardown fakes on PATH and its world."""

    def __init__(self, tmp: Path, w: dict[str, Any] | None = None) -> None:
        self.tmp = tmp
        self.sb = S.Sandbox(tmp / "sb", {})
        core = self.sb.stub_dir / "_teardown_fake"
        core.write_text(STUB.replace("__PYTHON__", sys.executable))
        core.chmod(0o755)
        for name in TOOLS:
            link = self.sb.stub_dir / name
            if link.is_symlink() or link.exists():
                link.unlink()
            link.symlink_to(core.name)
        self.world_path = self.sb.state_dir / "world.json"
        self.world_path.write_text(json.dumps(w if w is not None else world()))
        self.env = {"PLATFORM_ENV": str(env_file(tmp)), "KUBE_CONTEXT": CLUSTER, "KUBERNETES_SERVICE_HOST": ""}

    def run(self, *args: str, env: dict[str, str] | None = None) -> S.Result:
        return self.sb.run(SCRIPT, *args, env={**self.env, **(env or {})})

    def world(self) -> dict[str, Any]:
        return json.loads(self.world_path.read_text())


def tool_calls(res: S.Result, *tools: str) -> list[dict[str, Any]]:
    return [c for c in res.calls if c["tool"] in (tools or TOOLS)]


def words(call: dict[str, Any]) -> list[str]:
    return [a for a in call["argv"] if not a.startswith("-")]


def kube(res: S.Result, verb: str, resource: str) -> list[dict[str, Any]]:
    return [c for c in tool_calls(res, "kubectl") if words(c)[:2] == [verb, resource]]


def out(res: S.Result) -> str:
    return res.stdout + res.stderr


# ----------------------------------------------------------------------------- static
def test_script_is_an_executable_strict_bash_script_with_the_header() -> None:
    assert SCRIPT_PATH.is_file(), f"missing scripts/{SCRIPT}"
    assert os.access(SCRIPT_PATH, os.X_OK), f"scripts/{SCRIPT} is not executable"
    text = SCRIPT_PATH.read_text()
    lines = text.splitlines()
    assert lines[0] == "#!/usr/bin/env bash"
    head = lines[:6]
    assert "# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved." in head
    assert "# SPDX-License-Identifier: MIT-0" in head
    assert "set -euo pipefail" in text
    assert 'source "$SCRIPT_DIR/lib/common.sh"' in text, "reuse lib/common.sh (kube wrapper, guard, SCENARIO_NAMESPACES)"
    assert "require_kube_target" in text
    proc = subprocess.run(["bash", "-n", str(SCRIPT_PATH)], capture_output=True, text=True, check=False)
    assert proc.returncode == 0, proc.stderr


def test_script_is_portable_to_bash_3_2() -> None:
    text = "\n".join(ln for ln in SCRIPT_PATH.read_text().splitlines() if not ln.lstrip().startswith("#"))
    for pat in (r"\bmapfile\b", r"\breadarray\b", r"\b(?:declare|local|typeset) -A\b", r"\$\{\w+(?:,,|\^\^)\}",
                r"\bsetsid\b", r"&>>", r"\|&", r"\bcoproc\b", r"\bwait -n\b"):
        assert not re.search(pat, text), f"bash 4+ construct {pat!r} in scripts/{SCRIPT}"


def test_cluster_destroy_keeps_the_terraform_prompt() -> None:
    assert "-auto-approve" not in SCRIPT_PATH.read_text(), "--yes is not a reason to skip Terraform's own confirmation"


# ----------------------------------------------------------------------------- refusals
REFUSE_MODES = [("--yes",), ("--after-destroy", "--yes")]


@pytest.mark.parametrize("args", REFUSE_MODES, ids=["cluster", "after-destroy"])
def test_refuses_without_kube_context(tmp_path: Path, args: tuple[str, ...]) -> None:
    res = Box(tmp_path).run(*args, env={"KUBE_CONTEXT": ""})
    assert res.rc != 0 and "KUBE_CONTEXT" in out(res)
    assert tool_calls(res) == []


@pytest.mark.parametrize("args", REFUSE_MODES, ids=["cluster", "after-destroy"])
def test_refuses_a_context_of_another_cluster(tmp_path: Path, args: tuple[str, ...]) -> None:
    res = Box(tmp_path).run(*args, env={"KUBE_CONTEXT": "arn:aws:eks:us-east-2:123456789012:cluster/another-cluster"})
    assert res.rc != 0 and CLUSTER in out(res)
    assert tool_calls(res) == []


def test_refuses_to_run_in_cluster(tmp_path: Path) -> None:
    res = Box(tmp_path).run("--yes", env={"KUBERNETES_SERVICE_HOST": "10.96.0.1"})
    assert res.rc != 0
    assert tool_calls(res) == []


def test_refuses_when_the_generated_env_is_missing(tmp_path: Path) -> None:
    res = Box(tmp_path).run("--yes", env={"PLATFORM_ENV": str(tmp_path / "absent" / "platform.env")})
    assert res.rc != 0 and "gen_env.py" in out(res)
    assert tool_calls(res) == []


def test_refuses_an_env_that_gen_env_did_not_write(tmp_path: Path) -> None:
    res = Box(tmp_path).run("--after-destroy", "--yes", env={"PLATFORM_ENV": str(env_file(tmp_path, header=False))})
    assert res.rc != 0 and "gen_env.py" in out(res)
    assert tool_calls(res) == []


@pytest.mark.parametrize(
    ("args", "drop"),
    [(("--yes",), "CLUSTER_NAME"), (("--yes",), "GPU_POOLS"), (("--after-destroy", "--yes"), "RESULTS_BUCKET"),
     (("--after-destroy", "--empty-models", "--yes"), "MODEL_BUCKET"), (("--after-destroy", "--yes"), "AWS_REGION")],
    ids=["no-cluster", "no-pools", "no-results-bucket", "no-model-bucket", "no-region"],
)
def test_refuses_an_env_without_the_keys_it_needs(tmp_path: Path, args: tuple[str, ...], drop: str) -> None:
    res = Box(tmp_path).run(*args, env={"PLATFORM_ENV": str(env_file(tmp_path, **{drop: None}))})
    assert res.rc != 0 and drop in out(res)
    assert tool_calls(res) == []


@pytest.mark.parametrize(
    "args",
    [("--bogus",), ("extra",), ("--pool",), ("--timeout",), ("--empty-models",), ("--after-destroy", "--destroy-cluster")],
    ids=["unknown", "positional", "pool-no-value", "timeout-no-value", "models-without-after-destroy", "after-and-destroy"],
)
def test_usage_errors_exit_2_before_any_call(tmp_path: Path, args: tuple[str, ...]) -> None:
    res = Box(tmp_path).run("--yes", *args)
    assert res.rc == 2, out(res)
    assert tool_calls(res) == []


@pytest.mark.parametrize(
    "args",
    [("--timeout", "0"), ("--timeout", "1m"), ("--timeout", "-5"), ("--pool", "cpu-demo"), ("--pool", "gpu-x;rm"),
     ("--pool", "GPU-B200")],
    ids=["timeout-zero", "timeout-suffix", "timeout-negative", "pool-cpu-demo", "pool-semicolon", "pool-uppercase"],
)
def test_bad_values_are_refused_before_any_call(tmp_path: Path, args: tuple[str, ...]) -> None:
    res = Box(tmp_path).run("--yes", *args)
    assert res.rc != 0, out(res)
    assert tool_calls(res) == []


# ----------------------------------------------------------------------------- dry run
@pytest.mark.parametrize(
    "args",
    [(), ("--destroy-cluster",), ("--pool", "gpu-h100-od"), ("--after-destroy",), ("--after-destroy", "--empty-models")],
    ids=["cluster", "destroy-cluster", "explicit-pool", "after-destroy", "after-destroy-models"],
)
def test_dry_run_is_the_default_and_runs_nothing(tmp_path: Path, args: tuple[str, ...]) -> None:
    box = Box(tmp_path)
    before = box.world()
    res = box.run(*args)
    assert res.rc == 0, out(res)
    assert tool_calls(res) == [], [c["argv"] for c in tool_calls(res)]
    assert box.world() == before
    assert "DRY-RUN" in res.stdout


def test_cluster_dry_run_prints_every_cluster_command(tmp_path: Path) -> None:
    res = Box(tmp_path).run("--destroy-cluster")
    assert res.rc == 0, out(res)
    text = res.stdout
    for ns in NAMESPACES:
        assert f"--context={CLUSTER} delete dynamographdeployments.nvidia.com --all -n {ns}" in text, ns
    assert f"delete nodeclaims -l karpenter.sh/nodepool={GPU_POOL}" in text
    assert "get nodeclaims -l karpenter.sh/nodepool=cpu-demo" in text
    assert "terraform/cluster destroy" in text and "-chdir=" in text
    assert text.find("dynamographdeployments") < text.find("delete nodeclaims") < text.find("terraform/cluster destroy")


def test_after_destroy_dry_run_prints_the_bucket_commands(tmp_path: Path) -> None:
    res = Box(tmp_path).run("--after-destroy")
    assert res.rc == 0, out(res)
    text = res.stdout
    assert f"aws s3 rm s3://{RESULTS} --recursive --region {REGION}" in text
    assert f"list-object-versions --bucket {RESULTS}" in text and "delete-objects" in text
    assert not [ln for ln in text.splitlines() if "s3 rm" in ln and MODELS in ln], "the model bucket needs --empty-models"
    assert "terraform/bootstrap destroy" in text
    with_models = Box(tmp_path / "m").run("--after-destroy", "--empty-models")
    assert f"aws s3 rm s3://{MODELS} --recursive --region {REGION}" in with_models.stdout


# ----------------------------------------------------------------------------- --yes: the cluster steps
def test_yes_runs_the_documented_order(tmp_path: Path) -> None:
    box = Box(tmp_path)
    res = box.run("--destroy-cluster", "--yes")
    assert res.rc == 0, out(res)
    dgd = kube(res, "delete", "dynamographdeployments.nvidia.com")
    assert [T.ns_of(c["argv"]) for c in dgd] == [[ns] for ns in NAMESPACES], "every scenario namespace, in order"
    assert all("--all" in c["argv"] for c in dgd)
    gpu = kube(res, "delete", "nodeclaims")
    assert [S.arg_value(c["argv"], "-l") for c in gpu] == [f"karpenter.sh/nodepool={GPU_POOL}"]
    assert min(c["seq"] for c in gpu) > max(c["seq"] for c in dgd), "cells first, then the GPU NodeClaims"
    cpu = kube(res, "delete", "nodeclaim")
    assert [words(c)[2:] for c in cpu] == [["cpu-demo-empty"]], "only the cpu-demo NodeClaim with DaemonSet pods only"
    assert min(c["seq"] for c in cpu) > max(c["seq"] for c in dgd)
    polls = [c for c in kube(res, "get", "nodeclaims") if S.arg_value(c["argv"], "-l") == f"karpenter.sh/nodepool={GPU_POOL}"]
    assert polls and min(c["seq"] for c in polls) > min(c["seq"] for c in gpu)
    tf = tool_calls(res, "terraform")
    assert len(tf) == 1 and tf[0]["argv"][1:] == ["destroy"], tf
    assert Path(tf[0]["argv"][0].removeprefix("-chdir=")).resolve() == (S.REPO_ROOT / "terraform" / "cluster").resolve()
    assert tf[0]["seq"] > max(c["seq"] for c in tool_calls(res, "kubectl")), "the destroy comes after the GPU nodes are gone"
    assert tool_calls(res, "aws") == [], "the buckets wait for --after-destroy"
    w = box.world()
    assert w["dgds"] == {}
    assert [c["name"] for c in w["nodeclaims"] if c["pool"] == GPU_POOL] == []
    assert "cpu-demo-busy" in [c["name"] for c in w["nodeclaims"] if not c.get("deleting")]
    assert "DRY-RUN" not in res.stdout


def test_yes_without_destroy_cluster_never_runs_terraform(tmp_path: Path) -> None:
    res = Box(tmp_path).run("--yes")
    assert res.rc == 0, out(res)
    assert tool_calls(res, "terraform") == []
    assert "terraform/cluster destroy" in res.stdout, "the command is printed for the user"
    assert "--after-destroy" in res.stdout, "the next step is named"


@pytest.mark.parametrize("args", [("--destroy-cluster",), ("--after-destroy", "--yes"), ("--after-destroy", "--empty-models", "--yes")],
                         ids=["destroy-without-yes", "after-destroy", "after-destroy-models"])
def test_terraform_never_runs_without_destroy_cluster_and_yes(tmp_path: Path, args: tuple[str, ...]) -> None:
    res = Box(tmp_path).run(*args)
    assert res.rc == 0, out(res)
    assert tool_calls(res, "terraform") == []


def test_every_kubectl_call_carries_the_context(tmp_path: Path) -> None:
    res = Box(tmp_path).run("--yes", env={"KUBE_CONTEXT": T.ARN})
    assert res.rc == 0, out(res)
    calls = tool_calls(res, "kubectl")
    assert calls
    for c in calls:
        assert c["argv"][0] == f"--context={T.ARN}", c["argv"]


def test_deletes_stay_inside_the_named_namespaces_and_pools(tmp_path: Path) -> None:
    res = Box(tmp_path).run("--destroy-cluster", "--yes")
    assert res.rc == 0, out(res)
    for c in [c for c in tool_calls(res, "kubectl") if words(c)[:1] == ["delete"]]:
        argv, w = c["argv"], words(c)
        assert "-A" not in argv and "--all-namespaces" not in argv, argv
        if w[1] == "dynamographdeployments.nvidia.com":
            assert T.ns_of(argv) and T.ns_of(argv)[0] in NAMESPACES, argv
        elif w[1] == "nodeclaims":
            assert S.arg_value(argv, "-l") == f"karpenter.sh/nodepool={GPU_POOL}", argv
        elif w[1] == "nodeclaim":
            assert w[2:] == ["cpu-demo-empty"], argv
        else:
            pytest.fail(f"unexpected delete: {argv}")


def test_every_gpu_pool_of_the_env_is_drained(tmp_path: Path) -> None:
    pools = ("gpu-b200-od", "gpu-l40s-od")
    claims = [{"name": f"{p}-a1", "pool": p, "node": f"i-0{n}", "linger": 1} for n, p in enumerate(pools)]
    box = Box(tmp_path, world(nodeclaims=claims))
    res = box.run("--yes", env={"PLATFORM_ENV": str(env_file(tmp_path, GPU_POOLS="gpu-b200-od:8:8,gpu-l40s-od:1:0"))})
    assert res.rc == 0, out(res)
    assert sorted(S.arg_value(c["argv"], "-l") for c in kube(res, "delete", "nodeclaims")) == [
        f"karpenter.sh/nodepool={p}" for p in pools]
    assert box.world()["nodeclaims"] == []


def test_an_explicit_pool_replaces_the_env_pools(tmp_path: Path) -> None:
    claims = [{"name": "gpu-h100-od-a1", "pool": "gpu-h100-od", "node": "i-0h100", "linger": 0},
              {"name": "gpu-b200-cb-a1", "pool": GPU_POOL, "node": "i-0b200"}]
    box = Box(tmp_path, world(nodeclaims=claims))
    res = box.run("--yes", "--pool", "gpu-h100-od")
    assert res.rc == 0, out(res)
    assert [S.arg_value(c["argv"], "-l") for c in kube(res, "delete", "nodeclaims")] == ["karpenter.sh/nodepool=gpu-h100-od"]
    assert [c["name"] for c in box.world()["nodeclaims"]] == ["gpu-b200-cb-a1"]


def test_the_wait_polls_until_no_gpu_node_is_left(tmp_path: Path) -> None:
    claims = [{"name": "gpu-b200-cb-slow", "pool": GPU_POOL, "node": "i-0slow", "linger": 6}]
    box = Box(tmp_path, world(nodeclaims=claims))
    res = box.run("--destroy-cluster", "--yes")
    assert res.rc == 0, out(res)
    assert [c for c in res.calls if c["tool"] == "sleep"], "the loop sleeps between polls"
    assert box.world()["nodeclaims"] == []
    assert len(tool_calls(res, "terraform")) == 1


def test_a_gpu_node_that_stays_stops_the_run_before_the_destroy(tmp_path: Path) -> None:
    claims = [{"name": "gpu-b200-cb-stuck", "pool": GPU_POOL, "node": "i-0stuck", "stuck": True}]
    box = Box(tmp_path, world(nodeclaims=claims))
    res = box.run("--destroy-cluster", "--yes", "--timeout", "60")
    assert res.rc != 0
    assert re.search(r"(?i)timeout|timed out|after 60", out(res)), out(res)
    assert box.sb.clock() >= 60, "the wait honours --timeout on the fake clock"
    assert tool_calls(res, "terraform") == []


# ----------------------------------------------------------------------------- --after-destroy: the buckets
def _bucket_names(res: S.Result) -> set[str]:
    names = set()
    for c in tool_calls(res, "aws"):
        b = S.arg_value(c["argv"], "--bucket")
        if b:
            names.add(b)
        names |= {a[5:].split("/", 1)[0] for a in c["argv"] if a.startswith("s3://")}
    return names


def _assert_emptied(box: Box, res: S.Result, bucket: str, total: int) -> None:
    w = box.world()
    b = w["buckets"][bucket]
    assert (b["current"], b["versions"], b["markers"]) == ([], [], []), "every version and delete marker is gone"
    batches = [x["n"] for x in w["batches"] if x["bucket"] == bucket]
    assert batches and all(1 <= n <= 1000 for n in batches), batches
    assert sum(batches) == total
    rm = [c for c in tool_calls(res, "aws") if c["argv"][:3] == ["s3", "rm", f"s3://{bucket}"]]
    assert len(rm) == 1 and "--recursive" in rm[0]["argv"]
    lists = [c for c in tool_calls(res, "aws") if c["argv"][:2] == ["s3api", "list-object-versions"]
             and S.arg_value(c["argv"], "--bucket") == bucket]
    assert lists and rm[0]["seq"] < min(c["seq"] for c in lists), "aws s3 rm first, then the versions"


def test_after_destroy_empties_the_versioned_results_bucket_in_batches(tmp_path: Path) -> None:
    box = Box(tmp_path)
    models_before = box.world()["buckets"][MODELS]
    res = box.run("--after-destroy", "--yes")
    assert res.rc == 0, out(res)
    # 600 keys x 2 versions + 3, 250 old markers, 600 markers added by `s3 rm`: 2053 entries, more than two batches.
    _assert_emptied(box, res, RESULTS, 2053)
    assert len([x for x in box.world()["batches"] if x["bucket"] == RESULTS]) >= 3
    assert box.world()["buckets"][MODELS] == models_before, "the model bucket needs --empty-models"
    assert _bucket_names(res) == {RESULTS}
    assert tool_calls(res, "kubectl") == [] and tool_calls(res, "terraform") == []
    for c in tool_calls(res, "aws"):
        assert S.arg_value(c["argv"], "--region") == REGION, c["argv"]  # the harness' ambient AWS_REGION is eu-west-3
    assert "terraform/bootstrap destroy" in res.stdout


def test_batches_never_exceed_1000_even_when_a_listing_holds_more(tmp_path: Path) -> None:
    box = Box(tmp_path, world(page_size=5000))
    res = box.run("--after-destroy", "--yes")
    assert res.rc == 0, out(res)
    _assert_emptied(box, res, RESULTS, 2053)


def test_empty_models_also_empties_the_model_bucket(tmp_path: Path) -> None:
    box = Box(tmp_path)
    res = box.run("--after-destroy", "--empty-models", "--yes")
    assert res.rc == 0, out(res)
    _assert_emptied(box, res, RESULTS, 2053)
    b = box.world()["buckets"][MODELS]
    assert (b["current"], b["versions"], b["markers"]) == ([], [], [])
    assert _bucket_names(res) == {RESULTS, MODELS}


def test_bucket_names_come_only_from_the_generated_env(tmp_path: Path) -> None:
    decoys = {"env-results-override": versioned_bucket(3, 0, 0), "env-models-override": plain_bucket(2)}
    box = Box(tmp_path, world(buckets={RESULTS: versioned_bucket(5, 0, 1), MODELS: plain_bucket(2), **decoys}))
    res = box.run("--after-destroy", "--empty-models", "--yes",
                  env={"RESULTS_BUCKET": "env-results-override", "MODEL_BUCKET": "env-models-override"})
    assert res.rc == 0, out(res)
    assert _bucket_names(res) == {RESULTS, MODELS}
    for name, before in decoys.items():
        assert box.world()["buckets"][name] == before, f"{name} is not the bucket of the generated env"


def test_a_delete_error_stops_the_run(tmp_path: Path) -> None:
    res = Box(tmp_path, world(delete_errors=True)).run("--after-destroy", "--yes")
    assert res.rc != 0
    assert "AccessDenied" in out(res) or "Errors" in out(res) or "delete-objects" in out(res), out(res)
    assert len([c for c in tool_calls(res, "aws") if c["argv"][:2] == ["s3api", "delete-objects"]]) == 1


SYNC = f'aws s3 sync "s3://{RESULTS}/perf/" ./results --region {REGION}'


def _reminder(text: str) -> list[str]:
    """The lines that warn about the benchmark results under perf/ and give the command that copies them."""
    return [ln for ln in text.splitlines() if "WARNING" in ln and f"s3://{RESULTS}" in ln and SYNC in ln]


def test_after_destroy_warns_about_the_results_under_perf_before_it_empties_the_bucket(tmp_path: Path) -> None:
    """benchmarks/perf.yaml uploads the results under perf/ of the results bucket, and this run deletes every object
    version of that bucket: it says so, with the command that copies them, before the first delete."""
    res = Box(tmp_path).run("--after-destroy", "--yes")
    assert res.rc == 0, out(res)
    lines = _reminder(res.stdout)
    assert len(lines) == 1, res.stdout
    assert 0 <= res.stdout.find(lines[0]) < res.stdout.find(f"+ aws s3 rm s3://{RESULTS} "), "the warning comes first"


@pytest.mark.parametrize("mode", ["after-destroy-dry-run", "cluster-dry-run", "cluster", "from-terraform"])
def test_the_dry_run_and_the_next_step_hint_carry_the_same_reminder(tmp_path: Path, mode: str) -> None:
    warning = _reminder(Box(tmp_path / "ref").run("--after-destroy", "--yes").stdout)
    assert len(warning) == 1
    if mode == "after-destroy-dry-run":
        res = Box(tmp_path).run("--after-destroy")
        assert res.rc == 0, out(res)
        assert _reminder(res.stdout) == warning, res.stdout
        assert res.stdout.find(warning[0]) < res.stdout.find(f"[dry-run] aws s3 rm s3://{RESULTS} "), "before the plan of step 5"
        return
    box = fallback_box(tmp_path) if mode == "from-terraform" else Box(tmp_path)
    res = box.run(*{"cluster-dry-run": (), "cluster": ("--yes",), "from-terraform": ("--from-terraform",)}[mode])
    assert res.rc == 0, out(res)
    lines = res.stdout.splitlines()
    hint = [i for i, ln in enumerate(lines) if ln.startswith("next, once the cluster destroy has finished:")
            and "--after-destroy" in ln and "--yes" in ln]
    assert len(hint) == 1, res.stdout
    assert any(warning[0] in ln for ln in lines[hint[0]:hint[0] + 2]), "the hint line, or the line after it, carries the reminder"


# ----------------------------------------------------------------------------- --from-terraform: no generated env
def tf_outputs(**values: Any) -> dict[str, Any]:
    """What ``terraform output -json`` prints for these outputs."""
    return {k: {"sensitive": False, "type": "string" if isinstance(v, str) else "dynamic", "value": v}
            for k, v in values.items()}


# The cluster state after a failed apply seen on a real cluster: 27 of 29 outputs, no platform_env.
PARTIAL_CLUSTER = tf_outputs(cluster_name=CLUSTER, region=REGION, gpu_pools={GPU_POOL: {"gpus_per_node": 8}},
                             results_bucket_name=RESULTS, model_bucket_name=MODELS, vpc_id="vpc-0123456789abcdef0")
BOOTSTRAP = tf_outputs(model_storage_bucket=MODELS, results_bucket=RESULTS, model_storage_bucket_arn=f"arn:aws:s3:::{MODELS}",
                       results_bucket_arn=f"arn:aws:s3:::{RESULTS}")
DESTROYED: dict[str, Any] = {}  # terraform destroy leaves no output in the state


def fallback_box(tmp: Path, cluster: Any = None, bootstrap: Any = None, **over: Any) -> Box:
    """A box with no generated env and the given terraform outputs (default: the partial cluster and the bootstrap)."""
    outs = {"cluster": PARTIAL_CLUSTER if cluster is None else cluster, "bootstrap": BOOTSTRAP if bootstrap is None else bootstrap}
    box = Box(tmp, world(tf_outputs=outs, **over))
    box.env["PLATFORM_ENV"] = str(tmp / "absent" / "platform.env")
    return box


def output_reads(res: S.Result) -> list[str]:
    """The stacks whose ``terraform output -json`` the run read; fails on any other terraform call."""
    stacks = []
    for c in tool_calls(res, "terraform"):
        assert c["argv"][1:] == ["output", "-json"], f"only terraform output -json reads: {c['argv']}"
        stacks.append(Path(c["argv"][0].removeprefix("-chdir=")).resolve().relative_to(S.REPO_ROOT.resolve()).as_posix())
    return stacks


def test_without_the_generated_env_the_refusal_names_the_fallback(tmp_path: Path) -> None:
    res = Box(tmp_path).run("--yes", env={"PLATFORM_ENV": str(tmp_path / "absent" / "platform.env")})
    assert res.rc != 0
    assert "gen_env.py" in out(res) and "--from-terraform" in out(res), out(res)
    assert tool_calls(res) == []


def test_from_terraform_dry_run_reads_only_the_cluster_outputs(tmp_path: Path) -> None:
    box = fallback_box(tmp_path)
    before = box.world()
    res = box.run("--from-terraform")
    assert res.rc == 0, out(res)
    assert output_reads(res) == ["terraform/cluster"]
    assert tool_calls(res, "kubectl", "aws") == []
    assert box.world() == before
    assert "DRY-RUN" in res.stdout
    assert f"delete nodeclaims -l karpenter.sh/nodepool={GPU_POOL}" in res.stdout, "the pool comes from gpu_pools"
    assert re.search(r"(?i)terraform output", res.stdout), "the run says where its values come from"


def test_from_terraform_runs_the_cluster_steps_with_the_outputs(tmp_path: Path) -> None:
    box = fallback_box(tmp_path)
    res = box.run("--from-terraform", "--yes", env={"KUBE_CONTEXT": T.ARN})
    assert res.rc == 0, out(res)
    assert output_reads(res) == ["terraform/cluster"], "no destroy without --destroy-cluster"
    assert [T.ns_of(c["argv"]) for c in kube(res, "delete", "dynamographdeployments.nvidia.com")] == [[ns] for ns in NAMESPACES]
    assert [S.arg_value(c["argv"], "-l") for c in kube(res, "delete", "nodeclaims")] == [f"karpenter.sh/nodepool={GPU_POOL}"]
    assert all(c["argv"][0] == f"--context={T.ARN}" for c in tool_calls(res, "kubectl"))
    assert box.world()["dgds"] == {}
    nxt = res.stdout[res.stdout.find("next"):]
    for needle in ("--after-destroy", "--from-terraform", f"TEARDOWN_CLUSTER_NAME={CLUSTER}", f"TEARDOWN_AWS_REGION={REGION}"):
        assert needle in nxt, f"the next step, once the destroy removed the cluster outputs, names {needle}"


def test_from_terraform_keeps_the_context_guard(tmp_path: Path) -> None:
    res = fallback_box(tmp_path).run("--from-terraform", "--yes", env={"KUBE_CONTEXT": "another-cluster"})
    assert res.rc != 0 and CLUSTER in out(res)
    assert tool_calls(res, "kubectl", "aws") == []


@pytest.mark.parametrize("args", [("--from-terraform", "--yes"), ("--after-destroy", "--from-terraform", "--yes")],
                         ids=["cluster", "after-destroy"])
def test_from_terraform_refuses_when_nothing_names_the_cluster(tmp_path: Path, args: tuple[str, ...]) -> None:
    res = fallback_box(tmp_path, cluster=DESTROYED, bootstrap=DESTROYED).run(*args)
    assert res.rc != 0
    assert "CLUSTER_NAME" in out(res) and "TEARDOWN_CLUSTER_NAME" in out(res), out(res)
    assert tool_calls(res, "kubectl", "aws") == []


def test_an_unreadable_state_counts_as_no_outputs(tmp_path: Path) -> None:
    res = fallback_box(tmp_path, cluster="error").run("--from-terraform", "--yes")
    assert res.rc != 0 and "CLUSTER_NAME" in out(res)
    assert "terraform init" in out(res), "the terraform error reaches the operator"
    assert tool_calls(res, "kubectl", "aws") == []


def test_after_destroy_from_terraform_empties_the_buckets_of_the_bootstrap_outputs(tmp_path: Path) -> None:
    box = fallback_box(tmp_path, cluster=DESTROYED)
    res = box.run("--after-destroy", "--from-terraform", "--yes",
                  env={"TEARDOWN_CLUSTER_NAME": CLUSTER, "TEARDOWN_AWS_REGION": REGION})
    assert res.rc == 0, out(res)
    assert sorted(output_reads(res)) == ["terraform/bootstrap", "terraform/cluster"]
    _assert_emptied(box, res, RESULTS, 2053)
    assert _bucket_names(res) == {RESULTS}
    assert all(S.arg_value(c["argv"], "--region") == REGION for c in tool_calls(res, "aws"))
    assert tool_calls(res, "kubectl") == []


def test_after_destroy_from_terraform_dry_run_reads_only_the_outputs(tmp_path: Path) -> None:
    box = fallback_box(tmp_path, cluster=DESTROYED)
    before = box.world()
    res = box.run("--after-destroy", "--from-terraform", "--empty-models",
                  env={"TEARDOWN_CLUSTER_NAME": CLUSTER, "TEARDOWN_AWS_REGION": REGION})
    assert res.rc == 0, out(res)
    assert tool_calls(res, "kubectl", "aws") == []
    assert box.world() == before
    assert f"aws s3 rm s3://{RESULTS} --recursive --region {REGION}" in res.stdout
    assert f"aws s3 rm s3://{MODELS} --recursive --region {REGION}" in res.stdout


def test_after_destroy_from_terraform_needs_the_region_once_the_cluster_outputs_are_gone(tmp_path: Path) -> None:
    res = fallback_box(tmp_path, cluster=DESTROYED).run("--after-destroy", "--from-terraform", "--yes",
                                                         env={"TEARDOWN_CLUSTER_NAME": CLUSTER})
    assert res.rc != 0 and "AWS_REGION" in out(res) and "TEARDOWN_AWS_REGION" in out(res), out(res)
    assert tool_calls(res, "kubectl", "aws") == []


@pytest.mark.parametrize(
    ("cluster", "bootstrap", "env", "key"),
    [(PARTIAL_CLUSTER, tf_outputs(results_bucket="another-results-bucket", model_storage_bucket=MODELS), {}, "RESULTS_BUCKET"),
     (PARTIAL_CLUSTER, BOOTSTRAP, {"TEARDOWN_CLUSTER_NAME": "another-cluster"}, "CLUSTER_NAME"),
     (DESTROYED, BOOTSTRAP, {"TEARDOWN_CLUSTER_NAME": CLUSTER, "TEARDOWN_AWS_REGION": REGION,
                             "TEARDOWN_MODEL_BUCKET": "another-models-bucket"}, "MODEL_BUCKET")],
    ids=["outputs-disagree", "variable-vs-output", "variable-vs-bootstrap"],
)
def test_sources_that_disagree_are_refused(tmp_path: Path, cluster: Any, bootstrap: Any, env: dict[str, str], key: str) -> None:
    res = fallback_box(tmp_path, cluster=cluster, bootstrap=bootstrap).run(
        "--after-destroy", "--from-terraform", "--empty-models", "--yes", env={"KUBE_CONTEXT": CLUSTER, **env})
    assert res.rc != 0 and key in out(res), out(res)
    assert tool_calls(res, "kubectl", "aws") == []


def test_from_terraform_never_reads_the_ambient_environment(tmp_path: Path) -> None:
    decoys = {"env-results-override": versioned_bucket(3, 0, 0), "env-models-override": plain_bucket(2)}
    box = fallback_box(tmp_path, buckets={RESULTS: versioned_bucket(5, 0, 1), MODELS: plain_bucket(2), **decoys})
    res = box.run("--after-destroy", "--from-terraform", "--empty-models", "--yes",
                  env={"RESULTS_BUCKET": "env-results-override", "MODEL_BUCKET": "env-models-override",
                       "CLUSTER_NAME": "env-cluster", "AWS_REGION": "eu-west-3"})
    assert res.rc == 0, out(res)
    assert _bucket_names(res) == {RESULTS, MODELS}
    assert all(S.arg_value(c["argv"], "--region") == REGION for c in tool_calls(res, "aws"))
    for name, before in decoys.items():
        assert box.world()["buckets"][name] == before


def test_teardown_variables_are_ignored_without_from_terraform(tmp_path: Path) -> None:
    decoys = {"teardown-results-decoy": versioned_bucket(3, 0, 0)}
    box = Box(tmp_path, world(buckets={RESULTS: versioned_bucket(5, 0, 1), MODELS: plain_bucket(2), **decoys}))
    res = box.run("--after-destroy", "--yes", env={"TEARDOWN_RESULTS_BUCKET": "teardown-results-decoy"})
    assert res.rc == 0, out(res)
    assert _bucket_names(res) == {RESULTS}
    assert tool_calls(res, "terraform") == []
    assert box.world()["buckets"]["teardown-results-decoy"] == decoys["teardown-results-decoy"]


@pytest.mark.parametrize(
    ("cluster", "env", "args"),
    [(tf_outputs(cluster_name=CLUSTER, region=REGION, gpu_pools={"cpu-demo": {}}), {}, ("--from-terraform", "--yes")),
     (DESTROYED, {"TEARDOWN_CLUSTER_NAME": CLUSTER, "TEARDOWN_AWS_REGION": REGION, "TEARDOWN_RESULTS_BUCKET": "Bad_Bucket"},
      ("--after-destroy", "--from-terraform", "--yes")),
     (DESTROYED, {"TEARDOWN_CLUSTER_NAME": CLUSTER, "TEARDOWN_AWS_REGION": "us-east-2;rm"},
      ("--after-destroy", "--from-terraform", "--yes")),
     (DESTROYED, {"TEARDOWN_CLUSTER_NAME": "a b", "KUBE_CONTEXT": "a b", "TEARDOWN_AWS_REGION": REGION},
      ("--after-destroy", "--from-terraform", "--yes"))],
    ids=["pool-not-gpu", "bad-bucket", "bad-region", "bad-cluster-name"],
)
def test_from_terraform_values_pass_the_same_checks(tmp_path: Path, cluster: Any, env: dict[str, str], args: tuple[str, ...]) -> None:
    res = fallback_box(tmp_path, cluster=cluster).run(*args, env=env)
    assert res.rc == 1, out(res)  # a refused value, not a usage error (2)
    assert tool_calls(res, "kubectl", "aws") == []


def test_from_terraform_without_gpu_pools_needs_an_explicit_pool(tmp_path: Path) -> None:
    cluster = tf_outputs(cluster_name=CLUSTER, region=REGION)
    res = fallback_box(tmp_path, cluster=cluster).run("--from-terraform", "--yes")
    assert res.rc != 0 and "--pool" in out(res) and "gpu_pools" in out(res), out(res)
    assert tool_calls(res, "kubectl", "aws") == []
    ok = fallback_box(tmp_path / "p", cluster=cluster).run("--from-terraform", "--yes", "--pool", GPU_POOL)
    assert ok.rc == 0, out(ok)


# ----------------------------------------------------------------------------- the checklist
@pytest.mark.parametrize("args", [(), ("--yes",), ("--after-destroy", "--yes")], ids=["dry-run", "cluster", "after-destroy"])
def test_every_run_ends_with_the_checklist(tmp_path: Path, args: tuple[str, ...]) -> None:
    res = Box(tmp_path).run(*args)
    assert res.rc == 0, out(res)
    tail = res.stdout[res.stdout.rfind("checklist"):]
    assert "checklist" in res.stdout
    assert "ENI" in tail and "VPC" in tail
    assert re.search(r"50 GiB", tail) and "Prometheus" in tail
    assert "Capacity Block" in tail and "ODCR" in tail


# ----------------------------------------------------------------------------- docs/DEPLOY.md, section Teardown
def _teardown_section() -> str:
    text = DEPLOY.read_text()
    m = re.search(r"^## Teardown\s*$", text, re.M)
    assert m, "docs/DEPLOY.md keeps its '## Teardown' section (TROUBLESHOOTING.md links #teardown)"
    rest = text[m.end():]
    end = re.search(r"^#{1,2} ", rest, re.M)
    return rest[: end.start()] if end else rest


def test_deploy_teardown_describes_the_script_first_and_keeps_the_manual_order() -> None:
    sec = _teardown_section()
    first = sec.find("scripts/teardown.sh")
    assert first >= 0, "the Teardown section describes scripts/teardown.sh"
    assert first < sec.find("kubectl delete"), "the script comes first, the manual order is the fallback"
    for flag in ("--yes", "--destroy-cluster", "--after-destroy", "--empty-models", "--pool", "--timeout"):
        assert flag in sec, f"the Teardown section names {flag}"
    assert re.search(r"(?i)dry run|dry-run", sec)
    manual = sec[first:]
    order = ["kubectl delete", "karpenter.sh/nodepool", "terraform/cluster", "terraform destroy", "aws s3 rm",
             "terraform/bootstrap"]
    pos = -1
    for token in order:
        nxt = manual.find(token, pos + 1)
        assert nxt > pos, f"manual fallback order broken at {token!r}"
        pos = nxt


def test_deploy_teardown_documents_the_failed_apply_fallback() -> None:
    """After a failed or partial cluster apply there is no platform.env: the section says so, gives the
    --from-terraform runs (the TEARDOWN_* values once the destroy removed the cluster outputs) and how to empty the
    buckets by hand, versions and delete markers included."""
    sec = _teardown_section()
    flat = " ".join(sec.split())
    assert re.search(r"(?i)(failed|partial)[^.]*apply|apply[^.]*(failed|partial)", flat), "the case of a failed apply"
    for needle in ("--from-terraform", "TEARDOWN_CLUSTER_NAME", "TEARDOWN_AWS_REGION", "terraform output",
                   "list-object-versions", "delete-objects", "DeleteMarkers"):
        assert needle in sec, f"the Teardown section names {needle!r}"
    script = SCRIPT_PATH.read_text()
    for var in re.findall(r"TEARDOWN_[A-Z_]+", sec):
        assert var in script, f"{var} is not read by scripts/{SCRIPT}"


# ----------------------------------------------------------------------------- the numbering
def test_the_steps_are_numbered_out_of_five_in_the_script_and_in_the_deploy_section() -> None:
    script = SCRIPT_PATH.read_text()
    steps = re.findall(r'^\s*step "(\d)/(\d) ([^"]*)"', script, re.M)
    assert sorted((n, m) for n, m, _ in steps) == [(str(i), "5") for i in range(1, 6)], steps
    assert "destroy the cluster" in {n: t for n, _, t in steps}["3"], "step 3 destroys the cluster"
    assert re.findall(r"^#\s+(\d)\. ", script, re.M) == [str(i) for i in range(1, 6)], "the header lists the five steps"
    sec = _teardown_section()
    for needle in ("# dry run of steps 1 to 3", "# steps 1 and 2, then prints the cluster destroy command",
                   "# steps 1 to 3; Terraform still asks", "# step 4 for the results bucket, then prints step 5",
                   "# step 4, bucket names from terraform/bootstrap"):
        assert needle in sec, f"the Teardown section numbers the steps as the script: {needle!r}"
