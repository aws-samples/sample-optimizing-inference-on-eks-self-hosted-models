# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Digests of the published surface, which must not move unless a change means to move it.

The parsed content of the eight example manifests (``examples/<folder>/manifest.yaml``, written files applied as is; their
comments and READMEs may change), the model-staging tool and the Grafana edge stay as they are.  A tree entry is a digest of a directory: sha256 over the
sorted ``relative path NUL sha256(content)`` lines of its files (bytecode caches and ``.DS_Store`` left out).  An added,
removed or edited file moves the digest.  A file entry is the sha256 of the file's bytes; an example entry is ``parsed_digest``.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import lib_manifests as L
import lib_proc
import yaml

SKIP = {"__pycache__", ".DS_Store", ".pytest_cache", ".ruff_cache", ".mypy_cache"}

# directory (relative to the repository root) -> digest
FROZEN_TREES: dict[str, str] = {
    "tools/stage_model": "b75bf2e1ac10e667b1a0d5b67f6224ae1606ab15c4a870471af20ee5cf1a4cf6",
}
# example folder -> parsed_digest() of its manifest.yaml (moved on purpose only by a change that means to edit that manifest;
# comments and layout do not move it)
FROZEN_EXAMPLES: dict[str, str] = {
    "00-baseline": "4bf7d936799dc1b57801d2419b85fcae87e2407048b06881186d6f3195d98bd5",
    "01-kv-router": "ccfc4ce508d5de46456e3279c253292801f86805b58cbaa31f97b8b362ee9964",
    "02-hicache": "98ea8dc5d992baf8979b10ff197705619cba9cebb227d2877f3c5af23b81bb1d",
    "03-pd-disaggregation": "70c971a2c1047ef1e0013f3d2b73c9261aa0efcef268eb66b426348cbd3b9d09",
    "04-kv-router-hicache": "b15bbc21d824fa50df2a6a9a04827f393fd9841ededee60b575b87d972086190",
    "05-kv-router-pd": "bb0c220a089207d5b71831076bb38d288b6bdc336c004bb4dab0d1ce483f7031",
    "06-pd-hicache": "0577c4522326dd5ab68fd8d764ed3382a8d8bbf849e88fc6bf7086e9d5d0d185",
    "07-all-three": "c826906c168586d96f01da491e4260cab83bf9a59de166dd47fe512c5bd33b2b",
}
FROZEN_FILES: dict[str, str] = {  # single files (sha256 of the bytes)
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
        top_level_only: When true, only the files directly in ``rel`` count (sub-folders are left out).

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


def parsed_digest(path: Path) -> str:
    """Return the sha256 of the YAML documents of ``path`` as parsed (comments, quoting and layout do not count).

    Args:
        path: A YAML file.

    Returns:
        The hex sha256 of the documents as canonical JSON (sorted keys).
    """
    docs = list(yaml.safe_load_all(path.read_text()))
    return hashlib.sha256(json.dumps(docs, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
