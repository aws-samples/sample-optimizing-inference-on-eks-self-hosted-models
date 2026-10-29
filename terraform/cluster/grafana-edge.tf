# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

################################################################################
# Grafana edge (optional, var.enable_grafana_edge, default false):
# CloudFront -> VPC Origin -> internal NLB -> Grafana pod
#
# Off by default: reach Grafana with
#   kubectl -n monitoring port-forward svc/prometheus-grafana 3000:80
# Every resource and data source below has count = var.enable_grafana_edge ? 1 : 0.
#
# Publishes ONLY the Grafana UI (monitoring/prometheus-grafana, release
# `prometheus`) over HTTPS (viewer certificate: see the distribution). No
# Prometheus, Dynamo frontends or inference endpoints are exposed.
#
#   viewer --HTTPS--> CloudFront (+ AWS WAF, us-east-1)
#          --plain HTTP:grafana_edge_listener_port (private, VPC origin ENIs)-->
#            internal NLB (EKS Auto Mode, TCP listener)
#          --TCP:3000 (ip targets)--> Grafana pod
#
# Plain-HTTP origin hop (accepted trade-off, explicit opt-in): the Grafana
# login and password, the session cookie and every API call and query result
# cross the CloudFront -> NLB -> pod hops unencrypted. They stay on the private
# VPC origin path (layers 1 and 2 below), never on the internet. A CloudFront
# VPC origin does not accept an NLB with a TLS listener, so the
# ssl-cert/ssl-ports annotations cannot encrypt this hop; TLS on it needs an
# internal ALB origin with an ACM certificate for a domain you own (not shipped
# here). enable_grafana_edge = true is refused unless
# grafana_edge_allow_plain_http_origin = true (variables.tf).
#
# The NLB is created by the load balancing capability of EKS Auto Mode; this
# stack does not install the AWS Load Balancer Controller.
#
# Access control layers:
#   1. The NLB is `internal` (private subnets, no public IP).
#   2. The NLB security group only admits the CloudFront service-managed
#      security group `CloudFront-VPCOrigins-Service-SG` (option 2 of the
#      CloudFront VPC origins doc: more restrictive than the origin-facing
#      prefix list and it costs 1 SG rule instead of the prefix-list weight).
#   3. AWS WAF (managed rule groups + rate limits) on the distribution.
#   4. Grafana login (anonymous access and sign-up disabled, secure cookies).
#
# Admin password: NOT managed by Terraform. The chart keeps
# its random password in Secret monitoring/prometheus-grafana, key
# admin-password (observability.tf sets adminPassword = ""), so nothing secret
# is in state or outputs. Read it without echoing it, e.g.:
#   kubectl -n monitoring get secret prometheus-grafana \
#     -o jsonpath='{.data.admin-password}' | base64 -d
#
# Docs:
#   https://docs.aws.amazon.com/eks/latest/userguide/auto-configure-nlb.html
#   https://docs.aws.amazon.com/eks/latest/userguide/auto-networking.html
#   https://docs.aws.amazon.com/AmazonCloudFront/latest/DeveloperGuide/private-content-vpc-origins.html
#
# Single apply, no -target phases. The NLB is created by EKS Auto Mode, not by
# Terraform, and the dependency graph orders the rest:
#   1. kubernetes_service_v1.grafana_edge, after the monitoring namespace and
#      the NLB security group rules, waits until Auto Mode gives the Service
#      its load balancer;
#   2. data.aws_lb.grafana_edge finds that NLB by its Auto Mode stack tag;
#   3. aws_cloudfront_vpc_origin.grafana is created on it (CloudFront accepts
#      the NLB while it is still provisioning), then the ingress rule from the
#      CloudFront service-managed SG, the WAF and the distribution.
# The apply that turns the edge on takes about 12 minutes more: about 9.5 for
# the VPC origin (timeout 30m) and under 3 for the distribution (measured on a
# first apply). The helm values of the release `prometheus` get the CloudFront
# domain (root_url) in the same apply.
################################################################################
# Providers
#
# WAF web ACLs with scope CLOUDFRONT must be created in us-east-1.
################################################################################

provider "aws" {
  alias  = "us_east_1"
  region = "us-east-1"

  default_tags {
    tags = local.tags
  }
}

################################################################################
# Locals
################################################################################

locals {
  grafana_edge_name      = "${local.cluster_name}-grafana-edge"
  grafana_edge_namespace = "monitoring"
  grafana_edge_service   = "grafana-edge"
  grafana_container_port = 3000

  # Viewer certificate: the domain you own with its ACM certificate (us-east-1)
  # when grafana_edge_domain is set (variables.tf pairs it with
  # grafana_edge_viewer_certificate_arn), else the default *.cloudfront.net one.
  grafana_edge_custom_domain = var.grafana_edge_domain != ""
  grafana_edge_host          = local.grafana_edge_custom_domain ? var.grafana_edge_domain : one(aws_cloudfront_distribution.grafana[*].domain_name)

  # Values merged on top of the kube-prometheus-stack release `prometheus`
  # (observability.tf, second values document). Without the edge Grafana is
  # reached over a plain-HTTP port-forward: no server URL, no secure cookies.
  grafana_edge_values = yamlencode({
    grafana = {
      "grafana.ini" = {
        for section, values in {
          server = var.enable_grafana_edge ? {
            domain   = local.grafana_edge_host
            root_url = "https://${local.grafana_edge_host}/"
          } : null
          security = {
            for key, value in {
              cookie_secure    = var.enable_grafana_edge ? true : null
              cookie_samesite  = var.enable_grafana_edge ? "lax" : null
              disable_gravatar = true
            } : key => value if value != null
          }
          "auth.anonymous" = {
            enabled = false
          }
          users = {
            allow_sign_up = false
          }
        } : section => values if values != null
      }
    }
  })
}

################################################################################
# NLB security group
#
# Ingress: only the CloudFront VPC origin service-managed SG, on the listener
# port (rule created after the VPC origin exists, see below).
# Egress: only the Grafana container port on the node/pod security group
# (Auto Mode pods use the node SG = cluster primary SG; covers health checks).
################################################################################

resource "aws_security_group" "grafana_nlb" {
  #checkov:skip=CKV2_AWS_5:Attached to the Auto Mode NLB through the service.beta.kubernetes.io/aws-load-balancer-security-groups annotation of kubernetes_service_v1.grafana_edge.
  count = var.enable_grafana_edge ? 1 : 0

  name        = "${local.grafana_edge_name}-nlb"
  description = "Internal NLB for Grafana; ingress only from CloudFront VPC origins"
  vpc_id      = module.vpc.vpc_id

  tags = merge(local.tags, {
    Name = "${local.grafana_edge_name}-nlb"
  })
}

resource "aws_vpc_security_group_egress_rule" "grafana_nlb_to_pods" {
  count = var.enable_grafana_edge ? 1 : 0

  security_group_id            = aws_security_group.grafana_nlb[0].id
  description                  = "NLB to Grafana pods (traffic and health checks)"
  ip_protocol                  = "tcp"
  from_port                    = local.grafana_container_port
  to_port                      = local.grafana_container_port
  referenced_security_group_id = module.eks.cluster_primary_security_group_id
}

resource "aws_vpc_security_group_ingress_rule" "pods_from_grafana_nlb" {
  count = var.enable_grafana_edge ? 1 : 0

  security_group_id            = module.eks.cluster_primary_security_group_id
  description                  = "Grafana pods from the grafana-edge NLB"
  ip_protocol                  = "tcp"
  from_port                    = local.grafana_container_port
  to_port                      = local.grafana_container_port
  referenced_security_group_id = aws_security_group.grafana_nlb[0].id
}

################################################################################
# LoadBalancer Service -> internal NLB (EKS Auto Mode)
#
# A dedicated Service; the chart's ClusterIP Service is left untouched. Only
# annotations listed as supported by Auto Mode are used. No
# aws-load-balancer-ssl-cert/ssl-ports: the listener stays plain TCP, as a
# CloudFront VPC origin requires of an NLB (see the header).
################################################################################

resource "kubernetes_service_v1" "grafana_edge" {
  count = var.enable_grafana_edge ? 1 : 0

  metadata {
    name      = local.grafana_edge_service
    namespace = local.grafana_edge_namespace

    annotations = {
      "service.beta.kubernetes.io/aws-load-balancer-scheme"               = "internal"
      "service.beta.kubernetes.io/aws-load-balancer-nlb-target-type"      = "ip"
      "service.beta.kubernetes.io/aws-load-balancer-subnets"              = join(",", module.vpc.private_subnets)
      "service.beta.kubernetes.io/aws-load-balancer-security-groups"      = aws_security_group.grafana_nlb[0].id
      "service.beta.kubernetes.io/aws-load-balancer-healthcheck-protocol" = "HTTP"
      "service.beta.kubernetes.io/aws-load-balancer-healthcheck-path"     = "/api/health"
      "service.beta.kubernetes.io/aws-load-balancer-healthcheck-port"     = "traffic-port"
      "service.beta.kubernetes.io/aws-load-balancer-attributes"           = "load_balancing.cross_zone.enabled=true"
    }

    labels = {
      "app.kubernetes.io/name"      = "grafana-edge"
      "app.kubernetes.io/part-of"   = "observability"
      "app.kubernetes.io/component" = "edge"
    }
  }

  spec {
    type                = "LoadBalancer"
    load_balancer_class = "eks.amazonaws.com/nlb"

    # Same selector as the chart Service prometheus-grafana (release `prometheus`).
    selector = {
      "app.kubernetes.io/instance" = "prometheus"
      "app.kubernetes.io/name"     = "grafana"
    }

    port {
      name        = "http"
      port        = var.grafana_edge_listener_port
      target_port = local.grafana_container_port
      protocol    = "TCP"
    }
  }

  wait_for_load_balancer = true

  # The monitoring namespace (and, through it, the cluster and its creator
  # access entry) must exist first: nothing else orders the Service after it.
  depends_on = [
    kubectl_manifest.namespace,
    aws_vpc_security_group_egress_rule.grafana_nlb_to_pods,
    aws_vpc_security_group_ingress_rule.pods_from_grafana_nlb,
  ]
}

# NLB created by Auto Mode for the Service above. Auto Mode tags it with
# `service.eks.amazonaws.com/stack = <namespace>/<name>` (not the
# `service.k8s.aws/stack` key used by the self-managed AWS LB Controller).
data "aws_lb" "grafana_edge" {
  count = var.enable_grafana_edge ? 1 : 0

  tags = {
    "service.eks.amazonaws.com/stack" = "${kubernetes_service_v1.grafana_edge[0].metadata[0].namespace}/${kubernetes_service_v1.grafana_edge[0].metadata[0].name}"
    "eks:eks-cluster-name"            = local.cluster_name
  }
}

################################################################################
# CloudFront VPC origin
################################################################################

resource "aws_cloudfront_vpc_origin" "grafana" {
  count = var.enable_grafana_edge ? 1 : 0

  vpc_origin_endpoint_config {
    name = local.grafana_edge_name
    arn  = data.aws_lb.grafana_edge[0].arn

    # The NLB listener is plain HTTP (VPC origins reject an NLB with a TLS
    # listener): CloudFront connects to http_port only.
    origin_protocol_policy = "http-only"
    http_port              = var.grafana_edge_listener_port

    # Required by the API (HTTPSPort, default 443); not used with http-only.
    https_port = 443

    # Required by the provider; not used with http-only (no TLS to the origin).
    origin_ssl_protocols {
      items    = ["TLSv1.2"]
      quantity = 1
    }
  }

  timeouts {
    create = "30m"
    update = "30m"
    delete = "30m"
  }

  tags = local.tags
}

# Service-managed SG that CloudFront attaches to its VPC origin ENIs.
data "aws_security_group" "cloudfront_vpc_origins" {
  count = var.enable_grafana_edge ? 1 : 0

  vpc_id = module.vpc.vpc_id

  filter {
    name   = "group-name"
    values = ["CloudFront-VPCOrigins-Service-SG*"]
  }

  depends_on = [aws_cloudfront_vpc_origin.grafana]
}

resource "aws_vpc_security_group_ingress_rule" "grafana_nlb_from_cloudfront" {
  count = var.enable_grafana_edge ? 1 : 0

  security_group_id            = aws_security_group.grafana_nlb[0].id
  description                  = "CloudFront VPC origins to the Grafana NLB listener"
  ip_protocol                  = "tcp"
  from_port                    = var.grafana_edge_listener_port
  to_port                      = var.grafana_edge_listener_port
  referenced_security_group_id = data.aws_security_group.cloudfront_vpc_origins[0].id
}

################################################################################
# AWS WAF (scope CLOUDFRONT, us-east-1)
#
# No IP allowlist (human decision: reachable from any IP, Grafana login).
# Rate limits are per source IP over 5 minutes. The global limit is sized for
# dashboard auto-refresh (every panel issues one /api/ds/query per refresh) and
# shared conference NAT IPs; the login limit targets credential stuffing.
################################################################################

resource "aws_wafv2_web_acl" "grafana" {
  #checkov:skip=CKV2_AWS_31:Optional demo edge: sampled requests and CloudWatch metrics are on; full WAF logging needs a log destination (extra cost) and is left to the user.
  count = var.enable_grafana_edge ? 1 : 0

  provider = aws.us_east_1

  name = local.grafana_edge_name
  # WAF descriptions only allow [\w+=:#@/\-,.] and spaces (no parentheses).
  description = "Grafana edge on CloudFront - managed rule groups and rate limits"
  scope       = "CLOUDFRONT"

  default_action {
    allow {}
  }

  rule {
    name     = "AWS-AWSManagedRulesAmazonIpReputationList"
    priority = 0

    override_action {
      none {}
    }

    statement {
      managed_rule_group_statement {
        vendor_name = "AWS"
        name        = "AWSManagedRulesAmazonIpReputationList"
      }
    }

    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "grafana-ip-reputation"
      sampled_requests_enabled   = true
    }
  }

  rule {
    name     = "rate-limit-login"
    priority = 1

    action {
      block {}
    }

    statement {
      rate_based_statement {
        limit                 = var.grafana_waf_rate_limit_login
        aggregate_key_type    = "IP"
        evaluation_window_sec = 300

        scope_down_statement {
          and_statement {
            statement {
              byte_match_statement {
                search_string         = "/login"
                positional_constraint = "STARTS_WITH"

                field_to_match {
                  uri_path {}
                }

                text_transformation {
                  priority = 0
                  type     = "LOWERCASE"
                }
              }
            }

            statement {
              byte_match_statement {
                search_string         = "POST"
                positional_constraint = "EXACTLY"

                field_to_match {
                  method {}
                }

                text_transformation {
                  priority = 0
                  type     = "NONE"
                }
              }
            }
          }
        }
      }
    }

    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "grafana-rate-limit-login"
      sampled_requests_enabled   = true
    }
  }

  rule {
    name     = "rate-limit-global"
    priority = 2

    action {
      block {}
    }

    statement {
      rate_based_statement {
        limit                 = var.grafana_waf_rate_limit_global
        aggregate_key_type    = "IP"
        evaluation_window_sec = 300
      }
    }

    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "grafana-rate-limit-global"
      sampled_requests_enabled   = true
    }
  }

  rule {
    name     = "AWS-AWSManagedRulesKnownBadInputsRuleSet"
    priority = 3

    override_action {
      none {}
    }

    statement {
      managed_rule_group_statement {
        vendor_name = "AWS"
        name        = "AWSManagedRulesKnownBadInputsRuleSet"
      }
    }

    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "grafana-known-bad-inputs"
      sampled_requests_enabled   = true
    }
  }

  rule {
    name     = "AWS-AWSManagedRulesCommonRuleSet"
    priority = 4

    override_action {
      none {}
    }

    statement {
      managed_rule_group_statement {
        vendor_name = "AWS"
        name        = "AWSManagedRulesCommonRuleSet"

        dynamic "rule_action_override" {
          for_each = toset(var.grafana_waf_common_rule_count_overrides)

          content {
            name = rule_action_override.value

            action_to_use {
              count {}
            }
          }
        }
      }
    }

    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "grafana-common-rule-set"
      sampled_requests_enabled   = true
    }
  }

  visibility_config {
    cloudwatch_metrics_enabled = true
    metric_name                = "grafana-edge"
    sampled_requests_enabled   = true
  }

  tags = local.tags
}

################################################################################
# CloudFront distribution
#
# No caching (Managed-CachingDisabled); all viewer headers, cookies and query
# strings forwarded (Managed-AllViewer) so Grafana's session cookie, CSRF
# Origin/Host check and API calls work. WebSockets (Grafana Live) are supported
# by CloudFront natively when Upgrade/Connection are forwarded (AllViewer).
################################################################################

data "aws_cloudfront_cache_policy" "caching_disabled" {
  count = var.enable_grafana_edge ? 1 : 0

  name = "Managed-CachingDisabled"
}

data "aws_cloudfront_origin_request_policy" "all_viewer" {
  count = var.enable_grafana_edge ? 1 : 0

  name = "Managed-AllViewer"
}

# nosemgrep: terraform.aws.security.aws-insecure-cloudfront-distribution-tls-version, terraform.aws.security.aws-cloudfront-insecure-tls.aws-insecure-cloudfront-distribution-tls-version -- TLSv1.2_2021 (sni-only) whenever grafana_edge_domain and grafana_edge_viewer_certificate_arn are set; on the default *.cloudfront.net certificate CloudFront forces the TLSv1 security policy, which cannot be raised.
resource "aws_cloudfront_distribution" "grafana" {
  #checkov:skip=CKV2_AWS_42:The custom certificate is optional (grafana_edge_domain + grafana_edge_viewer_certificate_arn); without it the default *.cloudfront.net certificate is used.
  #checkov:skip=CKV_AWS_174:TLSv1.2_2021 (sni-only) whenever grafana_edge_viewer_certificate_arn is set; on the default *.cloudfront.net certificate CloudFront forces the TLSv1 security policy, which cannot be raised.
  #checkov:skip=CKV2_AWS_32:Grafana sets its own security headers (secure cookies, no embedding by default); a managed headers policy would change the live distribution.
  #checkov:skip=CKV2_AWS_47:The attached web ACL (aws_wafv2_web_acl.grafana) runs AWSManagedRulesKnownBadInputsRuleSet, which holds the Log4JRCE rules; Checkov cannot follow the count-indexed reference.
  count = var.enable_grafana_edge ? 1 : 0

  enabled         = true
  comment         = "Grafana (${local.cluster_name}) via VPC origin"
  aliases         = local.grafana_edge_custom_domain ? [var.grafana_edge_domain] : []
  price_class     = var.grafana_edge_price_class
  http_version    = "http2and3"
  is_ipv6_enabled = true
  web_acl_id      = aws_wafv2_web_acl.grafana[0].arn

  # Root URL: CloudFront asks Grafana for the page `dashboards` instead of `/`.
  # Signed in, Grafana answers with the same single-page app as for `/` (the
  # browser stays on `/`, so the home dashboard opens); signed out, it redirects
  # to /login. Not `login`: Grafana redirects a signed-in /login back to `/`,
  # which CloudFront would turn into /login again (redirect loop). Only the
  # default cache behavior exists, so its policies apply to the rewritten path.
  default_root_object = "dashboards"

  origin {
    origin_id   = "grafana-nlb"
    domain_name = data.aws_lb.grafana_edge[0].dns_name

    vpc_origin_config {
      vpc_origin_id            = aws_cloudfront_vpc_origin.grafana[0].id
      origin_read_timeout      = 60
      origin_keepalive_timeout = 5
    }
  }

  default_cache_behavior {
    target_origin_id         = "grafana-nlb"
    viewer_protocol_policy   = "redirect-to-https"
    allowed_methods          = ["GET", "HEAD", "OPTIONS", "PUT", "POST", "PATCH", "DELETE"]
    cached_methods           = ["GET", "HEAD"]
    cache_policy_id          = data.aws_cloudfront_cache_policy.caching_disabled[0].id
    origin_request_policy_id = data.aws_cloudfront_origin_request_policy.all_viewer[0].id
    compress                 = true
  }

  dynamic "logging_config" {
    for_each = var.grafana_edge_access_log_bucket == "" ? [] : [var.grafana_edge_access_log_bucket]

    content {
      bucket          = "${logging_config.value}.s3.amazonaws.com"
      prefix          = "grafana-edge/"
      include_cookies = false
    }
  }

  restrictions {
    geo_restriction {
      restriction_type = "none"
    }
  }

  # Viewer TLS. With grafana_edge_domain + grafana_edge_viewer_certificate_arn
  # (ACM, us-east-1): SNI and the TLSv1.2_2021 security policy. Without them:
  # the default *.cloudfront.net certificate, on which CloudFront forces the
  # TLSv1 security policy (it cannot be raised; hence the CKV_AWS_174 skip and
  # the Semgrep suppression above the resource). Security policy options:
  # https://docs.aws.amazon.com/AmazonCloudFront/latest/DeveloperGuide/DownloadDistValuesGeneral.html
  viewer_certificate {
    cloudfront_default_certificate = !local.grafana_edge_custom_domain
    acm_certificate_arn            = local.grafana_edge_custom_domain ? var.grafana_edge_viewer_certificate_arn : null
    ssl_support_method             = local.grafana_edge_custom_domain ? "sni-only" : null
    minimum_protocol_version       = local.grafana_edge_custom_domain ? "TLSv1.2_2021" : "TLSv1"
  }

  tags = merge(local.tags, {
    Name = local.grafana_edge_name
  })

  # The listener must admit the VPC origin SG before viewers hit the origin.
  depends_on = [aws_vpc_security_group_ingress_rule.grafana_nlb_from_cloudfront]
}

################################################################################
# Outputs (null when the edge is off)
################################################################################

output "grafana_url" {
  description = "Public Grafana URL (grafana_edge_domain, else the *.cloudfront.net name); null without the edge (use kubectl port-forward)"
  value       = var.enable_grafana_edge ? "https://${local.grafana_edge_host}/" : null
}

output "grafana_cloudfront_distribution_id" {
  description = "CloudFront distribution ID serving Grafana"
  value       = one(aws_cloudfront_distribution.grafana[*].id)
}

output "grafana_cloudfront_domain_name" {
  description = "*.cloudfront.net name of the Grafana distribution: the DNS target (CNAME or Route 53 alias) of grafana_edge_domain"
  value       = one(aws_cloudfront_distribution.grafana[*].domain_name)
}

output "grafana_nlb_dns_name" {
  description = "Internal NLB DNS name (resolvable only inside the VPC)"
  value       = one(data.aws_lb.grafana_edge[*].dns_name)
}

output "grafana_waf_web_acl_arn" {
  description = "AWS WAF web ACL (CLOUDFRONT scope) protecting the Grafana distribution"
  value       = one(aws_wafv2_web_acl.grafana[*].arn)
}

output "grafana_admin_secret" {
  description = "Where the chart-managed Grafana admin password lives (value never in state/outputs)"
  value       = { namespace = local.grafana_edge_namespace, name = "prometheus-grafana", key = "admin-password", user_key = "admin-user" }
}
