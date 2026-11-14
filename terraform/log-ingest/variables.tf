variable "project_name" { type = string }
variable "s3rds_query_url" { type = string }
variable "s3rds_user" { type = string }
variable "s3rds_password" {
  type      = string
  sensitive = true
}
variable "log_retention_days" { type = number }
variable "code_dir" {
  description = "The mt5-hedge package staged by build.py (contains mt5_hedge/server/log_ingest.py)"
  type        = string
}
variable "sign_key_prev" {
  type      = string
  sensitive = true
}
