# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

################################################################################
# Inputs of the pure gpu-pools module: plain values in, manifests out.
#
# The root module reads the EC2 facts (data.aws_ec2_instance_type,
# data.aws_ec2_instance_type_offerings, data.aws_ec2_capacity_block_reservation)
# and passes them here, so the grouping, layout, AZ and limits logic is
# testable with `terraform test` and plain variables (tests/gpu_pools_*.tftest.hcl).
#
# Every rejected input fails on a validation of the variable that carries it
# (cross-variable rules need Terraform >= 1.9). HCL `&&` / `||` short-circuit
# only from Terraform 1.12, so the conditions below never index into a value
# they also test, and coalesce() the EC2 facts the data source leaves null
# (efa_maximum_interfaces on a type without EFA).
################################################################################

variable "instance_types" {
  description = "EC2 facts of every requested GPU instance type, keyed by type (attribute names of the aws_ec2_instance_type data source; memory_size in MiB, total_instance_storage in GB)"
  type = map(object({
    maximum_network_cards   = number
    efa_supported           = bool
    efa_maximum_interfaces  = number
    default_vcpus           = number
    memory_size             = number
    total_instance_storage  = number
    supported_architectures = list(string)
    gpus = list(object({
      name         = string
      count        = number
      memory_size  = number
      manufacturer = string
    }))
  }))

  validation {
    condition     = length(var.instance_types) > 0
    error_message = "At least one GPU instance type is required."
  }

  validation {
    condition = alltrue([
      for t, v in var.instance_types : length(v.gpus) > 0 && alltrue([for g in v.gpus : g.manufacturer == "NVIDIA"])
    ])
    error_message = "Every instance type must carry NVIDIA GPUs: the pools set NVIDIA labels and request nvidia.com/gpu (CPU-only and non-NVIDIA accelerator types are not supported)."
  }

  validation {
    condition     = alltrue([for t, v in var.instance_types : contains(v.supported_architectures, "x86_64")])
    error_message = "Every instance type must support x86_64: the GPU NodePools require kubernetes.io/arch = amd64 (Graviton GPU types such as g5g are not supported)."
  }

  validation {
    # Static EFA layout (main.tf): one efa-only interface on card 0 / device 1
    # and one on device 0 of each further card. A type with more network cards
    # than EFA interfaces + 1 has no verified layout.
    condition = var.efa == "off" || alltrue([
      for t, v in var.instance_types : !v.efa_supported || coalesce(v.efa_maximum_interfaces, 0) == 0 || v.maximum_network_cards <= coalesce(v.efa_maximum_interfaces, 0) + 1
    ])
    error_message = "Unsupported EFA layout: an instance type has more network cards than EFA interfaces + 1 (no verified static layout). Use efa = \"off\" or another type."
  }
}

variable "capacity_type" {
  description = "How the GPU nodes are bought: capacity-block | odcr | on-demand | spot (Terraform never purchases capacity)"
  type        = string

  validation {
    condition     = contains(["capacity-block", "odcr", "on-demand", "spot"], var.capacity_type)
    error_message = "capacity_type must be one of: capacity-block, odcr, on-demand, spot."
  }
}

variable "reservation_ids" {
  description = "Capacity reservation IDs (cr-...) already bought by the user; required for capacity-block and odcr, empty for on-demand and spot"
  type        = list(string)
  default     = []

  validation {
    condition     = alltrue([for id in var.reservation_ids : can(regex("^cr-[0-9a-f]+$", id))])
    error_message = "reservation_ids must contain EC2 capacity reservation IDs (cr-<hex>)."
  }

  validation {
    condition     = contains(["capacity-block", "odcr"], var.capacity_type) ? length(var.reservation_ids) > 0 : length(var.reservation_ids) == 0
    error_message = "capacity-block and odcr need at least one reservation ID; on-demand and spot take none."
  }

  validation {
    condition     = length(distinct(var.reservation_ids)) == length(var.reservation_ids)
    error_message = "reservation_ids must not contain duplicates."
  }
}

variable "reservations" {
  description = "Capacity Block facts keyed by reservation ID (data.aws_ec2_capacity_block_reservation); {} for odcr, on-demand and spot"
  type = map(object({
    instance_type        = string
    instance_count       = number
    availability_zone_id = string
    state                = string
  }))
  default = {}

  validation {
    condition     = alltrue([for id, r in var.reservations : contains(var.vpc_az_ids, r.availability_zone_id)])
    error_message = "Every Capacity Block must be in an AZ of the VPC (vpc_az_ids)."
  }

  validation {
    condition     = alltrue([for id, r in var.reservations : contains(keys(var.instance_types), r.instance_type)])
    error_message = "Every Capacity Block must be for one of the requested instance types (a block of another type would bill without ever being used)."
  }
}

variable "offerings" {
  description = "AZ IDs offering each instance type (data.aws_ec2_instance_type_offerings), used to pick the AZ for on-demand and spot when gpu_az_ids is empty"
  type        = map(list(string))
  default     = {}

  validation {
    # Inline on purpose: a validation may not reference a local derived from
    # its own variable (cycle).
    condition = !contains(["on-demand", "spot"], var.capacity_type) || length(var.gpu_az_ids) > 0 || length(setintersection(
      toset(var.vpc_az_ids),
      [for t in keys(var.instance_types) : toset(lookup(var.offerings, t, []))]...
    )) > 0
    error_message = "No VPC AZ offers every requested instance type: request fewer types or set gpu_az_ids explicitly."
  }
}

variable "gpu_az_ids" {
  description = "AZ IDs of the GPU nodes; [] = derived (capacity-block: the reservations' AZs; on-demand/spot: the first VPC AZ offering every type); required for odcr"
  type        = list(string)
  default     = []

  validation {
    condition     = alltrue([for id in var.gpu_az_ids : contains(var.vpc_az_ids, id)])
    error_message = "Every gpu_az_ids entry must be an AZ ID of the VPC (vpc_az_ids)."
  }

  validation {
    condition     = var.capacity_type != "odcr" || length(var.gpu_az_ids) > 0
    error_message = "odcr needs gpu_az_ids (there is no data source for ODCR facts): list the AZ IDs of the reservations."
  }

  validation {
    condition = var.capacity_type != "capacity-block" || length(var.reservations) == 0 || alltrue([
      for id in var.gpu_az_ids : contains([for r in values(var.reservations) : r.availability_zone_id], id)
    ])
    error_message = "With capacity-block, gpu_az_ids may only list AZs of the Capacity Blocks (leave it empty to derive them)."
  }
}

variable "gpu_max_nodes" {
  description = "Upper bound of GPU nodes per pool (limits); null = derived (capacity-block: instances of the live blocks of the pool's types; on-demand/spot: 1); required for odcr"
  type        = number
  default     = null

  validation {
    condition     = var.gpu_max_nodes == null ? true : var.gpu_max_nodes >= 1 && floor(var.gpu_max_nodes) == var.gpu_max_nodes
    error_message = "gpu_max_nodes must be an integer >= 1 (or null)."
  }

  validation {
    condition     = var.capacity_type != "odcr" || var.gpu_max_nodes != null
    error_message = "odcr needs gpu_max_nodes (there is no data source for ODCR instance counts)."
  }
}

variable "vpc_az_ids" {
  description = "AZ IDs spanned by the VPC (each has one private subnet)"
  type        = list(string)
}

variable "az_name_by_id" {
  description = "AZ name of each AZ ID (Kubernetes zone labels use names)"
  type        = map(string)
}

variable "private_subnet_by_az_id" {
  description = "Private subnet ID of each VPC AZ ID"
  type        = map(string)
}

variable "node_role_name" {
  description = "Auto Mode node IAM role name used by the NodeClasses"
  type        = string
}

variable "security_group_id" {
  description = "Node security group (cluster primary SG; it carries the self-referencing all-traffic rules EFA needs)"
  type        = string
}

variable "project_name" {
  description = "Value of the Project tag on the EC2 instances of the NodeClasses"
  type        = string
}

variable "pod_ipv4_prefix_count" {
  description = "Number of /28 prefixes (16 pod IPs each) on the primary ENI of an EFA node"
  type        = number
  default     = 4
}

variable "ephemeral_storage_size" {
  description = "ephemeralStorage.size of the NodeClasses (Gi or Ti); below the instance store, Auto Mode builds the NVMe RAID0"
  type        = string
  default     = "256Gi"
}

variable "efa" {
  description = "auto = static EFA layout on every type with EFA; off = no advancedNetworking (Auto Mode manages ENIs/IPs)"
  type        = string
  default     = "auto"

  validation {
    condition     = contains(["auto", "off"], var.efa)
    error_message = "efa must be auto or off."
  }
}

variable "expire_after" {
  description = "NodePool expireAfter; null = 480h for reserved capacity, 168h for on-demand and spot"
  type        = string
  default     = null
}

variable "consolidate_after" {
  description = "consolidateAfter of on-demand and spot pools (reserved pools always use Never)"
  type        = string
  default     = "15m"
}

variable "product_overrides" {
  description = "nvidia.com/gpu.product value per EC2 GPU name, where the derived NVIDIA-<NAME> differs from the GPU feature discovery label"
  type        = map(string)
  default     = {}
}
