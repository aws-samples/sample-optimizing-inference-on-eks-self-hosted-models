# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

################################################################################
# VPC
#
# Spans every AZ of the region (or var.vpc_az_ids), selected and ordered by AZ
# ID, because EKS cluster subnets cannot change their AZ set later and a GPU
# reservation may land in any AZ. Subnet CIDRs depend only on the index in that
# fixed AZ-ID list. Changing the GPU types, reservations or AZs changes only the
# NodeClasses/NodePools, never the VPC, NAT or EKS.
################################################################################

data "aws_availability_zones" "available" {
  # Do not include local zones
  filter {
    name   = "opt-in-status"
    values = ["opt-in-not-required"]
  }
}

locals {
  az_name_by_id = zipmap(
    data.aws_availability_zones.available.zone_ids,
    data.aws_availability_zones.available.names,
  )

  vpc_az_ids = sort(coalesce(var.vpc_az_ids, data.aws_availability_zones.available.zone_ids))
  azs        = [for id in local.vpc_az_ids : local.az_name_by_id[id]]

  private_subnet_by_az_id = zipmap(local.vpc_az_ids, module.vpc.private_subnets)

  # cpu-demo (frontends, bench, monitoring) shares the AZ of the first GPU AZ
  # unless pinned, so requests do not cross AZs.
  primary_az_id             = coalesce(var.primary_az_id, module.gpu_pools.az_ids[0])
  primary_az                = lookup(local.az_name_by_id, local.primary_az_id, "")
  primary_private_subnet_id = lookup(local.private_subnet_by_az_id, local.primary_az_id, null)

  # EKS control-plane ENIs: every private subnet except AZs EKS rejects.
  control_plane_subnet_ids = [
    for i, id in local.vpc_az_ids : module.vpc.private_subnets[i]
    if !contains(var.eks_control_plane_excluded_az_ids, id)
  ]
}

module "vpc" {
  #checkov:skip=CKV_TF_1:Registry module pinned to an exact version (version below); registry sources have no commit hash.
  source  = "terraform-aws-modules/vpc/aws"
  version = "6.6.0"

  name = local.cluster_name
  cidr = var.vpc_cidr

  azs             = local.azs
  private_subnets = [for k, v in local.azs : cidrsubnet(var.vpc_cidr, 4, k + 1)]
  public_subnets  = [for k, v in local.azs : cidrsubnet(var.vpc_cidr, 8, k)]

  enable_nat_gateway = true
  single_nat_gateway = true

  public_subnet_tags = {
    "kubernetes.io/role/elb" = 1
  }

  private_subnet_tags = {
    "kubernetes.io/role/internal-elb" = 1
    # Used by EKS Auto Mode NodeClass subnetSelectorTerms to discover subnets.
    "eks.amazonaws.com/discovery" = local.cluster_name
  }

  tags = local.tags
}
