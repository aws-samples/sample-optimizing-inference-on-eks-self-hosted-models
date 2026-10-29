# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

output "node_classes" {
  description = "EKS Auto Mode NodeClass manifests (apiVersion, kind, metadata, spec) keyed by pool name"
  value       = local.node_classes
}

output "node_pools" {
  description = "Karpenter NodePool manifests (apiVersion, kind, metadata, spec) keyed by pool name"
  value       = local.node_pools
}

output "pools" {
  description = "Facts of each pool: member types, GPU name/product/count/memory, network cards, EFA interfaces, max nodes, vCPU, host memory, ephemeral storage, capacity type and AZs"
  value       = local.pools
}

output "az_ids" {
  description = "AZ IDs of the GPU nodes"
  value       = local.az_ids
}

output "azs" {
  description = "AZ names of the GPU nodes (topology.kubernetes.io/zone values)"
  value       = local.azs
}
