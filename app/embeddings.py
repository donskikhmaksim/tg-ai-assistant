"""Local embeddings via the mini's Ollama (bge-m3), behind the token Funnel.

Reuses qwen_base_url / qwen_api_key — same OpenAI-compatible endpoint. Fails
soft: any error returns None, so indexing/retrieval degrade gracefully to the
plain window + summary. This is INTENTIONAL policy (a duplicate TickTick task
is better than a silently lost one) and must not change — but a silent None
made "the endpoint really failed" indistinguishable from "the text just wasn't
similar" from the outside, so every failure is also logged to the
`embedding_failures` collection (see repositories.record_embedding_failure)
for visibility, without altering the fail-soft return.
"""
from __future__ import annotations

import logging

from openai import AsyncOpenAI

from . import repositories as repo
from .config import get_settings

logger = logging.getLogger(__name__)

_client: AsyncOpenAI | None = None


def _get_client() -> AsyncOpenAI:
    global _client
    if _client is None:
        s = get_settings()
        _client = AsyncOpenAI(base_url=s.qwen_base_url, api_key=s.qwen_api_key)
    return _client


async def embed(texts: list[str]) -> list[list[float]] | None:
    """Embed a batch of texts; returns vectors aligned with input, or None."""
    s = get_settings()
    if not s.embed_model or not texts:
        return None
    try:
        resp = await _get_client().embeddings.create(model=s.embed_model, input=texts)
        return [d.embedding for d in resp.data]
    except Exception as exc:  # noqa: BLE001 — fail soft
        logger.warning("Embedding failed", exc_info=True)
        try:
            await repo.record_embedding_failure(f"{type(exc).__name__}: {exc}", len(texts))
        except Exception:  # noqa: BLE001 — diagnostics must never break fail-soft
            logger.debug("Failed to record embedding failure", exc_info=True)
        return None
