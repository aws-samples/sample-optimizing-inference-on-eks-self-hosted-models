# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""``scripts/lib/common.sh``: one profile (Qwen3.8-27B-FP8).

Contract:

* the old track variable (``OLD_TRACK_VAR``) is neither read nor validated (any value is ignored), and no other track,
  mirror or model name or path survives in the file (the needles are built from fragments);
* ``PLATFORM_ENV`` defaults to ``$MANIFESTS_DIR/config/platform.env``; an explicit ``PLATFORM_ENV`` wins; keys become ``CFG_<KEY>``;
* ``MODEL_ID`` and ``MODEL_REVISION`` default to the config keys of the same name (an explicit env var wins) with
  NO literal fallback;
* ``SCENARIO_NAMESPACES`` is the fixed list ``s-agg s-agg-hic s-dis s-dis-hic``;
* the topology/scenario/hicache/PD helpers, ``sha256_of``, ``is_positive_number`` and ``dgd_pods_json`` are not defined;
* ``require_kube_target`` off-cluster needs ``KUBE_CONTEXT``; when the config carries ``CLUSTER_NAME`` the context must be that
  cluster, or its EKS ARN (``arn:aws:eks:<region>:<account>:cluster/<name>``). The cluster is NEVER derived from ``MODEL_BUCKET``;
  without ``CLUSTER_NAME`` any explicit context is accepted. In-cluster runs need no context and add no ``--context`` flag.
* kept helpers: ``log die now is_uint kube require_kube_target replace_finished_job node_shell``.
"""

from __future__ import annotations

from pathlib import Path

import lib_scripts as S
import pytest
import q38_scripts as T

MAN = str(T.MANIFESTS)
COMMON = S.SCRIPTS_DIR / "lib" / "common.sh"
# The removed names, spelled with fragments so this file does not carry them.
OLD_TRACK_VAR = "DEMO_" + "TRACK"
INCLUDE_SWITCH = "SCENARIO_INCLUDE_" + "LA" + "B"  # the removed opt-in


def val(tmp: Path, expr: str, env: dict[str, str] | None = None) -> str:
    return T.common_value(tmp, expr, env)


def rc_of(tmp: Path, snippet: str, env: dict[str, str] | None = None) -> S.Result:
    return T.run_common(S.Sandbox(tmp, {}), snippet, env)


def config(tmp: Path, **keys: str) -> dict[str, str]:
    """An explicit PLATFORM_ENV with only ``keys`` (no model keys unless given)."""
    tmp.mkdir(parents=True, exist_ok=True)
    path = tmp / "custom.env"
    path.write_text("".join(f"{k}={v}\n" for k, v in keys.items()))
    return {"PLATFORM_ENV": str(path)}


# ----------------------------------------------------------------------------- no track profile
def test_common_sh_names_no_other_track() -> None:
    text = COMMON.read_text()
    low = text.lower()
    for needle in (OLD_TRACK_VAR, "TRA" + "CKB", "TRACK" + "Q_", "QWEN" + "38_", "WEIGHTS_" + "MARKER", INCLUDE_SWITCH,
                   "IMAGE_" + "MIRROR"):
        assert needle not in text, f"lib/common.sh still names {needle}"
    for needle in ("g" + "lm", "flash", "track" + "-a", "track" + "-b", "track" + "-f", "mirror/", "radix" + "ark"):
        assert needle not in low, f"lib/common.sh still names {needle}"


@pytest.mark.parametrize("value", ["g" + "lm", "qwen38", "flash", "q38", "anything", ""])
def test_the_removed_profile_switch_is_ignored(tmp_path: Path, value: str) -> None:
    res = rc_of(tmp_path, 'printf "%s" "$MODEL_ID"', {OLD_TRACK_VAR: value})
    assert res.rc == 0, res.stdout + res.stderr
    assert res.stdout == T.MODEL


# ----------------------------------------------------------------------------- helpers
@pytest.mark.parametrize(
    "name",
    ["topology_namespace", "scenario_topology", "scenario_router", "hicache_roles", "pd_placement", "is_pd_topology",
     "topology_overlay", "hicache_l3_dir", "sha256_of", "is_positive_number", "dgd_pods_json"],
)
def test_removed_helpers_are_gone(tmp_path: Path, name: str) -> None:
    assert rc_of(tmp_path, f"type -t {name}").rc != 0, f"{name} is still defined"


@pytest.mark.parametrize("name", ["log", "die", "now", "is_uint", "kube", "require_kube_target", "replace_finished_job", "node_shell"])
def test_kept_helpers_are_defined(tmp_path: Path, name: str) -> None:
    assert rc_of(tmp_path, f"type -t {name}").rc == 0, f"{name} is missing"


# ----------------------------------------------------------------------------- identity and config
def test_identity_values(tmp_path: Path) -> None:
    assert val(tmp_path / "a", '"$MODEL_ID"') == T.MODEL
    assert val(tmp_path / "b", '"$MODEL_REVISION"') == T.REVISION


def test_explicit_env_wins(tmp_path: Path) -> None:
    assert val(tmp_path, '"$MODEL_REVISION"', {"MODEL_REVISION": "feedface"}) == "feedface"
    assert val(tmp_path / "a", '"$MODEL_ID"', {"MODEL_ID": "acme/Other-1B"}) == "acme/Other-1B"


def test_model_follows_the_config_keys(tmp_path: Path) -> None:
    env = config(tmp_path, AWS_REGION="eu-central-1", MODEL_ID="acme/Foo-7B", MODEL_REVISION="ab" * 20)
    assert val(tmp_path / "x", '"$MODEL_ID"', env) == "acme/Foo-7B"
    assert val(tmp_path / "y", '"$MODEL_REVISION"', env) == "ab" * 20


def test_no_literal_model_fallback_when_the_config_has_no_model_keys(tmp_path: Path) -> None:
    env = config(tmp_path, AWS_REGION="eu-central-1")
    for var in ("MODEL_ID", "MODEL_REVISION"):
        assert val(tmp_path / var, f'"${var}"', env) == "", var


def test_platform_env_default_and_explicit_file(tmp_path: Path) -> None:
    assert val(tmp_path, '"$PLATFORM_ENV"') == f"{MAN}/config/platform.env"
    for key in ("AWS_REGION", "MODEL_BUCKET", "RESULTS_BUCKET", "CLUSTER_NAME"):
        assert val(tmp_path / key, f'"$CFG_{key}"') == T.CFG[key], key
    env = config(tmp_path / "c", AWS_REGION="us-east-2", RESULTS_BUCKET="custom-bucket")
    assert val(tmp_path / "d", '"$CFG_AWS_REGION"', env) == "us-east-2"


def test_namespaces_are_the_four_scenario_ones(tmp_path: Path) -> None:
    want = "s-agg s-agg-hic s-dis s-dis-hic"
    assert val(tmp_path, '"$SCENARIO_NAMESPACES"') == want
    assert val(tmp_path / "include", '"$SCENARIO_NAMESPACES"', {INCLUDE_SWITCH: "1"}) == want


# ----------------------------------------------------------------------------- kube target guard
def guard(tmp: Path, context: str, env: dict[str, str] | None = None) -> S.Result:
    return rc_of(tmp, "require_kube_target", {"KUBE_CONTEXT": context, "KUBERNETES_SERVICE_HOST": "", **(env or {})})


@pytest.mark.parametrize("context", [T.CONTEXT, T.ARN])
def test_guard_accepts_the_configured_cluster_by_name_or_arn(tmp_path: Path, context: str) -> None:
    res = guard(tmp_path, context)
    assert res.rc == 0, res.stdout + res.stderr


@pytest.mark.parametrize(
    "context",
    ["qa-context", "dynamo-sample", "dynamo-sample-use2x", "xdynamo-sample-use2", "arn:aws:eks:us-east-2:123456789012:cluster/other",
     "arn:aws:eks:us-east-2:123456789012:cluster/dynamo-sample-use2-extra"],
)
def test_guard_refuses_another_cluster(tmp_path: Path, context: str) -> None:
    res = guard(tmp_path, context)
    assert res.rc != 0 and T.CONTEXT in res.stdout + res.stderr


def test_guard_refuses_a_missing_context_off_cluster(tmp_path: Path) -> None:
    res = guard(tmp_path, "")
    assert res.rc != 0 and "KUBE_CONTEXT" in res.stdout + res.stderr


def test_guard_in_cluster_needs_no_context(tmp_path: Path) -> None:
    assert guard(tmp_path, "", {"KUBERNETES_SERVICE_HOST": "10.96.0.1"}).rc == 0


def test_guard_pins_the_cluster_name_of_the_config_not_the_bucket_name(tmp_path: Path) -> None:
    env = config(tmp_path, MODEL_BUCKET="acme-models-123456789012-xyz", CLUSTER_NAME="my-cluster")
    assert guard(tmp_path / "ok", "my-cluster", env).rc == 0
    assert guard(tmp_path / "arn", "arn:aws:eks:eu-west-1:123456789012:cluster/my-cluster", env).rc == 0
    assert guard(tmp_path / "bucket", "acme-xyz", env).rc != 0, "the cluster must not be derived from MODEL_BUCKET"


def test_guard_without_cluster_name_accepts_any_explicit_context(tmp_path: Path) -> None:
    env = config(tmp_path, MODEL_BUCKET="acme-models-123456789012-xyz")
    assert guard(tmp_path / "a", "whatever", env).rc == 0
    assert guard(tmp_path / "b", "", env).rc != 0


SCRIPT_CASES: dict[str, tuple[str, ...]] = {
    "11-prefetch.sh": (),
}


@pytest.mark.parametrize("script", list(SCRIPT_CASES))
@pytest.mark.parametrize("context", ["qa-context", "dynamo-sample", "arn:aws:eks:us-west-2:123456789012:cluster/dynamo-sample-usw2"])
def test_scripts_refuse_another_cluster_context(tmp_path: Path, script: str, context: str) -> None:
    sb = S.Sandbox(tmp_path, {"pods": T.all_q38_pods()})
    res = sb.run(script, *SCRIPT_CASES[script], env={**T.ENV_Q, "KUBE_CONTEXT": context})
    assert res.rc != 0, res.stdout + res.stderr
    assert T.CONTEXT in res.stdout + res.stderr
    assert T.cluster_calls(res) == [], "no cluster call may precede the context check"


@pytest.mark.parametrize("script", list(SCRIPT_CASES))
def test_scripts_accept_the_configured_cluster_arn(tmp_path: Path, script: str) -> None:
    res = S.Sandbox(tmp_path, {}).run(script, *SCRIPT_CASES[script], env={**T.ENV_Q, "KUBE_CONTEXT": T.ARN})
    assert res.rc == 0, res.stdout + res.stderr
    assert T.cluster_calls(res) and all(f"--context={T.ARN}" in c["argv"] for c in T.cluster_calls(res))
