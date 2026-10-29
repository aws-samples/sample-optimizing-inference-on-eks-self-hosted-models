# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
#
# RED tests: the ECR repositories are encrypted with a customer managed KMS key of the stack (Checkov CKV_AWS_136), and
# the image-mirror Pod Identity policy stays least privilege (terraform/cluster/ecr.tf, terraform/cluster/iam-image-mirror.tf).
# Providers are mocked (nothing touches AWS or a cluster); the overrides are the ones of model_stager.tftest.hcl, plus a
# fixed caller identity and partition so the key policy is known at plan time. The account segment is not 12 digits.
#
# Contract: aws_kms_key.ecr (rotation on, 7-30 day deletion window, key policy: the account administers the key, and the
# cryptographic operations and grants are allowed only through ECR of the stack region via kms:ViaService),
# aws_kms_alias.ecr, and both repositories (the optional sglang-runtime one created) with encryption_type = "KMS" and
# kms_key = aws_kms_key.ecr.arn. ECR does the cryptography under the grant it creates on the key for each repository,
# so no pull or push principal gets a KMS action (the Auto Mode node role, the image mirror, the bench): the image-mirror
# policy has no kms: action. Its only Resource "*" statement is ecr:GetAuthorizationToken (no resource-level scoping)
# and no other credential-returning action is allowed.

mock_provider "aws" {}
# grafana-edge.tf configures a second, aliased aws provider (us-east-1, for the CLOUDFRONT web ACL): it is
# mocked too, so `terraform test` needs no AWS credentials.
mock_provider "aws" {
  alias = "us_east_1"
}
mock_provider "kubernetes" {}
mock_provider "helm" {}
mock_provider "kubectl" {}

override_module {
  target = module.eks
  outputs = {
    cluster_name                       = "demo-eks"
    cluster_endpoint                   = "https://example.invalid"
    cluster_certificate_authority_data = ""
    cluster_arn                        = "arn:aws:eks:us-east-2:000000000:cluster/demo-eks"
    node_iam_role_name                 = "node-role"
    cluster_primary_security_group_id  = "sg-placeholder"
  }
}

override_module {
  target = module.vpc
  outputs = {
    vpc_id                  = "vpc-placeholder"
    azs                     = ["us-east-2a", "us-east-2b", "us-east-2c"]
    private_subnets         = ["subnet-a", "subnet-b", "subnet-c"]
    private_route_table_ids = []
    public_route_table_ids  = []
  }
}

override_data {
  target = data.aws_caller_identity.current
  values = {
    account_id = "000000000"
    arn        = "arn:aws:iam::000000000:role/deployer"
  }
}

override_data {
  target = data.aws_partition.current
  values = {
    partition  = "aws"
    dns_suffix = "amazonaws.com"
  }
}

override_data {
  target = data.aws_availability_zones.available
  values = {
    zone_ids = ["use2-az1", "use2-az2", "use2-az3"]
    names    = ["us-east-2a", "us-east-2b", "us-east-2c"]
  }
}

override_data {
  target = data.aws_ec2_instance_type.gpu
  values = {
    maximum_network_cards   = 8
    efa_supported           = true
    efa_maximum_interfaces  = 8
    default_vcpus           = 192
    memory_size             = 2097152
    total_instance_storage  = 30400
    supported_architectures = ["x86_64"]
    gpus                    = [{ name = "B200", count = 8, memory_size = 183359, manufacturer = "NVIDIA" }]
  }
}

override_data {
  target = data.aws_ec2_instance_type_offerings.gpu
  values = {
    instance_types = ["p6-b200.48xlarge"]
    locations      = ["use2-az1"]
  }
}

# The key ARN is computed: fixed at plan time so the repositories' kms_key can be compared with it.
override_resource {
  target          = aws_kms_key.ecr
  override_during = plan
  values = {
    arn    = "arn:aws:kms:us-east-2:000000000:key/mock-ecr-key-id"
    key_id = "mock-ecr-key-id"
  }
}

variables {
  aws_region               = "us-east-2"
  cluster_name             = "demo-eks"
  eks_public_access_cidrs  = ["203.0.113.0/24"]
  gpu_instance_types       = ["p6-b200.48xlarge"]
  gpu_capacity_type        = "on-demand"
  capacity_reservation_ids = []
  model_id                 = "acme/tiny-model"
  model_revision           = "0123456789abcdef0123456789abcdef01234567"
  model_bucket_name        = "demo-models-bucket"
}

run "both_repositories_are_encrypted_with_the_stack_key" {
  command = plan

  # the sglang-runtime repository is optional (ecr_runtime_mirror.tftest.hcl): created here to check both
  variables {
    create_runtime_mirror_repository = true
  }

  assert {
    condition     = aws_ecr_repository.bench.encryption_configuration[0].encryption_type == "KMS" && aws_ecr_repository.sglang_runtime[0].encryption_configuration[0].encryption_type == "KMS"
    error_message = "both repositories use encryption_type = \"KMS\" (CKV_AWS_136), not AES256"
  }
  assert {
    condition = (
      aws_ecr_repository.bench.encryption_configuration[0].kms_key == "arn:aws:kms:us-east-2:000000000:key/mock-ecr-key-id" &&
      aws_ecr_repository.sglang_runtime[0].encryption_configuration[0].kms_key == "arn:aws:kms:us-east-2:000000000:key/mock-ecr-key-id"
    )
    error_message = "both repositories use the customer managed key of the stack (aws_kms_key.ecr.arn), not the AWS managed aws/ecr key"
  }
  # the rest of the repository contract is unchanged
  assert {
    condition     = aws_ecr_repository.bench.image_tag_mutability == "IMMUTABLE" && aws_ecr_repository.sglang_runtime[0].image_tag_mutability == "IMMUTABLE"
    error_message = "tags stay immutable"
  }
  assert {
    condition     = !aws_ecr_repository.bench.force_delete && !aws_ecr_repository.sglang_runtime[0].force_delete
    error_message = "force_delete stays false"
  }
  assert {
    condition     = aws_ecr_repository.bench.image_scanning_configuration[0].scan_on_push && aws_ecr_repository.sglang_runtime[0].image_scanning_configuration[0].scan_on_push
    error_message = "scan on push stays on"
  }
}

run "the_key_rotates_has_a_deletion_window_and_an_alias" {
  command = plan

  assert {
    condition     = aws_kms_key.ecr.enable_key_rotation
    error_message = "automatic key rotation is on (CKV_AWS_7)"
  }
  assert {
    condition     = aws_kms_key.ecr.deletion_window_in_days >= 7 && aws_kms_key.ecr.deletion_window_in_days <= 30
    error_message = "an explicit deletion window of 7 to 30 days"
  }
  assert {
    condition     = aws_kms_key.ecr.is_enabled && aws_kms_key.ecr.key_usage == "ENCRYPT_DECRYPT" && aws_kms_key.ecr.customer_master_key_spec == "SYMMETRIC_DEFAULT"
    error_message = "an enabled symmetric encryption key (ECR supports symmetric keys only)"
  }
  assert {
    condition     = aws_kms_alias.ecr.name == "alias/demo-eks-ecr" && aws_kms_alias.ecr.target_key_id == "mock-ecr-key-id"
    error_message = "alias/<cluster_name>-ecr points at the stack key"
  }
}

run "the_key_policy_is_least_privilege" {
  command = plan

  # only Allow statements, every one for the account itself (no "*" principal, no other account, no service principal)
  assert {
    condition     = alltrue([for s in jsondecode(aws_kms_key.ecr.policy).Statement : s.Effect == "Allow" && s.Principal == { AWS = "arn:aws:iam::000000000:root" }])
    error_message = "only Allow statements, and the principal is always arn:<partition>:iam::<account>:root"
  }
  # the account can always administer the key (PutKeyPolicy keeps it manageable), and nothing is a wildcard
  assert {
    condition = length([
      for s in jsondecode(aws_kms_key.ecr.policy).Statement : s
      if !contains(keys(s), "Condition") && contains(flatten([s.Action]), "kms:PutKeyPolicy") && contains(flatten([s.Action]), "kms:ScheduleKeyDeletion")
    ]) == 1
    error_message = "one unconditioned administration statement for the account (PutKeyPolicy, ScheduleKeyDeletion, ...)"
  }
  assert {
    condition     = alltrue([for a in flatten([for s in jsondecode(aws_kms_key.ecr.policy).Statement : s.Action]) : a != "kms:*" && a != "*"])
    error_message = "no kms:* or * action"
  }
  # the administration statement cannot use the key nor create grants (a grant would bypass kms:ViaService)
  assert {
    condition = alltrue([
      for s in jsondecode(aws_kms_key.ecr.policy).Statement :
      length(setintersection(toset(flatten([s.Action])), toset(["kms:Encrypt", "kms:Decrypt", "kms:ReEncrypt*", "kms:ReEncryptFrom", "kms:ReEncryptTo", "kms:GenerateDataKey*", "kms:GenerateDataKey", "kms:GenerateDataKeyWithoutPlaintext", "kms:CreateGrant"]))) == 0
      if !contains(keys(s), "Condition")
    ])
    error_message = "the unconditioned statement has no cryptographic action and no kms:CreateGrant"
  }
  # cryptographic operations and grants: only through ECR in the stack region, only for this account
  assert {
    condition = alltrue([
      for s in jsondecode(aws_kms_key.ecr.policy).Statement :
      s.Condition.StringEquals["kms:ViaService"] == "ecr.us-east-2.amazonaws.com" && s.Condition.StringEquals["kms:CallerAccount"] == "000000000"
      if contains(keys(s), "Condition")
    ])
    error_message = "the usage statement is conditioned on kms:ViaService = ecr.<region>.<dns suffix> and kms:CallerAccount = <account>"
  }
  assert {
    condition = length([
      for s in jsondecode(aws_kms_key.ecr.policy).Statement : s
      if contains(keys(s), "Condition") && contains(flatten([s.Action]), "kms:CreateGrant") && contains(flatten([s.Action]), "kms:Decrypt") && contains(flatten([s.Action]), "kms:GenerateDataKey*")
    ]) == 1
    error_message = "one usage statement through ECR: kms:CreateGrant (ECR's grant at repository creation), kms:GenerateDataKey* (push), kms:Decrypt (pull)"
  }
}

run "the_image_mirror_policy_has_no_kms_and_one_unavoidable_credential_action" {
  command = plan

  # ECR encrypts and decrypts under its own grant: the mirror pushes and pulls with ecr: actions only
  assert {
    condition     = alltrue([for a in flatten([for s in jsondecode(aws_iam_policy.image_mirror.policy).Statement : s.Action]) : startswith(a, "ecr:")])
    error_message = "ecr: actions only (no kms:, sts:, iam:, ...)"
  }
  assert {
    condition     = alltrue([for s in jsondecode(aws_iam_policy.image_mirror.policy).Statement : s.Effect == "Allow" && alltrue([for a in flatten([s.Action]) : !strcontains(a, "*")])])
    error_message = "no wildcard actions and no Deny/NotAction tricks"
  }
  # the only Resource "*" statement holds exactly ecr:GetAuthorizationToken (it has no resource-level scoping)
  assert {
    condition = alltrue([
      for s in jsondecode(aws_iam_policy.image_mirror.policy).Statement :
      flatten([s.Action]) == ["ecr:GetAuthorizationToken"]
      if contains(flatten([s.Resource]), "*")
    ])
    error_message = "Resource \"*\" only for a statement whose single action is ecr:GetAuthorizationToken"
  }
  assert {
    condition     = length([for s in jsondecode(aws_iam_policy.image_mirror.policy).Statement : s if contains(flatten([s.Action]), "ecr:GetAuthorizationToken")]) == 1
    error_message = "ecr:GetAuthorizationToken appears in exactly one statement"
  }
  # ecr: only and no wildcard (the first two asserts) leave ecr:GetAuthorizationToken as the only credential-returning
  # action of the Semgrep no-iam-creds-exposure list (the others are sts:, iam:, ec2:, rds-db:, ... or a service:* form)
  # push and pull stay scoped to the project repositories
  assert {
    condition = alltrue([
      for s in jsondecode(aws_iam_policy.image_mirror.policy).Statement :
      alltrue([for r in flatten([s.Resource]) : r == "arn:aws:ecr:us-east-2:000000000:repository/dynamo-sample/*"])
      if s.Sid == "PushProjectRepos"
    ])
    error_message = "push actions only on arn:<partition>:ecr:<region>:<account>:repository/<project_name>/*"
  }
}
