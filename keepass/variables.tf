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
# sign-in: any method that is on lets a request in; at least one must be on (the module checks the fields)
variable "http_auth" {
  description = "User + password (HTTP Basic): { enabled, username, password (16+) }"
  type = object({
    enabled  = bool
    username = optional(string, "")
    password = optional(string, "")
  })
  default   = { enabled = false }
  sensitive = true
}

variable "cloudflare_auth" {
  description = "Cloudflare Access in front of the URL: { enabled, team (<team>.cloudflareaccess.com), aud (the Access application's AUD tag) }"
  type = object({
    enabled = bool
    team    = optional(string, "")
    aud     = optional(string, "")
  })
  default = { enabled = false }
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
