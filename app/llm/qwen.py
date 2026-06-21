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

_SYSTEM = (
    "You are a triage filter. You read a fragment of a chat conversation "
    "(Russian or English) and decide whether it contains at least one actionable "
    "item: a task, a to-do, a promise, a commitment, an agreement, a request, or "
    "a deadline — for either participant. Casual chatter, greetings, reactions and "
    "small talk are NOT tasks. Respond with strict JSON only: {\"has_task\": true} "
    "or {\"has_task\": false}. No prose."
)

_client: AsyncOpenAI | None = None


def _get_client() -> AsyncOpenAI:
    global _client
    if _client is None:
        s = get_settings()
        _client = AsyncOpenAI(base_url=s.qwen_base_url, api_key=s.qwen_api_key)
    return _client


async def has_task(window_text: str) -> bool:
    s = get_settings()
    try:
        resp = await _get_client().chat.completions.create(
            model=s.qwen_model,
            messages=[
                {"role": "system", "content": _SYSTEM},
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
