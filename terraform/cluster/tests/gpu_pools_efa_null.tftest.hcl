# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
#
# Regression: the aws_ec2_instance_type data source returns efa_maximum_interfaces = null (not 0) when
# efa_supported = false (g5.xlarge, g6e.xlarge, ...). Terraform < 1.12 does not short-circuit || in the
# modules/gpu-pools EFA-layout validation, so `null == 0` / `null + 1` failed the plan with
# "argument must not be null". The fixtures (instance-types.json) carry the real null for those types.

variables {
  capacity_type           = "on-demand"
  vpc_az_ids              = ["use2-az1", "use2-az2", "use2-az3"]
  az_name_by_id           = { use2-az1 = "us-east-2c", use2-az2 = "us-east-2a", use2-az3 = "us-east-2b" }
  private_subnet_by_az_id = { use2-az1 = "subnet-az1", use2-az2 = "subnet-az2", use2-az3 = "subnet-az3" }
  node_role_name          = "node-role-PLACEHOLDER"
  security_group_id       = "sg-PLACEHOLDER"
  project_name            = "dynamo-sample"
}

run "non_efa_only_g6e_plans" {
  command = plan
  module {
    source = "./modules/gpu-pools"
  }
  variables {
    instance_types = { for k, v in jsondecode(file("tests/fixtures/instance-types.json")) : k => v if contains(["g6e.xlarge"], k) }
    offerings      = { "g6e.xlarge" = ["use2-az1"] }
  }
  assert {
    condition     = length(output.pools) == 1 && one(values(output.pools)).efa_interfaces == 0
    error_message = "g6e.xlarge (efa_maximum_interfaces = null) must plan as one pool with 0 EFA interfaces"
  }
}

run "non_efa_only_g5_plans" {
  command = plan
  module {
    source = "./modules/gpu-pools"
  }
  variables {
    instance_types = { for k, v in jsondecode(file("tests/fixtures/instance-types.json")) : k => v if contains(["g5.xlarge"], k) }
    offerings      = { "g5.xlarge" = ["use2-az1"] }
  }
  assert {
    condition     = length(output.pools) == 1 && one(values(output.pools)).efa_interfaces == 0
    error_message = "g5.xlarge (efa_maximum_interfaces = null) must plan as one pool with 0 EFA interfaces"
  }
}

run "mixed_efa_and_non_efa_plans" {
  command = plan
  module {
    source = "./modules/gpu-pools"
  }
  variables {
    instance_types = { for k, v in jsondecode(file("tests/fixtures/instance-types.json")) : k => v if contains(["p6-b200.48xlarge", "g6e.xlarge", "g5.xlarge"], k) }
    offerings = {
      "p6-b200.48xlarge" = ["use2-az1"]
      "g6e.xlarge"       = ["use2-az1"]
      "g5.xlarge"        = ["use2-az1"]
    }
  }
  assert {
    condition     = length(output.pools) == 3 && output.pools["gpu-b200-od"].efa_interfaces == 8 && length([for n, p in output.pools : n if p.efa_interfaces == 0]) == 2
    error_message = "mixed list: b200 keeps 8 EFA, the two non-EFA types get 0 (null must not poison the validation)"
  }
}

run "non_efa_with_efa_off_plans" {
  command = plan
  module {
    source = "./modules/gpu-pools"
  }
  variables {
    efa            = "off"
    instance_types = { for k, v in jsondecode(file("tests/fixtures/instance-types.json")) : k => v if contains(["g6e.xlarge"], k) }
    offerings      = { "g6e.xlarge" = ["use2-az1"] }
  }
  assert {
    condition     = one(values(output.pools)).efa_interfaces == 0
    error_message = "efa = off with a null efa_maximum_interfaces must plan with 0 EFA interfaces"
  }
}

# The coalesce fix must not weaken the layout rule for EFA types (SYNTHETIC: 2 EFA interfaces, 8 cards).
run "efa_type_with_too_many_cards_still_rejected" {
  command = plan
  module {
    source = "./modules/gpu-pools"
  }
  variables {
    instance_types = {
      "p6-b200.48xlarge" = merge(jsondecode(file("tests/fixtures/instance-types.json"))["p6-b200.48xlarge"], { efa_maximum_interfaces = 2 })
    }
    offerings = { "p6-b200.48xlarge" = ["use2-az1"] }
  }
  expect_failures = [var.instance_types]
}
