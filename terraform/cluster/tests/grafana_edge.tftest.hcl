# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
#
# RED tests: the optional Grafana edge (grafana-edge.tf) on a full ROOT plan. Providers are mocked (mock_provider),
# so nothing touches AWS or a cluster.
#
# Contract: the edge is off by default. Turned on, CloudFront reaches Grafana through a VPC origin and an internal
# NLB whose listener is plain HTTP: a CloudFront VPC origin does not accept an NLB with a TLS listener, so the hop
# between CloudFront and the NLB cannot be encrypted by this edge. That hop is a deliberate opt-in: the plan is refused
# unless grafana_edge_allow_plain_http_origin = true acknowledges it, and the listener port drives the VPC origin,
# the Service and the security group rule together.
#
# Viewer TLS: with grafana_edge_domain + grafana_edge_viewer_certificate_arn (ACM, us-east-1, set together) the
# distribution serves that domain over SNI with the TLSv1.2_2021 security policy; without them it uses the default
# *.cloudfront.net certificate, on which CloudFront forces the TLSv1 security policy. The root URL is served from the
# Grafana page `dashboards` (default root object), never from `login`.

mock_provider "aws" {}
# grafana-edge.tf configures a second, aliased aws provider (us-east-1, for the CLOUDFRONT web ACL): it is
# mocked too, so `terraform test` needs no AWS credentials.
mock_provider "aws" {
  alias = "us_east_1"
}
mock_provider "kubernetes" {}
mock_provider "helm" {}
mock_provider "kubectl" {}

# Scoping only: bare mock_provider values are not valid inputs (see root_validation.tftest.hcl). These overrides
# supply p6-b200 facts, stub the two registry modules and give the NLB data source ARN-shaped values, so only the
# edge contract under test can fail. The ARN account segments are deliberately not 12 digits. The ACM certificate
# ARN is a resource argument, so the aws provider validates it even when mocked: its account segment is the reserved
# value `aws`, which that validator accepts, instead of a 12-digit account ID.
override_module {
  target = module.eks
  outputs = {
    cluster_name                       = "demo-eks"
    cluster_endpoint                   = "https://example.invalid"
    cluster_certificate_authority_data = ""
    cluster_arn                        = "arn:aws:eks:us-east-2:000000000:cluster/demo-eks"
    node_iam_role_name                 = "node-role"
    cluster_primary_security_group_id  = "sg-placeholder"
  }
}

override_module {
  target = module.vpc
  outputs = {
    vpc_id                  = "vpc-placeholder"
    azs                     = ["us-east-2a", "us-east-2b", "us-east-2c"]
    private_subnets         = ["subnet-a", "subnet-b", "subnet-c"]
    private_route_table_ids = []
    public_route_table_ids  = []
  }
}

override_data {
  target = data.aws_availability_zones.available
  values = {
    zone_ids = ["use2-az1", "use2-az2", "use2-az3"]
    names    = ["us-east-2a", "us-east-2b", "us-east-2c"]
  }
}

override_data {
  target = data.aws_ec2_instance_type.gpu
  values = {
    maximum_network_cards   = 8
    efa_supported           = true
    efa_maximum_interfaces  = 8
    default_vcpus           = 192
    memory_size             = 2097152
    total_instance_storage  = 30400
    supported_architectures = ["x86_64"]
    gpus                    = [{ name = "B200", count = 8, memory_size = 183359, manufacturer = "NVIDIA" }]
  }
}

override_data {
  target = data.aws_ec2_instance_type_offerings.gpu
  values = {
    instance_types = ["p6-b200.48xlarge"]
    locations      = ["use2-az1"]
  }
}

override_data {
  target = data.aws_lb.grafana_edge
  values = {
    arn      = "arn:aws:elasticloadbalancing:us-east-2:000000000:loadbalancer/net/demo-eks-grafana-edge/0000"
    dns_name = "demo-eks-grafana-edge.elb.us-east-2.amazonaws.com"
  }
}

variables {
  aws_region               = "us-east-2"
  cluster_name             = "demo-eks"
  eks_public_access_cidrs  = ["203.0.113.0/24"]
  gpu_instance_types       = ["p6-b200.48xlarge"]
  gpu_capacity_type        = "on-demand"
  capacity_reservation_ids = []
  model_id                 = "Qwen/Qwen3.8-27B-FP8"
  model_revision           = "0123456789abcdef0123456789abcdef01234567"
}

run "edge_off_by_default_plans_no_edge_resource" {
  command = plan

  assert {
    condition = alltrue([
      length(aws_security_group.grafana_nlb) == 0,
      length(kubernetes_service_v1.grafana_edge) == 0,
      length(data.aws_lb.grafana_edge) == 0,
      length(aws_cloudfront_vpc_origin.grafana) == 0,
      length(aws_vpc_security_group_ingress_rule.grafana_nlb_from_cloudfront) == 0,
      length(aws_wafv2_web_acl.grafana) == 0,
      length(aws_cloudfront_distribution.grafana) == 0,
    ])
    error_message = "enable_grafana_edge defaults to false: no NLB Service, VPC origin, WAF or distribution is planned"
  }

  assert {
    condition     = output.grafana_url == null
    error_message = "without the edge there is no public Grafana URL (kubectl port-forward is the path)"
  }
}

run "edge_on_without_the_plain_http_acknowledgement_is_refused" {
  command = plan

  variables {
    enable_grafana_edge = true
  }

  expect_failures = [var.enable_grafana_edge]
}

run "edge_on_with_the_acknowledgement_plans_the_http_only_origin" {
  command = plan

  variables {
    enable_grafana_edge                  = true
    grafana_edge_allow_plain_http_origin = true
  }

  assert {
    condition     = aws_cloudfront_vpc_origin.grafana[0].vpc_origin_endpoint_config[0].origin_protocol_policy == "http-only"
    error_message = "the VPC origin is http-only: the NLB listener is plain HTTP (VPC origins reject an NLB with a TLS listener)"
  }

  assert {
    condition = alltrue([
      aws_cloudfront_vpc_origin.grafana[0].vpc_origin_endpoint_config[0].http_port == 80,
      kubernetes_service_v1.grafana_edge[0].spec[0].port[0].port == 80,
      aws_vpc_security_group_ingress_rule.grafana_nlb_from_cloudfront[0].from_port == 80,
      aws_vpc_security_group_ingress_rule.grafana_nlb_from_cloudfront[0].to_port == 80,
    ])
    error_message = "default listener port 80 on the VPC origin, the Service and the CloudFront security group rule"
  }

  assert {
    condition = alltrue([
      for key in ["service.beta.kubernetes.io/aws-load-balancer-ssl-cert", "service.beta.kubernetes.io/aws-load-balancer-ssl-ports"] :
      !contains(keys(kubernetes_service_v1.grafana_edge[0].metadata[0].annotations), key)
    ])
    error_message = "no TLS listener on the NLB: a CloudFront VPC origin does not accept an NLB with a TLS listener"
  }

  assert {
    condition = alltrue([
      kubernetes_service_v1.grafana_edge[0].metadata[0].annotations["service.beta.kubernetes.io/aws-load-balancer-scheme"] == "internal",
      kubernetes_service_v1.grafana_edge[0].metadata[0].annotations["service.beta.kubernetes.io/aws-load-balancer-healthcheck-protocol"] == "HTTP",
    ])
    error_message = "the NLB is internal and its health check is plain HTTP, like the listener"
  }

  assert {
    condition = alltrue([
      aws_cloudfront_distribution.grafana[0].default_cache_behavior[0].viewer_protocol_policy == "redirect-to-https",
      aws_cloudfront_distribution.grafana[0].viewer_certificate[0].cloudfront_default_certificate,
      length(aws_wafv2_web_acl.grafana) == 1,
    ])
    error_message = "viewers still reach CloudFront over HTTPS only, behind the WAF web ACL"
  }
}

run "listener_port_drives_the_origin_the_service_and_the_security_group" {
  command = plan

  variables {
    enable_grafana_edge                  = true
    grafana_edge_allow_plain_http_origin = true
    grafana_edge_listener_port           = 8080
  }

  assert {
    condition = alltrue([
      aws_cloudfront_vpc_origin.grafana[0].vpc_origin_endpoint_config[0].http_port == 8080,
      kubernetes_service_v1.grafana_edge[0].spec[0].port[0].port == 8080,
      aws_vpc_security_group_ingress_rule.grafana_nlb_from_cloudfront[0].from_port == 8080,
      aws_vpc_security_group_ingress_rule.grafana_nlb_from_cloudfront[0].to_port == 8080,
    ])
    error_message = "grafana_edge_listener_port must set the VPC origin HTTP port, the Service port and the security group rule"
  }
}

run "default_certificate_keeps_the_tls_policy_cloudfront_forces_on_it" {
  command = plan

  variables {
    enable_grafana_edge                  = true
    grafana_edge_allow_plain_http_origin = true
  }

  assert {
    condition = alltrue([
      aws_cloudfront_distribution.grafana[0].viewer_certificate[0].cloudfront_default_certificate,
      aws_cloudfront_distribution.grafana[0].viewer_certificate[0].minimum_protocol_version == "TLSv1",
      try(length(aws_cloudfront_distribution.grafana[0].aliases), -1) == 0,
    ])
    error_message = "without grafana_edge_domain the distribution uses the default *.cloudfront.net certificate, no alias, and the TLSv1 security policy CloudFront forces on that certificate"
  }
}

run "custom_domain_and_certificate_raise_the_viewer_tls_floor" {
  command = plan

  variables {
    enable_grafana_edge                  = true
    grafana_edge_allow_plain_http_origin = true
    grafana_edge_domain                  = "grafana.example.com"
    grafana_edge_viewer_certificate_arn  = "arn:aws:acm:us-east-1:aws:certificate/0000aaaa-0000-0000-0000-00000000aaaa"
  }

  assert {
    condition = alltrue([
      !aws_cloudfront_distribution.grafana[0].viewer_certificate[0].cloudfront_default_certificate,
      aws_cloudfront_distribution.grafana[0].viewer_certificate[0].acm_certificate_arn == "arn:aws:acm:us-east-1:aws:certificate/0000aaaa-0000-0000-0000-00000000aaaa",
      aws_cloudfront_distribution.grafana[0].viewer_certificate[0].ssl_support_method == "sni-only",
      aws_cloudfront_distribution.grafana[0].viewer_certificate[0].minimum_protocol_version == "TLSv1.2_2021",
    ])
    error_message = "a custom domain serves the ACM certificate over SNI with the TLSv1.2_2021 security policy"
  }

  assert {
    condition     = aws_cloudfront_distribution.grafana[0].aliases == toset(["grafana.example.com"])
    error_message = "grafana_edge_domain is the only alternate domain name of the distribution"
  }

  assert {
    condition = alltrue([
      output.grafana_url == "https://grafana.example.com/",
      strcontains(helm_release.kube_prometheus_stack.values[1], "https://grafana.example.com/"),
    ])
    error_message = "with a custom domain, grafana_url and the Grafana root_url use that domain, not the *.cloudfront.net name"
  }
}

run "default_root_object_is_the_grafana_dashboards_page" {
  command = plan

  variables {
    enable_grafana_edge                  = true
    grafana_edge_allow_plain_http_origin = true
  }

  assert {
    condition     = aws_cloudfront_distribution.grafana[0].default_root_object == "dashboards"
    error_message = "the root URL is served from the Grafana page `dashboards` (signed-in SPA page, sign-in redirect otherwise); `login` would loop, since Grafana redirects a signed-in /login back to /"
  }
}

run "custom_domain_without_a_certificate_is_refused" {
  command = plan

  variables {
    enable_grafana_edge                  = true
    grafana_edge_allow_plain_http_origin = true
    grafana_edge_domain                  = "grafana.example.com"
  }

  expect_failures = [var.grafana_edge_viewer_certificate_arn]
}

run "certificate_without_a_custom_domain_is_refused" {
  command = plan

  variables {
    enable_grafana_edge                  = true
    grafana_edge_allow_plain_http_origin = true
    grafana_edge_viewer_certificate_arn  = "arn:aws:acm:us-east-1:aws:certificate/0000aaaa-0000-0000-0000-00000000aaaa"
  }

  expect_failures = [var.grafana_edge_viewer_certificate_arn]
}

run "certificate_outside_us_east_1_is_refused" {
  command = plan

  variables {
    enable_grafana_edge                  = true
    grafana_edge_allow_plain_http_origin = true
    grafana_edge_domain                  = "grafana.example.com"
    grafana_edge_viewer_certificate_arn  = "arn:aws:acm:us-east-2:aws:certificate/0000aaaa-0000-0000-0000-00000000aaaa"
  }

  expect_failures = [var.grafana_edge_viewer_certificate_arn]
}
