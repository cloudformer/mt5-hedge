terraform {
  required_version = ">= 1.5"
  required_providers {
    aws      = { source = "hashicorp/aws", version = "~> 6.0" }
    archive  = { source = "hashicorp/archive", version = "~> 2.4" }
    external = { source = "hashicorp/external", version = "~> 2.3" }
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
