output "db_url" {
  description = "Database endpoint (web console at /, API under /api/), ends with /"
  value       = aws_lambda_function_url.db.function_url
}

output "bucket" {
  value = aws_s3_bucket.db.bucket
}
