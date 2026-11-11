# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""One platform config source: account, region, buckets (publish-critical guard).

Why: ``manifests/`` and ``scripts/`` must not hardcode any deployment value (a region, ``-use2``/``-euw3``/``-usw2``
short codes, the account id, an ECR host, a bucket name).  The public sample is re-pointed at another region by editing
ONE file, filled from Terraform outputs (``scripts/gen_env.py``); the tracked reference is ``platform.env.example``.

Contract
========
* ``manifests/config/platform.env`` is the single source: ``KEY=VALUE`` lines (``#`` comments), uppercase keys,
  no quotes/spaces/``$`` (so ``terraform output -raw`` can emit it).  Required keys: ``AWS_ACCOUNT_ID``,
  ``AWS_REGION``, ``MODEL_BUCKET``, ``RESULTS_BUCKET``.  Extra keys are allowed.
* The values reach the scripts because ``scripts/lib/common.sh`` reads the same file; ``scripts/prefetch.sh``
  sets them on the prefetch Job with ``kubectl set env --local`` (the Job itself carries placeholders).
* No ``eu-west-3`` / ``euw3`` / 12-digit account id / ECR host / bucket literal / any region literal anywhere in
  ``manifests/`` or ``scripts/`` except ``manifests/config/platform.env``.  The exception list is EMPTY on purpose:
  comments citing history are not allowed either (``vpc.amazonaws.com/efa`` is a resource name and matches none of
  the patterns).
* Scripts: env wins, the config is the default (``MODEL_BUCKET``, ``RESULTS_BUCKET``; ``MODEL_BUCKET_REGION`` /
  ``RESULTS_BUCKET_REGION`` override the region).  An ambient workstation ``AWS_REGION``/``AWS_DEFAULT_REGION``
  (the harness leaves ``eu-west-3`` in the environment on purpose) must NOT leak into S3 calls.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import lib_manifests as L
import lib_q38 as Q
import lib_scripts as S

REQUIRED = ("AWS_ACCOUNT_ID", "AWS_REGION", "MODEL_BUCKET", "RESULTS_BUCKET")
# The placeholder deployment of the tracked platform.env.example (account 123456789012, us-east-2).
EXPECTED = Q.PLACEHOLDER

REGION_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:us|eu|ap|sa|ca|me|af|il|mx)-(?:north|south|east|west|central|northeast|southeast|northwest|southwest)-[0-9](?![A-Za-z0-9])"
)
SHORT_RE = re.compile(r"(?<![A-Za-z0-9])(?:use|usw|euw|euc|eun|aps|apn|sae|cac)[0-9](?![A-Za-z0-9])")
ACCOUNT_RE = re.compile(r"(?<![A-Za-z0-9])[0-9]{12}(?![A-Za-z0-9])")
ECR_HOST_RE = re.compile(r"[0-9]{12}\.dkr\.ecr\.[a-z0-9-]+\.amazonaws\.com")
S3_RE = re.compile(r"s3://([a-z0-9][a-z0-9.-]*)")
FORBIDDEN_LITERALS = ("eu-west-3", "euw3", "us-west-2", "usw2")  # stale values of earlier deployments

# building blocks, not entry points (any `component*` directory is a kustomize Component)
EXCLUDE_PARTS = {"base", "components", "core", "frontend", "config"}


# ----------------------------------------------------------------------------- helpers
def _cfg() -> dict[str, str]:
    return L.platform_config()


def _targets() -> list[str]:
    out = []
    for kust in sorted(L.MANIFESTS.rglob("kustomization.yaml")):
        rel = kust.parent.relative_to(L.MANIFESTS)
        if rel.parts and not (set(rel.parts) & EXCLUDE_PARTS) and not any(x.startswith("component") for x in rel.parts):
            out.append(str(rel))
    return out


# Every kustomize entry point under manifests/ is a target.
TARGETS = _targets()


def _tree_files() -> list[Path]:
    config = L.CONFIG_ENV.resolve()
    # the tracked platform.env.example is the same config source, with placeholder values (account 123456789012);
    # a fresh clone has only the example, the generated platform.env is gitignored.  These two are the ONLY exceptions.
    examples = {p.resolve() for p in L.MANIFESTS.glob("config/platform.env.example")}
    files = []
    for root in (L.MANIFESTS, S.SCRIPTS_DIR):
        for p in sorted(root.rglob("*")):
            if p.is_file() and p.resolve() not in {config, *examples} and p.name != ".DS_Store" and "__pycache__" not in p.parts:
                files.append(p)
    return files


def _read(p: Path) -> str | None:
    try:
        return p.read_text()
    except UnicodeDecodeError:
        return None


# ----------------------------------------------------------------------------- the config source
def test_config_source_exists_with_required_keys() -> None:
    assert L.CONFIG_ENV.is_file(), f"missing {L.CONFIG_ENV}"
    cfg = _cfg()
    missing = [k for k in REQUIRED if not cfg.get(k)]
    assert not missing, f"platform.env lacks {missing}"


def test_config_is_plain_key_value_fillable_from_terraform_outputs() -> None:
    assert L.CONFIG_ENV.is_file()
    for n, raw in enumerate(L.CONFIG_ENV.read_text().splitlines(), 1):
        line = raw.strip()
        if line and not line.startswith("#"):
            assert re.fullmatch(r"[A-Z][A-Z0-9_]*=[^\s\"'#$`]*", line), f"platform.env:{n}: not plain KEY=VALUE: {raw!r}"
    assert not [k for k in _cfg() if re.search(r"TOKEN|SECRET|PASSWORD|PRIVATE", k)], "no credentials in the config"


def test_config_values_are_well_formed_and_consistent() -> None:
    cfg = _cfg()
    assert re.fullmatch(r"[0-9]{12}", cfg.get("AWS_ACCOUNT_ID", "")), cfg.get("AWS_ACCOUNT_ID")
    assert REGION_RE.fullmatch(cfg.get("AWS_REGION", "")), cfg.get("AWS_REGION")
    for key in ("MODEL_BUCKET", "RESULTS_BUCKET"):
        assert re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", cfg.get(key, "")), (key, cfg.get(key))
    assert cfg.get("MODEL_BUCKET") != cfg.get("RESULTS_BUCKET")


def test_config_holds_the_placeholder_deployment() -> None:
    cfg = _cfg()
    assert {k: cfg.get(k) for k in EXPECTED} == EXPECTED


# ----------------------------------------------------------------------------- nothing outside the config
def test_tree_to_scan_exists() -> None:
    assert [p for p in _tree_files() if p.name == "prefetch.sh"] and [p for p in _tree_files() if p.suffix == ".yaml"]


@pytest.mark.parametrize(
    "label,pattern",
    [
        ("eu-west-3 / euw3", re.compile("|".join(FORBIDDEN_LITERALS))),
        ("any region literal", REGION_RE),
        ("short region code (usw2, euw3, ...)", SHORT_RE),
        ("12-digit account id", ACCOUNT_RE),
        ("ECR host", re.compile(r"\.dkr\.ecr\.")),
        ("project bucket name", re.compile(r"(?:kubecon" + r"-dynamo|dynamo-sample)-(?:models|results)-")),
    ],
)
def test_no_region_account_or_bucket_literal_outside_the_config(label: str, pattern: re.Pattern[str]) -> None:
    """Explicit exception list: none.  Comments citing history count too."""
    offenders = []
    for p in _tree_files():
        text = _read(p)
        if text is None:
            continue
        for n, line in enumerate(text.splitlines(), 1):
            if pattern.search(line):
                offenders.append(f"{p.relative_to(L.REPO_ROOT)}:{n}: {line.strip()[:110]}")
    assert not offenders, f"{label} outside manifests/config/platform.env:\n" + "\n".join(offenders)


# ----------------------------------------------------------------------------- every rendered target
def test_render_targets_discovered() -> None:
    assert "node-ops" in TARGETS, TARGETS



@pytest.mark.parametrize("rel", TARGETS)
def test_render_carries_only_config_values(rel: str) -> None:
    cfg = _cfg()
    text = L.render_text(rel)
    assert not any(lit in text for lit in FORBIDDEN_LITERALS), "stale region literal in the rendered output"
    assert set(REGION_RE.findall(text)) <= {cfg.get("AWS_REGION")}, set(REGION_RE.findall(text))
    allowed_short = set(SHORT_RE.findall(" ".join(cfg.values())))
    assert set(SHORT_RE.findall(text)) <= allowed_short, set(SHORT_RE.findall(text))
    assert set(ACCOUNT_RE.findall(text)) <= {cfg.get("AWS_ACCOUNT_ID")}, set(ACCOUNT_RE.findall(text))
    assert not ECR_HOST_RE.findall(text), set(ECR_HOST_RE.findall(text))
    assert set(S3_RE.findall(text)) <= {cfg.get("MODEL_BUCKET"), cfg.get("RESULTS_BUCKET")}, set(S3_RE.findall(text))


# ----------------------------------------------------------------------------- scripts
KUBE_ENV = {"KUBE_CONTEXT": "dynamo-sample-use2", "KUBERNETES_SERVICE_HOST": ""}  # CLUSTER_NAME of the placeholder config


def _sandbox(tmp: Path) -> S.Sandbox:
    comps = [("worker-bbb-one", "10.0.1.101", "worker"), ("worker-ccc-two", "10.0.1.102", "worker")]
    pods = {"s-agg": [S.pod("fe-frontend-aaa", "10.0.9.201", "frontend", "s-agg")] + [S.pod(n, ip, t, "s-agg") for n, ip, t in comps]}
    return S.Sandbox(tmp, {"pods": pods, "markers": "present"}, default_env=S.series_env(tmp))


def test_common_sh_reads_the_same_config_file() -> None:
    assert "platform.env" in (S.SCRIPTS_DIR / "lib" / "common.sh").read_text()


def _prefetch(tmp: Path, *args: str, env: dict[str, str] | None = None) -> list[str]:
    res = _sandbox(tmp).run("prefetch.sh", *args, env={**KUBE_ENV, **(env or {})})
    assert res.rc == 0, res.stdout + res.stderr
    calls = [c for c in res.kubectl_calls() if "set" in c["argv"] and "env" in c["argv"]]
    assert calls, "no `kubectl set env` call carrying the Job parameters"
    return calls[0]["argv"]


def test_prefetch_defaults_to_the_configured_bucket_and_region_ignoring_ambient_region(tmp_path: Path) -> None:
    argv = _prefetch(tmp_path, env={"MODEL_BUCKET": ""})
    assert f"MODEL_S3_URI=s3://{L.cfg('MODEL_BUCKET')}/{L.cfg('MODEL_S3_PREFIX')}/" in argv, argv
    assert f"AWS_REGION={L.cfg('AWS_REGION')}" in argv, argv  # harness ambient AWS_REGION is eu-west-3


def test_prefetch_env_overrides_the_config(tmp_path: Path) -> None:
    argv = _prefetch(tmp_path, env={"MODEL_BUCKET": "custom-models-bucket", "MODEL_BUCKET_REGION": "eu-central-1", "MODEL_PREFIX": "custom-prefix"})
    assert "MODEL_S3_URI=s3://custom-models-bucket/custom-prefix/" in argv, argv
    assert "AWS_REGION=eu-central-1" in argv, argv
