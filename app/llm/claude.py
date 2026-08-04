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
                    "route": {
                        "type": ["string", "null"],
                        "description": "When a ROUTING section lists this chat's "
                        "destinations, the label of the destination this task "
                        "belongs to (exactly as listed), else null.",
                    },
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
                    "route",
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
    routes: list[dict[str, Any]] | None = None,
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
    routes_block = ""
    if routes:
        lines = "\n".join(
            f"- \"{r['label']}\"" + (f" — {r['hint']}" if r.get("hint") else "")
            for r in routes
            if r.get("label")
        )
        routes_block = (
            "\n# ROUTING\n"
            "This chat files tasks into SEVERAL destinations. For EACH new task "
            "decide which destination it belongs to by topic, and set its `route` "
            "field to that label EXACTLY as written below. If none clearly fits, "
            "set route to null (the task then goes to the chat's default).\n"
            f"{lines}\n"
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
        f"{routes_block}"
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
    routes: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """`model` is a per-chat/global alias (opus/sonnet/haiku); empty → env default.
    `effort` (low/medium/high/max) applies ONLY on the Anthropic API path — the CLI
    shim does not forward it. `system_prompt` overrides the guidance body.
    `routes` — this chat's multi-project destinations [{label, hint}]; when given,
    the model classifies each new task into one via the `route` output field."""
    s = get_settings()
    system = _build_system(chat_context, extract_rules, importance, people, base_prompt=system_prompt)
    user = _build_user_prompt(window_text, summary, open_tasks, retrieved, routes=routes)

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


async def complete(
    prompt: str,
    system: str = "",
    model: str | None = None,
    max_tokens: int = 4000,
) -> str | None:
    """General one-shot completion via the SAME tier-2 path as extract(): the CLI
    shim (subscription) when claude_cli_url is set, else the Anthropic API.

    Returns the model's raw text, or None on any error (callers decide how to
    degrade — never raises). Thinking disabled; no structured-output enforcement,
    so the caller parses. `model` is an alias (opus/sonnet/haiku) or empty for the
    env default. Used by one-off curation/analysis flows that need a plain
    completion, not the task-extraction schema."""
    s = get_settings()
    try:
        if s.claude_cli_url:
            payload = {"system": system, "prompt": prompt,
                       "model": (model or s.claude_cli_model)}
            headers = {"Authorization": f"Bearer {s.claude_cli_token}"}
            async with httpx.AsyncClient(timeout=s.claude_cli_timeout) as client:
                r = await client.post(s.claude_cli_url, json=payload, headers=headers)
            r.raise_for_status()
            data = r.json()
            if not data.get("ok"):
                logger.warning("complete() shim error: %r", data.get("error"))
                return None
            return data.get("result") or ""
        resp = await _get_client().messages.create(
            model=resolve_api_model(model),
            max_tokens=max_tokens,
            thinking={"type": "disabled"},
            system=system or "",
            messages=[{"role": "user", "content": prompt}],
        )
        return next((b.text for b in resp.content if b.type == "text"), "")
    except Exception:  # noqa: BLE001 — callers treat None as "unavailable"
        logger.warning("complete() failed", exc_info=True)
        return None


# ── Gray-zone dedup judge ──────────────────────────────────────────────────
# A cheap yes/no: are two task titles the SAME actionable task, just worded
# differently? Used only for the semantic-dedup gray band. Deliberately tiny
# (one call, a cheap model, no thinking) and FAIL-SAFE: any error returns None,
# and the caller treats None as "distinct" so a real task is never dropped.

_JUDGE_SYSTEM = (
    "You compare two to-do task CARDS for a task manager. Each card may carry a "
    "title plus extra fields (details/notes, due date, tags, project). Decide "
    "whether they are the SAME actionable task — the same concrete thing to "
    "actually do — merely worded differently or with extra detail. "
    "Weigh ALL fields, not just the title: a difference in ANY distinguishing "
    "detail means NOT the same — a different URL/link, a different number or "
    "amount, a different date/period, a different object, place, or person. "
    "(E.g. two 'watch this reel' cards with different links are DIFFERENT; "
    "'декларация за 2025' vs 'за 2026' are DIFFERENT.)\n"
    "CRUCIAL — shared entity is NOT sameness. Tasks about the same order, invoice, "
    "person, company, or project but describing a DIFFERENT action or a different "
    "STAGE of a process are DIFFERENT tasks: approve ≠ pay ≠ track ≠ confirm "
    "receipt; buy ≠ stock ≠ set up; two payments of different amounts are two "
    "tasks. Only say the same when it is literally the same action on the same "
    "object with no distinguishing detail — merely reworded. Conversely, the same "
    "underlying action counts as the SAME even if titles are phrased differently. "
    "Answer with a single word: yes or no."
)

# CLI-shim aliases map to concrete API model ids on the direct API path.
_JUDGE_API_MODELS = {
    "sonnet": "claude-sonnet-5",
    "haiku": "claude-haiku-4-5",
}


def _parse_yes_no(text: str) -> bool | None:
    t = (text or "").strip().lower()
    if t.startswith("yes"):
        return True
    if t.startswith("no"):
        return False
    return None


async def judge_same_task(a: str, b: str) -> bool | None:
    """True if `a` and `b` are the same actionable task, False if distinct, or
    None when the judge is unavailable/errors. Callers MUST treat None as
    distinct (create) — never merge on doubt. One tiny call; no API fallback on
    the shim path (mirrors extract()).

    `a`/`b` may be plain titles OR full multi-line task cards (title + details +
    due + tags + project). Passing full cards is far more precise — the judge
    weighs every field, so it won't merge two look-alike titles that differ only
    in a URL/number/date buried in the details."""
    a, b = (a or "").strip(), (b or "").strip()
    if not a or not b:
        return None
    s = get_settings()
    user = f"Task A:\n{a}\n\nTask B:\n{b}\n\nSame actionable task? Answer yes or no."
    try:
        if s.claude_cli_url:
            payload = {
                "system": _JUDGE_SYSTEM,
                "prompt": user,
                "model": s.dedup_judge_model,
            }
            headers = {"Authorization": f"Bearer {s.claude_cli_token}"}
            async with httpx.AsyncClient(timeout=min(s.claude_cli_timeout, 60)) as client:
                r = await client.post(s.claude_cli_url, json=payload, headers=headers)
            r.raise_for_status()
            data = r.json()
            if not data.get("ok"):
                return None
            return _parse_yes_no(data.get("result") or "")

        model = _JUDGE_API_MODELS.get(s.dedup_judge_model)
        if model is None:
            # "opus" → the configured extraction model; anything else → literal id.
            model = s.anthropic_model if s.dedup_judge_model == "opus" else s.dedup_judge_model
        resp = await _get_client().messages.create(
            model=model,
            max_tokens=5,
            thinking={"type": "disabled"},
            system=_JUDGE_SYSTEM,
            messages=[{"role": "user", "content": user}],
        )
        text = next((blk.text for blk in resp.content if blk.type == "text"), "")
        return _parse_yes_no(text)
    except Exception:  # noqa: BLE001 — fail safe: unknown → caller treats as distinct
        logger.warning("judge_same_task failed", exc_info=True)
        return None


# ── Final reality check (within-window) ─────────────────────────────────────
# `status_updates` above already tracks a task across WINDOWS — it matches
# against the memory's open-task list, built from PRIOR runs. It structurally
# cannot catch a task proposed and resolved/cancelled WITHIN the same window
# (e.g. "надо позвонить в DMV" ... two messages later ... "уже позвонил") —
# that task was never in the open-task list to begin with, so there is
# nothing for status_updates to match against. Nor does anything today catch
# two new_tasks in the SAME window that are actually one task at different
# stages of refinement (a vague early mention, then specifics a few messages
# later). This stage covers exactly those two same-window gaps — one call,
# the window text + every fresh candidate together, on the strongest model.
# FAIL-SAFE in both directions: on any doubt or error, nothing is dropped and
# nothing is merged — a stray duplicate is far better than a lost task.

VERIFY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "verdicts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "index": {"type": "integer"},
                    "still_open": {"type": "boolean"},
                    "merges_into": {
                        "type": ["integer", "null"],
                        "description": "Set ONLY when this candidate is the SAME "
                        "task as another candidate IN THIS WINDOW, caught at an "
                        "earlier/less specific stage — the 0-based index of the "
                        "OTHER candidate with the fuller, more final version. "
                        "That other candidate is kept; this one is dropped (any "
                        "detail only present here is preserved separately). "
                        "Never point two candidates at each other or at itself.",
                    },
                    "reason": {"type": ["string", "null"]},
                },
                "required": ["index", "still_open", "merges_into", "reason"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["verdicts"],
    "additionalProperties": False,
}

_VERIFY_SYSTEM = (
    "You review a list of candidate tasks freshly extracted from a chat "
    "conversation window, against the WINDOW TEXT itself — a final sanity "
    "check before any of them become real tasks. Two independent checks, "
    "for EACH candidate:\n\n"
    "1) RESOLVED WITHIN THIS WINDOW — still_open=false ONLY when the window "
    "text ITSELF, later on, clearly and explicitly shows the matter was "
    "resolved, cancelled, or is no longer needed (e.g. someone proposes "
    "calling a place, and a few messages later in the SAME window says they "
    "already called it, or that it turned out to be unnecessary). Do NOT use "
    "this for tasks resolved in EARLIER windows/history — that is tracked "
    "separately; you only see this one window. Never guess or infer a "
    "resolution that isn't actually stated. When unsure, or the window is "
    "simply silent on what happened next, default to still_open=true.\n\n"
    "2) SAME TASK, DIFFERENT STAGE — within this SAME window, the same real "
    "task can appear twice worded differently: an early vague mention, and a "
    "later, more specific refinement of the exact same ask (a name, amount, "
    "or detail added once the conversation got concrete). When you find such "
    "a pair, set merges_into on the LESS complete/earlier one to the index "
    "of the MORE complete/later one — only for genuinely the SAME underlying "
    "task, never for two related-but-distinct asks (e.g. 'buy X' and "
    "'return X' are different actions, not stages of one task). When unsure "
    "whether two candidates are really the same task, leave merges_into null "
    "on both — an extra task is far better than silently losing a distinct "
    "one.\n\n"
    "Return exactly one verdict per candidate, using its given 0-based index."
)

_VERIFY_CLI_INSTRUCTION = (
    "\n\n# OUTPUT FORMAT (STRICT)\n"
    "Return ONLY a single JSON object that validates against this JSON Schema. "
    "No prose, no explanation, no markdown, no code fences — just the raw JSON object.\n"
    "JSON Schema:\n" + json.dumps(VERIFY_SCHEMA, ensure_ascii=False)
)


def _verify_candidate_line(index: int, c: dict[str, Any]) -> str:
    line = f"{index}. {c.get('task', '')}"
    if c.get("details"):
        line += f" — {c['details']}"
    if c.get("deadline"):
        line += f" (due {c['deadline']})"
    return line


class VerifyResult:
    """Per-candidate verdicts from verify_still_open, SAME ORDER as input.
    `keep[i]` False → drop (resolved/cancelled within this window).
    `merge_into[i]` set → this candidate is the SAME task as candidate
    `merge_into[i]`, caught at an earlier/less complete stage this window;
    drop it too, after folding any detail unique to it into the survivor."""

    def __init__(self, n: int):
        self.keep: list[bool] = [True] * n
        self.merge_into: list[int | None] = [None] * n


async def verify_still_open(
    window_text: str, candidates: list[dict[str, Any]]
) -> VerifyResult:
    """`candidates` need at least a `task` key (`details`/`deadline` included
    in the prompt when present). Never raises — any failure keeps everything
    independent and unmerged, since this stage only trims/merges, never
    rescues."""
    result = VerifyResult(len(candidates))
    if not candidates:
        return result
    s = get_settings()
    user = (
        "# WINDOW\n" + window_text + "\n\n"
        "# CANDIDATE TASKS (freshly extracted from this window)\n"
        + "\n".join(_verify_candidate_line(i, c) for i, c in enumerate(candidates))
        + "\n"
    )
    try:
        if s.claude_cli_url:
            payload = {
                "system": _VERIFY_SYSTEM,
                "prompt": user + _VERIFY_CLI_INSTRUCTION,
                "model": (s.dedup_judge_model or s.claude_cli_model),
            }
            headers = {"Authorization": f"Bearer {s.claude_cli_token}"}
            async with httpx.AsyncClient(timeout=s.claude_cli_timeout) as client:
                r = await client.post(s.claude_cli_url, json=payload, headers=headers)
            r.raise_for_status()
            data = r.json()
            if not data.get("ok"):
                raise RuntimeError(f"claude-cli shim error: {data.get('error')!r}")
            raw = _parse_json_loose(data.get("result") or "")
        else:
            resp = await _get_client().messages.create(
                model=resolve_api_model(s.anthropic_model),
                max_tokens=3000,
                thinking={"type": "adaptive"},
                output_config={
                    "effort": "medium",
                    "format": {"type": "json_schema", "schema": VERIFY_SCHEMA},
                },
                system=[
                    {"type": "text", "text": _VERIFY_SYSTEM,
                     "cache_control": {"type": "ephemeral"}}
                ],
                messages=[{"role": "user", "content": user}],
            )
            text = next((b.text for b in resp.content if b.type == "text"), None)
            if text is None:
                raise ValueError(f"no text block (stop_reason={resp.stop_reason})")
            raw = json.loads(text)

        n = len(candidates)
        for v in raw.get("verdicts", []):
            i = v.get("index")
            if not (isinstance(i, int) and 0 <= i < n):
                continue
            if v.get("still_open") is False:
                result.keep[i] = False
                logger.info(
                    "verify_still_open: dropping %r — %s",
                    candidates[i].get("task"), v.get("reason"),
                )
                continue  # resolved candidates never also merge
            j = v.get("merges_into")
            if isinstance(j, int) and 0 <= j < n and j != i:
                result.merge_into[i] = j
                logger.info(
                    "verify_still_open: merging %r into %r — %s",
                    candidates[i].get("task"), candidates[j].get("task"), v.get("reason"),
                )
    except Exception:  # noqa: BLE001 — fail-safe: keep everything, unmerged
        logger.warning("verify_still_open failed — keeping all candidates", exc_info=True)
        return VerifyResult(len(candidates))
    return result


# ── Triage batch scoring (ports omi-task-extractor's app/llm/claude.py::
# score_triage_batch — see app/pipeline/triage.py for the caller/flow) ─────
# app/pipeline/triage.py runs one of these per tick over a whole ingest
# window's worth of `signals` (Telegram chat/day activity buckets) —
# deliberately batched, not one call per signal, since triage runs every
# TRIAGE_INTERVAL_MINUTES and per-signal calls would multiply cost by the
# tick's signal count for no benefit.

TRIAGE_SYSTEM = (
    "You triage a batch of SIGNALS — each one is a chunk of a Telegram chat's "
    "activity (a day's worth of messages with one counterparty or group) — for "
    "a personal task-management pipeline. For EACH signal, decide how much "
    "attention it deserves before anything gets turned into an actual task. "
    "Content may be in Russian or English.\n\n"
    "RUBRIC — score HIGHER (closer to 1.0) when: the OWNER made a commitment "
    "(said they will do something), someone made a commitment TO the owner, "
    "a counterparty is clearly waiting on a reply or action from the owner, "
    "or the signal names a concrete date, amount of money, or a person's "
    "name that will need follow-up.\n"
    "RUBRIC — score LOWER (closer to 0.0) when: it is small talk / social "
    "chit-chat with no ask, a recap or discussion of news/events with no "
    "action attached, or the same matter already tracked as an existing "
    "task (a plain re-mention with nothing new).\n\n"
    "category — pick exactly one per signal: \"commitment\" (someone "
    "promised to do something), \"request\" (someone is asking for or "
    "expecting an action, possibly from the owner), \"deadline\" (a concrete "
    "date/time-bound obligation), \"info\" (useful context, no action "
    "needed), \"noise\" (small talk, no real signal).\n\n"
    "reason — one short sentence explaining the score, in the same language "
    "as the signal's own content when reasonable.\n\n"
    "Score every signal independently. Return exactly one verdict per "
    "signal, using its given 0-based index."
)

TRIAGE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "verdicts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "index": {"type": "integer"},
                    "score": {
                        "type": "number",
                        "description": "0.0 (pure noise) to 1.0 (clearly needs "
                        "a human decision), per the RUBRIC.",
                    },
                    "category": {
                        "type": "string",
                        "enum": ["commitment", "request", "deadline", "info", "noise"],
                    },
                    "reason": {"type": "string"},
                },
                "required": ["index", "score", "category", "reason"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["verdicts"],
    "additionalProperties": False,
}

_TRIAGE_MAX_TOKENS = 4000

_TRIAGE_CLI_INSTRUCTION = (
    "\n\n# OUTPUT FORMAT (STRICT)\n"
    "Return ONLY a single JSON object that validates against this JSON Schema. "
    "No prose, no explanation, no markdown, no code fences — just the raw JSON object.\n"
    "JSON Schema:\n" + json.dumps(TRIAGE_SCHEMA, ensure_ascii=False)
)


def _triage_signal_line(index: int, item: dict[str, Any]) -> str:
    parts = [f"{index}. [{item.get('source') or '?'}] {item.get('title') or '(no title)'}"]
    if item.get("ts_start"):
        parts.append(f"   when: {item['ts_start']}")
    participants = item.get("participants_raw") or []
    if participants:
        parts.append(f"   participants: {', '.join(participants)}")
    if item.get("summary"):
        parts.append(f"   summary: {item['summary']}")
    return "\n".join(parts)


async def score_triage_batch(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One Claude call scoring a WHOLE batch of signals at once.

    `items`: [{title, summary, participants_raw, source, ts_start}],
    index-aligned with the caller's signal list (app/pipeline/triage.py
    builds `items` from `signals` docs and matches results back by index).

    Returns the raw list of verdicts the model produced —
    [{index, score, category, reason}, ...] — NOT guaranteed to have one
    entry per input item (a model that skips an index is the caller's
    problem to handle, same convention as verify_still_open's index
    matching). Returns [] on ANY failure (transport, parse, shape) —
    fail-open, same as judge_same_task/verify_still_open: the caller must
    treat a missing index as "not scored this tick", never invent a score.
    """
    if not items:
        return []
    s = get_settings()
    user = (
        "# SIGNALS\n"
        + "\n".join(_triage_signal_line(i, it) for i, it in enumerate(items))
        + "\n"
    )
    try:
        if s.claude_cli_url:
            payload = {
                "system": TRIAGE_SYSTEM,
                "prompt": user + _TRIAGE_CLI_INSTRUCTION,
                "model": (s.triage_model or s.claude_cli_model),
            }
            headers = {"Authorization": f"Bearer {s.claude_cli_token}"}
            async with httpx.AsyncClient(timeout=s.claude_cli_timeout) as client:
                r = await client.post(s.claude_cli_url, json=payload, headers=headers)
            r.raise_for_status()
            data = r.json()
            if not data.get("ok"):
                raise RuntimeError(f"claude-cli shim error: {data.get('error')!r}")
            raw = _parse_json_loose(data.get("result") or "")
        else:
            resp = await _get_client().messages.create(
                model=resolve_api_model(s.triage_model),
                max_tokens=_TRIAGE_MAX_TOKENS,
                thinking={"type": "adaptive"},
                output_config={
                    "effort": "low",
                    "format": {"type": "json_schema", "schema": TRIAGE_SCHEMA},
                },
                system=[
                    {"type": "text", "text": TRIAGE_SYSTEM, "cache_control": {"type": "ephemeral"}}
                ],
                messages=[{"role": "user", "content": user}],
            )
            text = next((b.text for b in resp.content if b.type == "text"), None)
            if text is None:
                raise ValueError(f"no text block (stop_reason={resp.stop_reason})")
            raw = json.loads(text)
        verdicts = raw.get("verdicts")
        return verdicts if isinstance(verdicts, list) else []
    except Exception:  # noqa: BLE001 — fail-open: [] means "nothing scored this tick"
        logger.warning("score_triage_batch failed", exc_info=True)
        return []


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
