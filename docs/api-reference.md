# HTTP API Reference

> Part of the [documentation index](README.md). See also: [request lifecycle](architecture/request-lifecycle.md), [Omni-Channel API reference](services/omnichannel/api-reference.md).
> **Authority:** _reference_ — describes current code; if the two disagree, the code wins.

`app/main.py` mounts three routers. All are **thin** — they parse the
request and call into `core`/service code (`CLAUDE.md` §2); no business
logic lives in a router.

## Versioning

Every router except `health` is mounted under **`/v1`** (`app/main.py`,
API review 2026-07-18). `/health` stays unversioned — it's an infra
liveness probe (ECS target group, Docker `HEALTHCHECK`), not a
client-facing contract, and shouldn't need updating on a version bump.

**Policy:** additive changes (new optional fields, new endpoints, new
query params) land in place under `/v1`. A change that breaks an existing
endpoint's request/response shape mints `/v2` for that router rather than
mutating `/v1` out from under already-integrated callers. There is no
external consumer yet, so this is a forward-looking discipline, not a
constraint anything is currently up against.

## `routers/health.py` — no auth, unversioned

| Route | Method | Notes |
|---|---|---|
| `/health` | GET | Pings DynamoDB (`ListTables`) and Redis (`PING`). `200` if both succeed, else `503`. This is what the ECS target group and Docker `HEALTHCHECK` probe |

## `routers/core_admin.py` — prefix `/v1/core`, requires `Authorization: Bearer <jwt>`

Admin/testing endpoints exercising Core end to end — not a full product
API, a thin surface for creating orgs, managing members/settings, and
sending email directly against Core.

| Route | Method | Auth | Notes |
|---|---|---|---|
| `/v1/core/orgs` | POST | any authenticated user | `{"name": str}` → creates an org with the caller as OWNER |
| `/v1/core/orgs/{org_id}/members` | GET | member (any role) | Lists members, owner-first |
| `/v1/core/orgs/{org_id}/members` | POST | OWNER/ADMIN | `{"user_id": str, "role": Role}` |
| `/v1/core/orgs/{org_id}/settings` | GET | member (any role) | Returns `OrgSettings` |
| `/v1/core/orgs/{org_id}/settings` | PATCH | OWNER/ADMIN | `{"changes": dict}` |
| `/v1/core/email/send` | POST | member of `body.org_id` | `{org_id, service_type, to, subject, body_html, body_text?, metadata?}` → `EmailResult` |

## `routers/omnichannel.py` — prefix `/v1/omnichannel`

See the full [Omni-Channel API reference](services/omnichannel/api-reference.md)
for every route (webhooks, connections, inbox reads, sending, assignment,
the SSE stream).

## Error responses

Every route returns errors in one uniform shape, driven by the
[`CoreError` hierarchy](core/shared-infrastructure.md#error-hierarchy):

```json
{"detail": "<human-readable message>", "error": "<ExceptionClassName>"}
```

with the HTTP status code taken from the raised exception's own
`status_code`. A `RateLimitError` additionally sets a `Retry-After` header.
See [request lifecycle](architecture/request-lifecycle.md#error-handling--one-exception-hierarchy-one-handler).

## Request correlation

Every response carries an `X-Request-Id` header — either echoed from the
request or freshly minted — and every structured log line emitted while
handling that request includes the same id
(`request_id_middleware`, `app/main.py`).

## Authentication

See [auth & authorization](architecture/auth-and-authorization.md) for the
full JWT validation flow. In short: `Authorization: Bearer <token>`, RS256
(Cognito) in any real environment, HS256 test tokens
(`core.auth.create_test_token`) everywhere else.
