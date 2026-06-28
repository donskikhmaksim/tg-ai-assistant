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
from datetime import timezone
from typing import Any
from urllib.parse import quote

from .. import repositories as repo
from ..config import get_settings
from ..llm import claude, qwen
from ..ticktick.mcp_client import get_ticktick
from ..web.auth import chat_link_token
from . import retrieve as retrieval
from .dedup import _zone, is_all_day_deadline, to_ticktick_due
from .windows import build_window, render_window

logger = logging.getLogger(__name__)


def _build_chat_context(doc: dict) -> str:
    """Build a shared context preamble from chat settings fields."""
    lines = []
    if doc.get("who"):
        lines.append(f"Кто этот человек / о чём чат: {doc['who']}")
    if doc.get("topics"):
        lines.append(f"Темы переписки: {doc['topics']}")
    if doc.get("task_side"):
        lines.append(f"Кому обычно ставятся задачи: {doc['task_side']}")
    if not lines:
        return ""
    return "--- Контекст этого чата ---\n" + "\n".join(lines) + "\n---"


def _merge_settings(global_doc: dict, chat_doc: dict) -> dict:
    """Merge global defaults with per-chat settings (per-chat wins on non-empty values)."""
    return {**global_doc, **{k: v for k, v in chat_doc.items() if v}}


async def run_batch() -> None:
    s = get_settings()
    chats = await repo.get_dirty_chats(s.quiet_minutes, s.max_dirty_minutes)
    if not chats:
        logger.debug("Batch: no chats ready")
        return
    logger.info("Batch: processing %d ready chat(s)", len(chats))
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

    # Archive embeddings for retrieval (dedup'd; permanent, survives raw TTL).
    await retrieval.index_messages(chat_id, messages)

    # Load settings: merge global defaults with per-chat overrides.
    global_doc = await repo.get_global_settings()
    per_chat_doc = await repo.get_chat_settings(chat_id)
    settings_doc = _merge_settings(global_doc, per_chat_doc)

    chat_context = _build_chat_context(settings_doc)
    filter_rules = settings_doc.get("filter_rules")
    extract_rules = settings_doc.get("extract_rules")
    importance = settings_doc.get("importance")
    people = settings_doc.get("people")

    # Tier 1 — cheap local gate (importance injected here too).
    if not await qwen.has_task(
        window_text,
        chat_context=chat_context,
        filter_rules=filter_rules,
        importance=importance,
    ):
        logger.info("Chat %s: Qwen says no task", chat_id)
        await repo.mark_processed(chat_id)
        return

    # Deep recall: relevant OLDER messages beyond the window + summary.
    window_ids = {m["messageId"] for m in window}
    retrieved = await retrieval.retrieve(chat_id, window_text, window_ids)

    # Tier 2 — Claude with long-term memory + retrieved context.
    summary = await repo.get_summary(chat_id)
    open_tasks = await repo.get_open_tasks(chat_id)
    result = await claude.extract(
        window_text, summary, open_tasks, retrieved,
        chat_context=chat_context,
        extract_rules=extract_rules,
        importance=importance,
        people=people,
    )

    # Memory first: persist the refreshed summary before raw expires.
    new_summary = result.get("updated_summary")
    if new_summary:
        await repo.set_summary(chat_id, new_summary)

    await _create_new_tasks(chat_id, result.get("new_tasks", []), messages)
    await _apply_status_updates(chat_id, open_tasks, result.get("status_updates", []))

    await repo.mark_processed(chat_id)


async def _resolve_project(chat_id: str) -> tuple[str | None, str, str | None]:
    """Returns (projectId, projectName, sectionId) for a chat's tasks.

    Explicit binding wins (and may pin a section/column). Otherwise tasks fall
    back to the configured default project (DEFAULT_PROJECT), resolved by name
    to a real TickTick id so they actually land in an inbox instead of only
    being stored locally. If the default name matches no project, we return
    (None, name, None) and the task stays local until the chat is bound.
    """
    binding = await repo.get_project_binding(chat_id)
    if binding:
        return (
            binding["ticktickProjectId"],
            binding.get("projectName", ""),
            binding.get("ticktickSectionId"),
        )

    s = get_settings()
    default_name = s.default_project
    # Prefer an explicit id (e.g. the built-in Inbox, which get_projects omits).
    if s.default_project_id:
        pid = s.default_project_id
        return pid, default_name or "Inbox", await _resolve_default_section(pid)
    # Otherwise resolve the configured default project name to a real id.
    if default_name:
        try:
            for p in await get_ticktick().get_projects():
                if p["name"] == default_name:
                    return p["id"], p["name"], await _resolve_default_section(p["id"])
        except Exception:  # noqa: BLE001
            logger.exception("Default project lookup failed for %r", default_name)
    return None, default_name, None


async def _resolve_default_section(project_id: str | None) -> str | None:
    """Column id of the configured default section inside `project_id`.

    Unbound ("мои") tasks land in this section so they're easy to triage.
    DEFAULT_SECTION_ID (an explicit column id) wins and bypasses the name
    lookup — necessary for the built-in Inbox, whose columns the API won't list.
    Otherwise the column is found by name (DEFAULT_SECTION). None if nothing
    matches or lookup fails — the task then goes to the project root."""
    s = get_settings()
    if s.default_section_id:
        return s.default_section_id
    name = s.default_section
    if not name or not project_id:
        return None
    sections = await get_ticktick().get_sections(project_id)
    # Diagnostic: surface exactly what the server lists for this project, so the
    # column id can be read from logs and pinned via DEFAULT_SECTION_ID.
    logger.info(
        "Sections in default project %s: %s",
        project_id,
        [(c.get("name"), c.get("id")) for c in sections],
    )
    for c in sections:
        if c["name"].strip().lower() == name.strip().lower():
            return c["id"]
    logger.info("Default section %r not found in project %s", name, project_id)
    return None


async def _create_new_tasks(
    chat_id: str, new_tasks: list[dict[str, Any]], messages: list[dict[str, Any]] | None = None
) -> None:
    if not new_tasks:
        return
    project_id, project_name, section_id = await _resolve_project(chat_id)
    source = _source_label(chat_id, await repo.get_chat_title(chat_id))
    is_group = chat_id.startswith("group_")
    default_tz = get_settings().default_timezone
    date_by_id = {m["messageId"]: m.get("date") for m in (messages or [])}
    link = _chat_link(chat_id)
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
            "details": t.get("details"),
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
            when = _source_time(t.get("source_message_ids"), date_by_id, default_tz)
            note = _task_note(t, source, when=when, link=link, is_group=is_group)
            tt_id = await tt.create_task(
                title=title,
                project_id=project_id,
                content=note,
                due_date=to_ticktick_due(t.get("deadline"), t.get("deadline_tz"), default_tz),
                section_id=section_id,
                is_all_day=is_all_day_deadline(t.get("deadline")),
            )
            if tt_id:
                await repo.set_task_ticktick_id(dedup, tt_id)
            logger.info("Chat %s: created TickTick task '%s' in %s", chat_id, title, project_name)
        except Exception:  # noqa: BLE001
            logger.exception("Chat %s: TickTick create_task failed for '%s'", chat_id, title)


def _source_label(chat_id: str, title: str | None) -> str | None:
    """Human-readable task source: group vs DM, with its name."""
    if not title:
        return "группа" if chat_id.startswith("group_") else None
    if chat_id.startswith("group_"):
        return f"группа «{title}»"
    return f"личка с «{title}»"


def _person(value: str | None) -> str | None:
    """Render a from/to name; the literal "me" becomes «я»."""
    if not value:
        return None
    return "я" if value.strip().lower() == "me" else value.strip()


def _source_time(
    ids: list[int] | None, date_by_id: dict[int, Any], tz_name: str
) -> str | None:
    """When the task was said: time of the latest source message, in tz_name."""
    dates = [date_by_id.get(i) for i in (ids or []) if date_by_id.get(i)]
    if not dates:
        return None
    dt = max(dates)
    zone = _zone(tz_name) or timezone.utc
    try:
        return dt.astimezone(zone).strftime("%d.%m %H:%M")
    except (ValueError, OSError):
        return None


def _chat_link(chat_id: str) -> str | None:
    """Link to the transcript page for this chat (token-gated). None if no
    WEBAPP_URL configured."""
    s = get_settings()
    base = (s.webapp_url or "").rstrip("/")
    if not base:
        return None
    token = chat_link_token(chat_id, s.bot_token)
    return f"{base}/chat?c={quote(chat_id)}&t={token}"


def _task_note(
    t: dict[str, Any],
    source: str | None = None,
    when: str | None = None,
    link: str | None = None,
    is_group: bool = False,
) -> str:
    # Markdown — TickTick renders it in the task description.
    meta = []
    if source:
        meta.append(f"**Источник:** {source}")
    if is_group:
        # In a group "who said it" and "who must do it" can differ — show both.
        frm = _person(t.get("from_name"))
        to = _person(t.get("to_name"))
        if frm or to:
            meta.append(f"**От:** {frm or '—'} · **Кому:** {to or '—'}")
    if when:
        meta.append(f"**Когда:** {when}")
    # In a DM, note responsibility only when it's the counterparty (own → no label).
    if not is_group and t.get("who") == "counterparty":
        name = t.get("counterpartyName")
        meta.append(f"**Ответственный:** {name or 'собеседник'}")

    blocks = []
    if meta:
        # Hard line breaks inside the meta block (two trailing spaces = <br> in md).
        blocks.append("  \n".join(meta))
    details = (t.get("details") or "").strip()
    if details:
        blocks.append(details)
    if link:
        blocks.append(f"[💬 Прочитать переписку]({link})")
    return "\n\n".join(blocks)


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
