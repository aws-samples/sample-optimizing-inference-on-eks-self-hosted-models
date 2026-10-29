# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Static contract for ``tools/bench-image/Dockerfile`` (the matrix-driver image).

The in-cluster driver is the Python ``tools/matrix_driver``; it needs neither the shell scripts nor the manifests, so the
image must not bake ``scripts/`` or ``manifests/`` (the shell matrix driver 99-run-matrix.sh left the tree).  It still has
to carry ``bash``, a **pinned** ``kubectl`` verified by checksum, ``demo_bench`` and ``aiperf==0.13.0``.  No image build is
attempted: the Dockerfile is parsed as text only.  Override the path with ``BENCH_DOCKERFILE``.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
DOCKERFILE = Path(os.environ.get("BENCH_DOCKERFILE", REPO_ROOT / "tools" / "bench-image" / "Dockerfile"))


def _instructions() -> list[str]:
    """Dockerfile instructions with comments dropped and line continuations joined."""
    assert DOCKERFILE.is_file(), f"missing {DOCKERFILE}"
    joined: list[str] = []
    buf = ""
    for raw in DOCKERFILE.read_text().splitlines():
        if not buf and raw.lstrip().startswith("#"):
            continue
        line = raw.rstrip()
        if line.endswith("\\"):
            buf += line[:-1] + " "
            continue
        joined.append((buf + line).strip())
        buf = ""
    return [i for i in joined if i]


def _text() -> str:
    return "\n".join(_instructions())


def test_aiperf_pinned_and_demo_bench_installed() -> None:
    text = _text()
    assert "aiperf==0.13.0" in text
    assert re.search(r"demo_bench", text)


def test_bash_is_explicitly_present() -> None:
    """The scripts use bash features and a bash shebang; do not rely on the base image happening to ship it."""
    assert re.search(r"\bbash\b", _text()), "no non-comment instruction mentions bash (apt-get install bash / bash --version)"


def test_kubectl_installed_with_pinned_version_and_checksum() -> None:
    text = _text()
    assert "kubectl" in text, "kubectl missing"
    assert re.search(r"\bv1\.[0-9]+\.[0-9]+\b", text), "kubectl version not pinned (e.g. ARG KUBECTL_VERSION=v1.35.x)"
    assert re.search(r"sha256sum\s+(-c|--check)|[0-9a-f]{64}", text), "kubectl download is not checksum-verified"
    assert "stable.txt" not in text and "latest" not in text.lower().replace("latest.", ""), "kubectl must not float"


@pytest.mark.parametrize("tree", ["scripts", "manifests"])
def test_image_does_not_bake_the_scripts_or_manifests_tree(tree: str) -> None:
    copies = [i for i in _instructions() if i.upper().startswith(("COPY", "ADD")) and "--from=" not in i]
    assert not [i for i in copies if re.search(rf"(^|[ /]){tree}/?(\s|$)", i + " ")], f"the image must not COPY {tree}/"


@pytest.mark.parametrize("gone", ["99-run-matrix", "70-demo-take", "20-topology-up", "30-run.sh"])
def test_dockerfile_does_not_mention_removed_scripts(gone: str) -> None:
    assert gone not in DOCKERFILE.read_text(), f"{gone} left the tree, the build would fail on it"


def test_final_user_is_numeric_non_root() -> None:
    users = [i.split(None, 1)[1] for i in _instructions() if i.upper().startswith("USER ")]
    assert users, "no USER instruction"
    uid = users[-1].split(":")[0]
    assert uid.isdigit() and int(uid) != 0, f"final USER must be a numeric non-root uid (runAsNonRoot), got {users[-1]!r}"


@pytest.mark.parametrize("bad", ["HF_TOKEN", "AWS_SECRET_ACCESS_KEY", "AWS_ACCESS_KEY_ID", ":latest"])
def test_no_secrets_or_floating_tags(bad: str) -> None:
    assert bad not in _text()


# ----------------------------------------------------------------------------- AWS CLI v2
# The driver uploads results with the AWS CLI from the matrix-driver pod. Without the CLI the data stays on the PVC.
SYSTEM_BIN_DIRS = ("/usr/local/bin", "/usr/bin", "/bin", "/usr/local/sbin", "/usr/sbin", "/sbin")


def _aws_instructions() -> list[str]:
    return [i for i in _instructions() if re.search(r"awscli|aws-cli|aws/install", i)]


def test_awscli_v2_installed_with_pinned_version() -> None:
    """AWS CLI v2 (the `awscli-exe-linux-*` installer) pinned to an exact 2.x.y, never the floating latest zip."""
    ins = _aws_instructions()
    assert ins, "no instruction installs the AWS CLI (awscli-exe-linux-<arch>-<ver>.zip + aws/install)"
    text = "\n".join(ins) + "\n" + "\n".join(i for i in _instructions() if i.upper().startswith("ARG ") and "AWSCLI" in i.upper())
    assert re.search(r"\b2\.[0-9]+\.[0-9]+\b", text), "AWS CLI version is not pinned to an exact 2.x.y (e.g. ARG AWSCLI_VERSION=2.x.y)"
    assert "awscli-exe-linux" in text and "aws/install" in text, "expected the official v2 bundled installer"
    assert not re.search(r"awscli-exe-linux-[a-z0-9_]+\.zip", text), "unversioned (floating latest) installer zip"
    assert "awscliv2.pip" not in text and not re.search(r"pip3?\s+install[^\n]*\bawscli\b(?!==2)", text), "do not use the v1 pip package"


def test_awscli_download_is_verified() -> None:
    """Signature (gpg --verify of the .sig) or checksum (sha256sum -c / a 64-hex digest) before installing."""
    text = "\n".join(_aws_instructions())
    gpg = re.search(r"gpg[^\n]*--verify", text) and re.search(r"\.zip\.sig", text)
    sha = re.search(r"sha256sum\s+(-c|--check)", text) and re.search(r"[0-9a-f]{64}", text)
    assert gpg or sha, "AWS CLI installer is not signature/checksum verified"


def test_awscli_installed_as_root_before_the_final_user() -> None:
    ins = _instructions()
    idx = [n for n, i in enumerate(ins) if "aws/install" in i]
    users = [n for n, i in enumerate(ins) if i.upper().startswith("USER ")]
    assert idx and users, "install instruction or USER missing"
    assert idx[-1] < users[-1], "aws/install must run before the final USER (needs root, system-wide dir)"


def test_aws_binary_dir_is_on_path() -> None:
    """Default installer links into /usr/local/bin; a custom `-b <dir>` must be on PATH via ENV."""
    ins = _instructions()
    installs = [i for i in ins if "aws/install" in i]
    assert installs, "aws/install missing"
    custom = re.findall(r"(?:-b|--bin-dir)[ =]+(/[^\s;&\\]+)", installs[-1])
    for d in custom:
        d = d.rstrip("/")
        env_path = " ".join(i for i in ins if i.upper().startswith("ENV ") and "PATH" in i)
        assert d in SYSTEM_BIN_DIRS or d in env_path, f"aws is linked into {d}, which is not on PATH"


def test_aws_version_smoke_check_runs_as_the_final_user() -> None:
    """`RUN aws --version` after the last USER proves the unprivileged runtime user can execute it."""
    ins = _instructions()
    users = [n for n, i in enumerate(ins) if i.upper().startswith("USER ")]
    assert users, "no USER instruction"
    smoke = [n for n, i in enumerate(ins) if i.upper().startswith("RUN ") and re.search(r"\baws\s+--version\b", i)]
    assert smoke and smoke[-1] > users[-1], "add `RUN aws --version` AFTER the final USER instruction"


# ----------------------------------------------------------------------------- FT-4b: HOME ownership, root RUNs, revision label
# A Rosetta/amd64 build left /home/bench/.cache as root:root 0755, so aiperf (uid 10001) died with PermissionError on
# /home/bench/.cache/aiperf.  Static contract: the runtime user owns its HOME tree when the image is finalised.
RUNTIME_UID = r"(?:10001(?::10001)?|bench(?::bench)?)"
ROOT_TIME_TOOLS = re.compile(r"\bpip\b|aiperf|demo-bench|aws/install|\baws\b|kubectl|huggingface|python\b")


def _home() -> str:
    homes = [m.group(1) for i in _instructions() if i.upper().startswith("ENV ") for m in re.finditer(r"\bHOME=(\S+)", i)]
    assert homes, "ENV HOME is not set: aiperf/pip would write caches to a root-owned HOME"
    return homes[-1].rstrip("/")


def _last_user_index() -> int:
    users = [n for n, i in enumerate(_instructions()) if i.upper().startswith("USER ")]
    assert users, "no USER instruction"
    return users[-1]


def test_home_cache_is_owned_by_the_runtime_user_when_the_image_is_finalised() -> None:
    """Before the final USER a RUN chowns HOME (recursively) or `$HOME/.cache` to 10001:10001, AFTER every RUN that
    executes tools as root (pip, aiperf, demo-bench, aws, kubectl, python), which may create root-owned caches."""
    ins, last_user = _instructions(), _last_user_index()
    home = re.escape(_home())
    target = rf"(?:{home}(?:/\.cache)?|\$HOME(?:/\.cache)?|\"\$HOME(?:/\.cache)?\"|\$\{{HOME\}}(?:/\.cache)?)/?"
    chown = re.compile(rf"chown\s+(?:-R\s+|--recursive\s+)?{RUNTIME_UID}\s+{target}(?:\s|;|&|$)")
    install_d = re.compile(rf"install\s+-d\s+(?:-m\s*\S+\s+)?-o\s*{RUNTIME_UID.replace('bench', 'bench')}\s+-g\s*{RUNTIME_UID}\s+{target}(?:\s|;|&|$)")
    fixers = [n for n, i in enumerate(ins[:last_user]) if i.upper().startswith("RUN ") and (chown.search(i) or install_d.search(i))]
    assert fixers, f"no RUN before the final USER makes {_home()} (or its .cache) owned by 10001:10001"
    tool_runs = [n for n, i in enumerate(ins[:last_user]) if i.upper().startswith("RUN ") and ROOT_TIME_TOOLS.search(i) and n not in fixers]
    assert not tool_runs or fixers[-1] > tool_runs[-1], (
        f"the ownership fix (instruction {fixers[-1]}) runs before a root RUN that executes tools (instruction {tool_runs[-1]}): "
        "that RUN can recreate root-owned files under HOME"
    )


FORBIDDEN_AFTER_USER = re.compile(
    r"\bapt(?:-get)?\b|\bdpkg\b|\buseradd\b|\bgroupadd\b|\bchown\b|\bsudo\b|\bsetcap\b|\bpip3?\s+install\b|-m\s+pip\s+install|aws/install|\bmkdir\s+(?:-p\s+)?/(?:usr|opt|etc|var|root)\b"
)


def test_no_run_after_the_final_user_needs_root() -> None:
    ins = _instructions()
    after = [i for i in ins[_last_user_index() + 1:] if i.upper().startswith("RUN ")]
    bad = [i for i in after if FORBIDDEN_AFTER_USER.search(i)]
    assert not bad, f"RUN after USER needs root: {bad}"


def test_oci_revision_label_comes_from_a_build_arg() -> None:
    """`org.opencontainers.image.revision` carries the git commit: LABEL value = a build ARG declared in the final stage
    (ARG is stage-scoped), with no hard-coded commit as default."""
    ins = _instructions()
    from_idx = [n for n, i in enumerate(ins) if i.upper().startswith("FROM ")]
    final = ins[from_idx[-1]:]
    labels = [i for i in final if i.upper().startswith("LABEL ") and "org.opencontainers.image.revision" in i]
    assert labels, "no org.opencontainers.image.revision label in the final stage"
    m = re.search(r"org\.opencontainers\.image\.revision\s*=\s*\"?\$\{?([A-Z][A-Z0-9_]*)\}?\"?", labels[-1])
    assert m, f"the revision label must take its value from a build ARG: {labels[-1]}"
    arg = m.group(1)
    decls = [i for i in final if re.match(rf"ARG\s+{arg}(?:=|\s|$)", i)]
    assert decls, f"ARG {arg} must be declared AFTER the final FROM (ARGs do not cross stages)"
    assert not re.search(r"=\s*\"?[0-9a-f]{40}\"?", decls[-1]), "no hard-coded commit as the default"


@pytest.mark.parametrize("tree", ["scripts", "manifests"])
def test_build_context_does_not_re_include_the_scripts_or_manifests_tree(tree: str) -> None:
    lines = [ln.strip() for ln in (REPO_ROOT / ".dockerignore").read_text().splitlines()]
    assert f"!{tree}" not in lines, f".dockerignore must not allow {tree}/ into the build context any more"


# ----------------------------------------------------------------------------- BuildKit, the frontend pin, linux/amd64
# Live test 3: the Dockerfile builds with BuildKit only (heredoc COPY, RUN --mount, the automatic TARGETARCH), an
# in-cluster builder that parses Dockerfiles without BuildKit stopped on it ("TARGETARCH: parameter not set"),
# `# syntax=docker/dockerfile:1` floated to whatever 1.x was current, and a buildx build on an Apple silicon Mac makes
# an arm64 image while the cpu-demo pool, where the bench pods run, is amd64 only.  The docs describe that builder
# without its name: tests/publish/test_public_runtime_image.py bans the name outside the design log.
NODEPOOL_TF = REPO_ROOT / "terraform" / "cluster" / "nodepool.tf"
BENCH_DEPLOYMENT = REPO_ROOT / "manifests" / "bench" / "deployment.yaml"
BUILD_DOCS = ["docs/DEPLOY.md", "tools/bench-image/Dockerfile"]


def test_the_syntax_frontend_is_pinned_by_digest_on_the_first_line() -> None:
    """A parser directive counts only on the very first line; the tag alone floats to the current 1.x frontend."""
    first = DOCKERFILE.read_text().splitlines()[0]
    assert re.fullmatch(r"# syntax=docker/dockerfile:1(?:\.[0-9]+){0,2}@sha256:[0-9a-f]{64}", first), first


def test_the_bench_pods_run_on_amd64_nodes_only() -> None:
    """The premise of --platform linux/amd64: the bench Deployment selects cpu-demo, an amd64-only pool."""
    assert re.search(r'key = "kubernetes.io/arch", operator = "In", values = \["amd64"\]', NODEPOOL_TF.read_text()), (
        "the cpu-demo arch changed: update the --platform of the build docs")
    assert "karpenter.sh/nodepool: cpu-demo" in BENCH_DEPLOYMENT.read_text()


def _doc_text(doc: str) -> str:
    """The build instructions of a doc: step 1 of section 7 of DEPLOY.md, the header comment of the Dockerfile."""
    text = (REPO_ROOT / doc).read_text()
    if doc.endswith("DEPLOY.md"):
        start = text.index("1. **Bench image.**")
        return text[start:text.index("2. **Inputs.**", start)]
    return "\n".join(ln for ln in text.splitlines() if ln.startswith("#"))


@pytest.mark.parametrize("doc", BUILD_DOCS)
def test_every_documented_build_targets_linux_amd64(doc: str) -> None:
    joined = re.sub(r"\\\n#?\s*", " ", _doc_text(doc))  # continuation lines (and their comment markers) joined
    builds = re.findall(r"docker buildx build [^\n]*", joined)
    assert builds, f"{doc} documents the build"
    for cmd in builds:
        assert re.search(r"--platform[ =]linux/amd64(?![\w,/])", cmd), f"{doc}: without --platform linux/amd64 a Mac M* builds arm64: {cmd}"


@pytest.mark.parametrize("doc", BUILD_DOCS)
def test_the_build_docs_say_buildkit_is_required_and_other_builders_fail(doc: str) -> None:
    flat = " ".join(_doc_text(doc).replace("#", " ").split())
    assert "BuildKit" in flat, f"{doc}: say that the build needs BuildKit"
    assert re.search(r"(?i)legacy builder[^.]*\bfail", flat), f"{doc}: say that the builders without BuildKit fail on it"
    assert "TARGETARCH: parameter not set" in flat, f"{doc}: quote the error of the builder that was tried"
