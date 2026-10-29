# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Multi-turn conversation trace for aiperf ``mooncake_trace`` replay (Track D, study 4.3).

``N`` sessions x ``T`` turns. Every request starts with the same ``system_tokens`` system prompt
(identical leading ``hash_ids`` blocks for every row of every session). Each session then carries
a private conversation: ``context_tokens`` in total at turn 0 (the system prompt included), plus
``growth_tokens`` per turn. Turn ``k + 1`` is turn ``k`` plus ``growth_tokens`` new tokens, so its
``hash_ids`` extend turn ``k``'s exactly and the whole previous turn is a reusable prefix.
Sessions share only the system prompt.

Rows are single-turn mooncake requests (``timestamp`` ms, ``input_length``, ``output_length``,
``hash_ids``): no ``session_id``, ``delay`` or ``messages``, otherwise aiperf accumulates turns.
The output length is fixed; ``ignore_eos`` is aiperf's job. Arrivals are a fixed schedule: turn
``k`` of a session is sent at ``phase + k * turn_period_ms``, with the session phases spread
evenly over one period (``turn_period_ms * j // sessions``) and assigned to sessions by ``seed``.
"""

from __future__ import annotations

import logging
import random
from typing import Any

logger = logging.getLogger(__name__)


def _validate(
    *,
    sessions: int,
    turns: int,
    system_tokens: int,
    context_tokens: int,
    growth_tokens: int,
    output_tokens: int,
    turn_period_ms: int,
    block_size: int,
) -> None:
    positive = {
        "block_size": block_size,
        "sessions": sessions,
        "turns": turns,
        "system_tokens": system_tokens,
        "context_tokens": context_tokens,
        "growth_tokens": growth_tokens,
        "output_tokens": output_tokens,
        "turn_period_ms": turn_period_ms,
    }
    for name, value in positive.items():
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"{name} must be an integer, got {value!r}")
        if value <= 0:
            raise ValueError(f"{name} must be > 0, got {value}")
    if context_tokens <= system_tokens:
        raise ValueError(
            f"context_tokens={context_tokens} must be larger than system_tokens={system_tokens} "
            "(the context includes the system prompt)"
        )
    for name in ("system_tokens", "context_tokens", "growth_tokens"):
        if positive[name] % block_size:
            raise ValueError(
                f"{name}={positive[name]} is not a multiple of block_size={block_size}"
            )


def generate_multiturn_trace(
    *,
    sessions: int = 40,
    turns: int = 8,
    system_tokens: int = 2048,
    context_tokens: int = 30720,
    growth_tokens: int = 1536,
    output_tokens: int = 128,
    turn_period_ms: int,
    seed: int,
    block_size: int = 512,
) -> list[dict[str, Any]]:
    """Generate the multi-turn trace (defaults: the Track D workload).

    Hash ids: ``0 .. system_tokens / block_size - 1`` are the shared system prompt; session ``s``
    owns the next contiguous range, long enough for its last turn. Turn ``k`` of session ``s`` is
    the system ids followed by the first ``(context_tokens - system_tokens) / block_size +
    k * growth_tokens / block_size`` ids of that range, so every id appears at most once per row
    and private ids never repeat across sessions. The ids do not depend on ``seed``; the seed only
    assigns sessions to the phase slots.

    Args:
        sessions: Number of conversations (``N``).
        turns: Turns per conversation (``T``).
        system_tokens: Shared system prompt, tokens (``S_sys``); a multiple of ``block_size``.
        context_tokens: Conversation length at turn 0, system prompt included (``C0``); a
            multiple of ``block_size`` and larger than ``system_tokens``.
        growth_tokens: Tokens added per turn (``G``); a multiple of ``block_size``.
        output_tokens: ``output_length`` of every request (OSL).
        turn_period_ms: Time between two turns of the same session (``Tp``), ms.
        seed: RNG seed for the session-to-phase assignment.
        block_size: Tokens per ``hash_ids`` block (aiperf ``--isl-block-size``).

    Returns:
        Rows sorted by ``timestamp`` (ties by session), each with exactly ``timestamp``,
        ``input_length``, ``output_length`` and ``hash_ids``.

    Raises:
        ValueError: On a non-positive size or period, a context not larger than the system
            prompt, or a token size that is not a multiple of ``block_size``.
    """
    _validate(
        sessions=sessions,
        turns=turns,
        system_tokens=system_tokens,
        context_tokens=context_tokens,
        growth_tokens=growth_tokens,
        output_tokens=output_tokens,
        turn_period_ms=turn_period_ms,
        block_size=block_size,
    )
    system_blocks = system_tokens // block_size
    first_private_blocks = (context_tokens - system_tokens) // block_size
    growth_blocks = growth_tokens // block_size
    private_per_session = first_private_blocks + (turns - 1) * growth_blocks
    system_ids = list(range(system_blocks))

    slots = list(range(sessions))
    random.Random(seed).shuffle(slots)
    phases = [turn_period_ms * slot // sessions for slot in slots]

    keyed: list[tuple[int, int, dict[str, Any]]] = []
    for session in range(sessions):
        base = system_blocks + session * private_per_session
        for turn in range(turns):
            private = first_private_blocks + turn * growth_blocks
            timestamp = phases[session] + turn * turn_period_ms
            row = {
                "timestamp": timestamp,
                "input_length": context_tokens + turn * growth_tokens,
                "output_length": output_tokens,
                "hash_ids": system_ids + list(range(base, base + private)),
            }
            keyed.append((timestamp, session, row))
    keyed.sort(key=lambda item: (item[0], item[1]))
    rows = [row for _, _, row in keyed]
    logger.info(
        "multiturn trace generated",
        extra={
            "rows": len(rows),
            "sessions": sessions,
            "turns": turns,
            "system_tokens": system_tokens,
            "context_tokens": context_tokens,
            "growth_tokens": growth_tokens,
            "output_tokens": output_tokens,
            "turn_period_ms": turn_period_ms,
            "block_size": block_size,
            "seed": seed,
            "duration_ms": rows[-1]["timestamp"],
        },
    )
    return rows
