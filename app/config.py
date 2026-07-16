"""Central configuration for A2Z Core.

Loads settings from environment (and a local `.env`) via pydantic-settings.
Exposes a single cached `settings()` accessor plus two registries that the
design doc asks to keep in one place:

  * ``TABLES``         — logical name -> DynamoDB table name
  * ``RATE_LIMITS``    — action -> (limit, window_seconds) defaults

Keeping these here prevents services from inventing their own table names or
rate limits (CLAUDE.md §7, §9).
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Process-wide configuration, sourced from env vars / `.env`."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- Runtime ---
    env: str = Field(default="local", alias="A2Z_ENV")
    log_level: str = Field(default="INFO", alias="A2Z_LOG_LEVEL")
    aws_region: str = Field(default="us-east-1", alias="AWS_REGION")

    # When set, boto3 clients target LocalStack. Empty/None => real AWS.
    aws_endpoint_url: str | None = Field(default=None, alias="AWS_ENDPOINT_URL")

    # --- Redis ---
    redis_url: str = Field(default="redis://localhost:6379/0", alias="REDIS_URL")

    # --- Postgres (shared instance, service-owned schemas -- Omni-Channel first) ---
    database_url: str = Field(
        default="postgresql+asyncpg://a2z:a2z-local-dev-only@localhost:5432/a2z",
        alias="DATABASE_URL",
    )

    # --- DynamoDB table names ---
    ddb_membership_table: str = Field(default="a2z-core-membership", alias="DDB_MEMBERSHIP_TABLE")
    ddb_audit_table: str = Field(default="a2z-core-audit", alias="DDB_AUDIT_TABLE")
    ddb_settings_table: str = Field(default="a2z-core-settings", alias="DDB_SETTINGS_TABLE")
    ddb_email_events_table: str = Field(
        default="a2z-core-email-events", alias="DDB_EMAIL_EVENTS_TABLE"
    )
    ddb_suppression_table: str = Field(
        default="a2z-core-suppression", alias="DDB_SUPPRESSION_TABLE"
    )
    ddb_files_table: str = Field(default="a2z-core-files", alias="DDB_FILES_TABLE")

    # --- S3 / EventBridge ---
    s3_bucket: str = Field(default="a2z-ledger", alias="S3_BUCKET")
    event_bus_name: str = Field(default="a2z-bus", alias="EVENT_BUS_NAME")

    # --- Cognito ---
    cognito_user_pool_id: str = Field(default="", alias="COGNITO_USER_POOL_ID")
    cognito_region: str = Field(default="us-east-1", alias="COGNITO_REGION")
    cognito_app_client_id: str = Field(default="", alias="COGNITO_APP_CLIENT_ID")

    # --- Test token signing (HS256). Never used when env == "prod". ---
    test_jwt_secret: str = Field(
        default="local-development-only-not-a-real-secret", alias="TEST_JWT_SECRET"
    )

    @property
    def is_prod(self) -> bool:
        return self.env == "prod"

    @property
    def cognito_issuer(self) -> str:
        """The `iss` claim Cognito stamps on its tokens."""
        return (
            f"https://cognito-idp.{self.cognito_region}.amazonaws.com/{self.cognito_user_pool_id}"
        )

    @property
    def tables(self) -> dict[str, str]:
        """Logical name -> physical DynamoDB table name."""
        return {
            "membership": self.ddb_membership_table,
            "audit": self.ddb_audit_table,
            "settings": self.ddb_settings_table,
            "email_events": self.ddb_email_events_table,
            "suppression": self.ddb_suppression_table,
            "files": self.ddb_files_table,
        }


@lru_cache(maxsize=1)
def settings() -> Settings:
    """Return the cached, process-wide Settings singleton."""
    return Settings()


# Registry of default rate limits: action -> (limit, window_seconds).
# Services read these instead of hardcoding literals (CLAUDE.md §7).
RATE_LIMITS: dict[str, tuple[int, int]] = {
    "email.send": (50, 3600),  # 50 / hour / org
    "ai.parse.user": (30, 60),  # 30 / min / user (future: Invoicing)
    "ai.parse.org": (500, 86400),  # 500 / day / org (future: Invoicing)
    "omnichannel.whatsapp.send": (80, 1),  # Meta pair-rate ceiling; tune per tier
}

# Cost note (CLAUDE.md §10): revisit DynamoDB provisioned capacity only if
# monthly spend crosses ~$100. On-demand is the MVP default everywhere.
DDB_BILLING_MODE = "PAY_PER_REQUEST"
