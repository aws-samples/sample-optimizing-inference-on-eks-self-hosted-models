# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

################################################################################
# Observability: kube-prometheus-stack (release `prometheus`), dcgm-exporter and
# the Dynamo Grafana dashboards.
#
# - Release name `prometheus` gives the Service
#   prometheus-kube-prometheus-prometheus.monitoring (used by the Dynamo
#   operator) and the `release: prometheus` label convention.
# - Selectors are opened (NilUsesHelmValues=false, any namespace) so the Dynamo
#   operator PodMonitors and the dcgm-exporter ServiceMonitor are picked up.
# - Control components are pinned to cpu-demo (the built-in `system` pool is
#   tainted CriticalAddonsOnly). node-exporter tolerates everything so diskstats
#   exist for the GPU node.
# - The Grafana admin password is NOT managed by Terraform: adminPassword = ""
#   overrides the chart default (prom-operator) so the Grafana subchart
#   generates a random one and keeps it across upgrades (lookup on its Secret).
#   Read it with:
#     kubectl -n monitoring get secret prometheus-grafana \
#       -o jsonpath='{.data.admin-password}' | base64 -d
################################################################################

locals {
  cpu_demo_node_selector = {
    "karpenter.sh/nodepool" = "cpu-demo"
  }

  # Explicit requests/limits so monitoring is accounted for on the shared
  # cpu-demo node. Measured on 2026-10-18 (no GPU load, 73k head series, 4h
  # peaks): prometheus 412 MiB / 47 mCPU, grafana 96 MiB, each grafana sidecar
  # ~98 MiB, operator 28 MiB, kube-state-metrics 15 MiB. Prometheus is sized
  # for the block (B200 DCGM + Dynamo 5s PodMonitors, 30d/45GB TSDB), not for
  # today's idle set. No CPU limits (avoid CFS throttling of scrapes).
  monitoring_resources = {
    prometheus = {
      requests = { cpu = "250m", memory = "2Gi" }
      limits   = { memory = "4Gi" }
    }
    grafana = {
      requests = { cpu = "100m", memory = "256Mi" }
      limits   = { memory = "512Mi" }
    }
    grafana_sidecar = {
      requests = { cpu = "10m", memory = "128Mi" }
      limits   = { memory = "256Mi" }
    }
    prometheus_operator = {
      requests = { cpu = "50m", memory = "64Mi" }
      limits   = { memory = "256Mi" }
    }
    kube_state_metrics = {
      requests = { cpu = "50m", memory = "64Mi" }
      limits   = { memory = "256Mi" }
    }
  }

  # Dashboards live in the repo root dashboards/ (source of truth); referenced
  # by relative path, never copied.
  sample_repo_dir = "${path.module}/../.."

  # Ready-made dashboard ConfigMaps:
  #   dynamo/disagg -> dashboards/dynamo, vendored from ai-dynamo/dynamo v1.5.1 (deploy/observability), Apache-2.0
  #   demo-*        -> dashboards/demo/*-configmap.yaml
  grafana_dashboard_files = {
    dynamo         = "${local.sample_repo_dir}/dashboards/dynamo/grafana-dynamo-dashboard-configmap.yaml"
    disagg         = "${local.sample_repo_dir}/dashboards/dynamo/grafana-disagg-dashboard-configmap.yaml"
    "demo-matrix"  = "${local.sample_repo_dir}/dashboards/demo/matrix-configmap.yaml"
    "demo-take"    = "${local.sample_repo_dir}/dashboards/demo/take-configmap.yaml"
    "demo-compare" = "${local.sample_repo_dir}/dashboards/demo/compare-configmap.yaml"
  }
}

resource "helm_release" "kube_prometheus_stack" {
  name             = "prometheus"
  repository       = "https://prometheus-community.github.io/helm-charts"
  chart            = "kube-prometheus-stack"
  version          = var.kube_prometheus_stack_version
  namespace        = "monitoring"
  create_namespace = false
  wait             = true
  timeout          = 900

  values = [yamlencode({
    defaultRules = {
      create = true
    }
    grafana = {
      enabled                  = true
      adminPassword            = ""
      defaultDashboardsEnabled = true
      service = {
        type = "ClusterIP"
      }
      resources = local.monitoring_resources.grafana
      sidecar = {
        resources = local.monitoring_resources.grafana_sidecar
      }
      nodeSelector = local.cpu_demo_node_selector
    }
    prometheus = {
      service = {
        type = "ClusterIP"
      }
      prometheusSpec = {
        serviceMonitorSelectorNilUsesHelmValues = false
        podMonitorSelectorNilUsesHelmValues     = false
        podMonitorNamespaceSelector             = {}
        # 15s gives DCGM/node-exporter >= 2 samples in the dashboards' 30s
        # rate() windows.
        scrapeInterval     = var.prometheus_scrape_interval
        evaluationInterval = var.prometheus_scrape_interval
        retention          = var.prometheus_retention
        retentionSize      = var.prometheus_retention_size
        storageSpec = {
          volumeClaimTemplate = {
            spec = {
              storageClassName = "auto-ebs-sc"
              accessModes      = ["ReadWriteOnce"]
              resources = {
                requests = {
                  storage = var.prometheus_storage_size
                }
              }
            }
          }
        }
        resources    = local.monitoring_resources.prometheus
        nodeSelector = local.cpu_demo_node_selector
      }
    }
    prometheusOperator = {
      resources    = local.monitoring_resources.prometheus_operator
      nodeSelector = local.cpu_demo_node_selector
      admissionWebhooks = {
        deployment = {
          nodeSelector = local.cpu_demo_node_selector
        }
        patch = {
          nodeSelector = local.cpu_demo_node_selector
        }
      }
    }
    alertmanager = {
      service = {
        type = "ClusterIP"
      }
      alertmanagerSpec = {
        nodeSelector = local.cpu_demo_node_selector
      }
    }
    kube-state-metrics = {
      resources    = local.monitoring_resources.kube_state_metrics
      nodeSelector = local.cpu_demo_node_selector
    }
    prometheus-node-exporter = {
      tolerations = [
        { operator = "Exists" }
      ]
    }
    }),
    # Grafana hardening (+ root_url with the CloudFront edge, grafana-edge.tf)
    local.grafana_edge_values,
  ]

  depends_on = [
    kubectl_manifest.namespace,
    kubectl_manifest.node_pool_cpu_demo,
    kubectl_manifest.auto_storage_class_default,
    kubectl_manifest.remove_legacy_gp2_default_annotation,
  ]
}

################################################################################
# NVIDIA DCGM Exporter
#
# DaemonSet restricted to GPU nodes (nvidia.com/gpu.present=true) and tolerant
# of the nvidia.com/gpu taint; without a GPU node it schedules nothing.
# The chart's default security context (root + SYS_ADMIN, needed for
# DCGM_FI_PROF_*) is kept; `monitoring` is PSA privileged.
# customMetrics = Dynamo's NVLink-enabled field list (v1.5.1).
# wait = false: GPU nodes come and go (reservations, scale to zero).
################################################################################

resource "helm_release" "dcgm_exporter" {
  name       = "dcgm-exporter"
  namespace  = "monitoring"
  repository = "https://nvidia.github.io/dcgm-exporter/helm-charts"
  chart      = "dcgm-exporter"
  version    = var.dcgm_exporter_version
  wait       = false
  timeout    = 300

  values = [yamlencode({
    service = {
      type = "ClusterIP"
    }
    serviceMonitor = {
      enabled       = true
      interval      = "15s"
      scrapeTimeout = "10s"
      additionalLabels = {
        release = "prometheus"
      }
    }
    nodeSelector = {
      "nvidia.com/gpu.present" = "true"
    }
    tolerations = [
      { key = "nvidia.com/gpu", operator = "Exists", effect = "NoSchedule" }
    ]
    customMetrics = file("${path.module}/observability/dcgm-metrics-with-nvlink.csv")
  })]

  depends_on = [helm_release.kube_prometheus_stack]
}

################################################################################
# Dynamo Grafana dashboards (picked up by the Grafana sidecar through the
# grafana_dashboard: "1" label; the manifests already target `monitoring`).
################################################################################

resource "kubectl_manifest" "grafana_dashboard" {
  for_each          = local.grafana_dashboard_files
  server_side_apply = true

  yaml_body = file(each.value)

  depends_on = [kubectl_manifest.namespace]
}

# SGLang engine dashboard: dashboards/sglang ships plain JSON, so it is wrapped
# in a sidecar-labelled ConfigMap here.
resource "kubectl_manifest" "grafana_sglang_dashboard" {
  server_side_apply = true

  yaml_body = yamlencode({
    apiVersion = "v1"
    kind       = "ConfigMap"
    metadata = {
      name      = "grafana-sglang-dashboard"
      namespace = "monitoring"
      labels = {
        grafana_dashboard = "1"
      }
    }
    data = {
      "sglang.json" = file("${local.sample_repo_dir}/dashboards/sglang/sglang.json")
    }
  })

  depends_on = [kubectl_manifest.namespace]
}

# NVIDIA DCGM exporter dashboard (GPU telemetry, dashboards/dcgm). It uses
# datasource uid `prometheus` (the kube-prometheus-stack default here) and only
# the `instance` (target) and `gpu` (dcgm-exporter) labels, both present on the
# 4.8.4 series.
resource "kubectl_manifest" "grafana_dcgm_dashboard" {
  server_side_apply = true

  yaml_body = yamlencode({
    apiVersion = "v1"
    kind       = "ConfigMap"
    metadata = {
      name      = "grafana-dcgm-dashboard"
      namespace = "monitoring"
      labels = {
        grafana_dashboard = "1"
      }
    }
    data = {
      "dcgm-grafana-dashboard.json" = file("${local.sample_repo_dir}/dashboards/dcgm/dcgm-grafana-dashboard.json")
    }
  })

  depends_on = [kubectl_manifest.namespace]
}
