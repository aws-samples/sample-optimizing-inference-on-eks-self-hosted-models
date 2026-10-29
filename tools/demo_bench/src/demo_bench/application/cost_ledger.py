# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Track D cost ledger: NodeClaim node-hours x price plus ODCR hourly cost (WP6c).

Input is a ``kubectl get nodeclaims -o json`` snapshot. A NodeClaim of the requested NodePool is
billed from ``metadata.creationTimestamp`` to ``metadata.deletionTimestamp`` (when set and in the
past) or to ``now``, at the hourly price of its ``node.kubernetes.io/instance-type`` label. Each
On-Demand Capacity Reservation bills ``hourly_usd`` from ``created_at`` to ``min(now, end_at)``,
used or not. The spend is compared with a budget: ``alert`` from ``warn_fraction`` of it,
``exceeded`` from 100 %.

Prices come from the caller (USD/h, EKS Auto Mode fee included); a missing price or instance
type is an error, never a silent 0. Limits: a snapshot only lists live NodeClaims, so a claim
that is already gone is not counted; and the caller avoids double counting by pricing NodeClaims
that run inside an ODCR at the Auto Mode fee only (the full price over-counts, the safe side).
"""

from __future__ import annotations

import logging
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

logger = logging.getLogger(__name__)

NODEPOOL_LABEL = "karpenter.sh/nodepool"
INSTANCE_TYPE_LABEL = "node.kubernetes.io/instance-type"
SECONDS_PER_HOUR = 3600.0
DEFAULT_WARN_FRACTION = 0.8

Status = Literal["ok", "alert", "exceeded"]


@dataclass(frozen=True)
class CostLedger:
    """Spend of one NodePool plus its ODCRs against a budget.

    Attributes:
        nodeclaim_usd: Sum of the counted NodeClaims' cost.
        odcr_usd: Sum of the ODCRs' cost.
        total_usd: ``nodeclaim_usd + odcr_usd``.
        node_hours: Sum of the counted NodeClaims' billed hours.
        budget_usd: Budget.
        fraction: ``total_usd / budget_usd``.
        status: ``ok`` (< warn fraction), ``alert`` (>= warn fraction, < 1) or ``exceeded``.
        warn_fraction: Fraction of the budget that raises the alert.
        now: End of the billing window.
        claims: Per counted claim: ``name``, ``instance_type``, ``usd_per_hour``, ``hours``,
            ``usd``.
        odcrs: Per ODCR: ``id``, ``hourly_usd``, ``hours``, ``usd``.
    """

    nodeclaim_usd: float
    odcr_usd: float
    total_usd: float
    node_hours: float
    budget_usd: float
    fraction: float
    status: Status
    warn_fraction: float
    now: datetime
    claims: list[dict[str, Any]] = field(default_factory=list)
    odcrs: list[dict[str, Any]] = field(default_factory=list)

    @property
    def alert(self) -> bool:
        """Whether the spend reached the warn fraction (``alert`` or ``exceeded``)."""
        return self.status != "ok"

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable view.

        Returns:
            Every field, ``alert`` included and ``now`` as an ISO 8601 string.
        """
        return {
            "nodeclaim_usd": self.nodeclaim_usd,
            "odcr_usd": self.odcr_usd,
            "total_usd": self.total_usd,
            "node_hours": self.node_hours,
            "budget_usd": self.budget_usd,
            "fraction": self.fraction,
            "status": self.status,
            "alert": self.alert,
            "warn_fraction": self.warn_fraction,
            "now": self.now.isoformat(),
            "claims": [dict(c) for c in self.claims],
            "odcrs": [dict(o) for o in self.odcrs],
        }


def parse_timestamp(value: Any, *, what: str) -> datetime:
    """Parse an RFC 3339 timestamp with an explicit offset (``...Z`` or ``+00:00``).

    Args:
        value: The raw value.
        what: Field description for the error message.

    Returns:
        A timezone-aware datetime.

    Raises:
        ValueError: If the value is not a string, not ISO 8601, or has no offset.
    """
    if not isinstance(value, str):
        raise ValueError(f"{what}: expected an RFC 3339 timestamp string, got {value!r}")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{what}: not an RFC 3339 timestamp: {value!r}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{what}: timestamp {value!r} has no UTC offset")
    return parsed


def _hours(start: datetime, end: datetime) -> float:
    return max(0.0, (end - start).total_seconds() / SECONDS_PER_HOUR)


def _rate(value: Any, *, what: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        raise ValueError(f"{what} must be a finite number, got {value!r}")
    if value < 0:
        raise ValueError(f"{what} must be >= 0, got {value!r}")
    return float(value)


def _validate(now: datetime, budget_usd: float, warn_fraction: float) -> None:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    if isinstance(budget_usd, bool) or not math.isfinite(budget_usd) or budget_usd <= 0:
        raise ValueError(f"budget_usd must be a finite number > 0, got {budget_usd!r}")
    if isinstance(warn_fraction, bool) or not 0 < warn_fraction <= 1:
        raise ValueError(f"warn_fraction must be in (0, 1], got {warn_fraction!r}")


def _items(nodeclaims: Any) -> list[Any]:
    if not isinstance(nodeclaims, dict) or not isinstance(nodeclaims.get("items"), list):
        raise ValueError("nodeclaims must be a kubectl JSON list document with an 'items' list")
    items: list[Any] = nodeclaims["items"]
    return items


def _claim_cost(
    item: Any, index: int, *, nodepool: str, prices: Mapping[str, Any], now: datetime
) -> dict[str, Any] | None:
    """Bill one NodeClaim, or return ``None`` when it belongs to another NodePool.

    Args:
        item: One element of ``items``.
        index: Its position (for error messages).
        nodepool: NodePool whose claims count.
        prices: Instance type -> USD/h.
        now: End of the billing window.

    Returns:
        The per-claim record, or ``None`` for a claim that does not count.

    Raises:
        ValueError: On a malformed item, or a counted claim without instance type or price.
    """
    meta = item.get("metadata") if isinstance(item, dict) else None
    if not isinstance(meta, dict):
        raise ValueError(f"items[{index}]: not a NodeClaim object with metadata")
    labels = meta.get("labels") or {}
    if not isinstance(labels, dict):
        raise ValueError(f"items[{index}]: metadata.labels is not an object")
    if labels.get(NODEPOOL_LABEL) != nodepool:
        return None
    name = str(meta.get("name") or f"items[{index}]")
    instance_type = labels.get(INSTANCE_TYPE_LABEL)
    if not isinstance(instance_type, str) or not instance_type:
        raise ValueError(f"nodeclaim {name!r} has no {INSTANCE_TYPE_LABEL} label")
    if instance_type not in prices:
        raise ValueError(f"no price for instance type {instance_type!r} (nodeclaim {name!r})")
    usd_per_hour = _rate(prices[instance_type], what=f"price of {instance_type!r}")
    start = parse_timestamp(meta.get("creationTimestamp"), what=f"nodeclaim {name!r} creation")
    end = now
    if meta.get("deletionTimestamp") is not None:
        deleted = parse_timestamp(meta["deletionTimestamp"], what=f"nodeclaim {name!r} deletion")
        end = min(deleted, now)
    hours = _hours(start, end)
    return {
        "name": name,
        "instance_type": instance_type,
        "usd_per_hour": usd_per_hour,
        "hours": hours,
        "usd": hours * usd_per_hour,
    }


def _odcr_cost(odcr: Any, index: int, *, now: datetime) -> dict[str, Any]:
    """Bill one capacity reservation from creation to ``min(now, end_at)``.

    Args:
        odcr: ``{"id", "hourly_usd", "created_at", "end_at"?}``.
        index: Its position (for error messages).
        now: End of the billing window.

    Returns:
        The per-ODCR record.

    Raises:
        ValueError: On a malformed entry.
    """
    if not isinstance(odcr, Mapping):
        raise ValueError(f"odcrs[{index}]: expected an object, got {odcr!r}")
    odcr_id = odcr.get("id")
    if not isinstance(odcr_id, str) or not odcr_id:
        raise ValueError(f"odcrs[{index}]: 'id' must be a non-empty string")
    hourly = _rate(odcr.get("hourly_usd"), what=f"odcr {odcr_id!r} hourly_usd")
    start = parse_timestamp(odcr.get("created_at"), what=f"odcr {odcr_id!r} created_at")
    end = now
    if odcr.get("end_at") is not None:
        end = min(parse_timestamp(odcr["end_at"], what=f"odcr {odcr_id!r} end_at"), now)
    hours = _hours(start, end)
    return {"id": odcr_id, "hourly_usd": hourly, "hours": hours, "usd": hours * hourly}


def _status(fraction: float, warn_fraction: float) -> Status:
    if fraction >= 1.0:
        return "exceeded"
    if fraction >= warn_fraction:
        return "alert"
    return "ok"


def compute_cost_ledger(
    nodeclaims: Any,
    *,
    nodepool: str,
    prices: Mapping[str, Any],
    now: datetime,
    budget_usd: float,
    odcrs: Sequence[Mapping[str, Any]] = (),
    warn_fraction: float = DEFAULT_WARN_FRACTION,
) -> CostLedger:
    """Compute the spend of one NodePool and its ODCRs against a budget (pure function).

    Args:
        nodeclaims: Decoded ``kubectl get nodeclaims -o json`` document (``{"items": [...]}``).
        nodepool: Value of the ``karpenter.sh/nodepool`` label whose claims count.
        prices: Instance type -> USD per hour (Auto Mode fee included by the caller).
        now: End of the billing window (timezone-aware).
        budget_usd: Budget, USD (> 0).
        odcrs: Capacity reservations ``{"id", "hourly_usd", "created_at", "end_at"?}``.
        warn_fraction: Fraction of the budget that raises the alert, in (0, 1].

    Returns:
        The ledger.

    Raises:
        ValueError: On a malformed snapshot or ODCR entry, a counted claim without instance type
            or price, a non-positive budget, a warn fraction outside (0, 1] or a naive ``now``.
    """
    _validate(now, budget_usd, warn_fraction)
    claims = [
        record
        for index, item in enumerate(_items(nodeclaims))
        if (record := _claim_cost(item, index, nodepool=nodepool, prices=prices, now=now))
        is not None
    ]
    odcr_records = [_odcr_cost(o, i, now=now) for i, o in enumerate(odcrs)]
    nodeclaim_usd = math.fsum(c["usd"] for c in claims)
    odcr_usd = math.fsum(o["usd"] for o in odcr_records)
    total_usd = nodeclaim_usd + odcr_usd
    fraction = total_usd / budget_usd
    ledger = CostLedger(
        nodeclaim_usd=nodeclaim_usd,
        odcr_usd=odcr_usd,
        total_usd=total_usd,
        node_hours=math.fsum(c["hours"] for c in claims),
        budget_usd=float(budget_usd),
        fraction=fraction,
        status=_status(fraction, warn_fraction),
        warn_fraction=warn_fraction,
        now=now,
        claims=claims,
        odcrs=odcr_records,
    )
    logger.info(
        "cost ledger computed",
        extra={
            "nodepool": nodepool,
            "claims": len(claims),
            "odcrs": len(odcr_records),
            "total_usd": round(total_usd, 2),
            "budget_usd": ledger.budget_usd,
            "fraction": round(fraction, 4),
            "status": ledger.status,
        },
    )
    return ledger
