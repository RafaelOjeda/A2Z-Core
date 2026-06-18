include "root" {
  path = find_in_parent_folders()
}

terraform {
  source = "../../../modules/eventbridge"
}

inputs = {
  bus_name = "a2z-bus"
}
