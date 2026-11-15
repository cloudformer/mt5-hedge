# cp example.tfvars mt5-hedge-keepass-dev.tfvars  →  fill in  →  terraform -chdir=keepass init && terraform -chdir=keepass apply -var-file=mt5-hedge-keepass-dev.tfvars
region       = "us-east-2"
aws_profile  = "dev"
env          = "dev"
project_name = "mt5-hedge-keepass"

console_auth_username = "admin"
console_auth_password = "CHANGE-ME-at-least-16-chars"
force_destroy         = false # the vaults are the whole point: a destroy must not delete them

# custom_template = { kdf = { unlock_ms = 3000 } }   # advanced: only these fields change
