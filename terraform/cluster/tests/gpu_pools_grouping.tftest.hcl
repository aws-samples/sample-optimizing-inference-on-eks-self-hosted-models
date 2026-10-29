# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
#
# RED tests for the gpu-pools module: grouping of GPU instance types by identical GPU/network layout.
#
# CONTRACT of terraform/cluster/modules/gpu-pools (pure module: plain input variables, NO data sources, NO
# resources; the root module wires data.aws_ec2_instance_type / offerings / capacity-block reservations in).
# Tests use plain variables and `command = plan` (no mock_provider) and the REAL EC2 facts captured by
# fixtures/capture_instance_types.py (fixtures/instance-types.json, read-only describe-instance-types).
#
# Inputs (see gpu_pools_validation.tftest.hcl for the rules, each enforced as a VARIABLE validation so that
# expect_failures can name it):
#   instance_types          map(object({maximum_network_cards, efa_supported, efa_maximum_interfaces,
#                             default_vcpus, memory_size (MiB), total_instance_storage (GB),
#                             supported_architectures, gpus = list(object({name, count, memory_size (MiB),
#                             manufacturer}))}))     same attribute names as the aws_ec2_instance_type data source
#   capacity_type           "capacity-block" | "odcr" | "on-demand" | "spot"
#   reservation_ids         list(string)   cr-... ids already bought by the user (Terraform never buys)
#   reservations            map(object({instance_type, instance_count, availability_zone_id, state}))  by cr id
#                           (data.aws_ec2_capacity_block_reservation; {} for odcr/on-demand/spot)
#   offerings               map(list(string))  instance type -> AZ IDs offering it
#   gpu_az_ids              list(string)   [] = auto
#   gpu_max_nodes           number         null = derived
#   vpc_az_ids, az_name_by_id, private_subnet_by_az_id, node_role_name, security_group_id, project_name
#   pod_ipv4_prefix_count (4), ephemeral_storage_size ("256Gi"), efa ("auto"|"off"), expire_after (null),
#   consolidate_after ("15m"), product_overrides (map(string), {})
# Outputs:
#   node_classes / node_pools   map(pool name => full manifest object {apiVersion, kind, metadata, spec})
#   pools                       map(pool name => {instance_types (sorted), gpu_name, gpu_product, gpus_per_node,
#                                 gpu_memory_mib, network_cards, efa_interfaces, max_nodes, vcpu_max,
#                                 host_memory_gib (min), ephemeral_storage_min_gib (min), ephemeral_backing
#                                 ("nvme"|"ebs"), capacity_type, az_ids, azs})
#   az_ids / azs                chosen GPU AZ IDs / AZ names
# Group key = GPU name | GPU memory | GPUs per node | network cards | EFA interfaces. Pool name =
# gpu-<slug(gpu name)>-<cb|odcr|od|spot>; groups whose base name collides ALL get a distinguishing suffix.

variables {
  capacity_type           = "on-demand"
  vpc_az_ids              = ["use2-az1", "use2-az2", "use2-az3"]
  az_name_by_id           = { use2-az1 = "us-east-2c", use2-az2 = "us-east-2a", use2-az3 = "us-east-2b" }
  private_subnet_by_az_id = { use2-az1 = "subnet-az1", use2-az2 = "subnet-az2", use2-az3 = "subnet-az3" }
  node_role_name          = "node-role-PLACEHOLDER"
  security_group_id       = "sg-PLACEHOLDER"
  project_name            = "dynamo-sample"
}

run "single_b200_group" {
  command = plan
  module {
    source = "./modules/gpu-pools"
  }
  variables {
    instance_types = { for k, v in jsondecode(file("tests/fixtures/instance-types.json")) : k => v if contains(["p6-b200.48xlarge"], k) }
    offerings      = { "p6-b200.48xlarge" = ["use2-az1", "use2-az2"] }
  }

  assert {
    condition     = jsonencode(keys(output.pools)) == jsonencode(["gpu-b200-od"])
    error_message = "p6-b200 + on-demand must yield exactly one pool named gpu-b200-od"
  }
  assert {
    condition     = output.pools["gpu-b200-od"].gpus_per_node == 8 && output.pools["gpu-b200-od"].efa_interfaces == 8 && output.pools["gpu-b200-od"].network_cards == 8
    error_message = "b200 facts must come from the instance type (8 GPUs, 8 EFA, 8 cards)"
  }
  assert {
    condition     = output.pools["gpu-b200-od"].gpu_product == "NVIDIA-B200"
    error_message = "gpu.product must be derived as NVIDIA-<GPU NAME>"
  }
  assert {
    condition     = jsonencode(output.node_pools["gpu-b200-od"].spec.template.metadata.labels) == jsonencode({ "workload" = "dynamo-gpu", "nvidia.com/gpu.present" = "true", "nvidia.com/gpu.product" = "NVIDIA-B200" })
    error_message = "labels must be workload, nvidia.com/gpu.present and the derived nvidia.com/gpu.product"
  }
  assert {
    condition     = length(output.node_classes["gpu-b200-od"].spec.advancedNetworking.networkInterfaces) == 9
    error_message = "b200 static EFA layout is 1 ENA + 8 efa-only"
  }
}

# All GPU families that are valid targets (x86_64, NVIDIA). Real facts, 14 distinct layout groups expected:
# p4d(A100 40G) p4de(A100 80G) p5(H100) p5en(H200) p6-b200 p6-b300 g5.xlarge(A10G,no EFA) g5.12xlarge(A10G,1 EFA)
# g6.48xlarge(L4) g6e.{xlarge,2xlarge}(L40S,1 GPU,no EFA: ONE group) g6e.12xlarge g6e.48xlarge g7e.4xlarge g7e.48xlarge
run "all_families_group_by_layout" {
  command = plan
  module {
    source = "./modules/gpu-pools"
  }
  variables {
    instance_types = { for k, v in jsondecode(file("tests/fixtures/instance-types.json")) : k => v if !contains(["c7i.large", "g5g.xlarge"], k) }
    offerings      = { for k, v in jsondecode(file("tests/fixtures/instance-types.json")) : k => ["use2-az1"] if !contains(["c7i.large", "g5g.xlarge"], k) }
  }

  assert {
    condition     = length(output.pools) == 14
    error_message = "15 valid instance types must collapse into 14 pools (g6e.xlarge+g6e.2xlarge share a layout; p4d and p4de differ by GPU memory)"
  }
  assert {
    condition     = length(output.pools["gpu-l4-od"].instance_types) == 1 && output.pools["gpu-l4-od"].gpus_per_node == 8
    error_message = "a family with a single group keeps the plain base name (gpu-l4-od)"
  }
  assert {
    condition     = output.pools["gpu-h100-od"].gpu_product == "NVIDIA-H100" && output.pools["gpu-h200-od"].gpu_product == "NVIDIA-H200" && output.pools["gpu-b300-od"].efa_interfaces == 16
    error_message = "single-group families: derived products and b300 16 EFA / 17 cards"
  }
  assert {
    condition     = length([for n, p in output.pools : n if startswith(n, "gpu-l40s-od")]) == 3 && length([for n, p in output.pools : n if startswith(n, "gpu-a100-od")]) == 2 && length([for n, p in output.pools : n if startswith(n, "gpu-a10g-od")]) == 2 && length([for n, p in output.pools : n if startswith(n, "gpu-rtx-pro-server-6000-od")]) == 2
    error_message = "colliding groups (same GPU name, different layout/count/memory) must get distinct names that keep the gpu-<slug>-od prefix"
  }
  assert {
    condition     = jsonencode(sort(output.pools[one([for n, p in output.pools : n if startswith(n, "gpu-l40s-od") && p.efa_interfaces == 0])].instance_types)) == jsonencode(["g6e.2xlarge", "g6e.xlarge"])
    error_message = "g6e.xlarge and g6e.2xlarge (1 card, no EFA, 1 GPU) must be ONE group"
  }
  assert {
    condition     = length([for n, p in output.pools : n if startswith(n, "gpu-a100-od") && p.gpu_memory_mib == 40960]) == 1 && length([for n, p in output.pools : n if startswith(n, "gpu-a100-od") && p.gpu_memory_mib == 81920]) == 1
    error_message = "p4d (40 GB) and p4de (80 GB) must NOT be merged"
  }
}

# Invariants of the static network layout for EVERY family.
run "layout_invariants_every_family" {
  command = plan
  module {
    source = "./modules/gpu-pools"
  }
  variables {
    instance_types = { for k, v in jsondecode(file("tests/fixtures/instance-types.json")) : k => v if !contains(["c7i.large", "g5g.xlarge"], k) }
    offerings      = { for k, v in jsondecode(file("tests/fixtures/instance-types.json")) : k => ["use2-az1"] if !contains(["c7i.large", "g5g.xlarge"], k) }
  }

  assert {
    condition = alltrue([for n, p in output.pools :
    (p.efa_interfaces == 0) == !contains(keys(output.node_classes[n].spec), "advancedNetworking")])
    error_message = "types without EFA must get NO advancedNetworking; types with EFA must get it"
  }
  assert {
    condition = alltrue([for n, p in output.pools :
      length([for ni in try(output.node_classes[n].spec.advancedNetworking.networkInterfaces, []) : ni if ni.interfaceType == "efa-only"]) == p.efa_interfaces
    ])
    error_message = "number of efa-only entries must equal efa_maximum_interfaces"
  }
  assert {
    condition = alltrue([for n, p in output.pools :
      p.efa_interfaces == 0 || try(
        output.node_classes[n].spec.advancedNetworking.networkInterfaces[0].interfaceType == "interface" &&
        output.node_classes[n].spec.advancedNetworking.networkInterfaces[0].networkCardIndex == 0 &&
        output.node_classes[n].spec.advancedNetworking.networkInterfaces[0].deviceIndex == 0 &&
        output.node_classes[n].spec.advancedNetworking.networkInterfaces[0].secondaryIPv4PrefixCount == 4,
    false)])
    error_message = "entry 0 must be the primary ENA on card 0 / device 0 carrying secondaryIPv4PrefixCount (default 4)"
  }
  assert {
    condition = alltrue([for n, p in output.pools :
      length([for ni in try(output.node_classes[n].spec.advancedNetworking.networkInterfaces, []) : ni if contains(keys(ni), "secondaryIPv4PrefixCount")]) == (p.efa_interfaces == 0 ? 0 : 1)
    ])
    error_message = "secondaryIPv4PrefixCount only on entry 0 (never on efa-only)"
  }
  assert {
    condition = alltrue([for n, p in output.pools :
      length(distinct([for ni in try(output.node_classes[n].spec.advancedNetworking.networkInterfaces, []) : "${ni.networkCardIndex}/${ni.deviceIndex}"])) == length(try(output.node_classes[n].spec.advancedNetworking.networkInterfaces, []))
    ])
    error_message = "(networkCardIndex, deviceIndex) pairs must be unique"
  }
  assert {
    condition = alltrue([for n, p in output.pools :
      alltrue([for ni in try(output.node_classes[n].spec.advancedNetworking.networkInterfaces, []) : ni.networkCardIndex < p.network_cards])
    ])
    error_message = "no interface may reference a network card the instance does not have"
  }
}

# Exact layouts for the shapes with a single card (one EFA) and four cards.
run "exact_layouts_single_and_four_cards" {
  command = plan
  module {
    source = "./modules/gpu-pools"
  }
  variables {
    instance_types = { for k, v in jsondecode(file("tests/fixtures/instance-types.json")) : k => v if contains(["g6e.12xlarge", "g6e.48xlarge", "p5.48xlarge"], k) }
    offerings      = { for k, v in jsondecode(file("tests/fixtures/instance-types.json")) : k => ["use2-az1"] if contains(["g6e.12xlarge", "g6e.48xlarge", "p5.48xlarge"], k) }
  }

  assert {
    condition = jsonencode([for ni in output.node_classes[one([for n, p in output.pools : n if p.gpu_name == "L40S" && p.gpus_per_node == 4])].spec.advancedNetworking.networkInterfaces : "${ni.networkCardIndex}/${ni.deviceIndex}/${ni.interfaceType}"]) == jsonencode([
      "0/0/interface", "0/1/efa-only"
    ])
    error_message = "g6e.12xlarge (1 card, 1 EFA): ENA + one efa-only on card 0 device 1"
  }
  assert {
    condition = jsonencode([for ni in output.node_classes[one([for n, p in output.pools : n if p.gpu_name == "L40S" && p.gpus_per_node == 8])].spec.advancedNetworking.networkInterfaces : "${ni.networkCardIndex}/${ni.deviceIndex}/${ni.interfaceType}"]) == jsonencode([
      "0/0/interface", "0/1/efa-only", "1/0/efa-only", "2/0/efa-only", "3/0/efa-only"
    ])
    error_message = "g6e.48xlarge (4 cards, 4 EFA): ENA + efa-only card0/dev1 + cards 1..3"
  }
  assert {
    condition     = length(output.node_classes["gpu-h100-od"].spec.advancedNetworking.networkInterfaces) == 33
    error_message = "p5.48xlarge (32 cards, 32 EFA): 1 ENA + 32 efa-only"
  }
}

run "mixed_list_produces_several_pools" {
  command = plan
  module {
    source = "./modules/gpu-pools"
  }
  variables {
    instance_types = { for k, v in jsondecode(file("tests/fixtures/instance-types.json")) : k => v if contains(["p6-b200.48xlarge", "g6e.xlarge", "g6e.2xlarge"], k) }
    offerings      = { for k, v in jsondecode(file("tests/fixtures/instance-types.json")) : k => ["use2-az1"] if contains(["p6-b200.48xlarge", "g6e.xlarge", "g6e.2xlarge"], k) }
  }

  assert {
    condition     = jsonencode(sort(keys(output.pools))) == jsonencode(["gpu-b200-od", "gpu-l40s-od"])
    error_message = "b200 + two single-GPU L40S types must yield exactly two pools"
  }
  assert {
    condition     = !contains(keys(output.node_classes["gpu-l40s-od"].spec), "advancedNetworking") && contains(keys(output.node_classes["gpu-b200-od"].spec), "advancedNetworking")
    error_message = "only the EFA pool gets advancedNetworking"
  }
  assert {
    condition     = output.node_pools["gpu-l40s-od"].spec.template.metadata.labels["nvidia.com/gpu.product"] == "NVIDIA-L40S" && output.node_pools["gpu-b200-od"].spec.template.metadata.labels["nvidia.com/gpu.product"] == "NVIDIA-B200"
    error_message = "each pool carries its own derived GPU product"
  }
  assert {
    condition = (
      jsonencode([for r in output.node_pools["gpu-l40s-od"].spec.template.spec.requirements : r.values if r.key == "node.kubernetes.io/instance-type"][0]) == jsonencode(["g6e.2xlarge", "g6e.xlarge"]) &&
      jsonencode([for r in output.node_pools["gpu-b200-od"].spec.template.spec.requirements : r.values if r.key == "node.kubernetes.io/instance-type"][0]) == jsonencode(["p6-b200.48xlarge"])
    )
    error_message = "instance-type requirement lists exactly the group's types, sorted"
  }
  assert {
    condition     = output.pools["gpu-l40s-od"].vcpu_max == 8 && output.node_pools["gpu-l40s-od"].spec.limits.cpu == 8 && output.node_pools["gpu-l40s-od"].spec.limits["nvidia.com/gpu"] == 1
    error_message = "limits use the group's max vCPU and gpus_per_node x max_nodes (1 node by default on on-demand)"
  }
  assert {
    condition     = output.pools["gpu-l40s-od"].ephemeral_storage_min_gib == 232 && output.pools["gpu-l40s-od"].ephemeral_backing == "ebs" && output.pools["gpu-b200-od"].ephemeral_backing == "nvme"
    error_message = "g6e.xlarge (250 GB instance store < 256Gi) must be reported as EBS-backed (min over the group = 232 GiB); b200 is NVMe"
  }
}

run "gfd_product_override_and_options" {
  command = plan
  module {
    source = "./modules/gpu-pools"
  }
  variables {
    instance_types        = { for k, v in jsondecode(file("tests/fixtures/instance-types.json")) : k => v if contains(["p5.48xlarge", "p6-b200.48xlarge"], k) }
    offerings             = { for k, v in jsondecode(file("tests/fixtures/instance-types.json")) : k => ["use2-az1"] if contains(["p5.48xlarge", "p6-b200.48xlarge"], k) }
    product_overrides     = { H100 = "NVIDIA-H100-80GB-HBM3" }
    pod_ipv4_prefix_count = 8
  }

  assert {
    condition     = output.pools["gpu-h100-od"].gpu_product == "NVIDIA-H100-80GB-HBM3" && output.pools["gpu-b200-od"].gpu_product == "NVIDIA-B200"
    error_message = "product_overrides (keyed by GPU name) replaces only the matching derived product"
  }
  assert {
    condition     = output.node_classes["gpu-b200-od"].spec.advancedNetworking.networkInterfaces[0].secondaryIPv4PrefixCount == 8
    error_message = "pod_ipv4_prefix_count must reach entry 0"
  }
}

run "efa_off_drops_advanced_networking" {
  command = plan
  module {
    source = "./modules/gpu-pools"
  }
  variables {
    efa            = "off"
    instance_types = { for k, v in jsondecode(file("tests/fixtures/instance-types.json")) : k => v if contains(["p6-b200.48xlarge"], k) }
    offerings      = { "p6-b200.48xlarge" = ["use2-az1"] }
  }

  assert {
    condition     = !contains(keys(output.node_classes["gpu-b200-od"].spec), "advancedNetworking")
    error_message = "efa = off must render no advancedNetworking"
  }
}
