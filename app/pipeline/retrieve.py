"""Embedding-based retrieval of relevant past messages (deep recall).

Two jobs, both fail-soft (degrade to window + summary on any error):
  - index_messages: embed not-yet-stored messages into the permanent archive.
  - retrieve: given the current window as a query, return the most relevant
    OLDER messages (semantic match), so a topic revisited weeks later is
    grounded in actual past evidence rather than a lossy summary.
"""
from __future__ import annotations

import logging
import math
from typing import Any

from .. import repositories as repo
from ..config import get_settings
from ..embeddings import embed

logger = logging.getLogger(__name__)


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


async def index_messages(chat_id: str, messages: list[dict[str, Any]]) -> None:
    """Embed and store any messages not already in the archive."""
    s = get_settings()
    if not s.embed_model:
        return
    msgs = [m for m in messages if (m.get("text") or "").strip()]
    if not msgs:
        return
    have = await repo.existing_vector_ids(chat_id, [m["messageId"] for m in msgs])
    todo = [m for m in msgs if m["messageId"] not in have]
    if not todo:
        return
    vecs = await embed([m["text"] for m in todo])
    if not vecs or len(vecs) != len(todo):
        return
    items = [
        {"messageId": m["messageId"], "text": m["text"], "date": m["date"], "embedding": v}
        for m, v in zip(todo, vecs)
    ]
    await repo.store_vectors(chat_id, items)


async def retrieve(chat_id: str, query_text: str, exclude_ids: set[int]) -> list[str]:
    """Top-k past message texts most relevant to the query window."""
    s = get_settings()
    if not s.embed_model or not query_text.strip():
        return []
    qv = await embed([query_text])
    if not qv:
        return []
    query = qv[0]
    candidates = await repo.get_chat_vectors(chat_id, exclude_ids)
    if not candidates:
        return []
    scored = [
        (_cosine(query, c["embedding"]), c["text"])
        for c in candidates
        if c.get("embedding")
    ]
    scored.sort(key=lambda x: x[0], reverse=True)
    return [text for score, text in scored[: s.retrieve_top_k] if score >= s.retrieve_min_score]
