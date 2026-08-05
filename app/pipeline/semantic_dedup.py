"""Pure similarity/decision core for semantic near-duplicate task detection.

No I/O here — embedding and storage live in the pipeline (batch.py). This module
only decides, given a query embedding and a set of candidate embeddings, whether
a new task is a near-duplicate of an existing one, and what (if anything) is
genuinely-new detail to append when enriching the existing task instead of
creating a second one.
"""
from __future__ import annotations

import logging
import math
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)


def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def best_match(
    query: list[float],
    candidates: list[dict[str, Any]],
    threshold: float,
) -> dict[str, Any] | None:
    """The candidate most similar to `query`, if its cosine ≥ `threshold`.

    Each candidate is a dict carrying at least an "embedding" (other keys — id,
    title, projectId — are passed through untouched). Returns a shallow copy of
    the winning candidate with an added "score", or None if nothing qualifies.
    Ties keep the first (highest-priority) candidate, so callers can order the
    list by preference (e.g. the chat's own open tasks before project tasks).
    """
    best: dict[str, Any] | None = None
    best_score = threshold
    for c in candidates:
        vec = c.get("embedding")
        if not vec:
            continue
        score = cosine(query, vec)
        if score > best_score or (best is None and score >= threshold):
            best, best_score = c, score
    if best is None:
        return None
    return {**best, "score": best_score}


def band(score: float, low: float, high: float) -> str:
    """Classify a best-match cosine into one of three bands:
    "duplicate" (≥ high), "distinct" (≤ low), or "gray" (in between)."""
    if score >= high:
        return "duplicate"
    if score <= low:
        return "distinct"
    return "gray"


async def decide_duplicate(
    score: float,
    low: float,
    high: float,
    judge: Callable[[], Awaitable[bool | None]],
    safety_net: float | None = None,
) -> bool:
    """Whether a new task should be treated as a duplicate of its best match.

    ≤ low → distinct (fast, no judge). ABOVE low → ALWAYS confirm with the cheap
    LLM `judge()` — we do NOT auto-merge on a high cosine anymore. A high cosine
    is not proof: tasks differing only in a URL or a number are ~0.93+ yet
    DISTINCT (e.g. 4 different Instagram reels, or "…декларация за 2025" vs
    "…2026"), and a false merge SKIPS creating the task — dropping a real one.
    BIAS TO SAFE: a judge that returns None or raises means DISTINCT (create).
    `high` is kept for signature/config compatibility but no longer auto-merges.

    `safety_net`: a NARROW carve-out for when the judge is UNAVAILABLE (raises,
    times out, or answers None) rather than having genuinely answered
    «distinct». If the cosine is at/above `safety_net` in that unavailable
    case, merge anyway instead of falling back to distinct — a near-certain
    duplicate shouldn't silently flood in just because the judge's LLM/shim
    infra happens to be down. Below `safety_net` (but above `low`) an
    unavailable judge still means distinct, unchanged. `safety_net=None` (the
    default) disables the net entirely — old callers that don't pass it keep
    the old always-distinct-on-doubt behaviour. Never engages when the judge
    answered normally (True/False)."""
    if score <= low:
        return False
    try:
        verdict = await judge()
    except Exception:  # noqa: BLE001 — never drop a task because the judge failed
        logger.warning("Dedup judge failed; treating as distinct (create)", exc_info=True)
        verdict = None
    if verdict is None:
        if safety_net is not None and score >= safety_net:
            logger.warning(
                "Dedup judge unavailable, auto-merged via safety-net cosine=%.3f (>= %.3f)",
                score, safety_net,
            )
            return True
        return False
    return verdict is True


def merge_details(existing: str | None, new: str | None) -> str | None:
    """The detail text to append when enriching, or None if there's nothing new.

    Empty/whitespace `new` → None. `new` already contained in `existing`
    (case-insensitive) → None (avoids re-appending the same context on every
    overlapping window). Otherwise the trimmed `new` text to append.
    """
    extra = (new or "").strip()
    if not extra:
        return None
    base = (existing or "").strip().lower()
    if base and extra.lower() in base:
        return None
    return extra
