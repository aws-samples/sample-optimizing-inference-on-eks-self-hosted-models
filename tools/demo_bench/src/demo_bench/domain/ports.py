# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Ports (interfaces) the application layer depends on.

Adapters live in ``demo_bench.infrastructure``; tests inject fakes. Nothing here performs I/O.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


class HttpTransportError(Exception):
    """Raised by an :class:`HttpClient` when no HTTP response was received.

    Examples: connection refused, DNS failure, timeout. HTTP error statuses are *not*
    transport errors: they are returned as an :class:`HttpResponse`.
    """


@runtime_checkable
class HttpResponse(Protocol):
    """Minimal HTTP response contract."""

    @property
    def status_code(self) -> int:
        """HTTP status code."""
        ...

    @property
    def text(self) -> str:
        """Decoded response body."""
        ...

    def json(self) -> Any:
        """Return the body decoded as JSON.

        Returns:
            The decoded JSON document.
        """
        ...


@runtime_checkable
class HttpClient(Protocol):
    """Synchronous HTTP transport used by the Prometheus and worker system-port adapters."""

    def request(
        self,
        method: str,
        url: str,
        *,
        json: Any = None,
        params: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
        timeout: float | None = None,
    ) -> HttpResponse:
        """Send one HTTP request.

        Args:
            method: HTTP method, e.g. ``GET`` or ``POST``.
            url: Absolute URL.
            json: Optional JSON-serialisable body.
            params: Optional query-string parameters.
            headers: Optional request headers.
            timeout: Optional timeout in seconds.

        Returns:
            The HTTP response (any status code).

        Raises:
            HttpTransportError: If no response was received.
        """
        ...


@dataclass(frozen=True)
class Sample:
    """One instant-vector sample returned by Prometheus.

    Attributes:
        labels: Series labels.
        value: Sample value (may be ``nan``/``inf`` as reported by Prometheus).
    """

    labels: dict[str, str] = field(default_factory=dict)
    value: float = 0.0


@runtime_checkable
class MetricsQuerier(Protocol):
    """Instant-query port over a Prometheus-compatible backend."""

    def query(self, promql: str, at: float) -> list[Sample]:
        """Evaluate ``promql`` at unix time ``at``.

        Args:
            promql: PromQL expression.
            at: Evaluation time, unix seconds.

        Returns:
            The instant-vector samples (empty when the series is absent).
        """
        ...

    def query_scalar(self, promql: str, at: float) -> float | None:
        """Evaluate a single-valued expression.

        Args:
            promql: PromQL expression expected to return at most one sample.
            at: Evaluation time, unix seconds.

        Returns:
            The value, or ``None`` when absent or not finite (never a fabricated 0).
        """
        ...
