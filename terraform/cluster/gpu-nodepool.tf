# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

################################################################################
# GPU NodeClasses/NodePools: one pair per group of modules/gpu-pools
#
# Pool name = gpu-<gpu>-<cb|odcr|od|spot> (p6-b200 + capacity-block =
# gpu-b200-cb, the name the Track Q manifests select). Only one purchase model
# per stack (var.gpu_capacity_type):
#
# - Reserved pools (capacity-block, odcr) select the reservations through
#   capacityReservationSelectorTerms; Capacity Blocks and targeted ODCRs are
#   never open-matched. A NodeClass that selects a reservation disables the
#   automatic use of open ODCRs cluster-wide (EKS UG auto-odcr), so mixing a
#   reserved pool with on-demand pools that rely on open ODCRs is not
#   supported.
# - No placementGroupSelector (not supported with Capacity Blocks).
# - Disruption budgets do not stop health-driven replacement (EC2 status-check
#   failures and scheduled events).
# - A reservation bills whether or not a node runs; delete the NodeClaim (or
#   let the block end) when done.
################################################################################

resource "kubectl_manifest" "gpu_node_class" {
  for_each          = module.gpu_pools.node_classes
  server_side_apply = true

  yaml_body = yamlencode(each.value)

  depends_on = [module.eks]
}

resource "kubectl_manifest" "gpu_node_pool" {
  for_each          = module.gpu_pools.node_pools
  server_side_apply = true

  yaml_body = yamlencode(each.value)

  depends_on = [kubectl_manifest.gpu_node_class]
}

# g6e.xlarge-like types (instance store below gpu_ephemeral_storage_size) put
# /mnt/.ephemeral on EBS instead of the NVMe RAID0: slower model loads.
check "gpu_ephemeral_storage_on_nvme" {
  assert {
    condition     = alltrue([for name, p in module.gpu_pools.pools : p.ephemeral_backing == "nvme"])
    error_message = "GPU pools with an instance store smaller than gpu_ephemeral_storage_size use EBS for ephemeral storage: ${join(", ", [for name, p in module.gpu_pools.pools : name if p.ephemeral_backing != "nvme"])}."
  }
}

# capacity-block: a pool whose instance types have no block in
# capacity_reservation_ids (any state) never gets a node and its limits fall
# back to 1 node. A block of a type outside gpu_instance_types is already an
# error (modules/gpu-pools var.reservations).
check "gpu_capacity_block_per_pool" {
  assert {
    condition = var.gpu_capacity_type != "capacity-block" || alltrue([
      for name, p in module.gpu_pools.pools : anytrue([
        for id, r in data.aws_ec2_capacity_block_reservation.gpu : contains(p.instance_types, r.instance_type)
      ])
    ])
    error_message = "GPU pools without a Capacity Block of their instance types (no node can launch; limits fall back to 1 node): ${join(", ", [for name, p in module.gpu_pools.pools : name if !anytrue([for id, r in data.aws_ec2_capacity_block_reservation.gpu : contains(p.instance_types, r.instance_type)])])}. Add the block to capacity_reservation_ids or drop the types from gpu_instance_types."
  }
}
