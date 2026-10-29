# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Target tree of the publish cleanup, sub-cycle B (RED until the developer renames and removes).

Decisions (2026-10-25): the public sample covers ONLY Qwen/Qwen3.8-27B-FP8 (Track Q).  Therefore:

* ``manifests/config-aps1`` becomes ``manifests/config`` (the old Oregon ``manifests/config`` goes away); the generated,
  untracked file is ``manifests/config/platform.env`` and the tracked reference is ``platform.env.example``.
* The platform keys ``QWEN38_IMAGE`` / ``QWEN38_MODEL`` / ``QWEN38_REVISION`` become ``RUNTIME_IMAGE`` / ``MODEL_ID`` /
  ``MODEL_REVISION`` everywhere; ``CLUSTER_NAME`` stays in the config.
* Tracks A/B/F, the image mirror, the shell matrix driver, demo capture/rbac and the compile-cache jobs leave the tree
  together with their scripts and with the tests/goldens that only guarded them.

Kept on purpose (ambiguous in the inventory, so not asserted absent): ``manifests/demo/rerender`` (Q3, depends on the
w2 take plans staying), ``manifests/stage-mocker`` (Q8), ``tools/bench-image/traces`` (Q7), ``plans/`` (sub-cycle C),
``scripts/lib/common.sh`` DEMO_TRACK logic (sub-cycle D) and the test files the inventory marks REWRITE/ADAPT.
Only paths and tokens are asserted here; no assertion about behaviour that still has to exist was relaxed.
"""

from __future__ import annotations

import importlib.util
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import sanitize_scan as S
import yaml

REPO = S.REPO_ROOT
MANIFESTS = REPO / "manifests"
THIS = Path(__file__).resolve().relative_to(REPO).as_posix()

# tokens are assembled from fragments so that this file never matches its own scans
OLD_CONFIG = "config" + "-aps1"
OLD_KEYS = ("QWEN38" + "_IMAGE", "QWEN38" + "_MODEL", "QWEN38" + "_REVISION")
OLD_KEY_RE = re.compile(r"QWEN38" + r"_(?:IMAGE|MODEL|REVISION)")

TEXT_SUFFIXES = {".md", ".py", ".sh", ".yaml", ".yml", ".json", ".env", ".example", ".tf", ".hcl", ".txt", ".expected", ".tfvars"}


def _tracked() -> list[str]:
    return S.tracked_files()


def _text_files(prefixes: tuple[str, ...] = ("",), skip: tuple[str, ...] = ("docs/sdd/",)) -> list[str]:
    out = []
    for rel in _tracked():
        if rel == THIS or rel.startswith(skip) or not rel.startswith(prefixes):
            continue
        p = REPO / rel
        if p.is_symlink() or p.suffix not in TEXT_SUFFIXES and p.name not in {"Dockerfile", ".gitignore", ".dockerignore"}:
            continue
        out.append(rel)
    return out


def _read(rel: str) -> str:
    return (REPO / rel).read_text(errors="replace")


def _under(rel: str, prefix: str) -> bool:
    return rel == prefix or rel.startswith(prefix + "/")


# ----------------------------------------------------------------------------- (c) removed trees and scripts
REMOVED_TREES = [
    "manifests/track-a-glm",
    "manifests/track-b-qwen",
    "manifests/track-f-glm53flash",
    "manifests/mirror",
    "manifests/config-aps1",
    "manifests/bench/matrix-driver",
    "manifests/demo/capture",
    "manifests/demo/rbac",
    "manifests/node-ops/compile-cache-download",
    "manifests/node-ops/compile-cache-upload",
    "manifests/track-q-qwen38/config",  # symlink to ../config-aps1: dangles after the rename
    "scripts/12-compile-cache.sh",
    "scripts/20-topology-up.sh",
    "scripts/30-run.sh",
    "scripts/70-demo-take.sh",
    "scripts/99-run-matrix.sh",
    "scripts/series",
]


@pytest.mark.parametrize("prefix", REMOVED_TREES)
def test_removed_tree_is_not_tracked(prefix: str) -> None:
    left = [p for p in _tracked() if _under(p, prefix)]
    assert not left, f"{prefix} must be removed from the public tree, still tracked: {left[:5]} ({len(left)} files)"


# tests/goldens/helpers that guarded ONLY removed things (inventory section 2); they are deleted with the thing
REMOVED_TEST_ASSETS = [
    # tests/manifests: Track A/B/F, in-pod PD, mirror, compile-cache, demo capture/rbac, bench/matrix-driver
    *(f"tests/manifests/{n}.py" for n in (
        "test_00_layout_and_schema", "test_10_images_and_forbidden", "test_20_constants_and_topologies",
        "test_30_workers", "test_40_frontends", "test_50_lab", "test_80_matrix_driver_pod", "test_90_image_mirror",
        "test_100_trackb_common", "test_103_driver_policy", "test_110_trackb_variants", "test_120_trackb_mirror",
        "test_130_inpod_pd", "test_140_block_plans", "test_150_compile_cache", "test_160_driver_job_and_frontends",
        "test_170_trackc_findings", "test_180_block1_sweep", "test_200_pd_bootstrap_host", "test_210_privileged_gpu_pin",
        "test_220_inpod_frontend_prefix", "test_230_inpod_discovery_identity", "test_240_inpod_decode_scrape",
        "test_250_demo_capture", "test_260_pd_kv_capacity", "test_270_hicache_l3_direct_io",
        "test_280_demo_capture_rbac", "test_401_trackf_layout", "test_402_trackf_recipe_flags",
        "test_403_trackf_images_and_pd_transport", "test_404_trackf_goldens_and_ownership",
        "lib_trackf", "lib_trackb", "lib_inpod",
    )),
    *(f"tests/manifests/{n}.json" for n in (
        "golden_demo_capture_rbac", "golden_inpod_discovery", "golden_inpod_launchers", "golden_non_c_renders",
        "golden_other_renders", "golden_trackf_renders",
    )),
    # tests/scripts: 20-topology-up, 30-run, 70-demo-take, 99-run-matrix, series, Track F profile
    *(f"tests/scripts/{n}.py" for n in (
        "test_10_run", "test_30_topology_up", "test_50_matrix_driver", "test_60_schedule", "test_70_pd_placement",
        "test_80_sweep_driver", "test_97_driver_robustness", "test_98_arrival_recorded", "test_100_demo_take",
        "test_103_lab_opt_in", "test_130_q38_profile_and_take",
        "test_104_deadline_at", "test_106_flash_spec_docs", "test_120_flash_profile", "test_121_flash_topology_up",
        "test_122_flash_run", "test_123_flash_prefetch", "trackf_scripts",
    )),
]


@pytest.mark.parametrize("rel", REMOVED_TEST_ASSETS)
def test_test_asset_that_only_guards_removed_things_is_deleted(rel: str) -> None:
    assert rel not in set(_tracked()), f"{rel} guards only removed things, delete it together with them"


def test_publish_critical_guards_survive_the_cleanup() -> None:
    """The cleanup must not delete the suites that guard what stays (95, 108, 410-412, 420, dashboards, driver, terraform)."""
    tracked = set(_tracked())
    must_stay = [
        "tests/manifests/test_95_region_config.py",
        "tests/scripts/test_108_w3_16k_workload.py",
        "tests/manifests/test_410_trackq_layout_and_config.py",
        "tests/manifests/test_411_trackq_cells_vs_reference_and_knobs.py",
        "tests/manifests/test_412_trackq_rendered_drift_and_goldens.py",
        "tests/manifests/test_420_examples.py",
        "tests/manifests/golden_trackq_renders.json",
        "tests/manifests/lib_trackq.py",
        "tests/manifests/lib_manifests.py",
        "tests/scripts/test_151_stage_model_script.py",
        "tests/terraform/test_tree_layout.py",
        "tools/matrix_driver/tests/test_render_driver.py",
        "tools/matrix_driver/tests/test_matrix_driver.py",
    ]
    missing = [p for p in must_stay if p not in tracked]
    assert not missing, f"publish-critical guards must stay: {missing}"
    assert any(p.startswith("tests/dashboards/") for p in tracked)


# ----------------------------------------------------------------------------- (a) manifests/config
KEPT_TREES = [
    "manifests/config/kustomization.yaml",
    "manifests/config/platform.env.example",
    "manifests/track-q-qwen38/frontend/kustomization.yaml",
    "manifests/bench/kustomization.yaml",
    "manifests/bench/deployment.yaml",
    "manifests/bench/serviceaccount.yaml",
    "manifests/node-ops/kustomization.yaml",
    "manifests/node-ops/prepull/kustomization.yaml",
    "manifests/stage-mocker/kustomization.yaml",
    "scripts/10-stage-model.sh",
    "scripts/11-prefetch.sh",
    "scripts/13-prepull.sh",
    "scripts/50-toggle-router.sh",
    "scripts/gen_env.py",
    "scripts/preflight-flags.sh",
    "scripts/render-examples.sh",
    "scripts/render-track-q.sh",
]


@pytest.mark.parametrize("rel", KEPT_TREES)
def test_kept_file_is_tracked(rel: str) -> None:
    assert rel in set(_tracked()), f"{rel} must stay in the public tree"


def test_config_is_the_single_platform_config_with_no_tracked_generated_file() -> None:
    tracked = _tracked()
    assert "manifests/config/platform.env" not in tracked, "the generated platform.env must stay untracked"
    assert [p for p in tracked if p.endswith("/platform.env")] == []
    examples = [p for p in tracked if p.endswith("platform.env.example")]
    assert examples == ["manifests/config/platform.env.example"], examples
    assert sorted(p for p in tracked if _under(p, "manifests/config")) == [
        "manifests/config/kustomization.yaml",
        "manifests/config/platform.env.example",
    ]


def test_config_example_is_the_track_q_config_of_the_docs_region_not_the_old_oregon_one() -> None:
    cfg = dict(
        ln.split("=", 1)
        for ln in _read("manifests/config/platform.env.example").splitlines()
        if ln.strip() and not ln.startswith("#")
    )
    assert cfg["AWS_REGION"] == "us-east-2"  # the aws_region of the tfvars examples and the docs
    assert cfg["GPU_NODEPOOL"] == "gpu-b200-cb"
    assert cfg["MODEL_ID"] == "Qwen/Qwen3.8-27B-FP8"
    assert not [v for v in cfg.values() if "us-west-2" in v or "usw2" in v], "no Oregon value may survive"


def test_generated_platform_env_is_gitignored_at_its_new_path_only() -> None:
    def ignored(path: str) -> bool:
        return subprocess.run(["git", "-C", str(REPO), "check-ignore", "-q", path], check=False).returncode == 0

    assert ignored("manifests/config/platform.env")
    assert not ignored("manifests/config/platform.env.example")
    assert OLD_CONFIG not in _read(".gitignore")


def test_no_file_names_the_old_config_directory() -> None:
    hits = [rel for rel in _text_files() if OLD_CONFIG in _read(rel)]
    assert not hits, f"{OLD_CONFIG} was renamed to config, stale references: {hits}"


STALE_PATH_TOKENS = [
    "track-a-glm", "track-b-qwen", "track-f-glm53flash", "manifests/mirror", "20-topology-up", "30-run.sh",
    "70-demo-take", "99-run-matrix", "12-compile-cache", "compile-cache-upload", "compile-cache-download",
    "bench/matrix-driver",
]


@pytest.mark.parametrize("token", STALE_PATH_TOKENS)
def test_manifests_and_scripts_do_not_reference_removed_paths(token: str) -> None:
    hits = [rel for rel in _text_files(("manifests/", "scripts/")) if token in _read(rel)]
    assert not hits, f"{token!r} still referenced in {hits}"


def _kustomization_dirs() -> list[Path]:
    return sorted(p.parent for p in MANIFESTS.rglob("kustomization.yaml") if "rendered" not in p.parts)


def test_every_kustomization_path_resolves() -> None:
    """A dangling component/resource (the renamed config link, a removed tree) fails here, not at apply time."""
    dangling = []
    for d in _kustomization_dirs():
        doc = yaml.safe_load((d / "kustomization.yaml").read_text()) or {}
        for key in ("resources", "components", "bases"):
            for ref in doc.get(key) or []:
                if "://" in str(ref) or str(ref).startswith("git@"):
                    continue
                if not (d / ref).exists():
                    dangling.append(f"{d.relative_to(REPO)}: {key} {ref}")
    assert not dangling, dangling


@pytest.mark.parametrize(
    "rel", ["bench", "node-ops", "track-q-qwen38/frontend"]
)
def test_consumers_use_the_renamed_config_component(rel: str) -> None:
    doc = yaml.safe_load((MANIFESTS / rel / "kustomization.yaml").read_text())
    resolved = {(MANIFESTS / rel / c).resolve() for c in doc["components"]}
    assert resolved == {(MANIFESTS / "config").resolve()}, doc["components"]
    assert not (MANIFESTS / rel / "config").is_symlink()


# ----------------------------------------------------------------------------- (b) env keys
NEW_KEYS = ("RUNTIME_IMAGE", "MODEL_ID", "MODEL_REVISION")


def test_no_old_qwen38_key_remains_anywhere() -> None:
    """docs/sdd/* is the append-only history and is exempt; QWEN38_TREE / QWEN38_CACHE_REPO are script constants."""
    hits = {rel: sorted(set(OLD_KEY_RE.findall(_read(rel)))) for rel in _text_files() if OLD_KEY_RE.search(_read(rel))}
    assert not hits, f"{OLD_KEYS} became RUNTIME_IMAGE / MODEL_ID / MODEL_REVISION, stale in: {hits}"


def test_config_example_carries_the_new_keys_and_the_cluster_name() -> None:
    cfg = dict(
        ln.split("=", 1)
        for ln in _read("manifests/config/platform.env.example").splitlines()
        if ln.strip() and not ln.startswith("#")
    )
    assert set(NEW_KEYS) | {"CLUSTER_NAME"} <= set(cfg), sorted(cfg)
    assert not [k for k in cfg if k.startswith("QWEN38_")]
    assert cfg["MODEL_ID"] == "Qwen/Qwen3.8-27B-FP8"
    assert re.fullmatch(r"[0-9a-f]{40}", cfg["MODEL_REVISION"])
    assert re.fullmatch(r".+@sha256:[0-9a-f]{64}", cfg["RUNTIME_IMAGE"])
    # The sample runs the public NGC image; the stack creates the sglang-runtime repository, the optional ECR mirror
    # target, only with create_runtime_mirror_repository = true, so the default config leaves the key empty.
    assert cfg["SGLANG_RUNTIME_REPO"] == ""
    assert cfg["RUNTIME_IMAGE"].startswith("nvcr.io/nvidia/ai-dynamo/sglang-runtime:")
    assert cfg["CLUSTER_NAME"]


def test_replacements_read_the_new_image_key() -> None:
    for rel in ("track-q-qwen38/base/kustomization.yaml", "track-q-qwen38/frontend/kustomization.yaml"):
        text = (MANIFESTS / rel).read_text()
        assert "fieldPath: data.RUNTIME_IMAGE" in text, rel


def _walk_images(node: Any) -> list[str]:
    if isinstance(node, dict):
        return [v for k, v in node.items() if k == "image" and isinstance(v, str)] + [
            i for v in node.values() for i in _walk_images(v)
        ]
    if isinstance(node, list):
        return [i for v in node for i in _walk_images(v)]
    return []


def _kustomize(rel: str) -> str:
    proc = subprocess.run(
        ["kubectl", "kustomize", str(MANIFESTS / rel)], capture_output=True, text=True, check=False, timeout=120
    )
    assert proc.returncode == 0, f"kubectl kustomize manifests/{rel} failed:\n{proc.stderr.strip()}"
    return proc.stdout


def _example_cfg() -> dict[str, str]:
    return dict(
        ln.split("=", 1)
        for ln in _read("manifests/config/platform.env.example").splitlines()
        if ln.strip() and not ln.startswith("#")
    )


# ----------------------------------------------------------------------------- the kept overlays still build
KEPT_OVERLAYS = [
    *(f"track-q-qwen38/cells/S{n}" for n in range(8)),
    *(f"track-q-qwen38/overlays/{t}" for t in ("t-agg", "t-agg-hic", "t-dis", "t-dis-hic")),
    "track-q-qwen38/frontends/rr",
    "track-q-qwen38/frontends/kv",
    "bench",
    "node-ops",
    "node-ops/prepull",
    "stage-mocker",
    "demo/rerender",
]


@pytest.mark.parametrize("rel", KEPT_OVERLAYS)
def test_kept_overlay_builds_without_leaking_the_config_map(rel: str) -> None:
    out = _kustomize(rel)
    docs = [d for d in yaml.safe_load_all(out) if d]
    assert docs, rel
    assert not [d for d in docs if d.get("kind") == "ConfigMap" and "platform" in d["metadata"]["name"]]


@pytest.mark.parametrize("n", range(8))
def test_track_q_cell_carries_the_configured_runtime_image(n: int) -> None:
    images = {i for d in yaml.safe_load_all(_kustomize(f"track-q-qwen38/cells/S{n}")) if d for i in _walk_images(d)}
    assert images == {_example_cfg()["RUNTIME_IMAGE"]}, images


def test_bench_renders_the_configured_registry_not_the_old_region() -> None:
    """Replaces golden_other_renders: bench used to flip silently Oregon -> Mumbai when 'config' changed meaning."""
    images = _walk_images(list(yaml.safe_load_all(_kustomize("bench"))))
    assert len(images) == 1, images
    assert images[0].startswith(_example_cfg()["BENCH_REPO"] + "@sha256:"), images[0]


def test_node_ops_prefetch_renders_the_configured_bucket_and_region() -> None:
    cfg = _example_cfg()
    jobs = [d for d in yaml.safe_load_all(_kustomize("node-ops")) if d and d["kind"] == "Job" and d["metadata"]["name"] == "model-prefetch"]
    assert len(jobs) == 1
    env = {e["name"]: e.get("value") for c in jobs[0]["spec"]["template"]["spec"]["containers"] for e in c.get("env", [])}
    assert env["AWS_REGION"] == cfg["AWS_REGION"]
    assert env["MODEL_S3_URI"].startswith(f"s3://{cfg['MODEL_BUCKET']}/"), env["MODEL_S3_URI"]


# ----------------------------------------------------------------------------- scripts and driver follow
def _load(path: Path, name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod  # @dataclass resolves string annotations through sys.modules[cls.__module__]
    spec.loader.exec_module(mod)
    return mod


def test_matrix_driver_reads_the_renamed_config_and_keys() -> None:
    rd = _load(REPO / "tools" / "matrix_driver" / "render_driver.py", "render_driver_b")
    assert rd.PLATFORM_ENV == MANIFESTS / "config" / "platform.env"
    env = {
        "BENCH_REPO": "r/bench", "RESULTS_BUCKET": "bkt", "AWS_REGION": "ap-south-1", "RUNTIME_IMAGE": "r/rt@sha256:" + "a" * 64,
    }
    images = {
        "MATRIX_BENCH_TAG": "t", "MATRIX_BENCH_DIGEST": "sha256:" + "b" * 64,
        "MATRIX_FFMPEG_TAG": "t", "MATRIX_FFMPEG_DIGEST": "sha256:" + "c" * 64,
    }
    assert rd.platform(env, images).cleanup_image == env["RUNTIME_IMAGE"]
