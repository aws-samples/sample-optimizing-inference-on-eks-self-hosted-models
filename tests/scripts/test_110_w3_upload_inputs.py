# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""W3-16K inputs upload (``workloads/w3-16k/upload-inputs.sh``) and the docs of the inputs flow.

Contract
========
``upload-inputs.sh [--dry-run|--yes] <rate> <trace-dir> <model-dir-or-tokenizer-dir> [results-bucket]``

* ``<rate>`` is one of the rates with a shipped plan (10, 25, 32); the destinations are read from
  ``plans/w3-16k/plan-w3-r<rate>.txt``: ``w2 trace=`` and ``sidecar=`` for the two files ``regen.sh`` writes,
  the directory of ``trace=`` for their ``SHA256SUMS``, ``tokenizer prefix=`` for the tokenizer files and the
  ``SHA256SUMS`` the script writes for them.  ``${RESULTS_BUCKET}`` of the plan becomes the bucket.
* Bucket: ``[results-bucket]``, else ``$RESULTS_BUCKET``, else ``RESULTS_BUCKET`` of the generated platform.env
  (``$PLATFORM_ENV``); none of them is a usage error (exit 2).  The script carries no bucket literal.
* Before any upload: the trace and the sidecar match ``trace_sha256`` / ``sidecar_sha256`` of the plan and the
  ``SHA256SUMS`` of ``<trace-dir>`` lists exactly those two lines; a mismatch or a missing file exits 1 with no
  ``aws`` call.  A tokenizer directory without ``tokenizer_config.json`` exits 1 the same way.
* Tokenizer file set: the files of ``TOKENIZER_FILES`` present in the directory (symlinks of an HF cache snapshot
  are followed); weights and anything else are never uploaded.  Its ``SHA256SUMS`` is ``sha256sum`` format, sorted by
  name.  stdout carries ``sums_sha256=<sha256 of that SHA256SUMS>``; stderr says whether the plan pins the same value.
* Default and ``--dry-run``: print one ``aws s3 cp <src> <dst> ...`` line per file on stdout and upload nothing.
  ``--yes``: print the same lines and run them, in order; the first failing upload stops the script (exit 1).
* ``--region`` is added only when the platform.env names ``AWS_REGION``.  No temporary file is left behind.
* Usage errors exit 2.  bash 3.2 compatible, ShellCheck clean.

The traces are regenerated with ``regen.sh`` (offline, a few seconds) so every check runs against the shipped plans.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import lib_proc
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
W3 = REPO_ROOT / "workloads" / "w3-16k"
UPLOAD = W3 / "upload-inputs.sh"
REGEN = W3 / "regen.sh"
README = W3 / "README.md"
PARAMS = W3 / "params.json"
PLANS = REPO_ROOT / "plans" / "w3-16k"
DEPLOY = REPO_ROOT / "docs" / "DEPLOY.md"
DEMO_SRC = REPO_ROOT / "tools" / "demo_bench" / "src"
SPDX = "# SPDX-License-Identifier: MIT-0"

RATES = (10, 25, 32)  # the rates with a shipped plan
TOKENIZER_FILES = ("added_tokens.json", "chat_template.jinja", "config.json", "merges.txt", "special_tokens_map.json",
                   "tokenizer.json", "tokenizer.model", "tokenizer_config.json", "vocab.json")
NOT_TOKENIZER = ("model-00001-of-00002.safetensors", "model.safetensors.index.json", "generation_config.json",
                 "README.md", ".gitattributes")
PROVENANCE = "n=1 per cell and rate; a different workload from W2, never pooled with it"

FAKE_AWS = r'''#!__PY__
import json, os, shutil, sys
from pathlib import Path

argv = sys.argv[1:]
with open(os.environ["FAKE_AWS_LOG"], "a") as fh:
    fh.write(json.dumps(argv) + "\n")
if os.environ.get("FAKE_AWS_FAIL"):
    sys.stderr.write("fake aws: upload failed\n")
    sys.exit(1)
if argv[:2] != ["s3", "cp"] or not argv[3].startswith("s3://"):
    sys.stderr.write("fake aws: unexpected call %r\n" % (argv,))
    sys.exit(2)
target = Path(os.environ["FAKE_S3"]) / argv[3][len("s3://"):]
target.parent.mkdir(parents=True, exist_ok=True)
shutil.copyfile(argv[2], target)
'''


# ------------------------------------------------------------------------------------------- helpers
def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _plan_inputs(rate: int, name: str | None = None) -> dict[str, dict[str, str]]:
    """The w2 and tokenizer fields of a shipped plan, parsed like the driver (``#`` starts a comment)."""
    fields: dict[str, dict[str, str]] = {}
    for line in (PLANS / f"{name or f'plan-w3-r{rate}'}.txt").read_text().splitlines():
        body = line.split("#", 1)[0].split()
        if body and body[0] in ("w2", "tokenizer"):
            fields[body[0]] = dict(tok.split("=", 1) for tok in body[1:])
    return fields


def _expected(rate: int, bucket: str, tok_names: list[str]) -> list[tuple[str, str]]:
    """(source basename, destination) in upload order for ``rate`` and ``bucket``."""
    f = _plan_inputs(rate)
    trace = f["w2"]["trace"].replace("${RESULTS_BUCKET}", bucket)
    side = f["w2"]["sidecar"].replace("${RESULTS_BUCKET}", bucket)
    prefix = f["tokenizer"]["prefix"].replace("${RESULTS_BUCKET}", bucket)
    out = [(f"w3-16k-r{rate}.jsonl", trace), (f"w3-16k-r{rate}.side.jsonl", side),
           ("SHA256SUMS", trace.rsplit("/", 1)[0] + "/SHA256SUMS")]
    out += [(n, prefix + n) for n in sorted(tok_names)]
    out.append(("SHA256SUMS", prefix + "SHA256SUMS"))
    return out


class Harness:
    """A fake ``aws`` first on PATH (logs argv, copies ``s3 cp`` sources into a fake S3 tree) and a clean env."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.bin = root / "bin"
        self.s3 = root / "s3"
        self.tmp = root / "tmpdir"
        self.log = root / "aws-calls.jsonl"
        for d in (self.bin, self.s3, self.tmp):
            d.mkdir(parents=True)
        aws = self.bin / "aws"
        aws.write_text(FAKE_AWS.replace("__PY__", sys.executable))
        aws.chmod(0o755)

    def env(self, **extra: str) -> dict[str, str]:
        env = {"PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}", "HOME": str(self.root), "TMPDIR": str(self.tmp),
               "FAKE_AWS_LOG": str(self.log), "FAKE_S3": str(self.s3), "PLATFORM_ENV": str(self.root / "absent.env")}
        env.update(extra)
        return env

    def run(self, *args: str, **extra: str) -> subprocess.CompletedProcess[str]:
        assert UPLOAD.is_file(), f"missing {UPLOAD.relative_to(REPO_ROOT)}"
        return subprocess.run(["bash", str(UPLOAD), *args], capture_output=True, text=True, env=self.env(**extra),
                              cwd=REPO_ROOT, timeout=120, check=False)

    def calls(self) -> list[list[str]]:
        if not self.log.exists():
            return []
        return [json.loads(ln) for ln in self.log.read_text().splitlines() if ln.strip()]

    def object(self, plan_uri: str, bucket: str) -> Path:
        """Where the fake S3 keeps the object (or prefix) a plan URI names in ``bucket``."""
        return self.s3 / plan_uri.replace("${RESULTS_BUCKET}", bucket)[len("s3://"):]

    def s3_files(self) -> list[Path]:
        return sorted(p for p in self.s3.rglob("*") if p.is_file())

    def platform_env(self, **values: str) -> Path:
        path = self.root / "platform.env"
        path.write_text("# generated\n" + "".join(f"{k}={v}\n" for k, v in values.items()))
        return path


def _commands(stdout: str) -> list[list[str]]:
    """The printed aws command lines, as argv without the leading ``aws``."""
    out = []
    for line in stdout.splitlines():
        tok = shlex.split(line)
        if tok and tok[0] == "aws":
            out.append(tok[1:])
    return out


def _pairs(cmds: list[list[str]]) -> list[tuple[str, str]]:
    for c in cmds:
        assert c[:2] == ["s3", "cp"], c
    return [(Path(c[2]).name, c[3]) for c in cmds]


def _flag(cmd: list[str], name: str) -> str | None:
    return cmd[cmd.index(name) + 1] if name in cmd else None


def _tree(path: Path) -> list[str]:
    return sorted(str(p.relative_to(path)) for p in path.rglob("*"))


# ------------------------------------------------------------------------------------------- fixtures
@pytest.fixture(scope="module")
def traces(tmp_path_factory: pytest.TempPathFactory) -> dict[int, Path]:
    """The window trace, sidecar and SHA256SUMS of every planned rate, regenerated by regen.sh."""
    env = dict(os.environ)
    env["DEMO_BENCH"] = f"{shlex.quote(sys.executable)} -m demo_bench"
    env["PYTHONPATH"] = str(DEMO_SRC)
    out = {}
    for rate in RATES:
        d = tmp_path_factory.mktemp(f"r{rate}")
        res = subprocess.run(["bash", str(REGEN), str(rate), str(d)], capture_output=True, text=True, env=env,
                             cwd=REPO_ROOT, timeout=600, check=False)
        assert res.returncode == 0, res.stdout + res.stderr
        out[rate] = d
    return out


@pytest.fixture
def h(tmp_path: Path) -> Harness:
    return Harness(tmp_path / "h")


@pytest.fixture
def trace_dir(traces: dict[int, Path], tmp_path: Path) -> Path:
    """A private copy of the r10 outputs (tests may corrupt it)."""
    d = tmp_path / "trace-r10"
    shutil.copytree(traces[10], d)
    return d


@pytest.fixture
def model_dir(tmp_path: Path) -> Path:
    """An HF-cache-like snapshot: tokenizer files (one a symlink to a blob) next to weights and other files."""
    blobs = tmp_path / "blobs"
    blobs.mkdir()
    (blobs / "abc123").write_bytes(b'{"model": {"type": "BPE"}, "fake": "tokenizer.json blob"}\n')
    snap = tmp_path / "snapshot"
    snap.mkdir()
    (snap / "tokenizer.json").symlink_to(blobs / "abc123")
    (snap / "tokenizer_config.json").write_text('{"tokenizer_class": "Qwen2Tokenizer"}\n')
    (snap / "vocab.json").write_text('{"a": 0}\n')
    (snap / "merges.txt").write_text("#version: 0.2\na b\n")
    (snap / "config.json").write_text('{"model_type": "qwen3"}\n')
    for name in NOT_TOKENIZER:
        (snap / name).write_bytes(b"not a tokenizer file\n")
    return snap


TOK_UPLOADED = ["config.json", "merges.txt", "tokenizer.json", "tokenizer_config.json", "vocab.json"]


# ------------------------------------------------------------------------------------------- layout and hygiene
def test_script_exists_is_executable_strict_and_licensed() -> None:
    assert UPLOAD.is_file(), f"missing {UPLOAD.relative_to(REPO_ROOT)}"
    assert os.access(UPLOAD, os.X_OK), "upload-inputs.sh must be executable"
    lines = UPLOAD.read_text().splitlines()
    assert lines[0] == "#!/usr/bin/env bash"
    assert SPDX in lines[:4]
    assert "set -euo pipefail" in UPLOAD.read_text()


def test_script_passes_shellcheck() -> None:
    if shutil.which("shellcheck") is None:
        pytest.skip("shellcheck not installed")
    assert UPLOAD.is_file()
    res = subprocess.run(["shellcheck", "-x", "--shell=bash", str(UPLOAD)], capture_output=True, text=True, check=False)
    assert res.returncode == 0, res.stdout + res.stderr


def test_script_is_bash_32_portable() -> None:
    assert UPLOAD.is_file()
    text = UPLOAD.read_text()
    for pat, what in ((r"\bmapfile\b|\breadarray\b", "mapfile/readarray"), (r"\b(?:declare|local|typeset) -A\b", "associative array"),
                      (r"\$\{[A-Za-z_][A-Za-z_0-9]*(?:,,|\^\^)", "case conversion"), (r"&>>", "&>>"), (r"\bcoproc\b", "coproc")):
        assert not re.search(pat, text), f"bash 4+ construct: {what}"
    assert subprocess.run(["bash", "-n", str(UPLOAD)], capture_output=True, check=False).returncode == 0


def test_script_carries_no_bucket_literal() -> None:
    assert UPLOAD.is_file()
    text = UPLOAD.read_text()
    assert "s3://" not in text, "destinations come from the plan, never from the script"
    assert not re.search(r"RESULTS_BUCKET:-[A-Za-z0-9]", text), "no default bucket"
    assert not re.search(r"(?<![0-9])[0-9]{12}(?![0-9])", text), "no account id"


# ------------------------------------------------------------------------------------------- usage
@pytest.mark.parametrize("args", [
    [], ["10"], ["10", "TRACE"], ["10", "TRACE", "MODEL", "bucket-a", "extra"], ["50", "TRACE", "MODEL"],
    ["7", "TRACE", "MODEL"], ["abc", "TRACE", "MODEL"], ["", "TRACE", "MODEL"], ["--bogus", "10", "TRACE", "MODEL"],
    ["--dry-run", "--yes", "10", "TRACE", "MODEL"],
], ids=["none", "rate-only", "no-model", "too-many", "rate-50-has-no-plan", "rate-7", "rate-abc", "rate-empty",
        "unknown-option", "dry-run-and-yes"])
def test_usage_errors_exit_2_without_aws(h: Harness, trace_dir: Path, model_dir: Path, args: list[str]) -> None:
    argv = [{"TRACE": str(trace_dir), "MODEL": str(model_dir)}.get(a, a) for a in args]
    res = h.run(*argv, RESULTS_BUCKET="bucket-a")
    assert res.returncode == 2, res.stdout + res.stderr
    assert "usage" in res.stderr.lower()
    assert h.calls() == [] and h.s3_files() == []


# ------------------------------------------------------------------------------------------- bucket
def test_no_bucket_anywhere_is_refused(h: Harness, trace_dir: Path, model_dir: Path) -> None:
    res = h.run("--yes", "10", str(trace_dir), str(model_dir))
    assert res.returncode == 2, res.stdout + res.stderr
    assert "RESULTS_BUCKET" in res.stderr
    assert h.calls() == [] and h.s3_files() == []


def test_bucket_from_the_environment_wins_over_platform_env(h: Harness, trace_dir: Path, model_dir: Path) -> None:
    pe = h.platform_env(RESULTS_BUCKET="bucket-from-file", AWS_REGION="us-east-2")
    res = h.run("10", str(trace_dir), str(model_dir), RESULTS_BUCKET="bucket-from-env", PLATFORM_ENV=str(pe))
    assert res.returncode == 0, res.stdout + res.stderr
    dsts = [d for _, d in _pairs(_commands(res.stdout))]
    assert dsts and all(d.startswith("s3://bucket-from-env/") for d in dsts), dsts


def test_bucket_and_region_from_platform_env_when_the_environment_has_none(h: Harness, trace_dir: Path, model_dir: Path) -> None:
    pe = h.platform_env(RESULTS_BUCKET="bucket-from-file", AWS_REGION="us-east-2")
    res = h.run("10", str(trace_dir), str(model_dir), PLATFORM_ENV=str(pe))
    assert res.returncode == 0, res.stdout + res.stderr
    cmds = _commands(res.stdout)
    assert cmds and all(c[3].startswith("s3://bucket-from-file/") for c in cmds)
    assert {_flag(c, "--region") for c in cmds} == {"us-east-2"}


def test_no_region_flag_without_platform_env(h: Harness, trace_dir: Path, model_dir: Path) -> None:
    res = h.run("10", str(trace_dir), str(model_dir), RESULTS_BUCKET="bucket-from-env", AWS_REGION="eu-west-1")
    assert res.returncode == 0, res.stdout + res.stderr
    cmds = _commands(res.stdout)
    assert cmds and all("--region" not in c for c in cmds), "the ambient AWS_REGION is never used"


def test_bucket_argument_wins(h: Harness, trace_dir: Path, model_dir: Path) -> None:
    pe = h.platform_env(RESULTS_BUCKET="bucket-from-file")
    res = h.run("10", str(trace_dir), str(model_dir), "bucket-from-arg", RESULTS_BUCKET="bucket-from-env", PLATFORM_ENV=str(pe))
    assert res.returncode == 0, res.stdout + res.stderr
    dsts = [d for _, d in _pairs(_commands(res.stdout))]
    assert dsts and all(d.startswith("s3://bucket-from-arg/") for d in dsts), dsts


@pytest.mark.parametrize("bad", ["Bad_Bucket", "a/b", "x", "bucket with space", "${RESULTS_BUCKET}"])
def test_invalid_bucket_name_is_refused(h: Harness, trace_dir: Path, model_dir: Path, bad: str) -> None:
    res = h.run("--yes", "10", str(trace_dir), str(model_dir), RESULTS_BUCKET=bad)
    assert res.returncode == 2, res.stdout + res.stderr
    assert h.calls() == [] and h.s3_files() == []


# ------------------------------------------------------------------------------------------- dry run
@pytest.mark.parametrize("flag", [[], ["--dry-run"]], ids=["default", "--dry-run"])
def test_dry_run_is_the_default_and_uploads_nothing(h: Harness, trace_dir: Path, model_dir: Path, flag: list[str]) -> None:
    before = (_tree(trace_dir), _tree(model_dir))
    res = h.run(*flag, "10", str(trace_dir), str(model_dir), RESULTS_BUCKET="bucket-a")
    assert res.returncode == 0, res.stdout + res.stderr
    assert len(_commands(res.stdout)) == 3 + len(TOK_UPLOADED) + 1
    assert h.calls() == [] and h.s3_files() == []
    assert (_tree(trace_dir), _tree(model_dir)) == before, "a dry run writes nothing next to the inputs"
    assert "--yes" in res.stderr, "the dry run says how to upload"
    assert _tree(h.tmp) == [], "no temporary file is left behind"


@pytest.mark.parametrize("rate", RATES)
def test_destinations_are_exactly_the_prefixes_of_the_plan(h: Harness, traces: dict[int, Path], model_dir: Path, rate: int) -> None:
    res = h.run(str(rate), str(traces[rate]), str(model_dir), RESULTS_BUCKET="bucket-a")
    assert res.returncode == 0, res.stdout + res.stderr
    assert _pairs(_commands(res.stdout)) == _expected(rate, "bucket-a", TOK_UPLOADED)


@pytest.mark.parametrize("rate", RATES)
def test_the_cells_plan_of_a_rate_reads_the_same_inputs(rate: int) -> None:
    assert _plan_inputs(rate) == _plan_inputs(rate, f"plan-w3-r{rate}-cells")


def test_sources_are_the_files_of_the_given_directories(h: Harness, trace_dir: Path, model_dir: Path) -> None:
    res = h.run("10", str(trace_dir), str(model_dir), RESULTS_BUCKET="bucket-a")
    assert res.returncode == 0, res.stdout + res.stderr
    srcs = [Path(c[2]) for c in _commands(res.stdout)]
    assert [s.parent for s in srcs[:3]] == [trace_dir] * 3
    assert [s.parent for s in srcs[3:-1]] == [model_dir] * len(TOK_UPLOADED)
    assert srcs[-1].name == "SHA256SUMS" and srcs[-1].parent != model_dir, "the tokenizer SHA256SUMS is written elsewhere"


# ------------------------------------------------------------------------------------------- --yes
def test_yes_uploads_exactly_the_printed_files_in_order(h: Harness, trace_dir: Path, model_dir: Path) -> None:
    res = h.run("--yes", "10", str(trace_dir), str(model_dir), RESULTS_BUCKET="bucket-a")
    assert res.returncode == 0, res.stdout + res.stderr
    printed = _commands(res.stdout)
    assert h.calls() == printed
    assert _pairs(printed) == _expected(10, "bucket-a", TOK_UPLOADED)
    f = _plan_inputs(10)
    assert _sha(h.object(f["w2"]["trace"], "bucket-a")) == f["w2"]["trace_sha256"]
    assert _sha(h.object(f["w2"]["sidecar"], "bucket-a")) == f["w2"]["sidecar_sha256"]
    sums = h.object(f["w2"]["trace"], "bucket-a").with_name("SHA256SUMS")
    assert sums.read_bytes() == (trace_dir / "SHA256SUMS").read_bytes()
    prefix = h.object(f["tokenizer"]["prefix"], "bucket-a")
    assert sorted(p.name for p in prefix.iterdir()) == sorted([*TOK_UPLOADED, "SHA256SUMS"])
    for name in TOK_UPLOADED:
        assert (prefix / name).read_bytes() == (model_dir / name).read_bytes(), name  # the symlink is followed
    assert not any(n in {p.name for p in h.s3_files()} for n in NOT_TOKENIZER), "weights and other files stay local"
    assert _tree(h.tmp) == []


def test_tokenizer_sha256sums_is_sorted_sha256sum_format_and_its_sha256_is_printed(h: Harness, trace_dir: Path, model_dir: Path) -> None:
    res = h.run("--yes", "10", str(trace_dir), str(model_dir), RESULTS_BUCKET="bucket-a")
    assert res.returncode == 0, res.stdout + res.stderr
    f = _plan_inputs(10)
    sums = h.object(f["tokenizer"]["prefix"], "bucket-a") / "SHA256SUMS"
    want = "".join(f"{_sha(model_dir / n)}  {n}\n" for n in sorted(TOK_UPLOADED))
    assert sums.read_text() == want
    printed = re.findall(r"(?m)^sums_sha256=([0-9a-f]{64})$", res.stdout)
    assert printed == [_sha(sums)]


def test_a_different_tokenizer_set_is_reported_against_both_plans(h: Harness, trace_dir: Path, model_dir: Path) -> None:
    res = h.run("10", str(trace_dir), str(model_dir), RESULTS_BUCKET="bucket-a")
    assert res.returncode == 0, res.stdout + res.stderr
    pinned = _plan_inputs(10)["tokenizer"]["sums_sha256"]
    assert "differs" in res.stderr and pinned in res.stderr
    assert "plan-w3-r10.txt" in res.stderr and "plan-w3-r10-cells.txt" in res.stderr


def test_the_dry_run_prints_the_same_sums_sha256_as_the_upload(h: Harness, trace_dir: Path, model_dir: Path) -> None:
    dry = h.run("10", str(trace_dir), str(model_dir), RESULTS_BUCKET="bucket-a")
    yes = h.run("--yes", "10", str(trace_dir), str(model_dir), RESULTS_BUCKET="bucket-a")
    assert dry.returncode == yes.returncode == 0, dry.stderr + yes.stderr
    printed = [re.findall(r"(?m)^sums_sha256=[0-9a-f]{64}$", r.stdout) for r in (dry, yes)]
    assert printed[0] == printed[1] and len(printed[0]) == 1


def test_a_failed_upload_stops_and_exits_1(h: Harness, trace_dir: Path, model_dir: Path) -> None:
    res = h.run("--yes", "10", str(trace_dir), str(model_dir), RESULTS_BUCKET="bucket-a", FAKE_AWS_FAIL="1")
    assert res.returncode == 1, res.stdout + res.stderr
    assert len(h.calls()) == 1, "nothing is uploaded after the first failure"
    assert _tree(h.tmp) == []


# ------------------------------------------------------------------------------------------- verification first
def _corrupt(trace_dir: Path, what: str) -> None:
    if what == "trace":
        with (trace_dir / "w3-16k-r10.jsonl").open("ab") as fh:
            fh.write(b"\n")
    elif what == "sidecar":
        p = trace_dir / "w3-16k-r10.side.jsonl"
        p.write_bytes(p.read_bytes()[:-2] + b"\n")
    elif what == "sums-wrong-hash":
        lines = (trace_dir / "SHA256SUMS").read_text().splitlines()
        (trace_dir / "SHA256SUMS").write_text("0" * 64 + lines[0][64:] + "\n" + lines[1] + "\n")
    elif what == "sums-extra-line":
        with (trace_dir / "SHA256SUMS").open("a") as fh:
            fh.write("0" * 64 + "  other.jsonl\n")
    elif what == "missing-sidecar":
        (trace_dir / "w3-16k-r10.side.jsonl").unlink()
    elif what == "missing-sums":
        (trace_dir / "SHA256SUMS").unlink()


@pytest.mark.parametrize("what", ["trace", "sidecar", "sums-wrong-hash", "sums-extra-line", "missing-sidecar", "missing-sums"])
@pytest.mark.parametrize("mode", [["--yes"], []], ids=["--yes", "dry-run"])
def test_a_trace_that_does_not_match_the_plan_aborts_before_any_upload(h: Harness, trace_dir: Path, model_dir: Path,
                                                                       what: str, mode: list[str]) -> None:
    _corrupt(trace_dir, what)
    res = h.run(*mode, "10", str(trace_dir), str(model_dir), RESULTS_BUCKET="bucket-a")
    assert res.returncode == 1, res.stdout + res.stderr
    assert h.calls() == [] and h.s3_files() == []
    assert _commands(res.stdout) == [], "no upload command is printed for inputs that fail the check"
    assert re.search(r"(?i)sha256|missing|SHA256SUMS", res.stderr)
    assert _tree(h.tmp) == []


def test_the_trace_dir_of_another_rate_aborts(h: Harness, trace_dir: Path, model_dir: Path) -> None:
    res = h.run("--yes", "25", str(trace_dir), str(model_dir), RESULTS_BUCKET="bucket-a")
    assert res.returncode == 1, res.stdout + res.stderr
    assert h.calls() == []


def test_a_mismatch_names_the_plan_and_both_hashes(h: Harness, trace_dir: Path, model_dir: Path) -> None:
    _corrupt(trace_dir, "trace")
    res = h.run("--yes", "10", str(trace_dir), str(model_dir), RESULTS_BUCKET="bucket-a")
    assert res.returncode == 1
    assert _plan_inputs(10)["w2"]["trace_sha256"] in res.stderr
    assert _sha(trace_dir / "w3-16k-r10.jsonl") in res.stderr
    assert "plan-w3-r10.txt" in res.stderr


@pytest.mark.parametrize("what", ["no-tokenizer-config", "missing-dir", "weights-only"])
def test_a_directory_without_the_tokenizer_aborts_before_any_upload(h: Harness, trace_dir: Path, model_dir: Path,
                                                                    tmp_path: Path, what: str) -> None:
    if what == "no-tokenizer-config":
        (model_dir / "tokenizer_config.json").unlink()
        target = model_dir
    elif what == "missing-dir":
        target = tmp_path / "nowhere"
    else:
        target = tmp_path / "weights"
        target.mkdir()
        (target / "model.safetensors").write_bytes(b"w")
    res = h.run("--yes", "10", str(trace_dir), str(target), RESULTS_BUCKET="bucket-a")
    assert res.returncode == 1, res.stdout + res.stderr
    assert "tokenizer_config.json" in res.stderr
    assert h.calls() == [] and h.s3_files() == []


# ------------------------------------------------------------------------------------------- docs: the inputs flow
def _deploy_step_2() -> str:
    text = DEPLOY.read_text()
    start = text.index("2. **Inputs.**")
    return text[start:text.index("3. **Render and launch.**", start)]


def test_deploy_step_2_is_the_upload_flow() -> None:
    step = _deploy_step_2()
    assert "No script uploads" not in DEPLOY.read_text()
    for needle in ("workloads/w3-16k/regen.sh", "workloads/w3-16k/upload-inputs.sh", "--yes", "sums_sha256", "RESULTS_BUCKET"):
        assert needle in step, f"DEPLOY.md step 2 lacks {needle!r}"
    assert step.index("regen.sh") < step.index("upload-inputs.sh"), "regenerate, then upload"
    text = DEPLOY.read_text()
    assert text.index("upload-inputs.sh") < text.index("render_driver.py --out out --launch"), "upload before the launch"


# The variables of step 2 come from the two files scripts/gen_env.py generates (section 3 of DEPLOY.md).
DEPLOY_SOURCE = "set -a; source manifests/config/platform.env; source config/sample.env; set +a"
FAKE_AWS_DEPLOY = r'''#!__PY__
import fnmatch, json, os, shutil, sys
from pathlib import Path

argv = sys.argv[1:]
with open(os.environ["FAKE_AWS_LOG"], "a") as fh:
    fh.write(json.dumps(argv) + "\n")
s3 = Path(os.environ["FAKE_S3"])
if argv[:3] == ["s3", "cp", "--recursive"] and argv[3].startswith("s3://"):
    src, dst = s3 / argv[3][len("s3://"):], Path(argv[4])
    excludes = [argv[i + 1] for i, a in enumerate(argv) if a == "--exclude"]
    for f in sorted(p for p in src.rglob("*") if p.is_file()):
        rel = f.relative_to(src).as_posix()
        if not any(fnmatch.fnmatch(rel, g) for g in excludes):
            (dst / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(f, dst / rel)
elif argv[:2] == ["s3", "cp"] and argv[3].startswith("s3://"):
    target = s3 / argv[3][len("s3://"):]
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(argv[2], target)
else:
    sys.stderr.write("fake aws: unexpected call %r\n" % (argv,))
    sys.exit(2)
'''


def _deploy_step_2_block() -> str:
    """The one bash block of step 2, dedented (it is indented under the list item)."""
    blocks = re.findall(r"```bash\n(.*?)\n[ \t]*```", _deploy_step_2(), re.DOTALL)
    assert len(blocks) == 1, "DEPLOY.md step 2 has exactly one bash block"
    return textwrap.dedent(blocks[0])


def _generate_env(root: Path) -> dict[str, str]:
    """Write platform.env and config/sample.env of the placeholder deployment under ``root`` with scripts/gen_env.py.

    Returns:
        The placeholder config (platform.env.example) the outputs were built from.
    """
    example = REPO_ROOT / "manifests" / "config" / "platform.env.example"
    cfg = dict(ln.split("=", 1) for ln in example.read_text().splitlines() if ln and not ln.startswith("#"))
    sample_keys = ("MODEL_ID", "MODEL_REVISION", "RUNTIME_IMAGE", "MODEL_S3_PREFIX")
    plat = {k: v for k, v in cfg.items() if k not in (*sample_keys, "MODEL_S3_URI")}
    samp = {k: cfg[k] for k in sample_keys} | {"RATE": "10", "MAX_ISL": "16384", "SESSIONS_PER_RATE": "75"}
    outputs = {"platform_env": {"value": plat}, "sample_env": {"value": samp}, "gpu_pools": {"value": {"gpu-b200-cb": {}}}}
    src = root / "tf-outputs.json"
    src.write_text(json.dumps(outputs))
    res = lib_proc.run([sys.executable, "-I", str(REPO_ROOT / "scripts" / "gen_env.py"), "--from-json", str(src),
                        "--repo-root", str(root)], capture_output=True, text=True, check=False)
    assert res.returncode == 0, res.stderr
    return cfg


def test_deploy_step_2_runs_as_written_from_a_repository_root(tmp_path: Path) -> None:
    """The bash block of step 2, run unchanged by bash from a repository root: the variables from the two generated
    env files, the two local directories, regen.sh, the tokenizer download and both upload-inputs.sh lines (a fake
    aws stands for S3; DEMO_BENCH replaces `uv run` so the run stays offline)."""
    root = tmp_path / "repo"
    root.mkdir()
    for name in ("workloads", "plans", "tools", "scripts"):
        (root / name).symlink_to(REPO_ROOT / name)
    cfg = _generate_env(root)
    model_src = tmp_path / "s3" / cfg["MODEL_S3_URI"][len("s3://"):]
    model_src.mkdir(parents=True)
    for name in ("tokenizer.json", "tokenizer_config.json", "config.json", "model.safetensors.index.json", ".done"):
        (model_src / name).write_text(f"{name}\n")
    (model_src / "model-00001-of-00001.safetensors").write_bytes(b"weights")
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    (fake_bin / "aws").write_text(FAKE_AWS_DEPLOY.replace("__PY__", sys.executable))
    (fake_bin / "aws").chmod(0o755)
    home, log = tmp_path / "home", tmp_path / "aws-calls.jsonl"
    home.mkdir()
    env = {"PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}", "HOME": str(home), "TMPDIR": str(tmp_path),
           "FAKE_AWS_LOG": str(log), "FAKE_S3": str(tmp_path / "s3"), "LC_ALL": "C",
           "DEMO_BENCH": f"{shlex.quote(sys.executable)} -m demo_bench", "PYTHONPATH": str(DEMO_SRC)}
    res = lib_proc.run(["bash", "-euo", "pipefail", "-c", _deploy_step_2_block()], cwd=root, env=env,
                       capture_output=True, text=True, timeout=600, check=False)
    assert res.returncode == 0, res.stdout[-3000:] + res.stderr[-3000:]
    calls = [json.loads(ln) for ln in log.read_text().splitlines()]
    download = calls[0]
    assert download[:4] == ["s3", "cp", "--recursive", cfg["MODEL_S3_URI"] + "/"], download
    assert _flag(download, "--region") == cfg["AWS_REGION"] and "*.safetensors" in download
    assert res.stdout.count("sums_sha256=") == 2, "the dry run and the upload both print sums_sha256"
    inputs = _plan_inputs(25)
    bucket = cfg["RESULTS_BUCKET"]
    trace = tmp_path / "s3" / inputs["w2"]["trace"].replace("${RESULTS_BUCKET}", bucket)[len("s3://"):]
    assert _sha(trace) == inputs["w2"]["trace_sha256"], "the uploaded trace is the one plan-w3-r25 pins"
    tok = tmp_path / "s3" / inputs["tokenizer"]["prefix"].replace("${RESULTS_BUCKET}", bucket)[len("s3://"):]
    assert sorted(p.name for p in tok.iterdir()) == ["SHA256SUMS", "config.json", "tokenizer.json", "tokenizer_config.json"]
    assert not [p for p in home.rglob("*.safetensors")], "the weights are never downloaded"


def test_deploy_step_2_commands_name_existing_scripts_and_flags() -> None:
    block = _deploy_step_2_block()
    lines = [ln.split("#", 1)[0].strip() for ln in block.splitlines()]
    assert lines[0] == DEPLOY_SOURCE, "the variables are defined first, from the two generated env files"
    mk = next((ln for ln in lines if ln.startswith("mkdir -p ")), "")
    dirs = re.findall(r'"\$(\w+)"', mk)
    assert len(dirs) == 2, f"one mkdir -p of the trace and model directories: {mk!r}"
    for var in dirs:
        assert any(re.match(rf"{var}=", ln) for ln in lines), f"{var} is set before the mkdir"
        assert sum(f'"${var}' in ln for ln in lines) >= 3, f"${var} is created, then used by the commands"
    scripts = {}
    for ln in lines:
        tok = shlex.split(ln) if ln else []
        if tok and tok[0].startswith("workloads/"):
            path = REPO_ROOT / tok[0]
            assert path.is_file() and os.access(path, os.X_OK), f"{tok[0]} is an executable script of the repository"
            usage = re.search(r'echo "usage: .*', path.read_text())
            assert usage, f"{tok[0]} prints a usage line"
            for flag in (t for t in tok[1:] if t.startswith("--")):
                assert flag in usage.group(0), f"{tok[0]} has no flag {flag}"
            scripts.setdefault(tok[0], []).append(tok)
    assert sorted(scripts) == ["workloads/w3-16k/regen.sh", "workloads/w3-16k/upload-inputs.sh"]
    assert [t[1:2] for t in scripts["workloads/w3-16k/upload-inputs.sh"]][-1] == ["--yes"], "the last line uploads"
    aws = [shlex.split(ln) for ln in lines if ln.startswith("aws ")]
    assert len(aws) == 1 and aws[0][:4] == ["aws", "s3", "cp", "--recursive"], aws
    assert aws[0][4] == "$MODEL_S3_URI/" and "--exclude" in aws[0] and _flag(aws[0], "--region") == "$AWS_REGION"


def test_deploy_step_2_states_what_the_plans_read_the_prerequisites_and_the_sums_callout() -> None:
    step = _deploy_step_2()
    flat = " ".join(re.sub(r"(?m)^\s*>[ \t]?", " ", step).split())  # blockquote markers dropped
    for needle in ("RESULTS_BUCKET", "MODEL_S3_URI", "AWS_REGION", "scripts/gen_env.py", "config/sample.env"):
        assert needle in step, f"step 2 names {needle!r}"
    for needle in ("repository root", "uv", "credentials", "results bucket", "trace", "sidecar", "sha256",
                   "tokenizer", "without the weights"):
        assert needle in flat, f"step 2 says {needle!r}"
    rates = re.search(r'^RATES="([0-9 ]+)"', UPLOAD.read_text(), re.MULTILINE)
    assert rates and rates.group(1).split() == [str(r) for r in RATES]
    assert "10, 25 or 32" in flat, "the rates with a plan"
    assert sorted(p.name for p in PLANS.glob("*.txt")) == sorted(
        n for r in RATES for n in (f"plan-w3-r{r}.txt", f"plan-w3-r{r}-cells.txt"))
    assert re.search(r"`plan-w3-r<rate>\.txt` runs S0 and S7", flat) and re.search(
        r"`plan-w3-r<rate>-cells\.txt` runs S1 to S6", flat), "which plan runs which cells"
    callout = re.search(r"(?ms)^\s*> \*\*.*?(?=^\s*$)", step)
    assert callout, "a > **...** callout"
    text = " ".join(re.sub(r"(?m)^\s*>[ \t]?", " ", callout.group(0)).split())
    for needle in ("sums_sha256", "upload-inputs.sh", "`plan-w3-r<rate>.txt`", "`plan-w3-r<rate>-cells.txt`",
                   "`tokenizer prefix=... sums_sha256=...`", "step 3"):
        assert needle in text, f"the callout says {needle!r}"
    assert re.search(r"(?i)both", text) and re.search(r"(?i)recorded runs", text)


def test_readme_drops_the_banner_and_states_the_provenance() -> None:
    text = README.read_text()
    low = text.lower()
    assert not re.search(r"(?m)^>", text), "no banner"
    assert "not a result of record" not in low
    assert PROVENANCE in text
    assert "[255, 615)" in text and "fixed before any W3 data" in text


def test_readme_says_how_it_works() -> None:
    text = README.read_text()
    m = re.search(r"(?ms)^## How it works\n(.*?)(?=^## )", text)
    assert m, "a '## How it works' section"
    sec = " ".join(m.group(1).split())
    for needle in ("open-loop", "demo-bench trace w2", "historical", "AIPerf", "--fixed-schedule", "params.json"):
        assert needle in sec, f"How it works lacks {needle!r}"


def test_readme_documents_the_upload() -> None:
    text = README.read_text()
    for needle in ("upload-inputs.sh", "--yes", "sums_sha256", "RESULTS_BUCKET"):
        assert needle in text, f"README lacks {needle!r}"
    assert "| `upload-inputs.sh` |" in text, "the file table lists upload-inputs.sh"


def test_readme_and_params_name_no_process_document() -> None:
    for p in (README, PARAMS):
        assert "amendment" not in p.read_text().lower(), p.name
