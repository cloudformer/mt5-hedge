# Every value comes from the tfvars file (see example.tfvars).

# ---- deploy ----
variable "region" {
  description = "AWS region"
  type        = string
}

# Credentials, pick one: aws_access_key + aws_secret_key (an IAM user's AK/SK), or aws_profile, or both "" = default chain
variable "aws_access_key" {
  description = "IAM access key id; \"\" = not used"
  type        = string
  default     = ""
}

variable "aws_secret_key" {
  description = "IAM secret access key; \"\" = not used"
  type        = string
  default     = ""
  sensitive   = true
}

variable "aws_profile" {
  description = "Local AWS CLI profile; \"\" = not used"
  type        = string
  default     = ""
}

variable "env" {
  description = "Environment label (tags)"
  type        = string
}

variable "project_name" {
  description = "Resource name prefix: <project_name>-lambda, bucket <project_name>-vaults-<account id>"
  type        = string
}

# ---- the web UI's sign-in (each vault also has its own master password) ----
variable "console_auth_username" {
  description = "Web UI user (HTTP Basic)"
  type        = string
}

variable "console_auth_password" {
  description = "Web UI password, at least 16 characters (the UI is a public URL)"
  type        = string
  sensitive   = true
}

variable "force_destroy" {
  description = "true = terraform destroy deletes the vaults too (set it, apply, then destroy); false = destroy stops at a non-empty bucket"
  type        = bool
}

variable "custom_template" {
  description = "Advanced: new-vault settings over the package's base template, field by field, e.g. { kdf = { unlock_ms = 3000 }, recycle_bin = true }; null = the base"
  type        = any
  default     = null
}
