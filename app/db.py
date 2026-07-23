"""Mongo (Motor) connection and index bootstrap.

Collections (see spec §5):
  raw_messages   — raw Telegram updates, TTL-expired on `date`
  tasks          — extracted tasks (permanent), unique on dedupHash
  chat_project_map — chat -> TickTick project binding
  chat_state     — per-chat processing cursor
  chat_summary   — long-term per-chat memory (permanent)
  bot_state      — owner id, business connection id, TickTick URL override

Audit/restore plane (Phase 0 — see docs logging-restore design):
  audit_log       — append-only mutation trail, TTL-expired on `ts` (~90d)
  state_snapshots — last-known state per object, keyed (server, targetId),
                    for out-of-band diffing (no TTL — pruned by inactivity)
  sync_cursors    — per-provider delta cursor (sync token / checkpoint)
"""
from __future__ import annotations

import logging

from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase
from pymongo import ASCENDING, DESCENDING, IndexModel
from pymongo.errors import OperationFailure

from .config import get_settings

logger = logging.getLogger(__name__)

_client: AsyncIOMotorClient | None = None
_db: AsyncIOMotorDatabase | None = None


def get_db() -> AsyncIOMotorDatabase:
    if _db is None:
        raise RuntimeError("Database not initialised; call init_db() first.")
    return _db


async def init_db() -> AsyncIOMotorDatabase:
    """Connect and ensure indexes. Idempotent."""
    global _client, _db
    settings = get_settings()
    _client = AsyncIOMotorClient(settings.mongo_url, tz_aware=True)
    _db = _client[settings.mongo_db]
    await _ensure_indexes(_db, settings.raw_ttl_seconds, settings.audit_ttl_seconds)
    logger.info("Mongo connected: db=%s", settings.mongo_db)
    return _db


async def close_db() -> None:
    global _client
    if _client is not None:
        _client.close()
        _client = None


async def _ensure_indexes(
    db: AsyncIOMotorDatabase,
    raw_ttl_seconds: int,
    audit_ttl_seconds: int = 7776000,
) -> None:
    await db.raw_messages.create_indexes([IndexModel([("chatId", ASCENDING), ("date", ASCENDING)])])
    # TTL index on `date`. If it already exists with a different expiry, recreate it.
    try:
        await db.raw_messages.create_index([("date", ASCENDING)], expireAfterSeconds=raw_ttl_seconds, name="raw_ttl")
    except OperationFailure:
        await db.raw_messages.drop_index("raw_ttl")
        await db.raw_messages.create_index([("date", ASCENDING)], expireAfterSeconds=raw_ttl_seconds, name="raw_ttl")

    await db.tasks.create_indexes(
        [
            IndexModel([("chatId", ASCENDING)]),
            IndexModel([("dedupHash", ASCENDING)], unique=True),
        ]
    )
    await db.chat_project_map.create_index([("chatId", ASCENDING)], unique=True)
    await db.chat_state.create_index([("chatId", ASCENDING)], unique=True)
    await db.chat_summary.create_index([("chatId", ASCENDING)], unique=True)
    await db.bot_state.create_index([("key", ASCENDING)], unique=True)
    # Permanent embedding archive for retrieval (NO TTL — survives raw expiry).
    await db.message_vectors.create_index(
        [("chatId", ASCENDING), ("messageId", ASCENDING)], unique=True
    )
    # Task-embedding cache for semantic near-duplicate dedup (also permanent).
    await db.task_vectors.create_index(
        [("scope", ASCENDING), ("key", ASCENDING)], unique=True
    )

    # ── Audit / restore plane (Phase 0) ──────────────────────────────────
    # `audit_log`: append-only mutation trail. TTL on `ts` (default 90d),
    # recreated-on-change exactly like `raw_ttl`. Supporting indexes give the
    # per-object history, "what got deleted last week", and batch-rollback
    # (trace_id) query surfaces from the design.
    try:
        await db.audit_log.create_index(
            [("ts", ASCENDING)], expireAfterSeconds=audit_ttl_seconds, name="audit_ttl"
        )
    except OperationFailure:
        await db.audit_log.drop_index("audit_ttl")
        await db.audit_log.create_index(
            [("ts", ASCENDING)], expireAfterSeconds=audit_ttl_seconds, name="audit_ttl"
        )
    await db.audit_log.create_indexes(
        [
            IndexModel([("target.id", ASCENDING), ("ts", DESCENDING)]),  # per-object history
            IndexModel([("server", ASCENDING), ("op", ASCENDING), ("ts", DESCENDING)]),
            IndexModel([("actor.trace_id", ASCENDING)]),  # roll back a whole bad batch
            IndexModel([("actor.chat_id", ASCENDING), ("ts", DESCENDING)]),
        ]
    )
    # `state_snapshots`: last-known state per object for out-of-band diffing.
    # Keyed (server, targetId); no TTL — overwritten in place, pruned by inactivity.
    await db.state_snapshots.create_index(
        [("server", ASCENDING), ("targetId", ASCENDING)], unique=True
    )
    # `sync_cursors`: one tiny row per provider holding its delta cursor.
    await db.sync_cursors.create_index([("provider", ASCENDING)], unique=True)
