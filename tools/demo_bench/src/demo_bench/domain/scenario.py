# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Scenario matrix: 8 scenarios on 4 topologies, factors R (router), O (HiCache), D (PD).

Spec 6.1 and the team decisions: R is the frontend ``DYN_ROUTER_MODE`` (``rr`` vs ``kv``) and is
toggled without redeploying; O and D are topology properties.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Literal

Router = Literal["rr", "kv"]
ROUTERS: Final[tuple[Router, Router]] = ("rr", "kv")


@dataclass(frozen=True)
class Topology:
    """One deployable topology (one DGD in its own namespace).

    Attributes:
        name: Topology id, e.g. ``t-agg-hic``.
        namespace: Kubernetes namespace of its DGD.
        hicache: Factor O level (1 = HiCache on).
        disagg: Factor D level (1 = prefill/decode disaggregated).
    """

    name: str
    namespace: str
    hicache: int
    disagg: int


@dataclass(frozen=True)
class Scenario:
    """One cell of the 2^3 factorial.

    Attributes:
        id: Scenario id ``S0``..``S7``.
        topology: Topology it runs on.
        router: Frontend router arm.
    """

    id: str
    topology: Topology
    router: Router

    @property
    def factors(self) -> tuple[int, int, int]:
        """Factor levels ``(r, o, d)`` in {0, 1}."""
        return (1 if self.router == "kv" else 0, self.topology.hicache, self.topology.disagg)


T_AGG: Final = Topology("t-agg", "s-agg", hicache=0, disagg=0)
T_AGG_HIC: Final = Topology("t-agg-hic", "s-agg-hic", hicache=1, disagg=0)
T_DIS: Final = Topology("t-dis", "s-dis", hicache=0, disagg=1)
T_DIS_HIC: Final = Topology("t-dis-hic", "s-dis-hic", hicache=1, disagg=1)

TOPOLOGIES: Final[tuple[Topology, ...]] = (T_AGG, T_AGG_HIC, T_DIS, T_DIS_HIC)

SCENARIOS: Final[tuple[Scenario, ...]] = (
    Scenario("S0", T_AGG, "rr"),
    Scenario("S1", T_AGG, "kv"),
    Scenario("S2", T_AGG_HIC, "rr"),
    Scenario("S3", T_DIS, "rr"),
    Scenario("S4", T_AGG_HIC, "kv"),
    Scenario("S5", T_DIS, "kv"),
    Scenario("S6", T_DIS_HIC, "rr"),
    Scenario("S7", T_DIS_HIC, "kv"),
)


def topology_by_name(name: str) -> Topology:
    """Look up a topology.

    Args:
        name: Topology id.

    Returns:
        The topology.

    Raises:
        ValueError: If the name is unknown.
    """
    for topo in TOPOLOGIES:
        if topo.name == name:
            return topo
    raise ValueError(f"unknown topology {name!r}; expected one of {[t.name for t in TOPOLOGIES]}")


def scenario_for(topology: Topology, router: Router) -> Scenario:
    """Return the scenario for a topology and router arm.

    Args:
        topology: Topology.
        router: Router arm.

    Returns:
        The scenario.
    """
    return next(s for s in SCENARIOS if s.topology == topology and s.router == router)


def scenario_by_id(scenario_id: str) -> Scenario:
    """Look up a scenario by id.

    Args:
        scenario_id: ``S0``..``S7``.

    Returns:
        The scenario.

    Raises:
        ValueError: If the id is unknown.
    """
    for scenario in SCENARIOS:
        if scenario.id == scenario_id:
            return scenario
    raise ValueError(f"unknown scenario {scenario_id!r}; expected S0..S7")
