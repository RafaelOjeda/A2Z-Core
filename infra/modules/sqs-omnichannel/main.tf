# SQS queues for Omni-Channel's message flow — one inbound queue per
# channel, one shared outbound queue, one events queue (for the invoice.paid
# EventBridge rule target, §6.3), each with its own DLQ. No VPC dependency —
# SQS is a regional AWS API endpoint, not something living inside a subnet
# (app/services/omnichannel/CLAUDE.md §7/§11).
#
# Any DLQ depth > 0 means a message failed processing repeatedly and needs a
# human (§10) — that alarm is wired in the ecs/observability modules, not
# here; this module only shapes the queues themselves.

variable "name_prefix" {
  type    = string
  default = "a2z-omnichannel"
}

variable "max_receive_count" {
  type    = number
  default = 5
}

locals {
  queue_names = [
    "inbound-whatsapp",
    "inbound-sms",
    "inbound-email",
    "outbound",
    "events",
  ]
}

resource "aws_sqs_queue" "dlq" {
  for_each = toset(local.queue_names)
  name     = "${var.name_prefix}-${each.value}-dlq"
}

resource "aws_sqs_queue" "main" {
  for_each = toset(local.queue_names)
  name     = "${var.name_prefix}-${each.value}"

  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.dlq[each.value].arn
    maxReceiveCount     = var.max_receive_count
  })
}

output "queue_urls" {
  value = { for k, q in aws_sqs_queue.main : k => q.url }
}

output "queue_arns" {
  value = { for k, q in aws_sqs_queue.main : k => q.arn }
}

output "dlq_arns" {
  value = { for k, q in aws_sqs_queue.dlq : k => q.arn }
}
