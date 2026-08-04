"""Cross-source dedup layer over triaged `signals` — the tg-ai-assistant port
of omi-task-extractor's app/pipeline/claim_dedup.py (see that repo for the
shared architecture this mirrors 1:1 at the algorithm level; only the
storage plumbing differs — no Repo class here, same "reach Mongo via
app.db.get_db()" convention as app/pipeline/triage.py, and
app/signals.py::upsert_signal here takes no repo argument).

── WHY THIS MODULE EXISTS EVEN THOUGH THIS REPO HAS ONLY ONE SIGNAL SOURCE
   TODAY (architectural decision, made explicitly rather than deferred) ──

This repo currently ingests exactly one signal source (telegram — see
app/signals.py). The omi reference's cross-source dedup only ever compares
signals from DIFFERENT sources (see group_signals below), so with a single
source every comparison is skipped at the `source_a == source_b` guard and
group_signals degrades to "every signal becomes its own singleton group" —
functionally a no-op today.

Ported anyway rather than skipped, for two concrete reasons:
  1. app/pipeline/triage.py already ships `_MULTI_SOURCE_BOOST` and
     `_has_multi_source_match`, explicitly documented there as "a
     documented no-op hook... in case a second signal source is ever added
     to this repo", reading the exact `matched_signal_ids` field THIS
     module is what writes. Porting claims.py/claim_dedup.py without this
     module would leave that hook permanently dead with no path to ever
     activate it; porting it makes the hook real today (still inert, but
     genuinely wired end to end) and means a future second source (e.g. a
     Gmail adapter, mirroring the omi reference's own omi+gmail split)
     lights up cross-source corroboration with ZERO further pipeline code
     — only a new ingest adapter feeding the shared `signals` collection.
  2. The port is small, mechanical, and low-risk: this module has no
     dependencies beyond ones already present in this repo
     (app/pipeline/semantic_dedup.py, app/embeddings.py, the triage LLM
     judge pattern) and ships with full test coverage (see
     tests/test_claims.py) proving the no-op-at-one-source behavior
     explicitly, not just asserting it in a docstring.

Same architecture as app/pipeline/semantic_dedup.py's task-near-duplicate
layer (embed -> cosine -> band -> LLM judge for anything above the low
band), REUSED here rather than reinvented — see
semantic_dedup.decide_duplicate's own docstring for why "above low" always
consults the judge instead of auto-merging on a high cosine (a high cosine
alone is not proof of sameness, only a candidate worth confirming).

Two signals are only ever compared as a match CANDIDATE when:
  - they come from DIFFERENT sources — this is cross-SOURCE dedup; two
    telegram chat/day buckets that happen to be near-duplicates of each
    other is a different problem this layer does not attempt to solve
    (today, with one source, this condition can never hold — see above),
    and
  - their ts_start instants are within DEDUP_WINDOW_HOURS (72h) of each
    other — a corroborating signal weeks later is a separate follow-up,
    not "the same signal".

Matched PAIRS are merged into connected components via a plain union-find,
so a three-way corroboration collapses into ONE claim-worthy group instead
of leaving two overlapping pairs unmerged — see group_signals.

Thresholds: Settings.claim_dedup_low/claim_dedup_high (app/config.py),
deliberately SEPARATE from dedup_low/dedup_high (task-title dedup in
app/pipeline/semantic_dedup.py + app/pipeline/batch.py) — see that config's
docstring for why (comparing full signal blurbs across sources sits lower
on the cosine scale than near-identical task titles).
"""
from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from .. import embeddings
from ..llm import claude
from ..signals import upsert_signal
from . import semantic_dedup as sd
from .triage import signal_id

log = logging.getLogger(__name__)

# Cross-source match candidates are only considered when both signals'
# ts_start fall within this many hours of each other — see module docstring.
DEDUP_WINDOW_HOURS = 72


def _signal_text(doc: dict[str, Any]) -> str:
    """title + summary, the text embedded for cross-source comparison — the
    same two fields app/pipeline/triage.py::score_signals feeds the triage
    LLM call, kept consistent so "what the model saw" stays the same shape
    across both stages."""
    title = (doc.get("title") or "").strip()
    summary = (doc.get("summary") or "").strip()
    return f"{title}\n{summary}".strip()


def _within_window(a: Any, b: Any, hours: int = DEDUP_WINDOW_HOURS) -> bool:
    """True when both `a`/`b` are datetimes within `hours` of each other.
    Fail-safe: anything that isn't two comparable datetimes (missing value,
    naive vs. aware mismatch) -> False (no match), never raises."""
    if a is None or b is None:
        return False
    try:
        return abs(a - b) <= timedelta(hours=hours)
    except TypeError:  # naive/aware datetime mismatch — treat as not matched
        return False


class _UnionFind:
    """Minimal union-find over signal_id strings — merges matched PAIRS into
    connected-component GROUPS (so a 3-way corroboration collapses into one
    claim, not two overlapping pairs)."""

    def __init__(self, ids: list[str]) -> None:
        self._parent = {i: i for i in ids}

    def find(self, x: str) -> str:
        while self._parent[x] != x:
            self._parent[x] = self._parent[self._parent[x]]
            x = self._parent[x]
        return x

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self._parent[ra] = rb


async def group_signals(
    docs: list[dict[str, Any]], settings: Any
) -> list[list[dict[str, Any]]]:
    """Group `docs` (triaged signals, verdict != "drop") into claim-worthy
    clusters: every doc becomes its own singleton group by default; docs
    that cross-source-match another doc in the batch are merged into one
    shared group instead. With only ONE signal source configured (this
    repo's current reality), every comparison is skipped at the
    `source_a == source_b` guard and every group comes back a singleton —
    see module docstring for why this is ported and tested anyway.

    Side effect: for every group of size >= 2, writes `matched_signal_ids`
    (the OTHER member ids, per doc) back onto each member signal via
    signals.upsert_signal — the field app/pipeline/triage.py's
    `_has_multi_source_match` reads. Singleton groups get no write (nothing
    changed for them).

    Fail-open: if embeddings are unavailable (empty EMBED_MODEL, or the
    embed call itself fails — see app/embeddings.py), every doc stays its
    own singleton group and nothing is written — whatever tick next sees
    these signals (they're only re-considered while still `claimed: false`)
    simply retries.
    """
    if not docs:
        return []
    ids = [signal_id(d) for d in docs]
    by_id = dict(zip(ids, docs))

    texts = [_signal_text(d) for d in docs]
    embeddable = [(i, t) for i, t in zip(ids, texts) if t]
    vectors: dict[str, list[float]] = {}
    if embeddable:
        vecs = await embeddings.embed([t for _, t in embeddable])
        if vecs and len(vecs) == len(embeddable):
            vectors = {i: v for (i, _), v in zip(embeddable, vecs)}

    uf = _UnionFind(ids)
    if vectors:
        n = len(docs)
        for a in range(n):
            for b in range(a + 1, n):
                doc_a, doc_b = docs[a], docs[b]
                if doc_a.get("source") == doc_b.get("source"):
                    continue  # cross-SOURCE dedup only, see module docstring
                if not _within_window(doc_a.get("ts_start"), doc_b.get("ts_start")):
                    continue
                id_a, id_b = ids[a], ids[b]
                vec_a, vec_b = vectors.get(id_a), vectors.get(id_b)
                if vec_a is None or vec_b is None:
                    continue
                score = sd.cosine(vec_a, vec_b)

                async def _judge(_ta: str = texts[a], _tb: str = texts[b]) -> bool | None:
                    return await claude.judge_same_signal(_ta, _tb)

                is_match = await sd.decide_duplicate(
                    score, settings.claim_dedup_low, settings.claim_dedup_high, _judge
                )
                if is_match:
                    uf.union(id_a, id_b)

    clusters: dict[str, list[str]] = {}
    for i in ids:
        clusters.setdefault(uf.find(i), []).append(i)

    groups: list[list[dict[str, Any]]] = []
    for members in clusters.values():
        groups.append([by_id[m] for m in members])
        if len(members) >= 2:
            for m in members:
                others = [x for x in members if x != m]
                doc = by_id[m]
                await upsert_signal(
                    {
                        "source": doc["source"],
                        "source_id": doc["source_id"],
                        "matched_signal_ids": others,
                    }
                )
    return groups
