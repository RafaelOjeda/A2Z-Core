# Custom EventBridge bus for cross-service domain events (CLAUDE.md §6).
#
# Single bus "a2z-bus". Producers namespace themselves via `source`
# (a2z.core, a2z.invoicing, ...). Subscribers/rules are added by services in
# later phases. EventBridge is ~$1/million events — negligible at MVP volume.

variable "bus_name" {
  type    = string
  default = "a2z-bus"
}

resource "aws_cloudwatch_event_bus" "a2z" {
  name = var.bus_name
}

output "bus_name" {
  value = aws_cloudwatch_event_bus.a2z.name
}

output "bus_arn" {
  value = aws_cloudwatch_event_bus.a2z.arn
}
