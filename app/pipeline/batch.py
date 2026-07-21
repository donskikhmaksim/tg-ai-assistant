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
from ..embeddings import embed
from ..llm import claude, qwen
from ..onboarding.ticktick_resolve import get_user_ticktick
from ..ticktick.mcp_client import TickTickMCP
from ..web.auth import chat_link_token
from . import retrieve as retrieval
from . import semantic_dedup as sd
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


def _as_float(value: Any, default: float) -> float:
    """Parse a settings value (often a string from the Mini App) to float."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


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

    window_text = render_window(window, s.default_timezone)

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
    # «Контроль» attribution toggle + marker/tag: per-chat override, else global,
    # else env default.
    control_mode = settings_doc.get("control_mode") or s.control_mode
    control_marker = settings_doc.get("control_marker") or s.control_marker
    control_tag = settings_doc.get("control_tag") or s.control_tag
    # Per-chat/global extraction model + effort + editable base prompt: per-chat
    # override, else global, else env/config default. Empty strings fall through.
    extract_model = settings_doc.get("extract_model") or s.extract_model
    extract_effort = settings_doc.get("extract_effort") or s.extract_effort
    extract_system_prompt = settings_doc.get("system_prompt") or s.system_prompt
    # Tier-1 endpoint (Mini App global, else env). Empty → tier-1 skipped entirely.
    qwen_base_url = settings_doc.get("qwen_base_url") or s.qwen_base_url
    # Semantic near-duplicate dedup: per-chat override, else global, else env.
    dedup_semantic = settings_doc.get("dedup_semantic") or s.dedup_semantic
    dedup_low = _as_float(settings_doc.get("dedup_low"), s.dedup_low)
    dedup_high = _as_float(settings_doc.get("dedup_high"), s.dedup_high)

    # Tier 1 — cheap local gate (importance injected here too).
    if not await qwen.has_task(
        window_text,
        chat_context=chat_context,
        filter_rules=filter_rules,
        importance=importance,
        base_url=qwen_base_url,
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
        model=extract_model,
        effort=extract_effort,
        system_prompt=extract_system_prompt,
    )

    # Memory first: persist the refreshed summary before raw expires.
    new_summary = result.get("updated_summary")
    if new_summary:
        await repo.set_summary(chat_id, new_summary)

    # Multi-tenant: route this chat's tasks to ITS owner's own TickTick. No
    # shared account — a chat whose owner has no connector keeps tasks local.
    owner = await repo.resolve_chat_owner(chat_id)
    tt = await get_user_ticktick(owner)
    if tt is None:
        logger.info(
            "Chat %s: owner %s has no TickTick connector — tasks kept local",
            chat_id, owner,
        )

    smap = await _resolve_section_map(chat_id)
    await _create_new_tasks(
        chat_id, result.get("new_tasks", []), messages, smap, tt,
        control_mode, control_marker, control_tag,
        open_tasks=open_tasks,
        sem_mode=dedup_semantic,
        sem_low=dedup_low,
        sem_high=dedup_high,
        sem_cap=s.dedup_project_task_cap,
    )
    await _apply_status_updates(chat_id, open_tasks, result.get("status_updates", []), smap, tt)
    await _route_rejected(chat_id, result.get("rejected", []), messages, smap, tt)

    await repo.mark_processed(chat_id)


async def _resolve_project(
    chat_id: str, tt: TickTickMCP | None
) -> tuple[str | None, str, str | None]:
    """Returns (projectId, projectName, sectionId) for a chat's tasks.

    Explicit binding wins (and may pin a section/column). Otherwise tasks fall
    back to the default project: the per-user GLOBAL setting (DEFAULT_PROJECT_ID
    set in the Mini App) takes priority, then the env default (DEFAULT_PROJECT_ID
    / DEFAULT_PROJECT by name), resolved to a real TickTick id so they actually
    land in an inbox instead of only being stored locally. If nothing matches we
    return (None, name, None) and the task stays local until the chat is bound.
    Remote lookups use THIS owner's `tt` client; without one we can only use
    binding/env ids and otherwise keep the task local.
    """
    binding = await repo.get_project_binding(chat_id)
    if binding:
        return (
            binding["ticktickProjectId"],
            binding.get("projectName", ""),
            binding.get("ticktickSectionId"),
        )

    s = get_settings()
    g = await repo.get_global_settings()
    default_name = s.default_project
    # Per-user global default (set in the Mini App) overrides the env default —
    # important for self-host, where DEFAULT_SECTION=TG shouldn't be baked in.
    default_id = (g.get("default_project_id") or s.default_project_id) or None
    # Prefer an explicit id (e.g. the built-in Inbox, which get_projects omits).
    if default_id:
        return default_id, default_name or "Inbox", await _resolve_default_section(default_id, tt, g)
    # Otherwise resolve the configured default project name to a real id.
    if default_name and tt is not None:
        try:
            for p in await tt.get_projects():
                if p["name"] == default_name:
                    return p["id"], p["name"], await _resolve_default_section(p["id"], tt, g)
        except Exception:  # noqa: BLE001
            logger.exception("Default project lookup failed for %r", default_name)
    return None, default_name, None


async def _resolve_default_section(
    project_id: str | None, tt: TickTickMCP | None, global_doc: dict | None = None
) -> str | None:
    """Column id of the configured default section inside `project_id`.

    Unbound ("мои") tasks land in this section so they're easy to triage. The
    per-user GLOBAL setting (DEFAULT_SECTION_ID set in the Mini App) wins, then
    the env DEFAULT_SECTION_ID (an explicit column id) — both bypass the name
    lookup, necessary for the built-in Inbox whose columns the API won't list.
    Otherwise the column is found by name (DEFAULT_SECTION). None if nothing
    matches or lookup fails — the task then goes to the project root."""
    s = get_settings()
    g = global_doc or {}
    section_id = g.get("default_section_id") or s.default_section_id
    if section_id:
        return section_id
    name = s.default_section
    if not name or not project_id or tt is None:
        return None
    sections = await tt.get_sections(project_id)
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


async def _resolve_section_map(chat_id: str) -> dict[str, Any]:
    """Optional per-category section routing. Returns:
      {enabled: bool, open|done|cancelled|rejected: {"id","name"} | None}
    Taken from the chat's section_map, else the global one (whole unit, not
    field-merged). When disabled, the pipeline keeps its default behavior:
    open tasks go to the default section, done/cancelled close locally, rejected
    are dropped."""
    chat_map = (await repo.get_chat_settings(chat_id)).get("section_map")
    smap = chat_map or (await repo.get_global_settings()).get("section_map") or {}
    return {
        "enabled": bool(smap.get("enabled")),
        "open": smap.get("open"),
        "done": smap.get("done"),
        "cancelled": smap.get("cancelled"),
        "rejected": smap.get("rejected"),
    }


def _section_for(smap: dict[str, Any], category: str) -> str | None:
    """The configured column id for a category, or None if unset/disabled."""
    if not smap.get("enabled"):
        return None
    entry = smap.get(category)
    return (entry or {}).get("id")


def _control_decision(chat_id: str, who: str | None, control_mode: str) -> str:
    """Attribution decision for a single extracted DM task. Returns one of
    "normal" | "control" | "skip".

    Only DMs (chatId `user_…`) are affected — groups keep their current behavior
    (from/to names already carry attribution). In a DM an action can be on the
    OWNER (who="me" → a normal to-do) or on the COUNTERPARTY (who="counterparty",
    delegated or self-volunteered) → a «Контроль» item the owner only TRACKS.
    `control_mode` toggles those: "off" skips them entirely (owner wants only
    their own tasks), anything else ("on", default) creates and marks them."""
    if not chat_id.startswith("user_"):
        return "normal"
    if (who or "me") != "counterparty":
        return "normal"
    return "skip" if control_mode == "off" else "control"


def _control_title(title: str, is_control: bool, marker: str) -> str:
    """The TickTick title for a task: a «Контроль» item gets the configured
    marker prefixed. An empty marker (user cleared it) leaves the title bare —
    the tag still carries the signal."""
    if is_control and marker:
        return f"{marker} {title}"
    return title


def _local_candidate(task: dict[str, Any], embedding: list[float]) -> dict[str, Any]:
    """A semantic-dedup candidate from a local (Mongo) open task."""
    return {
        "title": task.get("task"),
        "embedding": embedding,
        "chatId": task.get("chatId"),
        "details": task.get("details"),
        "dedupHash": task.get("dedupHash"),
        "ticktickTaskId": task.get("ticktickTaskId"),
        "projectId": task.get("projectId"),
    }


def _project_candidate(
    task: dict[str, str], project_id: str, embedding: list[float]
) -> dict[str, Any]:
    """A semantic-dedup candidate from a TickTick project task (no local doc)."""
    return {
        "title": task.get("title"),
        "embedding": embedding,
        "chatId": None,
        "details": None,
        "dedupHash": None,
        "ticktickTaskId": task.get("id"),
        "projectId": project_id,
    }


async def _cached_candidates(
    scope: str,
    items: list[tuple[dict[str, Any], str, str]],
    make: Any,
) -> list[dict[str, Any]]:
    """Resolve embeddings for `items` = [(raw, cache_key, title), …] under one
    cache `scope`, reusing stored vectors and embedding only new/changed titles
    (one batched call), then persisting the fresh ones. `make(raw, embedding)`
    builds each candidate. Fail-soft: items whose embedding can't be produced
    are simply omitted."""
    cache = await repo.get_task_vectors(scope)
    candidates: list[dict[str, Any]] = []
    to_embed: list[tuple[dict[str, Any], str, str]] = []
    for raw, key, title in items:
        cached = cache.get(key)
        if cached and cached.get("title") == title and cached.get("embedding"):
            candidates.append(make(raw, cached["embedding"]))
        else:
            to_embed.append((raw, key, title))
    if to_embed:
        vecs = await embed([title for _, _, title in to_embed])
        if vecs and len(vecs) == len(to_embed):
            store = []
            for (raw, key, title), vec in zip(to_embed, vecs):
                candidates.append(make(raw, vec))
                store.append({"key": key, "title": title, "embedding": vec})
            await repo.store_task_vectors(scope, store)
    return candidates


async def _build_semantic_dedup(
    chat_id: str,
    new_tasks: list[dict[str, Any]],
    open_tasks: list[dict[str, Any]],
    project_id: str | None,
    tt: TickTickMCP | None,
    sem_mode: str,
    sem_cap: int,
) -> tuple[list[dict[str, Any]] | None, dict[str, list[float]]]:
    """Prepare the semantic near-duplicate check for one chat.

    Returns (candidates, title_vectors). `candidates` is the list of existing
    tasks (this chat's open tasks + the bound project's tasks) with embeddings,
    or None when semantic dedup is off/unavailable (caller then relies on the
    exact-hash guard only). `title_vectors` maps each new-task title to its query
    embedding (a single batched call). If either the candidates or the query
    embeddings can't be produced, we return (None, {}) so the pipeline degrades
    cleanly to exact-hash dedup — never crashing a self-host without a model."""
    if sem_mode == "off" or not get_settings().embed_model:
        return None, {}

    # Query embeddings for the new task titles (one batch).
    uniq_titles = sorted({
        (t.get("task") or "").strip() for t in new_tasks if (t.get("task") or "").strip()
    })
    if not uniq_titles:
        return None, {}
    qvecs = await embed(uniq_titles)
    if not qvecs or len(qvecs) != len(uniq_titles):
        return None, {}  # embeddings down → exact-hash fallback
    title_vecs = dict(zip(uniq_titles, qvecs))

    # Candidate 1: this chat's local open tasks (Mongo), cached under chatId.
    local_items = [
        (ot, ot["dedupHash"], (ot.get("task") or "").strip())
        for ot in open_tasks
        if (ot.get("task") or "").strip()
    ]
    candidates = await _cached_candidates(chat_id, local_items, _local_candidate)

    # Candidate 2: tasks already in the bound TickTick project (capped), cached
    # under the project scope so multiple chats sharing a project reuse them.
    if tt is not None and project_id:
        proj_tasks = await tt.get_project_tasks(project_id, limit=sem_cap)
        proj_items = [
            (pt, f"tt:{pt['id']}", (pt.get("title") or "").strip())
            for pt in proj_tasks[:sem_cap]
            if pt.get("id") and (pt.get("title") or "").strip()
        ]
        candidates += await _cached_candidates(
            f"proj:{project_id}",
            proj_items,
            lambda pt, emb: _project_candidate(pt, project_id, emb),
        )

    return candidates, title_vecs


async def _enrich_duplicate(
    chat_id: str,
    match: dict[str, Any],
    new_task: dict[str, Any],
    tt: TickTickMCP | None,
) -> None:
    """Best-effort enrichment of the existing task a new one duplicates. Appends
    only genuinely-new detail: to the local Mongo doc (when the match is a local
    task) and, if the task lives in TickTick, as a comment (append-only, so no
    overwrite risk). Never raises — a failed enrich still means we skipped the
    duplicate, which is the primary goal."""
    extra = sd.merge_details(match.get("details"), new_task.get("details"))
    if not extra:
        return
    if match.get("dedupHash") and match.get("chatId"):
        try:
            await repo.append_task_details(match["chatId"], match["dedupHash"], extra)
        except Exception:  # noqa: BLE001
            logger.exception("Chat %s: local task enrich failed", chat_id)
    tt_id = match.get("ticktickTaskId")
    project_id = match.get("projectId")
    if tt is not None and tt_id and project_id:
        try:
            await tt.add_task_comment(
                project_id, tt_id, extra, task_title=match.get("title") or ""
            )
        except Exception:  # noqa: BLE001 — enrich comment is best-effort
            logger.debug(
                "Chat %s: TickTick enrich comment failed (best-effort)",
                chat_id, exc_info=True,
            )


async def _create_new_tasks(
    chat_id: str,
    new_tasks: list[dict[str, Any]],
    messages: list[dict[str, Any]] | None = None,
    smap: dict[str, Any] | None = None,
    tt: TickTickMCP | None = None,
    control_mode: str = "on",
    control_marker: str = "👁",
    control_tag: str = "контроль",
    open_tasks: list[dict[str, Any]] | None = None,
    sem_mode: str = "on",
    sem_low: float = 0.83,
    sem_high: float = 0.93,
    sem_cap: int = 200,
) -> None:
    if not new_tasks:
        return
    smap = smap or {"enabled": False}
    project_id, project_name, section_id = await _resolve_project(chat_id, tt)
    # Open (real) tasks ALWAYS fly. If a section is configured for "open", route
    # them there; otherwise keep the default section (never "nowhere").
    open_section = _section_for(smap, "open")
    if open_section:
        section_id = open_section

    # Semantic near-duplicate guard (belt-and-suspenders on top of the exact-hash
    # index). Build the candidate set + query embeddings ONCE per chat; degrades
    # to plain exact-hash dedup if embeddings are off/unavailable.
    matcher, title_vecs = await _build_semantic_dedup(
        chat_id, new_tasks, open_tasks or [], project_id, tt, sem_mode, sem_cap
    )
    chat_settings = await repo.get_chat_settings(chat_id)
    display_name = chat_settings.get("alias") or await repo.get_chat_title(chat_id)
    source = _source_label(chat_id, display_name)
    is_group = chat_id.startswith("group_")
    default_tz = get_settings().default_timezone
    date_by_id = {m["messageId"]: m.get("date") for m in (messages or [])}

    for t in new_tasks:
        title = (t.get("task") or "").strip()
        if not title:
            continue
        # DM attribution: skip / mark counterparty-action tasks as «Контроль».
        decision = _control_decision(chat_id, t.get("who"), control_mode)
        if decision == "skip":
            logger.info("Chat %s: control_mode=off, skipping counterparty task: %s", chat_id, title)
            continue
        is_control = decision == "control"

        # Semantic dedup: compare against the single best-matching existing task
        # (this chat's open tasks or the bound project) and decide by band —
        # ≥high auto-duplicate, ≤low distinct, gray zone asks the LLM judge.
        # Bias to safe: any uncertainty → create (never drop a real task).
        qvec = title_vecs.get(title) if matcher is not None else None
        if matcher is not None and qvec is not None:
            match = sd.best_match(qvec, matcher, sem_low)
            if match is not None:
                async def _judge(_title=title, _match_title=match.get("title")):
                    return await claude.judge_same_task(_title, _match_title)

                if await sd.decide_duplicate(match["score"], sem_low, sem_high, _judge):
                    await _enrich_duplicate(chat_id, match, t, tt)
                    logger.info(
                        "Chat %s: semantic duplicate (%.3f) of %r — enriched, not creating: %s",
                        chat_id, match["score"], match.get("title"), title,
                    )
                    continue

        dedup = repo.dedup_hash(chat_id, title)
        task_doc = {
            "chatId": chat_id,
            "task": title,
            "who": t.get("who", "me"),
            "control": is_control,
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

        # Register the fresh task so later tasks in THIS batch dedup against it,
        # and persist its embedding for cheap future runs.
        if matcher is not None and qvec is not None:
            matcher.append({
                "title": title, "embedding": qvec, "chatId": chat_id,
                "details": t.get("details"), "dedupHash": dedup,
                "ticktickTaskId": None, "projectId": project_id,
            })
            await repo.store_task_vectors(
                chat_id, [{"key": dedup, "title": title, "embedding": qvec}]
            )

        # Push to TickTick. project_id=None means Inbox; we need a real id, so
        # only create remotely when a project is bound. No connector (tt is None)
        # means this owner hasn't connected their TickTick — keep the task local.
        # Either way the task is recorded locally and can be synced later.
        if project_id is None or tt is None:
            logger.info("Chat %s: task stored locally (no project/connector): %s", chat_id, title)
            continue
        try:
            when = _source_time(t.get("source_message_ids"), date_by_id, default_tz)
            # Deep-link to this task's source messages so the transcript page
            # scrolls to and highlights exactly where it was discussed.
            link = _chat_link(chat_id, t.get("source_message_ids"))
            note = _task_note(t, source, when=when, link=link, is_group=is_group, is_control=is_control)
            # «Контроль» items get a visible title marker + a tag in TickTick (the
            # stored `task` stays raw so dedup/matching are unaffected).
            tt_title = _control_title(title, is_control, control_marker)
            tt_tags = [control_tag] if (is_control and control_tag) else None
            tt_id = await tt.create_task(
                title=tt_title,
                project_id=project_id,
                content=note,
                due_date=to_ticktick_due(t.get("deadline"), t.get("deadline_tz"), default_tz),
                section_id=section_id,
                is_all_day=is_all_day_deadline(t.get("deadline")),
                tags=tt_tags,
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


def _chat_link(chat_id: str, source_ids: list[int] | None = None) -> str | None:
    """Link to the transcript page for this chat (token-gated). None if no
    WEBAPP_URL configured. When `source_ids` are given they're appended as
    `&m=<id1>,<id2>` so the page highlights and scrolls to those messages."""
    s = get_settings()
    base = (s.webapp_url or "").rstrip("/")
    if not base:
        return None
    token = chat_link_token(chat_id, s.bot_token)
    url = f"{base}/chat?c={quote(chat_id)}&t={token}"
    ids = ",".join(str(i) for i in source_ids if i is not None) if source_ids else ""
    if ids:
        url += f"&m={quote(ids)}"
    return url


def _task_note(
    t: dict[str, Any],
    source: str | None = None,
    when: str | None = None,
    link: str | None = None,
    is_group: bool = False,
    is_control: bool = False,
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
    # A «Контроль» item — the owner tracks someone else's commitment — says so.
    if is_control:
        name = t.get("counterpartyName")
        meta.append(f"**👁 Контроль** · ответственный: {name or 'собеседник'}")
    elif not is_group and t.get("who") == "counterparty":
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
    chat_id: str, open_tasks: list[dict[str, Any]], updates: list[dict[str, Any]],
    smap: dict[str, Any] | None = None,
    tt: TickTickMCP | None = None,
) -> None:
    if not updates:
        return
    smap = smap or {"enabled": False}
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

        tt_id = task.get("ticktickTaskId")
        project_id = task.get("projectId")
        section = _section_for(smap, new_status)  # done / cancelled column, if configured

        if tt is None:
            continue  # local status already updated; no connector to sync to

        if tt_id and project_id:
            # Already in TickTick: complete it (done). cancelled stays closed
            # locally unless a section is configured (then we complete it too so
            # it lands as closed in the archive column via a later move — for now
            # we just complete `done`, matching prior behavior).
            if new_status == "done":
                try:
                    await tt.complete_task(project_id=project_id, task_id=tt_id)
                    logger.info("Chat %s: completed TickTick task '%s'", chat_id, task["task"])
                except Exception:  # noqa: BLE001
                    logger.exception("Chat %s: TickTick complete_task failed", chat_id)
        elif section and project_id:
            # Never pushed, but a section is configured for this category → archive
            # it there so nothing is silently lost. Create then complete (done).
            try:
                note = f"[{new_status}] {task.get('details') or ''}".strip()
                new_id = await tt.create_task(
                    title=task["task"], project_id=project_id,
                    content=note or None, section_id=section,
                )
                if new_status == "done" and new_id:
                    await tt.complete_task(project_id=project_id, task_id=new_id)
                logger.info("Chat %s: archived %s task '%s' to section %s",
                            chat_id, new_status, task["task"], section)
            except Exception:  # noqa: BLE001
                logger.exception("Chat %s: archive of %s task failed", chat_id, new_status)
        # else: no tt_id and no configured section → close locally only (default).


async def _route_rejected(
    chat_id: str, rejected: list[dict[str, Any]],
    messages: list[dict[str, Any]] | None = None,
    smap: dict[str, Any] | None = None,
    tt: TickTickMCP | None = None,
) -> None:
    """Rejected (junk / false-positive) candidates. Only surfaced to TickTick when
    a 'rejected' section is configured — otherwise they're dropped (default)."""
    smap = smap or {"enabled": False}
    section = _section_for(smap, "rejected")
    if not section or not rejected or tt is None:
        return
    project_id, _project_name, _default_section = await _resolve_project(chat_id, tt)
    if not project_id:
        return
    for r in rejected:
        title = (r.get("task") or "").strip()
        if not title:
            continue
        try:
            await tt.create_task(
                title=title, project_id=project_id,
                content=f"Отклонено: {r.get('reason', '')}".strip(),
                section_id=section,
            )
            logger.info("Chat %s: routed rejected item '%s' to review section", chat_id, title)
        except Exception:  # noqa: BLE001
            logger.exception("Chat %s: routing rejected item failed", chat_id)


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
