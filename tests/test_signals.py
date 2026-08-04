"""app/signals.py: the pure bucket->signal conversion, idempotent upsert
(repeated upsert never duplicates), and ingest_telegram_signals' touched-
bucket-then-full-day-refetch behaviour (the correctness fix that keeps a
re-ingest from regressing a signal doc back down to just its newest
messages — see the module docstring's "Ingest cursor / re-fetch tradeoff").

Follows the repo convention (tests/test_mcp_readonly.py): pure-logic + a
tiny in-memory fake Mongo at the repo.get_db()/signals.get_db() boundary, no
real Mongo, no real MCP transport.
"""
from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

from app import repositories as repo
from app import signals

# ─────────────────────────────────────────────────────────────────────────────
# Tiny in-memory fake Mongo — same shape as tests/test_mcp_readonly.py's,
# extended with $lt (day-range queries) and update_one/upsert (signals.py's
# upsert_signal).
# ─────────────────────────────────────────────────────────────────────────────


def _run(coro):
    return asyncio.run(coro)


def _matches(doc: dict, query: dict) -> bool:
    for k, cond in query.items():
        val = doc.get(k)
        if isinstance(cond, dict):
            for op, target in cond.items():
                if op == "$gte" and not (val is not None and val >= target):
                    return False
                if op == "$lt" and not (val is not None and val < target):
                    return False
        elif val != cond:
            return False
    return True


def _project(doc: dict, projection: dict | None) -> dict:
    if not projection:
        return dict(doc)
    return {k: v for k, v in doc.items() if k in projection or k == "_id"}


class FakeCursor:
    def __init__(self, docs: list[dict], projection: dict | None = None):
        self._docs = docs
        self._projection = projection
        self._sort_field = None
        self._sort_dir = 1

    def sort(self, field, direction=1):
        self._sort_field = field
        self._sort_dir = direction
        return self

    def __aiter__(self):
        docs = list(self._docs)
        if self._sort_field:
            docs.sort(key=lambda d: d.get(self._sort_field), reverse=(self._sort_dir == -1))
        self._it = iter(_project(d, self._projection) for d in docs)
        return self

    async def __anext__(self):
        try:
            return next(self._it)
        except StopIteration:
            raise StopAsyncIteration


class FakeCollection:
    def __init__(self, docs: list[dict] | None = None):
        self.docs: list[dict] = list(docs or [])

    def find(self, query: dict | None = None, projection: dict | None = None):
        query = query or {}
        return FakeCursor([d for d in self.docs if _matches(d, query)], projection)

    async def find_one(self, query: dict | None = None, projection: dict | None = None):
        query = query or {}
        for d in self.docs:
            if _matches(d, query):
                return _project(d, projection)
        return None

    async def update_one(self, query: dict, update: dict, upsert: bool = False):
        for d in self.docs:
            if _matches(d, query):
                d.update(update.get("$set", {}))
                for field in update.get("$unset", {}):
                    d.pop(field, None)
                return
        if upsert:
            new_doc = dict(query)
            new_doc.update(update.get("$set", {}))
            for field in update.get("$unset", {}):
                new_doc.pop(field, None)
            self.docs.append(new_doc)


class FakeDB:
    def __init__(self, raw_messages=None, chat_state=None, signals_docs=None):
        self.raw_messages = FakeCollection(raw_messages or [])
        self.chat_state = FakeCollection(chat_state or [])
        self.signals = FakeCollection(signals_docs or [])


def _use_fake_db(monkeypatch, raw_messages=None, chat_state=None, signals_docs=None) -> FakeDB:
    db = FakeDB(raw_messages, chat_state, signals_docs)
    monkeypatch.setattr(signals, "get_db", lambda: db)
    monkeypatch.setattr(repo, "get_db", lambda: db)
    return db


def _use_utc_settings(monkeypatch):
    monkeypatch.setattr(signals, "get_settings", lambda: SimpleNamespace(default_timezone="UTC"))


def _msg(chat_id: str, when: datetime, text: str, message_id: int, direction="in", sender=None):
    return {
        "chatId": chat_id,
        "direction": direction,
        "senderName": sender,
        "text": text,
        "messageId": message_id,
        "date": when,
    }


def _dt(day_offset: int, hour: int = 12):
    d = datetime.now(timezone.utc).date() - timedelta(days=day_offset)
    return datetime(d.year, d.month, d.day, hour, tzinfo=timezone.utc)


# ─────────────────────────────────────────────────────────────────────────────
# telegram_bucket_to_signal (pure conversion)
# ─────────────────────────────────────────────────────────────────────────────


def test_telegram_bucket_to_signal_basic_fields():
    day = date(2026, 8, 3)
    messages = [
        _msg("user_111", datetime(2026, 8, 3, 9, 0, tzinfo=timezone.utc), "первое", 1, direction="in", sender="Дима"),
        _msg("user_111", datetime(2026, 8, 3, 15, 30, tzinfo=timezone.utc), "второе", 2, direction="out"),
    ]

    doc = signals.telegram_bucket_to_signal("user_111", day, messages, "Дима")

    assert doc["source"] == "telegram"
    assert doc["source_id"] == "user_111:2026-08-03"
    assert doc["ts_start"] == datetime(2026, 8, 3, 9, 0, tzinfo=timezone.utc)
    assert doc["ts_end"] == datetime(2026, 8, 3, 15, 30, tzinfo=timezone.utc)
    assert doc["title"] == "Дима"
    assert doc["participants_raw"] == ["Дима", "владелец"]  # first-appearance order
    assert doc["summary"] == "Дима: первое\nвладелец: второе"
    assert doc["raw_ref"] == {"chat_id": "user_111", "date": "2026-08-03", "message_ids": [1, 2]}


def test_telegram_bucket_to_signal_sorts_unsorted_input():
    day = date(2026, 8, 3)
    messages = [
        _msg("user_1", datetime(2026, 8, 3, 15, 0, tzinfo=timezone.utc), "позже", 2),
        _msg("user_1", datetime(2026, 8, 3, 9, 0, tzinfo=timezone.utc), "раньше", 1),
    ]

    doc = signals.telegram_bucket_to_signal("user_1", day, messages, "T")

    assert doc["ts_start"] == datetime(2026, 8, 3, 9, 0, tzinfo=timezone.utc)
    assert doc["summary"] == "собеседник: раньше\nсобеседник: позже"


def test_telegram_bucket_to_signal_dedupes_participants_preserving_first_appearance():
    day = date(2026, 8, 3)
    messages = [
        _msg("group_-1", datetime(2026, 8, 3, 9, tzinfo=timezone.utc), "a", 1, sender="Аня"),
        _msg("group_-1", datetime(2026, 8, 3, 10, tzinfo=timezone.utc), "b", 2, sender="Боря"),
        _msg("group_-1", datetime(2026, 8, 3, 11, tzinfo=timezone.utc), "c", 3, sender="Аня"),
    ]

    doc = signals.telegram_bucket_to_signal("group_-1", day, messages, "Group")

    assert doc["participants_raw"] == ["Аня", "Боря"]


def test_telegram_bucket_to_signal_blank_text_yields_none_summary():
    day = date(2026, 8, 3)
    messages = [
        _msg("user_1", datetime(2026, 8, 3, 9, tzinfo=timezone.utc), "", 1),
        _msg("user_1", datetime(2026, 8, 3, 10, tzinfo=timezone.utc), None, 2),
    ]

    doc = signals.telegram_bucket_to_signal("user_1", day, messages, "T")

    assert doc["summary"] is None
    assert doc["raw_ref"]["message_ids"] == [1, 2]  # still tracked even without text


def test_telegram_bucket_to_signal_truncates_long_summary():
    day = date(2026, 8, 3)
    long_text = "x" * (signals._SUMMARY_MAX_CHARS + 500)
    messages = [_msg("user_1", datetime(2026, 8, 3, 9, tzinfo=timezone.utc), long_text, 1)]

    doc = signals.telegram_bucket_to_signal("user_1", day, messages, "T")

    assert doc["summary"].endswith("…[truncated]")
    assert len(doc["summary"]) < len(long_text)


# ─────────────────────────────────────────────────────────────────────────────
# upsert_signal (idempotency key: source + source_id)
# ─────────────────────────────────────────────────────────────────────────────


def test_upsert_signal_repeated_call_updates_in_place_no_duplicate(monkeypatch):
    db = _use_fake_db(monkeypatch)
    doc_v1 = {
        "source": "telegram", "source_id": "user_1:2026-08-03",
        "ts_start": datetime(2026, 8, 3, tzinfo=timezone.utc), "ts_end": datetime(2026, 8, 3, tzinfo=timezone.utc),
        "title": "Old", "summary": None, "participants_raw": [], "raw_ref": {},
    }
    doc_v2 = dict(doc_v1, title="New", participants_raw=["Дима"])

    _run(signals.upsert_signal(doc_v1))
    _run(signals.upsert_signal(doc_v2))

    assert len(db.signals.docs) == 1
    stored = db.signals.docs[0]
    assert stored["title"] == "New"
    assert stored["participants_raw"] == ["Дима"]
    assert stored["ingested_at"] is not None


def test_upsert_signal_unset_removes_stale_triage_and_claimed(monkeypatch):
    db = _use_fake_db(
        monkeypatch,
        signals_docs=[
            {
                "source": "telegram", "source_id": "user_1:2026-08-03",
                "title": "T", "triage": {"verdict": "auto"}, "claimed": True,
            }
        ],
    )

    _run(
        signals.upsert_signal(
            {"source": "telegram", "source_id": "user_1:2026-08-03", "title": "T2"},
            unset=["triage", "claimed"],
        )
    )

    stored = db.signals.docs[0]
    assert stored["title"] == "T2"
    assert "triage" not in stored
    assert "claimed" not in stored


def test_stale_on_content_change_includes_calendar_extracted():
    """A content-changed bucket (see _bucket_changed/ingest_telegram_signals)
    must invalidate calendar_extracted alongside triage/claimed — otherwise
    a re-ingested bucket with new activity would never get a second shot at
    calendar-events extraction (see app/pipeline/calendar_events.py's module
    docstring §1 for why re-extracting is SAFE — event_key is a content
    hash, not source_id-based, so it upserts rather than duplicating)."""
    assert signals._STALE_ON_CONTENT_CHANGE == ["triage", "claimed", "calendar_extracted"]


def test_upsert_signal_unset_removes_stale_calendar_extracted(monkeypatch):
    db = _use_fake_db(
        monkeypatch,
        signals_docs=[
            {
                "source": "telegram", "source_id": "user_1:2026-08-03",
                "title": "T", "calendar_extracted": True,
            }
        ],
    )

    _run(
        signals.upsert_signal(
            {"source": "telegram", "source_id": "user_1:2026-08-03", "title": "T2"},
            unset=signals._STALE_ON_CONTENT_CHANGE,
        )
    )

    stored = db.signals.docs[0]
    assert stored["title"] == "T2"
    assert "calendar_extracted" not in stored


# ─────────────────────────────────────────────────────────────────────────────
# _bucket_changed (pure) — the "should we invalidate triage/claimed" check
# ─────────────────────────────────────────────────────────────────────────────


def test_bucket_changed_false_for_brand_new_bucket():
    assert signals._bucket_changed(None, [1, 2]) is False


def test_bucket_changed_false_when_message_ids_identical():
    existing = {"raw_ref": {"message_ids": [1, 2]}}
    assert signals._bucket_changed(existing, [1, 2]) is False


def test_bucket_changed_true_when_new_messages_appended():
    existing = {"raw_ref": {"message_ids": [1, 2]}}
    assert signals._bucket_changed(existing, [1, 2, 3]) is True


def test_bucket_changed_true_when_existing_has_no_raw_ref():
    assert signals._bucket_changed({"title": "T"}, [1]) is True


# ─────────────────────────────────────────────────────────────────────────────
# ingest_telegram_signals
# ─────────────────────────────────────────────────────────────────────────────


def test_ingest_telegram_signals_upserts_and_counts(monkeypatch):
    _use_utc_settings(monkeypatch)
    now = _dt(0)
    db = _use_fake_db(
        monkeypatch,
        raw_messages=[
            _msg("user_1", _dt(0, 9), "привет", 1),
            _msg("user_1", _dt(0, 10), "как дела", 2, direction="out"),
        ],
        chat_state=[{"chatId": "user_1", "title": "Дима"}],
    )

    count = _run(signals.ingest_telegram_signals(since=now - timedelta(hours=6)))

    assert count == 1
    assert len(db.signals.docs) == 1
    doc = db.signals.docs[0]
    assert doc["source"] == "telegram"
    assert doc["title"] == "Дима"
    assert doc["participants_raw"] == ["собеседник", "владелец"]
    assert doc["ingested_at"] is not None


def test_ingest_telegram_signals_rebuilds_from_the_full_day_not_just_the_delta(monkeypatch):
    """Correctness fix under test: a message from EARLIER in the day (before
    `since`) must still show up in the signal doc once a LATER message in
    the same day triggers a re-ingest — a delta-only rebuild would silently
    regress the doc back down to just the new message."""
    _use_utc_settings(monkeypatch)
    early = _dt(0, 8)
    late = _dt(0, 20)
    since_cursor = _dt(0, 12)  # strictly between early and late
    db = _use_fake_db(
        monkeypatch,
        raw_messages=[
            _msg("user_1", early, "рано утром", 1),
            _msg("user_1", late, "поздно вечером", 2),
        ],
        chat_state=[{"chatId": "user_1", "title": "T"}],
    )

    count = _run(signals.ingest_telegram_signals(since=since_cursor))

    assert count == 1
    doc = db.signals.docs[0]
    assert doc["raw_ref"]["message_ids"] == [1, 2]  # BOTH messages, not just the late one
    assert doc["ts_start"] == early
    assert doc["ts_end"] == late


def test_ingest_telegram_signals_reingest_is_idempotent(monkeypatch):
    _use_utc_settings(monkeypatch)
    now = _dt(0)
    db = _use_fake_db(
        monkeypatch,
        raw_messages=[_msg("user_1", _dt(0, 9), "hi", 1)],
        chat_state=[{"chatId": "user_1", "title": "T"}],
    )

    _run(signals.ingest_telegram_signals(since=now - timedelta(hours=6)))
    _run(signals.ingest_telegram_signals(since=now - timedelta(hours=6)))  # re-ingest tick

    assert len(db.signals.docs) == 1


def test_ingest_telegram_signals_reset_stays_untouched_when_bucket_content_unchanged(monkeypatch):
    """A re-ingest tick that touches a bucket but finds the SAME message set
    (e.g. a sibling chat's new message advanced the cursor, or the tick just
    re-ran) must NOT reset an already-computed triage/claimed — only an
    actual content change should invalidate them."""
    _use_utc_settings(monkeypatch)
    now = _dt(0)
    db = _use_fake_db(
        monkeypatch,
        raw_messages=[_msg("user_1", _dt(0, 9), "hi", 1)],
        chat_state=[{"chatId": "user_1", "title": "T"}],
        signals_docs=[
            {
                "source": "telegram", "source_id": f"user_1:{_dt(0).date().isoformat()}",
                "raw_ref": {"chat_id": "user_1", "date": _dt(0).date().isoformat(), "message_ids": [1]},
                "triage": {"verdict": "auto"}, "claimed": True,
            }
        ],
    )

    _run(signals.ingest_telegram_signals(since=now - timedelta(hours=6)))

    stored = db.signals.docs[0]
    assert stored["triage"] == {"verdict": "auto"}
    assert stored["claimed"] is True


def test_ingest_telegram_signals_resets_stale_triage_and_claimed_on_new_message(monkeypatch):
    """The correctness fix under test (see app/signals.py's "Stale
    triage/claimed invalidation" docstring section): a bucket already
    triaged+claimed earlier today must have BOTH flags cleared once a NEW
    message shows up in that same (chat, day) bucket, so the next
    triage/claims tick re-evaluates it instead of silently ignoring
    everything discussed after the first tick of the day."""
    _use_utc_settings(monkeypatch)
    now = _dt(0)
    today = _dt(0).date().isoformat()
    db = _use_fake_db(
        monkeypatch,
        raw_messages=[
            _msg("user_1", _dt(0, 9), "рано", 1),
            _msg("user_1", _dt(0, 20), "новое сообщение", 2),  # arrived after triage/claims
        ],
        chat_state=[{"chatId": "user_1", "title": "T"}],
        signals_docs=[
            {
                "source": "telegram", "source_id": f"user_1:{today}",
                "raw_ref": {"chat_id": "user_1", "date": today, "message_ids": [1]},
                "triage": {"verdict": "auto", "rubric_version": "v0"}, "claimed": True,
            }
        ],
    )

    count = _run(signals.ingest_telegram_signals(since=now - timedelta(hours=1)))

    assert count == 1
    stored = db.signals.docs[0]
    assert "triage" not in stored
    assert "claimed" not in stored
    assert stored["raw_ref"]["message_ids"] == [1, 2]  # full bucket still rebuilt correctly


def test_ingest_telegram_signals_multiple_chats_and_days_produce_distinct_buckets(monkeypatch):
    _use_utc_settings(monkeypatch)
    db = _use_fake_db(
        monkeypatch,
        raw_messages=[
            _msg("user_1", _dt(0, 9), "a", 1),
            _msg("user_1", _dt(1, 9), "b", 2),
            _msg("group_-2", _dt(0, 9), "c", 3),
        ],
        chat_state=[
            {"chatId": "user_1", "title": "Дима"},
            {"chatId": "group_-2", "title": "Группа"},
        ],
    )

    count = _run(signals.ingest_telegram_signals(since=_dt(2) - timedelta(hours=1)))

    assert count == 3
    source_ids = sorted(d["source_id"] for d in db.signals.docs)
    today = _dt(0).date().isoformat()
    yesterday = _dt(1).date().isoformat()
    assert source_ids == sorted([f"user_1:{today}", f"user_1:{yesterday}", f"group_-2:{today}"])


def test_ingest_telegram_signals_skips_bucket_with_missing_chat_id_without_raising(monkeypatch):
    _use_utc_settings(monkeypatch)
    now = _dt(0)
    bad = _msg("user_1", _dt(0, 9), "x", 1)
    bad["chatId"] = None
    db = _use_fake_db(monkeypatch, raw_messages=[bad])

    count = _run(signals.ingest_telegram_signals(since=now - timedelta(hours=6)))

    assert count == 0
    assert db.signals.docs == []


def test_ingest_telegram_signals_no_new_messages_returns_zero(monkeypatch):
    _use_utc_settings(monkeypatch)
    now = _dt(0)
    db = _use_fake_db(monkeypatch, raw_messages=[])

    count = _run(signals.ingest_telegram_signals(since=now - timedelta(hours=6)))

    assert count == 0
    assert db.signals.docs == []


# ─────────────────────────────────────────────────────────────────────────────
# signals-ingest cursor (repositories.get/set_signals_last_run_at, backed by
# bot_state)
# ─────────────────────────────────────────────────────────────────────────────


def test_signals_cursor_defaults_to_none_then_advances(monkeypatch):
    class FakeBotStateCollection:
        def __init__(self):
            self.docs: list[dict] = []

        async def find_one(self, query):
            for d in self.docs:
                if d.get("key") == query.get("key"):
                    return d
            return None

        async def update_one(self, query, update, upsert=False):
            for d in self.docs:
                if d.get("key") == query.get("key"):
                    d.update(update.get("$set", {}))
                    return
            if upsert:
                new_doc = dict(query)
                new_doc.update(update.get("$set", {}))
                self.docs.append(new_doc)

    class FakeDb:
        def __init__(self):
            self.bot_state = FakeBotStateCollection()

    db = FakeDb()
    monkeypatch.setattr(repo, "get_db", lambda: db)

    assert _run(repo.get_signals_last_run_at()) is None

    when = datetime(2026, 8, 3, 12, tzinfo=timezone.utc)
    _run(repo.set_signals_last_run_at(when))

    assert _run(repo.get_signals_last_run_at()) == when

    later = when + timedelta(minutes=10)
    _run(repo.set_signals_last_run_at(later))

    assert _run(repo.get_signals_last_run_at()) == later
    assert len(db.bot_state.docs) == 1  # updated in place, not accumulated
