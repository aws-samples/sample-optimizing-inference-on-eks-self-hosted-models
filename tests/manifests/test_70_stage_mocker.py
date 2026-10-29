# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Stage mocker toggle demo (spec O-11, 9.1, 12.2; run of show 3:00-5:00).  RED until implemented.

Contract for ``manifests/stage-mocker/`` (CPU only, labelled "simulation"):

    manifests/stage-mocker/              renders the initial (round-robin) state == overlays/rr
    manifests/stage-mocker/overlays/rr   exactly 1 DGD, DYN_ROUTER_MODE=round-robin on the Frontend
    manifests/stage-mocker/overlays/kv   identical except DYN_ROUTER_MODE=kv

The on-stage step is ``kubectl diff`` / ``kubectl apply`` rr -> kv, so the two overlays may differ
**only** in that one env var.  Every flag pinned here was verified against Dynamo ``v1.5.1``
(``git show v1.5.1:<path>`` in the Dynamo repo):

* ``components/src/dynamo/mocker/args.py``: ``--model-path`` (174-178), ``--num-gpu-blocks-override``
  (193-201), ``--engine-type`` choices vllm|sglang|trtllm (428-436), ``--disaggregation-mode``
  agg|prefill|decode (494-501), ``--is-decode-worker`` "does not publish KV events" (509-515),
  ``--kv-bytes-per-token`` "auto-computed ... overrides" (575-581), ``--no-enable-prefix-caching``
  (235-241).
* ``components/src/dynamo/mocker/main.py:157-160``: a worker that advertises a router config
  (``--router-mode`` on the mocker) overrides the frontend's mode for its worker set, so mocker
  workers must NOT set a router mode: the rr -> kv toggle lives on the Frontend only.
* ``components/src/dynamo/common/configuration/groups/kv_router_args.py:403-415``:
  ``--router-kv-events`` / ``DYN_ROUTER_USE_KV_EVENTS`` default **True** (router receives cache
  state events from workers); ``--no-router-kv-events`` would make ``kv`` routing predictive only.
* ``docs/fern/pages/kubernetes/operations/simulation-with-dynosim/mocker-live-simulation.mdx``:
  mocker "registers workers, publishes KV events"; example DGD uses ``python3 -m dynamo.mocker``
  with ``--model-path Qwen/Qwen3-0.6B`` (public, anonymous HF tokenizer, no secret).
* ``container/templates/sglang_runtime.Dockerfile:111-126`` + ``pyproject.toml`` (wheel
  ``packages = ["components/src/dynamo"]``): the ``sglang-runtime`` image installs the ``ai-dynamo``
  wheel, hence ``dynamo.mocker`` (and the ``aisimulate`` wheel).  Assumption to confirm in the real
  image: ``python3 -m dynamo.mocker --help`` (add it to the preflight).
* ``container/templates/sglang_runtime.Dockerfile:64-65,503``: runtime user ``dynamo`` is uid 1000,
  gid 0 and the image uses ``USER dynamo`` (a *name*), so ``runAsNonRoot`` needs an explicit numeric
  ``runAsUser`` or the kubelet refuses to start the container.

Model/tokenizer decision: ``--model-path Qwen/Qwen3-0.6B`` pulled anonymously from the Hugging Face
Hub (not gated, ~no weights are needed by the mocker, only the tokenizer).  Rationale: it is the
documented v1.5.1 example, needs no secret and no S3 copy.  Consequence: ``HF_HUB_OFFLINE`` must not
be 1 in these pods and the cluster needs HF egress.  Explicit ``--kv-bytes-per-token`` is
mandatory because the auto path derives ``head_dim`` from hidden/heads (64 here, real 128).

FT-4 pins (real Paris cluster): workers pass ``--block-size 64`` (declared in ``mocker/args.py:202-208``);
memory requests are sized for one c7i-flex.xlarge so a surge frontend fits next to the old one
(worker <= 768Mi, frontend <= 512Mi, limits >= 2x requests; measured ~470 MiB / ~270 MiB).

PSA: spec 7 assigns ``stage-mocker`` ``baseline`` (warn ``restricted``).  Baseline is enforced
(no privileged/hostPath/hostNetwork/added capabilities/hostPort) and the restricted fields are
required too so ``kubectl apply`` on stage prints no PSA warning.
"""

from __future__ import annotations

import functools
import re
import subprocess
from typing import Any

import pytest

import lib_manifests as L

NS = "stage-mocker"
DEFAULT = "stage-mocker"
RR = "stage-mocker/overlays/rr"
KV = "stage-mocker/overlays/kv"
ALL = (DEFAULT, RR, KV)
MODEL = "Qwen/Qwen3-0.6B"
# Qwen3-0.6B config.json: 28 layers, 8 KV heads, head_dim 128, bf16 -> 28*2*8*128*2
KV_BYTES_PER_TOKEN = 28 * 2 * 8 * 128 * 2
SIMULATION_KEY = "demo.kubecon/simulation"
FALSY = {"false", "0", "no", "off"}


# ----------------------------------------------------------------------------- helpers
def _git_show(path: str) -> str:
    L.require_dynamo_repo()
    out = subprocess.run(
        ["git", "-C", str(L.DYNAMO_REPO), "show", f"v1.5.1:{path}"],
        capture_output=True, text=True, check=False,
    )
    assert out.returncode == 0, f"cannot read v1.5.1:{path} from {L.DYNAMO_REPO}: {out.stderr}"
    return out.stdout


@functools.lru_cache(maxsize=None)
def _v151_worker_flags() -> frozenset[str]:
    """All ``--flag`` literals declared by the v1.5.1 mocker args and shared config groups."""
    L.require_dynamo_repo()
    files = ["components/src/dynamo/mocker/args.py"]
    ls = subprocess.run(
        ["git", "-C", str(L.DYNAMO_REPO), "ls-tree", "--name-only", "v1.5.1",
         "components/src/dynamo/common/configuration/groups/"],
        capture_output=True, text=True, check=False,
    )
    assert ls.returncode == 0, ls.stderr
    files += [f for f in ls.stdout.split() if f.endswith(".py")]
    flags: set[str] = set()
    for f in files:
        flags |= set(re.findall(r"""["'](--[a-z0-9][a-z0-9-]*)["']""", _git_show(f)))
    assert "--num-gpu-blocks-override" in flags and "--kv-bytes-per-token" in flags
    return frozenset(flags)


def _flag_exists(flag: str) -> bool:
    known = _v151_worker_flags()
    return flag in known or (flag.startswith("--no-") and "--" + flag[5:] in known)


def _quantity(q: Any) -> float:
    m = re.fullmatch(r"([0-9]*\.?[0-9]+)(m|Ki|Mi|Gi|Ti|k|M|G|T)?", str(q))
    assert m, f"unparseable quantity {q!r}"
    mult = {None: 1, "m": 1e-3, "Ki": 2**10, "Mi": 2**20, "Gi": 2**30, "Ti": 2**40,
            "k": 1e3, "M": 1e6, "G": 1e9, "T": 1e12}[m.group(2)]
    return float(m.group(1)) * mult


def _dgd(rel: str) -> dict[str, Any]:
    return L.one_dgd(rel)


def _frontend(dgd: dict[str, Any]) -> dict[str, Any]:
    fronts = L.components_of_type(dgd, "frontend")
    assert len(fronts) == 1, f"expected exactly one frontend component, got {len(fronts)}"
    return fronts[0]


def _mockers(dgd: dict[str, Any]) -> list[dict[str, Any]]:
    found = L.components_of_type(dgd, "worker")
    assert found, "no mocker worker component (type: worker)"
    return found


def _all_containers(ps: dict[str, Any]) -> list[dict[str, Any]]:
    return list(ps.get("containers", []) or []) + list(ps.get("initContainers", []) or [])


def _worker_flags(comp: dict[str, Any]) -> dict[str, list[str]]:
    return L.parse_cli_flags(L.cli_tokens(L.main_container(comp)))


# ----------------------------------------------------------------------------- structure
@pytest.mark.parametrize("rel", ALL)
def test_renders_one_valid_dgd_in_stage_mocker_namespace(rel: str) -> None:
    """One v1beta1 DGD, valid against the v1.5.1 CRD, every namespaced object in `stage-mocker`."""
    docs = L.render(rel)
    dgd = _dgd(rel)
    assert dgd["apiVersion"] == "nvidia.com/v1beta1"
    assert L.schema_errors(dgd) == []
    for d in docs:
        if d["kind"] != "Namespace":
            assert d["metadata"].get("namespace") == NS, f"{d['kind']}/{d['metadata']['name']}"


@pytest.mark.parametrize("rel", ALL)
def test_frontend_plus_at_least_two_mocker_workers(rel: str) -> None:
    """A Frontend and mocker `worker` components with >= 2 replicas (routing needs a choice)."""
    dgd = _dgd(rel)
    assert _frontend(dgd).get("replicas", 1) >= 1
    assert sum(int(c.get("replicas", 1)) for c in _mockers(dgd)) >= 2
    assert {c["type"] for c in L.components(dgd)} <= {"frontend", "worker"}


@pytest.mark.parametrize("rel", ALL)
def test_simulation_label_on_dgd(rel: str) -> None:
    """The DGD is labelled/annotated `demo.kubecon/simulation: "true"` (talk labels it simulation)."""
    meta = _dgd(rel)["metadata"]
    vals = [(meta.get("labels") or {}).get(SIMULATION_KEY), (meta.get("annotations") or {}).get(SIMULATION_KEY)]
    assert "true" in vals, f"{SIMULATION_KEY}='true' missing in labels/annotations: {meta}"


# ----------------------------------------------------------------------------- CPU only
@pytest.mark.parametrize("rel", ALL)
def test_every_component_on_cpu_demo_pool(rel: str) -> None:
    for c in L.components(_dgd(rel)):
        assert L.pod_spec(c).get("nodeSelector", {}).get("karpenter.sh/nodepool") == "cpu-demo", c["name"]


@pytest.mark.parametrize("rel", ALL)
def test_no_gpu_requests_tolerations_or_runtime_class(rel: str) -> None:
    text = L.render_text(rel)
    assert "nvidia.com/gpu" not in text, "GPU resource request or toleration found"
    assert "gpu-b200" not in text
    assert "runtimeClassName" not in text


@pytest.mark.parametrize("rel", ALL)
def test_no_hf_token_or_secret_refs(rel: str) -> None:
    text = L.render_text(rel)
    for forbidden in ("secretRef", "secretKeyRef", "HF_TOKEN", "HUGGING_FACE_HUB_TOKEN", "hf-token", "kind: Secret"):
        assert forbidden not in text, forbidden
    for doc in L.render(rel):
        assert doc["kind"] != "Secret"
        for _, ps in L.iter_pod_specs([doc]):
            assert not any("secret" in v for v in ps.get("volumes", []) or [])
            for c in _all_containers(ps):
                assert not c.get("envFrom")


@pytest.mark.parametrize("rel", ALL)
def test_hf_hub_not_offline_because_tokenizer_is_fetched_anonymously(rel: str) -> None:
    """Qwen/Qwen3-0.6B tokenizer comes from the public Hub at start-up: HF_HUB_OFFLINE must not be 1."""
    for c in L.components(_dgd(rel)):
        v = L.env_value(L.main_container(c), "HF_HUB_OFFLINE")
        assert v in (None, "", "0"), f"{c['name']}: HF_HUB_OFFLINE={v}"


# ----------------------------------------------------------------------------- image
@pytest.mark.parametrize("rel", ALL)
def test_every_container_uses_pinned_runtime_image(rel: str) -> None:
    """Same digest-pinned sglang-runtime 1.5.1 image everywhere (it ships dynamo.mocker); no dev tag."""
    text = L.render_text(rel)
    assert "1.6.0" not in text and ":latest" not in text
    for c in L.components(_dgd(rel)):
        for cont in _all_containers(L.pod_spec(c)):
            assert cont["image"] == L.PINNED_IMAGE, f"{c['name']}/{cont['name']}: {cont['image']}"


# ----------------------------------------------------------------------------- mocker flags
@pytest.mark.parametrize("rel", ALL)
def test_workers_run_dynamo_mocker_module(rel: str) -> None:
    for c in _mockers(_dgd(rel)):
        toks = L.cli_tokens(L.main_container(c))
        assert "dynamo.mocker" in toks, toks
        assert toks[toks.index("dynamo.mocker") - 1] == "-m", toks
        assert "dynamo.sglang" not in toks and "dynamo.frontend" not in toks


@pytest.mark.parametrize("rel", ALL)
def test_workers_pin_public_tokenizer_model_and_sglang_engine(rel: str) -> None:
    for c in _mockers(_dgd(rel)):
        f = _worker_flags(c)
        assert f.get("model-path") == [MODEL], f
        assert f.get("engine-type") == ["sglang"], f


@pytest.mark.parametrize("rel", ALL)
def test_workers_set_explicit_kv_bytes_per_token_and_gpu_blocks(rel: str) -> None:
    """Explicit sizing (spec 12.1): auto derives head_dim from hidden/heads (64) instead of 128."""
    for c in _mockers(_dgd(rel)):
        f = _worker_flags(c)
        assert f.get("kv-bytes-per-token") == [str(KV_BYTES_PER_TOKEN)], f
        blocks = f.get("num-gpu-blocks-override")
        assert blocks and len(blocks) == 1 and blocks[0].isdigit() and int(blocks[0]) >= 1024, f


@pytest.mark.parametrize("rel", ALL)
def test_every_worker_flag_exists_in_dynamo_v1_5_1(rel: str) -> None:
    """Never pin a flag that v1.5.1 does not declare (args.py + shared router/config groups)."""
    for c in _mockers(_dgd(rel)):
        unknown = sorted(f for f in (f"--{k}" for k in _worker_flags(c)) if not _flag_exists(f))
        assert not unknown, f"{c['name']}: flags not in v1.5.1: {unknown}"


# ----------------------------------------------------------------------------- KV events are real
@pytest.mark.parametrize("rel", ALL)
def test_kv_events_enabled_not_load_only(rel: str) -> None:
    """kv routing must consume real KV events: workers publish (agg, prefix caching on), router subscribes."""
    dgd = _dgd(rel)
    for c in _mockers(dgd):
        f = _worker_flags(c)
        assert f.get("disaggregation-mode", ["agg"]) == ["agg"], f
        for bad in ("is-decode-worker", "is-prefill-worker", "no-enable-prefix-caching"):
            assert bad not in f, f"{c['name']}: --{bad} disables KV event publishing"
    for c in L.components(dgd):
        cont = L.main_container(c)
        assert "no-router-kv-events" not in _worker_flags(c)
        v = L.env_value(cont, "DYN_ROUTER_USE_KV_EVENTS")
        assert v is None or v.lower() not in FALSY, f"{c['name']}: DYN_ROUTER_USE_KV_EVENTS={v}"


@pytest.mark.parametrize("rel", ALL)
def test_router_mode_only_on_frontend_env(rel: str) -> None:
    """The toggle is `DYN_ROUTER_MODE` on the Frontend; mocker workers must not advertise a mode
    (main.py:157-160 would override the frontend), and a CLI `--router-mode` would beat the env var."""
    dgd = _dgd(rel)
    front = L.main_container(_frontend(dgd))
    assert L.env_value(front, "DYN_ROUTER_MODE") is not None
    assert "router-mode" not in L.parse_cli_flags(L.cli_tokens(front))
    names = [e["name"] for e in front.get("env", []) or []]
    assert names.count("DYN_ROUTER_MODE") == 1
    for c in _mockers(dgd):
        cont = L.main_container(c)
        assert "DYN_ROUTER_MODE" not in L.env_map(cont)
        assert "router-mode" not in _worker_flags(c)


# ----------------------------------------------------------------------------- the toggle
def _mode(rel: str) -> str | None:
    return L.env_value(L.main_container(_frontend(_dgd(rel))), "DYN_ROUTER_MODE")


def test_rr_overlay_is_round_robin_and_kv_overlay_is_kv() -> None:
    assert _mode(RR) == "round-robin"
    assert _mode(KV) == "kv"


def test_overlays_differ_only_in_router_mode_env() -> None:
    """Whole rendered output (all documents) is identical once DYN_ROUTER_MODE's value is masked."""
    rr, kv = L.render(RR), L.render(KV)
    assert L.deep_without_env(rr, "DYN_ROUTER_MODE") == L.deep_without_env(kv, "DYN_ROUTER_MODE")
    assert rr != kv, "overlays render identically: nothing to toggle"


def test_default_render_is_the_rr_starting_state() -> None:
    assert L.render(DEFAULT) == L.render(RR)


def test_same_dgd_identity_across_overlays() -> None:
    a, b = _dgd(RR)["metadata"], _dgd(KV)["metadata"]
    assert (a["name"], a["namespace"]) == (b["name"], b["namespace"])


# ----------------------------------------------------------------------------- PSA
def _effective(ps: dict[str, Any], c: dict[str, Any], key: str) -> Any:
    csc = c.get("securityContext") or {}
    return csc[key] if key in csc else (ps.get("securityContext") or {}).get(key)


@pytest.mark.parametrize("rel", ALL)
def test_psa_baseline_no_privileged_features(rel: str) -> None:
    """Baseline: no hostPath/hostIPC/hostNetwork/hostPID/privileged/added capabilities/hostPort."""
    for label, ps in L.iter_pod_specs(L.render(rel)):
        assert L.privileged_features(ps) == [], label
        for c in _all_containers(ps):
            assert not any(p.get("hostPort") for p in c.get("ports", []) or []), f"{label}/{c['name']}"


@pytest.mark.parametrize("rel", ALL)
def test_psa_restricted_fields_so_apply_prints_no_warning(rel: str) -> None:
    """warn=restricted: runAsNonRoot + numeric non-zero uid (image USER is a name), no escalation,
    drop ALL, seccomp RuntimeDefault, on every container (pod- or container-level)."""
    for label, ps in L.iter_pod_specs(L.render(rel)):
        for c in _all_containers(ps):
            who = f"{label}/{c['name']}"
            assert _effective(ps, c, "runAsNonRoot") is True, who
            uid = _effective(ps, c, "runAsUser")
            assert isinstance(uid, int) and uid != 0, f"{who}: runAsUser={uid!r}"
            sc = c.get("securityContext") or {}
            assert sc.get("allowPrivilegeEscalation") is False, who
            assert "ALL" in ((sc.get("capabilities") or {}).get("drop") or []), who
            assert not (sc.get("capabilities") or {}).get("add"), who
            seccomp = _effective(ps, c, "seccompProfile") or {}
            assert seccomp.get("type") in ("RuntimeDefault", "Localhost"), who


# ----------------------------------------------------------------------------- resources
@pytest.mark.parametrize("rel", ALL)
def test_explicit_cpu_and_memory_requests_and_limits(rel: str) -> None:
    for c in L.components(_dgd(rel)):
        res = L.main_container(c).get("resources") or {}
        for kind in ("requests", "limits"):
            for r in ("cpu", "memory"):
                assert (res.get(kind) or {}).get(r), f"{c['name']}: resources.{kind}.{r} missing"
        for r in ("cpu", "memory"):
            assert _quantity(res["limits"][r]) >= _quantity(res["requests"][r]), f"{c['name']}: {r}"


# ----------------------------------------------------------------------------- FT-4 pins (real Paris cluster)
@pytest.mark.parametrize("rel", ALL)
def test_workers_pass_block_size_64(rel: str) -> None:
    """FT-4: `--block-size 64`.  The flag is declared by v1.5.1 `mocker/args.py:202-208` (confirmed in
    `--help` on the real image); without it the SGLang engine type defaults to 1-token blocks."""
    assert "--block-size" in _v151_worker_flags()
    for c in _mockers(_dgd(rel)):
        assert _worker_flags(c).get("block-size") == ["64"], _worker_flags(c)


MI = 2**20
WORKER_REQUEST_MAX_MEM = 768 * MI   # measured working set ~470 MiB per worker
FRONTEND_REQUEST_MAX_MEM = 512 * MI  # measured working set ~270 MiB
NODE_ALLOCATABLE_CPU = 3.5  # c7i-flex.xlarge: 4 vCPU, minus system pods (conservative)
NODE_ALLOCATABLE_MEM = 6 * 2**30  # 8 GiB node, minus system pods/kubelet reserve (conservative)


def _requests(comp: dict[str, Any], kind: str, res: str) -> float:
    return _quantity((L.main_container(comp).get("resources") or {})[kind][res])


@pytest.mark.parametrize("rel", ALL)
def test_memory_requests_sized_for_one_small_node(rel: str) -> None:
    """Surge frontend must fit next to the old one on a single c7i-flex.xlarge (8 GiB)."""
    dgd = _dgd(rel)
    assert _requests(_frontend(dgd), "requests", "memory") <= FRONTEND_REQUEST_MAX_MEM
    for c in _mockers(dgd):
        assert _requests(c, "requests", "memory") <= WORKER_REQUEST_MAX_MEM, c["name"]


@pytest.mark.parametrize("rel", ALL)
def test_memory_limits_at_least_twice_the_request(rel: str) -> None:
    """Headroom over the measured working set (limits >= 2x requests), so a bursty pod is not OOM-killed."""
    for c in L.components(_dgd(rel)):
        req, lim = _requests(c, "requests", "memory"), _requests(c, "limits", "memory")
        assert lim >= 2 * req, f"{c['name']}: memory limit {lim / MI:.0f}Mi < 2 x request {req / MI:.0f}Mi"


@pytest.mark.parametrize("rel", ALL)
def test_whole_graph_plus_a_surge_frontend_fits_on_one_node(rel: str) -> None:
    """Derived fit check: every replica's request plus ONE extra frontend (rolling surge) must stay
    within a conservative c7i-flex.xlarge allocatable (3.5 vCPU, 6 GiB)."""
    dgd = _dgd(rel)
    front = _frontend(dgd)
    cpu = mem = 0.0
    for c in L.components(dgd):
        n = int(c.get("replicas", 1))
        cpu += n * _requests(c, "requests", "cpu")
        mem += n * _requests(c, "requests", "memory")
    cpu += _requests(front, "requests", "cpu")
    mem += _requests(front, "requests", "memory")
    assert cpu <= NODE_ALLOCATABLE_CPU, f"cpu requests with surge: {cpu}"
    assert mem <= NODE_ALLOCATABLE_MEM, f"memory requests with surge: {mem / 2**30:.2f}Gi"
