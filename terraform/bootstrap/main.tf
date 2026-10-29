# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

################################################################################
# Bootstrap: the S3 buckets that outlive the cluster
#
#   models   weights staged from Hugging Face (s3://<bucket>/<Org>-<Name>/ +
#            a `.done` object written last); read by the model-prefetch Job
#   results  benchmark results (versioned, never expired), written by the
#            results-writer Pod Identity role of ../cluster (no Delete*)
#
# ../cluster references both by name only, with the same naming rule as here.
# Apply this stack first and destroy it last.
################################################################################

provider "aws" {
  region = var.aws_region

  default_tags {
    tags = local.tags
  }
}

data "aws_caller_identity" "current" {}

data "aws_availability_zones" "available" {
  filter {
    name   = "opt-in-status"
    values = ["opt-in-not-required"]
  }
}

locals {
  account_id = data.aws_caller_identity.current.account_id

  # Region code of the AZ IDs (use2-az1 -> use2), as in ../cluster.
  region_code = split("-", sort(data.aws_availability_zones.available.zone_ids)[0])[0]

  cluster_name        = coalesce(var.cluster_name, "${var.project_name}-${local.region_code}")
  cluster_name_prefix = regex("^(.*)-([^-]+)$", local.cluster_name)[0]
  cluster_name_suffix = regex("^(.*)-([^-]+)$", local.cluster_name)[1]

  model_bucket_name   = coalesce(var.model_bucket_name, "${local.cluster_name_prefix}-models-${local.account_id}-${local.cluster_name_suffix}")
  results_bucket_name = coalesce(var.results_bucket_name, "${local.cluster_name_prefix}-results-${local.account_id}-${local.cluster_name_suffix}")

  tags = merge({
    Project     = var.project_name
    Environment = "demo"
    Region      = var.aws_region
    Stack       = "bootstrap"
    ManagedBy   = "terraform"
  }, var.tags)
}
