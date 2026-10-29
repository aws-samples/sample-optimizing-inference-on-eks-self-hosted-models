# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Session-revisit trace generation for aiperf ``mooncake_trace`` replay (spec 10.1, 10.3).

Every row is an independent single-turn request (``timestamp``, ``input_length``,
``output_length``, ``hash_ids``): no ``session_id``, ``delay`` or ``messages``, otherwise aiperf
accumulates turns. A "session" is only a shared ``hash_ids`` prefix.
"""

from __future__ import annotations

import hashlib
import json
import logging
import random
from collections.abc import Hashable, Iterable, Sequence
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

TRACE_KEYS = ("timestamp", "input_length", "output_length", "hash_ids")


def _require_positive(**values: int) -> None:
    for name, value in values.items():
        if value <= 0:
            raise ValueError(f"{name} must be > 0, got {value}")


def _revisit_order(rng: random.Random, populate: list[int]) -> list[int]:
    order = list(populate)
    rng.shuffle(order)
    if len(populate) >= 2:  # noqa: PLR2004 - a single session has only one order
        while order == populate:
            rng.shuffle(order)
    return order


def generate_session_trace(
    *,
    sessions: int,
    prefix_tokens: int,
    passes: int,
    seed: int,
    block_size: int = 512,
    unique_tokens: int = 512,
    output_tokens: int = 64,
    spacing_ms: int = 1000,
) -> list[dict[str, Any]]:
    """Generate a populate-then-revisit session trace.

    Pass 1 visits every session once (populate). Passes 2..N are independent seeded permutations
    of the sessions, never the populate order (for two or more sessions). Each row carries the
    session's shared prefix blocks followed by globally unique blocks, so only the prefix can hit.

    Args:
        sessions: Number of distinct sessions (shared prefixes).
        prefix_tokens: Shared prefix length in tokens; a multiple of ``block_size``.
        passes: Number of passes including populate.
        seed: RNG seed (same seed, same trace).
        block_size: Tokens per ``hash_ids`` block.
        unique_tokens: Per-request unique suffix in tokens; a positive multiple of
            ``block_size``.
        output_tokens: ``output_length`` of every request.
        spacing_ms: Interval between consecutive request timestamps in ms (first is 0).

    Returns:
        Trace rows in replay order.

    Raises:
        ValueError: On non-positive sizes or lengths that are not multiples of the block size.
    """
    _require_positive(
        sessions=sessions,
        prefix_tokens=prefix_tokens,
        passes=passes,
        block_size=block_size,
        unique_tokens=unique_tokens,
        output_tokens=output_tokens,
    )
    if spacing_ms < 0:
        raise ValueError(f"spacing_ms must be >= 0, got {spacing_ms}")
    for name, tokens in (("prefix_tokens", prefix_tokens), ("unique_tokens", unique_tokens)):
        if tokens % block_size:
            raise ValueError(f"{name}={tokens} is not a multiple of block_size={block_size}")

    prefix_blocks = prefix_tokens // block_size
    unique_blocks = unique_tokens // block_size
    rng = random.Random(seed)
    populate = list(range(sessions))
    orders = [populate] + [_revisit_order(rng, populate) for _ in range(passes - 1)]

    next_unique = sessions * prefix_blocks
    input_length = (prefix_blocks + unique_blocks) * block_size
    rows: list[dict[str, Any]] = []
    for order in orders:
        for session in order:
            prefix = list(range(session * prefix_blocks, (session + 1) * prefix_blocks))
            suffix = list(range(next_unique, next_unique + unique_blocks))
            next_unique += unique_blocks
            rows.append(
                {
                    "timestamp": len(rows) * spacing_ms,
                    "input_length": input_length,
                    "output_length": output_tokens,
                    "hash_ids": prefix + suffix,
                }
            )
    logger.info(
        "trace generated",
        extra={
            "rows": len(rows),
            "sessions": sessions,
            "passes": passes,
            "prefix_tokens": prefix_tokens,
            "seed": seed,
        },
    )
    return rows


def write_trace_jsonl(rows: Iterable[dict[str, Any]], path: Path) -> str:
    """Write trace rows as JSON Lines.

    Args:
        rows: Trace rows.
        path: Output file (parent directories are created).

    Returns:
        The sha256 hex digest of the written file (recorded per run, spec 10.1).
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = "".join(json.dumps(row, separators=(",", ":")) + "\n" for row in rows)
    data = payload.encode("utf-8")
    path.write_bytes(data)
    digest = hashlib.sha256(data).hexdigest()
    logger.info("trace written", extra={"path": str(path), "sha256": digest})
    return digest


def read_trace_jsonl(path: Path) -> list[dict[str, Any]]:
    """Read a JSON Lines trace.

    Args:
        path: Trace file.

    Returns:
        The rows (blank lines skipped).

    Raises:
        ValueError: If a line is not a JSON object.
    """
    rows: list[dict[str, Any]] = []
    for lineno, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        row = json.loads(line)
        if not isinstance(row, dict):
            raise ValueError(f"{path}:{lineno}: trace row is not a JSON object")
        rows.append(row)
    return rows


def lru_accesses(
    rows: Sequence[dict[str, Any]], *, prefix_tokens: int, block_size: int = 512
) -> list[tuple[Hashable, int]]:
    """Map trace rows to ``(session_key, prefix_tokens)`` accesses for the LRU tier model.

    The session key is the tuple of the leading ``prefix_tokens / block_size`` hash ids. Only the
    shared prefix is modelled; the per-request unique suffix can never be reused.

    Args:
        rows: Trace rows (``hash_ids`` required).
        prefix_tokens: Shared prefix length in tokens.
        block_size: Tokens per block.

    Returns:
        Accesses in trace order.

    Raises:
        ValueError: On invalid sizes or rows shorter than the prefix.
    """
    _require_positive(prefix_tokens=prefix_tokens, block_size=block_size)
    if prefix_tokens % block_size:
        raise ValueError(f"prefix_tokens={prefix_tokens} is not a multiple of {block_size}")
    blocks = prefix_tokens // block_size
    accesses: list[tuple[Hashable, int]] = []
    for i, row in enumerate(rows):
        hash_ids = row.get("hash_ids")
        if not isinstance(hash_ids, list) or len(hash_ids) < blocks:
            raise ValueError(f"row {i}: hash_ids shorter than the {blocks}-block prefix")
        accesses.append((tuple(hash_ids[:blocks]), prefix_tokens))
    return accesses
