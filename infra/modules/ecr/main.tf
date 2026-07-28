# ECR repository for the single A2Z Core image (Dockerfile builds one image;
# the EC2 box pulls it via user-data.sh/systemd — see modules/ec2-simple).
#
# A standalone module rather than folded into ec2-simple: the `iam` module's
# EC2 instance role needs this repo's ARN to grant image-pull permissions,
# and ec2-simple itself depends on iam for that same role — putting the repo
# in ec2-simple would make iam and ec2-simple depend on each other. Leaf
# module, no dependencies, same shape as dynamodb/s3/eventbridge.

variable "repository_name" {
  type    = string
  default = "a2z-core"
}

resource "aws_ecr_repository" "app" {
  name                 = var.repository_name
  image_tag_mutability = "MUTABLE"

  image_scanning_configuration {
    scan_on_push = true
  }
}

output "repository_url" {
  value = aws_ecr_repository.app.repository_url
}

output "repository_arn" {
  value = aws_ecr_repository.app.arn
}
