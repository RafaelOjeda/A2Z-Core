# ECS Fargate for the A2Z monolith (CLAUDE.md §2: one image, one task family).
#
# MVP posture (§10/§14): 0.25 vCPU / 512MB, desired 1, target-tracking
# autoscale to 3 on CPU. HTTP listener only for now — HTTPS needs an ACM cert
# + domain, deferred with the Route53 work (see infra/README.md). The future
# "worker" is the SAME image with a command override added to this task
# definition as a second container when a worker entrypoint exists (Phase 2).
#
# Push the image before applying:
#   docker build -t <ecr_url>:<tag> . && docker push <ecr_url>:<tag>

variable "name_prefix" {
  type    = string
  default = "a2z-core"
}

variable "image_tag" {
  type    = string
  default = "latest"
}

variable "vpc_id" {
  type = string
}

variable "public_subnet_ids" {
  type = list(string)
}

variable "private_subnet_ids" {
  type = list(string)
}

variable "alb_sg_id" {
  type = string
}

variable "app_sg_id" {
  type = string
}

variable "task_role_arn" {
  type = string
}

variable "execution_role_arn" {
  type = string
}

variable "redis_url" {
  type = string
}

variable "cognito_user_pool_id" {
  type = string
}

variable "cognito_app_client_id" {
  type = string
}

variable "ses_notifications_topic_arn" {
  type        = string
  description = "SNS topic Core attaches to each lazily-created SES config set (ses module)."
}

data "aws_region" "current" {}

resource "aws_ecr_repository" "app" {
  name = var.name_prefix
  image_scanning_configuration {
    scan_on_push = true
  }
}

resource "aws_ecs_cluster" "main" {
  name = var.name_prefix
  # Container Insights off — CloudWatch free tier is part of the cost target.
}

resource "aws_cloudwatch_log_group" "app" {
  name              = "/ecs/${var.name_prefix}"
  retention_in_days = 30
}

resource "aws_ecs_task_definition" "app" {
  family                   = var.name_prefix
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = 256
  memory                   = 512
  task_role_arn            = var.task_role_arn
  execution_role_arn       = var.execution_role_arn

  container_definitions = jsonencode([
    {
      name         = "web"
      image        = "${aws_ecr_repository.app.repository_url}:${var.image_tag}"
      essential    = true
      portMappings = [{ containerPort = 8000, protocol = "tcp" }]
      # Env names must match app/config.py aliases exactly. Table/bucket/bus
      # names ride on their config defaults (a2z-core-*, a2z-ledger, a2z-bus).
      environment = [
        { name = "A2Z_ENV", value = "prod" },
        { name = "AWS_REGION", value = data.aws_region.current.name },
        { name = "REDIS_URL", value = var.redis_url },
        { name = "COGNITO_USER_POOL_ID", value = var.cognito_user_pool_id },
        { name = "COGNITO_APP_CLIENT_ID", value = var.cognito_app_client_id },
        { name = "COGNITO_REGION", value = data.aws_region.current.name },
        { name = "SES_NOTIFICATIONS_TOPIC_ARN", value = var.ses_notifications_topic_arn },
      ]
      logConfiguration = {
        logDriver = "awslogs"
        options = {
          "awslogs-group"         = "/ecs/${var.name_prefix}"
          "awslogs-region"        = data.aws_region.current.name
          "awslogs-stream-prefix" = "web"
        }
      }
    }
  ])
}

resource "aws_lb" "main" {
  name               = var.name_prefix
  load_balancer_type = "application"
  subnets            = var.public_subnet_ids
  security_groups    = [var.alb_sg_id]
}

resource "aws_lb_target_group" "app" {
  name        = var.name_prefix
  port        = 8000
  protocol    = "HTTP"
  vpc_id      = var.vpc_id
  target_type = "ip"

  health_check {
    path = "/health"
    # /health returns 503 when DynamoDB/Redis are unreachable (by design), so
    # match 200 only — degraded tasks drain instead of receiving traffic.
    matcher             = "200"
    interval            = 30
    healthy_threshold   = 2
    unhealthy_threshold = 3
  }
}

resource "aws_lb_listener" "http" {
  load_balancer_arn = aws_lb.main.arn
  port              = 80
  protocol          = "HTTP"

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.app.arn
  }
}

resource "aws_ecs_service" "app" {
  name            = "${var.name_prefix}-web"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.app.arn
  launch_type     = "FARGATE"
  desired_count   = 1

  network_configuration {
    subnets         = var.private_subnet_ids
    security_groups = [var.app_sg_id]
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.app.arn
    container_name   = "web"
    container_port   = 8000
  }

  depends_on = [aws_lb_listener.http]
}

resource "aws_appautoscaling_target" "app" {
  max_capacity       = 3
  min_capacity       = 1
  resource_id        = "service/${aws_ecs_cluster.main.name}/${aws_ecs_service.app.name}"
  scalable_dimension = "ecs:service:DesiredCount"
  service_namespace  = "ecs"
}

resource "aws_appautoscaling_policy" "cpu" {
  name               = "${var.name_prefix}-cpu"
  policy_type        = "TargetTrackingScaling"
  resource_id        = aws_appautoscaling_target.app.resource_id
  scalable_dimension = aws_appautoscaling_target.app.scalable_dimension
  service_namespace  = aws_appautoscaling_target.app.service_namespace

  target_tracking_scaling_policy_configuration {
    target_value = 60
    predefined_metric_specification {
      predefined_metric_type = "ECSServiceAverageCPUUtilization"
    }
  }
}

output "ecr_repository_url" {
  value = aws_ecr_repository.app.repository_url
}

output "alb_dns_name" {
  value = aws_lb.main.dns_name
}
