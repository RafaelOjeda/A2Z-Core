#!/bin/bash
set -e

# A2Z Core EC2 user data script
# Installs Docker, pulls the application image, and starts the service

echo "=== A2Z Core EC2 Setup ==="
echo "Environment: ${environment}"
echo "Region: ${aws_region}"

# Update system
apt-get update
apt-get upgrade -y

# Install Docker
apt-get install -y docker.io
systemctl start docker
systemctl enable docker

# Install Docker Compose (optional, for complex setups)
curl -L "https://github.com/docker/compose/releases/latest/download/docker-compose-$(uname -s)-$(uname -m)" -o /usr/local/bin/docker-compose
chmod +x /usr/local/bin/docker-compose

# Install AWS CLI for ECR login
apt-get install -y awscli

# Configure Docker for ECR
aws ecr get-login-password --region ${aws_region} | docker login --username AWS --password-stdin ${ecr_repository_url}

# Create application directory
mkdir -p /opt/a2z-core
cd /opt/a2z-core

# Pull and run the Docker image
docker pull ${ecr_repository_url}:${docker_image_tag}

# Create environment file
cat > /opt/a2z-core/.env << EOF
# Database
DATABASE_URL=${database_url}

# Redis
REDIS_URL=${redis_url}

# AWS Region
AWS_REGION=${aws_region}

# Environment
ENVIRONMENT=${environment}

# Server
HOST=0.0.0.0
PORT=8000
EOF

# Create systemd service for the application
cat > /etc/systemd/system/a2z-core.service << EOF
[Unit]
Description=A2Z Core FastAPI Application
After=docker.service
Requires=docker.service

[Service]
Type=simple
User=root
WorkingDirectory=/opt/a2z-core
Environment="PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
EnvironmentFile=/opt/a2z-core/.env
ExecStartPre=-/usr/bin/docker pull ${ecr_repository_url}:${docker_image_tag}
ExecStart=/usr/bin/docker run --rm \
  --name a2z-core \
  -p 8000:8000 \
  --env-file /opt/a2z-core/.env \
  ${ecr_repository_url}:${docker_image_tag}
ExecStop=/usr/bin/docker stop a2z-core
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

# Enable and start the service
systemctl daemon-reload
systemctl enable a2z-core
systemctl start a2z-core

# Set up CloudWatch logs agent (optional)
# wget https://s3.amazonaws.com/amazoncloudwatch-agent/ubuntu/amd64/latest/amazon-cloudwatch-agent.deb
# dpkg -i amazon-cloudwatch-agent.deb

echo "=== A2Z Core EC2 Setup Complete ==="
