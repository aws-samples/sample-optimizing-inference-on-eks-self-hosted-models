# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests for ``application.reset.reset_caches`` (spec 9.6 cache reset)."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import pytest


def _pods() -> list[Any]:
    from demo_bench.application.reset import WorkerPod

    return [
        WorkerPod(name="w0", host="10.0.0.1", is_hicache=False),
        WorkerPod(name="w1", host="10.0.0.2", is_hicache=True),
        WorkerPod(name="w2", host="10.0.0.3", is_hicache=True, system_port=9191),
    ]


def _ok(fake_response_cls: Any) -> Any:
    return lambda req: fake_response_cls(200, {"status": "ok"})


def test_flush_all_and_clear_storage_only_on_hicache(
    fake_http_client_cls: Any, fake_response_cls: Any
) -> None:
    from demo_bench.application.reset import reset_caches

    http = fake_http_client_cls(_ok(fake_response_cls))
    records = reset_caches(_pods(), http, sleep=lambda s: None)
    urls = sorted((r.method, r.url) for r in http.requests)
    assert urls == sorted([
        ("POST", "http://10.0.0.1:9090/engine/flush_cache"),
        ("POST", "http://10.0.0.2:9090/engine/flush_cache"),
        ("POST", "http://10.0.0.3:9191/engine/flush_cache"),
        ("POST", "http://10.0.0.2:9090/engine/clear_hicache_storage"),
        ("POST", "http://10.0.0.3:9191/engine/clear_hicache_storage"),
    ])
    assert len(records) == 5


def test_request_body_is_empty_json_object(
    fake_http_client_cls: Any, fake_response_cls: Any
) -> None:
    from demo_bench.application.reset import reset_caches

    http = fake_http_client_cls(_ok(fake_response_cls))
    reset_caches(_pods(), http, sleep=lambda s: None)
    assert all(r.json == {} for r in http.requests)


def test_flush_precedes_clear_storage_per_pod(
    fake_http_client_cls: Any, fake_response_cls: Any
) -> None:
    from demo_bench.application.reset import reset_caches

    http = fake_http_client_cls(_ok(fake_response_cls))
    reset_caches(_pods(), http, sleep=lambda s: None)
    for host in ("10.0.0.2", "10.0.0.3"):
        seq = [r.url.rsplit("/", 1)[-1] for r in http.requests if host in r.url]
        assert seq == ["flush_cache", "clear_hicache_storage"]


def test_evidence_records(fake_http_client_cls: Any, fake_response_cls: Any) -> None:
    from demo_bench.application.reset import reset_caches

    http = fake_http_client_cls(_ok(fake_response_cls))
    records = reset_caches(_pods(), http, sleep=lambda s: None)
    r = next(x for x in records if x.pod == "w1" and x.endpoint == "clear_hicache_storage")
    assert r.ok is True and r.status_code == 200 and r.attempts == 1
    assert {x.pod for x in records} == {"w0", "w1", "w2"}
    assert sum(1 for x in records if x.endpoint == "flush_cache") == 3
    json.dumps([x.to_dict() for x in records])  # evidence must be JSON-serialisable


def test_never_sends_cache_salt(fake_http_client_cls: Any, fake_response_cls: Any) -> None:
    from demo_bench.application.reset import reset_caches

    http = fake_http_client_cls(_ok(fake_response_cls))
    records = reset_caches(_pods(), http, sleep=lambda s: None)
    for req in http.requests:
        blob = json.dumps(
            [req.method, req.url, req.json, dict(req.params or {}), dict(req.headers or {})]
        ).lower()
        assert "cache_salt" not in blob and "salt" not in blob
    assert "cache_salt" not in json.dumps([x.to_dict() for x in records]).lower()


def test_retries_with_exponential_backoff_when_requests_active(
    fake_http_client_cls: Any, fake_response_cls: Any
) -> None:
    from demo_bench.application.reset import WorkerPod, reset_caches

    calls = {"n": 0}

    def handler(req: Any) -> Any:
        calls["n"] += 1
        if calls["n"] <= 2:
            return fake_response_cls(
                409, {}, "Cache not flushed because there are requests are active (#running-req: 3)"
            )
        return fake_response_cls(200, {"status": "ok"})

    sleeps: list[float] = []
    http = fake_http_client_cls(handler)
    records = reset_caches([WorkerPod("w0", "10.0.0.1", False)], http, sleep=sleeps.append,
                           backoff_initial_s=1.0, backoff_factor=2.0, max_attempts=5)
    assert len(http.requests) == 3
    assert sleeps == [1.0, 2.0]
    assert records[0].attempts == 3 and records[0].ok


def test_persistent_busy_raises_after_bounded_attempts(
    fake_http_client_cls: Any, fake_response_cls: Any
) -> None:
    from demo_bench.application.reset import ResetError, WorkerPod, reset_caches

    http = fake_http_client_cls(lambda r: fake_response_cls(409, {}, "requests are active"))
    sleeps: list[float] = []
    with pytest.raises(ResetError) as ei:
        reset_caches([WorkerPod("w0", "10.0.0.1", False)], http, sleep=sleeps.append,
                     backoff_initial_s=1.0, backoff_factor=2.0, max_attempts=4)
    assert len(http.requests) == 4
    assert sleeps == [1.0, 2.0, 4.0]
    assert "w0" in str(ei.value)
    assert isinstance(ei.value.records, list)


def test_non_busy_error_fails_fast_without_retry(
    fake_http_client_cls: Any, fake_response_cls: Any
) -> None:
    from demo_bench.application.reset import ResetError, WorkerPod, reset_caches

    http = fake_http_client_cls(lambda r: fake_response_cls(500, {}, "internal error"))
    sleeps: list[float] = []
    with pytest.raises(ResetError):
        reset_caches([WorkerPod("w0", "10.0.0.1", False)], http, sleep=sleeps.append,
                     max_attempts=5)
    assert len(http.requests) == 1 and sleeps == []


def test_failure_on_one_pod_does_not_report_success(
    fake_http_client_cls: Any, fake_response_cls: Any
) -> None:
    from demo_bench.application.reset import ResetError, reset_caches

    def handler(req: Any) -> Any:
        if "10.0.0.2" in req.url:
            return fake_response_cls(500, {}, "boom")
        return fake_response_cls(200, {})

    with pytest.raises(ResetError, match="w1"):
        reset_caches(_pods(), fake_http_client_cls(handler), sleep=lambda s: None)


def test_empty_pod_list_is_an_error(fake_http_client_cls: Any, fake_response_cls: Any) -> None:
    from demo_bench.application.reset import ResetError, reset_caches

    with pytest.raises(ResetError):
        reset_caches([], fake_http_client_cls(_ok(fake_response_cls)), sleep=lambda s: None)


# --------------------------------------------------------------------------- "success: false" busy
# Primary source (Dynamo v1.5.1, ../dynamo): components/src/dynamo/sglang/engine_routes.py
#   - normalize_engine_route_value() lines 348-392 turns the SGLang ``FlushCacheReqOutput``
#     dataclass returned by the ``flush_cache:tm`` route into a plain dict (dataclass branch ~371);
#   - normalize_engine_route_result() lines 395-409 returns that dict unchanged (tuple results are
#     mapped to success/message/num_paused_requests at 399-405);
#   - tests/test_engine_routes.py:339 asserts a ``{"success": False, "message": ...}`` result is
#     passed through as the route body.
# Nothing in the route turns ``success: false`` into a non-2xx status, so a busy worker answers
# HTTP 200 with body ``{"success": false}`` and the reset must not read that as success.
def test_http_200_success_false_is_busy_and_retried_then_succeeds(
    fake_http_client_cls: Any, fake_response_cls: Any
) -> None:
    """HTTP 200 ``{"success": false}`` (v1.5.1 ``flush_cache:tm``) is busy: retry, then pass."""
    from demo_bench.application.reset import WorkerPod, reset_caches

    calls = {"n": 0}

    def handler(req: Any) -> Any:
        calls["n"] += 1
        if calls["n"] <= 2:
            return fake_response_cls(200, {"success": False, "message": "busy"})
        return fake_response_cls(200, {"success": True})

    sleeps: list[float] = []
    http = fake_http_client_cls(handler)
    records = reset_caches([WorkerPod("w0", "10.0.0.1", False)], http, sleep=sleeps.append,
                           backoff_initial_s=1.0, backoff_factor=2.0, max_attempts=5)
    assert len(http.requests) == 3 and sleeps == [1.0, 2.0]
    assert records[0].attempts == 3 and records[0].ok


def test_http_200_success_false_persistent_fails_after_bounded_attempts(
    fake_http_client_cls: Any, fake_response_cls: Any
) -> None:
    """Persistent 200 ``{"success": false}`` raises ``ResetError`` after ``max_attempts``."""
    from demo_bench.application.reset import ResetError, WorkerPod, reset_caches

    http = fake_http_client_cls(lambda r: fake_response_cls(200, {"success": False}))
    sleeps: list[float] = []
    with pytest.raises(ResetError) as ei:
        reset_caches([WorkerPod("w0", "10.0.0.1", False)], http, sleep=sleeps.append,
                     backoff_initial_s=1.0, backoff_factor=2.0, max_attempts=3)
    assert len(http.requests) == 3 and sleeps == [1.0, 2.0]
    assert "w0" in str(ei.value)
    assert ei.value.records and not ei.value.records[-1].ok
    assert ei.value.records[-1].attempts == 3


def test_http_200_success_true_is_not_busy(
    fake_http_client_cls: Any, fake_response_cls: Any
) -> None:
    """HTTP 200 ``{"success": true}`` passes on the first attempt without sleeping."""
    from demo_bench.application.reset import WorkerPod, reset_caches

    http = fake_http_client_cls(lambda r: fake_response_cls(200, {"success": True}))
    sleeps: list[float] = []
    records = reset_caches([WorkerPod("w0", "10.0.0.1", False)], http, sleep=sleeps.append)
    assert len(http.requests) == 1 and sleeps == [] and records[0].ok


class _BusyHandler(BaseHTTPRequestHandler):
    hits = 0

    def do_POST(self) -> None:
        type(self).hits += 1
        body = b'{"success": false}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        return


def test_cli_reset_busy_200_exits_nonzero_and_still_writes_evidence(tmp_path: Path) -> None:
    """CLI against a real local HTTP server answering 200 ``{"success": false}``.

    Expected: bounded retries, non-zero exit, and the evidence file written with ``ok: false``.
    """
    from demo_bench.cli import main

    _BusyHandler.hits = 0
    server = HTTPServer(("127.0.0.1", 0), _BusyHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    out = tmp_path / "reset.json"
    try:
        port = server.server_address[1]
        try:
            rc = main(["reset", "--pod", f"w0=127.0.0.1:{port}", "--max-attempts", "3",
                       "--backoff-initial-s", "0", "--backoff-factor", "1",
                       "--timeout-s", "5", "--out", str(out)])
        except SystemExit as exc:
            rc = int(exc.code or 0)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    assert rc != 0
    assert _BusyHandler.hits == 3
    doc = json.loads(out.read_text())
    assert doc["ok"] is False and doc["error"]
    assert doc["records"][-1]["attempts"] == 3 and doc["records"][-1]["ok"] is False
