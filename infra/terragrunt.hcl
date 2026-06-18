# Root Terragrunt config — shared remote state, provider, and tags.
#
# Mirrors what scripts/create_local_resources.py builds against LocalStack
# (CLAUDE.md §12). Data-plane resources are intentionally cost-lean: DynamoDB is
# on-demand (CLAUDE.md §10), S3 has lifecycle expiry (§11), TTLs are set at write
# time so there are no cleanup jobs.

locals {
  aws_region = "us-east-1" # single region for MVP (CLAUDE.md §14)
  common_tags = {
    Project   = "a2z-core"
    ManagedBy = "terragrunt"
  }
}

# Remote state in S3 with a DynamoDB lock table. Bucket/table names are
# environment-specific; override per live/ env if needed.
remote_state {
  backend = "s3"
  generate = {
    path      = "backend.tf"
    if_exists = "overwrite_terragrunt"
  }
  config = {
    bucket         = "a2z-terraform-state"
    key            = "${path_relative_to_include()}/terraform.tfstate"
    region         = local.aws_region
    encrypt        = true
    dynamodb_table = "a2z-terraform-locks"
  }
}

# Generate the AWS provider with default tags applied to everything.
generate "provider" {
  path      = "provider.tf"
  if_exists = "overwrite_terragrunt"
  contents  = <<EOF
provider "aws" {
  region = "${local.aws_region}"
  default_tags {
    tags = ${jsonencode(local.common_tags)}
  }
}
EOF
}
