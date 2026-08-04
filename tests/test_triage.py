"""app/pipeline/triage.py: score_signals' batched-LLM mapping (index
matching, clamping, the multi-source-boost no-op path — see triage.py's
_MULTI_SOURCE_BOOST docstring for why it never fires in this repo),
threshold classification (verdict_for_score), get_pending_triage_signals'
Mongo filter, run_triage_tick's write-back via signals.upsert_signal, and
record_triage_feedback — plus the two new read-only-MCP tools
(get_triage_queue / submit_triage_feedback) in app/mcp_readonly.py, called
directly the same way tests/test_mcp_readonly.py already calls
list_conversations/list_tasks/get_daily_bundle (mcp.tool() registers and
returns `fn` unchanged — see that module's own "MCP tool registration"
comment — so these stay directly callable without the protocol layer).

Mongo is faked at the get_db() boundary (signals + triage_feedback,
extended beyond tests/test_signals.py's simpler $gte/$lt-only fake with
$exists/$ne/$or/dotted-path support — triage's own queries need them) and
the LLM is faked at the app.llm.claude module boundary
(monkeypatch.setattr(claude, "score_triage_batch", ...)) — same mocking
style as tests/test_signals.py / tests/test_mcp_readonly.py: no real Mongo,
no real Anthropic call.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

import app.mcp_readonly as mcpro
from app import signals as signals_mod
from app.llm import claude
from app.pipeline import triage


def _run(coro):
    return asyncio.run(coro)


# ─────────────────────────────────────────────────────────────────────────────
# Fake Mongo boundary: db.signals / db.triage_feedback. Generalised beyond
# tests/test_signals.py's FakeCollection ($gte/$lt-only) — triage's own
# queries need $exists/$ne/$or/dotted-path too.
# ─────────────────────────────────────────────────────────────────────────────


def _get_path(doc: dict, path: str):
    cur = doc
    for part in path.split("."):
        if isinstance(cur, dict):
            cur = cur.get(part)
        else:
            return None
    return cur


def _path_exists(doc: dict, path: str) -> bool:
    cur = doc
    for part in path.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return False
    return True


def _matches(doc: dict, filt: dict) -> bool:
    for key, cond in filt.items():
        if key == "$or":
            if not any(_matches(doc, sub) for sub in cond):
                return False
            continue
        if isinstance(cond, dict) and "$exists" in cond:
            if _path_exists(doc, key) != cond["$exists"]:
                return False
            continue
        val = _get_path(doc, key)
        if isinstance(cond, dict):
            if "$gte" in cond and not (val is not None and val >= cond["$gte"]):
                return False
            if "$ne" in cond and val == cond["$ne"]:
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
    def __init__(self, signals=None, triage_feedback=None):
        self.signals = FakeCollection(signals)
        self.triage_feedback = FakeCollection(triage_feedback)


def _use_fake_db(monkeypatch, signal_docs=None, triage_feedback=None) -> FakeDB:
    db = FakeDB(signal_docs, triage_feedback)
    monkeypatch.setattr(triage, "get_db", lambda: db)
    monkeypatch.setattr(signals_mod, "get_db", lambda: db)
    monkeypatch.setattr(mcpro, "get_db", lambda: db)
    return db


def _signal_doc(source_id: str, ts_start: datetime, source: str = "telegram", **overrides) -> dict:
    doc = {
        "source": source,
        "source_id": source_id,
        "ts_start": ts_start,
        "ts_end": ts_start,
        "title": f"Signal {source_id}",
        "summary": "some summary",
        "participants_raw": ["владелец", "Дима"],
        "ingested_at": ts_start,
        "raw_ref": {"chat_id": "user_1", "date": "2026-08-01"},
    }
    doc.update(overrides)
    return doc


class FakeSettings:
    triage_low = 0.3
    triage_high = 0.7
    triage_lookback_days = 3


# ─────────────────────────────────────────────────────────────────────────────
# verdict_for_score
# ─────────────────────────────────────────────────────────────────────────────


def test_verdict_for_score_thresholds():
    assert triage.verdict_for_score(0.0, 0.3, 0.7) == "drop"
    assert triage.verdict_for_score(0.29, 0.3, 0.7) == "drop"
    assert triage.verdict_for_score(0.3, 0.3, 0.7) == "auto"
    assert triage.verdict_for_score(0.5, 0.3, 0.7) == "auto"
    assert triage.verdict_for_score(0.7, 0.3, 0.7) == "auto"
    assert triage.verdict_for_score(0.71, 0.3, 0.7) == "decision"
    assert triage.verdict_for_score(1.0, 0.3, 0.7) == "decision"


# ─────────────────────────────────────────────────────────────────────────────
# score_signals
# ─────────────────────────────────────────────────────────────────────────────


def test_score_signals_empty_batch_returns_empty_without_calling_llm(monkeypatch):
    async def must_not_run(items):
        raise AssertionError("score_triage_batch must not run for an empty batch")

    monkeypatch.setattr(claude, "score_triage_batch", must_not_run)

    assert _run(triage.score_signals([])) == []


def test_score_signals_maps_by_index_and_clamps_out_of_range_scores(monkeypatch):
    now = datetime(2026, 8, 1, tzinfo=timezone.utc)
    docs = [_signal_doc("user_1:2026-08-01", now), _signal_doc("user_2:2026-08-01", now)]

    async def fake_batch(items):
        assert len(items) == 2
        return [
            {"index": 0, "score": 1.5, "category": "commitment", "reason": "over"},
            {"index": 1, "score": -0.2, "category": "noise", "reason": "under"},
        ]

    monkeypatch.setattr(claude, "score_triage_batch", fake_batch)

    results = _run(triage.score_signals(docs))

    assert results[0] == {
        "signal_id": "telegram:user_1:2026-08-01", "score": 1.0,
        "category": "commitment", "reason": "over",
    }
    assert results[1] == {
        "signal_id": "telegram:user_2:2026-08-01", "score": 0.0,
        "category": "noise", "reason": "under",
    }


def test_score_signals_missing_verdict_index_yields_none_score(monkeypatch):
    now = datetime(2026, 8, 1, tzinfo=timezone.utc)
    docs = [_signal_doc("user_1:2026-08-01", now), _signal_doc("user_2:2026-08-01", now)]

    async def fake_batch(items):
        return [{"index": 0, "score": 0.9, "category": "request", "reason": "ok"}]

    monkeypatch.setattr(claude, "score_triage_batch", fake_batch)

    results = _run(triage.score_signals(docs))

    assert results[0]["score"] == 0.9
    assert results[1]["signal_id"] == "telegram:user_2:2026-08-01"
    assert results[1]["score"] is None
    assert results[1]["category"] is None


def test_score_signals_llm_failure_returns_all_unscored(monkeypatch):
    now = datetime(2026, 8, 1, tzinfo=timezone.utc)
    docs = [_signal_doc("user_1:2026-08-01", now)]

    async def failing_batch(items):
        return []

    monkeypatch.setattr(claude, "score_triage_batch", failing_batch)

    results = _run(triage.score_signals(docs))

    assert results == [
        {
            "signal_id": "telegram:user_1:2026-08-01",
            "score": None,
            "category": None,
            "reason": "scoring unavailable (batch call failed or returned no verdict for this signal)",
        }
    ]


def test_score_signals_multi_source_boost_applied_and_clamped(monkeypatch):
    now = datetime(2026, 8, 1, tzinfo=timezone.utc)
    docs = [_signal_doc("user_1:2026-08-01", now), _signal_doc("user_2:2026-08-01", now)]

    async def fake_batch(items):
        return [
            {"index": 0, "score": 0.5, "category": "request", "reason": "boosted"},
            {"index": 1, "score": 0.95, "category": "request", "reason": "clamped"},
        ]

    monkeypatch.setattr(claude, "score_triage_batch", fake_batch)
    # Both signals "have a multi-source match" in this test, exercising the
    # boost path even though this repo never sets matched_signal_ids today.
    monkeypatch.setattr(triage, "_has_multi_source_match", lambda doc: True)

    results = _run(triage.score_signals(docs))

    assert results[0]["score"] == pytest.approx(0.65)  # 0.5 + 0.15
    assert results[1]["score"] == 1.0  # 0.95 + 0.15 clamped to 1.0


def test_score_signals_no_boost_by_default_path_is_inert(monkeypatch):
    # _has_multi_source_match's real (unpatched) body always returns False in
    # this repo (no cross-source dedup layer here) — the boost must never
    # fire on its own.
    now = datetime(2026, 8, 1, tzinfo=timezone.utc)
    docs = [_signal_doc("user_1:2026-08-01", now)]

    async def fake_batch(items):
        return [{"index": 0, "score": 0.5, "category": "request", "reason": "no boost"}]

    monkeypatch.setattr(claude, "score_triage_batch", fake_batch)

    results = _run(triage.score_signals(docs))

    assert results[0]["score"] == 0.5


# ─────────────────────────────────────────────────────────────────────────────
# get_pending_triage_signals
# ─────────────────────────────────────────────────────────────────────────────


def test_get_pending_triage_signals_selects_untriaged_and_stale_rubric_within_window(monkeypatch):
    now = datetime(2026, 8, 3, 12, tzinfo=timezone.utc)
    untriaged = _signal_doc("c1", now - timedelta(hours=1))
    fresh_rubric = _signal_doc(
        "c2", now - timedelta(hours=1),
        triage={"score": 0.5, "category": "info", "verdict": "auto",
                "reason": "r", "rubric_version": triage.RUBRIC_VERSION, "scored_at": now},
    )
    stale_rubric = _signal_doc(
        "c3", now - timedelta(hours=1),
        triage={"score": 0.5, "category": "info", "verdict": "auto",
                "reason": "r", "rubric_version": "v-old", "scored_at": now},
    )
    too_old = _signal_doc("c4", now - timedelta(days=10))
    _use_fake_db(monkeypatch, signal_docs=[untriaged, fresh_rubric, stale_rubric, too_old])

    pending = _run(triage.get_pending_triage_signals(since=now - timedelta(days=3)))

    ids = sorted(d["source_id"] for d in pending)
    assert ids == ["c1", "c3"]


def test_get_pending_triage_signals_empty_when_nothing_pending(monkeypatch):
    now = datetime(2026, 8, 3, 12, tzinfo=timezone.utc)
    scored = _signal_doc(
        "c1", now,
        triage={"score": 0.5, "category": "info", "verdict": "auto",
                "reason": "r", "rubric_version": triage.RUBRIC_VERSION, "scored_at": now},
    )
    _use_fake_db(monkeypatch, signal_docs=[scored])

    pending = _run(triage.get_pending_triage_signals(since=now - timedelta(days=3)))

    assert pending == []


# ─────────────────────────────────────────────────────────────────────────────
# run_triage_tick
# ─────────────────────────────────────────────────────────────────────────────


def test_run_triage_tick_writes_triage_field_with_verdicts(monkeypatch):
    now = datetime(2026, 8, 3, 12, tzinfo=timezone.utc)
    low = _signal_doc("c-low", now - timedelta(hours=1))
    high = _signal_doc("c-high", now - timedelta(hours=1))
    db = _use_fake_db(monkeypatch, signal_docs=[low, high])

    async def fake_batch(items):
        return [
            {"index": 0, "score": 0.1, "category": "noise", "reason": "chit-chat"},
            {"index": 1, "score": 0.9, "category": "commitment", "reason": "needs a decision"},
        ]

    monkeypatch.setattr(claude, "score_triage_batch", fake_batch)

    count = _run(triage.run_triage_tick(FakeSettings()))

    assert count == 2
    by_id = {d["source_id"]: d for d in db.signals.docs}
    assert by_id["c-low"]["triage"]["verdict"] == "drop"
    assert by_id["c-low"]["triage"]["score"] == 0.1
    assert by_id["c-low"]["triage"]["rubric_version"] == triage.RUBRIC_VERSION
    assert by_id["c-low"]["triage"]["scored_at"] is not None
    assert by_id["c-high"]["triage"]["verdict"] == "decision"
    assert by_id["c-high"]["triage"]["category"] == "commitment"


def test_run_triage_tick_skips_unscored_signals(monkeypatch):
    now = datetime(2026, 8, 3, 12, tzinfo=timezone.utc)
    doc = _signal_doc("c1", now - timedelta(hours=1))
    db = _use_fake_db(monkeypatch, signal_docs=[doc])

    async def failing_batch(items):
        return []

    monkeypatch.setattr(claude, "score_triage_batch", failing_batch)

    count = _run(triage.run_triage_tick(FakeSettings()))

    assert count == 0
    assert "triage" not in db.signals.docs[0]


def test_run_triage_tick_no_pending_signals_returns_zero(monkeypatch):
    async def must_not_run(items):
        raise AssertionError("must not call the LLM when nothing is pending")

    monkeypatch.setattr(claude, "score_triage_batch", must_not_run)
    _use_fake_db(monkeypatch, signal_docs=[])

    count = _run(triage.run_triage_tick(FakeSettings()))

    assert count == 0


# ─────────────────────────────────────────────────────────────────────────────
# record_triage_feedback
# ─────────────────────────────────────────────────────────────────────────────


def test_record_triage_feedback_inserts_document(monkeypatch):
    db = _use_fake_db(monkeypatch)

    _run(triage.record_triage_feedback("telegram:c1", "approved", "looked right"))

    assert len(db.triage_feedback.docs) == 1
    doc = db.triage_feedback.docs[0]
    assert doc["signal_id"] == "telegram:c1"
    assert doc["human_verdict"] == "approved"
    assert doc["note"] == "looked right"
    assert doc["created_at"] is not None


def test_record_triage_feedback_note_optional(monkeypatch):
    db = _use_fake_db(monkeypatch)

    _run(triage.record_triage_feedback("telegram:c1", "pulled_from_dropped"))

    assert db.triage_feedback.docs[0]["note"] is None


def test_record_triage_feedback_rejects_unknown_verdict(monkeypatch):
    db = _use_fake_db(monkeypatch)

    with pytest.raises(ValueError):
        _run(triage.record_triage_feedback("telegram:c1", "maybe"))

    assert db.triage_feedback.docs == []


# ─────────────────────────────────────────────────────────────────────────────
# MCP tools: get_triage_queue / submit_triage_feedback — called directly,
# same style as tests/test_mcp_readonly.py's list_conversations etc.
# ─────────────────────────────────────────────────────────────────────────────


def test_get_triage_queue_returns_only_triaged_signals_newest_first(monkeypatch):
    now = datetime.now(timezone.utc)
    untriaged = _signal_doc("c-untriaged", now - timedelta(hours=2))
    older_triaged = _signal_doc(
        "c-old", now - timedelta(hours=3),
        triage={"score": 0.8, "category": "commitment", "verdict": "decision",
                "reason": "r1", "rubric_version": "v0", "scored_at": now},
    )
    newer_triaged = _signal_doc(
        "c-new", now - timedelta(hours=1),
        triage={"score": 0.2, "category": "noise", "verdict": "drop",
                "reason": "r2", "rubric_version": "v0", "scored_at": now},
    )
    _use_fake_db(monkeypatch, signal_docs=[untriaged, older_triaged, newer_triaged])

    items = _run(mcpro.get_triage_queue(days_back=3))

    assert [i["source_id"] for i in items] == ["c-new", "c-old"]  # newest first
    assert items[0]["verdict"] == "drop"
    assert items[0]["signal_id"] == "telegram:c-new"
    assert items[1]["verdict"] == "decision"


def test_get_triage_queue_filters_by_verdict(monkeypatch):
    now = datetime.now(timezone.utc)
    decision = _signal_doc(
        "c-decision", now,
        triage={"score": 0.9, "category": "commitment", "verdict": "decision",
                "reason": "r", "rubric_version": "v0", "scored_at": now},
    )
    drop = _signal_doc(
        "c-drop", now,
        triage={"score": 0.1, "category": "noise", "verdict": "drop",
                "reason": "r", "rubric_version": "v0", "scored_at": now},
    )
    _use_fake_db(monkeypatch, signal_docs=[decision, drop])

    items = _run(mcpro.get_triage_queue(verdict="decision", days_back=3))

    assert [i["source_id"] for i in items] == ["c-decision"]


def test_submit_triage_feedback_ok(monkeypatch):
    db = _use_fake_db(monkeypatch)

    result = _run(
        mcpro.submit_triage_feedback(signal_id="telegram:c1", verdict="approved", note="good call")
    )

    assert result == {"ok": True}
    assert db.triage_feedback.docs[0]["signal_id"] == "telegram:c1"
    assert db.triage_feedback.docs[0]["human_verdict"] == "approved"


def test_submit_triage_feedback_invalid_verdict_returns_error_not_raise(monkeypatch):
    db = _use_fake_db(monkeypatch)

    result = _run(mcpro.submit_triage_feedback(signal_id="telegram:c1", verdict="not-a-real-verdict"))

    assert result["ok"] is False
    assert "error" in result
    assert db.triage_feedback.docs == []
