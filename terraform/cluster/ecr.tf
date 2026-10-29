# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

################################################################################
# ECR repositories
#
#   <project_name>/bench           benchmark harness image (always created)
#   <project_name>/sglang-runtime  optional, only with
#                                  create_runtime_mirror_repository = true: the
#                                  digest-preserving copy of the public NGC
#                                  runtime image (docs/DEPLOY.md, "Mirror the
#                                  image to your ECR"). By default the workers
#                                  pull runtime_image from nvcr.io directly.
#                                  A stack applied before the variable existed
#                                  plans to destroy it unless the variable is
#                                  true; the delete fails while it holds images.
#
# Auto Mode nodes pull them with the node role, which carries the AWS managed
# AmazonEC2ContainerRegistryPullOnly policy (ecr:GetAuthorizationToken,
# BatchGetImage, GetDownloadUrlForLayer on "*"); same-account pulls need no
# repository policy. Pushes go through the image-mirror Pod Identity role
# (iam-image-mirror.tf).
#
# The repositories are encrypted with aws_kms_key.ecr, a customer managed key
# of the stack (about US$1 a month); the key always exists, like the bench
# repository. At repository creation ECR creates a grant on the key with the
# repository as grantee and then encrypts on push and decrypts on pull under
# that grant (ECR UG "Encryption at rest"), so no pull or push principal (node
# role, image mirror, bench) needs a KMS permission. Only
# the identity that creates and deletes the repositories does: kms:CreateGrant
# and kms:DescribeKey, both allowed by the key policy below (CreateGrant only
# through ECR), plus kms:RetireGrant in its IAM policy. The encryption of a
# repository cannot change after creation: on a stack whose repositories were
# created with S3-managed keys (the earlier setting), Terraform replaces them,
# and the delete fails while they hold images (force_delete = false); delete
# the images first, then mirror again.
################################################################################

locals {
  # Shared lifecycle policy: expire only UNTAGGED images after N days. Tagged
  # images (immutable mirror tags, bench git-SHA tags) are never expired.
  # Untagged children of a multi-arch index (platform/attestation manifests)
  # are protected while the index exists (ECR UG LifecyclePolicies).
  ecr_expire_untagged_policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Expire untagged images after ${var.ecr_untagged_expire_days} days"
      selection = {
        tagStatus   = "untagged"
        countType   = "sinceImagePushed"
        countUnit   = "days"
        countNumber = var.ecr_untagged_expire_days
      }
      action = {
        type = "expire"
      }
    }]
  })

  ecr_kms_account_root = "arn:${data.aws_partition.current.partition}:iam::${local.account_id}:root"
}

resource "aws_kms_key" "ecr" {
  description              = "Encrypts the ${var.project_name}/* ECR repositories of ${local.cluster_name}"
  key_usage                = "ENCRYPT_DECRYPT" # ECR accepts symmetric encryption keys only
  customer_master_key_spec = "SYMMETRIC_DEFAULT"
  is_enabled               = true
  enable_key_rotation      = true
  deletion_window_in_days  = 30 # no charge while pending deletion; the longest window to cancel a mistake

  # The account administers the key (it stays manageable through PutKeyPolicy)
  # but cannot use it directly: encryption, decryption and grants are allowed
  # only through ECR in this region, for this account (the ViaService pattern of
  # the KMS key policies of AWS services). No statement names "*" as principal.
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "KeyAdministrationByTheAccount"
        Effect    = "Allow"
        Principal = { AWS = local.ecr_kms_account_root }
        Action = [
          "kms:CancelKeyDeletion",
          "kms:CreateAlias",
          "kms:DeleteAlias",
          "kms:DescribeKey",
          "kms:DisableKey",
          "kms:DisableKeyRotation",
          "kms:EnableKey",
          "kms:EnableKeyRotation",
          "kms:GetKeyPolicy",
          "kms:GetKeyRotationStatus",
          "kms:ListGrants",
          "kms:ListKeyPolicies",
          "kms:ListKeyRotations",
          "kms:ListResourceTags",
          "kms:PutKeyPolicy",
          "kms:RetireGrant",
          "kms:RevokeGrant",
          "kms:RotateKeyOnDemand",
          "kms:ScheduleKeyDeletion",
          "kms:TagResource",
          "kms:UntagResource",
          "kms:UpdateAlias",
          "kms:UpdateKeyDescription",
        ]
        Resource = "*" # in a key policy, "*" is this key
      },
      {
        Sid       = "UseOnlyThroughEcrOfThisAccount"
        Effect    = "Allow"
        Principal = { AWS = local.ecr_kms_account_root }
        Action = [
          "kms:CreateGrant",
          "kms:Decrypt",
          "kms:Encrypt",
          "kms:GenerateDataKey*",
          "kms:ReEncrypt*",
        ]
        Resource = "*"
        Condition = {
          StringEquals = {
            "kms:ViaService"    = "ecr.${var.aws_region}.${data.aws_partition.current.dns_suffix}"
            "kms:CallerAccount" = local.account_id
          }
        }
      },
    ]
  })

  tags = {
    Name    = "${local.cluster_name}-ecr"
    Purpose = "Encryption at rest of the ECR repositories of ${var.project_name}"
  }
}

resource "aws_kms_alias" "ecr" {
  name          = "alias/${local.cluster_name}-ecr"
  target_key_id = aws_kms_key.ecr.key_id
}

resource "aws_ecr_repository" "bench" {
  name                 = "${var.project_name}/bench"
  image_tag_mutability = "IMMUTABLE"
  force_delete         = false

  image_scanning_configuration {
    scan_on_push = true
  }

  encryption_configuration {
    encryption_type = "KMS"
    kms_key         = aws_kms_key.ecr.arn
  }

  tags = {
    Name    = "${var.project_name}/bench"
    Purpose = "Benchmark harness image"
  }
}

resource "aws_ecr_lifecycle_policy" "bench" {
  repository = aws_ecr_repository.bench.name
  policy     = local.ecr_expire_untagged_policy
}

resource "aws_ecr_repository" "sglang_runtime" {
  count = var.create_runtime_mirror_repository ? 1 : 0

  name                 = "${var.project_name}/sglang-runtime"
  image_tag_mutability = "IMMUTABLE"
  force_delete         = false

  image_scanning_configuration {
    scan_on_push = true
  }

  encryption_configuration {
    encryption_type = "KMS"
    kms_key         = aws_kms_key.ecr.arn
  }

  tags = {
    Name    = "${var.project_name}/sglang-runtime"
    Purpose = "Digest-preserving mirror of the nvcr.io sglang-runtime images"
  }
}

resource "aws_ecr_lifecycle_policy" "sglang_runtime" {
  count = var.create_runtime_mirror_repository ? 1 : 0

  repository = aws_ecr_repository.sglang_runtime[0].name
  policy     = local.ecr_expire_untagged_policy
}
