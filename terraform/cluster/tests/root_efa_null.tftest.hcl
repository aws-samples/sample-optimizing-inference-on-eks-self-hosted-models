# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
#
# Regression (root module): data.aws_ec2_instance_type returns efa_maximum_interfaces = null when
# efa_supported = false. The whole root plan must work for non-EFA-only and mixed gpu_instance_types
# (Terraform < 1.12 evaluated the gpu-pools EFA validation without || short-circuit and failed with
# "argument must not be null"). Providers/modules are mocked exactly as in root_validation.tftest.hcl.

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



variables {
  aws_region               = "us-east-2"
  cluster_name             = "demo-eks"
  eks_public_access_cidrs  = ["203.0.113.0/24"]
  gpu_capacity_type        = "on-demand"
  capacity_reservation_ids = []
  model_id                 = "Qwen/Qwen3.8-27B-FP8"
  model_revision           = "0123456789abcdef0123456789abcdef01234567"
}

run "non_efa_only_g6e_root_plans" {
  command = plan
  # g6e.xlarge has a 250 GB instance store (< the 256Gi default ephemeral size): gpu-nodepool.tf warns that it
  # uses EBS. terraform test fails a run on any unexpected check failure, so the warning is declared (and so
  # required); the asserts below are still evaluated.
  expect_failures = [check.gpu_ephemeral_storage_on_nvme]
  variables {
    gpu_instance_types = ["g6e.xlarge"]
  }
  override_data {
    target = data.aws_ec2_instance_type.gpu
    values = {
      maximum_network_cards   = 1
      efa_supported           = false
      efa_maximum_interfaces  = null
      default_vcpus           = 4
      memory_size             = 32768
      total_instance_storage  = 250
      supported_architectures = ["x86_64"]
      gpus                    = [{ name = "L40S", count = 1, memory_size = 46068, manufacturer = "NVIDIA" }]
    }
  }
  override_data {
    target = data.aws_ec2_instance_type_offerings.gpu
    values = {
      instance_types = ["g6e.xlarge"]
      locations      = ["use2-az1"]
    }
  }
  assert {
    condition     = length(module.gpu_pools.pools) == 1 && one(values(module.gpu_pools.pools)).efa_interfaces == 0
    error_message = "g6e.xlarge only: one pool, 0 EFA interfaces"
  }
}

run "mixed_efa_and_non_efa_root_plans" {
  command = plan
  # g6e.xlarge has a 250 GB instance store (< the 256Gi default ephemeral size): gpu-nodepool.tf warns that it
  # uses EBS. terraform test fails a run on any unexpected check failure, so the warning is declared (and so
  # required); the asserts below are still evaluated.
  expect_failures = [check.gpu_ephemeral_storage_on_nvme]
  variables {
    gpu_instance_types = ["p6-b200.48xlarge", "g6e.xlarge"]
  }
  override_data {
    target = data.aws_ec2_instance_type.gpu["p6-b200.48xlarge"]
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
    target = data.aws_ec2_instance_type.gpu["g6e.xlarge"]
    values = {
      maximum_network_cards   = 1
      efa_supported           = false
      efa_maximum_interfaces  = null
      default_vcpus           = 4
      memory_size             = 32768
      total_instance_storage  = 250
      supported_architectures = ["x86_64"]
      gpus                    = [{ name = "L40S", count = 1, memory_size = 46068, manufacturer = "NVIDIA" }]
    }
  }
  override_data {
    target = data.aws_ec2_instance_type_offerings.gpu
    values = {
      instance_types = ["p6-b200.48xlarge", "g6e.xlarge"]
      locations      = ["use2-az1", "use2-az1"]
    }
  }
  assert {
    condition     = length(module.gpu_pools.pools) == 2
    error_message = "mixed list: two pools (b200 with EFA, g6e without)"
  }
}
