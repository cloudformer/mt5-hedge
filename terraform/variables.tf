# Every value comes from the tfvars file (see example.tfvars); nothing is hardcoded in the modules.

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
  description = "Environment label (tags, bucket name)"
  type        = string
}

variable "project_name" {
  description = "Resource name prefix: <project_name>-db-lambda, <project_name>-log-lambda, ..."
  type        = string
}

# ---- database (s3rds) ----
variable "db_bucket" {
  description = "S3 bucket holding the database files (global name, change if taken)"
  type        = string
}

variable "db_name" {
  description = "Database name: s3://<bucket>/db/<db_name>.sqlite; the log table lives in it"
  type        = string
}

variable "db_user" {
  description = "Database user (HTTP Basic): console login and the ingest function"
  type        = string
}

variable "db_password" {
  description = "Database password, at least 16 characters (the endpoint is a public URL)"
  type        = string
  sensitive   = true
  validation {
    condition     = length(var.db_password) >= 16
    error_message = "db_password must be at least 16 characters."
  }
}

variable "s3rds_version" {
  description = "pip spec of the database server packaged into the Lambda"
  type        = string
}

variable "console" {
  description = "Serve the web console at GET / on the database endpoint"
  type        = bool
}

variable "force_destroy" {
  description = "true = terraform destroy deletes the bucket WITH the databases; false = data survives a destroy"
  type        = bool
}

# ---- log ingest ----
variable "log_retention_days" {
  description = "CloudWatch retention for both Lambdas"
  type        = number
}

# ---- request signing (blue/green key rotation) ----
# Current key = mt5_hedge/sign_key.txt (shipped inside the PyPI package, read by terraform directly).
# Rotation: new sign_key.txt → publish package → put the OLD key here → apply → all machines upgraded → "" → apply.
variable "sign_key_prev" {
  description = "Previous signing key, accepted alongside the current one during a rotation; \"\" = none"
  type        = string
  default     = ""
  sensitive   = true
}
