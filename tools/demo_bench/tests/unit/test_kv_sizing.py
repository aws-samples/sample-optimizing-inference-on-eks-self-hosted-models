# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests for ``demo_bench.domain.kv_sizing`` (spec 9.5, 10.3, 12.1)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

GIB = 2**30


# --------------------------------------------------------------------------- model dims
def test_load_model_dims_reads_explicit_head_dim_not_derived(fixtures_dir: Path) -> None:
    from demo_bench.domain.kv_sizing import load_model_dims

    dims = load_model_dims(fixtures_dir / "qwen3_32b_config.json")
    # hidden_size // num_attention_heads == 80 (the Dynamo mocker bug); config says 128.
    assert dims.head_dim == 128
    assert dims.num_layers == 64
    assert dims.num_kv_heads == 8


def test_load_model_dims_missing_head_dim_raises_instead_of_deriving(
    fixtures_dir: Path, tmp_path: Path
) -> None:
    from demo_bench.domain.kv_sizing import load_model_dims

    cfg = json.loads((fixtures_dir / "qwen3_32b_config.json").read_text())
    del cfg["head_dim"]
    p = tmp_path / "config.json"
    p.write_text(json.dumps(cfg))
    with pytest.raises(ValueError, match="head_dim"):
        load_model_dims(p)


@pytest.mark.parametrize(
    ("dtype", "expected"),
    [("fp8_e4m3", 131072), ("fp8_e5m2", 131072), ("bfloat16", 262144), ("float16", 262144)],
)
def test_bytes_per_token_by_kv_dtype(fixtures_dir: Path, dtype: str, expected: int) -> None:
    from demo_bench.domain.kv_sizing import kv_bytes_per_token, load_model_dims

    dims = load_model_dims(fixtures_dir / "qwen3_32b_config.json")
    # 2 (K and V) * layers 64 * kv_heads 8 * head_dim 128 * dtype bytes; fp8 == 128 KiB (spec 5.3)
    assert kv_bytes_per_token(dims, dtype) == expected


def test_bytes_per_token_unknown_dtype_raises(fixtures_dir: Path) -> None:
    from demo_bench.domain.kv_sizing import kv_bytes_per_token, load_model_dims

    dims = load_model_dims(fixtures_dir / "qwen3_32b_config.json")
    with pytest.raises(ValueError, match="dtype"):
        kv_bytes_per_token(dims, "int3")


# --------------------------------------------------------------------------- capacity
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def _sizing(fixtures_dir: Path, **kw: float) -> object:
    from demo_bench.domain.kv_sizing import KvSizing, load_model_dims

    dims = load_model_dims(fixtures_dir / "qwen3_32b_config.json")
    args = {"hbm_gib": 100.0, "mem_fraction": 0.9, "weights_gib": 40.0, "overhead_gib": 2.0}
    args.update(kw)
    return KvSizing(dims=dims, kv_dtype="fp8_e4m3", **args)  # type: ignore[arg-type]


def test_device_tokens_per_rank_hand_computed(fixtures_dir: Path) -> None:
    s = _sizing(fixtures_dir)
    assert s.bytes_per_token == 131072  # type: ignore[attr-defined]
    # (100 * 0.9 - 40 - 2) GiB = 48 GiB; 48 * 2**30 / 2**17 = 393216
    assert s.device_tokens_per_rank() == 393216  # type: ignore[attr-defined]


def test_device_tokens_floors_fractional_result(fixtures_dir: Path) -> None:
    s = _sizing(fixtures_dir, hbm_gib=180.0, mem_fraction=0.88, weights_gib=110.0, overhead_gib=0.0)
    # 48.4 GiB * 8192 tokens/GiB = 396492.8 -> 396492
    assert s.device_tokens_per_rank() == 396492  # type: ignore[attr-defined]


def test_sessions_per_rank_floors(fixtures_dir: Path) -> None:
    s = _sizing(fixtures_dir)
    assert s.sessions_per_rank(32768) == 12  # type: ignore[attr-defined]  # 393216 / 32768
    assert s.sessions_per_rank(200_000) == 1  # type: ignore[attr-defined]
    assert s.sessions_per_rank(400_000) == 0  # type: ignore[attr-defined]


def test_sessions_per_rank_rejects_non_positive_length(fixtures_dir: Path) -> None:
    s = _sizing(fixtures_dir)
    with pytest.raises(ValueError):
        s.sessions_per_rank(0)  # type: ignore[attr-defined]


def test_no_room_for_kv_raises(fixtures_dir: Path) -> None:
    from demo_bench.domain.kv_sizing import KvSizing, load_model_dims

    dims = load_model_dims(fixtures_dir / "qwen3_32b_config.json")
    with pytest.raises(ValueError):
        KvSizing(dims=dims, kv_dtype="fp8_e4m3", hbm_gib=80.0, mem_fraction=0.5,
                 weights_gib=60.0, overhead_gib=0.0).device_tokens_per_rank()


@pytest.mark.parametrize("frac", [0.0, -0.1, 1.5])
def test_invalid_mem_fraction_raises(fixtures_dir: Path, frac: float) -> None:
    from demo_bench.domain.kv_sizing import KvSizing, load_model_dims

    dims = load_model_dims(fixtures_dir / "qwen3_32b_config.json")
    with pytest.raises(ValueError):
        KvSizing(dims=dims, kv_dtype="fp8_e4m3", hbm_gib=80.0, mem_fraction=frac,
                 weights_gib=10.0, overhead_gib=0.0).device_tokens_per_rank()


def test_host_pool_tokens_uses_decimal_gb(fixtures_dir: Path) -> None:
    s = _sizing(fixtures_dir)
    # 100 GB = 100e9 bytes (NOT GiB); 100e9 / 131072 = 762939.45 -> 762939
    assert s.host_pool_tokens(100) == 762939  # type: ignore[attr-defined]
    assert s.host_pool_tokens(0) == 0  # type: ignore[attr-defined]


def test_unique_capacity_write_through_mirrors_device(fixtures_dir: Path) -> None:
    s = _sizing(fixtures_dir)
    device = s.device_tokens_per_rank()  # type: ignore[attr-defined]
    host = s.host_pool_tokens(100)  # type: ignore[attr-defined]
    assert device == 393216 and host == 762939
    # write-through: L2 mirrors L1, unique tokens = max(device, host)
    assert s.unique_tokens_per_rank(100, write_through=True) == 762939  # type: ignore[attr-defined]
    # host smaller than device: nothing extra beyond device
    assert s.unique_tokens_per_rank(10, write_through=True) == device  # type: ignore[attr-defined]
    # write-back / no mirror: tiers add up
    assert s.unique_tokens_per_rank(100, write_through=False) == device + host  # type: ignore[attr-defined]


@given(
    hbm=st.floats(min_value=60, max_value=300),
    extra=st.floats(min_value=0.1, max_value=50),
)
def test_device_tokens_monotonic_in_hbm(hbm: float, extra: float) -> None:
    fixtures_dir = FIXTURES
    s1 = _sizing(fixtures_dir, hbm_gib=hbm, mem_fraction=1.0, weights_gib=10.0, overhead_gib=0.0)
    s2 = _sizing(fixtures_dir, hbm_gib=hbm + extra, mem_fraction=1.0, weights_gib=10.0,
                 overhead_gib=0.0)
    assert s2.device_tokens_per_rank() >= s1.device_tokens_per_rank()  # type: ignore[attr-defined]


# --------------------------------------------------------------------------- LRU replay
def _trace(order: str, size: int = 100) -> list[tuple[str, int]]:
    return [(c, size) for c in order]


def test_replay_lru_simple_device_hit() -> None:
    from demo_bench.domain.kv_sizing import replay_lru

    h = replay_lru(_trace("ABA"), device_tokens=200, host_tokens=0, storage_tokens=0)
    assert (h.device, h.host, h.storage, h.miss) == (1, 0, 0, 2)
    assert h.accesses == 3


def test_replay_lru_recency_protects_reused_entry() -> None:
    from demo_bench.domain.kv_sizing import replay_lru

    # A B C B : after C the device (2 entries) holds {B, C}; B hits.
    h = replay_lru(_trace("ABCB"), device_tokens=200, host_tokens=0, storage_tokens=0)
    assert (h.device, h.miss) == (1, 3)


def test_replay_lru_cyclic_access_pathology_gives_zero_device_hits() -> None:
    from demo_bench.domain.kv_sizing import replay_lru

    # 3 sessions cycling through a 2-session LRU: every revisit is evicted just before reuse.
    h = replay_lru(_trace("ABCABC"), device_tokens=200, host_tokens=0, storage_tokens=0)
    assert h.device == 0
    assert h.miss == 6


def test_replay_lru_cyclic_is_rescued_by_host_tier() -> None:
    from demo_bench.domain.kv_sizing import replay_lru

    h = replay_lru(_trace("ABCABC"), device_tokens=200, host_tokens=300, storage_tokens=0)
    assert (h.device, h.host, h.storage, h.miss) == (0, 3, 0, 3)


def test_replay_lru_cyclic_is_rescued_by_storage_tier_only() -> None:
    from demo_bench.domain.kv_sizing import replay_lru

    h = replay_lru(_trace("ABCABC"), device_tokens=200, host_tokens=200, storage_tokens=300)
    assert (h.device, h.host, h.storage, h.miss) == (0, 0, 3, 3)


def test_replay_lru_entry_larger_than_tier_never_hits_that_tier() -> None:
    from demo_bench.domain.kv_sizing import replay_lru

    h = replay_lru([("X", 300), ("X", 300)], device_tokens=200, host_tokens=300,
                   storage_tokens=0)
    assert (h.device, h.host, h.miss) == (0, 1, 1)


def test_replay_lru_zero_capacity_everywhere_is_all_miss() -> None:
    from demo_bench.domain.kv_sizing import replay_lru

    h = replay_lru(_trace("AAAA"), device_tokens=0, host_tokens=0, storage_tokens=0)
    assert h.miss == 4 and h.device == h.host == h.storage == 0


def test_replay_lru_empty_trace() -> None:
    from demo_bench.domain.kv_sizing import replay_lru

    h = replay_lru([], device_tokens=100, host_tokens=100, storage_tokens=100)
    assert h.accesses == 0 and h.miss == 0


_traces = st.lists(st.tuples(st.sampled_from("ABCDEFGH"), st.just(100)), max_size=60)
_caps = st.integers(min_value=0, max_value=1000)


@settings(max_examples=150)
@given(trace=_traces, dev=_caps, host=_caps, sto=_caps)
def test_property_hits_never_exceed_accesses(
    trace: list[tuple[str, int]], dev: int, host: int, sto: int
) -> None:
    from demo_bench.domain.kv_sizing import replay_lru

    h = replay_lru(trace, device_tokens=dev, host_tokens=host, storage_tokens=sto)
    assert h.device + h.host + h.storage + h.miss == len(trace) == h.accesses
    assert min(h.device, h.host, h.storage, h.miss) >= 0
    assert h.device + h.host + h.storage <= len(trace)


@settings(max_examples=150)
@given(trace=_traces, dev=_caps, extra=st.integers(min_value=0, max_value=500), host=_caps,
       sto=_caps)
def test_property_larger_device_never_reduces_device_or_total_hits(
    trace: list[tuple[str, int]], dev: int, extra: int, host: int, sto: int
) -> None:
    from demo_bench.domain.kv_sizing import replay_lru

    a = replay_lru(trace, device_tokens=dev, host_tokens=host, storage_tokens=sto)
    b = replay_lru(trace, device_tokens=dev + extra, host_tokens=host, storage_tokens=sto)
    assert b.device >= a.device
    assert b.device + b.host + b.storage >= a.device + a.host + a.storage


@settings(max_examples=150)
@given(trace=_traces, dev=_caps, host=_caps, extra=st.integers(min_value=0, max_value=500),
       sto=_caps)
def test_property_larger_host_or_storage_never_reduces_total_hits(
    trace: list[tuple[str, int]], dev: int, host: int, extra: int, sto: int
) -> None:
    from demo_bench.domain.kv_sizing import replay_lru

    a = replay_lru(trace, device_tokens=dev, host_tokens=host, storage_tokens=sto)
    b = replay_lru(trace, device_tokens=dev, host_tokens=host + extra, storage_tokens=sto)
    c = replay_lru(trace, device_tokens=dev, host_tokens=host, storage_tokens=sto + extra)
    total_a = a.device + a.host + a.storage
    assert b.device + b.host + b.storage >= total_a
    assert c.device + c.host + c.storage >= total_a
