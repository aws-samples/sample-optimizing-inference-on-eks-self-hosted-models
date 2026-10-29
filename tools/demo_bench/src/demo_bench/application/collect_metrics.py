# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Per-run engine/router/tier KPIs from Prometheus over the aiperf window (spec 10.1, 11)."""

from __future__ import annotations

import logging
import math
from dataclasses import asdict, dataclass, field
from typing import Any

from demo_bench.domain.ports import MetricsQuerier
from demo_bench.domain.promql import (
    engine_hit_rate_query,
    router_predicted_hit_query,
    tier_mix_query,
)
from demo_bench.domain.window import RunWindow

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RunMetrics:
    """KPIs of one run; absent signals are ``None`` (or an empty mapping), never 0.

    Attributes:
        engine_hit_rate: ``increase(cached)/increase(prompt)`` over the window.
        router_predicted_hit: Mean router-predicted hit (only in ``kv`` arms).
        tier_tokens: Cached tokens by ``cache_source`` over the window.
    """

    engine_hit_rate: float | None
    router_predicted_hit: float | None
    tier_tokens: dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable view.

        Returns:
            The metrics as a dict.
        """
        return asdict(self)


def collect_run_metrics(prom: MetricsQuerier, namespace: str, window: RunWindow) -> RunMetrics:
    """Evaluate the three KPI queries at the end of the profiling window.

    Args:
        prom: Prometheus instant-query port.
        namespace: DGD namespace.
        window: aiperf profiling window (warmup excluded).

    Returns:
        The run KPIs.
    """
    seconds = window.seconds
    engine = prom.query_scalar(engine_hit_rate_query(namespace, seconds), at=window.end_s)
    router = prom.query_scalar(router_predicted_hit_query(namespace, seconds), at=window.end_s)
    tiers: dict[str, float] = {}
    for sample in prom.query(tier_mix_query(namespace, seconds), at=window.end_s):
        source = sample.labels.get("cache_source")
        if source is not None and math.isfinite(sample.value):
            tiers[source] = sample.value
    result = RunMetrics(engine_hit_rate=engine, router_predicted_hit=router, tier_tokens=tiers)
    logger.info(
        "run metrics collected",
        extra={"namespace": namespace, "window_s": seconds, **result.to_dict()},
    )
    return result
