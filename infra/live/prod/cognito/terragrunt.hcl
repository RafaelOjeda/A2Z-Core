include "root" {
  path = find_in_parent_folders()
}

terraform {
  source = "../../../modules/cognito"
}

dependency "iam" {
  config_path = "../iam"

  mock_outputs = {
    cognito_lambda_role_arn = "arn:aws:iam::000000000000:role/mock-cognito"
    ses_lambda_role_arn     = "arn:aws:iam::000000000000:role/mock-ses"
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

inputs = {
  name_prefix = "a2z-core"
  # Build first: bash scripts/build_lambda.sh (produces dist/lambda.zip).
  lambda_zip_path         = "${get_repo_root()}/dist/lambda.zip"
  cognito_lambda_role_arn = dependency.iam.outputs.cognito_lambda_role_arn
  ses_lambda_role_arn     = dependency.iam.outputs.ses_lambda_role_arn
  ses_topic_arn           = dependency.ses.outputs.notifications_topic_arn

  lambda_env = {
    A2Z_ENV = "prod"
    # Table/bucket/bus names ride on app/config.py defaults (a2z-core-*).
  }
}
