# cp example.tfvars mt5-hedge-keepass-dev.tfvars  →  fill in  →  terraform -chdir=keepass init && terraform -chdir=keepass apply -var-file=mt5-hedge-keepass-dev.tfvars
region       = "us-east-2"
aws_profile  = "dev"
env          = "dev"
project_name = "mt5-hedge-keepass"

force_destroy = false # the vaults are the whole point: a destroy must not delete them

# sign-in: any method that is on lets a request in; at least one must be on
http_auth = {
  enabled  = true
  username = "admin"
  password = "CHANGE-ME-at-least-16-chars"
}
cloudflare_auth = {
  enabled = false
  team    = "myteam"    # <team>.cloudflareaccess.com
  aud     = "CHANGE-ME" # the Access application's AUD tag
}

# custom_template = { kdf = { unlock_ms = 3000 } }   # advanced: only these fields change
