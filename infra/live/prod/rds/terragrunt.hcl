include "root" {
  path = find_in_parent_folders()
}

terraform {
  source = "../../../modules/rds"
}

dependency "vpc" {
  config_path = "../vpc"

  mock_outputs = {
    private_subnet_ids = ["subnet-mock-a", "subnet-mock-b"]
    rds_sg_id          = "sg-mock"
  }
  mock_outputs_allowed_terraform_commands = ["validate", "plan"]
}

inputs = {
  name_prefix        = "a2z-core"
  private_subnet_ids = dependency.vpc.outputs.private_subnet_ids
  rds_sg_id          = dependency.vpc.outputs.rds_sg_id
}
