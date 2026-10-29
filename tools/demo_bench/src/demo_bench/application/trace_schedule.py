# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Fixed-schedule stamping of an unscheduled mooncake trace (aiperf ``--fixed-schedule``).

aiperf 0.13.0 reads ``timestamp`` in milliseconds from each ``mooncake_trace`` row and replays
rows in file order. The Dynamo agentic trace ships without timestamps ("noschedule"), so this
module adds a deterministic arrival process: every output row is the input row's original bytes
with a ``"timestamp"`` member appended before the closing brace.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import random
import tempfile
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal

logger = logging.getLogger(__name__)

Arrival = Literal["poisson", "uniform"]
ARRIVALS: tuple[str, ...] = ("poisson", "uniform")
FORBIDDEN_KEYS = frozenset({"timestamp", "session_id", "delay", "messages"})
MS_PER_S = 1000.0


class ScheduleError(ValueError):
    """The trace or the scheduling options are invalid; no output file is written."""


def arrival_times_ms(n: int, *, rate: float, seed: int, arrival: str = "poisson") -> Sequence[int]:
    """Arrival times in whole milliseconds for ``n`` requests at ``rate`` requests/s.

    ``poisson``: exponential inter-arrival gaps with mean ``1000 / rate`` ms drawn from
    ``random.Random(seed)`` (independent of ``PYTHONHASHSEED``). ``uniform``: constant gap
    ``1000 / rate`` ms, seed-independent. Times are cumulative sums rounded to integer ms
    (aiperf schedules at millisecond accuracy), start at 0, never decrease, and are prefix-stable:
    the first ``k`` values do not depend on ``n``.

    Args:
        n: Number of requests (0 gives an empty schedule).
        rate: Mean arrival rate in requests per second (finite, > 0).
        seed: RNG seed (used by ``poisson`` only).
        arrival: ``poisson`` or ``uniform``.

    Returns:
        ``n`` timestamps in ms.

    Raises:
        ValueError: On a non-positive or non-finite rate, a negative ``n`` or an unknown arrival.
    """
    if isinstance(rate, bool) or not math.isfinite(rate) or rate <= 0:
        raise ValueError(f"rate must be a finite number > 0 requests/s, got {rate!r}")
    if n < 0:
        raise ValueError(f"n must be >= 0, got {n}")
    if arrival not in ARRIVALS:
        raise ValueError(f"unknown arrival {arrival!r}; expected one of {ARRIVALS}")
    mean_gap_ms = MS_PER_S / rate
    if arrival == "uniform":
        return [round(i * mean_gap_ms) for i in range(n)]
    rng = random.Random(seed)
    times: list[int] = []
    elapsed = 0.0
    for i in range(n):
        if i:
            elapsed += rng.expovariate(1.0 / mean_gap_ms)
        times.append(round(elapsed))
    return times


@dataclass(frozen=True)
class ScheduleResult:
    """Record of one stamping run (written to the run metadata).

    Attributes:
        out: Output trace path.
        rows: Rows written.
        input_sha256: sha256 of the input file.
        output_sha256: sha256 of the output file.
        rate: Arrival rate, requests/s.
        seed: RNG seed.
        arrival: Arrival process.
        duration_ms: Timestamp of the last written row.
    """

    out: Path
    rows: int
    input_sha256: str
    output_sha256: str
    rate: float
    seed: int
    arrival: str
    duration_ms: int

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable view.

        Returns:
            The record as a dict (``out`` as a string).
        """
        doc = asdict(self)
        doc["out"] = str(self.out)
        return doc


def _read_rows(src: Path) -> tuple[bytes, list[str]]:
    try:
        data = Path(src).read_bytes()
    except OSError as exc:
        raise ScheduleError(f"cannot read trace {src}: {exc}") from exc
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ScheduleError(f"{src}: not UTF-8: {exc}") from exc
    lines: list[str] = []
    for lineno, raw in enumerate(text.split("\n"), start=1):
        line = raw.rstrip()
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError as exc:
            raise ScheduleError(f"{src}:{lineno}: malformed JSON: {exc}") from exc
        if not isinstance(row, dict) or not row or not line.endswith("}"):
            raise ScheduleError(f"{src}:{lineno}: each row must be a non-empty JSON object")
        clash = sorted(FORBIDDEN_KEYS & set(row))
        if clash:
            raise ScheduleError(
                f"{src}:{lineno}: row already has {clash}; input must be an unscheduled "
                "single-turn trace"
            )
        lines.append(line)
    if not lines:
        raise ScheduleError(f"{src}: trace has no rows")
    return data, lines


def _validate_limits(duration_s: float | None, rows: int | None, available: int) -> None:
    if duration_s is not None and rows is not None:
        raise ScheduleError("pass at most one of duration_s and rows")
    if rows is not None and not 0 < rows <= available:
        raise ScheduleError(f"rows must be in 1..{available}, got {rows}")
    if duration_s is not None and not (math.isfinite(duration_s) and duration_s > 0):
        raise ScheduleError(f"duration_s must be a finite number > 0, got {duration_s}")


def _atomic_write(out: Path, data: bytes) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{out.name}.", dir=out.parent)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        Path(tmp).replace(out)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def schedule_trace(
    src: Path,
    out: Path,
    *,
    rate: float,
    seed: int,
    arrival: str = "poisson",
    duration_s: float | None = None,
    rows: int | None = None,
) -> ScheduleResult:
    """Stamp ``timestamp`` (ms) onto every row of an unscheduled trace.

    Each output line is the input line up to, but excluding, its closing ``}``, followed by
    ``,"timestamp":<ms>}``. Row order is preserved and every row ends with one newline.
    ``rows`` keeps the first ``rows`` rows; ``duration_s`` keeps the rows whose timestamp is
    ``<= duration_s * 1000`` (inclusive, like aiperf's end offset). Both are prefixes of the full
    schedule. All validation happens before anything is written, and the output is replaced
    atomically.

    Args:
        src: Unscheduled JSONL trace (no ``timestamp``/``session_id``/``delay``/``messages``).
        out: Output path.
        rate: Mean arrival rate, requests/s.
        seed: RNG seed for ``poisson``.
        arrival: ``poisson`` or ``uniform``.
        duration_s: Optional schedule length in seconds.
        rows: Optional row limit.

    Returns:
        The stamping record.

    Raises:
        ScheduleError: On invalid input or options (also a ``ValueError``).
    """
    data, lines = _read_rows(src)
    _validate_limits(duration_s, rows, len(lines))
    try:
        times = arrival_times_ms(len(lines), rate=rate, seed=seed, arrival=arrival)
    except ValueError as exc:
        raise ScheduleError(str(exc)) from exc
    if rows is not None:
        keep = rows
    elif duration_s is not None:
        limit_ms = duration_s * MS_PER_S
        keep = sum(1 for t in times if t <= limit_ms)
    else:
        keep = len(lines)
    payload = "".join(
        f'{line[:-1]},"timestamp":{t}}}\n' for line, t in zip(lines[:keep], times, strict=False)
    ).encode("utf-8")
    out = Path(out)
    _atomic_write(out, payload)
    result = ScheduleResult(
        out=out,
        rows=keep,
        input_sha256=hashlib.sha256(data).hexdigest(),
        output_sha256=hashlib.sha256(payload).hexdigest(),
        rate=rate,
        seed=seed,
        arrival=arrival,
        duration_ms=times[keep - 1],
    )
    logger.info("trace scheduled", extra=result.to_dict())
    return result
