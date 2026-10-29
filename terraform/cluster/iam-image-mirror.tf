# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

################################################################################
# Image mirror (push) through EKS Pod Identity
#
# Assumed only by the ServiceAccount node-ops/image-mirror created below. The
# repository ships no Job that uses it: docs/DEPLOY.md copies the runtime image
# with crane from the workstation, under the operator's own credentials. It is
# there for an in-cluster copy Job you add yourself (for example `crane copy`,
# which keeps the manifest digest), run as that ServiceAccount, when the copy
# must run inside the VPC.
# Push is limited to the ${var.project_name}/* repositories; GetAuthorizationToken
# has no resource-level scoping. With var.image_mirror_source_region set, the
# Job may also read (never write) the bench repository of that region (same
# account) and, with var.create_runtime_mirror_repository (the copy target of
# that image here), its sglang-runtime repository; null = no cross-region
# statement. No KMS action: the repositories are KMS-encrypted (ecr.tf), and
# ECR encrypts on push and decrypts on pull under the grant it holds on the key.
################################################################################

locals {
  image_mirror_namespace       = "node-ops"
  image_mirror_service_account = "image-mirror"
  project_ecr_repos_arn        = "arn:${data.aws_partition.current.partition}:ecr:${var.aws_region}:${data.aws_caller_identity.current.account_id}:repository/${var.project_name}/*"
  image_mirror_source_repo_arns = var.image_mirror_source_region == null ? [] : [
    for repo in concat(["bench"], var.create_runtime_mirror_repository ? ["sglang-runtime"] : []) :
    "arn:${data.aws_partition.current.partition}:ecr:${var.image_mirror_source_region}:${data.aws_caller_identity.current.account_id}:repository/${var.project_name}/${repo}"
  ]
}

resource "aws_iam_role" "image_mirror" {
  name = "${local.cluster_name}-image-mirror"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "AllowEksAuthToAssumeRoleForPodIdentity"
      Effect    = "Allow"
      Principal = { Service = "pods.eks.amazonaws.com" }
      Action    = ["sts:AssumeRole", "sts:TagSession"]
      Condition = {
        StringEquals = {
          "aws:RequestTag/kubernetes-namespace"       = local.image_mirror_namespace
          "aws:RequestTag/kubernetes-service-account" = local.image_mirror_service_account
        }
      }
    }]
  })

  tags = {
    Name    = "${local.cluster_name}-image-mirror"
    Purpose = "ECR push for the image mirror Job via EKS Pod Identity"
  }
}

resource "aws_iam_policy" "image_mirror" {
  name        = "${local.cluster_name}-image-mirror"
  description = "Push to the ${var.project_name}/* ECR repositories for the image mirror Job"

  policy = jsonencode({
    Version = "2012-10-17"
    # The cross-region statement is dropped when no source region is set.
    Statement = [for s in [
      {
        Sid      = "EcrAuthToken"
        Effect   = "Allow"
        Action   = ["ecr:GetAuthorizationToken"] # nosemgrep: terraform.lang.security.iam.no-iam-creds-exposure, terraform.lang.security.iam.no-iam-creds-exposure.no-iam-creds-exposure -- docker/crane login to ECR needs this registry token (ECR UG image-push-iam), and the action supports only Resource "*"
        Resource = "*"
      },
      {
        Sid    = "PushProjectRepos"
        Effect = "Allow"
        Action = [
          "ecr:BatchCheckLayerAvailability",
          "ecr:InitiateLayerUpload",
          "ecr:UploadLayerPart",
          "ecr:CompleteLayerUpload",
          "ecr:PutImage",
          "ecr:BatchGetImage",
          "ecr:GetDownloadUrlForLayer",
          "ecr:DescribeImages",
        ]
        Resource = [local.project_ecr_repos_arn]
      },
      {
        Sid    = "PullSourceRegionRepos"
        Effect = "Allow"
        Action = [
          "ecr:BatchGetImage",
          "ecr:GetDownloadUrlForLayer",
          "ecr:BatchCheckLayerAvailability",
        ]
        Resource = local.image_mirror_source_repo_arns
      },
    ] : s if s.Sid != "PullSourceRegionRepos" || length(local.image_mirror_source_repo_arns) > 0]
  })
}

resource "aws_iam_role_policy_attachment" "image_mirror" {
  role       = aws_iam_role.image_mirror.name
  policy_arn = aws_iam_policy.image_mirror.arn
}

resource "kubernetes_service_account_v1" "image_mirror" {
  metadata {
    name      = local.image_mirror_service_account
    namespace = local.image_mirror_namespace
    labels = {
      "app.kubernetes.io/name"       = "image-mirror"
      "app.kubernetes.io/managed-by" = "terraform"
    }
  }

  depends_on = [kubectl_manifest.namespace]
}

resource "aws_eks_pod_identity_association" "image_mirror" {
  cluster_name    = module.eks.cluster_name
  namespace       = kubernetes_service_account_v1.image_mirror.metadata[0].namespace
  service_account = kubernetes_service_account_v1.image_mirror.metadata[0].name
  role_arn        = aws_iam_role.image_mirror.arn

  tags = local.tags
}
