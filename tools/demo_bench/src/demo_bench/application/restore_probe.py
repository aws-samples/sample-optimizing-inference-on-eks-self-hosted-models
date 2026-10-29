# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Restore-equivalence probe for HiCache tiers (Track D WP6b, study 4.5).

For every probe prompt, strictly one request at a time at temperature 0:

1. ``cold``: the prompt, nothing cached yet;
2. ``gpu``: the same prompt again, a device-tier (L1) prefix hit;
3. every filler prompt once, to push the probe prompt out of GPU memory;
4. ``host``: the same prompt, now restored from the host tier (L2);
5. optionally every storage filler prompt, then ``storage``: restored from L3.

The ``host`` (and ``storage``) answer must match the ``gpu`` one: an identical
``usage.prompt_tokens_details.cached_tokens`` (a truncated match makes the comparison
meaningless), the same number of generated tokens and a top-1 logprob within ``tolerance`` at every
position. Greedy text is not compared: greedy output can change with the prefix-hit length even
without HiCache (SGLang #43055), so a text difference alone never fails a prompt. A ``gpu`` stage
with ``cached_tokens == 0`` fails too: no cache engaged, so the probe would prove nothing.

Noise floor (critic: a cached and a cold path may legitimately differ by more than 1e-3 because
of a different chunking or kernel). :func:`measure_noise_floor` first measures, per prompt and
with the caches flushed before every cold request, cold vs cold and GPU hit vs cold; the probe
then uses the pre-registered ``max(tolerance, k x floor)``. A real corruption stays far above it.

The probe cannot see which tier served a request; the caller sizes the fillers (study: 1.5x L1 for
the host stage, beyond L2 for the storage stage) and confirms the tier on the ``cache_source``
counters.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from demo_bench.domain.ports import HttpClient, HttpResponse

logger = logging.getLogger(__name__)

HTTP_OK = 200
COMPLETIONS_PATH = "/v1/completions"
BODY_EXCERPT_CHARS = 300
COLD, GPU, HOST, STORAGE = "cold", "gpu", "host", "storage"


class ProbeError(RuntimeError):
    """The endpoint answered with an error or with a body the probe cannot use."""


@dataclass(frozen=True)
class NoiseFloor:
    """Legitimate top-1 logprob noise of the engine, measured before the probe.

    Attributes:
        cold_vs_cold: Per prompt, max ``|top1_A - top1_B|`` of two cold requests.
        gpu_vs_cold: Per prompt, max ``|top1_gpu - top1_cold|`` of a GPU hit and a cold request.
        k: Multiplier of the floor.
        base_tolerance: Lower bound of the effective tolerance.
    """

    cold_vs_cold: list[float]
    gpu_vs_cold: list[float]
    k: float = 3.0
    base_tolerance: float = 1e-3

    @property
    def floor(self) -> float:
        """Largest value of both controls."""
        return max([*self.cold_vs_cold, *self.gpu_vs_cold], default=0.0)

    @property
    def effective_tolerance(self) -> float:
        """Pre-registered threshold ``max(base_tolerance, k x floor)``."""
        return max(self.base_tolerance, self.k * self.floor)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable view.

        Returns:
            Both controls, ``floor``, ``k``, ``base_tolerance`` and ``effective_tolerance``.
        """
        return {
            "cold_vs_cold": list(self.cold_vs_cold),
            "gpu_vs_cold": list(self.gpu_vs_cold),
            "floor": self.floor,
            "k": self.k,
            "base_tolerance": self.base_tolerance,
            "effective_tolerance": self.effective_tolerance,
        }


@dataclass(frozen=True)
class StageObservation:
    """What one probe request returned.

    Attributes:
        cached_tokens: ``usage.prompt_tokens_details.cached_tokens`` (0 when absent).
        top1_logprobs: Top-1 logprob of every generated token, in order.
    """

    cached_tokens: int
    top1_logprobs: tuple[float, ...]


@dataclass(frozen=True)
class PromptResult:
    """Verdict for one probe prompt.

    Attributes:
        index: Position of the prompt in the input list.
        ok: Whether every compared stage matched the ``gpu`` stage.
        reasons: Why the prompt failed (empty when ``ok``).
        cached_tokens: ``cached_tokens`` per stage (``cold``, ``gpu``, ``host`` [, ``storage``]).
        max_logprob_diff: Largest ``|top1_stage - top1_gpu|`` per compared stage (``host``
            [, ``storage``]).
    """

    index: int
    ok: bool
    reasons: list[str] = field(default_factory=list)
    cached_tokens: dict[str, int] = field(default_factory=dict)
    max_logprob_diff: dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable view.

        Returns:
            The result as a dict.
        """
        return {
            "index": self.index,
            "ok": self.ok,
            "reasons": list(self.reasons),
            "cached_tokens": dict(self.cached_tokens),
            "max_logprob_diff": dict(self.max_logprob_diff),
        }


@dataclass(frozen=True)
class ProbeReport:
    """Outcome of a probe run (written as evidence per HiCache arm).

    Attributes:
        results: One :class:`PromptResult` per prompt, in input order.
        tolerance: Maximum allowed top-1 logprob difference.
        stages: Stages run per prompt, in order.
        model: Model name sent in every request.
        max_tokens: ``max_tokens`` of every request.
        top_logprobs: ``logprobs`` of every request.
        noise_floor: The measured floor the tolerance was derived from (``None``: plain
            tolerance).
    """

    results: list[PromptResult]
    tolerance: float
    stages: tuple[str, ...]
    model: str
    max_tokens: int
    top_logprobs: int
    noise_floor: NoiseFloor | None = None

    @property
    def total(self) -> int:
        """Number of probe prompts."""
        return len(self.results)

    @property
    def passed(self) -> int:
        """Number of prompts that passed."""
        return sum(1 for r in self.results if r.ok)

    @property
    def ok(self) -> bool:
        """Whether every prompt passed."""
        return self.passed == self.total

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable view.

        Returns:
            ``total``, ``passed``, ``ok``, ``tolerance``, the request settings, ``prompts``
            (one result dict per prompt) and, when measured, ``noise_floor``.
        """
        doc: dict[str, Any] = {
            "total": self.total,
            "passed": self.passed,
            "ok": self.ok,
            "tolerance": self.tolerance,
            "model": self.model,
            "stages": list(self.stages),
            "max_tokens": self.max_tokens,
            "top_logprobs": self.top_logprobs,
            "prompts": [r.to_dict() for r in self.results],
        }
        if self.noise_floor is not None:
            doc["noise_floor"] = self.noise_floor.to_dict()
        return doc


def _validate(
    prompts: Sequence[str],
    filler_prompts: Sequence[str],
    storage_filler_prompts: Sequence[str] | None,
    *,
    max_tokens: int,
    top_logprobs: int,
    tolerance: float,
) -> None:
    if not prompts:
        raise ValueError("prompts must not be empty")
    if not filler_prompts:
        raise ValueError("filler_prompts must not be empty (they push the prompt out of GPU)")
    if storage_filler_prompts is not None and not storage_filler_prompts:
        raise ValueError("storage_filler_prompts must be None (no storage stage) or non-empty")
    if isinstance(tolerance, bool) or not math.isfinite(tolerance) or tolerance < 0:
        raise ValueError(f"tolerance must be a finite number >= 0, got {tolerance!r}")
    for name, value in (("max_tokens", max_tokens), ("top_logprobs", top_logprobs)):
        if isinstance(value, bool) or value <= 0:
            raise ValueError(f"{name} must be > 0, got {value!r}")


def _excerpt(response: HttpResponse) -> str:
    return response.text[:BODY_EXCERPT_CHARS]


class _Endpoint:
    """One OpenAI-compatible completions endpoint with fixed request settings."""

    def __init__(
        self,
        http: HttpClient,
        *,
        base_url: str,
        model: str,
        max_tokens: int,
        top_logprobs: int,
        timeout_s: float,
    ) -> None:
        self._http = http
        self._url = base_url.rstrip("/") + COMPLETIONS_PATH
        self._model = model
        self._max_tokens = max_tokens
        self._top_logprobs = top_logprobs
        self._timeout_s = timeout_s

    def complete(self, prompt: str) -> Any:
        """POST one greedy completion with logprobs.

        Args:
            prompt: Prompt text.

        Returns:
            The decoded JSON body.

        Raises:
            ProbeError: On a non-200 status or a body that is not JSON.
            HttpTransportError: If no response was received (propagated).
        """
        body = {
            "model": self._model,
            "prompt": prompt,
            "max_tokens": self._max_tokens,
            "temperature": 0,
            "logprobs": self._top_logprobs,
        }
        response = self._http.request("POST", self._url, json=body, timeout=self._timeout_s)
        if response.status_code != HTTP_OK:
            raise ProbeError(
                f"POST {self._url} returned HTTP {response.status_code}: {_excerpt(response)}"
            )
        try:
            return response.json()
        except ValueError as exc:
            raise ProbeError(
                f"POST {self._url} returned a body that is not JSON: {_excerpt(response)}"
            ) from exc


def _observe(doc: Any) -> StageObservation:
    """Extract ``cached_tokens`` and the per-token top-1 logprobs from a completions body.

    Args:
        doc: Decoded response body.

    Returns:
        The observation.

    Raises:
        ProbeError: If the body has no usable ``choices[0].logprobs.top_logprobs``.
    """
    try:
        logprobs = doc["choices"][0]["logprobs"]
        top = logprobs["top_logprobs"]
        top1 = tuple(float(max(position.values())) for position in top)
    except (KeyError, IndexError, TypeError, ValueError, AttributeError) as exc:
        excerpt = str(doc)[:BODY_EXCERPT_CHARS]
        raise ProbeError(
            f"response has no usable choices[0].logprobs.top_logprobs: {excerpt}"
        ) from exc
    cached: Any = 0
    usage = doc.get("usage") if isinstance(doc, dict) else None
    details = usage.get("prompt_tokens_details") if isinstance(usage, dict) else None
    if isinstance(details, dict):
        cached = details.get("cached_tokens") or 0
    if isinstance(cached, bool) or not isinstance(cached, int):
        raise ProbeError(f"usage.prompt_tokens_details.cached_tokens is not an int: {cached!r}")
    return StageObservation(cached_tokens=cached, top1_logprobs=top1)


def _compare(
    stage: str, observed: StageObservation, gpu: StageObservation, tolerance: float
) -> tuple[float, list[str]]:
    """Compare one restored stage against the ``gpu`` stage.

    Args:
        stage: Stage name (``host`` or ``storage``).
        observed: The restored stage.
        gpu: The device-tier hit.
        tolerance: Maximum allowed top-1 logprob difference.

    Returns:
        The largest top-1 difference over the common positions and the failure reasons.
    """
    reasons: list[str] = []
    if observed.cached_tokens != gpu.cached_tokens:
        reasons.append(
            f"{stage}: cached_tokens {observed.cached_tokens} != gpu {gpu.cached_tokens} "
            "(truncated or missing restore)"
        )
    if len(observed.top1_logprobs) != len(gpu.top1_logprobs):
        reasons.append(
            f"{stage}: {len(observed.top1_logprobs)} logprob positions != gpu "
            f"{len(gpu.top1_logprobs)}"
        )
    diffs = [abs(a - b) for a, b in zip(observed.top1_logprobs, gpu.top1_logprobs, strict=False)]
    max_diff = max(diffs, default=0.0)
    if not max_diff <= tolerance:  # also catches NaN
        reasons.append(f"{stage}: max top-1 logprob diff {max_diff:.3g} > tolerance {tolerance:g}")
    return max_diff, reasons


def _max_diff(a: StageObservation, b: StageObservation) -> float:
    """Largest ``|top1_a - top1_b|`` over the common positions (0 when there is none)."""
    return max(
        (abs(x - y) for x, y in zip(a.top1_logprobs, b.top1_logprobs, strict=False)), default=0.0
    )


def measure_noise_floor(
    http: HttpClient,
    *,
    base_url: str,
    model: str,
    prompts: Sequence[str],
    flush: Callable[[], None],
    max_tokens: int = 256,
    top_logprobs: int = 5,
    timeout_s: float = 300.0,
    base_tolerance: float = 1e-3,
    k: float = 3.0,
) -> NoiseFloor:
    """Measure the legitimate top-1 logprob noise, one request at a time.

    Per prompt: ``flush``, cold A, ``flush``, cold B (cold vs cold); then ``flush``, cold C and
    the same prompt again, a GPU hit (GPU hit vs cold).

    Args:
        http: HTTP transport (the ``HttpClient`` port).
        base_url: Endpoint base URL; ``/v1/completions`` is appended.
        model: Model name for every request.
        prompts: Prompts to measure on (the probe prompts).
        flush: Empties the engine caches (GPU and host; L3 too when the engine has one).
        max_tokens: ``max_tokens`` of every request.
        top_logprobs: ``logprobs`` of every request.
        timeout_s: Per-request timeout, seconds.
        base_tolerance: Lower bound of the effective tolerance.
        k: Multiplier of the floor.

    Returns:
        The floor.

    Raises:
        ValueError: On empty prompts, ``k <= 0``, ``base_tolerance < 0`` or non-positive
            request sizes (before any request is sent).
        ProbeError: On a non-200 answer, a body without logprobs, a request after a flush that
            still reports ``cached_tokens > 0`` (the flush did not work) or a GPU hit with
            ``cached_tokens == 0``.
        HttpTransportError: If the endpoint is unreachable (propagated from ``http``).
    """
    if not prompts:
        raise ValueError("prompts must not be empty")
    if isinstance(k, bool) or not math.isfinite(k) or k <= 0:
        raise ValueError(f"k must be a finite number > 0, got {k!r}")
    if isinstance(base_tolerance, bool) or not math.isfinite(base_tolerance) or base_tolerance < 0:
        raise ValueError(f"base_tolerance must be a finite number >= 0, got {base_tolerance!r}")
    for name, value in (("max_tokens", max_tokens), ("top_logprobs", top_logprobs)):
        if isinstance(value, bool) or value <= 0:
            raise ValueError(f"{name} must be > 0, got {value!r}")
    endpoint = _Endpoint(
        http,
        base_url=base_url,
        model=model,
        max_tokens=max_tokens,
        top_logprobs=top_logprobs,
        timeout_s=timeout_s,
    )

    def cold(index: int, prompt: str) -> StageObservation:
        flush()
        observed = _observe(endpoint.complete(prompt))
        if observed.cached_tokens > 0:
            raise ProbeError(
                f"prompt {index}: cached_tokens {observed.cached_tokens} right after a flush; "
                "the flush did not empty the caches"
            )
        return observed

    cold_vs_cold: list[float] = []
    gpu_vs_cold: list[float] = []
    for index, prompt in enumerate(prompts):
        cold_vs_cold.append(_max_diff(cold(index, prompt), cold(index, prompt)))
        reference = cold(index, prompt)
        gpu = _observe(endpoint.complete(prompt))
        if gpu.cached_tokens <= 0:
            raise ProbeError(f"prompt {index}: cached_tokens 0 at the GPU hit (no prefix cache)")
        gpu_vs_cold.append(_max_diff(gpu, reference))
    floor = NoiseFloor(
        cold_vs_cold=cold_vs_cold, gpu_vs_cold=gpu_vs_cold, k=k, base_tolerance=base_tolerance
    )
    logger.info("restore probe noise floor", extra=floor.to_dict())
    return floor


def run_restore_probe(
    http: HttpClient,
    *,
    base_url: str,
    model: str,
    prompts: Sequence[str],
    filler_prompts: Sequence[str],
    storage_filler_prompts: Sequence[str] | None = None,
    max_tokens: int = 256,
    top_logprobs: int = 5,
    tolerance: float = 1e-3,
    timeout_s: float = 300.0,
    flush: Callable[[], None] | None = None,
    noise_floor: NoiseFloor | None = None,
) -> ProbeReport:
    """Run the restore-equivalence probe, one request at a time.

    Args:
        http: HTTP transport (the ``HttpClient`` port).
        base_url: Endpoint base URL; ``/v1/completions`` is appended (a trailing slash is fine).
        model: Model name for every request.
        prompts: Probe prompts (study: 16 prompts of ~30K tokens plus a fixed question).
        filler_prompts: Sent once after the ``gpu`` stage of every prompt, to evict it from GPU.
        storage_filler_prompts: Sent once after the ``host`` stage to evict it from host memory;
            ``None`` skips the ``storage`` stage.
        max_tokens: ``max_tokens`` of every request.
        top_logprobs: ``logprobs`` (top-k alternatives per token) of every request.
        tolerance: Maximum allowed ``|top1_restored - top1_gpu|``; with ``noise_floor`` it is a
            lower bound.
        timeout_s: Per-request timeout, seconds.
        flush: Called once before the first request, so the first ``cold`` stage is cold.
        noise_floor: Measured floor; the tolerance becomes ``max(tolerance,
            noise_floor.effective_tolerance)`` and the floor is recorded in the report.

    Returns:
        The report; ``ok`` only when every prompt passed.

    Raises:
        ValueError: On empty prompt/filler lists, an empty storage filler list, a negative
            tolerance or non-positive request sizes (before any request is sent).
        ProbeError: On a non-200 answer or a body without logprobs.
        HttpTransportError: If the endpoint is unreachable (propagated from ``http``).
    """
    _validate(
        prompts,
        filler_prompts,
        storage_filler_prompts,
        max_tokens=max_tokens,
        top_logprobs=top_logprobs,
        tolerance=tolerance,
    )
    if noise_floor is not None:
        tolerance = max(tolerance, noise_floor.effective_tolerance)
    endpoint = _Endpoint(
        http,
        base_url=base_url,
        model=model,
        max_tokens=max_tokens,
        top_logprobs=top_logprobs,
        timeout_s=timeout_s,
    )
    if flush is not None:
        flush()
    restored = (HOST,) if storage_filler_prompts is None else (HOST, STORAGE)
    fillers = {HOST: list(filler_prompts), STORAGE: list(storage_filler_prompts or ())}
    results: list[PromptResult] = []
    for index, prompt in enumerate(prompts):
        cold = _observe(endpoint.complete(prompt))
        gpu = _observe(endpoint.complete(prompt))
        cached = {COLD: cold.cached_tokens, GPU: gpu.cached_tokens}
        if cold.cached_tokens > 0:
            logger.warning(
                "cold request already hit the prefix cache (flush the caches before the probe)",
                extra={"index": index, "cached_tokens": cold.cached_tokens},
            )
        reasons: list[str] = []
        if gpu.cached_tokens <= 0:
            reasons.append("gpu: cached_tokens 0 at the GPU hit (no prefix cache engaged)")
        diffs: dict[str, float] = {}
        for stage in restored:
            for filler in fillers[stage]:
                endpoint.complete(filler)
            observed = _observe(endpoint.complete(prompt))
            cached[stage] = observed.cached_tokens
            diffs[stage], stage_reasons = _compare(stage, observed, gpu, tolerance)
            reasons += stage_reasons
        result = PromptResult(
            index=index,
            ok=not reasons,
            reasons=reasons,
            cached_tokens=cached,
            max_logprob_diff=diffs,
        )
        results.append(result)
        log = logger.info if result.ok else logger.warning
        log("restore probe prompt", extra=result.to_dict())
    report = ProbeReport(
        results=results,
        tolerance=tolerance,
        stages=(COLD, GPU, *restored),
        model=model,
        max_tokens=max_tokens,
        top_logprobs=top_logprobs,
        noise_floor=noise_floor,
    )
    logger.info(
        "restore probe done",
        extra={"total": report.total, "passed": report.passed, "ok": report.ok},
    )
    return report
