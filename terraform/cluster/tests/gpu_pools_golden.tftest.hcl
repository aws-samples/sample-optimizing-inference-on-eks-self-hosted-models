# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
#
# RED test: golden equivalence of the p6-b200 + capacity-block render with the LIVE objects of the tested cluster.
#
# fixtures/live-gpu-b200-cb.json was captured read-only from the running cluster by
# fixtures/capture_live_gpu_pool.py (kubectl get nodeclass,nodepool gpu-b200-cb -o json), keeping only `spec`,
# with account identifiers and the zone replaced by the placeholders used below. The ONLY fields removed are the
# CRD-server-defaulted ones listed in `excluded_crd_defaults` (asserted verbatim below so the list cannot be
# widened to make a failing run pass; that assertion lives in tests/terraform/test_cluster_static.py because a
# tftest assert must reference a configuration object). The render is compared with jsonencode(), which sorts object keys
# but keeps list order AND value types: a number rendered as the string "0" (Terraform unifies the
# element types of a list of objects with different attribute sets into map(string)) fails here. The
# comparison also keeps list order: the order of interfaces, requirements and values must be identical to the live one.

variables {
  capacity_type           = "capacity-block"
  vpc_az_ids              = ["use2-az1", "use2-az2", "use2-az3"]
  az_name_by_id           = { use2-az1 = "us-east-2c", use2-az2 = "us-east-2a", use2-az3 = "us-east-2b" }
  private_subnet_by_az_id = { use2-az1 = "subnet-PLACEHOLDER", use2-az2 = "subnet-other2", use2-az3 = "subnet-other3" }
  node_role_name          = "node-role-PLACEHOLDER"
  security_group_id       = "sg-PLACEHOLDER"
  project_name            = "dynamo-sample"
}

run "p6_b200_capacity_block_matches_live_objects" {
  command = plan
  module {
    source = "./modules/gpu-pools"
  }
  variables {
    instance_types  = { for k, v in jsondecode(file("tests/fixtures/instance-types.json")) : k => v if contains(["p6-b200.48xlarge"], k) }
    reservation_ids = ["cr-00000000000000000"]
    reservations    = { "cr-00000000000000000" = { instance_type = "p6-b200.48xlarge", instance_count = 1, availability_zone_id = "use2-az1", state = "active" } }
  }

  assert {
    condition     = output.node_classes["gpu-b200-cb"].apiVersion == "eks.amazonaws.com/v1" && output.node_classes["gpu-b200-cb"].kind == "NodeClass" && output.node_classes["gpu-b200-cb"].metadata.name == "gpu-b200-cb"
    error_message = "NodeClass header differs from the live object"
  }
  assert {
    condition     = output.node_pools["gpu-b200-cb"].apiVersion == "karpenter.sh/v1" && output.node_pools["gpu-b200-cb"].kind == "NodePool" && output.node_pools["gpu-b200-cb"].metadata.name == "gpu-b200-cb"
    error_message = "NodePool header differs from the live object"
  }
  assert {
    condition     = jsonencode(output.node_classes["gpu-b200-cb"].spec) == jsonencode(jsondecode(file("tests/fixtures/live-gpu-b200-cb.json")).nodeclass_spec)
    error_message = "NodeClass spec differs from the live gpu-b200-cb (after removing only the fixed CRD defaults)"
  }
  assert {
    condition     = jsonencode(output.node_pools["gpu-b200-cb"].spec) == jsonencode(jsondecode(file("tests/fixtures/live-gpu-b200-cb.json")).nodepool_spec)
    error_message = "NodePool spec differs from the live gpu-b200-cb"
  }
}
