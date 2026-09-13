"""The Postgres backup restore drill (DEPLOYMENT.md's backup runbook).

Proves that a `pg_dump` backup taken by
``infra/modules/ec2-simple/files/backup-postgres.sh`` can actually be
restored by its companion ``restore-postgres.sh`` -- the gap this whole
effort exists to close (see those two scripts' headers, and
``docs/architecture/single-box-mvp.md``'s note that the nightly cron "has
not been restore-tested").

This test runs the REAL scripts as subprocesses against two throwaway
Postgres databases -- never the shared ``a2z`` database
``tests/integration/omnichannel/conftest.py`` and
``tests/integration/invoicing/conftest.py`` reset per test session. That is
deliberate, not incidental:

- Dumping the live ``a2z`` database would make this test's result depend on
  test ordering, since those two conftests' autouse fixtures drop/recreate
  their schemas mid-session.
- "Restore into a fresh database, verify, then cut over" is also the actual
  supported operational procedure (see ``restore-postgres.sh``'s header) --
  so the drill exercises the real recommended path, not a shortcut.

Why a subprocess and not a pure-Python round-trip: the thing actually in
doubt is whether the deployed ``pg_dump | gzip`` -> ``psql
--single-transaction`` pipeline works end to end, not whether SQLAlchemy can
copy rows between two engines. A logical copy would prove nothing about the
script that runs at 3am on the box.

Skips (does not fail) when ``pg_dump``/``psql`` aren't on PATH -- CI installs
``postgresql-client`` explicitly (see ``.github/workflows/ci.yml``); this
lets the rest of the suite run on a laptop without it.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import uuid
from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select, text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from app.config import settings
from app.services.invoicing.models import Base as InvoicingBase
from app.services.invoicing.models import Invoice, InvoiceLineItem
from app.services.omnichannel.models import Base as OmnichannelBase
from app.services.omnichannel.models import ChannelIdentity, Conversation, Message, Template

pytestmark = [
    pytest.mark.integration,
    pytest.mark.postgres,
    pytest.mark.skipif(
        shutil.which("pg_dump") is None or shutil.which("psql") is None,
        reason="postgresql-client (pg_dump/psql) not installed",
    ),
]

_FILES_DIR = Path(__file__).resolve().parents[3] / "infra" / "modules" / "ec2-simple" / "files"
BACKUP_SCRIPT = _FILES_DIR / "backup-postgres.sh"
RESTORE_SCRIPT = _FILES_DIR / "restore-postgres.sh"

# The real Alembic heads (app/services/{omnichannel,invoicing}/migrations/versions/) --
# `Base.metadata.create_all` doesn't create `alembic_version`, so the drill
# seeds it explicitly with these so the restored database looks like what a
# real dump actually carries. See DEPLOYMENT.md's runbook for why a restored
# dump's alembic_version can be behind the app's expected head.
OMNICHANNEL_HEAD = "0004_message_subject"
INVOICING_HEAD = "310e8122e78e"


def _base_url() -> URL:
    return make_url(settings().database_url)


async def _recreate_database(name: str) -> None:
    """DROP + CREATE ``name`` outside any transaction block -- required,
    since CREATE/DROP DATABASE cannot run inside one. WITH (FORCE) (PG13+)
    so a lingering connection from a previous run can't block this."""
    admin_url = _base_url().set(database="postgres")
    admin = create_async_engine(admin_url, isolation_level="AUTOCOMMIT", poolclass=NullPool)
    try:
        async with admin.connect() as conn:
            await conn.exec_driver_sql(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
            await conn.exec_driver_sql(f'CREATE DATABASE "{name}"')
    finally:
        await admin.dispose()


async def _drop_database(name: str) -> None:
    admin_url = _base_url().set(database="postgres")
    admin = create_async_engine(admin_url, isolation_level="AUTOCOMMIT", poolclass=NullPool)
    try:
        async with admin.connect() as conn:
            await conn.exec_driver_sql(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
    finally:
        await admin.dispose()


def _engine_for(database: str) -> AsyncEngine:
    return create_async_engine(_base_url().set(database=database), poolclass=NullPool)


def _script_env(*, backup_dir: Path, database: str = "") -> dict[str, str]:
    """Environment driving backup-postgres.sh / restore-postgres.sh directly
    against the local Postgres, bypassing the box's docker-compose exec
    (``PG_EXEC=""``) -- see each script's own header for the full env-var
    contract this relies on. Host/port/password ride on the PGHOST/PGPORT/
    PGPASSWORD libpq conventions since neither script passes -h/-p, which is
    exactly what lets them run unchanged inside a docker-compose exec on the
    real box (no host/port needed there -- it connects over the container's
    own loopback)."""
    base = _base_url()
    env = dict(os.environ)
    env.update(
        {
            "PG_EXEC": "",
            "PGHOST": base.host or "localhost",
            "PGPORT": str(base.port or 5432),
            "PGUSER": base.username or "a2z",
            "PGPASSWORD": base.password or "",
            "S3_BUCKET": "",  # skip the upload step entirely -- no AWS in this test
            "BACKUP_DIR": str(backup_dir),
        }
    )
    if database:
        env["PGDATABASE"] = database
    return env


async def _seed(engine: AsyncEngine) -> dict[str, Any]:
    """Create both schemas' full DDL via the real models and seed rows
    chosen to expose specific restore failure modes -- see the assertions
    in the test for what each one catches."""
    async with engine.begin() as conn:
        await conn.exec_driver_sql("CREATE SCHEMA IF NOT EXISTS omnichannel")
        await conn.exec_driver_sql("CREATE SCHEMA IF NOT EXISTS invoicing")
        await conn.run_sync(OmnichannelBase.metadata.create_all)
        await conn.run_sync(InvoicingBase.metadata.create_all)
        # Base.metadata.create_all doesn't produce alembic_version -- both
        # real Alembic envs use version_table_schema, so seed it by hand.
        await conn.exec_driver_sql(
            "CREATE TABLE omnichannel.alembic_version "
            "(version_num VARCHAR(32) NOT NULL PRIMARY KEY)"
        )
        await conn.exec_driver_sql(
            "CREATE TABLE invoicing.alembic_version (version_num VARCHAR(32) NOT NULL PRIMARY KEY)"
        )
        await conn.execute(
            text("INSERT INTO omnichannel.alembic_version VALUES (:v)"),
            {"v": OMNICHANNEL_HEAD},
        )
        await conn.execute(
            text("INSERT INTO invoicing.alembic_version VALUES (:v)"),
            {"v": INVOICING_HEAD},
        )

    non_utc = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone(timedelta(hours=5, minutes=30)))
    variables = {
        "greeting": "héllo wörld 👋",
        "nested": {"list": [1, 2, 3], "empty": {}, "null": None},
    }
    seed: dict[str, Any] = {
        "org_a": "org-" + uuid.uuid4().hex[:8],
        "org_b": "org-" + uuid.uuid4().hex[:8],
        "last_message_at": non_utc,
        "template_variables": variables,
        "quantity": Decimal("1.500"),
        "total_cents": 4294967296,  # beyond int32 -- catches BigInteger truncation
        "dedup_key": "dedupe-" + uuid.uuid4().hex[:8],
    }

    async with AsyncSession(engine) as session:
        identity_a = ChannelIdentity(
            org_id=seed["org_a"], channel_type="email", external_id="a@example.com"
        )
        identity_b = ChannelIdentity(
            org_id=seed["org_b"], channel_type="email", external_id="b@example.com"
        )
        session.add_all([identity_a, identity_b])
        await session.flush()

        conv_a = Conversation(
            org_id=seed["org_a"],
            customer_identity_id=identity_a.id,
            last_message_at=non_utc,
        )
        conv_b = Conversation(
            org_id=seed["org_b"],
            customer_identity_id=identity_b.id,
            last_message_at=None,  # proves NULL survives, not just non-null values
        )
        session.add_all([conv_a, conv_b])
        await session.flush()

        msg_a1 = Message(
            org_id=seed["org_a"],
            conversation_id=conv_a.id,
            direction="inbound",
            channel_type="email",
            external_message_id="ext-" + uuid.uuid4().hex[:8],
            body_text="hello from org a",
        )
        msg_a2 = Message(
            org_id=seed["org_a"],
            conversation_id=conv_a.id,
            direction="outbound",
            channel_type="email",
            external_message_id="ext-" + uuid.uuid4().hex[:8],
            body_text="reply",
            client_dedup_key=seed["dedup_key"],
        )
        msg_b1 = Message(
            org_id=seed["org_b"],
            conversation_id=conv_b.id,
            direction="inbound",
            channel_type="email",
            external_message_id="ext-" + uuid.uuid4().hex[:8],
            body_text="hello from org b",
        )
        session.add_all([msg_a1, msg_a2, msg_b1])

        session.add(
            Template(
                org_id=seed["org_a"],
                name="greeting",
                body="Hi {{name}}",
                variables=variables,
            )
        )

        invoice_a = Invoice(
            org_id=seed["org_a"],
            invoice_number="INV-2026-000001",
            customer_email="customer@example.com",
            customer_name="Customer A",
            invoice_date=non_utc.date(),
            total_cents=seed["total_cents"],
            created_by="user-1",
        )
        invoice_b = Invoice(
            org_id=seed["org_b"],
            invoice_number="INV-2026-000002",
            customer_email="customer2@example.com",
            customer_name="Customer B",
            invoice_date=non_utc.date(),
            total_cents=1000,
            created_by="user-2",
        )
        session.add_all([invoice_a, invoice_b])
        await session.flush()

        session.add(
            InvoiceLineItem(
                org_id=seed["org_a"],
                invoice_id=invoice_a.id,
                description="Consulting",
                quantity=seed["quantity"],
                unit_price_cents=10000,
                amount_cents=15000,
            )
        )

        seed["message_a1_external_id"] = msg_a1.external_message_id
        seed["invoice_a_id"] = invoice_a.id
        seed["conversation_a_id"] = conv_a.id
        await session.commit()

    return seed


def _run(script: Path, args: list[str], env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["bash", str(script), *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, (
        f"{script.name} failed (exit {result.returncode})\n"
        f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
    )
    return result


@pytest.fixture
async def drill_databases() -> AsyncIterator[tuple[str, str]]:
    src = "a2z_drill_src_" + uuid.uuid4().hex[:8]
    tgt = "a2z_drill_tgt_" + uuid.uuid4().hex[:8]
    await _recreate_database(src)
    try:
        yield src, tgt
    finally:
        await _drop_database(src)
        await _drop_database(tgt)


async def test_restore_drill_round_trips_data_faithfully(
    drill_databases: tuple[str, str], tmp_path: Path
) -> None:
    """Seed both schemas in a throwaway source DB, dump it with the real
    backup script, restore it with the real restore script into a second
    throwaway DB, and check the result is faithful -- not just present."""
    src_db, tgt_db = drill_databases

    src_engine = _engine_for(src_db)
    try:
        seed = await _seed(src_engine)
    finally:
        await src_engine.dispose()

    backup_dir = tmp_path / "backup"
    backup_dir.mkdir()
    dump_result = _run(BACKUP_SCRIPT, [], _script_env(backup_dir=backup_dir, database=src_db))
    dump_files = list(backup_dir.glob("*.sql.gz"))
    assert len(dump_files) == 1, (
        f"expected exactly one dump file, found: {dump_files}\n{dump_result.stdout}"
    )
    dump_file = dump_files[0]
    assert dump_file.stat().st_size > 0

    restore_result = _run(
        RESTORE_SCRIPT,
        [str(dump_file), tgt_db],
        _script_env(backup_dir=backup_dir),
    )
    # The operator-facing summary the script prints -- both alembic heads
    # should be visible in it even before the SQL assertions below confirm
    # them independently.
    assert OMNICHANNEL_HEAD in restore_result.stdout
    assert INVOICING_HEAD in restore_result.stdout

    tgt_engine = _engine_for(tgt_db)
    try:
        async with AsyncSession(tgt_engine) as session:
            # --- alembic_version: proves a restored dump carries the schema
            # version it was taken at, which the runbook's migration-skew
            # step depends on. ---
            omni_version = (
                await session.execute(text("SELECT version_num FROM omnichannel.alembic_version"))
            ).scalar_one()
            inv_version = (
                await session.execute(text("SELECT version_num FROM invoicing.alembic_version"))
            ).scalar_one()
            assert omni_version == OMNICHANNEL_HEAD
            assert inv_version == INVOICING_HEAD

            # --- per-org isolation survives a restore, not just total counts. ---
            count_a = (
                (await session.execute(select(Message).where(Message.org_id == seed["org_a"])))
                .scalars()
                .all()
            )
            count_b = (
                (await session.execute(select(Message).where(Message.org_id == seed["org_b"])))
                .scalars()
                .all()
            )
            assert len(count_a) == 2
            assert len(count_b) == 1

            # --- Numeric(12,3) quantity: string comparison catches scale
            # loss (e.g. "1.5" instead of "1.500"), not just value drift. ---
            line_item = (
                await session.execute(
                    select(InvoiceLineItem).where(
                        InvoiceLineItem.invoice_id == seed["invoice_a_id"]
                    )
                )
            ).scalar_one()
            assert str(line_item.quantity) == "1.500"

            # --- BigInteger total_cents beyond int32 range: catches silent
            # truncation to a 32-bit column. ---
            invoice_a = await session.get(Invoice, seed["invoice_a_id"])
            assert invoice_a is not None
            assert invoice_a.total_cents == 4294967296

            # --- DateTime(timezone=True): a non-UTC offset round-trips to
            # the same instant, and NULL stays NULL (not coerced to a
            # default). Aware-datetime equality compares instants, so this
            # holds regardless of what tzinfo asyncpg hands back. ---
            conv_a = await session.get(Conversation, seed["conversation_a_id"])
            assert conv_a is not None
            assert conv_a.last_message_at == seed["last_message_at"]

            conv_b_row = (
                await session.execute(
                    select(Conversation).where(Conversation.org_id == seed["org_b"])
                )
            ).scalar_one()
            assert conv_b_row.last_message_at is None

            # --- JSONB: nested structures, non-ASCII, and an empty dict all
            # round-trip exactly (JSON damage is usually silent otherwise). ---
            template = (
                await session.execute(select(Template).where(Template.org_id == seed["org_a"]))
            ).scalar_one()
            assert template.variables == seed["template_variables"]

            # --- Structural proof #1: uq_message_idempotency
            # (channel_type, external_message_id) must still reject a
            # duplicate -- proves the constraint survived, not just the
            # rows it protects. ---
            with pytest.raises(IntegrityError):
                async with session.begin_nested():
                    session.add(
                        Message(
                            org_id=seed["org_a"],
                            conversation_id=seed["conversation_a_id"],
                            direction="inbound",
                            channel_type="email",
                            external_message_id=seed["message_a1_external_id"],
                            body_text="duplicate webhook retry",
                        )
                    )
                    await session.flush()

            # --- Structural proof #2: the partial unique index on
            # (org_id, conversation_id, client_dedup_key) where non-null
            # must still reject a duplicate key. ---
            with pytest.raises(IntegrityError):
                async with session.begin_nested():
                    session.add(
                        Message(
                            org_id=seed["org_a"],
                            conversation_id=seed["conversation_a_id"],
                            direction="outbound",
                            channel_type="email",
                            external_message_id="ext-" + uuid.uuid4().hex[:8],
                            body_text="double-submitted reply",
                            client_dedup_key=seed["dedup_key"],
                        )
                    )
                    await session.flush()
    finally:
        await tgt_engine.dispose()
