# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

################################################################################
# GPU discovery: EC2 facts of var.gpu_instance_types -> modules/gpu-pools
#
# Read at plan time (they depend on variables only), so the pool names (the
# for_each keys of gpu-nodepool.tf) are known before apply:
#   aws_ec2_instance_type              network cards, EFA, GPU name/count/memory,
#                                      vCPU, memory, instance store, arch
#   aws_ec2_instance_type_offerings    AZ IDs offering each type (on-demand/spot
#                                      AZ choice; existence, not capacity)
#   aws_ec2_capacity_block_reservation type, count, AZ and state of each
#                                      Capacity Block (capacity-block only)
# Targeted ODCRs have no data source: odcr needs gpu_az_ids and gpu_max_nodes.
# Calling identity needs ec2:DescribeInstanceTypes,
# ec2:DescribeInstanceTypeOfferings and ec2:DescribeCapacityReservations.
################################################################################

data "aws_ec2_instance_type" "gpu" {
  for_each      = toset(var.gpu_instance_types)
  instance_type = each.key
}

data "aws_ec2_instance_type_offerings" "gpu" {
  location_type = "availability-zone-id"

  filter {
    name   = "instance-type"
    values = var.gpu_instance_types
  }
}

data "aws_ec2_capacity_block_reservation" "gpu" {
  for_each = toset(var.gpu_capacity_type == "capacity-block" ? var.capacity_reservation_ids : [])
  id       = each.key
}

locals {
  # instance type -> AZ IDs offering it (instance_types and locations are
  # parallel lists).
  gpu_offerings = {
    for t in var.gpu_instance_types : t => sort(distinct([
      for i, offered in data.aws_ec2_instance_type_offerings.gpu.instance_types :
      data.aws_ec2_instance_type_offerings.gpu.locations[i] if offered == t
    ]))
  }
}

module "gpu_pools" {
  source = "./modules/gpu-pools"

  instance_types = {
    for t, d in data.aws_ec2_instance_type.gpu : t => {
      maximum_network_cards   = d.maximum_network_cards
      efa_supported           = d.efa_supported
      efa_maximum_interfaces  = d.efa_maximum_interfaces
      default_vcpus           = d.default_vcpus
      memory_size             = d.memory_size
      total_instance_storage  = d.total_instance_storage
      supported_architectures = d.supported_architectures
      gpus = [for g in d.gpus : {
        name         = g.name
        count        = g.count
        memory_size  = g.memory_size
        manufacturer = g.manufacturer
      }]
    }
  }

  capacity_type   = var.gpu_capacity_type
  reservation_ids = var.capacity_reservation_ids
  reservations = {
    for id, r in data.aws_ec2_capacity_block_reservation.gpu : id => {
      instance_type        = r.instance_type
      instance_count       = r.instance_count
      availability_zone_id = r.availability_zone_id
      state                = r.state
    }
  }
  offerings     = local.gpu_offerings
  gpu_az_ids    = var.gpu_az_ids
  gpu_max_nodes = var.gpu_max_nodes

  vpc_az_ids              = local.vpc_az_ids
  az_name_by_id           = local.az_name_by_id
  private_subnet_by_az_id = local.private_subnet_by_az_id
  node_role_name          = module.eks.node_iam_role_name
  security_group_id       = module.eks.cluster_primary_security_group_id
  project_name            = var.project_name

  pod_ipv4_prefix_count  = var.gpu_pod_ipv4_prefix_count
  ephemeral_storage_size = var.gpu_ephemeral_storage_size
  efa                    = var.gpu_efa
  expire_after           = var.gpu_expire_after
  consolidate_after      = var.gpu_consolidate_after
  product_overrides      = var.gpu_product_overrides
}
