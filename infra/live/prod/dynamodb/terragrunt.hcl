include "root" {
  path = find_in_parent_folders()
}

terraform {
  source = "../../../modules/dynamodb"
}

inputs = {
  name_prefix = "a2z-core"
}
