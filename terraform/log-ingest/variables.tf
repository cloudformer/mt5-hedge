variable "project_name" { type = string }
variable "s3rds_query_url" { type = string }
variable "s3rds_user" { type = string }
variable "s3rds_password" {
  type      = string
  sensitive = true
}
variable "log_retention_days" { type = number }
variable "schema_sql" { type = string }
variable "sign_key" {
  type      = string
  sensitive = true
}
variable "sign_key_prev" {
  type      = string
  sensitive = true
}
