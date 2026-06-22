"""Data-access helpers over the Mongo collections.

Kept deliberately thin: each function maps to one storage concern so the
pipeline and handlers read like the spec.
"""
from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from typing import Any

from pymongo import ReturnDocument

from .db import get_db


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# raw_messages + chat_state
# ---------------------------------------------------------------------------

async def save_raw_message(doc: dict[str, Any], title: str | None = None) -> None:
    """Persist a single update atomically, then bump the chat cursor.

    Loss is irreversible (Telegram never resends), so this runs before any
    processing. We de-dupe on (chatId, messageId) so re-delivered updates and
    manual reruns don't double-insert. `title` is the human-readable chat name
    (group title or DM counterparty), stored on chat_state for the Mini App.
    """
    db = get_db()
    await db.raw_messages.update_one(
        {"chatId": doc["chatId"], "messageId": doc["messageId"]},
        {"$set": doc},
        upsert=True,
    )
    await touch_chat_state(doc["chatId"], doc["date"], title)


async def touch_chat_state(chat_id: str, when: datetime, title: str | None = None) -> None:
    db = get_db()
    update: dict[str, Any] = {"$max": {"lastMessageAt": when}, "$setOnInsert": {"chatId": chat_id}}
    if title:
        update["$set"] = {"title": title}
    await db.chat_state.update_one({"chatId": chat_id}, update, upsert=True)


async def list_known_chats() -> list[dict[str, Any]]:
    """Every chat the bot has seen, newest activity first — for the Mini App."""
    db = get_db()
    cursor = db.chat_state.find({}, {"chatId": 1, "title": 1, "lastMessageAt": 1}).sort(
        "lastMessageAt", -1
    )
    return [d async for d in cursor]


async def get_chat_title(chat_id: str) -> str:
    """Human-readable chat name (group title / DM counterparty), else the id."""
    db = get_db()
    doc = await db.chat_state.find_one({"chatId": chat_id}, {"title": 1})
    return (doc or {}).get("title") or chat_id


async def get_dirty_chats() -> list[str]:
    """Chats with new messages since the last processed run.

    A chat is dirty when lastMessageAt > lastProcessedAt (or was never
    processed). Done in Python via $expr so the comparison is field-to-field.
    """
    db = get_db()
    cursor = db.chat_state.find(
        {
            "$or": [
                {"lastProcessedAt": {"$exists": False}},
                {"$expr": {"$gt": ["$lastMessageAt", "$lastProcessedAt"]}},
            ]
        },
        {"chatId": 1},
    )
    return [d["chatId"] async for d in cursor]


async def mark_processed(chat_id: str) -> None:
    db = get_db()
    await db.chat_state.update_one({"chatId": chat_id}, {"$set": {"lastProcessedAt": utcnow()}})


async def get_chat_messages(chat_id: str) -> list[dict[str, Any]]:
    """All retained raw messages for a chat, oldest first."""
    db = get_db()
    cursor = db.raw_messages.find({"chatId": chat_id}).sort("date", 1)
    return [d async for d in cursor]


# ---------------------------------------------------------------------------
# tasks
# ---------------------------------------------------------------------------

_WS = re.compile(r"\s+")


def normalize_task(text: str) -> str:
    return _WS.sub(" ", text.strip().lower())


def dedup_hash(chat_id: str, task: str) -> str:
    return hashlib.sha1(f"{chat_id}{normalize_task(task)}".encode()).hexdigest()


async def get_open_tasks(chat_id: str) -> list[dict[str, Any]]:
    db = get_db()
    cursor = db.tasks.find({"chatId": chat_id, "status": "open"}).sort("createdAt", 1)
    return [d async for d in cursor]


async def insert_task_if_new(task: dict[str, Any]) -> bool:
    """Insert respecting the unique dedupHash. Returns True if newly inserted."""
    db = get_db()
    res = await db.tasks.update_one(
        {"dedupHash": task["dedupHash"]},
        {"$setOnInsert": task},
        upsert=True,
    )
    return res.upserted_id is not None


async def set_task_ticktick_id(dedup: str, ticktick_task_id: str) -> None:
    db = get_db()
    await db.tasks.update_one(
        {"dedupHash": dedup},
        {"$set": {"ticktickTaskId": ticktick_task_id, "updatedAt": utcnow()}},
    )


async def update_task_status(chat_id: str, dedup: str, new_status: str) -> dict[str, Any] | None:
    db = get_db()
    return await db.tasks.find_one_and_update(
        {"chatId": chat_id, "dedupHash": dedup},
        {"$set": {"status": new_status, "updatedAt": utcnow()}},
        return_document=ReturnDocument.AFTER,
    )


# ---------------------------------------------------------------------------
# chat_summary (long-term memory)
# ---------------------------------------------------------------------------

async def get_summary(chat_id: str) -> str:
    db = get_db()
    doc = await db.chat_summary.find_one({"chatId": chat_id})
    return doc["summary"] if doc else ""


async def set_summary(chat_id: str, summary: str) -> None:
    db = get_db()
    await db.chat_summary.update_one(
        {"chatId": chat_id},
        {"$set": {"summary": summary, "updatedAt": utcnow()}, "$setOnInsert": {"chatId": chat_id}},
        upsert=True,
    )


# ---------------------------------------------------------------------------
# chat_project_map
# ---------------------------------------------------------------------------

async def get_project_binding(chat_id: str) -> dict[str, Any] | None:
    db = get_db()
    return await db.chat_project_map.find_one({"chatId": chat_id})


async def set_project_binding(chat_id: str, project_id: str, project_name: str) -> None:
    db = get_db()
    await db.chat_project_map.update_one(
        {"chatId": chat_id},
        {"$set": {"ticktickProjectId": project_id, "projectName": project_name}, "$setOnInsert": {"chatId": chat_id}},
        upsert=True,
    )


async def delete_project_binding(chat_id: str) -> bool:
    db = get_db()
    res = await db.chat_project_map.delete_one({"chatId": chat_id})
    return res.deleted_count > 0


async def list_project_bindings() -> list[dict[str, Any]]:
    db = get_db()
    return [d async for d in get_db().chat_project_map.find({})]


# ---------------------------------------------------------------------------
# bot_state (business connection / owner)
# ---------------------------------------------------------------------------

async def set_bot_state(key: str, value: Any) -> None:
    db = get_db()
    await db.bot_state.update_one({"key": key}, {"$set": {"value": value}}, upsert=True)


async def get_bot_state(key: str) -> Any:
    db = get_db()
    doc = await db.bot_state.find_one({"key": key})
    return doc["value"] if doc else None
