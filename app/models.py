"""Доменные модели: документы Mongo и структурный вывод LLM (§5, §7 ТЗ)."""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

Direction = Literal["in", "out"]
ChatType = Literal["dm", "group"]
Who = Literal["me", "counterparty"]
TaskStatus = Literal["open", "done", "cancelled"]
NewStatus = Literal["done", "cancelled"]


# ── Структурный вывод Tier 2 (Claude) ────────────────────────────────────────
class NewTask(BaseModel):
    task: str
    who: Who
    counterpartyName: str | None = None
    deadline: str | None = None  # YYYY-MM-DD | null
    suggested_project: str | None = None
    source_message_ids: list[int] = Field(default_factory=list)


class StatusUpdate(BaseModel):
    task_match: str
    new_status: NewStatus


class ExtractionResult(BaseModel):
    new_tasks: list[NewTask] = Field(default_factory=list)
    status_updates: list[StatusUpdate] = Field(default_factory=list)
    updated_summary: str = ""


# ── JSON-схемы для output_config.format ───────────────────────────────────────
TRIAGE_SCHEMA: dict = {
    "type": "object",
    "properties": {"has_task": {"type": "boolean"}},
    "required": ["has_task"],
    "additionalProperties": False,
}

EXTRACTION_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "new_tasks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "task": {"type": "string"},
                    "who": {"type": "string", "enum": ["me", "counterparty"]},
                    "counterpartyName": {"type": ["string", "null"]},
                    "deadline": {"type": ["string", "null"]},
                    "suggested_project": {"type": ["string", "null"]},
                    "source_message_ids": {
                        "type": "array",
                        "items": {"type": "integer"},
                    },
                },
                "required": [
                    "task",
                    "who",
                    "counterpartyName",
                    "deadline",
                    "suggested_project",
                    "source_message_ids",
                ],
                "additionalProperties": False,
            },
        },
        "status_updates": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "task_match": {"type": "string"},
                    "new_status": {"type": "string", "enum": ["done", "cancelled"]},
                },
                "required": ["task_match", "new_status"],
                "additionalProperties": False,
            },
        },
        "updated_summary": {"type": "string"},
    },
    "required": ["new_tasks", "status_updates", "updated_summary"],
    "additionalProperties": False,
}


# ── Внутренние DTO пайплайна ───────────────────────────────────────────────────
class WindowMessage(BaseModel):
    """Одно сырое сообщение в окне разговора."""

    direction: Direction
    sender_name: str
    text: str
    message_id: int
    date: datetime


class OpenTask(BaseModel):
    """Открытая задача чата, передаётся Claude как контекст памяти."""

    task: str
    who: Who
    deadline: str | None = None
    dedup_hash: str
    ticktick_task_id: str | None = None
    project_id: str | None = None
