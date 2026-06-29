"""Data-access helpers over the Mongo collections.

Kept deliberately thin: each function maps to one storage concern so the
pipeline and handlers read like the spec.
"""
from __future__ import annotations

import hashlib
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from pymongo import ReturnDocument, UpdateOne

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
    update: dict[str, Any] = {
        "$max": {"lastMessageAt": when},
        "$setOnInsert": {"chatId": chat_id, "firstSeenAt": when},
    }
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


async def count_messages_per_chat() -> dict[str, int]:
    """Return {chatId: message_count} for all chats, using raw_messages aggregation."""
    db = get_db()
    pipeline = [{"$group": {"_id": "$chatId", "count": {"$sum": 1}}}]
    result = {}
    async for doc in db.raw_messages.aggregate(pipeline):
        if doc["_id"]:
            result[doc["_id"]] = doc["count"]
    return result


async def get_chat_title(chat_id: str) -> str:
    """Human-readable chat name (group title / DM counterparty), else the id."""
    db = get_db()
    doc = await db.chat_state.find_one({"chatId": chat_id}, {"title": 1})
    return (doc or {}).get("title") or chat_id


async def get_dirty_chats(quiet_minutes: int = 0, max_dirty_minutes: int = 0) -> list[str]:
    """Chats ready for processing under a debounce policy.

    A chat is *dirty* when lastMessageAt > lastProcessedAt (or it was never
    processed). A dirty chat is *ready* when either:
      - it has been quiet for `quiet_minutes` (settled thought, not mid-chat), or
      - it has been dirty for `max_dirty_minutes` (max-wait safety so a chat
        that never goes quiet is still processed).
    With both thresholds at 0 this reduces to "every dirty chat" (legacy).
    """
    db = get_db()
    cursor = db.chat_state.find(
        {
            "$or": [
                {"lastProcessedAt": {"$exists": False}},
                {"$expr": {"$gt": ["$lastMessageAt", "$lastProcessedAt"]}},
            ]
        },
        {"chatId": 1, "lastMessageAt": 1, "lastProcessedAt": 1, "firstSeenAt": 1},
    )
    now = utcnow()
    quiet = timedelta(minutes=quiet_minutes)
    maxwait = timedelta(minutes=max_dirty_minutes)
    ready: list[str] = []
    async for d in cursor:
        last_msg = d.get("lastMessageAt")
        if last_msg is None:
            continue
        if quiet_minutes <= 0 and max_dirty_minutes <= 0:
            ready.append(d["chatId"])
            continue
        quiet_ok = (now - last_msg) >= quiet
        anchor = d.get("lastProcessedAt") or d.get("firstSeenAt") or last_msg
        maxwait_ok = (now - anchor) >= maxwait
        if quiet_ok or maxwait_ok:
            ready.append(d["chatId"])
    return ready


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


async def set_project_binding(
    chat_id: str,
    project_id: str,
    project_name: str,
    section_id: str | None = None,
    section_name: str | None = None,
) -> None:
    db = get_db()
    await db.chat_project_map.update_one(
        {"chatId": chat_id},
        {
            "$set": {
                "ticktickProjectId": project_id,
                "projectName": project_name,
                # Stored even when None so re-binding without a section clears it.
                "ticktickSectionId": section_id,
                "sectionName": section_name,
            },
            "$setOnInsert": {"chatId": chat_id},
        },
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
# message_vectors (permanent embedding archive for retrieval)
# ---------------------------------------------------------------------------

async def existing_vector_ids(chat_id: str, message_ids: list[int]) -> set[int]:
    db = get_db()
    cursor = db.message_vectors.find(
        {"chatId": chat_id, "messageId": {"$in": message_ids}}, {"messageId": 1}
    )
    return {d["messageId"] async for d in cursor}


async def store_vectors(chat_id: str, items: list[dict[str, Any]]) -> None:
    """Upsert embedding rows. Each item: messageId, text, date, embedding."""
    if not items:
        return
    db = get_db()
    ops = [
        UpdateOne(
            {"chatId": chat_id, "messageId": it["messageId"]},
            {"$setOnInsert": {**it, "chatId": chat_id}},
            upsert=True,
        )
        for it in items
    ]
    await db.message_vectors.bulk_write(ops, ordered=False)


async def get_chat_vectors(
    chat_id: str, exclude_ids: set[int], limit: int = 5000
) -> list[dict[str, Any]]:
    """Most recent stored vectors for a chat, excluding given message ids."""
    db = get_db()
    cursor = (
        db.message_vectors.find(
            {"chatId": chat_id, "messageId": {"$nin": list(exclude_ids)}},
            {"messageId": 1, "text": 1, "embedding": 1},
        )
        .sort("date", -1)
        .limit(limit)
    )
    return [d async for d in cursor]


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


# ---------------------------------------------------------------------------
# chat_settings (per-chat context + LLM rule overrides)
#
# Schema:
#   chatId        str
#   alias         str | None  — human-friendly short name for the chat (Mini App only)
#   who           str | None  — who this person is / what the group is about
#   topics        str | None  — what is usually discussed
#   task_side     str | None  — who tasks are assigned to
#   filter_rules  str | None  — Qwen+Claude: additional rules for detecting tasks
#   extract_rules str | None  — Claude: additional rules for extracting tasks
#   importance    str | None  — Qwen+Claude: what makes a task important vs not
#   people        str | None  — Claude only: names/roles reference for this chat
#
# Special chatId "__global__" holds defaults applied to all chats (per-chat overrides).
# ---------------------------------------------------------------------------

async def get_chat_settings(chat_id: str) -> dict[str, Any]:
    """Returns the full settings document for a chat, or {} if none."""
    db = get_db()
    doc = await db.chat_settings.find_one({"chatId": chat_id})
    return doc or {}


async def get_global_settings() -> dict[str, Any]:
    """Returns the global (default) settings document, or {} if none."""
    return await get_chat_settings("__global__")


async def update_global_settings(fields: dict[str, Any]) -> None:
    """Upsert the given fields into the global settings document."""
    await update_chat_settings("__global__", fields)


async def update_chat_settings(chat_id: str, fields: dict[str, Any]) -> None:
    """Upsert the given fields into the chat settings document."""
    db = get_db()
    await db.chat_settings.update_one(
        {"chatId": chat_id},
        {"$set": {"chatId": chat_id, **fields}},
        upsert=True,
    )


_DEFAULT_GLOBAL_FILTER_RULES = (
    "Оцени не только есть ли задача, но и стоит ли она внимания.\n"
    "Отклоняй (has_task: false) если задача явно незначительная в данном контексте: "
    "бытовая мелочь без дедлайна, абстрактные «когда-нибудь», риторические обещания.\n"
    "Принимай (has_task: true) если: есть конкретное действие + дедлайн/деньги/имя ответственного."
)


async def init_global_defaults(db: Any | None = None) -> None:
    """Pre-fill global settings with smart defaults if not already set.

    Called once at startup. The user can override any field via the UI.
    """
    _db = db or get_db()
    doc = await _db.chat_settings.find_one({"chatId": "__global__"})
    if doc and doc.get("filter_rules"):
        return  # Already configured — don't overwrite user's settings.
    await _db.chat_settings.update_one(
        {"chatId": "__global__"},
        {"$setOnInsert": {
            "chatId": "__global__",
            "filter_rules": _DEFAULT_GLOBAL_FILTER_RULES,
        }},
        upsert=True,
    )


async def clear_chat_settings_field(chat_id: str, field: str) -> None:
    """Unset a single field from the chat settings document."""
    db = get_db()
    await db.chat_settings.update_one(
        {"chatId": chat_id},
        {"$unset": {field: ""}, "$setOnInsert": {"chatId": chat_id}},
        upsert=True,
    )
