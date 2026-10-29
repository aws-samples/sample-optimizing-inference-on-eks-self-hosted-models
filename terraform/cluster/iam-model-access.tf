# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

################################################################################
# Model access through EKS Pod Identity
#
# The model bucket is owned by ../bootstrap and referenced here by name/ARN
# only (no data source, so this stack plans before it exists).
#
# model-reader (read-only) is assumed by node-ops/model-prefetch (S3 -> NVMe
# prefetch Job). Optional var.hf_token_secret_arn: GetSecretValue on THAT
# secret only (gated Hugging Face models). Terraform never creates or reads the
# secret, so the token value never enters state; a secret encrypted with a
# customer managed KMS key also needs kms:Decrypt on that key (not granted here).
#
# model-stager is assumed by node-ops/model-stager (Hugging Face -> S3 staging
# Job, scripts/10-stage-model.sh): write access under local.model_s3_prefix of
# the model bucket only (no delete, no other prefix, no Secrets Manager; its
# token travels in an ephemeral Kubernetes Secret).
################################################################################

locals {
  model_prefetch_namespace       = "node-ops"
  model_prefetch_service_account = "model-prefetch"
  model_stager_service_account   = "model-stager"
  # MODEL_S3_PREFIX of the generated env (same default as scripts/gen_env.py).
  model_s3_prefix = coalesce(var.model_s3_prefix, "${var.model_id}/${var.model_revision}")
}

resource "aws_iam_role" "model_reader" {
  name = "${local.cluster_name}-model-reader"

  # Restricted to the node-ops/model-prefetch ServiceAccount through the
  # session tags EKS Pod Identity sets (AWS EKS UG pod-id-role).
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "AllowEksAuthToAssumeRoleForPodIdentity"
      Effect    = "Allow"
      Principal = { Service = "pods.eks.amazonaws.com" }
      Action    = ["sts:AssumeRole", "sts:TagSession"]
      Condition = {
        StringEquals = {
          "aws:RequestTag/kubernetes-namespace"       = local.model_prefetch_namespace
          "aws:RequestTag/kubernetes-service-account" = local.model_prefetch_service_account
        }
      }
    }]
  })

  tags = {
    Name    = "${local.cluster_name}-model-reader"
    Purpose = "Read-only model bucket access via EKS Pod Identity"
  }
}

resource "aws_iam_policy" "model_reader" {
  name        = "${local.cluster_name}-model-reader"
  description = "Read-only access to ${local.model_bucket_name} for the model prefetch Job"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "ListModelBucket"
        Effect   = "Allow"
        Action   = ["s3:ListBucket"]
        Resource = [local.model_bucket_arn]
      },
      {
        Sid      = "ReadModelObjects"
        Effect   = "Allow"
        Action   = ["s3:GetObject"]
        Resource = ["${local.model_bucket_arn}/*"]
      },
    ]
  })
}

resource "aws_iam_role_policy_attachment" "model_reader" {
  role       = aws_iam_role.model_reader.name
  policy_arn = aws_iam_policy.model_reader.arn
}

resource "aws_iam_role_policy" "model_reader_hf_token" {
  count = var.hf_token_secret_arn == null ? 0 : 1

  name = "hf-token-read"
  role = aws_iam_role.model_reader.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid      = "ReadHuggingFaceToken"
      Effect   = "Allow"
      Action   = ["secretsmanager:GetSecretValue"]
      Resource = [var.hf_token_secret_arn]
    }]
  })
}

resource "kubernetes_service_account_v1" "model_prefetch" {
  metadata {
    name      = local.model_prefetch_service_account
    namespace = local.model_prefetch_namespace
    labels = {
      "app.kubernetes.io/name"       = "model-prefetch"
      "app.kubernetes.io/managed-by" = "terraform"
    }
  }

  depends_on = [kubectl_manifest.namespace]
}

resource "aws_eks_pod_identity_association" "model_prefetch" {
  cluster_name    = module.eks.cluster_name
  namespace       = kubernetes_service_account_v1.model_prefetch.metadata[0].namespace
  service_account = kubernetes_service_account_v1.model_prefetch.metadata[0].name
  role_arn        = aws_iam_role.model_reader.arn

  tags = local.tags
}

resource "aws_iam_role" "model_stager" {
  name = "${local.cluster_name}-model-stager"

  # Restricted to the node-ops/model-stager ServiceAccount through the session
  # tags EKS Pod Identity sets (AWS EKS UG pod-id-role).
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "AllowEksAuthToAssumeRoleForPodIdentity"
      Effect    = "Allow"
      Principal = { Service = "pods.eks.amazonaws.com" }
      Action    = ["sts:AssumeRole", "sts:TagSession"]
      Condition = {
        StringEquals = {
          "aws:RequestTag/kubernetes-namespace"       = local.model_prefetch_namespace
          "aws:RequestTag/kubernetes-service-account" = local.model_stager_service_account
        }
      }
    }]
  })

  tags = {
    Name    = "${local.cluster_name}-model-stager"
    Purpose = "Model staging writes under one prefix of the model bucket via EKS Pod Identity"
  }
}

resource "aws_iam_policy" "model_stager" {
  name        = "${local.cluster_name}-model-stager"
  description = "Write access under one model prefix of ${local.model_bucket_name} for the model staging Job"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        # Inventory and verification of the prefix (list_objects_v2).
        Sid       = "ListModelPrefix"
        Effect    = "Allow"
        Action    = ["s3:ListBucket"]
        Resource  = [local.model_bucket_arn]
        Condition = { StringLike = { "s3:prefix" = ["${local.model_s3_prefix}/*"] } }
      },
      {
        # Uploads (multipart included), the .done marker, and reading it back.
        Sid      = "StageModelObjects"
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:PutObject", "s3:AbortMultipartUpload"]
        Resource = ["${local.model_bucket_arn}/${local.model_s3_prefix}/*"]
      },
    ]
  })
}

resource "aws_iam_role_policy_attachment" "model_stager" {
  role       = aws_iam_role.model_stager.name
  policy_arn = aws_iam_policy.model_stager.arn
}

resource "kubernetes_service_account_v1" "model_stager" {
  metadata {
    name      = local.model_stager_service_account
    namespace = local.model_prefetch_namespace
    labels = {
      "app.kubernetes.io/name"       = "model-stage"
      "app.kubernetes.io/managed-by" = "terraform"
    }
  }

  depends_on = [kubectl_manifest.namespace]
}

resource "aws_eks_pod_identity_association" "model_stager" {
  cluster_name    = module.eks.cluster_name
  namespace       = kubernetes_service_account_v1.model_stager.metadata[0].namespace
  service_account = kubernetes_service_account_v1.model_stager.metadata[0].name
  role_arn        = aws_iam_role.model_stager.arn

  tags = local.tags
}
