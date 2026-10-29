# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""RED contract for the restore-equivalence probe (Track D WP6b, study 4.5).

Why logprobs and not text: greedy text of gpt-oss changes with the prefix-hit length even without
HiCache (SGLang #43055), so "greedy ON == greedy OFF" is not a valid test. The probe compares the
top-1 logprob of every generated token between a GPU-tier hit and a host-tier (and storage-tier)
hit of the SAME prompt, and requires an IDENTICAL ``cached_tokens`` (a truncated match makes the
comparison meaningless). Text differences alone never fail the probe.

Module ``demo_bench.application.restore_probe`` (stdlib only; HTTP through the ``HttpClient`` port):

* ``run_restore_probe(http, *, base_url, model, prompts, filler_prompts, storage_filler_prompts=None,
  max_tokens=256, top_logprobs=5, tolerance=1e-3, timeout_s=300.0) -> ProbeReport``.
  Per prompt, strictly sequential (concurrency 1), temperature 0:
  ``cold`` -> ``gpu`` (same prompt again) -> every ``filler_prompts`` item (sent once, to push the
  prompt out of GPU) -> ``host`` (same prompt) [-> every ``storage_filler_prompts`` item ->
  ``storage``, only when ``storage_filler_prompts`` is given].
* Request: ``POST {base_url}/v1/completions`` JSON with ``model``, ``prompt``, ``max_tokens``,
  ``temperature == 0``, ``logprobs == top_logprobs`` (``stream`` absent/false). ``base_url`` may
  carry a trailing slash.
* From the response: ``usage.prompt_tokens_details.cached_tokens`` (missing -> 0) and, per
  generated token, the top-1 logprob = ``max(choices[0].logprobs.top_logprobs[i].values())``.
* A prompt passes iff: (a) the ``gpu`` hit has ``cached_tokens > 0`` (otherwise no cache engaged and
  the probe proves nothing), (b) ``host`` (and ``storage``) ``cached_tokens`` equal the ``gpu`` one,
  (c) same number of generated tokens, (d) every ``|top1_host - top1_gpu|`` (and storage) is
  ``<= tolerance``. Failures list reasons that contain the words ``cached_tokens`` (b) or
  ``logprob`` (c, d) and the stage name.
* ``ProbeReport``: ``total``, ``passed``, ``ok`` (``passed == total``), ``results`` (one per prompt:
  ``index``, ``ok``, ``reasons``, ``cached_tokens`` {stage -> int}, ``max_logprob_diff``
  {stage -> float} for host/storage), ``to_dict()`` JSON-able with ``total``, ``passed``, ``ok``,
  ``tolerance``, ``prompts``.
* ``ValueError``: empty ``prompts`` or ``filler_prompts``, empty ``storage_filler_prompts`` list,
  ``tolerance < 0``. ``ProbeError(RuntimeError)``: a non-200 answer or a body without
  ``choices[0].logprobs`` (names the status/body); a transport failure
  (``HttpTransportError``) propagates.
* CLI ``demo-bench restore-probe --base-url URL --model M --prompts PATH --filler PATH
  [--storage-filler PATH] [--max-tokens 256] [--top-logprobs 5] [--tolerance 1e-3] [--out PATH]``;
  each input file is a JSON list of strings. Prints ``to_dict()`` JSON (also to ``--out``); exit 0
  when ``ok``, 1 when any prompt fails or the endpoint errors / is unreachable, 2 for invalid
  input (missing file, not a list of strings, empty list).

The tests run the REAL ``UrllibHttpClient`` against a local fake OpenAI-compatible server that
simulates a 3-tier LRU (GPU, host, storage) and records which tier served each request.
"""

from __future__ import annotations

import hashlib
import json
import threading
from collections import OrderedDict
from collections.abc import Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

N_PROMPTS = 16


def _tokens(prompt: str) -> int:
    return len(prompt.split())


@dataclass
class FakeEngine:
    """3-tier LRU. A prompt that was served moves to the GPU tier; overflow cascades down."""

    gpu_cap: int = 2
    host_cap: int = 3
    cache_enabled: bool = True
    host_drift: dict[str, float] = field(default_factory=dict)  # prompt -> logprob offset
    host_truncate: dict[str, int] = field(default_factory=dict)  # prompt -> cached_tokens lost
    host_text_changes: bool = False
    status: int = 200
    gpu: OrderedDict[str, None] = field(default_factory=OrderedDict)
    host: OrderedDict[str, None] = field(default_factory=OrderedDict)
    storage: set[str] = field(default_factory=set)
    log: list[tuple[str, str]] = field(default_factory=list)  # (prompt, tier)
    bodies: list[dict[str, Any]] = field(default_factory=list)

    def serve(self, prompt: str) -> tuple[str, int, float]:
        """Return (tier, cached_tokens, logprob_offset) and update the tiers."""
        tier = "cold"
        offset = 0.0
        cached = 0
        if self.cache_enabled:
            if prompt in self.gpu:
                tier, cached = "gpu", _tokens(prompt)
            elif prompt in self.host:
                tier = "host"
                cached = _tokens(prompt) - self.host_truncate.get(prompt, 0)
                offset = self.host_drift.get(prompt, 0.0)
            elif prompt in self.storage:
                tier = "storage"
                cached = _tokens(prompt) - self.host_truncate.get(prompt, 0)
                offset = self.host_drift.get(prompt, 0.0)
        self.log.append((prompt, tier))
        if self.cache_enabled:
            self.gpu.pop(prompt, None)
            self.host.pop(prompt, None)
            self.storage.discard(prompt)
            self.gpu[prompt] = None
            while len(self.gpu) > self.gpu_cap:
                old, _ = self.gpu.popitem(last=False)
                self.host[old] = None
            while len(self.host) > self.host_cap:
                old, _ = self.host.popitem(last=False)
                self.storage.add(old)
        return tier, cached, offset


def _handler(engine: FakeEngine) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args: Any) -> None:  # silence
            pass

        def do_POST(self) -> None:
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            engine.bodies.append({"path": self.path, **body})
            if engine.status != 200:
                self.send_response(engine.status)
                self.end_headers()
                self.wfile.write(b'{"error": "boom"}')
                return
            prompt = body["prompt"]
            tier, cached, offset = engine.serve(prompt)
            n = min(int(body["max_tokens"]), 8)
            base = int(hashlib.sha256(prompt.encode()).hexdigest()[:6], 16) % 100
            top = []
            for i in range(n):
                lp = -0.05 * (i + 1) - base * 1e-3 + offset
                top.append({f"t{i}": lp, f"alt{i}": lp - 1.5})
            text = "answer " * n
            if engine.host_text_changes and tier in {"host", "storage"}:
                text = "different " * n
            payload = {
                "choices": [{"text": text, "finish_reason": "length",
                             "logprobs": {"tokens": [f"t{i}" for i in range(n)],
                                          "token_logprobs": [next(iter(t.values())) for t in top],
                                          "top_logprobs": top}}],
                "usage": {"prompt_tokens": _tokens(prompt), "completion_tokens": n,
                          "prompt_tokens_details": {"cached_tokens": cached}},
            }
            data = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    return Handler


@pytest.fixture
def serve() -> Iterator[Any]:
    servers: list[ThreadingHTTPServer] = []

    def start(engine: FakeEngine) -> str:
        srv = ThreadingHTTPServer(("127.0.0.1", 0), _handler(engine))
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        servers.append(srv)
        return f"http://127.0.0.1:{srv.server_address[1]}"

    yield start
    for s in servers:
        s.shutdown()
        s.server_close()


def _prompts() -> list[str]:
    return [" ".join(f"p{i}w{j}" for j in range(40 + i)) for i in range(N_PROMPTS)]


def _fillers(tag: str, n: int) -> list[str]:
    return [" ".join(f"{tag}{i}w{j}" for j in range(30)) for i in range(n)]


def _run(url: str, **kw: Any) -> Any:
    from demo_bench.application.restore_probe import run_restore_probe
    from demo_bench.infrastructure.http import UrllibHttpClient

    args: dict[str, Any] = {"base_url": url, "model": "openai/gpt-oss-20b",
                            "prompts": _prompts(), "filler_prompts": _fillers("f", 3)}
    return run_restore_probe(UrllibHttpClient(), **(args | kw))


# ------------------------------------------------------------------ happy path
def test_16_of_16_pass_and_tiers_are_really_exercised(serve: Any) -> None:
    eng = FakeEngine()
    rep = _run(serve(eng))
    assert (rep.total, rep.passed, rep.ok) == (N_PROMPTS, N_PROMPTS, True)
    for p in _prompts():
        assert [t for q, t in eng.log if q == p] == ["cold", "gpu", "host"]
    res = rep.results[3]
    assert res.index == 3 and res.ok and res.reasons == []
    assert res.cached_tokens["gpu"] == res.cached_tokens["host"] == _tokens(_prompts()[3])
    assert res.cached_tokens["cold"] == 0
    assert res.max_logprob_diff["host"] == pytest.approx(0.0, abs=1e-12)


def test_request_shape_sequence_and_trailing_slash(serve: Any) -> None:
    eng = FakeEngine()
    rep = _run(serve(eng) + "/", prompts=_prompts()[:2], max_tokens=256, top_logprobs=5)
    assert rep.ok
    assert all(b["path"] == "/v1/completions" for b in eng.bodies)
    assert all(b["model"] == "openai/gpt-oss-20b" and b["max_tokens"] == 256 for b in eng.bodies)
    assert all(b["temperature"] == 0 and b["logprobs"] == 5 for b in eng.bodies)
    assert not any(b.get("stream") for b in eng.bodies)
    p0 = _prompts()[0]
    order = [b["prompt"] for b in eng.bodies][:2 + 3 + 1]
    assert order == [p0, p0, *_fillers("f", 3), p0]  # cold, gpu, fillers, host (sequential)


def test_storage_stage_runs_only_with_storage_fillers(serve: Any) -> None:
    eng = FakeEngine()
    rep = _run(serve(eng), prompts=_prompts()[:4], storage_filler_prompts=_fillers("s", 8))
    assert rep.ok
    for p in _prompts()[:4]:
        assert [t for q, t in eng.log if q == p] == ["cold", "gpu", "host", "storage"]
    assert rep.results[0].cached_tokens["storage"] == rep.results[0].cached_tokens["gpu"]
    assert "storage" in rep.results[0].max_logprob_diff

    eng2 = FakeEngine()
    rep2 = _run(serve(eng2), prompts=_prompts()[:2])
    assert "storage" not in rep2.results[0].cached_tokens


def test_text_difference_alone_does_not_fail(serve: Any) -> None:
    eng = FakeEngine(host_text_changes=True)
    assert _run(serve(eng)).ok  # gpt-oss greedy text is not a valid oracle (#43055)


# ------------------------------------------------------------------ the tolerance
def test_drift_below_and_above_tolerance(serve: Any) -> None:
    p = _prompts()
    ok = _run(serve(FakeEngine(host_drift={p[5]: 9e-4})))
    assert ok.ok
    bad = _run(serve(FakeEngine(host_drift={p[5]: 1.1e-3})))
    assert (bad.total, bad.passed, bad.ok) == (16, 15, False)
    assert [r.index for r in bad.results if not r.ok] == [5]
    reasons = " ".join(bad.results[5].reasons)
    assert "logprob" in reasons and "host" in reasons
    assert bad.results[5].max_logprob_diff["host"] == pytest.approx(1.1e-3, rel=1e-3)


def test_custom_tolerance(serve: Any) -> None:
    p = _prompts()
    eng = FakeEngine(host_drift={p[0]: 5e-3})
    assert _run(serve(eng), tolerance=1e-2).ok
    assert not _run(serve(FakeEngine(host_drift={p[0]: 5e-3})), tolerance=1e-3).ok


def test_storage_drift_is_caught(serve: Any) -> None:
    p = _prompts()
    eng = FakeEngine()
    # only the storage tier misbehaves: use a drift applied to storage-served requests too,
    # so the host stage of that prompt is also off. Assert the prompt fails and names a stage.
    eng.host_drift[p[1]] = 2e-3
    rep = _run(serve(eng), prompts=p[:3], storage_filler_prompts=_fillers("s", 8))
    assert [r.ok for r in rep.results] == [True, False, True]
    assert any(stage in " ".join(rep.results[1].reasons) for stage in ("host", "storage"))


# ------------------------------------------------------------------ cached_tokens identity
def test_truncated_cached_tokens_fails_even_with_identical_logprobs(serve: Any) -> None:
    p = _prompts()
    rep = _run(serve(FakeEngine(host_truncate={p[2]: 5})))
    assert [r.index for r in rep.results if not r.ok] == [2]
    assert "cached_tokens" in " ".join(rep.results[2].reasons)
    assert rep.results[2].cached_tokens["host"] == rep.results[2].cached_tokens["gpu"] - 5


def test_no_cache_at_all_does_not_pass_vacuously(serve: Any) -> None:
    rep = _run(serve(FakeEngine(cache_enabled=False)))
    assert rep.ok is False and rep.passed == 0
    assert "cached_tokens" in " ".join(rep.results[0].reasons)


# ------------------------------------------------------------------ errors and validation
def test_http_error_raises_probe_error(serve: Any) -> None:
    from demo_bench.application.restore_probe import ProbeError

    with pytest.raises(ProbeError, match="500"):
        _run(serve(FakeEngine(status=500)))


def test_unreachable_endpoint_raises_transport_error() -> None:
    from demo_bench.domain.ports import HttpTransportError

    with pytest.raises(HttpTransportError):
        _run("http://127.0.0.1:9")


@pytest.mark.parametrize(
    "kw",
    [{"prompts": []}, {"filler_prompts": []}, {"storage_filler_prompts": []}, {"tolerance": -1.0}],
)
def test_invalid_arguments(serve: Any, kw: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        _run(serve(FakeEngine()), **kw)


def test_to_dict_is_json_serialisable(serve: Any) -> None:
    d = _run(serve(FakeEngine()), prompts=_prompts()[:2]).to_dict()
    assert json.loads(json.dumps(d)) == d
    assert d["total"] == 2 and d["passed"] == 2 and d["ok"] is True and d["tolerance"] == 1e-3
    assert len(d["prompts"]) == 2


# ------------------------------------------------------------------ CLI
def _cli(argv: list[str]) -> int:
    from demo_bench.cli import main

    try:
        return int(main(argv))
    except SystemExit as exc:
        return int(exc.code or 0)


def _inputs(tmp_path: Path, n: int = 4) -> list[str]:
    (tmp_path / "prompts.json").write_text(json.dumps(_prompts()[:n]))
    (tmp_path / "filler.json").write_text(json.dumps(_fillers("f", 3)))
    return ["--model", "m", "--prompts", str(tmp_path / "prompts.json"),
            "--filler", str(tmp_path / "filler.json")]


def test_cli_pass_exits_0_and_writes_evidence(
    serve: Any, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    url = serve(FakeEngine())
    out = tmp_path / "ev" / "probe.json"
    assert _cli(["restore-probe", "--base-url", url, *_inputs(tmp_path), "--out", str(out)]) == 0
    rec = json.loads(capsys.readouterr().out)
    assert rec["ok"] is True and rec["total"] == 4 and rec["passed"] == 4
    assert json.loads(out.read_text()) == rec


def test_cli_failure_exits_1(serve: Any, tmp_path: Path,
                             capsys: pytest.CaptureFixture[str]) -> None:
    url = serve(FakeEngine(host_drift={_prompts()[1]: 1e-2}))
    assert _cli(["restore-probe", "--base-url", url, *_inputs(tmp_path)]) == 1
    rec = json.loads(capsys.readouterr().out)
    assert rec["ok"] is False and rec["passed"] == 3


def test_cli_endpoint_error_and_unreachable_exit_1(serve: Any, tmp_path: Path) -> None:
    assert _cli(["restore-probe", "--base-url", serve(FakeEngine(status=503)),
                 *_inputs(tmp_path)]) == 1
    assert _cli(["restore-probe", "--base-url", "http://127.0.0.1:9", *_inputs(tmp_path)]) == 1


def test_cli_storage_filler_option(serve: Any, tmp_path: Path,
                                   capsys: pytest.CaptureFixture[str]) -> None:
    (tmp_path / "sf.json").write_text(json.dumps(_fillers("s", 8)))
    argv = ["restore-probe", "--base-url", serve(FakeEngine()), *_inputs(tmp_path),
            "--storage-filler", str(tmp_path / "sf.json")]
    assert _cli(argv) == 0
    rec = json.loads(capsys.readouterr().out)
    assert "storage" in rec["prompts"][0]["cached_tokens"]


@pytest.mark.parametrize("content", ["{not json", '{"a": 1}', "[]", "[1, 2]"])
def test_cli_bad_prompt_file_exits_2(serve: Any, tmp_path: Path, content: str) -> None:
    argv = ["restore-probe", "--base-url", serve(FakeEngine()), *_inputs(tmp_path)]
    assert _cli(argv) == 0  # control: the valid inputs work
    (tmp_path / "prompts.json").write_text(content)
    assert _cli(argv) == 2


def test_cli_missing_file_exits_2(serve: Any, tmp_path: Path) -> None:
    good = ["restore-probe", "--base-url", serve(FakeEngine()), *_inputs(tmp_path)]
    assert _cli(good) == 0  # control
    argv = ["restore-probe", "--base-url", "http://127.0.0.1:9", "--model", "m", "--prompts",
            str(tmp_path / "nope.json"), "--filler", str(tmp_path / "nope2.json")]
    assert _cli(argv) == 2
