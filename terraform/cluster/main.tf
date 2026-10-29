# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

################################################################################
# EKS Auto Mode cluster for the NVIDIA Dynamo sample, one parametrized root for
# any region and any P/G GPU instance type (terraform.tfvars.example).
#
# Layout:
#   vpc.tf / vpc-endpoints.tf  VPC (every AZ, single NAT) + S3 gateway endpoint
#   eks.tf                     EKS Auto Mode (built-in `system` pool only)
#   nodepool.tf                cpu-demo NodeClass/NodePool + auto-ebs-sc
#   gpu-discovery.tf           EC2 facts of the GPU types -> modules/gpu-pools
#   gpu-nodepool.tf            one GPU NodeClass/NodePool per group of types
#   namespaces.tf              namespaces with explicit PSA labels
#   iam-*.tf                   Pod Identity roles (model reader, results
#                              writer, image mirror)
#   ecr.tf                     project ECR repositories
#   observability.tf           kube-prometheus-stack, dcgm-exporter, dashboards
#   dynamo-platform.tf         dynamo-platform (operator only)
#   grafana-edge.tf            optional CloudFront + WAF edge for Grafana
#   outputs.tf                 outputs, incl. the env maps of scripts/gen_env.py
#   moved.tf                   address moves of an earlier layout of this stack
################################################################################

data "aws_caller_identity" "current" {}

data "aws_partition" "current" {}

locals {
  account_id = data.aws_caller_identity.current.account_id

  # Region code of the AZ IDs (use2-az1 -> use2, use1-az4 -> use1).
  region_code = split("-", local.vpc_az_ids[0])[0]

  cluster_name = coalesce(var.cluster_name, "${var.project_name}-${local.region_code}")

  # Bucket names follow <prefix>-{models,results}-<account>-<suffix> for the
  # cluster <prefix>-<suffix>: scripts/lib/common.sh derives the Track Q
  # cluster from MODEL_BUCKET. Same rule as ../bootstrap (owner of the buckets).
  cluster_name_prefix = regex("^(.*)-([^-]+)$", local.cluster_name)[0]
  cluster_name_suffix = regex("^(.*)-([^-]+)$", local.cluster_name)[1]
  model_bucket_name   = coalesce(var.model_bucket_name, "${local.cluster_name_prefix}-models-${local.account_id}-${local.cluster_name_suffix}")
  results_bucket_name = coalesce(var.results_bucket_name, "${local.cluster_name_prefix}-results-${local.account_id}-${local.cluster_name_suffix}")

  tags = merge({
    Project     = var.project_name
    Environment = "demo"
    Region      = var.aws_region
    Stack       = "cluster"
    ManagedBy   = "terraform"
  }, var.tags)

  # The buckets are created by ../bootstrap; referenced by name/ARN string so
  # this stack plans even before they exist.
  model_bucket_arn   = "arn:${data.aws_partition.current.partition}:s3:::${local.model_bucket_name}"
  results_bucket_arn = "arn:${data.aws_partition.current.partition}:s3:::${local.results_bucket_name}"

  ecr_registry = "${local.account_id}.dkr.ecr.${var.aws_region}.${data.aws_partition.current.dns_suffix}"

  # In-cluster Prometheus (kube-prometheus-stack release `prometheus`).
  prometheus_endpoint = "http://prometheus-kube-prometheus-prometheus.monitoring.svc.cluster.local:9090"
}
