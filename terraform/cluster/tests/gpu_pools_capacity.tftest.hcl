# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
#
# RED tests for the gpu-pools module: capacity types, limits and AZ choice.
# Contract and inputs: see the header of gpu_pools_grouping.tftest.hcl.
#
# capacity-block / odcr : requirement karpenter.sh/capacity-type In [reserved], NodeClass
#                         capacityReservationSelectorTerms = sorted reservation ids, expireAfter 480h,
#                         disruption WhenEmpty + consolidateAfter Never + budgets [{nodes = "0"}]
# on-demand / spot      : NO capacityReservationSelectorTerms key, capacity-type In [on-demand] / [spot],
#                         expireAfter 168h, WhenEmpty + consolidate_after (15m) + budgets [{nodes = "1"}]
# limits                : nvidia.com/gpu = gpus_per_node x max_nodes ; cpu = vcpu_max x max_nodes
#   max_nodes           : capacity-block: gpu_max_nodes if set, else the sum of instance_count of the
#                         reservations (state active|scheduled|pending) whose instance_type is in THAT group,
#                         else 1 (state-tolerant: an expired block must not break the plan);
#                         odcr: gpu_max_nodes (required); on-demand/spot: gpu_max_nodes, else 1
# AZ                    : capacity-block: the AZ IDs of the (live) reservations; odcr: gpu_az_ids (required);
#                         on-demand/spot: gpu_az_ids, else ONE AZ = first (sorted) AZ ID offering ALL types and
#                         present in vpc_az_ids

variables {
  capacity_type           = "capacity-block"
  vpc_az_ids              = ["use2-az1", "use2-az2", "use2-az3"]
  az_name_by_id           = { use2-az1 = "us-east-2c", use2-az2 = "us-east-2a", use2-az3 = "us-east-2b" }
  private_subnet_by_az_id = { use2-az1 = "subnet-az1", use2-az2 = "subnet-az2", use2-az3 = "subnet-az3" }
  node_role_name          = "node-role-PLACEHOLDER"
  security_group_id       = "sg-PLACEHOLDER"
  project_name            = "dynamo-sample"
}

run "capacity_block_reserved_semantics" {
  command = plan
  module {
    source = "./modules/gpu-pools"
  }
  variables {
    instance_types  = { for k, v in jsondecode(file("tests/fixtures/instance-types.json")) : k => v if contains(["p6-b200.48xlarge"], k) }
    reservation_ids = ["cr-00000000000000001"]
    reservations    = { "cr-00000000000000001" = { instance_type = "p6-b200.48xlarge", instance_count = 1, availability_zone_id = "use2-az2", state = "active" } }
  }

  assert {
    condition     = jsonencode(keys(output.node_pools)) == jsonencode(["gpu-b200-cb"])
    error_message = "p6-b200 + capacity-block keeps the live pool name gpu-b200-cb"
  }
  assert {
    condition     = jsonencode([for r in output.node_pools["gpu-b200-cb"].spec.template.spec.requirements : r.values if r.key == "karpenter.sh/capacity-type"][0]) == jsonencode(["reserved"])
    error_message = "capacity-block pools require capacity-type reserved"
  }
  assert {
    condition     = jsonencode(output.node_classes["gpu-b200-cb"].spec.capacityReservationSelectorTerms) == jsonencode([{ id = "cr-00000000000000001" }])
    error_message = "NodeClass must select the reservation"
  }
  assert {
    condition     = output.node_pools["gpu-b200-cb"].spec.template.spec.expireAfter == "480h" && jsonencode(output.node_pools["gpu-b200-cb"].spec.disruption) == jsonencode({ consolidationPolicy = "WhenEmpty", consolidateAfter = "Never", budgets = [{ nodes = "0" }] })
    error_message = "reserved pools: expireAfter 480h and fully off disruption"
  }
  assert {
    condition     = jsonencode(output.az_ids) == jsonencode(["use2-az2"]) && jsonencode(output.azs) == jsonencode(["us-east-2a"]) && jsonencode(output.node_classes["gpu-b200-cb"].spec.subnetSelectorTerms) == jsonencode([{ id = "subnet-az2" }])
    error_message = "capacity-block AZ comes from the reservation (no offerings needed) and selects that AZ's private subnet"
  }
  assert {
    condition     = jsonencode(output.node_pools["gpu-b200-cb"].spec.limits) == jsonencode({ "nvidia.com/gpu" = 8, cpu = 192 }) && output.pools["gpu-b200-cb"].max_nodes == 1
    error_message = "limits = 8 GPU / 192 vCPU for one reserved p6-b200"
  }
}

run "odcr_reserved_with_explicit_az_and_max_nodes" {
  command = plan
  module {
    source = "./modules/gpu-pools"
  }
  variables {
    capacity_type   = "odcr"
    instance_types  = { for k, v in jsondecode(file("tests/fixtures/instance-types.json")) : k => v if contains(["p6-b200.48xlarge"], k) }
    reservation_ids = ["cr-00000000000000002", "cr-00000000000000001"]
    gpu_az_ids      = ["use2-az3"]
    gpu_max_nodes   = 2
  }

  assert {
    condition     = jsonencode(keys(output.node_pools)) == jsonencode(["gpu-b200-odcr"])
    error_message = "odcr pool name is gpu-b200-odcr"
  }
  assert {
    condition     = jsonencode([for r in output.node_pools["gpu-b200-odcr"].spec.template.spec.requirements : r.values if r.key == "karpenter.sh/capacity-type"][0]) == jsonencode(["reserved"])
    error_message = "odcr pools also require capacity-type reserved"
  }
  assert {
    condition     = jsonencode(output.node_classes["gpu-b200-odcr"].spec.capacityReservationSelectorTerms) == jsonencode([{ id = "cr-00000000000000001" }, { id = "cr-00000000000000002" }])
    error_message = "selector terms must be sorted (deterministic render regardless of input order)"
  }
  assert {
    condition     = jsonencode(output.node_pools["gpu-b200-odcr"].spec.limits) == jsonencode({ "nvidia.com/gpu" = 16, cpu = 384 })
    error_message = "gpu_max_nodes = 2 -> 16 GPU / 384 vCPU"
  }
  assert {
    condition     = jsonencode(output.azs) == jsonencode(["us-east-2b"])
    error_message = "odcr uses gpu_az_ids"
  }
}

run "on_demand_has_no_reservation_terms" {
  command = plan
  module {
    source = "./modules/gpu-pools"
  }
  variables {
    capacity_type  = "on-demand"
    instance_types = { for k, v in jsondecode(file("tests/fixtures/instance-types.json")) : k => v if contains(["p6-b200.48xlarge"], k) }
    offerings      = { "p6-b200.48xlarge" = ["use2-az1"] }
  }

  assert {
    condition     = !contains(keys(output.node_classes["gpu-b200-od"].spec), "capacityReservationSelectorTerms")
    error_message = "on-demand NodeClass must not carry capacityReservationSelectorTerms (not even empty)"
  }
  assert {
    condition     = jsonencode([for r in output.node_pools["gpu-b200-od"].spec.template.spec.requirements : r.values if r.key == "karpenter.sh/capacity-type"][0]) == jsonencode(["on-demand"])
    error_message = "on-demand pool requires capacity-type on-demand"
  }
  assert {
    condition     = output.node_pools["gpu-b200-od"].spec.template.spec.expireAfter == "168h" && jsonencode(output.node_pools["gpu-b200-od"].spec.disruption) == jsonencode({ consolidationPolicy = "WhenEmpty", consolidateAfter = "15m", budgets = [{ nodes = "1" }] })
    error_message = "on-demand: expireAfter 168h, WhenEmpty after 15m, budget 1"
  }
  assert {
    condition     = output.pools["gpu-b200-od"].max_nodes == 1 && jsonencode(output.node_pools["gpu-b200-od"].spec.limits) == jsonencode({ "nvidia.com/gpu" = 8, cpu = 192 })
    error_message = "on-demand without gpu_max_nodes defaults to 1 node (and the pool reports it so it can be surfaced)"
  }
}

run "spot_requirement" {
  command = plan
  module {
    source = "./modules/gpu-pools"
  }
  variables {
    capacity_type     = "spot"
    consolidate_after = "5m"
    expire_after      = "100h"
    instance_types    = { for k, v in jsondecode(file("tests/fixtures/instance-types.json")) : k => v if contains(["g6e.12xlarge"], k) }
    offerings         = { "g6e.12xlarge" = ["use2-az1"] }
    gpu_max_nodes     = 3
  }

  assert {
    condition     = jsonencode(keys(output.node_pools)) == jsonencode(["gpu-l40s-spot"])
    error_message = "single-group spot pool is named gpu-l40s-spot"
  }
  assert {
    condition     = jsonencode([for r in output.node_pools["gpu-l40s-spot"].spec.template.spec.requirements : r.values if r.key == "karpenter.sh/capacity-type"][0]) == jsonencode(["spot"])
    error_message = "spot pool requires capacity-type spot"
  }
  assert {
    condition     = !contains(keys(output.node_classes["gpu-l40s-spot"].spec), "capacityReservationSelectorTerms")
    error_message = "spot has no reservation terms"
  }
  assert {
    condition     = output.node_pools["gpu-l40s-spot"].spec.template.spec.expireAfter == "100h" && output.node_pools["gpu-l40s-spot"].spec.disruption.consolidateAfter == "5m"
    error_message = "expire_after and consolidate_after overrides must be honoured"
  }
  assert {
    condition     = jsonencode(output.node_pools["gpu-l40s-spot"].spec.limits) == jsonencode({ "nvidia.com/gpu" = 12, cpu = 144 })
    error_message = "gpu_max_nodes = 3 of 4 GPU / 48 vCPU nodes"
  }
}

# An expired block must not break the plan (every plan, including destroy, would otherwise fail).
run "expired_capacity_block_is_tolerated" {
  command = plan
  module {
    source = "./modules/gpu-pools"
  }
  variables {
    instance_types  = { for k, v in jsondecode(file("tests/fixtures/instance-types.json")) : k => v if contains(["p6-b200.48xlarge"], k) }
    reservation_ids = ["cr-00000000000000001"]
    reservations    = { "cr-00000000000000001" = { instance_type = "p6-b200.48xlarge", instance_count = 1, availability_zone_id = "use2-az1", state = "expired" } }
    gpu_az_ids      = ["use2-az1"]
  }

  assert {
    condition     = output.pools["gpu-b200-cb"].max_nodes == 1 && jsonencode(output.node_pools["gpu-b200-cb"].spec.limits) == jsonencode({ "nvidia.com/gpu" = 8, cpu = 192 })
    error_message = "expired block: fall back to 1 node instead of failing"
  }
}

run "expired_capacity_block_with_max_nodes_override" {
  command = plan
  module {
    source = "./modules/gpu-pools"
  }
  variables {
    instance_types  = { for k, v in jsondecode(file("tests/fixtures/instance-types.json")) : k => v if contains(["p6-b200.48xlarge"], k) }
    reservation_ids = ["cr-00000000000000001"]
    reservations    = { "cr-00000000000000001" = { instance_type = "p6-b200.48xlarge", instance_count = 1, availability_zone_id = "use2-az1", state = "cancelled" } }
    gpu_az_ids      = ["use2-az1"]
    gpu_max_nodes   = 3
  }

  assert {
    condition     = jsonencode(output.node_pools["gpu-b200-cb"].spec.limits) == jsonencode({ "nvidia.com/gpu" = 24, cpu = 576 })
    error_message = "gpu_max_nodes overrides the derivation for capacity-block"
  }
}

# Two blocks of different instance types: each pool counts only the reservations of ITS types.
run "capacity_block_limits_count_only_matching_instance_type" {
  command = plan
  module {
    source = "./modules/gpu-pools"
  }
  variables {
    instance_types  = { for k, v in jsondecode(file("tests/fixtures/instance-types.json")) : k => v if contains(["p6-b200.48xlarge", "p5.48xlarge"], k) }
    reservation_ids = ["cr-00000000000000001", "cr-00000000000000002"]
    reservations = {
      "cr-00000000000000001" = { instance_type = "p6-b200.48xlarge", instance_count = 1, availability_zone_id = "use2-az1", state = "active" }
      "cr-00000000000000002" = { instance_type = "p5.48xlarge", instance_count = 2, availability_zone_id = "use2-az1", state = "scheduled" }
    }
  }

  assert {
    condition     = jsonencode(output.node_pools["gpu-b200-cb"].spec.limits) == jsonencode({ "nvidia.com/gpu" = 8, cpu = 192 })
    error_message = "b200 pool must count only the b200 block (1 node)"
  }
  assert {
    condition     = jsonencode(output.node_pools["gpu-h100-cb"].spec.limits) == jsonencode({ "nvidia.com/gpu" = 16, cpu = 384 })
    error_message = "p5 pool must count only the p5 block (2 nodes)"
  }
}

# --- AZ auto-discovery (on-demand / spot) ---

run "az_auto_picks_one_az_from_the_intersection" {
  command = plan
  module {
    source = "./modules/gpu-pools"
  }
  variables {
    capacity_type  = "on-demand"
    instance_types = { for k, v in jsondecode(file("tests/fixtures/instance-types.json")) : k => v if contains(["p6-b200.48xlarge", "g6e.12xlarge"], k) }
    offerings = {
      "p6-b200.48xlarge" = ["use2-az2", "use2-az1"]
      "g6e.12xlarge"     = ["use2-az1", "use2-az3", "use2-az2"]
    }
  }

  assert {
    condition     = jsonencode(output.az_ids) == jsonencode(["use2-az1"]) && jsonencode(output.azs) == jsonencode(["us-east-2c"])
    error_message = "intersection {az1, az2}: exactly ONE AZ, the first sorted (use2-az1)"
  }
  assert {
    condition     = jsonencode([for r in output.node_pools["gpu-b200-od"].spec.template.spec.requirements : r.values if r.key == "topology.kubernetes.io/zone"][0]) == jsonencode(["us-east-2c"])
    error_message = "zone requirement uses the AZ name"
  }
}

run "az_auto_ignores_azs_outside_the_vpc" {
  command = plan
  module {
    source = "./modules/gpu-pools"
  }
  variables {
    capacity_type  = "on-demand"
    instance_types = { for k, v in jsondecode(file("tests/fixtures/instance-types.json")) : k => v if contains(["p6-b200.48xlarge"], k) }
    offerings      = { "p6-b200.48xlarge" = ["use2-az0", "use2-az2"] }
  }

  assert {
    condition     = jsonencode(output.az_ids) == jsonencode(["use2-az2"])
    error_message = "an offered AZ that is not a VPC AZ has no private subnet and must be skipped"
  }
}

run "az_auto_empty_intersection_fails" {
  command = plan
  module {
    source = "./modules/gpu-pools"
  }
  variables {
    capacity_type  = "on-demand"
    instance_types = { for k, v in jsondecode(file("tests/fixtures/instance-types.json")) : k => v if contains(["p6-b200.48xlarge", "g6e.12xlarge"], k) }
    offerings = {
      "p6-b200.48xlarge" = ["use2-az1"]
      "g6e.12xlarge"     = ["use2-az3"]
    }
  }

  expect_failures = [var.offerings]
}

run "explicit_gpu_az_ids_allow_multi_az" {
  command = plan
  module {
    source = "./modules/gpu-pools"
  }
  variables {
    capacity_type  = "on-demand"
    instance_types = { for k, v in jsondecode(file("tests/fixtures/instance-types.json")) : k => v if contains(["p6-b200.48xlarge"], k) }
    offerings      = { "p6-b200.48xlarge" = ["use2-az1"] }
    gpu_az_ids     = ["use2-az2", "use2-az1"]
  }

  assert {
    condition     = jsonencode(output.az_ids) == jsonencode(["use2-az1", "use2-az2"]) && jsonencode(output.node_classes["gpu-b200-od"].spec.subnetSelectorTerms) == jsonencode([{ id = "subnet-az1" }, { id = "subnet-az2" }])
    error_message = "explicit AZs are sorted and each gets its private subnet"
  }
}

run "gpu_az_outside_vpc_fails" {
  command = plan
  module {
    source = "./modules/gpu-pools"
  }
  variables {
    capacity_type  = "on-demand"
    instance_types = { for k, v in jsondecode(file("tests/fixtures/instance-types.json")) : k => v if contains(["p6-b200.48xlarge"], k) }
    offerings      = { "p6-b200.48xlarge" = ["use2-az1"] }
    gpu_az_ids     = ["use2-az9"]
  }

  expect_failures = [var.gpu_az_ids]
}

run "capacity_block_az_must_match_the_reservation" {
  command = plan
  module {
    source = "./modules/gpu-pools"
  }
  variables {
    instance_types  = { for k, v in jsondecode(file("tests/fixtures/instance-types.json")) : k => v if contains(["p6-b200.48xlarge"], k) }
    reservation_ids = ["cr-00000000000000001"]
    reservations    = { "cr-00000000000000001" = { instance_type = "p6-b200.48xlarge", instance_count = 1, availability_zone_id = "use2-az1", state = "active" } }
    gpu_az_ids      = ["use2-az2"]
  }

  expect_failures = [var.gpu_az_ids]
}
