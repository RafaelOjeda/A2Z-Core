# Omni-Channel — Open Decisions (§14)

> Part of the [documentation index](README.md). See also: [Omni-Channel service docs](services/omnichannel/README.md), [known issues](services/omnichannel/known-issues.md).
> **Authority:** _record_ — a dated decision/log, not a live description of current code.

Recorded per `app/services/omnichannel/CLAUDE.md` §14, before Step 2 (data
layer) so the Alembic baseline isn't written against unstated assumptions.

**None of these six change the Step 2 schema itself** — the columns they
touch (`channel_identities.customer_id`, the email fallback domain) are
already shaped to support either answer. They're recorded here so they're
tracked decisions rather than silent defaults, not because the schema is
blocked on them. **Items 1–5 are engineering defaults chosen so the build can
proceed; item 6 is a real business call and is flagged as unconfirmed.**

## 1. Multi-org agents in the UI: simultaneous or context-switch?

**Default: context-switch** (one active org at a time, an org switcher in
the header). Core's membership model supports either — this is purely
frontend UX and doesn't exist yet (no frontend is in scope, per the
Omni-Channel plan's scope revision). Revisit when the frontend is designed.

## 2. Auto-link a new WhatsApp number to a matching customer, or require agent-confirmed merge?

**Default: agent-confirmed merge only.** Auto-linking by phone-number match
risks silently merging two different people (e.g. a shared family/business
line) into one customer record, which then misattributes conversation
history and commission. `channel_identities.customer_id` stays nullable and
is written only when an agent explicitly confirms a merge in the UI.
Revisit if support volume makes manual merging a bottleneck.

## 3. Per-org SES domain verification at signup vs. a 30-day shared-domain grace period

**Default: 30-day shared-domain grace period**, formalizing the fallback
`core/email.py` already has half-built (`_DEFAULT_DOMAIN = "example.com"`,
used when an org hasn't verified its own domain). Concretely: a new org can
send through the shared fallback domain for 30 days from first send; after
that, sends fail closed with a typed error directing the org to verify a
domain. This lets a company try Omni-Channel immediately without a DNS
verification step blocking onboarding, while not letting the fallback
become permanent (shared-domain sending at scale risks that domain's own
deliverability reputation). Needs a `domain_grace_started_at` field on
`OrgSettings` (or equivalent) when this is actually implemented — not part
of the Step 2 schema, since it's `core.settings`/`core.email`'s concern, not
Omni-Channel's Postgres schema.

**Partially implemented (2026-07-18, API review — self-service connect):**
`core.email.start_domain_verification(org_id, domain, changed_by)` now does
the SES `verify_domain_identity`/`verify_domain_dkim` calls and hands back
the actual TXT + 3 CNAME records, and `core.email.get_domain_verification_status`
polls SES for live status (`GET /core/orgs/{org_id}/domain-verification`) —
so connecting an email channel (`POST /omnichannel/orgs/{org_id}/connections`
with `channel_type="email"`) no longer requires an engineer to read the
verification token off the SES console by hand. **Still not implemented:**
the 30-day grace-period *enforcement* described above — `send_email` still
only has the old unconditional dev/test fallback to `_DEFAULT_DOMAIN`
(non-prod only); there is no `domain_grace_started_at` field yet and no
production fail-closed path once 30 days pass without a verified domain.
That's a separate, real change to `send_email`'s hot path and deserves its
own review rather than riding in on the connect-flow fix.

**Related (2026-07-18): WhatsApp connect is now self-service too.**
`core.secrets.put_secret` was added (Core unfreeze — `core/secrets.py` was
read-only before) so `POST /omnichannel/orgs/{org_id}/connections` can
accept a `credentials` object (the access token etc. a user just typed into
a form) and store it itself, keyed by the new connection's id. The old path
— passing a `credentials_secret_key` an engineer already provisioned in
Secrets Manager out of band — still works and is mutually exclusive with
`credentials`. Before this, there was no way to connect a WhatsApp number
without AWS console/CLI access, which doesn't work for a self-service SaaS
product.

## 4. Voice transcription budget shape (v1.5)

Not a v1 decision. Voice is out of scope until v1.5 (see §15); leave
pricing-tier room per the plan's existing note and revisit then.

## 5. Public Inbox API day one vs. post-launch

**Default: post-launch.** v1 is scoped to the internal agent inbox only (see
the minimal-scope revision in the plan header); a public API is additional
surface area (auth, rate limits, versioning) with no confirmed customer
demand yet. Revisit once v1 is live and a real integration request exists.

## 6. Pricing tier shape ⚠ UNCONFIRMED — needs Rafael's input

The plan carries a placeholder from the original external plan: **"$49–79/mo
+ WhatsApp pass-through, SMS pass-through once un-deferred."** This is not a
decision, just an inherited number — Settings/Billing (a future service, per
root `CLAUDE.md` §14 "No Billing engine") needs the real shape, and pricing
is a business call this document can't make on its own. Flagging explicitly
so it isn't mistaken for settled. Revisit when billing is actually scoped.
