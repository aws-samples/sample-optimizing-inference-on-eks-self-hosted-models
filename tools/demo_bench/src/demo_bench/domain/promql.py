# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""PromQL for the per-run KPIs (spec 10.1, 11).

All queries are scoped by ``namespace`` and use ``increase`` over the aiperf profiling window.
The engine hit rate is ``increase(cached)/increase(prompt)``, never the ``sglang:cache_hit_rate``
gauge (last batch only).
"""

from __future__ import annotations

import re

_NAMESPACE_RE = re.compile(r"[a-z0-9]([-a-z0-9]*[a-z0-9])?")
_MAX_NAMESPACE_LEN = 63


def _validate(namespace: str, window_s: int) -> None:
    if (
        not isinstance(namespace, str)
        or len(namespace) > _MAX_NAMESPACE_LEN
        or not _NAMESPACE_RE.fullmatch(namespace)
    ):
        raise ValueError(f"invalid Kubernetes namespace {namespace!r}")
    if isinstance(window_s, bool) or not isinstance(window_s, int) or window_s <= 0:
        raise ValueError(f"window must be a positive number of seconds, got {window_s!r}")


def engine_hit_rate_query(namespace: str, window_s: int) -> str:
    """Engine prefix-cache hit rate over the window.

    Args:
        namespace: DGD namespace.
        window_s: Window length in seconds.

    Returns:
        PromQL expression.

    Raises:
        ValueError: On an invalid namespace or a non-positive window.
    """
    _validate(namespace, window_s)
    sel = f'{{namespace="{namespace}"}}[{window_s}s]'
    return (
        f"sum(increase(sglang:cached_tokens_total{sel})) / "
        f"sum(increase(sglang:prompt_tokens_total{sel}))"
    )


def router_predicted_hit_query(namespace: str, window_s: int) -> str:
    """Router-predicted KV hit (histogram mean), observed only with ``kv`` routing.

    ``increase(_sum) / increase(_count)`` over the window, with no ``clamp_min`` on the
    denominator (correction to spec 11, 2026-10-18: a clamped denominator understates the
    hit rate). When the count did not increase the result is NaN (0/0), which the Prometheus
    adapter reports as ``None``, never 0.

    Args:
        namespace: DGD namespace.
        window_s: Window length in seconds.

    Returns:
        PromQL expression.

    Raises:
        ValueError: On an invalid namespace or a non-positive window.
    """
    _validate(namespace, window_s)
    sel = f'{{namespace="{namespace}"}}[{window_s}s]'
    return (
        f"sum(increase(dynamo_component_router_kv_hit_rate_sum{sel})) / "
        f"sum(increase(dynamo_component_router_kv_hit_rate_count{sel}))"
    )


def tier_mix_query(namespace: str, window_s: int) -> str:
    """Cached tokens by tier (``device`` / ``host`` / ``storage_*``), excluding ``total``.

    Args:
        namespace: DGD namespace.
        window_s: Window length in seconds.

    Returns:
        PromQL expression.

    Raises:
        ValueError: On an invalid namespace or a non-positive window.
    """
    _validate(namespace, window_s)
    return (
        "sum by (cache_source) (increase(sglang:cached_tokens_total"
        f'{{namespace="{namespace}",cache_source!="total"}}[{window_s}s]))'
    )
