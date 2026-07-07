# ElastiCache Redis — backs the JWKS cache (auth), settings read cache, SES
# config-set-exists cache (email), and the sliding-window rate limiter.
#
# MVP posture (CLAUDE.md §10/§14): one cache.t4g.micro node (~$12/mo), no
# replication group, no Multi-AZ. Everything cached here is rebuildable, so a
# node loss costs latency, not data.

variable "name_prefix" {
  type    = string
  default = "a2z-core"
}

variable "private_subnet_ids" {
  type = list(string)
}

variable "redis_sg_id" {
  type        = string
  description = "Security group allowing :6379 from app tasks only (vpc module)."
}

resource "aws_elasticache_subnet_group" "redis" {
  name       = "${var.name_prefix}-redis"
  subnet_ids = var.private_subnet_ids
}

resource "aws_elasticache_cluster" "redis" {
  cluster_id         = "${var.name_prefix}-redis"
  engine             = "redis"
  node_type          = "cache.t4g.micro"
  num_cache_nodes    = 1
  port               = 6379
  subnet_group_name  = aws_elasticache_subnet_group.redis.name
  security_group_ids = [var.redis_sg_id]
}

output "redis_endpoint" {
  value = aws_elasticache_cluster.redis.cache_nodes[0].address
}

output "redis_url" {
  # Matches app/config.py REDIS_URL format.
  value = "redis://${aws_elasticache_cluster.redis.cache_nodes[0].address}:6379/0"
}
