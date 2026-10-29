# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

################################################################################
# GPU pools: group the requested instance types and render one EKS Auto Mode
# NodeClass + Karpenter NodePool per group.
#
# Group key = GPU name | GPU memory | GPUs per node | network cards | EFA
# interfaces: every member of a group shares the nvidia.com/gpu.product label,
# the GPU limits and the static network layout. Pool name =
# gpu-<slug(GPU name)>-<cb|odcr|od|spot>; when several groups share that base
# name (same GPU, other size), all of them get a <gpus>x<GiB>g[-efa<n>][-nc<n>]
# suffix. Changing gpu_instance_types can therefore rename (recreate) a pool.
#
# Static EFA layout (EKS UG create-node-class "Static Network Interface
# Configuration"; EC2 UG efa-acc-inst-types), for types with EFA and efa=auto:
#   card 0 / device 0   interface  primary ENA: node IP + pod IPs
#                                  (secondaryIPv4PrefixCount x /28)
#   card 0 / device 1   efa-only   EFA #1 (RDMA, no IPs)
#   cards 1.. / dev 0   efa-only   EFA #2..#n
# With networkInterfaces set, Auto Mode attaches no IPs, prefixes or ENIs after
# launch (pod density = pod_ipv4_prefix_count x 16). secondaryIPv4* is only
# valid on card 0, never on efa-only; hugepages cannot be combined with
# efa-only (NodeClass CRD rule), so this module never sets advancedCompute.
# Verified live on p6-b200 only; the other layouts follow the same rule.
#
# Capacity types (Karpenter capacity-type: reserved covers ODCRs and Capacity
# Blocks):
#   capacity-block, odcr  capacityReservationSelectorTerms, capacity-type
#                         reserved, WhenEmpty + Never + budgets "0", 480h (the
#                         reservation bills with or without a node)
#   on-demand, spot       no selector terms, WhenEmpty after consolidate_after,
#                         budgets "1", 168h
#
# Optional manifest keys are dropped with a null filter instead of merge() or
# conditionals over objects: those unify the element types and would turn the
# numbers of networkInterfaces into strings.
################################################################################

locals {
  reserved = contains(["capacity-block", "odcr"], var.capacity_type)

  capacity_suffix = lookup({ "capacity-block" = "cb", odcr = "odcr", "on-demand" = "od", spot = "spot" }, var.capacity_type, "")

  karpenter_capacity_type = local.reserved ? "reserved" : var.capacity_type

  # Capacity Block states that still hold (or will hold) instances. An expired,
  # cancelled or failed block must not break the plan: it simply stops counting.
  live_reservation_states = ["active", "scheduled", "pending", "payment-pending"]

  facts = {
    for t, v in var.instance_types : t => {
      gpu_name      = try(v.gpus[0].name, "")
      gpu_memory    = try(v.gpus[0].memory_size, 0)
      gpus_per_node = sum(concat([0], [for g in v.gpus : g.count]))
      cards         = v.maximum_network_cards
      efa           = v.efa_supported ? coalesce(v.efa_maximum_interfaces, 0) : 0
      vcpus         = v.default_vcpus
      memory_mib    = v.memory_size
      storage_gb    = v.total_instance_storage
    }
  }

  # group key => sorted member types
  groups = {
    for t, f in local.facts : "${f.gpu_name}|${f.gpu_memory}|${f.gpus_per_node}|${f.cards}|${f.efa}" => t...
  }

  group_info = {
    for k, members in local.groups : k => {
      types = sort(members)
      fact  = local.facts[sort(members)[0]]
      base  = "gpu-${trim(replace(lower(local.facts[sort(members)[0]].gpu_name), "/[^a-z0-9]+/", "-"), "-")}-${local.capacity_suffix}"
    }
  }

  groups_by_base = { for k, g in local.group_info : g.base => k... }

  pool_name = {
    for k, g in local.group_info : k => length(local.groups_by_base[g.base]) == 1 ? g.base : join("-", compact([
      g.base,
      "${g.fact.gpus_per_node}x${floor(g.fact.gpu_memory / 1024)}g",
      g.fact.efa > 0 ? "efa${g.fact.efa}" : "",
      g.fact.cards != max(g.fact.efa, 1) ? "nc${g.fact.cards}" : "",
    ]))
  }

  # --- AZs ------------------------------------------------------------------
  reservation_az_ids = sort(distinct([for r in values(var.reservations) : r.availability_zone_id]))

  offered_vpc_az_ids = sort(tolist(setintersection(
    toset(var.vpc_az_ids),
    [for t in keys(var.instance_types) : toset(lookup(var.offerings, t, []))]...
  )))

  az_ids = length(var.gpu_az_ids) > 0 ? sort(distinct(var.gpu_az_ids)) : (
    var.capacity_type == "capacity-block" ? local.reservation_az_ids : slice(local.offered_vpc_az_ids, 0, min(1, length(local.offered_vpc_az_ids)))
  )
  azs = [for id in local.az_ids : lookup(var.az_name_by_id, id, id)]

  subnet_terms = [for id in local.az_ids : { id = lookup(var.private_subnet_by_az_id, id, "") }]

  reservation_terms = [for id in sort(var.reservation_ids) : { id = id }]

  ephemeral_size_gib = try(tonumber(regex("^[0-9]+", var.ephemeral_storage_size)) * (endswith(var.ephemeral_storage_size, "Ti") ? 1024 : 1), 0)

  # --- pools ----------------------------------------------------------------
  pools = {
    for k, g in local.group_info : local.pool_name[k] => {
      instance_types = g.types
      gpu_name       = g.fact.gpu_name
      gpu_product    = lookup(var.product_overrides, g.fact.gpu_name, "NVIDIA-${trim(replace(upper(g.fact.gpu_name), "/[^A-Z0-9]+/", "-"), "-")}")
      gpus_per_node  = g.fact.gpus_per_node
      gpu_memory_mib = g.fact.gpu_memory
      network_cards  = g.fact.cards
      efa_interfaces = var.efa == "auto" ? g.fact.efa : 0
      vcpu_max       = max([for t in g.types : local.facts[t].vcpus]...)
      # The smallest member bounds what a pod can rely on.
      host_memory_gib           = floor(min([for t in g.types : local.facts[t].memory_mib]...) / 1024)
      ephemeral_storage_min_gib = floor(min([for t in g.types : local.facts[t].storage_gb]...) * 1000000000 / 1073741824)
      ephemeral_backing         = floor(min([for t in g.types : local.facts[t].storage_gb]...) * 1000000000 / 1073741824) > local.ephemeral_size_gib ? "nvme" : "ebs"
      max_nodes = var.gpu_max_nodes != null ? var.gpu_max_nodes : max(1, sum(concat([0], [
        for id, r in var.reservations : r.instance_count
        if var.capacity_type == "capacity-block" && contains(local.live_reservation_states, r.state) && contains(g.types, r.instance_type)
      ])))
      capacity_type = var.capacity_type
      az_ids        = local.az_ids
      azs           = local.azs
    }
  }

  interfaces = {
    for name, p in local.pools : name => [
      for i in range(p.efa_interfaces + 1) : {
        for key, value in {
          networkCardIndex         = i <= 1 ? 0 : i - 1
          deviceIndex              = i == 1 ? 1 : 0
          interfaceType            = i == 0 ? "interface" : "efa-only"
          secondaryIPv4PrefixCount = i == 0 ? var.pod_ipv4_prefix_count : null
        } : key => value if value != null
      }
    ]
  }

  disruption = local.reserved ? {
    consolidationPolicy = "WhenEmpty"
    consolidateAfter    = "Never"
    budgets             = [{ nodes = "0" }]
    } : {
    consolidationPolicy = "WhenEmpty"
    consolidateAfter    = var.consolidate_after
    budgets             = [{ nodes = "1" }]
  }

  expire_after = var.expire_after != null ? var.expire_after : (local.reserved ? "480h" : "168h")

  node_classes = {
    for name, p in local.pools : name => {
      apiVersion = "eks.amazonaws.com/v1"
      kind       = "NodeClass"
      metadata   = { name = name }
      spec = {
        for key, value in {
          role                             = var.node_role_name
          subnetSelectorTerms              = local.subnet_terms
          securityGroupSelectorTerms       = [{ id = var.security_group_id }]
          capacityReservationSelectorTerms = local.reserved ? local.reservation_terms : null
          ephemeralStorage = {
            size       = var.ephemeral_storage_size
            iops       = 3000
            throughput = 125
          }
          tags = {
            Project   = var.project_name
            nodeclass = name
          }
          advancedNetworking = p.efa_interfaces > 0 ? { networkInterfaces = local.interfaces[name] } : null
        } : key => value if value != null
      }
    }
  }

  node_pools = {
    for name, p in local.pools : name => {
      apiVersion = "karpenter.sh/v1"
      kind       = "NodePool"
      metadata   = { name = name }
      spec = {
        template = {
          metadata = {
            # GFD-style labels the recipes select; Auto Mode nodes do not run GFD.
            labels = {
              workload                 = "dynamo-gpu"
              "nvidia.com/gpu.present" = "true"
              "nvidia.com/gpu.product" = p.gpu_product
            }
          }
          spec = {
            nodeClassRef = {
              group = "eks.amazonaws.com"
              kind  = "NodeClass"
              name  = name
            }
            expireAfter = local.expire_after
            taints = [
              { key = "nvidia.com/gpu", value = "true", effect = "NoSchedule" }
            ]
            requirements = [
              { key = "karpenter.sh/capacity-type", operator = "In", values = [local.karpenter_capacity_type] },
              { key = "node.kubernetes.io/instance-type", operator = "In", values = p.instance_types },
              { key = "topology.kubernetes.io/zone", operator = "In", values = local.azs },
              { key = "kubernetes.io/arch", operator = "In", values = ["amd64"] },
            ]
          }
        }
        limits = {
          "nvidia.com/gpu" = p.gpus_per_node * p.max_nodes
          cpu              = p.vcpu_max * p.max_nodes
        }
        disruption = local.disruption
      }
    }
  }
}
