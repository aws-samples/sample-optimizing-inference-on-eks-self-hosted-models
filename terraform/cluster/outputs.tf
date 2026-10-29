# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

################################################################################
# Outputs (no sensitive values)
#
# platform_env, sample_env and gpu_pools feed scripts/gen_env.py, which writes
# the env files the manifests and scripts read (terraform/README.md).
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

  # SGLANG_RUNTIME_REPO stays a key without the optional sglang-runtime
  # repository (create_runtime_mirror_repository = false), with an empty value.
  platform_env = merge({
    AWS_ACCOUNT_ID       = local.account_id
    AWS_REGION           = var.aws_region
    CLUSTER_NAME         = local.cluster_name
    MODEL_BUCKET         = local.model_bucket_name
    RESULTS_BUCKET       = local.results_bucket_name
    ECR_REGISTRY         = local.ecr_registry
    SGLANG_RUNTIME_REPO  = var.create_runtime_mirror_repository ? aws_ecr_repository.sglang_runtime[0].repository_url : ""
    BENCH_REPO           = aws_ecr_repository.bench.repository_url
    GPU_CAPACITY_TYPE    = var.gpu_capacity_type
    GPU_POOLS            = join(",", [for name, p in module.gpu_pools.pools : "${name}:${p.gpus_per_node}:${p.efa_interfaces}"])
    PRIMARY_AZ           = local.primary_az
    GRAFANA_EDGE_ENABLED = tostring(var.enable_grafana_edge)
  }, local.single_gpu_pool)

  sample_env = merge({
    MODEL_ID          = var.model_id
    MODEL_REVISION    = var.model_revision
    MODEL_S3_PREFIX   = local.model_s3_prefix
    RUNTIME_IMAGE     = var.runtime_image
    RATE              = tostring(var.workload_rate)
    MAX_ISL           = tostring(var.workload_context_tokens)
    SESSIONS_PER_RATE = tostring(var.workload_sessions_per_rate)
  }, var.hf_token_secret_arn == null ? {} : { HF_TOKEN_SECRET_ARN = var.hf_token_secret_arn })
}

output "platform_env" {
  description = "KEY=VALUE map of the platform env (account, region, cluster, buckets, ECR, GPU shape); input of scripts/gen_env.py"
  value       = local.platform_env
}

output "sample_env" {
  description = "KEY=VALUE map of the model and workload settings; input of scripts/gen_env.py"
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

output "bench_ecr_repository_url" {
  description = "ECR repository URL for the bench image"
  value       = aws_ecr_repository.bench.repository_url
}

output "sglang_runtime_ecr_repository_url" {
  description = "ECR repository URL of the optional sglang-runtime mirror; null (not in terraform output) unless create_runtime_mirror_repository = true"
  value       = one(aws_ecr_repository.sglang_runtime[*].repository_url)
}

output "image_mirror_role_arn" {
  description = "Pod Identity role for node-ops/image-mirror (ECR push to the project repos)"
  value       = aws_iam_role.image_mirror.arn
}

output "results_bucket_name" {
  description = "Benchmark results bucket (owned by the bootstrap stack); RESULTS_BUCKET"
  value       = local.results_bucket_name
}

output "results_writer_role_arn" {
  description = "Pod Identity role for bench/matrix-driver and bench/bench (results bucket write, no delete)"
  value       = aws_iam_role.results_writer.arn
}

output "results_writer_association_ids" {
  description = "Pod Identity association IDs keyed by bench ServiceAccount"
  value       = { for sa, a in aws_eks_pod_identity_association.results_writer : sa => a.association_id }
}

output "vpc_az_ids" {
  description = "AZ IDs spanned by the VPC / EKS subnets (ordered; subnet CIDRs follow this order)"
  value       = local.vpc_az_ids
}

output "primary_az" {
  description = "Primary AZ (name and ID) for cpu-demo, frontends and bench"
  value       = { name = local.primary_az, id = local.primary_az_id }
}
