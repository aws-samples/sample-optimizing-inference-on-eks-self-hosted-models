# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

################################################################################
# Benchmark results writer through EKS Pod Identity
#
# The results bucket is owned by ../bootstrap (persistent, versioned) and
# referenced here by name/ARN only. The role can write, read and
# list but never delete (no Delete*): with versioning, overwrites keep history.
# Assumed by bench/matrix-driver and bench/bench (ServiceAccounts owned by the
# scenario manifests; Pod Identity associations do not require them to exist)
# and by node-ops/compile-cache (compile/JIT cache upload Job, which needs the
# privileged node-ops namespace for hostPath /mnt/.ephemeral/compile-cache).
################################################################################

locals {
  results_writer_namespace        = "bench"
  results_writer_service_accounts = ["matrix-driver", "bench"]

  compile_cache_namespace       = "node-ops"
  compile_cache_service_account = "compile-cache"
}

resource "aws_iam_role" "results_writer" {
  name = "${local.cluster_name}-results-writer"

  # Restricted through the session tags EKS Pod Identity sets (AWS EKS UG
  # pod-id-role): one statement per namespace so that each ServiceAccount list
  # applies only to its own namespace (a list value in StringEquals matches any
  # of its entries).
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "AllowEksAuthToAssumeRoleForPodIdentity"
        Effect    = "Allow"
        Principal = { Service = "pods.eks.amazonaws.com" }
        Action    = ["sts:AssumeRole", "sts:TagSession"]
        Condition = {
          StringEquals = {
            "aws:RequestTag/kubernetes-namespace"       = local.results_writer_namespace
            "aws:RequestTag/kubernetes-service-account" = local.results_writer_service_accounts
          }
        }
      },
      {
        Sid       = "AllowEksAuthToAssumeRoleForCompileCache"
        Effect    = "Allow"
        Principal = { Service = "pods.eks.amazonaws.com" }
        Action    = ["sts:AssumeRole", "sts:TagSession"]
        Condition = {
          StringEquals = {
            "aws:RequestTag/kubernetes-namespace"       = local.compile_cache_namespace
            "aws:RequestTag/kubernetes-service-account" = local.compile_cache_service_account
          }
        }
      },
    ]
  })

  tags = {
    Name    = "${local.cluster_name}-results-writer"
    Purpose = "Benchmark results write access via EKS Pod Identity"
  }
}

resource "aws_iam_policy" "results_writer" {
  name        = "${local.cluster_name}-results-writer"
  description = "Write/read/list (no delete) on ${local.results_bucket_name} for the benchmark pods"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "ListResultsBucket"
        Effect   = "Allow"
        Action   = ["s3:ListBucket"]
        Resource = [local.results_bucket_arn]
      },
      {
        Sid      = "ReadWriteResultsObjects"
        Effect   = "Allow"
        Action   = ["s3:PutObject", "s3:GetObject", "s3:AbortMultipartUpload"]
        Resource = ["${local.results_bucket_arn}/*"]
      },
    ]
  })
}

resource "aws_iam_role_policy_attachment" "results_writer" {
  role       = aws_iam_role.results_writer.name
  policy_arn = aws_iam_policy.results_writer.arn
}

resource "aws_eks_pod_identity_association" "results_writer" {
  for_each = toset(local.results_writer_service_accounts)

  cluster_name    = module.eks.cluster_name
  namespace       = local.results_writer_namespace
  service_account = each.key
  role_arn        = aws_iam_role.results_writer.arn

  tags = local.tags

  # The bench namespace must exist before pods use the association.
  depends_on = [kubectl_manifest.namespace]
}

resource "kubernetes_service_account_v1" "compile_cache" {
  metadata {
    name      = local.compile_cache_service_account
    namespace = local.compile_cache_namespace
    labels = {
      "app.kubernetes.io/name"       = "compile-cache"
      "app.kubernetes.io/managed-by" = "terraform"
    }
  }

  depends_on = [kubectl_manifest.namespace]
}

# Reuses the results-writer role (results bucket write/read/list, no delete);
# the upload Job writes under compile-cache/ in the results bucket.
resource "aws_eks_pod_identity_association" "compile_cache" {
  cluster_name    = module.eks.cluster_name
  namespace       = kubernetes_service_account_v1.compile_cache.metadata[0].namespace
  service_account = kubernetes_service_account_v1.compile_cache.metadata[0].name
  role_arn        = aws_iam_role.results_writer.arn

  tags = local.tags
}
