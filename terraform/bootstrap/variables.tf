# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

################################################################################
# Variables (same naming inputs as ../cluster: keep project_name / cluster_name
# identical in both stacks so the cluster finds the buckets by name)
################################################################################

variable "aws_region" {
  description = "AWS region of the buckets (the region of ../cluster)"
  type        = string

  validation {
    condition     = can(regex("^[a-z]{2}(-[a-z]+)+-[0-9]+$", var.aws_region))
    error_message = "aws_region must be a region code such as us-east-1 or us-east-2."
  }
}

variable "project_name" {
  description = "Project name: Project tag and default cluster name prefix (same value as ../cluster)"
  type        = string
  default     = "dynamo-sample"

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{1,30}[a-z0-9]$", var.project_name))
    error_message = "project_name must be 3-32 lowercase letters, digits or hyphens, starting with a letter."
  }
}

variable "cluster_name" {
  description = "EKS cluster name <prefix>-<suffix> of ../cluster; null = <project_name>-<region code of the AZ IDs>. The buckets are <prefix>-{models,results}-<account>-<suffix>"
  type        = string
  default     = null

  validation {
    condition     = var.cluster_name == null ? true : can(regex("^[a-z][a-z0-9-]{0,34}[a-z0-9]-[a-z0-9]+$", var.cluster_name)) && length(var.cluster_name) <= 38
    error_message = "cluster_name must be <= 38 lowercase letters, digits or hyphens, start with a letter and contain at least one hyphen (<prefix>-<suffix>)."
  }
}

variable "model_bucket_name" {
  description = "Model weights bucket name; null = <prefix>-models-<account>-<suffix> of cluster_name"
  type        = string
  default     = null
}

variable "results_bucket_name" {
  description = "Benchmark results bucket name; null = <prefix>-results-<account>-<suffix> of cluster_name"
  type        = string
  default     = null
}

variable "tags" {
  description = "Extra tags merged into the default tags"
  type        = map(string)
  default     = {}

  # The character class of the IAM Tag API; KMS accepts the same set. Same condition as ../cluster.
  validation {
    condition     = alltrue([for k, v in var.tags : can(regex("^[\\p{L}\\p{Z}\\p{N}_.:/=+@-]*$", k)) && can(regex("^[\\p{L}\\p{Z}\\p{N}_.:/=+@-]*$", v))])
    error_message = "tags keys and values may only use letters, digits, spaces and _ . : / = + - @ (KMS and IAM reject any other character at apply)."
  }
}
