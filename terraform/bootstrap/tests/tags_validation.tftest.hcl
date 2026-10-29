# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
#
# RED tests: the validation of var.tags (terraform/bootstrap/variables.tf). The extra tags are merged into the
# default_tags of the provider, and KMS and IAM reject a tag key or value with a character outside letters, digits,
# spaces and _ . : / = + - @ (TagException: Invalid tag at apply; the mock providers never check it). The plan must
# refuse them first. ../cluster uses the same condition (tests/terraform/test_tag_charset_static.py), so the safe map
# below, which plans here, covers both stacks. Providers are mocked: nothing touches AWS.

mock_provider "aws" {}

override_data {
  target = data.aws_caller_identity.current
  values = {
    account_id = "123456789012"
  }
}

override_data {
  target = data.aws_availability_zones.available
  values = {
    zone_ids = ["use2-az1", "use2-az2", "use2-az3"]
  }
}

variables {
  aws_region = "us-east-2"
}

run "safe_tags_plan" {
  command = plan
  variables {
    tags = {
      "cost-center"            = "ML platform 42"
      "team:owner"             = "a_b.c:d/e=f+g-h@i"
      "kubernetes.io/role/elb" = "1"
    }
  }
}

run "tag_value_with_a_star_rejected" {
  command = plan
  variables {
    tags = { Purpose = "dynamo-sample/*" }
  }
  expect_failures = [var.tags]
}

run "tag_key_with_a_comma_rejected" {
  command = plan
  variables {
    tags = { "cost,center" = "ml" }
  }
  expect_failures = [var.tags]
}
