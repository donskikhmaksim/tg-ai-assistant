"""Оркестрация батч-обработки одного чата и всего прогона (§7 ТЗ)."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from app.config import Settings
from app.db import repositories as repo
from app.dedup import dedup_hash, normalize_task
from app.llm.claude import ClaudeExtractor
from app.llm.qwen import QwenTriage
from app.mcp.ticktick import TickTickMCP
from app.models import ExtractionResult, NewTask, OpenTask
from app.pipeline.windows import build_window, render_window

log = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _deadline_to_iso(deadline: str | None) -> str | None:
    """'YYYY-MM-DD' -> ISO с временем для TickTick, иначе None."""
    if not deadline:
        return None
    try:
        datetime.strptime(deadline, "%Y-%m-%d")
    except ValueError:
        return None
    return f"{deadline}T09:00:00+0000"


class Processor:
    def __init__(
        self,
        settings: Settings,
        qwen: QwenTriage,
        claude: ClaudeExtractor,
        ticktick: TickTickMCP,
    ) -> None:
        self._s = settings
        self._qwen = qwen
        self._claude = claude
        self._ticktick = ticktick
        self._default_project_id: str | None = None

    # ── разрешение проекта (§7.6) ─────────────────────────────────────────────
    async def _resolve_default_project_id(self) -> str | None:
        if self._default_project_id is not None:
            return self._default_project_id
        try:
            projects = await self._ticktick.get_projects()
        except Exception:  # noqa: BLE001
            log.exception("Не удалось получить список проектов TickTick")
            return None
        want = self._s.default_project.strip().lower()
        for p in projects:
            if p["name"].strip().lower() == want:
                self._default_project_id = p["id"]
                break
        if self._default_project_id is None:
            log.warning(
                "Проект по умолчанию '%s' не найден среди проектов TickTick",
                self._s.default_project,
            )
        return self._default_project_id

    async def _project_for_chat(self, chat_id: str) -> str | None:
        mapping = await repo.get_project_mapping(chat_id)
        if mapping and mapping.get("ticktickProjectId"):
            return mapping["ticktickProjectId"]
        return await self._resolve_default_project_id()

    # ── обработка одного чата ─────────────────────────────────────────────────
    async def process_chat(self, chat_id: str) -> None:
        since = _now() - timedelta(hours=self._s.max_lookback_hours)
        docs = await repo.fetch_recent_messages(chat_id, since)
        window = build_window(docs, self._s.conv_gap_hours)
        if not window:
            await repo.set_last_processed(chat_id)
            return

        window_text = render_window(window)

        # Tier 1 — Qwen-ворота
        if not await self._qwen.has_task(window_text):
            log.info("chat=%s: Qwen — задач нет", chat_id)
            await repo.set_last_processed(chat_id)
            return

        # Tier 2 — Claude разбор с долговременной памятью
        summary = await repo.get_summary(chat_id)
        open_tasks = await repo.get_open_tasks(chat_id)
        result = await self._claude.extract(window_text, summary, open_tasks)

        # резюме переезжает в долговременную память до того, как сырьё затрётся
        if result.updated_summary:
            await repo.upsert_summary(chat_id, result.updated_summary)

        await self._apply_new_tasks(chat_id, result)
        await self._apply_status_updates(chat_id, result, open_tasks)

        await repo.set_last_processed(chat_id)
        log.info(
            "chat=%s: новых задач=%d, статус-апдейтов=%d",
            chat_id,
            len(result.new_tasks),
            len(result.status_updates),
        )

    async def _apply_new_tasks(self, chat_id: str, result: ExtractionResult) -> None:
        for nt in result.new_tasks:
            dh = dedup_hash(chat_id, nt.task)
            if await repo.task_exists(dh):  # дедуп (§7.5)
                continue
            project_id = await self._project_for_chat(chat_id)
            ticktick_id = await self._create_ticktick(nt, project_id)

            doc = {
                "chatId": chat_id,
                "task": nt.task,
                "who": nt.who,
                "counterpartyName": nt.counterpartyName,
                "deadline": nt.deadline,
                "status": "open",
                "sourceMessageIds": nt.source_message_ids,
                "dedupHash": dh,
                "ticktickTaskId": ticktick_id,
                "projectId": project_id,
            }
            inserted = await repo.insert_task(doc)
            if not inserted:
                log.info("chat=%s: задача-дубль пропущена при вставке", chat_id)

    async def _create_ticktick(
        self, nt: NewTask, project_id: str | None
    ) -> str | None:
        if not project_id:
            log.warning("Нет project_id — задача сохранена только в БД: %r", nt.task)
            return None
        who = "я" if nt.who == "me" else (nt.counterpartyName or "собеседник")
        content = f"Исполнитель: {who}"
        try:
            return await self._ticktick.create_task(
                title=nt.task,
                project_id=project_id,
                content=content,
                due_date=_deadline_to_iso(nt.deadline),
            )
        except Exception:  # noqa: BLE001
            log.exception("Ошибка создания задачи в TickTick: %r", nt.task)
            return None

    async def _apply_status_updates(
        self, chat_id: str, result: ExtractionResult, open_tasks: list[OpenTask]
    ) -> None:
        for su in result.status_updates:
            matched = self._match_open_task(su.task_match, open_tasks)
            if matched is None:
                continue
            await repo.update_task_status(matched.dedup_hash, su.new_status)
            # done → закрыть в TickTick (cancelled оставляем только в БД)
            if (
                su.new_status == "done"
                and matched.ticktick_task_id
                and matched.project_id
            ):
                try:
                    await self._ticktick.complete_task(
                        matched.project_id, matched.ticktick_task_id
                    )
                except Exception:  # noqa: BLE001
                    log.exception("Ошибка закрытия задачи в TickTick")

    @staticmethod
    def _match_open_task(
        task_match: str, open_tasks: list[OpenTask]
    ) -> OpenTask | None:
        norm = normalize_task(task_match)
        # точное совпадение по нормализованному тексту
        for t in open_tasks:
            if normalize_task(t.task) == norm:
                return t
        # частичное вхождение в обе стороны
        for t in open_tasks:
            nt = normalize_task(t.task)
            if norm and (norm in nt or nt in norm):
                return t
        return None

    # ── полный прогон (§7.1) ──────────────────────────────────────────────────
    async def run_batch(self) -> None:
        chats = await repo.get_dirty_chats()
        if not chats:
            log.debug("Грязных чатов нет — прогон пропущен")
            return
        log.info("Прогон: %d грязных чатов", len(chats))
        for chat_id in chats:
            try:
                await self.process_chat(chat_id)
            except Exception:  # noqa: BLE001
                log.exception("Ошибка обработки чата %s", chat_id)
