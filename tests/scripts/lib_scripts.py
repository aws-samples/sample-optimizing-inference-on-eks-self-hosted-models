# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Harness for the shell-script contract tests.

Scripts are executed for real (bash) with *stub* executables placed first on PATH:
``kubectl``, ``curl``, ``aws``, ``aiperf``, ``demo-bench``, ``uv``, ``sleep`` and ``date``.
Every stub appends one JSON line (tool, argv, seq) to ``$QA_STUB_DIR/calls.jsonl`` and
answers from ``$QA_STUB_DIR/config.json``.  ``sleep`` advances a fake clock that
``date +%s`` honours, so retry/backoff loops finish instantly but remain observable.

Nothing here talks to AWS or to a cluster.
"""

from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import lib_proc

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = Path(os.environ.get("SCRIPTS_DIR", REPO_ROOT / "scripts"))

REQUIRED_SCRIPTS = (
    "11-prefetch.sh",
    "13-prepull.sh",
    "50-toggle-router.sh",
    "preflight-flags.sh",
)

STUB_SOURCE = r'''#!__PYTHON__
import fcntl, hashlib, json, os, re, subprocess, sys, time
from pathlib import Path

D = Path(os.environ["QA_STUB_DIR"])
NAME = os.path.basename(sys.argv[0])
ARGV = sys.argv[1:]
CFG = json.loads((D / "config.json").read_text())


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
    return seq


def clock_offset():
    def get(st):
        return st.get("clock", 0.0)
    return locked(D / "state.json", get)


def parse_duration(txt):
    m = re.fullmatch(r"([0-9.]+)([smhd]?)", txt)
    if not m:
        return 0.0
    return float(m.group(1)) * {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}[m.group(2)]


def http(argv):
    joined = " ".join(argv)
    rules = CFG.get("http_rules", [])
    chosen = None
    for idx, rule in enumerate(rules):
        if all(tok in joined for tok in rule.get("all", [])) and any(t in joined for t in rule.get("any_of", [""])):
            chosen = idx
            break
    status, body = 200, CFG.get("default_http_body", "{}")
    if re.search(r"/v1/models", joined):
        body = json.dumps({"data": [{"id": "example-org/stub-model"}]})
    elif re.search(r"/engine/(flush_cache|clear_hicache_storage)", joined):
        body = json.dumps({"status": "success", "message": "KV cache cleared"})
    if chosen is not None:
        seqs = rules[chosen]["sequence"]
        def bump(st):
            c = st.setdefault("rule_counts", {})
            n = c.get(str(chosen), 0)
            c[str(chosen)] = n + 1
            return n
        n = locked(D / "state.json", bump)
        resp = seqs[min(n, len(seqs) - 1)]
        status, body = resp.get("status", 200), resp.get("body", "")
    out_file, write_out, fail = None, None, False
    for i, a in enumerate(argv):
        if a in ("-o", "--output") and i + 1 < len(argv):
            out_file = argv[i + 1]
        if a in ("-w", "--write-out") and i + 1 < len(argv):
            write_out = argv[i + 1]
        if a in ("-f", "--fail", "--fail-with-body") or (a.startswith("-") and not a.startswith("--") and "f" in a[1:] and a not in ("-o", "-w")):
            fail = True
    rc = 22 if (fail and status >= 400) else 0
    if out_file and out_file != "/dev/null":
        Path(out_file).write_text(body)
    elif not out_file:
        sys.stdout.write(body)
    if write_out:
        sys.stdout.write(write_out.replace("%{http_code}", str(status)).replace("\\n", "\n"))
    return rc


def label_match(labels, selector):
    for term in [t.strip() for t in selector.split(",") if t.strip()]:
        m = re.fullmatch(r"([\w./-]+)\s+in\s+\(([^)]*)\)", term)
        if m:
            if labels.get(m.group(1)) not in [v.strip() for v in m.group(2).split(",")]:
                return False
        elif "!=" in term:
            k, v = term.split("!=", 1)
            if labels.get(k) == v:
                return False
        elif "=" in term:
            k, v = term.replace("==", "=").split("=", 1)
            if labels.get(k) != v:
                return False
        elif labels.get(term) is None:
            return False
    return True


def namespace_of(argv):
    for i, a in enumerate(argv):
        if a in ("-n", "--namespace") and i + 1 < len(argv):
            return argv[i + 1]
        if a.startswith("--namespace="):
            return a.split("=", 1)[1]
    return None


def selector_of(argv):
    for i, a in enumerate(argv):
        if a in ("-l", "--selector") and i + 1 < len(argv):
            return argv[i + 1]
        if a.startswith("--selector="):
            return a.split("=", 1)[1]
    return None


def kubectl(argv):
    joined = " ".join(argv)
    if "--" in argv and "curl" in argv[argv.index("--"):]:
        return http(argv)
    if ".done" in joined:
        if CFG.get("markers", "present") == "present":
            print("/mnt/.ephemeral/models/stub-model/.done")
            return 0
        sys.stderr.write("ls: cannot access: No such file or directory\n")
        return 1
    verbs = [a for a in argv if not a.startswith("-")]
    verb = verbs[0] if verbs else ""
    if verb == "kustomize":
        sys.stdout.write(CFG.get("kustomize_output", "apiVersion: nvidia.com/v1beta1\nkind: DynamoGraphDeployment\nmetadata:\n  name: stub-dgd\nspec:\n  backendFramework: sglang\n  components: []\n"))
        return 0
    if verb == "set" and CFG.get("set_env_output"):
        # What `kubectl set env --local -o yaml` prints when it changed something.
        sys.stdout.write(CFG["set_env_output"])
        return 0
    if verb == "apply" and CFG.get("apply_requires_objects"):
        # Real kubectl: `apply -f <empty file>` fails ("no objects passed to apply").
        files = []
        for i, a in enumerate(argv):
            if a in ("-f", "--filename") and i + 1 < len(argv):
                files.append(argv[i + 1])
            elif a.startswith("--filename="):
                files.append(a.split("=", 1)[1])
        for f in files:
            body = Path(f).read_text() if Path(f).is_file() else ""
            with open(D / "applied.jsonl", "a") as fh:
                fh.write(json.dumps({"file": f, "content": body}) + "\n")
            if not body.strip():
                sys.stderr.write("error: no objects passed to apply\n")
                return 1
        return 0
    if verb == "get" and len(verbs) > 1 and verbs[1].split("/")[0] in ("job", "jobs") and "jobs" in CFG:
        # Job state model: CFG["jobs"] = {name: <Job object>}; absent -> NotFound.
        first = verbs[1]
        name = first.split("/", 1)[1] if "/" in first else (verbs[2] if len(verbs) > 2 else "")
        job = CFG["jobs"].get(name)
        if job is None:
            if "--ignore-not-found" in argv:
                return 0
            sys.stderr.write('Error from server (NotFound): jobs.batch "%s" not found\n' % name)
            return 1
        sys.stdout.write(json.dumps(job))
        return 0
    if verb == "get":
        res = verbs[1] if len(verbs) > 1 else ""
        if res.split("/")[0] in ("pods", "pod", "po"):
            ns = namespace_of(argv)
            items = CFG.get("pods", {}).get(ns, [])
            sel = selector_of(argv)
            if sel:
                items = [p for p in items if label_match(p["metadata"].get("labels", {}), sel)]
            sys.stdout.write(json.dumps({"apiVersion": "v1", "kind": "List", "items": items}))
            return 0
        sys.stdout.write(json.dumps({"apiVersion": "v1", "kind": "List", "items": []}))
        return 0
    if verb == "logs":
        tail = 100
        for i, a in enumerate(argv):
            if a.startswith("--tail="):
                tail = int(a.split("=", 1)[1])
            elif a == "--tail" and i + 1 < len(argv):
                tail = int(argv[i + 1])
        for n in range(1, tail + 1):
            print("BOOT-LOG-LINE-%d" % n)
        return 0
    if verb == "wait":
        if "--for=delete" in argv or "delete" in [argv[i + 1] for i, a in enumerate(argv[:-1]) if a == "--for"]:
            print("condition met")
            return 0
        if CFG.get("wait_behavior", "ready") == "timeout":
            tmo = 30.0
            for i, a in enumerate(argv):
                if a.startswith("--timeout="):
                    tmo = parse_duration(a.split("=", 1)[1])
                elif a == "--timeout" and i + 1 < len(argv):
                    tmo = parse_duration(argv[i + 1])
            def adv(st):
                st["clock"] = st.get("clock", 0.0) + tmo
            locked(D / "state.json", adv)
            sys.stderr.write("error: timed out waiting for the condition\n")
            return 1
        print("condition met")
        return 0
    return CFG.get("kubectl_default_rc", 0)


def demo_bench(argv):
    """Fake `demo-bench`: only `trace schedule` is modelled (deterministic timestamp per row)."""
    if argv[:2] != ["trace", "schedule"]:
        return 0
    opts, i = {}, 2
    while i < len(argv):
        a = argv[i]
        if a.startswith("--"):
            k, eq, v = a[2:].partition("=")
            if not eq:
                v = argv[i + 1] if i + 1 < len(argv) else None
                i += 1
            opts[k] = v
        i += 1
    for req in ("in", "out", "rate", "seed"):
        if not opts.get(req):
            sys.stderr.write("demo-bench: error: --%s is required\n" % req)
            return 2
    if CFG.get("schedule_rc", 0):
        sys.stderr.write("demo-bench: schedule generation failed\n")
        return int(CFG["schedule_rc"])
    try:
        rate = float(opts["rate"])
        seed = int(opts["seed"])
        rows = [json.loads(l) for l in Path(opts["in"]).read_text().splitlines() if l.strip()]
    except (ValueError, OSError) as exc:
        sys.stderr.write("demo-bench: error: %s\n" % exc)
        return 2
    if rate <= 0:
        sys.stderr.write("demo-bench: error: --rate must be > 0\n")
        return 2
    out = []
    for n, row in enumerate(rows):
        row["timestamp"] = int(n * 1000 / rate)
        row["arrival"] = opts.get("arrival", "default")
        row["schedule_seed"] = seed
        out.append(json.dumps(row, sort_keys=True))
    text = "\n".join(out) + "\n"
    Path(opts["out"]).write_text(text)
    with open(D / "schedule_outputs.jsonl", "a") as fh:
        fh.write(json.dumps({"out": opts["out"], "sha256": hashlib.sha256(text.encode()).hexdigest(), "opts": opts}) + "\n")
    return 0


def main():
    log()
    if NAME == "demo-bench":
        return demo_bench(ARGV)
    if NAME == "sleep":
        delta = sum(parse_duration(a) for a in ARGV)
        def adv(st):
            st["clock"] = st.get("clock", 0.0) + delta
        locked(D / "state.json", adv)
        return 0
    if NAME == "date":
        if any(a == "+%s" for a in ARGV):
            print(int(time.time() + clock_offset()))
            return 0
        return subprocess.call(["/bin/date"] + ARGV)
    if NAME == "curl":
        return http(ARGV)
    if NAME == "kubectl":
        return kubectl(ARGV)
    if NAME == "aws":
        sys.stdout.write(CFG.get("aws_output", ""))
        return 0
    return 0


sys.exit(main())
'''

STUB_NAMES = ("kubectl", "curl", "aws", "aiperf", "demo-bench", "uv", "sleep", "date")


def pod(
    name: str,
    ip: str,
    comp_type: str,
    ns: str,
    dgd: str = "stub-dgd",
    ready: bool = True,
    waiting: str | None = None,
    terminated: str | None = None,
    last_terminated: str | None = None,
    init_waiting: str | None = None,
) -> dict[str, Any]:
    """Build a canned Pod object carrying the labels the Dynamo operator sets.

    ``waiting``/``terminated``/``last_terminated`` set the main container state reason
    (e.g. CrashLoopBackOff, ImagePullBackOff, OOMKilled); ``init_waiting`` sets the
    init container's waiting reason.
    """
    state: dict[str, Any] = {"running": {"startedAt": "2026-10-19T00:00:00Z"}}
    if waiting:
        state = {"waiting": {"reason": waiting, "message": "canned"}}
    if terminated:
        state = {"terminated": {"reason": terminated, "exitCode": 137}}
    cstat: dict[str, Any] = {"name": "main", "ready": ready, "restartCount": 3 if (waiting or terminated or last_terminated) else 0, "state": state}
    if last_terminated:
        cstat["lastState"] = {"terminated": {"reason": last_terminated, "exitCode": 137}}
    status: dict[str, Any] = {
        "phase": "Running" if ready else "Pending",
        "podIP": ip,
        "conditions": [{"type": "Ready", "status": "True" if ready else "False"}],
        "containerStatuses": [cstat],
    }
    if init_waiting:
        status["initContainerStatuses"] = [
            {"name": "gate", "ready": False, "state": {"waiting": {"reason": init_waiting}}}
        ]
    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": name,
            "namespace": ns,
            "labels": {
                "nvidia.com/dynamo-component-type": comp_type,
                "nvidia.com/dynamo-graph-deployment-name": dgd,
            },
        },
        "status": status,
    }


def pods_for(ns: str, comps: list[tuple[str, str, str]]) -> list[dict[str, Any]]:
    """``comps`` = [(name, ip, component-type)]."""
    return [pod(n, ip, t, ns) for n, ip, t in comps]


@dataclass
class Result:
    rc: int
    stdout: str
    stderr: str
    calls: list[dict[str, Any]]
    elapsed: float

    # ---- call inspection
    def kubectl_calls(self) -> list[dict[str, Any]]:
        return [c for c in self.calls if c["tool"] == "kubectl"]

    def calls_matching(self, tool: str, *tokens: str) -> list[dict[str, Any]]:
        return [
            c
            for c in self.calls
            if c["tool"] == tool and all(t in " ".join(c["argv"]) for t in tokens)
        ]

    def http_requests(self) -> list[dict[str, Any]]:
        """All HTTP calls: direct ``curl`` or ``kubectl exec ... -- curl``."""
        out = []
        for c in self.calls:
            argv = c["argv"]
            if c["tool"] == "curl" or (
                c["tool"] == "kubectl" and "--" in argv and "curl" in argv[argv.index("--") :]
            ):
                out.append({"seq": c["seq"], "joined": " ".join(argv), "argv": argv})
        return out

    def engine_calls(self, route: str) -> list[dict[str, Any]]:
        return [r for r in self.http_requests() if f"/engine/{route}" in r["joined"]]

    def aiperf_calls(self) -> list[dict[str, Any]]:
        return [
            c
            for c in self.calls
            if c["tool"] == "aiperf" or (c["tool"] == "kubectl" and "aiperf" in c["argv"])
            or (c["tool"] == "kubectl" and any(a == "aiperf" or a.endswith("/aiperf") for a in c["argv"]))
        ]

    def schedule_calls(self) -> list[dict[str, Any]]:
        """Calls to `demo-bench trace schedule`."""
        return [c for c in self.calls if c["tool"] == "demo-bench" and c["argv"][:2] == ["trace", "schedule"]]

    def aiperf_text(self) -> str:
        return "\n".join(" ".join(c["argv"]) for c in self.aiperf_calls())

    def all_argv_text(self) -> str:
        return "\n".join(c["tool"] + " " + " ".join(c["argv"]) for c in self.calls)


@dataclass
class Sandbox:
    """An isolated run directory with stubs on PATH."""

    root: Path
    config: dict[str, Any] = field(default_factory=dict)
    default_env: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.stub_dir = self.root / "stubs"
        self.state_dir = self.root / "state"
        self.work_dir = self.root / "work"
        for d in (self.stub_dir, self.state_dir, self.work_dir):
            d.mkdir(parents=True, exist_ok=True)
        core = self.stub_dir / "_stub_core"
        core.write_text(STUB_SOURCE.replace("__PYTHON__", sys.executable))
        core.chmod(core.stat().st_mode | stat.S_IEXEC)
        for name in STUB_NAMES:
            link = self.stub_dir / name
            if not link.exists():
                link.symlink_to(core.name)

    def _prepare(self, env: dict[str, str] | None) -> dict[str, str]:
        (self.state_dir / "config.json").write_text(json.dumps(self.config))
        scrub = ("RATE", "SEED", "ARRIVAL", "CONCURRENCY", "SERIES_TRACE", "RESULTS_DIR", "SERIES_SPEEDUP", "SERIES_WINDOW_MS")
        full_env = {
            **{k: v for k, v in os.environ.items() if k not in scrub},
            "PATH": f"{self.stub_dir}:{os.environ['PATH']}",
            "QA_STUB_DIR": str(self.state_dir),
            "HOME": str(self.root),
            "AWS_REGION": "eu-west-3",
            "AWS_DEFAULT_REGION": "eu-west-3",
            "AWS_PROFILE": "qa-stub",
            "CLUSTER_NAME": "stale-cluster-euw3",
            "RESULTS_BUCKET": "qa-results-bucket",
            "RUN_ID": "qa-run",
            "NO_COLOR": "1",
            **self.default_env,
            **(env or {}),
        }
        return full_env

    def reset(self) -> None:
        """Clear the call log and the fake clock."""
        (self.state_dir / "schedule_outputs.jsonl").write_text("")
        (self.state_dir / "calls.jsonl").write_text("")
        (self.state_dir / "state.json").write_text("{}")
        (self.state_dir / "applied.jsonl").write_text("")

    def clock(self) -> float:
        """Seconds the fake clock advanced (sum of stub `sleep` and timed-out `kubectl wait`)."""
        return float(json.loads((self.state_dir / "state.json").read_text() or "{}").get("clock", 0.0))

    def schedule_outputs(self) -> list[dict[str, Any]]:
        """Files written by the fake `demo-bench trace schedule` (path, sha256, parsed options)."""
        path = self.state_dir / "schedule_outputs.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]

    def calls(self) -> list[dict[str, Any]]:
        calls = [
            json.loads(line)
            for line in (self.state_dir / "calls.jsonl").read_text().splitlines()
            if line.strip()
        ]
        calls.sort(key=lambda c: c["seq"])
        return calls

    def start(self, script: str, *args: str, env: dict[str, str] | None = None) -> subprocess.Popen[str]:
        """Start a script in its own session (no reset of the call log); caller manages it."""
        script_path = SCRIPTS_DIR / script
        assert script_path.is_file(), f"missing scripts/{script} (the contract script does not exist yet)"
        return lib_proc.start(
            [str(script_path), *args],
            cwd=self.work_dir,
            env=self._prepare(env),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )

    def run(
        self,
        script: str,
        *args: str,
        env: dict[str, str] | None = None,
        timeout: float = 120,
    ) -> Result:
        import time

        self.reset()
        full_env = self._prepare(env)
        script_path = SCRIPTS_DIR / script
        assert script_path.is_file(), f"missing scripts/{script} (the contract script does not exist yet)"
        start = time.time()
        proc = lib_proc.run(
            [str(script_path), *args],
            cwd=self.work_dir,
            env=full_env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        elapsed = time.time() - start
        return Result(proc.returncode, proc.stdout, proc.stderr, self.calls(), elapsed)


TRACE_ROWS = [
    {"input_length": 1024, "output_length": 16, "hash_ids": [1, 2]},
    {"input_length": 2048, "output_length": 8, "hash_ids": [3, 4, 5, 6]},
    {"input_length": 512, "output_length": 32, "hash_ids": [7]},
]


def series_env(root: Path, **overrides: str) -> dict[str, str]:
    """Environment of a fixed-schedule series run: raw (timestamp-less) trace, results dir, RATE and SEED.

    Creates ``<root>/raw-trace.jsonl`` (once) and returns env vars for ``30-run.sh``.
    Pass ``KEY=None``-style removal by omitting; use ``overrides`` to replace values.
    """
    root.mkdir(parents=True, exist_ok=True)
    trace = root / "raw-trace.jsonl"
    if not trace.exists():
        trace.write_text("".join(json.dumps(r) + "\n" for r in TRACE_ROWS))
    env = {
        "SERIES_TRACE": str(trace),
        "RESULTS_DIR": str(root / "results"),
        "RATE": "2.5",
        "SEED": "1234",
    }
    env.update(overrides)
    return env


def has_arg(argv: list[str], *names: str) -> bool:
    return any(a in names or any(a.startswith(n + "=") for n in names) for a in argv)


def arg_value(argv: list[str], name: str) -> str | None:
    for i, a in enumerate(argv):
        if a == name and i + 1 < len(argv):
            return argv[i + 1]
        if a.startswith(name + "="):
            return a.split("=", 1)[1]
    return None


NAMESPACE_RE = re.compile(r"(?:-n|--namespace)[ =]([\w-]+)")
