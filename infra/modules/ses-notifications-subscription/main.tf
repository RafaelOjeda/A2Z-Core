# Subscribes the box's SES-notifications webhook to the SNS topic
# (app/routers/ses_notifications.py) -- single-box MVP,
# docs/architecture/single-box-mvp.md.
#
# A standalone module rather than folded into `ses` or `ec2-simple`: the
# subscription needs BOTH the topic ARN (from `ses`) and the box's public
# endpoint (from `ec2`) -- and `ec2` already needs the topic ARN as an app
# env var, so putting the subscription in either of those modules would
# make `ses` and `ec2` depend on each other. This module sits after both.
#
# The endpoint must be a real, DNS-resolvable HTTPS URL before this can
# apply -- SNS's subscription confirmation handshake fetches it -- so set
# `endpoint_url` only once a domain points at the ec2 module's Elastic IP
# and Caddy has a cert for it (see ec2-simple/user-data.sh's Caddyfile
# comment). Applying this module before that is done will leave the
# subscription stuck "pending confirmation" in the SNS console; that's
# inert, not broken, and re-applying later (or just re-triggering
# confirmation from the console) picks it up once the endpoint is real.

variable "topic_arn" {
  type        = string
  description = "SES notifications SNS topic ARN (ses module's notifications_topic_arn output)."
}

variable "endpoint_url" {
  type        = string
  description = "https://<domain>/webhooks/ses-notifications -- must resolve to the ec2 module's Elastic IP with a valid cert."
}

resource "aws_sns_topic_subscription" "ses_notifications" {
  topic_arn = var.topic_arn
  protocol  = "https"
  endpoint  = var.endpoint_url
}
