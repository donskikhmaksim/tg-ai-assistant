"""Tier 2 — разбор окна через Claude (claude-opus-4-8), структурный вывод (§7 ТЗ).

Claude получает: (а) окно разговора с разметкой кто/когда, (б) долговременную
память чата (резюме + открытые задачи). Возвращает инкрементальный JSON:
новые задачи, обновления статусов, обновлённое резюме. Claude НЕ ходит в MCP —
только структурный JSON; создание задач делает бэкенд.
"""
from __future__ import annotations

import json
import logging
from datetime import date

from anthropic import AsyncAnthropic

from app.models import EXTRACTION_SCHEMA, ExtractionResult, OpenTask

log = logging.getLogger(__name__)

# Стабильный системный промпт — кэшируется (prompt caching, §7.4).
_SYSTEM = """\
Ты — ассистент, извлекающий задачи и договорённости из личной и групповой \
переписки владельца (RU/EN). Тебе дают ОКНО текущего разговора и ДОЛГОВРЕМЕННУЮ \
ПАМЯТЬ чата (резюме + уже известные открытые задачи).

Твоя работа — инкрементальная. Извлекай ТОЛЬКО то, чего ещё нет среди известных \
открытых задач. Различай, кто исполнитель:
- who="me" — обязательство/обещание/задача САМОГО ВЛАДЕЛЦА (его исходящие, отметка out);
- who="counterparty" — обязательство собеседника/участника группы (in).

Правила:
1. new_tasks: только реальные действия (прислать, сделать, позвонить, оплатить, \
   подготовить, договориться к сроку). Болтовню, факты без действия и уже \
   известные задачи НЕ включай.
2. deadline: формат YYYY-MM-DD, если срок назван явно или однозначно выводится \
   из текущей даты (она дана в запросе). Иначе null.
3. counterpartyName: имя собеседника/участника, если уместно, иначе null.
4. source_message_ids: id сообщений окна, из которых взята задача.
5. status_updates: если по более поздним сообщениям окна известная открытая \
   задача выполнена (done) или отменена (cancelled) — отметь это. task_match — \
   это точный текст соответствующей известной задачи.
6. updated_summary: сжатая обновлённая выжимка чата (о чём речь, договорённости, \
   нерешённые вопросы, кто кому что должен, важные факты/предпочтения). Это \
   переживёт TTL сырья — пиши так, чтобы через недели хватило контекста.

Отвечай строго по заданной JSON-схеме."""


def _format_open_tasks(tasks: list[OpenTask]) -> str:
    if not tasks:
        return "(открытых задач пока нет)"
    lines = []
    for t in tasks:
        dl = f", дедлайн {t.deadline}" if t.deadline else ""
        lines.append(f"- [{t.who}] {t.task}{dl}")
    return "\n".join(lines)


class ClaudeExtractor:
    def __init__(self, api_key: str, model: str = "claude-opus-4-8") -> None:
        self._client = AsyncAnthropic(api_key=api_key)
        self._model = model

    async def extract(
        self, window_text: str, summary: str, open_tasks: list[OpenTask]
    ) -> ExtractionResult:
        today = date.today().isoformat()
        user_content = (
            f"СЕГОДНЯ: {today}\n\n"
            f"=== ДОЛГОВРЕМЕННОЕ РЕЗЮМЕ ЧАТА ===\n"
            f"{summary or '(резюме пока нет)'}\n\n"
            f"=== ИЗВЕСТНЫЕ ОТКРЫТЫЕ ЗАДАЧИ ЧАТА ===\n"
            f"{_format_open_tasks(open_tasks)}\n\n"
            f"=== ОКНО РАЗГОВОРА (свежие сообщения) ===\n"
            f"{window_text}"
        )

        resp = await self._client.messages.create(
            model=self._model,
            max_tokens=4096,
            system=[
                {
                    "type": "text",
                    "text": _SYSTEM,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            output_config={
                "format": {"type": "json_schema", "schema": EXTRACTION_SCHEMA}
            },
            messages=[{"role": "user", "content": user_content}],
        )

        if resp.stop_reason == "refusal":
            log.warning("Claude отказал в разборе окна (refusal)")
            return ExtractionResult()

        text = next((b.text for b in resp.content if b.type == "text"), None)
        if not text:
            log.warning("Claude вернул пустой ответ")
            return ExtractionResult()

        try:
            data = json.loads(text)
            return ExtractionResult.model_validate(data)
        except (json.JSONDecodeError, ValueError):
            log.exception("Не удалось разобрать JSON от Claude")
            return ExtractionResult()
