# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

################################################################################
# VPC Endpoints
################################################################################

# S3 gateway endpoint: model weights (S3 -> NVMe prefetch), image layers stored
# in S3 and other S3 traffic stay on the AWS network instead of going through
# the NAT gateway, which removes the NAT per-GB processing charge. Gateway
# endpoints have no hourly or data charge.
resource "aws_vpc_endpoint" "s3" {
  vpc_id            = module.vpc.vpc_id
  service_name      = "com.amazonaws.${var.aws_region}.s3"
  vpc_endpoint_type = "Gateway"

  route_table_ids = concat(
    module.vpc.private_route_table_ids,
    module.vpc.public_route_table_ids,
  )

  tags = merge(local.tags, {
    Name = "${local.cluster_name}-s3-gateway"
  })
}
