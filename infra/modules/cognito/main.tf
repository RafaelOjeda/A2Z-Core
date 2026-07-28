# Cognito User Pool (CLAUDE.md §5).
#
# Single-box MVP (docs/architecture/single-box-mvp.md): the post-confirmation
# Lambda that used to live here is gone. CLAUDE.md §5 already preferred
# bootstrapping the Core user row on first authenticated request over the
# Lambda; that's now the only path (app/dependencies.py::current_user).
# There is no `lambda_config` block on the user pool anymore because there
# is no Lambda to wire it to.

variable "name_prefix" {
  type    = string
  default = "a2z-core"
}

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

output "user_pool_id" {
  value = aws_cognito_user_pool.main.id
}

output "user_pool_arn" {
  value = aws_cognito_user_pool.main.arn
}

output "app_client_id" {
  value = aws_cognito_user_pool_client.web.id
}
