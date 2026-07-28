# Network for the A2Z Core single-box MVP (docs/architecture/single-box-mvp.md).
#
# Deliberately MVP-lean, trimmed further than the original ECS-shaped
# network (§14):
#   * ONE public subnet, ONE AZ — there is one EC2 instance, not an ALB
#     needing two AZs' worth of subnets.
#   * NO NAT gateway, NO private subnets: the box sits in the public subnet
#     with its own Elastic IP and reaches AWS APIs directly. That was the
#     single biggest line item in the old network (~$32/mo) and it existed
#     only to give a *private* subnet's tasks egress -- there's no private
#     subnet left to serve.
#   * FREE gateway VPC endpoints for DynamoDB + S3 are kept anyway: they're
#     zero-cost and keep that traffic off the public internet path even
#     though the box itself has a public IP.
#   * ONE security group: the box terminates its own TLS (Caddy) and talks
#     to Postgres over localhost inside the same box -- there is no ALB SG
#     and no separate redis/rds SGs to manage.

variable "name_prefix" {
  type    = string
  default = "a2z-core"
}

variable "cidr_block" {
  type    = string
  default = "10.0.0.0/16"
}

data "aws_availability_zones" "available" {
  state = "available"
}

locals {
  az = data.aws_availability_zones.available.names[0]
}

resource "aws_vpc" "main" {
  cidr_block           = var.cidr_block
  enable_dns_support   = true
  enable_dns_hostnames = true
  tags                 = { Name = var.name_prefix }
}

resource "aws_internet_gateway" "igw" {
  vpc_id = aws_vpc.main.id
  tags   = { Name = var.name_prefix }
}

resource "aws_subnet" "public" {
  vpc_id                  = aws_vpc.main.id
  cidr_block              = cidrsubnet(var.cidr_block, 4, 0)
  availability_zone       = local.az
  map_public_ip_on_launch = true
  tags                    = { Name = "${var.name_prefix}-public-${local.az}" }
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.main.id
  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.igw.id
  }
  tags = { Name = "${var.name_prefix}-public" }
}

resource "aws_route_table_association" "public" {
  subnet_id      = aws_subnet.public.id
  route_table_id = aws_route_table.public.id
}

# --- Free gateway endpoints: DynamoDB + S3 traffic bypasses the public internet ---
data "aws_region" "current" {}

resource "aws_vpc_endpoint" "dynamodb" {
  vpc_id            = aws_vpc.main.id
  service_name      = "com.amazonaws.${data.aws_region.current.name}.dynamodb"
  vpc_endpoint_type = "Gateway"
  route_table_ids   = [aws_route_table.public.id]
  tags              = { Name = "${var.name_prefix}-ddb" }
}

resource "aws_vpc_endpoint" "s3" {
  vpc_id            = aws_vpc.main.id
  service_name      = "com.amazonaws.${data.aws_region.current.name}.s3"
  vpc_endpoint_type = "Gateway"
  route_table_ids   = [aws_route_table.public.id]
  tags              = { Name = "${var.name_prefix}-s3" }
}

# --- One security group: the internet -> the box, nothing else ---
resource "aws_security_group" "app" {
  name_prefix = "${var.name_prefix}-app-"
  vpc_id      = aws_vpc.main.id
  description = "The single EC2 instance -- Caddy terminates TLS on :443, webhooks/SSH need direct ingress"

  ingress {
    description = "HTTP (Caddy redirects to HTTPS; also serves ACME HTTP-01 challenges)"
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }
  ingress {
    description = "HTTPS -- webhook providers (Meta, SES/SNS) call in here directly"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }
  ingress {
    description = "SSH for operator access -- narrow this to a known IP/range before going live"
    from_port   = 22
    to_port     = 22
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
  lifecycle {
    create_before_destroy = true
  }
}

output "vpc_id" {
  value = aws_vpc.main.id
}

output "public_subnet_id" {
  value = aws_subnet.public.id
}

output "app_sg_id" {
  value = aws_security_group.app.id
}
