include "root" {
  path = find_in_parent_folders()
}

terraform {
  source = "../../../modules/ses"
}

inputs = {
  # First verified org sending domain; further org domains are verified at
  # onboarding (Design §2.3 — sender is {service_type}@{org.domain}).
  domain                   = "a2z.example.com"
  notifications_topic_name = "a2z-ses-notifications"
}
