include "root" {
  path = find_in_parent_folders()
}

terraform {
  source = "../../../modules/ecs"
}

dependency "vpc" {
  config_path = "../vpc"

  mock_outputs = {
    vpc_id             = "vpc-mock"
    public_subnet_ids  = ["subnet-mock-a", "subnet-mock-b"]
    private_subnet_ids = ["subnet-mock-c", "subnet-mock-d"]
    alb_sg_id          = "sg-mock-alb"
    app_sg_id          = "sg-mock-app"
  }
  mock_outputs_allowed_terraform_commands = ["validate", "plan"]
}

dependency "iam" {
  config_path = "../iam"

  mock_outputs = {
    task_role_arn      = "arn:aws:iam::000000000000:role/mock-task"
    execution_role_arn = "arn:aws:iam::000000000000:role/mock-exec"
  }
  mock_outputs_allowed_terraform_commands = ["validate", "plan"]
}

dependency "redis" {
  config_path = "../redis"

  mock_outputs = {
    redis_url = "redis://mock:6379/0"
  }
  mock_outputs_allowed_terraform_commands = ["validate", "plan"]
}

dependency "cognito" {
  config_path = "../cognito"

  mock_outputs = {
    user_pool_id  = "us-east-1_mock"
    app_client_id = "mockclientid"
  }
  mock_outputs_allowed_terraform_commands = ["validate", "plan"]
}

inputs = {
  name_prefix = "a2z-core"
  # Push the image before applying:
  #   docker build -t <ecr_url>:latest . && docker push <ecr_url>:latest
  image_tag = "latest"

  vpc_id             = dependency.vpc.outputs.vpc_id
  public_subnet_ids  = dependency.vpc.outputs.public_subnet_ids
  private_subnet_ids = dependency.vpc.outputs.private_subnet_ids
  alb_sg_id          = dependency.vpc.outputs.alb_sg_id
  app_sg_id          = dependency.vpc.outputs.app_sg_id

  task_role_arn      = dependency.iam.outputs.task_role_arn
  execution_role_arn = dependency.iam.outputs.execution_role_arn

  redis_url = dependency.redis.outputs.redis_url

  cognito_user_pool_id  = dependency.cognito.outputs.user_pool_id
  cognito_app_client_id = dependency.cognito.outputs.app_client_id
}
