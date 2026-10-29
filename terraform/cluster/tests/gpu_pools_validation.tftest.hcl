# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
#
# RED tests: every rejected input of the gpu-pools module fails on a NAMED VARIABLE validation (cross-variable
# validations need Terraform >= 1.9). A rejected input must produce no other error: the module has to stay
# evaluable (no index/lookup errors) when a validation fails.
# Contract and inputs: see the header of gpu_pools_grouping.tftest.hcl.

variables {
  capacity_type           = "on-demand"
  vpc_az_ids              = ["use2-az1", "use2-az2", "use2-az3"]
  az_name_by_id           = { use2-az1 = "us-east-2c", use2-az2 = "us-east-2a", use2-az3 = "us-east-2b" }
  private_subnet_by_az_id = { use2-az1 = "subnet-az1", use2-az2 = "subnet-az2", use2-az3 = "subnet-az3" }
  node_role_name          = "node-role-PLACEHOLDER"
  security_group_id       = "sg-PLACEHOLDER"
  project_name            = "dynamo-sample"
  instance_types          = { for k, v in jsondecode(file("tests/fixtures/instance-types.json")) : k => v if contains(["p6-b200.48xlarge"], k) }
  offerings               = { "p6-b200.48xlarge" = ["use2-az1"] }
}

run "empty_instance_types_rejected" {
  command = plan
  module {
    source = "./modules/gpu-pools"
  }
  variables {
    instance_types = {}
    offerings      = {}
  }
  expect_failures = [var.instance_types]
}

run "non_gpu_instance_type_rejected" {
  command = plan
  module {
    source = "./modules/gpu-pools"
  }
  variables {
    instance_types = { for k, v in jsondecode(file("tests/fixtures/instance-types.json")) : k => v if contains(["c7i.large"], k) }
    offerings      = { "c7i.large" = ["use2-az1"] }
  }
  expect_failures = [var.instance_types]
}

run "arm64_gpu_instance_type_rejected" {
  command = plan
  module {
    source = "./modules/gpu-pools"
  }
  variables {
    instance_types = { for k, v in jsondecode(file("tests/fixtures/instance-types.json")) : k => v if contains(["g5g.xlarge"], k) }
    offerings      = { "g5g.xlarge" = ["use2-az1"] }
  }
  expect_failures = [var.instance_types]
}

# SYNTHETIC (no real AMD type available in the capture regions): a non-NVIDIA accelerator must not get an NVIDIA label.
run "non_nvidia_gpu_rejected" {
  command = plan
  module {
    source = "./modules/gpu-pools"
  }
  variables {
    instance_types = {
      "g4ad.4xlarge" = merge(jsondecode(file("tests/fixtures/instance-types.json"))["g5.xlarge"], {
        gpus = [{ name = "Radeon Pro V520", count = 1, memory_size = 8192, manufacturer = "AMD" }]
      })
    }
    offerings = { "g4ad.4xlarge" = ["use2-az1"] }
  }
  expect_failures = [var.instance_types]
}

# SYNTHETIC: more network cards than EFA interfaces + 1 has no verified layout rule.
run "unsupported_card_efa_ratio_rejected" {
  command = plan
  module {
    source = "./modules/gpu-pools"
  }
  variables {
    instance_types = {
      "p9.synthetic" = merge(jsondecode(file("tests/fixtures/instance-types.json"))["p6-b200.48xlarge"], { maximum_network_cards = 12 })
    }
    offerings = { "p9.synthetic" = ["use2-az1"] }
  }
  expect_failures = [var.instance_types]
}

run "unknown_capacity_type_rejected" {
  command = plan
  module {
    source = "./modules/gpu-pools"
  }
  variables {
    capacity_type = "reserved"
  }
  expect_failures = [var.capacity_type]
}

run "capacity_block_requires_reservation_ids" {
  command = plan
  module {
    source = "./modules/gpu-pools"
  }
  variables {
    capacity_type   = "capacity-block"
    reservation_ids = []
    gpu_az_ids      = ["use2-az1"]
  }
  expect_failures = [var.reservation_ids]
}

run "odcr_requires_reservation_ids" {
  command = plan
  module {
    source = "./modules/gpu-pools"
  }
  variables {
    capacity_type   = "odcr"
    reservation_ids = []
    gpu_az_ids      = ["use2-az1"]
    gpu_max_nodes   = 1
  }
  expect_failures = [var.reservation_ids]
}

run "on_demand_rejects_reservation_ids" {
  command = plan
  module {
    source = "./modules/gpu-pools"
  }
  variables {
    capacity_type   = "on-demand"
    reservation_ids = ["cr-00000000000000001"]
  }
  expect_failures = [var.reservation_ids]
}

run "spot_rejects_reservation_ids" {
  command = plan
  module {
    source = "./modules/gpu-pools"
  }
  variables {
    capacity_type   = "spot"
    reservation_ids = ["cr-00000000000000001"]
  }
  expect_failures = [var.reservation_ids]
}

run "malformed_reservation_id_rejected" {
  command = plan
  module {
    source = "./modules/gpu-pools"
  }
  variables {
    capacity_type   = "odcr"
    reservation_ids = ["rsv-123"]
    gpu_az_ids      = ["use2-az1"]
    gpu_max_nodes   = 1
  }
  expect_failures = [var.reservation_ids]
}

run "odcr_requires_gpu_az_ids" {
  command = plan
  module {
    source = "./modules/gpu-pools"
  }
  variables {
    capacity_type   = "odcr"
    reservation_ids = ["cr-00000000000000001"]
    gpu_az_ids      = []
    gpu_max_nodes   = 1
  }
  expect_failures = [var.gpu_az_ids]
}

run "odcr_requires_gpu_max_nodes" {
  command = plan
  module {
    source = "./modules/gpu-pools"
  }
  variables {
    capacity_type   = "odcr"
    reservation_ids = ["cr-00000000000000001"]
    gpu_az_ids      = ["use2-az1"]
    gpu_max_nodes   = null
  }
  expect_failures = [var.gpu_max_nodes]
}

run "gpu_max_nodes_must_be_positive" {
  command = plan
  module {
    source = "./modules/gpu-pools"
  }
  variables {
    gpu_max_nodes = 0
  }
  expect_failures = [var.gpu_max_nodes]
}

run "efa_value_rejected" {
  command = plan
  module {
    source = "./modules/gpu-pools"
  }
  variables {
    efa = "true"
  }
  expect_failures = [var.efa]
}
