# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""W2 agentic trace for aiperf ``mooncake_trace`` replay (Flash plan 4.2).

``sessions`` agentic sessions x ``turns`` turns, plus cold ingestions:

* Every session row starts with the same ``global_prefix_tokens`` (system prompt + tools): its
  leading ``hash_ids`` are identical in every session row. The rest of a session is private.
* Turn 0 (``entry``) has ``entry_tokens`` input tokens, global prefix included; turn ``k`` has
  ``entry_tokens + k * growth_tokens`` and its ``hash_ids`` extend turn ``k - 1``'s exactly.
* Entry timestamps are spread (seeded, uniform) over the populate window
  ``[0, populate_s * 1000)`` ms. Each later turn comes a lognormal idle gap after the previous
  turn of its session: ``sigma = gap_sigma``, mean ``sessions / rate_per_s`` seconds, so the
  aggregate continuation rate is ``rate_per_s`` turns/s. Gaps are at least 1 ms.
* Cold ingestions: ``round(cold_fraction / (1 - cold_fraction) * sessions * turns)`` extra
  rows, so they are ``cold_fraction`` of all rows. Their ``hash_ids`` are never seen in any
  other row (not even the global prefix); their length is a seeded multiple of ``block_size``
  in ``[cold_min_tokens, cold_max_tokens]``; their timestamps are uniform over the session span.
* Output length: a fixed ``osl``, or sampled (seeded) from an empirical list, clipped at
  ``osl_cap``.

Rows are single-turn mooncake requests (``timestamp`` ms, ``input_length``, ``output_length``,
``hash_ids``), sorted by timestamp (ties: session rows by session, then cold rows). Open loop:
the timestamps are fixed, so ``aiperf --fixed-schedule`` sends every row at its time whatever the
latency. The sidecar maps every row (``line``) to its ``session``, ``turn`` and ``class``
(``entry``, ``continuation``, ``cold_ingestion``) and repeats ``input_length``.

Hash-id layout (independent of the seed): ``0 .. G-1`` is the global prefix; session ``s`` owns
the next contiguous range, long enough for its last turn; cold rows take the ids after the last
session. Each random quantity has its own seeded stream (entries, gaps, cold rows, OSL), so a
change of the OSL pool does not move the arrivals.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import random
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

ENTRY, CONTINUATION, COLD_INGESTION = "entry", "continuation", "cold_ingestion"
MS_PER_S = 1000.0


@dataclass(frozen=True)
class W2Trace:
    """A generated W2 trace.

    Attributes:
        rows: Mooncake rows (``timestamp``, ``input_length``, ``output_length``, ``hash_ids``),
            sorted by timestamp.
        sidecar: One entry per row, same order: ``line``, ``session`` (``None`` for cold),
            ``turn`` (``None`` for cold), ``class`` and ``input_length``.
    """

    rows: list[dict[str, Any]] = field(default_factory=list)
    sidecar: list[dict[str, Any]] = field(default_factory=list)


def encode_jsonl(items: Iterable[dict[str, Any]]) -> bytes:
    """Encode dicts as JSON Lines: compact separators, one newline per item.

    Args:
        items: Rows or sidecar entries.

    Returns:
        The UTF-8 bytes (hash them before writing to refuse an unexpected trace).
    """
    return "".join(json.dumps(item, separators=(",", ":")) + "\n" for item in items).encode()


def sha256_hex(data: bytes) -> str:
    """Return the sha256 hex digest of ``data``.

    Args:
        data: Bytes to hash.

    Returns:
        The hex digest.
    """
    return hashlib.sha256(data).hexdigest()


def _require_int(name: str, value: Any, *, block_size: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer, got {value!r}")
    if value <= 0:
        raise ValueError(f"{name} must be > 0, got {value}")
    if block_size is not None and value % block_size:
        raise ValueError(f"{name}={value} is not a multiple of block_size={block_size}")
    return value


def _require_positive_number(name: str, value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError(f"{name} must be a number, got {value!r}")
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be a finite number > 0, got {value!r}")
    return float(value)


def _osl_pool(osl: int | Sequence[int]) -> list[int]:
    """Return the OSL values as a list (one item for a fixed OSL)."""
    if isinstance(osl, int):
        return [_require_int("osl", osl)]
    if isinstance(osl, str | bytes) or not isinstance(osl, Sequence):
        raise ValueError(f"osl must be an integer or a sequence of integers, got {osl!r}")
    if not osl:
        raise ValueError("osl must not be an empty sequence")
    return [_require_int("osl item", value) for value in osl]


def _validate(
    *,
    sessions: int,
    turns: int,
    global_prefix_tokens: int,
    entry_tokens: int,
    growth_tokens: int,
    rate_per_s: float,
    gap_sigma: float,
    populate_s: float,
    cold_fraction: float,
    cold_min_tokens: int,
    cold_max_tokens: int,
    osl_cap: int,
    max_isl: int,
    block_size: int,
) -> None:
    _require_int("block_size", block_size)
    _require_int("sessions", sessions)
    _require_int("turns", turns)
    _require_int("osl_cap", osl_cap)
    _require_int("max_isl", max_isl)
    for name, value in (
        ("global_prefix_tokens", global_prefix_tokens),
        ("entry_tokens", entry_tokens),
        ("growth_tokens", growth_tokens),
        ("cold_min_tokens", cold_min_tokens),
        ("cold_max_tokens", cold_max_tokens),
    ):
        _require_int(name, value, block_size=block_size)
    _require_positive_number("rate_per_s", rate_per_s)
    _require_positive_number("populate_s", populate_s)
    if isinstance(gap_sigma, bool) or not isinstance(gap_sigma, int | float):
        raise ValueError(f"gap_sigma must be a number, got {gap_sigma!r}")
    if not math.isfinite(gap_sigma) or gap_sigma < 0:
        raise ValueError(f"gap_sigma must be a finite number >= 0, got {gap_sigma!r}")
    if isinstance(cold_fraction, bool) or not isinstance(cold_fraction, int | float):
        raise ValueError(f"cold_fraction must be a number, got {cold_fraction!r}")
    if not 0 <= cold_fraction < 1:
        raise ValueError(f"cold_fraction must be in [0, 1), got {cold_fraction!r}")
    if entry_tokens <= global_prefix_tokens:
        raise ValueError(
            f"entry_tokens={entry_tokens} must be larger than global_prefix_tokens="
            f"{global_prefix_tokens} (the entry context includes the global prefix)"
        )
    if cold_min_tokens > cold_max_tokens:
        raise ValueError(
            f"cold_min_tokens={cold_min_tokens} must be <= cold_max_tokens={cold_max_tokens}"
        )
    last_turn = entry_tokens + (turns - 1) * growth_tokens
    if last_turn > max_isl:
        raise ValueError(f"the last turn has {last_turn} input tokens > max_isl={max_isl}")
    if cold_fraction > 0 and cold_max_tokens > max_isl:
        raise ValueError(f"cold_max_tokens={cold_max_tokens} > max_isl={max_isl}")


def _stream(seed: int, name: str) -> random.Random:
    """Return the seeded RNG of one random quantity (str seeds are hashed with sha512)."""
    return random.Random(f"trace-w2/{seed}/{name}")


def generate_w2_trace(
    *,
    sessions: int,
    turns: int,
    global_prefix_tokens: int,
    entry_tokens: int,
    growth_tokens: int,
    rate_per_s: float,
    gap_sigma: float = 1.2,
    populate_s: float,
    cold_fraction: float,
    cold_min_tokens: int,
    cold_max_tokens: int,
    osl: int | Sequence[int],
    osl_cap: int = 12000,
    max_isl: int = 200000,
    seed: int,
    block_size: int = 512,
) -> W2Trace:
    """Generate the W2 agentic trace and its sidecar.

    Args:
        sessions: Live sessions (``N_s``).
        turns: Turns per session, entry included.
        global_prefix_tokens: Prefix shared by every session row (system + tools), tokens.
        entry_tokens: Turn-0 input, global prefix included, tokens.
        growth_tokens: Tokens added per turn.
        rate_per_s: Aggregate continuation rate, turns/s (sets the mean gap
            ``sessions / rate_per_s``).
        gap_sigma: Sigma of the lognormal idle gap (0 gives constant gaps).
        populate_s: Window over which the entries are spread, seconds.
        cold_fraction: Share of all rows that are cold ingestions, in ``[0, 1)``.
        cold_min_tokens: Smallest cold ingestion, tokens.
        cold_max_tokens: Largest cold ingestion, tokens.
        osl: Fixed output length, or a non-empty list to sample from (empirical OSL).
        osl_cap: Upper clip of every output length.
        max_isl: Largest allowed input length (last turn and largest cold ingestion).
        seed: RNG seed (same seed and parameters, same trace).
        block_size: Tokens per ``hash_ids`` block (aiperf ``--isl-block-size``); every token
            size must be a multiple of it.

    Returns:
        The rows and the sidecar.

    Raises:
        ValueError: On a non-positive size, rate or window, a negative ``gap_sigma``, a
            ``cold_fraction`` outside ``[0, 1)``, ``cold_min_tokens > cold_max_tokens``, a
            token size that is not a multiple of ``block_size``, an entry not larger than the
            global prefix, an input longer than ``max_isl`` or an empty / non-positive ``osl``.
    """
    _validate(
        sessions=sessions,
        turns=turns,
        global_prefix_tokens=global_prefix_tokens,
        entry_tokens=entry_tokens,
        growth_tokens=growth_tokens,
        rate_per_s=rate_per_s,
        gap_sigma=gap_sigma,
        populate_s=populate_s,
        cold_fraction=cold_fraction,
        cold_min_tokens=cold_min_tokens,
        cold_max_tokens=cold_max_tokens,
        osl_cap=osl_cap,
        max_isl=max_isl,
        block_size=block_size,
    )
    pool = [min(value, osl_cap) for value in _osl_pool(osl)]
    entry_rng, gap_rng = _stream(seed, "entry"), _stream(seed, "gap")
    cold_rng, osl_rng = _stream(seed, "cold"), _stream(seed, "osl")

    global_blocks = global_prefix_tokens // block_size
    first_private = (entry_tokens - global_prefix_tokens) // block_size
    growth_blocks = growth_tokens // block_size
    private_per_session = first_private + (turns - 1) * growth_blocks
    global_ids = list(range(global_blocks))
    populate_ms = populate_s * MS_PER_S
    mean_gap_s = sessions / rate_per_s
    mu = math.log(mean_gap_s) - gap_sigma**2 / 2

    # (timestamp, is_cold, session or cold index, turn, row, sidecar)
    keyed: list[tuple[int, int, int, int, dict[str, Any], dict[str, Any]]] = []
    for session in range(sessions):
        base = global_blocks + session * private_per_session
        timestamp = int(entry_rng.random() * populate_ms)
        for turn in range(turns):
            if turn:
                gap_ms = round(gap_rng.lognormvariate(mu, gap_sigma) * MS_PER_S)
                timestamp += max(1, gap_ms)
            length = entry_tokens + turn * growth_tokens
            private = first_private + turn * growth_blocks
            row = {
                "timestamp": timestamp,
                "input_length": length,
                "output_length": 0,
                "hash_ids": global_ids + list(range(base, base + private)),
            }
            side = {
                "line": -1,
                "session": session,
                "turn": turn,
                "class": CONTINUATION if turn else ENTRY,
                "input_length": length,
            }
            keyed.append((timestamp, 0, session, turn, row, side))

    span_ms = max(item[0] for item in keyed)
    cold_rows = round(cold_fraction / (1 - cold_fraction) * sessions * turns)
    next_id = global_blocks + sessions * private_per_session
    lo_blocks, hi_blocks = cold_min_tokens // block_size, cold_max_tokens // block_size
    for index in range(cold_rows):
        blocks = cold_rng.randint(lo_blocks, hi_blocks)
        timestamp = cold_rng.randint(0, span_ms)
        length = blocks * block_size
        row = {
            "timestamp": timestamp,
            "input_length": length,
            "output_length": 0,
            "hash_ids": list(range(next_id, next_id + blocks)),
        }
        next_id += blocks
        side = {
            "line": -1,
            "session": None,
            "turn": None,
            "class": COLD_INGESTION,
            "input_length": length,
        }
        keyed.append((timestamp, 1, index, 0, row, side))

    keyed.sort(key=lambda item: item[:4])
    rows: list[dict[str, Any]] = []
    sidecar: list[dict[str, Any]] = []
    for line, (_, _, _, _, row, side) in enumerate(keyed):
        row["output_length"] = pool[0] if len(pool) == 1 else osl_rng.choice(pool)
        side["line"] = line
        rows.append(row)
        sidecar.append(side)
    logger.info(
        "w2 trace generated",
        extra={
            "rows": len(rows),
            "sessions": sessions,
            "turns": turns,
            "cold_rows": cold_rows,
            "mean_gap_s": mean_gap_s,
            "gap_sigma": gap_sigma,
            "span_ms": rows[-1]["timestamp"],
            "seed": seed,
        },
    )
    return W2Trace(rows=rows, sidecar=sidecar)
