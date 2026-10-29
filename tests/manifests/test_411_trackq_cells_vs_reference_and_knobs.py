# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Track Q cells reproduce the reference render and differ only by the intended knob.

1. ``fixtures/cells_reference`` is the frozen output of the original ``render_cells.py`` (v2) of the recorded runs, with the
   placeholder config; its sha256 list is checked.
2. every repo cell render equals its reference cell: name, namespace, part-of label and the WHOLE ``spec`` (flags, env, volumes,
   resources, probes, init gate).  Only annotations (the workers-sha256 bookkeeping of the original renderer) may differ.
3. knob isolation, proved structurally and independently of the reference fixture:
   * S0 vs S1 (S2/S4, S3/S5, S6/S7): exactly the Frontend DYN_ROUTER_MODE value;
   * S1 vs S4: exactly the HiCache flags, env, L3 volume/mount, worker memory and the namespace;
   * S0 vs S7: after removing the declared topology plumbing (PD flags, EFA env/resources/privileged/GPU pin wrapper, HiCache on the
     prefill role, component names/types/replicas, namespace) the engine is identical;
   * across all 8 cells every worker container has the same engine core (CORE_ARGS, base env, probes, init gate, image).
"""

from __future__ import annotations

import copy
import hashlib
from typing import Any

import pytest

import lib_manifests as L
import lib_trackq as Q


# ----------------------------------------------------------------------------- 1. the fixture is what ran
def test_reference_fixture_is_intact() -> None:
    lines = (Q.FIXTURES / "SHA256SUMS").read_text().split("\n")
    sums = dict(reversed(line.split("  ")) for line in lines if line)
    assert sorted(sums) == [f"{c}.yaml" for c in Q.CELL_IDS]
    for name, want in sums.items():
        assert hashlib.sha256((Q.FIXTURES / name).read_bytes()).hexdigest() == want, name


# ----------------------------------------------------------------------------- 2. equal to the reference
@pytest.mark.parametrize("cell", Q.CELL_IDS)
def test_cell_reproduces_the_reference_render(cell: str) -> None:
    got, want = Q.normalised(Q.cell_doc(cell)), Q.normalised(Q.reference_doc(cell))
    assert Q.diff_paths(got, want) == [], f"{cell} differs from the reference render at {Q.diff_paths(got, want)[:12]}"


@pytest.mark.parametrize("cell", Q.CELL_IDS)
def test_cell_has_no_unexpected_documents(cell: str) -> None:
    docs = L.render(Q.cell_rel(cell))
    assert [d["kind"] for d in docs] == ["DynamoGraphDeployment"], [d["kind"] for d in docs]


# ----------------------------------------------------------------------------- 3a. router arm only
ROUTER_PATH = ".spec.components[0].podTemplate.spec.containers[0].env[2].value"


@pytest.mark.parametrize("topology,arms", Q.ARMS.items(), ids=list(Q.ARMS))
def test_two_arms_of_a_topology_differ_only_in_the_frontend_router_env(topology: str, arms: tuple[str, str]) -> None:
    rr, kv = (Q.cell_doc(c) for c in arms)
    assert Q.diff_paths(rr, kv) == [ROUTER_PATH], Q.diff_paths(rr, kv)
    fe_rr, fe_kv = (Q.main_of(Q.frontend_of(d)) for d in (rr, kv))
    assert L.env_value(fe_rr, "DYN_ROUTER_MODE") == "round-robin" and L.env_value(fe_kv, "DYN_ROUTER_MODE") == "kv"
    assert Q.env_pairs(fe_rr)[2][0] == "DYN_ROUTER_MODE" == Q.env_pairs(fe_kv)[2][0]


# ----------------------------------------------------------------------------- 3b. HiCache only (S1 vs S4)
def _as_hicache_agg(doc: dict[str, Any]) -> dict[str, Any]:
    """Apply, by hand and from the declared constants, the HiCache knob to an agg DGD."""
    out = copy.deepcopy(doc)
    out["metadata"]["namespace"] = "s-agg-hic"
    out["metadata"]["labels"]["app.kubernetes.io/component"] = "t-agg-hic"
    worker = Q.comp_of(out, "Worker")
    main = Q.main_of(worker)
    main["args"] = main["args"] + Q.hicache_args(Q.HICACHE_SIZE["agg"])
    main["env"] = main["env"] + [{"name": k, "value": v} for k, v in Q.HICACHE_ENV.items()]
    main["volumeMounts"] = main["volumeMounts"] + [{"name": "hicache-l3", "mountPath": "/kvcache/l3", "subPathExpr": "$(POD_NAME)"}]
    main["resources"]["requests"]["memory"], main["resources"]["limits"]["memory"] = Q.MEMORY["hicache-agg"]
    L.pod_spec(worker)["volumes"] = L.pod_spec(worker)["volumes"] + [
        {"name": "hicache-l3", "hostPath": {"path": Q.l3_path("t-agg-hic"), "type": "DirectoryOrCreate"}}
    ]
    return out


def test_s1_vs_s4_differ_only_in_hicache_flags_env_volumes_memory() -> None:
    expected = Q.normalised(_as_hicache_agg(Q.cell_doc("S1")))
    got = Q.normalised(Q.cell_doc("S4"))
    expected["metadata"]["labels"] = got["metadata"]["labels"] = {}  # the component label is bookkeeping
    assert Q.diff_paths(got, expected) == [], Q.diff_paths(got, expected)


def test_s0_vs_s2_differ_only_in_hicache() -> None:
    expected = Q.normalised(_as_hicache_agg(Q.cell_doc("S0")))
    got = Q.normalised(Q.cell_doc("S2"))
    expected["metadata"]["labels"] = got["metadata"]["labels"] = {}
    assert Q.diff_paths(got, expected) == []


def test_hicache_agg_is_200_gb_per_replica_and_l3_is_buffered_posix() -> None:
    for cell in ("S2", "S4"):
        main = Q.main_of(Q.comp_of(Q.cell_doc(cell), "Worker"))
        args = main["args"]
        assert args[args.index("--hicache-size") + 1] == "200"
        assert L.env_value(main, "SGLANG_HICACHE_NIXL_BACKEND_PLUGIN") == "POSIX"
        assert L.env_value(main, "SGLANG_HICACHE_NIXL_USE_DIRECT_IO") == "0", "buffered I/O"


# ----------------------------------------------------------------------------- 3c. PD plumbing (S0 vs S7) and the engine core
def _strip_to_core(comp: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Remove every declared knob from a worker component; return (core component, what was removed)."""
    out = copy.deepcopy(comp)
    removed: dict[str, Any] = {"hicache": None, "pd": None, "efa": False, "pin": False, "l3": False}
    main = Q.main_of(out)
    wrapper, rest = main["args"][0], main["args"][1:]
    assert rest[0] == "dynamo.sglang"
    flags = rest[1:]
    for size in ("200", "400"):
        flags, was = Q.strip_flags(flags, Q.hicache_args(size))
        if was:
            removed["hicache"] = size
    for role in ("prefill", "decode"):
        flags, was = Q.strip_flags(flags, Q.pd_args(role))
        if was:
            removed["pd"] = role
    assert flags == Q.CORE_ARGS, f"{comp['name']}: engine flags are not CORE_ARGS + declared knobs:\n{flags}"
    removed["pin"] = "GPU pin" in wrapper
    assert wrapper.rstrip().endswith('exec python3 -m dynamo.sglang "$@"')
    main["args"] = ["<wrapper>", "dynamo.sglang", *flags]
    names = {e["name"] for e in main["env"]}
    if names & set(Q.HICACHE_ENV):
        assert names >= set(Q.HICACHE_ENV)
        removed["l3"] = True
    removed["efa"] = bool(names & set(Q.EFA_ENV))
    main["env"] = [e for e in main["env"] if e["name"] not in set(Q.HICACHE_ENV) | set(Q.EFA_ENV)]
    main["volumeMounts"] = [m for m in main["volumeMounts"] if m["name"] != "hicache-l3"]
    spec = L.pod_spec(out)
    spec["volumes"] = [v for v in spec["volumes"] if v["name"] != "hicache-l3"]
    for side in ("requests", "limits"):
        main["resources"][side].pop("memory")
        main["resources"][side].pop("vpc.amazonaws.com/efa", None)
    main["securityContext"].pop("privileged", None)
    for key in ("name", "type", "replicas"):
        out.pop(key)
    return out, removed


def test_every_worker_of_every_cell_has_the_same_engine_core() -> None:
    cores = []
    for cell in Q.CELL_IDS:
        for comp in Q.workers_of(Q.cell_doc(cell)):
            core, _ = _strip_to_core(comp)
            cores.append((f"{cell}/{comp['name']}", core))
    base_name, base = cores[0]
    for name, core in cores[1:]:
        assert Q.diff_paths(core, base) == [], f"{name} differs from {base_name} outside the declared knobs: {Q.diff_paths(core, base)}"


def test_declared_knobs_per_cell_are_exactly_these() -> None:
    for cell, (topology, _) in Q.CELLS.items():
        for comp in Q.workers_of(Q.cell_doc(cell)):
            _, removed = _strip_to_core(comp)
            is_pd = topology in Q.DIS
            want_hicache = None
            if topology in Q.HIC and comp["name"] in ("Worker", "prefill"):
                want_hicache = Q.HICACHE_SIZE["prefill" if is_pd else "agg"]
            assert removed["hicache"] == want_hicache, (cell, comp["name"])
            assert removed["l3"] == (want_hicache is not None), (cell, comp["name"])
            assert removed["pd"] == (comp["name"] if is_pd else None), (cell, comp["name"])
            assert removed["efa"] is is_pd and removed["pin"] is is_pd, (cell, comp["name"])


def test_memory_follows_the_declared_kind_in_every_cell() -> None:
    for cell, (topology, _) in Q.CELLS.items():
        dgd = Q.cell_doc(cell)
        for comp in Q.workers_of(dgd):
            res = Q.main_of(comp)["resources"]
            hic = topology in Q.HIC and comp["name"] in ("Worker", "prefill")
            kind = ("hicache-pd" if topology in Q.DIS else "hicache-agg") if hic else ("decode" if comp["name"] == "decode" else "plain")
            assert (res["requests"]["memory"], res["limits"]["memory"]) == Q.MEMORY[kind], (cell, comp["name"])
        fe = Q.main_of(Q.frontend_of(dgd))["resources"]
        assert (fe["requests"]["memory"], fe["limits"]["memory"]) == Q.MEMORY["frontend"], cell


def test_total_host_l2_budget_is_equal_in_every_hicache_cell() -> None:
    """W2 record item 2: agg 8 x 200 GB == PD 4 prefill x 400 GB == 1600 GB; decode carries no HiCache."""
    totals = {}
    for cell, (topology, _) in Q.CELLS.items():
        if topology not in Q.HIC:
            continue
        total = 0
        for comp in Q.workers_of(Q.cell_doc(cell)):
            args = Q.main_of(comp)["args"]
            if "--hicache-size" in args:
                total += int(args[args.index("--hicache-size") + 1]) * int(comp["replicas"])
            else:
                assert comp["name"] == "decode"
        totals[cell] = total
    assert set(totals.values()) == {1600}, totals


def test_s0_vs_s7_differ_only_by_topology_plumbing() -> None:
    s0, s7 = Q.cell_doc("S0"), Q.cell_doc("S7")
    core0, _ = _strip_to_core(Q.comp_of(s0, "Worker"))
    for name in ("prefill", "decode"):
        core, _ = _strip_to_core(Q.comp_of(s7, name))
        assert Q.diff_paths(core, core0) == [], (name, Q.diff_paths(core, core0))
    assert Q.diff_paths(Q.frontend_of(s0), Q.frontend_of(s7)) == [ROUTER_PATH.replace(".spec.components[0]", "")]
    assert s0["spec"]["backendFramework"] == s7["spec"]["backendFramework"]
    assert (s0["metadata"]["namespace"], s7["metadata"]["namespace"]) == ("s-agg", "s-dis-hic")


def test_pd_cells_use_libfabric_over_efa_with_the_gpu_pin() -> None:
    for cell in ("S3", "S5", "S6", "S7"):
        for comp in Q.workers_of(Q.cell_doc(cell)):
            main = Q.main_of(comp)
            assert L.env_value(main, "SGLANG_DISAGGREGATION_NIXL_BACKEND") == "LIBFABRIC"
            assert L.env_value(main, "FI_PROVIDER") == "efa" and L.env_value(main, "EXPECTED_GPUS") == "1"
            assert main["resources"]["limits"]["vpc.amazonaws.com/efa"] == "1"
            assert main["securityContext"]["privileged"] is True and "GPU pin" in main["args"][0]
    for cell in ("S0", "S1", "S2", "S4"):
        main = Q.main_of(Q.comp_of(Q.cell_doc(cell), "Worker"))
        assert "privileged" not in main["securityContext"] and "vpc.amazonaws.com/efa" not in main["resources"]["limits"]


def test_the_l3_hostpath_is_per_namespace_and_only_where_hicache_runs() -> None:
    for cell, (topology, _) in Q.CELLS.items():
        for comp in Q.workers_of(Q.cell_doc(cell)):
            vol = L.volume_by_name(comp, "hicache-l3")
            if topology in Q.HIC and comp["name"] in ("Worker", "prefill"):
                assert vol and vol["hostPath"]["path"] == Q.l3_path(topology), (cell, vol)
            else:
                assert vol is None, (cell, comp["name"])
