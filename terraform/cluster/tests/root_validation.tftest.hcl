# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
#
# RED tests: validations of the ROOT module variables (terraform/cluster/variables.tf). Providers are mocked
# (mock_provider) so nothing touches AWS or a cluster; each run breaks exactly ONE input of an otherwise valid
# configuration and expects the failure on that variable. Only variable validations are exercised here: the
# GPU logic is tested on the pure module (gpu_pools_*.tftest.hcl) and the wiring is asserted statically by
# tests/terraform/test_cluster_static.py.
#
# Positive complement of the open-API rule (allow_open_eks_api = true accepts an open CIDR) is asserted
# statically in tests/terraform/test_cluster_static.py because a full root plan on mocks is brittle.

mock_provider "aws" {}
# grafana-edge.tf configures a second, aliased aws provider (us-east-1, for the CLOUDFRONT web ACL): it is
# mocked too, so `terraform test` needs no AWS credentials.
mock_provider "aws" {
  alias = "us_east_1"
}
mock_provider "kubernetes" {}
mock_provider "helm" {}
mock_provider "kubectl" {}

# Scoping only (no assert is changed): a run with expect_failures still plans every node that does NOT depend
# on the broken variable, and bare mock_provider values are not valid inputs (empty aws_ec2_instance_type facts
# are rejected by modules/gpu-pools, empty aws_availability_zones break the region code, the random caller ARN
# is rejected by module.eks). These overrides supply p6-b200 facts and stub the two registry modules, so only
# the variable validation under test can fail. The ARN account segment is deliberately not 12 digits.
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

run "open_eks_api_rejected" {
  command = plan
  variables {
    eks_public_access_cidrs = ["0.0.0.0/0"]
  }
  expect_failures = [var.eks_public_access_cidrs]
}

run "open_ipv6_eks_api_rejected" {
  command = plan
  variables {
    eks_public_access_cidrs = ["::/0"]
  }
  expect_failures = [var.eks_public_access_cidrs]
}

run "zero_prefix_eks_api_rejected_even_when_not_all_zero" {
  command = plan
  variables {
    eks_public_access_cidrs = ["10.0.0.0/0"]
  }
  expect_failures = [var.eks_public_access_cidrs]
}

run "empty_eks_cidrs_rejected" {
  command = plan
  variables {
    eks_public_access_cidrs = []
  }
  expect_failures = [var.eks_public_access_cidrs]
}

run "malformed_eks_cidr_rejected" {
  command = plan
  variables {
    eks_public_access_cidrs = ["not-a-cidr"]
  }
  expect_failures = [var.eks_public_access_cidrs]
}

run "empty_gpu_instance_types_rejected" {
  command = plan
  variables {
    gpu_instance_types = []
  }
  expect_failures = [var.gpu_instance_types]
}

run "non_gpu_family_rejected" {
  command = plan
  variables {
    gpu_instance_types = ["c7i.large"]
  }
  expect_failures = [var.gpu_instance_types]
}

run "duplicate_gpu_instance_types_rejected" {
  command = plan
  variables {
    gpu_instance_types = ["p6-b200.48xlarge", "p6-b200.48xlarge"]
  }
  expect_failures = [var.gpu_instance_types]
}

run "unknown_gpu_capacity_type_rejected" {
  command = plan
  variables {
    gpu_capacity_type = "reserved"
  }
  expect_failures = [var.gpu_capacity_type]
}

run "capacity_block_without_reservation_ids_rejected" {
  command = plan
  variables {
    gpu_capacity_type        = "capacity-block"
    capacity_reservation_ids = []
  }
  expect_failures = [var.capacity_reservation_ids]
}

run "odcr_without_reservation_ids_rejected" {
  command = plan
  variables {
    gpu_capacity_type        = "odcr"
    capacity_reservation_ids = []
    gpu_az_ids               = ["use2-az1"]
    gpu_max_nodes            = 1
  }
  expect_failures = [var.capacity_reservation_ids]
}

run "on_demand_with_reservation_ids_rejected" {
  command = plan
  variables {
    gpu_capacity_type        = "on-demand"
    capacity_reservation_ids = ["cr-00000000000000001"]
  }
  expect_failures = [var.capacity_reservation_ids]
}

run "model_revision_must_be_a_commit_sha" {
  command = plan
  variables {
    model_revision = "main"
  }
  expect_failures = [var.model_revision]
}

run "model_id_must_be_org_slash_name" {
  command = plan
  variables {
    model_id = "not-a-repo-id"
  }
  expect_failures = [var.model_id]
}

run "runtime_image_must_be_digest_pinned" {
  command = plan
  variables {
    runtime_image = "123.dkr.ecr.us-east-2.amazonaws.com/repo:latest"
  }
  expect_failures = [var.runtime_image]
}

run "hf_token_must_be_a_secrets_manager_arn" {
  command = plan
  variables {
    hf_token_secret_arn = "hf_notanarn"
  }
  expect_failures = [var.hf_token_secret_arn]
}

run "hf_token_ssm_arn_rejected" {
  command = plan
  variables {
    hf_token_secret_arn = "arn:aws:ssm:us-east-2::parameter/hf"
  }
  expect_failures = [var.hf_token_secret_arn]
}

run "aws_region_format_rejected" {
  command = plan
  variables {
    aws_region = "ohio"
  }
  expect_failures = [var.aws_region]
}

run "workload_values_must_be_positive" {
  command = plan
  variables {
    workload_rate = 0
  }
  expect_failures = [var.workload_rate]
}

# var.tags reaches the KMS key and the IAM roles through default_tags: KMS and IAM reject any character outside
# letters, digits, spaces and _ . : / = + - @ at apply, so the plan refuses it (key and value). The safe complement
# plans in terraform/bootstrap/tests/tags_validation.tftest.hcl, which uses the same condition.
run "tag_value_with_a_star_rejected" {
  command = plan
  variables {
    tags = { Purpose = "dynamo-sample/*" }
  }
  expect_failures = [var.tags]
}

run "tag_key_with_a_comma_rejected" {
  command = plan
  variables {
    tags = { "cost,center" = "ml" }
  }
  expect_failures = [var.tags]
}
