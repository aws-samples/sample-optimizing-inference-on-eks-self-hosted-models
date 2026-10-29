# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Prometheus adapter (instant queries) and the per-run KPI PromQL builders."""

from __future__ import annotations

import logging
import math
from typing import Any

from demo_bench.domain.ports import HttpClient, HttpTransportError, Sample
from demo_bench.domain.promql import (
    engine_hit_rate_query,
    router_predicted_hit_query,
    tier_mix_query,
)

__all__ = [
    "PrometheusClient",
    "PrometheusError",
    "Sample",
    "engine_hit_rate_query",
    "router_predicted_hit_query",
    "tier_mix_query",
]

logger = logging.getLogger(__name__)

HTTP_OK = 200


class PrometheusError(RuntimeError):
    """Prometheus returned an HTTP error, an error status or an unexpected payload."""


def _parse_value(pair: Any) -> float:
    if not isinstance(pair, list | tuple) or len(pair) != 2:  # noqa: PLR2004 - [ts, "value"]
        raise PrometheusError(f"unexpected sample value {pair!r}")
    return float(pair[1])


class PrometheusClient:
    """Instant-query client for the Prometheus HTTP API (``/api/v1/query``)."""

    def __init__(self, base_url: str, http: HttpClient, *, timeout_s: float = 30.0) -> None:
        """Create the client.

        Args:
            base_url: Prometheus base URL, e.g. ``http://prometheus.monitoring.svc:9090``.
            http: HTTP transport.
            timeout_s: Per-request timeout.
        """
        self._url = base_url.rstrip("/") + "/api/v1/query"
        self._http = http
        self._timeout_s = timeout_s

    def query(self, promql: str, at: float) -> list[Sample]:
        """Evaluate an instant query.

        Args:
            promql: PromQL expression.
            at: Evaluation time, unix seconds.

        Returns:
            Samples (vector) or a single unlabelled sample (scalar); empty when absent.

        Raises:
            PrometheusError: On transport failure, non-200, ``status: error`` or a bad payload.
        """
        params = {"query": promql, "time": f"{at:.3f}"}
        try:
            response = self._http.request("GET", self._url, params=params, timeout=self._timeout_s)
        except HttpTransportError as exc:
            raise PrometheusError(f"prometheus unreachable at {self._url}: {exc}") from exc
        if response.status_code != HTTP_OK:
            raise PrometheusError(f"HTTP {response.status_code}: {response.text[:300]}")
        try:
            body = response.json()
        except ValueError as exc:
            raise PrometheusError(f"non-JSON response: {response.text[:300]}") from exc
        if not isinstance(body, dict):
            raise PrometheusError(f"unexpected payload: {body!r}")
        if body.get("status") != "success":
            raise PrometheusError(
                f"query failed ({body.get('errorType', 'unknown')}): {body.get('error', body)}"
            )
        data = body.get("data") or {}
        result_type, result = data.get("resultType"), data.get("result")
        if result_type == "scalar":
            return [Sample(labels={}, value=_parse_value(result))]
        if result_type != "vector" or not isinstance(result, list):
            raise PrometheusError(f"unsupported result type {result_type!r}")
        samples = [
            Sample(labels=dict(item.get("metric") or {}), value=_parse_value(item.get("value")))
            for item in result
        ]
        logger.debug("prometheus query", extra={"promql": promql, "at": at, "n": len(samples)})
        return samples

    def query_scalar(self, promql: str, at: float) -> float | None:
        """Evaluate a single-valued expression.

        Args:
            promql: PromQL expression returning at most one sample.
            at: Evaluation time, unix seconds.

        Returns:
            The value, or ``None`` when absent, NaN or infinite (never a fabricated 0).

        Raises:
            PrometheusError: On query failure or more than one sample.
        """
        samples = self.query(promql, at)
        if not samples:
            return None
        if len(samples) > 1:
            raise PrometheusError(f"expected at most one sample, got {len(samples)}: {promql}")
        value = samples[0].value
        return value if math.isfinite(value) else None
