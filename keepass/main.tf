# The password vaults for this project: s3keepass, the Registry module cloudformer/infra/aws//modules/s3keepass
# (S3 bucket + Lambda + Function URL + web UI), deploying the s3keepass release that module version was tested with.
# Its own state, apart from ../terraform: the trading side can be rebuilt without touching the vaults.
#   terraform -chdir=keepass init && terraform -chdir=keepass apply -var-file=mt5-hedge-keepass-dev.tfvars
terraform {
  required_version = ">= 1.5"
  required_providers {
    aws      = { source = "hashicorp/aws", version = "~> 6.0" }
    archive  = { source = "hashicorp/archive", version = "~> 2.4" }
    external = { source = "hashicorp/external", version = "~> 2.3" }
  }
  backend "local" {
    path = "state/terraform.tfstate"
  }
}

provider "aws" {
  region     = var.region
  access_key = var.aws_access_key != "" ? var.aws_access_key : null
  secret_key = var.aws_secret_key != "" ? var.aws_secret_key : null
  profile    = var.aws_profile != "" ? var.aws_profile : null
  default_tags {
    tags = { project = var.project_name, env = var.env }
  }
}

module "keepass" {
  source  = "cloudformer/infra/aws//modules/s3keepass"
  version = "0.1.3" # deploys s3keepass 0.1.1 by default

  project_name          = var.project_name # bucket <project_name>-vaults-<account id>, function <project_name>-lambda
  console_auth_username = var.console_auth_username
  console_auth_password = var.console_auth_password
  force_destroy         = var.force_destroy
  custom_template       = var.custom_template
}

output "console_url" {
  description = "Open in a browser; sign in with console_auth_username / console_auth_password, then each vault's master password"
  value       = module.keepass.console_url
}

output "bucket" {
  value = module.keepass.bucket
}
