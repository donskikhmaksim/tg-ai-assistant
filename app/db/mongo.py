"""Подключение к MongoDB (Motor) и инициализация индексов (§5 ТЗ)."""
from __future__ import annotations

import logging

from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase
from pymongo import ASCENDING

log = logging.getLogger(__name__)

_client: AsyncIOMotorClient | None = None
_db: AsyncIOMotorDatabase | None = None


async def connect(mongo_url: str, db_name: str, raw_ttl_days: int) -> AsyncIOMotorDatabase:
    """Создать клиент и обеспечить нужные индексы. Идемпотентно."""
    global _client, _db
    _client = AsyncIOMotorClient(mongo_url, tz_aware=True)
    _db = _client[db_name]
    await _ensure_indexes(_db, raw_ttl_days)
    log.info("MongoDB подключена: db=%s", db_name)
    return _db


def get_db() -> AsyncIOMotorDatabase:
    if _db is None:
        raise RuntimeError("MongoDB не инициализирована — вызовите connect() сначала")
    return _db


async def close() -> None:
    if _client is not None:
        _client.close()


async def _ensure_indexes(db: AsyncIOMotorDatabase, raw_ttl_days: int) -> None:
    # raw_messages: ключ chatId+date + TTL на date
    await db.raw_messages.create_index([("chatId", ASCENDING), ("date", ASCENDING)])
    await db.raw_messages.create_index(
        [("date", ASCENDING)], expireAfterSeconds=raw_ttl_days * 24 * 3600
    )
    # защита от повторной вставки одного и того же апдейта
    await db.raw_messages.create_index(
        [("chatId", ASCENDING), ("messageId", ASCENDING)], unique=True
    )
    # tasks: {chatId} + уникальный dedupHash (страховка от дублей, §7.5)
    await db.tasks.create_index([("chatId", ASCENDING)])
    await db.tasks.create_index([("dedupHash", ASCENDING)], unique=True)
    # chat_project_map / chat_state / chat_summary: ключ по chatId
    await db.chat_project_map.create_index([("chatId", ASCENDING)], unique=True)
    await db.chat_state.create_index([("chatId", ASCENDING)], unique=True)
    await db.chat_summary.create_index([("chatId", ASCENDING)], unique=True)
