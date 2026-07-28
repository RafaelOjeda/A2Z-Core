include "root" {
  path = find_in_parent_folders()
}

terraform {
  source = "../../../modules/ses-notifications-subscription"
}

# Applied last, deliberately: it needs both the topic (ses) and a real,
# DNS-resolvable HTTPS endpoint (ec2) -- see the module's own header for why
# this can't live inside either of those without creating a dependency
# cycle between them.
dependency "ses" {
  config_path = "../ses"
  mock_outputs = {
    notifications_topic_arn = "arn:aws:sns:us-east-1:000000000000:a2z-ses-notifications"
  }
  mock_outputs_allowed_terraform_commands = ["validate", "plan"]
}

dependency "ec2" {
  config_path = "../ec2"
  mock_outputs = {
    public_ip = "203.0.113.10"
  }
  mock_outputs_allowed_terraform_commands = ["validate", "plan"]
}

inputs = {
  topic_arn = dependency.ses.outputs.notifications_topic_arn

  # REQUIRED override once DNS points a real domain at the ec2 module's
  # Elastic IP and Caddy has a cert for it (ec2-simple/user-data.sh). Until
  # then this subscription sits "pending confirmation" in the SNS console --
  # inert, not broken.
  endpoint_url = "https://CHANGE-ME.example.com/webhooks/ses-notifications"
}
