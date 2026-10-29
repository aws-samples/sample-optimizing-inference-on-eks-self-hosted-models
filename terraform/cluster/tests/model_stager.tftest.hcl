# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
#
# RED tests: the least-privilege Pod Identity role of the model staging Job (node-ops/model-stager,
# terraform/cluster/iam-model-access.tf). Providers are mocked (nothing touches AWS or a cluster); the overrides are the
# ones of root_efa_null.tftest.hcl so the plan reaches the IAM resources.
#
# Contract: aws_iam_role.model_stager, aws_iam_policy.model_stager (+ attachment), kubernetes_service_account_v1.model_stager
# (node-ops/model-stager) and aws_eks_pod_identity_association.model_stager. The object prefix is
# local.model_s3_prefix = coalesce(var.model_s3_prefix, "<model_id>/<model_revision>") (default null), the same value
# scripts/gen_env.py writes as MODEL_S3_PREFIX. Writes are allowed ONLY under that prefix of the models bucket, the listing only
# for that prefix; nothing else, no "*" action or resource, no Secrets Manager (the token travels in a Kubernetes Secret; the
# optional var.hf_token_secret_arn stays on the read-only prefetch role).

mock_provider "aws" {}
# grafana-edge.tf configures a second, aliased aws provider (us-east-1, for the CLOUDFRONT web ACL): it is
# mocked too, so `terraform test` needs no AWS credentials.
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
  model_id                 = "acme/tiny-model"
  model_revision           = "0123456789abcdef0123456789abcdef01234567"
  model_bucket_name        = "demo-models-bucket"
}

run "stager_identity_is_the_node_ops_model_stager_service_account" {
  command = plan

  assert {
    condition     = kubernetes_service_account_v1.model_stager.metadata[0].name == "model-stager" && kubernetes_service_account_v1.model_stager.metadata[0].namespace == "node-ops"
    error_message = "ServiceAccount node-ops/model-stager (the Job's serviceAccountName)"
  }
  # role_arn is unknown at plan time: the binding to aws_iam_role.model_stager (not model_reader) is asserted statically by
  # tests/terraform/test_model_stager_static.py
  assert {
    condition     = aws_eks_pod_identity_association.model_stager.service_account == "model-stager" && aws_eks_pod_identity_association.model_stager.namespace == "node-ops"
    error_message = "the Pod Identity association binds node-ops/model-stager"
  }
}

run "trust_policy_admits_only_that_service_account" {
  command = plan

  assert {
    condition     = jsondecode(aws_iam_role.model_stager.assume_role_policy).Statement[0].Principal.Service == "pods.eks.amazonaws.com"
    error_message = "only EKS Pod Identity may assume the role"
  }
  assert {
    condition = (
      jsondecode(aws_iam_role.model_stager.assume_role_policy).Statement[0].Condition.StringEquals["aws:RequestTag/kubernetes-namespace"] == "node-ops" &&
      jsondecode(aws_iam_role.model_stager.assume_role_policy).Statement[0].Condition.StringEquals["aws:RequestTag/kubernetes-service-account"] == "model-stager"
    )
    error_message = "the session tags must pin namespace node-ops and ServiceAccount model-stager"
  }
}

run "policy_is_scoped_to_the_models_bucket_and_the_model_prefix" {
  command = plan

  # no "*" action anywhere, only Allow
  assert {
    condition     = alltrue([for s in jsondecode(aws_iam_policy.model_stager.policy).Statement : s.Effect == "Allow" && alltrue([for a in flatten([s.Action]) : !strcontains(a, "*")])])
    error_message = "no wildcard actions and no Deny/NotAction tricks"
  }
  assert {
    condition     = alltrue([for a in flatten([for s in jsondecode(aws_iam_policy.model_stager.policy).Statement : s.Action]) : startswith(a, "s3:")])
    error_message = "S3 actions only (no Secrets Manager, KMS, EC2, ...)"
  }
  assert {
    condition     = !strcontains(aws_iam_policy.model_stager.policy, "secretsmanager")
    error_message = "the token never goes through Secrets Manager for the stager"
  }
  # the three write actions the Job needs are present
  assert {
    condition     = toset(["s3:PutObject", "s3:AbortMultipartUpload", "s3:ListBucket"]) == setintersection(toset(["s3:PutObject", "s3:AbortMultipartUpload", "s3:ListBucket"]), toset(flatten([for s in jsondecode(aws_iam_policy.model_stager.policy).Statement : s.Action])))
    error_message = "s3:PutObject, s3:AbortMultipartUpload and s3:ListBucket are required"
  }
  # nothing that deletes, reads other buckets' policies, or changes ACLs/policies
  assert {
    condition = length(setintersection(
      toset(flatten([for s in jsondecode(aws_iam_policy.model_stager.policy).Statement : s.Action])),
      toset(["s3:DeleteObject", "s3:DeleteObjectVersion", "s3:PutBucketPolicy", "s3:PutObjectAcl", "s3:PutBucketAcl", "s3:DeleteBucket", "s3:PutLifecycleConfiguration", "s3:ListAllMyBuckets", "s3:GetBucketPolicy"])
    )) == 0
    error_message = "no delete, ACL, policy or account-wide S3 action"
  }
  # object actions: resources are objects UNDER the model prefix of the models bucket and nothing else
  assert {
    condition = alltrue([
      for s in jsondecode(aws_iam_policy.model_stager.policy).Statement :
      alltrue([for r in flatten([s.Resource]) : endswith(r, ":::demo-models-bucket/acme/tiny-model/${var.model_revision}/*")])
      if length(setintersection(toset(flatten([s.Action])), toset(["s3:PutObject", "s3:AbortMultipartUpload", "s3:GetObject", "s3:ListMultipartUploadParts"]))) > 0
    ])
    error_message = "object-level actions are allowed only on <bucket>/<model_id>/<model_revision>/*"
  }
  # bucket-level actions: the models bucket only, and the listing is restricted to the prefix by a condition
  assert {
    condition = alltrue([
      for s in jsondecode(aws_iam_policy.model_stager.policy).Statement :
      alltrue([for r in flatten([s.Resource]) : endswith(r, ":::demo-models-bucket")]) && contains(keys(s), "Condition") &&
      alltrue([for v in flatten([values(lookup(s.Condition, "StringLike", lookup(s.Condition, "StringEquals", { "s3:prefix" = [] })))]) : startswith(v, "acme/tiny-model/${var.model_revision}")])
      if contains(flatten([s.Action]), "s3:ListBucket")
    ])
    error_message = "s3:ListBucket is allowed on the models bucket only, conditioned on s3:prefix under <model_id>/<model_revision>"
  }
  assert {
    condition     = length([for s in jsondecode(aws_iam_policy.model_stager.policy).Statement : s if contains(flatten([s.Action]), "s3:ListBucket")]) == 1
    error_message = "exactly one ListBucket statement"
  }
}

run "an_explicit_model_s3_prefix_moves_the_scope_with_it" {
  command = plan

  variables {
    model_s3_prefix = "Qwen3.8-27B-FP8"
  }

  assert {
    condition = alltrue([
      for s in jsondecode(aws_iam_policy.model_stager.policy).Statement :
      alltrue([for r in flatten([s.Resource]) : endswith(r, ":::demo-models-bucket/Qwen3.8-27B-FP8/*")])
      if contains(flatten([s.Action]), "s3:PutObject")
    ])
    error_message = "var.model_s3_prefix overrides <model_id>/<model_revision> in the object scope"
  }
  assert {
    condition     = !strcontains(aws_iam_policy.model_stager.policy, "acme/tiny-model")
    error_message = "the default prefix must not leak into the policy when an explicit prefix is set"
  }
}

run "the_optional_hf_token_secret_never_reaches_the_stager" {
  command = plan

  variables {
    hf_token_secret_arn = "arn:aws:secretsmanager:us-east-2:000000000:secret:hf-token-AbCdEf"
  }

  assert {
    condition     = !strcontains(aws_iam_policy.model_stager.policy, "secretsmanager") && !strcontains(aws_iam_policy.model_stager.policy, "hf-token")
    error_message = "var.hf_token_secret_arn is the model-prefetch role's option; the stager reads HF_TOKEN from the Kubernetes Secret"
  }
}

run "the_read_only_model_reader_role_is_not_widened" {
  command = plan

  assert {
    condition     = !strcontains(aws_iam_policy.model_reader.policy, "s3:Put") && !strcontains(aws_iam_policy.model_reader.policy, "s3:Abort") && !strcontains(aws_iam_policy.model_reader.policy, "s3:Delete")
    error_message = "model-reader stays read-only"
  }
}

run "an_unsafe_model_s3_prefix_is_rejected" {
  command = plan

  variables {
    model_s3_prefix = "../other"
  }

  expect_failures = [var.model_s3_prefix]
}

run "a_prefix_with_a_wildcard_is_rejected" {
  command = plan

  variables {
    model_s3_prefix = "models/*"
  }

  expect_failures = [var.model_s3_prefix]
}
