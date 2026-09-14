# Single EC2 instance running the whole platform (single-box MVP,
# docs/architecture/single-box-mvp.md): app + Postgres + Caddy via
# docker-compose, see infra/modules/ec2-simple/user-data.sh.

terraform {
  source = "${get_parent_terragrunt_dir()}/modules/ec2-simple"
}

include "root" {
  path = find_in_parent_folders()
}

dependency "vpc" {
  config_path = "../vpc"
  mock_outputs = {
    vpc_id           = "vpc-12345678"
    public_subnet_id = "subnet-12345678"
    app_sg_id        = "sg-12345678"
  }
  mock_outputs_allowed_terraform_commands = ["validate", "plan"]
}

dependency "iam" {
  config_path = "../iam"
  mock_outputs = {
    app_role_arn  = "arn:aws:iam::000000000000:role/a2z-core-app"
    app_role_name = "a2z-core-app"
  }
  mock_outputs_allowed_terraform_commands = ["validate", "plan"]
}

dependency "ecr" {
  config_path = "../ecr"
  mock_outputs = {
    repository_url = "000000000000.dkr.ecr.us-east-1.amazonaws.com/a2z-core"
  }
  mock_outputs_allowed_terraform_commands = ["validate", "plan"]
}

dependency "ses" {
  config_path = "../ses"
  mock_outputs = {
    notifications_topic_arn = "arn:aws:sns:us-east-1:000000000000:a2z-ses-notifications"
  }
  mock_outputs_allowed_terraform_commands = ["validate", "plan"]
}

dependency "cognito" {
  config_path = "../cognito"
  mock_outputs = {
    user_pool_id  = "us-east-1_mock00000"
    app_client_id = "mockappclientid00000000000000"
  }
  mock_outputs_allowed_terraform_commands = ["validate", "plan"]
}

inputs = {
  instance_type    = "t4g.small"
  environment      = "prod"
  app_name         = "a2z-core"
  vpc_id           = dependency.vpc.outputs.vpc_id
  public_subnet_id = dependency.vpc.outputs.public_subnet_id
  app_sg_id        = dependency.vpc.outputs.app_sg_id
  app_role_name    = dependency.iam.outputs.app_role_name

  ecr_repository_url = dependency.ecr.outputs.repository_url
  docker_image_tag   = "latest"

  # No domain registered yet -- left unset (the module's own default is
  # "", meaning Caddy serves plain HTTP; see user-data.sh). Unlike the ses
  # module's `domain` placeholder (an inert, harmless-to-fake SES identity),
  # a *fake* domain_name here actively breaks Caddy: given a hostname as
  # its site address (rather than `:80`), Caddy tries and retry-loops a
  # Let's Encrypt HTTP-01 challenge that can never succeed for a
  # non-resolving name, and serves nothing while it does. Once a real
  # domain exists, point a DNS A record at this module's `public_ip`
  # output *first*, then set this to that hostname (should match the
  # endpoint_url placeholder in
  # ../ses-notifications-subscription/terragrunt.hcl, which fronts the
  # same box).
  domain_name = ""

  # REQUIRED override -- do not commit a real password. Pass via
  # `terragrunt apply -var postgres_password=...` or a gitignored
  # *.auto.tfvars file; there is deliberately no default here.
  postgres_password = "CHANGE-ME-set-via--var-or-tfvars"

  s3_bucket                   = "a2z-ledger"
  cognito_user_pool_id        = dependency.cognito.outputs.user_pool_id
  cognito_app_client_id       = dependency.cognito.outputs.app_client_id
  ses_notifications_topic_arn = dependency.ses.outputs.notifications_topic_arn
}
