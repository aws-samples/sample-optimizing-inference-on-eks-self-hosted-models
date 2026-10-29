# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""``preflight-flags.sh`` with one ``--help`` capture per entrypoint (FT-4 finding).  RED until implemented.

Today only ``dynamo.sglang`` containers are checked.  Contract:

    preflight-flags.sh --help-file sglang=FILE [--help-file frontend=FILE] [--help-file mocker=FILE] <manifest|dir>...

* The label is one of ``sglang`` (``python3 -m dynamo.sglang``), ``frontend`` (``dynamo.frontend``),
  ``mocker`` (``dynamo.mocker``).  Each container is classified by the entrypoint module found in
  its command/args (including a ``bash -lc`` body) and its ``--flags`` (also ``--flag=value``) are
  matched exactly against the capture **of that entrypoint only** (a flag valid for another
  entrypoint is unknown here).  Containers with no recognised entrypoint are skipped.
* A manifest container whose entrypoint has no capture in a labelled invocation is an error
  (non-zero, the entrypoint is named); an unknown label, a missing or empty capture is a usage
  error (non-zero).  Output lines for problems name the flag and the entrypoint label.
* Back-compat: a single unlabelled ``--help-file FILE`` keeps the old meaning (``sglang`` only, other
  entrypoints ignored); the existing ``test_40_preflight_flags.py`` still applies.
* ConfigMap ``--config`` keys remain an SGLang-only concept.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import lib_scripts as S

HELP = {
    "sglang": "usage: python3 -m dynamo.sglang [-h]\n  --model-path M\n  --tensor-parallel-size N\n  --trust-remote-code\n  --kv-events-config C\n",
    "frontend": "usage: python3 -m dynamo.frontend [-h]\n  --router-mode MODE\n  --http-port PORT\n  --trust-remote-code\n  --router-kv-events\n",
    "mocker": "usage: python3 -m dynamo.mocker [-h]\n  --model-path M\n  --block-size N\n  --num-gpu-blocks-override N\n  --kv-bytes-per-token N\n  --engine-type T\n",
}
# flag valid only for one entrypoint -> used for cross-entrypoint negative controls
ONLY = {"sglang": "--tensor-parallel-size", "frontend": "--http-port", "mocker": "--block-size"}
MODULE = {"sglang": "dynamo.sglang", "frontend": "dynamo.frontend", "mocker": "dynamo.mocker"}
GOOD_ARGS = {
    "sglang": ["--model-path", "x", "--tensor-parallel-size", "4"],
    "frontend": ["--router-mode", "kv", "--http-port", "8000"],
    "mocker": ["--model-path", "x", "--block-size", "64", "--num-gpu-blocks-override", "1000"],
}


def _container(entry: str, args: list[str], *, bash: bool = False) -> str:
    if bash:
        body = f"set -e\\nexec python3 -m {MODULE[entry]} " + " ".join(args) + "\\n"
        return f'            - {{name: {entry}, image: x, command: [/bin/bash, -lc], args: ["{body}"]}}\n'
    toks = ", ".join(f"'{a}'" for a in args)
    return f"            - {{name: {entry}, image: x, command: [python3, -m, {MODULE[entry]}], args: [{toks}]}}\n"


def _manifest(tmp: Path, containers: dict[str, list[str]], *, bash: bool = False) -> Path:
    body = "".join(_container(e, a, bash=bash) for e, a in containers.items())
    path = tmp / "rendered.yaml"
    path.write_text(
        "apiVersion: nvidia.com/v1beta1\nkind: DynamoGraphDeployment\nmetadata: {name: t, namespace: s-agg}\n"
        "spec:\n  components:\n    - name: c\n      type: worker\n      podTemplate:\n        spec:\n          containers:\n" + body
    )
    return path


def _helps(tmp: Path, entries: tuple[str, ...] = ("sglang", "frontend", "mocker")) -> list[str]:
    out: list[str] = []
    for e in entries:
        f = tmp / f"{e}.help.txt"
        f.write_text(HELP[e])
        out += ["--help-file", f"{e}={f}"]
    return out


def _run(tmp: Path, manifest: Path, helps: list[str]) -> S.Result:
    return S.Sandbox(tmp / "sb", {}).run("preflight-flags.sh", *helps, str(manifest))


# ----------------------------------------------------------------------------- positive
def test_all_three_entrypoints_with_their_own_flags_pass(tmp_path: Path) -> None:
    m = _manifest(tmp_path, GOOD_ARGS)
    res = _run(tmp_path, m, _helps(tmp_path))
    assert res.rc == 0, res.stdout + res.stderr


def test_flag_equals_value_and_bash_lc_bodies_pass_per_entrypoint(tmp_path: Path) -> None:
    m = _manifest(tmp_path, {"mocker": ["--block-size=64", "--engine-type", "sglang"], "frontend": ["--router-mode=kv"]}, bash=True)
    res = _run(tmp_path, m, _helps(tmp_path))
    assert res.rc == 0, res.stdout + res.stderr


def test_only_needed_captures_are_required(tmp_path: Path) -> None:
    m = _manifest(tmp_path, {"mocker": GOOD_ARGS["mocker"]})
    res = _run(tmp_path, m, _helps(tmp_path, ("mocker",)))
    assert res.rc == 0, res.stdout + res.stderr


def test_container_without_a_known_entrypoint_is_skipped(tmp_path: Path) -> None:
    m = tmp_path / "m.yaml"
    m.write_text(
        "apiVersion: nvidia.com/v1beta1\nkind: DynamoGraphDeployment\nmetadata: {name: t}\nspec:\n  components:\n"
        "    - name: fe\n      type: frontend\n      podTemplate:\n        spec:\n          containers:\n"
        "            - {name: main, image: x, args: ['--not-checked-anywhere']}\n"
    )
    res = _run(tmp_path, m, _helps(tmp_path))
    assert res.rc == 0, res.stdout + res.stderr


# ----------------------------------------------------------------------------- negative control per entrypoint
@pytest.mark.parametrize("entry", list(MODULE))
def test_unknown_flag_fails_for_each_entrypoint_and_names_it(tmp_path: Path, entry: str) -> None:
    m = _manifest(tmp_path, {entry: [*GOOD_ARGS[entry], "--made-up-flag", "1"]})
    res = _run(tmp_path, m, _helps(tmp_path))
    out = res.stdout + res.stderr
    assert res.rc != 0
    assert "--made-up-flag" in out
    assert entry in out or MODULE[entry] in out, f"output should name the entrypoint {entry!r}: {out}"


@pytest.mark.parametrize("entry", list(MODULE))
def test_prefix_of_a_known_flag_is_rejected_per_entrypoint(tmp_path: Path, entry: str) -> None:
    prefix = ONLY[entry][:-2]  # e.g. --block-siz
    m = _manifest(tmp_path, {entry: [prefix, "1"]})
    res = _run(tmp_path, m, _helps(tmp_path))
    assert res.rc != 0 and prefix in res.stdout + res.stderr


@pytest.mark.parametrize(
    "entry,foreign",
    [("mocker", "sglang"), ("mocker", "frontend"), ("sglang", "mocker"), ("sglang", "frontend"), ("frontend", "sglang"), ("frontend", "mocker")],
)
def test_flag_valid_only_for_another_entrypoint_is_rejected(tmp_path: Path, entry: str, foreign: str) -> None:
    """Checked against its OWN capture: a flag that only another entrypoint declares is unknown here."""
    flag = ONLY[foreign]
    assert flag not in HELP[entry]  # fixture sanity
    m = _manifest(tmp_path, {entry: [flag, "1"]})
    res = _run(tmp_path, m, _helps(tmp_path))
    assert res.rc != 0 and flag in res.stdout + res.stderr


def test_unknown_flag_in_bash_lc_mocker_body_fails(tmp_path: Path) -> None:
    m = _manifest(tmp_path, {"mocker": ["--block-size", "64", "--bogus-in-body", "1"]}, bash=True)
    res = _run(tmp_path, m, _helps(tmp_path))
    assert res.rc != 0 and "--bogus-in-body" in res.stdout + res.stderr


def test_one_bad_entrypoint_fails_the_whole_run(tmp_path: Path) -> None:
    args = {**GOOD_ARGS, "mocker": [*GOOD_ARGS["mocker"], "--bogus", "1"]}
    res = _run(tmp_path, _manifest(tmp_path, args), _helps(tmp_path))
    assert res.rc != 0 and "--bogus" in res.stdout + res.stderr


# ----------------------------------------------------------------------------- usage errors
@pytest.mark.parametrize("missing", ["frontend", "mocker", "sglang"])
def test_entrypoint_without_a_capture_is_an_error(tmp_path: Path, missing: str) -> None:
    m = _manifest(tmp_path, GOOD_ARGS)
    have = tuple(e for e in MODULE if e != missing)
    res = _run(tmp_path, m, _helps(tmp_path, have))
    out = res.stdout + res.stderr
    assert res.rc != 0
    assert missing in out or MODULE[missing] in out, out


def test_unknown_label_is_a_usage_error(tmp_path: Path) -> None:
    f = tmp_path / "x.txt"
    f.write_text("--a\n")
    m = _manifest(tmp_path, {"mocker": GOOD_ARGS["mocker"]})
    res = _run(tmp_path, m, ["--help-file", f"planner={f}", *_helps(tmp_path, ("mocker",))])
    assert res.rc != 0


@pytest.mark.parametrize("kind", ["missing", "empty"])
def test_missing_or_empty_labelled_capture_is_an_error(tmp_path: Path, kind: str) -> None:
    f = tmp_path / "mocker.help.txt"
    if kind == "empty":
        f.write_text("")
    m = _manifest(tmp_path, {"mocker": GOOD_ARGS["mocker"]})
    res = _run(tmp_path, m, ["--help-file", f"mocker={f}"])
    assert res.rc != 0


# ----------------------------------------------------------------------------- back-compat
def test_unlabelled_help_file_still_means_sglang_only(tmp_path: Path) -> None:
    f = tmp_path / "sglang.help.txt"
    f.write_text(HELP["sglang"])
    m = _manifest(tmp_path, {"sglang": GOOD_ARGS["sglang"], "frontend": ["--anything-goes", "1"], "mocker": ["--also-ignored", "1"]})
    res = S.Sandbox(tmp_path / "sb", {}).run("preflight-flags.sh", "--help-file", str(f), str(m))
    assert res.rc == 0, res.stdout + res.stderr


def test_unlabelled_help_file_still_rejects_unknown_sglang_flag(tmp_path: Path) -> None:
    f = tmp_path / "sglang.help.txt"
    f.write_text(HELP["sglang"])
    m = _manifest(tmp_path, {"sglang": [*GOOD_ARGS["sglang"], "--nope", "1"]})
    res = S.Sandbox(tmp_path / "sb", {}).run("preflight-flags.sh", "--help-file", str(f), str(m))
    assert res.rc != 0 and "--nope" in res.stdout + res.stderr
