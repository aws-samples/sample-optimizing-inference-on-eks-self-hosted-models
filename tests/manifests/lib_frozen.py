# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Digests of what was published and must not move when the NVLink alternative is added.

The NVLink alternative (``components/pd-nvlink``, ``variants-nvlink/``, ``rendered-nvlink/`` and the ``nvlink/`` sub-folder of the four prefill/decode
example folders 03, 04, 06 and 07) is added NEXT TO the published Track Q material.  The published cells, their renders and goldens, the top-level files of the example folders 00 to 07
(the ``nvlink/`` sub-folders and the READMEs may be added), the matrix driver and the plans are the evidence behind the published numbers, so they stay byte-identical.  Each entry is a
digest of a directory: sha256 over the sorted ``relative path NUL sha256(content)`` lines of its files (bytecode caches and
``.DS_Store`` left out).  An added, removed or edited file moves the digest.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import lib_manifests as L
import lib_proc

SKIP = {"__pycache__", ".DS_Store", ".pytest_cache", ".ruff_cache", ".mypy_cache"}
EXAMPLE_FILES = ("diff-vs-baseline.diff", "kustomization.yaml", "rendered.yaml")  # the README of an example may gain a pointer

# directory (relative to the repository root) -> digest
FROZEN_TREES: dict[str, str] = {
    "tools/matrix_driver": "05a0ebcf0981ec0c128bfbc0c0680e62b7265533ff5df7984c5e00601dff99f9",
    "plans": "8f485235b1793d6d0a342d60f288d474461a37642c196513dd8fff88503fa726",
    "tools/demo_bench": "c375d2a90ae3e55acb5a40c00294b608839966b130047265fb394f721a1663b6",
    "tools/stage_model": "ee59ee5888e7bf19bef0469c8504491a65e88c0d6a5669f32567c97825e5e826",
    "tools/bench-image": "bc33549c0702c69c924f95575e251762fa13cb6682e2a2f99877e08085a646df",
    "manifests/track-q-qwen38/rendered": "7b13f36e954328f561843985cf0aacf59368bed6e8155b52a253ac9793c53200",
    "manifests/track-q-qwen38/base": "050d0652a189bfb0151181680839aa90f1b932f62aa55017111750c34927cf3a",
    "manifests/track-q-qwen38/cells": "a10a9adb9929199fa788f6a1fc5f69878d95fb25bd8ffdfaafc1aeefad12ff41",
    "manifests/track-q-qwen38/overlays": "ca3a58ac8217284f027449763ac63e8dcba7f3a25a9ddb37763c6ba4a0e26973",
    "manifests/track-q-qwen38/topologies": "b526b36e21fd58b548a7e6688ee9654b77e1d8a049f15918d656002c1ef4eb1f",
    "manifests/track-q-qwen38/dis": "6af369dc7b18e2b5167061801718d82bdf067ceddda03dbe242f3989a2955fdc",
    "manifests/track-q-qwen38/frontend": "5192102da781a6cd6ba9fa434948037e62e363b6ee813824f8742dae805ce465",
    "manifests/track-q-qwen38/frontends": "965e694145bc5e5731a4a4954e2e84b12b7c8d0999f6e15fd31fd20e21447162",
    "manifests/track-q-qwen38/driver": "b083fb330c7a90f732f16e76501262419b4c46ef84aad2608a54c924b26dfb7a",
    "manifests/track-q-qwen38/components/hicache-agg": "629f23a4b9caf3cd1d7fa0998ea66973b2862c07b16643419344c80a0c32532b",
    "manifests/track-q-qwen38/components/hicache-prefill": "0253a3642e097dbabf3a3352d957464cac6fded77628e936784085258538368a",
    "manifests/track-q-qwen38/components/pd-efa": "4b419a5cb3b3b80bb525c0d2fbd6191b2fc42ebbdae45a4ddfee66cf2c8c8e59",
    "manifests/track-q-qwen38/components/router-kv": "75b90a742a2b45699297287fbf941dd628320022ba604248b5528bfeba6a0698",
}
FROZEN_EXAMPLES: dict[str, str] = {  # example folder -> digest of EXAMPLE_FILES
    "00-baseline": "1943f0caad5ba3e1c3a0612756b53d8746eb132e6de8c1d646553ba42d1ccded",
    "01-kv-router": "ea97844e1e676090e30d9004b5228c3a77600738b6c204a875d88b66b61c0cf9",
    "02-hicache": "897aee6d2808cc1b479ed735c0910c272b9bb2c6aa380ea0b1941b72b2f15620",
    "03-pd-disaggregation": "2c5de4ec115a8effd9b1748135751694a38276078fea9d3a8f0f7dd6ad2ed0ae",
    "04-all-three": "7ec2fe88ecc7f7eb948747bef035f894482bf591c5a91ec5fae2bad83931632c",
    "05-kv-router-hicache": "1bbe77ffbdfca683e008f425c85a5a437b5362906a1c4a479161738030f85e60",
    "06-kv-router-pd": "58ee62a92915a342bc72a624348c90ca41ed3c57a2939ef40ef7fd8719f91078",
    "07-pd-hicache": "111c254acb5c1e46be06628b81ec1622b5697e285c1925b7d567468e52631d5c",
}
FROZEN_FILES: dict[str, str] = {  # single files (sha256 of the bytes)
    "tests/manifests/golden_trackq_renders.json": "be025cb3635f704fdc6acb7adc172ef4e5bba7d28247074bb2685270c07953a6",
    "terraform/cluster/grafana-edge.tf": "5f5418d4d9205fd55c4efc5f3362e2dfed2e527653ef13d1e4f3a2dc0435c216",
}


def tree_digest(rel: str, names: tuple[str, ...] | None = None, root: Path = L.REPO_ROOT, top_level_only: bool = False) -> str:
    """Return the digest of the directory ``rel`` (see the module docstring).

    The files are those git tracks plus the ones it does not ignore (an added file that is not committed yet counts, a
    virtualenv or a bytecode cache does not).

    Args:
        rel: Directory, relative to the repository root.
        names: When given, only the files of this name count.
        root: The repository root.
        top_level_only: When true, only the files directly in ``rel`` count (sub-folders such as ``nvlink/`` are left out).

    Returns:
        The hex sha256; the digest of an empty or missing directory is that of no lines.
    """
    listing = lib_proc.run(
        ["git", "-C", str(root), "ls-files", "-z", "--cached", "--others", "--exclude-standard", "--", rel],
        capture_output=True, check=True,
    ).stdout.decode()
    base = Path(rel)
    digest = hashlib.sha256()
    for path in sorted(p for p in listing.split("\0") if p):
        sub = Path(path).relative_to(base)
        name = sub.name
        if top_level_only and len(sub.parts) > 1:
            continue
        if not (root / path).is_file() or SKIP & set(sub.parts) or name.endswith(".pyc") or (names is not None and name not in names):
            continue
        digest.update(f"{sub.as_posix()}\0{hashlib.sha256((root / path).read_bytes()).hexdigest()}\n".encode())
    return digest.hexdigest()
