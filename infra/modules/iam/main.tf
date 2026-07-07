# Least-privilege IAM for A2Z Core (golden rule #5: no static keys — task and
# Lambda roles only). Resource names must match app/config.py defaults exactly:
# tables a2z-core-*, bucket a2z-ledger, bus a2z-bus.

variable "name_prefix" {
  type    = string
  default = "a2z-core"
}

variable "table_prefix" {
  type    = string
  default = "a2z-core"
}

variable "bucket_name" {
  type    = string
  default = "a2z-ledger"
}

variable "event_bus_arn" {
  type        = string
  description = "ARN of the a2z-bus EventBridge bus (from the eventbridge module)."
}

data "aws_caller_identity" "current" {}
data "aws_region" "current" {}

locals {
  table_arn_pattern = "arn:aws:dynamodb:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:table/${var.table_prefix}-*"
  # GSIs are distinct ARNs — Query against GSI1 etc. needs the /index/* form.
  index_arn_pattern = "${local.table_arn_pattern}/index/*"
  ddb_rw_actions = [
    "dynamodb:GetItem",
    "dynamodb:PutItem",
    "dynamodb:UpdateItem",
    "dynamodb:DeleteItem",
    "dynamodb:Query",
    "dynamodb:BatchWriteItem",
    "dynamodb:ConditionCheckItem",
    "dynamodb:DescribeTable",
  ]
}

# ---------------------------------------------------------------- ECS task role
# Assumed by the running monolith. Everything Core touches, nothing more.
resource "aws_iam_role" "task" {
  name = "${var.name_prefix}-task"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ecs-tasks.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "task" {
  name = "core-access"
  role = aws_iam_role.task.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "DynamoCore"
        Effect   = "Allow"
        Action   = local.ddb_rw_actions
        Resource = [local.table_arn_pattern, local.index_arn_pattern]
      },
      {
        # /health probes connectivity with ListTables; it is account-wide by nature.
        Sid      = "DynamoHealth"
        Effect   = "Allow"
        Action   = ["dynamodb:ListTables"]
        Resource = "*"
      },
      {
        Sid      = "LedgerObjects"
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"]
        Resource = "arn:aws:s3:::${var.bucket_name}/*"
      },
      {
        Sid      = "LedgerList"
        Effect   = "Allow"
        Action   = ["s3:ListBucket"]
        Resource = "arn:aws:s3:::${var.bucket_name}"
      },
      {
        # Core creates SES config sets lazily per {org_id}-{service_type}
        # (app/core/email.py, CLAUDE.md §8) — send alone is not enough.
        Sid    = "SesSendAndConfigSets"
        Effect = "Allow"
        Action = [
          "ses:SendEmail",
          "ses:SendRawEmail",
          "ses:CreateConfigurationSet",
          "ses:CreateConfigurationSetEventDestination",
          "ses:DescribeConfigurationSet",
        ]
        Resource = "*"
      },
      {
        Sid      = "PublishDomainEvents"
        Effect   = "Allow"
        Action   = ["events:PutEvents"]
        Resource = var.event_bus_arn
      },
    ]
  })
}

# ------------------------------------------------------------ ECS execution role
# Pulls the image and ships logs; AWS-managed policy is exactly this.
resource "aws_iam_role" "execution" {
  name = "${var.name_prefix}-execution"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ecs-tasks.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy_attachment" "execution" {
  role       = aws_iam_role.execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

# ----------------------------------------------------------------- Lambda roles
locals {
  lambda_assume = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

# cognito_post_confirm: creates the Core user row (membership table only).
resource "aws_iam_role" "cognito_lambda" {
  name               = "${var.name_prefix}-cognito-post-confirm"
  assume_role_policy = local.lambda_assume
}

resource "aws_iam_role_policy_attachment" "cognito_lambda_logs" {
  role       = aws_iam_role.cognito_lambda.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

resource "aws_iam_role_policy" "cognito_lambda" {
  name = "membership-write"
  role = aws_iam_role.cognito_lambda.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:Query"]
      Resource = [
        "arn:aws:dynamodb:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:table/${var.table_prefix}-membership",
        "arn:aws:dynamodb:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:table/${var.table_prefix}-membership/index/*",
      ]
    }]
  })
}

# ses_notifications: bounce/complaint -> suppression + email-events + event publish.
resource "aws_iam_role" "ses_lambda" {
  name               = "${var.name_prefix}-ses-notifications"
  assume_role_policy = local.lambda_assume
}

resource "aws_iam_role_policy_attachment" "ses_lambda_logs" {
  role       = aws_iam_role.ses_lambda.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

resource "aws_iam_role_policy" "ses_lambda" {
  name = "suppression-and-events"
  role = aws_iam_role.ses_lambda.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:Query"]
        Resource = [
          "arn:aws:dynamodb:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:table/${var.table_prefix}-email-events",
          "arn:aws:dynamodb:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:table/${var.table_prefix}-email-events/index/*",
          "arn:aws:dynamodb:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:table/${var.table_prefix}-suppression",
          "arn:aws:dynamodb:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:table/${var.table_prefix}-audit",
        ]
      },
      {
        Effect   = "Allow"
        Action   = ["events:PutEvents"]
        Resource = var.event_bus_arn
      },
    ]
  })
}

output "task_role_arn" {
  value = aws_iam_role.task.arn
}

output "execution_role_arn" {
  value = aws_iam_role.execution.arn
}

output "cognito_lambda_role_arn" {
  value = aws_iam_role.cognito_lambda.arn
}

output "ses_lambda_role_arn" {
  value = aws_iam_role.ses_lambda.arn
}
