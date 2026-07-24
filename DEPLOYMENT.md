# A2Z Core Deployment Guide

This guide covers deploying A2Z Core to AWS using a simplified single-EC2-instance setup.

## Architecture

**Current (Simplified)**
- Single EC2 instance running the FastAPI application in Docker
- AWS managed services: RDS (PostgreSQL), ElastiCache (Redis), DynamoDB, S3, SES, etc.
- Elastic IP for static IP address
- Auto-restart via systemd

This is ideal for MVP/development. As you scale, you can migrate to ECS Fargate, Kubernetes, or other distributed setups.

## Prerequisites

- AWS Account with appropriate IAM permissions
- Docker installed locally
- AWS CLI configured
- Terraform + Terragrunt installed
- ECR repository created for the Docker image

## Deployment Steps

### 1. Build and Push Docker Image

```bash
# Build the Docker image
docker build -t a2z-core:latest .

# Create ECR repository (if not exists)
aws ecr create-repository --repository-name a2z-core --region us-east-1

# Get ECR login token
aws ecr get-login-password --region us-east-1 | docker login --username AWS --password-stdin <ACCOUNT_ID>.dkr.ecr.us-east-1.amazonaws.com

# Tag and push image
docker tag a2z-core:latest <ACCOUNT_ID>.dkr.ecr.us-east-1.amazonaws.com/a2z-core:latest
docker push <ACCOUNT_ID>.dkr.ecr.us-east-1.amazonaws.com/a2z-core:latest
```

### 2. Set Up Infrastructure

First, update the configuration with your actual values:

```bash
cd infra/live/prod/ec2

# Edit terragrunt.hcl and update:
# - ecr_repository_url: your ECR repository URL
# - instance_type: t3.micro (free tier), t3.small, or t3.medium
```

Then deploy infrastructure:

```bash
cd infra/live/prod

# Plan deployment
terragrunt run-all plan

# Apply infrastructure
terragrunt run-all apply
```

### 3. Access the Application

After deployment, the EC2 instance will:
1. Pull the Docker image from ECR
2. Start the FastAPI application on port 8000
3. Auto-restart on failure

Get the public IP:

```bash
aws ec2 describe-instances \
  --filters "Name=tag:Name,Values=a2z-core-prod" \
  --query 'Reservations[0].Instances[0].PublicIpAddress' \
  --region us-east-1
```

Access the app: `http://<PUBLIC_IP>:8000`

View logs:

```bash
ssh -i your-key.pem ubuntu@<PUBLIC_IP>
sudo journalctl -u a2z-core -f
```

## Environment Variables

Set in `/opt/a2z-core/.env` on the EC2 instance:

```env
DATABASE_URL=postgresql://user:password@rds-endpoint:5432/a2z
REDIS_URL=redis://elasticache-endpoint:6379
AWS_REGION=us-east-1
ENVIRONMENT=prod
HOST=0.0.0.0
PORT=8000
```

## Scaling Up

As traffic grows, upgrade to a larger instance type:

```bash
# Update instance_type in infra/live/prod/ec2/terragrunt.hcl
instance_type = "t3.small"  # or t3.medium, t3.large

# Reapply Terraform
terragrunt apply
```

For production-grade multi-instance deployment, migrate to ECS Fargate, EKS, or App Runner.

## Monitoring

CloudWatch metrics are available by default (CPU, memory, network). For application-level logs, set up CloudWatch agent (optional - see user-data.sh comments).

## Troubleshooting

**Application not starting?**
```bash
ssh -i your-key.pem ubuntu@<PUBLIC_IP>
sudo systemctl status a2z-core
sudo journalctl -u a2z-core -n 50
```

**ECR authentication failed?**
```bash
# Re-authenticate Docker with ECR
aws ecr get-login-password --region us-east-1 | docker login --username AWS --password-stdin <ACCOUNT_ID>.dkr.ecr.us-east-1.amazonaws.com
```

**Database connection issues?**
- Verify RDS security group allows inbound on port 5432
- Check DATABASE_URL format and credentials
- Verify EC2 instance has IAM permissions for RDS access

## Migration to Distributed Setup

When ready to scale:

1. **ECS Fargate**: See `infra/modules/ecs/` for multi-instance Fargate setup
2. **EKS**: Kubernetes for advanced scaling and resource management
3. **App Runner**: AWS fully managed container service (minimal ops)

Each maintains the same Docker image and application code.
