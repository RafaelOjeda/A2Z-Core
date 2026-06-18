# SES plumbing for Core's email module (CLAUDE.md §8).
#
# Per-org/service configuration sets are created **lazily by Core** on first
# send (cached in Redis), so Terraform does not enumerate them. What Terraform
# owns is the shared, long-lived plumbing:
#   * the SNS topic SES publishes bounce/complaint notifications to
#   * a topic policy allowing SES to publish
#   * the org domain identity (verified once at org onboarding)
#
# The ses_notifications Lambda subscribes to this topic; Core attaches each new
# config set's event destination to it when it creates the config set.

variable "domain" {
  type        = string
  description = "Verified sending domain for the org (e.g. acme.com)."
}

variable "notifications_topic_name" {
  type    = string
  default = "a2z-ses-notifications"
}

data "aws_caller_identity" "current" {}

resource "aws_ses_domain_identity" "org" {
  domain = var.domain
}

resource "aws_sns_topic" "ses_notifications" {
  name = var.notifications_topic_name
}

# Allow SES to publish to the topic (scoped to this account).
resource "aws_sns_topic_policy" "ses_publish" {
  arn = aws_sns_topic.ses_notifications.arn
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ses.amazonaws.com" }
      Action    = "SNS:Publish"
      Resource  = aws_sns_topic.ses_notifications.arn
      Condition = {
        StringEquals = { "AWS:SourceAccount" = data.aws_caller_identity.current.account_id }
      }
    }]
  })
}

output "notifications_topic_arn" {
  value = aws_sns_topic.ses_notifications.arn
}

output "domain_identity_arn" {
  value = aws_ses_domain_identity.org.arn
}
