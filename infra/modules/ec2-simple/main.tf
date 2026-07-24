# Single EC2 instance for A2Z Core (simplified deployment)
#
# This module replaces the complex ECS setup with a simple EC2 instance.
# The instance runs Docker containers for the FastAPI application.
#
# Before first deployment:
#   1. Build Docker image: docker build -t a2z-core:latest .
#   2. Create ECR repo: aws ecr create-repository --repository-name a2z-core
#   3. Push image: docker tag a2z-core:latest <account>.dkr.ecr.<region>.amazonaws.com/a2z-core:latest
#                  docker push <account>.dkr.ecr.<region>.amazonaws.com/a2z-core:latest

variable "instance_type" {
  type    = string
  default = "t3.medium"
  description = "EC2 instance type (t3.micro for micro workloads, t3.small/medium for testing)"
}

variable "environment" {
  type    = string
  default = "dev"
  description = "Environment name (dev, staging, prod)"
}

variable "app_name" {
  type    = string
  default = "a2z-core"
  description = "Application name for tagging"
}

variable "vpc_id" {
  type = string
}

variable "public_subnet_id" {
  type = string
  description = "Public subnet ID for the EC2 instance"
}

variable "app_sg_id" {
  type = string
  description = "Security group ID for the application"
}

variable "task_role_arn" {
  type = string
  description = "IAM role ARN for EC2 instance (provides AWS API permissions)"
}

variable "ecr_repository_url" {
  type = string
  description = "ECR repository URL for the Docker image"
}

variable "docker_image_tag" {
  type    = string
  default = "latest"
  description = "Docker image tag to deploy"
}

variable "database_url" {
  type        = string
  sensitive   = true
  description = "RDS database connection string"
}

variable "redis_url" {
  type        = string
  description = "Redis connection string"
}

data "aws_ami" "ubuntu" {
  most_recent = true
  owners      = ["099720109477"] # Canonical

  filter {
    name   = "name"
    values = ["ubuntu/images/hvm-ssd/ubuntu-jammy-22.04-amd64-server-*"]
  }

  filter {
    name   = "virtualization-type"
    values = ["hvm"]
  }
}

data "aws_iam_role" "ec2_role" {
  name = "a2z-core-ec2-role"
}

resource "aws_iam_instance_profile" "app_profile" {
  name = "${var.app_name}-ec2-profile-${var.environment}"
  role = data.aws_iam_role.ec2_role.name
}

resource "aws_instance" "app" {
  ami                    = data.aws_ami.ubuntu.id
  instance_type          = var.instance_type
  subnet_id              = var.public_subnet_id
  vpc_security_group_ids = [var.app_sg_id]
  iam_instance_profile   = aws_iam_instance_profile.app_profile.name

  # User data script to set up Docker and run the application
  user_data = base64encode(templatefile("${path.module}/user-data.sh", {
    ecr_repository_url = var.ecr_repository_url
    docker_image_tag   = var.docker_image_tag
    database_url       = var.database_url
    redis_url          = var.redis_url
    aws_region         = data.aws_caller_identity.current.region
    environment        = var.environment
  }))

  associate_public_ip_address = true

  tags = {
    Name        = "${var.app_name}-${var.environment}"
    Environment = var.environment
    ManagedBy   = "Terraform"
  }

  monitoring = true

  root_block_device {
    volume_type           = "gp3"
    volume_size           = 30
    delete_on_termination = true
    encrypted             = true
  }

  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required"
    http_put_response_hop_limit = 1
  }
}

resource "aws_eip" "app" {
  instance = aws_instance.app.id
  domain   = "vpc"

  tags = {
    Name = "${var.app_name}-eip-${var.environment}"
  }

  depends_on = [aws_instance.app]
}

data "aws_caller_identity" "current" {}

output "instance_id" {
  value       = aws_instance.app.id
  description = "EC2 instance ID"
}

output "public_ip" {
  value       = aws_eip.app.public_ip
  description = "Elastic IP address for the instance"
}

output "public_dns" {
  value       = aws_instance.app.public_dns
  description = "Public DNS name"
}

output "security_group_id" {
  value       = var.app_sg_id
  description = "Security group ID"
}
