# Private S3 bucket for Core file storage (Design §3.3).
#
# All access is via short-lived signed URLs — the bucket blocks public access.
# Lifecycle keeps cost low (CLAUDE.md §10/§11): active 30d -> IA -> Glacier,
# expire at 90d. Per-file TTL is enforced in the files table; objects with no
# explicit TTL fall back to this lifecycle.

variable "bucket_name" {
  type    = string
  default = "a2z-ledger"
}

resource "aws_s3_bucket" "ledger" {
  bucket = var.bucket_name
}

resource "aws_s3_bucket_public_access_block" "ledger" {
  bucket                  = aws_s3_bucket.ledger.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "ledger" {
  bucket = aws_s3_bucket.ledger.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "ledger" {
  bucket = aws_s3_bucket.ledger.id

  rule {
    id     = "tiering-and-expiry"
    status = "Enabled"

    transition {
      days          = 30
      storage_class = "STANDARD_IA"
    }
    transition {
      days          = 60
      storage_class = "GLACIER"
    }
    expiration {
      days = 90
    }
    # Clean up incomplete multipart uploads so they don't accrue cost.
    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }
}

output "bucket_name" {
  value = aws_s3_bucket.ledger.id
}
