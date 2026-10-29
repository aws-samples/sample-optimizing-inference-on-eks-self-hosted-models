# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

################################################################################
# Namespaces with explicit Pod Security Admission labels
#
# The EKS default is effectively privileged, so every namespace sets
# enforce/warn/audit explicitly:
#   privileged -> scenario namespaces (s-*), node-ops (GPU-node Jobs/DaemonSets
#                 on hostPath /mnt/.ephemeral) and monitoring (node-exporter,
#                 dcgm-exporter with SYS_ADMIN)
#   baseline   -> dynamo-system, bench, stage-mocker (warn/audit at restricted)
################################################################################

locals {
  psa_profiles = {
    privileged = {
      enforce = "privileged"
      warn    = "privileged"
      audit   = "privileged"
    }
    baseline = {
      enforce = "baseline"
      warn    = "restricted"
      audit   = "restricted"
    }
  }

  namespaces = {
    "s-agg"         = "privileged"
    "s-agg-hic"     = "privileged"
    "s-dis"         = "privileged"
    "s-dis-hic"     = "privileged"
    "node-ops"      = "privileged"
    "monitoring"    = "privileged"
    "dynamo-system" = "baseline"
    "bench"         = "baseline"
    "stage-mocker"  = "baseline"
  }
}

resource "kubectl_manifest" "namespace" {
  for_each          = local.namespaces
  server_side_apply = true

  yaml_body = yamlencode({
    apiVersion = "v1"
    kind       = "Namespace"
    metadata = {
      name = each.key
      labels = {
        "pod-security.kubernetes.io/enforce"         = local.psa_profiles[each.value].enforce
        "pod-security.kubernetes.io/enforce-version" = "latest"
        "pod-security.kubernetes.io/warn"            = local.psa_profiles[each.value].warn
        "pod-security.kubernetes.io/warn-version"    = "latest"
        "pod-security.kubernetes.io/audit"           = local.psa_profiles[each.value].audit
        "pod-security.kubernetes.io/audit-version"   = "latest"
      }
    }
  })

  depends_on = [module.eks]
}
