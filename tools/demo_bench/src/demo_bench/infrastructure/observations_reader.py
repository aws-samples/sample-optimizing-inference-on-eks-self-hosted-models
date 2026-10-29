# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""CSV reader for matrix observations (one row per run, one column per KPI)."""

from __future__ import annotations

import csv
from pathlib import Path

from demo_bench.application.matrix import Observation
from demo_bench.domain.scenario import scenario_by_id


def read_observations(path: Path, kpi: str) -> list[Observation]:
    """Read observations for one KPI column.

    The CSV needs a ``round`` column, the ``kpi`` column, and either a ``scenario`` column
    (``S0``..``S7``) or the factor columns ``r``, ``o``, ``d`` (0/1).

    Args:
        path: CSV file with a header row.
        kpi: KPI column name, e.g. ``ttft_p50_ms``.

    Returns:
        The observations.

    Raises:
        ValueError: On missing columns or unparsable values.
    """
    with Path(path).open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        columns = set(reader.fieldnames or [])
        has_factors = {"r", "o", "d"} <= columns
        if (
            "round" not in columns
            or kpi not in columns
            or not ("scenario" in columns or has_factors)
        ):
            raise ValueError(
                f"{path}: need columns 'round', {kpi!r} and 'scenario' (or 'r','o','d'); "
                f"got {sorted(columns)}"
            )
        observations: list[Observation] = []
        for lineno, row in enumerate(reader, start=2):
            raw = (row.get(kpi) or "").strip()
            if not raw:
                raise ValueError(f"{path}:{lineno}: empty {kpi!r}")
            if "scenario" in columns and (row.get("scenario") or "").strip():
                r, o, d = scenario_by_id(row["scenario"].strip()).factors
            elif has_factors:
                r, o, d = int(row["r"]), int(row["o"]), int(row["d"])
            else:
                raise ValueError(f"{path}:{lineno}: no scenario or factor levels")
            observations.append(Observation(r=r, o=o, d=d, round=int(row["round"]), kpi=float(raw)))
    return observations
