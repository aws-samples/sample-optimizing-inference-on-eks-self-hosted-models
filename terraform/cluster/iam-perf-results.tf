# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

################################################################################
# Benchmark results upload through EKS Pod Identity
#
# benchmarks/perf.yaml runs as the ServiceAccount perf in a cell namespace
# (s-*) and uploads its results under perf/ of the results bucket, owned by
# ../bootstrap (SSE-S3, versioned) and referenced here by ARN only.
#
# The role can only create objects under perf/: no read, list or delete, and no
# KMS (SSE-S3). s3:PutObject also covers the create, part and complete calls of
# a multipart upload; s3:AbortMultipartUpload stops a failed one (AWS S3 UG
# mpuoverview). perf.yaml creates the ServiceAccount: an association may name
# one that does not exist yet (AWS EKS UG pod-id-association).
################################################################################

locals {
  perf_service_account = "perf"
  perf_namespaces      = toset([for ns in keys(local.namespaces) : ns if startswith(ns, "s-")])
}

resource "aws_iam_role" "perf_results" {
  name = "${local.cluster_name}-perf-results"

  # Restricted to the perf ServiceAccount of the cell namespaces through the
  # session tags EKS Pod Identity sets (AWS EKS UG pod-id-role). Both keys must
  # match; a list value matches any of its entries.
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "AllowEksAuthToAssumeRoleForPodIdentity"
      Effect    = "Allow"
      Principal = { Service = "pods.eks.amazonaws.com" }
      Action    = ["sts:AssumeRole", "sts:TagSession"]
      Condition = {
        StringEquals = {
          "aws:RequestTag/kubernetes-namespace"       = sort(local.perf_namespaces)
          "aws:RequestTag/kubernetes-service-account" = local.perf_service_account
        }
      }
    }]
  })

  tags = {
    Name    = "${local.cluster_name}-perf-results"
    Purpose = "Benchmark results upload under perf/ of the results bucket via EKS Pod Identity"
  }
}

resource "aws_iam_policy" "perf_results" {
  name        = "${local.cluster_name}-perf-results"
  description = "Uploads under perf/ of ${local.results_bucket_name} for the benchmark Job"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid      = "UploadPerfResults"
      Effect   = "Allow"
      Action   = ["s3:PutObject", "s3:AbortMultipartUpload"]
      Resource = ["${local.results_bucket_arn}/perf/*"]
    }]
  })
}

resource "aws_iam_role_policy_attachment" "perf_results" {
  role       = aws_iam_role.perf_results.name
  policy_arn = aws_iam_policy.perf_results.arn
}

resource "aws_eks_pod_identity_association" "perf_results" {
  for_each = local.perf_namespaces

  cluster_name    = module.eks.cluster_name
  namespace       = each.key
  service_account = local.perf_service_account
  role_arn        = aws_iam_role.perf_results.arn

  tags = local.tags

  depends_on = [kubectl_manifest.namespace]
}
