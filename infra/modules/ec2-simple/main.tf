# Single EC2 instance for A2Z Core (single-box MVP, docs/architecture/single-box-mvp.md).
#
# Runs the whole platform on one box: the app container (API + the
# Omni-Channel worker as a lifespan task, RUN_OMNICHANNEL_WORKER=true — see
# app/main.py's lifespan and Dockerfile), a Postgres container (no RDS), and
# Caddy for TLS termination. user-data.sh does all of the setup; this file
# just launches the instance with the right role/network/user-data.

variable "instance_type" {
  type        = string
  default     = "t4g.small"
  description = "EC2 instance type -- t4g.small (2 vCPU/2GB, arm64/Graviton) is the MVP default; bump if Postgres+app contend for memory."
}

variable "environment" {
  type        = string
  default     = "prod"
  description = "Environment name (dev, staging, prod)"
}

variable "app_name" {
  type        = string
  default     = "a2z-core"
  description = "Application name for tagging"
}

variable "vpc_id" {
  type = string
}

variable "public_subnet_id" {
  type        = string
  description = "Public subnet ID for the EC2 instance"
}

variable "app_sg_id" {
  type        = string
  description = "Security group ID for the application"
}

variable "app_role_name" {
  type        = string
  description = "Name (not ARN) of the IAM role this instance assumes -- from the iam module's app_role_name output."
}

variable "ecr_repository_url" {
  type        = string
  description = "ECR repository URL for the Docker image (from the ecr module)"
}

variable "docker_image_tag" {
  type        = string
  default     = "latest"
  description = "Docker image tag to deploy"
}

variable "postgres_password" {
  type        = string
  sensitive   = true
  description = "Password for the on-box Postgres container. Override via -var/tfvars; never commit a real value."
}

variable "s3_bucket" {
  type        = string
  default     = "a2z-ledger"
  description = "Ledger bucket (app/config.py's S3_BUCKET) -- the nightly Postgres backup writes under its backups/ prefix."
}

variable "cognito_user_pool_id" {
  type    = string
  default = ""
}

variable "cognito_app_client_id" {
  type    = string
  default = ""
}

variable "ses_notifications_topic_arn" {
  type    = string
  default = ""
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

data "aws_region" "current" {}

resource "aws_iam_instance_profile" "app_profile" {
  name = "${var.app_name}-ec2-profile-${var.environment}"
  role = var.app_role_name
}

resource "aws_instance" "app" {
  ami                    = data.aws_ami.ubuntu.id
  instance_type          = var.instance_type
  subnet_id              = var.public_subnet_id
  vpc_security_group_ids = [var.app_sg_id]
  iam_instance_profile   = aws_iam_instance_profile.app_profile.name

  user_data = base64encode(templatefile("${path.module}/user-data.sh", {
    ecr_repository_url          = var.ecr_repository_url
    docker_image_tag            = var.docker_image_tag
    postgres_password           = var.postgres_password
    s3_bucket                   = var.s3_bucket
    aws_region                  = data.aws_region.current.name
    environment                 = var.environment
    cognito_user_pool_id        = var.cognito_user_pool_id
    cognito_app_client_id       = var.cognito_app_client_id
    ses_notifications_topic_arn = var.ses_notifications_topic_arn
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
