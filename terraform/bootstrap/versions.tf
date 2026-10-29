# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

terraform {
  # >= 1.12: same minimum as cluster/ (one CLI for both stacks); the S3
  # backend `use_lockfile` (backend.tf.example) needs 1.10. Latest tested: 1.17.0.
  required_version = ">= 1.12"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 6.0.0, < 7.0.0"
    }
  }
}
