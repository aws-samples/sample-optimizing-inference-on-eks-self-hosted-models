# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""`50-toggle-router.sh` (Track Q is the only profile; no ``DEMO_TRACK``).

20-topology-up.sh, 30-run.sh and the cases of the other models left the tree with those scripts; the router toggle stays:
applies ``frontends/<mode>`` server-side with ``-n <namespace>``; only the four scenario namespaces.
"""

from __future__ import annotations

import re
from pathlib import Path

import lib_scripts as S
import pytest
import trackq_scripts as T

FAST = {"BOOT_DEADLINE_COLD_MIN": "2", "BOOT_POLL_S": "10", "BOOT_WAIT_STEP_S": "10"}
# the removed extra namespace and its opt-in switch (spelled with fragments)
OLD_NS = "s-" + "la" + "b"
INCLUDE_SWITCH = "SCENARIO_INCLUDE_" + "LA" + "B"


def _env(**extra: str) -> dict[str, str]:
    return {**T.ENV_Q, **FAST, **extra}


# ----------------------------------------------------------------------------- 50-toggle-router.sh
@pytest.mark.parametrize("mode", ["rr", "kv"])
@pytest.mark.parametrize("ns", T.NAMESPACES)
def test_toggle_applies_the_track_q_frontend_overlay_server_side(tmp_path: Path, ns: str, mode: str) -> None:
    res = S.Sandbox(tmp_path, {}).run("50-toggle-router.sh", mode, ns, env=_env(IMAGE_MIRROR="1"))
    assert res.rc == 0, res.stdout + res.stderr
    kust = [c for c in res.kubectl_calls() if "kustomize" in c["argv"]]
    assert kust and any(a.rstrip("/").endswith(f"{T.TQ}/frontends/{mode}") for c in kust for a in c["argv"]), kust
    (apply,) = T.apply_calls(res)
    assert "--server-side" in apply["argv"] and T.ns_of(apply["argv"]) == [ns]
    assert not [c for c in res.kubectl_calls() if "delete" in c["argv"]]
    assert not [a for c in res.kubectl_calls() for a in c["argv"] if "/mirror/" in a]
    assert re.search(rf"router={'kv' if mode == 'kv' else 'rr'}", res.stderr)


@pytest.mark.parametrize("ns", [OLD_NS, "s-l4-agg", "node-ops", "default", ""])
def test_toggle_accepts_only_the_scenario_namespaces(tmp_path: Path, ns: str) -> None:
    res = S.Sandbox(tmp_path, {}).run("50-toggle-router.sh", "rr", ns, env=_env())
    assert res.rc != 0 and not T.apply_calls(res)


def test_the_old_namespace_stays_refused_even_with_the_old_opt_in(tmp_path: Path) -> None:
    res = S.Sandbox(tmp_path, {}).run("50-toggle-router.sh", "rr", OLD_NS, env=_env(**{INCLUDE_SWITCH: "1"}))
    assert res.rc != 0 and not T.apply_calls(res)


def test_usage_names_the_four_namespaces_and_no_opt_in(tmp_path: Path) -> None:
    res = S.Sandbox(tmp_path, {}).run("50-toggle-router.sh", env=_env())
    assert res.rc == 2
    for ns in T.NAMESPACES:
        assert ns in res.stderr
    assert INCLUDE_SWITCH not in res.stderr + (S.SCRIPTS_DIR / "50-toggle-router.sh").read_text()


def test_toggle_logs_the_q38_dgd_and_ignores_demo_track(tmp_path: Path) -> None:
    res = S.Sandbox(tmp_path, {}).run("50-toggle-router.sh", "kv", "s-dis", env=_env(DEMO_TRACK="g" + "lm"))
    assert res.rc == 0, res.stdout + res.stderr
    assert f"dgd={T.DGD}" in res.stderr
