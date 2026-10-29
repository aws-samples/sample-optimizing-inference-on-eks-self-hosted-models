# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""RED contract for the restore-probe noise floor and the image gate (Flash tooling, item e).

Critic finding: a top-1 logprob tolerance of 1e-3 between a cached and a cold path can be
stepped on by legitimate numerical noise (different chunking/kernels), which would fail BOTH
images and wrongly push O out of the matrix. The probe therefore measures its own floor first and
pre-registers ``threshold = max(1e-3, k x floor)``.

Module ``demo_bench.application.restore_probe`` (extended):

* ``measure_noise_floor(http, *, base_url, model, prompts, flush, max_tokens=256, top_logprobs=5,
  timeout_s=300.0, base_tolerance=1e-3, k=3.0) -> NoiseFloor``. ``flush`` is a ``Callable[[],
  None]`` that empties the engine caches (GPU and host). Per prompt, strictly sequential at
  temperature 0: ``flush``, cold A, ``flush``, cold B  -> ``cold_vs_cold`` = max over positions
  of ``|top1_A - top1_B|``; then ``flush``, cold C, the same prompt again (a GPU hit) ->
  ``gpu_vs_cold`` = max over positions of ``|top1_gpu - top1_C|``. If a request that follows a
  ``flush`` reports ``cached_tokens > 0`` the flush did not work: ``ProbeError``.
* ``NoiseFloor``: ``cold_vs_cold`` and ``gpu_vs_cold`` (lists, one float per prompt), ``floor``
  (largest value of both lists), ``k``, ``base_tolerance``, ``effective_tolerance`` =
  ``max(base_tolerance, k * floor)``, ``to_dict()`` with those keys.
* ``run_restore_probe(..., flush=None, noise_floor=None)``: when ``flush`` is given it is called
  once before the first request. When ``noise_floor`` is given the tolerance used is
  ``max(tolerance, noise_floor.effective_tolerance)``, the report's ``tolerance`` is that value
  and ``ProbeReport.to_dict()["noise_floor"]`` is ``noise_floor.to_dict()`` (absent otherwise).
* ``ValueError``: empty ``prompts``, ``k <= 0``, ``base_tolerance < 0``.
* CLI ``demo-bench restore-probe ... [--flush-url URL] [--noise-k K]``: with ``--flush-url``
  (an absolute URL that is POSTed with an empty JSON body; non-200 -> exit 1) the CLI measures
  the floor on the prompts, then runs the probe with it (``--tolerance`` is the base). ``--noise-k``
  (default 3.0) without ``--flush-url`` is invalid input (exit 2).

Module ``demo_bench.application.image_gate``:

* ``decide_image(derived, control, *, accept_length_ratio, prefill_ratio, required_prompts=16)
  -> ImageDecision`` where ``derived`` / ``control`` are ``ProbeReport.to_dict()`` documents of
  the derived image and of the dev.1 control, run with the stages cold, gpu, host, storage.
  ``ValueError`` when a report has fewer than ``required_prompts`` prompts or lacks the ``host``
  or ``storage`` stage (the gate needs 16/16 in both). A report passes the restore gate iff its
  ``ok`` is true.
* Other conditions of the pre-registered rule: accept length ratio >= 0.85 and cold prefill at
  48K ratio >= 0.8 (both inclusive), measured derived / dev.1.
* ``ImageDecision``: ``state`` in ``derived_only_pass`` / ``both_pass`` / ``control_only_pass`` /
  ``both_fail``; ``image`` (``derived`` iff the derived image passes the restore gate AND the other
  conditions, else ``dev1``); ``o_valid`` (HiCache may be measured: the chosen image passed the
  restore gate); ``discriminating`` (the two images disagree, so the probe proved it can tell
  corruption from no corruption); ``reasons`` (list of str); ``to_dict()``.
* CLI ``demo-bench image-gate --derived P --control P --accept-length-ratio X --prefill-ratio X
  [--out P]``: prints ``to_dict()`` (also to ``--out``), exit 0 whatever the decision, 2 for
  invalid input.
"""

from __future__ import annotations

import json
import threading
from collections import OrderedDict
from collections.abc import Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

N = 4


def _tokens(prompt: str) -> int:
    return len(prompt.split())


@dataclass
class NoisyEngine:
    """2-tier LRU engine whose cold path has numerical jitter; ``/flush`` empties the tiers."""

    jitter: float = 0.0
    gpu_shift: float = 0.0
    host_drift: float = 0.0
    flush_works: bool = True
    flush_status: int = 200
    gpu_cap: int = 2
    host_cap: int = 3
    cold_calls: int = 0
    flushes: int = 0
    gpu: OrderedDict[str, None] = field(default_factory=OrderedDict)
    host: OrderedDict[str, None] = field(default_factory=OrderedDict)
    log: list[tuple[str, str]] = field(default_factory=list)

    def flush(self) -> None:
        self.flushes += 1
        if self.flush_works:
            self.gpu.clear()
            self.host.clear()

    def serve(self, prompt: str) -> tuple[int, float]:
        cached, offset, tier = 0, 0.0, "cold"
        if prompt in self.gpu:
            tier, cached, offset = "gpu", _tokens(prompt), self.gpu_shift
        elif prompt in self.host:
            tier, cached, offset = "host", _tokens(prompt), self.host_drift
        else:
            offset = (-self.jitter, 0.0, self.jitter)[self.cold_calls % 3]
            self.cold_calls += 1
        self.log.append((prompt, tier))
        self.gpu.pop(prompt, None)
        self.host.pop(prompt, None)
        self.gpu[prompt] = None
        while len(self.gpu) > self.gpu_cap:
            old, _ = self.gpu.popitem(last=False)
            self.host[old] = None
        while len(self.host) > self.host_cap:
            self.host.popitem(last=False)
        return cached, offset


def _handler(engine: NoisyEngine) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args: Any) -> None:
            pass

        def _send(self, status: int, payload: dict[str, Any]) -> None:
            data = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
            if self.path == "/flush":
                engine.flush()
                self._send(engine.flush_status, {"status": "ok"})
                return
            cached, offset = engine.serve(body["prompt"])
            n = 6
            top = [{f"t{i}": -0.05 * (i + 1) + offset, f"a{i}": -2.0} for i in range(n)]
            self._send(200, {
                "choices": [{"text": "x", "finish_reason": "length",
                             "logprobs": {"top_logprobs": top}}],
                "usage": {"prompt_tokens": _tokens(body["prompt"]), "completion_tokens": n,
                          "prompt_tokens_details": {"cached_tokens": cached}},
            })

    return Handler


@pytest.fixture
def serve() -> Iterator[Any]:
    servers: list[ThreadingHTTPServer] = []

    def start(engine: NoisyEngine) -> str:
        srv = ThreadingHTTPServer(("127.0.0.1", 0), _handler(engine))
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        servers.append(srv)
        return f"http://127.0.0.1:{srv.server_address[1]}"

    yield start
    for s in servers:
        s.shutdown()
        s.server_close()


def _prompts(n: int = N) -> list[str]:
    return [" ".join(f"p{i}w{j}" for j in range(40 + i)) for i in range(n)]


def _fillers() -> list[str]:
    return [" ".join(f"f{i}w{j}" for j in range(30)) for i in range(3)]


def _floor(url: str, engine: NoisyEngine, **kw: Any) -> Any:
    from demo_bench.application.restore_probe import measure_noise_floor
    from demo_bench.infrastructure.http import UrllibHttpClient

    http = UrllibHttpClient()
    args: dict[str, Any] = {
        "base_url": url, "model": "m", "prompts": _prompts(),
        "flush": lambda: http.request("POST", url + "/flush", json={}),
    }
    return measure_noise_floor(http, **(args | kw))


def _probe(url: str, **kw: Any) -> Any:
    from demo_bench.application.restore_probe import run_restore_probe
    from demo_bench.infrastructure.http import UrllibHttpClient

    args: dict[str, Any] = {"base_url": url, "model": "m", "prompts": _prompts(),
                            "filler_prompts": _fillers()}
    return run_restore_probe(UrllibHttpClient(), **(args | kw))


# ------------------------------------------------------------------ measuring the floor
def test_a_quiet_engine_has_a_zero_floor_and_the_base_tolerance(serve: Any) -> None:
    eng = NoisyEngine()
    nf = _floor(serve(eng), eng)
    assert nf.cold_vs_cold == pytest.approx([0.0] * N, abs=1e-12)
    assert nf.gpu_vs_cold == pytest.approx([0.0] * N, abs=1e-12)
    assert nf.floor == pytest.approx(0.0, abs=1e-12)
    assert nf.effective_tolerance == pytest.approx(1e-3)
    assert eng.flushes >= 3 * N  # before cold A, cold B and cold C of every prompt


def test_cold_vs_cold_jitter_sets_the_floor(serve: Any) -> None:
    eng = NoisyEngine(jitter=0.01)
    nf = _floor(serve(eng), eng)
    assert nf.cold_vs_cold == pytest.approx([0.01] * N, abs=1e-9)
    assert nf.floor == pytest.approx(0.01, abs=1e-9)
    assert nf.k == 3.0 and nf.base_tolerance == 1e-3
    assert nf.effective_tolerance == pytest.approx(0.03, abs=1e-8)


def test_gpu_hit_versus_cold_is_part_of_the_floor(serve: Any) -> None:
    eng = NoisyEngine(gpu_shift=0.02)
    nf = _floor(serve(eng), eng)
    assert nf.cold_vs_cold == pytest.approx([0.0] * N, abs=1e-12)
    assert nf.gpu_vs_cold == pytest.approx([0.02] * N, abs=1e-9)
    assert nf.floor == pytest.approx(0.02, abs=1e-9)
    assert nf.effective_tolerance == pytest.approx(0.06, abs=1e-8)


def test_k_scales_the_floor_and_the_base_is_a_lower_bound(serve: Any) -> None:
    eng = NoisyEngine(jitter=0.01)
    assert _floor(serve(eng), eng, k=10.0).effective_tolerance == pytest.approx(0.1, abs=1e-8)
    quiet = NoisyEngine(jitter=1e-5)
    nf = _floor(serve(quiet), quiet)
    assert nf.floor == pytest.approx(1e-5, abs=1e-9)
    assert nf.effective_tolerance == pytest.approx(1e-3)
    base = _floor(serve(quiet), quiet, base_tolerance=5e-3)
    assert base.effective_tolerance == pytest.approx(5e-3)


def test_a_flush_that_does_not_empty_the_cache_is_an_error(serve: Any) -> None:
    from demo_bench.application.restore_probe import ProbeError

    eng = NoisyEngine(flush_works=False)
    with pytest.raises(ProbeError, match="flush"):
        _floor(serve(eng), eng)


@pytest.mark.parametrize("kw", [{"prompts": []}, {"k": 0.0}, {"k": -1.0}, {"base_tolerance": -1.0}])
def test_invalid_floor_arguments(serve: Any, kw: dict[str, Any]) -> None:
    eng = NoisyEngine()
    with pytest.raises(ValueError):
        _floor(serve(eng), eng, **kw)


def test_to_dict_is_json_serialisable(serve: Any) -> None:
    eng = NoisyEngine(jitter=0.01)
    d = _floor(serve(eng), eng).to_dict()
    assert json.loads(json.dumps(d)) == d
    assert set(d) >= {"cold_vs_cold", "gpu_vs_cold", "floor", "k", "base_tolerance",
                      "effective_tolerance"}


# ------------------------------------------------------------------ using the floor
def test_drift_inside_k_times_the_floor_passes_with_it_and_fails_without(serve: Any) -> None:
    eng = NoisyEngine(jitter=0.01, host_drift=0.02)
    url = serve(eng)
    nf = _floor(url, eng)
    assert _probe(url).ok is False  # plain 1e-3: legitimate noise would sink the image
    rep = _probe(url, noise_floor=nf)
    assert rep.ok is True
    assert rep.tolerance == pytest.approx(0.03, abs=1e-8)
    d = rep.to_dict()
    assert d["tolerance"] == rep.tolerance and d["noise_floor"] == nf.to_dict()


def test_real_corruption_still_fails_with_the_floor(serve: Any) -> None:
    eng = NoisyEngine(jitter=0.01, host_drift=0.5)
    url = serve(eng)
    rep = _probe(url, noise_floor=_floor(url, eng))
    assert rep.ok is False and rep.passed == 0
    assert all("logprob" in " ".join(r.reasons) for r in rep.results)


def test_without_a_floor_the_report_has_no_noise_block(serve: Any) -> None:
    assert "noise_floor" not in _probe(serve(NoisyEngine())).to_dict()


def test_the_tolerance_argument_stays_a_lower_bound(serve: Any) -> None:
    eng = NoisyEngine(host_drift=0.02)
    url = serve(eng)
    nf = _floor(url, eng)  # zero floor
    assert _probe(url, noise_floor=nf, tolerance=0.05).tolerance == pytest.approx(0.05)
    assert _probe(url, noise_floor=nf, tolerance=0.05).ok is True


def test_flush_runs_once_before_the_first_request(serve: Any) -> None:
    eng = NoisyEngine()
    url = serve(eng)
    http_prompts = _prompts()
    eng.serve(http_prompts[0])  # pollute: the first probe prompt is already cached
    rep = _probe(url, flush=eng.flush)
    assert eng.flushes == 1
    assert rep.results[0].cached_tokens["cold"] == 0
    assert rep.ok


# ------------------------------------------------------------------ CLI
def _cli(argv: list[str]) -> int:
    from demo_bench.cli import main

    try:
        return int(main(argv))
    except SystemExit as exc:
        return int(exc.code or 0)


def _cli_args(tmp_path: Path, url: str, *extra: str) -> list[str]:
    (tmp_path / "prompts.json").write_text(json.dumps(_prompts()))
    (tmp_path / "filler.json").write_text(json.dumps(_fillers()))
    return ["restore-probe", "--base-url", url, "--model", "m", "--prompts",
            str(tmp_path / "prompts.json"), "--filler", str(tmp_path / "filler.json"), *extra]


def test_cli_flush_url_measures_and_uses_the_floor(
    serve: Any, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    eng = NoisyEngine(jitter=0.01, host_drift=0.02)
    url = serve(eng)
    out = tmp_path / "probe.json"
    assert _cli(_cli_args(tmp_path, url, "--flush-url", url + "/flush", "--out", str(out))) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["ok"] is True and doc["tolerance"] == pytest.approx(0.03, abs=1e-8)
    assert doc["noise_floor"]["floor"] == pytest.approx(0.01, abs=1e-9)
    assert json.loads(out.read_text()) == doc


def test_cli_without_flush_url_keeps_the_plain_tolerance(serve: Any, tmp_path: Path) -> None:
    eng = NoisyEngine(jitter=0.01, host_drift=0.02)
    assert _cli(_cli_args(tmp_path, serve(eng))) == 1


def test_cli_noise_k_option(serve: Any, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    eng = NoisyEngine(jitter=0.01, host_drift=0.02)
    url = serve(eng)
    assert _cli(_cli_args(tmp_path, url, "--flush-url", url + "/flush", "--noise-k", "1.5")) == 1
    assert json.loads(capsys.readouterr().out)["tolerance"] == pytest.approx(0.015, abs=1e-8)


def test_cli_noise_k_without_flush_url_is_invalid(serve: Any, tmp_path: Path) -> None:
    url = serve(NoisyEngine())
    assert _cli(_cli_args(tmp_path, url)) == 0  # control: no floor flags
    assert _cli(_cli_args(tmp_path, url, "--noise-k", "3")) == 2


def test_cli_failing_flush_exits_1(serve: Any, tmp_path: Path) -> None:
    eng = NoisyEngine(flush_status=500)
    url = serve(eng)
    assert _cli(_cli_args(tmp_path, url, "--flush-url", url + "/flush")) == 1


# ------------------------------------------------------------------ the image gate
STAGES = ["cold", "gpu", "host", "storage"]


def _report(passed: int, total: int = 16, stages: list[str] | None = None) -> dict[str, Any]:
    prompts = [{"index": i, "ok": i < passed, "reasons": [] if i < passed else ["host: logprob"],
                "cached_tokens": {}, "max_logprob_diff": {}} for i in range(total)]
    return {"total": total, "passed": passed, "ok": passed == total, "tolerance": 1e-3,
            "model": "m", "stages": stages or STAGES, "max_tokens": 64, "top_logprobs": 1,
            "prompts": prompts}


def _decide(derived: dict[str, Any], control: dict[str, Any], **kw: Any) -> Any:
    from demo_bench.application.image_gate import decide_image

    args = {"accept_length_ratio": 1.0, "prefill_ratio": 1.0}
    return decide_image(derived, control, **(args | kw))


@pytest.mark.parametrize(
    "case",
    [
        (True, False, "derived_only_pass", "derived", True, True),
        (True, True, "both_pass", "derived", True, False),
        (False, True, "control_only_pass", "dev1", True, True),
        (False, False, "both_fail", "dev1", False, False),
    ],
)
def test_four_outcomes(case: tuple[bool, bool, str, str, bool, bool]) -> None:
    derived_ok, control_ok, state, image, o_valid, discriminating = case
    d = _decide(_report(16 if derived_ok else 12), _report(16 if control_ok else 3))
    assert (d.state, d.image, d.o_valid, d.discriminating) == (state, image, o_valid, discriminating)
    assert d.reasons and all(isinstance(r, str) for r in d.reasons)
    assert state in json.dumps(d.to_dict())


def test_both_pass_is_flagged_as_not_discriminating_in_the_reasons() -> None:
    d = _decide(_report(16), _report(16))
    assert any("discriminat" in r for r in d.reasons)


def test_both_fail_says_hicache_is_out() -> None:
    d = _decide(_report(0), _report(0))
    assert any("both" in r.lower() for r in d.reasons)


@pytest.mark.parametrize(
    ("accept", "prefill", "image"),
    [(0.85, 0.8, "derived"), (0.849, 0.8, "dev1"), (0.85, 0.799, "dev1"), (1.2, 1.2, "derived")],
)
def test_other_conditions_are_inclusive_thresholds(accept: float, prefill: float, image: str) -> None:
    d = _decide(_report(16), _report(3), accept_length_ratio=accept, prefill_ratio=prefill)
    assert d.image == image


def test_derived_restore_ok_but_other_conditions_fail_falls_back_to_dev1_with_its_verdict() -> None:
    d = _decide(_report(16), _report(3), accept_length_ratio=0.5)
    assert (d.image, d.o_valid, d.state) == ("dev1", False, "derived_only_pass")
    assert any("accept" in r for r in d.reasons)
    d = _decide(_report(16), _report(16), prefill_ratio=0.1)
    assert (d.image, d.o_valid) == ("dev1", True)
    assert any("prefill" in r for r in d.reasons)


def test_a_report_needs_16_prompts_and_the_host_and_storage_stages() -> None:
    for bad in (_report(8, total=8), _report(16, stages=["cold", "gpu", "host"]),
                _report(16, stages=["cold", "gpu", "storage"])):
        with pytest.raises(ValueError):
            _decide(bad, _report(16))
        with pytest.raises(ValueError):
            _decide(_report(16), bad)
    assert _decide(_report(20, total=20), _report(0, total=20), required_prompts=20).image == "derived"


def test_15_of_16_is_a_failure() -> None:
    assert _decide(_report(15), _report(16)).image == "dev1"


def _cli_gate(tmp_path: Path, derived: dict[str, Any], control: dict[str, Any], *extra: str) -> int:
    (tmp_path / "d.json").write_text(json.dumps(derived))
    (tmp_path / "c.json").write_text(json.dumps(control))
    return _cli(["image-gate", "--derived", str(tmp_path / "d.json"), "--control",
                 str(tmp_path / "c.json"), "--accept-length-ratio", "0.9", "--prefill-ratio",
                 "0.95", *extra])


def test_cli_prints_the_decision(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    out = tmp_path / "gate.json"
    assert _cli_gate(tmp_path, _report(16), _report(2), "--out", str(out)) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["state"] == "derived_only_pass" and doc["image"] == "derived"
    assert doc["o_valid"] is True and doc["discriminating"] is True
    assert json.loads(out.read_text()) == doc


def test_cli_exit_0_even_when_both_fail(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert _cli_gate(tmp_path, _report(0), _report(0)) == 0
    assert json.loads(capsys.readouterr().out)["state"] == "both_fail"


def test_cli_invalid_input_exits_2(tmp_path: Path) -> None:
    assert _cli_gate(tmp_path, _report(16), _report(16)) == 0  # control
    assert _cli_gate(tmp_path, _report(8, total=8), _report(16)) == 2
    (tmp_path / "d.json").write_text("{")
    assert _cli(["image-gate", "--derived", str(tmp_path / "d.json"), "--control",
                 str(tmp_path / "c.json"), "--accept-length-ratio", "1", "--prefill-ratio", "1"]) == 2
    assert _cli(["image-gate", "--derived", str(tmp_path / "missing.json"), "--control",
                 str(tmp_path / "c.json"), "--accept-length-ratio", "1", "--prefill-ratio", "1"]) == 2
