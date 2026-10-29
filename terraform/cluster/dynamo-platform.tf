# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

################################################################################
# NVIDIA Dynamo Platform (operator only, var.dynamo_version)
#
# Chart: NGC dynamo-platform-<version>.tgz. Value paths cross-checked against
# ai-dynamo/dynamo v1.5.1 deploy/helm/charts/platform/values.yaml and
# components/operator/values.yaml:
#   - etcd/NATS not installed (not needed since 1.4.0; discovery = kubernetes).
#   - grove, kai-scheduler and snapshot neither installed nor enabled.
#   - podMonitors.enabled = true (not null): with null the PodMonitors render
#     only if the Prometheus CRDs exist at Helm time; depends_on guarantees that
#     too. The operator PodMonitors scrape every 5 s in all namespaces, so no
#     extra PodMonitors are added (double scrape). nixlTelemetry adds nixl*
#     ports.
#   - The operator chart exposes no nodeSelector, so it is pinned to cpu-demo
#     through controllerManager.affinity.
################################################################################

resource "helm_release" "dynamo_platform" {
  name             = "dynamo-platform"
  namespace        = "dynamo-system"
  create_namespace = false
  chart            = "https://helm.ngc.nvidia.com/nvidia/ai-dynamo/charts/dynamo-platform-${var.dynamo_version}.tgz"
  wait             = true
  timeout          = 900

  values = [yamlencode({
    global = {
      etcd = {
        install = false
      }
      nats = {
        install = false
      }
      grove = {
        install = false
        enabled = false
      }
      kai-scheduler = {
        install = false
        enabled = false
      }
      snapshot = {
        install = false
      }
    }
    dynamo-operator = {
      discoveryBackend = "kubernetes"
      controllerManager = {
        affinity = {
          nodeAffinity = {
            requiredDuringSchedulingIgnoredDuringExecution = {
              nodeSelectorTerms = [{
                matchExpressions = [{
                  key      = "karpenter.sh/nodepool"
                  operator = "In"
                  values   = ["cpu-demo"]
                }]
              }]
            }
          }
        }
      }
      dynamo = {
        metrics = {
          prometheusEndpoint = local.prometheus_endpoint
          podMonitors = {
            enabled       = true
            nixlTelemetry = true
            labels = {
              release = "prometheus"
            }
          }
        }
      }
      metricsService = {
        labels = {
          release = "prometheus"
        }
      }
    }
  })]

  depends_on = [
    helm_release.kube_prometheus_stack,
    kubectl_manifest.namespace,
    kubectl_manifest.node_pool_cpu_demo,
  ]
}
