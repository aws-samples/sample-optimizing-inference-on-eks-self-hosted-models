# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Helpers for the manifest contract tests (render, CRD validation, flag parsing).

The contract encoded here covers the scenario matrix and topologies, the scenario manifests, the toggle and
cache-reset mechanics, the harness fairness rules and the acceptance gates.  Nothing in this module weakens an expectation:
a missing directory or file is reported as a failure, never skipped.
"""

from __future__ import annotations

import functools
import json
import os
import shlex
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
MANIFESTS = Path(os.environ.get("MANIFESTS_DIR", REPO_ROOT / "manifests"))
TRACK_Q = "track-q-qwen38"

# Checkout of the Dynamo repository at v1.5.1 (the DGD CRD and recipes are read from it): DYNAMO_REPO, default a sibling
# directory named "dynamo".  Tests that need it skip with a reason when it is absent (see ``require_dynamo_repo``).
DYNAMO_REPO = Path(os.environ.get("DYNAMO_REPO", REPO_ROOT.parent / "dynamo"))
CRD_GIT_REF = "v1.5.1:deploy/operator/config/crd/bases/nvidia.com_dynamographdeployments.yaml"

# The stage-mocker image (manifests/stage-mocker).
PINNED_IMAGE = (
    "nvcr.io/nvidia/ai-dynamo/sglang-runtime:1.5.1"
    "@sha256:37badffd83eb334aee9d06fb442e36fc3c963314336c92622c78ac5c037d514d"
)

# Single platform config source (account, region, buckets, ECR host): manifests/config/platform.env.
# Read here so no test pins a region/account literal.  A missing file yields sentinels that match nothing.
CONFIG_ENV = MANIFESTS / "config" / "platform.env"


def platform_config() -> dict[str, str]:
    """Parse ``manifests/config/platform.env`` (``KEY=VALUE`` lines, ``#`` comments); ``{}`` if missing."""
    if not CONFIG_ENV.is_file():
        return {}
    out: dict[str, str] = {}
    for raw in CONFIG_ENV.read_text().splitlines():
        line = raw.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            out[key.strip()] = value.strip()
    return out


def cfg(key: str) -> str:
    """Config value, or an unmatchable sentinel when the config source is missing/incomplete."""
    return platform_config().get(key) or f"<platform.env:{key}-missing>"


# Default source of node-ops/job-model-prefetch.yaml: the config model (11-prefetch.sh overrides it per run).
S3_SOURCE = f"{cfg('MODEL_S3_URI')}/"

# topology overlay -> namespace
TOPOLOGIES: dict[str, str] = {
    "t-agg": "s-agg",
    "t-agg-hic": "s-agg-hic",
    "t-dis": "s-dis",
    "t-dis-hic": "s-dis-hic",
}
AGG_TOPOLOGIES = ("t-agg", "t-agg-hic")
DIS_TOPOLOGIES = ("t-dis", "t-dis-hic")
HIC_TOPOLOGIES = ("t-agg-hic", "t-dis-hic")
NO_HIC_TOPOLOGIES = ("t-agg", "t-dis")

PRIVILEGED_NAMESPACES = {"s-agg", "s-agg-hic", "s-dis", "s-dis-hic", "node-ops"}

WORKER_TYPES = ("worker", "prefill", "decode")


# --------------------------------------------------------------------------- render
def overlay_path(rel: str) -> Path:
    """Return the absolute path of a manifests-relative kustomize directory."""
    return MANIFESTS / rel


def topology_rel(topology: str) -> str:
    return f"{TRACK_Q}/overlays/{topology}"


def frontend_rel(mode: str) -> str:
    return f"{TRACK_Q}/frontends/{mode}"


@functools.lru_cache(maxsize=None)
def render_text(rel: str) -> str:
    """Render ``manifests/<rel>`` with ``kubectl kustomize`` and return the YAML text."""
    path = overlay_path(rel)
    assert path.is_dir(), f"missing kustomize directory: manifests/{rel}"
    assert (path / "kustomization.yaml").is_file() or (
        path / "kustomization.yml"
    ).is_file(), f"missing kustomization.yaml in manifests/{rel}"
    proc = subprocess.run(
        ["kubectl", "kustomize", str(path)],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    assert proc.returncode == 0, (
        f"`kubectl kustomize manifests/{rel}` failed:\n{proc.stderr.strip()}"
    )
    return proc.stdout


def render(rel: str) -> list[dict[str, Any]]:
    """Render ``manifests/<rel>`` and return the parsed documents."""
    return [d for d in yaml.safe_load_all(render_text(rel)) if d]


def dgds(docs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [d for d in docs if d.get("kind") == "DynamoGraphDeployment"]


def one_dgd(rel: str) -> dict[str, Any]:
    found = dgds(render(rel))
    assert len(found) == 1, (
        f"manifests/{rel} must render exactly one DynamoGraphDeployment, got {len(found)}"
    )
    return found[0]


def components(dgd: dict[str, Any]) -> list[dict[str, Any]]:
    return list(dgd["spec"]["components"])


def components_of_type(dgd: dict[str, Any], *types: str) -> list[dict[str, Any]]:
    return [c for c in components(dgd) if c.get("type") in types]


def workers(dgd: dict[str, Any]) -> list[dict[str, Any]]:
    return components_of_type(dgd, *WORKER_TYPES)


def pod_spec(comp: dict[str, Any]) -> dict[str, Any]:
    return comp.get("podTemplate", {}).get("spec", {}) or {}


def pod_meta(comp: dict[str, Any]) -> dict[str, Any]:
    return comp.get("podTemplate", {}).get("metadata", {}) or {}


def main_container(comp: dict[str, Any]) -> dict[str, Any]:
    for c in pod_spec(comp).get("containers", []):
        if c.get("name") == "main":
            return c
    raise AssertionError(f"component {comp.get('name')!r} has no container named 'main'")


def env_map(container: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {e["name"]: e for e in container.get("env", []) or []}


def env_value(container: dict[str, Any], name: str) -> str | None:
    """Return the literal value of env ``name`` ('' if empty), or None if unset."""
    entry = env_map(container).get(name)
    if entry is None:
        return None
    return str(entry.get("value", "")) if entry.get("value") is not None else ""


def volume_by_name(comp: dict[str, Any], name: str) -> dict[str, Any] | None:
    for v in pod_spec(comp).get("volumes", []) or []:
        if v.get("name") == name:
            return v
    return None


def host_path_volumes(comp: dict[str, Any]) -> list[dict[str, Any]]:
    return [v for v in pod_spec(comp).get("volumes", []) or [] if "hostPath" in v]


# ------------------------------------------------------------------ CRD validation
@functools.lru_cache(maxsize=1)
def require_dynamo_repo() -> None:
    """Skip (with a reason) when the Dynamo checkout the CRD is read from is not available."""
    if not (DYNAMO_REPO / ".git").exists():
        pytest.skip("needs a Dynamo checkout at v1.5.1: set DYNAMO_REPO (default: a sibling directory named 'dynamo')")


def crd_text() -> str:
    require_dynamo_repo()
    proc = subprocess.run(
        ["git", "-C", str(DYNAMO_REPO), "show", CRD_GIT_REF],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        pytest.fail(
            f"cannot extract the v1.5.1 DGD CRD from {DYNAMO_REPO}: {proc.stderr.strip()}"
        )
    return proc.stdout


def _convert(node: Any) -> Any:
    """Convert an OpenAPI v3 (CRD) schema node to a strict JSON-Schema draft-4 node.

    Only the Kubernetes-specific keywords are dropped (``x-kubernetes-*``).
    ``nullable`` is translated, and object nodes that declare ``properties`` without
    ``additionalProperties``/``x-kubernetes-preserve-unknown-fields`` are closed, which
    mimics structural-schema pruning (an unknown field is an error here).
    """
    if isinstance(node, list):
        return [_convert(n) for n in node]
    if not isinstance(node, dict):
        return node
    preserve = bool(node.get("x-kubernetes-preserve-unknown-fields"))
    out: dict[str, Any] = {}
    for key, val in node.items():
        if key.startswith("x-kubernetes-"):
            continue
        if key in ("properties", "definitions"):
            out[key] = {k: _convert(v) for k, v in val.items()}
        elif key in ("enum", "required", "default", "example", "examples"):
            out[key] = val
        else:
            out[key] = _convert(val)
    if out.pop("nullable", False) and isinstance(out.get("type"), str):
        out["type"] = [out["type"], "null"]
    if (
        out.get("type") == "object"
        and "properties" in out
        and "additionalProperties" not in out
        and not preserve
    ):
        out["additionalProperties"] = False
    return out


@functools.lru_cache(maxsize=1)
def dgd_validator() -> Any:
    from jsonschema import Draft4Validator

    crd = yaml.safe_load(crd_text())
    version = next(v for v in crd["spec"]["versions"] if v["name"] == "v1beta1")
    schema = _convert(version["schema"]["openAPIV3Schema"])
    return Draft4Validator(schema)


def schema_errors(dgd: dict[str, Any]) -> list[str]:
    errors = []
    for err in dgd_validator().iter_errors(dgd):
        path = "/".join(str(p) for p in err.absolute_path)
        errors.append(f"{path or '<root>'}: {err.message[:200]}")
    return errors


# ------------------------------------------------------- SGLang effective flag model
def _split_arg(text: str) -> list[str]:
    if any(ch.isspace() for ch in text):
        try:
            return shlex.split(text.replace("\\\n", " "), comments=False)
        except ValueError:
            return text.split()
    return [text]


def cli_tokens(container: dict[str, Any]) -> list[str]:
    tokens: list[str] = []
    for part in list(container.get("command", []) or []) + list(container.get("args", []) or []):
        tokens.extend(_split_arg(str(part)))
    return tokens


def parse_cli_flags(tokens: list[str]) -> dict[str, list[str]]:
    flags: dict[str, list[str]] = {}
    current: str | None = None
    for tok in tokens:
        if tok.startswith("--") and len(tok) > 2:
            name, eq, val = tok[2:].partition("=")
            current = name.replace("_", "-")
            flags.setdefault(current, [])
            if eq:
                flags[current].append(val)
        elif current is not None:
            flags[current].append(tok)
    return flags


def _configmaps(docs: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {d["metadata"]["name"]: d for d in docs if d.get("kind") == "ConfigMap"}


def config_flags(
    docs: list[dict[str, Any]], comp: dict[str, Any], cli: dict[str, list[str]]
) -> dict[str, Any]:
    """Return the flags provided through ``--config <file>`` (ConfigMap-mounted YAML)."""
    cfg_vals = cli.get("config")
    if not cfg_vals:
        return {}
    cfg_path = cfg_vals[0]
    container = main_container(comp)
    cms = _configmaps(docs)
    for mount in container.get("volumeMounts", []) or []:
        mp = mount["mountPath"].rstrip("/")
        if not cfg_path.startswith(mp + "/"):
            continue
        fname = cfg_path[len(mp) + 1 :]
        vol = volume_by_name(comp, mount["name"])
        if not vol or "configMap" not in vol:
            continue
        cm = cms.get(vol["configMap"]["name"])
        if cm and fname in (cm.get("data") or {}):
            loaded = yaml.safe_load(cm["data"][fname]) or {}
            return {str(k).replace("_", "-"): v for k, v in loaded.items()}
    raise AssertionError(
        f"component {comp.get('name')!r}: --config {cfg_path} is not backed by a mounted ConfigMap"
    )


def effective_flags(docs: list[dict[str, Any]], comp: dict[str, Any]) -> dict[str, Any]:
    """Merge ConfigMap flags (lower precedence) and CLI flags (higher precedence)."""
    cli = parse_cli_flags(cli_tokens(main_container(comp)))
    merged: dict[str, Any] = dict(config_flags(docs, comp, cli))
    for name, vals in cli.items():
        if name == "config":
            continue
        merged[name] = vals
    return merged


def scalar(flags: dict[str, Any], name: str) -> str | None:
    """Normalise a flag to a string ('true'/'false' for booleans), None if absent."""
    if name not in flags:
        return None
    val = flags[name]
    if isinstance(val, list):
        if not val:
            return "true"
        val = val[0] if len(val) == 1 else " ".join(str(v) for v in val)
    if isinstance(val, bool):
        return "true" if val else "false"
    return str(val)


def same(actual: str | None, expected: str) -> bool:
    if actual is None:
        return False
    try:
        return float(actual) == float(expected)
    except ValueError:
        return actual == expected


def flag_obj(flags: dict[str, Any], name: str) -> Any:
    """Return a structured flag (e.g. kv-events-config) as a dict when possible."""
    if name not in flags:
        return None
    val = flags[name]
    if isinstance(val, list):
        val = " ".join(val)
    if isinstance(val, str):
        try:
            return json.loads(val)
        except ValueError:
            return yaml.safe_load(val)
    return val


def flag_tokens(flags: dict[str, Any], name: str) -> list[str]:
    val = flags.get(name)
    if val is None:
        return []
    if isinstance(val, str):
        return val.replace(",", " ").split()
    if isinstance(val, list):
        out: list[str] = []
        for v in val:
            out.extend(str(v).replace(",", " ").split())
        return out
    return [str(val)]


def hicache_keys(flags: dict[str, Any]) -> list[str]:
    return sorted(k for k in flags if "hicache" in k or k == "enable-hierarchical-cache")


# ----------------------------------------------------------------------- pod specs
def iter_pod_specs(docs: list[dict[str, Any]]) -> list[tuple[str, dict[str, Any]]]:
    """Yield ``(label, podSpec)`` for every pod spec embedded in the documents."""
    found: list[tuple[str, dict[str, Any]]] = []
    for d in docs:
        kind, name = d.get("kind"), d.get("metadata", {}).get("name", "?")
        spec = d.get("spec", {}) or {}
        if kind == "DynamoGraphDeployment":
            for c in spec.get("components", []) or []:
                found.append((f"DGD/{name}:{c.get('name')}", pod_spec(c)))
        elif kind == "Pod":
            found.append((f"Pod/{name}", spec))
        elif kind in ("Deployment", "StatefulSet", "DaemonSet", "ReplicaSet", "Job"):
            found.append((f"{kind}/{name}", spec.get("template", {}).get("spec", {}) or {}))
        elif kind == "CronJob":
            tmpl = spec.get("jobTemplate", {}).get("spec", {}).get("template", {})
            found.append((f"CronJob/{name}", tmpl.get("spec", {}) or {}))
    return found


def privileged_features(ps: dict[str, Any]) -> list[str]:
    feats: list[str] = []
    if any("hostPath" in v for v in ps.get("volumes", []) or []):
        feats.append("hostPath")
    for key in ("hostIPC", "hostNetwork", "hostPID"):
        if ps.get(key):
            feats.append(key)
    for c in (ps.get("containers", []) or []) + (ps.get("initContainers", []) or []):
        sc = c.get("securityContext", {}) or {}
        if sc.get("privileged"):
            feats.append(f"privileged({c.get('name')})")
        if (sc.get("capabilities", {}) or {}).get("add"):
            feats.append(f"capabilities.add({c.get('name')})")
    return feats


def all_container_images(ps: dict[str, Any]) -> list[str]:
    return [c.get("image", "") for c in (ps.get("containers", []) or [])]


def text_of(container: dict[str, Any]) -> str:
    return "\n".join(
        str(p) for p in list(container.get("command", []) or []) + list(container.get("args", []) or [])
    )


def deep_without_env(obj: Any, env_name: str) -> Any:
    """Return ``obj`` with the value of env ``env_name`` replaced by a placeholder."""
    if isinstance(obj, dict):
        if obj.get("name") == env_name and "value" in obj:
            return {**{k: deep_without_env(v, env_name) for k, v in obj.items()}, "value": "<X>"}
        return {k: deep_without_env(v, env_name) for k, v in obj.items()}
    if isinstance(obj, list):
        return [deep_without_env(v, env_name) for v in obj]
    return obj


def all_render_dirs() -> list[str]:
    """Every manifests-relative directory the contract requires."""
    rels = [topology_rel(t) for t in TOPOLOGIES]
    rels += [frontend_rel("rr"), frontend_rel("kv")]
    rels += ["bench", "node-ops"]
    return rels


def assert_job_deadline_is_sane(job_spec: dict[str, Any], tokens: list[str]) -> None:
    """P-15 policy for a matrix/sweep driver Job's ``activeDeadlineSeconds``.

    Absent is allowed (the driver's own ``--deadline-hours`` / ``--deadline-at`` bound it).  A present value is the
    absolute Job bound, so it must be a positive integer and must never undercut the driver's own graceful
    ``--deadline-hours`` window (the Job would be killed before the driver can abort cleanly).  A short value such as a
    12 h bound for an 11 h driver window is legitimate; the old blanket ">= 24 h" rule is not required.
    """
    ad = job_spec.get("activeDeadlineSeconds")
    if ad is None:
        return
    assert isinstance(ad, int) and not isinstance(ad, bool) and ad > 0, f"activeDeadlineSeconds must be a positive int, got {ad!r}"
    if "--deadline-hours" in tokens:
        hours = float(tokens[tokens.index("--deadline-hours") + 1])
        assert ad >= hours * 3600, f"activeDeadlineSeconds {ad} undercuts --deadline-hours {hours} ({hours * 3600:.0f} s)"
