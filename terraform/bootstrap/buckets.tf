# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

################################################################################
# S3 buckets
#
# Both: SSE-S3, Block Public Access, BucketOwnerEnforced, TLS-only policy,
# force_destroy = false, and a lifecycle rule that only aborts incomplete
# multipart uploads (objects are never transitioned or expired: weights stay
# in S3 Standard for fast prefetch reads, results are kept).
################################################################################

resource "aws_s3_bucket" "model_storage" {
  #checkov:skip=CKV_AWS_18:Access logging is not needed for re-downloadable public model weights (cost).
  #checkov:skip=CKV_AWS_21:No versioning: weights are re-stageable from Hugging Face; versions would double the storage.
  #checkov:skip=CKV_AWS_144:No cross-region replication: the weights are re-stageable.
  #checkov:skip=CKV_AWS_145:SSE-S3 (AES256) is enough for public model weights; no KMS key to manage or pay for.
  #checkov:skip=CKV2_AWS_62:No consumer for event notifications.
  bucket        = local.model_bucket_name
  force_destroy = false

  tags = {
    Name    = local.model_bucket_name
    Purpose = "ML Model Storage"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "model_storage" {
  bucket = aws_s3_bucket.model_storage.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_public_access_block" "model_storage" {
  bucket = aws_s3_bucket.model_storage.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_ownership_controls" "model_storage" {
  bucket = aws_s3_bucket.model_storage.id

  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "model_storage" {
  bucket = aws_s3_bucket.model_storage.id

  rule {
    id     = "abort-incomplete-multipart-uploads"
    status = "Enabled"

    filter {}

    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }
}

data "aws_iam_policy_document" "model_storage_tls_only" {
  statement {
    sid     = "DenyInsecureTransport"
    effect  = "Deny"
    actions = ["s3:*"]

    resources = [
      aws_s3_bucket.model_storage.arn,
      "${aws_s3_bucket.model_storage.arn}/*",
    ]

    principals {
      type        = "*"
      identifiers = ["*"]
    }

    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }
}

resource "aws_s3_bucket_policy" "model_storage" {
  bucket = aws_s3_bucket.model_storage.id
  policy = data.aws_iam_policy_document.model_storage_tls_only.json

  # Apply the policy after the public access block so the two calls do not race.
  depends_on = [aws_s3_bucket_public_access_block.model_storage]
}

resource "aws_s3_bucket" "results" {
  #checkov:skip=CKV_AWS_18:Access logging is not needed for demo benchmark results (cost).
  #checkov:skip=CKV_AWS_144:No cross-region replication for demo benchmark results (cost); versioning protects them.
  #checkov:skip=CKV_AWS_145:SSE-S3 (AES256) is enough for benchmark results; no KMS key to manage or pay for.
  #checkov:skip=CKV2_AWS_62:No consumer for event notifications.
  bucket        = local.results_bucket_name
  force_destroy = false

  tags = {
    Name    = local.results_bucket_name
    Purpose = "Benchmark results"
  }
}

resource "aws_s3_bucket_versioning" "results" {
  bucket = aws_s3_bucket.results.id

  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "results" {
  bucket = aws_s3_bucket.results.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_public_access_block" "results" {
  bucket = aws_s3_bucket.results.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_ownership_controls" "results" {
  bucket = aws_s3_bucket.results.id

  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "results" {
  bucket = aws_s3_bucket.results.id

  rule {
    id     = "abort-incomplete-multipart-uploads"
    status = "Enabled"

    filter {}

    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }
}

data "aws_iam_policy_document" "results_tls_only" {
  statement {
    sid     = "DenyInsecureTransport"
    effect  = "Deny"
    actions = ["s3:*"]

    resources = [
      aws_s3_bucket.results.arn,
      "${aws_s3_bucket.results.arn}/*",
    ]

    principals {
      type        = "*"
      identifiers = ["*"]
    }

    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }
}

resource "aws_s3_bucket_policy" "results" {
  bucket = aws_s3_bucket.results.id
  policy = data.aws_iam_policy_document.results_tls_only.json

  # Apply the policy after the public access block so the two calls do not race.
  depends_on = [aws_s3_bucket_public_access_block.results]
}
