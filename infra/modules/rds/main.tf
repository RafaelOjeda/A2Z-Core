# Shared Postgres RDS instance — one instance for the whole platform; each
# service gets its own schema (Invoicing's "invoicing", Omni-Channel's
# "omnichannel"), never its own instance. That's the cost/DRY decision in
# app/services/omnichannel/CLAUDE.md §7/§10: reusing one instance avoids a
# second ~$15-25/mo RDS instance per service.
#
# MVP posture (docs/phase2-invoicing.md, CLAUDE.md §10/§14): single-AZ
# db.t4g.micro, private subnets, ingress from the app SG only. Everything in
# it is a service's own data (not Core's — Core stays on DynamoDB), so this
# module is provisioned once, ahead of whichever service (Invoicing or
# Omni-Channel) builds its schema first.
#
# The master password is AWS-managed (manage_master_user_password), not a
# Terraform-generated secret written to state — root CLAUDE.md golden rule
# #5 ("no secrets in code or env vars where avoidable"). RDS creates and
# rotates it in Secrets Manager; wiring that secret into the ECS task
# definition's `secrets` block is the ecs module's job (not yet done here —
# left for the deploy step that actually runs a service against real AWS).

variable "name_prefix" {
  type    = string
  default = "a2z-core"
}

variable "private_subnet_ids" {
  type = list(string)
}

variable "rds_sg_id" {
  type        = string
  description = "Security group allowing :5432 from app tasks only (vpc module)."
}

variable "instance_class" {
  type    = string
  default = "db.t4g.micro"
}

variable "allocated_storage_gb" {
  type    = number
  default = 20
}

resource "aws_db_subnet_group" "main" {
  name       = "${var.name_prefix}-rds"
  subnet_ids = var.private_subnet_ids
}

resource "aws_db_instance" "main" {
  identifier     = "${var.name_prefix}-rds"
  engine         = "postgres"
  engine_version = "16"
  instance_class = var.instance_class

  allocated_storage      = var.allocated_storage_gb
  storage_type           = "gp3"
  storage_encrypted      = true
  db_subnet_group_name   = aws_db_subnet_group.main.name
  vpc_security_group_ids = [var.rds_sg_id]

  db_name                     = "a2z_core"
  username                    = "a2z"
  manage_master_user_password = true

  multi_az                  = false # MVP: single-AZ (§10/§14)
  publicly_accessible       = false
  backup_retention_period   = 7
  skip_final_snapshot       = false
  final_snapshot_identifier = "${var.name_prefix}-rds-final"
  deletion_protection       = true
}

output "endpoint" {
  value = aws_db_instance.main.address
}

output "port" {
  value = aws_db_instance.main.port
}

output "master_user_secret_arn" {
  description = "Secrets Manager ARN holding the AWS-managed master credentials."
  value       = aws_db_instance.main.master_user_secret[0].secret_arn
}
