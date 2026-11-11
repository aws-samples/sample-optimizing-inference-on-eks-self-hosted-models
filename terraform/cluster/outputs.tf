# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

################################################################################
# Outputs (no sensitive values)
#
# platform_env, sample_env and gpu_pools feed scripts/gen_env.py, which writes
# manifests/config/platform.env, the env file the manifests and scripts read
# (terraform/README.md).
################################################################################

locals {
  # The single-value GPU keys exist only with exactly one pool; with several
  # pools they would depend on the order of gpu_instance_types (GPU_POOLS
  # lists every pool instead).
  single_gpu_pool = length(module.gpu_pools.pools) == 1 ? {
    GPU_NODEPOOL            = keys(module.gpu_pools.pools)[0]
    GPU_INSTANCE_TYPES      = join(",", values(module.gpu_pools.pools)[0].instance_types)
    GPU_PRODUCT             = values(module.gpu_pools.pools)[0].gpu_product
    GPUS_PER_NODE           = tostring(values(module.gpu_pools.pools)[0].gpus_per_node)
    EFA_INTERFACES_PER_NODE = tostring(values(module.gpu_pools.pools)[0].efa_interfaces)
    GPU_MEMORY_GIB_PER_GPU  = tostring(floor(values(module.gpu_pools.pools)[0].gpu_memory_mib / 1024))
    HOST_MEMORY_GIB         = tostring(values(module.gpu_pools.pools)[0].host_memory_gib)
    EPHEMERAL_MIN_GIB       = tostring(values(module.gpu_pools.pools)[0].ephemeral_storage_min_gib)
    GPU_AZ                  = join(",", module.gpu_pools.azs)
  } : {}

  platform_env = merge({
    AWS_ACCOUNT_ID       = local.account_id
    AWS_REGION           = var.aws_region
    CLUSTER_NAME         = local.cluster_name
    MODEL_BUCKET         = local.model_bucket_name
    RESULTS_BUCKET       = local.results_bucket_name
    GPU_CAPACITY_TYPE    = var.gpu_capacity_type
    GPU_POOLS            = join(",", [for name, p in module.gpu_pools.pools : "${name}:${p.gpus_per_node}:${p.efa_interfaces}"])
    PRIMARY_AZ           = local.primary_az
    GRAFANA_EDGE_ENABLED = tostring(var.enable_grafana_edge)
  }, local.single_gpu_pool)

  sample_env = merge({
    MODEL_ID        = var.model_id
    MODEL_REVISION  = var.model_revision
    MODEL_S3_PREFIX = local.model_s3_prefix
    RUNTIME_IMAGE   = var.runtime_image
  }, var.hf_token_secret_arn == null ? {} : { HF_TOKEN_SECRET_ARN = var.hf_token_secret_arn })
}

output "platform_env" {
  description = "KEY=VALUE map of the platform env (account, region, cluster, buckets, GPU shape); input of scripts/gen_env.py"
  value       = local.platform_env
}

output "sample_env" {
  description = "KEY=VALUE map of the model and the runtime image; input of scripts/gen_env.py"
  value       = local.sample_env
}

output "gpu_pools" {
  description = "GPU pools by name: member types, GPU product/count/memory, EFA interfaces, max_nodes (the NodePool limit), ephemeral_backing, AZs"
  value       = module.gpu_pools.pools
}

output "gpu_az" {
  description = "AZ IDs and names of the GPU nodes"
  value       = { ids = module.gpu_pools.az_ids, names = module.gpu_pools.azs }
}

output "cluster_name" {
  description = "EKS cluster name"
  value       = module.eks.cluster_name
}

output "region" {
  description = "AWS region"
  value       = var.aws_region
}

output "cluster_endpoint" {
  description = "EKS API endpoint"
  value       = module.eks.cluster_endpoint
}

output "cluster_arn" {
  description = "EKS cluster ARN"
  value       = module.eks.cluster_arn
}

output "configure_kubectl" {
  description = "Command to configure kubectl for this cluster"
  value       = "aws eks update-kubeconfig --name ${module.eks.cluster_name} --region ${var.aws_region} --alias ${module.eks.cluster_name}"
}

output "vpc_id" {
  description = "VPC ID"
  value       = module.vpc.vpc_id
}

output "private_subnets_by_az" {
  description = "Private subnet IDs keyed by AZ name"
  value       = zipmap(module.vpc.azs, module.vpc.private_subnets)
}

output "s3_vpc_endpoint_id" {
  description = "S3 gateway VPC endpoint ID"
  value       = aws_vpc_endpoint.s3.id
}

output "node_iam_role_name" {
  description = "Auto Mode node IAM role used by the custom NodeClasses"
  value       = module.eks.node_iam_role_name
}

output "namespaces" {
  description = "Namespaces and their PSA enforce level"
  value       = { for ns, profile in local.namespaces : ns => local.psa_profiles[profile].enforce }
}

output "model_bucket_name" {
  description = "Model bucket (owned by the bootstrap stack); MODEL_BUCKET"
  value       = local.model_bucket_name
}

output "model_reader_role_arn" {
  description = "Pod Identity role for node-ops/model-prefetch"
  value       = aws_iam_role.model_reader.arn
}

output "results_bucket_name" {
  description = "Results bucket (owned by the bootstrap stack); RESULTS_BUCKET"
  value       = local.results_bucket_name
}

output "vpc_az_ids" {
  description = "AZ IDs spanned by the VPC / EKS subnets (ordered; subnet CIDRs follow this order)"
  value       = local.vpc_az_ids
}

output "primary_az" {
  description = "Primary AZ (name and ID) of the cpu-demo pool"
  value       = { name = local.primary_az, id = local.primary_az_id }
}
