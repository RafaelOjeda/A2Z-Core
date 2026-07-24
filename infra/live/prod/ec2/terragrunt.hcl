# Simplified EC2 deployment for A2Z Core (development/testing)
# Replace the complex ECS setup with a single EC2 instance

terraform {
  source = "${get_parent_terragrunt_dir()}/modules/ec2-simple"
}

include "root" {
  path = find_in_parent_folders()
}

dependency "vpc" {
  config_path = "../vpc"
  mock_outputs = {
    vpc_id              = "vpc-12345678"
    public_subnet_ids   = ["subnet-12345678"]
    private_subnet_ids  = ["subnet-87654321"]
    app_sg_id           = "sg-12345678"
    alb_sg_id           = "sg-87654321"
  }
}

dependency "iam" {
  config_path = "../iam"
  mock_outputs = {
    task_role_arn       = "arn:aws:iam::123456789012:role/a2z-core-task-role"
    execution_role_arn  = "arn:aws:iam::123456789012:role/a2z-core-execution-role"
  }
}

dependency "rds" {
  config_path = "../rds"
  mock_outputs = {
    database_url = "postgresql://user:pass@localhost:5432/a2z"
  }
}

dependency "redis" {
  config_path = "../redis"
  mock_outputs = {
    redis_url = "redis://localhost:6379"
  }
}

inputs = {
  instance_type       = "t3.micro"  # Start small, scale up as needed
  environment         = "prod"
  app_name            = "a2z-core"
  vpc_id              = dependency.vpc.outputs.vpc_id
  public_subnet_id    = dependency.vpc.outputs.public_subnet_ids[0]
  app_sg_id           = dependency.vpc.outputs.app_sg_id
  task_role_arn       = dependency.iam.outputs.task_role_arn
  ecr_repository_url  = "YOUR_ACCOUNT_ID.dkr.ecr.us-east-1.amazonaws.com/a2z-core"
  docker_image_tag    = "latest"
  database_url        = dependency.rds.outputs.database_url
  redis_url           = dependency.redis.outputs.redis_url
}
