# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""In-memory fake of the cluster, S3 and aiperf for the matrix driver's failure-path tests and offline dry-run.

Test-only (never in the driver ConfigMap). It answers the exact kubectl argv shapes the driver issues, keeps a radix
"cache" per engine (so a canary repeated before the reset hits and one after a working flush misses), and records
every mutating action in ``events`` so tests can assert ordering (deploy, teardown, cleanup, S3 puts).
Injections: ``flush_broken`` (namespaces whose flush_cache never clears), ``broken_deploys`` (the next N fresh
deployments of a namespace have a flush_cache that leaves the cache in place; a re-deploy after that is healthy),
``deny`` (RBAC answers "no"), ``foreign_dgds`` / ``foreign_gpu_pods`` (exclusivity), ``dup_gpu`` (two engines on one
GPU), ``aiperf_rc`` / ``aiperf_duration_s`` / ``aiperf_sigterm`` (aiperf behaviour).
"""

from __future__ import annotations

import atexit
import contextlib
import hashlib
import itertools
import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any, ClassVar
from unittest import mock

import matrix_driver as md
import render_driver as rd

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
REGION = "test-region-1"  # synthetic: the repo keeps real region/account/bucket values only in platform.env
COMPOSE_FFMPEG_IMAGE = "mirror.test/bench:ffmpeg-test@sha256:" + "7" * 64


def _materialize_dgds() -> Path:
    """The driver's DGD_DIR: the repo's committed Track Q renders with the workers-sha256 annotation the render step adds."""
    dest = Path(tempfile.mkdtemp(prefix="q38-dgds-"))
    atexit.register(shutil.rmtree, dest, ignore_errors=True)
    for name, text in rd.annotated_dgds(REPO / "manifests" / "track-q-qwen38" / "rendered").items():
        (dest / name).write_text(text)
    return dest


DGD_DIR = _materialize_dgds()
NODE = "node-1"


def done(args: list[str], out: str = "", rc: int = 0, err: str = "") -> subprocess.CompletedProcess[str]:
    """A finished kubectl call."""
    return subprocess.CompletedProcess(["kubectl", *args], rc, out, err)


class FakeCluster:
    """The 4 scenario namespaces, one CB node with 8 GPUs, the bench Job and the watchdog CronJob."""

    def __init__(self, hard_stop: str, job_start: str | None = None) -> None:
        self.hard_stop = hard_stop
        self.dgds: dict[str, dict[str, Any]] = {}  # ns -> {"cell", "sha", "mode", "pods": [...]}
        self.engines: dict[str, dict[str, Any]] = {}  # pod name -> engine state
        self.frontend_counters: dict[str, dict[str, float]] = {}
        self.rr: dict[str, int] = {}
        self.lock = threading.Lock()
        self.events: list[tuple[str, ...]] = []
        self.cleanup_pods: dict[str, dict[str, Any]] = {}
        self.flush_broken: set[str] = set()  # every deployment of these namespaces
        # v3: a prompt hits from its hit_on_send-th send to the same engine (Qwen3.8 hybrid engine, measured: 3);
        # dgd_finalizer_stuck: namespaces whose DGD delete is accepted but the object stays (pods do go).
        self.hit_on_send = 3
        self.dgd_finalizer_stuck: set[str] = set()
        self.deletes: list[list[str]] = []  # every kubectl delete dgd argv
        self.broken_deploys: dict[str, int] = {}  # ns -> number of next fresh deployments with a broken flush
        self.deny: set[tuple[str, str, str]] = set()  # (verb, resource, namespace or "*")
        self.foreign_dgds: list[dict[str, Any]] = []
        self.foreign_gpu_pods = 0
        self.dup_gpu = False
        self.cronjob_present = True
        # v4 (takes): Jobs created by the driver in bench through `kubectl create -f - ` (JSON on stdin, kind Job).
        # The capture/compose pods are simulated: a Job whose outcome is "complete" writes what the real pod uploads
        # (capture: s3://<bucket>/<S3_PREFIX>/<TAKE_ID>/<TAKE_ID>.webm + frames tar; compose: every URI of COMPOSE_SPEC
        # ["outputs"]) into ``s3`` (set by the test); "failed" leaves the Job Failed; "create-fails" makes the create
        # itself exit 1 (RBAC/API error). Outcomes are matched on the Job name prefix: capture_outcome for names that
        # start with demo-capture-, compose_outcome for names that contain "compose".
        self.s3: Any = None
        self.jobs: dict[str, dict[str, Any]] = {}
        self.capture_outcome = "complete"
        self.capture_outcome_seq: list[str] = []  # consumed one per capture Job created (then capture_outcome)
        self.job_outcome: dict[str, str] = {}
        self.cron_env: list[dict[str, str]] | None = None  # watchdog CronJob env override (default: HARD_STOP only)
        self.compose_outcome = "complete"
        self.skip_compose_outputs = False  # a compose Job that completes without uploading its outputs
        self.uid = itertools.count(1)
        self.ip = itertools.count(10)
        start = job_start or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        self.job: dict[str, Any] = {"metadata": {"name": "q38-matrix-test"}, "status": {"startTime": start},
                    "spec": {"activeDeadlineSeconds": md.iso_epoch(hard_stop) - md.iso_epoch(start)}}

    # ------------------------------------------------------------ helpers
    def _pod(self, ns: str, role: str, idx: int) -> dict[str, Any]:
        n = next(self.uid)
        name = f"q38-{role}-{n:04d}"
        ip = f"10.0.0.{next(self.ip)}"
        gpu = 0 if role == "frontend" else 1
        pod = {"name": name, "uid": f"uid-{n}", "role": role, "ip": ip, "ns": ns, "gpu": gpu, "idx": idx,
               "mode": None}
        if role != "frontend":
            self.engines[name] = {"cache": {}, "worker_id": 10**14 + n, "ns": ns, "role": role,
                                  "c": {"prompt": 0.0, "cached": 0.0, "requests": 0.0}}
        return pod

    def _pods_json(self, pods: list[dict[str, Any]]) -> dict[str, Any]:
        items = []
        for p in pods:
            env = [{"name": "DYN_ROUTER_MODE", "value": p["mode"]}] if p["role"] == "frontend" else []
            req = {"nvidia.com/gpu": str(p["gpu"])} if p["gpu"] else {}
            items.append({
                "metadata": {"name": p["name"], "uid": p["uid"], "namespace": p["ns"],
                             "labels": {md.LABEL_DGD: md.DGD_NAME, md.LABEL_TYPE: p["role"]}},
                "spec": {"containers": [{"name": "main", "env": env, "resources": {"requests": req}}]},
                "status": {"phase": "Running", "podIP": p["ip"], "conditions": [{"type": "Ready", "status": "True"}],
                           "containerStatuses": [{"restartCount": 0, "imageID": "img@sha256:x"}]}})
        return {"items": items}

    def all_pods(self) -> list[dict[str, Any]]:
        """Every DGD pod."""
        return [p for d in self.dgds.values() for p in d["pods"]]

    def ns_of_url(self, url: str) -> str:
        """Namespace of a Frontend service URL."""
        m = re.search(r"q38-frontend\.([a-z-]+)\.svc", url)
        assert m, url
        return m.group(1)

    def pod_by_ip(self, ip: str) -> dict[str, Any]:
        """The DGD pod with this IP."""
        return next(p for p in self.all_pods() if p["ip"] == ip)

    # ------------------------------------------------------------ kubectl
    def kubectl(self, *args: str, timeout: float = 120, check: bool = True,
                stdin: str | None = None) -> subprocess.CompletedProcess[str]:
        """Answer one kubectl invocation of the driver."""
        a = list(args)
        r = self._kubectl(a, stdin)
        if check and r.returncode != 0:
            raise md.StepError(f"kubectl {' '.join(a[:6])} exited {r.returncode}: {r.stderr}")
        return r

    def _ns(self, a: list[str]) -> str:
        return a[a.index("-n") + 1] if "-n" in a else ""

    def _kubectl(self, a: list[str], stdin: str | None) -> subprocess.CompletedProcess[str]:
        verb = a[0]
        ns = self._ns(a)
        if verb == "auth":
            res = a[3] if len(a) > 3 else ""
            sub = a[a.index("--subresource") + 1] if "--subresource" in a else ""
            key = (a[2], res + (f"/{sub}" if sub else ""), ns or "*")
            return done(a, "no\n", 1) if key in self.deny else done(a, "yes\n")
        if a[:2] == ["get", "job"]:
            named = next((x for x in a[2:] if x in self.jobs), None)
            if named is not None:
                return done(a, json.dumps(self._job_view(named)))
            if any(x.startswith(("demo.kubecon/take-id", "q38-matrix/compose", "q38-matrix/run-id")) for x in a[2:]):
                return done(a, json.dumps({"items": [self._job_view(n) for n in self.jobs]}))
            return done(a, json.dumps(self.job))
        if a[0] == "wait":
            target = next((x.split("/", 1)[1] for x in a if x.startswith("job/")), None)
            if target is None or target not in self.jobs:
                return done(a, "", 1, "timed out waiting for the condition")
            ok = self._job_outcome(target) == "complete"
            return done(a, f"job.batch/{target} condition met\n" if ok else "", 0 if ok else 1,
                        "" if ok else "timed out waiting for the condition")
        if a[:2] == ["get", "cronjob"]:
            if not self.cronjob_present:
                return done(a, "", 1, "NotFound")
            cj = {"spec": {"suspend": False, "jobTemplate": {"spec": {"template": {"spec": {"containers": [
                {"name": "watchdog", "env": self.cron_env or [{"name": "HARD_STOP", "value": self.hard_stop}]}]}}}}}}
            return done(a, json.dumps(cj))
        if a[:2] == ["get", "dgd"] and "-A" in a:
            items = [{"metadata": {"name": md.DGD_NAME, "namespace": n,
                                   "labels": {"app.kubernetes.io/part-of": md.PART_OF}}} for n in self.dgds]
            return done(a, json.dumps({"items": items + self.foreign_dgds}))
        if a[:2] == ["get", "dgd"]:
            d = self.dgds.get(ns)
            if d is None:
                if "--ignore-not-found" in a:
                    return done(a, "")
                return done(a, "", 1, "NotFound")
            if "-o" in a and a[a.index("-o") + 1] == "name":
                return done(a, f"dynamographdeployment.nvidia.com/{md.DGD_NAME}\n")
            return done(a, json.dumps({"metadata": {"name": md.DGD_NAME, "labels": {"app.kubernetes.io/part-of": md.PART_OF},
                                                    "annotations": {"q38-matrix/workers-sha256": d["sha"]}}}))
        if a[:2] == ["delete", "dgd"]:
            return self._delete_dgd(a, ns)
        if a[:2] == ["get", "pods"] and "-A" in a:
            pods = self.all_pods() + [{"name": f"foreign-{i}", "uid": f"f{i}", "role": "x", "ip": "1.1.1.1", "ns": "other",
                                       "gpu": 1, "idx": 0, "mode": None} for i in range(self.foreign_gpu_pods)]
            return done(a, json.dumps(self._pods_json(pods)))
        if a[:2] == ["get", "pods"]:
            return done(a, json.dumps(self._pods_json(self.dgds.get(ns, {}).get("pods", []))))
        if a[:2] == ["get", "node"]:
            return done(a, json.dumps({"status": {"allocatable": {"nvidia.com/gpu": "8"}}}))
        if a[0] == "apply":
            return self._apply(Path(a[a.index("-f") + 1]))
        if a[:2] == ["delete", "pod"]:
            name = a[4]
            if name in self.cleanup_pods:
                self.cleanup_pods.pop(name)
                self.events.append(("delete-cleanup-pod", ns, name))
                return done(a)
            d = self.dgds[ns]
            old = next(p for p in d["pods"] if p["name"] == name)
            new = self._pod(ns, "frontend", 0)
            new["mode"] = old["mode"]
            d["pods"] = [new if p is old else p for p in d["pods"]]
            self.events.append(("recreate-frontend", ns, old["uid"], new["uid"]))
            return done(a)
        if a[:2] == ["create", "-f"] and json.loads(stdin or "{}").get("kind") == "Job":
            return self._create_job(a, json.loads(stdin or "{}"))
        if a[:2] == ["create", "-f"]:
            pod = json.loads(stdin or "{}")
            self.cleanup_pods[pod["metadata"]["name"]] = pod
            self.events.append(("create-cleanup-pod", pod["metadata"]["namespace"], pod["metadata"]["name"]))
            return done(a)
        if a[:2] == ["get", "pod"]:
            return done(a, json.dumps({"status": {"phase": "Succeeded"}}))
        if a[0] == "exec":
            return done(a, self._exec(a[3], a[-1]))
        if a[0] == "logs":
            return done(a, self._logs(a[3]))
        if a[:2] == ["get", "events"]:
            return done(a, "")
        raise AssertionError(f"unexpected kubectl call: {a}")

    def _job_outcome(self, name: str) -> str:
        if name in self.job_outcome:
            return self.job_outcome[name]
        return self.compose_outcome if "compose" in name else self.capture_outcome

    def _job_view(self, name: str) -> dict[str, Any]:
        job = json.loads(json.dumps(self.jobs[name]))
        outcome = self._job_outcome(name)
        job["status"] = ({"succeeded": 1, "conditions": [{"type": "Complete", "status": "True"}]} if outcome == "complete"
                         else {"failed": 1, "conditions": [{"type": "Failed", "status": "True"}]})
        return job

    def _create_job(self, a: list[str], job: dict[str, Any]) -> subprocess.CompletedProcess[str]:
        name = job["metadata"]["name"]
        if "compose" not in name and self.capture_outcome_seq:
            self.job_outcome[name] = self.capture_outcome_seq.pop(0)
        if self._job_outcome(name) == "create-fails":
            return done(a, "", 1, f"Error from server (Forbidden): jobs.batch \"{name}\" is forbidden")
        if name in self.jobs:
            return done(a, "", 1, f"Error from server (AlreadyExists): jobs.batch \"{name}\" already exists")
        self.jobs[name] = job
        self.events.append(("create-job", name))
        if self._job_outcome(name) == "complete" and self.s3 is not None:
            ctr = job["spec"]["template"]["spec"]["containers"][0]
            env = {e["name"]: e.get("value") for e in ctr.get("env", [])}
            uris: list[str] = []
            if "TAKE_ID" in env:
                base = f"{env['S3_PREFIX']}/{env['TAKE_ID']}/{env['TAKE_ID']}"
                uris = [f"s3://{self.s3.bucket}/{base}.webm", f"s3://{self.s3.bucket}/{base}-frames.tar"]
            elif "COMPOSE_SPEC" in env and not self.skip_compose_outputs:
                uris = list(json.loads(env["COMPOSE_SPEC"])["outputs"].values())
            for uri in uris:
                self.s3.objects[uri.removeprefix(f"s3://{self.s3.bucket}/")] = b"fake-media"
        return done(a, f"job.batch/{name} created\n")

    def _delete_dgd(self, a: list[str], ns: str) -> subprocess.CompletedProcess[str]:
        """kubectl delete dgd semantics: a name together with -l is refused (rc 1, nothing deleted); -l and
        --field-selector metadata.name= must match our q38 object; a stuck finalizer keeps the DGD (pods go)."""
        self.deletes.append(a)
        names, i = [], 2
        while i < len(a):
            if a[i] in ("-n", "-l", "--field-selector", "-o"):
                i += 2
                continue
            if not a[i].startswith("-"):
                names.append(a[i])
            i += 1
        if names and "-l" in a:
            return done(a, "", 1, "error: name cannot be provided when a selector is specified")
        sel = a[a.index("-l") + 1] if "-l" in a else None
        fsel = a[a.index("--field-selector") + 1] if "--field-selector" in a else None
        d = self.dgds.get(ns)
        match = (d is not None and (not names or names == [md.DGD_NAME])
                 and (sel is None or sel == f"app.kubernetes.io/part-of={md.PART_OF}")
                 and (fsel is None or fsel == f"metadata.name={md.DGD_NAME}"))
        if not match:
            return done(a, "") if "--ignore-not-found" in a else done(a, "", 1, "NotFound")
        assert d is not None
        for p in d["pods"]:
            self.engines.pop(p["name"], None)
        if ns in self.dgd_finalizer_stuck:
            d["pods"] = []
        else:
            self.dgds.pop(ns)
        self.events.append(("delete-dgd", ns))
        return done(a, f'dynamographdeployment.nvidia.com "{md.DGD_NAME}" deleted\n')

    def _apply(self, path: Path) -> subprocess.CompletedProcess[str]:
        cell = path.stem
        topology, mode = md.CELLS[cell]
        ns = md.TOPOLOGIES[topology]["namespace"]
        sha = re.search(r'q38-matrix/workers-sha256: "?([0-9a-f]{64})"?', path.read_text())
        assert sha
        d = self.dgds.get(ns)
        if d and d["sha"] == sha.group(1):
            if d["mode"] != mode:
                fe_old = next(p for p in d["pods"] if p["role"] == "frontend")
                fe = self._pod(ns, "frontend", 0)
                fe["mode"] = mode
                d["pods"] = [fe if p is fe_old else p for p in d["pods"]]
                d["mode"] = mode
                self.events.append(("toggle", ns, mode))
            return done(["apply"])
        roles = [("prefill", 4), ("decode", 4)] if md.TOPOLOGIES[topology]["pd"] else [("worker", 8)]
        pods = [self._pod(ns, "frontend", 0)]
        pods[0]["mode"] = mode
        for role, n in roles:
            pods += [self._pod(ns, role, i) for i in range(n)]
        broken = self.broken_deploys.get(ns, 0) > 0
        if broken:
            self.broken_deploys[ns] -= 1
        self.dgds[ns] = {"cell": cell, "sha": sha.group(1), "mode": mode, "pods": pods,
                         "flush_broken": broken or ns in self.flush_broken}
        self.events.append(("apply", ns, cell))
        return done(["apply"])

    def _exec(self, pod: str, script: str) -> str:
        p = next((q for q in self.all_pods() if q["name"] == pod), None)
        assert p is not None, pod
        if "find /kvcache/l3" in script:
            return "0\n0\n"
        if "/proc/net/dev" in script:
            return "  eth0: 1000 10 0 0 0 0 0 0 2000 20 0 0 0 0 0 0\n"
        if "/proc/diskstats" in script:
            return "   9 127 md127 10 0 2048 0 3 0 4096 0 0 0 0\n 259 0 nvme0n1 5 0 1024 0 1 0 8 0 0 0 0\n"
        if "/proc/meminfo" in script:
            return "MemTotal: 2097152000 kB\nMemFree: 1000000 kB\nMemAvailable: 1500000 kB\nCached: 40000 kB\n"
        if "VISIBLE_DEVICES" in script:
            engines = [q for q in self.all_pods() if q["role"] != "frontend"]
            i = engines.index(p)
            if self.dup_gpu and i == 1:
                i = 0
            pd = md.TOPOLOGIES[next(t for t, s in md.TOPOLOGIES.items() if s["namespace"] == p["ns"])]["pd"]
            if pd:
                smi = "\n".join(f"{k}, GPU-{k:04d}, 00000000:{0x50 + k:02X}:00.0" for k in range(8))
                return f"CUDA_VISIBLE_DEVICES=GPU-{i:04d}\nNVIDIA_VISIBLE_DEVICES=/var/run/nvidia\n---\n{smi}\n"
            return f"NVIDIA_VISIBLE_DEVICES=/var/run/nvidia\n---\n0, GPU-{i:04d}, 00000000:{0x50 + i:02X}:00.0\n"
        if "hw_counters" in script:
            return ""
        raise AssertionError(f"unexpected exec script: {script[:80]}")

    def _logs(self, pod: str) -> str:
        if pod in self.cleanup_pods:
            return ("files_before=12\nbytes_before=4096\nsubdirs_before=2\nfiles_after=0\n"
                    "df_bytes_total=30000000000000 df_bytes_used=100 df_bytes_avail=29999999999900 df_mount=/l3\n")
        e = self.engines.get(pod)
        if e is None:
            return ""
        lines = [f"INFO LocalKvIndexer enabled for worker {e['worker_id']} on endpoint x"]
        pd_ns = e["ns"] in ("s-dis", "s-dis-hic")
        if pd_ns:
            lines.append("NIXL INFO    _api.py:405 Backend LIBFABRIC was instantiated")
        if e["role"] in ("worker", "prefill") and e["ns"] in ("s-agg-hic", "s-dis-hic"):
            lines.append("INFO base._allocate_host_buffer: Allocating kv hierarchical KV host pool: 3213760 tokens, "
                         "105.31 GB host memory.")
        return "\n".join(lines) + "\n"

    # ------------------------------------------------------------ HTTP
    def http_req(self, method: str, url: str, body: dict[str, Any] | None = None,
                 timeout: float = 60) -> tuple[int, bytes]:
        """Frontend /v1/models and engine routes."""
        if url.endswith("/v1/models"):
            return 200, json.dumps({"data": [{"id": md.MODEL}]}).encode()
        m = re.match(r"http://([0-9.]+):9090/engine/(\w+)", url)
        assert m, url
        p = self.pod_by_ip(m.group(1))
        if m.group(2) == "flush_cache" and not self.dgds[p["ns"]]["flush_broken"]:
            self.engines[p["name"]]["cache"].clear()
        return 200, b'{"success": true}'

    def scrape(self, url: str) -> dict[str, float]:
        """Engine or Frontend /metrics as parsed series."""
        if "q38-frontend" in url:
            c = self.frontend_counters.get(self.ns_of_url(url), {"sum": 0.0, "count": 0.0})
            return {"dynamo_component_router_kv_hit_rate_sum": c["sum"], "dynamo_component_router_kv_hit_rate_count": c["count"]}
        ip = url.split("//", 1)[1].split(":", 1)[0]
        p = next((q for q in self.all_pods() if q["ip"] == ip), None)
        if p is None:
            return {}
        c = self.engines[p["name"]]["c"]
        return {'sglang:cached_tokens_total{cache_source="device"}': c["cached"], "sglang:prompt_tokens_total": c["prompt"],
                "sglang:num_requests_total": c["requests"], "sglang:num_running_reqs": 0.0, "sglang:num_queue_reqs": 0.0}

    def stream_completion(self, frontend: str, prompt: list[int], max_tokens: int, timeout: float = 900) -> dict[str, Any]:
        """Route (rr or kv-sticky), hit if the engine holds the prompt, then insert it."""
        with self.lock:
            return self._complete(self.ns_of_url(frontend), prompt, max_tokens)

    def _complete(self, ns: str, prompt: list[int], max_tokens: int) -> dict[str, Any]:
        d = self.dgds[ns]
        owners = [p for p in d["pods"] if p["role"] in ("worker", "prefill")]
        key = tuple(prompt)
        if d["mode"] == "kv":  # the router sees the stored blocks from the 1st send (overlap)
            held = [p for p in owners if key in self.engines[p["name"]]["cache"]]
            tgt = held[0] if held else owners[int(hashlib.sha256(str(key).encode()).hexdigest(), 16) % len(owners)]
        else:
            i = self.rr.get(ns, 0)
            tgt = owners[i % len(owners)]
            self.rr[ns] = i + 1
        e = self.engines[tgt["name"]]
        decodes = [p for p in d["pods"] if p["role"] == "decode"]
        dec = self.engines[decodes[int(e["c"]["requests"]) % len(decodes)]["name"]] if decodes else e
        if dec is not e:
            dec["c"]["requests"] += 1
        seen = e["cache"].get(key, 0)
        cached = len(prompt) if seen >= self.hit_on_send - 1 else 0
        e["cache"][key] = seen + 1
        e["c"]["prompt"] += len(prompt)
        e["c"]["cached"] += cached
        e["c"]["requests"] += 1
        predicted = None
        if d["mode"] == "kv":
            predicted = cached / len(prompt)
            fc = self.frontend_counters.setdefault(ns, {"sum": 0.0, "count": 0.0})
            fc["sum"] += predicted
            fc["count"] += 1
        return {"ok": True, "http_status": 200, "ttft_s": 0.01, "e2e_s": 0.02, "prompt_tokens": len(prompt),
                "completion_tokens": max_tokens, "cached_tokens": cached, "kv_hit_rate": predicted,
                "worker_id": {"prefill_worker_id": e["worker_id"], "decode_worker_id": dec["worker_id"]}}


class FakeS3(md.S3):
    """Append-only in-memory bucket (records the put order)."""

    def __init__(self, bucket: str = "test-bucket") -> None:
        super().__init__(bucket, REGION)
        self.objects: dict[str, bytes] = {}
        self.puts: list[str] = []

    def exists_key(self, key: str) -> bool:
        return key in self.objects

    def exists_prefix(self, prefix: str) -> bool:
        return any(k.startswith(prefix) for k in self.objects)

    def _key(self, uri: str) -> str:
        assert uri.startswith(f"s3://{self.bucket}/"), uri
        return uri[len(f"s3://{self.bucket}/"):]

    def get(self, uri: str, dest: Path) -> None:
        key = self._key(uri)
        if key not in self.objects:
            raise md.StepError(f"NoSuchKey {uri}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(self.objects[key])

    def get_dir(self, uri: str, dest: Path) -> None:
        raise AssertionError("not used offline")

    def put(self, src: Path, key: str) -> None:
        if key in self.objects:
            raise md.StepError(f"refusing to overwrite {self.uri(key)} (append-only)")
        self.objects[key] = src.read_bytes()
        self.puts.append(key)


class FakeAiperf:
    """Popen-like aiperf: writes a records export (+ raw records with nvext) for every trace row."""

    def __init__(self, cluster: FakeCluster, ns: str, trace: Path, art: Path, *, rc: int, duration_s: float,
                 sigterm: bool) -> None:
        self.rc, self.duration_s, self.sigterm = rc, duration_s, sigterm
        self.returncode: int | None = None
        rows = [json.loads(x) for x in trace.read_text().splitlines() if x.strip()]
        owners = [p for p in cluster.dgds[ns]["pods"] if p["role"] in ("worker", "prefill")]
        decodes = [p for p in cluster.dgds[ns]["pods"] if p["role"] == "decode"] or owners
        art.mkdir(parents=True, exist_ok=True)
        (art / "raw_records").mkdir()
        with open(art / "profile_export.jsonl", "w") as rec, open(art / "raw_records" / "raw_records_p.jsonl", "w") as raw:
            for i, r in enumerate(rows):
                meta = {"benchmark_phase": "profiling", "x_request_id": f"r{i}", "conversation_id": f"c{i}",
                        "turn_index": 0, "request_start_ns": 1_000_000 * i, "request_end_ns": 1_000_000 * i + 10**9}
                rec.write(json.dumps({"metadata": meta, "error": None, "metrics": {
                    "time_to_first_token": {"value": 100.0, "unit": "ms"},
                    "inter_token_latency": {"value": 8.0, "unit": "ms"},
                    "output_sequence_length": {"value": r["output_length"], "unit": "tokens"}}}) + "\n")
                wid = cluster.engines[owners[i % len(owners)]["name"]]["worker_id"]
                did = cluster.engines[decodes[i % len(decodes)]["name"]]["worker_id"]
                chunk = {"nvext": {"worker_id": {"prefill_worker_id": wid, "decode_worker_id": did}}}
                raw.write(json.dumps({"metadata": meta, "payload": {"messages": [{"role": "user", "content": "x"}]},
                                      "responses": [{"perf_ns": 1, "packets": [{"name": "data",
                                                                                 "value": json.dumps(chunk)}]}]}) + "\n")

    def wait(self, timeout: float | None = None) -> int:
        if self.sigterm:
            self.sigterm = False
            os.kill(os.getpid(), signal.SIGTERM)
            time.sleep(0.2)
        if self.returncode is not None:
            return self.returncode
        if timeout is not None and self.duration_s > timeout:
            raise subprocess.TimeoutExpired("aiperf", timeout)
        self.returncode = self.rc
        return self.rc

    def terminate(self) -> None:
        self.returncode = -15

    def kill(self) -> None:
        self.returncode = -9


class TDriver(md.Driver):
    """The real driver with the wheel install, input download, goodput subprocess and aiperf replaced."""

    cluster: FakeCluster
    aiperf_rc = 0
    aiperf_duration_s = 1.0
    aiperf_sigterm = False
    calibration_slo: ClassVar[dict[str, float]] = {"ttft_s": 4.2, "itl_ms": 25.0, "cold_factor": 1.4, "cold_prefill_tok_s": 30000.0}

    def install_demo_bench(self) -> dict[str, Any]:
        return {"wheel": "offline (demo_bench on PYTHONPATH)"}

    def fetch_inputs(self) -> dict[str, Any]:
        return self.check_trace()

    def goodput(self, run_dir: Path, st: md.Step) -> dict[str, Any] | None:
        return None

    def prom_metrics(self, ns: str, run_dir: Path) -> dict[str, Any] | None:
        return None

    def compute_calibration(self, st: md.Step) -> dict[str, Any]:
        return {"rule": "slo-calibration", "rule_applied": "offline fake", "slo": dict(self.calibration_slo), "in_band": True,
                "adjusted": False, "frozen_after_one_adjustment": True}

    def start_aiperf(self, cmd: list[str], log_fh: Any, cwd: Path) -> Any:
        art = Path(cmd[cmd.index("--artifact-dir") + 1])
        ns = self.cluster.ns_of_url(cmd[cmd.index("--url") + 1])
        self.cluster.events.append(("aiperf-start", ns))
        return FakeAiperf(self.cluster, ns, self.trace, art, rc=self.aiperf_rc, duration_s=self.aiperf_duration_s,
                          sigterm=self.aiperf_sigterm)


def write_trace(path: Path, rows: int = 20) -> tuple[Path, Path]:
    """A tiny trace + sidecar (synthetic, 2 rows per second)."""
    trace, side = path / "w2.jsonl", path / "w2.side.jsonl"
    path.mkdir(parents=True, exist_ok=True)
    with open(trace, "w") as t, open(side, "w") as s:
        for i in range(rows):
            t.write(json.dumps({"timestamp": 500 * i, "input_length": 1024, "output_length": 8, "hash_ids": [i]}) + "\n")
            s.write(json.dumps({"input_length": 1024, "class": "continuation"}) + "\n")
    return trace, side


@contextlib.contextmanager
def offline(plan_text: str, tmp: Path, *, hard_stop: str | None = None, bucket: str = "test-bucket",
            trace: tuple[Path, Path] | None = None, driver_cls: type[TDriver] = TDriver
            ) -> Iterator[tuple[TDriver, FakeCluster, FakeS3]]:
    """A TDriver on the fake cluster/S3 with the module adapters patched and sleeps disabled."""
    plan = md.parse_plan(plan_text)
    cluster = FakeCluster(hard_stop or plan.hard_stop)
    s3 = FakeS3(bucket)
    env = {"RESULTS_BUCKET": bucket, "CB_NODE": NODE, "WORK": str(tmp / "work"), "DGD_DIR": str(DGD_DIR),
           "DRIVER_DIR": str(HERE), "HARD_STOP": plan.hard_stop, "JOB_NAME": "q38-matrix-test",
           "L3_CLEANUP_IMAGE": "img@sha256:test", "L3_ROOT": "/mnt/.ephemeral/hicache-l3/qwen38-matrix",
           "HOME": str(tmp / "home"), "AWS_REGION": REGION, "COMPOSE_FFMPEG_IMAGE": COMPOSE_FFMPEG_IMAGE}
    with contextlib.ExitStack() as stack:
        stack.enter_context(mock.patch.dict(os.environ, env))
        stack.enter_context(mock.patch.object(md, "kubectl", cluster.kubectl))
        stack.enter_context(mock.patch.object(md, "http_req", cluster.http_req))
        stack.enter_context(mock.patch.object(md, "scrape", cluster.scrape))
        stack.enter_context(mock.patch.object(md, "stream_completion", cluster.stream_completion))
        stack.enter_context(mock.patch.object(md, "pause", lambda s: None))
        d = driver_cls(plan, None)
        d.cluster = cluster
        d.s3 = s3
        d.trace, d.sidecar = trace or write_trace(tmp / "inputs")
        yield d, cluster, s3
