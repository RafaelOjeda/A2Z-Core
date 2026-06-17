# Data Retention Policy

Retention is enforced by **TTL attributes written at insert time** and **S3
lifecycle rules** — never by cleanup jobs (CLAUDE.md §11). This keeps cost near
zero and makes retention auditable from the data itself.

| Data | Retention | Mechanism |
|---|---|---|
| Audit log | 7 years (tax/compliance) | DynamoDB `ttl` set on write |
| Email events | 90 days | DynamoDB `ttl` set on write |
| Suppression list | Indefinite | no TTL |
| Settings | Current only (no history table in MVP) | overwrite; changes captured in audit |
| Files (S3) | Per-file TTL if set, else org lifecycle (90d active → expire) | S3 lifecycle + `files.ttl` |

## Implementation notes

- The TTL attribute is named `ttl` on every table that has one
  (`app/aws_resources.py` → `TTL_ATTRIBUTE` / `TABLES_WITH_TTL`).
- TTL values are stored as **epoch seconds** (DynamoDB's required TTL format).
- Audit writes compute `ttl = now + 7 years`; email-events `now + 90 days`.
- File metadata sets `ttl` only when the caller passes `ttl_days`; the matching
  S3 object expiration is handled by an S3 lifecycle rule.
