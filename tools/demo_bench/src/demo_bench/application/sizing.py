# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Per-rank capacity report and expected tier mix (spec 9.5, 10.3).

The device pool and bytes/token can come from :class:`~demo_bench.domain.kv_sizing.KvSizing`
(dense/GQA models) or be taken verbatim from the SGLang boot log (required for MLA/DSA models,
where the GQA formula does not apply).
"""

from __future__ import annotations

from collections.abc import Hashable, Sequence
from dataclasses import asdict, dataclass
from typing import Any

from demo_bench.domain.kv_sizing import TierHits, host_pool_tokens, replay_lru, unique_tokens


@dataclass(frozen=True)
class SizingReport:
    """Capacity per DP rank and, optionally, the LRU tier mix of a trace.

    Attributes:
        bytes_per_token: KV bytes per token.
        device_tokens_per_rank: Device (L1) pool per rank.
        host_tokens_per_rank: HiCache host (L2) pool per rank.
        storage_tokens_per_rank: Storage (L3) budget per rank used by the LRU model.
        unique_tokens_per_rank: Distinct tokens across L1 and L2.
        write_through: Whether L2 mirrors L1.
        session_tokens: Session length used for ``sessions_per_rank``.
        sessions_per_rank: Whole sessions that fit on device.
        tier_hits: LRU replay outcome, when a trace was given.
    """

    bytes_per_token: int
    device_tokens_per_rank: int
    host_tokens_per_rank: int
    storage_tokens_per_rank: int
    unique_tokens_per_rank: int
    write_through: bool
    session_tokens: int | None
    sessions_per_rank: int | None
    tier_hits: TierHits | None

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable view including tier fractions.

        Returns:
            The report as a dict.
        """
        doc = asdict(self)
        if self.tier_hits is not None:
            total = self.tier_hits.accesses
            doc["tier_hits"]["accesses"] = total
            doc["tier_fractions"] = {
                tier: (getattr(self.tier_hits, tier) / total if total else None)
                for tier in ("device", "host", "storage", "miss")
            }
        return doc


def sizing_report(
    *,
    bytes_per_token: int,
    device_tokens_per_rank: int,
    hicache_size_gb: float = 0.0,
    storage_tokens_per_rank: int = 0,
    write_through: bool = True,
    session_tokens: int | None = None,
    accesses: Sequence[tuple[Hashable, int]] | None = None,
) -> SizingReport:
    """Compute the per-rank capacity report.

    Args:
        bytes_per_token: KV bytes per token.
        device_tokens_per_rank: Device pool per rank (KvSizing or boot log).
        hicache_size_gb: ``--hicache-size`` per rank in GB (0 = HiCache off).
        storage_tokens_per_rank: L3 budget per rank in tokens for the LRU model (0 = off).
        write_through: HiCache write policy mirrors L1 into L2.
        session_tokens: Optional session length for ``sessions_per_rank``.
        accesses: Optional ``(session_key, tokens)`` accesses replayed through the LRU tiers.

    Returns:
        The report.

    Raises:
        ValueError: On non-positive sizes.
    """
    if bytes_per_token <= 0 or device_tokens_per_rank <= 0:
        raise ValueError("bytes_per_token and device_tokens_per_rank must be > 0")
    if storage_tokens_per_rank < 0:
        raise ValueError("storage_tokens_per_rank must be >= 0")
    if session_tokens is not None and session_tokens <= 0:
        raise ValueError(f"session_tokens must be > 0, got {session_tokens}")
    host = host_pool_tokens(hicache_size_gb, bytes_per_token)
    hits = (
        replay_lru(
            accesses,
            device_tokens=device_tokens_per_rank,
            host_tokens=host,
            storage_tokens=storage_tokens_per_rank,
        )
        if accesses is not None
        else None
    )
    return SizingReport(
        bytes_per_token=bytes_per_token,
        device_tokens_per_rank=device_tokens_per_rank,
        host_tokens_per_rank=host,
        storage_tokens_per_rank=storage_tokens_per_rank,
        unique_tokens_per_rank=unique_tokens(
            device_tokens_per_rank, host, write_through=write_through
        ),
        write_through=write_through,
        session_tokens=session_tokens,
        sessions_per_rank=(
            device_tokens_per_rank // session_tokens if session_tokens is not None else None
        ),
        tier_hits=hits,
    )
