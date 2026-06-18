# DynamoDB tables for A2Z Core.
#
# Schema mirrors app/aws_resources.py / Design §3.1 exactly — these access
# patterns are load-bearing. All tables are on-demand (PAY_PER_REQUEST) per the
# cost decision (CLAUDE.md §10). TTL is enabled where retention applies so
# DynamoDB expires items for free (CLAUDE.md §11).

variable "name_prefix" {
  type    = string
  default = "a2z-core"
}

locals {
  billing_mode = "PAY_PER_REQUEST"
}

# --- Membership (single-table adjacency design) ---
resource "aws_dynamodb_table" "membership" {
  name         = "${var.name_prefix}-membership"
  billing_mode = local.billing_mode
  hash_key     = "PK"
  range_key    = "SK"

  attribute {
    name = "PK"
    type = "S"
  }
  attribute {
    name = "SK"
    type = "S"
  }
  attribute {
    name = "GSI1PK"
    type = "S"
  }
  attribute {
    name = "GSI1SK"
    type = "S"
  }

  global_secondary_index {
    name            = "GSI1"
    hash_key        = "GSI1PK"
    range_key       = "GSI1SK"
    projection_type = "ALL"
  }

  point_in_time_recovery {
    enabled = true
  }
}

# --- Audit (append-only, 7yr TTL) ---
resource "aws_dynamodb_table" "audit" {
  name         = "${var.name_prefix}-audit"
  billing_mode = local.billing_mode
  hash_key     = "event_id"

  attribute {
    name = "event_id"
    type = "S"
  }
  attribute {
    name = "org_id"
    type = "S"
  }
  attribute {
    name = "timestamp"
    type = "S"
  }
  attribute {
    name = "actor_id"
    type = "S"
  }

  global_secondary_index {
    name            = "GSI1"
    hash_key        = "org_id"
    range_key       = "timestamp"
    projection_type = "ALL"
  }
  global_secondary_index {
    name            = "GSI2"
    hash_key        = "actor_id"
    range_key       = "timestamp"
    projection_type = "ALL"
  }

  ttl {
    attribute_name = "ttl"
    enabled        = true
  }
  point_in_time_recovery {
    enabled = true
  }
}

# --- Settings (queried only by org_id) ---
resource "aws_dynamodb_table" "settings" {
  name         = "${var.name_prefix}-settings"
  billing_mode = local.billing_mode
  hash_key     = "org_id"

  attribute {
    name = "org_id"
    type = "S"
  }
  point_in_time_recovery {
    enabled = true
  }
}

# --- Email events (90d TTL) ---
resource "aws_dynamodb_table" "email_events" {
  name         = "${var.name_prefix}-email-events"
  billing_mode = local.billing_mode
  hash_key     = "message_id"

  attribute {
    name = "message_id"
    type = "S"
  }
  attribute {
    name = "org_id"
    type = "S"
  }
  attribute {
    name = "timestamp"
    type = "S"
  }

  global_secondary_index {
    name            = "org-timestamp-index"
    hash_key        = "org_id"
    range_key       = "timestamp"
    projection_type = "ALL"
  }

  ttl {
    attribute_name = "ttl"
    enabled        = true
  }
}

# --- Suppression (indefinite, no TTL) ---
resource "aws_dynamodb_table" "suppression" {
  name         = "${var.name_prefix}-suppression"
  billing_mode = local.billing_mode
  hash_key     = "org_id"
  range_key    = "email"

  attribute {
    name = "org_id"
    type = "S"
  }
  attribute {
    name = "email"
    type = "S"
  }
  point_in_time_recovery {
    enabled = true
  }
}

# --- Files (optional per-file TTL) ---
resource "aws_dynamodb_table" "files" {
  name         = "${var.name_prefix}-files"
  billing_mode = local.billing_mode
  hash_key     = "org_id"
  range_key    = "key"

  attribute {
    name = "org_id"
    type = "S"
  }
  attribute {
    name = "key"
    type = "S"
  }
  attribute {
    name = "service_type"
    type = "S"
  }

  global_secondary_index {
    name            = "service-index"
    hash_key        = "org_id"
    range_key       = "service_type"
    projection_type = "ALL"
  }

  ttl {
    attribute_name = "ttl"
    enabled        = true
  }
}

output "table_names" {
  value = [
    aws_dynamodb_table.membership.name,
    aws_dynamodb_table.audit.name,
    aws_dynamodb_table.settings.name,
    aws_dynamodb_table.email_events.name,
    aws_dynamodb_table.suppression.name,
    aws_dynamodb_table.files.name,
  ]
}
