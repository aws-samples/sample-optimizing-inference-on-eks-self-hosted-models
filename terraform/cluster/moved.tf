# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

################################################################################
# Address moves from an earlier layout of this stack
#
# The GPU NodeClass/NodePool gained a for_each (one per pool) and the Grafana
# edge resources gained a count (enable_grafana_edge). These blocks let a state
# of the earlier layout plan without replacing anything; on a new stack they are
# no-ops. Every other resource kept its address.
################################################################################

moved {
  from = kubectl_manifest.node_class_gpu_b200_cb[0]
  to   = kubectl_manifest.gpu_node_class["gpu-b200-cb"]
}

moved {
  from = kubectl_manifest.node_pool_gpu_b200_cb[0]
  to   = kubectl_manifest.gpu_node_pool["gpu-b200-cb"]
}

moved {
  from = aws_security_group.grafana_nlb
  to   = aws_security_group.grafana_nlb[0]
}

moved {
  from = aws_vpc_security_group_egress_rule.grafana_nlb_to_pods
  to   = aws_vpc_security_group_egress_rule.grafana_nlb_to_pods[0]
}

moved {
  from = aws_vpc_security_group_ingress_rule.pods_from_grafana_nlb
  to   = aws_vpc_security_group_ingress_rule.pods_from_grafana_nlb[0]
}

moved {
  from = kubernetes_service_v1.grafana_edge
  to   = kubernetes_service_v1.grafana_edge[0]
}

moved {
  from = aws_cloudfront_vpc_origin.grafana
  to   = aws_cloudfront_vpc_origin.grafana[0]
}

moved {
  from = aws_vpc_security_group_ingress_rule.grafana_nlb_from_cloudfront
  to   = aws_vpc_security_group_ingress_rule.grafana_nlb_from_cloudfront[0]
}

moved {
  from = aws_wafv2_web_acl.grafana
  to   = aws_wafv2_web_acl.grafana[0]
}

moved {
  from = aws_cloudfront_distribution.grafana
  to   = aws_cloudfront_distribution.grafana[0]
}
