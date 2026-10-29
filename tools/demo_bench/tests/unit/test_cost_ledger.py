# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""RED contract for the Track D cost ledger (WP6c): node-hours x price + ODCR hourly cost.

Module ``demo_bench.application.cost_ledger`` (stdlib only):

* ``compute_cost_ledger(nodeclaims, *, nodepool, prices, now, budget_usd, odcrs=(),
  warn_fraction=0.8) -> CostLedger``.
  - ``nodeclaims``: the decoded ``kubectl get nodeclaims -o json`` document (``{"items": [...]}``);
    anything else -> ``ValueError``.
  - Only items whose ``metadata.labels["karpenter.sh/nodepool"] == nodepool`` count (any other
    NodePool, or a missing label, is ignored). Instance type =
    ``metadata.labels["node.kubernetes.io/instance-type"]``; its hourly price comes from
    ``prices`` (USD/h, Auto Mode fee included by the caller). A counted claim without an instance
    type or without a price -> ``ValueError`` naming the claim / the type (never a silent 0).
  - A claim is billed from ``metadata.creationTimestamp`` to ``metadata.deletionTimestamp`` when
    present (and earlier than ``now``), else to ``now``. Timestamps are RFC 3339 (``...Z``);
    ``now`` is a tz-aware ``datetime``. A claim created after ``now`` costs 0 (clock skew).
  - ``odcrs``: sequence of dicts ``{"id", "hourly_usd", "created_at", "end_at"?}``; cost =
    ``hourly_usd`` x hours from ``created_at`` to ``min(now, end_at)`` (ODCRs bill used or not),
    never negative.
  - Result fields: ``nodeclaim_usd``, ``odcr_usd``, ``total_usd`` (sum), ``node_hours`` (sum of the
    counted claim hours), ``budget_usd``, ``fraction`` (total / budget), ``status``:
    ``"ok"`` (< warn_fraction), ``"alert"`` (>= warn_fraction and < 1.0), ``"exceeded"`` (>= 1.0);
    ``alert`` (bool, True for alert and exceeded); ``claims`` (per-claim name, instance_type,
    hours, usd); ``to_dict()`` (JSON-able with all of the above).
  - ``budget_usd <= 0``, ``warn_fraction`` outside (0, 1], naive ``now`` -> ``ValueError``.
* CLI ``demo-bench cost-ledger --nodeclaims PATH --nodepool NAME --prices PATH --budget-usd N
  [--odcr PATH] [--now ISO] [--out PATH]``: ``--prices`` is a JSON object type -> USD/h, ``--odcr``
  a JSON list as above. Prints the ``to_dict()`` JSON (and writes it to ``--out``). Exit 0 for
  ``ok`` and ``alert`` (an ``alert`` also logs a line containing ``ALERT`` on stderr), exit 1 for
  ``exceeded``, exit 2 for invalid input (bad file, unknown price, bad budget).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

NOW = datetime(2026, 10, 24, 12, 0, 0, tzinfo=UTC)
POOL = "l4-demo"


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _claim(name: str, hours_ago: float, *, pool: str | None = POOL, itype: str | None = "gr6.4xlarge",
           deleted_hours_ago: float | None = None) -> dict[str, Any]:
    labels: dict[str, str] = {}
    if pool is not None:
        labels["karpenter.sh/nodepool"] = pool
    if itype is not None:
        labels["node.kubernetes.io/instance-type"] = itype
    meta: dict[str, Any] = {"name": name, "labels": labels,
                            "creationTimestamp": _iso(NOW - timedelta(hours=hours_ago))}
    if deleted_hours_ago is not None:
        meta["deletionTimestamp"] = _iso(NOW - timedelta(hours=deleted_hours_ago))
    return {"apiVersion": "karpenter.sh/v1", "kind": "NodeClaim", "metadata": meta}


def _doc(*claims: dict[str, Any]) -> dict[str, Any]:
    return {"apiVersion": "v1", "kind": "List", "items": list(claims)}


def _ledger(doc: Any, **kw: Any) -> Any:
    from demo_bench.application.cost_ledger import compute_cost_ledger

    base: dict[str, Any] = {"nodepool": POOL, "prices": {"gr6.4xlarge": 2.0, "g6.8xlarge": 3.0},
                            "now": NOW, "budget_usd": 2000.0}
    return compute_cost_ledger(doc, **(base | kw))


# ------------------------------------------------------------------ pure function
def test_node_hours_times_price() -> None:
    led = _ledger(_doc(_claim("a", 10), _claim("b", 5.5, itype="g6.8xlarge")))
    assert led.node_hours == pytest.approx(15.5)
    assert led.nodeclaim_usd == pytest.approx(10 * 2.0 + 5.5 * 3.0)
    assert led.odcr_usd == 0
    assert led.total_usd == pytest.approx(36.5)
    assert led.fraction == pytest.approx(36.5 / 2000)
    assert led.status == "ok"
    assert led.alert is False
    assert {c["name"] for c in led.claims} == {"a", "b"}


def test_only_the_requested_nodepool_counts() -> None:
    led = _ledger(_doc(_claim("a", 10), _claim("other", 100, pool="gpu-g7e-odcr"),
                       _claim("nolabel", 100, pool=None, itype=None)))
    assert led.node_hours == pytest.approx(10)
    assert [c["name"] for c in led.claims] == ["a"]


def test_deleted_claim_stops_billing_at_deletion() -> None:
    led = _ledger(_doc(_claim("gone", 10, deleted_hours_ago=4)))
    assert led.node_hours == pytest.approx(6)
    assert led.total_usd == pytest.approx(12)


def test_future_creation_costs_zero() -> None:
    led = _ledger(_doc(_claim("skew", -1)))
    assert led.total_usd == 0 and led.node_hours == 0


def test_empty_items_is_zero_cost_ok() -> None:
    led = _ledger(_doc())
    assert led.total_usd == 0 and led.status == "ok" and led.fraction == 0


def test_odcr_bills_hourly_since_creation_used_or_not() -> None:
    odcr = [{"id": "cr-1", "hourly_usd": 6.64, "created_at": _iso(NOW - timedelta(hours=24))}]
    led = _ledger(_doc(), odcrs=odcr)
    assert led.odcr_usd == pytest.approx(6.64 * 24)
    assert led.total_usd == pytest.approx(6.64 * 24)
    assert led.nodeclaim_usd == 0


def test_odcr_stops_at_end_at_and_never_negative() -> None:
    odcr = [
        {"id": "ended", "hourly_usd": 1.0, "created_at": _iso(NOW - timedelta(hours=10)),
         "end_at": _iso(NOW - timedelta(hours=4))},
        {"id": "future", "hourly_usd": 1.0, "created_at": _iso(NOW + timedelta(hours=3))},
    ]
    assert _ledger(_doc(), odcrs=odcr).odcr_usd == pytest.approx(6.0)


def test_total_is_nodeclaims_plus_odcr() -> None:
    odcr = [{"id": "cr-1", "hourly_usd": 1.5, "created_at": _iso(NOW - timedelta(hours=2))}]
    led = _ledger(_doc(_claim("a", 3)), odcrs=odcr)
    assert led.total_usd == pytest.approx(6.0 + 3.0)


@pytest.mark.parametrize(
    ("price", "status", "alert"),
    [(7.9, "ok", False), (8.0, "alert", True), (9.9, "alert", True),
     (10.0, "exceeded", True), (15.0, "exceeded", True)],
)
def test_thresholds_for_a_100_usd_budget(price: float, status: str, alert: bool) -> None:
    # one claim, 10 h: spend = 10 * price against a budget of 100 USD
    led = _ledger(_doc(_claim("a", 10)), prices={"gr6.4xlarge": price}, budget_usd=100.0)
    assert led.status == status
    assert led.alert is alert


def test_exact_80_and_100_percent_boundaries() -> None:
    # 10 h x 20 USD/h = 200 USD
    doc = _doc(_claim("a", 10))
    prices = {"gr6.4xlarge": 20.0}
    assert _ledger(doc, prices=prices, budget_usd=250.0).status == "alert"  # exactly 80 %
    assert _ledger(doc, prices=prices, budget_usd=250.0).alert is True
    assert _ledger(doc, prices=prices, budget_usd=250.01).status == "ok"
    assert _ledger(doc, prices=prices, budget_usd=200.0).status == "exceeded"  # exactly 100 %
    assert _ledger(doc, prices=prices, budget_usd=199.99).status == "exceeded"
    assert _ledger(doc, prices=prices, budget_usd=200.01).status == "alert"


def test_custom_warn_fraction() -> None:
    doc = _doc(_claim("a", 10))
    led = _ledger(doc, prices={"gr6.4xlarge": 20.0}, budget_usd=400.0, warn_fraction=0.5)
    assert led.status == "alert"


def test_missing_price_or_instance_type_is_an_error_not_zero() -> None:
    with pytest.raises(ValueError, match=r"p5\.48xlarge"):
        _ledger(_doc(_claim("a", 1, itype="p5.48xlarge")))
    with pytest.raises(ValueError, match="noinst"):
        _ledger(_doc(_claim("noinst", 1, itype=None)))


def test_an_unpriced_claim_of_another_nodepool_is_ignored() -> None:
    led = _ledger(_doc(_claim("x", 1, pool="other", itype="p5.48xlarge")))
    assert led.total_usd == 0


@pytest.mark.parametrize("doc", [[], {"items": "no"}, {"kind": "List"}, None, "text"])
def test_bad_snapshot_document(doc: Any) -> None:
    with pytest.raises(ValueError):
        _ledger(doc)


@pytest.mark.parametrize("kw", [{"budget_usd": 0}, {"budget_usd": -1}, {"warn_fraction": 0},
                                {"warn_fraction": 1.5}, {"now": datetime(2026, 10, 24, 12, 0)}])
def test_invalid_parameters(kw: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        _ledger(_doc(), **kw)


def test_to_dict_is_json_serialisable_and_complete() -> None:
    d = _ledger(_doc(_claim("a", 2))).to_dict()
    assert json.loads(json.dumps(d)) == d
    for k in ("nodeclaim_usd", "odcr_usd", "total_usd", "node_hours", "budget_usd", "fraction",
              "status", "alert", "claims"):
        assert k in d


@settings(max_examples=60, deadline=None)
@given(seconds=st.lists(st.integers(0, 500 * 3600), max_size=8), extra=st.floats(0, 100),
       price=st.floats(0.01, 50))
def test_property_cost_is_additive_and_monotonic_in_time(
    seconds: list[int], extra: float, price: float
) -> None:
    # RFC 3339 timestamps carry whole seconds, so ages are generated in whole seconds.
    hours = [s / 3600 for s in seconds]
    claims = [_claim(f"n{i}", h) for i, h in enumerate(hours)]
    prices = {"gr6.4xlarge": price}
    led = _ledger(_doc(*claims), prices=prices)
    assert led.total_usd == pytest.approx(sum(hours) * price, rel=1e-6, abs=1e-3)
    later = _ledger(_doc(*claims), prices=prices, now=NOW + timedelta(hours=extra))
    assert later.total_usd >= led.total_usd - 1e-6
    split = sum(_ledger(_doc(c), prices=prices).total_usd for c in claims)
    assert split == pytest.approx(led.total_usd, rel=1e-6, abs=1e-3)


# ------------------------------------------------------------------ CLI
def _cli(argv: list[str]) -> int:
    from demo_bench.cli import main

    try:
        return int(main(argv))
    except SystemExit as exc:
        return int(exc.code or 0)


def _files(tmp_path: Path, doc: Any, prices: Any | None = None, odcr: Any | None = None
           ) -> list[str]:
    nc = tmp_path / "nc.json"
    nc.write_text(json.dumps(doc))
    pr = tmp_path / "prices.json"
    pr.write_text(json.dumps(prices if prices is not None else {"gr6.4xlarge": 20.0}))
    argv = ["cost-ledger", "--nodeclaims", str(nc), "--nodepool", POOL, "--prices", str(pr),
            "--now", _iso(NOW)]
    if odcr is not None:
        op = tmp_path / "odcr.json"
        op.write_text(json.dumps(odcr))
        argv += ["--odcr", str(op)]
    return argv


def test_cli_ok_prints_json_and_writes_out(tmp_path: Path,
                                           capsys: pytest.CaptureFixture[str]) -> None:
    out = tmp_path / "ledger" / "ledger.json"
    argv = [*_files(tmp_path, _doc(_claim("a", 10))), "--budget-usd", "1000", "--out", str(out)]
    assert _cli(argv) == 0
    rec = json.loads(capsys.readouterr().out)
    assert rec["total_usd"] == pytest.approx(200.0)
    assert rec["status"] == "ok"
    assert json.loads(out.read_text()) == rec


def test_cli_alert_at_80_percent_exits_0_with_alert_line(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    argv = [*_files(tmp_path, _doc(_claim("a", 10))), "--budget-usd", "250"]
    assert _cli(argv) == 0
    cap = capsys.readouterr()
    assert json.loads(cap.out)["status"] == "alert"
    assert "ALERT" in cap.err


def test_cli_exits_1_at_100_percent(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    argv = [*_files(tmp_path, _doc(_claim("a", 10))), "--budget-usd", "200"]
    assert _cli(argv) == 1
    assert json.loads(capsys.readouterr().out)["status"] == "exceeded"


def test_cli_exceeded_alert_tells_the_operator_what_to_do(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Sub-cycle F (4): the exceeded alert is addressed to the operator, not to a person of the original session."""
    argv = [*_files(tmp_path, _doc(_claim("a", 10))), "--budget-usd", "200"]
    assert _cli(argv) == 1
    messages = [r.getMessage() for r in caplog.records if r.levelname == "ERROR"]
    assert any("stop the experiment and ask the operator" in m for m in messages), messages
    assert not any("speak" + "er" in m.lower() for m in messages)


def test_cli_odcr_counts_toward_the_budget(tmp_path: Path,
                                           capsys: pytest.CaptureFixture[str]) -> None:
    odcr = [{"id": "cr-1", "hourly_usd": 6.64, "created_at": _iso(NOW - timedelta(hours=100))}]
    argv = [*_files(tmp_path, _doc(), odcr=odcr), "--budget-usd", "664"]
    assert _cli(argv) == 1
    assert json.loads(capsys.readouterr().out)["odcr_usd"] == pytest.approx(664.0)


@pytest.mark.parametrize("budget", ["0", "-5", "abc"])
def test_cli_invalid_budget_exits_2(tmp_path: Path, budget: str) -> None:
    ok = [*_files(tmp_path, _doc(_claim("a", 1))), "--budget-usd", "100"]
    assert _cli(ok) == 0  # control: the same invocation with a valid budget works
    assert _cli([*_files(tmp_path, _doc(_claim("a", 1))), "--budget-usd", budget]) == 2


def test_cli_unknown_price_and_bad_files_exit_2(tmp_path: Path) -> None:
    good = [*_files(tmp_path, _doc(_claim("a", 1))), "--budget-usd", "100"]  # 1h x 20 = 20 -> ok
    assert _cli(good) == 0  # control
    argv = [*_files(tmp_path, _doc(_claim("a", 1, itype="p5.48xlarge"))), "--budget-usd", "10"]
    assert _cli(argv) == 2
    missing = ["cost-ledger", "--nodeclaims", str(tmp_path / "nope.json"), "--nodepool", POOL,
               "--prices", str(tmp_path / "nope2.json"), "--budget-usd", "10"]
    assert _cli(missing) == 2
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    assert _cli(["cost-ledger", "--nodeclaims", str(bad), "--nodepool", POOL, "--prices",
                 str(bad), "--budget-usd", "10"]) == 2
