include "root" {
  path = find_in_parent_folders()
}

terraform {
  source = "../../../modules/sqs-omnichannel"
}

inputs = {
  name_prefix = "a2z-omnichannel"
}
