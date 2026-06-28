"""Tier 2 extraction: Claude (claude-opus-4-8) with structured output.

Given a conversation window plus the chat's long-term memory (summary + open
tasks), Claude returns an *incremental* JSON: new tasks, status updates for
already-known tasks, and a refreshed chat summary.

Design notes:
  - Structured output via `output_config.format` (json_schema) — the modern
    parameter; `output_format` is deprecated.
  - Adaptive thinking with a configurable effort level.
  - Prompt caching on the stable system prompt (the volatile window + memory go
    in the user turn, after the cached prefix).
"""
from __future__ import annotations

import json
import logging
from typing import Any

from anthropic import AsyncAnthropic

from ..config import get_settings

logger = logging.getLogger(__name__)

# Stable system prompt → cached. Keep it byte-identical across requests.
TIER2_DEFAULT_SYSTEM = (
    "You extract tasks, promises, commitments and agreements from a chat "
    "conversation and maintain a running memory of the chat.\n\n"
    "The conversation is between the OWNER (messages marked `out` / who=\"me\") and "
    "a COUNTERPARTY (messages marked `in`). Messages are multilingual (Russian and "
    "English); preserve the original language of task text.\n\n"
    "You are given:\n"
    "  1. A CONVERSATION WINDOW — the recent live exchange, each line tagged with "
    "direction, sender, time and message id.\n"
    "  2. LONG-TERM MEMORY — a prior summary of the chat and the list of currently "
    "OPEN tasks (these may pre-date the window; the raw messages behind them may be "
    "gone). Use it for context so a topic revisited weeks later is understood.\n"
    "  3. (sometimes) RETRIEVED PAST CONTEXT — individual OLDER messages from this "
    "chat, surfaced by semantic similarity to the window. Treat them as real prior "
    "evidence to resolve references and continuity; they may sit outside the window "
    "and the summary.\n\n"
    "Do INCREMENTAL work:\n"
    "  - new_tasks: only tasks NOT already in the open-task list. For each: who is "
    "responsible (\"me\" = owner, \"counterparty\"), the counterparty's name if known, "
    "a deadline or null, a suggested project name or null, the source message ids it "
    "came from, and `details`. Do not invent deadlines.\n"
    "    deadline: a bare date `YYYY-MM-DD`, OR `YYYY-MM-DDThh:mm` (24h) when a specific "
    "clock time was actually agreed. Use the time as spoken (wall-clock); do not convert "
    "it. Never invent a time — if only a day was agreed, emit just the date.\n"
    "    deadline_tz: an IANA timezone name ONLY if the conversation explicitly named a "
    "city or zone for that time (e.g. \"по Москве\" -> \"Europe/Moscow\", \"по Хабаровску\" "
    "-> \"Asia/Khabarovsk\", \"EST\" -> \"America/New_York\"); otherwise null (the backend "
    "assumes the owner's home zone).\n"
    "    from_name / to_name: in a GROUP, name the person who raised/assigned the task "
    "(from_name = the message sender) and the person it is FOR / who must do it "
    "(to_name). Use \"me\" for the owner. In a 1-1 DM, set both to null (it's implicit).\n"
    "    `details`: ONLY a concrete fact that helps do the task and is NOT already in "
    "the title — an amount, number, date, name, link, address, phone, id, or specific "
    "constraint. If you cannot name such a concrete fact, set details to null. NEVER "
    "include vague, hedged or speculative wording (\"возможно\", \"наверное\", \"что ли\", "
    "\"maybe\"), never paraphrase or restate the title, never invent or pad. When the "
    "source is unclear, fix it into a sensible task in the title and leave details null "
    "rather than echoing the confusion.\n"
    "  - status_updates: for existing open tasks that later messages show as completed "
    "or cancelled, reference the task by its text and give new_status done|cancelled.\n"
    "  - updated_summary: a COMPACT, STRUCTURED memory of the whole chat — short labeled "
    "lines, not prose. Cover: people & roles, key facts/preferences, decisions, open "
    "threads/questions, who owes whom. MERGE this window into the prior summary: keep it "
    "concise (aim ~150 words), drop resolved/stale items, and correct facts that changed "
    "— do NOT blindly append (this avoids drift and unbounded growth). This durable "
    "memory must survive after the raw messages expire.\n\n"
    "Be conservative: extract real commitments, not hypotheticals or small talk. "
    "Return only the structured JSON."
)

SYSTEM_PROMPT = TIER2_DEFAULT_SYSTEM

OUTPUT_SCHEMA: dict[str, Any] = {
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
                    "deadline_tz": {"type": ["string", "null"]},
                    "from_name": {"type": ["string", "null"]},
                    "to_name": {"type": ["string", "null"]},
                    "suggested_project": {"type": ["string", "null"]},
                    "source_message_ids": {"type": "array", "items": {"type": "integer"}},
                    "details": {"type": ["string", "null"]},
                },
                "required": [
                    "task",
                    "who",
                    "counterpartyName",
                    "deadline",
                    "deadline_tz",
                    "from_name",
                    "to_name",
                    "suggested_project",
                    "source_message_ids",
                    "details",
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

_client: AsyncAnthropic | None = None


def _get_client() -> AsyncAnthropic:
    global _client
    if _client is None:
        _client = AsyncAnthropic(api_key=get_settings().anthropic_api_key)
    return _client


def _build_user_prompt(
    window_text: str,
    summary: str,
    open_tasks: list[dict[str, Any]],
    retrieved: list[str] | None = None,
) -> str:
    open_lines = (
        "\n".join(
            f"- [{t.get('who', '?')}] {t['task']}"
            + (f" (deadline {t['deadline']})" if t.get("deadline") else "")
            for t in open_tasks
        )
        or "(none)"
    )
    retrieved_block = ""
    if retrieved:
        joined = "\n".join(f"- {r}" for r in retrieved)
        retrieved_block = (
            "\n# RETRIEVED PAST CONTEXT\n"
            "Older messages from this chat, semantically related to the window "
            "(may pre-date it):\n"
            f"{joined}\n"
        )
    return (
        "# CONVERSATION WINDOW\n"
        f"{window_text}\n\n"
        "# LONG-TERM MEMORY\n"
        "## Prior summary\n"
        f"{summary or '(none yet)'}\n\n"
        "## Currently open tasks for this chat\n"
        f"{open_lines}\n"
        f"{retrieved_block}"
    )


def _build_system(chat_context: str = "", extract_rules: str | None = None) -> str:
    """Compose the final system prompt: default + optional context + optional rules."""
    parts = [SYSTEM_PROMPT]
    if chat_context:
        parts.append(chat_context)
    if extract_rules:
        parts.append(f"Дополнительные правила извлечения для этого чата:\n{extract_rules}")
    return "\n\n".join(parts)


async def extract(
    window_text: str,
    summary: str,
    open_tasks: list[dict[str, Any]],
    retrieved: list[str] | None = None,
    chat_context: str = "",
    extract_rules: str | None = None,
) -> dict[str, Any]:
    s = get_settings()
    system = _build_system(chat_context, extract_rules)
    resp = await _get_client().messages.create(
        model=s.anthropic_model,
        max_tokens=8000,
        thinking={"type": "adaptive"},
        output_config={
            "effort": s.anthropic_effort,
            "format": {"type": "json_schema", "schema": OUTPUT_SCHEMA},
        },
        system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
        messages=[
            {"role": "user", "content": _build_user_prompt(window_text, summary, open_tasks, retrieved)}
        ],
    )
    # With structured output the model emits a JSON text block (after any thinking
    # blocks). Grab the first text block.
    text = next((b.text for b in resp.content if b.type == "text"), None)
    if text is None:
        raise ValueError(f"Claude returned no text block (stop_reason={resp.stop_reason})")
    return json.loads(text)
