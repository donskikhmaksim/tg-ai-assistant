"""Push locally stored tasks (ticktickTaskId=null, status=open) to TickTick via MCP.

Usage:
    railway run .venv/bin/python scripts/push_local_tasks.py

Idempotent: tasks that already have a ticktickTaskId are skipped.
The script creates each task via MCP and then searches by title to get its ID.
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path
from urllib.parse import quote

# Make sure the project root is on sys.path so `app.*` imports work.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from motor.motor_asyncio import AsyncIOMotorClient

from app.config import get_settings
from app.ticktick.mcp_client import TickTickMCP
from app.web.auth import chat_link_token

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# Everything comes from env / app settings — NEVER hardcode credentials here.
# MONGO_URL / TICKTICK_MCP_URL are read from the environment (or .env); the
# fallback project id uses the configured DEFAULT_PROJECT_ID.
DEFAULT_PROJECT_ID = os.environ.get("DEFAULT_PROJECT_ID") or get_settings().default_project_id

# NOTE: id lookup by title used to live here as a local `search_task_id()` that
# matched by SUBSTRING (`title.lower() in line.lower()`) — a shorter title
# would silently link to a longer near-duplicate's TickTick id. Reconciliation
# now goes through TickTickMCP.find_task_id_for_chat(), which matches the
# EXACT title (see app/ticktick/mcp_client.py) and accepts an `exclude` set so
# duplicate-titled docs in the same run don't collapse onto the same id.
#
# CHAT-OF-ORIGIN DISAMBIGUATION (added 2026-08-04) — and its LIMIT: when two
# different local docs share the exact same title (different chats, or the
# same chat twice), find_task_id_for_chat() tries to break the tie using the
# doc's `chatId` against the chat-of-origin marker the batch pipeline embeds
# in a task's content (the "[💬 Прочитать переписку](...&c=<chat_id>&...)"
# deep link — see app/pipeline/batch.py `_chat_link` / `_task_note`). That
# marker is ONLY present when WEBAPP_URL was configured at the time the task
# was created; a task created before that (or on a deploy that never set
# WEBAPP_URL) carries no chat-of-origin data at all. For those, THIS SCRIPT
# CANNOT reliably tell which doc a given TickTick id actually belongs to — a
# same-titled collision falls back to a best-effort positional pick (still
# guarantees every doc gets SOME id via `exclude`, just not necessarily the
# provably correct one) and a warning is logged so a human can verify it
# instead of trusting it silently.
#
# To shrink that gap for FUTURE runs of this very script: a task this script
# creates is stamped with the same "[💬 Прочитать переписку](...&c=<chat_id>&...)"
# marker the batch pipeline uses (see `_origin_marker` below), so a later
# invocation of this script CAN chat-disambiguate a title collision between
# two tasks it created itself. This does nothing for tasks that already exist
# without a marker (created before WEBAPP_URL was set, or by another path) —
# those remain genuinely undisambiguable from data alone.


def _origin_marker(chat_id: str | None, settings) -> str | None:
    """Same deep-link marker app/pipeline/batch.py `_chat_link` embeds in a
    task's content, so find_task_id_for_chat() can recover `chat_id` from it
    on a later run. None if WEBAPP_URL isn't configured (matches `_chat_link`)
    or chat_id is missing — the task is then created with no origin marker at
    all, same as before this change."""
    base = (settings.webapp_url or "").rstrip("/")
    if not base or not chat_id:
        return None
    token = chat_link_token(chat_id, settings.bot_token)
    return f"[💬 Прочитать переписку]({base}/chat?c={quote(chat_id)}&t={token})"


async def main() -> None:
    settings = get_settings()
    # Read from env first (e.g. the public proxy URL when running outside Railway),
    # then fall back to the app settings. Never hardcode credentials.
    mongo_url = os.environ.get("MONGO_URL") or settings.mongo_url
    db_name = os.environ.get("MONGO_DB") or settings.mongo_db
    mcp_url = os.environ.get("TICKTICK_MCP_URL") or settings.ticktick_mcp_url
    if not mcp_url:
        logger.error("TICKTICK_MCP_URL is not set — nothing to push to. Aborting.")
        sys.exit(1)

    logger.info("Connecting to MongoDB / %s", db_name)
    client = AsyncIOMotorClient(mongo_url)
    db = client[db_name]
    tasks_col = db["tasks"]

    query = {"ticktickTaskId": None, "status": "open"}
    docs = [d async for d in tasks_col.find(query)]
    logger.info("Found %d open tasks to push", len(docs))

    tt = TickTickMCP(url=mcp_url)

    # Build the batch: one task dict per doc, remembering which doc it came from.
    # IDEMPOTENCY: many "local" tasks are ALREADY in TickTick — the live batch
    # created them but couldn't write the id back (see create_task). So search by
    # title FIRST; if it already exists, just link the id and skip creating it,
    # otherwise we'd produce duplicates.
    #
    # `pending` is a LIST of (title, doc), not a dict keyed by title: two
    # different docs (different chats, or even the same chat) can have the
    # exact same title text. A dict keyed by title would silently drop all but
    # the last such doc, leaving it with ticktickTaskId=None forever — and the
    # script would recreate a duplicate for it on every future run.
    batch: list[dict] = []
    pending: list[tuple[str, dict]] = []
    linked_existing = 0
    # ids already claimed by another doc THIS run — passed to find_task_id so
    # duplicate-titled docs each get bound to a DIFFERENT TickTick task instead
    # of all collapsing onto the same one.
    used_ids: set[str] = set()
    for doc in docs:
        title = (doc.get("task") or "").strip()
        if not title:
            logger.warning("Skipping %s — empty title", doc["_id"])
            continue
        chat_id = doc.get("chatId")
        project_id = doc.get("projectId") or DEFAULT_PROJECT_ID
        existing_id, ambiguous = await tt.find_task_id_for_chat(
            title, chat_id, project_id, exclude=used_ids
        )
        if ambiguous:
            logger.warning(
                "Ambiguous title match for '%s' (doc %s, chat %s): multiple TickTick "
                "tasks share this exact title and no chat-of-origin marker cleanly "
                "singled one out — bound to a best-effort guess, verify manually.",
                title[:60], doc["_id"], chat_id,
            )
        if existing_id:
            await tasks_col.update_one({"_id": doc["_id"]}, {"$set": {"ticktickTaskId": existing_id}})
            used_ids.add(existing_id)
            linked_existing += 1
            logger.info("Already in TickTick, linked (no create): '%s'", title[:60])
            continue
        obj = {"title": title, "project_id": project_id}
        content_parts = []
        details = (doc.get("details") or "").strip()
        if details:
            content_parts.append(details)
        marker = _origin_marker(chat_id, settings)
        if marker:
            content_parts.append(marker)
        if content_parts:
            obj["content"] = "\n\n".join(content_parts)
        batch.append(obj)
        pending.append((title, doc))
    logger.info("Linked %d already-existing; %d to create", linked_existing, len(batch))

    if not batch:
        logger.info("Nothing to push.")
        client.close()
        return

    # Create everything in ONE call, chunked so a single request isn't huge.
    CHUNK = 40
    created = 0
    for i in range(0, len(batch), CHUNK):
        part = batch[i:i + CHUNK]
        res = await tt.create_tasks(part, summary=f"Досылаю {len(part)} задач из бэклога")
        logger.info("create_tasks chunk %d..%d -> %s", i, i + len(part), res.splitlines()[0] if res else "")
        created += len(part)

    # Reconcile: create_tasks doesn't return ids, so search each title (the v2
    # cache has settled by now) and write the id back so re-runs don't duplicate.
    # `pending` (not doc_by_title) so every doc is reconciled individually, even
    # when two docs share the exact same title — `exclude=used_ids` then makes
    # sure they bind to two DIFFERENT freshly-created TickTick tasks instead of
    # both grabbing the first search hit.
    ok = no_id = 0
    for title, doc in pending:
        chat_id = doc.get("chatId")
        project_id = doc.get("projectId") or DEFAULT_PROJECT_ID
        tt_id, ambiguous = await tt.find_task_id_for_chat(
            title, chat_id, project_id, exclude=used_ids
        )
        if ambiguous:
            logger.warning(
                "Ambiguous title match for '%s' (doc %s, chat %s): multiple TickTick "
                "tasks share this exact title and no chat-of-origin marker cleanly "
                "singled one out — bound to a best-effort guess, verify manually.",
                title[:60], doc["_id"], chat_id,
            )
        if tt_id:
            await tasks_col.update_one({"_id": doc["_id"]}, {"$set": {"ticktickTaskId": tt_id}})
            used_ids.add(tt_id)
            ok += 1
        else:
            no_id += 1
            logger.warning("Created but not found on reconcile: '%s' (doc %s)", title[:60], doc["_id"])

    client.close()
    logger.info("Done. Sent: %d | linked: %d | not-found-on-reconcile: %d", created, ok, no_id)


if __name__ == "__main__":
    asyncio.run(main())
