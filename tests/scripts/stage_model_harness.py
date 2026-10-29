# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Harness of the ``scripts/10-stage-model.sh`` tests: a recording fake ``kubectl`` that models a Secret, a Job and a ConfigMap.

Nothing here talks to AWS or to a cluster. The fake records, per call, its argv and the PAYLOAD it received (stdin plus the
contents of every file the argv names), so a test can tell where a value travelled: argv (visible in ``ps``), stdin or a file.

Environment knobs of the fake: ``FAKE_JOB_RESULT`` (complete | failed | hang), ``FAKE_JOB_PENDING_GETS`` (the first N
``get job`` calls after the apply show no condition yet), ``FAKE_SECRET_EXISTS`` (1: a Secret ``hf-token`` is left over
from an earlier run), ``FAKE_DIR`` (state and call log). Like the real one, ``kubectl wait`` on a condition the Job never
reaches blocks until its ``--timeout``.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import lib_proc
import lib_scripts as S
import yaml

TOKEN = "hf_" + "DUMMYTOKEN" + "0123456789abcdefghijklmnop"  # a dummy, never a real credential
REVISION = "0123456789abcdef0123456789abcdef01234567"
MODEL = "acme/tiny-model"
BUCKET = "demo-models-" + "1" * 12 + "-use2"
PREFIX = f"{MODEL}/{REVISION}"
URI = f"s3://{BUCKET}/{PREFIX}"
IMAGE = "public.ecr.aws/docker/library/python:3.12-slim@sha256:" + "a" * 64
SCRIPT = S.SCRIPTS_DIR / "10-stage-model.sh"
JOB = "model-stage"
# The name of the Kubernetes Secret object (not a credential), from fragments: Bandit B105 reads any literal assigned to
# a name like SECRET as a hard-coded password.
SECRET = "hf" + "-token"
NS = "node-ops"

FAKE_KUBECTL = r'''#!__PYTHON__
import base64, json, os, sys, time
from pathlib import Path

D = Path(os.environ["FAKE_DIR"])
ARGV = sys.argv[1:]
STATE = D / "state.json"


def load():
    return json.loads(STATE.read_text()) if STATE.exists() else {"secret": False, "job": False}


def save(st):
    STATE.write_text(json.dumps(st))


def payload():
    out = []
    if not sys.stdin.isatty():
        try:
            out.append(sys.stdin.read())
        except Exception:
            pass
    for a in ARGV:
        cand = a.split("=", 1)[-1] if a.startswith("-") else a
        if cand and cand != "-" and os.path.isfile(cand):
            try:
                out.append(Path(cand).read_text())
            except Exception:
                pass
    return "\n".join(out)


pl = payload()
with open(D / "calls.jsonl", "a") as fh:
    fh.write(json.dumps({"argv": ARGV, "payload": pl}) + "\n")
SKIP = ("-n", "--namespace", "-o", "--output", "-f", "--filename", "-l", "--selector", "--field-manager", "--timeout", "--for")
verbs, skip = [], False
for a in ARGV:
    if skip:
        skip = False
    elif a in SKIP:
        skip = True
    elif not a.startswith("-"):
        verbs.append(a)
st = load()
result = os.environ.get("FAKE_JOB_RESULT", "complete")
if os.environ.get("FAKE_SECRET_EXISTS") == "1" and not st.get("seeded"):
    st["secret"], st["seeded"] = True, True
    save(st)


def namespace():
    for i, a in enumerate(ARGV):
        if a in ("-n", "--namespace") and i + 1 < len(ARGV):
            return ARGV[i + 1]
        if a.startswith("--namespace="):
            return a.split("=", 1)[1]


if verbs[:2] == ["create", "configmap"]:
    data = {}
    for a in ARGV:
        if a.startswith("--from-file="):
            spec = a.split("=", 1)[1]
            key, _, path = spec.partition("=") if "=" in spec else (os.path.basename(spec), "", spec)
            data[key] = Path(path or spec).read_text()
    meta = {"name": verbs[2]}
    if namespace():
        meta["namespace"] = namespace()
    print(json.dumps({"apiVersion": "v1", "kind": "ConfigMap", "metadata": meta, "data": data}))
    sys.exit(0)
if verbs[:2] == ["create", "secret"]:
    if st["secret"] and not any(a.startswith("--dry-run") for a in ARGV):
        sys.stderr.write('Error from server (AlreadyExists): secrets "hf-token" already exists\n')
        sys.exit(1)
    if any(a.startswith("--dry-run") for a in ARGV):
        print(json.dumps({"apiVersion": "v1", "kind": "Secret", "metadata": {"name": verbs[3] if len(verbs) > 3 else "hf-token"}, "data": {"HF_TOKEN": base64.b64encode(pl.encode()).decode()}}))
        sys.exit(0)
    st["secret"] = True
    save(st)
    sys.exit(0)
if verbs[:1] == ["delete"]:
    if any(v.startswith("secret") for v in verbs[1:]):
        st["secret"] = False
    if any(v.startswith("job") for v in verbs[1:]):
        st["job"] = False
    save(st)
    sys.exit(0)
if verbs[:1] == ["apply"]:
    if '"kind": "Secret"' in pl or "kind: Secret" in pl:
        st["secret"] = True
    if "kind: Job" in pl or '"kind": "Job"' in pl:
        st["job"] = True
    save(st)
    sys.exit(0)
if verbs[:1] == ["get"] and any(v.startswith("job") for v in verbs[1:]):
    if not st["job"]:
        sys.exit(0)
    st["job_gets"] = st.get("job_gets", 0) + 1
    save(st)
    if result == "hang":
        (D / "waiting").write_text("1")
    pending = result == "hang" or st["job_gets"] <= int(os.environ.get("FAKE_JOB_PENDING_GETS", "0"))
    conds = [] if pending else [{"type": "Complete" if result == "complete" else "Failed", "status": "True"}]
    print(json.dumps({"apiVersion": "batch/v1", "kind": "Job", "status": {"conditions": conds}}))
    sys.exit(0)
if verbs[:1] == ["wait"]:
    want = next((a.split("=", 1)[1] for a in ARGV if a.startswith("--for=")), "")
    if result == "hang":
        (D / "waiting").write_text("1")
        for _ in range(300):
            time.sleep(0.1)
        sys.exit(1)
    ok = want.endswith("=" + result) or want.endswith("=" + result.capitalize())
    if not ok:
        raw = next((a.split("=", 1)[1] for a in ARGV if a.startswith("--timeout=")), "30s")
        time.sleep(min(float(raw[:-1]) * 60 if raw.endswith("m") else float(raw.rstrip("s")), 120))
        sys.stderr.write("error: timed out waiting for the condition\n")
    sys.exit(0 if ok else 1)
sys.exit(0)
'''


def _default_sigint() -> None:
    """Popen ``preexec_fn``: the script starts with the default SIGINT even when the test runner ignores it.

    A background job (``pytest ... &`` in a non-interactive shell) starts with SIGINT ignored, its children inherit that,
    and bash cannot trap a signal that was ignored on entry, so the INT-while-waiting test would hang.
    """
    signal.signal(signal.SIGINT, signal.SIG_DFL)


@dataclass
class Run:
    rc: int
    stdout: str
    stderr: str
    calls: list[dict[str, Any]]
    tmpdir: Path
    files: dict[str, Path] = field(default_factory=dict)

    def argv_text(self) -> str:
        return "\n".join(" ".join(c["argv"]) for c in self.calls)

    def is_secret_create(self, c: dict[str, Any]) -> bool:
        a = c["argv"]
        if any(t.startswith("--dry-run") for t in a):
            return False
        return ("create" in a and "secret" in a) or ("apply" in a and ("kind: Secret" in c["payload"] or '"kind": "Secret"' in c["payload"]))

    def secret_creates(self) -> list[int]:
        return [i for i, c in enumerate(self.calls) if self.is_secret_create(c)]

    def secret_deletes(self) -> list[int]:
        return [i for i, c in enumerate(self.calls) if "delete" in c["argv"] and any(t.startswith("secret") for t in c["argv"])]

    def mutating(self) -> list[dict[str, Any]]:
        """Calls other than the offline ``create configmap --dry-run=client`` render."""
        return [c for c in self.calls if not ("create" in c["argv"] and "configmap" in c["argv"] and any(t.startswith("--dry-run") for t in c["argv"]))]

    def applied_docs(self) -> list[dict[str, Any]]:
        docs: list[dict[str, Any]] = []
        for c in self.calls:
            if "apply" in c["argv"] and not self.is_secret_create(c):
                docs += [d for d in yaml.safe_load_all(c["payload"]) if isinstance(d, dict)]
        return docs

    def stdout_docs(self) -> list[dict[str, Any]]:
        return [d for d in yaml.safe_load_all(self.stdout) if isinstance(d, dict)]


class Box:
    """One isolated run directory: fake kubectl on PATH, env files, a private TMPDIR."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.bin = root / "bin"
        self.fake = root / "fake"
        self.tmp = root / "tmp"
        for d in (self.bin, self.fake, self.tmp, root / "cwd"):
            d.mkdir(parents=True, exist_ok=True)
        kubectl = self.bin / "kubectl"
        kubectl.write_text(FAKE_KUBECTL.replace("__PYTHON__", os.sys.executable))
        kubectl.chmod(0o755)
        self.platform_env = root / "platform.env"
        self.sample_env = root / "sample.env"
        self.platform_env.write_text(f"AWS_REGION=us-east-2\nMODEL_BUCKET={BUCKET}\n")
        self.write_sample()

    def write_sample(self, **over: str | None) -> None:
        vals = {"MODEL_ID": MODEL, "MODEL_REVISION": REVISION, "MODEL_S3_PREFIX": PREFIX, "MODEL_S3_URI": URI, **over}
        lines = ["# GENERATED (test)"] + [f"{k}={v}" for k, v in vals.items() if v is not None]
        self.sample_env.write_text("\n".join(lines) + "\n")

    def env(self, **over: str | None) -> dict[str, str]:
        base = {k: v for k, v in os.environ.items() if k not in ("HF_TOKEN", "MODEL_ID", "MODEL_REVISION", "MODEL_S3_URI", "MODEL_S3_PREFIX", "STAGE_IMAGE")}
        base.update(
            {
                "PATH": f"{self.bin}:{os.environ['PATH']}",
                "FAKE_DIR": str(self.fake),
                "TMPDIR": str(self.tmp),
                "HOME": str(self.root),
                "PLATFORM_ENV": str(self.platform_env),
                "SAMPLE_ENV": str(self.sample_env),
                "STAGE_IMAGE": IMAGE,
                "KUBE_CONTEXT": "qa-context",
                "NO_COLOR": "1",
            }
        )
        for k, v in over.items():
            if v is None:
                base.pop(k, None)
            else:
                base[k] = v
        return base

    def reset(self) -> None:
        for f in ("calls.jsonl", "state.json", "waiting"):
            (self.fake / f).unlink(missing_ok=True)

    def calls(self) -> list[dict[str, Any]]:
        p = self.fake / "calls.jsonl"
        return [json.loads(ln) for ln in p.read_text().splitlines() if ln.strip()] if p.exists() else []

    def run(self, *args: str, timeout: float = 60, **env: str | None) -> Run:
        self.reset()
        assert SCRIPT.is_file(), "scripts/10-stage-model.sh must exist"
        proc = lib_proc.run([str(SCRIPT), *args], cwd=self.root / "cwd", env=self.env(**env), stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout, check=False)
        return Run(proc.returncode, proc.stdout, proc.stderr, self.calls(), self.tmp)

    def start(self, *args: str, **env: str | None) -> subprocess.Popen[str]:
        self.reset()
        assert SCRIPT.is_file(), "scripts/10-stage-model.sh must exist"
        # preexec_fn is safe here: the test process starts no threads (PLW1509 is about forking a threaded parent).
        return lib_proc.start([str(SCRIPT), *args], cwd=self.root / "cwd", env=self.env(**env), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True, preexec_fn=_default_sigint)

    def wait_for_waiting(self, proc: subprocess.Popen[str], seconds: float = 20) -> None:
        end = time.time() + seconds
        while time.time() < end:
            if (self.fake / "waiting").exists():
                return
            assert proc.poll() is None, "the script ended before reaching the wait for the Job"
            time.sleep(0.05)
        raise AssertionError("the script never reached the wait for the Job")

    def kill_group(self, proc: subprocess.Popen[str], sig: int = signal.SIGTERM) -> Run:
        os.killpg(proc.pid, sig)
        out, err = proc.communicate(timeout=30)
        return Run(proc.returncode, out, err, self.calls(), self.tmp)

    def state(self) -> dict[str, Any]:
        p = self.fake / "state.json"
        return json.loads(p.read_text()) if p.exists() else dict.fromkeys(("secret", "job"), False)
