# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

output "model_storage_bucket" {
  description = "Model weights bucket (MODEL_BUCKET)"
  value       = aws_s3_bucket.model_storage.id
}

output "model_storage_bucket_arn" {
  description = "ARN of the model weights bucket"
  value       = aws_s3_bucket.model_storage.arn
}

output "results_bucket" {
  description = "Benchmark results bucket (RESULTS_BUCKET)"
  value       = aws_s3_bucket.results.id
}

output "results_bucket_arn" {
  description = "ARN of the benchmark results bucket"
  value       = aws_s3_bucket.results.arn
}
