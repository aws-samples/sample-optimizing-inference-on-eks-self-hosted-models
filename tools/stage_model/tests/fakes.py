# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Fakes of the two ports of stage_model: the Hugging Face API and S3 (boto3 client subset).

The errors are the REAL ``huggingface_hub`` / ``botocore`` classes, built exactly as the libraries raise them
(huggingface_hub 2.x: ``response=`` is mandatory; ``GatedRepoError`` is a subclass of ``RepositoryNotFoundError``).
"""

from __future__ import annotations

import hashlib
import io
import json
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx2
from botocore.exceptions import ClientError
from huggingface_hub import errors
from huggingface_hub.hf_api import RepoFile

TOKEN = "hf_" + "DUMMYTOKEN" + "0123456789abcdefghijklmnop"  # a dummy, never a real credential
REVISION = "0123456789abcdef0123456789abcdef01234567"
MODEL = "acme/tiny-model"
BUCKET = "demo-models-bucket"
PREFIX = f"{MODEL}/{REVISION}"
URI = f"s3://{BUCKET}/{PREFIX}"


def http_response(status: int) -> httpx2.Response:
    """Return a response object for the HF error constructors."""
    return httpx2.Response(status, request=httpx2.Request("GET", "https://huggingface.co/api/models/acme/tiny-model"))


def gated_error() -> errors.GatedRepoError:
    """403 of a gated repo whose licence the token's owner has not accepted."""
    return errors.GatedRepoError("403 Client Error: access to this repo is restricted", response=http_response(403))


def not_found_error() -> errors.RepositoryNotFoundError:
    """404 (also what the Hub returns for a private repo without a token)."""
    return errors.RepositoryNotFoundError("404 Client Error: repo not found", response=http_response(404))


def unauthorized_error() -> errors.HfHubHTTPError:
    """401 of an invalid or expired token."""
    return errors.HfHubHTTPError("401 Client Error: invalid token", response=http_response(401))


def sha256(data: bytes) -> str:
    """Return the hex sha256."""
    return hashlib.sha256(data).hexdigest()


def tree_entry(path: str, data: bytes, *, lfs: bool) -> RepoFile:
    """Return a ``RepoFile`` as ``list_repo_tree`` yields it (LFS files carry ``lfs.sha256``; the API key is ``oid``)."""
    kwargs: dict[str, Any] = {"path": path, "size": len(data), "oid": "git-" + sha256(data)[:8]}
    if lfs:
        kwargs["lfs"] = {"size": len(data), "oid": sha256(data), "pointerSize": 134}
    return RepoFile(**kwargs)


# A small but realistic repo: sharded safetensors (LFS), small json files (plain git), and noise.
CONTENT: dict[str, bytes] = {
    "model-00001-of-00002.safetensors": b"A" * 4096,
    "model-00002-of-00002.safetensors": b"B" * 2048,
    "model.safetensors.index.json": b'{"weight_map": {}}',
    "config.json": b'{"architectures": ["X"]}',
    "generation_config.json": b'{"do_sample": false}',
    "tokenizer.json": b'{"version": "1.0"}',
    "tokenizer_config.json": b'{"x": 1}',
    "special_tokens_map.json": b"{}",
    "README.md": b"# model card",
    ".gitattributes": b"*.safetensors filter=lfs",
    "pytorch_model-00001-of-00002.bin": b"C" * 4096,
    "pytorch_model.bin.index.json": b"{}",
    "tf_model.h5": b"D" * 128,
    "flax_model.msgpack": b"E" * 128,
}
LFS_PATHS = {p for p in CONTENT if p.endswith((".safetensors", ".bin", ".h5", ".msgpack"))}
WANTED = {
    "model-00001-of-00002.safetensors",
    "model-00002-of-00002.safetensors",
    "model.safetensors.index.json",
    "config.json",
    "generation_config.json",
    "tokenizer.json",
    "tokenizer_config.json",
    "special_tokens_map.json",
}


@dataclass
class FakeHf:
    """Fake of the HF port. ``gated`` is what ``model_info`` reports; ``content`` is the repo at REVISION."""

    gated: Any = False
    content: dict[str, bytes] = field(default_factory=lambda: dict(CONTENT))
    lfs_paths: set[str] = field(default_factory=lambda: set(LFS_PATHS))
    # needs_token: every call without a token raises the gated error (what the Hub does for a gated repo's files)
    needs_token: bool = False
    # errors to raise: {"model_info": exc, "list_repo_tree": exc, "hf_hub_download": exc}; a callable gets the token
    raises: dict[str, Any] = field(default_factory=dict)
    corrupt: dict[str, bytes] = field(default_factory=dict)  # what hf_hub_download writes instead of the real bytes
    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    resident_peak: int = 0

    def _enter(self, name: str, **kw: Any) -> None:
        self.calls.append((name, kw))
        exc = self.raises.get(name)
        if exc is not None:
            raise exc(kw.get("token")) if callable(exc) else exc
        if self.needs_token and not kw.get("token") and name != "model_info":
            raise gated_error()

    def model_info(self, repo_id: str, *, revision: str | None = None, token: Any = None, **kw: Any) -> SimpleNamespace:
        """Return the public metadata (``gated`` is visible without a token)."""
        self._enter("model_info", repo_id=repo_id, revision=revision, token=token)
        return SimpleNamespace(id=repo_id, sha=revision, gated=self.gated)

    def list_repo_tree(self, repo_id: str, *, recursive: bool = False, revision: str | None = None, token: Any = None, **kw: Any) -> Any:
        """Yield the files of the repo (a generator, like the real one)."""
        self._enter("list_repo_tree", repo_id=repo_id, revision=revision, token=token, recursive=recursive)
        return iter([tree_entry(p, d, lfs=p in self.lfs_paths) for p, d in self.content.items()])

    def hf_hub_download(self, repo_id: str, filename: str, *, revision: str | None = None, local_dir: Any = None, token: Any = None, **kw: Any) -> str:
        """Write the file under ``local_dir`` and return its path."""
        self._enter("hf_hub_download", repo_id=repo_id, filename=filename, revision=revision, token=token)
        assert local_dir is not None, "download into the work dir (local_dir=...)"
        target = Path(local_dir) / filename
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(self.corrupt.get(filename, self.content[filename]))
        resident = sum(1 for p in Path(local_dir).rglob("*") if p.is_file())
        self.resident_peak = max(self.resident_peak, resident)
        return str(target)

    def names(self, name: str) -> list[dict[str, Any]]:
        """Return the kwargs of every call to ``name``."""
        return [kw for n, kw in self.calls if n == name]


def _client_error(code: str, op: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": code}, "ResponseMetadata": {"HTTPStatusCode": 404}}, op)


@dataclass
class FakeS3:
    """Fake of the boto3 S3 client subset. ``list_objects_v2`` pages by ``page_size`` (the code must paginate)."""

    objects: dict[str, bytes] = field(default_factory=dict)
    page_size: int = 2
    truncate_to: dict[str, int] = field(default_factory=dict)  # key suffix -> stored length (a bad upload)
    ops: list[tuple[str, str]] = field(default_factory=list)
    uploads: list[dict[str, Any]] = field(default_factory=list)

    def head_object(self, Bucket: str, Key: str, **kw: Any) -> dict[str, Any]:
        """Return the size or raise the 404 ClientError."""
        self.ops.append(("head", Key))
        if Key not in self.objects:
            raise _client_error("404", "HeadObject")
        return {"ContentLength": len(self.objects[Key])}

    def get_object(self, Bucket: str, Key: str, **kw: Any) -> dict[str, Any]:
        """Return the body or raise NoSuchKey."""
        self.ops.append(("get", Key))
        if Key not in self.objects:
            raise _client_error("NoSuchKey", "GetObject")
        return {"Body": io.BytesIO(self.objects[Key]), "ContentLength": len(self.objects[Key])}

    def put_object(self, Bucket: str, Key: str, Body: Any, **kw: Any) -> dict[str, Any]:
        """Store a small object."""
        self.ops.append(("put", Key))
        self.objects[Key] = Body if isinstance(Body, bytes) else Body.encode() if isinstance(Body, str) else Body.read()
        return {}

    def upload_file(self, Filename: str, Bucket: str, Key: str, ExtraArgs: dict[str, Any] | None = None, Callback: Any = None, Config: Any = None) -> None:
        """Store a local file (boto3 does the multipart split)."""
        self.ops.append(("upload", Key))
        self.uploads.append({"Key": Key, "ExtraArgs": ExtraArgs or {}, "Bucket": Bucket})
        data = Path(Filename).read_bytes()
        for suffix, length in self.truncate_to.items():
            if Key.endswith(suffix):
                data = data[:length]
        self.objects[Key] = data

    def list_objects_v2(self, Bucket: str, Prefix: str = "", ContinuationToken: str | None = None, **kw: Any) -> dict[str, Any]:
        """Return one page of ``(Key, Size)`` sorted by key."""
        self.ops.append(("list", Prefix))
        keys = sorted(k for k in self.objects if k.startswith(Prefix))
        start = int(ContinuationToken or 0)
        page = keys[start : start + self.page_size]
        resp: dict[str, Any] = {"Contents": [{"Key": k, "Size": len(self.objects[k])} for k in page], "KeyCount": len(page)}
        if start + self.page_size < len(keys):
            resp["IsTruncated"] = True
            resp["NextContinuationToken"] = str(start + self.page_size)
        else:
            resp["IsTruncated"] = False
        return resp

    def keys(self) -> set[str]:
        """Return the stored keys."""
        return set(self.objects)

    def write_ops(self) -> list[tuple[str, str]]:
        """Return the upload/put operations in order."""
        return [o for o in self.ops if o[0] in ("upload", "put")]


def marker_fields(raw: bytes) -> dict[str, str]:
    """Parse a ``.done`` marker written either as JSON or as ``key=value`` lines."""
    text = raw.decode()
    try:
        doc = json.loads(text)
        return {str(k): str(v) for k, v in doc.items()}
    except (ValueError, AttributeError):
        return dict(line.split("=", 1) for line in text.splitlines() if "=" in line)
