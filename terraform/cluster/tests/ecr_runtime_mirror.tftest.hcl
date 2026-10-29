# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
#
# RED tests: the <project_name>/sglang-runtime ECR repository is optional (terraform/cluster/ecr.tf). The sample runs
# the public NGC image of runtime_image directly; the repository only serves the optional digest-preserving copy of
# docs/DEPLOY.md ("Mirror the image to your ECR"), so it exists only with create_runtime_mirror_repository = true.
# Providers are mocked (nothing touches AWS or a cluster); the overrides are those of ecr_kms.tftest.hcl, plus fixed
# repository URLs so the outputs are known at plan time. The account segment is not 12 digits.
#
# Contract: by default (false) neither the repository nor its lifecycle policy is planned, the bench repository, its
# lifecycle policy and the KMS key always are; the output sglang_runtime_ecr_repository_url is null, the
# SGLANG_RUNTIME_REPO key of platform_env is present and empty, and the image mirror reads no sglang-runtime repository
# of the source region. With true: one repository (KMS, immutable tags, scan on push, no force delete), one lifecycle
# policy on it, the output and SGLANG_RUNTIME_REPO are its URL, and the source-region read covers it again.

mock_provider "aws" {}
mock_provider "aws" {
  alias = "us_east_1"
}
mock_provider "kubernetes" {}
mock_provider "helm" {}
mock_provider "kubectl" {}

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
  target = data.aws_caller_identity.current
  values = {
    account_id = "000000000"
    arn        = "arn:aws:iam::000000000:role/deployer"
  }
}

override_data {
  target = data.aws_partition.current
  values = {
    partition  = "aws"
    dns_suffix = "amazonaws.com"
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

override_resource {
  target          = aws_kms_key.ecr
  override_during = plan
  values = {
    arn    = "arn:aws:kms:us-east-2:000000000:key/mock-ecr-key-id"
    key_id = "mock-ecr-key-id"
  }
}

# The repository URLs are computed: fixed at plan time so the outputs can be compared with them.
override_resource {
  target          = aws_ecr_repository.bench
  override_during = plan
  values = {
    repository_url = "000000000.dkr.ecr.us-east-2.amazonaws.com/dynamo-sample/bench"
  }
}

override_resource {
  target          = aws_ecr_repository.sglang_runtime
  override_during = plan
  values = {
    repository_url = "000000000.dkr.ecr.us-east-2.amazonaws.com/dynamo-sample/sglang-runtime"
  }
}

variables {
  aws_region                 = "us-east-2"
  cluster_name               = "demo-eks"
  eks_public_access_cidrs    = ["203.0.113.0/24"]
  gpu_instance_types         = ["p6-b200.48xlarge"]
  gpu_capacity_type          = "on-demand"
  capacity_reservation_ids   = []
  model_id                   = "acme/tiny-model"
  model_revision             = "0123456789abcdef0123456789abcdef01234567"
  model_bucket_name          = "demo-models-bucket"
  image_mirror_source_region = "us-west-2"
}

run "by_default_there_is_no_runtime_mirror_repository" {
  command = plan

  assert {
    condition     = length(aws_ecr_repository.sglang_runtime) == 0 && length(aws_ecr_lifecycle_policy.sglang_runtime) == 0
    error_message = "create_runtime_mirror_repository defaults to false: no sglang-runtime repository and no lifecycle policy for it"
  }
  assert {
    condition     = aws_ecr_repository.bench.name == "dynamo-sample/bench" && aws_ecr_lifecycle_policy.bench.repository == "dynamo-sample/bench"
    error_message = "the bench repository and its lifecycle policy are always created"
  }
  assert {
    condition     = aws_ecr_repository.bench.encryption_configuration[0].kms_key == "arn:aws:kms:us-east-2:000000000:key/mock-ecr-key-id" && aws_kms_alias.ecr.target_key_id == "mock-ecr-key-id"
    error_message = "the KMS key of the stack stays: the bench repository always exists and uses it"
  }
  assert {
    condition     = output.sglang_runtime_ecr_repository_url == null
    error_message = "without the repository the output sglang_runtime_ecr_repository_url is null"
  }
  assert {
    condition     = output.platform_env["SGLANG_RUNTIME_REPO"] == ""
    error_message = "SGLANG_RUNTIME_REPO stays a key of platform_env, with an empty value, when the repository is not created"
  }
  assert {
    condition     = output.platform_env["BENCH_REPO"] == "000000000.dkr.ecr.us-east-2.amazonaws.com/dynamo-sample/bench"
    error_message = "BENCH_REPO is the URL of the bench repository"
  }
  assert {
    condition = jsonencode(flatten([
      for s in jsondecode(aws_iam_policy.image_mirror.policy).Statement : s.Resource if s.Sid == "PullSourceRegionRepos"
    ])) == jsonencode(["arn:aws:ecr:us-west-2:000000000:repository/dynamo-sample/bench"])
    error_message = "without a runtime mirror repository the image mirror reads only the bench repository of the source region"
  }
}

run "the_runtime_mirror_repository_is_created_on_request" {
  command = plan

  variables {
    create_runtime_mirror_repository = true
  }

  assert {
    condition     = length(aws_ecr_repository.sglang_runtime) == 1 && length(aws_ecr_lifecycle_policy.sglang_runtime) == 1
    error_message = "create_runtime_mirror_repository = true: one sglang-runtime repository and one lifecycle policy"
  }
  assert {
    condition     = aws_ecr_repository.sglang_runtime[0].name == "dynamo-sample/sglang-runtime" && aws_ecr_lifecycle_policy.sglang_runtime[0].repository == "dynamo-sample/sglang-runtime"
    error_message = "the repository is <project_name>/sglang-runtime and the lifecycle policy is on it"
  }
  assert {
    condition = (
      aws_ecr_repository.sglang_runtime[0].encryption_configuration[0].encryption_type == "KMS" &&
      aws_ecr_repository.sglang_runtime[0].encryption_configuration[0].kms_key == "arn:aws:kms:us-east-2:000000000:key/mock-ecr-key-id" &&
      aws_ecr_repository.sglang_runtime[0].image_tag_mutability == "IMMUTABLE" &&
      aws_ecr_repository.sglang_runtime[0].image_scanning_configuration[0].scan_on_push &&
      !aws_ecr_repository.sglang_runtime[0].force_delete
    )
    error_message = "the repository keeps the contract of the bench one: the stack KMS key, immutable tags, scan on push, no force delete"
  }
  assert {
    condition     = aws_ecr_lifecycle_policy.sglang_runtime[0].policy == aws_ecr_lifecycle_policy.bench.policy
    error_message = "both repositories share the expire-untagged lifecycle policy"
  }
  assert {
    condition     = output.sglang_runtime_ecr_repository_url == "000000000.dkr.ecr.us-east-2.amazonaws.com/dynamo-sample/sglang-runtime"
    error_message = "the output is the URL of the repository"
  }
  assert {
    condition     = output.platform_env["SGLANG_RUNTIME_REPO"] == "000000000.dkr.ecr.us-east-2.amazonaws.com/dynamo-sample/sglang-runtime"
    error_message = "SGLANG_RUNTIME_REPO is the URL of the repository"
  }
  assert {
    condition = jsonencode(flatten([
      for s in jsondecode(aws_iam_policy.image_mirror.policy).Statement : s.Resource if s.Sid == "PullSourceRegionRepos"
      ])) == jsonencode([
      "arn:aws:ecr:us-west-2:000000000:repository/dynamo-sample/bench",
      "arn:aws:ecr:us-west-2:000000000:repository/dynamo-sample/sglang-runtime",
    ])
    error_message = "with the repository the image mirror may read the bench and sglang-runtime repositories of the source region"
  }
}
