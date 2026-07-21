"""Tier 1 triage: local Qwen via Ollama's OpenAI-compatible endpoint.

A cheap gate before Claude: "does this conversation window contain any task,
promise, or agreement?" — strict JSON {"has_task": bool}. Multilingual
(RU/EN) by instruction. On any error we fail OPEN (return True) so a flaky
local model never silently drops real tasks; Claude is the real filter.
"""
from __future__ import annotations

import json
import logging

from openai import AsyncOpenAI

from ..config import get_settings

logger = logging.getLogger(__name__)

TIER1_DEFAULT_SYSTEM = (
    "You are a HIGH-RECALL triage filter before an expensive extractor. You read "
    "a fragment of a chat conversation (Russian or English) and decide whether it "
    "MIGHT contain any actionable item — a task, to-do, promise, commitment, "
    "agreement, request, reminder, plan, or deadline — for either participant. "
    "Informal, hedged or vague phrasing (\"наверное надо\", \"нужно бы\", \"не забыть\", "
    "\"что ли\", \"как-нибудь\") STILL counts. Only answer false for content that is "
    "CLEARLY just greetings, reactions, emotions or small talk with no actionable "
    "hint at all. WHEN IN DOUBT, answer true — a stronger model does the real "
    "filtering. Respond with strict JSON only: {\"has_task\": true} or "
    "{\"has_task\": false}. No prose."
)

_SYSTEM = TIER1_DEFAULT_SYSTEM

# One client per distinct base_url. The endpoint is now configurable (Mini App
# global setting → passed in by the pipeline), so we can't cache a single client.
_clients: dict[str, AsyncOpenAI] = {}


def _client_for(base_url: str) -> AsyncOpenAI:
    client = _clients.get(base_url)
    if client is None:
        client = AsyncOpenAI(base_url=base_url, api_key=get_settings().qwen_api_key)
        _clients[base_url] = client
    return client


def _resolve_base_url(base_url: str | None) -> str:
    """Effective tier-1 endpoint: the caller-supplied value (from the Mini App
    global setting) if given, else the env default. Empty → tier-1 disabled."""
    return (base_url if base_url is not None else get_settings().qwen_base_url) or ""


def _build_system(
    chat_context: str = "",
    filter_rules: str | None = None,
    importance: str | None = None,
) -> str:
    """Compose the final system prompt: default + optional context + optional rules."""
    parts = [_SYSTEM]
    if chat_context:
        parts.append(chat_context)
    if filter_rules:
        parts.append(f"Дополнительные правила для этого чата:\n{filter_rules}")
    if importance:
        parts.append(f"Критерий важности задачи:\n{importance}")
    return "\n\n".join(parts)


async def has_task(
    window_text: str,
    chat_context: str = "",
    filter_rules: str | None = None,
    importance: str | None = None,
    base_url: str | None = None,
) -> bool:
    s = get_settings()
    endpoint = _resolve_base_url(base_url)
    if not endpoint:
        # Tier-1 disabled (no endpoint configured) → fail OPEN without any network
        # call, so a fresh self-host deploy never spams a non-existent localhost.
        return True
    system = _build_system(chat_context, filter_rules, importance)
    try:
        resp = await _client_for(endpoint).chat.completions.create(
            model=s.qwen_model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": window_text},
            ],
            temperature=0,
            response_format={"type": "json_object"},
        )
        content = resp.choices[0].message.content or "{}"
        return bool(json.loads(content).get("has_task", False))
    except Exception:  # noqa: BLE001 — fail open, let Claude decide
        logger.warning("Qwen triage failed; failing open (has_task=True)", exc_info=True)
        return True


async def healthcheck(base_url: str | None = None) -> tuple[bool, str]:
    """Honest tier-1 probe for the daily watchdog — does NOT fail open like
    has_task(). A minimal round-trip to the Qwen endpoint; returns (ok, detail).
    When no endpoint is configured, tier-1 is intentionally OFF: skip the probe
    and report ok (nothing to break)."""
    s = get_settings()
    endpoint = _resolve_base_url(base_url)
    if not endpoint:
        return True, ""
    try:
        resp = await _client_for(endpoint).chat.completions.create(
            model=s.qwen_model,
            messages=[
                {"role": "system", "content": 'Respond with strict JSON only: {"has_task": false}.'},
                {"role": "user", "content": "ping"},
            ],
            temperature=0,
            response_format={"type": "json_object"},
        )
        json.loads(resp.choices[0].message.content or "")  # must be parseable JSON
        return True, ""
    except Exception as e:  # noqa: BLE001
        return False, f"{type(e).__name__}: {e}"[:300]
