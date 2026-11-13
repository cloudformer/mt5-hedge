output "log_url" {
  description = "Paste into hedge.json as log_url on every mt5-hedge machine"
  value       = module.log_ingest.url
}

output "console_url" {
  description = "Open in a browser (user/password = db_user/db_password) to look at the hedge_log table"
  value       = module.s3rds.db_url
}

output "db_bucket" {
  value = module.s3rds.bucket
}
