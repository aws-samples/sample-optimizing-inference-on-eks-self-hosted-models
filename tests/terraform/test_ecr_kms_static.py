# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""RED tests: static contract of the KMS-encrypted ECR repositories and the image-mirror policy.

The plan-level proof is ``terraform/cluster/tests/ecr_kms.tftest.hcl``. Here: the repositories reference the stack key
(``aws_kms_key.ecr``) and the Checkov skip that kept them on AES256 is gone; the key policy builds every ARN and the
``kms:ViaService`` value from the data sources (no account ID, no hard-coded partition); and the one statement of the
image-mirror policy that Semgrep ``terraform.lang.security.iam.no-iam-creds-exposure`` flags, ``ecr:GetAuthorizationToken``,
carries a ``nosemgrep`` with that rule ID and a reason on the same line, while no other credential-returning action is
allowed anywhere in the file.
"""

from __future__ import annotations

import os
import re
import shutil
from pathlib import Path

import lib_proc
import pytest
from hcl import blocks, read_tf

REPO = Path(__file__).resolve().parents[2]
CLUSTER = REPO / "terraform" / "cluster"
ECR = "ecr.tf"
MIRROR = "iam-image-mirror.tf"
CREDS_RULE = "terraform.lang.security.iam.no-iam-creds-exposure"
# The credential-returning actions of the Semgrep rule (its service:* forms are covered by the no-wildcard check).
CREDS_ACTIONS = (
    "chime:CreateApiKey",
    "codepipeline:PollForJobs",
    "cognito-identity:GetOpenIdToken",
    "cognito-identity:GetOpenIdTokenForDeveloperIdentity",
    "cognito-identity:GetCredentialsForIdentity",
    "connect:GetFederationToken",
    "connect:GetFederationTokens",
    "ec2:GetPasswordData",
    "ecr:GetAuthorizationToken",
    "ecr-public:GetAuthorizationToken",
    "gamelift:RequestUploadCredentials",
    "iam:CreateAccessKey",
    "iam:CreateLoginProfile",
    "iam:CreateServiceSpecificCredential",
    "iam:ResetServiceSpecificCredential",
    "iam:UpdateAccessKey",
    "lightsail:GetInstanceAccessDetails",
    "lightsail:GetRelationalDatabaseMasterUserPassword",
    "rds-db:Connect",
    "redshift:GetClusterCredentials",
    "sso:GetRoleCredentials",
    "mediapackage:RotateChannelCredentials",
    "mediapackage:RotateIngestEndpointCredentials",
    "sts:AssumeRole",
    "sts:AssumeRoleWithSAML",
    "sts:AssumeRoleWithWebIdentity",
    "sts:GetFederationToken",
    "sts:GetSessionToken",
)


@pytest.fixture(scope="module")
def tf() -> dict[str, str]:
    return read_tf(CLUSTER)


@pytest.fixture(scope="module")
def ecr(tf: dict[str, str]) -> dict[str, str]:
    return blocks(tf[ECR], "resource", 2)


@pytest.fixture(scope="module")
def mirror(tf: dict[str, str]) -> dict[str, str]:
    return blocks(tf[MIRROR], "resource", 2)


# ----------------------------------------------------------------------------------- repositories and key
@pytest.mark.parametrize("repo", ["aws_ecr_repository.bench", "aws_ecr_repository.sglang_runtime"])
def test_repository_is_encrypted_with_the_stack_key(ecr: dict[str, str], repo: str) -> None:
    body = ecr[repo]
    assert re.search(r'encryption_type\s*=\s*"KMS"', body), f"{repo}: encryption_type = \"KMS\" (CKV_AWS_136)"
    assert re.search(r"kms_key\s*=\s*aws_kms_key\.ecr\.arn", body), f"{repo}: kms_key = aws_kms_key.ecr.arn"
    assert "AES256" not in body


def test_the_aes256_checkov_skip_is_gone() -> None:
    raw = (CLUSTER / ECR).read_text()
    assert "CKV_AWS_136" not in raw, "the repositories are KMS-encrypted now: no skip for CKV_AWS_136"
    assert "AES256" not in raw


def test_key_and_alias_are_declared_in_ecr_tf(ecr: dict[str, str]) -> None:
    key = ecr.get("aws_kms_key.ecr", "")
    assert key, "aws_kms_key.ecr must be declared in ecr.tf"
    assert re.search(r"enable_key_rotation\s*=\s*true", key), "rotation on (CKV_AWS_7)"
    m = re.search(r"deletion_window_in_days\s*=\s*(\d+)", key)
    assert m and 7 <= int(m.group(1)) <= 30, "explicit deletion window of 7 to 30 days"
    assert re.search(r"(?m)^\s*policy\s*=", key), "an explicit key policy (CKV2_AWS_64)"
    alias = ecr.get("aws_kms_alias.ecr", "")
    assert re.search(r"target_key_id\s*=\s*aws_kms_key\.ecr\.key_id", alias), "aws_kms_alias.ecr targets aws_kms_key.ecr"
    assert re.search(r'name\s*=\s*"alias/\$\{local\.cluster_name\}-ecr"', alias), "alias/<cluster_name>-ecr"


def test_key_policy_is_derived_from_the_data_sources(tf: dict[str, str], ecr: dict[str, str]) -> None:
    policy = ecr["aws_kms_key.ecr"]
    joined = "\n".join(tf.values())
    root = r'ecr_kms_account_root\s*=\s*"arn:\$\{data\.aws_partition\.current\.partition\}:iam::\$\{local\.account_id\}:root"'
    assert re.search(root, joined), "local.ecr_kms_account_root = arn:<partition>:iam::<account>:root from the data sources"
    assert re.search(r"AWS\s*=\s*local\.ecr_kms_account_root", policy), "the principal of the key policy is the account"
    via = r'"kms:ViaService"\s*=\s*"ecr\.\$\{var\.aws_region\}\.\$\{data\.aws_partition\.current\.dns_suffix\}"'
    assert re.search(via, policy), "kms:ViaService = ecr.<region>.<dns suffix> (partition-aware)"
    assert re.search(r'"kms:CallerAccount"\s*=\s*local\.account_id', policy)
    assert '"kms:*"' not in policy, "no kms:* action"
    assert '"*"' not in re.sub(r'Resource\s*=\s*"\*"', "", policy), "the only \"*\" is the key policy Resource (the key itself)"


# ----------------------------------------------------------------------------------- image mirror
def test_image_mirror_policy_needs_no_kms(mirror: dict[str, str]) -> None:
    assert "kms:" not in mirror["aws_iam_policy.image_mirror"], "ECR decrypts and encrypts under its own grant on the key"


def test_get_authorization_token_is_the_only_credential_action_and_is_suppressed_precisely(mirror: dict[str, str]) -> None:
    raw = (CLUSTER / MIRROR).read_text()
    # permission policies only: sts:AssumeRole in the trust policy (assume_role_policy) is granted to EKS Pod Identity
    policies = [b for n, b in mirror.items() if n.split(".")[0] in {"aws_iam_policy", "aws_iam_role_policy"}]
    assert policies and all("assume_role_policy" not in b for b in policies)
    code = "\n".join(policies)
    found = [a for a in CREDS_ACTIONS if re.search(rf'"{re.escape(a)}"', code, flags=re.IGNORECASE)]
    assert found == ["ecr:GetAuthorizationToken"], f"credential-returning actions in {MIRROR}: {found}"
    assert not re.search(r'"[a-z0-9-]+:[A-Za-z]*\*"', code), "no wildcard action (service:* or a prefix*)"
    lines = [ln for ln in raw.splitlines() if "ecr:GetAuthorizationToken" in ln and not ln.lstrip().startswith("#")]
    assert len(lines) == 1, lines
    m = re.search(rf"#\s*nosemgrep:\s*{re.escape(CREDS_RULE)}\b\s*(.*)$", lines[0])
    assert m, f"the Action line carries '# nosemgrep: {CREDS_RULE} <reason>' on the same line: {lines[0]!r}"
    assert len(m.group(1).split()) >= 4, "the suppression states a reason"
    assert raw.count("nosemgrep") == 1, "exactly one suppression in the file"


def test_get_authorization_token_statement_is_alone_on_resource_star(mirror: dict[str, str]) -> None:
    policy = mirror["aws_iam_policy.image_mirror"]
    stars = [m.start() for m in re.finditer(r'Resource\s*=\s*"\*"', policy)]
    assert len(stars) == 1, "one Resource \"*\" statement"
    stmt = policy[policy.rfind("{", 0, stars[0]) : policy.find("}", stars[0]) + 1]
    assert re.search(r'Action\s*=\s*\[\s*"ecr:GetAuthorizationToken"\s*\]', stmt), stmt


# ----------------------------------------------------------------------------------- terraform test
def test_tftest_file_exists() -> None:
    assert (CLUSTER / "tests" / "ecr_kms.tftest.hcl").is_file()


def _terraform() -> str | None:
    return os.environ.get("TERRAFORM_BIN") or shutil.which("terraform")


def test_terraform_test_passes_for_the_ecr_key() -> None:
    """Runs the plan-level test (mock providers, no AWS) when a Terraform >= 1.12 and an initialised ``terraform/cluster`` exist."""
    binary = _terraform()
    if binary is None or not (CLUSTER / ".terraform").is_dir():
        pytest.skip("terraform binary or `terraform init -backend=false` in terraform/cluster missing")
    ver = lib_proc.run([binary, "version"], capture_output=True, text=True, check=False).stdout
    m = re.search(r"Terraform v1\.(\d+)", ver)
    if not m or int(m.group(1)) < 12:
        pytest.skip(f"Terraform >= 1.12 required, found: {ver.splitlines()[0] if ver else 'none'} (set TERRAFORM_BIN)")
    proc = lib_proc.run([binary, "test", "-no-color", "-filter=tests/ecr_kms.tftest.hcl"], cwd=CLUSTER, capture_output=True, text=True, check=False)
    assert proc.returncode == 0, proc.stdout[-4000:] + proc.stderr[-2000:]
