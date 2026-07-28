# Least-privilege IAM for A2Z Core (golden rule #5: no static keys — one
# EC2 instance role only). Resource names must match app/config.py defaults
# exactly: tables a2z-core-*, bucket a2z-ledger, bus a2z-bus, queues
# a2z-omnichannel-*, secrets a2z/*.
#
# Single-box MVP (docs/architecture/single-box-mvp.md): there is one role,
# not the three this module used to define. ECS is gone (no execution role),
# and both Lambdas (Cognito post-confirm, SES notifications) were folded
# into the app itself (root CLAUDE.md §5/§8 — bootstrap-on-first-request and
# an HTTP endpoint, respectively) rather than staying separate functions, so
# their roles are gone too. Everything the app touches -- Core's DynamoDB/S3/
# SES/EventBridge, Omni-Channel's SQS/Secrets Manager, plus pulling its own
# image from ECR -- is one policy on one role, because on this box there is
# only one process to grant permissions to.

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

variable "ecr_repository_arn" {
  type        = string
  description = "ARN of the a2z-core ECR repository (from the ecr module) -- image pull on boot/restart."
}

variable "omnichannel_queue_prefix" {
  type        = string
  default     = "a2z-omnichannel-"
  description = "Prefix shared by all Omni-Channel SQS queues (inbound/outbound + DLQs)."
}

variable "secrets_prefix" {
  type        = string
  default     = "a2z/"
  description = "Prefix for all per-org/per-service Secrets Manager entries (core/secrets.py)."
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
  queue_arn_pattern  = "arn:aws:sqs:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:${var.omnichannel_queue_prefix}*"
  secret_arn_pattern = "arn:aws:secretsmanager:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:secret:${var.secrets_prefix}*"
}

# --------------------------------------------------------------- EC2 instance role
# Assumed by the box (instance profile); the app process running on it is
# the only thing that ever uses these credentials.
resource "aws_iam_role" "app" {
  name = "${var.name_prefix}-app"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ec2.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "app" {
  name = "app-access"
  role = aws_iam_role.app.id
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
          "ses:VerifyDomainIdentity",
          "ses:VerifyDomainDkim",
          "ses:GetIdentityVerificationAttributes",
        ]
        Resource = "*"
      },
      {
        Sid      = "PublishDomainEvents"
        Effect   = "Allow"
        Action   = ["events:PutEvents"]
        Resource = var.event_bus_arn
      },
      {
        # Omni-Channel's shared inbound/outbound queues + DLQs (queues.py,
        # app/services/omnichannel/CLAUDE.md §5.6). One statement for all
        # four queues -- a new channel never needs a new queue (§5.2).
        Sid      = "OmnichannelQueues"
        Effect   = "Allow"
        Action   = ["sqs:GetQueueUrl", "sqs:SendMessage", "sqs:ReceiveMessage", "sqs:DeleteMessage"]
        Resource = local.queue_arn_pattern
      },
      {
        # Per-org/per-service channel credentials (core/secrets.py). Create
        # is included because put_secret (the self-service write path) falls
        # back to CreateSecret when the name doesn't exist yet.
        Sid    = "OmnichannelSecrets"
        Effect = "Allow"
        Action = [
          "secretsmanager:GetSecretValue",
          "secretsmanager:PutSecretValue",
          "secretsmanager:CreateSecret",
        ]
        Resource = local.secret_arn_pattern
      },
      {
        # Pulling the app's own image on boot/restart (user-data.sh /
        # systemd ExecStartPre) -- GetAuthorizationToken has no resource-level
        # permissions and must be "*" per AWS's ECR auth model.
        Sid      = "EcrAuth"
        Effect   = "Allow"
        Action   = ["ecr:GetAuthorizationToken"]
        Resource = "*"
      },
      {
        Sid      = "EcrPull"
        Effect   = "Allow"
        Action   = ["ecr:BatchCheckLayerAvailability", "ecr:GetDownloadUrlForLayer", "ecr:BatchGetImage"]
        Resource = var.ecr_repository_arn
      },
    ]
  })
}

output "app_role_arn" {
  value = aws_iam_role.app.arn
}

output "app_role_name" {
  value = aws_iam_role.app.name
}
