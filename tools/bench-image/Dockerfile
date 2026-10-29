# syntax=docker/dockerfile:1@sha256:4edf897a3ffa55b89f906fc8cc78afdb3f1834cc9c7083565e611a8a7d5fe99e
#
# Bench / matrix-driver image (spec 10.1, plan WS4-3, FT-11).
#
# It runs aiperf 0.13.0 and demo-bench, and it is the image of the in-cluster matrix driver
# (tools/matrix_driver, mounted from a ConfigMap), which calls kubectl and the AWS CLI. The
# repository scripts/ and manifests/ trees are NOT baked: the driver needs neither.
#
# Build context is the REPOSITORY ROOT (it needs tools/); the allowlist in the root .dockerignore
# keeps the context small (no virtual environments, caches or local tool state):
#   cd sample-optimizing-inference-on-eks
#   docker buildx build -f tools/bench-image/Dockerfile --platform linux/amd64 \
#     --build-arg VCS_REF="$(git rev-parse HEAD)" -t <acct>.dkr.ecr.<region>.amazonaws.com/dynamo-sample/bench:<tag> .
# --platform linux/amd64: the bench pods run on the cpu-demo pool, which is amd64 only; without it a
# Mac with Apple silicon builds an arm64 image that cannot start there.
# BuildKit is required (docker buildx, or buildctl in a cluster): the heredoc COPY <<'KEY', RUN --mount
# and the automatic TARGETARCH are BuildKit features. The legacy builder and the builders that parse a
# Dockerfile themselves instead of through BuildKit fail on this file (one in-cluster builder stopped
# with "TARGETARCH: parameter not set"). The first line pins the BuildKit syntax frontend by digest:
# the docker/dockerfile:1 (= 1.27) index on 2026-10-27, which resolves anonymously on Docker Hub; move
# the pin to the digest of a newer docker/dockerfile:1 on purpose, never back to the bare tag.
# Build from a clean `git archive <commit>` so uncommitted edits cannot leak in.
# Record the pushed digest; manifests reference the image by digest only. Override BASE_IMAGE
# with the ECR mirror of the same digest when building in-account (O-15).
#
# kubectl: pinned KUBECTL_VERSION (cluster is Kubernetes 1.35) and verified against the
# per-arch SHA-256 published at https://dl.k8s.io/release/<version>/bin/linux/<arch>/kubectl.sha256
# (values copied below; a mismatch fails the build). Supported TARGETARCH: amd64, arm64.
#
# AWS CLI v2 (the driver uploads results with `aws s3`): official bundled installer
# awscli-exe-linux-<arch>-<AWSCLI_VERSION>.zip, pinned, verified twice before install: (1) gpg
# signature (.zip.sig) against the AWS CLI Team key embedded below, whose fingerprint
# FB5D B77F D5C1 18B8 0511 ADA8 A631 0ACC 4672 475C is checked (key and fingerprint copied from
# https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html on 2026-10-18;
# the key expires 2027-07); (2) the per-arch sha256 recorded below. Installed system-wide as root
# (/usr/local/aws-cli, linked into /usr/local/bin) and smoke-tested as the runtime user.
#
# Tokenizer: NOT baked into the image. aiperf gets --tokenizer from its caller and reads it at run
# time (a staged copy, or Hugging Face for a model that is not gated). HF_HOME must be writable (a
# volume when the root filesystem is read-only).
#
# Traces: every tools/bench-image/traces/*.jsonl present at build time is baked read-only into
# /traces together with /traces/SHA256SUMS (the digest is recorded per run).
#
# No secrets, tokens or credentials are used or stored by this build.
#
# ------------------------------------------------------------------------------------------------
# Trace provenance (for NOTICE)
#   File:      tools/bench-image/traces/64k_400_90kv_agent_new_noschedule_short_15perc.jsonl
#   Upstream:  https://github.com/ai-dynamo/dynamo (NVIDIA Dynamo), path
#              recipes/kimi-k2.6/perf/traces/64k_400_90kv_agent_new_noschedule_short_15perc.jsonl
#              (Git LFS object; two other recipes reference it by symlink).
#              Added in commit 66a95d5aa3b5d238fd94b6f8243c4e479cb7242c
#              ("feat(recipes): Add K2.6 recipes (#10187)", 2026-06-03); present in tag v1.5.1.
#   Upstream sha256: f20d3f2bc83dd1306cda659fbe34e7c4d85ca5497626c98bc0b1c4d2211379d0 (3541 rows).
#   License:   Apache License 2.0 (repository LICENSE; its MIT carve-out covers only
#              lib/llm/tests/data/deepseek-v3.2 and does not apply to this file).
#   Modified by this project (Apache-2.0 sec. 4(b) notice), 2026-10-18: dropped the 48 rows with
#              input_length + output_length >= 500000 (the recipe's context-length); every kept
#              row is byte-identical to upstream. Result: 3493 rows, sha256
#              ddc75ce76df4eccd5e3290f4665e993dbe891f60e84942410e2485cb5425d62e.
#              Baked UNSTAMPED on purpose: the fixed schedule is generated in the pod at run time
#              by `demo-bench trace schedule` with a fixed RATE/SEED that is recorded per run. Any
#              change to the baked file must be appended here with its parameters and the new
#              sha256.
#
# ------------------------------------------------------------------------------------------------
# Accepted scan findings (re-check at EVERY rebuild)
# Scan of record: Trivy 0.69.3, `trivy image --scanners vuln,secret` (all severities, unfixed
# included), vulnerability DB of 2026-10-28, of a linux/amd64 build of this file with kubectl
# v1.35.10 (go1.26.10), pip 26.2.2 and python:3.12.16-slim-trixie. Result: 0 CRITICAL, 51 HIGH,
# 64 MEDIUM, 64 LOW, 3 UNKNOWN, no secret (the previous pins, with the same DB: 1 CRITICAL, 55 HIGH,
# 73 MEDIUM, 65 LOW, 5 UNKNOWN). Per target, CRITICAL/HIGH/MEDIUM/LOW/UNKNOWN:
#   Debian 13.7 packages                     0 / 44 / 58 / 63 / 1
#   Python packages (all in pip/_vendor)     0 /  5 /  2 /  1 / 0
#   /usr/local/bin/kubectl                   0 /  2 /  4 /  0 / 2
#   AWS CLI bundle, Node.js                  none
# Bump a pin when a release fixes a row below and re-run the scan; the evidence of each run (JSON,
# tables, image id) is kept with the publication records, not in this repository.
# Remaining HIGH, with the reason each one is accepted:
#   kubectl v1.35.10, golang.org/x/net v0.56.0 and golang.org/x/text v0.39.0 (`go version -m`).
#   No upstream kubectl carries x/net >= v0.60.0 or x/text >= v0.41.0 on 2026-10-28: v1.36.6 has
#   the same modules, and v1.37.2 (x/net v0.57.0, x/text v0.40.0) is outside the +/-1 minor skew
#   of the 1.35 cluster. Waiting upstream.
#     CVE-2026-78669 (x/net, HTTP/2 SETTINGS flood): kubectl is only an HTTP client of the cluster's
#       own API server, over the pod's in-cluster ServiceAccount credentials, and opens no listener
#       (no `kubectl proxy` / `port-forward` in the driver).
#     CVE-2026-56851 (x/text, PRECIS Nickname profile): golang.org/x/text/secure/precis is not
#       linked into this kubectl build (none of its functions is in the binary, amd64 or arm64).
#   pip 26.2.2 (the newest pip on PyPI on 2026-10-28) vendors certifi 2026.10.19, which clears
#   the CRITICAL GHSA-6q48-6c7w-9gw2 that the SBOM of the previous pip (pip/_vendor/bom.cdx.json)
#   showed, but still vendors urllib3 2.7.0, msgpack 1.1.2 and setuptools 70.3.0. pip stays: at
#   the start of every run the matrix driver installs its mounted, sha256-checked demo_bench wheel
#   with `pip install --no-deps --no-index --target`, offline. The top-level urllib3 2.8.0, certifi
#   2026.10.23 and setuptools 84.2.1 (required by dash, an aiperf dependency) have no finding.
#     urllib3 2.7.0 CVE-2026-97687 (HTTPS proxy TLS settings), CVE-2026-97689 (chunked response
#       memory): pip's HTTP client; with --no-index and a local wheel pip makes no HTTP request.
#     msgpack 1.1.2 CVE-2026-114613 (C extension leak), GHSA-6v7p-g79w-8964 (SEGV on Unpacker
#       reuse): pip vendors only the pure-Python fallback, used for its HTTP cache, which is off
#       (PIP_NO_CACHE_DIR=1) and unused offline.
#     setuptools 70.3.0 CVE-2025-47273 (PackageIndex path traversal): pip vendors only
#       pkg_resources; setuptools.package_index is not in pip/_vendor.
#   Debian 13.7 packages, 44 rows with no Debian fix on 2026-10-28 (status affected; perl-base
#   fix_deferred); every bench-image container runs as uid 10001 with allowPrivilegeEscalation:
#   false and all capabilities dropped, and no tool of the image calls the affected code:
#     util-linux family (bsdutils, libblkid1, liblastlog2-2, libmount1, libsmartcols1, libuuid1,
#       login, mount, util-linux) x CVE-2026-76642, CVE-2026-78408, CVE-2026-78409, CVE-2026-78410
#       (36 rows): set-uid mount(8) X-mount options and nsenter --join-cgroup; nothing runs mount or
#       nsenter, and no_new_privs makes the set-uid bit ineffective.
#     ncurses (libncursesw6, libtinfo6, ncurses-base, ncurses-bin) CVE-2025-69720: the infocmp
#       command-line tool, which nothing runs (bash links libtinfo6, not infocmp).
#     libsystemd0, libudev1 CVE-2026-16742: systemd-homed, which is not installed.
#     libacl1 CVE-2026-54369: a second local user racing a privileged acl_*_file() caller; the
#       container has one unprivileged user and no privileged process at run time.
#     perl-base CVE-2026-9538: Archive::Tar, which is not installed (perl-modules is absent).
# MEDIUM, LOW and UNKNOWN rows are in the scan report and are not triaged here. The pip 25.0.1 of
# the base image (MEDIUM CVE-2026-6357 and others) is replaced by the pinned pip below.
#
# Fixed, not accepted (2026-10-22): a continuous scan of an earlier build of this image
# added CRITICAL GHSA-6q48-6c7w-9gw2 (certifi 2026.10.18, fixed in
# 2026.10.19) and CVE-2026-112403 (pillow 12.3.0, fixed in 13.0.0), both transitive. The pip layer
# pins certifi==2026.10.23 and pillow==13.0.0, exact versions at or above the fixed releases (the
# latest on PyPI on 2026-10-27, no advisory listed there), and the build fails if the installed
# versions are below the floors 2026.10.19 and 13.0.0. Check both for a newer release at every
# rebuild. aiperf 0.13.0 declares pillow~=12.3.0 (no aiperf release on PyPI allows 13 on
# 2026-10-22), so pip cannot resolve the pin; the layer is resolved by uv (pinned, removed again
# in the same RUN) with an override that replaces every pillow requirement by the pin.
# aiperf imports PIL at module load but uses it only for image/video datasets and HF image
# loaders, never for the text traces of this demo; Pillow 13.0.0 removes none of the APIs it calls
# (Image.open/new/fromarray/registered_extensions/LANCZOS, ImageDraw.Draw, UnidentifiedImageError).
# Drop the override when an aiperf release accepts pillow >= 13.

ARG BASE_IMAGE=python:3.12.16-slim-trixie@sha256:8523d6abeb48ceae0b7240d30c5808fc26cb79c1a635bacdf19cf3baecfbec07

# ---------------------------------------------------------------- demo_bench wheel
FROM ${BASE_IMAGE} AS wheel
ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /src
COPY tools/demo_bench/pyproject.toml ./pyproject.toml
COPY tools/demo_bench/src ./src
RUN python -m pip wheel --no-deps --wheel-dir /dist .

# ---------------------------------------------------------------- kubectl (pinned + checksum)
FROM ${BASE_IMAGE} AS kubectl
ARG TARGETARCH
ARG KUBECTL_VERSION=v1.35.10
ARG KUBECTL_SHA256_AMD64=c49a28f1f4efc4d475ed05f47f2ce8e563bf7bef69ed77fe1b924ea6d83b6327
ARG KUBECTL_SHA256_ARM64=283f1be6a46b3695b625e25030106b8ffe18bd0d3a900f5b65c259ca407f04f5
RUN set -eu; \
    case "${TARGETARCH}" in \
      amd64) sum="${KUBECTL_SHA256_AMD64}" ;; \
      arm64) sum="${KUBECTL_SHA256_ARM64}" ;; \
      *) echo "unsupported TARGETARCH '${TARGETARCH}'" >&2; exit 1 ;; \
    esac; \
    python -c "import sys, urllib.request; urllib.request.urlretrieve(sys.argv[1], sys.argv[2])" \
      "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/${TARGETARCH}/kubectl" /kubectl; \
    echo "${sum}  /kubectl" | sha256sum -c -; \
    chmod 0755 /kubectl

# ---------------------------------------------------------------- AWS CLI v2 (pinned + gpg + sha256)
FROM ${BASE_IMAGE} AS awscli
ARG TARGETARCH
ARG AWSCLI_VERSION=2.38.1
ARG AWSCLI_SHA256_X86_64=b11ed416d91f4251da363a17706938eeaf0e520745754c104c0c1890dd8363c7
ARG AWSCLI_SHA256_AARCH64=91360c0adb2ae508334ff657dce4787b598b443a11cf3dc58e08b74f12e16009
ARG AWSCLI_SIGNER_FPR=FB5DB77FD5C118B80511ADA8A6310ACC4672475C
COPY <<'KEY' /tmp/aws-cli-team.asc
-----BEGIN PGP PUBLIC KEY BLOCK-----

mQINBF2Cr7UBEADJZHcgusOJl7ENSyumXh85z0TRV0xJorM2B/JL0kHOyigQluUG
ZMLhENaG0bYatdrKP+3H91lvK050pXwnO/R7fB/FSTouki4ciIx5OuLlnJZIxSzx
PqGl0mkxImLNbGWoi6Lto0LYxqHN2iQtzlwTVmq9733zd3XfcXrZ3+LblHAgEt5G
TfNxEKJ8soPLyWmwDH6HWCnjZ/aIQRBTIQ05uVeEoYxSh6wOai7ss/KveoSNBbYz
gbdzoqI2Y8cgH2nbfgp3DSasaLZEdCSsIsK1u05CinE7k2qZ7KgKAUIcT/cR/grk
C6VwsnDU0OUCideXcQ8WeHutqvgZH1JgKDbznoIzeQHJD238GEu+eKhRHcz8/jeG
94zkcgJOz3KbZGYMiTh277Fvj9zzvZsbMBCedV1BTg3TqgvdX4bdkhf5cH+7NtWO
lrFj6UwAsGukBTAOxC0l/dnSmZhJ7Z1KmEWilro/gOrjtOxqRQutlIqG22TaqoPG
fYVN+en3Zwbt97kcgZDwqbuykNt64oZWc4XKCa3mprEGC3IbJTBFqglXmZ7l9ywG
EEUJYOlb2XrSuPWml39beWdKM8kzr1OjnlOm6+lpTRCBfo0wa9F8YZRhHPAkwKkX
XDeOGpWRj4ohOx0d2GWkyV5xyN14p2tQOCdOODmz80yUTgRpPVQUtOEhXQARAQAB
tCFBV1MgQ0xJIFRlYW0gPGF3cy1jbGlAYW1hem9uLmNvbT6JAlQEEwEIAD4CGwMF
CwkIBwIGFQoJCAsCBBYCAwECHgECF4AWIQT7Xbd/1cEYuAURraimMQrMRnJHXAUC
akV0ygUJDqP4lQAKCRCmMQrMRnJHXFHjD/9eyZLYcKuQOlLvtqSDtUBiEZf6ZZjM
i3ygYH8rJNtuToUH+HvSpe819urJCquXhDrlK6N+aqW0hCLtNABJG/vsafIgvIYJ
hSGgpgtNnQyMV1jViRWqPjbouw8OkYKBThUfT1i2Y+wn58ifs6ODBCmTexWtXspA
Si+Gt49xDOW0APmbOPnI+a4HJW6tVEo6MWS0WjzpiBayR3d1A4pt4YrPfSdDgpLo
h2SLQqlRqvvVZJaWBjhkErNFpfsBA06sDcPEOb0G8LBUbR4WOcdvhe5LubJbZuxC
AG9kNPCVeQP1ixwjgjXKysaxeQ6rv0VzIQgRp6tLVLWhy6AKDNvLjFSsmXZ1Wl08
Y/RlOHXlzLuQMRE6sR1wOdRxc9TsrNWTGiBK65cvSWOy03JeBkQQ8pesqltiyxI9
U21kkgiXtTSKNGfKK8pO27D81YANhRqPK7iTp6kuFiY2WtOg90KTMNlIT+Ff85Y2
b1rHj6Z0SrCkJujhWk3IBPic/wJgz01LEc/OAdUPlby90RJZcIBhSlWhT7mXnXIO
c0HWlNQrns2s3CTyYwZSiSlYe9ApeLwhjDo8NhbFuCAy61l6O5UsR4AfZxx/rGKv
2wFb1/RN/P4gNe6vmxZAPjR0AQcwD3tc2McimOLr/22kmPz8IH3I0X7WoSFr0Biz
E91G7bb0hOb/cA==
=knv7
-----END PGP PUBLIC KEY BLOCK-----
KEY
RUN set -eu; \
    apt-get update; \
    apt-get install -y --no-install-recommends gnupg unzip; \
    rm -rf /var/lib/apt/lists/*; \
    case "${TARGETARCH}" in \
      amd64) arch=x86_64; sum="${AWSCLI_SHA256_X86_64}" ;; \
      arm64) arch=aarch64; sum="${AWSCLI_SHA256_AARCH64}" ;; \
      *) echo "unsupported TARGETARCH '${TARGETARCH}'" >&2; exit 1 ;; \
    esac; \
    url="https://awscli.amazonaws.com/awscli-exe-linux-${arch}-${AWSCLI_VERSION}.zip"; \
    python -c "import sys, urllib.request; urllib.request.urlretrieve(sys.argv[1], sys.argv[2])" \
      "${url}" /tmp/awscli.zip; \
    python -c "import sys, urllib.request; urllib.request.urlretrieve(sys.argv[1], sys.argv[2])" \
      "${url}.sig" /tmp/awscli.zip.sig; \
    GNUPGHOME="$(mktemp -d)"; export GNUPGHOME; \
    gpg --batch --quiet --import /tmp/aws-cli-team.asc; \
    fpr="$(gpg --batch --with-colons --fingerprint | awk -F: '$1 == "fpr" {print $10; exit}')"; \
    [ "${fpr}" = "${AWSCLI_SIGNER_FPR}" ] || { echo "unexpected AWS CLI key ${fpr}" >&2; exit 1; }; \
    gpg --batch --verify /tmp/awscli.zip.sig /tmp/awscli.zip; \
    echo "${sum}  /tmp/awscli.zip" | sha256sum -c -; \
    mkdir -p /out; \
    unzip -q /tmp/awscli.zip -d /out; \
    rm -rf "${GNUPGHOME}" /tmp/awscli.zip /tmp/awscli.zip.sig

# ---------------------------------------------------------------- traces + checksums
FROM ${BASE_IMAGE} AS traces
COPY tools/bench-image/ /ctx/
RUN mkdir -p /out \
    && set -- /ctx/traces/*.jsonl \
    && if [ -e "$1" ]; then \
         cp /ctx/traces/*.jsonl /out/ && cd /out && sha256sum -- *.jsonl > SHA256SUMS; \
       fi \
    && chmod 0755 /out \
    && find /out -type f -exec chmod 0444 {} +

# ---------------------------------------------------------------- runtime
FROM ${BASE_IMAGE}

LABEL org.opencontainers.image.title="dynamo-sample-bench" \
      org.opencontainers.image.description="aiperf 0.13.0, demo-bench, kubectl and the AWS CLI for the matrix driver of the Dynamo on EKS demo"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HOME=/home/bench \
    HF_HOME=/home/bench/.cache/huggingface

RUN apt-get update \
    && apt-get install -y --no-install-recommends bash jq ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && bash --version > /dev/null \
    && jq --version

RUN groupadd --gid 10001 bench \
    && useradd --uid 10001 --gid 10001 --create-home --home-dir /home/bench \
         --shell /bin/bash bench

COPY --from=kubectl /kubectl /usr/local/bin/kubectl
RUN kubectl version --client

# AWS CLI v2: run the verified bundled installer from a bind mount (the installer is not kept).
RUN --mount=type=bind,from=awscli,source=/out,target=/tmp/awscli,rw \
    /tmp/awscli/aws/install --install-dir /usr/local/aws-cli --bin-dir /usr/local/bin \
    && aws --version

COPY --from=wheel /dist/ /tmp/wheels/
# demo_bench is installed with its `plot` extra (matplotlib, pinned in pyproject) so that
# `demo-bench plot sweep` also runs in-cluster. The wheel path goes through "$1" because
# `*.whl[plot]` would be read by the shell as a glob character class.
# One resolution for aiperf, demo_bench and the CVE fixes (see "Fixed, not accepted" above): uv
# resolves it because only uv can override aiperf's pillow~=12.3.0; pip stays the image's
# installer (pinned, asserted; the matrix driver installs its mounted wheel with it at run time, see
# "Accepted scan findings" above) and uv is uninstalled before the layer is committed. The build also
# fails if the certifi vendored by pip (pip._vendor) is below the certifi floor.
RUN set -- /tmp/wheels/demo_bench-*.whl \
    && python -m pip install "pip==26.2.2" "uv==0.13.5" \
    && printf '%s\n' 'pillow==13.0.0' > /tmp/uv-overrides.txt \
    && uv pip install --system --no-cache --compile-bytecode --override /tmp/uv-overrides.txt \
         "aiperf==0.13.0" "demo-bench[plot] @ file://$1" "certifi==2026.10.23" \
    && python -m pip uninstall -y uv \
    && rm -rf /tmp/wheels /tmp/uv-overrides.txt \
    && python -c "import importlib.metadata as m; assert m.version('aiperf') == '0.13.0', m.version('aiperf'); assert m.version('pip') == '26.2.2', m.version('pip'); assert m.version('matplotlib') == '3.11.2', m.version('matplotlib')" \
    && python -c "import importlib.metadata as m; from packaging.version import Version as V; assert V(m.version('certifi')) >= V('2026.10.19'), m.version('certifi'); assert V(m.version('pillow')) >= V('13.0.0'), m.version('pillow')" \
    && python -c "from packaging.version import Version as V; from pip._vendor import certifi; assert V(certifi.__version__) >= V('2026.10.19'), certifi.__version__" \
    && test ! -e /usr/local/bin/uv && test ! -e /usr/local/bin/uvx \
    && python -c "import aiperf.dataset.dataset_manager, aiperf.dataset.generator.image, aiperf.dataset.generator.video" \
    && python -c "from matplotlib.backends.backend_agg import FigureCanvasAgg" \
    && demo-bench --help > /dev/null \
    && demo-bench plot sweep --help > /dev/null

COPY --from=traces /out/ /traces/

# Provenance: the git commit the image is built from (--build-arg VCS_REF=$(git rev-parse HEAD)).
# Declared late so that a new commit only invalidates the last layers; an empty value fails.
ARG VCS_REF
LABEL org.opencontainers.image.revision="${VCS_REF}"
RUN test -n "${VCS_REF}" || { echo "pass --build-arg VCS_REF=<git commit>" >&2; exit 1; }

# Root-run tools above (pip, aiperf, demo-bench, aws, kubectl) can leave root-owned files under
# HOME (a root-owned ~/.cache broke aiperf in an earlier build): hand HOME to the runtime user
# after the last of them, right before the final USER.
RUN chown -R 10001:10001 /home/bench

USER 10001:10001
WORKDIR /home/bench
# The unprivileged runtime user can run the tools the driver calls and write its caches.
RUN aws --version && kubectl version --client > /dev/null \
    && mkdir -p "$HOME/.cache/aiperf" \
    && touch "$HOME/.cache/aiperf/.write-test" \
    && rm "$HOME/.cache/aiperf/.write-test"

CMD ["demo-bench", "--help"]
