# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Measurement window of one run (spec 10.1: aiperf profiling window, warmup excluded)."""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class RunWindow:
    """Wall-clock window of the profiling phase.

    Attributes:
        start_s: Earliest profiling request start, unix seconds.
        end_s: Latest profiling request end, unix seconds.
    """

    start_s: float
    end_s: float

    def __post_init__(self) -> None:
        """Validate ordering.

        Raises:
            ValueError: If the window ends before it starts.
        """
        if self.end_s < self.start_s:
            raise ValueError(f"window ends before it starts: {self.start_s} > {self.end_s}")

    @property
    def seconds(self) -> int:
        """Window length rounded up to whole seconds (PromQL range selector)."""
        return math.ceil(self.end_s - self.start_s)
