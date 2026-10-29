# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Offline routed-LRU gate for the W2 trace (Flash plan 4.4, critic: per-pod footprint).

The trace (mooncake rows + the W2 sidecar, in list order) is replayed on ``targets`` independent
engines, each with a GPU tier and an L2 (host) tier. The capacities come from the boot log
(``max_total_num_tokens`` per engine) and the ``--hicache-size`` KV share; they are inputs, not
estimates made here.

Cache model, per target:

* A request hits the longest prefix of its ``hash_ids`` resident in GPU or L2 (GPU only for the
  ``hit_gpu_*`` metrics).
* After the request every one of its blocks is in GPU with the recency of that request (blocks
  found in L2 are promoted).
* On overflow the least recently used blocks go first; among blocks of equal recency the deepest
  goes first (radix leaves), so a prefix survives. A block evicted from GPU moves to L2 (victim
  cache, same rule); a block evicted from L2 is dropped.

The GPU contents do not depend on the L2 (every block of a request lands in GPU whatever served
it), so one replay per routing gives both the GPU-only and the GPU + L2 hits.

Routing: ``rr`` sends row ``i`` to ``i % targets``. ``kv`` (sticky) puts a session on the target
with the fewest sessions at its first row (ties: lowest index) and keeps it there; cold rows go
round-robin on their own counter. The ceiling is one infinite cache without routing.

Hit ratios are block weighted over the rows with ``timestamp >= warmup_s * 1000``; earlier rows
are replayed but not counted. ``revisit_same_target_*`` and ``overlapped_turn_fraction`` are trace
properties and use every row.

Live states (the hybrid model's per-session checkpoints): a block is owned by a session when no
other session's row contains it; the live states of a tier are the sessions that own at least one
resident block there. The peak is taken after every request, per target.

L2 modes (``l2_mode``):

* ``exclusive`` (default): the victim tier above; GPU + L2 hold up to ``C_t + L2`` distinct tokens.
* ``inclusive``: SGLang HiCache ``write_through``, where every device node keeps a host copy, so
  GPU u L2 holds ``L2`` distinct tokens per target, not ``C_t + L2``. ``hit_gpu_*`` and
  ``peak_live_states_gpu_*`` come from the GPU tier alone (L2 of 0). ``hit_l2_*``,
  ``interaction_l2`` and ``peak_live_states_host_*`` come from one LRU of the L2 size (same
  eviction rule). An L2 of 0 means no L2: ``hit_l2_* == hit_gpu_*`` and no host live state.
  SGLang refuses a host pool smaller than the device pool; that case is not checked here.

Rule sets (``rules``):

* ``plan`` (default): the plan 4.4 thresholds, including the node-level ``w_over_sum_*`` checks.
* ``d41``: the d41 composed per-target checks, which supersede the node-level ``w_over_sum_*``
  (kept as one informational check). They need ``inclusive``, an L3 size and the engine context
  length. Added metrics: ``hit_l3_*`` (one LRU of ``L2 + L3`` tokens per target),
  ``l3_only_share_*`` (``hit_l3 - hit_l2``), the session-only kv footprints (cold rows excluded,
  unlike ``footprint_tokens_per_target_*``), the per-target tokens populated before ``warmup_s``
  under kv, ``sessions``, ``rows_over_context`` (rows with ``input_length + output_length >=
  context_len``, which SGLang rejects; every row), ``margins`` (distance of the banded checks to
  their bounds) and ``min_margin``.
"""

from __future__ import annotations

import logging
import math
from collections import OrderedDict, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

RR, KV = "rr", "kv"
GPU_TIER, L2_TIER = 0, 1
EXCLUSIVE, INCLUSIVE = "exclusive", "inclusive"
L2_MODES = (EXCLUSIVE, INCLUSIVE)
PLAN_RULES, D41_RULES = "plan", "d41"
RULE_SETS = (PLAN_RULES, D41_RULES)
REVISIT_BAND = 0.05
FOOTPRINT_FIT = 1.0
SESSIONS_PER_ENGINE = 8  # d41: N_s >= 8 x engines
REQUIRED_METRICS = (
    "targets",
    "ceiling_hit",
    "hit_gpu_rr",
    "hit_gpu_kv",
    "hit_l2_kv",
    "interaction_l2",
    "revisit_same_target_rr",
    "uncacheable_prefill_fraction",
    "overlapped_turn_fraction",
    "w_over_sum_gpu",
    "w_over_sum_l2",
    "footprint_over_l2_rr",
    "footprint_over_l2_kv",
)
# name, metric, lo, hi, informational (plan 4.4; the revisit band depends on the targets).
_Rule = tuple[str, str, float | None, float | None, bool]
_CEILING: _Rule = ("ceiling_single_cache", "ceiling_hit", 0.85, None, False)
_RR_GPU: _Rule = ("rr_gpu_only", "hit_gpu_rr", None, 0.40, False)
_KV_GPU: _Rule = ("kv_sticky_gpu_only", "hit_gpu_kv", 0.30, 0.65, False)
_UNCACHEABLE: _Rule = ("uncacheable_prefill", "uncacheable_prefill_fraction", 0.08, None, False)
_OVERLAP: _Rule = ("overlapped_turns", "overlapped_turn_fraction", None, 0.10, False)
_THRESHOLDS: tuple[_Rule, ...] = (
    _CEILING,
    _RR_GPU,
    _KV_GPU,
    ("kv_sticky_plus_l2", "hit_l2_kv", 0.80, None, False),
    ("kv_minus_rr_with_l2", "interaction_l2", 0.15, None, False),
    _UNCACHEABLE,
    _OVERLAP,
    ("w_over_sum_gpu", "w_over_sum_gpu", 1.8, 2.2, False),
    ("w_over_sum_l2", "w_over_sum_l2", None, 0.85, False),
    ("footprint_over_l2_rr", "footprint_over_l2_rr", FOOTPRINT_FIT, None, True),
    ("footprint_over_l2_kv", "footprint_over_l2_kv", None, FOOTPRINT_FIT, True),
)
_STATE_THRESHOLDS = (
    ("live_states_gpu", "live_states_gpu_ratio", None, 0.8),
    ("live_states_host", "live_states_host_ratio", None, 0.8),
)
# d41 composed checks (the W2 gate amendment), in verdict order around the target-dependent rules.
_D41_HIT_RULES: tuple[_Rule, ...] = (
    _CEILING,
    _RR_GPU,
    _KV_GPU,
    ("kv_sticky_plus_l2_inclusive", "hit_l2_kv", 0.80, None, False),
    ("kv_minus_rr_with_l2_inclusive", "interaction_l2", 0.15, None, False),
)
_D41_FOOTPRINT_RULES: tuple[_Rule, ...] = (
    ("rr_footprint_over_gpu", "footprint_min_over_gpu_rr", FOOTPRINT_FIT, None, False),
    ("rr_footprint_over_l2", "footprint_min_over_l2_rr", FOOTPRINT_FIT, None, False),
    ("l3_window_rr", "l3_only_share_rr", 0.03, None, False),
    ("populate_over_gpu", "populated_min_over_gpu_kv", FOOTPRINT_FIT, None, False),
)
_D41_SESSION_RULES: tuple[_Rule, ...] = (
    ("d41_c_t_lt_fkv_sessions", "session_footprint_min_over_gpu_kv", FOOTPRINT_FIT, None, False),
    ("d41_fkv_sessions_le_l2", "session_footprint_max_over_l2_kv", None, FOOTPRINT_FIT, False),
    ("context_fit_rows", "rows_over_context", None, 0.0, False),
    ("w_over_sum_gpu_superseded", "w_over_sum_gpu", 1.8, 2.2, True),
)
# The checks whose distance to their bounds makes ``margins`` / ``min_margin`` (d41 verdict).
_D41_MARGIN_CHECKS = (
    "kv_sticky_gpu_only",
    "kv_sticky_plus_l2_inclusive",
    "kv_minus_rr_with_l2_inclusive",
    "uncacheable_prefill",
    "ceiling_single_cache",
    "l3_window_rr",
    "d41_c_t_lt_fkv_sessions",
    "d41_fkv_sessions_le_l2",
)
D41_REQUIRED_METRICS = (
    "targets",
    "ceiling_hit",
    "hit_gpu_rr",
    "hit_gpu_kv",
    "hit_l2_kv",
    "interaction_l2",
    "revisit_same_target_rr",
    "uncacheable_prefill_fraction",
    "overlapped_turn_fraction",
    "w_over_sum_gpu",
    "footprint_min_over_gpu_rr",
    "footprint_min_over_l2_rr",
    "l3_only_share_rr",
    "populated_min_over_gpu_kv",
    "sessions",
    "session_footprint_min_over_gpu_kv",
    "session_footprint_max_over_l2_kv",
    "rows_over_context",
)


@dataclass(frozen=True)
class Check:
    """One pre-registered threshold applied to one metric.

    Attributes:
        name: Check name.
        metric: Metric the check reads.
        value: Metric value (``None`` when undefined, e.g. a ratio over an L2 of 0).
        lo: Inclusive lower bound (``None``: unbounded).
        hi: Inclusive upper bound (``None``: unbounded).
        passed: Whether ``value`` is defined and within the bounds.
        informational: Reported but not part of the gate verdict.
    """

    name: str
    metric: str
    value: float | None
    lo: float | None
    hi: float | None
    passed: bool
    informational: bool = False

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable view.

        Returns:
            Every field.
        """
        return {
            "name": self.name,
            "metric": self.metric,
            "value": self.value,
            "lo": self.lo,
            "hi": self.hi,
            "passed": self.passed,
            "informational": self.informational,
        }


@dataclass(frozen=True)
class GateReport:
    """Result of the offline gate.

    Attributes:
        metrics: Every metric of the replay (see the module docstring).
        checks: The thresholds applied to ``metrics``.
        params: The replay inputs (capacities, block size, warm-up), recorded as evidence.
    """

    metrics: dict[str, Any]
    checks: list[Check]
    params: dict[str, Any] = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        """Whether every non-informational check passed."""
        return all(c.passed for c in self.checks if not c.informational)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable view.

        Returns:
            ``passed``, ``params``, ``metrics`` and ``checks``.
        """
        return {
            "passed": self.passed,
            "params": dict(self.params),
            "metrics": dict(self.metrics),
            "checks": [c.to_dict() for c in self.checks],
        }


def _check(rule: _Rule, value: Any) -> Check:
    name, metric, lo, hi, informational = rule
    number = None if value is None else float(value)
    passed = (
        number is not None
        and not math.isnan(number)
        and (lo is None or number >= lo)
        and (hi is None or number <= hi)
    )
    return Check(name, metric, number, lo, hi, passed, informational)


def evaluate_thresholds(metrics: Mapping[str, Any], rules: str = PLAN_RULES) -> list[Check]:
    """Apply the plan 4.4 thresholds or the d41 composed checks.

    Args:
        metrics: Gate metrics; ``REQUIRED_METRICS`` (``D41_REQUIRED_METRICS`` for ``d41``) must
            be present, ``live_states_*_ratio`` are optional.
        rules: ``plan`` or ``d41`` (see the module docstring).

    Returns:
        One check per threshold. The rr revisit band is ``1/targets +- 0.05``. Under ``plan`` the
        footprint checks are informational (kv footprint fits the per-pod L2, rr footprint
        exceeds it). Under ``d41`` the session floor is ``8 x targets`` and ``w_over_sum_gpu`` is
        the only informational check.

    Raises:
        ValueError: If ``rules`` is unknown, a required metric is missing or ``targets`` is not a
            positive integer.
    """
    if rules not in RULE_SETS:
        raise ValueError(f"rules must be one of {', '.join(RULE_SETS)}, got {rules!r}")
    required = REQUIRED_METRICS if rules == PLAN_RULES else D41_REQUIRED_METRICS
    missing = [key for key in required if key not in metrics]
    if missing:
        raise ValueError(f"missing gate metrics: {', '.join(missing)}")
    targets = metrics["targets"]
    if isinstance(targets, bool) or not isinstance(targets, int) or targets <= 0:
        raise ValueError(f"targets must be an integer > 0, got {targets!r}")
    expected = 1 / targets
    revisit: _Rule = (
        "rr_revisit_same_target",
        "revisit_same_target_rr",
        expected - REVISIT_BAND,
        expected + REVISIT_BAND,
        False,
    )
    states: list[_Rule] = [(*rule, False) for rule in _STATE_THRESHOLDS if rule[1] in metrics]
    if rules == PLAN_RULES:
        applied: list[_Rule] = [*_THRESHOLDS[:5], revisit, *_THRESHOLDS[5:], *states]
    else:
        floor_lo = float(SESSIONS_PER_ENGINE * targets)
        floor: _Rule = ("sessions_floor", "sessions", floor_lo, None, False)
        applied = [
            *_D41_HIT_RULES,
            revisit,
            _UNCACHEABLE,
            _OVERLAP,
            *states,
            *_D41_FOOTPRINT_RULES,
            floor,
            *_D41_SESSION_RULES,
        ]
    return [_check(rule, metrics[rule[1]]) for rule in applied]


def _margins(checks: Sequence[Check]) -> dict[str, float]:
    """Distance of each d41 margin check to its nearest bound (negative: outside)."""
    out: dict[str, float] = {}
    for check in checks:
        if check.name not in _D41_MARGIN_CHECKS or check.value is None:
            continue
        sides = [check.value - check.lo] if check.lo is not None else []
        sides += [check.hi - check.value] if check.hi is not None else []
        out[check.name] = min(sides)
    return out


class _Engine:
    """One target: a GPU tier and an L2 victim tier, with per-session live-state counts.

    Each tier is an ``OrderedDict`` in eviction order, (recency, -depth) ascending: a request
    re-inserts its blocks deepest first, so the first item is always the next victim.
    """

    def __init__(self, gpu_blocks: int, l2_blocks: int, owner: Mapping[int, int]) -> None:
        self._capacity = (gpu_blocks, l2_blocks)
        self._tiers: tuple[OrderedDict[int, None], OrderedDict[int, None]] = (
            OrderedDict(),
            OrderedDict(),
        )
        self._owned: tuple[dict[int, int], dict[int, int]] = ({}, {})
        self._owner = owner
        self.peak = [0, 0]

    def _add(self, tier: int, block: int) -> None:
        self._tiers[tier][block] = None
        session = self._owner.get(block)
        if session is not None:
            self._owned[tier][session] = self._owned[tier].get(session, 0) + 1

    def _remove(self, tier: int, block: int) -> None:
        del self._tiers[tier][block]
        session = self._owner.get(block)
        if session is not None:
            self._owned[tier][session] -= 1
            if not self._owned[tier][session]:
                del self._owned[tier][session]

    def serve(self, ids: Sequence[int]) -> tuple[int, int]:
        """Replay one request.

        Args:
            ids: The request's ``hash_ids`` (a repeated id is kept once).

        Returns:
            The GPU-only prefix hit and the GPU + L2 prefix hit, in blocks.
        """
        gpu, l2 = self._tiers
        gpu_hit = 0
        while gpu_hit < len(ids) and ids[gpu_hit] in gpu:
            gpu_hit += 1
        hit = gpu_hit
        while hit < len(ids) and (ids[hit] in gpu or ids[hit] in l2):
            hit += 1
        unique = list(dict.fromkeys(ids))
        for block in unique:
            if block in gpu:
                self._remove(GPU_TIER, block)
            elif block in l2:
                self._remove(L2_TIER, block)
        for block in reversed(unique):  # deepest first: evicted first among equal recency
            self._add(GPU_TIER, block)
        while len(gpu) > self._capacity[GPU_TIER]:
            victim = next(iter(gpu))
            self._remove(GPU_TIER, victim)
            if self._capacity[L2_TIER]:
                self._add(L2_TIER, victim)
        while len(l2) > self._capacity[L2_TIER]:
            self._remove(L2_TIER, next(iter(l2)))
        for tier in (GPU_TIER, L2_TIER):
            self.peak[tier] = max(self.peak[tier], len(self._owned[tier]))
        return gpu_hit, hit


@dataclass
class _Replay:
    """Outcome of one routed replay."""

    hit_gpu: int = 0
    hit_l2: int = 0
    footprints: list[int] = field(default_factory=list)
    peak_gpu: list[int] = field(default_factory=list)
    peak_l2: list[int] = field(default_factory=list)


def _route(sessions: Sequence[int | None], targets: int, routing: str) -> list[int]:
    """Return the target of every row."""
    if routing == RR:
        return [i % targets for i in range(len(sessions))]
    placed: dict[int, int] = {}
    load = [0] * targets
    cold = 0
    out: list[int] = []
    for session in sessions:
        if session is None:
            out.append(cold % targets)
            cold += 1
            continue
        if session not in placed:
            target = min(range(targets), key=lambda t: (load[t], t))
            placed[session] = target
            load[target] += 1
        out.append(placed[session])
    return out


def _replay(
    rows: Sequence[Sequence[int]],
    route: Sequence[int],
    counted: Sequence[bool],
    *,
    targets: int,
    gpu_blocks: int,
    l2_blocks: int,
    owner: Mapping[int, int],
) -> _Replay:
    engines = [_Engine(gpu_blocks, l2_blocks, owner) for _ in range(targets)]
    seen: list[set[int]] = [set() for _ in range(targets)]
    out = _Replay()
    for ids, target, count in zip(rows, route, counted, strict=True):
        gpu_hit, hit = engines[target].serve(ids)
        seen[target].update(ids)
        if count:
            out.hit_gpu += gpu_hit
            out.hit_l2 += hit
    out.footprints = [len(s) for s in seen]
    out.peak_gpu = [e.peak[GPU_TIER] for e in engines]
    out.peak_l2 = [e.peak[L2_TIER] for e in engines]
    return out


def _replay_mode(
    rows: Sequence[Sequence[int]],
    route: Sequence[int],
    counted: Sequence[bool],
    *,
    targets: int,
    gpu_blocks: int,
    l2_blocks: int,
    owner: Mapping[int, int],
    l2_mode: str,
) -> _Replay:
    """Replay one routing with the L2 as a victim tier (exclusive) or a write-through copy.

    Inclusive: the GPU tier alone gives the GPU hits and live states; one LRU of ``l2_blocks``
    (GPU u L2, distinct) gives ``hit_l2`` and ``peak_l2``. With no L2 the GPU-only replay already
    has ``hit_l2 == hit_gpu`` and no host live state.
    """
    if l2_mode == EXCLUSIVE:
        return _replay(
            rows,
            route,
            counted,
            targets=targets,
            gpu_blocks=gpu_blocks,
            l2_blocks=l2_blocks,
            owner=owner,
        )
    out = _replay(
        rows, route, counted, targets=targets, gpu_blocks=gpu_blocks, l2_blocks=0, owner=owner
    )
    if l2_blocks:
        host = _replay(
            rows, route, counted, targets=targets, gpu_blocks=l2_blocks, l2_blocks=0, owner=owner
        )
        out.hit_l2, out.peak_l2 = host.hit_gpu, host.peak_gpu
    return out


def _parse(
    rows: Sequence[Mapping[str, Any]], sidecar: Sequence[Mapping[str, Any]]
) -> tuple[list[list[int]], list[float], list[int | None], list[int | None]]:
    if len(rows) != len(sidecar):
        raise ValueError(f"{len(rows)} trace rows but {len(sidecar)} sidecar entries")
    if not rows:
        raise ValueError("the trace has no rows")
    ids: list[list[int]] = []
    stamps: list[float] = []
    sessions: list[int | None] = []
    turns: list[int | None] = []
    for line, (row, side) in enumerate(zip(rows, sidecar, strict=True)):
        if not isinstance(row, Mapping) or not isinstance(side, Mapping):
            raise ValueError(f"line {line}: trace rows and sidecar entries must be objects")
        hash_ids, stamp = row.get("hash_ids"), row.get("timestamp")
        if not isinstance(hash_ids, list) or not all(
            isinstance(h, int) and not isinstance(h, bool) for h in hash_ids
        ):
            raise ValueError(f"line {line}: hash_ids must be a list of integers")
        if isinstance(stamp, bool) or not isinstance(stamp, int | float):
            raise ValueError(f"line {line}: timestamp must be a number, got {stamp!r}")
        if side.get("line", line) != line:
            raise ValueError(f"sidecar entry {line} has line={side.get('line')!r}")
        session, turn = side.get("session"), side.get("turn")
        for name, value in (("session", session), ("turn", turn)):
            if value is not None and (isinstance(value, bool) or not isinstance(value, int)):
                raise ValueError(f"line {line}: sidecar {name} must be an integer or null")
        ids.append(hash_ids)
        stamps.append(float(stamp))
        sessions.append(session)
        turns.append(turn)
    return ids, stamps, sessions, turns


def _validate_modes(
    *, l2_mode: str, rules: str, l3_tokens_per_target: int, context_len: int | None
) -> None:
    """Check the L2 mode, the rule set and the inputs ``d41`` needs."""
    if l2_mode not in L2_MODES:
        raise ValueError(f"l2_mode must be one of {', '.join(L2_MODES)}, got {l2_mode!r}")
    if rules not in RULE_SETS:
        raise ValueError(f"rules must be one of {', '.join(RULE_SETS)}, got {rules!r}")
    if isinstance(l3_tokens_per_target, bool) or not isinstance(l3_tokens_per_target, int):
        raise ValueError(f"l3_tokens_per_target must be an integer, got {l3_tokens_per_target!r}")
    if l3_tokens_per_target < 0:
        raise ValueError(f"l3_tokens_per_target must be >= 0, got {l3_tokens_per_target}")
    if context_len is not None and (
        isinstance(context_len, bool) or not isinstance(context_len, int) or context_len <= 0
    ):
        raise ValueError(f"context_len must be an integer > 0 when given, got {context_len!r}")
    if rules == D41_RULES and (
        l2_mode != INCLUSIVE or not l3_tokens_per_target or context_len is None
    ):
        raise ValueError(
            "rules=d41 needs l2_mode=inclusive, l3_tokens_per_target > 0 and context_len > 0 "
            f"(got l2_mode={l2_mode!r}, l3_tokens_per_target={l3_tokens_per_target}, "
            f"context_len={context_len!r})"
        )


def _validate(
    *,
    targets: int,
    gpu_tokens_per_target: int,
    l2_tokens_per_target: int,
    turn_duration_s: float,
    block_size: int,
    warmup_s: float,
    uncacheable_tokens: int,
    max_mamba_gpu: int | None,
    max_mamba_host: int | None,
) -> None:
    for name, value in (
        ("targets", targets),
        ("gpu_tokens_per_target", gpu_tokens_per_target),
        ("block_size", block_size),
        ("uncacheable_tokens", uncacheable_tokens),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError(f"{name} must be an integer > 0, got {value!r}")
    if l2_tokens_per_target < 0:
        raise ValueError(f"l2_tokens_per_target must be >= 0, got {l2_tokens_per_target}")
    for name, number in (("turn_duration_s", turn_duration_s), ("warmup_s", warmup_s)):
        if not math.isfinite(number) or number < 0:
            raise ValueError(f"{name} must be a finite number >= 0, got {number!r}")
    for name, limit in (("max_mamba_gpu", max_mamba_gpu), ("max_mamba_host", max_mamba_host)):
        if limit is not None and limit <= 0:
            raise ValueError(f"{name} must be > 0 when given, got {limit}")


def _ratio(num: float, den: float) -> float | None:
    return num / den if den else None


def _single_cache(
    ids: Sequence[Sequence[int]], counted: Sequence[bool], uncacheable_blocks: int
) -> tuple[int, int]:
    """Replay one infinite cache: counted prefix-hit blocks and uncacheable counted rows."""
    ceiling = uncacheable = 0
    seen: set[int] = set()
    for row, count in zip(ids, counted, strict=True):
        hit = 0
        while hit < len(row) and row[hit] in seen:
            hit += 1
        if count:
            ceiling += hit
            uncacheable += len(set(row) - seen) >= uncacheable_blocks
        seen.update(row)
    return ceiling, uncacheable


def _turn_metrics(
    sessions: Sequence[int | None],
    turns: Sequence[int | None],
    stamps: Sequence[float],
    routes: Mapping[str, Sequence[int]],
    turn_duration_s: float,
) -> tuple[int, int, dict[str, int]]:
    """Count the later turns, those overlapping their previous turn and the same-target revisits.

    Returns:
        ``(turns >= 1 with a previous turn, overlapped, revisits per routing)``.
    """
    previous: dict[int, tuple[int, float]] = {}
    later_turns = overlapped = 0
    revisits = dict.fromkeys(routes, 0)
    for i, (session, turn) in enumerate(zip(sessions, turns, strict=True)):
        if session is None:
            continue
        if turn is not None and turn >= 1 and session in previous:
            later_turns += 1
            prev_index, prev_stamp = previous[session]
            overlapped += stamps[i] - prev_stamp < turn_duration_s * 1000
            for routing, route in routes.items():
                revisits[routing] += route[i] == route[prev_index]
        previous[session] = (i, stamps[i])
    return later_turns, overlapped, revisits


def _kv_footprints(
    ids: Sequence[Sequence[int]],
    sessions: Sequence[int | None],
    counted: Sequence[bool],
    route: Sequence[int],
    targets: int,
) -> tuple[list[int], list[int]]:
    """Distinct blocks per target under ``route``: of the session rows, and of the warm-up rows.

    Returns:
        ``(session-only footprint, populated before the warm-up)`` per target, in blocks.
    """
    session_blocks: list[set[int]] = [set() for _ in range(targets)]
    populated: list[set[int]] = [set() for _ in range(targets)]
    for row, session, count, target in zip(ids, sessions, counted, route, strict=True):
        if session is not None:
            session_blocks[target].update(row)
        if not count:
            populated[target].update(row)
    return [len(s) for s in session_blocks], [len(s) for s in populated]


def _rows_over_context(rows: Sequence[Mapping[str, Any]], context_len: int) -> int:
    """Count the rows the engine rejects: ``input_length + output_length >= context_len``.

    Raises:
        ValueError: If a row has no integer ``input_length`` / ``output_length``.
    """
    over = 0
    for line, row in enumerate(rows):
        lengths = (row.get("input_length"), row.get("output_length"))
        tokens = [v for v in lengths if isinstance(v, int) and not isinstance(v, bool)]
        if len(tokens) != len(lengths):
            raise ValueError(f"line {line}: input_length and output_length must be integers")
        over += sum(tokens) >= context_len
    return over


def _d41_metrics(
    rows: Sequence[Mapping[str, Any]],
    ids: Sequence[Sequence[int]],
    sessions: Sequence[int | None],
    counted: Sequence[bool],
    route_kv: Sequence[int],
    *,
    targets: int,
    block_size: int,
    gpu_tokens_per_target: int,
    l2_tokens_per_target: int,
    context_len: int,
    foot_rr: Sequence[int],
) -> dict[str, Any]:
    """The per-target d41 metrics that do not come from a cache replay."""
    sessions_kv, populated = _kv_footprints(ids, sessions, counted, route_kv, targets)
    fkv = [n * block_size for n in sessions_kv]
    populated_kv = [n * block_size for n in populated]
    return {
        "sessions": len({s for s in sessions if s is not None}),
        "session_footprint_tokens_per_target_kv": fkv,
        "populated_tokens_per_target_kv": populated_kv,
        "footprint_min_over_gpu_rr": min(foot_rr) / gpu_tokens_per_target,
        "footprint_min_over_l2_rr": _ratio(min(foot_rr), l2_tokens_per_target),
        "populated_min_over_gpu_kv": min(populated_kv) / gpu_tokens_per_target,
        "session_footprint_min_over_gpu_kv": min(fkv) / gpu_tokens_per_target,
        "session_footprint_max_over_l2_kv": _ratio(max(fkv), l2_tokens_per_target),
        "rows_over_context": _rows_over_context(rows, context_len),
    }


def run_gate(
    rows: Sequence[Mapping[str, Any]],
    sidecar: Sequence[Mapping[str, Any]],
    *,
    targets: int,
    gpu_tokens_per_target: int,
    l2_tokens_per_target: int,
    turn_duration_s: float,
    block_size: int = 512,
    warmup_s: float = 0.0,
    uncacheable_tokens: int = 65536,
    max_mamba_gpu: int | None = None,
    max_mamba_host: int | None = None,
    l2_mode: str = EXCLUSIVE,
    rules: str = PLAN_RULES,
    l3_tokens_per_target: int = 0,
    context_len: int | None = None,
) -> GateReport:
    """Replay the W2 trace on routed engines and apply the plan thresholds or the d41 checks.

    Args:
        rows: Mooncake rows (``timestamp`` ms and ``hash_ids`` are used; ``input_length`` and
            ``output_length`` too under ``d41``), in replay order.
        sidecar: W2 sidecar, one entry per row (``session``, ``turn``).
        targets: Router targets (engines).
        gpu_tokens_per_target: GPU KV capacity per engine, tokens (boot log).
        l2_tokens_per_target: L2 KV capacity per engine, tokens (0: no L2).
        turn_duration_s: Expected duration of one turn (overlap metric), seconds.
        block_size: Tokens per ``hash_ids`` block.
        warmup_s: Rows before this time are replayed but not counted in the hit ratios.
        uncacheable_tokens: A counted row with at least this many never-seen tokens is an
            uncacheable prefill.
        max_mamba_gpu: Mamba state slots per engine on GPU (``max_mamba_cache_size``); adds
            ``live_states_gpu_ratio`` and its check.
        max_mamba_host: Mamba states per engine in the host pool; adds
            ``live_states_host_ratio`` and its check.
        l2_mode: ``exclusive`` (victim tier, default) or ``inclusive`` (HiCache write-through).
        rules: ``plan`` (default) or ``d41`` (composed per-target checks).
        l3_tokens_per_target: L3 KV capacity per engine, tokens (``d41`` only; must be > 0 there).
        context_len: Engine context length, tokens (``d41`` only; must be > 0 there).

    Returns:
        The report (metrics, checks, params).

    Raises:
        ValueError: On mismatched or malformed rows/sidecar, no counted row, a non-positive
            size (negative for the L2, the L3, the turn duration and the warm-up), an unknown
            ``l2_mode`` / ``rules``, or ``d41`` without ``inclusive``, an L3 or a context length.
    """
    _validate(
        targets=targets,
        gpu_tokens_per_target=gpu_tokens_per_target,
        l2_tokens_per_target=l2_tokens_per_target,
        turn_duration_s=turn_duration_s,
        block_size=block_size,
        warmup_s=warmup_s,
        uncacheable_tokens=uncacheable_tokens,
        max_mamba_gpu=max_mamba_gpu,
        max_mamba_host=max_mamba_host,
    )
    _validate_modes(
        l2_mode=l2_mode,
        rules=rules,
        l3_tokens_per_target=l3_tokens_per_target,
        context_len=context_len,
    )
    ids, stamps, sessions, turns = _parse(rows, sidecar)
    warmup_ms = warmup_s * 1000
    counted = [stamp >= warmup_ms for stamp in stamps]
    total = sum(len(row) for row, count in zip(ids, counted, strict=True) if count)
    if not total:
        raise ValueError(f"no counted block after warmup_s={warmup_s}")

    members: dict[int, set[int]] = defaultdict(set)
    for row, session in zip(ids, sessions, strict=True):
        if session is not None:
            for block in row:
                members[block].add(session)
    owner = {block: next(iter(s)) for block, s in members.items() if len(s) == 1}
    ceiling, uncacheable = _single_cache(ids, counted, -(-uncacheable_tokens // block_size))

    gpu_blocks = gpu_tokens_per_target // block_size
    l2_blocks = l2_tokens_per_target // block_size
    replays: dict[str, _Replay] = {}
    routes: dict[str, list[int]] = {}
    hit_l3: dict[str, int] = {}
    for routing in (RR, KV):
        routes[routing] = _route(sessions, targets, routing)
        replays[routing] = _replay_mode(
            ids,
            routes[routing],
            counted,
            targets=targets,
            gpu_blocks=gpu_blocks,
            l2_blocks=l2_blocks,
            owner=owner,
            l2_mode=l2_mode,
        )
        if rules == D41_RULES:  # GPU u L2 u L3: one LRU of L2 + L3 distinct tokens per target
            hit_l3[routing] = _replay(
                ids,
                routes[routing],
                counted,
                targets=targets,
                gpu_blocks=(l2_tokens_per_target + l3_tokens_per_target) // block_size,
                l2_blocks=0,
                owner=owner,
            ).hit_gpu

    later_turns, overlapped, revisits = _turn_metrics(
        sessions, turns, stamps, routes, turn_duration_s
    )
    working_set = len(members) * block_size
    rr, kv = replays[RR], replays[KV]
    foot_rr = [n * block_size for n in rr.footprints]
    foot_kv = [n * block_size for n in kv.footprints]
    metrics: dict[str, Any] = {
        "targets": targets,
        "ceiling_hit": ceiling / total,
        "hit_gpu_rr": rr.hit_gpu / total,
        "hit_gpu_kv": kv.hit_gpu / total,
        "hit_l2_rr": rr.hit_l2 / total,
        "hit_l2_kv": kv.hit_l2 / total,
        "interaction_l2": (kv.hit_l2 - rr.hit_l2) / total,
        "revisit_same_target_rr": _ratio(revisits[RR], later_turns),
        "revisit_same_target_kv": _ratio(revisits[KV], later_turns),
        "uncacheable_prefill_fraction": uncacheable / sum(counted),
        "overlapped_turn_fraction": _ratio(overlapped, later_turns),
        "working_set_tokens": working_set,
        "w_over_sum_gpu": working_set / (targets * gpu_tokens_per_target),
        "w_over_sum_l2": _ratio(working_set, targets * l2_tokens_per_target),
        "footprint_tokens_per_target_rr": foot_rr,
        "footprint_tokens_per_target_kv": foot_kv,
        "footprint_over_l2_rr": _ratio(max(foot_rr), l2_tokens_per_target),
        "footprint_over_l2_kv": _ratio(max(foot_kv), l2_tokens_per_target),
        "peak_live_states_gpu_rr": rr.peak_gpu,
        "peak_live_states_gpu_kv": kv.peak_gpu,
        "peak_live_states_host_rr": rr.peak_l2,
        "peak_live_states_host_kv": kv.peak_l2,
    }
    if max_mamba_gpu is not None:
        metrics["live_states_gpu_ratio"] = max(kv.peak_gpu) / max_mamba_gpu
    if max_mamba_host is not None:
        metrics["live_states_host_ratio"] = max(kv.peak_l2) / max_mamba_host
    if rules == D41_RULES and context_len is not None:  # context_len is validated for d41
        metrics["hit_l3_rr"] = hit_l3[RR] / total
        metrics["hit_l3_kv"] = hit_l3[KV] / total
        metrics["l3_only_share_rr"] = (hit_l3[RR] - rr.hit_l2) / total
        metrics["l3_only_share_kv"] = (hit_l3[KV] - kv.hit_l2) / total
        metrics |= _d41_metrics(
            rows,
            ids,
            sessions,
            counted,
            routes[KV],
            targets=targets,
            block_size=block_size,
            gpu_tokens_per_target=gpu_tokens_per_target,
            l2_tokens_per_target=l2_tokens_per_target,
            context_len=context_len,
            foot_rr=foot_rr,
        )
    checks = evaluate_thresholds(metrics, rules)
    if rules == D41_RULES:
        margins = _margins(checks)
        metrics["margins"] = margins
        metrics["min_margin"] = min(margins.values(), default=None)
    params = {
        "targets": targets,
        "gpu_tokens_per_target": gpu_tokens_per_target,
        "l2_tokens_per_target": l2_tokens_per_target,
        "turn_duration_s": turn_duration_s,
        "block_size": block_size,
        "warmup_s": warmup_s,
        "uncacheable_tokens": uncacheable_tokens,
        "max_mamba_gpu": max_mamba_gpu,
        "max_mamba_host": max_mamba_host,
        "l2_mode": l2_mode,
        "rules": rules,
        "l3_tokens_per_target": l3_tokens_per_target,
        "context_len": context_len,
        "rows": len(ids),
        "counted_rows": sum(counted),
    }
    report = GateReport(metrics=metrics, checks=checks, params=params)
    logger.info(
        "w2 gate done",
        extra={
            "passed": report.passed,
            "failed": [c.name for c in report.checks if not c.passed and not c.informational],
            "l2_mode": l2_mode,
            "rules": rules,
        },
    )
    return report
