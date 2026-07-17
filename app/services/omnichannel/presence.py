"""Redis-backed agent presence (§5.3).

Live status lives in Redis (shared cluster, key ``presence:{org_id}:{user_id}``,
~60s heartbeat TTL so a closed laptop decays to offline within a minute) —
this is the hot path routing reads. The Postgres ``presence`` row
(``models.Presence``) is a backup/audit write only; routing never reads it —
it exists so an operator can see "who was online last" even after a Redis
flush.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.core import clients
from app.services.omnichannel import db
from app.services.omnichannel.models import Presence

_HEARTBEAT_TTL_SECONDS = 60


def _key(org_id: str, user_id: str) -> str:
    return f"presence:{org_id}:{user_id}"


async def heartbeat(org_id: str, user_id: str, status: str = "online") -> None:
    """Record a presence heartbeat.

    Call this periodically (every 20-30s) from a connected client; a closed
    tab/laptop simply stops renewing the TTL and decays to offline within
    ``_HEARTBEAT_TTL_SECONDS`` — no explicit "going offline" signal needed.
    """
    redis = clients.redis_client()
    await redis.set(_key(org_id, user_id), status, ex=_HEARTBEAT_TTL_SECONDS)

    now = datetime.now(timezone.utc)
    async with db.get_session_context() as session:
        stmt = pg_insert(Presence).values(
            org_id=org_id, user_id=user_id, status=status, updated_at=now
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["org_id", "user_id"],
            set_={"status": stmt.excluded.status, "updated_at": stmt.excluded.updated_at},
        )
        await session.execute(stmt)


async def get_status(org_id: str, user_id: str) -> str:
    """Return "online" (or whatever status was last set) if the heartbeat
    hasn't expired, else "offline"."""
    redis = clients.redis_client()
    status = await redis.get(_key(org_id, user_id))
    return status or "offline"


async def list_online_agents(org_id: str, candidate_user_ids: list[str]) -> list[str]:
    """Filter a candidate list down to those with a live Redis heartbeat.

    Order is not meaningful — callers needing a specific ordering (e.g.
    round-robin's "waited longest") apply their own sort.
    """
    if not candidate_user_ids:
        return []
    redis = clients.redis_client()
    keys = [_key(org_id, uid) for uid in candidate_user_ids]
    statuses = await redis.mget(keys)
    return [uid for uid, status in zip(candidate_user_ids, statuses, strict=True) if status]
