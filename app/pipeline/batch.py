"""Batch pipeline (spec §7): runs every BATCH_INTERVAL_MIN.

For each dirty chat:
  1. Build the current conversation window from stored raw messages.
  2. Tier 1 (Qwen): does the window contain a task? If not, mark processed.
  3. Tier 2 (Claude): window + long-term memory -> incremental JSON.
  4. Persist updated summary (memory survives raw TTL).
  5. Dedup + create new tasks in TickTick under the bound project (or Inbox).
  6. Apply status updates (complete/cancel) in TickTick.
  7. Advance the processed cursor.
"""
from __future__ import annotations

import logging
from typing import Any

from .. import repositories as repo
from ..config import get_settings
from ..llm import claude, qwen
from ..ticktick.mcp_client import get_ticktick
from .dedup import to_ticktick_due
from .windows import build_window, render_window

logger = logging.getLogger(__name__)


async def run_batch() -> None:
    chats = await repo.get_dirty_chats()
    if not chats:
        logger.debug("Batch: no dirty chats")
        return
    logger.info("Batch: processing %d dirty chat(s)", len(chats))
    for chat_id in chats:
        try:
            await process_chat(chat_id)
        except Exception:  # noqa: BLE001 — one bad chat shouldn't stall the batch
            logger.exception("Batch: failed to process chat %s", chat_id)


async def process_chat(chat_id: str) -> None:
    s = get_settings()
    messages = await repo.get_chat_messages(chat_id)
    window = build_window(
        messages, gap_hours=s.conv_gap_hours, max_lookback_hours=s.max_lookback_hours
    )
    if not window:
        await repo.mark_processed(chat_id)
        return

    window_text = render_window(window)

    # Tier 1 — cheap local gate.
    if not await qwen.has_task(window_text):
        logger.info("Chat %s: Qwen says no task", chat_id)
        await repo.mark_processed(chat_id)
        return

    # Tier 2 — Claude with long-term memory.
    summary = await repo.get_summary(chat_id)
    open_tasks = await repo.get_open_tasks(chat_id)
    result = await claude.extract(window_text, summary, open_tasks)

    # Memory first: persist the refreshed summary before raw expires.
    new_summary = result.get("updated_summary")
    if new_summary:
        await repo.set_summary(chat_id, new_summary)

    await _create_new_tasks(chat_id, result.get("new_tasks", []))
    await _apply_status_updates(chat_id, open_tasks, result.get("status_updates", []))

    await repo.mark_processed(chat_id)


async def _resolve_project(chat_id: str) -> tuple[str | None, str]:
    """Returns (projectId, projectName). Falls back to Inbox (no explicit id)."""
    binding = await repo.get_project_binding(chat_id)
    if binding:
        return binding["ticktickProjectId"], binding.get("projectName", "")
    return None, get_settings().default_project


async def _create_new_tasks(chat_id: str, new_tasks: list[dict[str, Any]]) -> None:
    if not new_tasks:
        return
    project_id, project_name = await _resolve_project(chat_id)
    tt = get_ticktick()

    for t in new_tasks:
        title = (t.get("task") or "").strip()
        if not title:
            continue
        dedup = repo.dedup_hash(chat_id, title)
        task_doc = {
            "chatId": chat_id,
            "task": title,
            "who": t.get("who", "me"),
            "counterpartyName": t.get("counterpartyName"),
            "deadline": t.get("deadline"),
            "status": "open",
            "sourceMessageIds": t.get("source_message_ids", []),
            "dedupHash": dedup,
            "ticktickTaskId": None,
            "projectId": project_id,
            "createdAt": repo.utcnow(),
            "updatedAt": repo.utcnow(),
        }
        # Dedup: unique index + this guard mean overlapping windows don't duplicate.
        if not await repo.insert_task_if_new(task_doc):
            logger.debug("Chat %s: duplicate task skipped: %s", chat_id, title)
            continue

        # Push to TickTick. project_id=None means Inbox; we need a real id, so
        # only create remotely when a project is bound. Unbound tasks are still
        # recorded locally and can be synced once a project is attached.
        if project_id is None:
            logger.info("Chat %s: task stored locally (no project bound): %s", chat_id, title)
            continue
        try:
            note = _task_note(t)
            tt_id = await tt.create_task(
                title=title,
                project_id=project_id,
                content=note,
                due_date=to_ticktick_due(t.get("deadline")),
            )
            if tt_id:
                await repo.set_task_ticktick_id(dedup, tt_id)
            logger.info("Chat %s: created TickTick task '%s' in %s", chat_id, title, project_name)
        except Exception:  # noqa: BLE001
            logger.exception("Chat %s: TickTick create_task failed for '%s'", chat_id, title)


def _task_note(t: dict[str, Any]) -> str:
    bits = []
    who = t.get("who")
    if who == "counterparty":
        name = t.get("counterpartyName")
        bits.append(f"Responsible: {name}" if name else "Responsible: counterparty")
    elif who == "me":
        bits.append("Responsible: me")
    return "\n".join(bits)


async def _apply_status_updates(
    chat_id: str, open_tasks: list[dict[str, Any]], updates: list[dict[str, Any]]
) -> None:
    if not updates:
        return
    tt = get_ticktick()
    for u in updates:
        match = u.get("task_match", "")
        new_status = u.get("new_status")
        if new_status not in ("done", "cancelled"):
            continue
        task = _match_open_task(match, open_tasks)
        if task is None:
            logger.debug("Chat %s: status update had no matching open task: %s", chat_id, match)
            continue

        updated = await repo.update_task_status(chat_id, task["dedupHash"], new_status)
        if not updated:
            continue

        # Reflect completion in TickTick (cancelled tasks are just closed locally
        # unless we also want to complete them remotely — we complete `done` only).
        tt_id = task.get("ticktickTaskId")
        project_id = task.get("projectId")
        if new_status == "done" and tt_id and project_id:
            try:
                await tt.complete_task(project_id=project_id, task_id=tt_id)
                logger.info("Chat %s: completed TickTick task '%s'", chat_id, task["task"])
            except Exception:  # noqa: BLE001
                logger.exception("Chat %s: TickTick complete_task failed", chat_id)


def _match_open_task(match: str, open_tasks: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Resolve Claude's free-text task reference to a stored open task."""
    norm = repo.normalize_task(match)
    # 1) exact normalized match
    for t in open_tasks:
        if repo.normalize_task(t["task"]) == norm:
            return t
    # 2) containment either direction
    for t in open_tasks:
        nt = repo.normalize_task(t["task"])
        if norm and (norm in nt or nt in norm):
            return t
    return None
