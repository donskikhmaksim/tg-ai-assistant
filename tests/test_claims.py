"""Tests for the claims layer — the tg-ai-assistant port of
omi-task-extractor's tests/test_claims.py: app/pipeline/claim_dedup.py
(cross-source grouping — window, source, threshold, judge fail-safety,
matched_signal_ids write-back) and app/pipeline/claims.py (claimable-signal
query, card-doc building — no invented due dates, subtasks empty for a
single action, project falls back to "unsorted" — and the end-to-end tick).

Mongo is faked at the get_db() boundary (signals + claims), same mocking
style as tests/test_triage.py: no real Mongo, no real Anthropic call, no
real embedding endpoint.

This repo has only ONE signal source (telegram) today — see
app/pipeline/claim_dedup.py's module docstring for why the cross-source
dedup logic is ported and tested anyway. Most claim_dedup tests below use a
SYNTHETIC second source ("email") purely to exercise the dedup algorithm
independent of what sources this repo's ingest actually configures today —
see test_group_signals_single_real_source_never_forms_a_multi_signal_group
for the explicit "as deployed today" case.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from app import signals as signals_mod
from app.llm import claude
from app.pipeline import claim_dedup, claims
from app import mcp_readonly as mcpro


def _run(coro):
    return asyncio.run(coro)


# ── fake Mongo boundary: db.signals / db.claims ─────────────────────────────
# Same shape as tests/test_triage.py's FakeCollection, extended with $in
# (get_claimable_signals' triage.verdict filter).


def _get_path(doc: dict, path: str):
    cur = doc
    for part in path.split("."):
        if isinstance(cur, dict):
            cur = cur.get(part)
        else:
            return None
    return cur


def _matches(doc: dict, filt: dict) -> bool:
    for key, cond in filt.items():
        if key == "$or":
            if not any(_matches(doc, sub) for sub in cond):
                return False
            continue
        val = _get_path(doc, key)
        if isinstance(cond, dict):
            if "$gte" in cond and not (val is not None and val >= cond["$gte"]):
                return False
            if "$ne" in cond and val == cond["$ne"]:
                return False
            if "$in" in cond and val not in cond["$in"]:
                return False
        elif val != cond:
            return False
    return True


class FakeCursor:
    def __init__(self, docs):
        self._docs = list(docs)

    def sort(self, field, direction):
        self._docs.sort(key=lambda d: _get_path(d, field), reverse=(direction < 0))
        return self

    def __aiter__(self):
        return self._agen()

    async def _agen(self):
        for d in self._docs:
            yield d


class FakeCollection:
    def __init__(self, docs=None):
        self.docs: list[dict] = list(docs or [])

    def find(self, filt):
        return FakeCursor([d for d in self.docs if _matches(d, filt)])

    async def find_one(self, filt):
        for d in self.docs:
            if _matches(d, filt):
                return d
        return None

    async def update_one(self, filt, update, upsert=False):
        for d in self.docs:
            if _matches(d, filt):
                d.update(update.get("$set", {}))
                return
        if upsert:
            new_doc = dict(filt)
            new_doc.update(update.get("$set", {}))
            self.docs.append(new_doc)

    async def insert_one(self, doc):
        self.docs.append(dict(doc))


class FakeDB:
    def __init__(self, signals=None, claims=None):
        self.signals = FakeCollection(signals)
        self.claims = FakeCollection(claims)


def _use_fake_db(monkeypatch, signal_docs=None, claims_docs=None) -> FakeDB:
    db = FakeDB(signal_docs, claims_docs)
    monkeypatch.setattr(claims, "get_db", lambda: db)
    monkeypatch.setattr(signals_mod, "get_db", lambda: db)
    monkeypatch.setattr(mcpro, "get_db", lambda: db)
    return db


def _signal_doc(
    source_id: str,
    ts_start: datetime,
    source: str = "telegram",
    verdict: str = "auto",
    **overrides,
) -> dict:
    doc = {
        "source": source,
        "source_id": source_id,
        "ts_start": ts_start,
        "ts_end": ts_start,
        "title": f"Signal {source_id}",
        "summary": "some summary",
        "participants_raw": ["владелец"],
        "ingested_at": ts_start,
        "raw_ref": {"chat_id": "user_1", "date": "2026-08-01"},
        "triage": {
            "score": 0.5,
            "category": "commitment",
            "verdict": verdict,
            "reason": "r",
            "rubric_version": "v0",
            "scored_at": ts_start,
        },
    }
    doc.update(overrides)
    return doc


class FakeDedupSettings:
    claim_dedup_low = 0.75
    claim_dedup_high = 0.90


class FakeClaimsSettings(FakeDedupSettings):
    claims_lookback_days = 3


async def _no_ticktick():
    return None


# ── claim_dedup.group_signals ───────────────────────────────────────────
# Uses a SYNTHETIC "email" source (this repo doesn't ingest one today) purely
# to exercise the cross-source algorithm — see module docstring.


def test_group_signals_finds_cross_source_pair_within_window_above_threshold(monkeypatch):
    now = datetime(2026, 8, 3, 12, 0, tzinfo=timezone.utc)
    a = _signal_doc("tg-1", now, source="telegram")
    b = _signal_doc("email-1", now + timedelta(hours=10), source="email")
    _use_fake_db(monkeypatch, signal_docs=[a, b])

    async def fake_embed(texts):
        return [[1.0, 0.0] for _ in texts]  # identical -> cosine 1.0

    async def fake_judge(x, y):
        return True

    monkeypatch.setattr(claim_dedup.embeddings, "embed", fake_embed)
    monkeypatch.setattr(claude, "judge_same_signal", fake_judge)

    groups = _run(claim_dedup.group_signals([a, b], FakeDedupSettings()))

    assert len(groups) == 1
    assert {d["source_id"] for d in groups[0]} == {"tg-1", "email-1"}
    # matched_signal_ids was written back via signals.upsert_signal -> the
    # same FakeDB instance the module-level monkeypatch installed.
    db = signals_mod.get_db()
    by_id = {d["source_id"]: d for d in db.signals.docs}
    assert by_id["tg-1"]["matched_signal_ids"] == ["email:email-1"]
    assert by_id["email-1"]["matched_signal_ids"] == ["telegram:tg-1"]


def test_group_signals_no_match_outside_72h_window(monkeypatch):
    now = datetime(2026, 8, 3, 12, 0, tzinfo=timezone.utc)
    a = _signal_doc("tg-1", now, source="telegram")
    b = _signal_doc("email-1", now + timedelta(hours=73), source="email")
    db = _use_fake_db(monkeypatch, signal_docs=[a, b])

    async def fake_embed(texts):
        return [[1.0, 0.0] for _ in texts]  # would be a perfect match in-window

    async def must_not_run(x, y):
        raise AssertionError("judge must not be consulted for an out-of-window pair")

    monkeypatch.setattr(claim_dedup.embeddings, "embed", fake_embed)
    monkeypatch.setattr(claude, "judge_same_signal", must_not_run)

    groups = _run(claim_dedup.group_signals([a, b], FakeDedupSettings()))

    assert sorted(len(g) for g in groups) == [1, 1]
    assert "matched_signal_ids" not in db.signals.docs[0]
    assert "matched_signal_ids" not in db.signals.docs[1]


def test_group_signals_no_match_below_low_threshold(monkeypatch):
    now = datetime(2026, 8, 3, 12, 0, tzinfo=timezone.utc)
    a = _signal_doc("tg-1", now, source="telegram")
    b = _signal_doc("email-1", now + timedelta(hours=1), source="email")
    db = _use_fake_db(monkeypatch, signal_docs=[a, b])

    async def fake_embed(texts):
        # orthogonal vectors -> cosine 0.0, well below claim_dedup_low
        return [[1.0, 0.0], [0.0, 1.0]]

    async def must_not_run(x, y):
        raise AssertionError("judge must not be consulted at/below the low band")

    monkeypatch.setattr(claim_dedup.embeddings, "embed", fake_embed)
    monkeypatch.setattr(claude, "judge_same_signal", must_not_run)

    groups = _run(claim_dedup.group_signals([a, b], FakeDedupSettings()))

    assert sorted(len(g) for g in groups) == [1, 1]
    assert "matched_signal_ids" not in db.signals.docs[0]


def test_group_signals_judge_says_no_stays_distinct(monkeypatch):
    now = datetime(2026, 8, 3, 12, 0, tzinfo=timezone.utc)
    a = _signal_doc("tg-1", now, source="telegram")
    b = _signal_doc("email-1", now + timedelta(hours=1), source="email")
    _use_fake_db(monkeypatch, signal_docs=[a, b])

    async def fake_embed(texts):
        return [[1.0, 0.1], [1.0, 0.11]]  # high cosine, but judge says distinct

    async def fake_judge(x, y):
        return False

    monkeypatch.setattr(claim_dedup.embeddings, "embed", fake_embed)
    monkeypatch.setattr(claude, "judge_same_signal", fake_judge)

    groups = _run(claim_dedup.group_signals([a, b], FakeDedupSettings()))

    assert sorted(len(g) for g in groups) == [1, 1]


def test_group_signals_embeddings_unavailable_fails_open_to_singletons(monkeypatch):
    now = datetime(2026, 8, 3, 12, 0, tzinfo=timezone.utc)
    a = _signal_doc("tg-1", now, source="telegram")
    b = _signal_doc("email-1", now + timedelta(hours=1), source="email")
    db = _use_fake_db(monkeypatch, signal_docs=[a, b])

    async def fake_embed(texts):
        return None  # embeddings endpoint unavailable

    async def must_not_run(x, y):
        raise AssertionError("judge must not run without embeddings")

    monkeypatch.setattr(claim_dedup.embeddings, "embed", fake_embed)
    monkeypatch.setattr(claude, "judge_same_signal", must_not_run)

    groups = _run(claim_dedup.group_signals([a, b], FakeDedupSettings()))

    assert sorted(len(g) for g in groups) == [1, 1]
    assert "matched_signal_ids" not in db.signals.docs[0]


def test_group_signals_empty_input_returns_empty():
    assert _run(claim_dedup.group_signals([], FakeDedupSettings())) == []


def test_group_signals_single_real_source_never_forms_a_multi_signal_group(monkeypatch):
    """This repo's ACTUAL deployed reality (as opposed to the synthetic
    "email" source used above to exercise the algorithm): every ingested
    signal is source="telegram". Even two identical, same-minute telegram
    signals must stay distinct singletons — the source_a == source_b guard
    fires before the embedding/judge machinery ever runs, so claim_dedup is
    a genuine no-op with today's single-source configuration."""
    now = datetime(2026, 8, 3, 12, 0, tzinfo=timezone.utc)
    a = _signal_doc("user_1:2026-08-03", now, source="telegram")
    b = _signal_doc("user_2:2026-08-03", now, source="telegram")
    db = _use_fake_db(monkeypatch, signal_docs=[a, b])

    async def fake_embed(texts):
        return [[1.0, 0.0] for _ in texts]  # identical -> would match if compared

    async def must_not_run(x, y):
        raise AssertionError("judge must not be consulted for a same-source pair")

    monkeypatch.setattr(claim_dedup.embeddings, "embed", fake_embed)
    monkeypatch.setattr(claude, "judge_same_signal", must_not_run)

    groups = _run(claim_dedup.group_signals([a, b], FakeDedupSettings()))

    assert sorted(len(g) for g in groups) == [1, 1]
    assert "matched_signal_ids" not in db.signals.docs[0]
    assert "matched_signal_ids" not in db.signals.docs[1]


# ── claims.get_claimable_signals ────────────────────────────────────────


def test_get_claimable_signals_filters_verdict_claimed_and_window(monkeypatch):
    now = datetime(2026, 8, 3, 12, 0, tzinfo=timezone.utc)
    auto = _signal_doc("c-auto", now - timedelta(hours=1), verdict="auto")
    decision = _signal_doc("c-decision", now - timedelta(hours=1), verdict="decision")
    dropped = _signal_doc("c-drop", now - timedelta(hours=1), verdict="drop")
    already_claimed = _signal_doc(
        "c-claimed", now - timedelta(hours=1), verdict="auto", claimed=True
    )
    too_old = _signal_doc("c-old", now - timedelta(days=10), verdict="decision")
    _use_fake_db(
        monkeypatch, signal_docs=[auto, decision, dropped, already_claimed, too_old]
    )

    pending = _run(claims.get_claimable_signals(since=now - timedelta(days=3)))

    ids = sorted(d["source_id"] for d in pending)
    assert ids == ["c-auto", "c-decision"]


# ── claims._build_claim_doc ──────────────────────────────────────────────


def test_build_claim_doc_never_invents_a_due_date():
    now = datetime(2026, 8, 3, 12, 0, tzinfo=timezone.utc)
    group = [_signal_doc("c1", now, verdict="auto")]
    card = {
        "title": "Позвонить поставщику по счёту",
        "subtasks": [],
        "description": "«позвоню завтра» — владелец, 2026-08-03",
        "due_date": None,
        "needs_due_clarification": False,
        "project": "unsorted",
        "with_whom": None,
        "needs_clarification": False,
    }

    doc = claims._build_claim_doc(group, card, project_names=set())

    assert doc["due_date"] is None
    assert doc["needs_due_clarification"] is False
    assert doc["signal_ids"] == ["telegram:c1"]
    assert doc["verdict"] == "auto"
    assert doc["claim_id"]
    assert doc["created_at"] is not None


def test_build_claim_doc_subtasks_empty_for_single_action():
    now = datetime(2026, 8, 3, 12, 0, tzinfo=timezone.utc)
    group = [_signal_doc("c1", now)]
    card = {
        "title": "Отправить контракт клиенту",
        "subtasks": [],
        "description": "d",
        "due_date": None,
        "needs_due_clarification": False,
        "project": "unsorted",
        "with_whom": None,
        "needs_clarification": False,
    }

    doc = claims._build_claim_doc(group, card, project_names=set())

    assert doc["subtasks"] == []


def test_build_claim_doc_keeps_real_subtasks_for_multiple_actions():
    now = datetime(2026, 8, 3, 12, 0, tzinfo=timezone.utc)
    group = [_signal_doc("c1", now)]
    card = {
        "title": "Согласовать и отправить договор клиенту",
        "subtasks": ["Согласовать текст договора с юристом", "Отправить договор клиенту"],
        "description": "d",
        "due_date": None,
        "needs_due_clarification": False,
        "project": "unsorted",
        "with_whom": None,
        "needs_clarification": False,
    }

    doc = claims._build_claim_doc(group, card, project_names=set())

    assert doc["subtasks"] == [
        "Согласовать текст договора с юристом",
        "Отправить договор клиенту",
    ]


def test_build_claim_doc_project_falls_back_to_unsorted_when_not_in_closed_list():
    now = datetime(2026, 8, 3, 12, 0, tzinfo=timezone.utc)
    group = [_signal_doc("c1", now)]
    card = {
        "title": "Оплатить счёт за хостинг",
        "subtasks": [],
        "description": "d",
        "due_date": None,
        "needs_due_clarification": False,
        "project": "Совершенно случайный проект",  # not in the known list
        "with_whom": None,
        "needs_clarification": False,
    }

    doc = claims._build_claim_doc(group, card, project_names={"Work", "Personal"})

    assert doc["project"] == "unsorted"


def test_build_claim_doc_keeps_project_when_in_closed_list():
    now = datetime(2026, 8, 3, 12, 0, tzinfo=timezone.utc)
    group = [_signal_doc("c1", now)]
    card = {
        "title": "Оплатить счёт за хостинг",
        "subtasks": [],
        "description": "d",
        "due_date": None,
        "needs_due_clarification": False,
        "project": "Work",
        "with_whom": None,
        "needs_clarification": False,
    }

    doc = claims._build_claim_doc(group, card, project_names={"Work", "Personal"})

    assert doc["project"] == "Work"


def test_build_claim_doc_verdict_decision_wins_over_auto_in_a_group():
    now = datetime(2026, 8, 3, 12, 0, tzinfo=timezone.utc)
    group = [
        _signal_doc("c1", now, verdict="auto"),
        _signal_doc("c2", now, source="email", verdict="decision"),
    ]
    card = {
        "title": "t", "subtasks": [], "description": "d", "due_date": None,
        "needs_due_clarification": False, "project": "unsorted",
        "with_whom": None, "needs_clarification": False,
    }

    doc = claims._build_claim_doc(group, card, project_names=set())

    assert doc["verdict"] == "decision"
    assert set(doc["signal_ids"]) == {"telegram:c1", "email:c2"}


# ── claims.run_claims_tick (end-to-end, dedup + generation mocked) ──────


def test_run_claims_tick_writes_claim_and_marks_signals_claimed(monkeypatch):
    now = datetime(2026, 8, 3, 12, 0, tzinfo=timezone.utc)
    sig = _signal_doc("c1", now - timedelta(hours=1), verdict="decision")
    db = _use_fake_db(monkeypatch, signal_docs=[sig])

    async def fake_group(docs, settings):
        return [docs]  # one singleton group

    async def fake_generate(groups, project_names):
        return [
            {
                "index": 0,
                "title": "Отправить материалы клиенту",
                "subtasks": [],
                "description": "d",
                "due_date": None,
                "needs_due_clarification": False,
                "project": "unsorted",
                "with_whom": None,
                "needs_clarification": False,
            }
        ]

    monkeypatch.setattr(claims.claim_dedup, "group_signals", fake_group)
    monkeypatch.setattr(claude, "generate_claims_batch", fake_generate)
    monkeypatch.setattr(claims, "resolve_ticktick", _no_ticktick)

    count = _run(claims.run_claims_tick(FakeClaimsSettings()))

    assert count == 1
    assert len(db.claims.docs) == 1
    claim_doc = db.claims.docs[0]
    assert claim_doc["title"] == "Отправить материалы клиенту"
    assert claim_doc["signal_ids"] == ["telegram:c1"]
    assert claim_doc["verdict"] == "decision"
    by_id = {d["source_id"]: d for d in db.signals.docs}
    assert by_id["c1"]["claimed"] is True


def test_run_claims_tick_leaves_signal_unclaimed_when_generation_fails(monkeypatch):
    now = datetime(2026, 8, 3, 12, 0, tzinfo=timezone.utc)
    sig = _signal_doc("c1", now - timedelta(hours=1), verdict="auto")
    db = _use_fake_db(monkeypatch, signal_docs=[sig])

    async def fake_group(docs, settings):
        return [docs]

    async def failing_generate(groups, project_names):
        return []  # whole batch call failed, same contract as score_triage_batch

    monkeypatch.setattr(claims.claim_dedup, "group_signals", fake_group)
    monkeypatch.setattr(claude, "generate_claims_batch", failing_generate)
    monkeypatch.setattr(claims, "resolve_ticktick", _no_ticktick)

    count = _run(claims.run_claims_tick(FakeClaimsSettings()))

    assert count == 0
    assert db.claims.docs == []
    assert "claimed" not in db.signals.docs[0]


def test_run_claims_tick_no_pending_signals_returns_zero(monkeypatch):
    async def must_not_run(*a, **kw):
        raise AssertionError("must not run dedup/generation when nothing is pending")

    monkeypatch.setattr(claims.claim_dedup, "group_signals", must_not_run)
    monkeypatch.setattr(claude, "generate_claims_batch", must_not_run)
    monkeypatch.setattr(claims, "resolve_ticktick", _no_ticktick)
    _use_fake_db(monkeypatch, signal_docs=[])

    count = _run(claims.run_claims_tick(FakeClaimsSettings()))

    assert count == 0


# ── MCP tool: get_claims_queue — called directly, same style as
# tests/test_triage.py's get_triage_queue/submit_triage_feedback tests ────


def test_get_claims_queue_returns_only_matching_verdict(monkeypatch):
    now = datetime(2026, 8, 3, 12, 0, tzinfo=timezone.utc)
    decision_claim = {
        "claim_id": "claim-1", "signal_ids": ["telegram:c1"], "title": "Decide something",
        "subtasks": [], "description": "d", "due_date": None,
        "needs_due_clarification": False, "project": "unsorted",
        "with_whom": None, "needs_clarification": False,
        "verdict": "decision", "created_at": now,
    }
    auto_claim = {
        "claim_id": "claim-2", "signal_ids": ["telegram:t1"], "title": "Do something",
        "subtasks": [], "description": "d", "due_date": None,
        "needs_due_clarification": False, "project": "unsorted",
        "with_whom": None, "needs_clarification": False,
        "verdict": "auto", "created_at": now,
    }
    _use_fake_db(monkeypatch, claims_docs=[decision_claim, auto_claim])

    items = _run(mcpro.get_claims_queue(verdict="decision", days_back=3))

    assert [i["claim_id"] for i in items] == ["claim-1"]
    assert items[0]["title"] == "Decide something"


def test_get_claims_queue_returns_both_verdicts_when_unfiltered(monkeypatch):
    now = datetime(2026, 8, 3, 12, 0, tzinfo=timezone.utc)
    decision_claim = {
        "claim_id": "claim-1", "signal_ids": ["telegram:c1"], "title": "A",
        "subtasks": [], "description": "d", "due_date": None,
        "needs_due_clarification": False, "project": "unsorted",
        "with_whom": None, "needs_clarification": False,
        "verdict": "decision", "created_at": now,
    }
    auto_claim = {
        "claim_id": "claim-2", "signal_ids": ["telegram:t1"], "title": "B",
        "subtasks": [], "description": "d", "due_date": None,
        "needs_due_clarification": False, "project": "unsorted",
        "with_whom": None, "needs_clarification": False,
        "verdict": "auto", "created_at": now,
    }
    _use_fake_db(monkeypatch, claims_docs=[decision_claim, auto_claim])

    items = _run(mcpro.get_claims_queue(days_back=3))

    assert {i["claim_id"] for i in items} == {"claim-1", "claim-2"}
