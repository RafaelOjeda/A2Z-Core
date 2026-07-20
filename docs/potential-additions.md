# Potential Additions to A2Z Core

> Part of the [documentation index](README.md). See also: [Core module reference](core/README.md) (what exists today, for contrast with what's proposed here).
> **Authority:** _record_ — a dated decision/log, not a live description of current code.

This document captures common platform capabilities that are often shared across products and would fit naturally into A2Z Core as the platform layer grows.

## High-value shared capabilities

### 1. Feature flags and experimentation
- Per-org, per-user, and per-environment flags
- Gradual rollouts and staged releases
- Experimentation support for A/B testing

### 2. Background jobs and queues
- Async workers for email, reports, imports, and webhooks
- Retry policies and dead-letter handling
- Job status and observability

### 3. Usage metering and billing
- Track API usage, storage consumption, and active seats
- Enforce plan limits and entitlements
- Support billing events and usage reports

### 4. Webhooks and integrations
- Common outbound webhook delivery engine
- Signature verification and retry logic
- Event subscription management per org

### 5. Notifications
- In-app notifications, email digests, SMS, and push delivery
- Template management and preference settings
- Delivery tracking and failure handling

### 6. Search and indexing
- Shared search APIs for records, documents, and messages
- Relevance tuning and faceted filtering
- Indexing pipelines for new data

### 7. Workflow and approvals
- Reusable state machine support
- Approval chains and escalation rules
- Audit-friendly workflow history

### 8. Caching and distributed state
- Shared cache policies and invalidation rules
- Distributed locking and coordination primitives
- Session and temporary state management

### 9. File processing and document pipelines
- OCR, document parsing, and media transformations
- Thumbnail generation and content extraction
- Shared storage lifecycle and processing rules

### 10. Observability and reliability
- Common tracing, metrics, health checks, and alerting
- Standard error taxonomy and incident context
- Service-level dashboards and SLO support

## Suggested prioritization

1. Feature flags
2. Background jobs and queues
3. Usage metering and billing
4. Webhooks and integrations
5. Notifications

## Guiding principle

Any capability that is reused across multiple products and is not specific to one business domain should be considered a candidate for A2Z Core.
