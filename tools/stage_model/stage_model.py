# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Stage the weights of a Hugging Face model into the models bucket (Job node-ops/model-stage).

``scripts/10-stage-model.sh`` mounts this file and ``requirements.txt`` from the ConfigMap
``model-stage-code`` and runs it with the environment below. Credentials come from the Pod Identity
role ``model-stager`` (write access under the model prefix only).

Environment:
    MODEL_ID: ``<org>/<name>`` of the Hugging Face repository.
    MODEL_REVISION: 40-character commit sha (a branch or tag such as ``main`` moves; refused).
    MODEL_S3_URI: ``s3://<bucket>/<prefix>``; the files land flat under ``<prefix>/``.
    HF_TOKEN: optional, gated models only; empty counts as unset.
    WORK_DIR: scratch directory (one file at a time is on disk).

Flow:
    1. ``<prefix>/.done`` of the same model and revision present: exit 0 at once, no Hub call.
    2. ``model_info`` (never with a token) reads ``gated``. A public model never receives the token,
       even when HF_TOKEN is set; a gated model without HF_TOKEN fails fast with what to do.
    3. The repo tree at the revision is filtered: safetensors weights (``*.bin`` only when the repo
       has no safetensors), config, tokenizer, chat template, remote-code and licence files
       (``LICENSE``, ``NOTICE``, ``COPYING``, ``USE_POLICY``, with or without an extension).
    4. Per file: skip it when S3 already holds the listed size (a failed run resumes), else
       download it, check its size and, for LFS files, the sha256 the Hub lists, upload it with
       ``upload_file`` (multipart, SHA256 checksums) and delete the local copy.
    5. Every file's size in S3 must equal the Hub's; ``<prefix>/.done`` is written last
       (``key=value`` lines: model_id, revision, file_count, total_bytes).

Exit codes are the ``EXIT_*`` constants; the Job does not retry 2 to 5 (podFailurePolicy). The
token is redacted from every log record of this module.
"""

import argparse
import fnmatch
import hashlib
import logging
import os
import re
import shutil
import sys
import tempfile
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import boto3  # type: ignore[import-untyped]  # boto3 ships no type information (no py.typed)
from huggingface_hub import HfApi
from huggingface_hub import errors as hf_errors
from huggingface_hub.hf_api import RepoFile

LOG = logging.getLogger("stage_model")

EXIT_OK = 0
EXIT_CONFIG = 2
EXIT_GATED_NO_TOKEN = 3
EXIT_ACCESS_DENIED = 4
EXIT_NOT_FOUND = 5
EXIT_VERIFY = 6
EXIT_TRANSFER = 7

MARKER = ".done"
# <org>/<name>, neither part "." or "..": the default S3 prefix is <MODEL_ID>/<MODEL_REVISION>.
MODEL_ID_RE = re.compile(r"(?!\.\.?/)[A-Za-z0-9._-]+/(?!\.\.?$)[A-Za-z0-9._-]+")
REVISION_RE = re.compile(r"[0-9a-f]{40}")
S3_URI_RE = re.compile(r"s3://([a-z0-9][a-z0-9.-]{1,61}[a-z0-9])/(.+)")
SEGMENT_RE = re.compile(r"[A-Za-z0-9._-]+")
TOKEN_RE = re.compile(r"[A-Za-z0-9._-]{8,4096}")
TOKEN_SHAPED_RE = re.compile(r"hf_[A-Za-z0-9]{8,}")
# Besides the weights: config, tokenizer, chat template, remote-code and licence files. Hub paths are
# case-sensitive (matched with fnmatchcase); the licence patterns also match extensionless files.
METADATA_PATTERNS = (
    "*.json",
    "*.txt",
    "*.model",
    "*.tiktoken",
    "*.jinja",
    "*.py",
    "LICENSE*",
    "NOTICE*",
    "COPYING*",
    "USE_POLICY*",
)
TOKENS_PAGE = "https://huggingface.co/settings/tokens"


class HubPort(Protocol):
    """The Hugging Face calls of the Job (``huggingface_hub.HfApi`` implements them)."""

    def model_info(
        self, repo_id: str, *, revision: str | None = ..., token: bool | str | None = ...
    ) -> Any:
        """Return the model metadata (``.gated``: False, "auto" or "manual")."""

    def list_repo_tree(
        self,
        repo_id: str,
        *,
        recursive: bool = ...,
        revision: str | None = ...,
        token: bool | str | None = ...,
    ) -> Iterable[Any]:
        """Yield the files (``RepoFile``) and folders of the repo at ``revision``."""

    def hf_hub_download(
        self,
        repo_id: str,
        filename: str,
        *,
        revision: str | None = ...,
        local_dir: str | Path | None = ...,
        token: bool | str | None = ...,
    ) -> Any:
        """Download one file into ``local_dir`` and return its local path."""


class S3Port(Protocol):
    """The S3 calls of the Job (a boto3 S3 client implements them)."""

    def list_objects_v2(self, **kwargs: Any) -> dict[str, Any]:
        """Return one page of the objects under a prefix."""

    def get_object(self, **kwargs: Any) -> dict[str, Any]:
        """Return an object (``Body`` is a stream)."""

    def put_object(self, **kwargs: Any) -> dict[str, Any]:
        """Write a small object."""

    def upload_file(self, **kwargs: Any) -> None:
        """Upload a local file (multipart above the transfer threshold)."""


@dataclass(frozen=True)
class Config:
    """Validated input of a staging run."""

    model_id: str
    revision: str
    bucket: str
    prefix: str
    token: str | None
    work_dir: Path
    region: str | None

    @property
    def uri(self) -> str:
        """Return ``s3://<bucket>/<prefix>`` (no trailing slash)."""
        return f"s3://{self.bucket}/{self.prefix}"

    @property
    def page(self) -> str:
        """Return the model page on the Hub."""
        return f"https://huggingface.co/{self.model_id}"


class ConfigError(Exception):
    """An invalid input; the message names the variable and never holds the token."""


class StageError(Exception):
    """A failed run with its exit code; the message never holds the token."""

    def __init__(self, code: int, message: str) -> None:
        """Keep the exit code next to the message.

        Args:
            code: One of the EXIT_* constants.
            message: What failed and what to do.
        """
        super().__init__(message)
        self.code = code


def redact(text: str, secret: str | None) -> str:
    """Remove the token, and anything shaped like a Hugging Face token, from ``text``.

    Args:
        text: Any message.
        secret: The token of the run, or None.

    Returns:
        The text without the token.
    """
    if secret:
        text = text.replace(secret, "<redacted>")
    return TOKEN_SHAPED_RE.sub("hf_<redacted>", text)


class _Redactor(logging.Filter):
    """Redact the token from every record of LOG (exception texts included)."""

    def __init__(self, secret: str | None) -> None:
        super().__init__()
        self._secret = secret

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact(record.getMessage(), self._secret)
        record.args = ()
        record.exc_info = None
        record.exc_text = None
        return True


def load_config(environ: Mapping[str, str], work_dir: Path | None) -> Config:
    """Validate the environment before any network call.

    Args:
        environ: The Job environment.
        work_dir: Scratch directory override (else WORK_DIR, else the system temp dir).

    Returns:
        The validated configuration.

    Raises:
        ConfigError: A variable is missing or malformed.
    """
    model_id = environ.get("MODEL_ID", "")
    if not MODEL_ID_RE.fullmatch(model_id):
        raise ConfigError(
            "MODEL_ID must be <org>/<name> of [A-Za-z0-9._-], neither part . or .. "
            f"(got {model_id!r})"
        )
    revision = environ.get("MODEL_REVISION", "")
    if not REVISION_RE.fullmatch(revision):
        raise ConfigError(
            "MODEL_REVISION must be the 40-character lowercase hex commit sha of the model "
            f"(a branch or tag such as main moves; pin the commit), got {revision!r}"
        )
    uri = environ.get("MODEL_S3_URI", "")
    match = S3_URI_RE.fullmatch(uri)
    prefix = match.group(2).removesuffix("/") if match else ""
    segments = prefix.split("/")
    if not match or not all(SEGMENT_RE.fullmatch(s) and s not in (".", "..") for s in segments):
        raise ConfigError(f"MODEL_S3_URI must be s3://<bucket>/<prefix> (got {uri!r})")
    raw_token = environ.get("HF_TOKEN", "")
    if raw_token and not TOKEN_RE.fullmatch(raw_token):
        raise ConfigError(
            "HF_TOKEN is malformed: expected 8 to 4096 characters of A-Za-z0-9._- "
            "(the value is not printed)"
        )
    return Config(
        model_id=model_id,
        revision=revision,
        bucket=match.group(1),
        prefix=prefix,
        token=raw_token or None,
        work_dir=work_dir or Path(environ.get("WORK_DIR") or tempfile.gettempdir()),
        region=environ.get("AWS_REGION") or None,
    )


def list_objects(s3: S3Port, bucket: str, prefix: str) -> dict[str, int]:
    """Return ``{path relative to the prefix: size}`` of every object under ``<prefix>/``.

    Args:
        s3: S3 port.
        bucket: Models bucket.
        prefix: Model prefix (no trailing slash).

    Returns:
        Every object of every page.
    """
    found: dict[str, int] = {}
    request: dict[str, str] = {"Bucket": bucket, "Prefix": f"{prefix}/"}
    while True:
        page = s3.list_objects_v2(**request)
        for obj in page.get("Contents", []):
            found[obj["Key"][len(prefix) + 1 :]] = int(obj["Size"])
        next_token = page.get("NextContinuationToken")
        if not page.get("IsTruncated") or not next_token:
            return found
        request["ContinuationToken"] = next_token


def parse_marker(raw: bytes) -> dict[str, str]:
    """Parse a ``.done`` marker (``key=value`` lines; anything else yields no field).

    Args:
        raw: The object body.

    Returns:
        The fields.
    """
    fields: dict[str, str] = {}
    for line in raw.decode("utf-8", "replace").splitlines():
        key, sep, value = line.partition("=")
        if sep:
            fields[key.strip()] = value.strip()
    return fields


def select_files(entries: Iterable[object]) -> list[RepoFile]:
    """Keep the files a server needs; ``*.bin`` weights only when the repo has no safetensors.

    Args:
        entries: The repo tree (files and folders).

    Returns:
        The selected files, sorted by path.
    """
    files = [e for e in entries if isinstance(e, RepoFile)]
    has_safetensors = any(f.path.endswith(".safetensors") for f in files)
    chosen = []
    for f in files:
        if f.path.endswith((".bin", ".bin.index.json")):
            keep = not has_safetensors
        else:
            keep = f.path.endswith(".safetensors") or any(
                fnmatch.fnmatchcase(f.path, p) for p in METADATA_PATTERNS
            )
        if keep:
            chosen.append(f)
    return sorted(chosen, key=lambda f: f.path)


def sha256_of(path: Path) -> str:
    """Return the hex sha256 of a file (streamed).

    Args:
        path: Local file.

    Returns:
        The digest.
    """
    with path.open("rb") as fh:
        return hashlib.file_digest(fh, "sha256").hexdigest()


def gated_no_token(cfg: Config, gated: object) -> StageError:
    """Return the fail-fast error of a gated model without HF_TOKEN.

    Args:
        cfg: Run configuration.
        gated: What the Hub reports (``auto`` or ``manual``).

    Returns:
        The error.
    """
    return StageError(
        EXIT_GATED_NO_TOKEN,
        f"{cfg.model_id} is a gated model (gated={gated}) and HF_TOKEN is not set: accept the "
        f"licence on {cfg.page} with your Hugging Face account, create a read token at "
        f"{TOKENS_PAGE}, `export HF_TOKEN=<read token>` in the shell that runs "
        "scripts/10-stage-model.sh, and re-run it",
    )


def classify(exc: Exception, cfg: Config, *, token_sent: bool) -> StageError:
    """Map a Hub or transfer exception to an exit code and an actionable message.

    Args:
        exc: The exception.
        cfg: Run configuration.
        token_sent: Whether HF_TOKEN was sent with the Hub calls.

    Returns:
        The error to report.
    """
    status = exc.response.status_code if isinstance(exc, hf_errors.HfHubHTTPError) else None
    # GatedRepoError subclasses RepositoryNotFoundError, and the Hub answers 401 for a missing
    # repo: the gated and not-found classes come before the bare 401/403.
    gated = isinstance(exc, hf_errors.GatedRepoError)
    if isinstance(exc, hf_errors.RevisionNotFoundError):
        return StageError(
            EXIT_NOT_FOUND,
            f"revision {cfg.revision} not found in {cfg.model_id}: check MODEL_REVISION "
            f"against {cfg.page}/commits/main",
        )
    if not gated and isinstance(exc, hf_errors.RepositoryNotFoundError):
        return StageError(
            EXIT_NOT_FOUND,
            f"model {cfg.model_id} not found on Hugging Face (a private repo also answers "
            "not found): check MODEL_ID",
        )
    if gated or status in (401, 403):
        if not token_sent:
            return gated_no_token(cfg, "auto or manual")
        return StageError(
            EXIT_ACCESS_DENIED,
            f"Hugging Face refused HF_TOKEN for {cfg.model_id} (HTTP {status}): accept the "
            f"licence on {cfg.page} with the account that owns the token (a manually gated model "
            "also waits for the author's approval), and use a valid read token "
            f"({TOKENS_PAGE})",
        )
    return StageError(EXIT_TRANSFER, f"transfer failed: {type(exc).__name__}: {exc}")


def stage_file(cfg: Config, hf: HubPort, s3: S3Port, entry: RepoFile, token: str | bool) -> None:
    """Download one file, check it against the Hub listing, upload it and delete the local copy.

    Args:
        cfg: Run configuration.
        hf: Hugging Face port.
        s3: S3 port.
        entry: The file as the repo tree lists it.
        token: The token to send, or False.

    Raises:
        StageError: The download does not match the listed size or sha256 (EXIT_VERIFY).
    """
    scratch = Path(tempfile.mkdtemp(prefix="file.", dir=cfg.work_dir))
    try:
        local = Path(
            hf.hf_hub_download(
                cfg.model_id, entry.path, revision=cfg.revision, local_dir=scratch, token=token
            )
        )
        size = local.stat().st_size
        if size != entry.size:
            raise StageError(
                EXIT_VERIFY,
                f"size mismatch for {entry.path}: the Hub lists {entry.size} bytes, the download "
                f"has {size}",
            )
        if entry.lfs is not None and sha256_of(local) != entry.lfs.sha256:
            raise StageError(
                EXIT_VERIFY,
                f"sha256 mismatch for {entry.path}: the download differs from the Hub's "
                f"{entry.lfs.sha256} (not uploaded)",
            )
        s3.upload_file(
            Filename=str(local),
            Bucket=cfg.bucket,
            Key=f"{cfg.prefix}/{entry.path}",
            ExtraArgs={"ChecksumAlgorithm": "SHA256"},
        )
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def read_gated(cfg: Config, hf: HubPort) -> object:
    """Return the ``gated`` field of the model, asked without a token.

    Args:
        cfg: Run configuration.
        hf: Hugging Face port.

    Returns:
        False for a public model, else what the Hub reports.
    """
    try:
        info = hf.model_info(cfg.model_id, revision=cfg.revision, token=False)
    except hf_errors.GatedRepoError:
        return "auto or manual"  # even the metadata needs a token
    return getattr(info, "gated", False) or False


def stage(cfg: Config, hf: HubPort, s3: S3Port) -> int:
    """Run the staging flow (module docstring) and return the exit code.

    Args:
        cfg: Run configuration.
        hf: Hugging Face port.
        s3: S3 port.

    Returns:
        One of the EXIT_* constants.
    """
    token_sent = False
    try:
        present = list_objects(s3, cfg.bucket, cfg.prefix)
        if MARKER in present:
            body = s3.get_object(Bucket=cfg.bucket, Key=f"{cfg.prefix}/{MARKER}")["Body"].read()
            marker = parse_marker(body)
            if marker.get("model_id") == cfg.model_id and marker.get("revision") == cfg.revision:
                LOG.info(
                    "event=already_staged uri=%s/ revision=%s file_count=%s",
                    cfg.uri,
                    cfg.revision,
                    marker.get("file_count", "?"),
                )
                return EXIT_OK
            LOG.info("event=stale_marker revision=%s action=restage", marker.get("revision", "?"))

        gated = read_gated(cfg, hf)
        if gated and cfg.token is None:
            raise gated_no_token(cfg, gated)
        token: str | bool = cfg.token if gated and cfg.token else False
        token_sent = bool(token)
        LOG.info(
            "event=model model=%s revision=%s gated=%s auth=%s",
            cfg.model_id,
            cfg.revision,
            gated,
            "bearer" if token_sent else "none",
        )
        tree = hf.list_repo_tree(cfg.model_id, recursive=True, revision=cfg.revision, token=token)
        files = select_files(tree)
        if not any(f.path.endswith((".safetensors", ".bin")) for f in files):
            raise StageError(
                EXIT_NOT_FOUND,
                f"no safetensors or .bin weights in {cfg.model_id}@{cfg.revision} (other formats "
                "are not staged)",
            )
        total = sum(f.size for f in files)
        LOG.info("event=plan files=%d bytes=%d uri=%s/", len(files), total, cfg.uri)
        for n, entry in enumerate(files, 1):
            if present.get(entry.path) == entry.size:
                LOG.info("event=skip n=%d path=%s bytes=%d reason=in-s3", n, entry.path, entry.size)
                continue
            stage_file(cfg, hf, s3, entry, token)
            LOG.info("event=uploaded n=%d path=%s bytes=%d", n, entry.path, entry.size)

        stored = list_objects(s3, cfg.bucket, cfg.prefix)
        for entry in files:
            got = stored.get(entry.path)
            if got != entry.size:
                have = "no object" if got is None else f"{got} bytes"
                raise StageError(
                    EXIT_VERIFY,
                    f"S3 check failed for {entry.path}: the Hub lists {entry.size} bytes, "
                    f"{cfg.uri}/{entry.path} has {have}",
                )
        marker_body = (
            f"model_id={cfg.model_id}\nrevision={cfg.revision}\n"
            f"file_count={len(files)}\ntotal_bytes={total}\n"
        )
        s3.put_object(
            Bucket=cfg.bucket,
            Key=f"{cfg.prefix}/{MARKER}",
            Body=marker_body.encode(),
            ContentType="text/plain",
            ChecksumAlgorithm="SHA256",
        )
        LOG.info("event=staged uri=%s/ file_count=%d bytes=%d", cfg.uri, len(files), total)
        return EXIT_OK
    except StageError as exc:
        LOG.error("event=failed exit=%d reason=%s", exc.code, exc)
        return exc.code
    except Exception as exc:  # noqa: BLE001 - the Job's boundary: classify, log redacted, exit code
        err = classify(exc, cfg, token_sent=token_sent)
        LOG.error("event=failed exit=%d reason=%s", err.code, err)
        return err.code


def main(
    argv: list[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    hf: HubPort | None = None,
    s3: S3Port | None = None,
    work_dir: Path | None = None,
) -> int:
    """Validate the environment and stage the model.

    Args:
        argv: Command line without the program name (no options besides --help).
        environ: Environment (default: ``os.environ``).
        hf: Hugging Face port (default: ``huggingface_hub.HfApi()``).
        s3: S3 port (default: a boto3 S3 client in AWS_REGION).
        work_dir: Scratch directory (default: WORK_DIR, else the system temp dir).

    Returns:
        One of the EXIT_* constants.
    """
    argparse.ArgumentParser(description="Stage a Hugging Face model into S3.").parse_args(argv)
    try:
        cfg = load_config(os.environ if environ is None else environ, work_dir)
    except ConfigError as exc:
        LOG.error("event=config_error reason=%s", exc)
        return EXIT_CONFIG
    redactor = _Redactor(cfg.token)
    LOG.addFilter(redactor)
    try:
        cfg.work_dir.mkdir(parents=True, exist_ok=True)
        hub: HubPort = hf if hf is not None else HfApi()
        client: S3Port = s3 if s3 is not None else boto3.client("s3", region_name=cfg.region)
        return stage(cfg, hub, client)
    finally:
        LOG.removeFilter(redactor)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO, stream=sys.stderr, format="stage_model %(levelname)s %(message)s"
    )
    sys.exit(main())
