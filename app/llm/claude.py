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

import httpx
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
    "    who: decide by WHOSE action it is, NOT by who spoke. \"me\" when the OWNER must "
    "do it (the counterparty asks/expects the owner to act). \"counterparty\" when the "
    "OTHER person must do it — BOTH when the owner delegated it to them AND when they "
    "volunteered/committed themselves (\"ладно, я сделаю\", \"я скину\", \"я перезвоню\"). "
    "In a 1-1 DM these counterparty commitments are the owner TRACKING someone else's "
    "promise, not the owner's own to-do — still emit them as tasks with who=\"counterparty\" "
    "so they can be flagged; the backend marks them «Контроль».\n"
    "    Emit each distinct task ONCE with a single `who` — never output the same "
    "task twice (e.g. once as \"me\" and once as \"counterparty\"); pick the one "
    "correct attribution.\n"
    "    who LITMUS — after the message, in WHOSE hands does the action land? A "
    "directive aimed at the OWNER, even softly phrased (\"ты можешь запросить…\", "
    "\"тебе надо…\", \"забери…\", \"позвони…\", \"запроси callback…\") → who=\"me\"; do "
    "NOT flip to \"counterparty\" just because the other person raised it. A wish or "
    "intention the COUNTERPARTY voiced about THEIR OWN action (\"я хочу купить…\", "
    "\"я сделаю\", \"я скину\", \"мне надо…\") → who=\"counterparty\"; do NOT default such "
    "self-intentions to \"me\".\n"
    "    deadline: a bare date `YYYY-MM-DD`, OR `YYYY-MM-DDThh:mm` (24h) when a specific "
    "clock time was actually agreed. Use the time as spoken (wall-clock); do not convert "
    "it. Never invent a time — if only a day was agreed, emit just the date.\n"
    "    Every timestamp in the window is the owner's LOCAL time. Resolve relative "
    "dates (\"сегодня\", \"завтра\", \"в пятницу\", \"на выходных\", \"через неделю\") "
    "against the timestamp of the message they appear in, in that local zone — "
    "never UTC.\n"
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
    "  - rejected: borderline candidates you considered but decided are NOT real tasks "
    "(noise, small talk, hypotheticals, duplicates of obvious things). Give the candidate "
    "text and a one-line reason. Keep this SHORT — only genuinely ambiguous near-misses, "
    "not every message. This lets the user optionally review what was filtered out.\n"
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

# Short model aliases (as chosen per-chat/global in the Mini App) → concrete
# Anthropic API model ids for the direct-API path. The CLI shim takes the alias
# verbatim (`claude -p --model <alias>`), so this mapping is only used when
# running through the paid Anthropic API.
_API_MODEL_BY_ALIAS = {
    "opus": "claude-opus-4-8",
    "sonnet": "claude-sonnet-5",
    "haiku": "claude-haiku-4-5",
}


def resolve_api_model(alias: str | None) -> str:
    """Map a short model alias (opus/sonnet/haiku) to a concrete Anthropic API
    model id. Empty/unknown → the configured ANTHROPIC_MODEL default."""
    if alias:
        mapped = _API_MODEL_BY_ALIAS.get(alias.strip().lower())
        if mapped:
            return mapped
    return get_settings().anthropic_model

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
        "rejected": {
            "type": "array",
            "description": "Items that looked like a task but you decided are NOT "
            "real tasks (noise, chit-chat, already-obvious, false positives). "
            "Each has the candidate text and a short reason.",
            "items": {
                "type": "object",
                "properties": {
                    "task": {"type": "string"},
                    "reason": {"type": "string"},
                    "source_message_ids": {"type": "array", "items": {"type": "integer"}},
                },
                "required": ["task", "reason", "source_message_ids"],
                "additionalProperties": False,
            },
        },
        "updated_summary": {"type": "string"},
    },
    "required": ["new_tasks", "status_updates", "rejected", "updated_summary"],
    "additionalProperties": False,
}

# A safe, empty-but-valid result. Returned when a parsed extraction fails the
# shape check (see _is_valid_result) so a broken CUSTOM prompt degrades to "no
# tasks this run" instead of creating garbage tasks. An empty updated_summary is
# ignored by the pipeline (it only persists a truthy summary), so memory is kept.
_EMPTY_RESULT: dict[str, Any] = {
    "new_tasks": [],
    "status_updates": [],
    "rejected": [],
    "updated_summary": "",
}


def _is_valid_result(result: Any) -> bool:
    """Light shape guard for the parsed extraction result. The Anthropic API path
    already enforces OUTPUT_SCHEMA via output_config.format, but the CLI shim does
    NOT — so an editable/broken system_prompt could make the model emit the wrong
    shape. Validate the load-bearing top-level contract (the task-producing lists)
    so bad output is dropped rather than turned into tasks."""
    if not isinstance(result, dict):
        return False
    new_tasks = result.get("new_tasks")
    status_updates = result.get("status_updates")
    if not isinstance(new_tasks, list) or not isinstance(status_updates, list):
        return False
    for t in new_tasks:
        if not isinstance(t, dict) or not isinstance(t.get("task"), str):
            return False
    return True


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


def _build_system(
    chat_context: str = "",
    extract_rules: str | None = None,
    importance: str | None = None,
    people: str | None = None,
    base_prompt: str | None = None,
) -> str:
    """Compose the final system prompt: base + optional context + optional rules.

    `base_prompt` is the user's editable override of the built-in guidance body
    (SYSTEM_PROMPT); empty/None → the default. This ONLY swaps the guidance text —
    the JSON-output/schema contract is appended separately (output_config.format
    on the API path, _CLI_OUTPUT_INSTRUCTION on the shim), so a bad override can't
    remove the format contract."""
    parts = [base_prompt.strip() if (base_prompt and base_prompt.strip()) else SYSTEM_PROMPT]
    if chat_context:
        parts.append(chat_context)
    if extract_rules:
        parts.append(f"Дополнительные правила извлечения для этого чата:\n{extract_rules}")
    if importance:
        parts.append(f"Критерий важности задачи:\n{importance}")
    if people:
        parts.append(f"Справочник участников этого чата:\n{people}")
    return "\n\n".join(parts)


async def extract(
    window_text: str,
    summary: str,
    open_tasks: list[dict[str, Any]],
    retrieved: list[str] | None = None,
    chat_context: str = "",
    extract_rules: str | None = None,
    importance: str | None = None,
    people: str | None = None,
    model: str | None = None,
    effort: str | None = None,
    system_prompt: str | None = None,
) -> dict[str, Any]:
    """`model` is a per-chat/global alias (opus/sonnet/haiku); empty → env default.
    `effort` (low/medium/high/max) applies ONLY on the Anthropic API path — the CLI
    shim does not forward it. `system_prompt` overrides the guidance body."""
    s = get_settings()
    system = _build_system(chat_context, extract_rules, importance, people, base_prompt=system_prompt)
    user = _build_user_prompt(window_text, summary, open_tasks, retrieved)

    # Subscription path: run through the CLI shim (claude -p on a Mac mini). No
    # API fallback — on any failure we raise so the chat stays dirty and retries.
    if s.claude_cli_url:
        result = await _extract_via_cli(s, system, user, model)
    else:
        resp = await _get_client().messages.create(
            model=resolve_api_model(model),
            max_tokens=8000,
            thinking={"type": "adaptive"},
            output_config={
                "effort": (effort or s.anthropic_effort),
                "format": {"type": "json_schema", "schema": OUTPUT_SCHEMA},
            },
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": user}],
        )
        # With structured output the model emits a JSON text block (after any
        # thinking blocks). Grab the first text block.
        text = next((b.text for b in resp.content if b.type == "text"), None)
        if text is None:
            raise ValueError(f"Claude returned no text block (stop_reason={resp.stop_reason})")
        result = json.loads(text)

    # Shape guard: a broken CUSTOM system_prompt (esp. on the shim, which has no
    # schema enforcement) could yield the wrong shape. Degrade to "no tasks this
    # run" instead of creating garbage.
    if not _is_valid_result(result):
        logger.warning("Extraction result failed shape validation; treating as no-tasks")
        return dict(_EMPTY_RESULT)
    return result


# Schema description appended to the CLI prompt — the shim has no json_schema
# enforcement, so we instruct the model to emit exactly this shape.
_CLI_OUTPUT_INSTRUCTION = (
    "\n\n# OUTPUT FORMAT (STRICT)\n"
    "Return ONLY a single JSON object that validates against this JSON Schema. "
    "No prose, no explanation, no markdown, no code fences — just the raw JSON object.\n"
    "JSON Schema:\n" + json.dumps(OUTPUT_SCHEMA, ensure_ascii=False)
)


def _parse_json_loose(text: str) -> dict[str, Any]:
    """Parse a JSON object from model text that may be fenced or padded."""
    t = text.strip()
    if t.startswith("```"):
        # ```json\n...\n``` or ```\n...\n```
        t = t[3:]
        if t[:4].lower() == "json":
            t = t[4:]
        t = t.split("```", 1)[0]
        t = t.strip()
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        i, j = t.find("{"), t.rfind("}")
        if i >= 0 and j > i:
            return json.loads(t[i : j + 1])
        raise


async def _extract_via_cli(
    s: Any, system: str, user: str, model: str | None = None
) -> dict[str, Any]:
    # `model` is the per-chat/global alias (opus/sonnet/haiku), forwarded verbatim
    # as the shim's `--model` alias (claude_cli_model). Empty → CLAUDE_CLI_MODEL.
    # NOTE: effort is intentionally NOT sent — the shim (`claude -p`) does not
    # forward effort today, so per-chat effort only takes effect on the API path.
    payload = {
        "system": system,
        "prompt": user + _CLI_OUTPUT_INSTRUCTION,
        "model": (model or s.claude_cli_model),
    }
    headers = {"Authorization": f"Bearer {s.claude_cli_token}"}
    async with httpx.AsyncClient(timeout=s.claude_cli_timeout) as client:
        r = await client.post(s.claude_cli_url, json=payload, headers=headers)
    r.raise_for_status()
    data = r.json()
    if not data.get("ok"):
        raise RuntimeError(f"claude-cli shim error: {data.get('error')!r}")
    return _parse_json_loose(data.get("result") or "")


async def healthcheck() -> tuple[bool, str]:
    """Canary for the daily watchdog that exercises the SAME tier-2 path as
    extract(): the CLI shim when claude_cli_url is set (catches a dead shim, e.g.
    `claude` logged out -> 500), otherwise the Anthropic API. Returns (ok, detail);
    never raises, never falls back to the other path."""
    s = get_settings()
    if s.claude_cli_url:
        try:
            payload = {"prompt": "Reply with the single word: ok", "system": "",
                       "model": s.claude_cli_model}
            headers = {"Authorization": f"Bearer {s.claude_cli_token}"}
            async with httpx.AsyncClient(timeout=min(s.claude_cli_timeout, 60)) as client:
                r = await client.post(s.claude_cli_url, json=payload, headers=headers)
            r.raise_for_status()
            data = r.json()
            if not data.get("ok"):
                return False, f"shim error: {str(data.get('error'))[:200]}"
            return True, ""
        except Exception as e:  # noqa: BLE001
            return False, f"{type(e).__name__}: {str(e)[:200]}"
    try:
        await _get_client().messages.create(
            model=s.anthropic_model,
            max_tokens=8,
            messages=[{"role": "user", "content": "Reply with: ok"}],
        )
        return True, ""
    except Exception as e:  # noqa: BLE001
        return False, f"{type(e).__name__}: {str(e)[:200]}"
