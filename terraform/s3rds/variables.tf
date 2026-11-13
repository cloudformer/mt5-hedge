variable "project_name" { type = string }
variable "bucket" { type = string }
variable "user" { type = string }
variable "password" {
  type      = string
  sensitive = true
}
variable "s3rds_version" { type = string }
variable "console" { type = bool }
variable "force_destroy" { type = bool }
variable "log_retention_days" { type = number }
