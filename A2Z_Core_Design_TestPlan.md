# A2Z Core Platform: Design & Test Plan

**Status:** Ready for Phase 1 build  
**Scope:** Core only (no Invoicing, no Omni-Channel)  
**Duration:** Weeks 1–2 (standalone)

---

## 1. Core Vision & Scope

A2Z Core is the shared infrastructure layer for all A2Z services. It provides:

- **Auth:** Cognito integration, JWT validation
- **Membership:** User → Org → Role tenancy model
- **Email:** Multi-tenant email sending via SES with per-org/service isolation
- **Storage:** File upload/download for all services, org-scoped
- **Audit:** Append-only event log for compliance and debugging
- **Settings:** Org-level configuration (timezone, currency, domain, plan, etc.)

**Core is NOT:**
- Not Invoicing logic
- Not Omni-Channel logic
- Not Billing/payment logic
- Not Permission/RBAC logic (that's a future service)

**Core IS:**
- Independent and testable without any service
- A complete, locked module that services depend on
- Responsible for multi-tenancy enforcement
- Responsible for audit trails
- Responsible for cost isolation (one customer's high SES usage doesn't affect another's)

---

## 2. API Contracts

Each Core module has a clean Python interface. Services import and call these.

### 2.1 Auth Module

**Responsibility:** Validate JWT tokens from Cognito. Extract user identity.

```python
# core/auth.py

class AuthError(Exception):
    """Base auth exception."""
    pass

class InvalidTokenError(AuthError):
    """JWT is invalid, expired, or signature doesn't match."""
    pass

class MissingTokenError(AuthError):
    """No Authorization header."""
    pass

def validate_jwt(token: str) -> dict:
    """
    Validate a Cognito JWT and return claims.
    
    Args:
        token: Bearer token (without 'Bearer ' prefix)
    
    Returns:
        {
            'sub': str,           # Cognito user ID (immutable)
            'email': str,         # User email
            'email_verified': bool,
            'cognito:username': str,
        }
    
    Raises:
        InvalidTokenError: Token is invalid, expired, or bad signature
        MissingTokenError: No token provided
    
    Notes:
        - Caches Cognito public keys with 24h TTL in Redis
        - Rejects expired tokens
        - Rejects any token not signed by our Cognito User Pool
    """
    pass

def get_current_user_from_request(request: Request) -> dict:
    """
    Extract and validate JWT from FastAPI request.
    
    Args:
        request: FastAPI Request object
    
    Returns:
        JWT claims dict (same as validate_jwt)
    
    Raises:
        MissingTokenError: No Authorization header
        InvalidTokenError: Token invalid
    
    Usage:
        # In a FastAPI handler
        @app.get("/my-data")
        def get_data(request: Request):
            user = auth.get_current_user_from_request(request)
            sub = user['sub']
            # ... rest of logic
    """
    pass

def create_test_token(sub: str, email: str) -> str:
    """
    Generate a valid test JWT for integration tests.
    Only usable in test/dev mode.
    
    Args:
        sub: Cognito user ID
        email: Email claim
    
    Returns:
        Valid JWT string signed with test key
    """
    pass
```

### 2.2 Membership Module

**Responsibility:** Answer "Does user X belong to org Y with what role?"

```python
# core/membership.py

from typing import Optional, List
from datetime import datetime
from enum import Enum

class Role(str, Enum):
    """User roles in an org."""
    OWNER = "owner"          # Can do anything
    ADMIN = "admin"          # Can do most things
    MEMBER = "member"        # Can use services with restrictions
    GUEST = "guest"          # Read-only

class Membership(BaseModel):
    """User's membership in an org."""
    user_id: str              # Cognito sub
    org_id: str
    role: Role
    joined_at: datetime
    # Future: permissions override (for per-user customization)

class Org(BaseModel):
    """An organization (business)."""
    org_id: str               # UUID, slug-friendly
    name: str
    owner_id: str             # Cognito sub of creator
    created_at: datetime
    # ... other settings stored in Settings service

class MembershipError(Exception):
    pass

class NotFoundError(MembershipError):
    pass

class AlreadyExistsError(MembershipError):
    pass

# ===== Core Queries =====

async def get_membership(
    user_id: str,
    org_id: str,
) -> Optional[Membership]:
    """
    Get a user's membership in an org.
    
    Args:
        user_id: Cognito sub
        org_id: Org ID
    
    Returns:
        Membership object if exists, None if not
    
    Raises:
        None (returns None instead)
    
    Performance:
        < 50ms (DynamoDB single item get)
    
    Usage:
        membership = await core.membership.get_membership(user_id, org_id)
        if not membership:
            raise PermissionError("User not in org")
        if membership.role not in [Role.OWNER, Role.ADMIN]:
            raise PermissionError("Insufficient permissions")
    """
    pass

async def list_user_orgs(user_id: str) -> List[Org]:
    """
    List all orgs a user belongs to.
    
    Args:
        user_id: Cognito sub
    
    Returns:
        List of Org objects (not Membership; just the org metadata)
    
    Performance:
        < 100ms (DynamoDB query on GSI)
    
    Notes:
        - Used for org switcher in UI
        - Returns only orgs user is a member of
        - Orgs are returned in order of most recently accessed first (future)
    """
    pass

async def list_org_members(org_id: str) -> List[Membership]:
    """
    List all members of an org.
    
    Args:
        org_id: Org ID
    
    Returns:
        List of Membership objects (one per user in org)
    
    Performance:
        < 200ms (DynamoDB query)
    
    Notes:
        - Used for org settings / team management
        - Returns all members, sorted by role (owner first)
    """
    pass

# ===== Mutations =====

async def create_org(
    org_name: str,
    owner_id: str,  # Cognito sub
) -> Org:
    """
    Create a new org and add the creator as owner.
    
    Args:
        org_name: Human-readable org name
        owner_id: Cognito sub of creator
    
    Returns:
        Newly created Org object
    
    Side effects:
        - Creates org in DynamoDB
        - Creates OWNER membership for owner_id
        - Logs to audit: org.created
    
    Raises:
        None (assumes caller validated inputs)
    
    Performance:
        < 100ms
    """
    pass

async def add_member(
    org_id: str,
    user_id: str,  # Cognito sub
    role: Role,
    inviter_id: str,  # Who invited them (for audit)
) -> Membership:
    """
    Add a user to an org with a given role.
    
    Args:
        org_id: Org ID
        user_id: Cognito sub of user to add
        role: Role to grant (owner, admin, member, guest)
        inviter_id: Cognito sub of who invited them
    
    Returns:
        Newly created Membership
    
    Side effects:
        - Creates membership in DynamoDB
        - Logs to audit: member.added
    
    Raises:
        AlreadyExistsError: User already in org
    
    Performance:
        < 100ms
    
    Notes:
        - Does NOT validate that inviter has permission (caller must check)
        - Does NOT send email invitation (that's caller's job)
    """
    pass

async def change_role(
    org_id: str,
    user_id: str,
    new_role: Role,
    changer_id: str,  # Who made the change (for audit)
) -> Membership:
    """
    Change a user's role in an org.
    
    Args:
        org_id: Org ID
        user_id: Cognito sub of user whose role changes
        new_role: New role
        changer_id: Cognito sub of who made the change
    
    Returns:
        Updated Membership
    
    Side effects:
        - Updates membership in DynamoDB
        - Logs to audit: member.role_changed with {old_role, new_role}
    
    Raises:
        NotFoundError: Membership doesn't exist
    
    Performance:
        < 100ms
    """
    pass

async def remove_member(
    org_id: str,
    user_id: str,
    remover_id: str,  # Who removed them (for audit)
) -> None:
    """
    Remove a user from an org.
    
    Args:
        org_id: Org ID
        user_id: Cognito sub of user to remove
        remover_id: Cognito sub of who removed them
    
    Side effects:
        - Deletes membership in DynamoDB
        - Logs to audit: member.removed
    
    Raises:
        NotFoundError: Membership doesn't exist
    
    Performance:
        < 100ms
    
    Notes:
        - Last owner cannot be removed (caller must check)
    """
    pass

async def create_user_if_not_exists(
    user_id: str,  # Cognito sub
    email: str,
) -> None:
    """
    Create a user record if it doesn't exist.
    Called by Cognito post-signup Lambda.
    
    Args:
        user_id: Cognito sub
        email: User email
    
    Side effects:
        - Creates user in DynamoDB if not exists
        - Does nothing if user already exists (idempotent)
    
    Performance:
        < 50ms
    """
    pass
```

### 2.3 Email Module

**Responsibility:** Send emails on behalf of orgs via SES, with per-org/service isolation.

```python
# core/email.py

from enum import Enum
from typing import List, Optional
from dataclasses import dataclass

class ServiceType(str, Enum):
    """A2Z services that send email."""
    INVOICING = "invoicing"
    OMNICHANNEL = "omnichannel"
    APPOINTMENTS = "appointments"
    EXPENSES = "expenses"

class EmailStatus(str, Enum):
    """Delivery status of an email."""
    QUEUED = "queued"
    SENT = "sent"           # SES accepted it
    DELIVERED = "delivered" # Recipient received it
    BOUNCED = "bounced"     # Hard bounce (invalid address)
    COMPLAINED = "complained"  # Recipient marked as spam
    REJECTED = "rejected"   # SES rejected it (rate limit, etc.)

@dataclass
class EmailResult:
    """Result of send_email call."""
    message_id: str         # SES MessageId
    status: EmailStatus     # Initial status (usually SENT)
    timestamp: datetime
    # For webhook processing later:
    external_message_id: str  # SES ID (used in SNS notifications)

class EmailError(Exception):
    pass

class SuppressionListError(EmailError):
    """Email is on suppression list (bounced before)."""
    pass

class RateLimitError(EmailError):
    """Rate limit exceeded for this org/service."""
    pass

class InvalidAddressError(EmailError):
    """Email address invalid."""
    pass

async def send_email(
    org_id: str,
    service_type: ServiceType,
    to: str,  # Recipient email
    subject: str,
    body_html: str,
    body_text: Optional[str] = None,  # Auto-generated if not provided
    attachments: Optional[List[dict]] = None,
    reply_to: Optional[str] = None,
    metadata: Optional[dict] = None,  # Custom tags for SES
) -> EmailResult:
    """
    Send an email on behalf of an org.
    
    Args:
        org_id: Org ID (used to get domain, sender address, config set)
        service_type: Which A2Z service is sending (invoicing, omnichannel, etc.)
        to: Recipient email
        subject: Email subject
        body_html: HTML body (required)
        body_text: Plain text body (optional; auto-generated from HTML if not provided)
        attachments: List of {filename, content, mime_type} dicts
        reply_to: Reply-to address (defaults to service@org.domain)
        metadata: Custom SES message tags (e.g., {invoice_id: "1054"})
    
    Returns:
        EmailResult with message_id, status, timestamp
    
    Side effects:
        - Sends email via SES
        - Logs to audit: email.sent with {to, subject, service_type}
        - Records in email_events table (for bounce/complaint tracking)
    
    Raises:
        SuppressionListError: Email is on bounce/complaint list
        RateLimitError: Org exceeded email rate limit
        InvalidAddressError: Email address invalid
        EmailError: Any other SES error
    
    Performance:
        < 200ms (SES is async, but we wait for acceptance)
    
    Details on sender logic:
        1. Look up org settings to get verified domain (e.g., acme.com)
        2. Determine sender: {service_type}@{domain} (e.g., invoices@acme.com)
        3. Get org display name from settings (e.g., "Acme Jewelry")
        4. Determine SES config set: {org_id}-{service_type} (e.g., acme-invoicing)
        5. Check suppression list: is 'to' on bounce/complaint list for this org?
        6. Check rate limit: has this org/service hit 50/hour limit?
        7. Send via SES with config set + metadata
        8. Log result
    
    Example usage:
        result = await core.email.send_email(
            org_id="acme-jewelry",
            service_type=ServiceType.INVOICING,
            to="client@example.com",
            subject="Invoice #1054",
            body_html=rendered_html,
            metadata={"invoice_id": "1054", "amount": "1500.00"},
        )
        print(f"Email sent: {result.message_id}")
    """
    pass

async def get_email_status(message_id: str) -> EmailStatus:
    """
    Get the delivery status of an email.
    
    Args:
        message_id: The message_id from EmailResult
    
    Returns:
        Current status (queued, sent, delivered, bounced, complained, rejected)
    
    Performance:
        < 50ms
    
    Notes:
        - Status is updated asynchronously via SNS webhooks
        - Initial status is usually SENT (SES accepted it)
        - Bounces/complaints come later via SNS → Lambda → DynamoDB update
    """
    pass

async def get_suppression_list(org_id: str) -> dict:
    """
    Get bounce and complaint list for an org.
    
    Returns:
        {
            'bounced': [email1, email2, ...],
            'complained': [email3, ...],
        }
    
    Performance:
        < 100ms
    
    Notes:
        - Used by admin/support to debug suppression
        - Not used by send_email (that checks via DynamoDB query)
    """
    pass

async def unsuppress_email(org_id: str, email: str) -> None:
    """
    Remove email from bounce/complaint list.
    
    Args:
        org_id: Org ID
        email: Email address to unsuppress
    
    Side effects:
        - Removes from suppression table
        - Logs to audit: email.unsuppressed
    """
    pass
```

### 2.4 Storage Module

**Responsibility:** Upload/download files for all services, org-scoped and isolated.

```python
# core/storage.py

from typing import Optional
from datetime import datetime, timedelta

@dataclass
class StoredFile:
    """Metadata about a stored file."""
    key: str              # S3 key (full path including org_id)
    url: str              # Public S3 URL (if public) or signed URL
    signed_url: str       # Signed URL valid for 1 hour
    size_bytes: int
    mime_type: str
    uploaded_at: datetime
    uploaded_by: str      # Cognito sub of uploader

class StorageError(Exception):
    pass

class FileTooLargeError(StorageError):
    pass

class NotFoundError(StorageError):
    pass

async def upload_file(
    org_id: str,
    service_type: str,  # invoicing, omnichannel, etc.
    filename: str,
    content: bytes,
    mime_type: str,
    uploaded_by: str,  # Cognito sub
    ttl_days: Optional[int] = None,  # Auto-delete after N days
) -> StoredFile:
    """
    Upload a file to S3.
    
    Args:
        org_id: Org ID
        service_type: Which service is uploading
        filename: Original filename (e.g., "invoice-1054.pdf")
        content: File bytes
        mime_type: MIME type (e.g., "application/pdf")
        uploaded_by: Cognito sub of uploader
        ttl_days: If set, file auto-deletes after this many days
    
    Returns:
        StoredFile with key, url, signed_url, metadata
    
    Side effects:
        - Uploads to S3 at key: s3://ledger/{org_id}/{service_type}/{timestamp}_{filename}
        - Logs to audit: file.uploaded with {filename, size, mime_type}
        - Records metadata in DynamoDB for querying
    
    Raises:
        FileTooLargeError: File > 100 MB
    
    Performance:
        < 1 second (depends on file size)
    
    Notes:
        - S3 bucket is private; access via signed URLs only
        - Signed URLs expire after 1 hour by default
        - Files are org-scoped (S3 key includes org_id)
        - If ttl_days is set, S3 lifecycle rule deletes file after N days
    
    Example:
        result = await core.storage.upload_file(
            org_id="acme-jewelry",
            service_type="invoicing",
            filename="invoice-1054.pdf",
            content=pdf_bytes,
            mime_type="application/pdf",
            uploaded_by=user_sub,
            ttl_days=30,  # Auto-delete after 30 days
        )
        print(result.signed_url)  # Safe to embed in email
    """
    pass

async def download_file(org_id: str, key: str) -> bytes:
    """
    Download a file from S3.
    
    Args:
        org_id: Org ID (for access control)
        key: S3 key (from StoredFile.key)
    
    Returns:
        File bytes
    
    Raises:
        NotFoundError: File doesn't exist
        PermissionError: File doesn't belong to org_id
    
    Performance:
        < 500 ms (depends on file size)
    
    Notes:
        - Enforces org_id in key to prevent cross-org access
    """
    pass

async def get_file_metadata(org_id: str, key: str) -> StoredFile:
    """
    Get metadata about a file without downloading it.
    
    Returns:
        StoredFile metadata (size, mime_type, uploaded_at, etc.)
    
    Performance:
        < 50ms (DynamoDB lookup)
    """
    pass

async def delete_file(org_id: str, key: str, deleted_by: str) -> None:
    """
    Delete a file from S3.
    
    Args:
        org_id: Org ID
        key: S3 key
        deleted_by: Cognito sub of who deleted it
    
    Side effects:
        - Deletes from S3
        - Marks as deleted in DynamoDB (soft delete for audit trail)
        - Logs to audit: file.deleted
    """
    pass

async def list_files(
    org_id: str,
    service_type: Optional[str] = None,  # Filter by service
    filename_prefix: Optional[str] = None,
) -> List[StoredFile]:
    """
    List files for an org.
    
    Args:
        org_id: Org ID
        service_type: If set, only files uploaded by this service
        filename_prefix: If set, only files matching this prefix
    
    Returns:
        List of StoredFile metadata
    
    Performance:
        < 200ms
    """
    pass

def generate_signed_url(key: str, expires_in: int = 3600) -> str:
    """
    Generate a signed URL valid for a file.
    
    Args:
        key: S3 key
        expires_in: Seconds until URL expires (default 1 hour)
    
    Returns:
        HTTPS URL safe to share with anyone (including clients)
    
    Performance:
        < 50ms
    
    Notes:
        - Used when sending invoice PDFs to clients
        - Signed URL expires automatically
    """
    pass
```

### 2.5 Audit Module

**Responsibility:** Append-only event log for all actions.

```python
# core/audit.py

from enum import Enum
from typing import Optional
from datetime import datetime

class ActionType(str, Enum):
    """Types of auditable actions."""
    # Membership
    ORG_CREATED = "org.created"
    MEMBER_ADDED = "member.added"
    MEMBER_ROLE_CHANGED = "member.role_changed"
    MEMBER_REMOVED = "member.removed"
    # Email
    EMAIL_SENT = "email.sent"
    EMAIL_BOUNCED = "email.bounced"
    EMAIL_COMPLAINED = "email.complained"
    # Files
    FILE_UPLOADED = "file.uploaded"
    FILE_DELETED = "file.deleted"
    # Settings
    SETTINGS_CHANGED = "settings.changed"
    # Service-specific (invoicing, omnichannel, etc.)
    # Services define their own action types
    # e.g., INVOICE_CREATED = "invoice.created"

@dataclass
class AuditEvent:
    """An auditable action."""
    event_id: str           # UUID
    org_id: str
    timestamp: datetime
    actor_id: str           # Cognito sub of who did it
    action: ActionType
    resource_type: str      # What was affected (user, email, file, invoice, etc.)
    resource_id: str        # ID of the resource
    metadata: dict          # Action-specific details
    # e.g., for member.role_changed:
    #   {old_role: "member", new_role: "admin"}
    # e.g., for email.sent:
    #   {to: "client@example.com", subject: "...", service_type: "invoicing"}

class AuditError(Exception):
    pass

async def log_audit(
    org_id: str,
    actor_id: str,  # Cognito sub
    action: ActionType,
    resource_type: str,
    resource_id: str,
    metadata: Optional[dict] = None,
) -> AuditEvent:
    """
    Log an auditable action.
    
    Args:
        org_id: Org ID
        actor_id: Cognito sub of who did it
        action: What action (see ActionType enum)
        resource_type: What was affected (user, email, invoice, etc.)
        resource_id: ID of the resource
        metadata: Action-specific details (optional)
    
    Returns:
        Logged AuditEvent (includes generated event_id and timestamp)
    
    Side effects:
        - Appends to audit log in DynamoDB
    
    Performance:
        < 50ms
    
    Notes:
        - Audit log is append-only (never updated or deleted)
        - Used for compliance, debugging, activity feeds
        - Services can define their own ActionType values (not just Core's)
    
    Example:
        await core.audit.log_audit(
            org_id="acme-jewelry",
            actor_id=user_sub,
            action=ActionType.MEMBER_ADDED,
            resource_type="user",
            resource_id="new-user-sub",
            metadata={"role": "member", "email": "new@example.com"},
        )
    """
    pass

async def get_audit_events(
    org_id: str,
    action_type: Optional[ActionType] = None,
    actor_id: Optional[str] = None,
    resource_id: Optional[str] = None,
    from_time: Optional[datetime] = None,
    to_time: Optional[datetime] = None,
    limit: int = 100,
) -> List[AuditEvent]:
    """
    Query audit log with filters.
    
    Args:
        org_id: Org ID (required)
        action_type: Filter by action (optional)
        actor_id: Filter by actor (optional)
        resource_id: Filter by resource ID (optional)
        from_time: Start time (optional)
        to_time: End time (optional)
        limit: Max results (default 100)
    
    Returns:
        List of AuditEvent, sorted by timestamp descending
    
    Performance:
        < 500ms (may require DynamoDB scan in worst case)
    
    Notes:
        - Org_id is always required (no cross-org queries)
        - Used for activity logs, compliance reports
    """
    pass
```

### 2.6 Settings Module

**Responsibility:** Org-level configuration, cached.

```python
# core/settings.py

from typing import Optional
from datetime import datetime

@dataclass
class OrgSettings:
    """Organization settings."""
    org_id: str
    timezone: str              # e.g., "America/Los_Angeles"
    currency: str              # e.g., "USD"
    locale: str                # e.g., "en_US"
    domain: str                # Verified domain (e.g., "acme.com")
    invoice_number_prefix: str # e.g., "INV-" for INV-1054
    next_invoice_number: int   # Auto-incrementing counter
    plan_tier: str             # "free", "pro", "team"
    sender_name: str           # Display name for emails (e.g., "Acme Jewelry")
    metadata: dict             # Free-form custom settings
    updated_at: datetime

class SettingsError(Exception):
    pass

async def get_org_settings(org_id: str) -> OrgSettings:
    """
    Get org settings.
    
    Args:
        org_id: Org ID
    
    Returns:
        OrgSettings object (defaults applied if some fields missing)
    
    Performance:
        < 50ms (Redis cache with 5min TTL, falls back to DynamoDB)
    
    Notes:
        - Returns defaults for missing fields
        - Heavily cached since read frequently
    """
    pass

async def set_org_settings(
    org_id: str,
    changes: dict,
    changed_by: str,  # Cognito sub
) -> OrgSettings:
    """
    Update org settings.
    
    Args:
        org_id: Org ID
        changes: Dict of fields to update (partial update)
        changed_by: Cognito sub of who changed it
    
    Returns:
        Updated OrgSettings
    
    Side effects:
        - Updates in DynamoDB
        - Invalidates Redis cache
        - Logs to audit: settings.changed with {old_values, new_values}
    
    Raises:
        SettingsError: Invalid field or value
    
    Performance:
        < 100ms
    
    Example:
        new_settings = await core.settings.set_org_settings(
            org_id="acme-jewelry",
            changes={
                "timezone": "America/New_York",
                "invoice_number_prefix": "INV-2026-",
            },
            changed_by=user_sub,
        )
    """
    pass

def get_next_invoice_number(org_id: str, prefix: str) -> str:
    """
    Get the next invoice number for this org.
    Atomically increments the counter.
    
    Args:
        org_id: Org ID
        prefix: Prefix from settings (e.g., "INV-")
    
    Returns:
        Next invoice number as string (e.g., "INV-1054")
    
    Performance:
        < 50ms (atomic DynamoDB update)
    
    Notes:
        - Used by Invoicing service when creating invoices
        - Guarantees no gaps or collisions
    """
    pass
```

---

## 3. Database Schemas

### 3.1 DynamoDB (Membership, Audit, Settings)

**Single-table design for membership and related data.**

#### Table: `a2z-core-membership` (Primary)

| Partition Key | Sort Key | Attributes | GSI |
|---|---|---|---|
| `PK` | `SK` | | |
| `USER#{sub}` | `METADATA` | `created_at`, `email` | N/A |
| `USER#{sub}` | `ORG#{org_id}` | `role`, `joined_at` | GSI1: `ORG#{org_id}` / `USER#{sub}` |
| `ORG#{org_id}` | `METADATA` | `name`, `owner_id`, `created_at` | N/A |
| `ORG#{org_id}` | `USER#{sub}` | `role`, `joined_at` | GSI1: `ORG#{org_id}` / `USER#{sub}` |

**GSI1 (for listing members):** `ORG#{org_id}` (PK) / `USER#{sub}` (SK)  
This allows: "Give me all users in org X" in one query.

**Queries:**
- `get_membership(user_id, org_id)`: Get `USER#{sub}` + `ORG#{org_id}` → O(1)
- `list_my_orgs(user_id)`: Query `USER#{sub}` prefix → O(1) + O(n_orgs)
- `list_org_members(org_id)`: Query GSI1 `ORG#{org_id}` → O(1) + O(n_members)

#### Table: `a2z-core-audit` (Append-only)

| Column | Type | Notes |
|---|---|---|
| `event_id` | PK | UUID, immutable |
| `org_id` | SK, GSI1-PK | Org ID |
| `timestamp` | GSI1-SK | ISO datetime (allows range queries) |
| `actor_id` | GSI2-PK | Cognito sub |
| `action` | String | ActionType enum |
| `resource_type` | String | What was affected |
| `resource_id` | String | ID of resource |
| `metadata` | Map | Free-form action details |

**Indexes:**
- **Main:** `event_id` (PK)
- **GSI1:** `org_id` (PK) / `timestamp` (SK) — "get events for org X in time range Y"
- **GSI2:** `actor_id` (PK) / `timestamp` (SK) — "get events by user X in time range Y"

**TTL:** Optional; set to delete events after 7 years (audit retention requirement).

#### Table: `a2z-core-settings` (Org config)

| Column | Type | Notes |
|---|---|---|
| `org_id` | PK | UUID |
| `timezone` | String | e.g., "America/Los_Angeles" |
| `currency` | String | e.g., "USD" |
| `locale` | String | e.g., "en_US" |
| `domain` | String | Verified domain (e.g., "acme.com") |
| `invoice_number_prefix` | String | e.g., "INV-" |
| `next_invoice_number` | Number | Counter (incremented atomically) |
| `plan_tier` | String | "free", "pro", "team" |
| `sender_name` | String | Display name in emails |
| `metadata` | Map | Free-form |
| `updated_at` | String | ISO datetime |

**No GSI needed** (only queried by org_id).

#### Table: `a2z-core-email-events` (Email delivery tracking)

| Column | Type | Notes |
|---|---|---|
| `message_id` | PK | SES MessageId |
| `org_id` | SK | Org ID (for scoping) |
| `timestamp` | LSI-SK | ISO datetime |
| `to` | String | Recipient email |
| `service_type` | String | invoicing, omnichannel, etc. |
| `status` | String | queued, sent, delivered, bounced, complained, rejected |
| `subject` | String | Email subject (for debugging) |
| `metadata` | Map | Custom SES tags (invoice_id, etc.) |

**LSI:** `org_id` (PK) / `timestamp` (SK) — for querying recent emails per org.

#### Table: `a2z-core-suppression` (Bounce/complaint list)

| Column | Type | Notes |
|---|---|---|
| `org_id` | PK | Org ID |
| `email` | SK | Email address |
| `reason` | String | "bounce" or "complaint" |
| `timestamp` | String | ISO datetime |
| `bounce_type` | String | "Transient" or "Permanent" (if bounce) |

**Query:** "Is email X on suppression list for org Y?" → O(1) get.

#### Table: `a2z-core-files` (File metadata)

| Column | Type | Notes |
|---|---|---|
| `org_id` | PK | Org ID |
| `key` | SK | S3 key (full path) |
| `filename` | String | Original filename |
| `size_bytes` | Number | File size |
| `mime_type` | String | MIME type |
| `uploaded_at` | String | ISO datetime |
| `uploaded_by` | String | Cognito sub |
| `service_type` | String | invoicing, omnichannel, etc. |
| `ttl` | String | ISO datetime when file auto-deletes (optional) |
| `is_deleted` | Boolean | Soft delete flag |

**GSI:** `org_id` / `service_type` — query files by service.

### 3.2 Postgres (Invoicing & Service-Specific Tables)

**Invoicing service tables (defined by Invoicing, not Core, but needs schema sync).**

Core creates these tables (or Invoicing does in its migration), not because Core owns them, but to establish the database. Tables are:

- `invoices` — invoice headers
- `invoice_line_items` — line items
- `invoice_payments` — payment records
- (Omni-Channel will add `conversations`, `messages`, etc.)

Core doesn't touch these tables directly. Services own them.

### 3.3 S3 Bucket Structure

```
s3://ledger/
├── {org_id}/
│   ├── invoicing/
│   │   ├── invoice-1054-pdf-20260615-120000.pdf
│   │   ├── invoice-1055-pdf-20260616-090000.pdf
│   └── omnichannel/
│       ├── message-456-image-20260615-140000.jpg
│       └── message-457-voice-20260615-141500.m4a
└── {org_id}/
    └── ...
```

**Key pattern:** `{org_id}/{service_type}/{timestamp}_{filename}`

**Access:** All private. Signed URLs only (valid 1 hour).

**Lifecycle rules:**
- Standard storage (active): 30 days
- Glacier (archive): after 30 days
- Expire: after 90 days (or as set by TTL)

---

## 4. Integration Scenarios

These are integration tests that validate Core works as a cohesive unit.

### 4.1 Scenario: Create Org & Add Members

**Test:** Org creation → list members → change role → remove member.

```python
# tests/test_integration_membership.py

@pytest.mark.asyncio
async def test_create_org_and_manage_members():
    # Owner signs up
    owner_sub = "auth0|owner123"
    owner_email = "owner@acme.com"
    
    # Create user in Core (called by Cognito post-signup lambda)
    await core.membership.create_user_if_not_exists(
        user_id=owner_sub,
        email=owner_email,
    )
    
    # Create org
    org = await core.membership.create_org(
        org_name="Acme Jewelry",
        owner_id=owner_sub,
    )
    assert org.org_id is not None
    assert org.owner_id == owner_sub
    
    # Verify owner is in org with OWNER role
    membership = await core.membership.get_membership(owner_sub, org.org_id)
    assert membership is not None
    assert membership.role == Role.OWNER
    
    # List members (should be 1)
    members = await core.membership.list_org_members(org.org_id)
    assert len(members) == 1
    assert members[0].user_id == owner_sub
    
    # Add a team member
    member_sub = "auth0|member456"
    member_email = "sarah@acme.com"
    await core.membership.create_user_if_not_exists(
        user_id=member_sub,
        email=member_email,
    )
    new_member = await core.membership.add_member(
        org_id=org.org_id,
        user_id=member_sub,
        role=Role.MEMBER,
        inviter_id=owner_sub,
    )
    assert new_member.role == Role.MEMBER
    
    # List members (should be 2)
    members = await core.membership.list_org_members(org.org_id)
    assert len(members) == 2
    
    # Change member's role to ADMIN
    updated = await core.membership.change_role(
        org_id=org.org_id,
        user_id=member_sub,
        new_role=Role.ADMIN,
        changer_id=owner_sub,
    )
    assert updated.role == Role.ADMIN
    
    # Verify audit trail
    events = await core.audit.get_audit_events(
        org_id=org.org_id,
        action_type=ActionType.MEMBER_ROLE_CHANGED,
        resource_id=member_sub,
    )
    assert len(events) >= 1
    assert events[0].metadata['new_role'] == "admin"
```

### 4.2 Scenario: Send Email & Track Delivery

**Test:** Send email → verify it was logged → simulate bounce webhook → verify suppression.

```python
@pytest.mark.asyncio
async def test_send_email_and_track_delivery():
    org_id = "test-org-456"
    
    # Send email
    result = await core.email.send_email(
        org_id=org_id,
        service_type=ServiceType.INVOICING,
        to="client@example.com",
        subject="Invoice #1054",
        body_html="<p>Amount due: $1,500</p>",
        metadata={"invoice_id": "1054"},
    )
    
    assert result.status == EmailStatus.SENT
    message_id = result.message_id
    
    # Verify it was logged in email_events
    metadata = await core.storage.get_file_metadata(org_id, message_id)
    assert metadata is not None
    
    # Simulate SES bounce notification (webhook from SNS → Lambda)
    await core.email._handle_bounce_notification(
        org_id=org_id,
        message_id=message_id,
        to="client@example.com",
        bounce_type="Permanent",
    )
    
    # Verify email is now on suppression list
    suppression = await core.email.get_suppression_list(org_id)
    assert "client@example.com" in suppression['bounced']
    
    # Try to send to same email again → should raise SuppressionListError
    with pytest.raises(core.email.SuppressionListError):
        await core.email.send_email(
            org_id=org_id,
            service_type=ServiceType.INVOICING,
            to="client@example.com",
            subject="Invoice #1055",
            body_html="...",
        )
    
    # Unsuppress
    await core.email.unsuppress_email(org_id, "client@example.com")
    
    # Now it should work
    result2 = await core.email.send_email(
        org_id=org_id,
        service_type=ServiceType.INVOICING,
        to="client@example.com",
        subject="Invoice #1055",
        body_html="...",
    )
    assert result2.status == EmailStatus.SENT
```

### 4.3 Scenario: Upload File & Access via Signed URL

**Test:** Upload PDF → get signed URL → verify access → simulate TTL expiration.

```python
@pytest.mark.asyncio
async def test_upload_and_sign_file():
    org_id = "test-org-789"
    user_sub = "auth0|user789"
    
    # Upload file
    pdf_content = b"%PDF-1.4\n..."  # Fake PDF
    result = await core.storage.upload_file(
        org_id=org_id,
        service_type="invoicing",
        filename="invoice-1054.pdf",
        content=pdf_content,
        mime_type="application/pdf",
        uploaded_by=user_sub,
        ttl_days=30,
    )
    
    assert result.key is not None
    assert result.size_bytes == len(pdf_content)
    assert result.signed_url.startswith("https://")
    
    # Download using signed URL (simulate client access)
    # (In real test, would use boto3 or HTTP request)
    downloaded = await core.storage.download_file(org_id, result.key)
    assert downloaded == pdf_content
    
    # List files for org/service
    files = await core.storage.list_files(org_id, service_type="invoicing")
    assert len(files) >= 1
    assert files[0].filename == "invoice-1054.pdf"
    
    # Verify audit trail
    events = await core.audit.get_audit_events(
        org_id=org_id,
        action_type=ActionType.FILE_UPLOADED,
        resource_id=result.key,
    )
    assert len(events) >= 1
```

### 4.4 Scenario: Org Settings & Invoice Numbering

**Test:** Set org settings → auto-increment invoice numbers.

```python
@pytest.mark.asyncio
async def test_org_settings_and_invoice_numbering():
    org_id = "test-org-999"
    user_sub = "auth0|user999"
    
    # Get default settings
    settings = await core.settings.get_org_settings(org_id)
    assert settings.timezone == "UTC"  # Default
    assert settings.currency == "USD"  # Default
    
    # Update settings
    updated = await core.settings.set_org_settings(
        org_id=org_id,
        changes={
            "timezone": "America/Los_Angeles",
            "invoice_number_prefix": "INV-2026-",
        },
        changed_by=user_sub,
    )
    assert updated.timezone == "America/Los_Angeles"
    assert updated.invoice_number_prefix == "INV-2026-"
    
    # Get next invoice number (called by Invoicing service)
    num1 = core.settings.get_next_invoice_number(org_id, "INV-2026-")
    assert num1 == "INV-2026-1"
    
    num2 = core.settings.get_next_invoice_number(org_id, "INV-2026-")
    assert num2 == "INV-2026-2"
    
    # Verify audit trail
    events = await core.audit.get_audit_events(
        org_id=org_id,
        action_type=ActionType.SETTINGS_CHANGED,
    )
    assert len(events) >= 1
```

---

## 5. Test Plan

### 5.1 Unit Tests (Isolated)

Each Core module has unit tests that mock dependencies.

```
tests/
├── unit/
│   ├── test_auth.py              # JWT validation
│   ├── test_membership.py         # DynamoDB CRUD
│   ├── test_email.py             # SES sending (mocked)
│   ├── test_storage.py           # S3 operations (mocked)
│   ├── test_audit.py             # Logging (mocked)
│   └── test_settings.py          # Config CRUD (mocked)
├── integration/
│   ├── test_integration_membership.py
│   ├── test_integration_email.py
│   ├── test_integration_storage.py
│   └── test_integration_scenarios.py
├── load/
│   ├── test_load_membership.py   # Simulate 1,000 users
│   └── test_load_email.py        # Simulate 10,000 emails
└── fixtures/
    └── conftest.py               # Shared test setup
```

**Unit test example:**

```python
# tests/unit/test_membership.py

@pytest.fixture
async def mock_dynamodb(monkeypatch):
    """Mock DynamoDB client."""
    mock = AsyncMock()
    monkeypatch.setattr("core.membership.dynamodb", mock)
    return mock

@pytest.mark.asyncio
async def test_get_membership_found(mock_dynamodb):
    mock_dynamodb.get_item.return_value = {
        'Item': {
            'user_id': 'auth0|123',
            'org_id': 'acme',
            'role': 'owner',
        }
    }
    
    result = await core.membership.get_membership('auth0|123', 'acme')
    
    assert result.role == Role.OWNER
    mock_dynamodb.get_item.assert_called_once()

@pytest.mark.asyncio
async def test_get_membership_not_found(mock_dynamodb):
    mock_dynamodb.get_item.return_value = {'Item': None}
    
    result = await core.membership.get_membership('auth0|nonexistent', 'acme')
    
    assert result is None
```

### 5.2 Integration Tests (Real Resources)

Tests that use real (or local) DynamoDB, S3, SES.

**Setup:** Use LocalStack or AWS test containers to provide real-ish services.

```python
# tests/integration/conftest.py

@pytest.fixture(scope="session")
async def localstack():
    """Start LocalStack with DynamoDB, S3."""
    # Start container
    # Create tables
    # Create bucket
    yield stack
    # Cleanup

@pytest.fixture
async def org_id():
    """Create a test org."""
    org = await core.membership.create_org(
        org_name="Test Org",
        owner_id="test-user-123",
    )
    yield org.org_id
    # Cleanup
```

### 5.3 Load Tests

Verify Core handles production load.

```python
# tests/load/test_load_membership.py

@pytest.mark.asyncio
@pytest.mark.load
async def test_membership_queries_under_load():
    """1,000 concurrent get_membership queries."""
    org_id = "load-test-org"
    
    # Create 100 users in org
    users = [f"user-{i}" for i in range(100)]
    for user in users:
        await core.membership.create_user_if_not_exists(user, f"{user}@example.com")
        await core.membership.add_member(org_id, user, Role.MEMBER, "owner")
    
    # Execute 1,000 concurrent queries
    start = time.time()
    tasks = [
        core.membership.get_membership(random.choice(users), org_id)
        for _ in range(1000)
    ]
    results = await asyncio.gather(*tasks)
    elapsed = time.time() - start
    
    # Verify
    assert all(r is not None for r in results)
    assert elapsed < 10  # Should complete in < 10 seconds
    avg_latency = elapsed / 1000
    print(f"Average latency: {avg_latency*1000:.2f}ms")
    assert avg_latency < 0.050  # < 50ms per query
```

### 5.4 Performance Targets

| Operation | Target Latency | Why |
|---|---|---|
| `get_membership` | < 50ms | Single DynamoDB get |
| `list_org_members` | < 200ms | DynamoDB query (possibly large result set) |
| `send_email` | < 500ms | SES async call (we wait for acceptance) |
| `upload_file` | < 1s | Depends on file size |
| `log_audit` | < 50ms | DynamoDB put (fire & forget) |
| `get_org_settings` | < 50ms | Redis cache (fallback DynamoDB) |

### 5.5 Test Execution

```bash
# Unit tests (fast, no AWS)
pytest tests/unit -v

# Integration tests (need LocalStack)
pytest tests/integration -v

# Load tests (slow)
pytest tests/load -v -m load

# All tests
pytest tests -v --cov=core --cov-report=term-missing
```

---

## 6. Error Handling

Every Core module defines and raises specific errors. Services catch and handle them.

```python
# core/exceptions.py

class CoreError(Exception):
    """Base exception for all Core errors."""
    status_code: int = 500

class AuthError(CoreError):
    status_code = 401

class InvalidTokenError(AuthError):
    """JWT invalid or expired."""
    pass

class MissingTokenError(AuthError):
    """No JWT provided."""
    pass

class MembershipError(CoreError):
    status_code = 400

class NotFoundError(MembershipError):
    status_code = 404

class AlreadyExistsError(MembershipError):
    status_code = 409

class EmailError(CoreError):
    status_code = 400

class SuppressionListError(EmailError):
    status_code = 400

class RateLimitError(EmailError):
    status_code = 429

class StorageError(CoreError):
    status_code = 400

class FileTooLargeError(StorageError):
    pass

class AuditError(CoreError):
    status_code = 500
```

**Service error handling example:**

```python
# app/services/invoicing/handlers.py

@app.post("/invoices/send")
async def send_invoice(request: Request, invoice_id: str, org_id: str):
    try:
        user = auth.get_current_user_from_request(request)
        
        # Check membership
        membership = await core.membership.get_membership(user['sub'], org_id)
        if not membership:
            raise HTTPException(status_code=403, detail="Not in org")
        
        # Send email
        result = await core.email.send_email(...)
        
    except core.auth.MissingTokenError:
        raise HTTPException(status_code=401, detail="No token")
    except core.email.SuppressionListError as e:
        raise HTTPException(status_code=400, detail="Email on suppression list")
    except core.email.RateLimitError:
        raise HTTPException(status_code=429, detail="Rate limit exceeded")
```

---

## 7. Security Model

### 7.1 Tenancy Isolation

**Principle:** Every query enforces org_id scoping.

**Enforcement:**
- Membership queries: PK includes org_id
- Audit log: PK includes org_id
- Settings: PK is org_id
- Email events: PK/SK includes org_id
- Files: S3 key includes org_id; DynamoDB query scoped to org_id
- RLS in Postgres (future): `WHERE org_id = $1`

**Verification:** Code review ensures no query omits org_id scoping.

### 7.2 Authentication

**Flow:**
1. Client (mobile or web) signs up via Cognito UI
2. Cognito post-signup Lambda calls `core.membership.create_user_if_not_exists(sub, email)`
3. User gets JWT
4. On every request, service calls `auth.get_current_user_from_request(request)` to validate JWT
5. JWT is never trusted without signature verification

**No password management:** Cognito handles it.

### 7.3 Authorization

**Until Permissions service exists:** Services hardcode role checks.

```python
# Example in Invoicing service
user = auth.get_current_user_from_request(request)
membership = await core.membership.get_membership(user['sub'], org_id)

if membership is None:
    raise PermissionError("User not in org")

# Only OWNER/ADMIN can change invoice number prefix
if membership.role not in [Role.OWNER, Role.ADMIN]:
    raise PermissionError("Insufficient role")
```

### 7.4 Secrets Management

**SES credentials:** AWS IAM role for ECS task (no explicit keys).  
**DynamoDB access:** IAM role (no keys).  
**S3 access:** IAM role (no keys).  
**Cognito keys:** Cached from Cognito JWKS endpoint (public).

**No secrets in code or environment variables** (except for tests).

### 7.5 HTTPS Everywhere

All external traffic (API calls) is HTTPS. SES, Cognito, and Bedrock are AWS-internal or HTTPS.

---

## 8. Performance & Scaling

### 8.1 Expected Load (Year 1)

| Month | Orgs | Users | Emails/day | Emails/mo |
|---|---|---|---|---|
| Month 1 | 5 | 10 | 50 | 1K |
| Month 3 | 50 | 150 | 2K | 60K |
| Month 6 | 200 | 500 | 10K | 300K |
| Month 12 | 1K | 2K | 100K | 3M |

### 8.2 DynamoDB Sizing

| Table | RCU (peak) | WCU (peak) | Notes |
|---|---|---|---|
| Membership | 10 | 5 | Mostly reads; occasional adds |
| Audit | 5 | 50 | Heavy writes; moderate reads |
| Settings | 5 | 1 | Rarely updated |
| Email Events | 20 | 50 | Heavy writes (all emails); some reads |
| Suppression | 10 | 5 | Mostly reads; occasional adds |
| Files | 5 | 10 | Metadata reads; file uploads logged |

**Billing (on-demand):** ~$30/mo for 1K orgs.

### 8.3 SES Throughput

SES can send ~14 emails/second per domain. We won't hit that in year 1. At 3M emails/month, that's ~1,400 emails/day, which is trivial.

### 8.4 Caching (Redis)

**Heavily cached:**
- Org settings (5min TTL)
- Cognito public keys (24h TTL)
- Suppression list (for requests in same minute, 1min TTL)

**Expected:** < 100ms latency for 99th percentile requests.

---

## 9. Deployment & Operations

### 9.1 Infrastructure Setup (Phase 0)

```bash
# Terragrunt/Terraform
terragrunt run-all apply

# Creates:
# - VPC, subnets, security groups
# - RDS Postgres
# - DynamoDB tables
# - S3 bucket + lifecycle rules
# - SES (already created, just configure)
# - Cognito User Pool
# - CloudWatch logs
# - IAM roles/policies
```

### 9.2 Local Development

```bash
# Docker Compose for LocalStack
docker-compose up -d

# LocalStack provides:
# - DynamoDB
# - S3
# - SES (mock)

# Run tests
pytest tests/integration -v
```

### 9.3 Monitoring & Alerts

**CloudWatch Dashboards:**
- DynamoDB read/write latency
- SES send rate, bounces, complaints
- Lambda errors
- API latency (p50, p99)

**Alerts:**
- DynamoDB throttling
- SES bounce rate > 5%
- Email delivery failures
- Audit log lag

---

## 10. Timeline & Success Criteria

### Phase 0 (Days 1–3): Design & Setup

**Deliverables:**
- [x] This design document (finalized)
- [ ] AWS resources provisioned (RDS, DynamoDB, S3, SES, Cognito)
- [ ] Terragrunt code for all infrastructure
- [ ] LocalStack Docker Compose for dev

**Success:** Can run `pytest tests/integration -v` and have all tests pass (against LocalStack).

### Phase 1 (Weeks 1–2): Core Implementation

**Deliverables:**
- [ ] FastAPI skeleton with auth middleware
- [ ] All 6 Core modules (auth, membership, email, storage, audit, settings) — fully implemented
- [ ] All unit tests passing (coverage > 90%)
- [ ] All integration tests passing
- [ ] Load tests passing (latency targets met)

**Success:** 
- Membership queries < 50ms
- Email sends < 500ms
- Audit logging < 50ms
- Zero cross-org data leaks

### Phase 2 (Week 3): Invoicing Integration

**Deliverables:**
- [ ] Invoicing service uses Core correctly
- [ ] End-to-end test: create invoice → send email → receive webhook → mark paid

**Success:** Invoicing service works without modifying Core.

### Phase 3 (Week 4): Omni-Channel Integration

**Deliverables:**
- [ ] Omni-Channel service uses Core correctly
- [ ] Multi-service scenario: invoice sent → conversation updated

**Success:** Core works for multiple services simultaneously.

### Phase 4 (Week 5): Polish & Launch Prep

**Deliverables:**
- [ ] Performance tuning
- [ ] Security review
- [ ] Documentation complete
- [ ] Runbooks for on-call

**Success:** Core is production-ready.

---

## 11. Known Risks & Mitigations

| Risk | Mitigation |
|---|---|
| DynamoDB throttling if query patterns wrong | Load test early; use on-demand until patterns clear |
| SES reputation damaged by bad actors | Rate limiting + suppression list; strict org isolation |
| Cross-org data leak in queries | Code review every Core query; integration tests validate isolation |
| JWT validation cached incorrectly | Re-fetch keys on cache miss; 24h TTL is safe |
| Org settings stale due to cache | Invalidate Redis on update; 5min TTL is acceptable |
| Secrets leak in logs | Never log auth tokens, org IDs, email addresses |

---

## 12. Future Extensibility

**Core is designed to support:**
- Permission service (role → action matrix)
- Billing service (usage tracking → invoicing)
- API keys for service-to-service auth
- Webhook delivery (Core publishes events; services subscribe)
- Multi-region (future; requires key changes to partition schemes)

**Core will NOT change:**
- Tenancy model (org-based)
- Membership API (locked after Phase 1)
- Email abstraction (locked after Phase 1)
- Audit trail format (append-only forever)

---

## Appendix A: Deployment Checklist

- [ ] AWS account set up
- [ ] Cognito User Pool created
- [ ] SES domain verified (at least one org domain for testing)
- [ ] RDS Postgres provisioned
- [ ] DynamoDB tables created (with correct indexes)
- [ ] S3 bucket created with lifecycle rules
- [ ] IAM roles & policies for ECS task
- [ ] CloudWatch log groups created
- [ ] VPC configured (security groups, endpoints)
- [ ] Secrets Manager (if using; else rely on IAM roles)
- [ ] Terragrunt code reviewed and working

---

## Appendix B: API Response Examples

### Create Org

```json
POST /core/orgs
{
  "name": "Acme Jewelry"
}

201 Created
{
  "org_id": "acme-jewelry-uuid",
  "name": "Acme Jewelry",
  "owner_id": "auth0|123",
  "created_at": "2026-01-15T10:30:00Z"
}
```

### Add Member

```json
POST /core/orgs/{org_id}/members
{
  "user_id": "auth0|456",
  "role": "member"
}

201 Created
{
  "user_id": "auth0|456",
  "org_id": "acme-jewelry-uuid",
  "role": "member",
  "joined_at": "2026-01-15T10:35:00Z"
}
```

### Send Email

```json
POST /core/email/send
{
  "org_id": "acme-jewelry-uuid",
  "service_type": "invoicing",
  "to": "client@example.com",
  "subject": "Invoice #1054",
  "body_html": "<p>Amount due: $1,500</p>",
  "metadata": {"invoice_id": "1054"}
}

200 OK
{
  "message_id": "0000014a-1f5a-44ae-9d51-5e5f48f1e6b3",
  "status": "sent",
  "timestamp": "2026-01-15T10:40:00Z"
}
```

---

**End of Core Design & Test Plan**
