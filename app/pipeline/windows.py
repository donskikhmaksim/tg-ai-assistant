"""Сборка ОКНА РАЗГОВОРА из сырья (§7 ТЗ).

Окно отвечает только за «какие свежие сырые сообщения смотреть сейчас». Идём
назад от последнего сообщения, пока пауза между соседними не превысит
CONV_GAP_HOURS (или пока не упрёмся в начало выборки MAX_LOOKBACK_HOURS, который
уже применён при чтении из БД). Долговременная память — отдельный механизм.
"""
from __future__ import annotations

from datetime import timedelta

from app.models import WindowMessage


def build_window(docs: list[dict], gap_hours: int) -> list[WindowMessage]:
    """Из отсортированных по дате сырых документов вернуть «живое» окно.

    `docs` уже ограничены окном MAX_LOOKBACK при чтении из БД и отсортированы
    по дате по возрастанию.
    """
    if not docs:
        return []

    gap = timedelta(hours=gap_hours)
    # идём назад от последнего; включаем, пока пауза между соседями <= gap
    included: list[dict] = [docs[-1]]
    for prev, cur in zip(reversed(docs[:-1]), reversed(docs[1:])):
        # cur уже включён; проверяем разрыв до prev
        if cur["date"] - prev["date"] > gap:
            break
        included.append(prev)

    included.reverse()
    return [
        WindowMessage(
            direction=d.get("direction", "in"),
            sender_name=d.get("senderName") or "?",
            text=d.get("text") or "",
            message_id=d.get("messageId", 0),
            date=d["date"],
        )
        for d in included
    ]


def render_window(window: list[WindowMessage]) -> str:
    """Текстовая разметка окна для LLM: кто (in/out), когда, id и текст."""
    lines = []
    for m in window:
        stamp = m.date.strftime("%d.%m %H:%M")
        lines.append(
            f"[#{m.message_id} {stamp} | {m.direction}] {m.sender_name}: {m.text}"
        )
    return "\n".join(lines)
