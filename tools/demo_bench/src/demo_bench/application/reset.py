# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Cache reset on every worker pod through the Dynamo worker system port (spec 9.6).

``POST /engine/flush_cache`` on every worker, then ``POST /engine/clear_hicache_storage`` on
HiCache workers (workers start with ``--engine-route flush_cache:tm`` and, on O workers,
``clear_hicache_storage:tm``). No ``cache_salt`` is ever sent: on Dynamo 1.5.1 it breaks ``kv``
routing for SGLang.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from typing import Any

from demo_bench.domain.ports import HttpClient, HttpResponse, HttpTransportError

logger = logging.getLogger(__name__)

FLUSH_CACHE = "flush_cache"
CLEAR_HICACHE_STORAGE = "clear_hicache_storage"
BUSY_MARKER = "requests are active"
HTTP_OK = 200


@dataclass(frozen=True)
class WorkerPod:
    """A worker pod reachable on its Dynamo system port.

    Attributes:
        name: Pod name (evidence only).
        host: Pod IP or DNS name.
        is_hicache: Whether the worker runs HiCache (also clear L3 storage).
        system_port: ``DYN_SYSTEM_PORT`` of the worker.
    """

    name: str
    host: str
    is_hicache: bool
    system_port: int = 9090


@dataclass(frozen=True)
class ResetRecord:
    """Evidence of one reset call, written to the run metadata.

    Attributes:
        pod: Pod name.
        endpoint: ``flush_cache`` or ``clear_hicache_storage``.
        status_code: Last HTTP status (0 when no response was received).
        attempts: Number of attempts made.
        ok: Whether the call succeeded.
    """

    pod: str
    endpoint: str
    status_code: int
    attempts: int
    ok: bool

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable view.

        Returns:
            The record as a dict.
        """
        return asdict(self)


class ResetError(RuntimeError):
    """The reset could not be completed; the run must not start (fallback: recreate pods).

    Attributes:
        records: Evidence collected up to and including the failing call.
    """

    def __init__(self, message: str, records: list[ResetRecord]) -> None:
        """Create the error.

        Args:
            message: Human-readable reason (names the pod).
            records: Evidence collected so far.
        """
        super().__init__(message)
        self.records = records


def _reported_failure(response: HttpResponse) -> bool:
    """SGLang ``FlushCacheReqOutput``/``ClearHiCacheReqOutput`` report ``success: false``."""
    try:
        body = response.json()
    except (ValueError, TypeError):
        return False
    return isinstance(body, dict) and body.get("success") is False


def _is_busy(response: HttpResponse) -> bool:
    if BUSY_MARKER in response.text:
        return True
    # SGLang refuses to flush while requests are queued or running and answers 200 with
    # {"success": false} through the "tm" engine route; treat it as busy (retried, bounded).
    return response.status_code == HTTP_OK and _reported_failure(response)


def _call(
    pod: WorkerPod,
    endpoint: str,
    http: HttpClient,
    *,
    sleep: Callable[[float], None],
    max_attempts: int,
    backoff_initial_s: float,
    backoff_factor: float,
    max_wait_s: float,
    timeout_s: float,
    records: list[ResetRecord],
) -> None:
    url = f"http://{pod.host}:{pod.system_port}/engine/{endpoint}"
    waited = 0.0
    attempt = 0
    while True:
        attempt += 1
        try:
            response = http.request("POST", url, json={}, timeout=timeout_s)
        except HttpTransportError as exc:
            records.append(ResetRecord(pod.name, endpoint, 0, attempt, ok=False))
            msg = f"{pod.name}: {endpoint} unreachable at {url}: {exc}"
            raise ResetError(msg, records) from exc
        busy = _is_busy(response)
        if response.status_code == HTTP_OK and not busy:
            records.append(ResetRecord(pod.name, endpoint, response.status_code, attempt, ok=True))
            logger.info(
                "cache reset call ok",
                extra={"pod": pod.name, "endpoint": endpoint, "attempts": attempt},
            )
            return
        if not busy:
            records.append(ResetRecord(pod.name, endpoint, response.status_code, attempt, False))
            raise ResetError(
                f"{pod.name}: {endpoint} failed with HTTP {response.status_code}: "
                f"{response.text[:200]}",
                records,
            )
        delay = backoff_initial_s * backoff_factor ** (attempt - 1)
        if attempt >= max_attempts or waited + delay > max_wait_s:
            records.append(ResetRecord(pod.name, endpoint, response.status_code, attempt, False))
            raise ResetError(
                f"{pod.name}: {endpoint} still busy after {attempt} attempts "
                f"({waited:.1f}s waited): {response.text[:200]}",
                records,
            )
        logger.warning(
            "worker busy, retrying cache reset",
            extra={"pod": pod.name, "endpoint": endpoint, "attempt": attempt, "sleep_s": delay},
        )
        sleep(delay)
        waited += delay


def reset_caches(
    pods: Sequence[WorkerPod],
    http: HttpClient,
    *,
    sleep: Callable[[float], None] = time.sleep,
    max_attempts: int = 6,
    backoff_initial_s: float = 1.0,
    backoff_factor: float = 2.0,
    max_wait_s: float = 60.0,
    timeout_s: float = 30.0,
) -> list[ResetRecord]:
    """Flush every worker's cache, then clear HiCache storage on HiCache workers.

    A response containing ``requests are active`` (any status), or a 200 reporting
    ``success: false``, is "busy": sleep ``backoff_initial_s * backoff_factor**i`` and retry, up to
    ``max_attempts`` attempts and ``max_wait_s`` of cumulative sleep. Any other non-200 fails fast.

    Args:
        pods: Worker pods of the scenario namespace.
        http: HTTP transport.
        sleep: Sleep function (injectable for tests).
        max_attempts: Attempts per call, including the first.
        backoff_initial_s: First backoff delay.
        backoff_factor: Backoff multiplier.
        max_wait_s: Maximum cumulative backoff per call.
        timeout_s: Per-request timeout.

    Returns:
        One evidence record per call.

    Raises:
        ResetError: If the pod list is empty or any call fails; ``records`` holds the evidence.
        ValueError: If the retry parameters are invalid.
    """
    if max_attempts < 1 or backoff_initial_s < 0 or backoff_factor < 1 or max_wait_s < 0:
        raise ValueError("invalid retry parameters")
    records: list[ResetRecord] = []
    if not pods:
        raise ResetError("no worker pods to reset", records)
    plan = [(pod, FLUSH_CACHE) for pod in pods]
    plan += [(pod, CLEAR_HICACHE_STORAGE) for pod in pods if pod.is_hicache]
    for pod, endpoint in plan:
        _call(
            pod,
            endpoint,
            http,
            sleep=sleep,
            max_attempts=max_attempts,
            backoff_initial_s=backoff_initial_s,
            backoff_factor=backoff_factor,
            max_wait_s=max_wait_s,
            timeout_s=timeout_s,
            records=records,
        )
    logger.info(
        "cache reset complete",
        extra={"pods": [p.name for p in pods], "calls": len(records)},
    )
    return records


def records_to_json(records: Sequence[ResetRecord], error: str | None = None) -> str:
    """Serialise reset evidence for the run metadata.

    Args:
        records: Evidence records.
        error: Failure reason, if the reset failed.

    Returns:
        JSON text.
    """
    doc: dict[str, Any] = {
        "ok": error is None and bool(records) and all(r.ok for r in records),
        "error": error,
        "records": [r.to_dict() for r in records],
    }
    return json.dumps(doc, indent=2) + "\n"
