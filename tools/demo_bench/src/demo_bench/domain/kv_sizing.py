# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""KV-cache capacity arithmetic and the LRU tier model (spec 9.5, 10.3, 12.1).

The KV footprint formula here is the dense/GQA one, ``2 * layers * kv_heads * head_dim * bytes``.
It does not apply to MLA/DSA models; for those, pass the per-token size and the device
pool read from the SGLang boot log to :func:`demo_bench.application.sizing.sizing_report`.
"""

from __future__ import annotations

import json
import math
from collections import OrderedDict
from collections.abc import Hashable, Sequence
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Any

GIB = 2**30
GB = 10**9

KV_DTYPE_BYTES: dict[str, int] = {
    "fp8_e4m3": 1,
    "fp8_e5m2": 1,
    "bfloat16": 2,
    "float16": 2,
}


def _exact(value: float) -> Fraction:
    """Return the decimal value a user typed (``0.88`` -> ``22/25``), avoiding float drift."""
    return Fraction(str(value))


@dataclass(frozen=True)
class ModelDims:
    """Attention dimensions that drive the KV footprint.

    Attributes:
        num_layers: ``num_hidden_layers`` from ``config.json``.
        num_kv_heads: ``num_key_value_heads`` from ``config.json``.
        head_dim: ``head_dim`` from ``config.json`` (read, never derived).
    """

    num_layers: int
    num_kv_heads: int
    head_dim: int


def _positive_int(cfg: dict[str, Any], key: str) -> int:
    if key not in cfg:
        raise ValueError(f"config.json has no {key!r}; refusing to derive it")
    value = cfg[key]
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"config.json {key!r} must be a positive integer, got {value!r}")
    return value


def load_model_dims(path: Path) -> ModelDims:
    """Read the KV-relevant dimensions from a Hugging Face ``config.json``.

    ``head_dim`` must be explicit: deriving it as ``hidden_size // num_attention_heads`` gives
    80 instead of 128 for Qwen3-32B (the Dynamo mocker bug).

    Args:
        path: Path to ``config.json``.

    Returns:
        The model dimensions.

    Raises:
        ValueError: If ``head_dim`` (or another required key) is missing or invalid.
    """
    cfg = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(cfg, dict):
        raise ValueError(f"{path}: config.json must be a JSON object")
    return ModelDims(
        num_layers=_positive_int(cfg, "num_hidden_layers"),
        num_kv_heads=_positive_int(cfg, "num_key_value_heads"),
        head_dim=_positive_int(cfg, "head_dim"),
    )


def kv_bytes_per_token(dims: ModelDims, kv_dtype: str) -> int:
    """KV bytes per token for one full model replica: ``2 * layers * kv_heads * head_dim * b``.

    Args:
        dims: Model dimensions.
        kv_dtype: KV-cache dtype (``fp8_e4m3``, ``fp8_e5m2``, ``bfloat16``, ``float16``).

    Returns:
        Bytes per token (K and V).

    Raises:
        ValueError: If the dtype is unknown.
    """
    try:
        width = KV_DTYPE_BYTES[kv_dtype]
    except KeyError:
        known = ", ".join(sorted(KV_DTYPE_BYTES))
        raise ValueError(f"unknown kv dtype {kv_dtype!r}; expected one of {known}") from None
    return 2 * dims.num_layers * dims.num_kv_heads * dims.head_dim * width


def host_pool_tokens(hicache_size_gb: float, bytes_per_token: int) -> int:
    """Tokens that fit in a HiCache host pool sized in decimal GB (``--hicache-size``).

    Args:
        hicache_size_gb: Host pool size per rank in GB (10**9 bytes, not GiB).
        bytes_per_token: KV bytes per token.

    Returns:
        Floor of the token capacity.

    Raises:
        ValueError: If the size is negative or ``bytes_per_token`` is not positive.
    """
    if hicache_size_gb < 0:
        raise ValueError(f"hicache_size_gb must be >= 0, got {hicache_size_gb}")
    if bytes_per_token <= 0:
        raise ValueError(f"bytes_per_token must be > 0, got {bytes_per_token}")
    return math.floor(_exact(hicache_size_gb) * GB / bytes_per_token)


def unique_tokens(device_tokens: int, host_tokens: int, *, write_through: bool = True) -> int:
    """Distinct tokens held by device plus host tiers.

    Args:
        device_tokens: Device (L1) pool in tokens.
        host_tokens: Host (L2) pool in tokens.
        write_through: When true, L2 mirrors L1 so unique capacity is the larger pool.

    Returns:
        Unique token capacity.
    """
    return max(device_tokens, host_tokens) if write_through else device_tokens + host_tokens


@dataclass(frozen=True)
class KvSizing:
    """Per-rank KV capacity for one DP rank of a worker.

    Attributes:
        dims: Model dimensions.
        kv_dtype: KV-cache dtype.
        hbm_gib: HBM per GPU, GiB.
        mem_fraction: ``--mem-fraction-static``, in (0, 1].
        weights_gib: Weights resident per GPU, GiB.
        overhead_gib: Other static reservations per GPU, GiB.
    """

    dims: ModelDims
    kv_dtype: str
    hbm_gib: float
    mem_fraction: float
    weights_gib: float
    overhead_gib: float = 0.0

    def __post_init__(self) -> None:
        """Validate the static configuration.

        Raises:
            ValueError: If ``mem_fraction`` is outside (0, 1] or the dtype is unknown.
        """
        if not 0.0 < self.mem_fraction <= 1.0:
            raise ValueError(f"mem_fraction must be in (0, 1], got {self.mem_fraction}")
        kv_bytes_per_token(self.dims, self.kv_dtype)

    @property
    def bytes_per_token(self) -> int:
        """KV bytes per token."""
        return kv_bytes_per_token(self.dims, self.kv_dtype)

    def device_tokens_per_rank(self) -> int:
        """Device KV pool per rank in tokens.

        ``floor((hbm * mem_fraction - weights - overhead) GiB / bytes_per_token)``.

        Returns:
            Token capacity.

        Raises:
            ValueError: If nothing is left for the KV pool.
        """
        free_gib = (
            _exact(self.hbm_gib) * _exact(self.mem_fraction)
            - _exact(self.weights_gib)
            - _exact(self.overhead_gib)
        )
        if free_gib <= 0:
            raise ValueError(
                f"no room for KV: {self.hbm_gib} GiB * {self.mem_fraction} - "
                f"{self.weights_gib} GiB weights - {self.overhead_gib} GiB overhead <= 0"
            )
        return math.floor(free_gib * GIB / self.bytes_per_token)

    def sessions_per_rank(self, session_tokens: int) -> int:
        """Whole sessions of ``session_tokens`` that fit in the device pool.

        Args:
            session_tokens: Tokens per session.

        Returns:
            Floor of the session count.

        Raises:
            ValueError: If ``session_tokens`` is not positive.
        """
        if session_tokens <= 0:
            raise ValueError(f"session_tokens must be > 0, got {session_tokens}")
        return self.device_tokens_per_rank() // session_tokens

    def host_pool_tokens(self, hicache_size_gb: float) -> int:
        """HiCache host pool per rank in tokens (decimal GB).

        Args:
            hicache_size_gb: ``--hicache-size`` in GB.

        Returns:
            Token capacity.
        """
        return host_pool_tokens(hicache_size_gb, self.bytes_per_token)

    def unique_tokens_per_rank(self, hicache_size_gb: float, write_through: bool = True) -> int:
        """Distinct tokens per rank across device and host tiers.

        Args:
            hicache_size_gb: ``--hicache-size`` in GB.
            write_through: Whether L2 mirrors L1 (``write_through`` policy).

        Returns:
            Unique token capacity.
        """
        return unique_tokens(
            self.device_tokens_per_rank(),
            self.host_pool_tokens(hicache_size_gb),
            write_through=write_through,
        )


@dataclass(frozen=True)
class TierHits:
    """Outcome of an LRU replay.

    Attributes:
        device: Accesses served by the device tier.
        host: Accesses served by the host tier.
        storage: Accesses served by the storage tier.
        miss: Accesses served by no tier.
    """

    device: int
    host: int
    storage: int
    miss: int

    @property
    def accesses(self) -> int:
        """Total accesses."""
        return self.device + self.host + self.storage + self.miss


class _TokenLru:
    """LRU bounded in tokens; entries larger than the capacity are never stored."""

    def __init__(self, capacity: int) -> None:
        self.capacity = capacity
        self.used = 0
        self.entries: OrderedDict[Hashable, int] = OrderedDict()

    def __contains__(self, key: Hashable) -> bool:
        return key in self.entries

    def touch(self, key: Hashable, size: int) -> None:
        """Insert or refresh ``key`` as most recently used, evicting LRU entries as needed."""
        if key in self.entries:
            self.used -= self.entries.pop(key)
        if self.capacity == 0 or size > self.capacity:
            return
        while self.used + size > self.capacity:
            _, evicted = self.entries.popitem(last=False)
            self.used -= evicted
        self.entries[key] = size
        self.used += size


def replay_lru(
    trace: Sequence[tuple[Hashable, int]],
    *,
    device_tokens: int,
    host_tokens: int,
    storage_tokens: int,
) -> TierHits:
    """Replay ``(session_id, prefix_tokens)`` accesses through inclusive LRU tiers.

    Every access inserts or refreshes the entry in all three tiers; each tier is an independent
    LRU bounded in tokens (an entry larger than a tier is never stored there; capacity 0
    disables the tier). An access counts as a hit in the first tier (device, host, storage)
    that holds the entry before the access, else as a miss.

    Args:
        trace: Accesses in order.
        device_tokens: Device tier capacity in tokens.
        host_tokens: Host tier capacity in tokens.
        storage_tokens: Storage tier capacity in tokens.

    Returns:
        Hit counts per tier.

    Raises:
        ValueError: If a capacity or an entry size is negative.
    """
    caps = (device_tokens, host_tokens, storage_tokens)
    if min(caps) < 0:
        raise ValueError(f"tier capacities must be >= 0, got {caps}")
    tiers = [_TokenLru(c) for c in caps]
    counts = [0, 0, 0, 0]
    for key, size in trace:
        if size < 0:
            raise ValueError(f"entry size must be >= 0, got {size} for {key!r}")
        served = next((i for i, tier in enumerate(tiers) if key in tier), 3)
        counts[served] += 1
        for tier in tiers:
            tier.touch(key, size)
    return TierHits(device=counts[0], host=counts[1], storage=counts[2], miss=counts[3])
