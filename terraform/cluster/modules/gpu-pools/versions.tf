# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

terraform {
  # >= 1.12: `||` short-circuit (1.12) and cross-variable validations (1.9)
  # in variables.tf.
  required_version = ">= 1.12"
}
