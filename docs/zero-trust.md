# Zero Trust Policy for A2Z Services

**Status:** Adopted policy. Applies to Core, Omni-Channel, and every future
service (Invoicing, Appointments, Expenses, …).
**Audience:** anyone building or reviewing an A2Z service.
**Related:** [auth & authorization](architecture/auth-and-authorization.md) ·
[data flow & org-scoping](architecture/data-flow.md) ·
Design doc §7 (Security Model) · `CLAUDE.md` §4 (conventions) and §14 (scope).

---

## 1. What Zero Trust means here

Zero Trust is usually summarized as **"never trust, always verify"**: no
request, workload, or network location is trusted by default — every access
is authenticated, authorized, scoped, and audited, every time.

A2Z is a **modular monolith**, not a service mesh, so the classic Zero Trust
playbook (mTLS between microservices, per-service network identity) doesn't
map one-to-one. That's fine — Zero Trust is about *where you draw trust
boundaries and how you verify at them*, not about a specific topology. A2Z's
real boundaries are:

| Boundary | What crosses it | Verified by |
|---|---|---|
| Internet → API | User HTTP requests | Cognito JWT signature check on **every** request |
| Internet → webhooks/SNS | Channel webhooks, SES notifications | Signature/secret validation + idempotent handlers |
| App → AWS data plane | DynamoDB/S3/SES/EventBridge/Secrets Manager calls | IAM task-role policies (least privilege, no static keys) |
| Tenant → tenant | Nothing, ever | `org_id` scoping on every data access (§3) |
| Service → service (in-process) | Python imports of `core/` | Deliberately trusted — see §5 for the compensating controls |
| Service → service (cross-domain) | EventBridge events only | Bus policy + org-scoped event payloads |

The rest of this document states the policy at each boundary, then gives the
**per-service checklist** (§6) that makes the model scale: a new service that
satisfies the checklist inherits the whole posture without inventing any
security machinery of its own.

---

## 2. Principle 1 — Verify identity explicitly, on every request

**Policy:** No request is processed on the strength of where it came from.
Identity is proven cryptographically per request.

- Every authenticated endpoint validates the Cognito JWT **signature** against
  the cached JWKS (`core/auth.py`). Tokens are never trusted because a
  previous request from the same connection was valid; there are no sessions
  with ambient authority.
- Claims (`sub`, `email`) come only from a verified token. Nothing
  user-supplied (headers, body fields) is ever treated as identity.
- Machine entry points are verified too:
  - SNS → `ses_notifications` Lambda: message authenticity is checked and the
    handler is idempotent, so a replayed or duplicated delivery cannot
    escalate into duplicate state changes.
  - Channel webhooks (Omni-Channel and future services): each adapter must
    verify the provider's signature/shared secret before parsing the payload.
    An unverifiable webhook is rejected, not "best-effort processed".
- Cognito owns credentials. Services never see or store passwords, and the
  post-confirmation Lambda only maps a verified Cognito identity to a Core
  user record (`create_user_if_not_exists`, idempotent).

**For new services:** use `auth.get_current_user_from_request` (or the shared
FastAPI dependency) on every router. Do not add alternative auth paths, API
keys, or "internal" unauthenticated endpoints. If a service needs
machine-to-machine ingress (webhooks, schedulers), signature verification is
part of the adapter, tested like any other code path.

## 3. Principle 2 — Tenant isolation is micro-segmentation

In a multi-tenant platform, the most important segmentation isn't network
segments — it's **org boundaries**. A2Z's non-negotiable rule (CLAUDE.md §4)
*is* Zero Trust micro-segmentation applied to data:

- **Every** data access takes an `org_id` and is scoped by it. In DynamoDB the
  `org_id` is in the partition or sort key; in S3 it's the key prefix; in
  Redis it's in the key (`ratelimit:{org_id}:{action}`, settings cache keys);
  in Secrets Manager it's in the secret name; in future Postgres tables it
  will be a `WHERE org_id = $1` predicate (row-level security when Invoicing
  lands).
- `org_id` is never taken from the client at face value for authorization:
  the request's user must have a **membership** in that org
  (`core.membership.get_membership(sub, org_id)`), and role checks
  (`role in {OWNER, ADMIN}`) gate mutations. A valid token for org A grants
  exactly nothing in org B.
- Event payloads always carry `org_id` so subscribers scope their own
  processing — an event is a claim about one org, never a cross-tenant
  instruction.

**Verification, not intention:** every Core module (and every service module
that touches data) ships a test proving cross-org access fails. This is an
exit criterion, not a nice-to-have — see [testing.md](testing.md). A PR that
adds a data access path without a cross-org test is incomplete.

## 4. Principle 3 — Least privilege for workloads, no standing secrets

**Policy:** Workloads authenticate to AWS via short-lived, role-based
credentials scoped to exactly what they need. No long-lived keys exist.

- ECS tasks and Lambdas use **IAM task/execution roles** (`infra/modules/iam`).
  There are no AWS access keys in code, images, or environment variables.
- IAM policies name specific resources (the Core tables, the bucket, the
  `a2z-bus`), not `Resource: "*"`. When a new service adds a table or queue,
  its ARN is added to policy explicitly via Terragrunt — access is granted by
  diff, reviewed like code.
- Per-org third-party credentials (channel tokens, API keys) live in
  **Secrets Manager**, fetched through `core/secrets.py` (org-scoped names,
  Redis-cached with TTL). Services never persist them to their own stores,
  never log them, and never accept them via config files.
- Application config that isn't secret comes from env/config
  ([configuration.md](configuration.md)); anything secret goes through
  Secrets Manager. `.env.example` documents variables without values.

**For new services:** you get a resource, you get a policy line — nothing
more. If Invoicing needs its Postgres tables and a Bedrock model, its role
says so and says nothing about Omni-Channel's SQS queues. Prefer separate
IAM roles per task family (web vs. worker vs. Lambda) as they split, so blast
radius shrinks as the system grows.

## 5. Principle 4 — Assume breach: trusted zones are small, explicit, and compensated

Zero Trust doesn't mean zero trusted zones; it means every trusted zone is a
**deliberate, documented decision** with compensating controls.

A2Z's one big trusted zone is the **in-process boundary**: services import
`core/` directly, and CLAUDE.md §14 explicitly declines service-to-service
network auth while everything runs in one process. That is the right
trade-off for a monolith — a forged "caller identity" inside a single Python
process is meaningless anyway. The compensating controls are:

1. **Org-scoping at the data layer** (§3): even a buggy or compromised code
   path inside the process cannot express a cross-tenant query, because the
   APIs require `org_id` and the storage keys embed it.
2. **Typed, narrow Core APIs**: services can only do what `core/` functions
   allow — there is no "raw table handle" escape hatch. Core never imports
   from `services/`, so a service can't widen Core's behavior from outside.
3. **Events as the only cross-service channel**: services never import each
   other. A compromised or buggy service can emit events, but subscribers
   validate and re-scope on their side.
4. **Audit everything that mutates** (§7): lateral movement inside the
   process still leaves a trail.

**Re-evaluation trigger (write this into any extraction plan):** the moment
any component leaves the process — Core extracted to its own deployable, a
service split out, a second task family calling another over the network —
that hop becomes an untrusted boundary and gets authenticated
service-to-service identity (SigV4, IAM-auth on the transport, or mTLS via
service mesh) *before* it ships, not after. "It used to be in-process" is
not a trust argument.

Network posture backs this up even in the monolith:

- Data stores (RDS, ElastiCache) live in private subnets; security groups
  admit only the app tier. Nothing but the ALB is internet-facing.
- All external traffic is HTTPS/TLS; AWS service calls are TLS. As traffic
  grows, add **VPC endpoints** (Gateway for DynamoDB/S3, Interface for
  SES/EventBridge/Secrets Manager) so data-plane traffic never transits the
  public internet — this is a Terragrunt change, invisible to application
  code.
- Encryption at rest is on for every store (DynamoDB, S3, RDS, ElastiCache,
  Secrets Manager) via AWS-managed keys; move to customer-managed KMS keys
  only when a compliance requirement names it.

## 6. The scalable part: the per-service Zero Trust contract

The reason this model scales is that **services don't implement security —
they inherit it by construction**, the same way they inherit email and
storage. A new service (and each existing one, retroactively) must satisfy
this checklist before it ships. Copy it into the service's kickoff doc and
check the boxes in the PR that completes each item.

**Identity & authorization**
- [ ] Every router uses the shared auth dependency; zero unauthenticated
      endpoints except `/health`.
- [ ] Every org-scoped endpoint resolves membership via
      `core.membership.get_membership` and enforces role checks for
      mutations. The `org_id` used is the one the membership check ran
      against — never a second, unchecked copy from the payload.
- [ ] Any webhook/ingress adapter verifies the provider's signature before
      parsing, and the handler is idempotent.

**Data**
- [ ] Every table/key/prefix/secret the service owns embeds `org_id` in its
      key structure (or RLS predicate for Postgres).
- [ ] A cross-org isolation test exists for every data-access module and runs
      in CI.
- [ ] Retention/TTL is declared in [retention.md](retention.md) at design
      time — data that shouldn't exist can't be stolen.

**Workload**
- [ ] All AWS access is via the task role; the policy diff names the
      service's new resources explicitly and grants nothing else.
- [ ] Third-party credentials go through `core/secrets.py`; none are stored
      in the service's own tables, env vars, or logs.
- [ ] The service talks to other services only via EventBridge events, with
      `org_id` in every payload.

**Observability**
- [ ] Every mutation calls `core.audit.log_audit`.
- [ ] Structured logs carry `request_id`; no JWTs, secrets, message bodies,
      or unnecessary PII are logged.
- [ ] Rate limits for the service's expensive/abusable actions are registered
      in the config-driven registry (`core/rate_limit.py`) — denial of
      service by one tenant against the platform is a Zero Trust failure too.

**Review**
- [ ] The service's kickoff doc names its trust boundaries and any deliberate
      trusted zone (with compensating controls), following §5's pattern.
- [ ] Security review of the checklist happens before first deploy, and again
      when any boundary changes (new ingress, new store, extraction).

Because every item is either a Core call, a Terragrunt diff, or a test
pattern that already exists, the marginal cost per service is near zero —
which is exactly what makes the policy sustainable across N future services.

### Where the existing services stand

| Item | Core | Omni-Channel |
|---|---|---|
| Per-request JWT verification | ✅ `core/auth.py` | ✅ inherits |
| Membership + role checks | ✅ | ✅ (role-vocabulary gap documented in [auth doc](architecture/auth-and-authorization.md)) |
| Org-scoped storage keys | ✅ all tables/prefixes | ✅ (see [data-model.md](services/omnichannel/data-model.md)) |
| Cross-org isolation tests | ✅ per module (exit criterion) | ✅ |
| IAM roles, no static keys | ✅ `infra/modules/iam` | ✅ |
| Secrets via Secrets Manager | ✅ `core/secrets.py` | ✅ consumer |
| Webhook signature verification | n/a | Per adapter — verify against [known-issues.md](services/omnichannel/known-issues.md) for gaps |
| Audit on mutations | ✅ | ✅ |
| Events-only cross-service comms | ✅ publisher | ✅ |

Gaps found when auditing against this table become issues, not silent fixes —
the point of the checklist is that drift is visible.

## 7. Continuous verification

Zero Trust is a posture you *maintain*, not a milestone:

- **CI enforces the invariants:** cross-org tests, `ruff`, `mypy --strict`,
  and coverage gates run on every PR ([ci-cd.md](ci-cd.md)). A failing
  isolation test blocks merge exactly like a failing unit test.
- **Audit log is the flight recorder:** append-only, org-scoped, 7-year
  retention. When investigating an incident, the question "what did this
  identity do, in which org, when" must be answerable from `a2z-core-audit`
  alone.
- **CloudWatch is the runtime signal:** structured JSON logs with
  `request_id`, metrics on auth failures, rate-limit rejections, and
  suppression hits. Alarms on anomalies (spike in 401/403s, one org's
  rate-limit rejections) are the cheap early-warning layer — add them as
  traffic becomes non-trivial.
- **Policy review cadence:** re-read this document at each phase boundary
  (Invoicing kickoff, Omni-Channel distribution, any extraction) and whenever
  a checklist item in §6 changes meaning. Update the doc in the same PR that
  changes the boundary.

## 8. Maturity roadmap (deliberate, not aspirational)

Adopt hardening when the trigger fires — not before (cost/complexity), not
after (risk):

| Step | Trigger | Change |
|---|---|---|
| VPC endpoints for DynamoDB/S3/SES/EventBridge/Secrets Manager | Steady production traffic | Terragrunt only; no app change |
| Postgres row-level security | Invoicing's first table | RLS policies + `SET app.org_id` per request, alongside the `WHERE org_id` predicate |
| Split IAM roles per task family | Worker/web/Lambda diverge in what they touch | Narrower policies per role |
| WAF on the ALB | Public launch / first abuse | Managed rules + rate-based rules complementing app-level `rate_limit` |
| Customer-managed KMS keys | A compliance requirement names it | Key policy + rotation |
| Service-to-service authn (SigV4/mTLS) | **Any** component leaves the process | Mandatory before the split ships (§5) |
| Per-service suppression/secrets granularity | A real product need | The schemas already reserve the columns |

---

**The one-line summary:** authenticate every request cryptographically,
scope every byte to an org, grant workloads only what Terragrunt names,
treat the in-process boundary as the single documented trusted zone with
compensating controls, audit every mutation — and make every new service
inherit all of it through the §6 checklist rather than reimplementing any
of it.
