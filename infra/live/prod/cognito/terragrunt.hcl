include "root" {
  path = find_in_parent_folders()
}

terraform {
  source = "../../../modules/cognito"
}

# Leaf composition -- no dependencies. The post-confirm Lambda (which used
# to need an IAM role from `iam`) and the SES notifications subscription
# (which used to need `ses`'s topic ARN) are both gone from this module --
# see infra/modules/cognito/main.tf's header.

inputs = {
  name_prefix = "a2z-core"
}
