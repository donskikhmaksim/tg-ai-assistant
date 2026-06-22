"""Слой доступа к данным поверх коллекций Mongo (§5 ТЗ)."""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from pymongo import ASCENDING, ReturnDocument
from pymongo.errors import DuplicateKeyError

from app.db.mongo import get_db
from app.models import OpenTask

log = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ── raw_messages ──────────────────────────────────────────────────────────────
async def save_raw_message(doc: dict) -> bool:
    """Атомарно сохранить апдулайт до любой обработки (§6).

    Возвращает True, если сообщение новое; False — если дубль (chatId+messageId).
    В обоих случаях обновляет chat_state.lastMessageAt.
    """
    db = get_db()
    is_new = True
    try:
        await db.raw_messages.insert_one(doc)
    except DuplicateKeyError:
        is_new = False
    # дубли тоже двигают lastMessageAt безопасно (max)
    await touch_last_message(doc["chatId"], doc["date"])
    return is_new


async def fetch_recent_messages(chat_id: str, since: datetime) -> list[dict]:
    """Сырьё чата начиная с `since`, отсортированное по дате (для сборки окна)."""
    db = get_db()
    cursor = (
        db.raw_messages.find({"chatId": chat_id, "date": {"$gte": since}})
        .sort("date", ASCENDING)
    )
    return [doc async for doc in cursor]


# ── chat_state ────────────────────────────────────────────────────────────────
async def touch_last_message(chat_id: str, when: datetime) -> None:
    db = get_db()
    await db.chat_state.update_one(
        {"chatId": chat_id},
        {"$max": {"lastMessageAt": when}, "$setOnInsert": {"chatId": chat_id}},
        upsert=True,
    )


async def get_dirty_chats() -> list[str]:
    """Чаты с новыми сообщениями: lastMessageAt > lastProcessedAt (§7.1)."""
    db = get_db()
    query = {
        "$or": [
            {"lastProcessedAt": {"$exists": False}},
            {"$expr": {"$gt": ["$lastMessageAt", "$lastProcessedAt"]}},
        ]
    }
    cursor = db.chat_state.find(query, {"chatId": 1})
    return [doc["chatId"] async for doc in cursor]


async def set_last_processed(chat_id: str, when: datetime | None = None) -> None:
    db = get_db()
    await db.chat_state.update_one(
        {"chatId": chat_id},
        {"$set": {"lastProcessedAt": when or _now()}},
        upsert=True,
    )


# ── tasks ─────────────────────────────────────────────────────────────────────
async def get_open_tasks(chat_id: str) -> list[OpenTask]:
    db = get_db()
    cursor = db.tasks.find({"chatId": chat_id, "status": "open"})
    out: list[OpenTask] = []
    async for d in cursor:
        out.append(
            OpenTask(
                task=d["task"],
                who=d.get("who", "me"),
                deadline=d.get("deadline"),
                dedup_hash=d["dedupHash"],
                ticktick_task_id=d.get("ticktickTaskId"),
                project_id=d.get("projectId"),
            )
        )
    return out


async def task_exists(dedup_hash: str) -> bool:
    db = get_db()
    return await db.tasks.find_one({"dedupHash": dedup_hash}, {"_id": 1}) is not None


async def insert_task(doc: dict) -> bool:
    """Вставить задачу. False — если такой dedupHash уже есть (дубль)."""
    db = get_db()
    doc.setdefault("createdAt", _now())
    doc.setdefault("updatedAt", _now())
    try:
        await db.tasks.insert_one(doc)
        return True
    except DuplicateKeyError:
        return False


async def set_task_ticktick_id(dedup_hash: str, ticktick_task_id: str) -> None:
    db = get_db()
    await db.tasks.update_one(
        {"dedupHash": dedup_hash},
        {"$set": {"ticktickTaskId": ticktick_task_id, "updatedAt": _now()}},
    )


async def update_task_status(dedup_hash: str, new_status: str) -> dict | None:
    db = get_db()
    return await db.tasks.find_one_and_update(
        {"dedupHash": dedup_hash},
        {"$set": {"status": new_status, "updatedAt": _now()}},
        return_document=ReturnDocument.AFTER,
    )


# ── chat_summary (долговременная память, §5/§7) ───────────────────────────────
async def get_summary(chat_id: str) -> str:
    db = get_db()
    doc = await db.chat_summary.find_one({"chatId": chat_id})
    return doc["summary"] if doc else ""


async def upsert_summary(chat_id: str, summary: str) -> None:
    db = get_db()
    await db.chat_summary.update_one(
        {"chatId": chat_id},
        {"$set": {"summary": summary, "updatedAt": _now()}},
        upsert=True,
    )


# ── chat_project_map (§9) ─────────────────────────────────────────────────────
async def get_project_mapping(chat_id: str) -> dict | None:
    db = get_db()
    return await db.chat_project_map.find_one({"chatId": chat_id})


async def set_project_mapping(
    chat_id: str, project_id: str, project_name: str
) -> None:
    db = get_db()
    await db.chat_project_map.update_one(
        {"chatId": chat_id},
        {
            "$set": {
                "ticktickProjectId": project_id,
                "projectName": project_name,
            }
        },
        upsert=True,
    )


async def unset_project_mapping(chat_id: str) -> bool:
    db = get_db()
    res = await db.chat_project_map.delete_one({"chatId": chat_id})
    return res.deleted_count > 0


async def list_project_mappings() -> list[dict]:
    db = get_db()
    return [d async for d in db.chat_project_map.find({})]


# ── bot_state / settings (business_connection и т.п., §5/§6) ──────────────────
async def set_setting(key: str, value) -> None:
    db = get_db()
    await db.settings.update_one(
        {"_id": key}, {"$set": {"value": value}}, upsert=True
    )


async def get_setting(key: str, default=None):
    db = get_db()
    doc = await db.settings.find_one({"_id": key})
    return doc["value"] if doc else default
