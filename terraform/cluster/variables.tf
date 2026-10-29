# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

################################################################################
# Variables
#
# Every user-facing choice is documented in terraform.tfvars.example. No default
# holds an account ID, an IP, an AZ or a reservation ID. Account-specific names
# are derived from data.aws_caller_identity; AZs are selected by AZ ID (names
# differ per account).
################################################################################

# ----------------------------------------------------------------------------- stack

variable "aws_region" {
  description = "AWS region of the stack (it must offer the GPU instance types of gpu_instance_types)"
  type        = string

  validation {
    condition     = can(regex("^[a-z]{2}(-[a-z]+)+-[0-9]+$", var.aws_region))
    error_message = "aws_region must be a region code such as us-east-1 or us-east-2."
  }
}

variable "project_name" {
  description = "Project name: ECR repository prefix (<project_name>/sglang-runtime, <project_name>/bench), Project tag and default name prefix"
  type        = string
  default     = "dynamo-sample"

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{1,30}[a-z0-9]$", var.project_name))
    error_message = "project_name must be 3-32 lowercase letters, digits or hyphens, starting with a letter."
  }
}

variable "cluster_name" {
  description = "EKS cluster name <prefix>-<suffix> (also the prefix of IAM roles); null = <project_name>-<region code of the AZ IDs>, e.g. dynamo-sample-use2. The default bucket names are <prefix>-{models,results}-<account>-<suffix>"
  type        = string
  default     = null

  validation {
    condition     = var.cluster_name == null ? true : can(regex("^[a-z][a-z0-9-]{0,34}[a-z0-9]-[a-z0-9]+$", var.cluster_name)) && length(var.cluster_name) <= 38
    error_message = "cluster_name must be <= 38 lowercase letters, digits or hyphens, start with a letter and contain at least one hyphen (<prefix>-<suffix>)."
  }
}

variable "kubernetes_version" {
  description = "EKS Kubernetes version (review the add-on pins when changing it)"
  type        = string
  default     = "1.35"

  validation {
    condition     = can(regex("^1\\.[0-9]{2}$", var.kubernetes_version))
    error_message = "kubernetes_version must look like 1.35."
  }
}

variable "tags" {
  description = "Extra tags merged into the default tags (they win over Project, Environment, Region, Stack and ManagedBy)"
  type        = map(string)
  default     = {}

  # The character class of the IAM Tag API; KMS accepts the same set. Same condition as ../bootstrap.
  validation {
    condition     = alltrue([for k, v in var.tags : can(regex("^[\\p{L}\\p{Z}\\p{N}_.:/=+@-]*$", k)) && can(regex("^[\\p{L}\\p{Z}\\p{N}_.:/=+@-]*$", v))])
    error_message = "tags keys and values may only use letters, digits, spaces and _ . : / = + - @ (KMS and IAM reject any other character at apply)."
  }
}

# ----------------------------------------------------------------------------- network and API access

variable "vpc_cidr" {
  description = "CIDR block for the cluster VPC"
  type        = string
  default     = "10.70.0.0/16"

  validation {
    condition     = can(cidrhost(var.vpc_cidr, 0)) && try(tonumber(split("/", var.vpc_cidr)[1]) <= 16, false)
    error_message = "vpc_cidr must be a valid IPv4 CIDR of /16 or larger (one /20 private and one /24 public subnet per AZ are carved from it)."
  }
}

variable "vpc_az_ids" {
  description = "AZ IDs spanned by the VPC and the EKS node subnets; null = every opt-in-not-required AZ of the region (recommended: a later reservation in another AZ then needs no VPC change)"
  type        = list(string)
  default     = null
}

variable "eks_control_plane_excluded_az_ids" {
  description = "AZ IDs whose subnets EKS rejects for control-plane ENIs (e.g. use1-az3); kept as node subnets only"
  type        = list(string)
  default     = []
}

variable "primary_az_id" {
  description = "AZ ID of the cpu-demo pool (frontends, bench, monitoring); null = the first GPU AZ, so frontends and GPU workers share an AZ. Pin it to keep cpu-demo in place when the GPU AZ changes"
  type        = string
  default     = null

  validation {
    condition     = var.primary_az_id == null ? true : can(regex("^[a-z]+[0-9]+-az[0-9]+$", var.primary_az_id))
    error_message = "primary_az_id must be an AZ ID such as use1-az1."
  }
}

variable "eks_public_access_cidrs" {
  description = "CIDR blocks allowed to reach the public EKS API endpoint (your egress IPs). Required; a zero-length prefix needs allow_open_eks_api = true"
  type        = list(string)

  validation {
    condition     = length(var.eks_public_access_cidrs) > 0
    error_message = "eks_public_access_cidrs must list at least one CIDR."
  }

  validation {
    condition     = alltrue([for c in var.eks_public_access_cidrs : can(cidrhost(c, 0))])
    error_message = "eks_public_access_cidrs must contain valid CIDRs (e.g. 203.0.113.10/32)."
  }

  validation {
    # Any /0 (0.0.0.0/0, ::/0, 10.0.0.0/0, ...) opens the API to the Internet.
    condition     = var.allow_open_eks_api || alltrue([for c in var.eks_public_access_cidrs : try(tonumber(split("/", c)[1]) > 0, false)])
    error_message = "An open EKS API CIDR (prefix length 0) is rejected unless allow_open_eks_api = true. Use your egress IPs."
  }
}

variable "allow_open_eks_api" {
  description = "Accept a zero-length prefix in eks_public_access_cidrs (API reachable from any IP; authentication still requires IAM + an EKS access entry). Demo-only escape hatch"
  type        = bool
  default     = false
}

# ----------------------------------------------------------------------------- GPU

variable "gpu_instance_types" {
  description = "EC2 GPU instance types (P or G families) the GPU NodePools may launch; types with identical GPU and network layout share one NodeClass/NodePool"
  type        = list(string)

  validation {
    condition     = length(var.gpu_instance_types) > 0
    error_message = "gpu_instance_types must list at least one P or G instance type."
  }

  validation {
    condition     = alltrue([for t in var.gpu_instance_types : can(regex("^[pg][0-9]+[a-z0-9-]*\\.[0-9a-z]+$", t))])
    error_message = "gpu_instance_types may only contain P or G instance types (e.g. p6-b200.48xlarge, g6e.12xlarge)."
  }

  validation {
    condition     = length(distinct(var.gpu_instance_types)) == length(var.gpu_instance_types)
    error_message = "gpu_instance_types must not contain duplicates."
  }
}

variable "gpu_capacity_type" {
  description = "How the GPU nodes are bought: capacity-block | odcr | on-demand | spot. Terraform never purchases capacity: reservations are bought beforehand and listed in capacity_reservation_ids"
  type        = string
  default     = "on-demand"

  validation {
    condition     = contains(["capacity-block", "odcr", "on-demand", "spot"], var.gpu_capacity_type)
    error_message = "gpu_capacity_type must be one of: capacity-block, odcr, on-demand, spot."
  }
}

variable "capacity_reservation_ids" {
  description = "Capacity reservation IDs (cr-...) already bought: Capacity Blocks for capacity-block, targeted ODCRs for odcr; must be empty for on-demand and spot"
  type        = list(string)
  default     = []

  validation {
    condition     = alltrue([for id in var.capacity_reservation_ids : can(regex("^cr-[0-9a-f]+$", id))])
    error_message = "capacity_reservation_ids must contain EC2 capacity reservation IDs (cr-<hex>)."
  }

  validation {
    condition     = contains(["capacity-block", "odcr"], var.gpu_capacity_type) ? length(var.capacity_reservation_ids) > 0 : length(var.capacity_reservation_ids) == 0
    error_message = "gpu_capacity_type capacity-block and odcr need at least one capacity_reservation_ids entry; on-demand and spot take none."
  }
}

variable "gpu_az_ids" {
  description = "AZ IDs of the GPU nodes; [] = derived (capacity-block: the AZ of each block; on-demand/spot: the first VPC AZ offering every type). Required for odcr. Several AZs opt into multi-AZ GPU pools"
  type        = list(string)
  default     = []

  validation {
    condition     = alltrue([for id in var.gpu_az_ids : can(regex("^[a-z]+[0-9]+-az[0-9]+$", id))])
    error_message = "gpu_az_ids must contain AZ IDs such as use1-az1."
  }

  validation {
    condition     = var.gpu_capacity_type != "odcr" || length(var.gpu_az_ids) > 0
    error_message = "gpu_capacity_type = odcr needs gpu_az_ids: list the AZ IDs of the reservations (ODCRs have no data source)."
  }
}

variable "gpu_max_nodes" {
  description = "Maximum GPU nodes per pool (NodePool limits); null = derived (capacity-block: instances of the live blocks; on-demand/spot: 1). Required for odcr"
  type        = number
  default     = null

  validation {
    condition     = var.gpu_max_nodes == null ? true : var.gpu_max_nodes >= 1 && floor(var.gpu_max_nodes) == var.gpu_max_nodes
    error_message = "gpu_max_nodes must be an integer >= 1 (or null)."
  }

  validation {
    condition     = var.gpu_capacity_type != "odcr" || var.gpu_max_nodes != null
    error_message = "gpu_capacity_type = odcr needs gpu_max_nodes: the instance count of the reservations (ODCRs have no data source)."
  }
}

variable "gpu_efa" {
  description = "auto = static EFA interface layout on every type that supports EFA; off = no advancedNetworking (Auto Mode manages ENIs/IPs, no EFA devices)"
  type        = string
  default     = "auto"

  validation {
    condition     = contains(["auto", "off"], var.gpu_efa)
    error_message = "gpu_efa must be auto or off."
  }
}

variable "gpu_pod_ipv4_prefix_count" {
  description = "Number of /28 prefixes (16 pod IPs each) on the primary ENI of an EFA GPU node (static layout: no IPs are added after launch)"
  type        = number
  default     = 4

  validation {
    # >= 3 prefixes = >= 48 pod IPs, the floor for the >= 40 pods sized for the
    # GPU node (workers, DaemonSets, Jobs, debug/bench pods).
    condition     = var.gpu_pod_ipv4_prefix_count >= 3 && var.gpu_pod_ipv4_prefix_count <= 16 && floor(var.gpu_pod_ipv4_prefix_count) == var.gpu_pod_ipv4_prefix_count
    error_message = "gpu_pod_ipv4_prefix_count must be an integer between 3 (48 pod IPs) and 16."
  }
}

variable "gpu_ephemeral_storage_size" {
  description = "ephemeralStorage.size of the GPU NodeClasses; below the instance store Auto Mode builds an NVMe RAID0 at /mnt/.ephemeral, above it the node uses EBS (output gpu_pools[*].ephemeral_backing)"
  type        = string
  default     = "256Gi"

  validation {
    condition     = can(regex("^[0-9]+(Gi|Ti)$", var.gpu_ephemeral_storage_size))
    error_message = "gpu_ephemeral_storage_size must look like 256Gi or 1Ti."
  }
}

variable "gpu_expire_after" {
  description = "expireAfter of the GPU NodePools; null = 480h for reserved capacity, 168h for on-demand and spot (Auto Mode caps node lifetime at 21 days)"
  type        = string
  default     = null

  validation {
    condition     = var.gpu_expire_after == null ? true : can(regex("^[0-9]+h$", var.gpu_expire_after)) && try(tonumber(trimsuffix(var.gpu_expire_after, "h")) <= 504, false)
    error_message = "gpu_expire_after must be a number of hours <= 504, e.g. 480h."
  }
}

variable "gpu_consolidate_after" {
  description = "consolidateAfter of on-demand and spot GPU pools (reserved pools always use Never)"
  type        = string
  default     = "15m"

  validation {
    condition     = can(regex("^([0-9]+(s|m|h))+$|^Never$", var.gpu_consolidate_after))
    error_message = "gpu_consolidate_after must be a duration such as 15m or Never."
  }
}

variable "gpu_product_overrides" {
  description = "nvidia.com/gpu.product label per EC2 GPU name where the derived NVIDIA-<NAME> differs from GPU feature discovery (e.g. { H100 = \"NVIDIA-H100-80GB-HBM3\" })"
  type        = map(string)
  default     = {}
}

# ----------------------------------------------------------------------------- model and workload (generated env)

variable "model_id" {
  description = "Hugging Face repository ID of the served model (MODEL_ID of the generated env)"
  type        = string

  validation {
    condition     = can(regex("^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$", var.model_id))
    error_message = "model_id must be <org>/<name>, e.g. Qwen/Qwen3.8-27B-FP8."
  }
}

variable "model_revision" {
  description = "Hugging Face commit SHA of the model (40 hex characters; never a branch such as main)"
  type        = string

  validation {
    condition     = can(regex("^[0-9a-f]{40}$", var.model_revision))
    error_message = "model_revision must be a 40-character commit SHA."
  }
}

variable "model_s3_prefix" {
  description = "Object prefix of the model in the model bucket (MODEL_S3_PREFIX / MODEL_S3_URI of the generated env): scripts/10-stage-model.sh stages the model there and 11-prefetch.sh reads it; the model-stager role may write only there. null = <model_id>/<model_revision>"
  type        = string
  default     = null

  validation {
    condition     = var.model_s3_prefix == null ? true : (can(regex("^[A-Za-z0-9._-]+(/[A-Za-z0-9._-]+)*$", var.model_s3_prefix)) && !contains(split("/", var.model_s3_prefix), "..") && !contains(split("/", var.model_s3_prefix), "."))
    error_message = "model_s3_prefix must be segments of A-Za-z0-9._- joined by single slashes: no leading or trailing slash, no . or .. segment, no wildcard."
  }
}

variable "hf_token_secret_arn" {
  description = "Optional Secrets Manager secret ARN of a Hugging Face token (gated models). Terraform only grants secretsmanager:GetSecretValue on this ARN to the model-prefetch Pod Identity role; it never reads or stores the value"
  type        = string
  default     = null

  validation {
    condition     = var.hf_token_secret_arn == null ? true : can(regex("^arn:aws[a-z-]*:secretsmanager:[a-z0-9-]+:[0-9]+:secret:[A-Za-z0-9/_+=.@-]+$", var.hf_token_secret_arn))
    error_message = "hf_token_secret_arn must be a Secrets Manager secret ARN (arn:<partition>:secretsmanager:<region>:<account>:secret:<name>)."
  }
}

variable "runtime_image" {
  description = "Runtime image of the Frontend and the workers, <registry>/<repo>[:<tag>]@sha256:<digest> (RUNTIME_IMAGE of the generated env): the public NGC image of versions.env, or its mirror in the sglang-runtime repository this stack creates with create_runtime_mirror_repository = true; scripts/gen_env.py refuses an empty value"
  type        = string
  default     = ""

  validation {
    condition     = var.runtime_image == "" || can(regex("^[a-z0-9.-]+(:[0-9]+)?/[a-z0-9._/-]+(:[A-Za-z0-9._-]+)?@sha256:[0-9a-f]{64}$", var.runtime_image))
    error_message = "runtime_image must be pinned by digest: <registry>/<repo>@sha256:<64 hex>."
  }
}

variable "workload_rate" {
  description = "Request rate in req/s (RATE of the generated sample.env)"
  type        = number
  default     = 10

  validation {
    condition     = var.workload_rate > 0
    error_message = "workload_rate must be > 0."
  }
}

variable "workload_context_tokens" {
  description = "Maximum input sequence length of the trace in tokens (MAX_ISL of the generated sample.env; the --max-isl of workloads/w3-16k)"
  type        = number
  default     = 16384

  validation {
    condition     = var.workload_context_tokens > 0 && floor(var.workload_context_tokens) == var.workload_context_tokens
    error_message = "workload_context_tokens must be an integer > 0."
  }
}

variable "workload_sessions_per_rate" {
  description = "Sessions per unit of rate (SESSIONS_PER_RATE of the generated sample.env; sessions = rate x this, as in workloads/w3-16k)"
  type        = number
  default     = 75

  validation {
    condition     = var.workload_sessions_per_rate > 0 && floor(var.workload_sessions_per_rate) == var.workload_sessions_per_rate
    error_message = "workload_sessions_per_rate must be an integer > 0."
  }
}

# ----------------------------------------------------------------------------- buckets (owned by ../bootstrap)

variable "model_bucket_name" {
  description = "Model weights bucket created by ../bootstrap; null = <prefix>-models-<account>-<suffix> of cluster_name (the bootstrap default)"
  type        = string
  default     = null
}

variable "results_bucket_name" {
  description = "Benchmark results bucket created by ../bootstrap; null = <prefix>-results-<account>-<suffix> of cluster_name (the bootstrap default)"
  type        = string
  default     = null
}

# ----------------------------------------------------------------------------- add-ons and charts

variable "vpc_cni_addon_version" {
  description = "Pinned vpc-cni add-on version (an unpinned add-on follows most_recent and drifts the plan); review when changing kubernetes_version"
  type        = string
  default     = "v1.23.2-eksbuild.3"

  validation {
    condition     = can(regex("^v[0-9.]+-eksbuild\\.[0-9]+$", var.vpc_cni_addon_version))
    error_message = "vpc_cni_addon_version must look like v1.23.2-eksbuild.3."
  }
}

variable "kube_proxy_addon_version" {
  description = "Pinned kube-proxy add-on version (it must match kubernetes_version)"
  type        = string
  default     = "v1.35.3-eksbuild.49"

  validation {
    condition     = can(regex("^v[0-9.]+-eksbuild\\.[0-9]+$", var.kube_proxy_addon_version))
    error_message = "kube_proxy_addon_version must look like v1.35.3-eksbuild.49."
  }
}

variable "node_monitoring_agent_addon_version" {
  description = "Pinned eks-node-monitoring-agent add-on version (an upgrade rolls the agent on every node)"
  type        = string
  default     = "v1.7.2-eksbuild.1"

  validation {
    condition     = can(regex("^v[0-9.]+-eksbuild\\.[0-9]+$", var.node_monitoring_agent_addon_version))
    error_message = "node_monitoring_agent_addon_version must look like v1.7.2-eksbuild.1."
  }
}

variable "dynamo_version" {
  description = "NVIDIA Dynamo platform Helm chart version (NGC)"
  type        = string
  default     = "1.5.1"
}

variable "kube_prometheus_stack_version" {
  description = "kube-prometheus-stack Helm chart version"
  type        = string
  default     = "69.7.4"
}

variable "dcgm_exporter_version" {
  description = "dcgm-exporter Helm chart version"
  type        = string
  default     = "4.8.4"
}

variable "prometheus_retention" {
  description = "Prometheus TSDB retention"
  type        = string
  default     = "30d"
}

variable "prometheus_retention_size" {
  description = "Prometheus TSDB size cap (guard below the PVC size)"
  type        = string
  default     = "45GB"
}

variable "prometheus_scrape_interval" {
  description = "Prometheus global scrape/evaluation interval (the Dynamo operator PodMonitors set their own 5s)"
  type        = string
  default     = "15s"
}

variable "prometheus_storage_size" {
  description = "Prometheus PVC size (auto-ebs-sc gp3); do not shrink/resize an existing PVC from here"
  type        = string
  default     = "50Gi"
}

variable "ecr_untagged_expire_days" {
  description = "Days after push when UNTAGGED images expire in the project ECR repositories (tagged images never expire)"
  type        = number
  default     = 14

  validation {
    condition     = var.ecr_untagged_expire_days >= 1 && floor(var.ecr_untagged_expire_days) == var.ecr_untagged_expire_days
    error_message = "ecr_untagged_expire_days must be an integer >= 1."
  }
}

variable "create_runtime_mirror_repository" {
  description = "Create the <project_name>/sglang-runtime ECR repository for the optional digest-preserving copy of the runtime image (docs/DEPLOY.md, Mirror the image to your ECR). false = the workers pull the public NGC image of runtime_image, the output sglang_runtime_ecr_repository_url is null and SGLANG_RUNTIME_REPO of the generated env is empty"
  type        = bool
  default     = false
  nullable    = false
}

variable "image_mirror_source_region" {
  description = "Optional region of source <project_name>/bench and, with create_runtime_mirror_repository = true, <project_name>/sglang-runtime repositories (same account) the image mirror Job may read from; null = no cross-region read"
  type        = string
  default     = null

  validation {
    condition     = var.image_mirror_source_region == null ? true : can(regex("^[a-z]{2}(-[a-z]+)+-[0-9]+$", var.image_mirror_source_region))
    error_message = "image_mirror_source_region must be a region code (or null)."
  }
}

# ----------------------------------------------------------------------------- Grafana edge (optional)

variable "enable_grafana_edge" {
  description = "Publish Grafana through CloudFront (VPC origin) + AWS WAF. Default off: reach Grafana with kubectl port-forward. On, it also needs grafana_edge_allow_plain_http_origin = true"
  type        = bool
  default     = false

  # Cross-variable rule (Terraform >= 1.9): the CloudFront-to-NLB hop is plain HTTP, never a silent default.
  validation {
    condition     = !var.enable_grafana_edge || var.grafana_edge_allow_plain_http_origin
    error_message = "enable_grafana_edge = true sends Grafana traffic from CloudFront to the internal NLB over plain HTTP (listener grafana_edge_listener_port): the Grafana login and password, the session cookie and every API call and query result cross that hop unencrypted, on the private VPC origin path only (CloudFront VPC origin network interfaces in the private subnets to an internal NLB that admits only the CloudFront VPC origins security group). A CloudFront VPC origin does not accept a Network Load Balancer with a TLS listener, so this edge cannot encrypt that hop; TLS on it needs an internal Application Load Balancer origin with an ACM certificate for a domain you own, which this sample does not ship. Set grafana_edge_allow_plain_http_origin = true to accept the plain-HTTP hop, or keep the edge off and use kubectl port-forward. See terraform/README.md, section Grafana."
  }
}

variable "grafana_edge_allow_plain_http_origin" {
  description = "Acknowledge that the CloudFront-to-NLB hop of the Grafana edge is plain HTTP on the private VPC origin path (required with enable_grafana_edge = true; a VPC origin does not accept an NLB with a TLS listener)"
  type        = bool
  default     = false
}

variable "grafana_edge_listener_port" {
  description = "Plain-HTTP listener port of the internal NLB: the CloudFront VPC origin HTTP port (http-only), the Service port and the CloudFront security group rule"
  type        = number
  default     = 80
}

variable "grafana_edge_domain" {
  description = "Optional domain name you own for the Grafana edge (alternate domain name of the distribution, for example grafana.example.com), set together with grafana_edge_viewer_certificate_arn: viewers then get the TLSv1.2_2021 security policy over SNI. Empty = default *.cloudfront.net certificate, on which CloudFront forces the TLSv1 security policy"
  type        = string
  default     = ""
}

variable "grafana_edge_viewer_certificate_arn" {
  description = "Optional ARN of an issued ACM certificate in us-east-1 that covers grafana_edge_domain (CloudFront only uses ACM certificates from us-east-1), set together with grafana_edge_domain"
  type        = string
  default     = ""

  # Cross-variable rule (Terraform >= 1.9): the certificate only makes sense with the domain it covers.
  validation {
    condition     = (var.grafana_edge_viewer_certificate_arn == "") == (var.grafana_edge_domain == "")
    error_message = "Set grafana_edge_domain and grafana_edge_viewer_certificate_arn together (custom domain, TLSv1.2_2021 security policy over SNI) or leave both empty (default *.cloudfront.net certificate, on which CloudFront forces the TLSv1 security policy). See terraform/README.md, section Grafana."
  }

  validation {
    condition     = var.grafana_edge_viewer_certificate_arn == "" || can(regex("^arn:aws:acm:us-east-1:[^:]+:certificate/[A-Za-z0-9-]+$", var.grafana_edge_viewer_certificate_arn))
    error_message = "grafana_edge_viewer_certificate_arn must be the ARN of an ACM certificate in us-east-1 (arn:aws:acm:us-east-1:<account-id>:certificate/<id>): CloudFront only uses ACM certificates from US East (N. Virginia)."
  }
}

variable "grafana_edge_price_class" {
  description = "CloudFront price class for the Grafana distribution"
  type        = string
  default     = "PriceClass_100"

  validation {
    condition     = contains(["PriceClass_100", "PriceClass_200", "PriceClass_All"], var.grafana_edge_price_class)
    error_message = "grafana_edge_price_class must be PriceClass_100, PriceClass_200 or PriceClass_All."
  }
}

variable "grafana_edge_access_log_bucket" {
  description = "Optional S3 bucket name (ACLs enabled) for CloudFront standard access logs; empty disables logging"
  type        = string
  default     = ""
}

variable "grafana_waf_rate_limit_global" {
  description = "AWS WAF rate-based limit: max requests per source IP per 5-minute window, all paths"
  type        = number
  default     = 2000

  validation {
    condition     = var.grafana_waf_rate_limit_global >= 10
    error_message = "WAF rate-based limits must be >= 10."
  }
}

variable "grafana_waf_rate_limit_login" {
  description = "AWS WAF rate-based limit: max POST /login requests per source IP per 5-minute window"
  type        = number
  default     = 100

  validation {
    condition     = var.grafana_waf_rate_limit_login >= 10
    error_message = "WAF rate-based limits must be >= 10."
  }
}

# SizeRestrictions_BODY blocks bodies > 8 KB: saving a dashboard (POST
# /api/dashboards/db, ~12.8 KB) returned 403 with that terminating rule. Every
# other rule of the group stays in BLOCK.
variable "grafana_waf_common_rule_count_overrides" {
  description = "Rules of AWSManagedRulesCommonRuleSet switched to COUNT because they break legitimate Grafana traffic"
  type        = list(string)
  default     = ["SizeRestrictions_BODY"]
}
