# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

terraform {
  # >= 1.12: `||` / `&&` short-circuit (1.12), cross-variable validations
  # (1.9), `terraform test` with mock_provider (1.7) and the S3 backend
  # `use_lockfile` (1.10). Latest tested: 1.17.0.
  required_version = ">= 1.12"

  required_providers {
    # >= 6.53.0: data.aws_ec2_capacity_block_reservation.
    aws = {
      source  = "hashicorp/aws"
      version = ">= 6.53.0, < 7.0.0"
    }
    helm = {
      source  = "hashicorp/helm"
      version = "2.17.0"
    }
    kubectl = {
      source  = "alekc/kubectl"
      version = "2.1.3"
    }
    kubernetes = {
      source  = "hashicorp/kubernetes"
      version = "2.35.1"
    }
    # Required by the EKS module (OIDC thumbprint lookup), pinned here so the
    # lock file records it explicitly.
    tls = {
      source  = "hashicorp/tls"
      version = ">= 4.0.0"
    }
  }
}
