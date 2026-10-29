# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

################################################################################
# EKS Auto Mode - CPU NodeClass/NodePool (cpu-demo) and default StorageClass
#
# Conventions:
#   - kind: NodeClass (apiVersion: eks.amazonaws.com/v1)
#   - nodeClassRef.group is eks.amazonaws.com
#   - requirement keys use the eks.amazonaws.com/instance-* labels
#   - no class is named `default` (Auto Mode owns it when a built-in pool is on)
#
# cpu-demo hosts every non-GPU workload (Prometheus, Grafana, Dynamo operator,
# frontends, bench, stage mocker). It is pinned to the primary AZ (default: the
# first GPU AZ) so frontends and the bench pod share the GPU node AZ. Disruption is fully off
# (WhenEmpty + consolidateAfter Never + budgets nodes "0"): demo nodes are never
# consolidated or drift-replaced; expireAfter 480h stays below the Auto Mode
# 21-day maximum node lifetime. Empty nodes are therefore NOT reclaimed
# automatically: delete their NodeClaims by hand when no longer needed.
################################################################################

locals {
  # Shared by every NodeClass: primary-AZ private subnet + cluster primary SG
  # (the latter already carries the self-referencing all-traffic rules EFA
  # requires).
  primary_az_node_class_network = {
    subnetSelectorTerms        = [{ id = local.primary_private_subnet_id }]
    securityGroupSelectorTerms = [{ id = module.eks.cluster_primary_security_group_id }]
  }
}

resource "kubectl_manifest" "node_class_cpu_demo" {
  server_side_apply = true

  yaml_body = yamlencode({
    apiVersion = "eks.amazonaws.com/v1"
    kind       = "NodeClass"
    metadata = {
      name = "cpu-demo"
    }
    spec = merge(local.primary_az_node_class_network, {
      role = module.eks.node_iam_role_name
      # Room for the large SGLang/Dynamo runtime images pulled on CPU nodes
      # (frontend pre-pull, preflight).
      ephemeralStorage = {
        size       = "256Gi"
        iops       = 3000
        throughput = 125
      }
      tags = {
        Project   = var.project_name
        nodeclass = "cpu-demo"
      }
    })
  })

  lifecycle {
    precondition {
      condition     = local.primary_private_subnet_id != null
      error_message = "primary_az_id must be one of the VPC AZ IDs (local.vpc_az_ids)."
    }
  }

  depends_on = [module.eks]
}

resource "kubectl_manifest" "node_pool_cpu_demo" {
  server_side_apply = true

  yaml_body = yamlencode({
    apiVersion = "karpenter.sh/v1"
    kind       = "NodePool"
    metadata = {
      name = "cpu-demo"
    }
    spec = {
      template = {
        metadata = {
          labels = {
            workload = "cpu-demo"
          }
        }
        spec = {
          nodeClassRef = {
            group = "eks.amazonaws.com"
            kind  = "NodeClass"
            name  = "cpu-demo"
          }
          expireAfter = "480h"
          requirements = [
            { key = "eks.amazonaws.com/instance-category", operator = "In", values = ["c", "m", "r"] },
            { key = "eks.amazonaws.com/instance-cpu", operator = "In", values = ["4", "8", "16"] },
            { key = "eks.amazonaws.com/instance-generation", operator = "Gt", values = ["5"] },
            { key = "kubernetes.io/arch", operator = "In", values = ["amd64"] },
            { key = "karpenter.sh/capacity-type", operator = "In", values = ["on-demand"] },
            { key = "topology.kubernetes.io/zone", operator = "In", values = [local.primary_az] },
          ]
        }
      }
      limits = {
        cpu = 64
      }
      disruption = {
        consolidationPolicy = "WhenEmpty"
        consolidateAfter    = "Never"
        budgets             = [{ nodes = "0" }]
      }
    }
  })

  depends_on = [kubectl_manifest.node_class_cpu_demo]
}

################################################################################
# StorageClass for Auto Mode (auto-ebs-sc)
#
# Auto Mode does not create a StorageClass. The in-tree `gp2` class cannot be
# repointed (immutable provisioner), so `auto-ebs-sc` (EBS CSI managed by Auto
# Mode) is created and made the cluster default.
################################################################################

resource "kubectl_manifest" "auto_storage_class_default" {
  server_side_apply = true

  yaml_body = <<-YAML
    apiVersion: storage.k8s.io/v1
    kind: StorageClass
    metadata:
      name: auto-ebs-sc
      annotations:
        storageclass.kubernetes.io/is-default-class: "true"
    allowedTopologies:
      - matchLabelExpressions:
          - key: eks.amazonaws.com/compute-type
            values:
              - auto
    provisioner: ebs.csi.eks.amazonaws.com
    volumeBindingMode: WaitForFirstConsumer
    reclaimPolicy: Delete
    parameters:
      type: gp3
      encrypted: "true"
  YAML

  depends_on = [module.eks]
}

# Drop the default annotation from the legacy in-tree gp2 StorageClass so
# `auto-ebs-sc` is the only cluster default.
resource "kubectl_manifest" "remove_legacy_gp2_default_annotation" {
  server_side_apply = true
  force_new         = false

  yaml_body = <<-YAML
    apiVersion: storage.k8s.io/v1
    kind: StorageClass
    metadata:
      name: gp2
      annotations:
        storageclass.kubernetes.io/is-default-class: "false"
  YAML

  depends_on = [module.eks]
}
