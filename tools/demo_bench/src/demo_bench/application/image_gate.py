# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Pre-registered image gate G-IMG (Flash plan 2.3 and 5.1, critic: explicit outcomes).

Two restore-probe reports, run with the stages cold, gpu, host and storage: the derived image
(the candidate under test) and the dev.1 control. A report passes the restore gate iff its ``ok`` is
true, i.e. 16/16 prompts in both the host and the storage stage. The derived image is chosen for
every cell iff it passes the restore gate AND its accept length and cold prefill (48K) are at
least 0.85x and 0.8x those of dev.1; otherwise dev.1 is chosen. HiCache (O) can be measured only
when the chosen image passed the restore gate.

Outcomes are explicit:

* ``derived_only_pass``: the expected case; the probe told corruption from no corruption.
* ``both_pass``: no corruption to detect on this pair, so the probe was not discriminating.
* ``control_only_pass``: unexpected; dev.1 is chosen and O stays valid with it.
* ``both_fail``: HiCache is out of the matrix (or the probe threshold is wrong: check its noise
  floor before concluding).
"""

from __future__ import annotations

import logging
import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

DERIVED, DEV1 = "derived", "dev1"
REQUIRED_STAGES = ("host", "storage")
MIN_ACCEPT_LENGTH_RATIO = 0.85
MIN_PREFILL_RATIO = 0.8
STATES = {
    (True, True): "both_pass",
    (True, False): "derived_only_pass",
    (False, True): "control_only_pass",
    (False, False): "both_fail",
}


@dataclass(frozen=True)
class ImageDecision:
    """Outcome of the image gate.

    Attributes:
        state: ``derived_only_pass``, ``both_pass``, ``control_only_pass`` or ``both_fail``.
        image: ``derived`` or ``dev1``: the image of every cell.
        o_valid: Whether HiCache may be measured (the chosen image passed the restore gate).
        discriminating: Whether the two images disagree on the restore gate.
        reasons: Human-readable justification, one line per condition.
        derived_ok: Restore gate verdict of the derived image.
        control_ok: Restore gate verdict of dev.1.
        accept_length_ratio: Accept length, derived / dev.1.
        prefill_ratio: Cold prefill throughput at 48K, derived / dev.1.
    """

    state: str
    image: str
    o_valid: bool
    discriminating: bool
    reasons: list[str] = field(default_factory=list)
    derived_ok: bool = False
    control_ok: bool = False
    accept_length_ratio: float = 0.0
    prefill_ratio: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable view.

        Returns:
            Every field plus the thresholds of the rule.
        """
        return {
            "state": self.state,
            "image": self.image,
            "o_valid": self.o_valid,
            "discriminating": self.discriminating,
            "reasons": list(self.reasons),
            "derived_ok": self.derived_ok,
            "control_ok": self.control_ok,
            "accept_length_ratio": self.accept_length_ratio,
            "prefill_ratio": self.prefill_ratio,
            "min_accept_length_ratio": MIN_ACCEPT_LENGTH_RATIO,
            "min_prefill_ratio": MIN_PREFILL_RATIO,
        }


def _restore_ok(name: str, report: Any, required_prompts: int) -> bool:
    """Validate one probe report and return its ``ok``."""
    if not isinstance(report, Mapping):
        raise ValueError(f"{name}: expected a restore-probe report object")
    total, ok, stages = report.get("total"), report.get("ok"), report.get("stages")
    if isinstance(total, bool) or not isinstance(total, int) or total < required_prompts:
        raise ValueError(f"{name}: needs {required_prompts} prompts, report has total={total!r}")
    if not isinstance(ok, bool):
        raise ValueError(f"{name}: 'ok' must be a boolean, got {ok!r}")
    if not isinstance(stages, list) or not all(stage in stages for stage in REQUIRED_STAGES):
        raise ValueError(f"{name}: the report must include the stages {REQUIRED_STAGES}")
    return ok


def _ratio(name: str, value: float) -> float:
    if isinstance(value, bool) or not math.isfinite(value) or value < 0:
        raise ValueError(f"{name} must be a finite number >= 0, got {value!r}")
    return float(value)


def decide_image(
    derived: Mapping[str, Any],
    control: Mapping[str, Any],
    *,
    accept_length_ratio: float,
    prefill_ratio: float,
    required_prompts: int = 16,
) -> ImageDecision:
    """Apply the pre-registered image rule.

    Args:
        derived: ``ProbeReport.to_dict()`` of the derived image.
        control: ``ProbeReport.to_dict()`` of the dev.1 control.
        accept_length_ratio: Speculative accept length, derived / dev.1.
        prefill_ratio: Cold prefill throughput at 48K, derived / dev.1.
        required_prompts: Prompts each report must have.

    Returns:
        The decision.

    Raises:
        ValueError: When a report has fewer than ``required_prompts`` prompts, lacks the host or
            storage stage or has no boolean ``ok``, or a ratio is negative or not finite.
    """
    derived_ok = _restore_ok("derived", derived, required_prompts)
    control_ok = _restore_ok("control", control, required_prompts)
    accept = _ratio("accept_length_ratio", accept_length_ratio)
    prefill = _ratio("prefill_ratio", prefill_ratio)
    state = STATES[(derived_ok, control_ok)]
    reasons = [
        f"restore gate: derived {'pass' if derived_ok else 'fail'}, "
        f"dev1 {'pass' if control_ok else 'fail'} ({state})"
    ]
    if state == "both_pass":
        reasons.append("both images pass: the probe was not discriminating on this pair")
    elif state == "both_fail":
        reasons.append(
            "both images fail the restore gate: HiCache (O) is out of the matrix; check the "
            "noise floor of the probe before concluding"
        )
    accept_ok = accept >= MIN_ACCEPT_LENGTH_RATIO
    prefill_ok = prefill >= MIN_PREFILL_RATIO
    reasons.append(
        f"accept length ratio {accept:g} {'>=' if accept_ok else '<'} {MIN_ACCEPT_LENGTH_RATIO}"
    )
    reasons.append(
        f"cold prefill ratio {prefill:g} {'>=' if prefill_ok else '<'} {MIN_PREFILL_RATIO}"
    )
    image = DERIVED if derived_ok and accept_ok and prefill_ok else DEV1
    o_valid = derived_ok if image == DERIVED else control_ok
    reasons.append(f"image {image}; HiCache {'may' if o_valid else 'may not'} be measured with it")
    decision = ImageDecision(
        state=state,
        image=image,
        o_valid=o_valid,
        discriminating=derived_ok != control_ok,
        reasons=reasons,
        derived_ok=derived_ok,
        control_ok=control_ok,
        accept_length_ratio=accept,
        prefill_ratio=prefill,
    )
    logger.info("image gate", extra=decision.to_dict())
    return decision
