# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

################################################################################
# EKS Cluster (Auto Mode)
#
# Auto Mode provides managed compute (Karpenter), EBS CSI, load balancing and
# the NVIDIA device plugin. Only the built-in `system` pool is enabled
# (CriticalAddonsOnly taint); workloads land on cpu-demo (nodepool.tf) and on
# the GPU pools (gpu-nodepool.tf).
################################################################################

module "eks" {
  #checkov:skip=CKV_TF_1:Registry module pinned to an exact version (version below); registry sources have no commit hash.
  source  = "terraform-aws-modules/eks/aws"
  version = "21.15.1"

  name               = local.cluster_name
  kubernetes_version = var.kubernetes_version

  # Public endpoint restricted to var.eks_public_access_cidrs; private endpoint
  # for in-VPC traffic (nodes, pods).
  endpoint_public_access       = true
  endpoint_public_access_cidrs = var.eks_public_access_cidrs
  endpoint_private_access      = true

  # Gives the Terraform identity admin access to the cluster.
  enable_cluster_creator_admin_permissions = true

  # Pod Identity only (no OIDC provider / IRSA).
  enable_irsa = false

  # EKS default envelope encryption under an AWS-owned key. encryption_config
  # must be null (not {}) when no KMS key is created: with {} the module renders
  # key_arn = null and plan fails.
  encryption_config = null
  create_kms_key    = false

  enabled_log_types = ["audit"]

  # With a non-empty node_pools list the module also creates the Auto Mode node
  # IAM role and its EC2 access entry (reused by the custom NodeClasses).
  compute_config = {
    enabled    = true
    node_pools = ["system"]
  }

  # Pinned add-ons: an unpinned add-on follows most_recent, so a newly
  # published build drifts the plan (and rolls a DaemonSet on every node).
  addons = {
    coredns = {}
    eks-node-monitoring-agent = {
      addon_version = var.node_monitoring_agent_addon_version
    }
    eks-pod-identity-agent = {
      before_compute = true
    }
    kube-proxy = {
      addon_version = var.kube_proxy_addon_version
    }
    vpc-cni = {
      before_compute = true
      addon_version  = var.vpc_cni_addon_version
    }
  }

  # Nodes may use every private subnet (all AZs); control-plane ENIs skip the
  # AZ IDs EKS rejects.
  vpc_id                   = module.vpc.vpc_id
  subnet_ids               = module.vpc.private_subnets
  control_plane_subnet_ids = local.control_plane_subnet_ids

  tags = local.tags
}
