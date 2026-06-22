"""Нормализация текста задачи и расчёт dedup-хеша (§5, §7.5 ТЗ)."""
from __future__ import annotations

import hashlib
import re

_WS = re.compile(r"\s+")


def normalize_task(task: str) -> str:
    """Привести текст задачи к каноничному виду для дедупа."""
    return _WS.sub(" ", task.strip().lower())


def dedup_hash(chat_id: str, task: str) -> str:
    """sha1(chatId + normalized(task)) — стабильный ключ задачи в рамках чата."""
    payload = f"{chat_id}{normalize_task(task)}".encode("utf-8")
    return hashlib.sha1(payload).hexdigest()
