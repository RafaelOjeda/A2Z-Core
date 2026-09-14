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

variable "hosted_ui_domain_prefix" {
  type        = string
  description = "Globally-unique prefix for the Cognito-hosted domain -- becomes https://<prefix>.auth.<region>.amazoncognito.com. Cognito domain prefixes are first-come-first-served across ALL AWS accounts, not scoped to this one, so pick something specific (e.g. a company-namespaced string), not just \"a2z\" or \"a2z-core\"."
}

data "aws_region" "current" {}

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

# Hosted UI domain -- required for the authorization-code+PKCE flow (the
# BFF/browser-facing login path, e.g. A2Z-UI). A2Z-UI owns its own OAuth
# client on this pool (a separate module, alongside A2Z-UI's own infra);
# this is only the pool-level domain that any such client's hosted-UI
# endpoints (/oauth2/authorize, /oauth2/token, /oauth2/revoke, /logout)
# resolve under. The existing `web` client above is untouched -- it has no
# OAuth flows enabled and was never meant to use the hosted UI.
#
# Using Cognito's own domain (not a custom one) so this needs no ACM
# certificate or DNS -- consistent with no real domain existing yet
# elsewhere in this repo (ec2-simple's domain_name, ses's domain).
#
# Not set here: `managed_login_version = 2`, for Cognito's newer branded
# "Managed Login" UI. Left off because this repo pins no AWS provider
# version (no versions.tf), so whether the resolved provider supports that
# attribute is unverified; the classic hosted UI works identically for the
# authorization-code flow. Add it once a provider version is confirmed to
# support it.
resource "aws_cognito_user_pool_domain" "hosted_ui" {
  domain       = var.hosted_ui_domain_prefix
  user_pool_id = aws_cognito_user_pool.main.id
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

output "hosted_ui_base_url" {
  value       = "https://${aws_cognito_user_pool_domain.hosted_ui.domain}.auth.${data.aws_region.current.name}.amazoncognito.com"
  description = "Base URL for hosted-UI / OAuth endpoints: {this}/oauth2/authorize, {this}/oauth2/token, {this}/oauth2/revoke, {this}/logout."
}

output "issuer" {
  value       = "https://cognito-idp.${data.aws_region.current.name}.amazonaws.com/${aws_cognito_user_pool.main.id}"
  description = "OIDC issuer -- must match app/config.py's cognito_issuer property exactly, since Core validates every JWT's iss claim against it."
}
