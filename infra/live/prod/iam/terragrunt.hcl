include "root" {
  path = find_in_parent_folders()
}

terraform {
  source = "../../../modules/iam"
}

# Terragrunt wires module outputs together via dependency blocks (first use in
# this repo): outputs of the referenced stack become inputs here. mock_outputs
# let `validate`/`plan` run before the dependency has ever been applied.
dependency "eventbridge" {
  config_path = "../eventbridge"

  mock_outputs = {
    bus_arn = "arn:aws:events:us-east-1:000000000000:event-bus/a2z-bus"
  }
  mock_outputs_allowed_terraform_commands = ["validate", "plan"]
}

inputs = {
  name_prefix   = "a2z-core"
  table_prefix  = "a2z-core"
  bucket_name   = "a2z-ledger"
  event_bus_arn = dependency.eventbridge.outputs.bus_arn
}
