# cp example.tfvars prod.tfvars  →  fill in  →  terraform init && terraform apply -var-file=prod.tfvars
region       = "us-east-2"
aws_profile  = "dev"
env          = "prod"
project_name = "mt5-hedge"

db_bucket     = "prod-mt5-hedge-db-2026" # S3 names are global: change the suffix if taken
db_name       = "mt5-hedge"
db_user       = "admin"
db_password   = "CHANGE-ME-at-least-16-chars"
s3rds_version = "s3rds[server]==0.3.4"
console       = true
force_destroy = false # the log history is the whole point: a destroy must not delete it

log_retention_days = 30

sign_key_prev = "" # fill with the OLD mt5_hedge/sign_key.txt only while rotating keys
