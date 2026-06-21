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
SYSTEM_PROMPT = (
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
    "gone). Use it for context so a topic revisited weeks later is understood.\n\n"
    "Do INCREMENTAL work:\n"
    "  - new_tasks: only tasks NOT already in the open-task list. For each: who is "
    "responsible (\"me\" = owner, \"counterparty\"), the counterparty's name if known, "
    "an ISO date deadline (YYYY-MM-DD) or null, a suggested project name or null, and "
    "the source message ids it came from. Do not invent deadlines.\n"
    "  - status_updates: for existing open tasks that later messages show as completed "
    "or cancelled, reference the task by its text and give new_status done|cancelled.\n"
    "  - updated_summary: a compact running summary of the whole chat (what it's about, "
    "agreements, open questions, who owes whom, key facts/preferences), updated to "
    "reflect this window. This is the durable memory — write it to survive even after "
    "the raw messages expire.\n\n"
    "Be conservative: extract real commitments, not hypotheticals or small talk. "
    "Return only the structured JSON."
)

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
                    "suggested_project": {"type": ["string", "null"]},
                    "source_message_ids": {"type": "array", "items": {"type": "integer"}},
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

_client: AsyncAnthropic | None = None


def _get_client() -> AsyncAnthropic:
    global _client
    if _client is None:
        _client = AsyncAnthropic(api_key=get_settings().anthropic_api_key)
    return _client


def _build_user_prompt(window_text: str, summary: str, open_tasks: list[dict[str, Any]]) -> str:
    open_lines = (
        "\n".join(
            f"- [{t.get('who', '?')}] {t['task']}"
            + (f" (deadline {t['deadline']})" if t.get("deadline") else "")
            for t in open_tasks
        )
        or "(none)"
    )
    return (
        "# CONVERSATION WINDOW\n"
        f"{window_text}\n\n"
        "# LONG-TERM MEMORY\n"
        "## Prior summary\n"
        f"{summary or '(none yet)'}\n\n"
        "## Currently open tasks for this chat\n"
        f"{open_lines}\n"
    )


async def extract(window_text: str, summary: str, open_tasks: list[dict[str, Any]]) -> dict[str, Any]:
    s = get_settings()
    resp = await _get_client().messages.create(
        model=s.anthropic_model,
        max_tokens=8000,
        thinking={"type": "adaptive"},
        output_config={
            "effort": s.anthropic_effort,
            "format": {"type": "json_schema", "schema": OUTPUT_SCHEMA},
        },
        system=[{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": _build_user_prompt(window_text, summary, open_tasks)}],
    )
    # With structured output the model emits a JSON text block (after any thinking
    # blocks). Grab the first text block.
    text = next((b.text for b in resp.content if b.type == "text"), None)
    if text is None:
        raise ValueError(f"Claude returned no text block (stop_reason={resp.stop_reason})")
    return json.loads(text)
