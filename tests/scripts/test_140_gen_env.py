# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Tests for ``scripts/gen_env.py``: Terraform outputs -> the generated platform.env.

Contract:

* Input: ``terraform output -json`` of ``terraform/cluster`` (``--from-json FILE``, or, without it, the script
  runs ``terraform -chdir=<repo>/terraform/cluster output -json``). Outputs used: ``platform_env`` and
  ``sample_env`` (map(string), not sensitive) and ``gpu_pools`` (map of pool name -> facts).
* Output: ONE file, ``<repo>/manifests/config/platform.env`` (the file the scripts read: scripts/lib/common.sh and the
  staging script; gitignored, the committed copy is ``platform.env.example``).
  ``--platform-out`` / ``--repo-root`` override the paths; ``--sample-out`` no longer exists (argparse refuses it, exit 2).
  Plain ``KEY=VALUE`` lines (no quotes, spaces, ``$``), first line a ``# GENERATED`` header. A refusal exits
  non-zero, writes NOTHING and never echoes output values.
* platform.env also carries what the scripts read from ``sample_env``: MODEL_ID / MODEL_REVISION / RUNTIME_IMAGE,
  and HF_TOKEN_SECRET_ARN when Terraform sets it (var.hf_token_secret_arn), absent otherwise.
* Tested-shape check (the manifests hard-code ``karpenter.sh/nodepool: gpu-b200-cb`` and 8 GPUs per node): the
  script refuses unless there is exactly ONE pool, named ``gpu-b200-cb``, with GPUS_PER_NODE=8, unless
  ``--allow-untested`` is given; then it warns on stderr ("manifests untested") and adds
  ``GPU_SHAPE_SUPPORTED=terraform-only``. With several pools only ``GPU_POOLS`` is kept; the single-value
  keys (GPU_NODEPOOL, GPU_PRODUCT, GPUS_PER_NODE, EFA_INTERFACES_PER_NODE, GPU_MEMORY_GIB_PER_GPU,
  HOST_MEMORY_GIB, EPHEMERAL_MIN_GIB, GPU_INSTANCE_TYPES, GPU_AZ) are dropped because they would depend on the
  order of the user's list.
* Always refused (even with ``--allow-untested``): empty RUNTIME_IMAGE (RUNTIME_IMAGE would render an invalid
  image), unsafe values or keys, a missing or sensitive output.
* MODEL_ID other than ``Qwen/Qwen3.8-27B-FP8`` is refused without ``--allow-untested``
  (the example manifests serve that model).
"""

from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path

import lib_proc
import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "gen_env.py"
ACCT = "1" * 12  # built at runtime: no account-id literal in the repo
REGION = "us-east-2"
DIGEST = "sha256:" + "a" * 64
SINGLE_VALUE_KEYS = {
    "GPU_NODEPOOL",
    "GPU_PRODUCT",
    "GPUS_PER_NODE",
    "EFA_INTERFACES_PER_NODE",
    "GPU_MEMORY_GIB_PER_GPU",
    "HOST_MEMORY_GIB",
    "EPHEMERAL_MIN_GIB",
    "GPU_INSTANCE_TYPES",
    "GPU_AZ",
}


@pytest.fixture(autouse=True)
def _script_present() -> None:
    """Refusal tests must not pass vacuously because the script is missing."""
    assert SCRIPT.is_file(), "scripts/gen_env.py must exist"


def platform_env(**over: str) -> dict[str, str]:
    """Return the platform_env output of the tested shape (p6-b200 capacity block)."""
    env = {
        "AWS_ACCOUNT_ID": ACCT,
        "AWS_REGION": REGION,
        "CLUSTER_NAME": "demo-eks",
        "MODEL_BUCKET": f"demo-models-{ACCT}-use2",
        "RESULTS_BUCKET": f"demo-results-{ACCT}-use2",
        "GPU_NODEPOOL": "gpu-b200-cb",
        "GPU_POOLS": "gpu-b200-cb:8:8",  # terraform/cluster/outputs.tf always writes it (read by 90-teardown.sh)
        "GPU_INSTANCE_TYPES": "p6-b200.48xlarge",
        "GPU_PRODUCT": "NVIDIA-B200",
        "GPU_CAPACITY_TYPE": "capacity-block",
        "GPUS_PER_NODE": "8",
        "EFA_INTERFACES_PER_NODE": "8",
        "GPU_MEMORY_GIB_PER_GPU": "179",
        "HOST_MEMORY_GIB": "2048",
        "GPU_AZ": "us-east-2c",
        "PRIMARY_AZ": "us-east-2c",
        "EPHEMERAL_MIN_GIB": "28312",
        "GRAFANA_EDGE_ENABLED": "false",
    }
    env.update(over)
    return env


def sample_env(**over: str) -> dict[str, str]:
    """Return the sample_env output (model settings)."""
    env = {
        "MODEL_ID": "Qwen/Qwen3.8-27B-FP8",
        "MODEL_REVISION": "0123456789abcdef0123456789abcdef01234567",
        "RUNTIME_IMAGE": f"nvcr.io/nvidia/ai-dynamo/sglang-runtime@{DIGEST}",
    }
    env.update(over)
    return env


def tf_output(
    plat: dict[str, str] | None = None,
    samp: dict[str, str] | None = None,
    pools: dict | None = None,
    *,
    sensitive: bool = False,
) -> dict:
    """Return the JSON ``terraform output -json`` would print."""
    pools = pools if pools is not None else {"gpu-b200-cb": {"gpus_per_node": 8, "efa_interfaces": 8}}
    return {
        "platform_env": {"sensitive": sensitive, "type": ["map", "string"], "value": plat or platform_env()},
        "sample_env": {"sensitive": False, "type": ["map", "string"], "value": samp or sample_env()},
        "gpu_pools": {"sensitive": False, "type": "dynamic", "value": pools},
    }


def run(
    tmp_path: Path, out: dict, *extra: str, name: str = "tf.json"
) -> tuple[subprocess.CompletedProcess[str], Path]:
    """Run gen_env.py against ``out`` with an isolated repo root; return (proc, root)."""
    root = tmp_path / "repo"
    root.mkdir(exist_ok=True)
    src = tmp_path / name
    src.write_text(json.dumps(out))
    proc = lib_proc.run(
        [sys.executable, "-I", str(SCRIPT), "--from-json", str(src), "--repo-root", str(root), *extra],
        capture_output=True,
        text=True,
        check=False,
    )
    return proc, root


def parse_env(path: Path) -> dict[str, str]:
    """Parse a generated env file the way scripts/lib/common.sh does (KEY=VALUE, # comments)."""
    res: dict[str, str] = {}
    for line in path.read_text().splitlines():
        if not line or line.startswith("#"):
            continue
        key, _, val = line.partition("=")
        res[key] = val
    return res


def platform_file(root: Path) -> Path:
    """Default path of the one generated file under ``root``."""
    return root / "manifests" / "config" / "platform.env"


def files_under(root: Path) -> list[Path]:
    """Every file below ``root``: what a run wrote (a refusal must leave this empty)."""
    return sorted(p for p in root.rglob("*") if p.is_file())


def test_script_exists() -> None:
    assert SCRIPT.is_file(), "scripts/gen_env.py must exist"


def test_happy_path_writes_only_platform_env(tmp_path: Path) -> None:
    proc, root = run(tmp_path, tf_output())
    assert proc.returncode == 0, proc.stderr
    plat_f = platform_file(root)
    assert files_under(root) == [plat_f], "one generated file; no config/sample.env"
    assert not (root / "config").exists()
    plat = parse_env(plat_f)
    for k, v in {**platform_env(), **sample_env()}.items():
        assert plat[k] == v, k
    assert plat_f.read_text().splitlines()[0].startswith("# GENERATED")


def test_sample_out_is_gone_and_refused_with_a_usage_error(tmp_path: Path) -> None:
    proc, root = run(tmp_path, tf_output(), "--sample-out", str(tmp_path / "y" / "sample.env"))
    assert proc.returncode == 2, proc.stderr
    assert "--sample-out" in proc.stderr
    assert files_under(root) == [] and not (tmp_path / "y").exists(), "a usage error writes nothing"


def test_hf_token_secret_arn_lands_in_platform_env_when_terraform_sets_it(tmp_path: Path) -> None:
    arn = "arn:aws:secretsmanager:" + REGION + ":" + ACCT + ":secret:hf-token-AbCdEf"
    proc, root = run(tmp_path, tf_output(None, sample_env(HF_TOKEN_SECRET_ARN=arn)))
    assert proc.returncode == 0, proc.stderr
    plat = parse_env(platform_file(root))
    assert plat["HF_TOKEN_SECRET_ARN"] == arn
    assert "HF_TOKEN" not in plat, "the reference lands in the file, never a token"
    assert files_under(root) == [platform_file(root)]


def test_hf_token_secret_arn_is_absent_when_terraform_does_not_set_it(tmp_path: Path) -> None:
    proc, root = run(tmp_path, tf_output())
    assert proc.returncode == 0, proc.stderr
    assert "HF_TOKEN_SECRET_ARN" not in platform_file(root).read_text(), "not even as an empty key"


def test_unexpected_sample_env_keys_do_not_reach_platform_env(tmp_path: Path) -> None:
    """Only the model keys of sample_env are copied: a stray key is left out and never overwrites a platform_env key."""
    samp = sample_env(UNEXPECTED_KEY="x", CLUSTER_NAME="from-sample-env")
    proc, root = run(tmp_path, tf_output(None, samp))
    assert proc.returncode == 0, proc.stderr
    plat = parse_env(platform_file(root))
    assert "UNEXPECTED_KEY" not in plat
    assert plat["CLUSTER_NAME"] == platform_env()["CLUSTER_NAME"]


def test_default_config_stays_byte_identical_to_the_committed_example(tmp_path: Path) -> None:
    """Unifying the files changes nothing for the default config: the generated body is the example's body.

    The example is split back into the three Terraform outputs the way terraform/cluster/outputs.tf splits them
    (model keys in sample_env, the rest in platform_env); the example's own header comments are not generated.
    """
    example = (REPO / "manifests" / "config" / "platform.env.example").read_text().splitlines()
    pairs = [ln.split("=", 1) for ln in example if ln and not ln.startswith("#")]
    sample_keys = {"MODEL_ID", "MODEL_REVISION", "MODEL_S3_PREFIX", "RUNTIME_IMAGE"}
    samp = {k: v for k, v in pairs if k in sample_keys}
    plat = {k: v for k, v in pairs if k not in sample_keys | {"MODEL_S3_URI"}}
    proc, root = run(tmp_path, tf_output(plat, samp))
    assert proc.returncode == 0, proc.stderr
    body = platform_file(root).read_text().splitlines()[1:]
    assert body == [ln for ln in example if ln and not ln.startswith("#")]


def test_qwen38_aliases_derive_from_sample_env(tmp_path: Path) -> None:
    proc, root = run(tmp_path, tf_output())
    assert proc.returncode == 0, proc.stderr
    plat = parse_env(platform_file(root))
    s = sample_env()
    assert plat["MODEL_ID"] == s["MODEL_ID"]
    assert plat["MODEL_REVISION"] == s["MODEL_REVISION"]
    assert plat["RUNTIME_IMAGE"] == s["RUNTIME_IMAGE"]


def test_values_have_no_quotes_spaces_or_dollar(tmp_path: Path) -> None:
    proc, root = run(tmp_path, tf_output())
    assert proc.returncode == 0, proc.stderr
    for line in platform_file(root).read_text().splitlines():
        if line and not line.startswith("#"):
            assert re.fullmatch(r"[A-Z][A-Z0-9_]*=[^\s\"'$]*", line), line


def test_idempotent_two_runs_byte_identical(tmp_path: Path) -> None:
    proc, root = run(tmp_path, tf_output())
    assert proc.returncode == 0, proc.stderr
    first = platform_file(root).read_bytes()
    proc2, _ = run(tmp_path, tf_output())
    assert proc2.returncode == 0, proc2.stderr
    assert platform_file(root).read_bytes() == first


def test_explicit_output_paths(tmp_path: Path) -> None:
    out = tmp_path / "x" / "platform.env"
    proc, root = run(tmp_path, tf_output(), "--platform-out", str(out))
    assert proc.returncode == 0, proc.stderr
    assert out.is_file() and files_under(root) == [], "the file goes where --platform-out says, and nowhere else"


def consumer_keys() -> set[str]:
    """Keys the kustomizations of manifests/ and the scripts read from platform.env."""
    keys: set[str] = set()
    for kf in (REPO / "manifests").rglob("kustomization.yaml"):
        keys |= set(re.findall(r"fieldPath:\s*data\.([A-Z][A-Z0-9_]*)", kf.read_text()))
    for sh in [REPO / "scripts" / "lib" / "common.sh", *(REPO / "scripts").glob("*.sh")]:
        keys |= set(re.findall(r"CFG_([A-Z][A-Z0-9_]*)", sh.read_text()))
    return keys


def test_consumer_key_derivation_is_not_vacuous() -> None:
    assert {"AWS_REGION", "MODEL_BUCKET", "MODEL_ID", "MODEL_REVISION"} <= consumer_keys()


def test_generated_keys_cover_every_key_the_manifests_and_scripts_read(tmp_path: Path) -> None:
    proc, root = run(tmp_path, tf_output())
    assert proc.returncode == 0, proc.stderr
    missing = consumer_keys() - set(parse_env(platform_file(root)))
    assert not missing, f"platform.env lacks keys read by manifests/scripts: {sorted(missing)}"


def test_premise_manifests_hardcode_the_pool_name() -> None:
    hits = [
        p
        for p in (REPO / "examples").glob("*/manifest.yaml")
        if "karpenter.sh/nodepool: gpu-b200-cb" in p.read_text()
    ]
    assert hits, "premise of the tested-shape check: manifests select the gpu-b200-cb pool by name"


@pytest.mark.parametrize(
    ("plat", "pools"),
    [
        (platform_env(GPU_NODEPOOL="gpu-b200-od"), {"gpu-b200-od": {}}),
        (platform_env(GPU_NODEPOOL="gpu-h100-od", GPUS_PER_NODE="8"), {"gpu-h100-od": {}}),
        (platform_env(GPUS_PER_NODE="4"), {"gpu-b200-cb": {}}),
    ],
)
def test_untested_shape_refused_without_flag(tmp_path: Path, plat: dict[str, str], pools: dict) -> None:
    proc, root = run(tmp_path, tf_output(plat, None, pools))
    assert proc.returncode != 0
    assert "--allow-untested" in proc.stderr
    assert files_under(root) == [], "a refusal must write nothing"


def test_untested_shape_allowed_with_flag_warns_and_marks(tmp_path: Path) -> None:
    plat = platform_env(GPU_NODEPOOL="gpu-h100-od", GPU_PRODUCT="NVIDIA-H100")
    proc, root = run(tmp_path, tf_output(plat, None, {"gpu-h100-od": {}}), "--allow-untested")
    assert proc.returncode == 0, proc.stderr
    assert "manifests untested" in proc.stderr
    assert parse_env(platform_file(root))["GPU_SHAPE_SUPPORTED"] == "terraform-only"


def multi_pool_output() -> dict:
    plat = platform_env(GPU_POOLS="gpu-b200-od:8:8,gpu-l40s-od:1:0")  # carries single-value keys on purpose
    return tf_output(plat, None, {"gpu-b200-od": {}, "gpu-l40s-od": {}})


def test_multiple_pools_refused_without_flag(tmp_path: Path) -> None:
    proc, root = run(tmp_path, multi_pool_output())
    assert proc.returncode != 0
    assert "--allow-untested" in proc.stderr
    assert files_under(root) == []


def test_multiple_pools_drop_order_dependent_keys(tmp_path: Path) -> None:
    proc, root = run(tmp_path, multi_pool_output(), "--allow-untested")
    assert proc.returncode == 0, proc.stderr
    plat = parse_env(platform_file(root))
    assert plat["GPU_POOLS"] == "gpu-b200-od:8:8,gpu-l40s-od:1:0"
    assert not (SINGLE_VALUE_KEYS & set(plat)), sorted(SINGLE_VALUE_KEYS & set(plat))


def test_empty_runtime_image_always_refused(tmp_path: Path) -> None:
    for extra in ((), ("--allow-untested",)):
        proc, root = run(tmp_path, tf_output(None, sample_env(RUNTIME_IMAGE="")), *extra)
        assert proc.returncode != 0
        assert "RUNTIME_IMAGE" in proc.stderr
        assert files_under(root) == []


def test_other_model_refused_without_flag(tmp_path: Path) -> None:
    samp = sample_env(MODEL_ID="Org/Other-Model")
    proc, root = run(tmp_path, tf_output(None, samp))
    assert proc.returncode != 0
    assert "--allow-untested" in proc.stderr
    assert files_under(root) == []
    second = tmp_path / "again"
    second.mkdir()
    proc2, root2 = run(second, tf_output(None, samp), "--allow-untested")
    assert proc2.returncode == 0, proc2.stderr
    assert "manifests untested" in proc2.stderr
    assert platform_file(root2).is_file()


@pytest.mark.parametrize("bad", ["has space", 'has"quote', "has'quote", "has$var", "a b$c"])
def test_unsafe_values_refused(tmp_path: Path, bad: str) -> None:
    proc, root = run(tmp_path, tf_output(platform_env(CLUSTER_NAME=bad)))
    assert proc.returncode != 0
    assert files_under(root) == []
    assert bad not in proc.stdout, "values must not be echoed"


@pytest.mark.parametrize("bad_key", ["lower", "1BAD", "HAS-DASH", "HAS SPACE"])
def test_unsafe_keys_refused(tmp_path: Path, bad_key: str) -> None:
    plat = platform_env()
    plat[bad_key] = "x"
    proc, root = run(tmp_path, tf_output(plat))
    assert proc.returncode != 0
    assert files_under(root) == []


def test_missing_output_refused(tmp_path: Path) -> None:
    out = tf_output()
    del out["sample_env"]
    proc, root = run(tmp_path, out)
    assert proc.returncode != 0
    assert "sample_env" in proc.stderr
    assert files_under(root) == []


def test_outputs_of_a_failed_apply_name_the_incomplete_apply_and_every_missing_output(tmp_path: Path) -> None:
    """A cluster apply that failed half-way leaves most outputs in the state but not platform_env (found on a real
    cluster: 27 of 29). "apply terraform/cluster first" is wrong there: the apply ran. The refusal says the last apply did not finish,
    to read its errors, and names every missing required output (only names, never a value)."""
    out = tf_output()
    del out["platform_env"], out["gpu_pools"]
    out["cluster_name"] = {"sensitive": False, "type": "string", "value": "do-not-print-me"}
    proc, root = run(tmp_path, out)
    assert proc.returncode != 0
    err = proc.stderr
    assert "platform_env" in err and "gpu_pools" in err, err
    assert not re.search(r"(?i)apply terraform/cluster first", err), err
    assert re.search(r"(?i)last terraform apply", err) and re.search(r"(?i)fail|did not finish|stopped", err), err
    assert re.search(r"(?i)errors?", err), err
    assert "do-not-print-me" not in proc.stdout + err
    assert files_under(root) == []


def test_no_outputs_at_all_means_no_apply_yet(tmp_path: Path) -> None:
    """An empty ``terraform output -json`` ({}): terraform/cluster was never applied (or that is not its state)."""
    proc, root = run(tmp_path, {})
    assert proc.returncode != 0
    assert re.search(r"(?i)no outputs", proc.stderr) and re.search(r"(?i)apply", proc.stderr), proc.stderr
    assert not re.search(r"(?i)fail", proc.stderr), "nothing says that an apply failed"
    assert files_under(root) == []


def test_sensitive_output_refused_without_echo(tmp_path: Path) -> None:
    sensitive_value = "do-not-print-me"
    proc, root = run(tmp_path, tf_output(platform_env(CLUSTER_NAME=sensitive_value), sensitive=True))
    assert proc.returncode != 0
    assert sensitive_value not in proc.stdout + proc.stderr
    assert files_under(root) == []


def test_missing_input_file_fails(tmp_path: Path) -> None:
    proc = lib_proc.run(
        [sys.executable, "-I", str(SCRIPT), "--from-json", str(tmp_path / "nope.json"), "--repo-root", str(tmp_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode != 0


def test_without_from_json_it_calls_terraform_output_json(tmp_path: Path) -> None:
    """No --from-json: run ``terraform -chdir=<repo>/terraform/cluster output -json`` (fake binary on PATH)."""
    root = tmp_path / "repo"
    root.mkdir()
    payload = tmp_path / "payload.json"
    payload.write_text(json.dumps(tf_output()))
    argv_log = tmp_path / "argv.txt"
    fake = tmp_path / "bin" / "terraform"
    fake.parent.mkdir()
    fake.write_text(f'#!/bin/sh\nprintf "%s\\n" "$@" > "{argv_log}"\ncat "{payload}"\n')
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    env = {**os.environ, "PATH": f"{fake.parent}{os.pathsep}{os.environ['PATH']}"}
    proc = lib_proc.run(
        [sys.executable, "-I", str(SCRIPT), "--repo-root", str(root)],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    assert proc.returncode == 0, proc.stderr
    argv = argv_log.read_text().split()
    assert f"-chdir={root / 'terraform' / 'cluster'}" in argv
    assert argv[-2:] == ["output", "-json"]
    assert platform_file(root).is_file()
