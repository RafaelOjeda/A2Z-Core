# Cognito User Pool + the two out-of-band Lambdas (CLAUDE.md §5, §8).
#
# Both functions are deployed from ONE zip built by scripts/build_lambda.sh
# (app/ + runtime deps; boto3 comes from the Lambda runtime). Run that script
# before `terragrunt apply` here.
#
# Both Lambdas stay OUTSIDE the VPC deliberately: post_confirm touches only
# DynamoDB (membership) and ses_notifications touches DynamoDB + EventBridge —
# Redis is only on the email *send* path inside the monolith. Keep it that way;
# if a Redis dependency ever creeps into a Lambda path, fix the code path
# rather than VPC-attaching the Lambdas.

variable "name_prefix" {
  type    = string
  default = "a2z-core"
}

variable "lambda_zip_path" {
  type        = string
  description = "Path to dist/lambda.zip (scripts/build_lambda.sh)."
}

variable "cognito_lambda_role_arn" {
  type = string
}

variable "ses_lambda_role_arn" {
  type = string
}

variable "ses_topic_arn" {
  type        = string
  description = "SNS topic SES publishes bounce/complaint notifications to (ses module)."
}

variable "lambda_env" {
  type        = map(string)
  description = "Shared env for both Lambdas (A2Z_ENV, table names, EVENT_BUS_NAME)."
  default     = { A2Z_ENV = "prod" }
}

# ------------------------------------------------------------------- User pool
resource "aws_cognito_user_pool" "main" {
  name = var.name_prefix

  username_attributes      = ["email"]
  auto_verified_attributes = ["email"]

  password_policy {
    minimum_length    = 12
    require_lowercase = true
    require_numbers   = true
    require_uppercase = true
    require_symbols   = false
  }

  lambda_config {
    post_confirmation = aws_lambda_function.post_confirm.arn
  }
}

# Public SPA client — no secret; SRP only (auth.py validates the JWTs).
resource "aws_cognito_user_pool_client" "web" {
  name            = "${var.name_prefix}-web"
  user_pool_id    = aws_cognito_user_pool.main.id
  generate_secret = false

  explicit_auth_flows = [
    "ALLOW_USER_SRP_AUTH",
    "ALLOW_REFRESH_TOKEN_AUTH",
  ]
}

# ------------------------------------------------------------------- Lambdas
resource "aws_lambda_function" "post_confirm" {
  function_name    = "${var.name_prefix}-cognito-post-confirm"
  role             = var.cognito_lambda_role_arn
  filename         = var.lambda_zip_path
  source_code_hash = filebase64sha256(var.lambda_zip_path)
  handler          = "app.lambdas.cognito_post_confirm.handler"
  runtime          = "python3.12"
  timeout          = 10
  memory_size      = 256

  environment {
    variables = var.lambda_env
  }
}

resource "aws_lambda_permission" "cognito_invoke" {
  statement_id  = "AllowCognitoInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.post_confirm.function_name
  principal     = "cognito-idp.amazonaws.com"
  source_arn    = aws_cognito_user_pool.main.arn
}

resource "aws_lambda_function" "ses_notifications" {
  function_name    = "${var.name_prefix}-ses-notifications"
  role             = var.ses_lambda_role_arn
  filename         = var.lambda_zip_path
  source_code_hash = filebase64sha256(var.lambda_zip_path)
  handler          = "app.lambdas.ses_notifications.handler"
  runtime          = "python3.12"
  timeout          = 30
  memory_size      = 256

  environment {
    variables = var.lambda_env
  }
}

resource "aws_sns_topic_subscription" "ses_notifications" {
  topic_arn = var.ses_topic_arn
  protocol  = "lambda"
  endpoint  = aws_lambda_function.ses_notifications.arn
}

resource "aws_lambda_permission" "sns_invoke" {
  statement_id  = "AllowSNSInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.ses_notifications.function_name
  principal     = "sns.amazonaws.com"
  source_arn    = var.ses_topic_arn
}

output "user_pool_id" {
  value = aws_cognito_user_pool.main.id
}

output "user_pool_arn" {
  value = aws_cognito_user_pool.main.arn
}

output "app_client_id" {
  value = aws_cognito_user_pool_client.web.id
}
