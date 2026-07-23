"""Data-access helpers over the Mongo collections.

Kept deliberately thin: each function maps to one storage concern so the
pipeline and handlers read like the spec.
"""
from __future__ import annotations

import hashlib
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from bson import ObjectId
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


async def touch_chat_state(
    chat_id: str, when: datetime, title: str | None = None
) -> None:
    db = get_db()
    update: dict[str, Any] = {
        "$max": {"lastMessageAt": when},
        "$setOnInsert": {"chatId": chat_id, "firstSeenAt": when},
    }
    if title:
        update["$set"] = {"title": title}
    await db.chat_state.update_one({"chatId": chat_id}, update, upsert=True)


async def list_known_chats() -> list[dict[str, Any]]:
    """Every chat the bot has seen, newest activity first — for the Mini App.

    Single-tenant: all chats belong to the one owner, so there is no per-owner
    filtering.
    """
    db = get_db()
    cursor = db.chat_state.find(
        {}, {"chatId": 1, "title": 1, "lastMessageAt": 1}
    ).sort("lastMessageAt", -1)
    return [d async for d in cursor]


async def chat_activity_scores() -> dict[str, float]:
    """Return {chatId: score} where score = msgs_7d*3 + msgs_30d*1."""
    from datetime import datetime, timedelta, timezone
    db = get_db()
    now = datetime.now(timezone.utc)
    cut7  = now - timedelta(days=7)
    cut30 = now - timedelta(days=30)
    pipeline = [
        {"$match": {"date": {"$gte": cut30}}},
        {"$group": {
            "_id": "$chatId",
            "msgs30": {"$sum": 1},
            "msgs7":  {"$sum": {"$cond": [{"$gte": ["$date", cut7]}, 1, 0]}},
        }},
    ]
    result = {}
    async for doc in db.raw_messages.aggregate(pipeline):
        if doc["_id"]:
            result[doc["_id"]] = doc["msgs7"] * 3 + doc["msgs30"]
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


async def get_tasks_created_between(
    chat_id: str, start: datetime, end: datetime
) -> list[dict[str, Any]]:
    """Tasks first created for a chat in [start, end) (UTC), oldest first.

    Used by the end-of-day group summary. Bounds are UTC datetimes; the caller
    converts the local day to UTC before calling.
    """
    db = get_db()
    cursor = db.tasks.find(
        {"chatId": chat_id, "createdAt": {"$gte": start, "$lt": end}}
    ).sort("createdAt", 1)
    return [d async for d in cursor]


async def get_tasks_closed_between(
    chat_id: str, start: datetime, end: datetime
) -> list[dict[str, Any]]:
    """Tasks for a chat moved to done/cancelled in [start, end) (UTC).

    "Closed" = status in (done, cancelled) with updatedAt in the window — i.e.
    the bot completed/updated them that day. Used by the group summary.
    """
    db = get_db()
    cursor = db.tasks.find(
        {
            "chatId": chat_id,
            "status": {"$in": ["done", "cancelled"]},
            "updatedAt": {"$gte": start, "$lt": end},
        }
    ).sort("updatedAt", 1)
    return [d async for d in cursor]


async def list_group_chat_ids() -> list[str]:
    """chatIds of every group the bot has seen (chatId starts with "group_")."""
    db = get_db()
    cursor = db.chat_state.find(
        {"chatId": {"$regex": "^group_"}}, {"chatId": 1}
    )
    return [d["chatId"] async for d in cursor]


async def update_task_status(chat_id: str, dedup: str, new_status: str) -> dict[str, Any] | None:
    db = get_db()
    return await db.tasks.find_one_and_update(
        {"chatId": chat_id, "dedupHash": dedup},
        {"$set": {"status": new_status, "updatedAt": utcnow()}},
        return_document=ReturnDocument.AFTER,
    )


# ---------------------------------------------------------------------------
# task_vectors (embedding cache for semantic near-duplicate dedup)
#
# Keyed by (scope, key) so both candidate sources share one cache:
#   local open tasks  → scope = chatId,            key = task dedupHash
#   TickTick project  → scope = "proj:<projectId>", key = "tt:<ticktickTaskId>"
# `title` is the embedded text; a title change (re-word) invalidates the cache
# (store_task_vectors overwrites), so future comparisons see the new phrasing.
# Permanent (no TTL) — reuse keeps the per-run embedding cost tiny.
# ---------------------------------------------------------------------------

async def get_task_vectors(scope: str) -> dict[str, dict[str, Any]]:
    """Cached embeddings for a scope, as {key: {title, embedding}}."""
    db = get_db()
    cursor = db.task_vectors.find(
        {"scope": scope}, {"key": 1, "title": 1, "embedding": 1}
    )
    return {d["key"]: d async for d in cursor}


async def store_task_vectors(scope: str, items: list[dict[str, Any]]) -> None:
    """Upsert embedding rows. Each item: key, title, embedding."""
    if not items:
        return
    db = get_db()
    ops = [
        UpdateOne(
            {"scope": scope, "key": it["key"]},
            {"$set": {
                "scope": scope, "key": it["key"],
                "title": it["title"], "embedding": it["embedding"],
                "updatedAt": utcnow(),
            }},
            upsert=True,
        )
        for it in items
    ]
    await db.task_vectors.bulk_write(ops, ordered=False)


async def append_task_details(chat_id: str, dedup: str, extra: str) -> bool:
    """Append `extra` to a local task's details (enrich, never overwrite).

    Returns True if the task existed and was updated. The new text goes on a
    fresh paragraph after any existing details."""
    db = get_db()
    doc = await db.tasks.find_one(
        {"chatId": chat_id, "dedupHash": dedup}, {"details": 1}
    )
    if doc is None:
        return False
    existing = (doc.get("details") or "").strip()
    merged = f"{existing}\n\n{extra}".strip() if existing else extra
    await db.tasks.update_one(
        {"chatId": chat_id, "dedupHash": dedup},
        {"$set": {"details": merged, "updatedAt": utcnow()}},
    )
    return True


async def set_task_deadline_if_missing(chat_id: str, dedup: str, deadline: str) -> bool:
    """Set a local task's deadline ONLY if it has none recorded (deadline
    transfer on semantic dedup — enrich, never overwrite an existing deadline).

    Returns True if the task existed without a deadline and was updated."""
    db = get_db()
    res = await db.tasks.update_one(
        {
            "chatId": chat_id,
            "dedupHash": dedup,
            "$or": [
                {"deadline": None},
                {"deadline": ""},
                {"deadline": {"$exists": False}},
            ],
        },
        {"$set": {"deadline": deadline, "updatedAt": utcnow()}},
    )
    return res.modified_count > 0


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


# ---------------------------------------------------------------------------
# audit_log / state_snapshots / sync_cursors  (Phase 0 — audit/restore plane)
#
# audit_log       — append-only mutation trail (TTL on `ts`, ~90d). Written pre-
#                   mutation (before + intent) then patched with the result, the
#                   same "persist before processing" discipline as raw_messages.
# state_snapshots — last-known state per object, keyed (server, targetId), used
#                   by the out-of-band poller to diff what changed. No TTL.
# sync_cursors    — per-provider delta cursor (sync token / checkpoint).
#
# These are thin storage helpers; the writer/attribution logic lives in
# app/audit/. Every helper here is called from fail-open call sites — a raise
# never reaches the pipeline.
# ---------------------------------------------------------------------------

async def insert_audit_record(doc: dict[str, Any]) -> ObjectId:
    """Append one audit record, returning its _id (an ObjectId)."""
    db = get_db()
    res = await db.audit_log.insert_one(doc)
    return res.inserted_id


async def finalize_audit_record(record_id: ObjectId, fields: dict[str, Any]) -> None:
    """Patch an existing audit record with `after`/`result`/`diff` etc."""
    db = get_db()
    await db.audit_log.update_one({"_id": record_id}, {"$set": fields})


async def get_audit_record(record_id: ObjectId) -> dict[str, Any] | None:
    """Fetch one audit record by _id (used to recompute a diff on finalize)."""
    db = get_db()
    return await db.audit_log.find_one({"_id": record_id})


async def get_recent_audit_records(
    server: str,
    target_ids: list[str],
    since: datetime,
    capture_plane: str = "in_band",
) -> list[dict[str, Any]]:
    """Recent audit records for a server touching any of `target_ids` at/after
    `since` — used by the out-of-band poller to spot our own edits echoing back
    through the provider's sync feed (so they're not double-logged)."""
    db = get_db()
    query: dict[str, Any] = {"server": server, "ts": {"$gte": since}}
    if capture_plane:
        query["capture_plane"] = capture_plane
    if target_ids:
        query["target.id"] = {"$in": target_ids}
    cursor = db.audit_log.find(query).sort("ts", -1)
    return [d async for d in cursor]


async def get_state_snapshot(server: str, target_id: str) -> dict[str, Any] | None:
    """Last-known snapshot for one object, or None if never seen."""
    db = get_db()
    return await db.state_snapshots.find_one({"server": server, "targetId": target_id})


async def list_state_snapshots(server: str) -> dict[str, dict[str, Any]]:
    """All snapshots for a server as {targetId: snapshot_doc}."""
    db = get_db()
    cursor = db.state_snapshots.find({"server": server})
    return {d["targetId"]: d async for d in cursor}


async def upsert_state_snapshot(
    server: str, target_id: str, state: dict[str, Any]
) -> None:
    """Store/overwrite the last-known state of one object (in place)."""
    db = get_db()
    await db.state_snapshots.update_one(
        {"server": server, "targetId": target_id},
        {
            "$set": {"state": state, "updatedAt": utcnow()},
            "$setOnInsert": {"server": server, "targetId": target_id},
        },
        upsert=True,
    )


async def get_sync_cursor(provider: str) -> dict[str, Any] | None:
    """The stored delta cursor row for a provider, or None."""
    db = get_db()
    return await db.sync_cursors.find_one({"provider": provider})


async def set_sync_cursor(provider: str, cursor: Any) -> None:
    """Persist a provider's delta cursor (sync token / checkpoint / timestamp)."""
    db = get_db()
    await db.sync_cursors.update_one(
        {"provider": provider},
        {
            "$set": {"cursor": cursor, "updatedAt": utcnow()},
            "$setOnInsert": {"provider": provider},
        },
        upsert=True,
    )


# ---------------------------------------------------------------------------
# policy (manifest-policy admin — Phase 1: storage only, no enforcement here)
#
# One document per instance (single-tenant today, per CLAUDE.md):
# `_id: "policy:__global__"`. Holds only the OWNER's overrides — class-wide
# `defaults` and per-tool `tools` overrides — resolved over the static tool
# catalog (app/policy/catalog.py + catalog.json), which supplies each tool's
# `class` and a recommended tier. Nothing in this repo enforces the resolved
# tier yet: see app/policy/__init__.py for the phase split (this is the
# control plane; each MCP server's own pull+enforce is a later, separate
# phase in that server's repo).
# ---------------------------------------------------------------------------

_POLICY_DOC_ID = "policy:__global__"


async def get_policy() -> dict[str, Any]:
    """The stored policy doc, or a sane empty default if never saved.

    Empty defaults (`{}`) mean "no owner overrides yet" — every tool falls
    back through the catalog's own class defaults / recommended tier (see
    app/policy/catalog.py::resolve_tier).
    """
    db = get_db()
    doc = await db.policy.find_one({"_id": _POLICY_DOC_ID})
    if not doc:
        return {"_id": _POLICY_DOC_ID, "version": 0, "defaults": {}, "tools": {}}
    return doc


async def save_policy(
    defaults: dict[str, str], tools: dict[str, str], updated_by: int | None
) -> dict[str, Any]:
    """Overwrite `defaults`/`tools` wholesale, bump `version`, return the saved doc.

    Callers (the Mini App API) are expected to merge their partial edit over
    the CURRENT doc before calling this (read-modify-write), same as
    `update_chat_settings` callers do for chat settings — this function itself
    does a full replace, it doesn't merge.
    """
    db = get_db()
    current = await db.policy.find_one({"_id": _POLICY_DOC_ID}, {"version": 1})
    next_version = (current or {}).get("version", 0) + 1
    doc = {
        "_id": _POLICY_DOC_ID,
        "version": next_version,
        "updated_at": utcnow(),
        "updated_by": updated_by,
        "defaults": defaults,
        "tools": tools,
    }
    await db.policy.replace_one({"_id": _POLICY_DOC_ID}, doc, upsert=True)
    return doc
